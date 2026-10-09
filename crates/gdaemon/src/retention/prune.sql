-- A single statement is one transaction: deleted usage is archived or nothing is removed.
WITH candidates AS (
    SELECT id
    FROM token_events
    WHERE event_at < $1::text::timestamptz
    ORDER BY event_at, id
    LIMIT 10000
    FOR UPDATE
), deleted AS (
    DELETE FROM token_events
    WHERE id IN (SELECT id FROM candidates)
    RETURNING session_id, input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens
), archived AS (
    INSERT INTO token_event_retention_totals (
        session_id, input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens
    )
    SELECT session_id, sum(input_tokens), sum(output_tokens),
           sum(cache_creation_tokens), sum(cache_read_tokens)
    FROM deleted
    GROUP BY session_id
    ON CONFLICT (session_id) DO UPDATE SET
        input_tokens = token_event_retention_totals.input_tokens + excluded.input_tokens,
        output_tokens = token_event_retention_totals.output_tokens + excluded.output_tokens,
        cache_creation_tokens = token_event_retention_totals.cache_creation_tokens
            + excluded.cache_creation_tokens,
        cache_read_tokens = token_event_retention_totals.cache_read_tokens
            + excluded.cache_read_tokens
    RETURNING session_id
)
SELECT count(*)::bigint FROM deleted;
