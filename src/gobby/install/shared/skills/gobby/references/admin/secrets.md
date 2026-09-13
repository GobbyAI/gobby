# Secrets

Load before storing, locating, deleting, or rewrapping credentials. Discover
operator options with `uv run gobby secrets --help`. Use managed configuration
references such as `$secret:NAME` instead of embedding secret values in files.

1. Select scope explicitly when it matters. `--global` and `--project` are
   mutually exclusive. Without either, the CLI uses the registered current
   project when available, otherwise global scope. An unknown explicit project
   fails; it does not silently broaden to global.
2. Inspect `secrets list` metadata or `secrets get NAME`. The latter checks
   existence and does not reveal the value. Missing entries return failure.
3. Store through `secrets set NAME`, using its hidden prompt or `--stdin`.
   Empty values are rejected; stdin is stripped. Category and description are
   metadata, not authorization or configuration activation.
4. Update the intended config consumer to reference the stored name using the
   config capability. Verify scope resolution and the consumer's result without
   exposing credential contents.
5. Before deletion, identify consumers and the exact scope. A missing or
   undecryptable credential is not a reason to reset the whole secret store.

`secrets rekey --posture key-file|passphrase` rewraps the data-encryption key;
it does not re-encrypt every secret value. Passphrase posture requires supported
passphrase delivery to the runtime, including noninteractive services. Preserve
the required key material and recovery information with backups. Load
`backups.md` and `recovery.md` before a migration or recovery attempt.

Secret HTTP mutation routes require authentication, including on loopback.
Agent configuration schemas remain authoritative for managed
secret operations; never bypass task or configuration lifecycle with direct SQL.

Datastore passwords have a dedicated operator command:
`gobby datastores rotate-password postgres|falkordb`. Install reruns preserve
existing credentials. Rotation is local-hub only; PostgreSQL updates the role
and bootstrap DSN, while FalkorDB updates its managed secret and requires a
restart to recreate the container with the new password. Coordinate the window
and verify service readiness and client access afterward.

See [secrets](../../../../../../../../docs/guides/admin-operations.md#secrets)
and the [secret contract](../../../../../../../../docs/contracts/secrets.md).
