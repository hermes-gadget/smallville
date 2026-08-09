#!/usr/bin/env python3
'''Generate the public town's rolling three-day narrative summary.

Usage:
  cd reverie/backend_server && /home/ben/smallville/.venv/bin/python summarize_town.py [output.json]
'''
import datetime
import json
import os
import sys
import tempfile
from pathlib import Path


MOVEMENT_FILE_LIMIT = 300
MOVEMENT_EVENT_LIMIT = 300
ECONOMY_FEED_LIMIT = 100
CONVERSATION_CONTEXT_CHARS = 160000
MAX_UTTERANCE_CHARS = 2000
MAX_JSON_BYTES = 4 * 1024 * 1024

REPO_ROOT = Path(__file__).resolve().parents[2]
SIM_ROOT = REPO_ROOT / 'environment' / 'frontend_server' / 'storage' / 'public_sim'
DEFAULT_OUTPUT = SIM_ROOT / 'town_summary.json'


def _load_json(path, default):
  if not path.exists():
    return default
  if path.stat().st_size > MAX_JSON_BYTES:
    raise RuntimeError('input is larger than the safety limit: %s' % path)
  with path.open(encoding='utf-8') as source:
    return json.load(source)


def _parse_real_time(value, timezone):
  if not isinstance(value, str):
    return None
  try:
    parsed = datetime.datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
  except ValueError:
    return None
  if parsed.tzinfo is None:
    parsed = parsed.replace(tzinfo=timezone)
  return parsed.astimezone(timezone)


def _fit_latest(records, char_limit):
  kept = []
  used = 2
  for record in reversed(records):
    size = len(json.dumps(record, ensure_ascii=False)) + 2
    if kept and used + size > char_limit:
      break
    kept.append(record)
    used += size
  kept.reverse()
  return kept


def _conversation_context(now, cutoff):
  raw = _load_json(SIM_ROOT / 'chat_log.json', [])
  if not isinstance(raw, list):
    raise RuntimeError('chat_log.json must contain a list')

  records = []
  for entry in raw:
    if not isinstance(entry, dict):
      continue
    timestamp = _parse_real_time(entry.get('ts'), now.tzinfo)
    if not timestamp or timestamp < cutoff or timestamp > now:
      continue
    chat = []
    for exchange in entry.get('chat') or []:
      if not isinstance(exchange, (list, tuple)) or len(exchange) < 2:
        continue
      speaker = str(exchange[0]).strip()
      utterance = str(exchange[1]).strip()[:MAX_UTTERANCE_CHARS]
      if speaker and utterance:
        chat.append([speaker, utterance])
    if chat:
      records.append({
        'ts': entry.get('ts'),
        'step': entry.get('step'),
        'chat': chat,
      })

  return _fit_latest(records, CONVERSATION_CONTEXT_CHARS), len(records)


def _movement_context():
  movement_dir = SIM_ROOT / 'movement'
  if not movement_dir.is_dir():
    return [], None, 0

  candidates = []
  for path in movement_dir.glob('*.json'):
    try:
      candidates.append((path.stat().st_mtime_ns, path))
    except OSError:
      continue
  candidates.sort(key=lambda item: (item[0], item[1].name))
  selected = [item[1] for item in candidates[-MOVEMENT_FILE_LIMIT:]]

  events = []
  previous_descriptions = {}
  newest_meta = None
  files_read = 0
  for path in selected:
    try:
      movement = _load_json(path, {})
    except (OSError, ValueError):
      continue
    if not isinstance(movement, dict):
      continue
    files_read += 1
    meta = movement.get('meta') or {}
    if not isinstance(meta, dict):
      meta = {}
    else:
      newest_meta = meta
    personas = movement.get('persona') or {}
    if not isinstance(personas, dict):
      continue
    for name in sorted(personas):
      state = personas.get(name) or {}
      if not isinstance(state, dict):
        continue
      description = str(state.get('description') or '').strip()
      if not description or previous_descriptions.get(name) == description:
        continue
      previous_descriptions[name] = description
      events.append({
        'step': path.stem,
        'in_world_time': meta.get('curr_time'),
        'resident': name,
        'event': description[:1000],
      })

  return events[-MOVEMENT_EVENT_LIMIT:], newest_meta, files_read


def _economy_context():
  economy_dir = SIM_ROOT / 'economy'
  state = _load_json(economy_dir / 'economy_state.json', {})
  feed = _load_json(economy_dir / 'economy_feed.json', [])
  if not isinstance(state, dict):
    state = {}
  if not isinstance(feed, list):
    feed = []

  residents = {}
  resident_fields = (
    'balance', 'total_earned', 'total_spent', 'debt', 'status',
    'housing_status', 'health_status', 'hungry', 'student', 'jobs',
  )
  resident_data = state.get('residents') or {}
  if not isinstance(resident_data, dict):
    resident_data = {}
  for name, details in sorted(resident_data.items()):
    if isinstance(details, dict):
      residents[name] = {
        key: details.get(key) for key in resident_fields if key in details
      }

  shops = {}
  shop_data = state.get('shops') or {}
  if not isinstance(shop_data, dict):
    shop_data = {}
  for name, details in sorted(shop_data.items()):
    if not isinstance(details, dict):
      continue
    shops[name] = {
      'owner': details.get('owner'),
      'staff': details.get('staff'),
      'cash': details.get('cash'),
      'stock': {
        item: goods.get('stock')
        for item, goods in sorted((details.get('goods') or {}).items())
        if isinstance(goods, dict)
      },
    }

  compact_feed = []
  for entry in feed[-ECONOMY_FEED_LIMIT:]:
    if isinstance(entry, dict):
      compact_feed.append({
        'ts': entry.get('ts'),
        'step': entry.get('step'),
        'text': str(entry.get('text') or '')[:500],
      })

  return {
    'updated_at': state.get('updated_at'),
    'last_tick': state.get('last_tick'),
    'residents': residents,
    'shops': shops,
    'bank': state.get('bank'),
    'treasury': state.get('treasury'),
    'recent_feed': compact_feed,
  }


