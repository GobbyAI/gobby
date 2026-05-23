"""Grok CLI installation for Gobby hooks."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from shutil import copy2
from typing import Any

from gobby.cli.utils import get_install_dir

from .hook_commands import build_hook_command
from .shared import install_global_hooks, install_shared_content

logger = logging.getLogger(__name__)

_HOOK_TYPE_MAP = {
    "SessionStart": "session_start",
    "SessionEnd": "session_end",
    "UserPromptSubmit": "user_prompt_submit",
    "PreToolUse": "pre_tool_use",
    "PostToolUse": "post_tool_use",
    "PostToolUseFailure": "post_tool_use_failure",
    "PreCompact": "pre_compact",
    "Stop": "stop",
    "Notification": "notification",
}


def install_grok(project_path: Path, mode: str = "global") -> dict[str, Any]:
    """Install Gobby integration for Grok CLI native hook files."""
    hooks_installed: list[str] = []
    result: dict[str, Any] = {
        "success": False,
        "hooks_installed": hooks_installed,
        "workflows_installed": [],
        "commands_installed": [],
        "mcp_configured": False,
        "mcp_already_configured": False,
        "error": None,
    }

    install_dir = get_install_dir()
    source_hooks_template = install_dir / "grok" / "hooks-template.json"
    if not source_hooks_template.exists():
        result["error"] = f"Missing hooks template: {source_hooks_template}"
        return result

    hooks_dir = Path.home() / ".gobby" / "hooks"
    grok_home = Path.home() / ".grok"
    grok_hooks_dir = grok_home / "hooks"
    gobby_hook_file = grok_hooks_dir / "gobby.json"

    install_global_hooks()
    grok_hooks_dir.mkdir(parents=True, exist_ok=True)

    content_path = project_path / ".grok" if mode == "global" else project_path / ".grok"
    shared = install_shared_content(content_path, project_path)
    result["agents_installed"] = shared.get("agents", [])
    result["plugins_installed"] = shared.get("plugins", [])

    if gobby_hook_file.exists():
        backup_file = grok_hooks_dir / f"gobby.json.{int(time.time())}.backup"
        copy2(gobby_hook_file, backup_file)

    with open(source_hooks_template) as f:
        hook_config = json.load(f)

    _rewrite_grok_hook_commands(hook_config, hooks_dir)

    with open(gobby_hook_file, "w") as f:
        json.dump(hook_config, f, indent=2)

    hooks_installed.extend(hook_config.get("hooks", {}).keys())
    result["config_path"] = str(gobby_hook_file)
    result["success"] = True
    return result


def uninstall_grok(project_path: Path, mode: str = "global") -> dict[str, Any]:
    """Remove Gobby-owned Grok native hook file."""
    del project_path, mode
    hooks_removed: list[str] = []
    files_removed: list[str] = []
    result: dict[str, Any] = {
        "success": False,
        "hooks_removed": hooks_removed,
        "files_removed": files_removed,
        "mcp_removed": False,
        "error": None,
    }

    hook_file = Path.home() / ".grok" / "hooks" / "gobby.json"
    if not hook_file.exists():
        result["success"] = True
        return result

    try:
        with open(hook_file) as f:
            config = json.load(f)
    except json.JSONDecodeError:
        config = {}

    hooks = config.get("hooks")
    if isinstance(hooks, dict):
        hooks_removed.extend(str(name) for name in hooks)

    hook_file.unlink()
    files_removed.append(str(hook_file))
    result["success"] = True
    return result


def _rewrite_grok_hook_commands(hook_config: dict[str, Any], hooks_dir: Path) -> None:
    hooks = hook_config.get("hooks")
    if not isinstance(hooks, dict):
        return

    for grok_hook, native_hook in _HOOK_TYPE_MAP.items():
        if grok_hook not in hooks:
            continue
        command = build_hook_command("grok", native_hook, hooks_dir)
        _rewrite_commands(hooks[grok_hook], command)


def _rewrite_commands(node: Any, command: str) -> None:
    if isinstance(node, list):
        for item in node:
            _rewrite_commands(item, command)
        return
    if not isinstance(node, dict):
        return
    if isinstance(node.get("command"), str):
        node["command"] = command
    for value in node.values():
        _rewrite_commands(value, command)


__all__ = ["install_grok", "uninstall_grok"]
