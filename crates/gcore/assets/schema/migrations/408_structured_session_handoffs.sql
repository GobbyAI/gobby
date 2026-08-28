-- Replace rolling digest state with structured handoffs and normalized feedback.

ALTER TABLE sessions
    ADD COLUMN IF NOT EXISTS handoff_markdown text;

ALTER TABLE sessions
    DROP CONSTRAINT IF EXISTS sessions_summary_digest_turn_count_nonnegative,
    DROP COLUMN IF EXISTS summary_digest_turn_count,
    DROP COLUMN IF EXISTS digest_markdown,
    DROP COLUMN IF EXISTS last_turn_markdown,
    DROP COLUMN IF EXISTS last_digest_input_hash,
    DROP COLUMN IF EXISTS last_digested_pair_index;

ALTER TABLE session_summary_revisions
    DROP CONSTRAINT IF EXISTS session_summary_revisions_digest_turn_count_nonnegative,
    DROP CONSTRAINT IF EXISTS session_summary_revisions_generation_mode_valid,
    DROP COLUMN IF EXISTS source_digest_turn_count;

UPDATE session_summary_revisions
SET generation_mode = 'full'
WHERE generation_mode IN ('delta', 'digest_fallback');

ALTER TABLE session_summary_revisions
    ADD CONSTRAINT session_summary_revisions_generation_mode_valid
    CHECK (generation_mode = ANY (ARRAY['agent_authored'::text, 'full'::text, 'noop'::text]));

CREATE TABLE IF NOT EXISTS session_feedback (
    id uuid PRIMARY KEY,
    session_id uuid NOT NULL REFERENCES sessions(id) ON DELETE CASCADE DEFERRABLE,
    source text NOT NULL CHECK (btrim(source) <> ''),
    kind text NOT NULL CHECK (btrim(kind) <> ''),
    evidence text NOT NULL CHECK (btrim(evidence) <> ''),
    impact text NOT NULL CHECK (btrim(impact) <> ''),
    frequency text NOT NULL CHECK (btrim(frequency) <> ''),
    suggestion text CHECK (suggestion IS NULL OR btrim(suggestion) <> ''),
    disposition text CHECK (disposition IS NULL OR btrim(disposition) <> ''),
    reviewed boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_session_feedback_session_created
    ON session_feedback (session_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_session_feedback_unreviewed
    ON session_feedback (created_at) WHERE reviewed = false;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE session_feedback TO gobby_daemon_runtime;

WITH latest_open_claim AS (
    SELECT DISTINCT ON (claimed_by_session_id)
        claimed_by_session_id AS session_id,
        seq_num,
        title
    FROM tasks
    WHERE claimed_by_session_id IS NOT NULL
      AND closed_at IS NULL
    ORDER BY claimed_by_session_id, updated_at DESC, seq_num DESC
), automatic_titles AS (
    SELECT
        sessions.id,
        latest_open_claim.seq_num AS task_seq_num,
        latest_open_claim.title AS task_title,
        sessions.seq_num AS session_seq_num
    FROM sessions
    LEFT JOIN latest_open_claim ON latest_open_claim.session_id = sessions.id
)
UPDATE sessions
SET title = CASE
        WHEN automatic_titles.task_seq_num IS NOT NULL
            THEN '(gobby): Task #' || automatic_titles.task_seq_num::text
                || ' - ' || automatic_titles.task_title
        ELSE '(gobby): S#' || automatic_titles.session_seq_num::text
    END,
    title_source = CASE
        WHEN automatic_titles.task_seq_num IS NOT NULL THEN 'task'
        ELSE 'provisional'
    END
FROM automatic_titles
WHERE sessions.id = automatic_titles.id
  AND sessions.title_source IS DISTINCT FROM 'manual';

DELETE FROM rule_definitions
WHERE name IN (
    'auto-compact-after-task-close',
    'rearm-gobby-session-feedback-after-close',
    'review-gobby-session-feedback-before-compact',
    'review-gobby-session-feedback-on-stop',
    'reset-gobby-session-feedback-on-context-reset'
);
