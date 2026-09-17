import json
from pathlib import Path
import subprocess
import sqlite3
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "plugins" / "cachegate" / "scripts" / "cachegate.py"


class HookProcessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def run_hook(self, prompt="Ordinary request", raw=None, model="test"):
        payload = {"hook_event_name": "UserPromptSubmit", "session_id": "session",
                   "turn_id": "turn", "prompt": prompt, "model": model}
        result = subprocess.run(
            ["rtk", "proxy", sys.executable, str(SCRIPT), "--data-dir", str(self.directory), "hook"],
            input=json.dumps(payload) if raw is None else raw,
            capture_output=True, text=True, check=True,
        )
        self.assertEqual(result.stderr, "")
        return json.loads(result.stdout)

    def test_help_stops_locally_without_state_or_context(self):
        result = self.run_hook("cachegate help")
        self.assertEqual(set(result), {"continue", "stopReason"})
        self.assertFalse(result["continue"])
        self.assertFalse((self.directory / "state.sqlite3").exists())

    def test_separate_processes_share_mode(self):
        self.assertFalse(self.run_hook("cachegate confirm")["continue"])
        self.assertEqual(self.run_hook(), {})

    def test_multiple_blocks_allow_and_retry_across_processes(self):
        self.run_hook("cachegate confirm")
        self.assertEqual(self.run_hook("First request"), {})
        with sqlite3.connect(self.directory / "state.sqlite3") as db:
            db.execute("UPDATE sessions SET last_at = 0")
        for prompt in ("Request A", "Request B", "Request B"):
            result = self.run_hook(prompt)
            self.assertEqual(result["decision"], "block")
            self.assertIn("cachegate allow", result["reason"])
            self.assertNotIn("cachegate help", result["reason"])
        self.assertFalse(self.run_hook("cachegate allow")["continue"])
        self.assertFalse(self.run_hook("cachegate help")["continue"])
        self.assertFalse(self.run_hook("cachegate status")["continue"])
        self.assertEqual(self.run_hook("Request A (edited)", model="changed-model"), {})
        with sqlite3.connect(self.directory / "state.sqlite3") as db:
            db.execute("UPDATE sessions SET last_at = 0")
        self.assertEqual(self.run_hook("Request B")["decision"], "block")

    def test_malformed_input_returns_blocking_json(self):
        for raw in ("broken JSON", "[]", "null", "{}", '{"prompt":null}'):
            with self.subTest(raw=raw):
                self.assertEqual(self.run_hook(raw=raw)["decision"], "block")

    def test_corrupt_state_blocks_but_help_remains_available(self):
        (self.directory / "state.sqlite3").write_bytes(b"not a database")
        self.assertEqual(self.run_hook()["decision"], "block")
        self.assertFalse(self.run_hook("cachegate help")["continue"])

    def test_legacy_terminal_config_still_works(self):
        result = subprocess.run(
            ["rtk", "proxy", sys.executable, str(SCRIPT), "--data-dir", str(self.directory),
             "config", "--mode", "remind"], capture_output=True, text=True, check=True,
        )
        self.assertEqual(json.loads(result.stdout)["mode"], "remind")
        self.assertEqual(self.run_hook(), {})


if __name__ == "__main__":
    unittest.main()
