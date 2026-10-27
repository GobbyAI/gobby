"""Review status derived from a task's review step records, read at its latest linked commit."""

from __future__ import annotations


def review_status_sql(task: str) -> str:
    """SQL for the review status the task's current-epoch review record implies.

    NULL when the epoch has no review record, so callers fall back to the stored copy.
    An approval counts only at the latest linked commit, as ``approved_task_step`` does,
    and a rejection stands whatever the commit, as the step writer records it.
    A row whose epoch or sequence is not a plain integer is no review: the epoch compares
    as text and the sequence is filtered before its cast, so the read never raises.
    """
    commits = f"{task}.commits"
    tip = f"({commits} ->> (jsonb_array_length({commits}) - 1))"
    return f"""(
        SELECT CASE
            WHEN latest.record ->> 'status' = 'rejected' THEN 'invalid'
            WHEN latest.record ->> 'status' = 'approved'
              AND (
                  (latest.record ->> 'commit_sha' IS NULL
                   AND COALESCE(jsonb_array_length({commits}), 0) = 0)
                  OR (latest.record ->> 'commit_sha' IS NOT NULL
                      AND COALESCE(jsonb_array_length({commits}), 0) > 0
                      AND left(latest.record ->> 'commit_sha', length({tip})) = {tip})
              ) THEN 'valid'
            ELSE 'pending'
        END
        FROM (
            SELECT steps.record
            FROM (
                SELECT c.body::jsonb AS record
                FROM task_comments c
                WHERE c.task_id = {task}.id AND c.author_type = 'task_step'
            ) steps
            WHERE steps.record ->> 'step' = 'review'
              AND steps.record ->> 'agent_definition' <> 'security-reviewer'
              AND steps.record ->> 'epoch' = {task}.review_epoch::text
              AND steps.record ->> 'sequence' ~ '^[0-9]{{1,9}}$'
            ORDER BY (steps.record ->> 'sequence')::int DESC
            LIMIT 1
        ) latest
    )"""
