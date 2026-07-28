ALTER TABLE plan_review_evidence
ADD COLUMN IF NOT EXISTS quality_ledger JSONB,
ADD COLUMN IF NOT EXISTS repair_attestations JSONB,
ADD COLUMN IF NOT EXISTS prior_round_context JSONB;

ALTER TABLE plan_review_evidence
DROP CONSTRAINT IF EXISTS plan_review_evidence_quality_ledger_type,
DROP CONSTRAINT IF EXISTS plan_review_evidence_repair_attestations_type,
DROP CONSTRAINT IF EXISTS plan_review_evidence_prior_round_context_type;

ALTER TABLE plan_review_evidence
ADD CONSTRAINT plan_review_evidence_quality_ledger_type
CHECK (jsonb_typeof(quality_ledger) = 'array'),
ADD CONSTRAINT plan_review_evidence_repair_attestations_type
CHECK (jsonb_typeof(repair_attestations) = 'array'),
ADD CONSTRAINT plan_review_evidence_prior_round_context_type
CHECK (jsonb_typeof(prior_round_context) = 'object');
