Plan artifact: `.gobby/plans/rust-internal-mcp-servers.md`

# Native Rust architecture for internal `gobby-*` MCP servers

**Plan ID:** rust-internal-mcp-servers

## Overview
`kind: framing`

This plan defines the shared Rust architecture that #21560 (Tasks family) and
#21570 (MCP front door flip) consume. It does not port any family manager or
expand this plan into tasks. It establishes the contract, gateway, compatibility
backend, and cutover proof needed for one internal server to move at a time while
every MCP client continues to use the routes frozen by `.gobby/plans/gmcp-stdio.md`:
`GET /api/mcp/servers`, `GET /api/mcp/{server}/tools`,
`POST /api/mcp/tools/schema`, `POST /api/mcp/{server}/tools/{tool}`, and
`POST /api/mcp/tools/call`. The later streamable-HTTP `/mcp` transport uses the
same gateway and registry rather than a second dispatch path.

The restraint ladder stops at existing repository seams. `RouteFamily` already
keeps handlers in family crates and lets gdaemon compose `Proxy | Native |
Compare` backends. `ToolProxyService` already defines the policy order and public
result shapes. Hub mode already makes the hub authoritative and sends
machine-local work to nodes as commands. The plan adds the smallest missing seam:
a typed internal-tool descriptor shared by family crates and gdaemon, plus a
native gateway that can delegate an unported server to Python.

## Decision Record
`kind: framing`

1. **Handlers live with their managers in the existing route-family crates.**
   Each `crates/g<family>` crate exports its REST `RouteFamily`, service trait,
   and one internal MCP family descriptor. The descriptor binds public server
   name, tool metadata, typed input, handler, and read-only classification to
   the same injected service used by the REST routes. gdaemon owns composition
   and dispatch only.
   - Rejected: handlers in one gdaemon registry module. It would duplicate
     family service APIs, concentrate every manager dependency in gdaemon, and
     recreate the Python registry monolith.
   - Rejected: one process per internal server. These servers share the hub
     transaction boundary, session/rule context, and release lifecycle; extra
     RPC, supervision, and credentials solve no current isolation requirement.

2. **Schemas derive from Rust request types and are content-versioned.**
   Tool request structs derive `Deserialize` and `schemars::JsonSchema`. A
   shared normalizer emits the public JSON Schema shape, and the registry
   computes the existing 16-hex SHA-256 definition hash over canonical
   `{description,input_schema}` JSON. That hash is the schema version used for
   freshness, leases, search indexing, and parity evidence; there is no second
   numeric version to synchronize. A family migration must make its derived
   schema JSON-object-equal to the current Python schema before activation.
   - Rejected: hand-written JSON Schema beside request structs, because the two
     sources can drift.
   - Rejected: generating Rust schemas from Python registries, because it keeps
     Python in the build and makes the strangler boundary depend on the code
     being retired.
   - Rejected: a separately maintained semantic version, because the content
     hash already identifies the exact definition and detects every change.

3. **gdaemon owns one policy pass around every dispatch.** The gateway resolves
   the authenticated caller context, injects omitted schema-required
   `session_id`/`project_id` arguments from that context while preserving
   explicitly supplied target values, then runs the native rule engine's
   semantic `before_tool` event. It re-resolves the possibly modified
   server/tool, repeats that normalization, revalidates arguments, dispatches
   once, then runs `after_tool` once. A
   `before_tool` denial or evaluator failure dispatches nothing; an `after_tool`
   evaluator failure records diagnostics and preserves the completed handler's
   original result without redispatch or replay. Listing
   servers/tools and serving a schema update the same hub session variables and
   schema leases used by progressive discovery. Provider/runtime hook names such
   as `before_agent`, `after_agent`, and `stop` do not enter this API.
   - Rejected: policy in every family handler, because it permits inconsistent
     ordering and missed enforcement.
   - Rejected: continuing to send native calls through Python
     `ToolProxyService`, because #21570 makes gdaemon the policy owner and the
     Python hop would remain on every call.

