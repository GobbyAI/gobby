"""Placed spawns bind the terminal before exec and keep one in-doubt owner (#23010)."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest

from gobby.agents import spawn_executor, spawn_in_doubt_owner
from gobby.agents.lifecycle_reconciliation import LifecycleReconciliation
from gobby.agents.spawn_executor import (
    SpawnRequest,
    SpawnResult,
    execute_spawn,
    reap_stale_pending_terminals,
)
from gobby.agents.spawn_in_doubt_owner import OwnerStage
from gobby.agents.srt_runtime import SandboxLaunch
from gobby.mcp_proxy.tools.spawn_agent._failure_cleanup import (
    _cleanup_isolation_step,
    _terminate_spawn_process,
)
from gobby.storage.terminal_settlement import OrphanIdentity
from gobby.storage.terminals import Terminal, TerminalManager, mint_terminal_id
from gobby.terminals.host_client import HostUnavailableError
from gobby.terminals.in_doubt import in_doubt_spawns
from gobby.terminals.native_runtime import NativeTerminalRuntime
from gobby.terminals.runtime import (
    CommitSpawnRefusedError,
    PreparedSpawn,
    ProcessIdentity,
    TerminalHandle,
    TerminalRuntime,
    TerminalRuntimeRegistry,
    TerminalSpawnRequest,
)
from gobby.terminals.termination import TerminalInDoubtError, kill_terminal
from tests.agents.prepared_spawn import prepared_spawn
from tests.terminals.fakes import FakeRuntime, MemoryTerminalStore, runtime_registry

pytestmark = pytest.mark.unit

Binder = Callable[[str], Awaitable[None]]


@pytest.fixture(autouse=True)
def _instant_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_wait(delay: float) -> None:
        del delay
        await asyncio.sleep(0)

    monkeypatch.setattr(spawn_in_doubt_owner, "_sleep", no_wait)


@dataclass
class _Handoffs:
    stages: list[str] = field(default_factory=list)
    terminal_ids: list[str] = field(default_factory=list)


@pytest.fixture
def handoffs(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Handoffs]:
    seen = _Handoffs()
    schedule = spawn_executor._schedule_timeout_cleanup

    def recording(*args: Any, **kwargs: Any) -> None:
        if kwargs.get("in_doubt_owner"):
            seen.stages.append(kwargs["stage"])
            seen.terminal_ids.append(kwargs["terminal_id"])
        schedule(*args, **kwargs)

    monkeypatch.setattr(spawn_executor, "_schedule_timeout_cleanup", recording)
    yield seen


async def _drain_owners() -> None:
    """Wait for every retained late-cleanup task, including owners, to finish."""
    loop = asyncio.get_running_loop()
    for _ in range(50):
        # A task left by an earlier test's loop can never finish on this one.
        pending = [t for t in spawn_executor._TIMEOUT_CLEANUP_TASKS if t.get_loop() is loop]
        if not pending:
            return
        await asyncio.wait(pending, timeout=5)
    raise AssertionError("late cleanup did not finish")


class _OrderedStore(MemoryTerminalStore):
    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self.events = events

    def create_pending(self, *args: Any, **kwargs: Any) -> Terminal:
        self.events.append("create_pending")
        return super().create_pending(*args, **kwargs)


class _OrderedRuntime(FakeRuntime):
    events: list[str] | None = None

    async def reserve_observer(self, terminal_id: Any) -> Any:
        assert self.events is not None
        self.events.append("reserve_observer")
        return await super().reserve_observer(terminal_id)

    async def prepare_spawn(self, request: TerminalSpawnRequest) -> PreparedSpawn:
        assert self.events is not None
        self.events.append("prepare_spawn")
        return await super().prepare_spawn(request)


def _request(
    manager: MemoryTerminalStore,
    runtime: FakeRuntime,
    *,
    binder: Binder | None = None,
    **overrides: Any,
) -> SpawnRequest:
    fields: dict[str, Any] = {
        "prompt": "Test",
        "cwd": "/path",
        "provider": "claude",
        "session_id": "sess",
        "run_id": "run",
        "parent_session_id": "parent",
        "project_id": "proj",
        "session_manager": MagicMock(),
        "machine_id": "21000000-0000-4000-8000-000000000002",
        "prepared_spawn": prepared_spawn(),
        "terminal_backend": runtime.backend,
        "terminal_manager": cast(TerminalManager, manager),
        "terminal_runtime_registry": runtime_registry(runtime),
        "placement_binder": binder,
    }
    fields.update(overrides)
    return SpawnRequest(**fields)


def _record_wrap(monkeypatch: pytest.MonkeyPatch, events: list[str]) -> None:
    def wrap(launch: SandboxLaunch, command: list[str]) -> list[str]:
        del launch
        events.append("wrap_provider_command")
        return ["srt", "--", *command]

    monkeypatch.setattr(spawn_executor, "wrap_provider_command", wrap)


@pytest.mark.parametrize("backend", ["tmux", "native"])
async def test_bind_follows_wrap_and_precedes_exec(
    monkeypatch: pytest.MonkeyPatch, backend: str
) -> None:
    events: list[str] = []
    _record_wrap(monkeypatch, events)
    manager = _OrderedStore(events)
    runtime = _OrderedRuntime(backend=cast(Any, backend))
    runtime.events = events
    bound: list[tuple[str, str]] = []

    async def binder(terminal_id: str) -> None:
        row = manager.get(terminal_id)
        assert row is not None
        bound.append((terminal_id, row.state))
        events.append("bind")

    placed = await execute_spawn(_request(manager, runtime, binder=binder))

    exec_steps = ["reserve_observer", "prepare_spawn"] if backend == "native" else ["prepare_spawn"]
    assert placed.success is True
    assert events == ["wrap_provider_command", "create_pending", "bind", *exec_steps]
    assert bound == [(placed.terminal_id, "pending")]
    assert runtime.last_request is not None
    assert runtime.last_request.command[:2] == ["srt", "--"]

    events.clear()
    unplaced = await execute_spawn(_request(manager, runtime))

    assert unplaced.success is True
    assert events == ["wrap_provider_command", "create_pending", *exec_steps]
    assert not in_doubt_spawns.holds(placed.terminal_id or "")


@pytest.mark.parametrize("backend", ["tmux", "native"])
@pytest.mark.parametrize(
    "failure",
    [
        RuntimeError("busy"),
        LookupError("not_found"),
        # A publish failure raised after set_pane_terminal persisted the binding.
        ConnectionError("publish failed after binding persisted"),
    ],
)
async def test_bind_failure_fails_pending_terminal(
    monkeypatch: pytest.MonkeyPatch, backend: str, failure: Exception
) -> None:
    manager = MemoryTerminalStore()
    runtime = FakeRuntime(backend=cast(Any, backend))
    settled: list[str] = []
    settle = spawn_executor._settle_native_spawn_failure

    async def recording_settle(**kwargs: Any) -> tuple[str, str | None]:
        settled.append(kwargs["terminal_id"])
        return await settle(**kwargs)

    monkeypatch.setattr(spawn_executor, "_settle_native_spawn_failure", recording_settle)

    async def binder(terminal_id: str) -> None:
        del terminal_id
        raise failure

    result = await execute_spawn(_request(manager, runtime, binder=binder))

    assert result.success is False
    assert result.status == "failed"
    assert result.error == str(failure)
    row = manager.get(result.terminal_id or "")
    assert row is not None
    assert row.state == "exited"
    assert runtime.create_calls == 0
    assert runtime.last_request is None
    assert settled == ([row.id] if backend == "native" else [])
    assert not in_doubt_spawns.holds(row.id)


class _StagedStore(MemoryTerminalStore):
    """A store whose create runs gated in its worker thread and can fail around the commit."""

    def __init__(self) -> None:
        super().__init__()
        self.create_gate: threading.Event | None = None
        self.create_entered = threading.Event()
        self.raise_before_commit: BaseException | None = None
        self.raise_after_commit: BaseException | None = None
        self.after_create: Callable[[], None] | None = None
        self.refuse_promotion = False
        # Settlement methods (and ``get``) that raise while listed: a storage outage.
        self.down: set[str] = set()
        # Settlement writes that answer as a CAS matching no row, per method.
        self.cas_misses: dict[str, int] = {}
        # Settlement writes that report success but never persist.
        self.lost_writes = 0
        self.writes: list[tuple[str, dict[str, Any]]] = []
        # Holds each settlement write inside its worker thread until set.
        self.write_gate: threading.Event | None = None
        self.write_entered = threading.Event()

    def _settling(self, name: str, kwargs: dict[str, Any]) -> bool:
        """Record one settlement write attempt; true when it answers as a CAS miss."""
        self.writes.append((name, kwargs))
        self.write_entered.set()
        if self.write_gate is not None:
            assert self.write_gate.wait(5)
        if name in self.down:
            raise ConnectionError(f"{name}: storage unavailable")
        misses = self.cas_misses.get(name, 0)
        self.cas_misses[name] = max(0, misses - 1)
        return misses > 0

    def _lost(self, terminal_id: str) -> Terminal | None:
        self.lost_writes -= 1
        row = self.rows.get(terminal_id)
        return None if row is None else replace(row, state="exited")

    def get(self, terminal_id: str) -> Terminal | None:
        if "get" in self.down:
            raise ConnectionError("get: storage unavailable")
        return super().get(terminal_id)

    def fail_pending_attempt(self, terminal_id: str, **kwargs: Any) -> Terminal | None:
        if self._settling("fail_pending_attempt", kwargs):
            return None
        if self.lost_writes:
            return self._lost(terminal_id)
        return super().fail_pending_attempt(terminal_id, **kwargs)

    def mark_exited_attempt(self, terminal_id: str, **kwargs: Any) -> Terminal | None:
        if self._settling("mark_exited_attempt", kwargs):
            return None
        if self.lost_writes:
            return self._lost(terminal_id)
        return super().mark_exited_attempt(terminal_id, **kwargs)

    def mark_kill_failed(self, terminal_id: str, **kwargs: Any) -> Terminal | None:
        if self._settling("mark_kill_failed", kwargs):
            return None
        return super().mark_kill_failed(terminal_id, **kwargs)

    def record_orphan_identity(self, terminal_id: str, **kwargs: Any) -> Terminal | None:
        if self._settling("record_orphan_identity", kwargs):
            return None
        return super().record_orphan_identity(terminal_id, **kwargs)

    def create_pending(self, *args: Any, **kwargs: Any) -> Terminal:
        self.create_entered.set()
        if self.create_gate is not None:
            assert self.create_gate.wait(5)
        if self.raise_before_commit is not None:
            raise self.raise_before_commit
        row = super().create_pending(*args, **kwargs)
        if self.after_create is not None:
            self.after_create()
        if self.raise_after_commit is not None:
            raise self.raise_after_commit
        return row

    def retry_attempt_unsettled(self, terminal_id: str, attempt_generation: int) -> Terminal | None:
        if self.raise_before_commit is not None:
            raise self.raise_before_commit
        row = super().retry_attempt_unsettled(terminal_id, attempt_generation)
        if self.raise_after_commit is not None:
            raise self.raise_after_commit
        return row

    def promote_to_live(self, terminal_id: str, **kwargs: Any) -> Terminal | None:
        if self.refuse_promotion:
            return None
        return super().promote_to_live(terminal_id, **kwargs)


@dataclass(eq=False)
class _StagedRuntime(FakeRuntime):
    """FakeRuntime with a hold or a failure at each spawn stage and at the kill."""

    reserve_hold: asyncio.Event | None = None
    reserve_started: asyncio.Event | None = None
    reserve_error: Exception | None = None
    observer_error: Exception | None = None
    commit_hold: asyncio.Event | None = None
    commit_started: asyncio.Event | None = None
    commit_error: Exception | None = None
    # Raised by prepare_spawn after the session exists (a lost response).
    fail_after_create: Exception | None = None
    terminate_error: Exception | None = None
    # Terminates that return without killing anything.
    ineffective_kills: int = 0
    terminate_calls: int = 0
    # Raised, in order, by session_present before it answers.
    present_failures: list[BaseException] = field(default_factory=list)
    prepare_process: ProcessIdentity | None = None
    # The host restarts once the prepare returns, making its epoch stale.
    epoch_after_prepare: str | None = None
    # The runtime whose real terminate performs the kill.
    kill_via: TerminalRuntime | None = None

    async def prepare_spawn(self, request: TerminalSpawnRequest) -> PreparedSpawn:
        prepared = await super().prepare_spawn(request)
        if self.epoch_after_prepare is not None:
            self.host_epoch = self.epoch_after_prepare
        if self.fail_after_create is not None:
            raise self.fail_after_create
        return replace(prepared, process=self.prepare_process)

    async def terminate(self, terminal: Terminal, grace_seconds: float) -> None:
        self.terminate_calls += 1
        self.terminate_started.set()
        if self.terminate_error is not None:
            raise self.terminate_error
        if self.ineffective_kills:
            self.ineffective_kills -= 1
            return
        if self.kill_via is not None:
            await self.kill_via.terminate(terminal, grace_seconds)
            return
        await super().terminate(terminal, grace_seconds)

    async def session_present(self, terminal: Terminal) -> bool:
        if self.present_failures:
            raise self.present_failures.pop(0)
        return await super().session_present(terminal)

    async def reserve_observer(self, terminal_id: UUID) -> Mapping[str, str]:
        if self.reserve_started is not None:
            self.reserve_started.set()
        if self.reserve_hold is not None:
            await self.reserve_hold.wait()
        if self.reserve_error is not None:
            raise self.reserve_error
        return await super().reserve_observer(terminal_id)

    async def bind_observer(self, prepared: PreparedSpawn, reservation_id: str) -> None:
        if self.observer_error is not None:
            raise self.observer_error
        await super().bind_observer(prepared, reservation_id)

    async def commit_spawn(self, prepared: PreparedSpawn) -> TerminalHandle:
        if self.commit_started is not None:
            self.commit_started.set()
        if self.commit_hold is not None:
            await self.commit_hold.wait()
        if self.commit_error is not None:
            raise self.commit_error
        return await super().commit_spawn(prepared)


@dataclass
class _Exit:
    """One exit: its store and runtime, how the caller ends, and where the row lands."""

    backend: str
    handed_off: bool
    final_state: str | None
    caller: str = "failed"
    store: _StagedStore = field(default_factory=_StagedStore)
    runtime: _StagedRuntime = field(default_factory=_StagedRuntime)
    binder_hold: asyncio.Event | None = None
    binder_started: asyncio.Event | None = None
    binder_error: Exception | None = None
    existing: Terminal | None = None
    overrides: dict[str, Any] = field(default_factory=dict)
    # Runs while the caller is blocked in the stage worker; returns what to release.
    blocked_at: Callable[[_Exit], Awaitable[Callable[[], None]]] | None = None


async def _blocked_in_create(case: _Exit) -> Callable[[], None]:
    await asyncio.to_thread(case.store.create_entered.wait, 5)
    gate = case.store.create_gate
    assert gate is not None
    return gate.set


async def _blocked_in_bind(case: _Exit) -> Callable[[], None]:
    assert case.binder_started is not None and case.binder_hold is not None
    await case.binder_started.wait()
    return case.binder_hold.set


async def _blocked_in_reserve(case: _Exit) -> Callable[[], None]:
    runtime = case.runtime
    assert runtime.reserve_started is not None and runtime.reserve_hold is not None
    await runtime.reserve_started.wait()
    return runtime.reserve_hold.set


async def _blocked_in_prepare(case: _Exit) -> Callable[[], None]:
    runtime = case.runtime
    assert runtime.spawn_hold is not None
    while runtime.create_calls == 0:
        await asyncio.sleep(0)
    return runtime.spawn_hold.set


async def _blocked_in_commit(case: _Exit) -> Callable[[], None]:
    runtime = case.runtime
    assert runtime.commit_started is not None and runtime.commit_hold is not None
    await runtime.commit_started.wait()
    return runtime.commit_hold.set


def _exit_case(name: str) -> _Exit:
    tmux, native = "tmux", "native"
    if name == "E1-commit-then-raise":
        case = _Exit(tmux, True, "exited", caller="raises")
        case.store.raise_after_commit = ConnectionError("commit acknowledgment lost")
    elif name == "E1-rolled-back":
        case = _Exit(tmux, True, None, caller="raises")
        case.store.raise_before_commit = ConnectionError("insert rolled back")
    elif name == "E2":
        case = _Exit(tmux, True, "exited", caller="cancelled", blocked_at=_blocked_in_create)
        case.store.create_gate = threading.Event()
    elif name == "E3":
        case = _Exit(tmux, False, "exited", caller="cancelled")
        cancel = asyncio.Event()
        case.overrides["cancel_event"] = cancel
        case.store.after_create = cancel.set
    elif name == "E4":
        case = _Exit(native, False, "exited", binder_error=RuntimeError("busy"))
    elif name == "E5":
        case = _Exit(tmux, True, "exited", caller="cancelled", blocked_at=_blocked_in_bind)
        case.binder_hold, case.binder_started = asyncio.Event(), asyncio.Event()
    elif name == "E6":
        case = _Exit(native, False, "exited")
    elif name == "E7":
        case = _Exit(native, False, "exited")
        case.runtime.reserve_error = RuntimeError("reserve refused")
    elif name == "E8":
        case = _Exit(native, True, "exited", caller="cancelled", blocked_at=_blocked_in_reserve)
        case.runtime.reserve_hold, case.runtime.reserve_started = asyncio.Event(), asyncio.Event()
    elif name == "E9-timeout":
        case = _Exit(tmux, True, "exited", blocked_at=_blocked_in_prepare)
        case.runtime.spawn_hold = asyncio.Event()
        case.overrides["timeout_seconds"] = 0.01
    elif name == "E9-cancelled":
        case = _Exit(native, True, "exited", caller="cancelled", blocked_at=_blocked_in_prepare)
        case.runtime.spawn_hold = asyncio.Event()
    elif name == "E10":
        case = _Exit(tmux, True, "exited")
        case.runtime.typed_fail = True
    elif name == "E11":
        case = _Exit(native, False, "live", caller="success")
    elif name == "E12-observer":
        case = _Exit(native, True, "exited")
        case.runtime.observer_error = RuntimeError("observer bind failed")
    elif name == "E12-commit-refused":
        case = _Exit(native, True, "exited")
        case.runtime.commit_error = CommitSpawnRefusedError("refused")
    elif name == "E12-commit-error":
        case = _Exit(native, True, "exited")
        case.runtime.commit_error = RuntimeError("commit transport failed")
    elif name == "E12-lost-cas":
        case = _Exit(tmux, True, "exited")
        case.store.refuse_promotion = True
    elif name == "E13-raises":
        case = _Exit(tmux, True, "exited", caller="raises")
        case.runtime.commit_error = RuntimeError("tmux commit failed")
    elif name == "E13-cancelled":
        case = _Exit(tmux, True, "exited", caller="cancelled", blocked_at=_blocked_in_commit)
        case.runtime.commit_hold, case.runtime.commit_started = asyncio.Event(), asyncio.Event()
    else:
        assert name == "E14"
        case = _Exit(tmux, False, "pending")
        case.existing = case.store.create_pending("held-id", "proj", "tmux", "gobby", "gobby-held")
        case.overrides["retry_terminal_id"] = "held-id"
    case.runtime.backend = cast(Any, case.backend)
    return case


EXITS = [
    "E1-commit-then-raise",
    "E1-rolled-back",
    "E2",
    "E3",
    "E4",
    "E5",
    "E6",
    "E7",
    "E8",
    "E9-timeout",
    "E9-cancelled",
    "E10",
    "E11",
    "E12-observer",
    "E12-commit-refused",
    "E12-commit-error",
    "E12-lost-cas",
    "E13-raises",
    "E13-cancelled",
    "E14",
]


@pytest.mark.parametrize("name", EXITS)
async def test_every_exit_releases_or_hands_off_the_claim(
    monkeypatch: pytest.MonkeyPatch, handoffs: _Handoffs, name: str
) -> None:
    case = _exit_case(name)
    if name == "E6":
        monkeypatch.setattr(case.runtime, "reserve_observer", None)

    async def binder(terminal_id: str) -> None:
        del terminal_id
        if case.binder_started is not None:
            case.binder_started.set()
        if case.binder_hold is not None:
            await case.binder_hold.wait()
        if case.binder_error is not None:
            raise case.binder_error

    if case.existing is not None:
        assert in_doubt_spawns.claim(case.existing.id)
    request = _request(case.store, case.runtime, binder=binder, **case.overrides)
    caller = asyncio.create_task(execute_spawn(request))
    try:
        release: Callable[[], None] | None = None
        if case.blocked_at is not None:
            release = await case.blocked_at(case)
            if case.caller == "cancelled":
                # Repeated cancellation while the stage's worker is still running.
                caller.cancel()
                caller.cancel()
                caller.cancel()
        outcome: SpawnResult | BaseException
        try:
            outcome = await caller
        except (ConnectionError, RuntimeError) as exc:
            outcome = exc
        at_return = {row.id: row.state for row in case.store.rows.values()}
        if release is not None:
            release()
        await _drain_owners()
    finally:
        if case.existing is not None:
            in_doubt_spawns.release(case.existing.id)

    if case.caller == "raises":
        assert isinstance(outcome, Exception)
    else:
        assert isinstance(outcome, SpawnResult)
        assert outcome.success is (case.caller == "success")
        assert outcome.status == ("pending" if case.caller == "success" else case.caller)
    assert len(handoffs.stages) == (1 if case.handed_off else 0)
    rows = [row for row in case.store.rows.values() if row is not case.existing]
    if case.existing is not None:
        assert isinstance(outcome, SpawnResult)
        assert outcome.error == "terminal_in_doubt"
        assert rows == []
        assert case.existing.attempt_generation == 1
        assert case.existing.state == "pending"
        return
    assert [row.state for row in rows] == ([case.final_state] if case.final_state else [])
    if case.handed_off:
        # Nothing settles a handed-off row before the owner's proof.
        assert "exited" not in at_return.values()
    if name.startswith("E12") or name.startswith("E13"):
        # The owner kills through the prepared identity.
        if case.backend == "native":
            assert case.runtime.killed_host_ids == ["ht-1"]
        else:
            assert case.runtime.killed == [rows[0].spawn_key]
    for terminal_id in [*handoffs.terminal_ids, *(row.id for row in rows)]:
        assert not in_doubt_spawns.holds(terminal_id)


class _Backoff:
    """Parks the owner at its ``park_at``-th backoff sleep until the test resumes it."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, park_at: int = 1) -> None:
        self.entered = asyncio.Event()
        self.resume = asyncio.Event()
        self.delays: list[float] = []
        self._park_at = park_at
        monkeypatch.setattr(spawn_in_doubt_owner, "_sleep", self._sleep)

    async def _sleep(self, delay: float) -> None:
        self.delays.append(delay)
        if len(self.delays) < self._park_at:
            await asyncio.sleep(0)
            return
        self.entered.set()
        await self.resume.wait()


