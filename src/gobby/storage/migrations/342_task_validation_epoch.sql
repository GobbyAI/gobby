ALTER TABLE tasks
    ADD COLUMN validation_epoch INTEGER NOT NULL DEFAULT 0
    CHECK (validation_epoch >= 0);

ALTER TABLE tasks
    ADD CONSTRAINT tasks_require_validation_criteria
    CHECK (
        task_type = 'epic'
        OR NULLIF(BTRIM(validation_criteria), '') IS NOT NULL
    )
    NOT VALID;

ALTER TABLE verification_receipts
    ADD COLUMN validation_epoch INTEGER
    CHECK (validation_epoch IS NULL OR validation_epoch >= 0);

ALTER TABLE verification_receipts
    DROP CONSTRAINT verification_receipts_normalized_outcome_check;

UPDATE verification_receipts
SET normalized_outcome = 'pending'
WHERE normalized_outcome = 'provisional';

ALTER TABLE verification_receipts
    ADD CONSTRAINT verification_receipts_normalized_outcome_check
    CHECK (
        normalized_outcome IN ('pending', 'success', 'failure', 'unknown', 'conflicting')
    );
