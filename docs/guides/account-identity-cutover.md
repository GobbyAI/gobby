# Account Identity Cutover

This runbook moves a baseline-375 laptop datastore from the predecessor receipt
to canonical users, UUID machine ownership, and user-owned browser sessions.
The maintenance campaign is `account-identity-cutover`.

Identity cutover and Hub-PC provisioning are separate maintenance events. Finish
and soak this cutover locally. Later follow
[Hub-PC Datastore Move](../../.gobby/plans/hub-pc-datastore-move.md) unchanged
against the transformed datastore and matching binaries.

## Safety contract

- Use the reviewed task worktree and record its exact commit SHA.
- Stop every Gobby daemon before live mutation and prevent automatic restart.
- Keep a verified fresh PostgreSQL/globals backup until the soak period ends.
- Rehearsal evidence and staged binaries are valid only for their recorded SHA.
- Any commit change requires artifact regeneration and a complete new rehearsal.
- A reviewed-SHA mismatch at the live gate aborts the window.
- The scratch stack must use a dedicated `gobby_test` PostgreSQL database,
  isolated Qdrant and FalkorDB instances, an isolated `GOBBY_HOME`, and unused
  daemon ports. It must have no route to the live daemon or production stores.

The campaign refuses an unexpected receipt, any existing `users` table, or any
non-NULL machine owner. Current predecessor `upsert_seen` callers supply no
owner, so the expected live pre-state is all NULL. Preserve that query result as
evidence; the refusal remains a tripwire for unexpected live state.

## Commit-bound release artifacts

From the reviewed worktree, regenerate in this order:

1. Baseline checksum and `crates/gcore/src/schema/assets.rs` literal.
2. Catalog manifest from the protected test PostgreSQL instance.
3. Release `gdaemon` and `gcode` binaries.
4. `src/gobby/storage/schema_expected_identity.json` from release `gdaemon`.
5. Rust contract literals and exact contract tests.

Representative commands:

```bash
shasum -a 256 crates/gcore/assets/schema/baseline.sql
UPDATE_GCORE_SCHEMA_MANIFEST=1 \
GOBBY_SCHEMA_TEST_DATABASE_URL="$DATABASE_URL" \
cargo test -p gobby-core --features postgres \
  --test catalog_manifest_freshness \
  catalog_manifest_is_fresh_for_embedded_assets -- --exact
cargo build --release -p gobby-daemon -p gobby-code
uv run python scripts/generate_schema_expected_identity.py \
  --gdaemon target/release/gdaemon
cargo test -p gobby-core --features postgres --test schema_contract \
  embedded_assets_publish_a_complete_schema_identity -- --exact
cargo test -p gobby-daemon --test cli_contract \
  version_json_reports_exact_schema_identity_contract -- --exact
```

Set `DATABASE_URL` only to the protected test DSN. Stage immutable copies and
record their hashes:

```bash
install -d -m 0700 "$STAGE_DIR"
install -m 0755 target/release/gdaemon "$STAGE_DIR/gdaemon"
install -m 0755 target/release/gcode "$STAGE_DIR/gcode"
git rev-parse HEAD
shasum -a 256 crates/gcore/assets/schema/baseline.sql \
  "$STAGE_DIR/gdaemon" "$STAGE_DIR/gcode"
"$STAGE_DIR/gdaemon" schema version --json
```

Record reviewed SHA, baseline checksum, expected identity JSON, both binary
hashes, build host, and UTC timestamp together. A later source commit
invalidates the record even when a reviewer expects no binary change.

## Scratch rehearsal

1. Take a verified hub backup with `gobby hub-backup --json`.
2. Provision an isolated full stack. Confirm the PostgreSQL target reports
   database `gobby_test`; confirm all configured ports and store URLs differ
   from production.
3. Point a temporary `GOBBY_HOME/bootstrap.yaml` at the scratch services.
4. Restore the verified backup into the explicit scratch DSN:

   ```bash
   GOBBY_HOME="$SCRATCH_GOBBY_HOME" uv run gobby hub-backup restore \
     --database-url "$SCRATCH_DSN" --clean --yes "$BACKUP_ROOT"
   ```

