# Smallville — Town Operations Plan

> Live: `smallville.justarobot.uk` (Cloudflare tunnel → local VM `192.168.2.5:8000`)
> Repo: `/home/ben/smallville` → `hermes-gadget/smallville` (branch `main`)
> Sim backend: `smallville-reverie.service` · Frontend: `smallville-django.service` (user units, boot-enabled)

## Goal

A coherent, self-running AI town at **maximum 25 residents** that lives at a watchable
real-time pace, with a message feed showing what agents talk about, a polished UI, and a
hard guard on LLM spend.

## Current State (already done — keep working from here)

- **Move off VPS complete**: town now runs on the local VM only; VPS scrubbed
  (units, repo, key, tunnel removed). Cloudflare tunnel re-pointed by Ben.
- **9-persona town verified live** (base_the_ville_n9): plans generate, steps execute,
  movement files flow, world time syncs, State pages live, token monitor works.
- **Parallel planning**: all residents' daily plans + hourly schedules generate
  concurrently (6 workers) — 9 residents in ~4 min instead of ~20.
- **Parallel step loop**: perceive stays sequential (chats can't race); retrieve/plan/
  reflect/execute run 6-way concurrent.
- **Real-time pacing**: `game_sec_per_real_sec = 1.0` in `utils.py` — game clock advances
  with real elapsed time (knob: 60 = lively-fast, 1/60 = near-frozen).
- **Autonomous advance**: backend writes its own env files when no browser drives the
  walk — the town no longer freezes with no viewer open.
- **Fresh morning start**: base meta `curr_time = February 13, 2023, 08:00:00` — residents
  are up and active from boot instead of sleeping through a midnight start.
- **Robustness fixes**: hard 90s LLM deadline; fresh session per LLM attempt (kills
  connection-pool deadlock); task-decomp drift normalization; partial-env gap fill.
- **Token telemetry**: `temp_storage/token_usage.db` cumulative + live JSON snapshot,
  displayed in the UI's token monitor.

## Pending Tasks (in order)

### 1. Fix + verify the live town (DO FIRST)
- The sim crash-looped on a **partial env POST from the open browser** (3 of 9 personas)
  — `KeyError` at `reverie.py:356`. The gap-fill patch is written but NOT yet deployed.
- Deploy, restart `smallville-reverie`, confirm steady stepping (movement files advancing
  with no browser open), then screenshot the live site (map, residents, state page,
  token monitor).

### 2. Expand to 25 residents
- Switch the fork base to `base_the_ville_n25` (all 25 upstream residents; n9 was built
  from it). Update its `reverie/meta.json` (`curr_time` 08:00 start), reset `public_sim`,
  restart the unit.
- Verify: 25 residents in sidebar, 25 sprites on map, plan phase completes (~25 × 31
  calls, ~5-8 min), steps advance, chats happen, day rolls coherently (morning routines →
  work at Hobbs Cafe / Willow Market / library → evening wind-down).

### 3. Message UI (chat feed) — gpt-5.6-sol agent task
- Panel at **bottom-left, immediately right of the residents tab** (drawer).
- Shows what agents say to each other: backend appends chats (`persona.scratch.chat`
  after perceive/converse in `reverie.py` step loop) to `storage/<sim>/chat_log.json`
  (timestamped); frontend polls and renders a scrolling feed (speaker names, timestamps,
  avatars/colors, auto-scroll).
- Agent: **gpt-5.6-sol @ xhigh** via ForgeDeck, local repo only, edit+commit+push,
  no gradle/tests; I verify serially (Django template checks + browser).

### 4. UI improvements pass (same agent, after chat feed)
- Stop the UI's `process_environment` POST (backend is autonomous now; partial env posts
  are the crash source).
- Resident sprite coverage for all 25; roster order matches meta; empty/loading states;
  message panel responsive on mobile (320px); keyboard/UX polish.
- Keep payload contracts (update_environment, sim_code, step).

### 5. LLM spend guard (500M token stop)
- Cron watchdog (~30 min): read `token_usage.db` cumulative total; if ≥ 500,000,000 →
  stop `smallville-reverie.service` + notify. Current burn is ~1-2M/day, so this is a
  hard safety net, not an expected tripwire.
- 500M ≈ 500M input+output tokens combined (all-time, both LLM + embeddings if counted).

### 6. Verification checklist (each change)
- [ ] `curl -s -o /dev/null -w "%{http_code}" http://192.168.2.5:8000/` → 200
- [ ] movement files advance without a browser open (autonomous)
- [ ] world-time pill advances at ~real-time pace
- [ ] residents act per their schedules (morning → day → evening coherently)
- [ ] screenshots captured for each milestone (send to Ben)
- [ ] `git push origin main` after each merged change

## Run commands

```bash
# sim backend (headless, autonomous)
systemctl --user restart smallville-reverie
journalctl --user -u smallville-reverie -n 50 --no-pager

# frontend
systemctl --user restart smallville-django

# token totals
python3 - <<'EOF'
import sqlite3
c = sqlite3.connect('environment/frontend_server/temp_storage/token_usage.db')
for row in c.execute("SELECT name FROM sqlite_master WHERE type='table'"): print(row)
EOF
```
