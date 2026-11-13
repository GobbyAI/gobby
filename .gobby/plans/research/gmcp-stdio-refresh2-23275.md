# #23275 second evidence refresh (Plan Writer gobby#16770, 2026-10-13)

Session-scoped working notes. `.gobby/plans/gmcp-stdio.md` is the authority.
Baseline: stamped M1 at `208d6318fe` (2026-10-09). Tip swept: `97b1bbe055`
(1,039 commits later). Assignment: Orchestrator gobby#15977; task moved to Stage 2
#21544 (Lane 2), planning only, implementation after Stage 1 #21543 closes.
Adversary: Grok seat gobby#16771 (cannot wait; after `plans validate` exits 0,
commit, then send Orchestrator + 16771 path, SHA, file hash). Enhancer pass spent
(label `enhancer-pass-spent`), so no enhancer step.

## Unchanged (verified)
- All 1.1-1.8 production symbols exist (read_api_key/_for, daemon_auth_headers,
  _AGENT_IDENTITY_ENV_HEADERS, HealthFamily/unavailable, ghook capture,
  isolation/codex/sandbox/install symbols, TestBridgeAutoStart, _NOT_DAEMON entries,
  get_bridge_tool_timeouts, NumberMapConfigField, native_bin_name).
- stdio_proxy/stdio_tools/stdio_server/wait_tools/instructions/bridge.py untouched.
- rmcp 3.5.1 still latest (crates.io 2026-10-13, rust_version 1.88, Apache-2.0);
  `gobby-mcp` name still free. Workspace members unchanged; tokio 1.50.0; ghook
  declares gobby-core `version = "0.10.0"` (gcore crate now 0.10.8, req unchanged).
