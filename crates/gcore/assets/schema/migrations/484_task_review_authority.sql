-- Preserve the old authority of review records that still govern open work.
-- Runtime checks use the recorded facts, never these historical definition names.
UPDATE task_comments AS c
SET body = (
    c.body::jsonb || jsonb_build_object(
        'supplemental_review', c.body::jsonb ->> 'agent_definition' = 'security-reviewer',
        'agent_capabilities', CASE c.body::jsonb ->> 'agent_definition'
            WHEN 'security-reviewer' THEN '["approve_security_review"]'::jsonb
            WHEN 'orchestrator' THEN '["approve_report_review"]'::jsonb
            ELSE '[]'::jsonb
        END
    )
)::text
FROM tasks AS t
WHERE c.task_id = t.id
  AND t.closed_at IS NULL
  AND c.author_type = 'task_step'
  AND c.body::jsonb ->> 'step' = 'review'
  AND NOT (c.body::jsonb ? 'supplemental_review');
