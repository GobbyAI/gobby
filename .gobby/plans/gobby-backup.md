# gobby-backup: Rust Hub-Backup Runner With OS Scheduling

Plan artifact: `.gobby/plans/gobby-backup.md`

**Plan ID:** gobby-backup

## Overview
`kind: framing`

Task #20997 (Plan gobby-backup: Rust external hub-backup runner with
scheduling) under the epic #22949 (Lane 7 - Planning/research). This plan
replaces Draft 1
(`414a2d6c9d`). Josh's Draft 1 Decision Record stands except where a later
ruling superseded it; the V1 Plan Changelog lists each superseded item.

The plan builds `gbackup`, a Rust binary that backs up, verifies and restores
the local hub's datastores. It replaces the Python `gobby hub-backup`
implementation:
- A nightly OS timer runs a live backup while the daemon and the managed
  datastores stay up (P2, P3).
- A weekly OS timer restore-verifies the newest backup in disposable
  containers (P4).
- `gbackup restore` restores PostgreSQL, Qdrant, FalkorDB and files_home from
  a fully verified backup (P5).
- An operator-only cold mode adds the managed Docker volume tars (P6).
- launchd and systemd timers own the schedule (P7).
- `gobby hub-backup` becomes a passthrough shim, and the Python store,
  verify and manifest modules are deleted (P8).

Ownership:
- The new `gobby-backup` crate implements P1 to P6, except 1.4.
- The Python CLI implements 1.4, P7 and 8.1.
- 8.2 is documentation.
- The Orchestrator routes the leaves to developer seats.

Out of scope:
- Daemon-hosted backup scheduling, or creating a Gobby task when a backup
  fails.
- The maintenance-epoch surface. #23557 (Remove executor-less gobby
  hub-maintenance run campaigns and the epoch surface) removes it on the
  Python side.
- Hub table retention, and changes to the memory-dream schedule.
- Publishing `gbackup` as release assets and in the Homebrew formula
  (deferral D1).

## Decision Record
`kind: framing`

Sources: Josh's Draft 1 Decision Record (August 2026); Josh's rulings D1 to
D3 (2026-10-05, relayed by the Orchestrator gobby#14972); and the
Orchestrator's rulings A1 to A8 (2026-10-05).

1. **Scheduler owner.** The OS scheduler owns the cadence: launchd on macOS,
   and systemd user timers on Linux and WSL 2. The daemon never runs or
   schedules a backup. `gobby service install` installs the timers, and
   `gobby service uninstall` removes them (7.1, 7.2). There is no Windows
   Task Scheduler unit, because native Windows cannot own a hub (A7).
2. **Naming and install path.** The crate `gobby-backup` lives at
   `crates/gbackup`. It has binary `gbackup` and library `gobby_backup`.
   `gobby install` builds it from the workspace and promotes it to
   `~/.gobby/bin/gbackup` the way it installs `gclient`. It is outside the
   stamped `gcode`/`gdaemon`/`ghook` set, and it is unpublished (A2).
3. **Cadence.** Daily at 07:00 local, the timer runs
   `gbackup backup --scheduled`. Weekly on Sunday at 07:30 local, it runs
   `gbackup verify --scheduled`. Both sleep a jitter of 0 to 15 whole
   minutes derived from the machine ID before taking the run lock. The daily
   run follows the memory dream (`0 2 * * *` local).
4. **Lifecycle.** A live backup never stops the daemon or a datastore.
   `gbackup backup --cold` is operator-only. It refuses while the daemon
   runs, stops and restarts the three managed containers around the volume
   tars, and leaves the daemon for the operator to start. gbackup never
   stops or starts the daemon. There is no `--epoch` (D1; #23557 (Remove
   executor-less gobby hub-maintenance run campaigns and the epoch surface)
   removes the Python side).
5. **Manifest v3 parity.** gbackup owns the v3 manifest format, because
   #22846 (Retire the destructive-migration directive path) deleted the
   gcore gate and its fixture (A1). It keeps the Python
   format ID `gobby-hub-backup-manifest`, version 3, `manifest.json`, the
   JSON schema, the five store keys, the archive methods and the artifact
   paths. Specifics:
   - `epoch_id` is always null.
   - `gobby_version` is `gbackup <crate version>`.
   - A live backup records `volumes` with `details.skipped = true` and
     `details.reason = "live-mode"`. Both of its verification states are
     false, with method `skipped-live-mode`.
6. **Retention.** After each successful live backup, keep the 7 newest
   integrity-ok live backups under `~/.gobby/backups/hub/` and delete older
   live backups.
   - Integrity-ok means a parseable v3 live manifest whose four live stores
     record `archive_verified`, and whose artifacts all exist with their
     recorded sizes. Retention never re-hashes; verify and restore do.
   - Staging from a failed or interrupted run is always removed.
   - Cold backups, Python-written backups (their `volumes` is not skipped)
     and directories outside the default root are never pruned.
7. **Locking.** Every command takes the exclusive flock
   `~/.gobby/backups/hub/gbackup.lock`.
   - On a held lock, `backup --scheduled` records `skipped_locked` and exits
     0.
   - `verify --scheduled` waits for the lock, because the Sunday backup can
     still hold it at 07:30. The wait ends at the capture budget (1.2),
     and a timeout records `error`. Every blocking lock (`gbackup.lock`,
     `last-run.lock` and `.machine_id.lock`) has that deadline, and every
     external step has a timeout, PostgreSQL statements and lock waits
     included (1.3).
   - Interactive commands refuse on a held lock and exit 1, naming the
     holder.
   - Restore and cold also take the daemon's maintenance claim, which fails
     while the daemon runs. Cold takes `~/.gobby/managed-services.lock`
     around the container stop and start.
8. **Failure notification.** `~/.gobby/backups/hub/last-run.json` (mode
   0600) holds one record per scheduled mode, `backup` and `verify`. Each
   run replaces only its own record, so a verify never hides a failed
   nightly. Timer output appends to `~/.gobby/logs/gbackup.log` and
   `~/.gobby/logs/gbackup-verify.log`. No Gobby task is created, and nothing
   notifies the daemon. Draft 1's `gbackup status` is dropped, because the
   record file is the surface.
9. **Restore parity.** The command is
   `gbackup restore DIR --database-url URL [--clean] [--yes]`.
   - `--database-url` is required (A4).
   - Restore refuses while the daemon runs.
   - Without `--yes` it asks on a terminal, and it refuses when stdin is not
     a terminal.
   - It re-hashes every artifact. Every restored store (`postgres`,
     `qdrant`, `falkordb`, `files`) must record both `archive_verified` and
     `restore_verified` (D3 (a)). Operators run `gbackup verify DIR` first
     on a nightly that has not been verified yet.
   - It restores files_home, PostgreSQL globals, the PostgreSQL database,
     the Qdrant collections and the FalkorDB dump, then drains the restored
     ephemeral principals.
   - Volume tars are never restored.
   - Restore releases no maintenance epoch (D1). A manifest whose
     `epoch_id` is not null is refused before any write (5.2).
10. **Test protection.** A truthy `GOBBY_TEST_PROTECT` blocks every Docker
    invocation unless `GOBBY_TEST_ALLOW_DOCKER` is `1`, `true` or `yes`, the
    same contract as `docker_guard.py`. The test target is accepted only
    under `GOBBY_TEST_PROTECT`: the 60892 test DSN, the
    `gobby-*-test-1` containers, the `gobby_test_*` volumes and Qdrant on
    loopback port 60990. A set `GOBBY_HUB_REHEARSAL_PROFILE` is refused,
    because rehearsal profiles belong to the epoch surface.
11. **Python CLI deprecation.** `gobby hub-backup [ARGS]` execs
    `gbackup backup [ARGS]`, and `gobby hub-backup restore [ARGS]` execs
    `gbackup restore [ARGS]`. `_stores.py`, `_verify.py`, `_manifest.py`
    and `_content.py` are deleted (8.1).
    - Kept helpers stay while a live caller remains (A6). `_integrity.py`
      keeps `refuse_symlink_traversal`, `require_regular_file`,
      `open_regular_binary` and `file_digest`. `files_home.py` keeps the
      helpers that `pack.py` and `bootstrap_restore.py` use.
      `bootstrap_restore.py` and `rehearsal.py` stay whole.
    - `_start_daemon` stays because `hub_maintenance.py` imports it until
      #23557 (Remove executor-less gobby hub-maintenance run campaigns and
      the epoch surface) removes the campaigns. Those campaigns already fail
      before their `hub-backup --epoch` step, so 8.1 does not wait for
      #23557.
12. **Platforms.** gbackup runs only on a local-mode hub owner on macOS,
    Linux or WSL 2. It refuses on native Windows and on a remote-mode daemon
    (A7).
13. **Live consistency (A8 and the approved expectations).**
    - A live backup skips `drain_ephemeral_principals`, which would revoke
      the logins of running agents. Cold runs keep the drain.
    - PostgreSQL identity, probes, schema counts and roles are read in the
      transaction whose exported snapshot `pg_dump` uses.
    - Qdrant point counts and FalkorDB graph counts are taken before and
      after capture. When the two readings match, verify requires exact
      equality. Otherwise it requires the restored value to fall within the
      recorded range.
    - A Qdrant collection's content digest is recorded only when its
      before and after digests match. Otherwise it is null, and verify
      checks the count range alone.
    - A FalkorDB graph present in both `GRAPH.LIST` readings is required.
      A graph present in only one reading is optional and has no count
      expectation. A restored graph set must hold every required graph
      and nothing outside the two readings.
14. **Qdrant digest form.** gbackup hashes points in its own canonical JSON
    form and stays consistent with itself. Byte compatibility with Python's
    `json.dumps` is out of scope (AGENTS.md rule 10). `gbackup verify`
    refuses a manifest whose `gobby_version` does not start with `gbackup `.
    Python-written backups were already verified inline, and restore reads
    their flags and hashes, never their digests.

Rejected alternatives:
- Moving `crates/gdaemon/src/lifecycle/pid_file.rs` into gcore for the
  maintenance claim. The move is 730 lines with no behavior gain. gbackup
  depends on the `gobby-daemon` library instead.
- Installing through `gobby cutover` (Draft 1). That is superseded by A2.
- A gcore-owned manifest (Draft 1). That is superseded by A1.
- Re-hashing every retained backup nightly. That reads about 60 GB per
  night.
- A full recovery rehearsal on a disposable, test-owned hub stack
  (enhancer E7, declined by the Orchestrator on 2026-10-05). The weekly
  verify already restores every store into disposable containers, and a
  test-owned hub stack is new infrastructure.

## As-Is Facts
`kind: framing`

Source trace taken on `0.5.0` at `76e648bfcd`. Re-derive line numbers with
gcode before editing.

**Python backup command** (`src/gobby/cli/hub_backup/cli.py`, 942 lines):
- `hub_backup` (208-310) takes `--output`, `--epoch` and `--json`. It
  resolves the DSN from the bootstrap file (`postgres_backup.py:344-353`)
  and resolves the target (`_hub_backup_target`, 177-205). It runs
  `_preflight` (388-405: required containers running, 5 GiB free) and
  `_require_safe_qdrant_target` (408-421: the test target needs Qdrant on
  loopback port 60990). It stops the daemon, takes the maintenance claim,
  runs `_run_backup`, verifies artifacts, writes the manifest and publishes
  the staged directory. In `finally` it removes staging and restarts the
  daemon. The default output is `~/.gobby/backups/hub/<%Y%m%dT%H%M%SZ>`, and
  the output path must not exist.
- `_run_backup` (464-529) runs in this order: PostgreSQL identity, probes,
  schema counts and roles; `dump_postgres`; `snapshot_qdrant`;
  `dump_falkordb`; `_archive_volumes`; `archive_files_home_store`;
  `archive_rule_allow_audit_logs`; `_archive_machine_identity` (532-547,
  `~/.gobby/machine_id` read under its file lock to `identity/machine_id`);
  and finally `_verify_stores` (656), which restore-verifies all five stores
  inline.
- `restore_hub_backup` (313-380) takes `BACKUP_ROOT`, a required
  `--database-url`, `--clean` and `--yes`. It refuses while the daemon runs,
  loads the manifest, verifies artifacts and requires PostgreSQL archive and
  restore verification. Under the maintenance claim it runs
  `restore_hub_files`, `restore_postgres_globals`, `restore_postgres_backup`
  and `reconcile_restored_principals`.
- `src/gobby/cli/__init__.py:69` registers `hub-backup`.
  `src/gobby/cli/hub_maintenance.py:18-19` imports `_start_daemon` and
  `load_rehearsal_profile`, and line 321 runs `hub-backup --epoch`. #23557
  (Remove executor-less gobby hub-maintenance run campaigns and the epoch
  surface) removes that epoch path.

**Stores** (`src/gobby/cli/hub_backup/_stores.py`, 807 lines):
- Artifact paths (49-54): `postgres/gobby.dump`, `postgres/globals.sql`,
  `qdrant/<collection>.snapshot`, `falkordb/dump.rdb`,
  `volumes/<volume>.tar.gz` and `logs/`. `files_home.py:32` adds
  `files/files_home.tar`. `HUB_VOLUMES` (56-60) is `gobby_postgres_data`,
  `gobby_qdrant_data` and `gobby_falkordb_data`. Timeouts (62-70): BGSAVE
  300 s with a 0.5 s poll, connect 10 s, archive list 120 s, Redis 60 s,
  `docker cp` 600 s, volume archive 3600 s, Qdrant 120 s.
  `GOBBY_POSTGRES_DUMP_TIMEOUT_SECONDS` bounds `pg_dump`. The FalkorDB RDB
  path is `/var/lib/falkordb/data/dump.rdb` (72).
- `collect_postgres_identity` (120-147) reads `pg_control_system()`'s system
  identifier, the database name and OID, and `MAX(schema_migrations.version)`
  as the starting head. `collect_row_count_probes` (150-164) counts every
  `public` table. `collect_schema_object_counts` (167-192) counts schema
  objects. `collect_source_roles` (195-210) lists roles, excluding
  `MANAGED_PRINCIPAL_RE` (85-90).
- `drain_ephemeral_principals` (213-230) calls the database's drain function
  and then requires no ephemeral role to remain. `dump_postgres` (304-363)
  drains first. It then runs `docker exec <container> pg_dump -U <user> -d
  <db> -Fc` and `pg_dumpall -U <user> --globals-only --no-role-passwords`,
  and checks the archive with `pg_restore --list` (389-408). The artifact
  names are `postgres-dump` and `postgres-globals`.
- `restore_postgres_globals` (238-263) strips passwords, makes each `CREATE
  ROLE` idempotent (266-301) and replays the script with `psql -v
  ON_ERROR_STOP=1`. `reconcile_restored_principals` (233-235) is the drain.
- `snapshot_qdrant` (416-458) takes each collection in sorted order. It
  records the exact count and `qdrant_collection_digest`, creates a snapshot
  with `wait`, downloads it, and deletes the server snapshot.
- `dump_falkordb` (496-518) reads `LASTSAVE`, issues `BGSAVE`, waits for
  `LASTSAVE` to advance, and records `GRAPH.LIST`, per-graph counts and
  `DBSIZE`. It then copies the RDB with `docker cp`.
- `tar_volumes` (630-673) inventories each volume through a read-only
  `alpine` container and writes `volumes/<volume>.tar.gz` with
  `tar czf`.
- `_archive_volumes` (cli.py 550-582) stops the services first, refuses
  when they did not stop, and restarts them in `finally`. A failed stop
  raises before that `try`, so a partial stop leaves services stopped.
- `archive_rule_allow_audit_logs` (708-738) copies
  `rule-allow-audit.jsonl` and its numeric rotations from the logs directory
  to `logs/`. It refuses symlinks, and a missing directory archives nothing.
  The live set is about 2.4 GB of an 8.7 GB backup.

**Content digests** (`_content.py`): `qdrant_collection_digest` (28-48)
scrolls 256 points at a time with payload and vector. Each point is
canonicalized as JSON of `id`, `payload` and `vector` with sorted keys
(79-96). The sorted per-point digests are aggregated as length-prefixed
SHA-256 (145-150). `_tar_inventory` (99-134) records path, type, size and
SHA-256 per member, and refuses symlinks, hard links and special members.

**files_home** (`src/gobby/cli/hub_backup/files_home.py`, 595 lines):
- `maintenance_claim` (102-116) claims `gobby.pid` with role `maintenance`.
- `write_restricted_archive` (229-289) writes a non-gzip tar. It refuses an
  output inside the source, prewalks, preflights the member graph, writes a
  temp file outside the roots, fsyncs, renames and fsyncs the parent.
  `_emit_entries` (302-349) opens each file through
  `open_files_home_descendant` and fails with `swap` when device, inode,
  size or link count changed since the prewalk. The method is
  `files-home-prewalk+sha256` (34), and the caps are 100,000 members and
  100 GiB (28-29).
- `verify_files_home_archive` (380-396) accepts only file and directory
  members. `restore_hub_files` (585-595) restores into the destination
  files_home, rehashing, checking free space and publishing each file
  durably. `archived_bootstrap` and `merge_bootstrap_preserving_files_home`
  (525-582) serve `pack.py`.

**Verify** (`src/gobby/cli/hub_backup/_verify.py`, 841 lines):
- `verify_postgres_restore` (101-138) restores into a scratch container
  (141-169), replays globals, checks roles (212-252, tolerating missing
  managed principals), restores the dump with `PGOPTIONS=-c
  event_triggers=off` (60), and compares row counts and schema object
  counts.
- `verify_qdrant_restore` (392-452) uploads each snapshot to the hub Qdrant
  as scratch collection `hub_backup_verify_<name>` (57, 455-466), compares
  the count and digest, and deletes the scratch collection in `finally`.
- `verify_falkordb_restore` (499-548) loads the dump in a scratch container
  (551-579) and compares graph counts.
- `verify_volume_archives` (649-678) compares source inventories with a
  scratch extraction.
- Disposable containers carry a label and a per-run nonce (87-93). Removal
  re-inspects the exact ID, name and nonce, and refuses unlabeled,
  mismatched or Compose-managed containers (757-817).

**Manifest** (`src/gobby/cli/hub_backup/_manifest.py`, 296 lines): the
format, version and name constants; `STORE_KEYS`;
`HUB_BACKUP_MANIFEST_SCHEMA_V3` (45-117), with `additionalProperties: false`
throughout and a required, nullable `epoch_id`; and the record types
(120-210). `check_manifest_gate` (255-296) is used only by tests and is not
ported.

