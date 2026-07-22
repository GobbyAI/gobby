-- Persist the latest attention episode for each roster entry.
CREATE TABLE IF NOT EXISTS attention_states (
    entry_id TEXT PRIMARY KEY,
    run_id TEXT,
    session_id TEXT,
    attention_id TEXT NOT NULL,
    state TEXT CHECK (state IS NULL OR state = 'blocked'),
    reason TEXT,
    kind TEXT CHECK (kind IS NULL OR kind IN ('actionable', 'non_actionable')),
    fingerprint TEXT,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    since TIMESTAMPTZ,
    seen_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (
        state IS NULL
        OR (
            attention_id IS NOT NULL
            AND reason IS NOT NULL
            AND kind IS NOT NULL
            AND fingerprint IS NOT NULL
            AND since IS NOT NULL
        )
    )
);

CREATE INDEX IF NOT EXISTS idx_attention_states_blocked
    ON attention_states(updated_at DESC)
    WHERE state = 'blocked';
