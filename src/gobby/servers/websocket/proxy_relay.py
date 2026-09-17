"""Daemon→browser frame relay, queues, and host-frame pump (plan 4.3)."""

from __future__ import annotations

import asyncio
import base64
import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from gobby.config.tmux import ATTACH_HISTORY_LINES
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
    completion: asyncio.Future[None] | None = None


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
    _current: _Queued | None = None

    def start(self) -> None:
        if self._sender is None:
            self._sender = asyncio.create_task(self._run())

    def _pack(
        self,
        payload: dict[str, Any],
        completion: asyncio.Future[None] | None = None,
    ) -> _Queued:
        raw_bytes = canonical_json(payload)
        return _Queued(
            payload=payload,
            raw=raw_bytes.decode("utf-8"),
            size=len(raw_bytes),
            completion=completion,
        )

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

    def enqueue_lifecycle(
        self,
        payload: dict[str, Any],
        completion: asyncio.Future[None] | None = None,
    ) -> str | None:
        if self.closed:
            return "reserve_overflow"
        item = self._pack(payload, completion)
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
                self._current = item
                try:
                    await asyncio.wait_for(self.websocket.send(item.raw), timeout=timeout)
                except (TimeoutError, OSError, ConnectionError):
                    await self.shutdown(fail)
                    return
                except Exception:
                    logger.exception("terminal proxy relay send failed")
                    await self.shutdown(fail)
                    return
                else:
                    _settle_lifecycle(item)
                finally:
                    if self._current is item:
                        self._current = None
        except asyncio.CancelledError:
            raise

    async def shutdown(self, reason: str) -> None:
        if self.closed:
            return
        self.closed = True
        self._work.set()
        sender = self._sender
        self._sender = None
        pending_lifecycle = [item for item in [self._current, *self.life_q] if item is not None]
        if sender is not None and sender is not asyncio.current_task():
            sender.cancel()
            try:
                await sender
            except asyncio.CancelledError:
                pass
        self._current = None
        self.life_q.clear()
        self.life_bytes = 0
        self.frame_q.clear()
        self.frame_bytes = 0
        closer = getattr(self.websocket, "close", None)
        if callable(closer):
            result = closer()
            if asyncio.iscoroutine(result):
                try:
                    await result
                except Exception:
                    logger.debug("websocket close failed", exc_info=True)
        try:
            if callable(self.close):
                await self.close(reason)
        except Exception:
            logger.exception("terminal proxy relay cleanup failed")
        finally:
            error = ConnectionError(f"terminal proxy relay closed: {reason}")
            for item in pending_lifecycle:
                _settle_lifecycle(item, error)


