"""Offline smoke coverage for sim_store (temporary directories only)."""

import datetime
import json
import lzma
import os
import sys
import tempfile
import time
import unittest


BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if BACKEND_DIR not in sys.path:
  sys.path.insert(0, BACKEND_DIR)

import sim_store
from persona.memory_structures.associative_memory import AssociativeMemory


class FakeNode:
  def __init__(self, number, kind, created, poignancy, embedding, filling=None):
    self.node_id = "node_%d" % number
    self.node_count = number
    self.type_count = number
    self.type = kind
    self.depth = 1 if kind == "thought" else 0
    self.created = created
    self.expiration = None
    self.last_accessed = created
    self.subject = "Alice"
    self.predicate = "remembers"
    self.object = self.node_id
    self.description = "memory %d" % number
    self.embedding_key = "embedding_%d" % number
    self.poignancy = poignancy
    self.keywords = {"memory", str(number)}
    self.filling = filling or []
    self.embedding = embedding


class FakeMemory:
  def __init__(self, nodes):
    self.id_to_node = {node.node_id: node for node in nodes}
    self.seq_event = [node for node in reversed(nodes) if node.type == "event"]
    self.seq_thought = [node for node in reversed(nodes)
                        if node.type == "thought"]
    self.seq_chat = []
    self.kw_to_event = {"memory": list(self.seq_event)}
    self.kw_to_thought = {"memory": list(self.seq_thought)}
    self.kw_to_chat = {}
    self.kw_strength_event = {"memory": len(self.seq_event)}
    self.kw_strength_thought = {"memory": len(self.seq_thought)}
    self.embeddings = {node.embedding_key: node.embedding for node in nodes}


def read_xz_records(path):
  with lzma.open(path, "rt", encoding="utf-8") as archive:
    return [json.loads(line) for line in archive if line.strip()]


