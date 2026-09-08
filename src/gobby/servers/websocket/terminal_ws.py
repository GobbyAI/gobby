"""Backend-neutral terminal WebSocket handlers."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from unittest.mock import Mock

from gobby.config.terminals import TerminalConfig
from gobby.servers.websocket.terminal_input import WriteOutcome, record_turn_observation
from gobby.storage.projects import GLOBAL_PROJECT_ID
from gobby.storage.sessions import LIVE_SESSION_STATUS_ORDER
from gobby.storage.terminals import AttachLocator
from gobby.terminals.dimensions import InvalidTerminalDimensionsError, validate_dimensions
from gobby.terminals.leases import (
    LifecyclePublicationError,
    SizingDecision,
    TerminalLeaseRegistry,
    paste_oversize,
)
from gobby.terminals.runtime import Delivered, IndeterminateWrite, TerminalWriteError
from gobby.terminals.tmux_discovery import pane_owners, sweep_tmux_terminals
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

WRITE_FAULT_NAME = "terminal_write_fault"

PROXY_ATTACH_FAILURE_REASONS: dict[str, str] = {
    "runtime_unavailable": "no terminal runtime for backend",
    "proxy_unavailable": "proxy frame opener is not available",
    "locator_failed": "attach_locator raised",
    "locator_invalid": "attach_locator did not return an AttachLocator",
    "host_unavailable": "opening the proxy frame connection failed",
    "frame_invalid": "proxy frame opener returned an unusable frame",
    "proxy_start_failed": "proxy frame handshake or relay start failed",
}


def _log_proxy_attach_failure(terminal_id: str, code: str, *, exc_info: bool = False) -> str:
    logger.warning(
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


def write_handler_faulted() -> bool:
    """True when the isolated-daemon write-handler fault file is present."""
    home = os.environ.get("GOBBY_HOME")
    if not home:
        return False
    return Path(home).joinpath(WRITE_FAULT_NAME).is_file()


class TerminalWsMixin:
    """Observe-only attach, lease-gated writes, and inventory."""

    clients: dict[Any, dict[str, Any]]
    lease_registry: TerminalLeaseRegistry
    terminal_manager: Any
    write_coordinator: Any
    terminal_runtime_registry: Any
    terminal_config: Any
    terminal_services: Any | None = None
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
            return
        if not isinstance(terminal_id, str) or not terminal_id:
            await self._send_json(
                websocket,
                {
                    "type": "terminal_attach_result",
                    "request_id": request_id,
                    "success": False,
                    "code": "terminal_gone",
                    "terminal_id": terminal_id,
                },
            )
            return
        manager = getattr(self, "terminal_manager", None)
        row = None if manager is None else manager.get(terminal_id)
        if row is None:
            await self._send_json(
                websocket,
                {
                    "type": "terminal_attach_result",
                    "request_id": request_id,
                    "success": False,
                    "code": "terminal_gone",
                    "terminal_id": terminal_id,
                },
            )
            return
        registry = self._leases()
        viewer: Literal["web", "gclient"] = "web" if data.get("viewer") == "web" else "gclient"
        record = registry.attach(terminal_id, str(delivery), websocket=websocket, viewer=viewer)
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
        if failure is not None:
            registry.finalize(record.attachment_id, failure)
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
                "lease_generation": registry.generation(terminal_id),
                "success": True,
            },
        )

    async def _handle_terminal_detach(self, websocket: Any, data: dict[str, Any]) -> None:
        attachment_id = data.get("attachment_id")
        terminal_id = data.get("terminal_id")
        if isinstance(attachment_id, str):
            if attachment_id in self._proxy().attachments:
                await self._proxy().finalize_attachment(attachment_id, "detach")
            else:
                event = self._leases().finalize(attachment_id, "detach")
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
        """Every pending or live terminal on this machine, tmux panes included.

        A ``project_id`` (sent by the web project picker) narrows the page to
        that project plus terminals that belong to no project, which live
        under the global project. Without one the whole machine is listed.
        """
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
        machine_id = require_machine_id()
        panes = await self._sweep_tmux_panes(manager, machine_id)
        items, has_more = manager.list_page(
            None if project_id is None else [project_id, GLOBAL_PROJECT_ID],
            machine_id=machine_id,
            states=("pending", "live"),
            cursor_created_at=cursor_created_at,
            cursor_id=cursor_id,
            limit=limit,
        )
        serialized = []
        for row in items:
            item = inventory_item(row)
            pane = panes.get(row.locator_key or "")
            if pane is not None:
                item.update(
                    {
                        "name": pane.session_name,
                        "socket": os.path.basename(pane.socket_path),
                        "window_name": pane.window_name,
                        "pane_pid": pane.pane_pid,
                        "pane_title": pane.pane_title,
                        "pane_command": pane.pane_command,
                        "pane_path": pane.pane_path,
                    }
                )
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
        await self._send_json(websocket, payload)

    async def _sweep_tmux_panes(self, manager: Any, machine_id: str) -> dict[str, Any]:
        """Mirror the tmux servers into ``terminals`` before listing; never fails the list.

        A pane whose working directory is not inside a registered project is
        filed under the global project, so it shows up whichever project the
        web picker selects.
        """
        tmux_managers = [
            tmux
            for tmux in (
                getattr(self, "_tmux_mgr_default", None),
                getattr(self, "_tmux_mgr_gobby", None),
            )
            if tmux is not None
        ]
        if not tmux_managers:
            return {}
        session_manager = getattr(self, "session_manager", None)
        try:
            sessions = (
                []
                if session_manager is None
                else session_manager.list(
                    statuses=LIVE_SESSION_STATUS_ORDER,
                    machine_id=machine_id,
                    limit=1000,
                )
            )
            return await sweep_tmux_terminals(
                manager,
                tmux_managers,
                machine_id=machine_id,
                owners=pane_owners(sessions),
                fallback_project_id=GLOBAL_PROJECT_ID,
            )
        except Exception:
            logger.warning("tmux terminal discovery failed", exc_info=True)
            return {}

    async def _handle_terminal_create(self, websocket: Any, data: dict[str, Any]) -> None:
        request_id = data.get("request_id")
        try:
            validate_dimensions(data.get("rows"), data.get("cols"))
        except InvalidTerminalDimensionsError:
            await self._send_json(
                websocket,
                {
                    "type": "terminal_error",
                    "code": "invalid_dimensions",
                    "request_id": request_id,
                },
            )
            return
        from gobby.terminals.web_spawn import spawn_web_terminal

        manager = getattr(self, "terminal_manager", None)
        registry = getattr(self, "terminal_runtime_registry", None)
        # A picker with no project selected lists the whole machine; a terminal
        # created there belongs to the global project, like an unowned pane.
        project_id = data.get("project_id") or self._project_id(websocket) or GLOBAL_PROJECT_ID
        if manager is None or registry is None or not isinstance(project_id, str):
            reason = (
                "terminal manager unavailable"
                if manager is None
                else "terminal runtime registry unavailable"
                if registry is None
                else "project unresolved"
            )
            logger.warning("terminal_create refused: %s", reason)
            await self._send_json(
                websocket,
                {
                    "type": "terminal_create_result",
                    "success": False,
                    "request_id": request_id,
                    "reason": reason,
                },
            )
            return
        backend = getattr(self.terminal_config, "default_backend", None)
        runtime = registry.resolve(backend or TerminalConfig().default_backend)
        command = data.get("command") or ["zsh"]
        if not isinstance(command, list):
            command = ["zsh"]
        result = await spawn_web_terminal(
            manager=manager,
            runtime=runtime,
            project_id=project_id,
            session_id=None,
            rows=data.get("rows"),
            cols=data.get("cols"),
            cwd=data.get("cwd"),
            command=[str(part) for part in command],
        )
        if not result.success:
            logger.warning(
                "terminal_create spawn failed (backend=%s, terminal=%s): %s",
                runtime.backend,
                result.terminal_id,
                result.error,
            )
        await self._send_json(
            websocket,
            {
                "type": "terminal_create_result",
                "request_id": request_id,
                "success": result.success,
                "terminal_id": result.terminal_id,
                "backend": runtime.backend,
                "reason": result.error,
            },
        )
        if result.success:
            row = manager.get(result.terminal_id)
            if row is not None:
                await self.broadcast_tmux_session_event(
                    "created",
                    terminal_id=result.terminal_id,
                    terminal=inventory_item(row),
                )

    async def _handle_terminal_kill(self, websocket: Any, data: dict[str, Any]) -> None:
        terminal_id = data.get("terminal_id")
        manager = getattr(self, "terminal_manager", None)
        row = (
            None
            if manager is None or not isinstance(terminal_id, str)
            else manager.get(terminal_id)
        )
        transitioned = None
        if (
            row is not None
            and manager is not None
            and getattr(self, "terminal_runtime_registry", None) is not None
            and row.state in {"live", "orphaned"}
        ):
            runtime = self.terminal_runtime_registry.resolve(row.backend)
            await runtime.terminate(row, 1.0)
            transitioned = manager.mark_exited(row.id)
            if transitioned is not None:
                await self.broadcast_tmux_session_event("killed", terminal_id=row.id)
        await self._send_json(
            websocket,
            {
                "type": "terminal_kill_result",
                "success": transitioned is not None,
                "terminal_id": terminal_id,
                "request_id": data.get("request_id"),
            },
        )

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
        record = self._leases().get(attachment_id)
        manager = getattr(self, "terminal_manager", None)
        row = None if record is None or manager is None else manager.get(record.terminal_id)
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

    async def _handle_terminal_take_control(self, websocket: Any, data: dict[str, Any]) -> None:
        terminal_id = str(data.get("terminal_id") or "")
        attachment_id = str(data.get("attachment_id") or "")
        takeover = bool(data.get("takeover"))
        registry = self._leases()
        previous = registry.holder(terminal_id)
        result = registry.take_control(terminal_id, attachment_id, takeover=takeover)
        if result.granted and previous and previous != attachment_id:
            await self._fanout_lease_lost(
                previous,
                attachment_id,
                result.lease_generation,
                requester=websocket,
            )
        control = {
            "type": "terminal_control_result",
            "attachment_id": attachment_id,
            "granted": result.granted,
            "reason": result.reason,
            "lease_generation": result.lease_generation,
        }
        await self._send_control(websocket, control)

    async def _handle_terminal_release_control(self, websocket: Any, data: dict[str, Any]) -> None:
        attachment_id = str(data.get("attachment_id") or "")
        result = self._leases().release_control(attachment_id)
        await self._send_json(
            websocket,
            {
                "type": "terminal_control_result",
                "attachment_id": attachment_id,
                "granted": result.granted,
                "reason": result.reason,
                "lease_generation": result.lease_generation,
            },
        )

    async def _handle_terminal_input(self, websocket: Any, data: dict[str, Any]) -> None:
        if not data.get("attachment_id"):
            return
        await self._handle_operator_write(websocket, data, kind="input")

    async def _handle_terminal_paste(self, websocket: Any, data: dict[str, Any]) -> None:
        text = data.get("text")
        if isinstance(text, str) and paste_oversize(text):
            await self._write_outcome(
                websocket,
                data,
                outcome="refused",
                reason="oversize",
            )
            return
        await self._handle_operator_write(websocket, data, kind="paste")

    async def _handle_operator_write(
        self,
        websocket: Any,
        data: dict[str, Any],
        *,
        kind: Literal["input", "paste", "text"],
    ) -> None:
        terminal_id = data.get("terminal_id")
        attachment_id = data.get("attachment_id")
        seq = data.get("client_write_seq")
        payload = data.get("data") if kind == "input" else data.get("text")
        if not isinstance(terminal_id, str) or not isinstance(attachment_id, str):
            return
        if not isinstance(payload, str):
            payload = ""
        record = self._leases().get(attachment_id)
        generation = None if record is None else self._leases().generation(terminal_id)
        admitted = self._leases().admit_write(
            terminal_id,
            attachment_id=attachment_id,
            expected_lease_generation=generation
            if self._leases().holder(terminal_id) == attachment_id
            else -1,
            seq=seq,
            kind=kind,
            payload=payload.encode("utf-8"),
        )
        if not admitted.ok:
            await self._write_outcome(websocket, data, outcome="refused", reason=admitted.reason)
            return
        if admitted.recorded_outcome is not None:
            await self._write_outcome(
                websocket, data, outcome=admitted.recorded_outcome, reason=admitted.reason
            )
            return
        if admitted.join_inflight and isinstance(seq, int):
            joined = await self._wait_joined_write(attachment_id, seq)
            await self._write_outcome(websocket, data, outcome=joined[0], reason=joined[1])
            return
        if write_handler_faulted():
            if isinstance(seq, int):
                self._leases().complete_write(attachment_id, seq, "refused", "write_handler_fault")
            await self._write_outcome(
                websocket, data, outcome="refused", reason="write_handler_fault"
            )
            return
        outcome = "indeterminate"
        reason: str | None = "indeterminate_backend"
        try:
            outcome, reason = await self._deliver_operator_write(
                terminal_id,
                attachment_id,
                kind=kind,
                payload=payload,
                generation=generation,
                seq=seq,
            )
        finally:
            if isinstance(seq, int):
                self._leases().complete_write(attachment_id, seq, outcome, reason)
        record_turn_observation(
            self,
            terminal_id,
            kind=kind,
            payload=payload,
            outcome=outcome,
            seq=seq,
        )
        await self._write_outcome(websocket, data, outcome=outcome, reason=reason)

    async def _deliver_operator_write(
        self,
        terminal_id: str,
        attachment_id: str,
        *,
        kind: Literal["input", "paste", "text"],
        payload: str,
        generation: int | None,
        seq: object = None,
    ) -> tuple[WriteOutcome, str | None]:
        """Deliver an admitted write to the backend; returns (outcome, reason)."""
        outcome: WriteOutcome = "delivered"
        reason: str | None = None
        manager = getattr(self, "terminal_manager", None)
        row = None if manager is None else manager.get(terminal_id)
        runtime = None if row is None else self._runtime_for(row.backend)
        try:
            if runtime is not None:
                if kind == "paste":
                    result = await runtime.write_paste(row, payload)
                elif kind == "input":
                    result = await runtime.write_input(row, payload.encode("utf-8"))
                else:
                    result = await runtime.write_text(row, payload, False)
            elif getattr(self, "write_coordinator", None) is not None:
                from gobby.terminals.write_coordinator import WriteRequest

                coordinator = self.write_coordinator
                result = await coordinator.write(
                    WriteRequest(
                        terminal_id=terminal_id,
                        action_key=f"ws:{attachment_id}:{seq}",
                        origin="operator",
                        kind=kind,
                        payload=payload,
                        attachment_id=attachment_id,
                        expected_lease_generation=generation,
                    )
                )
            else:
                return outcome, reason
        except TerminalWriteError as exc:
            if exc.stage == "partial":
                reason = (
                    f"indeterminate_partial_delivered:{exc.delivered_bytes}"
                    if exc.delivered_bytes is not None
                    else "indeterminate_backend"
                )
                return "indeterminate", reason
            return "refused", "held"
        except (ConnectionError, OSError):
            return "indeterminate", "indeterminate_backend"
        if isinstance(result, IndeterminateWrite):
            outcome = "indeterminate"
            reason = "indeterminate_backend"
        elif not isinstance(result, Delivered):
            outcome = "refused"
            reason = "held"
        return outcome, reason

    async def _write_outcome(
        self,
        websocket: Any,
        data: dict[str, Any],
        *,
        outcome: str,
        reason: str | None,
    ) -> None:
        await self._send_json(
            websocket,
            {
                "type": "terminal_write_outcome",
                "terminal_id": data.get("terminal_id"),
                "attachment_id": data.get("attachment_id"),
                "client_write_seq": data.get("client_write_seq"),
                "outcome": outcome,
                "reason": reason,
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

    def _runtime_for(self, backend: str) -> Any | None:
        registry = getattr(self, "terminal_runtime_registry", None)
        if registry is None:
            return None
        resolve = getattr(registry, "resolve", None)
        if not callable(resolve):
            return None
        try:
            runtime = resolve(backend)
        except Exception:
            return None
        if isinstance(runtime, Mock):
            return None
        return runtime

    async def _resolve_attach_locator(self, row: Any) -> tuple[AttachLocator | None, str | None]:
        runtime = self._runtime_for(row.backend)
        if runtime is None:
            return None, _log_proxy_attach_failure(row.id, "runtime_unavailable")
        try:
            locator = await runtime.attach_locator(row)
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
            frame = await opener(locator)
        except Exception:
            return _log_proxy_attach_failure(row.id, "host_unavailable", exc_info=True)
        if not frame or not callable(getattr(frame, "read_message", None)):
            # The relay pump requires read_message; anything else dies after
            # the client was already told the attach succeeded.
            if frame is not None:
                await _close_frame_quietly(frame)
            return _log_proxy_attach_failure(row.id, "frame_invalid")
        try:
            await self._proxy().start_proxy(
                websocket,
                terminal_id=row.id,
                attachment_id=record.attachment_id,
                locator=locator,
                frame=frame,
                encoding=encoding,
            )
        except Exception:
            await _close_frame_quietly(frame)
            return _log_proxy_attach_failure(row.id, "proxy_start_failed", exc_info=True)
        return None

    async def _wait_joined_write(self, attachment_id: str, seq: int) -> tuple[str, str | None]:
        deadline = asyncio.get_running_loop().time() + 2.0
        while asyncio.get_running_loop().time() < deadline:
            completed = self._leases().completed_write(attachment_id, seq)
            if completed is not None:
                return completed
            await asyncio.sleep(0.01)
        return "indeterminate", "indeterminate_backend"

    def _leases(self) -> TerminalLeaseRegistry:
        registry = getattr(self, "lease_registry", None)
        if registry is None:
            registry = TerminalLeaseRegistry()
            self.lease_registry = registry
        return registry

    def _project_id(self, websocket: Any) -> str | None:
        meta = self.clients.get(websocket) or {}
        value = meta.get("project_id")
        return value if isinstance(value, str) else None
