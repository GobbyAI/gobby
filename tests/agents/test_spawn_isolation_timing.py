"""Isolation timing measures real boundaries without counting repair twice."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.agents import isolation_repair, spawn_timing
from gobby.agents.isolation_clone import CloneIsolationHandler
from gobby.agents.isolation_models import IsolationHandler, SpawnConfig
from gobby.agents.isolation_worktree import WorktreeIsolationHandler
from gobby.mcp_proxy.tools.spawn_agent import _worktree_reuse
from gobby.mcp_proxy.tools.spawn_agent._isolation_prepare import prepare_spawn_isolation

pytestmark = pytest.mark.unit


@dataclass
class Clock:
    now: float = 0.0

    def read(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    value = Clock()
    monkeypatch.setattr("gobby.agents.spawn_timing.time.perf_counter", value.read)
    return value


@pytest.fixture
def config(tmp_path: Path) -> SpawnConfig:
    return SpawnConfig(
        prompt="timing",
        task_id=None,
        task_title=None,
        task_seq_num=None,
        branch_name="timing",
        branch_prefix=None,
        base_branch="main",
        project_id="project",
        project_path=str(tmp_path),
        provider="codex",
        parent_session_id="parent",
    )


@pytest.mark.parametrize("provider", ["codex", "claude", "droid"])
async def test_spawn_timing_attributes_isolation_repair(
    monkeypatch: pytest.MonkeyPatch, clock: Clock, provider: str
) -> None:
    order: list[str] = []

    def step(name: str, seconds: float) -> Callable[..., Awaitable[SimpleNamespace]]:
        async def run(*args: object, **kwargs: object) -> SimpleNamespace:
            order.append(name)
            clock.now += seconds
            return SimpleNamespace(attempted=True, success=True)

        return run

    monkeypatch.setattr(isolation_repair, "_copy_cli_hooks", step("hooks", 1))
    monkeypatch.setattr(
        "gobby.utils.project_context.ensure_project_json_for_isolation", step("marker", 2)
    )
    monkeypatch.setattr(isolation_repair, "preseed_isolated_python_environment", step("seed", 3))
    monkeypatch.setattr(isolation_repair, "_patch_mcp_config_for_isolation", step("mcp", 4))
    monkeypatch.setattr(isolation_repair, "apply_isolation_git_hygiene", step("hygiene", 5))
    timings: dict[str, float] = {}

    await isolation_repair.repair_isolation_environment(
        main_repo_path="parent",
        isolated_path="child",
        provider=provider,
        phase_timings_ms=timings,
    )

    assert order == ["hooks", "marker", "seed", "mcp", "hygiene"]
    assert timings == {
        "isolation_hook_copy": 1000,
        "isolation_project_marker": 2000,
        "python_env_seed": 3000,
        "isolation_mcp_config": 4000,
        "isolation_git_hygiene": 5000,
    }


def test_idle_baseline_breakdown_is_complete(clock: Clock) -> None:
    clock.now = 33.9
    timings = {
        "isolation_prepare": 24000.0,
        "python_env_seed": 20000.0,
        "code_index_status": 979.0,
        "code_index_index": 7064.0,
        "code_index_search_content": 192.0,
        "_preflight_srt": 1234.0,
    }
    result = spawn_timing.complete_spawn_phase_timings(timings, started_at=0)
    assert result["spawn_wall"] == 33900
    assert result["unattributed"] == 431
    assert result["python_env_seed"] == 20000
    assert set(result) == set(spawn_timing.SPAWN_PHASES)


@pytest.mark.parametrize(
    ("kind", "fresh"),
    [
        ("worktree", False),
        ("worktree", True),
        ("clone", False),
        ("clone", True),
        ("reused-worktree", False),
        ("resumed-worktree", False),
    ],
)
async def test_isolation_paths_share_spawn_timing_map(
    monkeypatch: pytest.MonkeyPatch,
    config: SpawnConfig,
    clock: Clock,
    kind: str,
    fresh: bool,
    tmp_path: Path,
) -> None:
    async def repair(*, phase_timings_ms: dict[str, float], **kwargs: object) -> None:
        assert phase_timings_ms is config.phase_timings_ms
        phase_timings_ms["python_env_seed"] = 1000
        clock.now += 1

    storage = MagicMock()
    existing = SimpleNamespace(
        worktree_path=str(tmp_path),
        clone_path=str(tmp_path),
        id="workspace",
        branch_name="timing",
        base_branch="main",
    )
    storage.get_by_branch.return_value = None if fresh else existing
    storage.create.return_value = existing
    storage.is_claimed_by_live_session.return_value = False
    git = MagicMock()
    git.repo_path = tmp_path
    git.get_current_branch = AsyncMock(return_value="main")
    git.get_local_commit = AsyncMock(return_value="base-sha")
    git.run_git_command = AsyncMock(return_value=SimpleNamespace(returncode=0))
    git.create_worktree = AsyncMock(return_value=SimpleNamespace(success=True))
    git.create_clone = AsyncMock(return_value=SimpleNamespace(success=True))
    sync = AsyncMock(return_value=SimpleNamespace(base_commit_sha=None))
    handler: IsolationHandler
    if kind == "worktree":
        monkeypatch.setattr("gobby.agents.isolation_worktree.repair_isolation_environment", repair)
        monkeypatch.setattr("gobby.agents.worktree_reuse.sync_reused_worktree_to_base", sync)
        handler = WorktreeIsolationHandler(git, storage)
    elif kind == "clone":
        monkeypatch.setattr("gobby.agents.isolation_clone.repair_isolation_environment", repair)
        handler = CloneIsolationHandler(git, storage, git)
    else:
        monkeypatch.setattr(_worktree_reuse, "repair_isolation_environment", repair)
        monkeypatch.setattr(_worktree_reuse, "sync_reused_worktree_to_base", sync)
        if kind == "resumed-worktree":
            reused_context, _ = await _worktree_reuse.prepare_resumed_worktree(
                existing_worktree=existing,
                spawn_config=config,
                main_repo_path=str(tmp_path),
            )
            sync.assert_not_awaited()
        else:
            reused_context, _ = await _worktree_reuse.prepare_reused_worktree(
                existing_worktree=existing,
                git_manager=git,
                worktree_storage=storage,
                spawn_config=config,
                main_repo_path=str(tmp_path),
            )
        assert reused_context.cwd == str(tmp_path)
        assert config.phase_timings_ms == {"python_env_seed": 1000}
        return

    context, error = await prepare_spawn_isolation(handler, config)
    assert error is None
    assert context is not None and context.cwd == str(tmp_path)
    assert config.phase_timings_ms == {"python_env_seed": 1000, "isolation_prepare": 1000}


@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_prepare_failure_response_unchanged_after_move(
    config: SpawnConfig,
    clock: Clock,
    cleanup_fails: bool,
) -> None:
    class PrepareError(RuntimeError):
        code = "prepare_failed"

    handler = MagicMock(spec=IsolationHandler)

    async def fail(_: SpawnConfig) -> None:
        clock.now += 2
        raise PrepareError("git error")

    handler.prepare_environment = AsyncMock(side_effect=fail)
    handler.cleanup_environment = AsyncMock(
        side_effect=RuntimeError("cleanup error") if cleanup_fails else None
    )
    context, response = await prepare_spawn_isolation(handler, config)
    assert context is None
    assert response == {
        "success": False,
        "error": "Failed to prepare environment: git error",
        "error_code": "prepare_failed",
    }
    handler.cleanup_environment.assert_awaited_once_with(config)
    assert config.phase_timings_ms["isolation_prepare"] == 2000
