-- Keep poison transcripts retryable without monopolizing scheduled work.
ALTER TABLE sessions
    ADD COLUMN transcript_processing_failure_count integer NOT NULL DEFAULT 0
        CHECK (transcript_processing_failure_count >= 0),
    ADD COLUMN transcript_processing_last_error_code text,
    ADD COLUMN transcript_processing_last_error text,
    ADD COLUMN transcript_processing_last_failed_at timestamp with time zone;

DROP INDEX idx_sessions_pending_transcript;
CREATE INDEX idx_sessions_pending_transcript
    ON sessions (machine_id, created_at, id)
    WHERE status = 'expired' AND transcript_processed = FALSE
        AND transcript_processing_failure_count < 3;

-- Every session writer (including native writers) observes the same recovery rule.
CREATE FUNCTION reset_transcript_processing_on_recovery() RETURNS trigger
    LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.source IS DISTINCT FROM OLD.source
       OR NEW.transcript_path IS DISTINCT FROM OLD.transcript_path
       OR NEW.external_id IS DISTINCT FROM OLD.external_id
       OR (NEW.status IS DISTINCT FROM OLD.status AND NEW.status IN ('active', 'paused'))
    THEN
        NEW.transcript_processed := FALSE;
        NEW.transcript_processing_failure_count := 0;
        NEW.transcript_processing_last_error_code := NULL;
        NEW.transcript_processing_last_error := NULL;
        NEW.transcript_processing_last_failed_at := NULL;
    END IF;
    RETURN NEW;
END;
$$;
REVOKE ALL ON FUNCTION reset_transcript_processing_on_recovery() FROM PUBLIC;
CREATE TRIGGER sessions_transcript_processing_recovery
    BEFORE UPDATE OF source, transcript_path, external_id, status ON sessions
    FOR EACH ROW EXECUTE FUNCTION reset_transcript_processing_on_recovery();
