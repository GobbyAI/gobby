"""
Installation commands for hooks.
"""

import json
import logging
import re
import shutil
import sys
import time
from pathlib import Path
from shutil import copy2, copytree
from typing import Any

import click

from .utils import get_install_dir

logger = logging.getLogger(__name__)


def _install_shared_content(cli_path: Path, project_path: Path) -> dict[str, list[str]]:
    """Install shared content from src/install/shared/.

    Skills are CLI-specific and go to {cli_path}/skills/.
    Workflows are cross-CLI and go to {project_path}/.gobby/workflows/.
    """
    shared_dir = get_install_dir() / "shared"
    installed: dict[str, list[str]] = {"skills": [], "workflows": []}

    # Install shared skills to CLI directory
    shared_skills = shared_dir / "skills"
    if shared_skills.exists():
        target_skills = cli_path / "skills"
        target_skills.mkdir(parents=True, exist_ok=True)
        for skill_dir in shared_skills.iterdir():
            if skill_dir.is_dir():
                target_skill = target_skills / skill_dir.name
                if target_skill.exists():
                    shutil.rmtree(target_skill)
                copytree(skill_dir, target_skill)
                installed["skills"].append(skill_dir.name)

    # Install shared workflows to .gobby/workflows/ (cross-CLI)
    shared_workflows = shared_dir / "workflows"
    if shared_workflows.exists():
        target_workflows = project_path / ".gobby" / "workflows"
        target_workflows.mkdir(parents=True, exist_ok=True)
        for workflow_file in shared_workflows.iterdir():
            if workflow_file.is_file():
                copy2(workflow_file, target_workflows / workflow_file.name)
                installed["workflows"].append(workflow_file.name)

    return installed


def _install_cli_content(cli_name: str, target_path: Path) -> dict[str, list[str]]:
    """Install CLI-specific skills/workflows (layered on top of shared).

    CLI-specific content can add to or override shared content.
    """
    cli_dir = get_install_dir() / cli_name
    installed: dict[str, list[str]] = {"skills": [], "workflows": []}

    # CLI-specific skills (can override shared)
    cli_skills = cli_dir / "skills"
    if cli_skills.exists():
        target_skills = target_path / "skills"
        target_skills.mkdir(parents=True, exist_ok=True)
        for skill_dir in cli_skills.iterdir():
            if skill_dir.is_dir():
                target_skill = target_skills / skill_dir.name
                if target_skill.exists():
                    shutil.rmtree(target_skill)
                copytree(skill_dir, target_skill)
                installed["skills"].append(skill_dir.name)

    # CLI-specific workflows
    cli_workflows = cli_dir / "workflows"
    if cli_workflows.exists():
        target_workflows = target_path / "workflows"
        target_workflows.mkdir(parents=True, exist_ok=True)
        for workflow_file in cli_workflows.iterdir():
            if workflow_file.is_file():
                copy2(workflow_file, target_workflows / workflow_file.name)
                installed["workflows"].append(workflow_file.name)

    return installed


def _is_claude_code_installed() -> bool:
    """Check if Claude Code CLI is installed."""
    return shutil.which("claude") is not None


def _is_gemini_cli_installed() -> bool:
    """Check if Gemini CLI is installed."""
    return shutil.which("gemini") is not None


def _is_codex_cli_installed() -> bool:
    """Check if OpenAI Codex CLI is installed."""
    return shutil.which("codex") is not None


