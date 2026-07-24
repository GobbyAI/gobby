"""Structured issue persistence and recurring validation candidate tests."""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pytest_mock import MockerFixture

from gobby.config.tasks import TaskValidationConfig
from gobby.llm import LLMService
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._lifecycle_validation import validate_leaf_task_with_llm
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager
from gobby.tasks.validation import TaskValidator
from gobby.tasks.validation_history import ValidationHistoryManager
from gobby.tasks.validation_models import Issue, IssueSeverity, IssueType
from gobby.tasks.validation_verdict import (
    ValidationResult,
    _validation_result_from_data,
)

pytestmark = pytest.mark.integration

_ANCHOR = "src/gobby/tasks/validation.py:TaskValidator.validate_task"


class _StubValidator:
    """Return one preconfigured validation verdict."""

    def __init__(self, result: ValidationResult) -> None:
        self.result = result

    async def validate_task(self, **_kwargs: Any) -> ValidationResult:
        return self.result


def _make_task(manager: LocalTaskManager, project_id: str, title: str) -> Any:
    return manager.create_task(
        project_id=project_id,
        title=title,
        category="code",
        validation_criteria="Focused validation passes",
    )


def _issue(*, location: str | None = _ANCHOR, title: str = "Focused test fails") -> Issue:
    return Issue(
        issue_type=IssueType.TEST_FAILURE,
        severity=IssueSeverity.MAJOR,
        title=title,
        location=location,
    )


async def _successful_validation(
    manager: LocalTaskManager,
    task: Any,
    config: TaskValidationConfig,
) -> list[dict[str, Any]]:
    result = await validate_leaf_task_with_llm(
        task,
        cast(
            TaskValidator,
            _StubValidator(ValidationResult(status="valid", feedback="Focused validation passes")),
        ),
        "structured evidence",
        cast(RegistryContext, SimpleNamespace(task_manager=manager)),
        task.id,
        config,
    )
    assert result.can_close is True
    assert result.extra is not None
    return cast(list[dict[str, Any]], result.extra["recurring_validation_candidates"])


@pytest.mark.asyncio
async def test_issue_persistence_real_path(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    mocker: MockerFixture,
) -> None:
    manager = LocalTaskManager(temp_db)
    task = _make_task(manager, sample_project["id"], "Issue persistence")
    config = TaskValidationConfig(enabled=True, candidates=["claude/test-model"])
    llm = mocker.MagicMock(spec=LLMService)
    llm.call_json_feature = mocker.AsyncMock(
        return_value={
            "status": "invalid",
            "feedback": "Focused test still fails",
            "blocking_reasons": ["Focused test is failing"],
            "current_failure_evidence": ["pytest reports one failure"],
            "issues": [
                {
                    "title": "Focused test fails",
                    "type": "test_failure",
                    "severity": "major",
                    "location": _ANCHOR,
                }
            ],
        }
    )
    validator = TaskValidator(config, llm, db=temp_db)

    result = await validate_leaf_task_with_llm(
        task,
        validator,
        "structured evidence",
        cast(RegistryContext, SimpleNamespace(task_manager=manager)),
        task.id,
        config,
    )

    assert result.can_close is False
    history = ValidationHistoryManager(temp_db).get_iteration_history(task.id)
    assert len(history) == 1
    assert history[0].issues == [_issue()]


