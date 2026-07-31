"""Build pipeline configuration types."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml

DeliveryMode = Literal["auto", "pull_request"]
Isolation = Literal["none", "worktree", "clone"]
SkippableStage = Literal[
    "ideation",
    "research",
    "architecture",
    "prd",
    "planning",
    "expansion",
    "development",
    "epic_qa",
    "pr",
    "merge",
]

_SKIPPABLE_STAGE_VALUES: tuple[SkippableStage, ...] = (
    "ideation",
    "research",
    "architecture",
    "prd",
    "planning",
    "expansion",
    "development",
    "epic_qa",
    "pr",
    "merge",
)
SKIPPABLE_STAGES: frozenset[SkippableStage] = frozenset(_SKIPPABLE_STAGE_VALUES)
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StageCapOverride:
    """Per-stage build cap override."""

    stage_name: str
    max_work_attempts: int | None = None
    max_review_rounds: int | None = None


_LEGACY_CAP_DESTINATIONS: Mapping[str, tuple[str, str]] = {
    "max_expansion_attempts": ("expansion", "max_work_attempts"),
    "max_qa_rounds": ("development", "max_review_rounds"),
    "max_merge_attempts": ("merge", "max_work_attempts"),
    "max_epic_qa_rounds": ("epic_qa", "max_review_rounds"),
    "max_review_rounds": ("pr", "max_review_rounds"),
    "default_max_review_rounds": ("pr", "max_review_rounds"),
}


@dataclass(frozen=True)
class BuildConfig:
    """Configuration for build agent dispatch."""

    max_active_agents: int = 10


def load_build_config(
    project_root: str | Path,
    flag_overrides: Mapping[str, Any] | None = None,
) -> BuildConfig:
    """Load build config from defaults, global config, project config, and flags."""

    merged = _config_to_mapping(BuildConfig())
    for path in (
        Path.home() / ".gobby" / "build.yaml",
        Path(project_root) / ".gobby" / "build.yaml",
    ):
        _merge_config(merged, _load_yaml_mapping(path))

    if flag_overrides:
        _merge_config(merged, flag_overrides)

    return _build_config_from_mapping(merged)


def _config_to_mapping(cfg: BuildConfig) -> dict[str, Any]:
    return {"max_active_agents": cfg.max_active_agents}


def _load_yaml_mapping(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"build config has invalid YAML at {path}: {exc}") from exc
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ValueError(f"build config must be a mapping: {path}")

    return _string_key_mapping(raw, str(path))


def _merge_config(target: dict[str, Any], updates: Mapping[str, Any]) -> None:
    for key, value in updates.items():
        if key in _LEGACY_CAP_DESTINATIONS:
            _merge_legacy_cap(target, key, value)
            continue
        target[key] = value


def _build_config_from_mapping(raw: Mapping[str, Any]) -> BuildConfig:
    return BuildConfig(
        max_active_agents=_normalize_int(raw.get("max_active_agents", 10), "max_active_agents"),
    )


def _merge_legacy_cap(target: dict[str, Any], field_name: str, value: Any) -> None:
    stage_name, cap_field = _LEGACY_CAP_DESTINATIONS[field_name]
    stage_caps = _merge_stage_caps(target.get("stage_caps", {}), {})
    existing = stage_caps.get(stage_name, {})
    if not isinstance(existing, Mapping):
        existing = {}
    updated = dict(existing)
    updated[cap_field] = value
    stage_caps[stage_name] = updated
    target["stage_caps"] = stage_caps
    logger.warning(
        "build config field %s is deprecated; translated to stage_caps.%s.%s",
        field_name,
        stage_name,
        cap_field,
    )


def _merge_stage_caps(existing: Any, updates: Any) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    if existing:
        if not isinstance(existing, Mapping):
            raise ValueError("stage_caps must be a mapping")
        merged.update(_string_key_mapping(existing, "stage_caps"))
    if updates:
        if not isinstance(updates, Mapping):
            raise ValueError("stage_caps must be a mapping")
        for stage_name, raw_override in _string_key_mapping(updates, "stage_caps").items():
            if not isinstance(raw_override, Mapping):
                raise ValueError(f"stage_caps.{stage_name} must be a mapping")
            current = dict(merged.get(stage_name, {}))
            current.update(_string_key_mapping(raw_override, f"stage_caps.{stage_name}"))
            merged[stage_name] = current
    return merged


def _normalize_int(value: Any, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{field_name} must be an integer")
    if value < 1:
        raise ValueError(f"{field_name} must be greater than or equal to 1")
    return value


def _string_key_mapping(value: Mapping[Any, Any], source: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise ValueError(f"{source} must use string keys")
        result[key] = item
    return result


__all__ = [
    "BuildConfig",
    "DeliveryMode",
    "Isolation",
    "SKIPPABLE_STAGES",
    "StageCapOverride",
    "SkippableStage",
    "load_build_config",
]
