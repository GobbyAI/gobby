-- gobby:destructive
-- Re-key session machine attribution to machines(id) and merge projected-key collisions.
DO $migration$
DECLARE
    machine_column_type TEXT;
    invalid_machine_count BIGINT;
    orphan_machine_count BIGINT;
    composite_fk_count BIGINT;
    parent_group_count BIGINT;
    parent_loser_count BIGINT;
    child_delete_count BIGINT;
    child_repoint_count BIGINT;
    inventory_text TEXT;
    partition_expressions TEXT;
    nullable_expression TEXT;
    affected_expression TEXT;
    set_clause TEXT;
    fk_row RECORD;
    constraint_row RECORD;
    table_row RECORD;
BEGIN
    SELECT udt_name
    INTO machine_column_type
    FROM information_schema.columns
    WHERE table_schema = current_schema()
      AND table_name = 'sessions'
      AND column_name = 'machine_id';

    IF machine_column_type NOT IN ('text', 'uuid') THEN
        RAISE EXCEPTION
            'sessions.machine_id expected text or uuid, found %',
            COALESCE(machine_column_type, '<missing>');
    END IF;

    IF machine_column_type = 'text' THEN
        SELECT COUNT(*)
        INTO invalid_machine_count
        FROM sessions
        WHERE machine_id IS NOT NULL
          AND NOT pg_input_is_valid(BTRIM(machine_id), 'uuid')
          AND NOT (
              LOWER(BTRIM(machine_id)) IN (
                  'comms',
                  'cron',
                  'pipeline',
                  'system',
                  'unknown',
                  'unknown-machine',
                  '<source>'
              )
              OR LOWER(BTRIM(machine_id)) ~
                  '^web:[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
              OR LOWER(BTRIM(machine_id)) ~
                  '^legacy-missing:[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
              OR LOWER(BTRIM(machine_id)) LIKE 'live-browser-verification-%'
          );
        IF invalid_machine_count > 0 THEN
            RAISE EXCEPTION
                'sessions.machine_id zero-unmapped preflight found % unclassified values',
                invalid_machine_count;
        END IF;

        CREATE TEMP TABLE _gobby_session_machine_projection ON COMMIT DROP AS
        SELECT
            id,
            CASE
                WHEN machine_id IS NULL THEN NULL::UUID
                WHEN pg_input_is_valid(BTRIM(machine_id), 'uuid')
                    THEN BTRIM(machine_id)::UUID
                ELSE NULL::UUID
            END AS machine_id
        FROM sessions;
    ELSE
        CREATE TEMP TABLE _gobby_session_machine_projection ON COMMIT DROP AS
        SELECT id, machine_id
        FROM sessions;
    END IF;

    SELECT COUNT(*)
    INTO orphan_machine_count
    FROM _gobby_session_machine_projection AS projection
    LEFT JOIN machines ON machines.id = projection.machine_id
    WHERE projection.machine_id IS NOT NULL
      AND machines.id IS NULL;
    IF orphan_machine_count > 0 THEN
        RAISE EXCEPTION
            'sessions.machine_id FK preflight found % UUIDs absent from machines(id)',
            orphan_machine_count;
    END IF;

    CREATE TEMP TABLE _gobby_session_merge (
        loser_id UUID PRIMARY KEY,
        survivor_id UUID NOT NULL
    ) ON COMMIT DROP;

    INSERT INTO _gobby_session_merge(loser_id, survivor_id)
    WITH ranked AS (
        SELECT
            sessions.id,
            FIRST_VALUE(sessions.id) OVER collision AS survivor_id,
            ROW_NUMBER() OVER collision AS collision_rank
        FROM sessions
        JOIN _gobby_session_machine_projection AS projection
          ON projection.id = sessions.id
        WINDOW collision AS (
            PARTITION BY
                sessions.external_id,
                projection.machine_id,
                sessions.source,
                sessions.project_id,
                sessions.session_type
            ORDER BY
                sessions.updated_at DESC NULLS LAST,
                sessions.created_at DESC NULLS LAST,
                sessions.id DESC
        )
    )
    SELECT id, survivor_id
    FROM ranked
    WHERE collision_rank > 1;

    SELECT COUNT(DISTINCT survivor_id), COUNT(*)
    INTO parent_group_count, parent_loser_count
    FROM _gobby_session_merge;

    SELECT COUNT(*)
    INTO composite_fk_count
    FROM pg_constraint
    WHERE contype = 'f'
      AND confrelid = 'sessions'::REGCLASS
      AND (
          COALESCE(ARRAY_LENGTH(conkey, 1), 0) <> 1
          OR COALESCE(ARRAY_LENGTH(confkey, 1), 0) <> 1
      );
    IF composite_fk_count > 0 THEN
        RAISE EXCEPTION
            'sessions inbound FK inventory found % unsupported composite constraints',
            composite_fk_count;
    END IF;

    -- Outbound composite FKs from sessions (e.g. sessions_summary_revision_fk ->
    -- session_summary_revisions(id, session_id)) break when loser-owned child rows
    -- are repointed to the survivor while the loser session row still holds the
    -- back-reference. Only sessions_summary_revision_fk is a known shape; its
    -- loser back-references are cleared before repoint, and any other outbound
    -- composite FK fails loudly here instead of mid-merge.
    SELECT COUNT(*)
    INTO composite_fk_count
    FROM pg_constraint
    WHERE contype = 'f'
      AND conrelid = 'sessions'::REGCLASS
      AND conname <> 'sessions_summary_revision_fk'
      AND (
          COALESCE(ARRAY_LENGTH(conkey, 1), 0) <> 1
          OR COALESCE(ARRAY_LENGTH(confkey, 1), 0) <> 1
      );
    IF composite_fk_count > 0 THEN
        RAISE EXCEPTION
            'sessions outbound FK inventory found % unsupported composite constraints',
            composite_fk_count;
    END IF;

    UPDATE sessions
    SET summary_revision_id = NULL
    FROM _gobby_session_merge AS merge
    WHERE sessions.id = merge.loser_id
      AND sessions.summary_revision_id IS NOT NULL;

    CREATE TEMP TABLE _gobby_session_fk_inventory (
        constraint_oid OID NOT NULL,
        constraint_name NAME NOT NULL,
        child_table REGCLASS NOT NULL,
        child_attnum SMALLINT NOT NULL,
        child_column NAME NOT NULL
    ) ON COMMIT DROP;

    INSERT INTO _gobby_session_fk_inventory(
        constraint_oid,
        constraint_name,
        child_table,
        child_attnum,
        child_column
    )
    SELECT
        foreign_key.oid,
        foreign_key.conname,
        foreign_key.conrelid::REGCLASS,
        foreign_key.conkey[1],
        child_attribute.attname
    FROM pg_constraint AS foreign_key
    JOIN pg_attribute AS parent_attribute
      ON parent_attribute.attrelid = foreign_key.confrelid
     AND parent_attribute.attnum = foreign_key.confkey[1]
    JOIN pg_attribute AS child_attribute
      ON child_attribute.attrelid = foreign_key.conrelid
     AND child_attribute.attnum = foreign_key.conkey[1]
    WHERE foreign_key.contype = 'f'
      AND foreign_key.confrelid = 'sessions'::REGCLASS
      AND parent_attribute.attname = 'id';

    SELECT STRING_AGG(
        FORMAT(
            '%s.%I:%I[%s]',
            inventory.child_table,
            inventory.child_column,
            inventory.constraint_name,
            COALESCE(unique_inventory.constraints, 'none')
        ),
        '; ' ORDER BY inventory.child_table::TEXT, inventory.constraint_name
    )
    INTO inventory_text
    FROM _gobby_session_fk_inventory AS inventory
    LEFT JOIN LATERAL (
        SELECT STRING_AGG(
            FORMAT('%I(%s)', index_class.relname, unique_columns.columns),
            ',' ORDER BY index_class.relname
        ) AS constraints
        FROM pg_index AS unique_index
        JOIN pg_class AS index_class ON index_class.oid = unique_index.indexrelid
        JOIN LATERAL (
            SELECT STRING_AGG(attribute.attname, ',' ORDER BY key_column.ordinality) AS columns
            FROM UNNEST(unique_index.indkey) WITH ORDINALITY AS key_column(attnum, ordinality)
            JOIN pg_attribute AS attribute
              ON attribute.attrelid = unique_index.indrelid
             AND attribute.attnum = key_column.attnum
            WHERE key_column.ordinality <= unique_index.indnkeyatts
        ) AS unique_columns ON TRUE
        WHERE unique_index.indrelid = inventory.child_table
          AND unique_index.indisunique
    ) AS unique_inventory ON TRUE;

    RAISE NOTICE 'sessions FK inventory with unique indexes: %',
        COALESCE(inventory_text, 'none');

    CREATE TEMP TABLE _gobby_child_actions (
        child_table REGCLASS NOT NULL,
        fk_constraint OID NOT NULL,
        fk_column NAME NOT NULL,
        row_tid TID NOT NULL,
        survivor_id UUID NOT NULL,
        action TEXT NOT NULL DEFAULT 'repoint',
        reason TEXT
    ) ON COMMIT DROP;

    FOR fk_row IN
        SELECT *
        FROM _gobby_session_fk_inventory
        WHERE child_table <> 'sessions'::REGCLASS
    LOOP
        EXECUTE FORMAT(
            'INSERT INTO _gobby_child_actions('
            'child_table, fk_constraint, fk_column, row_tid, survivor_id'
            ') '
            'SELECT %L::REGCLASS, %s::OID, %L, child.ctid, merge.survivor_id '
            'FROM %s AS child '
            'JOIN _gobby_session_merge AS merge ON merge.loser_id = child.%I',
            fk_row.child_table::TEXT,
            fk_row.constraint_oid,
            fk_row.child_column,
            fk_row.child_table,
            fk_row.child_column
        );
    END LOOP;

    -- Collision inventory is driven by pg_index, not pg_constraint: plain
    -- CREATE UNIQUE INDEX uniqueness (e.g. idx_token_events_dedup) never gets a
    -- pg_constraint row but rejects merges just the same. Constraint-backed
    -- indexes appear here too, so nothing is lost. Partial indexes only rank
    -- rows satisfying their predicate; repoint never changes predicate inputs
    -- (session FK columns keep their NULL-ness and other columns are
    -- untouched), so evaluating the predicate pre-merge is faithful.
    FOR constraint_row IN
        SELECT
            index_class.relname AS index_name,
            unique_index.indrelid::REGCLASS AS child_table,
            key_columns.attnums AS conkey,
            key_columns.has_expression_key,
            unique_index.indnullsnotdistinct,
            PG_GET_EXPR(unique_index.indpred, unique_index.indrelid) AS predicate
        FROM pg_index AS unique_index
        JOIN pg_class AS index_class ON index_class.oid = unique_index.indexrelid
        JOIN LATERAL (
            SELECT
                ARRAY_AGG(key_column.attnum ORDER BY key_column.ordinality) AS attnums,
                BOOL_OR(key_column.attnum = 0) AS has_expression_key
            FROM UNNEST(unique_index.indkey) WITH ORDINALITY AS key_column(attnum, ordinality)
            WHERE key_column.ordinality <= unique_index.indnkeyatts
        ) AS key_columns ON TRUE
        WHERE unique_index.indisunique
          AND EXISTS (
              SELECT 1
              FROM _gobby_session_fk_inventory AS inventory
              WHERE inventory.child_table = unique_index.indrelid
                AND inventory.child_attnum = ANY(key_columns.attnums)
                AND inventory.child_table <> 'sessions'::REGCLASS
          )
    LOOP
        IF constraint_row.has_expression_key THEN
            RAISE EXCEPTION
                'unique index % on % mixes expression keys with session FK columns; merge cannot rank its collisions',
                constraint_row.index_name,
                constraint_row.child_table;
        END IF;
        SELECT
            STRING_AGG(
                CASE
                    WHEN inventory.child_attnum IS NOT NULL THEN FORMAT(
                        'COALESCE((SELECT merge.survivor_id '
                        'FROM _gobby_session_merge AS merge '
                        'WHERE merge.loser_id = child.%1$I), child.%1$I)',
                        attribute.attname
                    )
                    ELSE FORMAT('child.%I', attribute.attname)
                END,
                ', ' ORDER BY key_column.ordinality
            ),
            STRING_AGG(
                FORMAT(
                    '%s IS NULL',
                    CASE
                        WHEN inventory.child_attnum IS NOT NULL THEN FORMAT(
                            'COALESCE((SELECT merge.survivor_id '
                            'FROM _gobby_session_merge AS merge '
                            'WHERE merge.loser_id = child.%1$I), child.%1$I)',
                            attribute.attname
                        )
                        ELSE FORMAT('child.%I', attribute.attname)
                    END
                ),
                ' OR ' ORDER BY key_column.ordinality
            )
        INTO partition_expressions, nullable_expression
        FROM UNNEST(constraint_row.conkey) WITH ORDINALITY AS key_column(attnum, ordinality)
        JOIN pg_attribute AS attribute
          ON attribute.attrelid = constraint_row.child_table
         AND attribute.attnum = key_column.attnum
        LEFT JOIN (
            SELECT DISTINCT child_table, child_attnum
            FROM _gobby_session_fk_inventory
        ) AS inventory
          ON inventory.child_table = constraint_row.child_table
         AND inventory.child_attnum = key_column.attnum;

        SELECT STRING_AGG(
            FORMAT(
                'EXISTS (SELECT 1 FROM _gobby_session_merge AS merge '
                'WHERE merge.loser_id = child.%I)',
                child_column
            ),
            ' OR ' ORDER BY child_column
        )
        INTO affected_expression
        FROM (
            SELECT DISTINCT child_column
            FROM _gobby_session_fk_inventory
            WHERE child_table = constraint_row.child_table
        ) AS session_fk_columns;

        IF NOT constraint_row.indnullsnotdistinct THEN
            partition_expressions := FORMAT(
                '%s, CASE WHEN %s THEN child.ctid ELSE NULL::TID END',
                partition_expressions,
                nullable_expression
            );
        END IF;

        EXECUTE FORMAT(
            'WITH ranked AS ('
            'SELECT child.ctid AS row_tid, (%1$s) AS affected, '
            'ROW_NUMBER() OVER ('
            'PARTITION BY %2$s '
            'ORDER BY CASE WHEN (%1$s) THEN 1 ELSE 0 END, child.ctid'
            ') AS collision_rank '
            'FROM %3$s AS child '
            'WHERE %6$s'
            ') '
            'UPDATE _gobby_child_actions AS action '
            'SET action = ''delete'', reason = %4$L '
            'FROM ranked '
            'WHERE action.child_table = %5$L::REGCLASS '
            'AND action.row_tid = ranked.row_tid '
            'AND ranked.affected '
            'AND ranked.collision_rank > 1',
            affected_expression,
            partition_expressions,
            constraint_row.child_table,
            FORMAT('unique index %I', constraint_row.index_name),
            constraint_row.child_table::TEXT,
            COALESCE('(' || constraint_row.predicate || ')', 'TRUE')
        );
    END LOOP;

    SELECT
        COUNT(*) FILTER (WHERE action = 'delete'),
        COUNT(*) FILTER (WHERE action = 'repoint')
    INTO child_delete_count, child_repoint_count
    FROM (
        SELECT
            child_table,
            row_tid,
            CASE WHEN BOOL_OR(action = 'delete') THEN 'delete' ELSE 'repoint' END AS action
        FROM _gobby_child_actions
        GROUP BY child_table, row_tid
    ) AS row_actions;

    RAISE NOTICE
        'sessions collision preflight ledger: parent_groups=% parent_losers=% child_delete=% child_repoint=%',
        parent_group_count,
        parent_loser_count,
        child_delete_count,
        child_repoint_count;

    -- Migration 342 added tasks_require_validation_criteria as NOT VALID because
    -- grandfathered rows violate it, and any UPDATE re-checks unvalidated CHECK
    -- constraints for the touched row (the migration 361 precedent). Repointing
    -- loser-owned child rows must not be rejected by grandfathered data, so every
    -- unvalidated CHECK constraint on an affected table is dropped here and
    -- re-added verbatim (still NOT VALID) after the merge. DDL holds ACCESS
    -- EXCLUSIVE on these tables for the rest of the transaction, so no
    -- concurrent write can slip through the gap.
    CREATE TEMP TABLE _gobby_invalid_checks (
        check_table REGCLASS NOT NULL,
        constraint_name NAME NOT NULL,
        constraint_def TEXT NOT NULL
    ) ON COMMIT DROP;

    INSERT INTO _gobby_invalid_checks(check_table, constraint_name, constraint_def)
    SELECT
        invalid_check.conrelid::REGCLASS,
        invalid_check.conname,
        PG_GET_CONSTRAINTDEF(invalid_check.oid)
    FROM pg_constraint AS invalid_check
    WHERE invalid_check.contype = 'c'
      AND NOT invalid_check.convalidated
      AND invalid_check.conrelid IN (
          SELECT DISTINCT child_table::OID FROM _gobby_session_fk_inventory
          UNION
          SELECT 'sessions'::REGCLASS::OID
      );

    FOR constraint_row IN
        SELECT check_table, constraint_name
        FROM _gobby_invalid_checks
        ORDER BY check_table::TEXT, constraint_name
    LOOP
        EXECUTE FORMAT(
            'ALTER TABLE %1$s DROP CONSTRAINT %2$I',
            constraint_row.check_table,
            constraint_row.constraint_name
        );
    END LOOP;

    FOR table_row IN
        SELECT child_table
        FROM _gobby_child_actions
        GROUP BY child_table
        ORDER BY child_table::TEXT
    LOOP
        EXECUTE FORMAT(
            'DELETE FROM %1$s AS child '
            'USING ('
            'SELECT DISTINCT row_tid FROM _gobby_child_actions '
            'WHERE child_table = %2$L::REGCLASS AND action = ''delete'''
            ') AS doomed '
            'WHERE child.ctid = doomed.row_tid',
            table_row.child_table,
            table_row.child_table::TEXT
        );

        SELECT STRING_AGG(
            FORMAT(
                '%1$I = COALESCE((SELECT merge.survivor_id '
                'FROM _gobby_session_merge AS merge '
                'WHERE merge.loser_id = child.%1$I), child.%1$I)',
                child_column
            ),
            ', ' ORDER BY child_column
        )
        INTO set_clause
        FROM (
            SELECT DISTINCT child_column
            FROM _gobby_session_fk_inventory
            WHERE child_table = table_row.child_table
        ) AS session_fk_columns;

        EXECUTE FORMAT(
            'UPDATE %1$s AS child SET %2$s '
            'WHERE EXISTS ('
            'SELECT 1 FROM _gobby_child_actions AS action '
            'WHERE action.child_table = %3$L::REGCLASS '
            'AND action.action = ''repoint'' '
            'AND action.row_tid = child.ctid'
            ')',
            table_row.child_table,
            set_clause,
            table_row.child_table::TEXT
        );
    END LOOP;

    FOR fk_row IN
        SELECT *
        FROM _gobby_session_fk_inventory
        WHERE child_table = 'sessions'::REGCLASS
    LOOP
        EXECUTE FORMAT(
            'UPDATE sessions AS child '
            'SET %1$I = merge.survivor_id '
            'FROM _gobby_session_merge AS merge '
            'WHERE child.%1$I = merge.loser_id',
            fk_row.child_column
        );
    END LOOP;

    DELETE FROM sessions AS loser
    USING _gobby_session_merge AS merge
    WHERE loser.id = merge.loser_id;

    IF machine_column_type = 'text' THEN
        UPDATE sessions
        SET machine_id = NULL
        WHERE machine_id IS NOT NULL
          AND NOT pg_input_is_valid(BTRIM(machine_id), 'uuid');

        ALTER TABLE sessions
            ALTER COLUMN machine_id DROP NOT NULL,
            ALTER COLUMN machine_id TYPE UUID USING machine_id::UUID;
    ELSE
        ALTER TABLE sessions ALTER COLUMN machine_id DROP NOT NULL;
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint AS foreign_key
        JOIN pg_attribute AS child_attribute
          ON child_attribute.attrelid = foreign_key.conrelid
         AND child_attribute.attnum = foreign_key.conkey[1]
        WHERE foreign_key.contype = 'f'
          AND foreign_key.conrelid = 'sessions'::REGCLASS
          AND foreign_key.confrelid = 'machines'::REGCLASS
          AND child_attribute.attname = 'machine_id'
    ) THEN
        ALTER TABLE sessions
            ADD CONSTRAINT sessions_machine_id_fkey
            FOREIGN KEY (machine_id) REFERENCES machines(id);
    END IF;

    DROP INDEX IF EXISTS idx_sessions_unique;
    CREATE UNIQUE INDEX idx_sessions_unique
        ON sessions(external_id, machine_id, source, project_id, session_type)
        NULLS NOT DISTINCT;

    FOR constraint_row IN
        SELECT check_table, constraint_name, constraint_def
        FROM _gobby_invalid_checks
        ORDER BY check_table::TEXT, constraint_name
    LOOP
        EXECUTE FORMAT(
            'ALTER TABLE %1$s ADD CONSTRAINT %2$I %3$s',
            constraint_row.check_table,
            constraint_row.constraint_name,
            constraint_row.constraint_def
        );
    END LOOP;
END
$migration$;
