"""Executor-owned authority for native Ask pipeline stage calls."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any, Protocol

from gobby.ask.errors import AskPermissionDenied
from gobby.ask.permissions import ASK_PIPELINE_NAME, AskAgentStage
from gobby.workflows.pipeline_models import PipelineDefinition
from gobby.workflows.pipeline_state import (
    ExecutionStatus,
    PipelineExecution,
    StepExecution,
    StepStatus,
)

ASK_STAGE_TOOLS = frozenset({"prepare", "seed", "spawn", "validate", "admit_repair", "publish"})
_ASK_SERVER = "gobby-ask"
_DENIAL = "Ask internal stage requires authority from the owning Ask pipeline"


class PipelineExecutionReader(Protocol):
    def get_execution(self, execution_id: str) -> PipelineExecution | None: ...

    def get_steps_for_execution(self, execution_id: str) -> list[StepExecution]: ...


@dataclass(slots=True)
class _StageAuthority:
    manager: PipelineExecutionReader
    execution_id: str
    project_id: str
    pipeline_name: str
    step_execution_id: int
    step_id: str
    operation: str
    stage: str | None
    attempt: int | None
    task: asyncio.Task[Any] | None
    consumed: bool = False


_CURRENT_STAGE_AUTHORITY: ContextVar[_StageAuthority | None] = ContextVar(
    "ask_pipeline_stage_authority", default=None
)


def is_ask_stage_tool(server_name: str, tool_name: str) -> bool:
    return server_name == _ASK_SERVER and tool_name in ASK_STAGE_TOOLS


def _argument_stage(arguments: Mapping[str, Any]) -> str | None:
    stage = arguments.get("stage")
    return str(stage) if stage is not None else None


def _argument_attempt(arguments: Mapping[str, Any]) -> int | None:
    attempt = arguments.get("attempt")
    return attempt if isinstance(attempt, int) and not isinstance(attempt, bool) else None


@contextmanager
def pipeline_stage_authority(
    context: Mapping[str, Any],
    *,
    step_id: str,
    operation: str,
    arguments: Mapping[str, Any],
) -> Iterator[None]:
    """Bind one executor step to its exact persisted execution identity."""
    manager = context.get("_pipeline_execution_manager")
    execution_id = context.get("_pipeline_execution_id")
    pipeline_name = context.get("_pipeline_name")
    step_execution_id = context.get("_pipeline_step_execution_id")
    project_id = context.get("project_id")
    if not (
        manager is not None
        and isinstance(execution_id, str)
        and isinstance(project_id, str)
        and isinstance(pipeline_name, str)
        and isinstance(step_execution_id, int)
    ):
        yield
        return
    authority = _StageAuthority(
        manager=manager,
        execution_id=execution_id,
        project_id=project_id,
        pipeline_name=pipeline_name,
        step_execution_id=step_execution_id,
        step_id=step_id,
        operation=operation,
        stage=_argument_stage(arguments),
        attempt=_argument_attempt(arguments),
        task=asyncio.current_task(),
    )
    token: Token[_StageAuthority | None] = _CURRENT_STAGE_AUTHORITY.set(authority)
    try:
        yield
    finally:
        _CURRENT_STAGE_AUTHORITY.reset(token)


def _deny(reason: str) -> AskPermissionDenied:
    return AskPermissionDenied(f"{_DENIAL}: {reason}")


def _persisted_step(
    authority: _StageAuthority,
    execution: PipelineExecution,
) -> None:
    matches = [
        step
        for step in authority.manager.get_steps_for_execution(execution.id)
        if step.id == authority.step_execution_id
    ]
    if len(matches) != 1:
        raise _deny("current step execution is missing")
    step = matches[0]
    if step.execution_id != execution.id or step.step_id != authority.step_id:
        raise _deny("current step identity does not match the executor")
    if step.status is not StepStatus.RUNNING:
        raise _deny("current step is not running")


def _persisted_definition(
    execution: PipelineExecution,
    authority: _StageAuthority,
) -> None:
    try:
        definition = PipelineDefinition.model_validate_json(execution.definition_json or "")
    except ValueError as exc:
        raise _deny("persisted pipeline definition is invalid") from exc
    step = definition.get_step(authority.step_id)
    if step is None or step.mcp is None:
        raise _deny("persisted current step is not an MCP stage")
    if step.mcp.server != _ASK_SERVER or step.mcp.tool != authority.operation:
        raise _deny("persisted current step targets another operation")
    declared = step.mcp.arguments or {}
    if declared.get("stage") != authority.stage or declared.get("attempt") != authority.attempt:
        raise _deny("persisted stage or attempt does not match the call")


def _persisted_ask_context(execution: PipelineExecution) -> None:
    try:
        document = json.loads(execution.inputs_json or "")
    except json.JSONDecodeError as exc:
        raise _deny("persisted Ask execution context is invalid") from exc
    if not isinstance(document, Mapping):
        raise _deny("persisted Ask execution context is invalid")
    ask = document.get("ask")
    if not isinstance(ask, Mapping):
        raise _deny("persisted Ask execution context is invalid")
    context = ask.get("execution_context")
    if not isinstance(context, Mapping):
        raise _deny("persisted Ask execution context is invalid")
    expected = {"run_id": execution.id, "project_id": execution.project_id}
    if any(
        document.get(key) != value or context.get(key) != value for key, value in expected.items()
    ):
        raise _deny("persisted Ask execution context does not match the run")


def require_ask_stage_authority(
    manager: PipelineExecutionReader,
    operation: str,
    *,
    run_id: str,
    project_id: str,
    stage: AskAgentStage | str | None = None,
    attempt: int | None = None,
    consume: bool = True,
) -> None:
    """Validate and optionally consume authority for one internal Ask stage call."""
    authority = _CURRENT_STAGE_AUTHORITY.get()
    if authority is None:
        raise _deny("no executor authority is active")
    if authority.manager is not manager:
        raise _deny("executor storage does not own this Ask service")
    if authority.consumed:
        raise _deny("executor authority was already consumed")
    try:
        current_task = asyncio.current_task()
    except RuntimeError:
        current_task = None
    if current_task is None and consume:
        raise _deny("executor authority cannot be consumed outside its task")
    if current_task is not None and current_task is not authority.task:
        raise _deny("executor authority was inherited by another task")
    expected_stage = str(stage) if stage is not None else None
    if (
        authority.execution_id != run_id
        or authority.project_id != project_id
        or authority.pipeline_name != ASK_PIPELINE_NAME
        or authority.operation != operation
        or authority.stage != expected_stage
        or authority.attempt != attempt
    ):
        raise _deny("operation, run, project, stage, or attempt does not match")
    execution = manager.get_execution(authority.execution_id)
    if execution is None:
        raise _deny("pipeline execution is missing")
    if (
        execution.id != run_id
        or execution.project_id != project_id
        or execution.pipeline_name != ASK_PIPELINE_NAME
        or execution.status is not ExecutionStatus.RUNNING
    ):
        raise _deny("pipeline execution is not the active owning Ask run")
    _persisted_step(authority, execution)
    _persisted_definition(execution, authority)
    _persisted_ask_context(execution)
    if consume:
        authority.consumed = True


def ask_stage_tool_denial_reason(
    server_name: str,
    tool_name: str,
    arguments: Mapping[str, Any],
) -> str | None:
    """Return a denial for an internal stage call outside its owning executor step."""
    if not is_ask_stage_tool(server_name, tool_name):
        return None
    authority = _CURRENT_STAGE_AUTHORITY.get()
    manager = authority.manager if authority is not None else None
    try:
        if manager is None:
            raise _deny("no executor authority is active")
        run_id = arguments.get("run_id")
        project_id = arguments.get("project_id")
        if not isinstance(run_id, str) or not isinstance(project_id, str):
            raise _deny("run or project identity is missing")
        require_ask_stage_authority(
            manager,
            tool_name,
            run_id=run_id,
            project_id=project_id,
            stage=_argument_stage(arguments),
            attempt=_argument_attempt(arguments),
            consume=False,
        )
    except AskPermissionDenied as exc:
        return str(exc)
    return None


def stage_tool_is_discoverable(server_name: str, tool_name: str) -> bool:
    """Expose an internal stage schema only to its currently authorized executor step."""
    if not is_ask_stage_tool(server_name, tool_name):
        return True
    authority = _CURRENT_STAGE_AUTHORITY.get()
    if authority is None:
        return False
    try:
        require_ask_stage_authority(
            authority.manager,
            tool_name,
            run_id=authority.execution_id,
            project_id=authority.project_id,
            stage=authority.stage,
            attempt=authority.attempt,
            consume=False,
        )
    except AskPermissionDenied:
        return False
    return True
