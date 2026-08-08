"""Deterministic money, work, commerce, health, and education simulation.

The economy is deliberately a small local state machine.  It performs no
routine LLM work, treats every input as untrusted runtime data, and never lets
an economy failure stop Reverie's main step loop.
"""
import copy
import datetime
import fcntl
import hashlib
import json
import math
import os
import re
from contextlib import contextmanager

from global_methods import atomic_json_dump


SCHEMA_VERSION = 1
FEED_LIMIT = 300
COMMAND_HISTORY_LIMIT = 500
MAX_ADMIN_EMBEDDINGS_PER_STEP = 2

STUDENTS = {
  "Ayesha Khan", "Wolfgang Schulz", "Klaus Mueller", "Mei Lin",
  "Eddy Lin", "Rajiv Patel", "Francisco Lopez", "Giorgio Rossi",
}

# A job can constrain both sector and arena. This lets Tom earn shop wages at
# the Willows while receiving the separate mayoral wage only in the library.
JOBS = {
  "Isabella Rodriguez": [
    {"role": "Hobbs Cafe owner", "sector": "Hobbs Cafe", "rate": 16.0,
     "payer": "shop", "shop": "Hobbs Cafe"},
  ],
  "Arthur Burton": [
    {"role": "pub owner", "sector": "The Rose and Crown Pub", "rate": 15.0,
     "payer": "shop", "shop": "The Rose and Crown Pub"},
  ],
  "Tom Moreno": [
    {"role": "market owner", "sector": "The Willows Market and Pharmacy",
     "rate": 16.0, "payer": "shop",
     "shop": "The Willows Market and Pharmacy"},
    {"role": "mayor", "sector": "Oak Hill College", "arena": "library",
     "rate": 25.0, "payer": "treasury"},
  ],
  "John Lin": [
    {"role": "pharmacist", "sector": "The Willows Market and Pharmacy",
     "rate": 15.0, "payer": "shop",
     "shop": "The Willows Market and Pharmacy"},
  ],
  "Carmen Ortiz": [
    {"role": "supply-store owner", "sector": "Harvey Oak Supply Store",
     "rate": 15.0, "payer": "shop", "shop": "Harvey Oak Supply Store"},
  ],
  "Jennifer Moore": [
    {"role": "studio artist", "sector": "artist's co-living space",
     "rate": 14.0, "payer": "external"},
  ],
  "Latoya Williams": [
    {"role": "studio artist", "sector": "artist's co-living space",
     "rate": 14.0, "payer": "external"},
  ],
  "Yuriko Yamamoto": [
    {"role": "town banker", "sector": "Yuriko Yamamoto's house",
     "rate": 16.0, "payer": "bank"},
  ],
  "Maria Lopez": [
    {"role": "cafe helper (tips)", "sector": "Hobbs Cafe", "rate": 4.8,
     "payer": "shop", "shop": "Hobbs Cafe", "income_label": "tips"},
  ],
  "Ryan Park": [
    {"role": "API developer", "sector": "Hobbs Cafe", "rate": 16.0,
     "payer": "external"},
  ],
  "Abigail Chen": [
    {"role": "animator", "sector": "artist's co-living space", "rate": 15.0,
     "payer": "external"},
  ],
  "Sam Moore": [
    {"role": "campaign organizer", "sector": "Johnson Park", "rate": 14.0,
     "payer": "external"},
  ],
  "Hailey Johnson": [
    {"role": "writer", "sector": "artist's co-living space", "rate": 14.0,
     "payer": "external"},
  ],
  "Carlos Gomez": [
    {"role": "poet", "sector": "Carlos Gomez's apartment", "rate": 13.0,
     "payer": "external"},
  ],
  "Adam Smith": [
    {"role": "supply clerk", "sector": "Harvey Oak Supply Store",
     "rate": 12.5, "payer": "shop", "shop": "Harvey Oak Supply Store"},
  ],
  "Tamara Taylor": [
    {"role": "supply clerk", "sector": "Harvey Oak Supply Store",
     "rate": 12.0, "payer": "shop", "shop": "Harvey Oak Supply Store"},
  ],
}


