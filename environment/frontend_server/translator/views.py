"""
Author: Joon Sung Park (joonspk@stanford.edu)
File: views.py
"""
import os
import json
import secrets
import fcntl
import uuid
import sqlite3
from os import listdir
from django.conf import settings

import datetime
from django.shortcuts import render, HttpResponseRedirect
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from global_methods import *

from django.templatetags.static import static
from .models import *

def landing(request):
  """Render the public introduction to the live Smallville simulation."""
  return render(request, "landing/landing.html", {})


def get_token_usage(request):
  """Return the backend's live LLM token usage snapshot (JSON).

  The backend writes temp_storage/token_usage.json on every LLM call; the
  on-page monitor polls this endpoint to display live usage. The response
  also carries cumulative totals read from the SQLite log
  (temp_storage/token_usage.db) so all-time usage is queryable.
  """
  usage_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "temp_storage", "token_usage.json")
  payload = {
    "started_at": "-", "updated_at": "-", "total_tokens": 0,
    "prompt_tokens": 0, "completion_tokens": 0, "calls": 0,
    "by_model": {}, "embedding_calls": 0, "embedding_tokens": 0,
  }
  if os.path.exists(usage_file):
    try:
      with open(usage_file) as f:
        payload.update(json.load(f))
    except Exception:
      pass

  # Cumulative totals from the SQLite call log.
  payload["cumulative"] = {
    "total_tokens": 0, "prompt_tokens": 0, "completion_tokens": 0,
    "calls": 0, "embedding_calls": 0, "first_call_at": None,
    "last_call_at": None,
  }
  db_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "temp_storage", "token_usage.db")
  if os.path.exists(db_file):
    try:
      import sqlite3
      conn = sqlite3.connect(db_file, timeout=5)
      try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*), COALESCE(SUM(total_tokens),0),"
                    " COALESCE(SUM(prompt_tokens),0), COALESCE(SUM(completion_tokens),0),"
                    " SUM(CASE WHEN kind='embedding' THEN 1 ELSE 0 END),"
                    " MIN(ts), MAX(ts) FROM calls")
        row = cur.fetchone()
        payload["cumulative"] = {
          "calls": row[0] or 0,
          "total_tokens": row[1] or 0,
          "prompt_tokens": row[2] or 0,
          "completion_tokens": row[3] or 0,
          "embedding_calls": row[4] or 0,
          "first_call_at": row[5],
          "last_call_at": row[6],
        }
      finally:
        conn.close()
    except Exception:
      pass
  return JsonResponse(payload)


def get_chat_log(request):
  """Return the tail of a simulation's live conversation log."""
  sim_code = request.GET.get("sim_code") or request.POST.get("sim_code")
  payload = {"entries": []}
  if (not sim_code or sim_code in (".", "..")
      or os.path.basename(sim_code) != sim_code):
    return JsonResponse(payload)

  frontend_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
  chat_log_file = os.path.join(frontend_root, "storage", sim_code,
                               "chat_log.json")
  if os.path.exists(chat_log_file):
    try:
      with open(chat_log_file) as json_file:
        entries = json.load(json_file)
      if isinstance(entries, list):
        payload["entries"] = entries[-200:]
    except Exception:
      pass
  return JsonResponse(payload)


def get_town_summary(request):
  '''Return the latest generated summary for the public town.'''
  payload = {'summary': None, 'generated_at': None, 'covered': None}
  frontend_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
  summary_file = os.path.join(frontend_root, 'storage', 'public_sim',
                              'town_summary.json')
  if os.path.exists(summary_file):
    try:
      with open(summary_file) as json_file:
        summary = json.load(json_file)
      if isinstance(summary, dict):
        return JsonResponse(summary)
    except Exception:
      pass
  return JsonResponse(payload)


def _safe_sim_code(request, default="public_sim"):
  sim_code = (request.GET.get("sim_code") or request.POST.get("sim_code")
              or default)
  if (not sim_code or sim_code in (".", "..")
      or os.path.basename(sim_code) != sim_code):
    return None
  return sim_code


def _read_json(path, default):
  try:
    with open(path) as infile:
      return json.load(infile)
  except Exception:
    return default


