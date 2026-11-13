# #24657 adversary round: fix plan (working context, Plan Writer gobby#16767)

Source: Plan Adversary #16993 message a7426a40-97a3-4405-8623-6bf6f353f618 on 0f9fa1b and
fc37936 (8 blocking, 5 nits). Orchestrator #15977 rulings, 15:11 CDT: D4, D8, D15 and D22
affirmed as written; no new Python files (bundle and signing tooling and tests go in bash
or Rust; a doc assertion may extend an existing Python docs test module only); retarget
hub-mode 1.5 to a new module; F4 is real (gterm-v0.1.12 release not found), so add a gterm
bump and publish leaf. Fix F1-F5 and H1-H2 and the nits, then send the revised plan to
#16993. Both plans keep their stale M1 untouched in the repair commits (route f5577ae0).

## node-install-enrollment.md

- F2 (take a): one `contract` integer, admit when equal. Remove `CONTRACT_MIN_VERSION`,
  `ContractRange` and the `crates/gdaemon/src/nodes/contract_range.rs` target from 2.1
  (and the `nodes/mod.rs::*` target that only declared it). release-info prints
  `{"release", "contract": <CONTRACT_VERSION>}`; `node-release.json` `contract` is an
  integer (1.3 step 3 and 4); the hello carries `contract`; the refuse frame carries both
  contracts. `node_too_old` when node < hub or the hello has no contract; `hub_too_old`
  when node > hub. Name `node_commands::CONTRACT_VERSION`, never the literal 4 (N1).
  Touch D5 ("contract range the hub admits" -> "contract equal to the hub's"), D6, 2.1,
  1.3, 4.1 (title says "contract range"), 4.1.4, 4.2.
- F1 (take a): node-local idle gate. The hub sends `activate` when the node's reported
  staged release equals the target, and resends it on each `release_status` and
  heartbeat (absorbs N2's retry). The node promotes only when
  `state.agents` (`crate::state`, `Arc<NodeAgents>`) shows
  `AgentState::live_and_accepted() == 0` (`crates/gdaemon/src/nodes/agents/state.rs`),
  rechecking on `NodeAgents.changed` (`Notify`). The hub SQL idle check goes (N3 moot).
  A refused node has no channel: it stages the target from the refusal and promotes on
  staged and locally idle; drop `activate` from the refuse frame. `.staged-<target>`
  persists until a full promotion succeeds (N2). 4.3.5 and 4.4.4 cases: hub DB still says
  running for a run that died with the old hub while the node is idle -> promotes; node with
  a live run waits, run exits, promotes. Update D7, 4.3, 4.4, 4.4.1, 4.4.4.
- F3: under equality every `CONTRACT_VERSION` bump is a release event. Decision in D6 (or
  D22): publish the gdaemon release carrying a new contract before the hub cutover. Ordered
  step in release-guide.md and node-setup.md, plus a 5.1 acceptance item. When the resolver
  finds no published release at the hub's contract, the target is null and the refusal
  message says "no published gdaemon release speaks contract N". No cutover preflight
  (that would be new Python).
- F5: 3.9 waits 120 s for a first reply (connected or refused); then, while the machine row
  shows a refusal with a target or a `staged_release`, it keeps waiting up to a stated
  convergence bound (debug override like D21's `GOBBY_NODE_CONNECT_TIMEOUT_SECS`). Past it,
  exit 1 printing the row's state. Widen 3.9.3 or add an item; recheck 3.9's Granularity.
  D3 gains "install's wait covers the convergence".
- F4 (#15977 correction, 15:13 CDT, supersedes the 15:11 bump-leaf ruling): NO bump leaf.
  gterm-v0.1.12 was never tagged or published (`git ls-remote` and `gh release list` from
  the main checkout). After 1.1 lands, the first gterm-v0.1.12 push publishes the signed
  release under D22's E1 gate 3. State that case in D22, gate 3's tag order (gterm before
  gdaemon) and the Release ownership note on gterm (no gobby-core dependency, no bump).
  gclient is also unpublished (`UNPUBLISHED_MANAGED_BINS`); its first release comes from
  the bumped version at gate 3.
- F6: no new Python. 1.1 `tests/scripts/test_sign_release.py` -> new
  `crates/gdaemon/tests/release_scripts.rs` (bare path) running the script and verifying
  with `openssl pkeyutl -verify`, skipping without OpenSSL 3 or `sha256sum`. 1.3
  `scripts/release/node_bundle.py` -> `scripts/release/node_bundle.sh` (bash, jq); its tests
  join `release_scripts.rs` with fake `gh` and `cargo` on PATH. Test paths under `tests`
  bump nothing (`workspace_source_identity` excludes them). Docs guard
  `tests/docs/test_node_setup_docs.py` (new) -> a test in an existing Python docs module
  (`tests/docs/` has test_gcode_guidance.py, test_memory_guides.py and others) or Rust.
  Update E1 gate 2's pytest list and 5.1.
- F7: record "#15977, 15:11 CDT: affirmed" on D4, D8, D15 and D22.
- After edits: revalidate the scratch narrative (cut at `grep -b '^## M1'`, `head -c off-1`),
  cmp the M1 tail with scratchpad old-m1.txt, `ocr delegate rule`, commit with -m.

## hub-mode.md

- H1: agy has no session-start hook (`AGY_HOOK_NAMES` = PreInvocation, PreToolUse,
  PostToolUse, PostInvocation, Stop). The 5.1 fake: claude, codex, droid and grok pipe
  session-start; agy begins with its PreInvocation envelope under `GOBBY_SESSION_ID`.
- H2: retarget 1.5's `server.py` edit to a new same-extension module (ruling). One body
  paragraph must contain `split` or `move`, name `server.py`, and name a new bare-path
  `.py` Target that does not exist yet, e.g. `src/gobby/servers/websocket/caller_binding.py`
  holding the bind-and-reset that `handle_connection` enters. #24595 is open with its
  code already landed at 780fdf4ce2; #15977 ruled the retarget anyway.
- N4: under "Relations that set no edge", one line: grok and agy settlement on a node needs
  the global hooks #24657 3.7 and 3.8 install.
- N5: 5.1 research: grok's and agy's hook attention rides #24150's forwarding and the hub's
  local handling, which 5.1 proves through claude's PermissionRequest; 5.1 proves their
  attention through the pane.
- Revalidate hub-mode scratch (M1 at `grep -b`), ocr, commit.

Then send both SHAs to #16993 (dbba79f4-38e1-4695-b1bb-ed2c1ca32aeb) and wait.
