"""Cron job executor - dispatches jobs by action type."""

from __future__ import annotations

import asyncio
import json
import logging
import math
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, cast

from gobby.config.cron import CronConfig
from gobby.storage.cron import CronJobStorage
from gobby.storage.cron_models import CronJob, CronRun, CronRunStatus
from gobby.telemetry.health_metrics import record_automation_event

if TYPE_CHECKING:
    from gobby.workflows.pipeline_executor import PipelineExecutor

logger = logging.getLogger(__name__)

# Type for registered cron handlers: async callables that receive a CronJob and return output.
CronHandler = Callable[[CronJob], Awaitable[object]]
DEFAULT_DISPATCHER_HEARTBEAT_TICKS = 3
OVERLAP_POLICIES = frozenset({"skip_if_active", "allow"})
FAILURE_RESULT_STATUSES = frozenset({"failed", "failure", "error", "cancelled", "canceled"})
_SHELL_ERROR_TAIL_CHARS = 2000


class CronShellError(RuntimeError):
    """Shell action failed; ``output`` is the complete captured stdout."""

    def __init__(self, message: str, *, output: str) -> None:
        super().__init__(message)
        self.output = output


@dataclass(frozen=True)
class ActionOutcome:
    """Normalized cron action result."""

    status: CronRunStatus
    output: str | None = None
    error: str | None = None
    pipeline_execution_id: str | None = None
    agent_run_id: str | None = None
    background: Callable[[], Coroutine[Any, Any, None]] | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    background_name: str | None = field(default=None, repr=False, compare=False)


