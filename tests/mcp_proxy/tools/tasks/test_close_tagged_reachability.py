"""Gate 7 judges candidate and integration history, not unrelated checkout refs."""

import os
import subprocess
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.tasks import _lifecycle_close as lifecycle
from gobby.mcp_proxy.tools.tasks import _lifecycle_close_finalization as finalization
from gobby.mcp_proxy.tools.tasks import _lifecycle_close_preview as preview
from gobby.mcp_proxy.tools.tasks import _lifecycle_validation as validation
from gobby.mcp_proxy.tools.tasks._task_scope import NetCommitPaths, TaskScopeEvaluation
from tests.mcp_proxy.tools.tasks.test_close_task_flow import _ctx, _successful_transcript, _task

pytestmark = pytest.mark.unit


def _git(repo: Path, *args: str) -> str:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_SYSTEM=os.devnull,
        GIT_TEMPLATE_DIR="",
        GIT_AUTHOR_NAME="Tests",
        GIT_AUTHOR_EMAIL="tests@example.invalid",
        GIT_COMMITTER_NAME="Tests",
        GIT_COMMITTER_EMAIL="tests@example.invalid",
        GIT_AUTHOR_DATE="2026-10-17T12:00:00+00:00",
        GIT_COMMITTER_DATE="2026-10-17T12:00:00+00:00",
    )
    return subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgSign=false", *args],
        cwd=repo,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reachable_from,after_preview",
    [
        ("neither", ""),
        ("candidate", ""),
        ("integration", ""),
        ("neither", "land"),
        ("neither", "detach"),
    ],
)
async def test_gate7_tagged_commit_reachability(
    tmp_path: Path, reachable_from: str, after_preview: str
) -> None:
    repo = tmp_path / "main"
    repo.mkdir()
    _git(repo, "init", "-b", "0.5.0")
    _git(repo, "commit", "--allow-empty", "-m", "seed")
    seed = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-b", "candidate")
    if reachable_from == "candidate":
        _git(repo, "commit", "--allow-empty", "-m", "[gobby-#42] unlinked ancestor")
        missing = _git(repo, "rev-parse", "--short", "HEAD")
    _git(repo, "commit", "--allow-empty", "-m", "[gobby-#42] approved candidate")
    candidate = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "0.5.0")
    if reachable_from == "integration":
        _git(repo, "commit", "--allow-empty", "-m", "[gobby-#42] landed task work")
        missing = _git(repo, "rev-parse", "--short", "HEAD")
    side = tmp_path / "side"
    _git(repo, "worktree", "add", "-b", "bounced", str(side), seed)
    _git(side, "commit", "--allow-empty", "-m", "[gobby-#42] bounced side candidate")
    bounced = _git(side, "rev-parse", "--short", "HEAD")

    task = _task()
    task.seq_num = 42
    task.commits = [candidate]
    ctx = _ctx(task)
    transcript = _successful_transcript(task, command="uv run pytest tests/test_feature.py -q")
    with (
        patch.object(lifecycle, "resolve_task_id_for_mcp", return_value=task.id),
        patch.object(lifecycle, "resolve_task_repo_path", return_value=str(side)),
        patch.object(finalization, "_claimed_session_window_start", return_value=None),
        patch.object(finalization, "_linked_commit_paths", return_value=frozenset()),
        patch.object(finalization, "_committable_task_paths", return_value=set()),
        patch.object(validation, "task_dirty_paths_async", return_value=set()),
        patch.object(lifecycle, "resolve_close_commit_shas", return_value=([candidate], None)),
        patch.object(finalization, "resolve_close_commit_shas", return_value=([candidate], None)),
        patch.object(lifecycle, "collect_net_commit_paths", return_value=NetCommitPaths()),
        patch.object(lifecycle, "active_validation_backoff", return_value=None),
        patch.object(
            lifecycle, "evaluate_task_scope", return_value=TaskScopeEvaluation((), (), ())
        ),
        patch.object(lifecycle, "_derive_close_transcript_evidence", return_value=transcript),
        patch("gobby.workflows.task_claim_state.target_task_has_edits", return_value=False),
        patch("gobby.workflows.task_claim_state.task_edited_file_set", return_value=set()),
    ):
        evaluation = await lifecycle._evaluate_close(
            ctx,
            task_id=task.id,
            reason="completed",
            changes_summary="Exact candidate reviewed; bounced side draft excluded by ancestry.",
            commit_sha=candidate,
            project_path=str(side),
            response_detail="diagnostic",
            run_close_review=False,
        )
        if after_preview:
            if after_preview == "land":
                _git(repo, "merge", "--ff-only", "bounced")
            else:
                _git(repo, "checkout", "--detach")
            result = await finalization.commit_close(
                ctx,
                evaluation,
                reason="completed",
                changes_summary="Recheck authoritative integration state after preview.",
                skip_validation=False,
                override_justification=None,
                commit_sha=candidate,
            )
            assert result["closed"] is False
            assert "Task-tagged commits landed after evaluation" in result["message"]
            cast(MagicMock, ctx.task_manager.close_task).assert_not_called()

    gate = next(gate for gate in evaluation.checklist.gates if gate.item == 7)
    assert gate.passed == (reachable_from == "neither")
    response = evaluation.response(preview=True)
    assert gate.details is not None
    assert gate.details["excluded_unreachable_tagged_commit_shas"] == [bounced]
    assert "excluded-unreachable" in gate.message
    if reachable_from != "neither":
        assert response["unlinked_tagged_commit_shas"] == [missing]
        assert evaluation.error == "unlinked_tagged_commits"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["repository", "integration", "candidate", "history"])
async def test_gate7_git_failure_refuses_exclusions(tmp_path: Path, failure: str) -> None:
    repo = tmp_path / "main"
    repo.mkdir()
    _git(repo, "init", "-b", "0.5.0")
    _git(repo, "commit", "--allow-empty", "-m", "seed")
    candidate = _git(repo, "rev-parse", "HEAD")
    task = _task()
    task.seq_num = 42
    if failure == "integration":
        _git(repo, "checkout", "--detach")
    if failure == "candidate":
        candidate = "f" * 40
    with patch(
        "gobby.tasks.commits._task_tagged_git_history",
        side_effect=RuntimeError("Git history unavailable"),
    ) as history:
        divergence, error = await preview.unlinked_tagged_commits(
            _ctx(task).task_manager,
            task=task,
            task_id=task.id,
            commit_shas=[],
            cwd=str(tmp_path if failure == "repository" else repo),
            project_name="gobby",
            candidate_commit_sha=candidate,
        )
    assert divergence == ([], [])
    assert error is not None
    assert error["error"] == "tagged_commit_scan_failed"
    assert history.await_count == (1 if failure == "history" else 0)
