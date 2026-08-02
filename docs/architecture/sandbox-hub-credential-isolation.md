# Sandbox Hub Credential Isolation

Status: proposed for user acceptance
Task: #19353
Date: 2026-08-02

## Decision

Gobby will keep gcode's direct PostgreSQL data path and replace the shared hub
credential with a daemon-issued, run-scoped PostgreSQL login. The login will be
bound to one agent run and one project by database grants plus row-level
security. It will expire in PostgreSQL and will be revoked when the run reaches
any terminal state.

A migration-owned, non-login issuer role will own fixed security-definer
lifecycle functions. The daemon runtime role will receive execute permission
on those functions; it will not receive `CREATEROLE`, role membership in the
issuer, or arbitrary role-management authority.

The operator bootstrap connection remains a migration and maintenance
credential. Before the daemon accepts traffic, each served database connection
will assume a stable non-login daemon runtime role and verify that effective
identity on checkout. Request handling and agent credential lifecycle calls
will execute only under that runtime role.

The same change will narrow sandbox filesystem grants to explicit runtime
paths. The operator bootstrap and secret KEK will stay in daemon custody. Agent
runtime homes will contain a short-lived scoped DSN and no KEK link. Secret
references needed by gcode will be resolved through a typed daemon endpoint
authorized by the existing run-bound agent capability. That endpoint will
return non-secret configuration and run/project-scoped service capabilities.
Long-lived shared service credentials will remain in daemon custody.

Provider-native execution will be fail-closed. A provider-native backend may
run managed work only after a preflight proves that the operator bootstrap,
the KEK, other agents' runtime homes, and the managed SRT installation are
outside its readable and writable surfaces. Backends without that capability
will be rejected for managed execution. Daemon-side web-chat tools will use the
same credential and path policy through a per-request managed-execution lease.

This decision makes database authorization the durable boundary. Filesystem
isolation remains defense in depth, and loopback reach no longer converts a
file disclosure into operator-level hub access.

## Security invariants

1. A managed agent or daemon tool execution can obtain only a credential for
   its own run/request and project.
2. The operator DSN in the daemon bootstrap is absent from every agent read
   surface and every agent-generated runtime file.
3. The secret KEK is absent from every agent read surface, environment, runtime
   directory, and daemon response.
4. Disclosure of a run-scoped DSN yields only the documented gcode capability
   for that project until revocation or expiry.
5. PostgreSQL enforces project and command scope independently of SRT,
   provider-native policy, daemon availability, and loopback filtering.
6. Managed execution fails before provider launch or a daemon tool loop when
   credential issuance, sandbox preflight, or runtime materialization fails.
7. Every terminal agent transition attempts immediate revocation; PostgreSQL
   expiry and daemon-start reconciliation provide independent backstops. Every
   daemon tool request revokes in an outer `finally` block.
8. RLS identity derives from the authenticated PostgreSQL `session_user`; no
   caller-supplied run, project, role name, or session setting selects scope.
9. Hub backup and restore cannot preserve or resurrect an ephemeral agent
   login.

## Current exposure at HEAD

### Operator bootstrap and generated gcode bootstrap

The daemon's root bootstrap is `GOBBY_HOME/bootstrap.yaml`. `load_bootstrap()`
parses its inline `database_url`, requires it for runtime database startup, and
enforces mode `0600` (`src/gobby/config/bootstrap.py:86-154,218-233`).
`load_config(..., resolve_database_url=True)` then copies that value into the
runtime config (`src/gobby/config/app.py:518-603`), and
`runtime_hub_database()` opens the hub from it
(`src/gobby/storage/hub/runtime.py:20-37`). Mode `0600` protects against other
OS users; it does not protect against a sandboxed child running as the same
user once the path is granted.

Spawned agents receive a second copy. `_prepare_gcode_runtime()` writes the
supplied daemon `database_url` into
`GOBBY_HOME/gcode-runtime/<workspace>/bootstrap.yaml` and points the gcode
wrapper at that runtime home (`src/gobby/agents/code_index.py:324-370`). In
daemon mode, Rust gcode resolves the database URL from environment, the daemon
configuration response, or that bootstrap file
(`crates/gcode/src/db/resolution.rs:22-33,72-128`). The copied value currently
has the same hub authority as the daemon value.

