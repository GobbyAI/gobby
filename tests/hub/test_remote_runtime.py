"""A terminal on another machine is read, stopped, woken and gated through its node, never locally."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from gobby.agents.capture import terminate_managed_runtime_async
from gobby.agents.watchdog.composer_gate import composer_refuses_automation
from gobby.events.live_wake import composer_occupied_result
from gobby.events.wake import CONTINUE_WAKE_MESSAGE, NativeWakeTarget, WakeDispatcher
from gobby.hub import remote_cancel, remote_pane, remote_runtime, remote_terminal
from gobby.hub.node_client import NodeCommandError
from gobby.hub.node_commands import ReportAgent
from gobby.hub.remote_runtime import RemoteTerminalRuntime
from gobby.runner_init.orchestration import _send_remote_wake, _send_tmux_session_wake
from gobby.storage.agents import AgentRun
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.terminals import Terminal, TerminalManager
from gobby.terminals.composer_ledger import ComposerLedger
from gobby.terminals.leases import TerminalLeaseRegistry
from gobby.terminals.runtime import (
    Delivered,
    PreparedSpawn,
    TerminalSpawnRequest,
    TerminalWriteError,
)
from gobby.terminals.services import TerminalServices
from gobby.terminals.write_coordinator import UnresolvedWriteStore, WriteCoordinator, WriteRequest
from tests.agents.test_capture import FakeCaptureStorage
from tests.terminals.fakes import (
    FakeRuntime,
    MemoryTerminalStore,
    make_memory_terminal,
    runtime_registry,
)

pytestmark = pytest.mark.unit

REMOTE = "30000000-0000-4000-8000-000000000003"
WAKE_SESSION_ID = "9264a39c-68db-5eed-917c-6f7babb8e6b1"
NATIVE_TERMINAL_CONTEXT = {"parent_pid": 4242, "term_program": "gterm"}
_STAMP = datetime(2026, 1, 1, tzinfo=UTC)


class _Node:
    """Scripted node command client: queued replies, then a sticky failure if one is set."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, dict[str, Any]]] = []
        self.replies: list[dict[str, Any]] = []
        self.failure: NodeCommandError | None = None

    async def send(
        self,
        machine_id: str,
        command: str,
        payload: dict[str, Any],
        timeout_ms: int = 30000,
    ) -> dict[str, Any]:
        self.sent.append((machine_id, command, payload))
        if self.failure is not None:
            raise self.failure
        return self.replies.pop(0) if self.replies else {}


class _Runs:
    """Stands in for the hub's run table: one run, found by id."""

    def __init__(self, run: AgentRun) -> None:
        self._run = run

    def get(self, run_id: str) -> AgentRun | None:
        return self._run if run_id == self._run.id else None


@dataclass
class _Session:
    id: str
    terminal_context: object
    agent_depth: int = 0
    status: str = "paused"
    turn_count: int = 0
    session_type: str = "terminal"
    project_id: str | None = None


@pytest.fixture(autouse=True)
def pane_store() -> Iterator[None]:
    remote_pane.remote_pane_store.cache_clear()
    yield
    remote_pane.remote_pane_store.cache_clear()


@pytest.fixture
def node(monkeypatch: pytest.MonkeyPatch) -> _Node:
    fake = _Node()
    monkeypatch.setattr(remote_cancel, "node_command_client", lambda: fake)
    monkeypatch.setattr(remote_terminal, "node_command_client", lambda: fake)
    return fake


def _node_terminal(agent_run_id: str | None = None) -> Terminal:
    return replace(
        make_memory_terminal(backend="native", machine_id=REMOTE),
        agent_run_id=agent_run_id,
    )


def _run(run_id: str, terminal_id: str) -> AgentRun:
    return AgentRun(
        id=run_id,
        parent_session_id=str(uuid4()),
        child_session_id=None,
        provider="claude",
        prompt="do it",
        status="running",
        created_at=_STAMP,
        updated_at=_STAMP,
        machine_id=REMOTE,
        terminal_id=terminal_id,
    )


