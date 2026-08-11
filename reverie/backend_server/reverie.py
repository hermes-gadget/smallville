"""
Author: Joon Sung Park (joonspk@stanford.edu)

File: reverie.py
Description: This is the main program for running generative agent simulations
that defines the ReverieServer class. This class maintains and records all  
states related to the simulation. The primary mode of interaction for those  
running the simulation should be through the open_server function, which  
enables the simulator to input command-line prompts for running and saving  
the simulation, among other tasks.

Release note (June 14, 2023) -- Reverie implements the core simulation 
mechanism described in my paper entitled "Generative Agents: Interactive 
Simulacra of Human Behavior." If you are reading through these lines after 
having read the paper, you might notice that I use older terms to describe 
generative agents and their cognitive modules here. Most notably, I use the 
term "personas" to refer to generative agents, "associative memory" to refer 
to the memory stream, and "reverie" to refer to the overarching simulation 
framework.
"""
import json
import numpy
import datetime
import pickle
import time
import math
import os
import shutil
import traceback
import signal
import sys
from concurrent.futures import ThreadPoolExecutor

from global_methods import *
from utils import *
from maze import *
from persona.persona import *
from economy import economy_tick

# [DATA-STORE] BEGIN: optional fail-open persistence dependency.
try:
  import sim_store as _sim_store
except Exception:
  _sim_store = None
# [DATA-STORE] END

# Recently-logged conversation signatures (bounded) so a conversation that
# persists in scratch.chat across many steps is never re-logged, even when
# several distinct conversations coexist in the same step.

def _current_pacing():
  """Live-flippable game speed: <fs_temp_storage>/pacing.txt overrides the
  utils default, so the speed can change WITHOUT restarting the service
  (just `echo 60 > .../temp_storage/pacing.txt`). Invalid/missing -> default."""
  try:
    _p = f"{fs_temp_storage}/pacing.txt"
    if os.path.exists(_p):
      _v = float(open(_p).read().strip())
      if _v > 0:
        return _v
  except Exception:
    pass
  return game_sec_per_real_sec


def _advance_clock(curr_time, last_real_time, now_real_time, pacing):
  """Advance a simulation clock without applying a new rate retroactively."""
  elapsed = max(0.0, float(now_real_time) - float(last_real_time))
  candidate = curr_time + datetime.timedelta(
    seconds=elapsed * max(0.0, float(pacing)))
  return max(curr_time, candidate), float(now_real_time)


def _validate_environment(value, persona_names, maze_width, maze_height):
  """Return a strict, normalized resident-to-coordinate mapping."""
  if not isinstance(value, dict) or set(value) != set(persona_names):
    raise ValueError("environment must contain exactly the resident roster")
  normalized = {}
  for name in persona_names:
    position = value[name]
    if not isinstance(position, dict) or set(position) != {"x", "y"}:
      raise ValueError("resident position must contain only x and y")
    x, y = position["x"], position["y"]
    if (isinstance(x, bool) or isinstance(y, bool)
        or not isinstance(x, int) or not isinstance(y, int)):
      raise ValueError("resident coordinates must be integers")
    if not (0 <= x < maze_width and 0 <= y < maze_height):
      raise ValueError("resident coordinates are outside the maze")
    normalized[name] = {"x": x, "y": y}
  return normalized


def _select_disjoint_reactions(intents, persona_names):
  """Select deterministic reactions in which each chat participant is unique."""
  available = set(persona_names)
  claimed = set()
  selected = []
  for persona_name in sorted(intents):
    reaction_mode = intents[persona_name]
    if (not reaction_mode or persona_name not in available
        or persona_name in claimed):
      continue
    if reaction_mode[:9] == "chat with":
      target_name = reaction_mode[9:].strip()
      if (persona_name in claimed or target_name in claimed
          or target_name not in available):
        continue
      claimed.update((persona_name, target_name))
    else:
      claimed.add(persona_name)
    selected.append((persona_name, reaction_mode))
  return selected


def _prune_movement_history(movement_folder, newest_step, keep=500,
                            full_scan=False):
  """Retain only the newest numeric movement files.

  A single startup scan clears an existing backlog. Normal steps remove one
  exact old filename, avoiding an ever-growing directory scan in the hot loop.
  """
  cutoff = int(newest_step) - int(keep) + 1
  if cutoff <= 0:
    return
  try:
    if full_scan:
      candidates = os.listdir(movement_folder)
    else:
      candidates = [f"{cutoff - 1}.json"]
    for filename in candidates:
      stem, extension = os.path.splitext(filename)
      if extension != ".json" or not stem.isdigit() or int(stem) >= cutoff:
        continue
      try:
        os.remove(os.path.join(movement_folder, filename))
      except FileNotFoundError:
        pass
  except Exception:
    # History retention is housekeeping and must never stop a town step.
    pass


def _prune_environment_history(environment_folder, current_step):
  """Keep only the next environment input; consumed files are transient."""
  try:
    for filename in os.listdir(environment_folder):
      stem, extension = os.path.splitext(filename)
      if extension != ".json" or not stem.isdigit():
        continue
      if int(stem) != int(current_step):
        try:
          os.remove(os.path.join(environment_folder, filename))
        except FileNotFoundError:
          pass
  except Exception:
    # Retention is housekeeping and must never stop a town step.
    pass