**Restore** (`src/gobby/cli/postgres_backup.py`): `restore_postgres_backup`
(100-151) checks the SHA-256 and runs `pg_restore --list`. With `--clean`,
`_reset_postgres_database` runs `DROP DATABASE ... WITH (FORCE)` and
`CREATE DATABASE ... OWNER`. It then runs `docker exec -i -e
PGOPTIONS='-c event_triggers=off' pg_restore --no-owner` (44), releases the
restored maintenance epoch, and probes that `pg_search`, `pgaudit` and
`pgcrypto` are present. `_managed_postgres_container` (356-388) maps
`localhost:60891` with `gobby/gobby` to `gobby-postgres`, maps
`localhost:60892` with `gobby_test/gobby_test` under `GOBBY_TEST_PROTECT` to
`gobby-postgres-test-1`, and refuses anything else.

**Epoch login guard:** `crates/gcore/assets/schema/baseline.sql:6690-6740`
installs a login event trigger. It refuses connections while an unreleased
maintenance epoch exists, unless the session sets `-c event_triggers=off`.

**Guards and locks:**
- `src/gobby/cli/installers/docker_guard.py:35-56` defines the Docker guard.
- `src/gobby/cli/installers/managed_services_lock.py:61-105` flocks
  `~/.gobby/managed-services.lock` and records the holder.
- `src/gobby/paths.py:132-149` (`require_files_home`) refuses on `win32`,
  refuses in remote mode, and errors when files_home is not configured.
- The managed containers are `gobby-postgres`, `services-qdrant-1` and
  `services-falkordb-1` (`container_restart.py:16-18`).
- `src/gobby/data/docker-compose.services.yml:79` sets `pgaudit.log=none`,
  so no pgaudit log exists to back up.

**Rust reuse:**
- `crates/gdaemon/src/lifecycle/pid_file.rs::claim_pid_file` (379-404)
  ports the Python claim. It is public as
  `gobby_daemon::lifecycle::pid_file`, gated `#[cfg(unix)]`, and proven
  against Python by `crates/gdaemon/tests/pid_file_golden.rs`.
  `lifecycle/mod.rs:1` notes that the daemon does not use it yet.
- gcore provides:
  - `gobby_home` (`lib.rs:31`);
  - `bootstrap::postgres_database_url_from_bootstrap_file` (219) and
    `read_files_home_view` (233), which returns `FilesHomeView` with
    `DatastoreMode`;
  - `machine::read_machine_id_from_home` (34);
  - `postgres::connect_readonly` (27);
  - `ai::effective_config::ai_source_for_conn` (354);
  - `config::resolve_qdrant_config` (`config/resolve.rs:182`).
- `Cargo.lock` already has `chrono`, `flate2`, `jsonschema`, `nix`,
  `postgres`, `rustix`, `sha2` and `ureq`. `tar` is new.
- `.github/workflows/rust-ci.yml:150` runs
  `cargo nextest run --workspace` on Ubuntu, so a new workspace member is
  tested without a workflow edit.

**Install carriers (gclient pattern):**
- `src/gobby/cli/install_setup_gclient.py::install_gclient_from_submodule`
  (114-177) builds from the workspace, and `install_gclient` (261-341)
  chooses the method.
- `src/gobby/cli/install_setup.py` (615 lines) has
  `MANAGED_NATIVE_BINARY_NAMES` (280), `_MANAGED_NATIVE_BINARY_DESCRIPTIONS`
  (281-286), `_run_managed_native_binary_installs` (306-318) and the
  `_install_gclient` wrappers (575-615).
- Other carriers: `src/gobby/install/version_pins.py`
  (`MANAGED_BIN_VERSION_PINS` 5-12, `UNPUBLISHED_MANAGED_BINS` 14);
  `src/gobby/install/bin_freshness_promotion.py::WORKSPACE_BINARY_CRATES`
  (207); `src/gobby/utils/status.py::_MANAGED_BIN_LABELS` (28); and
  `src/gobby/cli/install_components.py` (`COMPONENTS` 63-79,
  `COMPONENT_LABELS` 95-111, `promote_client_binary` 205-226).
- `distribution.py:18` lists Homebrew helpers that the formula must
  install. gbackup is not added there (deferral D1).

**Service install:**
- `src/gobby/cli/installers/service.py` (652 lines) has
  `install_service` (491-512) and `uninstall_service` (515-526), which
  dispatch by platform. It also has `_write_macos_plist` (92-102).
- `service_common._render_template` renders Jinja templates from
  `src/gobby/install/shared/services/`.
- `src/gobby/cli/service.py` has the `install` (26-73) and `uninstall`
  (76-88) commands.
- `bundled_content_manifest.json` is untracked
  (`tests/install/test_bundled_content_manifest.py:22`), so it is not a
  target.

## Parity Checklist
`kind: framing`

Every current Python behavior maps to the deliverable that ports it, or to a
recorded change.

