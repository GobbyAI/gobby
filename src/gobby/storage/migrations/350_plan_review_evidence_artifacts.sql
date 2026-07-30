ALTER TABLE plan_review_evidence
ADD COLUMN IF NOT EXISTS vote_artifact JSONB,
ADD COLUMN IF NOT EXISTS vote_artifact_digest TEXT,
ADD COLUMN IF NOT EXISTS vote_receipt JSONB,
ADD COLUMN IF NOT EXISTS vote_receipt_digest TEXT;

ALTER TABLE plan_review_evidence
DROP CONSTRAINT IF EXISTS plan_review_evidence_vote_artifact_type,
DROP CONSTRAINT IF EXISTS plan_review_evidence_vote_artifact_pair,
DROP CONSTRAINT IF EXISTS plan_review_evidence_vote_receipt_type,
DROP CONSTRAINT IF EXISTS plan_review_evidence_vote_receipt_pair;

ALTER TABLE plan_review_evidence
ADD CONSTRAINT plan_review_evidence_vote_artifact_type
CHECK (jsonb_typeof(vote_artifact) = 'object'),
ADD CONSTRAINT plan_review_evidence_vote_artifact_pair
CHECK ((vote_artifact IS NULL) = (vote_artifact_digest IS NULL)),
ADD CONSTRAINT plan_review_evidence_vote_receipt_type
CHECK (jsonb_typeof(vote_receipt) = 'object'),
ADD CONSTRAINT plan_review_evidence_vote_receipt_pair
CHECK ((vote_receipt IS NULL) = (vote_receipt_digest IS NULL));