def _atomic_json_dump(payload, path):
  tmp = path + ".tmp"
  with open(tmp, "w") as outfile:
    json.dump(payload, outfile)
  os.replace(tmp, path)


def get_economy_feed(request):
  """Return the capped public tail of the economy activity feed."""
  payload = {"entries": []}
  sim_code = _safe_sim_code(request)
  if not sim_code:
    return JsonResponse(payload)
  frontend_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
  feed_file = os.path.join(frontend_root, "storage", sim_code, "economy",
                           "economy_feed.json")
  entries = _read_json(feed_file, [])
  if isinstance(entries, list):
    payload["entries"] = entries[-200:]
  return JsonResponse(payload)


def get_economy(request):
  """Return a read-only public projection of the town economy."""
  payload = {
    "ok": False, "updated_at": None, "balances": {}, "statuses": {},
    "residents": {}, "shops": {}, "bank": {}, "treasury": {}, "feed": [],
  }
  sim_code = _safe_sim_code(request)
  if not sim_code:
    return JsonResponse(payload)
  frontend_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
  economy_root = os.path.join(frontend_root, "storage", sim_code, "economy")
  state = _read_json(os.path.join(economy_root, "economy_state.json"), {})
  feed = _read_json(os.path.join(economy_root, "economy_feed.json"), [])
  if not isinstance(state, dict):
    state = {}
  residents = state.get("residents")
  if isinstance(residents, dict):
    for name, resident in residents.items():
      if not isinstance(resident, dict):
        continue
      public = {
        "balance": resident.get("balance", 0),
        "status": resident.get("status", "stable"),
        "debt": resident.get("debt", 0),
        "total_earned": resident.get("total_earned", 0),
        "total_spent": resident.get("total_spent", 0),
        "hunger": resident.get("hunger", 75),
        "energy": resident.get("energy", 75),
        "social": resident.get("social", 75),
        "education_score": resident.get("education_score", 0),
        "jobs": resident.get("jobs", []),
      }
      payload["residents"][name] = public
      payload["balances"][name] = public["balance"]
      payload["statuses"][name] = public["status"]
  for key in ("shops", "bank", "treasury"):
    value = state.get(key)
    if isinstance(value, dict):
      payload[key] = value
  if isinstance(feed, list):
    payload["feed"] = feed[-200:]
  payload["updated_at"] = state.get("updated_at")
  payload["ok"] = bool(state)
  return JsonResponse(payload)


def get_sim_store_stats(request):
  """Return cheap, public health statistics for the simulation store."""
  frontend_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
  store_dir = os.path.join(frontend_root, "storage", "public_sim", "sim_store")
  db_file = os.path.join(store_dir, "sim_store.db")
  archive_dir = os.path.join(store_dir, "archive")
  payload = {
    "db_size_bytes": 0,
    "wal_size_bytes": 0,
    "steps": 0,
    "memories": 0,
    "archive_count": 0,
  }
  try:
    payload["archive_count"] = sum(
      filename.endswith((".ndjson.xz", ".ndjson.zst"))
      for filename in os.listdir(archive_dir)
    )
  except OSError:
    pass
  if not os.path.exists(db_file):
    return JsonResponse(payload)

  try:
    payload["db_size_bytes"] = os.path.getsize(db_file)
    wal_file = db_file + "-wal"
    if os.path.exists(wal_file):
      payload["wal_size_bytes"] = os.path.getsize(wal_file)
    conn = sqlite3.connect(
      "file:%s?mode=ro" % db_file, uri=True, timeout=0.05)
    try:
      conn.execute("PRAGMA query_only=ON")
      conn.execute("PRAGMA busy_timeout=50")
      payload["steps"] = conn.execute(
        "SELECT COUNT(*) FROM steps").fetchone()[0]
      payload["memories"] = conn.execute(
        "SELECT COUNT(*) FROM memories").fetchone()[0]
    finally:
      conn.close()
  except (OSError, sqlite3.Error):
    pass
  return JsonResponse(payload)


_sim_state_cache = {"at": 0.0, "running": None}


