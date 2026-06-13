ALTER TABLE mcp_servers
    ADD COLUMN IF NOT EXISTS requires_oauth BOOLEAN DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS oauth_provider TEXT,
    ADD COLUMN IF NOT EXISTS connect_timeout DOUBLE PRECISION DEFAULT 30.0;

UPDATE mcp_servers
SET
    requires_oauth = COALESCE(requires_oauth, FALSE),
    connect_timeout = COALESCE(connect_timeout, 30.0);
