# Recovery

Load for failed startup, native/schema disagreement, interrupted maintenance,
or restoring an installation. These are operator procedures. Begin with
`gobby health`, `gobby status`, and the original failure; establish the target
hub and machine before choosing a repair.

1. For a token mismatch, load `authentication.md`. For an unavailable datastore,
   inspect its configured endpoint and service health. Do not reset volumes to
   repair a connectivity problem.
2. For native schema disagreement, use the coherent cutover procedure from the
   release guide. Startup and restart guards protect the running state; do not
   hand-edit identity pins or run a mismatched binary against the hub.
3. For destructive schema maintenance, discover `gobby hub-maintenance` and
   `gobby schema` options. A campaign obtains a verified epoch-bound backup,
   applies its guarded change, verifies the postcondition, and releases the
   fence. A failed campaign retains state for diagnosis and resumption.
4. Use `hub-maintenance status` before `resume`. Resume consumes the recorded
   hub state. `abort` records an explicit partial-state disposition and releases
   the fence; it is not an automatic rollback or a way to claim verification.
5. For restore, inspect the backup manifest and stop the daemon. The
   `hub-backup restore` command requires `--database-url` naming the intended
   target. `--clean` drops database objects; `--yes` suppresses confirmation.
   Neither is an ordinary diagnostic option. Verify artifacts, files, schema,
   principals, datastore health, and client access before reopening work.

Keep an interrupted restore stopped until its partial state is understood.
For a standalone PostgreSQL archive, `gobby postgres restore DUMP_OR_DIRECTORY`
targets the configured database and refuses while the daemon is running.
It verifies the backup unless the operator explicitly uses `--allow-unverified`.
`--clean` drops existing database objects; `--yes` skips confirmation. These
options do not select an isolated target: configure and verify that target first.
Retain the primary failure and any cleanup/restart failure as separate evidence.
Use isolated fixtures or temporary services for rehearsal; the user's daemon
database is never a test target.

For native schema diagnostics, `gdaemon schema version --json` prints the
binary's embedded identity without opening the hub. `gdaemon schema verify`
checks the configured hub's current schema after enforcing checkout identity;
its report counts checked receipts, seed rows and catalog objects. Preserve an
identity mismatch and coordinate the normal cutover with its owner. Direct
`schema apply` and `sweep-test-schemas` are migration/test implementation
entrypoints; use the documented Python maintenance workflow for live recovery.

`gobby files migrate` is hub-local and requires a provisioned `files_home` and
the stopped-daemon maintenance singleton. Upgrade or stop remote writers first;
bring any node-local legacy files to the hub's legacy source locations before
running it. Inspect its report and verify profile, personal files, and attachments
before startup. It does not collect files from other machines.

Physical M0 acceptance remains separate work in #19600. ROADMAP.md already
settles the architecture: the single-active Python bridge is transitional;
thin nodes perform machine-local duties and datastores stay on the hub.
Historical remote-runbook instructions predate current files-owner preflight.
Preserve that implementation limitation and the physical acceptance obligations
without presenting future thin-node operation as shipped.

See [hub installation contract](../../../../../../../../docs/guides/hub-install-contract.md),
[backup recovery](../../../../../../../../docs/guides/cli-commands.md#hub-backup-disaster-recovery),
and [physical acceptance](../../../../../../../../docs/guides/remote-docker-acceptance.md).
