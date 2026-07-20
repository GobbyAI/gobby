"""Regression contracts for structured lifecycle validation verdicts."""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.tasks._lifecycle_validation import validate_leaf_task_with_llm
from gobby.tasks.validation_verdict import ValidationResult as TaskValidationResult

pytestmark = pytest.mark.unit

_TASK_UPDATED_AT = datetime(2026, 1, 1, tzinfo=UTC)

_VALIDATION_FEEDBACK_17821 = (
    "The manifest and diff show a concrete root-cause fix: ensure_personal_project "
    "(src/gobby/storage/projects.py) now materializes .gobby/project.json with the canonical "
    "PERSONAL_PROJECT_ID for the personal workspace (write-if-missing, preserve-valid, "
    "repair-corrupt/wrong-id), which is exactly what daemon wiki routes need to avoid gwiki's "
    "invalid_scope failure for an uninitialized personal dir. This is covered by new focused "
    "tests in tests/storage/test_project_manager.py (identity materialization/preserve/repair) "
    "and a new daemon-route-seam test test_personal_scope_routes_resolve_uninitialized_workspace "
    "in tests/servers/routes/test_wiki_routes.py that provisions a bare gobby home and asserts "
    "/api/wiki/status, /pages, /graph all return ok:true for the personal scope, with a "
    "documented red-first failure against the pre-fix commit. A related second bug (vault-claim "
    "omission causing fallback-dir poisoning across five gwiki commands) was also fixed with a "
    "red-first Rust unit test in commands/health.rs described as covering the gwiki-side "
    "scope-resolution seam, backed by reported clean cargo test/clippy/fmt runs (826 passed). No "
    "frontend files changed per the manifest, consistent with the claim that the UI already "
    "degrades/renders correctly based on backend state; UI verification (headless run, "
    "screenshot, zero offline/degraded banner counts) supports criterion 3 without requiring "
    "source changes. Full raw diff for the five Rust files was omitted for length, but the "
    "manifest confirms the files/line-counts changed and the reported test evidence (specific "
    "failing line, specific passing counts, live e2e checks) is detailed and consistent with the "
    "narrative, so this omission isn't decision-blocking. All stated gates (mypy, ruff "
    "check/format, pytest 51 passed, cargo test/clippy/fmt) are reported clean with no "
    "contradicting or failing results."
)


class _NoBackoffConn:
    """Connection stub where validation backoff and history writes are inert."""

    rowcount = 0

    def execute(self, *_args: object, **_kwargs: object) -> _NoBackoffConn:
        return self

    def fetchone(self) -> None:
        return None


class _NoBackoffDB:
    def fetchone(self, *_args: object, **_kwargs: object) -> None:
        return None

    @contextlib.contextmanager
    def transaction(self) -> Iterator[_NoBackoffConn]:
        yield _NoBackoffConn()


def _task() -> SimpleNamespace:
    return SimpleNamespace(
        id="task-1",
        title="Task",
        description="Description",
        validation_criteria="Tests pass",
        category="code",
        updated_at=_TASK_UPDATED_AT,
    )


def _task_manager_mock() -> SimpleNamespace:
    return SimpleNamespace(
        update_task=MagicMock(),
        increment_validation_failure=MagicMock(return_value=(1, False)),
        db=_NoBackoffDB(),
    )


@pytest.mark.asyncio
async def test_documentation_auto_validation_returns_named_reset_branch() -> None:
    task = _task()
    manager = _task_manager_mock()
    validator = SimpleNamespace(validate_task=AsyncMock())

    result = await validate_leaf_task_with_llm(
        task,
        validator,
        "docs context",
        None,
        SimpleNamespace(task_manager=manager),
        task.id,
        None,
        is_documentation_only=True,
    )

    assert result.can_close is True
    assert result.validation_status == "valid"
    assert result.reset_reason == "documentation_auto_validation"
    validator.validate_task.assert_not_awaited()
    manager.update_task.assert_not_called()
    manager.increment_validation_failure.assert_not_called()


