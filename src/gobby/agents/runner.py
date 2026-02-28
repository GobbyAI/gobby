"""
Agent runner for orchestrating agent execution.

The AgentRunner coordinates:
- Creating child sessions for agents
- Tracking agent runs in the database
- Executing agents via LLM providers
- Handling tool calls via the MCP proxy
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from gobby.agents import runner_queries as _queries
from gobby.agents.registry import RunningAgent
from gobby.agents.runner_models import AgentConfig, AgentRunContext
from gobby.agents.runner_tracking import RunTracker
from gobby.agents.session import ChildSessionConfig, ChildSessionManager
from gobby.llm.executor import AgentExecutor, AgentResult, ToolHandler, ToolResult
from gobby.storage.agents import LocalAgentRunManager

__all__ = ["AgentRunner"]

if TYPE_CHECKING:
    from gobby.storage.database import DatabaseProtocol
    from gobby.storage.sessions import LocalSessionManager
    from gobby.workflows.loader import WorkflowLoader

logger = logging.getLogger(__name__)


class AgentRunner:
    """
    Orchestrates agent execution with session and run tracking.

    The runner:
    1. Creates a child session for the agent
    2. Records the agent run in the database
    3. Executes the agent via the appropriate LLM provider
    4. Updates run status based on execution result

    Example:
        >>> runner = AgentRunner(db, session_storage, executor)
        >>> result = await runner.run(AgentConfig(
        ...     prompt="Create a TODO list",
        ...     parent_session_id="sess-123",
        ...     project_id="proj-abc",
        ...     machine_id="machine-1",
        ...     source="claude",
        ... ))
    """

    def __init__(
        self,
        db: DatabaseProtocol,
        session_storage: LocalSessionManager,
        executors: dict[str, AgentExecutor],
        max_agent_depth: int = 1,
        workflow_loader: WorkflowLoader | None = None,
    ):
        """
        Initialize AgentRunner.

        Args:
            db: Database connection.
            session_storage: Session storage manager.
            executors: Map of provider name to executor instance.
            max_agent_depth: Maximum nesting depth for agents.
            workflow_loader: Optional WorkflowLoader for loading workflow definitions.
        """
        self.db = db
        self._session_storage = session_storage
        self._executors = executors
        self._child_session_manager = ChildSessionManager(
            session_storage,
            max_agent_depth=max_agent_depth,
        )
        self._run_storage = LocalAgentRunManager(db)
        if workflow_loader is not None:
            self._workflow_loader = workflow_loader
        else:
            from gobby.workflows.loader import WorkflowLoader as _WL

            self._workflow_loader = _WL()
        # Agent definitions are now loaded by the spawn_agent factory directly

        self.logger = logger

        # Workflow handler for hook evaluation on spawned agent tool calls
        self._workflow_handler: Any = None

        # Thread-safe in-memory tracking of running agents
        self._tracker = RunTracker()

    @property
    def workflow_handler(self) -> Any:
        """Workflow handler for hook evaluation on spawned agent tool calls."""
        return self._workflow_handler

    @workflow_handler.setter
    def workflow_handler(self, value: Any) -> None:
        self._workflow_handler = value

    @property
    def child_session_manager(self) -> ChildSessionManager:
        """Public accessor for the child session manager."""
        return self._child_session_manager

    @property
    def run_storage(self) -> LocalAgentRunManager:
        """Public accessor for the agent run storage manager."""
        return self._run_storage

    def get_executor(self, provider: str) -> AgentExecutor | None:
        """Get executor for a provider."""
        return self._executors.get(provider)

    def register_executor(self, provider: str, executor: AgentExecutor) -> None:
        """Register an executor for a provider."""
        self._executors[provider] = executor
        self.logger.info(f"Registered executor for provider: {provider}")

    def can_spawn(self, parent_session_id: str) -> tuple[bool, str, int]:
        """
        Check if an agent can be spawned from the given session.

        Args:
            parent_session_id: The session attempting to spawn.

        Returns:
            Tuple of (can_spawn, reason, parent_depth).
            The parent_depth is returned to avoid redundant depth lookups.
        """
        return self._child_session_manager.can_spawn_child(parent_session_id)

    def prepare_run(self, config: AgentConfig) -> AgentRunContext | AgentResult:
        """
        Prepare for agent execution by creating database records.

        Creates:
        - Child session linked to parent
        - Agent run record in database
        - Workflow state (if workflow specified)

        This method can be used separately for terminal mode, where we prepare
        the database state, then spawn a terminal process that picks up from
        the session via hooks.

        Args:
            config: Agent configuration.

        Returns:
            AgentRunContext on success, or AgentResult with error on failure.
        """
        # Validate required fields
        if not config.parent_session_id:
            return AgentResult(
                output="",
                status="error",
                error="parent_session_id is required",
                turns_used=0,
            )
        if not config.project_id:
            return AgentResult(
                output="",
                status="error",
                error="project_id is required",
                turns_used=0,
            )
        if not config.machine_id:
            return AgentResult(
                output="",
                status="error",
                error="machine_id is required",
                turns_used=0,
            )

        # Type narrowing for mypy - these are guaranteed non-None after validation above
        parent_session_id: str = config.parent_session_id
        project_id: str = config.project_id
        machine_id: str = config.machine_id

        # Check if we can spawn (also get parent_depth to avoid redundant lookups)
        can_spawn, reason, _parent_depth = self.can_spawn(parent_session_id)
        if not can_spawn:
            self.logger.warning(f"Cannot spawn agent: {reason}")
            return AgentResult(
                output="",
                status="error",
                error=reason,
                turns_used=0,
            )

        # Agent definition merging is now done by the spawn_agent factory
        # before calling spawn(). No need to re-load here.

        # Get effective workflow name (prefers 'workflow' over legacy 'workflow_name')
        effective_workflow = config.get_effective_workflow()

        # Validate workflow BEFORE creating child session to avoid orphaned sessions
        workflow_definition = None
        if effective_workflow and self._workflow_loader:
            workflow_definition = self._workflow_loader.load_workflow_sync(
                effective_workflow,
                project_path=config.project_path,
            )
            if workflow_definition:
                from gobby.workflows.definitions import PipelineDefinition, WorkflowDefinition

                # Reject lifecycle workflows - they run automatically via hooks
                if (
                    isinstance(workflow_definition, WorkflowDefinition)
                    and workflow_definition.type == "lifecycle"
                ):
                    self.logger.error(
                        f"Cannot use lifecycle workflow '{effective_workflow}' for agent spawning"
                    )
                    return AgentResult(
                        output="",
                        status="error",
                        error=(
                            f"Cannot use lifecycle workflow '{effective_workflow}' for agent spawning. "
                            f"Lifecycle workflows run automatically on events. "
                            f"Use a step workflow like 'plan-execute' instead."
                        ),
                        turns_used=0,
                    )
                # Ensure the loaded workflow is actually a PipelineDefinition or a valid WorkflowDefinition
                elif not isinstance(workflow_definition, (PipelineDefinition, WorkflowDefinition)):
                    return AgentResult(
                        output="",
                        status="error",
                        error=f"Loaded workflow '{effective_workflow}' is not a valid Agent Definition",
                        turns_used=0,
                    )

        # Create child session (now safe - workflow validated above)
        try:
            child_session = self._child_session_manager.create_child_session(
                ChildSessionConfig(
                    parent_session_id=parent_session_id,
                    project_id=project_id,
                    machine_id=machine_id,
                    source=config.source,
                    workflow_name=effective_workflow,
                    title=config.title,
                    git_branch=config.git_branch,
                    lifecycle_variables=config.lifecycle_variables,
                )
            )
        except ValueError as e:
            self.logger.error(f"Failed to create child session: {e}")
            return AgentResult(
                output="",
                status="error",
                error=str(e),
                turns_used=0,
            )

        # Log workflow assignment
        if workflow_definition:
            if isinstance(workflow_definition, PipelineDefinition):
                self.logger.info(
                    f"Pipeline '{effective_workflow}' assigned to agent session {child_session.id}"
                )
            else:
                self.logger.info(
                    f"Workflow '{effective_workflow}' loaded for agent session {child_session.id} "
                    f"(type={type(workflow_definition).__name__})"
                )
        elif effective_workflow:
            self.logger.error(f"Workflow '{effective_workflow}' not found")
            return AgentResult(
                output="",
                status="error",
                error=f"Workflow '{effective_workflow}' not found. Agent cannot start without its assigned pipeline.",
                turns_used=0,
            )

        # Create agent run record
        agent_run = self._run_storage.create(
            parent_session_id=parent_session_id,
            provider=config.provider,
            prompt=config.prompt,
            workflow_name=effective_workflow,
            model=config.model,
            child_session_id=child_session.id,
        )

        # Set terminal pickup metadata on child session for terminal mode
        # This allows terminal-spawned agents to pick up their state via hooks
        self._session_storage.update_terminal_pickup_metadata(
            session_id=child_session.id,
            workflow_name=effective_workflow,
            agent_run_id=agent_run.id,
            context_injected=config.context_injected,
            original_prompt=config.prompt,
        )

        self.logger.info(
            f"Prepared agent run {agent_run.id} "
            f"(child_session={child_session.id}, provider={config.provider})"
        )

        # Convert child session (internal type) to Session for storage
        # The create_child_session returns a Session dataclass
        session_obj = child_session

        return AgentRunContext(
            session=session_obj,
            run=agent_run,
        )

    async def execute_run(
        self,
        context: AgentRunContext,
        config: AgentConfig,
        tool_handler: ToolHandler | None = None,
    ) -> AgentResult:
        """
        Execute an agent using prepared context.

        This method runs the agent loop using the context created by prepare_run().
        For in_process mode only - terminal mode uses a different execution path.

        Args:
            context: Prepared run context from prepare_run().
            config: Agent configuration.
            tool_handler: Optional async callable for handling tool calls.

        Returns:
            AgentResult with execution outcome.
        """
        # Validate context
        if not context.session or not context.run:
            return AgentResult(
                output="",
                status="error",
                error="Invalid context: missing session or run",
                turns_used=0,
            )

        child_session = context.session
        agent_run = context.run

        # Get executor for provider
        executor = self.get_executor(config.provider)
        if not executor:
            error_msg = f"No executor registered for provider: {config.provider}"
            self.logger.error(error_msg)
            self._run_storage.fail(agent_run.id, error=error_msg)
            return AgentResult(
                output="",
                status="error",
                error=error_msg,
                turns_used=0,
            )

        # Start the run
        self._run_storage.start(agent_run.id)
        self.logger.info(
            f"Starting agent run {agent_run.id} "
            f"(child_session={child_session.id}, provider={config.provider})"
        )

        # Track in memory for real-time status
        # Note: parent_session_id is guaranteed non-None here because execute_run
        # is only called after prepare_run validates it
        self._tracker.track(
            run_id=agent_run.id,
            parent_session_id=config.parent_session_id,
            child_session_id=child_session.id,
            provider=config.provider,
            prompt=config.prompt,
            mode=config.mode,
            workflow_name=config.get_effective_workflow(),
            model=config.model,
            worktree_id=config.worktree_id,
        )

        # Set up tool handler with workflow filtering
        async def default_tool_handler(tool_name: str, arguments: dict[str, Any]) -> ToolResult:
            """Default tool handler that returns not implemented."""
            return ToolResult(
                tool_name=tool_name,
                success=False,
                error=f"Tool {tool_name} not implemented",
            )

        base_handler = tool_handler or default_tool_handler

        handler = base_handler

        # Track tool calls to preserve partial progress info on exception
        # Note: Each tool call within a turn counts separately. The executor's
        # run() method handles turns - we only track tool calls for monitoring.
        tool_calls_made = 0

        async def tracking_handler(tool_name: str, arguments: dict[str, Any]) -> ToolResult:
            nonlocal tool_calls_made
            tool_calls_made += 1
            # Update in-memory state for real-time monitoring
            # Note: turns_used is tracked by the executor, not per tool call
            self._tracker.update(
                agent_run.id,
                tool_calls_count=tool_calls_made,
            )
            return await handler(tool_name, arguments)

        async def hook_aware_handler(tool_name: str, arguments: dict[str, Any]) -> ToolResult:
            """Wrap tracking_handler with AFTER_TOOL hook evaluation."""
            result = await tracking_handler(tool_name, arguments)
            if not self._workflow_handler:
                return result
            try:
                from gobby.hooks.events import HookEvent, HookEventType, SessionSource

                event = HookEvent(
                    event_type=HookEventType.AFTER_TOOL,
                    session_id=child_session.id,
                    source=SessionSource.EMBEDDED,
                    timestamp=datetime.now(UTC),
                    data={
                        "tool_name": tool_name,
                        "tool_input": arguments,
                        "is_error": not result.success,
                    },
                    metadata={
                        "is_failure": not result.success,
                        "_platform_session_id": child_session.id,
                    },
                )
                response = await asyncio.to_thread(self._workflow_handler.evaluate, event)
                if response and response.context:
                    if not result.success:
                        result.error = (
                            f"{result.error}\n\n{response.context}"
                            if result.error
                            else response.context
                        )
                    else:
                        result_str = str(result.result) if result.result else ""
                        result.result = (
                            f"{result_str}\n\n{response.context}"
                            if result_str
                            else response.context
                        )
            except Exception:
                logger.debug(
                    "AFTER_TOOL hook eval failed for %s (fail-open)", tool_name, exc_info=True
                )
            return result

        # Execute the agent
        try:
            result = await executor.run(
                prompt=config.prompt,
                tools=config.tools or [],
                tool_handler=hook_aware_handler,
                system_prompt=config.system_prompt,
                model=config.model,
                max_turns=config.max_turns,
                timeout=config.timeout,
            )

            # Update run based on result
            if result.status == "success":
                self._run_storage.complete(
                    agent_run.id,
                    result=result.output,
                    tool_calls_count=len(result.tool_calls),
                    turns_used=result.turns_used,
                )
                self.logger.info(
                    f"Agent run {agent_run.id} completed successfully "
                    f"({result.turns_used} turns, {len(result.tool_calls)} tool calls)"
                )
            elif result.status == "timeout":
                self._run_storage.timeout(agent_run.id, turns_used=result.turns_used)
                self.logger.warning(f"Agent run {agent_run.id} timed out")
            elif result.status == "error":
                self._run_storage.fail(
                    agent_run.id,
                    error=result.error or "Unknown error",
                    tool_calls_count=len(result.tool_calls),
                    turns_used=result.turns_used,
                )
                self.logger.error(f"Agent run {agent_run.id} failed: {result.error}")
            else:
                # Partial completion
                self._run_storage.complete(
                    agent_run.id,
                    result=result.output,
                    tool_calls_count=len(result.tool_calls),
                    turns_used=result.turns_used,
                )
                self.logger.info(f"Agent run {agent_run.id} completed with status {result.status}")

            # Update session status
            if result.status in ("success", "partial"):
                self._session_storage.update_status(child_session.id, "completed")
            else:
                self._session_storage.update_status(child_session.id, "failed")

            # Persist cost to session storage for budget tracking
            if result.cost_info and result.cost_info.total_cost > 0:
                self._session_storage.add_cost(child_session.id, result.cost_info.total_cost)
                self.logger.debug(
                    f"Persisted cost ${result.cost_info.total_cost:.4f} "
                    f"for session {child_session.id}"
                )

            # Remove from in-memory tracking
            self._tracker.untrack(agent_run.id)

            # Set run_id and child_session_id on the result so callers don't need to call list_runs()
            result.run_id = agent_run.id
            result.child_session_id = child_session.id

            return result

        except Exception as e:
            self.logger.error(f"Agent execution failed: {e}", exc_info=True)
            # On exception, we don't know the actual turns used by the executor,
            # so we pass 0. tool_calls_made is the count we tracked.
            self._run_storage.fail(
                agent_run.id,
                error=str(e),
                tool_calls_count=tool_calls_made,
                turns_used=0,
            )
            self._session_storage.update_status(child_session.id, "failed")
            # Remove from in-memory tracking
            self._tracker.untrack(agent_run.id)
            return AgentResult(
                output="",
                status="error",
                error=str(e),
                turns_used=0,
            )

    async def run(
        self,
        config: AgentConfig,
        tool_handler: ToolHandler | None = None,
    ) -> AgentResult:
        """
        Run an agent with the given configuration.

        This is the main entry point that combines prepare_run() and execute_run()
        for in-process agent execution.

        Args:
            config: Agent configuration.
            tool_handler: Optional async callable for handling tool calls.
                If not provided, uses a default no-op handler.

        Returns:
            AgentResult with execution outcome.
        """
        # Prepare the run (create session, run record, workflow state)
        result = self.prepare_run(config)

        # If prepare_run returned an error, return it
        if isinstance(result, AgentResult):
            return result

        # Execute the run with the prepared context
        context = result
        return await self.execute_run(context, config, tool_handler)

    def get_run(self, run_id: str) -> Any | None:
        """Get an agent run by ID. Delegates to runner_queries."""
        return _queries.get_run(self, run_id)

    def get_run_id_by_session(self, session_id: str) -> str | None:
        """Get agent run_id by child session_id. Delegates to runner_queries."""
        return _queries.get_run_id_by_session(self, session_id)

    def list_runs(
        self,
        parent_session_id: str,
        status: str | None = None,
        limit: int = 100,
    ) -> list[Any]:
        """List agent runs for a session. Delegates to runner_queries."""
        return _queries.list_runs(self, parent_session_id, status=status, limit=limit)

    def cancel_run(self, run_id: str) -> bool:
        """Cancel a running agent. Delegates to runner_queries."""
        return _queries.cancel_run(self, run_id)

    def complete_run(self, run_id: str, result: str | None = None) -> bool:
        """Complete a running agent (self-termination). Delegates to runner_queries."""
        return _queries.complete_run(self, run_id, result=result)

    # -------------------------------------------------------------------------
    # In-memory Running Agents Management (delegated to RunTracker)
    # -------------------------------------------------------------------------

    def _track_running_agent(self, **kwargs: Any) -> RunningAgent:
        """Add an agent to tracking. Delegates to RunTracker.track()."""
        return self._tracker.track(**kwargs)

    def _untrack_running_agent(self, run_id: str) -> RunningAgent | None:
        """Remove an agent from tracking. Delegates to RunTracker.untrack()."""
        return self._tracker.untrack(run_id)

    def _update_running_agent(self, run_id: str, **kwargs: Any) -> RunningAgent | None:
        """Update agent tracking state. Delegates to RunTracker.update()."""
        return self._tracker.update(run_id, **kwargs)

    def get_running_agent(self, run_id: str) -> RunningAgent | None:
        """Get a running agent by ID. Delegates to RunTracker.get()."""
        return self._tracker.get(run_id)

    def get_running_agents(self, parent_session_id: str | None = None) -> list[RunningAgent]:
        """Get all running agents. Delegates to RunTracker.get_all()."""
        return self._tracker.get_all(parent_session_id)

    def get_running_agents_count(self) -> int:
        """Get count of running agents. Delegates to RunTracker.count()."""
        return self._tracker.count()

    def is_agent_running(self, run_id: str) -> bool:
        """Check if an agent is running. Delegates to RunTracker.is_running()."""
        return self._tracker.is_running(run_id)