def get_sim_state(request):
  """Return whether the Reverie AI loop is currently running (public).

  The town website stays up while the simulation is paused; this endpoint
  lets the UI show a visible PAUSED banner. The check runs ``systemctl --user
  is-active smallville-reverie.service`` (cheap, ~30ms) with a 5-second TTL
  cache so the 2-5s frontend poll never hammers subprocess spawn.
  """
  import subprocess
  import time as _time
  now = _time.time()
  if _sim_state_cache["at"] + 5.0 >= now and _sim_state_cache["running"] is not None:
    running = _sim_state_cache["running"]
  else:
    try:
      completed = subprocess.run(
        ["systemctl", "--user", "is-active", "smallville-reverie.service"],
        capture_output=True, text=True, timeout=3.0, check=False,
      )
      running = completed.returncode == 0 and completed.stdout.strip() == "active"
    except (OSError, subprocess.SubprocessError):
      running = None  # unknown — do not show a banner on failure
    _sim_state_cache["at"] = now
    _sim_state_cache["running"] = running
  return JsonResponse({
    "sim_running": running,
    "unit": "smallville-reverie.service",
    "checked_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
  })


def _is_admin_request(request):
  """Admin-only gate for private endpoints.

  Requires the X-Admin-Token header to
  match SMALLVILLE_ADMIN_TOKEN (env var or /home/ben/.smallville_admin_token).
  The token is read by the coordinator/Ben only — never exposed to the
  public site, so no UI code may call these endpoints.
  """
  token = settings.SMALLVILLE_ADMIN_TOKEN
  if not token:
    return False
  given = request.headers.get("X-Admin-Token", "")
  return secrets.compare_digest(given, token)


ADMIN_COMMANDS = {
  "give_money", "bankrupt", "make_rich", "set_status", "inject_event",
  "broadcast", "restock", "adjust_price", "add_good", "transfer",
  "set_pacing",
}


@csrf_exempt
def admin_command(request):
  """Atomically enqueue a private command for the Reverie step loop."""
  if not _is_admin_request(request):
    return JsonResponse({"ok": False, "error": "forbidden"}, status=403)
  if request.method != "POST":
    return JsonResponse({"ok": False, "error": "POST only"}, status=405)
  try:
    data = json.loads(request.body or "{}")
  except (TypeError, ValueError):
    return JsonResponse({"ok": False, "error": "invalid JSON"}, status=400)
  if not isinstance(data, dict):
    return JsonResponse({"ok": False, "error": "JSON object required"}, status=400)
  cmd = data.get("cmd")
  args = data.get("args", {})
  if cmd not in ADMIN_COMMANDS:
    return JsonResponse({"ok": False, "error": "unknown command"}, status=400)
  if not isinstance(args, dict):
    return JsonResponse({"ok": False, "error": "args must be an object"}, status=400)

  frontend_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
  temp_storage = os.path.join(frontend_root, "temp_storage")
  os.makedirs(temp_storage, exist_ok=True)
  queue_file = os.path.join(temp_storage, "admin_commands.json")
  lock_file = os.path.join(temp_storage, "admin_commands.lock")
  command_id = uuid.uuid4().hex
  queued = {
    "id": command_id,
    "cmd": cmd,
    "args": args,
    "status": "pending",
    "queued_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
  }
  try:
    with open(lock_file, "a+") as lock:
      fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
      try:
        queue = _read_json(queue_file, [])
        if not isinstance(queue, list):
          queue = []
        queue.append(queued)
        pending = [item for item in queue
                   if isinstance(item, dict)
                   and item.get("status") not in ("done", "failed")]
        completed = [item for item in queue
                     if isinstance(item, dict)
                     and item.get("status") in ("done", "failed")]
        _atomic_json_dump(pending + completed[-500:], queue_file)
      finally:
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
  except Exception:
    return JsonResponse({"ok": False, "error": "queue unavailable"}, status=503)
  return JsonResponse({"ok": True, "id": command_id})


