"""PostgreSQL lifetime lease for single-active-daemon ownership."""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any

import psycopg

_LEASE_NAMESPACE = "gobby-single-active-daemon-v1"
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


class ActiveDaemonLease:
    """Own a PostgreSQL session advisory lock for the active runtime lifetime."""

    def __init__(
        self,
        database_url: str,
        *,
        machine_id: str,
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
        self.application_name = f"{_APPLICATION_PREFIX}{machine_id}:{instance}"
        self.connect_timeout_seconds = connect_timeout_seconds
        self._connection: psycopg.Connection[Any] | None = None
        self._keys: tuple[int, int] | None = None
        self._mutex = threading.RLock()

    @property
    def acquired(self) -> bool:
        """Return whether this object currently owns an open lease session."""
        with self._mutex:
            return self._connection is not None

    def try_acquire(self) -> bool:
        """Attempt the lease once, retaining the dedicated connection on success."""
        with self._mutex:
            if self._connection is not None:
                self.heartbeat()
                return True

            connection = self._connect(self.application_name)
            try:
                keys = self._resolve_keys(connection)
                row = connection.execute(
                    "SELECT pg_try_advisory_lock(%s, %s)",
                    keys,
                ).fetchone()
                acquired = bool(row and row[0])
                if not acquired:
                    connection.close()
                    return False
                self._connection = connection
                self._keys = keys
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

    def release(self) -> None:
        """Release the lease and close its dedicated PostgreSQL session."""
        with self._mutex:
            connection = self._connection
            keys = self._keys
            self._connection = None
            self._keys = None
            if connection is None:
                return
            try:
                if keys is not None and not connection.closed:
                    connection.execute("SELECT pg_advisory_unlock(%s, %s)", keys).fetchone()
            except (psycopg.Error, OSError):
                pass
            finally:
                connection.close()

    def status(self) -> DaemonLeaseStatus:
        """Inspect the exact singleton lease without mutating ownership."""
        with self._connect(self._probe_application_name()) as connection:
            keys = self._resolve_keys(connection)
            owner = self._read_owner(connection, keys)
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
            keys = self._resolve_keys(connection)
            owner = self._read_owner(connection, keys)
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
            class_id = keys[0] & 0xFFFFFFFF
            object_id = keys[1] & 0xFFFFFFFF
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
                          AND locks.objsubid = 2
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
                if self._read_owner(connection, keys) is None:
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
    def _resolve_keys(connection: psycopg.Connection[Any]) -> tuple[int, int]:
        row = connection.execute(
            "SELECT hashtext(%s), hashtext(current_database())",
            (_LEASE_NAMESPACE,),
        ).fetchone()
        if row is None:
            raise DaemonLeaseError("PostgreSQL did not return active-daemon lease keys")
        return int(row[0]), int(row[1])

    @staticmethod
    def _read_owner(
        connection: psycopg.Connection[Any], keys: tuple[int, int]
    ) -> tuple[int, str, float] | None:
        class_id = keys[0] & 0xFFFFFFFF
        object_id = keys[1] & 0xFFFFFFFF
        row = connection.execute(
            """
            SELECT activity.pid,
                   activity.application_name,
                   EXTRACT(EPOCH FROM (clock_timestamp() - activity.state_change))::double precision
              FROM pg_locks AS locks
              JOIN pg_stat_activity AS activity ON activity.pid = locks.pid
             WHERE locks.locktype = 'advisory'
               AND locks.granted
               AND locks.objsubid = 2
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
        self._keys = None
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
