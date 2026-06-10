# Review: mcp_proxy core (server / manager / transports / services)

- **Scope:** `src/gobby/mcp_proxy/` EXCLUDING `tools/` (reviewed separately in
  `mcp_proxy-tools.md`). Covers top-level modules (server.py, manager.py,
  registries.py, instructions.py, stdio.py, importer.py, semantic_search.py,
  schema_hash.py, metrics*.py, lazy.py, bundled.py, wait_tools.py,
  daemon_control.py, actions.py, models.py, _coerce_arguments.py,
  _call_tool_wrapper.py, connection_cleanup.py, session_bootstrap.py,
  server_list.py), `client_manager/`, `services/`, and `transports/`.
  ~11,350 lines.
- **Reviewer:** Claude Fable 5 (4 parallel review agents + independent Blocker
  verification by synthesizer)
- **Commit / branch:** `0bb436f4d` on `0.5.0` (working tree clean at review time)
- **Summary:** 3 Blocker · 32 Important · 30 Nit — the proxy's discovery and
  transport plumbing is functional but the seams are weak: the agent-facing
  server-management tools don't persist, schema validation is silently disabled
  for every external server by an envelope-shape mismatch, and the stdio/websocket
  transports leak subprocesses on connect timeout. Sync DB on the event loop is
  endemic.

All three Blockers were independently re-verified against source by the
synthesizer (producer + consumer + trigger path read directly).

## Findings

### Server & discovery surface

### [BLOCKER] `add_mcp_server` / `remove_mcp_server` MCP tools are non-durable — persistence is commented out
- **Where:** `src/gobby/mcp_proxy/services/server_mgmt.py:88-92` and `:131-135`; exposed via `src/gobby/mcp_proxy/server.py:355-373`
- **Failure mode:** The MCP-tool path routes through `ServerManagementService`, whose persistence calls are literally commented out: `# self._config_manager.add_mcp_server(...) # Mocking this interaction` (add, line 92) and `# self._config_manager.remove_mcp_server(name)` (remove, line 135). `add_server` only calls `mcp_manager.add_server_config()` (in-memory dict write, `client_manager/server_registry.py:287-296`); `remove_server` only calls `remove_server_config()`. The HTTP route and `actions.py` correctly use `MCPClientManager.add_server`/`remove_server`, which upsert/delete via `mcp_db_manager` (`server_registry.py:161-221`). A server added via the `add_mcp_server` tool reports "added successfully" but vanishes on daemon restart; a server removed via `remove_mcp_server` reports "removed" but resurrects on restart from its surviving DB row. The service path also skips name normalization and duplicate detection — `add_server_config` silently overwrites an existing same-name config where the persistent path raises `ValueError`.
- **Why it matters:** Silent data loss / zombie config on the primary agent-facing management surface; violates the DB-is-source-of-truth contract. `tests/mcp_proxy/test_server_mgmt.py` never asserts persistence.
- **Minimal fix:** Route `ServerManagementService.add_server`/`remove_server` through `self._mcp_manager.add_server(config)` / `remove_server(name)` (the persistent paths); delete the "Mocking this interaction" stubs; add a regression test asserting the DB upsert/remove is called.
- **Confidence:** high — verified independently by two review agents and the synthesizer.

### [BLOCKER] `get_tool_schema` returns a different shape for external servers, silently disabling call_tool validation, arg injection, and error enrichment for every external tool
- **Where:** `src/gobby/mcp_proxy/services/tool_execution.py:489-491` (producer) vs `:265-266` (and the same `.get("success")` checks at `:176-177`, `:399-400`); real external shape proven by `src/gobby/mcp_proxy/client_manager/tool_inventory.py:164-171`
- **Failure mode:** For internal registries, `get_tool_schema` returns `{"success": True, "tool": {...}}` (tool_execution.py:465-470). For external servers it returns the **raw** `inputSchema` dict from `manager.get_tool_input_schema()` — `tool_inventory.get_tool_input_schema` returns `_validated_input_schema(tool_info.get("inputSchema", {}))` directly, no envelope. Every consumer checks `schema_result.get("success")`, which is falsy on a bare JSON schema, so for **all external servers** (every bundled default: github, linear, brave-search, context7, playwright, chrome-devtools): (a) argument validation in `call_tool` is silently skipped, (b) required `session_id`/`project_id` injection (`_inject_required_session_id_argument`, tool_execution.py:267-273) never happens, (c) schema enrichment of argument errors never fires. The documented contract — "The proxy validates parameters on every call_tool. If params are wrong, the error includes the full schema" (`instructions.py:24`, `install/shared/prompts/mcp/progressive-discovery.md:17`) — is violated for external tools. The public `get_tool_schema` tool also returns shape-inconsistent results (enveloped for internal, bare schema for external). Masked by mock drift: service-level tests mock `get_tool_input_schema` returning the envelope (`tests/mcp_proxy/services/test_tool_proxy_invalid_arguments_schema.py:43-50`, `tests/mcp_proxy/test_proxy_server.py:240-246`) — a shape the real manager never produces (`tests/mcp_proxy/test_manager_coverage.py:1324-1333` pins the bare-schema reality).
- **Why it matters:** Core progressive-discovery contract violated exactly where it matters most (third-party servers); agents lose validation and schema-hint feedback, and external tools whose schema requires `session_id` get no ambient injection.
- **Minimal fix:** In the external branch of `tool_execution.get_tool_schema`, wrap as `{"success": True, "tool": {"name": tool_name, "inputSchema": result}}` (or return enveloped `get_tool_info`). Fix the layered test mocks to match the real manager shape.
- **Confidence:** high — producer asymmetry and consumer checks verified directly by the synthesizer.

### [IMPORTANT] Empty-arguments call bypasses pre-validation of required parameters
- **Where:** `src/gobby/mcp_proxy/services/tool_execution.py:260-262` and `:274-282`
- **Failure mode:** `should_check_schema` is false when `arguments` is empty and there is no session/project context, so validation is skipped entirely. Even when the schema *is* fetched, line 274 (`if not arguments: return await _execute_tool_dispatch(...)`) short-circuits past the missing-required check. Calling a tool that requires `title` with `{}` dispatches directly; calling with `{"wrong": 1}` gets validated. If the underlying tool function has Python defaults for a schema-required param, the call silently executes with the default instead of being rejected.
- **Why it matters:** Violates "the proxy validates params on every call_tool"; inconsistent behavior between `{}` and non-empty bad args. The `{}` path is untested (`tests/mcp_proxy/services/test_tool_proxy_validation.py:239-273` covers only non-empty).
- **Minimal fix:** Remove the early return at line 274 and let `_check_arguments` handle empty dicts; drop the `bool(arguments)` term from `should_check_schema` (or keep it only when the schema has no `required`).
- **Confidence:** high — verified by synthesizer.

