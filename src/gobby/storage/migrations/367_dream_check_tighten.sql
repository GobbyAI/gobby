-- gobby:destructive
-- Retire snapshots produced by the removed merge/supersede dream actions.

ALTER TABLE memory_dream_runs
    DROP CONSTRAINT IF EXISTS memory_dream_runs_status_check;
ALTER TABLE memory_dream_runs
    ADD CONSTRAINT memory_dream_runs_status_check
    CHECK (
        status IN (
            'started', 'running', 'completed', 'failed', 'reverted',
            'revert_failed', 'revert_forfeited', 'interrupted', 'partial'
        )
    );

WITH affected_runs AS MATERIALIZED (
    SELECT DISTINCT run_id
      FROM memory_dream_snapshots
     WHERE action IN ('merge', 'supersede')
), forfeited_runs AS (
    UPDATE memory_dream_runs
       SET status = 'revert_forfeited',
           completed_at = COALESCE(completed_at, NOW()),
           updated_at = NOW()
     WHERE id IN (SELECT run_id FROM affected_runs)
    RETURNING id
)
DELETE FROM memory_dream_snapshots
 WHERE run_id IN (SELECT id FROM forfeited_runs);

ALTER TABLE memory_dream_snapshots
    DROP CONSTRAINT IF EXISTS memory_dream_snapshots_action_check;
ALTER TABLE memory_dream_snapshots
    ADD CONSTRAINT memory_dream_snapshots_action_check
    CHECK (action IN ('keep', 'delete', 'refresh', 'review', 'promote'));
