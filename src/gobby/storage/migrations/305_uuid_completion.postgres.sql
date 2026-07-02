-- Complete the native-uuid identity conversion started by 304.
--
-- 304 (as applied) left four gaps this migration closes:
--   1. code_calls.caller_symbol_id stayed NOT NULL, so module-scope calls
--      (no enclosing symbol) cannot be represented; the applied 304 deleted
--      those rows. Make the column nullable so reindex restores them as NULL.
--   2. task_stage_states.*_by_session_id previously doubled as actor labels
--      ('dispatcher', 'system'). Those writes now need a home: dedicated
--      *_by_actor columns.
--   3. Session-UUID columns 304 missed: session_variables.session_id,
--      rule_overrides.session_id, and gwiki_*.project_id (gwiki tables are
--      created by the gwiki binary and adopted by the hub, so they are
--      guarded by existence checks here).
--   4. agent_commands is dead code (no readers or writers) — dropped.
--
-- Statements must stay valid against BOTH a populated pre-305 schema and a
-- fresh baseline that already contains these changes (migrations replay on
-- top of the current baseline until the next flatten).

-- Cleanup: session_variables accumulated non-uuid test residue ('current',
-- 'sess-1', ...). Delete before the preflight so only genuine problems trip it.
DELETE FROM session_variables
 WHERE session_id::TEXT !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$';

-- Preflight: fail fast, with named offenders, if any to-be-converted column
-- still holds a value that cannot become uuid. A bare ALTER ... USING cast
-- failure reports only the value, not the column — this names both.
DO $$
DECLARE
    problems TEXT := '';
    n BIGINT;
    tbl TEXT;
BEGIN
    SELECT count(*) INTO n FROM rule_overrides
     WHERE session_id IS NOT NULL
       AND session_id::TEXT !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$';
    IF n > 0 THEN
        problems := problems || format('rule_overrides.session_id=%s ', n);
    END IF;

    FOREACH tbl IN ARRAY ARRAY[
        'gwiki_documents', 'gwiki_chunks', 'gwiki_links', 'gwiki_sources', 'gwiki_ingestions'
    ] LOOP
        IF to_regclass(tbl) IS NOT NULL THEN
            EXECUTE format(
                'SELECT count(*) FROM %I WHERE project_id IS NOT NULL AND project_id::TEXT <> '''' '
                || 'AND project_id::TEXT !~* ''^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$''',
                tbl
            ) INTO n;
            IF n > 0 THEN
                problems := problems || format('%s.project_id=%s ', tbl, n);
            END IF;
        END IF;
    END LOOP;

    IF problems <> '' THEN
        RAISE EXCEPTION 'uuid preflight failed; non-uuid values remain in: %', problems;
    END IF;
END;
$$;

-- (1) Module-scope calls have no enclosing symbol: NULL caller is legal.
--     Dedup already tolerates it (code_calls_unique_call_target is
--     UNIQUE NULLS NOT DISTINCT).
ALTER TABLE code_calls
    ALTER COLUMN caller_symbol_id DROP NOT NULL;

-- (2) Actor identity for stage transitions. Values: 'dispatcher', 'system',
--     'session' (a real session acted; its uuid is in *_by_session_id).
--     No backfill: the applied 304 already nulled historical actor labels and
--     pre-existing NULLs are indistinguishable from them.
ALTER TABLE task_stage_states
    ADD COLUMN IF NOT EXISTS entered_by_actor TEXT,
    ADD COLUMN IF NOT EXISTS completed_by_actor TEXT;

-- (3) Missed session-UUID columns.
ALTER TABLE session_variables
    ALTER COLUMN session_id TYPE UUID USING session_id::UUID;
ALTER TABLE rule_overrides
    ALTER COLUMN session_id TYPE UUID USING session_id::UUID;

DO $$
DECLARE
    tbl TEXT;
BEGIN
    FOREACH tbl IN ARRAY ARRAY[
        'gwiki_documents', 'gwiki_chunks', 'gwiki_links', 'gwiki_sources', 'gwiki_ingestions'
    ] LOOP
        IF to_regclass(tbl) IS NOT NULL AND (
            SELECT data_type FROM information_schema.columns
             WHERE table_schema = current_schema()
               AND table_name = tbl AND column_name = 'project_id'
        ) = 'text' THEN
            EXECUTE format(
                'ALTER TABLE %I ALTER COLUMN project_id TYPE UUID '
                || 'USING NULLIF(project_id::TEXT, '''')::UUID',
                tbl
            );
        END IF;
    END LOOP;
END;
$$;

-- (4) Dead table.
DROP TABLE IF EXISTS agent_commands;
