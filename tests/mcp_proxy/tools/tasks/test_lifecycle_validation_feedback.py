"""Feedback and accounting contracts for bounded criteria review."""

from __future__ import annotations

import json
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
from gobby.tasks.close_verdict import CloseCriterionVerdict, CloseVerdict
from gobby.tasks.close_verdict_memo import CloseVerdictMemo, TaskCloseVerdictMemo
from gobby.tasks.criteria_contract import split_validation_criteria
from gobby.tasks.validation import TaskValidator, ValidationPromptTooLarge
from gobby.tasks.validation_history import ValidationHistoryManager

pytestmark = pytest.mark.integration

_SESSION_ID = "00000000-0000-4000-8000-0000000009f1"


class _Validator:
    def __init__(self, outcome: CloseVerdict | Exception) -> None:
        self.outcome = outcome
        self.calls = 0
        self.last_kwargs: dict[str, object] = {}

    async def validate_task(self, **kwargs: object) -> CloseVerdict:
        self.calls += 1
        self.last_kwargs = kwargs
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def _task(manager: LocalTaskManager, project_id: str) -> Task:
    return manager.create_task(
        project_id=project_id,
        title="Feedback leaf",
        category="docs",
        validation_criteria="The guide documents the checklist.",
    )


async def _evaluate(
    task: Task,
    manager: LocalTaskManager,
    validator: _Validator | TaskValidator,
    reason: str = "completed",
    verdict_memo: CloseVerdictMemo | None = None,
    checklist_facts: dict[str, object] | None = None,
) -> Any:
    ctx = cast(RegistryContext, SimpleNamespace(task_manager=manager))
    return await evaluate_criteria_review(
        task=task,
        task_validator=cast(TaskValidator, validator),
        ctx=ctx,
        resolved_id=task.id,
        changes_summary="Documented the checklist.",
        diff_text="diff --git a/docs/guide.md b/docs/guide.md",
        checklist_facts=checklist_facts or {"validation_commands": "skipped:category"},
        validation_config=None,
        reason=reason,
        verdict_memo=verdict_memo,
    )


def _render_context(
    _path: str,
    context: dict[str, Any] | None = None,
    strict: bool = False,
) -> str:
    del strict
    return json.dumps(context or {}, sort_keys=True, default=str)


def _memo_validator(temp_db: HubDatabase) -> tuple[TaskValidator, AsyncMock]:
    """A real validator over a rendered prompt, with only the provider faked."""
    llm_service = MagicMock(spec=LLMService)
    llm_service.call_json_feature = AsyncMock(
        return_value={
            "status": "valid",
            "criteria": [{"index": 1, "satisfied": True, "gap": None}],
            "feedback": "Documentation criterion is satisfied.",
        }
    )
    validator = TaskValidator(TaskValidationConfig(), llm_service, db=temp_db)
    # The bundled prompt template is not seeded into the test hub, and the
    # memo keys on the rendered prompt, so render the context itself.
    cast(Any, validator._loader).render = _render_context
    return validator, llm_service.call_json_feature


