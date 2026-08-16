-- gobby:destructive
-- Directional drop of legacy workflow tables after a ledger-hash backstop.
-- Fresh lineages receipt-stamp this file without executing it. If it does run
-- against a post-7.1 baseline, absent tables are a no-op.
DO $drop_legacy$
DECLARE
    bad text;
BEGIN
    IF to_regclass('workflow_definitions') IS NULL
       AND to_regclass('workflow_instances') IS NULL
       AND to_regclass('legacy_copy_ledger') IS NULL THEN
        RETURN;
    END IF;

    IF to_regclass('workflow_definitions') IS NOT NULL THEN
        SELECT string_agg(format('%s (%s)', name, id), ', ' ORDER BY name, id)
        INTO bad
        FROM workflow_definitions
        WHERE workflow_type NOT IN ('agent', 'rule', 'variable', 'pipeline', 'workflow');
        IF bad IS NOT NULL THEN
            RAISE EXCEPTION
                'drop refused: unsupported workflow_type (ids and names): %',
                bad;
        END IF;

        SELECT string_agg(format('%s (%s)', name, id), ', ' ORDER BY name, id)
        INTO bad
        FROM workflow_definitions
        WHERE workflow_type = 'workflow'
          AND NOT (name ~ '-steps$' AND source = 'agent');
        IF bad IS NOT NULL THEN
            RAISE EXCEPTION
                'drop refused: unsupported standalone workflow row (ids and names): %',
                bad;
        END IF;

        SELECT string_agg(format('%s (%s)', name, id), ', ' ORDER BY name, id)
        INTO bad
        FROM workflow_definitions d
        WHERE NOT (d.name ~ '-steps$' AND d.source = 'agent')
          AND (
              to_regclass('legacy_copy_ledger') IS NULL
              OR NOT EXISTS (
                  SELECT 1
                  FROM legacy_copy_ledger l
                  WHERE l.legacy_id = d.id
              )
          );
        IF bad IS NOT NULL THEN
            RAISE EXCEPTION
                'drop refused: legacy row has no ledger entry (ids and names): %',
                bad;
        END IF;

        SELECT string_agg(format('%s (%s)', d.name, d.id), ', ' ORDER BY d.name, d.id)
        INTO bad
        FROM workflow_definitions d
        JOIN legacy_copy_ledger l ON l.legacy_id = d.id
        WHERE NOT (d.name ~ '-steps$' AND d.source = 'agent')
          AND l.source_hash IS DISTINCT FROM (
              CASE d.workflow_type
                  WHEN 'agent' THEN md5(
                      jsonb_build_object(
                          'id', d.id,
                          'project_id', d.project_id,
                          'name', d.name,
                          'description', d.description,
                          'enabled', COALESCE(d.enabled, true),
                          'enabled_user_modified', d.enabled_user_modified,
                          'definition_json', d.definition_json,
                          'source', CASE
                              WHEN d.source IN ('installed', 'custom', 'project')
                                  THEN d.source
                              ELSE 'installed'
                          END,
                          'tags', d.tags,
                          'deleted_at', d.deleted_at
                      )::text
                  )
                  WHEN 'rule' THEN md5(
                      jsonb_build_object(
                          'id', d.id,
                          'project_id', d.project_id,
                          'name', d.name,
                          'description', d.description,
                          'enabled', COALESCE(d.enabled, true),
                          'enabled_user_modified', d.enabled_user_modified,
                          'priority', COALESCE(d.priority, 100),
                          'sources', d.sources,
                          'definition_json', d.definition_json,
                          'source', CASE
                              WHEN d.source IN ('installed', 'custom', 'project')
                                  THEN d.source
                              ELSE 'installed'
                          END,
                          'tags', d.tags,
                          'deleted_at', d.deleted_at
                      )::text
                  )
                  WHEN 'variable' THEN md5(
                      jsonb_build_object(
                          'id', d.id,
                          'project_id', d.project_id,
                          'name', COALESCE(d.definition_json->>'variable', d.name),
                          'description', d.description,
                          'enabled', COALESCE(d.enabled, true),
                          'enabled_user_modified', d.enabled_user_modified,
                          'default_value', d.definition_json->'value',
                          'source', CASE
                              WHEN d.source IN ('installed', 'custom', 'project')
                                  THEN d.source
                              ELSE 'installed'
                          END,
                          'tags', d.tags,
                          'deleted_at', d.deleted_at
                      )::text
                  )
                  WHEN 'pipeline' THEN md5(
                      jsonb_build_object(
                          'id', d.id,
                          'project_id', d.project_id,
                          'name', d.name,
                          'description', d.description,
                          'enabled', COALESCE(d.enabled, true),
                          'enabled_user_modified', d.enabled_user_modified,
                          'version', COALESCE(d.version, '1.0'),
                          'definition_json', d.definition_json,
                          'canvas_json', d.canvas_json,
                          'source', CASE
                              WHEN d.source IN ('installed', 'custom', 'project')
                                  THEN d.source
                              ELSE 'installed'
                          END,
                          'tags', d.tags,
                          'deleted_at', d.deleted_at
                      )::text
                  )
                  ELSE NULL
              END
          );
        IF bad IS NOT NULL THEN
            RAISE EXCEPTION
                'drop refused: legacy row hash mismatch (ids and names): %',
                bad;
        END IF;
    END IF;

    DROP TABLE IF EXISTS workflow_instances;
    DROP TABLE IF EXISTS workflow_definitions;
    DROP TABLE IF EXISTS legacy_copy_ledger;
END
$drop_legacy$;

DROP FUNCTION IF EXISTS gobby_reject_workflow_instance_writes();
