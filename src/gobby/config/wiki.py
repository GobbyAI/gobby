from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field, field_validator


class WikiRootConfig(BaseModel):
    """One wiki root watched by the daemon."""

    scope: str = Field(description="Stable scope name for grouped watcher changes.")
    path: Path = Field(description="Wiki root path to watch.")

    @field_validator("scope")
    @classmethod
    def validate_scope(cls, value: str) -> str:
        scope = value.strip()
        if not scope:
            raise ValueError("scope must not be empty")
        return scope

    @field_validator("path")
    @classmethod
    def expand_path(cls, value: Path) -> Path:
        return value.expanduser()


class WikiConfig(BaseModel):
    """Daemon wiki watcher configuration."""

    enabled: bool = Field(default=True, description="Enable daemon wiki file watching.")
    roots: list[WikiRootConfig] = Field(
        default_factory=list,
        description="Project and topic wiki roots to watch.",
    )
    debounce_interval: float = Field(
        default=0.5,
        description="Seconds to wait after a burst before handing changes to indexing.",
    )
    poll_interval: float = Field(
        default=0.25,
        description="Seconds between filesystem scans.",
    )
    ignore_globs: list[str] = Field(
        default_factory=lambda: ["outputs/**", "meta/health/**"],
        description="Root-relative file globs ignored by the watcher.",
    )
    codewiki_on_commit: bool = Field(
        default=False,
        description=(
            "Refresh generated codewiki docs after post-commit code indexing. Runtime "
            "config key wiki.codewiki_on_commit is canonical after startup."
        ),
    )
    codewiki_nightly_enabled: bool = Field(
        default=False,
        description="Refresh generated codewiki docs on a nightly daemon cron schedule.",
    )
    codewiki_nightly_schedule_cron: str = Field(
        default="0 3 * * *",
        description=(
            "Cron expression for nightly codewiki refresh, interpreted in the configured "
            "or host-local timezone. Execution timestamps are stored in UTC."
        ),
    )
    codewiki_nightly_timezone: str | None = Field(
        default=None,
        description=(
            "IANA timezone for nightly codewiki refresh scheduling. Unset uses the "
            "daemon host's local timezone when it can be resolved, otherwise UTC."
        ),
    )
    codewiki_scopes: list[str] = Field(
        default_factory=list,
        description=(
            "Default repo-relative paths passed to gcode codewiki --scope. "
            "Empty preserves the all-core-files behavior."
        ),
    )
    codewiki_project_scopes_by_name: dict[str, list[str]] = Field(
        default_factory=dict,
        description=(
            "Project-name keyed codewiki scope overrides. Project names are unique "
            "operator-facing config keys; project UUIDs remain internal."
        ),
    )

    @field_validator("debounce_interval", "poll_interval")
    @classmethod
    def validate_positive_interval(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("interval must be greater than zero")
        return value

    @field_validator("codewiki_scopes", mode="before")
    @classmethod
    def validate_codewiki_scopes(cls, value: object) -> object:
        return _validate_codewiki_scope_list(value, field_name="codewiki_scopes")

    @field_validator("codewiki_project_scopes_by_name", mode="before")
    @classmethod
    def validate_codewiki_project_scopes_by_name(cls, value: object) -> object:
        if not isinstance(value, dict):
            raise ValueError("codewiki_project_scopes_by_name must be a mapping")
        for project_name, scopes in value.items():
            if not isinstance(project_name, str) or not project_name.strip():
                raise ValueError("codewiki project scope keys must be non-empty strings")
            _validate_codewiki_scope_list(
                scopes,
                field_name=f"codewiki_project_scopes_by_name.{project_name}",
            )
        return value


def resolve_codewiki_scopes(wiki_config: WikiConfig, project_name: str | None) -> list[str]:
    """Resolve codewiki scopes using project-name override then global fallback."""
    if project_name is not None and project_name in wiki_config.codewiki_project_scopes_by_name:
        return list(wiki_config.codewiki_project_scopes_by_name[project_name])
    return list(wiki_config.codewiki_scopes)


def _validate_codewiki_scope_list(value: object, *, field_name: str) -> object:
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be a list of non-empty strings")
    for scope in value:
        if not isinstance(scope, str) or not scope.strip():
            raise ValueError(f"{field_name} must contain only non-empty strings")
    return value
