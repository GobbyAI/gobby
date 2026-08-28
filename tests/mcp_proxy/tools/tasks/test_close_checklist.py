"""Close-checklist waiver contracts for a justified deliberate close."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import nullcontext
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import gobby.mcp_proxy.tools.tasks._lifecycle_close as lifecycle
import gobby.mcp_proxy.tools.tasks._lifecycle_close_finalization as close_finalization
from gobby.mcp_proxy.tools.task_repo_paths import CloseWorktreeRoot
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._lifecycle_close import _evaluate_close
from gobby.mcp_proxy.tools.tasks._lifecycle_close_preview import CloseEvaluation
from gobby.mcp_proxy.tools.tasks._lifecycle_validation import ValidationResult
from gobby.storage.tasks import Task
from gobby.tasks.acceptance_artifacts import AcceptanceArtifactResult, AcceptanceTest
from gobby.tasks.close_checklist import CloseGateResult
from gobby.tasks.epic_guards import EpicGuardResult
from gobby.tasks.transcript_evidence import TranscriptEvidence, TranscriptValidationRun

pytestmark = pytest.mark.unit

SESSION_ID = "00000000-0000-4000-8000-000000000301"
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
    return cast(
        RegistryContext,
        SimpleNamespace(
            task_manager=manager,
            task_validator=object(),
            project_manager=MagicMock(),
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


async def _evaluate(
    task: Task,
    *,
    override_justification: str | None,
    review: AsyncMock | None = None,
    guards: EpicGuardResult | None = None,
    artifacts: AcceptanceArtifactResult | None = None,
    close_root: CloseWorktreeRoot = NO_WORKTREE,
    project_path: str | None = None,
    acceptance_evaluator: MagicMock | None = None,
    transcript: TranscriptEvidence | None = None,
    changes_summary: str = "Implemented and tested.",
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
    with (
        patch.object(lifecycle, "evaluate_epic_guards", AsyncMock(return_value=guards))
        if guards is not None
        else nullcontext(),
        patch.object(lifecycle, "resolve_task_id_for_mcp", return_value=task.id),
        patch.object(lifecycle, "resolve_task_repo_path", return_value="/repo"),
        patch.object(lifecycle, "resolve_close_worktree_root", return_value=close_root),
        patch.object(close_finalization, "_claimed_session_window_start", return_value=None),
        patch.object(close_finalization, "_committable_task_paths", return_value={"src/a.py"}),
        patch.object(lifecycle, "_has_committable_edits", return_value=False),
        patch.object(lifecycle, "resolve_close_commit_shas", return_value=(["abc123"], None)),
        patch.object(
            lifecycle,
            "validate_commit_requirements",
            return_value=ValidationResult(can_close=True),
        ),
        patch.object(lifecycle, "active_validation_backoff", return_value=None),
        patch.object(
            lifecycle,
            "_derive_close_transcript_evidence",
            AsyncMock(return_value=transcript or _transcript()),
        ),
        patch.object(
            lifecycle,
            "evaluate_acceptance_artifacts",
            acceptance_evaluator or MagicMock(return_value=artifacts),
        ),
        patch.object(lifecycle, "collect_commit_diff_text", return_value="diff"),
        patch.object(lifecycle, "evaluate_criteria_review", review),
        patch("gobby.workflows.task_claim_state.target_task_has_edits", return_value=True),
        patch(
            "gobby.workflows.task_claim_state.task_edited_file_set",
            return_value={"src/a.py"},
        ),
    ):
        return await _evaluate_close(
            _ctx(task),
            task_id=task.id,
            reason="completed",
            changes_summary=changes_summary,
            commit_sha="abc123",
            project_path=project_path,
            response_detail="diagnostic",
            override_justification=override_justification,
        )


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
    sound, so it belongs with gate 14 under the deliberate-close waiver. The
    delivery gates stay hard and are asserted below.
    """
    evaluation = await _evaluate(
        _task(escalated=True),
        override_justification="Red window closed before the assertion-backed reds; work is green.",
    )

    assert evaluation.error is None
    assert evaluation.ready is True
    waived = {gate.item: gate.status for gate in evaluation.gates if gate.item in {12, 14}}
    assert waived == {12: "skipped", 14: "skipped"}
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
async def test_criteria_review_sees_guard_identity_without_its_stdout() -> None:
    """The facts handed to the criteria review must repeat for an unchanged close.

    checklist_facts feeds the review prompt and both fingerprints, so a fact
    that moves on every attempt makes the memoized verdict unreachable and no
    repeat close can ever be served from it (#20866). The guard runner's stdout
    is exactly that: a fresh pytest duration per run. Gate 13's own details
    keep it for diagnostics, because a guard that fails never reaches gate 14.
    """
    review = AsyncMock(
        return_value=ValidationResult(
            can_close=True,
            validation_status="valid",
            validation_feedback="Criteria satisfied.",
            reset_reason="llm_valid",
            extra={"verdict": {"status": "valid"}},
        )
    )
    guards = EpicGuardResult(
        passed=True,
        skipped=False,
        error_type=None,
        message="Epic guards passed.",
        paths=("tests/memory/test_recall.py",),
        source_task_ids=("00000000-0000-4000-8000-000000000102",),
        command="uv run pytest 'tests/memory/test_recall.py'",
        output="4 passed in 3.71s\n",
        fingerprint="guardfingerprint",
    )

    evaluation = await _evaluate(
        _task(escalated=False),
        override_justification=None,
        review=review,
        guards=guards,
        # No named acceptance test, so gate 12 has nothing to demand and the
        # evaluation reaches gate 14, which is what this test is about.
        artifacts=AcceptanceArtifactResult(
            passed=True,
            tests=(),
            findings=(),
            evidence_files=(),
        ),
    )

    assert evaluation.error is None
    await_args = review.await_args
    assert await_args is not None, "the criteria review must have run"
    facts = cast(dict[str, Any], await_args.kwargs["checklist_facts"])
    guard_facts = cast(dict[str, Any], facts["epic_guards"])
    guard_output = guards.output
    assert guard_output is not None
    assert "output" not in guard_facts
    assert guard_output not in json.dumps(facts, default=str)
    assert guard_facts["paths"] == ["tests/memory/test_recall.py"]
    assert guard_facts["fingerprint"] == "guardfingerprint"
    assert guard_facts["command"] == "uv run pytest 'tests/memory/test_recall.py'"
    gate_details = _gate(evaluation, 13).details
    assert gate_details["output"] == guards.output, "gate 13 keeps the runner output"


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
