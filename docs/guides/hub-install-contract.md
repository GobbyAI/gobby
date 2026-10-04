# Hub Install Contract

Gobby installation provisions or connects to the configured hub and writes
its bootstrap configuration. The Python installer owns Docker provisioning;
`gdaemon` owns canonical PostgreSQL schema application and verification.
Normal runtime commands validate the schema rather than silently migrating
it.

## Existing data

Reinstallation must preserve project identities, canonical sessions,
memories, and `code_*` index data. An existing database is not an invitation
to reset datastore volumes or replace project records. Connection or schema
validation failures stop installation before destructive changes.

Legacy standalone configuration is not a current runtime connection source.
`$GOBBY_HOME/bootstrap.yaml` selects PostgreSQL through `database_url` and
records the local or remote topology. Native clients obtain their runtime
connection material through daemon grants.

Legacy wiki schema retirement is an explicit, inventory-bound maintenance
operation. It is not a requirement to preserve or recreate `gwiki_*` tables
during installation. The retirement migration preserves shared datastore
objects and unrelated records.

## Native binaries and schema changes

The schema-aware native set contains `gcode`, `gdaemon`, and `ghook`.
Workspace promotion verifies their common schema identity, installs signed
staged files through new inodes, and writes the installed identity pin only
after the complete set promotes. See
[the cutover command](cli-commands.md#gobby-cutover).

`gdaemon schema plan` is the read-only dry run both `gobby restart` and
`gobby cutover` use to prove the start half before stopping or promoting
anything. It performs the same lineage validation and pending-migration
resolution `gdaemon schema apply` performs, but creates no schema, writes no
receipts, and never takes the apply advisory lock. It deliberately does not
verify the database identity: a database head behind the embedded head is the
normal state of every migration-owing restart and of every cutover. It prints
one line:

```text
schema gobby plan: database v437, code v438, baseline_pending=false, pending_migrations=1 [438]
```

Cutover additionally refuses to build from uncommitted schema inputs
(`crates/gcore/assets/schema`, `crates/gcore/src/schema`, and the identity pin)
unless `--allow-dirty` is passed.

Every migration applies through the same `schema apply` chain, including one
that drops or rewrites data; there is no separate destructive-migration path.

The live hub's schema advances only when the daemon that will serve it starts
(`gobby start`, `gobby restart`, or `gobby cutover`). `gobby start` holds the
singleton while it starts the managed services and applies pending migrations,
including when an OS service manages the daemon: it converts its claim into the
service launch reservation only after the apply, just before handing off to the
service manager. If the start cannot own the migration, it fails with an error
naming the singleton holder instead of launching a daemon against an older
schema. A `gobby` CLI command
migrates only while it holds the daemon singleton (`$GOBBY_HOME/gobby.pid.lock`)
as a maintenance claim, which also keeps a daemon from starting until the
migration finishes. When a daemon or a starting service holds the singleton, or
the lock is unwritable, the command opens the hub at the current schema without
migrating, so a promoted but not yet restarted `gdaemon` cannot move the hub
ahead of the running daemon. With no daemon running, first install and other CLI
commands still apply pending migrations. A maintenance claim is recorded in the
lock only; `gobby.pid` names the daemon, so a finished or crashed CLI never
blocks a daemon start.

## Files and client credentials

Hub-local installation requires an existing absolute, non-root `files_home` and
preserves its content. Literal tildes are invalid. Local bootstrap rejects
`hub_daemon_url`; remote bootstrap requires that HTTP(S) owner origin and rejects
`files_home`. The owner origin must differ from the remote process's own origin.
Remote clients use the hub owner for those files; they do not provision a second
files home. See
[hub-owned files home](../architecture/hub-owned-files-home.md).

Local `gobby install` owns `$GOBBY_HOME/local_cli_token` (normally
`~/.gobby/local_cli_token`) with mode `0600`. A database-unreachable install
can still create the token file; daemon startup adopts its hash into the
authentication configuration. Additional trusted client machines receive
the same token with the same permissions.

Remote installation requires the copied token and secret key; it never generates
or rotates the shared token. Its authenticated owner-profile probe must succeed
before datastore probes. Preserve each machine's own `machine_id` and register its
ordinary checkout roots separately. A hub-visible project is not proof of a local
checkout. See [machine and project ownership](shared-stack.md#machine-and-project-ownership).

`gobby auth token --rotate` replaces the token and its stored hash. Copy
the new token to additional client machines after rotation. Stateful daemon
HTTP and WebSocket surfaces require authentication.

_Last verified: 2026-09-12_