class _Compensation:
    """A deferred created-isolation step that counts its runs."""

    def __init__(self) -> None:
        self.runs = 0

    async def __call__(self) -> None:
        self.runs += 1


async def _noop_binder(terminal_id: str) -> None:
    del terminal_id


def _reconciliation(
    store: MemoryTerminalStore, registry: TerminalRuntimeRegistry | None
) -> LifecycleReconciliation:
    return LifecycleReconciliation(
        agent_run_manager=MagicMock(),
        db=MagicMock(),
        cleanup_handler=MagicMock(),
        run_db=AsyncMock(),
        terminal_manager=cast(TerminalManager, store),
        runtime_registry=registry,
        spawn_in_doubt_seconds=0.0,
    )


async def _prepare_dispatched(runtime: FakeRuntime) -> None:
    while runtime.create_calls == 0:
        await asyncio.sleep(0)


async def test_in_doubt_claim_spans_prepare() -> None:
    store = _StagedStore()
    runtime = _StagedRuntime(backend="tmux", spawn_hold=asyncio.Event())
    registry = runtime_registry(runtime)
    held_at_create: list[bool] = []
    store.after_create = lambda: held_at_create.extend(
        in_doubt_spawns.holds(terminal_id) for terminal_id in store.rows
    )

    caller = asyncio.create_task(execute_spawn(_request(store, runtime, binder=_noop_binder)))
    await _prepare_dispatched(runtime)
    row = next(iter(store.rows.values()))

    assert held_at_create == [True]
    assert in_doubt_spawns.holds(row.id)
    with pytest.raises(TerminalInDoubtError):
        await kill_terminal(cast(TerminalManager, store), registry, row)
    assert (
        await _terminate_spawn_process(
            pid=None, terminal_manager=store, terminal_runtime_registry=registry, terminal=row
        )
        is False
    )
    # The prepare has outlived spawn_in_doubt_seconds (0 here); neither reaper takes it.
    assert (
        await reap_stale_pending_terminals(
            cast(TerminalManager, store), registry, in_doubt_seconds=0
        )
        == []
    )
    assert await _reconciliation(store, registry).reap_stale_pending() == 0
    assert row.state == "pending"
    assert runtime.terminate_calls == 0

    assert runtime.spawn_hold is not None
    runtime.spawn_hold.set()
    result = await caller

    assert result.success is True
    assert row.state == "live"
    assert not in_doubt_spawns.holds(row.id)


