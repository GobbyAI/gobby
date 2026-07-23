-- Allow a failed plan-review spawn to re-prepare the same logical round while
-- retaining the expired evidence row as immutable audit history.
DROP INDEX IF EXISTS idx_plan_review_evidence_interactive_round;
CREATE UNIQUE INDEX idx_plan_review_evidence_interactive_round
ON plan_review_evidence(session_id, plan_path, round_number)
WHERE session_id IS NOT NULL AND expired_at IS NULL;

DROP INDEX IF EXISTS idx_plan_review_evidence_stage_round;
CREATE UNIQUE INDEX idx_plan_review_evidence_stage_round
ON plan_review_evidence(task_id, stage, round_number)
WHERE task_id IS NOT NULL AND expired_at IS NULL;
