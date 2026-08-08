# Smallville — Town Operations Plan

> Live: `smallville.justarobot.uk` (Cloudflare tunnel → local VM `192.168.2.5:8000`) ✅ VERIFIED 200
> Repo: `/home/ben/smallville` → `hermes-gadget/smallville` (branch `main`)
> Sim backend: `smallville-reverie.service` · Frontend: `smallville-django.service` (user units, boot-enabled)

## Goal

A coherent, self-running AI town at **maximum 25 residents** that lives at a watchable
real-time pace, with a message feed showing what agents talk about, a polished UI, and a
hard guard on LLM spend.

## Status: ALL TASKS DONE ✅ (2026-08-08)

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
- **Real-time pacing**: `game_sec_per_real_sec = 1.0` in `utils.py`. Clock = absolute
  wall-time mapping anchored on boot `curr_time` (NOT midnight — the 00:0x bug is fixed).
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
