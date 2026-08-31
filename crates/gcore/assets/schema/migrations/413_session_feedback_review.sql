-- Session-feedback review: enum vocabularies, review runs, review provenance.

-- 1) Gated-'other' label, added before remap so unmapped kinds can preserve
--    their original free-text value.
ALTER TABLE session_feedback ADD COLUMN kind_other_label text;

-- 2) Remap legacy free-text values into the enum vocabularies.
UPDATE session_feedback SET kind = 'bug'
    WHERE kind IN ('tool-defect', 'outage-with-no-in-band-recovery');
UPDATE session_feedback SET kind = 'useful'
    WHERE kind = 'useful-behavior';
UPDATE session_feedback SET kind = 'friction'
    WHERE kind IN (
        'workflow-friction', 'close-gate-friction', 'close-gate-latency',
        'process-friction', 'tdd-gate', 'tdd_evidence', 'diagnostic-legibility',
        'gcode-overlay-index', 'instruction-conflict'
    );
UPDATE session_feedback SET kind_other_label = kind, kind = 'other'
    WHERE kind NOT IN (
        'friction', 'bug', 'noise', 'surprise', 'missing-affordance', 'useful', 'other'
    );
UPDATE session_feedback SET kind_other_label = 'unspecified'
    WHERE kind = 'other' AND kind_other_label IS NULL;

UPDATE session_feedback SET frequency = 'always' WHERE frequency ILIKE 'every%';
UPDATE session_feedback SET frequency = 'repeated'
    WHERE frequency ILIKE 'repeat%' OR frequency ILIKE 'twice%' OR frequency ILIKE '%times%';
UPDATE session_feedback SET frequency = 'once'
    WHERE frequency ILIKE 'once%' OR frequency ILIKE 'this-session%';
UPDATE session_feedback SET frequency = 'repeated'
    WHERE frequency NOT IN ('once', 'repeated', 'always');

UPDATE session_feedback SET disposition = 'worked-around'
    WHERE disposition ILIKE '%worked around%' OR disposition = 'workaround';
UPDATE session_feedback SET disposition = 'fixed'
    WHERE disposition ILIKE 'fixed%' OR disposition ILIKE 'resolved%'
        OR disposition = 'implemented';
UPDATE session_feedback SET disposition = 'filed-task'
    WHERE disposition ILIKE 'filed%' OR disposition = 'file';
UPDATE session_feedback SET disposition = 'escalated'
    WHERE disposition ILIKE '%surfaced to the user%' OR disposition ILIKE '%escalat%';
UPDATE session_feedback SET disposition = 'noted'
    WHERE disposition IS NOT NULL
        AND disposition NOT IN ('worked-around', 'filed-task', 'fixed', 'escalated', 'noted');

-- 3) Vocabulary constraints.
ALTER TABLE session_feedback
    ADD CONSTRAINT session_feedback_kind_vocab CHECK (
        kind IN ('friction', 'bug', 'noise', 'surprise', 'missing-affordance', 'useful', 'other')
    ),
    ADD CONSTRAINT session_feedback_frequency_vocab CHECK (
        frequency IN ('once', 'repeated', 'always')
    ),
    ADD CONSTRAINT session_feedback_disposition_vocab CHECK (
        disposition IS NULL
        OR disposition IN ('worked-around', 'filed-task', 'fixed', 'escalated', 'noted')
    ),
    ADD CONSTRAINT session_feedback_kind_other_label_pairing CHECK (
        (kind = 'other') = (kind_other_label IS NOT NULL)
    ),
    ADD CONSTRAINT session_feedback_kind_other_label_nonblank CHECK (
        kind_other_label IS NULL OR btrim(kind_other_label) <> ''
    );

-- 4) Review runs journal for the post-hoc feedback consumer.
CREATE TABLE feedback_review_runs (
    id uuid NOT NULL,
    status text NOT NULL,
    dry_run boolean NOT NULL DEFAULT false,
    window_start timestamp with time zone,
    window_end timestamp with time zone,
    rows_considered integer NOT NULL DEFAULT 0,
    findings jsonb,
    actions jsonb,
    digest_md text,
    error text,
    created_at timestamp with time zone NOT NULL DEFAULT now(),
    completed_at timestamp with time zone,
    CONSTRAINT feedback_review_runs_status_vocab CHECK (
        status IN ('running', 'completed', 'failed', 'interrupted')
    )
);
ALTER TABLE ONLY feedback_review_runs ADD CONSTRAINT feedback_review_runs_pkey PRIMARY KEY (id);
CREATE INDEX idx_feedback_review_runs_created ON feedback_review_runs (created_at DESC);
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE feedback_review_runs TO gobby_daemon_runtime;

-- 5) Review provenance on consumed feedback rows.
ALTER TABLE session_feedback ADD COLUMN review_run_id uuid;
ALTER TABLE ONLY session_feedback ADD CONSTRAINT session_feedback_review_run_id_fkey
    FOREIGN KEY (review_run_id) REFERENCES feedback_review_runs(id) ON DELETE SET NULL;
CREATE INDEX idx_session_feedback_review_run ON session_feedback (review_run_id)
    WHERE review_run_id IS NOT NULL;
