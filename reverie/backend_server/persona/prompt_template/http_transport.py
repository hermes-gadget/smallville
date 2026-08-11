"""Killable JSON HTTP transport used by model and embedding calls."""

import json
import subprocess
import sys


class TransportError(RuntimeError):
  pass


class TransportTimeout(TransportError):
  pass


def _post_worker(request):
  import requests

  result = {}
  try:
    with requests.Session() as session:
      response = session.post(
        request["url"], json=request["payload"],
        headers=request.get("headers") or {},
        timeout=(min(10.0, request["timeout"]), request["timeout"]))
      result = {
        "status": response.status_code,
        "text": response.text,
        "headers": dict(response.headers),
      }
  except Exception as error:
    result = {"error": "%s: %s" % (type(error).__name__, error)}
  return result


def _worker_main():
  request = json.load(sys.stdin)
  json.dump(_post_worker(request), sys.stdout)


def post_json(url, payload, headers=None, deadline=30.0):
  """POST in a child process that is terminated at the hard deadline."""
  deadline = max(0.1, float(deadline))
  try:
    completed = subprocess.run(
      [sys.executable, __file__, "--worker"],
      input=json.dumps({"url": url, "payload": payload,
                        "headers": headers or {}, "timeout": deadline}),
      text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
      timeout=deadline, check=False)
    if completed.returncode != 0:
      raise TransportError("HTTP worker exited with status %d" %
                           completed.returncode)
    result = json.loads(completed.stdout)
    if result.get("error"):
      raise TransportError(result["error"])
    return result
  except subprocess.TimeoutExpired as error:
    raise TransportTimeout(
      "HTTP request exceeded %.1fs deadline" % deadline) from error
  except (TypeError, ValueError) as error:
    raise TransportError("HTTP worker produced no valid response") from error


if __name__ == "__main__":
  _worker_main()
