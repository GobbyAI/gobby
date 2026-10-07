"""Backend-neutral terminal WebSocket handlers."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any, Literal

# WRITE_FAULT_NAME is re-exported: e2e fixtures import it from this module.
from gobby.servers.websocket.terminal_ws_write import WRITE_FAULT_NAME as WRITE_FAULT_NAME
from gobby.servers.websocket.terminal_ws_write import TerminalWriteMixin
from gobby.storage.projects import GLOBAL_PROJECT_ID
from gobby.storage.terminals import AttachLocator, HostEpochMismatchError, TerminalNotReadyError
from gobby.terminals.foreground import (
    foreground_commands,
    process_shell,
    shell_cwds,
    shell_pid,
)
from gobby.terminals.frame_client import FrameProtocolError, encode_frame
from gobby.terminals.leases import (
    LifecyclePublicationError,
    SizingDecision,
    TerminalLeaseRegistry,
)
from gobby.terminals.runtime import UnregisteredBackendError
from gobby.terminals.ws_protocol import (
    TERMINAL_LIST_DEFAULT_PAGE_SIZE,
    TERMINAL_LIST_MAX_PAGE_SIZE,
    TERMINAL_WS_LIFECYCLE_SEND_TIMEOUT_S,
    TerminalPageTooLargeError,
    encode_page,
    inventory_item,
    parse_list_cursor,
)
from gobby.utils.json_helpers import json_dumps
from gobby.utils.machine_id import require_machine_id

logger = logging.getLogger(__name__)

_LIST_STATES = frozenset({"pending", "live", "exited", "orphaned"})
_DEFAULT_LIST_STATES = ("pending", "live")


def _list_states(raw: object) -> tuple[str, ...] | None:
    """The ``terminal_list`` states filter; ``None`` when the request's is malformed."""
    if raw is None:
        return _DEFAULT_LIST_STATES
    if not isinstance(raw, list) or not raw:
        return None
    if any(not isinstance(state, str) or state not in _LIST_STATES for state in raw):
        return None
    return tuple(dict.fromkeys(raw))


# The gterm host capability for `SetTerminalTheme` on its frame streams.
TERMINAL_THEME_CAPABILITY = "terminal_theme"

# Clients reconnect the moment HTTP serves, before the gterm host is adopted or
# spawned; an attach waits this long for that decision (#22002). With the two
# host budgets below it stays under gclient's 5s request deadline, so a slow
# host becomes a typed refusal the client shows and retries rather than a
# timeout it cannot name (#22544).
HOST_STARTUP_ATTACH_WAIT_SECONDS = 2.5

# Opening the host's frame socket and the frame handshake each get this long.
# Both are local I/O that finishes in milliseconds on a healthy host, and a
# host that does not answer holds this connection's serial dispatch, so the
# attach gives up and names the step that stalled.
PROXY_FRAME_OPEN_SECONDS = 1.0
PROXY_START_SECONDS = 1.0

PROXY_ATTACH_FAILURE_REASONS: dict[str, str] = {
    "terminal_exited": "terminal row is exited or orphaned; nothing to attach",
    "host_epoch_stale": "terminal belongs to an earlier gterm host incarnation",
    "host_not_ready": "terminal host or terminal spawn is not ready",
    "runtime_unavailable": "no terminal runtime for backend",
    "proxy_unavailable": "proxy frame opener is not available",
    "locator_failed": "attach_locator raised",
    "locator_invalid": "attach_locator did not return an AttachLocator",
    "host_unavailable": "opening the proxy frame connection failed",
    "frame_invalid": "proxy frame opener returned an unusable frame",
    "proxy_start_failed": "proxy frame handshake or relay start failed",
    "host_open_timeout": "opening the proxy frame connection did not finish in time",
    "proxy_start_timeout": "proxy frame handshake did not finish in time",
}


def _log_proxy_attach_failure(terminal_id: str, code: str, *, exc_info: bool = False) -> str:
    level = logging.DEBUG if code == "terminal_exited" else logging.WARNING
    logger.log(
        level,
        "proxy attach failed terminal_id=%s code=%s reason=%s",
        terminal_id,
        code,
        PROXY_ATTACH_FAILURE_REASONS[code],
        exc_info=exc_info,
    )
    return code