def _pane(*, tail: str = "", composer: str = "empty") -> dict[str, Any]:
    return {
        "attention": None,
        "injections": 0,
        "composer": {"state": composer, "line": None},
        "idle": {
            "status": "idle",
            "turn_in_flight": False,
            "unsubmitted_input": False,
            "queued_message_prompt": False,
            "capacity_message": None,
            "quota": None,
        },
        "tail": tail,
    }


def _report(run_id: str, terminal_id: str, *, tail: str = "", composer: str = "empty") -> None:
    agent = ReportAgent.model_validate(
        {
            "run_id": run_id,
            "terminal_id": terminal_id,
            "pid": 4242,
            "locator": "pane-1",
            "locator_key": "host-1:pane-1",
            "workspace": None,
            "pane": _pane(tail=tail, composer=composer),
            "sandbox": None,
        }
    )
    remote_pane.remote_pane_store().apply_report(REMOTE, 1, [agent])


def _patch_runs(monkeypatch: pytest.MonkeyPatch, run: AgentRun) -> None:
    monkeypatch.setattr(remote_runtime, "LocalAgentRunManager", lambda _db: _Runs(run))


def _services(terminal: Terminal) -> TerminalServices:
    return TerminalServices(
        manager=MemoryTerminalStore(terminal),
        registry=runtime_registry(FakeRuntime(backend="native")),
    )


def _clean(ledger: ComposerLedger, terminal_id: str) -> None:
    ledger.record_spawn(terminal_id, "")


def _draft(ledger: ComposerLedger, terminal_id: str) -> None:
    _clean(ledger, terminal_id)
    ledger.observe_write(terminal_id, origin="operator", kind="text", payload="operator draft")


def test_runtime_for_routes_by_machine(temp_db: HubDatabase) -> None:
    native = FakeRuntime(backend="native")
    tmux = FakeRuntime(backend="tmux")
    services = TerminalServices(
        manager=TerminalManager(temp_db),
        registry=runtime_registry(tmux, native),
    )

    assert services.runtime_for(make_memory_terminal(backend="native")) is native
    assert isinstance(
        services.runtime_for(make_memory_terminal(backend="native", machine_id=REMOTE)),
        RemoteTerminalRuntime,
    )


@pytest.mark.asyncio
async def test_liveness_and_snapshot_from_store(temp_db: HubDatabase) -> None:
    terminal = _node_terminal(str(uuid4()))
    runtime = RemoteTerminalRuntime(temp_db)

    assert await runtime.is_live(terminal) is False
    assert await runtime.session_present(terminal) is False
    missing = await runtime.snapshot(terminal, 2, mode="text")
    assert missing.text == ""

    _report(cast(str, terminal.agent_run_id), terminal.id, tail="one\ntwo\nthree")
    assert await runtime.is_live(terminal) is True
    assert await runtime.session_present(terminal) is True
    last_two = await runtime.snapshot(terminal, 2, mode="text")
    assert last_two.text == "two\nthree"
    assert last_two.truncated is False
    assert last_two.dropped_bytes is None
    assert last_two.total_bytes is None
    assert (await runtime.snapshot_full(terminal)).text == "one\ntwo\nthree"

    remote_pane.remote_pane_store().mark_stopped(terminal.id)
    assert await runtime.is_live(terminal) is False
    stopped = await runtime.snapshot(terminal, 2, mode="text")
    assert stopped.text == ""


