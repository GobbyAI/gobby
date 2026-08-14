-- Copy workflow_type='rule' rows into rule_definitions.
-- Guarded: absent workflow_definitions is a receipted no-op (fresh lineage after 7.1).
DO $copy_rules$
DECLARE
    dup_names text;
    source_n integer;
    joined_n integer;
    mismatch_ids text;
BEGIN
    IF to_regclass('workflow_definitions') IS NULL THEN
        RETURN;
    END IF;

    EXECUTE 'LOCK TABLE workflow_definitions IN ACCESS EXCLUSIVE MODE';

    SELECT string_agg(format('%s', name), ', ' ORDER BY name)
    INTO dup_names
    FROM (
        SELECT name
        FROM workflow_definitions
        WHERE workflow_type = 'rule' AND deleted_at IS NULL
        GROUP BY name, project_id
        HAVING count(*) > 1
    ) dups;
    IF dup_names IS NOT NULL THEN
        RAISE EXCEPTION
            'rule copy refused: duplicate live (name, project_id) among workflow_type=rule rows: %',
            dup_names;
    END IF;

    CREATE TEMP TABLE _rule_copy_src ON COMMIT DROP AS
    SELECT
        id,
        project_id,
        name,
        description,
        COALESCE(enabled, true) AS enabled,
        enabled_user_modified AS enabled_pinned,
        COALESCE(priority, 100) AS priority,
        sources,
        definition_json,
        CASE
            WHEN source IN ('installed', 'custom', 'project') THEN source
            ELSE 'installed'
        END AS source,
        tags,
        deleted_at,
        created_at,
        updated_at,
        md5(
            jsonb_build_object(
                'id', id,
                'project_id', project_id,
                'name', name,
                'description', description,
                'enabled', COALESCE(enabled, true),
                'enabled_user_modified', enabled_user_modified,
                'priority', COALESCE(priority, 100),
                'sources', sources,
                'definition_json', definition_json,
                'source', CASE
                    WHEN source IN ('installed', 'custom', 'project') THEN source
                    ELSE 'installed'
                END,
                'tags', tags,
                'deleted_at', deleted_at
            )::text
        ) AS source_hash
    FROM workflow_definitions
    WHERE workflow_type = 'rule';

    INSERT INTO rule_definitions (
        id, project_id, name, description, enabled, enabled_pinned,
        priority, sources, definition_json, source, tags, deleted_at,
        created_at, updated_at
    )
    SELECT
        id, project_id, name, description, enabled, enabled_pinned,
        priority, sources, definition_json, source, tags, deleted_at,
        created_at, updated_at
    FROM _rule_copy_src
    ON CONFLICT DO NOTHING;

    INSERT INTO legacy_copy_ledger (legacy_id, domain, source_hash)
    SELECT id, 'rules', source_hash
    FROM _rule_copy_src
    ON CONFLICT (legacy_id) DO NOTHING;

    CREATE TEMP TABLE _rule_copy_joined ON COMMIT DROP AS
    SELECT
        s.id AS source_id,
        s.name AS source_name,
        t.id AS target_id,
        t.project_id AS target_project_id,
        t.name AS target_name,
        t.description AS target_description,
        t.enabled AS target_enabled,
        t.enabled_pinned AS target_enabled_pinned,
        t.priority AS target_priority,
        t.sources AS target_sources,
        t.definition_json AS target_body,
        t.source AS target_source,
        t.tags AS target_tags,
        t.deleted_at AS target_deleted_at,
        t.created_at AS target_created_at,
        t.updated_at AS target_updated_at,
        s.project_id AS source_project_id,
        s.description AS source_description,
        s.enabled AS source_enabled,
        s.enabled_pinned AS source_enabled_pinned,
        s.priority AS source_priority,
        s.sources AS source_sources,
        s.definition_json AS source_body,
        s.source AS source_source,
        s.tags AS source_tags,
        s.deleted_at AS source_deleted_at,
        s.created_at AS source_created_at,
        s.updated_at AS source_updated_at
    FROM _rule_copy_src s
    LEFT JOIN rule_definitions t
        ON (
            s.deleted_at IS NULL
            AND t.deleted_at IS NULL
            AND t.name = s.name
            AND t.project_id IS NOT DISTINCT FROM s.project_id
        ) OR (
            s.deleted_at IS NOT NULL
            AND t.id = s.id
        );

    SELECT count(*) INTO source_n FROM _rule_copy_src;
    SELECT count(*) INTO joined_n FROM _rule_copy_joined WHERE target_id IS NOT NULL;
    IF source_n IS DISTINCT FROM joined_n THEN
        SELECT string_agg(format('%s (%s)', source_name, source_id), ', ')
        INTO mismatch_ids
        FROM _rule_copy_joined
        WHERE target_id IS NULL;
        RAISE EXCEPTION
            'rule copy count mismatch: % source rows, % joined targets; unmatched: %',
            source_n, joined_n, mismatch_ids;
    END IF;

    SELECT string_agg(format('%s (%s)', source_name, source_id), ', ')
    INTO mismatch_ids
    FROM _rule_copy_joined
    WHERE target_id IS DISTINCT FROM source_id;
    IF mismatch_ids IS NOT NULL THEN
        RAISE EXCEPTION
            'rule copy identity mismatch (target.id <> source.id): %',
            mismatch_ids;
    END IF;

    SELECT string_agg(format('%s (%s)', source_name, source_id), ', ')
    INTO mismatch_ids
    FROM _rule_copy_joined
    WHERE target_body IS DISTINCT FROM source_body
       OR target_name IS DISTINCT FROM source_name
       OR target_project_id IS DISTINCT FROM source_project_id
       OR target_description IS DISTINCT FROM source_description
       OR target_enabled IS DISTINCT FROM source_enabled
       OR target_enabled_pinned IS DISTINCT FROM source_enabled_pinned
       OR target_priority IS DISTINCT FROM source_priority
       OR target_sources IS DISTINCT FROM source_sources
       OR target_source IS DISTINCT FROM source_source
       OR target_tags IS DISTINCT FROM source_tags
       OR target_deleted_at IS DISTINCT FROM source_deleted_at
       OR target_created_at IS DISTINCT FROM source_created_at
       OR target_updated_at IS DISTINCT FROM source_updated_at;
    IF mismatch_ids IS NOT NULL THEN
        RAISE EXCEPTION
            'rule copy payload mismatch: %',
            mismatch_ids;
    END IF;
END
$copy_rules$;
