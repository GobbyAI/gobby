"""Intra-epic blockers that a finished leaf resolves inside an active epic team (redesign 8.3).

A blocker resolves for its dependents when both hold:

- the blocker and the blocked task share an ancestor epic with a live epic-mode team; and
- the blocker's latest current-epoch implementation record is approved and attests its tip:
  the record's commit is the blocker's last linked commit, or it is null while the blocker
  has no linked commits.

``approved_task_step`` in ``gobby.tasks.step_records`` is the Python contract for the same
rows. The projection in ``_queries`` and the hydration in ``_blocking`` both embed this
predicate, so they cannot drift apart.
"""

from gobby.storage.team_records import EPIC_MODE, live_team_rows_sql


def team_resolved_blocker_sql(blocker: str, blocked: str) -> str:
    """SQL true when the ``blocker`` task alias resolves the ``blocked`` task alias."""
    tip = f"({blocker}.commits ->> (jsonb_array_length({blocker}.commits) - 1))"
    return f"""(
        EXISTS (
            WITH RECURSIVE blocked_up(id) AS (
                SELECT parent_task_id FROM tasks
                WHERE id = {blocked}.id AND parent_task_id IS NOT NULL
                UNION
                SELECT p.parent_task_id FROM tasks p
                JOIN blocked_up b ON p.id = b.id
                WHERE p.parent_task_id IS NOT NULL
            ),
            blocker_up(id) AS (
                SELECT parent_task_id FROM tasks
                WHERE id = {blocker}.id AND parent_task_id IS NOT NULL
                UNION
                SELECT p.parent_task_id FROM tasks p
                JOIN blocker_up b ON p.id = b.id
                WHERE p.parent_task_id IS NOT NULL
            ),
            live AS ({live_team_rows_sql()})
            SELECT 1 FROM blocked_up
            JOIN blocker_up ON blocker_up.id = blocked_up.id
            JOIN live ON live.task_id = blocked_up.id::text AND live.mode = '{EPIC_MODE}'
        )
        AND EXISTS (
            SELECT 1 FROM (
                SELECT steps.record
                FROM (
                    SELECT c.body::jsonb AS record
                    FROM task_comments c
                    WHERE c.task_id = {blocker}.id AND c.author_type = 'task_step'
                ) steps
                WHERE steps.record ->> 'step' = 'implementation'
                  AND (steps.record ->> 'epoch')::int = {blocker}.review_epoch
                  AND steps.record ->> 'agent_definition' <> 'security-reviewer'
                ORDER BY (steps.record ->> 'sequence')::int DESC
                LIMIT 1
            ) latest
            WHERE latest.record ->> 'status' = 'approved'
              AND (
                  (latest.record ->> 'commit_sha' IS NULL
                   AND COALESCE(jsonb_array_length({blocker}.commits), 0) = 0)
                  OR (latest.record ->> 'commit_sha' IS NOT NULL
                      AND COALESCE(jsonb_array_length({blocker}.commits), 0) > 0
                      AND left(latest.record ->> 'commit_sha', length({tip})) = {tip})
              )
        )
    )"""
