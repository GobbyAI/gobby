"""Interactive and placed-seat quota observation (plan 4.5).

Placed terminals without an AgentRun feed the shared capacity service as hints only,
through the same observation helper and sink that spawned runs use. Observation never
sends keys, never mutates quota state, and never fails or completes a session.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from gobby.agents.interactive_attention_monitor import InteractiveAttentionMonitor
from gobby.providers.capacity_service import ProviderCapacityService
from gobby.providers.quota_alerts import AlertEdge
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.coordination_waits import CoordinationWaitManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.provider_capacity import ProviderCapacityStorage
from gobby.storage.sessions import SessionManager
from tests.agents.detection_test_support import BundledDetectionRegistry
from tests.agents.test_lifecycle_monitor import LifecycleRuntime, _pane_text
from tests.agents.test_lifecycle_monitor_watchdog_idle_recovery import (
    LOCAL_MACHINE_ID,
    _make_idle_monitor_run,
)
from tests.fixtures.isolated_checkout import patch_local_machine_id
from tests.terminals.fakes import make_memory_terminal, runtime_registry

pytestmark = pytest.mark.unit

DETECTION_REGISTRY = BundledDetectionRegistry()
_BASE = "❯ build\nrunning checks\n> step one\n"
_FORMS: dict[str, str] = {
    "claude": "You've hit your limit · resets 5pm\n",
    "codex": "You've hit your usage limit. Try again at 10:30 AM.\n",
    "grok": "Usage balance exhausted\n",
    "droid": "Standard usage limit reached\n",
    "agy": "Individual quota reached. Resets in 1h\n",
    "qwen": "quota exhausted: daily limit\nwill reset at 9pm\n",
}
_NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _local_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_local_machine_id(monkeypatch, LOCAL_MACHINE_ID)


@pytest.fixture
def agent_run_manager(temp_db: HubDatabase) -> LocalAgentRunManager:
    return LocalAgentRunManager(temp_db)


class _Sink:
    """Records every edge the shared alert sink receives."""

    def __init__(self) -> None:
        self.edges: list[AlertEdge] = []

    async def __call__(self, edge: AlertEdge) -> None:
        self.edges.append(edge)

    def hints(self) -> list[AlertEdge]:
        return [edge for edge in self.edges if edge.kind == "possible_limit"]


def _service(
    temp_db: HubDatabase, sink: _Sink, now: Callable[[], datetime] | None = None
) -> ProviderCapacityService:
    return ProviderCapacityService(
        ProviderCapacityStorage(temp_db, machine_id=LOCAL_MACHINE_ID),
        support_resolvers={},
        alert_sink=sink,
        clock=now or (lambda: _NOW),
    )


def _admissions(monkeypatch: pytest.MonkeyPatch, service: ProviderCapacityService) -> list[bool]:
    """Record each hint the service admits; replays and cooldown silences still count as calls."""
    admitted: list[bool] = []
    original = service.observe_pane_hint

    async def spy(hint: Any, *, generation: str, owner: str = "") -> bool:
        result = await original(hint, generation=generation, owner=owner)
        admitted.append(result)
        return result

    monkeypatch.setattr(service, "observe_pane_hint", spy)
    return admitted


async def _settle() -> None:
    for _ in range(10):
        await asyncio.sleep(0)


def _session(session_id: str, provider: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=session_id,
        source=provider,
        terminal_context={},
        updated_at=datetime.now(UTC),
        transcript_path=None,
    )


def _interactive(
    sessions: list[SimpleNamespace], runtime: LifecycleRuntime, service: ProviderCapacityService
) -> InteractiveAttentionMonitor:
    session_manager = Mock(db=Mock())
    session_manager.list.return_value = sessions
    return InteractiveAttentionMonitor(
        detection_registry=DETECTION_REGISTRY,
        session_manager=session_manager,
        attention_manager=None,
        registry=runtime_registry(runtime),
        provider_capacity_service=service,
    )


def _rows(monkeypatch: pytest.MonkeyPatch, rows: dict[str, Any]) -> None:
    monkeypatch.setattr(
        "gobby.storage.terminals.TerminalManager.get_live_for_session",
        lambda _manager, session_id: rows.get(session_id),
    )


def _spawned(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    agent_run_manager: LocalAgentRunManager,
    *,
    child_source: str,
) -> Any:
    monitor, _run = _make_idle_monitor_run(
        temp_db=temp_db,
        session_manager=session_manager,
        sample_project=sample_project,
        agent_run_manager=agent_run_manager,
        run_id=str(uuid.uuid4()),
        transcript_path=None,
        child_source=child_source,
        session_age_seconds=120,
    )
    return monitor


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", list(_FORMS))
async def test_all_provider_interactive_signals(
    temp_db: HubDatabase, monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    sink = _Sink()
    service = _service(temp_db, sink)
    admitted = _admissions(monkeypatch, service)
    session = _session(f"{provider}-placed", provider)
    _rows(monkeypatch, {session.id: make_memory_terminal(backend="tmux")})
    runtime = LifecycleRuntime(backend="tmux")
    runtime.snapshot_effects = [_BASE, _BASE + _FORMS[provider]]
    monitor = _interactive([session], runtime, service)

    await monitor._check_attention_panes(active_agent_sessions=set())
    assert admitted == []
    await monitor._check_attention_panes(active_agent_sessions=set())
    await _settle()

    assert admitted == [True]
    assert [edge.provider for edge in sink.hints()] == [provider]
    assert [edge.kind for edge in sink.edges] == ["possible_limit"]
    assert runtime.write_log == []


@pytest.mark.asyncio
async def test_interactive_observation_guards(
    temp_db: HubDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    sink = _Sink()
    service = _service(temp_db, sink)
    admitted = _admissions(monkeypatch, service)
    form = _FORMS["codex"]
    session = _session("codex-placed", "codex")
    row = make_memory_terminal(backend="tmux")
    _rows(monkeypatch, {session.id: row})
    runtime = LifecycleRuntime(backend="tmux")

    # First capture after attach baselines retained history; an identical redraw is silent.
    runtime.snapshot_effects = [_BASE + form, _BASE + form]
    monitor = _interactive([session], runtime, service)
    await monitor._check_attention_panes(active_agent_sessions=set())
    await monitor._check_attention_panes(active_agent_sessions=set())
    assert admitted == []

    # Restart: a fresh monitor baselines the visible banner instead of reporting it.
    runtime.snapshot_effects = [_BASE + form]
    restarted = _interactive([session], runtime, service)
    await restarted._check_attention_panes(active_agent_sessions=set())
    assert admitted == []

    # A complete new block after overlap is the only admitted shape.
    runtime.snapshot_effects = [_BASE + form + form]
    await restarted._check_attention_panes(active_agent_sessions=set())
    assert admitted == [True]

    # Ownership: a replacement terminal starts a new generation with its own baseline.
    _rows(monkeypatch, {session.id: make_memory_terminal(backend="tmux")})
    runtime.snapshot_effects = [_BASE + form + form]
    await restarted._check_attention_panes(active_agent_sessions=set())
    assert admitted == [True]

    # Overlap: a capture that shares no prefix with the baseline rebaselines silently.
    runtime.snapshot_effects = [_BASE + form, "other screen\nentirely\nunrelated\n" + form]
    await restarted._check_attention_panes(active_agent_sessions=set())
    await restarted._check_attention_panes(active_agent_sessions=set())
    assert admitted == [True]

    # Timeout: the failed capture ends the baseline, so the next capture only baselines.
    runtime.snapshot_effects = [TimeoutError("tmux capture timed out"), _BASE + form + form]
    await restarted._check_attention_panes(active_agent_sessions=set())
    await restarted._check_attention_panes(active_agent_sessions=set())
    assert admitted == [True]

    # Spawned terminals belong to the 4.4 owner and are never scanned here.
    modes_before = len(runtime.snapshot_modes)
    await restarted._check_attention_panes(active_agent_sessions={session.id})
    assert len(runtime.snapshot_modes) == modes_before

    # Shutdown leaves no poller behind.
    await restarted.start()
    await restarted.stop()
    assert restarted._task is None


@pytest.mark.asyncio
async def test_shared_provider_edge_across_seats(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    agent_run_manager: LocalAgentRunManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = {"now": _NOW}
    sink = _Sink()
    service = _service(temp_db, sink, lambda: clock["now"])
    admitted = _admissions(monkeypatch, service)
    spawned = _spawned(
        temp_db, session_manager, sample_project, agent_run_manager, child_source="codex"
    )
    spawned.set_provider_capacity_service(service)
    placed = _session("codex-placed", "codex")
    placed_row = make_memory_terminal(backend="tmux")
    _rows(monkeypatch, {placed.id: placed_row})
    placed_runtime = LifecycleRuntime(backend="tmux")
    interactive = _interactive([placed], placed_runtime, service)
    form = _FORMS["codex"]

    async def one_pass(spawned_text: str, placed_text: str) -> None:
        placed_runtime.snapshot_effects = [placed_text]
        with _pane_text(spawned, [spawned_text]):
            await spawned.check_attention_agents(reuse_for_idle=True)
            await interactive._check_attention_panes(active_agent_sessions=set())
        await _settle()

    # Both seats baseline, then both see the same new block in one pass. The second admission
    # is recorded silently inside the cooldown.
    await one_pass(_BASE, _BASE)
    await one_pass(_BASE + form, _BASE + form)
    assert admitted == [True, True]
    assert [edge.provider for edge in sink.hints()] == ["codex"]

    # Replay: the baseline returns and the identical occurrence reappears.
    await one_pass(_BASE, _BASE)
    await one_pass(_BASE + form, _BASE + form)
    assert admitted[-2:] == [False, False]
    assert len(sink.hints()) == 1

    # Disappearance: the placed terminal leaves; nothing is admitted and quota is untouched.
    _rows(monkeypatch, {placed.id: None})
    await one_pass(_BASE, _BASE)
    assert len(sink.hints()) == 1
    assert {edge.kind for edge in sink.edges} == {"possible_limit"}

    # Cooldown expiry: a replay past the window still stays silent, and only a new occurrence
    # notifies again, as a possible limit.
    clock["now"] = _NOW + timedelta(seconds=901)
    _rows(monkeypatch, {placed.id: placed_row})
    await one_pass(_BASE + form, _BASE + form)
    assert len(sink.hints()) == 1
    await one_pass(_BASE + form + form, _BASE + form + form)
    assert len(sink.hints()) == 2
    assert {edge.kind for edge in sink.edges} == {"possible_limit"}


@pytest.mark.asyncio
async def test_restart_keeps_placed_digests_past_spawned_retirement(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    agent_run_manager: LocalAgentRunManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = {"now": _NOW}
    sink = _Sink()
    form = _FORMS["codex"]
    placed = _session("codex-placed", "codex")
    _rows(monkeypatch, {placed.id: make_memory_terminal(backend="tmux")})

    # Before restart: the placed seat notifies once and its occurrence digest is persisted.
    before = _service(temp_db, sink, lambda: clock["now"])
    first_runtime = LifecycleRuntime(backend="tmux")
    first_runtime.snapshot_effects = [_BASE, _BASE + form]
    first = _interactive([placed], first_runtime, before)
    await first._check_attention_panes(active_agent_sessions=set())
    await first._check_attention_panes(active_agent_sessions=set())
    await _settle()
    assert len(sink.hints()) == 1

    # Restart after the cooldown. The spawned scan reports its live set before the placed seat
    # registers after restart, and the placed seat then replays the same occurrence.
    clock["now"] = _NOW + timedelta(seconds=901)
    after = _service(temp_db, sink, lambda: clock["now"])
    admitted = _admissions(monkeypatch, after)
    spawned = _spawned(
        temp_db, session_manager, sample_project, agent_run_manager, child_source="codex"
    )
    spawned.set_provider_capacity_service(after)
    restart_runtime = LifecycleRuntime(backend="tmux")
    restart_runtime.snapshot_effects = [_BASE, _BASE + form]
    restarted = _interactive([placed], restart_runtime, after)
    with _pane_text(spawned, [_BASE]):
        await spawned.check_attention_agents(reuse_for_idle=True)
    await restarted._check_attention_panes(active_agent_sessions=set())
    await restarted._check_attention_panes(active_agent_sessions=set())
    await _settle()

    # The persisted occurrence is still a duplicate, so the replay stays silent.
    assert admitted == [False]
    assert len(sink.hints()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", list(_FORMS))
async def test_placed_observation_runs_under_coordination_hold(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
) -> None:
    sink = _Sink()
    service = _service(temp_db, sink)
    admitted = _admissions(monkeypatch, service)
    owner = session_manager.register(
        external_id=f"{provider}-hold-owner",
        machine_id=LOCAL_MACHINE_ID,
        source="claude",
        project_id=sample_project["id"],
    )
    seat = session_manager.register(
        external_id=f"{provider}-held-seat",
        machine_id=LOCAL_MACHINE_ID,
        source=provider,
        project_id=sample_project["id"],
    )
    CoordinationWaitManager(temp_db).register(seat.id, owner.id, coordination_key="hold")
    assert CoordinationWaitManager(temp_db).has_active_wait(seat.id)
    seated = session_manager.get(seat.id)
    assert seated is not None
    status_before = seated.status
    session = _session(seat.id, provider)
    _rows(monkeypatch, {session.id: make_memory_terminal(backend="tmux")})
    runtime = LifecycleRuntime(backend="tmux")
    runtime.snapshot_effects = [_BASE, _BASE + _FORMS[provider]]
    monitor = _interactive([session], runtime, service)

    await monitor._check_attention_panes(active_agent_sessions=set())
    await monitor._check_attention_panes(active_agent_sessions=set())
    await _settle()

    # The hold stays in place and observation still runs; the session is neither keyed nor failed.
    assert admitted == [True]
    assert [edge.provider for edge in sink.hints()] == [provider]
    assert runtime.write_log == []
    assert CoordinationWaitManager(temp_db).has_active_wait(seat.id)
    still_seated = session_manager.get(seat.id)
    assert still_seated is not None
    assert still_seated.status == status_before


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", list(_FORMS))
async def test_quoted_bottom_form_yields_hint_only(
    temp_db: HubDatabase, monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    sink = _Sink()
    service = _service(temp_db, sink)
    admitted = _admissions(monkeypatch, service)
    session = _session(f"{provider}-quoted", provider)
    _rows(monkeypatch, {session.id: make_memory_terminal(backend="tmux")})
    runtime = LifecycleRuntime(backend="tmux")
    # The banner arrives as quoted text at the bottom of a newly appended block.
    runtime.snapshot_effects = [_BASE, _BASE + "> " + _FORMS[provider]]
    monitor = _interactive([session], runtime, service)

    await monitor._check_attention_panes(active_agent_sessions=set())
    await monitor._check_attention_panes(active_agent_sessions=set())
    await _settle()

    # A quoted copy is equally uncertain: one possible-limit hint and nothing else.
    assert admitted == [True]
    assert [edge.provider for edge in sink.hints()] == [provider]
    assert [edge.kind for edge in sink.edges] == ["possible_limit"]
    assert runtime.write_log == []
