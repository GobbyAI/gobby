-- BM25 disposition verdict record (#19421, plan 5.4).
-- EXPLAIN (ANALYZE) on every live BM25 query shape proved all eight pg_search
-- indexes are exercised via ParadeDB custom scans, which bypass the standard
-- pg_stat_user_indexes.idx_scan counter. All indexes stay; no index is
-- dropped. Full evidence: docs/evidence/bm25-verification.md.
-- This slot was reserved by the allocation manifest so the migration chain
-- stays contiguous regardless of the verdict.

SELECT 1 AS bm25_disposition_all_stay;
