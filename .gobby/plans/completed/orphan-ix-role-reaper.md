Plan artifact: `.gobby/plans/orphan-ix-role-reaper.md`

# Extend orphan-role reaper to sweep interactive roles

> **Plan ID:** orphan-ix-role-reaper
> **Root task:** #20407 (implement as this leaf; do not expand)

## Overview
`kind: framing`

Leftover `gobby_ix_*` roles with no `principal_bindings` row make the next `issue_or_reuse_interactive_principal` derive the same name and fail with `42710` forever. The two orphan loops (`reconcile_daemon` at baseline.sql:5608 and `drain_ephemeral_principals` at :5891) still match only `^gobby_agent_[0-9a-f]{32}_[1-9][0-9]*$`. Ship the next unused schema hop (389 if catalog head is still 388) that expands those regexes, drops the derived interactive name at issue/rotate time when the role exists with no binding, and proves the 42710 path is gone.

## Constraints
`kind: framing`

Decision Record:

1. Lightweight plan. Implement on `#20407`. No expansion epic.
2. Reaper **and** issue-time drop. Next `issue_interactive` must succeed without a daemon restart.
3. Reaper name match is one inlined regex with alternation branches:
   - existing `gobby_agent_[0-9a-f]{32}`
   - hash-format `gobby_ix_[0-9a-f]{16}`
   - slug-format `gobby_ix_[A-Za-z0-9]{1,8}_[0-9a-f]{8}_[0-9a-f]{8}` (pre-386)
   - `gobby_mnt_[0-9a-f]{32}` — folded after the coverage check below
   Generation suffix stays `_[1-9][0-9]*`. Must **not** match `gobby_ix_test`.
