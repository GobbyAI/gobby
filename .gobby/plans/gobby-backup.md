Plan artifact: `.gobby/plans/gobby-backup.md`

# gobby-backup

> **Plan ID:** `gobby-backup`
>
> **Owner epic:** #20997

## Overview
`kind: framing`

Replace the Python `gobby hub-backup` tool with a Rust binary `gbackup` (crate `gobby-backup` at `crates/gbackup`) that can run while the daemon and managed datastores stay up. An OS timer fires a live nightly dump plus hashes at 07:00 local, a weekly timer scratch-verifies the newest integrity-ok backup, and operators still have a cold volume path and restore. `gobby hub-backup` becomes a shim, then the Python implementation is deleted. Every implementation leaf runs in a linked worktree `wt-task-<task_ref>` created off the local `0.5.0` branch after that leaf has a task ref (e.g. `wt-task-20997` for `#20997`). Do not create the worktree until the task exists; do not implement on the main checkout.

## Constraints
`kind: framing`

**Decision Record (confirmed)**

1. Scheduled backup is live. No daemon stop, no datastore stop. Stores: live `pg_dump`/`pg_dumpall`, Qdrant snapshots, FalkorDB BGSAVE, files_home (live tar; exclude pid/socket/lock; in-flight JSONL may tear). Volume tars are an explicit operator/epoch cold path only.
2. Crate `gobby-backup` at `crates/gbackup`. Binary `gbackup` at `~/.gobby/bin/gbackup`.
3. OS scheduler owns cadence. macOS: launchd. Linux and WSL: systemd timer in the distro. Native Windows: Task Scheduler (`schtasks`). The daemon may trigger a run; it does not own the schedule.
4. Daily 07:00 local + 0–15 min jitter (after memory-dream `0 2 * * *` local, 4h admission).
5. Nightly: live dump + sha256. Weekly Sunday 07:30 local + jitter: scratch-verify the newest integrity-ok backup in disposable containers. Daemon never stops.
6. Keep the 7 most recent integrity-ok live backups under `~/.gobby/backups/hub/`. Failed/unverified staging is always removed. Cold/epoch backups are not auto-pruned.
7. Failure visibility: `~/.gobby/backups/hub/last-run.json` plus logs. No Gobby task.
8. gbackup owns backup, verify, restore, and cold. `gobby hub-backup` shims to gbackup, then Python `src/gobby/cli/hub_backup/` is deleted. Manifest v3 stays in gcore. Live runs still emit all five STORE_KEYS; `volumes` is present with `details.skipped=true` and `reason: "live-mode"`.
9. Destructive restore requires scratch-verified `files` (`restore_verified`). Skipped `volumes` on a live backup do not fail the gate. `--drop-existing` / `--yes` remain.
10. Named defaults: exclusive `~/.gobby/backups/hub/gbackup.lock` (overlapping run skips, exit 0); `GOBBY_TEST_PROTECT` fail-closed before real Docker (same contract as `ensure_docker_allowed`); secrets never in manifest or stdout; `--epoch` is a `gobby hub-maintenance` child only and may stop the daemon because the epoch owns lifecycle; `gobby cutover` installs gbackup.
11. Implement from a linked worktree off the local `0.5.0` branch. Name it `wt-task-<task_ref>` using the numeric task ref once it exists (`#20997` → `wt-task-20997`; expanded leaves use their own refs). Create the worktree only after claim/create of that task. Do not implement on the main checkout. `gobby start`/`restart` from that worktree still requires `GOBBY_ALLOW_WORKTREE_DAEMON=1` if a daemon is needed; crate work uses `cargo` in the worktree and installs `gbackup` via new inode.
12. After this plan is approved, the coordinating session calls `gobby-sessions:set_handoff` with `clear_session=true`. Required fields: nonblank `current_state`, at least one `next_steps` item pointing at `.gobby/plans/gobby-backup.md`, `#20997`, and `wt-task-<task_ref>`. Put the Decision Record in `key_decisions`. The successor must call `get_handoff()` first. Do not use `clear_session=true` from an autonomous agent-run (blocked by `block-autonomous-clear-session`); this interactive coordinator is the one that clears.

