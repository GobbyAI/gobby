"""
Shared content installation for Gobby hooks.

This module handles installing shared plugins and docs
that are used across all CLI integrations (Claude, Gemini, Codex, etc.).

Workflows, agents, rules, prompts, and skills are DB-managed:
they are synced from bundled YAML to the database during ``gobby install``
via :func:`sync_bundled_content_to_db`, NOT copied to ``.gobby/`` on disk.
"""

import json
import logging
import os
import shutil
import tempfile
from pathlib import Path
from shutil import copy2, copytree
from typing import TYPE_CHECKING, Any

from gobby.cli.utils import get_install_dir

if TYPE_CHECKING:
    from gobby.storage.database import DatabaseProtocol

logger = logging.getLogger(__name__)


def _install_file(source: Path, target: Path, executable: bool = False) -> None:
    """Install a single file by copying.

    Safely handles existing symlinks by unlinking before replacing,
    to support migration from dev-mode symlinks to copies.

    Args:
        source: Source file path
        target: Target file path
        executable: If True, chmod 0o755 after copying
    """
    if target.is_symlink():
        os.unlink(target)
    elif target.exists():
        target.unlink()

    copy2(source, target)
    if executable:
        target.chmod(0o755)


def install_global_hooks() -> list[str]:
    """Install shared hook files to ~/.gobby/hooks/ for global hook dispatch.

    Always copies files (never symlinks) since global hooks must work
    regardless of whether the source repo is available.

    Returns:
        List of installed filenames
    """
    shared_hooks_dir = get_install_dir() / "shared" / "hooks"
    global_hooks_dir = Path(
        os.environ.get("GOBBY_HOOKS_DIR", str(Path.home() / ".gobby" / "hooks"))
    )
    global_hooks_dir.mkdir(parents=True, exist_ok=True)
    installed: list[str] = []

    hook_files = {
        "hook_dispatcher.py": True,  # Make executable
        "validate_settings.py": True,  # Make executable
    }

    for filename, make_executable in hook_files.items():
        source_file = shared_hooks_dir / filename
        if not source_file.exists():
            logger.warning(f"Shared hook file not found: {source_file}")
            continue
        target_file = global_hooks_dir / filename
        copy2(source_file, target_file)
        if make_executable:
            target_file.chmod(0o755)
        installed.append(filename)

    return installed


def clean_project_hooks(settings_file: Path) -> list[str]:
    """Remove gobby hooks from a project-level settings/hooks JSON file.

    When hooks are installed globally, project-level hooks cause duplicates
    because CLIs merge both levels. This identifies gobby hooks by checking
    if any command string in the hook entry references ``hook_dispatcher.py``,
    which works across all CLI config formats (Claude settings.json, Gemini
    settings.json, Cursor hooks.json, Windsurf hooks.json).

    Args:
        settings_file: Path to the project-level JSON config file

    Returns:
        List of hook types that were removed
    """
    if not settings_file.exists():
        return []

    try:
        with open(settings_file) as f:
            settings = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Could not read project settings for hook cleanup: {e}")
        return []

    if "hooks" not in settings:
        return []

    removed: list[str] = []
    for hook_type in list(settings["hooks"].keys()):
        # Serialize the entry and check for our dispatcher signature
        entry_str = json.dumps(settings["hooks"][hook_type])
        if "hook_dispatcher.py" in entry_str:
            del settings["hooks"][hook_type]
            removed.append(hook_type)

    if not removed:
        return []

    # Remove empty hooks dict
    if not settings["hooks"]:
        del settings["hooks"]

    try:
        fd, temp_path = tempfile.mkstemp(
            dir=str(settings_file.parent), suffix=".tmp", prefix="settings_"
        )
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(settings, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_path, settings_file)
        except Exception:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
            raise
    except OSError as e:
        logger.warning(f"Failed to clean project-level hooks from {settings_file}: {e}")
        return []

    logger.info(f"Cleaned {len(removed)} gobby hook(s) from {settings_file}: {', '.join(removed)}")
    return removed


def install_shared_content(cli_path: Path, project_path: Path) -> dict[str, list[str]]:
    """Install shared content from src/install/shared/.

    Plugins are project-scoped and go to {project_path}/.gobby/plugins/.
    Docs are project-local and go to {project_path}/.gobby/docs/.

    Note: Workflows, agents, prompts, rules, and skills are DB-managed.
    They are synced to the database during ``gobby install`` via
    :func:`sync_bundled_content_to_db`, NOT copied to ``.gobby/``.

    Safely handles migration from dev-mode symlinks to copies.

    Args:
        cli_path: Path to CLI config directory (e.g., .claude, .gemini)
        project_path: Path to project root

    Returns:
        Dict with lists of installed items by type
    """
    shared_dir = get_install_dir() / "shared"
    installed: dict[str, list[str]] = {
        "plugins": [],
        "docs": [],
    }

    # Only plugins and docs are file-based; workflows/agents/rules/prompts/skills
    # are DB-managed and synced via sync_bundled_content_to_db().
    resource_dirs = [
        ("plugins", "plugins", "plugins"),
        ("docs", "docs", "docs"),
    ]

    for source_name, target_name, type_key in resource_dirs:
        source = shared_dir / source_name
        if not source.exists():
            continue

        target = project_path / ".gobby" / target_name

        # Migrate from symlink to copy if needed
        if target.is_symlink():
            os.unlink(target)

        target.mkdir(parents=True, exist_ok=True)
        if type_key == "plugins":
            _copy_plugins(source, target, installed)
        elif type_key == "docs":
            _copy_docs(source, target, installed)

    return installed