@csrf_exempt
def set_pacing(request):
  """Set the world-clock pacing (game seconds per real second). PRIVATE.

  Admin-only (see _is_admin_request) — the public site must never be able
  to flip world speed. Writes temp_storage/pacing.txt which the reverie
  step loop re-reads each step, so the change takes effect live without
  a restart.
  """
  if not _is_admin_request(request):
    return JsonResponse({"ok": False, "error": "forbidden"}, status=403)
  if request.method != "POST":
    return JsonResponse({"ok": False, "error": "POST only"}, status=405)
  try:
    data = json.loads(request.body or "{}")
    pacing = int(data.get("pacing", 0))
  except (ValueError, TypeError):
    return JsonResponse({"ok": False, "error": "pacing must be an int"}, status=400)
  if not 1 <= pacing <= 10000:
    return JsonResponse({"ok": False, "error": "pacing out of range 1..10000"}, status=400)
  frontend_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
  pacing_file = os.path.join(frontend_root, "temp_storage", "pacing.txt")
  pacing_tmp = pacing_file + ".tmp"
  with open(pacing_tmp, "w") as outfile:
    outfile.write(str(pacing))
  os.replace(pacing_tmp, pacing_file)
  return JsonResponse({"ok": True, "pacing": pacing})


def demo(request, sim_code, step, play_speed="2"): 
  move_file = f"compressed_storage/{sim_code}/master_movement.json"
  meta_file = f"compressed_storage/{sim_code}/meta.json"
  step = int(step)
  play_speed_opt = {"1": 1, "2": 2, "3": 4,
                    "4": 8, "5": 16, "6": 32}
  if play_speed not in play_speed_opt: play_speed = 2
  else: play_speed = play_speed_opt[play_speed]

  # Loading the basic meta information about the simulation.
  meta = dict() 
  with open (meta_file) as json_file: 
    meta = json.load(json_file)

  sec_per_step = meta["sec_per_step"]
  start_datetime = datetime.datetime.strptime(meta["start_date"] + " 00:00:00", 
                                              '%B %d, %Y %H:%M:%S')
  for i in range(step): 
    start_datetime += datetime.timedelta(seconds=sec_per_step)
  start_datetime = start_datetime.strftime("%Y-%m-%dT%H:%M:%S")

  # Loading the movement file
  raw_all_movement = dict()
  with open(move_file) as json_file: 
    raw_all_movement = json.load(json_file)
 
  # Loading all names of the personas
  persona_names = dict()
  persona_names = []
  persona_names_set = set()
  for p in list(raw_all_movement["0"].keys()): 
    persona_names += [{"original": p, 
                       "underscore": p.replace(" ", "_"), 
                       "initial": p[0] + p.split(" ")[-1][0]}]
    persona_names_set.add(p)

  # <all_movement> is the main movement variable that we are passing to the 
  # frontend. Whereas we use ajax scheme to communicate steps to the frontend
  # during the simulation stage, for this demo, we send all movement 
  # information in one step. 
  all_movement = dict()

  # Preparing the initial step. 
  # <init_prep> sets the locations and descriptions of all agents at the
  # beginning of the demo determined by <step>. 
  init_prep = dict() 
  for int_key in range(step+1): 
    key = str(int_key)
    val = raw_all_movement[key]
    for p in persona_names_set: 
      if p in val: 
        init_prep[p] = val[p]
  persona_init_pos = dict()
  for p in persona_names_set: 
    persona_init_pos[p.replace(" ","_")] = init_prep[p]["movement"]
  all_movement[step] = init_prep

  # Finish loading <all_movement>
  for int_key in range(step+1, len(raw_all_movement.keys())): 
    all_movement[int_key] = raw_all_movement[str(int_key)]

  context = {"sim_code": sim_code,
             "step": step,
             "persona_names": persona_names,
             "persona_init_pos": json.dumps(persona_init_pos), 
             "all_movement": json.dumps(all_movement), 
             "start_datetime": start_datetime,
             "sec_per_step": sec_per_step,
             "play_speed": play_speed,
             "mode": "demo"}
  template = "demo/demo.html"

  return render(request, template, context)


def UIST_Demo(request): 
  return demo(request, "March20_the_ville_n25_UIST_RUN-step-1-141", 2160, play_speed="3")


