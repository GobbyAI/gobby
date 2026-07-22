"""Schema and TOML loader for agent detection manifests."""

from __future__ import annotations

import re
import tomllib
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CURRENT_ENGINE_VERSION = 1
MAX_BOTTOM_NON_EMPTY_LINES = 200

DetectionState = Literal["blocked", "idle", "working", "stall"]

_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
_VERSION_RE = re.compile(r"^\d+(?:\.\d+)*$")
_BOTTOM_REGION_RE = re.compile(r"^bottom_non_empty_lines\(([1-9]\d*)\)$")


def bottom_non_empty_line_count(region: str) -> int | None:
    """Return the configured bottom-line count when *region* uses that selector."""

    match = _BOTTOM_REGION_RE.fullmatch(region)
    if match is None:
        return None
    return int(match.group(1))


class MatchClause(BaseModel):
    """A conjunction of literal and line-regex conditions."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    contains: tuple[str, ...] = Field(default_factory=tuple)
    line_regex: tuple[str, ...] = Field(default_factory=tuple)

    @field_validator("contains", "line_regex")
    @classmethod
    def validate_non_empty_values(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value.strip() for value in values):
            raise ValueError("match values must be non-empty strings")
        return values

    @model_validator(mode="after")
    def require_condition(self) -> Self:
        if not self.contains and not self.line_regex:
            raise ValueError("a match clause requires contains or line_regex")
        return self


class DetectionRule(MatchClause):
    """One prioritized pane-detection rule."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    id: str
    state: DetectionState
    reason: str | None = None
    priority: int = Field(ge=0, le=10_000)
    region: str
    not_: tuple[MatchClause, ...] = Field(default_factory=tuple, alias="not")

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if _IDENTIFIER_RE.fullmatch(value) is None:
            raise ValueError("rule id must be a lowercase identifier")
        return value

    @field_validator("region")
    @classmethod
    def validate_region(cls, value: str) -> str:
        if value in {"whole_recent", "prompt_box"}:
            return value
        line_count = bottom_non_empty_line_count(value)
        if line_count is None or line_count > MAX_BOTTOM_NON_EMPTY_LINES:
            raise ValueError("unsupported detection region")
        return value

    @model_validator(mode="after")
    def require_blocked_reason(self) -> Self:
        if self.state == "blocked" and (self.reason is None or not self.reason.strip()):
            raise ValueError("blocked rules require a reason")
        return self


class DetectionManifest(BaseModel):
    """Validated manifest for one agent provider."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    version: str
    engine: int = Field(ge=1)
    rules: tuple[DetectionRule, ...] = Field(min_length=1)

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if _IDENTIFIER_RE.fullmatch(value) is None:
            raise ValueError("manifest id must be a lowercase provider identifier")
        return value

    @field_validator("version")
    @classmethod
    def validate_version(cls, value: str) -> str:
        if _VERSION_RE.fullmatch(value) is None:
            raise ValueError("manifest version must be dotted-numeric")
        return value

    @field_validator("engine")
    @classmethod
    def validate_engine(cls, value: int) -> int:
        if value > CURRENT_ENGINE_VERSION:
            raise ValueError(
                f"manifest engine {value} is newer than supported engine {CURRENT_ENGINE_VERSION}"
            )
        return value

    @model_validator(mode="after")
    def require_unique_rule_ids(self) -> Self:
        rule_ids = [rule.id for rule in self.rules]
        if len(rule_ids) != len(set(rule_ids)):
            raise ValueError("manifest rule ids must be unique")
        return self

    @property
    def version_key(self) -> tuple[int, ...]:
        """Return a tuple suitable for monotonic version comparison."""

        return tuple(int(part) for part in self.version.split("."))


def load_manifest(content: str | bytes) -> DetectionManifest:
    """Parse and validate TOML manifest content."""

    text = content.decode("utf-8") if isinstance(content, bytes) else content
    return DetectionManifest.model_validate(tomllib.loads(text))
