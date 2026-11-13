# #23275 evidence refresh working notes (Plan Writer gobby#15860)

Session-scoped working notes for refreshing `.gobby/plans/gmcp-stdio.md`. The plan file is the authority.

Baseline: the plan's sweep ran at `6f5d0c8733` (2026-10-05). Consensus `fcc970aa76`, M1 `e4e60efccd`, Overview edit `a9d28a4c19` (#23695). Base and expansion validation pass at today's tip `ba4aa371f8`. Not registered, no child tasks.

Direction (LM7 relaying Adv1 gobby#15401): refresh evidence only, no redraft. Keep Decision 12's retirement, update its rationale, and retire the #23680 endpoint too. Adv1 delta-reviews the committed repair and re-derives M1.

## Drift found (edit the plan for each)

R1. Auth (1.1 research and implementation, 1.1.1, 1.1.2, 1.5.4).
- `crates/gcore/src/local_token.rs::read_local_cli_token` no longer exists. Use `read_api_key` / `read_api_key_for`: managed envelope (`GOBBY_MANAGED_EXECUTION_BOOTSTRAP`), then `GOBBY_AGENT_API_TOKEN`, then refuse when the bootstrap variable is set, then the bootstrap `api_key`. #23519's rewrite to the bootstrap key has landed in gcore.
- Python `src/gobby/utils/local_token.py::daemon_auth_headers` prefers `_managed_envelope_api_token` and adds `_AGENT_IDENTITY_ENV_HEADERS` only for an agent token (envelope or env). In gmcp, an agent token holds exactly when `GOBBY_MANAGED_EXECUTION_BOOTSTRAP` is set or `GOBBY_AGENT_API_TOKEN` is non-empty, so no gcore change is needed.
- `src/gobby/mcp_proxy/stdio_proxy.py::DaemonProxy._request` (lines 254-255) re-reads auth on every request while `GOBBY_MANAGED_EXECUTION_BOOTSTRAP` is set (capability renewal, #23865). Otherwise it caches the headers and re-reads once on a 401. Add an acceptance item for the per-request envelope re-read.
- The 1.5.4 test `tests/agents/test_spawn_executor.py::test_scrubbed_child_env_reaches_daemon_proxy_identity` now uses `signing_key=derive_managed_signing_key(...)` and a private `grant.json` envelope. Reword 1.5.4 to name the envelope.

R2. Decision 6 and 1.2 research: #23531 has landed (`f3d55136b7`). `src/gobby/servers/auth_service.py:95` admits agent tokens on `POST /api/mcp/bridge/ready`. Drop "until #23531 lands".

R3. Decision 12 and 1.7: #23680 (`f7ebca019a`) added `GET /api/mcp/bridge/tool-timeouts`. Add these to 1.7's Targets, research and acceptance:
- handler `src/gobby/servers/routes/mcp/endpoints/bridge.py::get_bridge_tool_timeouts` (`report_bridge_ready` stays);
- registration `src/gobby/servers/routes/mcp/tools.py:62`;
- agent route `src/gobby/servers/auth_service.py:96`;
- tests in `tests/servers/routes/mcp_endpoints/test_bridge.py`: `test_bridge_tool_timeouts_serve_the_active_config`, `test_bridge_tool_timeouts_wait_for_daemon_config`, `test_agent_tokens_may_read_bridge_tool_timeouts`;
- the docs row at `docs/guides/http-endpoints.md:299`;
- the fixture attribute at `tests/mcp_proxy/tools/tasks/test_lifecycle_close_orchestration.py:1535` (`tool_timeouts={}`), which would otherwise fail 1.7's `gcode grep -F "tool_timeouts" src tests web` no-hit check.

Rationale update: the route was added so the wrapper reads the map without hub I/O. The approved retirement still holds: gmcp keeps the 30 s default plus contract timeouts, and the setting, route and agent route leave together.

R4. 1.6 has a new launch site. `src/gobby/agents/grok_sandbox_home.py::_declare_gobby_mcp_server` (#23554, `6f56a8ec50`, 2026-10-07) writes `{"command": _resolved_gobby_mcp_command(), "args": ["mcp-server"]}` into the managed Grok `config.toml`. Its tests are in `tests/agents/test_grok_sandbox_home.py` (lines 61 and 78). Add both to 1.6, with an acceptance item.

R5. 1.6 latent miss. `tests/agents/test_spawn_executor.py::test_codex_terminal_direct_spawn` (around line 905) asserts the Codex override `uv run --no-sync --project /main/repo gobby mcp-server`. 1.6 changes `_codex_mcp_config_overrides`, so 1.6 must target it. 1.5 already targets that file, so 1.6 also depends on 1.5. The `.mcp.json` samples at lines ~2021 and ~2082 only feed the timeout environment; record them as no-edit.

R6. 1.2 research. `set_variable` gained `scope: "session" | "step"`, and the body is `{name, value, scope}`. The fixture capture picks it up. The line reference "stdio_proxy.py (lines 335 to 619)" is stale; name the methods instead.

R7. `EXTENDED_TIMEOUT_TOOL_NAMES` gained `land_commit` (#23726). The contract test does not depend on the values, so edit only where the text lists names.

R8. Recheck these numbers and line references:
- `sandbox_policy.py` is 940 lines (plan says 935) and `srt_runtime.py` is 932 (867);
- `spawn_executor_support.py` is 648 (599), `install_setup.py` is 569 (615) and `install_components.py` is 361 (367);
- the `isolation_repair.py` 225-235 reference, the `render_srt_settings` and `gobby_read_exceptions` line ranges, the status of #23273's `sandbox_credentials.py` split, and the `test_stateless_ambient_session.py` length (548).

R9. Constraints. Re-date the consumer sweep to today's tip, and add one V1 changelog entry for the refresh.

R10. Decision 1. Recheck that rmcp 3.5.0 is still the right pin (latest version and `rust_version`).

Unchanged: the wrapper header rules (stdio_proxy.py 218-251), the 1.4 install symbols, and the `mcp_config_*` sizes (221/203/420).