### [IMPORTANT] Schema fetch failure in the validation path raises out of `call_tool` instead of returning an envelope
- **Where:** `src/gobby/mcp_proxy/services/tool_execution.py:264` (raise originates at `:489-493`)
- **Failure mode:** `get_tool_schema` re-raises external-manager failures as `MCPError` (lines 492-493). The validation-path fetch at line 264 is **not** wrapped in try/except (unlike the identical calls at `:174-179` and `:397-402`, which are). The wrapper in `server.py` has only try/finally — no except — so the `MCPError` escapes as a protocol-level exception. A transient schema-fetch failure aborts a tool call that would otherwise succeed, with a confusing protocol error instead of the structured `success: False` envelope every other failure path returns.
- **Minimal fix:** Wrap line 264 in the same try/except used at `:174-179` and proceed without pre-validation (or return a structured envelope) when the fetch fails.
- **Confidence:** high

### [IMPORTANT] Every external `call_tool` performs a full downstream `list_tools` round-trip plus a sync DB write — no proxy-side schema cache
- **Where:** `src/gobby/mcp_proxy/services/tool_execution.py:260-264` → `client_manager/tool_inventory.py:174-194` (`get_tool_info` → `_list_tools_for_server`) and `:140-161` (`cache_discovered_tools` → sync `mcp_db_manager.cache_tools`)
- **Failure mode:** `should_check_schema` is true on essentially every call (any arguments, or session/project context). For external servers, fetching the schema re-lists **all** tools from the downstream server over the wire each time — `get_tool_info` filters fresh `list_tools` output and never reads the DB cache written by `cache_discovered_tools` — and each listing performs a synchronous Postgres write on the event loop. Each external tool call ≈ 1 extra full list_tools round-trip + 1 blocking DB write + the actual call. (Today the fetched schema is then discarded due to the envelope Blocker above — fixing that Blocker makes this hot-path cost permanent unless cached.)
- **Why it matters:** `call_tool` is THE hot path; this doubles latency per external call, scales with downstream tool-count, and contradicts the cached-just-in-time design the instructions describe.
- **Minimal fix:** Add an in-memory schema cache keyed by server, invalidated in `clear_connection_state`/reconnect; only write `cache_tools` when the tool list changed.
- **Confidence:** high

### [IMPORTANT] AI-generated server description is generated, then thrown away
- **Where:** `src/gobby/mcp_proxy/actions.py:76-86`
- **Failure mode:** `mcp_manager.add_server(config)` upserts the DB row (with `description=None`) **before** `generate_server_description` runs. The follow-up `config.description = server_description` mutates a local object — and for any config where `normalize_bundled_server_config` produced a `replace()` copy (every server added with `args=None`, since `normalize_persisted_args` maps `None → []`, `bundled.py:91-118`), it isn't even the object stored in `manager._configs`. No second upsert occurs and the returned result omits the description. Descriptions are `None` after restart.
- **Why it matters:** Wasted LLM cost/latency on the import path (`importer.py:590`); server descriptions used for discovery/recommendation silently never materialize.
- **Minimal fix:** After generating, persist via a dedicated update, update `manager._configs[name].description`, and include the description in the returned dict.
- **Confidence:** high

### [IMPORTANT] `set_variable`/`get_variable` tool descriptions advertise a `workflow` parameter the tools do not accept
- **Where:** `src/gobby/mcp_proxy/server.py:488` and `:514` (docstrings become FastMCP tool descriptions); `instructions.py:52` repeats the claim
- **Failure mode:** Both docstrings say "Pass workflow param to scope to a specific workflow instance", but the signatures have no `workflow` parameter and hardcode `workflow=None` (server.py:500, :525). The underlying tools do support `workflow` (`tools/workflows/_variables.py:70-79, 156-164`). An agent following the description gets a FastMCP input-validation error. The bundled instructions file already removed this claim; the in-code fallback and docstrings did not.
- **Minimal fix:** Either expose `workflow: str | None = None` and pass it through, or strip the sentence from both docstrings and `instructions.py:52`.
- **Confidence:** high

### [IMPORTANT] `search_tools` resolves project with inverted precedence — static daemon project wins over per-call context
- **Where:** `src/gobby/mcp_proxy/server.py:445-448`
- **Failure mode:** `search_tools` uses `self._mcp_manager.project_id` (static constructor value from daemon startup, `manager.py:87`) first, falling back to ambient `get_project_context()` only when the manager has none — the opposite of `_caller_project_ref` (server.py:78-84, context first) and `RecommendationService`. On a daemon serving multiple projects, a caller in project B gets semantic search results scoped to startup project A.
- **Why it matters:** Cross-project result leakage on a discovery tool.
- **Minimal fix:** Check `get_project_context()` first, falling back to `self._mcp_manager.project_id`, matching `_caller_project_ref`.
- **Confidence:** med — code asymmetry confirmed; end-to-end confirmation needs a non-default project context on the HTTP MCP path.

### [IMPORTANT] `coerce_string_arguments` tries the lossy `unicode_escape` candidate before the conservative quote-replace, silently corrupting backslash content
- **Where:** `src/gobby/mcp_proxy/_coerce_arguments.py:36-54`
- **Failure mode:** For the documented common failure (agent escapes only quotes), the `unicode_escape` candidate is tried first and frequently parses — but converts `\n`→newline, `\t`→tab, `\uXXXX`→decoded char, altering values the agent intended literally (regexes, Windows paths). The conservative `value.replace('\\"', '"')` candidate is only tried second. The corrupted dict is forwarded to the target tool with no error. Tests never cover backslash-containing values, so ordering is unpinned.
- **Why it matters:** Silent argument corruption on the call_tool recovery path — worse than failing, since the tool executes with wrong data.
- **Minimal fix:** Try the `\"`→`"` replacement first; fall back to `unicode_escape`. Add a test with `\n`/`\\` content asserting preservation.
- **Confidence:** med — behavior certain from code; impact frequency depends on agent payloads.

