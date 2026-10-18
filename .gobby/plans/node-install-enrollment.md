Plan artifact: `.gobby/plans/node-install-enrollment.md`

# Gobby 1.0 Stage 2: Rust-only node install and enrollment

**Plan ID:** node-install-enrollment

## Overview
`kind: framing`

Task #24657 (Rust-only node install and enrollment) is the plan root. Josh's 1.0
objective (2026-10-17) is a node that installs and runs with no Python and no Docker.
Today four node steps still need Python:

1. Enrollment is Python `gobby auth login` (`src/gobby/cli/auth_login.py::login`).
2. The only installer is Python `gobby install`, whose remote path demands the hub's
   `database_url` (`src/gobby/cli/_install_daemon.py::_run_install_preflight`).
3. The pinned Sandbox Runtime (SRT) is staged by Python
   (`src/gobby/cli/install_setup_srt.py::_install_srt_runtime`).
4. CLI hooks are installed by Python (`src/gobby/cli/installers/claude.py::install_claude`,
   `codex.py::install_codex`, `droid.py::install_droid`).

When this plan closes, an operator sets up a node with no Python interpreter on it:

```bash
CARGO_BUILD_JOBS=3 cargo build --release --locked -p gobby-code -p gobby-daemon -p gobby-hooks -p gobby-mcp
CARGO_BUILD_JOBS=3 cargo build --release --locked -p gobby-terminal --features vt-engine --bin gterm
target/release/gdaemon node install --hub https://hub.example:60887
~/.gobby/bin/gdaemon node enroll --hub https://hub.example:60887 --email me@example.com --fingerprint sha256:<hex>
~/.gobby/bin/gdaemon serve
```

- `gdaemon node install` writes the node bootstrap, promotes the five sibling
  binaries into `<gobby_home>/bin`, stages the pinned SRT, and installs the claude,
  codex and droid hooks and the `gmcp` MCP entry.
- `gdaemon node enroll` mints the node's API key at the hub, pins the hub
  certificate, and publishes both into the bootstrap.
- Every "run `gobby auth login`" remediation names `gdaemon node enroll`, and Python
  `gobby auth login` is deleted.

