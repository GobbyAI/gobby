"""Candidate resolution preserves links and refuses invalid explicit commits."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import gobby.mcp_proxy.tools.tasks._lifecycle_close as lifecycle
from gobby.mcp_proxy.tools.tasks._close_context import CloseAttributionSnapshot
from gobby.mcp_proxy.tools.tasks._lifecycle_close_preview import CloseEvaluation
from gobby.utils.machine_id import require_machine_id
from tests.mcp_proxy.tools.tasks.test_close_task_flow import _ctx, _task

pytestmark = pytest.mark.unit


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def candidate_repo(tmp_path: Path) -> tuple[Path, str, str]:
    _git(tmp_path, "init", "-b", "0.5.0")
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


async def _evaluate(repo: Path, linked: list[str], requested: str | None) -> CloseEvaluation:
    task = _task()
    task.commits = linked
    ctx = _ctx(task)
    with (
        patch.object(lifecycle, "resolve_task_id_for_mcp", return_value=task.id),
        patch.object(ctx, "resolve_session_id", return_value="session", create=True),
        patch.object(
            ctx.session_manager,
            "get",
            return_value=SimpleNamespace(id="session", machine_id=require_machine_id()),
        ),
        patch.object(lifecycle, "resolve_task_repo_path", return_value=str(repo)),
        patch.object(
            lifecycle,
            "capture_attribution",
            AsyncMock(return_value=CloseAttributionSnapshot({}, True)),
        ),
        patch.object(lifecycle, "main_checkout_target", AsyncMock(return_value=(repo, "0.5.0"))),
    ):
        return await lifecycle._evaluate_close(
            ctx,
            task_id=task.id,
            reason="completed",
            commit_sha=requested,
            project_path=str(repo),
            closing_session_id="session",
            response_detail="diagnostic",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("stored_short", [False, True])
@pytest.mark.parametrize("requested_short", [False, True])
async def test_already_linked_tip_keeps_complete_link_order(
    candidate_repo: tuple[Path, str, str], stored_short: bool, requested_short: bool
) -> None:
    repo, old, tip = candidate_repo
    linked = [tip[:10], old[:10]] if stored_short else [tip, old]
    evaluation = await _evaluate(repo, linked, tip[:12] if requested_short else tip)
    assert evaluation.ready, evaluation.response(preview=True)
    assert evaluation.commit_shas == linked
    assert evaluation.candidate_commit_sha == tip


@pytest.mark.asyncio
async def test_explicit_candidate_is_appended_once(candidate_repo: tuple[Path, str, str]) -> None:
    repo, old, tip = candidate_repo
    evaluation = await _evaluate(repo, [old, old], tip[:12])
    assert evaluation.ready
    assert evaluation.commit_shas == [old, tip]
    assert evaluation.candidate_commit_sha == tip


@pytest.mark.asyncio
@pytest.mark.parametrize("requested", ["", "missing-commit", "HEAD:tests/test_feature.py"])
async def test_invalid_explicit_candidate_does_not_fall_back_to_head(
    candidate_repo: tuple[Path, str, str], requested: str
) -> None:
    repo, old, _tip = candidate_repo
    evaluation = await _evaluate(repo, [old], requested)
    assert not evaluation.ready
    assert evaluation.error == "invalid_commit_sha"
    assert evaluation.candidate_commit_sha is None


@pytest.mark.asyncio
async def test_missing_link_is_not_hidden_by_a_valid_explicit_candidate(
    candidate_repo: tuple[Path, str, str],
) -> None:
    repo, _old, tip = candidate_repo
    evaluation = await _evaluate(repo, ["missing-commit"], tip)
    assert not evaluation.ready
    assert evaluation.commit_shas == ["missing-commit", tip]
    assert evaluation.checklist.gates[3].details["unresolved_commits"] == ["missing-commit"]
