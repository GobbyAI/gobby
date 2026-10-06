"""At most one unread live wake per session until the mailbox is read (#23125).

A delivered wake stays outstanding until a mailbox read after its write completes.
Later messages queue durably without another live wake. The recovery timeout
starts at completion and permits another wake only if mail remains unread.
The outstanding wake is stored with the session, so a fresh dispatcher keeps it.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any, cast

import pytest

import gobby.events.wake as wake_module
import gobby.events.wake_batch as wake_batch_module
import gobby.runner_init.orchestration as orchestration_module
import gobby.storage.inter_session_messages as messages_module
from gobby.agents.idle_detector import ComposerRead
from gobby.events.live_wake import TerminalActivity
from gobby.events.wake import LIVE_WAKE_FRESH_SECONDS, NativeWakeTarget, WakeDispatcher
from gobby.events.wake_recovery import WakeReplayCoordinator
from gobby.mcp_proxy.tools.agent_messaging import add_messaging_tools
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.sessions.mailbox import MailboxService
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.inter_session_messages import InterSessionMessageManager
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager
from gobby.terminals.host_client import HostBatchTarget
from gobby.terminals.leases import TerminalLeaseRegistry
from gobby.terminals.native_runtime import NativeTerminalRuntime
from gobby.terminals.write_coordinator import UnresolvedWriteStore, WriteCoordinator
from gobby.utils.datetime import utc_now
from tests.terminals.fakes import MemoryTerminalStore, make_memory_terminal, runtime_registry
from tests.terminals.test_native_runtime import FakeHostClient

pytestmark = pytest.mark.unit


class RecordingSender:
    """Terminal wake sender that records submissions and can fail once."""

    def __init__(self, *, fail_first: bool = False) -> None:
        self.submitted: list[str] = []
        self._fail_next = fail_first

    async def __call__(
        self,
        identity: str,
        message: str,
        *,
        submit: bool = False,
        clear_before_submit: bool = False,
        composer_confirmed_empty: bool = False,
        cli_source: str | None = None,
    ) -> datetime | None:
        await asyncio.sleep(0)
        if self._fail_next:
            self._fail_next = False
            raise RuntimeError("terminal write failed")
        self.submitted.append(identity)
        return None


def _register(session_manager: SessionManager, project_id: str, external_id: str) -> Session:
    return session_manager.register(
        external_id=external_id,
        machine_id=None,
        source="codex",
        project_id=project_id,
        title=external_id,
    )


class Harness:
    def __init__(
        self, temp_db: HubDatabase, session_manager: SessionManager, project_id: str
    ) -> None:
        self.db = temp_db
        self.session_manager = session_manager
        self.messages = InterSessionMessageManager(temp_db)
        self.sender_session = _register(session_manager, project_id, "sender")
        self.recipient = _register(session_manager, project_id, "recipient").id
        self.terminals = MemoryTerminalStore()
        terminal = replace(
            make_memory_terminal(backend="native"),
            session_id=self.recipient,
            project_id=project_id,
        )
        self.terminals.rows[terminal.id] = terminal
        self.terminal_id = terminal.id
        session_manager.update(
            self.recipient, status="paused", terminal_context={"gobby_terminal_id": terminal.id}
        )

    def dispatcher(self, sender: RecordingSender) -> WakeDispatcher:
        return WakeDispatcher(
            session_manager=self.session_manager,
            ism_manager=self.messages,
            tmux_sender=sender,
            terminal_manager=self.terminals,
        )

    async def send(self, dispatcher: WakeDispatcher, content: str) -> dict[str, Any]:
        result = await MailboxService(
            db=self.db,
            message_manager=self.messages,
            session_manager=self.session_manager,
            wake_dispatcher=dispatcher,
        ).send(
            from_session_id=self.sender_session.id,
            target="session",
            target_id=self.recipient,
            content=content,
            wake=True,
        )
        assert result.success is True
        [wake] = result.wake_results
        return wake

    def read_mailbox(self) -> None:
        pending = self.messages.get_undelivered_messages(self.recipient)
        self.messages.mark_delivered_batch([m.id for m in pending], self.recipient)

    async def public_send(
        self, dispatcher: WakeDispatcher, content: str, wake: bool | None
    ) -> dict[str, Any]:
        registry = InternalToolRegistry(name="gobby-agents", description="isolated messaging")
        add_messaging_tools(
            registry, self.messages, self.session_manager, self.db, wake_dispatcher=dispatcher
        )
        arguments: dict[str, Any] = {
            "from_session": self.sender_session.id,
            "target": "session",
            "target_id": self.recipient,
            "content": content,
            "brief": False,
        }
        if wake is not None:
            arguments["wake"] = wake
        result = await registry.call("send_message", arguments)
        assert result["success"] is True
        [outcome] = result["wake_results"]
        assert isinstance(outcome, dict)
        return outcome


@pytest.fixture
def harness(
    temp_db: HubDatabase, session_manager: SessionManager, sample_project: dict[str, Any]
) -> Harness:
    return Harness(temp_db, session_manager, sample_project["id"])


@pytest.mark.asyncio
async def test_messages_before_read_send_one_wake(harness: Harness) -> None:
    sender = RecordingSender()
    dispatcher = harness.dispatcher(sender)

    first = await harness.send(dispatcher, "one")
    second = await harness.send(dispatcher, "two")
    third = await harness.send(dispatcher, "three")

    assert first["delivered"] is True
    assert second["skipped"] == "debounced"
    assert third["skipped"] == "debounced"
    assert sender.submitted == [harness.terminal_id]
    assert len(harness.messages.get_undelivered_messages(harness.recipient)) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("wake", [None, True], ids=["default-wake", "explicit-wake"])
async def test_withheld_retry_does_not_repeat_a_later_successful_send(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, wake: bool | None
) -> None:
    sender = RecordingSender()
    retry_ready = asyncio.Event()
    release_retry = asyncio.Event()
    occupied = True

    async def probe(_session: object, _terminal: object | None) -> TerminalActivity:
        return TerminalActivity(ComposerRead("draft" if occupied else "empty", ""))

    async def wait_for_release(_delay: float) -> None:
        retry_ready.set()
        await release_retry.wait()

    dispatcher = WakeDispatcher(
        session_manager=harness.session_manager,
        ism_manager=harness.messages,
        tmux_sender=sender,
        terminal_manager=harness.terminals,
        activity_probe=probe,
    )
    monkeypatch.setattr(dispatcher, "_composer_retry_wait", wait_for_release)
    first = await harness.public_send(dispatcher, "one", wake)
    assert first["skipped"] == "composer_occupied"
    retry = dispatcher._composer_retries[harness.recipient]
    try:
        await asyncio.wait_for(retry_ready.wait(), timeout=5)
        occupied = False
        second = await harness.public_send(dispatcher, "two", wake)
        third = await harness.public_send(dispatcher, "three", wake)
        assert second["delivered"] is True
        assert third["skipped"] == "debounced"
        release_retry.set()
        await asyncio.wait_for(retry, timeout=5)
        assert sender.submitted == [harness.terminal_id]
        assert len(harness.messages.get_undelivered_messages(harness.recipient)) == 3
    finally:
        retry.cancel()
        await asyncio.gather(retry, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("wake", [None, True], ids=["default-wake", "explicit-wake"])
async def test_deferred_wake_does_not_replay_messages_read_while_active(
    harness: Harness, wake: bool | None
) -> None:
    sender = RecordingSender()
    refresh_ready = asyncio.Event()
    release_refresh = asyncio.Event()

    async def refresh(session_id: str) -> None:
        refresh_ready.set()
        await release_refresh.wait()
        harness.session_manager.update(session_id, status="paused")

    dispatcher = WakeDispatcher(
        session_manager=harness.session_manager,
        ism_manager=harness.messages,
        tmux_sender=sender,
        terminal_manager=harness.terminals,
        lifecycle_refresh=refresh,
    )
    harness.session_manager.update(harness.recipient, status="active")
    first = await harness.public_send(dispatcher, "already read on next hook", wake)
    assert first["skipped"] == "session_active"
    deferred = dispatcher._deferred_refreshes[harness.recipient]
    try:
        await asyncio.wait_for(refresh_ready.wait(), timeout=5)
        harness.read_mailbox()
        release_refresh.set()
        await asyncio.wait_for(deferred, timeout=5)
        assert sender.submitted == []
        assert harness.messages.get_undelivered_messages(harness.recipient) == []
        next_wake = await harness.public_send(dispatcher, "new unread message", wake)
        assert next_wake["delivered"] is True
        assert sender.submitted == [harness.terminal_id]
    finally:
        deferred.cancel()
        await asyncio.gather(deferred, return_exceptions=True)


@pytest.mark.asyncio
async def test_reading_the_mailbox_allows_the_next_wake(harness: Harness) -> None:
    sender = RecordingSender()
    dispatcher = harness.dispatcher(sender)

    await harness.send(dispatcher, "before read")
    harness.read_mailbox()
    after_read = await harness.send(dispatcher, "after read")
    held = await harness.send(dispatcher, "still unread")

    assert after_read["delivered"] is True
    assert held["skipped"] == "debounced"
    assert sender.submitted == [harness.terminal_id, harness.terminal_id]


@pytest.mark.asyncio
@pytest.mark.parametrize("wake", [None, True], ids=["default-wake", "explicit-wake"])
async def test_concurrent_active_sends_defer_one_wake_until_idle(
    harness: Harness, wake: bool | None
) -> None:
    sender = RecordingSender()
    release_refresh = asyncio.Event()

    async def refresh(session_id: str) -> None:
        await release_refresh.wait()
        harness.session_manager.update(session_id, status="paused")

    dispatcher = WakeDispatcher(
        session_manager=harness.session_manager,
        ism_manager=harness.messages,
        tmux_sender=sender,
        terminal_manager=harness.terminals,
        lifecycle_refresh=refresh,
    )
    harness.session_manager.update(harness.recipient, status="active")
    outcomes = await asyncio.gather(
        *(harness.public_send(dispatcher, content, wake) for content in ("one", "two", "three"))
    )
    assert all(outcome["skipped"] == "session_active" for outcome in outcomes)
    deferred = dispatcher._deferred_refreshes[harness.recipient]
    try:
        release_refresh.set()
        await asyncio.wait_for(deferred, timeout=5)
        assert sender.submitted == [harness.terminal_id]
        assert len(harness.messages.get_undelivered_messages(harness.recipient)) == 3
        before_read = await harness.public_send(dispatcher, "still unread", wake)
        assert before_read["skipped"] == "debounced"
        assert sender.submitted == [harness.terminal_id]
        harness.read_mailbox()
        after_read = await harness.public_send(dispatcher, "after read", wake)
        assert after_read["delivered"] is True
        assert sender.submitted == [harness.terminal_id, harness.terminal_id]
    finally:
        deferred.cancel()
        await asyncio.gather(deferred, return_exceptions=True)


@pytest.mark.asyncio
async def test_concurrent_deliveries_send_one_wake(harness: Harness) -> None:
    sender = RecordingSender()
    dispatcher = harness.dispatcher(sender)

    results = await asyncio.gather(
        harness.send(dispatcher, "a"),
        harness.send(dispatcher, "b"),
        harness.send(dispatcher, "c"),
    )

    assert sorted(bool(result["delivered"]) for result in results) == [False, False, True]
    assert sender.submitted == [harness.terminal_id]


@pytest.mark.asyncio
async def test_failed_wake_does_not_suppress_the_next(harness: Harness) -> None:
    sender = RecordingSender(fail_first=True)
    dispatcher = harness.dispatcher(sender)

    failed = await harness.send(dispatcher, "lost")
    retried = await harness.send(dispatcher, "retry")

    assert failed["delivered"] is False
    assert retried["delivered"] is True
    assert sender.submitted == [harness.terminal_id]


@pytest.mark.asyncio
async def test_pending_wake_survives_a_dispatcher_restart(harness: Harness) -> None:
    sender = RecordingSender()
    await harness.send(harness.dispatcher(sender), "before restart")

    restarted = harness.dispatcher(sender)
    held = await harness.send(restarted, "after restart")
    harness.read_mailbox()
    woken = await harness.send(restarted, "after read")

    assert held["skipped"] == "debounced"
    assert woken["delivered"] is True
    assert sender.submitted == [harness.terminal_id, harness.terminal_id]


@pytest.mark.asyncio
async def test_partial_read_of_a_deferred_backlog_consumes_the_wake(harness: Harness) -> None:
    """A rendering budget may defer older rows; reading any one consumes the wake."""
    sender = RecordingSender()
    dispatcher = harness.dispatcher(sender)
    for content in ("one", "two", "three"):
        await harness.send(dispatcher, content)
    first, *_deferred = harness.messages.get_undelivered_messages(harness.recipient)
    harness.messages.mark_delivered(first.id, harness.recipient)

    woken = await harness.send(dispatcher, "after partial read")

    assert woken["delivered"] is True
    assert sender.submitted == [harness.terminal_id, harness.terminal_id]


@pytest.mark.asyncio
async def test_lost_wake_expires_and_the_next_message_wakes(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wake reported delivered but never read stops suppressing once stale."""
    sender = RecordingSender()
    dispatcher = harness.dispatcher(sender)
    await harness.send(dispatcher, "lost")
    later = utc_now() + timedelta(seconds=LIVE_WAKE_FRESH_SECONDS)
    monkeypatch.setattr("gobby.events.wake.utc_now", lambda: later)

    woken = await harness.send(dispatcher, "after expiry")

    assert woken["delivered"] is True
    assert sender.submitted == [harness.terminal_id, harness.terminal_id]