| Python behavior (source) | gbackup deliverable |
| --- | --- |
| `hub-backup --output --json` (`cli.py:208-310`) | 1.1, 2.7 |
| `hub-backup --epoch` (`cli.py`, `hub_maintenance.py:321`) | Removed (D1; #23557 (Remove executor-less gobby hub-maintenance run campaigns and the epoch surface)) |
| Default output `~/.gobby/backups/hub/<UTC>` and absent-path check | 2.7 |
| DSN from bootstrap (`postgres_backup.py:344`) | 1.3 |
| Target resolution: production or test (`cli.py:177-205`) | 1.3 |
| Rehearsal-profile target (`rehearsal.py`) | Refused (1.3; Decision Record 10) |
| Managed PostgreSQL container by DSN (`postgres_backup.py:356-388`) | 1.3 |
| Preflight: containers running, 5 GiB free (`cli.py:388-405`) | 1.3 |
| Safe Qdrant target (`cli.py:408-421`) | 1.3 |
| Qdrant URL and key from config (`cli.py:845-891`) | 1.3 |
| Docker guard (`docker_guard.py:35-56`) | 1.3 |
| Windows and remote-mode refusal (`paths.py:132-149`) | 1.3 |
| Daemon stop and restart around a backup | Live keeps it up (2.7); cold refuses while it runs (6.1) |
| Maintenance claim (`files_home.py:102-116`) | 1.2; used by 5.2 and 6.1 |
| Source identity and starting head (`_stores.py:120-147`) | 2.2 |
| Row-count probes and schema object counts (`_stores.py:150-192`) | 2.2, in the exported snapshot |
| Source roles minus managed principals (`_stores.py:195-210`) | 2.2 |
| Drain before dump (`_stores.py:213-230`, 315) | Live skips (A8); cold drains (6.1) |
| `pg_dump -Fc`, globals without passwords, `pg_restore --list` (`_stores.py:304-408`) | 2.2 |
| Qdrant snapshots, counts, digests, server snapshot deletion (`_stores.py:416-458`) | 2.3 |
| FalkorDB BGSAVE, RDB copy, graph counts (`_stores.py:496-518`) | 2.4 |
| Volume tars with services stopped (`_stores.py:550-673`) | 6.1; live records them skipped (2.1) |
| files_home restricted archive (`files_home.py:229-377`) | 2.5 |
| Machine identity (`cli.py:532-547`) | 2.5 |
| Rule-allow audit logs (`_stores.py:708-738`) | 2.6, every nightly (D2) |
| Inline verify of all stores (`cli.py:656`) | Weekly `gbackup verify` (4.4); cold verifies inline (6.2) |
| Manifest v3 schema, types and atomic write (`_manifest.py`) | 2.1 |
| Artifact SHA-256 verification (`_integrity.py`) | 2.1 |
| Staged publish and staging cleanup (`cli.py`) | 1.2, 2.7 |
| Scratch PostgreSQL verify (`_verify.py:101-252`) | 4.1 |
| Disposable-container labels, nonce and removal checks (`_verify.py:87-93`, 757-817) | 4.1 |
| Qdrant scratch-collection verify (`_verify.py:392-466`) | 4.2 |
| FalkorDB scratch verify (`_verify.py:499-579`) | 4.2 |
| files_home archive verify (`files_home.py:380-396`) | 4.3 |
| Volume archive verify (`_verify.py:649-678`) | 6.2 |
| Restore arguments, daemon refusal and confirmation (`cli.py:313-380`) | 5.2 |
| Restore gate on PostgreSQL verification | 5.2, widened to every restored store (D3 (a)) |
| `restore_hub_files` (`files_home.py:585-595`) | Primitive 4.3; restore 5.2 |
| `restore_postgres_globals` (`_stores.py:238-301`) | 5.2 |
| Database reset, `pg_restore --no-owner`, extension probes (`postgres_backup.py:100-151`) | 5.2 |
| Restored epoch release (`postgres_backup.py:100-151`) | Removed (D1); restore refuses an epoch manifest before any write (5.2) |
| `reconcile_restored_principals` (`_stores.py:233-235`) | 5.2 |
| Qdrant and FalkorDB restore | New in 5.1 (D3 (a)) |
| `check_manifest_gate` (`_manifest.py:255-296`) | Test-only; not ported |
| `hub-backup` and `hub-backup restore` registration (`cli/__init__.py` line 69) | Shim that execs gbackup (8.1) |

## Constraints
`kind: framing`

- Rust conventions follow `crates/CLAUDE.md`. Each module's unit tests live
  at `<module>/tests.rs`, declared with `#[cfg(test)] #[path = "..."] mod
  tests;`. Tests run with `cargo nextest`. No production `.rs` file reaches
  1,000 lines.
- Tests never touch the daemon's hub. Unit tests drive Docker through a
  fake `docker` executable written into a temp directory, and pass the
  environment snapshot explicitly instead of mutating the process
  environment. Live checks run only against the isolated test hub with
  `GOBBY_TEST_PROTECT=1` and `GOBBY_TEST_ALLOW_DOCKER=1`.
- Database tests (`serial_db`) connect only after `pg::serial_db_url`
  (1.3) accepts the test target, and their commands set
  `GOBBY_TEST_PROTECT=1`.
- No DSN, password, API key or token appears in argv, stdout, logs,
  `last-run.json` or the manifest. PostgreSQL client tools run through
  `docker exec` in the managed container, which needs no password, as
  Python does today.
- `src/gobby/cli/installers/service.py` is 652 lines. Backup timers live in
  a new module, and `service.py` gains only the dispatch calls.
- Plan readers never read `~/.gobby/local_cli_token` or
  `~/.gobby/bootstrap.yaml`. gbackup reads the bootstrap file through gcore
  only.
- A crate change is live only after `gobby install` promotes the new
  binary.

## P1: Crate and Run Envelope
`kind: framing`

**Goal:** `gbackup` builds in the workspace, parses its final command
surface, serializes runs, records outcomes, resolves a safe target and
installs through `gobby install`, all before it captures any data.

### 1.1 gobby-backup crate and command surface [category: code]
`kind: deliverable`

Targets:
- `Cargo.toml`
- `Cargo.lock`
- `crates/gbackup/Cargo.toml`
- `crates/gbackup/src/main.rs`
- `crates/gbackup/src/lib.rs`
- `crates/gbackup/src/cli.rs`
- `crates/gbackup/tests/cli.rs`

**Granularity:** one leaf. The seven files are the crate skeleton and its
argument contract, and they cannot be committed apart: the workspace member,
the lockfile entry, the manifest, the two entry points, the parser and its
test.

**Research context:** The workspace `Cargo.toml:2` lists members `gcode`,
`gcore`, `gclient`, `gdaemon`, `ghook`, `gterminal` and `gterminals`.
Sibling crates use edition 2024, `rust-version = "1.88"` and license
`FSL-1.1-ALv2`. `crates/gdaemon/Cargo.toml` declares library
`gobby_daemon`, and its `lifecycle::pid_file` module is `#[cfg(unix)]`.
Decision Record 12 makes native Windows a refusal. Decision Records 4, 8
and 9 fix the surface: `--epoch` and `status` are gone, and restore needs
`--database-url`.

Implementation:
- Add `crates/gbackup` to the workspace members, and add
  `[profile.release.package.gobby-backup]` with `opt-level = 3` beside the
  sibling entries.
- `crates/gbackup/Cargo.toml`: package `gobby-backup`, version `0.1.0`,
  edition 2024, `rust-version = "1.88"`, license `FSL-1.1-ALv2`, binary
  `gbackup`, library `gobby_backup`. Dependencies: `anyhow`, `clap` with
  derive, `serde`, `serde_json`, `chrono`, `sha2`, `rustix` and
  `gobby-core` (path `../gcore`, feature `postgres`). Each later leaf adds
  only the dependencies it uses.
- `cli.rs` defines the full surface:
  `gbackup backup [--output DIR] [--json] [--scheduled] [--cold]`,
  `gbackup verify [DIR] [--json] [--scheduled]` and
  `gbackup restore DIR --database-url URL [--clean] [--yes]`.
  `--scheduled` conflicts with `--output` and `--cold`, and
  `verify --scheduled` conflicts with `DIR`.
- `main.rs` resets SIGPIPE on Unix. It maps each command to a library entry
  point that returns an exit code, and each entry point returns exit 2 with
  `not implemented` until its leaf lands. Under `#[cfg(not(unix))]`, `main`
  prints `gbackup requires a local hub on macOS, Linux or WSL 2` and exits
  2. All Unix-only code sits behind `#[cfg(unix)]`, so the workspace still
  builds on Windows.

Planned verification:
`cargo nextest run -p gobby-backup`, then `cargo clippy -p gobby-backup
--all-targets -- -D warnings` and `cargo fmt -p gobby-backup -- --check`.

**Acceptance:**

- 1.1.1 - `gbackup --version` prints the crate version and exits 0. test:
  `crates/gbackup/tests/cli.rs::version_prints_crate_version`.
- 1.1.2 - `--help` lists exactly `backup`, `verify` and `restore`, and no
  help text mentions `--epoch`. test:
  `crates/gbackup/tests/cli.rs::help_lists_three_commands_without_epoch`.
- 1.1.3 - `restore DIR` without `--database-url` is a usage error with exit
  2. test: `crates/gbackup/tests/cli.rs::restore_requires_database_url`.
- 1.1.4 - `backup --scheduled` combined with `--output` or `--cold`, and
  `verify --scheduled DIR`, are usage errors with exit 2. test:
  `crates/gbackup/tests/cli.rs::scheduled_conflicts_are_usage_errors`.

### 1.2 Run envelope: lock, run records, jitter, staging and maintenance claim [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `crates/gbackup/src/envelope.rs`
- `crates/gbackup/src/envelope/tests.rs`
- `crates/gbackup/src/lib.rs`
- `crates/gbackup/Cargo.toml`
- `Cargo.lock`

**Granularity:** one leaf with seven acceptance items. The lock, the run
record, the jitter, the staging sweep and the claim are small primitives
that every command consumes together. Each one alone would be a leaf with
no caller.

**Research context:** Decision Records 3, 6, 7 and 8 set the behavior.
crates/gdaemon/src/lifecycle/pid_file.rs::claim_pid_file(pid_file, role)
returns `io::Result<Option<PidFileClaim>>`. It yields `None` while another
holder has the lock, or while a live reservation or another live PID exists.
Python's `maintenance_claim` (src/gobby/cli/hub_backup/files_home.py:102-116) claims
`<gobby home>/gobby.pid` with role `maintenance`, and
`crates/gdaemon/tests/pid_file_golden.rs` proves the Rust record format
matches Python's. crates/gcore/src/machine.rs::read_machine_id_from_home reads the
machine ID. `rustix` (in `Cargo.lock`) provides `flock`, `openat` and
`statvfs`.

Implementation:
- The backup root is `<gobby_home()>/backups/hub`, created with mode 0700.
- `flock_until(file, deadline)` is the only blocking lock in gbackup. It
  tries a non-blocking exclusive flock every 0.5 s until the deadline,
  through an injected clock and sleeper. On expiry it fails with `lock
  wait timed out after <s> s`, naming the lock file and the holder it
  records. Callers pass the capture budget
  (`GOBBY_POSTGRES_DUMP_TIMEOUT_SECONDS`, default 600 s, read in 1.3).
- `RunLock::acquire(root, command, policy, deadline)` flocks
  `gbackup.lock` and then writes the holder (PID, command, start time)
  into it.
  - `Skip` (scheduled backup) returns a `skipped_locked` outcome.
  - `Wait` (scheduled verify) uses `flock_until`, and reports the seconds
    it waited. A timeout records status `error` and exits 1.
  - `Refuse` (every interactive command) fails with exit 1 and a message
    naming the holder's PID and command.
- `LastRun` is `{"backup": RunRecord | null, "verify": RunRecord | null}`.
  - A `RunRecord` holds `started_at`, `finished_at`, `status` (`ok`,
    `error` or `skipped_locked`), `backup_dir`, `error`, `pid` and
    `lock_wait_seconds`.
  - Only scheduled runs write it.
  - A writer takes `last-run.lock` through `flock_until`, reads the file,
    replaces its own mode's record, writes a 0600 temp file, fsyncs it,
    renames it over `last-run.json` and fsyncs the directory.
  - A failure before the rename (the lock deadline, the read, the temp
    write or its fsync, or the rename itself) leaves the previous
    `last-run.json` as it was and removes the temp file.
  - A directory fsync that fails after a successful rename leaves the
    new, complete record visible. Its durability is unconfirmed, and
    nothing is rolled back.
  - Either way, a timestamped line `<UTC> gbackup: could not record
    <mode> run: <error>` goes to stderr, which the timer appends to its
    log, and the run exits 1. After a rename the line says the
    record's durability is unconfirmed. A recording failure is never
    itself recorded.
  - An error string is the error chain's display. No code path formats a
    DSN or a key into an error.
- `jitter_minutes(machine_id)` takes the first eight bytes of the
  SHA-256 of the machine ID as a big-endian `u64`, modulo 16. The scheduled
  entry points sleep that long before taking the lock, through an injected
  sleeper. A missing machine ID fails the run.
- `Staging::create(parent)` makes a 0700 directory
  `.gbackup-staging-<UTC>-<pid>` and removes it on drop unless it was
  published. `sweep_stale_staging(root)` runs under the lock. It removes
  only real directories with that prefix directly under the root, and never
  follows a symlink.
- `maintenance_claim(home)` calls `claim_pid_file(&home.join("gobby.pid"),
  Role::Maintenance)`. `None` fails with `the Gobby daemon or another
  maintenance run holds gobby.pid; stop the daemon with gobby stop first`.
  This adds the `gobby-daemon` path dependency.

Planned verification:
`cargo nextest run -p gobby-backup -E 'test(envelope)'`, then `cargo clippy
-p gobby-backup --all-targets -- -D warnings`.

**Acceptance:**

- 1.2.1 - With the lock held, the `Skip` policy returns `skipped_locked`
  without waiting. test:
  `crates/gbackup/src/envelope/tests.rs::skip_policy_returns_skipped_locked`.
- 1.2.2 - The `Wait` policy acquires the lock after the holder releases it,
  and reports the wait. A holder that stays alive past the deadline fails
  the wait with a timeout naming the holder, and `flock_until` behaves the
  same for `last-run.lock`. test:
  `crates/gbackup/src/envelope/tests.rs::wait_policy_acquires_after_release_and_times_out`.
- 1.2.3 - The `Refuse` policy fails, naming the holder's PID and command.
  test: `crates/gbackup/src/envelope/tests.rs::refuse_policy_names_holder`.
- 1.2.4 - Writing a `verify` record keeps the existing `backup` record, and
  the file mode is 0600. With injected failures:
  - a `last-run.lock` held past the deadline, or a failed temp write,
    leaves the previous file byte-identical;
  - a directory fsync that fails after the rename leaves the new record
    in place and reports unconfirmed durability.

  Each failure prints the timestamped stderr line, exits 1, and attempts
  no rollback and no second record. test:
  `crates/gbackup/src/envelope/tests.rs::last_run_replaces_only_its_mode_and_never_clobbers`.
- 1.2.5 - Jitter is stable for one machine ID and always within 0 to 15
  minutes. test:
  `crates/gbackup/src/envelope/tests.rs::jitter_is_stable_and_bounded`.
- 1.2.6 - The sweep removes stale staging directories only. It leaves
  backups, other names and symlinks to staging-named targets untouched.
  test: `crates/gbackup/src/envelope/tests.rs::sweep_removes_only_stale_staging`.
- 1.2.7 - `maintenance_claim` fails while another handle holds a daemon
  claim on the same `gobby.pid`, and succeeds once it is released. test:
  `crates/gbackup/src/envelope/tests.rs::maintenance_claim_refuses_held_daemon_claim`.

### 1.3 Target resolution, Docker guard and preflight [category: code] (depends: 1.2)
`kind: deliverable`

Targets:
- `crates/gbackup/src/target.rs`
- `crates/gbackup/src/target/tests.rs`
- `crates/gbackup/src/docker.rs`
- `crates/gbackup/src/docker/tests.rs`
- `crates/gbackup/src/pg.rs`
- `crates/gbackup/src/pg/tests.rs`
- `crates/gbackup/src/lib.rs`

**Granularity:** one leaf with eight acceptance items. Target resolution,
the Docker runner and the PostgreSQL session are the run's bounded paths
to the target, and every later leaf uses all three.

**Research context:**
- `_hub_backup_target` (hub_backup/cli.py lines 177-205) and `_managed_postgres_container`
  (postgres_backup.py lines 356-388) define the mapping. Hosts `localhost`,
  `127.0.0.1` and `::1` count as local.
  - Port 60891 with user and database `gobby` maps to the production
    target: containers `gobby-postgres`, `services-qdrant-1` and
    `services-falkordb-1`, with `HUB_VOLUMES`.
  - Port 60892 with `gobby_test`/`gobby_test` maps to the test target, and
    only under `GOBBY_TEST_PROTECT`. Its containers are
    `gobby-postgres-test-1`, `gobby-qdrant-test-1` and
    `gobby-falkordb-test-1`. Its volumes are `gobby_test_postgres_data`,
    `gobby_test_qdrant_data` and `gobby_test_falkordb_data`
    (hub_backup/cli.py lines 127-133). Qdrant must be on loopback
    port 60990 (hub_backup/cli.py lines 408-421).
- `_preflight` (hub_backup/cli.py lines 388-405) requires the
  containers running and 5 GiB free (`MIN_FREE_BYTES`).
- Python reads Qdrant settings from the config store
  (hub_backup/cli.py lines 845-891). In Rust that is gcore's
  `connect_readonly` (postgres.rs), then `ai_source_for_conn`
  (ai/effective_config.rs), then `resolve_qdrant_config`
  (config/resolve.rs).
- The guard is defined in installers/docker_guard.py lines 35-56.
- gcore's `read_files_home_view` (bootstrap.rs) returns the datastore mode
  and files_home, and `postgres_database_url_from_bootstrap_file`
  (bootstrap.rs) returns the DSN.
- gcore's `connection_config` (postgres.rs) sets only `connect_timeout`,
  and `connect_readonly` sets `default_transaction_read_only`. Neither
  bounds a statement or a lock wait.
- Python's `pg_dump` budget defaults to `_SUBPROCESS_TIMEOUT_SECONDS`
  (600 s, postgres_backup.py line 42).

Implementation:
- `EnvSnapshot` is read once in `main`: `GOBBY_TEST_PROTECT`,
  `GOBBY_TEST_ALLOW_DOCKER`, `GOBBY_HUB_REHEARSAL_PROFILE` and
  `GOBBY_POSTGRES_DUMP_TIMEOUT_SECONDS`. Code and tests pass it explicitly.
- `Docker { program, allowed }`:
  - `run`, `capture_to_file` and `run_with_stdin_file` each take a timeout
    and kill the child when it expires.
  - A blocked guard fails before spawning.
  - Error text carries the action and exit status, never argv values taken
    from the DSN.
  - The program defaults to `docker` on `PATH`. Tests pass a fake script.
- `resolve_backup_target(env, home)` checks in order:
  1. Refuse a set rehearsal profile.
  2. Refuse remote mode, and an unconfigured files_home.
  3. Read the bootstrap DSN. A missing DSN fails with `PostgreSQL bootstrap
     DSN is not configured. Run gobby postgres install first.`
  4. Map the DSN as above. Anything else is refused with the host, port,
     user and database named, never the password.
- `resolve_restore_target(env, database_url)` applies the same mapping to
  the explicit URL.
- `resolve_qdrant(target, dsn)` returns the URL and an optional API key
  held in memory only. A missing URL fails. The test target requires
  loopback port 60990.
- `preflight(target, docker, root)` requires each target container to
  report `State.Running`, and at least 5 GiB free at the backup root.
- `pg::connect(dsn, access, budget)` opens every gbackup PostgreSQL
  session. It calls gcore's `connect_readonly` or `connect_readwrite`,
  then sets `statement_timeout` and `lock_timeout` to `budget`. The
  budget is the capture budget from `GOBBY_POSTGRES_DUMP_TIMEOUT_SECONDS`
  (default 600 s, as Python). There is no new setting. `resolve_qdrant`
  reads the config through it.
- `pg::serial_db_url(raw, env)` is test-only and gates every `serial_db`
  test:
  - Unset, it returns none and the test skips with a message.
  - Set, the URL must resolve through `resolve_restore_target` to the
    test target, which needs `GOBBY_TEST_PROTECT` and loopback port 60892
    with `gobby_test`/`gobby_test`.
  - The production DSN and any other URL fail before a connection
    opens, naming host, port, user and database but never the password.

Planned verification:
`cargo nextest run -p gobby-backup -E 'test(target) | test(docker) | test(pg)'`,
then `GOBBY_TEST_PROTECT=1 GBACKUP_POSTGRES_TEST_DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test cargo nextest run -p gobby-backup -E 'test(pg)'`
and `cargo clippy -p gobby-backup --all-targets -- -D warnings`.

**Acceptance:**

- 1.3.1 - With `GOBBY_TEST_PROTECT` truthy, every Docker call fails before
  spawning unless `GOBBY_TEST_ALLOW_DOCKER` is `1`, `true` or `yes`. test:
  `crates/gbackup/src/docker/tests.rs::test_protect_blocks_docker_without_allow`.
- 1.3.2 - A Docker call that outlives its timeout is killed and reported as
  timed out. test: `crates/gbackup/src/docker/tests.rs::timeout_kills_child`.
- 1.3.3 - The production DSN maps to the production target. The test DSN
  maps to the test target only under `GOBBY_TEST_PROTECT`. Any other DSN is
  refused without its password in the message. test:
  `crates/gbackup/src/target/tests.rs::dsn_maps_to_managed_target`.
- 1.3.4 - Remote mode, an unconfigured files_home and a set rehearsal
  profile are each refused before any Docker call. test:
  `crates/gbackup/src/target/tests.rs::refuses_remote_unconfigured_and_rehearsal`.
- 1.3.5 - The test target refuses a Qdrant URL that is not on loopback port
  60990, and a missing Qdrant URL fails. test:
  `crates/gbackup/src/target/tests.rs::qdrant_target_checks`.
- 1.3.6 - Preflight fails when a target container is not running or less
  than 5 GiB is free. test:
  `crates/gbackup/src/target/tests.rs::preflight_requires_running_containers_and_free_space`.
- 1.3.7 - Every gbackup PostgreSQL session sets `statement_timeout` and
  `lock_timeout` to the capture budget. A statement blocked by a
  conflicting lock fails at the lock timeout. test:
  `crates/gbackup/src/pg/tests.rs::serial_db_lock_conflict_times_out`.
- 1.3.8 - The `serial_db` gate skips when its variable is unset. It
  refuses the production DSN, any other URL, and the test DSN without
  `GOBBY_TEST_PROTECT`, each before a connection opens. test:
  `crates/gbackup/src/pg/tests.rs::serial_db_gate_refuses_non_test_targets`.

### 1.4 Managed install through gobby install [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `src/gobby/cli/install_setup_gbackup.py`
- `src/gobby/cli/install_setup.py::*` — scope-reason: add `gbackup` to `MANAGED_NATIVE_BINARY_NAMES`, `_MANAGED_NATIVE_BINARY_DESCRIPTIONS` and the installer map in `_run_managed_native_binary_installs`, plus `_GBACKUP_*` constants and thin `_install_gbackup*` wrappers beside the gclient ones (575-615)
- `src/gobby/install/version_pins.py::MANAGED_BIN_VERSION_PINS`
- `src/gobby/install/version_pins.py::UNPUBLISHED_MANAGED_BINS`
- `src/gobby/install/bin_freshness_promotion.py::WORKSPACE_BINARY_CRATES`
- `src/gobby/cli/install_components.py::COMPONENTS`
- `src/gobby/cli/install_components.py::COMPONENT_LABELS`
- `src/gobby/cli/install_components.py::_workspace_client_version`
- `src/gobby/cli/install_components.py::promote_client_binary`
- `src/gobby/cli/install_components.py::run_install_components`
- `tests/cli/test_install_setup_gbackup.py`
- `tests/cli/test_cli_install.py::*` — scope-reason: the managed binary tuple assertion at 1312 gains `gbackup`
- `tests/cli/test_install_setup_gterm.py::*` — scope-reason: the same tuple assertion at 587
- `tests/install/test_version_pins.py::*` — scope-reason: the unpublished-set assertion at 31 gains `gbackup`
- `tests/cli/test_install_binary_components.py::*` — scope-reason: client promotion covers `gbackup`
- `tests/cli/test_install_components.py::*` — scope-reason: the component count assertion at 57 becomes 13

**Granularity:** one leaf. Five hand-maintained production files change,
and all of them carry the one outcome: `gobby install` builds and promotes
gbackup. The test files only follow the new entries.

Consumers unchanged:
- `src/gobby/install/distribution.py` — no-edit-reason: `HOMEBREW_HELPERS` lists what the formula installs; gbackup joins it with the formula (D1).
- `src/gobby/install/bin_set_coherence.py` — no-edit-reason: the stamped set stays `gcode`, `gdaemon` and `ghook`; gbackup is promoted alone, as gclient is.
- `src/gobby/cli/install_setup_gclient.py` — no-edit-reason: reads only its own pin key.
- `src/gobby/cli/install_setup_gcode.py` — no-edit-reason: reads only its own pin key.
- `src/gobby/cli/install_setup_gdaemon.py` — no-edit-reason: reads only its own pin key.
- `src/gobby/cli/install_setup_ghook.py` — no-edit-reason: reads only its own pin key.
- `src/gobby/cli/install_setup_gterm.py` — no-edit-reason: reads only its own pin key.
- `src/gobby/cli/install_setup_versions.py` — no-edit-reason: looks a pin up by the name it is given.
- `src/gobby/install/bin_freshness_models.py` — no-edit-reason: iterates every pin, so it covers `gbackup` with no edit.
- `src/gobby/code_index/gcode_gateway.py` — no-edit-reason: reads the `gcode` pin only.
- `src/gobby/hooks/runtime_compat.py` — no-edit-reason: reads the `ghook` pin only.
- `src/gobby/cli/install.py` — no-edit-reason: calls `run_install_components` with the components the user names; the signature is unchanged.
- `tests/cli/installers/test_grok_installer.py` — no-edit-reason: reads unrelated pin keys.
- `tests/cli/test_install_ghook.py` — no-edit-reason: reads the `ghook` pin only.
- `tests/cli/test_install_setup.py` — no-edit-reason: reads the `gcode` pin only.
- `tests/cli/test_install_setup_gclient.py` — no-edit-reason: reads the `gclient` pin only.
- `tests/cli/test_install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin only.
- `tests/code_index/test_gcode_gateway.py` — no-edit-reason: reads the `gcode` pin only.
- `tests/hooks/test_runtime_compat.py` — no-edit-reason: reads the `ghook` pin only.
- `tests/install/test_distribution.py` — no-edit-reason: covers the Homebrew helpers, which exclude gbackup until D1.
- `tests/cli/test_install_srt_component.py` — no-edit-reason: drives the `srt` component only.

**Research context:** `install_setup_gclient.py::install_gclient`
(261-341) takes the workspace path when the binary is unpublished
(`is_published`). `install_gclient_from_submodule` (114-177) builds the
crate in the source checkout. It holds the native-bin lock, replaces the
binary through a new inode, signs it ad hoc and writes the source hash
(`workspace_binary_is_current`, `try_acquire_native_bin_lock`,
`write_source_hash`). When no build is possible and no binary exists, it
raises `ManagedBinaryReleaseMissing`, and
`_run_managed_native_binary_installs` (306-318) turns that into a warning.
`install_components.py::promote_client_binary` (205-226) promotes one
client binary without claiming the daemon singleton.
`WORKSPACE_BINARY_CRATES` (207-210) maps each binary to the crates whose
sources make it stale. gbackup compiles `gbackup`, `gcore` and `gdaemon`.

Implementation:
- `install_setup_gbackup.py` mirrors the workspace path of
  `install_setup_gclient.py` with `_CRATE_PACKAGE = "gobby-backup"` and
  `_CRATE_DIR = "gbackup"`. It provides `install_gbackup_from_submodule`,
  `write_gbackup_version_stamp` (`.gbackup-version`),
  `get_installed_gbackup_version` and `install_gbackup`. There is no
  GitHub or cargo method, because gbackup has no published artifact.
  `install_gbackup` returns a skip with reason `native Windows cannot own a
  hub` on `win32`.
- `install_setup.py` adds the name, the description `hub backup runner`,
  the installer entry and the wrappers.
- Pins: `"gbackup": "0.1.0"` in `MANAGED_BIN_VERSION_PINS`, and `gbackup`
  in `UNPUBLISHED_MANAGED_BINS`.
- `WORKSPACE_BINARY_CRATES["gbackup"] = ("gbackup", "gcore", "gdaemon")`.
- `install_components.py` adds the `gbackup` component and label, the crate
  directory, the promotion and stamp entries, and the
  `name in {"gclient", "gterm", "gbackup"}` branch.

Planned verification:
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/cli/test_install_setup_gbackup.py tests/cli/test_cli_install.py tests/cli/test_install_setup_gterm.py tests/install/test_version_pins.py tests/cli/test_install_binary_components.py tests/cli/test_install_components.py -q`,
then `uv run ruff check src/ && uv run mypy src/` and a test-types audit of
the changed tests.

**Acceptance:**

- 1.4.1 - A workspace build promotes `gbackup` into the bin directory
  through a new inode and writes `.gbackup-version`. test:
  `tests/cli/test_install_setup_gbackup.py::test_workspace_build_promotes_gbackup`.
- 1.4.2 - With no source checkout and no installed binary, the install
  raises `ManagedBinaryReleaseMissing`, and the managed install loop
  reports it as a warning and continues. test:
  `tests/cli/test_install_setup_gbackup.py::test_missing_workspace_warns_and_continues`.
- 1.4.3 - On `win32` the install skips with its reason. test:
  `tests/cli/test_install_setup_gbackup.py::test_windows_skips_gbackup`.
- 1.4.4 - The `gbackup` install component promotes the binary without
  claiming the daemon singleton. test:
  `tests/cli/test_install_binary_components.py::test_promote_client_binary_gbackup`.
- 1.4.5 - `gbackup` is unpublished and pinned at `0.1.0`. test:
  `tests/install/test_version_pins.py::test_gbackup_is_unpublished_with_pin`.

### 1.5 gbackup in gobby status [category: code] (depends: 1.4)
`kind: deliverable`

Targets:
- `src/gobby/utils/deps.py::*` — scope-reason: add `get_gbackup_version` beside `get_gclient_version` (113-115) and the `gobby.gbackup` probe plus the `gbackup`, `gbackup_path` and `gbackup_stale` keys in `collect_all_deps`
- `src/gobby/utils/status.py::_MANAGED_BIN_LABELS`
- `tests/utils/test_utils_status.py::*` — scope-reason: status rendering covers the gbackup line and its stale marker

**Research context:** `deps.py::collect_all_deps` reports each managed
binary's version, path and staleness (`native_bin_predates_source`).
`status.py` renders `_MANAGED_BIN_LABELS` (28) at 451-462, and marks a
binary stale when its sources are newer. A stale gbackup means the timers
run old code after a crate change, so the operator needs to see it.

Implementation:
- `get_gbackup_version()` returns
  `_get_native_binary_version("gbackup", ".gbackup-version")`.
- `collect_all_deps` adds the probe and the three keys. `gbackup` follows
  `gclient` in `_MANAGED_BIN_LABELS`.

Planned verification:
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/utils/test_utils_status.py -q`,
then `uv run ruff check src/ && uv run mypy src/`.

**Acceptance:**

- 1.5.1 - `gobby status` shows the gbackup version and path. test:
  `tests/utils/test_utils_status.py::test_status_shows_gbackup_version`.
- 1.5.2 - A stale gbackup shows the `[stale: rebuild and install]` marker.
  test: `tests/utils/test_utils_status.py::test_status_marks_stale_gbackup`.

## P2: Live Backup
`kind: framing`

**Goal:** `gbackup backup` captures PostgreSQL, Qdrant, FalkorDB, files_home,
the machine identity and the rule-allow audit logs while the daemon and the
datastores keep running, and publishes a v3 manifest.

### 2.1 Manifest v3 and artifact integrity [category: code] (depends: 1.3)
`kind: deliverable`

Targets:
- `crates/gbackup/src/manifest.rs`
- `crates/gbackup/src/manifest/schema_v3.json`
- `crates/gbackup/src/manifest/tests.rs`
- `crates/gbackup/src/lib.rs`
- `crates/gbackup/Cargo.toml`
- `Cargo.lock`

**Granularity:** one leaf with seven acceptance items. The manifest type,
its schema, its integrity check and its artifact-set rules are one contract
that every writer and reader shares.

**Research context:** Python's manifest module (_manifest.py) defines
format `gobby-hub-backup-manifest`, version 3, file name `manifest.json` and
the store keys `postgres`, `qdrant`, `falkordb`, `volumes` and `files`.
- Its JSON Schema (draft 2020-12, lines 45-117) sets
  `additionalProperties: false` on every object.
- The required top-level keys are:
  - `manifest_format` and `manifest_version`;
  - `created_at` and `gobby_version`;
  - `epoch_id`, a string or null;
  - `source_identity`: `pg_system_identifier` (a string), `database_name`
    and `database_oid` (an integer);
  - `backup_starting_head`;
  - `row_count_probes`, mapping a name to an integer;
  - `artifacts`, each with `name`, `path`, `sha256` (matching
    `^[0-9a-f]{64}$`) and `size_bytes` (at least 0);
  - `stores`.
- Each store holds `archive_verified`, `restore_verified` and an object
  `details`. A verification state is `{verified, method, timestamp}` (lines
  120-126).
- `artifact_integrity_errors` (_integrity.py lines 111-135) rejects:
  - an unsafe relative path;
  - a missing, non-regular or symlinked file;
  - a size or SHA-256 mismatch.

  It does not look for unlisted files.
- The Python artifact names are `postgres-dump`, `postgres-globals`,
  `qdrant-<collection>`, `falkordb-rdb`, `volume-<volume>`, `files-home`,
  `rule_allow_audit:<file>` and `machine_identity`.
- Decision Records 5, 6 and 14 apply.

Implementation:
- `schema_v3.json` carries Python's schema as JSON, including its `$id`.
  `manifest.rs` embeds it with `include_str!` and compiles it once with
  `jsonschema`.
- The serde types are `Manifest`, `SourceIdentity`, `Artifact`, `StoreRecord`
  and `VerificationState`, all with `deny_unknown_fields`.
  - `epoch_id` is `Option<String>`, and gbackup always writes null.
  - `GOBBY_VERSION` is `gbackup <CARGO_PKG_VERSION>`.
  - `Manifest::is_gbackup_written` checks that prefix.
- `write_manifest(dir, &manifest)` validates against the schema, then writes
  `manifest.json` with mode 0600. It writes a temp file, fsyncs it, renames
  it and fsyncs the directory.
- `read_manifest(dir)` refuses a symlinked or non-regular manifest, parses
  it, validates the schema and returns the typed value. Python-written
  manifests parse.
- `hash_artifact(root, rel)` resolves each path component with `openat` and
  `O_NOFOLLOW`, refuses a non-regular file, and streams the SHA-256 and the
  size.
- `verify_artifacts(root, artifacts)` collects every defect the Python check
  reports, and fails once with all of them.
- `check_artifact_set(&manifest)` adds the semantic rules the schema
  cannot state, and leaves the v3 JSON shape alone:
  - Artifact names are unique, and so are paths.
  - Only `volumes` may be skipped. `postgres`, `qdrant`, `falkordb` and
    `files` always need their records, whatever their details claim. A
    skipped `volumes` needs no volume records.
  - Each required record appears exactly once, at Python's paths: `postgres-dump` (`postgres/gobby.dump`),
    `postgres-globals` (`postgres/globals.sql`), `falkordb-rdb`
    (`falkordb/dump.rdb`), `files-home` (`files/files_home.tar`), one
    `qdrant-<name>` per collection in `details.collections` whose path
    equals that collection's `snapshot`, and one `volume-<v>` per
    recorded volume.
  - A `qdrant-` or `volume-` record with no matching detail entry is
    refused.
  - Defects are collected and reported once.
- Verify and restore run `check_artifact_set` before `verify_artifacts`.
  They read every input through `Manifest::artifact(name)`, never a
  fixed path, so every consumed byte is a hashed record.
- `integrity_ok_by_size(dir)` is the retention predicate of Decision Record
  6. It reads metadata only. It is true only for a gbackup-written live
  manifest.
- `StoreRecord::skipped_live_mode()` builds the live `volumes` record of
  Decision Record 5.

Planned verification:
`cargo nextest run -p gobby-backup -E 'test(manifest)'`, then `cargo clippy
-p gobby-backup --all-targets -- -D warnings`.

**Acceptance:**

- 2.1.1 - A written manifest validates against `schema_v3.json`, has mode
  0600, and records a null `epoch_id` and a `gobby_version` that starts
  with `gbackup `. test:
  `crates/gbackup/src/manifest/tests.rs::written_manifest_validates_and_is_private`.
- 2.1.2 - The schema rejects an unknown key, a missing store and a
  malformed SHA-256. test:
  `crates/gbackup/src/manifest/tests.rs::schema_rejects_unknown_missing_and_malformed`.
- 2.1.3 - A Python-written v3 fixture parses, and `is_gbackup_written` is
  false for it. test:
  `crates/gbackup/src/manifest/tests.rs::python_manifest_parses_but_is_not_gbackup_written`.
- 2.1.4 - `verify_artifacts` reports traversal, a symlinked component, a
  missing file, a size mismatch and a hash mismatch in one error. test:
  `crates/gbackup/src/manifest/tests.rs::verify_artifacts_reports_every_defect`.
- 2.1.5 - `integrity_ok_by_size` accepts a complete live backup even after
  an artifact's content changed at the same size. It rejects:
  - a size mismatch;
  - a missing artifact;
  - an unverified live store;
  - a cold manifest;
  - a Python-written manifest.

  test:
  `crates/gbackup/src/manifest/tests.rs::integrity_ok_checks_sizes_without_hashing`.
- 2.1.6 - The live `volumes` record has `details.skipped = true`,
  `details.reason = "live-mode"`, and both states unverified with method
  `skipped-live-mode`. test:
  `crates/gbackup/src/manifest/tests.rs::live_volumes_record_is_skipped_live_mode`.
- 2.1.7 - `check_artifact_set` refuses an omitted required record, a
  duplicate name, a duplicate path, a Qdrant snapshot path with no
  matching record, an orphan `qdrant-` record, and a `postgres` store
  whose details claim `skipped` while `postgres-globals` is omitted. It
  accepts the Python-written fixture, a cold manifest and a
  gbackup-written live manifest. test:
  `crates/gbackup/src/manifest/tests.rs::artifact_set_rules`.

### 2.2 PostgreSQL capture in an exported snapshot [category: code] (depends: 2.1)
`kind: deliverable`

Targets:
- `crates/gbackup/src/stores.rs`
- `crates/gbackup/src/stores/postgres.rs`
- `crates/gbackup/src/stores/postgres/tests.rs`
- `crates/gbackup/src/lib.rs`
- `crates/gbackup/Cargo.toml`
- `Cargo.lock`

**Research context:**
- `collect_postgres_identity` (_stores.py lines 120-147) reads:
  - the system identifier from `pg_control_system()`;
  - `current_database()` and its OID;
  - `MAX(version)` from `schema_migrations`. A missing table fails, and
    so does a table with no applied version (`schema_migrations has no
    applied version; refusing to back up an unmigrated database`, line
    139).
- Other collectors:
  - `collect_row_count_probes` (150-164) counts every base table in
    `public`.
  - `collect_schema_object_counts` (167-192) counts schema objects.
  - `collect_source_roles` (195-210) lists roles except those matching
    `MANAGED_PRINCIPAL_RE` (85-90): `gobby_agent_<32 hex>`, `gobby_ix_...`
    and `gobby_mnt_<32 hex>`, each with a numeric suffix.
- `dump_postgres` (304-363):
  - runs `pg_dump -U <user> -d <db> -Fc` and `pg_dumpall -U <user>
    --globals-only --no-role-passwords` through `docker exec` in the
    managed container;
  - checks the archive with `pg_restore --list` (`_check_archive_readable`,
    389-408, 120 s);
  - records the details `postgres_version` and `archive_list_checked`.

  The archive method is `pg-restore-list+sha256`, and
  `GOBBY_POSTGRES_DUMP_TIMEOUT_SECONDS` bounds `pg_dump`.
- `drain_ephemeral_principals` (213-230) runs
  `<auth schema>.drain_ephemeral_principals()` in the connection's current
  schema. It then requires that no login role matches
  `^gobby_agent_[0-9a-f]{32}_[1-9][0-9]*$` (`_EPHEMERAL_ROLE_SQL`, 96-100).
- gcore's `connect_readonly` and `connect_readwrite` (postgres.rs) open the
  bootstrap DSN.
- Decision Record 13: a live run reads identity, probes, schema counts and
  roles inside the transaction whose snapshot `pg_dump` imports, and skips
  the drain (A8).

Implementation:
- `stores.rs` declares the store modules and the shared result
  `StoreCapture { artifacts, details, archive_verified }`.
- `capture_postgres(ctx, mode)` runs in this order:
  1. In `Mode::Cold` only, run `drain_ephemeral_principals(dsn)` in a
     read-write `pg::connect` session (1.3).
  2. Open a read-only `pg::connect` session (1.3), run
     `BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY`, then
     `SELECT pg_export_snapshot()`.
  3. In that transaction, collect identity, the starting head, row-count
     probes, schema object counts and source roles with Python's queries.
  4. While the transaction stays open, run `docker exec <container>
     pg_dump -U <user> -d <db> -Fc --snapshot=<id>` into
     `postgres/gobby.dump`. Then end the transaction.
  5. Run `pg_dumpall --globals-only --no-role-passwords` into
     `postgres/globals.sql`, then `pg_restore --list` with the archive on
     stdin.
- Identity, head and probes fill the manifest's top-level fields. The
  details record `postgres_version`, `archive_list_checked`,
  `schema_object_counts` and `roles`, because the weekly verify runs in
  another process and needs those expectations.
- Identity collection fails before `pg_dump` runs when
  `schema_migrations` is missing or has no applied version, with
  Python's message.
- `drain_ephemeral_principals` is public for 5.2 and 6.1.
- Database tests are in the `serial_db` group, gated by
  `pg::serial_db_url` (1.3). Docker steps use the fake `docker`.

Planned verification:
`GOBBY_TEST_PROTECT=1 GBACKUP_POSTGRES_TEST_DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test cargo nextest run -p gobby-backup -E 'test(stores::postgres)'`,
then `cargo clippy -p gobby-backup --all-targets -- -D warnings`.

**Acceptance:**

- 2.2.1 - Probes, schema counts and roles come from the exporting
  transaction. A row that another connection commits after the export is
  not counted. test:
  `crates/gbackup/src/stores/postgres/tests.rs::serial_db_probes_read_exported_snapshot`.
- 2.2.2 - `pg_dump` runs with `-Fc --snapshot=<exported id>` while the
  transaction is open, and globals run with `--no-role-passwords`. No argv
  carries the DSN or a password. test:
  `crates/gbackup/src/stores/postgres/tests.rs::dump_argv_uses_snapshot_and_no_secrets`.
- 2.2.3 - A failed `pg_restore --list` fails the capture. test:
  `crates/gbackup/src/stores/postgres/tests.rs::unreadable_archive_fails_capture`.
- 2.2.4 - A live capture leaves an ephemeral login in place. A cold
  capture drains it first, and fails when one remains. test:
  `crates/gbackup/src/stores/postgres/tests.rs::serial_db_drain_runs_only_in_cold_mode`.
- 2.2.5 - Source roles exclude managed principals, and the details carry
  the schema counts and roles. test:
  `crates/gbackup/src/stores/postgres/tests.rs::serial_db_roles_exclude_managed_principals`.
- 2.2.6 - A missing `schema_migrations` table, or one with no applied
  version, fails identity collection before `pg_dump` runs. test:
  `crates/gbackup/src/stores/postgres/tests.rs::serial_db_unmigrated_database_fails`.

### 2.3 Qdrant capture [category: code] (depends: 2.2)
`kind: deliverable`

Targets:
- `crates/gbackup/src/stores.rs`
- `crates/gbackup/src/stores/qdrant.rs`
- `crates/gbackup/src/stores/qdrant/tests.rs`
- `crates/gbackup/Cargo.toml`
- `Cargo.lock`

**Research context:**
- `snapshot_qdrant` (_stores.py lines 416-458) takes the collections in
  sorted order. For each one it records the exact count and
  `qdrant_collection_digest`, creates a snapshot with `wait=true`, downloads
  it to `qdrant/<name>.snapshot`, and deletes the server snapshot.
  - The per-collection details are `points`, `snapshot` and
    `content_sha256`.
  - The artifact is `qdrant-<name>`, and the method is
    `snapshot-download+sha256`.
  - Calls time out after 120 s, and a missing Qdrant URL fails.
- `qdrant_collection_digest` (_content.py lines 28-48) scrolls 256 points at
  a time with payload and vector.
  - `_canonical_point` (79-96) serializes `id`, `payload` and `vector` as
    JSON with sorted keys and compact separators.
  - `_aggregate_digest` (145-150) hashes the sorted per-point digests with
    length prefixes.
- Verify scratch collections use the prefix `hub_backup_verify_` (_verify.py
  line 57).
- Decision Records 13 and 14 apply.
- `ureq` 2 is in `Cargo.lock`.
- Managed Qdrant has no API key. A configured key travels in the `api-key`
  header.

Implementation:
- `QdrantClient` uses `ureq` with a 120 s timeout. It can list collections,
  take an exact count, scroll, create a snapshot with `wait=true`, download
  a snapshot to a file, and delete a snapshot.
- Collections named with the `hub_backup_verify_` prefix are verify
  scratch, and are skipped.
- Each collection, in sorted order:
  1. Count and digest.
  2. Snapshot and download.
  3. Count and digest again.
  4. Delete the server snapshot. This runs on every path, including errors.
- The digest canonicalizes each point as a `serde_json::Value` with object
  keys sorted recursively, written as compact JSON. It does not depend on
  `serde_json`'s map order. The sorted per-point SHA-256 digests are hashed
  with 8-byte big-endian length prefixes.
- When the before and after readings are equal, the details record exact
  `points` and `content_sha256`.
  - Unequal counts record `points_min`, `points_max` and
    `live_drift: true`.
  - Unequal digests record `content_sha256: null`.
  - `snapshot` names the relative path.

Planned verification:
`cargo nextest run -p gobby-backup -E 'test(stores::qdrant)'`, then
`cargo clippy -p gobby-backup --all-targets -- -D warnings`.

**Acceptance:**

- 2.3.1 - Against a scripted HTTP server, each collection is snapshotted,
  downloaded to `qdrant/<name>.snapshot`, and its server snapshot deleted.
  Scratch-prefixed collections are skipped. test:
  `crates/gbackup/src/stores/qdrant/tests.rs::captures_each_collection_and_deletes_server_snapshot`.
- 2.3.2 - A failed download still deletes the server snapshot, and fails
  the capture. test:
  `crates/gbackup/src/stores/qdrant/tests.rs::failed_download_still_deletes_snapshot`.
- 2.3.3 - The digest does not change with point order or object key order.
  test: `crates/gbackup/src/stores/qdrant/tests.rs::digest_ignores_point_and_key_order`.
- 2.3.4 - Stable readings record exact `points` and `content_sha256`.
  Drifting readings record a count range, `live_drift` and a null digest.
  test: `crates/gbackup/src/stores/qdrant/tests.rs::drift_records_range_and_null_digest`.
- 2.3.5 - The API key is sent only in the header, and no error or detail
  contains it. test:
  `crates/gbackup/src/stores/qdrant/tests.rs::api_key_never_appears_in_errors_or_details`.

### 2.4 FalkorDB capture [category: code] (depends: 2.3)
`kind: deliverable`

Targets:
- `crates/gbackup/src/stores.rs`
- `crates/gbackup/src/stores/falkordb.rs`
- `crates/gbackup/src/stores/falkordb/tests.rs`

**Research context:**
- `dump_falkordb` (_stores.py lines 496-518) runs these steps:
  1. Read `LASTSAVE` and issue `BGSAVE`.
  2. Poll `LASTSAVE` every 0.5 s, for up to 300 s, until it advances.
  3. Record the sorted `GRAPH.LIST`, per-graph counts
     (`_falkordb_graph_counts`) and `DBSIZE`.
  4. Copy `/var/lib/falkordb/data/dump.rdb` with `docker cp` (600 s).
- `_redis_cli` (_stores.py lines 570-592) runs `docker exec <container> sh
  -c 'redis-cli -a "$GOBBY_FALKORDB_PASSWORD" --no-auth-warning --raw
  <request>'` with a 60 s timeout. The container's shell expands the
  password, so it never enters the host's argv.
- The artifact is `falkordb-rdb` at `falkordb/dump.rdb`, with method
  `bgsave-rdb-copy+sha256`. The details are `graphs`, `graph_inventory` and
  `dbsize`.
- Decision Record 13 applies.

Implementation:
- `capture_falkordb(ctx)` takes the graph counts and `DBSIZE` before
  `BGSAVE`, runs the save and the wait, and takes them again. Then it copies
  the RDB.
- Equal readings record Python's details. Unequal readings record each
  count as `{min, max}` and set `live_drift: true`.
- `graphs` holds the graphs present in both `GRAPH.LIST` readings, and
  `graph_inventory` counts only those. A graph in exactly one reading goes
  in `graphs_optional` with no counts, and sets `live_drift: true`.
  Stable membership writes no `graphs_optional`, as Python.
- `falkordb_matches(details, observed)` is the comparison that 4.2 and
  5.1 share. Every `graphs` entry must be present, no graph outside
  `graphs` and `graphs_optional` may be, and required counts and
  `DBSIZE` match exactly or by range.
- A `BGSAVE` that does not advance `LASTSAVE` within 300 s fails the
  capture. The clock and sleeper are injected.

Planned verification:
`cargo nextest run -p gobby-backup -E 'test(stores::falkordb)'`, then
`cargo clippy -p gobby-backup --all-targets -- -D warnings`.

**Acceptance:**

- 2.4.1 - Through the fake `docker`, capture waits for `LASTSAVE` to
  advance, then copies the RDB to `falkordb/dump.rdb`. test:
  `crates/gbackup/src/stores/falkordb/tests.rs::bgsave_waits_for_lastsave_then_copies`.
- 2.4.2 - A save that never completes fails at the timeout. test:
  `crates/gbackup/src/stores/falkordb/tests.rs::bgsave_timeout_fails_capture`.
- 2.4.3 - Stable readings record exact counts. Drifting readings record
  ranges and `live_drift`. test:
  `crates/gbackup/src/stores/falkordb/tests.rs::drift_records_count_ranges`.
- 2.4.4 - A graph created and another deleted during `BGSAVE` are both
  recorded as optional. `falkordb_matches` accepts a restored set with or
  without each of them, and refuses a missing required graph or an
  unknown one. test:
  `crates/gbackup/src/stores/falkordb/tests.rs::membership_drift_records_optional_graphs`.

### 2.5 files_home archive and machine identity [category: code] (depends: 2.4)
`kind: deliverable`

Targets:
- `crates/gbackup/src/stores.rs`
- `crates/gbackup/src/stores/files.rs`
- `crates/gbackup/src/stores/files/tests.rs`
- `crates/gbackup/Cargo.toml`
- `Cargo.lock`

**Research context:**
- `write_restricted_archive` (files_home.py lines 229-289) runs these
  steps:
  1. Refuse an output inside the source (`check_output_outside_sources`,
     119-129).
  2. Prewalk (`prewalk_files_home`, 132-145), recording the device, inode,
     size and link count from `lstat`.
  3. Preflight the member graph (170-203).
  4. Write a temp file outside the roots, fsync it, rename it and fsync the
     parent.
- `_emit_entries` (302-349) opens each file through
  `open_files_home_descendant`. It fails with code `swap` when the device,
  inode, size or link count changed.
- Symlinks and special files are refused. The caps are 100,000 members and
  100 GiB (lines 28-29). The tar is not gzipped.
- The artifact is `files-home` at `files/files_home.tar`, with method
  `files-home-prewalk+sha256` and details `members` and `sha256`.
- files_home comes from gcore's `read_files_home_view` (1.3).
- `_archive_machine_identity` (hub_backup/cli.py lines 532-547) reads
  `<gobby home>/machine_id` under `exclusive_file_lock` (durable_file.py
  lines 25-58). That lock flocks the sidecar `.machine_id.lock`. The file is
  written to `identity/machine_id` as artifact `machine_identity`, and a
  missing file archives nothing.
- `tar` is a new dependency.

Implementation:
- `capture_files(ctx)` prewalks with `lstat`. It rejects symlinks, special
  files and trees over the caps, and requires the staging directory to sit
  outside files_home.
- Each file is opened by walking its components with `openat` and
  `O_NOFOLLOW` from a files_home directory handle.
  - Its `fstat` must match the prewalk's device, inode, size and link
    count. Otherwise the run fails with `swap`.
  - Exactly the recorded size is read.
- The `tar` crate writes directories and files into a non-gzip temp
  archive. The archive is fsynced, renamed to `files/files_home.tar`, and
  the directory fsynced.
- `archive_verified` is set after the archive is read back: its members are
  only files and directories, and they match the prewalk inventory. The
  details carry `members`, `sha256` and the member inventory that verify
  compares (4.3).
- `capture_machine_identity(home)` takes the exclusive flock on
  `.machine_id.lock` through `flock_until` (1.2), reads `machine_id`, and writes `identity/machine_id`
  with mode 0600. A missing file archives nothing.

Planned verification:
`cargo nextest run -p gobby-backup -E 'test(stores::files)'`, then
`cargo clippy -p gobby-backup --all-targets -- -D warnings`.

**Acceptance:**

- 2.5.1 - The archive holds every file and directory of a fixture tree,
  with matching sizes and hashes. test:
  `crates/gbackup/src/stores/files/tests.rs::archive_matches_fixture_tree`.
- 2.5.2 - Each of these is refused:
  - a symlink;
  - a FIFO;
  - an output inside files_home;
  - a tree over the member cap.

  test:
  `crates/gbackup/src/stores/files/tests.rs::refuses_symlink_special_inside_output_and_caps`.
- 2.5.3 - A file replaced or resized between the prewalk and the archive
  fails with `swap`. A test hook makes the swap. test:
  `crates/gbackup/src/stores/files/tests.rs::swapped_file_fails_with_swap`.
- 2.5.4 - The machine identity is copied under the sidecar flock, and the
  copy waits while another holder has the lock. test:
  `crates/gbackup/src/stores/files/tests.rs::machine_identity_waits_for_sidecar_lock`.
- 2.5.5 - A missing `machine_id` archives nothing and does not fail the
  run. test:
  `crates/gbackup/src/stores/files/tests.rs::missing_machine_id_archives_nothing`.

### 2.6 Rule-allow audit logs [category: code] (depends: 2.5)
`kind: deliverable`

Targets:
- `crates/gbackup/src/stores.rs`
- `crates/gbackup/src/stores/logs.rs`
- `crates/gbackup/src/stores/logs/tests.rs`

**Research context:**
- `archive_rule_allow_audit_logs` (_stores.py lines 708-738) copies
  `rule-allow-audit.jsonl` and its numeric rotations (`.1` onward) from the
  logs directory to `logs/<name>`, as artifacts `rule_allow_audit:<name>`.
  It refuses symlinks, and a missing directory archives nothing.
- `resolved_logs_dir` (config/logging.py line 126) returns
  `<gobby home>/logs` when `logging.dir` holds the default `~/.gobby/logs`.
  Otherwise it returns the configured path with `~` expanded.
- In Rust, the key is read through `ConfigSource::config_value` (gcore
  config/resolve.rs), on the source that 1.3 opens for Qdrant.
- The live set is about 2.4 GB. Decision D2 puts the logs in every nightly.

Implementation:
- `capture_audit_logs(ctx)` resolves the logs directory as Python does,
  and lists `rule-allow-audit.jsonl` plus names that match
  `rule-allow-audit.jsonl.<digits>`.
- Symlinks fail the run. Each file is opened with `O_NOFOLLOW`, and
  exactly the size seen at open is copied. A log that grows during the copy
  keeps its tail for the next night.
- A missing directory or no matching files records `details.files = 0` and
  no artifacts.

Planned verification:
`cargo nextest run -p gobby-backup -E 'test(stores::logs)'`, then
`cargo clippy -p gobby-backup --all-targets -- -D warnings`.

**Acceptance:**

- 2.6.1 - The active log and its numeric rotations are copied to `logs/`
  under Python's artifact names. Other names in the directory are ignored.
  test: `crates/gbackup/src/stores/logs/tests.rs::copies_active_log_and_numeric_rotations`.
- 2.6.2 - A symlinked log fails the run, and a missing directory archives
  nothing. test:
  `crates/gbackup/src/stores/logs/tests.rs::symlink_fails_and_missing_dir_archives_nothing`.
- 2.6.3 - A configured `logging.dir` is used, and the default resolves to
  `<gobby home>/logs`. test:
  `crates/gbackup/src/stores/logs/tests.rs::logs_dir_follows_config`.

### 2.7 Live backup command [category: code] (depends: 2.6)
`kind: deliverable`

Targets:
- `crates/gbackup/src/backup.rs`
- `crates/gbackup/src/backup/tests.rs`
- `crates/gbackup/src/lib.rs`

**Research context:**
- Python's `hub_backup` (hub_backup/cli.py lines 208-310) defaults the
  output to `~/.gobby/backups/hub/<%Y%m%dT%H%M%SZ>`, and requires the path
  to be absent (`require_absent_output_path`, _integrity.py lines 165-174).
  It stages beside the final path (`create_staging_directory`, 147-162),
  writes the manifest, and publishes with `publish_staged_backup`
  (177-195), which fsyncs the tree and renames.
- `_emit_result` (hub_backup/cli.py lines 919-940) prints JSON with keys
  `manifest`, `backup_root`, `created_at`, `epoch_id`, `artifacts` (a
  count) and `stores` (each with `archive_verified` and `restore_verified`
  as booleans). It never prints the DSN or the Qdrant key.
- Decision Records 4, 5, 7 and 13 apply.

Implementation:
- `run_backup(args, env)` for an interactive live run:
  1. Take the run lock with `Refuse`.
  2. Sweep stale staging.
  3. Resolve the target and run preflight.
  4. Require the output path to be absent, and stage in its parent.
  5. Capture in Python's order: PostgreSQL, Qdrant, FalkorDB, files_home,
     audit logs, then machine identity.
  6. Record `volumes` as skipped for live mode.
  7. Hash every artifact, write the manifest, fsync the tree and rename the
     staging directory to the final path.
- Any error leaves no final directory. Staging is removed and the exit code
  is 1.
- `--json` prints Python's payload with `backup_root` and a null
  `epoch_id`. Text output matches Python's summary lines.
- The daemon and the datastores are never stopped. No maintenance claim is
  taken for a live run.
- `backup_once(ctx) -> Result<BackupOutcome>` is the reusable core for the
  scheduled (3.2) and cold (6.1) flows.

Planned verification:
`cargo nextest run -p gobby-backup -E 'test(backup)'`, then `cargo clippy -p
gobby-backup --all-targets -- -D warnings`. On the isolated test hub, run
`GOBBY_TEST_PROTECT=1 GOBBY_TEST_ALLOW_DOCKER=1 gbackup backup --output
<tmp>/b1 --json` against the test DSN, then `gbackup verify <tmp>/b1` after
P4 lands.

**Acceptance:**

- 2.7.1 - With every store faked, a live backup publishes a directory whose
  manifest lists every artifact with a correct hash, and whose `volumes`
  record is skipped for live mode. test:
  `crates/gbackup/src/backup/tests.rs::live_backup_publishes_complete_manifest`.
- 2.7.2 - A failure in any store leaves no final directory and no staging
  directory, and exits 1. test:
  `crates/gbackup/src/backup/tests.rs::store_failure_leaves_nothing_published`.
- 2.7.3 - An existing `--output` path is refused before any capture. test:
  `crates/gbackup/src/backup/tests.rs::existing_output_is_refused`.
- 2.7.4 - `--json` prints Python's keys with a null `epoch_id`, and no
  output contains the DSN password or the Qdrant key. test:
  `crates/gbackup/src/backup/tests.rs::json_payload_matches_python_keys_without_secrets`.
- 2.7.5 - A live run never invokes `gobby stop`, `docker stop` or the
  maintenance claim. test:
  `crates/gbackup/src/backup/tests.rs::live_run_never_stops_services`.

## P3: Retention and the Scheduled Backup
`kind: framing`

**Goal:** the nightly timer's `gbackup backup --scheduled` runs once per
machine and day, keeps 7 good live backups, and records its outcome.

### 3.1 Retention [category: code] (depends: 2.7)
`kind: deliverable`

Targets:
- `crates/gbackup/src/retention.rs`
- `crates/gbackup/src/retention/tests.rs`
- `crates/gbackup/src/lib.rs`

**Research context:** Python has no retention; backups accumulate under
`~/.gobby/backups/hub/`. Decision Record 6 sets the policy. A live backup
is about 8.7 GB, so 7 of them take about 61 GB. Directory names use
`%Y%m%dT%H%M%SZ`. `integrity_ok_by_size` (2.1) is the integrity predicate,
and `Manifest::is_gbackup_written` separates gbackup's backups from
Python's.

Implementation:
- `apply_retention(root)` runs under the run lock, with the keep count
  fixed at 7.
- Candidates are real directories directly under the root, never symlinks
  or staging directories, whose manifest parses as a gbackup-written live
  backup. They are ordered by the manifest's `created_at`, newest first.
- Walking from the newest, it counts integrity-ok backups. Once 7 are
  counted, every older candidate is deleted, whether it is intact or not.
  A failing candidate newer than the seventh good one is kept for the
  operator.
- With fewer than 7 integrity-ok backups, nothing is deleted.
- These are never touched: directories without a parseable manifest, cold
  backups, Python-written backups, and anything that is not a candidate.
- Deletion re-checks that the entry is not a symlink, then removes the
  tree. It returns the deleted paths for the run log.

Planned verification:
`cargo nextest run -p gobby-backup -E 'test(retention)'`, then `cargo clippy
-p gobby-backup --all-targets -- -D warnings`.

**Acceptance:**

- 3.1.1 - With nine integrity-ok live backups, the two oldest are deleted.
  test: `crates/gbackup/src/retention/tests.rs::keeps_seven_newest_integrity_ok`.
- 3.1.2 - A failing live backup newer than the seventh good one is kept,
  and one older than it is deleted. test:
  `crates/gbackup/src/retention/tests.rs::failing_backup_kept_only_inside_window`.
- 3.1.3 - Old cold, Python-written, unparseable, symlinked and foreign
  directories are never deleted. test:
  `crates/gbackup/src/retention/tests.rs::never_deletes_non_candidates`.
- 3.1.4 - With fewer than 7 integrity-ok backups, nothing is deleted. test:
  `crates/gbackup/src/retention/tests.rs::fewer_than_seven_good_deletes_nothing`.

### 3.2 Scheduled backup [category: code] (depends: 3.1)
`kind: deliverable`

Targets:
- `crates/gbackup/src/scheduled.rs`
- `crates/gbackup/src/scheduled/tests.rs`
- `crates/gbackup/src/lib.rs`

**Research context:** Decision Records 3, 6, 7 and 8 apply. 1.2 provides
the jitter, the `Skip` lock policy and `LastRun`. 2.7 provides
`backup_once`, and 3.1 provides retention. Timer output appends to
`~/.gobby/logs/gbackup.log` (7.1, 7.2), so each run prints one UTC-stamped
summary line on stdout, and errors on stderr.

Implementation:
- `run_scheduled_backup(env)` runs in this order:
  1. Read the machine ID and sleep the jitter.
  2. Take the lock with `Skip`. A held lock records `skipped_locked` and
     exits 0.
  3. Sweep stale staging.
  4. Run `backup_once` live, to the default output path.
  5. Apply retention.
  6. Write the `backup` record.
- A capture failure records `error` and exits 1, and retention does not
  run. A retention failure after a published backup records `error`, names
  the published directory, and exits 1.
- The record is written on every path, including a panic caught at the
  entry point. A failure to record follows 1.2: stderr carries the
  diagnostic and the run exits 1.

Planned verification:
`cargo nextest run -p gobby-backup -E 'test(scheduled)'`, then `cargo clippy
-p gobby-backup --all-targets -- -D warnings`.

**Acceptance:**

- 3.2.1 - With the lock held, the run records `skipped_locked`, captures
  nothing and exits 0. test:
  `crates/gbackup/src/scheduled/tests.rs::held_lock_records_skipped_locked`.
- 3.2.2 - A successful run records `ok` with the backup directory, after
  retention ran. test:
  `crates/gbackup/src/scheduled/tests.rs::success_records_ok_after_retention`.
- 3.2.3 - A failed capture records `error` with its message, keeps the
  existing `verify` record, skips retention and exits 1. test:
  `crates/gbackup/src/scheduled/tests.rs::capture_failure_records_error_and_skips_retention`.
- 3.2.4 - The jitter sleep finishes before the lock is taken. test:
  `crates/gbackup/src/scheduled/tests.rs::jitter_sleeps_before_lock`.
- 3.2.5 - A retention failure records `error` naming the published backup.
  test:
  `crates/gbackup/src/scheduled/tests.rs::retention_failure_records_error_with_backup_dir`.

## P4: Restore Verification
`kind: framing`

**Goal:** `gbackup verify` proves each store of a backup restores, in
disposable containers or scratch collections, and records the result in
the manifest. The weekly timer runs it on the newest unverified nightly.

### 4.1 Disposable containers and PostgreSQL verification [category: code] (depends: 3.2)
`kind: deliverable`

Targets:
- `crates/gbackup/src/verify.rs`
- `crates/gbackup/src/verify/containers.rs`
- `crates/gbackup/src/verify/containers/tests.rs`
- `crates/gbackup/src/verify/postgres.rs`
- `crates/gbackup/src/verify/postgres/tests.rs`
- `crates/gbackup/src/lib.rs`

**Research context:** Python's verify module (_verify.py) sets the
behavior.
- Images: `gobby-postgres-local:18-pgsearch` and `falkordb/falkordb:latest`
  (lines 49-50).
- Labels: `io.gobby.disposable=true` and `io.gobby.run-id=<nonce>` (64-65).
  Containers with a `com.docker.compose.` label are refused (66).
- Timeouts (68-73): Docker 600 s, removal 60 s, probe 30 s, PostgreSQL
  ready 120 s, FalkorDB ready 60 s, and a 1 s poll.
- `_start_scratch_postgres` (141-169) runs the image with a random
  superuser password and `shared_preload_libraries=pg_search,pgaudit`.
  Python passes the password in argv.
- `_replay_globals` (191-209) pipes the globals into `psql` without
  `ON_ERROR_STOP`, because a fresh cluster already has the bootstrap roles.
  The role check proves the outcome.
- `_check_roles` (212-252) requires every expected role, with matching
  `rolsuper` and `rolcanlogin`. It skips the bits of the bootstrap
  `postgres` role, and reports a missing managed principal by name instead
  of failing.
- `_restore_dump` (255-285) runs `createdb gobby`, then `pg_restore`
  without `--no-owner`, so ownership is proven too.
- `_check_row_counts` (288-304) and `_check_schema_object_counts` (307-350)
  compare exactly. Scratch `psql` sessions set `PGOPTIONS=-c
  event_triggers=off` (line 60).
- The method is `scratch-pg-restore+roles+rows+schema-counts`.
- `_remove_container` (757-775) and `_has_disposal_capability` (778-817)
  re-inspect the exact ID, name and run label before removal. They refuse
  unlabeled, mismatched or Compose-managed containers.

Implementation:
- `verify.rs` declares the verify modules.
- `Disposable::start(docker, spec)` names the container
  `gbackup-verify-<store>-<nonce>` and sets both labels. The random
  password goes into the Docker child's environment and is passed as a bare
  `-e NAME`, so it never enters argv.
- `Disposable::remove` inspects the ID. It runs `docker rm -f <id>` only
  when the name, the disposable label and the nonce all match and no
  Compose label exists. Otherwise it warns and leaves the container. Every
  verify path calls it, including errors.
- `verify_postgres(ctx, dir, manifest)` runs in this order:
  1. Start the scratch container and wait up to 120 s for `pg_isready`.
  2. Replay `postgres/globals.sql`, and check roles against
     `details.roles`.
  3. Create `gobby` and run `pg_restore` from `postgres/gobby.dump`.
  4. Compare `row_count_probes` and `details.schema_object_counts`
     exactly.

  It returns a verified state with Python's method and a UTC timestamp, or
  the failure.

Planned verification:
`cargo nextest run -p gobby-backup -E 'test(verify::containers) |
test(verify::postgres)'`, then `cargo clippy -p gobby-backup --all-targets
-- -D warnings`.

**Acceptance:**

- 4.1.1 - Removal refuses an unlabeled container, a container with a
  different nonce and a Compose-labelled container. test:
  `crates/gbackup/src/verify/containers/tests.rs::removal_refuses_foreign_containers`.
- 4.1.2 - The scratch password never appears in any recorded Docker argv.
  test:
  `crates/gbackup/src/verify/containers/tests.rs::scratch_password_stays_out_of_argv`.
- 4.1.3 - The scratch container is removed after a failed restore. test:
  `crates/gbackup/src/verify/containers/tests.rs::scratch_removed_after_failure`.
- 4.1.4 - Verification fails on a row-count mismatch, a schema-count
  mismatch and a missing non-managed role. It reports a missing managed
  principal without failing. test:
  `crates/gbackup/src/verify/postgres/tests.rs::verify_compares_probes_schema_and_roles`.

### 4.2 Qdrant and FalkorDB verification [category: code] (depends: 4.1)
`kind: deliverable`

Targets:
- `crates/gbackup/src/verify.rs`
- `crates/gbackup/src/verify/qdrant.rs`
- `crates/gbackup/src/verify/qdrant/tests.rs`
- `crates/gbackup/src/verify/falkordb.rs`
- `crates/gbackup/src/verify/falkordb/tests.rs`

**Research context:**
- `verify_qdrant_restore` (_verify.py lines 392-452) recovers each snapshot
  into the hub Qdrant as `hub_backup_verify_<name>` by upload (455-466). It
  compares the count and digest, and deletes the scratch collection in
  `finally` (469-484). The method is
  `qdrant-scratch-recover+point-content-digest`.
- `verify_falkordb_restore` (499-548) runs these steps:
  1. Copy the RDB into a scratch directory, bind-mounted at
     `/var/lib/falkordb/data`.
  2. Start a scratch container (551-579). The password goes in through
     `REDIS_ARGS` and `GOBBY_FALKORDB_PASSWORD`.
  3. Wait up to 60 s for readiness (582-591).
  4. Compare the graph list and per-graph counts (594-628).

  The method is `falkordb-scratch-rdb-load+graph-counts`.
- Decision Record 13 defines exact and range comparisons.

Implementation:
- `verify_qdrant` first deletes any scratch collection with the same name
  left by a crashed run.
  - It uploads each snapshot with `priority=snapshot`.
  - It compares the count: equal to `points`, or within
    `points_min..=points_max`.
  - It compares the digest when `content_sha256` is not null.
  - The scratch collection is deleted on every path.
- `verify_falkordb` works in a 0700 scratch directory inside a `Staging`
  directory (1.2), so a crashed run's leftovers are swept.
  - The passwords go through the child environment, as in 4.1.
  - It waits for `PING` and for loading to finish.
  - It compares `GRAPH.LIST`, each graph's counts and `DBSIZE` through
    `falkordb_matches` (2.4).
  - The container and the directory are removed on every path.

Planned verification:
`cargo nextest run -p gobby-backup -E 'test(verify::qdrant) |
test(verify::falkordb)'`, then `cargo clippy -p gobby-backup --all-targets
-- -D warnings`.

**Acceptance:**

- 4.2.1 - Against a scripted HTTP server, a stale scratch collection is
  deleted first. Each snapshot is uploaded, compared and deleted. test:
  `crates/gbackup/src/verify/qdrant/tests.rs::uploads_compares_and_deletes_scratch`.
- 4.2.2 - These fail verification: a count outside the recorded range, and
  a digest mismatch. A null digest checks the count alone. The scratch
  collection is deleted after a failure. test:
  `crates/gbackup/src/verify/qdrant/tests.rs::range_and_digest_rules`.
- 4.2.3 - FalkorDB verification compares the graph list, counts and
  `DBSIZE` through `falkordb_matches`, optional graphs included. It fails on a mismatch and removes the
  container either way. test:
  `crates/gbackup/src/verify/falkordb/tests.rs::compares_graphs_and_removes_container`.

### 4.3 files_home restore primitive and archive verification [category: code] (depends: 4.2)
`kind: deliverable`

Targets:
- `crates/gbackup/src/files_restore.rs`
- `crates/gbackup/src/files_restore/tests.rs`
- `crates/gbackup/src/verify.rs`
- `crates/gbackup/src/verify/files.rs`
- `crates/gbackup/src/verify/files/tests.rs`
- `crates/gbackup/src/lib.rs`

**Research context:**
- `restore_files_home_from_archive` (files_home.py lines 399-415) and
  `_restore_body` (418-481) run these steps:
  1. Re-hash the archive against the expected SHA-256.
  2. Preflight the member graph, and refuse anything other than files and
     directories.
  3. Require free space for the sum of the file sizes, and an owner
     files_home.
  4. Create directories as files_home descendants, then publish each file.
- `_publish_member` (484-522):
  1. Copies to a temp file and fsyncs it.
  2. Checks the copied size against the declared size.
  3. Replaces the target durably through a hidden `.<name>.restore-tmp`
     locator.

  Existing files are replaced, and files missing from the archive stay.
- `verify_files_home_archive` (380-396) only checks the members. gbackup
  restores into scratch and compares the inventory recorded at capture
  (2.5).

Implementation:
- The files restore is two calls, shared by verify (scratch) and restore
  (files_home, 5.2). Both work in a destination directory handle.
  - `preflight_files_restore(archive, dest_dir, expected_sha256)` runs
    steps 1 to 3 without writing, and returns a checked plan that holds
    the open archive. The member graph follows Python's
    `preflight_archive_graph` (files_home.py): duplicate members, file
    and directory prefix conflicts, the member and byte limits, and
    non-file members are refused. A member name that is absolute, or has
    an empty, `.` or `..` component, is refused too.
  - `restore_files_into(plan)` writes.
  - Directories are created one component at a time with `mkdirat`. A
    symlinked component fails, and is never followed.
  - Each file is copied to `.<name>.restore-tmp`, opened with `O_CREAT`,
    `O_EXCL` and `O_NOFOLLOW` in its parent's handle. The copy is exactly
    the member's size, then fsync, `renameat` over the name, and a parent
    fsync.
  - A temp file is removed on every failure.
- `verify_files(ctx, dir, manifest)` restores into a scratch directory
  inside a `Staging` directory. It compares path, type, size and SHA-256
  against `details.inventory`, then removes the scratch. The method is
  `files-home-scratch-restore+inventory`.

Planned verification:
`cargo nextest run -p gobby-backup -E 'test(files_restore) |
test(verify::files)'`, then `cargo clippy -p gobby-backup --all-targets --
-D warnings`.

**Acceptance:**

- 4.3.1 - A restore into a directory reproduces the archive. It replaces
  existing files and keeps unrelated ones. test:
  `crates/gbackup/src/files_restore/tests.rs::restore_reproduces_archive_and_keeps_unrelated`.
- 4.3.2 - `preflight_files_restore` refuses each of these before any file
  is written:
  - a hash mismatch;
  - a non-file member;
  - a duplicate, prefix-conflicting or unsafe member name;
  - insufficient space.

  A symlinked destination component fails without being followed. test:
  `crates/gbackup/src/files_restore/tests.rs::refusals_precede_writes`.
- 4.3.3 - A member whose copied size disagrees with its declared size fails
  and leaves no temp file. test:
  `crates/gbackup/src/files_restore/tests.rs::size_mismatch_leaves_no_temp`.
- 4.3.4 - `verify_files` passes for a captured archive, fails when the
  recorded inventory disagrees, and removes its scratch either way. test:
  `crates/gbackup/src/verify/files/tests.rs::verify_files_compares_inventory`.

### 4.4 Verify command [category: code] (depends: 4.3)
`kind: deliverable`

Targets:
- `crates/gbackup/src/verify.rs`
- `crates/gbackup/src/verify/tests.rs`
- `crates/gbackup/src/lib.rs`

**Research context:** Python has no standalone verify. Every backup ran
`_verify_stores` inline (hub_backup/cli.py line 656), with the daemon
stopped. gbackup moves restore verification out of the nightly. Decision
Records 3, 7, 8 and 14 set the selection, locking, recording and refusal
rules.

Implementation:
- `run_verify(args, env)`:
  1. Take the lock: `Refuse` interactively, `Wait` when scheduled.
  2. Resolve the target and run preflight.
  3. Select the backup: `DIR` when given; otherwise the newest live
     backup that passes `integrity_ok_by_size` and whose four live stores
     are not all `restore_verified`.
  4. Refuse a manifest that gbackup did not write.
  5. Run `check_artifact_set` (2.1), then `verify_artifacts` over every
     artifact. A failure stops before any store is verified.
- Then it verifies `postgres`, `qdrant`, `falkordb` and `files`, and
  continues after a store fails so that every failure is reported.
  - Each store's `restore_verified` becomes verified, or unverified with
    the attempted method and timestamp.
  - A live `volumes` record stays skipped.
  - The manifest is rewritten through `write_manifest`.
- No candidate means status `ok` with `nothing to verify`.
- `--scheduled` adds the jitter and writes the `verify` record. A failure
  to record follows 1.2.
- `--json` prints `backup_root`, each store's flags, and `errors`.
- Verify never takes the maintenance claim and never stops anything.
  Scratch Qdrant collections live on the hub Qdrant, as in Python.

Planned verification:
`cargo nextest run -p gobby-backup -E 'test(verify)'`, then `cargo clippy
-p gobby-backup --all-targets -- -D warnings`. On the isolated test hub, run
`GOBBY_TEST_PROTECT=1 GOBBY_TEST_ALLOW_DOCKER=1 gbackup verify <tmp>/b1
--json` on the 2.7 backup.

**Acceptance:**

- 4.4.1 - Verifying a backup marks each store `restore_verified` and
  rewrites the manifest, which still validates. test:
  `crates/gbackup/src/verify/tests.rs::verify_marks_stores_and_rewrites_manifest`.
- 4.4.2 - A failing store is marked unverified, the other stores are still
  verified, every error is listed, and the exit code is 1. test:
  `crates/gbackup/src/verify/tests.rs::store_failure_is_isolated_and_reported`.
- 4.4.3 - A Python-written manifest is refused before any container
  starts. test:
  `crates/gbackup/src/verify/tests.rs::refuses_python_written_manifest`.
- 4.4.4 - An artifact-set defect or a hash mismatch fails before any
  store is verified. test:
  `crates/gbackup/src/verify/tests.rs::integrity_failure_stops_before_stores`.
- 4.4.5 - Selection picks the newest integrity-ok live backup that is not
  yet verified, and reports `nothing to verify` when there is none. test:
  `crates/gbackup/src/verify/tests.rs::selects_newest_unverified_live_backup`.
- 4.4.6 - Scheduled verify waits for a held lock, then writes its record
  and leaves the `backup` record alone. test:
  `crates/gbackup/src/verify/tests.rs::scheduled_verify_waits_and_records`.

## P5: Restore
`kind: framing`

**Goal:** `gbackup restore` puts files_home, PostgreSQL, Qdrant and
FalkorDB back from a fully verified backup, with the daemon stopped, and
refuses before its first write whenever any step would fail.

### 5.1 Qdrant and FalkorDB restore [category: code] (depends: 4.4)
`kind: deliverable`

Targets:
- `crates/gbackup/src/restore.rs`
- `crates/gbackup/src/restore/qdrant.rs`
- `crates/gbackup/src/restore/qdrant/tests.rs`
- `crates/gbackup/src/restore/falkordb.rs`
- `crates/gbackup/src/restore/falkordb/tests.rs`
- `crates/gbackup/src/services_lock.rs`
- `crates/gbackup/src/services_lock/tests.rs`
- `crates/gbackup/src/lib.rs`

**Research context:**
- Python restores neither Qdrant nor FalkorDB. Decision D3 (a) adds both.
- `managed_services_lock` (managed_services_lock.py lines 61-105) flocks
  `~/.gobby/managed-services.lock` and writes the holder (`_write_holder`,
  136-147). The installers take it around compose operations.
- FalkorDB runs with `REDIS_ARGS=--requirepass ... --save 3600 1 300 100`
  and no append-only file (docker-compose.services.yml lines 12-28), so it
  loads `dump.rdb` at start.
- A Qdrant snapshot upload with `priority=snapshot` replaces the
  collection's data. 4.2 already uploads snapshots into scratch
  collections.
- Decision Record 13 defines exact and range comparisons.

Implementation:
- `ServicesLock::acquire(home, operation)` ports Python's lock file and
  holder format. A held lock fails, naming the holder.
- `preflight_qdrant(client, manifest, clean)`: without `--clean`, it
  refuses when any manifest collection already exists on the target.
- `restore_qdrant(client, dir, manifest, clean)`:
  - With `--clean`, it first deletes every collection without the scratch
    prefix.
  - It uploads each snapshot under its original name with
    `priority=snapshot`.
  - It checks each count, exactly or by range.
- `preflight_falkordb(docker, target, clean)`: without `--clean`, it
  refuses when `DBSIZE` is above 0.
- `restore_falkordb` takes the caller's held `ServicesLock` guard, and
  never acquires the lock itself:
  1. Arm the start cleanup, then `docker stop` the FalkorDB container.
     A stop that exits nonzero, outlives its Docker timeout (1.3) or
     leaves the container running fails the restore before the copy.
  2. `docker cp` the RDB to `/var/lib/falkordb/data/dump.rdb`.
  3. `docker start` the container.
  4. Wait up to 60 s for `PING` and for loading to finish.
  5. Compare the graph list and counts through `falkordb_matches` (2.4).

  Every path after the stop attempt runs the start and the readiness
  wait, a failed stop and a failed copy included. The restore keeps the
  original error, and also reports a failed start or readiness wait,
  naming the container. The guard stays held until the cleanup ends.
  This restarts the service. It never rolls back restored data.

Planned verification:
`cargo nextest run -p gobby-backup -E 'test(restore::qdrant) |
test(restore::falkordb) | test(services_lock)'`, then `cargo clippy -p
gobby-backup --all-targets -- -D warnings`.

**Acceptance:**

- 5.1.1 - Without `--clean`, a Qdrant target holding a manifest collection
  is refused before any upload. test:
  `crates/gbackup/src/restore/qdrant/tests.rs::existing_collection_refused_without_clean`.
- 5.1.2 - With `--clean`, non-scratch collections are deleted. Each
  snapshot is then uploaded under its original name and its count checked.
  test:
  `crates/gbackup/src/restore/qdrant/tests.rs::clean_restore_replaces_collections`.
- 5.1.3 - Without `--clean`, a non-empty FalkorDB target is refused. test:
  `crates/gbackup/src/restore/falkordb/tests.rs::non_empty_target_refused_without_clean`.
- 5.1.4 - FalkorDB restore stops, copies and starts while the caller
  holds the services lock, then compares counts. test:
  `crates/gbackup/src/restore/falkordb/tests.rs::restore_runs_under_held_services_lock`.
- 5.1.5 - The services lock writes Python's holder format and refuses
  while another process holds it. test:
  `crates/gbackup/src/services_lock/tests.rs::holder_format_matches_python`.
- 5.1.6 - A stop that exits nonzero, a stop that times out, a container
  still running after the stop, and a copy that fails each still attempt
  the start and the readiness wait. The restore fails with the original
  error. When the start or the wait also fails, it reports that failure
  too, naming the container. test:
  `crates/gbackup/src/restore/falkordb/tests.rs::every_path_after_stop_attempt_restarts_container`.

### 5.2 Restore command [category: code] (depends: 5.1)
`kind: deliverable`

Targets:
- `crates/gbackup/src/restore.rs`
- `crates/gbackup/src/restore/tests.rs`
- `crates/gbackup/src/restore/postgres.rs`
- `crates/gbackup/src/restore/postgres/tests.rs`
- `crates/gbackup/src/lib.rs`

**Granularity:** one leaf with eight acceptance items. Restore is one
destructive state machine. Its gate, its preflight and its steps cannot
ship apart, or a restore could write without its refusals.

**Research context:**
- `restore_hub_backup` (hub_backup/cli.py lines 313-380):
  1. Refuses while the daemon runs (`Stop the daemon first: gobby stop`).
  2. Loads the manifest and verifies the artifacts.
  3. Requires PostgreSQL archive and restore verification.
  4. Asks `Restore hub PostgreSQL data into the explicit target?` unless
     `--yes` is given.
  5. Under the maintenance claim, runs `restore_hub_files`,
     `restore_postgres_globals`, `restore_postgres_backup` and
     `reconcile_restored_principals`.
- `restore_postgres_globals` (_stores.py lines 238-301) strips passwords,
  makes each `CREATE ROLE` idempotent, and replays with `psql -v
  ON_ERROR_STOP=1`.
- `restore_postgres_backup` (postgres_backup.py lines 100-151):
  1. Requires the managed target, checks the SHA-256 and runs `pg_restore
     --list`.
  2. With `--clean`, runs `_reset_postgres_database` (265-288). That
     refuses the database `postgres`, connects to `postgres` with the
     target's credentials, and runs `DROP DATABASE IF EXISTS ... WITH
     (FORCE)` and `CREATE DATABASE ... OWNER <DSN user>`.
  3. `_run_pg_restore` (224-250) runs `docker exec -i -e PGOPTIONS=-c
     event_triggers=off <container> pg_restore --no-owner`.
  4. `_run_post_restore_probes` (314-345) requires `pg_search`, `pgaudit`
     and `pgcrypto`.

  Python prints the target DSN redacted.
- Decision Record 9 and D3 (a) set the gate. D1 removes the epoch release.
- The final login guard (baseline.sql lines 6743-6792) refuses
  connections while an unreleased maintenance epoch row exists, unless the
  session's `gobby.maintenance_epoch` setting matches it. Only a backup
  taken during a pre-D1 epoch carries one, and its manifest records a
  non-null `epoch_id`. Python released that epoch after `pg_restore`
  (postgres_backup.py lines 133-138). gbackup has no release (D1), so such
  a restore would lock every login out.

Implementation:
- `run_restore(args, env)` runs in this order:
  1. Take the lock with `Refuse`, then `maintenance_claim`. The claim
     fails while the daemon runs.
  2. Run `resolve_restore_target(env, url)` (1.3) and preflight.
  3. Read the manifest (Python-written manifests are accepted). Refuse a
     non-null `epoch_id`, naming D1. Run `check_artifact_set` (2.1), then
     `verify_artifacts` over every artifact.
  4. Gate: `postgres`, `qdrant`, `falkordb` and `files` must each record
     `archive_verified` and `restore_verified`. Otherwise refuse, naming the
     stores and suggesting `gbackup verify DIR`.
  5. Run every refusal check before any write:
     - `preflight_files_restore` (4.3): the hash, the member graph, space
       and ownership;
     - `pg_restore --list` in the target container;
     - a `--clean` target that is not `postgres`;
     - Qdrant and FalkorDB (5.1), with the Qdrant URL read from the target
       database's config;
     - `ServicesLock::acquire` (5.1). A held lock refuses here. The guard
       is held through the FalkorDB restart.
  6. Confirm. `--yes` skips the prompt. Otherwise a terminal prompt asks
     `Restore hub PostgreSQL, Qdrant, FalkorDB and files_home into the
     explicit target? [y/N]`. Without a terminal, it refuses with exit 1.
  7. Restore files_home with `restore_files_into` (4.3) and the manifest's
     `files-home` SHA-256.
  8. Replay globals: `restorable_globals` strips `PASSWORD` clauses and
     makes each `CREATE ROLE` idempotent, as Python does. The result is
     piped to `psql -v ON_ERROR_STOP=1` through `docker exec`.
  9. With `--clean`, reset the database as Python does, in a `pg::connect`
     session (1.3). `reset_database(dsn)` takes the target DSN alone.
  10. Run `pg_restore --no-owner` with `PGOPTIONS=-c event_triggers=off`.
  11. Probe the three extensions.
  12. Restore Qdrant, then FalkorDB (5.1) with the held services-lock
      guard.
  13. Run `drain_ephemeral_principals` (2.2).
  14. Print the redacted target.
- A failure after the first write exits 1 and lists the completed steps.
  There is no rollback, as in Python.

Planned verification:
`GOBBY_TEST_PROTECT=1 GBACKUP_POSTGRES_TEST_DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test cargo nextest run -p gobby-backup -E 'test(restore)'`,
then `cargo clippy -p gobby-backup --all-targets -- -D warnings`. A live
restore is out of scope for the leaf: it would replace the test hub.

**Acceptance:**

- 5.2.1 - A held daemon claim refuses before the manifest is read. test:
  `crates/gbackup/src/restore/tests.rs::daemon_claim_refuses_restore`.
- 5.2.2 - A restored store without `restore_verified` is refused, named,
  and pointed at `gbackup verify`. test:
  `crates/gbackup/src/restore/tests.rs::gate_requires_every_restored_store_verified`.
- 5.2.3 - Every preflight refusal happens before the first write: a
  non-null `epoch_id`, an artifact-set defect (a forged skipped
  `postgres` store without `postgres-globals` included), a late invalid
  tar member and a held services lock among them. The fake harness records no
  mutation. test:
  `crates/gbackup/src/restore/tests.rs::preflight_refusals_precede_mutation`.
- 5.2.4 - Without a terminal and without `--yes`, restore refuses, and
  `--yes` skips the prompt. test:
  `crates/gbackup/src/restore/tests.rs::confirmation_rules`.
- 5.2.5 - The steps run in this order:
  1. files;
  2. globals;
  3. the reset, under `--clean`;
  4. `pg_restore --no-owner` with `PGOPTIONS`;
  5. probes;
  6. Qdrant;
  7. FalkorDB;
  8. the drain.

  test: `crates/gbackup/src/restore/tests.rs::restore_runs_steps_in_order`.
- 5.2.6 - `restorable_globals` strips passwords and makes `CREATE ROLE`
  idempotent, and the replay uses `ON_ERROR_STOP`. test:
  `crates/gbackup/src/restore/postgres/tests.rs::globals_are_password_free_and_idempotent`.
- 5.2.7 - `--clean` refuses a target database named `postgres`. The reset
  recreates the database, owned by the DSN user. The `serial_db` test
  never resets `gobby_test`:
  - it creates a uniquely named `gbackup_reset_<pid>_<nanos>` database on
    the admitted server, and resets that one;
  - a guard drops it on every path;
  - `gobby_test`'s OID is the same before and after.

  test:
  `crates/gbackup/src/restore/postgres/tests.rs::serial_db_clean_reset_recreates_disposable_database`.
- 5.2.8 - A missing extension fails the probe and is named. No output
  contains the DSN password. test:
  `crates/gbackup/src/restore/postgres/tests.rs::probe_failure_names_extension_without_secrets`.

## P6: Cold Backup
`kind: framing`

**Goal:** an operator can take a full cold backup that adds the managed
Docker volume tars, with the daemon stopped, verified inline before it is
published.

### 6.1 Cold backup with volume archives [category: code] (depends: 5.2)
`kind: deliverable`

Targets:
- `crates/gbackup/src/cold.rs`
- `crates/gbackup/src/cold/tests.rs`
- `crates/gbackup/src/stores.rs`
- `crates/gbackup/src/stores/volumes.rs`
- `crates/gbackup/src/stores/volumes/tests.rs`
- `crates/gbackup/src/lib.rs`

**Research context:**
- `_archive_volumes` (cli.py lines 550-582) stops the services through
  Compose, refuses when they did not stop, restarts them in `finally`, and
  fails when the restart fails. Its `try` starts after the stop, so a
  failed or partial stop (566-569) restarts nothing. gbackup arms the
  start before the first stop.
- `tar_volumes` (630-673):
  - inventories each volume by streaming a tar out of a read-only `alpine`
    container into `tar_stream_inventory`;
  - writes `volumes/<volume>.tar.gz` with `tar czf` in a second `alpine`
    container, with a 3600 s timeout.

  The artifact is `volume-<volume>`, the method `tar-archive+sha256`, and
  the volumes come from the target (1.3).
- Python stopped the daemon for every backup. The managed containers are
  listed in `container_restart.py` lines 16-18.
- Decision Records 4, 7 and 13 apply: cold drains, and the operator starts
  the daemon.

Implementation:
- `run_cold_backup(args, env)` runs in this order:
  1. Take the lock with `Refuse`, then `maintenance_claim`. While the
     daemon runs, the claim fails with `stop the daemon with gobby stop
     first; start it again with gobby start afterwards`.
  2. Resolve the target and run preflight, then stage.
  3. Run the logical captures with `Mode::Cold`, so PostgreSQL drains
     first (2.2). Then capture Qdrant, FalkorDB, files_home, the audit logs
     and the machine identity.
  4. Take `ServicesLock` (5.1) and arm the start cleanup. Then `docker
     stop` the three managed containers and require each to report not
     running. A stop that exits nonzero, outlives its Docker timeout
     (1.3) or leaves a container running fails the run before any
     archive.
  5. Inventory and archive each volume as Python does.
  6. The cleanup runs on every path after the first stop attempt. For
     each managed container it runs `docker start` and waits up to
     60 s, as 5.1 does, for the container to report running. A failed
     start or wait never skips the remaining containers. The run keeps
     the original error and also reports each failed start or wait,
     naming the container. With no earlier error, a failed start or
     wait fails the run, and nothing is published. `ServicesLock` is
     released after the cleanup ends.
  7. Run the inline verify (6.2), write the manifest and publish.
- The final line tells the operator to run `gobby start`.
- Cold backups go to the same default root, and retention never prunes
  them (3.1).

Planned verification:
`cargo nextest run -p gobby-backup -E 'test(cold) | test(stores::volumes)'`,
then `cargo clippy -p gobby-backup --all-targets -- -D warnings`.

**Acceptance:**

- 6.1.1 - A held daemon claim refuses before any capture, with the `gobby
  stop` message. test:
  `crates/gbackup/src/cold/tests.rs::daemon_claim_refuses_cold_backup`.
- 6.1.2 - Under the services lock, containers are stopped, volumes are
  archived with inventories, and containers are started, in that order.
  The `volumes` record is archived and not skipped. test:
  `crates/gbackup/src/cold/tests.rs::stop_archive_start_order`.
- 6.1.3 - Each stop failure refuses the run before any archive: a stop
  that exits nonzero, one that times out, a container that keeps running,
  and a nonzero stop on the second container after the first stopped.
  Every managed container is started again, and the run reports the stop
  error. test:
  `crates/gbackup/src/cold/tests.rs::failed_stop_restarts_every_container`.
- 6.1.4 - A failed start, or a readiness wait that ends without the
  container running, fails the run even after the archives succeeded.
  The remaining containers are still started, and nothing is published.
  test: `crates/gbackup/src/cold/tests.rs::failed_start_or_wait_fails_run`.
- 6.1.5 - The PostgreSQL capture runs in cold mode, so the drain runs. test:
  `crates/gbackup/src/cold/tests.rs::cold_capture_drains_principals`.
- 6.1.6 - When a stop fails and a start or a readiness wait also fails,
  the remaining containers are still started, and the run reports the
  stop error and each failed start or wait. test:
  `crates/gbackup/src/cold/tests.rs::stop_and_restart_failures_all_reported`.

### 6.2 Volume verification and cold inline verify [category: code] (depends: 6.1)
`kind: deliverable`

Targets:
- `crates/gbackup/src/verify.rs`
- `crates/gbackup/src/verify/tests.rs`
- `crates/gbackup/src/verify/volumes.rs`
- `crates/gbackup/src/verify/volumes/tests.rs`
- `crates/gbackup/src/cold.rs`
- `crates/gbackup/src/cold/tests.rs`
- `crates/gbackup/Cargo.toml`
- `Cargo.lock`

**Research context:**
- `verify_volume_archives` (_verify.py lines 649-678) extracts each
  archive into scratch (681-698), and compares it with the source
  inventory (701-714). The method is
  `tar-extract+source-content-inventory`.
- `_tar_inventory` (_content.py lines 99-134) records path, type, size and
  SHA-256 per member. It refuses symlinks, hard links and special members.
- Python verified every backup inline. The weekly verify (4.4) selects live
  backups only, so cold backups are verified when they are taken.
- `flate2` is in `Cargo.lock`.

Implementation:
- `verify_volumes(dir, manifest)` streams each `.tar.gz` through `flate2`
  and `tar`, without extracting to disk. It builds the inventory with
  Python's member rules, and compares it with the inventory recorded at
  capture. A mismatch fails.
- `run_cold_backup` verifies all five stores inline before it writes the
  manifest: 4.1, 4.2, 4.3 and `verify_volumes`. Any failure publishes
  nothing and exits 1.
- `gbackup verify DIR` on a cold backup also verifies `volumes`.

Planned verification:
`cargo nextest run -p gobby-backup -E 'test(verify) | test(cold)'`,
then `cargo clippy -p gobby-backup --all-targets -- -D warnings`. On the
isolated test hub, with the test hub's daemon stopped, run
`GOBBY_TEST_PROTECT=1 GOBBY_TEST_ALLOW_DOCKER=1 gbackup backup --cold
--output <tmp>/c1 --json` against the test containers.

**Acceptance:**

- 6.2.1 - A matching archive verifies, and a member whose content changed
  fails. test:
  `crates/gbackup/src/verify/volumes/tests.rs::inventory_match_and_content_change`.
- 6.2.2 - A symlink or hard-link member is refused. test:
  `crates/gbackup/src/verify/volumes/tests.rs::refuses_link_members`.
- 6.2.3 - A cold backup is published only after all five stores verify
  inline. Any failure publishes nothing. test:
  `crates/gbackup/src/cold/tests.rs::cold_publishes_only_after_inline_verify`.
- 6.2.4 - `gbackup verify DIR` on a cold backup verifies all five stores,
  rewrites the `volumes` verification state, and exits 1 when volumes
  fail. test:
  `crates/gbackup/src/verify/tests.rs::verify_command_covers_cold_volumes`.

## P7: OS Timers
`kind: framing`

**Goal:** `gobby service install` schedules the nightly backup and the
weekly verify with launchd or systemd, and `gobby service uninstall`
removes them.

### 7.1 launchd timers and service wiring [category: code] (depends: 1.4, 4.4)
`kind: deliverable`

Targets:
- `src/gobby/cli/installers/backup_timers.py`
- `src/gobby/install/shared/services/com.gobby.backup.plist.j2`
- `src/gobby/install/shared/services/com.gobby.backup-verify.plist.j2`
- `src/gobby/cli/installers/service.py::install_service`
- `src/gobby/cli/installers/service.py::uninstall_service`
- `src/gobby/cli/service.py::install`
- `src/gobby/cli/service.py::uninstall`
- `tests/cli/installers/test_backup_timers.py`

**Granularity:** one leaf. Seven production files carry one outcome: the
service installer owns the macOS timers. The two templates, the timer
module and four call sites cannot be tested apart.

Consumers unchanged:
- `tests/cli/installers/test_cli_installers_service.py` — no-edit-reason: drives the daemon service paths, whose result keys stay; the timers add only `backup_timers` and warnings.
- `tests/cli/test_cli_service.py` — no-edit-reason: asserts the existing output lines, which stay; the timer line is new and covered in `test_backup_timers.py`.

**Research context:**
- `install_service` (installers/service.py lines 491-512) dispatches by
  platform after `_reserve_commanded_start`, and `uninstall_service`
  (515-526) does the same.
  - macOS writes `~/Library/LaunchAgents/<label>.plist`, quietly boots out
    the old job, and runs `launchctl bootstrap gui/<uid>` (lines 124-147
    and 243-281).
  - Linux writes a user unit (`_systemd_unit_path`, service_linux.py line
    19) and runs `systemctl --user` `daemon-reload`, `enable` and `start`
    (lines 39-41).
- `_render_template` (service_common.py lines 74-80) renders Jinja
  templates from the services template directory. `_build_path` (211-222)
  builds the daemon unit's PATH, which must reach `docker`.
- `require_files_home` (paths.py lines 132-149) refuses `win32`, remote
  mode and an unconfigured files_home. `resolved_logs_dir` (config/logging.py
  line 126) gives the logs directory.
- The CLI `install` (cli/service.py lines 26-73) prints the result and its
  `warnings`.
- launchd runs a `StartCalendarInterval` job missed during sleep once, on
  wake.
- Decision Records 1, 3, 8 and 12 apply.

Implementation:
- `backup_timers.py` provides `install_backup_timers()` and
  `uninstall_backup_timers()`, each returning a result dict.
  - Install skips with a reason, never an error, when:
    - the platform is not `darwin` or `linux`;
    - `require_files_home` refuses;
    - `~/.gobby/bin/gbackup` is missing (`run gobby install`).
  - On macOS it renders two plists:
    - `com.gobby.backup.plist.j2`: `StartCalendarInterval` hour 7 minute
      0, running `<bin>/gbackup backup --scheduled`;
    - `com.gobby.backup-verify.plist.j2`: weekday 0, hour 7, minute 30,
      running `<bin>/gbackup verify --scheduled`.

    Each plist carries the daemon's PATH, and sends stdout and stderr to
    `<logs>/gbackup.log` or `<logs>/gbackup-verify.log`. Each one is booted
    out quietly, then bootstrapped.
  - Uninstall boots out and removes both plists. Missing files are fine.
- `install_service` calls `install_backup_timers()` after a successful
  daemon install and returns the result under `backup_timers`. A skip or a
  failure becomes a warning.
- `uninstall_service` calls `uninstall_backup_timers()` first.
- The CLI prints `Backup timers: installed` or `Backup timers: skipped
  (<reason>)`.

Planned verification:
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/cli/installers/test_backup_timers.py tests/cli/installers/ -q -k "service or timer"`,
then `uv run ruff check src/ && uv run mypy src/` and a test-types audit of
the changed tests.

**Acceptance:**

- 7.1.1 - The rendered plists hold the schedules, the commands, the PATH
  and the log paths. test:
  `tests/cli/installers/test_backup_timers.py::test_macos_plists_render_schedules_and_logs`.
- 7.1.2 - With a fake `launchctl`, install bootstraps both jobs, and
  uninstall boots them out and removes both plists. test:
  `tests/cli/installers/test_backup_timers.py::test_macos_install_and_uninstall_drive_launchctl`.
- 7.1.3 - Remote mode, a missing files_home, a missing gbackup and `win32`
  each skip with a reason, and the service install still succeeds. test:
  `tests/cli/installers/test_backup_timers.py::test_timer_skips_never_fail_service_install`.
- 7.1.4 - `gobby service install` prints the timer outcome. test:
  `tests/cli/installers/test_backup_timers.py::test_service_install_reports_backup_timers`.

### 7.2 systemd user timers [category: code] (depends: 7.1)
`kind: deliverable`

Targets:
- `src/gobby/cli/installers/backup_timers.py`
- `src/gobby/install/shared/services/gobby-backup.service.j2`
- `src/gobby/install/shared/services/gobby-backup.timer.j2`
- `src/gobby/install/shared/services/gobby-backup-verify.service.j2`
- `src/gobby/install/shared/services/gobby-backup-verify.timer.j2`
- `tests/cli/installers/test_backup_timers.py`

**Research context:** the daemon's unit template
(`gobby-daemon.service.j2`, lines 11 and 23-24) sets
`Environment=PATH=...`, `StandardOutput=append:` and
`StandardError=append:`. `install_service_linux` (service_linux.py lines
25-77) drives `systemctl --user`. WSL 2 reports `linux`. With
`Persistent=true`, a timer missed while the machine was off runs at the
next boot.

Implementation:
- Both services are `Type=oneshot`, with `ExecStart=<bin>/gbackup backup
  --scheduled` or `ExecStart=<bin>/gbackup verify --scheduled`. Each gets
  the daemon's PATH, and appends stdout and stderr to `gbackup.log` or
  `gbackup-verify.log`.
- The timers use `OnCalendar=*-*-* 07:00:00` and `OnCalendar=Sun *-*-*
  07:30:00`, with `Persistent=true` and `WantedBy=timers.target`.
- Install writes the four units to the user unit directory, runs
  `daemon-reload`, and runs `enable --now` for both timers. When `systemctl
  --user` fails, for example on WSL without systemd, it skips with the
  reason.
- Uninstall runs `disable --now` for both timers, removes the four units,
  and runs `daemon-reload`.

Planned verification:
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/cli/installers/test_backup_timers.py -q`,
then `uv run ruff check src/ && uv run mypy src/`.

**Acceptance:**

- 7.2.1 - The rendered units hold the commands, the calendars,
  `Persistent=true` and the append logs. test:
  `tests/cli/installers/test_backup_timers.py::test_systemd_units_render_calendars_and_logs`.
- 7.2.2 - With a fake `systemctl`, install and uninstall run their commands
  in order. test:
  `tests/cli/installers/test_backup_timers.py::test_systemd_install_and_uninstall_drive_systemctl`.
- 7.2.3 - An unavailable user systemd skips the timers with a reason. test:
  `tests/cli/installers/test_backup_timers.py::test_systemd_unavailable_skips_timers`.

## P8: Python Retirement and Documentation
`kind: framing`

**Goal:** `gobby hub-backup` hands off to gbackup, the Python
implementation is gone, and the operator docs describe gbackup.

### 8.1 hub-backup shim and Python module retirement [category: code] (depends: 1.4, 6.2)
`kind: deliverable`

Targets:
- `src/gobby/cli/hub_backup/cli.py::*` — scope-reason: the implementation becomes passthrough `hub_backup` and `restore_hub_backup` commands that exec gbackup; `_start_daemon` stays for its maintenance caller
- `src/gobby/cli/hub_backup/_stores.py::*` — operation: delete — scope-reason: gbackup owns the store captures
- `src/gobby/cli/hub_backup/_verify.py::*` — operation: delete — scope-reason: gbackup owns restore verification
- `src/gobby/cli/hub_backup/_manifest.py::*` — operation: delete — scope-reason: gbackup owns manifest v3 (A1)
- `src/gobby/cli/hub_backup/_content.py::*` — operation: delete — scope-reason: gbackup owns the content digests
- `src/gobby/cli/hub_backup/_integrity.py::*` — scope-reason: keep only `refuse_symlink_traversal`, `require_regular_file`, `open_regular_binary` and `file_digest`
- `src/gobby/cli/hub_backup/files_home.py::*` — scope-reason: drop `archive_files_home_store`, `verify_files_home_archive`, `restore_hub_files`, the `FILES_ARCHIVE_RELPATH`, `FILES_STORE_KEY` and `FILES_ARCHIVE_METHOD` constants, and the `_manifest` import
- `src/gobby/cli/hub_backup/__init__.py::*` — scope-reason: drop the `_manifest` re-exports
- `tests/cli/hub_backup/test_hub_backup_shim.py`
- `tests/cli/hub_backup/test_cli_hub_backup_cli.py::*` — operation: delete — scope-reason: tests the retired implementation
- `tests/cli/hub_backup/test_stores.py::*` — operation: delete — scope-reason: tests the retired store module
- `tests/cli/hub_backup/test_verify.py::*` — operation: delete — scope-reason: tests the retired verify module
- `tests/cli/hub_backup/test_manifest.py::*` — operation: delete — scope-reason: tests the retired manifest module
- `tests/cli/hub_backup/test_globals_credentials.py::*` — operation: delete — scope-reason: tests the retired globals handling
- `tests/cli/installers/test_docker_guard.py::*` — scope-reason: delete the four hub-backup guard tests and their hub imports
- `tests/cli/test_hub_files_restore.py::*` — scope-reason: delete `test_hub_backup_restore_files_uses_dest_files_home`, `_fake_verified_manifest`, the `hub_cli` import and the `FILES_ARCHIVE_RELPATH` import
- `tests/cli/test_hub_backup_rehearsal.py::*` — scope-reason: delete the tests that drive retired internals (`_hub_backup_target`, `_archive_volumes`, `tar_volumes`, `stop_daemon`); keep the rehearsal-profile and `_start_daemon` tests
- `tests/cli/test_hub_maintenance.py::*` — scope-reason: delete `test_hub_backup_epoch_refuses_non_orchestrator_invocation` and its `hub_backup` import
- `tests/fixtures/test_live_hub_scan.py::*` — scope-reason: drop the allowlist entries of deleted or trimmed test files, because the scan requires an exact match

**Granularity:** one leaf, an atomic cut. Deleting the modules breaks
every importer, so the shim, the trims and the test removals land in one
commit.

Consumers unchanged:
- `src/gobby/cli/__init__.py` — no-edit-reason: registers `hub_backup.cli:hub_backup`, which the shim keeps.
- `src/gobby/cli/hub_maintenance.py` — no-edit-reason: imports the kept `_start_daemon` and `load_rehearsal_profile`; its `hub-backup --epoch` step is unreachable, because every campaign fails before it.
- `src/gobby/cli/hub_backup/rehearsal.py` — no-edit-reason: imports `refuse_symlink_traversal`, which `_integrity.py` keeps.
- `src/gobby/cli/hub_backup/bootstrap_restore.py` — no-edit-reason: imports kept files_home helpers.
- `src/gobby/cli/pack.py` — no-edit-reason: imports kept files_home helpers and `bootstrap_restore`.
- `src/gobby/cli/postgres_backup.py` — no-edit-reason: imports only `rehearsal`.
- `src/gobby/tasks/criterion_commands.py` — no-edit-reason: lists the `hub-backup` command name, which stays.
- `tests/cli/test_cli.py` — no-edit-reason: the registration and lazy-import checks still hold for the shim module.
- `tests/cli/test_pack.py` — no-edit-reason: uses kept files_home hooks.
- `tests/cli/hub_backup/test_files_home_platform_refusal.py` — no-edit-reason: tests the kept `require_destination_files_home`.
- `docs/reference-audit/admin.json` — no-edit-reason: cites `hub_backup` and `restore_hub_backup` in the shim module by symbol, and both names stay.

**Research context:**
- The command registry (`cli/__init__.py` line 69) loads `hub-backup`
  lazily from `hub_backup.cli`.
- Importers of the retired modules: `__init__.py` re-exports from
  `_manifest`, and `files_home.py` imports `file_digest`, `ArtifactRecord`
  and `VerificationState`.
- `pack.py` and `bootstrap_restore.py` use the files_home helpers
  `write_restricted_archive`, `restore_files_home_from_archive`,
  `maintenance_claim`, `check_output_outside_sources`,
  `destination_free_bytes`, `require_destination_files_home`,
  `archived_bootstrap` and `merge_bootstrap_preserving_files_home`. The
  restore path still calls `file_digest`.
- `hub_maintenance.py` imports `_start_daemon` (lines 18, 203 and 313).
  #23557 (Remove executor-less gobby hub-maintenance run campaigns and the
  epoch surface) records that every campaign fails before its
  `hub-backup --epoch` step, because no executor is registered.
- #23586 (hub-backup live-path recovery gaps: restore consumes unlisted
  inputs unverified, and a failed partial service stop skips the restart)
  is open. Its fix edits `restore_hub_backup` and `_archive_volumes` in
  `cli.py` and adds tests of both under `tests/cli/`.
- `tests/fixtures/test_live_hub_scan.py` (lines 15-39) requires its
  allowlist to equal the files it finds.

This leaf is a move out of `cli.py`, which shrinks from 942 lines to the
shim. The implementation goes to gbackup (P2 to P6), and the shim's tests
go in the new `tests/cli/hub_backup/test_hub_backup_shim.py`.

Implementation:
- `hub_backup` becomes a click command with `ignore_unknown_options`,
  `allow_extra_args`, `add_help_option=False` and unprocessed arguments.
  - A first argument `restore` calls `restore_hub_backup(rest)`, which
    execs `~/.gobby/bin/gbackup restore <rest>`.
  - Anything else execs `gbackup backup <args>`.
  - `os.execv` replaces the process, so the output and the exit code are
    gbackup's.
- A missing binary fails with `gbackup is not installed; run gobby
  install`. On `win32` the shim refuses, as Decision Record 12 says.
- `_start_daemon` stays, with its imports.
- The deletions and trims are as the Targets list them.
- Before the cut, `gcode grep` sweeps `tests/` for imports of the
  retired modules and symbols. A test added after expansion, #23586's
  among them, is deleted or trimmed in the same commit. Its file leaves
  the `test_live_hub_scan.py` allowlist, and a trimmed file joins the
  pytest scope.

Planned verification:
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/cli/hub_backup/ tests/cli/installers/test_docker_guard.py tests/cli/test_hub_files_restore.py tests/cli/test_hub_backup_rehearsal.py tests/cli/test_hub_maintenance.py tests/cli/test_pack.py tests/cli/test_cli.py tests/fixtures/test_live_hub_scan.py -q`,
then `uv run ruff check src/ && uv run mypy src/` and a test-types audit of
the changed tests.

**Acceptance:**

- 8.1.1 - `gobby hub-backup --output X --json` execs `gbackup backup
  --output X --json` with the arguments unchanged. test:
  `tests/cli/hub_backup/test_hub_backup_shim.py::test_backup_args_pass_through`.
- 8.1.2 - `gobby hub-backup restore DIR --database-url U --yes` execs
  `gbackup restore` with the same arguments. test:
  `tests/cli/hub_backup/test_hub_backup_shim.py::test_restore_args_pass_through`.
- 8.1.3 - A missing gbackup fails with a message naming `gobby install`,
  and `win32` is refused. test:
  `tests/cli/hub_backup/test_hub_backup_shim.py::test_missing_binary_and_windows_refuse`.
- 8.1.4 - The retired modules are gone. `files_home`, `bootstrap_restore`,
  `rehearsal`, `_integrity` and `cli._start_daemon` import cleanly. test:
  `tests/cli/hub_backup/test_hub_backup_shim.py::test_retired_modules_removed_and_kept_modules_import`.
- 8.1.5 - The live-hub scan allowlist equals its hits. test:
  `tests/fixtures/test_live_hub_scan.py::test_no_test_fixture_supplies_the_live_hub_coordinates`.

### 8.2 Backup documentation [category: docs] (depends: 7.2, 8.1)
`kind: deliverable`

Targets:
- `docs/guides/cli-commands.md`
- `docs/guides/admin-operations.md`
- `src/gobby/install/shared/skills/gobby/references/admin/backups.md`
- `src/gobby/install/shared/skills/gobby/references/admin/recovery.md`

**Research context:** stale text sits at:
- `cli-commands.md:38`, 326, 1073 and 1120, including the
  `#hub-backup-disaster-recovery` anchor that other pages link to;
- `admin-operations.md:248-262` and 362;
- `backups.md:4`, 18, 36 and 45;
- `recovery.md:22` and 62.

Implementation:
- `cli-commands.md` adds a `gbackup` section covering `backup` (with
  `--output`, `--json`, `--scheduled` and `--cold`), `verify` and
  `restore`. The `hub-backup` row and the disaster-recovery steps say that
  `gobby hub-backup` hands off to gbackup. The anchor stays.
- `admin-operations.md` describes:
  - the nightly and weekly timers;
  - `last-run.json` and the two logs;
  - keeping 7 backups;
  - the cold procedure: `gobby stop`, `gbackup backup --cold`, `gobby
    start`;
  - the restore procedure: `gbackup verify DIR`, `gobby stop`, `gbackup
    restore DIR --database-url URL [--clean]`.
- The two skill references say the same briefly, and refresh their
  `_Last verified_` dates.
- `--epoch` disappears from every page.

Planned verification:
`uv run gobby plans validate .gobby/plans/gobby-backup.md -p /Users/josh/Projects/gobby`
and a read-through against the shipped `gbackup --help`.

**Acceptance:**

- 8.2.1 - The admin guide documents the timers, the run record and
  retention. behavior: "gbackup backup --scheduled" in
  `docs/guides/admin-operations.md`.
- 8.2.2 - The recovery reference documents verify-then-restore. behavior:
  "gbackup restore" in
  `src/gobby/install/shared/skills/gobby/references/admin/recovery.md`.
- 8.2.3 - The command reference documents gbackup, and no page mentions
  `--epoch`. behavior: "gbackup verify" in `docs/guides/cli-commands.md`.

## D1 Publish gbackup: release assets and Homebrew formula (depends: 1.4)
`kind: deferred`

gbackup is installed from the workspace only, like gclient. A machine
without a source checkout gets no gbackup and no timers. Publishing needs
release-asset builds for each platform and a Homebrew formula entry
outside this repository, plus adding `gbackup` to `HOMEBREW_HELPERS`.

```yaml
deferral:
  task_ref: "created-at-expansion"
  reason: "Publishing needs release pipeline work and a formula change outside this repository; workspace install covers every current hub owner."
  owner: "orchestrator"
  original_acceptance_items:
    - D1.1
```

- D1.1 - Releases ship a `gbackup` asset per supported platform. The
  Homebrew formula installs it, `HOMEBREW_HELPERS` lists it, and
  `UNPUBLISHED_MANAGED_BINS` drops it.

## Rollout
`kind: framing`

1. Leaves land through the lane review path. A crate change is live only
   after `gobby install` promotes the new gbackup, which never touches the
   stamped daemon set and needs no daemon restart.
2. Until 7.1 and 7.2 land, the timers do not exist, and operators run
   `gbackup` by hand. Run `gobby service install` once after 7.2, then
   check `launchctl print gui/<uid>/com.gobby.backup` or `systemctl --user
   list-timers`.
3. 8.1 lands after 6.2. Two open tasks also edit what 8.1 trims, and
   neither gates it:
   - #23557 (Remove executor-less gobby hub-maintenance run campaigns and
     the epoch surface) edits `tests/cli/test_hub_maintenance.py`.
   - #23586 (hub-backup live-path recovery gaps: restore consumes unlisted
     inputs unverified, and a failed partial service stop skips the
     restart) edits `cli.py` and adds tests of the modules 8.1 retires.

   Either one that lands before expansion re-derives 8.1's Targets at
   expansion: its new tests of retired modules, the
   `test_live_hub_scan.py` allowlist and 8.1's pytest scope. One that
   lands later is caught by 8.1's sweep before the cut.