### [IMPORTANT] `list_tools` reports a dead external server as `success: True, tools: []`
- **Where:** `src/gobby/mcp_proxy/services/tool_execution.py:118-147` (with `client_manager/tool_inventory.py:46-58`)
- **Failure mode:** The `except MCPError` at line 121 is effectively dead: `manager.list_tools(server_name)` catches all exceptions internally (including the `MCPError` raised after retry exhaustion) and returns `{server_name: []}`. A connection-dead server yields `{"success": True, "tools": [], "tool_count": 0}`.
- **Why it matters:** An agent doing progressive discovery concludes the server has zero tools rather than that it's unreachable; the error-envelope contract is not honored for this failure class.
- **Minimal fix:** Propagate the failure for single-server queries (or call `_list_tools_for_server` directly so the `MCPError` path is reachable) and return `success: False` with the connection error.
- **Confidence:** high

### [IMPORTANT] Unresolvable explicit `session_id` silently disables tool filtering and workflow enforcement
- **Where:** `src/gobby/mcp_proxy/services/session_context.py:60-67` → `tool_execution.py:247-258`, `result_handling.py:93-95`
- **Failure mode:** `resolve_platform_session_id` returns `None` when session resolution raises `ValueError` (not found / ambiguous). With `effective_session_id=None`, both the tool filter (`tool_execution.py:249`) and before/after workflow enforcement (`result_handling.py:94-95`) are skipped entirely. An explicit `session_id` overrides ambient context, so a caller in a gated session can pass a bogus ref (e.g. `"#99999"`) and execute with no proxy-side enforcement — only a log warning.
- **Why it matters:** Enforcement-bypass path. For stdio/SDK callers where CLI-side hooks don't fire, the proxy-side check is the only enforcement layer.
- **Minimal fix:** When an *explicit* session ref fails to resolve, return `success: False` (invalid session reference) instead of degrading to anonymous, or fall back to ambient context rather than `None`.
- **Confidence:** med — bypass fully traced; severity depends on how load-bearing proxy-side enforcement is for hookless callers.

### [IMPORTANT] After-tool workflow failure can destroy a successful tool result
- **Where:** `src/gobby/mcp_proxy/services/tool_execution.py:340-347` and `services/result_handling.py:200, 209-216`
- **Failure mode:** `_execute_tool_dispatch` awaits `_apply_after_tool_workflow` with no error protection after `result` is computed. Inside, only `workflow_handler.evaluate` is wrapped; `service._get_effective_session_id(session_id)` (sync DB resolution catching only `ValueError`) and `build_after_tool_event` (includes `deepcopy(tool_output)` of arbitrary tool output) run unprotected. Any exception there propagates and the caller receives an error for a call whose side effects already committed.
- **Why it matters:** The agent retries an operation that already succeeded (duplicate task creation, double spawn) — the classic lost-result hazard.
- **Minimal fix:** Wrap the entire `_apply_after_tool_workflow` invocation in try/except-log, mirroring how `evaluate` failures are tolerated.
- **Confidence:** med — confirm by inducing a DB error during after-tool session resolution.

### [IMPORTANT] `error_code` casing inconsistent across invalid-argument producers
- **Where:** `src/gobby/mcp_proxy/services/schema_guidance.py:106` (`"invalid_arguments"`) vs `services/argument_validation.py:104,113` (`"INVALID_ARGUMENTS"`), surfaced via `result_handling.py:182-186`
- **Failure mode:** Two producers emit the same logical error with different casing; all other pipeline codes are uppercase enum values. Consumers branching on exact `error_code` strings (workflow `when:` expressions do exactly this) must know which internal path produced the error. Tests pin both casings, baking the drift in.
- **Minimal fix:** Standardize on the uppercase enum value; update `schema_guidance.py:106` and the pinned tests together.
- **Confidence:** high

### [IMPORTANT] Silent `except Exception: return None` in fallback description lookup
- **Where:** `src/gobby/mcp_proxy/services/fallback.py:326-327`
- **Failure mode:** `_get_tool_description` swallows every exception with no logging — schema errors, DB-down, even programming errors in the SQL — degrading fallback quality invisibly.
- **Why it matters:** Direct repo-contract violation (silent `except Exception`).
- **Minimal fix:** Log at debug and narrow to DB error types.
- **Confidence:** high

### [IMPORTANT] `success_rate=0.0` serialized as `null` in fallback suggestions
- **Where:** `src/gobby/mcp_proxy/services/fallback.py:44`
- **Failure mode:** `round(self.success_rate, 4) if self.success_rate else None` — a tool with a genuine 0% success rate is reported as `success_rate: null` ("no data"), indistinguishable from no-metrics. Scoring is unaffected (`_compute_score` uses `is not None` correctly); only the serialized payload lies.
- **Why it matters:** The payload exists to steer agents away from failing tools; it masks exactly the worst ones.
- **Minimal fix:** `if self.success_rate is not None`.
- **Confidence:** high

### Connection manager & transports

### [BLOCKER] stdio/websocket `connect()` is not cancellation-safe — leaks live subprocesses and orphaned tasks on connect timeout
- **Where:** `src/gobby/mcp_proxy/transports/stdio.py:100-180` (cleanup only in `except Exception` at `:147`; context overwrite at `:128`), `transports/websocket.py:33-100` (same shape at `:50`, `:69`); triggered by `client_manager/connections.py:237-240`
- **Failure mode:** `ensure_connected` wraps `_connect_server` in `asyncio.wait_for(..., timeout=manager.connection_timeout)`. On timeout, `CancelledError` is raised inside `connect()` — most likely at `await self._session.initialize()` (the dominant slow point for cold-start stdio servers, e.g. `npx` downloads). `CancelledError` is a `BaseException` in Python 3.13, so the `except Exception` cleanup block at stdio.py:147 never runs: the entered `stdio_client` context (spawned subprocess + anyio task group with running reader/writer tasks) and the entered `ClientSession` stay alive, state stuck at `CONNECTING`. The retry loop (connections.py:231-263) reuses the *same* cached transport object (connections.py:146-154), and `connect()` overwrites `self._transport_context = stdio_client(params)` at stdio.py:128 — the previously entered context is orphaned forever (its child tasks keep the generator frame referenced, so GC never closes it). One zombie subprocess + fds + two event-loop tasks leaked per timed-out attempt, up to `max_retries+1` per connect storm, repeating on every circuit-breaker recovery. The websocket transport leaks the open WS connection and reader tasks the same way.
- **Why it matters:** Resource leak under completely normal use (slow-starting stdio servers vs the 30s default timeout), accumulating for the daemon's lifetime.
- **Minimal fix:** Run the unwind on `except BaseException` (re-raising `CancelledError` after cleanup), and/or tear down any previously-entered contexts at the top of `connect()` — the HTTP transport already does the equivalent via its owner-task pattern (`http.py:48-50`).
- **Confidence:** high — SDK `stdio_client` source confirms subprocess kill lives in the generator's `finally`, which only runs on context exit; no cancellation-during-connect test exists. Verified by synthesizer (cleanup block, wait_for trigger, cached-object reuse all read directly).

