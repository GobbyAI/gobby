"""Request models for configuration routes."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, Strict

from gobby.storage.config_repository import MAX_CONFIG_REVISION

ConfigRevision = Annotated[int, Strict(), Field(ge=0, le=MAX_CONFIG_REVISION)]


class PatchConfigRequest(BaseModel):
    """Request body for PATCH /api/config/values."""

    model_config = ConfigDict(extra="forbid")

    expected_revision: ConfigRevision
    values: dict[str, object] = Field(default_factory=dict)
    unset: frozenset[str] = frozenset()


class ConfigDocumentRequest(BaseModel):
    """Request body for daemon YAML replacement endpoints."""

    model_config = ConfigDict(extra="forbid")

    expected_revision: ConfigRevision
    content: str


class SaveSecretRequest(BaseModel):
    """Request body for POST /api/config/secrets."""

    name: str
    value: str
    category: str = "general"
    description: str | None = None


class SavePromptOverrideRequest(BaseModel):
    """Request body for PUT /api/config/prompts/{path}."""

    content: str
