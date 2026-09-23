-- Only a live waiting row owns (waiter, owner, condition). A resolved,
-- timed-out, or cancelled wait must not block the next registration.
ALTER TABLE coordination_waits
    DROP CONSTRAINT IF EXISTS coordination_waits_waiter_session_id_owner_session_id_condi_key;
CREATE UNIQUE INDEX IF NOT EXISTS coordination_waits_live_identity
    ON coordination_waits (waiter_session_id, owner_session_id, condition_key)
    WHERE outcome = 'waiting';

-- A release message resolves the one waiting row that consumes it. A later
-- registration of the same condition starts waiting again.
CREATE OR REPLACE FUNCTION resolve_coordination_wait(wait_id text, reply_message_id text DEFAULT NULL)
RETURNS void
LANGUAGE plpgsql AS $$
DECLARE
    w coordination_waits%ROWTYPE;
    owner_status text;
    release_id text;
    terminal_outcome text;
BEGIN
    SELECT * INTO w FROM coordination_waits WHERE id = wait_id FOR UPDATE;
    IF NOT FOUND OR w.outcome <> 'waiting' THEN RETURN; END IF;
    SELECT status INTO owner_status FROM sessions WHERE id = w.owner_session_id;
    IF NOT w.reply THEN
        SELECT id INTO release_id FROM inter_session_messages AS message
          WHERE message.from_session = w.owner_session_id
            AND message.to_session = w.waiter_session_id
            AND message.message_type = 'coordination_release'
            AND message.metadata_json::jsonb ->> 'coordination_key' = w.coordination_key
            AND message.sent_at <= w.expires_at
            AND NOT EXISTS (
                SELECT 1 FROM coordination_waits AS previous
                WHERE previous.message_id = message.id::text
                  AND previous.id <> w.id
                  AND previous.outcome = 'released'
            )
          ORDER BY message.sent_at, message.id
          LIMIT 1;
    END IF;
    terminal_outcome := CASE
      WHEN clock_timestamp() >= w.expires_at THEN 'timeout'
      WHEN w.reply AND reply_message_id IS NOT NULL THEN 'replied'
      WHEN release_id IS NOT NULL THEN 'released'
      WHEN owner_status = ANY(w.statuses) THEN 'status_matched'
      WHEN owner_status IS NULL OR owner_status IN
        ('completed', 'cancelled', 'closed', 'expired', 'deleted') THEN 'owner_ended'
      ELSE NULL END;
    IF terminal_outcome IS NOT NULL THEN
        UPDATE coordination_waits SET outcome = terminal_outcome,
          matched_status = CASE WHEN terminal_outcome = 'status_matched' THEN owner_status END,
          message_id = CASE WHEN terminal_outcome = 'released' THEN release_id
                            WHEN terminal_outcome = 'replied' THEN reply_message_id END,
          completed_at = clock_timestamp() WHERE id = wait_id;
    END IF;
END;
$$;
