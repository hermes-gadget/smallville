#!/usr/bin/env python3
"""Offline Smallville economy smoke test.

Uses only a temporary directory and fake personas/maze tiles. It never opens
the real simulator storage or starts a server.
"""
import datetime
import json
import os
import sys
import tempfile
import time
import types
from types import SimpleNamespace


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_ROOT = os.path.join(REPO_ROOT, "reverie", "backend_server")
sys.path.insert(0, BACKEND_ROOT)

from economy import economy_tick  # noqa: E402


NAMES = [
  "Latoya Williams", "Rajiv Patel", "Abigail Chen", "Francisco Lopez",
  "Hailey Johnson", "Arthur Burton", "Ryan Park", "Isabella Rodriguez",
  "Giorgio Rossi", "Carlos Gomez", "Klaus Mueller", "Maria Lopez",
  "Ayesha Khan", "Wolfgang Schulz", "Mei Lin", "John Lin", "Eddy Lin",
  "Tom Moreno", "Jane Moreno", "Tamara Taylor", "Carmen Ortiz", "Sam Moore",
  "Jennifer Moore", "Yuriko Yamamoto", "Adam Smith",
]

STUDENT_NAMES = {
  "Ayesha Khan", "Wolfgang Schulz", "Klaus Mueller", "Mei Lin", "Eddy Lin",
  "Rajiv Patel", "Francisco Lopez", "Giorgio Rossi",
}

LOCATIONS = {
  "Isabella Rodriguez": ("Hobbs Cafe", "cafe"),
  "Maria Lopez": ("Hobbs Cafe", "cafe"),
  "Ryan Park": ("Hobbs Cafe", "cafe"),
  "Arthur Burton": ("The Rose and Crown Pub", "pub"),
  "Tom Moreno": ("The Willows Market and Pharmacy", "store"),
  "John Lin": ("The Willows Market and Pharmacy", "store"),
  "Carmen Ortiz": ("Harvey Oak Supply Store", "supply store"),
  "Adam Smith": ("Harvey Oak Supply Store", "supply store"),
  "Tamara Taylor": ("Harvey Oak Supply Store", "supply store"),
  "Latoya Williams": ("artist's co-living space", "common room"),
  "Jennifer Moore": ("artist's co-living space", "common room"),
  "Abigail Chen": ("artist's co-living space", "common room"),
  "Hailey Johnson": ("artist's co-living space", "common room"),
  "Yuriko Yamamoto": ("Yuriko Yamamoto's house", "main room"),
  "Sam Moore": ("Johnson Park", "park"),
  "Carlos Gomez": ("Carlos Gomez's apartment", "main room"),
  "Jane Moreno": ("Moreno family's house", "common room"),
}

ACTIONS = {
  "Isabella Rodriguez": "serving coffee behind the counter",
  "Maria Lopez": "taking cafe orders",
  "Ryan Park": "coding at a table",
  "Arthur Burton": "serving drinks behind the bar",
  "Tom Moreno": "managing the market",
  "John Lin": "dispensing medicine to a customer",
  "Carmen Ortiz": "organizing supplies",
  "Adam Smith": "stocking shelves",
  "Tamara Taylor": "stocking shelves",
  "Latoya Williams": "painting in the studio",
  "Jennifer Moore": "painting in the studio",
  "Abigail Chen": "animating a scene",
  "Hailey Johnson": "writing an article",
  "Yuriko Yamamoto": "reviewing town bank accounts",
  "Sam Moore": "planning the campaign",
  "Carlos Gomez": "writing poetry",
  "Jane Moreno": "resting at home",
}


class FakeMemory:
  def __init__(self):
    self.events = []

  def add_event(self, *args):
    self.events.append(args)


class FakePersona:
  def __init__(self, name, action, living_area, started):
    self.name = name
    self.a_mem = FakeMemory()
    self.scratch = SimpleNamespace(
      living_area=living_area,
      act_description=action,
      act_start_time=started,
      chatting_with=None,
    )