4. **The strangler selects a backend per public server name.** The `mcp` route
   family is native after #21566 and #24151. Within it, each internal server
   reuses the existing bootstrap `front_door.routes` map under its public name,
   for example `gobby-tasks: proxy | compare | native`; absent entries default
   to `proxy`. `proxy` calls a loopback-only Python compatibility endpoint that
   invokes `InternalToolRegistry` directly. `compare` returns Python's result,
   compares native list/schema responses and read-only calls, and leaves
   mutations on Python; isolated parity tests cover mutating tools. `native`
   dispatches only to Rust. This avoids double rule evaluation and makes one
   server independently reversible during migration.
   - Rejected: one global MCP switch, because the last family would hold the
     first family's cutover and rollback hostage.
   - Rejected: shadow-running mutating tools, because comparison must never
     duplicate an externally visible write.

5. **The hub serves every internal family; nodes relay.** In standalone mode
   the local gdaemon is both hub and node and serves the registry. In hub mode,
   the hub gdaemon serves `gobby-tasks`, `gobby-tasks-ops`, `gobby-plans`,
   `gobby-feedback`, `gobby-results`, `gobby-sessions`, `gobby-memory`,
   `gobby-review-learning`, `gobby-workflows`, `gobby-metrics`, `gobby-agents`,
   `gobby-worktrees`, `gobby-workspaces`, `gobby-clones`, `gobby-merge`,
   `gobby-hub`, `gobby-config`, `gobby-voice`, `gobby-skills`, `gobby-cron`, and
   `gobby-communications`. A node exposes no local `gobby-*` MCP registry and
   has no hub datastore credential; `gmcp` reaches the hub through the existing
   node relay. A hub handler that needs filesystem, terminal, checkout, or agent
   process work sends the typed hub-to-node command owned by hub mode.
   - Rejected: placing worktree, clone, merge, agent, or config MCP servers on
     the node merely because some effects are machine-local. Their authority,
     rows, policy, and caller context remain hub-owned; only the bounded effect
     executes on the selected node.

6. **Family adoption is manager-first and name-preserving.** #21560 defines
   the `gobby-tasks` and `gobby-tasks-ops` descriptors beside the Rust task
   manager and proves schema/result parity without activating them. #21570 owns
   the native gateway and moves each available family through `proxy`,
   `compare`, and `native` under the unchanged public name. Python registration
   is removed only after the native activation smoke passes. #21570 closes when
   the last internal server is native and the compatibility endpoint is gone.

## Constraints
`kind: framing`

- Public MCP names, argument meaning, result envelopes, structured error codes,
  timeout behavior, offloading, discovery ordering, and the distinction between
  caller session context and a tool's body `session_id` remain unchanged.
- Caller context is a non-serializable Rust `ToolContext`, not an input-schema
  field. It carries the resolved caller session, project/scope, authenticated
  principal, agent run, machine, intent, and request origin. A body
  `session_id` remains ordinary target-tool input.
- Missing caller context on an agent/wrapper call fails closed as today.
  Operator and trusted internal calls retain their explicit authority.
- The Python compatibility endpoint accepts only the inherited front-door
  secret from a loopback peer, exposes no external MCP server, and bypasses
  Python workflow enforcement because gdaemon has already performed it.
- The compatibility request carries a secret-authenticated internal context
  envelope separately from tool arguments. Python validates it, seeds the
  existing session/project/principal/agent-run ContextVars, invokes the
  registry, and resets every token in `finally`; the envelope never replaces or
  injects a tool's body `session_id`.
- The gateway treats a rule mutation as a new dispatch request: resolve the new
  server/tool, fetch its current schema, apply the existing schema-required-only
  missing `session_id`/`project_id` injection from caller context, and validate
  the modified arguments before any handler runs. Explicit target arguments are
  never overwritten, and native and Python backends receive the same normalized
  argument map.
- `schemars` is already present through `rmcp`, and `jsonschema` is already in
  the lockfile. Use those installed dependency families; add no schema compiler,
  code generator, or plugin system.
- The Rust `gobby-core` change affects every released binary that depends on it.
  The shipping change bumps and rebuilds `gcode`, `gdaemon`, `ghook`, `gclient`,
  and `gmcp`; the coherent `gcode`/`gdaemon`/`ghook` set is promoted together,
  while `gclient` and `gmcp` use their component release paths. `gterm` is not
  bumped because it does not depend on `gobby-core`.
