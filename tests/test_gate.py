from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "cachegate" / "scripts"))
from cachegate import Gate, HELP_HINT, Store, THRESHOLD_SECONDS


class GateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.store = Store(self.directory)
        self.now = 10000.0
        self.gate = Gate(self.store, lambda: self.now)
        self.sequence = 0

    def submit(self, prompt="Normal request", session="session-a", turn=None, model="model-a"):
        self.sequence += 1
        return self.gate.handle({
            "hook_event_name": "UserPromptSubmit", "session_id": session,
            "turn_id": turn or str(self.sequence), "prompt": prompt, "model": model,
        })

    def seed(self, mode="confirm", session="session-a"):
        self.store.set_mode(mode)
        self.assertEqual(self.submit("First request", session=session), {})

    def expire(self):
        self.now += THRESHOLD_SECONDS + 0.001

    def assert_blocked(self, result):
        self.assertEqual(result["decision"], "block")
        self.assertIn(HELP_HINT, result["reason"])

    def test_first_use_requires_mode(self):
        self.assert_blocked(self.submit())
        self.assertIsNone(self.store.mode())
        self.assertIsNone(self.store.last("session-a"))

    def test_help_is_local_without_state_file(self):
        result = self.submit("cachegate help")
        self.assertFalse(result["continue"])
        for command in ("remind", "confirm", "status", "allow", "cancel"):
            self.assertIn("cachegate " + command, result["stopReason"])
        self.assertFalse(self.store.path.exists())

    def test_bare_commands_persist_mode_without_recording_submission(self):
        for mode in ("remind", "confirm"):
            self.assertFalse(self.submit("cachegate " + mode)["continue"])
            self.assertEqual(Store(self.directory).mode(), mode)
            self.assertIsNone(self.store.last("session-a"))

    def test_exact_threshold_is_allowed(self):
        self.seed()
        self.now += THRESHOLD_SECONDS
        self.assertEqual(self.submit(), {})

    def test_over_threshold_stays_blocked_without_refreshing_clock(self):
        self.seed()
        previous = self.store.last("session-a")
        self.expire()
        self.assert_blocked(self.submit())
        self.now += 10
        self.assert_blocked(self.submit())
        self.assertEqual(self.store.last("session-a"), previous)

    def test_reminder_is_short_and_only_advertises_help(self):
        self.seed("remind")
        self.expire()
        result = self.submit()
        self.assertEqual(set(result), {"systemMessage"})
        notice = result["systemMessage"]
        self.assertIn(HELP_HINT, notice)
        self.assertEqual(notice.count("cachegate "), 1)
        self.assertLess(len(notice), 90)
        self.assertEqual(self.store.last("session-a")[0], self.now)

    def test_controls_do_not_refresh_clock(self):
        self.seed()
        previous = self.store.last("session-a")
        self.expire()
        self.assert_blocked(self.submit())
        for command in ("help", "status", "confirm", "allow", "cancel", "remind"):
            self.now += 1
            self.assertFalse(self.submit("cachegate " + command)["continue"])
            self.assertEqual(self.store.last("session-a"), previous)

    def test_single_allow_is_consumed_by_retry_and_uses_retry_time(self):
        self.seed()
        previous = self.store.last("session-a")
        self.expire()
        self.assert_blocked(self.submit("Blocked request"))
        self.submit("cachegate allow")
        self.assertEqual(self.store.last("session-a"), previous)
        self.now += 20
        self.assertEqual(self.submit("Blocked request"), {})
        self.assertEqual(self.store.last("session-a")[0], self.now)
        self.expire()
        self.assert_blocked(self.submit("Blocked request"))

    def test_allow_without_pending_does_not_preapprove(self):
        self.seed()
        result = self.submit("cachegate allow")
        self.assertIn("没有待确认", result["stopReason"])
        self.expire()
        self.assert_blocked(self.submit())

    def test_allow_is_bound_to_text_and_changed_request_revokes_it(self):
        self.seed()
        self.expire()
        self.assert_blocked(self.submit("Request A"))
        self.submit("cachegate allow")
        self.assert_blocked(self.submit("Request B"))
        self.assert_blocked(self.submit("Request A"))

    def test_allow_is_bound_to_model(self):
        self.seed()
        self.expire()
        self.assert_blocked(self.submit())
        self.submit("cachegate allow")
        self.assert_blocked(self.submit(model="model-b"))

    def test_allow_is_bound_to_session(self):
        self.seed()
        self.seed(session="session-b")
        self.expire()
        self.assert_blocked(self.submit())
        self.assert_blocked(self.submit(session="session-b"))
        self.submit("cachegate allow")
        self.assert_blocked(self.submit(session="session-b"))
        self.assertEqual(self.submit(), {})

    def test_cancel_revokes_permission(self):
        self.seed()
        self.expire()
        self.assert_blocked(self.submit())
        self.submit("cachegate allow")
        self.submit("cachegate cancel")
        self.assert_blocked(self.submit())

    def test_restart_preserves_timestamp_and_pending_permission(self):
        self.seed()
        self.expire()
        self.assert_blocked(self.submit())
        self.submit("cachegate allow")
        self.gate = Gate(Store(self.directory), lambda: self.now)
        self.assertEqual(self.submit(), {})

    def test_new_session_has_no_timeout_history(self):
        self.seed()
        self.expire()
        self.assertEqual(self.submit(session="new-session"), {})

    def test_mode_switch_takes_effect_on_next_retry(self):
        self.seed()
        self.expire()
        self.assert_blocked(self.submit())
        self.submit("cachegate remind")
        self.assertIn("systemMessage", self.submit())

    def test_clock_rollback_uses_selected_mode(self):
        self.seed()
        self.now -= 1
        result = self.submit()
        self.assert_blocked(result)
        self.assertIn("系统时间回退", result["reason"])
        self.submit("cachegate remind")
        self.assertIn("系统时间回退", self.submit()["systemMessage"])

    def test_identical_user_message_in_same_turn_is_checked(self):
        self.store.set_mode("confirm")
        self.assertEqual(self.submit(turn="same-turn"), {})
        previous = self.store.last("session-a")
        self.expire()
        self.assert_blocked(self.submit(turn="same-turn"))
        self.assertEqual(self.store.last("session-a"), previous)

    def test_different_user_message_in_same_turn_is_checked(self):
        self.store.set_mode("confirm")
        self.assertEqual(self.submit("First", turn="same-turn"), {})
        self.expire()
        self.assert_blocked(self.submit("Second", turn="same-turn"))

    def test_prose_and_multiline_mentions_are_normal_messages(self):
        self.store.set_mode("remind")
        for prompt in (
            "Explain cachegate remind", "cachegate remind in this example",
            "cachegate help\nThen edit the app", "Use cachegate confirm in the example",
        ):
            with self.subTest(prompt=prompt):
                self.assertEqual(self.submit(prompt), {})

    def test_unknown_control_is_consumed_locally(self):
        for command in ("unknown", "REMIND", "help?", "123"):
            with self.subTest(command=command):
                result = self.submit("cachegate " + command)
                self.assertFalse(result["continue"])
                self.assertIn(HELP_HINT, result["stopReason"])
        self.assertIsNone(self.store.mode())

    def test_mode_command_can_repair_invalid_mode(self):
        with self.store.transaction() as db:
            db.execute("INSERT INTO settings VALUES ('mode', 'invalid')")
        self.submit("cachegate confirm")
        self.assertEqual(self.store.mode(), "confirm")

    def test_legacy_state_preserves_mode_and_timestamp(self):
        db = sqlite3.connect(self.store.path)
        with db:
            db.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execute("INSERT INTO settings VALUES ('mode', 'confirm')")
            db.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, last_at REAL NOT NULL, turn_id TEXT NOT NULL)")
            db.execute("INSERT INTO sessions VALUES ('session-a', 1, 'old-turn')")
        db.close()
        self.assert_blocked(self.submit())
        self.assertEqual(self.store.mode(), "confirm")
        self.assertEqual(self.store.last("session-a"), (1, "old-turn"))

    def test_state_does_not_store_prompt_plaintext(self):
        self.seed()
        self.expire()
        text = "PRIVATE PROMPT MARKER 7ed13f9d"
        self.assert_blocked(self.submit(text))
        self.assertNotIn(text.encode(), self.store.path.read_bytes())


if __name__ == "__main__":
    unittest.main()
