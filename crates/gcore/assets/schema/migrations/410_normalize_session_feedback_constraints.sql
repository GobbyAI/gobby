-- Normalize feedback check names across fresh and upgraded databases.

ALTER TABLE session_feedback
    DROP CONSTRAINT IF EXISTS session_feedback_source_nonblank,
    DROP CONSTRAINT IF EXISTS session_feedback_source_check,
    DROP CONSTRAINT IF EXISTS session_feedback_kind_nonblank,
    DROP CONSTRAINT IF EXISTS session_feedback_kind_check,
    DROP CONSTRAINT IF EXISTS session_feedback_evidence_nonblank,
    DROP CONSTRAINT IF EXISTS session_feedback_evidence_check,
    DROP CONSTRAINT IF EXISTS session_feedback_impact_nonblank,
    DROP CONSTRAINT IF EXISTS session_feedback_impact_check,
    DROP CONSTRAINT IF EXISTS session_feedback_frequency_nonblank,
    DROP CONSTRAINT IF EXISTS session_feedback_frequency_check,
    DROP CONSTRAINT IF EXISTS session_feedback_suggestion_nonblank,
    DROP CONSTRAINT IF EXISTS session_feedback_suggestion_check,
    DROP CONSTRAINT IF EXISTS session_feedback_disposition_nonblank,
    DROP CONSTRAINT IF EXISTS session_feedback_disposition_check;

ALTER TABLE session_feedback
    ADD CONSTRAINT session_feedback_source_nonblank CHECK (btrim(source) <> ''),
    ADD CONSTRAINT session_feedback_kind_nonblank CHECK (btrim(kind) <> ''),
    ADD CONSTRAINT session_feedback_evidence_nonblank CHECK (btrim(evidence) <> ''),
    ADD CONSTRAINT session_feedback_impact_nonblank CHECK (btrim(impact) <> ''),
    ADD CONSTRAINT session_feedback_frequency_nonblank CHECK (btrim(frequency) <> ''),
    ADD CONSTRAINT session_feedback_suggestion_nonblank
        CHECK (suggestion IS NULL OR btrim(suggestion) <> ''),
    ADD CONSTRAINT session_feedback_disposition_nonblank
        CHECK (disposition IS NULL OR btrim(disposition) <> '');
