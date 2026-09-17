#!/usr/bin/env python3
"""On-demand Codex command hook. Python 3.10+, standard library only."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import sys
import time


THRESHOLD_SECONDS = 30 * 60
MODES = ("remind", "confirm")
DEFAULT_MODE = "remind"
LABELS = {"remind": "仅提醒", "confirm": "超时确认"}
HELP_HINT = "切换模式等功能见 cachegate help。"
ALLOW_HINT = "提交 cachegate allow 后，原样重发最后一条被拦截的消息。"
HELP_TEXT = """CacheGate 是一个轻量级 Codex 插件，帮助你留意长时间中断后继续会话的上下文开销。
提示词缓存（KV 缓存）可能在长时间空闲后失效，重新处理长上下文可能增加耗时和输入成本。
它在发送前检查同一会话的消息间隔，超过 30 分钟时默认仅提醒；也可切换为拦截，由你决定继续发送或手动 /clear 新建会话。

在 Codex 输入框单独提交以下命令：

cachegate remind   超过 30 分钟时仅提醒
cachegate confirm  超过 30 分钟时拦截
cachegate status   查看当前模式

仅在超时拦截后使用：
cachegate allow    允许原样重发最近被拦截的消息一次，不会自动发送
cachegate cancel   撤销单次许可和待确认记录