def _install_claude(project_path: Path) -> dict[str, Any]:
    """Install Gobby integration for Claude Code (hooks, skills, workflows)."""
    hooks_installed: list[str] = []
    result: dict[str, Any] = {
        "success": False,
        "hooks_installed": hooks_installed,
        "skills_installed": [],
        "workflows_installed": [],
        "error": None,
    }

    claude_path = project_path / ".claude"
    settings_file = claude_path / "settings.json"

    # Ensure .claude subdirectories exist
    claude_path.mkdir(parents=True, exist_ok=True)
    hooks_dir = claude_path / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)

    # Get source files
    install_dir = get_install_dir()
    claude_install_dir = install_dir / "claude"
    install_hooks_dir = claude_install_dir / "hooks"

    # Hook files to copy
    hook_files = {
        "hook_dispatcher.py": True,  # Make executable
        "validate_settings.py": True,  # Make executable
    }

    source_hooks_template = claude_install_dir / "hooks-template.json"

    # Verify all source files exist
    missing_files = []
    for filename in hook_files.keys():
        source_file = install_hooks_dir / filename
        if not source_file.exists():
            missing_files.append(str(source_file))

    if not source_hooks_template.exists():
        missing_files.append(str(source_hooks_template))

    if missing_files:
        result["error"] = f"Missing source files: {missing_files}"
        return result

    # Copy hook files
    for filename, make_executable in hook_files.items():
        source_file = install_hooks_dir / filename
        target_file = hooks_dir / filename

        if target_file.exists():
            target_file.unlink()

        copy2(source_file, target_file)
        if make_executable:
            target_file.chmod(0o755)

    # Install shared content (skills, workflows)
    shared = _install_shared_content(claude_path, project_path)
    # Install CLI-specific content (can override shared)
    cli = _install_cli_content("claude", claude_path)

    result["skills_installed"] = shared["skills"] + cli["skills"]
    result["workflows_installed"] = shared["workflows"] + cli["workflows"]

    # Backup existing settings.json if it exists
    if settings_file.exists():
        timestamp = int(time.time())
        backup_file = claude_path / f"settings.json.{timestamp}.backup"
        copy2(settings_file, backup_file)

    # Load existing settings or create empty
    if settings_file.exists():
        with open(settings_file) as f:
            existing_settings = json.load(f)
    else:
        existing_settings = {}

    # Load Gobby hooks from template
    with open(source_hooks_template) as f:
        gobby_settings_str = f.read()

    # Replace $PROJECT_PATH with absolute project path
    abs_project_path = str(project_path.resolve())
    gobby_settings_str = gobby_settings_str.replace("$PROJECT_PATH", abs_project_path)
    gobby_settings = json.loads(gobby_settings_str)

    # Ensure hooks section exists
    if "hooks" not in existing_settings:
        existing_settings["hooks"] = {}

    # Merge Gobby hooks
    gobby_hooks = gobby_settings.get("hooks", {})
    for hook_type, hook_config in gobby_hooks.items():
        existing_settings["hooks"][hook_type] = hook_config
        hooks_installed.append(hook_type)

    # Write merged settings back
    with open(settings_file, "w") as f:
        json.dump(existing_settings, f, indent=2)

    result["success"] = True
    return result


def _install_gemini(project_path: Path) -> dict[str, Any]:
    """Install Gobby integration for Gemini CLI (hooks, skills, workflows)."""
    hooks_installed: list[str] = []
    result: dict[str, Any] = {
        "success": False,
        "hooks_installed": hooks_installed,
        "skills_installed": [],
        "workflows_installed": [],
        "error": None,
    }

    gemini_path = project_path / ".gemini"
    settings_file = gemini_path / "settings.json"

    # Ensure .gemini subdirectories exist
    gemini_path.mkdir(parents=True, exist_ok=True)
    hooks_dir = gemini_path / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)

    # Get source files
    install_dir = get_install_dir()
    gemini_install_dir = install_dir / "gemini"
    install_hooks_dir = gemini_install_dir / "hooks"
    source_hooks_template = gemini_install_dir / "hooks-template.json"

    # Verify source files exist
    dispatcher_file = install_hooks_dir / "hook_dispatcher.py"
    if not dispatcher_file.exists():
        result["error"] = f"Missing hook dispatcher: {dispatcher_file}"
        return result

    if not source_hooks_template.exists():
        result["error"] = f"Missing hooks template: {source_hooks_template}"
        return result

    # Copy hook dispatcher
    target_dispatcher = hooks_dir / "hook_dispatcher.py"
    if target_dispatcher.exists():
        target_dispatcher.unlink()
    copy2(dispatcher_file, target_dispatcher)
    target_dispatcher.chmod(0o755)

    # Install shared content (skills, workflows)
    shared = _install_shared_content(gemini_path, project_path)
    # Install CLI-specific content (can override shared)
    cli = _install_cli_content("gemini", gemini_path)

    result["skills_installed"] = shared["skills"] + cli["skills"]
    result["workflows_installed"] = shared["workflows"] + cli["workflows"]

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

    # Resolve uv path dynamically to avoid PATH issues in Gemini CLI
    from shutil import which

    uv_path = which("uv")
    if not uv_path:
        uv_path = "uv"  # Fallback

    # Replace $PROJECT_PATH with absolute project path
    abs_project_path = str(project_path.resolve())

    # Replace variables in template
    gobby_settings_str = gobby_settings_str.replace("$PROJECT_PATH", abs_project_path)

    # Also replace "uv run python" with absolute path if found
    # The template uses "uv run python" by default
    if uv_path != "uv":
        gobby_settings_str = gobby_settings_str.replace("uv run python", f"{uv_path} run python")

    gobby_settings = json.loads(gobby_settings_str)

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

    # Write merged settings back
    with open(settings_file, "w") as f:
        json.dump(existing_settings, f, indent=2)

    result["success"] = True
    return result