@pytest.mark.parametrize(
    "narrative",
    [
        _VALIDATION_FEEDBACK_17821,
        "The wrapper sets FAILED=1 on failure and exits cleanly when every check passes.",
        "TDD red evidence recorded 3 failed tests before implementation; final green is clean.",
        "The API persists status=failed for failed jobs; all focused validation passes.",
        "Failure-bucket totals are rendered for each historical run; current checks are green.",
    ],
)
@pytest.mark.asyncio
async def test_valid_structured_verdict_ignores_failure_vocabulary(narrative: str) -> None:
    task = _task()
    manager = _task_manager_mock()
    inspection_summary = {"manifest_total": 3, "inspected_count": 2, "uninspected_count": 1}
    validator = SimpleNamespace(
        validate_task=AsyncMock(
            return_value=TaskValidationResult(
                status="valid",
                feedback=narrative,
                blocking_reasons=[],
                inspection_summary=inspection_summary,
            )
        )
    )

    result = await validate_leaf_task_with_llm(
        task,
        validator,
        "diff context",
        None,
        SimpleNamespace(task_manager=manager),
        task.id,
        None,
    )

    assert result.can_close is True
    assert result.validation_status == "valid"
    assert result.validation_feedback == narrative
    assert result.reset_reason == "llm_valid"
    assert result.extra == {"inspection_summary": inspection_summary}
    manager.update_task.assert_not_called()
    manager.increment_validation_failure.assert_not_called()


@pytest.mark.asyncio
async def test_invalid_structured_verdict_is_never_promoted_by_positive_narrative() -> None:
    task = _task()
    manager = _task_manager_mock()
    inspection_summary = {"manifest_total": 3, "inspected_count": 1, "uninspected_count": 2}
    validator = SimpleNamespace(
        validate_task=AsyncMock(
            return_value=TaskValidationResult(
                status="invalid",
                feedback="All validation criteria are satisfied.",
                blocking_reasons=["Required integration evidence is missing."],
                inspection_summary=inspection_summary,
            )
        )
    )

    result = await validate_leaf_task_with_llm(
        task,
        validator,
        "diff context",
        None,
        SimpleNamespace(task_manager=manager),
        task.id,
        None,
    )

    assert result.can_close is False
    assert result.extra["validation_status"] == "invalid"
    assert result.extra["inspection_summary"] == inspection_summary
    assert result.message.startswith("Close blocked: validation verdict 'invalid'")


@pytest.mark.asyncio
async def test_blocked_message_is_identical_across_response_and_persistence() -> None:
    task = _task()
    manager = _task_manager_mock()
    validator = SimpleNamespace(
        validate_task=AsyncMock(
            return_value=TaskValidationResult(
                status="invalid",
                feedback="Validation did not pass.",
                blocking_reasons=["pytest regression still fails", "lint gate failed"],
            )
        )
    )

    with patch(
        "gobby.mcp_proxy.tools.tasks._lifecycle_validation._record_validation_iteration"
    ) as record_iteration:
        result = await validate_leaf_task_with_llm(
            task,
            validator,
            "diff context",
            None,
            SimpleNamespace(task_manager=manager),
            task.id,
            None,
        )

    persisted_feedback = manager.increment_validation_failure.call_args.kwargs[
        "validation_feedback"
    ]
    history_feedback = record_iteration.call_args.kwargs["feedback"]
    assert result.message == persisted_feedback == history_feedback
    assert result.message.count("Close blocked:") == 1
    assert result.message == (
        "Close blocked: validation verdict 'invalid'\n"
        "Blocking reasons: pytest regression still fails; lint gate failed\n\n"
        "Validator feedback:\nValidation did not pass."
    )


@pytest.mark.asyncio
async def test_override_provenance_is_rendered_and_returned_structurally() -> None:
    task = _task()
    manager = _task_manager_mock()
    override: dict[str, object] = {
        "from": "valid",
        "to": "invalid",
        "reason": "current_failure_evidence",
        "evidence": ["pytest: 1 failed"],
    }
    validator = SimpleNamespace(
        validate_task=AsyncMock(
            return_value=TaskValidationResult(
                status="invalid",
                feedback="The implementation otherwise satisfies the criteria.",
                blocking_reasons=["pytest: 1 failed"],
                verdict_override=override,
            )
        )
    )

    result = await validate_leaf_task_with_llm(
        task,
        validator,
        "diff context",
        None,
        SimpleNamespace(task_manager=manager),
        task.id,
        None,
    )

    assert result.extra["verdict_override"] == override
    assert result.message.startswith(
        "Close blocked: validation verdict 'invalid' — verdict overridden: validator attested "
        "current failures: pytest: 1 failed"
    )
