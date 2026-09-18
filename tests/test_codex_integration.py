"""Opt-in real Codex test using a loopback fake model and isolated CODEX_HOME."""

import http.server
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "cachegate" / "scripts"))
from cachegate import Store
from install import prepare_plugin

ROOT = Path(__file__).resolve().parents[1]


class FakeModel(http.server.BaseHTTPRequestHandler):
    requests = []
    pauses = queue.Queue()

    def log_message(self, *args):
        pass

    def do_POST(self):
        self.requests.append(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        try:
            started, release = self.pauses.get_nowait()
        except queue.Empty:
            pass
        else:
            started.set()
            release.wait(timeout=20)
        item = {"id": "msg_mock", "type": "message", "role": "assistant", "status": "completed", "content": [{"type": "output_text", "text": "OK", "annotations": []}]}
        response = {"id": "resp_mock", "object": "response", "created_at": 1, "status": "completed", "model": "mock-model", "output": [item], "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}
        events = [
            {"type": "response.created", "response": {**response, "status": "in_progress", "output": []}},
            {"type": "response.output_item.done", "output_index": 0, "item": item},
            {"type": "response.completed", "response": response},
        ]
        body = "".join("data: " + json.dumps(event) + "\n\n" for event in events).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # Interrupting a turn closes its pending model request.


class Client:
    def __init__(self, env, cwd, log):
        self.log = open(log, "w")
        self.process = subprocess.Popen(["rtk", "proxy", "codex", "app-server"], cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log, text=True, bufsize=1)
        self.events = queue.Queue()
        self.sequence = 0
        self.backlog = []
        self.reader = threading.Thread(target=self.read, daemon=True)
        self.reader.start()
        self.rpc("initialize", {"clientInfo": {"name": "cachegate_test", "version": "0.2.0"}, "capabilities": {"experimentalApi": True}})
        self.send({"method": "initialized"})

    def read(self):
        for line in self.process.stdout:
            self.events.put(json.loads(line))

    def send(self, message):
        self.process.stdin.write(json.dumps({"jsonrpc": "2.0", **message}) + "\n")
        self.process.stdin.flush()

    def wait(self, predicate, timeout=20):
        deadline = time.monotonic() + timeout
        while True:
            for i, item in enumerate(self.backlog):
                if predicate(item):
                    return self.backlog.pop(i)
            try:
                item = self.events.get(timeout=max(0, deadline - time.monotonic()))
            except queue.Empty:
                raise AssertionError("Codex event timeout; events=" + json.dumps(self.backlog, ensure_ascii=False)[-16000:])
            if predicate(item):
                return item
            self.backlog.append(item)

    def rpc(self, method, params):
        self.sequence += 1
        request_id = self.sequence
        self.send({"id": request_id, "method": method, "params": params})
        result = self.wait(lambda x: x.get("id") == request_id and "method" not in x)
        if "error" in result:
            raise AssertionError(result)
        return result["result"]

    def close(self):
        self.process.stdin.close()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            self.process.wait(timeout=5)
        self.reader.join(timeout=2)
        self.process.stdout.close()
        self.log.close()


@unittest.skipUnless(os.environ.get("CACHEGATE_CODEX_INTEGRATION") == "1", "set CACHEGATE_CODEX_INTEGRATION=1 for isolated Codex integration")
class CodexIntegration(unittest.TestCase):
    def test_plugin_gate_before_model_request(self):
        self.check_plugin_gate(prepared=True)

    def test_repository_package_before_model_request(self):
        self.check_plugin_gate(prepared=False)

    def check_plugin_gate(self, prepared):
        # Keep logs and fixtures for diagnosing failures; never touch the user's Codex home.
        scratch = Path(tempfile.mkdtemp(prefix="cachegate-integration-"))
        print("Integration artifacts:", scratch, flush=True)
        codex_home = scratch / "codex"
        codex_home.mkdir()
        market = scratch / "market"
        plugin = market / "plugins" / "cachegate"
        plugin.parent.mkdir(parents=True)
        state_dir = scratch / "data" if prepared else codex_home / "cachegate"
        if prepared:
            prepare_plugin(plugin, state_dir)
        else:
            shutil.copytree(ROOT / "plugins" / "cachegate", plugin, ignore=shutil.ignore_patterns("__pycache__"))
        manifest = market / ".agents" / "plugins" / "marketplace.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_bytes((ROOT / ".agents/plugins/marketplace.json").read_bytes())
        FakeModel.requests = []
        FakeModel.pauses = queue.Queue()
        model_server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeModel)
        self.addCleanup(model_server.server_close)
        self.addCleanup(model_server.shutdown)
        threading.Thread(target=model_server.serve_forever, daemon=True).start()
        (codex_home / "config.toml").write_text(f'''
model_provider = "cachegate-test"
model = "mock-model"
approval_policy = "on-request"
sandbox_mode = "read-only"
[features]
enable_request_compression = false
apps = false
remote_plugin = false
memories = false
[model_providers.cachegate-test]
name = "Local test only"
base_url = "http://127.0.0.1:{model_server.server_port}/v1"
wire_api = "responses"
requires_openai_auth = false
supports_websockets = false
''')
        env = {**os.environ, "CODEX_HOME": str(codex_home)}
        env.pop("CACHEGATE_DATA_DIR", None)
        client = Client(env, scratch, scratch / "install.log")
        try:
            client.rpc("plugin/install", {"pluginName": "cachegate", "marketplacePath": str(manifest)})
        finally:
            client.close()
        client = Client(env, scratch, scratch / "run.log")
        self.addCleanup(client.close)
        hooks = client.rpc("hooks/list", {"cwds": [str(scratch)]})
        self.assertEqual(len(hooks["data"][0]["hooks"]), 3)
        self.assertEqual(hooks["data"][0]["errors"], [])
        thread = client.rpc("thread/start", {"cwd": str(scratch), "baseInstructions": "Reply OK.", "config": {"bypass_hook_trust": True}})["thread"]["id"]
        store = Store(state_dir)

        def submit(text, expected_requests):
            result = client.rpc("turn/start", {
                "threadId": thread,
                "input": [{"type": "text", "text": text, "text_elements": []}],
            })
            turn = result["turn"]["id"]
            client.wait(lambda x: x.get("method") == "turn/completed"
                        and x["params"]["turn"]["id"] == turn)
            self.assertEqual(len(FakeModel.requests), expected_requests)

        def expire():
            with store.transaction() as db:
                ended, active = db.execute(
                    "SELECT ended_at, active_turn_id FROM sessions WHERE id = ?", (thread,)
                ).fetchone()
                self.assertIsNotNone(ended)
                self.assertIsNone(active)
                db.execute("UPDATE sessions SET ended_at = ? WHERE id = ?", (time.time() - 1801, thread))
            return store.last(thread)

        def check_reminder():
            warning = client.wait(
                lambda x: x.get("method") == "hook/completed"
                and any("本次继续发送" in e.get("text", "") for e in x["params"]["run"]["entries"])
            )
            notices = [e["text"] for e in warning["params"]["run"]["entries"] if "本次继续发送" in e.get("text", "")]
            self.assertEqual(len(notices), 1)
            self.assertLess(len(notices[0]), 90)
            self.assertIn("cachegate help", notices[0])

        def check_blocked():
            warning = client.wait(
                lambda x: x.get("method") == "hook/completed"
                and any("本次已拦截" in e.get("text", "") for e in x["params"]["run"]["entries"])
            )
            notices = [e["text"] for e in warning["params"]["run"]["entries"] if "本次已拦截" in e.get("text", "")]
            self.assertEqual(len(notices), 1)
            self.assertIn("cachegate allow", notices[0])
            self.assertNotIn("cachegate help", notices[0])

        submit("cachegate help", 0)
        help_event = client.wait(
            lambda x: x.get("method") == "hook/completed"
            and any("cachegate allow" in entry.get("text", "") for entry in x["params"]["run"]["entries"])
        )
        self.assertTrue(help_event)
        submit("Fresh model request", 1)
        self.assertEqual(store.mode(), "remind")
        expire()
        submit("Default reminder request", 2)
        check_reminder()
        submit("cachegate confirm", 2)
        self.assertEqual(store.mode(), "confirm")

        baseline = expire()
        submit("Older blocked request", 2)
        check_blocked()
        submit("Another blocked request", 2)
        check_blocked()
        submit("Pending model request", 2)
        check_blocked()
        self.assertEqual(store.last(thread), baseline)
        submit("cachegate status", 2)
        submit("cachegate help", 2)
        self.assertEqual(store.last(thread), baseline)
        submit("cachegate allow", 2)
        submit("cachegate status", 2)
        submit("cachegate help", 2)
        self.assertEqual(store.last(thread), baseline)
        submit("Older blocked request", 3)

        baseline = expire()
        submit("Original request before editing", 3)
        check_blocked()
        submit("cachegate allow", 3)
        self.assertEqual(store.last(thread), baseline)
        submit("Edited request after approval", 4)

        baseline = expire()
        submit("Cancelled then reminded request", 4)
        submit("cachegate allow", 4)
        submit("cachegate cancel", 4)
        submit("Cancelled then reminded request", 4)
        self.assertEqual(store.last(thread), baseline)
        submit("cachegate remind", 4)
        self.assertEqual(store.mode(), "remind")
        submit("Cancelled then reminded request", 5)
        check_reminder()

        # Hold real HTTP responses while simulating an old submission timestamp.
        # Completion and interruption must establish fresh idle baselines.
        submit("cachegate confirm", 5)

        def start_long_turn(text):
            started, release = threading.Event(), threading.Event()
            self.addCleanup(release.set)
            FakeModel.pauses.put((started, release))
            result = client.rpc("turn/start", {
                "threadId": thread,
                "input": [{"type": "text", "text": text, "text_elements": []}],
            })
            turn = result["turn"]["id"]
            self.assertTrue(started.wait(timeout=10))
            with store.transaction() as db:
                self.assertEqual(db.execute(
                    "SELECT ended_at, active_turn_id FROM sessions WHERE id = ?", (thread,)
                ).fetchone(), (None, turn))
                db.execute("UPDATE sessions SET last_at = ? WHERE id = ?", (time.time() - 7200, thread))
            return turn, release

        turn, release = start_long_turn("Long running request")
        release.set()
        client.wait(lambda x: x.get("method") == "turn/completed" and x["params"]["turn"]["id"] == turn)
        self.assertGreater(time.time() - store.last(thread)[0], 1800)
        submit("Immediately after long task", 7)

        turn, release = start_long_turn("Long request with follow-up")
        client.rpc("turn/steer", {
            "threadId": thread, "expectedTurnId": turn,
            "input": [{"type": "text", "text": "Follow-up during execution", "text_elements": []}],
        })
        release.set()
        client.wait(lambda x: x.get("method") == "turn/completed" and x["params"]["turn"]["id"] == turn)
        self.assertEqual(len(FakeModel.requests), 9)
        submit("Immediately after follow-up", 10)

        turn, release = start_long_turn("Interrupted long request")
        client.rpc("turn/interrupt", {"threadId": thread, "turnId": turn})
        client.wait(lambda x: x.get("method") == "turn/completed" and x["params"]["turn"]["id"] == turn)
        release.set()
        with store.transaction() as db:
            ended, active = db.execute(
                "SELECT ended_at, active_turn_id FROM sessions WHERE id = ?", (thread,)
            ).fetchone()
        self.assertIsNone(active)
        self.assertIsNotNone(ended)
        self.assertLess(time.time() - ended, 10)
        self.assertGreater(time.time() - store.last(thread)[0], 1800)
        submit("Immediately after interruption", 12)
        expire()
        submit("Idle after completion", 12)
        check_blocked()
        self.assertFalse(any(x.get("method") == "mcpServer/elicitation/request" for x in client.backlog))
        self.assertFalse(any(
            x.get("method") == "mcpServer/startupStatus"
            and "cachegate" in json.dumps(x) for x in client.backlog
        ))

        # Inspect actual HTTP bodies, including the next request after local controls.
        payloads = [json.loads(raw) for raw in FakeModel.requests]
        model_text = json.dumps(payloads, ensure_ascii=False)
        for local_only in (
            "cachegate help", "cachegate confirm", "cachegate status",
            "cachegate allow", "cachegate cancel", "cachegate remind",
            "CacheGate", "mcp__cachegate",
            "Another blocked request", "Pending model request", "Original request before editing",
        ):
            self.assertNotIn(local_only, model_text)
        self.assertIn("Older blocked request", model_text)
        self.assertIn("Edited request after approval", model_text)
        self.assertIn("Fresh model request", model_text)
        self.assertIn("Default reminder request", model_text)
        self.assertIn("Cancelled then reminded request", model_text)
        (scratch / "model-requests.json").write_text(
            json.dumps(payloads, ensure_ascii=False, indent=2), encoding="utf-8"
        )


if __name__ == "__main__":
    unittest.main()