### KEK exposure

The key-file KEK lives at `GOBBY_HOME/.secret_kek`. The runtime builder imports
`SECRET_MATERIAL_FILENAMES` and `_link_runtime_assets()` symlinks that file
into each generated gcode runtime home
(`src/gobby/agents/code_index.py:26,453-465`; asserted by
`tests/agents/test_isolation.py:167-205`). Rust gcore reads that path directly,
unwraps the database-stored DEK, and decrypts secret values
(`crates/gcore/src/secrets.rs:48-57,89-100,160-204`). Gcode calls this path when
PostgreSQL-backed service configuration contains a secret reference
(`crates/gcode/src/config/services.rs:21-66`). Possession of the KEK plus hub
read access therefore exposes the encrypted secret store beyond gcode's
intended service settings.

### Filesystem grants

The canonical policy starts by denying the user home and the persistent
`gcode-runtime` root in `sensitive_home_roots()`
(`src/gobby/agents/sandbox_policy.py:195-208`). It then reverses that boundary:

- `gobby_read_exceptions()` adds the entire Gobby home and the current gcode
  runtime home to `read_paths`
  (`src/gobby/agents/sandbox_policy.py:251-274`).
- `gobby_write_exceptions()` adds the entire Gobby home to `write_paths`
  (`src/gobby/agents/sandbox_policy.py:225-248`).
- `sensitive_write_roots()` protects several credential roots and
  `GOBBY_HOME/local_cli_token` from writes. It omits
  `GOBBY_HOME/.secret_kek`, `GOBBY_HOME/bootstrap.yaml`, and the managed SRT
  installation (`src/gobby/agents/sandbox_policy.py:211-222`).
- `compute_sandbox_paths()` includes every write path in the read set, then
  emits the broad deny and allow lists together
  (`src/gobby/agents/sandbox.py:573-687`).

The regression contract explicitly requires the whole Gobby home to be
readable and writable and checks that `local_cli_token` remains readable
(`tests/agents/test_sandbox.py:854-881`). Under SRT v0.0.66 precedence,
`allowRead` wins over an overlapping `denyRead`; another deny entry cannot
repair the exposure while the whole Gobby home remains allowed. The allow
surface must shrink.

The broad write grant also contains the managed SRT installation.
`srt_install_root()` resolves below `GOBBY_HOME/tools/srt`
(`src/gobby/agents/srt_runtime.py:103-104`). The verifier checks receipt
fields, package identity, `runner.mjs`, and `package-lock.json`, while installed
package contents under `node_modules` receive no content hash verification
(`src/gobby/agents/srt_runtime.py:137-227`). An SRT-confined process can modify
an unverified installed module and leave a payload for a later launch.

### Loopback reach

`compute_sandbox_paths()` records daemon HTTP and WebSocket ports, currently
60887 and 60888, in `loopback_ports`
(`src/gobby/agents/sandbox.py:573-687`). SRT rendering does not enforce that
list. It sets `allowLocalBinding=true`, which permits loopback as a class so
ghook and the MCP server can reach the daemon and gcode can reach PostgreSQL
directly (`src/gobby/agents/srt_runtime.py:245-288`). The hub's loopback port
is consequently reachable from SRT. Any process that reads the current DSN can
authenticate directly.

Loopback denial is unsuitable as the primary fix. SRT exposes a boolean rather
than a port allowlist, and provider-native backends have different network
models. The selected design assumes loopback reach and enforces authority at
the daemon and PostgreSQL authentication layers.

### Affected execution backends

- **Managed SRT:** directly affected. The rendered policy carries the whole
  Gobby home in `allowRead` and `allowWrite`; SRT precedence makes the allow
  effective. Loopback permits direct PostgreSQL access.
- **Claude provider-native:** affected by Gobby's generated policy.
  `ClaudeSandboxResolver` passes every external write path, including the whole
  Gobby home, as `filesystem.allowWrite`
  (`src/gobby/agents/sandbox.py:285-345`).
- **Codex provider-native:** affected by Gobby's generated policy.
  `CodexSandboxResolver` emits `--add-dir` for every external write path,
  including the whole Gobby home (`src/gobby/agents/sandbox.py:348-392`). Its
  resolver exposes no Gobby deny-read surface.
