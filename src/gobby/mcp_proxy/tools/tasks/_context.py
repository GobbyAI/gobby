"""Registry context for task tools.

Provides RegistryContext dataclass that bundles shared state and helpers
used across task tool modules.
"""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from gobby.storage.projects import LocalProjectManager
from gobby.storage.session_tasks import SessionTaskManager
from gobby.storage.sessions import LocalSessionManager
from gobby.storage.task_dependencies import TaskDependencyManager
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.worktrees import LocalWorktreeManager
from gobby.utils.project_context import get_project_context
from gobby.workflows.state_manager import SessionVariableManager

if TYPE_CHECKING:
    from gobby.config.app import DaemonConfig
    from gobby.config.tasks import TaskValidationConfig
    from gobby.storage.database import DatabaseProtocol
    from gobby.sync.tasks import TaskSyncManager
    from gobby.tasks.validation import TaskValidator
    from gobby.events.completion_registry import CompletionEventRegistry
    from gobby.llm.service import LLMService


@dataclass
class RegistryContext:
    """Shared context for task tool registries.

    Bundles managers, config, and helper methods used across all task tools.
    """

    # Core managers
    task_manager: LocalTaskManager
    sync_manager: "TaskSyncManager"

    # Optional managers
    task_validator: "TaskValidator | None" = None
    config: "DaemonConfig | None" = None
    llm_service: "LLMService | None" = None
    completion_registry: "CompletionEventRegistry | None" = None

    # Derived managers (initialized in __post_init__)
    dep_manager: TaskDependencyManager = field(init=False)
    session_task_manager: SessionTaskManager = field(init=False)
    session_manager: LocalSessionManager = field(init=False)
    session_var_manager: SessionVariableManager = field(init=False)
    project_manager: LocalProjectManager = field(init=False)
    worktree_manager: LocalWorktreeManager = field(init=False)

    # Config settings (initialized in __post_init__)
    show_result_on_create: bool = field(init=False)
    auto_generate_on_expand: bool = field(init=False)
    validation_config: "TaskValidationConfig | None" = field(init=False)

    def __post_init__(self) -> None:
        """Initialize derived managers and config settings."""
        # Initialize managers from task_manager's database connection
        db = self.task_manager.db
        self.dep_manager = TaskDependencyManager(db)
        self.session_task_manager = SessionTaskManager(db)
        self.session_manager = LocalSessionManager(db)
        self.session_var_manager = SessionVariableManager(db)
        self.project_manager = LocalProjectManager(db)
        self.worktree_manager = LocalWorktreeManager(db)

        # Initialize config settings
        self.show_result_on_create = False
        self.auto_generate_on_expand = True
        self.validation_config = None

        if self.config is not None:
            tasks_config = self.config.get_gobby_tasks_config()
            self.show_result_on_create = tasks_config.show_result_on_create
            self.validation_config = tasks_config.validation
            self.auto_generate_on_expand = self.validation_config.auto_generate_on_expand

    def get_project_repo_path(self, project_id: str | None) -> str | None:
        """Get the repo_path for a project by ID."""
        if not project_id:
            return None
        project = self.project_manager.get(project_id)
        return project.repo_path if project else None

    def get_current_project_id(self) -> str | None:
        """Get the current project ID from context, or None if not in a project."""
        ctx = get_project_context()
        if ctx and ctx.get("id"):
            project_id: str = ctx["id"]
            return project_id
        return None

    def get_current_project_name(self) -> str | None:
        """Get the current project name from context, or None if not in a project."""
        ctx = get_project_context()
        if ctx and ctx.get("name"):
            name: str = ctx["name"]
            return name
        return None

    def resolve_project_filter(
        self, project: str | None = None, all_projects: bool = False
    ) -> str | None:
        """Resolve project filter to project_id.

        Delegates to resolve_project_filter_standalone for consistency.

        Args:
            project: Project name or UUID to filter by
            all_projects: If True, return None (no filter)

        Returns:
            project_id string, or None for all projects

        Raises:
            ValueError: If project name/UUID not found
        """
        return resolve_project_filter_standalone(project, all_projects, self.task_manager.db)

    def resolve_session_id(self, session_id: str) -> str:
        """Resolve session reference (#N, N, UUID, or prefix) to UUID.

        Args:
            session_id: Session reference string

        Returns:
            Resolved UUID string

        Raises:
            ValueError: If session cannot be resolved
        """
        project_id = self.get_current_project_id()
        return self.session_manager.resolve_session_reference(session_id, project_id)

    def resolve_project_from_session(self, session_id: str) -> str:
        """Resolve project_id from session (authoritative source).

        The session's project_id is the authoritative source for project
        affiliation. Falls back to context var then personal workspace.

        This prevents cross-project leakage when the daemon's CWD differs
        from the calling session's project (e.g., stdio MCP transport).

        Args:
            session_id: Session reference (unresolved — #N, N, UUID, prefix)

        Returns:
            Resolved project_id string
        """
        from gobby.storage.projects import PERSONAL_PROJECT_ID

        try:
            resolved_sid = self.resolve_session_id(session_id)
            session = self.session_manager.get(resolved_sid)
            if session and session.project_id:
                return session.project_id
        except Exception:
            pass
        # Fallback to context var (may be set by rules engine path)
        project_ctx = get_project_context()
        if project_ctx and project_ctx.get("id"):
            return str(project_ctx["id"])
        return PERSONAL_PROJECT_ID


def resolve_project_filter_standalone(
    project: str | None,
    all_projects: bool,
    db: "DatabaseProtocol",
) -> str | None:
    """Standalone project filter resolver for tools without RegistryContext.

    Args:
        project: Project name or UUID to filter by
        all_projects: If True, return None (no filter)
        db: Database connection

    Returns:
        project_id string, or None for all projects

    Raises:
        ValueError: If project name/UUID not found
    """
    if project:
        pm = LocalProjectManager(db)
        p = pm.resolve_ref(project)
        if not p:
            raise ValueError(f"Project not found: {project}")
        return p.id
    if all_projects:
        return None
    ctx = get_project_context()
    return ctx.get("id") if ctx else None
