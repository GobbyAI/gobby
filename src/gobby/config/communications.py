"""Communications configuration models."""

from pydantic import BaseModel, Field


class ChannelDefaults(BaseModel):
    """Default settings for communication channels."""

    rate_limit_per_minute: int = Field(default=30, ge=1)
    burst: int = Field(default=5, ge=1)
    retry_count: int = 3
    poll_interval_seconds: int = 30
    retention_days: int = 90


class CommunicationsConfig(BaseModel):
    """Configuration for the communications framework."""

    enabled: bool = False
    webhook_base_url: str = ""
    channel_defaults: ChannelDefaults = Field(default_factory=ChannelDefaults)
    inbound_enabled: bool = True
    outbound_enabled: bool = True
    auto_create_sessions: bool = True