- **Qwen provider-native:** affected by the broad external write inventory
  passed through `--include-directories`
  (`src/gobby/agents/sandbox.py:395-431`).
- **Droid provider-native:** unsupported by Gobby's policy resolver.
  `get_sandbox_resolver("droid")` raises instead of producing an enforceable
  policy (`src/gobby/agents/sandbox.py:547-570`;
  `tests/agents/test_sandbox.py:791-794`). A Droid path launched without SRT
  therefore has no Gobby filesystem boundary and is fully affected.
- **Grok provider-native:** unproven. Its resolver selects only a named
  `strict` or `workspace` profile and ignores `ResolvedSandboxPaths`
  (`src/gobby/agents/sandbox.py:434-452`), so Gobby cannot assert
  sensitive-root exclusion.
- **Disabled or unsupported sandbox:** fully affected by the host user's
  filesystem and loopback authority. AGY currently refuses terminal and web
  transport rather than launching (`src/gobby/agents/spawn_executor.py:116-123`;
  `src/gobby/servers/websocket/chat/runtime_manager.py:244-295`).
- **Web-chat tools:** affected independently of the provider process. Web chat
  defaults to `enabled/provider-native/network-enabled`
  (`src/gobby/agents/sandbox.py:157-165`), and session creation records the
  configured `enabled` bit
  (`src/gobby/servers/routes/agent_spawn.py:220-249`). `/api/llm/chat`
  dispatches through `ToolChatService`
  (`src/gobby/servers/routes/llm.py:273-346`); CLI tools use raw daemon-side
  subprocesses (`src/gobby/ai/_tool_chat_tools.py:237-294`). Those subprocesses
  execute outside SRT. `ToolChatRequest` carries a caller-supplied
  `project_path` and no agent-run or project-capability identity
  (`src/gobby/ai/_tool_chat_contracts.py:107-135`).

The architecture must cover every row. An SRT-only deny rule would leave
provider-native and daemon-side paths with operator credentials.

## Design evaluation

### Scoped database credential: PostgreSQL role with daemon-mediated secret resolution

The daemon requests a login role for each managed agent run or daemon tool-chat
request through fixed issuer functions. A non-login capability role defines
gcode's table and function privileges. PostgreSQL row-level security maps the
authenticated `session_user` to the managed execution's project and rejects
cross-project reads and writes, including after `SET ROLE` and inside
security-definer functions. The login
uses `NOSUPERUSER`, `NOCREATEDB`, `NOCREATEROLE`, `NOREPLICATION`, and
`NOBYPASSRLS`, with `VALID UNTIL` bounded by the run deadline and a one-hour
maximum lifetime. Rotation begins by 45 minutes and revokes the predecessor
after at most a five-minute connection-drain interval.

The agent's runtime bootstrap carries this scoped DSN. Gcode continues to use
its existing synchronous PostgreSQL client and SQL paths
(`crates/gcode/src/db/mod.rs:18-33`; `crates/gcore/src/postgres.rs:16-27`).
Secret-backed service settings are resolved by a typed daemon endpoint using
the existing run-bound capability, whose claims already include run, session,
project, issue time, and expiry; terminal launch injects that capability into
the run environment (`src/gobby/utils/local_token.py:51-133`;
`src/gobby/agents/constants.py:102-169`). The endpoint returns only the gcode
service capabilities authorized for that execution. Daemon tool chat mints an
equivalent request-bound capability from its authenticated session. A backing
service may enter managed mode only with a run/project-scoped
credential. When FalkorDB, Qdrant, or an embedding provider cannot mint that
scope, the daemon brokers the narrow service operation and keeps the shared
credential. The endpoint never exposes a general secret lookup primitive or a
long-lived shared service credential.

Assessment:

- Backend-agnostic authority: PostgreSQL applies grants, RLS, expiry, and
  revocation regardless of the launching sandbox. Provider preflight still
  excludes daemon-owned files.
- Direct-PostgreSQL performance: preserved for search, symbol retrieval,
  indexing, and projection metadata. Credential lifecycle, capability
  resolution, and unscopable non-PostgreSQL service operations cross the
  daemon.
