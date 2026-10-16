"""Agent clones fork the selected commit from the caller's local repository."""

import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.agents.isolation_clone import CloneIsolationHandler
from gobby.agents.isolation_models import SpawnConfig
from gobby.clones.git import CloneGitManager
from gobby.worktrees.base_branch import WorktreeBaseError
from gobby.worktrees.git import WorktreeGitManager


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "Clone Fork Test")
    (root / "tracked.txt").write_text("main bytes\n")
    git(root, "add", ".")
    git(root, "commit", "-m", "main")
    return root


def clone_handler(
    repo: Path, child: Path, monkeypatch: pytest.MonkeyPatch
) -> CloneIsolationHandler:
    monkeypatch.setattr("gobby.clones.git.CLONES_ROOT", child.parent)
    monkeypatch.setattr("gobby.agents.isolation_clone.repair_isolation_environment", AsyncMock())
    storage = MagicMock()
    storage.get_by_branch.return_value = None
    storage.create.side_effect = lambda **values: SimpleNamespace(id="clone", **values)
    instance = CloneIsolationHandler(CloneGitManager(repo), storage, WorktreeGitManager(repo))
    monkeypatch.setattr(instance, "_generate_clone_path", lambda *args: str(child))
    return instance


def clone_config(repo: Path, child: Path, base: str | None) -> SpawnConfig:
    return SpawnConfig(
        prompt="fork",
        task_id=None,
        task_title=None,
        task_seq_num=None,
        branch_name=child.name,
        branch_prefix=None,
        base_branch=base,
        project_id="project",
        project_path=str(repo),
        provider="codex",
        parent_session_id="parent",
    )


@pytest.mark.parametrize("base", [None, "main"])
async def test_clone_omission_and_detached_head(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, base: str | None
) -> None:
    main = git(repo, "rev-parse", "HEAD")
    git(repo, "switch", "-c", "caller")
    git(repo, "commit", "--allow-empty", "-m", "unpushed caller commit")
    selected = git(repo, "rev-parse", "HEAD") if base is None else main
    child = tmp_path / "clones" / "agent-clone"
    instance = clone_handler(repo, child, monkeypatch)
    source = instance._git_manager
    assert isinstance(source, WorktreeGitManager)
    resolve = AsyncMock(wraps=source.get_local_commit)
    monkeypatch.setattr(source, "get_local_commit", resolve)

    ctx = await instance.prepare_environment(clone_config(repo, child, base))

    assert git(child, "rev-parse", "HEAD") == selected
    assert git(child, "rev-parse", "--abbrev-ref", "HEAD") == "agent-clone"
    assert ctx.extra["base_commit_sha"] == selected
    assert ctx.extra["base_branch"] == ("caller" if base is None else "main")
    stored = instance._clone_storage.create.call_args.kwargs
    assert stored["base_branch"] == ("caller" if base is None else "main")
    resolve.assert_awaited_once_with(
        "HEAD^{commit}" if base is None else "refs/heads/main^{commit}"
    )

    git(repo, "switch", "--detach")
    detached_child = tmp_path / "clones" / "detached-clone"
    detached = clone_handler(repo, detached_child, monkeypatch)
    with pytest.raises(WorktreeBaseError) as error:
        await detached.prepare_environment(clone_config(repo, detached_child, None))
    assert error.value.code == "detached_head_requires_base"
    detached._clone_storage.get_by_branch.assert_not_called()
    detached._clone_storage.create.assert_not_called()
    assert not detached_child.exists()
