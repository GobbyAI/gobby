# Subscription SDK Integration (Codex + Droid Two-Mode Strangler)

**Plan ID:** subscription-sdk-integration

## Overview
`kind: framing`

Add subscription-authenticated SDK backends — `openai-codex` (AsyncCodex) and
`droid-sdk` — alongside the existing legacy paths for Codex text_generate and
vision_extract, and Droid text_generate, vision_extract, tool_chat, and
web_chat, selected by new
`ai.agent_sdk_routes.{codex,droid}.{capability}` config (`legacy|sdk`, all
defaults legacy). Codex tool chat has no SDK route: the pinned SDK exposes no
way to register Gobby's tools (6.1). A daemon-owned `AgentSDKRuntime` eagerly initializes
providers with ≥1 sdk route before HTTP readiness (non-fatally), exposes
sanitized diagnostics, and is the single owner of every SDK child's lifecycle.
Both providers end in the same graceful-then-forced process-group cleanup:
directly for the droid transport it spawns itself, and for Codex through the
vendor client's bounded close followed by a group sweep, which a launcher shim
makes safe by giving the child its own session (2.2). No fallback, no shadowing: an unavailable SDK route raises
the existing `CapabilityUnavailableError`. Every legacy path is preserved, and rollback is a
config change for every capability **except droid web chat**: conversations
already pinned `sdk` keep requiring the SDK runtime after a rollback, because
the pin is immutable and there is no fallback. Section 7.3 specifies that
branch and its operator remediation.

## Constraints
`kind: framing`

- **Exclusions**: Codex web chat stays on `CodexManagedChatSession` (public
  Codex SDK lacks interactive approval callbacks); **Codex tool chat has no SDK
  route at all** and stays on `CodexAppServerClient` permanently within this
  plan (6.1 states the verified upstream reason); Qwen unchanged; agent
  spawning stays tmux/CLI. Endpoint-scoped providers (Responses/OSS
  `endpoint:*` bindings) always stay legacy.
- **Auth**: a paid ChatGPT subscription account is required for Codex SDK
  (inspect `account()`; accept only an allowlisted paid `ChatgptAccount` plan;
  reject absent, API-key, Bedrock, `free`, `unknown`, and unrecognized values;
  never invoke SDK login). That check is the enforcement point, not a
  diagnostic: the pinned SDK cannot delete an inherited `OPENAI_API_KEY` from
  its child, so "never bill an API account" is guaranteed by rejecting the
  account rather than by shaping the environment (2.2). Droid SDK paths never consult `FACTORY_API_KEY`; they OAuth-seed
  isolated state via the existing `_droid_isolated_env` /
  `_seed_droid_factory_state` helpers.
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
- **What "no fallback" scopes to, precisely.** It forbids **same-provider
  backend substitution**: a request that resolved to `codex`/`sdk` must never
  be served by the legacy codex transport, and the routing wrappers (4.3, 6.3)
  are what guarantee that. It does **not** redefine what an unavailable
  provider means to the layer above. `ToolChatService.chat_result`
  (`src/gobby/ai/_tool_chat_service.py:87-152`) and its text-generation
  counterpart already catch `CapabilityUnavailableError`, record it, and try
  the next configured candidate — that is how *every* unavailable provider has
  always behaved, including a codex with no CLI installed. A routed-SDK
  provider that is unready surfaces as unavailable and participates in that
  pre-existing candidate loop unchanged. Making SDK unavailability terminal in
  those loops was considered and rejected: it would change dispatch semantics
  for legacy providers this plan promises to leave byte-identical, and it would
  fail a whole request that the operator explicitly configured a second
  candidate to serve. A candidate list cannot smuggle a same-provider fallback
  in through this door, because a provider resolves to exactly one binding per
  `(provider, model)` and the route decision lives inside that binding's
  wrapper.
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

Target: `pyproject.toml`, `uv.lock`, `tests/ai/agent_sdk/test_sdk_surface.py`

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

- Codex: `AsyncCodex` construction kwargs, `account()`, `thread_start` and its
  thread options (`sandbox`, `approval_mode`, `cwd`, `ephemeral`, `model`),
  `Thread.run`/`Thread.turn` and their `output_schema` parameter,
  `LocalImageInput`, usage-bearing turn-completed event. **Assert the absence
  of a tool surface too**: `thread_start`, `run`, and `turn` take no tool,
  handler, or registration parameter, and `ToolsV2` carries only `web_search`.
  A future release that adds one is the trigger to reopen 6.1 — and the test
  failing is how that becomes visible, instead of the plan silently continuing
  to assume the wrong answer in either direction.
- Droid: custom-transport protocol methods, client `initialize()`, image input
  type, `ToolUse`/`ToolResult`, permission-request and completion event classes.

A range bump that drops or renames any of these fails at P1, before adapter
work begins.

**The distribution name is not the import name.** The `openai-codex`
distribution installs the `openai_codex` package (`AsyncCodex` is re-exported
from `openai_codex.api` through `openai_codex.__init__`). `import codex`
resolves to nothing in this distribution, so every import in P2-P7 and in the
surface test reads `from openai_codex import ...`.

**Pin the launch properties 2.2 depends on.** `CodexConfig` exposes
`codex_bin`, `launch_args_override`, and `config_overrides` but no
environment-replacement or process-session control, and the client builds its
child environment by overlaying `CodexConfig.env` onto a copy of `os.environ`.
2.2's auth *and* process-ownership contracts are written against exactly that
behavior. Assert all of it here — the three fields, the overlay-not-replace env
semantics, the absence of any `start_new_session`/process-group control, and
the exact argv `launch_args_override` replaces
(`[codex_bin, *("--config", kv)..., "app-server", "--listen", "stdio://"]`) —
so a patch bump that changes any of it fails at P1 rather than silently
invalidating 2.2's reasoning.

**Also pin the auth response shape 2.2 classifies against.**
`GetAccountResponse.account` is `Account | None` where `Account` is a
`RootModel` union of `ApiKeyAccount | ChatgptAccount | AmazonBedrockAccount`,
and `ChatgptAccount.plan_type` is a `PlanType` enum whose members include
`free` and `unknown`. 2.2's subscription allowlist is written against that
exact union and that exact enum, so the surface test asserts both — a renamed
variant or a new plan value must fail here, where it is one line, rather than
inside a fail-open auth check.

**Acceptance:**

- 1.1.1 - Both SDKs declared with the pinned ranges, `uv lock` resolves
  cleanly, and the resulting `uv.lock` change is committed with the manifest.
  file: `pyproject.toml`. file: `uv.lock`.
- 1.1.2 - `import openai_codex` (exposing `AsyncCodex`) and `import droid_sdk`
  succeed in the project venv; embeddings/local tests pass against the
  resolved `openai`. test: `tests/ai/test_endpoints.py`.
- 1.1.3 - The offline surface test asserts every SDK symbol, method parameter,
  and event class consumed by P2-P7, runs without credentials or child
  processes, and fails on removal or rename. file:
  `tests/ai/agent_sdk/test_sdk_surface.py`.
- 1.1.4 - The surface test asserts `CodexConfig` carries `codex_bin`,
  `launch_args_override`, and `config_overrides`; that the client's child
  environment is an overlay onto inherited environment rather than a
  replacement; that no process-session or process-group control is exposed; and
  the exact argv shape `launch_args_override` replaces — so a bump that changes
  any of it fails here instead of inside 2.2. test:
  `tests/ai/agent_sdk/test_sdk_surface.py`.
- 1.1.5 - The surface test asserts the auth response shape 2.2 classifies
  against: `GetAccountResponse.account` is optional, `Account` unions exactly
  `ApiKeyAccount | ChatgptAccount | AmazonBedrockAccount`, and `PlanType`
  contains at least `free` and `unknown` alongside the paid tiers 2.2
  allowlists. A renamed variant or an unrecognized new plan value fails here.
  test: `tests/ai/agent_sdk/test_sdk_surface.py`.
- 1.1.6 - The surface test asserts the *absence* of a Codex dynamic-tool
  surface: `thread_start`, `Thread.run`, and `Thread.turn` expose no tool,
  handler, or registration parameter, and `ToolsV2` exposes only `web_search`.
  This is the tripwire for 6.1's non-goal. test:
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

class CodexAgentSdkRoutesConfig(BaseModel):   # text_generate, vision_extract
class DroidAgentSdkRoutesConfig(BaseModel):   # + tool_chat, web_chat
class AgentSdkRoutesConfig(BaseModel):        # codex, droid
    def sdk_configured_providers(self) -> tuple[str, ...]: ...
    @property
    def any_sdk(self) -> bool: ...
