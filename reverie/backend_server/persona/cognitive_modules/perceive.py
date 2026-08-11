"""
Author: Joon Sung Park (joonspk@stanford.edu)

File: perceive.py
Description: This defines the "Perceive" module for generative agents. 
"""
import sys
sys.path.append('../../')

from operator import itemgetter
from global_methods import *
from persona.prompt_template.gpt_structure import *
from persona.prompt_template.run_gpt_prompt import *

def generate_poig_score(persona, event_type, description): 
  if "is idle" in description: 
    return 1

  if event_type == "event": 
    return run_gpt_prompt_event_poignancy(persona, description)[0]
  elif event_type == "chat": 
    return run_gpt_prompt_chat_poignancy(persona, 
                           persona.scratch.act_description)[0]

def perceive_collect(persona, maze): 
  """
  Phase 1 of perceive: spatial memory update + event collection, WITHOUT
  any LLM calls. The expensive poignancy scoring and memory commits happen
  in perceive_commit(), which runs inside the parallel decide phase -- so a
  chat-heavy step no longer serializes the whole town on LLM latency.

  Returns a list of pending percept specs (dicts). Persona-private reads
  only (own a_mem/s_mem/scratch); safe in any order.
  """
  # PERCEIVE SPACE
  # We get the nearby tiles given our current tile and the persona's vision
  # radius. 
  nearby_tiles = maze.get_nearby_tiles(persona.scratch.curr_tile, 
                                       persona.scratch.vision_r)

  # We then store the perceived space. Note that the s_mem of the persona is
  # in the form of a tree constructed using dictionaries. 
  for i in nearby_tiles: 
    i = maze.access_tile(i)
    if i["world"]: 
      if (i["world"] not in persona.s_mem.tree): 
        persona.s_mem.tree[i["world"]] = {}
    if i["sector"]: 
      if (i["sector"] not in persona.s_mem.tree[i["world"]]): 
        persona.s_mem.tree[i["world"]][i["sector"]] = {}
    if i["arena"]: 
      if (i["arena"] not in persona.s_mem.tree[i["world"]]
                                              [i["sector"]]): 
        persona.s_mem.tree[i["world"]][i["sector"]][i["arena"]] = []
    if i["game_object"]: 
      if (i["game_object"] not in persona.s_mem.tree[i["world"]]
                                                    [i["sector"]]
                                                    [i["arena"]]): 
        persona.s_mem.tree[i["world"]][i["sector"]][i["arena"]] += [
                                                             i["game_object"]]

  # PERCEIVE EVENTS. 
  # We will perceive events that take place in the same arena as the
  # persona's current arena. 
  curr_arena_path = maze.get_tile_path(persona.scratch.curr_tile, "arena")
  # We do not perceive the same event twice (this can happen if an object is
  # extended across multiple tiles).
  percept_events_set = set()
  # We will order our percept based on the distance, with the closest ones
  # getting priorities. 
  percept_events_list = []
  # First, we put all events that are occuring in the nearby tiles into the
  # percept_events_list
  for tile in nearby_tiles: 
    tile_details = maze.access_tile(tile)
    if tile_details["events"]: 
      if maze.get_tile_path(tile, "arena") == curr_arena_path:  
        # This calculates the distance between the persona's current tile, 
        # and the target tile.
        dist = math.dist([tile[0], tile[1]], 
                         [persona.scratch.curr_tile[0], 
                          persona.scratch.curr_tile[1]])
        # Add any relevant events to our temp set/list with the distant info. 
        for event in tile_details["events"]: 
          if event not in percept_events_set: 
            percept_events_list += [[dist, event]]
            percept_events_set.add(event)

  # We sort, and perceive only persona.scratch.att_bandwidth of the closest
  # events. If the bandwidth is larger, then it means the persona can perceive
  # more elements within a small area. 
  percept_events_list = sorted(percept_events_list, key=itemgetter(0))
  perceived_events = []
  for dist, event in percept_events_list[:persona.scratch.att_bandwidth]: 
    perceived_events += [event]

  # Collect pending percepts without remote I/O. Embeddings and poignancy are
  # deferred to the bounded parallel commit phase.
  pending = []
  for p_event in perceived_events: 
    if not isinstance(p_event, (tuple, list)) or len(p_event) != 4:
      continue
    s, p, o, desc = p_event
    if not all(str(x).strip() for x in (s, p, o)):
      continue
    if not p: 
      # If the object is not present, then we default the event to "idle".
      p = "is"
      o = "idle"
      desc = "idle"
    desc = f"{s.split(':')[-1]} is {desc}"
    p_event = (s, p, o)

    # We retrieve the latest persona.scratch.retention events. If there is 
    # something new that is happening (that is, p_event not in latest_events),
    # then we add that event to the a_mem and return it. 
    latest_events = persona.a_mem.get_summarized_latest_events(
                                    persona.scratch.retention)
    if p_event not in latest_events:
      # We start by managing keywords. 
      keywords = set()
      sub = p_event[0]
      obj = p_event[2]
      if ":" in p_event[0]: 
        sub = p_event[0].split(":")[-1]
      if ":" in p_event[2]: 
        obj = p_event[2].split(":")[-1]
      keywords.update([sub, obj])

      # Get event embedding
      desc_embedding_in = desc
      if "(" in desc and ")" in desc:
        desc_embedding_in = desc_embedding_in.split("(", 1)[1].split(")", 1)[0].strip()
      if desc_embedding_in in persona.a_mem.embeddings: 
        event_embedding = persona.a_mem.embeddings[desc_embedding_in]
      else: 
        event_embedding = None
      spec = {
        "kind": "event",
        "s": s, "p": p, "o": o, "desc": desc,
        "desc_embedding_in": desc_embedding_in,
        "embedding_pair": (desc_embedding_in, event_embedding),
        "keywords": keywords,
        "chat": None,
      }

      # If we observe the persona's self chat, we include that in the memory
      # of the persona here (deferred: poignancy LLM call happens in commit).
      if p_event[0] == f"{persona.name}" and p_event[1] == "chat with": 
        curr_event = persona.scratch.act_event
        if persona.scratch.act_description in persona.a_mem.embeddings: 
          chat_embedding = persona.a_mem.embeddings[
                             persona.scratch.act_description]
        else: 
          chat_embedding = None
        spec["chat"] = {
          "curr_event": curr_event,
          "chat_embedding_pair": (persona.scratch.act_description,
                                  chat_embedding),
          "chat": persona.scratch.chat,
        }
      pending.append(spec)

  return pending


