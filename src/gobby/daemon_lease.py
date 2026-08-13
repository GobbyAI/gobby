"""PostgreSQL lifetime lease for single-active-daemon ownership."""

from __future__ import annotations

import secrets
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any

import psycopg

from gobby.deployment import deployment_advisory_key
from gobby.deployment import deployment_token as derive_deployment_token

_LEASE_PURPOSE = "single-active-daemon"
_APPLICATION_PREFIX = "gobby-lease-v1:"
_RECOVERY_TIMEOUT_SECONDS = 5.0


class DaemonLeaseError(RuntimeError):
    """Base error for active-daemon lease operations."""


class LeaseConnectionLostError(DaemonLeaseError):
    """Raised when the dedicated lease connection is no longer usable."""


class FreshLeaseOwnerError(DaemonLeaseError):
    """Raised when recovery targets a holder with a recent heartbeat."""


class UnverifiedLeaseOwnerError(DaemonLeaseError):
    """Raised when the exact lease is held by an unknown application."""


@dataclass(frozen=True, slots=True)
class DaemonLeaseStatus:
    """Observed owner of the singleton lease."""

    held: bool
    owner_pid: int | None = None
    owner_application_name: str | None = None
    heartbeat_age_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class RecoveredLeaseOwner:
    """Verified backend terminated during stale-owner recovery."""

    owner_pid: int
    owner_application_name: str
    heartbeat_age_seconds: float


def _advisory_lock_parts(key: int) -> tuple[int, int]:
    unsigned = key & 0xFFFFFFFFFFFFFFFF
    return (unsigned >> 32) & 0xFFFFFFFF, unsigned & 0xFFFFFFFF