### [IMPORTANT] `reconnect()` bypasses the per-server connection lock and circuit breaker — concurrent `connect()` on the same transport object
- **Where:** `src/gobby/mcp_proxy/client_manager/connections.py:278-299` vs lock acquisition in `ensure_connected` at `:221`
- **Failure mode:** The health monitor spawns `_reconnect` (`health.py:144-154`), which pops the old connection and calls `manager._connect_server(config)` with no lock and no timeout. A concurrent `ensure_connected` takes the lock, sees the new `CONNECTING` connection object, and calls `connection.connect()` on the *same object* while the reconnect task's `connect()` is mid-flight — both pass the `state != CONNECTED` guard; the second overwrites `_transport_context`/`_session_context`/`_session` of the first (clobbered state, double subprocess, one leaked). And since this path has no `wait_for`, a server wedged in `initialize()` hangs the reconnect task indefinitely; the connection sits at `CONNECTING` where the monitor skips it (`health.py:103-105`), so automatic recovery stops permanently.
- **Minimal fix:** Route `reconnect()` through `ensure_connected` (or acquire the connection lock and wrap in `asyncio.wait_for`).
- **Confidence:** high for lock bypass and missing timeout; med for the permanent-limbo scenario.

### [IMPORTANT] `is_connected` is dict membership, not liveness — failed connects reported as "connected"
- **Where:** `src/gobby/mcp_proxy/client_manager/server_registry.py:128-130`; insertion-before-connect at `connections.py:146-152` with no removal on failure (`:163-166`)
- **Failure mode:** `connect_server` stores the transport in `manager._connections` *before* awaiting `connection.connect()` and never removes it on failure. `is_connected(name)` returns `name in manager._connections`. After a single failed attempt, status surfaces report the server as connected: `services/server_resolution.py:70`, `servers/routes/mcp/endpoints/server.py:95`, `endpoints/registry.py:131`, `services/server_mgmt.py:123`. `list_connections()` similarly lists dead connections.
- **Why it matters:** User-visible wrong data in the management API/UI; "connected but every call fails" debugging sessions.
- **Minimal fix:** `return server_name in manager._connections and manager._connections[server_name].is_connected` (transports maintain an accurate `is_connected` property).
- **Confidence:** high

### [IMPORTANT] `call_tool` serves stale/dead sessions with no invalidation or retry (list_tools got the fix, call_tool didn't)
- **Where:** `src/gobby/mcp_proxy/client_manager/invocation.py:44-66`; stale-serve at `connections.py:208-211`
- **Failure mode:** When a downstream server dies, the transport's `_state` stays `CONNECTED` and `_session` stays set — nothing downgrades transport state on call errors — so `ensure_connected` returns the dead session immediately. `tool_inventory.list_tools_for_server` handles this via `retry_list_tools_after_failure` (`tool_inventory.py:92-122`, tested), but `invocation.call_tool` just records the failure and re-raises. Recovery waits on the health monitor: 60s interval and 5 consecutive failures for `UNHEALTHY`, so tool calls fail for 1-5 minutes after a server restart even though a reconnect would succeed instantly.
- **Minimal fix:** On connection-shaped errors (`ClosedResourceError`, `BrokenResourceError`, `EndOfStream`), `discard_connection` and retry once, mirroring the list_tools path.
- **Confidence:** high on the code path; med on exact recovery latency.

### [IMPORTANT] Cleanup paths gate disconnect on `is_connected`, dropping half-open transports without teardown
- **Where:** `src/gobby/mcp_proxy/connection_cleanup.py:62` (`discard_connection`) and `:91` (`finalize_disconnect_all`)
- **Failure mode:** Both only call `disconnect()` when `getattr(connection, "is_connected", False)`. A transport stuck in `CONNECTING` with entered contexts (the cancellation-leak state from the Blocker above, or a mid-connect transport at shutdown) reports `is_connected == False` and is popped with no disconnect — for stdio, abandoning a live subprocess instead of killing it.
- **Why it matters:** Compounds the subprocess/fd leak; `disconnect_all` at daemon shutdown is the last line of defense.
- **Minimal fix:** Drop the `is_connected` gate — all three transports' `disconnect()` implementations are idempotent and null-safe.
- **Confidence:** high

### [IMPORTANT] Reused transport objects keep stale resolved secrets/config across reconnect attempts
- **Where:** `src/gobby/mcp_proxy/client_manager/connections.py:144-154`
- **Failure mode:** `resolved_config = manager._resolve_secrets_in_config(config)` is only consumed when a *new* transport is created. When the cached (failed) transport is reused, the freshly resolved config is discarded and the transport reconnects with the headers/env resolved at first creation. A user who fixes a rotated `$secret:NAME` keeps failing with old credentials until the connection entry is removed or the daemon restarts.
- **Why it matters:** "I fixed the secret but it still fails" — silent and invisible in logs (values are correctly never logged).
- **Minimal fix:** Refresh `connection.config = resolved_config` when reusing, or recreate the transport per fresh connect attempt.
- **Confidence:** high on behavior; med on frequency.

### [IMPORTANT] `"sse"` transport validates and persists but cannot connect
- **Where:** `src/gobby/mcp_proxy/models.py:119,145` and `src/gobby/config/mcp.py:219` (accept `"sse"`) vs `transports/factory.py:32-41` (map has only http/stdio/websocket)
- **Failure mode:** An sse server passes `MCPServerConfig.validate()` and is persisted to the DB *before* connecting; then `create_transport_connection` raises `ValueError: Unsupported transport: sse`. The broken row survives restarts. The MCP SDK ships an sse client that is simply never wired in.
- **Minimal fix:** Either map `"sse"` in the factory or reject it at validation until supported.
- **Confidence:** high