- Blast radius: a disclosed runtime DSN reaches one project and the gcode SQL
  capability for one bounded lifetime.
- Implementation cost: requires a privilege inventory, RLS policies, a role
  manager, lifecycle reconciliation, gcode config-source changes, and narrow
  broker adapters for unscopable external services.

### Daemon-proxied gcode database access

In this design, gcode sends every database operation to the daemon. The agent
holds only its run-bound daemon capability; the daemon owns the sole
PostgreSQL pool and the KEK.

Assessment:

- Backend-agnostic authority: strong. Database and KEK material never enter
  the provider process.
- Direct-PostgreSQL performance: lost in the client. Every search result,
  index batch, freshness read, graph reconciliation, and status query gains an
  RPC and serialization boundary. Preserving throughput would require a
  long-lived daemon gcode service plus a new protocol for the broad existing
  command surface.
- Blast radius: daemon authorization can bind every request to a run and
  project.
- Implementation cost: highest. It replaces gcode's database abstraction,
  duplicates streaming/error semantics, and couples standalone gcode to a
  running daemon.

This design is rejected because it sacrifices the direct-PostgreSQL
performance requirement and creates a second gcode transport contract.

### Filesystem narrowing with the existing shared DSN

This design removes the whole-home allow entries, grants the current generated
runtime home, and leaves the existing shared DSN in that runtime bootstrap.
It also removes the KEK link and asks the daemon to resolve gcode settings.

Assessment:

- Backend-agnostic authority: weak. Any provider-native read escape or
  daemon-side subprocess disclosure recovers the shared hub credential.
- Direct-PostgreSQL performance: preserved.
- Blast radius: unchanged after one credential disclosure.
- Implementation cost: lowest, with security dependent on every sandbox
  backend remaining perfect.

This design is rejected because the database credential stays operator-wide
and turns any backend policy defect into full hub access.

## Selected architecture

### Database principals and authorization

Add a PostgreSQL-owned registry that records login role name, owner kind,
managed execution ID, optional agent run ID, session ID, project ID, issued
time, expiry, revoked time, and credential generation. The plaintext password
exists only during issuance and runtime materialization; the registry stores
no password or DSN.

Use one stable `NOLOGIN` capability role for agent gcode operations, one
stable `NOLOGIN` issuer role, one stable `NOLOGIN` daemon runtime role, and one
ephemeral `LOGIN` role per managed execution. The issuer owns the registry and
fixed issue, rotate, revoke, and reconcile functions. It has only the
role-management and backend-termination privileges needed by those functions.
After privileged startup migrations, every served daemon pool connection
assumes the daemon runtime role and verifies `current_user` on checkout. That
role can execute the lifecycle functions but cannot become the issuer, reset
to operator authority through application APIs, or supply a role name;
functions derive the reserved role name from a validated execution record and
quote every identifier.

The gcode capability role receives an explicit SQL inventory derived from
every gcode query at HEAD:

- read/write access to code-index rows for the bound project;
- read access to the minimal project identity and non-secret configuration
  required to resolve that project;
- execute access to narrowly defined helper functions needed by gcode;
- no access to task, memory, session, agent, operator-auth, secret-envelope,
  migration, backup, or maintenance data;
- no global prune, setup, schema migration, or cross-project administration.

RLS policies resolve the project from `session_user` through the registry.
Security-definer helpers use a fixed `search_path`, reject missing, expired,
revoked, or duplicated bindings, and return only the bound project. Policies
cover project-bearing tables directly and dependent code-index rows through
their project-bearing parent. The login and capability roles remain
`NOBYPASSRLS` and own no protected table.

The migration also normalizes database defaults: agent roles receive only
`CONNECT`, explicit schema `USAGE`, and enumerated table, sequence, and
function privileges. They receive no database `TEMP`, public-schema `CREATE`,
unreviewed function `EXECUTE`, large-object, extension-management, or indirect
role membership authority. Gcode commands whose semantics are global fail
clearly under an agent role. Standalone/operator gcode retains its existing
administrative path outside managed agents.

### Runtime handoff

