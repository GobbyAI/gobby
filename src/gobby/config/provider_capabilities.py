"""Configuration for provider capability collection."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ProviderCapabilitiesConfig(BaseModel):
    """Daemon background refresh of provider capability catalogs."""

    refresh_enabled: bool = Field(
        default=True,
        description=(
            "Refresh provider capability catalogs at daemon start and every 24 hours. "
            "Each refresh launches the installed provider CLIs."
        ),
    )
