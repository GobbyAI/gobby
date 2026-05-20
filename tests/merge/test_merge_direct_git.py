from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _git_succeeds(cwd: Path, *args: str) -> bool:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def _init_repo(repo: Path) -> None:
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (repo / "README.md").write_text("initial\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial")
    _git(repo, "update-ref", "refs/remotes/origin/main", "main")


def _create_target_branch(repo: Path, branch: str) -> None:
    if branch == "main":
        return
    _git(repo, "checkout", "-b", branch, "main")
    _git(repo, "update-ref", f"refs/remotes/origin/{branch}", branch)
    _git(repo, "checkout", "main")


def _create_feature_worktree(
    repo: Path,
    tmp_path: Path,
    branch: str,
    *,
    base_branch: str = "main",
) -> tuple[Path, str]:
    worktree_path = tmp_path / "feature-worktree"
    _git(repo, "worktree", "add", "-b", branch, str(worktree_path), base_branch)
    (worktree_path / "feature.txt").write_text("feature\n", encoding="utf-8")
    _git(worktree_path, "add", "feature.txt")
    _git(worktree_path, "commit", "-m", "feature")
    return worktree_path, _git(worktree_path, "rev-parse", "HEAD")


def _commit_on_main(
    repo: Path,
    filename: str,
    content: str,
    *,
    update_origin: bool = True,
) -> str:
    _git(repo, "checkout", "main")
    (repo / filename).write_text(content, encoding="utf-8")
    _git(repo, "add", filename)
    _git(repo, "commit", "-m", f"main update {filename}")
    if update_origin:
        _git(repo, "update-ref", "refs/remotes/origin/main", "main")
    return _git(repo, "rev-parse", "HEAD")


def _commit_on_branch(
    repo: Path,
    branch: str,
    filename: str,
    content: str,
    *,
    update_origin: bool = True,
) -> str:
    if branch == "main":
        return _commit_on_main(repo, filename, content, update_origin=update_origin)
    _git(repo, "checkout", branch)
    (repo / filename).write_text(content, encoding="utf-8")
    _git(repo, "add", filename)
    _git(repo, "commit", "-m", f"{branch} update {filename}")
    if update_origin:
        _git(repo, "update-ref", f"refs/remotes/origin/{branch}", branch)
    _git(repo, "checkout", "main")
    return _git(repo, "rev-parse", branch)


def _create_registry(
    temp_db,
    repo: Path,
    worktree_path: Path,
    branch: str,
    *,
    base_branch: str = "main",
):
    from gobby.mcp_proxy.tools.merge import create_merge_registry
    from gobby.storage.merge_resolutions import MergeResolutionManager
    from gobby.storage.projects import LocalProjectManager
    from gobby.storage.worktrees import LocalWorktreeManager
    from gobby.worktrees.git import WorktreeGitManager
    from gobby.worktrees.merge import MergeResolver

    project = LocalProjectManager(temp_db).create("merge-direct", repo_path=str(repo))
    worktree_manager = LocalWorktreeManager(temp_db)
    worktree = worktree_manager.create(
        project_id=project.id,
        branch_name=branch,
        worktree_path=str(worktree_path),
        base_branch=base_branch,
    )
    merge_storage = MergeResolutionManager(temp_db)
    registry = create_merge_registry(
        merge_storage=merge_storage,
        merge_resolver=MergeResolver(),
        git_manager=WorktreeGitManager(repo),
        worktree_manager=worktree_manager,
    )
    return registry, merge_storage, worktree


