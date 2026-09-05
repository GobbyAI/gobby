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

A destructive schema change uses `gobby hub-maintenance run schema-apply`.
The campaign stops the daemon, opens a maintenance epoch, obtains a verified
epoch-bound hub backup, applies the guarded schema change, verifies its
postcondition, and releases maintenance before starting the daemon.
Failure leaves the epoch available for `gobby hub-maintenance resume`.

## Files and client credentials

Hub-local installation requires an existing absolute `files_home` and
preserves its content. Remote clients use the hub owner for those files;
they do not provision a second files home. See
[hub-owned files home](../architecture/hub-owned-files-home.md).

`gobby install` owns `$GOBBY_HOME/local_cli_token` (normally
`~/.gobby/local_cli_token`) with mode `0600`. A database-unreachable install
can still create the token file; daemon startup adopts its hash into the
authentication configuration. Additional trusted client machines receive
the same token with the same permissions.

`gobby auth token --rotate` replaces the token and its stored hash. Copy
the new token to additional client machines after rotation. Stateful daemon
HTTP and WebSocket surfaces require authentication.
