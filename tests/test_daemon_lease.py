"""PostgreSQL-backed single-active-daemon lease contract."""

from __future__ import annotations

import hashlib
import hmac
import os
import uuid
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock

import psycopg
import pytest

from gobby.daemon_lease import (
    ActiveDaemonLease,
    FreshLeaseOwnerError,
    LeaseConnectionLostError,
    current_lease,
)
from gobby.deployment import deployment_advisory_key

_REPO_ROOT = Path(__file__).resolve().parents[1]
_BASELINE_SQL = _REPO_ROOT / "crates/gcore/assets/schema/baseline.sql"
_DAEMON_LEASE_PY = _REPO_ROOT / "src/gobby/daemon_lease.py"


def _test_database_url() -> str:
    value = os.environ.get("DATABASE_URL") or os.environ.get("GOBBY_TEST_POSTGRES_URL")
    if not value:
        pytest.skip("DATABASE_URL or GOBBY_TEST_POSTGRES_URL is required")
    return value


def _ensure_deployment_runtime(database_url: str) -> None:
    with psycopg.connect(database_url, autocommit=True) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS deployment_runtime (
                deployment_token TEXT PRIMARY KEY,
                fencing_epoch BIGINT NOT NULL DEFAULT 0,
                grant_signing_secret TEXT NOT NULL,
                epoch_updated_at TIMESTAMPTZ
            )
            """
        )


def _runtime_row(database_url: str, token: str) -> tuple[int, str]:
    with psycopg.connect(database_url) as connection:
        row = connection.execute(
            """
            SELECT fencing_epoch, grant_signing_secret
              FROM deployment_runtime
             WHERE deployment_token = %s
            """,
            (token,),
        ).fetchone()
    assert row is not None
    return int(row[0]), str(row[1])


@pytest.mark.unit
def test_lease_keying_uses_deployment_advisory_key() -> None:
    source = _DAEMON_LEASE_PY.read_text(encoding="utf-8")
    assert "hashtext" not in source
    assert "deployment_advisory_key" in source
    assert "single-active-daemon" in source


@pytest.mark.unit
def test_baseline_seals_deployment_runtime_and_interactive_ciphertext() -> None:
    baseline = _BASELINE_SQL.read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS deployment_runtime" in baseline
    assert "fencing_epoch BIGINT NOT NULL DEFAULT 0" in baseline
    assert "grant_signing_secret TEXT NOT NULL" in baseline
    assert "CREATE TABLE IF NOT EXISTS gobby_agent_auth.interactive_credential_material" in baseline
    assert "ciphertext" in baseline
    assert "aad_identity" in baseline
    assert "issue_or_reuse_interactive_principal" in baseline
    table_start = baseline.index(
        "CREATE TABLE IF NOT EXISTS gobby_agent_auth.interactive_credential_material"
    )
    table_end = baseline.index(");", table_start) + 2
    material = baseline[table_start:table_end].lower()
    for forbidden in ("dsn", "password", "connection_uri", "plaintext"):
        assert forbidden not in material


@pytest.mark.integration
def test_only_one_daemon_holds_the_runtime_lease() -> None:
    database_url = _test_database_url()
    _ensure_deployment_runtime(database_url)
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


def test_owns_live_lease_uses_only_cached_ownership() -> None:
    lease = ActiveDaemonLease("postgresql://unused.invalid/gobby", machine_id="machine-a")
    connection = MagicMock()
    connection.closed = False
    lease._connection = cast(Any, connection)
    lease._fencing_epoch = 7

    assert lease.owns_live_lease() is True
    connection.execute.assert_not_called()

    abort = lease.abort_heartbeat(on_invalidated=lambda: None, cancel_timeout_seconds=0.01)

    assert lease.owns_live_lease() is False
    connection.execute.assert_not_called()
    connection.cancel_safe.assert_called_once_with(timeout=0.01)
    lease.close_aborted_heartbeat(abort)


def test_aborted_owner_releases_advisory_lock_for_successor() -> None:
    database_url = _test_database_url()
    _ensure_deployment_runtime(database_url)
    token = uuid.uuid4().hex
    owner = ActiveDaemonLease(
        database_url,
        machine_id=str(uuid.uuid4()),
        deployment_token=token,
    )
    successor = ActiveDaemonLease(
        database_url,
        machine_id=str(uuid.uuid4()),
        deployment_token=token,
    )

    try:
        assert owner.try_acquire() is True
        assert current_lease() is owner
        abort = owner.abort_heartbeat(on_invalidated=lambda: None)
        owner.close_aborted_heartbeat(abort)

        assert owner.owns_live_lease() is False
        assert current_lease() is None
        assert successor.try_acquire() is True
    finally:
        owner.release()
        successor.release()


@pytest.mark.integration
def test_force_recovery_refuses_a_fresh_verified_owner() -> None:
    database_url = _test_database_url()
    _ensure_deployment_runtime(database_url)
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
    _ensure_deployment_runtime(database_url)
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


@pytest.mark.integration
def test_deployment_scoped_lease_and_epoch() -> None:
    database_url = _test_database_url()
    _ensure_deployment_runtime(database_url)
    first_token = uuid.uuid4().hex[:16]
    second_token = uuid.uuid4().hex[:16]
    assert deployment_advisory_key("single-active-daemon", token=first_token) != (
        deployment_advisory_key("single-active-daemon", token=second_token)
    )
    first = ActiveDaemonLease(
        database_url,
        machine_id=str(uuid.uuid4()),
        deployment_token=first_token,
    )
    second = ActiveDaemonLease(
        database_url,
        machine_id=str(uuid.uuid4()),
        deployment_token=second_token,
    )

    try:
        assert first.try_acquire() is True
        assert second.try_acquire() is True
        first_epoch, _first_secret = _runtime_row(database_url, first_token)
        second_epoch, _second_secret = _runtime_row(database_url, second_token)
        assert first.fencing_epoch == first_epoch == 1
        assert second.fencing_epoch == second_epoch == 1
        assert first.is_live() is True
        assert second.is_live() is True
        first.release()
        assert first.try_acquire() is True
        assert _runtime_row(database_url, first_token)[0] == 2
        assert _runtime_row(database_url, second_token)[0] == 1
    finally:
        first.release()
        second.release()


@pytest.mark.integration
def test_signing_secret_rotates_on_acquisition() -> None:
    database_url = _test_database_url()
    _ensure_deployment_runtime(database_url)
    token = uuid.uuid4().hex[:16]
    lease = ActiveDaemonLease(
        database_url,
        machine_id=str(uuid.uuid4()),
        deployment_token=token,
    )
    try:
        assert lease.try_acquire() is True
        assert current_lease() is lease
        first_epoch, first_secret = _runtime_row(database_url, token)
        archived_mac = hmac.new(
            first_secret.encode(),
            b"archived-grant",
            hashlib.sha256,
        ).hexdigest()
        lease.release()
        assert current_lease() is None

        with psycopg.connect(database_url, autocommit=True) as connection:
            connection.execute(
                """
                UPDATE deployment_runtime
                   SET fencing_epoch = %s,
                       grant_signing_secret = %s
                 WHERE deployment_token = %s
                """,
                (first_epoch, first_secret, token),
            )

        assert lease.try_acquire() is True
        restored_epoch, rotated_secret = _runtime_row(database_url, token)
        assert restored_epoch == first_epoch + 1
        assert rotated_secret != first_secret
        assert lease.grant_signing_secret == rotated_secret
        rotated_mac = hmac.new(
            rotated_secret.encode(),
            b"archived-grant",
            hashlib.sha256,
        ).hexdigest()
        assert not hmac.compare_digest(archived_mac, rotated_mac)
    finally:
        lease.release()
