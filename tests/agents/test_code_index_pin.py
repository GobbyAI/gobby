"""Pin lifecycle for agent code indexing (#23832 B2).

The agent preflight pins the isolation marker's selected commit immediately
before `gcode index`. A semantic pin miss exits zero and indexes locally; a
storage failure fails the preflight before any index, read, or spawn success.
"""

from collections.abc import Awaitable, Callable
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.agents.code_index import GcodeCommandError, ensure_isolation_code_index
from gobby.code_index.gcode_gateway import GcodeGateway, GcodeGatewayError
from gobby.mcp_proxy.tools.clones import create_clones_registry
from gobby.worktrees.creation import WorktreeCreationResult, create_worktree

GCODE = "/usr/local/bin/gcode"
GcodeRunner = Callable[..., Awaitable[None]]


def _recording_runner(
    calls: list[list[str]], *, pin_error: GcodeCommandError | None = None
) -> GcodeRunner:
    async def run(
        args: list[str],
        *,
        timeout: float,
        timeout_code: str,
        failure_code: str,
        env: dict[str, str] | None = None,
    ) -> None:
        calls.append(list(args))
        if pin_error is not None and args[1] == "pin":
            raise pin_error

    return run


def _subcommands(calls: list[list[str]]) -> list[str]:
    return [args[1] for args in calls]


