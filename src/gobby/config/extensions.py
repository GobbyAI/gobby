"""
Hook extensions configuration module.

Contains hook extension Pydantic config models:
- WebSocketBroadcastConfig: WebSocket event broadcasting
- WebhookEndpointConfig: Single webhook endpoint
- WebhooksConfig: Webhook dispatching settings
- HookExtensionsConfig: Combined extension settings

Extracted from app.py using Strangler Fig pattern for code decomposition.
"""

from pydantic import BaseModel, Field, field_validator

from gobby.config.url_validation import validate_endpoint_url

__all__ = [
    "WebSocketBroadcastConfig",
    "WebhookEndpointConfig",
    "WebhooksConfig",
    "HookExtensionsConfig",
]


class WebSocketBroadcastConfig(BaseModel):
    """Configuration for WebSocket event broadcasting."""

    enabled: bool = Field(
        default=True,
        description="Enable broadcasting hook events to WebSocket clients",
    )
    broadcast_events: list[str] = Field(
        default=[
            "session-start",
            "session-end",
            "pre-tool-use",
            "post-tool-use",
        ],
        description="List of hook event types to broadcast",
    )
    include_payload: bool = Field(
        default=True,
        description="Include event payload data in broadcast messages",
    )


class WebhookEndpointConfig(BaseModel):
    """Configuration for a single webhook endpoint."""

    name: str = Field(
        description="Unique name for this webhook endpoint",
    )
    url: str = Field(
        description="URL to POST webhook payloads to (supports ${ENV_VAR} substitution)",
    )
    events: list[str] = Field(
        default_factory=list,
        description="List of hook event types to trigger this webhook (empty = all events)",
    )
    headers: dict[str, str] = Field(
        default_factory=dict,
        description="Custom HTTP headers to include (supports ${ENV_VAR} substitution)",
    )
    timeout: float = Field(
        default=10.0,
        ge=1.0,
        le=60.0,
        description="Request timeout in seconds",
    )
    retry_count: int = Field(
        default=3,
        ge=0,
        le=10,
        description="Number of retries on failure",
    )
    retry_delay: float = Field(
        default=1.0,
        ge=0.1,
        le=30.0,
        description="Initial retry delay in seconds (doubles each retry)",
    )
    can_block: bool = Field(
        default=False,
        description="If True, webhook can block the action via response decision field",
    )
    fail_closed: bool = Field(
        default=False,
        description="If True, a failed blocking webhook blocks the action",
    )
    enabled: bool = Field(
        default=True,
        description="Enable or disable this webhook",
    )

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        return validate_endpoint_url(value, field_name="url")


class WebhooksConfig(BaseModel):
    """Configuration for HTTP webhooks triggered on hook events."""

    enabled: bool = Field(
        default=True,
        description="Enable webhook dispatching",
    )
    endpoints: list[WebhookEndpointConfig] = Field(
        default_factory=list,
        description="List of webhook endpoint configurations",
    )
    default_timeout: float = Field(
        default=10.0,
        ge=1.0,
        le=60.0,
        description="Default timeout for webhook requests",
    )
    async_dispatch: bool = Field(
        default=True,
        description="Dispatch webhooks asynchronously (non-blocking except for can_block)",
    )


class HookExtensionsConfig(BaseModel):
    """Configuration for hook extensions (broadcasting, webhooks)."""

    websocket: WebSocketBroadcastConfig = Field(
        default_factory=WebSocketBroadcastConfig,
        description="WebSocket broadcasting configuration",
    )
    webhooks: WebhooksConfig = Field(
        default_factory=WebhooksConfig,
        description="HTTP webhook configuration",
    )
