"""
Web UI configuration.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

__all__ = ["AuthConfig", "ToolApprovalConfig", "ToolApprovalPolicy", "UIConfig"]


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
    """Basic authentication for the web UI.

    Leave username and password empty to disable auth (default).
    Once both are set, the UI requires login. Password is encrypted
    via Fernet in the secrets table.
    """

    username: str = Field(
        default="",
        description="Username for web UI login. Leave empty to disable auth.",
    )
    password: str = Field(
        default="",
        description="Password for web UI login (encrypted in secrets table).",
    )
    session_secret: str = Field(
        default="",
        description="HMAC signing key for session cookies (auto-generated on first login).",
        json_schema_extra={"ui_hidden": True},
    )


class UIConfig(BaseModel):
    """Configuration for the web UI."""

    enabled: bool = Field(default=False, description="Enable web UI serving")
    mode: str = Field(default="production", description="'production' or 'dev'")
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
        if v not in ("production", "dev"):
            raise ValueError("UI mode must be 'production' or 'dev'")
        return v