### [IMPORTANT] WebSocket transport silently drops `config.headers`; auth-token plumbing is dead end-to-end
- **Where:** `src/gobby/mcp_proxy/transports/websocket.py:50` (`websocket_client(self.config.url)` — SDK signature takes only `url`); `transports/base.py:39-40, 70-72`
- **Failure mode:** Headers configured for a websocket server (the documented auth mechanism, `models.py:122`) are never sent. Separately, `auth_token`/`token_refresh_callback` are threaded from `MCPClientManager.__init__` through the factory into every transport — but no transport ever reads `_auth_token` or invokes the callback; `set_auth_token` mutates a field nothing consumes (only tests exercise it).
- **Why it matters:** Auth-protected websocket servers fail with opaque connection errors; the token plumbing is a false affordance.
- **Minimal fix:** Pass headers to the WS connect (underlying `websockets.connect` supports `additional_headers`), and either wire `_auth_token` into HTTP/WS headers or delete the plumbing.
- **Confidence:** high that the code is dead; med on real-world websocket-with-headers usage.

### [IMPORTANT] `add_server` persists and registers the server, then raises if the initial connect fails
- **Where:** `src/gobby/mcp_proxy/client_manager/server_registry.py:161-193`
- **Failure mode:** Config is stored in `_configs`, registered with the lazy connector, and upserted to the DB before `await manager._connect_server(config)`. If connect raises (bad URL, server down), the exception propagates as a failed add — but the server *was* added and persists; retrying the add fails with `ValueError: already exists`.
- **Minimal fix:** Catch connect errors and return `{"success": True, "connected": False, "error": ...}` (config-added, connection-deferred matches the lazy-connect design), or roll back on failure.
- **Confidence:** med — possibly intentional persist-then-connect, but the raised exception contradicts it.

### Stdio shim, importer, metrics & search

### [IMPORTANT] `archive_old_events` INSERT-then-DELETE is not atomic — crash window permanently double-counts archive totals
- **Where:** `src/gobby/mcp_proxy/metrics_events.py:392-429`
- **Failure mode:** The aggregate `INSERT ... ON CONFLICT ... DO UPDATE SET call_count = archive.call_count + excluded.call_count` (line 392) and the `DELETE FROM metrics_events WHERE created_at < %s` (line 426) are separate `db.execute` calls, each auto-committed (`storage/hub/postgres.py:171-180`). A daemon crash between them leaves old events in place; the next daily run (`runner_maintenance.py:160-171`) re-aggregates the same rows via the additive UPSERT, permanently inflating lifetime totals.
- **Why it matters:** Silent, permanent corruption of archive counts; violates the transaction-boundary contract.
- **Minimal fix:** Wrap both statements in one `with self.db.transaction() as txn:`.
- **Confidence:** high

### [IMPORTANT] `cleanup_old_metrics` aggregate/delete pair is non-atomic and uses two different cutoffs — rows deleted unaggregated or double-aggregated
- **Where:** `src/gobby/mcp_proxy/metrics_store.py:308-309, 339-368, 377-386` and `metrics.py:206-214`
- **Failure mode:** Three bugs in one path. (1) `aggregate_to_daily` computes `cutoff = now - retention` at line 308; `cleanup_old_metrics` computes a *new, later* cutoff at line 377 — rows whose `last_called_at` falls between the cutoffs are deleted without ever being aggregated (silent loss every daily run). (2) The SELECT, per-row UPSERTs, and DELETE each commit independently; a crash mid-sequence re-aggregates already-archived rows additively (double counting). (3) A concurrent `record_call` between SELECT and DELETE bumps `last_called_at` past the cutoff; the row survives with counts already rolled into daily, and gets aggregated again later.
- **Minimal fix:** Compute one cutoff in `ToolMetricsManager.cleanup_old_metrics` and pass it down; run aggregate + delete in a single transaction deleting exactly the aggregated row set.
- **Confidence:** high

### [IMPORTANT] `reset_metrics` with no filters deletes ALL projects' metrics, reachable from an MCP tool with all-default args
- **Where:** `src/gobby/mcp_proxy/metrics_store.py:293-301` (no-conditions branch: `DELETE FROM tool_metrics` with no WHERE), exposed via `tools/metrics.py:186-207` and `:216-234`
- **Failure mode:** Re-verified from the mcp_proxy-tools review: the unfiltered branch exists, and neither MCP tool wrapper injects the current project ID — all params default to `None`, so a no-arg `reset_metrics` call wipes the cross-project `tool_metrics` table. Sibling drift: reset touches only `tool_metrics`; `tool_metrics_daily` and `metrics_events` keep reporting the "deleted" data, so a reset is partial and inconsistent across the three stores.
- **Minimal fix:** Require at least one filter (or scope to the calling project) in the tool wrapper; decide whether reset cascades to daily/event stores.
- **Confidence:** high

### [IMPORTANT] Schema-drift detection ignores tool descriptions — description-only changes never re-embed (stale semantic search)
- **Where:** `src/gobby/mcp_proxy/schema_hash.py:19-37` (`compute_schema_hash` hashes only `inputSchema`) and `:210-250`; consumer `servers/routes/mcp/endpoints/registry.py:286-320`; embedded text `semantic_search.py:82-111`
- **Failure mode:** `_build_tool_text` embeds `Description: ...` prominently, but the reindex endpoint classifies a tool as "unchanged" purely from the inputSchema hash. A tool whose description changes (common across MCP server version bumps) keeps its old embedding and payload forever. (Internal-registry tools accidentally avoid this because registry.py:251 passes the whole `get_schema()` dict — including description — as "inputSchema", which also means internal vs external tools hash differently shaped inputs.)
- **Minimal fix:** Include description in the hashed canonical form — `compute_text_hash` already exists and is currently dead code.
- **Confidence:** high

### [IMPORTANT] Heartbeat task exception in `finally` masks the wait-tool result
- **Where:** `src/gobby/mcp_proxy/wait_tools.py:241-267` (specifically `:262-267`)
- **Failure mode:** `_heartbeat` calls `await ctx.report_progress(...)` with no exception handling. If the stdio client stream breaks mid-wait, the heartbeat task dies with that exception; in the `finally`, `await heartbeat_task` swallows only `CancelledError` — any other exception re-raises **from the finally**, discarding the tool call's return value (or replacing its original exception). Silent variant: once the heartbeat dies, no further keep-alives are sent.
- **Minimal fix:** Wrap the `_heartbeat` body in try/except-log, and/or `heartbeat_task.cancel()` + suppressed await instead of bare await.
- **Confidence:** high

