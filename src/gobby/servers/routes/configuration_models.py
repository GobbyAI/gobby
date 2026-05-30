"""Request models for configuration routes."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, model_validator


class SaveConfigRequest(BaseModel):
    """Request body for PUT /api/config/values."""

    values: dict[str, Any]


class SaveTemplateRequest(BaseModel):
    """Request body for PUT /api/config/template."""

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


class SaveUISettingsRequest(BaseModel):
    """Request body for PUT /api/config/ui-settings."""

    fontSize: int | None = None
    model: str | None = None
    theme: str | None = None
    defaultChatMode: str | None = None
    postPlanChatMode: str | None = None
    selectedProjectId: str | None = None
    selectedProvider: str | None = None

    @model_validator(mode="after")
    def require_at_least_one_setting(self) -> SaveUISettingsRequest:
        """Reject empty payloads and all-null no-op updates."""
        if all(value is None for value in self.model_dump().values()):
            raise ValueError("At least one UI setting must be provided")
        return self


class SaveApprovalRulesRequest(BaseModel):
    """Request body for PUT /api/config/tool-approvals/global."""

    rules: list[str]


class ImportConfigRequest(BaseModel):
    """Request body for POST /api/config/import."""

    config_store: dict[str, Any] | None = None
    config: dict[str, Any] | None = None
    config_secret_keys: list[str] | None = None
    prompts: dict[str, str] | None = None
