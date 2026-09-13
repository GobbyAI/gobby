-- Preserve the external DaemonConfig alias in the revisioned registry.
-- Python attribute names are not a second persistent configuration namespace.
DO $migration$
DECLARE
    current_revision bigint;
BEGIN
    SELECT revision INTO STRICT current_revision
      FROM config_state WHERE id = true FOR UPDATE;

    IF EXISTS (SELECT 1 FROM config_store WHERE starts_with(key, 'gobby_tasks.')) THEN
        IF EXISTS (
            SELECT 1 FROM config_store old_key
            JOIN config_store canonical_key
              ON canonical_key.key = 'gobby-tasks.' || substr(old_key.key, 13)
            WHERE starts_with(old_key.key, 'gobby_tasks.')
        ) THEN
            RAISE EXCEPTION 'Task configuration alias collision: reconcile gobby_tasks.* and gobby-tasks.* overrides before migration 434';
        END IF;

        IF current_revision >= 9007199254740991 THEN
            RAISE EXCEPTION 'Configuration revision exhausted before migration 434';
        END IF;

        UPDATE config_store
           SET key = 'gobby-tasks.' || substr(key, 13),
               revision = current_revision + 1
         WHERE starts_with(key, 'gobby_tasks.');
        UPDATE config_state SET revision = current_revision + 1 WHERE id = true;
    END IF;
END;
$migration$;
