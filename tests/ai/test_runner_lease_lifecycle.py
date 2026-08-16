"""Serving-lease deadline, renew-split, and re-acquisition lifecycle tests."""

from __future__ import annotations

import asyncio
import logging
import threading
from contextlib import suppress
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

import gobby.runner_init.embedding_lease as embedding_lease
import gobby.runner_init.services as services
from gobby.ai.embedding_switch import CompletedSwitchRecord
from gobby.config.embedding_keys import EMBEDDING_SWITCH_COMPLETED_KEY
from gobby.storage.embedding_generation_state import (
    EmbeddingGenerationLeaseExpired,
    EmbeddingGenerationLeaseLost,
    EmbeddingGenerationLeaseRenewTransient,
    EmbeddingGenerationState,
    EmbeddingServingLease,
)

pytestmark = pytest.mark.unit


class _StubCursor:
    def __init__(self, rowcount: int) -> None:
        self.rowcount = rowcount


class _StubHubDatabase:
    """Minimal HubDatabase stand-in for the ack INSERT and renew UPDATE."""

    def __init__(self) -> None:
        self.execute_calls: list[str] = []
        self.ack_inserted = threading.Event()
        self.renewed = threading.Event()
        self.renew_rowcount = 1
        self.fail_execute: Exception | None = None
        self.fail_renew_times = 0

    def execute(self, sql: str, params: tuple[object, ...] = ()) -> _StubCursor:
        if self.fail_execute is not None:
            raise self.fail_execute
        is_renew = "UPDATE embedding_generation_acks" in sql
        if self.fail_renew_times and is_renew:
            self.fail_renew_times -= 1
            raise ConnectionError("transient database failure")
        self.execute_calls.append(sql)
        if is_renew:
            self.renewed.set()
        if "INSERT INTO embedding_generation_acks" in sql:
            self.ack_inserted.set()
        return _StubCursor(self.renew_rowcount if is_renew else 1)

    def ack_insert_count(self) -> int:
        return sum(
            1 for sql in self.execute_calls if "INSERT INTO embedding_generation_acks" in sql
        )


class _FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def monotonic(self) -> float:
        return self.now


def _lease(
    state: EmbeddingGenerationState,
    *,
    generation: str = "run-1",
    revision: int = 7,
    lease_seconds: float = 30.0,
    watermark: int = 3,
) -> EmbeddingServingLease:
    return state.prepare_serving_lease(
        uuid4(),
        generation,
        revision,
        lease_seconds=lease_seconds,
        caught_up_watermark=watermark,
        required_watermark=watermark,
    )


def _completed_record(
    *,
    run_id: str = "run-1",
    committed_revision: int = 7,
    caught_up_watermark: int = 3,
) -> CompletedSwitchRecord:
    return CompletedSwitchRecord(
        run_id=run_id,
        committed_revision=committed_revision,
        physical_names={"memories": f"memories@{run_id}"},
        old_physical_names={},
        caught_up_watermark=caught_up_watermark,
        catalog_key="catalog",
        target_dim=4,
        target_model="model",
        target_query_prefix=None,
        target_api_base=None,
    )


