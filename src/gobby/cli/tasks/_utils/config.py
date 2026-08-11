"""Manager accessors and configuration gates for the task CLI."""

import logging
import sys
from pathlib import Path

import click

from gobby.cli.runtime import get_cli_runtime, require_cli_database
from gobby.storage.tasks import LocalTaskManager
from gobby.sync.task_github_import import GitHubIssueImporter
from gobby.sync.tasks import TaskBackupManager

logger = logging.getLogger(__name__)


def check_tasks_enabled() -> None:
    """Check if gobby-tasks is enabled, exit if not."""
    try:
        config = get_cli_runtime().require_config()
        if not config.gobby_tasks.enabled:
            click.echo("Error: gobby-tasks is disabled in configuration", err=True)
            sys.exit(1)
    except (FileNotFoundError, AttributeError, ImportError) as e:
        # Expected errors if config missing or invalid.
        # Fail open to allow CLI to work even if config is borked.
        logger.debug("check_tasks_enabled: skipping check due to %s", e)
    except Exception as e:
        # Unexpected errors handling config
        logger.warning("Error checking tasks config: %s", e)


def get_task_manager(*, apply_migrations: bool = True) -> LocalTaskManager:
    """Get initialized task manager."""
    try:
        db = require_cli_database(apply_migrations=apply_migrations)
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc
    return LocalTaskManager(db)


def get_backup_manager(path: str | Path | None = None) -> TaskBackupManager:
    """Get initialized task backup manager."""
    manager = get_task_manager()
    return TaskBackupManager(manager, backup_path=path)


def get_github_importer() -> GitHubIssueImporter:
    """Get initialized GitHub issue importer."""
    return GitHubIssueImporter(get_task_manager().db)
