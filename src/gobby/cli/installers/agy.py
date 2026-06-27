"""AGY CLI installation for Gobby hooks."""

from __future__ import annotations

import json
import os
import tempfile
import time
from copy import deepcopy
from pathlib import Path
from shutil import Error as ShutilError
from shutil import copy2
from typing import Any

from gobby.adapters.agy_contract import AGY_HOOK_NAMES
from gobby.agents.trust import seed_gobby_home_trust
from gobby.cli.utils import get_install_dir

from .hook_commands import config_contains_gobby_hook, rewrite_hook_template_commands
from .mcp_config import configure_mcp_server_json, remove_mcp_server_json
from .shared import install_cli_content, install_global_hooks, install_shared_content


def _global_hooks_dir() -> Path:
    return Path(os.environ.get("GOBBY_HOOKS_DIR", str(Path.home() / ".gobby" / "hooks")))


def _agy_config_dir() -> Path:
    return Path.home() / ".gemini" / "config"


def _agy_hooks_file() -> Path:
    if override := os.environ.get("GOBBY_AGY_HOOKS_FILE"):
        return Path(override).expanduser()
    return _agy_config_dir() / "hooks.json"


def _agy_mcp_file() -> Path:
    if override := os.environ.get("GOBBY_AGY_MCP_FILE"):
        return Path(override).expanduser()
    return _agy_config_dir() / "mcp_config.json"


def _load_json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def _atomic_write_json(path: Path, data: dict[str, Any]) -> str | None:
    path.parent.mkdir(parents=True, exist_ok=True)
    backup_path: Path | None = None
    if path.exists():
        timestamp = int(time.time())
        backup_path = path.parent / f"{path.name}.{timestamp}.backup"
        copy2(path, backup_path)

    fd, temp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp", prefix=f"{path.stem}_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, path)
    except Exception:
        if os.path.exists(temp_path):
            os.unlink(temp_path)
        raise
    return str(backup_path) if backup_path else None


def _load_agy_hooks_template(hooks_dir: Path) -> dict[str, Any]:
    template_path = get_install_dir() / "agy" / "hooks-template.json"
    if not template_path.exists():
        raise FileNotFoundError(f"Missing hooks template: {template_path}")
    with open(template_path, encoding="utf-8") as f:
        template = json.load(f)
    if not isinstance(template, dict):
        raise ValueError(f"{template_path} must contain a JSON object")
    rewrite_hook_template_commands(template, cli_name="agy", hooks_dir=hooks_dir)
    return template


def _merge_gobby_hooks(
    existing_settings: dict[str, Any],
    gobby_settings: dict[str, Any],
) -> dict[str, Any]:
    updated = deepcopy(existing_settings)
    hooks = updated.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        hooks = {}
        updated["hooks"] = hooks

    gobby_hooks = gobby_settings.get("hooks", {})
    if not isinstance(gobby_hooks, dict):
        raise ValueError("AGY hooks template does not contain a hooks object")

    for hook_type in AGY_HOOK_NAMES:
        hook_config = gobby_hooks.get(hook_type)
        if not isinstance(hook_config, list) or not hook_config:
            raise ValueError(f"AGY hooks template missing hook type: {hook_type}")

        current_config = hooks.get(hook_type)
        preserved: list[Any] = []
        if isinstance(current_config, list):
            preserved = [
                deepcopy(entry) for entry in current_config if not config_contains_gobby_hook(entry)
            ]
        hooks[hook_type] = preserved + deepcopy(hook_config)
    return updated


