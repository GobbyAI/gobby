# Hub-PC Datastore Move Runbook

> **Plan ID:** `hub-pc-datastore-move`
>
> **Owner epic:** #19379.19654
>
> **Planning task:** #19656

## Context
`kind: framing`

Move the authoritative PostgreSQL, Qdrant, and FalkorDB stores from the current
hub host to Hub-PC. The move changes the physical datastore host, not the M0
architecture: Hub-PC owns the managed Docker services, while each development
machine continues to run its own daemon against the shared stores.

The source remains the rollback authority until the target has passed every
acceptance gate. The procedure never removes source containers or volumes,
never restores a raw PostgreSQL volume, and never calls `docker rm -f` or
`docker volume rm`. PostgreSQL moves through the verified logical dump; Qdrant,
FalkorDB, and pgAudit logs move through the verified volume archives produced by
`gobby hub-backup`.

The current `gobby hub-backup restore` command restores PostgreSQL only. This
runbook says so explicitly and uses guarded, one-shot volume extraction for the
other stores rather than pretending a unified restore command exists.

## Locked Decisions
`kind: framing`

1. Hub-PC runs the exact same Gobby commit, Python environment, Rust binaries,
   Compose template, and container image digests as the source during cutover.
2. Hub-PC uses `datastore_mode: local`. Every other daemon uses
   `datastore_mode: remote` and a non-loopback PostgreSQL DSN naming Hub-PC.
3. Hub-PC exposes PostgreSQL `60891`, Qdrant `6333`, and FalkorDB `16379` only
   on one concrete Tailscale IPv4 address. Wildcard binds and public-LAN binds
   are forbidden. The FalkorDB browser stays loopback-only.
4. Tailscale ACLs admit those three ports only from the approved Gobby machines.
   API ports `60887` and `60888` remain governed separately by daemon auth and
   are not opened by this datastore move.
5. The existing shared `.secret_kek` and `local_cli_token` are copied through an
   authenticated, encrypted channel with mode `0600`. They are never put in the
   backup directory, command history, plan, or Git.
6. Hub-PC receives a new machine identity. The source `machine_id` artifact is
   retained for evidence but is not installed on Hub-PC. Stored records keep
   their original machine attribution.
7. Source services stay stopped after the final backup. The old volumes remain
   intact for seven days after acceptance, then require a separate operator
   decision; cleanup is not part of this runbook.
8. A two-hour maintenance window is reserved. The rehearsal establishes the
   expected duration; the live estimate is `max(60 minutes, rehearsal duration
   × 1.3)` capped at 120 minutes. Failure to reach the target verification gate
   by minute 90 triggers rollback, reserving 30 minutes to restore service.

## P0: Operator-Safe Move Plan
`kind: framing`

### 0.1 Commit the Hub-PC datastore move runbook [category: docs]
`kind: deliverable`

Target: `.gobby/plans/hub-pc-datastore-move.md`

The committed runbook is the cutover contract. Execution may substitute only
worksheet values such as hostnames, addresses, hashes, timestamps, and the
fresh restore nonce; changing a safety gate requires a reviewed plan revision.

**Acceptance:**
- 0.1.1 - The runbook specifies prerequisites, a synthetic non-production
  rehearsal, stop-consistent verified backup and transfer, per-store restore,
  configuration cutover, exact verification, downtime, and rollback. file:
  `.gobby/plans/hub-pc-datastore-move.md`.
- 0.1.2 - Destructive target writes require the approved Hub-PC identity, an
  exact allowlisted volume ID, the current restore nonce, no live mount, and a
  manifest-matching artifact; source and test resources cannot satisfy the
  capability. file: `.gobby/plans/hub-pc-datastore-move.md`.
- 0.1.3 - The plan uses the public PostgreSQL-only restore command truthfully,
  preserves source data, and contains no `docker rm -f` or volume-removal step.
  file: `.gobby/plans/hub-pc-datastore-move.md`.

## Preconditions
`kind: framing`

Record a signed cutover worksheet before the maintenance window. It contains:

- source and target hostnames, Tailscale IPv4 addresses, machine IDs, disk free
  space, UTC clock skew, Gobby commit/version, and container image digests;
- the approved DNS/MagicDNS name clients will use, with a verified TTL no more
  than 60 seconds, plus its pre-cutover value;
- SHA-256 hashes of both bootstrap files and the three shared secret files,
  recording only hashes and modes, never contents;
- current `gobby status`, `docker compose ps`, `docker volume inspect`, and
  maintenance-epoch status from the source;
- an inventory of every daemon using the hub and an operator assigned to stop,
  reconfigure, and restart each one;
- the backup destination and transfer destination, both on encrypted storage
  with mode `0700`, and at least three times the source datastore size free;
- a tested source-host restart command and the timestamped bootstrap copies
  required for rollback.

The window does not begin unless all daemons are on the same commit, no schema
migration is pending, no maintenance epoch is open, no agent/task transition is
in flight, the target has no containers or volumes named `gobby_*`, and the
backup destination is absent. A pre-existing target `gobby_*` resource is a hard
stop, not something this procedure deletes or adopts.

## R0: Non-Production Rehearsal
`kind: framing`

Rehearse the complete sequence on Hub-PC before scheduling the live move:

1. Use synthetic PostgreSQL, Qdrant, and FalkorDB fixtures. PostgreSQL must use
   only
   `postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test` with
   `GOBBY_TEST_PROTECT=1`; no rehearsal command may resolve the production DSN.
2. Use rehearsal-only containers, networks, directories, and volumes whose
   names begin `gobby_move_rehearsal_`. No production or ordinary test volume is
   mounted, renamed, cleared, or removed.
3. Run the same backup integrity checks, transfer, nonce-labelled volume
   creation, extraction, PostgreSQL logical restore, configuration edits, and
   verification queries specified below.
4. Inject one failure during archive transfer, one during volume extraction,
   and one before client cutover. Prove each leaves the source fixture usable
   and the target fixture fenced.
5. Record elapsed times and peak disk use for backup, transfer, volume restore,
   PostgreSQL restore, verification, and rollback. The signed worksheet and a
   successful rerun are required evidence for the live window.

Rehearsal cleanup may remove only resources carrying both the rehearsal prefix
and the current rehearsal nonce. Cleanup refusal is success when either fact is
missing. The live migration nonce is never reused for rehearsal.

## R1: Freeze and Final Verified Backup
`kind: framing`

1. Stop every remote/client daemon first with `gobby stop`. Confirm their
   status is stopped and that no daemon process still has a connection to the
   source PostgreSQL server.
2. On the source, run `gobby stop` without `--docker`. This stops the daemon but
   leaves the managed stores available to the backup command. Confirm no Gobby
   daemon is running. Do not use `hub-backup --epoch` directly: that flag is
   reserved for a child of `gobby hub-maintenance` with the matching open epoch.
3. Run `uv run gobby hub-backup --output <new-absolute-directory> --json`.
   Because the daemon was already stopped, the command leaves it stopped. A
   successful command must publish `gobby-hub-backup-manifest-v2.json` and report
   archive and scratch-restore verification for PostgreSQL, Qdrant, FalkorDB,
   and the Docker-volume archives.
4. Run `gobby stop --docker` and confirm all source managed containers are
   stopped. From this point until acceptance or rollback, starting a source
   service is forbidden.
5. Hash the manifest and every artifact again. Compare each path, size, and
   SHA-256 with the manifest. Refuse symlinks, unexpected files, unverified
   stores, and a source identity that differs from the pre-cutover worksheet.
6. Transfer the whole directory over authenticated SSH on the tailnet using a
   checksum-verifying, resumable copy. Re-run the manifest verification on
   Hub-PC; byte counts and SHA-256 hashes must match before restore starts.

The backup directory is immutable after verification. A failed or partial
backup is left with its diagnostics and cannot be promoted as migration input.

## R2: Prepare Hub-PC Without Adopting Existing Data
`kind: framing`

