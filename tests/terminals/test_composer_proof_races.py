"""Isolated checks of the race driver's observations of original asyncio locks."""

from __future__ import annotations

import asyncio
import gc
import hashlib
import importlib
import json
import tempfile
from collections.abc import Awaitable, Callable, Coroutine
from contextlib import ExitStack
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import httpx
import pytest

from gobby.agents.terminal_delivery import shielded_terminal_delivery
from gobby.events.wake import WakeDispatcher
from gobby.hooks import terminal_handoff_delivery
from gobby.hooks._normalization_tools import normalize_tool_fields
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.hooks.terminal_handoff_delivery import staged_handoff_from_event
from gobby.sessions.clear_continuation import CLEAR_ATTEMPT_VARIABLE
from gobby.sessions.handoff import (
    FAILED_HANDOFF_VARIABLE,
    HANDOFF_DISPATCH_GATE_VARIABLE,
    PENDING_HANDOFF_VARIABLE,
    ClaimedHandoffDelivery,
    build_handoff_continue_prompt,
)
from gobby.terminals.composer_lock import composer_action_lock
from gobby.terminals.leases import TerminalLeaseRegistry
from gobby.terminals.write_coordinator import UnresolvedWriteStore, WriteCoordinator
from tests.e2e import composer_proof_cleanup as cleanup_module
from tests.e2e import composer_proof_trace as trace_module
from tests.e2e.composer_proof import ProofRefused, Surface
from tests.e2e.composer_proof_live import LiveProof
from tests.e2e.composer_proof_race_observer import RaceObservation
from tests.e2e.conftest import AsyncMCPTestClient
from tests.terminals.fakes import (
    FakeRuntime,
    MemoryTerminalStore,
    make_memory_terminal,
    runtime_registry,
)
from tests.terminals.test_composer_proof_trace import registered_trace

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


async def test_before_write_barrier_holds_original_dispatch_without_submitting(
    tmp_path: Path,
) -> None:
    trace, event = registered_trace(tmp_path)
    own = trace.surfaces[str(event["terminal_id"])]
    trace.arm_before(own, "/compact")
    event = {**event, "payload_sha256": hashlib.sha256(b"/compact\n").hexdigest()}
    entered: list[str] = []

    async def original() -> None:
        await trace.accept({**event, "phase": "before"})
        entered.append("actual text transport")
        await trace.accept({**event, "phase": "after", "outcome": "Delivered"})

    async with asyncio.TaskGroup() as group:
        writer = group.create_task(original())
        try:
            await asyncio.wait_for(trace.staged.wait(), 1)
            assert not entered and not writer.done()
            assert [item["phase"] for item in trace.events] == ["before"]
        finally:
            trace.release.set()
    assert entered == ["actual text transport"]
    assert [item["phase"] for item in trace.events] == ["before", "after"]


@pytest.mark.parametrize("first", ["wake", "handoff"])
@pytest.mark.parametrize("clear", [False, True])
@pytest.mark.parametrize("failed", [False, True])
@pytest.mark.parametrize("corruption", [None, "extra_enter", "different_lock", "wake_bytes"])
async def test_race_evidence_requires_exact_writes_and_original_wait_order(
    first: str, clear: bool, failed: bool, corruption: str | None
) -> None:
    module = importlib.import_module("tests.e2e.composer_proof_races")
    attempt, terminal = str(uuid4()), str(uuid4())
    events: list[dict[str, object]] = []

    def lock(writer: str, state: str) -> None:
        events.append(
            {
                "phase": "lock",
                "writer": writer,
                "state": state,
                "lock_sha256": "b" * 64,
                "attempt_id": attempt if writer == "handoff" else None,
            }
        )

    def write(payload: str, kind: str) -> None:
        fields = {
            "origin": "automatic",
            "kind": kind,
            "submit": False,
            "payload_sha256": hashlib.sha256(payload.encode()).hexdigest(),
            "action_sha256": str(len(events)),
        }
        events.extend(
            [{**fields, "phase": "before"}, {**fields, "phase": "after", "outcome": "Delivered"}]
        )

    lock(first, "acquired")
    lock("handoff" if first == "wake" else "wake", "queued")
    if first == "wake":
        lock("handoff", "acquired")
    write("/clear\n" if clear else "/compact\n", "text")
    if failed:
        events[-1]["outcome"] = "Refused"
    else:
        write("enter", "key")
    if first == "handoff":
        lock("wake", "acquired")
    if not failed:
        write(build_handoff_continue_prompt() + "\n", "text")
        write("enter", "key")
    events.append(
        {
            "phase": "wake",
            "requested_session_id": "predecessor",
            "delivered": False,
            "skipped": "handoff_delivery_pending",
        }
    )
    if corruption == "extra_enter":
        write("enter", "key")
    elif corruption == "different_lock":
        events[1]["lock_sha256"] = "c" * 64
    elif corruption == "wake_bytes":
        write(trace_module.WAKE_TEXT, "text")
    events = [
        {**item, "terminal_id": terminal, "sequence": index + 1}
        for index, item in enumerate(events)
    ]
    arguments = {
        "terminal_id": terminal,
        "attempt_id": attempt,
        "first": first,
        "clear": clear,
        "requested_session_id": "predecessor",
        "failed": failed,
    }
    if corruption is None:
        module.require_race_evidence(events, **arguments)
    else:
        with pytest.raises(ProofRefused):
            module.require_race_evidence(events, **arguments)


