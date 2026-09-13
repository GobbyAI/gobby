"""Read-only liveness derived from durable child-session observations."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any, TypedDict

from gobby.utils.datetime import parse_stored_datetime, utc_now

from ._constants import ACTIVE_AGENT_RUN_STATUSES

STALL_SECONDS = 600


class AgentLiveness(TypedDict):
    child_status: str | None
    wait_kind: str | None
    blocked_on_parent: bool | None
    last_progress_at: datetime | None
    progress_age_seconds: float | None
    stall_suspected: bool


def liveness_from_row(row: Mapping[str, Any]) -> AgentLiveness:
    # Import at the read boundary: the lifecycle reducer itself uses storage.
    from gobby.sessions.turn_lifecycle import TurnLifecycleState

    child_status = row.get("child_status")
    payload = row.get("child_lifecycle_payload")
    if isinstance(payload, str):
        payload = json.loads(payload)
    lifecycle = TurnLifecycleState.from_payload(payload if isinstance(payload, Mapping) else None)
    kinds = {wait.kind for wait in lifecycle.waits} if child_status is not None else set()
    wait_kind = next((kind for kind in ("input", "approval", "handoff") if kind in kinds), None)
    coordination = row.get("coordination_wait") is True
    agent_wait = row.get("agent_wait") is True
    if wait_kind is None and child_status is not None:
        wait_kind = "coordination" if coordination else "agent" if agent_wait else None
    blocked_on_parent = None if child_status is None else row.get("parent_wait") is True
    started = parse_stored_datetime(row.get("started_at"))
    activity = (
        parse_stored_datetime(row.get("child_last_activity")) if child_status is not None else None
    )
    completed = parse_stored_datetime(row.get("completed_at"))
    # A resumed child may outlive this run; its later activity is not this run's progress.
    if activity is not None and completed is not None:
        activity = min(activity, completed)
    progress = max((value for value in (started, activity) if value is not None), default=None)
    age = max(0.0, (utc_now() - progress).total_seconds()) if progress is not None else None
    return AgentLiveness(
        child_status=child_status,
        wait_kind=wait_kind,
        blocked_on_parent=blocked_on_parent,
        last_progress_at=progress,
        progress_age_seconds=age,
        stall_suspected=(
            row.get("status") in ACTIVE_AGENT_RUN_STATUSES
            and wait_kind is None
            and age is not None
            and age >= STALL_SECONDS
        ),
    )
