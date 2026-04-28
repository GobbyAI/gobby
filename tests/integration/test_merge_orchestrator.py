"""End-to-end integration test for the merge-orchestrator's primitives.

Spins up a real git repository with three worktrees in distinct states:
  - wt-trivial: a clean branch that will merge into main without conflict
  - wt-conflict: a branch that edits the same line as main, predicting a conflict
  - wt-orphan: a worktree with an orphaned MERGE_HEAD planted manually

Then exercises the merge-landscape tools the orchestrator drives during
its survey phase (analyze_merge_landscape, predict_conflicts,
inspect_merge_state) against real git state. Asserts each scenario is
detected correctly so that the orchestrator's planning step would receive
faithful inputs in production.

The full LLM-driven agent dispatch loop (claim → load_skill → survey →
plan → execute → report) is deferred to the gobby-build e2e flow once
dispatcher rule 9 (#12725) lands. This test covers the surface area the
orchestrator interacts with via gobby-merge MCP tools.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.merge_landscape import register_merge_landscape_tools
from gobby.storage.worktrees import Worktree
from gobby.worktrees.git import WorktreeGitManager

pytestmark = [pytest.mark.integration]


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=check,
    )


@pytest.fixture
def merge_campaign_repo(tmp_path: Path) -> dict[str, object]:
    """Build a real git repo with three worktrees in distinct states.

    Returns a dict with:
      - repo_path: Path to the main repo
      - worktrees: dict mapping wt_id -> Worktree dataclass instance
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--initial-branch=main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")

    base = repo / "base.txt"
    base.write_text("line1\nline2\nline3\n")
    other = repo / "other.txt"
    other.write_text("alpha\nbeta\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")

    # Branch wt-trivial: edits other.txt only — no overlap with main's later edits.
    _git(repo, "checkout", "-b", "feat/trivial")
    other.write_text("alpha\nbeta\ngamma\n")
    _git(repo, "add", "other.txt")
    _git(repo, "commit", "-m", "trivial: extend other.txt")

    # Branch wt-conflict: edits line2 of base.txt.
    _git(repo, "checkout", "main")
    _git(repo, "checkout", "-b", "feat/conflict")
    base.write_text("line1\nbranch-version\nline3\n")
    _git(repo, "add", "base.txt")
    _git(repo, "commit", "-m", "conflict: change line2 to branch-version")

    # Now move main forward so feat/conflict overlaps.
    _git(repo, "checkout", "main")
    base.write_text("line1\nmain-version\nline3\n")
    _git(repo, "add", "base.txt")
    _git(repo, "commit", "-m", "main: change line2 to main-version")

    # Branch wt-orphan: trivial branch (we'll plant MERGE_HEAD manually below).
    _git(repo, "checkout", "-b", "feat/orphan")
    (repo / "orphan.txt").write_text("orphan content\n")
    _git(repo, "add", "orphan.txt")
    _git(repo, "commit", "-m", "orphan: add file")

    # Park the main repo back on main so each branch is free to be checked
    # out in its own worktree below.
    _git(repo, "checkout", "main")

    # Create real git worktrees pointing at each branch.
    wt_trivial_path = tmp_path / "wt-trivial"
    wt_conflict_path = tmp_path / "wt-conflict"
    wt_orphan_path = tmp_path / "wt-orphan"
    _git(repo, "worktree", "add", str(wt_trivial_path), "feat/trivial")
    _git(repo, "worktree", "add", str(wt_conflict_path), "feat/conflict")
    _git(repo, "worktree", "add", str(wt_orphan_path), "feat/orphan")

    # Plant an orphaned MERGE_HEAD in wt-orphan.
    git_dir_proc = _git(wt_orphan_path, "rev-parse", "--git-dir")
    orphan_git_dir = (wt_orphan_path / git_dir_proc.stdout.strip()).resolve()
    head_sha = _git(wt_orphan_path, "rev-parse", "HEAD").stdout.strip()
    (orphan_git_dir / "MERGE_HEAD").write_text(head_sha + "\n")

    def _wt(id_: str, branch: str, path: Path) -> Worktree:
        return Worktree(
            id=id_,
            project_id="proj-1",
            task_id=None,
            branch_name=branch,
            worktree_path=str(path),
            base_branch="main",
            agent_session_id=None,
            status="active",
            created_at="2026-04-28T00:00:00Z",
            updated_at="2026-04-28T00:00:00Z",
            merged_at=None,
            merge_state="pending" if id_ == "wt-orphan" else None,
        )

    worktrees = {
        "wt-trivial": _wt("wt-trivial", "feat/trivial", wt_trivial_path),
        "wt-conflict": _wt("wt-conflict", "feat/conflict", wt_conflict_path),
        "wt-orphan": _wt("wt-orphan", "feat/orphan", wt_orphan_path),
    }
    return {"repo_path": repo, "worktrees": worktrees}