5. Record pre-cutover schema identity, table counts, receipt, users-table
   absence, and all-NULL ownership. Run the staged campaign from the reviewed
   source and isolated home:

   ```bash
   GOBBY_TEST_PROTECT=1 \
   GOBBY_HOME="$SCRATCH_GOBBY_HOME" \
   GOBBY_NATIVE_BIN_DIR="$STAGE_DIR" \
   uv run gobby hub-maintenance run account-identity-cutover
   ```

6. Supply rehearsal-only name, normalized email, and password at the prompts.
   The campaign gathers and hashes identity before opening its mutation
   transaction. The transaction creates the user, assigns every machine,
   invalidates browser sessions, applies constraints, checks invariants, and
   writes the new receipt last.
7. Capture post-cutover schema identity, sole-user identity, machine ownership,
   zero auth sessions, and exact non-auth row-count equality.
8. Exercise `hub-maintenance resume` against the committed scratch result and
   confirm it verifies without prompting or repeating mutation.

Required SQL evidence:

```sql
SELECT version, filename, checksum FROM schema_migrations ORDER BY version;
SELECT id,
       '[redacted]' AS name,
       '[redacted]' AS email,
       created_at,
       updated_at
FROM users
ORDER BY id;
SELECT owner_user_id, count(*) FROM machines GROUP BY owner_user_id;
SELECT count(*) AS auth_sessions FROM auth_sessions;
SELECT count(*) FILTER (WHERE owner_user_id IS NULL) AS unowned_machines
FROM machines;
```

Do not copy `users.name` or `users.email` into the evidence bundle. The redacted
query proves sole-user identity without creating a second plaintext copy of
account PII. Restrict the bundle, keep it encrypted at rest, and delete it after
the soak period.

Capture exact counts for every table before and after. Differences are allowed
only for `users`, `auth_sessions`, `maintenance_epochs`, and
`destructive_batches`; every other table count must be identical. Preserve the
backup manifest, command transcript, SQL output, schema identity, commit record,
and staged binaries as one evidence bundle.

## Live single-machine cutover

1. Stop every active daemon and disable automatic restart.
2. Take and verify a fresh PostgreSQL/globals backup.
3. Record the old receipt, table counts, all-NULL ownership, source revision,
   and installed binary versions.
4. Merge the reviewed implementation into `0.5.0`.
5. Compare merged source SHA, reviewed SHA, generated artifact identities, and
   staged binary hashes with the rehearsal bundle. Abort on any mismatch.
6. Run the campaign with the staged binaries:

   ```bash
   GOBBY_NATIVE_BIN_DIR="$STAGE_DIR" \
   uv run gobby hub-maintenance run account-identity-cutover
   ```

   Do not set `GOBBY_TEST_PROTECT=1` during the live window. That test guard
   makes daemon shutdown a no-op. The campaign deliberately leaves the daemon
   stopped after committing and verifying this cutover so installed predecessor
   binaries cannot reopen the transformed datastore.

7. After verified release of the maintenance fence, confirm final schema
   identity and exact non-auth counts.
8. Install the staged `gdaemon` and `gcode` into the normal binary directory.
   Recheck their hashes and embedded schema identity.
9. Restart the local daemon. Perform a fresh email/password login and smoke
   tasks, memories, wiki, code index, and session creation/read paths.
10. Soak locally before scheduling the independent Hub-PC move.

Do not run `gobby install` or another datastore-opening command with the new
binaries installed before step 6. If post-commit verification fails because a
predecessor binary was resolved, install the staged binaries and run
`hub-maintenance resume`; the applied batch re-verifies without collecting
identity again.

## Failure and rollback

A mutation-transaction failure leaves the predecessor datastore unchanged and
keeps the maintenance fence active. Use `gobby hub-maintenance status --json`
to preserve state for diagnosis; use `resume` only after correcting a verified
prompt-free retry condition.

For post-commit verification or smoke failure:

1. Stop every Gobby process.
2. Restore the verified pre-cutover backup into a clean local datastore.
3. Restore the recorded predecessor source and binaries.
4. Verify the old receipt and every recorded row count.
5. Restart only after rollback verification passes.

Keep both backup and rehearsal evidence through the soak period. ConfigStore
4.3 identity dependency tracking is covered by task #19982.
