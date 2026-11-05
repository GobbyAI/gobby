"""Live-wake policy for fanout sends.

A global or project send commits one durable row per recipient. A recipient parked
between turns (idle, paused or waiting) that holds no open claimed task owes no live
wake: its row waits for the next natural turn, so restart and cutover notices do not
run a turn in every idle seat. Direct session, parent and agent sends keep their wake.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import TYPE_CHECKING

from gobby.storage.sessions._constants import TURN_ENDED_SESSION_STATUSES

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase


def parked_without_claim_session_ids(db: HubDatabase, session_ids: Sequence[str]) -> frozenset[str]:
    """Return the recipients parked between turns that hold no open claimed task."""
    if not session_ids:
        return frozenset()
    rows = db.fetchall(
        """
        SELECT s.id
          FROM sessions s
         WHERE s.id::text = ANY(%s)
           AND s.status = ANY(%s)
           AND NOT EXISTS (
               SELECT 1
                 FROM tasks t
                WHERE t.claimed_by_session_id = s.id
                  AND t.closed_at IS NULL
           )
        """,
        (list(session_ids), sorted(TURN_ENDED_SESSION_STATUSES)),
    )
    return frozenset(str(row["id"]) for row in rows)


def is_fanout_metadata(metadata_json: str | None) -> bool:
    """Whether a committed mailbox row came from a global or project send."""
    if not metadata_json:
        return False
    payload = json.loads(metadata_json)
    return isinstance(payload, dict) and payload.get("broadcast_id") is not None
