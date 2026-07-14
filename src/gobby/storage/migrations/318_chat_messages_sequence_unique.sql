WITH ranked_messages AS (
    SELECT
        id,
        conversation_id,
        seq,
        created_at,
        ROW_NUMBER() OVER (
            PARTITION BY conversation_id, seq
            ORDER BY created_at, id
        ) AS duplicate_rank,
        MAX(seq) OVER (PARTITION BY conversation_id) AS max_seq
    FROM chat_messages
),
duplicate_messages AS (
    SELECT
        id,
        max_seq + ROW_NUMBER() OVER (
            PARTITION BY conversation_id
            ORDER BY seq, created_at, id
        ) AS replacement_seq
    FROM ranked_messages
    WHERE duplicate_rank > 1
)
UPDATE chat_messages AS messages
SET seq = duplicates.replacement_seq
FROM duplicate_messages AS duplicates
WHERE messages.id = duplicates.id;

DROP INDEX IF EXISTS idx_chat_messages_conv_seq;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'chat_messages_conversation_seq_unique'
          AND conrelid = 'chat_messages'::regclass
    ) THEN
        ALTER TABLE chat_messages
        ADD CONSTRAINT chat_messages_conversation_seq_unique
        UNIQUE (conversation_id, seq);
    END IF;
END
$$;