```

All route fields default `AgentSdkRoute.LEGACY`. Two separate per-provider
models (not inheritance), so the two capabilities codex has no SDK backend for
— `web_chat` (no interactive approval callbacks) and `tool_chat` (no
dynamic-tool surface at all, 6.1) — are rejected outright rather than silently
accepted and ignored. **Six** leaves total: codex `{text_generate,
vision_extract}`, droid `{text_generate, vision_extract, tool_chat,
web_chat}`. Attach
`agent_sdk_routes: AgentSdkRoutesConfig = Field(default_factory=...)` to
`AIConfig` (currently `generation`-only, lines 199-207).

Add the audit row for `ai.agent_sdk_routes` to
`docs/audits/configuration-audit.md` and `'ai.agent_sdk_routes'` to
`OWNED_PATHS` in `ProvidersModelsSection.tsx` (ancestor path covers all six
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

- 1.2.1 - `AgentSdkRoutesConfig` exists with all-legacy defaults over exactly
  six leaves, and `extra="forbid"` rejects unknown keys — asserted for both
  `codex.web_chat` and `codex.tool_chat`, the two capabilities codex has no SDK
  backend for. symbol: `AgentSdkRoutesConfig`. file: `src/gobby/config/ai.py`.
- 1.2.2 - `DaemonConfig` round-trips `ai.agent_sdk_routes` through dump/load
  and invalid route values fail validation. test:
  `tests/config/test_app_config.py`.
- 1.2.3 - Audit row present and the settings section owns the new path with
  the frontend coverage test green. file: `docs/audits/configuration-audit.md`.
- 1.2.4 - `ProvidersModelsSection` exposes the six route selects, and offers no
  control for `codex.tool_chat` or `codex.web_chat`. file:
  `web/src/components/settings/sections/ProvidersModelsSection.tsx`.
- 1.2.5 - An AST-walking test fails when any module under `src/gobby/config/`
  imports `gobby.ai`, including under `TYPE_CHECKING`. test:
  `tests/config/test_import_direction.py`.

### 1.3 Sessions web_chat_backend migration [category: code]
`kind: deliverable`

Target: `src/gobby/storage/migrations/343_session_web_chat_backend.sql`,
`src/gobby/storage/postgres_baseline_schema.sql`,
`tests/storage/test_migration_contract.py`,
`tests/storage/test_migration_runner.py`

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

**A string-comparison test cannot support a claim about data.** The contract
test above compares SQL text in two files; it never runs migration 343 and
never sees a row. But 1.3.1 asserts something about *upgraded* data — that
pre-existing sessions end up `NULL` rather than defaulted — and the whole
three-state design in 7.3 rests on that being true. Add an **executable**
upgrade test alongside it, following the precedent already in the repo
(`tests/storage/test_migration_runner.py`,
`test_memory_source_session_upgrade_preserves_memory` at `:467`): create a
scratch schema, build a minimal pre-migration `sessions` table, insert a row,
apply `343_session_web_chat_backend.sql`, then assert the existing row's
`web_chat_backend` is `NULL`, that `'legacy'` and `'sdk'` are both accepted,
and that the CHECK rejects any other value. Three assertions on real
PostgreSQL, replacing an inference drawn from a diff.

**Acceptance:**

- 1.3.1 - Migration 343 adds the nullable column plus the
  `IS NULL OR IN ('legacy','sdk')` CHECK. file:
  `src/gobby/storage/migrations/343_session_web_chat_backend.sql`.
- 1.3.4 - The migration is **executed** against real PostgreSQL in a scratch
  schema over a pre-existing row: that row's `web_chat_backend` is `NULL`
  afterward, `'legacy'` and `'sdk'` are accepted, and any other value is
  rejected by the CHECK. test:
  `tests/storage/test_migration_runner.py::test_session_web_chat_backend_upgrade`.
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
`src/gobby/storage/sessions/_upsert.py`,
`tests/storage/test_sessions_import.py`

Follow the `sandbox_policy_hash` precedent trail (also a nullable TEXT column):

- `Session` dataclass: `web_chat_backend: str | None = None`; `from_row` guard
  (`row["web_chat_backend"] if "web_chat_backend" in row.keys() else None`);
  include in `to_dict`.
- `register` (Protocol + mixin): unchanged signature — new rows are written
  unpinned (`NULL`). No call site passes a backend.
- `create_web_chat_session`: unchanged — pre-created rows are also unpinned.
- `_bulk_update.update`: `web_chat_backend: str | None | UnsetType = UNSET`,
  following the sentinel idiom this same function already uses for
  `transcript_path`, `title`, `title_source`, and `git_branch`
  (`_bulk_update.py:73-91`). The sentinel is load-bearing, not stylistic: every
  plain `str | None = None` parameter in that signature treats `None` as *no
  change*, so without `UNSET` there is no way to express "clear the pin" at
  all — and a clear is the only way out of the stranded state 7.3 describes.
  `UNSET` ⇒ leave the column untouched; an explicit `None` ⇒ write SQL `NULL`
  (unpin); `'legacy'`/`'sdk'` ⇒ set; anything else rejected. This is a storage
  capability for admin and test tooling only — no CLI command and no HTTP route
  exposes it, and 8.2 must not describe it as an operator workflow.
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

**Both changes here are pinned by an existing exact-signature test.**
`tests/storage/test_sessions_import.py` holds
`EXPECTED_PUBLIC_METHOD_SIGNATURES` and asserts in
`test_session_manager_public_method_signatures_are_stable` that
`SessionManager`'s public method set and each literal signature match it
exactly — its `_normalized_signature` helper even renders `UNSET` defaults, so
the new `web_chat_backend: str | None | UnsetType = UNSET` parameter on
`update` is compared verbatim. Adding that parameter and adding
`pin_web_chat_backend` therefore both fail this test until the expected mapping
is updated, and updating it is a deliberate step of this section rather than
incidental fallout discovered during implementation.

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
- 1.4.5 - `_bulk_update.update` distinguishes all three states: omitting
  `web_chat_backend` leaves a pinned value untouched, passing an explicit `None`
  writes SQL `NULL` so the next connect re-resolves, and an invalid value is
  rejected. file: `src/gobby/storage/sessions/_bulk_update.py`. test:
  `tests/storage/sessions/test_registration.py`.
- 1.4.6 - `EXPECTED_PUBLIC_METHOD_SIGNATURES` is updated for `update`'s new
  `UNSET`-defaulted parameter and for the added `pin_web_chat_backend`, and
  `test_session_manager_public_method_signatures_are_stable` passes. file:
  `tests/storage/test_sessions_import.py`.

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

Target: `src/gobby/ai/agent_sdk/codex_client.py` (new),
`src/gobby/ai/agent_sdk/codex_launcher.py` (new)

`CodexSdkClient` owning one shared AsyncCodex process:

- `start()`: build `CodexConfig.env` with `GOBBY_HOOKS_DISABLED="1"` and
  `OPENAI_API_KEY=""`; set `launch_args_override` to the process-group shim
  below; start AsyncCodex; then verify auth by inspecting `account()` against
  the paid-tier allowlist below. Any rejection raises a fixed-message auth
  error (no detail leak) and the client is torn down. Never invoke SDK login
  methods.
- `thread()` async context manager yielding an ephemeral thread for one-shot
  use; concurrent threads supported on the single shared process.
- `close()`: `await AsyncCodex.close()` under a bounded timeout, executed off
  the event loop because the underlying implementation is blocking, then the
  bounded process-group sweep below.
- Coexists with `runner.codex_client` (`CodexAppServerClient`) and the
  per-endpoint clients — none of those change.

**"Not an API key" is not "a subscription" — classify the whole union.**
`GetAccountResponse.account` is `Account | None`, and `Account` is a union of
`ApiKeyAccount | ChatgptAccount | AmazonBedrockAccount`. A `ChatgptAccount`
additionally carries `plan_type: PlanType`, whose members include `free` and
`unknown`. Rejecting only `ApiKeyAccount` therefore accepts three distinct
non-subscription outcomes: an absent account, a Bedrock account, and a ChatGPT
account on `free` or `unknown`. The check must be an **allowlist over paid
ChatGPT tiers**, not a denylist over API keys:

```python
_SUBSCRIPTION_PLANS = frozenset({
    PlanType.go, PlanType.plus, PlanType.pro, PlanType.prolite,
    PlanType.team, PlanType.self_serve_business_usage_based,
    PlanType.business, PlanType.enterprise_cbp_usage_based,
    PlanType.enterprise, PlanType.edu,
})
```

Accept iff the account is present, its variant is `ChatgptAccount`, and its
`plan_type` is in that set. Everything else — absent, `ApiKeyAccount`,
`AmazonBedrockAccount`, `free`, `unknown`, and any value a future SDK release
adds — is rejected through the same fixed sanitized message. An allowlist is
required rather than a denylist precisely so a new enum member fails closed;
1.1.5 makes the enum drift visible at P1 instead of at runtime. The account
email and the rejected plan value never appear in the error, the log, or the
provider status.

**The Codex child gets its own process group, via a launcher shim.** The pinned
client calls `subprocess.Popen` without `start_new_session`, so by default the
Codex child lives in the **daemon's own process group** and a group signal
would terminate the daemon. That constraint is real, but the conclusion drawn
from it in the previous round — leave the child in the daemon's group and rely
solely on the vendor `close()` — is wrong, because the child is not
childless. `codex app-server` spawns processes of its own: MCP servers declared
in the user's `~/.codex/config.toml`, and sandboxed command executions. Note
that `ApprovalMode.deny_all` does **not** prevent the latter: it maps to
`AskForApproval(never)`, which auto-denies *escalations* while commands
permitted by the read-only sandbox still execute as real child processes.
Terminating only the app-server PID orphans those descendants, and a daemon
that leaks a process tree per rollback is not an acceptable outcome.

`CodexConfig.launch_args_override` replaces the client's argv wholesale, which
is the seam. Point it at a tiny Gobby-owned launcher
(`src/gobby/ai/agent_sdk/codex_launcher.py`, invoked as
`[sys.executable, "-m", "gobby.ai.agent_sdk.codex_launcher", <pidfile>,
<codex_bin>, *("--config", kv)..., "app-server", "--listen", "stdio://"]`) that
calls `os.setsid()`, writes its own PID to the pidfile, and `os.execv`s the
real binary. `setsid()` succeeds because a freshly forked `Popen` child is
never already a group leader; after `execv` the PID is unchanged, so the
recorded PID **is** the new PGID.

The shim exists for exactly one reason — to make group cleanup safe — and it
buys three things the vendor close cannot: the group is disjoint from the
daemon's, so `killpg` is safe; descendants are inside it; and the PGID is
known without reaching into the client's private `_proc`. Reconstructing the
argv is the cost, which is why 1.1.4 pins its exact shape.

Cleanup order is therefore: vendor `close()` first (it owns the stdio protocol
and performs close-stdin → `terminate()` → `wait(timeout=2)` → `kill()` against
the app-server PID), **then** `killpg(pgid, SIGTERM)` → bounded wait →
`killpg(pgid, SIGKILL)` for anything the app-server left behind, then remove
the pidfile. This is now the same graceful-then-forced group discipline 2.1
applies to the droid transport, rather than a per-provider exception.

**`account()` is the enforcement; the environment is only defense in depth.**
The pinned client builds its child environment as `os.environ.copy()` overlaid
with `CodexConfig.env`. An overlay can overwrite a key but cannot delete one,
so an ambient `OPENAI_API_KEY` in the daemon's environment cannot be removed
from the child through the public surface. The invariant that actually matters
is not "the variable is absent" but **"Gobby never issues a Codex SDK request
under API-key auth"**, and `account()` enforces that directly and fail-closed:
if the account is API-key-backed or absent, `start()` fails, the client is torn
down, and the provider is unavailable — the same outcome an absent credential
produces. Setting the key to the empty string is retained as best-effort
narrowing and must not be described anywhere as deletion. If a later round
demonstrates that an empty value is itself load-bearing, `CodexConfig.codex_bin`
plus a Gobby-owned exec shim is the escape hatch; it is deliberately not built
now, because it adds an installed artifact to harden a path `account()` already
closes.

**Never signal the daemon's own group.** The safety property the shim buys is
only real if it is asserted: cleanup must signal the recorded PGID and must
refuse to signal when that PGID equals the daemon's own
(`os.getpgrp()`) — the state that would exist if the shim silently failed to
`setsid`. In that case, fall back to the vendor close alone and record
`leaked`. A wrong group kill takes the daemon down with it, so the guard is not
optional and belongs at the signalling site, not in a comment.

**Acceptance:**

- 2.2.1 - Child env sets `GOBBY_HOOKS_DISABLED=1` and an empty
  `OPENAI_API_KEY`, and no code path claims or asserts that the variable is
  absent from the child. symbol: `CodexSdkClient`. file:
  `src/gobby/ai/agent_sdk/codex_client.py`.
- 2.2.2 - Account classification is table-driven and fail-closed over the whole
  pinned union: an absent account, an `ApiKeyAccount`, an
  `AmazonBedrockAccount`, a `ChatgptAccount` on `free`, one on `unknown`, and
  one carrying a plan value not in the allowlist are each rejected with the
  same fixed sanitized message, while each allowlisted paid tier is accepted.
  The message, the logs, and the provider status contain no account email and
  no rejected plan value, and no SDK login method is ever invoked. test:
  `tests/ai/agent_sdk/test_codex_client.py`.
- 2.2.3 - Concurrent ephemeral threads run on one shared process; `close()`
  is idempotent. test: `tests/ai/agent_sdk/test_codex_client.py`.
- 2.2.4 - An `account()` rejection tears down the client and leaves no
  surviving child, and `start()` reports the provider unavailable rather than
  returning a usable client. test: `tests/ai/agent_sdk/test_codex_client.py`.
- 2.2.5 - Cleanup runs the vendor close under a bounded off-loop call and then
  sweeps the recorded process group: a descendant spawned by a fake app-server
  is gone after `close()`, the escalation is SIGTERM → bounded wait → SIGKILL,
  and the sweep is skipped with `cleanup_outcome` `leaked` when the recorded
  PGID equals `os.getpgrp()`. behavior: "Codex process-group ownership" in
  `src/gobby/ai/agent_sdk/codex_client.py`. test:
  `tests/ai/agent_sdk/test_codex_client.py`.
- 2.2.6 - The launcher shim puts the child in its own session: the recorded
  PID equals the child's PGID, differs from the daemon's `os.getpgrp()`, and
  the exec'd argv is exactly the app-server argv the vendor client would have
  built. symbol: `codex_launcher`. file:
  `src/gobby/ai/agent_sdk/codex_launcher.py`. test:
  `tests/ai/agent_sdk/test_codex_client.py`.

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

  **The backoff is a terminal transition, so it must settle the queue.**
  Callers that already passed the readiness check and are blocked inside pool
  acquisition are invisible to a status flag: flipping the provider unavailable
  stops *new* admissions but leaves every existing waiter parked on a client
  that will now never be created. A droid text or tool-chat request would hang
  until its own request timeout rather than failing with the reason. Tripping
  the backoff therefore does three things atomically — close admission, fail
  every queued `acquire()` with `AgentSdkUnavailableError`, and restore the
  `active`/`queued` gauges to a state consistent with an empty queue — before
  the status flip is published. Reopening happens only through a
  generation-fenced recovery (2.4), never by a waiter racing back in.
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
- 2.3.4 - Tripping the backoff settles the queue: waiters already blocked in
  `acquire()` when the third failure lands each raise
  `AgentSdkUnavailableError` rather than hanging, the `queued` gauge returns to
  zero, and no waiter is admitted afterward without a generation-fenced
  recovery. test: `tests/ai/agent_sdk/test_droid_pool.py`.

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

`runtime.py` — `AgentSDKRuntime(config_getter, main_loop_getter,
shutdown_getter, max_concurrency, timeout_seconds)`:

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
  - `droid_web_chat_client()` (below) — web chat never reaches a lease either.
    This trigger only exists if backend selection can reach it, which
    constrains 7.3: selection fails fast when the runtime is **absent**, but a
    runtime that exists with an unready provider must fall through to client
    acquisition so this trigger runs. A readiness guard placed *before* the
    trigger would make the trigger dead code — the provider is unready, so
    selection rejects, so `droid_web_chat_client` is never called, so
    `ensure_provider` never runs, so the provider stays unready. Startup
    degradation would be permanent for web chat.

  Without the second and third triggers a vision-only or web_chat-only
  configuration is permanently unrecoverable after a startup failure, which is
  the failure this contract exists to prevent.

  **The flight belongs to the runtime; callers only observe it.** Single-flight
  means several unrelated callers await the same task, and with three trigger
  shapes those callers are a lease, a TTL-expired vision probe, and a web-chat
  connect — arbitrary request contexts that can be cancelled independently. A
  caller that awaits the shared `asyncio.Task` **directly** propagates its own
  cancellation into that task: one disconnected WebSocket cancels provider
  initialization for every other waiter, and each of them then observes a
  spurious `CancelledError` from work they never owned. Worse, the flight's own
  transactional cleanup then runs on a client nobody asked to tear down.

  Callers therefore await the runtime-owned task through `asyncio.shield`, so a
  caller's cancellation ends that caller's wait and nothing else. The flight is
  cancellable from exactly three places, all of which own it: the runtime's own
  `timeout_seconds` bound, generation invalidation, and `close()`. A caller
  whose shielded wait is cancelled leaves the flight running for the remaining
  waiters, and its own request fails normally.
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
- `set_degradation_sink(callback)`: the single edge by which the runtime reports
  a provider becoming healthy or unhealthy. The runtime must **not** reach into
  `runner.degraded_services` directly — it holds no runner reference, and
  `ServiceContainer` has no runner backref, so the degraded-entry mutations that
  `reconcile()` and `ensure_provider()` are required to perform are otherwise
  unimplementable through the documented object graph. 2.6 installs a callback
  that adds and discards `agent_sdk_<provider>` entries on the runner's existing
  set. One narrow callback, no second degradation registry.

**One provider lifecycle state, one per-provider generation.** `start()`,
`ensure_provider()`, `reconcile()`, and `close()` are four independent flights
over the same per-provider children, so it is their *interleavings*, not any one
of them, that decide whether the inert-state guarantee actually holds. Rather
than four ad-hoc locks, each provider carries one state
(`absent | initializing | ready | draining | closed`) and **its own** monotonic
generation, advanced when that provider's configured route set changes.

The generation is deliberately per-provider rather than one runtime-wide
counter. A shared counter is invalidated by any provider's config change, so a
droid flip would stale an in-flight codex `ensure` that nothing was wrong with —
and because the reconcile trigger reacts to *gained* and *lost* route edges, a
codex whose routes did not change gives it no edge to retry on. The provider
would be discarded and never re-initialized. Fencing on the provider's own
generation makes a flight stale only when the thing it is publishing actually
changed underneath it.

Four rules follow:

- **Admission is revoked before the drain, not during it.** Entering `draining`
  first flips the provider out of admission, so `codex_thread()`,
  `droid_lease()`, and `droid_web_chat_client()` raise
  `AgentSdkUnavailableError` immediately instead of queueing behind the drain.
  Without this, a lease or a dedicated client acquired mid-drain reopens the
  provider the rollback is in the middle of closing, and the drain completes
  against a child set that grew underneath it.
- **Publication is fenced by the provider's generation, and every child writer
  goes through the same gate.** A flight stamps the generation it began under
  and publishes its children only if that value is still current. A flight that
  finishes after a rollback or a `close()` sees a newer generation, discards its
  result, and closes what it built on the same graceful-then-forced path. This
  is what stops a late flight from resurrecting a provider the operator just
  disabled.

  The gate is only as good as its coverage, and `ensure_provider` is **not** the
  only path that creates or publishes a child. Four writers exist and all four
  enter the same commit guard: `start()`'s eager probe, `ensure_provider()`,
  the droid pool's lease-`finally` replenishment (2.3), and
  `droid_web_chat_client()`'s dedicated-client construction. Replenishment is
  the dangerous one — it fires from a `finally` block precisely while a forced
  drain is cancelling leases, so an ungated replenish recreates the child the
  drain just removed and the drain completes against a child set that grew
  behind it. The pool therefore sets its closing flag *before* cancelling
  leases, and any writer that finds the provider `draining`, `closed`, or
  generation-stale at commit time closes what it built locally instead of
  publishing it.
- **The synchronous rebind boundary does the work that cannot wait.**
  `ConfigurationRouteContext.set_runtime_config`
  (`src/gobby/servers/routes/configuration_context.py:69-81`) rebinds
  `services.config` and returns. Every live-config reader — the routing
  wrappers, the vision probe, `ensure_provider`, pool replenishment, dedicated
  web-chat client construction — observes the new routes on their very next
  read, which is *before* any scheduled reconcile pass has run. Deferring all
  of the runtime's reaction to that pass leaves a window in which a provider
  whose last sdk route was just removed is still admitting work, and a child
  created in that window is one the drain then has to chase.

  So the runtime exposes one synchronous `note_config_change()` that 2.6 calls
  from inside the rebind, before it schedules anything, and it does exactly
  three things — all cheap, none blocking, none awaiting:
  1. increment a monotonic **config epoch**;
  2. diff each provider's configured route set against the one the runtime is
     currently converged on, and increment the **generation** of each provider
     whose set changed (this is the concrete site for "advanced when that
     provider's configured route set changes");
  3. **revoke admission** for any provider that just lost its last sdk route —
     the same state transition entering `draining` performs, taken here so that
     no lease, replenishment, or dedicated client can be admitted between the
     write and the drain.

  Draining and initializing still happen in the scheduled pass. Only the parts
  that must not be observable-as-stale happen synchronously.
- **Reconcile converges on the newest write, and terminates.** Plain
  single-flight coalescing silently loses updates, and so does re-reading config
  at the end of a pass: a write that lands *after* the flight's final read but
  *before* the single-flight slot is released coalesces into a flight that is
  already exiting and gets no pass of its own.

  The config epoch above is what closes that race, and it is deliberately a
  counter rather than a dirty boolean. A boolean needs a consume point, and
  both placements are wrong: never clearing it makes every pass schedule
  another pass forever, and clearing it after a pass erases a write that landed
  *during* the pass. A counter needs no consumption. Each pass snapshots the
  epoch under the single-flight lock **before** its first config read, converges
  that snapshot, and at exit compares the snapshot against the current epoch;
  if they differ, a write happened since the pass began and it schedules exactly
  one fresh pass. Nothing is ever cleared, so nothing can be lost.

  Passes reschedule rather than loop in place. Looping in place is what a
  continuously writing config client turns into a pass that never returns —
  monopolizing the reconcile owner and blocking `close()` behind it.
  Rescheduling keeps latest-write-wins with at most one flight live, and leaves
  the shutdown gate a place to intervene: once `close()` begins, new passes are
  refused and the single in-flight pass is awaited or cancelled before children
  are drained.

**Initialization is transactional: acquire locally, validate, then commit.**
The commit guard above only protects what it can see, and provider
initialization creates real OS resources *before* there is anything to publish.
Codex starts a child and only then validates the account (2.2); droid builds
transport children, stderr drains, and isolated temp homes before the pool is
publishable. Every non-commit exit therefore has to dispose of what the flight
built: an auth rejection, a probe that never completes and hits the 30s bound, a
cancellation, or a commit refused as generation-stale. A leaked child from any
of these is invisible to `close()` precisely because it was never published,
which is the one class of leak the shutdown budget cannot catch.

The flight holds its new children and drains in a local set until the commit
guard accepts them. On any exit that is not a commit it runs the same bounded
graceful-then-forced cleanup plus temp-home removal and only then records the
sanitized failure. `asyncio.CancelledError` is a `BaseException` and must be
handled explicitly — an `except Exception` cleanup path silently skips exactly
the cancellation cases (shutdown, timeout) most likely to strand a child.

**`await asyncio.shield(cleanup())` is not sufficient, and getting this wrong
looks correct.** Shield protects the *inner* operation from cancellation; it
does not protect the awaiting frame. When the outer task is cancelled, the
`await` raises `CancelledError` in the flight immediately while the cleanup
coroutine keeps running in a task nobody holds. The flight then unwinds with
its children half-disposed and unpublished — invisible to `close()`, which is
the exact leak class this rule exists to prevent, reintroduced by the mechanism
meant to prevent it.

The cleanup must be **owned**, not merely shielded:

- Start it as a task the flight holds a reference to.
- Await it under one **absolute deadline** (the runtime's `timeout_seconds`,
  computed once so repeated cancellation cannot extend it), re-awaiting the
  same task rather than restarting cleanup if the frame is cancelled again.
- Consume its result — retrieving any exception so it is logged sanitized
  rather than surfacing as "exception was never retrieved".
- Record `cleanup_outcome` `closed` or `killed` from the settled task; record
  `leaked` **only** when the deadline expires with the task unfinished.
- Only then re-raise the deferred `CancelledError`, so cancellation semantics
  are preserved for the caller without being preserved at the cost of the
  child.

**Scheduled work is owned, not orphaned.** The two non-lease triggers
(`set_runtime_config` in 2.6, the vision probe in 5.2) are synchronous callers
that cannot await, so they schedule rather than call. Scheduling needs four
things, and the runtime owns them once instead of each caller reinventing them:

- **A loop that outlives the caller, reached through a getter.**
  `ServiceContainer.main_loop` already exists for exactly this case —
  "fire-and-forget work spawned from short-lived loops must be scheduled here to
  survive the caller" (`app_context.py`). It matters here because
  `set_runtime_config` can run on a worker thread with no running loop, where a
  bare `asyncio.create_task` raises immediately.

  The runtime cannot capture the loop, and cannot be handed it at construction:
  `runner_lifecycle.py:153-157` assigns `runner.main_loop` and
  `http_services.main_loop` from inside the running loop in `run_daemon`, which
  is strictly *after* Phase 4 builds the runtime. The constructor therefore
  takes `main_loop_getter` and `shutdown_getter` alongside the existing
  `config_getter`, wired at the same 2.5 site to `lambda: services.main_loop`
  and `lambda: services.shutdown_in_progress`. Reading through getters is also
  what keeps the shutdown gate honest — `shutdown_in_progress` is a mutable flag
  on the container, and a boolean copied at construction would be `False`
  forever. When `main_loop_getter()` returns `None`, skip rather than raise.
- **Strong references.** Tasks are retained in one runtime-owned set and
  discarded in a done-callback. A bare `create_task` handle can be garbage
  collected mid-flight.
- **An error sink.** The done-callback retrieves the exception and logs
  `sanitize_sdk_error(exc)`. An unretrieved exception on a dropped task surfaces
  as an "exception was never retrieved" warning carrying the raw provider
  message — noise and a credential leak in one.
- **A shutdown gate.** Scheduling is refused once `close()` has begun or
  `shutdown_getter()` reports shutdown, and `close()` settles the
  outstanding set *before* draining children — otherwise a late `ensure` spawns
  a child after the drain has already counted them, and it leaks.
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
- 2.4.8 - Recovery is reachable for a capability that never takes a lease:
  starting from an unready provider, `droid_web_chat_client` triggers
  `ensure_provider` and joins the same single flight as the lease paths. The
  vision half of this guarantee is asserted by 5.2.5, which owns the probe —
  the probe does not exist at this section's completion. test:
  `tests/ai/agent_sdk/test_runtime.py`.
- 2.4.10 - The lifecycle state and generation fence hold under all three races:
  a lease or `droid_web_chat_client` acquisition attempted against a `draining`
  provider raises instead of queueing; an `ensure_provider` flight that
  completes after a rollback discards and closes its children rather than
  publishing them; and a config write landing while a `reconcile()` is draining
  or initializing still converges on the newest route set. behavior: "provider
  lifecycle state" in `src/gobby/ai/agent_sdk/runtime.py`. test:
  `tests/ai/agent_sdk/test_runtime.py`.
- 2.4.11 - Scheduled reconcile/ensure work is owned: it runs on the loop
  returned by `main_loop_getter` when scheduled from a thread with no running
  loop, is retained until completion, reports failures through
  `sanitize_sdk_error` with no raw provider text and no "never retrieved"
  warning, is refused once `shutdown_getter()` reports shutdown, and is settled
  by `close()` before children are drained so no child outlives the drain.
  behavior: "scheduled work ownership" in
  `src/gobby/ai/agent_sdk/runtime.py`. test:
  `tests/ai/agent_sdk/test_runtime.py`.
- 2.4.12 - The runtime mutates degraded state only through
  `set_degradation_sink`, asserted at the runtime boundary alone: with no sink
  installed, recovery and reconciliation still succeed and touch nothing; with
  an injected fake sink, a recovered provider produces a discard call and a
  drained provider produces a clear call. The equivalent assertion against the
  real `runner.degraded_services` set belongs to 2.6.5, because 2.6 installs
  that sink and depends on this section — requiring it here would make this
  section uncompletable under its own dependency order. symbol:
  `set_degradation_sink`. test: `tests/ai/agent_sdk/test_runtime.py`.
- 2.4.13 - Latest-write-wins survives the flight-teardown race: a config write
  landing after a reconcile pass takes its final read but before its
  single-flight slot is released still produces a further pass, because the
  pass compares its entry-time epoch snapshot against the current epoch at
  exit and nothing is ever cleared. A provider whose routes did not change is
  never staled by an unrelated provider's write. Continuous writes do not
  prevent a pass from returning, and `close()` during an in-flight pass settles
  it before draining. behavior: "reconcile convergence" in
  `src/gobby/ai/agent_sdk/runtime.py`. test:
  `tests/ai/agent_sdk/test_runtime.py`.
- 2.4.16 - `note_config_change()` is synchronous and complete before the config
  write returns: removing a provider's last sdk route revokes its admission at
  that instant, so a lease, a pool replenishment, and a
  `droid_web_chat_client()` attempted after the rebind but before any reconcile
  pass runs each raise `AgentSdkUnavailableError` and create no child; the
  changed provider's generation advances and an unchanged provider's does not;
  and the call neither blocks nor awaits. symbol: `note_config_change`. test:
  `tests/ai/agent_sdk/test_runtime.py`.
- 2.4.17 - A cancelled initialization flight disposes its children before
  unwinding: cancelling the caller during provider init leaves zero surviving
  processes, drains, and temp homes; a second cancellation during cleanup does
  not restart or abandon it; cleanup exceeding the absolute deadline records
  `leaked` and every earlier outcome records `closed`/`killed`; and the
  `CancelledError` is re-raised to the caller afterward with no "never
  retrieved" warning. behavior: "owned cleanup" in
  `src/gobby/ai/agent_sdk/runtime.py`. test:
  `tests/ai/agent_sdk/test_runtime.py`.
- 2.4.18 - Caller cancellation is isolated from the shared flight: with two
  waiters on one `ensure_provider`, cancelling the first leaves the flight
  running and the second still observes a successful initialization; the
  cancelled caller's request fails on its own. Only runtime timeout, generation
  invalidation, and `close()` cancel the flight itself. test:
  `tests/ai/agent_sdk/test_runtime.py`.
- 2.4.14 - Every child writer is fenced: pool replenishment racing a forced
  drain, and a `start()` or `droid_web_chat_client()` construction that commits
  against a `draining`/`closed`/stale provider, each close the child they built
  locally and publish nothing, leaving zero surviving processes. test:
  `tests/ai/agent_sdk/test_runtime.py`. test:
  `tests/ai/agent_sdk/test_droid_pool.py`.
- 2.4.15 - Initialization is transactional: an `account()` rejection, a probe
  that never completes and hits the timeout bound, and a cancellation each
  leave zero surviving child processes, stderr drains, and temp homes, with the
  failure recorded sanitized. behavior: "transactional provider
  initialization" in `src/gobby/ai/agent_sdk/runtime.py`. test:
  `tests/ai/agent_sdk/test_runtime.py`.
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

**Construct inside `_init_llm_service`, but in its own `try`.** The runtime
must exist by the time anything builds a text-generation or tool-chat service,
and `_init_llm_service` (`src/gobby/runner_init/services.py:56-73`) is where
those builders run, during Phase 2. `init_services` has no exception handler of
its own — the only catch is the `except Exception: mark_service_degraded(runner,
"llm_service")` **inside** `_init_llm_service` — so constructing the runtime
before that call would let a constructor failure abort daemon initialization.

But putting the construction inside that same `try` is worse, and an earlier
round of this plan specified exactly that. `_init_llm_service` builds
`text_generation_service`, `llm_service`, and `tool_chat_service` in one `try`
whose handler marks `llm_service` degraded. A runtime constructor placed first
inside it therefore has two failure consequences it must not have: it aborts the
remaining three constructions, leaving the daemon with **no LLM services at
all** because an optional SDK runtime failed to build; and it publishes a
degraded signal attributable to `llm_service`. Under all-legacy config the
second one also violates the inert-state guarantee this plan repeats everywhere
else — an absent-by-configuration feature must produce zero degraded signals
(2.6.6).

Construct it in its own nested `try`/`except` as the first statement of
`_init_llm_service`, before the existing one:

- On success, assign `runner.agent_sdk_runtime`.
- On failure, log the sanitized error, leave `runner.agent_sdk_runtime` as
  `None`, and **continue** into the existing `try` so all three legacy services
  are built normally.
- Do **not** call `mark_service_degraded` here. The runtime's absence is
  surfaced where it is actually observable: `start_agent_sdk_runtime` (2.6)
  reports it per provider when routes are configured `sdk`, and the routing
  wrappers fail sdk-routed requests closed (4.3, 6.3). A daemon whose config is
  all-legacy is not degraded by this failure, because nothing was asked of it.

The constructor is synchronous and spawns nothing, so it is safe this early and
stays inert under all-legacy config.

**The config getter must dereference `ServiceContainer.config`, not
`runner.config`.** `set_runtime_config` rebinds `self.server.services.config`
and nothing else (Constraints). `runner.config` is written during Phase 2 and
never updated, so a getter closed over it is stale from the first config write
— which is exactly the bug the getter exists to prevent. The container does not
exist yet at Phase 2 (`runner_init/servers.py:33-60`, Phase 4), so the binding
is completed in two steps:

- Phase 2, in `_init_llm_service`: construct with `config_getter=lambda:
  runner.config`. Correct at this point — no config write can have happened yet.
  `main_loop_getter` and `shutdown_getter` default to inert (`None` / `False`);
  nothing can be scheduled before Phase 4 anyway.
- Phase 4, in `init_servers`, immediately after the `ServiceContainer` is
  built: one guarded `runner.agent_sdk_runtime.bind_service_context(...)` call
  binding all three getters —
  `config_getter=lambda: services.config or runner.config`,
  `main_loop_getter=lambda: services.main_loop`, and
  `shutdown_getter=lambda: services.shutdown_in_progress`. From here the config
  getter dereferences the exact attribute `set_runtime_config` rebinds; the
  `or runner.config` arm covers `ServiceContainer.config` being
  `DaemonConfig | None`.

  **The loop and shutdown getters cannot be supplied any earlier, and cannot be
  values.** `run_daemon` assigns `runner.main_loop` and
  `http_services.main_loop` from inside the running loop
  (`runner_lifecycle.py:153-157`) — strictly after this phase — so a loop passed
  at construction would always be `None`, and 2.4's scheduling rule would never
  fire. `shutdown_in_progress` is a mutable flag on the same container, so a
  boolean read once here would report `False` for the daemon's whole life and
  the shutdown gate would never close. Both must be getters resolved at use
  time, and this is the first site where the container they resolve against
  exists.

  **This rebind must be guarded, because the runtime is legitimately absent.**
  The degradation boundary above exists precisely so a constructor failure
  leaves `runner.agent_sdk_runtime` as `None` and the daemon continues. An
  unconditional `bind_service_context` in Phase 4 would then dereference `None`
  and abort `init_servers` — converting the degraded-but-running outcome this
  section is built to produce back into the aborted startup it is built to
  prevent. Skip the rebind when the runtime is absent; every later consumer
  already has to tolerate `agent_sdk_runtime=None` for the all-legacy case, so
  no other site needs a new branch.

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

- 2.5.1 - The runtime is constructed in its own `try` at the top of
  `_init_llm_service`, ahead of the existing service-construction `try`, is
  visible on both `GobbyRunner` and `ServiceContainer`, and spawns zero child
  processes at construction. file: `src/gobby/runner_init/services.py`.
- 2.5.2 - Construction-failure rollback closes the runtime. file:
  `src/gobby/runner_rollback.py`.
- 2.5.3 - A runtime constructor failure is contained: it never propagates out
  of `init_services`, `agent_sdk_runtime` is left `None`, and
  `text_generation_service`, `llm_service`, and `tool_chat_service` are all
  still constructed. Under all-legacy config that path marks nothing degraded —
  in particular `llm_service` is not degraded — so the inert-state guarantee
  holds. file: `src/gobby/runner_init/services.py`. test:
  `tests/runner_init/test_services.py`.
- 2.5.4 - After `init_servers`, the runtime's config getter observes a
  `set_runtime_config` rebind: a route flip delivered that way changes the value
  the getter returns, while a getter rooted at `runner.config` would not. file:
  `src/gobby/runner_init/servers.py`. test:
  `tests/runner_init/test_servers.py`.
- 2.5.5 - A Phase-2 constructor failure leaves `agent_sdk_runtime` absent and
  `init_servers` still completes: the Phase-4 getter rebind is skipped rather
  than raising, and both the `ServiceContainer` and the HTTP server are built
  normally. file: `src/gobby/runner_init/servers.py`. test:
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

- Return immediately when `runner.agent_sdk_runtime` is `None`, then
  `await runner.agent_sdk_runtime.start()`.
- Per-provider probe failure: sanitized warning +
  `mark_service_degraded(runner, "agent_sdk_codex"|"agent_sdk_droid")`;
  daemon still binds HTTP.
- `ensure_provider()` success later discards the degraded entry (mirror the
  monitor add/discard pattern).
- All-legacy: returns immediately, no degraded entries, no processes.

**The absent-runtime guard is the same guard 2.5 needs, at a second call
site.** 2.5's degradation boundary exists so that a constructor failure leaves
`runner.agent_sdk_runtime` as `None` and the daemon keeps running. An
unconditional `await runner.agent_sdk_runtime.start()` here would raise
`AttributeError` on exactly that path — before HTTP readiness, converting a
degraded-but-running daemon into a dead one, and doing so in the all-legacy
case too, where the settled contract demands zero SDK processes and zero
`agent_sdk` signals. The reconcile helper below already returns silently when
the runtime is absent; startup needs the identical branch.

**This section owns the degraded set; the runtime only reports.**
`runner.degraded_services` is runner-owned, and the runtime has no path to it
(no runner reference, and `ServiceContainer` carries no backref). So
`start_agent_sdk_runtime` installs the runtime's `set_degradation_sink` callback
(2.4) at the same point it starts the runtime — a closure over `runner` that
adds `agent_sdk_<provider>` on a failed probe and discards it on recovery or on
a reconcile that drops the provider. That single edge is what makes the
"reconciliation clears degraded entries" requirement below implementable at all;
without it the requirement names a mutation with no owner. Degradation state
stays in exactly one place.

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
It is single-flight via the runtime's own `reconcile()` (2.4), a no-op when the
route set is unchanged, and a no-op when `agent_sdk_runtime` is `None`. That is
one call at the exact point the configuration changes — not a polling loop, not
a watcher, and not a new service.

**The helper is half synchronous, and the split is the point.** Before it
schedules anything it calls the runtime's synchronous `note_config_change()`
(2.4), which bumps the config epoch, advances the generation of every provider
whose route set changed, and revokes admission for any provider that just lost
its last sdk route. Only the draining and initializing is scheduled.

The reason is that `set_runtime_config` rebinds `services.config` and returns
immediately, and every live-config reader in this plan — routing wrappers, the
vision probe, `ensure_provider`, pool replenishment, dedicated web-chat client
construction — sees the new value on its next read. If the runtime learned
about the change only when the scheduled pass ran, that whole interval would be
one in which a just-disabled provider still admits work and creates children
the drain then has to chase. `note_config_change()` cannot block, await, or
raise, so it is safe on the synchronous path that 2.6.4 protects.

**"Fire-and-forget" names the caller's obligation, not the task's.** The helper
does not call `asyncio.create_task` on whatever loop happens to be current:
`set_runtime_config` is reachable from an HTTP worker thread where no loop is
running and that call would raise, turning a config write into a 500. It hands
the coroutine to the runtime's owned scheduler (2.4), which places it on
`ServiceContainer.main_loop`, retains it, drains its exception through
`sanitize_sdk_error`, and refuses once `services.shutdown_in_progress` is set.
The helper itself stays a thin, total function: resolve the runtime, resolve the
loop, hand off, return. If either is absent it returns without raising —
`set_runtime_config` never fails because of reconciliation.

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
  reconciliation, including when `agent_sdk_runtime` is `None`, when
  `main_loop` is absent, and when it is called from a thread with no running
  event loop — and this holds with the synchronous `note_config_change()` call
  in place. file: `src/gobby/servers/routes/configuration_context.py`. test:
  `tests/servers/routes/test_configuration_routes.py`.
- 2.6.7 - Route removal takes effect at the write, not at the pass: with the
  reconcile scheduler stubbed so no pass ever runs, a `set_runtime_config` that
  removes a provider's last sdk route causes the very next lease, replenishment,
  and web-chat client acquisition to fail unavailable and spawn nothing. file:
  `src/gobby/runner_lifecycle_startup.py`. test:
  `tests/runner/test_lifecycle_startup.py`.
- 2.6.5 - `start_agent_sdk_runtime` installs the degradation sink, so a probe
  failure adds `agent_sdk_<provider>` to `runner.degraded_services` and a later
  `ensure_provider` recovery or a reconcile that drops the provider discards
  that same entry — with the runtime never referencing the runner directly.
  file: `src/gobby/runner_lifecycle_startup.py`. test:
  `tests/runner/test_lifecycle_startup.py`.
- 2.6.6 - A Phase-2 constructor failure carries through pre-readiness startup:
  `start_agent_sdk_runtime` returns without raising, HTTP readiness is still
  announced, and an all-legacy daemon on that path reports zero SDK processes
  and no `agent_sdk_<provider>` degraded entries. file:
  `src/gobby/runner_lifecycle_startup.py`. test:
  `tests/runner/test_lifecycle_startup.py`.

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
  graceful shutdown) — double-close is a no-op, and is skipped entirely when
  `agent_sdk_runtime` is `None`.

**`close()` contains its own failures; the shutdown owners must not see them.**
2.9's sanitization boundary is written around request adapters, and
`raise ... from None` there does nothing for this path. The owners here are
pre-existing generic helpers that log raw:
`runner_lifecycle_shutdown.py:91` and `:100` emit
`logger.warning("%s failed: %s", name, e, exc_info=True)` — the raw exception
string *and* a full traceback. An SDK `aclose`, a process-cleanup step, or a
stderr-drain teardown that raises with a token, a response fragment, or a home
path in its message would therefore print it, bypassing every control 2.9
establishes.

So `AgentSDKRuntime.close()` is internally non-raising for ordinary child
failures: it sanitizes each one through `sanitize_sdk_error`, records only the
fixed `cleanup_outcome` (`closed|killed|leaked`), and returns. If an aggregate
genuinely must escape, it escapes as a fixed sanitized domain exception raised
`from None`. Nothing changes in the three owners; the containment is the
runtime's job, which is also why one fix covers all three.

**Acceptance:**

- 2.7.1 - Runtime closes on FastAPI lifespan shutdown and on graceful daemon
  shutdown, double-close is safe, and an absent runtime is skipped. file:
  `src/gobby/servers/_app_lifecycle.py`.
- 2.7.2 - All SDK children are gone within the shutdown budget, by the
  ownership split 2.2 establishes: the droid transport's own process group is
  signalled, and the Codex child is settled through the vendor client's bounded
  close rather than a group signal. behavior: "cleanup outcome recording" in
  `src/gobby/ai/agent_sdk/runtime.py`.
- 2.7.3 - A child whose cleanup raises an exception carrying a token-shaped
  string, an env value, a home path, and response text produces no raw text in
  any of the three shutdown owners' logs — asserted by log capture on each —
  and the runtime reports only a fixed `cleanup_outcome`. test:
  `tests/runner/test_lifecycle_shutdown.py`. test:
  `tests/servers/test_app_lifecycle.py`.

### 2.8 Admin status diagnostics block [category: code] (depends: 2.4, 2.5)
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

**Guard the runtime, or this endpoint breaks exactly when it is needed.**
2.5 permits `services.agent_sdk_runtime` to be `None` after a constructor
failure, so an unconditional `.status_snapshot()` raises `AttributeError` on
precisely the degraded state this block exists to explain — and because the
snapshot is assembled into one payload, it can take the whole health response
down with it. `_health.py` already guards every other optional service this way
(`if runner is None`, `if tracker is None`, `if database is None`); follow that
convention and emit an empty block, which is also the correct rendering: a
runtime that does not exist has no providers to report.

**Acceptance:**

- 2.8.1 - `/api/admin/status` exposes the block with sanitized
  `startup_error` for a failed configured provider, `{}` when all-legacy, and
  `{}` — not an error — when `agent_sdk_runtime` is `None` after a Phase-2
  construction failure, with the rest of the health payload intact. file:
  `src/gobby/servers/routes/admin/_health.py`. test:
  `tests/servers/routes/admin/test_health.py`.
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
- One `agent_sdk_call` structured log per provider call, carrying exactly this
  key set (covers tool_chat/vision/web_chat):

  ```text
  provider, capability, model, configured_route, backend, ready,
  queue_wait_ms, latency_ms, usage_input_tokens, usage_output_tokens,
  success, error, cleanup_outcome
  ```

  `queue_wait_ms` is admission wait; `latency_ms` excludes it. Usage fields are
  ints. `error` is `None` on success and a `sanitize_sdk_error` string on
  failure. Optionally mirror to the existing OTel helpers
  (`inc_counter`/`observe_histogram`) at the same site.
- Never log prompts, responses, credentials, auth-file paths, home paths, env
  values, account emails, conversation or thread identifiers, or raw provider
  error strings. This exclusion list is part of the contract, not commentary:
  every section that emits the event asserts it.

**`cleanup_outcome` is not knowable per call, so it is nullable.** The event is
emitted by the adapter when the provider call returns (below), and at that
instant a concrete `closed|killed|leaked` value does not exist for two of the
three shapes. The shared Codex process is cleaned during `reconcile()` or
shutdown, never per request. `DroidSDKChatSession`'s dedicated client is
cleaned at conversation end, after an arbitrary number of calls. Only a droid
one-shot lease learns an outcome close enough to the call to report it — and
only if the adapter emits **after** the lease's context exit rather than inside
it.

So the field is typed `str | None` and carries:

- the real `closed|killed|leaked` value for a droid one-shot lease, which
  therefore emits after lease exit;
- `null` for every call on a shared or conversation-scoped client, meaning
  "this call did not own a lifecycle" — not "cleanup was skipped".

Concrete per-child cleanup outcomes remain owned by the runtime and are
reported through `provider_status()` / `status_snapshot()` and the cleanup
records `close()` and `reconcile()` write (2.4). Requiring a real value on
every call event would have forced adapters to either block on a lifecycle they
do not own or invent one, and an invented `closed` is worse than an honest
`null`.

**The emitter belongs in the adapters, because that is where the fields
exist.** The natural-looking design — the runtime emits from the lease's
`__aexit__` — cannot produce this event. `codex_thread()` and `droid_lease()`
take no capability, model, or configured-route argument, and the runtime has no
way to learn them: the model is resolved per request by the caller, and the
route was resolved at the caller's dispatch snapshot. Worse, usage and the
resolved model only exist *after* the adapter's call returns, which is after the
lease body has produced its result — the runtime would be emitting a mandatory
field set it structurally cannot fill. Giving the lease a metadata argument plus
a `record_outcome()` channel would fix that, but it is a wider API for no gain
over emitting where request and result already meet.

So each SDK adapter emits its own `agent_sdk_call` exactly once (4.1, 4.2, 5.1,
6.2), reading `queue_wait_ms` from the value the lease returns and
supplying everything else from the request it already holds and the result it
just produced. The runtime keeps ownership of `queue_wait_ms`, and of the
lease-scoped `cleanup_outcome` where one exists — the facts only it knows — and
exposes them; it does not own the event.

This also removes the special case web chat would otherwise need.
`DroidSDKChatSession` holds a *dedicated* client for the life of a conversation
(2.4) and issues a provider call per message, so a lease-scoped emitter would
have produced one event per conversation and none per message. Emitting at the
call boundary makes web chat the same rule as everything else: one event per
provider call, with `queue_wait_ms` of zero because a dedicated client has no
admission wait (7.2).

**Sanitize at the raise site, not at the log site — and suppress the cause.**
`feature_llm_call` logs `str(error)` verbatim
(`_text_generation_service.py:799`), and the tool-chat service folds
`str(exc)` into its error surfaces the same way. Neither is SDK-aware, so a raw
`AsyncCodex` or `droid-sdk` exception — which can carry a home path or auth
detail in its message — would reach structured logs untouched, defeating
`sanitize_sdk_error` (2.4). Every SDK boundary (4.1, 4.2, 5.1, 6.2, and
`DroidSDKChatSession` in 7.2) therefore wraps **every** provider exception in a
sanitized domain error before it escapes.

Wrapping alone is not sufficient. `raise Sanitized(...) from exc` keeps the raw
exception reachable as `__cause__`, and the HTTP and web-chat error paths call
`logger.exception`, which formats the entire chain — so the raw SDK message
lands in the log anyway, one frame below the sanitized one. The raise must
therefore be `raise Sanitized(...) from None`.

**State what `from None` actually does, because the test depends on it.** It
sets `__cause__` to `None` and `__suppress_context__` to `True`. It does *not*
clear `__context__`: a raise inside an `except` block always records the active
exception there, and Python keeps it. What `__suppress_context__` changes is the
traceback machinery, which stops walking the chain — so `logger.exception` emits
no raw provider text even though the original object is still attached. The
security property is therefore a property of the *formatted* output, and that is
what must be asserted. An acceptance demanding `__context__ is None` would be
unsatisfiable, and chasing it would push an implementer into pointlessly
re-raising outside the `except` block to satisfy a test rather than a threat.

Where the original is needed for diagnosis, log `sanitize_sdk_error(exc)` at the
raise site; never the exception object. Nothing downstream is modified to
compensate, and no new sanitization layer is added at the logging sites.

**One existing helper already carries a raw response preview into the logs, and
moving it does not fix that.** `_json_parse_failure`
(`_text_generation_service.py:120-127`) builds its message from
`raw[:240]` — 240 bytes of verbatim model output — and `feature_llm_call` stores
`str(error)`. Malformed provider output is exactly the case most likely to
contain something that must never be logged: an echoed prompt, a leaked
credential from the model's context, a home or auth path in an error the
provider stringified into its response. This is a pre-existing leak on the
legacy path, and 4.3 requires the helper to move byte-for-byte, which would
carry it onto the SDK path unchanged; the plan's own "never prompts, responses,
credentials, or auth-file paths" rule forbids both.

Drop the preview. The exception keeps the parse-error type, the parse-error
message, and `raw_len`, which is what a reader actually acts on — "the model
returned 12 bytes" and "the model returned 40 KB of prose" are different bugs,
and neither needs the bytes to diagnose. This is the least mechanism that
satisfies the rule: no second sanitized-vs-unsanitized exception pair, no
conditional redaction, no new field.

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
  path, an env value, and a token-shaped string into a fixed sanitized message;
  and a sanitized error raised at a boundary has `__cause__` of `None` and
  `__suppress_context__` of `True`, so the formatted traceback that
  `logger.exception` produces contains none of those three values even though
  `__context__` still holds the original. symbol: `sanitize_sdk_error`. test:
  `tests/ai/agent_sdk/test_diagnostics.py`.

  The end-to-end half of this assertion — an SDK adapter exception travelling
  all the way into `feature_llm_call` — is carried by 4.3.6 instead, because no
  SDK text adapter and no SDK-to-service path exists at 2.9's completion (2.9
  depends only on 2.4). Same deferral shape as 3.1.4 → 6.3.5.
- 2.9.2 - The runtime exposes `queue_wait_ms` to its callers, with
  `queue_wait_ms` and `latency_ms` disjoint, so an adapter can assemble the
  full `agent_sdk_call` field set without the runtime emitting it. A droid
  lease additionally exposes a concrete `closed|killed|leaked` outcome readable
  after its context exits, while shared and conversation-scoped clients expose
  none — the runtime never fabricates one to satisfy the event. test:
  `tests/ai/agent_sdk/test_runtime.py`.
- 2.9.4 - `_json_parse_failure` emits no verbatim provider output: given a
  malformed response containing a home path, an env value, a token-shaped
  string, and prompt text, neither the raised exception's message nor the
  `feature_llm_call` record contains any of them, while the parse-error type
  and `raw_len` are still present. symbol: `_json_parse_failure`. test:
  `tests/ai/test_text_generation.py`.

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

The SDK-adapter half of that assertion cannot live here: the SDK tool-chat
adapter is created in 6.2, which depends on this section, so an acceptance item
requiring it would be unsatisfiable at 3.1's completion. It is carried by 6.3
instead.

## P4: Text Generation SDK Routes
`kind: framing`

**Goal**: `agent_sdk_routes.*.text_generate=sdk` serves feature_low/mid/high
text generation through the SDKs with usage and structured output.

### 4.1 Codex SDK text adapter [category: code] (depends: 2.4, 2.9)
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
- 4.1.4 - Every AsyncCodex exception escaping this adapter is sanitized at the
  raise site per 2.9: a provider error carrying a home path, an env value, and a
  token-shaped string surfaces with none of the three, and the formatted
  traceback carries none of them either. test:
  `tests/ai/agent_sdk/test_codex_oneshot.py`.
- 4.1.5 - The adapter emits exactly one `agent_sdk_call` per request, on both
  the success and the failure path, whose keys are exactly `provider`,
  `capability`, `model`, `configured_route`, `backend`, `ready`,
  `queue_wait_ms`, `latency_ms`, `usage_input_tokens`, `usage_output_tokens`,
  `success`, `error`, `cleanup_outcome` — no more and no fewer. `model` is the
  resolved model, `queue_wait_ms` comes from the lease, the usage fields are
  ints, `error` is `None` on success and a `sanitize_sdk_error` string on
  failure, and `cleanup_outcome` is `None` because a shared Codex process has
  no per-call lifecycle (2.9). The record contains no prompt text, response
  text, credential, auth path, home path, env value, account email, thread
  identifier, or raw provider error string. This item is the schema every other
  SDK emitter is asserted against. test:
  `tests/ai/agent_sdk/test_codex_oneshot.py`.

### 4.2 Droid SDK text adapter [category: code] (depends: 2.4, 2.9)
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
- 4.2.3 - Every droid-sdk exception escaping this adapter is sanitized at the
  raise site per 2.9: a provider error carrying a home path, an env value, and a
  token-shaped string surfaces with none of the three, and the formatted
  traceback carries none of them either. test:
  `tests/ai/agent_sdk/test_droid_oneshot.py`.
- 4.2.4 - The adapter emits exactly one `agent_sdk_call` per request, on both
  the success and the failure path, whose keys are exactly `provider`,
  `capability`, `model`, `configured_route`, `backend`, `ready`,
  `queue_wait_ms`, `latency_ms`, `usage_input_tokens`, `usage_output_tokens`,
  `success`, `error`, `cleanup_outcome` — no more and no fewer. Unlike 4.1.5,
  `cleanup_outcome` carries a real `closed|killed|leaked` value, which requires
  the emission to happen **after** the lease context exits. The record contains
  no prompt text, response text, credential, auth path, home path, env value,
  account email, session identifier, or raw provider error string. test:
  `tests/ai/agent_sdk/test_droid_oneshot.py`.

### 4.3 Text routing wrapper and builder wiring [category: code] (depends: 2.5, 4.1, 4.2)
`kind: deliverable`

Target: `src/gobby/ai/agent_sdk/route_adapters.py` (new),
`src/gobby/ai/_text_generation_builder.py`,
`src/gobby/ai/_text_generation_service.py`,
`src/gobby/ai/_text_generation_helpers.py`,
`src/gobby/runner_init/services.py`,
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
  `config_getter()` — the daemon's single live getter (2.5), never a captured
  `DaemonConfig` and never one rooted at `runner.config` — and lazily builds and
  caches both inner adapters. The getter is passed to the wrapper directly
  rather than read off the runtime, because the wrapper must resolve routes
  when the runtime is `None`.

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
the error mapping (called at `_text_generation_service.py:595-612`). A
reimplementation would drift on the failure shape first.

**Name the owner, because the three are not currently in one place.**
`_json_request` and `_parse_json_text` already live in
`_text_generation_helpers.py` (`:176`), while `_json_parse_failure` is
service-local (`_text_generation_service.py:120`). "Promote all three to a
shared surface" is ambiguous about which module wins, and picking the service
would create an import from `gobby.ai.agent_sdk` back into the service that
constructs it. `_text_generation_helpers.py` is the dependency-low module and
already owns two of the three, so it owns all three: move `_json_parse_failure`
beside them, and put `StructuredJsonResult` there too, since both the producer
(`route_adapters.py`) and the consumer (`_text_generation_service.py`) import it
and neither should import the other.

The move is byte-for-byte **except** for the raw-preview removal 2.9 requires:
the helper's message drops `raw[:240]` and keeps the parse-error type, the
parse-error message, and `raw_len`. Moving it unchanged would carry a
pre-existing response-content leak onto the SDK path, and this is the section
that touches the helper.

**The public contract does not change.** `StructuredJsonResult` is an
adapter-private return shape that the service unpacks immediately; it must never
reach `TextGenerationService.generate_json`'s callers, which keep receiving a
plain `dict`. Only `RoutingTextGenerateAdapter` produces it.

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
- `_daemon_text_generation_adapter_factories` gains `agent_sdk_runtime` and
  `config_getter` kwargs and wraps the `"codex"`/`"droid"` entries **after**
  the endpoint loop (endpoint providers never wrapped), and only when a
  `config_getter` was supplied. It passes that getter, not the config object it
  was built with.

**Wrap unconditionally *in the daemon*; an absent runtime means unavailable,
not legacy.** The wrapper needs only a config getter to resolve a route — the
runtime is needed to *serve* an sdk route, not to *recognize* one. Installing
the wrapper only "when a runtime is present" therefore silently converts a
fail-closed condition into a fallback: after the Phase-2 constructor failure 2.5
explicitly permits, `agent_sdk_runtime` is `None`, the codex entry is left
unwrapped, and a request configured `codex`/`sdk` is served by the legacy codex
adapter with no error and no signal. That is the precise substitution the
no-fallback rule exists to forbid, and it is reachable in production through
both construction sites (6.3).

So within the daemon the wrapper is always installed, and it takes the runtime
as `AgentSDKRuntimeHandle | None`. With a runtime it behaves as specified above.
With `None` it serves `legacy` snapshots normally and raises
`CapabilityUnavailableError` for an `sdk` snapshot **without constructing the
legacy inner adapter** — the same outcome an unready provider produces, which
is also the honest one: the operator asked for a backend the daemon failed to
build.

**"Unconditionally" cannot mean "at every caller", because the builder has
non-daemon callers.** `build_daemon_text_generation_service` is called from
five places, only one of which is the daemon:

| Caller | Owns a runtime? |
| --- | --- |
| `_init_llm_service` (`runner_init/services.py:62`) | yes (2.5) |
| `create_llm_service` (`llm/factory.py:36`) | no |
| `LLMService.__init__` (`llm/service.py:85`) | no |
| `gobby projects verify` (`cli/projects.py:324`) | no |
| CLI task/session paths reaching the two above | no |

The out-of-daemon callers run in a process with no `AgentSDKRuntime`, no
`ServiceContainer`, and no SDK children — but they read the **same**
`DaemonConfig` from disk. Installing the wrapper there on the strength of
"unconditional" would make `gobby projects verify` start failing with
`CapabilityUnavailableError` the moment an operator promotes a route for the
daemon, breaking established CLI workflows that never had an SDK path to lose.
Fail-closed is right for the surface that was asked to serve an sdk route; it
is not right for a surface where sdk routes are out of scope by construction.

**One kwarg decides both, and it is the config getter.** Both builders gain a
`config_getter: Callable[[], DaemonConfig] | None = None` kwarg alongside the
optional `agent_sdk_runtime`. Route-aware wrappers are installed **iff a
`config_getter` was supplied**:

- The daemon supplies one at every construction site (`_init_llm_service` here;
  `servers/http.py` and `_init_llm_service` for tool chat in 6.3), so the
  wrapper is installed there whether or not the runtime exists — which is
  exactly the fail-closed property above.
- Out-of-daemon callers supply neither kwarg and get today's factories
  byte-identically: no wrapper, no route resolution, no behavior change. SDK
  routes are daemon-scoped, and this is the seam that says so in code rather
  than in prose.

The getter also has to exist *separately from the runtime* for a second reason:
a captured `DaemonConfig` goes stale on the first `set_runtime_config` rebind
(2.5), and when the runtime is `None` there is no runtime getter to borrow. The
daemon roots it at `lambda: services.config or runner.config`, the same
expression 2.5 binds and 7.3 uses.
- `build_daemon_text_generation_service` gains the matching optional
  `agent_sdk_runtime` and `config_getter` kwargs and threads both through.
  **This section also makes the production call site pass them**:
  `_init_llm_service` (`runner_init/services.py`) is where the daemon actually
  builds this service, so adding the kwargs without editing that seam would
  leave every SDK text route dead in production while the builder's own unit
  tests pass. The kwargs and their one real caller land together, which is why
  `runner_init/services.py` is in this section's Targets and why this section
  depends on 2.5 (which puts `runner.agent_sdk_runtime` there to pass).
- `_text_generation_service.py`: the `StructuredJsonResult` unpacking above at
  `:588-593`. No other change to JSON-path dispatch.

**Acceptance:**

- 4.3.1 - route=legacy dispatches to the existing CLI adapters and
  `generate_json` returns the parsed-text result byte-identically to today,
  reusing `_json_request` / `_parse_json_text` / `_json_parse_failure` so a
  malformed payload raises the same error type and shape as today, differing
  only by the removed raw preview; route=sdk dispatches to SDK adapters using
  the SDK's structured output. symbol: `RoutingTextGenerateAdapter`. test:
  `tests/ai/test_text_generation.py`.
- 4.3.9 - With a `config_getter` supplied and `agent_sdk_runtime=None`, the
  routing wrapper is still installed on the codex/droid entries: a `legacy`
  snapshot serves normally, and an `sdk` snapshot raises
  `CapabilityUnavailableError` without constructing or invoking the legacy
  inner adapter. test: `tests/ai/agent_sdk/test_route_adapters.py`. test:
  `tests/runner_init/test_services.py`.
- 4.3.10 - Out-of-daemon callers are unaffected: with **no** `config_getter`
  and no runtime, `build_daemon_text_generation_service` produces the exact
  factory set it produces today — no wrapper on any provider — and a
  `codex.text_generate=sdk` config on disk changes nothing about the result.
  Asserted at the two non-daemon entry points that reach the builder,
  `create_llm_service` (`llm/factory.py:36`) and `gobby projects verify`
  (`cli/projects.py:324`), so a promoted daemon route cannot break a CLI
  workflow that has no runtime. Neither file is modified. symbol:
  `build_daemon_text_generation_service`. test:
  `tests/ai/test_text_generation.py`.
- 4.3.11 - The wrapper's route resolution follows a live rebind with no runtime
  present: with `agent_sdk_runtime=None` and the daemon `config_getter`
  installed, a `set_runtime_config` flip between two requests changes the
  outcome of the second (legacy served → unavailable, and back), which a
  captured `DaemonConfig` could not do. test:
  `tests/ai/agent_sdk/test_route_adapters.py`.
- 4.3.12 - An SDK-routed text request emits `feature_llm_call` with
  `configured_route` `sdk`, the actual SDK `backend` value, and integer
  `usage_input_tokens`/`usage_output_tokens` — the SDK half of 2.9.1, which
  pins only the legacy route because no SDK text adapter exists at 2.9's
  completion. file: `src/gobby/ai/_text_generation_service.py`. test:
  `tests/ai/test_text_generation.py`.
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
- 4.3.7 - `_init_llm_service` passes both `runner.agent_sdk_runtime` and the
  daemon `config_getter` into `build_daemon_text_generation_service`, so a
  daemon built with sdk text routes serves them; and it still passes the getter
  when the runtime is absent, so that path fails closed rather than falling
  back. file: `src/gobby/runner_init/services.py`. test:
  `tests/runner_init/test_services.py`.
- 4.3.8 - `_text_generation_helpers.py` owns `_json_request`,
  `_parse_json_text`, `_json_parse_failure`, and `StructuredJsonResult`, with
  no import from the helpers module back into the service or the adapters; and
  `TextGenerationService.generate_json` still returns a plain `dict` to its
  callers on both routes. file: `src/gobby/ai/_text_generation_helpers.py`.
  test: `tests/ai/test_text_generation.py`.
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

### 5.1 SDK vision adapters [category: code] (depends: 2.4, 2.9, 4.1, 4.2, 4.3)
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
- 5.1.3 - Every SDK exception escaping either vision adapter is sanitized at the
  raise site per 2.9: a provider error carrying a home path, an env value, and a
  token-shaped string surfaces with none of the three, and the formatted
  traceback carries none of them either. test:
  `tests/ai/agent_sdk/test_vision_adapters.py`.
- 5.1.4 - Each vision adapter emits exactly one `agent_sdk_call` per
  `extract()`, on both the success and the failure path, whose keys are exactly
  `provider`, `capability`, `model`, `configured_route`, `backend`, `ready`,
  `queue_wait_ms`, `latency_ms`, `usage_input_tokens`, `usage_output_tokens`,
  `success`, `error`, `cleanup_outcome` — no more and no fewer, with
  `cleanup_outcome` `None` for codex (shared process) and a real value for
  droid (emitted after lease exit). The record contains no image path, no image
  bytes, no extracted text, no credential, auth path, home path, env value,
  account email, or raw provider error string. test:
  `tests/ai/agent_sdk/test_vision_adapters.py`.

### 5.2 Vision registry availability gate [category: code] (depends: 2.4, 5.1)
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
  registers the SDK adapters under `"codex"`/`"droid"` when present. Those
  adapters are created by 5.1, which is why this section depends on it: a
  registration that lands before the thing it registers is meaningless, and the
  end-to-end recovery assertion below cannot run without both halves present.

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
- 5.2.5 - A vision-only configuration recovers from a startup failure without a
  restart: with the provider unready and route=sdk, the probe schedules
  `ensure_provider`, and once it succeeds the next post-TTL probe reports the
  binding selectable and `VisionExtractService.extract` serves the request. This
  is the vision half of 2.4.8, asserted here because this section owns the
  probe. test: `tests/ai/test_capability_registry.py`.

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
- `routes/llm.py::extract_vision` (`:342`): use
  `services.vision_extract_service`, and **return 503 when it is absent** —
  do not rebuild. Error mapping for the service's own failures is unchanged
  (`CapabilityUnavailableError` → 400 `capability_unavailable`).

  **The `or build_...` fallback is not a fallback, it is a downgrade.** The
  per-request construction at `:342` is `build_daemon_vision_extract_service(
  config)` — config only. It has no `agent_sdk_runtime`, so it cannot register
  the SDK adapters (above) and cannot install the availability probe (5.2). If
  the persistent service failed to build, every subsequent vision request would
  silently take that path and see codex/droid vision as permanently
  unavailable, with no signal distinguishing "not configured" from "the daemon
  degraded". Threading the runtime into the fallback would fix the capability
  and keep two construction paths to maintain; returning 503 fixes it with
  none. The daemon has exactly one vision service, and if it is missing the
  daemon says so.

- **The two status routes must not answer from a registry the daemon does not
  use.** `/api/llm/status` (`:200`) and `/api/llm/vision/status` (`:335`) each
  call `build_daemon_ai_capability_registry(server.config)` with no probe
  argument, so with the 5.2 gate in place they would report codex/droid
  `vision_extract` unavailable while the persistent service is serving it. A
  status endpoint that contradicts the behavior it describes is worse than no
  endpoint. Both routes pass the same `agent_sdk_vision_probe` this section's
  wiring builds, derived from `services.agent_sdk_runtime`; when the runtime is
  absent they pass `None` and report exactly today's answer. That is two
  arguments at two call sites — no persistent registry, no new container field,
  and no change to `build_daemon_ai_capability_registry`'s default behavior.

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
- 5.3.4 - `extract_vision` never rebuilds the service: with
  `services.vision_extract_service` absent it returns 503 and constructs
  nothing, so a partially initialized daemon cannot silently serve vision from
  a probe-less, runtime-less registry that reports every SDK route
  unavailable. file: `src/gobby/servers/routes/llm.py`. test:
  `tests/servers/routes/test_llm_routes.py`.
- 5.3.5 - `/api/llm/status` and `/api/llm/vision/status` agree with what the
  daemon will actually serve: with an SDK vision route configured and the
  provider ready, both report codex/droid `vision_extract` available; with the
  runtime absent both report exactly today's answer. file:
  `src/gobby/servers/routes/llm.py`. test:
  `tests/servers/routes/test_llm_routes.py`.

## P6: Tool Chat SDK Routes
`kind: framing`

**Goal**: SDK-transport tool chat preserving Family B semantics via the
existing tool substrate, with honest limits.

### 6.1 Codex SDK tool chat is not buildable on the pinned SDK
`kind: framing`

Earlier rounds of this plan specified a `CodexSDKToolChatAdapter` running
Gobby's Family B tool substrate over `runtime.codex_thread()` with "dynamic
tool specs + tool handler". **That surface does not exist**, and the section is
removed rather than deferred, because nothing releasable can satisfy it.

Verified against the pin, not inferred:

- `AsyncCodex.thread_start` accepts `approval_mode`, `base_instructions`,
  `config`, `cwd`, `developer_instructions`, `ephemeral`, `model`,
  `model_provider`, `personality`, `sandbox`, `service_name`, `service_tier`,
  `session_start_source`, and `thread_source`. `Thread.run` / `Thread.turn` add
  only `effort`, `output_schema`, `summary`. No tool parameter, no handler
  callback, no registration call
  (`sdk/python/src/openai_codex/api.py` @ `rust-v0.144.4`).
- The generated `ThreadStartParams` has no tool field either. The only
  tool-shaped configuration reachable from a thread is `ToolsV2`, whose sole
  member is `web_search` — a built-in toggle, not a registration surface.
  `DynamicToolSpec` exists in the generated protocol types and is referenced by
  **nothing** in the released Python package
  (`sdk/python/src/openai_codex/generated/v2_all.py` @ `rust-v0.144.4`).
- The whole file `api.py` contains zero occurrences of the substring `tool`.
- `0.144.4` is the newest `openai-codex` release on PyPI (the only other
  published versions are `0.1.0b1`-`0.1.0b3`), so there is no later pin to move
  to. Raising the pin is not an available fix.

**Consequences, all of them:**

- `codex.tool_chat` is **not** a config key (1.2). It is not "route=legacy
  forever"; it does not exist, and `extra="forbid"` rejects it exactly as it
  rejects `codex.web_chat`.
- Codex tool chat keeps running on `CodexAppServerClient` unchanged, including
  its known usage drop. Fixing that drop is a separate concern on the legacy
  transport and is out of scope here.
- SDK tool chat in this plan means **droid only** (6.2, 6.3).
- Restoring a Codex SDK tool-chat route requires a released SDK that exposes a
  dynamic-tool registration and handler surface, and a fresh verification of
  that surface's exact call shape. It is a new plan, not a resumed section.

The general lesson is recorded because it cost two rounds: a plan may not
assign downstream work to a dependency call shape that has not been read at the
pinned version. 1.1.4 is the acceptance that enforces it for this dependency.

### 6.2 Droid SDK tool-chat adapter [category: code] (depends: 2.4, 2.9, 3.1)
`kind: deliverable`

Target: `src/gobby/ai/agent_sdk/tool_chat_droid.py` (new)

`DroidSDKToolChatAdapter(runtime)`: pool lease → typed `ToolUse` events →
`runtime.invoke` → `ToolResult` replies; assistant messages accumulate text
and `controller.record_turn()`; permission requests cancelled; completion
event supplies usage/stop_reason. Lease hygiene per 4.2.

This is the **only** SDK tool-chat adapter (6.1). It reuses `validate_policy` +
`ToolRuntime` (`_tool_chat_tools.py`) and `ToolLoopController` — the same
Family B substrate as legacy: read-only gcode scoped to
`request.project_path`, call budget, provenance trace. The daemon/CLI
tool-allowlist lockstep contract (`GCODE_READONLY_TOOLS` /
`GWIKI_READONLY_TOOLS`) is untouched. Budget exhaustion interrupts the session
so the N+1 tool call never executes, matching legacy. It returns a full
`ToolChatResult` including `usage` and `unsupported_limits` derived from
`_ENFORCED_LIMITS = {"max_tool_calls", "loop_timeout_seconds"}`.

**Acceptance:**

- 6.2.1 - ToolUse/ToolResult round-trip through `ToolRuntime` with
  provenance recorded; permission requests cancelled. symbol:
  `DroidSDKToolChatAdapter`. test:
  `tests/ai/agent_sdk/test_tool_chat_droid.py`.
- 6.2.2 - One lease per request with close+replenish on all exit paths and
  budget exhaustion interrupting the session. test:
  `tests/ai/agent_sdk/test_tool_chat_droid.py`.
- 6.2.3 - Result carries usage and `unsupported_limits` excluding exactly the
  enforced set `{"max_tool_calls", "loop_timeout_seconds"}`, and policy
  validation, `ToolRuntime` project scoping, trace/calls_used/budget_exhausted,
  and interruption before tool call N+1 all hold — this adapter carries the
  whole SDK half of the Family B contract, because it is the only SDK
  tool-chat adapter. test: `tests/ai/agent_sdk/test_tool_chat_droid.py`.
- 6.2.4 - Every droid-sdk exception escaping this adapter is sanitized at the
  raise site per 2.9 — including one raised from inside a tool-call round, which
  the tool-chat service folds into its own error surface — so no home path, env
  value, or token-shaped string reaches that surface or the formatted traceback.
  test: `tests/ai/agent_sdk/test_tool_chat_droid.py`.
- 6.2.5 - The adapter emits exactly one `agent_sdk_call` per request — one per
  request, not one per tool round — on both the success and the failure path,
  whose keys are exactly `provider`, `capability`, `model`, `configured_route`,
  `backend`, `ready`, `queue_wait_ms`, `latency_ms`, `usage_input_tokens`,
  `usage_output_tokens`, `success`, `error`, `cleanup_outcome` — no more and no
  fewer, with `cleanup_outcome` carrying a real value emitted after lease exit.
  The record contains no prompt text, response text, **tool arguments or tool
  results**, credential, auth path, home path, env value, account email,
  session identifier, or raw provider error string. test:
  `tests/ai/agent_sdk/test_tool_chat_droid.py`.

### 6.3 Tool-chat routing shim [category: code] (depends: 2.5, 4.3, 6.2)
`kind: deliverable`

Target: `src/gobby/ai/agent_sdk/route_adapters.py`,
`src/gobby/ai/_tool_chat_builder.py`, `src/gobby/ai/_tool_chat_spawn.py`,
`src/gobby/servers/routes/llm.py`, `src/gobby/servers/http.py`,
`src/gobby/runner_init/services.py`

`route_adapters.py` is **created** by 4.3; this section adds a second class to
it, hence that dependency edge. This section also **creates** the
`agent_sdk_runtime` and `config_getter` kwargs on
`build_daemon_tool_chat_service` and wires 2.5's runtime and getter into both
of its call sites, hence the edge on 2.5. The install rule is 4.3's verbatim:
wrappers are installed iff a `config_getter` was supplied, so both daemon
construction sites get them and the builder's non-daemon callers keep today's
behavior exactly.

`RoutingToolChatAdapter(config_getter, sdk_factories, legacy_factory)` wired for the
DAEMON and CLI style factories in `build_daemon_tool_chat_service` (new
`agent_sdk_runtime` kwarg). SDK iff `binding.provider in sdk_factories` AND
`not binding.metadata.get("endpoint")` AND route==sdk; everything else legacy.

`sdk_factories` contains **`"droid"` only** (6.1): codex has no SDK tool-chat
route and no `codex.tool_chat` config key, so a codex tool-chat binding is
never wrapped and never consults a route. Wrapping it would be dead code that
implies a promotable route the config surface cannot express.

**Both construction sites, not just one.** `build_daemon_tool_chat_service` is
called twice in the repo: `_init_llm_service`
(`src/gobby/runner_init/services.py:69`) and the fallback at
`src/gobby/servers/http.py:100-104`, which runs when
`services.tool_chat_service is None`. Pass `services.agent_sdk_runtime` at both.
A daemon that took the fallback path would otherwise serve tool chat from a
runtime-less service, resolving every `sdk` route to legacy with no diagnostic —
silently, because that is exactly what an unconfigured route looks like.

That reasoning generalizes, and it is why the wrapper here follows 4.3's
install rule — keyed on the `config_getter`, not on the runtime.
`agent_sdk_runtime` is `None` on two distinct daemon paths — the `http.py`
fallback when service construction was skipped, and the Phase-2 constructor
failure 2.5 permits — and on both, an unwrapped droid entry would serve an
`sdk`-configured request from the legacy adapter. Both sites pass the getter,
so the wrapper is installed on both and takes the runtime as optional; with
`None` it fails an `sdk` snapshot closed with `CapabilityUnavailableError` and
never constructs the legacy inner adapter. `build_daemon_tool_chat_service`'s
callers are exactly these two, both in-daemon, so there is no CLI exposure here
— but the kwarg still gates the install, so the two builders stay one rule.

No new `AIAdapterStyle`; `_TOOL_CHAT_EXECUTABLE_STYLES`,
`_RUNTIME_ADAPTER_STYLES`, style maps, and `ToolChatService` dispatch all
unchanged (`ToolChatResult.adapter_style` still reports the binding style).
Also fix the stale `_tool_chat_spawn.py` module docstring and `__all__`
omissions while touching the area.

The dispatch-snapshot rule from 4.3 applies: the route is snapshotted into a
local at the single entry point of `chat_result`, so a mid-request flip cannot
switch adapters, restart the tool loop, or double-charge the call budget. Route
resolution reads live config through the injected `config_getter` (2.5) — never
a captured object, never one rooted at `runner.config`, and never one reached
through the runtime, which may be `None`.

This section also carries the SDK half of the `unsupported_limits` route
assertion deferred from 3.1.4, because the SDK tool-chat adapter first exists
at 6.2.

**Acceptance:**

- 6.3.1 - Endpoint-metadata DAEMON bindings always route legacy; droid
  route=sdk dispatches to the SDK adapter; codex tool chat is never wrapped and
  always reaches `CodexAppServerClient`; grok/qwen (ACP style) untouched.
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
- 6.3.7 - The `servers/http.py` fallback construction passes both the runtime
  and the `config_getter`, so a tool-chat service built on that path resolves
  `sdk` routes identically to one built in `_init_llm_service`. file:
  `src/gobby/servers/http.py`. test: `tests/ai/test_tool_chat_service.py`.
- 6.3.8 - `_init_llm_service` passes `runner.agent_sdk_runtime` and the
  `config_getter` into `build_daemon_tool_chat_service`, so a daemon built with
  sdk tool-chat routes serves them. file: `src/gobby/runner_init/services.py`.
  test: `tests/runner_init/test_services.py`.
- 6.3.9 - With a `config_getter` supplied and `agent_sdk_runtime=None` on
  either construction path, a droid tool-chat request whose live route is `sdk`
  raises `CapabilityUnavailableError` and no legacy droid adapter is
  constructed or invoked, while `legacy`-routed requests are served normally;
  with no `config_getter` the builder produces today's factory set unchanged.
  test: `tests/ai/test_tool_chat_service.py`.

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

### 7.2 DroidSDKChatSession and backend [category: code] (depends: 7.1, 2.4, 2.9)
`kind: deliverable`

Target: `src/gobby/servers/websocket/chat/backends/droid_sdk.py` (new),
`src/gobby/servers/websocket/chat/backends/base.py`

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
  `web_chat_backend = "sdk"` classvar.
- **This section owns the `ManagedChatSessionBase` default**, which is why
  `backends/base.py` is a target here. `ManagedChatSessionBase`
  (`src/gobby/servers/websocket/chat/backends/base.py:83`) gains
  `web_chat_backend: str = "legacy"`, so every backend-managed session answers
  the attribute and only this one answers `"sdk"`. Naming the mutation without
  owning the file is how it goes missing: 7.3 targets the same file for
  routing work but does not own this field, and a section that reads
  `session.web_chat_backend` cannot be the section that first defines it. Note
  that Claude's `ChatSession` (`src/gobby/servers/chat_session.py:45`) is a
  different class entirely and deliberately does **not** gain the attribute —
  7.3's pin write is droid-scoped for exactly that reason.
- `start()` raises on runtime-not-ready or init failure **without** side
  effects — never constructs a legacy session; backend `health()` returns
  `ProviderBackendHealth`.
- Provider exceptions are sanitized at this boundary exactly as in the adapters
  (2.9): wrapped in a sanitized domain error and raised `from None`, so the web
  chat error paths — which use `logger.exception` and fold `str(exc)` into debug
  payloads — cannot format a raw droid-sdk message. Web chat is the one SDK
  surface not covered by the adapter list, and it is the surface closest to the
  user.
- **This session emits its own `agent_sdk_call` events**, one per provider call,
  on the same rule every SDK adapter follows (2.9) — the difference is only that
  its provider call is a message rather than a request. It holds a dedicated
  client for the conversation's whole life and never takes a lease, so
  `queue_wait_ms` is zero (a dedicated client waits on no admission gate).
  Everything else is identical: same key set, same sanitization, same
  exclusions.

  Because this emitter shares no code with the adapters' and no typed schema
  enforces the shape, its acceptance asserts the **exact** key set rather than a
  representative sample. A partial emitter here would be invisible — it would
  still log something plausible per message — and this is the surface where a
  leak would carry live user conversation content.

**Acceptance:**

- 7.2.1 - Session implements the full chat-session protocol (streaming,
  images, usage, interrupt, plans, permissions, lifecycle) against a fake
  client factory. symbol: `DroidSDKChatSession`. test:
  `tests/servers/websocket/chat/test_droid_sdk_backend.py`.
- 7.2.2 - `ask_user` requests are cancelled and no compact action is
  exposed. test: `tests/servers/websocket/chat/test_droid_sdk_backend.py`.
- 7.2.3 - `start()` failure raises with no legacy session and no client
  leak. test: `tests/servers/websocket/chat/test_droid_sdk_backend.py`.
- 7.2.6 - `ManagedChatSessionBase` defines `web_chat_backend` defaulting to
  `"legacy"`, `DroidSDKChatSession` overrides it to `"sdk"`, every other
  backend-managed session reports `"legacy"`, and Claude's `ChatSession` does
  not define the attribute at all. symbol: `ManagedChatSessionBase`. file:
  `src/gobby/servers/websocket/chat/backends/base.py`. test:
  `tests/servers/websocket/chat/test_droid_sdk_backend.py`.
- 7.2.4 - A droid-sdk exception carrying a home path, an env value, and a
  token-shaped string surfaces as a sanitized error with its cause suppressed,
  and a `logger.exception` capture of the failure contains none of the three.
  test: `tests/servers/websocket/chat/test_droid_sdk_backend.py`.
- 7.2.5 - A conversation that sends three messages emits three `agent_sdk_call`
  events — not one for the conversation — and each carries exactly the keys
  `provider`, `capability`, `model`, `configured_route`, `backend`, `ready`,
  `queue_wait_ms` (zero, because a dedicated client waits on no admission
  gate), `latency_ms`, `usage_input_tokens`, `usage_output_tokens`, `success`,
  `error`, `cleanup_outcome` (`None`, because the dedicated client's lifecycle
  spans the conversation, not the call) — no more and no fewer. Asserted on
  both a successful message and a failed one, and asserted to contain no prompt
  text, no response text, no credential, no auth path, no home path, no env
  value, no conversation identifier, and no raw provider error string. test:
  `tests/servers/websocket/chat/test_droid_sdk_backend.py`.

### 7.3 Backend selection and pinning [category: code] (depends: 7.2, 1.4, 2.5)
`kind: deliverable`

Target: `src/gobby/servers/websocket/chat/runtime_manager.py`,
`src/gobby/servers/websocket/chat/_session.py`,
`src/gobby/servers/websocket/chat/_web_chat_pin.py`,
`src/gobby/servers/websocket/chat/backends/base.py`,
`src/gobby/servers/websocket/handlers/session_observe_continue.py`,
`src/gobby/servers/websocket/handlers/session_config.py`,
`src/gobby/runner_init/servers.py`

This section consumes two things 2.5 creates — `runner.agent_sdk_runtime` and
the rebound config getter — and it changes the `WebChatRuntimeManager`
construction in `init_servers` to receive them, so it depends on 2.5 and targets
`runner_init/servers.py`. Injecting a dependency at a constructor the section
does not own is how the wiring silently goes missing in production.

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
  `"sdk"` → `DroidSDKChatSession`. All other providers ignore the kwarg.

  **Reject on absent, not on unready.** The failure condition here is narrower
  than it first looks, and getting it wrong disables the recovery path 2.4
  depends on. If the runtime is **absent** (all-legacy, or a failed
  construction) there is nothing to recover and selection raises immediately.
  But if the runtime **exists with an unready droid provider**, selection must
  fall through to `DroidSDKChatSession.start()` and let it acquire the client:
  `droid_web_chat_client()` is the only `ensure_provider` trigger web chat has
  (2.4), and it lives *inside* the path a readiness guard here would prevent
  anyone from reaching. Guarding on readiness makes the trigger dead code and
  makes startup degradation permanent for web chat — a daemon whose droid probe
  failed at boot could never serve web chat again without a restart, even after
  the underlying problem cleared. Failing closed is still the outcome when
  recovery genuinely fails: `ensure_provider` runs, and if it cannot make the
  provider ready the client acquisition raises and the connect fails with the
  same unready-SDK error. The difference is that it fails *after* trying.

  **Trying must be bounded, and must happen once.** "Fails after trying" is only
  acceptable if the trying ends. The 30s cooldown on `ensure_provider` (2.4)
  limits how *often* a probe runs, not how *long* one takes, so a provider whose
  initialization hangs leaves the WebSocket connect waiting indefinitely with no
  error and no timeout — the client sees a dead connection rather than a failed
  one. Both `ensure_provider` and the dedicated-client initialization that
  follows it run under the runtime's `timeout_seconds`, with the transactional
  cleanup of 2.4 disposing of anything a timed-out attempt built.

  It must also not become two attempts. `_session.py:828-836` catches
  `Exception` around `session.start(...)` and, whenever `resume_session_id` is
  set, clears it and calls `start()` again as a fresh session. That retry exists
  for one specific condition — the provider no longer has the session being
  resumed — but it is written against a bare `Exception`, so an SDK
  availability, auth, or timeout failure would trigger a **second** full SDK
  start: two bounded waits back to back, two initialization attempts, and two
  chances to strand partially built resources, all to reach the same fail-closed
  answer.

  **Scope the narrowing to this backend; do not narrow the shared catch.**
  That `except Exception` is on the path every provider takes — Claude, Codex,
  ACP (grok/qwen), and legacy Droid all reach it, and each raises its own
  provider-specific exception type for a missing resume target. Replacing the
  bare catch with a typed predicate would require a cross-backend
  resume-not-found contract that no section of this plan owns, and shipping it
  without one would either delete working recovery for four backends or push
  the implementation into matching on exception message text. Neither is worth
  it for a problem that exists only on the new path.

  So the guard is backend-scoped: inside the existing handler, when the session
  is a `DroidSDKChatSession`, retry only on the backend's explicit typed
  resume-not-found error and re-raise everything else; for every other session
  type the handler behaves exactly as it does today. One `isinstance` branch,
  no shared contract, and the no-fallback invariant holds where it is new.
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
  3. **Registration failure must fail the web-chat connect.** The call at
     `:685-700` currently sits under `except Exception as e: logger.warning(
     "Failed to register web-chat session in DB: %s", e)` (`:740-741`) and
     execution simply continues, so a web-chat session can run today with no
     durable row at all. That is incompatible with everything else in this
     section: with no row there is no `db_session_id`, so the write-once pin can
     never be written and never be checked, and the next reconnect resolves from
     live config as though the conversation had never been served. The
     fail-closed lookup policy in step 2 would be decorative if the write path
     it protects can be skipped silently.

     For **web-chat** connects specifically, a registration failure therefore
     tears down the provisional session and fails the connect, rather than
     warning and proceeding. Other session kinds keep the existing tolerant
     behavior — the invariant being protected is durable web-chat identity, and
     nothing else registered through this path depends on it.

     The row is still created **unpinned**, and `db_session_id`/`seq_num` remain
     available to the wiring at `:698-758` that consumes them. If register
     returns a **pre-existing** row whose `web_chat_backend` is non-NULL and
     disagrees with the backend already selected in step 1, the row wins: the
     mismatched session is torn down and the connect fails, and the pin is never
     rewritten to match a session that should not have been built. This is a
     lost race that the per-conversation lock makes rare, not impossible
     (a different daemon process can pin concurrently).
  4. **After** `await session.start(...)` returns successfully (`:829/836`),
     call `pin_web_chat_backend(session.db_session_id,
     session.web_chat_backend)` — **only when the effective provider is
     `droid`** and the stored pin was `NULL`.

     The droid scope is load-bearing, not tidiness. `web_chat_backend` is
     defined on `ManagedChatSessionBase` (7.2), and Claude's sessions are
     `ChatSession` (`src/gobby/servers/chat_session.py:45`), a different class
     that does not inherit from it and does not carry the attribute; neither
     does `ChatSessionProtocol`. A pin step written generically over
     "web-chat providers" therefore either raises `AttributeError` after a
     *successful* Claude start — failing a connect that had already worked — or
     survives only by a `getattr` default that quietly writes a meaningless
     `'legacy'` pin onto conversations this plan does not touch. That stray pin
     is not inert: it is exactly the value that would later deny a droid SDK
     session on a conversation that switched providers. Guard on
     `provider == "droid"` before touching the attribute, and leave Claude,
     Codex, and Qwen paths with no pin logic at all.

     This write is **not** best-effort, because both of its failure modes leave
     a live session that disagrees with the durable record:
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

**The pin is only authoritative if the provider it belongs to is resolved
first.** `continue_in_chat` computes `effective_provider = target_provider or
source_provider` (`session_observe_continue.py:194`), letting a provider named
in the *request* outrank the durable one, and then actively rewrites the stored
provider to match (`:288-296`). A reconnect that omits the provider, or carries
a stale one from before the conversation moved to droid, can therefore be
dispatched as a different provider entirely — and a non-droid branch never
consults the pin at all, so the mismatch check added above never runs. The pin
survives, correctly written, and is simply not read.

Load the durable `(provider, web_chat_backend)` as **one pair, from the stored
row, before** any pending or `continue_in_chat` override is applied. A stored
provider with a non-NULL pin is authoritative and a conflicting request-supplied
provider fails the connect rather than silently winning; a request provider may
still fill in a genuinely absent stored one. Resolving them together is the
point — they describe one durable identity, and splitting the decision is what
lets a transient field override half of it.

**`_pending_providers` is a second door into the same bypass, and it currently
has the highest precedence of all.** `_create_chat_session_inner` resolves the
provider at `:380-397` in this order: the popped `_pending_providers` entry
first, *then* `existing_db_session.source`, then the request argument, then the
configured binding. So the transient value set by a `session_config` message
outranks the durable row outright. A conversation whose row says
`(droid, sdk)` can be dispatched as `claude` or `codex` by a pending entry, and
those branches never look at `web_chat_backend` — the pin is intact, correctly
written, and simply never consulted. Fixing only the `continue_in_chat`
precedence would leave this adjacent path open, and it is the more reachable of
the two.

The same rule therefore covers both sites: the durable pair is resolved first,
and when the pin is non-NULL a conflicting pending provider fails the connect
rather than taking precedence. A pending provider still applies normally to an
unpinned conversation, which is every conversation this plan does not touch.

**And a third door writes the durable provider directly, upstream of both.**
`handle_set_provider` (`src/gobby/servers/websocket/handlers/session_config.py:623`)
does not merely queue a pending provider — before queueing it, it cancels the
live chat and calls `session_manager.update(..., source=provider)`, mutating
the durable row. Closing only the two read-side doors above leaves this one
open, and it is the one that defeats them: by the time
`_create_chat_session_inner` runs its conflict check, the stored `source` has
already been rewritten to the new provider, so there is no conflict left to
detect. A row that was `(droid, sdk)` becomes `(claude, sdk)` durably, and the
pin is now attached to a provider that never consults it. Checking for a
mismatch after the thing that causes it has been persisted is not a check.

The pin describes a durable `(provider, backend)` identity, so the provider
half cannot be mutated while the backend half is pinned. `handle_set_provider`
therefore loads the durable row **first**, before cancellation and before any
storage write, and rejects the switch with an error message when
`web_chat_backend` is non-NULL and the requested provider differs from the
stored `source` — no cancellation, no `update`, no pending entry, no
`provider_switched` event. This is the same rule as the other two doors stated
at the site that writes rather than the site that reads, and it inherits the
same remediation: the supported answer for a pinned conversation is a new
conversation. A conversation whose pin is `NULL` — every conversation this plan
does not touch — switches provider exactly as it does today.

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
operator just disabled.

**Remediation, stated at its real level.** The supported answer for a user whose
conversation is stranded this way is to start a new conversation; that is the
operator-facing remediation 8.2 documents. The pin can also be cleared directly
in session storage (1.4's explicit-`None` clear), which unstrands the existing
conversation by letting it re-resolve — but this plan ships **no** CLI command
and no HTTP route for it, so it is admin/test tooling, not an operator workflow,
and 8.2 must not present it as one. Building a dedicated operator command for a
documented non-goal would be new surface bought for an edge that already has a
supported answer; the storage-level clear exists so the state is recoverable at
all, not so it becomes a feature.

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
  clearing the stored pin to `NULL` (1.4.5) lets the next connect re-resolve
  from live config. test:
  `tests/servers/websocket/chat/test_provider_routing.py`.
- 7.3.9 - A runtime that exists with an unready droid provider does **not**
  short-circuit selection: the connect proceeds to client acquisition,
  `ensure_provider` runs, and a provider that recovers serves the conversation
  without a daemon restart. Only an absent runtime rejects before that point,
  and a recovery that fails still fails the connect. test:
  `tests/servers/websocket/chat/test_provider_routing.py`.
- 7.3.10 - A web-chat connect whose durable registration fails is torn down and
  fails, leaving no live session without a row; non-web-chat registration keeps
  its existing tolerant behavior. file:
  `src/gobby/servers/websocket/chat/_session.py`. test:
  `tests/servers/websocket/chat/test_provider_routing.py`.
- 7.3.11 - `continue_in_chat` resolves the stored provider and pin as one pair
  before applying request overrides: a reconnect that omits the provider still
  reaches the droid pin-aware path, a request provider conflicting with a pinned
  droid row fails the connect instead of overriding it, and a stored provider
  absent from the row can still be supplied by the request. file:
  `src/gobby/servers/websocket/handlers/session_observe_continue.py`. test:
  `tests/servers/websocket/chat/test_provider_routing.py`.
- 7.3.12 - `init_servers` constructs `WebChatRuntimeManager` with both
  `agent_sdk_runtime` and the rebound `config_getter`, and still constructs it
  successfully when the runtime is absent. file:
  `src/gobby/runner_init/servers.py`. test:
  `tests/runner_init/test_servers.py`.
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
- 7.3.13 - A stored `(droid, sdk)` row is not overridden by a pending provider:
  a `_pending_providers` entry naming `claude` or `codex` fails the connect
  instead of taking precedence, while a pending provider still applies normally
  to a conversation whose pin is `NULL`. file:
  `src/gobby/servers/websocket/chat/_session.py`. test:
  `tests/servers/websocket/chat/test_provider_routing.py`.
- 7.3.14 - Web-chat recovery is bounded and single-attempt: an
  `ensure_provider` or dedicated-client initialization that never completes
  fails the connect within the runtime timeout leaving no surviving child; a
  resumed `DroidSDKChatSession` whose start fails for an availability, auth, or
  timeout reason is **not** retried as a fresh start, while its explicit typed
  resume-not-found error still retries; and the retry behavior for Claude,
  Codex, ACP, and legacy Droid sessions on that same handler is unchanged. file:
  `src/gobby/servers/websocket/chat/_session.py`. test:
  `tests/servers/websocket/chat/test_provider_routing.py`.
- 7.3.15 - The pin write is droid-scoped: a successful Claude, Codex, or Qwen
  web-chat start completes without touching `web_chat_backend` and leaves the
  row's pin `NULL`, so no non-droid conversation acquires a stray `'legacy'`
  pin and no successful start can fail on a missing attribute. file:
  `src/gobby/servers/websocket/chat/_web_chat_pin.py`. test:
  `tests/servers/websocket/chat/test_provider_routing.py`.
- 7.3.16 - `handle_set_provider` cannot orphan a pin: against a row pinned
  `(droid, sdk)`, a `set_provider` to `claude` is rejected before the live
  session is cancelled and before any storage write — the durable `source` is
  unchanged, no pending provider is queued, and no `provider_switched` event is
  sent — while the same message against a row whose pin is `NULL` behaves
  exactly as it does today. file:
  `src/gobby/servers/websocket/handlers/session_config.py`. test:
  `tests/servers/websocket/handlers/test_set_provider.py`.

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

**Each scenario needs its own acceptance, or the section accepts nothing.**
A single "skips cleanly and the gates are registered" criterion is satisfied by
a file containing three `pass` bodies — it verifies the plumbing and none of the
behavior, in the one section whose entire purpose is behavioral verification
against a real authenticated SDK. The default-skip and gate-registration
assertion is still required (it is what keeps these tests out of everyone's
pre-push), but each promised scenario is accepted on its own terms, conditioned
on its gate and prerequisite executable being present.

**Acceptance:**

- 8.1.1 - Live tests skip cleanly without env/CLI and the gate-list
  assertion passes with both new gates. file: `pre-push-test.sh`. test:
  `tests/ci/test_postgres_test_stack.py`.
- 8.1.2 - With `GOBBY_RUN_CODEX_SDK_LIVE=1` and the codex executable present, a
  live Codex SDK text round-trip returns non-empty text with usage; a real
  authenticated paid-subscription account is **accepted** by the 2.2 allowlist;
  and the API-key account path is rejected with the fixed sanitized message.
  The accepted-account half matters because an allowlist that rejects
  everything also passes every rejection test. test:
  `tests/ai/agent_sdk/test_live_sdk.py`.
- 8.1.3 - With `GOBBY_RUN_DROID_SDK_LIVE=1` and the droid executable present, a
  live droid lease round-trip initializes exactly one session, returns text, and
  closes and replaces the leased client leaving no surviving process. test:
  `tests/ai/agent_sdk/test_live_sdk.py`.

### 8.2 Documentation updates [category: docs] (depends: 4.3, 5.3, 6.3, 7.3)
`kind: deliverable`

Target: `docs/guides/ai-configuration.md`, `docs/guides/llm-features.md`,
`docs/guides/providers-and-models.md`, `docs/guides/ai-daemon-contract.md`

Document `ai.agent_sdk_routes` (defaults, per-capability promotion, rollback
semantics, no-fallback semantics, vision's legacy=unavailable), the
`AgentSDKRuntime` diagnostics block, web-chat backend pinning, and
disambiguate the daemon `ai.agent_sdk_routes` namespace from the gcore/CLI
`ai.*` namespace. Refresh `_Last verified_` footers.

State the route surface as **six** keys and say why it is not eight: codex has
no `web_chat` route (no interactive approval callbacks) and no `tool_chat`
route (the pinned SDK exposes no way to register Gobby's tools — 6.1). An
operator who reads "SDK routes for codex and droid" and then cannot find
`ai.agent_sdk_routes.codex.tool_chat` must find the answer in the guide, not in
a validation error. Say plainly that Codex tool chat continues to run on the
existing transport and is unaffected by these settings.

Also document the caller-visible surfaces the routes add: the
`unsupported_limits` field on the tool-chat route's `investigation` block
(3.1), the `queue_wait_ms` / concurrency-gauge distinction between admission
delay and provider latency (2.8/2.9), and the three-state web-chat pin
(unpinned → written once after a successful first start → immutable).

Two operator-facing caveats must be stated plainly rather than left implied:

- **Rollback is config-only for text generation, vision, and tool chat, but
  not for droid web chat.** A conversation already pinned `sdk` keeps requiring
  the SDK runtime after a rollback to legacy and fails the connect if the
  runtime is gone. Document the remediation from 7.3 next to the claim, so an
  operator planning a rollback learns the exception before performing one — and
  document it accurately: the supported remediation is **starting a new
  conversation**. Do not describe clearing the pin as an operator procedure;
  no command or route ships for it, and telling an operator to reach into
  session storage would be inventing a workflow this plan does not build. For
  the three
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
  accurately with the daemon/CLI namespace distinction, document the surface as
  six keys, and state that codex has no `tool_chat` or `web_chat` route and
  why. file: `docs/guides/ai-configuration.md`.
- 8.2.2 - The guides document `unsupported_limits`, the admission-vs-provider
  latency split including the residual spawn-cold gate outside `queue_wait_ms`,
  and the pin lifecycle. file: `docs/guides/llm-features.md`.
- 8.2.3 - The rollback section states the droid web-chat exception and names
  starting a new conversation as the remediation, rather than claiming rollback
  is config-only for every capability or describing a pin-clearing command that
  does not exist. file: `docs/guides/ai-configuration.md`.

## V1 Plan Changelog

`kind: verification`
