-- gobby:destructive

-- Evidence block: _pgaudit_probe
-- Read-only hub catalog check (2026-07-31): 1 row; 24,576 total bytes;
-- _pgaudit_probe_pkey idx_scan=0.
-- Post-removal token sweep across src/, crates/, and tests/: zero executable references;
-- migration 359 and its contract assertion are the only source mentions.
-- Kept adjacent: pgAudit extension/logging config and readiness/log-permission health checks.
DROP TABLE IF EXISTS _pgaudit_probe;
