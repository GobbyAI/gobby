-- The 'validation_command' vocabulary was removed with the framework matchers
-- (#18703); writers set evidence_type explicitly, so the stale default only
-- misleads. Align the column default with the live vocabulary.
ALTER TABLE verification_receipts
    ALTER COLUMN evidence_type SET DEFAULT 'shell_command';