SHOP_DEFINITIONS = {
  "Hobbs Cafe": {
    "owner": "Isabella Rodriguez", "staff": ["Maria Lopez"], "cash": 700.0,
    "goods": {
      "coffee": {"price": 3.50, "stock": 30, "capacity": 30,
                 "kind": "food", "nutrition": 5},
      "tea": {"price": 2.75, "stock": 24, "capacity": 24,
              "kind": "food", "nutrition": 5},
      "breakfast": {"price": 7.50, "stock": 16, "capacity": 16,
                    "kind": "food", "nutrition": 24},
      "sandwich": {"price": 8.50, "stock": 18, "capacity": 18,
                   "kind": "food", "nutrition": 22},
      "pastry": {"price": 4.00, "stock": 20, "capacity": 20,
                 "kind": "food", "nutrition": 12},
      "lunch": {"price": 10.00, "stock": 16, "capacity": 16,
                "kind": "food", "nutrition": 28},
    },
  },
  "The Rose and Crown Pub": {
    "owner": "Arthur Burton", "staff": [], "cash": 850.0,
    "goods": {
      "beer": {"price": 5.50, "stock": 36, "capacity": 36,
               "kind": "pub", "nutrition": 3},
      "cocktail": {"price": 8.50, "stock": 20, "capacity": 20,
                   "kind": "pub", "nutrition": 2},
      "dinner": {"price": 13.00, "stock": 20, "capacity": 20,
                 "kind": "food", "nutrition": 32},
    },
  },
  "The Willows Market and Pharmacy": {
    "owner": "Tom Moreno", "staff": ["John Lin"], "cash": 1200.0,
    "goods": {
      "groceries": {"price": 12.00, "stock": 28, "capacity": 28,
                    "kind": "market", "nutrition": 35},
      "medicine": {"price": 18.00, "stock": 16, "capacity": 16,
                   "kind": "market", "nutrition": 0},
      "supplies": {"price": 9.00, "stock": 24, "capacity": 24,
                   "kind": "market", "nutrition": 0},
    },
  },
  "Harvey Oak Supply Store": {
    "owner": "Carmen Ortiz", "staff": ["Adam Smith", "Tamara Taylor"],
    "cash": 750.0,
    "goods": {
      "supplies": {"price": 8.00, "stock": 32, "capacity": 32,
                   "kind": "market", "nutrition": 0},
      "art supplies": {"price": 15.00, "stock": 18, "capacity": 18,
                       "kind": "market", "nutrition": 0},
      "household supplies": {"price": 11.00, "stock": 20, "capacity": 20,
                             "kind": "market", "nutrition": 0},
    },
  },
}

FOOD_WORDS = {
  "coffee", "tea", "breakfast", "lunch", "dinner", "sandwich", "pastry",
}
MARKET_WORDS = {"groceries", "medicine", "supplies"}
PUB_WORDS = {"beer", "cocktail"}
GENERIC_BUY_WORDS = {
  "eat", "eating", "meal", "shopping", "buying", "purchasing", "ordering",
  "shop for", "drink", "drinking", "picks up", "gets a",
  "having breakfast", "having lunch", "having dinner", "having coffee",
  "having tea", "having beer", "having a beer", "having a cocktail",
}


def _money(value):
  """Return a finite dollar value rounded to cents."""
  try:
    value = float(value)
  except (TypeError, ValueError):
    return 0.0
  if not math.isfinite(value):
    return 0.0
  return round(value + 0.0, 2)


def _number(value, default=0.0):
  try:
    value = float(value)
  except (TypeError, ValueError):
    return float(default)
  return value if math.isfinite(value) else float(default)


def _integer(value, default=0):
  try:
    return int(value)
  except (TypeError, ValueError, OverflowError):
    return int(default)


def _stable_int(value):
  return int(hashlib.sha256(value.encode("utf-8")).hexdigest()[:16], 16)


def _initial_balance(name):
  return round(80.0 + (_stable_int("balance:" + name) % 12001) / 100.0, 2)


def _tier(balance):
  if balance < 5:
    return "broke"
  if balance < 25:
    return "tight"
  if balance < 150:
    return "stable"
  if balance < 500:
    return "comfortable"
  if balance < 2000:
    return "wealthy"
  return "rich"


def _public_status(resident):
  if resident.get("admin_status"):
    return resident["admin_status"]
  if resident.get("housing_status") == "homeless":
    return "homeless"
  if resident.get("health_status") == "ill":
    return "ill"
  if resident.get("hungry"):
    return "hungry"
  if resident.get("purchase_broke"):
    return "broke"
  return resident.get("wealth_tier") or _tier(resident.get("balance", 0))


def _default_resident(name, persona=None):
  living_area = getattr(getattr(persona, "scratch", None), "living_area", "") or ""
  balance = _initial_balance(name)
  return {
    "balance": balance,
    "total_earned": 0.0,
    "total_spent": 0.0,
    "debt": 0.0,
    "wealth_tier": _tier(balance),
    "status": _tier(balance),
    "admin_status": "",
    "housing_status": "housed",
    "health_status": "healthy",
    "hunger": 75.0,
    "energy": 75.0,
    "social": 75.0,
    "education_score": 0.0,
    "bankruptcies": 0,
    "unpaid_rent": 0,
    "purchase_broke": False,
    "hungry": False,
    "has_medicine": False,
    "ill_rest_minutes": 0.0,
    "last_purchase_key": "",
    "living_area": living_area,
    "rent": _rent_for(living_area, name),
    "student": name in STUDENTS,
    "jobs": copy.deepcopy(JOBS.get(name, [])),
  }


def _rent_for(living_area, name):
  area = (living_area or "").lower()
  if "dorm" in area:
    return 5.0
  if "co-living" in area:
    return 8.0
  if "apartment" in area:
    return 10.0
  if "house" in area or "family" in area:
    return 12.0
  return float(5 + (_stable_int("rent:" + name) % 11))


def _default_state(personas, curr_time):
  residents = {
    name: _default_resident(name, persona)
    for name, persona in personas.items()
  }
  return {
    "schema_version": SCHEMA_VERSION,
    "updated_at": curr_time.strftime("%Y-%m-%d %H:%M:%S"),
    "last_tick": curr_time.isoformat(),
    "last_day": curr_time.date().isoformat(),
    "last_month": curr_time.strftime("%Y-%m"),
    "residents": residents,
    "shops": copy.deepcopy(SHOP_DEFINITIONS),
    "bank": {
      "banker": "Yuriko Yamamoto", "cash": 20000.0,
      "interest_rate_daily": 0.0025, "interest_earned": 0.0,
      "fees_collected": 0.0,
    },
    "treasury": {"cash": 50000.0, "rent_collected": 0.0},
    "processed_commands": {},
  }


