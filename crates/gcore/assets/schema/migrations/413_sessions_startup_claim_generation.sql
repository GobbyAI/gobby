-- Durable startup-context claim generation. A boolean cannot identify the
-- claimant for commit, rollback, or invalidation. generation is monotonic;
-- owner is the worker token holding a live claim; state is
-- idle|claimed|committed|invalidated.

ALTER TABLE sessions
    ADD COLUMN IF NOT EXISTS startup_claim_generation integer DEFAULT 0 NOT NULL,
    ADD COLUMN IF NOT EXISTS startup_claim_owner text,
    ADD COLUMN IF NOT EXISTS startup_claim_state text DEFAULT 'idle' NOT NULL;

ALTER TABLE sessions
    DROP CONSTRAINT IF EXISTS sessions_startup_claim_generation_nonnegative;

ALTER TABLE sessions
    ADD CONSTRAINT sessions_startup_claim_generation_nonnegative
    CHECK (startup_claim_generation >= 0);

ALTER TABLE sessions
    DROP CONSTRAINT IF EXISTS sessions_startup_claim_state_valid;

ALTER TABLE sessions
    ADD CONSTRAINT sessions_startup_claim_state_valid
    CHECK (startup_claim_state IN ('idle', 'claimed', 'committed', 'invalidated'));
