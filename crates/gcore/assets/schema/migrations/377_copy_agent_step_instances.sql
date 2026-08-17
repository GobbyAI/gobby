-- Copy live agent-step workflow_instances into agent_step_instances.
-- Guarded: absent workflow_instances is a receipted no-op (fresh lineage after 7.1).
DO $copy_instances$
DECLARE
    bad text;
    source_n integer;
    copied_n integer;
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM information_schema.tables
        WHERE table_schema = current_schema()
          AND table_name = 'workflow_instances'
    ) THEN
        RETURN;
    END IF;

    EXECUTE 'LOCK TABLE workflow_instances IN ACCESS EXCLUSIVE MODE';

    SELECT string_agg(format('%s %s', wi.session_id, wi.workflow_name), ', ')
    INTO bad
    FROM workflow_instances wi
    JOIN sessions s ON s.id = wi.session_id
    WHERE s.status IN ('active', 'paused', 'handoff_ready')
      AND wi.workflow_name !~ '-steps$';
    IF bad IS NOT NULL THEN
        RAISE EXCEPTION
            'instance copy refused: live session has non-agent-step instance: %',
            bad;
    END IF;

    CREATE TEMP TABLE _inst_live ON COMMIT DROP AS
    SELECT
        wi.id,
        wi.session_id,
        wi.workflow_name,
        wi.enabled,
        wi.current_step,
        wi.step_entered_at,
        wi.step_action_count,
        wi.total_action_count,
        wi.variables,
        wi.context_injected,
        wi.created_at,
        wi.updated_at,
        regexp_replace(wi.workflow_name, '-steps$', '') AS agent_name,
        s.project_id AS session_project_id,
        NULLIF(sv.variables ->> '_agent_type', '') AS agent_type
    FROM workflow_instances wi
    JOIN sessions s ON s.id = wi.session_id
    LEFT JOIN session_variables sv ON sv.session_id = wi.session_id
    WHERE s.status IN ('active', 'paused', 'handoff_ready')
      AND wi.workflow_name ~ '-steps$';

    SELECT string_agg(format('%s %s', session_id, '_agent_type'), ', ')
    INTO bad
    FROM (
        SELECT DISTINCT session_id
        FROM _inst_live
        WHERE agent_type IS NULL
    ) missing;
    IF bad IS NOT NULL THEN
        RAISE EXCEPTION
            'instance copy refused: live session has qualifying rows and no _agent_type: %',
            bad;
    END IF;

    CREATE TEMP TABLE _inst_selected ON COMMIT DROP AS
    SELECT DISTINCT ON (session_id)
        id,
        session_id,
        workflow_name,
        enabled,
        current_step,
        step_entered_at,
        step_action_count,
        total_action_count,
        variables,
        context_injected,
        created_at,
        updated_at,
        agent_name,
        session_project_id
    FROM _inst_live
    WHERE agent_name = agent_type
    ORDER BY session_id, updated_at DESC, id ASC;

    CREATE TEMP TABLE _inst_resolved ON COMMIT DROP AS
    SELECT
        sel.*,
        CASE
            WHEN jsonb_typeof(gen.steps) = 'array'
                AND jsonb_array_length(gen.steps) > 0
                THEN jsonb_build_object(
                    'steps', gen.steps,
                    'variables', COALESCE(gen.variables, '{}'::jsonb),
                    'exit_condition', gen.exit_condition
                )
            WHEN child.steps_json IS NOT NULL THEN
                jsonb_build_object(
                    'steps', child.steps_json,
                    'variables', COALESCE(child.variables_json, '{}'::jsonb),
                    'exit_condition', child.exit_condition
                )
            ELSE NULL
        END AS snapshot_json,
        CASE
            WHEN jsonb_typeof(gen.steps) = 'array'
                AND jsonb_array_length(gen.steps) > 0
                THEN 'generated'
            WHEN child.steps_json IS NOT NULL THEN 'rebuild'
            ELSE NULL
        END AS snapshot_branch,
        child.child_id AS agent_step_workflow_id,
        CASE
            WHEN jsonb_typeof(gen.steps) = 'array'
                AND jsonb_array_length(gen.steps) > 0
                THEN jsonb_build_object(
                    'steps', gen.steps,
                    'variables', COALESCE(gen.variables, '{}'::jsonb),
                    'exit_condition', gen.exit_condition
                )
            ELSE jsonb_build_object(
                'steps', child.steps_json,
                'variables', COALESCE(child.variables_json, '{}'::jsonb),
                'exit_condition', child.exit_condition
            )
        END AS expected_snapshot
    FROM _inst_selected sel
    LEFT JOIN LATERAL (
        SELECT
            COALESCE(
                CASE
                    WHEN jsonb_typeof(d.definition_json -> 'steps') = 'array'
                        THEN d.definition_json -> 'steps'
                    ELSE NULL
                END,
                CASE
                    WHEN jsonb_typeof(d.definition_json -> 'step_workflow' -> 'steps') = 'array'
                        THEN d.definition_json -> 'step_workflow' -> 'steps'
                    ELSE NULL
                END
            ) AS steps,
            COALESCE(
                NULLIF(d.definition_json -> 'variables', 'null'::jsonb),
                NULLIF(d.definition_json -> 'step_variables', 'null'::jsonb),
                NULLIF(d.definition_json -> 'step_workflow' -> 'variables', 'null'::jsonb),
                '{}'::jsonb
            ) AS variables,
            COALESCE(
                d.definition_json ->> 'exit_condition',
                d.definition_json -> 'step_workflow' ->> 'exit_condition'
            ) AS exit_condition
        FROM workflow_definitions d
        WHERE to_regclass('workflow_definitions') IS NOT NULL
          AND d.name = sel.workflow_name
          AND d.deleted_at IS NULL
          AND (
              d.project_id IS NOT DISTINCT FROM sel.session_project_id
              OR d.project_id IS NULL
          )
        ORDER BY
            CASE
                WHEN d.project_id IS NOT DISTINCT FROM sel.session_project_id THEN 0
                ELSE 1
            END,
            d.id ASC
        LIMIT 1
    ) gen ON TRUE
    LEFT JOIN LATERAL (
        SELECT
            c.id AS child_id,
            c.steps_json,
            c.variables_json,
            c.exit_condition
        FROM agent_definitions a
        JOIN agent_step_workflows c ON c.agent_definition_id = a.id
        WHERE to_regclass('agent_definitions') IS NOT NULL
          AND to_regclass('agent_step_workflows') IS NOT NULL
          AND a.name = sel.agent_name
          AND a.deleted_at IS NULL
          AND (
              a.project_id IS NOT DISTINCT FROM sel.session_project_id
              OR a.project_id IS NULL
          )
        ORDER BY
            CASE
                WHEN a.project_id IS NOT DISTINCT FROM sel.session_project_id THEN 0
                ELSE 1
            END,
            a.id ASC
        LIMIT 1
    ) child ON TRUE;

    SELECT string_agg(format('%s %s', session_id, agent_name), ', ')
    INTO bad
    FROM _inst_resolved
    WHERE snapshot_json IS NULL;
    IF bad IS NOT NULL THEN
        RAISE EXCEPTION
            'instance copy refused: no recoverable snapshot for %',
            bad;
    END IF;

    SELECT string_agg(format('%s current_step=%s', session_id, current_step), ', ')
    INTO bad
    FROM _inst_resolved r
    WHERE r.current_step IS NOT NULL
      AND NOT EXISTS (
        SELECT 1
        FROM jsonb_array_elements(r.snapshot_json -> 'steps') elem
        WHERE elem ->> 'name' IS NOT DISTINCT FROM r.current_step
    );
    IF bad IS NOT NULL THEN
        RAISE EXCEPTION
            'instance copy refused: current_step missing from snapshot: %',
            bad;
    END IF;

    INSERT INTO agent_step_instances (
        id, session_id, agent_step_workflow_id, agent_name, enabled,
        current_step, step_entered_at, step_action_count, total_action_count,
        variables, context_injected, snapshot_json, created_at, updated_at
    )
    SELECT
        id,
        session_id,
        agent_step_workflow_id,
        agent_name,
        enabled,
        current_step,
        step_entered_at,
        COALESCE(step_action_count, 0),
        COALESCE(total_action_count, 0),
        COALESCE(variables, '{}'::jsonb),
        COALESCE(context_injected, false),
        snapshot_json,
        created_at,
        updated_at
    FROM _inst_resolved
    ON CONFLICT DO NOTHING;

    SELECT count(*) INTO source_n FROM _inst_resolved;
    SELECT count(*) INTO copied_n
    FROM agent_step_instances t
    WHERE t.session_id IN (SELECT session_id FROM _inst_resolved);
    IF source_n IS DISTINCT FROM copied_n THEN
        RAISE EXCEPTION
            'instance copy count mismatch: % selected, % typed rows',
            source_n, copied_n;
    END IF;

    SELECT string_agg(format('%s', r.session_id), ', ')
    INTO bad
    FROM _inst_resolved r
    JOIN agent_step_instances t ON t.session_id = r.session_id
    WHERE t.id IS DISTINCT FROM r.id
       OR t.agent_name IS DISTINCT FROM r.agent_name
       OR t.enabled IS DISTINCT FROM r.enabled
       OR t.current_step IS DISTINCT FROM r.current_step
       OR t.step_entered_at IS DISTINCT FROM r.step_entered_at
       OR t.created_at IS DISTINCT FROM r.created_at
       OR t.updated_at IS DISTINCT FROM r.updated_at
       OR t.step_action_count IS DISTINCT FROM COALESCE(r.step_action_count, 0)
       OR t.total_action_count IS DISTINCT FROM COALESCE(r.total_action_count, 0)
       OR t.variables IS DISTINCT FROM COALESCE(r.variables, '{}'::jsonb)
       OR t.context_injected IS DISTINCT FROM COALESCE(r.context_injected, false)
       OR t.agent_step_workflow_id IS DISTINCT FROM r.agent_step_workflow_id
       OR t.snapshot_json IS DISTINCT FROM r.expected_snapshot
       OR t.snapshot_json IS DISTINCT FROM r.snapshot_json
       OR (
            t.current_step IS NOT NULL
            AND NOT EXISTS (
                SELECT 1
                FROM jsonb_array_elements(t.snapshot_json -> 'steps') elem
                WHERE elem ->> 'name' IS NOT DISTINCT FROM t.current_step
            )
       );
    IF bad IS NOT NULL THEN
        RAISE EXCEPTION 'instance copy payload mismatch: %', bad;
    END IF;

    CREATE OR REPLACE FUNCTION gobby_reject_workflow_instance_writes()
    RETURNS trigger
    LANGUAGE plpgsql
    AS $fn$
    BEGIN
        RAISE EXCEPTION
            'workflow_instances writes are rejected after agent_step_instances copy';
    END;
    $fn$;

    DROP TRIGGER IF EXISTS workflow_instances_reject_writes ON workflow_instances;
    CREATE TRIGGER workflow_instances_reject_writes
        BEFORE INSERT OR UPDATE OR DELETE ON workflow_instances
        FOR EACH ROW
        EXECUTE FUNCTION gobby_reject_workflow_instance_writes();
END
$copy_instances$;
