"""
Shared content installation for Gobby hooks.

This module handles installing shared plugins and docs
that are used across all CLI integrations (Claude, AGY, Codex, etc.).

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

from gobby.cli.installers.hook_commands import (
    config_contains_gobby_hook,
    remove_gobby_hook_handlers,
)
from gobby.cli.utils import get_install_dir

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase

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
    """Install shared hook helper files to ~/.gobby/hooks/ for global hook dispatch.

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
        "validate_settings.py": True,  # Make executable
    }

    for filename, make_executable in hook_files.items():
        source_file = shared_hooks_dir / filename
        if not source_file.exists():
            logger.warning("Shared hook file not found: %s", source_file)
            continue
        target_file = global_hooks_dir / filename
        copy2(source_file, target_file)
        if make_executable:
            target_file.chmod(0o755)
        installed.append(filename)

    return installed


def clean_project_hooks(settings_file: Path, *, flat: bool = False) -> list[str]:
    """Remove gobby hooks from a project-level settings/hooks JSON file.

    When hooks are installed globally, project-level hooks cause duplicates
    because CLIs merge both levels. This identifies gobby hooks by checking
    for the literal ``--gobby-owned`` marker in registered hook commands
    across all supported CLI config formats.

    Args:
        settings_file: Path to the project-level JSON config file
        flat: When True, treat the entire JSON object as a flat hooks map
            (hook event names as top-level keys, no ``hooks`` wrapper).  Used
            for Droid 0.159.1 which expects flat-format ``hooks.json``.

    Returns:
        List of hook types that were removed
    """
    if not settings_file.exists():
        return []

    try:
        with open(settings_file) as f:
            settings = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Could not read project settings for hook cleanup: %s", e)
        return []

    if flat:
        hooks_map: dict[str, Any] = settings
    else:
        if "hooks" not in settings:
            return []
        hooks_map = settings["hooks"]

    if not isinstance(hooks_map, dict):
        return []

    removed: list[str] = []
    for hook_type in list(hooks_map.keys()):
        hook_config = hooks_map[hook_type]
        if isinstance(hook_config, list):
            cleaned, handlers_removed = remove_gobby_hook_handlers(hook_config)
            if not handlers_removed:
                continue
            if cleaned:
                hooks_map[hook_type] = cleaned
            else:
                del hooks_map[hook_type]
            removed.append(hook_type)
        elif config_contains_gobby_hook(hook_config):
            del hooks_map[hook_type]
            removed.append(hook_type)

    if not removed:
        return []

    if not flat:
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
        logger.warning("Failed to clean project-level hooks from %s: %s", settings_file, e)
        return []

    logger.info(
        "Cleaned %s gobby hook(s) from %s: %s", len(removed), settings_file, ", ".join(removed)
    )
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
        cli_path: Path to CLI config directory (e.g., .claude or AGY's .gemini)
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
    db: "HubDatabase",
    skip_types: set[str] | None = None,
    *,
    only: set[str] | None = None,
) -> dict[str, Any]:
    """Sync bundled content, then user templates, into the database.

    The bundled fan-out lives in :mod:`gobby.sync_registry`. This wrapper
    keeps user-template import for install-time callers.
    """
    from gobby.sync_registry import sync_bundled_content_to_db as _sync_bundled

    result = _sync_bundled(db, only=only, skip_types=skip_types)

    try:
        from gobby.utils.dev import is_dev_mode

        if not is_dev_mode():
            user_synced = _sync_user_templates_to_db(db)
            if user_synced > 0:
                result["total_synced"] += user_synced
                result["details"]["user_templates"] = {"synced": user_synced}
    except Exception as e:
        msg = f"Failed to sync user templates: {e}"
        logger.warning(msg)
        result["errors"].append(msg)

    return result


def registered_project_id(db: "HubDatabase", project_path: Path) -> str | None:
    """Return the registered project id for ``project_path``.

    Project rule rows reference ``projects``, so an unregistered project
    (``gobby init`` has not run against this database) keeps its rules global
    and says so instead of failing every row on the foreign key.
    """
    from gobby.storage.projects import LocalProjectManager
    from gobby.utils.project_context import get_project_context

    context = get_project_context(project_path)
    project_id = context.get("id") if context else None
    if not isinstance(project_id, str) or not project_id:
        return None
    if LocalProjectManager(db).get(project_id) is None:
        logger.warning(
            "Project %s at %s is not registered; its .gobby/workflows rules sync as global",
            project_id,
            project_path,
        )
        return None
    return project_id


