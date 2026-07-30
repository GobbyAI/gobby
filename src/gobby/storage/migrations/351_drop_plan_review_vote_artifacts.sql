ALTER TABLE plan_review_evidence
    DROP CONSTRAINT IF EXISTS plan_review_evidence_vote_artifact_type,
    DROP CONSTRAINT IF EXISTS plan_review_evidence_vote_artifact_pair,
    DROP CONSTRAINT IF EXISTS plan_review_evidence_vote_receipt_type,
    DROP CONSTRAINT IF EXISTS plan_review_evidence_vote_receipt_pair;

ALTER TABLE plan_review_evidence
    DROP COLUMN IF EXISTS vote_artifact,
    DROP COLUMN IF EXISTS vote_artifact_digest,
    DROP COLUMN IF EXISTS vote_receipt,
    DROP COLUMN IF EXISTS vote_receipt_digest;
