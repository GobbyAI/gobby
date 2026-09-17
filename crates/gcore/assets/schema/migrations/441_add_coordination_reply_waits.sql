-- A reply wait holds for the next ordinary message the owner session sends the
-- waiter, so a worker that asked its parent a question can yield the turn
-- durably instead of polling or being swept up as stuck.
ALTER TABLE coordination_waits ADD COLUMN reply boolean NOT NULL DEFAULT false;
ALTER TABLE coordination_waits DROP CONSTRAINT coordination_waits_check;
ALTER TABLE coordination_waits ADD CONSTRAINT coordination_waits_check
    CHECK ((coordination_key IS NOT NULL)::int + (statuses IS NOT NULL)::int + reply::int = 1);
ALTER TABLE coordination_waits DROP CONSTRAINT coordination_waits_outcome_check;
ALTER TABLE coordination_waits ADD CONSTRAINT coordination_waits_outcome_check
    CHECK (outcome IN ('waiting', 'released', 'replied', 'status_matched', 'owner_ended',
                       'cancelled', 'timeout'));

-- Reply resolution is commit-ordered, never clock-ordered: only the committed
-- message trigger names a reply message, so a message that committed before the
-- wait existed never resolves it and no sender/hub clock skew can fake one.
DROP TRIGGER coordination_release_committed ON inter_session_messages;
DROP FUNCTION coordination_release_committed();
DROP FUNCTION resolve_coordination_wait(text);

CREATE FUNCTION resolve_coordination_wait(wait_id text, reply_message_id text DEFAULT NULL)
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
        SELECT id INTO release_id FROM inter_session_messages
          WHERE from_session = w.owner_session_id AND to_session = w.waiter_session_id
            AND message_type = 'coordination_release'
            AND metadata_json::jsonb ->> 'coordination_key' = w.coordination_key
            AND sent_at <= w.expires_at
          ORDER BY sent_at, id LIMIT 1;
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

-- Every message takes the owner fence, not just a release: a reply wait that
-- registers concurrently commits before this loop reads it, so the reply it was
-- registered for can never slip past unseen.
CREATE FUNCTION coordination_message_committed() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE wait_id text;
BEGIN
    PERFORM id FROM sessions WHERE id = NEW.from_session FOR UPDATE;
    FOR wait_id IN SELECT id FROM coordination_waits
      WHERE owner_session_id = NEW.from_session AND waiter_session_id = NEW.to_session
        AND outcome = 'waiting' AND reply
    LOOP
        PERFORM resolve_coordination_wait(wait_id, NEW.id::text);
    END LOOP;
    IF NEW.message_type = 'coordination_release' THEN
        FOR wait_id IN SELECT id FROM coordination_waits
          WHERE owner_session_id = NEW.from_session AND waiter_session_id = NEW.to_session
            AND outcome = 'waiting'
            AND coordination_key = NEW.metadata_json::jsonb ->> 'coordination_key'
        LOOP
            PERFORM resolve_coordination_wait(wait_id);
        END LOOP;
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER coordination_message_committed AFTER INSERT ON inter_session_messages
    FOR EACH ROW EXECUTE FUNCTION coordination_message_committed();

REVOKE ALL ON FUNCTION resolve_coordination_wait(text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION coordination_message_committed() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION resolve_coordination_wait(text, text),
    coordination_message_committed() TO gobby_daemon_runtime;
