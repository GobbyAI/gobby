"""
FastAPI route module for Gobby communications framework.
"""

import json
import logging
from dataclasses import asdict
from typing import Any, Literal, cast

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from gobby.communications.manager import EventSubscriptionNotFoundError
from gobby.servers.http import HTTPServer

logger = logging.getLogger(__name__)


def create_communications_router(server: HTTPServer) -> APIRouter:
    """Create communications router."""
    router = APIRouter(prefix="/api/comms", tags=["communications"])

    class ChannelCreateRequest(BaseModel):
        channel_type: str = Field(..., description="Type of channel (e.g., slack, telegram)")
        name: str = Field(..., description="Unique name for the channel")
        config: dict[str, Any] = Field(default_factory=dict, description="Channel configuration")
        secrets: dict[str, Any] | None = Field(None, description="Optional secrets")

    class ChannelUpdateRequest(BaseModel):
        name: str | None = Field(None, description="Updated channel name")
        config: dict[str, Any] | None = Field(None, description="Updated channel config")
        enabled: bool | None = Field(None, description="Enable or disable channel")
        secrets: dict[str, Any] | None = Field(None, description="Updated channel secrets")

    class EventSubscriptionCreateRequest(BaseModel):
        name: str
        channel: str
        event_pattern: str
        project_id: str | None = None
        global_scope: bool = False
        session_id: str | None = None
        priority: int = 0
        enabled: bool = True

    class EventSubscriptionUpdateRequest(BaseModel):
        name: str | None = None
        channel: str | None = None
        event_pattern: str | None = None
        project_id: str | None = None
        global_scope: bool | None = None
        session_id: str | None = None
        priority: int | None = None
        enabled: bool | None = None

    class SendMessageRequest(BaseModel):
        channel_name: str = Field(..., description="Name of the destination channel")
        content: str = Field(..., description="Message content")
        session_id: str | None = Field(None, description="Optional originating session")
        metadata: dict[str, Any] | None = Field(None, description="Optional message metadata")

    @router.post("/webhooks/{channel_name}")
    async def receive_webhook(
        channel_name: str,
        request: Request,
    ) -> Any:
        """Receive an inbound webhook for a channel."""
        comms_manager = server.services.communications_manager
        if not comms_manager:
            raise HTTPException(status_code=503, detail="Communications manager not available")

        # Get raw body and headers
        body = await request.body()
        headers = dict(request.headers)

        # Try parsing JSON to pass as dict if it is JSON, else pass bytes
        payload: dict[str, Any] | bytes = body
        if request.headers.get("content-type", "").startswith("application/json"):
            try:
                if body:
                    payload = json.loads(body)
                else:
                    payload = {}
            except json.JSONDecodeError:
                pass

        try:
            messages = await comms_manager.handle_inbound(
                channel_name, payload, headers, raw_body=body
            )

            # Check for challenge response (e.g., Slack url_verification)
            for msg in messages:
                if msg.content_type == "url_verification":
                    return Response(content=msg.content, media_type="text/plain")
                if msg.content_type == "interaction_ping":
                    return json.loads(msg.content)

            return {"status": "ok", "messages": len(messages)}
        except ValueError as e:
            logger.warning("Webhook validation failed for channel %s: %s", channel_name, e)
            raise HTTPException(status_code=400, detail=str(e)) from e
        except Exception as e:
            logger.error("Error processing webhook for channel %s: %s", channel_name, e)
            raise HTTPException(status_code=500, detail="Internal server error") from e

    @router.get("/webhooks/{channel_name}")
    async def verify_webhook(
        channel_name: str,
        request: Request,
    ) -> Response:
        """Handle webhook verification challenges via GET."""
        challenge = request.query_params.get("validationToken") or request.query_params.get(
            "challenge"
        )
        if challenge:
            return Response(content=challenge, media_type="text/plain")

        return Response(content="ok", media_type="text/plain")

    @router.post("/send")
    async def send_message(request: SendMessageRequest) -> dict[str, Any]:
        """Send a message to a named channel."""
        comms_manager = server.services.communications_manager
        if not comms_manager:
            raise HTTPException(status_code=503, detail="Communications manager not available")

        try:
            message = await comms_manager.send_message(
                request.channel_name,
                request.content,
                session_id=request.session_id,
                metadata=request.metadata,
            )
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e

        if message.status != "sent":
            raise HTTPException(
                status_code=502,
                detail=message.error or "Message delivery failed",
            )
        return asdict(message)

    @router.get("/channels")
    async def list_channels() -> list[dict[str, Any]]:
        """List all channels."""
        comms_manager = server.services.communications_manager
        if not comms_manager:
            raise HTTPException(status_code=503, detail="Communications manager not available")

        channels = comms_manager.list_channels()
        return [comms_manager.channel_to_dict(c) for c in channels]

    @router.post("/channels")
    async def create_channel(request: ChannelCreateRequest) -> dict[str, Any]:
        """Create a new channel."""
        comms_manager = server.services.communications_manager
        if not comms_manager:
            raise HTTPException(status_code=503, detail="Communications manager not available")

        try:
            channel = await comms_manager.add_channel(
                channel_type=request.channel_type,
                name=request.name,
                config=request.config,
                secrets=request.secrets,
            )
            return cast("dict[str, Any]", comms_manager.channel_to_dict(channel))
        except Exception as e:
            logger.exception("Failed to add channel: %s", e)
            raise HTTPException(status_code=400, detail="Invalid channel configuration") from e

    @router.put("/channels/{channel_id}")
    async def update_channel(channel_id: str, request: ChannelUpdateRequest) -> dict[str, Any]:
        """Update channel configuration."""
        comms_manager = server.services.communications_manager
        if not comms_manager:
            raise HTTPException(status_code=503, detail="Communications manager not available")

        channel = comms_manager.get_channel(channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail="Channel not found")

        if request.name is not None:
            name = request.name.strip()
            if not name:
                raise HTTPException(status_code=400, detail="Channel name is required")
            channel.name = name
        if request.config is not None:
            preserved_secret_refs = {
                key: value
                for key, value in channel.config_json.items()
                if key not in request.config
                and isinstance(value, str)
                and value.startswith("$secret:")
            }
            channel.config_json = {**preserved_secret_refs, **request.config}
        if request.enabled is not None:
            channel.enabled = request.enabled

        updated = await comms_manager.update_channel(channel, secrets=request.secrets)

        return cast("dict[str, Any]", comms_manager.channel_to_dict(updated))

    @router.delete("/channels/{channel_id}")
    async def remove_channel(channel_id: str) -> dict[str, Any]:
        """Remove a channel."""
        comms_manager = server.services.communications_manager
        if not comms_manager:
            raise HTTPException(status_code=503, detail="Communications manager not available")

        channel = comms_manager.get_channel(channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail="Channel not found")

        try:
            await comms_manager.remove_channel(channel.name)
            return {"status": "ok", "deleted": channel_id}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.get("/channels/{channel_id}/status")
    async def get_channel_status(channel_id: str) -> dict[str, Any]:
        """Get channel health/status."""
        comms_manager = server.services.communications_manager
        if not comms_manager:
            raise HTTPException(status_code=503, detail="Communications manager not available")

        channel = comms_manager.get_channel(channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail="Channel not found")

        status = comms_manager.get_channel_status(channel.name)
        return dict(status)

    @router.get("/messages")
    async def list_messages(
        channel_id: str | None = None,
        session_id: str | None = None,
        direction: Literal["inbound", "outbound"] | None = None,
        limit: int = Query(50, ge=1, le=1000),
        offset: int = Query(0, ge=0),
    ) -> list[dict[str, Any]]:
        """List messages with optional filters."""
        comms_manager = server.services.communications_manager
        if not comms_manager:
            raise HTTPException(status_code=503, detail="Communications manager not available")

        messages = comms_manager.list_messages(
            channel_id=channel_id,
            session_id=session_id,
            direction=direction,
            limit=limit,
            offset=offset,
        )
        return [asdict(m) for m in messages]

    @router.post("/subscriptions")
    async def create_event_subscription(
        request: EventSubscriptionCreateRequest,
    ) -> dict[str, Any]:
        """Create an event subscription."""
        comms_manager = server.services.communications_manager
        if not comms_manager:
            raise HTTPException(status_code=503, detail="Communications manager not available")
        try:
            rule = comms_manager.create_event_subscription(
                name=request.name,
                channel=request.channel,
                event_pattern=request.event_pattern,
                project_id=request.project_id,
                global_scope=request.global_scope,
                session_id=request.session_id,
                priority=request.priority,
                enabled=request.enabled,
            )
            return cast("dict[str, Any]", comms_manager.event_subscription_to_dict(rule))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @router.get("/subscriptions")
    async def list_event_subscriptions(
        channel: str | None = None,
        project_id: str | None = None,
        global_scope: bool | None = None,
        enabled: bool | None = None,
        event_pattern: str | None = None,
    ) -> list[dict[str, Any]]:
        """List event subscriptions with exact filters."""
        comms_manager = server.services.communications_manager
        if not comms_manager:
            raise HTTPException(status_code=503, detail="Communications manager not available")
        try:
            rules = comms_manager.list_event_subscriptions(
                channel=channel,
                project_id=project_id,
                global_scope=global_scope,
                enabled=enabled,
                event_pattern=event_pattern,
            )
            return [comms_manager.event_subscription_to_dict(rule) for rule in rules]
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @router.get("/subscriptions/{subscription_id}")
    async def get_event_subscription(subscription_id: str) -> dict[str, Any]:
        """Get an event subscription."""
        comms_manager = server.services.communications_manager
        if not comms_manager:
            raise HTTPException(status_code=503, detail="Communications manager not available")
        try:
            rule = comms_manager.get_event_subscription(subscription_id)
            return cast("dict[str, Any]", comms_manager.event_subscription_to_dict(rule))
        except EventSubscriptionNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e

    @router.patch("/subscriptions/{subscription_id}")
    async def update_event_subscription(
        subscription_id: str,
        request: EventSubscriptionUpdateRequest,
    ) -> dict[str, Any]:
        """Partially update an event subscription."""
        comms_manager = server.services.communications_manager
        if not comms_manager:
            raise HTTPException(status_code=503, detail="Communications manager not available")
        changes = request.model_dump(exclude_unset=True)
        try:
            rule = comms_manager.update_event_subscription(subscription_id, **changes)
            return cast("dict[str, Any]", comms_manager.event_subscription_to_dict(rule))
        except EventSubscriptionNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @router.delete("/subscriptions/{subscription_id}")
    async def delete_event_subscription(subscription_id: str) -> dict[str, Any]:
        """Delete an event subscription."""
        comms_manager = server.services.communications_manager
        if not comms_manager:
            raise HTTPException(status_code=503, detail="Communications manager not available")
        try:
            comms_manager.delete_event_subscription(subscription_id)
            return {"status": "ok", "deleted": subscription_id}
        except EventSubscriptionNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e

    return router