模式在本机共享，下次提交生效。单次许可只用于对应会话和消息文本。
不想发送时直接不重发即可，无需 cancel。也可手动 /clear 新建会话。
时间阈值无法保证 KV 缓存命中，/clear 不会恢复过期缓存。"""


def data_dir() -> Path:
    override = os.environ.get("CACHEGATE_DATA_DIR")
    if override:
        return Path(override).expanduser()
    return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser() / "cachegate"


class Store:
    def __init__(self, directory: Path):
        self.path = directory / "state.sqlite3"

    @contextmanager
    def transaction(self):
        """Serialize short local decisions and close every connection explicitly."""
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=5)
        try:
            with db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                db.execute("""CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, last_at REAL NOT NULL,
                    turn_id TEXT NOT NULL)""")
                db.execute("""CREATE TABLE IF NOT EXISTS pending (
                    session_id TEXT PRIMARY KEY, digest TEXT NOT NULL,
                    approved INTEGER NOT NULL DEFAULT 0)""")
                yield db
        finally:
            db.close()

    def mode(self):
        with self.transaction() as db:
            return read_mode(db)

    def set_mode(self, mode):
        if mode not in MODES:
            raise ValueError("mode must be remind or confirm")
        with self.transaction() as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES ('mode', ?)", (mode,))

    def last(self, session):
        with self.transaction() as db:
            return db.execute("SELECT last_at, turn_id FROM sessions WHERE id = ?", (session,)).fetchone()


def read_mode(db):
    row = db.execute("SELECT value FROM settings WHERE key = 'mode'").fetchone()
    if row and row[0] not in MODES:
        raise ValueError("Invalid saved mode")
    return row[0] if row else DEFAULT_MODE


def stopped(message):
    return {"continue": False, "stopReason": "CacheGate：" + message}


def blocked(message):
    return {"decision": "block", "reason": "CacheGate：" + message}


def control_command(prompt):
    # Only a dedicated single-line command is consumed. Prose and code blocks pass on.
    match = re.fullmatch(r"cachegate(?:[ \t]+(\S+))?", prompt.strip())
    return (match[1] or "help") if match else None


class Gate:
    def __init__(self, store, clock=time.time):
        self.store = store
        self.clock = clock

    def handle(self, payload):
        if not isinstance(payload, dict):
            raise ValueError("Expected a hook object")
        if payload.get("hook_event_name") != "UserPromptSubmit":
            raise ValueError("Expected UserPromptSubmit")
        prompt = payload.get("prompt")
        if not isinstance(prompt, str):
            raise ValueError("Expected prompt text")
        command = control_command(prompt)
        if command == "help":
            # Help remains available even if the state file needs repair.
            return {"continue": False, "stopReason": HELP_TEXT}
        if command and command not in (*MODES, "status", "allow", "cancel"):
            return stopped("未知命令。" + HELP_HINT)
        session, turn = payload.get("session_id"), payload.get("turn_id")
        if not all(isinstance(value, str) and value for value in (session, turn)):
            raise ValueError("Missing session or turn id")
        with self.store.transaction() as db:
            if command:
                return self.control(db, command, session)
            return self.check(db, payload, session, turn)

    def control(self, db, command, session):
        if command in MODES:
            db.execute("INSERT OR REPLACE INTO settings VALUES ('mode', ?)", (command,))
            return stopped(f"已切换为{LABELS[command]}。")
        if command == "status":
            return stopped(f"当前模式：{LABELS[read_mode(db)]}；阈值：超过 30 分钟。")
        if command == "allow":
            result = db.execute("UPDATE pending SET approved = 1 WHERE session_id = ?", (session,))
            if not result.rowcount:
                return stopped("本会话没有待确认的消息。" + HELP_HINT)
            return stopped("已允许最后一条被拦截的消息，请以原模型原样重发。")
        db.execute("DELETE FROM pending WHERE session_id = ?", (session,))
        return stopped("已撤销本会话的单次许可。")

    def check(self, db, payload, session, turn):
        mode = read_mode(db)
        now = self.clock()
        if not math.isfinite(now):
            raise ValueError("Invalid clock")
        # The hook exposes text and model, not attachment bytes or the final API body.
        identity = json.dumps([payload["prompt"], payload.get("model")], ensure_ascii=False)
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        previous = db.execute(
            "SELECT last_at FROM sessions WHERE id = ?", (session,)
        ).fetchone()
        # A running turn can receive multiple submissions, even with identical text.
        gap = now - previous[0] if previous else None
        if gap is not None and not math.isfinite(gap):
            raise ValueError("Invalid stored timestamp")
        stale = gap is not None and (gap > THRESHOLD_SECONDS or gap < 0)
        notice = {}
        if stale:
            elapsed = "已超过 30 分钟" if gap > 0 else "系统时间回退"
            if mode == "confirm":
                pending = db.execute(
                    "SELECT digest, approved FROM pending WHERE session_id = ?", (session,)
                ).fetchone()
                if pending != (digest, 1):
                    db.execute("INSERT OR REPLACE INTO pending VALUES (?, ?, 0)", (session, digest))
                    if pending is not None and pending[1] == 1:
                        return blocked("消息文本或模型与许可不匹配，本次已拦截。" + ALLOW_HINT)
                    return blocked(f"{elapsed}，本次已拦截。" + ALLOW_HINT)
            else:
                notice = {"systemMessage": f"CacheGate：{elapsed}，本次继续发送。" + HELP_HINT}
        db.execute(
            "INSERT OR REPLACE INTO sessions (id, last_at, turn_id) VALUES (?, ?, ?)",
            (session, now, turn),
        )
        db.execute("DELETE FROM pending WHERE session_id = ?", (session,))
        return notice


def run_hook(directory, input_stream=sys.stdin):
    try:
        return Gate(Store(directory)).handle(json.load(input_stream))
    except (OSError, sqlite3.Error, ValueError, TypeError, OverflowError):
        # A valid blocking result avoids Codex treating a process failure as a skipped hook.
        return blocked("本地检查失败。" + HELP_HINT)


def main():
    parser = argparse.ArgumentParser(description="CacheGate：Codex 消息提交前的 30 分钟间隔检查")
    parser.add_argument("--data-dir", type=Path, default=None)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("hook", help="处理 stdin 中的一次 Codex hook 事件")
    config = sub.add_parser("config", help="兼容原有终端配置入口")
    config.add_argument("--mode", choices=MODES)
    args = parser.parse_args()
    directory = args.data_dir or data_dir()
    if args.command == "hook":
        print(json.dumps(run_hook(directory), ensure_ascii=False))
    else:
        store = Store(directory)
        if args.mode:
            store.set_mode(args.mode)
        print(json.dumps({"mode": store.mode(), "threshold_seconds": THRESHOLD_SECONDS,
                          "state_path": str(store.path)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