Issue the PostgreSQL role before provider launch. Write a private, per-run
runtime bootstrap containing the daemon endpoint and scoped DSN. The path is
the only gcode runtime home granted to that run. Do not copy the daemon
bootstrap, operator token, KEK, sibling runtime homes, services directory, or
managed tool installation into it.

Daemon tool chat performs the same handoff into a private per-request runtime
before its tool loop begins.

The handoff format is backend-neutral. SRT, provider-native CLIs, resumed
agents, and daemon-side tool-chat gcode calls receive the same runtime-home
contract. Agent runtimes are owned by their agent run. Tool-chat runtimes are
owned by a request-scoped managed-execution lease. A generation number supports
rotation without reusing credentials.

The runtime builder must atomically publish a successor bootstrap before
retiring the prior role. Gcode opens new connections with the current
generation; existing connections are terminated during revocation or after a
bounded rotation grace period.

### KEK custody and gcode service configuration

Remove `SECRET_MATERIAL_FILENAMES` from `_link_runtime_assets()` and delete the
runtime KEK symlink/copy fallback. The daemon remains the only component that
loads `GOBBY_HOME/.secret_kek` or the configured passphrase posture.

Add one authenticated daemon operation that returns a typed gcode service
capability bundle for the caller's run and project. The daemon resolves only
the existing gcode settings required for FalkorDB, Qdrant, embeddings, and
code-vector configuration. Authorization derives identity from the verified
agent token; request parameters cannot select another run or project.
Responses may contain non-secret settings and scoped service credentials whose
authority and expiry do not exceed the run. Responses exclude shared service
credentials, secret names, wrapped DEKs, KEK material, unrelated
configuration, and the operator database URL.

For a service without run/project-scoped credentials, the bundle supplies a
typed daemon capability instead of its secret. Gcode calls that narrow daemon
operation for the affected projection or embedding action. Unsupported
secret-dependent features fail closed. PostgreSQL search, retrieval, and
index-table writes stay on the direct connection path.

The Rust config source uses this bundle before attempting PostgreSQL-backed
secret expansion. Managed mode rejects local KEK resolution. Standalone and
operator invocations retain explicit local secret resolution outside an agent
runtime.

### Filesystem and backend enforcement

Replace whole-home entries in `gobby_read_exceptions()` and
`gobby_write_exceptions()` with an explicit inventory of run-owned state. The
runtime builder materializes every required non-secret Gobby asset below the
current run root. The agent receives read-only access to its bootstrap, prompt,
machine identity, wrapper, and policy files, plus write access only to sibling
`tmp`, hook-spool, log, and per-run tool-cache directories. The workspace and
provider auth/config paths remain governed by their dedicated helpers.
Toolchain executables, resolved interpreter/package targets, and shared caches
are read-only; a provider or MCP subprocess that needs a writable cache uses
the per-run cache. No other Gobby-home or shared mutable tool path is allowed.

Add `GOBBY_HOME/bootstrap.yaml`, `GOBBY_HOME/.secret_kek`,
`GOBBY_HOME/local_cli_token`, the persistent gcode-runtime root, and
`GOBBY_HOME/tools/srt` to the sensitive-root contract. Parent-directory allow
entries are forbidden when they contain a sensitive descendant. Tests must
assert effective access with real backend semantics; list membership alone is
insufficient under SRT precedence.

Move SRT installation outside every agent write root or make the installation
tree immutable to the agent principal. Extend verification to cover all
runtime-loaded package content through a pinned manifest. Launch fails on a
missing, extra, writable, or mismatched installed file.

Each provider-native resolver declares and preflights its effective sensitive
read/write exclusions. A resolver that cannot express or verify those
exclusions returns an unsupported-backend error. Session metadata records
`launch.enforced` and backend identity after preflight; it never derives the
security label from `config.enabled` alone.

Daemon-side tool-chat execution receives a managed runtime context. Gcode CLI
subprocesses receive the request lease's scoped runtime home. Built-in
filesystem tools enforce the same sensitive-root deny set before touching a
path. Arbitrary raw subprocess execution is unavailable to a managed web-chat
tool policy.

### Lifecycle

Issuance:

