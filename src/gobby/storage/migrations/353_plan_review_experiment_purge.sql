UPDATE plan_review_evidence
SET expired_at = NOW()
WHERE finalized_at IS NULL
  AND expired_at IS NULL;

ALTER TABLE plan_review_evidence
    DROP CONSTRAINT IF EXISTS plan_review_evidence_quality_ledger_type,
    DROP CONSTRAINT IF EXISTS plan_review_evidence_repair_attestations_type,
    DROP CONSTRAINT IF EXISTS plan_review_evidence_prior_round_context_type,
    DROP CONSTRAINT IF EXISTS plan_review_evidence_vote_artifact_type,
    DROP CONSTRAINT IF EXISTS plan_review_evidence_vote_artifact_pair,
    DROP CONSTRAINT IF EXISTS plan_review_evidence_vote_receipt_type,
    DROP CONSTRAINT IF EXISTS plan_review_evidence_vote_receipt_pair;

ALTER TABLE plan_review_evidence
    DROP COLUMN IF EXISTS quality_ledger,
    DROP COLUMN IF EXISTS repair_attestations,
    DROP COLUMN IF EXISTS prior_round_context,
    DROP COLUMN IF EXISTS vote_artifact,
    DROP COLUMN IF EXISTS vote_artifact_digest,
    DROP COLUMN IF EXISTS vote_receipt,
    DROP COLUMN IF EXISTS vote_receipt_digest;
