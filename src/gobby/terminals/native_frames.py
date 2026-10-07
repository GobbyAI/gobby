"""Native terminal frame streams and observer reservations."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from gobby.storage.terminals import AttachLocator
from gobby.terminals.frame_client import FrameClient
from gobby.terminals.host_client import HostCommandError, HostUnavailableError
from gobby.terminals.host_protocol import frames_socket_path
from gobby.terminals.runtime import PreparedSpawn
from gobby.utils.local_token import read_local_api_token


def _mark_host_error_stage(exc: HostCommandError, stage: str) -> None:
    if exc.stage is None:
        exc.stage = stage


class NativeFrameStreamMixin:
    """Manage streams using state owned by the native terminal runtime."""

    _client: Any
    _frame_client: Any | None
    _observer_streams: dict[str, FrameClient]
    _subscribed: bool

    if TYPE_CHECKING:

        async def _ensure(self) -> None: ...

    def _socket_dir(self) -> Path | None:
        manager = getattr(self._client, "_manager", None)
        directory = getattr(manager, "socket_dir", None)
        if directory is None:
            directory = getattr(self._client, "socket_dir", None)
        if directory is None:
            return None
        return Path(directory)

    def _frame_token(self) -> str:
        return read_local_api_token() or ""

    async def _open_frame_stream(self, locator: AttachLocator) -> FrameClient:
        epoch = locator.frame_host_epoch or str(getattr(self._client, "host_epoch", "") or "")
        directory = self._socket_dir()
        if directory is None:
            raise HostCommandError("attach_failed")
        try:
            reader, writer = await asyncio.open_unix_connection(str(frames_socket_path(directory)))
        except (OSError, ConnectionError) as exc:
            raise HostCommandError("attach_failed") from exc
        client = FrameClient(reader, writer)
        try:
            await client.handshake(
                AttachLocator(
                    backend="native",
                    frame_host_epoch=epoch,
                    host_terminal_id=locator.host_terminal_id,
                ),
                local_token=self._frame_token(),
            )
        except BaseException:
            await client.close()
            raise
        return client

    async def _bind_frames(self, locator: AttachLocator, reservation_id: str) -> None:
        """Attach the daemon observer for one terminal on a stream of its own.

        A frame stream belongs to the host that answered its handshake and to
        the terminal it last attached: the host swaps the attachment on every
        ``attach_terminal`` and closes the stream when that terminal is removed
        or its unread frames lag out. Reusing one stream across terminals
        therefore unbinds the previous observer and, after the first terminal
        exits, writes every later bind into a dead socket. Each bind gets a
        fresh stream, drained by a pump that forgets it at EOF.
        """
        if self._frame_client is not None:
            await self._frame_client.attach_terminal(locator, reservation_id=reservation_id)
            return
        key = locator.host_terminal_id or ""
        stream = await self._open_frame_stream(locator)
        try:
            await stream.attach_terminal(locator, reservation_id=reservation_id)
        except BaseException:
            await stream.close()
            raise
        previous = self._observer_streams.pop(key, None)
        self._observer_streams[key] = stream
        stream.start_pump(on_closed=lambda: self._forget_stream(key, stream))
        if previous is not None:
            await previous.close()

    def _forget_stream(self, key: str, stream: FrameClient) -> None:
        if self._observer_streams.get(key) is stream:
            del self._observer_streams[key]

    async def close_frame_streams(self) -> None:
        streams = list(self._observer_streams.values())
        self._observer_streams.clear()
        for stream in streams:
            await stream.close()

    async def reserve_observer(self, terminal_id: UUID) -> Mapping[str, str]:
        try:
            await self._ensure()
            subscribe = getattr(self._client, "subscribe_events", None)
            if callable(subscribe) and not self._subscribed:
                await subscribe()
                self._subscribed = True
            reserve_key = str(terminal_id)
            payload = await self._client.reserve_observer(str(terminal_id), reserve_key)
        except HostCommandError as exc:
            _mark_host_error_stage(exc, "reserve")
            raise
        except (ConnectionError, OSError, TimeoutError) as exc:
            raise HostUnavailableError(str(exc) or "gterm host unavailable") from exc
        return {
            "reservation_id": str(payload.get("reservation_id") or ""),
            "reserve_key": str(payload.get("reserve_key") or reserve_key),
        }

    async def release_observer(self, reservation_id: str, reserve_key: str) -> Mapping[str, Any]:
        await self._ensure()
        release = getattr(self._client, "release_observer", None)
        if not callable(release):
            return {"ok": True, "released": True}
        payload = await release(reservation_id, reserve_key)
        return payload if isinstance(payload, dict) else {"ok": True, "released": True}

    async def bind_observer(self, prepared: PreparedSpawn, reservation_id: str) -> None:
        locator = prepared.locator or AttachLocator(
            backend="native",
            frame_host_epoch=str(getattr(self._client, "host_epoch", "") or ""),
            host_terminal_id=prepared.host_terminal_id,
        )
        await self._bind_frames(locator, reservation_id)
        prepared.acknowledge_observer()
