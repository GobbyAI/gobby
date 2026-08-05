# Pre-flatten divergence ledger

- Migration 346 is historical and unattested because pre-354 rows intentionally
  carry no filename/checksum. Migration 355 reconciles its reused-slot lineage.
- Migration 356 reconciles the recorded live-schema drift before this snapshot.
- `gobby_install_ownership` is installer-owned and accepted outside baseline
  seed authority.
- `gwiki_*` tables are gcore-owned standalone-adoption objects and are excluded
  from Gobby baseline DDL comparison.

The pinned normalized DDL and seed manifest were produced only after the live
comparison reported zero unexplained divergences.
