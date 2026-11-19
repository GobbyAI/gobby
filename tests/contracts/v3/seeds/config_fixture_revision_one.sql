-- Fixture state for config_values_patch: the configuration revision is 1 and the
-- code_index.enabled row holds its fixture value, so the PATCH below is the only write.
UPDATE config_state SET revision = 1 WHERE id = TRUE;
UPDATE config_store
   SET value = 'false', source = 'e2e-fixture', revision = 1
 WHERE key = 'code_index.enabled';
