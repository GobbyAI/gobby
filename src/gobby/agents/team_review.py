"""Read explicit task holds for admitted teams without synthesizing review phases."""

from __future__ import annotations

from gobby.agents.team_admission_records import TeamBinding, team_binding
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.team_records import EPIC_MODE


def seat_team_held(db: HubDatabase, binding: TeamBinding) -> bool:
    """Whether an epic team is held, read from the store without persisting anything.

    A leaf's rejection or dependency blocks that leaf, not the whole team.
    """
    if binding.parent_team_id is not None:
        parent = team_binding(db, binding.parent_team_id)
        if parent is None:
            return True
        binding = parent
    if binding.mode != EPIC_MODE:
        return False
    epic = LocalTaskManager(db).get_task(binding.task_id)
    return epic.escalated_at is not None
