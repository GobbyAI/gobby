-- Intrinsic holds are data. Routing and scheduled release belong to the
-- separate task-lifecycle feature, not to a time-dependent state projection.
ALTER TABLE tasks
    ADD COLUMN hold_state text,
    ADD COLUMN follow_up_at timestamp with time zone,
    ADD CONSTRAINT tasks_hold_state_check
        CHECK (hold_state IS NULL OR hold_state IN ('needs_human', 'deferred')),
    ADD CONSTRAINT tasks_deferred_follow_up_check
        CHECK (hold_state IS DISTINCT FROM 'deferred' OR follow_up_at IS NOT NULL);

-- Keep the original close text before replacing the former free-form reasons.
INSERT INTO task_comments (id, task_id, author, author_type, body)
SELECT gen_random_uuid(), id, 'migration:477', 'system',
       jsonb_build_object(
           'kind', 'legacy_close_reason_snapshot',
           'closed_reason', closed_reason,
           'closed_at', closed_at,
           'updated_at', updated_at
       )::text
FROM tasks
WHERE closed_at IS NOT NULL
  AND (closed_reason IS NULL OR closed_reason NOT IN ('done', 'moot', 'superseded', 'wont_do'));

UPDATE tasks SET closed_reason = CASE
    WHEN closed_reason IN ('obsolete', 'moot') THEN 'moot'
    WHEN closed_reason IN ('duplicate', 'superseded') THEN 'superseded'
    WHEN closed_reason IN ('wont_fix', 'out_of_repo', 'wont_do') THEN 'wont_do'
    ELSE 'done'
END
WHERE closed_at IS NOT NULL;
ALTER TABLE tasks ADD CONSTRAINT tasks_closed_reason_check
    CHECK (closed_at IS NULL OR (
        closed_reason IS NOT NULL AND closed_reason IN ('done', 'moot', 'superseded', 'wont_do')
    ));

CREATE OR REPLACE FUNCTION compute_task_state_bucket(p_task_id uuid) RETURNS text
    LANGUAGE sql STABLE
    AS $$
    SELECT CASE
        WHEN t.closed_at IS NOT NULL THEN 'closed'
        WHEN t.escalated_at IS NOT NULL OR COALESCE(t.is_escalated, FALSE) IS TRUE THEN 'escalated'
        WHEN t.hold_state IS NOT NULL THEN t.hold_state
        WHEN COALESCE(t.labels, '[]'::jsonb) @> '["awaiting-human-review"]'::jsonb
            THEN 'needs_human'
        WHEN t.claimed_by_session_id IS NOT NULL THEN 'in_progress'
        ELSE 'open'
    END
    FROM tasks t
    WHERE t.id = p_task_id
$$;

ALTER TABLE tasks DROP CONSTRAINT tasks_state_bucket_check;
ALTER TABLE tasks ALTER COLUMN state_bucket SET DEFAULT 'open';
UPDATE tasks SET state_bucket = compute_task_state_bucket(id);
ALTER TABLE tasks ADD CONSTRAINT tasks_state_bucket_check
    CHECK (state_bucket IN ('open', 'in_progress', 'needs_human', 'escalated', 'deferred', 'closed'));

DROP TRIGGER tasks_state_bucket_au ON tasks;
CREATE TRIGGER tasks_state_bucket_au
    AFTER UPDATE OF closed_at, escalated_at, is_escalated, claimed_by_session_id, hold_state, labels
    ON tasks FOR EACH ROW EXECUTE FUNCTION refresh_task_state_bucket_from_task();
