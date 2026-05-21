"""Manager accessors and configuration gates for the task CLI."""

import logging
import sys

import click

from gobby.config.app import load_config
from gobby.storage.hub.runtime import open_runtime_hub_database
from gobby.storage.tasks import LocalTaskManager
from gobby.sync.tasks import TaskSyncManager

logger = logging.getLogger(__name__)

TASKS_EXPORT_PATH = ".gobby/tasks.jsonl"


def check_tasks_enabled() -> None:
    """Check if gobby-tasks is enabled, exit if not."""
    try:
        config = load_config()
        if not config.gobby_tasks.enabled:
            click.echo("Error: gobby-tasks is disabled in configuration", err=True)
            sys.exit(1)
    except (FileNotFoundError, AttributeError, ImportError) as e:
        # Expected errors if config missing or invalid.
        # Fail open to allow CLI to work even if config is borked.
        logger.debug("check_tasks_enabled: skipping check due to %s", e)
    except Exception as e:
        # Unexpected errors handling config
        logger.warning(f"Error checking tasks config: {e}")


def get_task_manager() -> LocalTaskManager:
    """Get initialized task manager."""
    try:
        db = open_runtime_hub_database(apply_migrations=False)
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc
    return LocalTaskManager(db)


def get_sync_manager() -> TaskSyncManager:
    """Get initialized sync manager."""
    manager = get_task_manager()
    return TaskSyncManager(manager, export_path=TASKS_EXPORT_PATH)