def _install_codex_notify() -> dict[str, Any]:
    """Install Codex notify script and configure ~/.codex/config.toml."""
    import json as _json

    files_installed: list[str] = []
    result: dict[str, Any] = {
        "success": False,
        "files_installed": files_installed,
        "skills_installed": [],
        "workflows_installed": [],
        "config_updated": False,
        "error": None,
    }

    install_dir = get_install_dir()
    source_notify = install_dir / "codex" / "hooks" / "hook_dispatcher.py"
    if not source_notify.exists():
        result["error"] = f"Missing source file: {source_notify}"
        return result

    # Install hook dispatcher to ~/.gobby/hooks/codex/hook_dispatcher.py
    notify_dir = Path.home() / ".gobby" / "hooks" / "codex"
    notify_dir.mkdir(parents=True, exist_ok=True)
    target_notify = notify_dir / "hook_dispatcher.py"

    if target_notify.exists():
        target_notify.unlink()

    copy2(source_notify, target_notify)
    target_notify.chmod(0o755)
    files_installed.append(str(target_notify))

    # Install shared content - skills to ~/.codex, workflows to ~/.gobby
    codex_home = Path.home() / ".codex"
    gobby_home = Path.home()  # workflows go to ~/.gobby/workflows/
    shared = _install_shared_content(codex_home, gobby_home)
    # Install CLI-specific content (can override shared)
    cli = _install_cli_content("codex", codex_home)

    result["skills_installed"] = shared["skills"] + cli["skills"]
    result["workflows_installed"] = shared["workflows"] + cli["workflows"]

    # Update ~/.codex/config.toml
    codex_config_dir = codex_home

    codex_config_dir.mkdir(parents=True, exist_ok=True)
    codex_config_path = codex_config_dir / "config.toml"

    notify_command = ["python3", str(target_notify)]
    notify_line = f"notify = {_json.dumps(notify_command)}"

    try:
        if codex_config_path.exists():
            existing = codex_config_path.read_text(encoding="utf-8")
        else:
            existing = ""

        pattern = re.compile(r"(?m)^\\s*notify\\s*=.*$")
        if pattern.search(existing):
            updated = pattern.sub(notify_line, existing)
        else:
            updated = (existing.rstrip() + "\n\n" if existing.strip() else "") + notify_line + "\n"

        if updated != existing:
            if codex_config_path.exists():
                backup_path = codex_config_path.with_suffix(".toml.bak")
                backup_path.write_text(existing, encoding="utf-8")

            codex_config_path.write_text(updated, encoding="utf-8")
            result["config_updated"] = True

        result["success"] = True
        return result

    except Exception as e:
        result["error"] = f"Failed to update Codex config: {e}"
        return result


def _uninstall_claude(project_path: Path) -> dict[str, Any]:
    """Uninstall Gobby integration from Claude Code."""
    import shutil

    hooks_removed: list[str] = []
    files_removed: list[str] = []
    skills_removed: list[str] = []
    result: dict[str, Any] = {
        "success": False,
        "hooks_removed": hooks_removed,
        "files_removed": files_removed,
        "skills_removed": skills_removed,
        "error": None,
    }

    claude_path = project_path / ".claude"
    settings_file = claude_path / "settings.json"
    hooks_dir = claude_path / "hooks"
    skills_dir = claude_path / "skills"

    if not settings_file.exists():
        result["error"] = f"Settings file not found: {settings_file}"
        return result

    # Backup settings.json
    timestamp = int(time.time())
    backup_file = claude_path / f"settings.json.{timestamp}.backup"
    copy2(settings_file, backup_file)

    # Remove hooks from settings.json
    with open(settings_file) as f:
        settings = json.load(f)

    if "hooks" in settings:
        hook_types = [
            "SessionStart",
            "SessionEnd",
            "UserPromptSubmit",
            "PreToolUse",
            "PostToolUse",
            "PreCompact",
            "Notification",
            "Stop",
            "SubagentStart",
            "SubagentStop",
            "PermissionRequest",
        ]

        for hook_type in hook_types:
            if hook_type in settings["hooks"]:
                del settings["hooks"][hook_type]
                hooks_removed.append(hook_type)

        with open(settings_file, "w") as f:
            json.dump(settings, f, indent=2)

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

    # Remove Gobby skills
    install_dir = get_install_dir()
    install_skills_dir = install_dir / "claude" / "skills"

    if install_skills_dir.exists():
        for skill_dir in install_skills_dir.iterdir():
            if skill_dir.is_dir():
                target_skill_dir = skills_dir / skill_dir.name
                if target_skill_dir.exists():
                    shutil.rmtree(target_skill_dir)
                    skills_removed.append(skill_dir.name)

    result["success"] = True
    return result


