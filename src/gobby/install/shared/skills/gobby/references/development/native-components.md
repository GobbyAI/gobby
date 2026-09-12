# Native components

Load before changing a native component, choosing its validation or diagnosing
a source/installed-binary mismatch. Load [obligations](obligations.md), then
`rust` before Rust edits and the applicable crate instructions.

| Owner | Responsibilities | Human guide |
| --- | --- | --- |
| `gobby-code` → `gcode` | Code facts, parsing, index/search and projections | [gcode](../../../../../../../../docs/guides/gcode-development-guide.md) |
| `gobby-core` | Shared identity, grant, configuration and datastore primitives | [gobby-core](../../../../../../../../docs/guides/gcore-development-guide.md) |
| `gobby-hooks` → `ghook` | Provider hook transport and response contracts | [ghook](../../../../../../../../docs/guides/ghook-development-guide.md) |
| `gobby-terminal` → `gterm` | Native PTY host and semantic frames | [terminal build](../../../../../../../../docs/guides/gterminal-development-guide.md#build) |
| `gobby-client` → `gclient` | Workspace TUI, public daemon API and frame client | [client status](../../../../../../../../docs/guides/gterminal-development-guide.md#client-status) |

`gobby-daemon` → `gdaemon` owns native hub schema/operator work. Installation,
schema application and daemon lifecycle belong to admin procedures. Contributor
guidance does not authorize production schema or credential changes.

## Preserve boundaries

Use shared core infrastructure only when multiple consumers need it; keep domain
facts and command rendering in their owners. Check Cargo features independently:
all-features success can hide missing feature dependencies. Runtime clients use
daemon-issued grants; normal commands do not apply hub schema. Low-level marker
discovery does not register a checkout. Follow typed recovery on missing grants,
checkout identity, service availability or schema mismatch.

Use [code-index guidance](../code-index/overview.md) for indexing/navigation
operations. Native graph transport belongs to core; graph facts and projection
policy belong to gcode. `ghook --diagnose --cli codex --type SessionStart` is a
non-dispatch probe; `ghook schema-identity --json` does not apply schema.
`ghook --version` writes a runtime stamp. Fabricated `--gobby-owned` events and
spool recovery require isolated state, not live sessions.

## Build, install and verify

Run focused package/test commands named by the relevant guide. A source build
does not change the daemon's installed binary. After actual crate changes,
coordinate and reinstall via a new inode (copy to a dotfile, then rename over
the binary); compare built and installed hashes. Never overwrite an executing
signed macOS binary in place. `gterm` needs `--features vt-engine --bin gterm`
and its supported Zig/SDK toolchain; a library-only build does not validate it.
`gclient` builds without Zig. See
[rebuild and reinstall](../../../../../../../../docs/guides/gterminal-development-guide.md#rebuild-and-reinstall).

gcode database tests apply schema in a dedicated database's `public` namespace.
Use an owned disposable `_test` database on the isolated hub, explicitly set
`GCODE_POSTGRES_TEST_DATABASE_URL`, enable its `pg_search` extension, and supply
a temporary `GOBBY_HOME`/fixture machine identity. Do not reset the shared Python
test database when Rust provisioning reports foreign lineage. See
[database fixtures](../../../../../../../../docs/guides/gcode-development-guide.md#contributor-database-fixtures).

Managed macOS SRT runs permit sockets inside their canonical current-run temp
directory, including nested paths. Use `CLAUDE_CODE_TMPDIR` or run `TMPDIR`,
short unique paths (encoded Unix socket path below 104 bytes), and clean owned
processes/files even on failure. Other runs and symlink escapes are excluded;
operator grants remain separate and `allowAllUnixSockets` remains false.
Linux/WSL2 retain socket restrictions while allowing run-local file writes.
Web-chat permissions are separate.

Terminal Guard H groups 2, 3 and 6, runtime-contract tests and external attach
can require live sockets. Record OS/provider/run, commit, policy/temp path,
commands/results and process sets before/after. Skipped or policy-denied cases
are UNVALIDATED; the coordinator must obtain the required platform evidence.
A parent-shell pass does not replace explicitly required spawned-agent proof.
Never kill baseline operator hosts to hide a leak. See
[sandbox validation](../../../../../../../../docs/guides/gterminal-development-guide.md#sandboxed-validation-by-operating-system).

Inspect installed configuration before declaring the effective terminal backend
or any rule active. Template defaults are source evidence only. Native spawning
requires an available host and has no silent tmux fallback; use typed failures
and the relevant agent/session recovery procedure.
