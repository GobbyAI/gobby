"""HTTP inventory for durable terminal rows."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query

from gobby.storage.terminals import AttachLocator, Terminal, TerminalManager
from gobby.terminals.leases import TerminalLeaseRegistry
from gobby.terminals.ws_protocol import (
    TERMINAL_LIST_DEFAULT_PAGE_SIZE,
    TERMINAL_LIST_MAX_PAGE_SIZE,
    TerminalPageTooLargeError,
    encode_page,
    inventory_item,
    parse_list_cursor,
)
from gobby.utils.machine_id import require_machine_id

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer

DEFAULT_STATES = ("pending", "live")


def create_terminals_router(server: HTTPServer) -> APIRouter:
    """Register GET /api/terminals and GET /api/terminals/{id}."""
    router = APIRouter(tags=["terminals"])
    fallback_registry: TerminalLeaseRegistry | None = None

    def _manager() -> TerminalManager:
        manager = getattr(server.services, "terminal_manager", None)
        if not isinstance(manager, TerminalManager):
            raise HTTPException(status_code=503, detail="terminal_manager unavailable")
        return manager

    def _lease_registry() -> TerminalLeaseRegistry:
        nonlocal fallback_registry
        websocket_server = getattr(server.services, "websocket_server", None) or getattr(
            server, "websocket_server", None
        )
        registry = getattr(websocket_server, "lease_registry", None)
        if isinstance(registry, TerminalLeaseRegistry):
            return registry
        if fallback_registry is None:
            fallback_registry = TerminalLeaseRegistry()
        return fallback_registry

    @router.get("/api/terminals")
    def list_terminals(
        project_id: str = Query(...),
        states: str | None = Query(None),
        backend: str | None = Query(None),
        cursor: str | None = Query(None),
        limit: int = Query(TERMINAL_LIST_DEFAULT_PAGE_SIZE),
    ) -> dict[str, Any]:
        manager = _manager()
        machine_id = require_machine_id()
        page_size = max(1, min(limit, TERMINAL_LIST_MAX_PAGE_SIZE))
        parsed_states = _parse_states(states)
        try:
            created_at, cursor_id = parse_list_cursor(cursor)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid cursor") from exc
        snapshot = (
            _lease_registry().lifecycle_snapshot()
            if created_at is None and cursor_id is None
            else None
        )
        items, has_more = manager.list_page(
            [project_id],
            machine_id=machine_id,
            states=parsed_states,
            backend=backend,
            cursor_created_at=created_at,
            cursor_id=cursor_id,
            limit=page_size,
        )
        serialized = [_row_json(row, _attach(server, manager, row)) for row in items]
        next_cursor = None
        item_cursors = [f"{row.created_at.isoformat()}|{row.id}" for row in items]
        if has_more and items:
            next_cursor = item_cursors[-1]
        try:
            return encode_page(
                serialized,
                next_cursor,
                snapshot=snapshot,
                item_cursors=item_cursors,
            )
        except TerminalPageTooLargeError as exc:
            raise HTTPException(status_code=413, detail="terminal_page_too_large") from exc

    @router.get("/api/terminals/{terminal_id}")
    def get_terminal(terminal_id: str) -> dict[str, Any]:
        manager = _manager()
        machine_id = require_machine_id()
        try:
            UUID(terminal_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid terminal id") from exc
        row = manager.get(terminal_id)
        if row is None or row.machine_id != machine_id:
            raise HTTPException(status_code=404, detail="terminal not found")
        return _row_json(row, _attach(server, manager, row))

    return router


def _parse_states(raw: str | None) -> tuple[str, ...]:
    if raw is None:
        return DEFAULT_STATES
    if raw == "all":
        return ("pending", "live", "exited", "orphaned")
    parts = tuple(item.strip() for item in raw.split(",") if item.strip())
    return parts or DEFAULT_STATES


def _live_host_epoch(server: HTTPServer, row: Terminal) -> str:
    host = getattr(server.services, "terminal_host_manager", None)
    epoch = getattr(host, "host_epoch", None) if host is not None else None
    if isinstance(epoch, str) and epoch:
        return epoch
    return row.host_epoch or ""


def _socket_dir(server: HTTPServer) -> Path:
    host = getattr(server.services, "terminal_host_manager", None)
    directory = getattr(host, "socket_dir", None) if host is not None else None
    if isinstance(directory, Path):
        return directory
    return Path.home() / ".gobby"


def _attach(server: HTTPServer, manager: TerminalManager, row: Terminal) -> AttachLocator | None:
    try:
        return manager.attach_locator(
            row.id,
            live_host_epoch=_live_host_epoch(server, row),
            socket_dir=_socket_dir(server),
        )
    except Exception:
        return None


def _row_json(row: Terminal, attach: AttachLocator | None) -> dict[str, Any]:
    payload = inventory_item(row)
    payload["id"] = row.id
    payload["created_at"] = row.created_at.isoformat()
    payload["attach"] = None if attach is None else asdict(attach)
    return payload
