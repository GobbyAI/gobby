"""
Codex CLI installation for Gobby hooks.

This module handles installing and uninstalling Gobby hook integration
for OpenAI Codex CLI via hooks.json (codex_hooks feature).
"""

import json
import logging
import os
import tempfile
import tomllib
from copy import deepcopy
from pathlib import Path
from typing import Any

import tomli_w

from gobby.cli.utils import get_install_dir

from .hook_commands import config_contains_gobby_hook, rewrite_hook_template_commands
from .mcp_config import (
    configure_mcp_server_toml,
    remove_mcp_server_toml,
    strip_mcp_tool_overrides_toml,
)
from .shared import (
    clean_project_hooks,
    install_cli_content,
    install_global_hooks,
    install_shared_content,
)

logger = logging.getLogger(__name__)


def _get_hooks_dir() -> Path:
    """Get the global hooks directory path."""
    return Path(os.environ.get("GOBBY_HOOKS_DIR", str(Path.home() / ".gobby" / "hooks")))


def _load_toml_config(content: str) -> dict[str, Any]:
    """Parse a TOML config string into a mutable dict."""
    if not content.strip():
        return {}
    parsed = tomllib.loads(content)
    if not isinstance(parsed, dict):
        return {}
    return parsed


def _insert_top_level_table(
    config: dict[str, Any], table_name: str, table_value: dict[str, Any]
) -> None:
    """Insert a new top-level table before existing table sections."""
    if table_name in config:
        config[table_name] = table_value
        return

    reordered: dict[str, Any] = {}
    inserted = False
    for key, value in config.items():
        if not inserted and isinstance(value, dict):
            reordered[table_name] = table_value
            inserted = True
        reordered[key] = value

    if not inserted:
        reordered[table_name] = table_value

    config.clear()
    config.update(reordered)


def _set_toml_value(config: dict[str, Any], key: str, value: Any) -> None:
    """Set a dotted TOML key inside a parsed config dict."""
    parts = key.split(".")
    current = config
    for index, part in enumerate(parts[:-1]):
        existing = current.get(part)
        if isinstance(existing, dict):
            current = existing
            continue

        new_table: dict[str, Any] = {}
        if index == 0:
            _insert_top_level_table(config, part, new_table)
            current = config[part]
        else:
            current[part] = new_table
            current = new_table

    current[parts[-1]] = value


def _remove_toml_key(config: dict[str, Any], key: str) -> None:
    """Remove a dotted TOML key from a parsed config dict."""
    parts = key.split(".")
    current: dict[str, Any] = config
    parents: list[tuple[dict[str, Any], str]] = []

    for part in parts[:-1]:
        child = current.get(part)
        if not isinstance(child, dict):
            return
        parents.append((current, part))
        current = child

    if parts[-1] not in current:
        return

    del current[parts[-1]]

    for parent, part in reversed(parents):
        child = parent.get(part)
        if isinstance(child, dict) and not child:
            del parent[part]
            continue
        break


def _dump_toml_config(config_path: Path, config: dict[str, Any]) -> None:
    """Write a parsed TOML config back to disk."""
    with open(config_path, "wb") as f:
        tomli_w.dump(config, f, multiline_strings=True)


def _migrate_from_notify(config: dict[str, Any], hooks_dir: Path) -> None:
    """Remove legacy notify config and clean up old notify script."""
    _remove_toml_key(config, "notify")

    # Clean up old installed notify script
    old_notify = hooks_dir / "codex" / "hook_dispatcher.py"
    if old_notify.exists():
        old_notify.unlink()
    old_notify_dir = hooks_dir / "codex"
    if old_notify_dir.exists():
        try:
            is_empty = not any(old_notify_dir.iterdir())
        except OSError:
            is_empty = False
        if is_empty:
            try:
                old_notify_dir.rmdir()
            except OSError:
                pass

