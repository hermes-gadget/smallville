# Smallville — Town Operations Plan

> Live: `smallville.justarobot.uk` (Cloudflare tunnel → local VM `192.168.2.5:8000`) ✅ VERIFIED 200
> Repo: `/home/ben/smallville` → `hermes-gadget/smallville` (branch `main`)
> Sim backend: `smallville-reverie.service` · Frontend: `smallville-django.service` (user units, boot-enabled)

## Goal

A coherent, self-running AI town at **maximum 25 residents** that lives at a watchable
real-time pace, with a message feed showing what agents talk about, a polished UI, and a
hard guard on LLM spend.

## Status: ALL TASKS DONE ✅ + PERF PASS + LIFE SIM + DATA STORE (2026-08-08)

### Merged agent wave (2026-08-08, main b6c6a136; fixed 19a5abdb + 448c5b0f)
- **Landing page** (ui-wave-1): public one-pager at site root — hero, live town
  stats ticker (tokens/calls/conversations), features, how-it-works. `^$` → landing.
- **Sim UI polish** (ui-wave-2): resident modal, chat-panel mobile collapse +
  unread badge, loading/offline states, 320-768px audit, dead-code cleanup.
- **Life sim** (life-sim): `reverie/backend_server/economy.py` (1047 lines) —
  balances/tiers, jobs+salaries (per game-minute at workplace), 4 shops with
  goods/stock/cash, purchases by action keyword, banker **Yuriko Yamamoto**
  (treasury, 0.25% daily interest, bankruptcy declarations, debt, homeless flag
  after 3x), rent, hunger/energy/social + illness, education score, bounded
  economy feed + `economy` field in movement payload; admin command queue
  (give_money/bankrupt/make_rich/inject_event/broadcast/restock/adjust_price/
  add_good/transfer/set_pacing) via POST `/admin/command/` (token-gated);
  public GET `/get_economy/` + `/get_economy_feed/`; step cap → 1,000,000,000;
  movement pruning (keep newest 500). Smoke: tick avg 5.5ms.
- **SQLite system-of-record** (data-store): `sim_store.py` — steps + memories
  (embedding BLOBs) in WAL SQLite at storage/public_sim/sim_store/sim_store.db,
  lzma cold archives (7-day hot window), poignancy×recency eviction (keep 1000/
  persona, guards: <48h or poignancy≥6 never evicted), sparse node-ID allocators
  in associative_memory.py, GET `/get_sim_store_stats/`. Fail-open hooks, 100ms
  hard cap. Live: 113MB DB, 2.6K steps + 26.7K memories and growing.
- **Post-merge fixes**: get_economy missing return (merge conflict casualty);
  **meta.json persistence** — pre-existing bug where save() only ran at run end,
  so every systemd restart rewound the town to the original fork date/step 0.
  Now persisted every 10 steps + graceful SIGTERM save. Verified: restart
  resumed at step 60 (was rewinding to 0 every time).

### Verified live (2026-08-08 late)
- Town stepping ~1.4s/step, resumes in place across restarts.
- Economy: 25 residents with balances, salaries streaming to the feed
  ('Yuriko Yamamoto earned $0.41 in salary as town banker'), banker + treasury
  active (bank cash 20,000 → 19,762), 4 shops stocked.
- All endpoints 200: landing, simulator_home, get_economy, get_economy_feed,
  get_sim_store_stats, get_chat_log; /set_pacing/ + /admin/command/ still 403
  without the admin token.

### Perf pass (2026-08-08, commit e094e8b9)
- **~4x faster steps**: perceive split into `perceive_collect` (sequential, LLM-free)
  + `perceive_commit` (poignancy scoring + memory adds) which now runs INSIDE the
  parallel decide phase. Chat-heavy steps: 4-6s → 0.1-3s. Steady state ≈ 1.4s/step.
- **Day-rollover crash fixed**: `ChatGPT_single_request` was called by
  `revise_identity` but never defined — every in-place rollover NameError'd and
  re-anchored the clock backwards. Now defined (single-shot chat call).
- **Reflection budget**: max 6 reflections/step (was: boot storm of 200+ calls in
  one 233s step).
- **Save throttling**: scratch.json every step, full memory dump every 10 steps
  (embeddings.json ~250KB × 25 personas was rewriting every step).
- **Journal quiet**: ~40 debug prints removed, `debug=False` (was 38K lines/90s).
- **Selection ring fixed**: rewritten as Graphics ellipse @ depth 3 (Shape ellipses
  don't render in some WebGL envs; old depth 0.9 hid it under roofs; camera also
  centered selections behind the chat panel). Ring pixel-verified.
- **Camera follow**: selected resident tracked live (sprite kept at 40% viewport
  height, clear of the chat panel; user pan/keys suspend follow for 2.5s).
- **World speed API**: POST `/set_pacing/` `{"pacing": N}` (1..10000) — live flip,
  no restart. **ADMIN-ONLY**: requires `X-Admin-Token` header matching
  `/home/ben/.smallville_admin_token` (chmod 600) or the `SMALLVILLE_ADMIN_TOKEN`
  env var — public visitors get 403. No UI calls it; operators manage pacing with
  `echo N > .../pacing.txt` or curl with the token header.
