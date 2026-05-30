-- Enforce normalized context usage ratios on existing PostgreSQL installs.

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'sessions_context_usage_ratio_range'
          AND conrelid = 'sessions'::regclass
    ) THEN
        ALTER TABLE sessions
        ADD CONSTRAINT sessions_context_usage_ratio_range
        CHECK (
            context_usage_ratio IS NULL
            OR (context_usage_ratio >= 0 AND context_usage_ratio <= 1)
        );
    END IF;
END $$;
