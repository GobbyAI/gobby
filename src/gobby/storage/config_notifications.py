"""Pool-exempt PostgreSQL configuration revision notifications."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Protocol

from gobby.storage.config_repository import MAX_CONFIG_REVISION


class Notification(Protocol):
    @property
    def payload(self) -> str: ...


class NotificationConnection(Protocol):
    autocommit: bool

    async def execute(self, command: str) -> object: ...

    def notifies(
        self,
        *,
        timeout: float | None = None,
        stop_after: int | None = None,
    ) -> AsyncIterator[Notification]: ...

    async def close(self) -> None: ...


ConnectionFactory = Callable[[], Awaitable[NotificationConnection]]


class ConfigNotificationListener:
    """Own one autocommit LISTEN connection for configuration revisions."""

    CHANNEL = "gobby_config_changed"

    def __init__(self, connection_factory: ConnectionFactory) -> None:
        self._connection_factory = connection_factory
        self._connection: NotificationConnection | None = None

    async def connect(self) -> None:
        """Connect and acknowledge an active autocommit subscription."""
        await self.close()
        connection = await self._connection_factory()
        if not connection.autocommit:
            await connection.close()
            raise RuntimeError("Configuration LISTEN connection must use autocommit")
        try:
            await connection.execute(f"LISTEN {self.CHANNEL}")
        except BaseException:
            await connection.close()
            raise
        self._connection = connection

    async def revisions(self) -> AsyncIterator[int]:
        connection = self._connection
        if connection is None:
            raise RuntimeError("Configuration listener is not connected")
        async for notification in connection.notifies():
            try:
                revision = int(notification.payload)
            except ValueError:
                continue
            if revision >= 0:
                yield min(revision, MAX_CONFIG_REVISION)

    async def close(self) -> None:
        connection = self._connection
        self._connection = None
        if connection is not None:
            await connection.close()


__all__ = ["ConfigNotificationListener", "ConnectionFactory"]
