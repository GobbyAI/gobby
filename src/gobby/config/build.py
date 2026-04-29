"""Build pipeline configuration types."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, TypedDict, cast

import yaml

Isolation = Literal["none", "worktree", "clone"]
SkippableStage = Literal["plan_review", "test_arch", "expanding", "qa", "holistic_review", "pr"]

_SKIPPABLE_STAGE_VALUES: tuple[SkippableStage, ...] = (
    "plan_review",
    "test_arch",
    "expanding",
    "qa",
    "holistic_review",
    "pr",
)
SKIPPABLE_STAGES: frozenset[SkippableStage] = frozenset(_SKIPPABLE_STAGE_VALUES)


class BuildProfile(TypedDict):
    """Resolved build profile options."""

    skip_stages: list[str]
    isolation: Isolation
    yolo: bool


_DEFAULT_PROFILE_TEMPLATES: Mapping[str, tuple[tuple[str, ...], Isolation, bool]] = {
    "quick": (
        ("plan_review", "test_arch", "expanding", "qa", "holistic_review", "pr"),
        "none",
        False,
    ),
    "review": (("plan_review", "pr"), "worktree", False),
    "full": ((), "worktree", False),
    "full-yolo": (("pr",), "worktree", True),
}


def _default_clones_dir() -> Path:
    return Path.home() / ".gobby" / "clones"


def _default_profiles() -> dict[str, BuildProfile]:
    return {
        name: {"skip_stages": list(skip_stages), "isolation": isolation, "yolo": yolo}
        for name, (skip_stages, isolation, yolo) in _DEFAULT_PROFILE_TEMPLATES.items()
    }


@dataclass(frozen=True)
class BuildConfig:
    """Configuration for build orchestration and dispatch defaults."""

    default_skip_stages: tuple[str, ...] = ()
    default_isolation: Isolation = "worktree"
    default_yolo: bool = False
    max_expansion_attempts: int = 3
    max_qa_rounds: int = 5
    max_merge_attempts: int = 3
    max_holistic_rounds: int = 3
    max_review_rounds: int = 3
    default_target_branch: str | None = None
    clones_dir: Path = field(default_factory=_default_clones_dir)
    cleanup_clones_on_merge: bool = True
    max_active_agents: int = 10
    dispatch_interval_seconds: int = 60
    profiles: dict[str, BuildProfile] = field(default_factory=_default_profiles)

    @property
    def default_max_review_rounds(self) -> int:
        """Backward-compatible alias for the consolidated review cap."""
        return self.max_review_rounds


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


def resolve_profile(
    cfg: BuildConfig,
    name: str,
    input_ref: str,
    input_kind: str | None = None,
    has_plan_file: bool = False,
) -> BuildProfile:
    """Resolve a build profile name, including the auto profile shortcut."""

    profile_name = (
        _resolve_auto_profile_name(input_ref, input_kind, has_plan_file) if name == "auto" else name
    )
    if profile_name not in cfg.profiles:
        raise ValueError(f"unknown build profile: {profile_name}")

    return _copy_profile(cfg.profiles[profile_name])


def _config_to_mapping(cfg: BuildConfig) -> dict[str, Any]:
    return {
        "default_skip_stages": cfg.default_skip_stages,
        "default_isolation": cfg.default_isolation,
        "default_yolo": cfg.default_yolo,
        "max_expansion_attempts": cfg.max_expansion_attempts,
        "max_qa_rounds": cfg.max_qa_rounds,
        "max_merge_attempts": cfg.max_merge_attempts,
        "max_holistic_rounds": cfg.max_holistic_rounds,
        "max_review_rounds": cfg.max_review_rounds,
        "default_target_branch": cfg.default_target_branch,
        "clones_dir": cfg.clones_dir,
        "cleanup_clones_on_merge": cfg.cleanup_clones_on_merge,
        "max_active_agents": cfg.max_active_agents,
        "dispatch_interval_seconds": cfg.dispatch_interval_seconds,
        "profiles": {name: _copy_profile(profile) for name, profile in cfg.profiles.items()},
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
        if key == "profiles":
            _merge_profiles(target, value)
            continue
        target[key] = value


def _merge_profiles(target: dict[str, Any], value: Any) -> None:
    if not isinstance(value, Mapping):
        raise ValueError("build config profiles must be a mapping")

    raw_profiles = target.setdefault("profiles", {})
    if not isinstance(raw_profiles, dict):
        raise ValueError("build config profiles must be a mapping")

    updates = _string_key_mapping(value, "profiles")
    for name, profile in updates.items():
        if not isinstance(profile, Mapping):
            raise ValueError(f"build profile must be a mapping: {name}")

        existing = raw_profiles.get(name, {})
        merged_profile: dict[str, Any] = {}
        if isinstance(existing, Mapping):
            merged_profile.update(_string_key_mapping(existing, f"profiles.{name}"))
        merged_profile.update(_string_key_mapping(profile, f"profiles.{name}"))
        raw_profiles[name] = merged_profile


def _build_config_from_mapping(raw: Mapping[str, Any]) -> BuildConfig:
    return BuildConfig(
        default_skip_stages=_normalize_stages(
            raw.get("default_skip_stages", ()), "default_skip_stages"
        ),
        default_isolation=_normalize_isolation(
            raw.get("default_isolation", "worktree"), "default_isolation"
        ),
        default_yolo=_normalize_bool(raw.get("default_yolo", False), "default_yolo"),
        max_expansion_attempts=_normalize_int(
            raw.get("max_expansion_attempts", 3), "max_expansion_attempts"
        ),
        max_qa_rounds=_normalize_int(raw.get("max_qa_rounds", 5), "max_qa_rounds"),
        max_merge_attempts=_normalize_int(raw.get("max_merge_attempts", 3), "max_merge_attempts"),
        max_holistic_rounds=_normalize_int(
            raw.get("max_holistic_rounds", 3), "max_holistic_rounds"
        ),
        max_review_rounds=_normalize_int(
            raw.get("max_review_rounds", raw.get("default_max_review_rounds", 3)),
            "max_review_rounds",
        ),
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
        profiles=_normalize_profiles(raw.get("profiles", {})),
    )


def _normalize_profiles(value: Any) -> dict[str, BuildProfile]:
    if not isinstance(value, Mapping):
        raise ValueError("profiles must be a mapping")

    profiles: dict[str, BuildProfile] = {}
    for name, raw_profile in _string_key_mapping(value, "profiles").items():
        if not isinstance(raw_profile, Mapping):
            raise ValueError(f"profile must be a mapping: {name}")
        profile = _string_key_mapping(raw_profile, f"profiles.{name}")
        profiles[name] = {
            "skip_stages": list(
                _normalize_stages(profile.get("skip_stages", ()), f"profiles.{name}.skip_stages")
            ),
            "isolation": _normalize_isolation(
                profile.get("isolation", "worktree"), f"profiles.{name}.isolation"
            ),
            "yolo": _normalize_bool(profile.get("yolo", False), f"profiles.{name}.yolo"),
        }
    return profiles


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


def _resolve_auto_profile_name(
    input_ref: str,
    input_kind: str | None,
    has_plan_file: bool,
) -> str:
    if input_kind == "plan_file" or _looks_like_plan_file(input_ref):
        return "review"
    if input_kind in (None, "leaf"):
        return "quick"
    if input_kind == "epic":
        if has_plan_file:
            return "full"
        raise ValueError(f"auto profile for epic {input_ref} requires a plan artifact")
    raise ValueError(f"unknown build input kind: {input_kind}")


def _looks_like_plan_file(input_ref: str) -> bool:
    path = Path(input_ref)
    if path.suffix != ".md":
        return False
    parts = path.parts
    try:
        return parts.index(".gobby") < parts.index("plans")
    except ValueError:
        return False


def _copy_profile(profile: BuildProfile) -> BuildProfile:
    return {
        "skip_stages": list(profile["skip_stages"]),
        "isolation": profile["isolation"],
        "yolo": profile["yolo"],
    }


__all__ = [
    "BuildConfig",
    "BuildProfile",
    "Isolation",
    "SKIPPABLE_STAGES",
    "SkippableStage",
    "load_build_config",
    "resolve_profile",
]
