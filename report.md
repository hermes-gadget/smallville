# Smallville chat-fix and town-summary report

## Root cause

Conversation actions used `Scratch.act_check_finished()` to decide when to
resume the scheduled day. That method compared only the formatted time of day
and required exact equality. Chat end-times are rounded to a minute while the
simulation clock can step past that exact second, so a completed chat could
remain active indefinitely. The stale `chatting_with` value then caused
`lets_talk()` to reject every new conversation involving either resident, and
the cleanup in `plan()` never ran because the stale `act_event` still said
`chat with`.

The live, read-only evidence matched this path: at world time February 15,
2023 around 16:25 (step 940+), seven residents still had February 13 chat
end-times, `chat with` action events, and an 800-turn partner buffer. The fix
uses a full datetime `>=` completion check and treats a chat with no end-time as
finished. On the next plan pass, `_determine_action()` calls `add_new_action()`
for the current scheduled activity, which clears the chat fields and replaces
the `chat with` event. The existing per-exchange `_live_log_chat()` calls are
unchanged.

## Per-file changes

- `reverie/backend_server/persona/memory_structures/scratch.py`: make action
  completion tolerate skipped timestamps and self-heal missing chat end-times.
- `reverie/backend_server/persona/prompt_template/gpt_structure.py`: add
  backward-compatible, per-call thinking, reasoning-effort, and token-budget
  overrides; existing callers still use the unchanged simulation defaults.
- `reverie/backend_server/summarize_town.py`: add the standalone, bounded
  three-day summary generator. It reads chat, the newest 300 movement files,
  and bounded economy state/feed context; requests strict JSON with thinking
  enabled, high reasoning effort, and 64k max tokens; validates the response;
  and atomically replaces only the selected output file. LLM telemetry is
  redirected to a temporary directory for this standalone call.
- `environment/frontend_server/translator/views.py`: add the public-town
  summary JSON reader and the required HTTP 200 null fallback.
- `environment/frontend_server/frontend_server/urls.py`: expose
  `/get_town_summary/`.
- `environment/frontend_server/templates/base.html`: add the fixed responsive
  `Town summary` panel before the token monitor, render narrative and up to five
  highlights safely, show generated time, and poll every 60 seconds.

## Verification

- Traced `plan()` → `act_check_finished()` → `_determine_action()` →
  `add_new_action()` and the `lets_talk()` gating path with TokenSave.
- Read live `public_sim` state without modifying it: seven stale chatting
  residents were confirmed, along with current reverie/movement time and the
  frozen chat-log tail at step 162.
- Ran `git diff --check` during each change set.
- Ran system-Python `python3 -m py_compile` on every changed Python file. No
  Django import, server, simulation, service restart, or live-storage write was
  performed.

## Residual risks

- Per the task's syntax-only verification rule, the Django route/panel and the
  maximum-thinking gateway call were not executed end to end. The first
  scheduled generator run should use an explicit `/tmp/...json` output for an
  operator smoke check before scheduling the default live output.
- Existing frozen chat state is deliberately not rewritten out of band. It
  will clear naturally on each resident's first plan pass after deployment.
- Summary quality depends on the gateway returning the requested JSON shape;
  malformed or failed responses exit 1 and intentionally preserve the prior
  summary file.