class FakeMaze:
  def __init__(self, locations):
    self.locations = locations

  def access_tile(self, tile):
    return self.locations[tile]


def _build_town(started):
  personas = {}
  tiles = {}
  tile_locations = {}
  for index, name in enumerate(NAMES):
    sector, arena = LOCATIONS.get(name, ("Oak Hill College", "classroom"))
    living_area = ("Dorm for Oak Hill College" if name in STUDENT_NAMES
                   else "a Smallville house")
    personas[name] = FakePersona(
      name, ACTIONS.get(name, "studying in class"), living_area, started)
    tile = (index, 0)
    tiles[name] = tile
    tile_locations[tile] = {"sector": sector, "arena": arena}
  return personas, tiles, FakeMaze(tile_locations)


def _commands():
  specs = [
    ("rich", "make_rich", {"persona": "Ryan Park"}),
    ("transfer", "transfer",
     {"from": "Ryan Park", "to": "Ayesha Khan", "amount": 10}),
    ("grant", "give_money", {"persona": "Ayesha Khan", "amount": 5}),
    ("bankrupt-1", "bankrupt", {"persona": "Carlos Gomez"}),
    ("bankrupt-2", "bankrupt", {"persona": "Carlos Gomez"}),
    ("bankrupt-3", "bankrupt", {"persona": "Carlos Gomez"}),
    ("status", "set_status", {"persona": "Jane Moreno", "status": "ill"}),
    ("restock", "restock", {"shop": "Hobbs Cafe"}),
    ("price", "adjust_price",
     {"shop": "Hobbs Cafe", "good": "coffee", "price": 4.25}),
    ("good", "add_good",
     {"shop": "Hobbs Cafe", "good": "scone", "price": 4.75}),
    ("event", "inject_event",
     {"persona": "Ayesha Khan",
      "description": "A scholarship interview is tomorrow."}),
    ("broadcast", "broadcast",
     {"description": "A summer fair opens in Johnson Park."}),
    ("pacing", "set_pacing", {"pacing": 120}),
  ]
  return [
    {"id": ident, "cmd": cmd, "args": args, "status": "pending"}
    for ident, cmd, args in specs
  ]