@pytest.mark.asyncio
async def test_merge_start_rejects_target_branch_as_source(temp_db, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    branch = "feature/reject-target-source"
    _init_repo(repo)
    worktree_path, _feature_sha = _create_feature_worktree(repo, tmp_path, branch)
    registry, _merge_storage, worktree = _create_registry(temp_db, repo, worktree_path, branch)

    result = await registry.call(
        "merge_start",
        {"worktree_id": worktree.id, "source_branch": "main", "target_branch": "main"},
    )

    assert result["success"] is False
    assert "source_branch and target_branch must differ" in result["error"]


@pytest.mark.parametrize("target_branch", ["main", "0.4.7"])
@pytest.mark.asyncio
async def test_merge_start_uses_local_target_branch_ref(
    temp_db,
    tmp_path: Path,
    target_branch: str,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    branch = f"feature/local-target-{target_branch.replace('.', '-')}"
    _init_repo(repo)
    _create_target_branch(repo, target_branch)
    worktree_path, _feature_sha = _create_feature_worktree(
        repo,
        tmp_path,
        branch,
        base_branch=target_branch,
    )
    target_sha = _commit_on_branch(
        repo,
        target_branch,
        "target.txt",
        "local target\n",
        update_origin=False,
    )
    origin_sha = _git(repo, "rev-parse", f"origin/{target_branch}")
    registry, _merge_storage, worktree = _create_registry(
        temp_db,
        repo,
        worktree_path,
        branch,
        base_branch=target_branch,
    )

    result = await registry.call(
        "merge_start",
        {"worktree_id": worktree.id, "source_branch": branch, "target_branch": target_branch},
    )

    assert result["success"] is True
    assert _git(worktree_path, "rev-parse", "MERGE_HEAD") == target_sha
    assert _git(worktree_path, "rev-parse", "MERGE_HEAD") != origin_sha


@pytest.mark.asyncio
async def test_merge_start_uses_remote_ref_when_origin_target_is_explicit(
    temp_db,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    branch = "feature/explicit-origin-target"
    _init_repo(repo)
    worktree_path, _feature_sha = _create_feature_worktree(repo, tmp_path, branch)
    local_main_sha = _commit_on_main(repo, "target.txt", "local target\n", update_origin=False)
    registry, _merge_storage, worktree = _create_registry(temp_db, repo, worktree_path, branch)

    result = await registry.call(
        "merge_start",
        {"worktree_id": worktree.id, "source_branch": branch, "target_branch": "origin/main"},
    )

    assert result["success"] is True
    assert not (worktree_path / "target.txt").exists()
    assert not _git_succeeds(worktree_path, "merge-base", "--is-ancestor", local_main_sha, "HEAD")


@pytest.mark.asyncio
async def test_merge_start_refreshes_stale_resolved_resolution(
    temp_db,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    branch = "feature/stale-resolved"
    _init_repo(repo)
    worktree_path, _feature_sha = _create_feature_worktree(repo, tmp_path, branch)
    main_sha = _commit_on_main(repo, "target.txt", "target\n", update_origin=False)
    registry, merge_storage, worktree = _create_registry(temp_db, repo, worktree_path, branch)
    merge_storage.create_resolution(
        worktree_id=worktree.id,
        source_branch=branch,
        target_branch="main",
        status="resolved",
        tier_used="git_auto",
    )

    refreshed = await registry.call(
        "merge_start",
        {"worktree_id": worktree.id, "source_branch": branch, "target_branch": "main"},
    )

    assert refreshed["success"] is True
    assert "reused_resolution" not in refreshed
    result = await registry.call("merge_apply", {"resolution_id": refreshed["resolution_id"]})
    assert result["success"] is True
    assert result["direct_merge"] is False
    assert _git(worktree_path, "merge-base", "--is-ancestor", main_sha, "HEAD") == ""


@pytest.mark.asyncio
async def test_pending_resolution_without_rows_hydrates_current_git_conflicts(
    temp_db,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    branch = "feature/hydrate-missing-conflicts"
    _init_repo(repo)
    (repo / "shared.py").write_text("value = 'base'\n", encoding="utf-8")
    _git(repo, "add", "shared.py")
    _git(repo, "commit", "-m", "add shared")
    _git(repo, "update-ref", "refs/remotes/origin/main", "main")
    worktree_path, _feature_sha = _create_feature_worktree(repo, tmp_path, branch)
    (worktree_path / "shared.py").write_text("value = 'feature'\n", encoding="utf-8")
    _git(worktree_path, "add", "shared.py")
    _git(worktree_path, "commit", "-m", "feature shared")
    _commit_on_main(repo, "shared.py", "value = 'main'\n", update_origin=False)
    merge = subprocess.run(
        ["git", "merge", "--no-commit", "--no-ff", "main"],
        cwd=worktree_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert merge.returncode != 0

    registry, merge_storage, worktree = _create_registry(temp_db, repo, worktree_path, branch)
    resolution = merge_storage.create_resolution(
        worktree_id=worktree.id,
        source_branch=branch,
        target_branch="main",
        status="pending",
    )

    inspected = await registry.call("inspect_merge_state", {"worktree_id": worktree.id})
    assert inspected["state"] == "merging"
    assert inspected["active_resolution_id"] == resolution.id
    assert inspected["conflicts"][0]["file_path"] == "shared.py"
    assert inspected["conflicts"][0]["status"] == "pending"

    status = await registry.call("merge_status", {"resolution_id": resolution.id})
    assert status["pending_count"] == 1
    assert status["conflicts"][0]["file_path"] == "shared.py"
    stored = merge_storage.list_conflicts(resolution_id=resolution.id)
    assert len(stored) == 1

    merge_storage.update_conflict(stored[0].id, status="resolved")
    merge_storage.update_resolution(resolution.id, status="resolved", tier_used="full_file_ai")

    normalized = await registry.call("merge_status", {"resolution_id": resolution.id})

    assert normalized["pending_count"] == 1
    assert normalized["resolved_count"] == 0
    assert normalized["conflicts"][0]["status"] == "pending"
    assert normalized["resolution"]["status"] == "pending"
    assert merge_storage.get_conflict(stored[0].id).status == "pending"
    assert merge_storage.get_resolution(resolution.id).status == "pending"


@pytest.mark.asyncio
async def test_merge_apply_fast_forwards_reused_clean_resolution(temp_db, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    branch = "feature/direct-ff"
    _init_repo(repo)
    worktree_path, feature_sha = _create_feature_worktree(repo, tmp_path, branch)
    registry, _merge_storage, worktree = _create_registry(temp_db, repo, worktree_path, branch)

    first = await registry.call(
        "merge_start",
        {"worktree_id": worktree.id, "source_branch": branch, "target_branch": "main"},
    )
    assert first["success"] is True

    reused = await registry.call(
        "merge_start",
        {"worktree_id": worktree.id, "source_branch": branch, "target_branch": "main"},
    )
    assert reused["success"] is True
    assert reused["reused_resolution"] is True

    result = await registry.call("merge_apply", {"resolution_id": reused["resolution_id"]})

    assert result["success"] is True
    assert result["direct_merge"] is True
    assert result["merge_strategy"] == "ff-only"
    assert result["merge_sha"] == feature_sha
    assert result["commit_sha"] == feature_sha
    assert _git(repo, "rev-parse", "main") == feature_sha
    assert _git(repo, "rev-list", "--count", "main") == "2"


@pytest.mark.asyncio
async def test_no_ff_strategy_bypasses_reuse_and_creates_merge_commit(
    temp_db,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    branch = "feature/direct-no-ff"
    _init_repo(repo)
    worktree_path, feature_sha = _create_feature_worktree(repo, tmp_path, branch)
    registry, merge_storage, worktree = _create_registry(temp_db, repo, worktree_path, branch)

    first = await registry.call(
        "merge_start",
        {"worktree_id": worktree.id, "source_branch": branch, "target_branch": "main"},
    )
    assert first["success"] is True

    no_ff = await registry.call(
        "merge_start",
        {
            "worktree_id": worktree.id,
            "source_branch": branch,
            "target_branch": "main",
            "strategy": "no-ff",
        },
    )
    assert no_ff["success"] is True
    assert "reused_resolution" not in no_ff
    assert no_ff["tier"] == "git_no_ff"
    assert merge_storage.get_resolution(no_ff["resolution_id"]).tier_used == "git_no_ff"

    result = await registry.call("merge_apply", {"resolution_id": no_ff["resolution_id"]})

    assert result["success"] is True
    assert result["direct_merge"] is True
    assert result["merge_strategy"] == "no-ff"
    assert result["merge_sha"] == _git(repo, "rev-parse", "main")
    assert result["merge_sha"] != feature_sha
    assert _git(repo, "rev-parse", "main^2") == feature_sha
    assert _git(repo, "rev-list", "--count", "main") == "3"
