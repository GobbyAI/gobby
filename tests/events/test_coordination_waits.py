"""Storage, transport, and MCP contracts for durable coordination waits."""

from __future__ import annotations

import asyncio
import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import psycopg
import pytest

from gobby.events.completion_registry import CompletionEventRegistry
from gobby.events.coordination_waits import CoordinationWaitService
from gobby.events.wake import WakeDispatcher
from gobby.mcp_proxy.tools.agents_registry import create_agents_registry
from gobby.storage.coordination_waits import CoordinationWaitManager, coordination_wait_payload
from gobby.storage.hub.postgres import PostgresHubDatabase
from gobby.storage.inter_session_messages import InterSessionMessageManager
from gobby.storage.projects import PERSONAL_PROJECT_ID
from gobby.storage.sessions import SessionManager
from gobby.utils.machine_id import require_machine_id
from gobby.utils.session_context import session_context_for_test

pytestmark = pytest.mark.integration


@dataclass
class CoordinationHarness:
    db: PostgresHubDatabase
    manager: CoordinationWaitManager
    messages: InterSessionMessageManager
    owner: str
    waiter: str
    stranger: str

    def release(self, key: str = "restart-1", **kwargs: Any) -> str:
        return self.messages.create_message(
            from_session=kwargs.pop("from_session", self.owner),
            to_session=kwargs.pop("to_session", self.waiter),
            content="Released",
            message_type=kwargs.pop("message_type", "coordination_release"),
            metadata_json=json.dumps({"coordination_key": key}),
            **kwargs,
        ).id

    def reply(self, **kwargs: Any) -> str:
        return self.messages.create_message(
            from_session=kwargs.pop("from_session", self.owner),
            to_session=kwargs.pop("to_session", self.waiter),
            content="Use the staging DSN",
            **kwargs,
        ).id

    def wait(self, **kwargs: Any) -> dict[str, Any]:
        if "statuses" not in kwargs and "reply" not in kwargs:
            kwargs.setdefault("coordination_key", "restart-1")
        return self.manager.register(self.waiter, self.owner, **kwargs)

    def row(self, wait_id: str) -> dict[str, Any]:
        row = self.db.fetchone("SELECT * FROM coordination_waits WHERE id = %s", (wait_id,))
        assert row is not None
        return dict(row)

    def status(self, status: str) -> None:
        self.db.execute("UPDATE sessions SET status = %s WHERE id = %s", (status, self.owner))


def _install_live_wait_identity(db: PostgresHubDatabase) -> None:
    """Apply this branch's migration. Installed gdaemon does not embed it yet."""
    sql_path = (
        Path(__file__).resolve().parents[2]
        / "crates/gcore/assets/schema/migrations/446_coordination_wait_live_identity.sql"
    )
    statement: list[str] = []
    in_dollar = False
    for line in sql_path.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("--") and not in_dollar:
            continue
        if line.count("$$") % 2 == 1:
            in_dollar = not in_dollar
        statement.append(line)
        if not in_dollar and line.rstrip().endswith(";"):
            db.execute("\n".join(statement))
            statement = []


@pytest.fixture
def harness(postgres_db: PostgresHubDatabase) -> CoordinationHarness:
    _install_live_wait_identity(postgres_db)
    owner, waiter, stranger = (str(uuid.uuid4()) for _ in range(3))
    for session_id in (owner, waiter, stranger):
        postgres_db.execute(
            "INSERT INTO sessions (id, external_id, machine_id, source, project_id, status) "
            "VALUES (%s, %s, %s, 'codex', %s, 'active')",
            (session_id, session_id, require_machine_id(), PERSONAL_PROJECT_ID),
        )
    return CoordinationHarness(
        postgres_db,
        CoordinationWaitManager(postgres_db),
        InterSessionMessageManager(postgres_db),
        owner,
        waiter,
        stranger,
    )


def test_terminal_coordination_wait_can_be_rearmed(harness: CoordinationHarness) -> None:
    harness.release()
    resolved = harness.wait()
    assert resolved["outcome"] == "released"
    fresh = harness.wait()
    assert fresh["id"] != resolved["id"]
    payload = coordination_wait_payload(fresh)
    assert payload["wait_id"] == fresh["id"]
    assert payload["outcome"] == "waiting"
    assert payload["notification_registered"] is True
    repeated = harness.wait(timeout=3600)
    assert repeated["id"] == fresh["id"]
    assert repeated["expires_at"] == fresh["expires_at"]


