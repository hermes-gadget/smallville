"""
Author: Joon Sung Park (joonspk@stanford.edu)

File: gpt_structure.py
Description: Wrapper functions for calling LLM APIs.

Modernized (hermes-gadget fork): all LLM traffic now goes through the
OpenCode Go gateway (OpenAI-compatible chat completions at
https://opencode.ai/zen/go/v1) with deepseek-v4-flash as the default model.
Embeddings are served by the local LM Studio box (192.168.2.4). No legacy
OpenAI SDK dependency is required -- this module uses `requests` only.

Function signatures are kept identical to the upstream file so the persona
cognitive modules work unchanged.
"""
import json
import os
import random
import sqlite3
import sys
import tempfile
import threading
import time

import requests

from utils import *

_SESSION = requests.Session()

# ---------------------------------------------------------------------------
# Token usage telemetry
# ---------------------------------------------------------------------------
# Every successful LLM/embedding call is recorded here and written to
# token_usage_file (JSON) so the Django frontend can display live usage on
# the public page. The counter resets on process start (module import).
_usage_lock = threading.Lock()
_usage = {
    "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    "updated_at": "-",
    "total_tokens": 0,
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "calls": 0,
    "by_model": {},
    "embedding_calls": 0,
    "embedding_tokens": 0,
}


def _record_usage(usage, model, embedding=False):
  """Add one API response's usage to the counter, persist the snapshot and
  append the call to the cumulative SQLite store (queryable)."""
  with _usage_lock:
    if embedding:
      _usage["embedding_calls"] += 1
      _usage["embedding_tokens"] += int(usage.get("prompt_tokens") or 0)
    else:
      prompt = int(usage.get("prompt_tokens") or 0)
      completion = int(usage.get("completion_tokens") or 0)
      _usage["prompt_tokens"] += prompt
      _usage["completion_tokens"] += completion
      _usage["calls"] += 1
      _usage["by_model"][model] = (
        _usage["by_model"].get(model, 0) + prompt + completion)
    _usage["total_tokens"] = (
      _usage["prompt_tokens"] + _usage["completion_tokens"]
      + _usage["embedding_tokens"])
    _usage["updated_at"] = time.strftime("%H:%M:%S")
    _write_usage_snapshot()
    _log_usage_row(model, usage, embedding)


def _log_usage_row(model, usage, embedding):
  """Append one call to the cumulative SQLite log (never breaks the sim)."""
  try:
    path = os.path.abspath(token_usage_db)
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5)
    try:
      conn.execute(
        "CREATE TABLE IF NOT EXISTS calls ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "ts TEXT NOT NULL,"
        "model TEXT NOT NULL,"
        "kind TEXT NOT NULL,"
        "prompt_tokens INTEGER NOT NULL,"
        "completion_tokens INTEGER NOT NULL,"
        "total_tokens INTEGER NOT NULL)")
      conn.execute(
        "INSERT INTO calls (ts, model, kind, prompt_tokens, completion_tokens, total_tokens)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (time.strftime("%Y-%m-%d %H:%M:%S"), model,
         "embedding" if embedding else "llm",
         int(usage.get("prompt_tokens") or 0),
         int(usage.get("completion_tokens") or 0),
         int(usage.get("total_tokens")
             or (usage.get("prompt_tokens") or 0)
             + (usage.get("completion_tokens") or 0))))
      conn.commit()
    finally:
      conn.close()
  except Exception as e:  # telemetry must never break the simulation
    print(f"[usage telemetry] db: {e}", file=sys.stderr)


def _write_usage_snapshot():
  """Atomically write the usage snapshot for the Django frontend to read."""
  try:
    path = token_usage_file
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    with os.fdopen(fd, "w") as f:
      json.dump(_usage, f)
    os.replace(tmp, os.path.abspath(path))
  except Exception as e:  # telemetry must never break the simulation
    print(f"[usage telemetry] {e}", file=sys.stderr)


# Initialize the on-page monitor with a zeroed snapshot at process start.
_write_usage_snapshot()


def temp_sleep(seconds=0.1):
  time.sleep(seconds)