def home(request):
  f_curr_sim_code = "temp_storage/curr_sim_code.json"
  f_curr_step = "temp_storage/curr_step.json"

  if not check_if_file_exists(f_curr_step): 
    context = {}
    template = "home/error_start_backend.html"
    return render(request, template, context)

  with open(f_curr_sim_code) as json_file:  
    sim_code = json.load(json_file)["sim_code"]
  
  with open(f_curr_step) as json_file:  
    step = json.load(json_file)["step"]

  # NOTE: the step file is intentionally NOT deleted here anymore -- the
  # backend refreshes it every step, so the page can be reloaded any time
  # instead of showing "please start the back end first".

  persona_dir = f"storage/{sim_code}/personas"
  available_names = []
  if os.path.isdir(persona_dir):
    for i in find_filenames(persona_dir, ""):
      x = i.split("/")[-1].strip()
      if x and x[0] != ".":
        available_names.append(x)

  ordered_names = []
  meta_file = f"storage/{sim_code}/reverie/meta.json"
  if check_if_file_exists(meta_file):
    try:
      with open(meta_file) as json_file:
        meta_names = json.load(json_file).get("persona_names", [])
      ordered_names.extend(name for name in meta_names
                           if isinstance(name, str) and name)
    except Exception:
      pass
  ordered_names.extend(sorted(name for name in available_names
                              if name not in ordered_names))
  persona_names = [[name, name.replace(" ", "_")]
                   for name in ordered_names]
  persona_init_pos = []
  file_count = []
  environment_dir = f"storage/{sim_code}/environment"
  if os.path.isdir(environment_dir):
    for i in find_filenames(environment_dir, ".json"):
      x = i.split("/")[-1].strip()
      if x and x[0] != ".":
        try:
          file_count.append(int(x.split(".")[0]))
        except ValueError:
          pass
  if file_count:
    curr_json = f'storage/{sim_code}/environment/{str(max(file_count))}.json'
    with open(curr_json) as json_file:
      persona_init_pos_dict = json.load(json_file)
    for name in ordered_names:
      val = persona_init_pos_dict.get(name)
      if isinstance(val, dict) and "x" in val and "y" in val:
        persona_init_pos.append([name, val["x"], val["y"]])

  context = {"sim_code": sim_code,
             "step": step, 
             "persona_names": persona_names,
             "persona_init_pos": persona_init_pos,
             "mode": "simulate"}
  template = "home/home.html"
  return render(request, template, context)


def replay(request, sim_code, step): 
  sim_code = sim_code
  step = int(step)

  persona_names = []
  persona_names_set = set()
  for i in find_filenames(f"storage/{sim_code}/personas", ""): 
    x = i.split("/")[-1].strip()
    if x[0] != ".": 
      persona_names += [[x, x.replace(" ", "_")]]
      persona_names_set.add(x)

  persona_init_pos = []
  file_count = []
  for i in find_filenames(f"storage/{sim_code}/environment", ".json"):
    x = i.split("/")[-1].strip()
    if x[0] != ".": 
      file_count += [int(x.split(".")[0])]
  curr_json = f'storage/{sim_code}/environment/{str(max(file_count))}.json'
  with open(curr_json) as json_file:  
    persona_init_pos_dict = json.load(json_file)
    for key, val in persona_init_pos_dict.items(): 
      if key in persona_names_set: 
        persona_init_pos += [[key, val["x"], val["y"]]]

  context = {"sim_code": sim_code,
             "step": step,
             "persona_names": persona_names,
             "persona_init_pos": persona_init_pos, 
             "mode": "replay"}
  template = "home/home.html"
  return render(request, template, context)