class CronExecutor:
    """Dispatches cron jobs to the appropriate execution backend."""

    def __init__(
        self,
        storage: CronJobStorage,
        agent_runner: Any | None = None,
        pipeline_executor: PipelineExecutor | None = None,
        services: object | None = None,
        config: CronConfig | None = None,
        run_db: Callable[..., Awaitable[Any]] | None = None,
    ):
        self.storage = storage
        self.agent_runner = agent_runner
        self.pipeline_executor = pipeline_executor
        self.services = services
        self.config = config or CronConfig()
        self._run_db_callback = run_db
        self._handlers: dict[str, CronHandler] = {}
        self._background_tasks: set[asyncio.Task[None]] = set()

    async def _run_db(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Run synchronous executor storage work outside the event loop."""
        if self._run_db_callback is None:
            return await asyncio.to_thread(func, *args, **kwargs)
        return await self._run_db_callback(func, *args, **kwargs)

    def register_handler(self, name: str, handler: CronHandler) -> None:
        """Register a named handler for the 'handler' action type.

        Args:
            name: Handler name (referenced in action_config["handler"])
            handler: Async callable that receives a CronJob and returns output string
        """
        self._handlers[name] = handler

    def has_handler(self, name: str) -> bool:
        """Return whether a named handler is registered."""
        return name in self._handlers

    async def shutdown(self) -> None:
        """Cancel and await cron executor background tasks."""
        if not self._background_tasks:
            return

        tasks = list(self._background_tasks)
        logger.info("Cancelling %s cron background task(s)", len(tasks))
        for task in tasks:
            task.cancel()

        results = await asyncio.gather(*tasks, return_exceptions=True)
        for task, result in zip(tasks, results, strict=True):
            if isinstance(result, Exception) and not isinstance(result, asyncio.CancelledError):
                logger.warning("Cron background task %s raised during shutdown: %s", task, result)

        self._background_tasks.clear()

    def _action_timeout_seconds(self, job: CronJob) -> float:
        """Resolve and validate the outer timeout for a bounded cron action."""
        configured = job.action_config.get("timeout_seconds")
        if configured is None:
            return float(self.config.running_timeout_seconds)
        if (
            isinstance(configured, bool)
            or not isinstance(configured, (int, float))
            or not math.isfinite(configured)
            or configured <= 0
        ):
            raise ValueError("action_config.timeout_seconds must be a positive finite number")
        return float(configured)

    async def _wait_for_action(
        self,
        job: CronJob,
        action_factory: Callable[[], Awaitable[object]],
    ) -> object:
        """Await a bounded cron action within its configured outer timeout."""
        timeout = self._action_timeout_seconds(job)
        try:
            return await asyncio.wait_for(action_factory(), timeout=timeout)
        except TimeoutError as exc:
            raise RuntimeError(
                f"{job.action_type} cron action timed out after {timeout:g}s"
            ) from exc

    async def execute(self, job: CronJob, run: CronRun) -> CronRun:
        """Execute a cron job and update the run record.

        Args:
            job: The cron job to execute
            run: The cron run record to update

        Returns:
            Updated CronRun with status and output
        """
        now = datetime.now(UTC).isoformat()
        await self._run_db(self.storage.update_run, run.id, status="running", started_at=now)
        record_automation_event("cron", "fired")

        outcome: ActionOutcome
        try:
            raw_output: object
            if job.action_type == "agent_spawn":
                raw_output = await self._wait_for_action(
                    job,
                    lambda: self._execute_agent_spawn(job),
                )
            elif job.action_type == "pipeline":
                raw_output = await self._wait_for_action(
                    job,
                    lambda: self._execute_pipeline(job, run),
                )
            elif job.action_type == "shell":
                raw_output = await self._execute_shell(job)
            elif job.action_type == "handler":
                raw_output = await self._wait_for_action(
                    job,
                    lambda: self._execute_handler(job),
                )
            elif job.action_type == "dispatcher":
                raw_output = await self._execute_dispatcher(job)
            else:
                raise ValueError(f"Unknown action_type: {job.action_type}")

            outcome = self._coerce_action_result(raw_output)
        except Exception as e:
            logger.exception("Cron job %s (%s) failed", job.id, job.name)
            captured = getattr(e, "output", None)
            outcome = ActionOutcome(
                status="failed",
                output=captured if isinstance(captured, str) else None,
                error=str(e),
            )

        completed_at = datetime.now(UTC).isoformat()
        updated = await self._run_db(
            self.storage.update_run,
            run.id,
            status=outcome.status,
            completed_at=completed_at,
            output=outcome.output,
            error=outcome.error,
            agent_run_id=outcome.agent_run_id,
            pipeline_execution_id=outcome.pipeline_execution_id,
        )
        if outcome.background is not None:
            task: asyncio.Task[None] = asyncio.create_task(
                outcome.background(),
                name=outcome.background_name,
            )
            self._track_background_task(task)
        else:
            terminal_outcome = "failed" if outcome.status == "failed" else "succeeded"
            record_automation_event("cron", terminal_outcome)
        return updated or run

    def _coerce_action_result(self, result: object) -> ActionOutcome:
        if isinstance(result, ActionOutcome):
            return result
        if isinstance(result, Mapping):
            return self._coerce_mapping_result(result, self._serialize_mapping(result))
        if isinstance(result, str):
            parsed = self._parse_json_object(result)
            if parsed is not None:
                return self._coerce_mapping_result(parsed, result)
            return ActionOutcome(status="completed", output=result)
        if result is None:
            return ActionOutcome(status="completed", output="")
        return ActionOutcome(status="completed", output=str(result))

    def _coerce_mapping_result(self, result: Mapping[str, Any], output: str) -> ActionOutcome:
        status = str(result.get("status", "")).lower()
        failed = (
            result.get("success") is False
            or result.get("ok") is False
            or status in FAILURE_RESULT_STATUSES
        )
        if failed:
            error = result.get("error") or result.get("message") or output
            return ActionOutcome(status="failed", output=output, error=str(error))
        return ActionOutcome(status="completed", output=output)

    def _parse_json_object(self, value: str) -> dict[str, Any] | None:
        stripped = value.strip()
        if not stripped.startswith("{"):
            return None
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None

    def _serialize_mapping(self, value: Mapping[str, Any]) -> str:
        try:
            return json.dumps(dict(value), sort_keys=True)
        except TypeError:
            return str(value)

    def _overlap_policy(self, job: CronJob) -> Literal["skip_if_active", "allow"]:
        policy = job.action_config.get("overlap_policy", "skip_if_active")
        if not isinstance(policy, str) or policy not in OVERLAP_POLICIES:
            raise ValueError(
                "Invalid overlap_policy for cron job "
                f"{job.id}: {policy!r}; expected 'skip_if_active' or 'allow'"
            )
        if policy == "allow":
            return "allow"
        return "skip_if_active"

    def _active_child_skip_outcome(self, job: CronJob) -> ActionOutcome | None:
        if self._overlap_policy(job) == "allow":
            return None
        active_children = self.storage.active_children_for_job(job.id, job.action_type)
        if not active_children:
            return None
        child = active_children[0]
        return ActionOutcome(
            status="skipped",
            output=(f"Skipped: active child {child['type']} {child['id']} is {child['status']}"),
        )

    def _pipeline_executor_for(self, project_id: str) -> PipelineExecutor | None:
        """Resolve pipeline infrastructure in the cron job's project scope."""
        getter = getattr(self.services, "get_pipeline_executor", None)
        if callable(getter):
            executor = getter(project_id)
            if executor is not None:
                return cast("PipelineExecutor", executor)
        return self.pipeline_executor

    async def _execute_agent_spawn(self, job: CronJob) -> ActionOutcome:
        """Execute an agent_spawn action."""
        skipped = cast(
            ActionOutcome | None,
            await self._run_db(self._active_child_skip_outcome, job),
        )
        if skipped is not None:
            return skipped

        from gobby.agents.readiness import spawn_readiness_blocker

        readiness_reason = spawn_readiness_blocker(self.services)
        if readiness_reason is not None:
            logger.info(
                "Cron agent spawn skipped",
                extra={"readiness_reason": readiness_reason},
            )
            return ActionOutcome(
                status="skipped",
                output=f"Agent spawn skipped: {readiness_reason}",
            )
        if not self.agent_runner:
            raise RuntimeError("agent_runner not configured for cron executor")

        config = job.action_config
        prompt = config.get("prompt", "")
        if not prompt:
            raise ValueError("agent_spawn action requires a 'prompt' in action_config")

        provider = config.get("provider", "claude")
        timeout = config.get("timeout_seconds", 300)
        workflow = config.get("workflow")

        # Resolve agent_definition if specified
        agent_def_name = config.get("agent_definition")
        if agent_def_name:
            from gobby.workflows.agent_resolver import resolve_agent

            agent_body = await self._run_db(
                resolve_agent,
                agent_def_name,
                self.storage.db,
                project_id=job.project_id,
            )
            if agent_body:
                preamble = agent_body.build_prompt_preamble()
                if preamble:
                    prompt = f"{preamble}\n\n---\n\n{prompt}"
                # Use agent definition's provider if no explicit provider in config
                if "provider" not in config and agent_body.provider != "inherit":
                    provider = agent_body.provider

        # Spawn agent via spawn_agent_impl (all agents go through tmux)
        from gobby.mcp_proxy.tools.spawn_agent._implementation import spawn_agent_impl

        daemon_config = getattr(self.services, "config", None) or getattr(
            self.agent_runner, "config", None
        )
        from gobby.agents.spawn_models import resolve_terminal_backend

        scheduled_backend = resolve_terminal_backend(None, daemon_config)
        result = await spawn_agent_impl(
            prompt=prompt,
            runner=self.agent_runner,
            provider=provider,
            workflow=workflow,
            timeout=timeout,
            parent_session_id=job.project_id,  # Cron jobs use project as parent context
            session_manager=getattr(self.agent_runner, "child_session_manager", None),
            db=self.storage.db,
            completion_registry=getattr(self.services, "completion_registry", None),
            daemon_config=daemon_config,
            terminal_backend=scheduled_backend,
        )

        if result.get("success") is True:
            run_id = result.get("run_id")
            if isinstance(run_id, str) and run_id:
                return ActionOutcome(
                    status="dispatched",
                    output=f"Agent dispatched: run_id={run_id}",
                    agent_run_id=run_id,
                )
            return ActionOutcome(
                status="failed",
                error="Agent spawn succeeded without a structured run_id",
            )
        error = result.get("error", "unknown")
        return ActionOutcome(status="failed", error=f"Agent spawn failed: {error}")

    async def _execute_pipeline(self, job: CronJob, run: CronRun) -> ActionOutcome:
        """Execute a pipeline action."""
        skipped = cast(
            ActionOutcome | None,
            await self._run_db(self._active_child_skip_outcome, job),
        )
        if skipped is not None:
            return skipped

        pipeline_executor = self._pipeline_executor_for(job.project_id)
        if pipeline_executor is None:
            raise RuntimeError("pipeline_executor not configured for cron executor")

        config = job.action_config
        pipeline_name = config.get("pipeline_name")
        if not pipeline_name:
            raise ValueError("pipeline action requires 'pipeline_name' in action_config")

        inputs = config.get("inputs", {})

        # Use pipeline_executor's loader (has DB context) instead of bare PipelineLoader()
        loader = pipeline_executor.loader
        if not loader:
            raise RuntimeError("pipeline_executor has no loader configured")
        pipeline = await loader.load_pipeline(pipeline_name, job.project_id)
        if not pipeline:
            raise ValueError(f"Pipeline '{pipeline_name}' not found")
        if not pipeline.enabled:
            return ActionOutcome(
                status="skipped",
                output=f"Skipped: pipeline '{pipeline_name}' is disabled",
            )

        # Create a session for the cron-triggered pipeline so spawned agents
        # have a valid parent_session_id (required by spawn_agent).
        # The system session is the root parent for all cron-triggered work.
        from gobby.storage.sessions import system_session_id

        session_id: str | None = None
        sm = pipeline_executor.session_manager
        if sm:
            try:
                cron_session = await self._run_db(
                    sm.register,
                    external_id=f"cron-{job.id}-{run.id}-{pipeline_name}",
                    machine_id=None,
                    source="cron",
                    project_id=job.project_id,
                    title=f"cron:{job.name}",
                    parent_session_id=system_session_id(),
                    agent_depth=0,
                )
                session_id = cron_session.id
            except Exception:
                logger.warning("Failed to create session for cron pipeline", exc_info=True)

        # Set project context so MCP tools can resolve task refs like #9916
        # Must include project_path — spawn_agent_impl requires it.
        project_ctx: dict[str, Any] | None = None
        if job.project_id:
            try:
                project_ctx = await self._run_db(self._pipeline_project_context, job.project_id)
            except Exception:
                logger.debug(
                    "Failed to resolve repo_path for project %s", job.project_id, exc_info=True
                )

        execution_manager = getattr(pipeline_executor, "execution_manager", None)
        if execution_manager is None:
            raise RuntimeError("pipeline_executor has no execution_manager configured")

        try:
            definition_snapshot = pipeline.model_dump_json()
        except Exception:
            definition_snapshot = json.dumps(
                {"name": pipeline.name, "error": "serialization failed"}
            )

        execution = await self._run_db(
            execution_manager.create_execution,
            pipeline_name=pipeline.name,
            inputs_json=json.dumps(inputs),
            session_id=session_id,
            definition_json=definition_snapshot,
        )

        def _background() -> Coroutine[Any, Any, None]:
            return self._run_pipeline_background(
                job=job,
                pipeline_executor=pipeline_executor,
                pipeline=pipeline,
                inputs=inputs,
                project_id=job.project_id,
                cron_run_id=run.id,
                execution_id=execution.id,
                pipeline_name=pipeline.name,
                session_id=session_id,
                project_ctx=project_ctx,
            )

        return ActionOutcome(
            status="dispatched",
            output=f"Pipeline dispatched: execution_id={execution.id}",
            pipeline_execution_id=execution.id,
            background=_background,
            background_name=f"cron-pipeline-{pipeline.name}-{execution.id[:8]}",
        )

    def _pipeline_project_context(self, project_id: str) -> dict[str, Any]:
        """Resolve the project path used by cron-triggered pipeline agents."""
        from gobby.storage.projects import LocalProjectManager

        project_ctx: dict[str, Any] = {"id": project_id}
        project = LocalProjectManager(self.storage.db).get(project_id)
        if project is not None and project.repo_path:
            project_ctx["project_path"] = project.repo_path
        return project_ctx

    async def _run_pipeline_background(
        self,
        *,
        job: CronJob,
        pipeline_executor: PipelineExecutor,
        pipeline: Any,
        inputs: dict[str, Any],
        project_id: str,
        cron_run_id: str,
        execution_id: str,
        pipeline_name: str,
        session_id: str | None,
        project_ctx: dict[str, Any] | None,
    ) -> None:
        """Run a pre-created pipeline execution in the background."""
        from gobby.utils.project_context import reset_project_context, set_project_context
        from gobby.workflows.pipeline_state import ApprovalRequired, ExecutionStatus

        token = set_project_context(project_ctx)
        try:
            try:
                completed_execution = await self._wait_for_action(
                    job,
                    lambda: pipeline_executor.execute(
                        pipeline=pipeline,
                        inputs=inputs,
                        project_id=project_id,
                        execution_id=execution_id,
                        session_id=session_id,
                    ),
                )
            except ApprovalRequired:
                return
            except Exception as e:
                logger.exception("Background pipeline '%s' failed: %s", pipeline_name, e)
                try:
                    await self._run_db(
                        self._record_pipeline_execution_failure,
                        pipeline_executor,
                        execution_id,
                        str(e),
                    )
                except Exception:
                    logger.exception("Failed to mark background pipeline as failed")
                await self._run_db(
                    self._record_pipeline_run_failure,
                    cron_run_id,
                    execution_id,
                    str(e),
                )
                record_automation_event("cron", "failed")
                return

            execution_status = getattr(
                completed_execution,
                "status",
                ExecutionStatus.COMPLETED,
            )
            if execution_status == ExecutionStatus.FAILED:
                error = f"Pipeline failed: execution_id={execution_id}"
                await self._run_db(
                    self._record_pipeline_run_failure,
                    cron_run_id,
                    execution_id,
                    error,
                )
                record_automation_event("cron", "failed")
            elif execution_status == ExecutionStatus.CANCELLED:
                error = f"Pipeline cancelled: execution_id={execution_id}"
                await self._run_db(
                    self._record_pipeline_run_failure,
                    cron_run_id,
                    execution_id,
                    error,
                )
                record_automation_event("cron", "failed")
            else:
                await self._run_db(
                    self.storage.update_run,
                    cron_run_id,
                    status="completed",
                    completed_at=datetime.now(UTC).isoformat(),
                    output=f"Pipeline completed: execution_id={execution_id}",
                    error=None,
                )
                record_automation_event("cron", "succeeded")
        finally:
            reset_project_context(token)

    def _record_pipeline_execution_failure(
        self,
        pipeline_executor: PipelineExecutor,
        execution_id: str,
        error: str,
    ) -> None:
        """Fail running pipeline steps and their parent execution."""
        from gobby.workflows.pipeline_state import ExecutionStatus, StepStatus

        execution_manager = pipeline_executor.execution_manager
        steps = execution_manager.get_steps_for_execution(execution_id)
        for step in steps:
            if step.status == StepStatus.RUNNING:
                execution_manager.update_step_execution(
                    step_execution_id=step.id,
                    status=StepStatus.FAILED,
                    error=error,
                )
        execution_manager.update_execution_status(
            execution_id=execution_id,
            status=ExecutionStatus.FAILED,
            outputs_json=json.dumps({"error": error}),
        )

    def _record_pipeline_run_failure(
        self,
        cron_run_id: str,
        execution_id: str,
        error: str,
    ) -> None:
        """Persist a background pipeline failure on its originating cron run."""
        self.storage.update_run(
            cron_run_id,
            status="failed",
            completed_at=datetime.now(UTC).isoformat(),
            error=error,
            pipeline_execution_id=execution_id,
        )

    def _track_background_task(self, task: asyncio.Task[None]) -> None:
        self._background_tasks.add(task)

        def _on_done(done: asyncio.Task[None]) -> None:
            self._background_tasks.discard(done)
            if not done.cancelled() and done.exception():
                logger.error("Cron background task failed: %s", done.exception())

        task.add_done_callback(_on_done)

    async def _execute_shell(self, job: CronJob) -> str:
        """Execute a shell command action."""
        config = job.action_config
        command = config.get("command")
        if not command:
            raise ValueError("shell action requires 'command' in action_config")

        args = config.get("args", [])
        cwd = config.get("cwd")
        timeout = config.get("timeout_seconds", 60)

        cmd = [command] + args

        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )

            stdout, _ = await asyncio.wait_for(
                process.communicate(),
                timeout=timeout,
            )

            output = stdout.decode("utf-8", errors="replace") if stdout else ""

            if process.returncode != 0:
                if len(output) <= _SHELL_ERROR_TAIL_CHARS:
                    preview = output
                    message = f"Command exited with code {process.returncode}: {preview}"
                else:
                    preview = output[-_SHELL_ERROR_TAIL_CHARS:]
                    message = (
                        f"Command exited with code {process.returncode}: [truncated]\n"
                        f"{preview} full output stored on cron run ({len(output)} chars)"
                    )
                raise CronShellError(message, output=output)

            return output

        except asyncio.CancelledError:
            if process is not None:
                try:
                    process.kill()
                    await process.wait()
                except ProcessLookupError:
                    pass
            raise
        except TimeoutError as err:
            if process:
                process.terminate()
            raise RuntimeError(f"Shell command timed out after {timeout}s") from err

    async def _execute_handler(self, job: CronJob) -> object:
        """Execute a registered handler action.

        The handler name is read from action_config["handler"] and dispatched
        to a previously registered async callable.
        """
        name = job.action_config.get("handler")
        if not name:
            raise ValueError("handler action requires 'handler' in action_config")
        handler = self._handlers.get(name)
        if not handler:
            available = list(self._handlers.keys())
            raise ValueError(f"No handler registered: '{name}'. Available: {available}")
        return await handler(job)

    async def _execute_dispatcher(self, job: CronJob) -> ActionOutcome:
        """Execute the dispatcher heartbeat action."""
        from gobby.dispatch.dispatcher import run_heartbeat

        config = job.action_config
        project_id = config.get("project_id", job.project_id)
        max_ticks = self._dispatcher_heartbeat_ticks(job)
        ticks = 0
        scanned = 0
        executed = 0
        skipped = 0
        cap_reached = False
        reason: str | None = None

        for _ in range(max_ticks):
            result = await run_heartbeat(
                db=self.storage.db,
                project_id=project_id,
                startup=bool(config.get("startup", False)),
                max_active_agents=config.get("max_active_agents"),
                services=self.services,
            )
            ticks += 1
            scanned += result.scanned
            executed += result.executed
            skipped += result.skipped
            cap_reached = cap_reached or result.cap_reached
            reason = result.reason or ("cap_reached" if result.cap_reached else reason)

            if result.executed == 0 or result.cap_reached or result.reason:
                break

        output = (
            "Dispatcher heartbeat completed: "
            f"ticks={ticks}, "
            f"scanned={scanned}, "
            f"executed={executed}, "
            f"skipped={skipped}, "
            f"cap_reached={cap_reached}, "
            f"reason={reason}"
        )
        if cap_reached or reason:
            return ActionOutcome(
                status="failed",
                output=output,
                error=f"Dispatcher heartbeat stopped: {reason or 'cap_reached'}",
            )
        return ActionOutcome(status="completed", output=output)

    def _dispatcher_heartbeat_ticks(self, job: CronJob) -> int:
        config = job.action_config
        raw = config.get("max_ticks")
        if raw is None:
            return DEFAULT_DISPATCHER_HEARTBEAT_TICKS if job.name == "gobby:dispatcher" else 1
        try:
            max_ticks = int(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("dispatcher action max_ticks must be a positive integer") from exc
        if max_ticks < 1:
            raise ValueError("dispatcher action max_ticks must be a positive integer")
        return max_ticks
