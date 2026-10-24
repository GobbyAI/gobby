"""Epic-mode review state for an epic-bound team (addendum 8.4).

The review loop derives every leaf's state from tasks, step records and Git; nothing here
recounts transcripts. ``step_records.py`` stays team-agnostic.
"""

from __future__ import annotations

from dataclasses import dataclass

from gobby.storage.hub.protocol import HubDatabase


@dataclass(frozen=True)
class EpicReviewState:
    """An epic team's review state as of one pass.

    ``holds`` names each task holding the epic: an escalated leaf, an externally blocked
    leaf, or the escalated epic itself. ``next_reviewers`` maps each reviewed leaf to the
    reviewer definition whose seat reviews its next round. ``epic_tip_ready`` is true when
    every leaf has a current approval and a linked epic tip awaits its review round.
    """

    held: bool
    holds: tuple[str, ...]
    next_reviewers: dict[str, str]
    epic_tip_ready: bool


def sync_epic_review(db: HubDatabase, team_id: str) -> EpicReviewState:
    """Derive team ``team_id``'s review state; escalate its epic once on a third rejection."""
    raise NotImplementedError
