"""Run-command effect preparation, execution, delivery, and auditing."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from gobby.hooks.background_tasks import create_background_task
from gobby.hooks.effect_deadline import (
    BLOCKING_EFFECT_BUDGET_SECONDS,
    BlockingEffectDeadline,
    remaining_blocking_effect_seconds,
)
from gobby.hooks.events import ContextPart, HookEvent
from gobby.skills.materialization import SkillScriptMaterializer
from gobby.storage.definitions.rules import RuleDefinitionRow
from gobby.workflows.engine._offload import offload
from gobby.workflows.engine.run_command import (
    RunCommandResult,
    build_run_command_payload,
    execute_run_command,
    resolve_materialized_skill_script,
)

logger = logging.getLogger(__name__)


_RUN_COMMAND_DEFAULT_TIMEOUT_SECONDS = 5.0
_RUN_COMMAND_BACKGROUND_DEFAULT_TIMEOUT_SECONDS = 30.0
_RUN_COMMAND_INLINE_FLOOR_SECONDS = 1.0


@dataclass(frozen=True)
class _PreparedRunCommand:
    command: list[str]
    environment: dict[str, str]
    scripts_dir: Path | None = None
    script: str | None = None


class RunCommandEffectsMixin:
    """Mixin providing bounded run-command rule effects."""

    _background_run_commands: dict[tuple[str, str], asyncio.Task[None]]
    db: Any
    skill_script_materializer: SkillScriptMaterializer

    def __init__(self) -> None:
        self._background_run_commands = {}

    async def _apply_run_command(
        self,
        effect: Any,
        row: RuleDefinitionRow,
        ctx: dict[str, Any],
        context_parts: list[ContextPart],
    ) -> None:
        """Execute a bounded detector command and fail open on every failure."""
        event = ctx.get("event")
        if not isinstance(event, HookEvent):
            return

        try:
            payload = build_run_command_payload(event)
            stdin_payload = json.dumps(payload).encode("utf-8")
        except Exception:
            logger.warning("run_command[%s]: event payload could not be encoded", row.name)
            return
        cwd = str(payload["cwd"])
        platform_session_id = event.metadata.get("_platform_session_id") if event.metadata else None
        if not isinstance(platform_session_id, str) or not platform_session_id:
            platform_session_id = None

        if effect.background:
            timeout = effect.timeout_seconds or _RUN_COMMAND_BACKGROUND_DEFAULT_TIMEOUT_SECONDS
            registry = self._background_run_command_registry()
            key = (event.session_id, str(row.id))
            existing = registry.get(key)
            if existing is not None and not existing.done():
                logger.info(
                    "run_command[%s]: suppressed duplicate background run for session %s",
                    row.name,
                    event.session_id,
                    extra={"rule_name": row.name, "session_id": event.session_id},
                )
                return
            task = create_background_task(
                self._run_command_then_deliver(
                    [str(part) for part in (effect.command or [])],
                    cwd,
                    stdin_payload,
                    timeout,
                    project_id=event.project_id,
                    skill=effect.skill,
                    script=effect.script,
                    rule_name=row.name,
                    rule_id=str(row.id),
                    platform_session_id=platform_session_id,
                )
            )
            registry[key] = task

            def cleanup(completed: asyncio.Task[None]) -> None:
                if registry.get(key) is completed:
                    registry.pop(key, None)

            task.add_done_callback(cleanup)
            return

        cap = min(
            effect.timeout_seconds or _RUN_COMMAND_DEFAULT_TIMEOUT_SECONDS,
            BLOCKING_EFFECT_BUDGET_SECONDS,
        )
        floor = min(_RUN_COMMAND_INLINE_FLOOR_SECONDS, cap)
        deadline = ctx.get("_blocking_deadline")
        if not isinstance(deadline, BlockingEffectDeadline):
            deadline = None
        preparation_timeout = max(
            floor,
            remaining_blocking_effect_seconds(deadline, maximum=cap),
        )
        preparation_started = time.perf_counter()
        prepared = await self._prepare_run_command(
            [str(part) for part in (effect.command or [])],
            project_id=event.project_id,
            skill=effect.skill,
            script=effect.script,
            timeout=preparation_timeout,
            background=False,
        )
        if isinstance(prepared, RunCommandResult):
            await self._record_run_command_failure(
                prepared,
                rule_name=row.name,
                rule_id=str(row.id),
                platform_session_id=platform_session_id,
            )
            return

        preparation_elapsed = time.perf_counter() - preparation_started
        remaining_budget = remaining_blocking_effect_seconds(deadline, maximum=cap)
        unused_preparation = max(0.0, preparation_timeout - preparation_elapsed)
        execution_timeout = max(floor, min(remaining_budget, unused_preparation))
        result = await self._execute_run_command(
            prepared,
            cwd,
            stdin_payload,
            execution_timeout,
            skill=effect.skill,
            script=effect.script,
            rule_name=row.name,
            rule_id=str(row.id),
            platform_session_id=platform_session_id,
            background=False,
        )
        if result.context and effect.inject_result:
            context_parts.append((f"rule:{row.name}", result.context))

    async def _prepare_run_command(
        self,
        command: list[str],
        *,
        project_id: str | None,
        skill: str | None,
        script: str | None,
        timeout: float,
        background: bool,
    ) -> _PreparedRunCommand | RunCommandResult:
        if skill is None or script is None:
            return _PreparedRunCommand(command, {})

        started = time.perf_counter()
        try:
            materialized = await asyncio.wait_for(
                self.skill_script_materializer.resolve(skill, project_id=project_id),
                timeout=timeout,
            )
            resolve_materialized_skill_script(materialized.scripts_dir, script)
        except TimeoutError:
            return RunCommandResult.skill_resolution_failure(
                "skill_resolution_timeout",
                started=started,
                timeout_seconds=timeout,
                background=background,
                skill=skill,
                script=script,
            )
        except Exception:
            logger.debug(
                "run_command: skill resolution failed",
                extra={"skill": skill, "script": script},
                exc_info=True,
            )
            return RunCommandResult.skill_resolution_failure(
                "skill_resolution_error",
                started=started,
                timeout_seconds=timeout,
                background=background,
                skill=skill,
                script=script,
            )
        return _PreparedRunCommand(
            command,
            materialized.environment,
            scripts_dir=materialized.scripts_dir,
            script=script,
        )

    async def _execute_run_command(
        self,
        prepared: _PreparedRunCommand,
        cwd: str,
        stdin_payload: bytes,
        timeout: float,
        *,
        skill: str | None,
        script: str | None,
        rule_name: str,
        rule_id: str,
        platform_session_id: str | None,
        background: bool,
    ) -> RunCommandResult:
        spawn_guard: AbstractAsyncContextManager[None] | None = None
        command_factory = None
        if prepared.scripts_dir is not None and prepared.script is not None:
            scripts_dir = prepared.scripts_dir
            materialized_script = prepared.script
            spawn_guard = self.skill_script_materializer.execution_guard(scripts_dir)

            def resolve_command() -> list[str]:
                target = resolve_materialized_skill_script(scripts_dir, materialized_script)
                return [*prepared.command, str(target)]

            command_factory = resolve_command

        result = replace(
            await execute_run_command(
                prepared.command,
                cwd=cwd,
                stdin_payload=stdin_payload,
                timeout_seconds=timeout,
                background=background,
                environment=prepared.environment,
                spawn_guard=spawn_guard,
                command_factory=command_factory,
            ),
            skill=skill,
            script=script,
        )
        if result.status == "success":
            await self._audit_run_command(
                result,
                rule_name=rule_name,
                rule_id=rule_id,
                platform_session_id=platform_session_id,
            )
        else:
            await self._record_run_command_failure(
                result,
                rule_name=rule_name,
                rule_id=rule_id,
                platform_session_id=platform_session_id,
            )
        return result

    async def _run_command_then_deliver(
        self,
        command: list[str],
        cwd: str,
        stdin_payload: bytes,
        timeout: float,
        *,
        project_id: str | None,
        skill: str | None,
        script: str | None,
        rule_name: str,
        rule_id: str,
        platform_session_id: str | None,
    ) -> None:
        """Background variant: run the command, deliver output on the next turn."""
        started = time.perf_counter()
        prepared = await self._prepare_run_command(
            command,
            project_id=project_id,
            skill=skill,
            script=script,
            timeout=timeout,
            background=True,
        )
        if isinstance(prepared, RunCommandResult):
            await self._record_run_command_failure(
                prepared,
                rule_name=rule_name,
                rule_id=rule_id,
                platform_session_id=platform_session_id,
            )
            return
        timeout -= time.perf_counter() - started
        if timeout <= 0:
            result = RunCommandResult.deadline_exhausted(
                timeout_seconds=max(timeout, 0.0),
                background=True,
                skill=skill,
                script=script,
            )
            await self._record_run_command_failure(
                result,
                rule_name=rule_name,
                rule_id=rule_id,
                platform_session_id=platform_session_id,
            )
            return
        result = await self._execute_run_command(
            prepared,
            cwd,
            stdin_payload,
            timeout,
            skill=skill,
            script=script,
            rule_name=rule_name,
            rule_id=rule_id,
            platform_session_id=platform_session_id,
            background=True,
        )
        if not result.context or not platform_session_id:
            return
        try:
            from gobby.storage.inter_session_messages import InterSessionMessageManager

            manager = InterSessionMessageManager(self.db)
            await offload(
                manager.create_message,
                from_session=platform_session_id,
                to_session=platform_session_id,
                content=result.context,
                message_type="command_result",
            )
        except Exception:
            logger.warning("run_command[%s]: background delivery failed", rule_name, exc_info=True)

    async def _record_run_command_failure(
        self,
        result: RunCommandResult,
        *,
        rule_name: str,
        rule_id: str,
        platform_session_id: str | None,
    ) -> None:
        await self._audit_run_command(
            result,
            rule_name=rule_name,
            rule_id=rule_id,
            platform_session_id=platform_session_id,
        )
        logger.warning(
            "run_command[%s]: %s phase=%s skill=%s script=%s "
            "timeout=%.3fs elapsed=%.3fs (fail-open)",
            rule_name,
            result.status,
            result.phase,
            result.skill,
            result.script,
            result.timeout_seconds,
            result.duration_ms / 1000,
        )

    async def _audit_run_command(
        self,
        result: RunCommandResult,
        *,
        rule_name: str,
        rule_id: str,
        platform_session_id: str | None,
    ) -> None:
        if not platform_session_id or getattr(self, "db", None) is None:
            return
        try:
            from gobby.storage.workflow_audit import WorkflowAuditManager

            manager = WorkflowAuditManager(self.db)
            await offload(
                manager.log,
                session_id=platform_session_id,
                step=rule_name,
                event_type="effect",
                result=result.status,
                rule_id=rule_id,
                context={
                    "phase": result.phase,
                    "skill": result.skill,
                    "script": result.script,
                    "duration_ms": result.duration_ms,
                    "exit_code": result.exit_code,
                    "stdout_bytes": result.stdout_bytes,
                    "stderr_bytes": result.stderr_bytes,
                    "timeout_seconds": result.timeout_seconds,
                    "overflow_stream": result.overflow_stream,
                    "background": result.background,
                },
            )
        except Exception:
            logger.warning("run_command[%s]: audit write failed", rule_name, exc_info=True)

    def _background_run_command_registry(
        self,
    ) -> dict[tuple[str, str], asyncio.Task[None]]:
        return self._background_run_commands
