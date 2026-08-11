"""
Author: Joon Sung Park (joonspk@stanford.edu)

File: persona.py
Description: Defines the Persona class that powers the agents in Reverie. 

Note (May 1, 2023) -- this is effectively GenerativeAgent class. Persona was
the term we used internally back in 2022, taking from our Social Simulacra 
paper.
"""
import math
import sys
import datetime
import json
import os
import random
import shutil
import tempfile
import time
sys.path.append('../')

from global_methods import *

from persona.memory_structures.spatial_memory import *
from persona.memory_structures.associative_memory import *
from persona.memory_structures.scratch import *

from persona.cognitive_modules.perceive import *
from persona.cognitive_modules.retrieve import *
from persona.cognitive_modules.plan import *
from persona.cognitive_modules.reflect import *
from persona.cognitive_modules.execute import *
from persona.cognitive_modules.converse import *


def _checkpoint_valid(directory):
  try:
    with open(os.path.join(directory, "associative_memory", "nodes.json")) as f:
      nodes = json.load(f)
    with open(os.path.join(directory, "associative_memory", "embeddings.json")) as f:
      embeddings = json.load(f)
    with open(os.path.join(directory, "associative_memory", "kw_strength.json")) as f:
      json.load(f)
    with open(os.path.join(directory, "scratch.json")) as f:
      json.load(f)
    with open(os.path.join(directory, "spatial_memory.json")) as f:
      json.load(f)
    return all(node.get("embedding_key") in embeddings
               for node in nodes.values())
  except (OSError, ValueError, TypeError, AttributeError):
    return False


def _resolve_checkpoint(bootstrap):
  manifest_path = os.path.join(bootstrap, "checkpoint.json")
  try:
    with open(manifest_path) as f:
      manifest = json.load(f)
    for generation in (manifest.get("current"), manifest.get("previous")):
      if not generation:
        continue
      candidate = os.path.join(bootstrap, "checkpoints", generation)
      if _checkpoint_valid(candidate):
        return candidate
  except (OSError, ValueError, TypeError):
    pass
  return bootstrap


def _fsync_tree(directory):
  for root, _, files in os.walk(directory):
    for filename in files:
      with open(os.path.join(root, filename), "rb") as checkpoint_file:
        os.fsync(checkpoint_file.fileno())
    descriptor = os.open(root, os.O_RDONLY)
    try:
      os.fsync(descriptor)
    finally:
      os.close(descriptor)