def _normalise_state(state, personas, curr_time):
  if not isinstance(state, dict):
    state = {}
  default = _default_state(personas, curr_time)
  for key, value in default.items():
    state.setdefault(key, value)
  if not isinstance(state.get("residents"), dict):
    state["residents"] = {}
  for name, persona in personas.items():
    resident = state["residents"].setdefault(name, _default_resident(name, persona))
    base = _default_resident(name, persona)
    for key, value in base.items():
      resident.setdefault(key, value)
    # Job definitions are code-owned; persisted balances and life state are not.
    resident["jobs"] = copy.deepcopy(JOBS.get(name, []))
    resident["student"] = name in STUDENTS
    if not resident.get("living_area"):
      resident["living_area"] = base["living_area"]
    resident["rent"] = _money(resident.get("rent", base["rent"]))
    for key in ("balance", "total_earned", "total_spent", "debt"):
      resident[key] = _money(resident.get(key))
    for key in ("hunger", "energy", "social"):
      resident[key] = max(0.0, min(100.0, _number(resident.get(key), 75.0)))
    resident["education_score"] = max(
      0.0, _number(resident.get("education_score"), 0.0))
    resident["bankruptcies"] = max(
      0, _integer(resident.get("bankruptcies"), 0))
    resident["unpaid_rent"] = max(
      0, _integer(resident.get("unpaid_rent"), 0))
    resident["wealth_tier"] = resident.get("wealth_tier") or _tier(resident["balance"])
    resident["status"] = resident.get("status") or _public_status(resident)

  if not isinstance(state.get("shops"), dict):
    state["shops"] = {}
  for shop_name, shop_default in SHOP_DEFINITIONS.items():
    shop = state["shops"].setdefault(shop_name, copy.deepcopy(shop_default))
    for key in ("owner", "staff", "cash"):
      shop.setdefault(key, copy.deepcopy(shop_default[key]))
    shop["cash"] = _money(shop["cash"])
    if not isinstance(shop.get("goods"), dict):
      shop["goods"] = {}
    for good_name, good_default in shop_default["goods"].items():
      good = shop["goods"].setdefault(good_name, copy.deepcopy(good_default))
      for key, value in good_default.items():
        good.setdefault(key, value)
      good["price"] = max(0.01, _money(good["price"]))
      good["capacity"] = max(0, _integer(good["capacity"]))
      good["stock"] = max(
        0, min(_integer(good["stock"]), good["capacity"]))

  if not isinstance(state.get("bank"), dict):
    state["bank"] = copy.deepcopy(default["bank"])
  for key, value in default["bank"].items():
    state["bank"].setdefault(key, value)
  for key in ("cash", "interest_earned", "fees_collected"):
    state["bank"][key] = _money(state["bank"].get(key, default["bank"].get(key, 0)))
  state["bank"]["interest_rate_daily"] = max(
    0.0, min(1.0, _number(state["bank"].get("interest_rate_daily"), 0.0025)))
  if not isinstance(state.get("treasury"), dict):
    state["treasury"] = copy.deepcopy(default["treasury"])
  for key, value in default["treasury"].items():
    state["treasury"].setdefault(key, value)
  for key in ("cash", "rent_collected"):
    state["treasury"][key] = _money(
      state["treasury"].get(key, default["treasury"].get(key, 0)))
  if not isinstance(state.get("processed_commands"), dict):
    state["processed_commands"] = {}
  state["schema_version"] = SCHEMA_VERSION
  return state


def _load_json(path, default):
  try:
    with open(path) as infile:
      value = json.load(infile)
    return value
  except Exception:
    return copy.deepcopy(default)


def _feed(entries, curr_time, step, text):
  entries.append({
    "ts": curr_time.strftime("%Y-%m-%d %H:%M:%S"),
    "step": int(step),
    "text": str(text),
  })
  if len(entries) > FEED_LIMIT:
    del entries[:-FEED_LIMIT]


def _location(maze, tile):
  try:
    detail = maze.access_tile(tile) or {}
  except Exception:
    detail = {}
  return detail.get("sector", "") or "", detail.get("arena", "") or ""


def _job_matches(job, sector, arena):
  if job.get("sector") != sector:
    return False
  return not job.get("arena") or job["arena"] == arena


def _pay_from(state, job, requested):
  payer = job.get("payer")
  if payer == "shop":
    account = state["shops"].get(job.get("shop"), {})
  elif payer == "bank":
    account = state["bank"]
  elif payer == "treasury":
    account = state["treasury"]
  else:
    return requested
  available = max(0.0, _money(account.get("cash", 0)))
  paid = min(requested, available)
  account["cash"] = _money(available - paid)
  return paid