- Python `_managed_envelope_api_token` renamed `read_managed_api_token` (#23923);
  plan never names it. Rust local_token.rs changes are test-only.
- GOBBY_MCP_WRAPPER_STALE literal lives in wait_tools.py (request_context calls it).

## Drift to apply
S1 Framing: #23275 now under Stage 2 #21544 (Lane 2). Overview "Lane 7 plans it in
   place" stale. Implementation waits for Stage 1 #21543.
S2 Roles: Program Director / Merge Manager no longer exist. Release Manager holds
   activation (rebuild+promote binaries) and heavy-run slots; reviewer LANDs via
   land_commit; Lane Manager releases closes; Orchestrator restarts/cutovers/pushes.
   Fix D1 (owner, D1.1/D1.2 text), 1.6 landing gate, Constraints landing order,
   D5 owner, D6 owner, deferral reasons ("PD operations").
S3 `.gobby/roles/_common.md` is GONE: Constraints "heavy work under ..." -> project
   operating policy (Release Manager slot, CARGO_BUILD_JOBS=3, cap 6).
S4 D3: #23519 CLOSED 2026-10-09. Deferral reason + Finalization step 3 (edge on
   #23519) stale; remaining gate is D3.5 decision + needs-planning hold.
S5 D4: #23274 (Stage 1, under #21555, lane-3) has all leaves closed incl. relay
   #24089 and pair test #24092 (node-channel 1.6.1 = api-keys D2.5,
   `tests/e2e/test_hub_node_pair.py::test_node_enrolls_and_relays`). Appending D4.1
   (gmcp on node) to #23274 would deadlock (Stage 1 task waiting on Stage 2 gmcp).
   Proposal: drop D4 + Finalization step 2; Parent D3 Mapping D2.5 -> delivered by
   #24092; add in-plan item 1.5.5: gmcp on enrolled node reaches hub via relay
   (reuse `pair_hub`/`pair_node` fixtures; node home bootstrap has daemon_port +
   api_key so gmcp's daemon_url/read_api_key resolve with GOBBY_HOME=node home).
   New target `tests/e2e/test_hub_node_pair.py::*`.
S6 D2 Finalization step 1 rationale stale (cycle via Stage 1 parent no longer
   true; both under #21544). Cited-parent route still validates
   (src/gobby/plans/deferral.py::_has_valid_cited_parent: parent not in #23275's
   dependency closure). New rationale: blocked-by would make the stdio plan wait
   for the whole flip.
S7 Task-graph flag for Orchestrator (not a plan edit): #23275 still `blocks`
   #21555 (Stage 1) while its parent #21544 waits on #21543 -> deadlock; also
   label `needs-planning`, escalated flag.
S8 NEW 1.7 consumers:
   - `scripts/generate_parity_ledger.py` (#24315, e07a749e68, today) imports
     `register_proxy_tools` from stdio_tools to emit `MCP stdio gobby:*` rows; also
     inventories `CLI gobby mcp-server`. Fix: read stdio tool names from
     `crates/gmcp/contracts/tools_list.v1.json`; sources sentence names `gmcp`;
     `docs/contracts/parity-ledger-status.json` gains
     `"CLI gobby mcp-server": "dropped by decision #23275"` (+ 12 stdio rows
     `delegated`?). Regenerate `docs/contracts/parity-ledger.md` (generated, do not
     edit). Test: `tests/test_parity_ledger.py` (asserts "MCP stdio
     gobby:init_project"; test_committed_ledger_matches_current_public_inventory).
     Status rules: proxied|delegated|dropped by decision #N; stale non-dropped keys raise.
   - `tests/workflows/test_active_progressive_discovery_guidance.py`
     `ACTIVE_GUIDANCE_FILES` lists stdio_tools.py -> read_text FileNotFoundError
     after delete. Swap for tools_list.v1.json.
   - `tests/terminals/test_no_direct_tmux_consumers.py` `_OWNED_CONSUMER_PATHS`
     lists mcp_proxy/stdio.py + stdio_server.py (stale, silently empty) -> drop.
   - `docs/reference-audit/mcp-servers.json` row gobby:init_project implementation
     `src/gobby/mcp_proxy/stdio_tools.py` `register_proxy_tools.init_project` ->
     point at gmcp `crates/gmcp/src/tools.rs` init_project handler (1.2 must name it).
   - `docs/guides/ghook-development-guide.md:301` names current_terminal_context.
   - Add search `gcode grep -F "mcp_proxy/stdio" src tests scripts` to 1.7 checks.
S9 NEW 1.5 consumer: `tests/servers/routes/mcp_endpoints/test_terminal_session_revival.py`
   (#24044) imports `_proxy_with_response, _success_response` from
   tests.mcp_proxy.test_stateless_stdio_proxy (deleted in 1.7) and patches
   `gobby.mcp_proxy.stdio_proxy.current_terminal_context`/`daemon_auth_headers` in
   the `mcp_after_hook` case of
   `test_expired_owner_recovers_only_from_live_session_traffic`. Build the wrapper
   headers directly (protocol header + X-Gobby-Terminal-Context JSON filtered to
   TERMINAL_CONTEXT_KEYS, no session header).
S10 1.5.3 test renamed: `tests/servers/test_mcp_execution_context.py` now has
   `test_run_identity_transport_chain` (imports DaemonProxy,
   DaemonProxyDependencies); `test_stdio_and_http_share_trusted_agent_identity`
   no longer exists.
S11 1.5.4 stale: #23923 keeps managed tokens in the envelope only. Test asserts
   `GOBBY_AGENT_API_TOKEN` NOT in child env and no override holds the token;
   child env has GOBBY_SESSION_ID, GOBBY_HOME, GOBBY_DAEMON_URL,
   GOBBY_MANAGED_EXECUTION_BOOTSTRAP. Reword 1.5.4.
S12 1.8 new render_srt_settings importers (from gobby.agents.srt_runtime):
   tests/agents/test_spawn_executor_providers.py, test_sandbox_policy.py,
   test_archivist_sandbox.py -> add to 1.8 Targets. Ranges now:
   render_srt_settings 547-610, prepare_sandbox_launch 628-885,
   gobby_read_exceptions 367-399, mcp_config_read_exceptions 414-442.
   Sizes: sandbox_policy.py 958, srt_runtime.py 956. docs/architecture/
   sandbox-hub-credential-isolation.md names gobby_read_exceptions() without path (no edit).
S13 1.7: `web/src/api/runtimeConfigCodecVectors.gen.ts` holds no tool_timeouts
   (unchanged since 2026-09-05) -> drop that Target and "web vectors" from 1.7.3.
S14 1.6: mcp_config_json.py now 189 lines (was 420; #24225 moved Claude writes to
   new `src/gobby/claude_config.py::update_claude_config`). isolation
   `_patch_mcp_config_for_isolation` now 230-292 and also writes the entry into
   ~/.claude.json projects via update_claude_config (gmcp entry flows there too).
   provider_mcp_config_error 295-331. spawn_executor_support.py 643 lines,
   install_setup.py 570, install_setup_gclient.py 342, isolation_repair.py 331.
S15 Decision 1: re-date rmcp/name recheck to 2026-10-13.
S16 Constraints: re-date consumer sweep to 97b1bbe055 (rg fallback, gcode index
   refresh under load).
S17 Changelog: add 2026-10-13 refresh entry (no consensus yet); withdraw stale M1
   (new items/targets make it stale) - adversary re-derives.
