"""Cron job executor - dispatches jobs by action type."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from gobby.storage.cron import CronJobStorage
from gobby.storage.cron_models import CronJob, CronRun, CronRunStatus

if TYPE_CHECKING:
    from gobby.workflows.pipeline_executor import PipelineExecutor

logger = logging.getLogger(__name__)

# Type for registered cron handlers: async callables that receive a CronJob and return output.
CronHandler = Callable[[CronJob], Awaitable[object]]
DEFAULT_DISPATCHER_HEARTBEAT_TICKS = 3
OVERLAP_POLICIES = frozenset({"skip_if_active", "allow"})
FAILURE_RESULT_STATUSES = frozenset({"failed", "failure", "error", "cancelled", "canceled"})


@dataclass(frozen=True)
class ActionOutcome:
    """Normalized cron action result."""

    status: CronRunStatus
    output: str | None = None
    error: str | None = None
    pipeline_execution_id: str | None = None
    agent_run_id: str | None = None


class CronExecutor:
    """Dispatches cron jobs to the appropriate execution backend."""

    def __init__(
        self,
        storage: CronJobStorage,
        agent_runner: Any | None = None,
        pipeline_executor: PipelineExecutor | None = None,
        services: object | None = None,
    ):
        self.storage = storage
        self.agent_runner = agent_runner
        self.pipeline_executor = pipeline_executor
        self.services = services
        self._handlers: dict[str, CronHandler] = {}
        self._background_tasks: set[asyncio.Task[None]] = set()

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

    async def execute(self, job: CronJob, run: CronRun) -> CronRun:
        """Execute a cron job and update the run record.

        Args:
            job: The cron job to execute
            run: The cron run record to update

        Returns:
            Updated CronRun with status and output
        """
        now = datetime.now(UTC).isoformat()
        self.storage.update_run(run.id, status="running", started_at=now)

        outcome: ActionOutcome
        try:
            raw_output: object
            if job.action_type == "agent_spawn":
                raw_output = await self._execute_agent_spawn(job)
            elif job.action_type == "pipeline":
                raw_output = await self._execute_pipeline(job, run)
            elif job.action_type == "shell":
                raw_output = await self._execute_shell(job)
            elif job.action_type == "handler":
                raw_output = await self._execute_handler(job)
            elif job.action_type == "dispatcher":
                raw_output = await self._execute_dispatcher(job)
            else:
                raise ValueError(f"Unknown action_type: {job.action_type}")

            outcome = self._coerce_action_result(raw_output)
        except Exception as e:
            logger.exception(f"Cron job {job.id} ({job.name}) failed")
            outcome = ActionOutcome(status="failed", error=str(e))

        completed_at = datetime.now(UTC).isoformat()
        updated = self.storage.update_run(
            run.id,
            status=outcome.status,
            completed_at=completed_at,
            output=self._truncate(outcome.output, 10000),
            error=self._truncate(outcome.error, 5000),
            agent_run_id=outcome.agent_run_id,
            pipeline_execution_id=outcome.pipeline_execution_id,
        )
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

    def _truncate(self, value: str | None, limit: int) -> str | None:
        if value is None or len(value) <= limit:
            return value
        return value[:limit]

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

    async def _execute_agent_spawn(self, job: CronJob) -> ActionOutcome:
        """Execute an agent_spawn action."""
        skipped = self._active_child_skip_outcome(job)
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

            agent_body = resolve_agent(
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

        result = await spawn_agent_impl(
            prompt=prompt,
            runner=self.agent_runner,
            provider=provider,
            workflow=workflow,
            timeout=timeout,
            parent_session_id=job.project_id,  # Cron jobs use project as parent context
            session_manager=getattr(self.agent_runner, "child_session_manager", None),
            db=self.storage.db,
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
        skipped = self._active_child_skip_outcome(job)
        if skipped is not None:
            return skipped

        if not self.pipeline_executor:
            raise RuntimeError("pipeline_executor not configured for cron executor")

        config = job.action_config
        pipeline_name = config.get("pipeline_name")
        if not pipeline_name:
            raise ValueError("pipeline action requires 'pipeline_name' in action_config")

        inputs = config.get("inputs", {})

        # Use pipeline_executor's loader (has DB context) instead of bare WorkflowLoader()
        loader = self.pipeline_executor.loader
        if not loader:
            raise RuntimeError("pipeline_executor has no loader configured")
        pipeline = await loader.load_pipeline(pipeline_name)
        if not pipeline:
            raise ValueError(f"Pipeline '{pipeline_name}' not found")

        # Create a session for the cron-triggered pipeline so spawned agents
        # have a valid parent_session_id (required by spawn_agent).
        # The system session is the root parent for all cron-triggered work.
        from gobby.storage.sessions import SYSTEM_SESSION_ID

        session_id: str | None = None
        sm = self.pipeline_executor.session_manager
        if sm:
            try:
                cron_session = sm.register(
                    external_id=f"cron-{job.id}-{run.id}-{pipeline_name}",
                    machine_id="cron",
                    source="cron",
                    project_id=job.project_id,
                    title=f"cron:{job.name}",
                    parent_session_id=SYSTEM_SESSION_ID,
                    agent_depth=0,
                )
                session_id = cron_session.id
            except Exception:
                logger.warning("Failed to create session for cron pipeline", exc_info=True)

        # Set project context so MCP tools can resolve task refs like #9916
        # Must include project_path — spawn_agent_impl requires it.
        project_ctx: dict[str, Any] | None = None
        if job.project_id:
            project_ctx = {"id": job.project_id}
            # Look up repo_path from projects table
            try:
                row = self.storage.db.execute(
                    "SELECT repo_path FROM projects WHERE id = %s",
                    (job.project_id,),
                ).fetchone()
                if row and row["repo_path"]:
                    project_ctx["project_path"] = row["repo_path"]
            except Exception:
                logger.debug(
                    f"Failed to resolve repo_path for project {job.project_id}", exc_info=True
                )

        execution_manager = getattr(self.pipeline_executor, "execution_manager", None)
        if execution_manager is None:
            raise RuntimeError("pipeline_executor has no execution_manager configured")

        try:
            definition_snapshot = pipeline.model_dump_json()
        except Exception:
            definition_snapshot = json.dumps(
                {"name": pipeline.name, "error": "serialization failed"}
            )

        execution = execution_manager.create_execution(
            pipeline_name=pipeline.name,
            inputs_json=json.dumps(inputs),
            session_id=session_id,
            definition_json=definition_snapshot,
        )

        task = asyncio.create_task(
            self._run_pipeline_background(
                pipeline=pipeline,
                inputs=inputs,
                project_id=job.project_id,
                execution_id=execution.id,
                pipeline_name=pipeline.name,
                session_id=session_id,
                project_ctx=project_ctx,
            ),
            name=f"cron-pipeline-{pipeline.name}-{execution.id[:8]}",
        )
        self._track_background_task(task)

        return ActionOutcome(
            status="dispatched",
            output=f"Pipeline dispatched: execution_id={execution.id}",
            pipeline_execution_id=execution.id,
        )

    async def _run_pipeline_background(
        self,
        *,
        pipeline: Any,
        inputs: dict[str, Any],
        project_id: str,
        execution_id: str,
        pipeline_name: str,
        session_id: str | None,
        project_ctx: dict[str, Any] | None,
    ) -> None:
        """Run a pre-created pipeline execution in the background."""
        from gobby.utils.project_context import reset_project_context, set_project_context
        from gobby.workflows.pipeline_state import ApprovalRequired, ExecutionStatus, StepStatus

        if not self.pipeline_executor:
            return

        token = set_project_context(project_ctx)
        try:
            try:
                await self.pipeline_executor.execute(
                    pipeline=pipeline,
                    inputs=inputs,
                    project_id=project_id,
                    execution_id=execution_id,
                    session_id=session_id,
                )
            except ApprovalRequired:
                pass
            except Exception as e:
                logger.error(f"Background pipeline '{pipeline_name}' failed: {e}", exc_info=True)
                try:
                    steps = self.pipeline_executor.execution_manager.get_steps_for_execution(
                        execution_id
                    )
                    for step in steps:
                        if step.status == StepStatus.RUNNING:
                            self.pipeline_executor.execution_manager.update_step_execution(
                                step_execution_id=step.id,
                                status=StepStatus.FAILED,
                                error=str(e),
                            )
                    self.pipeline_executor.execution_manager.update_execution_status(
                        execution_id=execution_id,
                        status=ExecutionStatus.FAILED,
                        outputs_json=json.dumps({"error": str(e)}),
                    )
                except Exception:
                    logger.error("Failed to mark background pipeline as failed", exc_info=True)
        finally:
            reset_project_context(token)

    def _track_background_task(self, task: asyncio.Task[None]) -> None:
        self._background_tasks.add(task)

        def _on_done(done: asyncio.Task[None]) -> None:
            self._background_tasks.discard(done)
            if not done.cancelled() and done.exception():
                logger.error(f"Cron background task failed: {done.exception()}")

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
                raise RuntimeError(
                    f"Command exited with code {process.returncode}: {output[:2000]}"
                )

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

    async def _execute_dispatcher(self, job: CronJob) -> str:
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

        return (
            "Dispatcher heartbeat completed: "
            f"ticks={ticks}, "
            f"scanned={scanned}, "
            f"executed={executed}, "
            f"skipped={skipped}, "
            f"cap_reached={cap_reached}, "
            f"reason={reason}"
        )

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
