"""
Admin routes for Gobby HTTP server.

Provides status, metrics, config, and shutdown endpoints.
Decomposed via Strangler Fig pattern.
"""

from typing import TYPE_CHECKING

from fastapi import APIRouter

from gobby.servers.routes.admin._config import register_config_routes
from gobby.servers.routes.admin._health import create_health_router, register_health_routes
from gobby.servers.routes.admin._lease import register_lease_routes
from gobby.servers.routes.admin._lifecycle import register_lifecycle_routes
from gobby.servers.routes.admin._stats import register_stats_routes
from gobby.servers.routes.admin._testing import register_testing_routes
from gobby.servers.routes.admin._token_timeseries import register_token_timeseries_routes
from gobby.servers.routes.admin._usage import register_usage_routes

__all__ = [
    "create_admin_router",
    "create_health_router",
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
    register_lease_routes(router, server)
    register_testing_routes(router, server)
    register_stats_routes(router, server)
    register_usage_routes(router, server)
    register_token_timeseries_routes(router, server)

    return router