@pytest.mark.asyncio
async def test_urgent_wake_is_suppressed_while_a_fresh_wake_is_outstanding(
    harness: Harness,
) -> None:
    sender = RecordingSender()
    dispatcher = harness.dispatcher(sender)
    await harness.send(dispatcher, "first")

    urgent = await dispatcher.dispatch_live_wake(harness.recipient, priority="urgent")

    assert urgent["skipped"] == "debounced"
    assert sender.submitted == [harness.terminal_id]


@pytest.mark.asyncio
async def test_abandoned_write_allows_the_next_public_send(harness: Harness) -> None:
    abandon = True

    class AbandonedSender(RecordingSender):
        async def __call__(self, identity: str, message: str, **kwargs: Any) -> None:
            nonlocal abandon
            if abandon:
                abandon = False
                raise asyncio.CancelledError
            await super().__call__(identity, message, **kwargs)

    sender = AbandonedSender()
    dispatcher = harness.dispatcher(sender)
    with pytest.raises(asyncio.CancelledError):
        await harness.public_send(dispatcher, "abandoned", True)
    assert sender.submitted == []
    assert (await harness.public_send(dispatcher, "next", True))["delivered"] is True
    assert sender.submitted == [harness.terminal_id]
    assert len(harness.messages.get_undelivered_messages(harness.recipient)) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("wake", [None, True], ids=["default-wake", "explicit-wake"])