@pytest.mark.asyncio
async def test_agent_preflight_pins_marker_before_index(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    calls: list[list[str]] = []
    phase_timings: dict[str, float] = {}

    with patch("gobby.agents.code_index._run_gcode", side_effect=_recording_runner(calls)):
        await ensure_isolation_code_index(
            str(workspace), gcode_bin=Path(GCODE), phase_timings_ms=phase_timings
        )

    assert _subcommands(calls) == ["status", "pin", "index", "search-content"]
    assert calls[1] == [GCODE, "pin", "--quiet", "--project", str(workspace)]
    assert "code_index_pin" in phase_timings


@pytest.mark.asyncio
async def test_pin_failure_forces_cold_index_without_parent_fallthrough(
    tmp_path: Path,
) -> None:
    # A semantic selection miss exits zero: gcode records the cold pin itself, and the
    # preflight indexes the workspace locally without naming any parent project.
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    calls: list[list[str]] = []

    with patch("gobby.agents.code_index._run_gcode", side_effect=_recording_runner(calls)):
        await ensure_isolation_code_index(str(workspace), gcode_bin=Path(GCODE))

    subcommands = _subcommands(calls)
    assert subcommands.index("pin") < subcommands.index("index")
    index_call = calls[subcommands.index("index")]
    assert index_call == [GCODE, "index", "--quiet", "--project", str(workspace)]
    for args in calls:
        if args[1] in {"pin", "index"}:
            assert args[-1] == str(workspace)


@pytest.mark.asyncio
async def test_pin_storage_failure_blocks_success(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    calls: list[list[str]] = []
    pin_error = GcodeCommandError(
        "gcode_pin_failed:1:pin transaction rolled back", output="database unavailable"
    )

    with (
        patch(
            "gobby.agents.code_index._run_gcode",
            side_effect=_recording_runner(calls, pin_error=pin_error),
        ),
        pytest.raises(GcodeCommandError, match="gcode_pin_failed"),
    ):
        await ensure_isolation_code_index(str(workspace), gcode_bin=Path(GCODE))

    assert _subcommands(calls) == ["status", "pin"]


BASE_COMMIT = "a" * 40


async def _create_direct_worktree(
    tmp_path: Path, pinner: AsyncMock
) -> tuple[WorktreeCreationResult, MagicMock]:
    git_manager = MagicMock()
    git_manager.repo_path = str(tmp_path / "repo")
    git_manager.create_worktree = AsyncMock(return_value=MagicMock(success=True, error=None))
    git_manager.delete_worktree = AsyncMock()
    storage = MagicMock()
    storage.get_by_branch.return_value = None
    storage.create.return_value = MagicMock(
        id="wt-1",
        worktree_path=str(tmp_path / "worktree"),
        branch_name="seat-branch",
        project_id="project-1",
        base_branch="main",
        task_id=None,
    )
    with patch(
        "gobby.worktrees.creation.select_worktree_base",
        AsyncMock(return_value=("main", BASE_COMMIT)),
    ):
        result = await create_worktree(
            git_manager=git_manager,
            worktree_storage=storage,
            project_id="project-1",
            branch_name="seat-branch",
            worktree_path=str(tmp_path / "worktree"),
            sidecar_writer=AsyncMock(),
            pinner=pinner,
            event_emitter=lambda *_args, **_kwargs: {},
        )
    return result, storage


@pytest.mark.asyncio
async def test_direct_paths_pin_before_return(tmp_path: Path) -> None:
    # Direct worktree creation pins its checkout once, before the success return.
    pinner = AsyncMock()
    result, _storage = await _create_direct_worktree(tmp_path, pinner)
    assert result.success, result.error
    pinner.assert_awaited_once_with(tmp_path / "worktree")

    # A pin failure rolls back the worktree record and reports a typed code.
    failing_pinner = AsyncMock(side_effect=GcodeGatewayError("gcode_pin_failed:1:pin failed"))
    result, storage = await _create_direct_worktree(tmp_path, failing_pinner)
    assert not result.success
    assert result.error_code == "code_index_pin_failed"
    storage.delete.assert_called_once_with("wt-1")

    # Direct clone creation writes the isolation marker, then pins, before success.
    order: list[str] = []
    marker = AsyncMock(side_effect=lambda *_args, **_kwargs: order.append("marker"))
    pin = AsyncMock(side_effect=lambda *_args, **_kwargs: order.append("pin"))
    clone_git = MagicMock()
    clone_git.repo_path = str(tmp_path / "repo")
    clone_git.create_clone = AsyncMock(return_value=MagicMock(success=True))
    clone_git.get_remote_url = AsyncMock(return_value=None)
    clone_storage = MagicMock()
    clone_storage.create.return_value = MagicMock(to_dict=MagicMock(return_value={"id": "clone-1"}))
    registry = create_clones_registry(
        clone_storage=clone_storage,
        git_manager=clone_git,
        project_id="11111111-1111-4111-8111-111111110001",
    )
    clone_path = tmp_path / "clone"
    with (
        patch(
            "gobby.mcp_proxy.tools._clones_creation.select_worktree_base",
            AsyncMock(return_value=("main", BASE_COMMIT)),
        ),
        patch("gobby.mcp_proxy.tools._clones_creation.WorktreeGitManager"),
        patch(
            "gobby.mcp_proxy.tools._clones_creation.ensure_project_json_for_isolation",
            marker,
        ),
        patch.object(GcodeGateway, "pin_project", pin),
    ):
        clone = await registry.call(
            "create_clone", {"branch_name": "main", "clone_path": str(clone_path)}
        )
    assert clone["success"] is True, clone
    marker.assert_awaited_once_with(
        str(tmp_path / "repo"), str(clone_path), base_commit=BASE_COMMIT
    )
    pin.assert_awaited_once_with(clone_path)
    assert order == ["marker", "pin"]


@pytest.mark.asyncio
async def test_clone_pin_failure_rolls_back_clone(tmp_path: Path) -> None:
    # A pin failure deletes the new clone directory and writes no clone record.
    clone_git = MagicMock()
    clone_git.repo_path = str(tmp_path / "repo")
    clone_git.create_clone = AsyncMock(return_value=MagicMock(success=True))
    clone_git.get_remote_url = AsyncMock(return_value=None)
    clone_git.delete_clone = AsyncMock(return_value=MagicMock(success=True))
    clone_storage = MagicMock()
    registry = create_clones_registry(
        clone_storage=clone_storage,
        git_manager=clone_git,
        project_id="11111111-1111-4111-8111-111111110001",
    )
    clone_path = tmp_path / "clone"
    failing_pin = AsyncMock(side_effect=GcodeGatewayError("gcode_pin_failed:1:pin failed"))
    with (
        patch(
            "gobby.mcp_proxy.tools._clones_creation.select_worktree_base",
            AsyncMock(return_value=("main", BASE_COMMIT)),
        ),
        patch("gobby.mcp_proxy.tools._clones_creation.WorktreeGitManager"),
        patch(
            "gobby.mcp_proxy.tools._clones_creation.ensure_project_json_for_isolation",
            AsyncMock(),
        ),
        patch.object(GcodeGateway, "pin_project", failing_pin),
    ):
        result = await registry.call(
            "create_clone", {"branch_name": "main", "clone_path": str(clone_path)}
        )
    assert result["success"] is False
    assert "gcode_pin_failed" in result["error"]
    clone_git.create_clone.assert_awaited_once()
    clone_git.delete_clone.assert_awaited_once_with(str(clone_path), force=True)
    clone_storage.create.assert_not_called()
