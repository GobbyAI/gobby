"""
Gemini CLI installation for Gobby hooks.

This module handles installing and uninstalling Gobby hooks
and workflows for Gemini CLI.
"""

import json
import logging
import time
from pathlib import Path
from shutil import copy2
from typing import Any

from gobby.cli.utils import get_install_dir

from .hook_commands import rewrite_hook_template_commands
from .mcp_config import configure_mcp_server_json, remove_mcp_server_json
from .shared import (
    clean_project_hooks,
    install_cli_content,
    install_global_hooks,
    install_shared_content,
)
from .skill_install import install_router_skills_as_gemini_skills

logger = logging.getLogger(__name__)


def install_gemini(project_path: Path, mode: str = "global") -> dict[str, Any]:
    """Install Gobby integration for Gemini CLI (hooks, workflows).

    Args:
        project_path: Path to the project root
        mode: "global" installs hooks to ~/.gobby/hooks/ and settings to
            ~/.gemini/settings.json. "project" installs per-project (existing behavior).

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
        gemini_path = Path.home() / ".gemini"
    else:
        gemini_path = project_path / ".gemini"
    settings_file = gemini_path / "settings.json"

    # Ensure directories exist
    gemini_path.mkdir(parents=True, exist_ok=True)

    # Get source files
    install_dir = get_install_dir()
    gemini_install_dir = install_dir / "gemini"
    source_hooks_template = gemini_install_dir / "hooks-template.json"

    if not source_hooks_template.exists():
        result["error"] = f"Missing hooks template: {source_hooks_template}"
        return result

    # Install hook files (always global)
    install_global_hooks()
    # Clean up project-level hooks to prevent double-firing
    cleaned = clean_project_hooks(project_path / ".gemini" / "settings.json")
    if cleaned:
        result["project_hooks_cleaned"] = cleaned

    # Install shared content (plugins) - project-scoped
    content_path = gemini_path if mode == "project" else project_path / ".gemini"
    shared = install_shared_content(content_path, project_path)
    # Install CLI-specific content (can override shared)
    cli = install_cli_content("gemini", gemini_path)

    # Skills are now auto-synced to database on daemon startup (sync_bundled_skills)
    # No longer need to copy to .gemini/skills/

    result["workflows_installed"] = []  # DB-managed via sync_bundled_content_to_db()
    result["agents_installed"] = shared.get("agents", [])
    result["commands_installed"] = cli.get("commands", [])
    result["plugins_installed"] = shared.get("plugins", [])

    # Install router skills (gobby, g) as Gemini skills
    skills_dir = gemini_path / "skills"
    router_skills = install_router_skills_as_gemini_skills(skills_dir)
    result["commands_installed"].extend(router_skills)

    # Backup existing settings.json if it exists
    if settings_file.exists():
        timestamp = int(time.time())
        backup_file = gemini_path / f"settings.json.{timestamp}.backup"
        copy2(settings_file, backup_file)

    # Load existing settings or create empty
    if settings_file.exists():
        try:
            with open(settings_file) as f:
                existing_settings = json.load(f)
        except json.JSONDecodeError:
            # If invalid JSON, treat as empty but warn (backup already made)
            existing_settings = {}
    else:
        existing_settings = {}

    # Load Gobby hooks from template
    with open(source_hooks_template) as f:
        gobby_settings_str = f.read()

    # Replace $HOOKS_DIR with absolute hooks directory path
    gobby_settings_str = gobby_settings_str.replace("$HOOKS_DIR", str(hooks_dir.resolve()))

    gobby_settings = json.loads(gobby_settings_str)
    rewrite_hook_template_commands(
        gobby_settings,
        cli_name="gemini",
        hooks_dir=hooks_dir,
    )

    # Ensure hooks section exists
    if "hooks" not in existing_settings:
        existing_settings["hooks"] = {}

    # Merge Gobby hooks (preserving any existing hooks)
    gobby_hooks = gobby_settings.get("hooks", {})
    for hook_type, hook_config in gobby_hooks.items():
        existing_settings["hooks"][hook_type] = hook_config
        hooks_installed.append(hook_type)

    # Crucially, ensure hooks are enabled in Gemini CLI
    if "general" not in existing_settings:
        existing_settings["general"] = {}
    existing_settings["general"]["enableHooks"] = True

    # Disable tip prompts — they block agent input in headless/YOLO mode
    if "ui" not in existing_settings:
        existing_settings["ui"] = {}
    existing_settings["ui"]["hideTips"] = True

    # Write merged settings back
    with open(settings_file, "w") as f:
        json.dump(existing_settings, f, indent=2)

    # Configure MCP server in global settings (~/.gemini/settings.json)
    global_settings = Path.home() / ".gemini" / "settings.json"
    mcp_result = configure_mcp_server_json(global_settings)
    if mcp_result["success"]:
        result["mcp_configured"] = mcp_result.get("added", False)
        result["mcp_already_configured"] = mcp_result.get("already_configured", False)
    else:
        # MCP config failure is non-fatal, just log it
        logger.warning(f"Failed to configure MCP server: {mcp_result['error']}")

    # Install agent scripts (used by meeseeks workflow)
    scripts_installed = _install_agent_scripts(install_dir)
    result["scripts_installed"] = scripts_installed

    result["success"] = True
    return result


def _install_agent_scripts(install_dir: Path) -> list[str]:
    """Install shared agent scripts to ~/.gobby/scripts/.

    Installs scripts like agent_shutdown.sh used by workflows.

    Args:
        install_dir: Path to the install source directory

    Returns:
        List of installed script names
    """
    scripts_installed: list[str] = []
    source_scripts_dir = install_dir / "shared" / "scripts"
    target_scripts_dir = Path.home() / ".gobby" / "scripts"

    if not source_scripts_dir.exists():
        logger.debug(f"No scripts directory found at {source_scripts_dir}")
        return scripts_installed

    # Ensure target directory exists
    target_scripts_dir.mkdir(parents=True, exist_ok=True)

    # Copy all scripts
    for script_file in source_scripts_dir.glob("*.sh"):
        target_file = target_scripts_dir / script_file.name
        copy2(script_file, target_file)
        # Make executable
        target_file.chmod(0o755)
        scripts_installed.append(script_file.name)
        logger.debug(f"Installed script: {script_file.name}")

    return scripts_installed


def uninstall_gemini(project_path: Path) -> dict[str, Any]:
    """Uninstall Gobby integration from Gemini CLI.

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

    gemini_path = project_path / ".gemini"
    settings_file = gemini_path / "settings.json"
    hooks_dir = gemini_path / "hooks"

    if not settings_file.exists():
        # No settings file means nothing to uninstall
        result["success"] = True
        return result

    # Backup settings.json
    timestamp = int(time.time())
    backup_file = gemini_path / f"settings.json.{timestamp}.backup"
    copy2(settings_file, backup_file)

    # Remove hooks from settings.json
    try:
        with open(settings_file) as f:
            settings = json.load(f)
    except json.JSONDecodeError as e:
        logger.warning(
            "Could not parse %s (%s); treating as empty and continuing uninstall",
            settings_file,
            e,
        )
        settings = {}

    if "hooks" in settings:
        hook_types = [
            "SessionStart",
            "SessionEnd",
            "BeforeAgent",
            "AfterAgent",
            "BeforeTool",
            "AfterTool",
            "BeforeToolSelection",
            "BeforeModel",
            "AfterModel",
            "PreCompress",
            "Notification",
        ]

        for hook_type in hook_types:
            if hook_type in settings["hooks"]:
                del settings["hooks"][hook_type]
                hooks_removed.append(hook_type)

        # Also remove the "general" section if "enableHooks" was the only entry
        if "general" in settings and settings["general"].get("enableHooks") is True:
            # Check if there are other entries in "general"
            if len(settings["general"]) == 1:
                del settings["general"]
            else:
                del settings["general"]["enableHooks"]

        with open(settings_file, "w") as f:
            json.dump(settings, f, indent=2)

    # Remove hook dispatcher
    dispatcher_file = hooks_dir / "hook_dispatcher.py"
    if dispatcher_file.exists():
        dispatcher_file.unlink()
        files_removed.append("hook_dispatcher.py")

    # Attempt to remove empty hooks directory
    try:
        if hooks_dir.exists() and not any(hooks_dir.iterdir()):
            hooks_dir.rmdir()
    except Exception as e:
        logger.debug(f"Best-effort cleanup of hooks directory failed: {e}")

    # Remove MCP server from global settings (~/.gemini/settings.json)
    global_settings = Path.home() / ".gemini" / "settings.json"
    mcp_result = remove_mcp_server_json(global_settings)
    if mcp_result["success"]:
        result["mcp_removed"] = mcp_result.get("removed", False)

    result["success"] = True
    return result
