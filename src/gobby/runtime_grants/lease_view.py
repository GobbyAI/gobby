"""Read-only projection of the native owner's deployment signing authority."""

from __future__ import annotations

import threading
from typing import Any

import psycopg

_current: RuntimeLeaseView | None = None
_lock = threading.Lock()


def current_lease() -> RuntimeLeaseView | None:
    with _lock:
        return _current


class RuntimeLeaseView:
    """The backend selects authority; only gdaemon acquires and rotates it."""

    def __init__(self, database_url: str, *, machine_id: str, deployment_token: str) -> None:
        self.database_url = database_url
        self.machine_id = machine_id
        self.deployment_token = deployment_token
        self.fencing_epoch: int | None = None
        self.grant_signing_secret: str | None = None
        self.acquired = False

    def bind(self) -> None:
        global _current
        connection: psycopg.Connection[tuple[Any, ...]] = psycopg.connect(
            self.database_url, autocommit=True
        )
        with connection:
            row = connection.execute(
                "SELECT fencing_epoch, grant_signing_secret FROM deployment_runtime "
                "WHERE deployment_token = %s",
                (self.deployment_token,),
            ).fetchone()
        if row is None or not row[1]:
            raise RuntimeError("native deployment runtime authority is unavailable")
        self.fencing_epoch = int(row[0])
        self.grant_signing_secret = str(row[1])
        with _lock:
            self.acquired = True
            _current = self

    def owns_live_lease(self) -> bool:
        return self.acquired

    def is_live(self) -> bool:
        return self.acquired

    def release(self) -> None:
        global _current
        with _lock:
            self.acquired = False
            self.fencing_epoch = None
            self.grant_signing_secret = None
            if _current is self:
                _current = None
