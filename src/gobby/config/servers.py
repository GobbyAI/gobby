"""
Server configuration module.

Contains server and networking Pydantic config models:
- WebSocketSettings: WebSocket server settings (port, ping interval)
- MCPClientProxyConfig: MCP proxy settings (timeouts, search mode)

Extracted from app.py using Strangler Fig pattern for code decomposition.
"""

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from gobby.config.bootstrap import DEFAULT_WEBSOCKET_PORT

__all__ = ["WebSocketSettings", "MCPClientProxyConfig"]


class WebSocketSettings(BaseModel):
    """WebSocket server configuration."""

    enabled: bool = Field(
        default=True,
        description="Enable WebSocket server for real-time communication",
    )
    port: int = Field(
        default=DEFAULT_WEBSOCKET_PORT,
        description="Port for WebSocket server to listen on",
    )
    ping_interval: int = Field(
        default=30,
        description="Ping interval in seconds for keepalive",
    )
    ping_timeout: int = Field(
        default=10,
        description="Pong timeout in seconds before considering connection dead",
    )

    @field_validator("port")
    @classmethod
    def validate_port(cls, v: int) -> int:
        """Validate port number is in valid range."""
        if not (1024 <= v <= 65535):
            raise ValueError("Port must be between 1024 and 65535")
        return v

    @field_validator("ping_interval", "ping_timeout")
    @classmethod
    def validate_positive(cls, v: int) -> int:
        """Validate value is positive."""
        if v <= 0:
            raise ValueError("Value must be positive")
        return v


class MCPClientProxyConfig(BaseModel):
    """MCP client proxy configuration for downstream MCP servers."""

    enabled: bool = Field(
        default=True,
        description="Enable MCP client proxy for downstream MCP servers",
    )
    connect_timeout: float = Field(
        default=30.0,
        description="Timeout in seconds for establishing connections to MCP servers",
    )
    proxy_timeout: int = Field(
        default=30,
        description="Timeout in seconds for proxy calls to downstream MCP servers",
    )
    tool_timeout: int = Field(
        default=30,
        description="Timeout in seconds for tool schema operations",
    )
    tool_timeouts: dict[str, float] = Field(
        default_factory=dict,
        description="Map of tool names to specific timeouts in seconds",
    )

    # Semantic search (embedding model/endpoint configured in EmbeddingsConfig)
    search_mode: Literal["llm", "semantic", "hybrid"] = Field(
        default="llm",
        description="Default search mode for tool recommendations: 'llm' (LLM-based), 'semantic' (embedding similarity), 'hybrid' (both)",
    )
    min_similarity: float = Field(
        default=0.3,
        description="Minimum similarity threshold for semantic search results (0.0-1.0)",
    )
    top_k: int = Field(
        default=10,
        description="Default number of results to return for semantic search",
    )

    # Refresh settings
    refresh_on_server_add: bool = Field(
        default=True,
        description="Automatically refresh tool embeddings when adding a new MCP server",
    )
    refresh_timeout: float = Field(
        default=300.0,
        description="Timeout in seconds for tool refresh operations (embedding generation)",
    )

    @field_validator("connect_timeout", "refresh_timeout")
    @classmethod
    def validate_connect_timeout(cls, v: float) -> float:
        """Validate timeout is positive."""
        if v <= 0:
            raise ValueError("Timeout must be positive")
        return v

    @field_validator("proxy_timeout", "tool_timeout")
    @classmethod
    def validate_timeout(cls, v: int) -> int:
        """Validate timeout is positive."""
        if v <= 0:
            raise ValueError("Timeout must be positive")
        return v

    @field_validator("min_similarity")
    @classmethod
    def validate_min_similarity(cls, v: float) -> float:
        """Validate min_similarity is between 0 and 1."""
        if not 0.0 <= v <= 1.0:
            raise ValueError("min_similarity must be between 0.0 and 1.0")
        return v

    @field_validator("top_k")
    @classmethod
    def validate_top_k(cls, v: int) -> int:
        """Validate top_k is positive."""
        if v <= 0:
            raise ValueError("top_k must be positive")
        return v
