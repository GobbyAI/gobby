"""The live-team definition that blocker resolution and team admission share.

A team is live while its admit step execution holds the team record, the record has no
teardown outcome, and its worktree row is active. Team admission reads these rows through
``gobby.agents.team_scope``; blocker resolution embeds the same SQL through
``gobby.storage.tasks._team_blockers``. The SQL is written once, here.
"""

ADMISSION_STEP = "admit"
EPIC_MODE = "epic"


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
          AND w.status = 'active'
    """
