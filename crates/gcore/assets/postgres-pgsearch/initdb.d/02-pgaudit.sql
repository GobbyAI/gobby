CREATE EXTENSION IF NOT EXISTS pgaudit;

-- Audit-only probe row. Created here so the validation-window healthcheck has
-- a stable target before the application schema exists.
CREATE TABLE IF NOT EXISTS _pgaudit_probe (
    id              INTEGER PRIMARY KEY,
    last_probed_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO _pgaudit_probe (id) VALUES (1)
ON CONFLICT (id) DO NOTHING;
