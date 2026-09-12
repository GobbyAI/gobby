CREATE TABLE coordination_waits (
    id text PRIMARY KEY,
    waiter_session_id uuid NOT NULL,
    owner_session_id uuid NOT NULL,
    condition_key text NOT NULL,
    coordination_key text,
    statuses text[],
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    expires_at timestamptz NOT NULL,
    outcome text NOT NULL DEFAULT 'waiting'
        CHECK (outcome IN ('waiting', 'released', 'status_matched', 'owner_ended',
                           'cancelled', 'timeout')),
    matched_status text,
    message_id text,
    completed_at timestamptz,
    delivered_at timestamptz,
    CHECK ((coordination_key IS NOT NULL) <> (statuses IS NOT NULL)),
    UNIQUE (waiter_session_id, owner_session_id, condition_key)
);
CREATE INDEX coordination_waits_owner ON coordination_waits (owner_session_id)
    WHERE outcome = 'waiting';
CREATE INDEX coordination_waits_pending ON coordination_waits (waiter_session_id)
    WHERE delivered_at IS NULL;
GRANT SELECT, INSERT, UPDATE, DELETE ON coordination_waits TO gobby_daemon_runtime;

-- Registration and release insertion fence on the owner row. Status updates
-- already hold that lock, closing the check/subscribe gap across connections.
CREATE FUNCTION resolve_coordination_wait(wait_id text) RETURNS void
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
    SELECT id INTO release_id FROM inter_session_messages
      WHERE from_session = w.owner_session_id AND to_session = w.waiter_session_id
        AND message_type = 'coordination_release'
        AND metadata_json::jsonb ->> 'coordination_key' = w.coordination_key
        AND sent_at <= w.expires_at
      ORDER BY sent_at, id LIMIT 1;
    terminal_outcome := CASE
      WHEN clock_timestamp() >= w.expires_at THEN 'timeout'
      WHEN release_id IS NOT NULL THEN 'released'
      WHEN owner_status = ANY(w.statuses) THEN 'status_matched'
      WHEN owner_status IS NULL OR owner_status IN
        ('completed', 'cancelled', 'closed', 'expired', 'deleted') THEN 'owner_ended'
      ELSE NULL END;
    IF terminal_outcome IS NOT NULL THEN
        UPDATE coordination_waits SET outcome = terminal_outcome,
          matched_status = CASE WHEN terminal_outcome = 'status_matched' THEN owner_status END,
          message_id = CASE WHEN terminal_outcome = 'released' THEN release_id END,
          completed_at = clock_timestamp() WHERE id = wait_id;
    END IF;
END;
$$;

CREATE FUNCTION coordination_wait_changed() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' OR OLD.outcome IS DISTINCT FROM NEW.outcome THEN
        PERFORM pg_notify('gobby_coordination_wait', NEW.id);
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER coordination_wait_changed AFTER INSERT OR UPDATE ON coordination_waits
    FOR EACH ROW EXECUTE FUNCTION coordination_wait_changed();

CREATE FUNCTION coordination_release_committed() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE wait_id text;
BEGIN
    IF NEW.message_type <> 'coordination_release' THEN RETURN NEW; END IF;
    PERFORM id FROM sessions WHERE id = NEW.from_session FOR UPDATE;
    FOR wait_id IN SELECT id FROM coordination_waits
      WHERE owner_session_id = NEW.from_session AND waiter_session_id = NEW.to_session
        AND outcome = 'waiting'
        AND coordination_key = NEW.metadata_json::jsonb ->> 'coordination_key'
    LOOP
        PERFORM resolve_coordination_wait(wait_id);
    END LOOP;
    RETURN NEW;
END;
$$;
CREATE TRIGGER coordination_release_committed AFTER INSERT ON inter_session_messages
    FOR EACH ROW EXECUTE FUNCTION coordination_release_committed();

CREATE FUNCTION coordination_owner_changed() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE wait_id text;
BEGIN
    FOR wait_id IN SELECT id FROM coordination_waits
      WHERE owner_session_id = OLD.id AND outcome = 'waiting'
    LOOP
        PERFORM resolve_coordination_wait(wait_id);
    END LOOP;
    RETURN NULL;
END;
$$;
CREATE TRIGGER coordination_owner_changed AFTER UPDATE OF status OR DELETE ON sessions
    FOR EACH ROW EXECUTE FUNCTION coordination_owner_changed();

REVOKE ALL ON FUNCTION resolve_coordination_wait(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION coordination_wait_changed() FROM PUBLIC;
REVOKE ALL ON FUNCTION coordination_release_committed() FROM PUBLIC;
REVOKE ALL ON FUNCTION coordination_owner_changed() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION resolve_coordination_wait(text), coordination_wait_changed(),
    coordination_release_committed(), coordination_owner_changed() TO gobby_daemon_runtime;
