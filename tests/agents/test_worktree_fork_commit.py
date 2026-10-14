"""Selected fork provenance and tracked bytes survive isolation preparation."""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.agents.isolation_clone import CloneIsolationHandler
from gobby.agents.isolation_models import SpawnConfig
from gobby.agents.isolation_repair import repair_isolation_environment
from gobby.agents.isolation_worktree import WorktreeIsolationHandler
from gobby.agents.worktree_reuse import _blocking_status_lines, sync_reused_worktree_to_base
from gobby.worktrees.git import WorktreeGitManager


async def test_explicit_reuse_records_selected_base(repo: Path, tmp_path: Path) -> None:
    from gobby.mcp_proxy.tools.spawn_agent._worktree_reuse import prepare_reused_worktree

    child = tmp_path / "reused"
    git(repo, "worktree", "add", "-b", "candidate", str(child), "main")
    git(repo, "switch", "-c", "release")
    (repo / "new-base.txt").write_text("release bytes\n")
    git(repo, "add", "new-base.txt")
    git(repo, "commit", "-m", "advance release")
    selected = git(repo, "rev-parse", "HEAD")
    storage = MagicMock()
    existing = SimpleNamespace(worktree_path=str(child), branch_name="candidate", id="wt-id")
    context, _ = await prepare_reused_worktree(
        existing_worktree=existing,
        git_manager=WorktreeGitManager(repo),
        worktree_storage=storage,
        spawn_config=handler_config(repo, child, None),
        main_repo_path=str(repo),
    )
    storage.update.assert_called_once_with("wt-id", base_branch="release")
    assert context.extra["base_commit_sha"] == selected
    assert git(child, "rev-parse", "HEAD") == selected


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
    git(root, "config", "user.name", "Isolation Fork Test")
    for name in ("modified.txt", "staged.txt", "deleted.txt", ".codex/dirty-parent-sentinel.txt"):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("selected bytes\n")
    (root / ".mcp.json").write_text(json.dumps({"mcpServers": {"other": {"command": "kept"}}}))
    git(root, "add", ".")
    git(root, "commit", "-m", "selected files")
    return root


def handler_config(repo: Path, child: Path, base: str | None) -> SpawnConfig:
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


def handler(repo: Path, child: Path, monkeypatch: pytest.MonkeyPatch) -> WorktreeIsolationHandler:
    storage = MagicMock()
    storage.get_by_branch.return_value = None
    storage.create.side_effect = lambda **values: SimpleNamespace(id="fork", **values)
    instance = WorktreeIsolationHandler(WorktreeGitManager(repo), storage)
    monkeypatch.setattr(instance, "_generate_worktree_path", lambda *args: str(child))
    return instance


@pytest.mark.parametrize(
    ("explicit", "definition", "base"),
    [
        (None, None, None),
        (None, "inherit", None),
        ("main", "inherit", "main"),
        (None, "main", "main"),
    ],
)
async def test_omitted_base_survives_to_one_head_resolution(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    explicit: str | None,
    definition: str | None,
    base: str | None,
) -> None:
    from gobby.mcp_proxy.tools.spawn_agent._isolation_prepare import requested_base_branch

    requested = requested_base_branch(explicit, definition)
    assert requested == base
    main = git(repo, "rev-parse", "HEAD")
    git(repo, "switch", "-c", "caller")
    git(repo, "commit", "--allow-empty", "-m", "caller advances")
    selected = git(repo, "rev-parse", "HEAD") if base is None else main
    child = tmp_path / "fork"
    instance = handler(repo, child, monkeypatch)
    resolve = AsyncMock(wraps=instance._git_manager.get_local_commit)
    monkeypatch.setattr(instance._git_manager, "get_local_commit", resolve)
    ctx = await instance.prepare_environment(handler_config(repo, child, requested))
    assert git(child, "rev-parse", "HEAD") == selected
    assert ctx.extra["base_commit_sha"] == selected
    resolve.assert_awaited_once_with(
        "HEAD^{commit}" if base is None else "refs/heads/main^{commit}"
    )


