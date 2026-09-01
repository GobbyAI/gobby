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
from collections.abc import Iterator, Sequence
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import tomlkit
from tomlkit.exceptions import TOMLKitError
from tomlkit.items import Table
from tomlkit.toml_document import TOMLDocument

from gobby.agents.trust import seed_cli_trust, seed_gobby_home_trust
from gobby.cli.utils import get_install_dir

from .hook_commands import (
    config_contains_gobby_hook,
    merge_gobby_hook_groups,
    remove_gobby_hook_handlers,
    rewrite_hook_template_commands,
    set_gobby_hook_timeouts,
)
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
type HookTrustEntry = tuple[str, str]

CODEX_HOOK_EVENT_KEY_LABELS: dict[str, str] = {
    "PreToolUse": "pre_tool_use",
    "PermissionRequest": "permission_request",
    "PostToolUse": "post_tool_use",
    "PreCompact": "pre_compact",
    "PostCompact": "post_compact",
    "SessionStart": "session_start",
    "SubagentStart": "subagent_start",
    "UserPromptSubmit": "user_prompt_submit",
    "SubagentStop": "subagent_stop",
    "Stop": "stop",
    "SessionEnd": "session_end",
}

CODEX_MATCHER_HASH_EVENTS = {
    "PreToolUse",
    "PermissionRequest",
    "PostToolUse",
    "PreCompact",
    "PostCompact",
    "SessionStart",
    "SubagentStart",
    "SubagentStop",
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


def _quarantine_corrupt_hooks_file(hooks_file: Path, reason: str) -> None:
    """Move an unreadable hooks file aside so foreign hooks are never silently lost."""
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    reservation_fd, reservation_name = tempfile.mkstemp(
        dir=str(hooks_file.parent),
        prefix=f"{hooks_file.name}.{timestamp}.",
        suffix=".corrupt",
    )
    os.close(reservation_fd)
    corrupt_path = Path(reservation_name)
    try:
        os.replace(hooks_file, corrupt_path)
    except OSError:
        try:
            if corrupt_path.stat().st_size == 0:
                corrupt_path.unlink()
        except OSError:
            logger.warning("Failed to clean unused quarantine reservation %s", corrupt_path)
        raise
    logger.warning(
        "Existing %s is unusable (%s); preserved it at %s and installing fresh. "
        "Recover any non-Gobby hooks from the preserved file manually.",
        hooks_file,
        reason,
        corrupt_path,
    )


def _codex_hook_state_key(
    hooks_file: Path, event_name: str, group_index: int, handler_index: int
) -> str:
    event_label = CODEX_HOOK_EVENT_KEY_LABELS[event_name]
    return f"{hooks_file.resolve()}:{event_label}:{group_index}:{handler_index}"


def _normalize_codex_command(command: Any) -> str | None:
    """Normalize Codex command strings and argv-style command sequences."""
    if isinstance(command, str):
        normalized = " ".join(command.split())
        return normalized or None

    if isinstance(command, Sequence) and not isinstance(command, (str, bytes, bytearray)):
        parts: list[str] = []
        for part in command:
            if not isinstance(part, str):
                return None
            normalized_part = " ".join(part.split())
            if normalized_part:
                parts.append(normalized_part)
        return " ".join(parts) or None

    return None


def _normalized_codex_command_hook_hash(
    event_name: str,
    group: dict[str, Any],
    hook: dict[str, Any],
) -> str | None:
    """Return Codex's normalized trust hash for one command hook."""
    command = _normalize_codex_command(hook.get("command"))
    if command is None:
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
) -> Iterator[HookTrustEntry]:
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
                if not isinstance(handler, dict) or not config_contains_gobby_hook(handler):
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


def _remove_stale_gobby_hook_trust_state(
    state_table: Table,
    hooks_file: Path,
    current_entries: list[HookTrustEntry],
    previous_entries: list[HookTrustEntry],
) -> None:
    """Remove stale Gobby-owned positional trust entries for this hooks file.

    Codex keys hook trust by file, event, group index, and handler index. Reinstalling
    can move Gobby handlers to new positions, so old Gobby keys must be pruned while
    unrelated user trust state remains intact.
    """
    current_keys = {key for key, _ in current_entries}
    hooks_prefix = f"{hooks_file.resolve()}:"
    event_labels = set(CODEX_HOOK_EVENT_KEY_LABELS.values())
    previous_keys = {key for key, _ in previous_entries}
    previous_suffixes = {
        key.removeprefix(hooks_prefix) for key in previous_keys if key.startswith(hooks_prefix)
    }

    for key in list(state_table.keys()):
        if not isinstance(key, str) or not key.startswith(hooks_prefix):
            continue
        if key in current_keys:
            continue
        suffix_parts = key.removeprefix(hooks_prefix).split(":")
        if len(suffix_parts) < 3 or suffix_parts[0] not in event_labels:
            continue
        suffix = key.removeprefix(hooks_prefix)
        if key in previous_keys or suffix in previous_suffixes:
            del state_table[key]