4. After the first nightly, read `~/.gobby/backups/hub/last-run.json`.
   After the first Sunday, check that the newest live manifest records
   `restore_verified` for its four live stores.

## V1 Plan Changelog
`kind: verification`

- 2026-10-05 16:40 CDT: Rewrite by the Lane 7 Plan Writer gobby#15434 on
  #20997 (Plan gobby-backup: Rust external hub-backup runner with
  scheduling), replacing Draft 1 (`414a2d6c9d`). Draft 1 items superseded:
  - item 3's Windows Task Scheduler unit (A7);
  - item 6's epoch retention clause (D1);
  - item 8's gcore-owned manifest (A1);
  - item 9's files-only restore gate, replaced by D3 (a);
  - item 10's `--epoch` and cutover install (D1, A2);
  - items 11 and 12 (A3);
  - `gbackup status`, dropped for `last-run.json` (Decision Record 8).

  Design choices:
  - The live run reads PostgreSQL expectations in the exported snapshot,
    and records Qdrant and FalkorDB drift as ranges.
  - Verify waits on the lock, while the scheduled backup skips.
  - Retention checks sizes only.
  - gbackup verifies only manifests it wrote.
  - D1 is widened to release assets plus the formula.
- 2026-10-05 16:51 CDT: Enhancer pass (run `7027aef5`) on `efe6a44`. The
  Orchestrator gobby#14972 accepted E1 to E6 and declined E7.
  - E1: 4.3 splits `preflight_files_restore` from the write. 5.2 runs it
    and takes `ServicesLock` before confirmation, and holds the guard
    through the FalkorDB restart.
  - E2: 5.1 attempts a FalkorDB start on every path after the stop (5.1.6).
  - E3: `pg::serial_db_url` (1.3) admits only the test target under
    `GOBBY_TEST_PROTECT`, and every `serial_db` command sets it.
  - E4: `pg::connect` (1.3) sets `statement_timeout` and `lock_timeout` from
    the capture budget, which keeps Decision Record 7's wait bounded.
  - E5: 2.2.6 also refuses a `schema_migrations` table with no applied
    version.
  - E6: 6.2 targets `cold/tests.rs` and `verify/tests.rs`, and 6.2.4 moves to
    the verify command harness.
  - E7: a full recovery rehearsal on a test-owned hub, declined and recorded
    under Rejected alternatives.
