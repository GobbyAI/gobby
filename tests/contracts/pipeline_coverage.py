"""Coverage check for the hook pipeline corpus.

The handler-event axis is introspected from ``EventHandlers.get_handler_map``, the collaborator
axis is the manifest's collaborator registry, and the scenario axis is the manifest's required
scenario list. The check collects what the committed cases carry on the axis and raises
``ValueError`` naming every required item that no case carries.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from gobby.hooks.event_handlers import EventHandlers
from tests.contracts import pipeline_corpus

AXES = ("handler_event", "scenario", "collaborator_kind")


def _required(manifest: Mapping[str, Any], axis: str) -> list[str]:
    if axis == "handler_event":
        return sorted(event.value for event in EventHandlers().get_handler_map())
    if axis == "collaborator_kind":
        return [str(name) for name in manifest.get("collaborators", [])]
    return [str(name) for name in manifest.get("required", {}).get("scenario", [])]


def _carried(case: Mapping[str, Any], axis: str) -> str | None:
    if axis == "handler_event":
        return case.get("event_type")
    if axis == "scenario":
        return case.get("scenario")
    if case.get("kind") != "collaborator_failure":
        return None
    return case.get("failing_collaborator")


def check_coverage(corpus_dir: Path, axis: str) -> None:
    """Raise ``ValueError`` naming each required item on ``axis`` that no case carries."""
    if axis not in AXES:
        raise ValueError(f"unknown coverage axis {axis!r}")
    manifest, cases = pipeline_corpus.load_corpus(corpus_dir)
    required = _required(manifest, axis)
    if not required:
        raise ValueError(f"no required {axis} items")
    carried = {_carried(case, axis) for case in cases}
    missing = [item for item in required if item not in carried]
    if missing:
        raise ValueError(f"{axis} coverage: no case carries {missing}")
