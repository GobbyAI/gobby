-- Plan enhancement (interactive step 4.5 / autonomous pre-adversary sub-loop)
-- artifact counters. Mirrors the expansion_attempts integer pattern: a target
-- round count, a completed round count, and a convergence flag the dispatch
-- rule reads to decide whether to spawn another enhancer pass or fall through
-- to the unchanged adversary review.
ALTER TABLE task_artifacts
    ADD COLUMN IF NOT EXISTS plan_enhancement_rounds INTEGER NOT NULL DEFAULT 0;
ALTER TABLE task_artifacts
    ADD COLUMN IF NOT EXISTS plan_enhancement_rounds_completed INTEGER NOT NULL DEFAULT 0;
ALTER TABLE task_artifacts
    ADD COLUMN IF NOT EXISTS plan_enhancement_converged BOOLEAN NOT NULL DEFAULT FALSE;
