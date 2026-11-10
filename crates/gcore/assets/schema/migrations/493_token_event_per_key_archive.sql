-- Retention archives usage per message key, so an expired event whose key is archived is never
-- re-inserted by the audit. An unkeyed event archives under the '' key. Archive rows written
-- before this migration have no message key, so they take ''. That row cannot be split by
-- message, and the audit treats any session holding a '' row as already archived.
-- Separate statements: Postgres resolves ALTER COLUMN against the pre-statement catalog, so one
-- ALTER TABLE cannot alter a column that its own ADD COLUMN creates.
ALTER TABLE token_event_retention_totals ADD COLUMN message_key text NOT NULL DEFAULT '';
ALTER TABLE token_event_retention_totals ALTER COLUMN message_key DROP DEFAULT;

ALTER TABLE token_event_retention_totals DROP CONSTRAINT token_event_retention_totals_pkey;
ALTER TABLE token_event_retention_totals ADD PRIMARY KEY (session_id, message_key);
