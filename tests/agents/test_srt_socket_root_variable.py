"""The SRT run's socket root reaches the rule engine as a daemon-written session variable (#25365).

block-temp-scratchpad-writes admits writes under the socket root that a macOS SRT launch
granted its own run. The variable is recorded from the launch that granted it, withdrawn
when a relaunch grants none, reserved from agent writes, and carried to a sandboxed clear
successor. That successor runs in the same process under the same grant.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.agents import resume_executor, spawn_executor_providers
from gobby.agents.srt_runtime import (
    SandboxLaunch,
    inherit_run_socket_root,
    record_run_socket_root,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.reserved_variables import is_reserved_workflow_variable
from gobby.workflows.state_manager import SessionVariableManager
from tests.agents.test_resume_executor import (
    _original_run,
    _patch_common,
    _resume_metadata,
    _runner,
    _spawn_result,
)
from tests.agents.test_spawn_executor_providers import _sandbox_request
from tests.fixtures.landing_case import make_landing_case
from tests.fixtures.team_case import TeamWorld, make_team_world

pytestmark = pytest.mark.unit

SOCKET_ROOT_VARIABLE = "_agent_srt_socket_root"
RUN_ROOT = "/private/var/folders/5w/9cmg71vd2m108t5r_fb77l0h0000gn/T/gobby-aaaa1111"
SEAT = "fixture-worker"


@pytest.fixture
def world(
    temp_db: HubDatabase,
    sample_project: dict[str, object],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> TeamWorld:
    """A project with real session rows; Gobby home sits under the test's HOME."""
    world = make_team_world(
        make_landing_case(temp_db, sample_project, tmp_path), tmp_path, monkeypatch
    )
    home = (tmp_path / "home").resolve()
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("GOBBY_HOME", str(home / ".gobby"))
    return world


def _socket_root(db: HubDatabase, session_id: str) -> dict[str, object]:
    return SessionVariableManager(db).get_variable_subset(session_id, (SOCKET_ROOT_VARIABLE,))


def _srt_launch(root: str | None = RUN_ROOT) -> SandboxLaunch:
    return SandboxLaunch(
        backend="srt",
        enforced=True,
        provider_executable="/managed/provider",
        policy_path="/policy/settings.json",
        violation_path="/policy/violations.jsonl",
        node_path="/managed/node",
        runner_path="/managed/runner.mjs",
        run_socket_root=root,
    )


def test_socket_root_variable_is_reserved_from_agent_writes() -> None:
    assert is_reserved_workflow_variable(SOCKET_ROOT_VARIABLE)


@pytest.mark.parametrize(
    "name",
    ["_agent_workspace_dir", "_agent_workspace_epic_dir", "_agent_workspace_persistent"],
)
def test_workspace_binding_variables_are_reserved_from_agent_writes(name: str) -> None:
    # The rule admits the bound workspace folders too, so a seat must not rebind them.
    assert is_reserved_workflow_variable(name)


def test_launch_socket_root_is_recorded_on_the_session(world: TeamWorld) -> None:
    db = world.case.db
    session = world.case.session(SEAT)

    record_run_socket_root(db, session, _srt_launch())

    assert _socket_root(db, session) == {SOCKET_ROOT_VARIABLE: RUN_ROOT}


def test_relaunch_without_a_socket_root_withdraws_the_stale_one(world: TeamWorld) -> None:
    db = world.case.db
    session = world.case.session(SEAT)
    record_run_socket_root(db, session, _srt_launch())
    assert _socket_root(db, session) == {SOCKET_ROOT_VARIABLE: RUN_ROOT}

    record_run_socket_root(db, session, SandboxLaunch(backend="provider-native", enforced=False))

    assert _socket_root(db, session) == {}


def test_sandboxed_clear_successor_inherits_the_socket_root(world: TeamWorld) -> None:
    db = world.case.db
    predecessor = world.case.session(SEAT)
    successor = world.case.session(SEAT)
    db.execute("UPDATE sessions SET sandbox_enabled = TRUE WHERE id = %s", (predecessor,))
    record_run_socket_root(db, predecessor, _srt_launch())

    inherit_run_socket_root(db, successor, predecessor)

    assert _socket_root(db, successor) == {SOCKET_ROOT_VARIABLE: RUN_ROOT}


def test_unsandboxed_clear_successor_does_not_inherit_a_socket_root(world: TeamWorld) -> None:
    db = world.case.db
    predecessor = world.case.session(SEAT)
    successor = world.case.session(SEAT)
    SessionVariableManager(db).merge_variables(predecessor, {SOCKET_ROOT_VARIABLE: RUN_ROOT})

    inherit_run_socket_root(db, successor, predecessor)

    assert _socket_root(db, successor) == {}


@pytest.mark.asyncio
async def test_spawn_records_the_launch_socket_root_on_the_child_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, spawn_context, run_manager = _sandbox_request()
    request.session_manager = MagicMock()
    # The seat has no assigned task worktree, so the run's worktree lookup finds no row.
    request.session_manager._storage.db.fetchone.return_value = None
    request.machine_id = "machine"
    request.project_path = "/workspace"
    request.resume_metadata_json = {}
    launch = _srt_launch()
    monkeypatch.setattr(
        "gobby.storage.project_checkouts.require_root", MagicMock(return_value="/workspace")
    )
    monkeypatch.setattr(
        spawn_executor_providers, "prepare_sandbox_launch", AsyncMock(return_value=launch)
    )
    record = MagicMock()
    monkeypatch.setattr(spawn_executor_providers, "record_run_socket_root", record)

    result = await spawn_executor_providers._prepare_provider_sandbox(
        request, spawn_context, "codex", {}
    )

    assert result is launch
    assert run_manager.fail.call_count == 0
    record.assert_called_once()
    assert record.call_args.args[1] == spawn_context.session_id
    assert record.call_args.args[2] is launch


@pytest.mark.asyncio
@pytest.mark.usefixtures("stub_srt_verifier")
async def test_resume_records_the_relaunch_socket_root_on_the_child_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _runner()
    spawner = MagicMock()
    spawner.spawn.return_value = _spawn_result()
    _patch_common(monkeypatch, spawner=spawner, finalize=AsyncMock())
    launch = _srt_launch()
    monkeypatch.setattr(resume_executor, "prepare_sandbox_launch", AsyncMock(return_value=launch))
    record = MagicMock()
    monkeypatch.setattr(resume_executor, "record_run_socket_root", record)

    result = await resume_executor.resume_agent_run(
        _original_run(),
        resume_metadata=_resume_metadata(),
        runner=runner,
        session_manager=MagicMock(),
    )

    assert result.success is True
    record.assert_called_once()
    assert record.call_args.args[1] == _original_run().child_session_id
    assert record.call_args.args[2] is launch