def _run_payroll(state, personas, personas_tile, maze, delta_minutes,
                 curr_time, step, feed):
  if delta_minutes <= 0:
    return
  hours = delta_minutes / 60.0
  for name, persona in personas.items():
    resident = state["residents"][name]
    sector, arena = _location(maze, personas_tile.get(name))
    for job in JOBS.get(name, []):
      if not _job_matches(job, sector, arena):
        continue
      requested = _money(job["rate"] * hours)
      if requested <= 0:
        continue
      paid = _money(_pay_from(state, job, requested))
      if paid <= 0:
        continue
      resident["balance"] = _money(resident["balance"] + paid)
      resident["total_earned"] = _money(resident["total_earned"] + paid)
      label = job.get("income_label", "salary")
      _feed(feed, curr_time, step,
            "%s earned $%.2f in %s as %s." %
            (name, paid, label, job["role"]))


def _date_range(last_day, current_day):
  try:
    cursor = datetime.date.fromisoformat(last_day)
  except (TypeError, ValueError):
    return []
  days = []
  cursor += datetime.timedelta(days=1)
  # A corrupt cursor must not create an unbounded catch-up loop.
  while cursor <= current_day and len(days) < 366:
    days.append(cursor)
    cursor += datetime.timedelta(days=1)
  return days


def _daily_events(state, personas, day, curr_time, step, feed):
  for shop in state["shops"].values():
    for good in shop.get("goods", {}).values():
      good["stock"] = max(0, int(good.get("capacity", 0)))
  _feed(feed, curr_time, step, "Smallville's shops restocked for a new day.")

  for name, resident in state["residents"].items():
    if name in STUDENTS:
      stipend = min(6.0, max(0.0, _money(state["treasury"].get("cash", 0))))
      state["treasury"]["cash"] = _money(state["treasury"]["cash"] - stipend)
      resident["balance"] = _money(resident["balance"] + stipend)
      resident["total_earned"] = _money(resident["total_earned"] + stipend)
      _feed(feed, curr_time, step,
            "%s received a $%.2f student stipend." % (name, stipend))

    if resident.get("housing_status") != "homeless":
      rent = max(5.0, min(15.0, _money(resident.get("rent", 10.0))))
      paid = min(rent, max(0.0, resident["balance"]))
      shortfall = _money(rent - paid)
      resident["balance"] = _money(resident["balance"] - paid)
      resident["total_spent"] = _money(resident["total_spent"] + paid)
      state["treasury"]["cash"] = _money(state["treasury"]["cash"] + paid)
      state["treasury"]["rent_collected"] = _money(
        state["treasury"].get("rent_collected", 0) + paid)
      if shortfall:
        resident["debt"] = _money(resident["debt"] + shortfall)
        resident["unpaid_rent"] = int(resident.get("unpaid_rent", 0)) + 1
        _feed(feed, curr_time, step,
              "%s could not pay $%.2f rent; $%.2f became debt." %
              (name, rent, shortfall))
        if (resident["unpaid_rent"] >= 3
            and _stable_int("eviction:%s:%s" % (day.isoformat(), name)) % 2 == 0):
          resident["housing_status"] = "homeless"
          _feed(feed, curr_time, step,
                "%s became homeless after repeated unpaid rent." % name)
      else:
        resident["unpaid_rent"] = 0
        _feed(feed, curr_time, step,
              "%s paid $%.2f rent to the town treasury." % (name, rent))

    debt = max(0.0, _money(resident.get("debt", 0)))
    if debt:
      interest = max(0.01, _money(debt * state["bank"]["interest_rate_daily"]))
      paid_interest = min(interest, max(0.0, resident["balance"]))
      unpaid_interest = _money(interest - paid_interest)
      resident["balance"] = _money(resident["balance"] - paid_interest)
      resident["total_spent"] = _money(resident["total_spent"] + paid_interest)
      resident["debt"] = _money(resident["debt"] + unpaid_interest)
      state["bank"]["cash"] = _money(state["bank"]["cash"] + paid_interest)
      state["bank"]["interest_earned"] = _money(
        state["bank"].get("interest_earned", 0) + interest)
      _feed(feed, curr_time, step,
            "%s was charged $%.2f daily bank interest." % (name, interest))

    if (resident.get("health_status") != "ill"
        and _stable_int("illness:%s:%s" % (day.isoformat(), name)) % 100 == 0):
      resident["health_status"] = "ill"
      resident["has_medicine"] = False
      resident["ill_rest_minutes"] = 0.0
      _feed(feed, curr_time, step, "%s has fallen ill." % name)

  jane = state["residents"].get("Jane Moreno")
  tom = state["residents"].get("Tom Moreno")
  if jane is not None and tom is not None:
    allowance = 40.0
    paid = min(allowance, max(0.0, tom["balance"]))
    shortfall = _money(allowance - paid)
    tom["balance"] = _money(tom["balance"] - paid)
    tom["total_spent"] = _money(tom["total_spent"] + paid)
    tom["debt"] = _money(tom["debt"] + shortfall)
    jane["balance"] = _money(jane["balance"] + allowance)
    jane["total_earned"] = _money(jane["total_earned"] + allowance)
    _feed(feed, curr_time, step,
          "Jane Moreno received her $40.00 household allowance from Tom Moreno.")


def _action_text(persona):
  scratch = getattr(persona, "scratch", None)
  return str(getattr(scratch, "act_description", "") or "").lower()