@pytest.mark.asyncio
async def test_valid_verdict_preserves_feedback_without_mutating_task(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    manager = LocalTaskManager(temp_db)
    task = _task(manager, sample_project["id"])
    validator = _Validator(
        CloseVerdict(
            status="valid",
            criteria=(
                CloseCriterionVerdict(
                    1,
                    "The guide documents the checklist.",
                    True,
                    None,
                ),
            ),
            feedback="Documentation criterion is satisfied.",
        )
    )

    result = await _evaluate(task, manager, validator)

    assert result.can_close is True
    assert result.validation_feedback == "Documentation criterion is satisfied."
    assert result.reset_reason == "llm_valid"
    assert validator.calls == 1
    assert validator.last_kwargs["closure_reason"] == "completed"
    refreshed = manager.get_task(task.id)
    assert refreshed is not None and refreshed.validation_status == task.validation_status


@pytest.mark.asyncio
async def test_no_work_reason_threads_through_to_the_validator(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    manager = LocalTaskManager(temp_db)
    task = _task(manager, sample_project["id"])
    validator = _Validator(
        CloseVerdict(
            status="valid",
            criteria=(
                CloseCriterionVerdict(
                    1,
                    "The guide documents the checklist.",
                    True,
                    None,
                ),
            ),
            feedback="Obsolescence justification is coherent.",
        )
    )

    result = await _evaluate(task, manager, validator, reason="obsolete")

    assert result.can_close is True
    assert validator.last_kwargs["closure_reason"] == "obsolete"


@pytest.mark.asyncio
async def test_invalid_verdict_returns_first_gap_and_increments_once(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    manager = LocalTaskManager(temp_db)
    task = _task(manager, sample_project["id"])
    validator = _Validator(
        CloseVerdict(
            status="invalid",
            criteria=(
                CloseCriterionVerdict(
                    1,
                    "The guide documents the checklist.",
                    False,
                    "Add the category matrix to the guide.",
                ),
            ),
            feedback="One criterion remains incomplete.",
        )
    )

    result = await _evaluate(task, manager, validator)

    assert result.can_close is False
    assert result.extra["blocking_reasons"] == ["Add the category matrix to the guide."]
    assert result.extra["validation_fail_count"] == 1
    refreshed = manager.get_task(task.id)
    assert refreshed is not None and refreshed.validation_fail_count == 1
    history = ValidationHistoryManager(temp_db).get_iteration_history(task.id)
    assert [(item.iteration, item.status) for item in history] == [(1, "invalid")]


@pytest.mark.asyncio
async def test_unchanged_evidence_reuses_the_persisted_verdict(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    manager = LocalTaskManager(temp_db)
    task = _task(manager, sample_project["id"])
    validator, call_json_feature = _memo_validator(temp_db)
    memo = TaskCloseVerdictMemo(
        TaskCloseReviewStore(temp_db),
        task_id=task.id,
        task_ref=f"#{task.seq_num}",
        caller_session_id=_SESSION_ID,
        close_arguments={"reason": "completed"},
        criteria=split_validation_criteria(task.validation_criteria or ""),
    )

    first = await _evaluate(task, manager, validator, verdict_memo=memo)
    refreshed = manager.get_task(task.id)
    assert refreshed is not None
    second = await _evaluate(refreshed, manager, validator, verdict_memo=memo)

    assert first.can_close is True
    assert second.can_close is True
    assert second.extra["verdict"] == first.extra["verdict"]
    assert call_json_feature.await_count == 1


@pytest.mark.asyncio
async def test_new_commit_evidence_invalidates_the_persisted_verdict(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    manager = LocalTaskManager(temp_db)
    task = _task(manager, sample_project["id"])
    validator, call_json_feature = _memo_validator(temp_db)
    memo = TaskCloseVerdictMemo(
        TaskCloseReviewStore(temp_db),
        task_id=task.id,
        task_ref=f"#{task.seq_num}",
        caller_session_id=_SESSION_ID,
        close_arguments={"reason": "completed"},
        criteria=split_validation_criteria(task.validation_criteria or ""),
    )

    await _evaluate(task, manager, validator, verdict_memo=memo)
    refreshed = manager.get_task(task.id)
    assert refreshed is not None
    await _evaluate(
        refreshed,
        manager,
        validator,
        verdict_memo=memo,
        checklist_facts={"validation_commands": "skipped:category", "commit_shas": ["deadbee"]},
    )

    assert call_json_feature.await_count == 2


@pytest.mark.asyncio
async def test_prompt_too_large_is_actionable_without_failure_accounting(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    manager = LocalTaskManager(temp_db)
    task = _task(manager, sample_project["id"])
    message = (
        "Task-close criteria-review prompt is 32001 characters, exceeding the configured limit "
        "of 32000 characters at gobby-tasks.validation.close_review_prompt_max_chars. Split the "
        "task into smaller tasks and preserve every validation criterion."
    )
    validator = _Validator(ValidationPromptTooLarge(message))

    result = await _evaluate(task, manager, validator)

    assert result.error_type == "validation_prompt_too_large"
    assert result.message == message
    refreshed = manager.get_task(task.id)
    assert refreshed is not None and refreshed.validation_fail_count == 0
    assert ValidationHistoryManager(temp_db).get_iteration_history(task.id) == []
