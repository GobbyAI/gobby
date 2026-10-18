Plan artifact: `.gobby/plans/node-install-enrollment.md`

# Gobby 1.0 Stage 2: Rust-only node install, enrollment and updates

**Plan ID:** node-install-enrollment

## Overview
`kind: framing`

Task #24657 (Rust-only node install and enrollment) is the plan root. Josh's 1.0
objective (2026-10-17) is a node that installs and runs with no Python and no Docker.
His setup (2026-10-18) is one hub and nodes on his laptops and Mac mini. He develops
Gobby on the hub and does other work on the nodes, and most users never develop
Gobby at all. A node therefore has no Gobby checkout and no Rust, Zig or Python
toolchain. It installs and updates only from published, signed releases, and its hub
decides which release it runs.

Today four node steps still need Python:

1. Enrollment is Python `gobby auth login` (`src/gobby/cli/auth_login.py::login`).
2. The only installer is Python `gobby install`, whose remote path demands the hub's
   `database_url` (`src/gobby/cli/_install_daemon.py::_run_install_preflight`).
3. The pinned Sandbox Runtime (SRT) is staged by Python
   (`src/gobby/cli/install_setup_srt.py::_install_srt_runtime`).
4. CLI hooks are installed by Python (`src/gobby/cli/installers/claude.py::install_claude`,
   `codex.py::install_codex`, `droid.py::install_droid`, `grok.py::install_grok`,
   `agy.py::install_agy`).

When this plan closes, three commands turn a fresh laptop into a node:

```bash
v=0.4.80  # example: any published gdaemon release; install moves the node to the hub's target
curl -fsSL "https://github.com/GobbyAI/gobby/releases/download/gdaemon-v$v/gobby-node-$(uname -s)-$(uname -m).tar.gz" | tar -xz
./gobby-node/gobby node install --hub https://hub.example:60887 --email me@example.com
```

- `gobby` is the bundle's client (gclient). It hands `node install` and
  `node enroll` to the bundle's `gdaemon` before it looks for a daemon.
- Install checks the bundle's signed manifest and enrolls the machine at the hub,
  asking for the password and for trust in the hub certificate's fingerprint. It
  promotes the six binaries into `~/.gobby/bin`, stages the pinned SRT, and
  installs Gobby hooks and the `gmcp` MCP entry for each of claude, codex, droid,
  grok and agy found on `PATH`. It then starts a launchd agent (macOS) or a systemd
  user unit (Linux) and waits until the hub lists the node as connected.
- From then on the hub drives updates. Every connect's acknowledgement names the
  release the hub wants. The node stages it and switches once it runs no agents.
  When the hub upgrades, every node follows.
- In any gclient window on any enrolled machine, hub or node, `gobby node list`
  shows every machine the operator owns with its release, target and connection.
  `gobby node update --machine <ref>` or `--all` makes the hub drive an update now.