1. Install the pinned Gobby build and its managed Compose template on Hub-PC.
   Record version and image-digest parity before any restore.
2. Write Hub-PC bootstrap configuration with `datastore_mode: local`, the
   source PostgreSQL credentials, `services_bind_address` set to the approved
   Hub-PC Tailscale IPv4 address, and daemon authentication required. Set file
   mode `0600` and copy the shared KEK/token files through the approved secret
   channel.
3. Generate a cryptographically random live restore nonce. Before the install
   may create data, create the four exact named volumes
   `gobby_postgres_data`, `gobby_pgaudit_log`, `gobby_qdrant_data`, and
   `gobby_falkordb_data` with labels identifying the target machine ID, task
   `19656`, and this restore nonce.
4. Run the local install once to materialize configuration and verify the pinned
   services can start. Stop the daemon and Compose stack normally. Do not pass
   `--docker` to any command that would address the source host.
5. Before each volume write, verify all of the following: the command is on the
   recorded Hub-PC host; the exact volume name is in the four-name allowlist;
   all three labels equal the worksheet; no container currently mounts the
   volume; and the archive path/hash equals the manifest. Any mismatch aborts
   the restore.

The restore helper container may use Docker's ordinary `--rm` lifecycle after
it exits. It must not use `docker rm -f`, remove a volume, address a container by
friendly/random name, or mount any source/test volume. Immutable volume ID plus
restore nonce is the destructive-write capability.

## R3: Restore the Datastores
`kind: framing`

Restore only onto the fenced Hub-PC volumes:

1. Clear the installer-created contents of `gobby_qdrant_data`,
   `gobby_falkordb_data`, and `gobby_pgaudit_log` through a one-shot helper that
   enforces the R2 label/host/no-mount gates. Extract the corresponding verified
   `volumes/<name>.tar.gz` archive into the empty volume. Do not extract
   `volumes/gobby_postgres_data.tar.gz`; cross-host PostgreSQL restore is logical.
2. Start the Hub-PC Compose services with the daemon still stopped. Wait for
   PostgreSQL, Qdrant, and FalkorDB health checks to pass on loopback and the
   approved tailnet address.
3. Restore PostgreSQL with
   `uv run gobby hub-backup restore <backup-directory> --database-url
   <explicit-Hub-PC-DSN> --clean --yes`. The DSN must resolve to Hub-PC and must
   differ from both the source DSN and the protected test DSN. The restore
   command validates the manifest, restores globals before the logical dump,
   and reconciles ephemeral principals.
4. Keep every daemon stopped while datastore verification runs. A restore
   warning, skipped artifact, hash mismatch, role mismatch, or health timeout is
   fatal; do not continue to client configuration.

## R4: Datastore Verification
`kind: framing`

Run these read-only checks against Hub-PC and attach output to the worksheet:

1. PostgreSQL identity and schema receipt:

   ```sql
   SELECT current_database(), current_user,
          inet_server_addr(), inet_server_port(), version();
   SELECT version, name, checksum
   FROM schema_migrations
   ORDER BY version;
   ```

   The server address must be Hub-PC. The migration rows, source roles, public
   table count, and public index/constraint/trigger/function counts must match
   the manifest. For every public table, run an exact `count(*)` and compare it
   with `row_count_probes`; zero-row tables are included.

2. Qdrant: list collections, obtain an exact point count for each collection,
   and compute the canonical content digest. Collection names, point counts,
   and SHA-256 values must equal
   `stores.qdrant.details.collections` in the manifest.
3. FalkorDB: run `GRAPH.LIST` and `DBSIZE`; for every graph run read-only node
   and relationship count queries. The graph list, per-graph counts, and DB size
   must equal `stores.falkordb.details` in the manifest.
4. Docker volumes: recompute member count, total uncompressed bytes, and the
   canonical content digest for each restored volume and compare them with
   `stores.volumes.details.source_inventories`.
5. Security: confirm the three datastore listeners bind only to loopback and
   the approved Tailscale IPv4, verify the ACL from one approved and one denied
   host, and confirm no secret appears in process arguments or worksheet output.

