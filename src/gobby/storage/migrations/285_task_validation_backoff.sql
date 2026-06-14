-- Durable backoff state for validation LLM infrastructure failures. When every
-- generation candidate fails (timeout/unavailable/transport), the dispatcher and
-- manual close path must back off instead of re-running validation every
-- heartbeat. Persisted so the backoff survives daemon restarts.
CREATE TABLE IF NOT EXISTS task_validation_backoff (
    task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    next_retry_at TIMESTAMPTZ,
    last_error TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
