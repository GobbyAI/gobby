DO $migration$
DECLARE
    target_schema TEXT := current_schema();
BEGIN
    EXECUTE FORMAT(
        $function$
        CREATE OR REPLACE FUNCTION %I.gobby_maintenance_epoch_login_guard()
        RETURNS event_trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog
        AS $body$
        DECLARE
            active_epoch UUID;
            supplied_epoch TEXT;
        BEGIN
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
                    MESSAGE =
                        'Gobby hub maintenance is active; use '
                        '`gobby hub-maintenance status` to inspect it or '
                        '`gobby hub-maintenance resume` from the operator shell.';
            END IF;
        END;
        $body$
        $function$,
        target_schema,
        target_schema
    );
END;
$migration$;
