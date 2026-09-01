"""
Claude Code installation for Gobby hooks.

This module handles installing and uninstalling Gobby hooks
and workflows for Claude Code CLI.
"""

import json
import logging
import os
import shlex
import tempfile
import time
from pathlib import Path
from shutil import copy2
from typing import Any

from gobby.adapters.claude_contract import CLAUDE_PASCAL_HOOK_NAMES
from gobby.agents.trust import seed_gobby_home_trust
from gobby.cli.utils import get_install_dir
from gobby.utils.durable_file import durable_replace_text
from gobby.utils.native_bin import resolve_native_bin_or_default

from .hook_commands import (
    merge_gobby_hook_groups,
    remove_gobby_hook_handlers,
    rewrite_hook_template_commands,
    set_gobby_hook_timeouts,
)
from .mcp_config import configure_mcp_server_json, remove_mcp_server_json
from .shared import (
    clean_project_hooks,
    install_cli_content,
    install_global_hooks,
    install_shared_content,
)
from .skill_install import backup_gobby_skills, install_router_skills_as_commands

logger = logging.getLogger(__name__)

# Hook types that Gobby registers (must match hooks-template.json)
_GOBBY_HOOK_TYPES = list(CLAUDE_PASCAL_HOOK_NAMES)


_STATUSLINE_GHOOK_MARKER = "--gobby-owned --cli=claude --type=statusline"
_AUTO_MEMORY_PROVENANCE_FILENAME = ".gobby-auto-memory.json"


def _is_gobby_statusline_command(command: str) -> bool:
    """Return whether a statusLine command belongs to Gobby."""
    return _STATUSLINE_GHOOK_MARKER in command


def _with_statusline_downstream(command: str, downstream: str | None) -> str:
    """Attach the downstream env var when wrapping a foreign statusLine."""
    if not downstream:
        return command
    quoted_downstream = "'" + downstream.replace("'", "'\\''") + "'"
    return f"GOBBY_STATUSLINE_DOWNSTREAM={quoted_downstream} {command}"


def _build_statusline_command(downstream: str | None) -> str:
    """Build the ghook statusLine command."""
    ghook_bin = resolve_native_bin_or_default("ghook")
    command = f"{shlex.quote(ghook_bin)} {_STATUSLINE_GHOOK_MARKER}"
    return _with_statusline_downstream(command, downstream)


def _configure_statusline(settings: dict[str, Any], hooks_dir: Path) -> None:
    """Configure statusLine to use Gobby's middleware, preserving any downstream.

    If statusLine already points to our handler, re-wrap to update paths.
    If it points to something else, wrap it as GOBBY_STATUSLINE_DOWNSTREAM.
    """
    existing = settings.get("statusLine")

    downstream: str | None = None

    if existing and isinstance(existing, dict):
        existing_cmd = existing.get("command", "")
        if _is_gobby_statusline_command(existing_cmd):
            # Already ours — extract downstream if present
            downstream = _extract_downstream(existing_cmd)
        else:
            # Foreign command — save as downstream
            downstream = existing_cmd
    elif existing and isinstance(existing, str):
        if _is_gobby_statusline_command(existing):
            downstream = _extract_downstream(existing)
        else:
            downstream = existing

    settings["statusLine"] = {
        "type": "command",
        "command": _build_statusline_command(downstream),
    }


def _extract_downstream(command: str) -> str | None:
    """Extract the downstream command from GOBBY_STATUSLINE_DOWNSTREAM."""
    try:
        parts = shlex.split(command, posix=True)
    except ValueError:
        return None

    for part in parts:
        if part.startswith("GOBBY_STATUSLINE_DOWNSTREAM="):
            return part.split("=", 1)[1]
    return None


def _remove_gobby_hooks(settings: dict[str, Any]) -> list[str]:
    """Remove only Gobby-owned handlers for Gobby's registered event types."""
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return []

    removed: list[str] = []
    for hook_type in _GOBBY_HOOK_TYPES:
        if hook_type not in hooks:
            continue
        hook_config = hooks[hook_type]
        groups = hook_config if isinstance(hook_config, list) else [hook_config]
        cleaned, handlers_removed = remove_gobby_hook_handlers(groups)
        if not handlers_removed:
            continue
        if cleaned:
            hooks[hook_type] = cleaned
        else:
            del hooks[hook_type]
        removed.append(hook_type)

    if not hooks:
        settings.pop("hooks", None)
    return removed


