# Smallville simulation store design

## Goals and invariants

The SQLite store is the durable system of record for persona movement and
associative memories. Existing movement JSON remains the frontend transport
and is not changed as a read contract; only its newest 500 files are retained.
The live simulation must continue if the store is unavailable, locked, corrupt,
or slow. Store hooks therefore fail open, report each failure/timeout class only
once, and may block the step loop for at most 100 ms.

Production data lives below
`environment/frontend_server/storage/public_sim/sim_store/`. Development and
verification must inject a temporary database/archive root and must never read
or write the production `storage/public_sim` tree. No service lifecycle action
is part of this change.

## Hot store

`sim_store.py` owns connection setup and schema creation. New databases use
WAL, `synchronous=NORMAL`, a short busy timeout, and incremental auto-vacuum.
The two tables are:

- `steps(persona, step, ts, sector, arena, tile_x, tile_y, action,
  description, chat, economy)` with `(step, persona)` as the primary key.
- `memories(persona, node_id, kind, text, embedding, poignancy, created_at,
  last_accessed)` with `(persona, node_id)` as the primary key.

The requested step/persona and memory poignancy/created indexes are explicit.
Each complete step is inserted with one `executemany` transaction. Store-only
metadata contains the current `personas_tile` coordinates and maze tile
sector/arena; it is never added to movement JSON. Structured chat and economy
values are stored as compact JSON text. Timestamps are normalized to sortable
ISO text.

Embeddings are encoded as little-endian IEEE-754 float32 bytes without NumPy.
`array`/`struct` and `math` keep the module standard-library-only. The deployed
virtualenv has no `zstandard`, so cold files use stdlib `lzma` and the `.xz`
suffix.

Full persona saves trigger an incremental memory sync. Existing node IDs and
last-access times are compared first so unchanged embeddings are not rewritten.
Associative-memory node IDs remain stable and may become sparse after eviction;
its loader, allocator, and saver therefore use stored numeric IDs rather than
assuming `node_1..node_N` is contiguous. All rewritten associative-memory JSON
files use temp-file plus `os.replace` atomicity.

## Query path

`query_memories(persona, query_embedding, k, filters)` first applies SQL
filters for kind, minimum poignancy, and creation/access recency, then caps the
candidate set at 2,000. It decodes float32 blobs, skips dimension mismatches or
zero vectors, calculates cosine similarity in-process, and returns the top-k
records with their scores. Existing cognitive retrieval continues to use the
in-memory associative hot set, so the simulation does not acquire a database
dependency.

## Cold archive and retention

Every 1,440 steps, maintenance computes a seven-game-day ISO timestamp cutoff.
`archive_old` streams matching step rows to a uniquely named NDJSON `.xz`
temporary file, fsyncs it, atomically renames it, and only then deletes the
exported rows. A failed delete can cause a later duplicate export but cannot
lose data. Incremental vacuum reclaims a bounded number of free pages. The
same maintenance pass removes movement JSON files older than the newest 500.

Memory eviction targets 1,000 nodes per persona, but safety guards take
precedence over the cap. A node is eligible only when all are true:

- it is at least 48 game-hours old;
- its poignancy is below 6;
- it is already reflected (a thought, or referenced by a thought's `filling`).

Eligible nodes rank by `poignancy * recency_decay ** age_hours`; the lowest are
evicted until the target is met. Protected nodes can therefore leave a persona
above 1,000. Evicted database rows are atomically archived as
`memories_archive_<date>...ndjson.xz` before deletion. The in-memory node
indexes and embedding map are pruned to the same kept-ID set and the resulting
`nodes.json`, `embeddings.json`, and keyword-strength state are atomically
saved.

## Loop isolation and observability

`reverie.py` changes are confined to `[DATA-STORE]` banner blocks. A small
dedicated executor runs hot-store and maintenance work. Each hook awaits at
most 100 ms; a timed-out job is not queued again while it remains in flight,
preventing unbounded backlog. Exceptions are contained and warnings are
deduplicated. Existing `STEPTIME` logging and step ordering remain intact.

The public `GET /get_sim_store_stats/` endpoint opens the database read-only
when it exists and returns database bytes, step/memory counts, and archive file
count. Missing or unreadable storage returns zeroed statistics rather than
creating files or surfacing an error.

## Verification

Offline verification uses the project virtualenv and only temporary
directories. It covers schema/batch insertion timing and counts, `.xz`
step-archive round trips and deletion, guarded eviction plus disk/in-memory
pruning, memory-archive counts, semantic top-k correctness, Python compilation,
and `manage.py check`. No test points at the default production path.