class Persona: 
  def __init__(self, name, folder_mem_saved=False):
    # PERSONA BASE STATE 
    # <name> is the full name of the persona. This is a unique identifier for
    # the persona within Reverie. 
    self.name = name

    # PERSONA MEMORY 
    # If there is already memory in folder_mem_saved, we load that. Otherwise,
    # we create new memory instances. 
    # <s_mem> is the persona's spatial memory. 
    bootstrap = f"{folder_mem_saved}/bootstrap_memory"
    checkpoint = _resolve_checkpoint(bootstrap)
    f_s_mem_saved = f"{checkpoint}/spatial_memory.json"
    self.s_mem = MemoryTree(f_s_mem_saved)
    # <s_mem> is the persona's associative memory. 
    f_a_mem_saved = f"{checkpoint}/associative_memory"
    self.a_mem = AssociativeMemory(f_a_mem_saved)
    # <scratch> is the persona's scratch (short term memory) space. 
    scratch_saved = f"{checkpoint}/scratch.json"
    self.scratch = Scratch(scratch_saved)


  def save(self, save_folder, full=True): 
    """
    Save persona's current state (i.e., memory). 

    INPUT: 
      save_folder: The folder where we wil be saving our persona's state. 
      full: When False, only scratch.json is written (cheap). Full memory
            (spatial + associative incl. embeddings.json) is written only
            when full=True -- the caller throttles this so a 25-resident
            town doesn't rewrite ~7MB of embeddings JSON every step.
    OUTPUT: 
      None
    """
    f_scratch = f"{save_folder}/scratch.json"
    self.scratch.save(f_scratch)

    if not full:
      return

    checkpoints = os.path.join(save_folder, "checkpoints")
    os.makedirs(checkpoints, exist_ok=True)
    pending = tempfile.mkdtemp(prefix=".pending-", dir=checkpoints)
    try:
      os.makedirs(os.path.join(pending, "associative_memory"))
      self.scratch.save(os.path.join(pending, "scratch.json"))
      self.s_mem.save(os.path.join(pending, "spatial_memory.json"))
      self.a_mem.save(os.path.join(pending, "associative_memory"))
      if not _checkpoint_valid(pending):
        raise ValueError("persona checkpoint failed validation")
      _fsync_tree(pending)
      generation = "generation-%d" % time.time_ns()
      completed = os.path.join(checkpoints, generation)
      os.replace(pending, completed)
      directory_fd = os.open(checkpoints, os.O_RDONLY)
      try:
        os.fsync(directory_fd)
      finally:
        os.close(directory_fd)

      manifest_path = os.path.join(save_folder, "checkpoint.json")
      previous = None
      try:
        with open(manifest_path) as manifest_file:
          previous = json.load(manifest_file).get("current")
      except (OSError, ValueError, TypeError):
        pass
      atomic_json_dump({"current": generation, "previous": previous},
                       manifest_path)
      directory_fd = os.open(save_folder, os.O_RDONLY)
      try:
        os.fsync(directory_fd)
      finally:
        os.close(directory_fd)
      retained = {generation, previous}
      for entry in os.listdir(checkpoints):
        if entry.startswith("generation-") and entry not in retained:
          shutil.rmtree(os.path.join(checkpoints, entry), ignore_errors=True)
    except Exception:
      shutil.rmtree(pending, ignore_errors=True)
      raise


  def perceive(self, maze):
    """
    Perceive events around the persona (collect + commit). Kept for
    compatibility with the upstream public API; the step loop uses the
    split _move_perceive/_move_decide so LLM work stays parallel.
    """
    pending = perceive_collect(self, maze)
    return perceive_commit(self, pending)


  def retrieve(self, perceived):
    """
    This function takes the events that are perceived by the persona as input
    and returns a set of related events and thoughts that the persona would 
    need to consider as context when planning. 

    INPUT: 
      perceive: a list of <ConceptNode> that are perceived and new.  
    OUTPUT: 
      retrieved: dictionary of dictionary. The first layer specifies an event,
                 while the latter layer specifies the "curr_event", "events", 
                 and "thoughts" that are relevant.
    """
    return retrieve(self, perceived)


  def plan(self, maze, personas, new_day, retrieved, enable_reactions=True,
           sim_code=None, step=None):
    """
    Main cognitive function of the chain. It takes the retrieved memory and 
    perception, as well as the maze and the first day state to conduct both 
    the long term and short term planning for the persona. 

    INPUT: 
      maze: Current <Maze> instance of the world. 
      personas: A dictionary that contains all persona names as keys, and the 
                Persona instance as values. 
      new_day: This can take one of the three values. 
        1) <Boolean> False -- It is not a "new day" cycle (if it is, we would
           need to call the long term planning sequence for the persona). 
        2) <String> "First day" -- It is literally the start of a simulation,
           so not only is it a new day, but also it is the first day. 
        2) <String> "New day" -- It is a new day. 
      retrieved: dictionary of dictionary. The first layer specifies an event,
                 while the latter layer specifies the "curr_event", "events", 
                 and "thoughts" that are relevant.
    OUTPUT 
      The target action address of the persona (persona.scratch.act_address).
    """
    return plan(self, maze, personas, new_day, retrieved, enable_reactions,
                sim_code=sim_code, step=step)


  def execute(self, maze, personas, plan):
    """
    This function takes the agent's current plan and outputs a concrete 
    execution (what object to use, and what tile to travel to). 

    INPUT: 
      maze: Current <Maze> instance of the world. 
      personas: A dictionary that contains all persona names as keys, and the 
                Persona instance as values. 
      plan: The target action address of the persona  
            (persona.scratch.act_address).
    OUTPUT: 
      execution: A triple set that contains the following components: 
        <next_tile> is a x,y coordinate. e.g., (58, 9)
        <pronunciatio> is an emoji.
        <description> is a string description of the movement. e.g., 
        writing her next novel (editing her novel) 
        @ double studio:double studio:common room:sofa
    """
    return execute(self, maze, personas, plan)


  def reflect(self):
    """
    Reviews the persona's memory and create new thoughts based on it. 

    INPUT: 
      None
    OUTPUT: 
      None
    """
    reflect(self)


  def move(self, maze, personas, curr_tile, curr_time, sim_code=None, step=None):
    """
    This is the main cognitive function where our main sequence is called. 

    INPUT: 
      maze: The Maze class of the current world. 
      personas: A dictionary that contains all persona names as keys, and the 
                Persona instance as values. 
      curr_tile: A tuple that designates the persona's current tile location 
                 in (row, col) form. e.g., (58, 39)
      curr_time: datetime instance that indicates the game's current time. 
    OUTPUT: 
      execution: A triple set that contains the following components: 
        <next_tile> is a x,y coordinate. e.g., (58, 9)
        <pronunciatio> is an emoji.
        <description> is a string description of the movement. e.g., 
        writing her next novel (editing her novel) 
        @ double studio:double studio:common room:sofa
    """
    # Updating persona's scratch memory with <curr_tile>. 
    self.scratch.curr_tile = curr_tile

    # We figure out whether the persona started a new day, and if it is a new
    # day, whether it is the very first day of the simulation. This is 
    # important because we set up the persona's long term plan at the start of
    # a new day. 
    new_day = False
    if not self.scratch.curr_time: 
      new_day = "First day"
    elif (self.scratch.curr_time.strftime('%A %B %d')
          != curr_time.strftime('%A %B %d')):
      new_day = "New day"
    self.scratch.curr_time = curr_time

    # Main cognitive sequence begins here. Phase 1 (perceive) stays
    # sequential in the caller so co-located personas' chats can't race;
    # phase 2 (retrieve/plan/reflect/execute) is safe to parallelize.
    new_day, perceived = self._move_perceive(maze, curr_tile, curr_time)
    return self._move_decide(maze, personas, new_day, perceived,
                             sim_code=sim_code, step=step)

  def _move_perceive(self, maze, curr_tile, curr_time):
    """Phase 1 of move(): tile bookkeeping + perception COLLECTION.

    No LLM calls here -- poignancy scoring + memory commits are deferred
    to _move_decide (via perceive_commit) so they run concurrently in the
    parallel phase instead of serializing the whole town on chat-heavy
    steps. Returns (new_day, pending_percepts)."""
    self.scratch.curr_tile = curr_tile
    new_day = False
    if not self.scratch.curr_time:
      new_day = "First day"
    elif (self.scratch.curr_time.strftime('%A %B %d')
          != curr_time.strftime('%A %B %d')):
      new_day = "New day"
    self.scratch.curr_time = curr_time
    pending = perceive_collect(self, maze)
    return new_day, pending

  def _move_decide(self, maze, personas, new_day, percept_batch,
                   defer_reactions=False, sim_code=None, step=None):
    """Phase 2 of move(): perceive commit (poignancy + memory) + retrieve +
    plan + reflect + execute. All work is persona-private (own memory, LLM,
    embeddings) -- safe to run concurrently for all personas in a step."""
    perceived = perceive_commit(self, percept_batch)
    retrieved = self.retrieve(perceived)
    plan = self.plan(maze, personas, new_day, retrieved,
                     enable_reactions=not defer_reactions,
                     sim_code=sim_code, step=step)
    self.reflect()
    if defer_reactions:
      focused_event = False
      if retrieved.keys():
        focused_event = _choose_retrieved(self, retrieved)
      return focused_event
    return self.execute(maze, personas, plan)


  def _move_execute(self, maze, personas):
    """Execute after the step coordinator has committed reactions."""
    return self.execute(maze, personas, self.scratch.act_address)


  def open_convo_session(self, convo_mode): 
    open_convo_session(self, convo_mode)
    































