-- Split memory exposure from access and retire the recall-signal stack (#22840).
-- Surfacing counts move to surfaced_count/last_surfaced_at; access restarts at zero.
-- Plain migration (no destructive directive) so fresh lineages execute the drop too.
ALTER TABLE memories
    ADD COLUMN surfaced_count integer DEFAULT 0,
    ADD COLUMN last_surfaced_at timestamp with time zone;

UPDATE memories
SET surfaced_count = access_count,
    last_surfaced_at = last_accessed_at,
    access_count = 0,
    last_accessed_at = NULL;

-- Qualify every target to the runner's schema: IF EXISTS must never fall through
-- search_path into another schema. RESTRICT refuses unrecorded outside consumers;
-- the recall_holdout_consumed -> recall_gate_runs FK resolves inside one statement.
DO $retire_recall_signals$
BEGIN
    EXECUTE format(
        'DROP TABLE IF EXISTS %1$I.recall_gate_runs, %1$I.recall_holdout_consumed, '
        '%1$I.recall_injection_outcomes, %1$I.recall_shadow_audit_verdicts, '
        '%1$I.recall_shadow_judge_state, %1$I.recall_shadow_prompt_snapshot, '
        '%1$I.recall_signal_hits, %1$I.recall_signal_requests, '
        '%1$I.recall_usefulness RESTRICT',
        current_schema()
    );
END
$retire_recall_signals$;
