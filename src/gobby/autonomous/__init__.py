"""Autonomous execution infrastructure for Gobby.

This module provides infrastructure for autonomous task execution including:
- Stop signal management for graceful shutdown
- Progress tracking for detecting stagnation
- Stuck detection for breaking out of loops
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from gobby.autonomous.progress_tracker import ProgressEvent as ProgressEvent
    from gobby.autonomous.progress_tracker import ProgressSummary as ProgressSummary
    from gobby.autonomous.progress_tracker import ProgressTracker as ProgressTracker
    from gobby.autonomous.progress_tracker import ProgressType as ProgressType
    from gobby.autonomous.stop_registry import StopRegistry as StopRegistry
    from gobby.autonomous.stop_registry import StopSignal as StopSignal
    from gobby.autonomous.stuck_detector import StuckDetectionResult as StuckDetectionResult
    from gobby.autonomous.stuck_detector import StuckDetector as StuckDetector
    from gobby.autonomous.stuck_detector import TaskSelectionEvent as TaskSelectionEvent

__all__ = [
    "ProgressEvent",
    "ProgressSummary",
    "ProgressTracker",
    "ProgressType",
    "StopRegistry",
    "StopSignal",
    "StuckDetectionResult",
    "StuckDetector",
    "TaskSelectionEvent",
]

_EXPORTS = {
    "ProgressEvent": ("gobby.autonomous.progress_tracker", "ProgressEvent"),
    "ProgressSummary": ("gobby.autonomous.progress_tracker", "ProgressSummary"),
    "ProgressTracker": ("gobby.autonomous.progress_tracker", "ProgressTracker"),
    "ProgressType": ("gobby.autonomous.progress_tracker", "ProgressType"),
    "StopRegistry": ("gobby.autonomous.stop_registry", "StopRegistry"),
    "StopSignal": ("gobby.autonomous.stop_registry", "StopSignal"),
    "StuckDetectionResult": ("gobby.autonomous.stuck_detector", "StuckDetectionResult"),
    "StuckDetector": ("gobby.autonomous.stuck_detector", "StuckDetector"),
    "TaskSelectionEvent": ("gobby.autonomous.stuck_detector", "TaskSelectionEvent"),
}


def __getattr__(name: str) -> Any:
    try:
        module_name, attr_name = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc

    value = getattr(import_module(module_name), attr_name)
    globals()[name] = value
    return value
