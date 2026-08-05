"""PostgreSQL-backed single-active-daemon lease contract."""

from __future__ import annotations

import os
import uuid

import pytest

from gobby.daemon_lease import (
    ActiveDaemonLease,
    FreshLeaseOwnerError,
    LeaseConnectionLostError,
)


def _test_database_url() -> str:
    value = os.environ.get("GOBBY_TEST_POSTGRES_URL")
    if not value:
        pytest.skip("GOBBY_TEST_POSTGRES_URL is required")
    return value


@pytest.mark.integration
def test_only_one_daemon_holds_the_runtime_lease() -> None:
    database_url = _test_database_url()
    first = ActiveDaemonLease(database_url, machine_id=str(uuid.uuid4()))
    second = ActiveDaemonLease(database_url, machine_id=str(uuid.uuid4()))

    try:
        assert first.try_acquire() is True
        assert second.try_acquire() is False
        status = second.status()
        assert status.held is True
        assert status.owner_application_name == first.application_name

        first.release()
        assert second.try_acquire() is True
    finally:
        first.release()
        second.release()


@pytest.mark.integration
def test_force_recovery_refuses_a_fresh_verified_owner() -> None:
    database_url = _test_database_url()
    owner = ActiveDaemonLease(database_url, machine_id=str(uuid.uuid4()))
    standby = ActiveDaemonLease(database_url, machine_id=str(uuid.uuid4()))

    try:
        assert owner.try_acquire() is True
        owner.heartbeat()
        with pytest.raises(FreshLeaseOwnerError, match="lease owner is still fresh"):
            standby.recover_stale_owner(stale_after_seconds=60.0)
    finally:
        owner.release()
        standby.release()


@pytest.mark.integration
def test_verified_stale_owner_is_terminated_before_promotion() -> None:
    database_url = _test_database_url()
    owner = ActiveDaemonLease(database_url, machine_id=str(uuid.uuid4()))
    standby = ActiveDaemonLease(database_url, machine_id=str(uuid.uuid4()))

    try:
        assert owner.try_acquire() is True
        recovered = standby.recover_stale_owner(stale_after_seconds=0.0)
        assert recovered.owner_application_name == owner.application_name
        assert standby.try_acquire() is True
        with pytest.raises(LeaseConnectionLostError):
            owner.heartbeat()
    finally:
        owner.release()
        standby.release()