def _ensure_codex_hook_trust_state(
    config: TOMLDocument,
    hooks_file: Path,
    previous_entries: list[HookTrustEntry] | None = None,
) -> set[str]:
    """Mark installed Gobby hooks as trusted in Codex config.toml."""
    hooks_config = json.loads(hooks_file.read_text(encoding="utf-8"))
    entries = list(_iter_gobby_hook_trust_entries(hooks_file, hooks_config))
    if not entries:
        return set()

    hooks_table = _ensure_table(config, "hooks")
    state_table = _ensure_table(hooks_table, "state")
    _remove_stale_gobby_hook_trust_state(state_table, hooks_file, entries, previous_entries or [])
    trusted_keys: set[str] = set()

    for key, trusted_hash in entries:
        existing = state_table.get(key)
        entry = existing if isinstance(existing, Table) else tomlkit.table()
        if "enabled" in entry:
            # Codex disables a hook by writing enabled=false (e.g. after repeated
            # timeouts). A disabled Gobby hook starves the daemon of its events and
            # deadlocks enforcement gates, so restore the default-enabled state.
            if not entry["enabled"]:
                logger.warning(
                    "Codex had disabled the Gobby hook %s; re-enabling it. "
                    "Gobby enforcement depends on this hook.",
                    key,
                )
            del entry["enabled"]
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


