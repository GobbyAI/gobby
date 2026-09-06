-- Session summary revision retirement (#21889). The current summary remains on
-- sessions; structured handoffs and their delivery receipts are independent.
-- Ordinary transactional migration: fresh installs execute it against the
-- baseline@420 objects and existing hubs apply it at daemon startup.
-- Qualify every target to the runner's schema. RESTRICT makes an unexpected
-- dependency fail the transaction before any part of the retirement can persist.
DO $remove_session_summary_revisions$
BEGIN
    EXECUTE format(
        'ALTER TABLE %1$I.sessions '
        'DROP CONSTRAINT IF EXISTS sessions_summary_revision_fk RESTRICT',
        current_schema()
    );
    EXECUTE format(
        'ALTER TABLE %1$I.sessions '
        'DROP COLUMN IF EXISTS summary_revision_id RESTRICT',
        current_schema()
    );
    EXECUTE format(
        'DROP TABLE IF EXISTS %1$I.session_summary_revisions RESTRICT',
        current_schema()
    );
END
$remove_session_summary_revisions$;