def _sync_user_templates_to_db(db: "HubDatabase") -> int:
    """Sync user-created templates from project and global directories.

    Reads YAML files from .gobby/workflows/<type>/ (project) and
    ~/.gobby/workflows/<type>/ (global), syncing them as source='template'
    with tags=['user'].

    Returns:
        Total number of items synced or updated.
    """
    from gobby.mcp_proxy.sync_servers import sync_mcp_server_files
    from gobby.mcp_proxy.sync_templates import sync_bundled_mcp_templates
    from gobby.paths import (
        get_global_mcp_servers_dir,
        get_global_mcp_templates_dir,
        get_global_rules_dir,
        get_global_variables_dir,
        get_project_mcp_servers_dir,
        get_project_mcp_templates_dir,
        get_project_rules_dir,
        get_project_variables_dir,
    )
    from gobby.storage.secrets import SecretStore

    total = 0
    project_path = Path.cwd()
    project_id = registered_project_id(db, project_path)

    # Each type is synced once across its complete set of user roots so
    # same-tag orphan cleanup sees the full on-disk namespace.
    sync_pairs: list[tuple[list[Path], str, str, str]] = [
        (
            [get_project_rules_dir(project_path), get_global_rules_dir()],
            "gobby.workflows.sync_rules",
            "sync_bundled_rules",
            "rules",
        ),
        (
            [get_project_variables_dir(project_path), get_global_variables_dir()],
            "gobby.workflows.sync_variables",
            "sync_bundled_variables",
            "variables",
        ),
    ]

    for paths, module_path, func_name, content_type in sync_pairs:
        if not any(path.exists() for path in paths):
            continue
        try:
            module = __import__(module_path, fromlist=[func_name])
            sync_fn = getattr(module, func_name)
            if content_type == "rules":
                # Rows from the project directory belong to that project so
                # they never fire in another project; the global root stays
                # global.
                sync_result = sync_fn(
                    db,
                    rules_path=paths,
                    tag="user",
                    project_id=project_id,
                    project_root=paths[0],
                )
            elif content_type == "variables":
                sync_result = sync_fn(db, variables_path=paths, tag="user")
            else:
                continue
            synced = sync_result.get("synced", 0) + sync_result.get("updated", 0)
            total += synced
            if synced > 0:
                logger.info("Synced %s user %s from %s", synced, content_type, paths)
        except Exception as e:
            logger.warning("Failed to sync user %s from %s: %s", content_type, paths, e)

    template_roots = [
        get_project_mcp_templates_dir(project_path),
        get_global_mcp_templates_dir(),
    ]
    if any(path.exists() for path in template_roots):
        try:
            sync_result = sync_bundled_mcp_templates(
                db,
                template_roots,
                tag="user",
                project_id=project_id,
                project_root=template_roots[0],
            )
            synced = sync_result.get("synced", 0) + sync_result.get("updated", 0)
            total += synced
            if synced > 0:
                logger.info("Synced %s user mcp_templates from %s", synced, template_roots)
            if sync_result.get("orphaned_global"):
                bundled = sync_bundled_mcp_templates(db)
                total += bundled.get("synced", 0) + bundled.get("updated", 0)
        except Exception as e:
            logger.warning("Failed to sync user mcp_templates from %s: %s", template_roots, e)

    server_roots = [
        get_project_mcp_servers_dir(project_path),
        get_global_mcp_servers_dir(),
    ]
    if any(path.exists() for path in server_roots):
        try:
            sync_result = sync_mcp_server_files(
                db,
                server_roots,
                project_id=project_id,
                project_root=server_roots[0],
                secret_store=SecretStore(db),
            )
            synced = sync_result.get("synced", 0) + sync_result.get("updated", 0)
            total += synced
            if synced > 0:
                logger.info("Synced %s user mcp_servers from %s", synced, server_roots)
            _reconcile_synced_mcp_instances(db, sync_result.get("affected_ids") or [])
        except Exception as e:
            logger.warning("Failed to sync user mcp_servers from %s: %s", server_roots, e)

    return total


def _reconcile_synced_mcp_instances(db: "HubDatabase", affected_ids: list[Any]) -> None:
    """Refresh each synced instance in the running daemon, or skip if unreachable."""
    if not affected_ids:
        return
    import importlib

    import click

    mcp_mod = importlib.import_module("gobby.cli.mcp_proxy")
    call_mcp_api = mcp_mod.call_mcp_api
    check_daemon_running = mcp_mod.check_daemon_running
    get_daemon_client = mcp_mod.get_daemon_client
    from gobby.storage.mcp import LocalMCPManager
    from gobby.storage.projects import GLOBAL_PROJECT_ID

    try:
        client = get_daemon_client(None)
    except Exception:
        click.echo("MCP live reconcile skipped: daemon client unavailable")
        return
    if not check_daemon_running(client):
        click.echo("MCP live reconcile skipped: daemon not running")
        return
    manager = LocalMCPManager(db)
    for server_id in affected_ids:
        row = manager.get_server_by_id(str(server_id))
        if row is None:
            continue
        payload: dict[str, Any] = {"server_id": str(row.id)}
        if str(row.project_id) == GLOBAL_PROJECT_ID:
            payload["scope"] = "global"
        else:
            payload["project_id"] = str(row.project_id)
        result = call_mcp_api(client, "/api/mcp/refresh", method="POST", json_data=payload)
        if result is None:
            click.echo("MCP live reconcile skipped: daemon not reachable")
            return


def install_cli_content(cli_name: str, target_path: Path) -> dict[str, list[str]]:
    """Install CLI-specific commands (layered on top of shared).

    CLI-specific content can add to or override shared content.

    Args:
        cli_name: Name of the CLI (e.g., "claude", "agy", "codex")
        target_path: Path to CLI config directory

    Returns:
        Dict with lists of installed items by type
    """
    cli_dir = get_install_dir() / cli_name
    installed: dict[str, list[str]] = {"commands": []}

    # CLI-specific commands (slash commands)
    # Claude/AGY/Qwen: commands/, Codex: prompts/
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