@pytest.mark.parametrize("batch", [False, True], ids=["single-terminal", "native-batch"])
async def test_read_before_enter_does_not_consume_the_dispatched_wake(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, wake: bool | None, batch: bool
) -> None:
    clock = [utc_now()]
    monkeypatch.setattr(wake_module, "utc_now", lambda: clock[0])
    monkeypatch.setattr(wake_batch_module, "utc_now", lambda: clock[0])
    monkeypatch.setattr(messages_module, "utc_now", lambda: clock[0])
    started = asyncio.Event()
    release = asyncio.Event()

    class HeldSender(RecordingSender):
        async def __call__(self, identity: str, message: str, **kwargs: Any) -> datetime:
            started.set()
            await release.wait()
            await super().__call__(identity, message, **kwargs)
            return clock[0]

    sender = HeldSender()
    dispatcher = harness.dispatcher(sender)

    async def native_send(targets: list[NativeWakeTarget]) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for target in targets:
            submitted_at = await sender(target.terminal_id, "[Gobby] Check messages", submit=True)
            results.append(
                {
                    "session_id": target.session_id,
                    "delivered": True,
                    "method": "terminal",
                    "submit_dispatched_at": submitted_at.isoformat(),
                }
            )
        return results

    async def first_send() -> dict[str, Any]:
        if not batch:
            return await harness.public_send(dispatcher, "one", wake)
        dispatcher._native_batch_sender = native_send
        harness.messages.create_message(harness.sender_session.id, harness.recipient, "one")
        [outcome] = await dispatcher.dispatch_live_wakes([harness.recipient])
        return outcome

    send = asyncio.create_task(first_send())
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        clock[0] += timedelta(seconds=1)
        harness.read_mailbox()
        # A slow prelude must start its recovery window only at Enter dispatch.
        clock[0] += timedelta(seconds=LIVE_WAKE_FRESH_SECONDS + 1)
        release.set()
        assert (await asyncio.wait_for(send, timeout=5))["delivered"] is True
        second = await harness.public_send(dispatcher, "two", wake)
        third = await harness.public_send(dispatcher, "three", wake)
        assert second["skipped"] == third["skipped"] == "debounced"
        assert sender.submitted == [harness.terminal_id]
        clock[0] += timedelta(seconds=1)
        harness.read_mailbox()
        assert (await harness.public_send(dispatcher, "after submit read", wake))["delivered"]
        assert sender.submitted == [harness.terminal_id, harness.terminal_id]
    finally:
        send.cancel()
        await asyncio.gather(send, return_exceptions=True)


