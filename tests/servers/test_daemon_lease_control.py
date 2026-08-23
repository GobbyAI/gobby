"""Minimal standby lease-control HTTP surface."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from dataclasses import dataclass

from fastapi.testclient import TestClient

from gobby.daemon_lease import (
    DaemonLeaseStatus,
    FreshLeaseOwnerError,
    LeaseConnectionLostError,
    LeaseHeartbeatAbort,
)
from gobby.daemon_lease_control import (
    LeaseLoss,
    StandbyLeaseControl,
    create_standby_app,
    monitor_active_lease,
)


@dataclass
class FakeLease:
    acquire_result: bool = False
    recovered: bool = False

    def status(self) -> DaemonLeaseStatus:
        return DaemonLeaseStatus(
            held=not self.acquire_result,
            owner_pid=42 if not self.acquire_result else None,
            owner_application_name="gobby-lease-v1:owner:instance"
            if not self.acquire_result
            else None,
            heartbeat_age_seconds=0.25 if not self.acquire_result else None,
        )

    def try_acquire(self) -> bool:
        return self.acquire_result

    def heartbeat(self) -> None:
        return None

    def abort_heartbeat(
        self,
        *,
        on_invalidated: Callable[[], None],
        cancel_timeout_seconds: float = 1.0,
    ) -> LeaseHeartbeatAbort:
        on_invalidated()
        return LeaseHeartbeatAbort(None, "connection_unavailable")

    def close_aborted_heartbeat(self, abort: LeaseHeartbeatAbort) -> None:
        return None

    def recover_stale_owner(self, *, stale_after_seconds: float) -> object:
        if not self.recovered:
            raise FreshLeaseOwnerError("active-daemon lease owner is still fresh")
        return object()


@dataclass
class LostLease(FakeLease):
    def heartbeat(self) -> None:
        raise LeaseConnectionLostError("lease connection lost")


def _control(lease: FakeLease, events: list[str]) -> StandbyLeaseControl:
    return StandbyLeaseControl(
        lease=lease,
        database_url="postgresql://test.invalid/gobby_test",
        local_token="lease-token",
        promotion_requested=asyncio.Event(),
        schema_verifier=lambda _url: events.append("verify"),
    )


def test_standby_exposes_only_health_status_and_lease_control() -> None:
    control = _control(FakeLease(), [])
    client = TestClient(create_standby_app(control))

    assert client.get("/api/health").json()["lease_mode"] == "standby"
    assert client.get("/api/admin/status").json()["lease"]["mode"] == "standby"
    assert client.get("/api/admin/lease/status").json() == {
        "mode": "standby",
        "held": True,
        "owner_pid": 42,
        "owner_application_name": "gobby-lease-v1:owner:instance",
        "heartbeat_age_seconds": 0.25,
    }
    assert client.get("/mcp").status_code == 404
    assert client.get("/api/sessions").status_code == 404


def test_promotion_requires_auth_and_schema_verification_before_acquisition() -> None:
    events: list[str] = []
    lease = FakeLease(acquire_result=True)
    control = _control(lease, events)
    client = TestClient(create_standby_app(control))

    assert client.post("/api/admin/lease/promote").status_code == 401
    response = client.post(
        "/api/admin/lease/promote",
        headers={"Authorization": "Bearer lease-token"},
    )

    assert response.status_code == 200
    assert response.json()["promoting"] is True
    assert events == ["verify"]
    assert control.promotion_requested.is_set()


def test_promotion_reports_current_owner_when_lease_is_held() -> None:
    control = _control(FakeLease(acquire_result=False), [])
    client = TestClient(create_standby_app(control))

    response = client.post(
        "/api/admin/lease/promote",
        headers={"Authorization": "Bearer lease-token"},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["owner_pid"] == 42


def test_recovery_refuses_fresh_owner() -> None:
    control = _control(FakeLease(), [])
    client = TestClient(create_standby_app(control))

    response = client.post(
        "/api/admin/lease/recover",
        params={"stale_after_seconds": 60},
        headers={"Authorization": "Bearer lease-token"},
    )

    assert response.status_code == 409
    assert "still fresh" in response.json()["detail"]


async def test_lease_connection_loss_requests_active_shutdown() -> None:
    lease = LostLease()
    stop = asyncio.Event()
    losses: list[LeaseLoss] = []
    invalidated = asyncio.Event()

    await asyncio.wait_for(
        monitor_active_lease(
            lease,
            stop=stop,
            on_loss=losses.append,
            on_invalidation=invalidated.set,
            heartbeat_interval_seconds=0.001,
        ),
        timeout=1.0,
    )

    assert invalidated.is_set()
    assert [loss.reason for loss in losses] == ["lease_connection_lost"]


@dataclass
class BlockedLease(FakeLease):
    def __init__(self) -> None:
        self.heartbeat_started = threading.Event()
        self.cancellation_called = threading.Event()

    def heartbeat(self) -> None:
        self.heartbeat_started.set()
        threading.Event().wait()

    def abort_heartbeat(
        self,
        *,
        on_invalidated: Callable[[], None],
        cancel_timeout_seconds: float = 1.0,
    ) -> LeaseHeartbeatAbort:
        on_invalidated()
        self.cancellation_called.set()
        return LeaseHeartbeatAbort(None, "cancel_requested")


async def test_lease_heartbeat_timeout_requests_active_shutdown() -> None:
    lease = BlockedLease()
    stop = asyncio.Event()
    losses: list[LeaseLoss] = []
    invalidated = asyncio.Event()

    await asyncio.wait_for(
        monitor_active_lease(
            lease,
            stop=stop,
            on_loss=losses.append,
            on_invalidation=invalidated.set,
            heartbeat_interval_seconds=0.001,
            heartbeat_timeout_seconds=0.01,
            heartbeat_cleanup_timeout_seconds=0.02,
        ),
        timeout=1.0,
    )

    assert lease.heartbeat_started.is_set()
    assert lease.cancellation_called.is_set()
    assert invalidated.is_set()
    assert len(losses) == 1
    assert losses[0].reason == "lease_heartbeat_timeout"
    assert losses[0].cancellation_outcome == "cancel_requested"
    assert losses[0].worker_settled is False
    heartbeat_threads = [
        thread for thread in threading.enumerate() if thread.name == "gobby-lease-heartbeat"
    ]
    assert heartbeat_threads
    assert all(thread.daemon for thread in heartbeat_threads)


async def test_lease_monitor_stop_returns_before_heartbeat() -> None:
    lease = BlockedLease()
    stop = asyncio.Event()
    stop.set()
    shutdown_requested = asyncio.Event()

    await asyncio.wait_for(
        monitor_active_lease(
            lease,
            stop=stop,
            on_loss=lambda _loss: shutdown_requested.set(),
            heartbeat_interval_seconds=0.001,
            heartbeat_timeout_seconds=0.05,
        ),
        timeout=1.0,
    )

    assert not shutdown_requested.is_set()
