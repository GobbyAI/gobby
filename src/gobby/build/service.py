"""Compatibility facade for build lifecycle automation entry points."""

from __future__ import annotations

from gobby.build.dispatch_tick import DispatcherTickSummary
from gobby.build.lifecycle import build
from gobby.build.options import BuildIsolationResolution, BuildOptions, resolve_build_isolation
from gobby.build.project_controls import build_resume, build_stop
from gobby.build.results import BuildControlResult, BuildLifecycleEvent, BuildResult
from gobby.build.stage_manifest import AUTOMATED_LEAF_CATEGORIES, resolve_stage_manifest_specs

__all__ = [
    "AUTOMATED_LEAF_CATEGORIES",
    "BuildControlResult",
    "BuildIsolationResolution",
    "DispatcherTickSummary",
    "BuildLifecycleEvent",
    "BuildOptions",
    "BuildResult",
    "build",
    "build_resume",
    "build_stop",
    "resolve_stage_manifest_specs",
    "resolve_build_isolation",
]