def _install_hooks_file(
    hooks_file: Path,
    hooks_dir: Path,
    *,
    hook_timeout_seconds: int = 120,
) -> tuple[list[str], list[HookTrustEntry]]:
    """Load hooks-template.json, substitute $HOOKS_DIR, merge into a Codex hooks file.

    Returns installed hook type names and Gobby hook trust entries from the
    pre-clean hooks file.
    """
    install_dir = get_install_dir()
    template_path = install_dir / "codex" / "hooks-template.json"

    if not template_path.exists():
        raise FileNotFoundError(f"Missing hooks template: {template_path}")

    template_str = template_path.read_text(encoding="utf-8")
    template_str = template_str.replace("$HOOKS_DIR", str(hooks_dir.resolve()))
    gobby_hooks_config = json.loads(template_str)
    rewrite_hook_template_commands(gobby_hooks_config, cli_name="codex", hooks_dir=hooks_dir)
    set_gobby_hook_timeouts(
        gobby_hooks_config,
        timeout=hook_timeout_seconds,
        hook_overrides={"SessionEnd": 3},
    )

    existing: dict[str, Any] = {}
    if hooks_file.exists():
        try:
            parsed: Any = json.loads(hooks_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            _quarantine_corrupt_hooks_file(hooks_file, str(e))
        else:
            if isinstance(parsed, dict):
                existing = parsed
            else:
                _quarantine_corrupt_hooks_file(
                    hooks_file, f"expected a JSON object, got {type(parsed).__name__}"
                )

    previous_gobby_trust_entries: list[HookTrustEntry] = []
    if isinstance(existing.get("hooks"), dict):
        previous_gobby_trust_entries = list(_iter_gobby_hook_trust_entries(hooks_file, existing))

    if not isinstance(existing.get("hooks"), dict):
        existing["hooks"] = {}

    hooks_installed = []
    for hook_type, hook_config in gobby_hooks_config.get("hooks", {}).items():
        existing_groups = existing["hooks"].get(hook_type, [])
        if not isinstance(existing_groups, list):
            existing_groups = []
        existing["hooks"][hook_type] = merge_gobby_hook_groups(
            existing_groups, hook_config, is_gobby_hook=config_contains_gobby_hook
        )
        hooks_installed.append(hook_type)

    _atomic_write_json(hooks_file, existing)

    return hooks_installed, previous_gobby_trust_entries


def _install_hooks_json(
    codex_home: Path,
    hooks_dir: Path,
    *,
    hook_timeout_seconds: int = 120,
) -> tuple[list[str], list[HookTrustEntry]]:
    """Load hooks-template.json, substitute $HOOKS_DIR, merge into ~/.codex/hooks.json."""
    return _install_hooks_file(
        codex_home / "hooks.json",
        hooks_dir,
        hook_timeout_seconds=hook_timeout_seconds,
    )


def install_codex(
    project_path: Path,
    *,
    mode: str = "global",
    hook_timeout_seconds: int = 120,
) -> dict[str, Any]:
    """Install Codex hooks via hooks.json and configure MCP server.

    Args:
        project_path: Project root directory. Shared content (plugins)
            installs to {project_path}/.gobby/.
        mode: Installation mode. Only "global" is supported for Codex.
            Accepted for interface consistency with other CLI installers.

    Returns:
        Dict with installation results including success status and installed items
    """
    if mode != "global":
        logger.warning("Codex install: mode=%r not supported, falling back to global", mode)
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
        installed_hooks, previous_gobby_trust_entries = _install_hooks_json(
            codex_home,
            hooks_dir,
            hook_timeout_seconds=hook_timeout_seconds,
        )
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

        # Enable stable hooks and remove the deprecated codex_hooks flag.
        _remove_toml_key(updated_config, "features.codex_hooks")
        _set_toml_value(updated_config, "features.hooks", True)
        _ensure_codex_hook_trust_state(
            updated_config,
            codex_home / "hooks.json",
            previous_entries=previous_gobby_trust_entries,
        )

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
        logger.warning("Failed to configure MCP server: %s", mcp_result["error"])

    # 5b. Strip per-tool approval overrides so tools inherit session approval mode
    strip_result = strip_mcp_tool_overrides_toml(codex_config_path)
    if strip_result["success"] and strip_result.get("stripped"):
        result["mcp_tools_stripped"] = True
    elif not strip_result["success"]:
        logger.warning("Failed to strip MCP tool overrides: %s", strip_result["error"])

    try:
        trust_result = seed_gobby_home_trust("codex")
    except (OSError, ValueError, TOMLKitError) as e:
        result["error"] = f"Failed to seed Codex trust: {e}"
        return result
    result["trust"] = trust_result
    if not trust_result.get("success"):
        errors = trust_result.get("errors") or []
        detail = "; ".join(str(error) for error in errors) if errors else "unknown error"
        result["error"] = f"Failed to seed Codex trust: {detail}"
        return result
    if trust_result.get("files_written"):
        result["config_updated"] = True

    result["success"] = True
    return result


def install_codex_project_hooks(
    project_path: Path,
    *,
    hook_timeout_seconds: int = 120,
) -> dict[str, Any]:
    """Install project-local Codex hooks for a worktree without global Codex content."""
    hooks_installed: list[str] = []
    files_installed: list[str] = []
    result: dict[str, Any] = {
        "success": False,
        "hooks_installed": hooks_installed,
        "files_installed": files_installed,
        "config_updated": False,
        "trust": None,
        "error": None,
    }

    codex_home = Path.home() / ".codex"
    codex_home.mkdir(parents=True, exist_ok=True)
    hooks_dir = _get_hooks_dir()

    try:
        files_installed.extend(install_global_hooks())
    except OSError as e:
        result["error"] = f"Failed to install global hooks: {e}"
        return result

    try:
        project_codex_dir = project_path / ".codex"
        project_codex_dir.mkdir(parents=True, exist_ok=True)
        project_hooks_path = project_codex_dir / "hooks.json"
        installed_hooks, previous_gobby_trust_entries = _install_hooks_file(
            project_hooks_path,
            hooks_dir,
            hook_timeout_seconds=hook_timeout_seconds,
        )
        hooks_installed.extend(installed_hooks)
    except (FileNotFoundError, OSError, json.JSONDecodeError) as e:
        result["error"] = f"Failed to install project hooks.json: {e}"
        return result

    codex_config_path = codex_home / "config.toml"
    try:
        existing_config = ""
        parsed_config: TOMLDocument = tomlkit.document()
        if codex_config_path.exists():
            existing_config = codex_config_path.read_text(encoding="utf-8")
            parsed_config = _load_toml_config(existing_config)
        updated_config: TOMLDocument = deepcopy(parsed_config)

        _remove_toml_key(updated_config, "features.codex_hooks")
        _set_toml_value(updated_config, "features.hooks", True)
        _ensure_codex_hook_trust_state(
            updated_config,
            project_hooks_path,
            previous_entries=previous_gobby_trust_entries,
        )

        if updated_config != parsed_config:
            if codex_config_path.exists():
                backup_path = codex_config_path.with_suffix(".toml.bak")
                backup_path.write_text(existing_config, encoding="utf-8")

            _dump_toml_config(codex_config_path, updated_config)
            result["config_updated"] = True

    except Exception as e:
        result["error"] = f"Failed to update Codex config: {e}"
        return result

    trust_result = seed_cli_trust("codex", project_path).as_dict()
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
                        if config_contains_gobby_hook(hook_groups):
                            del hooks_config["hooks"][hook_type]
                            result["hooks_removed"].append(hook_type)
                        continue

                    cleaned_groups, removed = remove_gobby_hook_handlers(
                        hook_groups, is_gobby_hook=config_contains_gobby_hook
                    )
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
            logger.warning("Could not clean hooks.json: %s", e)

    # 2. Update config.toml: remove hook feature flags and Gobby trust state
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

            if updated != existing:
                backup_path = codex_config_path.with_suffix(".toml.bak")
                backup_path.write_text(existing_text, encoding="utf-8")
                _dump_toml_config(codex_config_path, updated)
                result["config_updated"] = True
    except Exception as e:
        logger.warning("Failed to update config.toml during uninstall: %s", e)

    # 4. Remove MCP server from config
    mcp_result = remove_mcp_server_toml(codex_config_path)
    if mcp_result["success"]:
        result["mcp_removed"] = mcp_result.get("removed", False)

    result["success"] = True
    return result