def _action_key(persona, sector, arena):
  scratch = getattr(persona, "scratch", None)
  started = getattr(scratch, "act_start_time", None)
  if hasattr(started, "isoformat"):
    started = started.isoformat()
  return "%s|%s|%s|%s" % (_action_text(persona), started or "", sector, arena)


def _mentions(text, phrase):
  return bool(re.search(r"(?<!\w)%s(?!\w)" % re.escape(phrase), text))


def _purchase_candidates(shop, action):
  goods = shop.get("goods", {})
  if not any(_mentions(action, word) for word in GENERIC_BUY_WORDS):
    return []
  explicit = [
    (name, good) for name, good in goods.items()
    if _mentions(action, name.lower()) and int(good.get("stock", 0)) > 0
  ]
  if explicit:
    return explicit
  words = set()
  if any(_mentions(action, word) for word in FOOD_WORDS):
    words.add("food")
  if any(_mentions(action, word) for word in MARKET_WORDS):
    words.add("market")
  if any(_mentions(action, word) for word in PUB_WORDS):
    words.add("pub")
  if not words and any(_mentions(action, word) for word in GENERIC_BUY_WORDS):
    words = {"food", "market", "pub"}
  return [
    (name, good) for name, good in goods.items()
    if good.get("kind") in words and int(good.get("stock", 0)) > 0
  ]


def _declare_bankruptcy(state, name, resident, curr_time, step, feed):
  resident["bankruptcies"] = int(resident.get("bankruptcies", 0)) + 1
  filing_fee = 5.0
  resident["debt"] = _money(resident["debt"] + filing_fee)
  state["bank"]["fees_collected"] = _money(
    state["bank"].get("fees_collected", 0) + filing_fee)
  # Yuriko receives a filing fee from the bank, separately from hourly pay.
  banker = state["residents"].get("Yuriko Yamamoto")
  banker_fee = min(1.0, max(0.0, state["bank"]["cash"]))
  if banker is not None and banker_fee:
    state["bank"]["cash"] = _money(state["bank"]["cash"] - banker_fee)
    banker["balance"] = _money(banker["balance"] + banker_fee)
    banker["total_earned"] = _money(banker["total_earned"] + banker_fee)
  _feed(feed, curr_time, step, "Yuriko declares %s bankrupt." % name)
  if resident["bankruptcies"] >= 3 and resident.get("housing_status") != "homeless":
    resident["housing_status"] = "homeless"
    _feed(feed, curr_time, step,
          "%s became homeless after three bankruptcies." % name)


def _run_purchases(state, personas, personas_tile, maze, curr_time, step, feed):
  for name, persona in personas.items():
    sector, arena = _location(maze, personas_tile.get(name))
    shop = state["shops"].get(sector)
    if not shop:
      continue
    action = _action_text(persona)
    candidates = _purchase_candidates(shop, action)
    if not candidates:
      continue
    resident = state["residents"][name]
    action_key = _action_key(persona, sector, arena)
    if action_key == resident.get("last_purchase_key"):
      continue
    resident["last_purchase_key"] = action_key
    affordable = [item for item in candidates
                  if _money(item[1].get("price")) <= resident["balance"]]
    if not affordable:
      resident["purchase_broke"] = True
      cheapest = min(_money(item[1].get("price")) for item in candidates)
      _feed(feed, curr_time, step,
            "%s could not afford a $%.2f purchase at %s and is broke." %
            (name, cheapest, sector))
      if resident["balance"] <= 0:
        _declare_bankruptcy(state, name, resident, curr_time, step, feed)
      continue

    luxury = (_tier(resident.get("balance", 0)) == "rich"
              and _stable_int("luxury:%s:%s" % (name, action_key)) % 4 == 0)
    chooser = max if luxury else min
    good_name, good = chooser(
      affordable, key=lambda item: (_money(item[1].get("price")), item[0]))
    price = _money(good["price"])
    resident["balance"] = _money(resident["balance"] - price)
    resident["total_spent"] = _money(resident["total_spent"] + price)
    resident["purchase_broke"] = False
    shop["cash"] = _money(shop.get("cash", 0) + price)
    good["stock"] = max(0, int(good.get("stock", 0)) - 1)
    nutrition = float(good.get("nutrition", 0) or 0)
    if nutrition:
      resident["hunger"] = min(100.0, resident["hunger"] + nutrition)
    if good_name.lower() == "medicine":
      resident["has_medicine"] = True
    _feed(feed, curr_time, step,
          "%s bought %s at %s for $%.2f%s." %
          (name, good_name, sector, price, " as a rich-list treat" if luxury else ""))


