#!/usr/bin/env python3
"""Local MCP hook for Codex. Python 3.10+, standard library only."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import queue
import sqlite3
import sys
import threading
import time
import uuid


THRESHOLD_SECONDS = 30 * 60
MODES = ("remind", "confirm")
CLEAR_HINT = "可手动执行 /clear 新建会话，再重新输入请求；新会话不保留当前对话上下文。"
CACHE_NOTE = "时间间隔不能证明 KV 缓存是否命中，/clear 也不能恢复过期缓存。"


def data_dir() -> Path:
    if os.environ.get("CACHEGATE_DATA_DIR"):
        return Path(os.environ["CACHEGATE_DATA_DIR"]).expanduser()
    return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "cachegate"


class Store:
    """Short SQLite transactions; never hold a DB lock while the user decides."""

    def __init__(self, directory: Path):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = directory / "state.sqlite3"
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, last_at REAL NOT NULL, turn_id TEXT NOT NULL)")

    def connect(self):
        return sqlite3.connect(self.path, timeout=5)

    def mode(self):
        with self.connect() as db:
            row = db.execute("SELECT value FROM settings WHERE key = 'mode'").fetchone()
        if row and row[0] not in MODES:
            raise ValueError("保存的模式无效，请通过 config --mode 修复。")
        return row[0] if row else None

    def set_mode(self, mode):
        if mode not in MODES:
            raise ValueError("mode must be remind or confirm")
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES ('mode', ?)", (mode,))

    def last(self, session):
        with self.connect() as db:
            return db.execute("SELECT last_at, turn_id FROM sessions WHERE id = ?", (session,)).fetchone()

    def record(self, session, turn, timestamp, previous):
        # A second Codex process may resume the same session while a dialog is open.
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT last_at, turn_id FROM sessions WHERE id = ?", (session,)).fetchone()
            if current != previous:
                return False
            db.execute("INSERT OR REPLACE INTO sessions VALUES (?, ?, ?)", (session, timestamp, turn))
        return True


def blocked(reason):
    return {"decision": "block", "reason": "CacheGate：" + reason}


class Gate:
    def __init__(self, store, clock=time.time):
        self.store = store
        self.clock = clock

    def configure(self, ask):
        answer = ask(
            "CacheGate 设置：同一会话两次获准提交的用户消息相距超过 30 分钟时如何处理？" + CACHE_NOTE,
            "mode",
            [("remind", "仅提醒并继续发送"), ("confirm", "拦截，明确允许后发送")],
        )
        if answer not in MODES:
            return blocked("尚未选择模式，本次请求未获准发送。请重试并选择模式。")
        self.store.set_mode(answer)
        return {"systemMessage": f"CacheGate：已保存 {answer} 模式，阈值为超过 30 分钟。"}

    def check(self, session, turn, ask):
        if not isinstance(session, str) or not session or not isinstance(turn, str) or not turn:
            return blocked("缺少会话或消息标识，无法检查间隔。")
        notice = {}
        if self.store.mode() is None:
            notice = self.configure(ask)
            if notice.get("decision") == "block":
                return notice
        mode = self.store.mode()
        previous = self.store.last(session)
        if previous and previous[1] == turn:
            return notice
        gap = self.clock() - previous[0] if previous else None
        if gap is not None and (gap > THRESHOLD_SECONDS or gap < 0):
            elapsed = f"距本会话上次获准提交已超过 30 分钟（约 {gap / 60:.1f} 分钟）。" if gap > 0 else "系统时间发生回退，无法可靠计算消息间隔。"
            warning = "CacheGate：" + elapsed + CACHE_NOTE
            if mode == "confirm":
                answer = ask(warning + " 是否继续发送当前请求？" + CLEAR_HINT,
                             "action", [("send", "允许发送当前请求"), ("cancel", "取消，返回后自行决定是否 /clear")])
                if answer != "send":
                    return blocked("本次请求未获准发送。" + CLEAR_HINT)
            else:
                notice = {"systemMessage": warning + CLEAR_HINT}
        if not self.store.record(session, turn, self.clock(), previous):
            return blocked("同一会话的提交状态已变化，请重新提交后检查。")
        return notice


TOOLS = [
    {
        "name": "check_prompt",
        "description": "Codex UserPromptSubmit lifecycle hook. Called by the host before a user message; do not call from the model.",
        "inputSchema": {"type": "object", "properties": {"session_id": {"type": "string"}, "turn_id": {"type": "string"}}, "required": ["session_id", "turn_id"], "additionalProperties": False},
    },
    {
        "name": "configure",
        "description": "Ask the user to choose CacheGate remind or confirm mode in a native form and persist their choice. Call only when the user requests a settings change.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
]


class Server:
    """Newline-delimited MCP stdio; dispatch independently of elicitation replies."""

    def __init__(self, gate):
        self.gate = gate
        self.output_lock = threading.Lock()
        self.lock = threading.Lock()
        self.pending = {}
        self.calls = {}
        self.form_supported = False
        self.closed = threading.Event()

    def send(self, message):
        with self.output_lock:
            print(json.dumps({"jsonrpc": "2.0", **message}, ensure_ascii=False), flush=True)

    def ask(self, message, field, choices, cancelled):
        if not self.form_supported or cancelled.is_set():
            return None
        request_id = "cachegate-" + uuid.uuid4().hex
        response = queue.Queue(maxsize=1)
        with self.lock:
            self.pending[request_id] = response
        try:
            self.send({"id": request_id, "method": "elicitation/create", "params": {
                "mode": "form", "message": message,
                "requestedSchema": {"type": "object", "properties": {field: {
                    "type": "string", "oneOf": [{"const": value, "title": title} for value, title in choices]
                }}, "required": [field]},
            }})
            while not self.closed.is_set() and not cancelled.is_set():
                try:
                    reply = response.get(timeout=0.1)
                except queue.Empty:
                    continue
                result = reply.get("result", {})
                content = result.get("content")
                if result.get("action") == "accept" and isinstance(content, dict):
                    return content.get(field)
                return None
            return None
        finally:
            with self.lock:
                self.pending.pop(request_id, None)

    def call(self, request, cancelled):
        try:
            params = request.get("params", {})
            ask = lambda message, field, choices: self.ask(message, field, choices, cancelled)
            name = params.get("name")
            if name == "configure":
                output = self.gate.configure(ask)
            elif name == "check_prompt":
                args = params.get("arguments", {})
                output = self.gate.check(args.get("session_id"), args.get("turn_id"), ask)
            else:
                self.send({"id": request["id"], "error": {"code": -32602, "message": "Unknown tool"}})
                return
        except (OSError, sqlite3.Error, ValueError, TypeError, AttributeError) as error:
            # Return a valid blocking hook result, not an MCP error (which Codex ignores).
            output = blocked(f"本地检查失败（{type(error).__name__}），请检查 CacheGate 配置和数据目录后重试。")
        finally:
            with self.lock:
                self.calls.pop(request["id"], None)
        self.send({"id": request["id"], "result": {
            "content": [{"type": "text", "text": json.dumps(output, ensure_ascii=False)}],
            "structuredContent": output, "isError": False,
        }})

    def run(self):
        try:
            for line in sys.stdin:
                try:
                    request = json.loads(line)
                except json.JSONDecodeError:
                    self.send({"id": None, "error": {"code": -32700, "message": "Invalid JSON"}})
                    continue
                if not isinstance(request, dict):
                    continue
                method = request.get("method")
                request_id = request.get("id")
                if method is None:
                    with self.lock:
                        pending = self.pending.get(request_id)
                        if pending and pending.empty():
                            pending.put_nowait(request)
                elif method == "initialize":
                    params = request.get("params", {})
                    capabilities = params.get("capabilities", {}).get("elicitation")
                    self.form_supported = isinstance(capabilities, dict) and (not capabilities or "form" in capabilities)
                    self.send({"id": request_id, "result": {
                        "protocolVersion": "2025-11-25", "capabilities": {"tools": {}},
                        "serverInfo": {"name": "cachegate", "version": "0.1.0"},
                        "instructions": "check_prompt is for lifecycle hooks only. Use configure only when the user asks to change CacheGate settings; the user's native form response controls the setting.",
                    }})
                elif method == "ping":
                    self.send({"id": request_id, "result": {}})
                elif method == "tools/list":
                    self.send({"id": request_id, "result": {"tools": TOOLS}})
                elif method == "tools/call":
                    cancelled = threading.Event()
                    with self.lock:
                        self.calls[request_id] = cancelled
                    threading.Thread(target=self.call, args=(request, cancelled), daemon=True).start()
                elif method == "notifications/cancelled":
                    with self.lock:
                        cancelled = self.calls.get(request.get("params", {}).get("requestId"))
                        if cancelled:
                            cancelled.set()
                elif request_id is not None:
                    self.send({"id": request_id, "error": {"code": -32601, "message": "Method not found"}})
        finally:
            self.closed.set()


def main():
    parser = argparse.ArgumentParser(description="CacheGate: Codex 消息提交前的 30 分钟间隔检查")
    parser.add_argument("--data-dir", type=Path, default=None, help="覆盖本地状态目录")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve", help="运行 MCP stdio 服务")
    config = sub.add_parser("config", help="查看或修改全局模式")
    config.add_argument("--mode", choices=MODES)
    args = parser.parse_args()
    store = Store(args.data_dir or data_dir())
    if args.command == "serve":
        Server(Gate(store)).run()
    else:
        if args.mode:
            store.set_mode(args.mode)
        print(json.dumps({"mode": store.mode(), "threshold_seconds": THRESHOLD_SECONDS, "state_path": str(store.path)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
