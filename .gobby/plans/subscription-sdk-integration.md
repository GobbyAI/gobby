# Subscription SDK Integration (Codex + Droid Two-Mode Strangler)

**Plan ID:** subscription-sdk-integration

## Overview
`kind: framing`

Add subscription-authenticated SDK backends — `openai-codex` (AsyncCodex) and
`droid-sdk` — alongside the existing legacy paths for Codex/Droid
text_generate, vision_extract, tool_chat, and Droid web_chat, selected by new
`ai.agent_sdk_routes.{codex,droid}.{capability}` config (`legacy|sdk`, all
defaults legacy). A daemon-owned `AgentSDKRuntime` eagerly initializes
providers with ≥1 sdk route before HTTP readiness (non-fatally), exposes
sanitized diagnostics, and owns all SDK child processes. No fallback, no
shadowing: an unavailable SDK route raises the existing
`CapabilityUnavailableError`. Every legacy path is preserved, and rollback is a
config change for every capability **except droid web chat**: conversations
already pinned `sdk` keep requiring the SDK runtime after a rollback, because
the pin is immutable and there is no fallback. Section 7.3 specifies that
branch and its operator remediation.

## Constraints
`kind: framing`

- **Exclusions**: Codex web chat stays on `CodexManagedChatSession` (public
  Codex SDK lacks interactive approval callbacks); Qwen unchanged; agent
  spawning stays tmux/CLI. Endpoint-scoped providers (Responses/OSS
  `endpoint:*` bindings) always stay legacy.
- **Auth**: ChatGPT-subscription account required for Codex SDK (inspect
  `account()`; reject API-key accounts; never invoke SDK login). Droid SDK
  paths never consult `FACTORY_API_KEY`; they OAuth-seed isolated state via the
  existing `_droid_isolated_env` / `_seed_droid_factory_state` helpers.
- **No fallback / no shadow / no duplicate requests.** Exactly one **dispatch**
  resolution per request, taken from live config and snapshotted at the
  request's single entry point, so a mid-flight config flip can neither move nor
  duplicate a request (4.3 dispatch-snapshot rule). Availability probes are not
  dispatch resolutions: the vision registry probe (5.2) reports selectability
  and is TTL-cached, so it may be stale by up to one TTL window; the binding
  dispatch decision is still made exactly once, in `extract()`. The droid
  web-chat backend is pinned in `sessions.web_chat_backend` after the first
  session successfully starts — `NULL` until then — and is authoritative on
  every reconnect and immutable thereafter.
- **Live config is read through a getter, and the getter must dereference the
  object that actually gets rebound.**
  `ConfigurationRouteContext.set_runtime_config`
  (`src/gobby/servers/routes/configuration_context.py:69-81`) rebinds exactly
  one attribute — `self.server.services.config` — to a newly constructed
  `DaemonConfig`. It does not mutate the existing object in place, and it never
  touches `runner.config`. Two distinct defects follow, and both are defects:
  capturing a `DaemonConfig` value at construction, and holding a getter rooted
  at `runner.config`, which is written once during Phase 2 and never updated
  again. `ServiceContainer` is built in `runner_init/servers.py:33-60` (Phase 4)
  and seeded `config=runner.config`; from that moment the two diverge on the
  first config write. Every route-resolution site therefore reads through a
  getter that resolves the live `ServiceContainer.config` — see 2.5 for the
  single indirection all of them share.
- **Vision route resolution.** Vision's registry probe (5.2) and its adapter
  `extract()` (5.1) both consult the route, but only `extract()` is a dispatch
  decision. The probe answers "is this binding selectable right now"; a
  stale-permissive answer is caught by `extract()`, which fails the request
  rather than serving it on a rolled-back route.
- **Rollback caveat**: rollback is a config change for text generation, vision,
  and tool chat. Droid web-chat conversations already pinned `sdk` still
  require the SDK runtime after a rollback to legacy; see 7.3.
- **Honest limits**: SDK tool-chat adapters must not claim enforcement of
  `max_turns`, `tool_timeout_seconds`, or `max_bytes_per_tool_result`; no
  explicit Droid compact action (droid-sdk 0.1.2 has no public `compact()`);
  no reliance on provider-native hooks (`GOBBY_HOOKS_DISABLED=1` on all SDK
  children; Gobby in-process lifecycle authoritative).
- **Observability**: record provider, capability, model, configured route,
  actual backend, readiness, latency, usage, success/failure, cleanup outcome.
  Never prompts, responses, credentials, or auth files.
- **Import direction**: `config.app → config.ai` must never import `gobby.ai`
  (known circular-init hazard). The `AgentSdkRoute` enum and route config
  models live in `src/gobby/config/ai.py`; `src/gobby/ai/agent_sdk/` imports
  config, never the reverse.
- **Non-goals**: removing any legacy route (separate approved task after
  sustained SDK parity); `VisionExtractResult` usage field; Codex native-hook
  passthrough claims; live config-flip re-routing of pinned web-chat
  conversations.
- All-legacy config must be behaviorally inert: zero SDK processes, zero
  degraded/unavailable signals (degradation invariant: features absent by
  configuration are not degraded). This is a property of the *state*, not just
  of startup. A daemon that booted with sdk routes and was then rolled back to
  all-legacy must converge on the same inert state without a restart — see the
  reconciliation contract in 2.4/2.6. "Rollback is a config change" is false if
  it leaves live SDK children or stale degraded entries behind.
- New non-test source files stay under 1,000 lines (split modules as listed).

## P1: Foundations
`kind: framing`

**Goal**: Dependencies, config surface, and the sessions schema exist so every
later phase can build against them.

### 1.1 Add subscription SDK dependencies [category: config]
`kind: deliverable`

Target: `pyproject.toml`, `tests/ai/agent_sdk/test_sdk_surface.py`

Add to `[project].dependencies`:

```toml
"openai-codex>=0.144.4,<0.145",
"droid-sdk>=0.1.2,<0.2",
```

Run `uv lock`. Watch the resolution interaction with the existing
`openai>=1.0.0` pin (used only by embeddings/local adapters); if `openai-codex`
raises the `openai` floor, the embeddings and local-provider test suites must
pass against the resolved version.

Both SDKs are young and the pinned ranges still admit patch drift, so a
resolvable version can satisfy `import` while breaking a surface a later phase
depends on. Add an **offline** surface-compatibility test
(`tests/ai/agent_sdk/test_sdk_surface.py`, unit, no credentials, no spawned
processes) that imports the exact symbols consumed downstream and asserts their
shape via `inspect.signature` / attribute presence:

- Codex: `AsyncCodex` construction kwargs, `account()`, thread creation and
  thread options (sandbox/approvals/tools/cwd), structured-output/schema
  parameter, `LocalImageInput`, usage-bearing turn-completed event.
- Droid: custom-transport protocol methods, client `initialize()`, image input
  type, `ToolUse`/`ToolResult`, permission-request and completion event classes.

A range bump that drops or renames any of these fails at P1, before adapter
work begins.

**Acceptance:**

- 1.1.1 - Both SDKs declared with the pinned ranges and `uv lock` resolves
  cleanly. file: `pyproject.toml`.
- 1.1.2 - `import codex` (openai-codex) and `import droid_sdk` succeed in the
  project venv; embeddings/local tests pass against the resolved `openai`.
  test: `tests/ai/test_endpoints.py`.
- 1.1.3 - The offline surface test asserts every SDK symbol, method parameter,
  and event class consumed by P2-P7, runs without credentials or child
  processes, and fails on removal or rename. file:
  `tests/ai/agent_sdk/test_sdk_surface.py`.

### 1.2 Agent SDK routes config model [category: code]
`kind: deliverable`

Target: `src/gobby/config/ai.py`, `docs/audits/configuration-audit.md`,
`web/src/components/settings/sections/ProvidersModelsSection.tsx`,
`tests/config/test_import_direction.py`

Add Pydantic v2 models (all `extra="forbid"`), colocated in `src/gobby/config/ai.py` so
the config package never imports `gobby.ai`:

```python
class AgentSdkRoute(StrEnum):
    LEGACY = "legacy"
    SDK = "sdk"

class CodexAgentSdkRoutesConfig(BaseModel):   # text_generate, vision_extract, tool_chat
class DroidAgentSdkRoutesConfig(BaseModel):   # + web_chat
class AgentSdkRoutesConfig(BaseModel):        # codex, droid
    def sdk_configured_providers(self) -> tuple[str, ...]: ...
    @property
    def any_sdk(self) -> bool: ...
```

All route fields default `AgentSdkRoute.LEGACY`. Two separate per-provider
models (not inheritance) so `codex.web_chat` is rejected outright. Attach
`agent_sdk_routes: AgentSdkRoutesConfig = Field(default_factory=...)` to
`AIConfig` (currently `generation`-only, lines 199-207).

Add the audit row for `ai.agent_sdk_routes` to
`docs/audits/configuration-audit.md` and `'ai.agent_sdk_routes'` to
`OWNED_PATHS` in `ProvidersModelsSection.tsx` (ancestor path covers all seven
leaves per the sections coverage test), rendering schema-driven enum selects.

**Enforce the import direction, don't just assert it in prose.** The
`config.app → config.ai` package must never import `gobby.ai`; violating it is
a circular-init hazard that surfaces as an import-time crash far from its
cause. Add `tests/config/test_import_direction.py`, which walks the AST of
every module under `src/gobby/config/` and fails on any `import gobby.ai` or
`from gobby.ai ...` statement (including inside `TYPE_CHECKING` blocks, since
the hazard is about module ownership, not runtime cost). This is the only
acceptance-level coverage of the constraint anywhere in the plan.

**Acceptance:**

- 1.2.1 - `AgentSdkRoutesConfig` exists with all-legacy defaults and
  `extra="forbid"` rejecting unknown keys (including `codex.web_chat`).
  symbol: `AgentSdkRoutesConfig`. file: `src/gobby/config/ai.py`.
- 1.2.2 - `DaemonConfig` round-trips `ai.agent_sdk_routes` through dump/load
  and invalid route values fail validation. test:
  `tests/config/test_app_config.py`.
- 1.2.3 - Audit row present and the settings section owns the new path with
  the frontend coverage test green. file: `docs/audits/configuration-audit.md`.
- 1.2.4 - `ProvidersModelsSection` exposes the seven route selects. file:
  `web/src/components/settings/sections/ProvidersModelsSection.tsx`.
- 1.2.5 - An AST-walking test fails when any module under `src/gobby/config/`
  imports `gobby.ai`, including under `TYPE_CHECKING`. test:
  `tests/config/test_import_direction.py`.

### 1.3 Sessions web_chat_backend migration [category: code]
`kind: deliverable`

Target: `src/gobby/storage/migrations/343_session_web_chat_backend.sql`,
`src/gobby/storage/postgres_baseline_schema.sql`,
`tests/storage/test_migration_contract.py`

Migration `343_session_web_chat_backend.sql` (343 is the next contiguous
number after `342_task_validation_epoch.sql`):

```sql
ALTER TABLE sessions
    ADD COLUMN web_chat_backend TEXT;

ALTER TABLE sessions
    ADD CONSTRAINT sessions_web_chat_backend_valid
    CHECK (web_chat_backend IS NULL OR web_chat_backend IN ('legacy', 'sdk'));
```

**Nullable, three-state, deliberately.** `NULL` means *unpinned* — no backend
has served this conversation yet. It is distinct from a pinned `'legacy'`.
A `NOT NULL DEFAULT 'legacy'` column cannot express that difference, and the
write-once-after-successful-start pin in 7.3 depends on it: with a default the
row is indistinguishable from a conversation that genuinely ran on legacy, so
either a failed SDK start leaves a permanent `sdk` pin (pin before start), or
a later config flip silently migrates an in-flight legacy conversation to the
SDK backend on reconnect (pin after start, no third state). Both are wrong.
`NULL` → resolve from live config; non-NULL → authoritative, immutable.

Mirror both statements in the baseline `sessions` table (column after
`sandbox_policy_hash`, constraint alongside the named CHECKs at lines 237-267).
Existing rows become `NULL` (unpinned) and resolve from live config on their
next connect, which is `legacy` under the shipped defaults. Add a schema
contract test (pattern: `test_memory_dream_due_version_schema_contract`, lines
301-310) asserting the column and CHECK strings appear in BOTH files.

**Acceptance:**

- 1.3.1 - Migration 343 adds the nullable column plus the
  `IS NULL OR IN ('legacy','sdk')` CHECK, and existing rows are NULL after
  migration. file:
  `src/gobby/storage/migrations/343_session_web_chat_backend.sql`.
- 1.3.2 - Baseline schema carries the identical column and constraint. file:
  `src/gobby/storage/postgres_baseline_schema.sql`.