def _install_hooks_json(codex_home: Path, hooks_dir: Path) -> list[str]:
    """Load hooks-template.json, substitute $HOOKS_DIR, merge into ~/.codex/hooks.json.

    Returns list of installed hook type names.
    """
    install_dir = get_install_dir()
    template_path = install_dir / "codex" / "hooks-template.json"

    if not template_path.exists():
        raise FileNotFoundError(f"Missing hooks template: {template_path}")

    template_str = template_path.read_text(encoding="utf-8")
    template_str = template_str.replace("$HOOKS_DIR", str(hooks_dir.resolve()))
    gobby_hooks_config = json.loads(template_str)
    rewrite_hook_template_commands(gobby_hooks_config, cli_name="codex", hooks_dir=hooks_dir)

    hooks_file = codex_home / "hooks.json"
    existing: dict[str, Any] = {}
    if hooks_file.exists():
        try:
            existing = json.loads(hooks_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Could not read existing hooks.json, overwriting: {e}")

    if "hooks" not in existing:
        existing["hooks"] = {}

    hooks_installed = []
    for hook_type, hook_config in gobby_hooks_config.get("hooks", {}).items():
        existing["hooks"][hook_type] = hook_config
        hooks_installed.append(hook_type)

    # Atomic write
    fd, temp_path = tempfile.mkstemp(dir=str(codex_home), suffix=".tmp", prefix="hooks_")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(existing, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, hooks_file)
    except Exception:
        if os.path.exists(temp_path):
            os.unlink(temp_path)
        raise

    return hooks_installed


def _is_gobby_hook(hook_entry: Any) -> bool:
    """Check if a hooks.json entry was installed by Gobby.

    Inspects the entry's command/args for the hook_dispatcher.py path
    rather than doing a broad string search on the JSON serialization.
    """
    return config_contains_gobby_hook(hook_entry)


def install_codex(project_path: Path, *, mode: str = "global") -> dict[str, Any]:
    """Install Codex hooks via hooks.json and configure MCP server.

    Args:
        project_path: Project root directory. Shared content (plugins)
            installs to {project_path}/.gobby/.
        mode: Installation mode. Only "global" is supported for Codex.
            Accepted for interface consistency with claude/gemini installers.

    Returns:
        Dict with installation results including success status and installed items
    """
    if mode != "global":
        logger.warning(f"Codex install: mode={mode!r} not supported, falling back to global")
    hooks_installed: list[str] = []
    files_installed: list[str] = []
    result: dict[str, Any] = {
        "success": False,
        "hooks_installed": hooks_installed,
        "files_installed": files_installed,
        "workflows_installed": [],
        "commands_installed": [],
        "agents_installed": [],
        "plugins_installed": [],
        "config_updated": False,
        "mcp_configured": False,
        "mcp_already_configured": False,
        "mcp_tools_stripped": False,
        "error": None,
    }

    codex_home = Path.home() / ".codex"
    codex_home.mkdir(parents=True, exist_ok=True)
    hooks_dir = _get_hooks_dir()

    # 1. Install shared global hooks (hook_dispatcher.py etc.)
    try:
        global_hooks = install_global_hooks()
        files_installed.extend(global_hooks)

        # Clean up project-level hooks to prevent double-firing
        cleaned = clean_project_hooks(project_path / ".codex" / "hooks.json")
        if cleaned:
            result["project_hooks_cleaned"] = cleaned
    except OSError as e:
        result["error"] = f"Failed to install global hooks: {e}"
        return result

    # 2. Install hooks.json
    try:
        installed_hooks = _install_hooks_json(codex_home, hooks_dir)
        hooks_installed.extend(installed_hooks)
    except (FileNotFoundError, OSError, json.JSONDecodeError) as e:
        result["error"] = f"Failed to install hooks.json: {e}"
        return result

    # 3. Install shared + CLI content
    shared = install_shared_content(codex_home, project_path)
    cli = install_cli_content("codex", codex_home)

    result["workflows_installed"] = []  # DB-managed via sync_bundled_content_to_db()
    result["agents_installed"] = shared.get("agents", [])
    result["commands_installed"] = cli.get("commands", [])
    result["plugins_installed"] = shared.get("plugins", [])

    # 4. Update ~/.codex/config.toml: enable feature flag, migrate from notify
    codex_config_path = codex_home / "config.toml"
    try:
        existing_config = ""
        parsed_config: dict[str, Any] = {}
        if codex_config_path.exists():
            existing_config = codex_config_path.read_text(encoding="utf-8")
            parsed_config = _load_toml_config(existing_config)
        updated_config = deepcopy(parsed_config)

        # Migrate from legacy notify mechanism
        _migrate_from_notify(updated_config, hooks_dir)

        # Enable codex_hooks feature flag
        _set_toml_value(updated_config, "features.codex_hooks", True)

        if updated_config != parsed_config:
            if codex_config_path.exists():
                backup_path = codex_config_path.with_suffix(".toml.bak")
                backup_path.write_text(existing_config, encoding="utf-8")

            _dump_toml_config(codex_config_path, updated_config)
            result["config_updated"] = True

    except Exception as e:
        result["error"] = f"Failed to update Codex config: {e}"
        return result

    # 5. Configure MCP server in config.toml
    mcp_result = configure_mcp_server_toml(codex_config_path)
    if mcp_result["success"]:
        result["mcp_configured"] = mcp_result.get("added", False)
        result["mcp_already_configured"] = mcp_result.get("already_configured", False)
    else:
        logger.warning(f"Failed to configure MCP server: {mcp_result['error']}")

    # 5b. Strip per-tool approval overrides so tools inherit session approval mode
    strip_result = strip_mcp_tool_overrides_toml(codex_config_path)
    if strip_result["success"] and strip_result.get("stripped"):
        result["mcp_tools_stripped"] = True
    elif not strip_result["success"]:
        logger.warning(f"Failed to strip MCP tool overrides: {strip_result['error']}")

    result["success"] = True
    return result


def uninstall_codex(project_path: Path | None = None) -> dict[str, Any]:
    """Uninstall Codex hooks and remove configuration.

    Returns:
        Dict with uninstallation results including success status and removed items
    """
    result: dict[str, Any] = {
        "success": False,
        "hooks_removed": [],
        "files_removed": [],
        "config_updated": False,
        "mcp_removed": False,
        "error": None,
    }

    codex_home = Path.home() / ".codex"
    hooks_dir = _get_hooks_dir()

    # 1. Remove gobby hooks from ~/.codex/hooks.json
    hooks_file = codex_home / "hooks.json"
    if hooks_file.exists():
        try:
            hooks_config = json.loads(hooks_file.read_text(encoding="utf-8"))
            if "hooks" in hooks_config:
                for hook_type in list(hooks_config["hooks"].keys()):
                    if _is_gobby_hook(hooks_config["hooks"][hook_type]):
                        del hooks_config["hooks"][hook_type]
                        result["hooks_removed"].append(hook_type)

                if not hooks_config["hooks"]:
                    del hooks_config["hooks"]

                if result["hooks_removed"]:
                    if hooks_config:
                        hooks_file.write_text(
                            json.dumps(hooks_config, indent=2) + "\n", encoding="utf-8"
                        )
                    else:
                        hooks_file.unlink()
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Could not clean hooks.json: {e}")

    # 2. Clean up legacy notify script if still present
    old_notify = hooks_dir / "codex" / "hook_dispatcher.py"
    if old_notify.exists():
        old_notify.unlink()
        result["files_removed"].append(str(old_notify))
    old_notify_dir = hooks_dir / "codex"
    if old_notify_dir.exists() and not any(old_notify_dir.iterdir()):
        try:
            old_notify_dir.rmdir()
        except OSError:
            pass

    # 3. Update config.toml: remove feature flag and legacy notify
    codex_config_path = codex_home / "config.toml"
    try:
        if codex_config_path.exists():
            existing_text = codex_config_path.read_text(encoding="utf-8")
            existing = _load_toml_config(existing_text)
            updated = deepcopy(existing)

            # Remove feature flag
            _remove_toml_key(updated, "features.codex_hooks")

            # Remove legacy notify if still present
            _remove_toml_key(updated, "notify")

            if updated != existing:
                backup_path = codex_config_path.with_suffix(".toml.bak")
                backup_path.write_text(existing_text, encoding="utf-8")
                _dump_toml_config(codex_config_path, updated)
                result["config_updated"] = True
    except Exception as e:
        logger.warning(f"Failed to update config.toml during uninstall: {e}")

    # 4. Remove MCP server from config
    mcp_result = remove_mcp_server_toml(codex_config_path)
    if mcp_result["success"]:
        result["mcp_removed"] = mcp_result.get("removed", False)

    result["success"] = True
    return result


# Backward-compatible aliases
install_codex_notify = install_codex
uninstall_codex_notify = uninstall_codex