def test_rearm_after_replied_timeout_and_cancelled(harness: CoordinationHarness) -> None:
    cases = (
        ("replied", {"reply": True}),
        ("timeout", {"coordination_key": "timeout-case"}),
        ("cancelled", {"statuses": ["completed"]}),
    )
    for outcome, kwargs in cases:
        original = harness.wait(**kwargs)
        harness.manager.db.execute(
            "UPDATE coordination_waits SET outcome = %s, completed_at = clock_timestamp() "
            "WHERE id = %s",
            (outcome, original["id"]),
        )
        fresh = harness.wait(**kwargs)
        assert fresh["id"] != original["id"]
        assert fresh["outcome"] == "waiting"
        assert coordination_wait_payload(fresh)["notification_registered"] is True
        repeated = harness.wait(**kwargs)
        assert repeated["id"] == fresh["id"]
        assert repeated["expires_at"] == fresh["expires_at"]


def test_early_release_and_repeated_registration(harness: CoordinationHarness) -> None:
    message_id = harness.release()
    row = harness.wait()
    repeated = harness.wait(timeout=3600)
    assert row["outcome"] == "released"
    assert row["message_id"] == message_id
    assert repeated["id"] != row["id"]
    assert repeated["outcome"] == "waiting"
    assert (row["expires_at"] - row["created_at"]).total_seconds() == pytest.approx(900, abs=1)
    assert (repeated["expires_at"] - repeated["created_at"]).total_seconds() == pytest.approx(
        3600, abs=1
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"coordination_key": "x", "statuses": ["paused"]},
        {"coordination_key": " "},
        {"statuses": []},
        {"statuses": ["bogus"]},
        {"coordination_key": "x", "timeout": 0},
        {"coordination_key": "x", "timeout": 3601},
        {"coordination_key": "x", "timeout": float("nan")},
        {"reply": False},
        {"reply": True, "coordination_key": "x"},
        {"reply": True, "statuses": ["paused"]},
        {"reply": True, "timeout": 3601},
    ],
)
def test_invalid_registration(harness: CoordinationHarness, kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        harness.manager.register(harness.waiter, harness.owner, **kwargs)
    count = harness.db.fetchone("SELECT count(*) AS n FROM coordination_waits")
    assert count is not None and count["n"] == 0


def test_release_matches_structured_identity_only(harness: CoordinationHarness) -> None:
    row = harness.wait()
    harness.release("wrong")
    harness.release(from_session=harness.stranger)
    harness.release(to_session=harness.stranger)
    harness.release(message_type="message")
    assert harness.row(row["id"])["outcome"] == "waiting"
    message_id = harness.release()
    completed = harness.row(row["id"])
    harness.release()
    assert completed["outcome"] == "released"
    assert completed["message_id"] == message_id
    assert harness.row(row["id"]) == completed


def test_reply_resolves_on_the_next_owner_message(harness: CoordinationHarness) -> None:
    row = harness.wait(reply=True)
    assert row["outcome"] == "waiting"
    message_id = harness.reply()
    completed = harness.row(row["id"])
    harness.reply()
    assert completed["outcome"] == "replied"
    assert completed["message_id"] == message_id
    assert completed["matched_status"] is None
    assert harness.row(row["id"]) == completed
    fresh = harness.wait(reply=True)
    assert fresh["id"] != completed["id"]
    assert fresh["outcome"] == "waiting"


def test_reply_ignores_messages_that_predate_registration(harness: CoordinationHarness) -> None:
    harness.reply()
    harness.release()
    row = harness.wait(reply=True)
    assert row["outcome"] == "waiting"
    assert harness.row(row["id"])["outcome"] == "waiting"


def test_reply_matches_the_owner_to_waiter_direction_only(harness: CoordinationHarness) -> None:
    row = harness.wait(reply=True)
    harness.reply(from_session=harness.stranger)
    harness.reply(to_session=harness.stranger)
    assert harness.row(row["id"])["outcome"] == "waiting"
    # Any durable message type from the owner answers the question, including
    # one shaped as a release for a key this wait never named.
    message_id = harness.release()
    completed = harness.row(row["id"])
    assert completed["outcome"] == "replied"
    assert completed["message_id"] == message_id


def test_reply_keeps_its_deadline_and_the_owner_ending(harness: CoordinationHarness) -> None:
    expired = harness.wait(reply=True)
    harness.db.execute(
        "UPDATE coordination_waits SET expires_at = clock_timestamp() - interval '1 second' "
        "WHERE id = %s",
        (expired["id"],),
    )
    harness.reply()
    assert harness.row(expired["id"])["outcome"] == "timeout"

    orphaned = harness.manager.register(harness.stranger, harness.owner, reply=True)
    harness.status("completed")
    assert harness.row(orphaned["id"])["outcome"] == "owner_ended"


def test_transient_status_and_idempotent_set(harness: CoordinationHarness) -> None:
    row = harness.wait(statuses=["paused", "awaiting_input", "paused"])
    repeated = harness.wait(statuses=["awaiting_input", "paused"], timeout=3600)
    assert row == repeated
    harness.status("paused")
    harness.status("active")
    completed = harness.row(row["id"])
    assert completed["outcome"] == "status_matched"
    assert completed["matched_status"] == "paused"


@pytest.mark.parametrize("status", ["completed", "cancelled", "closed", "expired", "deleted"])
def test_owner_ending_and_requested_terminal_status(
    harness: CoordinationHarness,
    status: str,
) -> None:
    release_wait = harness.wait()
    status_wait = harness.wait(statuses=[status])
    harness.status(status)
    assert harness.row(release_wait["id"])["outcome"] == "owner_ended"
    assert harness.row(status_wait["id"])["outcome"] == "status_matched"


def test_owner_deletion(harness: CoordinationHarness) -> None:
    row = harness.wait()
    harness.db.execute("DELETE FROM sessions WHERE id = %s", (harness.owner,))
    assert harness.row(row["id"])["outcome"] == "owner_ended"
    with pytest.raises(ValueError, match="does not exist"):
        harness.wait()


def test_cancel_is_owned_and_terminal(harness: CoordinationHarness) -> None:
    row = harness.wait()
    with pytest.raises(ValueError, match="does not belong"):
        harness.manager.cancel(row["id"], harness.stranger)
    cancelled = harness.manager.cancel(row["id"], harness.waiter)
    harness.release()
    assert cancelled["outcome"] == "cancelled"
    assert harness.manager.cancel(row["id"], harness.waiter) == cancelled


def test_expiry_uses_original_deadline(harness: CoordinationHarness) -> None:
    row = harness.wait(timeout=3600)
    harness.db.execute(
        "UPDATE coordination_waits SET expires_at = clock_timestamp() - interval '1 second' "
        "WHERE id = %s",
        (row["id"],),
    )
    expired = harness.wait(timeout=3600)
    assert expired["outcome"] == "timeout"
    assert expired["expires_at"] < row["expires_at"]
    harness.release()
    assert harness.row(row["id"]) == expired


@pytest.mark.parametrize("event", ["release", "status", "reply"])
def test_rolled_back_events_do_not_resolve(harness: CoordinationHarness, event: str) -> None:
    conditions: dict[str, dict[str, Any]] = {
        "release": {},
        "status": {"statuses": ["paused"]},
        "reply": {"reply": True},
    }
    row = harness.wait(**conditions[event])
    with pytest.raises(RuntimeError, match="rollback"):
        with harness.db.transaction():
            if event == "status":
                harness.status("paused")
            elif event == "reply":
                harness.reply()
            else:
                harness.release()
            raise RuntimeError("rollback")
    assert harness.row(row["id"])["outcome"] == "waiting"


@pytest.mark.parametrize("event", ["release", "status"])
def test_concurrent_registration_and_committed_event(
    harness: CoordinationHarness,
    event: str,
) -> None:
    barrier = threading.Barrier(2)

    def register() -> dict[str, Any]:
        barrier.wait(timeout=5)
        return harness.wait(statuses=["paused"]) if event == "status" else harness.wait()

    def publish() -> None:
        barrier.wait(timeout=5)
        harness.status("paused") if event == "status" else harness.release()

    with ThreadPoolExecutor(max_workers=2) as executor:
        registration = executor.submit(register)
        publication = executor.submit(publish)
        row = registration.result(timeout=10)
        publication.result(timeout=10)
    assert harness.row(row["id"])["outcome"] == (
        "status_matched" if event == "status" else "released"
    )


async def connection_for(harness: CoordinationHarness) -> psycopg.AsyncConnection[Any]:
    return await psycopg.AsyncConnection.connect(harness.db.conninfo, autocommit=True)


@pytest.mark.asyncio
async def test_listener_restart_and_duplicate_delivery(harness: CoordinationHarness) -> None:
    delivered = asyncio.Event()
    payloads: list[dict[str, Any]] = []

    async def wake(session: str, message: str, payload: dict[str, Any]) -> dict[str, bool]:
        assert session == harness.waiter
        payloads.append(payload)
        delivered.set()
        return {"ism_persisted": True}

    row = harness.wait()
    registry = CompletionEventRegistry(wake_callback=wake)
    service = CoordinationWaitService(
        harness.manager,
        registry,
        require_machine_id(),
        lambda: connection_for(harness),
    )
    await service.start()
    try:
        await asyncio.wait_for(service._queue.join(), 5)
        assert row["id"] in service._timers
        harness.release()
        await asyncio.wait_for(delivered.wait(), 5)
        await asyncio.wait_for(service._queue.join(), 5)
        assert payloads[0]["outcome"] == "released"
        assert payloads[0]["completion_id"] == row["id"]
    finally:
        await service.stop()

    # A committed terminal event while the daemon is down must be recovered.
    second = harness.wait(coordination_key="restart-2")
    harness.release("restart-2")
    recovered = CoordinationWaitService(
        harness.manager,
        registry,
        require_machine_id(),
        lambda: connection_for(harness),
    )
    await recovered.start()
    try:
        await asyncio.wait_for(recovered._queue.join(), 5)
        await recovered.process(second["id"])
        assert len(payloads) == 2
        assert harness.row(second["id"])["delivered_at"] is not None
    finally:
        await recovered.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status", ["interrupted", "awaiting_input", "awaiting_approval", "awaiting_handoff"]
)
async def test_protected_wakes_preserve_mailbox(
    harness: CoordinationHarness,
    status: str,
) -> None:
    harness.db.execute("UPDATE sessions SET status = %s WHERE id = %s", (status, harness.waiter))
    sender = AsyncMock()
    dispatcher = WakeDispatcher(
        session_manager=SessionManager(harness.db),
        ism_manager=harness.messages,
        tmux_sender=sender,
        tmux_pane_sender=sender,
        sdk_resumer=sender,
    )
    row = harness.wait()
    harness.release()
    service = CoordinationWaitService(
        harness.manager,
        CompletionEventRegistry(wake_callback=dispatcher.wake),
        require_machine_id(),
        lambda: connection_for(harness),
    )
    await service.process(row["id"])
    await service.process(row["id"])
    notifications = [
        message
        for message in harness.messages.get_messages(harness.waiter)
        if message.message_type == "completion_notification"
    ]
    assert len(notifications) == 1
    assert harness.row(row["id"])["delivered_at"] is not None
    sender.assert_not_called()


