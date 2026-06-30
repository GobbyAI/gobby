CREATE TABLE IF NOT EXISTS machines (
    machine_id TEXT PRIMARY KEY,
    hostname TEXT,
    os TEXT,
    label TEXT,
    tailscale_name TEXT,
    owner_user_id TEXT,
    first_seen TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE machines ADD COLUMN IF NOT EXISTS hostname TEXT;
ALTER TABLE machines ADD COLUMN IF NOT EXISTS os TEXT;
ALTER TABLE machines ADD COLUMN IF NOT EXISTS label TEXT;
ALTER TABLE machines ADD COLUMN IF NOT EXISTS tailscale_name TEXT;
ALTER TABLE machines ADD COLUMN IF NOT EXISTS owner_user_id TEXT;
ALTER TABLE machines ADD COLUMN IF NOT EXISTS first_seen TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE machines ADD COLUMN IF NOT EXISTS last_seen TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE INDEX IF NOT EXISTS idx_machines_last_seen ON machines(last_seen);

CREATE INDEX IF NOT EXISTS idx_machines_owner_user_id ON machines(owner_user_id)
WHERE owner_user_id IS NOT NULL;
