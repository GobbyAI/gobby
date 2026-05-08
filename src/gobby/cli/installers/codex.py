"""
Codex CLI installation for Gobby hooks.

This module handles installing and uninstalling Gobby hook integration
for OpenAI Codex CLI via hooks.json (hooks feature).
"""

import hashlib
import json
import logging
import os
import tempfile
from collections.abc import Iterator
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import tomlkit
from tomlkit.items import Table
from tomlkit.toml_document import TOMLDocument

from gobby.agents.trust import seed_gobby_home_trust
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

type TomlValue = str | int | float | bool | datetime | list["TomlValue"] | dict[str, "TomlValue"]

CODEX_HOOK_EVENT_KEY_LABELS: dict[str, str] = {
    "PreToolUse": "pre_tool_use",
    "PermissionRequest": "permission_request",
    "PostToolUse": "post_tool_use",
    "PreCompact": "pre_compact",
    "PostCompact": "post_compact",
    "SessionStart": "session_start",
    "UserPromptSubmit": "user_prompt_submit",
    "Stop": "stop",
}

CODEX_MATCHER_HASH_EVENTS = {
    "PreToolUse",
    "PermissionRequest",
    "PostToolUse",
    "PreCompact",
    "PostCompact",
    "SessionStart",
}


def _get_hooks_dir() -> Path:
    """Get the global hooks directory path."""
    return Path(os.environ.get("GOBBY_HOOKS_DIR", str(Path.home() / ".gobby" / "hooks")))


def _load_toml_config(content: str) -> TOMLDocument:
    """Parse a TOML config string into a mutable TOML document."""
    if not content.strip():
        return tomlkit.document()
    parsed = tomlkit.parse(content)
    return parsed


def _insert_top_level_table(config: TOMLDocument, table_name: str, table_value: Table) -> None:
    """Insert a new top-level table before existing table sections."""
    if table_name in config:
        config[table_name] = table_value
        return

    reordered = tomlkit.document()
    inserted = False
    for key, value in config.items():
        if not inserted and isinstance(value, Table):
            reordered[table_name] = table_value
            inserted = True
        reordered[key] = value

    if not inserted:
        reordered[table_name] = table_value

    for key in list(config.keys()):
        del config[key]
    for key, value in reordered.items():
        config[key] = value


def _set_toml_value(config: TOMLDocument, key: str, value: TomlValue) -> None:
    """Set a dotted TOML key inside a parsed config dict."""
    parts = key.split(".")
    current: TOMLDocument | Table | dict[str, Any] = config
    for index, part in enumerate(parts[:-1]):
        existing = current.get(part)
        if isinstance(existing, (dict, Table)):
            current = existing
            continue
        if existing is not None:
            path_prefix = ".".join(parts[: index + 1])
            raise ValueError(f"Cannot set nested key {path_prefix!r}: existing value is a scalar")

        new_table = tomlkit.table()
        if index == 0:
            _insert_top_level_table(config, part, new_table)
            current = cast(Table, config[part])
        else:
            current[part] = new_table
            current = new_table

    current[parts[-1]] = tomlkit.item(value)


def _remove_toml_key(config: TOMLDocument, key: str) -> None:
    """Remove a dotted TOML key from a parsed config dict."""
    parts = key.split(".")
    current: dict[str, Any] | Table = config
    parents: list[tuple[dict[str, Any] | Table, str]] = []

    for part in parts[:-1]:
        child = current.get(part)
        if not isinstance(child, (dict, Table)):
            return
        parents.append((current, part))
        current = child

    if parts[-1] not in current:
        return

    del current[parts[-1]]

    for parent, part in reversed(parents):
        child = parent.get(part)
        if isinstance(child, (dict, Table)) and not child:
            del parent[part]
            continue
        break


def _dump_toml_config(config_path: Path, config: TOMLDocument) -> None:
    """Write a parsed TOML config back to disk without stripping comments."""
    config_path.write_text(tomlkit.dumps(config), encoding="utf-8")


def _migrate_from_notify(config: TOMLDocument, hooks_dir: Path) -> None:
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


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """Atomically write JSON to a file in its parent directory."""
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


