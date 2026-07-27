ALTER TABLE verification_receipts
DROP CONSTRAINT verification_receipts_attribution_source_check;

ALTER TABLE verification_receipts
ADD CONSTRAINT verification_receipts_attribution_source_check
CHECK (
    attribution_source IN (
        'active_task',
        'sole_claim',
        'explicit_task',
        'worktree_task',
        'manual_assignment',
        'unassigned'
    )
);