@pytest.mark.asyncio
async def test_mcp_registration_and_cancellation(harness: CoordinationHarness) -> None:
    registry = create_agents_registry(
        MagicMock(),
        session_manager=SessionManager(harness.db),
        db=harness.db,
        completion_registry=CompletionEventRegistry(),
    )
    with session_context_for_test(harness.waiter):
        row = await registry.call(
            "wait_for_coordination",
            {
                "owner_session": harness.owner,
                "coordination_key": "restart-1",
            },
        )
        assert row["outcome"] == "waiting"
        cancelled = await registry.call("cancel_coordination_wait", {"wait_id": row["wait_id"]})
        assert cancelled["outcome"] == "cancelled"


@pytest.mark.asyncio
async def test_mcp_reply_wait_defaults_to_the_spawning_parent(
    harness: CoordinationHarness,
) -> None:
    registry = create_agents_registry(
        MagicMock(),
        session_manager=SessionManager(harness.db),
        db=harness.db,
        completion_registry=CompletionEventRegistry(),
    )
    with session_context_for_test(harness.waiter):
        orphan = await registry.call("wait_for_coordination", {"reply": True})
        assert orphan["success"] is False
        assert "parent" in orphan["error"]

        harness.db.execute(
            "UPDATE sessions SET parent_session_id = %s WHERE id = %s",
            (harness.owner, harness.waiter),
        )
        row = await registry.call("wait_for_coordination", {"reply": True})
        assert row["outcome"] == "waiting"
        assert row["owner_session_id"] == harness.owner
        harness.reply()
        assert harness.row(row["wait_id"])["outcome"] == "replied"

    with session_context_for_test(harness.stranger):
        named = await registry.call(
            "wait_for_coordination", {"owner_session": harness.owner, "reply": True}
        )
        assert named["outcome"] == "waiting"
        assert named["owner_session_id"] == harness.owner