class SimStoreSmokeTest(unittest.TestCase):
  def setUp(self):
    self.temporary = tempfile.TemporaryDirectory(prefix="smallville-store-")
    self.root = self.temporary.name
    self.db_path = os.path.join(self.root, "sim_store.db")
    self.archive_dir = os.path.join(self.root, "archive")
    self.conn = sim_store.connect(self.db_path)

  def tearDown(self):
    self.conn.close()
    self.temporary.cleanup()

  def test_step_batch_archive_and_round_trip(self):
    personas = ("Alice", "Bob", "Cara")
    started = datetime.datetime(2026, 1, 1, 8, 0, 0)
    for step in range(50):
      states = {
          name: {
              "movement": [step + offset, step + offset + 1],
              "pronunciatio": "walk",
              "description": "%s walking" % name,
              "chat": [[name, "hello"]] if step % 10 == 0 else None,
              "economy": {"coins": step + offset},
          }
          for offset, name in enumerate(personas)
      }
      meta = {
          "curr_time": started + datetime.timedelta(minutes=step),
          "personas_tile": {
              name: (step + offset, step + offset + 1)
              for offset, name in enumerate(personas)
          },
          "locations": {
              name: {"sector": "sector-%d" % offset,
                     "arena": "arena-%d" % offset}
              for offset, name in enumerate(personas)
          },
      }
      self.assertEqual(sim_store.store_step(self.conn, step, states, meta), 3)

    batch = {
        "Persona %02d" % index: {
            "movement": [index, index + 1], "description": "moving"
        }
        for index in range(25)
    }
    before = time.perf_counter()
    self.assertEqual(sim_store.store_step(
        self.conn, 50, batch, {"curr_time": started}), 25)
    batch_ms = (time.perf_counter() - before) * 1000.0
    self.assertLess(batch_ms, 50.0)

    self.assertEqual(
        self.conn.execute("SELECT COUNT(*) FROM steps").fetchone()[0], 175)
    sample = self.conn.execute(
        "SELECT sector, arena, tile_x, tile_y, economy FROM steps "
        "WHERE step = 10 AND persona = 'Bob'").fetchone()
    self.assertEqual(tuple(sample[:4]), ("sector-1", "arena-1", 11, 12))
    self.assertEqual(json.loads(sample[4]), {"coins": 11})

    archived = sim_store.archive_old(self.conn, 25, self.archive_dir)
    self.assertEqual(archived["archived"], 75)
    self.assertEqual(archived["deleted"], 75)
    self.assertTrue(archived["path"].endswith(".ndjson.xz"))
    self.assertTrue(os.path.exists(archived["path"]))
    records = read_xz_records(archived["path"])
    self.assertEqual(len(records), 75)
    self.assertTrue(all(record["step"] < 25 for record in records))
    self.assertEqual(
        self.conn.execute("SELECT COUNT(*) FROM steps").fetchone()[0], 100)

    movement_dir = os.path.join(self.root, "movement")
    os.makedirs(movement_dir)
    for step in range(505):
      with open(os.path.join(movement_dir, "%d.json" % step), "w") as outfile:
        outfile.write("{}")
    self.assertEqual(sim_store.prune_json_tail(movement_dir, keep=500), 5)
    self.assertFalse(os.path.exists(os.path.join(movement_dir, "4.json")))
    self.assertTrue(os.path.exists(os.path.join(movement_dir, "5.json")))
    print("SMOKE steps=175 batch_25_ms=%.3f archived=75 remaining=100 "
          "xz_round_trip=75 movement_tail=500" % batch_ms)

  def test_runtime_wait_is_hard_capped(self):
    runtime = sim_store.BoundedStore(db_path=self.db_path)
    before = time.perf_counter()
    result = runtime._call("slow-smoke", time.sleep, 0.2)
    elapsed_ms = (time.perf_counter() - before) * 1000.0
    self.assertIsNone(result)
    self.assertLess(elapsed_ms, 150.0)
    runtime.close()
    print("SMOKE runtime_timeout_ms=%.3f hard_cap_ms=100 fail_open=true" %
          elapsed_ms)

  def test_semantic_query_guarded_eviction_and_pruned_files(self):
    now = datetime.datetime(2026, 1, 10, 12, 0, 0)
    old = now - datetime.timedelta(hours=200)
    young = now - datetime.timedelta(hours=24)
    nodes = [
        FakeNode(1, "event", old, 5, [1.0, 0.0, 0.0]),
        FakeNode(2, "event", old, 4, [0.8, 0.2, 0.0]),
        FakeNode(3, "thought", old, 3, [0.0, 1.0, 0.0],
                 filling=["node_1", "node_2"]),
        FakeNode(4, "thought", old, 2, [0.0, 0.8, 0.2]),
        FakeNode(5, "event", old, 7, [0.9, 0.1, 0.0]),
        FakeNode(6, "event", young, 1, [0.0, 0.0, 1.0]),
        FakeNode(7, "event", old, 1, [0.1, 0.1, 0.8]),
        FakeNode(8, "thought", old, 1, [0.2, 0.7, 0.1]),
    ]
    memory = FakeMemory(nodes)
    sync = sim_store.store_memories("Alice", memory, conn=self.conn)
    self.assertEqual(sync["inserted"], 8)
    unchanged = sim_store.store_memories("Alice", memory, conn=self.conn)
    self.assertEqual(unchanged, {"inserted": 0, "updated": 0,
                                 "unchanged": 8})

    ranked = sim_store.query_memories(
        "Alice", [1.0, 0.0, 0.0], 2,
        {"min_poignancy": 1, "candidate_limit": 20}, conn=self.conn)
    self.assertEqual(ranked[0]["node_id"], "node_1")
    self.assertEqual(len(ranked), 2)
    self.assertGreater(ranked[0]["similarity"], ranked[1]["similarity"])

    save_dir = os.path.join(self.root, "bootstrap_memory", "associative_memory")
    evicted = sim_store.evict_memories(
        "Alice", memory, self.archive_dir, max_nodes=5, conn=self.conn,
        save_dir=save_dir, now=now)
    self.assertEqual(evicted["kept"], 5)
    self.assertEqual(evicted["pruned"], 3)
    self.assertEqual(evicted["archived"], 3)
    self.assertEqual(os.path.basename(evicted["path"]),
                     "memories_archive_2026-01-10.ndjson.xz")
    self.assertEqual(set(memory.id_to_node),
                     {"node_1", "node_2", "node_5", "node_6", "node_7"})
    self.assertIn("node_5", memory.id_to_node)  # poignancy guard
    self.assertIn("node_6", memory.id_to_node)  # 48-hour guard
    self.assertIn("node_7", memory.id_to_node)  # reflection guard
    self.assertEqual(
        self.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0], 5)
    self.assertEqual(len(read_xz_records(evicted["path"])), 3)

    with open(os.path.join(save_dir, "nodes.json"), encoding="utf-8") as infile:
      saved_nodes = json.load(infile)
    with open(os.path.join(save_dir, "embeddings.json"),
              encoding="utf-8") as infile:
      saved_embeddings = json.load(infile)
    self.assertEqual(set(saved_nodes), set(memory.id_to_node))
    self.assertEqual(len(saved_embeddings), 5)
    print("SMOKE memories=8 top_k=%s kept=5 pruned=3 archived=3 "
          "nodes_json=5 embeddings_json=5" %
          [record["node_id"] for record in ranked])

  def test_sparse_associative_memory_round_trip_and_allocator(self):
    memory_dir = os.path.join(self.root, "sparse-memory")
    os.makedirs(memory_dir)
    common = {
        "expiration": None, "subject": "Alice", "predicate": "saw",
        "object": "rain", "description": "Alice saw rain",
        "poignancy": 2, "keywords": ["rain"], "filling": [],
        "created": "2026-01-01 08:00:00",
        "last_accessed": "2026-01-02 08:00:00",
    }
    first = dict(common, node_count=1, type_count=1, type="event", depth=0,
                 embedding_key="event-1")
    third = dict(common, node_count=3, type_count=1, type="thought", depth=1,
                 embedding_key="thought-3", filling=["node_1"])
    with open(os.path.join(memory_dir, "nodes.json"), "w") as outfile:
      json.dump({"node_3": third, "node_1": first}, outfile)
    with open(os.path.join(memory_dir, "embeddings.json"), "w") as outfile:
      json.dump({"event-1": [1, 0], "thought-3": [0, 1]}, outfile)
    with open(os.path.join(memory_dir, "kw_strength.json"), "w") as outfile:
      json.dump({"kw_strength_event": {}, "kw_strength_thought": {}}, outfile)

    memory = AssociativeMemory(memory_dir)
    self.assertEqual(set(memory.id_to_node), {"node_1", "node_3"})
    added = memory.add_event(
        datetime.datetime(2026, 1, 3, 8), None, "Alice", "saw", "sun",
        "Alice saw sun", {"sun"}, 3, ("event-4", [0.5, 0.5]), [])
    self.assertEqual(added.node_id, "node_4")
    memory.save(memory_dir)
    reloaded = AssociativeMemory(memory_dir)
    self.assertEqual(set(reloaded.id_to_node), {"node_1", "node_3", "node_4"})
    self.assertEqual(reloaded.id_to_node["node_1"].last_accessed,
                     datetime.datetime(2026, 1, 2, 8))
    del reloaded.id_to_node["node_4"]
    reloaded.seq_event = [node for node in reloaded.seq_event
                          if node.node_id != "node_4"]
    del reloaded.embeddings["event-4"]
    reloaded.save(memory_dir)
    after_eviction = AssociativeMemory(memory_dir)
    next_node = after_eviction.add_event(
        datetime.datetime(2026, 1, 4, 8), None, "Alice", "saw", "moon",
        "Alice saw moon", {"moon"}, 3, ("event-5", [0.2, 0.8]), [])
    self.assertEqual(next_node.node_id, "node_5")


if __name__ == "__main__":
  unittest.main(verbosity=2)
