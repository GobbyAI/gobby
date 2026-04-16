"""Shared helpers for rendering and identifying Gobby hook commands."""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any

from gobby.utils.native_bin import local_native_bin_path

_GOBBY_OWNED_MARKER = "--gobby-owned"


def is_gobby_hook_command(command: str) -> bool:
    """Return whether a command string belongs to Gobby-managed hooks."""
    return _GOBBY_OWNED_MARKER in command or "hook_dispatcher.py" in command


def config_contains_gobby_hook(node: Any) -> bool:
    """Recursively detect Gobby-managed hook commands in a config fragment."""
    if isinstance(node, str):
        return is_gobby_hook_command(node)

    if isinstance(node, list):
        return any(config_contains_gobby_hook(item) for item in node)

    if isinstance(node, dict):
        for field in ("command", "cmd", "script"):
            value = node.get(field)
            if isinstance(value, str) and is_gobby_hook_command(value):
                return True
        return any(config_contains_gobby_hook(value) for value in node.values())

    return False


def build_hook_command_prefix(
    hooks_dir: Path,
    *,
    ghook_bin: str | None = None,
    uv_bin: str | None = None,
) -> str:
    """Build the shared command prefix for hook templates."""
    resolved_ghook = ghook_bin
    if resolved_ghook is None:
        local_ghook = local_native_bin_path("ghook")
        if local_ghook.exists():
            resolved_ghook = str(local_ghook)

    if resolved_ghook:
        return f"{shlex.quote(resolved_ghook)} {_GOBBY_OWNED_MARKER}"

    resolved_uv = uv_bin if uv_bin is not None else "uv"
    dispatcher = hooks_dir / "hook_dispatcher.py"
    return f"{shlex.quote(resolved_uv)} run {shlex.quote(str(dispatcher.resolve()))}"


def build_hook_command(
    cli_name: str,
    hook_type: str,
    hooks_dir: Path,
    *,
    ghook_bin: str | None = None,
    uv_bin: str | None = None,
) -> str:
    """Build the full hook command for a CLI hook type."""
    prefix = build_hook_command_prefix(hooks_dir, ghook_bin=ghook_bin, uv_bin=uv_bin)
    return f"{prefix} --cli={cli_name} --type={hook_type}"


def rewrite_hook_template_commands(
    hooks_config: dict[str, Any],
    *,
    cli_name: str,
    hooks_dir: Path,
    ghook_bin: str | None = None,
    uv_bin: str | None = None,
) -> dict[str, Any]:
    """Rewrite all template command hooks to the current preferred hook command."""
    hooks = hooks_config.get("hooks")
    if not isinstance(hooks, dict):
        return hooks_config

    for hook_type, hook_config in hooks.items():
        command = build_hook_command(
            cli_name,
            hook_type,
            hooks_dir,
            ghook_bin=ghook_bin,
            uv_bin=uv_bin,
        )
        _rewrite_commands(hook_config, command)

    return hooks_config


def _rewrite_commands(node: Any, command: str) -> None:
    """Recursively replace command entries inside a hook template fragment."""
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
