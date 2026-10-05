-- Cumulative usage can exceed int32 on long-lived sessions.
ALTER TABLE sessions
    ALTER COLUMN usage_input_tokens TYPE bigint,
    ALTER COLUMN usage_output_tokens TYPE bigint,
    ALTER COLUMN usage_cache_creation_tokens TYPE bigint,
    ALTER COLUMN usage_cache_read_tokens TYPE bigint;
