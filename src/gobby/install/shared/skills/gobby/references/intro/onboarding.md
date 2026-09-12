# Onboarding

Load before resolving working-profile storage, installation prerequisites or
fresh-session injection. For content editing, load [profile](profile.md).

## Resolve the owner

Read only relevant bootstrap fields: `datastore_mode`, `files_home` and
`hub_daemon_url`. Do not expose database credentials or CLI tokens. Local mode
requires an existing absolute files-home directory. Remote mode has no local
files home and routes profile reads/writes to the configured hub owner.
`daemon_url` identifies the current process, which may not be that owner.

Current Python bootstrap, profile transport and the
[hub install contract](../../../../../../../../docs/guides/hub-install-contract.md#files-and-client-credentials)
support these two modes. Do not infer new deployment support or a physical
multi-machine acceptance result from a passing local profile test.

## Operator prerequisites

Installation, migration and daemon restarts are operator procedures, separate
from an ordinary profile update:

1. Provision an existing absolute bind directory on the hub, conventionally
   `$GOBBY_HOME/files`; do not use `$GOBBY_HOME` or its legacy `personal` tree.
2. Run the full local `gobby install --files-home <existing-absolute-directory>`.
   Remote installation refuses `--files-home`; configure its hub origin and
   securely provision the hub's existing credential instead.
3. For legacy files, upgrade or stop remote writers, collect their remaining
   files at the hub's legacy source locations, then run `gobby files migrate`
   while the hub daemon is stopped. Start only after migration completes.

Migration does not collect files from other machines, create the files-home root,
or overwrite divergent destinations. Preserve both versions on divergence. A
partial migration needs inspection of the error, destinations and remaining
sources before retry; never delete sources merely to make it pass. A successful
run returns the published/skipped inventory; failure need not return that report.
Follow the [files-home migration contract](../../../../../../../../docs/architecture/hub-owned-files-home.md#migration).
Routine profile editing needs neither reinstallation nor migration.

## Session injection

Profile reading seeds `user_profile_content` in session variables. The bundled
`inject-user-profile` rule injects nonempty content at `session_start` when source
is not `resume` and the session is not spawned. Empty/missing profile content is
not injected. Handled profile/OS read errors are logged and seed empty content;
bootstrap and files-home ownership errors require operator recovery.

Before claiming this rule is active, inspect
`gobby-workflows:get_rule(name="inject-user-profile")` through the schema gate
and check effective session rule selection. A template's enabled value is not
installed-state evidence. A file update does not reload existing context or make
spawned/resumed sessions satisfy the fresh-session condition.

If injection is absent, check owner reachability, profile content, logs, the
seeded variable and actual rule conditions. Do not widen the rule or copy private
profile text into a repository to bypass the boundary. See
[getting started](../../../../../../../../docs/guides/README.md#getting-started).
