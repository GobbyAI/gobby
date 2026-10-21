"""Shared legality and terminal-stage rules for stage transitions."""

from gobby.storage.hub.protocol import Transaction
from gobby.storage.tasks._stage_types import IllegalStageTransitionError, StageState


def illegal(row: StageState, verb: str) -> IllegalStageTransitionError:
    return IllegalStageTransitionError(
        row.stage_name,
        row.state,
        verb,
        row.review_policy,
    )


def terminal_after_done(conn: Transaction, task_id: str, stage_name: str) -> bool:
    row = conn.execute(
        """
        SELECT COUNT(*) AS count
          FROM task_stage_states
         WHERE task_id = %s
           AND state != 'done'
           AND stage_name != %s
        """,
        (task_id, stage_name),
    ).fetchone()
    return bool(row is not None and int(row["count"]) == 0)
