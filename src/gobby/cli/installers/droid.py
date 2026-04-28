"""Factory Droid CLI installation for Gobby hooks."""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import time
from copy import deepcopy
from pathlib import Path
from shutil import copy2
from typing import Any

from gobby.adapters.droid_contract import DROID_PASCAL_HOOK_NAMES
from gobby.cli.utils import get_install_dir

from .hook_commands import config_contains_gobby_hook, rewrite_hook_template_commands
from .mcp_config import configure_mcp_server_json, remove_mcp_server_json
from .shared import (
    clean_project_hooks,
    install_cli_content,
    install_global_hooks,
    install_shared_content,
)

logger = logging.getLogger(__name__)

_EMPTY_PROJECT_HOOKS_WARNING = (
    "Project-level hooks config at {path} is empty; it overrides user-level and will silently "
    "disable Gobby droid hooks. Add Gobby's hook entries to the project-level file or remove "
    "the empty hooks key."
)


def _global_hooks_dir() -> Path:
    """Return the shared Gobby hook helper directory."""
    return Path(os.environ.get("GOBBY_HOOKS_DIR", str(Path.home() / ".gobby" / "hooks")))


def _factory_dir(project_path: Path, mode: str) -> Path:
    """Return the Factory config directory for the requested install scope."""
    if mode == "global":
        return Path.home() / ".factory"
    return project_path / ".factory"


def _droid_hooks_file(project_path: Path, mode: str) -> Path:
    """Return Droid's hooks.json path, honoring Gobby test/operator overrides."""
    if override := os.environ.get("GOBBY_DROID_HOOKS_FILE"):
        return Path(override).expanduser()
    if hooks_dir := os.environ.get("GOBBY_HOOKS_DIR"):
        return Path(hooks_dir).expanduser() / "hooks.json"
    return _factory_dir(project_path, mode) / "hooks" / "hooks.json"


def _droid_mcp_file(project_path: Path, mode: str) -> Path:
    """Return Droid's MCP config path."""
    return _factory_dir(project_path, mode) / "mcp.json"


def _load_json_file(path: Path) -> dict[str, Any]:
    """Load a JSON object from path, returning an empty object when absent."""
    if not path.exists():
        return {}
    with open(path) as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def _atomic_write_json(path: Path, data: dict[str, Any]) -> str | None:
    """Atomically write JSON and return the backup path when an existing file was backed up."""
    path.parent.mkdir(parents=True, exist_ok=True)
    backup_path: Path | None = None
    if path.exists():
        timestamp = int(time.time())
        backup_path = path.parent / f"{path.name}.{timestamp}.backup"
        copy2(path, backup_path)

    fd, temp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp", prefix=f"{path.stem}_")
    try:
        with os.fdopen(fd, "w") as f:
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


def _load_droid_hooks_template(hooks_dir: Path) -> dict[str, Any]:
    """Load and rewrite the bundled Droid hooks template."""
    template_path = get_install_dir() / "droid" / "hooks-template.json"
    if not template_path.exists():
        raise FileNotFoundError(f"Missing hooks template: {template_path}")

    with open(template_path) as f:
        template = json.load(f)
    if not isinstance(template, dict):
        raise ValueError(f"{template_path} must contain a JSON object")

    rewrite_hook_template_commands(template, cli_name="droid", hooks_dir=hooks_dir)
    return template


def _merge_gobby_hooks(
    existing_settings: dict[str, Any],
    gobby_settings: dict[str, Any],
) -> dict[str, Any]:
    """Merge Droid Gobby hook entries while preserving non-Gobby entries."""
    updated = deepcopy(existing_settings)
    hooks = updated.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        hooks = {}
        updated["hooks"] = hooks

    gobby_hooks = gobby_settings.get("hooks", {})
    if not isinstance(gobby_hooks, dict):
        raise ValueError("Droid hooks template does not contain a hooks object")

    for hook_type in DROID_PASCAL_HOOK_NAMES:
        hook_config = gobby_hooks.get(hook_type)
        if not isinstance(hook_config, list) or not hook_config:
            raise ValueError(f"Droid hooks template missing hook type: {hook_type}")

        current_config = hooks.get(hook_type)
        preserved: list[Any] = []
        if isinstance(current_config, list):
            preserved = [
                deepcopy(entry) for entry in current_config if not config_contains_gobby_hook(entry)
            ]
        hooks[hook_type] = preserved + deepcopy(hook_config)

    return updated