- No production file may reach 1,000 lines. Keep registry/schema mechanics in
  `gobby-core`, gateway/policy mechanics in the gdaemon MCP module, and family
  handlers in their owning crates.

Consumer sweep evidence (observed at HEAD `af5c224422812c5e0544d897b892d15530b8c289`):
`gcode evidence` found `RouteFamily` in `crates/gcore/src/route_family.rs`
(excerpt hash `9a5e1634a8364951a3d160a863ee85355f532a3d50b4d88437f548e90dcd376f`),
`ToolProxyService.call_tool` in `src/gobby/mcp_proxy/services/tool_proxy.py`
(`a437901341ce29f95bde9b015076f2430f73935e6b247a4f4fb0b87f83db2669`),
`InternalToolRegistry.tool` in `src/gobby/mcp_proxy/tools/internal.py`
(`feb9da325e695beb3f1e51e43263794a969230e107cc562026e108c6bdeebb35`),
and `_set_context_for_request` in
`src/gobby/servers/routes/mcp/endpoints/request_context.py`
(content hash `3ba5fdf81919f59f9d39805420c4bbe790d80c3eaf6e8a596dc849131293140d`).
Implementation refreshes any stale citation before editing.

## P1: Shared family contract
`kind: framing`

### 1.1 Typed internal-tool descriptor and schema identity [category: code]
`kind: deliverable`

Targets:
- `Cargo.toml`
- `Cargo.lock`
- `crates/gcore/Cargo.toml`
- `crates/gcore/src/lib.rs::*` — scope-reason: add the public MCP module beside the file's module declarations without changing `gobby_home`
- `crates/gcore/src/mcp.rs`
- `crates/gcore/tests/mcp_contract.rs`
- `docs/contracts/internal-mcp.md`

**Research context:**
- Observed: `crates/gcore/src/route_family.rs::RouteFamily` is the existing
  shared seam between gdaemon and family crates. Its `claims` method already
  leaves unclaimed requests on the Python proxy.
- Observed: `src/gobby/mcp_proxy/tools/internal.py::InternalToolRegistry.tool`
  derives only a shallow schema from Python annotations, while
  `InternalToolRegistry.register` accepts an exact schema and read-only flag.
- Observed: `src/gobby/mcp_proxy/schema_hash.py::compute_schema_hash` hashes
  canonical description-plus-schema JSON to 16 hex characters. Preserve this
  algorithm so a port does not invalidate freshness state for an unchanged
  definition.
- Chosen approach: add proposed `ToolFamily`, `ToolDefinition`, `ToolContext`,
  and `ToolError` types in `gobby-core`. A family returns immutable descriptors;
  gdaemon owns the registry and supplies context at call time. Derive and
  normalize schemas once at startup, validate duplicate names and descriptor
  invariants once, and cache the canonical JSON/hash.
- Planned verification:
  `cargo nextest run -p gobby-core -E 'test(mcp_contract)'` and
  `cargo fmt -p gobby-core -- --check` pass. Fixture assertions prove stable
  canonical ordering, hash parity with Python vectors, duplicate rejection,
  context exclusion, and validation-before-deserialization.

**Granularity:** One lifecycle owner: the shared immutable descriptor/schema
contract. Seven Targets include manifests and documentation; only two are Rust
production modules, and the acceptance items are one atomic reusable seam.

**Acceptance:**

- 1.1.1 - A family crate can publish one immutable server descriptor containing
  typed async handlers, descriptions, brief text, output schema, and read-only
  metadata without depending on gdaemon. symbol: `ToolFamily`.
- 1.1.2 - Request types derive the normalized input schema used for both
  validation and discovery; malformed arguments fail before deserialization or
  handler execution. test: `crates/gcore/tests/mcp_contract.rs::derived_schema_validates_before_dispatch`.
- 1.1.3 - Canonical Rust definition hashes equal committed Python parity vectors
  and are invariant to JSON key order. test: `crates/gcore/tests/mcp_contract.rs::definition_hash_matches_python_vectors`.
