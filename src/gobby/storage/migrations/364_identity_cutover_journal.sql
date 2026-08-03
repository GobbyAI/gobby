-- Non-destructive precursor for the app-level machine identity cutover.
ALTER TABLE sessions
    ALTER COLUMN machine_id DROP NOT NULL;

CREATE TABLE IF NOT EXISTS identity_cutover_journal (
    old_id TEXT PRIMARY KEY,
    new_id UUID,
    disposition TEXT NOT NULL CHECK (disposition IN ('rotated', 'retired')),
    phase TEXT NOT NULL CHECK (phase IN ('started', 'db_committed', 'file_committed')),
    token UUID NOT NULL UNIQUE,
    had_machine BOOLEAN NOT NULL,
    session_count BIGINT NOT NULL,
    machine_snapshot JSONB,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    db_committed_at TIMESTAMPTZ,
    file_committed_at TIMESTAMPTZ,
    CHECK ((disposition = 'rotated' AND new_id IS NOT NULL) OR disposition = 'retired')
);

CREATE TABLE IF NOT EXISTS retired_machine_identities (
    old_id TEXT PRIMARY KEY,
    retired_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    disposition TEXT NOT NULL
);

CREATE OR REPLACE FUNCTION gobby_identity_cutover_fence()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    supplied_token TEXT;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM identity_cutover_journal) THEN
        IF TG_OP = 'DELETE' THEN
            RETURN OLD;
        END IF;
        RETURN NEW;
    END IF;

    supplied_token := current_setting('gobby.identity_cutover', TRUE);
    IF EXISTS (
        SELECT 1
        FROM identity_cutover_journal
        WHERE token::TEXT = supplied_token
          AND phase <> 'file_committed'
    ) THEN
        IF TG_OP = 'DELETE' THEN
            RETURN OLD;
        END IF;
        RETURN NEW;
    END IF;

    IF TG_TABLE_NAME = 'machines'
       OR TG_OP = 'DELETE'
       OR TG_OP = 'INSERT' AND NEW.machine_id IS NOT NULL
       OR TG_OP = 'UPDATE' AND NEW.machine_id IS DISTINCT FROM OLD.machine_id THEN
        RAISE EXCEPTION USING
            ERRCODE = '55000',
            MESSAGE = 'Gobby identity cutover fence rejects this identity write';
    END IF;

    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS machines_identity_cutover_fence ON machines;
CREATE TRIGGER machines_identity_cutover_fence
BEFORE INSERT OR UPDATE OR DELETE ON machines
FOR EACH ROW EXECUTE FUNCTION gobby_identity_cutover_fence();

DROP TRIGGER IF EXISTS sessions_identity_cutover_fence ON sessions;
CREATE TRIGGER sessions_identity_cutover_fence
BEFORE INSERT OR UPDATE OF machine_id OR DELETE ON sessions
FOR EACH ROW EXECUTE FUNCTION gobby_identity_cutover_fence();