class ActiveDaemonLease:
    """Own a PostgreSQL session advisory lock for the active runtime lifetime."""

    def __init__(
        self,
        database_url: str,
        *,
        machine_id: str,
        deployment_token: str | None = None,
        connect_timeout_seconds: int = 5,
    ) -> None:
        if not database_url:
            raise ValueError("database_url is required")
        if not machine_id:
            raise ValueError("machine_id is required")
        if connect_timeout_seconds <= 0:
            raise ValueError("connect_timeout_seconds must be positive")
        instance = uuid.uuid4().hex[:8]
        self.database_url = database_url
        self.machine_id = machine_id
        self.deployment_token = deployment_token or derive_deployment_token()
        self.application_name = f"{_APPLICATION_PREFIX}{machine_id}:{instance}"
        self.connect_timeout_seconds = connect_timeout_seconds
        self._connection: psycopg.Connection[Any] | None = None
        self._key: int | None = None
        self._fencing_epoch: int | None = None
        self._grant_signing_secret: str | None = None
        self._mutex = threading.RLock()

    @property
    def acquired(self) -> bool:
        """Return whether this object currently owns an open lease session."""
        with self._mutex:
            return self._connection is not None

    @property
    def fencing_epoch(self) -> int | None:
        """Return the epoch cached at the last successful acquisition."""
        with self._mutex:
            return self._fencing_epoch

    @property
    def grant_signing_secret(self) -> str | None:
        """Return the signing secret rotated at the last successful acquisition."""
        with self._mutex:
            return self._grant_signing_secret

    def try_acquire(self) -> bool:
        """Attempt the lease once, retaining the dedicated connection on success."""
        with self._mutex:
            if self._connection is not None:
                self.heartbeat()
                return True

            connection = self._connect(self.application_name)
            try:
                key = deployment_advisory_key(_LEASE_PURPOSE, token=self.deployment_token)
                row = connection.execute("SELECT pg_try_advisory_lock(%s)", (key,)).fetchone()
                acquired = bool(row and row[0])
                if not acquired:
                    connection.close()
                    return False
                secret = secrets.token_urlsafe(32)
                with connection.transaction():
                    bumped = connection.execute(
                        """
                        INSERT INTO deployment_runtime (
                            deployment_token,
                            fencing_epoch,
                            grant_signing_secret,
                            epoch_updated_at
                        ) VALUES (%s, 1, %s, clock_timestamp())
                        ON CONFLICT (deployment_token) DO UPDATE
                           SET fencing_epoch = deployment_runtime.fencing_epoch + 1,
                               grant_signing_secret = EXCLUDED.grant_signing_secret,
                               epoch_updated_at = clock_timestamp()
                        RETURNING fencing_epoch, grant_signing_secret
                        """,
                        (self.deployment_token, secret),
                    ).fetchone()
                if bumped is None:
                    raise DaemonLeaseError("deployment_runtime did not return an epoch")
                self._connection = connection
                self._key = key
                self._fencing_epoch = int(bumped[0])
                self._grant_signing_secret = str(bumped[1])
                return True
            except BaseException:
                connection.close()
                raise

    def heartbeat(self) -> None:
        """Refresh holder liveness or fail closed when the lease session is lost."""
        with self._mutex:
            connection = self._connection
            if connection is None:
                raise LeaseConnectionLostError("active-daemon lease is not held")
            try:
                connection.execute("SELECT 1").fetchone()
            except (psycopg.Error, OSError) as exc:
                self._forget_connection(connection)
                raise LeaseConnectionLostError("active-daemon lease connection was lost") from exc

    def is_live(self) -> bool:
        """Return whether the lease session is alive and the cached epoch is current."""
        with self._mutex:
            try:
                self.heartbeat()
            except LeaseConnectionLostError:
                return False
            owned_epoch = self._fencing_epoch
            token = self.deployment_token
        if owned_epoch is None:
            return False
        with self._connect(self._probe_application_name()) as connection:
            row = connection.execute(
                """
                SELECT fencing_epoch
                  FROM deployment_runtime
                 WHERE deployment_token = %s
                """,
                (token,),
            ).fetchone()
        return row is not None and int(row[0]) == owned_epoch

    def release(self) -> None:
        """Release the lease and close its dedicated PostgreSQL session."""
        with self._mutex:
            connection = self._connection
            key = self._key
            self._connection = None
            self._key = None
            self._fencing_epoch = None
            self._grant_signing_secret = None
            if connection is None:
                return
            try:
                if key is not None and not connection.closed:
                    connection.execute("SELECT pg_advisory_unlock(%s)", (key,)).fetchone()
            except (psycopg.Error, OSError):
                pass
            finally:
                connection.close()

    def status(self) -> DaemonLeaseStatus:
        """Inspect the exact singleton lease without mutating ownership."""
        with self._connect(self._probe_application_name()) as connection:
            key = deployment_advisory_key(_LEASE_PURPOSE, token=self.deployment_token)
            owner = self._read_owner(connection, key)
        if owner is None:
            return DaemonLeaseStatus(held=False)
        owner_pid, application_name, heartbeat_age = owner
        return DaemonLeaseStatus(
            held=True,
            owner_pid=owner_pid,
            owner_application_name=application_name,
            heartbeat_age_seconds=heartbeat_age,
        )

    def recover_stale_owner(self, *, stale_after_seconds: float) -> RecoveredLeaseOwner:
        """Terminate only a verified stale backend holding the exact lease."""
        if stale_after_seconds < 0:
            raise ValueError("stale_after_seconds cannot be negative")
        with self._connect(self._probe_application_name()) as connection:
            key = deployment_advisory_key(_LEASE_PURPOSE, token=self.deployment_token)
            owner = self._read_owner(connection, key)
            if owner is None:
                raise DaemonLeaseError("active-daemon lease has no owner")
            owner_pid, application_name, heartbeat_age = owner
            if not application_name.startswith(_APPLICATION_PREFIX):
                raise UnverifiedLeaseOwnerError(
                    "active-daemon lease holder has an unrecognized application identity"
                )
            if heartbeat_age < stale_after_seconds:
                raise FreshLeaseOwnerError(
                    "active-daemon lease owner is still fresh "
                    f"({heartbeat_age:.3f}s < {stale_after_seconds:.3f}s)"
                )
            class_id, object_id = _advisory_lock_parts(key)
            terminated = connection.execute(
                """
                SELECT pg_terminate_backend(activity.pid)
                  FROM pg_stat_activity AS activity
                 WHERE activity.pid = %s
                   AND activity.application_name = %s
                   AND clock_timestamp() - activity.state_change >=
                       make_interval(secs => %s)
                   AND EXISTS (
                       SELECT 1
                         FROM pg_locks AS locks
                        WHERE locks.pid = activity.pid
                          AND locks.locktype = 'advisory'
                          AND locks.granted
                          AND locks.objsubid = 1
                          AND locks.classid = %s
                          AND locks.objid = %s
                   )
                """,
                (
                    owner_pid,
                    application_name,
                    stale_after_seconds,
                    class_id,
                    object_id,
                ),
            ).fetchone()
            if not terminated or terminated[0] is not True:
                raise DaemonLeaseError(
                    "lease owner changed or refreshed during stale recovery verification"
                )
            deadline = time.monotonic() + _RECOVERY_TIMEOUT_SECONDS
            while time.monotonic() < deadline:
                if self._read_owner(connection, key) is None:
                    return RecoveredLeaseOwner(
                        owner_pid=owner_pid,
                        owner_application_name=application_name,
                        heartbeat_age_seconds=heartbeat_age,
                    )
                time.sleep(0.05)
        raise DaemonLeaseError("stale lease owner did not release within recovery timeout")

    def _connect(self, application_name: str) -> psycopg.Connection[Any]:
        return psycopg.connect(
            self.database_url,
            autocommit=True,
            connect_timeout=self.connect_timeout_seconds,
            application_name=application_name,
        )

    def _probe_application_name(self) -> str:
        return f"gobby-lease-probe-v1:{self.machine_id[:8]}:{uuid.uuid4().hex[:8]}"

    @staticmethod
    def _read_owner(connection: psycopg.Connection[Any], key: int) -> tuple[int, str, float] | None:
        class_id, object_id = _advisory_lock_parts(key)
        row = connection.execute(
            """
            SELECT activity.pid,
                   activity.application_name,
                   EXTRACT(EPOCH FROM (clock_timestamp() - activity.state_change))::double precision
              FROM pg_locks AS locks
              JOIN pg_stat_activity AS activity ON activity.pid = locks.pid
             WHERE locks.locktype = 'advisory'
               AND locks.granted
               AND locks.objsubid = 1
               AND locks.classid = %s
               AND locks.objid = %s
            """,
            (class_id, object_id),
        ).fetchone()
        if row is None:
            return None
        return int(row[0]), str(row[1]), float(row[2])

    def _forget_connection(self, connection: psycopg.Connection[Any]) -> None:
        self._connection = None
        self._key = None
        self._fencing_epoch = None
        self._grant_signing_secret = None
        try:
            connection.close()
        except (psycopg.Error, OSError):
            pass


__all__ = [
    "ActiveDaemonLease",
    "DaemonLeaseError",
    "DaemonLeaseStatus",
    "FreshLeaseOwnerError",
    "LeaseConnectionLostError",
    "RecoveredLeaseOwner",
    "UnverifiedLeaseOwnerError",
]