def _llm_chat(messages,
              model=None,
              max_tokens=None,
              temperature=1.0,
              top_p=1.0,
              frequency_penalty=0.0,
              presence_penalty=0.0,
              stop=None,
              retries=4):
  """POST a chat-completions request to the OpenCode Go gateway.

  Returns the assistant message content. Raises RuntimeError when the
  gateway is unreachable, returns an error status, or returns an empty
  completion (thinking models can burn their whole token budget on
  reasoning_content -- retries give them another chance).
  """
  payload = {
    "model": model or llm_model,
    "messages": messages,
    "temperature": temperature,
    "max_tokens": max_tokens or llm_max_tokens,
    "top_p": top_p,
    "frequency_penalty": frequency_penalty,
    "presence_penalty": presence_penalty,
  }
  if not llm_thinking:
    payload["thinking"] = {"type": "disabled"}
  if stop:
    payload["stop"] = stop

  url = openai_base_url.rstrip("/") + "/chat/completions"
  headers = {
    "Authorization": f"Bearer {openai_api_key}",
    "Content-Type": "application/json",
  }

  # Hard wall-clock deadline per attempt. The gateway can trickle a
  # thinking response indefinitely (read timeouts reset per chunk), which
  # would hang the simulation forever. We run the request on a daemon
  # thread with its OWN session and abandon it after llm_hard_timeout
  # seconds. A shared session pool is fatal here: stuck trickle-streams
  # occupy every pooled connection and later attempts block on pool
  # acquire forever. Fresh session per attempt = leaked sockets die with
  # the gateway's keepalive, never the pool.
  for attempt in range(retries):
    box = {}

    def _post():
      try:
        _s = requests.Session()
        box["resp"] = _s.post(url, json=payload, headers=headers,
                              timeout=60)
      except Exception as e:  # noqa: BLE001 - surface any failure to the caller
        box["err"] = e

    try:
      t = threading.Thread(target=_post, daemon=True)
      t.start()
      t.join(timeout=llm_hard_timeout)
      if "err" in box:
        raise box["err"]
      if "resp" not in box:
        raise RuntimeError(
          f"LLM call exceeded hard timeout ({llm_hard_timeout}s)")
      resp = box["resp"]
      if resp.status_code == 200:
        data = resp.json()
        _record_usage(data.get("usage") or {}, payload["model"])
        content = data["choices"][0]["message"].get("content") or ""
        if content.strip():
          return content
        raise RuntimeError("empty completion (reasoning ate the token budget)")
      if resp.status_code in (429, 500, 502, 503, 504):
        time.sleep(min(2 ** attempt, 30) + random.random())
        continue
      raise RuntimeError(f"LLM API {resp.status_code}: {resp.text[:300]}")
    except requests.RequestException as e:
      if attempt == retries - 1:
        raise RuntimeError(f"LLM request failed: {e}") from e
      time.sleep(min(2 ** attempt, 30) + random.random())
  raise RuntimeError("LLM request failed after retries")


# ============================================================================
# #####################[SECTION 1: CHATGPT-3 STRUCTURE] ######################
# ============================================================================

def GPT4_request(prompt):
  """Single-shot request (deepseek-v4-flash, thinking disabled)."""
  temp_sleep()
  try:
    return _llm_chat([{"role": "user", "content": prompt}],
                     model=llm_model)
  except Exception as e:
    print(f"[LLM ERROR] GPT4_request: {e}", file=sys.stderr)
    return "ChatGPT ERROR"


def ChatGPT_request(prompt):
  """Single-shot request on the default model (deepseek-v4-flash)."""
  temp_sleep()
  try:
    return _llm_chat([{"role": "user", "content": prompt}])
  except Exception as e:
    print(f"[LLM ERROR] ChatGPT_request: {e}", file=sys.stderr)
    return "ChatGPT ERROR"