@pytest.mark.asyncio
async def test_configured_recurrence_thresholds(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    manager = LocalTaskManager(temp_db)
    history = ValidationHistoryManager(temp_db)

    below_default = _make_task(manager, sample_project["id"], "Below default threshold")
    for iteration in range(1, 3):
        history.record_iteration(
            below_default.id,
            iteration,
            "invalid",
            issues=[_issue(title=f"Focused test fail {iteration}")],
        )
    default_config = TaskValidationConfig(enabled=True, candidates=["claude/test-model"])
    assert await _successful_validation(manager, below_default, default_config) == []

    at_default = _make_task(manager, sample_project["id"], "At default threshold")
    for iteration in range(1, 4):
        history.record_iteration(
            at_default.id,
            iteration,
            "invalid",
            issues=[_issue(title=f"Focused test fail {iteration}")],
        )
    default_candidates = await _successful_validation(manager, at_default, default_config)
    assert len(default_candidates) == 1
    assert default_candidates[0]["distinct_iteration_count"] == 3
    assert default_candidates[0]["failed_iterations"] == [1, 2, 3]
    assert default_candidates[0]["anchors"] == [_ANCHOR]
    assert default_candidates[0]["passing_iteration"] == {
        "iteration": 4,
        "status": "valid",
        "feedback": "Focused validation passes",
    }

    custom = _make_task(manager, sample_project["id"], "Custom threshold")
    for iteration in range(1, 3):
        history.record_iteration(custom.id, iteration, "invalid", issues=[_issue()])
    custom_config = TaskValidationConfig(
        enabled=True,
        candidates=["claude/test-model"],
        recurring_issue_threshold=2,
    )
    custom_candidates = await _successful_validation(manager, custom, custom_config)
    assert len(custom_candidates) == 1
    assert custom_candidates[0]["distinct_iteration_count"] == 2

    duplicates = _make_task(manager, sample_project["id"], "Within-iteration duplicates")
    history.record_iteration(
        duplicates.id,
        1,
        "invalid",
        issues=[_issue(), _issue(), _issue()],
    )
    assert await _successful_validation(manager, duplicates, default_config) == []


@pytest.mark.asyncio
async def test_candidate_anchor_and_noise_gates(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    manager = LocalTaskManager(temp_db)
    history = ValidationHistoryManager(temp_db)
    config = TaskValidationConfig(enabled=True, candidates=["claude/test-model"])

    unanchored = _make_task(manager, sample_project["id"], "Unanchored recurrence")
    for iteration in range(1, 4):
        history.record_iteration(
            unanchored.id,
            iteration,
            "invalid",
            issues=[_issue(location=None)],
        )
    assert await _successful_validation(manager, unanchored, config) == []

    for status in ("error", "pending"):
        noisy = _make_task(manager, sample_project["id"], f"{status} noise")
        for iteration in range(1, 4):
            history.record_iteration(noisy.id, iteration, status, issues=[_issue()])
        assert await _successful_validation(manager, noisy, config) == []

    clean = _make_task(manager, sample_project["id"], "Clean validation history")
    assert await _successful_validation(manager, clean, config) == []


def test_issue_wire_schema_defensive_parse(caplog: pytest.LogCaptureFixture) -> None:
    canonical = {
        "status": "invalid",
        "feedback": "Focused test still fails",
        "blocking_reasons": ["Focused test is failing"],
        "current_failure_evidence": ["pytest reports one failure"],
        "issues": [
            {
                "title": "Focused test fails",
                "type": "test_failure",
                "severity": "major",
                "location": _ANCHOR,
            }
        ],
    }
    assert _validation_result_from_data(canonical).issues == [_issue()]

    with caplog.at_level(logging.WARNING, logger="gobby.tasks.validation_verdict"):
        non_list = _validation_result_from_data({**canonical, "issues": {"bad": "shape"}})
        mixed = _validation_result_from_data(
            {
                **canonical,
                "issues": [
                    canonical["issues"][0],
                    "not-an-object",
                    {
                        "title": "Unknown issue",
                        "type": "unknown",
                        "severity": "major",
                        "location": _ANCHOR,
                    },
                    {
                        "type": "lint_error",
                        "severity": "minor",
                        "location": _ANCHOR,
                    },
                ],
            }
        )

    assert non_list.status == "invalid"
    assert non_list.issues == []
    assert mixed.status == "invalid"
    assert mixed.issues == [_issue()]
    assert "non-list issues payload" in caplog.text
    assert caplog.text.count("dropping malformed validation issue") == 3


def test_validation_prompt_structured_issue_contract() -> None:
    prompt = Path("src/gobby/install/shared/prompts/validation/validate.md").read_text()

    assert '"issues"' in prompt
    assert '"type"' in prompt
    for issue_type in (
        "test_failure",
        "lint_error",
        "acceptance_gap",
        "type_error",
        "security",
    ):
        assert issue_type in prompt
    for severity in ("blocker", "major", "minor"):
        assert severity in prompt
