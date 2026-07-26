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
`CapabilityUnavailableError`. Rollback is a config change; every legacy path
is preserved.

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
- **No fallback / no shadow / no duplicate requests.** Route resolved once per
  request from live config; droid web-chat backend pinned at conversation
  creation via `sessions.web_chat_backend` (authoritative on reconnect,
  immutable mid-conversation).
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
  configuration are not degraded).
- New non-test source files stay under 1,000 lines (split modules as listed).

## P1: Foundations
`kind: framing`

**Goal**: Dependencies, config surface, and the sessions schema exist so every
later phase can build against them.

### 1.1 Add subscription SDK dependencies [category: config]
`kind: deliverable`

Target: `pyproject.toml`

Add to `[project].dependencies`:

```toml
"openai-codex>=0.144.4,<0.145",
"droid-sdk>=0.1.2,<0.2",
```

Run `uv lock`. Watch the resolution interaction with the existing
`openai>=1.0.0` pin (used only by embeddings/local adapters); if `openai-codex`
raises the `openai` floor, the embeddings and local-provider test suites must
pass against the resolved version.

**Acceptance:**

- 1.1.1 - Both SDKs declared with the pinned ranges and `uv lock` resolves
  cleanly. file: `pyproject.toml`.
- 1.1.2 - `import codex` (openai-codex) and `import droid_sdk` succeed in the
  project venv; embeddings/local tests pass against the resolved `openai`.
  test: `tests/ai/test_endpoints.py`.

### 1.2 Agent SDK routes config model [category: code]
`kind: deliverable`

Target: `src/gobby/config/ai.py`, `docs/audits/configuration-audit.md`,
`web/src/components/settings/sections/ProvidersModelsSection.tsx`

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

### 1.3 Sessions web_chat_backend migration [category: code]
`kind: deliverable`

Target: `src/gobby/storage/migrations/343_session_web_chat_backend.sql`,
`src/gobby/storage/postgres_baseline_schema.sql`,
`tests/storage/test_migration_contract.py`

Migration `343_session_web_chat_backend.sql` (343 is the next contiguous
number after `342_task_validation_epoch.sql`):

```sql
ALTER TABLE sessions
    ADD COLUMN web_chat_backend TEXT NOT NULL DEFAULT 'legacy';

ALTER TABLE sessions
    ADD CONSTRAINT sessions_web_chat_backend_valid
    CHECK (web_chat_backend IN ('legacy', 'sdk'));
```

Mirror both statements in the baseline `sessions` table (column after
`sandbox_policy_hash`, constraint alongside the named CHECKs at lines 237-267).
Existing rows backfill to `legacy` via the default. Add a schema contract test
(pattern: `test_memory_dream_due_version_schema_contract`, lines 301-310)
asserting the column and CHECK strings appear in BOTH files.

**Acceptance:**

- 1.3.1 - Migration 343 adds the NOT NULL DEFAULT 'legacy' column plus
  legacy|sdk CHECK. file:
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

Follow the `sandbox_policy_hash` precedent trail:

- `Session` dataclass: `web_chat_backend: str = "legacy"`; `from_row` guard
  (`row["web_chat_backend"] if "web_chat_backend" in row.keys() else
  "legacy"`); include in `to_dict`.
- `register` (Protocol + mixin): `web_chat_backend: str = "legacy"` kwarg,
  INSERT column + param.
- `create_web_chat_session`: accept + validate against `{'legacy','sdk'}`,
  pass through.
- `_bulk_update.update`: optional `web_chat_backend` for test/admin tooling.
- `_upsert.update_existing_session`: **no mutation** — pinned-at-creation
  invariant, add a comment stating it.

The field is ignored for non-web-chat sessions (default covers them); no
changes to agent-spawn or terminal-session registration call sites.

**Acceptance:**

- 1.4.1 - `Session` round-trips `web_chat_backend` through register →
  from_row → to_dict with default `legacy`. symbol: `Session`. test:
  `tests/storage/sessions/test_models.py`.
- 1.4.2 - `create_web_chat_session` persists a validated backend value and
  rejects invalid values. file: `src/gobby/storage/sessions/_web_chat_crud.py`.
- 1.4.3 - Upsert/reconnect never mutates a stored `web_chat_backend`.
  test: `tests/storage/sessions/test_registration.py`.

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

**Acceptance:**

- 2.3.1 - Each lease initializes exactly one fresh session and the leased
  client is closed and replaced afterward (success, error, and cancellation
  paths). symbol: `DroidSdkClientPool`. test:
  `tests/ai/agent_sdk/test_droid_pool.py`.
- 2.3.2 - Pool size respects `spawn_cold_max_concurrency`; excess leases
  queue. test: `tests/ai/agent_sdk/test_droid_pool.py`.
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
  sdk after startup (live config via `config_getter` — `save_config_values`
  swaps `services.config` in place, so reads are live).