- 1.1.4 - Duplicate server/tool names, invalid public names, and schemas that
  expose `ToolContext` fail registry construction. test: `crates/gcore/tests/mcp_contract.rs::invalid_descriptors_fail_startup`.
- 1.1.5 - The durable contract documents the family-author checklist, schema
  compatibility rule, per-server backend states, handler context, and hub-only
  placement. behavior: "internal MCP family contract" in `docs/contracts/internal-mcp.md`.

## P2: Native gateway and strangler
`kind: framing`

### 2.1 Native policy, discovery, and dispatch gateway [category: code] (depends: 1.1, #21566, #24151)
`kind: deliverable`

Targets:
- `crates/gdaemon/Cargo.toml`
- `crates/gdaemon/src/lib.rs`
- `crates/gdaemon/src/front_door/mod.rs::FrontDoorState`
- `crates/gdaemon/src/front_door/mod.rs::FrontDoorState::new`
- `crates/gdaemon/src/front_door/routes.rs::FAMILIES`
- `crates/gdaemon/src/mcp/mod.rs`
- `crates/gdaemon/src/mcp/context.rs`
- `crates/gdaemon/src/mcp/gateway.rs`
- `crates/gdaemon/src/mcp/policy.rs`
- `crates/gdaemon/tests/mcp_gateway.rs`

Consumers unchanged:
- `crates/gdaemon/src/serve.rs` — no-edit-reason: existing constructor calls keep the same `FrontDoorState::new` signature; the constructor initializes the added registry field.

**Research context:**
- Observed: `src/gobby/mcp_proxy/services/tool_proxy.py::ToolProxyService.call_tool`
  is the current single call entry point. Its implementation delegates context,
  argument validation, before/after workflow handling, dispatch, structured
  errors, timeout, and result offloading to focused service modules.
- Observed: `src/gobby/servers/routes/mcp/endpoints/request_context.py::_set_context_for_request`
  treats the session header as caller identity, keeps body `session_id` as tool
  input, resolves post-clear agent-run bindings, and refuses wrapper requests
  without a session.
- Observed: `src/gobby/mcp_proxy/services/schema_guidance.py::record_schema_shown`
  writes `unlocked_tools`; `src/gobby/mcp_proxy/services/session_context.py::record_discovery_state`
  writes `servers_listed` and `listed_servers`. These are hub session variables,
  not process-local caches.
- Read-only dependencies: #21566 supplies the Rust external-MCP multiplexer;
  #24151 supplies the native workflow/rule evaluator and session-variable
  service. This section composes them and does not reimplement either manager.
- Chosen approach: register one proposed `McpFamily` in the existing static
  `FAMILIES` table. The gateway owns the public REST and streamable-HTTP MCP
  adapters, resolves a `ToolContext`, executes one policy pipeline, and chooses
  native family, Python compatibility, or external multiplexer dispatch.
  `crates/gdaemon/src/lib.rs` exports the proposed `mcp` module so integration
  tests and the front-door composition use the same implementation.
- Restart contract: streamable HTTP keeps no authoritative transport session
  state. Discovery, leases, workflow state, and tool data remain in the hub.
  After either a gdaemon or Python-backend restart, a client reconnects and can
  list, lease, and call again. An interrupted call returns a structured
  transport/backend-unavailable result and is never replayed automatically,
  because replay could duplicate a mutation.
- Planned verification:
  `cargo nextest run -p gobby-daemon --test mcp_gateway`,
  `cargo nextest run -p gobby-daemon --test http_contracts`, and
  `cargo fmt -p gobby-daemon -- --check` pass against isolated PostgreSQL.

**Granularity:** Seven acceptance items triggered inspection. Two independently
testable concerns are already separated: 1.1 owns descriptor/schema
construction; 2.1 owns one request-time gateway lifecycle covering context,
policy, discovery, dispatch, and reconnect behavior. The four proposed MCP
modules keep its subconcerns below the production-file ceiling without creating
separate services.

**Acceptance:**

- 2.1.1 - The native gateway serves the existing REST surface and `/mcp` from
  one registry, with unchanged server/tool names and result envelopes. test:
  `crates/gdaemon/tests/mcp_gateway.rs::rest_and_streamable_http_share_one_catalog`.
