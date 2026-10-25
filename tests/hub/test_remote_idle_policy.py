"""The hub applies its idle policy to node runs from the facts their node reports."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Literal, cast
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from gobby.agents.agent_cleanup import AgentCleanupHandler
from gobby.agents.idle_check_handler import IdleCheckHandler
from gobby.agents.idle_detector import IdleDetector
from gobby.agents.prompt_detector import PromptDetector
from gobby.agents.stall_classifier import StallClassifier
from gobby.agents.watchdog import WatchdogReaderRegistry
from gobby.config.tmux import TmuxConfig
from gobby.hub import remote_cancel, remote_pane, remote_runtime, remote_terminal
from gobby.hub.node_commands import ReportAgent
from gobby.storage.agents import AgentRun, LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.terminals import Terminal
from gobby.terminals.composer_ledger import ComposerLedger
from gobby.terminals.leases import TerminalLeaseRegistry
from gobby.terminals.services import TerminalServices
from gobby.terminals.write_coordinator import UnresolvedWriteStore, WriteCoordinator
from gobby.utils.machine_id import require_machine_id
from tests.agents.detection_test_support import BundledDetectionRegistry
from tests.agents.test_capture import FakeCaptureStorage
from tests.terminals.fakes import (
    FakeRuntime,
    MemoryTerminalStore,
    make_memory_terminal,
    runtime_registry,
)

pytestmark = pytest.mark.unit

REMOTE = "30000000-0000-4000-8000-000000000003"
MAX_REPROMPTS = 2
_STAMP = datetime(2026, 1, 1, tzinfo=UTC)
# A tail the pane detectors read as neither idle, quota nor context-full, so only the
# store's idle block can drive a node run's decision.
NEUTRAL_TAIL = "Reading files...\n"
LOCAL_IDLE_PANE = "❯\n"


class _Node:
    """Scripted node command client that records every command it receives."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, dict[str, Any]]] = []
        self.replies: list[dict[str, Any]] = []

    async def send(
        self,
        machine_id: str,
        command: str,
        payload: dict[str, Any],
        timeout_ms: int = 30000,
    ) -> dict[str, Any]:
        self.sent.append((machine_id, command, payload))
        return self.replies.pop(0) if self.replies else {}

    def cancels(self, terminal: Terminal) -> list[str]:
        return [
            payload["signal"]
            for machine, command, payload in self.sent
            if (machine, command) == (REMOTE, "agent.cancel")
            and payload["terminal_id"] == terminal.id
        ]

    def texts(self, terminal: Terminal) -> list[str]:
        return [
            payload["data"]
            for machine, command, payload in self.sent
            if (machine, command) == (REMOTE, "terminal.write")
            and payload["terminal_id"] == terminal.id
            and payload["kind"] == "text"
        ]


class _Runs(FakeCaptureStorage):
    """The hub's run table: capture-and-kill storage plus active runs listed by machine."""

    def list_active_for_machine(self, machine_id: str) -> list[AgentRun]:
        return [
            run
            for run in self.runs.values()
            if run.machine_id == machine_id and run.status in ("pending", "running")
        ]


@dataclass(frozen=True)
class _Entry:
    """One agent in a node report: its terminal, idle facts and composer state."""

    terminal: Terminal
    idle: dict[str, Any] | None
    composer: Literal["empty", "draft", "unknown"] = "empty"


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


def _terminal(machine_id: str, backend: Literal["tmux", "native"] = "native") -> Terminal:
    return replace(
        make_memory_terminal(backend=backend, machine_id=machine_id),
        agent_run_id=str(uuid4()),
    )


def _run(terminal: Terminal) -> AgentRun:
    return AgentRun(
        id=cast(str, terminal.agent_run_id),
        parent_session_id=str(uuid4()),
        child_session_id=None,
        provider="claude",
        prompt="do it",
        status="running",
        created_at=_STAMP,
        updated_at=_STAMP,
        machine_id=terminal.machine_id,
        terminal_id=terminal.id,
    )


def _idle(
    *,
    status: str = "idle",
    unsubmitted_input: bool = False,
    quota: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "turn_in_flight": False,
        "unsubmitted_input": unsubmitted_input,
        "queued_message_prompt": False,
        "capacity_message": None,
        "quota": quota,
    }


