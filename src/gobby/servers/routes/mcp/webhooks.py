"""
Webhooks management routes for Gobby HTTP server.

Provides webhook configuration listing and testing endpoints.
Extracted from mcp.py as part of Strangler Fig decomposition.
"""

import logging
import time
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException, Request

from gobby.servers.routes.dependencies import get_server

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer

logger = logging.getLogger(__name__)


def create_webhooks_router() -> APIRouter:
    """
    Create webhooks management router using dependency injection.

    Returns:
        Configured APIRouter with webhooks endpoints
    """
    router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])

    @router.get("")
    async def list_webhooks(
        server: "HTTPServer" = Depends(get_server),
    ) -> dict[str, Any]:
        """
        List configured webhook endpoints.

        Returns:
            List of webhook endpoint configurations
        """
        config = server.config
        if not config:
            return {
                "success": True,
                "enabled": False,
                "endpoints": [],
            }

        webhooks_config = config.hook_extensions.webhooks

        endpoints = [
            {
                "name": e.name,
                "url": e.url,
                "events": e.events,
                "enabled": e.enabled,
                "can_block": e.can_block,
                "timeout": e.timeout,
                "retry_count": e.retry_count,
            }
            for e in webhooks_config.endpoints
        ]

        return {
            "success": True,
            "enabled": webhooks_config.enabled,
            "endpoints": endpoints,
        }

    @router.post("/test")
    async def test_webhook(
        request: Request,
        server: "HTTPServer" = Depends(get_server),
    ) -> dict[str, Any]:
        """
        Test a webhook endpoint by sending a test event.

        Request body:
            {"name": "webhook-name", "event_type": "notification"}

        Returns:
            Test result with status code and response time
        """
        import httpx

        try:
            body = await request.json()
        except (ValueError, Exception):
            raise HTTPException(status_code=400, detail="Invalid JSON body") from None

        try:
            webhook_name = body.get("name")
            event_type = body.get("event_type", "notification")

            if not webhook_name:
                raise HTTPException(status_code=400, detail="Webhook name required")

            config = server.config
            if not config:
                return {"success": False, "error": "Configuration not available"}

            webhooks_config = config.hook_extensions.webhooks
            if not webhooks_config.enabled:
                return {"success": False, "error": "Webhooks are disabled"}

            # Find the webhook endpoint
            endpoint = None
            for e in webhooks_config.endpoints:
                if e.name == webhook_name:
                    endpoint = e
                    break

            if endpoint is None:
                return {"success": False, "error": f"Webhook not found: {webhook_name}"}

            if not endpoint.enabled:
                return {"success": False, "error": f"Webhook is disabled: {webhook_name}"}

            # Build test payload
            test_payload = {
                "event_type": event_type,
                "test": True,
                "timestamp": time.time(),
                "data": {
                    "message": f"Test event from gobby CLI for webhook '{webhook_name}'",
                },
            }

            # Send test request
            start_time = time.perf_counter()
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    endpoint.url,
                    json=test_payload,
                    headers=endpoint.headers,
                    timeout=endpoint.timeout,
                )
            response_time_ms = (time.perf_counter() - start_time) * 1000

            success = 200 <= response.status_code < 300

            return {
                "success": success,
                "status_code": response.status_code,
                "response_time_ms": response_time_ms,
                "error": None if success else f"HTTP {response.status_code}",
            }

        except httpx.TimeoutException:
            return {"success": False, "error": "Request timed out"}
        except httpx.RequestError as e:
            return {"success": False, "error": f"Request failed: {e}"}
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Webhook test error: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    return router
