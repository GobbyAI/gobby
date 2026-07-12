"""
Web UI configuration.
"""

from __future__ import annotations

from ipaddress import IPv6Address, ip_address
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = [
    "AuthConfig",
    "ToolApprovalConfig",
    "ToolApprovalPolicy",
    "UIConfig",
    "is_loopback_bind_host",
]


def is_loopback_bind_host(host: str) -> bool:
    """Return whether a bind host is unambiguously local-only.

    Hostname resolution is deliberately avoided: only the reserved localhost
    name and numeric loopback addresses are trusted by this security boundary.
    """
    normalized = host.casefold()
    if normalized.endswith("."):
        normalized = normalized[:-1]
    if normalized == "localhost":
        return True

    try:
        address = ip_address(normalized)
    except ValueError:
        return False

    if address.is_loopback:
        return True
    return (
        isinstance(address, IPv6Address)
        and address.ipv4_mapped is not None
        and address.ipv4_mapped.is_loopback
    )


class ToolApprovalPolicy(BaseModel):
    """A single tool approval policy matching server/tool glob patterns."""

    server_pattern: str = Field(default="*", description="Glob pattern for server name")
    tool_pattern: str = Field(default="*", description="Glob pattern for tool name")
    policy: Literal["auto", "approve_once", "always_ask"] = Field(
        default="always_ask",
        description="Approval policy: 'auto', 'approve_once', or 'always_ask'",
    )


class ToolApprovalConfig(BaseModel):
    """Configuration for tool approval UI in web chat."""

    enabled: bool = Field(default=False, description="Enable tool approval prompts")
    default_policy: Literal["auto", "approve_once", "always_ask"] = Field(
        default="auto",
        description="Default policy: 'auto' (no prompts), 'approve_once', or 'always_ask'",
    )
    policies: list[ToolApprovalPolicy] = Field(
        default_factory=list,
        description="Per-tool approval policies (server/tool glob patterns)",
    )


class AuthConfig(BaseModel):
    """Non-secret web authentication settings."""

    model_config = ConfigDict(extra="ignore")

    username: str = Field(
        default="",
        description="Username for web UI login.",
    )


class UIConfig(BaseModel):
    """Configuration for the web UI."""

    enabled: bool = Field(default=False, description="Enable web UI serving")
    mode: str = Field(default="auto", description="'auto', 'production', or 'dev'")
    port: int = Field(default=60889, description="Dev server port (dev mode only)")
    host: str = Field(default="localhost", description="Dev server host (dev mode only)")
    web_dir: str | None = Field(
        default=None, description="Path to web/ dir (auto-detected if None)"
    )
    memory_graph_limit: int = Field(
        default=5000,
        ge=50,
        le=5000,
        description="Default display limit for the 2D memory graph (nodes)",
    )
    knowledge_graph_limit: int = Field(
        default=5000,
        ge=50,
        le=5000,
        description="Default display limit for the 3D knowledge graph (entities)",
    )

    @field_validator("port")
    @classmethod
    def validate_port(cls, v: int) -> int:
        """Validate port number is in valid range."""
        if not (1024 <= v <= 65535):
            raise ValueError("Port must be between 1024 and 65535")
        return v

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, v: str) -> str:
        if v not in ("auto", "production", "dev"):
            raise ValueError("UI mode must be 'auto', 'production', or 'dev'")
        return v