async def _close_frame_quietly(frame: Any) -> None:
    closer = getattr(frame, "close", None)
    if not callable(closer):
        return
    try:
        result = closer()
        if asyncio.iscoroutine(result):
            await result
    except Exception:
        logger.debug("closing failed proxy frame raised", exc_info=True)


class TerminalWsMixin(TerminalWriteMixin):
    """Observe-only attach, lease-gated writes, and inventory."""

    clients: dict[Any, dict[str, Any]]
    lease_registry: TerminalLeaseRegistry
    terminal_manager: Any
    write_coordinator: Any
    terminal_runtime_registry: Any
    terminal_services: Any | None = None
    terminal_host_manager: Any | None = None
    open_proxy_frame: Any | None = None
    if TYPE_CHECKING:

        async def broadcast_tmux_session_event(
            self,
            event: str,
            terminal_id: str = "",
            session_name: str | None = None,
            socket: str | None = None,
            terminal: dict[str, Any] | None = None,
        ) -> None: ...

        async def _apply_terminal_sizing(
            self, terminal_id: str, sizing: SizingDecision | None
        ) -> None: ...

    async def _send_json(self, websocket: Any, payload: dict[str, Any]) -> None:
        await websocket.send(json_dumps(payload))

    async def _handle_terminal_attach(self, websocket: Any, data: dict[str, Any]) -> None:
        started = time.monotonic()

        def log_slow(
            outcome: str,
            logged_terminal_id: str | None,
            row_done: float,
            lease_done: float,
            transport_done: float,
        ) -> None:
            completed = time.monotonic()
            if completed - started < 1.0:
                return
            client_id = getattr(self, "clients", {}).get(websocket, {}).get("id")
            logger.warning(
                "Slow terminal attach | client_id=%s terminal_id=%s outcome=%s total_ms=%.1f "
                "row_ms=%.1f lease_ms=%.1f transport_ms=%.1f reply_ms=%.1f",
                client_id,
                logged_terminal_id,
                outcome,
                (completed - started) * 1000,
                (row_done - started) * 1000,
                (lease_done - row_done) * 1000,
                (transport_done - lease_done) * 1000,
                (completed - transport_done) * 1000,
            )

        request_id = data.get("request_id")
        terminal_id = data.get("terminal_id")
        delivery = data.get("frame_delivery") or "proxy"
        encoding = data.get("encoding", "terminal_ansi")
        if encoding not in {"terminal_ansi", "semantic_frame"}:
            await self._send_json(
                websocket,
                {
                    "type": "terminal_error",
                    "request_id": request_id,
                    "code": "invalid_encoding",
                },
            )
            log_slow("invalid_encoding", None, started, started, started)
            return
        if not isinstance(terminal_id, str) or not terminal_id:
            await self._send_json(
                websocket,
                {
                    "type": "terminal_attach_result",
                    "request_id": request_id,
                    "success": False,
                    "code": "terminal_gone",
                    "reason": "terminal row is unavailable",
                    "terminal_id": terminal_id,
                },
            )
            log_slow("terminal_gone", None, started, started, started)
            return
        manager = getattr(self, "terminal_manager", None)
        row = None if manager is None else await asyncio.to_thread(manager.get, terminal_id)
        row_loaded = time.monotonic()
        if row is None:
            await self._send_json(
                websocket,
                {
                    "type": "terminal_attach_result",
                    "request_id": request_id,
                    "success": False,
                    "code": "terminal_gone",
                    "reason": "terminal row is unavailable",
                    "terminal_id": terminal_id,
                },
            )
            log_slow("terminal_gone", None, row_loaded, row_loaded, row_loaded)
            return
        # Any live backend attaches; one with no registered runtime is refused
        # later as runtime_unavailable, once the lease is taken.
        if row.state in {"exited", "orphaned"}:
            code = f"terminal_{row.state}"
            await self._send_json(
                websocket,
                {
                    "type": "terminal_attach_result",
                    "request_id": request_id,
                    "success": False,
                    "code": code,
                    "reason": "terminal row is exited or orphaned; nothing to attach",
                    "terminal_id": terminal_id,
                },
            )
            log_slow(code, row.id, row_loaded, row_loaded, row_loaded)
            return
        registry = self._leases()
        viewer: Literal["web", "gclient"] = "web" if data.get("viewer") == "web" else "gclient"
        record = await registry.attach(
            terminal_id,
            str(delivery),
            websocket=websocket,
            viewer=viewer,
            backend=str(row.backend),
            terminal=row,
        )
        lease_acquired = time.monotonic()
        locator: AttachLocator | None = None
        if str(delivery) == "direct":
            locator, failure = await self._resolve_attach_locator(row)
            if (
                failure is None
                and locator is not None
                and not locator.is_valid_for_direct(row.backend)
            ):
                failure = _log_proxy_attach_failure(row.id, "locator_invalid")
        else:
            failure = await self._start_proxy_attach(websocket, row, record, encoding)
        transport_ready = time.monotonic()
        if failure is not None:
            await registry.finalize(record.attachment_id, failure)
            await self._send_json(
                websocket,
                {
                    "type": "terminal_attach_result",
                    "request_id": request_id,
                    "terminal_id": terminal_id,
                    "attachment_id": record.attachment_id,
                    "success": False,
                    "code": failure,
                    "reason": PROXY_ATTACH_FAILURE_REASONS[failure],
                },
            )
            log_slow(failure, row.id, row_loaded, lease_acquired, transport_ready)
            return
        await self._send_json(
            websocket,
            {
                "type": "terminal_attach_result",
                "request_id": request_id,
                "terminal_id": terminal_id,
                "attachment_id": record.attachment_id,
                "rows": row.rows or 24,
                "cols": row.cols or 80,
                "backend": row.backend,
                "frame_delivery": record.frame_delivery,
                "direct": None if locator is None else locator.direct_block(),
                # What the frame host behind `direct` accepts beyond the base
                # protocol; a direct client sends nothing else on that stream.
                # A proxied native pane gets what the daemon relays for it
                # (`terminal_set_theme`).
                "host_capabilities": (
                    list(getattr(self.terminal_host_manager, "capabilities", ()))
                    if locator is not None
                    else self._relayed_host_capabilities(row.backend)
                ),
                "lease_generation": registry.generation(terminal_id),
                "lease_holder": registry.holder_info(terminal_id),
                "success": True,
            },
        )
        self._proxy().start_pump(record.attachment_id)
        log_slow("success", row.id, row_loaded, lease_acquired, transport_ready)

    async def _handle_terminal_detach(self, websocket: Any, data: dict[str, Any]) -> None:
        attachment_id = data.get("attachment_id")
        terminal_id = data.get("terminal_id")
        if isinstance(attachment_id, str):
            if attachment_id in self._proxy().attachments:
                await self._proxy().finalize_attachment(attachment_id, "detach")
            else:
                event = await self._leases().finalize(attachment_id, "detach")
                if event is not None:
                    await self._apply_terminal_sizing(event.terminal_id, event.sizing)
                    payload = {
                        "type": "terminal_attachment_finalized",
                        "terminal_id": event.terminal_id,
                        "attachment_id": event.attachment_id,
                        "reason": event.reason,
                        "lease_generation": event.lease_generation,
                    }

                    async def publish(stamped: dict[str, Any]) -> None:
                        await self._send_json(websocket, stamped)

                    await self._leases().publish_lifecycle(payload, publish)
        await self._send_json(
            websocket,
            {
                "type": "terminal_detach_result",
                "success": True,
                "terminal_id": terminal_id,
                "attachment_id": attachment_id,
                "request_id": data.get("request_id"),
            },
        )

    async def _handle_terminal_list(self, websocket: Any, data: dict[str, Any]) -> None:
        """Every pending or live terminal on this machine.

        A ``project_id`` (sent by the web project picker) narrows the page to
        that project plus terminals that belong to no project, which live
        under the global project. Without one the whole machine is listed.
        """
        started = time.monotonic()
        request_id = data.get("request_id")
        try:
            cursor_created_at, cursor_id = parse_list_cursor(data.get("cursor"))
        except ValueError:
            await self._send_json(
                websocket,
                {"type": "terminal_error", "code": "invalid_cursor", "request_id": request_id},
            )
            return
        snapshot = (
            self._leases().lifecycle_snapshot()
            if cursor_created_at is None and cursor_id is None
            else None
        )
        envelope = {"type": "terminal_list", "request_id": request_id}
        manager = getattr(self, "terminal_manager", None)
        if manager is None:
            await self._send_json(
                websocket,
                encode_page([], None, snapshot=snapshot, envelope=envelope),
            )
            return
        project_id = data.get("project_id") or self._project_id(websocket)
        if not isinstance(project_id, str):
            project_id = None
        limit = data.get("limit", TERMINAL_LIST_DEFAULT_PAGE_SIZE)
        if not isinstance(limit, int) or isinstance(limit, bool):
            limit = TERMINAL_LIST_DEFAULT_PAGE_SIZE
        limit = max(1, min(limit, TERMINAL_LIST_MAX_PAGE_SIZE))
        states = _list_states(data.get("states"))
        if states is None:
            await self._send_json(
                websocket,
                {"type": "terminal_error", "code": "invalid_states", "request_id": request_id},
            )
            return
        machine_id = require_machine_id()
        # A slow page query runs off the loop so it does not delay other clients.
        items, has_more = await asyncio.to_thread(
            manager.list_page,
            None if project_id is None else [project_id, GLOBAL_PROJECT_ID],
            machine_id=machine_id,
            states=states,
            cursor_created_at=cursor_created_at,
            cursor_id=cursor_id,
            limit=limit,
        )
        listed = time.monotonic()
        shell_pids = {row.id: pid for row in items if (pid := shell_pid(row)) is not None}
        native_commands = await asyncio.to_thread(foreground_commands, shell_pids)
        commands_read = time.monotonic()
        native_cwds = await asyncio.to_thread(shell_cwds, shell_pids)
        cwds_read = time.monotonic()
        serialized = []
        for row in items:
            item = inventory_item(row, lease_holder=self._leases().holder_info(row.id))
            item["command"] = native_commands.get(row.id) or process_shell(row)
            item["cwd"] = native_cwds.get(row.id)
            serialized.append(item)
        item_cursors = [f"{row.created_at.isoformat()}|{row.id}" for row in items]
        next_cursor = None if not has_more else item_cursors[-1]
        try:
            payload = encode_page(
                serialized,
                next_cursor,
                snapshot=snapshot,
                item_cursors=item_cursors,
                envelope=envelope,
            )
        except TerminalPageTooLargeError:
            await self._send_json(
                websocket,
                {
                    "type": "terminal_error",
                    "code": "terminal_page_too_large",
                    "request_id": request_id,
                },
            )
            return
        encoded = time.monotonic()
        await self._send_json(websocket, payload)
        sent = time.monotonic()
        if sent - started >= 1.0:
            client_id = getattr(self, "clients", {}).get(websocket, {}).get("id")
            logger.warning(
                "Slow terminal list | client_id=%s count=%d total_ms=%.1f "
                "page_ms=%.1f commands_ms=%.1f cwds_ms=%.1f encode_ms=%.1f send_ms=%.1f",
                client_id,
                len(items),
                (sent - started) * 1000,
                (listed - started) * 1000,
                (commands_read - listed) * 1000,
                (cwds_read - commands_read) * 1000,
                (encoded - cwds_read) * 1000,
                (sent - encoded) * 1000,
            )

    def _relayed_host_capabilities(self, backend: str) -> list[str]:
        host_capabilities = getattr(self.terminal_host_manager, "capabilities", ())
        if backend == "native" and TERMINAL_THEME_CAPABILITY in host_capabilities:
            return [TERMINAL_THEME_CAPABILITY]
        return []

    async def _handle_terminal_set_theme(self, websocket: Any, data: dict[str, Any]) -> None:
        """Declare a proxied pane's terminal theme on its host frame stream.

        Only the websocket that owns the attachment may declare for it, and
        only while that attachment holds the writer lease or nobody does. The
        gterm input grant goes to direct holders alone, so the host treats a
        proxied holder's pane as ungranted and would apply any stream's
        theme; this lease check is what keeps an observer from recolouring
        it. A refused theme is remembered and declared when the attachment
        takes control (``_declare_holder_theme``).
        """
        attachment_id = data.get("attachment_id")
        record = (
            self._proxy().attachments.get(attachment_id) if isinstance(attachment_id, str) else None
        )
        declare = getattr(getattr(record, "frame", None), "declare_terminal_theme", None)
        theme = data.get("theme")
        code = None
        if (
            record is None
            or record.websocket is not websocket
            or not callable(declare)
            or not self._relayed_host_capabilities(record.backend)
        ):
            code = "theme_not_relayed"
        else:
            try:
                encode_frame({"type": "set_terminal_theme", "theme": theme})
            except FrameProtocolError:
                code = "invalid_terminal_theme"
            else:
                record.theme = theme
                if self._leases().holder(record.terminal_id) in (None, attachment_id):
                    await declare(attachment_id, theme)
                else:
                    code = "theme_not_relayed"
        if code is not None:
            # No attachment_id: gclient routes an attachment's terminal_error
            # as the answer to its pending take/release control request.
            await self._send_json(
                websocket,
                {"type": "terminal_error", "code": code, "terminal_id": data.get("terminal_id")},
            )

    async def _declare_holder_theme(self, attachment_id: str) -> None:
        """Declare the theme a proxied attachment sent before it took control.

        gclient sends a pane's theme once, so an observer refused while
        another attachment held the lease would otherwise keep that holder's
        colours after taking over.
        """
        record = self._proxy().attachments.get(attachment_id)
        declare = getattr(getattr(record, "frame", None), "declare_terminal_theme", None)
        if record is not None and record.theme is not None and callable(declare):
            await declare(attachment_id, record.theme)

    async def _handle_terminal_set_scroll_offset(
        self, websocket: Any, data: dict[str, Any]
    ) -> None:
        attachment_id = data.get("attachment_id")
        if not isinstance(attachment_id, str):
            return
        requested = int(data.get("rows_from_live_edge") or 0)
        max_rows = int(data.get("max_rows") or 0)
        if max_rows <= 0:
            max_rows = requested
        applied = self._leases().set_scroll_offset(attachment_id, requested, max_rows)
        frame = self._proxy().frame_for(attachment_id)
        setter = getattr(frame, "set_scroll_offset", None)
        # The attach snapshot carries the backend, so a scroll event never
        # reads the terminals row.
        record = self._leases().get(attachment_id)
        row = None if record is None else record.terminal
        if callable(setter) and (row is None or row.backend == "native"):
            await setter(applied.applied_rows)
        await self._send_json(
            websocket,
            {
                "type": "terminal_scroll_offset_applied",
                "terminal_id": data.get("terminal_id"),
                "attachment_id": attachment_id,
                "applied_rows": applied.applied_rows,
                "max_rows": applied.max_rows,
            },
        )

    async def _fanout_lease_lost(
        self,
        previous: str,
        holder: str,
        generation: int,
        *,
        requester: Any | None = None,
    ) -> None:
        message = {
            "type": "terminal_lease_lost",
            "attachment_id": previous,
            "holder": holder,
            "lease_generation": generation,
        }

        async def publish(stamped: dict[str, Any]) -> None:
            recipients = list(self.clients)
            if requester is not None and requester not in self.clients:
                recipients.append(requester)
            raw = json_dumps(stamped)

            async def send_one(recipient: Any) -> None:
                try:
                    await asyncio.wait_for(
                        recipient.send(raw), timeout=TERMINAL_WS_LIFECYCLE_SEND_TIMEOUT_S
                    )
                except Exception:
                    logger.debug("lease_lost fanout failed", exc_info=True)

            await asyncio.gather(*(send_one(recipient) for recipient in recipients))

        await self._leases().publish_lifecycle(message, publish)

    async def _send_control(self, websocket: Any, payload: dict[str, Any]) -> None:
        hub = self._proxy()
        if websocket in hub.relays or websocket in hub.by_socket:
            try:
                await hub.emit_lifecycle(websocket, payload)
            except LifecyclePublicationError:
                logger.debug("terminal control lifecycle send failed", exc_info=True)
            await asyncio.sleep(0)
            return
        await self._send_json(websocket, payload)

    def _proxy(self) -> Any:
        hub = getattr(self, "_proxy_hub", None)
        if hub is None:
            from gobby.servers.websocket.proxy_relay import ProxyHub

            hub = ProxyHub(self)
            self._proxy_hub = hub
        return hub

    async def _cleanup_terminal_client(self, websocket: Any) -> None:
        reason = (
            "daemon_shutdown"
            if getattr(self, "_terminal_shutdown_in_progress", False)
            else "ws_close"
        )
        events = await self._leases().finalize_websocket(websocket, reason)
        for event in events:
            await self._apply_terminal_sizing(event.terminal_id, event.sizing)
        hub = getattr(self, "_proxy_hub", None)
        if hub is not None:
            await hub.drop_socket(websocket, "ws_close")

    async def _cleanup_terminals(self) -> None:
        hub = getattr(self, "_proxy_hub", None)
        if hub is not None:
            for websocket in list(hub.relays):
                await hub.drop_socket(websocket, "ws_close")

    def _runtime_for(self, backend: str) -> Any | None:
        registry = getattr(self, "terminal_runtime_registry", None)
        if registry is None:
            return None
        resolve = getattr(registry, "resolve", None)
        if not callable(resolve):
            return None
        try:
            runtime = resolve(backend)
        except UnregisteredBackendError:
            return None
        return runtime

    async def _resolve_attach_locator(self, row: Any) -> tuple[AttachLocator | None, str | None]:
        if row.state in {"exited", "orphaned"}:
            return None, _log_proxy_attach_failure(row.id, "terminal_exited")
        host = self.terminal_host_manager
        if host is not None and not await host.wait_startup_settled(
            HOST_STARTUP_ATTACH_WAIT_SECONDS
        ):
            return None, _log_proxy_attach_failure(row.id, "host_not_ready")
        runtime = self._runtime_for(row.backend)
        if runtime is None:
            return None, _log_proxy_attach_failure(row.id, "runtime_unavailable")
        try:
            locator = await runtime.attach_locator(row)
        except TerminalNotReadyError:
            return None, _log_proxy_attach_failure(row.id, "host_not_ready")
        except HostEpochMismatchError:
            return None, _log_proxy_attach_failure(row.id, "host_epoch_stale")
        except Exception:
            return None, _log_proxy_attach_failure(row.id, "locator_failed", exc_info=True)
        if not isinstance(locator, AttachLocator):
            return None, _log_proxy_attach_failure(row.id, "locator_invalid")
        return locator, None

    async def _start_proxy_attach(
        self, websocket: Any, row: Any, record: Any, encoding: str
    ) -> str | None:
        locator, failure = await self._resolve_attach_locator(row)
        if failure is not None or locator is None:
            return failure
        opener = getattr(self, "open_proxy_frame", None)
        if not callable(opener):
            return _log_proxy_attach_failure(row.id, "proxy_unavailable")
        try:
            frame = await asyncio.wait_for(opener(locator), PROXY_FRAME_OPEN_SECONDS)
        except TimeoutError:
            return _log_proxy_attach_failure(row.id, "host_open_timeout")
        except Exception:
            return _log_proxy_attach_failure(row.id, "host_unavailable", exc_info=True)
        if not frame or not callable(getattr(frame, "read_message", None)):
            # The relay pump requires read_message; anything else dies after
            # the client was already told the attach succeeded.
            if frame is not None:
                await _close_frame_quietly(frame)
            return _log_proxy_attach_failure(row.id, "frame_invalid")
        handed_off = False
        try:
            await asyncio.wait_for(
                self._proxy().start_proxy(
                    websocket,
                    terminal_id=row.id,
                    attachment_id=record.attachment_id,
                    locator=locator,
                    frame=frame,
                    encoding=encoding,
                    terminal=row,
                ),
                PROXY_START_SECONDS,
            )
            handed_off = True
        except TimeoutError:
            return _log_proxy_attach_failure(row.id, "proxy_start_timeout")
        except asyncio.CancelledError:
            raise
        except Exception:
            return _log_proxy_attach_failure(row.id, "proxy_start_failed", exc_info=True)
        finally:
            if not handed_off:
                await asyncio.shield(_close_frame_quietly(frame))
        return None

    def _leases(self) -> TerminalLeaseRegistry:
        registry = getattr(self, "lease_registry", None)
        if not isinstance(registry, TerminalLeaseRegistry):
            raise RuntimeError("terminal lease registry is not configured")
        return registry

    def _project_id(self, websocket: Any) -> str | None:
        meta = self.clients.get(websocket) or {}
        value = meta.get("project_id")
        return value if isinstance(value, str) else None