- 2.1.2 - Authenticated headers and claims resolve one caller `ToolContext`;
  omitted schema-required `session_id`/`project_id` fields are injected before
  validation and backend selection, explicitly supplied target fields remain
  unchanged, and missing agent/wrapper context fails closed. test:
  `crates/gdaemon/tests/mcp_gateway.rs::caller_and_target_session_ids_remain_distinct`.
- 2.1.3 - The gateway runs `before_tool`, re-resolves and revalidates a modified
  call, and dispatches once; a `before_tool` denial or evaluator failure
  dispatches nothing. It then runs `after_tool` once; when that evaluator fails,
  diagnostics retain the failure and the original handler result is preserved
  without redispatch or replay. The test uses a mutating handler to prove the
  side effect occurs exactly once. test: `crates/gdaemon/tests/mcp_gateway.rs::policy_wraps_dispatch_exactly_once`.
- 2.1.4 - `list_mcp_servers`, `list_tools`, and `get_tool_schema` update the
  authoritative session discovery variables and schema lease for the resolved
  caller. test: `crates/gdaemon/tests/mcp_gateway.rs::discovery_updates_hub_session_state`.
- 2.1.5 - Validation errors, timeout errors, offloaded results, and tool failures
  retain the current structured codes and response shapes. test:
  `crates/gdaemon/tests/mcp_gateway.rs::gateway_error_contract_matches_python`.
- 2.1.6 - External server calls and management operations use #21566's
  multiplexer behind the same policy pass; internal and external dispatch cannot
  bypass the gateway. test: `crates/gdaemon/tests/mcp_gateway.rs::external_and_internal_calls_share_policy`.
- 2.1.7 - A fresh client can list, lease, and call after either gdaemon or the
  Python backend restarts; an interrupted call is failed once and is never
  replayed automatically. test: `crates/gdaemon/tests/mcp_gateway.rs::streamable_http_recovers_across_restarts_without_replay`.

### 2.2 Python compatibility backend and per-server cutover [category: code] (depends: 2.1)
`kind: deliverable`

Targets:
- `crates/gdaemon/src/mcp/dispatch.rs`
- `crates/gdaemon/src/mcp/python_backend.rs`
- `crates/gdaemon/src/front_door/routes.rs::unimplemented_families`
- `src/gobby/runner_backend.py::front_door_secret`
- `src/gobby/servers/routes/mcp/tools.py::create_mcp_router`
- `src/gobby/servers/routes/mcp/endpoints/internal_backend.py`
- `crates/gdaemon/tests/mcp_strangler.rs`
- `tests/servers/routes/mcp_endpoints/test_internal_backend.py`
- `tests/e2e/test_internal_mcp_strangler.py`

Consumers unchanged:
- `crates/gdaemon/src/serve.rs` — no-edit-reason: it keeps passing the full route table to `unimplemented_families`; that function learns which `gobby-*` subfamilies the MCP registry implements.
- `src/gobby/hub/node_client.py` — no-edit-reason: its current secret read is unchanged; the compatibility endpoint is an additional consumer.
- `src/gobby/runner_init/servers.py` — no-edit-reason: backend startup continues to latch the same inherited secret.
- `src/gobby/servers/websocket/native_publish.py` — no-edit-reason: native WebSocket publication continues to authenticate with the same secret.
- `tests/e2e/native_ws_probe_bootstrap.py` — no-edit-reason: the existing probe still covers the same secret bootstrap behavior.
- `tests/servers/test_native_ws_publish.py` — no-edit-reason: existing native-publish assertions remain valid and require no MCP-specific edit.
- `tests/test_runner_env_scrub.py` — no-edit-reason: the compatibility route reuses the latched secret and does not change environment scrubbing.
- `src/gobby/servers/_app_routes.py` — no-edit-reason: it continues to mount the router returned by `create_mcp_router` without knowing individual endpoints.
- `src/gobby/servers/routes/mcp/__init__.py` — no-edit-reason: its existing router re-export remains valid.
- `tests/mcp_proxy/test_discovery_deadlines.py` — no-edit-reason: public discovery deadlines are unchanged by the private compatibility route.
- `tests/mcp_proxy/test_oauth_registry.py` — no-edit-reason: external OAuth registry routing is unchanged.
- `tests/servers/routes/mcp_endpoints/test_bridge.py` — no-edit-reason: bridge route registration remains unchanged.
- `tests/servers/routes/mcp_endpoints/test_discovery_routes.py` — no-edit-reason: public discovery route registration remains unchanged and gains focused coverage in the new test.
- `tests/servers/routes/mcp_endpoints/test_http_scope_resolution_matrix.py` — no-edit-reason: public scope resolution is owned by the native gateway, while this Python router consumer still mounts unchanged endpoints.
- `tests/servers/routes/mcp_endpoints/test_registry_routes.py` — no-edit-reason: external server registry routes remain unchanged.
- `tests/servers/routes/mcp_endpoints/test_template_routes.py` — no-edit-reason: template routes remain unchanged.

