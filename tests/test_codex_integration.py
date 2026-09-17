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
from cachegate import Store
from install import prepare_plugin

ROOT = Path(__file__).resolve().parents[1]


class FakeModel(http.server.BaseHTTPRequestHandler):
    requests = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        self.requests.append(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
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
        self.wfile.write(body)


class Client:
    def __init__(self, env, cwd, log):
        self.log = open(log, "w")
        self.process = subprocess.Popen(["rtk", "proxy", "codex", "app-server"], cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log, text=True, bufsize=1)
        self.events = queue.Queue()
        self.sequence = 0
        self.backlog = []
        self.reader = threading.Thread(target=self.read, daemon=True)
        self.reader.start()
        self.rpc("initialize", {"clientInfo": {"name": "cachegate_test", "version": "0.1.0"}, "capabilities": {"experimentalApi": True}})
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
        # Keep logs and fixtures for diagnosing failures; never touch the user's Codex home.
        scratch = Path(tempfile.mkdtemp(prefix="cachegate-integration-"))
        print("Integration artifacts:", scratch, flush=True)
        codex_home = scratch / "codex"
        codex_home.mkdir()
        market = scratch / "market"
        plugin = market / "plugins" / "cachegate"
        plugin.parent.mkdir(parents=True)
        prepare_plugin(plugin, scratch / "data")
        manifest = market / ".agents" / "plugins" / "marketplace.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({"name": "cachegate-test", "plugins": [{"name": "cachegate", "source": {"source": "local", "path": "./plugins/cachegate"}, "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"}, "category": "Productivity"}]}))
        FakeModel.requests = []
        model_server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeModel)
        self.addCleanup(model_server.server_close)
        self.addCleanup(model_server.shutdown)
        threading.Thread(target=model_server.serve_forever, daemon=True).start()
        (codex_home / "config.toml").write_text(f'''
model_provider = "cachegate-test"
model = "mock-model"
approval_policy = "on-request"
sandbox_mode = "read-only"
mcp_optional_startup_grace_ms = 0
[features]
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
        env = {**os.environ, "CODEX_HOME": str(codex_home), "CACHEGATE_DATA_DIR": str(scratch / "data")}
        client = Client(env, scratch, scratch / "install.log")
        try:
            client.rpc("plugin/install", {"pluginName": "cachegate", "marketplacePath": str(manifest)})
        finally:
            client.close()
        client = Client(env, scratch, scratch / "run.log")
        self.addCleanup(client.close)
        hooks = client.rpc("hooks/list", {"cwds": [str(scratch)]})
        self.assertEqual(len(hooks["data"][0]["hooks"]), 1)
        self.assertEqual(hooks["data"][0]["errors"], [])
        thread = client.rpc("thread/start", {"cwd": str(scratch), "baseInstructions": "Reply OK.", "config": {"bypass_hook_trust": True}})["thread"]["id"]
        store = Store(scratch / "data")

        def start():
            client.rpc("turn/start", {"threadId": thread, "input": [{"type": "text", "text": "Test message", "text_elements": []}]})

        def answer(action, content=None):
            event = client.wait(lambda x: x.get("method") == "mcpServer/elicitation/request")
            self.assertEqual(len(FakeModel.requests), expected_requests)
            client.send({"id": event["id"], "result": {"action": action, "content": content}})
            return event

        def complete():
            event = client.wait(lambda x: x.get("method") == "turn/completed")
            return event

        expected_requests = 0
        start()
        answer("cancel")
        complete()
        self.assertEqual(len(FakeModel.requests), 0)
        self.assertIsNone(store.mode())

        start()
        answer("accept", {"mode": "confirm"})
        complete()
        self.assertEqual(len(FakeModel.requests), 1)
        self.assertEqual(store.mode(), "confirm")

        old = store.last(thread)
        self.assertIsNotNone(old)
        store.record(thread, old[1], time.time() - 1801, old)
        baseline = store.last(thread)
        expected_requests = 1
        start()
        answer("decline")
        complete()
        self.assertEqual(len(FakeModel.requests), 1)
        self.assertEqual(store.last(thread), baseline)

        start()
        answer("accept", {"action": "send"})
        complete()
        self.assertEqual(len(FakeModel.requests), 2)

        old = store.last(thread)
        store.record(thread, old[1], time.time() - 1801, old)
        store.set_mode("remind")
        start()
        complete()
        self.assertEqual(len(FakeModel.requests), 3)
        warning = client.wait(lambda x: x.get("method") == "hook/completed" and any("超过 30 分钟" in e.get("text", "") for e in x["params"]["run"]["entries"]))
        self.assertTrue(warning)
        self.assertFalse(any(x.get("method") == "mcpServer/elicitation/request" for x in client.backlog))


if __name__ == "__main__":
    unittest.main()