def test_activate_deadline_from_pre_write_monotonic(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _StubHubDatabase()
    state = EmbeddingGenerationState(cast(Any, db))
    clock = _FakeClock()
    monkeypatch.setattr("gobby.storage.embedding_generation_state.time.monotonic", clock.monotonic)
    lease = _lease(state, lease_seconds=30.0)
    original_acknowledge = state.acknowledge

    def slow_acknowledge(*args: Any, **kwargs: Any) -> bool:
        clock.now += 5.0
        return original_acknowledge(*args, **kwargs)

    monkeypatch.setattr(state, "acknowledge", slow_acknowledge)

    lease.activate()

    # deadline = t0 (captured before the write) + 30 TTL - 3 margin; the 5s of
    # write latency already elapsed, so 22s remain — not 27.
    assert lease.remaining_seconds() == pytest.approx(22.0)


def test_renew_deadline_from_pre_write_monotonic(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _StubHubDatabase()
    state = EmbeddingGenerationState(cast(Any, db))
    clock = _FakeClock()
    monkeypatch.setattr("gobby.storage.embedding_generation_state.time.monotonic", clock.monotonic)
    lease = _lease(state, lease_seconds=30.0)
    lease.activate()

    def slow_renew(*args: Any, **kwargs: Any) -> None:
        clock.now += 4.0

    monkeypatch.setattr(state, "renew", slow_renew)

    lease.renew()

    assert lease.remaining_seconds() == pytest.approx(23.0)


def test_renew_rowcount_mismatch_fences_immediately() -> None:
    db = _StubHubDatabase()
    state = EmbeddingGenerationState(cast(Any, db))
    lease = _lease(state)
    lease.activate()
    db.renew_rowcount = 0

    with pytest.raises(EmbeddingGenerationLeaseLost, match="no longer matches"):
        lease.renew()
    with pytest.raises(EmbeddingGenerationLeaseLost, match="fenced"):
        lease.assert_serving()


def test_renew_connection_error_is_transient_without_fencing() -> None:
    db = _StubHubDatabase()
    state = EmbeddingGenerationState(cast(Any, db))
    lease = _lease(state)
    lease.activate()
    db.fail_execute = ConnectionError("socket closed")

    with pytest.raises(EmbeddingGenerationLeaseRenewTransient):
        lease.renew()

    lease.assert_serving()
    assert lease.remaining_seconds() > 0.0


def test_acknowledge_requires_watermark_proof() -> None:
    db = _StubHubDatabase()
    state = EmbeddingGenerationState(cast(Any, db))

    with pytest.raises(ValueError, match="watermark proof"):
        state.acknowledge(uuid4(), "run-1", 7, lease_seconds=30.0)

    state.acknowledge(uuid4(), "run-1", 7, lease_seconds=30.0, acknowledged=False)
    assert db.ack_insert_count() == 1


def test_serving_lease_convenience_method_deleted() -> None:
    assert not hasattr(EmbeddingGenerationState, "serving_lease")
    assert not hasattr(EmbeddingGenerationState, "replay_after")


def test_remaining_seconds_zero_when_fenced_or_unactivated() -> None:
    db = _StubHubDatabase()
    state = EmbeddingGenerationState(cast(Any, db))
    lease = _lease(state)

    assert lease.remaining_seconds() == 0.0
    lease.activate()
    assert lease.remaining_seconds() > 0.0
    lease.fence()
    assert lease.remaining_seconds() == 0.0


def test_unmanaged_lease_identity_uses_ledger_watermark() -> None:
    class _WatermarkState:
        def watermark(self) -> int:
            return 42

    generation, revision, proof = services._serving_lease_identity(
        {}, 7, cast(Any, _WatermarkState())
    )

    assert generation == "config-revision:7"
    assert revision == 7
    assert proof == 42


def test_managed_lease_identity_uses_completed_record() -> None:
    record = _completed_record(run_id="run-9", committed_revision=12, caught_up_watermark=9)
    managed = {EMBEDDING_SWITCH_COMPLETED_KEY: record}

    identity = services._serving_lease_identity(managed, 99, cast(Any, object()))

    assert identity == ("run-9", 12, 9)


def test_managed_lease_renews_while_daemon_event_loop_is_stalled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(embedding_lease, "_EMBEDDING_LEASE_RENEW_SECONDS", 0.01)
    loop = asyncio.new_event_loop()
    db = _StubHubDatabase()
    handle = embedding_lease._ManagedEmbeddingLease(
        _lease(EmbeddingGenerationState(cast(Any, db))),
        loop,
        read_completed_record=lambda: None,
        request_rebuild=lambda _record: None,
    )

    try:
        handle.activate()
        # The loop never runs, reproducing the scheduling resource stalled by spawn.
        assert db.renewed.wait(timeout=1.0)
        assert handle.renewal is not None
        assert not handle.renewal.done()
    finally:
        handle.dispose()
        if handle.renewal is not None:
            handle.renewal.result(timeout=1.0)
        loop.close()


def test_renew_with_backoff_retries_transient_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(embedding_lease, "_EMBEDDING_RENEW_BACKOFF_INITIAL_SECONDS", 0.01)
    db = _StubHubDatabase()
    state = EmbeddingGenerationState(cast(Any, db))
    lease = _lease(state, lease_seconds=30.0)
    lease.activate()
    db.fail_renew_times = 2

    assert embedding_lease._renew_with_backoff(lease) is True

    lease.assert_serving()
    assert db.fail_renew_times == 0
    transient_logs = [
        message
        for message in caplog.messages
        if message.startswith("Embedding generation lease renewal failed transiently")
    ]
    assert len(transient_logs) == 2
    for attempt, message in enumerate(transient_logs, start=1):
        assert f"attempt={attempt}" in message
        assert "elapsed_ms=" in message
        assert "remaining_lease_seconds=" in message
        assert "cause=ConnectionError: transient database failure" in message


def test_renew_with_backoff_fences_when_deadline_budget_exhausted() -> None:
    db = _StubHubDatabase()
    state = EmbeddingGenerationState(cast(Any, db))
    # TTL 1.0 -> margin 0.1 -> remaining ~0.9, below the 1.0s initial backoff,
    # so the first transient failure exhausts the budget and fences.
    lease = _lease(state, lease_seconds=1.0)
    lease.activate()
    db.fail_execute = ConnectionError("partition")

    assert embedding_lease._renew_with_backoff(lease) is False

    with pytest.raises(EmbeddingGenerationLeaseLost, match="fenced"):
        lease.assert_serving()


def test_renew_with_backoff_logs_slow_success_at_debug(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    db = _StubHubDatabase()
    state = EmbeddingGenerationState(cast(Any, db))
    lease = _lease(state, lease_seconds=30.0)
    lease.activate()
    # Only the module-local clock is faked, so the elapsed measurement crosses
    # the 1s slow threshold while the lease's own deadline math stays real.
    clock = iter((0.0, 2.0))
    monkeypatch.setattr(embedding_lease, "time", SimpleNamespace(monotonic=lambda: next(clock)))

    with caplog.at_level(logging.DEBUG, logger=embedding_lease.__name__):
        assert embedding_lease._renew_with_backoff(lease) is True

    slow_records = [
        record
        for record in caplog.records
        if record.getMessage().startswith("Embedding generation lease renewal completed slowly")
    ]
    assert len(slow_records) == 1
    assert slow_records[0].levelno == logging.DEBUG
    assert "elapsed_ms=2000.0" in slow_records[0].getMessage()


@pytest.mark.asyncio
async def test_reacquire_matching_record_reacknowledges_fresh_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(embedding_lease, "_EMBEDDING_REACQUIRE_POLL_SECONDS", 0.01)
    db = _StubHubDatabase()
    state = EmbeddingGenerationState(cast(Any, db))
    lease = _lease(state, generation="run-1", revision=7, watermark=3)
    lease.activate()
    lease.fence()
    rebuilds: list[CompletedSwitchRecord | None] = []
    handle = embedding_lease._ManagedEmbeddingLease(
        lease,
        asyncio.get_running_loop(),
        read_completed_record=lambda: _completed_record(run_id="run-1", committed_revision=7),
        request_rebuild=rebuilds.append,
    )

    assert await embedding_lease._reacquire_lease(handle) is True

    assert handle.lease is not lease
    handle.assert_serving()
    assert db.ack_insert_count() == 2
    assert rebuilds == []


@pytest.mark.asyncio
async def test_reacquire_mismatch_requests_rebuild_and_stays_fenced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(embedding_lease, "_EMBEDDING_REACQUIRE_POLL_SECONDS", 0.01)
    db = _StubHubDatabase()
    state = EmbeddingGenerationState(cast(Any, db))
    lease = _lease(state, generation="run-1", revision=7)
    lease.activate()
    lease.fence()
    newer = _completed_record(run_id="run-2", committed_revision=9)
    rebuilds: list[CompletedSwitchRecord | None] = []
    handle = embedding_lease._ManagedEmbeddingLease(
        lease,
        asyncio.get_running_loop(),
        read_completed_record=lambda: newer,
        request_rebuild=rebuilds.append,
    )

    assert await embedding_lease._reacquire_lease(handle) is False

    assert rebuilds == [newer]
    with pytest.raises(EmbeddingGenerationLeaseLost, match="fenced"):
        handle.assert_serving()
    assert db.ack_insert_count() == 1


@pytest.mark.asyncio
async def test_reacquire_unmanaged_lease_without_record_reacknowledges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(embedding_lease, "_EMBEDDING_REACQUIRE_POLL_SECONDS", 0.01)
    db = _StubHubDatabase()
    state = EmbeddingGenerationState(cast(Any, db))
    lease = _lease(state, generation="config-revision:5", revision=5)
    lease.activate()
    lease.fence()
    handle = embedding_lease._ManagedEmbeddingLease(
        lease,
        asyncio.get_running_loop(),
        read_completed_record=lambda: None,
        request_rebuild=lambda record: pytest.fail("unexpected rebuild request"),
    )

    assert await embedding_lease._reacquire_lease(handle) is True

    handle.assert_serving()


@pytest.mark.asyncio
async def test_reacquire_unmanaged_lease_lost_to_newer_serving_lease_requests_rebuild(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(embedding_lease, "_EMBEDDING_REACQUIRE_POLL_SECONDS", 0.01)
    db = _StubHubDatabase()
    state = EmbeddingGenerationState(cast(Any, db))
    lease = _lease(state, generation="config-revision:5", revision=5)
    lease.activate()
    lease.fence()
    monkeypatch.setattr(
        EmbeddingServingLease,
        "activate",
        lambda _lease: (_ for _ in ()).throw(
            EmbeddingGenerationLeaseLost("cannot replace a newer serving lease")
        ),
    )
    rebuilds: list[CompletedSwitchRecord | None] = []
    handle = embedding_lease._ManagedEmbeddingLease(
        lease,
        asyncio.get_running_loop(),
        read_completed_record=lambda: None,
        request_rebuild=rebuilds.append,
    )

    assert await embedding_lease._reacquire_lease(handle) is False

    assert rebuilds == [None]
    assert db.ack_insert_count() == 1


def test_renew_loop_routes_local_deadline_lapse_to_reacquisition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(embedding_lease, "_EMBEDDING_LEASE_RENEW_SECONDS", 0.0)
    events: list[str] = []
    reacquired: list[object] = []

    def deadline_lapsed(_lease: object, *, stop_event: threading.Event) -> bool:
        raise EmbeddingGenerationLeaseExpired("Embedding generation serving lease expired")

    def stop_after_reacquire(handle: object) -> bool:
        reacquired.append(handle)
        events.append("reacquire")
        return False

    monkeypatch.setattr(embedding_lease, "_renew_with_backoff", deadline_lapsed)
    monkeypatch.setattr(
        embedding_lease, "_reacquire_lease_from_renewal_thread", stop_after_reacquire
    )
    lease = MagicMock()
    lease.generation = "run-1"
    lease.revision = 7
    lease.fence.side_effect = lambda: events.append("fence")
    handle = cast(Any, SimpleNamespace(lease=lease, renewal_stop=threading.Event()))

    embedding_lease._renew_embedding_lease(handle)

    assert events == ["fence", "reacquire"]
    assert reacquired == [handle]
    lease.fence.assert_called_once_with()


def test_renew_loop_routes_rowcount_mismatch_to_reacquisition(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(embedding_lease, "_EMBEDDING_LEASE_RENEW_SECONDS", 0.0)
    db = _StubHubDatabase()
    state = EmbeddingGenerationState(cast(Any, db))
    lease = _lease(state, generation="run-1", revision=7)
    lease.activate()
    db.renew_rowcount = 0
    reacquisitions: list[object] = []

    def stop_after_reacquire(handle: object) -> bool:
        reacquisitions.append(handle)
        return False

    monkeypatch.setattr(
        embedding_lease, "_reacquire_lease_from_renewal_thread", stop_after_reacquire
    )
    loop = asyncio.new_event_loop()
    handle = embedding_lease._ManagedEmbeddingLease(
        lease,
        loop,
        read_completed_record=lambda: _completed_record(),
        request_rebuild=lambda _record: None,
    )

    try:
        with caplog.at_level(logging.WARNING, logger=embedding_lease.__name__):
            embedding_lease._renew_embedding_lease(handle)
    finally:
        loop.close()

    assert reacquisitions == [handle]
    with pytest.raises(EmbeddingGenerationLeaseLost, match="fenced"):
        handle.assert_serving()
    recovery_records = [
        record for record in caplog.records if "attempting re-acquisition" in record.message
    ]
    assert len(recovery_records) == 1
    recovery_context = vars(recovery_records[0])
    assert recovery_context["expected_generation"] == "run-1"
    assert recovery_context["expected_revision"] == 7


@pytest.mark.asyncio
async def test_rebuild_request_reconciles_and_reprepares_current_revision() -> None:
    rebuilt = asyncio.Event()
    revisions: list[int] = []
    subscribers: list[str] = []
    memory_services = object()
    snapshot = SimpleNamespace(revision=7, failed_live_keys={})

    class Runtime:
        def capture(self) -> object:
            return SimpleNamespace(
                snapshot=snapshot,
                services={"memory_services": memory_services},
            )

        async def reconcile_revision(self, revision: int) -> None:
            revisions.append(revision)

        async def reprepare_subscriber(self, name: str) -> object:
            subscribers.append(name)
            rebuilt.set()
            return snapshot

    runner = cast(Any, SimpleNamespace(config_runtime=Runtime()))

    services._request_memory_services_rebuild(runner, asyncio.get_running_loop(), None)
    await asyncio.wait_for(rebuilt.wait(), timeout=1.0)

    assert revisions == [7]
    assert subscribers == ["memory_services"]


@pytest.mark.asyncio
async def test_rebuild_request_skips_reprepare_when_reconcile_replaces_service() -> None:
    replacement_visible = asyncio.Event()
    subscribers: list[str] = []
    snapshot = SimpleNamespace(revision=7, failed_live_keys={})

    class Runtime:
        memory_services = object()
        reconciled = False

        def capture(self) -> object:
            if self.reconciled:
                replacement_visible.set()
            return SimpleNamespace(
                snapshot=snapshot,
                services={"memory_services": self.memory_services},
            )

        async def reconcile_revision(self, revision: int) -> None:
            assert revision == 7
            self.memory_services = object()
            self.reconciled = True

        async def reprepare_subscriber(self, name: str) -> object:
            subscribers.append(name)
            return snapshot

    runner = cast(Any, SimpleNamespace(config_runtime=Runtime()))

    services._request_memory_services_rebuild(runner, asyncio.get_running_loop(), None)
    await asyncio.wait_for(replacement_visible.wait(), timeout=1.0)

    assert subscribers == []


@pytest.mark.asyncio
async def test_rebuild_request_retries_degraded_reprepare(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(services, "_EMBEDDING_REACQUIRE_POLL_SECONDS", 0)
    rebuilt = asyncio.Event()
    memory_services = object()
    clean = SimpleNamespace(revision=7, failed_live_keys={})
    degraded = SimpleNamespace(
        revision=7,
        failed_live_keys={"memory.embedding_model": SimpleNamespace(subscriber="memory_services")},
    )

    class Runtime:
        reprepare_calls = 0

        def capture(self) -> object:
            return SimpleNamespace(
                snapshot=clean,
                services={"memory_services": memory_services},
            )

        async def reconcile_revision(self, revision: int) -> None:
            assert revision == 7

        async def reprepare_subscriber(self, name: str) -> object:
            assert name == "memory_services"
            self.reprepare_calls += 1
            if self.reprepare_calls == 2:
                rebuilt.set()
                return clean
            return degraded

    runtime = Runtime()
    runner = cast(Any, SimpleNamespace(config_runtime=runtime))

    services._request_memory_services_rebuild(runner, asyncio.get_running_loop(), None)
    await asyncio.wait_for(rebuilt.wait(), timeout=1.0)

    assert runtime.reprepare_calls == 2


def test_rebuild_request_ignores_runtime_startup_window() -> None:
    class Runtime:
        capture_calls = 0

        def capture(self) -> object:
            self.capture_calls += 1
            raise RuntimeError("ConfigRuntime has not started")

    runtime = Runtime()
    runner = cast(Any, SimpleNamespace(config_runtime=runtime))
    loop = asyncio.new_event_loop()
    try:
        services._request_memory_services_rebuild(runner, loop, None)
    finally:
        loop.close()
    assert runtime.capture_calls == 1


def test_completed_switch_reader_treats_value_error_as_malformed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Store:
        def get_internal_lifecycle(self, key: str) -> object:
            assert key == EMBEDDING_SWITCH_COMPLETED_KEY
            return {}

    def invalid_record(_raw: object) -> CompletedSwitchRecord:
        raise ValueError("malformed completed record")

    monkeypatch.setattr(services, "ConfigStore", lambda _database: Store())
    monkeypatch.setattr(CompletedSwitchRecord, "from_dict", invalid_record)
    runner = cast(Any, SimpleNamespace(database=object()))

    assert services._read_completed_switch_record(runner) is None


@pytest.mark.asyncio
async def test_reacquire_polls_until_storage_reachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(embedding_lease, "_EMBEDDING_REACQUIRE_POLL_SECONDS", 0.01)
    db = _StubHubDatabase()
    state = EmbeddingGenerationState(cast(Any, db))
    lease = _lease(state, generation="run-1", revision=7)
    lease.activate()
    lease.fence()
    probes = {"count": 0}

    def read_record() -> CompletedSwitchRecord | None:
        probes["count"] += 1
        if probes["count"] < 3:
            raise ConnectionError("storage still unreachable")
        return _completed_record(run_id="run-1", committed_revision=7)

    handle = embedding_lease._ManagedEmbeddingLease(
        lease,
        asyncio.get_running_loop(),
        read_completed_record=read_record,
        request_rebuild=lambda record: pytest.fail("unexpected rebuild request"),
    )

    assert await embedding_lease._reacquire_lease(handle) is True

    assert probes["count"] == 3
    handle.assert_serving()


@pytest.mark.asyncio
async def test_reacquire_disposal_during_probe_does_not_reacknowledge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(embedding_lease, "_EMBEDDING_REACQUIRE_POLL_SECONDS", 0.01)
    db = _StubHubDatabase()
    state = EmbeddingGenerationState(cast(Any, db))
    lease = _lease(state, generation="run-1", revision=7)
    lease.activate()
    lease.fence()
    probe_started = threading.Event()
    release_probe = threading.Event()

    def read_record() -> CompletedSwitchRecord | None:
        probe_started.set()
        release_probe.wait()
        return _completed_record(run_id="run-1", committed_revision=7)

    handle = embedding_lease._ManagedEmbeddingLease(
        lease,
        asyncio.get_running_loop(),
        read_completed_record=read_record,
        request_rebuild=lambda _record: pytest.fail("unexpected rebuild request"),
    )
    recovery = asyncio.create_task(embedding_lease._reacquire_lease(handle))
    await asyncio.wait_for(asyncio.to_thread(probe_started.wait), timeout=1.0)
    handle.dispose()
    release_probe.set()

    assert await asyncio.wait_for(recovery, timeout=1.0) is False
    assert db.ack_insert_count() == 1


@pytest.mark.asyncio
async def test_renew_loop_fences_reacquires_and_resumes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(embedding_lease, "_EMBEDDING_LEASE_RENEW_SECONDS", 0.01)
    monkeypatch.setattr(embedding_lease, "_EMBEDDING_REACQUIRE_POLL_SECONDS", 0.01)
    db = _StubHubDatabase()
    state = EmbeddingGenerationState(cast(Any, db))
    # TTL 1.0 keeps remaining below the initial backoff so one transient
    # failure fences without backoff sleeps.
    lease = _lease(state, generation="run-1", revision=7, lease_seconds=1.0)
    lease.activate()
    db.ack_inserted.clear()

    reacquisition_count = 0

    def read_record() -> CompletedSwitchRecord | None:
        nonlocal reacquisition_count
        reacquisition_count += 1
        db.fail_execute = None
        if reacquisition_count == 2:
            db.renew_rowcount = 1
        return _completed_record(run_id="run-1", committed_revision=7)

    handle = embedding_lease._ManagedEmbeddingLease(
        lease,
        asyncio.get_running_loop(),
        read_completed_record=read_record,
        request_rebuild=lambda record: pytest.fail("unexpected rebuild request"),
    )
    db.fail_execute = ConnectionError("partition")
    loop_task = asyncio.create_task(
        asyncio.to_thread(embedding_lease._renew_embedding_lease, handle)
    )
    try:
        acknowledged = await asyncio.wait_for(asyncio.to_thread(db.ack_inserted.wait), timeout=5.0)
        assert acknowledged
        assert handle.lease is not lease
        handle.assert_serving()

        first_successor = handle.lease
        db.ack_inserted.clear()
        db.renew_rowcount = 0
        acknowledged = await asyncio.wait_for(asyncio.to_thread(db.ack_inserted.wait), timeout=5.0)
        assert acknowledged
        assert handle.lease is not first_successor
        db.renewed.clear()
        renewed = await asyncio.wait_for(asyncio.to_thread(db.renewed.wait), timeout=5.0)
        assert renewed
        handle.dispose()
        await asyncio.wait_for(loop_task, timeout=5.0)
        assert reacquisition_count == 2
        assert db.ack_insert_count() == 3
        with pytest.raises(EmbeddingGenerationLeaseLost):
            handle.assert_serving()
    finally:
        if not loop_task.done():
            handle.renewal_stop.set()
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(loop_task, timeout=5.0)
