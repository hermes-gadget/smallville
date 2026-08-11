"""Bounded SQLite persistence and cold archives for the live simulation.

The default paths point at the public simulation, but every public operation
accepts an injected connection/path so tests can stay entirely in temporary
directories.  This module intentionally depends only on the Python standard
library.
"""

import base64
import copy
import datetime
import json
import lzma
import math
import os
import re
import sqlite3
import struct
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout


_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
DEFAULT_SIM_ROOT = os.path.join(
    _PROJECT_ROOT, "environment", "frontend_server", "storage", "public_sim")
DEFAULT_STORE_DIR = os.path.join(DEFAULT_SIM_ROOT, "sim_store")
DEFAULT_DB_PATH = os.path.join(DEFAULT_STORE_DIR, "sim_store.db")
DEFAULT_ARCHIVE_DIR = os.path.join(DEFAULT_STORE_DIR, "archive")

MAX_QUERY_CANDIDATES = 2000
DEFAULT_MAX_MEMORIES = 1000
EVICTION_MIN_AGE_HOURS = 48
EVICTION_PROTECTED_POIGNANCY = 6.0
DEFAULT_RECENCY_DECAY = 0.995

_DT_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%B %d, %Y, %H:%M:%S",
)
_NODE_NUMBER = re.compile(r"^(?:node_)?(\d+)$")


class BoundedStore:
  """Fail-open runtime adapter whose calls wait for at most 100 ms."""

  def __init__(self, db_path=None, archive_dir=None, timeout=0.1):
    self.db_path = db_path
    self.archive_dir = archive_dir
    # Reserve a small amount of scheduler overhead inside the 100 ms wall cap.
    self.timeout = min(0.09, max(0.0, float(timeout)))
    self._executor = ThreadPoolExecutor(max_workers=2)
    self._pending = {}
    self._warned = set()
    self._step_conn = None

  def warn_once(self, key, message):
    if key not in self._warned:
      self._warned.add(key)
      print("[DATA-STORE] %s" % message, flush=True)

  def _call(self, label, function, *args):
    pending = self._pending.get(label)
    if pending is not None:
      if not pending.done():
        return None
      try:
        pending.result()
      except Exception as error:
        self.warn_once("error-" + label,
                       "%s failed (%s); continuing"
                       % (label, type(error).__name__))
      self._pending.pop(label, None)
    try:
      future = self._executor.submit(function, *args)
    except Exception as error:
      self.warn_once("error-" + label,
                     "%s failed (%s); continuing"
                     % (label, type(error).__name__))
      return None
    self._pending[label] = future
    try:
      result = future.result(timeout=self.timeout)
      self._pending.pop(label, None)
      return result
    except FutureTimeout:
      self.warn_once("timeout-" + label,
                     "%s reached 100 ms hard cap; continuing" % label)
      return None
    except Exception as error:
      self._pending.pop(label, None)
      self.warn_once("error-" + label,
                     "%s failed (%s); continuing"
                     % (label, type(error).__name__))
      return None

  def _connection(self):
    if self._step_conn is None:
      self._step_conn = connect(self.db_path)
    return self._step_conn

  def _write_step(self, step, personas_data, meta):
    return store_step(self._connection(), step, personas_data, meta)

  def store_step(self, step, personas_data, meta):
    return self._call("step", self._write_step, step, personas_data, meta)

  def _write_memories(self, snapshots):
    conn = connect(self.db_path)
    try:
      return {
          persona: store_memories(persona, memory, conn=conn)
          for persona, memory in snapshots.items()
      }
    finally:
      conn.close()

  def store_memories(self, snapshots):
    return self._call("memory", self._write_memories, snapshots)

  def archive_and_evict(self, cutoff, personas, sim_folder, now):
    snapshots = {
        name: copy.deepcopy(getattr(persona, "a_mem", persona))
        for name, persona in (personas or {}).items()
    }
    result = self._call(
        "maintenance", run_maintenance, cutoff, snapshots, sim_folder, now,
        self.db_path, self.archive_dir)
    if result is not None:
      self._apply_maintenance(result, personas, sim_folder)
    return result

  def poll_maintenance(self, personas, sim_folder):
    """Apply a completed background selection at a safe step boundary."""
    future = self._pending.get("maintenance")
    if future is None or not future.done():
      return None
    self._pending.pop("maintenance", None)
    try:
      result = future.result()
      self._apply_maintenance(result, personas, sim_folder)
      return result
    except Exception as error:
      self.warn_once("error-maintenance",
                     "maintenance failed (%s); continuing" %
                     type(error).__name__)
      return None

  def _apply_maintenance(self, result, personas, sim_folder):
    """Synchronously commit selected evictions without stale live refs."""
    deletions = []
    for persona_name, details in result.get("memories", {}).items():
      persona = (personas or {}).get(persona_name)
      if persona is None:
        continue
      memory = getattr(persona, "a_mem", persona)
      node_map, _ = _memory_parts(memory)
      live_ids = {str(node_id) for node_id in node_map}
      evicted_ids = {
          str(node_id) for node_id in details.get("evicted_ids", [])
          if str(node_id) in live_ids
      }
      if not evicted_ids:
        continue
      kept_ids = live_ids - evicted_ids
      _prune_memory_object(memory, kept_ids,
                           considered_ids=evicted_ids, save_dir=None)
      save_folder = os.path.join(
          sim_folder, "personas", persona_name, "bootstrap_memory")
      if hasattr(persona, "save"):
        persona.save(save_folder, full=True)
      deletions.extend((str(persona_name), node_id)
                       for node_id in evicted_ids)
    if deletions:
      conn = connect(self.db_path)
      try:
        with conn:
          conn.executemany(
              "DELETE FROM memories WHERE persona = ? AND node_id = ?",
              deletions)
      finally:
        conn.close()

  def close(self, wait=True):
    self._executor.shutdown(wait=wait)
    if self._step_conn is not None:
      self._step_conn.close()
      self._step_conn = None