def ChatGPT_safe_generate_response(prompt,
                                   example_output,
                                   special_instruction,
                                   repeat=3,
                                   fail_safe_response="error",
                                   func_validate=None,
                                   func_clean_up=None,
                                   verbose=False):
  # prompt = 'GPT-3 Prompt:\n"""\n' + prompt + '\n"""\n'
  prompt = '"""\n' + prompt + '\n"""\n'
  prompt += f"Output the response to the prompt above in json. {special_instruction}\n"
  prompt += "Example output json:\n"
  prompt += '{"output": "' + str(example_output) + '"}'

  if verbose:
    print("CHAT GPT PROMPT")
    print(prompt)

  for i in range(repeat):
    try:
      curr_gpt_response = ChatGPT_request(prompt).strip()
      end_index = curr_gpt_response.rfind('}') + 1
      curr_gpt_response = curr_gpt_response[:end_index]
      curr_gpt_response = json.loads(curr_gpt_response)["output"]

      if func_validate(curr_gpt_response, prompt=prompt):
        return func_clean_up(curr_gpt_response, prompt=prompt)

      if verbose:
        print("---- repeat count: \n", i, curr_gpt_response)
        print(curr_gpt_response)
        print("~~~~")

    except:
      pass

  return False


def GPT4_safe_generate_response(prompt,
                                example_output,
                                special_instruction,
                                repeat=3,
                                fail_safe_response="error",
                                func_validate=None,
                                func_clean_up=None,
                                verbose=False):
  prompt = 'GPT-3 Prompt:\n"""\n' + prompt + '\n"""\n'
  prompt += f"Output the response to the prompt above in json. {special_instruction}\n"
  prompt += "Example output json:\n"
  prompt += '{"output": "' + str(example_output) + '"}'

  if verbose:
    print("CHAT GPT PROMPT")
    print(prompt)

  for i in range(repeat):
    try:
      curr_gpt_response = GPT4_request(prompt).strip()
      end_index = curr_gpt_response.rfind('}') + 1
      curr_gpt_response = curr_gpt_response[:end_index]
      curr_gpt_response = json.loads(curr_gpt_response)["output"]

      if func_validate(curr_gpt_response, prompt=prompt):
        return func_clean_up(curr_gpt_response, prompt=prompt)

      if verbose:
        print("---- repeat count: \n", i, curr_gpt_response)
        print(curr_gpt_response)
        print("~~~~")

    except:
      pass

  return False


def ChatGPT_safe_generate_response_OLD(prompt,
                                       repeat=3,
                                       fail_safe_response="error",
                                       func_validate=None,
                                       func_clean_up=None,
                                       verbose=False):
  if verbose:
    print("CHAT GPT PROMPT")
    print(prompt)

  for i in range(repeat):
    try:
      curr_gpt_response = ChatGPT_request(prompt).strip()
      if func_validate(curr_gpt_response, prompt=prompt):
        return func_clean_up(curr_gpt_response, prompt=prompt)
      if verbose:
        print(f"---- repeat count: {i}")
        print(curr_gpt_response)
        print("~~~~")

    except:
      pass
  print("FAIL SAFE TRIGGERED")
  return fail_safe_response


# ============================================================================
# ###################[SECTION 2: ORIGINAL GPT-3 STRUCTURE] ###################
# ============================================================================

def GPT_request(prompt, gpt_parameter):
  """Original Completion-style request, mapped onto chat completions.

  The gpt_parameter dict may carry engine/temperature/max_tokens/top_p/
  frequency_penalty/presence_penalty/stream/stop keys; `stream` is ignored.

  Legacy engines (text-davinci-003, etc.) don't exist on the OpenCode Go
  gateway, so they are all mapped onto the configured modern model
  (deepseek-v4-flash). max_tokens is raised to llm_max_tokens to give the
  model room for the full requested output.
  """
  temp_sleep()
  try:
    return _llm_chat(
      [{"role": "user", "content": prompt}],
      model=llm_model,
      max_tokens=max(llm_max_tokens, gpt_parameter.get("max_tokens") or 0),
      temperature=gpt_parameter.get("temperature", 1.0),
      top_p=gpt_parameter.get("top_p", 1.0),
      frequency_penalty=gpt_parameter.get("frequency_penalty", 0.0),
      presence_penalty=gpt_parameter.get("presence_penalty", 0.0),
      stop=gpt_parameter.get("stop"),
    )
  except Exception as e:
    print(f"[LLM ERROR] GPT_request: {e}", file=sys.stderr)
    return "TOKEN LIMIT EXCEEDED"


