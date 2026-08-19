-- Tokenized deletion lease, publication state, and durable cleanup fences
-- for hub-owned chat attachments.

ALTER TABLE chat_attachments
    ADD COLUMN claim_token uuid,
    ADD COLUMN claimed_at timestamp with time zone,
    ADD COLUMN published boolean;

UPDATE chat_attachments
   SET published = TRUE
 WHERE published IS NULL;

ALTER TABLE chat_attachments
    ALTER COLUMN published SET DEFAULT FALSE,
    ALTER COLUMN published SET NOT NULL;

CREATE INDEX idx_chat_attachments_claim
    ON chat_attachments USING btree (claim_token, claimed_at)
    WHERE claim_token IS NOT NULL;

CREATE INDEX idx_chat_attachments_unpublished
    ON chat_attachments USING btree (created_at)
    WHERE published IS FALSE;

CREATE TABLE chat_attachment_cleanup_fences (
    scope_kind text NOT NULL,
    scope_id text NOT NULL,
    token uuid,
    owner text,
    claimed_at timestamp with time zone,
    state text NOT NULL,
    CONSTRAINT chat_attachment_cleanup_fences_pkey PRIMARY KEY (scope_kind, scope_id),
    CONSTRAINT chat_attachment_cleanup_fences_scope_kind_check
        CHECK (scope_kind IN ('conversation', 'session')),
    CONSTRAINT chat_attachment_cleanup_fences_state_check
        CHECK (state IN ('idle', 'active', 'terminal'))
);

GRANT SELECT, INSERT, DELETE, UPDATE ON TABLE chat_attachment_cleanup_fences
    TO gobby_daemon_runtime;