### [IMPORTANT] `DaemonProxy.call_tool` crashes on non-numeric wait-tool timeout while the guard layer handles the same input gracefully
- **Where:** `src/gobby/mcp_proxy/stdio.py:280` (`float(raw_timeout)`, unguarded) vs `wait_tools.py:162-165` (same parse wrapped in try/except)
- **Failure mode:** `{"timeout": "5m"}` passes the tolerant guard layer (`requested_timeout = None`), then `DaemonProxy.call_tool` re-parses with bare `float()` → `ValueError` propagates as a raw MCP error instead of the structured envelope every other failure path returns.
- **Minimal fix:** Mirror the `try/except (TypeError, ValueError)` at stdio.py:280, falling back to 300.0.
- **Confidence:** high

### [IMPORTANT] `_strip_none` is applied to every proxied daemon response, silently mutating tool results
- **Where:** `src/gobby/mcp_proxy/stdio.py:197` (inside `_request`, applied to all HTTP-200 bodies); helper at `:49-61`
- **Failure mode:** The documented rationale (docstring :50-56, comment :495-497) is stripping nulls from tool **inputSchemas** for strict Jinja templates. But `_request` strips `None` recursively from *all* responses, including `call_tool` results — `{"parent_id": null}` comes back with the key deleted, so consumers can't distinguish "null" from "absent", and any tool legitimately returning `null` has its payload rewritten.
- **Why it matters:** The proxy should be transparent for results; silent payload mutation surfaces as confusing behavior far from the cause.
- **Minimal fix:** Strip nulls only on schema-shaped endpoints (already done at `create_stdio_mcp_server:497-499`), not on data responses.
- **Confidence:** med — may be a deliberate LMStudio workaround, but the stated rationale covers only schemas.

### [IMPORTANT] `start_daemon_process` never drains or closes the child's stdout/stderr pipes on the success path
- **Where:** `src/gobby/mcp_proxy/daemon_control.py:101-141`
- **Failure mode:** The child (`gobby daemon start`, which polls startup for up to 60s printing progress) is spawned with `stdout=PIPE, stderr=PIPE`; `communicate()` is awaited only if it exits within 0.5s. On the success path both pipes stay open and unread: if the child's output exceeds the ~64KB pipe buffer (verbose mode, rich progress, error spew), the child blocks forever on `write()` — a wedged process leaked under the stdio shim, plus leaked fds.
- **Minimal fix:** Spawn with `DEVNULL` (re-spawn with pipes only on the immediate-crash diagnostic path), or attach a drain task.
- **Confidence:** med — needs >64KB output to deadlock; the fd leak is certain.

### [IMPORTANT] `has_embeddings` silently swallows all exceptions
- **Where:** `src/gobby/mcp_proxy/semantic_search.py:312-313`
- **Failure mode:** `except Exception: return False` with no logging. A Qdrant outage, auth failure, or the deliberate dimension-conflict `RuntimeError` from `_ensure_tool_collection` (line 194) is indistinguishable from "no embeddings exist" — callers may trigger a full re-embed (slow, costly with cloud embedding APIs) with zero diagnostic trail.
- **Minimal fix:** Log the exception; let the dimension-conflict error propagate or log at error level.
- **Confidence:** high

### [IMPORTANT] `refresh_tools_incremental` is dead in production and harbors two latent contract bugs against `SchemaHashManager`
- **Where:** `src/gobby/storage/mcp.py:706-725` (caller side; in-scope contract surface `schema_hash.py:238-248`); referenced only by tests — production refresh uses `cache_tools`
- **Failure mode:** (1) Case drift: `check_tools_for_changes` keys results by original-case tool name (schema_hash.py:239) while the caller stores and tests membership with lowercased names (mcp.py:706, 724, 750) — any tool with an uppercase character falls to "unchanged" forever, so updates are never written. (2) Schema-key drift: the checker hashes `inputSchema or input_schema` (schema_hash.py:240) but the storer hashes `inputSchema or args` (mcp.py:725) — tools delivered with alternate keys are classified "changed" on every refresh (perpetual re-embed churn).
- **Minimal fix:** Delete `refresh_tools_incremental` (and its tests) or normalize names and schema keys identically on both sides.
- **Confidence:** high on logic; med on exposure (no production caller today).

### Cross-cutting

### [IMPORTANT] Synchronous DB and crypto work on the event loop throughout the async proxy
- **Where (representative sites):**
  - `server.py:247-254` (`resolve_and_seed_contexts` — sync session/project DB resolution inside async `call_tool`), `:155` (`record_servers_listed` → sync variable write), `:494-501`/`:520-526` (sync `_set_var`/`_get_var` in async handlers)
  - `client_manager/server_registry.py:171, 219, 254, 271` (sync `upsert`/`remove_server`/`update_server` psycopg transactions in async methods); `client_manager/tool_inventory.py:151` (`cache_tools` in the async list flow); `client_manager/secrets.py:43-65` (Fernet decryption + DB in async `connect_server`)
  - `services/session_context.py:55-58, 92, 140-149` (sync session resolution and variable latches on the call path); `services/schema_guidance.py:57, 82`; `services/fallback.py:233-237, 315-325`
  - `metrics.py:48-109` (`record_call` — two blocking psycopg statements per proxied tool call, invoked from the `finally` of `invocation.call_tool:79`)
- **Failure mode:** Blocking psycopg/crypto calls run directly on the daemon's single event loop on every proxied tool call, list, schema fetch, and variable access. Under DB latency (remote Postgres, pool contention, fsync stalls) the entire MCP server — all sessions, heartbeats, WebSocket traffic — stalls together. The sanctioned bridge already exists and is used in adjacent code: `run_db` ("bounded executor bridge for blocking database calls", `registries.py:77,108`) and `asyncio.to_thread` in `result_handling.py:112-139`.
- **Why it matters:** Direct violation of the repo async contract on the hottest paths in the daemon. Same systemic finding as mcp_proxy-tools.md; the fix should land together with the tools-layer offload so the event-loop-serialization masking is removed once, deliberately.
- **Minimal fix:** Route these sites through `run_db`/`asyncio.to_thread`, starting with the `call_tool` path (`resolve_and_seed_contexts`, `record_call`, session resolution).
- **Confidence:** high — sites verified sync by all four reviewers; practical impact is workload-dependent.

## Nits

