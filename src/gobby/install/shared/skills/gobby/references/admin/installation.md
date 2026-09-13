# Installation

Load before operator setup, repair, component reinstall, cutover, or removal.
Start with `uv run gobby install --help` and the system requirements guide.
Identify local hub versus remote node before selecting an installation path.

For a local hub, provision an existing absolute `files_home` directory before
installation. It holds hub-owned profile, personal files, and chat attachments;
it is separate from datastore volumes. The installer validates that root and
uses maintenance ownership while publishing its identity. Local installation
provisions managed PostgreSQL, Qdrant, and FalkorDB through Docker Compose.

1. Inspect the existing bootstrap and installation status without displaying
   credentials. Coordinate a quiet window before operations requiring the
   stopped-daemon singleton.
2. Run the full installer with `--files-home` on initial local setup. Bare
   installation detects supported provider CLIs, configures hooks, establishes
   account identity, and reconciles optional sections. Noninteractive execution
   requires `--no-interactive`; it is not implicit permission to replace state.
3. For an existing installation, named components run only those components,
   in argument order after deduplication. Consult current help for supported
   names and embedding overrides; `--embedding-provider` requires `--embedding-url`.
4. Read every component result. Resolve failed dependencies or unavailable
   services before treating installation as complete. Verify daemon readiness
   and each affected provider's MCP/hook connection.

A remote bootstrap requires `hub_daemon_url`, rejects `files_home`, and uses
the hub's existing local CLI token. Remote installation checks connectivity
and authentication and skips local managed-service provisioning. It does not
create or rotate the hub token. Load `portability.md` before moving state.

`gobby datastores expose` is a transitional, local-hub operator procedure.
It validates the bind/published host, stages the service bind, checks readiness,
then publishes shared endpoints; failures attempt to restore the previous
Compose state. Its existence does not authorize network exposure or establish
the future thin-node topology. See the requirements guide's remote-mode limits.
For Qdrant-specific repair, `gobby qdrant install --port PORT` reinstalls its
Compose service and reports the configured URL; verify status and coordinate
the requested daemon restart.

Use the release guide and `gobby cutover --help` for coherent native binary
promotion. Coordinate its daemon restart; do not overwrite signed installed
binaries in place or hand-edit schema identity pins. Check installed rows after
sync instead of inferring active policy from templates. Load the skills and
integrations capabilities for their respective install-specific procedures.

`gobby init -C <checkout>` registers the project, attempts initial code indexing,
and installs applicable Git hooks. Indexing failure is reported separately from
project initialization; inspect the output before assuming the index is ready.

`gobby sync` reconciles bundled definitions into the database. Production sync
checks integrity; another source checkout is refused without an explicit override.
`--verify-only --fail-on-verify` checks integrity without syncing. Development
mode skips that check and says so. `--reinstall` deletes and reinstalls only the
selected bundled definitions, preserving user/project definitions; inspect its
confirmation and current help before use. Do not use `--force` as routine repair.

`gobby uninstall COMPONENT...` removes only named components; `git-hooks` uses
the checkout selected with `-C`. Bare uninstall removes detected provider hooks,
global hook dispatchers, managed RTK/Impeccable, and UI exposure. It preserves
datastore containers/volumes, bootstrap, secrets, and the files home; it is not
a data reset or package removal.

An installer failure is not a reason to delete volumes, reset credentials, or
force synchronization from another checkout. Preserve the failing output and
repair the named preflight or component. Load `recovery.md` for schema refusal.

See [requirements](../../../../../../../../docs/guides/system-requirements.md),
[install options](../../../../../../../../docs/guides/cli-commands.md#gobby-install-and-gobby-uninstall),
and [native releases](../../../../../../../../docs/guides/release-guide.md).
