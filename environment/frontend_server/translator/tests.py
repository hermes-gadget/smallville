import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

from django.test import SimpleTestCase, override_settings


BACKEND_DIR = os.path.abspath(os.path.join(
  os.path.dirname(__file__), "..", "..", "..", "reverie", "backend_server"))
if BACKEND_DIR not in sys.path:
  sys.path.insert(0, BACKEND_DIR)

from persona.cognitive_modules import converse
from persona.prompt_template import run_gpt_prompt
from translator import views


class PublicEndpointTests(SimpleTestCase):
  def test_sim_state_reports_the_validated_current_pacing(self):
    views._sim_state_cache.update({"at": 0.0, "running": None})
    completed = mock.Mock(returncode=0, stdout="active\n")
    with mock.patch("subprocess.run", return_value=completed), \
        mock.patch.object(views, "_read_public_pacing", return_value=24):
      response = self.client.get("/get_sim_state/")

    self.assertEqual(response.status_code, 200)
    self.assertEqual(response.json()["pacing"], 24)
    self.assertTrue(response.json()["sim_running"])

  def test_pacing_reader_prefers_a_valid_live_override(self):
    with tempfile.TemporaryDirectory(prefix="smallville-pacing-") as root:
      os.makedirs(os.path.join(root, "temp_storage"))
      os.makedirs(os.path.join(root, "storage", "public_sim", "reverie"))
      with open(os.path.join(root, "storage", "public_sim", "reverie",
                             "meta.json"), "w") as output:
        json.dump({"clock_pacing": 24}, output)
      with open(os.path.join(root, "temp_storage", "pacing.txt"), "w") as output:
        output.write("48")

      self.assertEqual(views._read_public_pacing(root), 48)

  def test_landing_page_uses_live_pacing_labels(self):
    response = self.client.get("/")
    body = response.content.decode("utf-8")

    self.assertEqual(response.status_code, 200)
    self.assertIn('id="live-pacing"', body)
    self.assertIn('id="map-pacing"', body)
    self.assertNotIn("60×", body)

  @override_settings(SMALLVILLE_ADMIN_TOKEN="")
  def test_admin_command_requires_authentication(self):
    response = self.client.post(
      "/admin/command/", data=json.dumps({"cmd": "broadcast", "args": {}}),
      content_type="application/json")

    self.assertEqual(response.status_code, 403)


class ChatLogContextTests(unittest.TestCase):
  def test_chat_logs_use_explicit_simulation_and_step_context(self):
    with tempfile.TemporaryDirectory(prefix="smallville-chat-") as root:
      with mock.patch.object(converse, "fs_storage", root):
        converse._live_log_chat([["Alice", "hello"]], "sim_a", 7)
        converse._live_log_chat([["Bob", "hi"]], "sim_b", 19)

      with open(os.path.join(root, "sim_a", "chat_log.json")) as infile:
        sim_a = json.load(infile)
      with open(os.path.join(root, "sim_b", "chat_log.json")) as infile:
        sim_b = json.load(infile)

    self.assertEqual(sim_a[0]["step"], 7)
    self.assertEqual(sim_b[0]["step"], 19)
    self.assertEqual(sim_a[0]["chat"], [["Alice", "hello"]])
    self.assertEqual(sim_b[0]["chat"], [["Bob", "hi"]])


class PromptLoggingTests(unittest.TestCase):
  def test_pronunciatio_does_not_enable_prompt_logging(self):
    with mock.patch.object(run_gpt_prompt, "generate_prompt",
                           return_value="prompt"), \
        mock.patch.object(run_gpt_prompt, "ChatGPT_safe_generate_response",
                          return_value="🙂") as response:
      captured = io.StringIO()
      with contextlib.redirect_stdout(captured):
        result = run_gpt_prompt.run_gpt_prompt_pronunciatio("washing dishes", None)

    self.assertEqual(result[0], "🙂")
    self.assertEqual(captured.getvalue(), "")
    self.assertNotIn(True, response.call_args.args)
