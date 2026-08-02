ALTER TABLE workflow_definitions
    ADD COLUMN IF NOT EXISTS enabled_user_modified BOOLEAN NOT NULL DEFAULT FALSE;