def _uninstall_gemini(project_path: Path) -> dict[str, Any]:
    """Uninstall Gobby integration from Gemini CLI."""
    hooks_removed: list[str] = []
    files_removed: list[str] = []
    result: dict[str, Any] = {
        "success": False,
        "hooks_removed": hooks_removed,
        "files_removed": files_removed,
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
    with open(settings_file) as f:
        settings = json.load(f)

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
    except Exception:
        pass

    result["success"] = True
    return result


def _uninstall_codex_notify() -> dict[str, Any]:
    """Uninstall Codex notify script and remove from ~/.codex/config.toml."""
    files_removed: list[str] = []
    result: dict[str, Any] = {
        "success": False,
        "files_removed": files_removed,
        "config_updated": False,
        "error": None,
    }

    # Remove hook dispatcher from ~/.gobby/hooks/codex/hook_dispatcher.py
    notify_file = Path.home() / ".gobby" / "hooks" / "codex" / "hook_dispatcher.py"
    if notify_file.exists():
        notify_file.unlink()
        files_removed.append(str(notify_file))

    # Try to remove empty parent directories
    notify_dir = notify_file.parent
    try:
        if notify_dir.exists() and not any(notify_dir.iterdir()):
            notify_dir.rmdir()
    except Exception:
        pass

    # Update ~/.codex/config.toml to remove notify line
    codex_config_path = Path.home() / ".codex" / "config.toml"

    try:
        if codex_config_path.exists():
            existing = codex_config_path.read_text(encoding="utf-8")

            # Remove notify = [...] line
            pattern = re.compile(r"(?m)^\s*notify\s*=.*$\n?")
            if pattern.search(existing):
                updated = pattern.sub("", existing)

                # Clean up multiple blank lines
                updated = re.sub(r"\n{3,}", "\n\n", updated)

                if updated != existing:
                    # Backup before modifying
                    backup_path = codex_config_path.with_suffix(".toml.bak")
                    backup_path.write_text(existing, encoding="utf-8")

                    codex_config_path.write_text(updated, encoding="utf-8")
                    result["config_updated"] = True

        result["success"] = True
        return result

    except Exception as e:
        result["error"] = f"Failed to update Codex config: {e}"
        return result


def _install_git_hooks(project_path: Path) -> dict[str, Any]:
    """Install Gobby git hooks to the current repository."""
    git_dir = project_path / ".git"
    if not git_dir.exists():
        return {"success": False, "error": "Not a git repository (no .git directory found)"}

    hooks_dir = git_dir / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)

    source_hooks_dir = get_install_dir() / "hooks" / "git"
    if not source_hooks_dir.exists():
        # Fallback for dev mode
        source_hooks_dir = get_install_dir().parent / "hooks" / "git"

    if not source_hooks_dir.exists():
        return {"success": False, "error": f"Source hooks not found in {source_hooks_dir}"}

    installed = []

    # Map source filenames to git hook names
    hook_map = {"post-merge": "post-merge", "pre-commit": "pre-commit"}

    for source_name, target_name in hook_map.items():
        source_file = source_hooks_dir / source_name
        target_file = hooks_dir / target_name

        if not source_file.exists():
            continue

        # If hook exists, we need to be careful. code logic below:
        # 1. If it's ours (check content?), replace it?
        # 2. If it's user's, append our call?
        # For this MVP, we will simpler: if file exists, warn and skip unless we spot our marker.
        # Actually, let's just append our logic if not present.

        hook_content = source_file.read_text()

        if target_file.exists():
            current_content = target_file.read_text()
            if "Gobby Task Auto-Sync" in current_content:
                # Already installed, maybe update?
                # For now, assume it's fine.
                pass
            else:
                # Append to existing
                with open(target_file, "a") as f:
                    f.write("\n" + hook_content)
                target_file.chmod(0o755)
                installed.append(target_name)
        else:
            # Create new
            copy2(source_file, target_file)
            target_file.chmod(0o755)
            installed.append(target_name)

    return {"success": True, "installed": installed}


