DROP TRIGGER sessions_delete_unassigned_verification_receipts ON sessions;

DROP FUNCTION delete_unassigned_verification_receipts_for_session();

DROP TABLE verification_receipts;

ALTER TABLE tasks
DROP COLUMN validation_epoch;
