"""SQL guard: a claim owner that may still resume keeps its claim.

Every sweep that takes a claim away from its owner ANDs this guard into its
ownership UPDATE, so the decision is made atomically with the write rather than
from a read that a hold, revival, or compaction can overtake.
"""

from datetime import datetime, timedelta
from typing import Any

from gobby.sessions.compact_markers import (
    HANDOFF_COMPACT_CONTINUE_FRESH_SECONDS,
    HANDOFF_COMPACT_CONTINUE_VARIABLE,
)
from gobby.sessions.contested_expiry import (
    CONTESTED_EXPIRY_CAUSES,
    CONTESTED_TERMINAL_EXPIRY_VARIABLE,
)
from gobby.storage.sessions._constants import LIVE_SESSION_STATUS_ORDER
from gobby.storage.sessions._operator_claim_hold import (
    LIVE_OPERATOR_CLAIM_HOLD_SQL,
    VALID_STAMP_SQL,
    live_operator_claim_hold_params,
    valid_stamp_params,
)

OWNER_MAY_RESUME_GUARD_SQL = f"""
    NOT EXISTS (
        SELECT 1 FROM sessions s
         WHERE s.id = tasks.claimed_by_session_id
           AND s.status = ANY(%s)
    )
    AND NOT EXISTS (
        -- A fresh compact-continue marker means the owner is mid-compaction
        -- and will resume; its lifecycle status can be transiently stale, so
        -- marker age (not claim age) decides eligibility here.
        SELECT 1
          FROM session_variables sv
         WHERE sv.session_id = tasks.claimed_by_session_id
           AND jsonb_typeof(sv.variables -> %s) = 'object'
           AND jsonb_typeof(sv.variables -> %s -> 'created_at') = 'string'
           AND sv.variables -> %s ->> 'created_at' >= %s
           AND sv.variables -> %s ->> 'created_at' <= %s
    )
    AND NOT EXISTS (
        -- SessionStart expires every terminal session sharing a reused
        -- terminal context before anything validates who owns the terminal;
        -- revive_expired_terminal_session settles that afterwards and
        -- routinely reverses it. That writer records the cause on the way out,
        -- so the owner's status is transiently stale in the same way a
        -- mid-compaction owner's is, and the claim outlives it. An expiry that
        -- left no marker -- inactivity, a killed tmux server, an explicit
        -- close -- is final and keeps the ordinary schedule, which is what
        -- stops this from shadowing the marker grace above for every other
        -- expired terminal session. session_variables is a shared store, so
        -- the cause has to name one of the two speculative writers: a fresh
        -- created_at left under this key by anything else is not a contest.
        SELECT 1
          FROM sessions s
          JOIN session_variables sv ON sv.session_id = s.id
         WHERE s.id = tasks.claimed_by_session_id
           AND s.session_type = 'terminal'
           AND s.status = 'expired'
           AND jsonb_typeof(sv.variables -> %s) = 'object'
           AND sv.variables -> %s ->> 'cause' = ANY(%s)
           AND {VALID_STAMP_SQL}
    )
    AND NOT EXISTS (
        -- An operator parked this seat on purpose (a directed CLI update,
        -- say), so the expiry that follows its exit is not a death. Status
        -- writes leave the hold alone; release or a proven resume clears it,
        -- and the revival horizon bounds it.
        SELECT 1
          FROM session_variables sv
         WHERE sv.session_id = tasks.claimed_by_session_id
           AND {LIVE_OPERATOR_CLAIM_HOLD_SQL}
    )"""


def owner_may_resume_guard_params(now: datetime) -> list[Any]:
    """Parameters for OWNER_MAY_RESUME_GUARD_SQL, in placeholder order."""
    compact_cutoff = now - timedelta(seconds=HANDOFF_COMPACT_CONTINUE_FRESH_SECONDS)
    compact = HANDOFF_COMPACT_CONTINUE_VARIABLE
    contested = CONTESTED_TERMINAL_EXPIRY_VARIABLE
    return [
        list(LIVE_SESSION_STATUS_ORDER),
        compact,
        compact,
        compact,
        compact_cutoff.isoformat(),
        compact,
        now.isoformat(),
        contested,
        contested,
        sorted(CONTESTED_EXPIRY_CAUSES),
        *valid_stamp_params(contested, now),
        *live_operator_claim_hold_params(now),
    ]


__all__ = ["OWNER_MAY_RESUME_GUARD_SQL", "owner_may_resume_guard_params"]