def _run_health_and_education(state, personas, personas_tile, maze,
                              delta_minutes, curr_time, step, feed):
  hours = max(0.0, delta_minutes) / 60.0
  if hours <= 0:
    return
  for name, persona in personas.items():
    resident = state["residents"][name]
    sector, arena = _location(maze, personas_tile.get(name))
    action = _action_text(persona)
    is_sleeping = any(word in action for word in ("sleep", "nap", "rest in bed"))
    is_resting = is_sleeping or "resting" in action or "recover" in action
    resident["hunger"] = max(0.0, resident["hunger"] - 3.0 * hours)
    resident["social"] = max(0.0, resident["social"] - 1.5 * hours)
    if is_sleeping:
      resident["energy"] = min(100.0, resident["energy"] + 13.0 * hours)
    else:
      resident["energy"] = max(0.0, resident["energy"] - 2.5 * hours)
    if sector in ("Hobbs Cafe", "The Rose and Crown Pub"):
      resident["social"] = min(100.0, resident["social"] + 4.0 * hours)
    scratch = getattr(persona, "scratch", None)
    if getattr(scratch, "chatting_with", None):
      resident["social"] = min(100.0, resident["social"] + 8.0 * hours)
    if name in STUDENTS and sector == "Oak Hill College":
      resident["education_score"] = round(
        float(resident.get("education_score", 0)) + hours, 3)
    if resident.get("health_status") == "ill" and is_resting:
      resident["ill_rest_minutes"] = round(
        float(resident.get("ill_rest_minutes", 0)) + delta_minutes, 2)
      if resident.get("has_medicine") and resident["ill_rest_minutes"] >= 60:
        resident["health_status"] = "healthy"
        resident["has_medicine"] = False
        resident["ill_rest_minutes"] = 0.0
        _feed(feed, curr_time, step,
              "%s recovered after medicine and rest." % name)


def _sync_statuses(state, curr_time, step, feed):
  for name, resident in state["residents"].items():
    old_tier = resident.get("wealth_tier")
    new_tier = _tier(resident.get("balance", 0))
    if old_tier != new_tier:
      _feed(feed, curr_time, step,
            "%s moved from %s to %s wealth." % (name, old_tier, new_tier))
      if new_tier == "rich":
        _feed(feed, curr_time, step,
              "%s joined Smallville's rich list." % name)
    resident["wealth_tier"] = new_tier
    resident["hungry"] = bool(
      resident.get("hunger", 0) < 20
      and (new_tier == "broke" or resident.get("purchase_broke")))
    old_status = resident.get("status")
    new_status = _public_status(resident)
    resident["status"] = new_status
    if old_status != new_status:
      _feed(feed, curr_time, step,
            "%s's status changed from %s to %s." %
            (name, old_status or "unknown", new_status))


def _sync_scratch(state, personas, personas_tile, maze):
  snapshot = {}
  for name, persona in personas.items():
    resident = state["residents"][name]
    scratch = getattr(persona, "scratch", None)
    if scratch is not None:
      sector, arena = _location(maze, personas_tile.get(name))
      scratch.curr_sector = sector
      scratch.curr_arena = arena
      scratch.economy_balance = resident["balance"]
      scratch.economy_total_earned = resident["total_earned"]
      scratch.economy_total_spent = resident["total_spent"]
      scratch.economy_debt = resident["debt"]
      scratch.economy_status = resident["status"]
      scratch.health_hunger = round(resident["hunger"], 2)
      scratch.health_energy = round(resident["energy"], 2)
      scratch.health_social = round(resident["social"], 2)
      scratch.education_score = resident["education_score"]
    snapshot[name] = {
      "balance": resident["balance"], "status": resident["status"],
    }
  return snapshot


@contextmanager
def _locked_queue(temp_storage):
  os.makedirs(temp_storage, exist_ok=True)
  lock_path = os.path.join(temp_storage, "admin_commands.lock")
  with open(lock_path, "a+") as lock_file:
    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
    try:
      yield
    finally:
      fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _trim_processed(processed):
  while len(processed) > COMMAND_HISTORY_LIMIT:
    processed.pop(next(iter(processed)))


def _resident_for_command(state, name):
  if not isinstance(name, str) or name not in state["residents"]:
    raise ValueError("unknown persona")
  return state["residents"][name]


def _shop_for_command(state, name):
  if not isinstance(name, str):
    raise ValueError("unknown shop")
  matches = [key for key in state["shops"] if key.casefold() == name.casefold()]
  if not matches:
    raise ValueError("unknown shop")
  return matches[0], state["shops"][matches[0]]


def _positive_amount(value, label="amount"):
  value = _money(value)
  if value <= 0 or value > 1000000:
    raise ValueError("%s must be between 0.01 and 1000000" % label)
  return value


def _admin_embedding(description, context):
  if description in context["embedding_cache"]:
    return context["embedding_cache"][description]
  if context["embedding_calls"] >= MAX_ADMIN_EMBEDDINGS_PER_STEP:
    raise ValueError("admin embedding budget exhausted for this step")
  from persona.prompt_template.gpt_structure import get_embedding
  embedding = get_embedding(description)
  context["embedding_calls"] += 1
  context["embedding_cache"][description] = embedding
  return embedding


def _inject_memory(persona, description, embedding, curr_time):
  name = persona.name
  persona.a_mem.add_event(
    curr_time, None, name, "experiences", description,
    description, {name, "admin event"}, 5,
    (description, embedding), None)


def _write_pacing(temp_storage, pacing):
  pacing = int(pacing)
  if not 1 <= pacing <= 10000:
    raise ValueError("pacing out of range 1..10000")
  path = os.path.join(temp_storage, "pacing.txt")
  tmp = path + ".tmp"
  with open(tmp, "w") as outfile:
    outfile.write(str(pacing))
  os.replace(tmp, path)
  return pacing