##############################################################################
#                                  REVERIE                                   #
##############################################################################

class ReverieServer: 
  def __init__(self, 
               fork_sim_code,
               sim_code):
    # FORKING FROM A PRIOR SIMULATION:
    # <fork_sim_code> indicates the simulation we are forking from. 
    # Interestingly, all simulations must be forked from some initial 
    # simulation, where the first simulation is "hand-crafted".
    self.fork_sim_code = fork_sim_code
    fork_folder = f"{fs_storage}/{self.fork_sim_code}"

    # <sim_code> indicates our current simulation. The first step here is to 
    # copy everything that's in <fork_sim_code>, but edit its 
    # reverie/meta/json's fork variable. When forking a simulation into
    # ITSELF (resume pattern: same sim name on restart), there is nothing
    # to copy -- we simply load the existing state. This also avoids
    # shutil.copytree crashing on an existing destination.
    self.sim_code = sim_code
    sim_folder = f"{fs_storage}/{self.sim_code}"
    if fork_sim_code != sim_code:
      copyanything(fork_folder, sim_folder)

    with open(f"{sim_folder}/reverie/meta.json") as json_file:  
      reverie_meta = json.load(json_file)

    reverie_meta["fork_sim_code"] = fork_sim_code
    atomic_json_dump(reverie_meta, f"{sim_folder}/reverie/meta.json")

    # LOADING REVERIE'S GLOBAL VARIABLES
    # The start datetime of the Reverie: 
    # <start_datetime> is the datetime instance for the start datetime of 
    # the Reverie instance. Once it is set, this is not really meant to 
    # change. It takes a string date in the following example form: 
    # "June 25, 2022"
    # e.g., ...strptime(June 25, 2022, "%B %d, %Y")
    self.start_time = datetime.datetime.strptime(
                        f"{reverie_meta['start_date']}, 00:00:00",  
                        "%B %d, %Y, %H:%M:%S")
    # <curr_time> is the datetime instance that indicates the game's current
    # time. This gets incremented by <sec_per_step> amount everytime the world
    # progresses (that is, everytime curr_env_file is recieved). 
    self.curr_time = datetime.datetime.strptime(reverie_meta['curr_time'], 
                                                "%B %d, %Y, %H:%M:%S")
    # <sec_per_step> denotes the number of seconds in game time that each 
    # step moves foward. 
    self.sec_per_step = reverie_meta['sec_per_step']
    
    # <maze> is the main Maze instance. Note that we pass in the maze_name
    # (e.g., "double_studio") to instantiate Maze. 
    # e.g., Maze("double_studio")
    self.maze = Maze(reverie_meta['maze_name'])
    
    # <step> denotes the number of steps that our game has taken. A step here
    # literally translates to the number of moves our personas made in terms
    # of the number of tiles. 
    self.step = reverie_meta['step']

    # [DATA-STORE] BEGIN: bounded background hooks never own loop liveness.
    self._data_store = _sim_store.BoundedStore() if _sim_store else None
    if self._data_store is None:
      print("[DATA-STORE] module unavailable; disabled", flush=True)
    # [DATA-STORE] END

    # SETTING UP PERSONAS IN REVERIE
    # <personas> is a dictionary that takes the persona's full name as its 
    # keys, and the actual persona instance as its values.
    # This dictionary is meant to keep track of all personas who are part of
    # the Reverie instance. 
    # e.g., ["Isabella Rodriguez"] = Persona("Isabella Rodriguezs")
    self.personas = dict()
    # <personas_tile> is a dictionary that contains the tile location of
    # the personas (!-> NOT px tile, but the actual tile coordinate).
    # The tile take the form of a set, (row, col). 
    # e.g., ["Isabella Rodriguez"] = (58, 39)
    self.personas_tile = dict()
    
    # # <persona_convo_match> is a dictionary that describes which of the two
    # # personas are talking to each other. It takes a key of a persona's full
    # # name, and value of another persona's full name who is talking to the 
    # # original persona. 
    # # e.g., dict["Isabella Rodriguez"] = ["Maria Lopez"]
    # self.persona_convo_match = dict()
    # # <persona_convo> contains the actual content of the conversations. It
    # # takes as keys, a pair of persona names, and val of a string convo. 
    # # Note that the key pairs are *ordered alphabetically*. 
    # # e.g., dict[("Adam Abraham", "Zane Xu")] = "Adam: baba \n Zane:..."
    # self.persona_convo = dict()

    # Loading in all personas. 
    init_env_file = f"{sim_folder}/environment/{str(self.step)}.json"
    init_env = json.load(open(init_env_file))
    for persona_name in reverie_meta['persona_names']: 
      persona_folder = f"{sim_folder}/personas/{persona_name}"
      p_x = init_env[persona_name]["x"]
      p_y = init_env[persona_name]["y"]
      curr_persona = Persona(persona_name, persona_folder)

      self.personas[persona_name] = curr_persona
      self.personas_tile[persona_name] = (p_x, p_y)
      self.maze.tiles[p_y][p_x]["events"].add(curr_persona.scratch
                                              .get_curr_event_and_desc())

    # REVERIE SETTINGS PARAMETERS:  
    # <server_sleep> denotes the amount of time that our while loop rests each
    # cycle; this is to not kill our machine. 
    self.server_sleep = 0.1

    # SIGNALING THE FRONTEND SERVER: 
    # curr_sim_code.json contains the current simulation code, and
    # curr_step.json contains the current step of the simulation. These are 
    # used to communicate the code and step information to the frontend. 
    # Note that step file is removed as soon as the frontend opens up the 
    # simulation. 
    curr_sim_code = dict()
    curr_sim_code["sim_code"] = self.sim_code
    with open(f"{fs_temp_storage}/curr_sim_code.json", "w") as outfile: 
      outfile.write(json.dumps(curr_sim_code, indent=2))
    
    curr_step = dict()
    curr_step["step"] = self.step
    with open(f"{fs_temp_storage}/curr_step.json", "w") as outfile: 
      outfile.write(json.dumps(curr_step, indent=2))


  def save(self): 
    """
    Save all Reverie progress -- this includes Reverie's global state as well
    as all the personas.  

    INPUT
      None
    OUTPUT 
      None
      * Saves all relevant data to the designated memory directory
    """
    # <sim_folder> points to the current simulation folder.
    sim_folder = f"{fs_storage}/{self.sim_code}"

    # Save Reverie meta information.
    reverie_meta = dict() 
    reverie_meta["fork_sim_code"] = self.fork_sim_code
    reverie_meta["start_date"] = self.start_time.strftime("%B %d, %Y")
    reverie_meta["curr_time"] = self.curr_time.strftime("%B %d, %Y, %H:%M:%S")
    reverie_meta["sec_per_step"] = self.sec_per_step
    reverie_meta["maze_name"] = self.maze.maze_name
    reverie_meta["persona_names"] = list(self.personas.keys())
    reverie_meta["step"] = self.step
    reverie_meta["clock_pacing"] = _current_pacing()
    reverie_meta["clock_updated_at"] = datetime.datetime.utcnow().strftime(
      "%Y-%m-%dT%H:%M:%SZ")
    reverie_meta_f = f"{sim_folder}/reverie/meta.json"
    atomic_json_dump(reverie_meta, reverie_meta_f)

    # Save the personas.
    for persona_name, persona in self.personas.items(): 
      save_folder = f"{sim_folder}/personas/{persona_name}/bootstrap_memory"
      persona.save(save_folder)


  def start_path_tester_server(self): 
    """
    Starts the path tester server. This is for generating the spatial memory
    that we need for bootstrapping a persona's state. 

    To use this, you need to open server and enter the path tester mode, and
    open the front-end side of the browser. 

    INPUT 
      None
    OUTPUT 
      None
      * Saves the spatial memory of the test agent to the path_tester_env.json
        of the temp storage. 
    """
    def print_tree(tree): 
      def _print_tree(tree, depth):
        dash = " >" * depth

        if type(tree) == type(list()): 
          if tree:
            print (dash, tree)
          return 

        for key, val in tree.items(): 
          if key: 
            print (dash, key)
          _print_tree(val, depth+1)
      
      _print_tree(tree, 0)

    # <curr_vision> is the vision radius of the test agent. Recommend 8 as 
    # our default. 
    curr_vision = 8
    # <s_mem> is our test spatial memory. 
    s_mem = dict()

    # The main while loop for the test agent. 
    while (True): 
      try: 
        curr_dict = {}
        tester_file = fs_temp_storage + "/path_tester_env.json"
        if check_if_file_exists(tester_file): 
          with open(tester_file) as json_file: 
            curr_dict = json.load(json_file)
            os.remove(tester_file)
          
          # Current camera location
          curr_sts = self.maze.sq_tile_size
          curr_camera = (int(math.ceil(curr_dict["x"]/curr_sts)), 
                         int(math.ceil(curr_dict["y"]/curr_sts))+1)
          curr_tile_det = self.maze.access_tile(curr_camera)

          # Initiating the s_mem
          world = curr_tile_det["world"]
          if curr_tile_det["world"] not in s_mem: 
            s_mem[world] = dict()

          # Iterating throughn the nearby tiles.
          nearby_tiles = self.maze.get_nearby_tiles(curr_camera, curr_vision)
          for i in nearby_tiles: 
            i_det = self.maze.access_tile(i)
            if (curr_tile_det["sector"] == i_det["sector"] 
                and curr_tile_det["arena"] == i_det["arena"]): 
              if i_det["sector"] != "": 
                if i_det["sector"] not in s_mem[world]: 
                  s_mem[world][i_det["sector"]] = dict()
              if i_det["arena"] != "": 
                if i_det["arena"] not in s_mem[world][i_det["sector"]]: 
                  s_mem[world][i_det["sector"]][i_det["arena"]] = list()
              if i_det["game_object"] != "": 
                if (i_det["game_object"] 
                    not in s_mem[world][i_det["sector"]][i_det["arena"]]):
                  s_mem[world][i_det["sector"]][i_det["arena"]] += [
                                                         i_det["game_object"]]

        # Incrementally outputting the s_mem and saving the json file. 
        print ("= " * 15)
        out_file = fs_temp_storage + "/path_tester_out.json"
        with open(out_file, "w") as outfile: 
          outfile.write(json.dumps(s_mem, indent=2))
        print_tree(s_mem)

      except:
        pass

      time.sleep(self.server_sleep * 10)


  def start_server(self, int_counter): 
    """
    The main backend server of Reverie. 
    This function retrieves the environment file from the frontend to 
    understand the state of the world, calls on each personas to make 
    decisions based on the world state, and saves their moves at certain step
    intervals. 
    INPUT
      int_counter: Integer value for the number of steps left for us to take
                   in this iteration. 
    OUTPUT 
      None
    """
    # <sim_folder> points to the current simulation folder.
    sim_folder = f"{fs_storage}/{self.sim_code}"

    # Graceful shutdown: a SIGTERM (systemd restart) mid-run used to lose
    # the world clock + step (save() only ran at run end), rewinding the
    # town on every restart. Save on signal so restarts resume in place.
    _shutdown_requested = False

    def _shutdown_save(signum, frame):
      nonlocal _shutdown_requested
      print("[reverie] signal %s received; deferring save to step boundary" %
            signum, flush=True)
      _shutdown_requested = True
    try:
      signal.signal(signal.SIGTERM, _shutdown_save)
      signal.signal(signal.SIGINT, _shutdown_save)
    except Exception:
      pass

    # Forked base sims ship without movement/ (upstream demos fork from the
    # July1 sims, which have it). Create the runtime dirs so the first step
    # doesn't crash on the movement write.
    os.makedirs(f"{sim_folder}/movement", exist_ok=True)
    os.makedirs(f"{sim_folder}/environment", exist_ok=True)
    os.makedirs(f"{sim_folder}/reverie", exist_ok=True)
    _prune_environment_history(f"{sim_folder}/environment", self.step)
    _prune_movement_history(f"{sim_folder}/movement", self.step - 1,
                            full_scan=True)
    # A resumed run may find STALE movement files from a previous run at
    # step numbers >= the resume point (the old run kept writing past the
    # saved meta step). The frontend polls movement/{step}.json and would
    # show those old dates until the new run overwrites each file, so
    # delete them at boot: the new run recreates each file when it reaches
    # that step.
    try:
      _stale_movement = [
        filename for filename in os.listdir(f"{sim_folder}/movement")
        if filename.endswith(".json")
        and filename[:-5].isdigit()
        and int(filename[:-5]) >= self.step
      ]
      for filename in _stale_movement:
        os.remove(os.path.join(f"{sim_folder}/movement", filename))
    except Exception:
      pass

    # When a persona arrives at a game object, we give a unique event
    # to that object. 
    # e.g., ('double studio[...]:bed', 'is', 'unmade', 'unmade')
    # Later on, before this cycle ends, we need to return that to its 
    # initial state, like this: 
    # e.g., ('double studio[...]:bed', None, None, None)
    # So we need to keep track of which event we added. 
    # <game_obj_cleanup> is used for that. 
    game_obj_cleanup = dict()

    # The main while loop of Reverie. 
    _last_clock_real = time.monotonic()
    _last_env_error_log = 0.0
    while (True): 
      if _shutdown_requested:
        self.save()
        return
      # Done with this iteration if <int_counter> reaches 0. 
      if int_counter == 0: 
        break

      # <curr_env_file> file is the file that our frontend outputs. When the
      # frontend has done its job and moved the personas, then it will put a 
      # new environment file that matches our step count. That's when we run 
      # the content of this for loop. Otherwise, we just wait. 
      curr_env_file = f"{sim_folder}/environment/{self.step}.json"
      if self._data_store is not None:
        self._data_store.poll_maintenance(self.personas, sim_folder)
      if check_if_file_exists(curr_env_file):
        # Mark the real-time start of this step so the game clock can be
        # paced from actual elapsed wall time.
        _step_real_start = time.time()
        env_retrieved = False
        new_env = None
        # If we have an environment file, it means we have a new perception
        # input to our personas. So we first retrieve it.
        try:
          with open(curr_env_file) as json_file:
            new_env = _validate_environment(
              json.load(json_file), self.personas.keys(),
              self.maze.maze_width, self.maze.maze_height)
          env_retrieved = True
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as error:
          now = time.monotonic()
          if now - _last_env_error_log >= 5.0:
            print("[reverie] invalid environment step %s: %s" %
                  (self.step, error), file=sys.stderr, flush=True)
            _last_env_error_log = now
      
        if env_retrieved: 
          # This is where we go through <game_obj_cleanup> to clean up all 
          # object actions that were used in this cylce. 
          for key, val in game_obj_cleanup.items(): 
            # We turn all object actions to their blank form (with None). 
            self.maze.turn_event_from_tile_idle(key, val)
          # Then we initialize game_obj_cleanup for this cycle. 
          game_obj_cleanup = dict()

          # We first move our personas in the backend environment to match 
          # the frontend environment. 
          for persona_name, persona in self.personas.items(): 
            # <curr_tile> is the tile that the persona was at previously. 
            curr_tile = self.personas_tile[persona_name]
            # <new_tile> is the tile that the persona will move to right now,
            # during this cycle. 
            new_tile = (new_env[persona_name]["x"], 
                        new_env[persona_name]["y"])

            # We actually move the persona on the backend tile map here. 
            self.personas_tile[persona_name] = new_tile
            self.maze.remove_subject_events_from_tile(persona.name, curr_tile)
            self.maze.add_event_from_tile(persona.scratch
                                         .get_curr_event_and_desc(), new_tile)

            # Now, the persona will travel to get to their destination. *Once*
            # the persona gets there, we activate the object action.
            if not persona.scratch.planned_path: 
              # We add that new object action event to the backend tile map. 
              # At its creation, it is stored in the persona's backend. 
              game_obj_cleanup[persona.scratch
                               .get_curr_obj_event_and_desc()] = new_tile
              self.maze.add_event_from_tile(persona.scratch
                                     .get_curr_obj_event_and_desc(), new_tile)
              # We also need to remove the temporary blank action for the 
              # object that is currently taking the action. 
              blank = (persona.scratch.get_curr_obj_event_and_desc()[0], 
                       None, None, None)
              self.maze.remove_event_from_tile(blank, new_tile)

          # Economy/life state observes the action chosen on the preceding
          # step at the location just reached. It runs before perception and
          # is isolated so corrupt runtime state or an admin command can never
          # interrupt the existing sequential-perceive/parallel-decide loop.
          economy_snapshot = {}
          try:
            economy_snapshot = economy_tick(
              sim_folder, self.personas, self.personas_tile, self.maze,
              self.curr_time, self.step, fs_temp_storage)
          except Exception:
            traceback.print_exc()

          # Then we need to actually have each of the personas perceive and
          # move. The movement for each of the personas comes in the form of
          # x y coordinates where the persona will move towards. e.g., (50, 34)
          # This is where the core brains of the personas are invoked. 
          #
          # Phase 1 (sequential, LLM-free): perceive COLLECTION. Chat-critical
          # state is only read here; the poignancy LLM calls and memory
          # commits are deferred into the parallel decide phase below, so a
          # chat-heavy step no longer serializes the whole town.
          import time as _time
          _t0 = _time.time()
          perceive_state = dict()
          for persona_name, persona in self.personas.items():
            try:
              perceive_state[persona_name] = persona._move_perceive(
                self.maze, self.personas_tile[persona_name], self.curr_time)
            except Exception:
              # A perception failure must not kill the whole town.
              traceback.print_exc()
              perceive_state[persona_name] = (None, None)
          _t1 = _time.time()

          # Phase 2 (parallel): retrieve / plan / reflect / execute. All
          # persona-private LLM + embedding work with no shared writes, so
          # the whole town decides concurrently -- a ~3-4x step speedup.
          reset_reflection_budget()
          movements = {"persona": dict(), 
                       "meta": dict()}
          with ThreadPoolExecutor(max_workers=6) as _ex:
            _futs = {_ex.submit(persona._move_decide,
                                self.maze, self.personas,
                                perceive_state[n][0], perceive_state[n][1],
                                True): n
                     for n, persona in self.personas.items()}
            _focused = {}
            for _f in _futs:
              persona_name = _futs[_f]
              try:
                _focused[persona_name] = _f.result()
              except Exception:
                traceback.print_exc()
                _focused[persona_name] = False

          # Reaction intentions are read-only. A single deterministic
          # coordinator selects disjoint pairs and performs cross-persona
          # mutations, so no target can be claimed or overwritten twice.
          with ThreadPoolExecutor(max_workers=6) as _ex:
            _intent_futs = {
              _ex.submit(prepare_reaction, self.personas[name], focused,
                         self.personas): name
              for name, focused in _focused.items() if focused
            }
            _intents = {}
            for _f, name in _intent_futs.items():
              try:
                _intents[name] = _f.result()
              except Exception:
                traceback.print_exc()
                _intents[name] = False

          for persona_name, reaction_mode in _select_disjoint_reactions(
              _intents, self.personas):
            commit_reaction(self.maze, self.personas[persona_name],
                            _focused[persona_name], reaction_mode,
                            self.personas)

          with ThreadPoolExecutor(max_workers=6) as _ex:
            _execute_futs = {
              _ex.submit(persona._move_execute, self.maze, self.personas): name
              for name, persona in self.personas.items()
            }
            for _f, persona_name in _execute_futs.items():
              try:
                next_tile, pronunciatio, description = _f.result()
              except Exception:
                traceback.print_exc()
                _p = self.personas[persona_name]
                next_tile = self.personas_tile[persona_name]
                pronunciatio = "…"
                description = (getattr(_p.scratch, "curr_description", None)
                               or "continuing what I was doing")
              persona = self.personas[persona_name]
              movements["persona"][persona_name] = {}
              movements["persona"][persona_name]["movement"] = next_tile
              movements["persona"][persona_name]["pronunciatio"] = pronunciatio
              movements["persona"][persona_name]["description"] = description
              movements["persona"][persona_name]["chat"] = (persona
                                                            .scratch.chat)
              movements["persona"][persona_name]["economy"] = (
                economy_snapshot.get(persona_name) or {
                  "balance": getattr(persona.scratch, "economy_balance", 0),
                  "status": getattr(persona.scratch, "economy_status",
                                    "stable"),
                })

          # Live chat feed: logged per-exchange inside converse.py (agent_chat_v2
          # appends the growing thread as each utterance happens), so the feed
          # updates mid-conversation. No step-level logging here anymore.
          import utils
          utils.current_step = self.step

          # Include the meta information about the current stage in the 
          # movements dictionary. 
          movements["meta"]["curr_time"] = (self.curr_time 
                                            .strftime("%B %d, %Y, %H:%M:%S"))
          movements["meta"]["step"] = self.step
          # We then write the personas' movements to a file that will be sent 
          # to the frontend server. 
          # Example json output: 
          # {"persona": {"Maria Lopez": {"movement": [58, 9]}},
          #  "persona": {"Klaus Mueller": {"movement": [38, 12]}}, 
          #  "meta": {curr_time: <datetime>}}
          curr_move_file = f"{sim_folder}/movement/{self.step}.json"
          atomic_json_dump(movements, curr_move_file)
          _prune_movement_history(f"{sim_folder}/movement", self.step)
          atomic_json_dump({
            "step": self.step,
            "persona": {
              name: {"movement": list(details.get("movement") or [])}
              for name, details in movements["persona"].items()
            },
          }, f"{sim_folder}/reverie/current_state.json")

          # [DATA-STORE] BEGIN: persist the complete step without changing JSON.
          if self._data_store is not None:
            try:
              _store_meta = dict(movements["meta"])
              _store_meta["personas_tile"] = dict(self.personas_tile)
              _store_meta["locations"] = {
                name: self.maze.access_tile(tile)
                for name, tile in self.personas_tile.items()
              }
              self._data_store.store_step(
                self.step, movements["persona"], _store_meta)
            except Exception:
              self._data_store.warn_once(
                "hook-step", "step hook failed; continuing")
          # [DATA-STORE] END

          # Persist each persona's live state. scratch.json is written EVERY
          # step (the frontend modal reads it live); the full memory dump
          # (nodes.json + embeddings.json, ~250KB per persona) is throttled
          # to every 10 steps to keep per-step I/O bounded -- the embeddings
          # repair script covers a corrupt full save.
          _full_save = (self.step % 10 == 0)
          for persona_name, persona in self.personas.items():
            persona.save(f"{sim_folder}/personas/{persona_name}/bootstrap_memory",
                         full=_full_save)

          # Persist the world clock + step on every full save so a SIGTERM
          # restart resumes where the town was instead of re-anchoring the
          # clock to the last COMPLETED run (pre-existing bug: save() only
          # ran at run end, so systemd restarts rewound the town to the
          # original fork date/step 0).
          if _full_save:
            try:
              _meta = {
                "fork_sim_code": self.fork_sim_code,
                "start_date": self.start_time.strftime("%B %d, %Y"),
                "curr_time": self.curr_time.strftime("%B %d, %Y, %H:%M:%S"),
                "sec_per_step": self.sec_per_step,
                "maze_name": self.maze.maze_name,
                "persona_names": list(self.personas.keys()),
                "step": self.step,
                "clock_pacing": _current_pacing(),
                "clock_updated_at": datetime.datetime.utcnow().strftime(
                  "%Y-%m-%dT%H:%M:%SZ"),
              }
              atomic_json_dump(_meta, f"{sim_folder}/reverie/meta.json")
            except Exception:
              print("[reverie] meta.json persist failed; continuing",
                    flush=True)

          # [DATA-STORE] BEGIN: light incremental sync after each full save.
          if _full_save and self._data_store is not None:
            try:
              _memory_snapshots = {
                persona_name: (dict(persona.a_mem.id_to_node),
                               dict(persona.a_mem.embeddings))
                for persona_name, persona in self.personas.items()
              }
              self._data_store.store_memories(_memory_snapshots)
            except Exception:
              self._data_store.warn_once(
                "hook-memory", "memory hook failed; continuing")
          # [DATA-STORE] END

          # [DATA-STORE] BEGIN: seven-day archive, guarded eviction, JSON tail.
          if (self.step > 0 and self.step % 1440 == 0
              and self._data_store is not None):
            try:
              _archive_cutoff = self.curr_time - datetime.timedelta(days=7)
              self._data_store.archive_and_evict(
                _archive_cutoff, self.personas, sim_folder, self.curr_time)
            except Exception:
              self._data_store.warn_once(
                "hook-maintenance", "maintenance hook failed; continuing")
          # [DATA-STORE] END

          # Environment inputs are a one-step handoff, not simulation history.
          # Remove the consumed file only after the movement/current-state and
          # persona checkpoint writes above have completed.
          try:
            os.remove(curr_env_file)
          except FileNotFoundError:
            pass

          # After this cycle, the world takes one step forward, and the 
          # current time moves by <sec_per_step> amount. 
          self.step += 1
          print(f"[STEPTIME] step {self.step}: perceive={_t1-_t0:.2f}s "
                f"decide={_time.time()-_t1:.2f}s "
                f"total={_time.time()-_t0:.2f}s", flush=True)
          # Keep the frontend's step file fresh every step so the
          # simulator_home page can be (re)loaded at any time.
          curr_step = dict()
          curr_step["step"] = self.step
          atomic_json_dump(curr_step, f"{fs_temp_storage}/curr_step.json")
          # Apply the current pacing only to time elapsed since the previous
          # sample. Rate changes can therefore neither rewind nor jump time.
          self.curr_time, _last_clock_real = _advance_clock(
            self.curr_time, _last_clock_real, time.monotonic(),
            _current_pacing())

          int_counter -= 1
          
      else:
        # Autonomous advance: upstream waits for the browser's
        # process_environment POST to supply the next env file, so the
        # town freezes with no viewer open. When the env file is missing
        # and the browser isn't driving (nothing posted it within a
        # couple of seconds), the backend writes it itself from the last
        # movement targets and the simulation keeps living.
        if self.step > 0:
          _src_mv = f"{sim_folder}/movement/{self.step - 1}.json"
          if check_if_file_exists(_src_mv):
            with open(_src_mv) as _jf:
              _mv_data = json.load(_jf)
            _env = {}
            for _pname, _m in _mv_data.get("persona", {}).items():
              _t = _m.get("movement") or [0, 0]
              _env[_pname] = {"x": _t[0], "y": _t[1]}
            atomic_json_dump(_env, curr_env_file)
            continue

      # Sleep so we don't burn our machines. 
      time.sleep(self.server_sleep)


  def open_server(self, run_steps=None): 
    """
    Open up an interactive terminal prompt that lets you run the simulation 
    step by step and probe agent state. 

    INPUT 
      None
    OUTPUT
      None
    """
    print ("Note: The agents in this simulation package are computational")
    print ("constructs powered by generative agents architecture and LLM. We")
    print ("clarify that these agents lack human-like agency, consciousness,")
    print ("and independent decision-making.\n---")

    # <sim_folder> points to the current simulation folder.
    sim_folder = f"{fs_storage}/{self.sim_code}"

    # When a run length is provided on the command line (headless/unit
    # mode), execute it directly and exit -- systemd restarts the service
    # to continue. Interactive mode is unchanged when run_steps is None.
    if run_steps is not None:
      print (f"Auto-running {run_steps} steps.")
      self.start_server(run_steps)
      print ("Run finished.")
      return

    while True: 
      sim_command = input("Enter option: ")
      sim_command = sim_command.strip()
      ret_str = ""

      try: 
        if sim_command.lower() in ["f", "fin", "finish", "save and finish"]: 
          # Finishes the simulation environment and saves the progress. 
          # Example: fin
          self.save()
          break

        elif sim_command.lower() == "start path tester mode": 
          # Starts the path tester and removes the currently forked sim files.
          # Note that once you start this mode, you need to exit out of the
          # session and restart in case you want to run something else. 
          shutil.rmtree(sim_folder) 
          self.start_path_tester_server()

        elif sim_command.lower() == "exit": 
          # Finishes the simulation environment but does not save the progress
          # and erases all saved data from current simulation. 
          # Example: exit 
          shutil.rmtree(sim_folder) 
          break 

        elif sim_command.lower() == "save": 
          # Saves the current simulation progress. 
          # Example: save
          self.save()

        elif sim_command[:3].lower() == "run": 
          # Runs the number of steps specified in the prompt.
          # Example: run 1000
          int_count = int(sim_command.split()[-1])
          rs.start_server(int_count)

        elif ("print persona schedule" 
              in sim_command[:22].lower()): 
          # Print the decomposed schedule of the persona specified in the 
          # prompt.
          # Example: print persona schedule Isabella Rodriguez
          ret_str += (self.personas[" ".join(sim_command.split()[-2:])]
                      .scratch.get_str_daily_schedule_summary())

        elif ("print all persona schedule" 
              in sim_command[:26].lower()): 
          # Print the decomposed schedule of all personas in the world. 
          # Example: print all persona schedule
          for persona_name, persona in self.personas.items(): 
            ret_str += f"{persona_name}\n"
            ret_str += f"{persona.scratch.get_str_daily_schedule_summary()}\n"
            ret_str += f"---\n"

        elif ("print hourly org persona schedule" 
              in sim_command.lower()): 
          # Print the hourly schedule of the persona specified in the prompt.
          # This one shows the original, non-decomposed version of the 
          # schedule.
          # Ex: print persona schedule Isabella Rodriguez
          ret_str += (self.personas[" ".join(sim_command.split()[-2:])]
                      .scratch.get_str_daily_schedule_hourly_org_summary())

        elif ("print persona current tile" 
              in sim_command[:26].lower()): 
          # Print the x y tile coordinate of the persona specified in the 
          # prompt. 
          # Ex: print persona current tile Isabella Rodriguez
          ret_str += str(self.personas[" ".join(sim_command.split()[-2:])]
                      .scratch.curr_tile)

        elif ("print persona chatting with buffer" 
              in sim_command.lower()): 
          # Print the chatting with buffer of the persona specified in the 
          # prompt.
          # Ex: print persona chatting with buffer Isabella Rodriguez
          curr_persona = self.personas[" ".join(sim_command.split()[-2:])]
          for p_n, count in curr_persona.scratch.chatting_with_buffer.items(): 
            ret_str += f"{p_n}: {count}"

        elif ("print persona associative memory (event)" 
              in sim_command.lower()):
          # Print the associative memory (event) of the persona specified in
          # the prompt
          # Ex: print persona associative memory (event) Isabella Rodriguez
          ret_str += f'{self.personas[" ".join(sim_command.split()[-2:])]}\n'
          ret_str += (self.personas[" ".join(sim_command.split()[-2:])]
                                       .a_mem.get_str_seq_events())

        elif ("print persona associative memory (thought)" 
              in sim_command.lower()): 
          # Print the associative memory (thought) of the persona specified in
          # the prompt
          # Ex: print persona associative memory (thought) Isabella Rodriguez
          ret_str += f'{self.personas[" ".join(sim_command.split()[-2:])]}\n'
          ret_str += (self.personas[" ".join(sim_command.split()[-2:])]
                                       .a_mem.get_str_seq_thoughts())

        elif ("print persona associative memory (chat)" 
              in sim_command.lower()): 
          # Print the associative memory (chat) of the persona specified in
          # the prompt
          # Ex: print persona associative memory (chat) Isabella Rodriguez
          ret_str += f'{self.personas[" ".join(sim_command.split()[-2:])]}\n'
          ret_str += (self.personas[" ".join(sim_command.split()[-2:])]
                                       .a_mem.get_str_seq_chats())

        elif ("print persona spatial memory" 
              in sim_command.lower()): 
          # Print the spatial memory of the persona specified in the prompt
          # Ex: print persona spatial memory Isabella Rodriguez
          self.personas[" ".join(sim_command.split()[-2:])].s_mem.print_tree()

        elif ("print current time" 
              in sim_command[:18].lower()): 
          # Print the current time of the world. 
          # Ex: print current time
          ret_str += f'{self.curr_time.strftime("%B %d, %Y, %H:%M:%S")}\n'
          ret_str += f'steps: {self.step}'

        elif ("print tile event" 
              in sim_command[:16].lower()): 
          # Print the tile events in the tile specified in the prompt 
          # Ex: print tile event 50, 30
          cooordinate = [int(i.strip()) for i in sim_command[16:].split(",")]
          for i in self.maze.access_tile(cooordinate)["events"]: 
            ret_str += f"{i}\n"

        elif ("print tile details" 
              in sim_command.lower()): 
          # Print the tile details of the tile specified in the prompt 
          # Ex: print tile event 50, 30
          cooordinate = [int(i.strip()) for i in sim_command[18:].split(",")]
          for key, val in self.maze.access_tile(cooordinate).items(): 
            ret_str += f"{key}: {val}\n"

        elif ("call -- analysis" 
              in sim_command.lower()): 
          # Starts a stateless chat session with the agent. It does not save 
          # anything to the agent's memory. 
          # Ex: call -- analysis Isabella Rodriguez
          persona_name = sim_command[len("call -- analysis"):].strip() 
          self.personas[persona_name].open_convo_session("analysis")

        elif ("call -- load history" 
              in sim_command.lower()): 
          curr_file = maze_assets_loc + "/" + sim_command[len("call -- load history"):].strip() 
          # call -- load history the_ville/agent_history_init_n3.csv

          rows = read_file_to_list(curr_file, header=True, strip_trail=True)[1]
          clean_whispers = []
          for row in rows: 
            agent_name = row[0].strip() 
            whispers = row[1].split(";")
            whispers = [whisper.strip() for whisper in whispers]
            for whisper in whispers: 
              clean_whispers += [[agent_name, whisper]]

          load_history_via_whisper(self.personas, clean_whispers)

        print (ret_str)

      except:
        traceback.print_exc()
        print ("Error.")
        pass


if __name__ == '__main__':
  # Headless mode: python reverie.py <fork_sim> <sim_name> [run_steps]
  # lets the unit/systemd drive the simulation without piped stdin.
  if len(sys.argv) >= 3:
    origin = sys.argv[1]
    target = sys.argv[2]
    run_steps = int(sys.argv[3]) if len(sys.argv) > 3 else None
  else:
    origin = input("Enter the name of the forked simulation: ").strip()
    target = input("Enter the name of the new simulation: ").strip()
    run_steps = None

  rs = ReverieServer(origin, target)
  rs.open_server(run_steps)







