@pytest.mark.parametrize("first", ["wake", "handoff"])
async def test_controller_queues_original_competing_waiter_before_text_release(
    tmp_path: Path, first: str
) -> None:
    """Protocol simulation checks scheduling; it supplies no provider acceptance evidence."""
    module = importlib.import_module("tests.e2e.composer_proof_races")
    trace, event = registered_trace(tmp_path)
    own = trace.surfaces[str(event["terminal_id"])]
    terminal = replace(
        make_memory_terminal(),
        id=own.terminal_id,
        session_id=own.session_id,
        host_epoch=own.host_epoch,
        project_id=own.project_id,
    )
    coordinator = WriteCoordinator(
        cast(UnresolvedWriteStore, MemoryTerminalStore(terminal)),
        runtime_registry(FakeRuntime()),
        lease_registry=TerminalLeaseRegistry(daemon_epoch="proof-epoch"),
    )
    observer = RaceObservation(trace.accept)
    attempt, message = str(uuid4()), str(uuid4())
    settled = asyncio.Event()
    staged: list[str] = []
    transports: list[str] = []
    seat = SimpleNamespace(
        surface=own,
        external_id="unit-provider",
        frames=SimpleNamespace(revision=0),
        ws=SimpleNamespace(messages=[]),
    )
    proof = SimpleNamespace(trace=trace, evidence=[], handoffs_pending=set())
    controller = module.ComposerRaces(cast(Any, proof), MagicMock())

    async def empty(_seat: Any, **_kwargs: Any) -> None:
        assert _seat is seat

    async def held(_seat: Any, command: str, **kwargs: Any) -> Any:
        await asyncio.wait_for(trace.staged.wait(), 1)
        assert coordinator.logical_action_lock(own.terminal_id).locked()
        assert command == "/compact" and kwargs["before_write"] is True
        seat.frames.revision += 1
        return SimpleNamespace(cursor=(0, 0))

    async def durable(target: Surface, ident: str) -> None:
        assert target == own and ident == message
        transports.append("durable")

    async def emit(payload: str, kind: str) -> None:
        fields = {
            **event,
            "kind": kind,
            "payload_sha256": hashlib.sha256(payload.encode()).hexdigest(),
            "action_sha256": hashlib.sha256(str(len(trace.events)).encode()).hexdigest(),
        }
        await trace.accept({**fields, "phase": "before"})
        assert trace.release.is_set(), "original text dispatch must wait for controller release"
        await trace.accept({**fields, "phase": "after", "outcome": "Delivered"})

    async def wake() -> None:
        with observer.writer("wake"):
            async with composer_action_lock(own.terminal_id, coordinator):
                assert staged == [attempt], "wake preflight must observe the staged handoff"
        await trace.accept(
            {
                "phase": "wake",
                **{key: value for key, value in own.__dict__.items() if key != "provider"},
                "requested_session_id": own.session_id,
                "delivered": False,
                "skipped": "handoff_delivery_pending",
                "priority": "urgent",
                "monotonic": 1.0,
            }
        )

    async def handoff() -> None:
        with observer.writer("handoff", attempt):
            async with composer_action_lock(own.terminal_id, coordinator):
                await emit("/compact\n", "text")
                await emit("enter", "key")
            async with composer_action_lock(own.terminal_id, coordinator):
                await emit(build_handoff_continue_prompt() + "\n", "text")
                await emit("enter", "key")
        settled.set()

    async def stage(_seat: Any, *, clear: bool) -> dict[str, Any]:
        assert not clear and trace.admitted.is_set()
        assert not coordinator.logical_action_lock(own.terminal_id).locked()
        staged.append(attempt)
        return {"input_data": {"tool_output": {"result": {"attempt_id": attempt}}}}

    async def settled_transport(*_args: Any, **_kwargs: Any) -> None:
        await asyncio.wait_for(settled.wait(), 1)

    proof.empty, proof.held_boundary, proof.durable = empty, held, durable
    with observer.install():
        async with asyncio.TaskGroup() as group:

            async def notice(_seat: Any, priority: str) -> str:
                assert priority == "urgent"
                group.create_task(wake())
                return message

            async def relay(_seat: Any, _envelope: dict[str, Any]) -> None:
                transports.append("completion relay")
                group.create_task(handoff())

            proof.notice = notice
            with (
                patch.object(controller, "stage", stage),
                patch.object(controller, "relay", relay),
                patch.object(controller, "boundary", settled_transport),
                patch.object(controller, "settle", settled_transport),
            ):
                await controller.race(cast(Any, seat), clear=False, first=cast(Any, first))
    assert transports == ["completion relay", "durable"]
    assert proof.evidence[-1]["first"] == first and proof.evidence[-1]["attempt_id"] == attempt
    assert not coordinator.logical_action_lock(own.terminal_id).locked()


async def test_observer_reads_owned_exited_row_through_original_coordinator() -> None:
    terminal = replace(
        make_memory_terminal(),
        state="exited",
        session_id=str(uuid4()),
        host_epoch=str(uuid4()),
        project_id="00000000-0000-0000-0000-000000000e2e",
    )
    coordinator = WriteCoordinator(
        cast(UnresolvedWriteStore, MemoryTerminalStore(terminal)),
        runtime_registry(FakeRuntime()),
        lease_registry=TerminalLeaseRegistry(daemon_epoch="proof-epoch"),
    )
    observer = RaceObservation(AsyncMock())
    surface = Surface(
        "claude",
        str(terminal.session_id),
        terminal.id,
        str(terminal.host_epoch),
        str(terminal.project_id),
    )
    with observer.install():
        lock = coordinator.logical_action_lock(terminal.id)
        assert await observer.lookup_terminal(surface) == terminal
        with pytest.raises(ProofRefused, match="binding"):
            await observer.lookup_terminal(replace(surface, host_epoch=str(uuid4())))
        assert not lock.locked()
        del lock
        gc.collect()
        assert not observer.locks
        assert await observer.lookup_terminal(surface) == terminal


AcquireObserver = Callable[
    [
        asyncio.Lock,
        Callable[[asyncio.Lock], Awaitable[bool]],
        Callable[[dict[str, object]], Awaitable[None]],
        dict[str, object],
    ],
    Coroutine[Any, Any, bool],
]


def acquire_observer() -> AcquireObserver:
    observer = getattr(trace_module, "observe_acquire", None)
    assert callable(observer), "race driver must observe the original lock's actual wait queue"
    return cast(AcquireObserver, observer)


@pytest.mark.parametrize("writer", ["wake", "handoff"])
async def test_observer_proves_original_lock_wait_before_acquisition(writer: str) -> None:
    observe = acquire_observer()
    lock = asyncio.Lock()
    original = asyncio.Lock.acquire
    await original(lock)
    queued = asyncio.Event()
    receipts: list[dict[str, object]] = []

    async def exchange(event: dict[str, object]) -> None:
        receipts.append(event)
        if event["state"] == "queued":
            assert lock.locked()
            assert any(not waiter.done() for waiter in lock._waiters or ())
            queued.set()

    task = asyncio.create_task(observe(lock, original, exchange, {"writer": writer}))
    try:
        await asyncio.wait_for(queued.wait(), 1)
        assert not task.done(), "second writer must still be in the original lock's queue"
        assert [event["state"] for event in receipts] == ["queued"]
        lock.release()
        assert await asyncio.wait_for(task, 1) is True
        assert lock.locked()
        assert [event["state"] for event in receipts] == ["queued", "acquired"]
        assert all(event["writer"] == writer for event in receipts)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if lock.locked():
            lock.release()