- 2026-10-05 17:05 CDT: Adversary findings B1 to B5 from Adv1
  gobby#15401 on `399bbbe`. The Writer accepted all five.
  - B1: `flock_until` (1.2) bounds every blocking lock by the capture
    budget, and Decision Record 7 says so.
  - B2: restore refuses a non-null `epoch_id` before any write (5.2,
    Decision Record 9).
  - B3: FalkorDB graph membership drift records `graphs_optional`, and
    `falkordb_matches` (2.4) is shared by 4.2 and 5.1 (Decision Record 13).
  - B4: the reset test works on its own disposable database (5.2.7).
  - B5: `check_artifact_set` (2.1) runs before every verify and restore.
- 2026-10-05 17:20 CDT: Adv1 verified `ed7821d`, which resolved B2 to B4.
  The Writer accepted its two adjacent residuals.
  - B1: a failure to record writes a stderr diagnostic and exits 1
    (1.2.4, 3.2, 4.4).
  - B5: only `volumes` may be skipped. A forged skipped required store
    is refused before any write (2.1.7, 5.2.3).
  - 8.1 also removes the `FILES_ARCHIVE_RELPATH` import from
    `tests/cli/test_hub_files_restore.py`.
- 2026-10-05 17:21 CDT: Adv1 verified `3aeb4c7` and asked that old-record
  preservation cover only failures before the rename. `dd7449b` makes that
  change (1.2, 1.2.4, 3.2).
