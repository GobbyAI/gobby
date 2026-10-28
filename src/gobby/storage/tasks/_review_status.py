"""Review status derived from a task's review step records, read at its closure or latest commit."""

from __future__ import annotations


def review_status_sql(task: str) -> str:
    """SQL for the review status the task's current-epoch review record implies.

    NULL when the epoch has no review record, so callers fall back to the stored copy.
    An approval counts at ``closed_commit_sha`` once the task is closed, and while it is
    open only at the latest linked commit, as ``approved_task_step`` does for open tasks.
    ``approved_task_step`` still reads the tip for closed tasks. Reopening clears
    ``closed_commit_sha``, so a non-NULL value means the task is closed. A rejection stands whatever the commit, as the step
    writer records it. A row whose epoch or sequence is not a plain integer is no review:
    the epoch compares as text and the sequence is filtered before its cast, so the read
    never raises.
    """
    commits = f"{task}.commits"
    tip = f"({commits} ->> (jsonb_array_length({commits}) - 1))"
    target = f"COALESCE({task}.closed_commit_sha, {tip})"
    sha = "latest.record ->> 'commit_sha'"
    return f"""(
        SELECT CASE
            WHEN latest.record ->> 'status' = 'rejected' THEN 'invalid'
            WHEN latest.record ->> 'status' = 'approved'
              AND (
                  ({sha} IS NULL
                   AND COALESCE(jsonb_array_length({commits}), 0) = 0)
                  OR ({sha} IS NOT NULL
                      AND (lower({sha}) = lower({target})
                           OR (length({sha}) >= 7 AND length({target}) >= 7
                               AND (starts_with(lower({sha}), lower({target}))
                                    OR starts_with(lower({target}), lower({sha}))))))
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