def _remove_gobby_hooks(settings: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Remove Gobby-managed Droid hook entries while preserving unrelated hooks."""
    updated = deepcopy(settings)
    hooks = updated.get("hooks")
    if not isinstance(hooks, dict):
        return updated, []

    removed: list[str] = []
    for hook_type in DROID_PASCAL_HOOK_NAMES:
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


def _warn_empty_project_hooks(project_path: Path) -> list[str]:
    """Warn when project Factory settings contain an empty hooks override."""
    warnings: list[str] = []
    for settings_path in (
        project_path / ".factory" / "settings.json",
        project_path / ".factory" / "settings.local.json",
    ):
        if not settings_path.exists():
            continue
        try:
            settings = _load_json_file(settings_path)
        except (json.JSONDecodeError, OSError, ValueError) as exc:
            logger.warning("Could not inspect Droid project settings at %s: %s", settings_path, exc)
            continue
        if settings.get("hooks") in ({}, []):
            warning = _EMPTY_PROJECT_HOOKS_WARNING.format(path=settings_path)
            print(warning, file=sys.stderr)
            warnings.append(warning)
    return warnings


def install_droid(project_path: Path, mode: str = "global") -> dict[str, Any]:
    """Install Gobby integration for Factory Droid hooks and MCP registration."""
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
        "error": None,
    }

    hooks_dir = _global_hooks_dir()
    droid_path = _factory_dir(project_path, mode)
    hooks_file = _droid_hooks_file(project_path, mode)
    hooks_file.parent.mkdir(parents=True, exist_ok=True)
    droid_path.mkdir(parents=True, exist_ok=True)

    try:
        install_global_hooks()
        if mode == "global":
            cleaned = clean_project_hooks(project_path / ".factory" / "hooks" / "hooks.json")
            if cleaned:
                result["project_hooks_cleaned"] = cleaned
    except OSError as exc:
        result["error"] = f"Failed to install hook helper files: {exc}"
        return result

    warnings = _warn_empty_project_hooks(project_path)
    if warnings:
        result["warnings"] = warnings

    try:
        gobby_settings = _load_droid_hooks_template(hooks_dir)
        existing_settings = _load_json_file(hooks_file)
        updated_settings = _merge_gobby_hooks(existing_settings, gobby_settings)
    except (FileNotFoundError, ValueError, json.JSONDecodeError, OSError) as exc:
        result["error"] = f"Failed to prepare Droid hooks: {exc}"
        return result

    hooks_installed.extend(DROID_PASCAL_HOOK_NAMES)
    hooks_changed = updated_settings != existing_settings
    if hooks_changed or not hooks_file.exists():
        try:
            backup_path = _atomic_write_json(hooks_file, updated_settings)
            if backup_path:
                result["backup_path"] = backup_path
        except OSError as exc:
            result["error"] = f"Failed to write Droid hooks: {exc}"
            return result
    else:
        result["already_configured"] = True

    shared = install_shared_content(
        droid_path if mode == "project" else project_path / ".factory", project_path
    )
    cli = install_cli_content("droid", droid_path)
    result["commands_installed"] = cli.get("commands", [])
    result["plugins_installed"] = shared.get("plugins", [])

    mcp_result = configure_mcp_server_json(
        _droid_mcp_file(project_path, mode),
        extra_server_fields={"type": "stdio"},
    )
    if mcp_result["success"]:
        result["mcp_configured"] = mcp_result.get("added", False) or mcp_result.get(
            "updated", False
        )
        result["mcp_already_configured"] = mcp_result.get("already_configured", False)
    else:
        logger.warning("Failed to configure Droid MCP server: %s", mcp_result["error"])

    if result["already_configured"] and not result["mcp_already_configured"]:
        result["already_configured"] = False

    result["success"] = True
    return result


def uninstall_droid(project_path: Path, mode: str = "global") -> dict[str, Any]:
    """Remove Gobby integration from Factory Droid hooks and MCP config."""
    hooks_removed: list[str] = []
    files_removed: list[str] = []
    result: dict[str, Any] = {
        "success": False,
        "hooks_removed": hooks_removed,
        "files_removed": files_removed,
        "mcp_removed": False,
        "error": None,
    }

    hooks_file = _droid_hooks_file(project_path, mode)
    if hooks_file.exists():
        try:
            existing_settings = _load_json_file(hooks_file)
            updated_settings, removed = _remove_gobby_hooks(existing_settings)
        except (json.JSONDecodeError, OSError, ValueError) as exc:
            result["error"] = f"Failed to read Droid hooks: {exc}"
            return result

        hooks_removed.extend(removed)
        if updated_settings != existing_settings:
            try:
                backup_path = _atomic_write_json(hooks_file, updated_settings)
                if backup_path:
                    result["backup_path"] = backup_path
            except OSError as exc:
                result["error"] = f"Failed to write Droid hooks: {exc}"
                return result

    mcp_result = remove_mcp_server_json(_droid_mcp_file(project_path, mode))
    if mcp_result["success"]:
        result["mcp_removed"] = mcp_result.get("removed", False)
    else:
        logger.warning("Failed to remove Droid MCP server: %s", mcp_result["error"])

    result["success"] = True
    return result