Any mismatch triggers R7 rollback. The runbook does not permit accepting a
partial store and rebuilding it later.

## R5: Publish Hub-PC and Repoint Clients
`kind: framing`

1. On Hub-PC, run `gobby datastores expose --bind <approved-ts-ipv4> --host
   <approved-dns-name>`. Reconfirm the three shared endpoints in `config_store`
   and cold-start the Hub-PC daemon. Hub-PC is the only daemon started for the
   first smoke test.
2. Exercise task read/list, memory search, gcode search, wiki search, session
   creation, and one reversible test task transition through Hub-PC. Remove the
   test record through the normal task lifecycle; do not write ad hoc SQL.
3. Update the DNS alias to Hub-PC and wait for the recorded TTL. On each other
   machine, atomically replace bootstrap configuration with
   `datastore_mode: remote` and the Hub-PC PostgreSQL DSN. Preserve a timestamped
   mode-`0600` pre-cutover copy. Do not copy Hub-PC's `machine_id`.
4. Start one client daemon at a time. Its remote-mode preflight must reach all
   three stores without Docker, and its local machine ID must remain unchanged.
   Repeat the smoke tests and verify newly written records carry that client's
   machine attribution.
5. Convert the old hub host last: stop its old Compose stack, switch its daemon
   to remote mode, and start only the daemon. Confirm no old local datastore
   container has restarted.

## R6: Acceptance and Observation
`kind: framing`

Cutover is accepted only when:

- every manifest comparison in R4 is exact;
- every expected daemon reports healthy against Hub-PC and no daemon is still
  connected to the source stores;
- task, memory, gcode, wiki, session, and machine-attribution smoke tests pass
  from Hub-PC and one remote client;
- a restart of Hub-PC services preserves the exposed bind address and all three
  endpoints, followed by a clean daemon restart;
- daemon logs contain no task-owned warning or error for five continuous
  minutes after the final restart; any such diagnostic is fixed and the
  five-minute window restarts from zero;
- the cutover worksheet records start/end times, downtime, backup manifest
  hash, target identities, verification outputs, bootstrap hashes, and the
  operator's acceptance timestamp.

Provider-capability warnings are outside this task and remain owned by session
#10186; they are not altered or suppressed by this runbook.

## R7: Rollback
`kind: framing`

Rollback is mandatory on any fatal gate or at minute 90 without acceptance:

1. Stop all client and Hub-PC daemons. Stop the Hub-PC Compose services normally.
   Do not delete target containers, volumes, or the transferred backup.
2. Restore the DNS alias and every machine's timestamped bootstrap file. Verify
   hashes and mode `0600`; confirm the old hub is again `datastore_mode: local`
   and every other machine names the source datastore host.
3. Start the unchanged source Compose stack and source daemon. Verify the same
   pre-cutover PostgreSQL identity, schema receipt, Qdrant inventory, and
   FalkorDB inventory, then start clients one at a time.
4. Run the task/memory/gcode/wiki/session smoke tests and observe source daemon
   logs for five continuous clean minutes. Record the failed gate and rollback
   completion in the worksheet.

Target artifacts remain fenced for diagnosis. Cleanup is a later, explicit
operation requiring the target machine identity and restore nonce; rollback
itself never performs destructive cleanup.

## V1: Plan Verification
`kind: verification`

- The runbook names the current public backup and PostgreSQL restore commands
  exactly and does not claim that `hub-backup restore` restores other stores.
- Backup, transfer, restore, configuration, read-only verification, security,
  downtime, acceptance, and rollback all have fail-closed gates.
- Rehearsal uses only synthetic data and the protected `gobby_test` PostgreSQL
  DSN; production data and ordinary test data are never test inputs.
- No step invokes `docker rm -f`, removes a Docker volume, adopts a pre-existing
  target resource, restores the source machine ID, or starts both old and new
  authoritative stacks.
- The move remains under root epic #19379 through planning epic #19654; it is
  not an orphan deferral.
