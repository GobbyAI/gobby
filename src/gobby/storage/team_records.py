"""The live-team definition that team admission and claim authority share.

A team is live while its admit step execution holds the team record, the record has no
teardown outcome, and its worktree row is live: active, or merged by a landing while the
open task's team keeps working. ``gobby.agents.team_scope`` reads these rows and checks
trees against the same ``LIVE_TREE_STATUSES``. The SQL is written once, here.
"""

from gobby.storage.worktrees import WorktreeStatus

ADMISSION_STEP = "admit"
EPIC_MODE = "epic"
LIVE_TREE_STATUSES = frozenset({WorktreeStatus.ACTIVE.value, WorktreeStatus.MERGED.value})
_LIVE_TREE_STATUS_SQL = ", ".join(f"'{status}'" for status in sorted(LIVE_TREE_STATUSES))


def live_team_rows_sql() -> str:
    """Live team records as ``admit_id``, ``team_id``, ``task_id``, ``mode`` and ``worktree_id``."""
    return f"""
        SELECT se.id AS admit_id,
               se.output_json #>> '{{team,team_id}}' AS team_id,
               se.output_json #>> '{{team,task_id}}' AS task_id,
               se.output_json #>> '{{team,mode}}' AS mode,
               se.output_json #>> '{{team,worktree_id}}' AS worktree_id
        FROM step_executions se
        JOIN worktrees w ON w.id::text = se.output_json #>> '{{team,worktree_id}}'
        WHERE se.step_id = '{ADMISSION_STEP}'
          AND se.output_json #>> '{{team,team_id}}' = se.execution_id::text
          AND se.output_json #>> '{{teardown,outcome}}' IS NULL
          AND w.status IN ({_LIVE_TREE_STATUS_SQL})
    """
