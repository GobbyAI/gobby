"""Each pipeline step's deterministic invocation id in its context (deploy-runbook 7.2)."""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import MagicMock

import pytest

from gobby.workflows.definitions import PipelineDefinition, PipelineStep
from gobby.workflows.pipeline_executor import PipelineExecutor

pytestmark = pytest.mark.unit

EXECUTION_ID = "pe-test-123"


def _expected(step_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"gobby-pipeline:{EXECUTION_ID}:{step_id}"))


async def _seen_ids(
    monkeypatch: pytest.MonkeyPatch,
    executor: PipelineExecutor,
    *,
    execution_id: str | None,
) -> dict[str, tuple[Any, Any]]:
    """Run the pipeline; return each executed step's context and rendered invocation id."""
    pipeline = PipelineDefinition(
        name="runbook",
        steps=[
            PipelineStep(id="s1", exec="true"),
            PipelineStep(id="invocation_id", exec="true"),
            PipelineStep(
                id="s2",
                exec="true",
                condition=f"${{{{ invocation_id == '{_expected('s2')}' }}}}",
            ),
        ],
    )
    seen: dict[str, tuple[Any, Any]] = {}

    async def capture(step: PipelineStep, context: dict[str, Any], project_id: str) -> Any:
        rendered = executor.renderer.build_render_context(context).get("invocation_id")
        seen[step.id] = (context.get("invocation_id"), rendered)
        return {"ok": True}

    monkeypatch.setattr(executor, "_execute_step", capture)
    await executor.execute(
        pipeline=pipeline, inputs={}, project_id="proj-123", execution_id=execution_id
    )
    return seen


async def test_invocation_id_is_stable_per_step(
    monkeypatch: pytest.MonkeyPatch,
    mock_db: MagicMock,
    mock_execution_manager: MagicMock,
    mock_llm_service: MagicMock,
) -> None:
    mock_db.fetchone.return_value = None
    mock_execution_manager.get_steps_for_execution.return_value = []

    def executor() -> PipelineExecutor:
        return PipelineExecutor(
            db=mock_db, execution_manager=mock_execution_manager, llm_service=mock_llm_service
        )

    first = await _seen_ids(monkeypatch, executor(), execution_id=None)
    restarted = await _seen_ids(monkeypatch, executor(), execution_id=EXECUTION_ID)

    expected = {step: (_expected(step), _expected(step)) for step in ("s1", "invocation_id", "s2")}
    assert first == expected
    assert restarted == expected
    assert _expected("s1") != _expected("s2")
