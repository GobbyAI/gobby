ALTER TABLE schema_migrations
    ADD COLUMN IF NOT EXISTS filename TEXT,
    ADD COLUMN IF NOT EXISTS checksum TEXT;

CREATE TABLE IF NOT EXISTS maintenance_epochs (
    id UUID PRIMARY KEY,
    campaign TEXT NOT NULL
        CHECK (
            campaign IN (
                'schema-apply',
                'purge',
                'reconcile',
                'identity-cutover',
                'flatten'
            )
        ),
    opened_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    opened_by TEXT NOT NULL,
    scope_note TEXT NOT NULL,
    released_at TIMESTAMPTZ,
    released_by_command TEXT,
    CHECK (
        (released_at IS NULL AND released_by_command IS NULL)
        OR
        (released_at IS NOT NULL AND released_by_command IS NOT NULL)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS maintenance_epochs_one_open
    ON maintenance_epochs ((TRUE))
    WHERE released_at IS NULL;

CREATE INDEX IF NOT EXISTS maintenance_epochs_open_lookup
    ON maintenance_epochs (opened_at, id)
    WHERE released_at IS NULL;

CREATE TABLE IF NOT EXISTS destructive_batches (
    id UUID PRIMARY KEY,
    maintenance_epoch_id UUID NOT NULL
        REFERENCES maintenance_epochs(id)
        DEFERRABLE INITIALLY DEFERRED,
    campaign TEXT NOT NULL
        CHECK (
            campaign IN (
                'schema-apply',
                'purge',
                'reconcile',
                'identity-cutover',
                'flatten'
            )
        ),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'applied', 'verified', 'aborted')),
    backup_manifest_path TEXT,
    backup_manifest_sha256 TEXT
        CHECK (
            backup_manifest_sha256 IS NULL
            OR backup_manifest_sha256 ~ '^[0-9a-f]{64}$'
        ),
    intent JSONB NOT NULL DEFAULT '{}'::JSONB
        CHECK (jsonb_typeof(intent) = 'object'),
    migration_plan JSONB NOT NULL DEFAULT '[]'::JSONB
        CHECK (jsonb_typeof(migration_plan) = 'array'),
    target_receipts JSONB NOT NULL DEFAULT '{}'::JSONB
        CHECK (jsonb_typeof(target_receipts) = 'object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    verified_at TIMESTAMPTZ,
    aborted_at TIMESTAMPTZ,
    abort_disposition TEXT,
    UNIQUE (maintenance_epoch_id),
    CHECK (
        (status = 'verified' AND verified_at IS NOT NULL)
        OR
        (status <> 'verified' AND verified_at IS NULL)
    ),
    CHECK (
        (
            status = 'aborted'
            AND aborted_at IS NOT NULL
            AND abort_disposition IS NOT NULL
        )
        OR
        (
            status <> 'aborted'
            AND aborted_at IS NULL
            AND abort_disposition IS NULL
        )
    )
);

CREATE INDEX IF NOT EXISTS destructive_batches_epoch_lookup
    ON destructive_batches (maintenance_epoch_id, created_at);

DO $migration$
DECLARE
    target_schema TEXT := current_schema();
    trigger_name TEXT :=
        'gobby_maintenance_epoch_login_' || SUBSTRING(MD5(current_schema()) FOR 16);
BEGIN
    EXECUTE FORMAT(
        $function$
        CREATE OR REPLACE FUNCTION %I.gobby_maintenance_epoch_login_guard()
        RETURNS event_trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        AS $body$
        DECLARE
            active_epoch UUID;
            supplied_epoch TEXT;
        BEGIN
            IF pg_catalog.current_schema() IS DISTINCT FROM %L THEN
                RETURN;
            END IF;
            IF pg_catalog.pg_is_in_recovery() THEN
                RETURN;
            END IF;

            SELECT id
            INTO active_epoch
            FROM %I.maintenance_epochs
            WHERE released_at IS NULL;

            IF active_epoch IS NULL THEN
                RETURN;
            END IF;

            supplied_epoch :=
                pg_catalog.current_setting('gobby.maintenance_epoch', TRUE);
            IF supplied_epoch IS DISTINCT FROM active_epoch::TEXT THEN
                RAISE EXCEPTION USING
                    ERRCODE = '55000',
                    MESSAGE = pg_catalog.format(
                        'GOBBY_MAINTENANCE_EPOCH %%s is active; reconnect through '
                        '`gobby hub-maintenance resume` with the epoch token '
                        '(repair escape: superuser options=''-c event_triggers=off'')',
                        active_epoch
                    );
            END IF;
        END;
        $body$
        $function$,
        target_schema,
        target_schema,
        target_schema
    );

    IF NOT EXISTS (
        SELECT 1
        FROM pg_catalog.pg_event_trigger
        WHERE evtname = trigger_name
    ) THEN
        EXECUTE FORMAT(
            'CREATE EVENT TRIGGER %I ON login '
            'EXECUTE FUNCTION %I.gobby_maintenance_epoch_login_guard()',
            trigger_name,
            target_schema
        );
    END IF;

    EXECUTE FORMAT('ALTER EVENT TRIGGER %I ENABLE ALWAYS', trigger_name);
END;
$migration$;
