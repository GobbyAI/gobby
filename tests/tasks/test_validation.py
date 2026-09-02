"""Continuity contracts for task-close criteria review."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.config.tasks import TaskValidationConfig
from gobby.llm import LLMService
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._lifecycle_validation import evaluate_criteria_review
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.task_close_reviews import TaskCloseReviewStore
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.tasks.close_verdict_memo import TaskCloseVerdictMemo
from gobby.tasks.criteria_contract import split_validation_criteria
from gobby.tasks.generation_schemas import TASK_CLOSE_VALIDATION_SCHEMA
from gobby.tasks.validation import TaskValidator

pytestmark = pytest.mark.integration

_SESSION_ID = "00000000-0000-4000-8000-000000002145"
_CRITERION = "The close response reports complete evidence requirements."
_GAP = "Exercise the close adapter instead of only the review engine."
_REQUIRED_EVIDENCE = (
    "Invoke close_task through the real rule and adapter, then capture the MCP response receipt."
)
_TEMPLATE_PATH = (
    Path(__file__).resolve().parents[2] / "src/gobby/install/shared/prompts/validation/validate.md"
)


def test_prompt_and_generation_schema_define_optional_required_evidence() -> None:
    template = _TEMPLATE_PATH.read_text(encoding="utf-8")
    assert "must state the complete evidence set" in template
    assert "Requirements already stated in the prior review" in template
    assert "Do not add an implementation or evidence requirement absent from" in template

    criteria_schema = TASK_CLOSE_VALIDATION_SCHEMA["properties"]["criteria"]["items"]
    assert criteria_schema["properties"]["required_evidence"] == {"type": ["string", "null"]}
    assert "required_evidence" not in criteria_schema["required"]


def _render_context(
    _path: str,
    context: dict[str, Any] | None = None,
    strict: bool = False,
) -> str:
    del strict
    return json.dumps(context or {}, sort_keys=True, default=str)


def _validator(temp_db: HubDatabase) -> tuple[TaskValidator, AsyncMock]:
    llm_service = MagicMock(spec=LLMService)
    call_json_feature = AsyncMock(
        side_effect=[
            {
                "status": "invalid",
                "criteria": [
                    {
                        "index": 1,
                        "satisfied": False,
                        "gap": _GAP,
                        "required_evidence": _REQUIRED_EVIDENCE,
                    }
                ],
                "feedback": "The criterion needs stronger evidence.",
            },
            {
                "status": "invalid",
                "criteria": [
                    {
                        "index": 1,
                        "satisfied": False,
                        "gap": _GAP,
                        "required_evidence": _REQUIRED_EVIDENCE,
                    }
                ],
                "feedback": "The criterion still needs stronger evidence.",
            },
        ]
    )
    llm_service.call_json_feature = call_json_feature
    validator = TaskValidator(TaskValidationConfig(), llm_service, db=temp_db)
    cast(Any, validator._loader).render = _render_context
    return validator, call_json_feature


async def _evaluate(
    *,
    task: Task,
    manager: LocalTaskManager,
    validator: TaskValidator,
    memo: TaskCloseVerdictMemo,
    commit_sha: str,
) -> Any:
    ctx = cast(RegistryContext, SimpleNamespace(task_manager=manager))
    return await evaluate_criteria_review(
        task=task,
        task_validator=validator,
        ctx=ctx,
        resolved_id=task.id,
        changes_summary="Added complete close-review evidence requirements.",
        diff_text=f"diff --git a/src/review.py b/src/review.py\n+commit = '{commit_sha}'",
        checklist_facts={"validation_commands": "passed", "commit_shas": [commit_sha]},
        validation_config=None,
        reason="completed",
        verdict_memo=memo,
    )


async def test_rejection_evidence_reaches_response_and_next_review(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        project_id=sample_project["id"],
        title="Preserve close-review requirements",
        category="code",
        implementation_domain="backend",
        validation_criteria=_CRITERION,
    )
    validator, call_json_feature = _validator(temp_db)
    memo = TaskCloseVerdictMemo(
        TaskCloseReviewStore(temp_db),
        task_id=task.id,
        task_ref=f"#{task.seq_num}",
        caller_session_id=_SESSION_ID,
        close_arguments={"reason": "completed"},
        criteria=split_validation_criteria(task.validation_criteria or ""),
    )

    await _evaluate(
        task=task,
        manager=manager,
        validator=validator,
        memo=memo,
        commit_sha="first",
    )
    refreshed = manager.get_task(task.id)
    assert refreshed is not None
    result = await _evaluate(
        task=refreshed,
        manager=manager,
        validator=validator,
        memo=memo,
        commit_sha="second",
    )

    second_prompt = json.loads(call_json_feature.await_args_list[1].args[1])
    prior_requirements = second_prompt["prior_requirements"]
    assert "Criterion 1" in prior_requirements
    assert f"Gap: {_GAP}" in prior_requirements
    assert f"Required evidence: {_REQUIRED_EVIDENCE}" in prior_requirements
    complete_requirement = f"{_GAP} Required evidence: {_REQUIRED_EVIDENCE}"
    assert result.extra["blocking_reasons"] == [complete_requirement]
    assert result.extra["required_actions"] == [complete_requirement]
    assert result.extra["verdict"]["criteria"][0]["required_evidence"] == _REQUIRED_EVIDENCE

    refreshed = manager.get_task(task.id)
    assert refreshed is not None
    await _evaluate(
        task=refreshed,
        manager=manager,
        validator=validator,
        memo=memo,
        commit_sha="second",
    )
    assert call_json_feature.await_count == 2
