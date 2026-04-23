"""Qwen CLI installation for Gobby hooks."""

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

_HOOK_TYPES = [
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


def install_qwen(project_path: Path, mode: str = "global") -> dict[str, Any]:
    """Install Gobby integration for Qwen CLI."""
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
    qwen_path = Path.home() / ".qwen" if mode == "global" else project_path / ".qwen"
    settings_file = qwen_path / "settings.json"
    qwen_path.mkdir(parents=True, exist_ok=True)

    install_dir = get_install_dir()
    qwen_install_dir = install_dir / "qwen"
    source_hooks_template = qwen_install_dir / "hooks-template.json"
    if not source_hooks_template.exists():
        result["error"] = f"Missing hooks template: {source_hooks_template}"
        return result

    install_global_hooks()
    cleaned = clean_project_hooks(project_path / ".qwen" / "settings.json")
    if cleaned:
        result["project_hooks_cleaned"] = cleaned

    content_path = qwen_path if mode == "project" else project_path / ".qwen"
    shared = install_shared_content(content_path, project_path)
    cli = install_cli_content("qwen", qwen_path)

    result["workflows_installed"] = []
    result["agents_installed"] = shared.get("agents", [])
    result["commands_installed"] = cli.get("commands", [])
    result["plugins_installed"] = shared.get("plugins", [])

    skills_dir = qwen_path / "skills"
    router_skills = install_router_skills_as_gemini_skills(skills_dir)
    result["commands_installed"].extend(router_skills)

    if settings_file.exists():
        timestamp = int(time.time())
        backup_file = qwen_path / f"settings.json.{timestamp}.backup"
        copy2(settings_file, backup_file)

    if settings_file.exists():
        try:
            with open(settings_file) as f:
                existing_settings = json.load(f)
        except json.JSONDecodeError as exc:
            result["error"] = f"settings.json is malformed: {exc}"
            return result
    else:
        existing_settings = {}

    with open(source_hooks_template) as f:
        gobby_settings_str = f.read()

    gobby_settings_str = gobby_settings_str.replace("$HOOKS_DIR", str(hooks_dir.resolve()))

    gobby_settings = json.loads(gobby_settings_str)
    rewrite_hook_template_commands(
        gobby_settings,
        cli_name="qwen",
        hooks_dir=hooks_dir,
    )

    if "hooks" not in existing_settings:
        existing_settings["hooks"] = {}
    for hook_type, hook_config in gobby_settings.get("hooks", {}).items():
        existing_settings["hooks"][hook_type] = hook_config
        hooks_installed.append(hook_type)

    existing_settings.setdefault("general", {})
    existing_settings["general"]["enableHooks"] = True

    existing_settings.setdefault("ui", {})
    existing_settings["ui"]["hideTips"] = True

    with open(settings_file, "w") as f:
        json.dump(existing_settings, f, indent=2)

    global_settings = Path.home() / ".qwen" / "settings.json"
    mcp_result = configure_mcp_server_json(global_settings)
    if mcp_result["success"]:
        result["mcp_configured"] = mcp_result.get("added", False)
        result["mcp_already_configured"] = mcp_result.get("already_configured", False)
    else:
        logger.warning(f"Failed to configure MCP server: {mcp_result['error']}")

    scripts_installed = _install_agent_scripts(install_dir)
    result["scripts_installed"] = scripts_installed

    result["success"] = True
    return result


def _install_agent_scripts(install_dir: Path) -> list[str]:
    """Install shared agent scripts to ~/.gobby/scripts/."""
    scripts_installed: list[str] = []
    source_scripts_dir = install_dir / "shared" / "scripts"
    target_scripts_dir = Path.home() / ".gobby" / "scripts"

    if not source_scripts_dir.exists():
        logger.debug(f"No scripts directory found at {source_scripts_dir}")
        return scripts_installed

    target_scripts_dir.mkdir(parents=True, exist_ok=True)

    for script_file in source_scripts_dir.glob("*.sh"):
        target_file = target_scripts_dir / script_file.name
        copy2(script_file, target_file)
        target_file.chmod(0o755)
        scripts_installed.append(script_file.name)
        logger.debug(f"Installed script: {script_file.name}")

    return scripts_installed


def uninstall_qwen(project_path: Path, mode: str = "project") -> dict[str, Any]:
    """Uninstall Gobby integration from Qwen CLI."""
    hooks_removed: list[str] = []
    files_removed: list[str] = []
    result: dict[str, Any] = {
        "success": False,
        "hooks_removed": hooks_removed,
        "files_removed": files_removed,
        "mcp_removed": False,
        "error": None,
    }

    qwen_path = Path.home() / ".qwen" if mode == "global" else project_path / ".qwen"
    settings_file = qwen_path / "settings.json"
    hooks_dir = qwen_path / "hooks"

    if not settings_file.exists():
        result["success"] = True
        return result

    timestamp = int(time.time())
    backup_file = qwen_path / f"settings.json.{timestamp}.backup"
    copy2(settings_file, backup_file)

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
        for hook_type in _HOOK_TYPES:
            if hook_type in settings["hooks"]:
                del settings["hooks"][hook_type]
                hooks_removed.append(hook_type)

        if not settings["hooks"]:
            del settings["hooks"]

    with open(settings_file, "w") as f:
        json.dump(settings, f, indent=2)

    if hooks_dir.exists():
        for hook_file in hooks_dir.iterdir():
            if hook_file.is_file():
                hook_file.unlink()
                files_removed.append(f"hooks/{hook_file.name}")
        try:
            hooks_dir.rmdir()
        except OSError:
            pass

    global_settings = Path.home() / ".qwen" / "settings.json"
    mcp_result = remove_mcp_server_json(global_settings)
    if mcp_result["success"]:
        result["mcp_removed"] = mcp_result.get("removed", False)
    else:
        logger.warning(f"Failed to remove MCP server: {mcp_result['error']}")

    result["success"] = True
    return result
