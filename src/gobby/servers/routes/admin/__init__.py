"""
Admin routes for Gobby HTTP server.

Provides status, metrics, config, and shutdown endpoints.
Decomposed via Strangler Fig pattern.
"""

from typing import TYPE_CHECKING

from fastapi import APIRouter

from gobby.servers.routes.admin._config import register_config_routes
from gobby.servers.routes.admin._health import register_health_routes
from gobby.servers.routes.admin._lifecycle import register_lifecycle_routes
from gobby.servers.routes.admin._setup import register_setup_routes
from gobby.servers.routes.admin._testing import register_testing_routes

__all__ = [
    "create_admin_router",
]

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer


def create_admin_router(server: "HTTPServer") -> APIRouter:
    """
    Create admin router with endpoints bound to server instance.

    Args:
        server: HTTPServer instance for accessing state and dependencies

    Returns:
        Configured APIRouter with admin endpoints
    """
    router = APIRouter(prefix="/api/admin", tags=["admin"])

    register_health_routes(router, server)
    register_config_routes(router, server)
    register_lifecycle_routes(router, server)
    register_testing_routes(router, server)
    register_setup_routes(router, server)

    return router