ABSENCE_CASES = ["native-probe-raises", "tmux-present-raises", "tmux-kill-unproven", "restart"]


async def _restart_reaping_is_strict() -> None:
    store = MemoryTerminalStore()
    tmux = _StagedRuntime(backend="tmux", ineffective_kills=1)
    native = _StagedRuntime(backend="native")
    present = store.create_pending("tmux-present", "proj", "tmux", "gobby", "gobby-present")
    tmux.live_keys.add("gobby-present")
    unanswered = store.create_pending("native-down", "proj", "native", "gobby", "native-down")
    native.find_host_failures.append(HostUnavailableError("gterm host unavailable"))
    listed = store.create_pending("native-listed", "proj", "native", "gobby", "native-listed")
    native.live_keys.add("native-listed")
    absent = store.create_pending("native-absent", "proj", "native", "gobby", "native-absent")

    assert await _reconciliation(store, None).reap_stale_pending() == 0
    assert {row.state for row in store.rows.values()} == {"pending"}

    assert await _reconciliation(store, runtime_registry(tmux, native)).reap_stale_pending() == 1

    assert absent.state == "exited"
    assert (present.state, unanswered.state, listed.state) == ("pending", "pending", "pending")
    assert tmux.terminate_calls == 1


@pytest.mark.parametrize("name", ABSENCE_CASES)
async def test_identityless_row_stays_pending_until_absence_proven(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    if name == "restart":
        await _restart_reaping_is_strict()
        return
    backoff = _Backoff(monkeypatch)
    store = _StagedStore()
    lost = RuntimeError("spawn response lost")
    if name == "native-probe-raises":
        runtime = _StagedRuntime(backend="native", fail_spawn=True)
        runtime.find_host_failures = [HostUnavailableError("gterm host unavailable")] * 9
    elif name == "tmux-present-raises":
        runtime = _StagedRuntime(backend="tmux", fail_after_create=lost)
        runtime.present_failures = [ConnectionError("tmux server unreachable")] * 9
    else:
        runtime = _StagedRuntime(backend="tmux", fail_after_create=lost, ineffective_kills=9)

    result = await execute_spawn(_request(store, runtime, binder=_noop_binder))
    row = store.get(result.terminal_id or "")
    assert row is not None
    compensation = _Compensation()
    assert in_doubt_spawns.defer(row.id, compensation)
    await backoff.entered.wait()

    assert result.success is False
    assert row.state == "pending"
    assert in_doubt_spawns.holds(row.id)
    assert runtime.create_calls == 1
    if name == "tmux-kill-unproven":
        # Three immediate retries and the first backoff cycle: one kill per cycle.
        assert runtime.terminate_calls == 4

    runtime.find_host_failures.clear()
    runtime.present_failures.clear()
    runtime.ineffective_kills = 0
    backoff.resume.set()
    await _drain_owners()

    assert row.state == "exited"
    assert [write for write, _ in store.writes] == ["fail_pending_attempt"]
    assert not in_doubt_spawns.holds(row.id)
    assert compensation.runs == 1
    assert runtime.create_calls == 1


@pytest.mark.asyncio
async def test_binder_free_retry_refuses_placed_owner_in_settlement_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _StagedStore()
    runtime = _StagedRuntime(backend="tmux", fail_spawn=True)
    backoff = _Backoff(monkeypatch)
    store.down.add("fail_pending_attempt")

    failed = await execute_spawn(_request(store, runtime, binder=_noop_binder))
    assert failed.success is False
    assert failed.terminal_id is not None
    row = store.rows[failed.terminal_id]
    original = replace(row)
    retry_runtime = FakeRuntime(backend="tmux", fail_spawn=True)
    await asyncio.wait_for(backoff.entered.wait(), timeout=2)

    try:
        assert in_doubt_spawns.holds(row.id)
        result = await execute_spawn(_request(store, retry_runtime, retry_terminal_id=row.id))

        assert result.error == "terminal_in_doubt"
        assert result.success is False
        assert result.terminal_id is None
        assert retry_runtime.create_calls == 0
        assert (row.attempt_generation, row.attempt_started_at) == (
            original.attempt_generation,
            original.attempt_started_at,
        )
        assert row.state == "pending"
        assert in_doubt_spawns.holds(row.id)
    finally:
        # Restore the owner's row during RED teardown if the buggy retry changed it.
        store.rows[row.id] = original
        store.down.clear()
        backoff.resume.set()
        await _drain_owners()

    assert store.rows[row.id].state == "exited"
    assert not in_doubt_spawns.holds(row.id)


@pytest.mark.asyncio
async def test_binder_free_retry_refuses_stale_reaper_mid_kill() -> None:
    kill_resume = asyncio.Event()

    class KillHeldRuntime(_StagedRuntime):
        async def terminate(self, terminal: Terminal, grace_seconds: float) -> None:
            self.terminate_started.set()
            await kill_resume.wait()
            await super().terminate(terminal, grace_seconds)

    store = _StagedStore()
    runtime = KillHeldRuntime(backend="tmux")
    row = store.create_pending(mint_terminal_id(), "proj", "tmux", "gobby", "reaper-key")
    pair = (row.attempt_generation, row.attempt_started_at)
    runtime.live_keys.add("reaper-key")
    reaper = asyncio.create_task(
        reap_stale_pending_terminals(
            cast(TerminalManager, store), runtime_registry(runtime), in_doubt_seconds=0
        )
    )
    retry_runtime = FakeRuntime(backend="tmux")
    await asyncio.wait_for(runtime.terminate_started.wait(), timeout=2)

    try:
        assert in_doubt_spawns.holds(row.id)
        result = await execute_spawn(_request(store, retry_runtime, retry_terminal_id=row.id))

        assert result.error == "terminal_in_doubt"
        assert result.success is False
        assert result.terminal_id is None
        assert retry_runtime.create_calls == 0
        assert (row.attempt_generation, row.attempt_started_at) == pair
        assert row.state == "pending"
        assert in_doubt_spawns.holds(row.id)
    finally:
        kill_resume.set()
        reaped = await asyncio.wait_for(reaper, timeout=2)

    assert reaped == [row.id]
    assert row.state == "exited"
    assert not in_doubt_spawns.holds(row.id)


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["tmux", "native"])
@pytest.mark.parametrize("outcome", ["success", "timeout", "cancelled"])
async def test_binder_free_retry_retains_claim_until_prepare_settles(
    handoffs: _Handoffs, backend: str, outcome: str
) -> None:
    store = _StagedStore()
    runtime = _StagedRuntime(backend=cast(Any, backend), spawn_hold=asyncio.Event())
    row = store.create_pending(mint_terminal_id(), "proj", backend, "gobby", "retry-key")
    prior = (row.attempt_generation, row.attempt_started_at)
    request = _request(
        store,
        runtime,
        retry_terminal_id=row.id,
        timeout_seconds=0.01 if outcome == "timeout" else None,
    )
    caller = asyncio.create_task(execute_spawn(request))
    await asyncio.wait_for(_prepare_dispatched(runtime), timeout=2)

    try:
        assert row.state == "pending"
        assert row.attempt_generation == prior[0] + 1
        assert in_doubt_spawns.holds(row.id)
        assert (
            await reap_stale_pending_terminals(
                cast(TerminalManager, store), runtime_registry(runtime), in_doubt_seconds=0
            )
            == []
        )
        if outcome == "success":
            assert runtime.spawn_hold is not None
            runtime.spawn_hold.set()
        elif outcome == "cancelled":
            caller.cancel()
            caller.cancel()
        result = await asyncio.wait_for(caller, timeout=2)
        if outcome == "success":
            assert result.success is True
            assert handoffs.stages == []
            assert row.state == "live"
        else:
            assert result.success is False
            assert result.prior_attempt == prior
            assert result.error == (
                "cancelled"
                if outcome == "cancelled"
                else "spawn_timeout"
                if backend == "native"
                else "spawn timed out"
            )
            assert handoffs.stages == ["prepare"]
            assert row.state == "pending"
            assert in_doubt_spawns.holds(row.id)
    finally:
        assert runtime.spawn_hold is not None
        runtime.spawn_hold.set()
        if not caller.done():
            await asyncio.wait_for(caller, timeout=2)
        await _drain_owners()

    assert row.state == ("live" if outcome == "success" else "exited")
    assert not in_doubt_spawns.holds(row.id)