def _install_antigravity(project_path: Path) -> dict[str, Any]:
    """Install Gobby integration for Antigravity agent (hooks, skills, workflows)."""
    hooks_installed: list[str] = []
    result: dict[str, Any] = {
        "success": False,
        "hooks_installed": hooks_installed,
        "skills_installed": [],
        "workflows_installed": [],
        "error": None,
    }

    antigravity_path = project_path / ".antigravity"
    settings_file = antigravity_path / "settings.json"

    # Ensure .antigravity subdirectories exist
    antigravity_path.mkdir(parents=True, exist_ok=True)
    hooks_dir = antigravity_path / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)

    # Get source files
    install_dir = get_install_dir()
    antigravity_install_dir = install_dir / "antigravity"
    install_hooks_dir = antigravity_install_dir / "hooks"
    source_hooks_template = antigravity_install_dir / "hooks-template.json"

    # Verify source files exist
    dispatcher_file = install_hooks_dir / "hook_dispatcher.py"
    if not dispatcher_file.exists():
        result["error"] = f"Missing hook dispatcher: {dispatcher_file}"
        return result

    if not source_hooks_template.exists():
        result["error"] = f"Missing hooks template: {source_hooks_template}"
        return result

    # Copy hook dispatcher
    target_dispatcher = hooks_dir / "hook_dispatcher.py"
    if target_dispatcher.exists():
        target_dispatcher.unlink()
    copy2(dispatcher_file, target_dispatcher)
    target_dispatcher.chmod(0o755)

    # Install shared content (skills, workflows)
    shared = _install_shared_content(antigravity_path, project_path)
    # Install CLI-specific content (can override shared)
    cli = _install_cli_content("antigravity", antigravity_path)

    result["skills_installed"] = shared["skills"] + cli["skills"]
    result["workflows_installed"] = shared["workflows"] + cli["workflows"]

    # Backup existing settings.json if it exists
    if settings_file.exists():
        timestamp = int(time.time())
        backup_file = antigravity_path / f"settings.json.{timestamp}.backup"
        copy2(settings_file, backup_file)

    # Load existing settings or create empty
    if settings_file.exists():
        try:
            with open(settings_file) as f:
                existing_settings = json.load(f)
        except json.JSONDecodeError:
            existing_settings = {}
    else:
        existing_settings = {}

    # Load Gobby hooks from template
    with open(source_hooks_template) as f:
        gobby_settings_str = f.read()

    # Resolve uv path
    from shutil import which

    uv_path = which("uv")
    if not uv_path:
        uv_path = "uv"

    abs_project_path = str(project_path.resolve())

    # Replace variables
    gobby_settings_str = gobby_settings_str.replace("$PROJECT_PATH", abs_project_path)
    if uv_path != "uv":
        gobby_settings_str = gobby_settings_str.replace("uv run python", f"{uv_path} run python")

    gobby_settings = json.loads(gobby_settings_str)

    # Ensure hooks section exists
    if "hooks" not in existing_settings:
        existing_settings["hooks"] = {}

    # Merge Gobby hooks
    gobby_hooks = gobby_settings.get("hooks", {})
    for hook_type, hook_config in gobby_hooks.items():
        existing_settings["hooks"][hook_type] = hook_config
        hooks_installed.append(hook_type)

    # Enable hooks
    if "general" not in existing_settings:
        existing_settings["general"] = {}
    existing_settings["general"]["enableHooks"] = True

    # Write settings
    with open(settings_file, "w") as f:
        json.dump(existing_settings, f, indent=2)

    result["success"] = True
    return result


