"""Shared projections for stage-native task dataclasses."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from gobby.storage.tasks._stage_registry import StageRegistryEntry
from gobby.storage.tasks._stage_states import StageState

_OPERATION_STATE_OMITS = frozenset(
    {
        "entered_at",
        "entered_by_session_id",
        "completed_at",
        "completed_by_session_id",
        "completed_commit_sha",
    }
)


def stage_state_view(stage: StageState) -> dict[str, Any]:
    """Return the full API/MCP projection for a task stage row."""
    return asdict(stage)


def stage_state_operation_view(stage: StageState) -> dict[str, Any]:
    """Return the compact projection used by mutating MCP stage operations."""
    return {
        key: value
        for key, value in stage_state_view(stage).items()
        if key not in _OPERATION_STATE_OMITS
    }


def stage_registry_entry_view(entry: StageRegistryEntry) -> dict[str, Any]:
    """Return the API/MCP projection for a stage registry entry."""
    return asdict(entry)
