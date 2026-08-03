-- gobby:destructive
-- Finalize the app-level identity cutover after every journal row is durable.
DO $guard$
DECLARE
    legacy_shape BOOLEAN;
    incomplete_count BIGINT;
    invalid_machine_count BIGINT;
    uncovered_count BIGINT;
    unmapped_count BIGINT;
BEGIN
    SELECT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'machines'
          AND column_name = 'machine_id'
    ) INTO legacy_shape;

    IF NOT legacy_shape
       AND NOT EXISTS (SELECT 1 FROM identity_cutover_journal) THEN
        RETURN;
    END IF;

    SELECT COUNT(*) INTO incomplete_count
    FROM identity_cutover_journal
    WHERE phase <> 'file_committed';
    IF incomplete_count > 0 THEN
        RAISE EXCEPTION
            'identity cutover has % journal rows not file_committed', incomplete_count;
    END IF;

    IF legacy_shape THEN
        EXECUTE $sql$
            SELECT COUNT(*)
            FROM machines
            WHERE NOT pg_input_is_valid(machine_id, 'uuid')
        $sql$ INTO invalid_machine_count;
        IF invalid_machine_count > 0 THEN
            RAISE EXCEPTION
                'machines UUID preflight found % uncastable identities',
                invalid_machine_count;
        END IF;
    END IF;

    IF legacy_shape THEN
        EXECUTE $sql$
            SELECT COUNT(*)
            FROM machines AS machine
            WHERE EXISTS (SELECT 1 FROM identity_cutover_journal)
              AND NOT EXISTS (
                  SELECT 1
                  FROM identity_cutover_journal AS journal
                  WHERE journal.disposition = 'rotated'
                    AND journal.phase = 'file_committed'
                    AND journal.new_id::TEXT = machine.machine_id
              )
        $sql$ INTO uncovered_count;
        EXECUTE $sql$
            SELECT COUNT(*)
            FROM sessions AS session
            LEFT JOIN machines AS machine ON machine.machine_id = session.machine_id
            WHERE session.machine_id IS NOT NULL
              AND machine.machine_id IS NULL
        $sql$ INTO unmapped_count;
    ELSE
        SELECT COUNT(*) INTO uncovered_count
        FROM machines AS machine
        WHERE EXISTS (SELECT 1 FROM identity_cutover_journal)
          AND NOT EXISTS (
              SELECT 1
              FROM identity_cutover_journal AS journal
              WHERE journal.disposition = 'rotated'
                AND journal.phase = 'file_committed'
                AND journal.new_id = machine.id
          );
        SELECT COUNT(*) INTO unmapped_count
        FROM sessions AS session
        LEFT JOIN machines AS machine ON machine.id::TEXT = session.machine_id
        WHERE session.machine_id IS NOT NULL
          AND machine.id IS NULL;
    END IF;

    IF uncovered_count > 0 THEN
        RAISE EXCEPTION
            'identity cutover has % machine rows without completed journal coverage',
            uncovered_count;
    END IF;
    IF unmapped_count > 0 THEN
        RAISE EXCEPTION
            'identity cutover zero-unmapped gate found % session identities', unmapped_count;
    END IF;
END;
$guard$;

DROP TRIGGER IF EXISTS machines_identity_cutover_fence ON machines;
DROP TRIGGER IF EXISTS sessions_identity_cutover_fence ON sessions;
DROP FUNCTION IF EXISTS gobby_identity_cutover_fence();

DO $machines$
DECLARE
    legacy_shape BOOLEAN;
BEGIN
    SELECT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'machines'
          AND column_name = 'machine_id'
    ) INTO legacy_shape;

    IF legacy_shape THEN
        EXECUTE 'ALTER TABLE machines RENAME COLUMN machine_id TO id';
        EXECUTE 'ALTER TABLE machines ALTER COLUMN id TYPE UUID USING id::UUID';
    ELSIF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'machines'
          AND column_name = 'id'
          AND data_type = 'uuid'
    ) THEN
        RAISE EXCEPTION 'machines has neither legacy machine_id nor UUID id shape';
    END IF;
END;
$machines$;

ALTER TABLE bin_update_state
    ADD COLUMN IF NOT EXISTS machine_id UUID;

DO $bin_backfill$
DECLARE
    missing_count BIGINT;
    machine_count BIGINT;
    sole_machine UUID;
BEGIN
    SELECT COUNT(*) INTO missing_count
    FROM bin_update_state
    WHERE machine_id IS NULL;
    IF missing_count = 0 THEN
        RETURN;
    END IF;

    SELECT COUNT(*) INTO machine_count FROM machines;
    IF machine_count <> 1 THEN
        RAISE EXCEPTION
            'cannot backfill % bin_update_state rows: expected one registered machine, found %',
            missing_count,
            machine_count;
    END IF;
    SELECT id INTO sole_machine FROM machines LIMIT 1;
    UPDATE bin_update_state SET machine_id = sole_machine WHERE machine_id IS NULL;
END;
$bin_backfill$;

ALTER TABLE bin_update_state
    ALTER COLUMN machine_id SET NOT NULL,
    DROP CONSTRAINT IF EXISTS bin_update_state_pkey;

ALTER TABLE bin_update_state
    ADD CONSTRAINT bin_update_state_pkey PRIMARY KEY (machine_id, tool_name);

DO $bin_fk$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'bin_update_state'::regclass
          AND conname = 'bin_update_state_machine_id_fkey'
    ) THEN
        ALTER TABLE bin_update_state
            ADD CONSTRAINT bin_update_state_machine_id_fkey
            FOREIGN KEY (machine_id) REFERENCES machines(id) ON DELETE CASCADE;
    END IF;
END;
$bin_fk$;
