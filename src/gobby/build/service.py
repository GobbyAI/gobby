"""Core build service types."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from gobby.config.build import Isolation


@dataclass
class BuildOptions:
    """Resolved options for a build request."""

    profile: str | None
    skip_stages: list[str]
    isolation: Isolation
    yolo: bool
    max_review_rounds: int
    target_branch: str | None = None
    assigned_agent: str | None = None
    clones_dir: Path | None = None


@dataclass
class BuildResult:
    """Summary returned by build service surfaces."""

    task_id: str
    created: bool
    initial_lifecycle: str
    applied_stages_skipped: list[str]
    tick_dispatched: int


__all__ = ["BuildOptions", "BuildResult"]