@pytest.mark.asyncio
async def test_restart_rehydrates_active_wait_and_deadline(harness: CoordinationHarness) -> None:
    row = harness.wait()
    wake = AsyncMock(return_value={"ism_persisted": True})
    service = CoordinationWaitService(
        harness.manager,
        CompletionEventRegistry(wake_callback=wake),
        require_machine_id(),
        lambda: connection_for(harness),
    )
    await service.start()
    try:
        await asyncio.wait_for(service._queue.join(), 5)
        assert row["id"] in service._timers
    finally:
        await service.stop()
    restarted = CoordinationWaitService(
        CoordinationWaitManager(harness.db),
        CompletionEventRegistry(wake_callback=wake),
        require_machine_id(),
        lambda: connection_for(harness),
    )
    await restarted.start()
    try:
        await asyncio.wait_for(restarted._queue.join(), 5)
        assert row["id"] in restarted._timers
        # Advance durable time and fire the registered deadline callback directly.
        harness.db.execute(
            "UPDATE coordination_waits SET expires_at = clock_timestamp() - interval '1 second' "
            "WHERE id = %s",
            (row["id"],),
        )
        restarted._queue.put_nowait(row["id"])
        await asyncio.wait_for(restarted._queue.join(), 5)
        assert harness.row(row["id"])["outcome"] == "timeout"
        assert row["id"] not in restarted._timers
        wake.assert_awaited_once()
        assert wake.call_args.args[2]["outcome"] == "timeout"
    finally:
        await restarted.stop()