def _build_prompt(now, cutoff, conversations, conversation_count,
                  movement_events, movement_meta, movement_files_read,
                  economy):
  context = {
    'real_time_coverage': {
      'from': cutoff.isoformat(timespec='seconds'),
      'to': now.isoformat(timespec='seconds'),
    },
    'in_world_time': (movement_meta or {}).get('curr_time'),
    'conversation_records_in_window': conversation_count,
    'conversation_records_included': len(conversations),
    'conversations': conversations,
    'movement_files_read': movement_files_read,
    'recent_movement_events': movement_events,
    'economy': economy,
  }
  return '''You are the chronicler for a simulated small town with 25 residents.
Using only the supplied records, write a SHORT town briefing for the real-time
period shown. Keep it brief: a reader should grasp it in 10 seconds.

Return only valid JSON with exactly this shape:
{"summary": "One sentence, max 30 words, capturing the town's overall state.",
 "highlights": ["3-5 short bullet points, each max 15 words, listing the most notable conversations, relationships, or events."]}

Rules: no prose paragraphs, no headings, no extra keys. Bullets must be short
and punchy. Do not invent events; if records are sparse say so in the summary.
Town records:
''' + json.dumps(context, ensure_ascii=False, separators=(',', ':'))


def _request_summary(prompt):
  with tempfile.TemporaryDirectory(prefix='smallville-summary-') as telemetry_dir:
    import utils
    utils.token_usage_file = os.path.join(telemetry_dir, 'token_usage.json')
    utils.token_usage_db = os.path.join(telemetry_dir, 'token_usage.db')

    from persona.prompt_template import gpt_structure
    return gpt_structure.ChatGPT_request(
      prompt,
      thinking=True,
      reasoning_effort='high',
      max_tokens=64000,
    )


def _parse_response(response):
  if not isinstance(response, str) or not response.strip():
    raise RuntimeError('LLM returned an empty response')
  if response.strip() == 'ChatGPT ERROR':
    raise RuntimeError('LLM request failed')

  text = response.strip()
  if text.startswith('```'):
    lines = text.splitlines()
    if lines and lines[0].startswith('```'):
      lines = lines[1:]
    if lines and lines[-1].strip() == '```':
      lines = lines[:-1]
    text = '\n'.join(lines).strip()

  try:
    parsed = json.loads(text)
  except ValueError:
    start = text.find('{')
    end = text.rfind('}')
    if start == -1 or end <= start:
      raise RuntimeError('LLM response was not valid JSON')
    try:
      parsed = json.loads(text[start:end + 1])
    except ValueError as error:
      raise RuntimeError('LLM response was not valid JSON') from error

  if not isinstance(parsed, dict):
    raise RuntimeError('LLM response must be a JSON object')
  summary = parsed.get('summary')
  highlights = parsed.get('highlights')
  if not isinstance(summary, str) or not summary.strip():
    raise RuntimeError('LLM response did not contain a summary')
  if not isinstance(highlights, list):
    raise RuntimeError('LLM response did not contain highlights')

  clean_highlights = []
  for highlight in highlights[:5]:
    if isinstance(highlight, str) and highlight.strip():
      clean_highlights.append(highlight.strip())
  return summary.strip(), clean_highlights


def _atomic_write(path, payload):
  path = Path(path).resolve()
  if not path.parent.is_dir():
    raise RuntimeError('output directory does not exist: %s' % path.parent)
  fd, temp_path = tempfile.mkstemp(
    prefix='.%s.' % path.name,
    suffix='.tmp',
    dir=str(path.parent),
  )
  try:
    with os.fdopen(fd, 'w', encoding='utf-8') as output:
      json.dump(payload, output, ensure_ascii=False, indent=2)
      output.write('\n')
      output.flush()
      os.fsync(output.fileno())
    os.chmod(temp_path, 0o644)
    os.replace(temp_path, str(path))
  finally:
    if os.path.exists(temp_path):
      os.unlink(temp_path)


def main():
  if len(sys.argv) > 2:
    print('usage: summarize_town.py [output.json]', file=sys.stderr)
    return 2
  output_path = Path(sys.argv[1]) if len(sys.argv) == 2 else DEFAULT_OUTPUT

  try:
    now = datetime.datetime.now().astimezone()
    cutoff = now - datetime.timedelta(hours=72)
    conversations, conversation_count = _conversation_context(now, cutoff)
    movement_events, movement_meta, movement_files_read = _movement_context()
    economy = _economy_context()
    prompt = _build_prompt(
      now, cutoff, conversations, conversation_count,
      movement_events, movement_meta, movement_files_read, economy,
    )
    response = _request_summary(prompt)
    summary, highlights = _parse_response(response)
    payload = {
      'generated_at': datetime.datetime.now().astimezone().isoformat(
        timespec='seconds'),
      'covered_from': cutoff.isoformat(timespec='seconds'),
      'covered_to': now.isoformat(timespec='seconds'),
      'summary': summary,
      'highlights': highlights,
    }
    _atomic_write(output_path, payload)
  except Exception as error:
    print('town summary failed: %s' % error, file=sys.stderr)
    return 1

  print('wrote town summary to %s' % Path(output_path).resolve())
  return 0


if __name__ == '__main__':
  sys.exit(main())