def _report(*entries: _Entry, machine_id: str = REMOTE) -> None:
    """Store one node report carrying the given agents' panes, all with a neutral tail."""
    agents = [
        ReportAgent.model_validate(
            {
                "run_id": entry.terminal.agent_run_id,
                "terminal_id": entry.terminal.id,
                "pid": 4242,
                "locator": f"pane-{entry.terminal.id}",
                "locator_key": f"host-1:{entry.terminal.id}",
                "workspace": None,
                "pane": {
                    "attention": None,
                    "injections": 0,
                    "composer": {"state": entry.composer, "line": None},
                    "idle": entry.idle,
                    "tail": NEUTRAL_TAIL,
                },
                "sandbox": None,
            }
        )
        for entry in entries
    ]
    remote_pane.remote_pane_store().apply_report(machine_id, 1, agents)


class _Harness:
    """One idle handler over a memory terminal store that holds the given terminals."""

    def __init__(
        self, db: HubDatabase, monkeypatch: pytest.MonkeyPatch, *terminals: Terminal
    ) -> None:
        store = MemoryTerminalStore(db=db)
        for terminal in terminals:
            store.rows[terminal.id] = terminal
        self.runs = _Runs(*(_run(terminal) for terminal in terminals))
        # The remote runtime resolves the run it kills through the hub's run table.
        monkeypatch.setattr(remote_runtime, "LocalAgentRunManager", lambda _db: self.runs)
        self.local = FakeRuntime(backend="tmux")
        registry = runtime_registry(self.local)
        services = TerminalServices(
            manager=store,
            registry=registry,
            coordinator=WriteCoordinator(
                cast(UnresolvedWriteStore, store),
                registry,
                lease_registry=TerminalLeaseRegistry(daemon_epoch="test-epoch"),
            ),
        )
        detection = BundledDetectionRegistry()
        self.detector = IdleDetector(detection, "claude")
        self.cleanup_agent = AsyncMock()
        cleanup = MagicMock(spec=AgentCleanupHandler)
        cleanup.cleanup_agent = self.cleanup_agent

        async def run_db(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
            return func(*args, **kwargs)

        self.handler = IdleCheckHandler(
            agent_run_manager=cast(LocalAgentRunManager, self.runs),
            db=db,
            get_session_manager=lambda: None,
            idle_detector=self.detector,
            prompt_detector=PromptDetector(detection, "claude"),
            stall_classifier=cast(StallClassifier, MagicMock(spec=StallClassifier)),
            watchdog_readers=WatchdogReaderRegistry(),
            cleanup_handler=cleanup,
            tmux_config=TmuxConfig(
                idle_timeout_seconds=60,
                idle_reprompt_delay_seconds=60,
                max_reprompt_attempts=MAX_REPROMPTS,
            ),
            run_db=run_db,
            terminal_services=services,
        )
        self.sync = AsyncMock()
        monkeypatch.setattr(self.handler.attention_tracker, "sync", self.sync)

    def run(self, terminal: Terminal) -> AgentRun:
        run = self.runs.get(cast(str, terminal.agent_run_id))
        assert run is not None
        return run

    def idle_long_ago(self, terminal: Terminal, *, reprompts: int = 0) -> None:
        state = self.detector.get_state(cast(str, terminal.agent_run_id))
        state.first_idle_at = time.monotonic() - 10_000
        state.reprompt_count = reprompts

    def reprompts(self, terminal: Terminal) -> int:
        return self.detector.get_state(cast(str, terminal.agent_run_id)).reprompt_count

    def idle_tracked(self, terminal: Terminal) -> bool:
        return self.detector.get_state(cast(str, terminal.agent_run_id)).first_idle_at is not None

    def synced(self, terminal: Terminal) -> bool:
        return any(call.args[0].id == terminal.agent_run_id for call in self.sync.await_args_list)


@pytest.mark.asyncio
async def test_idle_node_run_reprompted(
    temp_db: HubDatabase, node: _Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    terminal = _terminal(REMOTE)
    harness = _Harness(temp_db, monkeypatch, terminal)
    _report(_Entry(terminal, _idle(status="idle")))
    harness.idle_long_ago(terminal)

    handled = await harness.handler.check_idle_agents()

    assert handled == 1
    assert node.texts(terminal) == [IdleDetector.REPROMPT_MESSAGE]
    assert harness.reprompts(terminal) == 1
    assert node.cancels(terminal) == []
    assert harness.local.write_log == []
    harness.cleanup_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_node_run_failed_after_max_reprompts(
    temp_db: HubDatabase, node: _Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    terminal = _terminal(REMOTE)
    harness = _Harness(temp_db, monkeypatch, terminal)
    _report(_Entry(terminal, _idle(status="idle")))
    harness.idle_long_ago(terminal, reprompts=MAX_REPROMPTS)
    node.replies = [{"stopped": True}]

    handled = await harness.handler._handle_idle_check(harness.run(terminal))

    assert handled == 1
    assert node.cancels(terminal) == ["TERM"]
    assert node.texts(terminal) == []
    harness.cleanup_agent.assert_awaited_once()
    assert harness.local.write_log == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("idle", "terminal_reason"),
    [
        pytest.param(
            _idle(status="idle", quota={"provider": "claude", "reset_time": None}),
            "provider_quota_exhausted",
            id="reported-quota",
        ),
        pytest.param(_idle(status="context_full"), None, id="context-full"),
    ],
)
async def test_quota_and_context_full_fail_node_run(
    temp_db: HubDatabase,
    node: _Node,
    monkeypatch: pytest.MonkeyPatch,
    idle: dict[str, Any],
    terminal_reason: str | None,
) -> None:
    terminal = _terminal(REMOTE)
    harness = _Harness(temp_db, monkeypatch, terminal)
    _report(_Entry(terminal, idle))
    harness.idle_long_ago(terminal)
    node.replies = [{"stopped": True}]

    handled = await harness.handler._handle_idle_check(harness.run(terminal))

    assert handled == 1
    assert node.cancels(terminal) == ["TERM"]
    assert node.texts(terminal) == []
    harness.cleanup_agent.assert_awaited_once()
    assert harness.cleanup_agent.await_args is not None
    assert harness.cleanup_agent.await_args.kwargs.get("terminal_reason") == terminal_reason


@pytest.mark.asyncio
async def test_draft_and_missing_entry_hold(
    temp_db: HubDatabase, node: _Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    drafted = _terminal(REMOTE)
    unreported = _terminal(REMOTE)
    no_idle_facts = _terminal(REMOTE)
    harness = _Harness(temp_db, monkeypatch, drafted, unreported, no_idle_facts)
    # The node reports a held draft for one run and a pane without idle facts for
    # another; the third run's terminal is absent from the report.
    _report(
        _Entry(drafted, _idle(status="idle", unsubmitted_input=True), composer="draft"),
        _Entry(no_idle_facts, None),
    )
    for terminal in (drafted, unreported, no_idle_facts):
        harness.idle_long_ago(terminal, reprompts=1)

    handled = await harness.handler.check_idle_agents()

    assert handled == 0
    assert node.sent == []
    harness.cleanup_agent.assert_not_awaited()
    # The reported draft is read as unsubmitted input, which resets idle tracking.
    assert harness.reprompts(drafted) == 0
    assert not harness.idle_tracked(drafted)
    # A run with no entry, or no idle facts, is skipped untouched and unsynced.
    for skipped in (unreported, no_idle_facts):
        assert harness.reprompts(skipped) == 1
        assert harness.idle_tracked(skipped)
        assert not harness.synced(skipped)

    # Checked directly, the unreported run is skipped the same way.
    assert await harness.handler._handle_idle_check(harness.run(unreported)) == 0
    assert harness.reprompts(unreported) == 1
    assert harness.idle_tracked(unreported)
    assert not harness.synced(unreported)
    assert node.sent == []


@pytest.mark.asyncio
async def test_local_idle_unchanged(
    temp_db: HubDatabase,
    node: _Node,
    monkeypatch: pytest.MonkeyPatch,
    composer_ledger: ComposerLedger,
) -> None:
    local_machine = require_machine_id()
    terminal = _terminal(local_machine, backend="tmux")
    harness = _Harness(temp_db, monkeypatch, terminal)
    composer_ledger.record_spawn(terminal.id, "")
    harness.local.snapshot_text = LOCAL_IDLE_PANE
    # A store entry under this machine's id carries facts that would hold or fail the
    # run; a local run's facts come from its own pane, so the entry changes nothing.
    _report(
        _Entry(terminal, _idle(status="context_full"), composer="draft"),
        machine_id=local_machine,
    )
    harness.idle_long_ago(terminal)

    handled = await harness.handler.check_idle_agents()

    assert handled == 1
    assert node.sent == []
    assert ("text", IdleDetector.REPROMPT_MESSAGE) in harness.local.write_log
    assert harness.reprompts(terminal) == 1
    assert harness.synced(terminal)
    harness.cleanup_agent.assert_not_awaited()

    # Out of reprompts, the same local run is failed through its local terminal.
    harness.idle_long_ago(terminal, reprompts=MAX_REPROMPTS)

    assert await harness.handler._handle_idle_check(harness.run(terminal)) == 1
    harness.cleanup_agent.assert_awaited_once()
    assert node.sent == []
