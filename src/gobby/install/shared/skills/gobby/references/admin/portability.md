# Portability

Load before operator machine migration, pack/unpack, or moving hub-owned files.
Discover `gobby pack --help`, `gobby unpack --help`, and `gobby files --help`.
Identify source and destination homes, files roots, machine identities, project
checkouts, datastore targets, and available recovery artifacts first.

1. Preview `gobby pack --dry-run`. Its inventory can include bootstrap,
   machine identity, secret material, transcripts, summaries, services, hooks,
   certificates, scripts, the current project's `.gobby`, hub-owned files,
   supported Docker volumes, and a configured PostgreSQL logical dump.
2. Coordinate the real pack's daemon/service downtime. Keep the archive outside
   source trees and preserve its restricted permissions. `--no-transcripts`
   and `--no-docker` deliberately omit data; record those omissions.
3. Provision the destination's existing files-home root and bootstrap before
   unpacking. Preview the archive with `unpack --dry-run`; this lists contents
   without proving complete extraction validation or restore success.
4. Restore only to the intended target. `--force` skips the overwrite prompt;
   it does not make a conflicting destination safe. `--no-postgres` and
   `--no-docker` skip those restore surfaces, not every service lifecycle action.
5. Preserve the destination machine identity for a new machine. Unpack skips
   archived `machine_id` unless `--restore-identity` explicitly selects
   same-machine disaster recovery. The destination's configured files-home
   root is retained when bootstrap is merged.
6. Check each store and restored file set, reinstall affected provider clients,
   verify authentication, and register destination checkout roots. A hub project
   record does not establish that its checkout exists on this machine.

Unpack may stop services, restore PostgreSQL, reinstall repository Git hooks,
and restart previously running components. An extraction failure can leave
partial state and stopped services; diagnose before resuming. Load `recovery.md`.
Do not describe a portable archive as a byte-for-byte copy of every local file.

The profile, personal tree, and chat attachments belong to the hub's files
home. Remote bootstrap supplies `hub_daemon_url` and rejects `files_home`.
`gobby files migrate` is a stopped-daemon, hub-owned campaign: collect remaining
legacy sources on the hub first, preserve divergent destinations, and verify
publication before retiring sources. It does not collect files from other nodes.

See [pack/unpack](../../../../../../../../docs/guides/admin-operations.md#pack-and-unpack)
and [files ownership](../../../../../../../../docs/architecture/hub-owned-files-home.md).
