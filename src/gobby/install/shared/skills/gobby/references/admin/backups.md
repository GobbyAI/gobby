# Backups

Load before operator backup, restore verification, or backup retention changes.
Use `uv run gobby hub-backup --help` for restore-verified hub snapshots and
`gobby pack --help` for portable archives. Task JSONL backups belong to the
tasks capability; configuration export is not a complete hub backup.

1. Identify the source hub, Gobby home, files home, datastore endpoints, and
   exact destination. Use a new output directory outside the files being
   archived. Coordinate downtime before a real backup.
2. Run `gobby hub-backup --output DIRECTORY --json`. Preflight checks the
   managed datastore target and available storage. The command stages its
   output, backs up stores and hub files, verifies artifacts and restore
   evidence, then publishes the completed backup and manifest.
3. Inspect the manifest and each store's archive and restore verification.
   A directory or archive existing does not establish a successful backup.
4. Keep the manifest with every referenced artifact and required secret
   recovery material. Protect their permissions and avoid credential-bearing
   command output in shared evidence.
5. Rehearse recovery against an isolated target before relying on the backup.
   Retention cleanup requires identifying the exact backup set and preserving
   a usable recovery point; there is no implied permission to prune volumes.

The ordinary command stops the daemon for its maintenance window and restarts
it when it had been running. `--epoch` is reserved for a matching
hub-maintenance child invocation and leaves lifecycle ownership with that
campaign. A manually supplied epoch is not a valid substitute.

`hub-backup restore` requires an explicit PostgreSQL target, verifies the
manifest and artifacts, and requires the daemon stopped. It restores hub files,
PostgreSQL globals and data, and reconciles principals. It does not automatically
restore every Qdrant/FalkorDB artifact in the backup. Load `recovery.md` before
using it and verify the individual store recovery procedures.

Use `portability.md` for pack/unpack. Their dry-run output is an inventory,
not evidence that the eventual restore will succeed.

See [disaster recovery](../../../../../../../../docs/guides/cli-commands.md#hub-backup-disaster-recovery)
and [admin operations](../../../../../../../../docs/guides/admin-operations.md).