@pytest.mark.asyncio
async def test_expiry_without_unread_mail_does_not_replay_a_wake(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    sender = RecordingSender()
    dispatcher = harness.dispatcher(sender)
    assert (await harness.send(dispatcher, "one"))["delivered"] is True
    clock = utc_now() + timedelta(seconds=LIVE_WAKE_FRESH_SECONDS + 1)
    monkeypatch.setattr(wake_module, "utc_now", lambda: clock)
    harness.read_mailbox()
    result = await dispatcher.dispatch_live_wake(harness.recipient)
    assert result["skipped"] == "debounced"
    assert sender.submitted == [harness.terminal_id]
    assert (await harness.send(dispatcher, "new unread mail"))["delivered"] is True
    assert sender.submitted == [harness.terminal_id, harness.terminal_id]


@pytest.mark.asyncio
@pytest.mark.parametrize("wake", [None, True], ids=["default-wake", "explicit-wake"])
@pytest.mark.parametrize("batch", [False, True], ids=["single-terminal", "native-batch"])
async def test_submit_hook_read_before_sender_settles_does_not_strand_paused_mail(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, wake: bool | None, batch: bool
) -> None:
    clock = [utc_now()]
    monkeypatch.setattr(wake_module, "utc_now", lambda: clock[0])
    monkeypatch.setattr(wake_batch_module, "utc_now", lambda: clock[0])
    monkeypatch.setattr(messages_module, "utc_now", lambda: clock[0])

    class SubmittedSender(RecordingSender):
        async def __call__(self, identity: str, message: str, **kwargs: Any) -> datetime:
            dispatched_at = clock[0]
            await super().__call__(identity, message, **kwargs)
            # Enter is already on the PTY. Its hook acknowledges before the
            # sender's latch settlement/host acknowledgement returns to wake.py.
            clock[0] += timedelta(milliseconds=5)
            harness.read_mailbox()
            clock[0] += timedelta(milliseconds=20)
            return dispatched_at

    sender = SubmittedSender()
    dispatcher = harness.dispatcher(sender)

    async def native_send(targets: list[NativeWakeTarget]) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for target in targets:
            dispatched_at = await sender(target.terminal_id, "[Gobby] Check messages", submit=True)
            results.append(
                {
                    "session_id": target.session_id,
                    "delivered": True,
                    "method": "terminal",
                    "submit_dispatched_at": dispatched_at.isoformat(),
                }
            )
        return results

    if batch:
        dispatcher._native_batch_sender = native_send
        harness.messages.create_message(
            harness.sender_session.id,
            harness.recipient,
            "one",
            metadata_json='{"wake_requested":true}',
        )
        [first] = await dispatcher.dispatch_live_wakes([harness.recipient])
    else:
        first = await harness.public_send(dispatcher, "one", wake)
    assert first["delivered"] is True
    clock[0] += timedelta(seconds=5)
    second = await harness.public_send(dispatcher, "after prompt read", wake)

    async def run_db(operation: Any, *args: Any) -> Any:
        return operation(*args)

    replay = WakeReplayCoordinator(
        message_manager=harness.messages,
        session_manager=harness.session_manager,
        dispatcher=dispatcher,
        run_db=run_db,
    )
    await replay.request_replay(harness.recipient)
    assert second["delivered"] is True
    assert sender.submitted == [harness.terminal_id, harness.terminal_id]
    assert harness.messages.get_undelivered_messages(harness.recipient) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("batch", [False, True], ids=["sequence", "native-batch"])
@pytest.mark.parametrize(
    "read_after_enter", [False, True], ids=["pre-enter-read", "submit-hook-read"]
)
async def test_real_wake_adapter_orders_mailbox_read_at_enter_dispatch(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, batch: bool, read_after_enter: bool
) -> None:
    clock = [utc_now()]
    for module in (wake_module, wake_batch_module, messages_module, orchestration_module):
        monkeypatch.setattr(module, "utc_now", lambda: clock[0])
    host = FakeHostClient()
    row = harness.terminals.rows[harness.terminal_id]
    row.host_epoch = host.host_epoch
    row.locator = {"host_terminal_id": "ht-1"}
    runtime = NativeTerminalRuntime(host)
    coordinator = WriteCoordinator(
        cast(UnresolvedWriteStore, harness.terminals),
        runtime_registry(runtime),
        lease_registry=TerminalLeaseRegistry(daemon_epoch="test-epoch"),
    )
    monkeypatch.setattr(
        orchestration_module, "wake_write_services", lambda: (harness.terminals, coordinator)
    )
    submits: list[datetime] = []

    async def pace(seconds: float) -> None:
        # The old turn may read while text is paced before the new Enter.
        if seconds and not read_after_enter:
            clock[0] += timedelta(milliseconds=5)
            harness.read_mailbox()
        clock[0] += timedelta(seconds=seconds)

    monkeypatch.setattr("gobby.terminals.write_coordinator.asyncio.sleep", pace)

    def on_enter() -> None:
        submits.append(clock[0])
        if read_after_enter:
            clock[0] += timedelta(milliseconds=5)
            harness.read_mailbox()
        clock[0] += timedelta(milliseconds=20)

    async def write(**kwargs: Any) -> dict[str, Any]:
        result = await FakeHostClient.write(host, **kwargs)
        if kwargs.get("kind") == "key" and kwargs.get("data") == b"\r":
            on_enter()
        return result

    async def write_batch(targets: list[HostBatchTarget]) -> list[dict[str, Any]]:
        result = await FakeHostClient.write_batch(host, targets)
        if any(operation.data == b"\r" for target in targets for operation in target.operations):
            on_enter()
        return result

    monkeypatch.setattr(host, "write", write)
    monkeypatch.setattr(host, "write_batch", write_batch)
    dispatcher = WakeDispatcher(
        session_manager=harness.session_manager,
        ism_manager=harness.messages,
        tmux_sender=orchestration_module._send_tmux_session_wake,
        terminal_manager=harness.terminals,
        native_batch_sender=orchestration_module._send_native_wake_batch if batch else None,
    )
    harness.messages.create_message(
        harness.sender_session.id, harness.recipient, "one", metadata_json='{"wake_requested":true}'
    )
    [first] = await dispatcher.dispatch_live_wakes([harness.recipient])
    assert first["delivered"] is True
    assert len(submits) == 1
    clock[0] += timedelta(seconds=5)
    second = await harness.public_send(dispatcher, "two", None)

    async def run_db(operation: Any, *args: Any) -> Any:
        return operation(*args)

    replay = WakeReplayCoordinator(
        message_manager=harness.messages,
        session_manager=harness.session_manager,
        dispatcher=dispatcher,
        run_db=run_db,
    )
    await replay.request_replay(harness.recipient)
    if read_after_enter:
        assert second["delivered"] is True
        assert len(submits) == 2
        assert harness.messages.get_undelivered_messages(harness.recipient) == []
    else:
        third = await harness.public_send(dispatcher, "three", None)
        assert second["skipped"] == third["skipped"] == "debounced"
        assert len(submits) == 1
        clock[0] += timedelta(milliseconds=5)
        harness.read_mailbox()
        assert (await harness.public_send(dispatcher, "after submit read", None))["delivered"]
        assert len(submits) == 2
    assert row.unresolved_writes == {}
