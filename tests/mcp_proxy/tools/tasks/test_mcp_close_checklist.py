"""Close-checklist waiver contracts for a justified deliberate close."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, Literal, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import gobby.mcp_proxy.tools.tasks._lifecycle_close as lifecycle
import gobby.mcp_proxy.tools.tasks._lifecycle_close_finalization as close_finalization
import gobby.mcp_proxy.tools.tasks._lifecycle_validation as lifecycle_validation
from gobby.mcp_proxy.tools.task_repo_paths import CloseWorktreeRoot
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._lifecycle_close import _evaluate_close
from gobby.mcp_proxy.tools.tasks._lifecycle_close_preview import CloseEvaluation
from gobby.mcp_proxy.tools.tasks._lifecycle_validation import ValidationResult
from gobby.mcp_proxy.tools.tasks._task_scope import TaskScopeEvaluation
from gobby.storage.tasks import Task
from gobby.tasks.acceptance_artifacts import AcceptanceArtifactResult, AcceptanceTest
from gobby.tasks.close_checklist import CloseGateResult, evaluate_validation_commands
from gobby.tasks.transcript_evidence import (
    TranscriptEdit,
    TranscriptEvidence,
    TranscriptValidationRun,
    TranscriptValidationSegment,
)

pytestmark = pytest.mark.unit

SESSION_ID = "00000000-0000-4000-8000-000000000301"
MACHINE_ID = "21000000-0000-4000-8000-000000000001"
NOW = datetime(2026, 8, 23, 12, 5, tzinfo=UTC)
WORKTREE = "/worktrees/wt-101"
NO_WORKTREE = CloseWorktreeRoot(None, None, "the task has no registered isolation worktree")
NAMED_TEST = AcceptanceTest(
    reference="tests/memory/test_recall.py::test_batched_read_failure_injects_nothing",
    path="tests/memory/test_recall.py",
    symbol="test_batched_read_failure_injects_nothing",
    body="async def test_batched_read_failure_injects_nothing() -> None:\n    assert True\n",
)


@pytest.fixture(autouse=True)
def _committed_manifest_is_current() -> Iterator[None]:
    with patch.object(lifecycle, "check_linked_committed_bundled_manifest", return_value=None):
        yield


def _task(
    *,
    escalated: bool,
    tdd_required: bool = False,
    validation_criteria: str | None = None,
) -> Task:
    task = Task(
        id="00000000-0000-4000-8000-000000000101",
        project_id="00000000-0000-4000-8000-000000000201",
        title="Close checklist leaf",
        category="code",
        priority=2,
        task_type="task",
        created_at=datetime(2026, 8, 23, 12, tzinfo=UTC),
        updated_at=datetime(2026, 8, 23, 12, tzinfo=UTC),
        claimed_by_session_id=SESSION_ID,
        validation_criteria=validation_criteria
        or (
            "Recall injects nothing when the batched read raises.\n"
            "Acceptance artifacts:\n"
            f"- test: `{NAMED_TEST.reference}`"
        ),
        validation_fail_count=0,
        labels=["tdd:required"] if tdd_required else [],
        stages=({"stage_name": "development", "position": 0, "state": "in_progress"},),
    )
    if not escalated:
        return task
    return replace(task, escalated_at=NOW, escalation_reason="Needs a human decision.")


def _ctx(task: Task) -> RegistryContext:
    manager = MagicMock()
    manager.db = MagicMock()
    manager.get_task.return_value = task
    manager.list_tasks.return_value = []
    close_session = SimpleNamespace(id=SESSION_ID, machine_id=MACHINE_ID)
    return cast(
        RegistryContext,
        SimpleNamespace(
            task_manager=manager,
            task_validator=object(),
            project_manager=MagicMock(),
            session_manager=SimpleNamespace(get=lambda _session_id: close_session),
            session_var_manager=SimpleNamespace(get_variables=lambda _session_id: {}),
            validation_config=None,
            resolve_session_id=lambda session_id: session_id,
            get_current_project_name=lambda: "gobby",
        ),
    )


def _transcript() -> TranscriptEvidence:
    """A clean test run with no red, so gate 12 has nothing to accept."""
    return TranscriptEvidence(
        validation_runs=(
            TranscriptValidationRun(
                session_id=SESSION_ID,
                source="claude",
                command="uv run pytest tests/memory/test_recall.py -q",
                categories=("test",),
                matcher_id="pytest",
                label="pytest",
                outcome="success",
                started_at=NOW,
                completed_at=NOW,
                order=1,
                exit_code=0,
            ),
        ),
        sessions=(SESSION_ID,),
    )


def _operational_transcript() -> TranscriptEvidence:
    return TranscriptEvidence(
        validation_runs=(
            *_transcript().validation_runs,
            TranscriptValidationRun(
                session_id=SESSION_ID,
                source="claude",
                command="uv run gobby restart --wait",
                categories=("config",),
                matcher_id="gobby-restart",
                label="gobby restart",
                outcome="success",
                started_at=NOW,
                completed_at=NOW,
                order=2,
                exit_code=0,
            ),
        ),
        sessions=(SESSION_ID,),
    )


def _transcript_with_test_types_audit() -> TranscriptEvidence:
    return TranscriptEvidence(
        validation_runs=(
            *_transcript().validation_runs,
            TranscriptValidationRun(
                session_id=SESSION_ID,
                source="claude",
                command=(
                    "uv run gobby test-types audit tests/ "
                    "--baseline .gobby/test-types-baseline.json --fail-on-new"
                ),
                categories=("type_check",),
                matcher_id="gobby-test-types-audit",
                label="Gobby test-types ratchet",
                outcome="success",
                started_at=NOW,
                completed_at=NOW,
                order=2,
                exit_code=0,
                validation_segments=(
                    TranscriptValidationSegment(
                        command=(
                            "gobby test-types audit tests/ "
                            "--baseline .gobby/test-types-baseline.json --fail-on-new"
                        ),
                        categories=("type_check",),
                    ),
                ),
            ),
        ),
        sessions=(SESSION_ID,),
    )


async def _evaluate(
    task: Task,
    *,
    override_justification: str | None,
    review: AsyncMock | None = None,
    artifacts: AcceptanceArtifactResult | None = None,
    close_root: CloseWorktreeRoot = NO_WORKTREE,
    project_path: str | None = None,
    acceptance_evaluator: MagicMock | None = None,
    transcript: TranscriptEvidence | None = None,
    changes_summary: str = "Implemented and tested.",
    dirty_paths: set[str] | None = None,
    foreign_owner_sessions: dict[str, str] | None = None,
    response_detail: Literal["concise", "diagnostic"] = "diagnostic",
    reason: str = "completed",
    has_edits: bool = True,
    transcript_deriver: AsyncMock | None = None,
    linked_paths: set[str] | None = None,
) -> CloseEvaluation:
    review = review or AsyncMock(
        return_value=ValidationResult(
            can_close=True,
            validation_status="valid",
            validation_feedback="Criteria satisfied.",
            reset_reason="llm_valid",
            extra={"verdict": {"status": "valid"}},
        )
    )
    artifacts = artifacts or AcceptanceArtifactResult(
        passed=True,
        tests=(NAMED_TEST,),
        findings=(),
        evidence_files=(),
    )
    attributed_paths = (dirty_paths or {"src/a.py"}) if has_edits else set()
    foreign_owners = {
        path: (SimpleNamespace(session_ref=session_ref),)
        for path, session_ref in (foreign_owner_sessions or {}).items()
    }
    with (
        patch.object(lifecycle, "resolve_task_id_for_mcp", return_value=task.id),
        patch.object(lifecycle, "resolve_task_repo_path", return_value="/repo"),
        patch.object(lifecycle, "resolve_close_worktree_root", return_value=close_root),
        patch.object(close_finalization, "_claimed_session_window_start", return_value=None),
        patch.object(
            close_finalization,
            "_committable_task_paths",
            return_value=attributed_paths,
        ),
        patch.object(lifecycle, "_task_dirty_paths", return_value=dirty_paths or set()),
        patch.object(
            lifecycle_validation,
            "foreign_owned_dirty_paths",
            return_value=foreign_owners,
        ),
        patch.object(lifecycle, "resolve_close_commit_shas", return_value=(["abc123"], None)),
        patch.object(
            lifecycle,
            "validate_commit_requirements",
            return_value=ValidationResult(can_close=True),
        ),
        patch.object(lifecycle, "active_validation_backoff", return_value=None),
        patch.object(
            lifecycle,
            "evaluate_task_scope",
            return_value=TaskScopeEvaluation((), (), ()),
        ),
        patch.object(lifecycle, "collect_commit_paths", return_value=linked_paths or set()),
        patch.object(
            lifecycle,
            "_derive_close_transcript_evidence",
            transcript_deriver or AsyncMock(return_value=transcript or _transcript()),
        ),
        patch.object(
            lifecycle,
            "evaluate_acceptance_artifacts",
            acceptance_evaluator or MagicMock(return_value=artifacts),
        ),
        patch.object(lifecycle, "collect_commit_diff_text", return_value="diff"),
        patch.object(lifecycle, "evaluate_criteria_review", review),
        patch("gobby.workflows.task_claim_state.target_task_has_edits", return_value=has_edits),
        patch(
            "gobby.workflows.task_claim_state.task_edited_file_set",
            return_value=attributed_paths,
        ),
    ):
        return await _evaluate_close(
            _ctx(task),
            task_id=task.id,
            reason=reason,
            changes_summary=changes_summary,
            commit_sha="abc123",
            project_path=project_path,
            response_detail=response_detail,
            override_justification=override_justification,
        )


@pytest.mark.asyncio
async def test_linked_commit_paths_reach_both_validation_evaluations() -> None:
    evaluator = MagicMock(wraps=evaluate_validation_commands)
    with patch.object(lifecycle, "evaluate_validation_commands", evaluator):
        evaluation = await _evaluate(
            _task(escalated=False),
            override_justification=None,
            transcript=_transcript_with_test_types_audit(),
            linked_paths={"tests/deleted.py"},
        )

    assert evaluation.error is None
    assert evaluator.call_count == 2
    assert [call.kwargs["changed_paths"] for call in evaluator.call_args_list] == [
        {"src/a.py", "tests/deleted.py"},
        {"src/a.py", "tests/deleted.py"},
    ]


@pytest.mark.parametrize("response_detail", ["concise", "diagnostic"])
@pytest.mark.asyncio
async def test_uncommitted_task_edits_names_dirty_paths(
    response_detail: Literal["concise", "diagnostic"],
) -> None:
    evaluation = await _evaluate(
        _task(escalated=False),
        override_justification=None,
        dirty_paths={"src/z.py", "src/a.py"},
        foreign_owner_sessions={"src/a.py": "#11380"},
        response_detail=response_detail,
    )

    response = evaluation.response(preview=True)
    assert response["error"] == "uncommitted_task_edits"
    assert response["message"] == (
        "Task-attributed files still have uncommitted changes: "
        "src/a.py (dirty by session #11380), src/z.py. Commit them, or ask the owner "
        "to commit or release_task_paths, and retry."
    )
    if response_detail == "diagnostic":
        gate = next(item for item in response["checklist"] if item["item"] == 9)
        assert gate["details"] == {
            "dirty_paths": ["src/a.py", "src/z.py"],
            "foreign_owner_sessions": {"src/a.py": "#11380", "src/z.py": None},
        }
    else:
        assert "checklist" not in response


@pytest.mark.asyncio
async def test_uncommitted_task_edits_passes_when_clean() -> None:
    evaluation = await _evaluate(
        _task(escalated=False),
        override_justification=None,
        dirty_paths=set(),
    )

    gate = next(item for item in evaluation.gates if item.item == 9)
    assert gate.status == "passed"
    assert gate.message == "No task-attributed files are dirty."
    assert gate.details == {}


@pytest.mark.asyncio
async def test_close_preview_surfaces_uncredited_validation_runs() -> None:
    command = "uv run pytest tests/memory/test_recall.py -q | tail -1"
    transcript = TranscriptEvidence(
        validation_runs=(
            TranscriptValidationRun(
                session_id=SESSION_ID,
                source="claude",
                command=command,
                categories=("test",),
                matcher_id="pytest",
                label="pytest",
                outcome="success",
                started_at=NOW,
                completed_at=NOW,
                order=1,
                exit_code=0,
            ),
        ),
        sessions=(SESSION_ID,),
    )

    evaluation = await _evaluate(
        _task(escalated=False),
        override_justification=None,
        transcript=transcript,
        response_detail="diagnostic",
    )

    response = evaluation.response(preview=True)
    assert response["error"] == "validation_command_required"
    assert response["validation_commands"]["uncredited_runs"] == [
        {"command": command, "reason": "wrapped", "wrapper_reason": "pipeline"}
    ]


@pytest.mark.asyncio
async def test_close_preview_returns_all_explicit_command_gaps_before_review() -> None:
    stale_command = "uv run pytest tests/memory/test_recall.py -q"
    missing_command = "uv run ruff check src/gobby/tasks/close_checklist.py"
    transcript = TranscriptEvidence(
        validation_runs=(
            TranscriptValidationRun(
                session_id=SESSION_ID,
                source="claude",
                command=f"GOBBY_TEST_PROTECT=1 {stale_command}",
                categories=("test",),
                matcher_id="pytest",
                label="pytest",
                outcome="success",
                started_at=NOW,
                completed_at=NOW,
                order=1,
                exit_code=0,
            ),
        ),
        edits=(
            TranscriptEdit(
                session_id=SESSION_ID,
                source="claude",
                path="src/a.py",
                timestamp=NOW + timedelta(seconds=1),
                order=2,
                tool_name="apply_patch",
            ),
        ),
        sessions=(SESSION_ID,),
    )
    review = AsyncMock()

    evaluation = await _evaluate(
        _task(
            escalated=False,
            validation_criteria=f"`{stale_command}` passes. `{missing_command}` passes.",
        ),
        override_justification=None,
        transcript=transcript,
        review=review,
        response_detail="concise",
    )

    response = evaluation.response(preview=True)
    assert response["error"] == "validation_command_required"
    assert response["message"].count("Run `") == 2
    assert stale_command in response["message"]
    assert missing_command in response["message"]
    review.assert_not_awaited()


@pytest.mark.asyncio
async def test_unclaimed_no_work_close_reviews_disposition_without_session_commands() -> None:
    task = replace(_task(escalated=False), claimed_by_session_id=None)
    derive = AsyncMock(return_value=_operational_transcript())
    review = AsyncMock(return_value=ValidationResult(can_close=True, validation_status="valid"))
    with patch("gobby.utils.session_context.get_current_session_id", return_value=SESSION_ID):
        evaluation = await _evaluate(
            task,
            override_justification=None,
            reason="obsolete",
            has_edits=False,
            transcript_deriver=derive,
            review=review,
            changes_summary="This task is obsolete because its requested feature was retired.",
        )
    derive.assert_not_awaited()
    review.assert_awaited_once()
    assert review.await_args is not None
    assert review.await_args.kwargs["reason"] == "obsolete"
    assert evaluation.ready
    assert evaluation.extra["validation_commands"]["latest_runs"] == []


def _unresolved_artifacts() -> AcceptanceArtifactResult:
    """Gate 11 output when gcode finds no such test in the evaluated root."""
    return AcceptanceArtifactResult(
        passed=False,
        tests=(),
        findings=(
            f"{NAMED_TEST.reference}: gcode could not resolve the exact test body: "
            "expected one matching symbol, found 0",
        ),
        evidence_files=(),
    )


@pytest.mark.asyncio
async def test_close_gates_evaluate_the_registered_worktree_root() -> None:
    """A task whose linked commit lives on its worktree branch closes from there.

    Named acceptance tests may exist only on that branch, so gate 11 resolving
    them against the main checkout failed for the wrong reason (#21098).
    """
    acceptance = MagicMock(
        return_value=AcceptanceArtifactResult(
            passed=True, tests=(NAMED_TEST,), findings=(), evidence_files=()
        )
    )

    evaluation = await _evaluate(
        _task(escalated=False),
        override_justification=None,
        close_root=CloseWorktreeRoot(WORKTREE, WORKTREE, None),
        acceptance_evaluator=acceptance,
    )

    assert evaluation.repo_path == WORKTREE
    assert _gate(evaluation, 3).message == (
        f"Task repository resolved to the registered worktree {WORKTREE}."
    )
    assert acceptance.call_args.kwargs["repo_path"] == WORKTREE


@pytest.mark.asyncio
async def test_unresolved_named_test_names_the_registered_worktree_and_project_path() -> None:
    """When the worktree default cannot apply, the diagnostic says why and how to choose."""
    skip = f"registered worktree {WORKTREE} was not used: linked commit abc123 is not reachable"

    evaluation = await _evaluate(
        _task(escalated=False),
        override_justification=None,
        artifacts=_unresolved_artifacts(),
        close_root=CloseWorktreeRoot(WORKTREE, None, skip),
    )

    assert evaluation.error == "acceptance_artifacts_invalid"
    assert evaluation.message == (
        f"{NAMED_TEST.reference}: gcode could not resolve the exact test body: "
        "expected one matching symbol, found 0 "
        f"Named test {NAMED_TEST.reference} did not resolve in /repo; {skip}. "
        "Pass project_path=<registered worktree or clone path> "
        "to evaluate the task branch there."
    )


@pytest.mark.asyncio
async def test_unregistered_worktree_does_not_suggest_a_project_path() -> None:
    """Without a registered worktree there is no path to pass, so do not ask for one.

    The hint used to be appended in the same breath as saying the task has no
    registered isolation worktree, sending the caller hunting for one (#21237).
    """
    evaluation = await _evaluate(
        _task(escalated=False),
        override_justification=None,
        artifacts=_unresolved_artifacts(),
        close_root=NO_WORKTREE,
    )

    assert evaluation.error == "acceptance_artifacts_invalid"
    assert evaluation.message == _unresolved_artifacts().findings[0]


@pytest.mark.asyncio
async def test_explicit_project_path_keeps_the_bare_gate_11_finding() -> None:
    """The caller who chose the root does not get told to choose one."""
    evaluation = await _evaluate(
        _task(escalated=False),
        override_justification=None,
        artifacts=_unresolved_artifacts(),
        project_path="/repo",
    )

    assert evaluation.error == "acceptance_artifacts_invalid"
    assert evaluation.message == _unresolved_artifacts().findings[0]


def _gate(evaluation: CloseEvaluation, item: int) -> CloseGateResult:
    return next(gate for gate in evaluation.gates if gate.item == item)


@pytest.mark.asyncio
async def test_justified_deliberate_close_waives_tdd_evidence() -> None:
    """A human who decided the task closes must be able to close it.

    Gate 12 asks whether the loop was followed, not whether the deliverable is
    sound, so it belongs with gate 13 under the deliberate-close waiver. The
    delivery gates stay hard and are asserted below.
    """
    evaluation = await _evaluate(
        _task(escalated=True),
        override_justification="Red window closed before the assertion-backed reds; work is green.",
    )

    assert evaluation.error is None
    assert evaluation.ready is True
    waived = {gate.item: gate.status for gate in evaluation.gates if gate.item in {12, 13}}
    assert waived == {12: "skipped", 13: "skipped"}
    delivery = {gate.item: gate.status for gate in evaluation.gates if 7 <= gate.item <= 11}
    assert sorted(delivery) == [7, 8, 9, 10, 11], "the delivery gates still ran"
    assert "failed" not in delivery.values()
    assert delivery[10] == "passed", "the clean validation run is still required"


@pytest.mark.asyncio
async def test_escalated_close_without_justification_still_fails_tdd_evidence() -> None:
    """The waiver is the justification, so an escalated task alone does not earn it."""
    evaluation = await _evaluate(
        _task(escalated=True, tdd_required=True),
        override_justification=None,
    )

    assert evaluation.error == "tdd_evidence_missing"
    assert _gate(evaluation, 12).status == "failed"


@pytest.mark.asyncio
async def test_unescalated_close_with_justification_still_fails_tdd_evidence() -> None:
    """An ordinary leaf cannot buy its way past gate 12 by supplying a justification."""
    evaluation = await _evaluate(
        _task(escalated=False, tdd_required=True),
        override_justification="No red evidence, closing anyway.",
    )

    assert evaluation.error == "tdd_evidence_missing"
    assert _gate(evaluation, 12).status == "failed"


@pytest.mark.asyncio
async def test_criteria_review_receives_successful_transcript_operational_actions() -> None:
    review = AsyncMock(
        return_value=ValidationResult(
            can_close=True,
            validation_status="valid",
            validation_feedback="Criteria satisfied.",
            reset_reason="llm_valid",
            extra={"verdict": {"status": "valid"}},
        )
    )

    evaluation = await _evaluate(
        _task(
            escalated=False,
            validation_criteria="Restart the daemon and verify the service is healthy.",
        ),
        override_justification=None,
        review=review,
        transcript=_operational_transcript(),
    )

    assert evaluation.error is None
    await_args = review.await_args
    assert await_args is not None
    facts = cast(dict[str, Any], await_args.kwargs["checklist_facts"])
    assert facts["transcript_operational_actions"] == ["restart:daemon,gobby"]


async def test_21319_criterion_does_not_require_description_operational_evidence() -> None:
    criterion = (
        "Close evidence accepts one assertion-backed cycle across multiple named artifacts only "
        "when every named artifact has later passing coverage, while collection/setup-only "
        "failures remain rejected. test: "
        "`tests/tasks/test_acceptance_artifacts.py::"
        "test_tdd_evidence_accepts_one_cycle_with_multiple_green_artifacts`."
    )
    task = replace(
        _task(escalated=False, validation_criteria=criterion),
        description=(
            "Repair close evidence evaluation so one named acceptance artifact supplies the "
            "task's assertion-backed pre-production failure while every named artifact must "
            "have post-production passing coverage. Implement on the 0.5.0 branch and restart "
            "the daemon."
        ),
    )

    evaluation = await _evaluate(task, override_justification=None)

    assert evaluation.error is None
