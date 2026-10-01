"""A close candidate is independent of historical commit-link insertion order."""

import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.config.tasks import TaskValidationConfig
from gobby.mcp_proxy.tools.tasks import _lifecycle_close as lifecycle
from gobby.mcp_proxy.tools.tasks import _lifecycle_close_finalization as finalization
from gobby.mcp_proxy.tools.tasks import _lifecycle_close_preview as preview
from gobby.mcp_proxy.tools.tasks import _lifecycle_validation as validation
from gobby.mcp_proxy.tools.tasks._close_evaluation_support import CloseAttributionSnapshot
from gobby.mcp_proxy.tools.tasks._lifecycle_close_preview import resolve_close_commit_shas
from gobby.mcp_proxy.tools.tasks._lifecycle_validation import ValidationResult
from gobby.mcp_proxy.tools.tasks._task_scope import NetCommitPaths, TaskScopeEvaluation
from gobby.tasks.acceptance_artifacts import resolve_acceptance_tests_async
from gobby.tasks.validation import TaskValidator
from tests.mcp_proxy.tools.tasks.test_close_task_flow import (
    _ctx,
    _ready_evaluation,
    _successful_transcript,
    _task,
)

pytestmark = pytest.mark.unit


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def candidate_repo(tmp_path: Path) -> tuple[Path, str, str]:
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "test@example.invalid")
    _git(tmp_path, "config", "user.name", "Candidate regression")
    test_path = tmp_path / "tests/test_feature.py"
    test_path.parent.mkdir()
    test_path.write_text("def test_feature():\n    assert feature() == 'old'\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "old test body")
    old = _git(tmp_path, "rev-parse", "HEAD")
    test_path.write_text("def test_feature():\n    assert feature() == 'reviewed'\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "reviewed test body")
    return tmp_path, old, _git(tmp_path, "rev-parse", "HEAD")


@pytest.mark.asyncio
@pytest.mark.parametrize("stored_short", [False, True])
@pytest.mark.parametrize("requested_short", [False, True])
async def test_already_linked_tip_keeps_complete_link_order(
    candidate_repo: tuple[Path, str, str],
    stored_short: bool,
    requested_short: bool,
) -> None:
    repo, old, tip = candidate_repo
    task = _task()
    linked = [tip[:10], old[:10]] if stored_short else [tip, old]
    task.commits = linked
    commits, error = await resolve_close_commit_shas(
        cast(MagicMock, _ctx(task).task_manager),
        task=task,
        task_id=task.id,
        claim_started_at=None,
        commit_sha=tip[:12] if requested_short else tip,
        cwd=str(repo),
        project_name="gobby",
    )
    assert error is None
    assert commits == linked
    candidate, candidate_error = await preview.select_close_candidate(
        commits, tip[:12] if requested_short else tip, cwd=str(repo)
    )
    assert candidate_error is None
    assert candidate == tip


@pytest.mark.asyncio
async def test_named_acceptance_body_uses_explicit_close_candidate(
    candidate_repo: tuple[Path, str, str],
) -> None:
    repo, old, tip = candidate_repo
    task = _task(criteria="Test: tests/test_feature.py::test_feature")
    task.commits = [tip, old]
    review = AsyncMock(
        return_value=ValidationResult(
            can_close=True,
            validation_status="valid",
            validation_feedback="Approved.",
            extra={"verdict": {"status": "valid"}},
        )
    )
    transcript = _successful_transcript(task, command="uv run pytest tests/test_feature.py -q")
    with (
        patch.object(preview, "normalize_commit_sha", return_value=tip),
        patch.object(lifecycle, "resolve_task_id_for_mcp", return_value=task.id),
        patch.object(lifecycle, "resolve_task_repo_path", return_value=str(repo)),
        patch.object(finalization, "_claimed_session_window_start", return_value=None),
        patch.object(finalization, "_linked_commit_paths", return_value=frozenset()),
        patch.object(finalization, "_committable_task_paths", return_value=set()),
        patch.object(validation, "task_dirty_paths_async", return_value=set()),
        patch.object(lifecycle, "unlinked_tagged_commits", return_value=(([], []), None)),
        patch.object(lifecycle, "collect_net_commit_paths", return_value=NetCommitPaths()),
        patch.object(lifecycle, "active_validation_backoff", return_value=None),
        patch.object(
            lifecycle, "evaluate_task_scope", return_value=TaskScopeEvaluation((), (), ())
        ),
        patch.object(lifecycle, "_derive_close_transcript_evidence", return_value=transcript),
        patch.object(lifecycle, "collect_commit_diff_text", return_value="complete linked patch"),
        patch.object(lifecycle, "evaluate_close_review", review),
        patch("gobby.workflows.task_claim_state.target_task_has_edits", return_value=False),
        patch("gobby.workflows.task_claim_state.task_edited_file_set", return_value=set()),
    ):
        result = await lifecycle._evaluate_close(
            _ctx(task, validator=object()),
            task_id=task.id,
            reason="completed",
            changes_summary="Reviewed exact tip.",
            commit_sha=tip,
            project_path=str(repo),
            response_detail="diagnostic",
        )
    assert result.ready is True
    assert result.commit_shas == [tip, old]
    assert review.await_count == 1
    bodies = review.call_args.kwargs["test_bodies"]
    assert "'reviewed'" in bodies
    assert "'old'" not in bodies


@pytest.mark.asyncio
async def test_missing_named_test_file_names_the_close_candidate(
    candidate_repo: tuple[Path, str, str],
) -> None:
    # The body is read from the close candidate, so the finding must name it:
    # blaming the last linked commit sent #23110's diagnosis to the wrong commit.
    repo, old, tip = candidate_repo
    tests, findings = await resolve_acceptance_tests_async(
        "Test: tests/test_absent.py::test_absent",
        str(repo),
        [tip, old],
        candidate_commit_sha=tip,
    )
    assert tests == ()
    assert findings == (
        "tests/test_absent.py::test_absent: could not resolve the committed test body: "
        f"close candidate {tip[:12]} does not contain tests/test_absent.py",
    )


@pytest.fixture
def quiet_close() -> Iterator[None]:
    with (
        patch.object(finalization, "unlinked_tagged_commits", return_value=(([], []), None)),
        patch.object(
            finalization, "evaluate_task_scope", return_value=TaskScopeEvaluation((), (), ())
        ),
        patch.object(finalization, "notify_parent_on_task_state_change"),
        patch.object(finalization, "_queue_close_memory_review"),
        patch.object(finalization, "_cleanup_closed_claim"),
    ):
        yield


@pytest.mark.asyncio
@pytest.mark.parametrize("candidate_present", [True, False])
async def test_finalization_preserves_candidate_and_refuses_missing_one(
    candidate_repo: tuple[Path, str, str], quiet_close: None, candidate_present: bool
) -> None:
    repo, old, tip = candidate_repo
    task = _task()
    task.commits = [tip[:10], old[:10]]
    ctx = _ctx(task)
    attribution = CloseAttributionSnapshot(
        owner_session_id=task.claimed_by_session_id or "",
        attributed=False,
        raw_paths=frozenset(),
        edited_paths=frozenset(),
        clean_proof_paths=frozenset(),
        had_attributed_edits=False,
        claim_started_at=None,
        used_commit_fallback=True,
    )
    evaluation = _ready_evaluation(task, attribution=attribution)
    evaluation.repo_path = str(repo)
    evaluation.commit_shas = [tip[:10], old[:10]]
    evaluation.candidate_commit_sha = tip if candidate_present else None
    with (
        patch.object(finalization, "capture_attribution", return_value=attribution),
        patch.object(finalization, "link_close_commit_shas", return_value=(task, None)),
    ):
        result = await finalization.commit_close(
            ctx,
            evaluation,
            reason="completed",
            changes_summary="Reviewed exact tip.",
            skip_validation=False,
            override_justification=None,
            commit_sha=tip[:12] if candidate_present else None,
        )
    close = cast(MagicMock, ctx.task_manager.close_task)
    if candidate_present:
        assert result["closed"] is True
        assert close.call_count == 1
        assert close.call_args.kwargs["closed_commit_sha"] == tip
        assert task.commits == [tip[:10], old[:10]]
    else:
        assert result["closed"] is False
        close.assert_not_called()


def test_candidate_only_change_invalidates_the_review_fingerprint() -> None:
    validator = TaskValidator(TaskValidationConfig())
    facts = {"commit_shas": ["a" * 40, "b" * 40], "candidate_commit_sha": "a" * 40}
    before = validator.prepare_task_review(
        title="Candidate",
        changes_summary="Same complete patch.",
        validation_criteria="Focused tests pass.",
        diff_text="Same linked net patch.",
        checklist_facts=facts,
    )
    after = validator.prepare_task_review(
        title="Candidate",
        changes_summary="Same complete patch.",
        validation_criteria="Focused tests pass.",
        diff_text="Same linked net patch.",
        checklist_facts={**facts, "candidate_commit_sha": "b" * 40},
    )
    assert before.diff_sha == after.diff_sha
    assert before.test_bodies_sha == after.test_bodies_sha
    assert before.review_fingerprint != after.review_fingerprint
    assert before.evidence_fingerprint != after.evidence_fingerprint


@pytest.mark.asyncio
async def test_invalid_explicit_candidate_does_not_fall_back_to_head(
    candidate_repo: tuple[Path, str, str],
) -> None:
    repo, old, tip = candidate_repo
    task = _task()
    task.commits = [tip, old]
    commits, error = await resolve_close_commit_shas(
        cast(MagicMock, _ctx(task).task_manager),
        task=task,
        task_id=task.id,
        claim_started_at=None,
        commit_sha="deadbeef" * 5,
        cwd=str(repo),
        project_name="gobby",
    )
    assert commits == [tip, old]
    assert error is not None
    assert error["error"] == "invalid_commit_sha"
