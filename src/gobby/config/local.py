"""
Local model endpoint configuration.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

__all__ = ["LocalConfig"]


class LocalConfig(BaseModel):
    """Configuration for local model endpoint (e.g., LMStudio)."""

    url: str = Field(
        description="Local model API endpoint (e.g., http://localhost:1234/v1)",
    )
    model: str = Field(
        description="Model name to load/use at the local endpoint",
    )
    api_key: str | None = Field(
        default=None,
        description="API key for the local endpoint. Use $secret:NAME for encrypted secrets store.",
    )
