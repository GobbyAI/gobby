"""Build pipeline configuration types."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, TypedDict, cast

import yaml

Isolation = Literal["none", "worktree", "clone"]
SkippableStage = Literal["plan_review", "test_arch", "expanding", "qa", "holistic_review", "pr"]
InputKind = Literal["plan_file", "leaf", "epic"]

SKIPPABLE_STAGES: frozenset[SkippableStage] = cast(
    frozenset[SkippableStage],
    frozenset({"plan_review", "test_arch", "expanding", "qa", "holistic_review", "pr"}),
)


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

_VALID_ISOLATIONS = frozenset({"none", "worktree", "clone"})


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
    default_max_review_rounds: int = 3
    default_target_branch: str | None = None
    clones_dir: Path = field(default_factory=_default_clones_dir)
    cleanup_clones_on_merge: bool = True
    max_active_agents: int = 10
    dispatch_interval_seconds: int = 60
    profiles: dict[str, BuildProfile] = field(default_factory=_default_profiles)


def load_build_config(
    project_root: str | Path | None = None,
    flag_overrides: Mapping[str, object] | None = None,
) -> BuildConfig:
    """Load build config from defaults, global YAML, project YAML, and explicit overrides."""

    cfg = BuildConfig()
    cfg = _apply_overrides(cfg, _load_yaml_mapping(Path.home() / ".gobby" / "build.yaml"))

    if project_root is not None:
        cfg = _apply_overrides(
            cfg, _load_yaml_mapping(Path(project_root) / ".gobby" / "build.yaml")
        )

    if flag_overrides:
        cfg = _apply_overrides(cfg, flag_overrides)

    return cfg


def resolve_profile(
    cfg: BuildConfig,
    name: str,
    input_ref: str,
    *,
    input_kind: InputKind | None = None,
    has_plan_file: bool | None = None,
) -> BuildProfile:
    """Resolve a named build profile, including the input-sensitive auto profile."""

    profile_name = (
        _resolve_auto_profile(input_ref, input_kind, has_plan_file) if name == "auto" else name
    )
    try:
        profile = cfg.profiles[profile_name]
    except KeyError as exc:
        raise ValueError(f"Unknown build profile: {profile_name}") from exc
    return _copy_profile(profile)


def _resolve_auto_profile(
    input_ref: str,
    input_kind: InputKind | None,
    has_plan_file: bool | None,
) -> str:
    kind = input_kind or _infer_input_kind(input_ref)
    if kind == "plan_file":
        return "review"
    if kind == "leaf":
        return "quick"
    if has_plan_file:
        return "full"
    raise ValueError("auto profile for an epic requires an existing plan artifact")


def _infer_input_kind(input_ref: str) -> InputKind:
    if input_ref.startswith("#"):
        return "leaf"
    return "plan_file"


def _apply_overrides(cfg: BuildConfig, overrides: Mapping[str, object]) -> BuildConfig:
    default_skip_stages = cfg.default_skip_stages
    default_isolation = cfg.default_isolation
    default_yolo = cfg.default_yolo
    default_max_review_rounds = cfg.default_max_review_rounds
    default_target_branch = cfg.default_target_branch
    clones_dir = cfg.clones_dir
    cleanup_clones_on_merge = cfg.cleanup_clones_on_merge
    max_active_agents = cfg.max_active_agents
    dispatch_interval_seconds = cfg.dispatch_interval_seconds
    profiles = {name: _copy_profile(profile) for name, profile in cfg.profiles.items()}

    for key, value in overrides.items():
        if key == "default_skip_stages":
            default_skip_stages = tuple(_coerce_str_sequence(value, key))
        elif key == "default_isolation":
            default_isolation = _coerce_isolation(value, key)
        elif key == "default_yolo":
            default_yolo = _coerce_bool(value, key)
        elif key == "default_max_review_rounds":
            default_max_review_rounds = _coerce_int(value, key)
        elif key == "default_target_branch":
            default_target_branch = _coerce_optional_str(value, key)
        elif key == "clones_dir":
            clones_dir = _coerce_path(value, key)
        elif key == "cleanup_clones_on_merge":
            cleanup_clones_on_merge = _coerce_bool(value, key)
        elif key == "max_active_agents":
            max_active_agents = _coerce_int(value, key)
        elif key == "dispatch_interval_seconds":
            dispatch_interval_seconds = _coerce_int(value, key)
        elif key == "profiles":
            profiles.update(_coerce_profiles(value))

    return BuildConfig(
        default_skip_stages=default_skip_stages,
        default_isolation=default_isolation,
        default_yolo=default_yolo,
        default_max_review_rounds=default_max_review_rounds,
        default_target_branch=default_target_branch,
        clones_dir=clones_dir,
        cleanup_clones_on_merge=cleanup_clones_on_merge,
        max_active_agents=max_active_agents,
        dispatch_interval_seconds=dispatch_interval_seconds,
        profiles=profiles,
    )


def _load_yaml_mapping(path: Path) -> Mapping[str, object]:
    if not path.exists():
        return {}

    raw = yaml.safe_load(path.read_text())
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"Build config must be a mapping: {path}")
    return cast(Mapping[str, object], raw)


def _coerce_profiles(value: object) -> dict[str, BuildProfile]:
    raw_profiles = _coerce_mapping(value, "profiles")
    profiles: dict[str, BuildProfile] = {}

    for name, raw_profile in raw_profiles.items():
        profile = _coerce_mapping(raw_profile, f"profiles.{name}")
        profiles[name] = {
            "skip_stages": _coerce_str_sequence(
                profile.get("skip_stages", ()), f"{name}.skip_stages"
            ),
            "isolation": _coerce_isolation(
                profile.get("isolation", "worktree"), f"{name}.isolation"
            ),
            "yolo": _coerce_bool(profile.get("yolo", False), f"{name}.yolo"),
        }

    return profiles


def _coerce_mapping(value: object, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be a mapping")
    for key in value:
        if not isinstance(key, str):
            raise ValueError(f"{field_name} keys must be strings")
    return cast(Mapping[str, object], value)


def _coerce_str_sequence(value: object, field_name: str) -> list[str]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise ValueError(f"{field_name} must be a sequence of strings")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError(f"{field_name} entries must be strings")
        result.append(item)
    return result


def _coerce_isolation(value: object, field_name: str) -> Isolation:
    if not isinstance(value, str) or value not in _VALID_ISOLATIONS:
        valid = ", ".join(sorted(_VALID_ISOLATIONS))
        raise ValueError(f"{field_name} must be one of: {valid}")
    return cast(Isolation, value)


def _coerce_bool(value: object, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field_name} must be a boolean")
    return value


def _coerce_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be an integer")
    return value


def _coerce_optional_str(value: object, field_name: str) -> str | None:
    if value is None or isinstance(value, str):
        return value
    raise ValueError(f"{field_name} must be a string or null")


def _coerce_path(value: object, field_name: str) -> Path:
    if isinstance(value, str | Path):
        return Path(value).expanduser()
    raise ValueError(f"{field_name} must be a path string")


def _copy_profile(profile: BuildProfile) -> BuildProfile:
    return {
        "skip_stages": list(profile["skip_stages"]),
        "isolation": profile["isolation"],
        "yolo": profile["yolo"],
    }


__all__ = [
    "BuildConfig",
    "BuildProfile",
    "InputKind",
    "Isolation",
    "SKIPPABLE_STAGES",
    "SkippableStage",
    "load_build_config",
    "resolve_profile",
]