def perceive_commit(persona, pending): 
  """
  Phase 2 of perceive: poignancy scoring (LLM) + memory commits.

  Safe to run concurrently per persona -- it only touches the persona's own
  a_mem/scratch. Returns the list of new <ConceptNode> instances (the
  perceived events), matching the original perceive() return contract.
  """
  ret_events = []
  for spec in pending: 
    if spec["embedding_pair"][1] is None:
      embedding = get_embedding(spec["embedding_pair"][0],
                                defer_on_error=True)
      if embedding is None:
        continue
      spec["embedding_pair"] = (spec["embedding_pair"][0], embedding)
    if (spec.get("chat")
        and spec["chat"]["chat_embedding_pair"][1] is None):
      chat_embedding = get_embedding(
        spec["chat"]["chat_embedding_pair"][0], defer_on_error=True)
      if chat_embedding is None:
        spec["chat"] = None
      else:
        spec["chat"]["chat_embedding_pair"] = (
          spec["chat"]["chat_embedding_pair"][0], chat_embedding)
    # Get event poignancy. 
    event_poignancy = generate_poig_score(persona, 
                                          "event", 
                                          spec["desc_embedding_in"])

    chat_node_ids = []
    if spec.get("chat"): 
      chat_poignancy = generate_poig_score(persona, "chat", 
                                           spec["chat"]["chat_embedding_pair"][0])
      chat_node = persona.a_mem.add_chat(persona.scratch.curr_time, None,
                    spec["chat"]["curr_event"][0],
                    spec["chat"]["curr_event"][1],
                    spec["chat"]["curr_event"][2],
                    spec["chat"]["chat_embedding_pair"][0],
                    spec["keywords"], chat_poignancy,
                    spec["chat"]["chat_embedding_pair"],
                    spec["chat"]["chat"])
      chat_node_ids = [chat_node.node_id]

    # Finally, we add the current event to the agent's memory. 
    ret_events += [persona.a_mem.add_event(persona.scratch.curr_time, None,
                         spec["s"], spec["p"], spec["o"], spec["desc"],
                         spec["keywords"], event_poignancy,
                         spec["embedding_pair"], chat_node_ids)]

    persona.scratch.importance_trigger_curr -= event_poignancy
    persona.scratch.importance_ele_n += 1

  return ret_events




  