1. Create the agent-run record and determine its project and deadline.
2. In one daemon-controlled operation, create the ephemeral PostgreSQL login,
   register its binding, grant the capability role, and set `VALID UNTIL`
   through the fixed issuer function.
3. Materialize the runtime bootstrap with mode `0600` and the authenticated
   service-capability endpoint.
4. Preflight database scope and the selected sandbox backend.
5. Launch the provider only after every step succeeds.

Daemon tool-chat issuance:

1. Resolve the authoritative project ID from the authenticated session and
   canonical project path before accepting a tool request.
2. Create a request-scoped managed-execution record, capability, PostgreSQL
   role, and runtime home before entering the tool loop.
3. Pass only that runtime to daemon-side gcode subprocesses and the verified
   execution identity to built-in path guards.
4. Revoke the role, capability, and runtime in an outer `finally` block on
   success, error, cancellation, client disconnect, or daemon shutdown.

Rotation:

1. Create a new role and generation by 45 minutes or earlier when the run
   deadline requires it.
2. Atomically replace the managed-execution bootstrap.
3. Allow a connection-drain interval of at most five minutes.
4. Revoke the old role, terminate its sessions, and remove its registry row
   after audit retention is recorded.

Revocation and exit:

1. Agent exit is a terminal transition. `complete`, `fail`, `timeout`, `cancel`,
   explicit termination, spawn rollback, and watchdog cleanup enqueue an
   idempotent revocation.
2. Revocation disables login, removes role membership, terminates active
   sessions for that role, drops the role, and deletes the runtime bootstrap.
3. The run reaches its terminal state even when the hub is temporarily
   unavailable; the failed revocation remains durable for retry.
4. Daemon startup reconciles every non-active agent role against agent-run
   state and removes expired or orphaned roles before accepting new spawns.
5. PostgreSQL `VALID UNTIL` bounds authentication during daemon failure, and a
   server-side reaper terminates connections that outlive the recorded expiry.

Backup and restore:

1. Hub backup enters maintenance, stops new issuance, terminates managed runs,
   and revokes every ephemeral agent role before the existing
   `pg_dumpall --globals-only` step
   (`src/gobby/cli/hub_backup/_stores.py:144-184`).
2. Backup aborts when the registry or PostgreSQL catalog still contains a
   reserved-prefix agent login. Stable issuer, daemon runtime, and capability
   roles remain in the globals artifact; ephemeral logins never enter it.
3. Restore reconciliation drops any reserved-prefix login and clears stale
   bindings before the daemon accepts agent work, including restores of a
   legacy artifact that predates the backup exclusion.

### Failure behavior

- Role issuance or scope verification failure aborts the spawn and removes any
  partially created role and runtime file.
- Typed service-capability failure disables secret-dependent gcode features
  with a precise error; it does not fall back to local KEK access.
- Daemon loss leaves existing direct PostgreSQL work usable until the role
  expires. New service-capability resolution and rotation fail closed.
- Provider-native preflight failure rejects that backend before a session is
  labeled sandboxed.
- Revocation retry is idempotent across already-terminated connections,
  missing runtime files, and already-dropped roles.

## Implementation epic definition

The implementation epic can be expanded directly from the work packages
below. Each package produces its listed exit evidence before dependent work
begins.

### WP1 — Scoped PostgreSQL authorization substrate

Scope:

- Add a migration and baseline schema for agent DB principal bindings, the
  non-login issuer and gcode capability roles, fixed lifecycle functions,
  grants, RLS policies, expiry metadata, and audit events.
- Normalize database/schema/function default privileges and prove the daemon
  runtime role has lifecycle-function execution without `CREATEROLE`, issuer
  membership, or raw role-management privileges.
- Separate privileged migration/maintenance connections from served daemon
  traffic. Assert the non-login daemon runtime role on every pool checkout and
  reject a connection with any other effective identity.
- Inventory every SQL statement reachable from managed gcode commands and map
  it to project-scoped read, project-scoped write, or operator-only behavior.
- Add database tests proving same-project operations work and cross-project,
  task, memory, session, secret, migration, backup, and administrative access
  fail.

Exit evidence:

- A machine-readable privilege manifest covers every managed gcode SQL path.
- Tests execute representative search, symbol, index, freshness, graph, vector,
  and status commands through an ephemeral role.
