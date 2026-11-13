# #24657 Rust-only node install and enrollment: research notes

Plan Writer gobby#16767, 2026-10-18. Draft target: `.gobby/plans/node-install-enrollment.md`.

## Sources read
- Task #24657 description and validation criteria (3 criteria; criterion 3: each leaf names
  the Python surface it replaces and a test proving the node path runs with no Python on PATH).
- Audit `.gobby/plans/research/1.0-dependency-audit-2026-10-17.md` section (c), lines 257-285.
- #20151 / `.gobby/plans/installer-wizard.md`: Python wizard, parked by Josh until 0.5.0
  release prep. Its decision 1 automates today's remote bridge (DSN + key); decision 12: fresh
  node bootstrap holds `datastore_mode: remote`, `database_url`, `hub_daemon_url`. Retires the
  DSN inputs when #21579 lands. Not reusable for a Rust path, but its idempotency rule
  (read state from the owning store, write only on change) is a good model.
- `.gobby/plans/client-operator-verbs.md` (#21571, S3.1): gclient command-mode verbs extend
  the hand-rolled parser in `crates/gclient/src/command.rs` (decision 2, clap rejected).
  Inventory rows: `auth login`/`auth key`/`auth credentials` keep on gobby-backend, ref #21555;
  `install`/`uninstall` keep, ref #21591; `gobby.cli.install_setup_srt` keep ref #21591.
  S3.2 #21573 renames gclient to gobby.

## Enrollment (gap 2)
- `src/gobby/cli/auth_login.py::enroll` (82-157): reads bootstrap; `_enrolled_hub` requires
  `datastore_mode: remote` + `hub_daemon_url`; `--hub` must equal it ("login never re-homes a
  node"); plain http to non-loopback needs `--insecure`; `--fingerprint` only with https.
- TLS: `_approved_certificate` fetches the server cert, prints `sha256:<hex of DER>`, matches
  `--fingerprint` or asks to trust; stages PEM; password sent only to the pinned origin,
  `trust_env=False` (no proxy).
- `POST /api/auth/keys/bootstrap` body: email, password, machine_id, hostname, os, label.
  Hub route is Python (`routes/api_keys.py::bootstrap_key`), proxied by the front door; the
  node only calls HTTP, so the hub side needs no change.
- `_verified`: 401 = bad credentials; response must carry key (parseable `gobby_` format),
  key_id/user_id/machine_id UUIDs, machine_id equal to ours, hint == hint(key), key_id !=
  prior api_key_id.
- `_publish` under `exclusive_file_lock(bootstrap)`: writes `tls/hub.pem` (durable replace),
  sets `api_key`, `api_key_id`, `hub_cert` (or drops hub_cert for http), publishes bootstrap by
  rename (`publish_bootstrap_yaml_locked`). On failure `_settle`/`_revoke` the minted key.
- machine id: `src/gobby/utils/machine_id.py::_get_or_create_machine_id` creates
  `<gobby_home>/machine_id` (uuid4) under lock if missing. gcore has only readers
  (`crates/gcore/src/machine.rs::read_machine_id_from_home`).
- `crates/gdaemon/src/state.rs::NodeServices::prepare` refuses without `api_key`
  ("run `gobby auth login` to enroll"), needs `hub_daemon_url`, and `hub_cert` for https.
- Remediation strings naming `gobby auth login`: gdaemon state.rs, gcode cli_error.rs, ghook
  diagnose.rs (AUTH_401_REMEDIATION), Python middleware/auth.py, daemon_client.py.

## Bootstrap (gap 1)
- e2e `tests/e2e/test_hub_node_pair.py::pair_node` writes a node bootstrap with
  `datastore_mode: remote`, `hub_daemon_url`, `bind_host`, `daemon_port`, `websocket_port`
  (no database_url), runs Python `gobby auth login --fingerprint`, then runs bare
  `gdaemon serve` with GOBBY_HOME. So node gdaemon already runs with no DSN; only the Python
  installer demands one.
- `src/gobby/cli/_install_daemon.py::_run_install_preflight`: remote mode needs
  `database_url` ("Remote datastore install requires database_url"), local needs Docker.
- gcore `crates/gcore/src/bootstrap.rs` has readers only (`parse_hub_database_bootstrap`,
  `HubDatabaseBootstrap` with api_key, hub_daemon_url, hub_cert...); no writer.
  Defaults: DEFAULT_DAEMON_PORT 60887, DEFAULT_WEBSOCKET_PORT 60888, DEFAULT_BIND_HOST.
- Bootstrap is 0600, written by rename.

## Binaries
- `src/gobby/install/bin_set_coherence.py`: `workspace_build_only(member)`; gcode, gdaemon,
  ghook install only from a source build (`promote_workspace_binary_set`: codesign, new inode,
  identity stamp, release identity). gclient and gterm also download from GitHub releases
  (`src/gobby/cli/install_release.py::_download_release_binary`, repo GobbyAI/gobby,
  `<artifact>-<target>.<ext>` + `.sha256`).
- Release workflows exist: release-gclient, -gcode, -gdaemon, -ghook, -gterminal (no gmcp).
- The "mixed installed binary set" check is Python (`src/gobby/storage/schema_divergence.py`),
  hub start path; a node gdaemon starts without it.

## SRT (gap 3)
- `src/gobby/cli/install_setup_srt.py::_install_srt_runtime`: needs node + npm; verified
  tarball download (`_download_verified_tarball`, 16 MiB cap, 8 requests), package.json,
  bundled lockfile `src/gobby/install/srt-package-lock.json` (sha256 vs
  `SRT_RELEASE.lockfile_sha256`), `npm ci --ignore-scripts --no-audit --no-fund --omit=dev`
  (180 s), package name/version check, `srt_package_patch.apply_srt_proxy_patch`, copies
  `src/gobby/agents/srt_runner.mjs` to runner.mjs (sha256 vs `SRT_RELEASE.runner_sha256`),
  receipt.json (+node path), content manifest, immutable, promote to
  `<gobby_home>/tools/srt/<SRT_RELEASE.version>` under `srt_install_lock`.
- Pin: `src/gobby/utils/dependency_requirements.py::SRT_RELEASE`.
- #24374 (node-halves 2.5) ports verification to gdaemon (`sandbox/runtime.rs`):
  content-manifest.json maps runner.mjs, package-lock.json, node_modules/ to sha256 or
  `link:<target>`. node-halves Ruling 8: "gobby install places the pinned runtime; gdaemon
  verifies and runs it and never installs." So staging belongs in the install verb, not
  gdaemon.

## CLI hooks (gap 4)
- `src/gobby/cli/installers/claude.py::install_claude` (global): install_global_hooks,
  project hook cleanup, shared content, CLI content, router skills as commands, settings.json
  hooks merge from `src/gobby/install/claude/hooks-template.json` ($HOOKS_DIR,
  `rewrite_hook_template_commands`, timeouts, SessionEnd <= 60), autoMemoryEnabled false +
  provenance, statusline, MCP server in ~/.claude.json, trust seed.
- codex installer: hooks.json + config.toml hook trust state (hashed entries), 794 lines.
- `hook_commands.py`: `--gobby-owned` marker, `merge_gobby_hook_groups`,
  `rewrite_hook_template_commands` (`__GOBBY_HOOK_COMMAND__`).
- Project-level hooks are cleaned to avoid double firing, so spawned claude/codex runs rely
  on GLOBAL hooks. #24372 (node-halves 2.3) copies project `.claude`/`.codex` and renders
  droid's template only. The audit's "spawned runs are covered" holds for droid; claude and
  codex spawned runs also need the global hooks this plan installs. (Verify before stating.)
- hub-mode: until #24151 (S2.11 hub half) lands, a node session that misses its pre-created
  row (a /clear successor, an operator's own CLI on the node) is refused at registration.

## Carrier analysis (gdaemon vs gclient)
- gdaemon (clap: `schema`, `serve`, `compare-report`) already has: pinned TLS client
  (`crates/gdaemon/src/front_door/tls.rs::pinned_client_config`, `fingerprint`), hyper-rustls
  client, `HubRelay`, bootstrap readers via gcore, PID claim, node module tree; #24374 adds the
  Rust SRT pin + content-manifest verifier (`nodes/agents/sandbox/runtime.rs`, 2.5.4 pin parity
  test); #24372 adds droid hook template rendering + `merge_gobby_hook_groups` port
  (`nodes/agents/repair.rs`, include_str of `src/gobby/install/droid/hooks-template.json`).
  Every node runs gdaemon; gclient (TUI, ratatui) is optional on a headless node.
- gclient command mode: pane verbs only (list, new-tab, split, ...); `dispatch_inner` needs
  daemon url + token before any verb; reqwest rustls. #21571 (client verbs) not implemented
  (no `docs/contracts/cli-inventory.toml`); S3.2 #21573 renames gclient to gobby.
- node-halves Ruling 8 (in #24374): "`gobby install` places the pinned runtime; gdaemon
  verifies and runs it and never installs." Read as: the serving daemon never installs.
- `gobby auth login` is node-only (a local hub's api_key comes from `ensure_local_api_key` at
  start; login refuses a local bootstrap). So every "run `gobby auth login`" remediation is
  node-facing: gdaemon state.rs, gcode cli_error.rs, ghook diagnose.rs, Python
  middleware/auth.py, daemon_client.py, remote_preflight.py.

## Node start and docs
- #24075 (closed): `gobby start/restart/cutover` refuse a `datastore_mode: remote` bootstrap.
  gdaemon-node-channel decision 15: "an operator starts one by hand with `gdaemon serve`".
- `docs/guides/shared-stack.md` Client setup (68-125) still tells a node to write a DSN,
  copy `.secret_kek`, run `gobby auth login` then `gobby install`.
- Python `service.py` writes a LaunchAgent / systemd unit for the Python daemon.

## Fact checks (advisor, 2026-10-18)
- Release: `.github/workflows/release-gdaemon.yml` publishes on tag `gdaemon-v<Cargo version>`
  archives `gdaemon-<target>.tar.gz|.zip` + `.sha256` for aarch64/x86_64 darwin, linux-gnu,
  windows-msvc via `gh release create --latest=false`. ghook/gcode/gclient/gterminal have
  similar workflows; gmcp has none.
- Spawn path: node-halves gives claude `--settings '{"sandbox": {"enabled": false}}'` under
  SRT (merges with user settings) and `CLAUDE_CONFIG_DIR` only for task-close-reviewer, which
  copies the host settings.json. So spawned claude (and codex) runs on a node take hooks from
  the node's GLOBAL config: gap 4 is on the spawn path, not just interactive.
- hub-mode Decision 7 / `NODE_HOOKLESS_PROVIDERS = {"grok", "agy"}`: grok and agy send no
  hooks from a node. Node hook targets: claude, codex, droid.
- Still to read before drafting gap 4: `hook_commands.py::build_hook_command_prefix`,
  `codex.py::_normalized_codex_command_hook_hash` (codex trust hash must match codex's own).

## Decisions sent to #15977 (2026-10-18), awaiting rulings
1. Carrier: gdaemon `node` subcommands (rec) vs gclient verbs.
2. Python `gobby auth login`: delete, pair_node uses the Rust verb (rec) vs keep.
3. Scope: (i) binaries: release download vs documented cargo build; (ii) daemon start:
   by hand `gdaemon serve`, service out to #21571 D1 (rec); (iii) Node.js required for SRT.
4. Hooks: claude, codex, droid hooks + gmcp MCP entry + codex trust (rec); no shared content,
   router commands, statusline, autoMemory, validate_settings.py.

## RULED by #15977 at 10:22 CDT 2026-10-18 (verbatim)
"I take all four recommendations.
E1: (a), gdaemon node <verb> subcommands. Ruling 8 reads as you put it: the serving daemon
never installs; an explicit operator subcommand may. Record that reading in the Decision Record.
Every 'run gobby auth login' remediation you listed names the new verb, with the gdaemon, ghook
and gcode 0.0.1 bumps in the leaves that touch them.
E2: (a), delete Python gobby auth login. test_hub_node_pair.py::pair_node enrolls with the Rust
verb and serves as the no-Python enrollment proof.
E3: (i)(a), cargo build --release plus gdaemon node install, copying the sibling binaries by new
inode with ad-hoc signing. Reuse promote_workspace_binary_set semantics rather than a second
promotion path, including the identity stamp for the gcode/gdaemon/ghook set, or state in the
plan why a node cannot use it. (ii)(a), the operator runs gdaemon serve; the service unit stays
with #21571 D1, and the docs leaf says so. (iii) Node.js and npm are a stated node requirement
for SRT.
E4: (a), with the exclusions you named. Say plainly that interactive node sessions register at
the hub only after #24151, and that this leaf does not wait on it.
Finish the draft decision-complete and hand it to Plan Adversary 16993. After the stamp, it
comes to me and then goes to Josh."

## Post-ruling research (2026-10-18, second pass)

Deps all OPEN: #24374 (2.5 SRT verify; `SRT_VERSION` const + include_str parity test; verifies
under the Python `srt_install_lock` file), #24372 (2.3; `nodes/agents/repair.rs` renders droid
template via include_str, ports merge_gobby_hook_groups, `~/.claude.json` mkdir lock
`<path>.lock` 10 s stale for mcpServers, golden pattern: Python test
`tests/agents/test_node_isolation_golden.py` + Rust `node_agents.rs::droid_hooks_match_python_golden`
against `crates/gdaemon/tests/fixtures/node_isolation/droid_hooks.json`), #24355 (gmcp-stdio 1.6:
MCP entry `{"command": "<gobby_home>/bin/gmcp", "args": []}`), gmcp-stdio 1.4 installs gmcp like
gclient (not in stamped set). #24604 targets test_hub_node_live.py, not test_hub_node_pair.py.

gdaemon: main.rs 316 lines, clap `Cli`/`Command {Schema, Serve, CompareReport}`; lib.rs 16
lines (`#[cfg(unix)] pub mod state`); state.rs 195 lines (`NodeServices::prepare` lines 32-74,
both remediation strings). Deps already: libc (unix), uuid v4, toml 1, serde_json, sha2,
hyper-rustls(ring,tls12,no roots), tokio-rustls, rcgen; dev: assert_cmd. Locked but not gdaemon
deps: `yaml_serde` 0.10.4 (gcore uses as `serde_yaml`), `toml_edit` 0.25.13, `webpki-roots`.
Reuse: `front_door::relay::HubRelay::new(origin, Some(pem))` + `forward(Request)` = pinned TLS,
no env proxy, sets Host; `front_door::tls::{pinned_client_config, fingerprint}` (fingerprint is
`sha256:<hex DER>`); no cert-capture verifier exists (new: rustls `ServerCertVerifier` that
records the end-entity cert). gcore: `bootstrap::{bootstrap_path, parse_hub_database_bootstrap}`
(validates + normalizes hub_daemon_url via `bootstrap/run_mode.rs::parse_mode_fields`: remote
needs hub_daemon_url, refuses files_home, refuses own origin); `machine::read_machine_id_from_home`.
Existing flock users: `lifecycle/pid_file.rs`, `nodes/hooks/ledger.rs` (libc::flock).
Tests: `tests/common/mod.rs` (`self_signed`, `start_tls_front_door`, `tls_connect`),
`tests/node_support/{mod.rs,fake_hub.rs}`, `front_door.rs::spawn_tls_serve`.

Python enroll spec (`auth_login.py`, 640 lines; `login` cmd 348-374 options --hub --email
--fingerprint --label(default hostname) --insecure; password `click.prompt(hide_input)` reads
stdin when not a TTY — e2e `_login` pipes `TEST_PASSWORD\n`): `enroll` 82-157, `_enrolled_hub`
(remote required, --hub must equal, fingerprint https-only, http non-loopback needs --insecure),
`_approved_certificate` (fetch cert, print fingerprint, match or confirm y/N), `_stage` (staged
pem in tls/ 0700), POST `/api/auth/keys/bootstrap` {email,password,machine_id,hostname,os,label}
10 s timeout, `_verified`, `_publish` (sidecar flock `.bootstrap.yaml.lock` 0600; write
`tls/hub.pem` durable 0600; set api_key, api_key_id, hub_cert or drop it; yaml safe_dump
sort_keys=False; durable replace 0600), `_settle` (COMMITTED if bootstrap names key_id; else
restore prior pem or unlink), `_revoke` (POST /api/auth/login → gobby_session cookie → DELETE
/api/auth/keys/{id}), `_network_error` (SAN miss message). Outcome messages for PUBLISHED,
COMMITTED, UNRESOLVED, revoke failure, rolled back. Keep `key`, `key_list`, `key_revoke`,
`_key_*`, `_mint_key`, `_rotate_key` (gobby auth key). machine_id: `_get_or_create_machine_id`
= read, else flock `.machine_id.lock`, re-read, write uuid4 durable 0600.
Login consumers: `src/gobby/cli/auth.py` (imports key, login), `tests/cli/test_auth_login.py`
(login + key tests), `tests/e2e/test_auth_login_e2e.py` (`_login` runs python -c cli auth login;
`test_login_pins_self_signed_hub`, `test_login_refuses_host_outside_sans`, `_node_home`),
`tests/e2e/test_hub_node_pair.py` imports `_login`, TEST_EMAIL from it.

"gobby auth login" strings (all must name `gdaemon node enroll`): crates: gclient
startup.rs:147 (+tests/startup.rs:871), gcode cli_error.rs:183,186, gcore local_token.rs:15,
gdaemon state.rs:39,51 (+tests/nodes.rs:482, run_modes.rs:54), ghook diagnose.rs:41 (+311).
Python: auth_login.py:383,396 (key cmds), hub_backup/bootstrap_restore.py:66,
installers/remote_preflight.py:378,402,460, sessions_dirty_paths.py:19,45, hooks/inbox.py:374,
servers/middleware/auth.py:53, utils/daemon_client.py:51, skill ref
install/shared/skills/gobby/references/admin/authentication.md:20. Tests: test_pack.py:1035,
config/test_remote_ui_auth.py:103, contracts/http/auth_missing_auth.json:24,
servers/test_auth_middleware.py:202, test_http_middleware.py:53,198,
utils/test_daemon_client.py:13, workflows/test_dirty_edit_grants.py:192,215,299. Docs:
contracts/parity-ledger.md:51 + parity-ledger-status.d/daemon-config-auth-infra.json:22,
contracts/secrets.md:27, guides/admin-operations.md:60, cli-commands.md:463,505,837,
configuration.md:271-280, gterminal-development-guide.md:354, http-endpoints.md:50,88,
hub-install-contract.md:86, shared-stack.md:102-105, system-requirements.md:235-246,
reference-audit/admin.json:554. No edit: tests/tasks/expansion/test_contract_validation_criteria.py
(arbitrary phrase), docs/audits/*, docs/research/*.
gcore change bumps gcode+gdaemon+ghook; gclient source edit bumps gclient.
Python install remote path: `_install_daemon.py::_run_install_preflight` (70-125) imports
`installers/remote_preflight.py::run_remote_preflight` (DSN/qdrant/falkordb probes); tests in
tests/cli/test_cli_install.py:988-1107. Candidate: Python `gobby install` refuses a remote
bootstrap naming `gdaemon node install` (like #24075), deleting remote_preflight.py — DECIDE.

Promotion (`bin_set_coherence.py::promote_workspace_binary_set`): probe each candidate
(`gdaemon schema version --json`, else `<bin> schema-identity --json`), validate 6 fields
(`schema_identity_pin.py::validate_identity`), require agreement, per-member nonblocking flock
`<bin_dir>/.locks/<name>.lock` 0644 (`bin_freshness_locks.py::try_acquire_native_bin_lock`),
stage in `mkdtemp(".<name>-staging-", bin_dir)`, copy+fsync+chmod 0755, darwin
`codesign -f -s -` (30 s), `os.replace`, then stamp `.gdaemon-schema-identity.json` =
compact sorted JSON + "\n" (`stamp_bytes`), 0644 atomic. Release identity
(`.<name>-release-identity.json`, source hash of crate closure) and version stamp/sidecar are
read only by Python (`bin_release_identity.py`, `bin_freshness*`): no Rust reader (grep) → node
skips them, stated in plan. SET_MEMBERS (`utils/native_bin.py`) = gcode, gdaemon, ghook.
Node binaries: stamped set + gmcp (package gobby-mcp 0.1.0) + gterm (package gobby-terminal,
`--features vt-engine --bin gterm`, needs Zig 0.16; node gdaemon runs `<home>/bin/gterm` host per
node-halves). gclient not installed by node install. macOS sign with `/usr/bin/codesign`.

SRT: `install_setup_srt.py::_install_srt_runtime` (62-146) + `srt_runtime.py`
(`srt_install_root` = `<home>/tools/srt/<ver>`, `build_srt_content_manifest`,
`write_srt_content_manifest` compact sorted JSON no newline `content-manifest.json`,
`make_srt_installation_immutable` 0555/0444 + seccomp helpers, `srt_install_lock` =
`export_file_lock(root)`), `_promote_install` (.previous backup), `_require_node`
(`node_dependency_status`, `NODE_MIN_VERSION = "22.12.0"`), receipt = `SRT_RELEASE.receipt_fields()`
+ node path. Pin (`dependency_requirements.py:67-84`): package @anthropic-ai/sandbox-runtime 0.0.79,
tarball_url, tarball_sha256 5a730e…, npm_integrity, lockfile_sha256 efd7a3…, runner_sha256
44658a…, http_proxy/mux_proxy/macos_profile/which sha256. Patch (`srt_package_patch.py` 112
lines): 4 entries (path, original sha, expected sha, needle, replacement) + macOS monitor needle;
skip when already expected. DESIGN: tarball via `npm pack <pkg>@<ver> --pack-destination <staging>
--ignore-scripts` (libnpmpack writes registry bytes verbatim; sha256 pin check) → no Rust HTTPS
client; embed lock + runner with include_bytes and verify sha; port patch table with include_str
parity test against both Python files.

Hooks: `hook_commands.py`: prefix `shlex.quote(<home>/bin/ghook) --gobby-owned`; full
`<prefix> --cli=X --type=Y` for `__GOBBY_HOOK_COMMAND__`; rewrite keeps flags;
`merge_gobby_hook_groups` (drop gobby handlers, append template); timeouts. claude
(`claude.py::install_claude` 157-389): template `src/gobby/install/claude/hooks-template.json`,
timeout 120, SessionEnd 60, merge per hook type into `~/.claude/settings.json` (backup
`settings.json.<ts>.backup`, atomic write indent 2), MCP via `mcp_config_json.py::configure_mcp_server_json(~/.claude.json)`.
codex (`codex.py::install_codex` 576-706): `_install_hooks_file` (template, timeouts 120,
Interrupt 3, SessionEnd 3, quarantine corrupt, merge into `~/.codex/hooks.json`), config.toml:
remove `features.codex_hooks`, set `features.hooks = true`, `_ensure_codex_hook_trust_state`
(`[hooks.state."<resolved hooks.json>:<EventLabel>:<g>:<h>"] trusted_hash = "sha256:…"` from
`_normalized_codex_command_hook_hash`: JSON sorted compact ensure_ascii False of
{event_name label, hooks:[{async,command(normalized whitespace),timeout(default 600, min 1),type,
statusMessage?}], matcher? for CODEX_MATCHER_HASH_EVENTS}; drops `enabled`; stale + dead
entries removed), backup `config.toml.bak`, MCP via `mcp_config_toml.py`. droid
(`droid.py::install_droid` 264-366): flat `~/.factory/hooks.json` via `_load_droid_hooks_template`
+ `_merge_gobby_hooks` over DROID_PASCAL_HOOK_NAMES, MCP `~/.factory/mcp.json`. Every installer
also calls `seed_gobby_home_trust` (`agents/trust.py`) — not in E4 scope: DECIDE (exclude, with
reason) . Templates contain no `$HOOKS_DIR`.

Leaf sketch (gdaemon module `crates/gdaemon/src/node_setup/`, cfg(unix); tests
`crates/gdaemon/tests/node_setup.rs` + shared `tests/node_setup_support/mod.rs` scrubbed-PATH
command builder): 1.1 bootstrap writer (`gdaemon node install --hub` writes when absent; refuse
local/hub mode or different hub) + harness; 1.2 binaries; 1.3 enroll (+machine_id); 1.4
remediation strings + delete Python login + pair_node/_login switch + e2e; 1.5 SRT; 1.6 claude
hooks+MCP; 1.7 codex hooks+trust+MCP; 1.8 droid hooks+MCP; 1.9 docs (shared-stack Client setup,
system-requirements, cli-commands, configuration, hub-install-contract; cargo build line; manual
`gdaemon serve`; service with #21571 D1; Node.js 22.12 + npm; Zig 0.16 for gterm; #24151 note).
Chain all gdaemon-bumping leaves (Cargo.lock, version_pins.py, gdaemon Cargo.toml, main.rs).

## Plan design notes
- Criterion 3 harness: Rust integration test spawns the verb with PATH scrubbed to a temp dir
  (only node/npm/git stubs as needed), against gdaemon's TLS stub hub (`spawn_tls_serve`).
  SRT tarball source injectable (local stub) and npm stubbable.
- Enrollment >6 acceptance items: Granularity decision; bootstrap writer its own leaf, in
  gdaemon (no gcore change, avoids 3-crate bump).
- Every gdaemon leaf bumps gdaemon Cargo.toml/Cargo.lock: chain dependencies; main.rs shared.
- No `kind: deferred` sections (#15977 ruling 2026-10-18). V2: must-statements, "none has run
  yet" (memory da52a076).
- Password: `--password-file` + hidden TTY prompt (installer-wizard decision 8 pattern).

## Edges and context
- #24657 blocked by #24604 (Live hub-to-node proof, hub-mode 5.1). #24604 enrolls with
  Python `gobby auth login` in `pair_node`.
- #21591 Install and setup route family (S2.13 epic, rust, gobby-1.0): hook installation,
  project init, template sync, first-run setup. Hub-side.
- Josh 1.0 objective (memory 9bf4bb37): node install Rust-only; no Python, no Docker.

## Revision 2 (2026-10-18, after stamp c5dcd2c): Josh's three corrections via #15977
Status: plan at c5dcd2c (M1 manifest_digest 1bc8c4f7…, rendered ce350837…) is superseded.
Every narrative edit invalidates M1: withdraw it per memory f5577ae0 (commit the repair with
stale M1 untouched, then remove the whole M1 block and its preceding blank line plus a
renewed-consensus entry, after adversary agreement), then #16993 re-derives.

ORCH 12:52 (Josh: "E4 is unacceptable"; "Yes, no cutting corners"; "gclient becomes Gobby
after the port. Commands run through Gobby even if they are forwarded through gdaemon."):
1. E4 reversed: the node installs hooks plus the gmcp MCP entry for all five providers
   (claude, codex, droid, grok, agy). This reverses hub-mode Decision 7. Plan removal of
   NODE_HOOKLESS_PROVIDERS and its consumers; add a grok leaf and an agy leaf with no-Python
   tests, like 1.7 to 1.9.
2. E1 revised: operator commands are `gobby node install` and `gobby node enroll`, through the
   ported gclient (becomes `gobby` at #21573). gdaemon may carry or forward the implementation.
   The plan must say how gobby reaches gdaemon on a fresh node with no daemon.
ORCH 12:57 (Josh: "E3 is wrong. Hubs should handle cutovers. Nodes should update through
crates not source."):
3. E3 revised: nodes install and update from published crates. No source checkout, no cargo
   build from source on the node, no hand start with `gdaemon serve`. Drop the Node 22.12+ and
   npm requirement only where it came from building from source. (Writer note: SRT itself needs
   Node and npm (npm pack/ci, runner.mjs), so the requirement stays for SRT. Say so explicitly
   and flag it to #15977.) The hub orders and activates each node's update; a node never runs
   its own cutover. Carry this through the Decision Record, gates, constraints and every
   acceptance item that assumes a source build or a node-run cutover.
Deliver: re-stamp by #16993, then send #15977 the stamped SHA. No deferrals.

### Findings so far
- NODE_HOOKLESS_PROVIDERS exists only in plans: `.gobby/plans/hub-mode.md:2909` (hub-mode 2.6,
  reconciler settles grok/agy runs on the node's `exited` report; acceptance 2.6.8
  `tests/hub/test_remote_reconcile.py::test_hookless_exit_settles_from_exit_code`), lines
  442 and 2757 (node-halves 2.3.3 gives grok/agy no hook directory; node-halves 2.13, 2.14
  and 3.3 pane attention that grok/agy depend on; hub-mode row 5.1 at line 497). No code hit
  for the constant (`gcode grep -w`). Next: check the task status of hub-mode 2.6 and of
  node-halves 2.3.3, 2.13, 2.14 and 3.3 (#24372 is node-halves 2.3). Consumers in unlanded
  plans or tasks are edits to other plans or tasks, routed through #15977. Landed code is
  targeted here.
- gclient (crate gobby-client 0.1.58, bin `gclient`): `startup::run` (startup.rs:697) sends a
  first non-dash arg to `command::dispatch`. `dispatch_inner` (command.rs:169-223) handles
  `help` first, then resolves `--daemon-url`/`daemon_url()` and the token
  (`read_api_key`) before `verbs::Action::parse`/run. So `node` must be routed before
  URL/token resolution, like `help`. Plan: `gobby node <verb> <args>` Unix-execs the
  sibling `<dir of canonicalized current_exe>/gdaemon node <verb> <args>` (stdin, TTY and
  exit status pass through). No daemon and no HTTP. Help table is command/help.rs
  (`is_verb`, `verb`, `table`).
- #21573 (epic, blocked by #21571): the gobby-client binary ships as `gobby`, `gclient`
  disappears, and its exit is "`gobby start` brings up story A". #21571 (client-operator-verbs
  plan, `.gobby/plans/client-operator-verbs.md`) defers start/stop/restart/lease to its D1
  (line 1216).
- grok installer (`src/gobby/cli/installers/grok.py::install_grok`): gate ghook >= 0.5.0
  (`_MIN_GHOOK_VERSION_FOR_GROK`; crates/ghook is 0.9.29, so it passes). Template
  `install/grok/hooks-template.json` is rewritten with `rewrite_hook_template_commands(cli_name="grok")`
  plus timeouts and written whole to `~/.grok/hooks/gobby.json` (prior file backed up as
  `gobby.json.<ts>.backup`). `~/.grok/config.toml` gets `[compat.claude] hooks = false` via
  tomlkit (backup `config.toml.<ts>.backup`; no write if already false). No MCP entry is
  written by install_grok: find where grok reads MCP servers (open question).
- agy installer (`src/gobby/cli/installers/agy.py::install_agy`): global mode only. Hooks at
  `~/.gemini/config/hooks.json` (env GOBBY_AGY_HOOKS_FILE), merged with
  `_merge_gobby_hooks` from `_load_agy_hooks_template` and written atomically with a backup.
  `_verify_agy_hook_registration` runs `agy -p /hooks --output-format json` if `agy` is on
  PATH (10 s) and checks the hook named AGY_GOBBY_HOOK_NAME. MCP at
  `~/.gemini/config/mcp_config.json` (env GOBBY_AGY_MCP_FILE), `configure_mcp_server_json`
  with `extra_server_fields={"type": "stdio"}` and `follow_provider_symlink=True`. Also
  `seed_gobby_home_trust("agy")` (D8 says the node skips home trust; confirm for agy) and
  shared and CLI content (E4 excluded).
- Open research for E3: are crates published (`cargo publish` in .github/workflows/release-*.yml)?
  Is there binstall metadata or prebuilt binaries? What does "published crates" mean for
  gterm (Zig, vt-engine) and gmcp (no release workflow)? Is there an existing hub-driven node
  update or cutover design (gdaemon-node-channel.md, node-halves.md, hub-mode.md,
  gdaemon-runtime-family.md)?

## Revision 3: FULL REWORK (2026-10-18, supersedes Revision 2's targeted edits)
ORCH 13:03 (Josh 12:59 "The whole plan needs to be reworked."; 13:02 framing verbatim: "The
planning agents should be thinking about this from a developer experience perspective. Or
consider my use case. I want one hub machine and nodes installed on my laptops and Mac mini.
Once that's in place I'll likely only be doing Gobby development (like the 1.0 port after
we've got hub-node working) on the hub, and using my nodes to do other work. Most users aren't
going to be developing Gobby but will instead be using Gobby for their own projects.")
Nodes are user machines running their own projects; no Gobby checkout, no toolchain.
Requirements (all, no corners cut):
1. All five providers (claude, codex, droid, grok, agy) get hooks and the MCP entry on a node.
2. Operator commands are Gobby commands (`gobby node install`, `gobby node enroll`) through the
   ported gclient that becomes gobby; gdaemon carries them.
3. Nodes install and update through published releases, never source.
4. The hub drives node cutovers.
5. Version skew specified both ways (hub older than node; node older than hub).
6. Install sets up launchd (and Linux systemd), no manual launch.
7. Push back where I disagree, as a stated decision with a reason.
(a) Prebuilt, signed per-platform binary sets per released version; crate version as identity;
    hub tells the node which version to fetch.
(b) Hub upgrades first, then drives node updates (steady state node <= hub). Nodes advertise a
    supported protocol range at enrollment and every connect. Hub refuses a node it can't
    speak to with an actionable error. Acceptance items for both directions.
Write for Josh's DX end to end: fresh laptop to working node.
ORCH 13:04: Josh verbatim "I need to be able to control all of these machines, hubs and nodes,
from any gclient window." Every hub and node operation works from any gclient window on any
enrolled machine; authority and routing specified.
ORCH 13:05: Josh verbatim "1. Nodes install from prebuilt binaries only. When a node starts, it
checks for upgrades and installs them. If the hub sends a command to a node to install an
update, it does so." Finalize: prebuilt only, no toolchain, no source on nodes. Two update
triggers: a check at node start and a hub-sent install command. A start-time check must never
move a node past what its hub can speak. Stamp hold lifted.
Adversary #16993 early flags (13:0x): (1) prebuilt signed binaries, where published, how
verified; (2) replace gmcp-no-release and workspace_build_only; node writes
.gdaemon-schema-identity.json and release-identity records from release metadata; (3) skew rule
and refusal message per direction, which component checks, which handshake, how the hub cutover
resolves it; (4) launchd/systemd owner, change to #21571 D1 ruling, cutover restart on new
inodes; (5) who bootstraps the first gdaemon on a fresh laptop; the exact first command.
Release facts: release-{gclient,gcode,gdaemon,ghook,gterminal}.yml publish crates.io +
GitHub binaries `<bin>-<target>.tar.gz` + `.sha256` (gdaemon: 4 unix + 2 windows triples;
gterm/gclient: 4 Stage-0 triples). release-gcore crates.io only. No gmcp release workflow.
Tags `<bin>-v<ver>`; docs/guides/release-guide.md. Python `install_release.py` downloads
gclient/gterm with sha256 sidecar; binstall fallback in install_setup_gclient/gterm.
No existing hub-driven node update design in node-channel/node-halves/hub-mode/runtime-family.

### Revision 3 design (settled by the writer, 2026-10-18; advisor-checked)
- Facts: grok MCP = `~/.grok/config.toml` `[mcp_servers.gobby]` {command,args}
  (`src/gobby/agents/grok_sandbox_home.py::_declare_gobby_mcp_server`); interactive grok
  otherwise inherits from ~/.claude.json via Claude-compat. gcore's `ureq 2` is non-optional
  (tls+gzip default) so gdaemon can declare it with no new lock entry; `ring` is locked
  (rustls). Hub channel is Rust: `crates/gdaemon/src/nodes/channel.rs` (`Hello{kind,
  node_version, platform}` without deny_unknown_fields; ack `{type,machine_id}`;
  `persist_heartbeat` writes machines.node_version). Command contract v4
  (`docs/contracts/node-commands.md`, `node_commands.rs::CONTRACT_VERSION`, 930 lines; Python
  models `src/gobby/hub/node_commands.py`). machines API (`nodes/machines_api.rs`) lists the
  caller's machines (owner_user_id) with node_version and connected. agent_runs has
  machine_id + status ('pending','running' live). Python service templates:
  `src/gobby/install/shared/services/com.gobby.daemon.plist.j2`, `gobby-daemon.service.j2`
  (ProtectSystem=strict; unusable for a node that edits user projects). gmcp: package
  gobby-mcp 0.1.0, publishable metadata + README present, not in MANAGED_BIN_VERSION_PINS.
  #24276 (hub-mode 2.6) and #24372 (node-halves 2.3) open+blocked. #21573 open+blocked epic.
- Node release = gdaemon release (identity = gdaemon crate version, tag gdaemon-v<V>). The
  gdaemon release job assembles `gobby-node-<uname -s>-<uname -m>.tar.gz` from member release
  assets at the tagged commit's versions (no rebuild), verifies member signatures, probes set
  identity on linux x86_64, writes node-release.json (release, contract range, per-target
  member sha256), signs. Client ships in the bundle as `gobby`.
- Every release: SHA256SUMS + SHA256SUMS.sig (Ed25519, openssl pkeyutl -rawin, secret
  GOBBY_RELEASE_SIGNING_KEY in env `release`); public key pinned in gdaemon (manual task asked
  of #15977). Only gdaemon releases are marked latest.
- First command: curl latest bundle | tar -xz; `./gobby-node/gobby node install --hub ...`.
- gobby routes `node install|enroll` before URL/token and execs sibling gdaemon;
  `node list|update` are HTTP client verbs via the local daemon (relayed on a node).
- Nodes never ask GitHub what to run: the hub resolves the target (highest published release
  <= hub gdaemon version whose contract range overlaps the hub's), caches it, and sends it in
  the ack, in a refusal, and in `release` frames. Release frames are channel-layer (like
  hello/ack), outside command contract `v`, so Python is untouched.
- Skew: hello carries contract range; hub refuses non-overlap with reason node_too_old or
  hub_too_old, message, and target; node converges to the target (downgrade allowed).
- Activation: hub sends activate when the machine has no pending/running agent_runs, checked
  at admission and on each heartbeat persist; node promotes and exits 75; launchd KeepAlive
  (SuccessfulExit=false) / systemd Restart=on-failure restarts on the new inode.
- No automatic rollback: pre-swap probe of the staged set; recovery is withdrawing the bad
  GitHub release (hub retargets) or re-running the first command.
- Debug-only (cfg(debug_assertions)) env overrides for release base URL and an extra test key.

### ORCH 13:31 rulings
#24717 (Release signing key for node binary updates) is a manual P1. Josh generates the key,
stores GOBBY_RELEASE_SIGNING_KEY in a protected `release` env, and hands over the public key.
A seat commits it at crates/gdaemon/assets/release-signing.pub. The node-verifier leaf and the
publication gate carry (depends: #24717). #24604 does not wait on publication. I'm AUTHORIZED
to edit hub-mode in a separate commit: Decision 7 (l.83), the #24276 NODE_HOOKLESS paragraph
(l.2909), 2.6.8 (l.2985 + M1 label l.4760), and lines 442, 497, 2757, 3132, 3876 and 4099.
Grok and agy settle through hooks; pane attention stays for all five. NAME IN THIS PLAN which
hub-mode M1 entries change; 16993 re-stamps hub-mode too. #21571 D1 needs no change. M1:
commit the rework with M1 untouched (Write narrative + M1 copied verbatim from c5dcd2c lines
837-1131, then diff-check the M1 region with git show), validate a scratch copy without M1,
and at consensus remove M1 and add the renewed-consensus entry.

### More facts
- Release jobs end with `gh release create "$tag" "${assets[@]}" ... --latest=false` (all
  five binary workflows); `release.yml` (Python `v*`) owns GitHub "latest". So the first
  command names a version: `v=<any published gdaemon release>; curl -fsSL
  "https://github.com/GobbyAI/gobby/releases/download/gdaemon-v$v/gobby-node-$(uname -s)-$(uname -m).tar.gz" | tar -xz`
  then `./gobby-node/gobby node install --hub <url> --email <e>`. Install converges to the
  hub's target, so any published release works.
- Signing: `scripts/release/sign_release.sh` (OpenSSL 3 `pkeyutl -sign -rawin`, sha256sum).
  Modes: `sums <dir>` writes SHA256SUMS and SHA256SUMS.sig; `file <f>` writes <f>.sig. The
  pytest uses `cryptography` (dep, 50.0.0) for keys and verify, and skips without OpenSSL 3 and
  sha256sum (CI ubuntu covers it). Bundle assembly is `scripts/release/node_bundle.py`
  (stdlib): verifies member SHA256SUMS sigs with openssl, probes identity on linux x86_64
  (`gdaemon schema version --json`, `<bin> schema-identity --json`), reads member versions via
  `cargo metadata --no-deps`, and gets the contract range from `gdaemon node release-info
  --json`. It writes node-release.json + node-release.json.sig, and the bundle carries
  node-release.json + .sig so an extracted bundle self-verifies (integrity only; the first
  download's trust root is HTTPS from GitHub). Members are downloaded by `gh release
  download <tag>`. gmcp gets six triples like gdaemon; gterm and gclient have four.
- Hub frames: the hub loop has no outbound sender yet (registry has a close oneshot only).
  3.x adds an mpsc sender per live connection, or reuses a node-halves outbound sender if one
  exists at the starting tip. Routes are mounted via `nodes/machines_api.rs::router`/`is_route`
  (front_door/mod.rs:80,199). machines rows (`machine()`) gain target_release and
  staged_release. Node side: `channel.rs::connect_client` sends hello and checks the ack;
  `run_client` loops frames.
- agy: template `src/gobby/install/agy/hooks-template.json`; grok template
  `src/gobby/install/grok/hooks-template.json`. Still to read before writing the leaf:
  `install_agy` 210-313 and `_verify_agy_hook_registration` 116-169 (what happens on failure),
  and grok's timeouts in `install_grok`.
- gclient: `command.rs::dispatch_inner` (169-223), `::is_verb`, `command/help.rs::VERBS`,
  `::is_verb`, `::table`, `::verb`. gdaemon main.rs: `::Command`, `::main`.

### Leaf list (final)
P1 Signed release publishing: 1.1 every binary release signs SHA256SUMS [config]; 1.2 gmcp
  release workflow [config] (dep 1.1); 1.3 gdaemon release carries node bundles [config]
  (dep 1.2, 2.1).
P2 gobby node + enrollment: 2.1 `gdaemon node` subcommand, harness, `release-info`,
  CONTRACT_MIN_VERSION in new nodes/contract_range? (node_commands.rs is 930 lines, so no
  edit there) [code]; 2.2 gclient routes `node install|enroll` before URL/token and execs the
  sibling gdaemon (bump gclient) [code]; 2.3 `gobby node enroll` (old 1.1 bootstrap rules
  merged into publish, plus old 1.3; nothing written on failure; Granularity) [code];
  2.4 remediation names `gobby node enroll` (old 1.4) [code]; 2.5 Python login retires and
  pair_node enrolls with `gdaemon node enroll` (old 1.5; dep #24604) [code].
P3 Release targets and skew: 3.1 release verification library `crates/gdaemon/src/release.rs`
  (not cfg(unix); ring Ed25519, ureq, tar) (dep #24717) [code]; 3.2 handshake contract range
  + refuse frames both directions (nodes/release_frames.rs; tests in new
  crates/gdaemon/tests/node_release.rs) [code]; 3.3 hub resolver nodes/release_target.rs +
  `GET /api/nodes/release-target` + ack target [code].
P4 Install: 4.1 install = enroll-if-needed, target, verify own bundle or fetch, promote (old
  1.2 semantics + .node-release.json) [code]; 4.2 SRT (old 1.6; remediation `gobby node
  install`; dep #24374); 4.3 claude (dep #24372, #24355); 4.4 codex; 4.5 droid; 4.6 grok
  (hooks file + compat + explicit [mcp_servers.gobby] gmcp args=[]); 4.7 agy (hooks.json merge,
  mcp_config.json type stdio follow symlink, agy -p /hooks check, no home trust D8);
  4.8 service unit launchd com.gobby.node / systemd gobby-node.service (no ProtectSystem;
  install-time PATH; KeepAlive SuccessfulExit=false; Restart=on-failure), wait until the
  hub's machines API shows the node connected.
P5 Hub-driven updates: 5.1 node stages on ack/refuse/release frames, reports release_status,
  activates by promote + exit 75; 5.2 hub sends release frames, activation when the machine
  has no pending/running agent_runs (at admission + each heartbeat), `POST /api/nodes/update`
  (owner-scoped, machine ref or all); 5.3 gclient `node list` (GET /api/machines) and `node
  update --machine <ref>|--all` over HTTP via the local daemon (relayed on nodes).
P6: 6.1 docs (node setup with the first command, release guide with tag order and gdaemon
  last, cli refs, gclient guide). E1 gates: tests; publication gate after the last bump leaf
  (dep #24717); live fresh laptop; live update and skew; no Python.
Separate commit: hub-mode edits (list the changed hub-mode M1 entries in this plan's Decision
Record).
