"""Detached task-close review gate routing tests."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock
from uuid import UUID

import pytest

import gobby.mcp_proxy.tools.tasks._lifecycle_review_gate as review_gate
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._lifecycle_review_gate import (
    SubmittedCloseReview,
    evaluate_close_criteria,
)
from gobby.mcp_proxy.tools.tasks._lifecycle_validation import ValidationResult
from gobby.storage.tasks import Task
from gobby.tasks.criteria_contract import split_validation_criteria
from gobby.tasks.validation import PreparedCloseReview, TaskValidator

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_ordinary_review_prepares_without_a_one_shot_provider_call() -> None:
    validation_commands = {"latest_outcomes": {"test": "success"}, "latest_runs": []}

    result = await asyncio.wait_for(
        _evaluate(checklist_facts={"validation_commands": validation_commands}),
        timeout=0.05,
    )

    assert result.error_type == "close_review_required"
    assert result.extra["review_fingerprint"] == "close"
    assert result.extra["deterministic_evidence_fingerprint"] == "evidence"
    # Gate 10's record is forwarded for the reviewer launch prompt.
    assert result.extra["validation_commands"] == validation_commands
    assert "spawn_request" not in result.extra
    assert "review_run_id" not in result.extra


@pytest.mark.asyncio
async def test_detached_review_reports_the_normalized_criterion_count() -> None:
    result = await _evaluate(criteria="- A top\n  - A nested one\n  - A nested two\n- B top")

    assert result.error_type == "close_review_required"
    assert result.extra["criterion_count"] == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["valid", "invalid"])
async def test_matching_submitted_verdict_uses_shared_accounting(
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    accounted = ValidationResult(can_close=status == "valid")
    account = MagicMock(return_value=accounted)
    monkeypatch.setattr(review_gate, "account_criteria_verdict", account)

    result = await _evaluate(
        submitted=SubmittedCloseReview(
            verdict={
                "status": status,
                "criteria": [{"index": 1, "satisfied": status == "valid", "gap": None}],
                "feedback": status,
            },
            review_fingerprint="close",
            evidence_fingerprint="evidence",
            diff_sha="diff",
            test_bodies_sha="tests",
            stable_facts={},
        )
    )

    assert result is accounted
    account.assert_called_once()
    assert account.call_args.kwargs["verdict"].status == status
    assert result.extra["deterministic_evidence_fingerprint"] == "evidence"


@pytest.mark.asyncio
async def test_submitted_invalid_verdict_preserves_required_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accounted = ValidationResult(can_close=False, error_type="validation_failed")
    account = MagicMock(return_value=accounted)
    monkeypatch.setattr(review_gate, "account_criteria_verdict", account)

    await _evaluate(
        submitted=SubmittedCloseReview(
            verdict={
                "status": "invalid",
                "criteria": [
                    {
                        "index": 1,
                        "satisfied": False,
                        "gap": "The real close path was not exercised.",
                        "required_evidence": (
                            "Run the real close adapter and capture its MCP response receipt."
                        ),
                    }
                ],
                "feedback": "The close evidence is incomplete.",
            },
            review_fingerprint="close",
            evidence_fingerprint="evidence",
            diff_sha="diff",
            test_bodies_sha="tests",
            stable_facts={},
        )
    )

    parsed = account.call_args.kwargs["verdict"]
    assert parsed.criteria[0].required_evidence == (
        "Run the real close adapter and capture its MCP response receipt."
    )


@pytest.mark.asyncio
async def test_stale_submitted_fingerprint_skips_accounting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = MagicMock()
    monkeypatch.setattr(review_gate, "account_criteria_verdict", account)

    result = await _evaluate(
        submitted=SubmittedCloseReview(
            verdict={"status": "valid", "criteria": [], "feedback": "ok"},
            review_fingerprint="stale",
            evidence_fingerprint="evidence",
        )
    )

    assert result.error_type == "agentic_review_stale"
    account.assert_not_called()


@pytest.mark.asyncio
async def test_malformed_submitted_verdict_can_be_corrected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = MagicMock()
    monkeypatch.setattr(review_gate, "account_criteria_verdict", account)

    result = await _evaluate(
        submitted=SubmittedCloseReview(
            verdict={"status": "unknown", "criteria": [], "feedback": "invalid status"},
            review_fingerprint="close",
            evidence_fingerprint="evidence",
            diff_sha="diff",
            test_bodies_sha="tests",
            stable_facts={},
        )
    )

    assert result.error_type == "agentic_review_malformed"
    account.assert_not_called()


@pytest.mark.asyncio
async def test_operational_criteria_block_before_inline_or_submitted_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepare = MagicMock(return_value=_prepared())
    account = MagicMock()
    monkeypatch.setattr(review_gate, "account_criteria_verdict", account)

    result = await _evaluate(
        criteria="Install the release, restart the daemon, and run a smoke check.",
        changes_summary="Implementation and tests are complete.",
        submitted=SubmittedCloseReview(
            verdict={"status": "valid", "criteria": [], "feedback": "ok"},
            review_fingerprint="close",
            evidence_fingerprint="evidence",
        ),
        prepare=prepare,
    )

    assert result.error_type == "operational_evidence_missing"
    assert result.extra["missing_operational_actions"] == ["install", "restart", "smoke"]
    hint = result.extra["operational_evidence_hint"]
    assert "completion verb 'installed' with subject 'release'" in hint
    assert "completion verb 'restarted' with subject 'daemon'" in hint
    assert "completion verb 'passed' with subject 'smoke test'" in hint
    prepare.assert_not_called()
    account.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("changes_summary", "checklist_facts"),
    [
        ("Release installed; daemon restart completed; smoke check passed.", {}),
        (
            "Implementation complete.",
            {
                "transcript_operational_actions": [
                    "install:release",
                    "restart:daemon",
                    "smoke",
                ]
            },
        ),
    ],
)
async def test_operational_criteria_reach_review_with_completion_evidence(
    changes_summary: str,
    checklist_facts: dict[str, object],
) -> None:
    prepare = MagicMock(return_value=_prepared())

    result = await _evaluate(
        criteria="Install the release, restart the daemon, and run a smoke check.",
        changes_summary=changes_summary,
        checklist_facts=checklist_facts,
        prepare=prepare,
    )

    assert result.error_type == "close_review_required"
    prepare.assert_called_once()


@pytest.mark.asyncio
async def test_spawned_agent_skips_live_operational_evidence() -> None:
    result = await _evaluate(
        criteria="- Live: restart the daemon.\n- Install the release.",
        changes_summary="Release installed successfully.",
        agent_caller=True,
    )

    assert result.error_type == "close_review_required"
    assert result.extra["coordinator_owned_pending"] is True


@pytest.mark.asyncio
async def test_non_agent_still_requires_live_operational_evidence() -> None:
    result = await _evaluate(
        criteria="- Live: restart the daemon.\n- Install the release.",
        changes_summary="Release installed successfully.",
        agent_caller=False,
    )

    assert result.error_type == "operational_evidence_missing"
    assert result.extra["missing_operational_actions"] == ["restart"]


@pytest.mark.asyncio
async def test_spawned_agent_live_verdict_is_pending_external(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accounted = ValidationResult(can_close=False, error_type="external_pending")
    account = MagicMock(return_value=accounted)
    monkeypatch.setattr(review_gate, "account_criteria_verdict", account)

    result = await _evaluate(
        criteria="- Live: restart the daemon.\n- Focused tests pass.",
        changes_summary="Daemon restart completed.",
        agent_caller=True,
        submitted=SubmittedCloseReview(
            verdict={
                "status": "valid",
                "criteria": [
                    {"index": 1, "state": "satisfied", "satisfied": True, "gap": None},
                    {"index": 2, "state": "satisfied", "satisfied": True, "gap": None},
                ],
                "feedback": "Everything passed.",
            },
            review_fingerprint="close",
            evidence_fingerprint="evidence",
            diff_sha="diff",
            test_bodies_sha="tests",
            stable_facts={},
        ),
    )

    assert result is accounted
    parsed = account.call_args.kwargs["verdict"]
    assert [criterion.verdict_state for criterion in parsed.criteria] == [
        "pending_external",
        "satisfied",
    ]


@pytest.mark.asyncio
async def test_non_agent_live_criterion_evaluates_normally_and_can_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accounted = ValidationResult(can_close=True, validation_status="valid")
    account = MagicMock(return_value=accounted)
    monkeypatch.setattr(review_gate, "account_criteria_verdict", account)

    result = await _evaluate(
        criteria="Live: restart the daemon.",
        changes_summary="Daemon restart completed successfully.",
        agent_caller=False,
        submitted=SubmittedCloseReview(
            verdict={
                "status": "valid",
                "criteria": [{"index": 1, "state": "satisfied", "satisfied": True, "gap": None}],
                "feedback": "Live verification passed.",
            },
            review_fingerprint="close",
            evidence_fingerprint="evidence",
            diff_sha="diff",
            test_bodies_sha="tests",
            stable_facts={},
        ),
    )

    assert result.can_close is True
    assert account.call_args.kwargs["verdict"].criteria[0].verdict_state == "satisfied"


@pytest.mark.asyncio
async def test_no_work_disposition_skips_operational_evidence_gate() -> None:
    prepare = MagicMock(return_value=_prepared())

    result = await _evaluate(
        criteria="Deploy the service and run a smoke check.",
        changes_summary="Superseded by the replacement task.",
        reason="obsolete",
        prepare=prepare,
    )

    assert result.error_type == "close_review_required"
    prepare.assert_called_once()


def _prepared(criteria: tuple[str, ...] = ("Criterion.",)) -> PreparedCloseReview:
    return PreparedCloseReview(
        criteria=criteria,
        review_fingerprint="close",
        evidence_fingerprint="evidence",
        diff_sha="diff",
        test_bodies_sha="tests",
        stable_facts={},
        manifest_count=1,
        excerpt_chars=10,
    )


async def _evaluate(
    *,
    submitted: SubmittedCloseReview | None = None,
    criteria: str = "Criterion.",
    changes_summary: str = "summary",
    checklist_facts: dict[str, object] | None = None,
    reason: str = "completed",
    prepare: MagicMock | None = None,
    agent_caller: bool | None = None,
) -> ValidationResult:
    task = Task(
        id="task",
        project_id="project",
        title="Task",
        priority=2,
        task_type="task",
        created_at=datetime(2026, 8, 21, tzinfo=UTC),
        updated_at=datetime(2026, 8, 21, tzinfo=UTC),
        validation_criteria=criteria,
    )
    prepare_review = prepare or MagicMock(
        return_value=_prepared(split_validation_criteria(criteria))
    )
    validator = cast(
        TaskValidator,
        SimpleNamespace(prepare_task_review=prepare_review),
    )
    closing_session_id = None
    if agent_caller is None:
        ctx = cast(RegistryContext, SimpleNamespace())
    else:
        closing_session_id = "caller"
        db = SimpleNamespace(
            fetchone=MagicMock(
                return_value={"id": UUID("00000000-0000-0000-0000-000000000001")}
                if agent_caller
                else None
            )
        )
        ctx = cast(
            RegistryContext,
            SimpleNamespace(task_manager=SimpleNamespace(db=db)),
        )
    return await evaluate_close_criteria(
        task=task,
        task_validator=validator,
        ctx=ctx,
        resolved_id=task.id,
        changes_summary=changes_summary,
        diff_text="diff",
        checklist_facts=checklist_facts or {},
        validation_config=None,
        reason=reason,
        description="",
        test_bodies="tests",
        closing_session_id=closing_session_id,
        submitted_review=submitted,
    )