@pytest.mark.asyncio
async def test_terminate_goes_through_node(
    temp_db: HubDatabase, node: _Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = str(uuid4())
    terminal = _node_terminal(run_id)
    run = _run(run_id, terminal.id)
    _patch_runs(monkeypatch, run)
    runtime = RemoteTerminalRuntime(temp_db)
    _report(run_id, terminal.id, tail="final line")

    node.replies = [{"stopped": False}, {"stopped": True}]
    await runtime.terminate(terminal, grace_seconds=0)

    assert [payload["signal"] for _, _, payload in node.sent] == ["TERM", "KILL"]
    assert node.sent[0] == (
        REMOTE,
        "agent.cancel",
        {"run_id": run_id, "terminal_id": terminal.id, "signal": "TERM", "close_terminal": True},
    )
    assert await runtime.session_present(terminal) is False

    # Capture-before-kill reads the stored tail and reports the kill as done.
    remote_pane.remote_pane_store.cache_clear()
    _report(run_id, terminal.id, tail="final line")
    node.replies = [{"stopped": True}]
    storage = FakeCaptureStorage(run)
    stopped = await terminate_managed_runtime_async(
        storage=storage, run=run, terminal=terminal, runtime=runtime, action="cancel"
    )
    assert stopped.success is True
    assert "final line" in (storage.runs[run_id].result or "")

    # An offline node leaves the terminal live, and the kill reports failure.
    remote_pane.remote_pane_store.cache_clear()
    _report(run_id, terminal.id, tail="final line")
    node.failure = NodeCommandError("node_offline", "Target has no live channel", "none")
    offline = await terminate_managed_runtime_async(
        storage=FakeCaptureStorage(run),
        run=run,
        terminal=terminal,
        runtime=runtime,
        action="cancel",
    )
    assert offline.success is False
    assert await runtime.session_present(terminal) is True


@pytest.mark.parametrize(
    ("local", "composer", "stopped", "seed", "refuses"),
    [
        pytest.param(False, "empty", False, _draft, False, id="node-empty-ignores-ledger-draft"),
        pytest.param(False, "draft", False, _clean, True, id="node-draft"),
        pytest.param(False, "unknown", False, _clean, True, id="node-unknown"),
        pytest.param(False, None, False, _clean, True, id="node-missing-entry"),
        pytest.param(False, "empty", True, _clean, True, id="node-stopped-entry"),
        pytest.param(True, "draft", False, _clean, False, id="local-clean-ignores-store-draft"),
        pytest.param(True, "empty", False, _draft, True, id="local-draft"),
    ],
)
def test_remote_composer_gate(
    composer_ledger: ComposerLedger,
    local: bool,
    composer: str | None,
    stopped: bool,
    seed: Callable[[ComposerLedger, str], None],
    refuses: bool,
) -> None:
    terminal = make_memory_terminal(backend="native") if local else _node_terminal()
    run_id = str(uuid4())
    run = _run(run_id, terminal.id)
    seed(composer_ledger, terminal.id)
    if composer is not None:
        _report(run_id, terminal.id, composer=composer)
    if stopped:
        remote_pane.remote_pane_store().mark_stopped(terminal.id)

    assert composer_refuses_automation(_services(terminal), run) is refuses


@pytest.mark.asyncio
async def test_remote_runtime_refuses_local_operations(temp_db: HubDatabase) -> None:
    runtime = RemoteTerminalRuntime(temp_db)
    terminal = _node_terminal()

    with pytest.raises(TerminalWriteError) as text:
        await runtime.write_text(terminal, "ls", True)
    assert text.value.stage == "none"
    with pytest.raises(TerminalWriteError):
        await runtime.write_key(terminal, "enter")
    with pytest.raises(TerminalWriteError):
        await runtime.write_input(terminal, b"ls\n")
    with pytest.raises(TerminalWriteError):
        await runtime.write_paste(terminal, "ls")
    # The refusals never read their arguments, so a None placeholder is enough here.
    with pytest.raises(NotImplementedError):
        await runtime.prepare_spawn(cast(TerminalSpawnRequest, None))
    with pytest.raises(NotImplementedError):
        await runtime.commit_spawn(cast(PreparedSpawn, None))
    with pytest.raises(NotImplementedError):
        await runtime.resize(terminal, 24, 80)
    with pytest.raises(NotImplementedError):
        await runtime.attach_locator(terminal)


def _coordinator(store: MemoryTerminalStore, native: FakeRuntime) -> WriteCoordinator:
    return WriteCoordinator(
        cast(UnresolvedWriteStore, store),
        runtime_registry(native),
        lease_registry=TerminalLeaseRegistry(daemon_epoch="test-epoch"),
    )


def _wake_dispatcher(store: MemoryTerminalStore, batch: AsyncMock) -> WakeDispatcher:
    session_manager = MagicMock()
    session_manager.get.return_value = _Session(
        id=WAKE_SESSION_ID, terminal_context=NATIVE_TERMINAL_CONTEXT
    )
    return WakeDispatcher(
        session_manager=session_manager,
        ism_manager=MagicMock(),
        tmux_sender=_send_tmux_session_wake,
        native_batch_sender=batch,
        terminal_manager=store,
        remote_wake_sender=_send_remote_wake,
    )


@pytest.mark.asyncio
async def test_wake_to_node_terminal_writes_through_the_node(
    node: _Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    terminal = replace(_node_terminal(), session_id=WAKE_SESSION_ID)
    store = MemoryTerminalStore(terminal)
    native = FakeRuntime(backend="native")
    monkeypatch.setattr(
        "gobby.runner_init.orchestration.wake_write_services",
        lambda: (store, _coordinator(store, native)),
    )
    _report(str(uuid4()), terminal.id, composer="empty")
    batch = AsyncMock(return_value=[])

    result = await _wake_dispatcher(store, batch).dispatch_live_wake(WAKE_SESSION_ID)

    assert result["delivered"] is True
    assert node.sent == [
        (
            REMOTE,
            "terminal.write",
            {
                "terminal_id": terminal.id,
                "kind": "text",
                "data": CONTINUE_WAKE_MESSAGE,
                "submit": False,
            },
        ),
        (
            REMOTE,
            "terminal.write",
            {"terminal_id": terminal.id, "kind": "key", "data": "enter", "submit": False},
        ),
    ]
    batch.assert_not_awaited()
    assert native.write_log == []


@pytest.mark.asyncio
async def test_draft_node_composer_withholds_the_wake(
    node: _Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    terminal = replace(_node_terminal(), session_id=WAKE_SESSION_ID)
    store = MemoryTerminalStore(terminal)
    native = FakeRuntime(backend="native")
    monkeypatch.setattr(
        "gobby.runner_init.orchestration.wake_write_services",
        lambda: (store, _coordinator(store, native)),
    )
    _report(str(uuid4()), terminal.id, composer="draft")
    batch = AsyncMock(return_value=[])

    result = await _wake_dispatcher(store, batch).dispatch_live_wake(WAKE_SESSION_ID)

    assert result == composer_occupied_result(WAKE_SESSION_ID, method="terminal")
    assert node.sent == []
    batch.assert_not_awaited()
    assert native.write_log == []


class _ScriptedCoordinator:
    """Delivers every write except one terminal's, which fails outside the typed errors."""

    def __init__(self, failing_terminal_id: str) -> None:
        self._failing = failing_terminal_id

    async def write(
        self, request: WriteRequest, *, on_dispatch: Callable[[], None] | None = None
    ) -> Delivered:
        if request.terminal_id == self._failing:
            raise RuntimeError("row store unavailable")
        if on_dispatch is not None:
            on_dispatch()
        return Delivered()


@pytest.mark.asyncio
async def test_one_failed_node_wake_does_not_fail_the_others(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failing_id, delivered_id = str(uuid4()), str(uuid4())
    monkeypatch.setattr(
        "gobby.runner_init.orchestration.wake_write_services",
        lambda: (None, _ScriptedCoordinator(failing_id)),
    )
    targets = [
        NativeWakeTarget(
            session_id=str(uuid4()), terminal_id=failing_id, cli_source=None, drain=False
        ),
        NativeWakeTarget(
            session_id=str(uuid4()), terminal_id=delivered_id, cli_source=None, drain=False
        ),
    ]

    results = await _send_remote_wake(targets)

    assert [result["delivered"] for result in results] == [False, True]
    assert results[0]["error_code"] == "remote_wake_failed"