- `codex_thread()` / `droid_lease()` context managers with
  `Semaphore(max_concurrency)` admission; raise `AgentSdkUnavailableError`
  when closed/unavailable (callers translate to `CapabilityUnavailableError`).
- `droid_web_chat_client(conversation_id)` create/release — dedicated client
  per SDK web-chat conversation (not pool-leased).
- `provider_status()` / `status_snapshot()` (empty dict when nothing
  configured); `close()` idempotent, bounded ~5s, records per-child
  `cleanup_outcome` (`closed|killed|leaked`).

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
  provider or capability and reads the live config object. symbol:
  `resolve_agent_sdk_route`. test: `tests/ai/agent_sdk/test_routes.py`.
- 2.4.3 - Leases raise `AgentSdkUnavailableError` after `close()`;
  `ensure_provider` is single-flight and recovers a previously failed
  provider. test: `tests/ai/agent_sdk/test_runtime.py`.
- 2.4.4 - `sanitize_sdk_error` never emits env values, tokens, or home
  paths. symbol: `sanitize_sdk_error`. test:
  `tests/ai/agent_sdk/test_diagnostics.py`.

### 2.5 Construction and rollback wiring [category: code] (depends: 2.4)
`kind: deliverable`

Target: `src/gobby/runner_init/servers.py`, `src/gobby/runner.py`,
`src/gobby/app_context.py`, `src/gobby/runner_rollback.py`

- `runner_init/servers.py`: after the codex_client block (:94-102), construct
  `runner.agent_sdk_runtime = AgentSDKRuntime(config_getter=lambda:
  services.config, ...)` (sync constructor spawns nothing) and set
  `services.agent_sdk_runtime`.
- `runner.py`: `agent_sdk_runtime: AgentSDKRuntime | None` in the Phase-4
  field block (:189-192).
- `app_context.py`: `agent_sdk_runtime` field on `ServiceContainer`.
- `runner_rollback.py`: add `("agent sdk runtime",
  getattr(runner, "agent_sdk_runtime", None))` to the resources tuple
  (:91-95); `_settle_async_close` already handles the coroutine.

**Acceptance:**

- 2.5.1 - Runtime constructed in `init_servers`, visible on both
  `GobbyRunner` and `ServiceContainer`, with zero child processes at
  construction. file: `src/gobby/runner_init/servers.py`.
- 2.5.2 - Construction-failure rollback closes the runtime. file:
  `src/gobby/runner_rollback.py`.

### 2.6 Pre-readiness eager start and degradation [category: code] (depends: 2.5)
`kind: deliverable`

Target: `src/gobby/runner_lifecycle_startup.py` (new),
`src/gobby/runner_lifecycle.py`

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

**Acceptance:**

- 2.6.1 - HTTP readiness is announced only after SDK init settles, and a
  failed probe never prevents daemon startup. behavior: "non-fatal
  pre-readiness init" in `src/gobby/runner_lifecycle_startup.py`.
- 2.6.2 - Probe failure of a configured provider adds
  `agent_sdk_<provider>` to `runner.degraded_services`; all-legacy adds
  nothing. test: `tests/runner/test_lifecycle_startup.py`.

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
  "startup_error": "<sanitized>", "routes": {"text_generate": "sdk", ...}}}
