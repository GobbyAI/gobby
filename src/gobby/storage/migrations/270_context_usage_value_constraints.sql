-- Enforce normalized context usage token and confidence values on existing installs.

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'sessions_context_usage_tokens_nonnegative'
          AND conrelid = 'sessions'::regclass
    ) THEN
        ALTER TABLE sessions
        ADD CONSTRAINT sessions_context_usage_tokens_nonnegative
        CHECK (
            (usage_input_tokens IS NULL OR usage_input_tokens >= 0)
            AND (usage_output_tokens IS NULL OR usage_output_tokens >= 0)
            AND (usage_cache_creation_tokens IS NULL OR usage_cache_creation_tokens >= 0)
            AND (usage_cache_read_tokens IS NULL OR usage_cache_read_tokens >= 0)
            AND (context_window IS NULL OR context_window >= 0)
            AND (context_used_tokens IS NULL OR context_used_tokens >= 0)
            AND (last_prompt_input_tokens IS NULL OR last_prompt_input_tokens >= 0)
            AND (last_prompt_uncached_input_tokens IS NULL OR last_prompt_uncached_input_tokens >= 0)
            AND (last_prompt_cache_read_tokens IS NULL OR last_prompt_cache_read_tokens >= 0)
            AND (last_prompt_cache_creation_tokens IS NULL OR last_prompt_cache_creation_tokens >= 0)
            AND (last_completion_output_tokens IS NULL OR last_completion_output_tokens >= 0)
        );
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'sessions_context_usage_confidence_valid'
          AND conrelid = 'sessions'::regclass
    ) THEN
        ALTER TABLE sessions
        ADD CONSTRAINT sessions_context_usage_confidence_valid
        CHECK (
            context_usage_confidence IS NULL
            OR context_usage_confidence IN ('reported', 'estimated', 'unknown')
        );
    END IF;
END $$;
