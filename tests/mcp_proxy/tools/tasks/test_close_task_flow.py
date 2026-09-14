"""Checklist evaluation and conditional-close flow contracts."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import gobby.mcp_proxy.tools.tasks._lifecycle_close as lifecycle
import gobby.mcp_proxy.tools.tasks._lifecycle_close_finalization as close_finalization
import gobby.mcp_proxy.tools.tasks._lifecycle_close_tool as close_tool
import gobby.mcp_proxy.tools.tasks._lifecycle_validation as lifecycle_validation
from gobby.config.tasks import TaskValidationConfig
from gobby.llm import LLMService
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.tasks._close_evaluation_support import (
    CloseAttributionSnapshot,
    CloseEvaluationFingerprint,
    closes_as_structural_parent,
    fingerprint_differences,
)
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._lifecycle_close import _commit_close, _evaluate_close
from gobby.mcp_proxy.tools.tasks._lifecycle_close_preview import CloseEvaluation
from gobby.mcp_proxy.tools.tasks._lifecycle_close_tool import register_close_task
from gobby.mcp_proxy.tools.tasks._lifecycle_validation import ValidationResult
from gobby.mcp_proxy.tools.tasks._notifications import _notification_tasks as notifications
from gobby.mcp_proxy.tools.tasks._task_scope import TaskScopeEvaluation
from gobby.sessions.machine_scope import RemoteSessionOwnershipError
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.task_close_reviews import TaskCloseReviewStore
from gobby.storage.tasks import LocalTaskManager, Task, TaskHasOpenChildrenError
from gobby.tasks.acceptance_artifacts import AcceptanceArtifactResult, AcceptanceTest
from gobby.tasks.tdd_evidence import TddEvidenceResult
from gobby.tasks.transcript_evidence import (
    TranscriptEvidence,
    TranscriptEvidenceUnavailable,
    TranscriptValidationRun,
)
from gobby.tasks.validation import TaskValidator
from gobby.utils.machine_id import require_machine_id
from gobby.utils.session_context import session_context_for_test
from gobby.workflows.state_manager import SessionVariableManager

pytestmark = pytest.mark.unit

_MACHINE_ID = "21000000-0000-4000-8000-000000000001"


@pytest.fixture(autouse=True)
def _close_gates_are_quiet() -> Iterator[None]:
    with (
        patch.object(lifecycle, "collect_commit_paths", return_value=set()),
        patch.object(lifecycle, "unlinked_tagged_commits", return_value=(([], []), None)),
        patch.object(
            close_finalization,
            "unlinked_tagged_commits",
            return_value=(([], []), None),
        ),
    ):
        yield


def _task(*, criteria: str | None = "Focused tests pass.") -> Task:
    return Task(
        id="00000000-0000-4000-8000-000000000101",
        project_id="00000000-0000-4000-8000-000000000201",
        title="Close checklist leaf",
        category="code",
        priority=2,
        task_type="task",
        created_at=datetime(2026, 7, 27, 12, tzinfo=UTC),
        updated_at=datetime(2026, 7, 27, 12, tzinfo=UTC),
        claimed_by_session_id="00000000-0000-4000-8000-000000000301",
        validation_criteria=criteria,
        validation_fail_count=0,
        stages=({"stage_name": "development", "position": 0, "state": "in_progress"},),
    )


def _ctx(
    task: Task,
    validator: object = None,
    *,
    agent_registry: object | None = None,
) -> RegistryContext:
    manager = MagicMock()
    manager.db = MagicMock()
    manager.get_task.return_value = task
    manager.list_tasks.return_value = []
    close_session = SimpleNamespace(id=task.claimed_by_session_id, machine_id=_MACHINE_ID)
    return cast(
        RegistryContext,
        SimpleNamespace(
            task_manager=manager,
            task_validator=validator,
            agent_registry=agent_registry,
            project_manager=MagicMock(),
            session_manager=SimpleNamespace(get=lambda _session_id: close_session),
            session_var_manager=SimpleNamespace(get_variables=lambda _session_id: {}),
            validation_config=None,
            resolve_session_id=lambda session_id: session_id,
            get_current_project_name=lambda: "gobby",
        ),
    )


def _successful_transcript(task: Task, *, command: str) -> TranscriptEvidence:
    now = datetime(2026, 9, 2, 12, 5, tzinfo=UTC)
    return TranscriptEvidence(
        validation_runs=(
            TranscriptValidationRun(
                session_id=task.claimed_by_session_id or "",
                source="codex",
                command=command,
                categories=("test",),
                matcher_id="pytest",
                label="pytest",
                outcome="success",
                started_at=now,
                completed_at=now,
                order=1,
                exit_code=0,
            ),
        ),
        sessions=(task.claimed_by_session_id or "",),
    )


async def _evaluate_named_test_close(
    task: Task,
    *,
    tdd_result: TddEvidenceResult,
) -> tuple[CloseEvaluation, MagicMock]:
    review = AsyncMock(
        return_value=ValidationResult(
            can_close=True,
            validation_status="valid",
            validation_feedback="Criteria satisfied.",
            reset_reason="llm_valid",
            extra={"verdict": {"status": "valid"}},
        )
    )
    transcript = _successful_transcript(
        task,
        command="uv run pytest tests/test_example.py -q",
    )
    artifacts = AcceptanceArtifactResult(
        passed=True,
        tests=(
            AcceptanceTest(
                reference="tests/test_example.py::test_example",
                path="tests/test_example.py",
                symbol="test_example",
                body="def test_example() -> None:\n    assert True\n",
            ),
        ),
        findings=(),
        evidence_files=(),
    )
    tdd_check = MagicMock(return_value=tdd_result)

    with (
        patch.object(lifecycle, "resolve_task_id_for_mcp", return_value=task.id),
        patch.object(lifecycle, "resolve_task_repo_path", return_value="/repo"),
        patch.object(close_finalization, "_claimed_session_window_start", return_value=None),
        patch.object(close_finalization, "_linked_commit_paths", return_value=frozenset()),
        patch.object(close_finalization, "_committable_task_paths", return_value=set()),
        patch.object(lifecycle_validation, "task_dirty_paths_async", return_value=set()),
        patch.object(
            lifecycle,
            "resolve_close_commit_shas",
            return_value=(["abc123"], None),
        ),
        patch.object(lifecycle, "active_validation_backoff", return_value=None),
        patch.object(
            lifecycle,
            "evaluate_task_scope",
            return_value=TaskScopeEvaluation((), (), ()),
        ),
        patch.object(
            lifecycle,
            "_derive_close_transcript_evidence",
            AsyncMock(return_value=transcript),
        ),
        patch.object(lifecycle, "evaluate_acceptance_artifacts", return_value=artifacts),
        patch.object(lifecycle, "evaluate_tdd_evidence", tdd_check),
        patch.object(lifecycle, "collect_commit_diff_text", return_value="diff"),
        patch.object(lifecycle, "evaluate_criteria_review", review),
        patch("gobby.workflows.task_claim_state.target_task_has_edits", return_value=False),
        patch("gobby.workflows.task_claim_state.task_edited_file_set", return_value=set()),
    ):
        evaluation = await _evaluate_close(
            _ctx(task, validator=object()),
            task_id=task.id,
            reason="completed",
            changes_summary="Implemented and tested.",
            commit_sha="abc123",
            project_path=None,
            response_detail="diagnostic",
        )

    return evaluation, tdd_check


def _ready_evaluation(
    task: Task,
    *,
    attribution: CloseAttributionSnapshot | None = None,
    children_state: tuple[tuple[str, str | None, bool], ...] = (),
) -> CloseEvaluation:
    if attribution is None:
        attribution = CloseAttributionSnapshot(
            owner_session_id=task.claimed_by_session_id or "",
            attributed=False,
            raw_paths=frozenset(),
            edited_paths=frozenset(),
            clean_proof_paths=frozenset(),
            had_attributed_edits=False,
            claim_started_at=None,
        )
    evaluation = CloseEvaluation(task.id)
    evaluation.task = task
    evaluation.task_id = task.id
    evaluation.repo_path = "/repo"
    evaluation.resolved_session_id = task.claimed_by_session_id
    evaluation.fingerprint = CloseEvaluationFingerprint.capture(
        task,
        children_state=children_state,
        attribution=attribution,
    )
    evaluation.scope_snapshot = ((), (), (), (), ())
    evaluation.pass_gate(11, "criteria_review", "Passed.")
    return evaluation


@pytest.mark.asyncio
async def test_missing_criteria_stops_before_llm() -> None:
    task = _task(criteria=None)
    review = AsyncMock()
    ctx = _ctx(task, validator=object())

    with (
        patch.object(lifecycle, "resolve_task_id_for_mcp", return_value=task.id),
        patch.object(lifecycle, "resolve_task_repo_path", return_value="/repo"),
        patch.object(lifecycle, "evaluate_criteria_review", review),
    ):
        evaluation = await _evaluate_close(
            ctx,
            task_id=task.id,
            reason="completed",
            changes_summary="Implemented.",
            commit_sha=None,
            project_path=None,
            response_detail="diagnostic",
        )

    assert evaluation.error == "missing_validation_criteria"
    assert [gate.item for gate in evaluation.gates] == [1, 2, 3, 4, 5]
    review.assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_task_edit_entry_allows_no_edit_research_close() -> None:
    task = replace(_task(), category="research")
    ctx = _ctx(task, validator=object())
    ctx.session_var_manager = cast(
        SessionVariableManager,
        SimpleNamespace(get_variables=lambda _session_id: {"task_edited_files": {task.id: []}}),
    )
    review = AsyncMock(
        return_value=ValidationResult(
            can_close=True,
            validation_status="valid",
            validation_feedback="Research criteria satisfied.",
            reset_reason="llm_valid",
        )
    )

    with (
        patch.object(lifecycle, "resolve_task_id_for_mcp", return_value=task.id),
        patch.object(lifecycle, "resolve_task_repo_path", return_value="/repo"),
        patch.object(close_finalization, "_claimed_session_window_start", return_value=None),
        patch.object(lifecycle, "resolve_close_commit_shas", return_value=([], None)),
        patch.object(lifecycle, "collect_commit_diff_text", return_value=""),
        patch.object(
            lifecycle,
            "_derive_close_transcript_evidence",
            AsyncMock(return_value=TranscriptEvidence()),
        ),
        patch.object(lifecycle, "evaluate_criteria_review", review),
    ):
        evaluation = await _evaluate_close(
            ctx,
            task_id=task.id,
            reason="completed",
            changes_summary="Completed read-only research.",
            commit_sha=None,
            project_path=None,
            response_detail="diagnostic",
        )

    assert evaluation.ready is True
    assert evaluation.had_attributed_edits is False
    assert evaluation.commit_shas == []
    review.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "failure", "missing", "unavailable", "remote"])
async def test_manual_close_supplies_available_command_evidence_to_review(outcome: str) -> None:
    task = replace(_task(criteria="npm ci and focused Prettier check succeed."), category="manual")
    ctx = _ctx(task, validator=object())
    ctx.session_var_manager = cast(
        SessionVariableManager,
        SimpleNamespace(get_variables=lambda _session_id: {"task_edited_files": {task.id: []}}),
    )
    evidence = _successful_transcript(task, command="npx prettier --check web")
    run = replace(evidence.validation_runs[0], categories=("format",))
    install = replace(run, command="npm ci", categories=(), order=run.order + 1)
    if outcome == "failure":
        install = replace(install, outcome="failure", exit_code=1)
    evidence = replace(evidence, validation_runs=(run,), command_runs=(install,))
    if outcome == "missing":
        evidence = TranscriptEvidence(sessions=(task.claimed_by_session_id or "",))
    derive = AsyncMock(return_value=evidence)
    if outcome == "unavailable":
        derive.side_effect = TranscriptEvidenceUnavailable(
            "Transcript missing", source="codex", attempted_paths=("/missing.jsonl",)
        )
    elif outcome == "remote":
        derive.side_effect = RemoteSessionOwnershipError("Transcript belongs to another machine")
    review = AsyncMock(
        return_value=ValidationResult(can_close=False, error_type="agentic_review_required")
    )
    with (
        patch.object(lifecycle, "resolve_task_id_for_mcp", return_value=task.id),
        patch.object(lifecycle, "resolve_task_repo_path", return_value="/repo"),
        patch.object(close_finalization, "_claimed_session_window_start", return_value=None),
        patch.object(lifecycle, "resolve_close_commit_shas", return_value=([], None)),
        patch.object(lifecycle, "collect_commit_diff_text", return_value=""),
        patch.object(lifecycle, "_derive_close_transcript_evidence", derive),
        patch.object(lifecycle, "active_validation_backoff") as backoff,
        patch.object(lifecycle, "evaluate_criteria_review", review),
    ):
        evaluation = await _evaluate_close(
            ctx,
            task_id=task.id,
            reason="completed",
            changes_summary="Restored dependencies.",
            commit_sha=None,
            project_path=None,
            response_detail="diagnostic",
        )
    derive.assert_awaited_once()
    backoff.assert_not_called()
    review.assert_awaited_once()
    assert evaluation.error == "agentic_review_required"
    assert next(gate for gate in evaluation.gates if gate.item == 10).status == "skipped"
    facts = review.call_args.kwargs["checklist_facts"]["validation_commands"]
    assert facts == evaluation.extra["validation_commands"]
    if outcome in {"success", "failure"}:
        assert [(item["core_command"], item["outcome"]) for item in facts["latest_runs"]] == [
            ("npx prettier --check web", "success"),
            ("npm ci", outcome),
        ]
    else:
        assert facts["latest_runs"] == []
        if outcome == "unavailable":
            assert "Transcript missing" in facts["degraded_capabilities"][0]
        elif outcome == "remote":
            assert "another machine" in facts["degraded_capabilities"][0]


@pytest.mark.asyncio
async def test_no_work_disposition_skips_delivery_gates_but_runs_review() -> None:
    task = replace(_task(), category="research")
    ctx = _ctx(task, validator=object())
    ctx.session_var_manager = cast(
        SessionVariableManager,
        SimpleNamespace(get_variables=lambda _session_id: {"task_edited_files": {task.id: []}}),
    )
    review = AsyncMock(
        return_value=ValidationResult(
            can_close=True,
            validation_status="valid",
            validation_feedback="Duplicate disposition is specific.",
            reset_reason="llm_valid",
        )
    )

    with (
        patch.object(lifecycle, "resolve_task_id_for_mcp", return_value=task.id),
        patch.object(lifecycle, "resolve_task_repo_path", return_value="/repo"),
        patch.object(close_finalization, "_claimed_session_window_start", return_value=None),
        patch.object(lifecycle, "resolve_close_commit_shas", return_value=([], None)),
        patch.object(lifecycle, "collect_commit_diff_text", return_value=""),
        patch.object(
            lifecycle,
            "_derive_close_transcript_evidence",
            AsyncMock(return_value=TranscriptEvidence()),
        ),
        patch.object(lifecycle, "evaluate_criteria_review", review),
    ):
        evaluation = await _evaluate_close(
            ctx,
            task_id=task.id,
            reason="duplicate",
            changes_summary="Duplicate of #100, which owns this exact behavior.",
            commit_sha=None,
            project_path=None,
            response_detail="diagnostic",
        )

    assert evaluation.ready is True
    delivery = {gate.name: gate.status for gate in evaluation.gates[10:12]}
    assert delivery == {
        "acceptance_artifacts": "skipped",
        "tdd_evidence": "skipped",
    }
    review.assert_awaited_once()


@pytest.mark.asyncio
async def test_ready_leaf_detaches_criteria_review_and_records_latency() -> None:
    task = _task()
    ctx = _ctx(task, validator=object())
    review = AsyncMock(
        return_value=ValidationResult(
            can_close=False,
            error_type="agentic_review_required",
            extra={
                "review_fingerprint": "review",
                "deterministic_evidence_fingerprint": "evidence",
            },
        )
    )
    now = datetime(2026, 7, 27, 12, 5, tzinfo=UTC)
    transcript = TranscriptEvidence(
        validation_runs=(
            TranscriptValidationRun(
                session_id=task.claimed_by_session_id or "",
                source="codex",
                command="uv run pytest tests/tasks/test_close_checklist.py -q",
                categories=("test",),
                matcher_id="pytest",
                label="pytest",
                outcome="success",
                started_at=now,
                completed_at=now,
                order=1,
                exit_code=0,
            ),
        ),
        sessions=(task.claimed_by_session_id or "",),
    )
    linked_paths = AsyncMock(return_value=frozenset({"src/a.py"}))

    with (
        patch.object(lifecycle, "resolve_task_id_for_mcp", return_value=task.id),
        patch.object(lifecycle, "resolve_task_repo_path", return_value="/repo"),
        patch.object(close_finalization, "_claimed_session_window_start", return_value=None),
        patch.object(close_finalization, "_linked_commit_paths", linked_paths),
        patch.object(close_finalization, "_committable_task_paths", return_value={"src/a.py"}),
        patch.object(lifecycle_validation, "task_dirty_paths_async", return_value=set()),
        patch.object(
            lifecycle,
            "resolve_close_commit_shas",
            return_value=(["base123", "abc123"], None),
        ),
        patch.object(
            lifecycle,
            "validate_commit_requirements",
            return_value=ValidationResult(can_close=True),
        ),
        patch.object(lifecycle, "active_validation_backoff", return_value=None),
        patch.object(
            lifecycle,
            "_derive_close_transcript_evidence",
            AsyncMock(return_value=transcript),
        ),
        patch.object(lifecycle, "collect_commit_diff_text", return_value="diff"),
        patch.object(lifecycle, "evaluate_criteria_review", review),
        patch.object(lifecycle, "perf_counter", side_effect=[10.0, 10.0125]),
        patch(
            "gobby.workflows.task_claim_state.target_task_has_edits",
            return_value=False,
        ),
        patch(
            "gobby.workflows.task_claim_state.task_edited_file_set",
            return_value=set(),
        ),
    ):
        evaluation = await _evaluate_close(
            ctx,
            task_id=task.id,
            reason="completed",
            changes_summary="Implemented and tested.",
            commit_sha="abc123",
            project_path=None,
            response_detail="diagnostic",
        )

    assert evaluation.ready is False
    assert evaluation.error == "agentic_review_required"
    assert evaluation.extra["criteria_review_duration_ms"] == 12.5
    assert [gate.item for gate in evaluation.gates] == list(range(1, 14))
    linked_paths.assert_awaited_once_with(task, "/repo", ("base123", "abc123"))
    review.assert_awaited_once()


async def _evaluate_with_tagged_scan(
    scan: tuple[list[str], list[str]],
) -> tuple[CloseEvaluation, AsyncMock, AsyncMock, MagicMock]:
    task = _task()
    ctx = _ctx(task, validator=object())
    review = AsyncMock(
        return_value=ValidationResult(
            can_close=True,
            validation_status="valid",
            validation_feedback="Criteria satisfied.",
            reset_reason="llm_valid",
            extra={"verdict": {"status": "valid"}},
        )
    )
    now = datetime(2026, 7, 27, 12, 5, tzinfo=UTC)
    transcript = TranscriptEvidence(
        validation_runs=(
            TranscriptValidationRun(
                session_id=task.claimed_by_session_id or "",
                source="codex",
                command="uv run pytest tests/tasks/test_close_checklist.py -q",
                categories=("test",),
                matcher_id="pytest",
                label="pytest",
                outcome="success",
                started_at=now,
                completed_at=now,
                order=1,
                exit_code=0,
            ),
        ),
        sessions=(task.claimed_by_session_id or "",),
    )
    derive_transcript = AsyncMock(return_value=transcript)
    tagged_scan = AsyncMock(return_value=(scan, None))

    with (
        patch.object(lifecycle, "resolve_task_id_for_mcp", return_value=task.id),
        patch.object(lifecycle, "resolve_task_repo_path", return_value="/repo"),
        patch.object(close_finalization, "_claimed_session_window_start", return_value=None),
        patch.object(close_finalization, "_linked_commit_paths", return_value=frozenset({"a"})),
        patch.object(close_finalization, "_committable_task_paths", return_value={"a"}),
        patch.object(lifecycle_validation, "task_dirty_paths_async", return_value=set()),
        patch.object(lifecycle, "resolve_close_commit_shas", return_value=(["abc123"], None)),
        patch.object(lifecycle, "unlinked_tagged_commits", tagged_scan),
        patch.object(
            lifecycle,
            "validate_commit_requirements",
            return_value=ValidationResult(can_close=True),
        ),
        patch.object(lifecycle, "active_validation_backoff", return_value=None),
        patch.object(lifecycle, "_derive_close_transcript_evidence", derive_transcript),
        patch.object(lifecycle, "collect_commit_diff_text", return_value="diff"),
        patch.object(lifecycle, "evaluate_criteria_review", review),
        patch("gobby.workflows.task_claim_state.target_task_has_edits", return_value=False),
        patch("gobby.workflows.task_claim_state.task_edited_file_set", return_value=set()),
    ):
        evaluation = await _evaluate_close(
            ctx,
            task_id=task.id,
            reason="completed",
            changes_summary="Implemented and tested.",
            commit_sha="abc123",
            project_path=None,
            response_detail="diagnostic",
        )
    return evaluation, review, derive_transcript, tagged_scan


@pytest.mark.asyncio
async def test_unlinked_tagged_commit_on_head_fails_gate_seven_before_review() -> None:
    evaluation, review, derive_transcript, tagged_scan = await _evaluate_with_tagged_scan(
        (["abc999"], ["def888"])
    )

    assert evaluation.ready is False
    assert evaluation.error == "unlinked_tagged_commits"
    assert [gate.item for gate in evaluation.gates] == list(range(1, 8))
    assert evaluation.gates[-1].status == "failed"
    assert "abc999" in (evaluation.message or "")
    assert "link_commit(task_id, commit_sha)" in (evaluation.action or "")
    assert "auto_link_commits(task_id, since='2026-07-27T12:00:00+00:00')" in (
        evaluation.action or ""
    )
    assert evaluation.extra["unlinked_tagged_commit_shas"] == ["abc999"]
    assert evaluation.extra["other_ref_tagged_commit_shas"] == ["def888"]
    assert tagged_scan.call_args.kwargs["commit_shas"] == ["abc123"]
    derive_transcript.assert_not_awaited()
    review.assert_not_awaited()


@pytest.mark.asyncio
async def test_tagged_commit_only_on_another_ref_is_a_diagnostic_not_a_blocker() -> None:
    evaluation, review, _derive_transcript, _scan = await _evaluate_with_tagged_scan(
        ([], ["def888"])
    )

    assert evaluation.ready is True
    assert [gate.item for gate in evaluation.gates] == list(range(1, 14))
    assert evaluation.gates[6].details == {"other_ref_tagged_commit_shas": ["def888"]}
    assert evaluation.extra["other_ref_tagged_commit_shas"] == ["def888"]
    review.assert_awaited_once()


@pytest.mark.asyncio
async def test_named_acceptance_test_skips_tdd_gate_when_task_does_not_require_tdd() -> None:
    task = replace(
        _task(criteria="Behavior is pinned. test: `tests/test_example.py::test_example`."),
        category="test",
    )

    evaluation, tdd_check = await _evaluate_named_test_close(
        task,
        tdd_result=TddEvidenceResult(
            passed=False,
            skipped=False,
            findings=("no production edit follows the test edit",),
        ),
    )

    assert evaluation.ready is True
    assert evaluation.gates[11].name == "tdd_evidence"
    assert evaluation.gates[11].status == "skipped"
    tdd_check.assert_not_called()


@pytest.mark.asyncio
async def test_named_acceptance_test_keeps_tdd_gate_when_task_requires_tdd() -> None:
    task = replace(
        _task(criteria="Behavior is pinned. test: `tests/test_example.py::test_example`."),
        labels=["tdd:required"],
    )

    evaluation, tdd_check = await _evaluate_named_test_close(
        task,
        tdd_result=TddEvidenceResult(
            passed=False,
            skipped=False,
            findings=("no production edit follows the test edit",),
        ),
    )

    assert evaluation.ready is False
    assert evaluation.error == "tdd_evidence_missing"
    assert evaluation.gates[-1].name == "tdd_evidence"
    assert evaluation.gates[-1].status == "failed"
    tdd_check.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scope_justification", "justification_error", "continues"),
    [
        pytest.param(
            None,
            "A scope_justification is required for out-of-scope paths.",
            False,
            id="missing",
        ),
        pytest.param(
            "too short",
            "scope_justification must be at least 20 characters.",
            False,
            id="invalid",
        ),
        pytest.param(
            "The shared implementation path is required by the scoped tests.",
            None,
            True,
            id="valid",
        ),
    ],
)
async def test_scope_justification_controls_downstream_close_evidence(
    scope_justification: str | None,
    justification_error: str | None,
    continues: bool,
) -> None:
    task = replace(_task(), labels=["tdd:required"])
    ctx = _ctx(task, validator=object())
    scope = TaskScopeEvaluation(
        declared_paths=("tests/",),
        actual_paths=("src/gobby/service.py",),
        out_of_scope_paths=("src/gobby/service.py",),
        scope_justification=scope_justification if continues else None,
        justification_error=justification_error,
    )
    transcript = AsyncMock(
        return_value=_successful_transcript(
            task,
            command="uv run pytest tests/tasks/test_close_checklist.py -q",
        )
    )
    artifacts = AcceptanceArtifactResult(
        passed=True,
        tests=(),
        findings=(),
        evidence_files=(),
    )
    scope_check = AsyncMock(return_value=scope)
    dirty_paths = AsyncMock(return_value=set())
    validation_paths = AsyncMock(return_value=set())
    diff = AsyncMock(return_value="diff")
    acceptance = AsyncMock(return_value=artifacts)
    tdd = MagicMock(return_value=TddEvidenceResult(passed=True, skipped=False, findings=()))
    review = AsyncMock(
        return_value=ValidationResult(
            can_close=True,
            validation_status="valid",
            validation_feedback="Criteria satisfied.",
            reset_reason="llm_valid",
            extra={"verdict": {"status": "valid"}},
        )
    )

    with (
        patch.object(lifecycle, "resolve_task_id_for_mcp", return_value=task.id),
        patch.object(lifecycle, "resolve_task_repo_path", return_value="/repo"),
        patch.object(close_finalization, "_claimed_session_window_start", return_value=None),
        patch.object(
            close_finalization,
            "_committable_task_paths",
            return_value={"src/gobby/service.py"},
        ),
        patch.object(lifecycle, "resolve_close_commit_shas", return_value=(["abc123"], None)),
        patch.object(
            lifecycle,
            "validate_commit_requirements",
            return_value=ValidationResult(can_close=True),
        ),
        patch.object(lifecycle, "evaluate_task_scope", scope_check),
        patch.object(lifecycle_validation, "task_dirty_paths_async", dirty_paths),
        patch.object(lifecycle, "collect_commit_paths", validation_paths),
        patch.object(lifecycle, "active_validation_backoff", return_value=None),
        patch.object(lifecycle, "_derive_close_transcript_evidence", transcript),
        patch.object(lifecycle, "collect_commit_diff_text", diff),
        patch.object(lifecycle, "evaluate_acceptance_artifacts", acceptance),
        patch.object(lifecycle, "evaluate_tdd_evidence", tdd),
        patch.object(lifecycle, "evaluate_criteria_review", review),
        patch(
            "gobby.workflows.task_claim_state.target_task_has_edits",
            return_value=True,
        ),
        patch(
            "gobby.workflows.task_claim_state.task_edited_file_set",
            return_value={"src/gobby/service.py"},
        ),
    ):
        evaluation = await _evaluate_close(
            ctx,
            task_id=task.id,
            reason="completed",
            changes_summary="Implemented and tested.",
            commit_sha="abc123",
            project_path=None,
            response_detail="diagnostic",
            scope_justification=scope_justification,
        )

    assert scope_check.call_args.kwargs["scope_justification"] == scope_justification
    if continues:
        assert evaluation.ready is True
        assert [gate.item for gate in evaluation.gates] == list(range(1, 14))
        dirty_paths.assert_awaited_once()
        validation_paths.assert_called_once()
        transcript.assert_awaited_once()
        diff.assert_awaited_once()
        acceptance.assert_awaited_once()
        tdd.assert_called_once()
        review.assert_awaited_once()
        return

    response = evaluation.response(preview=True)
    assert evaluation.error == "task_scope_mismatch"
    assert [gate.item for gate in evaluation.gates] == list(range(1, 9))
    assert response["out_of_scope_paths"] == ["src/gobby/service.py"]
    assert response["blocking_reasons"] == [justification_error]
    assert "scope_justification" in response["required_actions"][0]
    dirty_paths.assert_not_awaited()
    validation_paths.assert_not_called()
    transcript.assert_not_awaited()
    diff.assert_not_awaited()
    acceptance.assert_not_awaited()
    tdd.assert_not_called()
    review.assert_not_awaited()


@pytest.mark.asyncio
async def test_blocked_preview_returns_diagnostics_without_commit() -> None:
    ctx = _ctx(_task())
    evaluation = CloseEvaluation("task", response_detail="diagnostic").fail(
        9,
        "uncommitted_task_edits",
        "uncommitted_task_edits",
        "Commit the task-attributed edits.",
    )
    evaluate = AsyncMock(return_value=evaluation)
    commit = AsyncMock()
    registry = InternalToolRegistry("gobby-tasks")
    register_close_task(registry, ctx)

    with (
        patch.object(close_tool, "_evaluate_close", evaluate),
        patch.object(close_tool, "_commit_close", commit),
        patch.object(close_tool, "active_review_response", return_value=None),
    ):
        result = await registry.call(
            "close_task",
            {
                "task_id": "task",
                "changes_summary": "Implemented.",
                "preview": True,
                "response_detail": "diagnostic",
            },
        )

    assert result["success"] is False
    assert result["closed"] is False
    assert result["checklist"][0]["item"] == 9
    evaluate.assert_awaited_once()
    commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_ready_preview_commits_same_evaluation() -> None:
    ctx = _ctx(_task())
    evaluation = CloseEvaluation("task")
    evaluation.pass_gate(1, "task_exists", "Task exists.")
    evaluate = AsyncMock(return_value=evaluation)
    commit = AsyncMock(
        return_value={"success": True, "closed": True, "preview": False, "can_close": True}
    )
    launch = AsyncMock()
    registry = InternalToolRegistry("gobby-tasks")
    register_close_task(registry, ctx)

    with (
        patch.object(close_tool, "_evaluate_close", evaluate),
        patch.object(close_tool, "_commit_close", commit),
        patch.object(close_tool, "active_review_response", return_value=None),
        patch.object(close_tool, "launch_close_review", launch),
    ):
        result = await registry.call(
            "close_task",
            {
                "task_id": "task",
                "changes_summary": "Implemented.",
                "preview": True,
            },
        )

    assert result["closed"] is True
    assert result["preview"] is True
    launch.assert_not_awaited()
    evaluate.assert_awaited_once()
    commit.assert_awaited_once()
    awaited = commit.await_args
    assert awaited is not None
    assert awaited.args[1] is evaluation


@pytest.mark.asyncio
async def test_concurrent_ordinary_closes_share_review_without_closing_or_releasing_claim() -> None:
    task = _task()
    spawn_started = asyncio.Event()
    release_spawn = asyncio.Event()

    async def spawn_validator(*_args: object, **_kwargs: object) -> dict[str, object]:
        spawn_started.set()
        await release_spawn.wait()
        return {"success": True, "run_id": "00000000-0000-4000-8000-000000000778"}

    agent_registry = SimpleNamespace(call=AsyncMock(side_effect=spawn_validator))
    ctx = _ctx(task, validator=object(), agent_registry=agent_registry)
    evaluation = CloseEvaluation(task.id)
    evaluation.task = task
    evaluation.task_id = task.id
    evaluation.repo_path = "/repo"
    evaluation.resolved_session_id = task.claimed_by_session_id
    evaluation.commit_shas = ["abc123"]
    evaluation.error = "agentic_review_required"
    evaluation.extra.update(
        {
            "review_fingerprint": "review-fingerprint",
            "deterministic_evidence_fingerprint": "evidence-fingerprint",
            "diff_sha": "diff-sha",
            "test_bodies_sha": "test-bodies-sha",
            "stable_facts": {},
            "criteria_review_duration_ms": 4.25,
        }
    )
    review_fields = {
        "id": "review-id",
        "caller_session_id": task.claimed_by_session_id,
        "review_fingerprint": "review-fingerprint",
        "evidence_fingerprint": "evidence-fingerprint",
        "task_id": task.id,
        "close_arguments": {"preview": True},
    }
    launching_review = SimpleNamespace(**review_fields, status="launching")
    running_review = SimpleNamespace(**review_fields, status="running")
    store = MagicMock()
    store.create_or_get_active.side_effect = [
        (launching_review, True),
        (launching_review, False),
    ]
    store.bind_run.return_value = running_review
    registry = InternalToolRegistry("gobby-tasks")
    register_close_task(registry, ctx)

    with (
        patch.object(close_tool, "active_review_response", return_value=None),
        patch.object(close_tool, "_evaluate_close", AsyncMock(return_value=evaluation)),
        patch(
            "gobby.mcp_proxy.tools.tasks._lifecycle_close_orchestration.TaskCloseReviewStore",
            return_value=store,
        ),
    ):
        first_close = asyncio.create_task(
            registry.call(
                "close_task",
                {
                    "task_id": task.id,
                    "changes_summary": "Implemented and tested.",
                    "commit_sha": "abc123",
                    "preview": True,
                },
            )
        )
        await spawn_started.wait()
        pending_result = await registry.call(
            "close_task",
            {
                "task_id": task.id,
                "changes_summary": "Implemented and tested.",
                "commit_sha": "abc123",
                "preview": True,
            },
        )
        release_spawn.set()
        result = await first_close

    assert result["error"] == "agentic_review_required"
    assert result["closed"] is False
    assert result["can_close"] is False
    assert result["review_id"] == "review-id"
    assert result["validator_run_id"] == "00000000-0000-4000-8000-000000000778"
    assert result["criteria_review_duration_ms"] == 4.25
    assert pending_result["error"] == "agentic_review_pending"
    assert pending_result["review_id"] == "review-id"
    assert task.closed_at is None
    assert task.claimed_by_session_id == "00000000-0000-4000-8000-000000000301"
    cast(MagicMock, ctx.task_manager.close_task).assert_not_called()
    assert store.create_or_get_active.call_count == 2
    store.bind_run.assert_called_once_with("review-id", "00000000-0000-4000-8000-000000000778")
    agent_registry.call.assert_awaited_once()
    prompt = agent_registry.call.await_args.args[1]["prompt"]
    assert 'changes_summary="Implemented and tested."' in prompt


@pytest.mark.asyncio
@pytest.mark.integration
async def test_ordinary_close_detaches_real_validation_and_preserves_persisted_claim(
    temp_db: HubDatabase,
    sample_git_project: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager = LocalTaskManager(temp_db)
    session = SessionManager(temp_db).register(
        external_id="ordinary-detached-close",
        machine_id=require_machine_id(),
        source="codex",
        project_id=sample_git_project["id"],
    )
    task = manager.create_task(
        project_id=sample_git_project["id"],
        title="Ordinary detached close",
        category="code",
        task_type="task",
        implementation_domain="backend",
        validation_criteria="Focused tests pass.",
    )
    claimed = manager.claim_task(task.id, session.id)
    provider_call = AsyncMock()
    validator = TaskValidator(
        TaskValidationConfig(),
        cast(LLMService, SimpleNamespace(call_json_feature=provider_call)),
        temp_db,
    )
    render_prompt = MagicMock(
        side_effect=lambda _path, context: "\n".join(str(value) for value in context.values())
    )
    agent_call = AsyncMock(
        return_value={
            "success": True,
            "run_id": "00000000-0000-4000-8000-000000000777",
        }
    )
    ctx = RegistryContext(
        task_manager=manager,
        task_validator_resolver=lambda: validator,
        agent_registry_resolver=lambda: cast(
            InternalToolRegistry,
            SimpleNamespace(call=agent_call),
        ),
    )
    registry = InternalToolRegistry("gobby-tasks")
    register_close_task(registry, ctx)
    transcript = _successful_transcript(
        claimed,
        command="uv run pytest tests/tasks/test_validation.py -q",
    )
    caplog.set_level("INFO", logger=lifecycle.__name__)

    with (
        session_context_for_test(session.id),
        patch.object(validator._loader, "render", render_prompt),
        patch.object(lifecycle, "resolve_task_id_for_mcp", return_value=task.id),
        patch.object(
            lifecycle,
            "resolve_task_repo_path",
            return_value=sample_git_project["repo_path"],
        ),
        patch.object(close_finalization, "_claimed_session_window_start", return_value=None),
        patch.object(close_finalization, "_linked_commit_paths", return_value=frozenset()),
        patch.object(close_finalization, "_committable_task_paths", return_value=set()),
        patch.object(lifecycle_validation, "task_dirty_paths_async", return_value=set()),
        patch.object(lifecycle, "resolve_close_commit_shas", return_value=(["abc123"], None)),
        patch.object(
            lifecycle,
            "validate_commit_requirements",
            return_value=ValidationResult(can_close=True),
        ),
        patch.object(
            lifecycle,
            "_derive_close_transcript_evidence",
            AsyncMock(return_value=transcript),
        ),
        patch.object(lifecycle, "collect_commit_diff_text", return_value="small diff"),
        patch("gobby.workflows.task_claim_state.target_task_has_edits", return_value=False),
        patch("gobby.workflows.task_claim_state.task_edited_file_set", return_value=set()),
    ):
        result = await registry.call(
            "close_task",
            {
                "task_id": task.id,
                "changes_summary": "Implemented and tested.",
                "commit_sha": "abc123",
                "preview": True,
            },
        )

    assert result["error"] == "agentic_review_required"
    assert result["closed"] is False
    assert result["can_close"] is False
    assert result["review_id"]
    assert result["validator_run_id"] == "00000000-0000-4000-8000-000000000777"
    assert result["prompt_chars"] < result["prompt_limit"]
    assert 0 <= result["criteria_review_duration_ms"] < 50
    provider_call.assert_not_awaited()
    persisted = manager.get_task(task.id)
    assert persisted.closed_at is None
    assert persisted.claimed_by_session_id == session.id
    stored_review = TaskCloseReviewStore(temp_db).get(result["review_id"])
    assert stored_review is not None
    assert stored_review.task_id == task.id
    assert stored_review.agent_run_id == "00000000-0000-4000-8000-000000000777"
    assert any("mode=detached" in message for message in caplog.messages)


def test_close_task_schema_has_automated_review_surface() -> None:
    registry = InternalToolRegistry("gobby-tasks")
    register_close_task(registry, _ctx(_task()))

    close_schema = registry.get_schema("close_task")
    submit_schema = registry.get_schema("submit_close_review")

    assert close_schema is not None
    assert "review_run_id" not in close_schema["inputSchema"]["properties"]
    assert submit_schema is not None
    assert submit_schema["inputSchema"]["required"] == ["review_id", "verdict"]


@pytest.mark.asyncio
async def test_commit_set_change_returns_stale_without_close() -> None:
    task = _task()
    ctx = _ctx(task)
    evaluation = CloseEvaluation(task.id)
    evaluation.task = task
    evaluation.task_id = task.id
    evaluation.repo_path = "/repo"
    evaluation.commit_shas = ["before"]
    evaluation.pass_gate(11, "criteria_review", "Passed.")

    with patch.object(
        close_finalization,
        "resolve_close_commit_shas",
        return_value=(["after"], None),
    ):
        result = await _commit_close(
            ctx,
            evaluation,
            changes_summary="Implemented the task changes.",
            reason="completed",
            skip_validation=False,
            override_justification=None,
            commit_sha=None,
        )

    assert result["error"] == "stale_task_state"
    assert result["closed"] is False
    cast(MagicMock, ctx.task_manager.close_task).assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        "category",
        "task-type",
        "claim-owner",
        "validation-criteria",
        "escalation",
        "closure",
        "parent-linkage",
    ],
)
async def test_gate_input_change_returns_stale_without_close(change: str) -> None:
    task = _task()
    ctx = _ctx(task)
    changed_task = {
        "category": lambda: replace(task, category="docs"),
        "task-type": lambda: replace(task, task_type="epic"),
        "claim-owner": lambda: replace(
            task,
            claimed_by_session_id="00000000-0000-4000-8000-000000000999",
        ),
        "validation-criteria": lambda: replace(
            task,
            validation_criteria="Changed criteria.",
        ),
        "escalation": lambda: replace(task, is_escalated=True),
        "closure": lambda: replace(
            task,
            closed_at=datetime(2026, 7, 27, 12, 1, tzinfo=UTC),
        ),
        "parent-linkage": lambda: replace(
            task,
            parent_task_id="00000000-0000-4000-8000-000000000888",
        ),
    }[change]()
    cast(MagicMock, ctx.task_manager.get_task).return_value = changed_task
    evaluation = _ready_evaluation(task)

    result = await _commit_close(
        ctx,
        evaluation,
        changes_summary="Implemented the task changes.",
        reason="completed",
        skip_validation=False,
        override_justification=None,
        commit_sha=None,
    )

    assert result["error"] == "stale_task_state"
    assert result["closed"] is False
    cast(MagicMock, ctx.task_manager.close_task).assert_not_called()


@pytest.mark.asyncio
async def test_children_change_returns_stale_without_close() -> None:
    task = _task()
    child = replace(
        _task(),
        id="00000000-0000-4000-8000-000000000102",
        parent_task_id=task.id,
        closed_at=datetime(2026, 7, 27, 12, 1, tzinfo=UTC),
    )
    ctx = _ctx(task)
    cast(MagicMock, ctx.task_manager.list_tasks).return_value = [child]
    evaluation = _ready_evaluation(task)

    result = await _commit_close(
        ctx,
        evaluation,
        changes_summary="Implemented the task changes.",
        reason="completed",
        skip_validation=False,
        override_justification=None,
        commit_sha=None,
    )

    assert result["error"] == "stale_task_state"
    cast(MagicMock, ctx.task_manager.close_task).assert_not_called()


@pytest.mark.asyncio
async def test_new_attributed_paths_during_review_return_stale() -> None:
    task = _task()
    ctx = _ctx(task)
    evaluation = _ready_evaluation(task)

    with (
        patch(
            "gobby.workflows.task_claim_state.target_task_has_edits",
            return_value=True,
        ),
        patch(
            "gobby.workflows.task_claim_state.task_edited_file_set",
            return_value={"src/new.py"},
        ),
        patch.object(
            close_finalization,
            "_committable_task_paths",
            return_value={"src/new.py"},
        ),
    ):
        result = await _commit_close(
            ctx,
            evaluation,
            changes_summary="Implemented the task changes.",
            reason="completed",
            skip_validation=False,
            override_justification=None,
            commit_sha=None,
        )

    assert result["error"] == "stale_task_state"
    cast(MagicMock, ctx.task_manager.close_task).assert_not_called()


@pytest.mark.asyncio
async def test_same_owner_reclaim_window_change_returns_stale() -> None:
    task = _task()
    ctx = _ctx(task)
    attribution = CloseAttributionSnapshot(
        owner_session_id=task.claimed_by_session_id or "",
        attributed=False,
        raw_paths=frozenset(),
        edited_paths=frozenset(),
        clean_proof_paths=frozenset(),
        had_attributed_edits=False,
        claim_started_at="2026-07-27T12:00:00Z",
    )
    evaluation = _ready_evaluation(task, attribution=attribution)

    with patch.object(
        close_finalization,
        "_claimed_session_window_start",
        return_value="2026-07-27T12:05:00Z",
    ):
        result = await _commit_close(
            ctx,
            evaluation,
            changes_summary="Implemented the task changes.",
            reason="completed",
            skip_validation=False,
            override_justification=None,
            commit_sha=None,
        )

    assert result["error"] == "stale_task_state"
    cast(MagicMock, ctx.task_manager.close_task).assert_not_called()


@pytest.mark.asyncio
async def test_benign_bookkeeping_change_does_not_stale_close() -> None:
    task = _task()
    fresh = replace(
        task,
        updated_at=datetime(2026, 7, 27, 12, 1, tzinfo=UTC),
        path_cache="19236.19238",
    )
    ctx = _ctx(task)
    cast(MagicMock, ctx.task_manager.get_task).return_value = fresh
    evaluation = _ready_evaluation(task)

    with (
        patch.object(
            close_finalization,
            "resolve_close_commit_shas",
            return_value=([], None),
        ) as resolve_commits,
        patch.object(
            close_finalization,
            "link_close_commit_shas",
            return_value=(fresh, None),
        ) as link_commits,
        patch.object(
            close_finalization,
            "notify_parent_on_task_state_change",
        ) as notify_parent,
    ):
        result = await _commit_close(
            ctx,
            evaluation,
            changes_summary="Implemented the task changes.",
            reason="completed",
            skip_validation=False,
            override_justification=None,
            commit_sha=None,
        )

    assert result["closed"] is True
    cast(MagicMock, ctx.task_manager.close_task).assert_called_once()
    resolve_commits.assert_called_once()
    link_commits.assert_called_once()
    notify_parent.assert_called_once()


@pytest.mark.asyncio
async def test_scope_justification_is_rechecked_and_persisted_on_close() -> None:
    task = _task()
    ctx = _ctx(task)
    evaluation = _ready_evaluation(task)
    justification = "The shared implementation path is required by the scoped tests."
    scope = TaskScopeEvaluation(
        declared_paths=("tests/",),
        actual_paths=("src/gobby/service.py",),
        out_of_scope_paths=("src/gobby/service.py",),
        scope_justification=justification,
    )
    evaluation.scope_snapshot = scope.snapshot()
    evaluation.scope_justification = justification

    with (
        patch.object(close_finalization, "resolve_close_commit_shas", return_value=([], None)),
        patch.object(close_finalization, "evaluate_task_scope", return_value=scope),
        patch.object(close_finalization, "link_close_commit_shas", return_value=(task, None)),
        patch.object(close_finalization, "notify_parent_on_task_state_change"),
    ):
        result = await _commit_close(
            ctx,
            evaluation,
            changes_summary="Implemented the task changes.",
            reason="completed",
            skip_validation=False,
            override_justification=None,
            commit_sha=None,
        )

    assert result["closed"] is True
    close_task = cast(MagicMock, ctx.task_manager.close_task)
    assert close_task.call_count == 1
    assert close_task.call_args.args == (task.id,)
    assert close_task.call_args.kwargs["validation_override_reason"] == (
        f"Task scope justification: {justification}"
    )
    assert close_task.call_args.kwargs["reason"] == "completed"


@pytest.mark.asyncio
async def test_justified_escalated_close_skips_review_and_persists_override() -> None:
    task = replace(
        _task(),
        is_escalated=True,
        escalated_at=datetime(2026, 7, 27, 12, 1, tzinfo=UTC),
        escalation_reason="Repeated invalid review",
        validation_fail_count=5,
    )
    ctx = _ctx(task, validator=object())
    review = AsyncMock()

    with (
        patch.object(lifecycle, "resolve_task_id_for_mcp", return_value=task.id),
        patch.object(lifecycle, "resolve_task_repo_path", return_value="/repo"),
        patch.object(close_finalization, "_claimed_session_window_start", return_value=None),
        patch.object(lifecycle, "resolve_close_commit_shas", return_value=([], None)),
        patch.object(lifecycle, "evaluate_criteria_review", review),
    ):
        evaluation = await _evaluate_close(
            ctx,
            task_id=task.id,
            reason="completed",
            changes_summary="Deliberately resolved the escalated task.",
            commit_sha=None,
            project_path=None,
            response_detail="diagnostic",
            override_justification="Reviewed and accepted the current implementation.",
        )

    assert evaluation.ready is True
    assert evaluation.validation_reset_reason == "escalated_deliberate_close"
    assert evaluation.gates[-1].status == "skipped"
    review.assert_not_awaited()

    with (
        patch.object(close_finalization, "resolve_close_commit_shas", return_value=([], None)),
        patch.object(close_finalization, "link_close_commit_shas", return_value=(task, None)),
        patch.object(close_finalization, "_claimed_session_window_start", return_value=None),
        patch.object(close_finalization, "notify_parent_on_task_state_change"),
        patch.object(close_finalization, "_cleanup_closed_claim"),
    ):
        result = await _commit_close(
            ctx,
            evaluation,
            changes_summary="Implemented the task changes.",
            reason="completed",
            skip_validation=False,
            override_justification="Reviewed and accepted the current implementation.",
            commit_sha=None,
        )

    assert result["closed"] is True
    cast(MagicMock, ctx.task_manager.close_task).assert_called_once_with(
        task.id,
        reason="completed",
        closed_in_session_id=task.claimed_by_session_id,
        closed_commit_sha=None,
        closed_ancestors=[],
        validation_override_reason="Reviewed and accepted the current implementation.",
        expected_updated_at=task.updated_at,
        reset_validation_fail_count=True,
        validation_status="valid",
        validation_feedback=None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("override_justification", [None, "   "])
async def test_escalated_close_without_justification_converges_on_actionable_blocker(
    override_justification: str | None,
) -> None:
    task = replace(
        _task(),
        is_escalated=True,
        escalated_at=datetime(2026, 7, 27, 12, 1, tzinfo=UTC),
        escalation_reason="Repeated invalid review",
        validation_fail_count=5,
    )
    ctx = _ctx(task, validator=object())
    review = AsyncMock()

    with (
        patch.object(lifecycle, "resolve_task_id_for_mcp", return_value=task.id),
        patch.object(lifecycle, "resolve_task_repo_path", return_value="/repo"),
        patch.object(close_finalization, "_claimed_session_window_start", return_value=None),
        patch.object(lifecycle, "resolve_close_commit_shas", return_value=([], None)),
        patch.object(lifecycle, "evaluate_criteria_review", review),
    ):
        results = [
            await _evaluate_close(
                ctx,
                task_id=task.id,
                reason="completed",
                changes_summary="Attempted deliberate close.",
                commit_sha=None,
                project_path=None,
                response_detail="diagnostic",
                override_justification=override_justification,
            )
            for _ in range(2)
        ]

    assert [evaluation.error for evaluation in results] == [
        "task_escalated",
        "task_escalated",
    ]
    assert all("override_justification" in (evaluation.action or "") for evaluation in results)
    review.assert_not_awaited()
    cast(MagicMock, ctx.task_manager.close_task).assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("override_justification", [None, "   "])
async def test_escalated_structural_parent_requires_justification(
    override_justification: str | None,
) -> None:
    task = replace(
        _task(),
        task_type="epic",
        is_escalated=True,
        escalated_at=datetime(2026, 7, 27, 12, 1, tzinfo=UTC),
        escalation_reason="Needs human review",
    )
    ctx = _ctx(task, validator=object())
    review = AsyncMock()

    with (
        patch.object(lifecycle, "resolve_task_id_for_mcp", return_value=task.id),
        patch.object(lifecycle, "resolve_task_repo_path", return_value="/repo"),
        patch.object(lifecycle, "resolve_close_commit_shas", return_value=([], None)),
        patch.object(lifecycle, "evaluate_criteria_review", review),
    ):
        evaluation = await _evaluate_close(
            ctx,
            task_id=task.id,
            reason="completed",
            changes_summary=None,
            commit_sha=None,
            project_path=None,
            response_detail="diagnostic",
            override_justification=override_justification,
        )

    assert evaluation.error == "task_escalated"
    assert "override_justification" in (evaluation.action or "")
    review.assert_not_awaited()
    cast(MagicMock, ctx.task_manager.close_task).assert_not_called()


@pytest.mark.asyncio
async def test_justified_escalated_structural_parent_closes_and_persists_override() -> None:
    task = replace(
        _task(),
        task_type="epic",
        is_escalated=True,
        escalated_at=datetime(2026, 7, 27, 12, 1, tzinfo=UTC),
        escalation_reason="Needs human review",
        validation_fail_count=5,
    )
    ctx = _ctx(task, validator=object())
    review = AsyncMock()

    with (
        patch.object(lifecycle, "resolve_task_id_for_mcp", return_value=task.id),
        patch.object(lifecycle, "resolve_task_repo_path", return_value="/repo"),
        patch.object(lifecycle, "resolve_close_commit_shas", return_value=([], None)),
        patch.object(lifecycle, "evaluate_criteria_review", review),
    ):
        evaluation = await _evaluate_close(
            ctx,
            task_id=task.id,
            reason="completed",
            changes_summary=None,
            commit_sha=None,
            project_path=None,
            response_detail="diagnostic",
            override_justification="Reviewed: obsolete escalation, closing deliberately.",
        )

    assert evaluation.ready is True
    assert evaluation.validation_reset_reason == "escalated_deliberate_close"
    assert evaluation.gates[-1].status == "skipped"
    review.assert_not_awaited()

    with (
        patch.object(close_finalization, "resolve_close_commit_shas", return_value=([], None)),
        patch.object(close_finalization, "link_close_commit_shas", return_value=(task, None)),
        patch.object(close_finalization, "notify_parent_on_task_state_change"),
        patch.object(close_finalization, "_cleanup_closed_claim"),
        patch("gobby.hooks.event_handlers._plan.on_epic_terminal"),
    ):
        result = await _commit_close(
            ctx,
            evaluation,
            changes_summary="Implemented the task changes.",
            reason="completed",
            skip_validation=False,
            override_justification="Reviewed: obsolete escalation, closing deliberately.",
            commit_sha=None,
        )

    assert result["closed"] is True
    cast(MagicMock, ctx.task_manager.close_task).assert_called_once_with(
        task.id,
        reason="completed",
        closed_in_session_id=task.claimed_by_session_id,
        closed_commit_sha=None,
        closed_ancestors=[],
        validation_override_reason="Reviewed: obsolete escalation, closing deliberately.",
        expected_updated_at=task.updated_at,
        reset_validation_fail_count=True,
        validation_status="valid",
        validation_feedback=None,
    )


@pytest.mark.asyncio
async def test_dirty_attributed_edit_is_collected_before_acceptance() -> None:
    task = _task()
    ctx = _ctx(task, validator=object())
    transcript = AsyncMock(
        return_value=_successful_transcript(
            task,
            command="uv run pytest tests/tasks/test_close_checklist.py -q",
        )
    )
    artifacts = AcceptanceArtifactResult(
        passed=True,
        tests=(),
        findings=(),
        evidence_files=(),
    )
    review = AsyncMock()

    with (
        patch.object(lifecycle, "resolve_task_id_for_mcp", return_value=task.id),
        patch.object(lifecycle, "resolve_task_repo_path", return_value="/repo"),
        patch.object(close_finalization, "_claimed_session_window_start", return_value=None),
        patch.object(close_finalization, "_committable_task_paths", return_value={"src/a.py"}),
        patch.object(lifecycle_validation, "task_dirty_paths_async", return_value={"src/a.py"}),
        patch.object(
            lifecycle,
            "resolve_close_commit_shas",
            return_value=(["abc123"], None),
        ),
        patch.object(
            lifecycle,
            "validate_commit_requirements",
            return_value=ValidationResult(can_close=True),
        ),
        patch.object(lifecycle, "active_validation_backoff", return_value=None),
        patch.object(lifecycle, "_derive_close_transcript_evidence", transcript),
        patch.object(lifecycle, "collect_commit_diff_text", return_value="diff"),
        patch.object(lifecycle, "evaluate_acceptance_artifacts", return_value=artifacts),
        patch.object(lifecycle, "evaluate_criteria_review", review),
        patch(
            "gobby.workflows.task_claim_state.target_task_has_edits",
            return_value=True,
        ),
        patch(
            "gobby.workflows.task_claim_state.task_edited_file_set",
            return_value={"src/a.py"},
        ),
    ):
        evaluation = await _evaluate_close(
            ctx,
            task_id=task.id,
            reason="completed",
            changes_summary="Implemented and tested.",
            commit_sha="abc123",
            project_path=None,
            response_detail="diagnostic",
        )

    assert evaluation.error == "uncommitted_task_edits"
    assert evaluation.gates[-1].item == 11
    transcript.assert_awaited_once()
    review.assert_not_awaited()


@pytest.mark.asyncio
async def test_epic_skips_leaf_gates_without_llm() -> None:
    task = replace(_task(), task_type="epic", commits=["abc123"])
    ctx = _ctx(task, validator=object())
    review = AsyncMock()

    with (
        patch.object(lifecycle, "resolve_task_id_for_mcp", return_value=task.id),
        patch.object(lifecycle, "resolve_task_repo_path", return_value="/repo"),
        patch.object(lifecycle, "evaluate_criteria_review", review),
    ):
        evaluation = await _evaluate_close(
            ctx,
            task_id=task.id,
            reason="completed",
            changes_summary=None,
            commit_sha=None,
            project_path=None,
            response_detail="diagnostic",
        )

    assert evaluation.ready is True
    assert evaluation.commit_shas == ["abc123"]
    assert [gate.item for gate in evaluation.gates] == list(range(1, 14))
    assert all(gate.status == "skipped" for gate in evaluation.gates[4:])
    review.assert_not_awaited()


def _closed_child_of(task: Task) -> Task:
    return replace(
        _task(),
        id="00000000-0000-4000-8000-000000000102",
        parent_task_id=task.id,
        closed_at=datetime(2026, 7, 27, 12, 1, tzinfo=UTC),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "owned_work",
    [
        pytest.param(
            {"claimed_by_session_id": "00000000-0000-4000-8000-000000000301"}, id="claimed"
        ),
        pytest.param({"claimed_by_session_id": None, "commits": ["abc123"]}, id="linked-commit"),
    ],
)
async def test_worked_task_with_a_closed_child_keeps_its_leaf_gates(
    owned_work: dict[str, Any],
) -> None:
    """A worked leaf that gained a found-work child is still a leaf.

    #21046 closed under the claimed leaf #20969; the direct close of #20969
    then skipped every leaf gate because the task had a child.
    """
    task = replace(_task(criteria=None), **owned_work)
    ctx = _ctx(task, validator=object())
    cast(MagicMock, ctx.task_manager.list_tasks).return_value = [_closed_child_of(task)]

    with (
        patch.object(lifecycle, "resolve_task_id_for_mcp", return_value=task.id),
        patch.object(lifecycle, "resolve_task_repo_path", return_value="/repo"),
    ):
        evaluation = await _evaluate_close(
            ctx,
            task_id=task.id,
            reason="completed",
            changes_summary="Implemented.",
            commit_sha=None,
            project_path=None,
            response_detail="diagnostic",
            closing_session_id="00000000-0000-4000-8000-000000000301",
        )

    assert evaluation.skip_leaf_checks is False
    assert evaluation.error == "missing_validation_criteria"
    assert [gate.item for gate in evaluation.gates] == [1, 2, 3, 4, 5]


@pytest.mark.asyncio
async def test_unworked_parent_with_a_closed_child_still_skips_leaf_gates() -> None:
    task = replace(_task(), claimed_by_session_id=None, commits=None)
    ctx = _ctx(task, validator=object())
    cast(MagicMock, ctx.task_manager.list_tasks).return_value = [_closed_child_of(task)]
    review = AsyncMock()

    with (
        patch.object(lifecycle, "resolve_task_id_for_mcp", return_value=task.id),
        patch.object(lifecycle, "resolve_task_repo_path", return_value="/repo"),
        patch.object(lifecycle, "evaluate_criteria_review", review),
    ):
        evaluation = await _evaluate_close(
            ctx,
            task_id=task.id,
            reason="completed",
            changes_summary=None,
            commit_sha=None,
            project_path=None,
            response_detail="diagnostic",
            closing_session_id="00000000-0000-4000-8000-000000000301",
        )

    assert evaluation.skip_leaf_checks is True
    assert evaluation.ready is True
    assert all(gate.status == "skipped" for gate in evaluation.gates[4:])
    review.assert_not_awaited()


@pytest.mark.asyncio
async def test_commit_epic_persists_allowed_valid_status() -> None:
    task = replace(_task(), task_type="epic")
    ctx = _ctx(task)
    manager = cast(MagicMock, ctx.task_manager)
    evaluation = CloseEvaluation(task.id)
    evaluation.task = task
    evaluation.task_id = task.id
    evaluation.repo_path = "/repo"
    evaluation.resolved_session_id = task.claimed_by_session_id
    evaluation.commit_shas = []
    evaluation.is_epic = True
    evaluation.skip_leaf_checks = True
    evaluation.fingerprint = CloseEvaluationFingerprint.capture(
        task,
        children_state=(),
        attribution=None,
    )

    with (
        patch.object(close_finalization, "resolve_close_commit_shas", return_value=([], None)),
        patch.object(close_finalization, "link_close_commit_shas", return_value=(task, None)),
        patch.object(close_finalization, "notify_parent_on_task_state_change"),
        patch.object(close_finalization, "_cleanup_closed_claim"),
        patch("gobby.hooks.event_handlers._plan.on_epic_terminal"),
    ):
        result = await _commit_close(
            ctx,
            evaluation,
            changes_summary="Implemented the task changes.",
            reason="completed",
            skip_validation=False,
            override_justification=None,
            commit_sha=None,
        )

    assert result["closed"] is True
    assert manager.close_task.call_args.kwargs["validation_status"] == "valid"


def test_closed_task_cleanup_removes_only_its_edit_entry() -> None:
    variables = {
        "claimed_tasks": {"task-1": "#1", "task-2": "#2"},
        "active_task_id": "task-1",
        "task_edited_files": {
            "task-1": ["src/closed.py"],
            "task-2": ["src/remaining.py"],
        },
    }
    session_var_manager = MagicMock()
    session_var_manager.get_variables.return_value = variables
    session_manager = MagicMock()
    ctx = cast(
        RegistryContext,
        SimpleNamespace(
            session_task_manager=MagicMock(),
            session_var_manager=session_var_manager,
            session_manager=session_manager,
        ),
    )
    evaluation = CloseEvaluation("task-1")
    evaluation.task_id = "task-1"
    evaluation.resolved_session_id = "session"
    evaluation.edit_session_id = "session"

    close_finalization._cleanup_closed_claim(ctx, evaluation, ["abc123"])

    updates = session_var_manager.merge_variables.call_args.args[1]
    assert updates["task_edited_files"] == {"task-2": ["src/remaining.py"]}
    session_manager.clear_had_edits.assert_not_called()


@pytest.mark.asyncio
async def test_close_awaits_commit_resolution_and_validation() -> None:
    """Git subprocess ownership lives below these awaited daemon-facing helpers."""
    task = _task()
    ctx = _ctx(task, validator=object())
    # Both helpers run before the scope gate. Later deterministic gates are
    # controlled so the collected scope mismatch remains the primary failure.
    scope = TaskScopeEvaluation(
        declared_paths=("tests/",),
        actual_paths=("src/gobby/service.py",),
        out_of_scope_paths=("src/gobby/service.py",),
        justification_error="A scope_justification is required for out-of-scope paths.",
    )
    resolved_on: list[int] = []
    validated_on: list[int] = []
    transcript = AsyncMock(
        return_value=_successful_transcript(
            task,
            command="uv run pytest tests/tasks/test_close_checklist.py -q",
        )
    )
    artifacts = AcceptanceArtifactResult(
        passed=True,
        tests=(),
        findings=(),
        evidence_files=(),
    )

    async def record_resolve(*_args: object, **_kwargs: object) -> tuple[list[str], None]:
        resolved_on.append(threading.get_ident())
        return (["abc123"], None)

    async def record_validate(*_args: object, **_kwargs: object) -> ValidationResult:
        validated_on.append(threading.get_ident())
        return ValidationResult(can_close=True)

    with (
        patch.object(lifecycle, "resolve_task_id_for_mcp", return_value=task.id),
        patch.object(lifecycle, "resolve_task_repo_path", return_value="/repo"),
        patch.object(close_finalization, "_claimed_session_window_start", return_value=None),
        patch.object(
            close_finalization,
            "_committable_task_paths",
            return_value={"src/gobby/service.py"},
        ),
        patch.object(lifecycle, "resolve_close_commit_shas", record_resolve),
        patch.object(lifecycle, "validate_commit_requirements", record_validate),
        patch.object(lifecycle, "evaluate_task_scope", return_value=scope),
        patch.object(lifecycle_validation, "task_dirty_paths_async", return_value=set()),
        patch.object(lifecycle, "active_validation_backoff", return_value=None),
        patch.object(lifecycle, "_derive_close_transcript_evidence", transcript),
        patch.object(lifecycle, "collect_commit_diff_text", return_value="diff"),
        patch.object(lifecycle, "evaluate_acceptance_artifacts", return_value=artifacts),
        patch.object(lifecycle, "evaluate_criteria_review", AsyncMock()),
        patch(
            "gobby.workflows.task_claim_state.target_task_has_edits",
            return_value=True,
        ),
        patch(
            "gobby.workflows.task_claim_state.task_edited_file_set",
            return_value={"src/gobby/service.py"},
        ),
    ):
        evaluation = await _evaluate_close(
            ctx,
            task_id=task.id,
            reason="completed",
            changes_summary="Implemented and tested.",
            commit_sha="abc123",
            project_path=None,
            response_detail="diagnostic",
        )

    assert evaluation.error == "task_scope_mismatch"
    loop_thread = threading.get_ident()
    assert resolved_on, "resolve_close_commit_shas must run"
    assert validated_on, "validate_commit_requirements must run"
    assert resolved_on == [loop_thread]
    assert validated_on == [loop_thread]


@pytest.mark.asyncio
async def test_commit_close_links_and_closes_off_the_event_loop() -> None:
    """The two mutations commit_close makes both block, so neither may run on the loop.

    link_close_commit_shas reaches git by its own route -- the storage layer's
    link_commit -> normalize_commit_sha -> run_git_command -> subprocess.run --
    which #20861's three offloads did not cover. The storage close_task
    transition runs synchronous psycopg, and _close_eligible_ancestors walks up
    the tree inside the transaction it holds open, so its cost grows with depth
    and sibling count. The loop-lag watchdog caught both below commit_close
    (#20862).
    """
    task = _task()
    ctx = _ctx(task)
    evaluation = _ready_evaluation(task)
    linked_on: list[int] = []
    closed_on: list[int] = []

    def record_link(*_args: object, **_kwargs: object) -> tuple[Task, None]:
        linked_on.append(threading.get_ident())
        return (task, None)

    def record_close(*_args: object, **_kwargs: object) -> None:
        closed_on.append(threading.get_ident())

    cast(MagicMock, ctx.task_manager.close_task).side_effect = record_close

    with (
        patch.object(close_finalization, "resolve_close_commit_shas", return_value=([], None)),
        patch.object(close_finalization, "link_close_commit_shas", record_link),
        patch.object(close_finalization, "notify_parent_on_task_state_change"),
    ):
        result = await _commit_close(
            ctx,
            evaluation,
            changes_summary="Implemented the task changes.",
            reason="completed",
            skip_validation=False,
            override_justification=None,
            commit_sha=None,
        )

    assert result["closed"] is True
    loop_thread = threading.get_ident()
    assert linked_on, "link_close_commit_shas must run"
    assert closed_on, "the storage close_task transition must run"
    assert loop_thread not in linked_on
    assert loop_thread not in closed_on


@pytest.mark.asyncio
async def test_commit_close_runs_every_storage_call_off_the_event_loop() -> None:
    """No synchronous storage call in commit_close may sit on the loop thread.

    #20861 and #20862 offloaded the pieces the 0.2s loop-lag watchdog named --
    the ones that fork git or hold a transaction open while walking ancestors.
    The rest of commit_close still reached psycopg synchronously, which left
    whether the function blocks the loop depending on which of its calls you
    looked at. Each of these is reached from an async function and awaited to
    completion exactly where it ran before (#20864).
    """
    task = replace(_task(), task_type="epic", seq_num=4242)
    ancestor = replace(
        _task(),
        id="00000000-0000-4000-8000-000000000103",
        title="Ancestor epic",
        task_type="epic",
        seq_num=4241,
    )
    ctx = _ctx(task)
    threads: dict[str, list[int]] = {
        name: []
        for name in (
            "get_task",
            "list_tasks",
            "close_task",
            "on_epic_terminal",
            "cleanup_claim",
        )
    }

    def record_get_task(task_id: str, *_args: object, **_kwargs: object) -> Task:
        threads["get_task"].append(threading.get_ident())
        return ancestor if task_id == ancestor.id else task

    def record_list_tasks(*_args: object, **_kwargs: object) -> list[Task]:
        threads["list_tasks"].append(threading.get_ident())
        return []

    def record_close(
        *_args: object,
        closed_ancestors: list[str] | None = None,
        **_kwargs: object,
    ) -> None:
        threads["close_task"].append(threading.get_ident())
        # The transition reports what it auto-closed by filling this list, which
        # is what puts _collect_closed_ancestors on the path below.
        if closed_ancestors is not None:
            closed_ancestors.append(ancestor.id)

    def recorder(name: str) -> Callable[..., None]:
        def record(*_args: object, **_kwargs: object) -> None:
            threads[name].append(threading.get_ident())

        return record

    notify_threads: list[int] = []

    manager = cast(MagicMock, ctx.task_manager)
    manager.get_task.side_effect = record_get_task
    manager.list_tasks.side_effect = record_list_tasks
    manager.close_task.side_effect = record_close

    evaluation = CloseEvaluation(task.id)
    evaluation.task = task
    evaluation.task_id = task.id
    evaluation.repo_path = "/repo"
    evaluation.resolved_session_id = task.claimed_by_session_id
    evaluation.edit_session_id = task.claimed_by_session_id
    evaluation.is_epic = True
    evaluation.skip_leaf_checks = True
    evaluation.fingerprint = CloseEvaluationFingerprint.capture(
        task,
        children_state=(),
        attribution=None,
    )
    evaluation.scope_snapshot = ((), (), ())
    evaluation.pass_gate(11, "criteria_review", "Passed.")

    with (
        patch.object(close_finalization, "resolve_close_commit_shas", return_value=([], None)),
        patch.object(close_finalization, "link_close_commit_shas", return_value=(task, None)),
        patch.object(
            close_finalization,
            "notify_parent_on_task_state_change",
            _record_notify_threads(notify_threads),
        ),
        patch.object(close_finalization, "_cleanup_closed_claim", recorder("cleanup_claim")),
        patch(
            "gobby.hooks.event_handlers._plan.on_epic_terminal",
            recorder("on_epic_terminal"),
        ),
    ):
        result = await _commit_close(
            ctx,
            evaluation,
            changes_summary="Implemented the task changes.",
            reason="completed",
            skip_validation=False,
            override_justification=None,
            commit_sha=None,
        )

    assert result["closed"] is True
    assert result["closed_ancestors"] == [
        {"id": ancestor.id, "ref": "#4241", "title": "Ancestor epic"}
    ]
    loop_thread = threading.get_ident()
    for name, idents in threads.items():
        assert idents, f"{name} must run"
        assert loop_thread not in idents, f"{name} ran on the event loop thread"
    # The notifier is not a storage call -- it schedules a coroutine, which
    # needs the loop it is scheduling onto, so it belongs on this thread.
    assert notify_threads == [loop_thread, loop_thread]


def _record_notify_threads(sink: list[int]) -> Callable[..., None]:
    def record(*_args: object, **_kwargs: object) -> None:
        sink.append(threading.get_ident())

    return record


def _epic_close_evaluation(task: Task) -> CloseEvaluation:
    evaluation = CloseEvaluation(task.id)
    evaluation.task = task
    evaluation.task_id = task.id
    evaluation.repo_path = "/repo"
    evaluation.resolved_session_id = task.claimed_by_session_id
    evaluation.edit_session_id = task.claimed_by_session_id
    evaluation.is_epic = True
    evaluation.skip_leaf_checks = True
    evaluation.fingerprint = CloseEvaluationFingerprint.capture(
        task, children_state=(), attribution=None
    )
    evaluation.scope_snapshot = ((), (), ())
    evaluation.pass_gate(11, "criteria_review", "Passed.")
    return evaluation


@pytest.mark.asyncio
async def test_closing_schedules_the_parent_and_ancestor_broadcasts() -> None:
    """The real notifier must reach a loop it can schedule the broadcast on.

    Off the loop, resolve_background_loop returns None, schedule_background_task
    raises RuntimeError, and the notifier swallows it -- so stubbing the
    notifier would hide exactly the defect (#20871). It runs for real here, and
    the loop's own task factory records what it scheduled: the registry is no
    good for this, because a finished notification pops itself out of it.
    """
    task = replace(_task(), task_type="epic", seq_num=4242)
    ancestor = replace(task, id="00000000-0000-4000-8000-000000000414", seq_num=4241)
    ctx = _ctx(task)
    manager = cast(MagicMock, ctx.task_manager)
    manager.get_task.side_effect = lambda task_id, *_a, **_k: (
        ancestor if task_id == ancestor.id else task
    )
    manager.list_tasks.return_value = []

    def record_close(*_args: object, **kwargs: object) -> None:
        closed = kwargs.get("closed_ancestors")
        if isinstance(closed, list):
            closed.append(ancestor.id)

    manager.close_task.side_effect = record_close

    scheduled: list[str] = []
    loop = asyncio.get_running_loop()
    previous_factory = loop.get_task_factory()

    def factory(target_loop: Any, coro: Any, **kwargs: Any) -> asyncio.Task[None]:
        created: asyncio.Task[None] = asyncio.Task(coro, loop=target_loop, **kwargs)
        if created.get_name().startswith("gobby-parent-notification-"):
            scheduled.append(created.get_name())
        return created

    loop.set_task_factory(cast(Any, factory))
    try:
        with (
            patch.object(close_finalization, "resolve_close_commit_shas", return_value=([], None)),
            patch.object(close_finalization, "link_close_commit_shas", return_value=(task, None)),
            patch.object(close_finalization, "_cleanup_closed_claim", lambda *a, **k: None),
            patch("gobby.hooks.event_handlers._plan.on_epic_terminal", lambda *a, **k: None),
        ):
            result = await _commit_close(
                ctx,
                _epic_close_evaluation(task),
                changes_summary="Implemented the task changes.",
                reason="completed",
                skip_validation=False,
                override_justification=None,
                commit_sha=None,
            )
    finally:
        loop.set_task_factory(cast(Any, previous_factory))
        for pending in list(notifications.values()):
            pending.cancel()
        await asyncio.gather(*notifications.values(), return_exceptions=True)
        notifications.clear()

    assert result["closed"] is True
    # Ancestors are described and notified before the task's own broadcast.
    assert scheduled == [
        f"gobby-parent-notification-{ancestor.id}-closed",
        f"gobby-parent-notification-{task.id}-closed",
    ]


@pytest.mark.asyncio
async def test_a_child_created_during_the_close_window_asks_for_a_retry() -> None:
    task = replace(_task(), task_type="epic", seq_num=4242)
    ctx = _ctx(task)
    manager = cast(MagicMock, ctx.task_manager)
    manager.list_tasks.return_value = []
    manager.close_task.side_effect = TaskHasOpenChildrenError(
        task.id, ["00000000-0000-4000-8000-000000000415 (Late child)"]
    )

    with (
        patch.object(close_finalization, "resolve_close_commit_shas", return_value=([], None)),
        patch.object(close_finalization, "link_close_commit_shas", return_value=(task, None)),
    ):
        result = await _commit_close(
            ctx,
            _epic_close_evaluation(task),
            changes_summary="Implemented the task changes.",
            reason="completed",
            skip_validation=False,
            override_justification=None,
            commit_sha=None,
        )

    assert result.get("error") == "stale_task_state"
    assert result.get("stale_state") is True
    assert "Late child" in str(result.get("message"))


_QA_SESSION = "00000000-0000-4000-8000-000000000301"


@pytest.mark.asyncio
async def test_worked_leaf_with_a_closed_child_passes_the_commit_recheck() -> None:
    """The commit recheck classifies a worked leaf the way the evaluation did.

    #20728 carried a linked commit and closed found-work children: the
    evaluation captured its attribution, the recheck recomputed the task as a
    structural parent with no attribution, and every close looped on
    stale_task_state (#21093).
    """
    task = replace(_task(), claimed_by_session_id=None, commits=["abc123"])
    ctx = _ctx(task)
    cast(MagicMock, ctx.task_manager.list_tasks).return_value = [_closed_child_of(task)]
    _children, state = close_finalization.children_state(ctx, task.id)
    attribution = await close_finalization.capture_attribution(
        ctx,
        task=task,
        task_id=task.id,
        resolved_session_id=_QA_SESSION,
        repo_path="/repo",
    )
    evaluation = CloseEvaluation(task.id)
    evaluation.task = task
    evaluation.task_id = task.id
    evaluation.repo_path = "/repo"
    evaluation.resolved_session_id = _QA_SESSION
    evaluation.commit_shas = ["before"]
    evaluation.fingerprint = CloseEvaluationFingerprint.capture(
        task, children_state=state, attribution=attribution
    )
    evaluation.pass_gate(11, "criteria_review", "Passed.")

    with patch.object(
        close_finalization,
        "resolve_close_commit_shas",
        return_value=(["after"], None),
    ):
        result = await _commit_close(
            ctx,
            evaluation,
            changes_summary="Implemented the task changes.",
            reason="completed",
            skip_validation=False,
            override_justification=None,
            commit_sha=None,
        )

    # The gate-input fingerprint matched; the recheck stopped at the patched
    # commit-set comparison instead.
    assert result["error"] == "stale_task_state"
    assert result["message"].startswith("The prospective commit set changed")
    assert "changed_gate_inputs" not in result


@pytest.mark.asyncio
async def test_gate_input_change_names_the_changed_fields() -> None:
    task = _task()
    ctx = _ctx(task)
    evaluation = CloseEvaluation(task.id)
    evaluation.task = task
    evaluation.task_id = task.id
    evaluation.repo_path = "/repo"
    evaluation.resolved_session_id = _QA_SESSION
    evaluation.commit_shas = ["before"]
    evaluation.fingerprint = CloseEvaluationFingerprint.capture(
        replace(task, validation_criteria="Old criteria."),
        children_state=(),
        attribution=None,
    )

    result = await _commit_close(
        ctx,
        evaluation,
        changes_summary="Implemented the task changes.",
        reason="completed",
        skip_validation=False,
        override_justification=None,
        commit_sha=None,
    )

    assert result["error"] == "stale_task_state"
    assert result["changed_gate_inputs"] == ["validation_criteria", "attribution"]
    assert "(validation_criteria, attribution)" in result["message"]
    cast(MagicMock, ctx.task_manager.close_task).assert_not_called()


@pytest.mark.parametrize(
    ("task_kwargs", "has_children", "expected"),
    [
        pytest.param({"task_type": "epic"}, False, True, id="epic"),
        pytest.param(
            {"claimed_by_session_id": None, "commits": None}, True, True, id="unworked-parent"
        ),
        pytest.param(
            {"claimed_by_session_id": None, "commits": ["abc123"]},
            True,
            False,
            id="linked-commit-leaf",
        ),
        pytest.param({"commits": None}, True, False, id="claimed-leaf"),
        pytest.param(
            {"claimed_by_session_id": None, "commits": None}, False, False, id="childless"
        ),
    ],
)
def test_closes_as_structural_parent(
    task_kwargs: dict[str, Any], has_children: bool, expected: bool
) -> None:
    task = replace(_task(), **task_kwargs)

    assert closes_as_structural_parent(task, has_children=has_children) is expected


def test_fingerprint_differences_name_nested_attribution_fields() -> None:
    task = _task()
    before = CloseAttributionSnapshot(
        owner_session_id=_QA_SESSION,
        attributed=True,
        raw_paths=frozenset({"a.py"}),
        edited_paths=frozenset({"a.py"}),
        clean_proof_paths=frozenset({"a.py"}),
        had_attributed_edits=True,
        claim_started_at=None,
    )
    after = replace(before, raw_paths=frozenset(), edited_paths=frozenset())
    expected = CloseEvaluationFingerprint.capture(task, children_state=(), attribution=before)
    fresh = CloseEvaluationFingerprint.capture(task, children_state=(), attribution=after)

    assert fingerprint_differences(expected, fresh) == [
        "attribution.raw_paths",
        "attribution.edited_paths",
    ]
    assert fingerprint_differences(None, fresh) == ["evaluation"]


@pytest.mark.asyncio
async def test_tagged_commit_landing_after_evaluation_returns_stale_without_close() -> None:
    task = _task()
    ctx = _ctx(task)
    evaluation = _ready_evaluation(task)

    with patch.object(
        close_finalization,
        "unlinked_tagged_commits",
        return_value=((["abc999"], []), None),
    ):
        result = await _commit_close(
            ctx,
            evaluation,
            changes_summary="Implemented the task changes.",
            reason="completed",
            skip_validation=False,
            override_justification=None,
            commit_sha=None,
        )

    assert result["error"] == "stale_task_state"
    assert "not linked" in result["message"]
    cast(MagicMock, ctx.task_manager.close_task).assert_not_called()


def _memory_review_close_context(
    monkeypatch: pytest.MonkeyPatch,
    *,
    spawned: bool = False,
) -> tuple[RegistryContext, CloseEvaluation, MagicMock]:
    task = _task()
    ctx = _ctx(task)
    evaluation = _ready_evaluation(task)
    evaluation.edit_session_id = task.claimed_by_session_id
    evaluation.commit_shas = ["abc1234"]
    root = SimpleNamespace(id="root", parent_session_id=None, agent_run_id=None, agent_depth=0)
    parent = SimpleNamespace(
        id="interactive", parent_session_id="root", agent_run_id=None, agent_depth=0
    )
    child = SimpleNamespace(
        id=task.claimed_by_session_id,
        parent_session_id="interactive" if spawned else None,
        agent_run_id="worker" if spawned else None,
        agent_depth=1 if spawned else 0,
    )
    sessions = {item.id: item for item in (root, parent, child)}
    ctx.session_manager = MagicMock()
    ctx.session_manager.get.side_effect = sessions.get
    manager = cast(MagicMock, ctx.task_manager)

    def close_task(*_args: object, **_kwargs: object) -> Task:
        closed = replace(
            task,
            claimed_by_session_id=None,
            closed_at=datetime(2026, 9, 13, tzinfo=UTC),
            closed_reason="completed",
            commits=["abc1234"],
        )
        manager.get_task.return_value = closed
        return closed

    manager.close_task.side_effect = close_task
    monkeypatch.setattr(
        close_finalization,
        "capture_attribution",
        AsyncMock(
            return_value=CloseAttributionSnapshot(
                owner_session_id=task.claimed_by_session_id or "",
                attributed=False,
                raw_paths=frozenset(),
                edited_paths=frozenset(),
                clean_proof_paths=frozenset(),
                had_attributed_edits=False,
                claim_started_at=None,
            )
        ),
    )
    monkeypatch.setattr(
        close_finalization, "resolve_close_commit_shas", AsyncMock(return_value=(["abc1234"], None))
    )
    monkeypatch.setattr(
        close_finalization, "link_close_commit_shas", MagicMock(return_value=(task, None))
    )
    monkeypatch.setattr(close_finalization, "notify_parent_on_task_state_change", MagicMock())
    monkeypatch.setattr(close_finalization, "_cleanup_closed_claim", MagicMock())
    queue = MagicMock()
    monkeypatch.setattr(SessionVariableManager, "queue_memory_review", queue)
    return ctx, evaluation, queue


@pytest.mark.asyncio
async def test_commit_close_queues_review_on_interactive_claimer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx, evaluation, queue = _memory_review_close_context(monkeypatch)
    result = await _commit_close(
        ctx,
        evaluation,
        reason="completed",
        changes_summary="Fixed routing.",
        skip_validation=False,
        override_justification=None,
        commit_sha="abc1234",
    )
    assert result["closed"] is True
    queue.assert_called_once_with(
        evaluation.edit_session_id,
        {
            "closure_id": f"{evaluation.task_id}:2026-09-13T00:00:00+00:00",
            "task_id": evaluation.task_id,
            "task_ref": evaluation.task_id,
            "changes_summary": "Fixed routing.",
        },
    )


@pytest.mark.asyncio
async def test_commit_close_queues_review_on_nearest_interactive_ancestor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx, evaluation, queue = _memory_review_close_context(monkeypatch, spawned=True)
    result = await _commit_close(
        ctx,
        evaluation,
        reason="completed",
        changes_summary="Fixed routing.",
        skip_validation=False,
        override_justification=None,
        commit_sha="abc1234",
    )
    assert result["closed"] is True
    assert queue.call_count == 1
    assert queue.call_args.args[0] == "interactive"
    assert (
        queue.call_args.args[1]["closure_id"] == f"{evaluation.task_id}:2026-09-13T00:00:00+00:00"
    )


@pytest.mark.asyncio
async def test_validator_close_queues_memory_review(monkeypatch: pytest.MonkeyPatch) -> None:
    from gobby.mcp_proxy.tools.tasks import _lifecycle_close_orchestration as orchestration
    from tests.mcp_proxy.tools.tasks.test_lifecycle_close_orchestration import (
        _authenticate,
        _review,
        _Store,
        _verdict,
    )

    ctx, evaluation, queue = _memory_review_close_context(monkeypatch, spawned=True)
    review = _review(status="running", run_id="run")
    review = replace(
        review,
        task_id=evaluation.task_id or "",
        close_arguments={
            **review.close_arguments,
            "changes_summary": "Persisted validator summary.",
            "commit_sha": "abc1234",
        },
    )
    store = _Store(review)
    _authenticate(monkeypatch, review)
    monkeypatch.setattr(orchestration, "TaskCloseReviewStore", lambda _db: store)
    result = await orchestration.submit_close_review(
        ctx,
        review_id="review",
        verdict=_verdict("valid"),
        evaluate_close=AsyncMock(return_value=evaluation),
        commit_close=_commit_close,
    )
    assert result["review_status"] == "closed"
    assert queue.call_count == 1
    assert queue.call_args.args[0] == "interactive"
    assert queue.call_args.args[1]["changes_summary"] == "Persisted validator summary."


@pytest.mark.asyncio
async def test_memory_review_queue_failure_preserves_committed_close(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    ctx, evaluation, queue = _memory_review_close_context(monkeypatch)
    queue.side_effect = RuntimeError("queue unavailable")
    result = await _commit_close(
        ctx,
        evaluation,
        reason="completed",
        changes_summary="Fixed routing.",
        skip_validation=False,
        override_justification=None,
        commit_sha="abc1234",
    )
    assert result["closed"] is True
    assert "Failed to queue memory review" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(("agent_run_id", "agent_depth"), [("worker", 0), (None, 1)])
async def test_memory_review_skips_lineage_without_interactive_session(
    monkeypatch: pytest.MonkeyPatch,
    agent_run_id: str | None,
    agent_depth: int,
) -> None:
    ctx, evaluation, queue = _memory_review_close_context(monkeypatch, spawned=True)
    assert evaluation.edit_session_id is not None
    session = ctx.session_manager.get(evaluation.edit_session_id)
    assert session is not None
    session.parent_session_id = None
    session.agent_run_id = agent_run_id
    session.agent_depth = agent_depth
    result = await _commit_close(
        ctx,
        evaluation,
        reason="completed",
        changes_summary="Fixed routing.",
        skip_validation=False,
        override_justification=None,
        commit_sha="abc1234",
    )
    assert result["closed"] is True
    queue.assert_not_called()
