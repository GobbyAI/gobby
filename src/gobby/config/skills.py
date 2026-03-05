"""
Skills configuration for Gobby daemon.

Provides configuration for skill injection and discovery.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


class HubConfig(BaseModel):
    """
    Configuration for a skill hub or collection.
    """

    type: Literal["clawdhub", "skillsmp", "github-collection", "claude-plugins"] = Field(
        ...,
        description="Type of the hub: 'clawdhub', 'skillsmp', 'github-collection', or 'claude-plugins'",
    )

    base_url: str | None = Field(
        default=None,
        description="Base URL for the hub",
    )

    repo: str | None = Field(
        default=None,
        description="GitHub repository (e.g. 'owner/repo')",
    )

    branch: str | None = Field(
        default=None,
        description="Git branch to use",
    )

    path: str | None = Field(
        default=None,
        description="Subdirectory path within the repository where skills are located",
    )

    auth_key_name: str | None = Field(
        default=None,
        description="Environment variable name for auth key",
    )


class SkillsConfig(BaseModel):
    """
    Configuration for skill injection and discovery.

    Controls whether and how skills are injected into session context.
    """

    inject_core_skills: bool = Field(
        default=True,
        description="Whether to inject core skills into session context",
    )

    core_skills_path: str | None = Field(
        default=None,
        description="Override path for core skills (default: install/shared/skills/)",
    )

    injection_format: Literal["summary", "full", "none"] = Field(
        default="summary",
        description="Format for skill injection: 'summary' (names only), 'full' (with content), 'none' (disabled)",
    )

    hubs: dict[str, HubConfig] = Field(
        default_factory=lambda: {
            "anthropic-skills": HubConfig(
                type="github-collection",
                repo="anthropics/skills",
                branch="main",
                path="skills",
            ),
            "claude-plugins": HubConfig(
                type="claude-plugins",
                base_url="https://claude-plugins.dev",
            ),
            "clawdhub": HubConfig(
                type="clawdhub",
            ),
            "skillsmp": HubConfig(
                type="skillsmp",
                base_url="https://skillsmp.com/api/v1",
                auth_key_name="SKILLSMP_API_KEY",
            ),
        },
        description="Configured skill hubs keyed by hub name",
    )

    @field_validator("injection_format")
    @classmethod
    def validate_injection_format(cls, v: str) -> str:
        """Validate injection_format is one of the allowed values."""
        allowed = {"summary", "full", "none"}
        if v not in allowed:
            raise ValueError(f"injection_format must be one of {allowed}, got '{v}'")
        return v