**Research context:**
- Observed: `src/gobby/mcp_proxy/registries.py::setup_internal_registries`
  composes the 21 public internal registries named in Decision 5 from their
  managers. The compatibility endpoint can reuse those registry objects without
  calling `ToolProxyService` again.
- Observed: `src/gobby/runner_backend.py::front_door_secret` exposes the secret
  inherited by the supervised Python backend; it is the existing trust material
  for a loopback gdaemon-to-backend route.
- Observed: `.gobby/plans/gmcp-stdio.md` fixes every client on the public
  `/api/mcp/...` routes, so backend choice is invisible to `gmcp`. The hub-mode
  architecture keeps PostgreSQL and policy at the hub and sends only bounded
  machine effects to nodes.
- Chosen approach: the public gateway looks up the server's existing
  `front_door.routes` entry. `proxy` uses the secret-bound direct-registry
  endpoint, `compare` returns Python while comparing safe native observations,
  and `native` invokes the Rust descriptor. The endpoint implements list,
  schema, and call only for internal registries and never exposes external
  configuration or workflow enforcement.
- Context projection: gdaemon serializes only the already-resolved caller
  session, project/scope, principal, agent run, machine, intent, and origin into
  a private envelope authenticated by the same loopback secret. The endpoint
  rejects missing, malformed, or contradictory identity fields; reuses
  `resolve_and_seed_contexts`; validates the projected agent run with the
  existing run checks/setter; seeds the validated projected principal directly
  with `set_request_principal`; invokes `InternalToolRegistry.call`; and resets
  every token in `finally`. It does not call the public-request
  `_seed_request_principal` authentication wrapper, which would reinterpret the
  private front-door request instead of preserving the projected principal.
  Concurrent requests receive independent contexts. The endpoint receives
  gdaemon's already normalized tool arguments and never independently rewrites
  `session_id` or `project_id` target inputs.
- Strangler order for every family: port/inject the manager; publish the family
  descriptor with committed Python parity fixtures; set its server entry to
  `compare`; clear list/schema/read-only differences and run mutation tests in
  isolation; set it to `native`; smoke through `gmcp`; then remove that Python
  registry. Other server entries remain `proxy` throughout.
- #21560 consumption: `gtasks` owns the `gobby-tasks` and `gobby-tasks-ops`
  descriptors and parity fixtures but leaves activation to #21570. #21570
  owns this gateway, the backend-state changes, and final compatibility-route
  deletion.
- Planned verification:
  `cargo nextest run -p gobby-daemon --test mcp_strangler`,
  `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/servers/routes/mcp_endpoints/test_internal_backend.py -v`,
  and the same protected pytest prefix for
  `tests/e2e/test_internal_mcp_strangler.py -v` pass with isolated daemons.

**Granularity:** Seven acceptance items share one lifecycle owner: compatibility
dispatch from Python to native per public server. The Rust and Python unit tests
plus one isolated e2e test prove the same cutover state machine; splitting them would create test-only
wrapper work rather than an independently shippable outcome.

**Acceptance:**

- 2.2.1 - The compatibility endpoint refuses non-loopback or wrong-secret
  requests, resolves only an internal registry, and invokes it without a second
  workflow pass. test: `tests/servers/routes/mcp_endpoints/test_internal_backend.py::test_endpoint_is_secret_bound_and_policy_free`.
