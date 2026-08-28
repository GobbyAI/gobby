"""Daemon→browser frame relay, queues, and host-frame pump (plan 4.3)."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from gobby.storage.terminals import AttachLocator
from gobby.terminals.frame_client import FrameLagError, FrameProtocolError
from gobby.terminals.ws_protocol import (
    TERMINAL_WS_FRAME_QUEUE_BYTES,
    TERMINAL_WS_FRAME_QUEUE_ENTRIES,
    TERMINAL_WS_FRAME_SEND_TIMEOUT_S,
    TERMINAL_WS_LIFECYCLE_RESERVE_MAX_BYTES,
    TERMINAL_WS_LIFECYCLE_RESERVE_MAX_ENTRIES,
    TERMINAL_WS_LIFECYCLE_SEND_TIMEOUT_S,
    canonical_json,
    emit_proxied_event,
)

logger = logging.getLogger(__name__)

LIFECYCLE_TYPES = frozenset(
    {"terminal_attachment_finalized", "terminal_lease_lost", "terminal_control_result"}
)


@dataclass
class _Queued:
    payload: dict[str, Any]
    raw: str
    size: int


@dataclass
class SocketRelay:
    """Per-WebSocket outbound frame queue plus reserved lifecycle capacity."""

    websocket: Any
    close: Any
    frame_q: deque[_Queued] = field(default_factory=deque)
    frame_bytes: int = 0
    life_q: deque[_Queued] = field(default_factory=deque)
    life_bytes: int = 0
    closed: bool = False
    _work: asyncio.Event = field(default_factory=asyncio.Event)
    _sender: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._sender is None:
            self._sender = asyncio.create_task(self._run())

    def _pack(self, payload: dict[str, Any]) -> _Queued:
        raw_bytes = canonical_json(payload)
        return _Queued(payload=payload, raw=raw_bytes.decode("utf-8"), size=len(raw_bytes))

    def enqueue_frame(self, payload: dict[str, Any]) -> str | None:
        if self.closed:
            return "relay_overflow"
        item = self._pack(payload)
        if (
            len(self.frame_q) >= TERMINAL_WS_FRAME_QUEUE_ENTRIES
            or self.frame_bytes + item.size > TERMINAL_WS_FRAME_QUEUE_BYTES
        ):
            return "relay_overflow"
        self.frame_q.append(item)
        self.frame_bytes += item.size
        self._work.set()
        return None

    def enqueue_lifecycle(self, payload: dict[str, Any]) -> str | None:
        if self.closed:
            return "reserve_overflow"
        item = self._pack(payload)
        if (
            len(self.life_q) >= TERMINAL_WS_LIFECYCLE_RESERVE_MAX_ENTRIES
            or self.life_bytes + item.size > TERMINAL_WS_LIFECYCLE_RESERVE_MAX_BYTES
        ):
            return "reserve_overflow"
        self.life_q.append(item)
        self.life_bytes += item.size
        self._work.set()
        return None

    async def _run(self) -> None:
        try:
            while not self.closed:
                if not self.life_q and not self.frame_q:
                    self._work.clear()
                    await self._work.wait()
                    continue
                if self.life_q:
                    item = self.life_q.popleft()
                    self.life_bytes -= item.size
                    timeout = TERMINAL_WS_LIFECYCLE_SEND_TIMEOUT_S
                    fail = "reserve_overflow"
                else:
                    item = self.frame_q.popleft()
                    self.frame_bytes -= item.size
                    timeout = TERMINAL_WS_FRAME_SEND_TIMEOUT_S
                    fail = "proxy_lag"
                try:
                    await asyncio.wait_for(self.websocket.send(item.raw), timeout=timeout)
                except (TimeoutError, OSError, ConnectionError):
                    await self.shutdown(fail)
                    return
        except asyncio.CancelledError:
            return

    async def shutdown(self, reason: str) -> None:
        if self.closed:
            return
        self.closed = True
        self._work.set()
        sender = self._sender
        self._sender = None
        if sender is not None and sender is not asyncio.current_task():
            sender.cancel()
        closer = getattr(self.websocket, "close", None)
        if callable(closer):
            result = closer()
            if asyncio.iscoroutine(result):
                try:
                    await result
                except Exception:
                    logger.debug("websocket close failed", exc_info=True)
        if callable(self.close):
            await self.close(reason)


@dataclass
class ProxyAttachment:
    """One daemon host-frame attachment bound to a browser attachment_id."""

    terminal_id: str
    attachment_id: str
    websocket: Any
    frame: Any
    task: asyncio.Task[None] | None = None


class ProxyHub:
    """Owns per-socket relays and host-frame pumps."""

    def __init__(self, owner: Any) -> None:
        self._owner = owner
        self.relays: dict[Any, SocketRelay] = {}
        self.attachments: dict[str, ProxyAttachment] = {}
        self.by_socket: dict[Any, set[str]] = {}

    def relay_for(self, websocket: Any) -> SocketRelay:
        relay = self.relays.get(websocket)
        if relay is None:
            relay = SocketRelay(
                websocket=websocket, close=lambda reason: self._on_socket_fail(websocket, reason)
            )
            self.relays[websocket] = relay
            relay.start()
        return relay

    async def start_proxy(
        self,
        websocket: Any,
        *,
        terminal_id: str,
        attachment_id: str,
        locator: AttachLocator,
        frame: Any,
    ) -> None:
        handshake = getattr(frame, "handshake", None)
        if callable(handshake):
            # The browser feeds ANSI bytes to its ghostty-vt core; the host's
            # default semantic frames carry cell grids and map to empty
            # terminal_output.
            await handshake(locator, encoding="terminal_ansi")
        attach = getattr(frame, "attach_terminal", None)
        if callable(attach):
            await attach(locator, reservation_id=None)
        record = ProxyAttachment(
            terminal_id=terminal_id,
            attachment_id=attachment_id,
            websocket=websocket,
            frame=frame,
        )
        self.attachments[attachment_id] = record
        self.by_socket.setdefault(websocket, set()).add(attachment_id)
        record.task = asyncio.create_task(self._pump(record))

    def frame_for(self, attachment_id: str) -> Any | None:
        record = self.attachments.get(attachment_id)
        return None if record is None else record.frame

    async def emit_event(
        self, websocket: Any, event: dict[str, Any], *, message_seq: int
    ) -> str | None:
        try:
            messages = emit_proxied_event(event, message_seq=message_seq)
        except (ValueError, TypeError):
            logger.debug("proxy emit refused", exc_info=True)
            return None
        relay = self.relay_for(websocket)
        for message in messages:
            overflow = relay.enqueue_frame(message)
            if overflow is not None:
                await relay.shutdown(overflow)
                return overflow
            await asyncio.sleep(0)
        return None

    async def emit_lifecycle(self, websocket: Any, event: dict[str, Any]) -> None:
        relay = self.relay_for(websocket)
        overflow = relay.enqueue_lifecycle(event)
        if overflow is not None:
            await relay.shutdown(overflow)

    async def finalize_attachment(self, attachment_id: str, reason: str) -> None:
        record = self.attachments.pop(attachment_id, None)
        if record is None:
            return
        owned = self.by_socket.get(record.websocket)
        if owned is not None:
            owned.discard(attachment_id)
        task = record.task
        record.task = None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
        closer = getattr(record.frame, "close", None)
        if callable(closer):
            result = closer()
            if asyncio.iscoroutine(result):
                try:
                    await result
                except Exception:
                    logger.debug("frame close failed", exc_info=True)
        event = self._owner._leases().finalize(attachment_id, reason)
        if event is None:
            return
        payload = {
            "type": "terminal_attachment_finalized",
            "terminal_id": event.terminal_id,
            "attachment_id": event.attachment_id,
            "reason": event.reason,
            "lease_generation": event.lease_generation,
        }
        if reason in {"relay_overflow", "reserve_overflow", "ws_close"}:
            return
        await self.emit_lifecycle(record.websocket, payload)

    async def drop_socket(self, websocket: Any, reason: str) -> None:
        ids = list(self.by_socket.pop(websocket, set()))
        for attachment_id in ids:
            await self.finalize_attachment(attachment_id, reason)
        relay = self.relays.pop(websocket, None)
        if relay is not None and not relay.closed:
            relay.closed = True
            relay._work.set()
            if relay._sender is not None:
                relay._sender.cancel()

    async def _on_socket_fail(self, websocket: Any, reason: str) -> None:
        ids = list(self.by_socket.get(websocket, set()))
        for attachment_id in ids:
            self._owner._leases().finalize(attachment_id, reason)
            record = self.attachments.pop(attachment_id, None)
            if record is None:
                continue
            closer = getattr(record.frame, "close", None)
            if callable(closer):
                result = closer()
                if asyncio.iscoroutine(result):
                    try:
                        await result
                    except Exception:
                        logger.debug("frame close failed", exc_info=True)
            if record.task is not None and record.task is not asyncio.current_task():
                record.task.cancel()
        self.by_socket.pop(websocket, None)
        relay = self.relays.pop(websocket, None)
        if relay is not None:
            relay.closed = True

    async def _pump(self, record: ProxyAttachment) -> None:
        try:
            while True:
                message = await record.frame.read_message()
                mapped = _map_host_frame(message, record.terminal_id, record.attachment_id)
                if mapped is None:
                    kind = message.get("type") if isinstance(message, dict) else None
                    if kind in {"error", "terminal_exited"}:
                        await self.finalize_attachment(record.attachment_id, "host_loss")
                        return
                    continue
                try:
                    seq = self._owner._leases().next_message_seq(record.attachment_id)
                except Exception:
                    await self.finalize_attachment(record.attachment_id, "message_seq_overflow")
                    return
                overflow = await self.emit_event(record.websocket, mapped, message_seq=seq)
                if overflow is not None or record.attachment_id not in self.attachments:
                    return
        except FrameProtocolError:
            await self.finalize_attachment(record.attachment_id, "proxy_frame_eof")
        except FrameLagError:
            await self.finalize_attachment(record.attachment_id, "proxy_lag")
        except asyncio.CancelledError:
            return
        except Exception:
            logger.debug("proxy pump failed", exc_info=True)
            await self.finalize_attachment(record.attachment_id, "host_loss")


def _map_host_frame(
    message: dict[str, Any], terminal_id: str, attachment_id: str
) -> dict[str, Any] | None:
    kind = message.get("type")
    if kind == "attach_history":
        text = message.get("text")
        return {
            "type": "terminal_attach_history",
            "terminal_id": terminal_id,
            "attachment_id": attachment_id,
            "text": "" if not isinstance(text, str) else text,
            "truncated": bool(message.get("truncated")),
            "dropped_bytes": int(message.get("dropped_bytes") or 0),
            "total_bytes": int(message.get("total_bytes") or 0),
        }
    if kind in {"terminal", "frame"}:
        raw = message.get("bytes")
        if isinstance(raw, bytes):
            data = raw.decode("utf-8", errors="replace")
        elif isinstance(raw, str):
            data = raw
        else:
            data = ""
        return {
            "type": "terminal_output",
            "terminal_id": terminal_id,
            "attachment_id": attachment_id,
            "data": data,
        }
    if kind == "scroll_offset_applied":
        return {
            "type": "terminal_scroll_offset_applied",
            "terminal_id": terminal_id,
            "attachment_id": attachment_id,
            "applied_rows": int(message.get("applied_rows") or 0),
            "max_rows": int(message.get("max_rows") or 0),
        }
    return None
