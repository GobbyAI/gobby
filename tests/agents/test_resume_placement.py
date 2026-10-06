"""Placed resume re-places against current state (placed-agent-launch 1.7)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.agents import resume_executor, spawn_executor, spawn_in_doubt_owner
from gobby.agents.resume_executor import ResumeAgentResult
from gobby.agents.sandbox import SandboxConfig
from gobby.agents.srt_runtime import SandboxLaunch, SrtRuntimeError
from gobby.mcp_proxy.tools.spawn_agent import _implementation as impl
from gobby.storage.agents import AgentRun
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.terminals.in_doubt import in_doubt_spawns
from gobby.terminals.runtime import TerminalSpawnRequest
from tests.mcp_proxy.tools.spawn_agent.test_placement import (
    LOCAL_MACHINE_ID,
    SEAT,
    _assert_pane_held,
    _assert_seat_free,
    _build,
    _drain_owners,
    _ExecOrderedRuntime,
    _Harness,
    _held_seat,
    _panes,
    _spawn,
    _split,
    _tab,
    _tabs,
    _terminal_states,
)
from tests.terminals.fakes import FakeRuntime

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("stub_srt_verifier")]


@pytest.fixture(autouse=True)
def _local_machine_identity(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(impl, "get_machine_id", lambda: LOCAL_MACHINE_ID)
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


@pytest.fixture(autouse=True)
def _instant_owner_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_wait(delay: float) -> None:
        del delay
        await asyncio.sleep(0)

    monkeypatch.setattr(spawn_in_doubt_owner, "_sleep", no_wait)


@pytest.fixture
def finalize(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncMock:
    """Stub the resume side effects outside the placed launch; return the handoff spy."""
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "home"))
    handoff = AsyncMock()
    monkeypatch.setattr(resume_executor, "finalize_resume_handoff_async", handoff)
    monkeypatch.setattr("gobby.agents.resume_finalization.finalize_resume_handoff_async", handoff)
    monkeypatch.setattr(resume_executor, "notify_parent_of_recovery", MagicMock())
    monkeypatch.setattr(resume_executor, "_fire_resume_started", MagicMock())
    monkeypatch.setattr(resume_executor, "pre_approve_directory", lambda *_args: None)
    monkeypatch.setattr(
        resume_executor,
        "prepare_sandbox_launch",
        AsyncMock(return_value=SandboxLaunch(backend="srt", enforced=True)),
    )
    return handoff


@dataclass
class _HeldRuntime(FakeRuntime):
    """Signals when the provider prepare starts, then waits on ``spawn_hold``."""

    entered: asyncio.Event = field(default_factory=asyncio.Event)

    async def prepare_spawn(self, request: TerminalSpawnRequest) -> Any:
        self.entered.set()
        return await super().prepare_spawn(request)


def _harness(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runtime: FakeRuntime,
) -> _Harness:
    return _build(
        db=temp_db,
        project=sample_project,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        runtimes=(runtime,),
    )


@pytest.fixture
def placed(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> _Harness:
    return _harness(temp_db, sample_project, tmp_path, monkeypatch, FakeRuntime(backend="native"))


def _parked_original(h: _Harness) -> AgentRun:
    """A placed agent run stopped by a daemon stop, with a real child session."""
    child = h.sessions.register(
        external_id=f"resume-child-{uuid.uuid4()}",
        machine_id=LOCAL_MACHINE_ID,
        source="claude",
        project_id=h.project_id,
        parent_session_id=h.parent_id,
    )
    original = h.runs.create(
        parent_session_id=h.parent_id,
        child_session_id=child.id,
        provider="claude",
        prompt="Run the seat",
    )
    h.db.execute("UPDATE sessions SET agent_run_id = %s WHERE id = %s", (original.id, child.id))
    h.runs.start(original.id)
    parked = h.runs.cancel(original.id, terminal_reason="daemon_stop")
    assert parked is not None
    return parked


def _tab_snapshot(h: _Harness) -> dict[str, Any]:
    return {
        "kind": "tab",
        "workspace_id": h.workspace.id,
        "title": SEAT,
        "beside_pane_id": None,
        "axis": None,
    }


def _metadata(
    h: _Harness, placement: dict[str, Any] | None, *, backend: str = "srt"
) -> dict[str, Any]:
    return {
        "provider": "claude",
        "provider_native_session_id": "native-123",
        "cwd": h.project_path,
        "project_id": h.project_id,
        "parent_session_id": h.parent_id,
        "machine_id": LOCAL_MACHINE_ID,
        "sandbox_config": SandboxConfig(enabled=True, backend=backend).model_dump(),
        "worktree_id": h.checkout_mode.worktree_id,
        "placement": placement,
    }


async def _resume(
    h: _Harness,
    original: AgentRun,
    metadata: dict[str, Any],
    *,
    reserver: Any = ...,
    session_manager: Any = None,
) -> ResumeAgentResult:
    return await resume_executor.resume_agent_run(
        original,
        resume_metadata=metadata,
        runner=h.runner,
        session_manager=session_manager or h.sessions,
        agent_pane_reserver=h.reserver if reserver is ... else reserver,
    )


def _successor_status(h: _Harness, run_id: str | None) -> tuple[str, str | None]:
    assert run_id is not None
    run = h.runs.get(run_id)
    assert run is not None
    return run.status, run.terminal_reason


@pytest.mark.parametrize("kind", ["tab", "split"])
async def test_snapshot_carries_validated_placement(placed: _Harness, kind: str) -> None:
    h = placed
    beside = _held_seat(h, "beside", state="exited")
    raw = _tab(h) if kind == "tab" else _split(beside.id)

    placed_reply = await _spawn(h, raw)
    unplaced_reply = await _spawn(h, None)
    await asyncio.gather(*list(impl._spawn_background_tasks.values()))

    assert placed_reply["success"] is True, placed_reply
    run = h.runs.get(placed_reply["run_id"])
    assert run is not None and run.resume_metadata_json is not None
    assert run.resume_metadata_json["placement"] == {
        "kind": kind,
        "workspace_id": h.workspace.id,
        "title": SEAT,
        "beside_pane_id": None if kind == "tab" else beside.id,
        "axis": None if kind == "tab" else "horizontal",
    }
    unplaced = h.runs.get(unplaced_reply["run_id"])
    assert unplaced is not None and unplaced.resume_metadata_json is not None
    assert "placement" not in unplaced.resume_metadata_json


async def test_placed_resume_replaces_before_exec(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    finalize: AsyncMock,
) -> None:
    order: list[str] = []
    h = _harness(
        temp_db,
        sample_project,
        tmp_path,
        monkeypatch,
        _ExecOrderedRuntime(backend="native", order=order),
    )
    for name in ("preflight", "reserve", "bind"):
        real = getattr(h.reserver, name)

        async def recording(*args: Any, _name: str = name, _real: Any = real, **kw: Any) -> Any:
            order.append(_name)
            return await _real(*args, **kw)

        monkeypatch.setattr(h.reserver, name, recording)

    result = await _resume(h, _parked_original(h), _metadata(h, _tab_snapshot(h)))

    assert result.success is True, result.error
    assert order == ["preflight", "reserve", "bind", "exec"]
    [terminal_id] = _terminal_states(h)
    assert _terminal_states(h) == {terminal_id: "live"}
    assert list(_panes(h).values()) == [terminal_id]
    assert len(_tabs(h)) == 1
    assert _successor_status(h, result.run_id) == ("running", None)
    assert finalize.await_count == 1
    _assert_seat_free(h)
    _assert_pane_held(h)


@pytest.mark.parametrize(
    "case",
    [
        "missing",
        "moved",
        "forbidden",
        "parent_unresolved",
        "sandbox_required",
        "no_reserver",
        "wrap_failure",
    ],
)
async def test_placed_resume_refusals_park_successor(
    placed: _Harness,
    monkeypatch: pytest.MonkeyPatch,
    finalize: AsyncMock,
    case: str,
) -> None:
    h = placed
    snapshot = _tab_snapshot(h)
    backend = "srt"
    reserver: Any = ...
    session_manager: Any = None
    expected = f"placement_error:{case}"
    if case == "missing":
        snapshot["workspace_id"] = str(uuid.uuid4())
        expected = "placement_error:not_found"
    elif case == "moved":
        # The beside pane now lives in another workspace than the one recorded.
        beside = _held_seat(h, "beside", state="exited")
        other = h.workspaces.create(LOCAL_MACHINE_ID, "elsewhere")[0]
        snapshot = {
            "kind": "split",
            "workspace_id": other.id,
            "title": SEAT,
            "beside_pane_id": beside.id,
            "axis": "horizontal",
        }
        expected = "placement_error:not_found"
    elif case == "forbidden":
        outsider = LocalProjectManager(h.db).create(name="resume-outsider")
        change = h.workspaces.create_tab(
            h.workspace.id, pane_id=str(uuid.uuid4()), project_id=outsider.id, title="foreign"
        )
        snapshot = {
            "kind": "split",
            "workspace_id": h.workspace.id,
            "title": SEAT,
            "beside_pane_id": change.panes[0].id,
            "axis": "horizontal",
        }
    elif case == "parent_unresolved":
        sessions = h.sessions
        session_manager = SimpleNamespace(
            get=lambda session_id: None if session_id == h.parent_id else sessions.get(session_id)
        )
    elif case == "sandbox_required":
        # The managed-SRT resume gate refuses before placement is resolved.
        backend = "provider-native"
        expected = "sandbox_required"
    elif case == "no_reserver":
        reserver = None
        expected = "placement_error:placement_unavailable"
    elif case == "wrap_failure":

        def refuse(launch: SandboxLaunch, command: list[str]) -> list[str]:
            raise SrtRuntimeError("srt policy rejected")

        monkeypatch.setattr(spawn_executor, "wrap_provider_command", refuse)
        expected = "resume_spawn_failed:SrtRuntimeError:srt policy rejected"
    original = _parked_original(h)
    panes, terminals, tabs = _panes(h), _terminal_states(h), _tabs(h)

    result = await _resume(
        h,
        original,
        _metadata(h, snapshot, backend=backend),
        reserver=reserver,
        session_manager=session_manager,
    )

    assert result.success is False
    assert result.error == expected
    assert _successor_status(h, result.run_id) == ("cancelled", "daemon_stop")
    assert finalize.await_count == 1
    assert _terminal_states(h) == terminals
    assert _panes(h) == panes and _tabs(h) == tabs
    _assert_seat_free(h)


@pytest.mark.parametrize("exit_kind", ["failed", "cancelled"])
async def test_placed_resume_cleanup_once(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    finalize: AsyncMock,
    exit_kind: str,
) -> None:
    hold = asyncio.Event()
    runtime = _HeldRuntime(backend="native", spawn_hold=hold, typed_fail=exit_kind == "failed")
    h = _harness(temp_db, sample_project, tmp_path, monkeypatch, runtime)
    if exit_kind == "failed":
        hold.set()
    releases: list[str | None] = []
    real_release = h.reserver.release

    async def counting_release(reserved: Any, *, terminal_id: str | None) -> None:
        releases.append(terminal_id)
        await real_release(reserved, terminal_id=terminal_id)

    monkeypatch.setattr(h.reserver, "release", counting_release)
    original = _parked_original(h)

    resume = asyncio.create_task(_resume(h, original, _metadata(h, _tab_snapshot(h))))
    if exit_kind == "cancelled":
        # The placed executor answers one cancellation with a cancelled result (1.2).
        await asyncio.wait_for(runtime.entered.wait(), timeout=5)
        resume.cancel()
    result = await resume
    hold.set()
    await _drain_owners()

    assert result.success is False
    assert _successor_status(h, result.run_id) == ("cancelled", "daemon_stop")

    assert runtime.create_calls == 1
    [terminal_id] = _terminal_states(h)
    assert releases == [terminal_id]
    assert finalize.await_count == 1
    assert _terminal_states(h) == {terminal_id: "exited"}
    h.workspaces.sweep_dead_panes(h.workspace.id)
    assert _panes(h) == {} and _tabs(h) == set()
    _assert_seat_free(h)


async def test_placed_resume_cancel_during_preflight_parks_successor(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    finalize: AsyncMock,
) -> None:
    runtime = _HeldRuntime(backend="native", spawn_hold=asyncio.Event())
    h = _harness(temp_db, sample_project, tmp_path, monkeypatch, runtime)
    entered, hold = asyncio.Event(), asyncio.Event()
    real_preflight = h.reserver.preflight

    async def held_preflight(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        await hold.wait()
        return await real_preflight(*args, **kwargs)

    reserve = AsyncMock(side_effect=h.reserver.reserve)
    monkeypatch.setattr(h.reserver, "preflight", held_preflight)
    monkeypatch.setattr(h.reserver, "reserve", reserve)
    original = _parked_original(h)

    resume = asyncio.create_task(_resume(h, original, _metadata(h, _tab_snapshot(h))))
    await asyncio.wait_for(entered.wait(), timeout=5)
    for _ in range(3):
        resume.cancel()
        await asyncio.sleep(0)
    with pytest.raises(asyncio.CancelledError):
        await resume

    [successor] = h.db.fetchall(
        "SELECT id FROM agent_runs WHERE parent_session_id = %s AND id <> %s",
        (h.parent_id, original.id),
    )
    assert _successor_status(h, successor["id"]) == ("cancelled", "daemon_stop")
    assert finalize.await_count == 1
    reserve.assert_not_awaited()
    assert runtime.create_calls == 0
    assert _panes(h) == {} and _tabs(h) == set()
    _assert_seat_free(h)


async def test_placed_resume_cancel_keeps_in_doubt_owner(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    finalize: AsyncMock,
) -> None:
    hold = asyncio.Event()
    runtime = _HeldRuntime(backend="native", spawn_hold=hold)
    h = _harness(temp_db, sample_project, tmp_path, monkeypatch, runtime)
    original = _parked_original(h)

    resume = asyncio.create_task(_resume(h, original, _metadata(h, _tab_snapshot(h))))
    await asyncio.wait_for(runtime.entered.wait(), timeout=5)
    for _ in range(3):
        resume.cancel()
        await asyncio.sleep(0)
    with pytest.raises(asyncio.CancelledError):
        await resume

    [terminal_id] = _terminal_states(h)
    assert _terminal_states(h) == {terminal_id: "pending"}
    assert in_doubt_spawns.holds(terminal_id)
    [pane_id] = _panes(h)
    assert _panes(h) == {pane_id: terminal_id}
    _assert_pane_held(h)
    assert finalize.await_count == 1

    hold.set()
    await _drain_owners()

    assert not in_doubt_spawns.holds(terminal_id)
    assert runtime.create_calls == 1
    assert _terminal_states(h) == {terminal_id: "exited"}
    h.workspaces.sweep_dead_panes(h.workspace.id)
    assert _panes(h) == {}
    _assert_seat_free(h)
