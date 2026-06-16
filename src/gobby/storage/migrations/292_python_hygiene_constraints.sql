-- Add integrity constraints for Python hygiene triage fixes.

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM pg_constraint
         WHERE conname = 'session_summary_revisions_generation_mode_valid'
           AND conrelid = 'session_summary_revisions'::regclass
    ) THEN
        ALTER TABLE session_summary_revisions
            ADD CONSTRAINT session_summary_revisions_generation_mode_valid
            CHECK (
                generation_mode IN (
                    'agent_authored',
                    'full',
                    'delta',
                    'digest_fallback',
                    'noop'
                )
            );
    END IF;
END $$;

ALTER TABLE task_validation_backoff
    DROP CONSTRAINT IF EXISTS task_validation_backoff_task_id_fkey;

ALTER TABLE task_validation_backoff
    ADD CONSTRAINT task_validation_backoff_task_id_fkey
    FOREIGN KEY (task_id)
    REFERENCES tasks(id)
    ON DELETE CASCADE
    DEFERRABLE INITIALLY IMMEDIATE
    NOT VALID;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM pg_constraint
         WHERE conname = 'task_artifacts_plan_enhancement_rounds_nonnegative'
           AND conrelid = 'task_artifacts'::regclass
    ) THEN
        ALTER TABLE task_artifacts
            ADD CONSTRAINT task_artifacts_plan_enhancement_rounds_nonnegative
            CHECK (plan_enhancement_rounds >= 0);
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM pg_constraint
         WHERE conname = 'task_artifacts_plan_enhancement_rounds_completed_nonnegative'
           AND conrelid = 'task_artifacts'::regclass
    ) THEN
        ALTER TABLE task_artifacts
            ADD CONSTRAINT task_artifacts_plan_enhancement_rounds_completed_nonnegative
            CHECK (plan_enhancement_rounds_completed >= 0);
    END IF;
END $$;
