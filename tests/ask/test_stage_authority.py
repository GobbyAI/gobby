"""Trusted executor authority for native Ask pipeline stages."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

import pytest

from gobby.ask.errors import AskPermissionDenied
from gobby.ask.permissions import AskAgentStage
from gobby.ask.stage_authority import (
    ask_stage_tool_denial_reason,
    pipeline_stage_authority,
    require_ask_stage_authority,
    stage_tool_is_discoverable,
)
from gobby.workflows.pipeline_models import MCPStepConfig, PipelineDefinition, PipelineStep
from gobby.workflows.pipeline_state import (
    ExecutionStatus,
    PipelineExecution,
    StepExecution,
    StepStatus,
)

RUN_ID = "ask-run"
PROJECT_ID = "project-id"
STEP_ID = "validate_initial"


@dataclass
class _Manager:
    execution: PipelineExecution
    steps: list[StepExecution]

    def get_execution(self, execution_id: str) -> PipelineExecution | None:
        return self.execution if self.execution.id == execution_id else None

    def get_steps_for_execution(self, execution_id: str) -> list[StepExecution]:
        return list(self.steps) if self.execution.id == execution_id else []


def _case() -> tuple[_Manager, dict[str, Any], dict[str, Any]]:
    now = datetime.now(UTC)
    arguments = {
        "run_id": RUN_ID,
        "project_id": PROJECT_ID,
        "stage": "investigator",
        "attempt": 0,
    }
    definition = PipelineDefinition(
        name="native-ask",
        steps=[
            PipelineStep(
                id=STEP_ID,
                mcp=MCPStepConfig(
                    server="gobby-ask",
                    tool="validate",
                    arguments={
                        "run_id": "${{ inputs.run_id }}",
                        "project_id": "${{ inputs.project_id }}",
                        "stage": "investigator",
                        "attempt": 0,
                    },
                ),
            )
        ],
    )
    inputs = {
        "run_id": RUN_ID,
        "project_id": PROJECT_ID,
        "ask": {"execution_context": {"run_id": RUN_ID, "project_id": PROJECT_ID}},
    }
    execution = PipelineExecution(
        id=RUN_ID,
        pipeline_name="native-ask",
        project_id=PROJECT_ID,
        status=ExecutionStatus.RUNNING,
        created_at=now,
        updated_at=now,
        inputs_json=json.dumps(inputs),
        definition_json=definition.model_dump_json(),
    )
    step = StepExecution(
        id=7,
        execution_id=RUN_ID,
        step_id=STEP_ID,
        status=StepStatus.RUNNING,
    )
    manager = _Manager(execution, [step])
    context = {
        "project_id": PROJECT_ID,
        "_pipeline_execution_manager": manager,
        "_pipeline_execution_id": RUN_ID,
        "_pipeline_name": "native-ask",
        "_pipeline_step_execution_id": step.id,
    }
    return manager, context, arguments


def _require(
    manager: _Manager,
    arguments: dict[str, Any],
    *,
    operation: str = "validate",
    consume: bool = False,
) -> None:
    require_ask_stage_authority(
        manager,
        operation,
        run_id=str(arguments["run_id"]),
        project_id=str(arguments["project_id"]),
        stage=AskAgentStage(str(arguments["stage"])),
        attempt=int(arguments["attempt"]),
        consume=consume,
    )


@pytest.mark.asyncio
async def test_proxy_check_accepts_exact_persisted_executor_step() -> None:
    manager, context, arguments = _case()

    with pipeline_stage_authority(
        context,
        step_id=STEP_ID,
        operation="validate",
        arguments=arguments,
    ):
        assert ask_stage_tool_denial_reason("gobby-ask", "validate", arguments) is None
        assert stage_tool_is_discoverable("gobby-ask", "validate")
        _require(manager, arguments)


@pytest.mark.parametrize(
    "mismatch",
    ["run", "project", "step", "stage", "attempt", "operation"],
)
@pytest.mark.asyncio
async def test_authority_rejects_identity_mismatches(mismatch: str) -> None:
    manager, context, arguments = _case()
    bound_arguments = dict(arguments)
    operation = "validate"
    if mismatch == "run":
        arguments["run_id"] = "another-run"
    elif mismatch == "project":
        arguments["project_id"] = "another-project"
    elif mismatch == "step":
        context["_pipeline_step_execution_id"] = 999
    elif mismatch == "stage":
        arguments["stage"] = "reviewer"
    elif mismatch == "attempt":
        arguments["attempt"] = 1
    else:
        operation = "spawn"

    with pipeline_stage_authority(
        context,
        step_id=STEP_ID,
        operation="validate",
        arguments=bound_arguments,
    ):
        with pytest.raises(PermissionError, match="owning Ask pipeline"):
            _require(manager, arguments, operation=operation)


@pytest.mark.parametrize("target", ["execution", "step"])
@pytest.mark.asyncio
async def test_authority_rejects_non_running_persisted_state(target: str) -> None:
    manager, context, arguments = _case()
    if target == "execution":
        manager.execution = replace(manager.execution, status=ExecutionStatus.COMPLETED)
    else:
        manager.steps[0] = replace(manager.steps[0], status=StepStatus.COMPLETED)

    with pipeline_stage_authority(
        context,
        step_id=STEP_ID,
        operation="validate",
        arguments=arguments,
    ):
        with pytest.raises(PermissionError, match="not the active|current step is not running"):
            _require(manager, arguments)


@pytest.mark.asyncio
async def test_generic_sibling_pipeline_cannot_impersonate_ask_run() -> None:
    manager, context, arguments = _case()
    manager.execution = replace(
        manager.execution,
        id="sibling-run",
        pipeline_name="generic-run-pipeline",
    )
    manager.steps[0] = replace(manager.steps[0], execution_id="sibling-run")
    context.update(
        {
            "_pipeline_execution_id": "sibling-run",
            "_pipeline_name": "generic-run-pipeline",
        }
    )

    with pipeline_stage_authority(
        context,
        step_id=STEP_ID,
        operation="validate",
        arguments=arguments,
    ):
        with pytest.raises(PermissionError, match="owning Ask pipeline"):
            _require(manager, arguments)


@pytest.mark.asyncio
async def test_background_child_cannot_inherit_reusable_authority() -> None:
    manager, context, arguments = _case()

    async def use_in_child() -> None:
        assert not stage_tool_is_discoverable("gobby-ask", "validate")
        with pytest.raises(PermissionError, match="inherited by another task"):
            _require(manager, arguments, consume=True)

    with pipeline_stage_authority(
        context,
        step_id=STEP_ID,
        operation="validate",
        arguments=arguments,
    ):
        await asyncio.create_task(use_in_child())
        _require(manager, arguments, consume=True)

    denial = ask_stage_tool_denial_reason("gobby-ask", "validate", arguments)
    assert denial is not None
    assert "no executor authority is active" in denial


@pytest.mark.parametrize(
    "ask_value",
    [[], {"execution_context": []}, {"execution_context": "invalid"}],
    ids=["ask-list", "context-list", "context-string"],
)
@pytest.mark.asyncio
async def test_malformed_persisted_ask_context_is_typed_denial(ask_value: object) -> None:
    manager, context, arguments = _case()
    manager.execution = replace(
        manager.execution,
        inputs_json=json.dumps({"run_id": RUN_ID, "project_id": PROJECT_ID, "ask": ask_value}),
    )

    with pipeline_stage_authority(
        context,
        step_id=STEP_ID,
        operation="validate",
        arguments=arguments,
    ):
        with pytest.raises(AskPermissionDenied, match="persisted Ask execution context is invalid"):
            _require(manager, arguments)


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [RuntimeError("boom"), asyncio.CancelledError()])
async def test_authority_resets_after_error_or_cancellation(error: BaseException) -> None:
    _manager, context, arguments = _case()

    with pytest.raises(type(error)):
        with pipeline_stage_authority(
            context,
            step_id=STEP_ID,
            operation="validate",
            arguments=arguments,
        ):
            raise error

    denial = ask_stage_tool_denial_reason("gobby-ask", "validate", arguments)
    assert denial is not None
    assert "no executor authority is active" in denial