- 2.2.2 - Valid internal context envelopes seed and reset caller, project,
  principal, and agent-run ContextVars independently across concurrent calls.
  An agent remains that exact agent principal, operator authority exists only
  when explicitly projected, contradictory run/principal/session data and
  missing or forged envelopes fail closed, and body `session_id` remains tool
  input. test: `tests/servers/routes/mcp_endpoints/test_internal_backend.py::test_context_envelope_isolated_and_reset`.
- 2.2.3 - One server can be `native` while every absent or `proxy` server still
  lists schemas and executes through Python under the same public name. test:
  `crates/gdaemon/tests/mcp_strangler.rs::native_and_python_servers_coexist`.
- 2.2.4 - `compare` returns Python, compares list/schema and read-only results,
  records differences, and executes each mutating call only once in Python.
  test: `crates/gdaemon/tests/mcp_strangler.rs::compare_never_duplicates_mutation`.
- 2.2.5 - A node has no internal registry or hub credential; a node-side `gmcp`
  call reaches the hub registry, while standalone serves the same descriptor
  locally. test: `tests/e2e/test_internal_mcp_strangler.py::test_hub_node_and_standalone_placement`.
- 2.2.6 - Native dispatch remains available when Python is down; a still-proxied
  server returns a typed backend-unavailable error rather than falling back to
  another machine or bypassing policy. test: `crates/gdaemon/tests/mcp_strangler.rs::backend_loss_is_server_scoped`.
- 2.2.7 - The `gobby-tasks` sample parity corpus proves identical tool inventory,
  schemas, structured errors, and representative read/write results before its
  server entry can become `native`. test: `tests/e2e/test_internal_mcp_strangler.py::test_tasks_family_cutover_contract`.

## P3: Coherent release proof
`kind: framing`

### 3.1 Rebuild and version every affected shipped binary [category: code] (depends: 1.1, 2.1, 2.2)
`kind: deliverable`

Targets:
- `Cargo.lock`
- `crates/gcode/Cargo.toml`
- `crates/gdaemon/Cargo.toml`
- `crates/ghook/Cargo.toml`
- `crates/gclient/Cargo.toml`
- `crates/gmcp/Cargo.toml`
- `docs/contracts/internal-mcp.md`
- `src/gobby/install/version_pins.py::MANAGED_BIN_VERSION_PINS`

Consumers unchanged:
- `src/gobby/cli/install_setup_gclient.py` — no-edit-reason: installer freshness checks already read the gclient pin dynamically.
- `src/gobby/cli/install_setup_gcode.py` — no-edit-reason: installer freshness checks already read the gcode pin dynamically.
- `src/gobby/cli/install_setup_gdaemon.py` — no-edit-reason: installer freshness checks already read the gdaemon pin dynamically.
- `src/gobby/cli/install_setup_ghook.py` — no-edit-reason: installer freshness checks already read the ghook pin dynamically.
- `src/gobby/cli/install_setup_gmcp.py` — no-edit-reason: installer freshness checks already read the gmcp pin dynamically.
- `src/gobby/cli/install_setup_gterm.py` — no-edit-reason: the unrelated gterm pin and its dynamic installer lookup do not change.
- `src/gobby/cli/install_setup_versions.py` — no-edit-reason: the shared minimum-version predicate already reads every managed pin dynamically.
- `src/gobby/code_index/gcode_versions.py` — no-edit-reason: both gcode maintenance floors already derive from `MANAGED_BIN_VERSION_PINS["gcode"]` and advance automatically with the pin.
- `src/gobby/hooks/runtime_compat.py` — no-edit-reason: its ghook compatibility floor already derives from the managed ghook pin.
- `src/gobby/install/bin_freshness_models.py` — no-edit-reason: freshness records already enumerate the managed pin map dynamically.
- `src/gobby/install/distribution.py` — no-edit-reason: distribution inspection already selects each managed binary's minimum from the shared pin map.
- `tests/cli/installers/test_grok_installer.py` — no-edit-reason: the unrelated grok installer expectations continue to read the unchanged gterm pin fixture path.
- `tests/cli/test_install_ghook.py` — no-edit-reason: ghook install tests already derive expectations from the managed pin.
- `tests/cli/test_install_setup.py` — no-edit-reason: shared setup tests already derive expected managed versions from the pin map.
- `tests/cli/test_install_setup_gclient.py` — no-edit-reason: gclient setup tests already derive expectations from the managed pin.
- `tests/cli/test_install_setup_gdaemon.py` — no-edit-reason: gdaemon setup tests already derive expectations from the managed pin.
- `tests/cli/test_install_setup_gmcp.py` — no-edit-reason: gmcp setup tests already derive expectations from the managed pin.
- `tests/cli/test_install_setup_gterm.py` — no-edit-reason: the unrelated gterm pin and assertions do not change.
- `tests/code_index/test_gcode_gateway.py` — no-edit-reason: gcode compatibility expectations already derive from the managed gcode pin.
- `tests/hooks/test_runtime_compat.py` — no-edit-reason: hook compatibility expectations already derive from the managed ghook pin.
- `tests/install/test_distribution.py` — no-edit-reason: distribution tests already parameterize expectations from the managed pin map.
- `tests/install/test_version_pins.py` — no-edit-reason: the existing generic parity test covers every changed crate pin without a test edit.

