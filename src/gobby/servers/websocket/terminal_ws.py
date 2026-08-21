"""Backend-neutral terminal WebSocket handlers."""

from __future__ import annotations

import logging
from typing import Any, cast

from gobby.terminals.dimensions import InvalidTerminalDimensionsError, validate_dimensions
from gobby.terminals.leases import TerminalLeaseRegistry, paste_oversize
from gobby.terminals.ws_protocol import (
    TERMINAL_LIST_DEFAULT_PAGE_SIZE,
    TERMINAL_LIST_MAX_PAGE_SIZE,
    encode_page,
    inventory_item,
)
from gobby.utils.json_helpers import json_dumps

logger = logging.getLogger(__name__)


class TerminalWsMixin:
    """Observe-only attach, lease-gated writes, and inventory."""

    clients: dict[Any, dict[str, Any]]
    lease_registry: TerminalLeaseRegistry
    terminal_manager: Any
    write_coordinator: Any
    terminal_runtime_registry: Any
    terminal_config: Any

    async def _send_json(self, websocket: Any, payload: dict[str, Any]) -> None:
        await websocket.send(json_dumps(payload))

    async def _handle_terminal_attach(self, websocket: Any, data: dict[str, Any]) -> None:
        request_id = data.get("request_id")
        terminal_id = data.get("terminal_id")
        delivery = data.get("frame_delivery") or "proxy"
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
        record = registry.attach(terminal_id, str(delivery), websocket=websocket)
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
                "lease_generation": registry.generation(terminal_id),
                "success": True,
            },
        )

    async def _handle_terminal_detach(self, websocket: Any, data: dict[str, Any]) -> None:
        attachment_id = data.get("attachment_id")
        terminal_id = data.get("terminal_id")
        if isinstance(attachment_id, str):
            event = self._leases().finalize(attachment_id, "detach")
            if event is not None:
                await self._send_json(
                    websocket,
                    {
                        "type": "terminal_attachment_finalized",
                        "terminal_id": event.terminal_id,
                        "attachment_id": event.attachment_id,
                        "reason": event.reason,
                        "lease_generation": event.lease_generation,
                    },
                )
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
        manager = getattr(self, "terminal_manager", None)
        project_id = data.get("project_id") or self._project_id(websocket)
        if manager is None or not isinstance(project_id, str):
            await self._send_json(
                websocket,
                {
                    "type": "terminal_list",
                    "request_id": data.get("request_id"),
                    "items": [],
                    "next_cursor": None,
                },
            )
            return
        limit = data.get("limit", TERMINAL_LIST_DEFAULT_PAGE_SIZE)
        if not isinstance(limit, int):
            limit = TERMINAL_LIST_DEFAULT_PAGE_SIZE
        limit = max(1, min(limit, TERMINAL_LIST_MAX_PAGE_SIZE))
        items, has_more = manager.list_page(project_id, states=("pending", "live"), limit=limit)
        payload = encode_page(
            [inventory_item(row) for row in items],
            None if not has_more else f"{items[-1].created_at.isoformat()}|{items[-1].id}",
        )
        payload["type"] = "terminal_list"
        payload["request_id"] = data.get("request_id")
        await self._send_json(websocket, payload)

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
        project_id = data.get("project_id") or self._project_id(websocket)
        if manager is None or registry is None or not isinstance(project_id, str):
            await self._send_json(
                websocket,
                {"type": "terminal_create_result", "success": False, "request_id": request_id},
            )
            return
        runtime = registry.resolve(getattr(self.terminal_config, "default_backend", "tmux"))
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
        await self._send_json(
            websocket,
            {
                "type": "terminal_create_result",
                "request_id": request_id,
                "success": result.success,
                "terminal_id": result.terminal_id,
                "backend": runtime.backend,
            },
        )

    async def _handle_terminal_kill(self, websocket: Any, data: dict[str, Any]) -> None:
        terminal_id = data.get("terminal_id")
        manager = getattr(self, "terminal_manager", None)
        row = (
            None
            if manager is None or not isinstance(terminal_id, str)
            else manager.get(terminal_id)
        )
        if (
            row is not None
            and manager is not None
            and getattr(self, "terminal_runtime_registry", None) is not None
        ):
            runtime = self.terminal_runtime_registry.resolve(row.backend)
            await runtime.terminate(row, 1.0)
            manager.mark_exited(row.id)
        await self._send_json(
            websocket,
            {
                "type": "terminal_kill_result",
                "success": True,
                "terminal_id": terminal_id,
                "request_id": data.get("request_id"),
            },
        )

    async def _handle_terminal_resize(self, websocket: Any, data: dict[str, Any]) -> None:
        attachment_id = data.get("attachment_id")
        if not isinstance(attachment_id, str):
            return
        try:
            validate_dimensions(data.get("rows"), data.get("cols"))
        except InvalidTerminalDimensionsError:
            await self._send_json(
                websocket, {"type": "terminal_error", "code": "invalid_dimensions"}
            )
            return
        admitted = self._leases().resize_pty(attachment_id, data.get("rows"), data.get("cols"))
        if not admitted.ok:
            await self._send_json(
                websocket,
                {
                    "type": "terminal_error",
                    "code": admitted.reason,
                    "attachment_id": attachment_id,
                },
            )

    async def _handle_terminal_set_viewport(self, websocket: Any, data: dict[str, Any]) -> None:
        attachment_id = data.get("attachment_id")
        if not isinstance(attachment_id, str):
            return
        try:
            self._leases().set_viewport(attachment_id, data.get("rows"), data.get("cols"))
        except (KeyError, InvalidTerminalDimensionsError):
            await self._send_json(
                websocket, {"type": "terminal_error", "code": "invalid_dimensions"}
            )

    async def _handle_terminal_set_scroll_offset(
        self, websocket: Any, data: dict[str, Any]
    ) -> None:
        attachment_id = data.get("attachment_id")
        if not isinstance(attachment_id, str):
            return
        applied = self._leases().set_scroll_offset(
            attachment_id,
            int(data.get("rows_from_live_edge") or 0),
            int(data.get("max_rows") or 0),
        )
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
            await self._send_json(
                websocket,
                {
                    "type": "terminal_lease_lost",
                    "attachment_id": previous,
                    "holder": attachment_id,
                    "lease_generation": result.lease_generation,
                },
            )
            await self._fanout_lease_lost(previous, attachment_id, result.lease_generation)
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
        if data.get("attachment_id"):
            await self._handle_operator_write(websocket, data, kind="input")
            return
        from gobby.servers.websocket.handlers.core import HandlerMixin

        await HandlerMixin._handle_terminal_input(cast(HandlerMixin, self), websocket, data)

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
        self, websocket: Any, data: dict[str, Any], *, kind: str
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
        coordinator = getattr(self, "write_coordinator", None)
        outcome = "delivered"
        reason = None
        if coordinator is not None:
            from gobby.terminals.runtime import Delivered, IndeterminateWrite
            from gobby.terminals.write_coordinator import WriteRequest

            result = await coordinator.write(
                WriteRequest(
                    terminal_id=terminal_id,
                    action_key=f"ws:{attachment_id}:{seq}",
                    origin="operator",
                    kind="paste" if kind == "paste" else "text",
                    payload=payload,
                    attachment_id=attachment_id,
                    expected_lease_generation=generation,
                )
            )
            if isinstance(result, IndeterminateWrite):
                outcome = "indeterminate"
                reason = "indeterminate_backend"
            elif not isinstance(result, Delivered):
                outcome = "refused"
                reason = "held"
        if isinstance(seq, int):
            self._leases().complete_write(attachment_id, seq, outcome, reason)
        await self._write_outcome(websocket, data, outcome=outcome, reason=reason)

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

    async def _fanout_lease_lost(self, previous: str, holder: str, generation: int) -> None:
        message = {
            "type": "terminal_lease_lost",
            "attachment_id": previous,
            "holder": holder,
            "lease_generation": generation,
        }
        for ws in list(self.clients.keys()):
            try:
                await ws.send(json_dumps(message))
            except Exception:
                logger.debug("lease_lost fanout failed", exc_info=True)

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
