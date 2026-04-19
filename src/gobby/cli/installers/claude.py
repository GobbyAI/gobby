"""
Claude Code installation for Gobby hooks.

This module handles installing and uninstalling Gobby hooks
and workflows for Claude Code CLI.
"""

import json
import logging
import os
import tempfile
import time
from pathlib import Path
from shutil import copy2
from typing import Any

from gobby.adapters.claude_contract import CLAUDE_PASCAL_HOOK_NAMES
from gobby.cli.utils import get_install_dir

from .hook_commands import rewrite_hook_template_commands
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


_STATUSLINE_MARKER = "statusline_handler.py"


def _configure_statusline(settings: dict[str, Any], hooks_dir: Path) -> None:
    """Configure statusLine to use Gobby's middleware, preserving any downstream.

    If statusLine already points to our handler, re-wrap to update paths.
    If it points to something else, wrap it as GOBBY_STATUSLINE_DOWNSTREAM.
    """
    handler_path = str((hooks_dir / "statusline_handler.py").resolve())
    existing = settings.get("statusLine")

    downstream: str | None = None

    if existing and isinstance(existing, dict):
        existing_cmd = existing.get("command", "")
        if _STATUSLINE_MARKER in existing_cmd:
            # Already ours — extract downstream if present
            downstream = _extract_downstream(existing_cmd)
        else:
            # Foreign command — save as downstream
            downstream = existing_cmd
    elif existing and isinstance(existing, str):
        if _STATUSLINE_MARKER in existing:
            downstream = _extract_downstream(existing)
        else:
            downstream = existing

    if downstream:
        # Shell-escape single quotes in downstream command
        escaped = downstream.replace("'", "'\\''")
        command = f"GOBBY_STATUSLINE_DOWNSTREAM='{escaped}' python3 {handler_path}"
    else:
        command = f"python3 {handler_path}"

    settings["statusLine"] = {"type": "command", "command": command}


def _extract_downstream(command: str) -> str | None:
    """Extract the downstream command from GOBBY_STATUSLINE_DOWNSTREAM='...'."""
    import re

    match = re.search(r"GOBBY_STATUSLINE_DOWNSTREAM='([^']*(?:\\'[^']*)*)'", command)
    if match:
        return match.group(1).replace("\\'", "'")
    return None


def _restore_statusline(settings: dict[str, Any]) -> None:
    """On uninstall, restore the original statusLine or remove it."""
    existing = settings.get("statusLine")
    if not existing:
        return

    cmd = existing.get("command", "") if isinstance(existing, dict) else str(existing)
    if _STATUSLINE_MARKER not in cmd:
        return  # Not ours

    downstream = _extract_downstream(cmd)
    if downstream:
        settings["statusLine"] = {"type": "command", "command": downstream}
    else:
        del settings["statusLine"]