def _clean_gobby_handlers_from_groups(groups: list[Any]) -> tuple[list[Any], bool]:
    """Remove Gobby-owned command handlers while preserving unrelated handlers."""
    cleaned_groups: list[Any] = []
    removed = False

    for group in groups:
        if not isinstance(group, dict):
            if _is_gobby_hook(group):
                removed = True
            else:
                cleaned_groups.append(group)
            continue

        handlers = group.get("hooks")
        if not isinstance(handlers, list):
            if _is_gobby_hook(group):
                removed = True
            else:
                cleaned_groups.append(group)
            continue

        cleaned_handlers = [handler for handler in handlers if not _is_gobby_hook(handler)]
        if len(cleaned_handlers) != len(handlers):
            removed = True

        if cleaned_handlers:
            cleaned_group = deepcopy(group)
            cleaned_group["hooks"] = cleaned_handlers
            cleaned_groups.append(cleaned_group)

    return cleaned_groups, removed


def _codex_hook_state_key(
    hooks_file: Path, event_name: str, group_index: int, handler_index: int
) -> str:
    event_label = CODEX_HOOK_EVENT_KEY_LABELS[event_name]
    return f"{hooks_file.resolve()}:{event_label}:{group_index}:{handler_index}"


def _normalized_codex_command_hook_hash(
    event_name: str,
    group: dict[str, Any],
    hook: dict[str, Any],
) -> str | None:
    """Return Codex's normalized trust hash for one command hook."""
    command = hook.get("command")
    if not isinstance(command, str) or not command.strip():
        return None

    try:
        timeout = int(hook.get("timeout", 600))
    except (TypeError, ValueError):
        timeout = 600
    timeout = max(1, timeout)

    normalized_hook: dict[str, Any] = {
        "async": hook.get("async") if isinstance(hook.get("async"), bool) else False,
        "command": command,
        "timeout": timeout,
        "type": "command",
    }
    status_message = hook.get("statusMessage")
    if isinstance(status_message, str):
        normalized_hook["statusMessage"] = status_message

    identity: dict[str, Any] = {
        "event_name": CODEX_HOOK_EVENT_KEY_LABELS[event_name],
        "hooks": [normalized_hook],
    }
    matcher = group.get("matcher")
    if event_name in CODEX_MATCHER_HASH_EVENTS and matcher is not None:
        identity["matcher"] = matcher

    serialized = json.dumps(
        identity,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(serialized).hexdigest()}"


def _iter_gobby_hook_trust_entries(
    hooks_file: Path,
    hooks_config: dict[str, Any],
) -> Iterator[tuple[str, str]]:
    """Yield Codex hooks.state key/hash pairs for Gobby-owned command hooks."""
    hooks = hooks_config.get("hooks")
    if not isinstance(hooks, dict):
        return

    for event_name in CODEX_HOOK_EVENT_KEY_LABELS:
        groups = hooks.get(event_name)
        if not isinstance(groups, list):
            continue
        for group_index, group in enumerate(groups):
            if not isinstance(group, dict):
                continue
            handlers = group.get("hooks")
            if not isinstance(handlers, list):
                continue
            for handler_index, handler in enumerate(handlers):
                if not isinstance(handler, dict) or not _is_gobby_hook(handler):
                    continue
                current_hash = _normalized_codex_command_hook_hash(event_name, group, handler)
                if current_hash:
                    yield (
                        _codex_hook_state_key(
                            hooks_file,
                            event_name,
                            group_index,
                            handler_index,
                        ),
                        current_hash,
                    )


def _ensure_table(parent: TOMLDocument | Table | dict[str, Any], key: str) -> Table:
    existing = parent.get(key)
    if isinstance(existing, Table):
        return existing
    new_table = tomlkit.table()
    parent[key] = new_table
    return new_table


def _ensure_codex_hook_trust_state(config: TOMLDocument, hooks_file: Path) -> set[str]:
    """Mark installed Gobby hooks as trusted in Codex config.toml."""
    hooks_config = json.loads(hooks_file.read_text(encoding="utf-8"))
    entries = list(_iter_gobby_hook_trust_entries(hooks_file, hooks_config))
    if not entries:
        return set()

    hooks_table = _ensure_table(config, "hooks")
    state_table = _ensure_table(hooks_table, "state")
    trusted_keys: set[str] = set()

    for key, trusted_hash in entries:
        existing = state_table.get(key)
        entry = existing if isinstance(existing, Table) else tomlkit.table()
        entry["trusted_hash"] = trusted_hash
        state_table[key] = entry
        trusted_keys.add(key)

    return trusted_keys


