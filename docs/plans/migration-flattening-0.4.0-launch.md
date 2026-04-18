# Flatten Storage Migrations for 0.4.0 Baseline

## Status

This plan is partially implemented and the original `v212` wording is now stale.

Already landed in the repo:

- `src/gobby/storage/migrations.py` sets `BASELINE_VERSION = 214`.
- `src/gobby/storage/baseline_schema.sql` already includes the schema elements originally
  called out here, plus later additions through `v214`, including:
  - `pending_interactions` (introduced in `v202`)
  - `tasks.claimed_by_session_id REFERENCES sessions(id) ON DELETE SET NULL`
  - `tasks.lifecycle_stage`
  - `expansion_runs`
  - `agent_runs.claimed_session_id`
  - `sessions.title_source`
  - `sessions.sandbox_enabled` / `sessions.sandbox_policy_hash`
- Fresh-database tests already verify several of those baseline features.

Not yet landed:

- `_MIN_MIGRATION_VERSION` is still `171`.
- Historical migrations `172`-`214` are still present, including `208`-`214`.
- `run_migrations()` still upgrades existing databases from `171..214`.
- Migration comments and unsupported-version messaging still describe the `v171` floor.
- Historical upgrade tests still exist for `208`, `211`, and `212`.

## Current Goal

If we still want a fully flattened storage baseline for `0.4.0`, the coherent target is now
`v214`, not `v212`.

Fresh databases already bootstrap straight to `214`. The remaining work is to drop historical
upgrade support below `214` and align code, tests, and docs around that contract.

## Implementation Changes

### Task and setup

- Create and claim a Gobby task through `gobby-tasks` MCP before editing.
- Create a timestamped backup of `~/.gobby/gobby-hub.db` before changing migration code.
- Do not mutate the live database in place during implementation.

### Storage baseline

- Keep `src/gobby/storage/baseline_schema.sql` as the canonical `v214` schema.
- Treat the repo schema as canonical rather than freezing quirks from any local live DB dump.
- Preserve the current baseline features formerly added incrementally through `v202`-`v214`,
  especially:
  - `pending_interactions`
  - canonical task ownership / lifecycle fields
  - `expansion_runs`
  - agent-run claim ownership
  - session title provenance and sandbox metadata

### `migrations.py` cleanup

- Leave `BASELINE_VERSION` at `214`.
- Raise `_MIN_MIGRATION_VERSION` from `171` to `214`.
- Remove historical migration entries `172`-`214`; with a `214` floor they are dead code.
- Remove helper functions only used by those deleted migrations.
- Update module docs, comments, and log messages so they describe a `214` baseline/floor
  instead of the old `171` boundary.
- Keep the baseline application path, version detection, unsupported-version error type, and
  FTS setup helpers intact.

### Runtime behavior

- `version == 0`: apply baseline, record `214`, return `1`.
- `version == 214`: no-op, return `0`.
- `0 < version < 214`: raise `MigrationUnsupportedError` with explicit backup and recreate
  guidance.
- Future migrations above `214` continue to append to `MIGRATIONS` normally.

### Documentation

- Update release and developer notes that still describe `171` as the minimum supported
  migration version.
- Document the reset path for users with older local databases.

## Test Changes

- Keep and update tests for:
  - fresh DB bootstrap to `214`
  - idempotency on rerun
  - unsupported-version failure below `214`
  - baseline presence of schema features formerly added incrementally through `v202`-`v214`
- Remove tests that only validate historical upgrade paths or partial-application recovery for
  deleted migrations, including the current `208`, `211`, and `212` migration tests.
- Update expected migration counts so fresh DB setup reflects baseline-only initialization,
  unless new post-`214` migrations are introduced.

## Acceptance Criteria

- `baseline_schema.sql` alone is sufficient to produce the intended `v214` schema for a fresh
  database.
- `_MIN_MIGRATION_VERSION == 214`.
- `MIGRATIONS` contains no entries at or below `214`.
- `run_migrations()` no longer upgrades databases from `171..213`.
- Existing tests pass after replacing historical-upgrade assertions with the new `214`
  baseline contract.
- Error messaging for unsupported databases clearly tells operators to back up, restore, or
  recreate the database.

## Notes

- The original `212`-specific plan is obsolete because the repo advanced beyond it.
- `pending_interactions` belongs in the baseline verification set, but it was introduced in
  `v202`, not in the `208`-`212` slice.
- Removing only `208`-`212` while raising the migration floor would leave older dead
  migrations behind. The cleanup should remove the full `172`-`214` historical path together.