def connect(db_path=None):
  """Open a configured store connection and initialize its schema."""
  if db_path is None:
    db_path = DEFAULT_DB_PATH
  db_path = os.path.abspath(os.fspath(db_path))
  os.makedirs(os.path.dirname(db_path), exist_ok=True)
  is_new = not os.path.exists(db_path)
  conn = sqlite3.connect(db_path, timeout=0.075, check_same_thread=False)
  try:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=75")
    if is_new:
      conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS steps(
          persona TEXT,
          step INTEGER,
          ts TEXT,
          sector TEXT,
          arena TEXT,
          tile_x INTEGER,
          tile_y INTEGER,
          action TEXT,
          description TEXT,
          chat TEXT,
          economy TEXT,
          PRIMARY KEY(step, persona)
        );
        CREATE TABLE IF NOT EXISTS memories(
          persona TEXT,
          node_id TEXT,
          kind TEXT,
          text TEXT,
          embedding BLOB,
          poignancy REAL,
          created_at TEXT,
          last_accessed TEXT,
          PRIMARY KEY(persona, node_id)
        );
        CREATE INDEX IF NOT EXISTS idx_steps_step ON steps(step);
        CREATE INDEX IF NOT EXISTS idx_steps_persona ON steps(persona);
        CREATE INDEX IF NOT EXISTS idx_memories_persona_poignancy
          ON memories(persona, poignancy DESC);
        CREATE INDEX IF NOT EXISTS idx_memories_created_at
          ON memories(created_at);
        """
    )
    conn.commit()
    return conn
  except Exception:
    conn.close()
    raise


def _parse_datetime(value):
  if value is None or value == "":
    return None
  if isinstance(value, datetime.datetime):
    return value
  if isinstance(value, datetime.date):
    return datetime.datetime.combine(value, datetime.time())
  value = str(value)
  for fmt in _DT_FORMATS:
    try:
      return datetime.datetime.strptime(value, fmt)
    except ValueError:
      pass
  try:
    return datetime.datetime.fromisoformat(value)
  except (TypeError, ValueError):
    return None


def _iso_datetime(value):
  parsed = _parse_datetime(value)
  if parsed is not None:
    return parsed.strftime("%Y-%m-%d %H:%M:%S")
  return "" if value is None else str(value)


def _json_text(value):
  if value is None:
    return None
  if isinstance(value, str):
    return value
  return json.dumps(value, ensure_ascii=False, separators=(",", ":"),
                    default=str)


def _value(source, key, default=None):
  if isinstance(source, dict):
    return source.get(key, default)
  return getattr(source, key, default)


def _tile_xy(tile):
  if isinstance(tile, dict):
    tile = (tile.get("x"), tile.get("y"))
  try:
    if tile is not None and len(tile) >= 2:
      return int(tile[0]), int(tile[1])
  except (TypeError, ValueError):
    pass
  return None, None


def store_step(conn, step, personas_data, meta):
  """Insert or replace every persona row for one step in one transaction."""
  meta = meta or {}
  persona_tiles = meta.get("personas_tile") or {}
  locations = meta.get("locations") or {}
  economies = meta.get("economy") or {}
  ts = _iso_datetime(meta.get("curr_time") or meta.get("ts"))
  rows = []
  for persona, state in (personas_data or {}).items():
    state = state or {}
    location = locations.get(persona) or {}
    tile = (state.get("tile") or persona_tiles.get(persona)
            or state.get("movement"))
    tile_x, tile_y = _tile_xy(tile)
    sector = state.get("sector") or location.get("sector")
    arena = state.get("arena") or location.get("arena")
    action = (state.get("action") or state.get("pronunciatio")
              or state.get("description"))
    economy = state.get("economy")
    if economy is None and isinstance(economies, dict):
      economy = economies.get(persona)
    rows.append((
        str(persona), int(step), ts, sector, arena, tile_x, tile_y,
        _json_text(action), _json_text(state.get("description")),
        _json_text(state.get("chat")), _json_text(economy),
    ))

  if not rows:
    return 0
  with conn:
    conn.executemany(
        """
        INSERT OR REPLACE INTO steps(
          persona, step, ts, sector, arena, tile_x, tile_y,
          action, description, chat, economy
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
  return len(rows)


def _float32_blob(embedding):
  if embedding is None:
    return b""
  if isinstance(embedding, memoryview):
    embedding = embedding.tobytes()
  if isinstance(embedding, (bytes, bytearray)):
    raw = bytes(embedding)
    if len(raw) % 4:
      raise ValueError("float32 embedding byte length must be divisible by 4")
    return raw
  values = [float(item) for item in embedding]
  if not values:
    return b""
  return struct.pack("<%sf" % len(values), *values)


def _decode_float32(blob):
  if blob is None:
    return ()
  raw = bytes(blob)
  if not raw or len(raw) % 4:
    return ()
  return struct.unpack("<%sf" % (len(raw) // 4), raw)


def _memory_parts(nodes):
  if hasattr(nodes, "id_to_node"):
    return nodes.id_to_node, getattr(nodes, "embeddings", {})
  if (isinstance(nodes, (tuple, list)) and len(nodes) == 2
      and isinstance(nodes[1], dict)):
    return nodes[0], nodes[1]
  return nodes, {}


def _memory_items(nodes):
  node_map, embeddings = _memory_parts(nodes)
  if isinstance(node_map, dict):
    items = list(node_map.items())
  else:
    items = [(_value(node, "node_id"), node) for node in (node_map or [])]
  return [(str(node_id), node) for node_id, node in items if node_id], embeddings


def _memory_metadata(node_id, node):
  kind = _value(node, "kind") or _value(node, "type") or ""
  text = _value(node, "text")
  if text is None:
    text = _value(node, "description", "")
  poignancy = _value(node, "poignancy", 0.0)
  created = _value(node, "created_at") or _value(node, "created")
  accessed = _value(node, "last_accessed") or created
  return {
      "node_id": str(node_id),
      "kind": str(kind),
      "text": str(text),
      "poignancy": float(poignancy or 0.0),
      "created_at": _iso_datetime(created),
      "last_accessed": _iso_datetime(accessed),
  }


def _node_embedding(node, embeddings):
  direct = _value(node, "embedding")
  if direct is not None:
    return direct
  embedding_key = _value(node, "embedding_key")
  if embedding_key is not None:
    return embeddings.get(embedding_key)
  return None


def store_memories(persona, nodes, conn=None):
  """Incrementally upsert a persona's nodes and float32 embeddings."""
  owns_connection = conn is None
  if owns_connection:
    conn = connect()
  try:
    existing = {
        row["node_id"]: row
        for row in conn.execute(
            "SELECT node_id, kind, text, poignancy, created_at, last_accessed "
            "FROM memories WHERE persona = ?", (str(persona),))
    }
    inserts = []
    updates = []
    items, embeddings = _memory_items(nodes)
    for node_id, node in items:
      metadata = _memory_metadata(node_id, node)
      old = existing.get(node_id)
      comparable = (
          metadata["kind"], metadata["text"], metadata["poignancy"],
          metadata["created_at"], metadata["last_accessed"],
      )
      if old is None:
        inserts.append((
            str(persona), node_id, metadata["kind"], metadata["text"],
            sqlite3.Binary(_float32_blob(_node_embedding(node, embeddings))),
            metadata["poignancy"], metadata["created_at"],
            metadata["last_accessed"],
        ))
      elif comparable != (
          old["kind"], old["text"], old["poignancy"],
          old["created_at"], old["last_accessed"],
      ):
        updates.append((
            metadata["kind"], metadata["text"], metadata["poignancy"],
            metadata["created_at"], metadata["last_accessed"],
            str(persona), node_id,
        ))

    if inserts or updates:
      with conn:
        if inserts:
          conn.executemany(
              """
              INSERT OR REPLACE INTO memories(
                persona, node_id, kind, text, embedding, poignancy,
                created_at, last_accessed
              ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
              """,
              inserts,
          )
        if updates:
          conn.executemany(
              """
              UPDATE memories
              SET kind = ?, text = ?, poignancy = ?, created_at = ?,
                  last_accessed = ?
              WHERE persona = ? AND node_id = ?
              """,
              updates,
          )
    return {"inserted": len(inserts), "updated": len(updates),
            "unchanged": len(items) - len(inserts) - len(updates)}
  finally:
    if owns_connection:
      conn.close()


def query_memories(persona, query_embedding, k=10, filters=None, conn=None):
  """Cosine-rank up to 2,000 SQL-filtered memory candidates."""
  filters = filters or {}
  owns_connection = conn is None
  if owns_connection:
    conn = connect()
  try:
    clauses = ["persona = ?"]
    params = [str(persona)]
    kinds = filters.get("kinds") or filters.get("kind")
    if kinds:
      if isinstance(kinds, str):
        kinds = [kinds]
      kinds = list(kinds)
      clauses.append("kind IN (%s)" % ",".join("?" for _ in kinds))
      params.extend(kinds)
    if filters.get("min_poignancy") is not None:
      clauses.append("poignancy >= ?")
      params.append(float(filters["min_poignancy"]))
    created_after = filters.get("created_after") or filters.get("since")
    if created_after is not None:
      clauses.append("created_at >= ?")
      params.append(_iso_datetime(created_after))
    if filters.get("last_accessed_after") is not None:
      clauses.append("last_accessed >= ?")
      params.append(_iso_datetime(filters["last_accessed_after"]))
    limit = max(1, min(MAX_QUERY_CANDIDATES,
                       int(filters.get("candidate_limit",
                                       MAX_QUERY_CANDIDATES))))
    params.append(limit)
    rows = conn.execute(
        "SELECT persona, node_id, kind, text, embedding, poignancy, "
        "created_at, last_accessed FROM memories WHERE %s "
        "ORDER BY poignancy DESC, last_accessed DESC LIMIT ?"
        % " AND ".join(clauses),
        params,
    ).fetchall()

    query = tuple(float(value) for value in query_embedding)
    query_norm = math.sqrt(sum(value * value for value in query))
    if not query or query_norm == 0:
      return []
    ranked = []
    for row in rows:
      embedding = _decode_float32(row["embedding"])
      if len(embedding) != len(query) or not embedding:
        continue
      norm = math.sqrt(sum(value * value for value in embedding))
      if norm == 0:
        continue
      similarity = (sum(a * b for a, b in zip(embedding, query))
                    / (norm * query_norm))
      record = {key: row[key] for key in row.keys() if key != "embedding"}
      record["similarity"] = similarity
      ranked.append(record)
    ranked.sort(key=lambda item: item["similarity"], reverse=True)
    return ranked[:max(0, int(k))]
  finally:
    if owns_connection:
      conn.close()


def _archive_record(row):
  record = {key: row[key] for key in row.keys()}
  if "embedding" in record:
    record["embedding"] = base64.b64encode(
        bytes(record["embedding"] or b"")).decode("ascii")
    record["embedding_encoding"] = "base64-float32-le"
  return record


def _unique_archive_path(archive_dir, stem):
  os.makedirs(archive_dir, exist_ok=True)
  candidate = os.path.join(archive_dir, stem + ".ndjson.xz")
  counter = 1
  while os.path.exists(candidate):
    candidate = os.path.join(
        archive_dir, "%s_%04d.ndjson.xz" % (stem, counter))
    counter += 1
  return candidate


def _atomic_xz_dump(records, destination):
  os.makedirs(os.path.dirname(destination), exist_ok=True)
  fd, temporary = tempfile.mkstemp(
      prefix="." + os.path.basename(destination) + ".",
      suffix=".tmp", dir=os.path.dirname(destination))
  os.close(fd)
  count = 0
  try:
    with lzma.open(temporary, "wt", encoding="utf-8", preset=6) as archive:
      for record in records:
        archive.write(json.dumps(record, ensure_ascii=False,
                                 separators=(",", ":"), default=str))
        archive.write("\n")
        count += 1
    with open(temporary, "rb") as archive_file:
      os.fsync(archive_file.fileno())
    os.replace(temporary, destination)
    return count
  except Exception:
    try:
      os.unlink(temporary)
    except OSError:
      pass
    raise


def archive_old(conn, cutoff, archive_dir):
  """Archive and delete step rows older than an integer or timestamp cutoff."""
  if isinstance(cutoff, int):
    where = "step < ?"
    value = cutoff
  else:
    where = "ts < ?"
    value = _iso_datetime(cutoff)
  summary = conn.execute(
      "SELECT COUNT(*) AS count, MIN(step) AS first_step, "
      "MAX(step) AS last_step FROM steps WHERE " + where, (value,)
  ).fetchone()
  count = int(summary["count"] or 0)
  if not count:
    return {"archived": 0, "deleted": 0, "path": None}

  stem = "steps_archive_%s_%s" % (
      summary["first_step"], summary["last_step"])
  destination = _unique_archive_path(archive_dir, stem)
  cursor = conn.execute(
      "SELECT persona, step, ts, sector, arena, tile_x, tile_y, action, "
      "description, chat, economy FROM steps WHERE " + where
      + " ORDER BY step, persona", (value,))
  archived = _atomic_xz_dump((_archive_record(row) for row in cursor),
                             destination)
  deleted = 0
  while True:
    with conn:
      batch_deleted = conn.execute(
          "DELETE FROM steps WHERE rowid IN ("
          "SELECT rowid FROM steps WHERE " + where + " LIMIT 5000)",
          (value,),
      ).rowcount
    deleted += batch_deleted
    if batch_deleted < 5000:
      break
    # Let the per-step WAL writer acquire the lock between bounded batches.
    time.sleep(0.005)
  try:
    conn.execute("PRAGMA incremental_vacuum(2000)")
  except sqlite3.DatabaseError:
    pass
  return {"archived": archived, "deleted": deleted, "path": destination}


def _node_sort_key(item):
  node_id, node = item
  count = _value(node, "node_count")
  if count is not None:
    try:
      return int(count)
    except (TypeError, ValueError):
      pass
  match = _NODE_NUMBER.match(str(node_id))
  return int(match.group(1)) if match else 0


def _node_json(node_id, node):
  created = _value(node, "created_at") or _value(node, "created")
  expiration = _value(node, "expiration")
  return {
      "node_count": _value(node, "node_count", _node_sort_key((node_id, node))),
      "type_count": _value(node, "type_count", 0),
      "type": _value(node, "type") or _value(node, "kind", ""),
      "depth": _value(node, "depth", 0),
      "created": _iso_datetime(created),
      "expiration": _iso_datetime(expiration) if expiration else None,
      "subject": _value(node, "subject", ""),
      "predicate": _value(node, "predicate", ""),
      "object": _value(node, "object", ""),
      "description": _value(node, "description", _value(node, "text", "")),
      "embedding_key": _value(node, "embedding_key", node_id),
      "poignancy": float(_value(node, "poignancy", 0.0) or 0.0),
      "keywords": list(_value(node, "keywords", []) or []),
      "filling": _value(node, "filling"),
  }


def _atomic_json_dump(value, destination):
  os.makedirs(os.path.dirname(destination), exist_ok=True)
  fd, temporary = tempfile.mkstemp(
      prefix="." + os.path.basename(destination) + ".",
      suffix=".tmp", dir=os.path.dirname(destination), text=True)
  try:
    with os.fdopen(fd, "w", encoding="utf-8") as outfile:
      json.dump(value, outfile, ensure_ascii=False)
      outfile.flush()
      os.fsync(outfile.fileno())
    os.replace(temporary, destination)
  except Exception:
    try:
      os.unlink(temporary)
    except OSError:
      pass
    raise


def _prune_memory_object(memory, kept_ids, considered_ids=None, save_dir=None):
  node_map, embeddings = _memory_parts(memory)
  if not isinstance(node_map, dict):
    raise TypeError("eviction requires a mutable node mapping")
  kept_ids = set(kept_ids)
  considered_ids = {str(node_id) for node_id in (considered_ids or node_map)}
  for node_id in list(node_map):
    if str(node_id) in considered_ids and str(node_id) not in kept_ids:
      del node_map[node_id]

  # Nodes created while a background archive was compressing were not part of
  # the ranking snapshot and must never be mistaken for eviction candidates.
  kept_ids = {str(node_id) for node_id in node_map}

  for attr in ("seq_event", "seq_thought", "seq_chat"):
    sequence = getattr(memory, attr, None)
    if sequence is not None:
      setattr(memory, attr, [node for node in sequence
                             if str(_value(node, "node_id")) in kept_ids])
  for attr in ("kw_to_event", "kw_to_thought", "kw_to_chat"):
    keyword_map = getattr(memory, attr, None)
    if keyword_map is not None:
      setattr(memory, attr, {
          keyword: [node for node in sequence
                    if str(_value(node, "node_id")) in kept_ids]
          for keyword, sequence in keyword_map.items()
          if any(str(_value(node, "node_id")) in kept_ids
                 for node in sequence)
      })

  used_embedding_keys = {
      _value(node, "embedding_key")
      for node in node_map.values()
      if _value(node, "embedding_key") is not None
  }
  if isinstance(embeddings, dict):
    for embedding_key in list(embeddings):
      if embedding_key not in used_embedding_keys:
        del embeddings[embedding_key]

  for kind, attr in (("event", "kw_strength_event"),
                     ("thought", "kw_strength_thought")):
    if hasattr(memory, attr):
      strengths = {}
      for node in node_map.values():
        if (_value(node, "kind") or _value(node, "type")) != kind:
          continue
        predicate = _value(node, "predicate", "")
        object_value = _value(node, "object", "")
        if "%s %s" % (predicate, object_value) == "is idle":
          continue
        for keyword in (_value(node, "keywords", []) or []):
          keyword = str(keyword).lower()
          strengths[keyword] = strengths.get(keyword, 0) + 1
      setattr(memory, attr, strengths)

  if save_dir:
    if hasattr(memory, "save"):
      memory.save(save_dir)
    else:
      ordered = sorted(node_map.items(), key=_node_sort_key, reverse=True)
      _atomic_json_dump(
          {str(node_id): _node_json(node_id, node)
           for node_id, node in ordered},
          os.path.join(save_dir, "nodes.json"),
      )
      _atomic_json_dump(embeddings, os.path.join(save_dir, "embeddings.json"))


def evict_memories(persona, nodes, archive_dir, max_nodes=DEFAULT_MAX_MEMORIES,
                   conn=None, save_dir=None, now=None,
                   recency_decay=DEFAULT_RECENCY_DECAY, delete_rows=True):
  """Archive and prune only old, low-poignancy, reflected memory nodes."""
  items, embeddings = _memory_items(nodes)
  total = len(items)
  max_nodes = max(0, int(max_nodes))
  if total <= max_nodes:
    return {"kept": total, "pruned": 0, "archived": 0, "path": None,
            "evicted_ids": []}

  now = _parse_datetime(now) or datetime.datetime.utcnow()
  reflected = set()
  for node_id, node in items:
    if (_value(node, "kind") or _value(node, "type")) == "thought":
      reflected.add(node_id)
      reflected.update(str(value) for value in (_value(node, "filling") or []))

  protected = set()
  eligible = []
  for node_id, node in items:
    metadata = _memory_metadata(node_id, node)
    created = _parse_datetime(metadata["created_at"])
    age_hours = ((now - created).total_seconds() / 3600.0
                 if created is not None else 0.0)
    is_protected = (
        age_hours < EVICTION_MIN_AGE_HOURS
        or metadata["poignancy"] >= EVICTION_PROTECTED_POIGNANCY
        or node_id not in reflected
    )
    if is_protected:
      protected.add(node_id)
    else:
      score = metadata["poignancy"] * (float(recency_decay) ** age_hours)
      eligible.append((score, node_id, node))

  eligible.sort(key=lambda item: (item[0], _node_sort_key((item[1], item[2]))),
                reverse=True)
  available_slots = max(0, max_nodes - len(protected))
  kept_eligible = {node_id for _, node_id, _ in eligible[:available_slots]}
  kept_ids = protected | kept_eligible
  evicted = [(node_id, node) for _, node_id, node in eligible[available_slots:]]
  if not evicted:
    return {"kept": total, "pruned": 0, "archived": 0, "path": None,
            "evicted_ids": []}

  date_part = now.strftime("%Y-%m-%d")
  destination = _unique_archive_path(
      archive_dir, "memories_archive_%s" % date_part)

  def records():
    for node_id, node in evicted:
      metadata = _memory_metadata(node_id, node)
      yield {
          "persona": str(persona),
          "node_id": node_id,
          "kind": metadata["kind"],
          "text": metadata["text"],
          "embedding": base64.b64encode(
              _float32_blob(_node_embedding(node, embeddings))).decode("ascii"),
          "embedding_encoding": "base64-float32-le",
          "poignancy": metadata["poignancy"],
          "created_at": metadata["created_at"],
          "last_accessed": metadata["last_accessed"],
      }

  archived = _atomic_xz_dump(records(), destination)
  if delete_rows:
    owns_connection = conn is None
    if owns_connection:
      conn = connect()
    try:
      with conn:
        conn.executemany(
            "DELETE FROM memories WHERE persona = ? AND node_id = ?",
            [(str(persona), node_id) for node_id, _ in evicted],
        )
    finally:
      if owns_connection:
        conn.close()

  _prune_memory_object(
      nodes, kept_ids, considered_ids={node_id for node_id, _ in items},
      save_dir=save_dir)
  return {"kept": len(_memory_items(nodes)[0]), "pruned": len(evicted),
          "archived": archived, "path": destination,
          "evicted_ids": [node_id for node_id, _ in evicted]}


def prune_json_tail(directory, keep=500):
  """Delete numeric movement JSON files older than the newest ``keep``."""
  keep = max(0, int(keep))
  if not os.path.isdir(directory):
    return 0
  numbered = []
  for filename in os.listdir(directory):
    stem, extension = os.path.splitext(filename)
    if extension == ".json" and stem.isdigit():
      numbered.append((int(stem), os.path.join(directory, filename)))
  numbered.sort()
  old = numbered[:-keep] if keep else numbered
  removed = 0
  for _, path in old:
    try:
      os.unlink(path)
      removed += 1
    except FileNotFoundError:
      pass
  return removed


def run_maintenance(cutoff, memory_snapshots, sim_folder, now=None,
                    db_path=None, archive_dir=None,
                    max_nodes=DEFAULT_MAX_MEMORIES, movement_keep=500):
  """Run one archive/eviction/tail-pruning maintenance pass."""
  if db_path is None:
    db_path = DEFAULT_DB_PATH
  if archive_dir is None:
    archive_dir = DEFAULT_ARCHIVE_DIR
  conn = connect(db_path)
  try:
    result = {"steps": None, "memories": {}, "movement_files_pruned": 0}
    for persona_name, memory in (memory_snapshots or {}).items():
      result["memories"][persona_name] = evict_memories(
          persona_name, memory, archive_dir, max_nodes=max_nodes, conn=conn,
          save_dir=None, now=now, delete_rows=False)
    result["steps"] = archive_old(conn, cutoff, archive_dir)
    result["movement_files_pruned"] = prune_json_tail(
        os.path.join(sim_folder, "movement"), movement_keep)
    return result
  finally:
    conn.close()