@click.command("install")
@click.option(
    "--claude",
    "install_claude",
    is_flag=True,
    help="Install Claude Code hooks only",
)
@click.option(
    "--gemini",
    "install_gemini",
    is_flag=True,
    help="Install Gemini CLI hooks only",
)
@click.option(
    "--codex",
    "install_codex",
    is_flag=True,
    help="Configure Codex notify integration (interactive Codex)",
)
@click.option(
    "--hooks",
    "install_hooks",
    is_flag=True,
    help="Install Git hooks for task auto-sync",
)
@click.option(
    "--all",
    "install_all",
    is_flag=True,
    default=False,
    help="Install hooks for all detected CLIs (default behavior when no flags specified)",
)
@click.option(
    "--antigravity",
    "install_antigravity",
    is_flag=True,
    help="Install Antigravity agent hooks (internal)",
)
def install(
    install_claude: bool,
    install_gemini: bool,
    install_codex: bool,
    install_hooks: bool,
    install_all: bool,
    install_antigravity: bool,
) -> None:
    """Install Gobby hooks to AI coding CLIs and Git.

    By default (no flags), installs to all detected CLIs.
    Use --claude, --gemini, --codex to install only to specific CLIs.
    Use --hooks to install Git hooks for task auto-sync.
    """
    project_path = Path.cwd()

    # Determine which CLIs to install
    # If no flags specified, act like --all (but don't force git hooks unless implied or explicit)
    # Actually, let's keep git hooks opt-in or part of --all?
    # Let's make --all include git hooks if we are in a git repo?
    # For safety, let's make git hooks explicit or part of --all if user approves?
    # Requirement: "Users must run this command explicitly to enable auto-sync"
    # So --all might NOT include hooks by default in this logic unless we change policy.
    # Let's explicitly check flags.

    if (
        not install_claude
        and not install_gemini
        and not install_codex
        and not install_hooks
        and not install_all
        and not install_antigravity
    ):
        install_all = True

    codex_detected = _is_codex_cli_installed()

    # Build list of CLIs to install
    clis_to_install = []

    if install_all:
        # Auto-detect installed CLIs
        if _is_claude_code_installed():
            clis_to_install.append("claude")
        if _is_gemini_cli_installed():
            clis_to_install.append("gemini")
        if codex_detected:
            clis_to_install.append("codex")

        # Check for git
        if (project_path / ".git").exists():
            install_hooks = True  # Include git hooks in --all? Or leave separate?
            # Let's include them in --all for "complete setup", but maybe log it clearly.

        if not clis_to_install and not install_hooks:
            click.echo("No supported AI coding CLIs detected.")
            click.echo("\nSupported CLIs:")
            click.echo("  - Claude Code: npm install -g @anthropic-ai/claude-code")
            click.echo("  - Gemini CLI:  npm install -g @google/gemini-cli")
            click.echo("  - Codex CLI:   npm install -g @openai/codex")
            click.echo(
                "\nYou can still install manually with --claude, --gemini, or --codex flags."
            )
            sys.exit(1)
    else:
        if install_claude:
            clis_to_install.append("claude")
        if install_gemini:
            clis_to_install.append("gemini")
        if install_codex:
            clis_to_install.append("codex")
        if install_antigravity:
            clis_to_install.append("antigravity")

    # Get install directory info
    install_dir = get_install_dir()
    is_dev_mode = "src" in str(install_dir)

    click.echo("=" * 60)
    click.echo("  Gobby Hooks Installation")
    click.echo("=" * 60)
    click.echo(f"\nProject: {project_path}")
    if is_dev_mode:
        click.echo("Mode: Development (using source directory)")

    toggles = list(clis_to_install)
    if install_hooks:
        toggles.append("git-hooks")

    click.echo(f"Components to configure: {', '.join(toggles)}")
    click.echo("")

    # Track results
    results = {}

    # Install Claude Code hooks
    if "claude" in clis_to_install:
        click.echo("-" * 40)
        click.echo("Claude Code")
        click.echo("-" * 40)

        result = _install_claude(project_path)
        results["claude"] = result

        if result["success"]:
            click.echo(f"Installed {len(result['hooks_installed'])} hooks")
            for hook in result["hooks_installed"]:
                click.echo(f"  - {hook}")
            if result["skills_installed"]:
                click.echo(f"Installed {len(result['skills_installed'])} skills")
                for skill in result["skills_installed"]:
                    click.echo(f"  - {skill}")
            if result.get("workflows_installed"):
                click.echo(f"Installed {len(result['workflows_installed'])} workflows")
                for workflow in result["workflows_installed"]:
                    click.echo(f"  - {workflow}")
            click.echo(f"Configuration: {project_path / '.claude' / 'settings.json'}")
        else:
            click.echo(f"Failed: {result['error']}", err=True)
        click.echo("")

    # Install Gemini CLI hooks
    if "gemini" in clis_to_install:
        click.echo("-" * 40)
        click.echo("Gemini CLI")
        click.echo("-" * 40)

        result = _install_gemini(project_path)
        results["gemini"] = result

        if result["success"]:
            click.echo(f"Installed {len(result['hooks_installed'])} hooks")
            for hook in result["hooks_installed"]:
                click.echo(f"  - {hook}")
            if result.get("skills_installed"):
                click.echo(f"Installed {len(result['skills_installed'])} skills")
                for skill in result["skills_installed"]:
                    click.echo(f"  - {skill}")
            if result.get("workflows_installed"):
                click.echo(f"Installed {len(result['workflows_installed'])} workflows")
                for workflow in result["workflows_installed"]:
                    click.echo(f"  - {workflow}")
            click.echo(f"Configuration: {project_path / '.gemini' / 'settings.json'}")
        else:
            click.echo(f"Failed: {result['error']}", err=True)
        click.echo("")

    # Configure Codex notify integration (interactive Codex)
    if "codex" in clis_to_install:
        click.echo("-" * 40)
        click.echo("Codex")
        click.echo("-" * 40)

        if not codex_detected:
            click.echo("Codex CLI not detected in PATH (`codex`).", err=True)
            click.echo("Install Codex first, then re-run:")
            click.echo("  npm install -g @openai/codex\n")
            results["codex"] = {"success": False, "error": "Codex CLI not detected"}
        else:
            result = _install_codex_notify()
            results["codex"] = result

            if result["success"]:
                click.echo("Installed Codex notify integration")
                for file_path in result["files_installed"]:
                    click.echo(f"  - {file_path}")
                if result.get("config_updated"):
                    click.echo("Updated: ~/.codex/config.toml (set `notify = ...`)")
                else:
                    click.echo("~/.codex/config.toml already configured")

                if result.get("skills_installed"):
                    click.echo(f"Installed {len(result['skills_installed'])} skills")
                    for skill in result["skills_installed"]:
                        click.echo(f"  - {skill}")
                if result.get("workflows_installed"):
                    click.echo(f"Installed {len(result['workflows_installed'])} workflows")
                    for workflow in result["workflows_installed"]:
                        click.echo(f"  - {workflow}")
            else:
                click.echo(f"Failed: {result['error']}", err=True)
        click.echo("")

    # Install Git Hooks
    if install_hooks:
        click.echo("-" * 40)
        click.echo("Git Hooks (Task Auto-Sync)")
        click.echo("-" * 40)

        result = _install_git_hooks(project_path)
        results["git-hooks"] = result

        if result["success"]:
            if result.get("installed"):
                click.echo("Installed git hooks:")
                for hook in result["installed"]:
                    click.echo(f"  - {hook}")
            else:
                click.echo("Git hooks already installed")
        else:
            click.echo(f"Failed: {result['error']}", err=True)
        click.echo("")

    # Install Antigravity hooks
    # Note: Antigravity is an internal configuration, so we treat it similarly to Gemini
    if "antigravity" in clis_to_install:
        click.echo("-" * 40)
        click.echo("Antigravity Agent")
        click.echo("-" * 40)

        result = _install_antigravity(project_path)
        results["antigravity"] = result

        if result["success"]:
            click.echo(f"Installed {len(result['hooks_installed'])} hooks")
            for hook in result["hooks_installed"]:
                click.echo(f"  - {hook}")
            if result["skills_installed"]:
                click.echo(f"Installed {len(result['skills_installed'])} skills")
                for skill in result["skills_installed"]:
                    click.echo(f"  - {skill}")
            if result.get("workflows_installed"):
                click.echo(f"Installed {len(result['workflows_installed'])} workflows")
                for workflow in result["workflows_installed"]:
                    click.echo(f"  - {workflow}")
            click.echo(f"Configuration: {project_path / '.antigravity' / 'settings.json'}")
        else:
            click.echo(f"Failed: {result['error']}", err=True)
        click.echo("")

    # Summary
    click.echo("=" * 60)
    click.echo("  Summary")
    click.echo("=" * 60)

    all_success = all(r.get("success", False) for r in results.values())

    if all_success:
        click.echo("\nInstallation completed successfully!")
    else:
        failed = [cli for cli, r in results.items() if not r.get("success", False)]
        click.echo(f"\nSome installations failed: {', '.join(failed)}")

    click.echo("\nNext steps:")
    click.echo("  1. Ensure the Gobby daemon is running:")
    click.echo("     gobby start")
    click.echo("  2. Start a new session in your AI coding CLI")
    click.echo("  3. Your sessions will now be tracked locally")

    if not all_success:
        sys.exit(1)


