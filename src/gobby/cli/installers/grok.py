"""Grok CLI installation for Gobby hooks."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from shutil import copy2
from typing import Any, Final

import tomlkit
from tomlkit.items import Table
from tomlkit.toml_document import TOMLDocument

from gobby.cli.utils import get_install_dir
from gobby.install.bin_freshness_models import is_at_least_version
from gobby.utils.deps import get_ghook_version

from .hook_commands import rewrite_hook_template_commands, set_gobby_hook_timeouts
from .shared import install_global_hooks, install_shared_content

logger = logging.getLogger(__name__)

_MIN_GHOOK_VERSION_FOR_GROK: Final[str] = "0.5.0"


def _grok_ghook_support_error() -> str | None:
    """Return an actionable error when ghook cannot route native Grok hooks."""
    installed = get_ghook_version()
    if is_at_least_version(installed, _MIN_GHOOK_VERSION_FOR_GROK):
        return None

    if installed is None:
        detail = "the installed version could not be determined"
    else:
        detail = f"found {installed!r}"
    return (
        f"Grok hooks require ghook >= {_MIN_GHOOK_VERSION_FOR_GROK}; {detail}. "
        "Run `gobby update` and retry."
    )


def install_grok(
    project_path: Path,
    mode: str = "global",
    *,
    hook_timeout_seconds: int = 120,
) -> dict[str, Any]:
    """Install Gobby integration for Grok CLI native hook files."""
    hooks_installed: list[str] = []
    result: dict[str, Any] = {
        "success": False,
        "hooks_installed": hooks_installed,
        "workflows_installed": [],
        "commands_installed": [],
        "mcp_configured": False,
        "mcp_already_configured": False,
        "grok_claude_hooks_disabled": False,
        "grok_claude_hooks_already_disabled": False,
        "grok_config_backup_path": None,
        "messages": [],
        "error": None,
    }

    if support_error := _grok_ghook_support_error():
        result["error"] = support_error
        return result

    install_dir = get_install_dir()
    source_hooks_template = install_dir / "grok" / "hooks-template.json"
    if not source_hooks_template.exists():
        result["error"] = f"Missing hooks template: {source_hooks_template}"
        return result

    hooks_dir = Path.home() / ".gobby" / "hooks"
    grok_home = Path.home() / ".grok"
    grok_hooks_dir = grok_home / "hooks"
    gobby_hook_file = grok_hooks_dir / "gobby.json"
    grok_config_file = grok_home / "config.toml"

    install_global_hooks()
    grok_hooks_dir.mkdir(parents=True, exist_ok=True)

    content_path = project_path / ".grok" if mode == "global" else project_path / ".grok"
    shared = install_shared_content(content_path, project_path)
    result["agents_installed"] = shared.get("agents", [])
    result["plugins_installed"] = shared.get("plugins", [])

    compat_result = _disable_claude_hook_compat(grok_config_file)
    if not compat_result["success"]:
        result["error"] = compat_result["error"]
        return result
    result["grok_config_path"] = compat_result["config_path"]
    result["grok_config_backup_path"] = compat_result["backup_path"]
    result["grok_claude_hooks_disabled"] = compat_result["disabled"]
    result["grok_claude_hooks_already_disabled"] = compat_result["already_disabled"]
    if compat_result["backup_path"]:
        result["messages"].append(f"Backed up Grok config: {compat_result['backup_path']}")
    if compat_result["disabled"]:
        result["messages"].append(
            "Disabled Grok Claude-hook compatibility in ~/.grok/config.toml; "
            "native Grok hooks remain in ~/.grok/hooks/gobby.json."
        )
    else:
        result["messages"].append(
            "Grok Claude-hook compatibility is already disabled in ~/.grok/config.toml; "
            "native Grok hooks remain in ~/.grok/hooks/gobby.json."
        )

    if gobby_hook_file.exists():
        backup_file = grok_hooks_dir / f"gobby.json.{int(time.time())}.backup"
        copy2(gobby_hook_file, backup_file)

    with open(source_hooks_template) as f:
        hook_config = json.load(f)

    rewrite_hook_template_commands(hook_config, cli_name="grok", hooks_dir=hooks_dir)
    set_gobby_hook_timeouts(hook_config, timeout=hook_timeout_seconds)

    with open(gobby_hook_file, "w") as f:
        json.dump(hook_config, f, indent=2)

    hooks_installed.extend(hook_config.get("hooks", {}).keys())
    result["config_path"] = str(gobby_hook_file)
    result["success"] = True
    return result


def _disable_claude_hook_compat(config_path: Path) -> dict[str, Any]:
    """Disable Grok's Claude hook compatibility while preserving TOML formatting."""
    result: dict[str, Any] = {
        "success": False,
        "disabled": False,
        "already_disabled": False,
        "config_path": str(config_path),
        "backup_path": None,
        "error": None,
    }

    config_path.parent.mkdir(parents=True, exist_ok=True)
    existing_text = ""
    config: TOMLDocument
    if config_path.exists():
        try:
            existing_text = config_path.read_text(encoding="utf-8")
            config = tomlkit.parse(existing_text)
        except tomlkit.exceptions.ParseError as exc:
            result["error"] = f"Failed to parse TOML {config_path}: {exc}"
            return result
        except OSError as exc:
            result["error"] = f"Failed to read {config_path}: {exc}"
            return result
    else:
        config = tomlkit.document()

    try:
        compat = _toml_table(config, "compat")
        claude = _toml_table(compat, "claude")
    except ValueError as exc:
        result["error"] = str(exc)
        return result

    if claude.get("hooks") is False:
        result["success"] = True
        result["already_disabled"] = True
        return result

    if config_path.exists():
        timestamp = int(time.time())
        backup_path = config_path.parent / f"{config_path.name}.{timestamp}.backup"
        try:
            backup_path.write_text(existing_text, encoding="utf-8")
            result["backup_path"] = str(backup_path)
        except OSError as exc:
            result["error"] = f"Failed to create backup: {exc}"
            return result

    claude["hooks"] = False

    try:
        config_path.write_text(tomlkit.dumps(config), encoding="utf-8")
    except OSError as exc:
        result["error"] = f"Failed to write {config_path}: {exc}"
        return result

    result["success"] = True
    result["disabled"] = True
    return result


def _toml_table(parent: TOMLDocument | Table, key: str) -> Table:
    value = parent.get(key)
    if isinstance(value, Table):
        return value
    if value is not None:
        raise ValueError(f"Cannot configure [compat.claude]: {key!r} is not a TOML table")
    table = tomlkit.table()
    parent[key] = table
    return table


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


__all__ = ["install_grok", "uninstall_grok"]
