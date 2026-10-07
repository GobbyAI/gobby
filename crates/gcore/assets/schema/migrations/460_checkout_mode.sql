-- Checkout separation is independent of sandbox isolation.
ALTER TABLE tasks RENAME COLUMN isolation TO checkout_mode;
ALTER TABLE tasks RENAME CONSTRAINT tasks_isolation_check TO tasks_checkout_mode_check;
ALTER TABLE build_profiles RENAME COLUMN isolation TO checkout_mode;
ALTER TABLE build_profiles RENAME CONSTRAINT build_profiles_isolation_check TO build_profiles_checkout_mode_check;

UPDATE agent_definitions
SET definition_json = (definition_json - 'isolation')
                      || jsonb_build_object('checkout_mode', definition_json -> 'isolation'),
    updated_at = CURRENT_TIMESTAMP
WHERE definition_json ? 'isolation';