def _restore_statusline(settings: dict[str, Any]) -> None:
    """On uninstall, restore the original statusLine or remove it."""
    existing = settings.get("statusLine")
    if not existing:
        return

    cmd = existing.get("command", "") if isinstance(existing, dict) else str(existing)
    if not _is_gobby_statusline_command(cmd):
        return  # Not ours

    downstream = _extract_downstream(cmd)
    if downstream:
        settings["statusLine"] = {"type": "command", "command": downstream}
    else:
        del settings["statusLine"]


def install_claude(
    project_path: Path,
    mode: str = "global",
    *,
    hook_timeout_seconds: int = 120,
) -> dict[str, Any]:
    """Install Gobby integration for Claude Code (hooks, workflows).

    Args:
        project_path: Path to the project root
        mode: "global" installs hooks to ~/.gobby/hooks/ and settings to
            ~/.claude/settings.json. "project" installs per-project (existing behavior).

    Returns:
        Dict with installation results including success status and installed items
    """
    hooks_installed: list[str] = []
    result: dict[str, Any] = {
        "success": False,
        "hooks_installed": hooks_installed,
        "workflows_installed": [],
        "commands_installed": [],
        "mcp_configured": False,
        "mcp_already_configured": False,
        "trust": None,
        "error": None,
    }

    if hook_timeout_seconds <= 0:
        result["error"] = "hook_timeout_seconds must be positive"
        return result

    hooks_dir = Path.home() / ".gobby" / "hooks"
    if mode == "global":
        claude_path = Path.home() / ".claude"
    else:
        claude_path = project_path / ".claude"
    settings_file = claude_path / "settings.json"
    auto_memory_provenance_file = claude_path / _AUTO_MEMORY_PROVENANCE_FILENAME

    # Ensure directories exist
    claude_path.mkdir(parents=True, exist_ok=True)

    # Backup existing gobby skills (now auto-synced from database)
    skills_dir = claude_path / "skills"
    backup_result = backup_gobby_skills(skills_dir)
    if backup_result["backed_up"] > 0:
        logger.info("Backed up %s existing gobby skills", backup_result["backed_up"])

    # Get source files
    install_dir = get_install_dir()
    claude_install_dir = install_dir / "claude"

    source_hooks_template = claude_install_dir / "hooks-template.json"

    if not source_hooks_template.exists():
        result["error"] = f"Missing source files: [{source_hooks_template}]"
        return result

    # Install hook files (always global)
    try:
        install_global_hooks()
        # Clean up project-level hooks to prevent double-firing
        cleaned = clean_project_hooks(project_path / ".claude" / "settings.json")
        if cleaned:
            result["project_hooks_cleaned"] = cleaned
    except OSError as e:
        logger.error("Failed to install hook files: %s", e)
        result["error"] = f"Failed to install hook files: {e}"
        return result

    # Install shared content (plugins) - project-scoped
    try:
        content_path = claude_path if mode == "project" else project_path / ".claude"
        shared = install_shared_content(content_path, project_path)
    except Exception as e:
        logger.error("Failed to install shared content: %s", e)
        result["error"] = f"Failed to install shared content: {e}"
        return result

    # Install CLI-specific content (can override shared)
    try:
        cli = install_cli_content("claude", claude_path)
    except Exception as e:
        logger.error("Failed to install CLI content: %s", e)
        result["error"] = f"Failed to install CLI content: {e}"
        return result

    result["workflows_installed"] = []  # DB-managed via sync_bundled_content_to_db()
    result["agents_installed"] = shared.get("agents", [])
    result["commands_installed"] = cli.get("commands", [])
    result["plugins_installed"] = shared.get("plugins", [])

    # Install router skills (gobby, g) as flattened commands
    commands_dir = claude_path / "commands"
    router_commands = install_router_skills_as_commands(commands_dir)
    result["commands_installed"].extend(router_commands)

    # Skills are now auto-synced to database on daemon startup (sync_bundled_skills)
    # No longer need to copy to .claude/skills/

    # Backup existing settings.json if it exists
    backup_file = None
    if settings_file.exists():
        timestamp = int(time.time())
        backup_file = claude_path / f"settings.json.{timestamp}.backup"
        try:
            copy2(settings_file, backup_file)
        except OSError as e:
            logger.error("Failed to create backup of settings.json: %s", e)
            result["error"] = f"Failed to create backup: {e}"
            return result

        # Verify backup exists
        if not backup_file.exists():
            logger.error("Backup file was not created successfully")
            result["error"] = "Backup file was not created successfully"
            return result

    # Load existing settings or create empty
    existing_settings: dict[str, Any] = {}
    if settings_file.exists():
        try:
            with open(settings_file) as f:
                existing_settings = json.load(f)
        except json.JSONDecodeError as e:
            logger.error("Failed to parse settings.json: %s", e)
            result["error"] = f"Failed to parse settings.json: {e}"
            return result
        except OSError as e:
            logger.error("Failed to read settings.json: %s", e)
            result["error"] = f"Failed to read settings.json: {e}"
            return result

    # Load Gobby hooks from template
    try:
        with open(source_hooks_template) as f:
            gobby_settings_str = f.read()
    except OSError as e:
        logger.error("Failed to read hooks template: %s", e)
        result["error"] = f"Failed to read hooks template: {e}"
        return result

    # Replace $HOOKS_DIR with absolute hooks directory path
    gobby_settings_str = gobby_settings_str.replace("$HOOKS_DIR", str(hooks_dir.resolve()))

    try:
        gobby_settings = json.loads(gobby_settings_str)
    except json.JSONDecodeError as e:
        logger.error("Failed to parse hooks template: %s", e)
        result["error"] = f"Failed to parse hooks template: {e}"
        return result
    rewrite_hook_template_commands(gobby_settings, cli_name="claude", hooks_dir=hooks_dir)
    set_gobby_hook_timeouts(
        gobby_settings,
        timeout=hook_timeout_seconds,
        hook_overrides={"SessionEnd": min(hook_timeout_seconds, 60)},
    )

    # Ensure hooks section exists
    if "hooks" not in existing_settings:
        existing_settings["hooks"] = {}

    # Merge Gobby hooks
    gobby_hooks = gobby_settings.get("hooks", {})
    for hook_type, hook_config in gobby_hooks.items():
        existing_settings["hooks"][hook_type] = merge_gobby_hook_groups(
            existing_settings["hooks"].get(hook_type, []), hook_config
        )
        hooks_installed.append(hook_type)

    # Gobby owns memory for settings it creates. User-authored true/false values
    # remain untouched and are never marked as Gobby-managed.
    introduced_auto_memory = "autoMemoryEnabled" not in existing_settings
    if introduced_auto_memory:
        existing_settings["autoMemoryEnabled"] = False

    # Configure statusLine for token tracking middleware before persisting settings.
    _configure_statusline(existing_settings, hooks_dir)

    provenance_file_existed = auto_memory_provenance_file.exists()
    if introduced_auto_memory:
        try:
            durable_replace_text(
                auto_memory_provenance_file,
                json.dumps({"managed": True, "previous": None}, indent=2) + "\n",
            )
        except OSError as e:
            logger.error("Failed to write auto-memory provenance: %s", e)
            result["error"] = f"Failed to write auto-memory provenance: {e}"
            return result

    # Write merged settings back using atomic write
    try:
        fd, temp_path = tempfile.mkstemp(dir=str(claude_path), suffix=".tmp", prefix="settings_")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(existing_settings, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            # Atomic replace
            os.replace(temp_path, settings_file)
        except Exception:
            # Clean up temp file if it still exists
            if os.path.exists(temp_path):
                os.unlink(temp_path)
            raise
    except OSError as e:
        logger.error("Failed to write settings.json: %s", e)
        if introduced_auto_memory and not provenance_file_existed:
            try:
                auto_memory_provenance_file.unlink(missing_ok=True)
            except OSError as cleanup_error:
                logger.error("Failed to remove auto-memory provenance: %s", cleanup_error)
        # Attempt to restore from backup if we have one
        if backup_file and backup_file.exists():
            try:
                copy2(backup_file, settings_file)
                logger.info("Restored settings.json from backup after write failure")
            except OSError as restore_error:
                logger.error("Failed to restore from backup: %s", restore_error)
        result["error"] = f"Failed to write settings.json: {e}"
        return result

    # Configure MCP server in global settings (~/.claude.json)
    # Note: Claude Code uses ~/.claude.json for user-scoped MCP servers
    global_settings = Path.home() / ".claude.json"
    mcp_result = configure_mcp_server_json(global_settings)
    if mcp_result["success"]:
        result["mcp_configured"] = mcp_result.get("added", False)
        result["mcp_already_configured"] = mcp_result.get("already_configured", False)
    else:
        # MCP config failure is non-fatal, just log it
        logger.warning("Failed to configure MCP server: %s", mcp_result["error"])

    result["trust"] = seed_gobby_home_trust("claude")

    result["success"] = True
    return result


def uninstall_claude(project_path: Path) -> dict[str, Any]:
    """Uninstall Gobby integration from Claude Code.

    Args:
        project_path: Path to the project root

    Returns:
        Dict with uninstallation results including success status and removed items
    """
    hooks_removed: list[str] = []
    files_removed: list[str] = []

    result: dict[str, Any] = {
        "success": False,
        "hooks_removed": hooks_removed,
        "files_removed": files_removed,
        "mcp_removed": False,
        "error": None,
    }

    claude_path = project_path / ".claude"
    settings_file = claude_path / "settings.json"
    auto_memory_provenance_file = claude_path / _AUTO_MEMORY_PROVENANCE_FILENAME
    hooks_dir = claude_path / "hooks"

    if not settings_file.exists():
        result["error"] = f"Settings file not found: {settings_file}"
        return result

    # Backup settings.json with verification
    timestamp = int(time.time())
    backup_file = claude_path / f"settings.json.{timestamp}.backup"
    try:
        copy2(settings_file, backup_file)
    except OSError as e:
        logger.error("Failed to create backup of settings.json: %s", e)
        result["error"] = f"Failed to create backup: {e}"
        return result

    # Verify backup exists before proceeding
    if not backup_file.exists():
        logger.error("Backup file was not created successfully")
        result["error"] = "Backup file was not created successfully"
        return result

    # Read and parse settings.json
    try:
        with open(settings_file) as f:
            settings = json.load(f)
    except json.JSONDecodeError as e:
        logger.error("Failed to parse settings.json: %s", e)
        result["error"] = f"Failed to parse settings.json: {e}"
        return result
    except OSError as e:
        logger.error("Failed to read settings.json: %s", e)
        result["error"] = f"Failed to read settings.json: {e}"
        return result

    auto_memory_managed = False
    if auto_memory_provenance_file.exists():
        try:
            provenance = json.loads(auto_memory_provenance_file.read_text())
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("Failed to read auto-memory provenance: %s", e)
        else:
            auto_memory_managed = isinstance(provenance, dict) and provenance.get("managed") is True

    # Restore original statusLine (or remove if no downstream)
    before_mutation = json.dumps(settings, sort_keys=True)
    _restore_statusline(settings)
    hooks_removed.extend(_remove_gobby_hooks(settings))
    prior = settings.pop("_gobbyAutoMemoryPrior", None)
    if isinstance(prior, dict):
        if prior.get("existed"):
            settings["autoMemoryEnabled"] = prior.get("value")
        else:
            settings.pop("autoMemoryEnabled", None)
    elif auto_memory_managed and settings.get("autoMemoryEnabled") is False:
        settings.pop("autoMemoryEnabled", None)

    if json.dumps(settings, sort_keys=True) != before_mutation:
        # Write to temp file and atomically replace
        try:
            # Create temp file in same directory for atomic replace
            fd, temp_path = tempfile.mkstemp(
                dir=str(claude_path), suffix=".tmp", prefix="settings_"
            )
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(settings, f, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                # Atomic replace
                os.replace(temp_path, settings_file)
            except Exception:
                # Clean up temp file if it still exists
                if os.path.exists(temp_path):
                    os.unlink(temp_path)
                raise
        except OSError as e:
            logger.error("Failed to write settings.json: %s", e)
            # Attempt to restore from backup
            try:
                copy2(backup_file, settings_file)
                logger.info("Restored settings.json from backup after write failure")
            except OSError as restore_error:
                logger.error("Failed to restore from backup: %s", restore_error)
            result["error"] = f"Failed to write settings.json: {e}"
            return result

    if auto_memory_managed:
        try:
            auto_memory_provenance_file.unlink(missing_ok=True)
        except OSError as e:
            logger.error("Failed to remove auto-memory provenance: %s", e)
            result["error"] = f"Failed to remove auto-memory provenance: {e}"
            return result

    # Remove hook files (mirrors install_global_hooks)
    hook_files = ["validate_settings.py"]

    for filename in hook_files:
        file_path = hooks_dir / filename
        if file_path.exists():
            file_path.unlink()
            files_removed.append(filename)

    # Remove MCP server from global settings (~/.claude.json)
    global_settings = Path.home() / ".claude.json"
    mcp_result = remove_mcp_server_json(global_settings)
    if mcp_result["success"]:
        result["mcp_removed"] = mcp_result.get("removed", False)

    result["success"] = True
    return result
