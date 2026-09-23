-- Drop the stored override for gobby-tasks.validation.system_prompt. #22636
-- removed the key from the config registry with the one-shot close-validation
-- path, and the registry fails closed on residual stored keys at every read.
DO $migration$
DECLARE
    current_revision bigint;
BEGIN
    SELECT revision INTO STRICT current_revision
      FROM config_state WHERE id = true FOR UPDATE;

    IF EXISTS (
        SELECT 1 FROM config_store WHERE key = 'gobby-tasks.validation.system_prompt'
    ) THEN
        IF current_revision >= 9007199254740991 THEN
            RAISE EXCEPTION 'Configuration revision exhausted before migration 445';
        END IF;

        DELETE FROM config_store WHERE key = 'gobby-tasks.validation.system_prompt';
        UPDATE config_state SET revision = current_revision + 1 WHERE id = true;
    END IF;
END;
$migration$;