- 2026-10-05 17:21 CDT: Consensus between the Plan Writer gobby#15434 and
  the Plan Adversary gobby#15401 on `dd7449b`. B1 to B5 are resolved, and
  no blocking finding remains. Base validation exits 0 with no warnings.
  The Adversary derives and applies M1 next.
- 2026-10-05 17:36 CDT: CR7 gobby#15396 bounced `99667e6`. The Writer
  accepted its two blocking and two low findings, with the coverage
  cases Adv1 gobby#15401 added.
  - B1: 5.1 and 6.1 arm the container start before the first stop
    attempt. A stop that errors, times out or stops only some
    containers still restarts them (5.1.6, 6.1.3, 6.1.6).
  - B2: Rollout 3 and 8.1 name #23586 (hub-backup live-path recovery
    gaps: restore consumes unlisted inputs unverified, and a failed
    partial service stop skips the restart). 8.1 sweeps for new tests
    of retired modules before its cut.
  - The `_archive_volumes` citations name `cli.py`, and 1.4's planned
    verification runs `tests/cli/test_install_components.py`.
- 2026-10-05 17:40 CDT: Adv1 verified `8a9c34f` and asked that a failed
  readiness wait fail the cold run as a failed start does. 6.1 bounds
  the wait at 5.1's 60 s and continues past either failure (6.1.4,
  6.1.6).