def _execute_admin(command, state, personas, curr_time, step, feed,
                   temp_storage, context):
  cmd = command.get("cmd")
  args = command.get("args") or {}
  if not isinstance(args, dict):
    raise ValueError("args must be an object")
  if cmd == "give_money":
    name = args.get("persona")
    amount = _positive_amount(args.get("amount"))
    resident = _resident_for_command(state, name)
    resident["balance"] = _money(resident["balance"] + amount)
    resident["total_earned"] = _money(resident["total_earned"] + amount)
    resident["purchase_broke"] = False
    _feed(feed, curr_time, step, "%s received $%.2f by admin grant." % (name, amount))
    return {"persona": name, "balance": resident["balance"]}
  if cmd == "bankrupt":
    name = args.get("persona")
    resident = _resident_for_command(state, name)
    resident["balance"] = 0.0
    resident["purchase_broke"] = True
    _declare_bankruptcy(state, name, resident, curr_time, step, feed)
    return {"persona": name, "bankruptcies": resident["bankruptcies"]}
  if cmd == "make_rich":
    name = args.get("persona")
    resident = _resident_for_command(state, name)
    target = _money(args.get("amount", 2500.0))
    if target < 2000 or target > 1000000:
      raise ValueError("rich amount must be between 2000 and 1000000")
    grant = max(0.0, _money(target - resident["balance"]))
    resident["balance"] = max(resident["balance"], target)
    resident["total_earned"] = _money(resident["total_earned"] + grant)
    resident["purchase_broke"] = False
    _feed(feed, curr_time, step, "%s was made rich with $%.2f." % (name, target))
    return {"persona": name, "balance": resident["balance"]}
  if cmd == "set_status":
    name = args.get("persona")
    status = str(args.get("status", "")).strip().lower()
    if len(status) > 64:
      raise ValueError("status is too long")
    resident = _resident_for_command(state, name)
    if status == "homeless":
      resident["housing_status"] = "homeless"
      resident["admin_status"] = ""
    elif status in ("housed", "home"):
      resident["housing_status"] = "housed"
      resident["admin_status"] = ""
    elif status == "ill":
      resident["health_status"] = "ill"
      resident["admin_status"] = ""
    elif status in ("healthy", "well"):
      resident["health_status"] = "healthy"
      resident["admin_status"] = ""
    elif status == "hungry":
      resident["hunger"] = min(resident["hunger"], 10.0)
      resident["admin_status"] = "hungry"
    else:
      resident["admin_status"] = status
    _feed(feed, curr_time, step,
          "%s received admin status %s." % (name, status or "automatic"))
    return {"persona": name, "status": status or "automatic"}
  if cmd in ("inject_event", "broadcast"):
    description = str(args.get("description", "")).strip()
    if not description or len(description) > 2000:
      raise ValueError("description must contain 1..2000 characters")
    embedding = _admin_embedding(description, context)
    if cmd == "inject_event":
      name = args.get("persona")
      if name not in personas:
        raise ValueError("unknown persona")
      targets = [personas[name]]
    else:
      targets = list(personas.values())
    for persona in targets:
      _inject_memory(persona, description, embedding, curr_time)
    _feed(feed, curr_time, step,
          "Admin event reached %d resident%s: %s" %
          (len(targets), "" if len(targets) == 1 else "s", description))
    return {"injected": len(targets)}
  if cmd == "restock":
    shop_name, shop = _shop_for_command(state, args.get("shop"))
    for good in shop["goods"].values():
      good["stock"] = int(good.get("capacity", 0))
    _feed(feed, curr_time, step, "%s was restocked by admin." % shop_name)
    return {"shop": shop_name}
  if cmd == "adjust_price":
    shop_name, shop = _shop_for_command(state, args.get("shop"))
    good_name = str(args.get("good", "")).strip()
    matches = [key for key in shop["goods"] if key.casefold() == good_name.casefold()]
    if not matches:
      raise ValueError("unknown good")
    price = _positive_amount(args.get("price"), "price")
    shop["goods"][matches[0]]["price"] = price
    _feed(feed, curr_time, step,
          "%s changed %s's price to $%.2f." % (shop_name, matches[0], price))
    return {"shop": shop_name, "good": matches[0], "price": price}
  if cmd == "add_good":
    shop_name, shop = _shop_for_command(state, args.get("shop"))
    good_name = str(args.get("good", "")).strip().lower()
    if not good_name or len(good_name) > 80:
      raise ValueError("good must contain 1..80 characters")
    price = _positive_amount(args.get("price"), "price")
    capacity = int(args.get("capacity", 20))
    if not 1 <= capacity <= 10000:
      raise ValueError("capacity out of range 1..10000")
    shop["goods"][good_name] = {
      "price": price, "stock": capacity, "capacity": capacity,
      "kind": str(args.get("kind", "market"))[:32], "nutrition": 0,
    }
    _feed(feed, curr_time, step,
          "%s added %s at $%.2f." % (shop_name, good_name, price))
    return {"shop": shop_name, "good": good_name, "price": price}
  if cmd == "transfer":
    from_name = args.get("from")
    to_name = args.get("to")
    amount = _positive_amount(args.get("amount"))
    source = _resident_for_command(state, from_name)
    target = _resident_for_command(state, to_name)
    if from_name == to_name:
      raise ValueError("transfer parties must differ")
    if source["balance"] < amount:
      raise ValueError("insufficient funds")
    source["balance"] = _money(source["balance"] - amount)
    source["total_spent"] = _money(source["total_spent"] + amount)
    target["balance"] = _money(target["balance"] + amount)
    target["total_earned"] = _money(target["total_earned"] + amount)
    _feed(feed, curr_time, step,
          "%s transferred $%.2f to %s." % (from_name, amount, to_name))
    return {"from": from_name, "to": to_name, "amount": amount}
  if cmd == "set_pacing":
    pacing = _write_pacing(temp_storage, args.get("pacing"))
    return {"pacing": pacing}
  raise ValueError("unknown command")


