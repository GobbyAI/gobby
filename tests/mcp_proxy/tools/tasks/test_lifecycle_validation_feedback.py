"""Feedback and accounting contracts for bounded criteria review."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._lifecycle_validation import evaluate_criteria_review
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.tasks.close_verdict import CloseCriterionVerdict, CloseVerdict
from gobby.tasks.validation import TaskValidator, ValidationPromptTooLarge
from gobby.tasks.validation_history import ValidationHistoryManager

pytestmark = pytest.mark.integration


class _Validator:
    def __init__(self, outcome: CloseVerdict | Exception) -> None:
        self.outcome = outcome
        self.calls = 0

    async def validate_task(self, **_kwargs: object) -> CloseVerdict:
        self.calls += 1
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
    validator: _Validator,
) -> Any:
    ctx = cast(RegistryContext, SimpleNamespace(task_manager=manager))
    return await evaluate_criteria_review(
        task=task,
        task_validator=cast(TaskValidator, validator),
        ctx=ctx,
        resolved_id=task.id,
        changes_summary="Documented the checklist.",
        diff_text="diff --git a/docs/guide.md b/docs/guide.md",
        checklist_facts={"validation_commands": "skipped:category"},
        validation_config=None,
    )


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
    refreshed = manager.get_task(task.id)
    assert refreshed is not None and refreshed.validation_status == task.validation_status


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