def _copy_plugins(source: Path, target: Path, installed: dict[str, list[str]]) -> None:
    """Copy plugin files from source to target."""
    for plugin_file in source.iterdir():
        if plugin_file.is_file() and plugin_file.suffix == ".py":
            copy2(plugin_file, target / plugin_file.name)
            installed["plugins"].append(plugin_file.name)


def _copy_docs(source: Path, target: Path, installed: dict[str, list[str]]) -> None:
    """Copy doc files from source to target."""
    for doc_file in source.iterdir():
        if doc_file.is_file():
            copy2(doc_file, target / doc_file.name)
            installed["docs"].append(doc_file.name)


def sync_bundled_content_to_db(
    db: "DatabaseProtocol",
    skip_types: set[str] | None = None,
) -> dict[str, Any]:
    """Sync all bundled content (skills, prompts, rules, agents, workflows) to the database.

    Called during ``gobby install`` as the single import point.
    The daemon no longer syncs on startup (except in dev mode).

    Args:
        db: Database connection implementing DatabaseProtocol.
        skip_types: Optional set of content type names to skip (e.g. ``{"workflows"}``).
            Used by the integrity checker to block tampered types.

    Returns:
        Dict with total_synced count and any errors.
    """
    result: dict[str, Any] = {
        "total_synced": 0,
        "errors": [],
        "details": {},
    }

    # (content_type, module_path, function_name)
    sync_targets: list[tuple[str, str, str]] = [
        ("skills", "gobby.skills.sync", "sync_bundled_skills"),
        ("prompts", "gobby.prompts.sync", "sync_bundled_prompts"),
        ("agents", "gobby.agents.sync", "sync_bundled_agents"),
        ("workflows", "gobby.workflows.sync", "sync_bundled_workflows"),
        ("rules", "gobby.workflows.sync", "sync_bundled_rules"),
        ("variables", "gobby.workflows.sync", "sync_bundled_variables"),
    ]

    for content_type, module_path, func_name in sync_targets:
        if skip_types and content_type in skip_types:
            logger.debug(f"Skipping sync of bundled {content_type}")
            result["details"][content_type] = {"skipped": True}
            continue
        try:
            module = __import__(module_path, fromlist=[func_name])
            sync_fn = getattr(module, func_name)
            sync_result = sync_fn(db)
            synced = sync_result.get("synced", 0) + sync_result.get("updated", 0)
            result["total_synced"] += synced
            result["details"][content_type] = sync_result
            if synced > 0:
                logger.info(f"Synced {synced} bundled {content_type} to database")
        except Exception as e:
            msg = f"Failed to sync bundled {content_type}: {e}"
            logger.warning(msg)
            result["errors"].append(msg)

    # Auto-install templates so bundled skills are immediately active.
    # Existing users: skills already source='installed' from migration; skipped.
    # New users: templates created then immediately installed.
    try:
        from gobby.storage.skills import LocalSkillManager

        skill_storage = LocalSkillManager(db)
        installed_count = skill_storage.install_all_templates()
        if installed_count > 0:
            logger.info(f"Auto-installed {installed_count} skill templates")
            result["details"].setdefault("skills", {})["auto_installed"] = installed_count
    except Exception as e:
        msg = f"Failed to auto-install skill templates: {e}"
        logger.warning(msg)
        result["errors"].append(msg)

    # Auto-install and enable gobby-tagged workflow definition templates.
    # Creates source='installed' copies of templates tagged "gobby" and enables them.
    try:
        from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager

        wf_manager = LocalWorkflowDefinitionManager(db)
        installed = wf_manager.install_all_templates(tag="gobby")
        if installed:
            with db.transaction() as conn:
                ids = [row.id for row in installed]
                placeholders = ",".join("?" * len(ids))
                conn.execute(
                    f"UPDATE workflow_definitions SET enabled = 1, updated_at = datetime('now') "
                    f"WHERE id IN ({placeholders})",
                    ids,
                )
            logger.info(f"Auto-installed and enabled {len(installed)} gobby-tagged definitions")
            result["details"]["workflow_definitions"] = {"auto_installed": len(installed)}
    except Exception as e:
        msg = f"Failed to auto-install workflow definition templates: {e}"
        logger.warning(msg)
        result["errors"].append(msg)

    return result


def install_cli_content(cli_name: str, target_path: Path) -> dict[str, list[str]]:
    """Install CLI-specific commands (layered on top of shared).

    CLI-specific content can add to or override shared content.

    Args:
        cli_name: Name of the CLI (e.g., "claude", "gemini", "codex")
        target_path: Path to CLI config directory

    Returns:
        Dict with lists of installed items by type
    """
    cli_dir = get_install_dir() / cli_name
    installed: dict[str, list[str]] = {"commands": []}

    # CLI-specific commands (slash commands)
    # Claude/Gemini: commands/, Codex: prompts/
    for cmd_dir_name in ["commands", "prompts"]:
        cli_commands = cli_dir / cmd_dir_name
        if cli_commands.exists():
            target_commands = target_path / cmd_dir_name
            target_commands.mkdir(parents=True, exist_ok=True)
            for item in cli_commands.iterdir():
                if item.is_dir():
                    # Directory of commands (e.g., memory/)
                    target_subdir = target_commands / item.name
                    if target_subdir.exists():
                        shutil.rmtree(target_subdir)
                    copytree(item, target_subdir)
                    installed["commands"].append(f"{item.name}/")
                elif item.is_file():
                    # Single command file
                    copy2(item, target_commands / item.name)
                    installed["commands"].append(item.name)

    return installed
