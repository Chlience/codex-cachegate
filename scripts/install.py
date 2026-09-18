#!/usr/bin/env python3
"""Prepare a new personal plugin and install it using Codex's plugin CLI."""

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "plugins" / "cachegate"


def prepare_plugin(destination, state_dir):
    """Use the installed hook's own root and an explicit, stable data directory."""
    destination = destination.resolve()
    if destination.exists():
        raise FileExistsError(f"已存在插件目录，不覆盖：{destination}")
    destination.mkdir(parents=True)
    for relative in [".codex-plugin/plugin.json", "hooks/hooks.json", "scripts/cachegate.py"]:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(SOURCE / relative, target)
    shutil.copy2(ROOT / "README.md", destination / "README.md")
    config = json.loads((destination / "hooks/hooks.json").read_text(encoding="utf-8"))
    for event, groups in config["hooks"].items():
        for group in groups:
            for handler in group["hooks"]:
                handler["command"] = (
                    shlex.quote(sys.executable) + ' "${PLUGIN_ROOT}/scripts/cachegate.py" '
                    + shlex.join(["--data-dir", str(state_dir.resolve()), "hook", "--event", event])
                )
    (destination / "hooks/hooks.json").write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def register_personal(home_dir, state_dir):
    marketplace = home_dir / ".agents" / "plugins" / "marketplace.json"
    original = marketplace.read_bytes() if marketplace.exists() else None
    catalog = json.loads(original) if original is not None else {
        "name": "personal", "interface": {"displayName": "Personal"}, "plugins": []
    }
    if not isinstance(catalog, dict) or not re.fullmatch(r"[A-Za-z0-9_-]+", str(catalog.get("name", ""))):
        raise ValueError(f"marketplace 名称无效：{marketplace}")
    entries = catalog.get("plugins")
    if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
        raise ValueError(f"marketplace plugins 必须为对象数组：{marketplace}")
    if any(entry.get("name") == "cachegate" for entry in entries):
        raise FileExistsError(f"已有 cachegate 条目，不覆盖：{marketplace}")
    destination = home_dir / "plugins" / "cachegate"
    prepare_plugin(destination, state_dir)
    entries.append({
        "name": "cachegate", "source": {"source": "local", "path": "./plugins/cachegate"},
        "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
        "category": "Productivity",
    })
    marketplace.parent.mkdir(parents=True, exist_ok=True)
    current = marketplace.read_bytes() if marketplace.exists() else None
    if current != original:
        raise RuntimeError(f"marketplace 已被其他进程修改，未写入；已准备的插件保留于 {destination}")
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=marketplace.parent, prefix="cachegate-", suffix=".json", delete=False) as handle:
        json.dump(catalog, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(marketplace)
    return catalog["name"], destination, marketplace


def main():
    parser = argparse.ArgumentParser(description="准备并安装新的 CacheGate 个人插件；不会覆盖已有 CacheGate。")
    parser.add_argument("--prepare-only", action="store_true", help="仅准备个人市场条目，暂不调用 Codex 安装")
    args = parser.parse_args()
    if not args.prepare_only and shutil.which("codex") is None:
        parser.error("找不到 codex，请先安装 Codex CLI。")
    codex_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()
    state_dir = Path(os.environ.get("CACHEGATE_DATA_DIR", str(codex_home / "cachegate"))).expanduser()
    try:
        name, destination, marketplace = register_personal(Path.home(), state_dir)
    except (OSError, ValueError, RuntimeError) as error:
        parser.exit(1, f"安装未完成：{error}\n")
    selector = f"cachegate@{name}"
    print(f"插件源目录：{destination}\n个人市场：{marketplace}", flush=True)
    if not args.prepare_only:
        # No shell and no implicit hook trust. The user reviews the hook in /hooks.
        result = subprocess.run(["codex", "plugin", "add", selector])
        if result.returncode:
            parser.exit(result.returncode, f"Codex 安装失败。源文件和市场条目已保留；解决错误后重试：codex plugin add {selector}\n")
    else:
        print(f"准备完成，安装命令：codex plugin add {selector}")
    print("安装后新建会话，在 /hooks 审阅并信任 CacheGate，然后输入 cachegate help。")


if __name__ == "__main__":
    main()