@dataclass
class _DownHost:
    """A gterm host client that cannot be reached after a host restart."""

    host_epoch: str = ""

    async def ensure_connected(self) -> None:
        raise HostUnavailableError("gterm host unavailable")

    async def list_terminals(self) -> list[object]:
        raise HostUnavailableError("gterm host unavailable")


TIMEOUT_KILLS = [
    ("tmux", "proven", "exited"),
    ("native", "proven", "exited"),
    ("tmux", "session-present", "orphaned"),
    ("tmux", "terminate-raises", "orphaned"),
    ("native", "stale-no-process", "orphaned"),
    ("native", "stale-dead-group", "exited"),
]


@pytest.mark.parametrize(("backend", "kill", "final"), TIMEOUT_KILLS)
async def test_placed_timeout_holds_pending_then_late_settlement(
    monkeypatch: pytest.MonkeyPatch, handoffs: _Handoffs, backend: str, kill: str, final: str
) -> None:
    store = _StagedStore()
    runtime = _StagedRuntime(backend=cast(Any, backend), spawn_hold=asyncio.Event())
    reaped: list[dict[str, object]] = []
    if kill == "session-present":
        runtime.ineffective_kills = 99
    elif kill == "terminate-raises":
        runtime.terminate_error = RuntimeError("tmux kill-session failed")
    elif kill.startswith("stale"):
        runtime.epoch_after_prepare = "epoch-restarted"
        runtime.kill_via = NativeTerminalRuntime(_DownHost())
        monkeypatch.setattr(
            "gobby.terminals.native_runtime.reap_recorded_process",
            lambda process, *, grace_seconds, now=None: reaped.append(dict(process)),
        )
        monkeypatch.setattr(
            "gobby.terminals.native_runtime.recorded_process_group_is_alive",
            lambda process: False,
        )
        if kill == "stale-dead-group":
            runtime.prepare_process = ProcessIdentity(pgid=4321, start_time=1784592177)

    result = await execute_spawn(
        _request(store, runtime, binder=_noop_binder, timeout_seconds=0.01)
    )
    row = store.get(result.terminal_id or "")
    assert row is not None

    assert result.error == ("spawn_timeout" if backend == "native" else "spawn timed out")
    assert result.retryable_infrastructure is True
    assert handoffs.stages == ["prepare"]
    assert row.state == "pending"
    assert in_doubt_spawns.holds(row.id)
    compensation = _Compensation()
    assert in_doubt_spawns.defer(row.id, compensation)

    assert runtime.spawn_hold is not None
    runtime.spawn_hold.set()
    await _drain_owners()

    assert row.state == final
    assert not in_doubt_spawns.holds(row.id)
    if final == "exited":
        assert compensation.runs == 1
        if kill == "stale-dead-group":
            assert [process.get("pgid") for process in reaped] == [4321]
        return
    # One CAS moves the row from pending to orphaned with the prepared identity.
    assert [write for write, _ in store.writes] == ["mark_kill_failed"]
    identity = store.writes[0][1]["identity"]
    assert row.locator == dict(identity.locator)
    assert row.locator_key == identity.locator_key is not None
    assert row.host_epoch == identity.host_epoch
    if backend == "native":
        assert row.host_epoch == "epoch"
        assert row.process == {"host_terminal_id": "ht-1"}
    assert compensation.runs == 0


