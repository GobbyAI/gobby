"""Checkout attribution and linked-path fallback for close cleanliness checks."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from gobby.mcp_proxy.tools.tasks import _close_context as close_context
from gobby.mcp_proxy.tools.tasks._close_context import capture_attribution
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.storage.tasks import Task

pytestmark = pytest.mark.unit

TASK_ID = "11111111-2222-4333-8444-555555555555"
PROJECT_ID = "11111111-1111-4111-8111-111111110001"
OWNER_SESSION_ID = "owner-session"
COMMITTED_PATHS = frozenset({"src/gobby/memory/recall.py", "tests/memory/test_recall.py"})
IGNORED_SCRATCH = ".gobby/evidence/scratch.md"


@pytest.fixture
def repo_with_task_commit(tmp_path: Path) -> tuple[str, str]:
    """Return a repository path and the sha of a commit touching `COMMITTED_PATHS`."""

    def git(*args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    git("init", "-q")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test User")
    for relative in sorted(COMMITTED_PATHS):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("VALUE = 1\n")
        git("add", relative)
    git("commit", "-qm", "task work")
    return str(tmp_path), git("rev-parse", "HEAD")


def _task(*, commits: list[str] | None) -> Task:
    return cast(
        Task,
        SimpleNamespace(
            id=TASK_ID,
            seq_num=20766,
            project_id=PROJECT_ID,
            commits=commits,
            claimed_by_session_id=OWNER_SESSION_ID,
            is_closed=False,
            closed_at=None,
        ),
    )


@pytest.fixture(autouse=True)
def _no_foreign_owners(monkeypatch: pytest.MonkeyPatch) -> None:
    """Commit-fallback paths consult live ownership rows; none exist in these tests."""

    async def _no_owners(*_args: object, **_kwargs: object) -> set[str]:
        return set()

    monkeypatch.setattr(close_context, "foreign_owned_dirty_paths_async", _no_owners)


def _ctx(variables: dict[str, Any]) -> RegistryContext:
    return cast(
        RegistryContext,
        SimpleNamespace(
            task_manager=SimpleNamespace(db=None),
            session_var_manager=SimpleNamespace(get_variables=lambda _session: variables),
            session_task_manager=SimpleNamespace(get_task_sessions=lambda _task_id: []),
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("linked", [True, False])
async def test_linked_and_prospective_commits_fill_lost_attribution(
    repo_with_task_commit: tuple[str, str], linked: bool
) -> None:
    repo_path, commit_sha = repo_with_task_commit
    snapshot = await capture_attribution(
        _ctx({}),
        task=_task(commits=[commit_sha] if linked else None),
        resolved_session_id="closing-session",
        repo_path=repo_path,
        prospective_commit_shas=(commit_sha,),
    )
    assert snapshot.complete
    assert snapshot.clean_checkouts == {repo_path: COMMITTED_PATHS}


@pytest.mark.asyncio
async def test_checkout_attribution_wins_and_commitless_task_stays_empty(
    repo_with_task_commit: tuple[str, str],
) -> None:
    repo_path, commit_sha = repo_with_task_commit
    path = "src/gobby/memory/recall.py"
    live = await capture_attribution(
        _ctx(
            {
                "task_edited_files": {TASK_ID: [path]},
                "task_edited_file_checkouts": {TASK_ID: {repo_path: [path]}},
            }
        ),
        task=_task(commits=[commit_sha]),
        resolved_session_id="closing-session",
        repo_path=repo_path,
        prospective_commit_shas=(commit_sha,),
    )
    assert live.complete
    assert live.clean_checkouts == {repo_path: frozenset({path})}
    no_edit = await capture_attribution(
        _ctx({}),
        task=_task(commits=None),
        resolved_session_id="closing-session",
        repo_path=repo_path,
        prospective_commit_shas=(),
    )
    assert no_edit.complete
    assert no_edit.clean_checkouts == {}


@pytest.mark.asyncio
async def test_unscoped_ledger_is_not_replaced_by_commit_fallback(
    repo_with_task_commit: tuple[str, str],
) -> None:
    repo_path, commit_sha = repo_with_task_commit
    snapshot = await capture_attribution(
        _ctx({"task_edited_files": {TASK_ID: ["unscoped.py"]}}),
        task=_task(commits=[commit_sha]),
        resolved_session_id="closing-session",
        repo_path=repo_path,
        prospective_commit_shas=(commit_sha,),
    )
    assert not snapshot.complete
    assert snapshot.clean_checkouts == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ledger_paths,expected_paths",
    [
        ((IGNORED_SCRATCH,), frozenset()),
        (
            (IGNORED_SCRATCH, "src/gobby/memory/recall.py"),
            frozenset({"src/gobby/memory/recall.py"}),
        ),
        (("src/gobby/memory/recall.py",), frozenset({"src/gobby/memory/recall.py"})),
    ],
)
async def test_ignored_checkout_ledger_paths_are_not_committable(
    repo_with_task_commit: tuple[str, str],
    ledger_paths: tuple[str, ...],
    expected_paths: frozenset[str],
) -> None:
    repo_path, commit_sha = repo_with_task_commit
    root = Path(repo_path)
    (root / ".gitignore").write_text(".gobby/evidence/\n", encoding="utf-8")
    scratch = root / IGNORED_SCRATCH
    scratch.parent.mkdir(parents=True)
    scratch.write_text("temporary evidence\n", encoding="utf-8")
    snapshot = await capture_attribution(
        _ctx(
            {
                "task_edited_files": {TASK_ID: list(ledger_paths)},
                "task_edited_file_checkouts": {TASK_ID: {repo_path: list(ledger_paths)}},
            }
        ),
        task=_task(commits=[commit_sha]),
        resolved_session_id="closing-session",
        repo_path=repo_path,
        prospective_commit_shas=(commit_sha,),
    )
    assert snapshot.complete
    assert snapshot.clean_checkouts == {repo_path: expected_paths}
