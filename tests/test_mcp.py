import json
from pathlib import Path
import queue
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from cachegate import Store


class McpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name))
        script = Path(__file__).resolve().parents[1] / "scripts" / "cachegate.py"
        self.process = subprocess.Popen(["rtk", "proxy", sys.executable, str(script), "--data-dir", self.temp.name, "serve"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
        self.messages = queue.Queue()
        self.reader = threading.Thread(target=self.read, daemon=True)
        self.reader.start()
        self.addCleanup(self.close)

    def close(self):
        self.process.stdin.close()
        self.process.wait(timeout=5)
        self.reader.join(timeout=1)
        self.process.stdout.close()
        self.process.stderr.close()

    def read(self):
        for line in self.process.stdout:
            self.messages.put(json.loads(line))

    def send(self, message):
        self.process.stdin.write(json.dumps({"jsonrpc": "2.0", **message}) + "\n")
        self.process.stdin.flush()

    def receive(self):
        return self.messages.get(timeout=5)

    def initialize(self, capabilities=None):
        self.send({"id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25", "capabilities": {"elicitation": {"form": {}}} if capabilities is None else capabilities}})
        self.assertEqual(self.receive()["result"]["serverInfo"]["name"], "cachegate")

    def check(self, request_id=2, session="session", turn="turn"):
        self.send({"id": request_id, "method": "tools/call", "params": {"name": "check_prompt", "arguments": {"session_id": session, "turn_id": turn}}})

    def test_setup_uses_native_form_and_blocks_on_cancel(self):
        self.initialize()
        self.check()
        form = self.receive()
        self.assertEqual(form["method"], "elicitation/create")
        self.send({"id": form["id"], "result": {"action": "cancel"}})
        result = self.receive()["result"]
        self.assertEqual(result["structuredContent"]["decision"], "block")
        self.assertFalse(result["isError"])
        self.assertIsNone(self.store.mode())

    def test_no_form_support_cannot_silently_choose_mode(self):
        self.initialize({})
        self.check()
        self.assertEqual(self.receive()["result"]["structuredContent"]["decision"], "block")

    def test_remind_works_without_form_support(self):
        self.store.set_mode("remind")
        self.store.record("session", "previous", 1, None)
        self.initialize({})
        self.check()
        self.assertIn("systemMessage", self.receive()["result"]["structuredContent"])

    def test_client_cancellation_preserves_state(self):
        self.store.set_mode("confirm")
        self.store.record("session", "previous", 1, None)
        self.initialize()
        self.check()
        form = self.receive()
        self.send({"method": "notifications/cancelled", "params": {"requestId": 2}})
        self.assertEqual(self.receive()["result"]["structuredContent"]["decision"], "block")
        self.assertEqual(self.store.last("session"), (1, "previous"))
        # A reply after cancellation must not approve a subsequent request.
        self.send({"id": form["id"], "result": {"action": "accept", "content": {"action": "send"}}})
        self.check(3)
        self.assertEqual(self.receive()["method"], "elicitation/create")

    def test_local_failure_returns_blocking_hook_result(self):
        with sqlite3.connect(self.store.path) as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES ('mode', 'bad-mode')")
        self.initialize()
        self.check()
        result = self.receive()["result"]
        self.assertFalse(result["isError"])
        self.assertEqual(result["structuredContent"]["decision"], "block")

    def test_other_session_can_continue_while_confirmation_is_pending(self):
        self.store.set_mode("confirm")
        self.store.record("session", "previous", 1, None)
        self.initialize()
        self.check()
        form = self.receive()
        self.check(3, "other-session")
        self.assertEqual(self.receive(), {"jsonrpc": "2.0", "id": 3, "result": {"content": [{"type": "text", "text": "{}"}], "structuredContent": {}, "isError": False}})
        self.send({"id": form["id"], "result": {"action": "decline"}})
        self.assertEqual(self.receive()["result"]["structuredContent"]["decision"], "block")


if __name__ == "__main__":
    unittest.main()
