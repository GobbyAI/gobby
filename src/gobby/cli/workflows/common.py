"""Common helper functions for workflow CLI commands."""

from pathlib import Path

from gobby.cli.utils import resolve_session_id as resolve_session_id
from gobby.storage.database import LocalDatabase
from gobby.workflows.loader import WorkflowLoader
from gobby.workflows.state_manager import SessionVariableManager

_db_instance: LocalDatabase | None = None
_session_var_manager_instance: SessionVariableManager | None = None


def get_workflow_loader() -> WorkflowLoader:
    """Get workflow loader instance."""
    return WorkflowLoader()


def get_session_var_manager(db: LocalDatabase | None = None) -> SessionVariableManager:
    """Get session variable manager instance (cached).

    Args:
        db: Optional database instance to inject. If not provided, a shared
            instance is used. LocalDatabase uses thread-local connections
            internally, so sharing one instance is safe.
    """
    global _db_instance, _session_var_manager_instance
    if db is not None:
        return SessionVariableManager(db)
    if _session_var_manager_instance is None:
        _db_instance = LocalDatabase()
        _session_var_manager_instance = SessionVariableManager(_db_instance)
    return _session_var_manager_instance


def _reset_session_var_manager_for_tests() -> None:
    """Reset cached session variable manager instances (for test isolation)."""
    global _db_instance, _session_var_manager_instance
    if _db_instance is not None:
        close_fn = getattr(_db_instance, "close", None)
        if close_fn is not None:
            close_fn()
    _db_instance = None
    _session_var_manager_instance = None


def truncate_id(session_id: str, length: int = 12) -> str:
    """Truncate ID for display, appending '...' only if truncated."""
    return f"{session_id[:length]}..." if len(session_id) > length else session_id


def get_project_path() -> Path | None:
    """Get current project path if in a gobby project."""
    cwd = Path.cwd()
    if (cwd / ".gobby").exists():
        return cwd
    return None