@pytest.mark.asyncio
async def test_failed_delivery_retains_outcome_for_recovery(harness: CoordinationHarness) -> None:
    row = harness.wait()
    harness.release()
    wake = AsyncMock(side_effect=[{"ism_persisted": False}, {"ism_persisted": True}])
    service = CoordinationWaitService(
        harness.manager,
        CompletionEventRegistry(wake_callback=wake),
        require_machine_id(),
        lambda: connection_for(harness),
    )
    try:
        await service.process(row["id"])
        assert harness.row(row["id"])["delivered_at"] is None
        assert row["id"] in service._timers
    finally:
        await service.stop()
    restarted = CoordinationWaitService(
        harness.manager,
        CompletionEventRegistry(wake_callback=wake),
        require_machine_id(),
        lambda: connection_for(harness),
    )
    await restarted.start()
    try:
        await asyncio.wait_for(restarted._queue.join(), 5)
        assert harness.row(row["id"])["delivered_at"] is not None
        assert wake.await_count == 2
        assert wake.call_args.args[2]["outcome"] == "released"
    finally:
        await restarted.stop()


def test_concurrent_replays_share_one_subscription(harness: CoordinationHarness) -> None:
    barrier = threading.Barrier(2)

    def register(timeout: int) -> dict[str, Any]:
        barrier.wait(timeout=5)
        return harness.wait(timeout=timeout)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(register, 900)
        second = executor.submit(register, 3600)
        assert first.result(timeout=10) == second.result(timeout=10)
    count = harness.db.fetchone("SELECT count(*) AS n FROM coordination_waits")
    assert count is not None and count["n"] == 1


@pytest.mark.asyncio
async def test_runtime_role_can_register_resolve_and_listen(harness: CoordinationHarness) -> None:
    runtime = PostgresHubDatabase(harness.db.conninfo, runtime_role="gobby_daemon_runtime")
    wake = AsyncMock(return_value={"ism_persisted": True})
    service = CoordinationWaitService(
        CoordinationWaitManager(runtime),
        CompletionEventRegistry(wake_callback=wake),
        require_machine_id(),
        runtime.open_runtime_async_connection,
    )
    try:
        row = service.manager.register(harness.waiter, harness.owner, statuses=["paused"])
        await service.start()
        await asyncio.wait_for(service._queue.join(), 5)
        runtime.execute("UPDATE sessions SET status = 'paused' WHERE id = %s", (harness.owner,))
        # Reconcile explicitly, then wait for the real notification consumer's
        # acknowledgement through its queue rather than polling database state.
        await service.recover()
        await asyncio.wait_for(service._queue.join(), 5)
        assert harness.row(row["id"])["outcome"] == "status_matched"
        assert harness.row(row["id"])["delivered_at"] is not None
        wake.assert_awaited_once()
    finally:
        await service.stop()
        runtime.close()
