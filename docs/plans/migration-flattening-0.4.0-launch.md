# Flatten Storage Migrations for 0.4.0 Baseline

## Summary

- Make `v212` the storage baseline for `0.4.0`.
- Update `baseline_schema.sql` to the canonical final `v212` schema.
- Remove the `208`-`212` incremental migration path and the tests that only exist to validate those historical upgrades.
- Treat any database below `212` as unsupported with a fail-fast recovery message.
- Back up `~/.gobby/gobby-hub.db` before implementation; do not mutate it as part of this work.

## Implementation Changes

### Task and setup

- Create and claim a Gobby task through `gobby-tasks` MCP before editing.
- Create a timestamped backup of `~/.gobby/gobby-hub.db` before changing migration code.

### Storage baseline

- Update `src/gobby/storage/baseline_schema.sql` so it directly represents the intended `v212` end state.
- Ensure the SQL includes final structures previously supplied by `208`-`212`, including:
  - `tasks.claimed_by_session_id REFERENCES sessions(id) ON DELETE SET NULL`
  - `tasks.lifecycle_stage`
  - `expansion_runs`
  - `agent_runs.claimed_session_id`
  - `pending_interactions`
- Keep canonical repo semantics rather than freezing quirks from the current live DB dump.

### `migrations.py` cleanup

- Bump `BASELINE_VERSION` from `211` to `212`.
- Bump `_MIN_MIGRATION_VERSION` from `171` to `212`.
- Remove migration entries `208`-`212`.
- Remove helper functions only used by those migrations.
- Update module docs and comments so they describe the new `212` floor instead of the old `171` compatibility boundary.
- Leave the baseline application path, version detection, unsupported-version error, and FTS setup helpers intact.

### Runtime behavior

- `version == 0`: apply baseline, record `212`, return `1`.
- `version == 212`: no-op, return `0`.
- `version < 212`: raise `MigrationUnsupportedError` with backup and recreate guidance.

### Documentation

- Update any release or developer notes that still describe the pre-`212` migration support window.
- Document the reset path for users with older local databases.

## Test Changes

- Keep and update tests for:
  - fresh DB bootstrap to `212`
  - idempotency on rerun
  - unsupported-version failure below `212`
  - baseline presence of schema features formerly added by `208`-`212`
- Remove tests that only simulate historical upgrade paths through `208`-`212`, including partial-application recovery cases tied to those migrations.
- Update expected migration counts so fresh DB setup reflects baseline-only initialization rather than baseline plus trailing migrations.

## Acceptance Criteria

- `baseline_schema.sql` alone is sufficient to produce the intended `v212` schema for a fresh database.
- `MIGRATIONS` contains no entries at or below `212`.
- `run_migrations()` no longer upgrades databases from `171..211`.
- Existing tests pass after replacing historical-upgrade assertions with the new baseline contract.
- Error messaging for unsupported databases clearly tells operators to restore from backup or recreate the database.

## Assumptions

- `0.4.0` is allowed to break automatic upgrades for databases below `212`.
- The live database is backed up before code changes, but not normalized in place.
- The correct baseline source is the repo's canonical intended end state, not the exact DDL in the current `~/.gobby/gobby-hub.db`.