```

Empty object when every route is legacy — absent-by-configuration produces
zero signals (invariant from #16609/#18371).

**Acceptance:**

- 2.8.1 - `/api/admin/status` exposes the block with sanitized
  `startup_error` for a failed configured provider and `{}` when all-legacy.
  file: `src/gobby/servers/routes/admin/_health.py`.

### 2.9 Observability events [category: code] (depends: 2.4)
`kind: deliverable`

Target: `src/gobby/ai/_text_generation_service.py`,
`src/gobby/ai/agent_sdk/runtime.py`

- Extend the `feature_llm_call` structured log (:764-803) `extra` with
  `configured_route`, `backend` (e.g. `codex_sdk`/`codex_cli`), and
  `usage_input_tokens`/`usage_output_tokens` lifted from
  `LLMTextResult.usage` when present (covers text_generate on both routes).
- `AgentSDKRuntime` emits one `agent_sdk_call` structured log per lease:
  `provider, capability, model, configured_route, backend, ready, latency_ms,
  usage (ints), success, error (sanitized), cleanup_outcome` (covers
  tool_chat/vision/web_chat). Optionally mirror to the existing OTel helpers
  (`inc_counter`/`observe_histogram`) at the same site.
- Never log prompts, responses, credentials, or auth-file paths.

**Acceptance:**

- 2.9.1 - `feature_llm_call` carries route/backend/usage fields on both
  routes. file: `src/gobby/ai/_text_generation_service.py`. test:
  `tests/ai/test_text_generation.py`.
- 2.9.2 - `agent_sdk_call` events carry the full field set with sanitized
  errors and no content. test: `tests/ai/agent_sdk/test_runtime.py`.

## P3: Shared Capability Surfaces
`kind: framing`

**Goal**: The cross-cutting contract changes every capability route builds on.

### 3.1 Unsupported-limits reporting contract [category: code]
`kind: deliverable`

Target: `src/gobby/ai/_tool_chat_contracts.py`,
`src/gobby/ai/_tool_chat_service.py`, `src/gobby/ai/_tool_chat_codex.py`,
`src/gobby/ai/_tool_chat_droid.py`, `src/gobby/ai/_tool_chat_adapters.py`,
`src/gobby/ai/_tool_chat_spawn.py`

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

### 4.3 Text routing wrapper and builder wiring [category: code] (depends: 4.1, 4.2)
`kind: deliverable`

Target: `src/gobby/ai/agent_sdk/route_adapters.py` (new),
`src/gobby/ai/_text_generation_builder.py`,
`src/gobby/ai/_text_generation_service.py`

- `RoutingTextGenerateAdapter(provider, config, legacy_factory, sdk_factory)`:
  resolves the route at the top of each call, lazily builds and caches both
  inner adapters, exposes `supports_native_json(request)` (False when routed
  legacy — legacy CLI adapters have no `generate_json`).
- `_daemon_text_generation_adapter_factories` gains
  `agent_sdk_runtime` kwarg; when present, wraps the `"codex"`/`"droid"`
  entries **after** the endpoint loop (endpoint providers never wrapped).
- `_text_generation_service.py:573`: honor the optional
  `supports_native_json` probe before choosing the structured path — legacy
  dispatch stays byte-identical (probe absent ⇒ current behavior).

**Acceptance:**

- 4.3.1 - route=legacy dispatches to the existing CLI adapters including the
  parsed-text JSON path; route=sdk dispatches to SDK adapters with the
  structured path. symbol: `RoutingTextGenerateAdapter`. test:
  `tests/ai/test_text_generation.py`.
- 4.3.2 - Route is re-resolved per request (config flip between calls
  changes the inner adapter). test:
  `tests/ai/agent_sdk/test_route_adapters.py`.
- 4.3.3 - Endpoint-scoped providers are never wrapped. test:
  `tests/ai/test_endpoints.py`.

## P5: Vision SDK Routes
`kind: framing`

**Goal**: First-ever codex/droid vision, plus a persistent shared
`VisionExtractService`.

### 5.1 SDK vision adapters [category: code] (depends: 2.4)
`kind: deliverable`

Target: `src/gobby/ai/agent_sdk/codex_oneshot.py`,
`src/gobby/ai/agent_sdk/droid_oneshot.py`

`CodexSDKVisionExtractAdapter` (Codex `LocalImageInput(request.image_path)`)
and `DroidSDKVisionExtractAdapter` (droid typed image input), both implementing
`VisionExtractAdapter.extract`. Each re-checks route==sdk and runtime
readiness at `extract()` time and raises `CapabilityUnavailableError`
otherwise. Output flows through the existing service validation. Documented
non-goal: no usage field on `VisionExtractResult`.

**Acceptance:**

- 5.1.1 - Codex adapter passes `LocalImageInput`; droid adapter passes typed
  image input; both raise `CapabilityUnavailableError` when route=legacy or
  runtime unready. symbol: `CodexSDKVisionExtractAdapter`. test:
  `tests/ai/agent_sdk/test_vision_adapters.py`.

### 5.2 Vision registry availability gate [category: code] (depends: 2.4)
`kind: deliverable`

Target: `src/gobby/ai/registry_builder.py`, `src/gobby/ai/registry.py`,
`src/gobby/ai/vision.py`

- `_vision_extract_binding` gains optional `agent_sdk_vision_probe:
  Callable[[str], bool] | None`; for codex/droid with a probe, return an
  available-when-probed binding using `availability_probe` +
  `availability_probe_ttl_seconds` (probe = route==sdk AND
  `runtime.is_ready(provider)`); claude/grok/qwen branches unchanged; no new
  adapter style.
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

### 5.3 Persistent shared VisionExtractService [category: code] (depends: 5.1, 5.2)
`kind: deliverable`

Target: `src/gobby/app_context.py`, `src/gobby/runner_init/services.py`,
`src/gobby/runner_init/servers.py`, `src/gobby/servers/routes/llm.py`

- `ServiceContainer.vision_extract_service: VisionExtractService | None`.
- Build once in `_init_llm_service` with `agent_sdk_runtime`; pass to the
  container.
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

### 6.3 Tool-chat routing shim [category: code] (depends: 6.1, 6.2)
`kind: deliverable`

Target: `src/gobby/ai/agent_sdk/route_adapters.py`,
`src/gobby/ai/_tool_chat_builder.py`, `src/gobby/ai/_tool_chat_spawn.py`

`RoutingToolChatAdapter(config, sdk_factories, legacy_factory)` wired for the
DAEMON and CLI style factories in `build_daemon_tool_chat_service` (new
`agent_sdk_runtime` kwarg). SDK iff `binding.provider in sdk_factories` AND
`not binding.metadata.get("endpoint")` AND route==sdk; everything else legacy.
No new `AIAdapterStyle`; `_TOOL_CHAT_EXECUTABLE_STYLES`,
`_RUNTIME_ADAPTER_STYLES`, style maps, and `ToolChatService` dispatch all
unchanged (`ToolChatResult.adapter_style` still reports the binding style).
Also fix the stale `_tool_chat_spawn.py` module docstring and `__all__`
omissions while touching the area.

**Acceptance:**

- 6.3.1 - Endpoint-metadata DAEMON bindings always route legacy; codex/droid
  route=sdk dispatches to SDK adapters; grok/qwen (ACP style) untouched.
  symbol: `RoutingToolChatAdapter`. test:
  `tests/ai/test_tool_chat_service.py`.
- 6.3.2 - Style allowlists and service dispatch are diff-free apart from the
  3.1 log line. file: `src/gobby/ai/_tool_chat_builder.py`.
- 6.3.3 - `_tool_chat_spawn.py` docstring reflects reality and `__all__`
  lists the locally defined adapters. file:
  `src/gobby/ai/_tool_chat_spawn.py`.

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

**Acceptance:**

- 7.2.1 - Session implements the full chat-session protocol (streaming,
  images, usage, interrupt, plans, permissions, lifecycle) against a fake
  client factory. symbol: `DroidSDKChatSession`. test:
  `tests/servers/websocket/chat/test_droid_sdk_backend.py`.
- 7.2.2 - `ask_user` requests are cancelled and no compact action is
  exposed. test: `tests/servers/websocket/chat/test_droid_sdk_backend.py`.
- 7.2.3 - `start()` failure raises with no legacy session and no client
  leak. test: `tests/servers/websocket/chat/test_droid_sdk_backend.py`.

### 7.3 Backend selection and pinning [category: code] (depends: 7.2, 1.4)
`kind: deliverable`

Target: `src/gobby/servers/websocket/chat/runtime_manager.py`,
`src/gobby/servers/websocket/chat/_session.py`,
`src/gobby/servers/websocket/chat/backends/base.py`

- `WebChatRuntimeManager.__init__` gains `agent_sdk_runtime` and
  conditionally builds the droid SDK backend;
  `create_session(..., web_chat_backend: str | None = None)` — for droid,
  `resolved = web_chat_backend or resolve_web_chat_backend(config, "droid")`;
  `"sdk"` → `DroidSDKChatSession`; runtime absent/unready → raise (no legacy
  fallback). All other providers ignore the kwarg.
- `_session.py` (two minimal edits): pass the stored row value into
  `create_session`; register new rows with
  `web_chat_backend=getattr(session, "web_chat_backend", "legacy")` — the pin
  is written exactly once with the value that actually served the first
  session; hydrated rows never re-register, so it is immutable
  mid-conversation and authoritative on reconnect.

**Acceptance:**

- 7.3.1 - New droid conversation with route=sdk creates a
  `DroidSDKChatSession` and pins the row `sdk`; stored `legacy` beats a
  later sdk config flip. file:
  `src/gobby/servers/websocket/chat/runtime_manager.py`. test:
  `tests/servers/websocket/chat/test_runtime_manager.py`.
- 7.3.2 - Runtime unavailable + route=sdk errors without creating any
  legacy session or changing the stored pin. test:
  `tests/servers/websocket/chat/test_provider_routing.py`.
- 7.3.3 - Codex/Claude/Qwen web-chat creation paths are behaviorally
  unchanged. test:
  `tests/servers/websocket/chat/test_provider_routing.py`.

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
by config change, no-fallback semantics, vision's legacy=unavailable), the
`AgentSDKRuntime` diagnostics block, web-chat backend pinning, and
disambiguate the daemon `ai.agent_sdk_routes` namespace from the gcore/CLI
`ai.*` namespace. Refresh `_Last verified_` footers.

**Acceptance:**

- 8.2.1 - All four guides describe the new routes, diagnostics, and pinning
  accurately with the daemon/CLI namespace distinction. file:
  `docs/guides/ai-configuration.md`.

## V1 Plan Changelog
`kind: verification`

<!-- Enhancement and adversarial review rounds append entries here. -->
