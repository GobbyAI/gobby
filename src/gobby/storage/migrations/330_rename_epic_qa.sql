-- Rename the epic-level review agent and stage before the 0.5.0 schema ships.
-- Stage foreign keys are deferrable so the registry key and all references can
-- move atomically inside the migration runner transaction.
SET CONSTRAINTS ALL DEFERRED;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'task_artifacts'
          AND column_name = 'holistic_attempts'
    ) AND NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'task_artifacts'
          AND column_name = 'epic_qa_attempts'
    ) THEN
        ALTER TABLE task_artifacts
        RENAME COLUMN holistic_attempts TO epic_qa_attempts;
    END IF;
END
$$;

UPDATE task_stage_states
SET stage_name = 'epic_qa'
WHERE stage_name = 'holistic_qa';

UPDATE task_type_default_stages
SET stage_name = 'epic_qa'
WHERE stage_name = 'holistic_qa';

UPDATE task_stages_registry
SET name = 'epic_qa',
    display_label = 'Epic QA',
    description = 'Whole-epic review after every leaf is parked.',
    default_agent = 'epic-reviewer',
    reviewer_agent = 'epic-reviewer',
    bundled_hash = NULL,
    updated_at = NOW()
WHERE name = 'holistic_qa';

UPDATE task_stage_states
SET reviewer_agent = 'epic-reviewer'
WHERE reviewer_agent = 'holistic-reviewer';

UPDATE tasks
SET assigned_agent = 'epic-reviewer'
WHERE assigned_agent = 'holistic-reviewer';

UPDATE sessions
SET workflow_name = 'epic-reviewer'
WHERE workflow_name = 'holistic-reviewer';

UPDATE agent_runs
SET workflow_name = 'epic-reviewer'
WHERE workflow_name = 'holistic-reviewer';

UPDATE agent_runs
SET agent_name = 'epic-reviewer'
WHERE agent_name = 'holistic-reviewer';

UPDATE build_profiles AS profiles
SET skip_stages_json = normalized.stages
FROM (
    SELECT profile.id,
           jsonb_agg(
               to_jsonb(
                   CASE stage.value
                       WHEN 'holistic_qa' THEN 'epic_qa'
                       WHEN 'holistic_review' THEN 'epic_qa'
                       ELSE stage.value
                   END
               )
               ORDER BY stage.ordinality
           ) AS stages
    FROM build_profiles AS profile
    CROSS JOIN LATERAL jsonb_array_elements_text(profile.skip_stages_json)
        WITH ORDINALITY AS stage(value, ordinality)
    GROUP BY profile.id
) AS normalized
WHERE profiles.id = normalized.id
  AND (
      profiles.skip_stages_json ? 'holistic_qa'
      OR profiles.skip_stages_json ? 'holistic_review'
  );