Each leaf names the Python surface it replaces and a test that runs the node path
with a `PATH` holding no Python interpreter (#24657 validation criterion 3).

## Constraints
`kind: framing`

- **No backward compatibility.** 0.5.0 has not shipped.
- **Unix only.** `node_setup` is `#[cfg(unix)]`, like `state`: it uses `flock`,
  `termios` and `codesign`. `gdaemon node` does not exist on Windows.
- **No-Python harness.** `crates/gdaemon/tests/node_setup_support/mod.rs` (1.1) runs
  the built `gdaemon` (`env!("CARGO_BIN_EXE_gdaemon")`, copied into a temporary
  release directory) with `env_clear()`, `HOME` and `GOBBY_HOME` in a temporary
  directory, and `PATH` set to one stub directory. That directory holds tripwire
  scripts named `python`, `python3` and `uv`; each writes `<sandbox>/python-invoked`
  and exits 127. Every no-Python test asserts the marker is absent. Later leaves add
  their stubs (sibling binaries, `node`, `npm`, `claude`, `codex`, `droid`) to the same
  sandbox, so every earlier verb-level test keeps running the whole verb.
- **Release ownership.** Each leaf bumps by 0.0.1, from the executor's starting tip,
  every shipped binary whose crate or recursive path dependencies it changes. It
  updates `src/gobby/install/version_pins.py::MANAGED_BIN_VERSION_PINS` and
  `Cargo.lock` with the bump
  (`tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`).
  - A change confined to `crates/gdaemon` bumps `gdaemon`.
  - 1.4 changes `crates/gcore`, so it bumps `gcode`, `gdaemon` and `ghook`, and its
    `crates/gclient` edit bumps `gclient`.
  - Python-only leaves and paths with a `tests` component bump nothing
    (`src/gobby/install/bin_release_identity.py::workspace_source_identity`).
  - Every bumping leaf edits `Cargo.lock` and `version_pins.py`, so the bumping
    leaves form one chain: 1.1, 1.2, 1.3, 1.4, 1.6, 1.7, 1.8, 1.9.
  - The `gcode` maintenance floor `MIN_GCODE_PRUNE_BUDGET_VERSION` is a literal in
    `src/gobby/code_index/gcode_gateway.py` (969 lines), and
    `tests/code_index/test_gcode_gateway.py` requires it to equal the `gcode` pin.
    code-index-family 1.1 moves the three gcode floors to
    `src/gobby/code_index/gcode_versions.py` and derives this one from the pin. If
    the literal is still in `gcode_gateway.py` at 1.4's starting tip, 1.4 makes that
    same move; otherwise 1.4 edits neither file.
- **Concurrent editors and seams.**
  - #24374 (SRT install verification and the launch chokepoint, node-halves 2.5)
    creates `crates/gdaemon/src/nodes/agents/sandbox/runtime.rs` with `SRT_VERSION`,
    the content-manifest builder and verifier, and the install lock. 1.6 depends on
    it.
  - #24372 (Worktree and clone preparation, repair and removal, node-halves 2.3)
    creates `crates/gdaemon/src/nodes/agents/repair.rs` with the droid template
    renderer, the `merge_gobby_hook_groups` port and the `~/.claude.json.lock` mkdir
    lock. 1.7 depends on it.
  - #24355 (Every launch site starts `~/.gobby/bin/gmcp`, gmcp-stdio 1.6) sets the
    MCP entry to `<gobby_home>/bin/gmcp` with no arguments in every Python writer.
    1.7 depends on it, so the hook goldens record the `gmcp` entry.
  - #24604 (Live hub-to-node proof) runs on `tests/e2e/test_hub_node_pair.py`'s
    `pair_node`. 1.5 depends on it, so the fixture does not change under it.
- **Dependencies.** No new external crate enters `Cargo.lock`. gdaemon adds
  `serde_yaml = { package = "yaml_serde", version = "0.10.4" }` (as gcore names it)
  and `toml_edit = { version = "0.25", default-features = false, features = ["parse", "display"] }`.
  Both are locked; `display` pulls `toml_writer`, also locked.
- **Heavy work.** Every cargo run sets `CARGO_BUILD_JOBS=3` and scopes with `-p`.
- **Monolith ceiling.** Every targeted production file stays under 1,000 lines,
  counting inline `#[cfg(test)]` modules. Unit tests go in `<module>/tests.rs`.

## Decision Record
`kind: framing`

Orchestrator #15977 ruled E1 to E4 at 10:22 CDT on 2026-10-18 and took every
recommendation. D5 to D9 are the Plan Writer's, recorded for review.

- **E1, carrier.** The verbs are `gdaemon node install` and `gdaemon node enroll`,
  in `crates/gdaemon/src/node_setup/`. Every node runs gdaemon; gclient is optional
  on a headless node. node-halves Ruling 8 ("`gobby install` places the pinned
  runtime; gdaemon verifies and runs it and never installs") reads as: the serving
  daemon never installs, and an explicit operator subcommand may. `gdaemon serve`
  installs nothing. Rejected: gclient command-mode verbs, whose dispatcher needs a
  daemon URL and token before any verb runs.
- **E2, Python login.** Python `gobby auth login` is deleted (1.5). `gobby auth key`
  stays. `tests/e2e/test_hub_node_pair.py::pair_node` enrolls with
  `gdaemon node enroll` and is the no-Python enrollment proof.
- **E3, scope.**
  - (i) Binaries come from `cargo build --release` on the node.
    `gdaemon node install` promotes them with `promote_workspace_binary_set`'s
    semantics (1.2), including the identity stamp. Release downloads are rejected:
    `gmcp` has no release workflow, and the stamped set installs only from a source
    build (`src/gobby/install/bin_set_coherence.py::workspace_build_only`).
  - (ii) The operator starts the node with `gdaemon serve`. The service unit stays
    with #21571 (Client operator verbs) D1; 1.10 says so.
  - (iii) Node.js 22.12.0 or later and npm are node requirements for SRT.
- **E4, hooks.** The node installs claude, codex and droid hooks, the `gmcp` MCP
  entry, and codex hook trust. It installs no shared content, router commands,
  statusline, `autoMemoryEnabled`, or `validate_settings.py`. grok and agy send no
  hooks from a node (hub-mode Decision 7, `NODE_HOOKLESS_PROVIDERS`). Spawned claude
  and codex runs on a node read the node's global hook config, because the Python
  installer cleans project-level hooks. Interactive node sessions register at the
  hub only after #24151 (S2.11 hub half); 1.7 to 1.9 do not wait on it.
- **D5, promotion records.** The node writes the identity stamp
  `.gdaemon-schema-identity.json`, as #15977 required. It skips
  `.<name>-release-identity.json`, the version stamp and the install sidecar: only
  Python reads them (`src/gobby/install/bin_release_identity.py`,
  `src/gobby/install/bin_freshness*.py`), and no crate does.
- **D6, SRT tarball.** `npm pack @anthropic-ai/sandbox-runtime@0.0.79 --pack-destination <staging> --ignore-scripts`
  fetches the tarball, and the stager checks it against the pinned SHA-256. npm is
  already required, so gdaemon needs no web-PKI HTTPS client. The lockfile and runner
  are embedded from their D11 copies with `include_bytes!` and checked against their
  pins.
- **D7, password and trust prompts.** On a TTY, enroll prompts `Password: ` with
  echo off. Otherwise it reads one line from stdin, which the e2e fixture pipes.
  Without `--fingerprint`, enroll shows the fingerprint and asks `Trust this
  certificate? [y/N]` on a TTY; with stdin not a TTY it refuses and asks for
  `--fingerprint`. `--email` is required.
- **D8, home trust.** Node install does not call `seed_gobby_home_trust`. Each node
  spawn seeds its workspace's trust at launch (node-halves 2.6), and an operator's
  own CLI asks on first use.
- **D9, Python remote install.** Python `gobby install`'s remote path stays as it
  is, except that its remediation strings name `gdaemon node enroll` (1.5). No node
  path reaches it, and retiring it belongs with #21591 (Install and setup route
  family) and the parked #20151 (installer wizard). 1.10 removes it from the node
  setup docs.
- **D10, SRT fixture.** The harness's `npm pack` stub copies the real registry
  tarball, committed as `crates/gdaemon/tests/fixtures/node_setup/sandbox-runtime-0.0.79.tgz`
  (3,975,831 bytes). `.pre-commit-config.yaml` excludes that exact path from
  `check-added-large-files --maxkb=500`. Each SRT bump adds about 4 MB to history.
  Rejected: a tarball composed from the `tests/fixtures/srt/` pieces. The receipt
  records `tarball_sha256` and the verb stages with the pinned consts, so the verb's
  own staging path (1.6.1, 1.9.3, 1.10.3) could not run without Python or a pin
  seam in the shipped binary.
- **D11, embedded Python-tree files.** gdaemon embeds crate-local copies, under
  `crates/gdaemon/assets/node_setup/`, of the SRT lockfile, `srt_runner.mjs`, and
  the claude and codex hook templates. `workspace_source_identity`
  (`src/gobby/install/bin_release_identity.py`) hashes only the crate closure and the
  root Cargo and toolchain files. Embedding a `src/gobby` file would change
  gdaemon's bytes with no bump, which the same-version promotion guard cannot see. A
  test under `crates/gdaemon/tests` compares each copy with its original byte for
  byte. A Python-side edit then fails that test until the copy changes, and changing
  the copy bumps gdaemon.

## P1: Node install and enrollment
`kind: framing`

One phase, no sub-epic. The verb order inside `gdaemon node install` is: bootstrap
(1.1), binaries (1.2), SRT (1.6), then claude (1.7), codex (1.8) and droid (1.9)
hooks. CLI configs change only after the steps before them succeed.

### 1.1 `gdaemon node install` writes the node bootstrap [category: code]
`kind: deliverable`

Targets:
- `crates/gdaemon/src/main.rs::Command`
- `crates/gdaemon/src/main.rs::main`
- `crates/gdaemon/src/lib.rs`
- `crates/gdaemon/src/node_setup/mod.rs`
- `crates/gdaemon/src/node_setup/bootstrap.rs`
- `crates/gdaemon/src/node_setup/files.rs`
- `crates/gdaemon/tests/node_setup.rs`
- `crates/gdaemon/tests/node_setup_support/mod.rs`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: bumps this leaf's binaries in `MANAGED_BIN_VERSION_PINS` (Release ownership)

Python surface replaced: the remote branch of
`src/gobby/cli/_install_daemon.py::_run_install_preflight`, which demands
`database_url`, and the hand-written node bootstrap in `docs/guides/shared-stack.md`.

- `main.rs` gains `Command::Node { command: NodeCommand }` under `#[cfg(unix)]`,
  dispatched to `gobby_daemon::node_setup::run`. `lib.rs` declares
  `#[cfg(unix)] pub mod node_setup;`. `NodeCommand::Install { hub: String }` takes a
  required `--hub`.
- `files.rs::replace_file(path, bytes, mode)` writes a temporary file beside `path`
  with `create_new` and `mode`, fsyncs it, renames it over `path`, fsyncs the
  directory, and removes the temporary file on any failure. Every later leaf writes
  through it.
- `bootstrap.rs` resolves the home with `gobby_core::bootstrap::bootstrap_path` and
  creates it 0700.
  - Absent bootstrap: it writes `datastore_mode: remote` and `hub_daemon_url: <hub>`
    as YAML (0600), then re-parses the bytes with
    `gobby_core::bootstrap::parse_hub_database_bootstrap`. That parse normalizes the
    URL and refuses a non-http(s) URL and the node's own origin
    (`crates/gcore/src/bootstrap/run_mode.rs`); on refusal nothing is written. Ports
    and bind host keep gcore's defaults.
  - Present bootstrap in remote mode with the same normalized hub: kept unchanged.
  - Present bootstrap with another hub: refused, "this node is enrolled with
    <hub>; node install never re-homes a node".
  - Present bootstrap in local or hub mode: refused, naming the mode and
    "node install needs a fresh GOBBY_HOME".
- `node_setup_support/mod.rs` is the no-Python harness (Constraints).

Research context:
- `tests/e2e/test_hub_node_pair.py::pair_node` already runs `gdaemon serve` on a
  bootstrap with no `database_url`; only the Python installer demands one.
- gcore has bootstrap readers only. `front_door::tls::write_owner_only` is
  create-only, so it cannot replace a file.
- Planned checks: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-daemon -E 'binary(node_setup)'`.

**Acceptance:**

- 1.1.1 - On an empty home, `gdaemon node install --hub <url>` writes a 0600 remote bootstrap that gcore parses to that hub, with no tripwire fired. test: `crates/gdaemon/tests/node_setup.rs::install_writes_node_bootstrap_without_python`.
- 1.1.2 - A bootstrap for the same hub is kept byte for byte, and one for another hub, a local or hub bootstrap, or a `--hub` naming the node's own origin is refused with nothing written. test: `crates/gdaemon/tests/node_setup.rs::install_refuses_local_or_rehomed_bootstrap`.
- 1.1.3 - `gdaemon` is one patch version above the executor's starting tip in `crates/gdaemon/Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 1.2 `gdaemon node install` promotes the sibling binaries [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `crates/gdaemon/src/node_setup/mod.rs`
- `crates/gdaemon/src/node_setup/binaries.rs`
- `crates/gdaemon/tests/node_setup.rs`
- `crates/gdaemon/tests/node_setup_support/mod.rs`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: bumps this leaf's binaries in `MANAGED_BIN_VERSION_PINS` (Release ownership)

Python surface replaced:
`src/gobby/install/bin_set_coherence.py::promote_workspace_binary_set`.

- The source directory is the parent of `std::env::current_exe()`, canonicalized.
  When it is `<gobby_home>/bin`, the step prints that the binaries are installed and
  skips.
- Members: the stamped set `gcode`, `gdaemon`, `ghook`, then `gmcp` and `gterm`. A
  missing member refuses before any copy, naming each missing file and the two
  `cargo build` lines from the Overview.
- Identity: each stamped member is probed as
  `probe_set_member_identity` does (`gdaemon schema version --json`, else
  `<bin> schema-identity --json`, 10 s). Each result must carry the six fields
  `src/gobby/storage/schema_identity_pin.py::validate_identity` requires, and the
  three must be equal; otherwise the step refuses with nothing promoted.
- Promotion, per member in order: take a non-blocking `flock` on
  `<bin>/.locks/<name>.lock`; copy into a `.<name>-staging-*` directory under
  `<bin>`, fsync, `chmod 0755`; on macOS run `/usr/bin/codesign -f -s - <staged>`
  (30 s); rename over `<bin>/<name>`. A failure reports promoted and unpromoted
  members as Python does.
- After all five: the stamp `<bin>/.gdaemon-schema-identity.json` holds the identity
  as compact JSON with sorted keys and a trailing newline
  (`src/gobby/storage/schema_identity_pin.py::stamp_bytes`), 0644, through
  `replace_file`. No release identity, version stamp or sidecar is written (D5).
- The harness gains stub siblings: `gcode` and `ghook` scripts that print the
  identity the copied `gdaemon` prints, and `gmcp` and `gterm` scripts. On macOS
  `codesign` signs a script through extended attributes.

Research context:
- `promote_workspace_binary_set` probes, validates, requires agreement, locks with
  `bin_freshness_locks.py::try_acquire_native_bin_lock`, stages through
  `bin_freshness_promotion.py::stage_and_promote_binary_file`, signs with
  `_codesign_workspace_binary`, and stamps with `_write_identity_stamp`.
- `SET_MEMBERS` (`src/gobby/utils/native_bin.py`) is `gcode`, `gdaemon`, `ghook`.
  `gmcp` (package `gobby-mcp`) and `gterm` (package `gobby-terminal`, feature
  `vt-engine`, Zig 0.16) are promoted unstamped, as gmcp-stdio 1.4 and the gterm
  installer do. node-halves runs `<gobby_home>/bin/gterm` as the node terminal host.
- Planned checks: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-daemon -E 'binary(node_setup)'`.

**Acceptance:**

- 1.2.1 - Install from a release directory promotes all five members to new inodes at 0755 and writes the stamp bytes, with no tripwire fired. test: `crates/gdaemon/tests/node_setup.rs::install_promotes_sibling_binaries_without_python`.
- 1.2.2 - A missing member or a disagreeing identity is refused before any member is promoted, and install run from `<gobby_home>/bin` skips promotion. test: `crates/gdaemon/tests/node_setup.rs::install_refuses_incoherent_or_missing_siblings`.
- 1.2.3 - `gdaemon` is one patch version above the executor's starting tip in `crates/gdaemon/Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 1.3 `gdaemon node enroll` [category: code] (depends: 1.2)
`kind: deliverable`

Targets:
- `crates/gdaemon/src/node_setup/mod.rs`
- `crates/gdaemon/src/node_setup/enroll.rs`
- `crates/gdaemon/src/node_setup/enroll/tests.rs`
- `crates/gdaemon/src/node_setup/hub_pin.rs`
- `crates/gdaemon/src/node_setup/machine_id.rs`
- `crates/gdaemon/tests/node_setup.rs`
- `crates/gdaemon/tests/node_setup_support/mod.rs`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: bumps this leaf's binaries in `MANAGED_BIN_VERSION_PINS` (Release ownership)

Python surface replaced: `src/gobby/cli/auth_login.py::enroll` and `::login`, and
`src/gobby/utils/machine_id.py::_get_or_create_machine_id`.

**Granularity:** one leaf with seven acceptance items. They are the stages of one
enrollment transaction (pin, mint, verify, publish, settle), and the rollback items
test the publish and settle code the success item runs.

`NodeCommand::Enroll { hub, email, fingerprint: Option, label: Option, insecure }`
ports `enroll` in Python's order and with its messages, except where noted:

- Bootstrap: must be remote mode with `hub_daemon_url`; `--hub` must equal it after
  normalization ("enroll never re-homes a node"). `--fingerprint` is refused with
  http. Plain http to a host that is not loopback needs `--insecure`.
- `machine_id.rs`: reads `<gobby_home>/machine_id`; when absent, takes `flock` on
  `<gobby_home>/.machine_id.lock`, re-reads, and writes a v4 UUID 0600 through
  `replace_file`.
- `hub_pin.rs`: for https, a TLS handshake to the hub's host and port with a rustls
  `ServerCertVerifier` that accepts and records the end-entity certificate. Its
  fingerprint is `front_door::tls::fingerprint` of the PEM. Enroll prints it, then
  matches `--fingerprint` or asks (D7). The PEM is staged in `<gobby_home>/tls/`
  (0700).
- Password per D7. `POST /api/auth/keys/bootstrap` with `email`, `password`,
  `machine_id`, `hostname` (`front_door::tls::machine_hostname`), `os` (`Darwin` or
  `Linux`, Python's `platform.system()` values), and `label` (default hostname). The
  hyper client uses `front_door::tls::pinned_client_config` with the staged PEM,
  reads no proxy environment, and has a 10 s timeout. A certificate name failure
  gives `_network_error`'s SAN message; the password was not sent.
- Verification as `_verified`: 401 is "the hub rejected the email or password.";
  the key passes `gobby_core::api_key_format::parse`; `key_id`, `user_id` and
  `machine_id` are UUIDs; `machine_id` is ours; `hint` equals `&key[45..49]` (the
  last four body characters, `src/gobby/utils/api_key_format.py::hint`); `key_id`
  differs from the prior `api_key_id`.
- Publish as `_publish`, under `flock` on `<gobby_home>/.bootstrap.yaml.lock` (0600):
  write `tls/hub.pem` (0600), set `api_key`, `api_key_id` and `hub_cert` (or remove
  `hub_cert` for http) in the YAML mapping, and replace the bootstrap (0600). The
  YAML round trip keeps key order.
- Settle as `_settle` and revoke as `_revoke` (`POST /api/auth/login`, the
  `gobby_session` cookie, `DELETE /api/auth/keys/{id}`). Messages that name
  `gobby auth key` name "`api_key_id` in bootstrap.yaml" and "the hub UI" instead.
- Success prints "Enrolled with <origin>: API key <hint> (id <key_id>)."

Research context:
- `crates/gdaemon/src/front_door/relay.rs::HubRelay::forward` turns transport errors
  into 502 responses, so enroll uses the hyper client directly to keep the SAN
  message.
- `gobby_core::machine::local_os_name` returns `macos`; Python sent `Darwin`.
- Tests use `crates/gdaemon/tests/node_support/fake_hub.rs::FakeHub` for scripted
  replies behind `crates/gdaemon/tests/common/mod.rs::start_tls_front_door` with a
  `common::self_signed` certificate, and a key that `api_key_format::parse`
  accepts. The Python behaviors come from `tests/cli/test_auth_login.py`.
- Planned checks: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-daemon -E 'binary(node_setup)'`.

**Acceptance:**

- 1.3.1 - Against a self-signed TLS stub hub, enroll with the matching fingerprint and a piped password publishes `api_key`, `api_key_id`, `hub_cert` and `tls/hub.pem` at 0600 and creates `machine_id`, with no tripwire fired. test: `crates/gdaemon/tests/node_setup.rs::enroll_pins_hub_and_publishes_key_without_python`.
- 1.3.2 - Each malformed or mismatched hub reply (bad key, non-UUID ids, foreign machine, wrong hint, reused key id) refuses with nothing saved and names the unverified key id. test: `crates/gdaemon/tests/node_setup.rs::enroll_refuses_unverified_reply`.
- 1.3.3 - A local bootstrap, a different `--hub`, `--fingerprint` with http, and http to a non-loopback host without `--insecure` are refused before any request. test: `crates/gdaemon/tests/node_setup.rs::enroll_refuses_local_bootstrap_and_hub_mismatch`.
- 1.3.4 - A 401, an unreachable hub, a stalled hub past 10 s, a fingerprint mismatch and a certificate that does not cover the host each leave the prior enrollment byte for byte, and the SAN case says the password was not sent. test: `crates/gdaemon/tests/node_setup.rs::enroll_failure_keeps_prior_enrollment`.
- 1.3.5 - An unusable bootstrap lock or an unwritable home rolls back the pin and revokes the minted key through login and DELETE, and a failed revoke names the key id. test: `crates/gdaemon/tests/node_setup.rs::enroll_rolls_back_and_revokes`.
- 1.3.6 - Settle reports committed when the bootstrap names the key, and unresolved when the prior pin cannot be restored. test: `crates/gdaemon/src/node_setup/enroll/tests.rs::settle_decides_by_bootstrap`.
- 1.3.7 - `gdaemon` is one patch version above the executor's starting tip in `crates/gdaemon/Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 1.4 Rust remediation names `gdaemon node enroll` [category: code] (depends: 1.3)
`kind: deliverable`

Targets:
- `crates/gdaemon/src/state.rs::NodeServices::prepare`
- `crates/gdaemon/tests/nodes.rs::node_startup_requires_a_pinned_hub`
- `crates/gdaemon/tests/run_modes.rs::node_prepare_takes_only_the_claim`
- `crates/gdaemon/tests/node_setup.rs`
- `crates/gcore/src/local_token.rs::read_api_key_at`
- `crates/gcode/src/cli_error.rs::unauthorized_recovery`
- `crates/ghook/src/diagnose.rs::*` — scope-reason: `AUTH_401_REMEDIATION` and its inline test's assertion name the new verb
- `crates/gclient/src/startup.rs::StartupError`
- `crates/gclient/tests/startup.rs::non_string_daemon_url_reports_configuration_error`
- `crates/gdaemon/Cargo.toml`
- `crates/gcode/Cargo.toml`
- `crates/ghook/Cargo.toml`
- `crates/gclient/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: bumps this leaf's binaries in `MANAGED_BIN_VERSION_PINS` (Release ownership)
- `src/gobby/code_index/gcode_gateway.py::*` — scope-reason: only when the gcode floors are still here, they move out to `gcode_versions.py` and the module imports them back (Release ownership)
- `src/gobby/code_index/gcode_versions.py`

Python surface replaced: the Python `gobby auth login` these messages send a node
operator to.

- Each message below names `gdaemon node enroll` where it named `gobby auth login`:
  `NodeServices::prepare`'s two refusals, `read_api_key_at`,
  `unauthorized_recovery`'s two texts, `AUTH_401_REMEDIATION`, and
  `StartupError`'s token message. The tests that assert them follow.
- The gcode floor: when `MIN_GCODE_PRUNE_BUDGET_VERSION` is a literal in
  `gcode_gateway.py`, this leaf moves `MIN_GCODE_GRAPH_VERSION`,
  `MIN_GCODE_PRUNE_BUDGET_VERSION` and `GCODE_ALLOW_MISSING_INDEXED_FILE_VERSION` to
  the new `gcode_versions.py`, derives the prune floor from the `gcode` pin, and
  imports them back into `gcode_gateway.py`, as code-index-family 1.1 specifies.
  The validator's production-size-growth rule requires that new file for any edit
  to the 969-line `gcode_gateway.py`.
- A guard test walks every `crates/*/src` file and fails on the literal
  `gobby auth login`.

Research context:
- Sites: `crates/gdaemon/src/state.rs` lines 39 and 51, `crates/gcore/src/local_token.rs`
  line 15, `crates/gcode/src/cli_error.rs` lines 183 and 186,
  `crates/ghook/src/diagnose.rs` lines 41 and 311, `crates/gclient/src/startup.rs`
  line 147; tests `crates/gdaemon/tests/nodes.rs` line 482,
  `crates/gdaemon/tests/run_modes.rs` line 54, `crates/gclient/tests/startup.rs`
  line 871.
- `tests/code_index/test_gcode_gateway.py` asserts `GCODE_PIN == MIN_GCODE_PRUNE_BUDGET_VERSION`.
- Planned checks: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-daemon -p gobby-core -p gobby-code -p gobby-hooks -p gobby-client`; `GOBBY_TEST_PROTECT=1 uv run pytest tests/install/test_version_pins.py tests/code_index/test_gcode_gateway.py -v`.

Consumers unchanged:
- `tests/tasks/expansion/test_contract_validation_criteria.py` — no-edit-reason: uses `gobby auth login` as an arbitrary phrase in validation-criteria fixtures.
- `crates/gcore/src/grant/acquisition.rs` — no-edit-reason: calls `read_api_key_at` and propagates its error text unchanged.

**Acceptance:**

- 1.4.1 - `gdaemon serve` on an unenrolled node home refuses naming `gdaemon node enroll`, with no tripwire fired. test: `crates/gdaemon/tests/node_setup.rs::serve_without_key_names_enroll_without_python`.
- 1.4.2 - No file under `crates/*/src` contains `gobby auth login`. test: `crates/gdaemon/tests/node_setup.rs::no_crate_names_python_login`.
- 1.4.3 - The node startup and claim tests assert the new remediation. test: `crates/gdaemon/tests/nodes.rs::node_startup_requires_a_pinned_hub`.
- 1.4.4 - `gcode`, `gdaemon` and `ghook` are one patch version above the executor's starting tip in their `Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`, and so is `gclient`. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.
- 1.4.5 - The gcode prune floor equals the bumped `gcode` pin. test: `tests/code_index/test_gcode_gateway.py::test_managed_gcode_pin_matches_prune_budget_floor`.

### 1.5 Python login retires and pair_node enrolls with the Rust verb [category: code] (depends: 1.3, #24604)
`kind: deliverable`

Targets:
- `src/gobby/cli/auth_login.py::*` — scope-reason: deletes `login` and every helper only it reaches; `_key_data`, `_key_client` and the module docstring name `gdaemon node enroll`
- `src/gobby/cli/auth.py::*` — scope-reason: registers `key` only
- `src/gobby/cli/hub_backup/bootstrap_restore.py::restore_archived_bootstrap`
- `src/gobby/cli/sessions_dirty_paths.py::_LOGIN_HINT`
- `src/gobby/cli/sessions_dirty_paths.py::approve_dirty_paths`
- `src/gobby/cli/installers/remote_preflight.py::*` — scope-reason: its three remediation strings name `gdaemon node enroll` (D9)
- `src/gobby/hooks/inbox.py::_drain_hook_inbox_once_locked`
- `src/gobby/servers/middleware/auth.py::_LOGIN_GUIDANCE`
- `src/gobby/utils/daemon_client.py::DAEMON_AUTH_REMEDIATION`
- `src/gobby/install/shared/skills/gobby/references/admin/authentication.md`
- `docs/reference-audit/admin.json::*` — scope-reason: the `gobby auth login` operation leaves `operations` and a `gdaemon node enroll` `native_cli` entry replaces it
- `tests/cli/test_auth_login.py::*` — scope-reason: deletes the login tests and their fake hub; the `gobby auth key` tests stay
- `tests/cli/test_pack.py::_REENROLL_LINE`
- `tests/config/test_remote_ui_auth.py::test_http_server_allows_remote_ui_with_mandatory_auth`
- `tests/contracts/http/auth_missing_auth.json::*` — scope-reason: the recorded guidance text names the new verb
- `tests/servers/test_auth_middleware.py::test_protected_routes_require_auth_when_enabled`
- `tests/servers/test_http_middleware.py::*` — scope-reason: the guidance literal and the prefix-matrix assertion name the new verb
- `tests/utils/test_daemon_client.py::AUTH_REMEDIATION`
- `tests/workflows/test_dirty_edit_grants.py::*` — scope-reason: three assertions name the new verb
- `tests/e2e/test_auth_login_e2e.py::*` — scope-reason: `_login` becomes `_enroll`, which runs `gdaemon node enroll` with a tripwire-only `PATH`; both tests drive it
- `tests/e2e/test_hub_node_pair.py::*` — scope-reason: `pair_node` enrolls through `_enroll` and asserts no tripwire fired
- `tests/fixtures/test_gdaemon_binary.py::test_pair_node_launches_prepared_binary`
- `docs/contracts/parity-ledger-status.d/daemon-config-auth-infra.json::*` — scope-reason: the `CLI gobby auth login` row becomes `dropped by decision #24657`, and the `auth key` notes' line references follow the shortened module
- `docs/contracts/parity-ledger.md`

Python surface replaced: `gobby auth login` itself.

- `auth_login.py` keeps `key`, `key_list`, `key_revoke` and what they reach
  (`_key_*`, `_mint_key`, `_rotate_key`, `_publish_rotation`, `_restore_rotation`,
  and the shared helpers they call). Every message in Python that sends a node
  operator to `gobby auth login` names `gdaemon node enroll`.
- `_enroll(home, *args)` runs `<native bin dir>/gdaemon node enroll --hub <origin> --email TEST_EMAIL ...`
  with the env from `prepare_daemon_env`, `GOBBY_HOME`, and `PATH` set to a
  directory holding only tripwire `python`, `python3` and `uv` scripts. It pipes
  `TEST_PASSWORD`. `pair_node` calls it with `--hub` and `--fingerprint` and asserts
  the tripwire marker is absent. The two e2e tests become
  `test_enroll_pins_self_signed_hub` and `test_enroll_refuses_host_outside_sans`.
- `admin.json`: the `gobby auth login` operation leaves `operations`, and a
  `native_cli` entry for `gdaemon node enroll --hub URL --email EMAIL [--fingerprint sha256:...] [--label LABEL] [--insecure]`
  points at `crates/gdaemon/src/node_setup/enroll.rs::run` with the same reference
  and the `source-inventory` evidence. `authentication.md` step 3 names the verb.
- Parity ledger: the `CLI gobby auth login` row's status becomes
  `dropped by decision #24657`, with a note naming `gdaemon node enroll`
  (`crates/gdaemon/src/node_setup/enroll.rs::run`). `docs/contracts/parity-ledger.md`
  is regenerated with `scripts/generate_parity_ledger.py`. The dropped row keeps its
  surface key.

Research context:
- `scripts/generate_parity_ledger.py::render_ledger` refuses a surface absent from
  the inventory unless its status is `dropped by decision #N`, and statuses other
  than `proxied`, `delegated` and that drop are invalid. The row therefore changes
  in the leaf that deletes the command.
- `tests/skills/reference_library_helpers.py::documentation_errors` resolves every
  `gobby ` operation in the Click registry, so the deleted command must leave
  `operations` in the same leaf.
- `tests/fixtures/test_gdaemon_binary.py` patches `pair._login` by name.
- Planned checks: `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/cli/test_auth_login.py tests/cli/test_pack.py tests/config/test_remote_ui_auth.py tests/servers/test_auth_middleware.py tests/servers/test_http_middleware.py tests/utils/test_daemon_client.py tests/workflows/test_dirty_edit_grants.py tests/fixtures/test_gdaemon_binary.py tests/skills/test_reference_library.py tests/test_parity_ledger.py tests/contracts/test_http_corpus.py tests/e2e/test_auth_login_e2e.py tests/e2e/test_hub_node_pair.py -v`.

Consumers unchanged:
- `src/gobby/cli/install.py` — no-edit-reason: its remote path keeps calling `run_remote_preflight` (D9).
- `src/gobby/cli/sessions.py` — no-edit-reason: registers `approve_dirty_paths`, whose signature is unchanged.
- `tests/hooks/test_inbox.py` — no-edit-reason: asserts no remediation text from `_drain_hook_inbox_once_locked`.
- `tests/hooks/test_inbox_barrier_deadline.py` — no-edit-reason: asserts no remediation text from `_drain_hook_inbox_once_locked`.

**Acceptance:**

- 1.5.1 - `pair_node` enrolls with `gdaemon node enroll` under a tripwire-only `PATH`, the tripwire stays silent, and the enrolled node relays and handshakes. test: `tests/e2e/test_hub_node_pair.py::test_node_enrolls_and_relays`.
- 1.5.2 - `gdaemon node enroll` pins a self-signed live hub. test: `tests/e2e/test_auth_login_e2e.py::test_enroll_pins_self_signed_hub`.
- 1.5.3 - `gobby auth login` is no longer a registered command, and `gobby auth key` still is. test: `tests/cli/test_auth_login.py::test_login_command_is_gone`.
- 1.5.4 - The audit lists `gdaemon node enroll` and no `gobby auth login` operation. test: `tests/skills/test_reference_library.py::test_reference_contract_3_2_2`.
- 1.5.5 - The middleware guidance names the new verb. test: `tests/servers/test_auth_middleware.py::test_protected_routes_require_auth_when_enabled`.
- 1.5.6 - The committed parity ledger matches the inventory with `CLI gobby auth login` dropped by decision #24657. test: `tests/test_parity_ledger.py::test_committed_ledger_matches_current_public_inventory`.

### 1.6 `gdaemon node install` stages the pinned SRT [category: code] (depends: 1.4, #24374)
`kind: deliverable`

Targets:
- `crates/gdaemon/src/node_setup/mod.rs`
- `crates/gdaemon/src/node_setup/srt.rs`
- `crates/gdaemon/src/node_setup/srt/tests.rs`
- `crates/gdaemon/src/nodes/agents/sandbox/runtime.rs`
- `crates/gdaemon/assets/node_setup/srt-package-lock.json`
- `crates/gdaemon/assets/node_setup/srt_runner.mjs`
- `crates/gdaemon/tests/node_setup.rs`
- `crates/gdaemon/tests/node_setup_support/mod.rs`
- `crates/gdaemon/tests/fixtures/node_setup/sandbox-runtime-0.0.79.tgz`
- `.pre-commit-config.yaml`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: bumps this leaf's binaries in `MANAGED_BIN_VERSION_PINS` (Release ownership)

Python surface replaced: `src/gobby/cli/install_setup_srt.py::_install_srt_runtime`.

The step ports `_install_srt_runtime` in its order, under the install lock #24374
uses:

- A verified install at `<gobby_home>/tools/srt/<SRT_VERSION>` is reused (the
  `.previous` backup is removed). Otherwise:
- `node` and `npm` are resolved from `PATH`; `node --version` must be at least
  22.12.0 (`NODE_MIN_VERSION`). Either missing is refused naming the requirement.
- Staging is a `.<version>-*` directory beside the target. D6's `npm pack` writes
  the tarball, whose SHA-256 must equal the pin. `package.json` is
  `install_setup_srt.py::_PACKAGE_JSON`. The embedded lockfile (the D11 copy of
  `src/gobby/install/srt-package-lock.json`) is written as `package-lock.json` and
  checked. `npm ci --ignore-scripts --no-audit --no-fund --omit=dev` runs (180 s).
  The installed package's name and version must match.
- The patch table ports `src/gobby/agents/srt_package_patch.py`: each entry's file,
  original and expected SHA-256, needle and replacement, plus the macOS monitor
  needle. A file already at its expected hash is skipped; any other hash refuses.
- The embedded runner (the D11 copy of `src/gobby/agents/srt_runner.mjs`) is written as `runner.mjs`
  (0700) and checked. `receipt.json` is `SrtRelease.receipt_fields()` plus `node`.
- The content manifest uses #24374's builder (widened to `pub(crate)` in
  `runtime.rs`). The tree is made immutable as `make_srt_installation_immutable`
  does (0555 for directories, executables and the two `apply-seccomp` helpers;
  0444 otherwise). Promotion is `_promote_install` (rename the target to
  `.<version>.previous`, rename staging in, remove the backup, restore it on
  failure). #24374's verifier then runs.
- The pins (tarball, lockfile, runner and the four patched-file hashes, with every
  receipt field) sit beside `SRT_VERSION` in `runtime.rs`. Its lockout remediation
  `run \`gobby install srt\`` becomes `run \`gdaemon node install\``.
- The harness gains stub `node` (prints `v22.12.0`) and `npm`. `npm pack` copies the
  committed registry tarball fixture (D10), and `npm ci` extracts it into
  `node_modules/@anthropic-ai/sandbox-runtime` with `/usr/bin/tar`. All four patch
  targets are in that package. The executor fetches the fixture once with
  `npm pack @anthropic-ai/sandbox-runtime@0.0.79`, and a test checks its hash. The
  fixture's exact path joins the `exclude` of `.pre-commit-config.yaml`'s
  `check-added-large-files` hook.

Research context:
- `SrtRelease` (`src/gobby/utils/dependency_requirements.py`) pins version 0.0.79
  and the hashes; `verify_srt_installation_locked` checks the runner, lockfile,
  receipt, patched files, manifest and the Node version.
- Planned checks: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-daemon -E 'binary(node_setup) | binary(node_agents)'`.

**Acceptance:**

- 1.6.1 - Install stages SRT from the fixture with stub `node` and `npm`, #24374's verifier accepts it, and a second run reuses it, with no tripwire fired. test: `crates/gdaemon/tests/node_setup.rs::install_stages_pinned_srt_without_python`.
- 1.6.2 - A tarball off its pin, Node older than 22.12.0, or no `npm` is refused, leaving no staging directory and no install. test: `crates/gdaemon/tests/node_setup.rs::install_refuses_srt_off_pin`.
- 1.6.3 - The Rust pins, receipt fields and patch table equal the Python ones (read with `include_str!`), the lockfile and runner copies equal their Python originals byte for byte, and the embedded lockfile, runner and fixture match their pins. test: `crates/gdaemon/tests/node_setup.rs::srt_pins_match_python`.
- 1.6.4 - Patches apply once, skip a patched file, and refuse unknown bytes; a failed promotion restores the previous install. test: `crates/gdaemon/src/node_setup/srt/tests.rs::patches_and_promotion_follow_python`.
- 1.6.5 - `gdaemon` is one patch version above the executor's starting tip in `crates/gdaemon/Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 1.7 `gdaemon node install` installs claude hooks and the MCP entry [category: code] (depends: 1.6, #24372, #24355)
`kind: deliverable`

Targets:
- `crates/gdaemon/src/node_setup/mod.rs`
- `crates/gdaemon/src/node_setup/hooks.rs`
- `crates/gdaemon/src/node_setup/claude.rs`
- `crates/gdaemon/src/nodes/agents/repair.rs`
- `crates/gdaemon/assets/node_setup/claude-hooks-template.json`
- `crates/gdaemon/tests/node_setup.rs`
- `crates/gdaemon/tests/node_setup_support/mod.rs`
- `crates/gdaemon/tests/fixtures/node_setup/claude_golden.json`
- `tests/install/test_node_setup_golden.py`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: bumps this leaf's binaries in `MANAGED_BIN_VERSION_PINS` (Release ownership)

Python surface replaced: the hook, timeout and MCP steps of
`src/gobby/cli/installers/claude.py::install_claude`.

- A CLI's step runs when its command (`claude`, `codex`, `droid`) resolves on
  `PATH`; otherwise install prints that it was skipped.
- `hooks.rs` holds what 1.8 and 1.9 share: the command prefix
  `<quoted <gobby_home>/bin/ghook> --gobby-owned` with Python's `shlex.quote` rule
  (bare when every character is in `[A-Za-z0-9_@%+=:,./-]`, else single-quoted
  with `'` as `'"'"'`); the `__GOBBY_HOOK_COMMAND__` rewrite to
  `<prefix> --cli=<cli> --type=<hook>` (`hook_commands.py::rewrite_hook_template_commands`);
  and the timeout setter (`set_gobby_hook_timeouts`). The `shlex` crate is not
  used: it quotes `=` and `%`, which Python leaves bare, and codex's trust hash
  covers the command bytes. Merging uses `repair.rs`'s `merge_gobby_hook_groups`
  port, widened to `pub(crate)`.
- `claude.rs`: the template is `include_str!` of the D11 copy of
  `src/gobby/install/claude/hooks-template.json`; timeouts are 120 s with
  `SessionEnd` 60 s. `~/.claude/settings.json` is backed up as
  `settings.json.<unix seconds>.backup`, each hook type is merged, and the file is
  replaced with two-space JSON. The `gmcp` entry goes into `~/.claude.json`
  `mcpServers.gobby` under #24372's `~/.claude.json.lock` mkdir lock, keeping every
  other key.
- Golden: `claude_golden.json` holds an input `settings.json` and `.claude.json`
  and the expected `hooks` and `mcpServers.gobby`, with the home written as
  `$HOME`. The Python test sets `HOME` to a temporary directory and `GOBBY_HOME` to
  `$HOME/.gobby`, removes `GOBBY_NATIVE_BIN_DIR`, and creates executable
  `$HOME/.gobby/bin/ghook` and `gmcp`, so `resolve_native_bin` returns those paths
  (`src/gobby/utils/native_bin.py::native_bin_dir` reads `Path.home()`). It runs
  `install_claude(project, mode="global")` and compares; with
  `GOBBY_RECORD_NODE_SETUP_GOLDEN=1` it rewrites the golden. The Rust test uses the
  same layout, runs the verb, and compares parsed values.

Research context:
- `install_claude` also installs shared content, router commands, the statusline,
  `autoMemoryEnabled` and `validate_settings.py` (`shared.py::install_global_hooks`);
  E4 excludes them. The Python hook command does not use the hooks directory
  (`hook_commands.py::build_hook_command_prefix`).
- Key order in rewritten JSON is not part of the contract; the golden compares
  parsed values.
- Planned checks: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-daemon -E 'binary(node_setup)'`; `GOBBY_TEST_PROTECT=1 uv run pytest tests/install/test_node_setup_golden.py -v`.

**Acceptance:**

- 1.7.1 - Install over the golden's input writes the golden's claude hooks and `gmcp` entry, keeps the user's handlers and other keys, and backs up `settings.json`, with no tripwire fired. test: `crates/gdaemon/tests/node_setup.rs::install_claude_hooks_match_python_golden`.
- 1.7.2 - Python's installer produces the claude golden. test: `tests/install/test_node_setup_golden.py::test_claude_golden_matches_python`.
- 1.7.3 - A CLI absent from `PATH` is skipped with its config untouched, and a path with a space is single-quoted as Python quotes it. test: `crates/gdaemon/tests/node_setup.rs::install_skips_absent_cli_and_quotes_like_python`.
- 1.7.4 - `gdaemon` is one patch version above the executor's starting tip in `crates/gdaemon/Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.
- 1.7.5 - The crate-local claude template copy equals `src/gobby/install/claude/hooks-template.json` byte for byte. test: `crates/gdaemon/tests/node_setup.rs::claude_template_copy_matches_python`.

### 1.8 `gdaemon node install` installs codex hooks, trust and the MCP entry [category: code] (depends: 1.7)
`kind: deliverable`

Targets:
- `crates/gdaemon/src/node_setup/mod.rs`
- `crates/gdaemon/src/node_setup/codex.rs`
- `crates/gdaemon/src/node_setup/codex/tests.rs`
- `crates/gdaemon/assets/node_setup/codex-hooks-template.json`
- `crates/gdaemon/tests/node_setup.rs`
- `crates/gdaemon/tests/node_setup_support/mod.rs`
- `crates/gdaemon/tests/fixtures/node_setup/codex_golden.json`
- `crates/gdaemon/tests/fixtures/node_setup/codex_trust_vectors.json`
- `tests/install/test_node_setup_golden.py`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: bumps this leaf's binaries in `MANAGED_BIN_VERSION_PINS` (Release ownership)

Python surface replaced: `src/gobby/cli/installers/codex.py::install_codex`'s hook,
trust and MCP steps.

- Hooks as `_install_hooks_file`: the template is `include_str!` of the D11 copy of
  `src/gobby/install/codex/hooks-template.json`; timeouts are 120 s with
  `Interrupt` and `SessionEnd` 3 s; a corrupt `~/.codex/hooks.json` is quarantined
  as Python names it; groups are merged.
- `~/.codex/config.toml` through `toml_edit`, so comments and layout survive:
  remove `features.codex_hooks`, set `features.hooks = true`, and write the trust
  state as `_ensure_codex_hook_trust_state` does. Each Gobby handler gets
  `[hooks.state."<resolved hooks.json>:<EventLabel>:<g>:<h>"] trusted_hash = "sha256:<hex>"`,
  hashed as `_normalized_codex_command_hook_hash` does (sorted compact JSON,
  non-ASCII kept, of the event label and the hook's `async`, whitespace-normalized
  `command`, `timeout` (default 600, minimum 1), `type`, optional `statusMessage`,
  and `matcher` for `CODEX_MATCHER_HASH_EVENTS`; `enabled` dropped). Stale and dead
  Gobby entries are removed. The prior file is copied to `config.toml.bak`.
- The MCP entry is `configure_mcp_server_toml`'s `[mcp_servers.gobby]` block with
  the `gmcp` command and `args = []`.
- Golden: `codex_golden.json` holds an input `hooks.json` and `config.toml` and the
  expected hooks, `features`, `hooks.state` and `mcp_servers.gobby` as JSON, with the
  home as `$HOME` and every `trusted_hash` value blanked on both sides. The hashed
  command embeds the temporary home, so no recorded hash can match another run. The
  Python test sets up the home as 1.7's does and runs `install_codex`.
- Trust vectors: `codex_trust_vectors.json` holds fixed hook entries with fixed
  commands and their `_normalized_codex_command_hook_hash` values. The Python test
  checks them and rewrites them under `GOBBY_RECORD_NODE_SETUP_GOLDEN=1`;
  `codex/tests.rs` replays them against the Rust hash.

Research context:
- Codex rejects a hook whose trust hash differs from its own computation, so the
  vectors pin the hash bytes.
- Planned checks: as 1.7.

**Acceptance:**

- 1.8.1 - Install over the golden's input writes the golden's codex hooks, trust-entry keys and `gmcp` block, each `trusted_hash` is the Rust hash of the hook it names, and a user comment in `config.toml` survives, with no tripwire fired. test: `crates/gdaemon/tests/node_setup.rs::install_codex_hooks_match_python_golden`.
- 1.8.2 - Python's installer produces the codex golden. test: `tests/install/test_node_setup_golden.py::test_codex_golden_matches_python`.
- 1.8.3 - `gdaemon` is one patch version above the executor's starting tip in `crates/gdaemon/Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.
- 1.8.4 - The Rust trust hash equals each vector's Python hash. test: `crates/gdaemon/src/node_setup/codex/tests.rs::trust_hash_matches_python_vectors`.
- 1.8.5 - Python's hash produces the trust vectors. test: `tests/install/test_node_setup_golden.py::test_codex_trust_vectors_match_python`.
- 1.8.6 - The crate-local codex template copy equals `src/gobby/install/codex/hooks-template.json` byte for byte. test: `crates/gdaemon/tests/node_setup.rs::codex_template_copy_matches_python`.

### 1.9 `gdaemon node install` installs droid hooks and the MCP entry [category: code] (depends: 1.8)
`kind: deliverable`

Targets:
- `crates/gdaemon/src/node_setup/mod.rs`
- `crates/gdaemon/src/node_setup/droid.rs`
- `crates/gdaemon/tests/node_setup.rs`
- `crates/gdaemon/tests/node_setup_support/mod.rs`
- `crates/gdaemon/tests/fixtures/node_setup/droid_golden.json`
- `tests/install/test_node_setup_golden.py`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: bumps this leaf's binaries in `MANAGED_BIN_VERSION_PINS` (Release ownership)

Python surface replaced: `src/gobby/cli/installers/droid.py::install_droid`'s hook
and MCP steps.

- Hooks: #24372's droid template renderer, with the node's `ghook`, merged over
  `~/.factory/hooks.json` across the droid hook names as `_merge_gobby_hooks` does.
- MCP: `~/.factory/mcp.json` `mcpServers.gobby` is the `gmcp` entry; other servers
  stay.
- Golden as 1.7's, with `install_droid(project, mode="global")`.
- The sequence test runs `gdaemon node install` and then `gdaemon node enroll`
  against the TLS stub hub in one sandbox, as 1.10 documents.

Research context:
- `install_droid` writes the flat global `hooks.json`; #24372's project file is
  `.factory/hooks/hooks.json`.
- Planned checks: as 1.7.

**Acceptance:**

- 1.9.1 - Install over the golden's input writes the golden's droid hooks and `gmcp` entry, with no tripwire fired. test: `crates/gdaemon/tests/node_setup.rs::install_droid_hooks_match_python_golden`.
- 1.9.2 - Python's installer produces the droid golden. test: `tests/install/test_node_setup_golden.py::test_droid_golden_matches_python`.
- 1.9.3 - Install then enroll in one sandbox leaves a node home that `NodeServices::prepare` accepts, with no tripwire fired. test: `crates/gdaemon/tests/node_setup.rs::documented_node_sequence_runs_without_python`.
- 1.9.4 - `gdaemon` is one patch version above the executor's starting tip in `crates/gdaemon/Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 1.10 Node setup docs [category: docs] (depends: 1.5, 1.9)
`kind: deliverable`

Targets:
- `docs/guides/shared-stack.md`
- `docs/guides/system-requirements.md`
- `docs/guides/cli-commands.md`
- `docs/guides/configuration.md`
- `docs/guides/admin-operations.md`
- `docs/guides/hub-install-contract.md`
- `docs/guides/http-endpoints.md`
- `docs/guides/gterminal-development-guide.md`
- `docs/contracts/secrets.md`
- `tests/docs/test_node_setup_docs.py`

Python surface replaced: the Python node setup in `shared-stack.md` (Client setup),
which writes a DSN, copies `.secret_kek`, and runs `gobby auth login` and
`gobby install`.

- Client setup becomes the Overview's five commands. It states the node
  requirements: a Rust toolchain, Zig 0.16 for `gterm`, Node.js 22.12.0 or later
  and npm for SRT, and git. It says no Python or Docker is needed, that the
  operator starts the node with `gdaemon serve` and the service unit is #21571 D1's,
  and that interactive node sessions register at the hub only after #24151.
- Every other page names `gdaemon node enroll` where it named `gobby auth login`.
  `cli-commands.md` documents both `gdaemon node` verbs and drops the login entry.
  `http-endpoints.md` shows the new guidance text.
- `tests/docs/test_node_setup_docs.py` walks `docs/**/*.md`, skipping `docs/audits/`,
  `docs/research/` and the generated `docs/contracts/parity-ledger.md`, whose dropped
  row keeps the surface key (1.5), and fails on `gobby auth login`.

Research context:
- Sites: `shared-stack.md` 102 to 105, `system-requirements.md` 235 to 246,
  `cli-commands.md` 463, 505 and 837, `configuration.md` 271 to 280,
  `admin-operations.md` 60, `hub-install-contract.md` 86, `http-endpoints.md` 50
  and 88, `gterminal-development-guide.md` 354, and `secrets.md` 27. `docs/audits/`
  and `docs/research/` are historical and stay.
- Planned checks: `GOBBY_TEST_PROTECT=1 uv run pytest tests/docs/test_node_setup_docs.py -v`.

**Acceptance:**

- 1.10.1 - Client setup documents the five commands, the node requirements, manual `gdaemon serve`, #21571 D1 and the #24151 note, and no Python step. file: `docs/guides/shared-stack.md`.
- 1.10.2 - The CLI reference documents `gdaemon node install` and `gdaemon node enroll` and has no `gobby auth login` entry. file: `docs/guides/cli-commands.md`.
- 1.10.3 - The documented sequence is the one the sandbox runs without Python. test: `crates/gdaemon/tests/node_setup.rs::documented_node_sequence_runs_without_python`.
- 1.10.4 - No Markdown page under `docs/` outside `docs/audits/`, `docs/research/` and the generated parity ledger names `gobby auth login`. test: `tests/docs/test_node_setup_docs.py::test_no_page_names_python_login`.

## E1 End-to-end verification
`kind: verification`

These are completion gates for P1. Except for plan validation
(`uv run gobby plans validate .gobby/plans/node-install-enrollment.md -p /Users/josh/Projects/gobby`),
none has run yet. Each must hold on `0.5.0`:

1. `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-daemon -p gobby-core -p gobby-code -p gobby-hooks -p gobby-client`
   must pass.
2. `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/install/ tests/cli/test_auth_login.py tests/e2e/test_auth_login_e2e.py tests/e2e/test_hub_node_pair.py tests/skills/test_reference_library.py tests/test_parity_ledger.py tests/docs/test_node_setup_docs.py`
   must pass.
3. On a machine of the operator's whose `PATH` has no Python, the Overview's five
   commands must enroll a node that the hub lists as connected, and
   `~/.gobby/tools/srt/0.0.79` must pass the node's verifier.

## V1 Plan Changelog
`kind: framing`

- 2026-10-18: First draft by Plan Writer gobby#16767 under #15977's rulings E1 to E4
  (10:22 CDT), with D5 to D9 recorded for review.
- 2026-10-18: Consensus between Plan Writer gobby#16767 and Plan Adversary gobby#16993
  at 7b35fc9. The review's four blocking findings are closed: the parity ledger row
  drops in 1.5, D10 commits the real SRT tarball, the goldens share one home layout
  and trust vectors pin the codex hash, and D11 embeds crate-local copies. Both nits
  are closed. The 1.4 floor move stays because production-size-growth requires it.
  The bare targets on `gcode_versions.py`, `runtime.rs` and `repair.rs` are
  re-scoped before any re-derive once their files carry symbols.

## M1 Task Manifest
`kind: manifest`

```yaml
- title: '`gdaemon node install` writes the node bootstrap'
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: '1.1.1: On an empty home, `gdaemon node install --hub <url>`
    writes a 0600 remote bootstrap that gcore parses to that hub, with no tripwire
    fired. test: `crates/gdaemon/tests/node_setup.rs::install_writes_node_bootstrap_without_python`.

    1.1.2: A bootstrap for the same hub is kept byte for byte, and one for another
    hub, a local or hub bootstrap, or a `--hub` naming the node''s own origin is refused
    with nothing written. test: `crates/gdaemon/tests/node_setup.rs::install_refuses_local_or_rehomed_bootstrap`.

    1.1.3: `gdaemon` is one patch version above the executor''s starting tip in `crates/gdaemon/Cargo.toml`,
    `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.'
  labels:
  - covers:node-install-enrollment:1.1:1.1.1
  - covers:node-install-enrollment:1.1:1.1.2
  - covers:node-install-enrollment:1.1:1.1.3
  tdd: true
  source_section: '1.1'
  implementation_domain: backend
- title: '`gdaemon node install` promotes the sibling binaries'
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  validation_criteria: '1.2.1: Install from a release directory promotes all five
    members to new inodes at 0755 and writes the stamp bytes, with no tripwire fired.
    test: `crates/gdaemon/tests/node_setup.rs::install_promotes_sibling_binaries_without_python`.

    1.2.2: A missing member or a disagreeing identity is refused before any member
    is promoted, and install run from `<gobby_home>/bin` skips promotion. test: `crates/gdaemon/tests/node_setup.rs::install_refuses_incoherent_or_missing_siblings`.

    1.2.3: `gdaemon` is one patch version above the executor''s starting tip in `crates/gdaemon/Cargo.toml`,
    `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.'
  labels:
  - covers:node-install-enrollment:1.2:1.2.1
  - covers:node-install-enrollment:1.2:1.2.2
  - covers:node-install-enrollment:1.2:1.2.3
  tdd: true
  source_section: '1.2'
  implementation_domain: backend
- title: '`gdaemon node enroll`'
  category: code
  task_type: feature
  depends_on:
  - '1.2'
  validation_criteria: '1.3.1: Against a self-signed TLS stub hub, enroll with the
    matching fingerprint and a piped password publishes `api_key`, `api_key_id`, `hub_cert`
    and `tls/hub.pem` at 0600 and creates `machine_id`, with no tripwire fired. test:
    `crates/gdaemon/tests/node_setup.rs::enroll_pins_hub_and_publishes_key_without_python`.

    1.3.2: Each malformed or mismatched hub reply (bad key, non-UUID ids, foreign
    machine, wrong hint, reused key id) refuses with nothing saved and names the unverified
    key id. test: `crates/gdaemon/tests/node_setup.rs::enroll_refuses_unverified_reply`.

    1.3.3: A local bootstrap, a different `--hub`, `--fingerprint` with http, and
    http to a non-loopback host without `--insecure` are refused before any request.
    test: `crates/gdaemon/tests/node_setup.rs::enroll_refuses_local_bootstrap_and_hub_mismatch`.

    1.3.4: A 401, an unreachable hub, a stalled hub past 10 s, a fingerprint mismatch
    and a certificate that does not cover the host each leave the prior enrollment
    byte for byte, and the SAN case says the password was not sent. test: `crates/gdaemon/tests/node_setup.rs::enroll_failure_keeps_prior_enrollment`.

    1.3.5: An unusable bootstrap lock or an unwritable home rolls back the pin and
    revokes the minted key through login and DELETE, and a failed revoke names the
    key id. test: `crates/gdaemon/tests/node_setup.rs::enroll_rolls_back_and_revokes`.

    1.3.6: Settle reports committed when the bootstrap names the key, and unresolved
    when the prior pin cannot be restored. test: `crates/gdaemon/src/node_setup/enroll/tests.rs::settle_decides_by_bootstrap`.

    1.3.7: `gdaemon` is one patch version above the executor''s starting tip in `crates/gdaemon/Cargo.toml`,
    `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.'
  labels:
  - covers:node-install-enrollment:1.3:1.3.1
  - covers:node-install-enrollment:1.3:1.3.2
  - covers:node-install-enrollment:1.3:1.3.3
  - covers:node-install-enrollment:1.3:1.3.4
  - covers:node-install-enrollment:1.3:1.3.5
  - covers:node-install-enrollment:1.3:1.3.6
  - covers:node-install-enrollment:1.3:1.3.7
  tdd: true
  source_section: '1.3'
  implementation_domain: backend
- title: Rust remediation names `gdaemon node enroll`
  category: code
  task_type: feature
  depends_on:
  - '1.3'
  validation_criteria: '1.4.1: `gdaemon serve` on an unenrolled node home refuses
    naming `gdaemon node enroll`, with no tripwire fired. test: `crates/gdaemon/tests/node_setup.rs::serve_without_key_names_enroll_without_python`.

    1.4.2: No file under `crates/*/src` contains `gobby auth login`. test: `crates/gdaemon/tests/node_setup.rs::no_crate_names_python_login`.

    1.4.3: The node startup and claim tests assert the new remediation. test: `crates/gdaemon/tests/nodes.rs::node_startup_requires_a_pinned_hub`.

    1.4.4: `gcode`, `gdaemon` and `ghook` are one patch version above the executor''s
    starting tip in their `Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`,
    and so is `gclient`. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

    1.4.5: The gcode prune floor equals the bumped `gcode` pin. test: `tests/code_index/test_gcode_gateway.py::test_managed_gcode_pin_matches_prune_budget_floor`.'
  labels:
  - covers:node-install-enrollment:1.4:1.4.1
  - covers:node-install-enrollment:1.4:1.4.2
  - covers:node-install-enrollment:1.4:1.4.3
  - covers:node-install-enrollment:1.4:1.4.4
  - covers:node-install-enrollment:1.4:1.4.5
  tdd: true
  source_section: '1.4'
  implementation_domain: fullstack
- title: Python login retires and pair_node enrolls with the Rust verb
  category: code
  task_type: feature
  depends_on:
  - '1.3'
  validation_criteria: '1.5.1: `pair_node` enrolls with `gdaemon node enroll` under
    a tripwire-only `PATH`, the tripwire stays silent, and the enrolled node relays
    and handshakes. test: `tests/e2e/test_hub_node_pair.py::test_node_enrolls_and_relays`.

    1.5.2: `gdaemon node enroll` pins a self-signed live hub. test: `tests/e2e/test_auth_login_e2e.py::test_enroll_pins_self_signed_hub`.

    1.5.3: `gobby auth login` is no longer a registered command, and `gobby auth key`
    still is. test: `tests/cli/test_auth_login.py::test_login_command_is_gone`.

    1.5.4: The audit lists `gdaemon node enroll` and no `gobby auth login` operation.
    test: `tests/skills/test_reference_library.py::test_reference_contract_3_2_2`.

    1.5.5: The middleware guidance names the new verb. test: `tests/servers/test_auth_middleware.py::test_protected_routes_require_auth_when_enabled`.

    1.5.6: The committed parity ledger matches the inventory with `CLI gobby auth
    login` dropped by decision #24657. test: `tests/test_parity_ledger.py::test_committed_ledger_matches_current_public_inventory`.'
  labels:
  - covers:node-install-enrollment:1.5:1.5.1
  - covers:node-install-enrollment:1.5:1.5.2
  - covers:node-install-enrollment:1.5:1.5.3
  - covers:node-install-enrollment:1.5:1.5.4
  - covers:node-install-enrollment:1.5:1.5.5
  - covers:node-install-enrollment:1.5:1.5.6
  tdd: true
  source_section: '1.5'
  implementation_domain: backend
- title: '`gdaemon node install` stages the pinned SRT'
  category: code
  task_type: feature
  depends_on:
  - '1.4'
  validation_criteria: '1.6.1: Install stages SRT from the fixture with stub `node`
    and `npm`, #24374''s verifier accepts it, and a second run reuses it, with no
    tripwire fired. test: `crates/gdaemon/tests/node_setup.rs::install_stages_pinned_srt_without_python`.

    1.6.2: A tarball off its pin, Node older than 22.12.0, or no `npm` is refused,
    leaving no staging directory and no install. test: `crates/gdaemon/tests/node_setup.rs::install_refuses_srt_off_pin`.

    1.6.3: The Rust pins, receipt fields and patch table equal the Python ones (read
    with `include_str!`), the lockfile and runner copies equal their Python originals
    byte for byte, and the embedded lockfile, runner and fixture match their pins.
    test: `crates/gdaemon/tests/node_setup.rs::srt_pins_match_python`.

    1.6.4: Patches apply once, skip a patched file, and refuse unknown bytes; a failed
    promotion restores the previous install. test: `crates/gdaemon/src/node_setup/srt/tests.rs::patches_and_promotion_follow_python`.

    1.6.5: `gdaemon` is one patch version above the executor''s starting tip in `crates/gdaemon/Cargo.toml`,
    `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.'
  labels:
  - covers:node-install-enrollment:1.6:1.6.1
  - covers:node-install-enrollment:1.6:1.6.2
  - covers:node-install-enrollment:1.6:1.6.3
  - covers:node-install-enrollment:1.6:1.6.4
  - covers:node-install-enrollment:1.6:1.6.5
  tdd: true
  source_section: '1.6'
  implementation_domain: backend
- title: '`gdaemon node install` installs claude hooks and the MCP entry'
  category: code
  task_type: feature
  depends_on:
  - '1.6'
  validation_criteria: '1.7.1: Install over the golden''s input writes the golden''s
    claude hooks and `gmcp` entry, keeps the user''s handlers and other keys, and
    backs up `settings.json`, with no tripwire fired. test: `crates/gdaemon/tests/node_setup.rs::install_claude_hooks_match_python_golden`.

    1.7.2: Python''s installer produces the claude golden. test: `tests/install/test_node_setup_golden.py::test_claude_golden_matches_python`.

    1.7.3: A CLI absent from `PATH` is skipped with its config untouched, and a path
    with a space is single-quoted as Python quotes it. test: `crates/gdaemon/tests/node_setup.rs::install_skips_absent_cli_and_quotes_like_python`.

    1.7.4: `gdaemon` is one patch version above the executor''s starting tip in `crates/gdaemon/Cargo.toml`,
    `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

    1.7.5: The crate-local claude template copy equals `src/gobby/install/claude/hooks-template.json`
    byte for byte. test: `crates/gdaemon/tests/node_setup.rs::claude_template_copy_matches_python`.'
  labels:
  - covers:node-install-enrollment:1.7:1.7.1
  - covers:node-install-enrollment:1.7:1.7.2
  - covers:node-install-enrollment:1.7:1.7.3
  - covers:node-install-enrollment:1.7:1.7.4
  - covers:node-install-enrollment:1.7:1.7.5
  tdd: true
  source_section: '1.7'
  implementation_domain: backend
- title: '`gdaemon node install` installs codex hooks, trust and the MCP entry'
  category: code
  task_type: feature
  depends_on:
  - '1.7'
  validation_criteria: '1.8.1: Install over the golden''s input writes the golden''s
    codex hooks, trust-entry keys and `gmcp` block, each `trusted_hash` is the Rust
    hash of the hook it names, and a user comment in `config.toml` survives, with
    no tripwire fired. test: `crates/gdaemon/tests/node_setup.rs::install_codex_hooks_match_python_golden`.

    1.8.2: Python''s installer produces the codex golden. test: `tests/install/test_node_setup_golden.py::test_codex_golden_matches_python`.

    1.8.3: `gdaemon` is one patch version above the executor''s starting tip in `crates/gdaemon/Cargo.toml`,
    `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

    1.8.4: The Rust trust hash equals each vector''s Python hash. test: `crates/gdaemon/src/node_setup/codex/tests.rs::trust_hash_matches_python_vectors`.

    1.8.5: Python''s hash produces the trust vectors. test: `tests/install/test_node_setup_golden.py::test_codex_trust_vectors_match_python`.

    1.8.6: The crate-local codex template copy equals `src/gobby/install/codex/hooks-template.json`
    byte for byte. test: `crates/gdaemon/tests/node_setup.rs::codex_template_copy_matches_python`.'
  labels:
  - covers:node-install-enrollment:1.8:1.8.1
  - covers:node-install-enrollment:1.8:1.8.2
  - covers:node-install-enrollment:1.8:1.8.3
  - covers:node-install-enrollment:1.8:1.8.4
  - covers:node-install-enrollment:1.8:1.8.5
  - covers:node-install-enrollment:1.8:1.8.6
  tdd: true
  source_section: '1.8'
  implementation_domain: backend
- title: '`gdaemon node install` installs droid hooks and the MCP entry'
  category: code
  task_type: feature
  depends_on:
  - '1.8'
  validation_criteria: '1.9.1: Install over the golden''s input writes the golden''s
    droid hooks and `gmcp` entry, with no tripwire fired. test: `crates/gdaemon/tests/node_setup.rs::install_droid_hooks_match_python_golden`.

    1.9.2: Python''s installer produces the droid golden. test: `tests/install/test_node_setup_golden.py::test_droid_golden_matches_python`.

    1.9.3: Install then enroll in one sandbox leaves a node home that `NodeServices::prepare`
    accepts, with no tripwire fired. test: `crates/gdaemon/tests/node_setup.rs::documented_node_sequence_runs_without_python`.

    1.9.4: `gdaemon` is one patch version above the executor''s starting tip in `crates/gdaemon/Cargo.toml`,
    `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.'
  labels:
  - covers:node-install-enrollment:1.9:1.9.1
  - covers:node-install-enrollment:1.9:1.9.2
  - covers:node-install-enrollment:1.9:1.9.3
  - covers:node-install-enrollment:1.9:1.9.4
  tdd: true
  source_section: '1.9'
  implementation_domain: backend
- title: Node setup docs
  category: docs
  task_type: feature
  depends_on:
  - '1.5'
  - '1.9'
  validation_criteria: '1.10.1: Client setup documents the five commands, the node
    requirements, manual `gdaemon serve`, #21571 D1 and the #24151 note, and no Python
    step. file: `docs/guides/shared-stack.md`.

    1.10.2: The CLI reference documents `gdaemon node install` and `gdaemon node enroll`
    and has no `gobby auth login` entry. file: `docs/guides/cli-commands.md`.

    1.10.3: The documented sequence is the one the sandbox runs without Python. test:
    `crates/gdaemon/tests/node_setup.rs::documented_node_sequence_runs_without_python`.

    1.10.4: No Markdown page under `docs/` outside `docs/audits/`, `docs/research/`
    and the generated parity ledger names `gobby auth login`. test: `tests/docs/test_node_setup_docs.py::test_no_page_names_python_login`.'
  labels:
  - covers:node-install-enrollment:1.10:1.10.1
  - covers:node-install-enrollment:1.10:1.10.2
  - covers:node-install-enrollment:1.10:1.10.3
  - covers:node-install-enrollment:1.10:1.10.4
  tdd: false
  source_section: '1.10'
  assigned_agent: tech-writer
```