P1 publishes signed releases, gmcp's included, and per-platform node bundles. P2
adds `gobby node` and enrollment. P3 installs a node from a bundle. P4 makes the hub
drive updates and settles version skew in both directions. P5 documents it. E1's
publication gate precedes its live gates. Each leaf names the Python surface it
replaces, and node-path tests run with a `PATH` that holds no Python interpreter
(#24657 validation criterion 3).

## Constraints
`kind: framing`

- **No backward compatibility.** 0.5.0 has not shipped.
- **Unix nodes.** `gdaemon node` and `crates/gdaemon/src/node_setup/` are
  `#[cfg(unix)]`, like `state` and `gdaemon serve`: they use `flock`, `termios`,
  `codesign`, launchd and systemd. Bundles exist for `Darwin-arm64`,
  `Darwin-x86_64`, `Linux-x86_64` and `Linux-aarch64`. `crates/gdaemon/src/release.rs`
  and `crates/gdaemon/src/nodes/` stay portable, so the Windows gdaemon archives
  still build.
- **No-toolchain harness.** `crates/gdaemon/tests/node_setup_support/mod.rs` (2.1)
  runs the built `gdaemon` (`env!("CARGO_BIN_EXE_gdaemon")`, copied into a
  temporary bundle directory) with `env_clear()`, `HOME` and `GOBBY_HOME` in a
  temporary directory, and `PATH` set to one stub directory. That directory holds
  tripwire scripts named `python`, `python3`, `uv`, `cargo`, `rustc` and `zig`; each
  writes `<sandbox>/toolchain-invoked` and exits 127. It also links the system
  `tar`, which bundle extraction runs. Every no-Python test asserts the marker is
  absent. Later leaves add their stubs (sibling binaries, `node`, `npm`,
  `launchctl`, `systemctl`, `loginctl` and the five CLIs) to the same sandbox, so
  every earlier verb-level test keeps running the whole verb. From 3.2 on, the
  harness signs the bundle's `node-release.json` with a test key (D21). From 3.3 on,
  the sandbox also holds a seeded verified SRT install (D19), which the verb's SRT
  step reuses. 3.3.3 and 3.3.6 are exempt; they run on an empty SRT home.
- **Release ownership.** Each leaf bumps by 0.0.1, from the executor's starting tip,
  every shipped binary whose crate or recursive path dependencies it changes. It
  updates `src/gobby/install/version_pins.py::MANAGED_BIN_VERSION_PINS` and
  `Cargo.lock` with the bump
  (`tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`).
  - A change confined to `crates/gdaemon` bumps `gdaemon`; one confined to
    `crates/gclient` bumps `gclient`.
  - `gmcp` ships from 1.2 on. A leaf that changes `crates/gmcp` or its path
    dependencies bumps it in `crates/gmcp/Cargo.toml` and `Cargo.lock`; `gmcp` has
    no managed pin.
  - 2.4 changes `crates/gcore`, so it bumps `gcode`, `gdaemon`, `ghook` and `gmcp`,
    and its `crates/gclient` edit bumps `gclient`. `gterm` does not depend on
    `gobby-core`, and no leaf changes it, so bundles carry `gterm` 0.1.12. That
    version has never been tagged or published (#15977, 15:13 CDT), so its first
    push, at E1's gate 3, publishes it signed. `gclient` has no published release
    either (`UNPUBLISHED_MANAGED_BINS` in `version_pins.py`); its first is the
    version its last bumping leaf leaves.
  - Python-only leaves, `.github/` and `scripts/` changes, and paths with a `tests`
    component bump nothing
    (`src/gobby/install/bin_release_identity.py::workspace_source_identity`).
  - Every bumping leaf edits `Cargo.lock` and `version_pins.py`, so the bumping
    leaves form one chain: 2.1, 2.2, 2.3, 2.4, 3.1 to 3.9, then 4.1 to 4.5.
  - The `gcode` maintenance floor `MIN_GCODE_PRUNE_BUDGET_VERSION` is a literal in
    `src/gobby/code_index/gcode_gateway.py` (969 lines), and
    `tests/code_index/test_gcode_gateway.py` requires it to equal the `gcode` pin.
    #24719 (Derive the gcode prune-budget floor from the managed gcode pin) makes it
    read `MANAGED_BIN_VERSION_PINS["gcode"]` in place, as `MIN_GCODE_GRAPH_VERSION`
    already does. 2.4 depends on it and edits neither the floor nor its module. If
    code-index-family 1.1 (#24670) lands first, its location applies (D24).
- **Concurrent editors and seams.**
  - #24717 (Release signing key for node binary updates) is Josh's manual task. It
    ends with the public key committed at `crates/gdaemon/assets/release-signing.pub`.
    3.1 depends on it, and E1's publication gate waits for it.
  - #24374 (SRT install verification and the launch chokepoint, node-halves 2.5)
    creates `crates/gdaemon/src/nodes/agents/sandbox/runtime.rs` with `SRT_VERSION`,
    the content-manifest builder and verifier, and the install lock. 3.3 depends on
    it.
  - #24372 (Worktree and clone preparation, repair and removal, node-halves 2.3)
    creates `crates/gdaemon/src/nodes/agents/repair.rs` with the droid template
    renderer, the `merge_gobby_hook_groups` port and the `~/.claude.json.lock` mkdir
    lock. 3.4 depends on it.
  - #24355 (Every launch site starts `~/.gobby/bin/gmcp`, gmcp-stdio 1.6) sets the
    MCP entry to `<gobby_home>/bin/gmcp` with no arguments in every Python writer,
    `src/gobby/agents/grok_sandbox_home.py::_declare_gobby_mcp_server` included. 3.4
    depends on it, so the hook goldens record the `gmcp` entry.
  - #24604 (Live hub-to-node proof) runs on `tests/e2e/test_hub_node_pair.py`'s
    `pair_node`. 2.5 depends on it, so the fixture does not change under it. #24604
    does not wait for publication (#15977, 13:31 CDT).
  - hub-mode 1.2 (gdaemon internal node-command route over the node channel) sends
    hub frames to nodes. If `crates/gdaemon/src/nodes/registry.rs` carries a
    per-connection outbound sender at 4.4's starting tip, 4.4's frames use it;
    otherwise 4.4 adds one.
  - #21573 (Naming switch) renames gclient to gobby across the product. This plan
    names only the bundle's copy `gobby` (D11).
- **Dependencies.** No new external crate enters `Cargo.lock`. gdaemon adds
  `serde_yaml = { package = "yaml_serde", version = "0.10.4" }` (as gcore names it),
  `toml_edit = { version = "0.25", default-features = false, features = ["parse", "display"] }`,
  `ureq = { version = "2", features = ["json"] }` (as gcore declares it; 2.12.1 is
  locked) and `ring = "0.17"` (0.17.14 is locked through rustls). `display` pulls
  `toml_writer`, also locked. Bundle extraction runs the system `tar`. The release
  scripts are bash and run on CI runners, never on a node. They use `jq`, `openssl`
  3 and `sha256sum`, which the Ubuntu runner provides, and
  `crates/gdaemon/tests/release_scripts.rs` tests them with the same tools.
- **No new Python.** Josh's fleet rule (14:41 CDT) and #15977's rulings (15:11 and
  16:16 CDT) allow no new Python file. The release scripts and their tests are bash
  and Rust (1.1, 1.3), and the docs guard is a Rust test (5.1). The one new Python
  file is `tests/install/test_node_setup_golden.py`, a test that records the goldens
  the Rust installer matches (3.4 to 3.8) from the existing Python installers, so it
  pins current behavior (D24).
- **Heavy work.** Every cargo run sets `CARGO_BUILD_JOBS=3` and scopes with `-p`.
- **Monolith ceiling.** Every targeted production file stays under 1,000 lines,
  counting inline `#[cfg(test)]` modules. Unit tests go in `<module>/tests.rs`.

## Decision Record
`kind: framing`

Orchestrator #15977 ordered this rework at 13:03 CDT on 2026-10-18 (Josh: "The whole
plan needs to be reworked.") and added Josh's requirements at 13:04, 13:05 and 13:11.
It ruled on #24717, #24604, #21571 and the hub-mode edits at 13:31, and at 15:11
affirmed D4, D8, D15 and D22 and ruled out new Python files. At 16:16 it kept the
golden test and dropped `gcode_versions.py` (D24). The Plan Writer records D1 to
D24; the pushbacks Josh asked for are marked.

- **D1, release identity.** A node release is a gdaemon release. Its version is the
  gdaemon crate version and its tag is `gdaemon-v<version>`. The gdaemon release
  carries the node bundles, assembled from the member releases at the versions the
  tagged commit's manifests name; nothing is rebuilt. Rejected: a separate node
  version, which would be a second identity to keep in step with gdaemon's, and
  building the members inside the gdaemon workflow, which duplicates five workflows.
- **D2, signing.** Every binary release publishes `SHA256SUMS` and
  `SHA256SUMS.sig`, an Ed25519 signature made by OpenSSL 3
  (`openssl pkeyutl -sign -rawin`) with the secret `GOBBY_RELEASE_SIGNING_KEY` from
  the protected GitHub environment `release`. #24717 has Josh generate the key,
  store the private half and hand over only the public key. gdaemon embeds that key
  and verifies with `ring`. Rejected: `.sha256` sidecars alone, because whoever can
  replace an asset can replace its sidecar; and minisign or Sigstore, which add
  tooling for one check.
- **D3, first command and trust root.** The first download is trusted through HTTPS
  from GitHub. The bundle's signed `node-release.json` then proves its members are
  intact, which guards against corruption, not against a hostile first download.
  The command names a version because GitHub's "latest" release is the Python `v*`
  release (`.github/workflows/release.yml`), and every binary workflow publishes
  with `--latest=false`. Any published release works, because install moves the
  node to the hub's target, and install's wait covers that move (3.9). Rejected: an
  install script piped to `sh`, which is a
  second artifact to publish and audit; and marking gdaemon releases latest, which
  takes "latest" from the Python product.
- **D4, command routing and authority.** `gobby`, the bundle's gclient, handles
  `node install` and `node enroll` before it resolves a daemon URL or token, and
  execs `gdaemon node <verb>` from its own directory, because those verbs create the
  credentials every other verb needs. `node list` and `node update` are ordinary
  client verbs. They call the local daemon with the local key, a node's front door
  relays them to its hub (`crates/gdaemon/src/front_door/mod.rs::FrontDoor::handle`
  forwards every node request except hook ingress), and the hub answers only for
  machines the caller owns. That gives Josh every machine from any gclient window.
  **Pushback:** install and enroll run only on the machine being set up, because
  they write that machine's credentials and service. The hub upgrades through
  `gobby cutover` on the hub, because it is the machine that builds from source.
  Agent and terminal operations on other machines route through hub-mode's node
  commands and are outside this plan. #15977 affirmed this at 15:11 CDT.
- **D5, update target.** Nodes never ask GitHub what to run. The hub resolves the
  target: the highest published `gdaemon-v` release no newer than the hub's own
  gdaemon whose `node-release.json` contract equals the hub's (D6). It caches the
  answer for 10 minutes and keeps the last good answer when GitHub is unreachable.
  The target reaches a node in the acknowledgement of every connect, which is the
  start-time check Josh asked for; in refusals; and in `release` frames when the
  target changes or the operator runs `gobby node update`. Because the target never
  exceeds the hub, a start-time check never moves a node past what its hub speaks. A
  node newer than the target moves down to it.
- **D6, version skew.** Hub and node allow no contract skew. The hello carries the
  node's `contract`, its `node_commands::CONTRACT_VERSION`, and the hub admits the
  node only when it equals the hub's. Otherwise the hub sends a `refuse` frame and
  closes with code 4426:
  - `node_too_old` when the node's contract is below the hub's, or when the hello
    carries none (a node from before this plan);
  - `hub_too_old` when the node's contract is above the hub's.

  The frame carries both contracts, the target and a message naming both gdaemon
  versions and what happens next. Refuse-and-converge is the policy both ways: the
  node moves to the target (D7), and `gobby node list` shows the refusal. Only the
  hub checks, and hubs upgrade first. A node from before this plan cannot update
  itself, so its refusal message tells the operator to re-run the install command on
  it. Release frames belong to the channel layer, like hello and ack, and sit outside
  the command contract's `v`, so the Python models in `src/gobby/hub/node_commands.py`
  do not change.

  Because a target is always a published release (D5), every `CONTRACT_VERSION`
  change is a release event. The gdaemon release carrying the new contract is
  published before the hub cuts over to it, and the release guide and node guide
  order the steps that way (5.1). A hub that cuts over first resolves no target, and
  each refusal says no published gdaemon release speaks its contract yet; nodes
  converge once one is published.
- **D7, activation and restart.** The node stages the target beside its binaries and
  reports it. It activates the staged target once its own agent state holds no run
  (`AgentState::live_and_accepted() == 0`), checking when staging completes, at each
  channel ping, and at each refusal. It promotes the staged set to new inodes with the identity stamp, writes
  `.node-release.json`, and `gdaemon serve` exits with status 75. launchd
  (`KeepAlive` with `SuccessfulExit` false) or systemd (`Restart=on-failure`) then
  starts the new binaries. A refused node, which has no channel, activates the same
  way from the refusal's target. The target is the only word the node needs from the
  hub, so no `activate` frame exists. The hub's `agent_runs` rows do not gate
  activation: after a hub cutover they can list runs that ended with the old hub
  until the node reports again (hub-mode Story 3), and only the node knows what it
  runs. Interactive provider sessions are not agent runs and do not gate activation.
  A failed promotion keeps the staged set, and the next check retries it.
- **D8, no automatic rollback.** **Pushback:** the staged set is verified and its
  identity probed before the swap, which catches a corrupt or incoherent set. A
  release that verifies but misbehaves is fixed by withdrawing it on GitHub, after
  which the hub targets the previous release and nodes move down to it, or by
  re-running the install command. Keeping and health-checking a previous set would
  add a second lifecycle for a case the release process already handles. #15977
  affirmed this at 15:11 CDT.
- **D9, service.** `gobby node install` owns the node's service:
  `~/Library/LaunchAgents/com.gobby.node.plist` on macOS and the systemd user unit
  `gobby-node.service` on Linux. Both run `<gobby_home>/bin/gdaemon serve` with the
  install-time `PATH`, so spawned CLIs, `node` and `npm` resolve as in the
  operator's shell. The Python daemon templates are not reused, because
  `src/gobby/install/shared/services/gobby-daemon.service.j2` sets
  `ProtectSystem=strict`, which would stop a node from editing the operator's
  projects. #21571 (Client operator verbs) D1 governs the local daemon's service
  and needs no text change (#15977, 13:31 CDT). Linux user units stop at logout
  unless lingering is on; install prints `loginctl enable-linger <user>` when it is
  off and does not change it.
- **D10, release state stays in memory.** The hub keeps each live node's staged
  release and each refused node's last refusal in its registry, and a node reports
  again on reconnect. Rejected: `machines` columns, which would change gdaemon's
  schema identity for state that goes stale when a channel closes.
- **D11, PATH and naming.** Install prints `export PATH="<gobby_home>/bin:$PATH"`
  when that directory is not on `PATH` and edits no shell profile, because profiles
  belong to the operator and differ per shell. Until #21573 renames gclient, a
  machine Python installed runs the same verbs as `gclient node ...`.
- **D12, Python login.** Python `gobby auth login` is deleted (2.5).
  `gobby auth key` stays. `tests/e2e/test_hub_node_pair.py::pair_node` enrolls with
  `gdaemon node enroll` and is the no-Python enrollment proof.
- **D13, hooks on all five CLIs.** The node installs claude, codex, droid, grok and
  agy hooks, the `gmcp` MCP entry, codex hook trust, and grok's Claude-hook
  compatibility switch. It installs no shared content, router commands,
  statusline, `autoMemoryEnabled` or `validate_settings.py`. Spawned runs on a node
  read the node's global hook config, because the Python installer cleans
  project-level hooks. Interactive node sessions register at the hub only after
  #24151 (S2.11 hub half); P3 does not wait on it. grok and agy now send hooks from
  a node, which retires hub-mode Decision 7's hookless path (D23).
- **D14, promotion records.** The node writes the identity stamp
  `.gdaemon-schema-identity.json` and `.node-release.json`, the release's signed
  manifest, which names every member's version and hash. It skips
  `.<name>-release-identity.json`, the version stamp and the install sidecar: only
  Python reads them (`src/gobby/install/bin_release_identity.py`,
  `src/gobby/install/bin_freshness*.py`), and no crate does.
- **D15, SRT tarball and Node.js.**
  `npm pack @anthropic-ai/sandbox-runtime@0.0.79 --pack-destination <staging> --ignore-scripts`
  fetches the tarball, and the stager checks it against the pinned SHA-256. The
  lockfile and runner are embedded from their D20 copies with `include_bytes!` and
  checked against their pins. **Pushback:** Node.js 22.12.0 or later and npm stay
  node requirements, because SRT is an npm package; bundling Node.js would mean
  publishing and updating a JavaScript runtime for every target. #15977 affirmed
  this at 15:11 CDT.
- **D16, password and trust prompts.** On a TTY, enroll prompts `Password: ` with
  echo off. Otherwise it reads one line from stdin, which the e2e fixture pipes.
  Without `--fingerprint`, enroll shows the fingerprint and asks `Trust this
  certificate? [y/N]` on a TTY; with stdin not a TTY it refuses and asks for
  `--fingerprint`. `--email` is required whenever enrollment runs.
- **D17, home trust.** Node install does not call `seed_gobby_home_trust`, for agy
  or any CLI. Each node spawn seeds its workspace's trust at launch (node-halves
  2.6), and an operator's own CLI asks on first use.
- **D18, Python remote install.** Python `gobby install`'s remote path stays as it
  is, except that its remediation strings name `gobby node enroll` (2.5). No node
  path reaches it, and retiring it belongs with #21591 (Install and setup route
  family) and the parked #20151 (installer wizard). 5.1 removes it from the node
  setup docs.
- **D19, SRT test tarball.** No registry tarball is committed (#15977, 12:30 CDT).
  Tests compose a minimal tarball in a temporary directory at test time, from the
  four patch targets' exact 0.0.79 bytes in `tests/fixtures/srt/` and the
  `package.json` fields the installer reads. The SRT step is two library functions:
  `fetch_tarball` (D15's `npm pack` and the pin check) and `install_from_tarball`,
  which takes a tarball its caller has checked and writes the pinned receipt. The
  verb chains them with the pinned consts.
  - The default suite installs from the composed tarball through
    `install_from_tarball`, and seeds a verified install that way for every
    verb-level test from 3.3 on. The verb itself refuses the composed tarball as
    off-pin.
  - One operator-opt-in test (`#[ignore]`, network) runs the verb's real
    `npm pack` fetch.
  - Rejected: committing the 3,975,831-byte registry tarball (repository size).
- **D20, embedded Python-tree files.** gdaemon embeds crate-local copies, under
  `crates/gdaemon/assets/node_setup/`, of the SRT lockfile, `srt_runner.mjs`, and the
  claude, codex, grok and agy hook templates. `workspace_source_identity`
  (`src/gobby/install/bin_release_identity.py`) hashes only the crate closure and the
  root Cargo and toolchain files. Embedding a `src/gobby` file would change
  gdaemon's bytes with no bump, which the same-version promotion guard cannot see.
  A test under `crates/gdaemon/tests` compares each copy with its original byte for
  byte, so a Python-side edit fails that test until the copy changes, and changing
  the copy bumps gdaemon.
- **D21, debug-only test overrides.** `release.rs` and the node verbs read
  `GOBBY_RELEASE_BASE_URL`, `GOBBY_RELEASE_API_URL`, `GOBBY_RELEASE_TEST_KEY` and
  `GOBBY_NODE_CONNECT_TIMEOUT_SECS` and `GOBBY_NODE_CONVERGE_TIMEOUT_SECS` only
  under `cfg(debug_assertions)`. Tests run
  debug builds against a local fake release server and a test key. Release builds
  ignore the variables, so a published binary trusts only the embedded key.
- **D22, publication is a gate.** Publishing pushes tags, and only Josh authorizes a
  push, so publication is E1's gate 3 rather than a leaf. It follows the last
  bumping leaf and #24717. Every member's release at its manifest version is a
  first publication: 2.4 bumps `gcode`, `ghook`, `gdaemon` and `gmcp`, the gclient
  leaves bump `gclient`, and `gterm-v0.1.12` has never been tagged (Release
  ownership). Each therefore carries signed checksums. #15977 affirmed this at
  15:11 CDT.
- **D23, hub-mode edits.** A separate commit edits `.gobby/plans/hub-mode.md` under
  #15977's 13:31 authorization so grok and agy settle through hooks, while pane
  attention stays for all five: Decision 7 lists their attention hooks, and the
  Constraints and Cross-plan edges lines that cite it follow. 2.6 drops the
  `NODE_HOOKLESS_PROVIDERS` paragraph and holds every `launched: true` exit for a
  second report (2.6.3). A grok run settles through its forwarded session end. An
  agy run, which has no session-end hook, settles through `_dead_terminal_reason`
  from the turn its forwarded `Stop` completed, as it does locally (#23778), which
  now passes a node run's forwarded turns as `attested_turns`; 2.6.8 is rewritten
  to prove it. 3.3, 5.1 (research, fake provider and 5.1.2) follow, and a new V1
  entry records the change. hub-mode's M1 entries for 2.6, 3.3 and 5.1 change, so
  Plan Adversary #16993 re-stamps hub-mode too. #15977 updates the text of #24276 (hub-mode 2.6)
  and #24372. node-halves 2.3.3 needs no change, because grok and agy hooks are
  global.
- **D24, no new Python.** Josh's fleet rule (14:41 CDT) allows no new Python file,
  and #15977 ruled on this plan's two at 16:16 CDT.
  `tests/install/test_node_setup_golden.py` stays: it records goldens from the
  existing Python installers, so it pins current behavior.
  `src/gobby/code_index/gcode_versions.py` is dropped, conditional or not. Moving
  existing constants into a new Python module is new Python that the Rust port
  replaces, and neither hub-node nor current-code stability needs it. The gcode
  floors stay where they are. #24719 (Derive the gcode prune-budget floor from the
  managed gcode pin, 16:19 CDT) derives the prune floor from the pin in place, so
  2.4's `gcode` bump edits no 969-line `gcode_gateway.py`, which the size-growth
  rule would refuse without a new module. If code-index-family 1.1 (#24670) lands
  first, its location applies.

## P1: Signed release publishing
`kind: framing`

Every binary release gains a signed checksum list, gmcp gains a release workflow,
and each gdaemon release carries a node bundle per Unix target. No leaf here changes
a crate, so none bumps a version.

### 1.1 Every binary release publishes signed checksums [category: config]
`kind: deliverable`

Targets:
- `scripts/release/sign_release.sh`
- `.github/workflows/release-gcode.yml`
- `.github/workflows/release-ghook.yml`
- `.github/workflows/release-gdaemon.yml`
- `.github/workflows/release-gclient.yml`
- `.github/workflows/release-gterminal.yml`
- `crates/gcode/tests/release_workflows.rs::*` — scope-reason: a new test asserts the signing step in every binary release workflow
- `crates/gdaemon/tests/release_scripts.rs`

Python surface replaced: none. This is release tooling (D2), and its tests are Rust
(No new Python).

- `sign_release.sh` (bash, `set -euo pipefail`) writes `GOBBY_RELEASE_SIGNING_KEY`
  (a PEM Ed25519 private key) to a 0600 temporary file that a trap removes. It
  refuses an empty key and an OpenSSL older than 3.0 (`openssl version`), writing
  nothing.
  - `sums <file>...` writes `SHA256SUMS` in the current directory, one
    `sha256sum` line per named file, sorted by name, then
    `openssl pkeyutl -sign -inkey <key> -rawin -in SHA256SUMS -out SHA256SUMS.sig`.
  - `file <path>` writes `<path>.sig` the same way.
- Each workflow's `release` job gains `environment: release`, checks out the tag
  with the pinned `actions/checkout` the other jobs use, and runs a `Sign checksums`
  step (`shell: bash`) after `Generate SHA-256 checksums`:
  `scripts/release/sign_release.sh sums` over the same globs as its `assets` array,
  with `GOBBY_RELEASE_SIGNING_KEY: ${{ secrets.GOBBY_RELEASE_SIGNING_KEY }}`. The
  `assets` array gains `SHA256SUMS SHA256SUMS.sig`. The `.sha256` sidecars stay,
  because `install_release.py` reads them.
- `release_workflows_sign_checksums` checks all five workflows: the step sits between
  checksum generation and `gh release create`, runs the script's `sums` mode, maps
  the secret, the job names `environment: release`, and the assets list both files.
- `release_scripts.rs` runs `scripts/release/sign_release.sh` by its path from
  `CARGO_MANIFEST_DIR` in a temporary directory, with a key from
  `openssl genpkey -algorithm ed25519`, and verifies each signature with
  `openssl pkeyutl -verify -pubin -rawin`. Each test returns early with a message
  when `openssl` 3 or `sha256sum` is missing; CI's Ubuntu runner has both. A path
  with a `tests` component bumps nothing (Release ownership).

Research context:
- Release jobs end with
  `gh release create "$tag" "${assets[@]}" --repo "$GITHUB_REPOSITORY" --generate-notes --verify-tag --latest=false`
  and do not check out the repository today. Tags are `gcode-v*`, `ghook-v*`,
  `gdaemon-v*`, `gclient-v*` and `gterm-v*`; archives are `<bin>-<target>.tar.gz`
  (and `.zip` on Windows) holding the bare binary.
- `crates/gcode/tests/release_workflows.rs::release_workflows_generate_and_upload_archive_checksums`
  already checks the checksum step of `release-gcode.yml` and `release-ghook.yml`.
- Planned checks: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-code -E 'binary(release_workflows)'`;
  `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-daemon -E 'binary(release_scripts)'`.

**Acceptance:**

- 1.1.1 - `sign_release.sh sums` writes a `SHA256SUMS` listing each named file's hash and a `SHA256SUMS.sig` that the matching Ed25519 public key verifies. test: `crates/gdaemon/tests/release_scripts.rs::sums_signature_verifies`.
- 1.1.2 - `sign_release.sh file` writes a detached signature the public key verifies, and either mode refuses an empty key with nothing written. test: `crates/gdaemon/tests/release_scripts.rs::file_mode_and_missing_key`.
- 1.1.3 - Each of the five binary release workflows signs its checksums in the `release` environment before `gh release create` and uploads `SHA256SUMS` and `SHA256SUMS.sig`. test: `crates/gcode/tests/release_workflows.rs::release_workflows_sign_checksums`.

### 1.2 gmcp release workflow [category: config] (depends: 1.1)
`kind: deliverable`

Targets:
- `.github/workflows/release-gmcp.yml`
- `crates/gcode/tests/release_workflows.rs::*` — scope-reason: a new test checks `release-gmcp.yml`, and the signing test's list gains it

Python surface replaced: none. gmcp had no release (Josh, 13:11 CDT: "Including gmcp").

- `release-gmcp.yml` follows `release-gdaemon.yml` for package `gobby-mcp` and
  binary `gmcp`: it runs on `gmcp-v*` tags, refuses a tag that differs from
  `crates/gmcp/Cargo.toml`'s version, tests the crate, builds gdaemon's six targets,
  archives `gmcp-<target>.tar.gz` (Unix) and `gmcp-<target>.zip` (Windows) with
  `.sha256` sidecars, publishes `gobby-mcp` to crates.io, signs its checksums (1.1)
  and runs `gh release create ... --latest=false`.
- `release_gmcp_builds_and_uploads_six_targets` checks the tag trigger, the version
  check, the six targets, the archive names and the release command.

Research context:
- `crates/gmcp/Cargo.toml` is `gobby-mcp` 0.1.0 with publishable metadata and a
  README, and depends on `gobby-core` by path and version 0.10.0, which
  `release-gcore.yml` publishes.
- gdaemon builds `aarch64-apple-darwin`, `x86_64-apple-darwin`,
  `x86_64-unknown-linux-gnu`, `aarch64-unknown-linux-gnu`, `x86_64-pc-windows-msvc`
  and `aarch64-pc-windows-msvc`.
- Planned checks: as 1.1.

**Acceptance:**

- 1.2.1 - `release-gmcp.yml` runs on `gmcp-v*` tags, refuses a tag that differs from the crate version, builds the six gdaemon targets, and uploads the `gmcp-<target>` archives with their `.sha256` sidecars. test: `crates/gcode/tests/release_workflows.rs::release_gmcp_builds_and_uploads_six_targets`.
- 1.2.2 - `release-gmcp.yml` signs its checksums like the other binary workflows. test: `crates/gcode/tests/release_workflows.rs::release_workflows_sign_checksums`.

### 1.3 gdaemon releases carry node bundles [category: config] (depends: 1.2, 2.1)
`kind: deliverable`

Targets:
- `scripts/release/node_bundle.sh`
- `.github/workflows/release-gdaemon.yml`
- `crates/gdaemon/tests/release_workflow.rs::*` — scope-reason: a new test asserts the bundle step's place and inputs
- `crates/gdaemon/tests/release_scripts.rs`

Python surface replaced: none (D1). The script is bash and its tests are Rust (No
new Python).

- The gdaemon `release` job runs `scripts/release/node_bundle.sh --tag "$tag"`
  (`shell: bash`) with `GH_TOKEN` after checksum generation and before signing. The
  script (`set -euo pipefail`) works in a fresh directory and refuses on the first
  failure, so a bundle step that fails publishes nothing:
  1. Versions come from `cargo metadata --no-deps --format-version 1`, read with
     `jq`, for `gobby-client`, `gobby-daemon`, `gobby-code`, `gobby-hooks`,
     `gobby-mcp` and `gobby-terminal`. gdaemon's must equal the tag.
  2. For each other member, `gh release download <bin>-v<version>` fetches its four
     Unix archives with `SHA256SUMS` and `SHA256SUMS.sig`. The script verifies the
     signature with
     `openssl pkeyutl -verify -pubin -inkey crates/gdaemon/assets/release-signing.pub -rawin -in SHA256SUMS -sigfile SHA256SUMS.sig`
     and each archive's hash. A missing release or asset fails naming the member and
     version, so a gdaemon tag pushed before its members publishes nothing. gdaemon's
     own archives come from the job's build artifacts.
  3. The Linux x86_64 members are extracted and probed:
     `gdaemon schema version --json`, then `gcode schema-identity --json` and
     `ghook schema-identity --json`, which must agree on the six identity fields
     `src/gobby/storage/schema_identity_pin.py::validate_identity` requires.
     `gdaemon node release-info --json` (2.1) gives the contract.
  4. `jq -n` writes `node-release.json`: `release`, `contract` (the integer
     release-info prints), `versions` (member name to version) and `members` (bundle
     target to member name to SHA-256). Member names are `gobby`, `gdaemon`, `gcode`,
     `ghook`, `gmcp` and `gterm`; gclient's binary ships as `gobby` (D11).
     `sign_release.sh file` signs it.
  5. For each of `Darwin-arm64` (`aarch64-apple-darwin`), `Darwin-x86_64`,
     `Linux-x86_64` and `Linux-aarch64`, `gobby-node-<target>.tar.gz` holds the
     directory `gobby-node/` with the six members (0755), `node-release.json` and
     `node-release.json.sig`.
- The `assets` array gains the four bundles, `node-release.json` and
  `node-release.json.sig`, and the `Sign checksums` step (1.1) covers them.
- `release_scripts.rs` gains the script's tests. Each runs it against fake `gh` and
  `cargo` scripts on `PATH` serving signed member archives whose binaries are shell
  scripts printing an identity and a release-info. The working directory is a
  temporary tree whose `crates/gdaemon/assets/release-signing.pub` is a test key.
  Each returns early with a message when `jq`, `openssl` 3 or `sha256sum` is
  missing.

Research context:
- `crates/gdaemon/tests/release_workflow.rs::release_workflow_builds_publishes_and_packages_gdaemon`
  asserts required strings in `release-gdaemon.yml`.
- `gdaemon schema version --json` and `<bin> schema-identity --json` are the probes
  `src/gobby/install/bin_set_coherence.py::probe_set_member_identity` runs.
- Planned checks: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-daemon -E 'binary(release_workflow) | binary(release_scripts)'`.

**Acceptance:**

- 1.3.1 - From verified member archives, the script writes a signed `node-release.json` with the contract, each member's version and per-target hash, and four bundles that each extract to `gobby-node/` with six executable members and the signed manifest. test: `crates/gdaemon/tests/release_scripts.rs::bundles_carry_signed_manifest`.
- 1.3.2 - A member release that is missing, carries a bad signature or a hash mismatch, a gdaemon version that differs from the tag, or disagreeing identities fail the script with nothing written. test: `crates/gdaemon/tests/release_scripts.rs::bundle_refuses_unverified_or_incoherent_members`.
- 1.3.3 - `release-gdaemon.yml` assembles the bundles after checksum generation and before signing, and uploads the bundles and the signed manifest. test: `crates/gdaemon/tests/release_workflow.rs::release_workflow_publishes_node_bundles`.

## P2: `gobby node` and enrollment
`kind: framing`

`gdaemon node` carries the verbs (D4), `gobby` hands them to it, and enrollment
creates the node's bootstrap, key and certificate pin in one transaction.

### 2.1 `gdaemon node` and the no-toolchain harness [category: code]
`kind: deliverable`

Targets:
- `crates/gdaemon/src/main.rs::Command`
- `crates/gdaemon/src/main.rs::main`
- `crates/gdaemon/src/lib.rs`
- `crates/gdaemon/src/node_setup/mod.rs`
- `crates/gdaemon/tests/node_setup.rs`
- `crates/gdaemon/tests/node_setup_support/mod.rs`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: bumps this leaf's binaries in `MANAGED_BIN_VERSION_PINS` (Release ownership)

Python surface replaced: none yet; this leaf gives the later verbs their home and
harness.

- `main.rs` gains `Command::Node { command: NodeCommand }` under `#[cfg(unix)]`,
  dispatched to `gobby_daemon::node_setup::run`. `lib.rs` declares
  `#[cfg(unix)] pub mod node_setup;`. This leaf's only verb is
  `NodeCommand::ReleaseInfo { json: bool }`.
- `gdaemon node release-info --json` prints
  `{"release": "<CARGO_PKG_VERSION>", "contract": <node_commands::CONTRACT_VERSION>}`;
  without `--json` it prints `gdaemon <version>, node contract <contract>`. 1.3
  reads it.
- `node_setup_support/mod.rs` is the no-toolchain harness (Constraints).

Research context:
- `crates/gdaemon/src/node_commands.rs::CONTRACT_VERSION` is a `u64`, and
  `node_commands/checkout_read.rs` rejects any other `v`. #24660 (Node-commands
  contract) may change its value, so no text here names one.
- `crates/gdaemon/src/main.rs` is 316 lines and `lib.rs` 16.
- Planned checks: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-daemon -E 'binary(node_setup)'`.

**Acceptance:**

- 2.1.1 - `gdaemon node release-info --json` prints the crate version and `node_commands::CONTRACT_VERSION` in the sandbox, with no tripwire fired. test: `crates/gdaemon/tests/node_setup.rs::release_info_reports_version_and_contract_without_python`.
- 2.1.2 - `gdaemon` is one patch version above the executor's starting tip in `crates/gdaemon/Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 2.2 `gobby node install` and `gobby node enroll` run the sibling gdaemon [category: code] (depends: 2.1)
`kind: deliverable`

Targets:
- `crates/gclient/src/command.rs::dispatch_inner`
- `crates/gclient/src/command/help.rs::VERBS`
- `crates/gclient/src/command/node.rs`
- `crates/gclient/tests/node_verbs.rs`
- `crates/gclient/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: bumps this leaf's binaries in `MANAGED_BIN_VERSION_PINS` (Release ownership)

Python surface replaced: the Python `gobby` entry point for node setup (D4).

- `dispatch_inner` routes the verb `node` whose first positional is `install` or
  `enroll` to `node::exec_gdaemon` right after help handling, before
  `--daemon-url` or the token is resolved. It canonicalizes
  `std::env::current_exe()` and execs `<its directory>/gdaemon node <verb> <the
  remaining arguments>` with `std::os::unix::process::CommandExt::exec`, so the
  prompts, output and exit status are gdaemon's. A missing sibling `gdaemon` exits 2
  naming the expected path. On Windows the two verbs exit 2 with "gobby node runs
  on macOS and Linux".
- `help.rs::VERBS` gains `node` with usage
  `node install --hub URL [--email EMAIL] [--fingerprint sha256:HEX] [--label LABEL] [--insecure] | node enroll --hub URL --email EMAIL [...]`,
  which also makes `command.rs::is_verb` accept it. 4.5 adds `list` and `update`.
- The test copies `env!("CARGO_BIN_EXE_gclient")` into a temporary directory beside a
  stub `gdaemon` script that records its arguments and exits 7, then runs it with
  `env_clear()` and an empty `HOME`, so no daemon URL or token exists.

Research context:
- `crates/gclient/src/command.rs::dispatch_inner` resolves `--daemon-url` (else
  `gobby_core::daemon_url::daemon_url`) and the token (else
  `gobby_core::local_token::read_api_key`) before any action runs, so an unenrolled
  machine cannot reach a verb behind it.
- Planned checks: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-client -E 'binary(node_verbs)'`.

**Acceptance:**

- 2.2.1 - With no daemon URL or token available, `gclient node install` and `gclient node enroll` exec the sibling `gdaemon node <verb>` with every argument unchanged and return its exit status. test: `crates/gclient/tests/node_verbs.rs::node_setup_verbs_exec_sibling_gdaemon`.
- 2.2.2 - A missing sibling `gdaemon` exits 2 naming the expected path. test: `crates/gclient/tests/node_verbs.rs::node_setup_verbs_name_missing_gdaemon`.
- 2.2.3 - `gclient` is one patch version above the executor's starting tip in `crates/gclient/Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 2.3 `gobby node enroll` [category: code] (depends: 2.2)
`kind: deliverable`

Targets:
- `crates/gdaemon/src/node_setup/mod.rs`
- `crates/gdaemon/src/node_setup/files.rs`
- `crates/gdaemon/src/node_setup/bootstrap.rs`
- `crates/gdaemon/src/node_setup/enroll.rs`
- `crates/gdaemon/src/node_setup/enroll/tests.rs`
- `crates/gdaemon/src/node_setup/hub_pin.rs`
- `crates/gdaemon/src/node_setup/machine_id.rs`
- `crates/gdaemon/tests/node_setup.rs`
- `crates/gdaemon/tests/node_setup_support/mod.rs`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: bumps this leaf's binaries in `MANAGED_BIN_VERSION_PINS` (Release ownership)

Python surface replaced: `src/gobby/cli/auth_login.py::enroll` and `::login`,
`src/gobby/utils/machine_id.py::_get_or_create_machine_id`, the remote branch of
`src/gobby/cli/_install_daemon.py::_run_install_preflight`, which demands
`database_url`, and the hand-written node bootstrap in `docs/guides/shared-stack.md`.

**Granularity:** one leaf with eight acceptance items. They are the stages of one
enrollment transaction (bootstrap, pin, mint, verify, publish, settle), and the
rollback items test the publish and settle code the success item runs.

`NodeCommand::Enroll { hub, email, fingerprint: Option, label: Option, insecure }`
ports `enroll` in Python's order and with its messages, except where noted. A failed
enroll writes no bootstrap, key or pin; `machine_id` persists, because it is the
machine's identity.

- `files.rs::replace_file(path, bytes, mode)` writes a temporary file beside `path`
  with `create_new` and `mode`, fsyncs it, renames it over `path`, fsyncs the
  directory, and removes the temporary file on any failure. Every later leaf writes
  through it.
- `bootstrap.rs` resolves the home with `gobby_core::bootstrap::bootstrap_path` and
  creates it 0700. Before any request it checks the bootstrap enroll would publish,
  by building its YAML and parsing it with
  `gobby_core::bootstrap::parse_hub_database_bootstrap`, which normalizes the URL
  and refuses a non-http(s) URL and the node's own origin
  (`crates/gcore/src/bootstrap/run_mode.rs`).
  - Absent bootstrap: publish writes a new one with `datastore_mode: remote`,
    `hub_daemon_url: <hub>` and the key fields. Ports and bind host keep gcore's
    defaults.
  - Present bootstrap in remote mode with the same normalized hub: publish updates
    its key fields and keeps every other key.
  - Present bootstrap with another hub: refused, "this node is enrolled with <hub>;
    enroll never re-homes a node".
  - Present bootstrap in local or hub mode: refused, naming the mode and "node
    enroll needs a fresh GOBBY_HOME".
- `--fingerprint` is refused with http. Plain http to a host that is not loopback
  needs `--insecure`.
- `machine_id.rs` reads `<gobby_home>/machine_id`; when it is absent, it takes `flock`
  on `<gobby_home>/.machine_id.lock`, reads again, and writes a v4 UUID 0600 through
  `replace_file`.
- `hub_pin.rs`: for https, a TLS handshake to the hub's host and port with a rustls
  `ServerCertVerifier` that accepts and records the end-entity certificate. Its
  fingerprint is `front_door::tls::fingerprint` of the PEM. Enroll prints it, then
  matches `--fingerprint` or asks (D16). The PEM is staged in `<gobby_home>/tls/`
  (0700).
- Password per D16. `POST /api/auth/keys/bootstrap` with `email`, `password`,
  `machine_id`, `hostname` (`front_door::tls::machine_hostname`), `os` (`Darwin` or
  `Linux`, Python's `platform.system()` values), and `label` (default hostname). The
  hyper client uses `front_door::tls::pinned_client_config` with the staged PEM,
  reads no proxy environment, and has a 10 s timeout. A certificate name failure
  gives `_network_error`'s SAN message; the password was not sent.
- Verification as `_verified`: 401 is "the hub rejected the email or password."; the
  key passes `gobby_core::api_key_format::parse`; `key_id`, `user_id` and
  `machine_id` are UUIDs; `machine_id` is ours; `hint` equals `&key[45..49]` (the
  last four body characters, `src/gobby/utils/api_key_format.py::hint`); `key_id`
  differs from the prior `api_key_id`.
- Publish as `_publish`, under `flock` on `<gobby_home>/.bootstrap.yaml.lock` (0600):
  write `tls/hub.pem` (0600), set `api_key`, `api_key_id` and `hub_cert` (or remove
  `hub_cert` for http) in the YAML mapping, and replace or create the bootstrap
  (0600). The YAML round trip keeps key order.
- Settle as `_settle` and revoke as `_revoke` (`POST /api/auth/login`, the
  `gobby_session` cookie, `DELETE /api/auth/keys/{id}`). Messages that name
  `gobby auth key` name "`api_key_id` in bootstrap.yaml" and "the hub UI" instead.
- Success prints "Enrolled with <origin>: API key <hint> (id <key_id>)."

Research context:
- `tests/e2e/test_hub_node_pair.py::pair_node` already runs `gdaemon serve` on a
  bootstrap with no `database_url`; only the Python installer demands one.
- gcore has bootstrap readers only. `front_door::tls::write_owner_only` is
  create-only, so it cannot replace a file.
- `crates/gdaemon/src/front_door/relay.rs::HubRelay::forward` turns transport errors
  into 502 responses, so enroll uses the hyper client directly to keep the SAN
  message.
- `gobby_core::machine::local_os_name` returns `macos`; Python sent `Darwin`.
- Tests use `crates/gdaemon/tests/node_support/fake_hub.rs::FakeHub` for scripted
  replies behind `crates/gdaemon/tests/common/mod.rs::start_tls_front_door` with a
  `common::self_signed` certificate, and a key that `api_key_format::parse` accepts.
  The Python behaviors come from `tests/cli/test_auth_login.py`.
- Planned checks: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-daemon -E 'binary(node_setup)'`.

**Acceptance:**

- 2.3.1 - On an empty home, against a self-signed TLS stub hub, enroll with the matching fingerprint and a piped password writes a 0600 remote bootstrap that gcore parses to that hub with `api_key`, `api_key_id` and `hub_cert`, writes `tls/hub.pem` at 0600 and creates `machine_id`, with no tripwire fired. test: `crates/gdaemon/tests/node_setup.rs::enroll_creates_node_bootstrap_without_python`.
- 2.3.2 - Enrolling again with the same hub replaces the key and keeps every other bootstrap key, and another hub, a local or hub bootstrap, or a `--hub` naming the node's own origin is refused before any request with nothing written. test: `crates/gdaemon/tests/node_setup.rs::enroll_refuses_rehome_and_local_bootstrap`.
- 2.3.3 - Each malformed or mismatched hub reply (bad key, non-UUID ids, foreign machine, wrong hint, reused key id) refuses with nothing saved and names the unverified key id. test: `crates/gdaemon/tests/node_setup.rs::enroll_refuses_unverified_reply`.
- 2.3.4 - `--fingerprint` with http, and http to a non-loopback host without `--insecure`, are refused before any request. test: `crates/gdaemon/tests/node_setup.rs::enroll_refuses_insecure_transport`.
- 2.3.5 - A 401, an unreachable hub, a stalled hub past 10 s, a fingerprint mismatch and a certificate that does not cover the host each leave the prior enrollment byte for byte, or no bootstrap on an empty home, and the SAN case says the password was not sent. test: `crates/gdaemon/tests/node_setup.rs::enroll_failure_keeps_prior_enrollment`.
- 2.3.6 - An unusable bootstrap lock or an unwritable home rolls back the pin and revokes the minted key through login and DELETE, and a failed revoke names the key id. test: `crates/gdaemon/tests/node_setup.rs::enroll_rolls_back_and_revokes`.
- 2.3.7 - Settle reports committed when the bootstrap names the key, and unresolved when the prior pin cannot be restored. test: `crates/gdaemon/src/node_setup/enroll/tests.rs::settle_decides_by_bootstrap`.
- 2.3.8 - `gdaemon` is one patch version above the executor's starting tip in `crates/gdaemon/Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 2.4 Rust remediation names `gobby node enroll` [category: code] (depends: 2.3, #24719)
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
- `crates/gmcp/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: bumps this leaf's binaries in `MANAGED_BIN_VERSION_PINS` (Release ownership)

Python surface replaced: the Python `gobby auth login` these messages send a node
operator to.

- Each message below names `gobby node enroll` where it named `gobby auth login`:
  `NodeServices::prepare`'s two refusals, `read_api_key_at`,
  `unauthorized_recovery`'s two texts, `AUTH_401_REMEDIATION`, and `StartupError`'s
  token message. The tests that assert them follow.
- The gcode floor: after #24719, `MIN_GCODE_PRUNE_BUDGET_VERSION` reads the `gcode`
  pin, so this leaf's pin bump moves it and the leaf edits neither the floor nor
  `gcode_gateway.py` (D24).
- A guard test walks every `crates/*/src` file and fails on the literal
  `gobby auth login`.

Research context:
- Sites: `crates/gdaemon/src/state.rs` lines 39 and 51, `crates/gcore/src/local_token.rs`
  line 15, `crates/gcode/src/cli_error.rs` lines 183 and 186,
  `crates/ghook/src/diagnose.rs` lines 41 and 311, `crates/gclient/src/startup.rs`
  line 147; tests `crates/gdaemon/tests/nodes.rs` line 482,
  `crates/gdaemon/tests/run_modes.rs` line 54, `crates/gclient/tests/startup.rs`
  line 871.
- `crates/gmcp` depends on `gobby-core`, so the gcore edit bumps it once 1.2 ships it.
- `tests/code_index/test_gcode_gateway.py` asserts `GCODE_PIN == MIN_GCODE_PRUNE_BUDGET_VERSION`.
  `src/gobby/code_index/gcode_gateway.py` line 29 already derives
  `MIN_GCODE_GRAPH_VERSION` from `MANAGED_BIN_VERSION_PINS["gcode"]`; #24719 does
  the same for line 30.
- Planned checks: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-daemon -p gobby-core -p gobby-code -p gobby-hooks -p gobby-client -p gobby-mcp`; `GOBBY_TEST_PROTECT=1 uv run pytest tests/install/test_version_pins.py tests/code_index/test_gcode_gateway.py -v`.

Consumers unchanged:
- `tests/tasks/expansion/test_contract_validation_criteria.py` — no-edit-reason: uses `gobby auth login` as an arbitrary phrase in validation-criteria fixtures.
- `crates/gcore/src/grant/acquisition.rs` — no-edit-reason: calls `read_api_key_at` and propagates its error text unchanged.

**Acceptance:**

- 2.4.1 - `gdaemon serve` on an unenrolled node home refuses naming `gobby node enroll`, with no tripwire fired. test: `crates/gdaemon/tests/node_setup.rs::serve_without_key_names_enroll_without_python`.
- 2.4.2 - No file under `crates/*/src` contains `gobby auth login`. test: `crates/gdaemon/tests/node_setup.rs::no_crate_names_python_login`.
- 2.4.3 - The node startup and claim tests assert the new remediation. test: `crates/gdaemon/tests/nodes.rs::node_startup_requires_a_pinned_hub`.
- 2.4.4 - `gcode`, `gdaemon`, `ghook` and `gclient` are one patch version above the executor's starting tip in their `Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.
- 2.4.5 - `gmcp` is one patch version above the executor's starting tip in `crates/gmcp/Cargo.toml` and `Cargo.lock`. file: `crates/gmcp/Cargo.toml`.
- 2.4.6 - The gcode prune floor equals the bumped `gcode` pin. test: `tests/code_index/test_gcode_gateway.py::test_managed_gcode_pin_matches_prune_budget_floor`.

### 2.5 Python login retires and pair_node enrolls with the Rust verb [category: code] (depends: 2.3, #24604)
`kind: deliverable`

Targets:
- `src/gobby/cli/auth_login.py::*` — scope-reason: deletes `login` and every helper only it reaches; `_key_data`, `_key_client` and the module docstring name `gobby node enroll`
- `src/gobby/cli/auth.py::*` — scope-reason: registers `key` only
- `src/gobby/cli/hub_backup/bootstrap_restore.py::restore_archived_bootstrap`
- `src/gobby/cli/sessions_dirty_paths.py::_LOGIN_HINT`
- `src/gobby/cli/sessions_dirty_paths.py::approve_dirty_paths`
- `src/gobby/cli/installers/remote_preflight.py::*` — scope-reason: its three remediation strings name `gobby node enroll` (D18)
- `src/gobby/hooks/inbox.py::_drain_hook_inbox_once_locked`
- `src/gobby/servers/middleware/auth.py::_LOGIN_GUIDANCE`
- `src/gobby/utils/daemon_client.py::DAEMON_AUTH_REMEDIATION`
- `src/gobby/install/shared/skills/gobby/references/admin/authentication.md`
- `docs/reference-audit/admin.json::*` — scope-reason: the `gobby auth login` operation leaves `operations` and a `gobby node enroll` `native_cli` entry replaces it
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

- `auth_login.py` keeps `key`, `key_list`, `key_revoke` and what they reach (`_key_*`,
  `_mint_key`, `_rotate_key`, `_publish_rotation`, `_restore_rotation`, and the
  shared helpers they call). Every message in Python that sends a node operator to
  `gobby auth login` names `gobby node enroll`.
- `_enroll(home, *args)` runs `<native bin dir>/gdaemon node enroll --hub <origin> --email TEST_EMAIL ...`
  with the env from `prepare_daemon_env`, `GOBBY_HOME`, and `PATH` set to a directory
  holding only tripwire `python`, `python3` and `uv` scripts. It pipes
  `TEST_PASSWORD`. `pair_node` calls it with `--hub` and `--fingerprint` and asserts
  the tripwire marker is absent. The fixture builds `gdaemon` but not `gclient`, so
  it runs gdaemon directly; 2.2 proves the `gobby` route. The two e2e tests become
  `test_enroll_pins_self_signed_hub` and `test_enroll_refuses_host_outside_sans`.
- `admin.json`: the `gobby auth login` operation leaves `operations`, and a
  `native_cli` entry for `gobby node enroll --hub URL --email EMAIL [--fingerprint sha256:...] [--label LABEL] [--insecure]`
  points at `crates/gdaemon/src/node_setup/enroll.rs::run` with the same reference
  and the `source-inventory` evidence. `authentication.md` step 3 names the verb.
- Parity ledger: the `CLI gobby auth login` row's status becomes
  `dropped by decision #24657`, with a note naming `gobby node enroll`
  (`crates/gdaemon/src/node_setup/enroll.rs::run`). `docs/contracts/parity-ledger.md`
  is regenerated with `scripts/generate_parity_ledger.py`. The dropped row keeps its
  surface key.

Research context:
- `scripts/generate_parity_ledger.py::render_ledger` refuses a surface absent from the
  inventory unless its status is `dropped by decision #N`, and statuses other than
  `proxied`, `delegated` and that drop are invalid. The row therefore changes in the
  leaf that deletes the command.
- `tests/skills/reference_library_helpers.py::documentation_errors` resolves every
  `gobby ` operation in the Click registry, so the deleted command must leave
  `operations` in the same leaf.
- `tests/fixtures/test_gdaemon_binary.py` patches `pair._login` by name.
- Planned checks: `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/cli/test_auth_login.py tests/cli/test_pack.py tests/config/test_remote_ui_auth.py tests/servers/test_auth_middleware.py tests/servers/test_http_middleware.py tests/utils/test_daemon_client.py tests/workflows/test_dirty_edit_grants.py tests/fixtures/test_gdaemon_binary.py tests/skills/test_reference_library.py tests/test_parity_ledger.py tests/contracts/test_http_corpus.py tests/e2e/test_auth_login_e2e.py tests/e2e/test_hub_node_pair.py -v`.

Consumers unchanged:
- `src/gobby/cli/install.py` — no-edit-reason: its remote path keeps calling `run_remote_preflight` (D18).
- `src/gobby/cli/sessions.py` — no-edit-reason: registers `approve_dirty_paths`, whose signature is unchanged.
- `tests/hooks/test_inbox.py` — no-edit-reason: asserts no remediation text from `_drain_hook_inbox_once_locked`.
- `tests/hooks/test_inbox_barrier_deadline.py` — no-edit-reason: asserts no remediation text from `_drain_hook_inbox_once_locked`.

**Acceptance:**

- 2.5.1 - `pair_node` enrolls with `gdaemon node enroll` under a tripwire-only `PATH`, the tripwire stays silent, and the enrolled node relays and handshakes. test: `tests/e2e/test_hub_node_pair.py::test_node_enrolls_and_relays`.
- 2.5.2 - `gdaemon node enroll` pins a self-signed live hub. test: `tests/e2e/test_auth_login_e2e.py::test_enroll_pins_self_signed_hub`.
- 2.5.3 - `gobby auth login` is no longer a registered command, and `gobby auth key` still is. test: `tests/cli/test_auth_login.py::test_login_command_is_gone`.
- 2.5.4 - The audit lists `gobby node enroll` and no `gobby auth login` operation. test: `tests/skills/test_reference_library.py::test_reference_contract_3_2_2`.
- 2.5.5 - The middleware guidance names the new verb. test: `tests/servers/test_auth_middleware.py::test_protected_routes_require_auth_when_enabled`.
- 2.5.6 - The committed parity ledger matches the inventory with `CLI gobby auth login` dropped by decision #24657. test: `tests/test_parity_ledger.py::test_committed_ledger_matches_current_public_inventory`.

## P3: Node install from a bundle
`kind: framing`

`gobby node install` runs its steps in this order: enrollment when the node has no
key (2.3), the bundle's binaries (3.2), SRT (3.3), claude (3.4), codex (3.5),
droid (3.6), grok (3.7) and agy (3.8) hooks, then the service (3.9). A CLI's config
changes only after every step before it succeeds, and a CLI absent from `PATH` is
skipped with a message.

### 3.1 Release verification library [category: code] (depends: 2.4, #24717)
`kind: deliverable`

Targets:
- `crates/gdaemon/src/release.rs`
- `crates/gdaemon/src/release/tests.rs`
- `crates/gdaemon/src/lib.rs`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: bumps this leaf's binaries in `MANAGED_BIN_VERSION_PINS` (Release ownership)

Python surface replaced: `src/gobby/install/install_release.py`'s sidecar-checked
downloads, for node binaries (D2).

`release.rs` is portable (Constraints), and `lib.rs` declares `pub mod release;`.

- Locations: assets download from
  `https://github.com/GobbyAI/gobby/releases/download/<tag>/<asset>`, and the release
  list is `https://api.github.com/repos/GobbyAI/gobby/releases?per_page=100`, with
  D21's debug-only overrides.
- `verify_signature(message, signature)`: the embedded key is
  `include_str!("../assets/release-signing.pub")`, the PEM SubjectPublicKeyInfo that
  `openssl pkey -pubout` writes. The parser base64-decodes it (`base64` is already a
  dependency), requires the 44-byte DER to start with the Ed25519 prefix
  `302a300506032b6570032100`, and verifies with `ring::signature::ED25519` over the
  last 32 bytes. In debug builds a key in `GOBBY_RELEASE_TEST_KEY`, in the same PEM
  form, is accepted as well.
- `parse_sums(text)` reads `sha256sum` lines into a map; a malformed line or a
  duplicate name refuses.
- `NodeRelease` (serde, unknown fields ignored so an older node can read a newer
  manifest): `release`, `contract: u64`, `versions` and `members` as 1.3
  writes them. The member set is exactly `gobby`, `gdaemon`, `gcode`, `ghook`,
  `gmcp` and `gterm`.
- `bundle_target()` maps `std::env::consts::{OS, ARCH}`: `macos`/`aarch64` to
  `Darwin-arm64`, `macos`/`x86_64` to `Darwin-x86_64`, `linux`/`x86_64` to
  `Linux-x86_64`, `linux`/`aarch64` to `Linux-aarch64`, anything else to `None`.
- `verify_bundle_dir(dir)` reads `node-release.json` and its `.sig` from `dir`,
  verifies the signature, and checks every member's SHA-256 for `bundle_target()`.
  A missing or mismatched member refuses, naming it.
- `fetch(url, limit)` uses a `ureq` agent with a 10 s connect timeout and a 120 s
  overall timeout, and refuses a non-200 status or a body over `limit` (1 MiB for
  JSON and sums, 256 MiB for a bundle).
- `fetch_node_release(version)` downloads `node-release.json` and its `.sig` from
  `gdaemon-v<version>` and verifies them.
- `download_bundle(version, dest)` downloads the release's `SHA256SUMS` and its
  `.sig`, verifies the signature, downloads `gobby-node-<target>.tar.gz`, checks its
  hash against `SHA256SUMS`, extracts it with `tar -xzf <file> -C <staging>` into a
  fresh staging directory beside `dest`, runs `verify_bundle_dir` on the extracted
  `gobby-node/`, and renames it to `dest`. Any failure removes the staging directory.
- `published_releases()` lists the GitHub releases and keeps `gdaemon-v` tags that
  are neither drafts nor prereleases, as versions, newest first.
- Tests sign with an `ring::signature::Ed25519KeyPair` generated in the test and
  serve a fake release from a local `std::net::TcpListener` thread.

Research context:
- gcore declares `ureq = { version = "2", features = ["json"] }` without
  `default-features = false`, so rustls and gzip are already built for it; `ring`
  0.17.14 is locked through rustls.
- `crates/gdaemon/assets/release-signing.pub` comes from #24717.
- Planned checks: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-daemon -E 'test(release::)'`.

**Acceptance:**

- 3.1.1 - A signature by the test key over `SHA256SUMS` verifies, and a changed byte, another key or a malformed public key refuses. test: `crates/gdaemon/src/release/tests.rs::signature_verifies_only_the_signed_bytes`.
- 3.1.2 - The embedded `release-signing.pub` parses as an Ed25519 key. test: `crates/gdaemon/src/release/tests.rs::embedded_release_key_parses`.
- 3.1.3 - `download_bundle` from a fake release yields a verified bundle directory, and a bad `SHA256SUMS` signature, a bundle hash mismatch or a member hash mismatch refuses with no staging directory left. test: `crates/gdaemon/src/release/tests.rs::download_bundle_verifies_before_and_after_extraction`.
- 3.1.4 - `published_releases` keeps only published `gdaemon-v` tags, newest first. test: `crates/gdaemon/src/release/tests.rs::published_releases_keep_gdaemon_tags`.
- 3.1.5 - `bundle_target` names the four bundle targets and nothing for other platforms. test: `crates/gdaemon/src/release/tests.rs::bundle_target_matches_uname_names`.
- 3.1.6 - `gdaemon` is one patch version above the executor's starting tip in `crates/gdaemon/Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 3.2 `gobby node install` enrolls and promotes the bundle [category: code] (depends: 3.1)
`kind: deliverable`

Targets:
- `crates/gdaemon/src/node_setup/mod.rs`
- `crates/gdaemon/src/node_setup/install.rs`
- `crates/gdaemon/src/node_setup/binaries.rs`
- `crates/gdaemon/tests/node_setup.rs`
- `crates/gdaemon/tests/node_setup_support/mod.rs`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: bumps this leaf's binaries in `MANAGED_BIN_VERSION_PINS` (Release ownership)

Python surface replaced:
`src/gobby/install/bin_set_coherence.py::promote_workspace_binary_set`.

- `NodeCommand::Install { hub, email: Option, fingerprint: Option, label: Option, insecure }`.
  When the bootstrap has no `api_key`, install runs 2.3's enroll with the same
  arguments, and `--email` is then required; a missing `--email` is refused before
  anything is written. When the bootstrap names the same hub with a key, enrollment
  is skipped. Enroll's own refusals apply otherwise.
- `binaries.rs`: the source directory is the parent of `std::env::current_exe()`,
  canonicalized.
  - When it is `<gobby_home>/bin`, the step prints that the binaries are installed
    and skips.
  - Otherwise `release::verify_bundle_dir(source)` must pass. A directory without a
    valid signed manifest refuses: "install runs from a published node bundle",
    followed by the Overview's first two commands.
  - Identity: `gdaemon`, `gcode` and `ghook` are probed as
    `probe_set_member_identity` does (`gdaemon schema version --json`, else
    `<bin> schema-identity --json`, 10 s). Each result must carry the six fields
    `src/gobby/storage/schema_identity_pin.py::validate_identity` requires, and the
    three must be equal; otherwise the step refuses with nothing promoted.
  - `promote_set(source, bin, manifest)` promotes, per member in order (`gdaemon`,
    `gcode`, `ghook`, `gmcp`, `gterm`, `gobby`): take a non-blocking `flock` on
    `<bin>/.locks/<name>.lock`; copy into a `.<name>-staging-*` directory under
    `<bin>`, fsync, `chmod 0755`; on macOS run `/usr/bin/codesign -f -s - <staged>`
    (30 s); rename over `<bin>/<name>`. A failure reports promoted and unpromoted
    members as Python does. Hashes are checked before signing, because signing
    changes the bytes.
  - After all six: the stamp `<bin>/.gdaemon-schema-identity.json` holds the identity
    as compact JSON with sorted keys and a trailing newline
    (`src/gobby/storage/schema_identity_pin.py::stamp_bytes`), 0644, and
    `<bin>/.node-release.json` holds the verified manifest bytes, 0644, both through
    `replace_file` (D14). 4.3 reuses `promote_set`.
- Install prints D11's `export PATH` line when `<gobby_home>/bin` is not on `PATH`.
- The harness builds a bundle directory from the copied `gdaemon` and stub siblings:
  `gcode` and `ghook` scripts that print the identity the copied `gdaemon` prints,
  and `gobby`, `gmcp` and `gterm` scripts. It writes their `node-release.json` and
  signs it with a test key passed as `GOBBY_RELEASE_TEST_KEY`. On macOS `codesign`
  signs a script through extended attributes.

Research context:
- `promote_workspace_binary_set` probes, validates, requires agreement, locks with
  `bin_freshness_locks.py::try_acquire_native_bin_lock`, stages through
  `bin_freshness_promotion.py::stage_and_promote_binary_file`, signs with
  `_codesign_workspace_binary`, and stamps with `_write_identity_stamp`.
- `SET_MEMBERS` (`src/gobby/utils/native_bin.py`) is `gcode`, `gdaemon`, `ghook`.
  node-halves runs `<gobby_home>/bin/gterm` as the node terminal host.
- Planned checks: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-daemon -E 'binary(node_setup)'`.

**Acceptance:**

- 3.2.1 - Install from a signed bundle directory on an empty home enrolls against the TLS stub hub, promotes all six members to new inodes at 0755, writes the identity stamp and `.node-release.json`, and prints the `export PATH` line, with no tripwire fired. test: `crates/gdaemon/tests/node_setup.rs::install_enrolls_and_promotes_bundle_without_python`.
- 3.2.2 - An unsigned or tampered bundle, a missing member or disagreeing identities refuse before any member is promoted. test: `crates/gdaemon/tests/node_setup.rs::install_refuses_unverified_or_incoherent_bundle`.
- 3.2.3 - Install run from `<gobby_home>/bin` on an enrolled home skips enrollment and promotion and needs no `--email`, while an unenrolled home without `--email` is refused with nothing written. test: `crates/gdaemon/tests/node_setup.rs::install_skips_done_steps`.
- 3.2.4 - `gdaemon` is one patch version above the executor's starting tip in `crates/gdaemon/Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 3.3 `gobby node install` stages the pinned SRT [category: code] (depends: 3.2, #24374)
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
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: bumps this leaf's binaries in `MANAGED_BIN_VERSION_PINS` (Release ownership)

Python surface replaced: `src/gobby/cli/install_setup_srt.py::_install_srt_runtime`.

The step ports `_install_srt_runtime` in its order, under the install lock #24374
uses:

- A verified install at `<gobby_home>/tools/srt/<SRT_VERSION>` is reused (the
  `.previous` backup is removed). Otherwise:
- `node` and `npm` are resolved from `PATH`; `node --version` must be at least
  22.12.0 (`NODE_MIN_VERSION`). Either missing is refused naming the requirement
  (D15).
- Staging is a `.<version>-*` directory beside the target. `fetch_tarball` runs D15's
  `npm pack` and requires the tarball's SHA-256 to equal the pin;
  `install_from_tarball` does every step below (D19). `package.json` is
  `install_setup_srt.py::_PACKAGE_JSON`. The embedded lockfile (the D20 copy of
  `src/gobby/install/srt-package-lock.json`) is written as `package-lock.json` and
  checked. `npm ci --ignore-scripts --no-audit --no-fund --omit=dev` runs (180 s).
  The installed package's name and version must match.
- The patch table ports `src/gobby/agents/srt_package_patch.py`: each entry's file,
  original and expected SHA-256, needle and replacement, plus the macOS monitor
  needle. A file already at its expected hash is skipped; any other hash refuses.
- The embedded runner (the D20 copy of `src/gobby/agents/srt_runner.mjs`) is written
  as `runner.mjs` (0700) and checked. `receipt.json` is `SrtRelease.receipt_fields()`
  plus `node`.
- The content manifest uses #24374's builder (widened to `pub(crate)` in
  `runtime.rs`). The tree is made immutable as `make_srt_installation_immutable`
  does (0555 for directories, executables and the two `apply-seccomp` helpers; 0444
  otherwise). Promotion is `_promote_install` (rename the target to
  `.<version>.previous`, rename staging in, remove the backup, restore it on
  failure). #24374's verifier then runs.
- The pins (tarball, lockfile, runner and the four patched-file hashes, with every
  receipt field) sit beside `SRT_VERSION` in `runtime.rs`. Its lockout remediation
  `run \`gobby install srt\`` becomes `run \`gobby node install\``.
- The harness gains stub `node` (prints `v22.12.0`) and `npm`, and two helpers.
  `minimal_srt_tarball` decompresses the four `.js.gz` originals in
  `tests/fixtures/srt/` to their `package/dist/...` paths, writes `package.json` with
  the pinned name and version, and archives them with `/usr/bin/tar -czf`. The stub
  `npm pack` copies that tarball, and `npm ci` extracts it into
  `node_modules/@anthropic-ai/sandbox-runtime`. `seed_verified_srt` runs
  `install_from_tarball` on it, and every verb-level test from 3.3 on calls it, so the
  verb's SRT step reuses that install. 3.3.3 and 3.3.6 do not, because they exercise
  `fetch_tarball` on an empty SRT home.

Research context:
- `SrtRelease` (`src/gobby/utils/dependency_requirements.py`) pins version 0.0.79 and
  the hashes; `verify_srt_installation_locked` checks the runner, lockfile, receipt,
  patched files, manifest and the Node version. It compares the receipt with
  `SRT_RELEASE.receipt_fields()`, so a seeded install carrying the pinned receipt
  verifies.
- `tests/fixtures/srt/README.md`: the `.js.gz` files are the exact 0.0.79 bytes, and
  Python's fake installations already copy them and apply the staging patches.
- Planned checks: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-daemon -E 'binary(node_setup) | binary(node_agents)'`;
  operator opt-in, with network:
  `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-daemon -E 'test(install_fetches_registry_srt)' --run-ignored only`.

**Acceptance:**

- 3.3.1 - `install_from_tarball` stages the composed minimal tarball with stub `node` and `npm`, and #24374's verifier accepts the result. test: `crates/gdaemon/tests/node_setup.rs::srt_installs_from_minimal_tarball`.
- 3.3.2 - The verb reuses a seeded verified install without running `npm`, with no tripwire fired. test: `crates/gdaemon/tests/node_setup.rs::install_reuses_verified_srt_without_python`.
- 3.3.3 - The verb refuses a tarball off its pin (the composed one), Node older than 22.12.0, or no `npm`, leaving no staging directory and no install, with no tripwire fired. test: `crates/gdaemon/tests/node_setup.rs::install_refuses_srt_off_pin`.
- 3.3.4 - The Rust pins, receipt fields and patch table equal the Python ones (read with `include_str!`), the lockfile and runner copies equal their Python originals byte for byte, and the embedded lockfile and runner match their pins. test: `crates/gdaemon/tests/node_setup.rs::srt_pins_match_python`.
- 3.3.5 - Patches apply once, skip a patched file, and refuse unknown bytes; a failed promotion restores the previous install. test: `crates/gdaemon/src/node_setup/srt/tests.rs::patches_and_promotion_follow_python`.
- 3.3.6 - Run as the opt-in check, with network and Node.js 22.12.0 or later on `PATH` after the tripwires, the verb fetches the registry tarball with `npm pack`, matches its pin, and #24374's verifier accepts the install, with no tripwire fired. test: `crates/gdaemon/tests/node_setup.rs::install_fetches_registry_srt`.
- 3.3.7 - `gdaemon` is one patch version above the executor's starting tip in `crates/gdaemon/Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 3.4 `gobby node install` installs claude hooks and the MCP entry [category: code] (depends: 3.3, #24372, #24355)
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

- A CLI's step runs when its command (`claude`, `codex`, `droid`, `grok`, `agy`)
  resolves on `PATH`; otherwise install prints that it was skipped.
- `hooks.rs` holds what 3.5 to 3.8 share:
  - the command prefix `<quoted <gobby_home>/bin/ghook> --gobby-owned`, quoted by
    Python's `shlex.quote` rule (bare when every character is in
    `[A-Za-z0-9_@%+=:,./-]`, else single-quoted with `'` as `'"'"'`);
  - the port of `src/gobby/cli/installers/hook_commands.py::rewrite_hook_template_commands`:
    a command's `ghook --gobby-owned` prefix becomes that prefix with every flag
    after it kept, and a bare `__GOBBY_HOOK_COMMAND__` becomes
    `<prefix> --cli=<cli> --type=<hook>`;
  - the timeout setter (`set_gobby_hook_timeouts`).

  The `shlex` crate is not used: it quotes `=` and `%`, which Python leaves bare,
  and codex's trust hash covers the command bytes. Merging uses `repair.rs`'s
  `merge_gobby_hook_groups` port, widened to `pub(crate)`.
- `claude.rs`: the template is `include_str!` of the D20 copy of
  `src/gobby/install/claude/hooks-template.json`; timeouts are 120 s with
  `SessionEnd` 60 s. `~/.claude/settings.json` is backed up as
  `settings.json.<unix seconds>.backup`, each hook type is merged, and the file is
  replaced with two-space JSON. The `gmcp` entry goes into `~/.claude.json`
  `mcpServers.gobby` under #24372's `~/.claude.json.lock` mkdir lock, keeping every
  other key.
- Golden: `claude_golden.json` holds an input `settings.json` and `.claude.json` and
  the expected `hooks` and `mcpServers.gobby`, with the home written as `$HOME`. The
  Python test sets `HOME` to a temporary directory and `GOBBY_HOME` to
  `$HOME/.gobby`, removes `GOBBY_NATIVE_BIN_DIR`, and creates executable
  `$HOME/.gobby/bin/ghook` and `gmcp`, so `resolve_native_bin` returns those paths
  (`src/gobby/utils/native_bin.py::native_bin_dir` reads `Path.home()`). It runs
  `install_claude(project, mode="global")` and compares; with
  `GOBBY_RECORD_NODE_SETUP_GOLDEN=1` it rewrites the golden. The Rust test uses the
  same layout, runs the verb, and compares parsed values.

Research context:
- `install_claude` also installs shared content, router commands, the statusline,
  `autoMemoryEnabled` and `validate_settings.py` (`shared.py::install_global_hooks`);
  D13 excludes them. The Python hook command does not use the hooks directory
  (`hook_commands.py::build_hook_command_prefix`).
- Key order in rewritten JSON is not part of the contract; the golden compares parsed
  values.
- Planned checks: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-daemon -E 'binary(node_setup)'`; `GOBBY_TEST_PROTECT=1 uv run pytest tests/install/test_node_setup_golden.py -v`.

**Acceptance:**

- 3.4.1 - Install over the golden's input writes the golden's claude hooks and `gmcp` entry, keeps the user's handlers and other keys, and backs up `settings.json`, with no tripwire fired. test: `crates/gdaemon/tests/node_setup.rs::install_claude_hooks_match_python_golden`.
- 3.4.2 - Python's installer produces the claude golden. test: `tests/install/test_node_setup_golden.py::test_claude_golden_matches_python`.
- 3.4.3 - A CLI absent from `PATH` is skipped with its config untouched, and a path with a space is single-quoted as Python quotes it. test: `crates/gdaemon/tests/node_setup.rs::install_skips_absent_cli_and_quotes_like_python`.
- 3.4.4 - The crate-local claude template copy equals `src/gobby/install/claude/hooks-template.json` byte for byte. test: `crates/gdaemon/tests/node_setup.rs::claude_template_copy_matches_python`.
- 3.4.5 - `gdaemon` is one patch version above the executor's starting tip in `crates/gdaemon/Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 3.5 `gobby node install` installs codex hooks, trust and the MCP entry [category: code] (depends: 3.4)
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

- Hooks as `_install_hooks_file`: the template is `include_str!` of the D20 copy of
  `src/gobby/install/codex/hooks-template.json`; timeouts are 120 s with `Interrupt`
  and `SessionEnd` 3 s; a corrupt `~/.codex/hooks.json` is quarantined as Python
  names it; groups are merged.
- `~/.codex/config.toml` through `toml_edit`, so comments and layout survive: remove
  `features.codex_hooks`, set `features.hooks = true`, and write the trust state as
  `_ensure_codex_hook_trust_state` does. Each Gobby handler gets
  `[hooks.state."<resolved hooks.json>:<EventLabel>:<g>:<h>"] trusted_hash = "sha256:<hex>"`,
  hashed as `_normalized_codex_command_hook_hash` does (sorted compact JSON,
  non-ASCII kept, of the event label and the hook's `async`, whitespace-normalized
  `command`, `timeout` (default 600, minimum 1), `type`, optional `statusMessage`,
  and `matcher` for `CODEX_MATCHER_HASH_EVENTS`; `enabled` dropped). Stale and dead
  Gobby entries are removed. The prior file is copied to `config.toml.bak`.
- The MCP entry is `configure_mcp_server_toml`'s `[mcp_servers.gobby]` block with the
  `gmcp` command and `args = []`.
- Golden: `codex_golden.json` holds an input `hooks.json` and `config.toml` and the
  expected hooks, `features`, `hooks.state` and `mcp_servers.gobby` as JSON, with the
  home as `$HOME` and every `trusted_hash` value blanked on both sides. The hashed
  command embeds the temporary home, so no recorded hash can match another run. The
  Python test sets up the home as 3.4's does and runs `install_codex`.
- Trust vectors: `codex_trust_vectors.json` holds fixed hook entries with fixed
  commands and their `_normalized_codex_command_hook_hash` values. The Python test
  checks them and rewrites them under `GOBBY_RECORD_NODE_SETUP_GOLDEN=1`;
  `codex/tests.rs` replays them against the Rust hash.

Research context:
- Codex rejects a hook whose trust hash differs from its own computation, so the
  vectors pin the hash bytes.
- Planned checks: as 3.4.

**Acceptance:**

- 3.5.1 - Install over the golden's input writes the golden's codex hooks, trust-entry keys and `gmcp` block, each `trusted_hash` is the Rust hash of the hook it names, and a user comment in `config.toml` survives, with no tripwire fired. test: `crates/gdaemon/tests/node_setup.rs::install_codex_hooks_match_python_golden`.
- 3.5.2 - Python's installer produces the codex golden. test: `tests/install/test_node_setup_golden.py::test_codex_golden_matches_python`.
- 3.5.3 - The Rust trust hash equals each vector's Python hash. test: `crates/gdaemon/src/node_setup/codex/tests.rs::trust_hash_matches_python_vectors`.
- 3.5.4 - Python's hash produces the trust vectors. test: `tests/install/test_node_setup_golden.py::test_codex_trust_vectors_match_python`.
- 3.5.5 - The crate-local codex template copy equals `src/gobby/install/codex/hooks-template.json` byte for byte. test: `crates/gdaemon/tests/node_setup.rs::codex_template_copy_matches_python`.
- 3.5.6 - `gdaemon` is one patch version above the executor's starting tip in `crates/gdaemon/Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 3.6 `gobby node install` installs droid hooks and the MCP entry [category: code] (depends: 3.5)
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
- Golden as 3.4's, with `install_droid(project, mode="global")`.

Research context:
- `install_droid` writes the flat global `hooks.json`; #24372's project file is
  `.factory/hooks/hooks.json`.
- Planned checks: as 3.4.

**Acceptance:**

- 3.6.1 - Install over the golden's input writes the golden's droid hooks and `gmcp` entry, with no tripwire fired. test: `crates/gdaemon/tests/node_setup.rs::install_droid_hooks_match_python_golden`.
- 3.6.2 - Python's installer produces the droid golden. test: `tests/install/test_node_setup_golden.py::test_droid_golden_matches_python`.
- 3.6.3 - `gdaemon` is one patch version above the executor's starting tip in `crates/gdaemon/Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 3.7 `gobby node install` installs grok hooks and the MCP entry [category: code] (depends: 3.6)
`kind: deliverable`

Targets:
- `crates/gdaemon/src/node_setup/mod.rs`
- `crates/gdaemon/src/node_setup/grok.rs`
- `crates/gdaemon/assets/node_setup/grok-hooks-template.json`
- `crates/gdaemon/tests/node_setup.rs`
- `crates/gdaemon/tests/node_setup_support/mod.rs`
- `crates/gdaemon/tests/fixtures/node_setup/grok_golden.json`
- `tests/install/test_node_setup_golden.py`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: bumps this leaf's binaries in `MANAGED_BIN_VERSION_PINS` (Release ownership)

Python surface replaced: `src/gobby/cli/installers/grok.py::install_grok`'s hook and
compatibility steps, and the `[mcp_servers.gobby]` entry
`src/gobby/agents/grok_sandbox_home.py::_declare_gobby_mcp_server` writes for
spawned runs.

- Hooks: `~/.grok/hooks/gobby.json` is Gobby's own file. It is the template
  (`include_str!` of the D20 copy of `src/gobby/install/grok/hooks-template.json`)
  rewritten by `hooks.rs`, which keeps the template's `--cli=grok` and `--type`
  flags, with every Gobby timeout at 120 s. An existing file is backed up as
  `gobby.json.<unix seconds>.backup`, and the file is replaced with two-space JSON.
- `~/.grok/config.toml` through `toml_edit`, so comments and other tables survive:
  - `[compat.claude] hooks = false`, as `_disable_claude_hook_compat` sets it, so
    grok does not also run the claude hooks from 3.4;
  - `[mcp_servers.gobby]` with `command = "<gobby_home>/bin/gmcp"` and `args = []`,
    replacing any operator `gobby` server, as `_declare_gobby_mcp_server` writes it
    after #24355. Python's interactive installer leaves grok's MCP entry to Claude
    compatibility through `~/.claude.json`; the node writes it because
    `config.toml` outranks every Claude-compatibility source and claude may be
    absent.

  When the file changes, the prior file is backed up as
  `config.toml.<unix seconds>.backup`, and the file is written 0600.
- Golden: `grok_golden.json` holds an input `gobby.json` and a `config.toml` with a
  user comment and another server, and the expected hooks, `compat` and
  `mcp_servers.gobby`. The Python test sets up the home as 3.4's does, with a stub
  `ghook` that answers `--version` at or above `_MIN_GHOOK_VERSION_FOR_GROK`, runs
  `install_grok(project, mode="global")` and then
  `_declare_gobby_mcp_server(<home>/.grok/config.toml)`, and compares.

Research context:
- `install_grok` takes `hook_timeout_seconds=120`, refuses a `ghook` older than
  0.5.0 (`_grok_ghook_support_error`), and writes the hooks file whole rather than
  merging it.
- Planned checks: as 3.4.

**Acceptance:**

- 3.7.1 - Install over the golden's input writes the golden's grok hooks, `compat.claude.hooks = false` and `gmcp` server, keeps the user comment and the other server, and backs up both files, with no tripwire fired. test: `crates/gdaemon/tests/node_setup.rs::install_grok_hooks_match_python_golden`.
- 3.7.2 - Python's installer and MCP writer produce the grok golden. test: `tests/install/test_node_setup_golden.py::test_grok_golden_matches_python`.
- 3.7.3 - The crate-local grok template copy equals `src/gobby/install/grok/hooks-template.json` byte for byte. test: `crates/gdaemon/tests/node_setup.rs::grok_template_copy_matches_python`.
- 3.7.4 - `gdaemon` is one patch version above the executor's starting tip in `crates/gdaemon/Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 3.8 `gobby node install` installs agy hooks and the MCP entry [category: code] (depends: 3.7)
`kind: deliverable`

Targets:
- `crates/gdaemon/src/node_setup/mod.rs`
- `crates/gdaemon/src/node_setup/agy.rs`
- `crates/gdaemon/assets/node_setup/agy-hooks-template.json`
- `crates/gdaemon/tests/node_setup.rs`
- `crates/gdaemon/tests/node_setup_support/mod.rs`
- `crates/gdaemon/tests/fixtures/node_setup/agy_golden.json`
- `tests/install/test_node_setup_golden.py`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: bumps this leaf's binaries in `MANAGED_BIN_VERSION_PINS` (Release ownership)

Python surface replaced: `src/gobby/cli/installers/agy.py::install_agy`'s hook,
verification and MCP steps.

- Hooks: `~/.gemini/config/hooks.json` is keyed by hook name. The template (the D20
  copy of `src/gobby/install/agy/hooks-template.json`) nests its events under
  `gobby`, and `hooks.rs` rewrites them, keeping `--cli=agy`, with 120 s timeouts.
  The merge follows `_merge_gobby_hooks`: every top-level entry that
  `config_contains_gobby_hook` recognizes is dropped, `gobby` is set, and other
  named hooks stay. The file is written only when it changes, after a
  `hooks.json.<unix seconds>.backup` of the prior file.
- MCP: `~/.gemini/config/mcp_config.json` `mcpServers.gobby` is
  `{"command": "<gobby_home>/bin/gmcp", "args": [], "type": "stdio"}`, and other
  servers stay. When the file is a symlink the write goes to its target, as
  `configure_mcp_server_json(..., extra_server_fields={"type": "stdio"}, follow_provider_symlink=True)`
  does.
- Verification as `_verify_agy_hook_registration`: `agy -p /hooks --output-format json`
  (10 s). Install prints the verified events, or the error. A failed check is
  reported and does not fail install, as in Python.
- No home trust is seeded (D17).
- Golden: `agy_golden.json` holds input `hooks.json` and `mcp_config.json` with
  another named hook and server, and the expected files. The Python test sets up the
  home as 3.4's does, keeps `agy` off `PATH` so verification is skipped, runs
  `install_agy(project, mode="global")`, and compares the two files.

Research context:
- `install_agy` supports only global mode, takes `hook_timeout_seconds=120`, and also
  installs shared and CLI content and seeds home trust; D13 and D17 exclude them.
- `src/gobby/adapters/agy_contract.py::AGY_GOBBY_HOOK_NAME` is `gobby`.
- Planned checks: as 3.4.

**Acceptance:**

- 3.8.1 - Install over the golden's input writes the golden's agy hooks and `gmcp` entry, keeps the other named hook and server, and writes through a symlinked `mcp_config.json`, with no tripwire fired. test: `crates/gdaemon/tests/node_setup.rs::install_agy_hooks_match_python_golden`.
- 3.8.2 - Python's installer produces the agy golden. test: `tests/install/test_node_setup_golden.py::test_agy_golden_matches_python`.
- 3.8.3 - A stub `agy` whose `/hooks` lists the gobby hook is reported verified, and one that fails is reported without failing install. test: `crates/gdaemon/tests/node_setup.rs::install_agy_reports_hook_verification`.
- 3.8.4 - The crate-local agy template copy equals `src/gobby/install/agy/hooks-template.json` byte for byte. test: `crates/gdaemon/tests/node_setup.rs::agy_template_copy_matches_python`.
- 3.8.5 - `gdaemon` is one patch version above the executor's starting tip in `crates/gdaemon/Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 3.9 `gobby node install` starts the node service and waits for the hub [category: code] (depends: 3.8)
`kind: deliverable`

Targets:
- `crates/gdaemon/src/node_setup/mod.rs`
- `crates/gdaemon/src/node_setup/service.rs`
- `crates/gdaemon/tests/node_setup.rs`
- `crates/gdaemon/tests/node_setup_support/mod.rs`
- `crates/gdaemon/tests/fixtures/node_setup/com.gobby.node.plist`
- `crates/gdaemon/tests/fixtures/node_setup/gobby-node.service`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: bumps this leaf's binaries in `MANAGED_BIN_VERSION_PINS` (Release ownership)

Python surface replaced: none on a node. The old node setup had the operator run
`gdaemon serve` by hand; Python's service templates serve the local daemon (D9).

- macOS: `~/Library/LaunchAgents/com.gobby.node.plist` (0644) has `Label`
  `com.gobby.node`; `ProgramArguments` `<gobby_home>/bin/gdaemon`, `serve`;
  `EnvironmentVariables` with the install-time `PATH`, and `GOBBY_HOME` when it is
  set; `RunAtLoad` true; `KeepAlive` with `SuccessfulExit` false;
  `ThrottleInterval` 10; and `StandardOutPath` and `StandardErrorPath`
  `<gobby_home>/logs/node.log`. Install runs `launchctl bootout gui/<uid>/com.gobby.node`,
  ignoring a not-loaded result, then `launchctl bootstrap gui/<uid> <plist>`.
- Linux: `~/.config/systemd/user/gobby-node.service` (0644) has
  `ExecStart=<gobby_home>/bin/gdaemon serve`, `Environment=PATH=<install-time PATH>`
  and `GOBBY_HOME` when it is set, `Restart=on-failure`, `RestartSec=10`, and
  `WantedBy=default.target`, with no `ProtectSystem` (D9). Install runs
  `systemctl --user daemon-reload`, `systemctl --user enable gobby-node.service` and
  `systemctl --user restart gobby-node.service`. When `systemctl --user` fails
  because no user manager runs, install refuses, naming the unit path and the
  commands. When `loginctl show-user <user> --property=Linger` reports `no`, it
  prints `loginctl enable-linger <user>`.
- Both files are written through `replace_file`, so a re-run rewrites them and
  restarts the service with the current `PATH`.
- Wait: install polls `GET <hub>/api/machines` with the node's key through enroll's
  pinned client every 2 s until this `machine_id`'s row shows `connected: true`,
  then prints "Node <hostname> is connected to <hub> at gdaemon <version>." It
  waits up to 120 s for the row to show either a connection or a refusal. While the
  row shows a refusal and a non-null `target_release`, the node is moving to that
  target (D7), so install prints "Moving to gdaemon <target>" once and keeps
  waiting, up to 600 s from the start. D21's debug overrides shorten both bounds.
  Past a bound, or on a refusal with no target, it prints the row's `refusal`, the
  log path and the command that shows the service's state, and exits 1.
- The harness gains stub `launchctl`, `systemctl` and `loginctl` scripts that record
  their arguments, and the stub hub answers `/api/machines`. Each OS's test is
  `#[cfg(target_os = ...)]`. The fixtures write the home and `PATH` as `$HOME`,
  `$GOBBY_HOME` and `$PATH`.

Research context:
- `src/gobby/install/shared/services/com.gobby.daemon.plist.j2` uses `KeepAlive`
  with `SuccessfulExit` false and `ThrottleInterval` 10;
  `gobby-daemon.service.j2` uses `Restart=on-failure` and `ProtectSystem=strict`.
- `crates/gdaemon/src/nodes/machines_api.rs::list` returns the caller's machines with
  `connected` from the registry. Rows gain `refusal` in 4.1 and `target_release` in
  4.2; until then a production row has neither and the wait ends on `connected`
  alone. The stub hub serves both fields, so this leaf's tests cover the moving case.
- A bundle download is at most 256 MiB (3.1), and launchd's `ThrottleInterval` and
  systemd's `RestartSec` are 10 s, so 600 s covers a download, a promotion and a
  restart on a slow link.
- Planned checks: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-daemon -E 'binary(node_setup)'`.

**Acceptance:**

- 3.9.1 - On macOS, install writes the plist fixture's content for the sandbox, boots it out and in through `launchctl`, and returns once the stub hub lists the machine connected, with no tripwire fired. test: `crates/gdaemon/tests/node_setup.rs::install_loads_launch_agent_and_waits_without_python`.
- 3.9.2 - On Linux, install writes the unit fixture's content, reloads, enables and restarts it through `systemctl --user`, prints the linger command when lingering is off, and returns once the stub hub lists the machine connected, with no tripwire fired. test: `crates/gdaemon/tests/node_setup.rs::install_starts_systemd_unit_and_waits_without_python`.
- 3.9.3 - While the stub hub's row shows a refusal with a target, install prints that the node is moving to it and keeps waiting past the first bound, and it returns once the row shows the machine connected. test: `crates/gdaemon/tests/node_setup.rs::install_waits_while_node_moves_to_target`.
- 3.9.4 - When the hub never lists the node connected, or refuses it with no target, install exits 1 after the bound and prints the refusal the hub reports and the log path. test: `crates/gdaemon/tests/node_setup.rs::install_reports_unconnected_node`.
- 3.9.5 - The Overview's install command runs whole in one sandbox, from bundle and enrollment through SRT, the five CLIs and the service, and leaves a node home that `NodeServices::prepare` accepts, with no tripwire fired. test: `crates/gdaemon/tests/node_setup.rs::documented_node_install_runs_without_python`.
- 3.9.6 - `gdaemon` is one patch version above the executor's starting tip in `crates/gdaemon/Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

## P4: Hub-driven updates and version skew
`kind: framing`

The handshake settles skew (D6), the hub resolves the target (D5), the node stages
and activates it (D7), and the operator can drive it from any window (D4). Channel
frames are JSON text frames: node frames carry `kind`, hub frames carry `type`, as
hello and ack do today.

### 4.1 The handshake carries the contract and the hub refuses skew both ways [category: code] (depends: 3.9)
`kind: deliverable`

Targets:
- `crates/gdaemon/src/nodes/channel.rs::Hello`
- `crates/gdaemon/src/nodes/channel.rs::hello_version`
- `crates/gdaemon/src/nodes/channel.rs::hub_channel`
- `crates/gdaemon/src/nodes/channel.rs::connect_client`
- `crates/gdaemon/src/nodes/release_frames.rs`
- `crates/gdaemon/src/nodes/release_frames/tests.rs`
- `crates/gdaemon/src/nodes/mod.rs::*` — scope-reason: declares the new `release_frames` module
- `crates/gdaemon/src/nodes/registry.rs::Registry`
- `crates/gdaemon/src/nodes/machines_api.rs::machine`
- `crates/gdaemon/tests/node_release.rs`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: bumps this leaf's binaries in `MANAGED_BIN_VERSION_PINS` (Release ownership)

Python surface replaced: none; the channel is Rust on both ends.

- The node's hello gains `contract`, its `node_commands::CONTRACT_VERSION`. `Hello`
  reads it as an optional field, and `hello_version` becomes a parser that returns
  the whole hello.
- `release_frames.rs::admit(hub, node)` admits when the two contracts are equal and
  otherwise returns a `Refusal` with `reason` (D6), both contracts and `message`. The
  message names both gdaemon versions and both contracts, says which side is older,
  and gives the next step:
  - `node_too_old` with a target: "The node moves to gdaemon <target>
    automatically." For a hello with no contract: "Re-run the node install command
    (docs/guides/node-setup.md) on <hostname>."
  - `hub_too_old` with a target: "Upgrade the hub first to keep this node on gdaemon
    <version>; until then the node moves down to gdaemon <target>."
  - Either reason with no target: "No published gdaemon release speaks contract
    <hub contract> yet; publish one (docs/guides/release-guide.md)."

  4.2 fills the target; until then a refusal carries `target_release: null`.
- `hub_channel` runs `admit` after reading the hello and before registering. On a
  refusal it sends `{"type": "refuse", "reason", "hub_contract", "node_contract",
  "target_release", "message"}`, closes with 4426, records
  `{reason, message, node_version, at}` in the registry's refusal map under the
  machine, and writes nothing to `machines`. Admission clears the machine's entry.
- `connect_client` logs a `refuse` frame's message at warn level, and `run_client`
  keeps its backoff (4.3 acts on it).
- `machine()` adds `refusal` (`null` or `{reason, message, at}`) to each row.
- `tests/node_release.rs` drives the hub with a raw websocket client that sends
  crafted hellos, using the hub fixtures `crates/gdaemon/tests/nodes.rs` uses.

Research context:
- `crates/gdaemon/src/nodes/channel.rs::hub_channel` reads the hello, registers,
  persists the heartbeat, checks the key and acks; it knows the machine from the
  key before the hello. `Hello` has no `deny_unknown_fields`. `channel.rs` is 418
  lines.
- `crates/gdaemon/src/nodes/registry.rs::Registry` holds a close sender per
  connection and nothing else per machine.
- Planned checks: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-daemon -E 'binary(node_release) | binary(nodes) | test(release_frames::)'`.

**Acceptance:**

- 4.1.1 - A hello whose contract is below the hub's is refused with `node_too_old`, close code 4426, both contracts and a message naming both gdaemon versions, and the machine is neither registered nor heartbeated. test: `crates/gdaemon/tests/node_release.rs::hub_refuses_node_too_old`.
- 4.1.2 - A hello whose contract is above the hub's is refused with `hub_too_old` and a message telling the operator to upgrade the hub first. test: `crates/gdaemon/tests/node_release.rs::hub_refuses_hub_too_old`.
- 4.1.3 - A hello without a contract is refused as `node_too_old` with the re-install instruction. test: `crates/gdaemon/tests/node_release.rs::hub_refuses_node_without_contract`.
- 4.1.4 - `admit` admits exactly when the contracts are equal, names `node_too_old` below and `hub_too_old` above, and with no target gives the no-published-release message. test: `crates/gdaemon/src/nodes/release_frames/tests.rs::admit_follows_contract_rule`.
- 4.1.5 - `GET /api/machines` shows a refused machine's refusal, and a later admission clears it. test: `crates/gdaemon/tests/node_release.rs::machines_api_reports_refusal`.
- 4.1.6 - `gdaemon` is one patch version above the executor's starting tip in `crates/gdaemon/Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 4.2 The hub resolves the release target [category: code] (depends: 4.1)
`kind: deliverable`

Targets:
- `crates/gdaemon/src/nodes/release_target.rs`
- `crates/gdaemon/src/nodes/release_target/tests.rs`
- `crates/gdaemon/src/nodes/mod.rs::*` — scope-reason: declares the new `release_target` module and adds the `HubNodes` field
- `crates/gdaemon/src/serve.rs::*` — scope-reason: the `HubNodes` literal in the Unix `run` gains the resolver; the file has two `run` variants
- `crates/gdaemon/tests/nodes.rs::channel_door`
- `crates/gdaemon/src/nodes/channel.rs::send_ack`
- `crates/gdaemon/src/nodes/channel.rs::hub_channel`
- `crates/gdaemon/src/nodes/release_frames.rs`
- `crates/gdaemon/src/nodes/machines_api.rs::machine`
- `crates/gdaemon/tests/node_release.rs`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: bumps this leaf's binaries in `MANAGED_BIN_VERSION_PINS` (Release ownership)

Python surface replaced: none (D5).

- `ReleaseTarget`, a `HubNodes` field: `current()` returns the cached target and
  refreshes it in a blocking task when it is older than 10 minutes; `refresh()`
  refreshes now. Resolution walks `release::published_releases()` newest first,
  skips versions above the hub's `CARGO_PKG_VERSION`, and takes the first whose
  `release::fetch_node_release` verifies and whose `contract` equals the hub's
  `node_commands::CONTRACT_VERSION`. A release without a valid signed manifest is
  skipped. A
  failed refresh keeps the last good target and logs a warning; with none, the
  target is `null`.
- The ack becomes `{"type": "ack", "machine_id", "target_release"}`, the refuse
  frame's `target_release` is filled, and its message uses it.
- `machine()` adds `target_release` to each row.
- Tests serve a fake GitHub API and release assets locally through D21's overrides.

Research context:
- `crates/gdaemon/src/serve.rs::run` builds `HubNodes` with `db`, `registry` and
  `timing`; `crates/gdaemon/tests/nodes.rs::channel_door` builds one too.
- GitHub allows 60 unauthenticated API requests an hour per address; a 10-minute
  cache spends at most six, plus operator-forced refreshes.
- Planned checks: as 4.1, plus `-E 'test(release_target::)'`.

**Acceptance:**

- 4.2.1 - With releases above, equal to and below the hub's version, the target is the highest one not above the hub whose contract equals the hub's, a release without a valid signed manifest is skipped, and with no such release the target is null. test: `crates/gdaemon/src/nodes/release_target/tests.rs::target_is_newest_admitted_release_not_above_hub`.
- 4.2.2 - A failed refresh keeps the last good target, and the cache refreshes after 10 minutes. test: `crates/gdaemon/src/nodes/release_target/tests.rs::failed_refresh_keeps_last_target`.
- 4.2.3 - The ack and the refuse frame carry the target, and `GET /api/machines` rows show it. test: `crates/gdaemon/tests/node_release.rs::ack_and_refusal_carry_target`.
- 4.2.4 - `gdaemon` is one patch version above the executor's starting tip in `crates/gdaemon/Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 4.3 The node stages the target and activates it when idle [category: code] (depends: 4.2)
`kind: deliverable`

Targets:
- `crates/gdaemon/src/node_setup/mod.rs`
- `crates/gdaemon/src/node_setup/update.rs`
- `crates/gdaemon/src/node_setup/update/tests.rs`
- `crates/gdaemon/src/nodes/channel.rs::connect_client`
- `crates/gdaemon/src/nodes/channel.rs::run_client`
- `crates/gdaemon/src/nodes/release_frames.rs`
- `crates/gdaemon/src/serve.rs::*` — scope-reason: `shutdown_signal` also waits for a restart request, which new `request_restart` and `restart_requested` expose
- `crates/gdaemon/src/main.rs::main`
- `crates/gdaemon/tests/node_release.rs`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: bumps this leaf's binaries in `MANAGED_BIN_VERSION_PINS` (Release ownership)

Python surface replaced: the Python cutover that promoted a node's binaries
(`promote_workspace_binary_set`) as an update path (D7).

- Triggers: the ack's `target_release`, a refuse frame's, and a
  `{"type": "release", "target_release"}` frame. A target that differs from the
  running `CARGO_PKG_VERSION` is staged unless `<bin>/.staged-<target>/` already
  holds a verified set.
- Staging runs in a blocking task so heartbeats continue:
  `release::download_bundle(target, <bin>/.staged-<target>)`, then 3.2's identity
  probe on the staged `gdaemon`, `gcode` and `ghook`. Older `.staged-*`
  directories are removed. Success sends
  `{"kind": "release_status", "staged": "<target>", "error": null}`; failure sends
  `"staged": null` with the error, leaves no staging directory, and the next trigger
  retries.
- Activation (D7): with a verified `.staged-<target>/` for the current target, the
  node takes the `NodeServices.agents` state lock and checks
  `AgentState::live_and_accepted() == 0`. It checks when staging completes, at each
  `run_client` ping and at each refusal. It holds the lock through promotion and the
  restart request, so no run is accepted in between.
  - Idle: `binaries::promote_set` runs from the staged directory and writes the
    stamp and `.node-release.json`. The node then removes the staged directory, logs
    "activating gdaemon <target>; exiting for the service to restart", and calls
    `serve::request_restart`. `serve`'s graceful shutdown completes and `main` exits
    with status 75.
  - A live run: the node keeps the staged set and its release. A refused node keeps
    reconnecting with its backoff.
  - A promotion failure sends `release_status` with the error when a channel is up,
    keeps `.staged-<target>` and the running process, and the next check retries.
    The retry promotes every member again, which repairs a set the failure left
    mixed.

**Granularity:** one leaf with seven acceptance items. Staging and activation are
one node-side lifecycle (stage, verify, gate, promote, restart), and each item tests
one of its transitions or failures.

Research context:
- `crates/gdaemon/src/serve.rs::shutdown_signal` waits for ctrl-c or SIGTERM; a
  clean `serve` returns `Ok(())`, and launchd and systemd restart only a non-zero
  exit under D9's units.
- `crates/gdaemon/src/nodes/channel.rs::run_client` takes `Arc<NodeServices>`, which
  knows the bootstrap path and so the home, and pings every
  `ChannelTiming::ping_interval` (30 s).
- `crates/gdaemon/src/state.rs::NodeServices` holds `agents: Arc<NodeAgents>`.
  `crates/gdaemon/src/nodes/agents/state.rs::AgentState::live_and_accepted` counts
  every run record, accepted, preparing or launched.
- Planned checks: as 4.1, plus `-E 'test(node_setup::update::)'`.

**Acceptance:**

- 4.3.1 - An ack naming a newer target makes the node download, verify and stage it from a fake release and report it staged, while heartbeats continue. test: `crates/gdaemon/tests/node_release.rs::node_stages_target_from_ack`.
- 4.3.2 - With no run in its agent state, the node promotes the staged target to new inodes with the stamp and `.node-release.json` once staging completes, and `gdaemon serve` exits with status 75. test: `crates/gdaemon/tests/node_release.rs::node_activates_staged_release_and_exits_75`.
- 4.3.3 - A target older than the running release is staged and activated the same way. test: `crates/gdaemon/tests/node_release.rs::node_downgrades_to_older_target`.
- 4.3.4 - A failed download or verification reports the error, leaves no staging directory and keeps the current set running. test: `crates/gdaemon/tests/node_release.rs::node_reports_failed_staging`.
- 4.3.5 - A refused node stages the refusal's target and activates without a channel, while the hub's `agent_runs` still lists a run on the machine as running. test: `crates/gdaemon/tests/node_release.rs::refused_node_converges_to_target`.
- 4.3.6 - Activation waits while the agent state holds an accepted, preparing or launched run, and proceeds at the first check after the state empties; a failed promotion keeps `.staged-<target>`, and the next check promotes it. test: `crates/gdaemon/src/node_setup/update/tests.rs::activation_waits_for_idle_state_and_retries_failed_promotion`.
- 4.3.7 - `gdaemon` is one patch version above the executor's starting tip in `crates/gdaemon/Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 4.4 The hub drives staging [category: code] (depends: 4.3)
`kind: deliverable`

Targets:
- `crates/gdaemon/src/nodes/channel.rs::hub_channel`
- `crates/gdaemon/src/nodes/registry.rs::Registry`
- `crates/gdaemon/src/nodes/release_frames.rs`
- `crates/gdaemon/src/nodes/release_target.rs`
- `crates/gdaemon/src/nodes/update_api.rs`
- `crates/gdaemon/src/nodes/mod.rs::*` — scope-reason: declares the new `update_api` module
- `crates/gdaemon/src/nodes/machines_api.rs::router`
- `crates/gdaemon/src/nodes/machines_api.rs::is_route`
- `crates/gdaemon/src/nodes/machines_api.rs::machine`
- `crates/gdaemon/src/serve.rs::*` — scope-reason: the Unix `run` starts the target refresh task beside `HubNodes`
- `crates/gdaemon/tests/node_release.rs`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: bumps this leaf's binaries in `MANAGED_BIN_VERSION_PINS` (Release ownership)

Python surface replaced: none (D5, D7).

- Hub frames to a live node go through the per-connection outbound sender
  (Constraints).
- A `release_status` frame records the staged release or error in the registry, and
  `machine()` adds `staged_release` and `release_error` to each row. The hub sends
  no activation word; the node activates on its own agent state (D7).
- A task started beside `HubNodes` refreshes the target every 10 minutes. When the
  target changes, each live node whose `node_version` differs gets
  `{"type": "release", "target_release": "<target>"}`.
- `POST /api/nodes/update` takes `{"machine": <ref>}` or `{"all": true}`; any other
  body is 400. It calls `refresh()`, then reports per owned machine selected:
  `sent` (live, version differs, frame sent), `current`, `offline`, or `no_target`.
  The response is `{"target_release", "machines": [{"ref", "hostname", "outcome"}]}`.
  A ref the caller does not own is 404 `machine_not_owned` with nothing sent.
  `machines_api.rs::router` and `::is_route` mount it.

Research context:
- `crates/gdaemon/src/nodes/machines_api.rs::list` scopes rows to the caller's
  `owner_user_id`; `machine()` builds each row.
- Planned checks: as 4.1.

Consumers unchanged:
- `crates/gdaemon/src/front_door/mod.rs` — no-edit-reason: mounts `router` and dispatches every `is_route` path to `handle`, so the new route needs no front-door change.

**Acceptance:**

- 4.4.1 - A target change sends `release` to each live node whose version differs and to no other. test: `crates/gdaemon/tests/node_release.rs::target_change_sends_release_frames`.
- 4.4.2 - `POST /api/nodes/update` for one machine or all reports `sent`, `current`, `offline` or `no_target` per owned machine, and a machine the caller does not own is refused with nothing sent. test: `crates/gdaemon/tests/node_release.rs::update_api_drives_owned_machines_only`.
- 4.4.3 - `GET /api/machines` rows show the staged release and any staging error. test: `crates/gdaemon/tests/node_release.rs::machines_api_reports_staged_release`.
- 4.4.4 - `gdaemon` is one patch version above the executor's starting tip in `crates/gdaemon/Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 4.5 `gobby node list` and `gobby node update` [category: code] (depends: 4.4)
`kind: deliverable`

Targets:
- `crates/gclient/src/command/node.rs`
- `crates/gclient/src/command/verbs.rs::Action`
- `crates/gclient/src/command/verbs.rs::Action::parse`
- `crates/gclient/src/command/verbs.rs::Action::run`
- `crates/gclient/src/command/help.rs::VERBS`
- `crates/gclient/tests/node_verbs.rs`
- `crates/gclient/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: bumps this leaf's binaries in `MANAGED_BIN_VERSION_PINS` (Release ownership)

Python surface replaced: none; Python had no machine list or node update command.

- `node list [--json]` calls `GET /api/machines` and prints REF, HOST, OS, GDAEMON,
  TARGET, STAGED and STATE (`connected`, `offline`, or `refused: <reason>`);
  `--json` prints the response.
- `node update (--machine REF | --all) [--json]` calls `POST /api/nodes/update` and
  prints the target and each machine's outcome. Neither or both flags is a usage
  error (exit 2).
- Both resolve the daemon URL and key like every verb, so the hub's own daemon
  answers on the hub and a node's front door relays to the hub (D4).
- `help.rs::VERBS`' `node` entry gains both forms.

Research context:
- `crates/gclient/src/command/verbs.rs` is 628 lines; the HTTP calls and rendering
  live in `node.rs`.
- Planned checks: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-client -E 'binary(node_verbs)'`.

Consumers unchanged:
- `crates/gclient/src/command/tests.rs` — no-edit-reason: its parse helper calls `Action::parse` with an unchanged signature; the new verbs' tests live in `crates/gclient/tests/node_verbs.rs`.

**Acceptance:**

- 4.5.1 - `gclient node list` renders each machine's release, target, staged release and state from a fake daemon's `/api/machines`, and `--json` prints the response. test: `crates/gclient/tests/node_verbs.rs::node_list_renders_machines`.
- 4.5.2 - `gclient node update --machine <ref>` and `--all` post the matching body and print each outcome, and neither or both flags is a usage error. test: `crates/gclient/tests/node_verbs.rs::node_update_posts_selection`.
- 4.5.3 - `gclient` is one patch version above the executor's starting tip in `crates/gclient/Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

## P5: Documentation
`kind: framing`

One leaf documents the node from a fresh laptop to updates, and the release order
that publishes it.

### 5.1 Node setup, release and CLI docs [category: docs] (depends: 2.5, 4.5)
`kind: deliverable`

Targets:
- `docs/guides/node-setup.md`
- `docs/guides/shared-stack.md`
- `docs/guides/release-guide.md`
- `docs/guides/system-requirements.md`
- `docs/guides/cli-commands.md`
- `docs/guides/gclient-user-guide.md`
- `docs/guides/configuration.md`
- `docs/guides/admin-operations.md`
- `docs/guides/hub-install-contract.md`
- `docs/guides/http-endpoints.md`
- `docs/guides/gterminal-development-guide.md`
- `docs/contracts/secrets.md`
- `crates/gdaemon/tests/node_setup.rs`

Python surface replaced: the Python node setup in `shared-stack.md` (Client setup),
which writes a DSN, copies `.secret_kek`, and runs `gobby auth login` and
`gobby install`.

- `node-setup.md` is the node guide: requirements (macOS or Linux on the four
  targets, git, Node.js 22.12.0 or later and npm, `curl` and `tar`; no Python, Rust,
  Zig or Docker); the Overview's three commands with the prompts they show; what
  install changes for each CLI; the service and its log; the `PATH` line; updates,
  refusals and both skew directions with their messages; `gobby node list` and
  `gobby node update`; recovery by re-running the install command; the channel
  frames (hello `contract`, ack, refuse, release, release_status); and the rule
  that a contract change is published before the hub cuts over (D6).
- `shared-stack.md`'s Client setup points at `node-setup.md`.
- `release-guide.md` adds signing (#24717's key, the `release` environment), the gmcp
  workflow, and the tag order: every changed member first, gdaemon last, because the
  gdaemon job bundles the members' published releases. The first publication pushes
  `gterm-v0.1.12`, which has never been tagged. An ordered step for a change to
  `node_commands::CONTRACT_VERSION` publishes the gdaemon release carrying it before
  `gobby cutover` moves the hub to it, because the hub targets only published
  releases at its own contract (D6).
- `cli-commands.md` and `gclient-user-guide.md` document the four `gobby node` verbs
  and D11's `gclient` note, and drop the login entry. Every other page names
  `gobby node enroll` where it named `gobby auth login`; `http-endpoints.md` shows
  the new guidance text and `POST /api/nodes/update`.
- `crates/gdaemon/tests/node_setup.rs::no_doc_names_python_login` walks
  `docs/**/*.md` from `CARGO_MANIFEST_DIR` with `std::fs`, skipping `docs/audits/`,
  `docs/research/` and the generated `docs/contracts/parity-ledger.md`, whose dropped
  row keeps the surface key (2.5), and fails on `gobby auth login`. It is Rust (No new
  Python), beside the test that runs this guide's install command.

Research context:
- Sites: `shared-stack.md` 102 to 105, `system-requirements.md` 235 to 246,
  `cli-commands.md` 463, 505 and 837, `configuration.md` 271 to 280,
  `admin-operations.md` 60, `hub-install-contract.md` 86, `http-endpoints.md` 50 and
  88, `gterminal-development-guide.md` 354, and `secrets.md` 27. `docs/audits/` and
  `docs/research/` are historical and stay.
- Planned checks: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-daemon -E 'binary(node_setup)'`.

**Acceptance:**

- 5.1.1 - The node guide documents the requirements, the three commands, the service, updates, both skew directions and the two operator verbs, with no Python step. file: `docs/guides/node-setup.md`.
- 5.1.2 - The release guide documents signing, the gmcp workflow and the member-first, gdaemon-last tag order. file: `docs/guides/release-guide.md`.
- 5.1.3 - The CLI reference documents the four `gobby node` verbs and has no `gobby auth login` entry. file: `docs/guides/cli-commands.md`.
- 5.1.4 - The documented install command is the one the sandbox runs without Python. test: `crates/gdaemon/tests/node_setup.rs::documented_node_install_runs_without_python`.
- 5.1.5 - No Markdown page under `docs/` outside `docs/audits/`, `docs/research/` and the generated parity ledger names `gobby auth login`. test: `crates/gdaemon/tests/node_setup.rs::no_doc_names_python_login`.
- 5.1.6 - The release guide orders a contract change as publish first, then hub cutover, and the node guide states the same rule with the no-published-release refusal. file: `docs/guides/release-guide.md`.

## E1 End-to-end verification
`kind: verification`

These are completion gates for the implementation. Except for plan validation
(`uv run gobby plans validate .gobby/plans/node-install-enrollment.md -p /Users/josh/Projects/gobby`),
none has run yet. Each must hold on `0.5.0` before #24657 closes:

1. `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-daemon -p gobby-core -p gobby-code -p gobby-hooks -p gobby-client -p gobby-mcp`
   must pass, skew in both directions included (4.1, 4.3). The
   `release_scripts.rs` tests must run rather than return early, so this gate
   runs where `jq`, OpenSSL 3 and `sha256sum` are on `PATH`: CI's Ubuntu runner, or a
   Mac with Homebrew's `openssl@3/bin` and `coreutils/libexec/gnubin` first on
   `PATH`.
2. `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/install/ tests/cli/test_auth_login.py tests/e2e/test_auth_login_e2e.py tests/e2e/test_hub_node_pair.py tests/skills/test_reference_library.py tests/test_parity_ledger.py`
   must pass. Gate 1 runs the release-script and docs tests (1.1, 1.3, 5.1).
3. Publication (D22): after the last bumping leaf lands and #24717 closes, Josh
   pushes the tags in the release guide's order: `gcode`, `ghook`, `gmcp`, `gclient`
   and `gterm` (`gterm-v0.1.12`, never tagged before), then `gdaemon` last. Each
   member release must carry
   `SHA256SUMS` and `SHA256SUMS.sig`, and the gdaemon release must carry the four
   bundles and the signed `node-release.json`. Gates 4 and 5 wait for this one.
4. On one of Josh's Macs with no Python, Rust or Zig on `PATH`, the Overview's three
   commands must enroll the node, start `com.gobby.node`, and leave it listed as
   connected at the hub's target by `gobby node list` in a gclient window on the hub.
   `~/.gobby/tools/srt/0.0.79` must pass the node's verifier, and each of the five
   CLIs present must carry Gobby hooks and the `gmcp` entry.
5. After the hub moves to a newer published release, that node must move to it
   without operator action once it runs no agents, and
   `gobby node update --machine <ref>` run in a gclient window on another node must
   drive the same update.

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
- 2026-10-18: Orchestrator #15977 (12:30 CDT) rejected the committed registry
  tarball and accepted D5 to D9 and D11 as written. D10 now composes a minimal
  tarball at test time, and 1.6 splits the SRT step into `fetch_tarball` and
  `install_from_tarball`. Verb-level tests seed a verified install, and one opt-in
  test proves the registry fetch. Re-scoping those bare targets is part of
  expansion if their files land first.
- 2026-10-18: Renewed consensus between Plan Writer gobby#16767 and Plan Adversary
  gobby#16993 on the D10 repair at 91f3f31, with R1 folded in: 1.6.3 and 1.6.6 run
  on an empty SRT home, not the seeded install. The stale M1 is withdrawn for a fresh
  derivation.
- 2026-10-18: Full rework by Plan Writer gobby#16767 under Orchestrator #15977's
  orders (13:03 to 13:31 CDT) for Josh's developer experience: published signed
  releases with gmcp and node bundles (P1), `gobby node` verbs (P2), install with a
  service and all five CLIs (P3), and hub-driven updates with skew handled both ways
  (P4). Sections are renumbered and decisions recorded as D1 to D23. The stale M1
  stays until consensus.
- 2026-10-18: Revision by Plan Writer gobby#16767 for Plan Adversary gobby#16993's
  review of 0f9fa1b and Orchestrator #15977's rulings (15:11 and 15:13 CDT). D6
  admits on one equal contract integer, and a contract change is published before
  the hub cuts over. D7 gates activation on the node's own agent state, so the
  `activate` frame and the hub's `agent_runs` check go. 3.9 waits while a new node
  moves to the hub's target. The release scripts are bash with Rust tests, and the
  docs guard is Rust. D22 records that every member release at gate 3, `gterm-v0.1.12`
  included, is a first publication. D4, D8, D15 and D22 carry #15977's affirmation.
  The stale M1 stays until consensus.
- 2026-10-18: Revision by Plan Writer gobby#16767 for Plan Adversary gobby#16993's
  round-2 gate and Orchestrator #15977's rulings (16:16 and 16:19 CDT). D24 keeps
  `tests/install/test_node_setup_golden.py` and drops `gcode_versions.py`. 2.4 depends
  on #24719 (Derive the gcode prune-budget floor from the managed gcode pin) and
  edits neither the floor nor `gcode_gateway.py`. E1 gate 1 requires `jq` on `PATH` as well, and
  1.3's tests return early without it. The stale M1 stays until consensus.

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
  validation_criteria: '1.6.1: `install_from_tarball` stages the composed minimal
    tarball with stub `node` and `npm`, and #24374''s verifier accepts the result.
    test: `crates/gdaemon/tests/node_setup.rs::srt_installs_from_minimal_tarball`.

    1.6.2: The verb reuses a seeded verified install without running `npm`, with no
    tripwire fired. test: `crates/gdaemon/tests/node_setup.rs::install_reuses_verified_srt_without_python`.

    1.6.3: The verb refuses a tarball off its pin (the composed one), Node older than
    22.12.0, or no `npm`, leaving no staging directory and no install, with no tripwire
    fired. test: `crates/gdaemon/tests/node_setup.rs::install_refuses_srt_off_pin`.

    1.6.4: The Rust pins, receipt fields and patch table equal the Python ones (read
    with `include_str!`), the lockfile and runner copies equal their Python originals
    byte for byte, and the embedded lockfile and runner match their pins. test: `crates/gdaemon/tests/node_setup.rs::srt_pins_match_python`.

    1.6.5: Patches apply once, skip a patched file, and refuse unknown bytes; a failed
    promotion restores the previous install. test: `crates/gdaemon/src/node_setup/srt/tests.rs::patches_and_promotion_follow_python`.

    1.6.6: Run as the opt-in check, with network and Node.js 22.12.0 or later on `PATH`
    after the tripwires, the verb fetches the registry tarball with `npm pack`, matches
    its pin, and #24374''s verifier accepts the install, with no tripwire fired. test:
    `crates/gdaemon/tests/node_setup.rs::install_fetches_registry_srt`.

    1.6.7: `gdaemon` is one patch version above the executor''s starting tip in `crates/gdaemon/Cargo.toml`,
    `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.'
  labels:
  - covers:node-install-enrollment:1.6:1.6.1
  - covers:node-install-enrollment:1.6:1.6.2
  - covers:node-install-enrollment:1.6:1.6.3
  - covers:node-install-enrollment:1.6:1.6.4
  - covers:node-install-enrollment:1.6:1.6.5
  - covers:node-install-enrollment:1.6:1.6.6
  - covers:node-install-enrollment:1.6:1.6.7
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