async def test_unplaced_timeout_unchanged(handoffs: _Handoffs) -> None:
    store = MemoryTerminalStore()
    runtime = FakeRuntime(backend="tmux", spawn_hold=asyncio.Event())

    result = await execute_spawn(_request(store, runtime, timeout_seconds=0.01))
    row = next(iter(store.rows.values()))

    assert result.success is False
    assert result.error == "spawn timed out"
    assert row.state == "pending"
    assert not in_doubt_spawns.holds(row.id)
    assert handoffs.stages == []

    assert runtime.spawn_hold is not None
    runtime.spawn_hold.set()
    # The late cleanup is scheduled only once the prepare completes.
    await runtime.terminate_started.wait()
    await _drain_owners()

    assert runtime.killed == [row.spawn_key]
    assert row.state == "exited"


CANCEL_MOMENTS = [
    ("native", "while-unresolved"),
    ("tmux", "as-success-completes"),
    ("tmux", "as-failure-completes"),
]


@pytest.mark.parametrize(("backend", "moment"), CANCEL_MOMENTS)
async def test_placed_cancellation_has_one_owner(
    handoffs: _Handoffs, backend: str, moment: str
) -> None:
    store = _StagedStore()
    runtime = _StagedRuntime(
        backend=cast(Any, backend),
        spawn_hold=asyncio.Event(),
        fail_spawn=moment == "as-failure-completes",
    )
    caller = asyncio.create_task(execute_spawn(_request(store, runtime, binder=_noop_binder)))
    await _prepare_dispatched(runtime)
    assert runtime.spawn_hold is not None
    if moment != "while-unresolved":
        # The prepare completes in the same loop turn the cancellation lands in.
        runtime.spawn_hold.set()
    caller.cancel()
    caller.cancel()
    caller.cancel()
    result = await caller
    row = next(iter(store.rows.values()))
    held_at_return = in_doubt_spawns.holds(row.id)
    runtime.spawn_hold.set()
    await _drain_owners()

    assert result.status == "cancelled"
    assert held_at_return
    assert handoffs.stages == ["prepare"]
    assert runtime.create_calls == 1
    assert row.state == "exited"
    assert not in_doubt_spawns.holds(row.id)
    if moment == "as-success-completes":
        assert runtime.killed == [row.spawn_key]