def install_claude(project_path: Path, mode: str = "global") -> dict[str, Any]:
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
        "error": None,
    }

    hooks_dir = Path.home() / ".gobby" / "hooks"
    if mode == "global":
        claude_path = Path.home() / ".claude"
    else:
        claude_path = project_path / ".claude"
    settings_file = claude_path / "settings.json"

    # Ensure directories exist
    claude_path.mkdir(parents=True, exist_ok=True)

    # Backup existing gobby skills (now auto-synced from database)
    skills_dir = claude_path / "skills"
    backup_result = backup_gobby_skills(skills_dir)
    if backup_result["backed_up"] > 0:
        logger.info(f"Backed up {backup_result['backed_up']} existing gobby skills")

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
        logger.error(f"Failed to install hook files: {e}")
        result["error"] = f"Failed to install hook files: {e}"
        return result

    # Install shared content (plugins) - project-scoped
    try:
        content_path = claude_path if mode == "project" else project_path / ".claude"
        shared = install_shared_content(content_path, project_path)
    except Exception as e:
        logger.error(f"Failed to install shared content: {e}")
        result["error"] = f"Failed to install shared content: {e}"
        return result

    # Install CLI-specific content (can override shared)
    try:
        cli = install_cli_content("claude", claude_path)
    except Exception as e:
        logger.error(f"Failed to install CLI content: {e}")
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
            logger.error(f"Failed to create backup of settings.json: {e}")
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
            logger.error(f"Failed to parse settings.json: {e}")
            result["error"] = f"Failed to parse settings.json: {e}"
            return result
        except OSError as e:
            logger.error(f"Failed to read settings.json: {e}")
            result["error"] = f"Failed to read settings.json: {e}"
            return result

    # Load Gobby hooks from template
    try:
        with open(source_hooks_template) as f:
            gobby_settings_str = f.read()
    except OSError as e:
        logger.error(f"Failed to read hooks template: {e}")
        result["error"] = f"Failed to read hooks template: {e}"
        return result

    # Replace $HOOKS_DIR with absolute hooks directory path
    gobby_settings_str = gobby_settings_str.replace("$HOOKS_DIR", str(hooks_dir.resolve()))

    try:
        gobby_settings = json.loads(gobby_settings_str)
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse hooks template: {e}")
        result["error"] = f"Failed to parse hooks template: {e}"
        return result
    rewrite_hook_template_commands(gobby_settings, cli_name="claude", hooks_dir=hooks_dir)

    # Ensure hooks section exists
    if "hooks" not in existing_settings:
        existing_settings["hooks"] = {}

    # Merge Gobby hooks
    gobby_hooks = gobby_settings.get("hooks", {})
    for hook_type, hook_config in gobby_hooks.items():
        existing_settings["hooks"][hook_type] = hook_config
        hooks_installed.append(hook_type)

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
        logger.error(f"Failed to write settings.json: {e}")
        # Attempt to restore from backup if we have one
        if backup_file and backup_file.exists():
            try:
                copy2(backup_file, settings_file)
                logger.info("Restored settings.json from backup after write failure")
            except OSError as restore_error:
                logger.error(f"Failed to restore from backup: {restore_error}")
        result["error"] = f"Failed to write settings.json: {e}"
        return result

    # Configure statusLine for token tracking middleware
    _configure_statusline(existing_settings, hooks_dir)

    # Configure MCP server in global settings (~/.claude.json)
    # Note: Claude Code uses ~/.claude.json for user-scoped MCP servers
    global_settings = Path.home() / ".claude.json"
    mcp_result = configure_mcp_server_json(global_settings)
    if mcp_result["success"]:
        result["mcp_configured"] = mcp_result.get("added", False)
        result["mcp_already_configured"] = mcp_result.get("already_configured", False)
    else:
        # MCP config failure is non-fatal, just log it
        logger.warning(f"Failed to configure MCP server: {mcp_result['error']}")

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
        logger.error(f"Failed to create backup of settings.json: {e}")
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
        logger.error(f"Failed to parse settings.json: {e}")
        result["error"] = f"Failed to parse settings.json: {e}"
        return result
    except OSError as e:
        logger.error(f"Failed to read settings.json: {e}")
        result["error"] = f"Failed to read settings.json: {e}"
        return result

    # Restore original statusLine (or remove if no downstream)
    _restore_statusline(settings)

    if "hooks" in settings:
        for hook_type in _GOBBY_HOOK_TYPES:
            if hook_type in settings["hooks"]:
                del settings["hooks"][hook_type]
                hooks_removed.append(hook_type)

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
            logger.error(f"Failed to write settings.json: {e}")
            # Attempt to restore from backup
            try:
                copy2(backup_file, settings_file)
                logger.info("Restored settings.json from backup after write failure")
            except OSError as restore_error:
                logger.error(f"Failed to restore from backup: {restore_error}")
            result["error"] = f"Failed to write settings.json: {e}"
            return result

    # Remove hook files
    hook_files = [
        "hook_dispatcher.py",
        "validate_settings.py",
        "README.md",
        "HOOK_SCHEMAS.md",
    ]

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