def main():
  embedding_calls = []
  fake_gpt = types.ModuleType("persona.prompt_template.gpt_structure")

  def fake_embedding(text):
    embedding_calls.append(text)
    return [0.1, 0.2, 0.3]

  fake_gpt.get_embedding = fake_embedding
  sys.modules["persona.prompt_template.gpt_structure"] = fake_gpt

  start = datetime.datetime(2023, 2, 13, 10, 0)
  personas, tiles, maze = _build_town(start)
  with tempfile.TemporaryDirectory(prefix="smallville-economy-") as root:
    sim_folder = os.path.join(root, "storage", "public_sim")
    temp_storage = os.path.join(root, "temp_storage")
    os.makedirs(temp_storage)
    economy_tick(sim_folder, personas, tiles, maze, start, 100, temp_storage)

    personas["Ryan Park"].scratch.act_description = "having lunch while coding"
    personas["Ryan Park"].scratch.act_start_time = (
      start + datetime.timedelta(minutes=1))
    with open(os.path.join(temp_storage, "admin_commands.json"), "w") as outfile:
      json.dump(_commands(), outfile)

    current = start + datetime.timedelta(hours=1)
    snapshot = economy_tick(
      sim_folder, personas, tiles, maze, current, 101, temp_storage)
    economy_folder = os.path.join(sim_folder, "economy")
    with open(os.path.join(economy_folder, "economy_state.json")) as infile:
      state = json.load(infile)
    with open(os.path.join(economy_folder, "economy_feed.json")) as infile:
      feed = json.load(infile)
    with open(os.path.join(temp_storage, "admin_commands.json")) as infile:
      queue = json.load(infile)

    assert len(state["residents"]) == 25
    assert all(item["status"] == "done" for item in queue)
    assert state["residents"]["Carlos Gomez"]["status"] == "homeless"
    assert state["residents"]["Ryan Park"]["wealth_tier"] == "rich"
    assert state["shops"]["Hobbs Cafe"]["goods"]["lunch"]["stock"] == 15
    assert state["shops"]["Hobbs Cafe"]["goods"]["coffee"]["price"] == 4.25
    assert state["shops"]["Hobbs Cafe"]["goods"]["scone"]["stock"] == 20
    assert state["residents"]["Ayesha Khan"]["education_score"] == 1.0
    assert len(personas["Ayesha Khan"].a_mem.events) == 2
    assert len(personas["Tom Moreno"].a_mem.events) == 1
    assert len(embedding_calls) == 2
    with open(os.path.join(temp_storage, "pacing.txt")) as infile:
      assert infile.read() == "120"

    jane_tile = tiles["Jane Moreno"]
    maze.locations[jane_tile] = {
      "sector": "The Willows Market and Pharmacy", "arena": "store"}
    personas["Jane Moreno"].scratch.act_description = "buying medicine"
    personas["Jane Moreno"].scratch.act_start_time = (
      current + datetime.timedelta(minutes=1))
    medicine_time = current + datetime.timedelta(minutes=1)
    economy_tick(sim_folder, personas, tiles, maze, medicine_time, 102,
                 temp_storage)
    maze.locations[jane_tile] = {
      "sector": "Moreno family's house", "arena": "common room"}
    personas["Jane Moreno"].scratch.act_description = "resting at home"
    personas["Jane Moreno"].scratch.act_start_time = medicine_time
    performance_base = medicine_time + datetime.timedelta(hours=1)
    recovered = economy_tick(
      sim_folder, personas, tiles, maze, performance_base, 103, temp_storage)
    assert recovered["Jane Moreno"]["status"] == "stable"

    timings = []
    for offset in range(1, 101):
      tick_at = performance_base + datetime.timedelta(minutes=offset)
      before = time.perf_counter()
      economy_tick(sim_folder, personas, tiles, maze, tick_at, 101 + offset,
                   temp_storage)
      timings.append((time.perf_counter() - before) * 1000)

    with open(os.path.join(economy_folder, "economy_state.json")) as infile:
      state_before_interest = json.load(infile)
    economy_tick(
      sim_folder, personas, tiles, maze,
      current + datetime.timedelta(days=1), 1000, temp_storage)
    with open(os.path.join(economy_folder, "economy_state.json")) as infile:
      state_after_interest = json.load(infile)
    with open(os.path.join(economy_folder, "economy_feed.json")) as infile:
      feed_after_interest = json.load(infile)
    assert state_after_interest["bank"]["interest_rate_daily"] == 0.0025
    assert (state_after_interest["bank"]["interest_earned"]
            > state_before_interest["bank"]["interest_earned"])
    assert len(feed_after_interest) == 300

    result = {
      "residents": len(state["residents"]),
      "commands": {"done": len(queue), "failed": 0},
      "embedding_calls": len(embedding_calls),
      "purchase": {"good": "lunch", "remaining_stock": 15},
      "student_score": state["residents"]["Ayesha Khan"]["education_score"],
      "bankruptcy": snapshot["Carlos Gomez"],
      "rich": snapshot["Ryan Park"],
      "ill": snapshot["Jane Moreno"],
      "recovered": recovered["Jane Moreno"],
      "feed_entries": len(feed_after_interest),
      "bank": {
        "interest_rate_daily": state_after_interest["bank"]["interest_rate_daily"],
        "interest_earned": state_after_interest["bank"]["interest_earned"],
      },
      "tick_ms_100_runs": {
        "average": round(sum(timings) / len(timings), 3),
        "maximum": round(max(timings), 3),
      },
    }
    assert result["tick_ms_100_runs"]["maximum"] < 50
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
  main()