async def test_cancelled_original_waiter_never_acquires_or_releases_first_writer() -> None:
    observe = acquire_observer()
    lock = asyncio.Lock()
    await lock.acquire()
    queued = asyncio.Event()
    receipts: list[dict[str, object]] = []

    async def exchange(event: dict[str, object]) -> None:
        receipts.append(event)
        if event["state"] == "queued":
            queued.set()

    task = asyncio.create_task(observe(lock, asyncio.Lock.acquire, exchange, {"writer": "handoff"}))
    try:
        await asyncio.wait_for(queued.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        assert lock.locked(), "cancellation must preserve the first writer's ownership"
        assert not any(not waiter.done() for waiter in lock._waiters or ())
        assert [event["state"] for event in receipts] == ["queued", "cancelled"]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if lock.locked():
            lock.release()


async def test_uncontended_acquire_reports_no_fabricated_queue_wait() -> None:
    observe = acquire_observer()
    lock = asyncio.Lock()
    receipts: list[dict[str, object]] = []

    async def exchange(event: dict[str, object]) -> None:
        receipts.append(event)

    try:
        assert await observe(lock, asyncio.Lock.acquire, exchange, {"writer": "wake"}) is True
        assert lock.locked()
        assert [event["state"] for event in receipts] == ["acquired"]
    finally:
        if lock.locked():
            lock.release()


async def test_receipt_failure_after_acquire_releases_lock_without_entering_writer() -> None:
    observe = acquire_observer()
    lock = asyncio.Lock()

    async def exchange(event: dict[str, object]) -> None:
        assert event["state"] == "acquired"
        raise ProofRefused("controller refused receipt")

    with pytest.raises(ProofRefused, match="controller refused"):
        await observe(lock, asyncio.Lock.acquire, exchange, {"writer": "handoff"})
    assert not lock.locked()


@pytest.mark.parametrize("state", ["queued", "acquired", "cancelled"])
@pytest.mark.parametrize("writer", ["wake", "handoff"])
async def test_trace_accepts_scoped_original_lock_receipts(
    tmp_path: Path, state: str, writer: str
) -> None:
    trace, write = registered_trace(tmp_path)
    event = {key: write[key] for key in ("terminal_id", "session_id", "project_id", "host_epoch")}
    event.update(
        phase="lock",
        state=state,
        writer=writer,
        lock_sha256="c" * 64,
        attempt_id="owned-attempt" if writer == "handoff" else None,
    )
    await trace.accept(event)
    assert trace.events == [{**event, "sequence": 1}]


@pytest.mark.parametrize("newline", [False, True])
async def test_trace_accepts_only_source_built_continuation(tmp_path: Path, newline: bool) -> None:
    trace, event = registered_trace(tmp_path)
    prompt = build_handoff_continue_prompt() + ("\n" if newline else "")
    event["payload_sha256"] = hashlib.sha256(prompt.encode()).hexdigest()
    await trace.accept(event)
    assert trace.events[0]["payload_sha256"] == event["payload_sha256"]
    event["payload_sha256"] = hashlib.sha256((prompt + "extra").encode()).hexdigest()
    with pytest.raises(ProofRefused, match="unexpected automatic bytes"):
        await trace.accept(event)
    assert len(trace.events) == 1


@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize("clear", [False, True])
async def test_completion_relay_preserves_real_result_and_registered_identity(
    tmp_path: Path, provider: str, clear: bool
) -> None:
    trace, _ = registered_trace(tmp_path)
    own = next(iter(trace.surfaces.values()))
    own = Surface(provider, own.session_id, own.terminal_id, own.host_epoch, own.project_id)
    external, machine = str(uuid4()), str(uuid4())
    result: dict[str, Any] = {
        "success": True,
        "result": {
            "handoff_staged": True,
            "delivery_pending": True,
            "session_id": own.session_id,
            "attempt_id": "actual-attempt",
            "clear_session": clear,
            "session_type": "terminal",
            "cli": provider,
        },
    }
    build = getattr(trace_module, "completion_envelope", None)
    assert callable(build), "public staged result needs an identity-bound controller relay"
    before = datetime.now(UTC)
    envelope = build(own, external, machine, result)
    assert envelope["source"] == provider
    assert before <= datetime.fromisoformat(envelope["enqueued_at"]) <= datetime.now(UTC)
    data = envelope["input_data"]
    assert data["session_id"] == external and data["machine_id"] == machine
    assert data["project_id"] == own.project_id
    assert data["tool_output"] is result
    normalize_tool_fields(data)
    event = HookEvent(
        event_type=HookEventType.AFTER_TOOL,
        session_id=external,
        source=SessionSource(provider),
        timestamp=datetime.now(UTC),
        data=data,
        metadata={"_platform_session_id": own.session_id},
    )
    staged = staged_handoff_from_event(event)
    assert staged is not None
    assert staged.session_id == own.session_id
    assert staged.attempt_id == "actual-attempt" and staged.clear_session is clear
    for field, value in (("session_id", str(uuid4())), ("handoff_staged", False)):
        malformed = {**result, "result": {**result["result"], field: value}}
        with pytest.raises(ProofRefused, match="staged result"):
            build(own, external, machine, malformed)


@pytest.mark.parametrize("first_writer", ["wake", "handoff"])
async def test_installed_observer_keeps_original_shared_lock_and_task_reentrancy(
    first_writer: str,
) -> None:
    module = importlib.import_module("tests.e2e.composer_proof_race_observer")
    terminal = make_memory_terminal()
    coordinator = WriteCoordinator(
        cast(UnresolvedWriteStore, MemoryTerminalStore(terminal)),
        runtime_registry(FakeRuntime()),
        lease_registry=TerminalLeaseRegistry(daemon_epoch="proof-epoch"),
    )
    original_lock = coordinator.logical_action_lock(terminal.id)
    queued = asyncio.Event()
    finished = asyncio.Event()
    receipts: list[dict[str, object]] = []

    async def exchange(event: dict[str, object]) -> None:
        receipts.append(event)
        if event["state"] == "queued":
            queued.set()

    observer = module.RaceObservation(exchange)
    second = "handoff" if first_writer == "wake" else "wake"

    async def second_writer() -> None:
        with observer.writer(second, "owned-attempt" if second == "handoff" else None):
            async with composer_action_lock(terminal.id, coordinator):
                finished.set()

    with (
        observer.install(),
        observer.writer(first_writer, "owned-attempt" if first_writer == "handoff" else None),
    ):
        assert coordinator.logical_action_lock(terminal.id) is original_lock
        async with composer_action_lock(terminal.id, coordinator):
            async with composer_action_lock(terminal.id, coordinator):
                assert original_lock.locked()
            task = asyncio.create_task(second_writer())
            try:
                await asyncio.wait_for(queued.wait(), 1)
                assert not finished.is_set()
                assert [(item["phase"], item["state"]) for item in receipts] == [
                    ("admission", "ready"),
                    ("lock", "acquired"),
                    ("admission", "ready"),
                    ("lock", "queued"),
                ]
            except BaseException:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise
        await asyncio.wait_for(task, 1)
    assert finished.is_set() and not original_lock.locked()
    assert [item["writer"] for item in receipts] == [
        first_writer,
        first_writer,
        second,
        second,
        second,
    ]
    assert len({item["lock_sha256"] for item in receipts}) == 1
    assert all(item["terminal_id"] == terminal.id for item in receipts)
    assert all(item["session_id"] == terminal.session_id for item in receipts)


@pytest.mark.parametrize("first_writer", ["wake", "handoff"])
async def test_real_entrypoints_label_original_shared_lock(first_writer: str) -> None:
    module = importlib.import_module("tests.e2e.composer_proof_race_observer")
    terminal = replace(make_memory_terminal(), session_id=str(uuid4()), host_epoch=str(uuid4()))
    coordinator = WriteCoordinator(
        cast(UnresolvedWriteStore, MemoryTerminalStore(terminal)),
        runtime_registry(FakeRuntime()),
        lease_registry=TerminalLeaseRegistry(daemon_epoch="proof-epoch"),
    )
    entered, release, queued = asyncio.Event(), asyncio.Event(), asyncio.Event()
    receipts: list[dict[str, object]] = []
    attempt = str(uuid4())
    claimed = ClaimedHandoffDelivery(str(terminal.session_id), attempt, str(uuid4()), False)
    manager = MagicMock()
    manager.get.return_value = SimpleNamespace(source="claude")

    async def exchange(event: dict[str, object]) -> None:
        receipts.append(event)
        if event.get("state") == "queued":
            queued.set()

    async def original(kind: str) -> None:
        async with composer_action_lock(terminal.id, coordinator):
            if kind == first_writer:
                entered.set()
                await release.wait()

    async def wake(_dispatcher: Any, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        await original("wake")
        return {"delivered": True}

    async def handoff(_claimed: ClaimedHandoffDelivery, **_kwargs: Any) -> None:
        await original("handoff")

    observer = module.RaceObservation(exchange)
    with (
        patch.object(WakeDispatcher, "_dispatch_live_wake_unlocked", wake),
        patch.object(terminal_handoff_delivery, "_settle_delivery", handoff),
        observer.install(),
    ):

        async def invoke(kind: str) -> None:
            if kind == "wake":
                await WakeDispatcher._dispatch_live_wake_unlocked(
                    cast(WakeDispatcher, object()), str(terminal.session_id)
                )
            else:
                await terminal_handoff_delivery._settle_delivery(
                    claimed,
                    session_manager=manager,
                    agent_run_manager=MagicMock(),
                    terminal_manager=None,
                    terminal_runtime_registry=None,
                )

        first = asyncio.create_task(invoke(first_writer))
        second: asyncio.Task[None] | None = None
        try:
            await asyncio.wait_for(entered.wait(), 1)
            second_kind = "handoff" if first_writer == "wake" else "wake"
            second = asyncio.create_task(invoke(second_kind))
            await asyncio.wait_for(queued.wait(), 1)
            assert not first.done() and not second.done()
            release.set()
            await asyncio.wait_for(asyncio.gather(first, second), 1)
        finally:
            release.set()
            first.cancel()
            if second is not None:
                second.cancel()
            await asyncio.gather(
                first, *([] if second is None else [second]), return_exceptions=True
            )
    locks = [item for item in receipts if item.get("phase") == "lock"]
    assert [item["state"] for item in locks] == ["acquired", "queued", "acquired"]
    assert [item["writer"] for item in locks] == [first_writer, second_kind, second_kind]
    assert len({item["lock_sha256"] for item in locks}) == 1
    assert all(
        item["attempt_id"] == (attempt if item["writer"] == "handoff" else None) for item in locks
    )


@pytest.mark.parametrize("transport", ["direct", "socket", "quiesce"])
async def test_control_cancels_actual_caller_after_binding_and_waits_owned_work(
    transport: str,
) -> None:
    module = importlib.import_module("tests.e2e.composer_proof_race_observer")
    terminal = replace(
        make_memory_terminal(),
        session_id=str(uuid4()),
        host_epoch=str(uuid4()),
        project_id="00000000-0000-0000-0000-000000000e2e",
    )
    coordinator = WriteCoordinator(
        cast(UnresolvedWriteStore, MemoryTerminalStore(terminal)),
        runtime_registry(FakeRuntime()),
        lease_registry=TerminalLeaseRegistry(daemon_epoch="proof-epoch"),
    )
    held, release, physical_done = asyncio.Event(), asyncio.Event(), asyncio.Event()
    claimed = ClaimedHandoffDelivery(str(terminal.session_id), str(uuid4()), str(uuid4()), False)
    surface = Surface(
        "claude",
        str(terminal.session_id),
        terminal.id,
        str(terminal.host_epoch),
        str(terminal.project_id),
    )
    manager = MagicMock()
    manager.get.return_value = SimpleNamespace(source="claude")
    receipts: list[dict[str, object]] = []

    async def exchange(event: dict[str, object]) -> None:
        receipts.append(event)

    async def physical() -> None:
        async with composer_action_lock(terminal.id, coordinator):
            held.set()
            await release.wait()
            physical_done.set()

    async def handoff(_claimed: ClaimedHandoffDelivery, **_kwargs: Any) -> None:
        await shielded_terminal_delivery("real-control-test", physical, raise_if_closed=True)

    observer = module.RaceObservation(exchange)
    control: cleanup_module.CleanupControl | None = None
    server: asyncio.Task[None] | None = None
    quiescer: asyncio.Task[None] | None = None
    quiescing = asyncio.Event()

    async def drain(surfaces: list[Surface]) -> None:
        quiescing.set()
        await observer.quiesce(surfaces)

    temporary = tempfile.TemporaryDirectory(prefix="r2proof-", dir="/tmp")
    mocks = ExitStack()
    if transport in {"socket", "quiesce"}:
        control = cleanup_module.CleanupControl(
            Path(temporary.name) / "control.sock",
            cancel_handoff=observer.cancel,
            drain_handoffs=drain if transport == "quiesce" else None,
        )
        dispatcher = WakeDispatcher(manager, MagicMock())
        mocks.enter_context(
            patch.object(
                dispatcher,
                "_terminal_route_for_session",
                AsyncMock(return_value=SimpleNamespace(managed_terminal=terminal)),
            )
        )
        server = asyncio.create_task(control.serve(dispatcher))
        await asyncio.wait_for(control.started.wait(), 1)
    with patch.object(terminal_handoff_delivery, "_settle_delivery", handoff), observer.install():
        caller = asyncio.create_task(
            terminal_handoff_delivery._settle_delivery(
                claimed,
                session_manager=manager,
                agent_run_manager=MagicMock(),
                terminal_manager=None,
                terminal_runtime_registry=None,
            )
        )
        try:
            await asyncio.wait_for(held.wait(), 1)
            wrong = Surface(
                "claude",
                str(uuid4()),
                terminal.id,
                str(terminal.host_epoch),
                str(terminal.project_id),
            )
            if control is None:
                with pytest.raises(ProofRefused, match="binding"):
                    observer.cancel(wrong, claimed.attempt_id)
                assert observer.cancel(surface, claimed.attempt_id) is True
            elif transport == "socket":
                with pytest.raises(ProofRefused, match="cancel"):
                    await cleanup_module.cancel_caller(control.socket, wrong, claimed.attempt_id)
                assert not caller.cancelling()
                await cleanup_module.cancel_caller(control.socket, surface, claimed.attempt_id)
            else:
                quiescer = asyncio.create_task(
                    cleanup_module.quiesce_proof(control.socket, [surface])
                )
                await asyncio.wait_for(quiescing.wait(), 1)
            fence: asyncio.Future[None] = asyncio.get_running_loop().create_future()
            asyncio.get_running_loop().call_soon(fence.set_result, None)
            await fence
            assert not caller.done() and not physical_done.is_set()
            if quiescer is not None:
                assert not quiescer.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(caller, 1)
            if quiescer is not None:
                await asyncio.wait_for(quiescer, 1)
                with pytest.raises(ProofRefused, match="quiesced"):
                    await terminal_handoff_delivery._settle_delivery(
                        ClaimedHandoffDelivery(
                            str(terminal.session_id), str(uuid4()), str(uuid4()), False
                        ),
                        session_manager=manager,
                        agent_run_manager=MagicMock(),
                        terminal_manager=None,
                        terminal_runtime_registry=None,
                    )
        finally:
            release.set()
            caller.cancel()
            await asyncio.gather(caller, return_exceptions=True)
            if quiescer is not None:
                quiescer.cancel()
                await asyncio.gather(quiescer, return_exceptions=True)
            if server is not None:
                server.cancel()
                await asyncio.gather(server, return_exceptions=True)
            mocks.close()
            temporary.cleanup()
    assert physical_done.is_set()
    assert [event["state"] for event in receipts if event.get("phase") == "caller"] == ["cancelled"]
    with pytest.raises(ProofRefused, match="caller"):
        observer.cancel(surface, claimed.attempt_id)


@pytest.mark.parametrize("state", ["settled", "cancelled"])
async def test_trace_accepts_actual_caller_completion_only(tmp_path: Path, state: str) -> None:
    trace, original = registered_trace(tmp_path)
    event = {
        key: original[key] for key in ("session_id", "terminal_id", "host_epoch", "project_id")
    }
    event.update(phase="caller", state=state, attempt_id=str(uuid4()))
    await trace.accept(event)
    assert trace.events[0]["state"] == state
    with pytest.raises(ProofRefused):
        await trace.accept({**event, "state": "pretend_cancelled"})
    with pytest.raises(ProofRefused):
        await trace.accept({**event, "attempt_id": None})


async def test_clear_trace_rebinds_only_after_readback_and_keeps_exact_caller_receipt(
    tmp_path: Path,
) -> None:
    trace, event = registered_trace(tmp_path)
    old = trace.surfaces[str(event["terminal_id"])]
    new = replace(old, session_id=str(uuid4()))
    attempt = str(uuid4())
    readbacks: list[tuple[Surface, Surface, str]] = []

    async def authorize(predecessor: Surface, successor: Surface, attempt_id: str) -> None:
        readbacks.append((predecessor, successor, attempt_id))

    trace.authorize_clear = authorize
    trace.expect_clear(old, attempt)
    await trace.accept({**event, "session_id": new.session_id})
    assert readbacks == [(old, new, attempt)]
    assert trace.surfaces[old.terminal_id] == new
    caller: dict[str, object] = {
        "phase": "caller",
        "terminal_id": old.terminal_id,
        "session_id": old.session_id,
        "project_id": old.project_id,
        "host_epoch": old.host_epoch,
        "attempt_id": attempt,
        "state": "settled",
    }
    await trace.accept(caller)
    with pytest.raises(ProofRefused, match="binding"):
        await trace.accept(event)
    with pytest.raises(ProofRefused, match="binding"):
        await trace.accept({**caller, "attempt_id": str(uuid4())})
    assert len(trace.events) == 2


async def test_clear_trace_refuses_an_unarmed_or_excluded_successor(tmp_path: Path) -> None:
    trace, event = registered_trace(tmp_path)
    old = trace.surfaces[str(event["terminal_id"])]
    authorizer = AsyncMock()
    trace.authorize_clear = authorizer
    with pytest.raises(ProofRefused, match="binding"):
        await trace.accept({**event, "session_id": str(uuid4())})
    trace.expect_clear(old, str(uuid4()))
    excluded = next(iter(trace.scope.excluded))
    with pytest.raises(ProofRefused):
        await trace.accept({**event, "session_id": excluded})
    authorizer.assert_not_awaited()
    assert not trace.events


@pytest.mark.parametrize("change", ["none", "unconsumed", "parent", "terminal", "attachment"])
async def test_clear_readback_authorizes_only_actual_lineage_and_attachment(
    tmp_path: Path, change: str
) -> None:
    module = importlib.import_module("tests.e2e.composer_proof_races")
    trace, event = registered_trace(tmp_path)
    old = trace.surfaces[str(event["terminal_id"])]
    new = replace(old, session_id=str(uuid4()))
    attempt = str(uuid4())
    predecessor: dict[str, Any] = {
        "id": old.session_id,
        "source": old.provider,
        "project_id": old.project_id,
        "external_id": str(uuid4()),
        "machine_id": str(uuid4()),
    }
    successor = {
        **predecessor,
        "id": new.session_id,
        "external_id": str(uuid4()),
        "parent_session_id": old.session_id,
    }
    marker = {
        "attempt_id": attempt,
        "consumed_by": new.session_id,
        "handoff_record_id": str(uuid4()),
    }
    row = {
        "id": new.terminal_id,
        "session_id": new.session_id,
        "project_id": new.project_id,
    }
    locator = SimpleNamespace(frame_host_epoch=new.host_epoch, host_socket="/owned/host.sock")
    seat = SimpleNamespace(surface=old, external_id=predecessor["external_id"], locator=locator)
    proof = MagicMock()
    proof.scope, proof.trace, proof.seats, proof.evidence = trace.scope, trace, [seat], []
    proof.terminal = AsyncMock(return_value=row)
    proof.locator.return_value = locator
    proof.call = AsyncMock(side_effect=[predecessor, successor])
    variables = MagicMock()
    variables.get_variables.return_value = {CLEAR_ATTEMPT_VARIABLE: marker}
    if change == "unconsumed":
        marker["consumed_by"] = str(uuid4())
    elif change == "parent":
        successor["parent_session_id"] = str(uuid4())
    elif change == "terminal":
        row["session_id"] = str(uuid4())
    elif change == "attachment":
        proof.locator.return_value = SimpleNamespace(
            frame_host_epoch=new.host_epoch, host_socket="/foreign/host.sock"
        )
    module.ComposerRaces(proof, variables)
    trace.expect_clear(old, attempt)
    observed = {**event, "session_id": new.session_id}
    if change == "none":
        await trace.accept(observed)
        assert seat.surface == new and seat.external_id == successor["external_id"]
        variables.get_variables.assert_called_once_with(old.session_id)
        proof.call.assert_any_await("gobby-sessions", "get_session", {"session_id": old.session_id})
        proof.call.assert_any_await("gobby-sessions", "get_session", {"session_id": new.session_id})
        assert proof.evidence[0]["attempt_id"] == attempt
    else:
        with pytest.raises(ProofRefused):
            await trace.accept(observed)
        assert seat.surface == old and seat.external_id == predecessor["external_id"]
        assert trace.surfaces[old.terminal_id] == old and not trace.events


@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize("clear", [False, True])
@pytest.mark.parametrize("feedback_refused", [False, True])
async def test_handoff_stage_uses_public_bound_mcp_and_complete_instruction_pages(
    tmp_path: Path, provider: str, clear: bool, feedback_refused: bool
) -> None:
    module = importlib.import_module("tests.e2e.composer_proof_races")
    trace, event = registered_trace(tmp_path)
    old = trace.surfaces[str(event["terminal_id"])]
    own = replace(old, provider=provider)
    trace.surfaces[own.terminal_id] = own
    details = {
        "id": own.session_id,
        "source": provider,
        "project_id": own.project_id,
        "external_id": str(uuid4()),
        "machine_id": str(uuid4()),
    }
    seat = SimpleNamespace(surface=own, external_id=details["external_id"])
    staged: dict[str, Any] = {
        "success": True,
        "result": {
            "handoff_staged": True,
            "delivery_pending": True,
            "session_id": own.session_id,
            "attempt_id": str(uuid4()),
            "clear_session": clear,
            "session_type": "terminal",
            "cli": provider,
        },
    }
    calls: list[dict[str, Any]] = []
    leases: set[tuple[str, str]] = set()

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Gobby-Session-Id"] == own.session_id
        body = json.loads(request.content)
        key = (body["server_name"], body["tool_name"])
        if request.url.path == "/api/mcp/tools/schema":
            leases.add(key)
            return httpx.Response(200, json={"success": True})
        assert request.url.path == "/api/mcp/tools/call" and key in leases
        calls.append(body)
        if body["tool_name"] == "get_skill_file":
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "result": {
                        "file": {
                            "content": "exact instruction page",
                            "skill_name": "gobby",
                            "path": "references/sessions/handoffs.md",
                        },
                        "page": {
                            "complete": "cursor" in body["arguments"],
                            "next_cursor": None if "cursor" in body["arguments"] else "page-two",
                        },
                    },
                },
            )
        if body["tool_name"] == "feedback":
            return httpx.Response(
                200, json={"success": True, "result": {"success": not feedback_refused}}
            )
        assert body["tool_name"] == "set_handoff"
        assert body["arguments"]["clear_session"] is clear
        return httpx.Response(200, json=staged)

    client = AsyncMCPTestClient.__new__(AsyncMCPTestClient)
    client.session_id = None
    client.client = httpx.AsyncClient(
        base_url="http://proof.invalid", transport=httpx.MockTransport(handle)
    )
    proof = MagicMock()
    proof.daemon = SimpleNamespace(http_url="http://proof.invalid", gobby_home=tmp_path)
    proof.trace, proof.seats, proof.evidence = trace, [seat], []
    proof.handoffs_pending = set()
    proof.validate, proof.call = AsyncMock(), AsyncMock(return_value=details)
    controller = module.ComposerRaces(proof, MagicMock())
    with (
        patch.object(module, "AsyncMCPTestClient", return_value=client),
        patch.object(module, "daemon_token", return_value="isolated-unit-token"),
    ):
        if feedback_refused:
            with pytest.raises(ProofRefused, match="feedback"):
                await controller.stage(seat, clear=clear)
            assert client.client.is_closed
            assert [call["tool_name"] for call in calls] == [
                "get_skill_file",
                "get_skill_file",
                "feedback",
            ]
            assert not proof.handoffs_pending and not trace.clear_attempts
            return
        envelope = await controller.stage(seat, clear=clear)
    assert client.client.is_closed
    assert envelope["input_data"]["tool_output"] == staged
    assert envelope["input_data"]["session_id"] == details["external_id"]
    assert [call["tool_name"] for call in calls] == [
        "get_skill_file",
        "get_skill_file",
        "feedback",
        "set_handoff",
    ]
    assert calls[1]["arguments"] == {"cursor": "page-two"}
    assert calls[2]["arguments"] == {"observations": []}
    assert (own.terminal_id in trace.clear_attempts) is clear
    proof.validate.assert_awaited_once_with(seat)