### [NIT] `get_tool_schema` accepts and silently discards `session_id`/`record_discovery`
- **Where:** `src/gobby/mcp_proxy/services/tool_execution.py:449` (`del session_id, record_discovery`)
- **Note:** Callers thread these in good faith (`server.py:343-347`, `tool_proxy.py:257-271`); schema-fetch discovery is never recorded despite the parameter promising it. Record or remove.

### [NIT] `list_tools(server_name="gobby")` aggregates every internal registry's tools in one response
- **Where:** `src/gobby/mcp_proxy/services/tool_execution.py:76-99`
- **Note:** Bypasses per-server progressive discovery and skips `record_listed_server` for the listed servers, so discovery-gating rules won't credit it.

### [NIT] `list_mcp_servers` duplicates `list_servers` with drifted connectivity semantics
- **Where:** `src/gobby/mcp_proxy/server.py:102-156` vs `services/server_resolution.py:60-91`
- **Note:** server.py counts dict membership pre-filter but `state == "connected"` post-filter — two definitions of "connected" in one response. Consolidate.

### [NIT] `list_tools` tool filtering uses raw `session_id` while `call_tool` filters on the resolved UUID
- **Where:** `src/gobby/mcp_proxy/services/tool_execution.py:95-96, 104-105, 144-145` vs `:249`
- **Note:** A `#N` ref filters nothing at list time but filters at call time — tools can be listed that are then blocked.

### [NIT] `list_tools`/`get_tool_schema` error envelopes omit `error_code`
- **Where:** `src/gobby/mcp_proxy/services/tool_execution.py:112-116, 149-157, 471-487`
- **Note:** call_tool failures carry `error_code`; discovery failures don't — consumers can't branch uniformly.

### [NIT] Over-broad `is_argument_error` heuristic reshapes non-argument failures
- **Where:** `src/gobby/mcp_proxy/services/argument_validation.py:31-51` (applied at `tool_execution.py:395-412`)
- **Note:** Any error containing "missing"/"invalid"/"unknown"/"field"/"expected" (e.g. "task not found: unknown task id") is rewritten into an invalid-arguments envelope with "Review the schema" guidance — misleading for not-found/state errors.

### [NIT] `_recommend_llm` returns `success: True` with empty recommendations when LLM output is unparseable
- **Where:** `src/gobby/mcp_proxy/services/recommendation.py:224-235`
- **Note:** Parse failure carries no error indicator; "no tools match" and "LLM returned garbage" are indistinguishable. `agent_id` (line 50) is accepted and unused.

### [NIT] Redundant local import shadowing module-level import
- **Where:** `src/gobby/mcp_proxy/services/tool_execution.py:418`
- **Note:** `get_project_context` already imported at line 8.

### [NIT] Health-check failures discard the real error
- **Where:** `src/gobby/mcp_proxy/client_manager/health.py:81, 116`; `transports/base.py:93`
- **Note:** `record_failure("Health check failed")` throws away the gathered exception; `except (TimeoutError, Exception)` is redundant and unlogged. `last_error` never shows why checks fail.

### [NIT] Duplicated health-check logic
- **Where:** `src/gobby/mcp_proxy/client_manager/health.py:63-87` vs `:100-156`
- **Note:** `health_check_all` is a copy of the monitor loop body minus reconnection; extract one helper.

### [NIT] Dead aliases in the manager facade
- **Where:** `src/gobby/mcp_proxy/manager.py:36, 55`
- **Note:** `_create_transport_connection`/`_truncate_tool_brief` exist only as test import conveniences; tests patch the public name.

### [NIT] Unresolved secret refs are stripped, then the connection proceeds without the credential
- **Where:** `src/gobby/mcp_proxy/client_manager/secrets.py:45-58`
- **Note:** A missing `$secret:NAME` silently removes (e.g.) the `Authorization` header and connects anyway; failing fast would surface the misconfiguration sooner. (Values are correctly never logged.)

### [NIT] `timeout=0` means "no timeout" in `call_tool`
- **Where:** `src/gobby/mcp_proxy/client_manager/invocation.py:46`
- **Note:** `if timeout:` should be `if timeout is not None:`.

### [NIT] `disconnect_server` deletes the health entry for a still-configured server
- **Where:** `src/gobby/mcp_proxy/client_manager/connections.py:195`
- **Note:** The server vanishes from `get_server_health()` until the next connect; setting `DISCONNECTED` would suffice.

### [NIT] `LazyServerConnector.unregister_server` drops the connection lock while it may be held
- **Where:** `src/gobby/mcp_proxy/lazy.py:202-210` with `:277-291`
- **Note:** Remove-then-re-add while a connect attempt holds the old lock yields a fresh lock, permitting two concurrent connection attempts to the same server.

### [NIT] Duplicated `CloneGitManager` construction with inconsistent exception handling
- **Where:** `src/gobby/mcp_proxy/registries.py:263-269` (`except (TypeError, OSError, RuntimeError)`, debug) vs `:327-334` (`except Exception`, warning)
- **Note:** Same constructor, two copies, two catch policies; extract a helper.

### [NIT] Private-attribute injection into `HubManager`
- **Where:** `src/gobby/mcp_proxy/registries.py:420-422`
- **Note:** `hub_manager._skill_description_config = ...` pokes a private field from outside; make it a constructor arg.

### [NIT] Fallback instructions drift from the bundled prompt file
- **Where:** `src/gobby/mcp_proxy/instructions.py:48-53` vs `install/shared/prompts/mcp/progressive-discovery.md:48-51`
- **Note:** Fallback still documents the removed `workflow` param and lacks the `<code_search>` section; a sync test would prevent divergence.

### [NIT] `_repair_tool_collection_and_retry` never retries
- **Where:** `src/gobby/mcp_proxy/semantic_search.py:206-231`
- **Note:** Docstring says "repair ... and retry once" and callers pass `_upsert`/`_search` actions, but the body unconditionally raises; `action` is never invoked.

### [NIT] Dead code: `_cosine_similarity`, `compute_text_hash` consumers, vestigial `tool_embeddings` table
- **Where:** `src/gobby/mcp_proxy/semantic_search.py:31-52, 77-79, 334-347`
- **Note:** Production search runs through Qdrant; only tests import these. `SearchResult.embedding_id` is always 0 and the `tool_embeddings`/`text_hash` schema appears orphaned.

### [NIT] `_find_missing_secrets` computes `path` strings that are never used
- **Where:** `src/gobby/mcp_proxy/importer.py:727-737`
- **Note:** Only `match.group(0)` is appended; per-key path bookkeeping is dead, and duplicate placeholders are reported repeatedly.

