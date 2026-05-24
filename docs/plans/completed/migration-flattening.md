# Migration Flattening (Strangler Fig)

## Overview

Flatten the existing `migrations.py` and `migrations_legacy.py` into a single, clean baseline schema using a **strangler fig pattern** for safe, incremental migration with rollback capability at each phase.

**Goal**: Establish a new `BASELINE_SCHEMA_V2` in `src/gobby/storage/migrations.py` that reflects the current production state (v75), with a feature flag to switch between old and new paths. Eventually delete `src/gobby/storage/migrations_legacy.py` once confident.

## Constraints

- Must backup existing DB before any code changes to ensure data safety
- Must support both new installs (create fresh DB) and existing DBs (no re-migration)
- **Rollback must be possible at any phase** by reverting config or code
- Old migration path stays intact until final removal phase
- Feature flag controls which baseline path is used

## Strangler Fig Strategy

```text
Phase 1: Add new baseline alongside old (additive, no removal)
    ↓
Phase 2: Add feature flag to select baseline path
    ↓
Phase 3: Test both paths, old remains as fallback
    ↓
Phase 4: Default to new baseline, monitor
    ↓
Phase 5: Remove old path (only after confidence period)
```

**Rollback at any phase:** Flip `use_flattened_baseline: false` in config or revert code.

## Phase 1: Schema Capture ✅

**Goal**: Capture current schema and add new baseline constants (additive only).

**Tasks:**

- [x] Create timestamped backup of PostgreSQL hub (category: manual) - #6179
- [x] Dump current schema to src/gobby/storage/schema_dump.sql (category: manual) - #6180
- [x] Review schema_dump.sql for correctness (category: manual) - #6181
- [x] Add BASELINE_SCHEMA_V2 constant with v75 schema (category: code) - #6182
- [x] Add BASELINE_VERSION_V2 = 75 constant (category: code) - #6182

**Rollback:** Delete new constants if issues found.

## Phase 2: Feature Flag ✅

**Goal**: Add config option to control which baseline path is used.

**Tasks:**

- [x] Add `use_flattened_baseline: bool` field to DaemonConfig (category: code) - #6183
- [x] Default to False (old behavior) for safety (category: code) - #6183

**Config Location:** `~/.gobby/config.yaml` or `src/gobby/config/app.py`

**Rollback:** Set `use_flattened_baseline: false` in config.

## Phase 3: Branching Logic ✅

**Goal**: Modify run_migrations() to use new baseline when flag is enabled.

**Tasks:**

- [x] Update run_migrations() to check use_flattened_baseline flag (category: code) - #6184, #6185
- [x] When True: use BASELINE_SCHEMA_V2/VERSION_V2 for new DBs (category: code) - #6185
- [x] When False: use existing BASELINE_SCHEMA/VERSION + legacy path (category: code) - #6185
- [x] Add logging to indicate which path was used (category: code) - #6185

**Logic:**

```python
if config.use_flattened_baseline:
    # New path: apply V2 baseline directly
    _apply_baseline_v2(db)
else:
    # Old path: existing logic with legacy migrations
    _apply_baseline(db)  # or run legacy if v < 60
```

**Rollback:** Flag controls path, no code changes needed.

## Phase 4: Testing & Validation ✅

**Goal**: Validate both paths work correctly.

**Tasks:**

- [x] Test new install with flag=False (old path) (category: manual) - #6186
- [x] Test new install with flag=True (new path) (category: manual) - #6187
- [x] Test existing DB upgrade with flag=False (category: manual) - verified in #6186
- [x] Test existing DB with flag=True (should be no-op) (category: manual) - verified in #6187
- [x] Compare schema output between both paths (category: manual) - #6188

**Rollback:** Set flag=False to use old path.

## Phase 5: Default to New Baseline ✅

**Goal**: Flip default to use new baseline, keep old as escape hatch.

**Tasks:**

- [x] Change default of use_flattened_baseline to True (category: code) - #6189
- [x] Update documentation to note the change (category: docs) - #6190
- [ ] Monitor for any issues in production use (category: manual) - #6191 (ongoing)

**Rollback:** Set `use_flattened_baseline: false` explicitly in config.

## Phase 6: Cleanup (Future)

**Goal**: Remove old migration path once confident (after monitoring period).

**Tasks:**

- [ ] Remove BASELINE_SCHEMA (old v60 baseline) (category: code)
- [ ] Remove BASELINE_VERSION constant (category: code)
- [ ] Remove use_flattened_baseline flag and branching logic (category: code)
- [ ] Rename BASELINE_SCHEMA_V2 to BASELINE_SCHEMA (category: code)
- [ ] Remove migrations_legacy import from run_migrations() (category: code)
- [ ] Delete src/gobby/storage/migrations_legacy.py (category: code)
- [ ] Clear MIGRATIONS list (all in baseline now) (category: code)

**Rollback:** Git revert entire cleanup commit.