**Non-goals**

- Daemon-hosted backup cron.
- Gobby task / start-time catch-up on failure.
- Raising Grok MCP output caps (follow-up #21145).
- Hub table TTL (hub-data-retention / #19379).
- Changing memory-dream schedule.

**Python parity map**

| Current Python behavior | Owner |
| --- | --- |
| Live `pg_dump -Fc` + `pg_dumpall --globals-only` after `gobby_agent_auth.drain_ephemeral_principals()` | 2.1 |
| Qdrant collection snapshots via API | 2.2 |
| FalkorDB `BGSAVE` + copy dump via container | 2.3 |
| files_home tar, archive_verified | 2.4 |
| Volume tars of managed data volumes | 6.1 |
| Stop daemon + `_services_stop` around volume tar; restart even on failure | 6.1 |
| `--epoch` only as hub-maintenance child; leave daemon stopped | 6.2 |
| Manifest v3, STORE_KEYS, no secrets | 2.5 |
| sha256 artifact integrity | 2.5 / 3.1 |
| Scratch restore-verify (postgres roles/ACL, qdrant, falkordb, volumes, files) | 4.1 / 6.3 |
| `gobby hub-backup restore` + `--drop-existing` `--yes` + files gate | 5.1 |
| `GOBBY_TEST_PROTECT` docker fail-closed | 1.2 |
| `--json` manifest path + store summary | 2.5 |
| Default root `~/.gobby/backups/hub/<UTC timestamp>/` | 2.5 |
| Refuses while daemon running unless it stopped it | 6.1 only; live may run with daemon up |

**Consumer / literal sweep (authoring)**

- `gcode grep -w VerifiedBackupManifest crates src` → `crates/gcore/src/schema/gate.rs`, `crates/gcore/src/schema/mod.rs`, `crates/gcore/src/schema/runner.rs`, `crates/gcore/src/schema/runner_tests.rs`, `crates/gdaemon/src/main.rs`. Owned consumers targeted in 5.2.
- `gcode grep -F "_BINARY_NAMES" src/gobby/cli/cutover.py` → cutover binary set. Targeted via `cutover.py::*` in 1.3.
- P8 re-runs `gcode grep -w hub_backup src/gobby tests` and `gcode grep -w files_home src/gobby tests` from the implementing checkout.

**Production size**

| File | Lines | Disposition |
| --- | --- | --- |
| `src/gobby/cli/hub_backup/cli.py` | 901 | P8 shim/delete |
| `src/gobby/cli/installers/service.py` | 646 | Do not grow; backup timers in a new module |
| `crates/gcore/src/schema/gate.rs` | 390 | Safe to edit |
| `src/gobby/cli/cutover.py` | 367 | Safe to edit |

No schema/config `.py` edits, so `derived-carriers` is not triggered.

## P1: Crate, CLI, lock, last-run, Docker guard
`kind: framing`

**Goal**: `gbackup` exists in the workspace, installs via cutover, and can refuse concurrent runs without dumping anything.

### 1.1 Add gobby-backup crate and clap CLI [category: code]
`kind: deliverable`

Targets:
- `crates/gbackup/Cargo.toml`
- `crates/gbackup/src/main.rs`
- `crates/gbackup/src/lib.rs`
- `crates/gbackup/src/cli.rs`
- `Cargo.toml`
- `crates/gbackup/tests/cli_smoke.rs`

Add workspace member `crates/gbackup`. Package name `gobby-backup`, edition 2024, rust-version 1.88, license FSL-1.1-ALv2 (same as sibling crates). Binary name `gbackup`. Library `gobby_backup`. Depend on `anyhow`, `clap` (derive), `gobby-core` (path; home/machine helpers), `serde`, `serde_json`, `fs4`. Add `[profile.release.package.gobby-backup] opt-level = 3`.

```text
gbackup backup [--output DIR] [--cold] [--epoch ID] [--json] [--scheduled]
gbackup verify [DIR] [--scheduled]
gbackup restore <DIR> [--database-url URL] [--drop-existing] [--yes]
gbackup status
```

P1 stubs for backup/verify/restore may exit 2 `not_implemented` after lock + last-run. `gbackup --version` exits 0. Unix SIGPIPE reset like gwiki. No secrets in help or default output.

**Acceptance:**

- 1.1.1 - Workspace builds `gbackup`. file: `crates/gbackup/Cargo.toml`.
- 1.1.2 - Subcommands `backup`, `verify`, `restore`, `status` exist. test: `crates/gbackup/tests/cli_smoke.rs`.
- 1.1.3 - `--version` exits 0. test: `crates/gbackup/tests/cli_smoke.rs`.

### 1.2 Implement lock, last-run.json, jitter, Docker test-protect [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `crates/gbackup/src/lock.rs`
- `crates/gbackup/src/last_run.rs`
- `crates/gbackup/src/schedule.rs`
- `crates/gbackup/src/docker.rs`
- `crates/gbackup/src/lib.rs`
- `crates/gbackup/src/lock/tests.rs`
- `crates/gbackup/src/last_run/tests.rs`
- `crates/gbackup/src/schedule/tests.rs`
- `crates/gbackup/src/docker/tests.rs`

Exclusive non-blocking flock on `~/.gobby/backups/hub/gbackup.lock` via `fs4`. If held: log already running, last-run `skipped_locked`, exit 0.

`~/.gobby/backups/hub/last-run.json` mode 0600:

```json
{
  "started_at": "RFC3339",
  "finished_at": "RFC3339",
  "status": "ok | error | skipped_locked | not_implemented",
  "mode": "live | cold | verify | status",
  "backup_root": null,
  "error": null,
  "pid": 0
}
```

Never put DSNs, API keys, or passwords in this file.

`--scheduled` sleeps `blake2s(machine_id) % 16` minutes (0–15) using `gobby_core::machine::read_machine_id_from_home`. Tests inject sleep; no wall-clock sleep in unit tests.

If `GOBBY_TEST_PROTECT` is truthy and `GOBBY_TEST_ALLOW_DOCKER` is not, refuse spawning a real `docker` binary. Same fail-closed contract as `ensure_docker_allowed`.

Use `#[cfg(test)] #[path = ".../tests.rs"] mod tests;` — no large inline `#[cfg(test)]` modules.

**Acceptance:**

- 1.2.1 - Second process exits 0 when the lock is held. test: `crates/gbackup/src/lock/tests.rs`.
- 1.2.2 - last-run.json writes status without secrets. test: `crates/gbackup/src/last_run/tests.rs`.
- 1.2.3 - `--scheduled` jitter is 0–15 minutes from machine id. test: `crates/gbackup/src/schedule/tests.rs`.
- 1.2.4 - `GOBBY_TEST_PROTECT` blocks real docker. test: `crates/gbackup/src/docker/tests.rs`.

### 1.3 Install gbackup through cutover [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `src/gobby/cli/cutover.py::*` — scope-reason: fifth native binary in the existing cutover set
- `tests/cli/test_cutover.py::*` — scope-reason: cutover tests assert the native binary set

Add `gbackup` beside `gcode`, `gdaemon`, `ghook`, `gwiki`. `cargo build --release -p gobby-backup`. Install via new inode. Sign on macOS. Cutover must install gbackup even if it was missing so existing hubs gain the fifth binary.

**Acceptance:**

- 1.3.1 - Cutover builds and installs `gbackup` to `~/.gobby/bin`. file: `src/gobby/cli/cutover.py`.
- 1.3.2 - Missing prior gbackup is not a hard cutover failure. test: `tests/cli/test_cutover.py`.

## P2: Live store dumps and v3 manifest
`kind: framing`

**Goal**: One live `gbackup backup` writes postgres, qdrant, falkordb, and files artifacts plus a valid v3 manifest with volumes skipped.

### 2.1 Dump Postgres live [category: code] (depends: P1)
`kind: deliverable`

Targets:
- `crates/gbackup/src/stores/postgres.rs`
- `crates/gbackup/src/stores/postgres/tests.rs`
- `crates/gbackup/src/bootstrap.rs`

Read `database_url` from bootstrap via gobby-core home helpers. Never log the DSN. `SELECT gobby_agent_auth.drain_ephemeral_principals()` then `pg_dump -Fc` and `pg_dumpall --globals-only` through `docker exec` on the managed postgres container (same discovery as Python `_managed_postgres_container`). Keep ownership/ACLs. All docker through `docker.rs`. Relpaths match Python `_stores.py`. Do not stop the daemon or container.

**Acceptance:**

- 2.1.1 - Live dump drains ephemeral principals then writes dump + globals. file: `crates/gbackup/src/stores/postgres.rs`.
- 2.1.2 - DSN is never logged or written to last-run/manifest. test: `crates/gbackup/src/stores/postgres/tests.rs`.

### 2.2 Snapshot Qdrant live [category: code] (depends: P1)
`kind: deliverable`

Targets:
- `crates/gbackup/src/stores/qdrant.rs`
- `crates/gbackup/src/stores/qdrant/tests.rs`

For each collection: count points, create snapshot wait=true, download, delete snapshot. Fail closed if no managed Qdrant URL. Artifacts `qdrant/<collection>.snapshot`.

**Acceptance:**

- 2.2.1 - Each collection produces a snapshot artifact and the remote snapshot is deleted. file: `crates/gbackup/src/stores/qdrant.rs`.
- 2.2.2 - Missing Qdrant URL fails closed. test: `crates/gbackup/src/stores/qdrant/tests.rs`.

### 2.3 Dump FalkorDB live [category: code] (depends: P1)
`kind: deliverable`

Targets:
- `crates/gbackup/src/stores/falkordb.rs`
- `crates/gbackup/src/stores/falkordb/tests.rs`

docker exec `redis-cli`: LASTSAVE, BGSAVE, wait for LASTSAVE change, copy dump. Record graph counts. Container name matches Python `FALKORDB_CONTAINER`.

**Acceptance:**

- 2.3.1 - BGSAVE dump is copied after LASTSAVE advances. file: `crates/gbackup/src/stores/falkordb.rs`.
- 2.3.2 - docker exec goes through the test-protect guard. test: `crates/gbackup/src/stores/falkordb/tests.rs`.

### 2.4 Archive files_home live [category: code] (depends: P1)
`kind: deliverable`

Targets:
- `crates/gbackup/src/stores/files.rs`
- `crates/gbackup/src/stores/files/tests.rs`

Port restricted tar writer from `archive_files_home_store` / `write_restricted_archive`: refuse writing the archive into the source tree, refuse symlink escape, archive_verified after tar list. Live exclusions: `*.pid`, `*.sock`, `*.lock`, `gobby.pid.lock`. Daemon stays up. In-flight JSONL may tear.

**Acceptance:**

- 2.4.1 - files_home tar excludes pid/socket/lock and is archive_verified. file: `crates/gbackup/src/stores/files.rs`.
- 2.4.2 - Output path inside files_home is rejected. test: `crates/gbackup/src/stores/files/tests.rs`.

### 2.5 Write v3 manifest with volumes skipped [category: code] (depends: 2.1, 2.2, 2.3, 2.4)
`kind: deliverable`

Targets:
- `crates/gbackup/src/backup.rs`
- `crates/gbackup/src/backup/tests.rs`
- `crates/gcore/src/schema/gate.rs::file_sha256`
- `crates/gcore/tests/fixtures/hub_backup_manifest/v3_live_skipped_volumes.json`
- `crates/gcore/tests/schema_contract.rs::*` — scope-reason: round-trip a live-mode fixture whose volumes store is skipped

Stage then publish to `~/.gobby/backups/hub/<UTC timestamp>/`. Validate with `parse_backup_manifest` before publish. All five STORE_KEYS present. `volumes.details` is `{ "skipped": true, "reason": "live-mode" }`; volume verification flags false with method `skipped-live-mode`. Live stores `archive_verified` after hashing; `restore_verified` stays false until P4. Export `file_sha256` from gcore (or a public wrapper) instead of duplicating. `--json` prints manifest_path + store summary, no DSN. Staging cleanup fail-closed.

**Acceptance:**

- 2.5.1 - Live backup publishes a parseable v3 manifest with volumes skipped. test: `crates/gbackup/src/backup/tests.rs`.
- 2.5.2 - gcore fixture round-trips skipped volumes. file: `crates/gcore/tests/fixtures/hub_backup_manifest/v3_live_skipped_volumes.json`.
- 2.5.3 - `--json` includes manifest_path and store summary and no DSN. test: `crates/gbackup/src/backup/tests.rs`.

## P3: Retention and scheduled live run
`kind: framing`

**Goal**: Successful live backups prune to 7 integrity-ok dirs; `--scheduled` applies jitter then backup.

### 3.1 Retain 7 integrity-ok live backups [category: code] (depends: 2.5)
`kind: deliverable`

Targets:
- `crates/gbackup/src/retention.rs`
- `crates/gbackup/src/retention/tests.rs`
- `crates/gbackup/src/backup.rs`

Integrity-ok: artifact hashes match and live stores (postgres, qdrant, falkordb, files) have `archive_verified.verified`. Keep 7 newest live backups by `created_at` (volumes skipped). Delete older live dirs. Always delete failed staging. Cold backups (`volumes` not skipped or `epoch_id` set) are never auto-deleted.

**Acceptance:**

- 3.1.1 - Eighth successful live backup deletes the oldest integrity-ok live dir. test: `crates/gbackup/src/retention/tests.rs`.
- 3.1.2 - Cold/epoch directories are not pruned. test: `crates/gbackup/src/retention/tests.rs`.
- 3.1.3 - Hash mismatch fails the run and removes staging. test: `crates/gbackup/src/backup/tests.rs`.

### 3.2 Wire `--scheduled` live backup [category: code] (depends: 3.1, 1.2)
`kind: deliverable`

Targets:
- `crates/gbackup/src/cli.rs`
- `crates/gbackup/src/backup.rs`
- `crates/gbackup/tests/scheduled_backup.rs`

`gbackup backup --scheduled`: lock → jitter → live backup → retention → last-run. No `--cold`. Missing bootstrap/docker: last-run `error`, non-zero exit. Locked overlap: exit 0.

**Acceptance:**

- 3.2.1 - `--scheduled` runs jitter then live backup and writes last-run. test: `crates/gbackup/tests/scheduled_backup.rs`.
- 3.2.2 - Overlap skip remains exit 0. test: `crates/gbackup/tests/scheduled_backup.rs`.

## P4: Scratch restore-verify
`kind: framing`

**Goal**: `gbackup verify` scratch-restores stores in disposable Docker containers without stopping the daemon.

### 4.1 Implement scratch verify for live stores [category: code] (depends: P2)
`kind: deliverable`

Targets:
- `crates/gbackup/src/verify.rs`
- `crates/gbackup/src/verify/tests.rs`
- `crates/gbackup/src/verify/postgres.rs`
- `crates/gbackup/src/verify/qdrant.rs`
- `crates/gbackup/src/verify/falkordb.rs`
- `crates/gbackup/src/verify/files.rs`

Port Python `_verify.py` live-store paths. Disposable containers: `io.gobby.disposable=true` plus per-run nonce; cleanup re-inspects exact ID/name/nonce; refuse unlabeled, mismatched, Compose-managed. Postgres: globals replay, role expectations excluding managed-principal namespace `^(gobby_agent_[0-9a-f]{32}|gobby_ix_(...)|gobby_mnt_[0-9a-f]{32})_[1-9][0-9]*$`, dump restore, row-count probes, schema object counts. Qdrant recover snapshot. FalkorDB restore dump and graph counts. files_home extract-verify. Do not verify volumes on live backups. Success sets `restore_verified` on postgres/qdrant/falkordb/files and rewrites the manifest.

`gbackup verify` with no DIR: newest integrity-ok live backup that is not yet restore-verified. `--scheduled`: lock + jitter + that selection.

**Acceptance:**

- 4.1.1 - Scratch verify marks live stores restore_verified and leaves volumes skipped. file: `crates/gbackup/src/verify.rs`.
- 4.1.2 - Disposable container cleanup refuses unlabeled containers. test: `crates/gbackup/src/verify/tests.rs`.
- 4.1.3 - `verify --scheduled` verifies the newest integrity-ok unrestored live backup. test: `crates/gbackup/src/verify/tests.rs`.

## P5: Restore and destructive gate
`kind: framing`

**Goal**: Restore a backup with the files gate, allowing skipped volumes on live backups.

### 5.1 Restore live stores [category: code] (depends: 4.1)
`kind: deliverable`

Targets:
- `crates/gbackup/src/restore.rs`
- `crates/gbackup/src/restore/tests.rs`

Load manifest, integrity, require `files.restore_verified`. Restore postgres globals then dump, qdrant snapshots, falkordb dump, files_home (port `restore_hub_files` / destination files_home / bootstrap merge). `--database-url`, `--drop-existing`, `--yes`. Without `--yes`, refuse. Skip volume restore when skipped. Reconcile restored principals. Restore releases any active epoch **in the restored target** with `released_by_command='restore'`; never release origin state.

**Acceptance:**

- 5.1.1 - Restore applies postgres, qdrant, falkordb, files from a restore-verified live backup. file: `crates/gbackup/src/restore.rs`.
- 5.1.2 - Missing `--yes` refuses destructive restore. test: `crates/gbackup/src/restore/tests.rs`.
- 5.1.3 - Unverified files store refuses restore. test: `crates/gbackup/src/restore/tests.rs`.

### 5.2 Allow skipped volumes in VerifiedBackupManifest [category: code] (depends: 2.5)
`kind: deliverable`

Targets:
- `crates/gcore/src/schema/gate.rs::VerifiedBackupManifest::verify`
- `crates/gcore/src/schema/gate.rs::StoreRecord`
- `crates/gcore/src/schema/runner.rs::apply_with_backup`
- `crates/gcore/src/schema/runner_tests.rs::*` — scope-reason: gate tests must accept live skipped volumes and still require files restore_verified
- `crates/gdaemon/src/main.rs::load_newest_backup_manifest`

A store with `details.skipped == true` and non-empty `details.reason` does not need `restore_verified`. Other stores still do. `files` still needs archive and restore verified. `parse_backup_manifest` still requires exactly the five STORE_KEYS. No schema migration.

**Acceptance:**

- 5.2.1 - Live skipped volumes pass verify when files are restore_verified. test: `crates/gcore/src/schema/runner_tests.rs`.
- 5.2.2 - Unverified files still fail the gate. test: `crates/gcore/src/schema/runner_tests.rs`.
- 5.2.3 - gdaemon still parses v3 manifests including skipped volumes. file: `crates/gdaemon/src/main.rs`.

## P6: Cold volume path and epoch
`kind: framing`

**Goal**: Operator/epoch backups can stop managed containers, tar volumes, and restart.

### 6.1 Cold backup with volume tars [category: code] (depends: 2.5)
`kind: deliverable`

Targets:
- `crates/gbackup/src/cold.rs`
- `crates/gbackup/src/stores/volumes.rs`
- `crates/gbackup/src/cold/tests.rs`

Match Python order: stop daemon (`shutdown_source cli_hub_backup`) → logical dumps (containers still up) → `_services_stop` → tar `gobby_postgres_data`, `gobby_qdrant_data`, `gobby_falkordb_data`, `gobby_pgaudit_log` → restart services in `finally` → restart daemon unless `--epoch`. Refuse volume tar if services did not stop.

**Acceptance:**

- 6.1.1 - `--cold` tars volumes only after services stop and restarts services even if tar fails. file: `crates/gbackup/src/cold.rs`.
- 6.1.2 - Volume tar while services are up is refused. test: `crates/gbackup/src/cold/tests.rs`.

### 6.2 Honor `--epoch` as hub-maintenance child [category: code] (depends: 6.1, 3.2)
`kind: deliverable`

Targets:
- `crates/gbackup/src/cold.rs`
- `crates/gbackup/src/cli.rs`
- `crates/gbackup/src/epoch.rs`
- `crates/gbackup/src/cold/tests.rs`

`--epoch <id>` requires the Python `MAINTENANCE_EPOCH_ENV` value equal to that id and hub epoch ownership. Leaves daemon stopped. Records `epoch_id` on the manifest. Implies cold.

**Acceptance:**

- 6.2.1 - `--epoch` without matching env/ownership fails. test: `crates/gbackup/src/cold/tests.rs`.
- 6.2.2 - Successful epoch backup leaves the daemon stopped and sets manifest.epoch_id. file: `crates/gbackup/src/epoch.rs`.

### 6.3 Scratch-verify volume archives on cold backups [category: code] (depends: 4.1, 6.1)
`kind: deliverable`

Targets:
- `crates/gbackup/src/verify/volumes.rs`
- `crates/gbackup/src/verify.rs`
- `crates/gbackup/src/verify/tests.rs`

Port `verify_volume_archives`. Only when `volumes` is not skipped. Used by cold runs and `gbackup verify` on a cold dir.

**Acceptance:**

- 6.3.1 - Cold backup verify extracts volume tars in a scratch dir and sets volumes.restore_verified. test: `crates/gbackup/src/verify/tests.rs`.

## P7: OS timers
`kind: framing`

**Goal**: Installing the daemon service also installs daily backup and weekly verify timers.

### 7.1 Install launchd, systemd, and Task Scheduler backup timers [category: code] (depends: 3.2, 4.1)
`kind: deliverable`

Targets:
- `src/gobby/cli/installers/backup_service.py`
- `src/gobby/install/shared/services/com.gobby.backup.plist.j2`
- `src/gobby/install/shared/services/com.gobby.backup-verify.plist.j2`
- `src/gobby/install/shared/services/gobby-backup.service.j2`
- `src/gobby/install/shared/services/gobby-backup.timer.j2`
- `src/gobby/install/shared/services/gobby-backup-verify.service.j2`
- `src/gobby/install/shared/services/gobby-backup-verify.timer.j2`
- `src/gobby/install/shared/services/gobby-backup.task.xml.j2`
- `src/gobby/install/shared/services/gobby-backup-verify.task.xml.j2`
- `src/gobby/cli/installers/service.py::install_service`
- `src/gobby/cli/installers/service.py::uninstall_service`
- `src/gobby/cli/service.py::install`
- `src/gobby/cli/service.py::uninstall`
- `src/gobby/install/bundled_content_manifest.json::*` — scope-reason: add hashes for backup timer templates
- `tests/cli/installers/test_backup_service.py`
- `tests/cli/installers/test_cli_installers_service.py::*` — scope-reason: install/uninstall coverage must include backup timer units
- `tests/cli/test_cli_service.py::*` — scope-reason: CLI install/uninstall tests cover backup timer side effects

New module — `service.py` only dispatches. Daily 07:00 local: `gbackup backup --scheduled`. Weekly Sunday 07:30 local: `gbackup verify --scheduled`. Jitter in the binary. Uninstall removes backup units. WSL uses systemd. Native Windows uses schtasks. Refresh `bundled_content_manifest.json` with the existing install-content recipe.

**Acceptance:**

- 7.1.1 - Daemon service install also installs daily and weekly backup units. file: `src/gobby/cli/installers/backup_service.py`.
- 7.1.2 - Uninstall removes those units. test: `tests/cli/installers/test_backup_service.py`.
- 7.1.3 - macOS, Linux/WSL, and Windows templates exist. file: `src/gobby/install/shared/services/gobby-backup.timer.j2`.

## P8: Python shim and deletion
`kind: framing`

**Goal**: `gobby hub-backup` execs gbackup; Python store/verify/manifest modules go away.

### 8.1 Shim gobby hub-backup to gbackup then delete Python implementation [category: code] (depends: 5.1, 6.2, 6.3, 7.1)
`kind: deliverable`

Targets:
- `src/gobby/cli/hub_backup/cli.py::*` — scope-reason: replace group with exec-to-gbackup shim
- `src/gobby/cli/hub_backup/shim.py`
- `src/gobby/cli/hub_backup/_stores.py::*` — scope-reason: delete after rust parity
- `src/gobby/cli/hub_backup/_verify.py::*` — scope-reason: delete after rust parity
- `src/gobby/cli/hub_backup/_manifest.py::*` — scope-reason: delete after rust parity
- `src/gobby/cli/hub_backup/_integrity.py::*` — scope-reason: delete after rust parity
- `src/gobby/cli/hub_backup/_content.py::*` — scope-reason: delete after rust parity
- `src/gobby/cli/hub_backup/files_home.py::*` — scope-reason: delete unless the files_home sweep proves a remaining Python consumer
- `tests/cli/hub_backup/test_cli_hub_backup_cli.py::*` — scope-reason: keep registration/shim tests, drop Python implementation tests
- `crates/gbackup/tests/parity_cli.rs`

Split the 901-line `cli.py` and move the Click group into new `src/gobby/cli/hub_backup/shim.py` so the production file stays under the ceiling. Resolve `~/.gobby/bin/gbackup`. `gobby hub-backup` → `gbackup backup` (`--output/--epoch/--json`). `gobby hub-backup restore` → `gbackup restore`. Delete `_stores.py`, `_verify.py`, `_manifest.py`, `_integrity.py`, `_content.py` after rust tests cover them. This leaf runs `gcode grep -w hub_backup src/gobby tests` and `gcode grep -w files_home src/gobby tests`; keep `files_home.py` only if a remaining Python consumer needs it. The old `cli.py` is removed or reduced to a re-export from `shim.py`.

**Acceptance:**

- 8.1.1 - `gobby hub-backup --help` still works and dispatches to gbackup. test: `tests/cli/hub_backup/test_cli_hub_backup_cli.py`.
- 8.1.2 - Python dump/verify/manifest modules are gone unless the files_home sweep proves a remaining consumer. file: `src/gobby/cli/hub_backup/cli.py`.
- 8.1.3 - Flag mapping covers output, epoch, json, restore drop-existing/yes. test: `crates/gbackup/tests/parity_cli.rs`.

## V2 End-to-end checks
`kind: verification`

- `cargo nextest run -p gobby-backup`
- `cargo nextest run -p gobby-core -E 'test(backup) | test(schema_contract) | test(gate) | test(VerifiedBackup)'`
- `cargo test --doc -p gobby-backup`
- `GOBBY_TEST_PROTECT=1 uv run pytest tests/cli/hub_backup tests/cli/installers/test_backup_service.py tests/cli/test_cutover.py -q`
- Manual: `gbackup backup --json` with daemon up; daemon stays healthy; volumes skipped; `gbackup verify` on that dir; restore without `--yes` refuses.

## V1 Plan Changelog
`kind: verification`

**Draft 1** `kind: verification`

- Decision Record confirmed interactively (live nightly, gbackup crate, OS timers including WSL, 07:00 local, weekly scratch verify, 7 integrity-ok, last-run.json only, Python shim-then-delete).
- Follow-up filed: #21145 skill MCP cap split.
- Implementation isolation: linked worktree off local `0.5.0`, named `wt-task-<task_ref>` after the task exists.
- Post-approval: interactive coordinator `set_handoff(clear_session=true)`; successor starts with `get_handoff()`.
