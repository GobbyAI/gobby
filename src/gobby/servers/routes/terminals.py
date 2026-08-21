"""HTTP inventory for durable terminal rows."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query

from gobby.storage.terminals import AttachLocator, Terminal, TerminalManager
from gobby.terminals.ws_protocol import (
    TERMINAL_LIST_DEFAULT_PAGE_SIZE,
    TERMINAL_LIST_MAX_ENCODED_BYTES,
    TERMINAL_LIST_MAX_PAGE_SIZE,
    encode_page,
    inventory_item,
)

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer

DEFAULT_STATES = ("pending", "live")


def create_terminals_router(server: HTTPServer) -> APIRouter:
    """Register GET /api/terminals and GET /api/terminals/{id}."""
    router = APIRouter(tags=["terminals"])

    def _manager() -> TerminalManager:
        manager = getattr(server.services, "terminal_manager", None)
        if not isinstance(manager, TerminalManager):
            raise HTTPException(status_code=503, detail="terminal_manager unavailable")
        return manager

    @router.get("/api/terminals")
    def list_terminals(
        project_id: str = Query(...),
        states: str | None = Query(None),
        backend: str | None = Query(None),
        cursor: str | None = Query(None),
        limit: int = Query(TERMINAL_LIST_DEFAULT_PAGE_SIZE),
    ) -> dict[str, Any]:
        manager = _manager()
        page_size = max(1, min(limit, TERMINAL_LIST_MAX_PAGE_SIZE))
        parsed_states = _parse_states(states)
        created_at, cursor_id = _parse_cursor(cursor)
        items, has_more = manager.list_page(
            project_id,
            states=parsed_states,
            backend=backend,
            cursor_created_at=created_at,
            cursor_id=cursor_id,
            limit=page_size,
        )
        serialized = [_row_json(row, _attach(manager, row)) for row in items]
        next_cursor = None
        if has_more and items:
            last = items[-1]
            next_cursor = f"{last.created_at.isoformat()}|{last.id}"
        page = encode_page(serialized, next_cursor)
        encoded = __import__("json").dumps(page, separators=(",", ":")).encode("utf-8")
        if len(encoded) > TERMINAL_LIST_MAX_ENCODED_BYTES:
            page = encode_page(
                serialized[:-1], serialized[-2]["id"] if len(serialized) > 1 else None
            )
        return page

    @router.get("/api/terminals/{terminal_id}")
    def get_terminal(terminal_id: str) -> dict[str, Any]:
        manager = _manager()
        try:
            UUID(terminal_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid terminal id") from exc
        row = manager.get(terminal_id)
        if row is None:
            raise HTTPException(status_code=404, detail="terminal not found")
        return _row_json(row, _attach(manager, row))

    return router


def _parse_states(raw: str | None) -> tuple[str, ...]:
    if raw is None:
        return DEFAULT_STATES
    if raw == "all":
        return ("pending", "live", "exited", "orphaned")
    parts = tuple(item.strip() for item in raw.split(",") if item.strip())
    return parts or DEFAULT_STATES


def _parse_cursor(raw: str | None) -> tuple[datetime | None, str | None]:
    if not raw:
        return None, None
    created_at, _, terminal_id = raw.partition("|")
    if not terminal_id:
        return None, None
    return datetime.fromisoformat(created_at), terminal_id


def _attach(manager: TerminalManager, row: Terminal) -> AttachLocator | None:
    try:
        return manager.attach_locator(
            row.id,
            live_host_epoch=row.host_epoch or "",
            socket_dir=Path.home() / ".gobby",
        )
    except Exception:
        return None


def _row_json(row: Terminal, attach: AttachLocator | None) -> dict[str, Any]:
    payload = inventory_item(row)
    payload["id"] = row.id
    payload["created_at"] = row.created_at.isoformat()
    payload["attach"] = None if attach is None else asdict(attach)
    return payload