def generate_prompt(curr_input, prompt_lib_file):
  """
  Takes in the current input (e.g. comment that you want to classifiy) and
  the path to a prompt file. The prompt file contains the raw str prompt that
  will be used, which contains the following substr: !<INPUT>! -- this
  function replaces this substr with the actual curr_input to produce the
  final promopt that will be sent to the GPT3 server.
  ARGS:
    curr_input: the input we want to feed in (IF THERE ARE MORE THAN ONE
                INPUT, THIS CAN BE A LIST.)
    prompt_lib_file: the path to the promopt file.
  RETURNS:
    a str prompt that will be sent to OpenAI's GPT server.
  """
  if type(curr_input) == type("string"):
    curr_input = [curr_input]
  curr_input = [str(i) for i in curr_input]

  f = open(prompt_lib_file, "r")
  prompt = f.read()
  f.close()
  for count, i in enumerate(curr_input):
    prompt = prompt.replace(f"!<INPUT {count}>!", i)
  if "<commentblockmarker>###</commentblockmarker>" in prompt:
    prompt = prompt.split("<commentblockmarker>###</commentblockmarker>")[1]
  return prompt.strip()


def safe_generate_response(prompt,
                           gpt_parameter,
                           repeat=5,
                           fail_safe_response="error",
                           func_validate=None,
                           func_clean_up=None,
                           verbose=False):
  if verbose:
    print(prompt)

  for i in range(repeat):
    curr_gpt_response = GPT_request(prompt, gpt_parameter)
    try:
      if func_validate(curr_gpt_response, prompt=prompt):
        return func_clean_up(curr_gpt_response, prompt=prompt)
    except Exception as e:
      # Modern models occasionally deviate from the expected output format;
      # a parse failure here should retry, never kill the simulation.
      if verbose:
        print(f"---- parse retry ({i}): {e}")
    if verbose:
      print("---- repeat count: ", i, curr_gpt_response)
      print(curr_gpt_response)
      print("~~~~")
  return fail_safe_response


def get_embedding(text, model=None):
  """Embed text via the local LM Studio server (OpenAI-compatible)."""
  text = text.replace("\n", " ")
  if not text:
    text = "this is blank"

  url = embedding_base_url.rstrip("/") + "/embeddings"
  payload = {"model": model or embedding_model, "input": [text]}

  for attempt in range(3):
    try:
      resp = requests.post(url, json=payload, timeout=60)
      if resp.status_code == 200:
        data = resp.json()
        _record_usage(data.get("usage") or {}, embedding_model, embedding=True)
        return data["data"][0]["embedding"]
      raise RuntimeError(f"embedding API {resp.status_code}: {resp.text[:200]}")
    except (requests.RequestException, RuntimeError) as e:
      if attempt == 2:
        raise RuntimeError(f"embedding request failed: {e}") from e
      time.sleep(2 ** attempt + random.random())
  raise RuntimeError("embedding request failed")


if __name__ == '__main__':
  gpt_parameter = {"engine": llm_model, "max_tokens": 50,
                   "temperature": 0, "top_p": 1, "stream": False,
                   "frequency_penalty": 0, "presence_penalty": 0,
                   "stop": ['"']}
  curr_input = ["driving to a friend's house"]
  prompt_lib_file = "prompt_template/test_prompt_July5.txt"
  prompt = generate_prompt(curr_input, prompt_lib_file)

  def __func_validate(gpt_response):
    if len(gpt_response.strip()) <= 1:
      return False
    if len(gpt_response.strip().split(" ")) > 1:
      return False
    return True

  def __func_clean_up(gpt_response):
    cleaned_response = gpt_response.strip()
    return cleaned_response

  output = safe_generate_response(prompt,
                                  gpt_parameter,
                                  5,
                                  "rest",
                                  __func_validate,
                                  __func_clean_up,
                                  True)

  print(output)
