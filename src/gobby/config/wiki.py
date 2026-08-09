from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field, field_validator

DEFAULT_WIKI_IGNORE_GLOBS: tuple[str, ...] = (
    "outputs/**",
    "meta/health/**",
    "meta/librarian/**",
    "meta/upkeep/**",
    "_meta/**",
    # gwiki-written machine state: raw source captures (rewritten by refresh
    # and collect) and vault-internal state. Watching them re-triggers an
    # index for changes gwiki itself just wrote.
    "raw/**",
    "inbox/**",
    "_gwiki/**",
)

CODEWIKI_DORMANCY_NOTICE = (
    "CodeWiki generation is paused pending the wiki redesign; this key has no effect until "
    "#19665 re-enables orchestration."
)


class WikiRootConfig(BaseModel):
    """One wiki root watched by the daemon."""

    scope: str = Field(
        description=(
            "Scope kind label: 'project' for a project vault (may repeat across "
            "roots; the watcher disambiguates by root path) or 'topic:<name>' "
            "for a hub topic vault."
        )
    )
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
        default_factory=lambda: list(DEFAULT_WIKI_IGNORE_GLOBS),
        description="Root-relative file globs ignored by the watcher.",
    )
    codewiki_on_commit: bool = Field(
        default=False,
        description=(
            f"Stored preference for post-commit CodeWiki generation. {CODEWIKI_DORMANCY_NOTICE}"
        ),
    )
    codewiki_nightly_enabled: bool = Field(
        default=True,
        description=(
            f"Stored preference for nightly CodeWiki generation. {CODEWIKI_DORMANCY_NOTICE}"
        ),
    )
    codewiki_nightly_schedule_cron: str = Field(
        default="0 3 * * *",
        description=(
            f"Stored cron expression for nightly CodeWiki generation. {CODEWIKI_DORMANCY_NOTICE}"
        ),
    )
    codewiki_nightly_timezone: str | None = Field(
        default=None,
        description=(
            f"Stored IANA timezone for nightly CodeWiki generation. {CODEWIKI_DORMANCY_NOTICE}"
        ),
    )
    codewiki_scopes: list[str] = Field(
        default_factory=list,
        description=(
            f"Stored default repo-relative CodeWiki generation scopes. {CODEWIKI_DORMANCY_NOTICE}"
        ),
    )
    codewiki_project_scopes_by_name: dict[str, list[str]] = Field(
        default_factory=dict,
        description=(
            "Stored project-name keyed CodeWiki generation scope overrides. "
            f"{CODEWIKI_DORMANCY_NOTICE}"
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
        validated: dict[str, list[str]] = {}
        for project_name, scopes in value.items():
            if not isinstance(project_name, str) or not project_name.strip():
                raise ValueError("codewiki project scope keys must be non-empty strings")
            project_key = project_name.strip()
            validated[project_key] = _validate_codewiki_scope_list(
                scopes,
                field_name=f"codewiki_project_scopes_by_name.{project_key}",
            )
        return validated


def _validate_codewiki_scope_list(value: object, *, field_name: str) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be a list of non-empty strings")
    scopes: list[str] = []
    for scope in value:
        if not isinstance(scope, str):
            raise ValueError(f"{field_name} must contain only non-empty strings")
        stripped_scope = scope.strip()
        if not stripped_scope:
            raise ValueError(f"{field_name} must contain only non-empty strings")
        scopes.append(stripped_scope)
    return scopes