async def test_cleanup_preserves_isolated_state_with_an_unsettled_handoff(tmp_path: Path) -> None:
    proof = LiveProof.__new__(LiveProof)
    proof.trace, _ = registered_trace(tmp_path)
    proof.seats = []
    proof.mcp, proof.http = MagicMock(), AsyncMock()
    proof.created_terminal_ids = []
    proof.creation_uncertain = False
    proof.handoffs_pending = {str(uuid4())}
    with patch("tests.e2e.composer_proof_live.quiesce_proof", new_callable=AsyncMock):
        with pytest.raises(ProofRefused, match="preserve isolated state"):
            await proof.close()


@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize("rejected", [False, True])
@pytest.mark.parametrize("identity_changed", [False, True])
async def test_completion_transport_relays_once_without_manufacturing_lifecycle(
    tmp_path: Path, provider: str, rejected: bool, identity_changed: bool
) -> None:
    module = importlib.import_module("tests.e2e.composer_proof_races")
    trace, event = registered_trace(tmp_path)
    own = replace(trace.surfaces[str(event["terminal_id"])], provider=provider)
    trace.surfaces[own.terminal_id] = own
    external_id, machine_id = str(uuid4()), str(uuid4())
    seat = SimpleNamespace(surface=own, external_id=external_id)
    raw = {
        "success": True,
        "result": {
            "handoff_staged": True,
            "delivery_pending": True,
            "session_id": own.session_id,
            "attempt_id": str(uuid4()),
            "clear_session": False,
            "session_type": "terminal",
            "cli": provider,
        },
    }
    envelope = trace_module.completion_envelope(own, external_id, machine_id, raw)
    requests: list[dict[str, Any]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/hooks/execute"
        requests.append(json.loads(request.content))
        return httpx.Response(503 if rejected else 200, json={"continue": True})

    async with httpx.AsyncClient(
        base_url="http://proof.invalid", transport=httpx.MockTransport(handle)
    ) as http:
        proof = MagicMock()
        proof.trace, proof.seats, proof.evidence, proof.http = trace, [seat], [], http
        proof.handoffs_pending = {own.session_id}
        proof.validate = AsyncMock()
        proof.call = AsyncMock(
            return_value={
                "id": own.session_id,
                "source": provider,
                "project_id": own.project_id,
                "external_id": str(uuid4()) if identity_changed else external_id,
                "machine_id": machine_id,
            }
        )
        controller = module.ComposerRaces(proof, MagicMock())
        if identity_changed:
            with pytest.raises(ProofRefused, match="identity"):
                await controller.relay(seat, envelope)
            assert not proof.evidence
        elif rejected:
            with pytest.raises(httpx.HTTPStatusError):
                await controller.relay(seat, envelope)
            assert not proof.evidence
        else:
            await controller.relay(seat, envelope)
            assert proof.evidence[0]["transport"] == "controller public hook; real staged result"
        assert requests == ([] if identity_changed else [envelope])
        assert not trace.events
        assert proof.handoffs_pending == {own.session_id}


@pytest.mark.parametrize("clear", [False, True])
@pytest.mark.parametrize("failed", [False, True])
@pytest.mark.parametrize("corruption", [None, "receipt", "payload", "after_receipt"])
async def test_settlement_requires_canonical_receipt_and_bound_public_consumption(
    tmp_path: Path, clear: bool, failed: bool, corruption: str | None
) -> None:
    module = importlib.import_module("tests.e2e.composer_proof_races")
    trace, event = registered_trace(tmp_path)
    predecessor = trace.surfaces[str(event["terminal_id"])]
    own = replace(predecessor, session_id=str(uuid4())) if clear and not failed else predecessor
    trace.surfaces[own.terminal_id] = own
    seat = SimpleNamespace(surface=own, external_id=str(uuid4()))
    attempt_id, record_id = str(uuid4()), str(uuid4())
    if own != predecessor:
        trace.predecessors[(predecessor.session_id, attempt_id)] = predecessor
    marker = {"attempt_id": attempt_id, "handoff_record_id": record_id, "clear_session": clear}
    variables = MagicMock()
    snapshot = (
        {
            FAILED_HANDOFF_VARIABLE: {
                "attempt_id": attempt_id,
                "handoff_record_id": record_id,
                "delivery_state": "failed_not_deliverable",
            },
            HANDOFF_DISPATCH_GATE_VARIABLE: {
                "attempt_id": attempt_id,
                "clear_session": clear,
                "delivery_pending": False,
                "delivery_failed": True,
                "delivery_state": "failed_not_deliverable",
            },
        }
        if failed
        else {PENDING_HANDOFF_VARIABLE: marker}
    )
    variables.get_variables.return_value = snapshot
    markdown = "## Current State\nExact isolated authored handoff.\n"
    variables.db.fetchone.return_value = {
        "id": record_id,
        "session_id": predecessor.session_id,
        "rendered_markdown": markdown,
    }
    receipt = {
        "handoff_id": record_id,
        "attempt_id": attempt_id,
        "boundary_kind": "clear" if clear else "compact",
        "continuation_session_id": own.session_id,
    }
    expected_receipts = [] if failed else [receipt]
    before = [receipt] if failed else []
    variables.db.fetchall.side_effect = [
        before if corruption == "receipt" else expected_receipts,
        [receipt, receipt] if corruption == "after_receipt" else expected_receipts,
    ]
    await trace.accept(
        {
            "phase": "caller",
            "state": "cancelled",
            "attempt_id": attempt_id,
            "terminal_id": own.terminal_id,
            "session_id": own.session_id,
            "project_id": own.project_id,
            "host_epoch": own.host_epoch,
        }
    )
    requests: list[dict[str, Any]] = []
    clients: list[AsyncMCPTestClient] = []

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Gobby-Session-Id"] == own.session_id
        body = json.loads(request.content)
        if request.url.path == "/api/mcp/tools/schema":
            assert body == {"server_name": "gobby-sessions", "tool_name": "get_handoff"}
            return httpx.Response(200, json={"success": True})
        assert request.url.path == "/api/mcp/tools/call"
        requests.append(body)
        found = failed or len(requests) == 1
        result = {
            "success": True,
            "found": found,
            "session_id": predecessor.session_id if found else None,
            "handoff": ("wrong payload" if corruption == "payload" else markdown) if found else "",
        }
        if failed:
            result.update(attempt_id=attempt_id, delivery_state="failed_not_deliverable")
        return httpx.Response(200, json={"success": True, "result": result})

    def client_factory(*_args: Any) -> AsyncMCPTestClient:
        client = AsyncMCPTestClient.__new__(AsyncMCPTestClient)
        client.session_id = None
        client.client = httpx.AsyncClient(
            base_url="http://proof.invalid", transport=httpx.MockTransport(handle)
        )
        clients.append(client)
        return client

    proof = MagicMock()
    proof.daemon = SimpleNamespace(http_url="http://proof.invalid", gobby_home=tmp_path)
    proof.trace, proof.seats, proof.evidence = trace, [seat], []
    proof.scope = trace.scope
    proof.handoffs_pending = {predecessor.session_id}
    proof.validate = AsyncMock()
    controller = module.ComposerRaces(proof, variables)
    with (
        patch.object(module, "AsyncMCPTestClient", side_effect=client_factory),
        patch.object(module, "daemon_token", return_value="isolated-unit-token"),
    ):
        arguments: dict[str, Any] = {
            "predecessor": predecessor,
            "attempt_id": attempt_id,
            "clear": clear,
            "after": 0,
            "outcome": "failed" if failed else "delivered",
            "caller_state": "cancelled",
        }
        if corruption:
            with pytest.raises(ProofRefused):
                await controller.settle(seat, **arguments)
            assert proof.handoffs_pending == {predecessor.session_id}
        else:
            await controller.settle(seat, **arguments)
            assert not proof.handoffs_pending
            assert len(requests) == 2
            assert [body["arguments"] for body in requests] == (
                [{"failed_attempt_id": attempt_id}] * 2 if failed else [{}, {}]
            )
            assert proof.evidence[-1]["receipt_count"] == (0 if failed else 1)
    assert all(client.client.is_closed for client in clients)


@pytest.mark.parametrize("rebound", [False, True])
async def test_durable_notice_keeps_original_target_after_clear(
    tmp_path: Path, rebound: bool
) -> None:
    trace, event = registered_trace(tmp_path)
    predecessor = trace.surfaces[str(event["terminal_id"])]
    successor = replace(predecessor, session_id=str(uuid4()))
    trace.surfaces[predecessor.terminal_id] = successor
    message_id = str(uuid4())
    proof = LiveProof.__new__(LiveProof)
    proof.scope, proof.evidence = trace.scope, []
    read = AsyncMock(
        return_value={
            "message": {
                "id": message_id,
                "to_session": successor.session_id if rebound else predecessor.session_id,
                "delivered_at": None,
            }
        }
    )
    with patch.object(proof, "call", read):
        if rebound:
            with pytest.raises(ProofRefused, match="durable"):
                await proof.durable(predecessor, message_id)
            assert not proof.evidence
        else:
            await proof.durable(predecessor, message_id)
            assert proof.evidence == [
                {"message_id": message_id, "durable": True, "delivered_at": None}
            ]
    read.assert_awaited_once_with(
        "gobby-agents", "get_inter_session_message", {"message_id": message_id}
    )


async def test_original_waiter_receipt_rereads_binding_after_acquisition() -> None:
    module = importlib.import_module("tests.e2e.composer_proof_race_observer")
    terminal = replace(make_memory_terminal(), session_id=str(uuid4()))
    successor = replace(terminal, session_id=str(uuid4()))
    store = MemoryTerminalStore(terminal)
    coordinator = WriteCoordinator(
        cast(UnresolvedWriteStore, store),
        runtime_registry(FakeRuntime()),
        lease_registry=TerminalLeaseRegistry(daemon_epoch="proof-epoch"),
    )
    receipts: list[dict[str, object]] = []
    queued = asyncio.Event()

    async def exchange(event: dict[str, object]) -> None:
        receipts.append(event)
        if event.get("state") == "queued":
            queued.set()

    observer = module.RaceObservation(exchange)

    async def wake() -> None:
        with observer.writer("wake"):
            async with composer_action_lock(terminal.id, coordinator):
                assert store.get(terminal.id) == successor

    with observer.install():
        async with asyncio.TaskGroup() as group:
            async with composer_action_lock(terminal.id, coordinator):
                task = group.create_task(wake())
                await asyncio.wait_for(queued.wait(), 1)
                assert not task.done()
                store.rows[terminal.id] = successor
    assert [(item["state"], item["session_id"]) for item in receipts] == [
        ("ready", terminal.session_id),
        ("queued", terminal.session_id),
        ("acquired", successor.session_id),
    ]
    assert len({item["lock_sha256"] for item in receipts}) == 1
    assert all(item["host_epoch"] == terminal.host_epoch for item in receipts)


@pytest.mark.parametrize("changed_epoch", [False, True])
async def test_wake_outcome_keeps_request_target_and_rereads_original_physical_row(
    changed_epoch: bool,
) -> None:
    module = importlib.import_module("tests.e2e.composer_proof_race_observer")
    terminal = replace(make_memory_terminal(), session_id=str(uuid4()))
    successor = replace(
        terminal,
        session_id=str(uuid4()),
        host_epoch=str(uuid4()) if changed_epoch else terminal.host_epoch,
    )
    store = MemoryTerminalStore(terminal)
    coordinator = WriteCoordinator(
        cast(UnresolvedWriteStore, store),
        runtime_registry(FakeRuntime()),
        lease_registry=TerminalLeaseRegistry(daemon_epoch="proof-epoch"),
    )
    receipts: list[dict[str, object]] = []

    async def exchange(event: dict[str, object]) -> None:
        receipts.append(event)

    observer = module.RaceObservation(exchange)
    sessions = MagicMock()
    sessions.get.return_value = SimpleNamespace(id=terminal.session_id)
    dispatcher = WakeDispatcher(sessions, MagicMock())
    route = AsyncMock(return_value=SimpleNamespace(managed_terminal=terminal))
    calls: list[str] = []

    async def original(
        _dispatcher: WakeDispatcher, session_id: str, **_kwargs: Any
    ) -> dict[str, Any]:
        calls.append(session_id)
        store.rows[terminal.id] = successor
        return {"delivered": False, "skipped": "session_awaiting_handoff"}

    with observer.install(), patch.object(dispatcher, "_terminal_route_for_session", route):
        lock = coordinator.logical_action_lock(terminal.id)
        assert not lock.locked()
        if changed_epoch:
            with pytest.raises(ProofRefused, match="physical"):
                await observer.observe_wake(dispatcher, original, str(terminal.session_id))
            assert not receipts
        else:
            result = await observer.observe_wake(dispatcher, original, str(terminal.session_id))
            assert result == {"delivered": False, "skipped": "session_awaiting_handoff"}
            assert len(receipts) == 1
            assert receipts[0]["requested_session_id"] == terminal.session_id
            assert receipts[0]["session_id"] == successor.session_id
            assert receipts[0]["terminal_id"] == terminal.id
        assert calls == [terminal.session_id]
        route.assert_awaited_once()


@pytest.mark.parametrize("wrong_target", [False, True])
async def test_trace_wake_receipt_accepts_only_current_or_canonical_predecessor_request(
    tmp_path: Path, wrong_target: bool
) -> None:
    trace, event = registered_trace(tmp_path)
    predecessor = trace.surfaces[str(event["terminal_id"])]
    own = replace(predecessor, session_id=str(uuid4()))
    trace.surfaces[own.terminal_id] = own
    trace.predecessors[(predecessor.session_id, str(uuid4()))] = predecessor
    receipt = {
        "phase": "wake",
        "terminal_id": own.terminal_id,
        "session_id": own.session_id,
        "project_id": own.project_id,
        "host_epoch": own.host_epoch,
        "requested_session_id": str(uuid4()) if wrong_target else predecessor.session_id,
        "delivered": False,
        "skipped": "session_awaiting_handoff",
        "priority": "urgent",
        "monotonic": 10.0,
    }
    if wrong_target:
        with pytest.raises(ProofRefused):
            await trace.accept(receipt)
        assert not trace.events
    else:
        await trace.accept(receipt)
        assert trace.events[0]["requested_session_id"] == predecessor.session_id
        assert trace.events[0]["session_id"] == own.session_id


async def test_acquisition_barrier_holds_original_lock_before_writer_body(tmp_path: Path) -> None:
    module = importlib.import_module("tests.e2e.composer_proof_race_observer")
    trace, event = registered_trace(tmp_path)
    own = trace.surfaces[str(event["terminal_id"])]
    terminal = replace(
        make_memory_terminal(),
        id=own.terminal_id,
        session_id=own.session_id,
        project_id=own.project_id,
        host_epoch=own.host_epoch,
    )
    coordinator = WriteCoordinator(
        cast(UnresolvedWriteStore, MemoryTerminalStore(terminal)),
        runtime_registry(FakeRuntime()),
        lease_registry=TerminalLeaseRegistry(daemon_epoch="proof-epoch"),
    )
    observer = module.RaceObservation(trace.accept)
    trace.arm_wake_acquisition(own)
    entered = asyncio.Event()
    order: list[str] = []

    async def wake() -> None:
        with observer.writer("wake"):
            async with composer_action_lock(terminal.id, coordinator):
                entered.set()
                order.append("wake")

    async def handoff() -> None:
        with observer.writer("handoff", str(uuid4())):
            async with composer_action_lock(terminal.id, coordinator):
                order.append("handoff")

    with observer.install():
        async with asyncio.timeout(3), asyncio.TaskGroup() as group:
            group.create_task(wake())
            await trace.acquired.wait()
            assert coordinator.logical_action_lock(terminal.id).locked()
            assert not entered.is_set()
            group.create_task(handoff())
            queued = await trace.wait_for(
                lambda receipt: receipt.get("state") == "queued"
                and receipt.get("writer") == "handoff",
                after=0,
                timeout=1,
            )
            assert not order
            trace.acquisition_release.set()
    assert order == ["wake", "handoff"]
    assert not coordinator.logical_action_lock(terminal.id).locked()
    assert len({receipt["lock_sha256"] for receipt in trace.events}) == 1
    assert queued["phase"] == "lock"


async def test_admission_barrier_releases_into_original_lock_waiter(tmp_path: Path) -> None:
    module = importlib.import_module("tests.e2e.composer_proof_race_observer")
    trace, event = registered_trace(tmp_path)
    own = trace.surfaces[str(event["terminal_id"])]
    terminal = replace(
        make_memory_terminal(),
        id=own.terminal_id,
        session_id=own.session_id,
        host_epoch=own.host_epoch,
        project_id=own.project_id,
    )
    coordinator = WriteCoordinator(
        cast(UnresolvedWriteStore, MemoryTerminalStore(terminal)),
        runtime_registry(FakeRuntime()),
        lease_registry=TerminalLeaseRegistry(daemon_epoch="proof-epoch"),
    )
    observer = module.RaceObservation(trace.accept)
    attempt = str(uuid4())
    trace.arm_admission(own, "wake")

    async def wake() -> None:
        with observer.writer("wake"):
            async with composer_action_lock(terminal.id, coordinator):
                pass

    with observer.install():
        lock = coordinator.logical_action_lock(terminal.id)
        task = asyncio.create_task(wake())
        try:
            await asyncio.wait_for(trace.admitted.wait(), 1)
            assert not lock.locked() and not task.done()
            with observer.writer("handoff", attempt):
                async with composer_action_lock(terminal.id, coordinator):
                    trace.admission_release.set()
                    await trace.wait_for(
                        lambda item: item.get("phase") == "lock"
                        and item.get("writer") == "wake"
                        and item.get("state") == "queued",
                        after=0,
                        timeout=1,
                    )
                    assert lock.locked() and not task.done()
                    assert lock._waiters and not lock._waiters[-1].done()
            await asyncio.wait_for(task, 1)
        finally:
            trace.admission_release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    receipts = [item for item in trace.events if item["phase"] == "lock"]
    assert [(item["writer"], item["state"]) for item in receipts] == [
        ("handoff", "acquired"),
        ("wake", "queued"),
        ("wake", "acquired"),
    ]
    assert len({item["lock_sha256"] for item in receipts}) == 1


@pytest.mark.parametrize(
    "change",
    [
        "none",
        "command_receipt_race",
        "parent",
        "attempt",
        "consumer",
        "project",
        "epoch",
        "terminal",
        "provider",
        "excluded",
        "external",
        "machine",
    ],
)
async def test_clear_successor_requires_canonical_lineage_and_same_physical_surface(
    tmp_path: Path, change: str
) -> None:
    module = importlib.import_module("tests.e2e.composer_proof_races")
    trace, event = registered_trace(tmp_path)
    old = trace.surfaces[str(event["terminal_id"])]
    new = replace(old, session_id=str(uuid4()))
    attempt = str(uuid4())
    marker: dict[str, Any] = {
        "attempt_id": attempt,
        "consumed_by": new.session_id,
        "command_sent_at": datetime.now(UTC).isoformat(),
        "handoff_record_id": str(uuid4()),
    }
    predecessor: dict[str, Any] = {
        "id": old.session_id,
        "source": old.provider,
        "project_id": old.project_id,
        "external_id": str(uuid4()),
        "machine_id": str(uuid4()),
    }
    successor: dict[str, Any] = {
        **predecessor,
        "id": new.session_id,
        "external_id": str(uuid4()),
        "parent_session_id": old.session_id,
    }
    scope = trace.scope
    if change == "command_receipt_race":
        marker.pop("command_sent_at")
    elif change == "parent":
        successor["parent_session_id"] = str(uuid4())
    elif change == "attempt":
        marker["attempt_id"] = str(uuid4())
    elif change == "consumer":
        marker["consumed_by"] = str(uuid4())
    elif change == "project":
        new = replace(new, project_id=str(uuid4()))
    elif change == "epoch":
        new = replace(new, host_epoch="another-epoch")
    elif change == "terminal":
        new = replace(new, terminal_id=str(uuid4()))
    elif change == "provider":
        new = replace(new, provider="codex")
    elif change == "excluded":
        scope = replace(scope, excluded=frozenset({new.session_id}))
    elif change == "external":
        successor["external_id"] = predecessor["external_id"]
    elif change == "machine":
        successor["machine_id"] = str(uuid4())
    if change in {"none", "command_receipt_race"}:
        module.require_clear_successor(scope, old, new, attempt, marker, predecessor, successor)
    else:
        with pytest.raises(ProofRefused):
            module.require_clear_successor(scope, old, new, attempt, marker, predecessor, successor)
