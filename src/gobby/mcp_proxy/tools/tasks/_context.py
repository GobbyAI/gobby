"""Registry context for task tools.

Provides RegistryContext dataclass that bundles shared state and helpers
used across task tool modules.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from gobby.storage.project_checkouts import require_root
from gobby.storage.projects import LocalProjectManager
from gobby.storage.session_tasks import SessionTaskManager
from gobby.storage.sessions import SessionManager
from gobby.storage.task_dependencies import TaskDependencyManager
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.workspace_machine_scope import require_local_machine_id
from gobby.storage.worktrees import LocalWorktreeManager
from gobby.utils.project_context import get_project_context
from gobby.workflows.state_manager import SessionVariableManager

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from gobby.config.app import DaemonConfig
    from gobby.config.tasks import TaskValidationConfig
    from gobby.events.completion_registry import CompletionEventRegistry
    from gobby.llm.service import LLMService
    from gobby.mcp_proxy.manager import MCPClientManager
    from gobby.mcp_proxy.tools.internal import InternalToolRegistry
    from gobby.review_learning.service import ReviewLearningService
    from gobby.storage.hub.protocol import HubDatabase
    from gobby.tasks.validation import TaskValidator


@dataclass
class RegistryContext:
    """Shared context for task tool registries.

    Bundles managers, config, and helper methods used across all task tools.
    """

    # Core managers
    task_manager: LocalTaskManager

    # Optional managers. Runtime-replaceable services are held as resolver
    # callables so every tool call observes the current runtime epoch.
    task_validator_resolver: "Callable[[], TaskValidator | None] | None" = None
    startup_config: "DaemonConfig | None" = None
    config_resolver: "Callable[[], DaemonConfig | None] | None" = None
    llm_service_resolver: "Callable[[], LLMService | None] | None" = None
    completion_registry: "CompletionEventRegistry | None" = None
    mcp_manager_resolver: "Callable[[], MCPClientManager | None] | None" = None
    agent_registry_resolver: "Callable[[], InternalToolRegistry | None] | None" = None
    review_learning_service: "ReviewLearningService | None" = None

    # Derived managers (initialized in __post_init__)
    dep_manager: TaskDependencyManager = field(init=False)
    session_task_manager: SessionTaskManager = field(init=False)
    session_manager: SessionManager = field(init=False)
    session_var_manager: SessionVariableManager = field(init=False)
    project_manager: LocalProjectManager = field(init=False)
    worktree_manager: LocalWorktreeManager = field(init=False)

    def __post_init__(self) -> None:
        """Initialize derived managers."""
        # Initialize managers from task_manager's database connection
        db = self.task_manager.db
        self.dep_manager = TaskDependencyManager(db)
        self.session_task_manager = SessionTaskManager(db)
        self.session_manager = SessionManager(db)
        self.session_var_manager = SessionVariableManager(db)
        self.project_manager = LocalProjectManager(db)
        self.worktree_manager = LocalWorktreeManager(db)

    @property
    def config(self) -> "DaemonConfig | None":
        """Resolve configuration from the operation epoch or startup fallback."""
        config = self.config_resolver() if self.config_resolver is not None else None
        return config if config is not None else self.startup_config

    @property
    def show_result_on_create(self) -> bool:
        config = self.config
        return config.get_gobby_tasks_config().show_result_on_create if config else False

    @property
    def validation_config(self) -> "TaskValidationConfig | None":
        config = self.config
        return config.get_gobby_tasks_config().validation if config else None

    @property
    def task_validator(self) -> "TaskValidator | None":
        """Resolve the current-epoch task validator per access."""
        resolver = self.task_validator_resolver
        return resolver() if resolver is not None else None

    @property
    def llm_service(self) -> "LLMService | None":
        """Resolve the current-epoch LLM service per access."""
        resolver = self.llm_service_resolver
        return resolver() if resolver is not None else None

    @property
    def mcp_manager(self) -> "MCPClientManager | None":
        """Resolve the current-epoch external MCP manager per access."""
        resolver = self.mcp_manager_resolver
        return resolver() if resolver is not None else None

    @property
    def agent_registry(self) -> "InternalToolRegistry | None":
        """Resolve the internal agent registry lazily after registry setup completes."""
        resolver = self.agent_registry_resolver
        return resolver() if resolver is not None else None

    def get_project_repo_path(
        self, project_id: str | None, machine_id: str | None = None
    ) -> str | None:
        """Resolve the machine checkout root for a project."""
        if not project_id:
            return None
        resolved_machine = require_local_machine_id(
            machine_id, resource_kind="project_checkout", resource_id=project_id
        )
        return require_root(self.task_manager.db, project_id, resolved_machine)

    def checkout_machine_id(self, project_id: str, session_ref: str | None = None) -> str:
        """Return the session machine when present, else the local daemon machine."""
        if session_ref:
            session_id = self.resolve_session_id(session_ref)
            session = self.session_manager.get(session_id)
            if session is None or not session.machine_id:
                from gobby.storage.project_checkouts import MissingMachineContextError

                raise MissingMachineContextError(
                    f"session {session_ref} has no machine_id for project checkout"
                )
            return require_local_machine_id(
                session.machine_id,
                resource_kind="project_checkout",
                resource_id=project_id,
            )
        return require_local_machine_id(
            None, resource_kind="project_checkout", resource_id=project_id
        )

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
        affiliation. A provided session that cannot be resolved must fail
        closed rather than falling back to a different project.

        This prevents cross-project leakage when the daemon's CWD differs
        from the calling session's project (e.g., stdio MCP transport).

        Args:
            session_id: Session reference (unresolved — #N, N, UUID, prefix)

        Returns:
            Resolved project_id string
        """
        try:
            resolved_sid = self.resolve_session_id(session_id)
            session = self.session_manager.get(resolved_sid)
        except (ValueError, KeyError, LookupError) as exc:
            logger.warning(
                "Cannot resolve project for session %s: %s",
                session_id,
                exc,
                exc_info=True,
            )
            raise ValueError(f"Cannot resolve project for session '{session_id}': {exc}") from exc

        if session is None:
            message = f"Resolved session '{resolved_sid}' was not found"
            logger.warning("Cannot resolve project for session %s: %s", session_id, message)
            raise ValueError(message)
        if not session.project_id:
            message = f"Resolved session '{resolved_sid}' has no project"
            logger.warning("Cannot resolve project for session %s: %s", session_id, message)
            raise ValueError(message)
        return session.project_id


def resolve_project_filter_standalone(
    project: str | None,
    all_projects: bool,
    db: "HubDatabase",
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