- **UI wave in flight**: `ui-wave-1` (public landing page) + `ui-wave-2` (resident
  modal polish, chat panel mobile collapse, states, mobile audit) — sol xhigh
  agents; merge after review. World-speed UI deliberately NOT included (private
  endpoint, operator-managed).

### Done — runtime & architecture
- **Move off VPS complete**: town runs on the local VM only; VPS scrubbed (units, repo,
  key, tunnel removed). Cloudflare tunnel points at `192.168.2.5:8000` (Ben-installed).
- **25 residents live**: fork base = `base_the_ville_n25` (all 25, morning start
  `curr_time = February 13, 2023, 08:00:00`). Town boots with residents up and active.
- **Parallel planning**: all residents' daily plans + hourly schedules generate
  concurrently (6 workers). Fixed the batch-explosion bug (leader state must persist for
  the day — popping it made every queued follower re-run the whole town's planning;
  was burning 2.26M tokens per boot).
- **Parallel step loop**: perceive stays sequential (chats can't race); retrieve/plan/
  reflect/execute run 6-way concurrent.
- **Live-flippable speed**: `game_sec_per_real_sec` in `utils.py` is the default;
  `environment/frontend_server/temp_storage/pacing.txt` overrides it LIVE (no service
  restart — the step loop re-reads it every step). `echo 60 > .../pacing.txt` flips
  instantly. Current: 60× (one game-minute per real second → game day ≈ 24 real min).
  NOTE: the clock is an absolute wall-time mapping (anchor = boot time), so lowering
  the ratio makes the clock tick slower but keeps it monotonic-in-real-time; the world
  time pill reflects the CURRENT ratio.
- **Resilience**: None-safe poignancy scores (LLM None → mid score 5); per-persona
  try/except in both perceive and decide phases — one persona's failure can no longer
  kill the town (falls back to "stay in place").
- **Autonomous advance**: backend writes its own env files when no browser drives the
  walk — town never freezes without a viewer. Partial env POSTs from the browser are
  gap-filled (was crashing the sim on missing residents).
- **Atomic saves**: all hot JSON writes (movement, env, scratch, embeddings, curr_step)
  go through tmp+rename — a kill mid-save can no longer corrupt persona state (this
  crash-looped the unit once; `repair_embeddings.py` regenerates a corrupted
  `embeddings.json` from `nodes.json`).
- **Robustness**: hard 90s LLM deadline; fresh session per LLM attempt (kills the
  connection-pool deadlock from trickling gateway streams); task-decomp drift
  normalization; runtime state untracked from git.

### Done — features
- **Chat message UI (gpt-5.6-sol @ xhigh agent, `sw-chat-ui`)**: backend appends
  conversations to `storage/<sim>/chat_log.json` (deduped — a conversation persisting in
  scratch across steps no longer re-logs); `get_chat_log/` endpoint; frontend panel at
  bottom-left, right of the resident drawer — "TOWN CONVERSATIONS" scrolling feed,
  color-coded speakers, timestamps, auto-scroll, collapse button, mobile-aware.
- **UI improvements (same agent)**: `process_environment` POST removed (backend is
  autonomous — it was the partial-env crash source); sprite fallback for all 25
  residents; roster from meta order; awaiting-action empty states.
- **500M token guard**: cron `smallville-token-guard` (every 30 min) — reads
  `token_usage.db` `calls` table `SUM(total_tokens)`; ≥ 500,000,000 → stops
  `smallville-reverie` + alerts. Current burn ≈ 5.3M all-time — guard is a safety net.

### Verified live (screenshots on record)
- 25 residents in drawer, 17/25 awake at 08:00 boot (sleepers have later wake hours)
- World time pill advances at real-time pace (08:04 at 4 min post-boot)
- Coherent day: morning routines → Hobbs Cafe / Willow Market / library → real
  conversations (Ayesha & Klaus: community gardens + Shakespeare; Tom & Jane: mayor
  election + meatloaf)
- Chat feed live on public site; token monitor shows live + all-time usage

## Run commands

```bash
# sim backend (headless, autonomous)
systemctl --user restart smallville-reverie
journalctl --user -u smallville-reverie -n 50 --no-pager

# frontend
systemctl --user restart smallville-django

# token totals (watchdog query)
python3 - <<'EOF'
import sqlite3
c = sqlite3.connect('environment/frontend_server/temp_storage/token_usage.db')
print(c.execute('SELECT COALESCE(SUM(total_tokens),0) FROM calls').fetchone()[0])
EOF
```

## Known trade-offs / future ideas
- Chat panel occludes part of the map (collapsible; could dock into the drawer on
  narrow screens).
- First-boot plan phase ≈ 700 LLM calls (~5-8 min) per new day — the parallel batch
  makes it a one-time fixed cost per day.
- With real-time pacing a game day lasts a real day; set `game_sec_per_real_sec = 60`
  in `utils.py` for a lively fast-forward day (~24 real min).