LATE_FAILURES = ["tmux-dimension-query", "native-response-lost", "native-host-unreachable"]


@pytest.mark.parametrize("name", LATE_FAILURES)
async def test_late_prepare_failure_requires_proven_absence(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    backoff = _Backoff(monkeypatch)
    store = _StagedStore()
    backend = "tmux" if name.startswith("tmux") else "native"
    runtime = _StagedRuntime(
        backend=cast(Any, backend), fail_after_create=RuntimeError("late prepare failure")
    )
    if name == "native-host-unreachable":
        runtime.find_host_failures = [HostUnavailableError("gterm host unavailable")] * 9

    result = await execute_spawn(_request(store, runtime, binder=_noop_binder))
    row = store.get(result.terminal_id or "")
    assert row is not None
    if name == "native-host-unreachable":
        await backoff.entered.wait()
        assert row.state == "pending"
        assert in_doubt_spawns.holds(row.id)
        assert runtime.killed_host_ids == []
        runtime.find_host_failures.clear()
    backoff.resume.set()
    await _drain_owners()

    assert result.success is False
    if backend == "tmux":
        assert runtime.killed == [row.spawn_key]
    else:
        assert runtime.killed_host_ids == ["ht-1"]
    assert row.state == "exited"
    assert [write for write, _ in store.writes] == ["fail_pending_attempt"]
    assert not in_doubt_spawns.holds(row.id)


def _own(
    store: _StagedStore,
    runtime: FakeRuntime,
    row: Terminal,
    stage: OwnerStage,
    *,
    task: asyncio.Future[Any] | None = None,
    prepared: PreparedSpawn | None = None,
    pair: tuple[int, datetime] | None | bool = True,
    prior: tuple[int, datetime] | None = None,
) -> None:
    """Claim ``row`` and hand it to one owner, as the placed path's handoff does."""
    assert in_doubt_spawns.claim(row.id)
    owned = (row.attempt_generation, row.attempt_started_at) if pair is True else pair or None
    spawn_executor._schedule_timeout_cleanup(
        task,
        manager=cast(TerminalManager, store),
        runtime=runtime,
        backend=runtime.backend,
        terminal_id=row.id,
        spawn_key=row.spawn_key or row.id,
        attempt_generation=None if owned is None else owned[0],
        attempt_started_at=None if owned is None else owned[1],
        in_doubt_owner=True,
        stage=stage,
        prepared=prepared,
        prior_attempt=prior,
    )


async def _prepared_row(
    store: _StagedStore, runtime: FakeRuntime, *, live: bool
) -> tuple[Terminal, PreparedSpawn]:
    terminal_id = mint_terminal_id()
    spawn_key = f"gobby-{terminal_id[:8]}"
    row = store.create_pending(terminal_id, "proj", runtime.backend, "gobby", spawn_key)
    prepared = await runtime.prepare_spawn(
        TerminalSpawnRequest(terminal_id=UUID(terminal_id), spawn_key=spawn_key, command=["x"])
    )
    if live:
        row.state = "live"
    return row, prepared


def _failed(exc: Exception) -> asyncio.Future[Any]:
    future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    future.set_exception(exc)
    return future


STORAGE_FAILURES = [
    "mark-kill-failed-raises",
    "already-exited",
    "final-write-fails",
    "wrong-state-readback",
    "orphan-identity-cas-misses",
]


@pytest.mark.parametrize("name", STORAGE_FAILURES)
async def test_owner_contains_storage_failures(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    backoff = _Backoff(monkeypatch)
    store = _StagedStore()
    if name == "mark-kill-failed-raises":
        runtime = _StagedRuntime(backend="tmux")
        row, prepared = await _prepared_row(store, runtime, live=True)
        store.down.add("mark_kill_failed")
        _own(store, runtime, row, "promote", prepared=prepared)
        await _drain_owners()
        # The failed orphan step still lets the owner make its kill decision.
        assert runtime.killed == [row.spawn_key]
        attempts = spawn_in_doubt_owner.IMMEDIATE_RETRIES + 1
        assert [write for write, _ in store.writes] == [
            *["mark_kill_failed"] * attempts,
            "mark_exited_attempt",
        ]
        assert row.state == "exited"
        assert not in_doubt_spawns.holds(row.id)
        return
    if name == "already-exited":
        runtime = _StagedRuntime(backend="tmux")
        row = store.create_pending(mint_terminal_id(), "proj", "tmux", "gobby", "gobby-done")
        row.state = "exited"
        _own(store, runtime, row, "bind")
        await _drain_owners()
        assert store.writes == []
        assert not in_doubt_spawns.holds(row.id)
        return
    if name == "orphan-identity-cas-misses":
        runtime = _StagedRuntime(
            backend="native", terminate_error=HostUnavailableError("gterm host unavailable")
        )
        row, prepared = await _prepared_row(store, runtime, live=True)
        store.cas_misses["mark_kill_failed"] = 99
        _own(store, runtime, row, "promote", prepared=prepared)
        held_state, final = "live", "orphaned"
    else:
        runtime = _StagedRuntime(backend="tmux")
        row = store.create_pending(mint_terminal_id(), "proj", "tmux", "gobby", "gobby-late")
        if name == "final-write-fails":
            store.down.add("fail_pending_attempt")
        else:
            store.lost_writes = 99
        _own(store, runtime, row, "prepare", task=_failed(RuntimeError("prepare failed")))
        held_state, final = "pending", "exited"
    await backoff.entered.wait()

    assert in_doubt_spawns.holds(row.id)
    assert row.state == held_state
    assert backoff.delays == [spawn_in_doubt_owner.BACKOFF_START_SECONDS]
    if name == "orphan-identity-cas-misses":
        assert "record_orphan_identity" in [write for write, _ in store.writes]

    store.down.clear()
    store.cas_misses.clear()
    store.lost_writes = 0
    backoff.resume.set()
    await _drain_owners()

    assert row.state == final
    assert not in_doubt_spawns.holds(row.id)


def _done(value: object) -> asyncio.Future[Any]:
    future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    future.set_result(value)
    return future


class _Isolation:
    """A created-isolation handler whose removals are counted."""

    def __init__(self) -> None:
        self.removals = 0

    async def cleanup_environment(self, spawn_config: object) -> None:
        del spawn_config
        self.removals += 1

    async def step(
        self, store: MemoryTerminalStore, terminal_id: str, prior: tuple[int, datetime] | None
    ) -> None:
        """The failure cleanup's isolation step for this attempt's terminal."""
        await _cleanup_isolation_step(
            self,
            object(),
            cleanup=True,
            run_id="run",
            terminal_id=terminal_id,
            terminal_manager=store,
            held=True,
            settled=False,
            prior_attempt=prior,
        )


SETTLEMENT_WRITES = {
    "fail_pending_attempt",
    "mark_exited_attempt",
    "mark_kill_failed",
    "record_orphan_identity",
}
RECOVERIES = [
    ("exited", "prepare"),
    ("orphan-from-pending", "prepare"),
    ("orphan-from-live", "promote"),
    ("failed", "create"),
    ("failed", "bind"),
    ("failed", "reserve"),
    ("cas-miss", "bind"),
    ("wrong-state", "bind"),
]


@pytest.mark.parametrize(("settlement", "stage"), RECOVERIES)
async def test_owner_retries_settlement_until_storage_recovers(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    settlement: str,
    stage: OwnerStage,
) -> None:
    caplog.set_level("INFO", logger=spawn_in_doubt_owner.__name__)
    backoff = _Backoff(monkeypatch, park_at=7)
    store = _StagedStore()
    isolation = _Isolation()
    task: asyncio.Future[Any] | None = None
    prepared: PreparedSpawn | None = None
    identity_source: PreparedSpawn | None = None
    if settlement == "orphan-from-live":
        runtime = _StagedRuntime(backend="native", terminate_error=RuntimeError("kill failed"))
        row, prepared = await _prepared_row(store, runtime, live=True)
        identity_source = prepared
    elif stage == "prepare":
        runtime = _StagedRuntime(
            backend="tmux", ineffective_kills=0 if settlement == "exited" else 99
        )
        row, identity_source = await _prepared_row(store, runtime, live=False)
        task = _done(identity_source)
    else:
        runtime = _StagedRuntime(backend="native")
        row = store.create_pending(mint_terminal_id(), "proj", "native", "gobby", "native-key")
        task = {"create": _done(row), "reserve": _failed(RuntimeError("refused"))}.get(stage)
    if settlement == "cas-miss":
        store.cas_misses["fail_pending_attempt"] = 99
    elif settlement == "wrong-state":
        store.lost_writes = 99
    else:
        store.down = set(SETTLEMENT_WRITES)
    before = row.state
    creates_before = runtime.create_calls
    _own(store, runtime, row, stage, task=task, prepared=prepared, pair=stage != "create")
    compensation = _Compensation()
    assert in_doubt_spawns.defer(row.id, compensation)
    await isolation.step(store, row.id, None)
    await backoff.entered.wait()
    kills_at_park = runtime.terminate_calls

    # The owner keeps its claim across the capped backoff; nothing else takes the row.
    assert backoff.delays == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0]
    assert row.state == before
    assert in_doubt_spawns.claim(row.id) is False
    registry = runtime_registry(runtime)
    with pytest.raises(TerminalInDoubtError):
        await kill_terminal(cast(TerminalManager, store), registry, row)
    assert (
        await reap_stale_pending_terminals(
            cast(TerminalManager, store), registry, in_doubt_seconds=0
        )
        == []
    )
    assert await _reconciliation(store, registry).reap_stale_pending() == 0
    assert row.state == before

    store.down.clear()
    store.cas_misses.clear()
    store.lost_writes = 0
    backoff.resume.set()
    await _drain_owners()

    assert not in_doubt_spawns.holds(row.id)
    # No settlement cycle kills, probes the prepare again or runs provider code.
    assert runtime.terminate_calls == kills_at_park
    assert runtime.create_calls == creates_before
    assert all(
        (kwargs["attempt_generation"], kwargs["attempt_started_at"])
        == (row.attempt_generation, row.attempt_started_at)
        for _, kwargs in store.writes
    )
    messages = [record.getMessage() for record in caplog.records]
    # One WARNING as the retry starts (then at most one per 10 minutes), one INFO at the end.
    assert sum(message.startswith("In-doubt owner retrying") for message in messages) == 1
    assert sum(message.startswith("In-doubt owner confirmed") for message in messages) == 1
    if settlement.startswith("orphan"):
        assert identity_source is not None
        assert row.state == "orphaned"
        assert row.locator_key == identity_source.locator_key
        assert compensation.runs == 0
        assert isolation.removals == 0
        return
    assert row.state == "exited"
    assert compensation.runs == 1
    assert isolation.removals == 1
    # A step deferred after the release decides from the row: exited removes.
    await isolation.step(store, row.id, None)
    assert isolation.removals == 2


def test_read_back_with_another_pair_is_not_confirmation() -> None:
    row = MemoryTerminalStore().create_pending("t", "proj", "tmux", "gobby", "gobby-t")
    attempt = spawn_in_doubt_owner.InDoubtAttempt(
        manager=cast(TerminalManager, MemoryTerminalStore()),
        runtime=cast(TerminalRuntime, FakeRuntime()),
        backend="tmux",
        terminal_id=row.id,
        spawn_key="gobby-t",
        pair=(row.attempt_generation, row.attempt_started_at),
    )
    exited = replace(row, state="exited")
    bumped = replace(exited, attempt_generation=row.attempt_generation + 1)

    assert spawn_in_doubt_owner._confirmed(attempt, exited, "exited", None) is True
    assert spawn_in_doubt_owner._confirmed(attempt, bumped, "exited", None) is False


def test_orphan_read_back_needs_the_complete_prepared_identity() -> None:
    row = MemoryTerminalStore().create_pending("t", "proj", "native", "gobby", "gobby-t")
    attempt = spawn_in_doubt_owner.InDoubtAttempt(
        manager=cast(TerminalManager, MemoryTerminalStore()),
        runtime=cast(TerminalRuntime, FakeRuntime()),
        backend="native",
        terminal_id=row.id,
        spawn_key="gobby-t",
        pair=(row.attempt_generation, row.attempt_started_at),
    )
    identity = OrphanIdentity(
        locator={"host_terminal_id": "ht-1"},
        locator_key="key-1",
        host_epoch="epoch-1",
        process={"host_terminal_id": "ht-1", "pgid": 4242, "start_time": 5.0},
    )
    kept = replace(
        row,
        state="orphaned",
        locator={"host_terminal_id": "ht-1"},
        locator_key="key-1",
        host_epoch="epoch-1",
        process={"cwd": "/work", "host_terminal_id": "ht-1", "pgid": 4242, "start_time": 5.0},
    )
    other_locator = replace(kept, locator={"host_terminal_id": "ht-2"})
    no_process = replace(kept, process={"cwd": "/work"})
    other_group = replace(kept, process={**(kept.process or {}), "pgid": 9191})

    assert spawn_in_doubt_owner._confirmed(attempt, kept, "orphan", identity) is True
    assert spawn_in_doubt_owner._confirmed(attempt, other_locator, "orphan", identity) is False
    assert spawn_in_doubt_owner._confirmed(attempt, no_process, "orphan", identity) is False
    assert spawn_in_doubt_owner._confirmed(attempt, other_group, "orphan", identity) is False


CANCEL_POINTS = ["drain", "first-settlement", "backoff", "retry-write"]


@pytest.mark.parametrize("shutdown", [False, True], ids=["waiters", "shutdown"])
@pytest.mark.parametrize("point", CANCEL_POINTS)
async def test_owner_retry_survives_cancellation_until_shutdown(
    monkeypatch: pytest.MonkeyPatch, handoffs: _Handoffs, point: str, shutdown: bool
) -> None:
    backoff = _Backoff(monkeypatch)
    releases: list[str] = []
    release = in_doubt_spawns.release

    def counting_release(terminal_id: str) -> list[Any]:
        releases.append(terminal_id)
        return release(terminal_id)

    monkeypatch.setattr(in_doubt_spawns, "release", counting_release)
    store = _StagedStore()
    runtime = _StagedRuntime(backend="tmux", spawn_hold=asyncio.Event())
    if point == "first-settlement":
        store.write_gate = threading.Event()
    elif point in {"backoff", "retry-write"}:
        store.down = set(SETTLEMENT_WRITES)
    loop = asyncio.get_running_loop()
    caller = asyncio.create_task(execute_spawn(_request(store, runtime, binder=_noop_binder)))
    await _prepare_dispatched(runtime)
    caller.cancel()
    caller.cancel()
    assert (await caller).status == "cancelled"
    row = next(iter(store.rows.values()))
    compensation = _Compensation()
    assert in_doubt_spawns.defer(row.id, compensation)
    [owner] = [t for t in spawn_executor._TIMEOUT_CLEANUP_TASKS if t.get_loop() is loop]
    waiter = asyncio.ensure_future(asyncio.shield(owner))

    assert runtime.spawn_hold is not None
    if point != "drain":
        runtime.spawn_hold.set()
    if point == "first-settlement":
        await asyncio.to_thread(store.write_entered.wait, 5)
    elif point in {"backoff", "retry-write"}:
        await backoff.entered.wait()
    if point == "retry-write":
        store.write_gate = threading.Event()
        store.write_entered.clear()
        backoff.resume.set()
        await asyncio.to_thread(store.write_entered.wait, 5)

    try:
        if shutdown:
            # Loop teardown cancels the owner task itself.
            owner.cancel()
            with pytest.raises(asyncio.CancelledError):
                await owner
            store.down = set(SETTLEMENT_WRITES)
        else:
            for _ in range(3):
                caller.cancel()
                waiter.cancel()
                await asyncio.sleep(0)
            assert not owner.done()
            store.down.clear()
        if store.write_gate is not None:
            store.write_gate.set()
        runtime.spawn_hold.set()
        backoff.resume.set()
        await _drain_owners()

        if shutdown:
            assert releases == []
            assert compensation.runs == 0
            assert in_doubt_spawns.holds(row.id)
            assert row.state == "pending"
            return
        assert handoffs.stages == ["prepare"]
        assert releases == [row.id]
        assert compensation.runs == 1
        assert row.state == "exited"
    finally:
        # The registry is process-wide; a shutdown leaves the claim until exit.
        release(row.id)


async def test_cancelled_release_still_runs_every_deferred_step_once() -> None:
    terminal_id = mint_terminal_id()
    gate = asyncio.Event()
    entered = asyncio.Event()
    first = _Compensation()
    second = _Compensation()

    async def blocking_step() -> None:
        entered.set()
        await gate.wait()
        await first()

    assert in_doubt_spawns.claim(terminal_id)
    assert in_doubt_spawns.defer(terminal_id, blocking_step)
    assert in_doubt_spawns.defer(terminal_id, second)
    releasing = asyncio.create_task(
        spawn_in_doubt_owner.release_claim(terminal_id, run_deferred=True)
    )
    await entered.wait()
    releasing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await releasing
    gate.set()
    for _ in range(5):
        await asyncio.sleep(0)

    assert (first.runs, second.runs) == (1, 1)
    assert not in_doubt_spawns.holds(terminal_id)


INDETERMINATE = [
    "create-commits-then-raises",
    "create-rolls-back",
    "bump-commits-then-raises",
    "bump-rolls-back",
    "bump-without-binder-commits-then-raises",
    "bump-without-binder-rolls-back",
    "read-back-outage",
]


@pytest.mark.parametrize("name", INDETERMINATE)
async def test_indeterminate_create_is_recovered_by_read_back(
    monkeypatch: pytest.MonkeyPatch, handoffs: _Handoffs, name: str
) -> None:
    backoff = _Backoff(monkeypatch)
    store = _StagedStore()
    runtime = _StagedRuntime(backend="tmux")
    isolation = _Isolation()
    overrides: dict[str, Any] = {}
    earlier: Terminal | None = None
    if name.startswith("bump"):
        earlier = store.create_pending(mint_terminal_id(), "proj", "tmux", "gobby", "gobby-old")
        overrides["retry_terminal_id"] = earlier.id
    prior = None if earlier is None else (earlier.attempt_generation, earlier.attempt_started_at)
    if name.endswith("rolls-back"):
        store.raise_before_commit = ConnectionError("rolled back")
    else:
        store.raise_after_commit = ConnectionError("commit acknowledgment lost")
    if name == "read-back-outage":
        store.down = {"get"}
    compensation = _Compensation()

    with pytest.raises(ConnectionError):
        binder = None if "without-binder" in name else _noop_binder
        await execute_spawn(_request(store, runtime, binder=binder, **overrides))
    [terminal_id] = handoffs.terminal_ids
    # The owner has not run yet: the claim is held for steps deferred before release.
    assert in_doubt_spawns.defer(terminal_id, compensation)
    await isolation.step(store, terminal_id, prior)
    if name == "read-back-outage":
        await backoff.entered.wait()
        assert in_doubt_spawns.holds(terminal_id)
        store.down.clear()
    backoff.resume.set()
    await _drain_owners()

    row = store.rows.get(terminal_id)
    assert handoffs.stages == ["create"]
    assert not in_doubt_spawns.holds(terminal_id)
    assert compensation.runs == 1
    assert isolation.removals == 1
    assert runtime.create_calls == 0
    writes = [write for write, _ in store.writes]
    if name.endswith("rolls-back"):
        assert writes == []
        if earlier is None:
            assert row is None
        else:
            assert row is earlier
            assert (earlier.state, earlier.attempt_generation) == ("pending", prior and prior[0])
    else:
        assert writes == ["fail_pending_attempt"]
        assert row is not None
        assert row.state == "exited"
    # A step deferred after the release decides from the row the owner settled.
    await isolation.step(store, terminal_id, prior)
    assert isolation.removals == 2
    if earlier is not None and name.endswith("rolls-back"):
        # A row carrying a newer pair keeps created isolation.
        earlier.attempt_generation += 1
        await isolation.step(store, terminal_id, prior)
        assert isolation.removals == 2