def _remove_codex_hook_trust_state(config: TOMLDocument, state_keys: set[str]) -> None:
    """Remove only trust-state entries that correspond to Gobby-owned hooks."""
    if not state_keys:
        return

    hooks_table = config.get("hooks")
    if not isinstance(hooks_table, (dict, Table)):
        return

    state_table = hooks_table.get("state")
    if not isinstance(state_table, (dict, Table)):
        return

    for key in state_keys:
        if key in state_table:
            del state_table[key]

    if not state_table:
        del hooks_table["state"]
    if not hooks_table:
        del config["hooks"]


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

    if not isinstance(existing.get("hooks"), dict):
        existing["hooks"] = {}

    hooks_installed = []
    for hook_type, hook_config in gobby_hooks_config.get("hooks", {}).items():
        existing_groups = existing["hooks"].get(hook_type, [])
        if not isinstance(existing_groups, list):
            existing_groups = []
        cleaned_groups, _ = _clean_gobby_handlers_from_groups(existing_groups)
        existing["hooks"][hook_type] = cleaned_groups + hook_config
        hooks_installed.append(hook_type)

    _atomic_write_json(hooks_file, existing)

    return hooks_installed


def _is_gobby_hook(hook_entry: Any) -> bool:
    """Check if a hooks.json entry was installed by Gobby.

    Inspects the entry's command/args for the ``--gobby-owned`` marker
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
        "trust": None,
        "error": None,
    }

    codex_home = Path.home() / ".codex"
    codex_home.mkdir(parents=True, exist_ok=True)
    hooks_dir = _get_hooks_dir()

    # 1. Install shared global hook helper files.
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

    # 4. Update ~/.codex/config.toml: enable stable hooks and trust Gobby handlers
    codex_config_path = codex_home / "config.toml"
    try:
        existing_config = ""
        parsed_config: TOMLDocument = tomlkit.document()
        if codex_config_path.exists():
            existing_config = codex_config_path.read_text(encoding="utf-8")
            parsed_config = _load_toml_config(existing_config)
        updated_config: TOMLDocument = deepcopy(parsed_config)

        # Migrate from legacy notify mechanism
        _migrate_from_notify(updated_config, hooks_dir)

        # Enable stable hooks and remove the deprecated codex_hooks flag.
        _remove_toml_key(updated_config, "features.codex_hooks")
        _set_toml_value(updated_config, "features.hooks", True)
        _ensure_codex_hook_trust_state(updated_config, codex_home / "hooks.json")

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

    trust_result = seed_gobby_home_trust("codex")
    result["trust"] = trust_result
    if trust_result.get("files_written"):
        result["config_updated"] = True

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
    gobby_hook_state_keys: set[str] = set()
    if hooks_file.exists():
        try:
            hooks_config = json.loads(hooks_file.read_text(encoding="utf-8"))
            if isinstance(hooks_config, dict) and isinstance(hooks_config.get("hooks"), dict):
                gobby_hook_state_keys = {
                    key for key, _ in _iter_gobby_hook_trust_entries(hooks_file, hooks_config)
                }
                for hook_type in list(hooks_config["hooks"].keys()):
                    hook_groups = hooks_config["hooks"][hook_type]
                    if not isinstance(hook_groups, list):
                        if _is_gobby_hook(hook_groups):
                            del hooks_config["hooks"][hook_type]
                            result["hooks_removed"].append(hook_type)
                        continue

                    cleaned_groups, removed = _clean_gobby_handlers_from_groups(hook_groups)
                    if not removed:
                        continue
                    if cleaned_groups:
                        hooks_config["hooks"][hook_type] = cleaned_groups
                    else:
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

    # 3. Update config.toml: remove hook feature flags, Gobby trust state, and legacy notify
    codex_config_path = codex_home / "config.toml"
    try:
        if codex_config_path.exists():
            existing_text = codex_config_path.read_text(encoding="utf-8")
            existing = _load_toml_config(existing_text)
            updated = deepcopy(existing)

            # Remove feature flags
            _remove_toml_key(updated, "features.hooks")
            _remove_toml_key(updated, "features.codex_hooks")
            _remove_codex_hook_trust_state(updated, gobby_hook_state_keys)

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