- Direct SQL adversary tests cannot bypass project scope through crafted IDs,
  `search_path`, transactions, prepared statements, `SET ROLE`, caller-set
  session values, public defaults, or role changes.

### WP2 — Credential manager and run lifecycle

Scope:

- Add a daemon credential manager for issuance, atomic rotation, revocation,
  expiry, connection termination, and orphan reconciliation.
- Integrate issuance before provider launch and revocation with
  `src/gobby/storage/agents/_lifecycle.py` terminal transitions, spawn rollback,
  watchdog cleanup, and daemon startup.
- Add request-scoped managed-execution leases for `ToolChatService`, with
  authoritative project resolution and unconditional `finally` revocation.
- Persist only role identity and lifecycle metadata. Redact DSNs and passwords
  from logs, events, traces, errors, session metadata, and task evidence.
- Enforce a one-hour maximum role lifetime, rotation by 45 minutes, and a
  maximum five-minute predecessor drain.

Exit evidence:

- Tests cover success plus partial-create rollback, repeated revocation,
  daemon restart, hub outage, hard-killed agent, rotation race, expired live
  connection, tool-request cancellation/disconnect, and orphan role recovery.
- No terminal run or completed tool request retains a login-capable PostgreSQL
  role after reconciliation.
- The password and full DSN exist only in bounded issuance/runtime buffers and
  the mode-`0600` managed-execution bootstrap.

### WP3 — DSN handoff and gcode direct connection

Scope:

- Change `src/gobby/agents/code_index.py` to accept a scoped credential object
  and write only the run-scoped DSN to the generated runtime bootstrap.
- Remove the operator `database_url` from all agent environment, wrapper,
  preflight, daemon-config, and generated-file paths.
- Update `crates/gcode/src/db/resolution.rs` to distinguish managed scoped
  runtime from standalone/operator resolution and reject shared bootstrap
  fallback in managed mode.
- Preserve the direct connection path in `crates/gcode/src/db/mod.rs` and
  `crates/gcore/src/postgres.rs`.

Exit evidence:

- Spawn, resume, terminal-agent, and daemon-side web-chat gcode paths use the
  same scoped runtime contract.
- Managed gcode cannot resolve the root daemon bootstrap even when its runtime
  bootstrap is absent or malformed.
- Performance tests compare direct scoped-role search and index throughput to
  the current direct path. Identical-dataset p95 latency and bulk-index
  throughput may regress by no more than 10%, and PostgreSQL round-trip counts
  per operation must remain unchanged.

### WP4 — KEK removal and typed service capabilities

Scope:

- Remove `.secret_kek` from runtime asset linking and delete stale links and
  copies from every managed runtime root.
- Add an authenticated, execution/project-bound daemon service-capability endpoint
  with an explicit response schema and configuration-key allowlist.
- Classify every gcode service field as non-secret, run/project-scoped, or
  daemon-brokered. Never return a long-lived shared service credential.
- Add narrow broker operations for configured FalkorDB, Qdrant, or embedding
  capabilities that cannot issue run/project-scoped credentials.
- Update `crates/gcode/src/config/services.rs` and gcore configuration plumbing
  so managed mode consumes the capability bundle and rejects local KEK/secret
  expansion.
- Keep standalone/operator secret handling on its explicit non-agent path.

Exit evidence:

- Filesystem snapshots and symlink-resolution tests find no KEK material in
  any agent runtime or granted path.
- Endpoint tests reject operator tokens exposed through an agent context,
  expired tokens, mismatched run/project claims, arbitrary secret names, and
  unapproved configuration keys.
- Search and indexing work without KEK access. Secret-dependent embeddings and
  projections use a scoped credential or their typed broker operation.
- Runtime snapshots and responses contain no long-lived shared service
  credential.

### WP5 — Filesystem, SRT integrity, and provider-native gates

Scope:

- Replace whole-home grants in `src/gobby/agents/sandbox_policy.py` with
  explicit run-owned paths and add the sensitive-root parent/descendant
  invariant.
- Split the generated run root into daemon-written read-only assets and the
  four writable directories for tmp, hook spool, logs, and tool cache. Propagate
  the per-run cache to provider and MCP subprocesses.