async def test_detached_handler_refuses_before_storage_effects(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.worktrees.base_branch import WorktreeBaseError

    git(repo, "switch", "--detach")
    child = tmp_path / "detached-fork"
    instance = handler(repo, child, monkeypatch)
    before = git(repo, "rev-parse", "HEAD")
    with pytest.raises(WorktreeBaseError) as error:
        await instance.prepare_environment(handler_config(repo, child, None))
    assert error.value.code == "detached_head_requires_base"
    instance._worktree_storage.get_by_branch.assert_not_called()
    instance._worktree_storage.create.assert_not_called()
    assert not child.exists()
    assert git(repo, "rev-parse", "HEAD") == before


async def test_reuse_conflict_preserves_candidate(repo: Path, tmp_path: Path) -> None:
    from gobby.agents.worktree_reuse import ReusedWorktreeRebaseConflict

    child = tmp_path / "conflicted"
    git(repo, "worktree", "add", "-b", "candidate", str(child), "main")
    (child / "modified.txt").write_text("candidate bytes\n")
    git(child, "commit", "-am", "candidate changes")
    candidate = git(child, "rev-parse", "HEAD")
    (repo / "modified.txt").write_text("new base bytes\n")
    git(repo, "commit", "-am", "base changes")
    selected = git(repo, "rev-parse", "HEAD")
    with pytest.raises(ReusedWorktreeRebaseConflict) as error:
        await sync_reused_worktree_to_base(
            git_manager=WorktreeGitManager(repo),
            worktree_path=str(child),
            base_branch="main",
            base_commit_sha=selected,
        )
    assert error.value.base_commit_sha == selected
    assert error.value.worktree_path == str(child)
    assert git(child, "rev-parse", "HEAD") == candidate
    assert (child / "modified.txt").read_text() == "candidate bytes\n"
    assert git(child, "status", "--porcelain") == ""


async def test_repair_does_not_follow_tracked_provider_root(
    repo: Path,
    tmp_path: Path,
) -> None:
    git(repo, "rm", "-r", ".codex")
    outside = tmp_path / "provider-target"
    outside.mkdir()
    sentinel = outside / "hooks.txt"
    sentinel.write_text("preserved bytes\n")
    (repo / ".codex").symlink_to(outside, target_is_directory=True)
    git(repo, "add", ".codex")
    git(repo, "commit", "-m", "tracked provider link")
    child = tmp_path / "provider-fork"
    git(repo, "worktree", "add", "-b", "provider-child", str(child), "main")
    (repo / ".codex").unlink()
    (repo / ".codex").mkdir()
    (repo / ".codex" / "hooks.txt").write_text("dirty parent bytes\n")
    await repair_isolation_environment(
        main_repo_path=str(repo),
        isolated_path=str(child),
        provider="codex",
    )
    assert (child / ".codex").is_symlink()
    assert sentinel.read_text() == "preserved bytes\n"


async def test_dirty_parent_fork_yields_clean_worktree(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected = git(repo, "rev-parse", "HEAD")
    (repo / "modified.txt").write_text("parent modified")
    (repo / "staged.txt").write_text("parent staged")
    git(repo, "add", "staged.txt")
    (repo / "deleted.txt").unlink()
    (repo / "untracked.txt").write_text("parent only")
    (repo / ".codex/dirty-parent-sentinel.txt").write_text("parent hook dirt")
    (repo / ".mcp.json").write_text(json.dumps({"mcpServers": {"parent-only": {}}}))
    child = tmp_path / "fork"
    instance = handler(repo, child, monkeypatch)
    await instance.prepare_environment(handler_config(repo, child, None))
    for name in git(child, "ls-files").splitlines():
        if name in {".mcp.json", ".factory/hooks/hooks.json"}:
            continue
        expected = subprocess.check_output(["git", "-C", str(repo), "show", f"{selected}:{name}"])
        assert (child / name).read_bytes() == expected, name
    servers = json.loads((child / ".mcp.json").read_text())["mcpServers"]
    assert servers["other"] == {"command": "kept"}
    assert set(servers) == {"other", "gobby"}
    assert not (child / "untracked.txt").exists()
    assert not _blocking_status_lines(git(child, "status", "--porcelain"), child, repo)


async def test_repair_preserves_selected_provider_and_mcp_bytes(repo: Path, tmp_path: Path) -> None:
    child = tmp_path / "repair"
    git(repo, "clone", "--local", str(repo), str(child))
    (repo / ".codex/dirty-parent-sentinel.txt").write_text("parent hook dirt")
    await repair_isolation_environment(
        main_repo_path=str(repo), isolated_path=str(child), provider="codex"
    )
    assert (child / ".codex/dirty-parent-sentinel.txt").read_text() == "selected bytes\n"
    assert json.loads((child / ".mcp.json").read_text())["mcpServers"]["other"] == {
        "command": "kept"
    }


@pytest.mark.parametrize("selected_config", ["{", "[]", '{"mcpServers": []}'])
async def test_repair_refuses_malformed_selected_mcp_without_overwriting(
    repo: Path, tmp_path: Path, selected_config: str
) -> None:
    (repo / ".mcp.json").write_text(selected_config)
    git(repo, "add", ".mcp.json")
    git(repo, "commit", "-m", "malformed selected config")
    child = tmp_path / "invalid-mcp"
    git(repo, "clone", "--local", str(repo), str(child))
    (repo / ".mcp.json").write_text('{"mcpServers": {"parent": {"command": "dirty"}}}')
    with pytest.raises(ValueError):
        await repair_isolation_environment(
            main_repo_path=str(repo), isolated_path=str(child), provider="codex"
        )
    assert (child / ".mcp.json").read_text() == selected_config
    assert git(child, "status", "--porcelain", "--", ".mcp.json") == ""


async def test_repair_refuses_provider_copy_when_tracked_files_are_unavailable(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gobby.utils.daemon_git import daemon_git

    child = tmp_path / "uninspectable"
    git(repo, "clone", "--local", str(repo), str(child))
    selected_hooks = (child / ".codex/dirty-parent-sentinel.txt").read_text()
    (repo / ".codex/dirty-parent-sentinel.txt").write_text("dirty parent")
    monkeypatch.setattr(daemon_git, "run", AsyncMock(return_value=None))
    with pytest.raises(RuntimeError, match="Cannot inspect tracked provider files"):
        await repair_isolation_environment(
            main_repo_path=str(repo), isolated_path=str(child), provider="codex"
        )
    assert (child / ".codex/dirty-parent-sentinel.txt").read_text() == selected_hooks
    assert git(child, "status", "--porcelain", "--", ".codex") == ""


async def test_refresh_preserves_workspace_ancestry(repo: Path, tmp_path: Path) -> None:
    child = tmp_path / "reuse"
    git(repo, "worktree", "add", "-b", "candidate", str(child))
    (child / "candidate.txt").write_text("candidate work")
    git(child, "add", ".")
    git(child, "commit", "-m", "candidate")
    (repo / "base.txt").write_text("selected base")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base advances")
    selected = git(repo, "rev-parse", "HEAD")
    git(repo, "commit", "--allow-empty", "-m", "base moves after selection")
    result = await sync_reused_worktree_to_base(
        git_manager=WorktreeGitManager(repo),
        worktree_path=str(child),
        base_branch="main",
        base_commit_sha=selected,
    )
    assert result.base_commit_sha == selected
    assert git(child, "rev-parse", "HEAD^") == selected
    assert (child / "candidate.txt").read_text() == "candidate work"


@pytest.mark.parametrize("base", [None, "main"])
async def test_clone_boundary_distinguishes_omitted_base_and_explicit_main(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, base: str | None
) -> None:
    git(repo, "switch", "-c", "caller")
    child = tmp_path / "clone"
    manager = MagicMock()
    manager.create_clone = AsyncMock(return_value=SimpleNamespace(success=True))
    storage = MagicMock()
    storage.get_by_branch.return_value = None
    storage.create.side_effect = lambda **values: SimpleNamespace(id="clone", **values)
    instance = CloneIsolationHandler(manager, storage, WorktreeGitManager(repo))
    monkeypatch.setattr(instance, "_generate_clone_path", lambda *args: str(child))
    monkeypatch.setattr("gobby.agents.isolation_clone.repair_isolation_environment", AsyncMock())
    context = await instance.prepare_environment(handler_config(repo, child, base))
    assert manager.create_clone.call_args.kwargs["base_branch"] == (
        "caller" if base is None else "main"
    )
    assert storage.create.call_args.kwargs["base_branch"] == ("caller" if base is None else "main")
    assert context.clone_id == "clone"
    assert context.cwd == str(child)