@click.command("uninstall")
@click.option(
    "--claude",
    "uninstall_claude",
    is_flag=True,
    help="Uninstall Claude Code hooks only",
)
@click.option(
    "--gemini",
    "uninstall_gemini",
    is_flag=True,
    help="Uninstall Gemini CLI hooks only",
)
@click.option(
    "--codex",
    "uninstall_codex",
    is_flag=True,
    help="Uninstall Codex notify integration",
)
@click.option(
    "--all",
    "uninstall_all",
    is_flag=True,
    default=False,
    help="Uninstall hooks from all CLIs (default behavior when no flags specified)",
)
@click.confirmation_option(prompt="Are you sure you want to uninstall Gobby hooks?")
def uninstall(
    uninstall_claude: bool, uninstall_gemini: bool, uninstall_codex: bool, uninstall_all: bool
) -> None:
    """Uninstall Gobby hooks from AI coding CLIs.

    By default (no flags), uninstalls from all CLIs that have hooks installed.
    Use --claude, --gemini, or --codex to uninstall only from specific CLIs.

    Uninstalls from project-level directories in current working directory.
    """
    project_path = Path.cwd()

    # Determine which CLIs to uninstall
    # If no flags specified, act like --all
    if not uninstall_claude and not uninstall_gemini and not uninstall_codex and not uninstall_all:
        uninstall_all = True

    # Build list of CLIs to uninstall
    clis_to_uninstall = []

    if uninstall_all:
        # Check which CLIs have hooks installed
        claude_settings = project_path / ".claude" / "settings.json"
        gemini_settings = project_path / ".gemini" / "settings.json"
        codex_notify = Path.home() / ".gobby" / "hooks" / "codex" / "hook_dispatcher.py"

        if claude_settings.exists():
            clis_to_uninstall.append("claude")
        if gemini_settings.exists():
            clis_to_uninstall.append("gemini")
        if codex_notify.exists():
            clis_to_uninstall.append("codex")

        if not clis_to_uninstall:
            click.echo("No Gobby hooks found to uninstall.")
            click.echo(f"\nChecked: {project_path / '.claude'}")
            click.echo(f"         {project_path / '.gemini'}")
            click.echo(f"         {codex_notify}")
            sys.exit(0)
    else:
        if uninstall_claude:
            clis_to_uninstall.append("claude")
        if uninstall_gemini:
            clis_to_uninstall.append("gemini")
        if uninstall_codex:
            clis_to_uninstall.append("codex")

    click.echo("=" * 60)
    click.echo("  Gobby Hooks Uninstallation")
    click.echo("=" * 60)
    click.echo(f"\nProject: {project_path}")
    click.echo(f"CLIs to uninstall from: {', '.join(clis_to_uninstall)}")
    click.echo("")

    # Track results
    results = {}

    # Uninstall Claude Code hooks
    if "claude" in clis_to_uninstall:
        click.echo("-" * 40)
        click.echo("Claude Code")
        click.echo("-" * 40)

        result = _uninstall_claude(project_path)
        results["claude"] = result

        if result["success"]:
            if result["hooks_removed"]:
                click.echo(f"Removed {len(result['hooks_removed'])} hooks from settings")
                for hook in result["hooks_removed"]:
                    click.echo(f"  - {hook}")
            if result["files_removed"]:
                click.echo(f"Removed {len(result['files_removed'])} files")
            if result["skills_removed"]:
                click.echo(f"Removed {len(result['skills_removed'])} skills")
            if not result["hooks_removed"] and not result["files_removed"]:
                click.echo("  (no hooks found to remove)")
        else:
            click.echo(f"Failed: {result['error']}", err=True)
        click.echo("")

    # Uninstall Gemini CLI hooks
    if "gemini" in clis_to_uninstall:
        click.echo("-" * 40)
        click.echo("Gemini CLI")
        click.echo("-" * 40)

        result = _uninstall_gemini(project_path)
        results["gemini"] = result

        if result["success"]:
            if result["hooks_removed"]:
                click.echo(f"Removed {len(result['hooks_removed'])} hooks from settings")
                for hook in result["hooks_removed"]:
                    click.echo(f"  - {hook}")
            if result["files_removed"]:
                click.echo(f"Removed {len(result['files_removed'])} files")
            if not result["hooks_removed"] and not result["files_removed"]:
                click.echo("  (no hooks found to remove)")
        else:
            click.echo(f"Failed: {result['error']}", err=True)
        click.echo("")

    # Uninstall Codex notify integration
    if "codex" in clis_to_uninstall:
        click.echo("-" * 40)
        click.echo("Codex")
        click.echo("-" * 40)

        result = _uninstall_codex_notify()
        results["codex"] = result

        if result["success"]:
            if result["files_removed"]:
                click.echo(f"Removed {len(result['files_removed'])} files")
                for f in result["files_removed"]:
                    click.echo(f"  - {f}")
            if result.get("config_updated"):
                click.echo("Updated: ~/.codex/config.toml (removed `notify = ...`)")
            if not result["files_removed"] and not result.get("config_updated"):
                click.echo("  (no codex integration found to remove)")
        else:
            click.echo(f"Failed: {result['error']}", err=True)
        click.echo("")

    # Summary
    click.echo("=" * 60)
    click.echo("  Summary")
    click.echo("=" * 60)

    all_success = all(r.get("success", False) for r in results.values())

    if all_success:
        click.echo("\nUninstallation completed successfully!")
    else:
        failed = [cli for cli, r in results.items() if not r.get("success", False)]
        click.echo(f"\nSome uninstallations failed: {', '.join(failed)}")

    if not all_success:
        sys.exit(1)