- Replace the readability assertion at
  `tests/agents/test_sandbox.py:854-881` with effective-denial tests for the
  operator bootstrap, operator token, KEK, sibling runtimes, and SRT install.
- Make the SRT installation immutable from managed runs and extend
  `src/gobby/agents/srt_runtime.py` verification across loaded package content.
- Add provider-native capability declarations and effective preflights for
  Claude, Codex, Droid, Qwen, and Grok. Reject any backend that cannot prove
  the sensitive-root contract; Droid remains rejected until it has an
  enforceable resolver.

Exit evidence:

- Host-level integration tests attempt direct reads, parent traversal,
  symlink traversal, writes, rename replacement, and later-launch SRT
  persistence under each supported backend.
- Web-chat and spawned-agent session metadata records the verified backend and
  enforcement result after launch preflight.
- No supported backend grants a parent of a sensitive path.

### WP6 — Loopback and daemon-side tool enforcement

Scope:

- Authorize every agent-capable daemon endpoint from verified run/project
  claims and limit the service-capability endpoint to its typed response.
- Give daemon-side gcode subprocesses the same scoped runtime context as
  terminal agents, owned by a request-scoped managed-execution lease.
- Resolve web-chat project identity server-side; never derive database scope
  directly from `ToolChatRequest.project_path` or another caller claim.
- Apply the sensitive-root path guard to built-in filesystem tools and remove
  raw arbitrary subprocess availability from managed web-chat policies.
- Add adversary tests that assume all loopback ports are reachable.

Exit evidence:

- Possession of a scoped DSN cannot access another project or any non-gcode
  hub domain even with unrestricted loopback.
- A managed web-chat tool cannot read the operator bootstrap, token, KEK, or
  sibling runtime and cannot cause the daemon to return them.
- Success, failure, cancellation, disconnect, and daemon shutdown leave no
  live tool-chat login or runtime home after reconciliation.
- Session UI/API reports `sandbox_enabled` only from an enforced launch and
  includes the effective backend identity.

### WP7 — Cutover, cleanup, and operator recovery

Scope:

- Revoke and remove every legacy generated runtime bootstrap carrying the
  shared DSN and every runtime KEK link/copy during upgrade.
- Integrate hub backup maintenance with agent-role drain, abort the globals dump
  while any ephemeral login remains, and make restore reconciliation remove
  reserved-prefix logins before agent startup.
- Add an operator command to enumerate active scoped roles and force-revoke a
  run without exposing credential material.
- Document recovery for failed rotation, daemon outage, database restore, and
  stale-role reconciliation.
- Remove compatibility with shared agent DSNs and KEK-linked runtime homes.

Exit evidence:

- Upgrade tests prove legacy material is removed before managed execution is
  re-enabled.
- Backup artifacts contain the stable issuer, daemon runtime, and capability
  roles and no ephemeral login; restore tests prove legacy agent roles cannot
  become usable.
- A repository-wide credential fixture scan and runtime snapshot contain no
  operator DSN, password, token, or KEK value.
- End-to-end tests cover issue, use, rotate, resume, normal exit, crash,
  timeout, forced revoke, expiry, daemon restart, tool-request cancellation,
  and provider-native refusal.

## Validation gates for the implementation epic

The follow-up epic is complete when all work-package evidence passes in an
isolated test hub and these system properties hold:

1. Root bootstrap, operator token, and KEK paths are unreadable and unwritable
   in every supported managed execution path.
2. SRT-installed executable content is immutable to the agent and fully
   verified before every launch.
3. A leaked run-scoped DSN cannot cross project or gcode-domain boundaries.
4. Gcode retains direct PostgreSQL behavior for its data plane.
5. Role expiry, every agent terminal path, and every daemon tool-request exit
   remove usable database authority.
6. Web chat reports sandboxing only after enforcement and applies the same
   credential and filesystem policy to daemon-side tools.
7. Logs, traces, errors, fixtures, task evidence, and session records contain
   path references and redacted identifiers only.
8. Service-capability responses never reveal the KEK or a long-lived shared
   service credential.

User acceptance of this document completes the architecture criterion for
task #19353. Implementation belongs to the follow-up epic defined above.
