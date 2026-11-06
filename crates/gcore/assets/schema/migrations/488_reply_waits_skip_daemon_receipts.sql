-- The daemon writes agent_started and agent_recovery receipts in the child's
-- name. The child never authored them, so they never answer a reply wait.
CREATE OR REPLACE FUNCTION coordination_message_committed() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE wait_id text;
BEGIN
    PERFORM id FROM sessions WHERE id = NEW.from_session FOR UPDATE;
    IF NEW.message_type NOT IN ('agent_started', 'agent_recovery') THEN
        FOR wait_id IN SELECT id FROM coordination_waits
          WHERE owner_session_id = NEW.from_session AND waiter_session_id = NEW.to_session
            AND outcome = 'waiting' AND reply
        LOOP
            PERFORM resolve_coordination_wait(wait_id, NEW.id::text);
        END LOOP;
    END IF;
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