def _drain_admin_commands(state, state_file, personas, curr_time, step, feed,
                          temp_storage):
  queue_file = os.path.join(temp_storage, "admin_commands.json")
  if not os.path.exists(queue_file):
    return
  with _locked_queue(temp_storage):
    queue = _load_json(queue_file, [])
    if not isinstance(queue, list):
      queue = []
    claimed = []
    for command in queue:
      if isinstance(command, dict) and command.get("status") in ("pending", "processing"):
        command["status"] = "processing"
        command["started_at"] = curr_time.strftime("%Y-%m-%d %H:%M:%S")
        claimed.append(copy.deepcopy(command))
    if not claimed:
      return
    atomic_json_dump(queue, queue_file)

  outcomes = {}
  context = {"embedding_cache": {}, "embedding_calls": 0}
  processed = state["processed_commands"]
  for command in claimed:
    command_id = str(command.get("id", "")).strip()
    if not command_id:
      continue
    if command_id in processed:
      outcomes[command_id] = processed[command_id]
      continue
    try:
      result = _execute_admin(command, state, personas, curr_time, step, feed,
                              temp_storage, context)
      outcome = {"status": "done", "result": result}
    except Exception as exc:
      outcome = {"status": "failed", "result": {"error": str(exc)}}
    processed[command_id] = outcome
    _trim_processed(processed)
    # Replay protection reaches disk before the queue is acknowledged.
    atomic_json_dump(state, state_file)
    outcomes[command_id] = outcome

  with _locked_queue(temp_storage):
    queue = _load_json(queue_file, [])
    if not isinstance(queue, list):
      queue = []
    for command in queue:
      if not isinstance(command, dict):
        continue
      outcome = outcomes.get(str(command.get("id", "")))
      if outcome:
        command.update(outcome)
        command["finished_at"] = curr_time.strftime("%Y-%m-%d %H:%M:%S")
    pending = [item for item in queue
               if isinstance(item, dict) and item.get("status") not in ("done", "failed")]
    completed = [item for item in queue
                 if isinstance(item, dict) and item.get("status") in ("done", "failed")]
    atomic_json_dump(pending + completed[-COMMAND_HISTORY_LIMIT:], queue_file)


def economy_tick(sim_folder, personas, personas_tile, maze, curr_time, step,
                 temp_storage):
  """Advance the life simulation once and return movement-safe snapshots.

  All normal work is O(residents + shops). The function accepts explicit
  paths and fake persona/maze objects so it can be smoke-tested without ever
  starting or touching the real simulation.
  """
  economy_folder = os.path.join(sim_folder, "economy")
  os.makedirs(economy_folder, exist_ok=True)
  state_file = os.path.join(economy_folder, "economy_state.json")
  feed_file = os.path.join(economy_folder, "economy_feed.json")
  state = _normalise_state(_load_json(state_file, {}), personas, curr_time)
  feed = _load_json(feed_file, [])
  if not isinstance(feed, list):
    feed = []
  feed = feed[-FEED_LIMIT:]

  # Admin mutations are intentionally ordered before the autonomous tick.
  _drain_admin_commands(state, state_file, personas, curr_time, step, feed,
                        temp_storage)

  try:
    last_tick = datetime.datetime.fromisoformat(state.get("last_tick", ""))
    delta_minutes = (curr_time - last_tick).total_seconds() / 60.0
  except (TypeError, ValueError):
    delta_minutes = 0.0
  if delta_minutes < 0 or not math.isfinite(delta_minutes):
    delta_minutes = 0.0

  for day in _date_range(state.get("last_day"), curr_time.date()):
    _daily_events(state, personas, day, curr_time, step, feed)
  state["last_day"] = curr_time.date().isoformat()

  current_month = curr_time.strftime("%Y-%m")
  if state.get("last_month") != current_month:
    for name in sorted(STUDENTS.intersection(state["residents"])):
      score = state["residents"][name].get("education_score", 0)
      _feed(feed, curr_time, step,
            "%s begins %s with %.2f education points." %
            (name, current_month, score))
    state["last_month"] = current_month

  _run_payroll(state, personas, personas_tile, maze, delta_minutes,
               curr_time, step, feed)
  _run_health_and_education(state, personas, personas_tile, maze,
                            delta_minutes, curr_time, step, feed)
  _run_purchases(state, personas, personas_tile, maze, curr_time, step, feed)
  _sync_statuses(state, curr_time, step, feed)

  state["last_tick"] = curr_time.isoformat()
  state["updated_at"] = curr_time.strftime("%Y-%m-%d %H:%M:%S")
  snapshot = _sync_scratch(state, personas, personas_tile, maze)
  atomic_json_dump(state, state_file)
  atomic_json_dump(feed[-FEED_LIMIT:], feed_file)
  return snapshot