- 2026-10-05 17:42 CDT: Renewed consensus between the Plan Writer
  gobby#15434 and the Plan Adversary gobby#15401 on `de726f0`. CR7's B1,
  B2 and both low findings are resolved, and no blocking finding
  remains. The stale M1 is removed. Base validation exits 0 with no
  warnings. The Adversary derives and applies M1 next.

## V2: Verification
`kind: verification`

Each leaf runs its planned verification after its final edit. After the
last leaf lands:

```bash
cargo nextest run -p gobby-backup
GOBBY_TEST_PROTECT=1 GBACKUP_POSTGRES_TEST_DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test cargo nextest run -p gobby-backup -E 'test(serial_db)'
cargo clippy -p gobby-backup --all-targets -- -D warnings && cargo fmt -p gobby-backup -- --check
DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/cli/test_install_setup_gbackup.py tests/cli/test_cli_install.py tests/cli/test_install_setup_gterm.py tests/install/test_version_pins.py tests/cli/test_install_binary_components.py tests/cli/test_install_components.py tests/utils/test_utils_status.py tests/cli/installers/test_backup_timers.py tests/cli/hub_backup/ tests/cli/installers/test_docker_guard.py tests/cli/test_hub_files_restore.py tests/cli/test_hub_backup_rehearsal.py tests/cli/test_hub_maintenance.py tests/cli/test_pack.py tests/cli/test_cli.py tests/fixtures/test_live_hub_scan.py -q
uv run ruff format --check src/ && uv run ruff check src/ && uv run mypy src/
uv run gobby plans validate .gobby/plans/gobby-backup.md -p /Users/josh/Projects/gobby
```

On the isolated test hub, with `GOBBY_TEST_PROTECT=1` and
`GOBBY_TEST_ALLOW_DOCKER=1`, run `gbackup backup --output <tmp>/b1 --json`
and then `gbackup verify <tmp>/b1 --json`. Both must exit 0, and every live
store must record `restore_verified`.