4. Loop predicates stay as they are. `reconcile_daemon` still requires `NOT EXISTS` on `principal_bindings`. `drain_ephemeral_principals` still drops every regex match after it revokes unrevoked bindings.
5. No new SQL helper function. Inline the regex and the drop sequence (avoids another `GRANT EXECUTE` footgun like #20406 / migration 388).
6. Hop only. Do not flatten `baseline.sql`. Do not rewrite parent/predecessor/worktree baseline fixtures.
7. Do not delete `principal_bindings` rows or `interactive_credential_material` rows. Revoked bindings are the audit trail and the `MAX(credential_generation)+1` counter. Material-row lifetime is `#20408`.
8. Do not change hub-backup role inventory filters.
9. `rotate_interactive_principal` gets the same drop-before-create as issue (same `CREATE ROLE` collision). Do not change `issue_maintenance_principal`: `gobby_mnt_*` embeds a per-execution UUID, so an orphan cannot lock out a new execution. Mnt leftovers are reaper hygiene only.

`gobby_mnt_*` coverage check (settled, not deferred):

- Name: `'gobby_mnt_' || replace(p_execution_id::TEXT, '-', '') || '_1'` in `385_issue_maintenance_principal.sql`.
- `revoke_principal` drops the role only when it is called and a binding still names it.
- Neither reaper regex matches `gobby_mnt_`. Hub-backup filters only `gobby_agent_*`. Nothing else sweeps leftover mnt roles. Fold the branch in.

Hop number: committed catalog head is 388 (`388_grant_interactive_role_name.sql`). `.gobby/plans/hub-owned-files-home.md` also reserves `389_chat_attachments_deletion_lease.sql` but that file is not in the tree. At implementation start, take `max(existing migration versions)+1`. Filename `NNN_sweep_interactive_orphan_roles.sql`.

Copy issue/rotate bodies from **387**, not from `baseline.sql` (baseline still has the pre-386 slug issuer). Copy reaper bodies from `baseline.sql` (no later hop replaced them).

Identity pin recipe (same as #20406): register the hop in `crates/gcore/src/schema/assets.rs` `MIGRATIONS`; bump `src/gobby/storage/schema_expected_identity.json` (`latest_version` / `latest_checksum` = sha256 of the new file / `assets_root_hash`); update `GOLDEN_LATEST_CHECKSUM`, `GOLDEN_ASSETS_ROOT_HASH`, and `latest_version: 388` in `crates/gcore/src/grant/bundle.rs` `expected_schema_identity`; pin `crates/gcore/tests/schema_contract.rs`, `crates/gcore/src/grant/tests.rs` `expected_schema_identity_tracks_catalog_head`, `crates/gdaemon/tests/cli_contract.rs`; regenerate `tests/runtime_grants/golden/*.json` (docstring on `test_golden_vectors.py`: update `schema_identity`, recompute `payload_checksum`, re-sign with `GOLDEN_SECRET`, write `model_dump_canonical()` bytes exactly). `scripts/generate_schema_expected_identity.py` can emit the identity JSON from a rebuilt `gdaemon`. Load the `rust` skill before crate edits.

Non-goals: baseline flatten, binding/material purge, hub-backup filters, `ALTER ROLE` reuse instead of drop+create, new helper functions, sweeping non-matching names such as `gobby_ix_test`.

## P1: Schema hop and proof
`kind: framing`

**Goal**: Live hubs reap hash-format, slug-format, and mnt orphans; a leftover hash-format role no longer `42710`s issue or rotate.

### 1.1 Ship the orphan-sweep hop and pin identity [category: code]
`kind: deliverable`

Targets:
- `crates/gcore/assets/schema/migrations/389_sweep_interactive_orphan_roles.sql`
- `crates/gcore/src/schema/assets.rs::*` — scope-reason: `MIGRATIONS` is an unindexed const table; append the new hop and its checksum
- `src/gobby/storage/schema_expected_identity.json`
- `crates/gcore/src/grant/bundle.rs::expected_schema_identity`
- `crates/gcore/tests/schema_contract.rs::embedded_assets_publish_a_complete_schema_identity`
- `crates/gcore/src/grant/tests.rs::expected_schema_identity_tracks_catalog_head`
- `crates/gdaemon/tests/cli_contract.rs::version_json_reports_exact_schema_identity_contract`
- `tests/runtime_grants/golden/brokered_datastores.json`
- `tests/runtime_grants/golden/direct_datastores.json`
- `tests/runtime_grants/golden/old_client_new_grant.json`
- `tests/runtime_grants/golden/unavailable_datastores.json`
- `tests/storage/test_managed_credentials.py::test_hash_format_ix_orphan_does_not_42710_on_issue`
- `tests/storage/test_managed_credentials.py::test_reconcile_reaps_slug_and_mnt_orphans`
- `tests/storage/test_managed_credentials.py::test_reconcile_spares_bound_ix_and_unmatched_names`
- `tests/storage/test_managed_credentials.py::test_drain_reaps_hash_format_ix_orphan`

TDD: write the four tests first against an isolated hub (`authorization_fixture`). They fail on catalog 388. Then ship the hop and pins until they pass.

**Regex** (identical literal in both reaper `WHERE` clauses):

```sql
rolname ~ '^(gobby_agent_[0-9a-f]{32}|gobby_ix_([0-9a-f]{16}|[A-Za-z0-9]{1,8}_[0-9a-f]{8}_[0-9a-f]{8})|gobby_mnt_[0-9a-f]{32})_[1-9][0-9]*$'
```

Keep each loop's existing extra predicates and drop sequence. `reconcile_daemon` already does `NOT EXISTS (SELECT 1 FROM principal_bindings …)` plus `orphan_revocation_retries` when sessions remain. `drain_ephemeral_principals` already `REVOKE`s `gobby_gcode_capability` and has no binding check.

**Issue-time / rotate-time drop** — insert immediately before `CREATE ROLE` in the 387 bodies of `issue_or_reuse_interactive_principal` and `rotate_interactive_principal`:

```sql
IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = derived_role_name)
   AND NOT EXISTS (
       SELECT 1 FROM principal_bindings
       WHERE role_name = derived_role_name
   )
THEN
    EXECUTE format('ALTER ROLE %I NOLOGIN', derived_role_name);
    EXECUTE format(
        'REVOKE %I FROM %I',
        'gobby_gcode_capability',
        derived_role_name
    );
    PERFORM pg_terminate_backend(pid, 5000)
    FROM pg_stat_activity
    WHERE usename = derived_role_name::TEXT
      AND pid <> pg_backend_pid();
    IF EXISTS (
        SELECT 1 FROM pg_stat_activity
        WHERE usename = derived_role_name::TEXT
    ) THEN
        RAISE EXCEPTION 'managed principal still has active database sessions'
            USING ERRCODE = '55006';
    END IF;
    EXECUTE format('DROP ROLE %I', derived_role_name);
END IF;
```

`CREATE OR REPLACE` preserves owner and `EXECUTE` grants. Do not add a new function. Do not `ALTER OWNER` / re-`GRANT` unless replace somehow drops them.

**Tests** (use `_manager`, `_secret_store`, `authorization_fixture`; admin `CREATE ROLE` / `to_regrole` / `DROP ROLE` in `finally`):

1. `test_hash_format_ix_orphan_does_not_42710_on_issue` — `SELECT gobby_agent_auth.interactive_role_name(token, machine_id, project_id, 1)`, `CREATE ROLE` that name `LOGIN`, no binding row, `issue_interactive(...)`. Assert success, `reused is False`, no `duplicate_object` / `42710`. Role is usable (`SELECT 1` on the issued DSN).
2. `test_reconcile_reaps_slug_and_mnt_orphans` — plant `gobby_ix_tokentok_deadbeef_cafed00d_1` and `gobby_mnt_{uuid4().hex}_1` with no bindings. `manager.reconcile()`. Both `to_regrole` are NULL.
3. `test_reconcile_spares_bound_ix_and_unmatched_names` — `issue_interactive` then plant `gobby_ix_test`. `reconcile()`. Issued role and `gobby_ix_test` still exist.
4. `test_drain_reaps_hash_format_ix_orphan` — plant a hash-format name via `interactive_role_name` with no binding. `SELECT gobby_agent_auth.drain_ephemeral_principals()`. `to_regrole` is NULL. Do not call drain while a live bound interactive principal exists (drain revokes every unrevoked binding).

**Identity pins** after the SQL file exists: sha256 the hop, append `MIGRATIONS`, bump identity JSON and GOLDEN consts, update the three contract assertions, regenerate the four golden grant files.

**Validation** (do not run the full suite). Rebuild and reinstall is required, not optional: the daemon applies schema from the installed `gdaemon`, and gcode/ghook/gwiki embed the same gcore identity. A committed hop is not live until the binaries are replaced.

Never `cp` in place over `~/.gobby/bin/{gcode,gdaemon,ghook,gwiki}`. macOS caches the code signature per inode and SIGKILLs (exit 137, no stderr) any exec of an overwritten binary.

```bash
# 1. Rebuild the four installed workspace binaries.
cargo build --release -p gobby-daemon -p gobby-code -p gobby-hooks -p gobby-wiki

# 2. Reinstall via a new inode (cp to a sibling, then mv -f over the old name).
install_gobby_bin() {
  local name="$1"
  cp "target/release/${name}" "${HOME}/.gobby/bin/.${name}.new"
  mv -f "${HOME}/.gobby/bin/.${name}.new" "${HOME}/.gobby/bin/${name}"
}
install_gobby_bin gdaemon
install_gobby_bin gcode
install_gobby_bin ghook
install_gobby_bin gwiki

# 3. Focused tests (isolated hub; prefix required).
GOBBY_TEST_PROTECT=1 uv run pytest tests/storage/test_managed_credentials.py \
  -k "hash_format_ix_orphan or reconcile_reaps_slug or reconcile_spares_bound or drain_reaps_hash" -v
GOBBY_TEST_PROTECT=1 uv run pytest tests/runtime_grants/test_golden_vectors.py -v
cargo test -p gobby-core --features postgres embedded_assets_publish_a_complete_schema_identity
cargo test -p gobby-core expected_schema_identity_tracks_catalog_head
cargo test -p gobby-daemon version_json_reports_exact_schema_identity_contract
```

**Acceptance:**

- 1.1.1 - Next unused hop replaces both reaper loops with the combined agent/ix-hash/ix-slug/mnt regex. file: `crates/gcore/assets/schema/migrations/389_sweep_interactive_orphan_roles.sql`.
- 1.1.2 - `issue_or_reuse_interactive_principal` and `rotate_interactive_principal` drop a derived hash-format role that has no `principal_bindings` row before `CREATE ROLE`. file: `crates/gcore/assets/schema/migrations/389_sweep_interactive_orphan_roles.sql`.
- 1.1.3 - A planted hash-format `gobby_ix_*` role with no binding no longer makes `issue_interactive` fail with `42710`. test: `tests/storage/test_managed_credentials.py::test_hash_format_ix_orphan_does_not_42710_on_issue`.
- 1.1.4 - `reconcile()` drops a planted slug-format `gobby_ix_*` orphan and a planted `gobby_mnt_*` orphan. test: `tests/storage/test_managed_credentials.py::test_reconcile_reaps_slug_and_mnt_orphans`.
- 1.1.5 - `reconcile()` leaves a bound interactive role and `gobby_ix_test` in place. test: `tests/storage/test_managed_credentials.py::test_reconcile_spares_bound_ix_and_unmatched_names`.
- 1.1.6 - `drain_ephemeral_principals()` drops a planted hash-format orphan. test: `tests/storage/test_managed_credentials.py::test_drain_reaps_hash_format_ix_orphan`.
- 1.1.7 - Schema identity pins move with the hop (`latest_version`, checksums, root hash, golden vectors). file: `src/gobby/storage/schema_expected_identity.json`.
- 1.1.8 - Release `gdaemon`, `gcode`, `ghook`, and `gwiki` are rebuilt and reinstalled into `~/.gobby/bin` via a new inode before the task is closed. behavior: "rebuild and reinstall workspace binaries" in this plan.

## Verification
`kind: verification`

Plan Verification:

- No filler "write tests for X" tasks. The four tests are the TDD surface of 1.1.
- Single deliverable, no dependency edges.
- Category `code`, backend.
- Phase heading is `P1`.
- 1.1 is self-contained: regex, drop SQL, hop/identity recipe, test names, commands, and new-inode binary reinstall.
- `gobby_mnt_*` inclusion is a settled repo fact, not an implementer choice.
- Binding/material rows stay; `#20408` is untouched.

Ready for review.

## V1 Plan Changelog
`kind: verification`

**Draft** — Lightweight elicitation. Depth: Lightweight. Recovery: reaper + issue-time drop. Sweep both `gobby_ix_` formats and `gobby_mnt_*` (nothing else reaps mnt leftovers). No binding/material purge, no baseline flatten, no hub-backup filter change. Implement on #20407. Validation requires `cargo build --release` of gdaemon/gcode/ghook/gwiki and new-inode reinstall into `~/.gobby/bin`.