@dataclass
class ProxyAttachment:
    """One daemon host-frame attachment bound to a browser attachment_id."""

    terminal_id: str
    attachment_id: str
    websocket: Any
    frame: Any
    encoding: str
    backend: str
    terminal: Any = None
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
        encoding: str,
        terminal: Any = None,
    ) -> None:
        handshake = getattr(frame, "handshake", None)
        if callable(handshake):
            await handshake(locator, encoding=encoding)
        attach = getattr(frame, "attach_terminal", None)
        if callable(attach):
            await attach(locator, reservation_id=None)
        record = ProxyAttachment(
            terminal_id=terminal_id,
            attachment_id=attachment_id,
            websocket=websocket,
            frame=frame,
            encoding=encoding,
            backend=locator.backend,
            terminal=terminal,
        )
        self.attachments[attachment_id] = record
        self.by_socket.setdefault(websocket, set()).add(attachment_id)

    def start_pump(self, attachment_id: str) -> None:
        """Begin relaying host frames once the client holds its attach result.

        Frames are keyed by attachment id, and the client learns that id from
        ``terminal_attach_result``, so nothing may reach the socket before the
        result does. Unknown or already pumping attachments are left alone.
        """
        record = self.attachments.get(attachment_id)
        if record is None or record.task is not None:
            return
        record.task = asyncio.create_task(self._pump(record))

    async def _emit_native_history(self, record: ProxyAttachment) -> str | None:
        """Send the history frame a native ANSI attachment never gets from the host.

        The host captures attach history only for tmux panes, but every viewer
        that renders raw bytes treats ``terminal_attach_history`` as the start of
        an attachment: the web terminal resets its buffer and leaves the attaching
        state on it, so a native row without one never renders. Native scrollback
        lives behind the host's ``snapshot`` verb instead, so the daemon reads it
        in ANSI form and sends it as the history; a host that cannot answer still
        gets a frame, marked unavailable, so the viewer attaches. It goes out
        before the first host frame so it precedes the first ``terminal_output``.
        """
        history: dict[str, Any] = {
            "type": "terminal_attach_history",
            "terminal_id": record.terminal_id,
            "attachment_id": record.attachment_id,
            "text": "",
            "truncated": False,
            "dropped_bytes": 0,
            "total_bytes": 0,
        }
        runtime = None if record.terminal is None else self._owner._runtime_for("native")
        if runtime is not None:
            host = getattr(self._owner, "terminal_host_manager", None)
            lines = getattr(host, "tmux_attach_history_lines", ATTACH_HISTORY_LINES)
            try:
                snapshot = await runtime.snapshot(record.terminal, lines=lines, mode="ansi")
            except Exception:
                logger.debug("native attach history unavailable", exc_info=True)
                history["unavailable"] = True
            else:
                history.update(
                    text=snapshot.text,
                    truncated=snapshot.truncated,
                    dropped_bytes=snapshot.dropped_bytes,
                    total_bytes=snapshot.total_bytes,
                )
        try:
            seq = self._owner._leases().next_message_seq(record.attachment_id)
        except Exception:
            await self.finalize_attachment(record.attachment_id, "message_seq_overflow")
            return "message_seq_overflow"
        return await self.emit_event(record.websocket, history, message_seq=seq)

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
        async def publish(stamped: dict[str, Any]) -> None:
            relay = self.relay_for(websocket)
            completion = asyncio.get_running_loop().create_future()
            overflow = relay.enqueue_lifecycle(stamped, completion)
            if overflow is not None:
                await relay.shutdown(overflow)
                completion.cancel()
                raise ConnectionError(f"terminal proxy relay closed: {overflow}")
            await asyncio.shield(completion)

        await self._owner._leases().publish_lifecycle(event, publish)

    async def finalize_attachment(self, attachment_id: str, reason: str) -> None:
        record = self.attachments.pop(attachment_id, None)
        if record is None:
            return
        owned = self.by_socket.get(record.websocket)
        if owned is not None:
            owned.discard(attachment_id)
        event = await self._owner._leases().finalize(attachment_id, reason)
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
        if event is None:
            return
        await self._owner._apply_terminal_sizing(event.terminal_id, event.sizing)
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
            await relay.shutdown(reason)

    async def _on_socket_fail(self, websocket: Any, reason: str) -> None:
        ids = list(self.by_socket.get(websocket, set()))
        for attachment_id in ids:
            event = await self._owner._leases().finalize(attachment_id, reason)
            if event is not None:
                await self._owner._apply_terminal_sizing(event.terminal_id, event.sizing)
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
            if record.backend == "native" and record.encoding == "terminal_ansi":
                if await self._emit_native_history(record) is not None:
                    return
            while True:
                message = await record.frame.read_message()
                mapped = _map_host_frame(
                    message,
                    record.terminal_id,
                    record.attachment_id,
                    record.encoding,
                )
                if mapped is None:
                    kind = message.get("type") if isinstance(message, dict) else None
                    if kind in {"error", "terminal_exited"}:
                        await self.finalize_attachment(record.attachment_id, "host_loss")
                        return
                    continue
                if mapped.get("type") == "terminal_output":
                    observer = getattr(self._owner, "terminal_turn_observer", None)
                    output = mapped.get("data")
                    if observer is not None and isinstance(output, str):
                        observer.observe_output(record.terminal_id, output)
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
    message: dict[str, Any], terminal_id: str, attachment_id: str, encoding: str
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
        if encoding == "semantic_frame":
            raw = message.get("raw")
            if not isinstance(raw, bytes):
                return None
            return {
                "type": "terminal_frame",
                "terminal_id": terminal_id,
                "attachment_id": attachment_id,
                "encoding": "bincode-b64",
                "payload": base64.b64encode(raw).decode("ascii"),
            }
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


def _settle_lifecycle(item: _Queued, error: Exception | None = None) -> None:
    completion = item.completion
    if completion is not None and not completion.done():
        if error is None:
            completion.set_result(None)
        else:
            completion.set_exception(error)
