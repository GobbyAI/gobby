"""Pool-exempt LISTEN/poll service for definition-revision invalidation."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from contextlib import suppress
from typing import cast

from gobby.storage.config_notifications import ConnectionFactory, NotificationConnection
from gobby.storage.definitions.revisions import (
    DEFINITION_DOMAINS,
    NOTIFY_CHANNEL,
    DefinitionDomain,
    bump_definitions_revision,
)

logger = logging.getLogger(__name__)


class DefinitionRevisionListener:
    """Own one autocommit LISTEN connection and a poll-healing loop."""

    CHANNEL = NOTIFY_CHANNEL

    def __init__(
        self,
        connection_factory: ConnectionFactory,
        *,
        fetch_revisions: Callable[[], Mapping[DefinitionDomain, int]],
        poll_interval: float = 30.0,
        reconnect_backoff: float = 0.1,
    ) -> None:
        self._connection_factory = connection_factory
        self._fetch_revisions = fetch_revisions
        self._poll_interval = poll_interval
        self._reconnect_backoff = reconnect_backoff
        self._connection: NotificationConnection | None = None
        self._observed: dict[DefinitionDomain, int] = dict.fromkeys(DEFINITION_DOMAINS, 0)
        self._listen_task: asyncio.Task[None] | None = None
        self._poll_task: asyncio.Task[None] | None = None
        self._reconnect_lock: asyncio.Lock | None = None
        self._closed = False
        self._started = False
        self._healthy = True

    @property
    def listen_task(self) -> asyncio.Task[None] | None:
        return self._listen_task

    @property
    def poll_task(self) -> asyncio.Task[None] | None:
        return self._poll_task

    async def start(self) -> None:
        if self._closed:
            raise RuntimeError("DefinitionRevisionListener is closed")
        if self._started:
            return
        self._seed_observed()
        await self._connect()
        self._reconnect_lock = asyncio.Lock()
        self._started = True
        self._healthy = True
        self._listen_task = asyncio.create_task(
            self._listen(),
            name="definition-revision-listener",
        )
        self._poll_task = asyncio.create_task(
            self._poll(),
            name="definition-revision-poll",
        )

    def _seed_observed(self) -> None:
        seeded = dict.fromkeys(DEFINITION_DOMAINS, 0)
        for domain, revision in self._fetch_revisions().items():
            if domain in DEFINITION_DOMAINS:
                seeded[domain] = int(revision)
        self._observed = seeded

    async def _connect(self) -> None:
        await self._close_connection()
        connection = await self._connection_factory()
        if not connection.autocommit:
            await connection.close()
            raise RuntimeError("Definition revision LISTEN connection must use autocommit")
        try:
            await connection.execute(f"LISTEN {self.CHANNEL}")
        except BaseException:
            await connection.close()
            raise
        self._connection = connection

    async def _close_connection(self) -> None:
        connection = self._connection
        self._connection = None
        if connection is not None:
            with suppress(Exception):
                await connection.close()

    def _apply_observed(self, domain: str, revision: int) -> None:
        if domain not in DEFINITION_DOMAINS:
            logger.debug("Ignoring unknown definition revision domain %r", domain)
            return
        checked = cast(DefinitionDomain, domain)
        last = self._observed.get(checked, 0)
        if revision <= last:
            return
        self._observed[checked] = revision
        bump_definitions_revision(checked)

    def _handle_payload(self, payload: str) -> None:
        domain, separator, revision_text = payload.partition(":")
        if not separator:
            logger.debug("Ignoring invalid definition revision payload %r", payload)
            return
        try:
            revision = int(revision_text)
        except ValueError:
            logger.debug("Ignoring invalid definition revision payload %r", payload)
            return
        if revision < 0:
            logger.debug("Ignoring invalid definition revision payload %r", payload)
            return
        self._apply_observed(domain, revision)

    async def _listen(self) -> None:
        while not self._closed:
            try:
                connection = self._connection
                if connection is None:
                    raise RuntimeError("Definition revision listener is not connected")
                async for notification in connection.notifies():
                    if self._closed:
                        return
                    self._handle_payload(notification.payload)
                raise ConnectionError("Definition revision notification stream ended")
            except asyncio.CancelledError:
                raise
            except Exception:
                self._healthy = False
                logger.warning("Definition revision listener disconnected", exc_info=True)
                await self._reconnect()

    async def _poll(self) -> None:
        while not self._closed:
            await asyncio.sleep(self._poll_interval)
            if self._closed:
                return
            try:
                persistent = self._fetch_revisions()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("Definition revision poll failed", exc_info=True)
                continue
            for domain, revision in persistent.items():
                self._apply_observed(str(domain), int(revision))

    async def _reconnect(self) -> None:
        lock = self._reconnect_lock
        if lock is None:
            return
        async with lock:
            if self._healthy or self._closed:
                return
            while not self._closed:
                await self._close_connection()
                await asyncio.sleep(self._reconnect_backoff)
                try:
                    await self._connect()
                    self._healthy = True
                    return
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning(
                        "Definition revision listener reconnect failed",
                        exc_info=True,
                    )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._healthy = False
        for task in (self._listen_task, self._poll_task):
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        self._listen_task = None
        self._poll_task = None
        await self._close_connection()
        self._started = False


__all__ = ["DefinitionRevisionListener"]
