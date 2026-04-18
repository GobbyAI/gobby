"""Config endpoints for admin router."""

import logging
import time
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException

from gobby.utils.version import get_version

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer

logger = logging.getLogger(__name__)


def register_config_routes(router: APIRouter, server: "HTTPServer") -> None:
    @router.get("/config")
    async def get_config() -> dict[str, Any]:
        """
        Get daemon configuration and version information.

        Returns:
            Configuration data including ports, features, and versions
        """
        start_time = time.perf_counter()

        try:
            config_data = {
                "server": {
                    "port": server.port,
                    "test_mode": server.test_mode,
                    "running": server._running,
                    "version": get_version(),
                },
                "features": {
                    "session_manager": server.session_manager is not None,
                    "mcp_manager": server.mcp_manager is not None,
                },
                "endpoints": {
                    "mcp": [
                        "/api/mcp/{server_name}/tools/{tool_name}",
                    ],
                    "sessions": [
                        "/api/sessions/register",
                        "/api/sessions/{id}",
                    ],
                    "admin": [
                        "/api/admin/status",
                        "/api/admin/metrics",
                        "/api/admin/config",
                        "/api/admin/shutdown",
                    ],
                },
            }

            response_time_ms = (time.perf_counter() - start_time) * 1000

            return {
                "status": "success",
                "config": config_data,
                "response_time_ms": response_time_ms,
            }

        except Exception as e:
            logger.error(f"Config retrieval error: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e)) from e