**Research context:**
- Observed: all five listed binaries depend directly on `gobby-core`; the new
  shared MCP module changes their recursive source identity even when only
  gdaemon calls the new API.
- Constraint: the repository release contract requires a `0.0.1` bump for each
  affected shipped binary, a matching lockfile, complete rebuilds, and coherent
  promotion. `gcode`, `gdaemon`, and `ghook` share the installed-set identity;
  `gclient` and `gmcp` are promoted through their component paths.
- Observed planning baselines on 2026-10-23 were `gcode` 1.9.49, `gdaemon`
  0.4.84, `ghook` 0.9.37, `gclient` 0.1.71, and `gmcp` 0.1.2. They are evidence,
  not prescribed shipping versions; prerequisite releases may advance them.
- Chosen approach: make one final shipping change after all source/parity checks,
  then have the Orchestrator announce and perform the required cutover. No
  developer directly overwrites installed binaries or restarts the daemon.
- Planned verification: targeted build and test commands from 1.1, 2.1, and 2.2
  are green; `uv run gobby cutover` preflight accepts the candidate source and
  schema identity; post-cutover `gmcp` list/schema/call smoke reaches one native
  and one compatibility server.

**Granularity:** This is one release-coherence obligation over six manifest
files, not a second implementation state machine.

**Acceptance:**

- 3.1.1 - At the shipping commit, `gcode`, `gdaemon`, `ghook`, `gclient`, and
  `gmcp` are each exactly one patch version above that binary's immediately
  preceding released version; their Cargo manifests, `Cargo.lock`, and
  `MANAGED_BIN_VERSION_PINS` agree, and the gcode maintenance floors derive the
  resulting gcode pin unchanged.
  behavior: "affected binary versions" in `Cargo.lock`.
- 3.1.2 - The coherent installed trio is promoted through
  `promote_workspace_binary_set`, and the separately promoted `gclient` and
  `gmcp` pass their identity guards; no binary is copied in place. behavior:
  "coherent native component promotion" in `docs/contracts/internal-mcp.md`.
- 3.1.3 - After the announced cutover, `gmcp` lists the unchanged catalog,
  leases a native schema, calls one native read/write tool, and calls one
  still-proxied tool successfully. test: `tests/e2e/test_internal_mcp_strangler.py::test_post_cutover_mixed_catalog_smoke`.

## V1 Plan Changelog
`kind: framing`

2026-10-23 — The Plan Writer and Plan Adversary reached consensus on the
narrative after resolving IMCP-01 through IMCP-06; no unresolved governing
questions remain, and project-aware base validation passed warning-free.

## V2 Verification
`kind: verification`

- Base plan validation: `uv run gobby plans validate .gobby/plans/rust-internal-mcp-servers.md -p /Users/josh/Projects/gobby`.
- Expansion validation remains unrun until the Plan Adversary derives and
  applies `## M1 Task Manifest` from consensus bytes.
- Focused implementation checks are owned by their deliverable sections. No
  full pytest or whole-workspace Rust suite is required.
