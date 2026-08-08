"""
Author: Joon Sung Park (joonspk@stanford.edu)

File: spatial_memory.py
Description: Defines the MemoryTree class that serves as the agents' spatial
memory that aids in grounding their behavior in the game world. 
"""
import json
import sys
sys.path.append('../../')

from utils import *
from global_methods import *

class MemoryTree: 
  def __init__(self, f_saved): 
    self.tree = {}
    if check_if_file_exists(f_saved): 
      self.tree = json.load(open(f_saved))


  @staticmethod
  def _resolve_key(parent, wanted):
    """Tolerant key lookup: exact, then case-insensitive, then containment.

    Modern LLMs occasionally drift from the exact maze names (stray braces,
    extra words, case changes), so lookups fall back to the best matching
    key instead of crashing the simulation.
    """
    if not isinstance(parent, dict) or not wanted:
      return None
    if wanted in parent:
      return wanted
    w = wanted.strip().lower()
    for k in parent:
      if k.lower() == w:
        return k
    for k in parent:
      if w and (w in k.lower() or k.lower() in w):
        return k
    return None


  def print_tree(self): 
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
    
    _print_tree(self.tree, 0)
    

  def save(self, out_json):
    with open(out_json, "w") as outfile:
      json.dump(self.tree, outfile) 



  def get_str_accessible_sectors(self, curr_world): 
    """
    Returns a summary string of all the arenas that the persona can access 
    within the current sector. 

    Note that there are places a given persona cannot enter. This information
    is provided in the persona sheet. We account for this in this function. 

    INPUT
      None
    OUTPUT 
      A summary string of all the arenas that the persona can access. 
    EXAMPLE STR OUTPUT
      "bedroom, kitchen, dining room, office, bathroom"
    """
    x = ", ".join(list(self.tree[curr_world].keys()))
    return x


  def get_str_accessible_sector_arenas(self, sector): 
    """
    Returns a summary string of all the arenas that the persona can access 
    within the current sector. 

    Note that there are places a given persona cannot enter. This information
    is provided in the persona sheet. We account for this in this function. 

    INPUT
      None
    OUTPUT 
      A summary string of all the arenas that the persona can access. 
    EXAMPLE STR OUTPUT
      "bedroom, kitchen, dining room, office, bathroom"
    """
    curr_world, curr_sector = sector.split(":")
    if not curr_sector: 
      return ""
    world_key = self._resolve_key(self.tree, curr_world)
    if not world_key:
      return ""
    sector_key = self._resolve_key(self.tree[world_key], curr_sector)
    if not sector_key:
      return ""
    x = ", ".join(list(self.tree[world_key][sector_key].keys()))
    return x


  def get_str_accessible_arena_game_objects(self, arena):
    """
    Get a str list of all accessible game objects that are in the arena. If 
    temp_address is specified, we return the objects that are available in
    that arena, and if not, we return the objects that are in the arena our
    persona is currently in. 

    INPUT
      temp_address: optional arena address
    OUTPUT 
      str list of all accessible game objects in the gmae arena. 
    EXAMPLE STR OUTPUT
      "phone, charger, bed, nightstand"
    """
    x = arena.split(":")
    curr_world = x[0].strip() if len(x) > 0 else ""
    curr_sector = x[1].strip() if len(x) > 1 else ""
    curr_arena = x[2].strip() if len(x) > 2 else ""

    if not curr_arena: 
      return ""

    world_key = self._resolve_key(self.tree, curr_world)
    if not world_key:
      return ""
    sector_key = self._resolve_key(self.tree[world_key], curr_sector)
    if not sector_key:
      return ""
    arena_key = self._resolve_key(self.tree[world_key][sector_key], curr_arena)
    if not arena_key:
      return ""

    return ", ".join(list(self.tree[world_key][sector_key][arena_key]))


if __name__ == '__main__':
  x = f"../../../../environment/frontend_server/storage/the_ville_base_LinFamily/personas/Eddy Lin/bootstrap_memory/spatial_memory.json"
  x = MemoryTree(x)
  x.print_tree()

  print (x.get_str_accessible_sector_arenas("dolores double studio:double studio"))