- 1.3.3 - Contract tests pass: contiguity (343 is next) and the new
  both-files schema assertion. test:
  `tests/storage/test_migration_contract.py::test_session_web_chat_backend_schema_contract`.

### 1.4 Thread web_chat_backend through session storage [category: code] (depends: 1.3)
`kind: deliverable`

Target: `src/gobby/storage/session_models.py`,
`src/gobby/storage/sessions/_crud.py`,
`src/gobby/storage/sessions/_web_chat_crud.py`,
`src/gobby/storage/sessions/_bulk_update.py`,
`src/gobby/storage/sessions/_upsert.py`

Follow the `sandbox_policy_hash` precedent trail (also a nullable TEXT column):

- `Session` dataclass: `web_chat_backend: str | None = None`; `from_row` guard
  (`row["web_chat_backend"] if "web_chat_backend" in row.keys() else None`);
  include in `to_dict`.
- `register` (Protocol + mixin): unchanged signature — new rows are written
  unpinned (`NULL`). No call site passes a backend.
- `create_web_chat_session`: unchanged — pre-created rows are also unpinned.
- `_bulk_update.update`: optional `web_chat_backend` (validated against
  `{'legacy','sdk'}`) for test/admin tooling only.
- `_upsert.update_existing_session`: **no mutation** — the pin is owned solely
  by `pin_web_chat_backend`; add a comment stating it.
- `_web_chat_crud.pin_web_chat_backend(session_id, backend) -> bool`: the
  **write-once pin** used by 7.3. Single statement,
  `UPDATE sessions SET web_chat_backend = %s WHERE id = %s AND
  web_chat_backend IS NULL`, returning whether a row was written. Rejects
  values outside `{'legacy','sdk'}`. The `IS NULL` predicate makes it
  idempotent and race-safe under concurrent first connects: the first writer
  wins, later writers no-op, and no pinned value is ever changed or cleared.

The pin is written only after a session has actually served the conversation
(7.3), so a failed first start leaves the row unpinned and the next attempt
re-resolves from live config.

The field stays `NULL` for non-web-chat sessions; no changes to agent-spawn or
terminal-session registration call sites.

**Acceptance:**

- 1.4.1 - `Session` round-trips `web_chat_backend` through register →
  from_row → to_dict, defaulting to `None` for rows written without a pin.
  symbol: `Session`. test: `tests/storage/sessions/test_models.py`.
- 1.4.2 - `register` and `create_web_chat_session` leave `web_chat_backend`
  NULL. file: `src/gobby/storage/sessions/_web_chat_crud.py`. test:
  `tests/storage/sessions/test_registration.py`.
- 1.4.3 - Upsert/reconnect never mutates a stored `web_chat_backend`.
  test: `tests/storage/sessions/test_registration.py`.
- 1.4.4 - `pin_web_chat_backend` writes exactly once from NULL, returns False
  without writing against an already-pinned row (including a concurrent
  second caller), never clears or changes a pin, and rejects invalid values.
  symbol: `pin_web_chat_backend`. test:
  `tests/storage/sessions/test_registration.py`.

## P2: Agent SDK Runtime
`kind: framing`

**Goal**: A daemon-owned runtime that owns all SDK processes, starts eagerly
and non-fatally before HTTP readiness, cleans up on all three shutdown paths,
and reports sanitized diagnostics.

### 2.1 Droid SDK transport [category: code] (depends: 1.1)
`kind: deliverable`

Target: `src/gobby/ai/agent_sdk/droid_transport.py` (new)

`GobbyDroidTransport` implementing droid-sdk's custom-transport protocol:

- `connect()`: strip `FACTORY_API_KEY` from the base env, then
  `_droid_isolated_env(base, temp_home)` (sets `GOBBY_HOOKS_DISABLED="1"`) and
  `_seed_droid_factory_state(base, temp_home)` (OAuth seeding always runs —
  the API-key short-circuit can't trigger on a stripped env). Spawn with
  `start_new_session=True`, `stderr=PIPE`.
- Continuous stderr drain via `SubprocessStderrDrain`
  (`adapters/subprocess_stderr.py:55-161`) with a redacting capture using the
  `_redact_droid_stderr` patterns (`backends/droid.py:93-98`) — droid-sdk
  0.1.2 deadlocks if stderr is not drained.
- `close()`: graceful→forced — SIGTERM the process group, wait ≤2s, SIGKILL,
  stop drain, remove temp home (pattern: `_cleanup_cli_process`,
  `_text_generation_adapters.py:239-299`).

**Acceptance:**

- 2.1.1 - Transport spawns in an isolated process group with isolated
  `$HOME`/XDG, hooks disabled, no `FACTORY_API_KEY`, OAuth state seeded.
  symbol: `GobbyDroidTransport`. file:
  `src/gobby/ai/agent_sdk/droid_transport.py`.
- 2.1.2 - stderr is drained continuously and redacted (bearer/api-key/token
  patterns). test: `tests/ai/agent_sdk/test_droid_transport.py`.
- 2.1.3 - `close()` escalates SIGTERM→SIGKILL on the group and always stops
  the drain. behavior: "graceful-then-forced cleanup" in
  `src/gobby/ai/agent_sdk/droid_transport.py`.

### 2.2 Codex SDK client [category: code] (depends: 1.1)
`kind: deliverable`

Target: `src/gobby/ai/agent_sdk/codex_client.py` (new)

`CodexSdkClient` owning one shared AsyncCodex process:

- `start()`: env = ambient minus `OPENAI_API_KEY`, plus
  `GOBBY_HOOKS_DISABLED="1"`; start AsyncCodex; then verify auth by
  inspecting `account()` — require a ChatGPT-subscription account; API-key or
  absent account raises a fixed-message auth error (no detail leak). Never
  invoke SDK login methods.
- `thread()` async context manager yielding an ephemeral thread for one-shot
  use; concurrent threads supported on the single shared process.
- `close()`: SDK aclose, then process-group SIGTERM→SIGKILL backstop.
- Coexists with `runner.codex_client` (`CodexAppServerClient`) and the
  per-endpoint clients — none of those change.

**Acceptance:**

- 2.2.1 - Child env excludes `OPENAI_API_KEY` and sets
  `GOBBY_HOOKS_DISABLED=1`. symbol: `CodexSdkClient`. file:
  `src/gobby/ai/agent_sdk/codex_client.py`.
- 2.2.2 - API-key accounts are rejected with a fixed sanitized message and no
  SDK login method is ever invoked. test:
  `tests/ai/agent_sdk/test_codex_client.py`.
- 2.2.3 - Concurrent ephemeral threads run on one shared process; `close()`
  is idempotent. test: `tests/ai/agent_sdk/test_codex_client.py`.

### 2.3 Droid SDK worker pool [category: code] (depends: 2.1)
`kind: deliverable`

Target: `src/gobby/ai/agent_sdk/droid_pool.py` (new)

`DroidSdkClientPool`: bounded pool (size =
`ai.generation.spawn_cold_max_concurrency`, default 3) of
connected-but-uninitialized clients using `GobbyDroidTransport`.

- `lease()` context manager: acquire → `initialize()` exactly one fresh
  session → yield → finally close the client (never reused), kill its process
  group, spawn a replacement.
- Never initialize multiple sessions per worker; never use
  `droid_sdk.query()` or cold-connect per request.
- Replenishment backoff: after 3 consecutive replacement failures, stop
  replenishing and flip provider status unavailable until the next
  `ensure_provider()`.
- Saturation gauges: expose `max_size`, `active` (leased), and `queued`
  (waiters) as plain ints for the diagnostics surfaces in 2.8/2.9, and time
  the acquire wait so callers can attribute delay to admission rather than to
  the provider.

**Acceptance:**

- 2.3.1 - Each lease initializes exactly one fresh session and the leased
  client is closed and replaced afterward (success, error, and cancellation
  paths). symbol: `DroidSdkClientPool`. test:
  `tests/ai/agent_sdk/test_droid_pool.py`.
- 2.3.2 - Pool size respects `spawn_cold_max_concurrency`; excess leases
  queue, and `max_size`/`active`/`queued` report the saturation state under a
  deterministic fake clock. test: `tests/ai/agent_sdk/test_droid_pool.py`.
- 2.3.3 - Consecutive replacement failures trip the unavailable backoff
  instead of respawn-looping. behavior: "replenishment backoff" in
  `src/gobby/ai/agent_sdk/droid_pool.py`.

### 2.4 Runtime facade, route resolver, diagnostics [category: code] (depends: 1.2, 2.2, 2.3)
`kind: deliverable`

Target: `src/gobby/ai/agent_sdk/runtime.py`,
`src/gobby/ai/agent_sdk/routes.py`, `src/gobby/ai/agent_sdk/diagnostics.py`,
`src/gobby/ai/agent_sdk/__init__.py` (new)

`routes.py` — pure helpers reading live config per call (imports from
`gobby.config.ai`, never the reverse):

```python
def resolve_agent_sdk_route(config, provider: str, capability: AICapability) -> AgentSdkRoute
def resolve_web_chat_backend(config, provider: str) -> str   # 'sdk' only for droid+sdk
```

plus the narrow consumer Protocols (`AgentSDKRuntimeHandle` with
`is_ready(provider)` / `unavailable_reason(provider)`, `CodexThreadRunner`,
`DroidWorkerPool`, `DroidWebChatClientFactory`) that later phases fake in
tests.

`runtime.py` — `AgentSDKRuntime(config_getter, max_concurrency,
timeout_seconds)`:

- `start()`: eager init of every provider with ≥1 sdk route; never raises;
  per-provider sanitized failure capture; bounded by `asyncio.timeout(30)`
  with concurrent probes; immediate no-op (zero processes) when
  `any_sdk` is False.
- `ensure_provider(provider)`: single-flight lazy init for routes flipped to
  sdk after startup. Config is read through `config_getter()` on every call,
  never captured, and the getter resolves live `ServiceContainer.config` (2.5).
  **Who calls it, and when.** `ensure_provider` is not ambient background work;
  without a named trigger a post-startup flip to `sdk`, or a droid pool that
  tripped its replenishment backoff (2.3), stays unavailable until the daemon
  restarts. It is single-flight per provider and rate limited by a cooldown
  (30s) so a persistently failing provider degrades to one probe per cooldown
  window instead of one probe per request; within the cooldown callers raise
  `AgentSdkUnavailableError` immediately. There are three triggers, one per
  capability shape, because the lease path alone does not cover every capability:
  - `codex_thread()` / `droid_lease()` — covers text generation and tool chat,
    which reach the runtime through a lease.
  - the vision availability probe (5.2) — vision never reaches a lease, because
    `CapabilityBinding.available()` filters the binding out first. When the
    probe finds route==sdk and the provider unready it schedules
    `ensure_provider` and returns the current (false) status; recovery lands on
    the next probe after the TTL.
  - `droid_web_chat_client()` (below) — web chat never reaches a lease either,
    because backend selection rejects on an unready runtime before any client
    is built.

  Without the second and third triggers a vision-only or web_chat-only
  configuration is permanently unrecoverable after a startup failure, which is
  the failure this contract exists to prevent.
- `reconcile()`: the symmetric partner of `ensure_provider`, and the only reason
  a live rollback is a real rollback. Recomputes each provider's configured
  route set from `config_getter()` and converges the runtime on it:
  - a provider that gained its first sdk route → `ensure_provider(provider)`;
  - a provider whose **last** sdk route became legacy → close its children on
    the same bounded graceful-then-forced path as `close()`, record
    `cleanup_outcome`, and clear its degraded entry (2.6);
  - in-flight leases finish under the route they snapshotted; drain waits for
    them within the same ~5s budget `close()` uses, then forces.

  It is single-flight, idempotent, and a no-op when the configured route set is
  unchanged, so repeated config writes cost one comparison. The trigger is the
  config-change path itself (2.6) — a rollback that no request follows must
  still converge, so there is nothing lazier that works.
- `codex_thread()` / `droid_lease()` context managers with
  `Semaphore(max_concurrency)` admission; raise `AgentSdkUnavailableError`
  when closed/unavailable (callers translate to `CapabilityUnavailableError`).
  Each lease measures `queue_wait_ms` — the monotonic time spent waiting for
  admission (semaphore, plus pool acquire for droid) before any provider work
  starts — so `latency_ms` stays attributable to the provider. Both fields are
  reported per 2.9.
- `droid_web_chat_client(conversation_id)` create/release — dedicated client
  per SDK web-chat conversation (not pool-leased). Like the lease paths it
  calls `ensure_provider("droid")` when droid is configured `sdk` but not
  ready, so a web_chat-only configuration can recover from a startup failure.
  It is constructed on
  `GobbyDroidTransport` (2.1), exactly like a pooled worker: the security
  guarantees of requirement 4 — `FACTORY_API_KEY` stripped from the base env,
  `GOBBY_HOOKS_DISABLED="1"` set, OAuth state seeded into an isolated
  `$HOME`/XDG via `_droid_isolated_env` / `_seed_droid_factory_state`, own
  process group, drained-and-redacted stderr — are properties of the transport
  and must hold for every SDK child, including long-lived web-chat ones.
  Release closes the client and removes its temp home on the same
  graceful-then-forced path as `GobbyDroidTransport.close()`.
- `provider_status()` / `status_snapshot()` (empty dict when nothing
  configured); `close()` idempotent, bounded ~5s, records per-child
  `cleanup_outcome` (`closed|killed|leaked`). Status carries the concurrency
  gauges (`max_concurrency`, `active`, `queued`, and the droid pool's
  `max_size`/`active`/`queued`) as ints — counts only, never identifiers,
  content, or paths.

`diagnostics.py` — `AgentSdkProviderStatus` dataclass + `sanitize_sdk_error()`
modeled on `_sanitized_activation_error` (`ai/endpoint_activation.py:227-233`):
auth-shaped errors collapse to fixed strings; nothing token- or path-shaped
survives.

**Acceptance:**

- 2.4.1 - `start()` with all-legacy config creates zero processes and an
  empty snapshot; with sdk routes it probes concurrently and captures
  sanitized failures without raising. symbol: `AgentSDKRuntime`. test:
  `tests/ai/agent_sdk/test_runtime.py`.
- 2.4.2 - `resolve_agent_sdk_route` defaults legacy for unset/unknown
  provider or capability, and resolution follows a `set_runtime_config`
  rebind: a flip observed through a `config_getter` changes the resolved route
  on the next call, while a captured config object would not. symbol:
  `resolve_agent_sdk_route`. test: `tests/ai/agent_sdk/test_routes.py`.
- 2.4.3 - Leases raise `AgentSdkUnavailableError` after `close()`;
  `ensure_provider` is single-flight and recovers a previously failed
  provider. test: `tests/ai/agent_sdk/test_runtime.py`.
- 2.4.6 - A lease against a configured-but-unready provider triggers exactly
  one `ensure_provider` probe, recovers the provider on success, and during the
  cooldown after a failure raises `AgentSdkUnavailableError` without
  re-probing; concurrent leases share the single flight. behavior:
  "ensure_provider triggers" in
  `src/gobby/ai/agent_sdk/runtime.py`. test:
  `tests/ai/agent_sdk/test_runtime.py`.
- 2.4.8 - Recovery is reachable for every configured capability, not only the
  leased ones: starting from an unready provider, a vision-only configuration
  recovers through the availability probe and a web_chat-only configuration
  recovers through `droid_web_chat_client`, each reaching the same single
  flight. test: `tests/ai/agent_sdk/test_runtime.py`.
- 2.4.9 - `reconcile()` converges the runtime on the configured route set:
  flipping a provider's last sdk route to legacy closes its children, records
  `cleanup_outcome`, and leaves zero SDK processes; flipping a route to sdk
  initializes the provider; an unchanged route set is a no-op; and an in-flight
  lease finishes on its snapshotted route before the drain completes. symbol:
  `reconcile`. test: `tests/ai/agent_sdk/test_runtime.py`.
- 2.4.7 - `droid_web_chat_client` builds its client on `GobbyDroidTransport`,
  so its child has no `FACTORY_API_KEY`, sets `GOBBY_HOOKS_DISABLED=1`, runs in
  an isolated `$HOME`/XDG with seeded OAuth state and its own process group,
  and release closes the client and removes the temp home. test:
  `tests/ai/agent_sdk/test_runtime.py`.
- 2.4.4 - `sanitize_sdk_error` never emits env values, tokens, or home
  paths. symbol: `sanitize_sdk_error`. test:
  `tests/ai/agent_sdk/test_diagnostics.py`.
- 2.4.5 - A lease that waits on a saturated semaphore or pool reports
  non-zero `queue_wait_ms` with `latency_ms` covering only provider work, and
  the status snapshot's concurrency gauges track active/queued leases under a
  fake clock. behavior: "admission accounting" in
  `src/gobby/ai/agent_sdk/runtime.py`. test:
  `tests/ai/agent_sdk/test_runtime.py`.

### 2.5 Construction and rollback wiring [category: code] (depends: 2.4)
`kind: deliverable`

Target: `src/gobby/runner_init/services.py`, `src/gobby/runner_init/servers.py`,
`src/gobby/runner.py`, `src/gobby/app_context.py`,
`src/gobby/runner_rollback.py`

This section **constructs and owns** the runtime and the config indirection.
It deliberately does *not* pass the runtime to any builder: the
`agent_sdk_runtime` kwargs on `build_daemon_text_generation_service` and
`build_daemon_tool_chat_service` do not exist yet — 4.3 and 6.3 create them,
and both take a dependency on this section. Passing a kwarg the callee does not
accept would make this section's own acceptance unsatisfiable at completion.

**Construct inside the existing degradation boundary.** The runtime must exist
by the time anything builds a text-generation or tool-chat service, and
`_init_llm_service` (`src/gobby/runner_init/services.py:56-73`) is where those
builders run, during Phase 2. But `init_services` has no exception handler of
its own: the only catch is the `except Exception: mark_service_degraded(runner,
"llm_service")` **inside** `_init_llm_service`. Constructing the runtime
immediately *before* that call would put it outside the boundary, so a
constructor failure would propagate out of `init_services` and abort daemon
initialization instead of degrading one service. Construct it as the **first
statement inside `_init_llm_service`'s existing `try`**. The constructor is
synchronous and spawns nothing, so it is safe this early and stays inert under
all-legacy config.

**The config getter must dereference `ServiceContainer.config`, not
`runner.config`.** `set_runtime_config` rebinds `self.server.services.config`
and nothing else (Constraints). `runner.config` is written during Phase 2 and
never updated, so a getter closed over it is stale from the first config write
— which is exactly the bug the getter exists to prevent. The container does not
exist yet at Phase 2 (`runner_init/servers.py:33-60`, Phase 4), so the binding
is completed in two steps:

- Phase 2, in `_init_llm_service`: construct with `config_getter=lambda:
  runner.config`. Correct at this point — no config write can have happened yet.
- Phase 4, in `init_servers`, immediately after the `ServiceContainer` is
  built: `runner.agent_sdk_runtime.set_config_getter(lambda: services.config or
  runner.config)`. From here the getter dereferences the exact attribute
  `set_runtime_config` rebinds. The `or runner.config` arm covers
  `ServiceContainer.config` being `DaemonConfig | None`.

The runtime owns this **single** getter and every route-resolution site takes it
from the runtime rather than closing over its own config — text (4.3), vision
(5.2), and tool chat (6.3). `WebChatRuntimeManager` (7.3) is the one exception:
it is constructed in `init_servers` where `services` is already in scope, so it
receives `config_getter=lambda: services.config or runner.config` directly, and
must still work when `agent_sdk_runtime` is `None`.

- `runner.py`: `agent_sdk_runtime: AgentSDKRuntime | None` declared alongside
  the other service fields (:148-192), set during Phase 2.
- `app_context.py`: `agent_sdk_runtime` field on `ServiceContainer`, populated
  at the same point.
- `runner_rollback.py`: add `("agent sdk runtime",
  getattr(runner, "agent_sdk_runtime", None))` to the resources tuple
  (:91-95); `_settle_async_close` already handles the coroutine.

**Acceptance:**

- 2.5.1 - The runtime is constructed as the first statement inside
  `_init_llm_service`'s existing `try`, is visible on both `GobbyRunner` and
  `ServiceContainer`, and spawns zero child processes at construction. file:
  `src/gobby/runner_init/services.py`.
- 2.5.2 - Construction-failure rollback closes the runtime. file:
  `src/gobby/runner_rollback.py`.
- 2.5.3 - A constructor failure marks `llm_service` degraded and lets
  `init_services` return normally; it never propagates out of `init_services`.
  file: `src/gobby/runner_init/services.py`. test:
  `tests/runner_init/test_services.py`.
- 2.5.4 - After `init_servers`, the runtime's config getter observes a
  `set_runtime_config` rebind: a route flip delivered that way changes the value
  the getter returns, while a getter rooted at `runner.config` would not. file:
  `src/gobby/runner_init/servers.py`. test:
  `tests/runner_init/test_servers.py`.

### 2.6 Pre-readiness eager start and degradation [category: code] (depends: 2.5)
`kind: deliverable`

Target: `src/gobby/runner_lifecycle_startup.py` (new),
`src/gobby/runner_lifecycle.py`,
`src/gobby/servers/routes/configuration_context.py`

New `async def start_agent_sdk_runtime(runner)` called in `run_daemon` after
`await require_managed_services_ready(runner)` (:205) and before
`uvicorn.Config(...)` (:208) — a deliberately separate, **non-fatal**
pre-readiness step (the managed-services gate stays hard). Semantics:

- `await runner.agent_sdk_runtime.start()`.
- Per-provider probe failure: sanitized warning +
  `mark_service_degraded(runner, "agent_sdk_codex"|"agent_sdk_droid")`;
  daemon still binds HTTP.
- `ensure_provider()` success later discards the degraded entry (mirror the
  monitor add/discard pattern).
- All-legacy: returns immediately, no degraded entries, no processes.

**The rollback trigger.** Startup is not the only transition that has to reach
the inert state. Nothing else in this plan runs on a config write, so after an
operator flips the last sdk route back to legacy the daemon keeps its Codex
process, its droid workers, and its `agent_sdk_<provider>` degraded entries
until it restarts — which makes "rollback is a config change" false. A lazy
trigger cannot fix this, because the whole point of a rollback is that no SDK
request follows it.

This section therefore adds the one trigger that closes the gap: a module-level
`schedule_agent_sdk_reconcile(services)` in `runner_lifecycle_startup.py`,
called from `ConfigurationRouteContext.set_runtime_config` after the rebind.
It is fire-and-forget (`set_runtime_config` is synchronous and must stay that
way), single-flight via the runtime's own `reconcile()` (2.4), a no-op when the
route set is unchanged, and a no-op when `agent_sdk_runtime` is `None`. That is
one call at the exact point the configuration changes — not a polling loop, not
a watcher, and not a new service.

- Reconciliation that drops a provider also discards its
  `agent_sdk_<provider>` degraded entry, so an all-legacy daemon reports zero
  degraded SDK signals regardless of what it booted with.

**Acceptance:**

- 2.6.1 - HTTP readiness is announced only after SDK init settles, and a
  failed probe never prevents daemon startup. behavior: "non-fatal
  pre-readiness init" in `src/gobby/runner_lifecycle_startup.py`.
- 2.6.2 - Probe failure of a configured provider adds
  `agent_sdk_<provider>` to `runner.degraded_services`; all-legacy adds
  nothing. test: `tests/runner/test_lifecycle_startup.py`.
- 2.6.3 - A daemon started with sdk routes and then rolled back to all-legacy
  through `set_runtime_config` converges without a restart and with no
  subsequent request: zero SDK child processes and no `agent_sdk_<provider>`
  entry in `runner.degraded_services`. behavior: "rollback reconciliation" in
  `src/gobby/runner_lifecycle_startup.py`. test:
  `tests/runner/test_lifecycle_startup.py`.
- 2.6.4 - `set_runtime_config` stays synchronous and never raises or blocks on
  reconciliation, including when `agent_sdk_runtime` is `None`. file:
  `src/gobby/servers/routes/configuration_context.py`. test:
  `tests/servers/routes/test_configuration_routes.py`.

### 2.7 Shutdown owners [category: code] (depends: 2.5)
`kind: deliverable`

Target: `src/gobby/servers/_app_lifecycle.py`,
`src/gobby/runner_lifecycle_shutdown.py`

- `_app_lifecycle.py`: after the web-chat runtime-manager stop (:362-367),
  `await services.agent_sdk_runtime.close()` in try/except with warning.
- `runner_lifecycle_shutdown.py::_stop_started_services` (:477-530): add a
  bounded (≤5s) best-effort `agent_sdk_runtime.close()`.
  `_reap_remaining_child_processes` remains the backstop.
- `close()` is idempotent across all three owners (rollback, lifespan,
  graceful shutdown) — double-close is a no-op.

**Acceptance:**

- 2.7.1 - Runtime closes on FastAPI lifespan shutdown and on graceful daemon
  shutdown, and double-close is safe. file:
  `src/gobby/servers/_app_lifecycle.py`.
- 2.7.2 - All SDK child process groups are gone within the shutdown budget.
  behavior: "cleanup outcome recording" in
  `src/gobby/ai/agent_sdk/runtime.py`.

### 2.8 Admin status diagnostics block [category: code] (depends: 2.4)
`kind: deliverable`

Target: `src/gobby/servers/routes/admin/_health.py`

Add `"agent_sdk_runtime": services.agent_sdk_runtime.status_snapshot()` to the
GET `/api/admin/status` payload (next to `provider_models`, :554). Per-provider
shape reuses the `ProviderBackendHealth` keys extended with `configured` and
`routes`:

```json
{"codex": {"provider": "codex", "configured": true, "available": false,
  "startup_error": "<sanitized>", "routes": {"text_generate": "sdk", ...},
  "concurrency": {"max": 8, "active": 2, "queued": 0},
  "pool": {"max_size": 3, "active": 1, "queued": 0}}}
```

`concurrency` is the shared admission semaphore; `pool` is droid-only (absent
for codex). Counts only — no identifiers, content, or paths. Together with
`queue_wait_ms` (2.9) they let an operator distinguish "saturated, work is
queueing" from "provider is slow" without adding a new surface.

Empty object when every route is legacy — absent-by-configuration produces
zero signals (invariant from #16609/#18371).

**Acceptance:**

- 2.8.1 - `/api/admin/status` exposes the block with sanitized
  `startup_error` for a failed configured provider and `{}` when all-legacy.
  file: `src/gobby/servers/routes/admin/_health.py`.
- 2.8.2 - The block reports `concurrency` gauges for every configured
  provider and `pool` gauges for droid, as integer counts only. test:
  `tests/servers/routes/admin/test_health.py`.

### 2.9 Observability events [category: code] (depends: 2.4)
`kind: deliverable`

Target: `src/gobby/ai/_text_generation_service.py`,
`src/gobby/ai/agent_sdk/runtime.py`

- Extend the `feature_llm_call` structured log (:764-803) `extra` with
  `configured_route`, `backend` (e.g. `codex_sdk`/`codex_cli`), and
  `usage_input_tokens`/`usage_output_tokens` lifted from
  `LLMTextResult.usage` when present (covers text_generate on both routes).
- `AgentSDKRuntime` emits one `agent_sdk_call` structured log per lease:
  `provider, capability, model, configured_route, backend, ready,
  queue_wait_ms, latency_ms, usage (ints), success, error (sanitized),
  cleanup_outcome` (covers tool_chat/vision/web_chat). `queue_wait_ms` is
  admission wait; `latency_ms` excludes it. Optionally mirror to the existing
  OTel helpers (`inc_counter`/`observe_histogram`) at the same site.
- Never log prompts, responses, credentials, or auth-file paths.

**Sanitize at the raise site, not at the log site — and suppress the cause.**
`feature_llm_call` logs `str(error)` verbatim
(`_text_generation_service.py:799`), and the tool-chat service folds
`str(exc)` into its error surfaces the same way. Neither is SDK-aware, so a raw
`AsyncCodex` or `droid-sdk` exception — which can carry a home path or auth
detail in its message — would reach structured logs untouched, defeating
`sanitize_sdk_error` (2.4). Every SDK boundary (4.1, 4.2, 5.1, 6.1, 6.2, and
`DroidSDKChatSession` in 7.2) therefore wraps **every** provider exception in a
sanitized domain error before it escapes.

Wrapping alone is not sufficient. `raise Sanitized(...) from exc` keeps the raw
exception reachable as `__cause__`, and the HTTP and web-chat error paths call
`logger.exception`, which formats the entire chain — so the raw SDK message
lands in the log anyway, one frame below the sanitized one. The raise must
therefore be `raise Sanitized(...) from None`, which suppresses both
`__cause__` and `__context__`. Where the original is needed for diagnosis, log
`sanitize_sdk_error(exc)` at the raise site; never the exception object.
Nothing downstream is modified to compensate, and no new sanitization layer is
added at the logging sites.

**Residual service-level admission.** SDK-routed text requests keep their
existing CLI/DAEMON binding styles, so they still pass the text-generation
service's spawn-cold admission semaphore (`_text_generation_service.py:55-68`)
*before* the adapter runs. That wait happens outside the runtime and is
invisible to `queue_wait_ms`. This is accepted, not fixed: exempting SDK
requests would mean touching binding-style dispatch, which this plan
deliberately leaves diff-free. `queue_wait_ms` is documented as *runtime*
admission only, and 8.2 states that a saturated daemon can show low
`queue_wait_ms` while requests still queue upstream.

**Acceptance:**

- 2.9.1 - `feature_llm_call` carries route/backend/usage fields on the legacy
  text route. file: `src/gobby/ai/_text_generation_service.py`. test:
  `tests/ai/test_text_generation.py`.
- 2.9.3 - `sanitize_sdk_error` collapses a provider exception carrying a home
  path, an env value, and a token-shaped string into a fixed sanitized message,
  and the sanitized error raised at a boundary has both `__cause__` and
  `__context__` cleared, so `logger.exception` formats no raw provider text.
  symbol: `sanitize_sdk_error`. test:
  `tests/ai/agent_sdk/test_diagnostics.py`.

  The end-to-end half of this assertion — an SDK adapter exception travelling
  all the way into `feature_llm_call` — is carried by 4.3.6 instead, because no
  SDK text adapter and no SDK-to-service path exists at 2.9's completion (2.9
  depends only on 2.4). Same deferral shape as 3.1.4 → 6.3.5.
- 2.9.2 - `agent_sdk_call` events carry the full field set including
  `queue_wait_ms` and `latency_ms` as disjoint measurements, with sanitized
  errors and no content. test: `tests/ai/agent_sdk/test_runtime.py`.

## P3: Shared Capability Surfaces
`kind: framing`

**Goal**: The cross-cutting contract changes every capability route builds on.

### 3.1 Unsupported-limits reporting contract [category: code]
`kind: deliverable`

Target: `src/gobby/ai/_tool_chat_contracts.py`,
`src/gobby/ai/_tool_chat_service.py`, `src/gobby/ai/_tool_chat_codex.py`,
`src/gobby/ai/_tool_chat_droid.py`, `src/gobby/ai/_tool_chat_adapters.py`,
`src/gobby/ai/_tool_chat_spawn.py`, `src/gobby/servers/routes/llm.py`

New uniform surface (no such concept exists today):

```python
TOOL_LOOP_LIMIT_FIELDS: tuple[str, ...] = (
    "max_turns", "max_tool_calls", "max_bytes_per_tool_result",
    "tool_timeout_seconds", "loop_timeout_seconds",
)
def unsupported_limit_fields(enforced: Iterable[str]) -> tuple[str, ...]: ...

@dataclass(frozen=True, kw_only=True)
class ToolChatResult:
    ...
    unsupported_limits: tuple[str, ...] = ()
```

Every adapter (legacy included — Codex, Droid, the runtime-style adapters,
and the Grok/Qwen spawn adapters in `src/gobby/ai/_tool_chat_spawn.py`)
declares a module-level `_ENFORCED_LIMITS` frozenset and populates the field. `ToolChatService.chat_result` logs one
structured `tool_chat limits partially enforced` info line when non-empty.
Also fix the stale `ToolLoopLimits` "(Family A)" docstring while touching the
file. Field names stay aligned with the generated
`crates/gcore/contracts/tool_loop_limits.v1.json` contract.

A log line is an operator signal, not an API. The caller that requested the
limits must be able to read which of them the selected adapter did not honor,
so extend the `investigation` provenance block already returned by the
tool-chat route (`src/gobby/servers/routes/llm.py:312-318`, currently
`tool_use_count`/`turns`/`tools`/`adapter_style`/`stop_reason`) with
`"unsupported_limits": list(result.unsupported_limits)` — always present,
empty list when everything requested was enforced. Document the field in 8.2.
The structured log stays as the operator-facing companion.

**Acceptance:**

- 3.1.1 - `ToolChatResult.unsupported_limits` exists with a frozen default
  keeping all existing constructions valid. symbol:
  `ToolChatResult`. file: `src/gobby/ai/_tool_chat_contracts.py`.
- 3.1.2 - `unsupported_limit_fields` validates against
  `TOOL_LOOP_LIMIT_FIELDS` and returns sorted missing fields. test:
  `tests/ai/test_tool_chat_service.py`.
- 3.1.3 - Legacy Codex/Droid/runtime adapters populate the field and the
  service logs when it is non-empty. test:
  `tests/ai/test_tool_chat_protocols.py`.
- 3.1.4 - The tool-chat route's `investigation` block always carries
  `unsupported_limits`, asserted against the exact sorted values for a legacy
  adapter and as an empty list when every requested limit was enforced. file:
  `src/gobby/servers/routes/llm.py`. test:
  `tests/servers/routes/test_llm_routes.py`.

The SDK-adapter half of that assertion cannot live here: SDK tool-chat
adapters are created in 6.1/6.2, which depend on this section, so an
acceptance item requiring one would be unsatisfiable at 3.1's completion. It
is carried by 6.3 instead.

## P4: Text Generation SDK Routes
`kind: framing`

**Goal**: `agent_sdk_routes.*.text_generate=sdk` serves feature_low/mid/high
text generation through the SDKs with usage and structured output.

### 4.1 Codex SDK text adapter [category: code] (depends: 2.4)
`kind: deliverable`

Target: `src/gobby/ai/agent_sdk/codex_oneshot.py` (new)

`CodexSDKTextGenerateAdapter(runtime, timeout_seconds)` implementing
`TextGenerateAdapter` + `generate_json`:

- Ephemeral thread per request via `runtime.codex_thread()`; neutral cwd
  (`neutral_textgen_cwd()`), read-only sandbox, `deny_all` approvals, no
  tools; reuse the established one-shot contract (`ONE_SHOT_DIRECTIVE` lane
  from task #17061).
- `generate` returns usage-bearing `LLMTextResult`; `generate_json` uses the
  SDK structured-output schema.
- Not-ready runtime → `CapabilityUnavailableError(TEXT_GENERATE,
  provider="codex", reason=runtime.unavailable_reason(...))`.

**Acceptance:**

- 4.1.1 - Thread options pin read-only sandbox, deny_all, no tools, neutral
  cwd, ephemeral. symbol: `CodexSDKTextGenerateAdapter`. test:
  `tests/ai/agent_sdk/test_codex_oneshot.py`.
- 4.1.2 - `generate_json` round-trips a schema-validated object and
  `generate` returns `LLMTextResult` with usage. test:
  `tests/ai/agent_sdk/test_codex_oneshot.py`.
- 4.1.3 - Unready runtime raises `CapabilityUnavailableError` (no fallback).
  test: `tests/ai/agent_sdk/test_codex_oneshot.py`.

### 4.2 Droid SDK text adapter [category: code] (depends: 2.4)
`kind: deliverable`

Target: `src/gobby/ai/agent_sdk/droid_oneshot.py` (new)

`DroidSDKTextGenerateAdapter(runtime, timeout_seconds)`:

- One `runtime.droid_lease()` per request → fresh isolated session, tools
  disabled, permission requests cancelled; collect final text + usage.
- Lease released on success, error, and cancellation.
- Unready runtime → `CapabilityUnavailableError`.

**Acceptance:**

- 4.2.1 - Exactly one lease per request; released on success, error, and
  cancellation. symbol: `DroidSDKTextGenerateAdapter`. test:
  `tests/ai/agent_sdk/test_droid_oneshot.py`.
- 4.2.2 - Tools disabled and permission requests cancelled within the
  session. test: `tests/ai/agent_sdk/test_droid_oneshot.py`.

### 4.3 Text routing wrapper and builder wiring [category: code] (depends: 2.5, 4.1, 4.2)
`kind: deliverable`

Target: `src/gobby/ai/agent_sdk/route_adapters.py` (new),
`src/gobby/ai/_text_generation_builder.py`,
`src/gobby/ai/_text_generation_service.py`,
`tests/ai/agent_sdk/test_route_dispatch_contract.py` (new)

This section **creates** the `agent_sdk_runtime` kwarg on
`build_daemon_text_generation_service` /
`_daemon_text_generation_adapter_factories` and wires 2.5's runtime into it,
hence the dependency on 2.5. It also **creates**
`tests/ai/agent_sdk/test_route_dispatch_contract.py`, the shared
cross-capability contract test that 5.1 and 6.3 extend — both depend on this
section for that reason.

- `RoutingTextGenerateAdapter(provider, config_getter, legacy_factory,
  sdk_factory)`: resolves the route at the top of each call through
  `config_getter()` — the runtime's single getter (2.5), never a captured
  `DaemonConfig` and never one rooted at `runner.config` — and lazily builds and
  caches both inner adapters.

**Dispatch-snapshot rule** (binding on every routed capability — 4.3, 5.1,
6.3): resolve `AgentSdkRoute` once into a local variable at the request's
**single entry point** and use that value for the whole request, including any
error mapping. A config flip mid-flight must not move a request between
backends, split it across both, or produce a second attempt. "Once per request"
is the resolution count, not just the selection point: unavailability, timeout,
or failure on the selected backend raises `CapabilityUnavailableError` with
zero construction or invocation of the other backend. The next request observes
the flip.

**The JSON path has to be inside the snapshot, so `supports_native_json` is
not viable.** The service does not make one call into the adapter. It decides
the structured-vs-parsed-text path with a synchronous probe, then performs an
admission `await` (`_await_admitted_candidate`), and only then dispatches
(`_text_generation_service.py:550-663`). A flip landing in that window breaks
the rule in both directions: a probe that answered "native JSON" can be
followed by dispatch into a legacy inner adapter that has no `generate_json`,
and a probe that answered "parsed text" can be followed by dispatch onto the
SDK backend. An adapter-local snapshot cannot span two entry points the
adapter does not control.

So the routing adapter owns the whole JSON decision instead of advertising a
capability the service must act on. `RoutingTextGenerateAdapter.generate_json`
is a single entry point: it snapshots the route once, and when the snapshot
resolves `legacy` it performs the existing parsed-text JSON path itself. To be
byte-identical to today that branch must reuse the service's own helpers, not
reimplement them: `_json_request(candidate)` for the request transformation,
`_parse_json_text(raw)` for the parse, and `_json_parse_failure(raw, exc)` for
the error mapping (`_text_generation_service.py:595-612`). Promote the three
from module-private to a shared internal surface both modules import; a
reimplementation would drift on the failure shape first.

**But the service's outcome label cannot stay diff-free.** Line 593 sets
`parse_outcome = "provider_structured"` unconditionally whenever
`generate_json` is callable, and the routing wrapper always exposes
`generate_json`. Left alone, every *legacy* JSON request would be logged as
`provider_structured` when it was in fact `parsed_text` — the dispatch is
correct and the metric lies. An earlier draft of this plan claimed service
dispatch was diff-free here; that claim is withdrawn, because it was bought
with inaccurate observability.

The minimal correction keeps every existing `generate_json` implementation
untouched: `generate_json` **may** return a `StructuredJsonResult(data,
parse_outcome)` instead of a bare `dict`, and the service unpacks it when
present:

```python
outcome = getattr(returned, "parse_outcome", "provider_structured")
result = getattr(returned, "data", returned)
```

Only `RoutingTextGenerateAdapter` returns the richer shape; claude, local, and
every endpoint adapter keep returning a plain `dict` and keep labelling
`provider_structured`. That is a handful of lines at one site, and it is the
least mechanism that keeps both the dispatch and the label honest.

- Cross-capability contract test: table-driven over `text_generate`,
  `vision_extract`, and `tool_chat` × {legacy→sdk flip, sdk→legacy flip} in
  `tests/ai/agent_sdk/test_route_dispatch_contract.py`, asserting (a) the
  in-flight call finishes on its snapshotted backend, (b) the next call
  observes the flip, (c) SDK unavailable/timeout yields exactly one SDK
  attempt and no legacy adapter is constructed or called.
- `_daemon_text_generation_adapter_factories` gains
  `agent_sdk_runtime` kwarg; when present, wraps the `"codex"`/`"droid"`
  entries **after** the endpoint loop (endpoint providers never wrapped). It
  passes the runtime's config getter, not the config object it was built with.
- `build_daemon_text_generation_service` gains the matching optional
  `agent_sdk_runtime` kwarg and threads it through; `_init_llm_service` (2.5)
  starts passing `runner.agent_sdk_runtime` at this point.
- `_text_generation_service.py`: the `StructuredJsonResult` unpacking above at
  `:588-593`. No other change to JSON-path dispatch.

**Acceptance:**

- 4.3.1 - route=legacy dispatches to the existing CLI adapters and
  `generate_json` returns the parsed-text result byte-identically to today,
  reusing `_json_request` / `_parse_json_text` / `_json_parse_failure` so a
  malformed payload raises the same error as today; route=sdk dispatches to SDK
  adapters using the SDK's structured output. symbol:
  `RoutingTextGenerateAdapter`. test: `tests/ai/test_text_generation.py`.
- 4.3.5 - A legacy-routed JSON request is logged `json_parse_outcome:
  parsed_text`, not `provider_structured`, and an sdk-routed one is logged
  `provider_structured`; adapters that return a bare `dict` are unaffected and
  still label `provider_structured`. file:
  `src/gobby/ai/_text_generation_service.py`. test:
  `tests/ai/test_text_generation.py`.
- 4.3.6 - A provider exception raised inside an SDK text adapter reaches
  `feature_llm_call` already sanitized: the logged `error` contains no home
  path, env value, or token-shaped text even though the log records
  `str(error)` verbatim, and `logger.exception` emits no raw provider text
  because the cause is suppressed (2.9). test:
  `tests/ai/test_text_generation.py`.
- 4.3.2 - Route is re-resolved per request (config flip between calls
  changes the inner adapter). test:
  `tests/ai/agent_sdk/test_route_adapters.py`.
- 4.3.3 - Endpoint-scoped providers are never wrapped. test:
  `tests/ai/test_endpoints.py`.
- 4.3.4 - The dispatch-snapshot rule holds for all three routed capabilities:
  a flip after dispatch never changes the in-flight backend, the next call
  observes it, and SDK failure produces one SDK attempt with no legacy
  construction or invocation. behavior: "dispatch-snapshot rule" in
  `src/gobby/ai/agent_sdk/route_adapters.py`. test:
  `tests/ai/agent_sdk/test_route_dispatch_contract.py`.

## P5: Vision SDK Routes
`kind: framing`

**Goal**: First-ever codex/droid vision, plus a persistent shared
`VisionExtractService`.

### 5.1 SDK vision adapters [category: code] (depends: 2.4, 4.1, 4.2, 4.3)
`kind: deliverable`

Target: `src/gobby/ai/agent_sdk/codex_oneshot.py`,
`src/gobby/ai/agent_sdk/droid_oneshot.py`

Both modules are **created** by 4.1 and 4.2; this section only adds the vision
adapter classes to them, hence those dependency edges. The edge on 4.3 is for
the same reason: 4.3 defines the dispatch-snapshot rule this section cites and
creates `tests/ai/agent_sdk/test_route_dispatch_contract.py`, the shared test
file 5.1.2 extends with the vision rows. Without that edge a valid topological
order completes 5.1 while neither the rule nor the file exists.

`CodexSDKVisionExtractAdapter` (Codex `LocalImageInput(request.image_path)`)
and `DroidSDKVisionExtractAdapter` (droid typed image input), both implementing
`VisionExtractAdapter.extract`. Output flows through the existing service
validation. Documented non-goal: no usage field on `VisionExtractResult`.

**`extract()` is vision's single dispatch resolution.** Vision is the one
capability where the route is consulted at two moments — the registry
`availability_probe` (5.2, TTL-cached) and this adapter — and it would be easy
to read that as two dispatch decisions for one request, violating the
once-per-request rule. It is not, and the plan says so explicitly rather than
adding machinery to avoid it: the probe answers a *selectability* question and
its answer may be up to one TTL stale; `extract()` makes the one dispatch
decision. Concretely, `extract()` resolves the route and runtime readiness once
into locals before its first `await` and uses those values for the rest of the
call, per the dispatch-snapshot rule from 4.3. A stale-but-permissive probe
followed by a flip to legacy therefore fails the request with
`CapabilityUnavailableError`; it never silently degrades to a legacy vision
path (there isn't one). Dropping the re-check to get a literal single
resolution would trade that protection for nothing.

**Acceptance:**

- 5.1.1 - Codex adapter passes `LocalImageInput`; droid adapter passes typed
  image input; both raise `CapabilityUnavailableError` when route=legacy or
  runtime unready. symbol: `CodexSDKVisionExtractAdapter`. test:
  `tests/ai/agent_sdk/test_vision_adapters.py`.
- 5.1.2 - `extract()` snapshots the route before its first await; a flip
  after entry does not change the backend serving that call, and exactly one
  dispatch resolution occurs per request regardless of whether the probe was a
  cache hit or a cache miss. behavior:
  "dispatch-snapshot rule" in `src/gobby/ai/agent_sdk/droid_oneshot.py`.
  test: `tests/ai/agent_sdk/test_route_dispatch_contract.py`.

### 5.2 Vision registry availability gate [category: code] (depends: 2.4)
`kind: deliverable`

Target: `src/gobby/ai/registry_builder.py`, `src/gobby/ai/registry.py`,
`src/gobby/ai/vision.py`

- `_vision_extract_binding` gains optional `agent_sdk_vision_probe:
  Callable[[str], bool] | None`; for codex/droid with a probe, return an
  available-when-probed binding using `availability_probe` +
  `availability_probe_ttl_seconds` (probe = route==sdk AND
  `runtime.is_ready(provider)`); claude/grok/qwen branches unchanged; no new
  adapter style. The probe closure reads the route through the runtime's
  config getter (2.5), never a captured `DaemonConfig` and never one rooted at
  `runner.config` (see Constraints). When it finds route==sdk and the provider
  unready it also schedules `ensure_provider` (2.4) and returns the current
  status, which is what makes a vision-only configuration recoverable — vision
  never reaches a lease, so without this it could never re-probe.
- **Pin the TTL to the registry default of 2.0 seconds explicitly.**
  `CapabilityBinding.available()` (`src/gobby/ai/registry.py:217-245`) caches
  the probe result for the TTL in **both** polarities — a negative result is
  cached exactly as long as a positive one. So a `legacy → sdk` flip stays
  invisible to vision selection for up to one TTL window, and an `sdk → legacy`
  flip leaves a stale-permissive binding for the same window. The stale-positive
  case is already caught by 5.1's `extract()` re-check, which fails the call
  rather than serving it. The stale-negative case is a bounded delay before the
  capability becomes selectable, which is acceptable only because the window is
  small — so the value must be stated rather than inherited, and it must not be
  raised without revisiting this reasoning.
- Thread the kwarg through `build_daemon_ai_capability_registry` (default
  None ⇒ today's bindings exactly).
- `_daemon_vision_extract_adapters` gains `agent_sdk_runtime` kwarg and
  registers the SDK adapters under `"codex"`/`"droid"` when present.

**Acceptance:**

- 5.2.1 - Without a probe, vision bindings are byte-identical to today
  (codex/droid unavailable). file: `src/gobby/ai/registry_builder.py`. test:
  `tests/ai/test_capability_registry.py`.
- 5.2.2 - With probe true, codex/droid `vision_extract` bindings become
  selectable and `VisionExtractService.extract` succeeds end-to-end with a
  fake adapter. test: `tests/ai/test_vision_extraction.py`.
- 5.2.3 - The binding is constructed with an explicit 2.0s
  `availability_probe_ttl_seconds`, and under a fake clock both a
  stale-negative and a stale-positive probe result persist for at most that
  window. file: `src/gobby/ai/registry_builder.py`. test:
  `tests/ai/test_capability_registry.py`.
- 5.2.4 - The probe observes a `set_runtime_config` rebind: once the TTL
  expires, a legacy→sdk flip delivered that way makes the codex/droid binding
  selectable, and a probe closure holding a captured config — or one rooted at
  `runner.config` — would not. test: `tests/ai/test_capability_registry.py`.

### 5.3 Persistent shared VisionExtractService [category: code] (depends: 2.5, 5.1, 5.2)
`kind: deliverable`

Target: `src/gobby/app_context.py`, `src/gobby/runner.py`,
`src/gobby/runner_init/services.py`, `src/gobby/runner_init/servers.py`,
`src/gobby/servers/routes/llm.py`

The `depends: 2.5` edge is load-bearing, not decorative: this section consumes
`services.agent_sdk_runtime` and edits two files (`app_context.py`,
`runner_init/services.py`) that 2.5 also edits.

- `ServiceContainer.vision_extract_service: VisionExtractService | None`.
- `runner.py`: declare `vision_extract_service: VisionExtractService | None`
  alongside the other service fields (:148-192). Without this the wiring below
  has no attribute to read, and no other section declares it.
- Build once in `_init_llm_service` with `agent_sdk_runtime` (available there
  per 2.5); assign to both `runner` and the container.
- `runner_init/servers.py`: replace the per-request/fresh construction at
  :165-169 with `set_vision_extract_service(runner.vision_extract_service)`
  and hoist into the unconditional communications block (communications get
  vision even with WebSocket disabled).
- `routes/llm.py::extract_vision` (~:365): container-first
  (`services.vision_extract_service or build_...`); error mapping unchanged
  (`CapabilityUnavailableError` → 400 `capability_unavailable`).

**Acceptance:**

- 5.3.1 - One `VisionExtractService` instance is shared by the HTTP route and
  communications (sticker vision), including when WebSocket is disabled.
  file: `src/gobby/runner_init/servers.py`. test:
  `tests/servers/routes/test_llm_routes.py`.
- 5.3.2 - `ServiceContainer` exposes the service and existing route tests
  pass unmodified. symbol: `ServiceContainer`. file:
  `src/gobby/app_context.py`.
- 5.3.3 - `GobbyRunner` declares `vision_extract_service`, and the
  `runner_init/servers.py` wiring reads that declared field rather than an
  undeclared attribute. file: `src/gobby/runner.py`.

## P6: Tool Chat SDK Routes
`kind: framing`

**Goal**: SDK-transport tool chat preserving Family B semantics via the
existing tool substrate, with honest limits.

### 6.1 Codex SDK tool-chat adapter [category: code] (depends: 2.4, 3.1)
`kind: deliverable`

Target: `src/gobby/ai/agent_sdk/tool_chat_codex.py` (new)

`CodexSDKToolChatAdapter(runtime, config)`:

- Reuses `validate_policy` + `ToolRuntime` (`_tool_chat_tools.py`) and
  `ToolLoopController` — same Family B substrate as legacy: read-only gcode
  scoped to `request.project_path`, call budget, provenance trace. The
  daemon/CLI tool-allowlist lockstep contract (`GCODE_READONLY_TOOLS` /
  `GWIKI_READONLY_TOOLS`) is untouched.
- `runtime.codex_thread()` with dynamic tool specs + tool handler; consume
  typed SDK events directly (no JSONL parser reuse): deltas → narrative,
  tool calls → `runtime.invoke` (recorded/counted), turn-completed → turns +
  usage accumulation. Budget exhaustion interrupts the thread (the N+1 tool
  call never executes — same contract as legacy).
- Returns full `ToolChatResult` including `usage` (fixes the legacy Codex
  usage drop) and `unsupported_limits` from `_ENFORCED_LIMITS =
  {"max_tool_calls", "loop_timeout_seconds"}`.

**Acceptance:**

- 6.1.1 - Policy validated; tools served by `ToolRuntime` scoped to the
  requested project; trace/calls_used/budget_exhausted populated. symbol:
  `CodexSDKToolChatAdapter`. test:
  `tests/ai/agent_sdk/test_tool_chat_codex.py`.
- 6.1.2 - Budget exhaustion interrupts before executing tool call N+1.
  test: `tests/ai/agent_sdk/test_tool_chat_codex.py`.
- 6.1.3 - Result carries usage and `unsupported_limits` excluding exactly
  the enforced set. test: `tests/ai/agent_sdk/test_tool_chat_codex.py`.

### 6.2 Droid SDK tool-chat adapter [category: code] (depends: 2.4, 3.1)
`kind: deliverable`

Target: `src/gobby/ai/agent_sdk/tool_chat_droid.py` (new)

`DroidSDKToolChatAdapter(runtime)`: pool lease → typed `ToolUse` events →
`runtime.invoke` → `ToolResult` replies; assistant messages accumulate text
and `controller.record_turn()`; permission requests cancelled; completion
event supplies usage/stop_reason. Same substrate, budget, and
`unsupported_limits` contract as 6.1; lease hygiene per 4.2.

**Acceptance:**

- 6.2.1 - ToolUse/ToolResult round-trip through `ToolRuntime` with
  provenance recorded; permission requests cancelled. symbol:
  `DroidSDKToolChatAdapter`. test:
  `tests/ai/agent_sdk/test_tool_chat_droid.py`.
- 6.2.2 - One lease per request with close+replenish on all exit paths and
  budget exhaustion interrupting the session. test:
  `tests/ai/agent_sdk/test_tool_chat_droid.py`.
- 6.2.3 - Result carries usage and `unsupported_limits` excluding exactly the
  enforced set, so the honest-limits contract is pinned for the droid adapter
  and not only for codex (6.1.3). test:
  `tests/ai/agent_sdk/test_tool_chat_droid.py`.

### 6.3 Tool-chat routing shim [category: code] (depends: 2.5, 4.3, 6.1, 6.2)
`kind: deliverable`

Target: `src/gobby/ai/agent_sdk/route_adapters.py`,
`src/gobby/ai/_tool_chat_builder.py`, `src/gobby/ai/_tool_chat_spawn.py`,
`src/gobby/servers/routes/llm.py`, `src/gobby/servers/http.py`

`route_adapters.py` is **created** by 4.3; this section adds a second class to
it, hence that dependency edge. This section also **creates** the
`agent_sdk_runtime` kwarg on `build_daemon_tool_chat_service` and wires 2.5's
runtime into both of its call sites, hence the edge on 2.5.

`RoutingToolChatAdapter(config_getter, sdk_factories, legacy_factory)` wired for the
DAEMON and CLI style factories in `build_daemon_tool_chat_service` (new
`agent_sdk_runtime` kwarg). SDK iff `binding.provider in sdk_factories` AND
`not binding.metadata.get("endpoint")` AND route==sdk; everything else legacy.

**Both construction sites, not just one.** `build_daemon_tool_chat_service` is
called twice in the repo: `_init_llm_service`
(`src/gobby/runner_init/services.py:69`) and the fallback at
`src/gobby/servers/http.py:100-104`, which runs when
`services.tool_chat_service is None`. Pass `services.agent_sdk_runtime` at both.
A daemon that took the fallback path would otherwise serve tool chat from a
runtime-less service, resolving every `sdk` route to legacy with no diagnostic —
silently, because that is exactly what an unconfigured route looks like.
No new `AIAdapterStyle`; `_TOOL_CHAT_EXECUTABLE_STYLES`,
`_RUNTIME_ADAPTER_STYLES`, style maps, and `ToolChatService` dispatch all
unchanged (`ToolChatResult.adapter_style` still reports the binding style).
Also fix the stale `_tool_chat_spawn.py` module docstring and `__all__`
omissions while touching the area.

The dispatch-snapshot rule from 4.3 applies: the route is snapshotted into a
local at the single entry point of `chat_result`, so a mid-request flip cannot
switch adapters, restart the tool loop, or double-charge the call budget. Route
resolution reads live config through the runtime's getter (2.5) — never a
captured object, and never one rooted at `runner.config`.

This section also carries the SDK half of the `unsupported_limits` route
assertion deferred from 3.1.4, because SDK tool-chat adapters first exist at
6.1/6.2.

**Acceptance:**

- 6.3.1 - Endpoint-metadata DAEMON bindings always route legacy; codex/droid
  route=sdk dispatches to SDK adapters; grok/qwen (ACP style) untouched.
  symbol: `RoutingToolChatAdapter`. test:
  `tests/ai/test_tool_chat_service.py`.
- 6.3.4 - A flip mid tool-loop leaves the in-flight request on its
  snapshotted adapter with one tool budget and no second attempt. behavior:
  "dispatch-snapshot rule" in `src/gobby/ai/agent_sdk/route_adapters.py`.
  test: `tests/ai/agent_sdk/test_route_dispatch_contract.py`.
- 6.3.2 - Style allowlists and service dispatch are diff-free apart from the
  3.1 log line. file: `src/gobby/ai/_tool_chat_builder.py`.
- 6.3.3 - `_tool_chat_spawn.py` docstring reflects reality and `__all__`
  lists the locally defined adapters. file:
  `src/gobby/ai/_tool_chat_spawn.py`.
- 6.3.5 - The tool-chat route's `investigation.unsupported_limits` reports the
  exact sorted values for an SDK-routed request, completing the assertion 3.1.4
  scopes to legacy adapters. file: `src/gobby/servers/routes/llm.py`. test:
  `tests/servers/routes/test_llm_routes.py`.
- 6.3.6 - Tool-chat route resolution is re-read per request through the config
  getter: a flip delivered by a `set_runtime_config` rebind between two requests
  changes the adapter selected for the second. test:
  `tests/ai/test_tool_chat_service.py`.
- 6.3.7 - The `servers/http.py` fallback construction passes the runtime, so a
  tool-chat service built on that path resolves `sdk` routes identically to one
  built in `_init_llm_service`. file: `src/gobby/servers/http.py`. test:
  `tests/ai/test_tool_chat_service.py`.

## P7: Droid SDK Web Chat
`kind: framing`

**Goal**: A full-protocol Droid web-chat session on droid-sdk with the backend
pinned per conversation.

### 7.1 Droid SDK event translation [category: code] (depends: 1.1)
`kind: deliverable`

Target: `src/gobby/servers/websocket/chat/backends/droid_sdk_events.py` (new)

Pure functions translating droid-sdk typed events into the existing
`StreamEvent` vocabulary emitted by `droid_stream.py` today (peer module):
text/tool/token-usage events, plan-tool passthrough shapes consumed by
`droid_plan`, permission-request surfacing, and normalization of
provider-emitted automatic compaction events when present. No JSON-RPC/JSONL
parser reuse.

**Acceptance:**

- 7.1.1 - Golden-table tests map every supported typed event to the same
  `StreamEvent` shapes `droid_stream.py` produces, including token usage and
  automatic-compaction normalization. file:
  `src/gobby/servers/websocket/chat/backends/droid_sdk_events.py`. test:
  `tests/servers/websocket/chat/test_droid_sdk_events.py`.

### 7.2 DroidSDKChatSession and backend [category: code] (depends: 7.1, 2.4)
`kind: deliverable`

Target: `src/gobby/servers/websocket/chat/backends/droid_sdk.py` (new)

`DroidSDKChatSession(ManagedWebChatPermissionsMixin, ManagedChatSessionBase)`
implementing `ChatSessionProtocol`, plus `DroidSDKWebChatBackend`:

- Dedicated droid-sdk client per conversation via
  `runtime.droid_web_chat_client(conversation_id)`; orchestration-only module
  per the droid decomposition convention — plans via `droid_plan.py`,
  permissions via `droid_permissions.py`, tool names via
  `droid_tool_name_adapter` (all reused as-is).
- Supports new + resumed sessions, settings, images, streaming `ChatEvent`s,
  usage accumulation, cancellation/interrupt, plans, lifecycle callbacks;
  `ask_user` requests cancelled; **no** explicit compact action;
  `web_chat_backend = "sdk"` classvar (`ManagedChatSessionBase` gains the
  `"legacy"` default).
- `start()` raises on runtime-not-ready or init failure **without** side
  effects — never constructs a legacy session; backend `health()` returns
  `ProviderBackendHealth`.
- Provider exceptions are sanitized at this boundary exactly as in the adapters
  (2.9): wrapped in a sanitized domain error and raised `from None`, so the web
  chat error paths — which use `logger.exception` and fold `str(exc)` into debug
  payloads — cannot format a raw droid-sdk message. Web chat is the one SDK
  surface not covered by the adapter list, and it is the surface closest to the
  user.

**Acceptance:**

- 7.2.1 - Session implements the full chat-session protocol (streaming,
  images, usage, interrupt, plans, permissions, lifecycle) against a fake
  client factory. symbol: `DroidSDKChatSession`. test:
  `tests/servers/websocket/chat/test_droid_sdk_backend.py`.
- 7.2.2 - `ask_user` requests are cancelled and no compact action is
  exposed. test: `tests/servers/websocket/chat/test_droid_sdk_backend.py`.
- 7.2.3 - `start()` failure raises with no legacy session and no client
  leak. test: `tests/servers/websocket/chat/test_droid_sdk_backend.py`.
- 7.2.4 - A droid-sdk exception carrying a home path, an env value, and a
  token-shaped string surfaces as a sanitized error with its cause suppressed,
  and a `logger.exception` capture of the failure contains none of the three.
  test: `tests/servers/websocket/chat/test_droid_sdk_backend.py`.

### 7.3 Backend selection and pinning [category: code] (depends: 7.2, 1.4)
`kind: deliverable`

Target: `src/gobby/servers/websocket/chat/runtime_manager.py`,
`src/gobby/servers/websocket/chat/_session.py`,
`src/gobby/servers/websocket/chat/_web_chat_pin.py`,
`src/gobby/servers/websocket/chat/backends/base.py`

- `WebChatRuntimeManager.__init__` gains `agent_sdk_runtime` **and
  `config_getter`**, and conditionally builds the droid SDK backend. The getter
  is required, not optional: the manager is one of the components Constraints
  names as capturing config, and it must keep working when
  `agent_sdk_runtime` is `None` (all-legacy, or a failed construction), so it
  cannot borrow the runtime's. It is constructed in `init_servers` where
  `services` is in scope, so it receives `lambda: services.config or
  runner.config` directly (2.5).
  `create_session(..., web_chat_backend: str | None = None)` — for droid,
  `resolved = web_chat_backend or resolve_web_chat_backend(config_getter(),
  "droid")` (the stored pin wins; `None` means unpinned, so resolve live);
  `"sdk"` → `DroidSDKChatSession`; runtime absent/unready → raise (no legacy
  fallback). All other providers ignore the kwarg.
- `_session.py`, three minimal edits, all inside the existing
  per-conversation creation lock (`_create_chat_session_inner`, so concurrent
  first connects are already serialized):
  1. **The pin is read only from the pre-create lookup.** `create_session` is
     called at `:446-459`, which is *before* `session_manager.register(...)` at
     `:685-700`. The only stored row available at selection time is
     `existing_db_session`, resolved at `:350-366`. Pass
     `existing_db_session.web_chat_backend` (or `None` when there is no row)
     into `create_session`; `None` ⇒ resolve from live config. The register
     branch cannot contribute to selection, because by the time it runs the
     session object already exists.
  2. **Harden the lookup, and never treat a failed lookup as unpinned.** The
     `except` at `:364-365` currently swallows any lookup error to a `debug`
     line and leaves `existing_db_session = None`. For droid web chat that
     turns a transient DB error into a silent re-resolution from live config,
     which can serve a conversation pinned `sdk` on the legacy backend — a
     direct violation of the immutability invariant. A lookup failure must
     raise rather than fall through to selection: an errored connect the client
     can retry is correct; a conversation quietly served by the wrong backend is
     not.

     **Fail closed unconditionally, not just when the provider is droid.** The
     obvious narrowing — raise only for droid — does not work, and this is the
     subtle part. The lookup at `:350-366` runs *before* effective-provider
     resolution (`effective_provider` is computed at `:332-346` from the request
     and the configured binding), and for a reconnect that omits the provider,
     or supplies a stale non-droid one, the stored row is the only evidence that
     the conversation is droid at all. Conditioning the raise on a provider
     value that the missing row was supposed to supply is circular: exactly the
     reconnects most likely to mis-resolve are the ones the condition excludes.
     Any durable web-chat lookup failure therefore fails the connect, for every
     provider. This is strictly simpler than the conditional version, and it
     costs only a retryable error on a path that is already broken.
  3. Leave `session_manager.register(...)` (`:685-700`) untouched, so the row
     is created unpinned and `db_session_id`/`seq_num` remain available to the
     wiring at `:698-758` that consumes them. If register returns a
     **pre-existing** row whose `web_chat_backend` is non-NULL and disagrees
     with the backend already selected in step 1, the row wins and the
     mismatched session is torn down and the connect fails; the pin is never
     rewritten to match a session that should not have been built. This is a
     lost race that the per-conversation lock makes rare, not impossible
     (a different daemon process can pin concurrently).
  4. **After** `await session.start(...)` returns successfully (`:829/836`),
     call `pin_web_chat_backend(session.db_session_id,
     session.web_chat_backend)` for web-chat providers whose stored pin was
     `NULL`. This write is **not** best-effort, because both of its failure
     modes leave a live session that disagrees with the durable record:
     - **Returns `False`** (the row was pinned concurrently — the conditional
       update matched nothing). The per-conversation creation lock is
       process-local and cannot serialize a second daemon, so two daemons can
       select opposite backends from the same `NULL` row and both start. Exactly
       one wins the pin; the loser must not keep serving. Re-read the stored
       value, and if it disagrees with the running session, tear the session
       down and fail the connect — the same teardown path as the step-3
       mismatch. The database value is the winner by definition; the losing
       session is the thing that has to go.
     - **Raises** (DB error). Fail closed the same way: tear down and fail the
       connect. An unpinned live session is precisely the state that lets a
       later reconnect resolve a different backend, so allowing it to continue
       trades a retryable error for a silent split-brain conversation.

     Both paths log at the same severity as the existing register failure at
     `:741`. The cost is a failed connect the client retries; the alternative is
     a conversation served by two backends.

Pinning after a proven start is what makes the invariant honest: the stored
value always names a backend that actually served this conversation. A failed
Droid SDK start leaves the row unpinned, so a retry re-resolves from current
config instead of inheriting a pin that never worked. Once written the value
is authoritative on every reconnect and never changes — including across a
config flip in either direction.

**Pinned `sdk` under an all-legacy config.** This is the one place the plan's
"rollback is a config change" claim does not hold, and it is deliberate. A
conversation pinned `sdk` keeps requesting the SDK backend after an operator
rolls `droid.web_chat` back to `legacy`, because the pin is authoritative and
there is no fallback. Behavior in that state is fully specified:
`resolve_web_chat_backend` is not consulted (the pin wins), the runtime is
required, and if it is absent or unready the connect fails with the same
`CapabilityUnavailableError`-shaped error as any other unready SDK route. The
rollback does **not** implicitly spawn an SDK child: every `ensure_provider`
trigger (2.4) is gated on the provider being configured `sdk`, and after a
rollback it is not — and `reconcile()` will already have drained it. A pinned
conversation therefore fails fast rather than resurrecting a runtime the
operator just disabled. Operator remediation is
explicit and out-of-band: clear the pin via the `_bulk_update` admin path (1.4)
to let the conversation re-resolve, or start a new conversation. 8.2 documents
this as the one non-config-only rollback.

**Line budget.** `_session.py` is 988 lines and the repo caps non-test sources
at 1,000. The four edits above are additive and would cross it. Extract the pin
read, the droid lookup-failure policy, and the post-start pin write into
`src/gobby/servers/websocket/chat/_web_chat_pin.py`, leaving `_session.py` with
call sites only. That keeps the file under the cap without a speculative
refactor of unrelated code.

**Acceptance:**

- 7.3.1 - New droid conversation with route=sdk creates a
  `DroidSDKChatSession` and pins the row `sdk` only after `start()` succeeds;
  a stored pin beats a later config flip in both directions. file:
  `src/gobby/servers/websocket/chat/runtime_manager.py`. test:
  `tests/servers/websocket/chat/test_runtime_manager.py`.
- 7.3.2 - Runtime unavailable + route=sdk errors without creating any
  legacy session and leaves the row unpinned, so a retry after the runtime
  recovers (or after a flip to legacy) resolves from current config. test:
  `tests/servers/websocket/chat/test_provider_routing.py`.
- 7.3.3 - Codex/Claude/Qwen web-chat creation paths are behaviorally
  unchanged. test:
  `tests/servers/websocket/chat/test_provider_routing.py`.
- 7.3.4 - Two independent runtime managers with opposite configs racing the
  same `NULL` row persist exactly one backend value, and the manager that loses
  the pin tears its session down and fails the connect instead of continuing to
  serve; a `pin_web_chat_backend` write that raises fails closed the same way,
  leaving no live unpinned session. test:
  `tests/servers/websocket/chat/test_runtime_manager.py`.
- 7.3.5 - Backend selection reads the pin from the pre-create lookup only: a
  row pinned `sdk` selects the SDK backend even with live config set to
  `legacy`, and a lookup failure raises instead of resolving from live config —
  including when the reconnect omits the provider or supplies a stale non-droid
  one, and on the `continue_in_chat` path. file:
  `src/gobby/servers/websocket/chat/_web_chat_pin.py`. test:
  `tests/servers/websocket/chat/test_provider_routing.py`.
- 7.3.6 - A conversation pinned `sdk` under an all-legacy config fails the
  connect with the unready-SDK error and spawns no SDK child process, and
  clearing the pin through the admin path lets the next connect re-resolve.
  test: `tests/servers/websocket/chat/test_provider_routing.py`.
- 7.3.7 - `_session.py` stays under the 1,000-line cap, with the pin read,
  lookup-failure policy, and post-start pin write living in
  `_web_chat_pin.py`. file:
  `src/gobby/servers/websocket/chat/_session.py`.
- 7.3.8 - `WebChatRuntimeManager` resolves the droid backend through its
  `config_getter` on every unpinned creation, so a `set_runtime_config` rebind
  between two creations changes the backend selected for the second; the
  manager also works with `agent_sdk_runtime=None`. file:
  `src/gobby/servers/websocket/chat/runtime_manager.py`. test:
  `tests/servers/websocket/chat/test_runtime_manager.py`.

## P8: Rollout Hardening
`kind: framing`

**Goal**: Opt-in live verification and accurate documentation; ship all-legacy.

### 8.1 Opt-in authenticated live tests [category: test] (depends: 4.3, 6.3, 7.3)
`kind: deliverable`

Target: `tests/ai/agent_sdk/test_live_sdk.py` (new), `pre-push-test.sh`,
`tests/ci/test_postgres_test_stack.py`

Live smoke tests gated `GOBBY_RUN_CODEX_SDK_LIVE=1` /
`GOBBY_RUN_DROID_SDK_LIVE=1` (+ `shutil.which` skips), marked `integration`,
disabled by default: SDK text round-trip, account rejection path, droid lease
round-trip. Register both gates in the `pre-push-test.sh` deselect block and
the gate assertion list (:318-331).

**Acceptance:**

- 8.1.1 - Live tests skip cleanly without env/CLI and the gate-list
  assertion passes with both new gates. file: `pre-push-test.sh`. test:
  `tests/ci/test_postgres_test_stack.py`.

### 8.2 Documentation updates [category: docs] (depends: 4.3, 5.3, 6.3, 7.3)
`kind: deliverable`

Target: `docs/guides/ai-configuration.md`, `docs/guides/llm-features.md`,
`docs/guides/providers-and-models.md`, `docs/guides/ai-daemon-contract.md`

Document `ai.agent_sdk_routes` (defaults, per-capability promotion, rollback
semantics, no-fallback semantics, vision's legacy=unavailable), the
`AgentSDKRuntime` diagnostics block, web-chat backend pinning, and
disambiguate the daemon `ai.agent_sdk_routes` namespace from the gcore/CLI
`ai.*` namespace. Refresh `_Last verified_` footers.

Also document the caller-visible surfaces the routes add: the
`unsupported_limits` field on the tool-chat route's `investigation` block
(3.1), the `queue_wait_ms` / concurrency-gauge distinction between admission
delay and provider latency (2.8/2.9), and the three-state web-chat pin
(unpinned → written once after a successful first start → immutable).

Two operator-facing caveats must be stated plainly rather than left implied:

- **Rollback is config-only for text generation, vision, and tool chat, but
  not for droid web chat.** A conversation already pinned `sdk` keeps requiring
  the SDK runtime after a rollback to legacy and fails the connect if the
  runtime is gone. Document the remediation from 7.3 (clear the pin via the
  admin path, or start a new conversation) next to the claim, so an operator
  planning a rollback learns the exception before performing one. For the three
  capabilities where rollback *is* config-only, say what that actually means
  operationally: the flip takes effect on the next request, and reconciliation
  (2.6) drains the provider's child processes and clears its degraded entries
  without a restart, so `/api/admin/status` returns to `{}`.
- **`queue_wait_ms` measures runtime admission only.** SDK-routed text requests
  still pass the text-generation service's spawn-cold admission gate before
  reaching the runtime (2.9), so a saturated daemon can report low
  `queue_wait_ms` while requests genuinely queue upstream. An operator reading
  these gauges to size concurrency needs to know they do not cover the whole
  wait.

**Acceptance:**

- 8.2.1 - All four guides describe the new routes, diagnostics, and pinning
  accurately with the daemon/CLI namespace distinction. file:
  `docs/guides/ai-configuration.md`.
- 8.2.2 - The guides document `unsupported_limits`, the admission-vs-provider
  latency split including the residual spawn-cold gate outside `queue_wait_ms`,
  and the pin lifecycle. file: `docs/guides/llm-features.md`.
- 8.2.3 - The rollback section states the droid web-chat exception and its
  remediation rather than claiming rollback is config-only for every
  capability. file: `docs/guides/ai-configuration.md`.

## V1 Plan Changelog
`kind: verification`

<!-- Enhancement and adversarial review rounds append entries here. -->

**Round 1** `kind: enhancement`

- enhancer_run: ac6b1de4-5fbc-4ae0-a6ef-5c0b730482f9
- enhancer_session: 5adb2615-a0ca-41aa-beee-c38f5471c0a3
- converged: false
- suggestions_presented: 5
- accepted:
  - E1 / better / caller-visible `unsupported_limits` on the tool-chat route's
    `investigation` block
  - E2 / better / dispatch-snapshot rule + cross-capability route-flip
    contract test
  - E3 / better / web-chat pin written only after a successful first start
    (accepted with a corrected implementation; see notes)
  - E4 / better / offline SDK surface-compatibility test at P1
  - E5 / better / `queue_wait_ms` and concurrency gauges separating admission
    delay from provider latency
- declined:
  - (none)
- resolution_notes: All five suggestions were folded into the artifact and
  `uv run gobby plans validate` passes (8 phases, consumer sweep green).
  Details below.

- **E1 accepted** (§ 3.1) — `unsupported_limits` was log-only, so callers had
  no machine-readable statement of unenforced limits. Verified the gap at
  `src/gobby/servers/routes/llm.py:312-318`; the `investigation` block now
  carries the field (acceptance 3.1.4), added to Targets and to the 8.2 docs.
- **E2 accepted** (§§ 4.3, 5.1, 6.3) — added the binding **dispatch-snapshot
  rule**: the route is snapshotted into a local before the first `await` and
  governs the whole request; unavailability yields one attempt on the selected
  backend with zero construction or invocation of the other. New table-driven
  contract test `tests/ai/agent_sdk/test_route_dispatch_contract.py`
  (acceptance 4.3.4, 5.1.2, 6.3.4). Vision called out explicitly because its
  selection was split between a TTL-cached registry probe and the adapter
  re-check.
- **E3 accepted with a corrected fix** (§§ 1.3, 1.4, 7.3) — the diagnosis was
  right: registration at `_session.py:687-697` precedes `session.start()` at
  `:829/836`, so pinning at registration records a backend that may never have
  served the conversation. The proposed fix (defer registration until after
  `start()`) was rejected: `db_session_id`/`seq_num` are consumed at
  `:698-758`, before start, so reordering breaks the chat-mode persistence and
  event wiring. Replaced with a write-once pin after a successful start
  (`pin_web_chat_backend`, `UPDATE ... WHERE web_chat_backend IS NULL`), which
  is race-safe under the existing per-conversation creation lock.
  **This forced a schema correction in 1.3**: the column becomes nullable
  (`TEXT`, CHECK `IS NULL OR IN ('legacy','sdk')`) instead of
  `NOT NULL DEFAULT 'legacy'`. A two-state column cannot distinguish *unpinned*
  from *pinned legacy*, and without that third state the post-start pin either
  strands failed SDK starts on an `sdk` pin or lets a config flip migrate a
  live legacy conversation to the SDK backend on reconnect.
- **E4 accepted** (§ 1.1) — added an offline SDK surface-compatibility test
  (`tests/ai/agent_sdk/test_sdk_surface.py`, acceptance 1.1.3) so patch drift
  inside the pinned ranges fails at P1 rather than mid-adapter-work.
- **E5 accepted** (§§ 2.3, 2.4, 2.8, 2.9) — added `queue_wait_ms` around
  admission (disjoint from `latency_ms`) plus integer concurrency/pool gauges
  in the sanitized status snapshot and `agent_sdk_call` event, with fake-clock
  tests. Counts only; the content/credential/path ban is unchanged.

Deferred to adversarial review (raised while applying E3, not resolved here):
a conversation pinned `sdk` still requires the SDK runtime after a config
rollback to legacy, which qualifies the plan's "rollback is a config change"
claim.

**Round 1** `kind: verification`

- reviewer_run: 37b596ac-4ede-4f3a-8546-0e5653780049
- reviewer_session: 47225731-0706-43d1-8003-7db4fdd59264
- verdict: needs_review
- findings:
  - F1 / high / §§ 2.4, 4.3, 5.2, 6.3 / `set_runtime_config` rebinds
    `services.config`, so any captured config object is blind to route flips
  - F2 / high / § 4.3 / the service's JSON-path probe and adapter dispatch are
    separated by an admission `await`, so an adapter-local snapshot cannot
    govern both
  - F3 / high / §§ 7.3, 2.4, 2.6, 8.2 / unqualified "rollback is a config
    change" conflicts with the immutable web-chat pin
  - F4 / high / §§ 7.3, 1.4 / `create_session` precedes `register`, so the
    register branch cannot feed a stored pin into selection; a swallowed lookup
    failure serves a pinned row from live config
  - F5 / medium / §§ 3.1, 6.1, 6.2, 6.3 / acceptance 3.1.4 requires an SDK
    adapter that only exists after sections depending on 3.1
  - F6 / medium / §§ 5.1, 6.3, 4.1, 4.2, 4.3 / sections edit files their
    non-dependencies create
  - F7 / medium / §§ 2.3, 2.4, 2.6 / no component ever calls `ensure_provider`,
    so recovery requires a daemon restart
  - F8 / high / §§ 2.5, 4.3, 5.3, 6.3 / builders run in Phase 2 but the runtime
    is constructed in Phase 4; `servers/http.py:100-104` is an unlisted second
    tool-chat construction site
  - F9 / medium / §§ 2.9, 4.1, 4.2 / `feature_llm_call` logs `str(error)`
    verbatim, bypassing `sanitize_sdk_error`
  - F10 / medium / §§ 6.2, 3.1 / no 6.2 acceptance pins the droid
    usage/`unsupported_limits` contract
  - F11 / medium / §§ 2.4, 7.2, 2.1 / `droid_web_chat_client` states no
    transport or env isolation, leaving requirement 4 uncovered for web-chat
    children
  - F12 / low / §§ 1.2, 2.4 / hard requirement 7 has no covering acceptance
    item or test
  - F13 / low / §§ 2.4, 2.9, 4.3 / SDK-routed text still passes the spawn-cold
    admission gate that `queue_wait_ms` cannot observe
  - F14 / low / § 7.3 / `_session.py` is 988 of 1,000 permitted lines
  - F15 / low / § 5.3 / wiring references an undeclared
    `runner.vision_extract_service`
  - F16 / medium / §§ 2.9, 4.3 / acceptance 2.9.1 asserts both routes before
    4.3 builds the sdk route
  - F17 / medium / §§ 5.3, 2.5 / 5.3 consumes a 2.5 output with no ordering edge
  - F18 / low / §§ 8.2, 2.8, 2.9 / 8.2 documents 2.8/2.9 surfaces outside its
    depends closure — **declined**
- resolution_notes: 17 of 18 findings accepted and applied; F18 declined (8.2
  is a docs deliverable written against merged surfaces, so the ordering edge
  buys nothing). Additionally revived dismissed candidate RI-8 — its dismissal
  rested on the registry's 2.0s default TTL, but § 5.2 passed
  `availability_probe_ttl_seconds` without committing to a value, so the TTL is
  now pinned explicitly with both cache polarities documented (acceptance
  5.2.3). F1, F2, and F4 each invalidated part of the preceding enhancement
  round: E2's "snapshot before the first `await`" is replaced by a
  single-entry-point snapshot with `generate_json` owning the whole JSON
  decision (`supports_native_json` removed entirely), and E3's pin read path is
  re-sourced from the pre-create lookup. F8 moved runtime construction from
  `init_servers` (Phase 4) to before `_init_llm_service` (Phase 2). F3 is
  resolved by qualifying the rollback claim in Overview/Constraints and
  specifying the pinned-`sdk`-under-legacy branch with remediation in 7.3.
  `uv run gobby plans validate` passes (8 phases, consumer sweep green).
  This round returned no canonical result: the reviewer completed
  `validate_plan_review_coverage` (`ok: true`, attestation digest
  5733e46052646eb8059b4c4b93bfdb9a747d3c088c95473ec68459956b702a9a, 3 lanes,
  23 candidates, 18 emitted, 5 dismissed, shadow manifest valid at 28 entries)
  and then called `end_agent_run` with empty arguments, so the findings were
  recovered from the server-validated payload rather than from a returned
  round result. No `render_v1_round_checkpoint` fence exists for this round.

**Round 2** `kind: verification`

- reviewer_run: 9480c6ea-a239-4cb0-a5c7-146e34d1f9a9
- reviewer_session: ef373aeb-69a3-4036-8b28-8c0b77e5dc10 (#9624)
- reviewer_model: codex / gpt-5.6-sol / xhigh
- evidence_id: 90dcf2db-dec8-4d88-859d-3705b61835c8
- plan_hash: bc90761cb4a06ad1b8e9029baaaa2a5baaa651645d0ca85ece509f0b39912424
- verdict: needs_review
- attestation_digest: 4e2c5efd1bdf1920d85369d654c99387a7d892e33aafec9b78171859a65b906a
- lanes: requirements_traceability (4), repository_blast_radius (5),
  runtime_invariants (4); 13 candidates, 12 emitted, 1 dismissed; shadow
  manifest valid at 28 entries
- findings (all severity `blocking`):
  - R2-F1 / §§ 2.4, 2.5, 4.3, 5.2, 6.3, 7.3 / the getter is rooted at
    `runner.config`, but `set_runtime_config` rebinds only `services.config`;
    7.3 has no getter at all
  - R2-F2 / §§ 2.4, 2.5, 2.6, 2.7 / no config-change path drains a provider or
    clears its degraded entry when the last sdk route flips to legacy
  - R2-F3 / §§ 2.4, 5.2, 7.2, 7.3 / `ensure_provider` is reachable only from
    the lease paths, so vision-only and web_chat-only configs never recover
  - R2-F4 / §§ 1.4, 7.3 / the process-local lock cannot serialize two daemons;
    the daemon losing `pin_web_chat_backend` keeps serving the wrong backend
  - R2-F5 / § 7.3 / a lookup failure can hide a pinned droid row when the
    reconnect omits or misstates the provider
  - R2-F6 / §§ 2.9, 4.1, 4.2, 5.1, 6.1, 6.2, 7.2 / `logger.exception` formats
    `__cause__`, so wrapping without suppressing the cause still leaks;
    `DroidSDKChatSession` was omitted from the sanitization list
  - R2-F7 / §§ 2.5, 4.3, 6.3 / 2.5 passes `agent_sdk_runtime` to builders whose
    kwargs 4.3 and 6.3 create, neither of which precedes it
  - R2-F8 / §§ 4.3, 5.1 / 5.1 cites the dispatch-snapshot rule and
    `test_route_dispatch_contract.py` without a 4.3 edge
  - R2-F9 / §§ 2.9, 4.1, 4.2 / acceptance 2.9.3 needs an SDK-adapter-to-service
    path that does not exist at 2.9's completion
  - R2-F10 / §§ 2.9, 4.3 / the service labels any callable `generate_json`
    `provider_structured`, so the routing wrapper mislabels every legacy JSON
    request
  - R2-F11 / § 2.5 / construction sits outside `_init_llm_service`'s try, so a
    constructor failure aborts `init_services` instead of degrading
  - R2-F12 / §§ 5.1, 5.2 / an uncached vision request resolves the route twice
- resolution_notes: all 12 accepted; 3 narrowed to less mechanism than the
  reviewer proposed. R2-F1, R2-F9, and R2-F10 are defects in Round 1's own
  fixes. R2-F1: verified against the repo — `set_runtime_config` rebinds
  `self.server.services.config` and nothing else, and `ServiceContainer`
  (`runner_init/servers.py:33-60`, Phase 4) is seeded from `runner.config` and
  diverges on the first write; the runtime now owns one getter, bound to
  `runner.config` at Phase 2 and rebound to `services.config` in `init_servers`,
  and every routed capability takes it from there (7.3 receives it directly,
  since it must work with `agent_sdk_runtime=None`). R2-F2 and R2-F3 collapsed
  into one addition rather than two: a single `reconcile()` on the runtime,
  triggered from `set_runtime_config`, which both drains providers that lost
  their last sdk route and initializes ones that gained a route — plus two
  one-line `ensure_provider` triggers at the vision probe and
  `droid_web_chat_client`, the two capabilities that never reach a lease. No
  polling loop and no new service. R2-F10: verified — `_text_generation_service.py:593`
  sets `provider_structured` unconditionally when `generate_json` is callable;
  the "service dispatch is diff-free" claim is withdrawn, and `generate_json`
  may now return `StructuredJsonResult(data, parse_outcome)` which the service
  unpacks, leaving every existing adapter untouched. The legacy branch must
  reuse `_json_request` / `_parse_json_text` / `_json_parse_failure` so the
  failure shape cannot drift. R2-F11: verified — `_init_llm_service`
  (`runner_init/services.py:56-73`) owns the only `except`, so construction
  moved to the first statement inside its `try`. R2-F5 and R2-F12 were both
  narrowed to *less* mechanism: F5's fail-closed rule drops the "when the
  provider is droid" condition entirely (the condition was circular — the
  missing row is what identifies the provider), and F12 is resolved as a
  specification correction rather than a redesign, stating that the registry
  probe is availability status and `extract()` is the single dispatch
  resolution, which preserves the stale-positive protection a literal
  single-resolution rewrite would have discarded. Two candidates from the
  requirements_traceability lane were dropped by the reviewer: RT-4 (a narrowed
  re-raise of Round 1's declined F18) and RT-5 (`.coverage-ledger.yaml`, absent
  for every plan in the repo, so a stale contract clause rather than a defect
  here). `uv run gobby plans validate` passes (8 phases, consumer sweep green).
