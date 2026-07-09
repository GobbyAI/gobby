"""Common helper functions for workflow CLI commands."""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from gobby.cli.utils import resolve_session_id as resolve_session_id
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.hub.runtime import open_runtime_hub_database
from gobby.workflows.loader import WorkflowLoader
from gobby.workflows.state_manager import SessionVariableManager

_db_instance: HubDatabase | None = None
_session_var_manager_instance: SessionVariableManager | None = None


def create_workflow_loader(db: HubDatabase | None = None) -> WorkflowLoader:
    """Get a DB-backed workflow loader.

    Workflow and pipeline definitions live in the DB registry; a loader
    without a database cannot see bundled definitions.
    """
    if db is not None:
        return WorkflowLoader(db=db)
    return WorkflowLoader(db=open_runtime_hub_database(apply_migrations=False))


def get_workflow_loader() -> WorkflowLoader:
    return create_workflow_loader()


def get_session_var_manager(db: HubDatabase | None = None) -> SessionVariableManager:
    """Get session variable manager instance (cached).

    Args:
        db: Optional database instance to inject. If not provided, a shared
            active hub connection is used.
    """
    global _db_instance, _session_var_manager_instance
    if db is not None:
        return SessionVariableManager(db)
    if _session_var_manager_instance is None:
        _db_instance = open_runtime_hub_database(apply_migrations=False)
        _session_var_manager_instance = SessionVariableManager(_db_instance)
    return _session_var_manager_instance


@contextmanager
def session_var_manager_context(db: HubDatabase | None = None) -> Iterator[SessionVariableManager]:
    """Yield a session variable manager and close cached CLI resources afterwards."""
    manager = get_session_var_manager(db)
    try:
        yield manager
    finally:
        if db is None:
            close_session_var_manager()


def close_session_var_manager() -> None:
    """Close and clear the cached session variable manager database."""
    global _db_instance, _session_var_manager_instance
    db = _db_instance
    _db_instance = None
    _session_var_manager_instance = None
    if db is not None:
        db.close()


def _reset_session_var_manager_for_tests() -> None:
    """Reset cached session variable manager instances (for test isolation)."""
    close_session_var_manager()


def truncate_id(session_id: str, length: int = 12) -> str:
    """Truncate ID for display, appending '...' only if truncated."""
    return f"{session_id[:length]}..." if len(session_id) > length else session_id


def get_project_path() -> Path | None:
    """Get current project path if in a gobby project."""
    cwd = Path.cwd()
    if (cwd / ".gobby").exists():
        return cwd
    return None