def _remove_gobby_hooks(settings: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    updated = deepcopy(settings)
    hooks = updated.get("hooks")
    if not isinstance(hooks, dict):
        return updated, []

    removed: list[str] = []
    for hook_type in AGY_HOOK_NAMES:
        hook_config = hooks.get(hook_type)
        if not isinstance(hook_config, list):
            continue
        preserved = [entry for entry in hook_config if not config_contains_gobby_hook(entry)]
        if len(preserved) == len(hook_config):
            continue
        removed.append(hook_type)
        if preserved:
            hooks[hook_type] = preserved
        else:
            del hooks[hook_type]

    if not hooks:
        del updated["hooks"]
    return updated, removed


def install_agy(project_path: Path, mode: str = "global") -> dict[str, Any]:
    """Install Gobby integration for AGY hooks and MCP registration."""
    hooks_installed: list[str] = []
    result: dict[str, Any] = {
        "success": False,
        "hooks_installed": hooks_installed,
        "workflows_installed": [],
        "commands_installed": [],
        "agents_installed": [],
        "plugins_installed": [],
        "mcp_configured": False,
        "mcp_already_configured": False,
        "already_configured": False,
        "trust": None,
        "error": None,
    }
    if mode != "global":
        result["error"] = "AGY integration only supports global install mode"
        return result

    hooks_dir = _global_hooks_dir()
    agy_config_dir = _agy_config_dir()
    hooks_file = _agy_hooks_file()
    agy_config_dir.mkdir(parents=True, exist_ok=True)

    try:
        install_global_hooks()
        gobby_settings = _load_agy_hooks_template(hooks_dir)
        existing_settings = _load_json_file(hooks_file)
        updated_settings = _merge_gobby_hooks(existing_settings, gobby_settings)
    except (FileNotFoundError, ValueError, json.JSONDecodeError, OSError) as exc:
        result["error"] = f"Failed to prepare AGY hooks: {exc}"
        return result

    hooks_installed.extend(AGY_HOOK_NAMES)
    hooks_changed = updated_settings != existing_settings
    if hooks_changed or not hooks_file.exists():
        try:
            backup_path = _atomic_write_json(hooks_file, updated_settings)
            if backup_path:
                result["backup_path"] = backup_path
        except OSError as exc:
            result["error"] = f"Failed to write AGY hooks: {exc}"
            return result
    else:
        result["already_configured"] = True

    try:
        shared = install_shared_content(project_path / ".gemini", project_path)
        cli = install_cli_content("agy", agy_config_dir)
    except (OSError, ShutilError) as exc:
        result["error"] = f"Failed to install AGY shared content: {exc}"
        return result
    result["commands_installed"] = cli.get("commands", [])
    result["plugins_installed"] = shared.get("plugins", [])

    try:
        mcp_result = configure_mcp_server_json(
            _agy_mcp_file(),
            extra_server_fields={"type": "stdio"},
        )
    except OSError as exc:
        result["error"] = f"Failed to configure AGY MCP server: {exc}"
        return result
    if mcp_result["success"]:
        result["mcp_configured"] = mcp_result.get("added", False) or mcp_result.get(
            "updated", False
        )
        result["mcp_already_configured"] = mcp_result.get("already_configured", False)
    else:
        result["error"] = f"Failed to configure AGY MCP server: {mcp_result.get('error')}"
        return result

    if result["already_configured"] and not result["mcp_already_configured"]:
        result["already_configured"] = False

    try:
        trust_result = seed_gobby_home_trust("agy")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        result["error"] = f"Failed to seed AGY trust: {exc}"
        return result
    result["trust"] = trust_result
    if not trust_result.get("success"):
        errors = trust_result.get("errors") or []
        detail = "; ".join(str(error) for error in errors) if errors else "unknown error"
        result["error"] = f"Failed to seed AGY trust: {detail}"
        return result
    result["success"] = True
    return result


def uninstall_agy(project_path: Path, mode: str = "global") -> dict[str, Any]:
    """Remove Gobby integration from AGY hooks and MCP config."""
    del project_path
    hooks_removed: list[str] = []
    files_removed: list[str] = []
    result: dict[str, Any] = {
        "success": False,
        "hooks_removed": hooks_removed,
        "files_removed": files_removed,
        "mcp_removed": False,
        "error": None,
    }
    if mode != "global":
        result["error"] = "AGY integration only supports global uninstall mode"
        return result

    hooks_file = _agy_hooks_file()
    if hooks_file.exists():
        try:
            existing_settings = _load_json_file(hooks_file)
            updated_settings, removed = _remove_gobby_hooks(existing_settings)
        except (json.JSONDecodeError, OSError, ValueError) as exc:
            result["error"] = f"Failed to read AGY hooks: {exc}"
            return result

        hooks_removed.extend(removed)
        if updated_settings != existing_settings:
            try:
                backup_path = _atomic_write_json(hooks_file, updated_settings)
                if backup_path:
                    result["backup_path"] = backup_path
            except OSError as exc:
                result["error"] = f"Failed to write AGY hooks: {exc}"
                return result

    mcp_result = remove_mcp_server_json(_agy_mcp_file())
    if mcp_result["success"]:
        result["mcp_removed"] = mcp_result.get("removed", False)
    else:
        result["error"] = f"Failed to remove AGY MCP server: {mcp_result.get('error')}"
        return result

    result["success"] = True
    return result
