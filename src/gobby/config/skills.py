"""
Skills configuration for Gobby daemon.

Provides configuration for skill discovery and hub search.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from gobby.config.url_validation import validate_optional_endpoint_url


class HubConfig(BaseModel):
    """
    Configuration for a skill hub or collection.
    """

    type: Literal["clawdhub", "skillsmp", "github-collection", "github-topic", "claude-plugins"] = (
        Field(
            ...,
            description=(
                "Type of the hub: 'clawdhub', 'skillsmp', 'github-collection', "
                "'github-topic', or 'claude-plugins'"
            ),
        )
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
        description="Secret name in SecretStore for the hub's auth key",
    )

    topic: str = Field(
        default="gobby-skill",
        min_length=1,
        description="GitHub repository topic used for discovery",
    )

    auth_token_env: str = Field(
        default="GITHUB_TOKEN",
        min_length=1,
        description="Environment or secret name containing the GitHub token",
    )

    cache_ttl_seconds: int = Field(
        default=1800,
        gt=0,
        description="GitHub topic discovery cache TTL in seconds",
    )

    @property
    def auth_secret_name(self) -> str | None:
        """Return the SecretStore key used to authenticate this hub."""
        return self.auth_token_env if self.type == "github-topic" else self.auth_key_name

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str | None) -> str | None:
        return validate_optional_endpoint_url(value, field_name="base_url")


class SkillsConfig(BaseModel):
    """
    Configuration for skill discovery.

    Controls whether and how skills are advertised for on-demand loading.
    """

    inject_core_skills: bool = Field(
        default=True,
        description="Whether to advertise core skills in session context",
    )

    core_skills_path: str | None = Field(
        default=None,
        description="Override path for core skills (default: install/shared/skills/)",
    )

    injection_format: Literal["summary", "full", "none"] = Field(
        default="summary",
        description="Format selector for skill manifests: 'summary', 'full', or 'none'",
    )

    soft_delete_retention_days: int = Field(
        default=30,
        gt=0,
        description="Days to retain soft-deleted skills before permanent removal",
    )

    bundled_max_content_size: int = Field(
        default=15_000,
        gt=0,
        description=(
            "Maximum character and UTF-8 byte count for each bundled SKILL.md or reference"
        ),
    )

    hubs: dict[str, HubConfig] = Field(
        default_factory=lambda: {
            "anthropic-skills": HubConfig(
                type="github-collection",
                repo="anthropics/skills",
                branch="main",
                path="skills",
            ),
            "gobby-topic": HubConfig(type="github-topic"),
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
