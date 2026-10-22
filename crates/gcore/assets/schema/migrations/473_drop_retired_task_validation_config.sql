-- #23341 removed these TaskValidationConfig fields from the registry.
-- Keep model-selection settings used by found-work confirmation and feedback.
DO $migration$
DECLARE
    current_revision bigint;
    retired_keys constant text[] := ARRAY[
        'gobby-tasks.validation.auto_generate_on_create',
        'gobby-tasks.validation.auto_generate_on_expand',
        'gobby-tasks.validation.close_review_max_concurrency_per_project',
        'gobby-tasks.validation.close_review_min_severity',
        'gobby-tasks.validation.close_review_prompt_max_chars',
        'gobby-tasks.validation.close_review_total_timeout_seconds',
        'gobby-tasks.validation.close_review_validator_timeout_seconds',
        'gobby-tasks.validation.close_validation_escalation_threshold',
        'gobby-tasks.validation.criteria_prompt_path',
        'gobby-tasks.validation.criteria_system_prompt',
        'gobby-tasks.validation.escalation_enabled',
        'gobby-tasks.validation.escalation_notify',
        'gobby-tasks.validation.escalation_webhook_url',
        'gobby-tasks.validation.max_iterations',
        'gobby-tasks.validation.require_clean_attributed_paths_on_close'
    ];
BEGIN
    SELECT revision INTO STRICT current_revision
      FROM config_state WHERE id = true FOR UPDATE;

    IF EXISTS (SELECT 1 FROM config_store WHERE key = ANY(retired_keys)) THEN
        IF current_revision >= 9007199254740991 THEN
            RAISE EXCEPTION 'Configuration revision exhausted before migration 473';
        END IF;

        DELETE FROM config_store WHERE key = ANY(retired_keys);
        UPDATE config_state SET revision = current_revision + 1 WHERE id = true;
    END IF;
END;
$migration$;
