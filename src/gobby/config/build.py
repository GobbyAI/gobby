"""Build pipeline configuration types."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

import yaml

Isolation = Literal["none", "worktree", "clone"]
SkippableStage = Literal[
    "ideation",
    "research",
    "architecture",
    "prd",
    "planning",
    "test_arch",
    "expansion",
    "development",
    "holistic_qa",
    "pr",
    "merge",
]

_SKIPPABLE_STAGE_VALUES: tuple[SkippableStage, ...] = (
    "ideation",
    "research",
    "architecture",
    "prd",
    "planning",
    "test_arch",
    "expansion",
    "development",
    "holistic_qa",
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
    "max_holistic_rounds": ("holistic_qa", "max_review_rounds"),
    "max_review_rounds": ("pr", "max_review_rounds"),
    "default_max_review_rounds": ("pr", "max_review_rounds"),
}


def _default_clones_dir() -> Path:
    return Path.home() / ".gobby" / "clones"


@dataclass(frozen=True)
class BuildConfig:
    """Configuration for build orchestration and dispatch defaults."""

    default_skip_stages: tuple[str, ...] = ()
    default_isolation: Isolation = "worktree"
    stage_caps: dict[str, StageCapOverride] = field(default_factory=dict)
    default_target_branch: str | None = None
    clones_dir: Path = field(default_factory=_default_clones_dir)
    cleanup_clones_on_merge: bool = True
    max_active_agents: int = 10
    dispatch_interval_seconds: int = 60


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
    return {
        "default_skip_stages": cfg.default_skip_stages,
        "default_isolation": cfg.default_isolation,
        "stage_caps": {
            stage_name: _stage_cap_to_mapping(stage_cap)
            for stage_name, stage_cap in cfg.stage_caps.items()
        },
        "default_target_branch": cfg.default_target_branch,
        "clones_dir": cfg.clones_dir,
        "cleanup_clones_on_merge": cfg.cleanup_clones_on_merge,
        "max_active_agents": cfg.max_active_agents,
        "dispatch_interval_seconds": cfg.dispatch_interval_seconds,
    }


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
        if key == "stage_caps":
            target["stage_caps"] = _merge_stage_caps(target.get("stage_caps", {}), value)
            continue
        target[key] = value


def _build_config_from_mapping(raw: Mapping[str, Any]) -> BuildConfig:
    return BuildConfig(
        default_skip_stages=_normalize_stages(
            raw.get("default_skip_stages", ()), "default_skip_stages"
        ),
        default_isolation=_normalize_isolation(
            raw.get("default_isolation", "worktree"), "default_isolation"
        ),
        stage_caps=_normalize_stage_caps(raw.get("stage_caps", {}), "stage_caps"),
        default_target_branch=_normalize_optional_str(
            raw.get("default_target_branch"), "default_target_branch"
        ),
        clones_dir=_normalize_path(
            raw.get("clones_dir", Path.home() / ".gobby" / "clones"), "clones_dir"
        ),
        cleanup_clones_on_merge=_normalize_bool(
            raw.get("cleanup_clones_on_merge", True), "cleanup_clones_on_merge"
        ),
        max_active_agents=_normalize_int(raw.get("max_active_agents", 10), "max_active_agents"),
        dispatch_interval_seconds=_normalize_int(
            raw.get("dispatch_interval_seconds", 60), "dispatch_interval_seconds"
        ),
    )


def _stage_cap_to_mapping(stage_cap: StageCapOverride) -> dict[str, int | None]:
    return {
        "max_work_attempts": stage_cap.max_work_attempts,
        "max_review_rounds": stage_cap.max_review_rounds,
    }


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


def _normalize_stage_caps(value: Any, field_name: str) -> dict[str, StageCapOverride]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be a mapping")
    normalized: dict[str, StageCapOverride] = {}
    for stage_name, raw_override in _string_key_mapping(value, field_name).items():
        if not isinstance(raw_override, Mapping):
            raise ValueError(f"{field_name}.{stage_name} must be a mapping")
        override = _string_key_mapping(raw_override, f"{field_name}.{stage_name}")
        max_work_attempts = _normalize_optional_int(
            override.get("max_work_attempts"),
            f"{field_name}.{stage_name}.max_work_attempts",
        )
        max_review_rounds = _normalize_optional_int(
            override.get("max_review_rounds"),
            f"{field_name}.{stage_name}.max_review_rounds",
        )
        if max_work_attempts is None and max_review_rounds is None:
            continue
        normalized[stage_name] = StageCapOverride(
            stage_name=stage_name,
            max_work_attempts=max_work_attempts,
            max_review_rounds=max_review_rounds,
        )
    return normalized


def _normalize_stages(value: Any, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{field_name} must be a list of stages")

    stages: list[str] = []
    for stage in value:
        if not isinstance(stage, str):
            raise ValueError(f"{field_name} must contain only strings")
        if stage not in SKIPPABLE_STAGES:
            raise ValueError(f"unknown skippable stage for {field_name}: {stage}")
        stages.append(stage)
    return tuple(stages)


def _normalize_isolation(value: Any, field_name: str) -> Isolation:
    if value not in ("none", "worktree", "clone"):
        raise ValueError(f"{field_name} must be one of: none, worktree, clone")
    return cast(Isolation, value)


def _normalize_bool(value: Any, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field_name} must be a boolean")
    return value


def _normalize_int(value: Any, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{field_name} must be an integer")
    return value


def _normalize_optional_int(value: Any, field_name: str) -> int | None:
    if value is None:
        return None
    normalized = _normalize_int(value, field_name)
    if normalized < 1:
        raise ValueError(f"{field_name} must be greater than or equal to 1")
    return normalized


def _normalize_optional_str(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    return value


def _normalize_path(value: Any, field_name: str) -> Path:
    if isinstance(value, Path):
        return value.expanduser()
    if isinstance(value, str):
        return Path(value).expanduser()
    raise ValueError(f"{field_name} must be a path")


def _string_key_mapping(value: Mapping[Any, Any], source: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise ValueError(f"{source} must use string keys")
        result[key] = item
    return result


__all__ = [
    "BuildConfig",
    "Isolation",
    "SKIPPABLE_STAGES",
    "StageCapOverride",
    "SkippableStage",
    "load_build_config",
]