### [NIT] `_parse_and_add_config` no longer adds anything
- **Where:** `src/gobby/mcp_proxy/importer.py:635`
- **Note:** Behavior is a non-mutating preview per its docstring; rename to match.

### [NIT] Duplicate in-function `import os`
- **Where:** `src/gobby/mcp_proxy/stdio.py:674, 708`
- **Note:** `os` is imported at module level (line 10).

### [NIT] Relies on private FastMCP internals
- **Where:** `src/gobby/mcp_proxy/stdio.py:497` (`mcp._tool_manager._tools`)
- **Note:** Breaks silently on MCP SDK upgrades; prefer a public tool-listing API.

### [NIT] New `httpx.AsyncClient` per request; unencoded URL path segments
- **Where:** `src/gobby/mcp_proxy/stdio.py:187` and `:282`
- **Note:** No connection reuse for proxied calls; `f"/api/mcp/{server_name}/tools/{tool_name}"` corrupts on names containing `/`, space, or `?`. Pool a client; quote path segments.

### [NIT] `from typing import cast` inside the process-scan loop
- **Where:** `src/gobby/mcp_proxy/daemon_control.py:63`
- **Note:** Executes per matching process iteration; hoist to module top.

### [NIT] `get_timeseries` loads all raw rows into Python for bucketing
- **Where:** `src/gobby/mcp_proxy/metrics_events.py:282-292, 307-348`
- **Note:** 30d/"all" ranges on a busy daemon fetch every event row; `date_trunc` + `GROUP BY` in SQL would bound memory/latency.

### [NIT] `tm-` + 6-hex-char primary key has a 24-bit collision space
- **Where:** `src/gobby/mcp_proxy/metrics_store.py:106`
- **Note:** PK collision on a *new* (project, server, tool) row isn't covered by the `ON CONFLICT(project_id, server_name, tool_name)` clause; the call's metrics are dropped. ~0.06% per new row at 10k existing rows. Use full `uuid4().hex`.

### [NIT] Orphaned background task after outer cancellation in `_await_with_guard`
- **Where:** `src/gobby/mcp_proxy/wait_tools.py:217-225`
- **Note:** The `_consume_background_result` callback is attached only on `TimeoutError`; if the shielded outer await is cancelled, the inner task runs with no consumer → "Task exception was never retrieved" noise. Attach the callback at task creation.

### [NIT] Phantom TYPE_CHECKING import (found while tracing; file formally out of scope)
- **Where:** `src/gobby/servers/http.py:38`
- **Note:** `from gobby.utils.tool_metrics import ToolMetricsManager` — that module doesn't exist; the real class is `gobby.mcp_proxy.metrics.ToolMetricsManager`. Harmless at runtime (TYPE_CHECKING) but resolves to nothing.

## Systemic patterns

1. **Mock-shape drift hides cross-layer contract breaks.** Service-layer tests mock `get_tool_input_schema` returning an envelope the real manager never produces — exactly how the external-validation Blocker shipped green. Layer-boundary shapes need contract tests against the real implementation, not hand-written mocks.

2. **Stub code shipped as production.** "`# Mocking this interaction`" persistence stubs in `server_mgmt.py` landed on the live MCP tool surface; nothing asserts durability of add/remove. Pair with pattern 4: the persisting twin existed the whole time.

3. **Sync DB/crypto on the async hot path, despite the sanctioned bridge existing.** `resolve_and_seed_contexts`, `record_call`, session resolution, variable latches, secrets decryption, `cache_tools`, and config upserts all block the event loop while `run_db`/`asyncio.to_thread` are already used in adjacent code (`registries.py`, `result_handling.py`). Same finding as mcp_proxy-tools.md — fix as one deliberate migration, because today's event-loop serialization is also masking check-then-act races that offloading will expose.

4. **Parallel implementations drifting apart.** `GobbyDaemonTools.list_mcp_servers` vs `server_resolution.list_servers` (connectivity semantics); `ServerManagementService` vs `MCPClientManager.add_server`/`actions.py` (persistence, duplicate checks); wait-timeout parsing in `prepare_client_guard` vs `DaemonProxy.call_tool` (lenient vs crashing); schema-key/name normalization in `schema_hash` vs `storage/mcp` (latent stale-schema bugs). Every pair has already diverged in behavior.

5. **Cleanup and status gated on optimistic state instead of actual resources.** `is_connected` doubles as a membership check (`server_registry.py:128`) and gates teardown (`connection_cleanup.py:62,91`); transports never downgrade state on runtime errors. The codebase repeatedly conflates "we have an object" with "the resource is alive" — producing the leak findings, the stale-session serving, and the false-positive status reporting.

6. **Cancellation unhandled on every transport setup path except HTTP.** HTTP got the owner-task redesign precisely to solve cancel-scope problems; stdio and websocket still enter anyio contexts in one task, exit in another (see the cancel-scope error suppression at stdio.py:189-211 / websocket.py:110-133), with no `BaseException`-safe unwind. Porting the owner-task pattern would resolve the Blocker and the suppression hacks together.

7. **Best-effort degradation hides hard failures.** Unreachable servers become empty tool lists; unresolvable sessions become anonymous (enforcement off); unparseable LLM output becomes a successful empty recommendation; failed embedding probes become "no embeddings". Each individually defensible — collectively a proxy that rarely admits something is broken.

8. **Multi-statement maintenance jobs without transactions.** Every aggregate-then-delete path in metrics (`metrics_store`, `metrics_events`) issues independent auto-committed statements; `PostgresHubDatabase.transaction()` exists and is unused in these files. Crash windows and concurrent-writer races all stem from this one habit.

9. **Test gaps mirror the bugs.** Zero cancellation-during-connect transport tests; stale-session recovery tested for `list_tools` only; no `reconnect` vs `ensure_connected` race test; `{}`-arguments validation path untested; rollup-job atomicity untested. The places coverage stops are exactly where the findings cluster.

10. **Importer secret handling: clean.** No plaintext secret leakage to logs found; the `<YOUR_*>` placeholder contract is consistent between the import prompt and `SECRET_PLACEHOLDER_PATTERN`. Imported `env`/`headers` are stored plaintext in `mcp_servers`, matching the existing storage design rather than an importer regression. The cross-project copy path (`importer.py:145-161`) propagates real secrets between projects by design — worth an explicit approval step if projects ever become multi-tenant.
