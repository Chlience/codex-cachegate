import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from cachegate import Gate, Store, THRESHOLD_SECONDS


class GateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name))
        self.now = 10000.0
        self.gate = Gate(self.store, lambda: self.now)
        self.questions = []
        self.answers = []

    def ask(self, message, field, choices):
        self.questions.append((message, field, choices))
        return self.answers.pop(0)

    def check(self, turn="next", session="session-a"):
        return self.gate.check(session, turn, self.ask)

    def seed(self, mode="confirm"):
        self.store.set_mode(mode)
        self.check("first")

    def test_first_use_requires_explicit_mode_and_persists_it(self):
        self.answers = ["confirm"]
        self.assertIn("systemMessage", self.check())
        self.assertEqual(Store(Path(self.temp.name)).mode(), "confirm")
        self.assertEqual(self.questions[0][1], "mode")

    def test_cancel_setup_preserves_unconfigured_state(self):
        self.answers = [None]
        self.assertEqual(self.check()["decision"], "block")
        self.assertIsNone(self.store.mode())
        self.assertIsNone(self.store.last("session-a"))

    def test_exactly_thirty_minutes_does_not_prompt(self):
        self.seed()
        self.now += THRESHOLD_SECONDS
        self.assertEqual(self.check(), {})
        self.assertEqual(self.questions, [])

    def test_above_threshold_requires_send(self):
        self.seed()
        self.now += THRESHOLD_SECONDS + 0.001
        self.answers = ["send"]
        self.assertEqual(self.check(), {})
        self.assertEqual(len(self.questions), 1)
        self.assertEqual(self.store.last("session-a")[0], self.now)

    def test_cancel_does_not_reset_clock_and_retry_prompts_again(self):
        self.seed()
        previous = self.store.last("session-a")
        self.now += 1801
        for answer in [None, "cancel", "invalid"]:
            self.answers = [answer]
            self.assertEqual(self.check()["decision"], "block")
            self.assertEqual(self.store.last("session-a"), previous)
        self.assertEqual(len(self.questions), 3)

    def test_remind_continues_without_asking_or_model_context(self):
        self.seed("remind")
        self.now += 1801
        self.assertEqual(set(self.check()), {"systemMessage"})
        self.assertEqual(self.questions, [])

    def test_sessions_are_isolated(self):
        self.seed()
        self.now += 1801
        self.assertEqual(self.check(session="session-b"), {})
        self.assertEqual(self.questions, [])

    def test_resume_uses_persisted_timestamp(self):
        self.seed()
        self.now += 1801
        self.gate = Gate(Store(Path(self.temp.name)), lambda: self.now)
        self.answers = [None]
        self.assertEqual(self.check()["decision"], "block")

    def test_configuration_changes_take_effect_without_restart(self):
        self.seed()
        self.now += 1801
        Store(Path(self.temp.name)).set_mode("remind")
        self.assertIn("systemMessage", self.check())

    def test_accept_records_time_after_user_decision(self):
        self.seed()
        self.now += 1801

        def delayed(*args):
            self.now += 120
            return "send"

        self.gate.check("session-a", "next", delayed)
        self.assertEqual(self.store.last("session-a")[0], self.now)

    def test_clock_rollback_requires_decision(self):
        self.seed()
        self.now -= 60
        self.answers = [None]
        self.assertEqual(self.check()["decision"], "block")

    def test_missing_ids_block(self):
        self.assertEqual(self.gate.check("", "turn", self.ask)["decision"], "block")
        self.assertIsNone(self.store.mode())

    def test_duplicate_callback_does_not_advance_timestamp(self):
        self.seed()
        self.now += 60
        self.check("first")
        self.assertEqual(self.store.last("session-a")[0], 10000)

    def test_concurrent_state_change_does_not_overwrite_newer_submission(self):
        self.seed()
        previous = self.store.last("session-a")
        self.now += 1801

        def concurrent(*args):
            self.store.record("session-a", "other", self.now + 1, previous)
            return "send"

        result = self.gate.check("session-a", "next", concurrent)
        self.assertEqual(result["decision"], "block")
        self.assertEqual(self.store.last("session-a")[1], "other")

    def test_cli_config(self):
        script = Path(__file__).resolve().parents[1] / "scripts" / "cachegate.py"
        result = subprocess.run(["rtk", "proxy", sys.executable, str(script), "--data-dir", self.temp.name, "config", "--mode", "remind"], check=True, capture_output=True, text=True)
        self.assertEqual(json.loads(result.stdout)["mode"], "remind")

    def test_only_timing_and_identifiers_are_stored(self):
        self.seed()
        with sqlite3.connect(self.store.path) as db:
            names = [row[1] for row in db.execute("PRAGMA table_info(sessions)")]
        self.assertEqual(names, ["id", "last_at", "turn_id"])


if __name__ == "__main__":
    unittest.main()