def persona_state_json(request):
  """Lightweight persona state for the resident modal: traits, objective,
  daily plan, lifestyle, age + live action/location. JSON only."""
  sim_code = _safe_sim_code(request)
  if not sim_code:
    return JsonResponse({"error": "bad simulation"}, status=400)
  persona_name = request.GET.get("persona_name", "")
  if not persona_name:
    return JsonResponse({"error": "persona_name required"}, status=400)
  # Underscore form (URL-safe) or spaced form both accepted.
  _name = persona_name.replace("_", " ")
  _safe = os.path.normpath(_name)
  if _safe != _name or "/" in _name or ".." in _name:
    return JsonResponse({"error": "bad name"}, status=400)
  frontend_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
  memory = os.path.join(frontend_root, "storage", sim_code, "personas", _name,
                        "bootstrap_memory")
  if not os.path.exists(memory):
    return JsonResponse({"error": "no state"}, status=404)
  try:
    with open(memory + "/scratch.json") as json_file:
      scratch = json.load(json_file)
  except Exception:
    return JsonResponse({"error": "no state"}, status=404)
  return JsonResponse({
    "name": scratch.get("name", _name),
    "age": scratch.get("age"),
    "living_area": scratch.get("living_area"),
    "curr_sector": scratch.get("curr_sector", ""),
    "curr_arena": scratch.get("curr_arena", ""),
    "innate": scratch.get("innate", ""),
    "learned": scratch.get("learned", ""),
    "currently": scratch.get("currently", ""),
    "daily_plan_req": scratch.get("daily_plan_req", ""),
    "lifestyle": scratch.get("lifestyle", ""),
    "curr_action": (scratch.get("curr_action")
                    or scratch.get("act_description", "")),
    "economy": {
      "balance": scratch.get("economy_balance", 0),
      "total_earned": scratch.get("economy_total_earned", 0),
      "total_spent": scratch.get("economy_total_spent", 0),
      "debt": scratch.get("economy_debt", 0),
      "status": scratch.get("economy_status", "stable"),
      "hunger": scratch.get("health_hunger", 75),
      "energy": scratch.get("health_energy", 75),
      "social": scratch.get("health_social", 75),
      "education_score": scratch.get("education_score", 0),
    },
  })


def replay_persona_state(request, sim_code, step, persona_name): 
  step = int(step)

  persona_name_underscore = persona_name
  persona_name = " ".join(persona_name.split("_"))
  memory = f"storage/{sim_code}/personas/{persona_name}/bootstrap_memory"
  if not os.path.exists(memory): 
    memory = f"compressed_storage/{sim_code}/personas/{persona_name}/bootstrap_memory"

  with open(memory + "/scratch.json") as json_file:  
    scratch = json.load(json_file)

  with open(memory + "/spatial_memory.json") as json_file:  
    spatial = json.load(json_file)

  with open(memory + "/associative_memory/nodes.json") as json_file:  
    associative = json.load(json_file)

  a_mem_event = []
  a_mem_chat = []
  a_mem_thought = []

  for count in range(len(associative.keys()), 0, -1): 
    node_id = f"node_{str(count)}"
    node_details = associative[node_id]

    if node_details["type"] == "event":
      a_mem_event += [node_details]

    elif node_details["type"] == "chat":
      a_mem_chat += [node_details]

    elif node_details["type"] == "thought":
      a_mem_thought += [node_details]
  
  context = {"sim_code": sim_code,
             "step": step,
             "persona_name": persona_name, 
             "persona_name_underscore": persona_name_underscore, 
             "scratch": scratch,
             "spatial": spatial,
             "a_mem_event": a_mem_event,
             "a_mem_chat": a_mem_chat,
             "a_mem_thought": a_mem_thought}
  template = "persona_state/persona_state.html"
  return render(request, template, context)


def path_tester(request):
  context = {}
  template = "path_tester/path_tester.html"
  return render(request, template, context)


def update_environment(request): 
  """
  <BACKEND to FRONTEND> 
  This sends the backend computation of the persona behavior to the frontend
  visual server. 
  It does this by reading the new movement information from 
  "storage/movement.json" file.

  ARGS:
    request: Django request
  RETURNS: 
    HttpResponse
  """
  # f_curr_sim_code = "temp_storage/curr_sim_code.json"
  # with open(f_curr_sim_code) as json_file:  
  #   sim_code = json.load(json_file)["sim_code"]

  data = json.loads(request.body)
  step = data["step"]
  sim_code = data["sim_code"]

  response_data = {"<step>": -1}
  if (check_if_file_exists(f"storage/{sim_code}/movement/{step}.json")):
    with open(f"storage/{sim_code}/movement/{step}.json") as json_file: 
      response_data = json.load(json_file)
      response_data["<step>"] = step

  return JsonResponse(response_data)


def path_tester_update(request): 
  """
  Processing the path and saving it to path_tester_env.json temp storage for 
  conducting the path tester. 

  ARGS:
    request: Django request
  RETURNS: 
    HttpResponse: string confirmation message. 
  """
  data = json.loads(request.body)
  camera = data["camera"]

  with open(f"temp_storage/path_tester_env.json", "w") as outfile:
    outfile.write(json.dumps(camera, indent=2))

  return HttpResponse("received")
