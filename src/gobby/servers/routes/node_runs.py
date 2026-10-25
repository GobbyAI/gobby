"""Node transport adapters for hub-owned launch outcomes."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer


def create_node_runs_router(server: HTTPServer) -> APIRouter:
    return APIRouter(prefix="/api/nodes", tags=["nodes"])
