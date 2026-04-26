"""Build pipeline configuration types."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, TypedDict

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
    default_max_review_rounds: int = 3
    default_target_branch: str | None = None
    clones_dir: Path = field(default_factory=_default_clones_dir)
    cleanup_clones_on_merge: bool = True
    max_active_agents: int = 10
    dispatch_interval_seconds: int = 60
    profiles: dict[str, BuildProfile] = field(default_factory=_default_profiles)


__all__ = [
    "BuildConfig",
    "BuildProfile",
    "Isolation",
    "SKIPPABLE_STAGES",
    "SkippableStage",
]
