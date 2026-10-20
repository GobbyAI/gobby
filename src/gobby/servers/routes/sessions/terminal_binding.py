"""Explicit native-pane attachment, reserved for the local operator recovery key."""

from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING, Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from gobby.sessions.terminal_binding import repair_native_terminal_binding
from gobby.storage.session_resolution import resolve_session_reference

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer


class NativeTerminalBindingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    terminal_id: UUID


def register_terminal_binding_routes(router: APIRouter, server: HTTPServer) -> None:
    @router.post("/{session_ref}/native-terminal-binding")
    async def attach_native_terminal(
        session_ref: str, payload: NativeTerminalBindingRequest, request: Request
    ) -> dict[str, Any]:
        # A front-door MCP caller can have principal=None, including agents.
        # Only the loopback operator recovery credential admits manual selection.
        if not server.auth_service.break_glass_admits(request):
            raise HTTPException(status_code=403, detail="Local operator recovery key required")
        manager = server.session_manager
        if manager is None:
            raise HTTPException(status_code=503, detail="Session manager not available")
        try:
            session_id = await server.run_db(resolve_session_reference, manager.db, session_ref)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        session = await server.run_db(manager.get, session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")
        if session.source != "codex":
            raise HTTPException(status_code=400, detail="Native repair requires a Codex session")
        result = await server.run_db(
            repair_native_terminal_binding,
            manager,
            session,
            selected_terminal_id=str(payload.terminal_id),
        )
        return {"session_id": session.id, "terminal_binding": asdict(result)}