@pytest.fixture
def registry(merge_campaign_repo: dict[str, object]) -> InternalToolRegistry:
    worktrees = merge_campaign_repo["worktrees"]
    assert isinstance(worktrees, dict)

    worktree_manager = MagicMock()
    worktree_manager.list_worktrees.return_value = list(worktrees.values())
    worktree_manager.get.side_effect = lambda wid: worktrees.get(wid)

    git_manager = WorktreeGitManager(merge_campaign_repo["repo_path"])

    registry = InternalToolRegistry(name="gobby-merge", description="test")
    register_merge_landscape_tools(
        registry,
        worktree_manager=worktree_manager,
        git_manager=git_manager,
    )
    return registry


@pytest.mark.asyncio
async def test_analyze_merge_landscape_all_three_worktrees(
    registry: InternalToolRegistry,
) -> None:
    """analyze_merge_landscape returns all three worktrees with correct stats."""
    result = await registry.call("analyze_merge_landscape", {})

    assert result["success"] is True
    assert len(result["worktrees"]) == 3

    by_id = {entry["worktree_id"]: entry for entry in result["worktrees"]}
    assert set(by_id.keys()) == {"wt-trivial", "wt-conflict", "wt-orphan"}

    trivial = by_id["wt-trivial"]
    assert trivial["branch"] == "feat/trivial"
    assert trivial["files_touched"] == ["other.txt"]
    # `git rev-list --count base...HEAD` is the symmetric diff — non-zero is
    # all we need to assert; the exact value depends on what main accumulated.
    assert isinstance(trivial["divergence_commits"], int)
    assert trivial["divergence_commits"] >= 1

    conflict = by_id["wt-conflict"]
    assert conflict["branch"] == "feat/conflict"
    assert conflict["files_touched"] == ["base.txt"]

    orphan = by_id["wt-orphan"]
    assert orphan["branch"] == "feat/orphan"
    assert orphan["merge_state"] == "pending"


@pytest.mark.asyncio
async def test_predict_conflicts_distinguishes_clean_from_conflicting(
    registry: InternalToolRegistry,
) -> None:
    """predict_conflicts identifies which worktrees conflict against main."""
    result = await registry.call(
        "predict_conflicts",
        {
            "worktree_ids": ["wt-trivial", "wt-conflict", "wt-orphan"],
            "target_branch": "main",
        },
    )

    assert result["success"] is True
    targets = {p["worktree_id"]: p for p in result["target_predictions"]}

    # wt-trivial only edits other.txt — clean against main.
    assert targets["wt-trivial"]["clean"] is True
    assert targets["wt-trivial"]["conflict_files"] == []

    # wt-conflict edits base.txt line2 which main also changed — must conflict.
    assert targets["wt-conflict"]["clean"] is False
    assert "base.txt" in targets["wt-conflict"]["conflict_files"]

    # wt-orphan adds a new file — clean against main.
    assert targets["wt-orphan"]["clean"] is True


@pytest.mark.asyncio
async def test_inspect_merge_state_detects_orphaned_merge_head(
    registry: InternalToolRegistry,
) -> None:
    """inspect_merge_state finds the planted MERGE_HEAD in wt-orphan only."""
    orphan_result = await registry.call(
        "inspect_merge_state", {"worktree_id": "wt-orphan"}
    )
    assert orphan_result["success"] is True
    assert orphan_result["state"] == "merging"
    assert orphan_result["has_merge_head"] is True

    trivial_result = await registry.call(
        "inspect_merge_state", {"worktree_id": "wt-trivial"}
    )
    assert trivial_result["success"] is True
    assert trivial_result["state"] == "clean"
    assert trivial_result["has_merge_head"] is False
    assert trivial_result["conflicted_files"] == []

    conflict_result = await registry.call(
        "inspect_merge_state", {"worktree_id": "wt-conflict"}
    )
    assert conflict_result["success"] is True
    assert conflict_result["state"] == "clean"
    assert conflict_result["has_merge_head"] is False


@pytest.mark.asyncio
async def test_orchestrator_yaml_loads(tmp_path: Path) -> None:
    """The merge-orchestrator agent definition parses and exposes the expected steps."""
    import yaml

    yaml_path = (
        Path(__file__).resolve().parents[2]
        / "src/gobby/install/shared/workflows/agents/merge-orchestrator.yaml"
    )
    assert yaml_path.exists(), f"merge-orchestrator.yaml missing at {yaml_path}"
    data = yaml.safe_load(yaml_path.read_text())

    assert data["name"] == "merge-orchestrator"
    step_names = [s["name"] for s in data["steps"]]
    assert step_names == [
        "claim",
        "load_skill",
        "survey",
        "plan",
        "execute",
        "report",
        "terminate",
    ]
    assert data["provider"] == "codex"
    assert data["isolation"] == "none"
