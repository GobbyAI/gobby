-- Latest successful provider-capacity observation for each local machine.
-- Python storage/provider_capacity.py owns DML; schema authority stays here.

CREATE TABLE IF NOT EXISTS provider_capacity_snapshots (
    machine_id uuid NOT NULL REFERENCES machines(id) ON DELETE CASCADE,
    provider text NOT NULL,
    state text NOT NULL,
    observed_at timestamptz NOT NULL,
    windows jsonb NOT NULL,
    reason text,
    source_version text NOT NULL,
    PRIMARY KEY (machine_id, provider),
    CONSTRAINT provider_capacity_snapshots_provider_nonempty CHECK (btrim(provider) <> ''),
    CONSTRAINT provider_capacity_snapshots_state_valid CHECK (
        state IN ('available', 'exhausted')
    ),
    CONSTRAINT provider_capacity_snapshots_windows_array CHECK (
        jsonb_typeof(windows) = 'array'
    )
);
