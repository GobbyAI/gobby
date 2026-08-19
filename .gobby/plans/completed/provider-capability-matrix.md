# Extensible Provider-Model Capability Matrix (#19483)

**Plan ID:** provider-capability-matrix

## Overview
`kind: framing`

Build a source-derived capability service for Claude, Codex, Droid, Grok, and
Qwen that unifies model identity, reasoning, context limits, latency, speed
routes, multipliers, provenance, and refresh health behind the existing
`GET /api/providers/models` contract. PostgreSQL becomes the authoritative
store (replacing the `~/.gobby/provider-model-catalog.json` runtime cache and
the static Python catalogs in `src/gobby/servers/`); provider collector and
activation registries let future providers be added without schema changes or
branches in the core resolver. The provider-independent `model_metadata`
registry (OpenRouter) is preserved as the shared context/output fallback.
Follow-up to #19475 (closed, commit `aa73f787b`); prerequisite for epic #19564.

## Context
`kind: framing`

What exists today (verified 2026-08-03):

- Catalog engine: `src/gobby/servers/provider_models.py::ProviderModelCatalog`
  — live→cache→static→failed fallback, runtime JSON cache
  `~/.gobby/provider-model-catalog.json` (`_CACHE_VERSION = 5`). There is no
  shipped JSON catalog; static data lives in Python:
  `servers/routes/providers.py::_BASE_MODEL_CATALOG` (claude/codex static;
  qwen `[]`), `servers/provider_model_defaults.py` (`DROID_MODEL_CATALOG`,
  `AGY_MODELS`, `GEMINI_FAMILY_MODELS`),
  `servers/provider_models_grok.py::GROK_STATIC_MODEL_CATALOG`.
- The droid docs parser `_parse_droid_docs_models` already fetches
  `https://docs.factory.ai/models.md` and **discards the multiplier column**.
  Fast variants exist only as separate model IDs (`claude-opus-4-6-fast`,
  `gpt-5.4-fast`, `gpt-5.3-codex-fast`, `glm-5.2-fast`,
  `grok-composer-2.5-fast`).
- `model_metadata` table: PK `(model)`, populated from OpenRouter by a 24 h
  loop (`runner_model_metadata_refresh.py`) that sleeps before first refresh.
  Its only consumer is context-window fallback.
- Effort handling: `agents/provider_capabilities.py::PROVIDER_CAPABILITIES`
  static fallback efforts (agy absent; droid's `off`/`none`/`minimal` missing)
  and `agents/reasoning.py::resolve_spawn_reasoning`, which still rejects
  models absent from the startup catalog with `unsupported_model` (#19475
  residual — fixed by this plan in 3.2/3.3).
- No speed/service-tier concept exists anywhere in `src/` or `web/src`.
- External sources verified: `code.claude.com/docs/en/model-config.md` (alias
  tables, `/fast` notes), `platform.claude.com/docs/en/about-claude/models/overview.md`
  (canonical IDs, context window, max output, comparative latency, thinking
  support), `docs.factory.ai/models.md` (IDs, reasoning efforts, usage
  multipliers, explicit Fast entries; **no context windows**). The Codex
  `openai_models.rs` file is the protocol *type* definition — actual metadata
  (`ModelInfo`, `ModelServiceTier`, `SPEED_TIER_FAST = "fast"`,
  `supports_fast_mode()`, `ReasoningEffort` incl. `None`/`Minimal`/`XHigh`/
  `Max`/`Ultra`) comes from the **local app-server `/models` endpoint**, not
  from GitHub.

## Constraints
`kind: framing`

- 0.5.0 unshipped: no backward compatibility. `GET /api/providers/models` is
  replaced outright; all consumers update in the same phase. No compatibility
  adapters.
- The DB is authoritative. Static Python catalogs are demoted to a one-time
  bundled seed (provenance `bundled`, health `stale`) applied only when a
  provider has zero rows — needed because Claude and Droid collectors depend
  on remote docs and a fresh offline install must not show empty pickers.
  Codex/Grok/Qwen collectors are local-source and need no seed.
- Static provider metadata remains limited to transport mechanics
  (`providers/registry.py::ProviderMetadata`, reasoning flag styles in
  `agents/provider_capabilities.py`) and parser registration.
- AGY is out of scope (epic #18653): the read route keeps serving AGY from
  `AGY_MODELS`, and this plan must not edit `AGY_MODELS` or
  `GEMINI_FAMILY_MODELS` — the in-flight `.gobby/plans/agy-full-integration.md`
  owns `provider_model_defaults.py` edits. The droid side of the
  `provider_model_defaults.py:15-17` default-effort conflict dissolves when
  droid rows come from the collector.
- Configured `endpoint:<name>` providers and local generation groups stay
  outside the matrix; the route continues composing them as today.
- Epic #19564 (blocked by this task) owns all non-contract surfaces: web
  per-send controls, `/fast <message>`, launch UI/CLI flags, native terminal
  hooks and provider-native commands, unavailable/degraded UX. Frontend work
  here is limited to compatibility in `web/src/lib/providerModels.ts`.
- `speed_mode` is request-scoped, never inherited or persisted (not added to
  launch-defaults or agent definitions). No automatic model substitution.
- Remote source material must never become arbitrary commands, environment
  writes, or filesystem paths: activation descriptors validate against a
  registered handler allowlist; v1 registers only `model_selector`,
  `cli_config`, and `request_parameter` (each has a concrete v1 consumer).
  `settings_overlay` / `native_command` handlers belong to #19564 — the
  registry accepts new kinds by registration without schema change.
- Migrations: the plan provisionally uses slot **371**; slots ≥371 are
  contested by the credential-isolation epic (#19543 WP2+), so the coordinator
  re-verifies `MAX(version)` against both disk and the live hub at merge and
  assigns the final number then — renaming the file and updating the chain
  test expectations is part of landing 1.1, not a plan change. Strict
  contiguity ≥354; new tables go in BOTH `postgres_baseline_schema.sql` and
  the numbered migration (contract tests compare them). If 1.1 lands after
  the #19424 migration flatten, the no-new-migrations-until-0.5.0 rule
  applies instead: create the three tables directly in the regenerated
  baseline with no numbered migration file, and 1.1's migration-file target
  plus acceptance 1.1.2 are satisfied by the baseline-only path.
- Capability history is out of scope: current state only.
- The `deferred-from:19483:D1` label on #19564 must be reconciled with the
  registered plan-id at execution time (single `update_task` label edit) so
  the D1 deferral gate resolves.
- Tests: `GOBBY_TEST_PROTECT=1`, focused runs only; scoped Ruff, mypy, and
  test-type audit; never the full suite. All new/touched hand-maintained
  files stay under the 1,000-line ceiling (split modules accordingly).
- Rust touch (4.3) requires rebuild **and reinstall** of `~/.gobby/bin/gwiki`.

## P1: Capability domain and persistence
`kind: framing`

**Goal**: Typed capability domain, three new tables, and a store with atomic
per-provider snapshot replacement.

### 1.1 Add provider capability tables (capability-matrix migration + baseline) [category: code]
`kind: deliverable`

Targets:
- `src/gobby/storage/migrations/371_provider_capability_matrix.sql`
- `src/gobby/storage/postgres_baseline_schema.sql`

New migration `371_provider_capability_matrix.sql` (slot provisional — final
number assigned at merge per Constraints; non-destructive, plain
`CREATE TABLE`), mirrored verbatim in `postgres_baseline_schema.sql`:

```sql
CREATE TABLE provider_capability_refresh_state (
    provider TEXT NOT NULL,
    source_key TEXT NOT NULL,
    source_url TEXT,
    required BOOLEAN NOT NULL DEFAULT TRUE,
    generation BIGINT NOT NULL DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'pending',  -- pending|ok|stale|error
    attempts INTEGER NOT NULL DEFAULT 0,
    last_attempt_at TIMESTAMPTZ,
    last_success_at TIMESTAMPTZ,
    last_error TEXT,
    CONSTRAINT provider_capability_refresh_state_pkey
        PRIMARY KEY (provider, source_key)
);

CREATE TABLE provider_model_capabilities (
    provider TEXT NOT NULL,
    canonical_model TEXT NOT NULL,
    display_name TEXT NOT NULL,
    aliases JSONB NOT NULL DEFAULT '[]',
    available BOOLEAN NOT NULL DEFAULT TRUE,
    hidden BOOLEAN NOT NULL DEFAULT FALSE,
    is_default BOOLEAN NOT NULL DEFAULT FALSE,
    context_length INTEGER,
    max_output_tokens INTEGER,
    reasoning TEXT NOT NULL DEFAULT 'unknown',  -- known|unsupported|unknown
    supported_efforts JSONB,   -- NULL = unknown; [] = explicitly empty
    default_effort TEXT,
    latency_class TEXT,        -- slower|moderate|fast|fastest|NULL
    input_modalities JSONB,
    supports_tools BOOLEAN,
    generation BIGINT NOT NULL,
    provenance JSONB NOT NULL,
    CONSTRAINT provider_model_capabilities_pkey
        PRIMARY KEY (provider, canonical_model)
);

CREATE TABLE provider_model_routes (
    provider TEXT NOT NULL,
    canonical_model TEXT NOT NULL,
    speed_mode TEXT NOT NULL,  -- standard|fast
    selector TEXT NOT NULL,
    available BOOLEAN NOT NULL DEFAULT TRUE,
    usage_multiplier NUMERIC,
    throughput_multiplier NUMERIC,
    latency_class TEXT,
    activations JSONB NOT NULL DEFAULT '[]',
    generation BIGINT NOT NULL,
    provenance JSONB NOT NULL,
    CONSTRAINT provider_model_routes_pkey
        PRIMARY KEY (provider, canonical_model, speed_mode),
    CONSTRAINT provider_model_routes_capability_fkey
        FOREIGN KEY (provider, canonical_model)
        REFERENCES provider_model_capabilities (provider, canonical_model)
        ON DELETE CASCADE
);
```

Design notes:

- Provider names and `speed_mode`/`state`/`reasoning` values are TEXT, not DB
  enums — extensibility without schema change; validation is application-side
  (1.2).
- Activations are an ordered JSONB array on the route row, not a fourth
  table: they are only ever read with their route and replaced atomically
  with the snapshot; nothing queries them independently.
- `provenance` maps fact name → `{source_key, source_url, observed_at}`.
- `model_metadata` is untouched.

**Acceptance:**

- 1.1.1 - The capability-matrix migration creates the three tables and the baseline matches it. test: `tests/storage/test_migration_contract.py::test_provider_capability_tables_match_baseline`.
- 1.1.2 - Migration chain remains contiguous through the assigned slot. test: `tests/storage/test_migration_contract.py::test_postgres_migrations_preserve_known_post_baseline_sequence`.
- 1.1.3 - Routes cascade-delete with their capability row. test: `tests/storage/test_provider_capability_store.py::test_route_rows_cascade_on_capability_delete`.

### 1.2 Typed capability domain and activation validation [category: code]
`kind: deliverable`

Targets:
- `src/gobby/providers/capabilities/__init__.py`
- `src/gobby/providers/capabilities/models.py`
- `src/gobby/providers/capabilities/activation.py`

New package `src/gobby/providers/capabilities/`. `models.py` defines frozen
dataclasses / `StrEnum`s mirroring 1.1:

```python
class SpeedMode(StrEnum):
    STANDARD = "standard"
    FAST = "fast"

class ReasoningSupport(StrEnum):
    KNOWN = "known"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"

class SourceState(StrEnum):
    PENDING = "pending"; OK = "ok"; STALE = "stale"; ERROR = "error"

@dataclass(frozen=True)
class FactProvenance:
    source_key: str
    source_url: str | None
    observed_at: datetime

@dataclass(frozen=True)
class ActivationDescriptor:
    kind: str                      # validated against registry
    surface: str                   # e.g. "spawn-cli", "app-server", "tool-chat"
    params: Mapping[str, str]      # string-to-string only

@dataclass(frozen=True)
class ModelRoute:
    speed_mode: SpeedMode
    selector: str
    available: bool
    usage_multiplier: Decimal | None
    throughput_multiplier: Decimal | None
    latency_class: str | None
    activations: tuple[ActivationDescriptor, ...]
    provenance: Mapping[str, FactProvenance]

@dataclass(frozen=True)
class ModelCapability: ...          # all 1.1 columns, routes: tuple[ModelRoute, ...]

@dataclass(frozen=True)
class ProviderSnapshot:
    provider: str
    generation: int
    models: tuple[ModelCapability, ...]
    sources: tuple[SourceHealth, ...]
```

`activation.py` holds the handler registry: `register_activation_handler(kind,
handler)`, `validate_activation(descriptor)`. Handlers declare the surfaces
they serve and validate `params` (string values only, allowlisted keys, no
paths/env/exec). v1 registers `model_selector` (params: none — selector comes
from the route row), `cli_config` (params: `key`, `value` for codex
`-c key=value`), `request_parameter` (params: `name`, `value`). Unknown kinds
or malformed params raise `ActivationValidationError`; snapshot validation
(2.1) rejects the whole provider snapshot on any invalid descriptor.

**Acceptance:**

- 1.2.1 - Typed domain round-trips all 1.1 fields incl. NULL-vs-empty efforts. test: `tests/providers/capabilities/test_models.py::test_supported_efforts_null_vs_empty_distinct`.
- 1.2.2 - Unknown activation kind rejected. test: `tests/providers/capabilities/test_activation.py::test_unknown_activation_kind_rejected`.
- 1.2.3 - Source-derived payloads cannot smuggle exec/env/path params. test: `tests/providers/capabilities/test_activation.py::test_activation_params_reject_non_string_and_disallowed_keys`.
- 1.2.4 - New handler kinds register without touching schema or resolver. test: `tests/providers/capabilities/test_activation.py::test_register_new_handler_kind`.

### 1.3 Capability store with atomic snapshot replacement [category: code] (depends: 1.1, 1.2)
`kind: deliverable`

Targets:
- `src/gobby/providers/capabilities/store.py`

`ProviderCapabilityStore(db)` using the hub transaction boundary and psycopg
`%s` placeholders (per CLAUDE.md):

- `replace_provider_snapshot(snapshot)` — single transaction: bump
  generation, `DELETE` provider's capability rows (routes cascade), insert new
  rows, upsert `provider_capability_refresh_state` per source. All-or-nothing.
- `get_provider_snapshot(provider)`, `get_all_snapshots()` — read models with
  routes + source health in provider display order.
- `record_source_failure(provider, source_key, error)` — increments attempts,
  sets `state='error'` (or `'stale'` when rows exist), leaves rows intact.
- `mark_stale(provider)` for seed rows and aged snapshots.
- `has_rows(provider)` for seeding (2.6).

**Acceptance:**

- 1.3.1 - Replacement is atomic: a failing insert leaves prior rows intact. test: `tests/providers/capabilities/test_store.py::test_failed_replace_retains_last_good_rows`.
- 1.3.2 - Provider rows exist without any OpenRouter `model_metadata` row. test: `tests/providers/capabilities/test_store.py::test_capability_rows_independent_of_model_metadata`.
- 1.3.3 - Source failure marks health without touching model rows. test: `tests/providers/capabilities/test_store.py::test_source_failure_updates_health_only`.

## P2: Provider collectors
`kind: framing`

**Goal**: One validated, complete snapshot per provider from its own sources;
registered by provider key; no core-resolver branches.

### 2.1 Collector protocol, registry, and snapshot validation [category: code] (depends: P1)
`kind: deliverable`

Targets:
- `src/gobby/providers/capabilities/collectors/__init__.py`
- `src/gobby/providers/capabilities/collectors/base.py`

`base.py`:

```python
class CapabilityCollector(Protocol):
    provider: str
    sources: tuple[SourceSpec, ...]      # source_key, url, required
    async def collect(self) -> ProviderSnapshot: ...
```

`register_collector(collector)` / `collectors()` registry keyed by provider.
Shared snapshot validation runs before any store write: non-empty models for
a successful snapshot, every route's activation descriptors validate (1.2),
selectors non-empty, `fast` routes only beside an existing `standard` route
or as explicitly source-declared fast-only entries, every fact carries
provenance. A snapshot failing validation is treated as a failed refresh
(3.1): prior rows retained, health `error`.

Extensibility proof: a fake provider registers a collector + a fake
activation handler and flows end-to-end without schema or resolver edits.

**Acceptance:**

- 2.1.1 - Registry dispatches by provider key. symbol: `register_collector`. file: `src/gobby/providers/capabilities/collectors/base.py`.
- 2.1.2 - Empty/malformed snapshots are rejected before write. test: `tests/providers/capabilities/test_collector_validation.py::test_empty_snapshot_rejected`.
- 2.1.3 - Fake provider + fake activation adapter integrate without editing schema or core resolver. test: `tests/providers/capabilities/test_extensibility.py::test_fake_provider_end_to_end`.

### 2.2 Claude collector [category: code] (depends: 2.1)
`kind: deliverable`

Targets:
- `src/gobby/providers/capabilities/collectors/claude.py`

Public docs only (no authenticated Models API). Required sources:

- `models-overview` → `https://platform.claude.com/docs/en/about-claude/models/overview.md`
  — canonical IDs (`Claude API ID` row), aliases, context window, max output,
  comparative latency (normalize Slower/Moderate/Fast/Fastest → lowercase),
  adaptive/extended thinking. Parse both the current-models table and the
  legacy accordion table (same columns).
- `model-config` → `https://code.claude.com/docs/en/model-config.md`
  — Claude Code alias table (`fable`, `opus`, `sonnet`, `haiku`, `sonnet[1m]`,
  `opus[1m]`, `opusplan`) and per-provider alias→version resolution; used to
  map aliases onto canonical rows (stored in `aliases`).
- `effort-docs` → `https://platform.claude.com/docs/en/build-with-claude/effort.md`
  (linked from the overview) — supported effort levels; source for
  `supported_efforts`/`default_effort`.

Emit `standard` routes with `selector = canonical_model`. Emit a `fast` route
only if a feed explicitly declares a programmatic fast selector/parameter —
none is currently published, so v1 Claude has no fast route and requests
resolve `fast_unavailable`. The native `/fast` session toggle is #19564
(`native_command`). Markdown-table parsing uses fixture-pinned parsers; a
layout change that breaks parsing fails the snapshot (health `error`), never
degrades silently.

**Acceptance:**

- 2.2.1 - Alias→canonical changes in fixtures propagate to rows. test: `tests/providers/capabilities/collectors/test_claude.py::test_alias_to_canonical_mapping`.
- 2.2.2 - Latency/context/output facts carry per-fact provenance with source URLs. test: `tests/providers/capabilities/collectors/test_claude.py::test_fact_provenance`.
- 2.2.3 - Layout drift in either required feed fails the snapshot. test: `tests/providers/capabilities/collectors/test_claude.py::test_malformed_table_fails_snapshot`.

### 2.3 Codex collector (local app-server) [category: code] (depends: 2.1)
`kind: deliverable`

Targets:
- `src/gobby/providers/capabilities/collectors/codex.py`

Consume the **local Codex app-server** model metadata (the existing
`CodexAppServerClient` transport in `src/gobby/adapters/codex_impl/client.py`
is the access path; the protocol shape is `ModelInfo` from
`codex-rs/protocol/src/openai_models.rs`). Facts: model IDs, display names,
`context_window`/`max_context_window`, reasoning efforts (map protocol
variants incl. `none`/`minimal`/`xhigh`/`max`/`ultra` verbatim into
`supported_efforts`), and service tiers. When `supports_fast_mode()`-style
tier data reports a `fast` tier, emit a `fast` route that **retains the
standard selector** and carries `request_parameter` activation
(`surface: "app-server"`) with the tier name from the source. Terminal CLI
spawns have no verified tier flag, so no `spawn-cli` activation is emitted —
the resolver reports `fast_unavailable` on that surface (surface-specific
activations doing their job, not a gap). Codex absent/not running → snapshot
failure, health `error`, last-good rows retained.

**Acceptance:**

- 2.3.1 - Same-selector fast tier becomes a fast route with request_parameter activation. test: `tests/providers/capabilities/collectors/test_codex.py::test_fast_tier_same_selector_route`.
- 2.3.2 - Protocol effort variants map into supported_efforts verbatim. test: `tests/providers/capabilities/collectors/test_codex.py::test_reasoning_effort_variants`.
- 2.3.3 - App-server unavailability fails the snapshot without dropping rows. test: `tests/providers/capabilities/collectors/test_codex.py::test_app_server_down_retains_last_good`.

### 2.4 Droid collector (Factory docs, multipliers, fast pairing) [category: code] (depends: 2.1)
`kind: deliverable`

Targets:
- `src/gobby/providers/capabilities/collectors/droid.py`

Port the existing `https://docs.factory.ai/models.md` table parse from
`src/gobby/servers/provider_models.py::_parse_droid_docs_models` — and stop
discarding the multiplier column: `| Label | model-id | 1× | efforts |` rows
yield `usage_multiplier` (`Decimal`, strip `×`). Reasoning efforts and
defaults come from the efforts cell (the droid `off`/`none`/`minimal` levels
the old static fallback lost). Context windows are absent from this source →
leave `context_length` NULL so resolution falls through to `model_metadata`
(the current `_DROID_PROVIDER_CATALOG_CONTEXT_LENGTHS` static table retires
with P3).

Fast pairing: pair a `fast` route onto a standard row only when the source
explicitly identifies the entry as Fast (label contains the standalone word
"Fast" / "Fast Mode") **and** a matching standard entry exists after
stripping the `-fast` suffix; the fast route's `selector` is the fast model
ID with `model_selector` activation (`surface: "spawn-cli"` and
`"tool-chat"`). Suffix alone is insufficient; unpaired Fast entries remain
standalone standard rows (fast-only entries like `glm-5.2-fast` stay
selectable as models). Note some Fast variants carry **higher** multipliers
(e.g. `gpt-5.5-fast` 5×) — record, never infer cheapness from "fast".

**Acceptance:**

- 2.4.1 - Usage multipliers parse into route rows. test: `tests/providers/capabilities/collectors/test_droid.py::test_usage_multiplier_parsed`.
- 2.4.2 - Explicit Fast + matching standard entry pairs a fast route; suffix-only does not. test: `tests/providers/capabilities/collectors/test_droid.py::test_fast_pairing_requires_explicit_label_and_standard_match`.
- 2.4.3 - Missing context stays NULL and resolves via model_metadata. test: `tests/providers/capabilities/collectors/test_droid.py::test_context_falls_back_to_model_metadata`.

### 2.5 Grok and Qwen collectors (local discovery) [category: code] (depends: 2.1)
`kind: deliverable`

Targets:
- `src/gobby/providers/capabilities/collectors/grok.py`
- `src/gobby/providers/capabilities/collectors/qwen.py`

Port the existing ACP/config discovery from
`src/gobby/servers/provider_models.py` (`_discover_grok_models`,
`_discover_qwen_models`, `_discover_acp_models`) into collectors. Emit
standard routes only unless discovery explicitly declares an accelerated
route; `grok-composer-2.5-fast` remains a standalone standard row (its own
model, no verified standard twin). Grok context windows come from discovery
(e.g. 200k/512k as today); Qwen facts are whatever discovery reports —
reasoning stays `unknown` (NULL efforts) when the source is silent, never a
fabricated empty set. CLI missing → snapshot failure, health `error`.

**Acceptance:**

- 2.5.1 - Grok/Qwen discovery yields standard-only routes. test: `tests/providers/capabilities/collectors/test_grok_qwen.py::test_standard_only_discovery`.
- 2.5.2 - Silent sources produce reasoning=unknown with NULL efforts. test: `tests/providers/capabilities/collectors/test_grok_qwen.py::test_unknown_reasoning_null_efforts`.

### 2.6 Bundled seed for remote-source providers [category: code] (depends: 1.3)
`kind: deliverable`

Targets:
- `src/gobby/providers/capabilities/seed.py`

Cold-start guard for the two remote-source providers only. `seed.py` holds a
minimal snapshot (claude: the four alias families with canonical IDs, efforts
low–max; droid: the current `DROID_MODEL_CATALOG` entries incl. fast
pairings) as data moved from today's static dicts. `apply_seed(store)` runs
at startup before the first refresh and writes a provider's seed **only when
`has_rows(provider)` is false**, with provenance `{source_key: "bundled"}`
and health `stale`. The first successful collector snapshot replaces it.
Never re-applied over live rows; not an authoritative source.

**Acceptance:**

- 2.6.1 - Seed applies only to empty providers and marks health stale. test: `tests/providers/capabilities/test_seed.py::test_seed_only_when_empty`.
- 2.6.2 - A successful refresh atomically replaces seed rows and clears stale. test: `tests/providers/capabilities/test_seed.py::test_refresh_replaces_seed`.

## P3: Refresh coordination and resolution
`kind: framing`

**Goal**: Nonblocking startup refresh, 24 h cadence, last-good retention; one
resolver for context, reasoning, and speed routes; old catalog retired.

### 3.1 Refresh coordinator and daemon wiring [category: code] (depends: P2)
`kind: deliverable`

Targets:
- `src/gobby/providers/capabilities/refresh.py`
- `src/gobby/runner_init/servers.py::init_servers`
- `src/gobby/runner_lifecycle_startup.py::_refresh_provider_model_catalog`
- `src/gobby/runner_lifecycle_periodic.py::*` — scope-reason: register the capability refresh loop beside the existing model-metadata loop and wire shutdown drain
- `src/gobby/app_context.py::ServiceContainer`

`refresh.py`: `CapabilityRefreshCoordinator` — on daemon startup apply seed
(2.6), serve stored rows immediately, then refresh all registered collectors
**concurrently** (unlike the metadata loop, refresh first, then sleep) and
every 24 h thereafter. Per provider: run collector → validate (2.1) →
`replace_provider_snapshot` only when **all required sources** parsed and
validated; any failure → `record_source_failure`, prior rows retained. Bounded
per-source timeout and attempt counters. Shutdown drains in-flight refreshes
(mirror `MODEL_METADATA_DRAIN_TIMEOUT_SECONDS` handling). `AppContext` gains
`provider_capability_service` (replacing `provider_model_catalog` in P3/P4
consumers); `runner_lifecycle_startup._refresh_provider_model_catalog` is
replaced by the coordinator kickoff.

**Acceptance:**

- 3.1.1 - Startup serves stored rows without blocking on refresh. test: `tests/providers/capabilities/test_refresh.py::test_startup_nonblocking`.
- 3.1.2 - Network failure / empty / malformed responses retain last-good rows and mark health stale or error. test: `tests/providers/capabilities/test_refresh.py::test_failed_refresh_retains_last_good`.
- 3.1.3 - Refresh reruns on the 24-hour schedule after an immediate first pass. test: `tests/providers/capabilities/test_refresh.py::test_schedule_immediate_then_daily`.
- 3.1.4 - Atomic replacement: readers never observe a half-written provider. test: `tests/providers/capabilities/test_refresh.py::test_atomic_snapshot_swap`.

### 3.2 Capability resolver and typed speed results [category: code] (depends: 3.1)
`kind: deliverable`

Targets:
- `src/gobby/providers/capabilities/resolve.py`

`CapabilityResolver(store, model_metadata_store)`:

- **Context precedence** (first hit wins, source recorded): explicit caller
  override → selected route override → provider-model edge
  (`provider_model_capabilities.context_length`) → OpenRouter
  `model_metadata` → typed unknown.
- **Reasoning**: tri-state per 1.1. `unsupported` or an effort outside a
  known set → typed rejection before dispatch. `unknown` + transport supports
  an effort argument → pass through, resolution marked `unverified` (this
  replaces the `unsupported_model` rejection residual from #19475).
- **Route resolution**: `resolve_route(provider, model, speed_mode, surface)`
  → typed result; never substitutes an alternate model:

```python
class SpeedStatus(StrEnum):
    STANDARD = "standard"            # standard requested
    FAST_CONFIGURED = "fast_configured"  # activation applied, no provider confirmation
    FAST_APPLIED = "fast_applied"    # provider confirmed accelerated execution
    FAST_UNAVAILABLE = "fast_unavailable"  # no usable fast route on this surface; fail before dispatch
    FAST_DEGRADED = "fast_degraded"  # provider fell back; output preserved

@dataclass(frozen=True)
class SpeedResolution:
    requested: SpeedMode
    effective: SpeedMode
    status: SpeedStatus
    selector: str
    activations: tuple[ActivationDescriptor, ...]  # filtered to surface, order preserved
    reason: str | None
```

Confirmation evidence contract: `fast_applied`/`fast_degraded` are upgraded
post-execution by the surface adapter (5.1) only from provider-reported
response metadata (echoed model/tier matching — droid/grok echo the model,
codex app-server echoes the tier); absent evidence stays `fast_configured`.

**Acceptance:**

- 3.2.1 - Context precedence resolves in the specified order with source tags. test: `tests/providers/capabilities/test_resolve.py::test_context_precedence_order`.
- 3.2.2 - Explicitly unsupported reasoning is rejected; unknown passes through as unverified. test: `tests/providers/capabilities/test_resolve.py::test_reasoning_tristate`.
- 3.2.3 - fast on a fast-route-less model/surface yields fast_unavailable before dispatch. test: `tests/providers/capabilities/test_resolve.py::test_fast_unavailable_pre_dispatch`.
- 3.2.4 - standard default; fast route resolution returns surface-filtered ordered activations. test: `tests/providers/capabilities/test_resolve.py::test_route_resolution_surface_filtering`.

### 3.3 Rewire spawn reasoning and retire the legacy catalog [category: code] (depends: 3.2)
`kind: deliverable`

Targets:
- `src/gobby/agents/reasoning.py::resolve_spawn_reasoning`
- `src/gobby/agents/reasoning.py::_get_provider_models`
- `src/gobby/agents/reasoning.py::_supported_efforts`
- `src/gobby/agents/provider_capabilities.py::*` — scope-reason: fallback capability data moves to the matrix; the module keeps only transport mechanics
- `src/gobby/servers/provider_models.py::*` — scope-reason: module retires; live-discovery/parse logic has moved into P2 collectors
- `src/gobby/servers/provider_models_grok.py::*` — scope-reason: static grok catalog retires into the grok collector/seed
- `src/gobby/llm/context_windows.py::*` — scope-reason: retire the droid static context-length table and route lookups through the resolver
- `src/gobby/ai/registry_builder.py::_feature_candidate_models_by_provider`

`resolve_spawn_reasoning` consults `CapabilityResolver` (keeping
`SpawnReasoningResolution`'s shape, plus `unverified` status): models absent
from the matrix no longer hard-reject. `agents/provider_capabilities.py`
keeps only transport mechanics (`reasoning_flag`, `sandbox`) — the
`fallback_reasoning_efforts` capability data moves to the matrix. Delete
`ProviderModelCatalog`, its JSON file cache, `_BASE_MODEL_CATALOG` (in 4.1),
grok static catalog, and the droid static context table; `DROID_MODEL_CATALOG`
data survives only inside `seed.py` (2.6). `AGY_MODELS` /
`GEMINI_FAMILY_MODELS` and every AGY path stay untouched.

**Acceptance:**

- 3.3.1 - Spawn reasoning resolves through the matrix; unknown model passes through unverified instead of unsupported_model. test: `tests/agents/test_reasoning.py::test_unknown_model_passes_through_unverified`.
- 3.3.2 - `ProviderModelCatalog` and the JSON cache are gone from src. behavior: "no references to provider-model-catalog.json or ProviderModelCatalog remain" in `src/gobby/`.
- 3.3.3 - Transport mechanics (reasoning flag styles, sandbox) still drive CLI argv construction. test: `tests/agents/spawners/test_command_builder.py::test_reasoning_flag_styles_unchanged`.

## P4: Read contract and consumers
`kind: framing`

**Goal**: `GET /api/providers/models` serves the matrix; every consumer moves
in the same phase.

### 4.1 Replace the /api/providers/models response [category: code] (depends: P3)
`kind: deliverable`

Targets:
- `src/gobby/servers/routes/providers.py::*` — scope-reason: response construction is rewritten around the capability service; the base model catalog dict and static merge helpers are deleted
- `tests/servers/routes/test_providers.py::*` — scope-reason: ~18 of 24 route tests assert the replaced payload

Replaced response (no compatibility shim). Per provider entry keeps the
provider-level metadata fields the frontend reads (`provider`,
`display_name`, `available`, `installed`, `deprecated`,
`deprecation_message`, `supports_web_chat`, `supports_agent_spawn`,
`unavailable_reason`, `execution_provider`, `provider_type`) and replaces the
model/source payload:

```json
{
  "providers": [{
    "provider": "droid",
    "...provider metadata fields...": "unchanged",
    "refresh": {
      "generation": 12,
      "sources": [{"source_key": "factory-docs", "state": "ok",
                    "last_success_at": "...", "last_error": null}]
    },
    "models": [{
      "canonical_model": "gpt-5.4",
      "display_name": "GPT-5.4",
      "aliases": [],
      "available": true, "hidden": false, "is_default": false,
      "context_length": {"value": 200000, "source": "registry"},
      "max_output_tokens": {"value": null, "source": "unknown"},
      "latency_class": null,
      "reasoning": {"status": "known", "supported_efforts": ["low","medium","high"],
                     "default_effort": "medium"},
      "input_modalities": ["text"], "supports_tools": true,
      "routes": {
        "standard": {"selector": "gpt-5.4", "available": true,
                      "usage_multiplier": "1", "activations": [
                        {"kind": "model_selector", "surface": "spawn-cli", "params": {}}]},
        "fast": {"selector": "gpt-5.4-fast", "available": true,
                  "usage_multiplier": "5", "activations": [
                    {"kind": "model_selector", "surface": "spawn-cli", "params": {}}]}
      },
      "provenance": {"usage_multiplier": {"source_key": "factory-docs",
                       "source_url": "https://docs.factory.ai/models.md",
                       "observed_at": "..."}}
    }]
  }]
}
```

AGY entries keep today's static construction (`AGY_MODELS` + availability
metadata) presented in the same envelope with
`refresh.sources: [{"source_key": "static", "state": "ok"}]`. Configured
endpoints and local generation groups compose exactly as today. Auth matrix
row (`auth_service.py`) unchanged.

**Acceptance:**

- 4.1.1 - Route serves matrix rows with refresh health, reasoning metadata, routes, multipliers, and provenance per model. test: `tests/servers/routes/test_providers.py::test_models_response_matrix_shape`.
- 4.1.2 - Cold start (seed rows) serves claude/droid with health stale; a never-refreshed local-source provider serves empty models with state pending, not an error. test: `tests/servers/routes/test_providers.py::test_cold_start_seed_and_pending`.
- 4.1.3 - AGY, configured endpoints, and local generation groups keep composing. test: `tests/servers/routes/test_providers.py::test_agy_and_endpoint_groups_unchanged`.

### 4.2 Frontend compatibility in providerModels.ts [category: code] (depends: 4.1)
`kind: deliverable`

Targets:
- `web/src/lib/providerModels.ts::fetchProviderModelCatalog`
- `web/src/lib/providerModels.ts::isProviderModelEntry`
- `web/src/lib/providerModels.ts::isProviderModelOption`

`providerModels.ts` is the single fetch/validation choke point; its runtime
guards silently drop unrecognized entries, so they must be rewritten for the
new shape. Map matrix models into the existing internal
`ProviderModelOption` shape (`value` = canonical_model, `label`,
`reasoning.{supported_efforts, default_effort}` from the reasoning object,
`context_length` from the typed fact, `input_modalities`, `hidden`) so the
~12 downstream components (`ProviderPicker`, `useChatInputProviderSelection`,
`useReasoningPreferences`, `AgentEditForm`, …) compile and behave unchanged.
Expose the new `routes` and `refresh` data as additional typed fields for
#19564 without building any UI. Update vitest harness mocks
(`chatPageTestSetup.tsx` etc.) to the new payload.

**Acceptance:**

- 4.2.1 - Catalog fetch validates and maps the new response; existing pickers render models and efforts. test: `web/src/lib/__tests__/providerModels.test.ts::maps_matrix_response`.
- 4.2.2 - Routes/refresh fields are typed and passed through untouched. test: `web/src/lib/__tests__/providerModels.test.ts::exposes_routes_and_refresh`.

### 4.3 Update gwiki daemon contract string [category: code] (depends: 4.1)
`kind: deliverable`

Targets:
- `crates/gwiki/src/daemon.rs::*` — scope-reason: the EndpointContract response_shape string literal for the Synthesis capability hard-codes the old providers-models response shape

Update the `response_shape` literal (`crates/gwiki/src/daemon.rs:116-125`) to
the 4.1 envelope. Load the `rust` skill before editing; rebuild and
**reinstall** `~/.gobby/bin/gwiki` (a committed change is not live until
reinstalled). `crates/gcore/src/ai/probe.rs` needs no change (its tests
assert the route is *not* called).

**Acceptance:**

- 4.3.1 - Contract string matches the new envelope and gwiki tests pass. test: `crates/gwiki/src/daemon.rs` unit tests via `cargo test -p gobby-wiki`.

### 4.4 Update provider/model documentation [category: docs] (depends: 4.1)
`kind: deliverable`

Targets:
- `docs/guides/http-endpoints.md`
- `docs/guides/providers-and-models.md`
- `docs/guides/web-ui.md`
- `docs/guides/gwiki-development-guide.md`
- `docs/guides/ai-daemon-contract.md`

Refresh the five guides that document the old response: new envelope, refresh
health semantics, speed routes/multipliers, provenance, `speed_mode` request
field and typed speed results (P5), and the collector/seed architecture.

**Acceptance:**

- 4.4.1 - All five guides describe the new contract with no references to the retired catalog/cache. behavior: "matrix response and speed_mode documented" in `docs/guides/providers-and-models.md`.

## P5: Execution contracts
`kind: framing`

**Goal**: `speed_mode` on Gobby-owned execution requests, resolved to an exact
route before dispatch, applied via surface adapters, reported as a typed
result.

### 5.1 Speed activation service and result reporting [category: code] (depends: 3.2)
`kind: deliverable`

Targets:
- `src/gobby/providers/capabilities/apply.py`

`apply.py`: surface-adapter layer translating a `SpeedResolution` into
concrete dispatch mutations, keyed by activation kind + surface:

- `model_selector` → replace the model argument with `route.selector`
  (spawn argv via `build_cli_command`'s existing `model` param; tool-chat via
  request model).
- `cli_config` → append codex `-c key=value` pairs through the existing
  `SpawnRequest.codex_config_overrides` tuple.
- `request_parameter` → set a named parameter on app-server / tool-chat
  request payloads.

Post-execution confirmation: `finalize_speed(resolution, response_metadata)`
upgrades `fast_configured` → `fast_applied` when provider-echoed model/tier
matches the fast route, or → `fast_degraded` (output preserved; `requested`,
`effective`, `reason` reported) when the echo shows fallback. The typed
result dict (`speed: {requested, effective, status, reason}`) is attached to
spawn results, chat stream completion metadata, and tool-chat responses.
`fast_unavailable` raises a typed pre-dispatch error — no dispatch, no
substitution.

**Acceptance:**

- 5.1.1 - Ordered activations apply per surface; model_selector swaps selector, cli_config extends codex overrides, request_parameter sets payload fields. test: `tests/providers/capabilities/test_apply.py::test_activation_application_per_surface`.
- 5.1.2 - Provider-confirmed fallback yields fast_degraded with preserved output and reason. test: `tests/providers/capabilities/test_apply.py::test_fast_degraded_upgrade`.
- 5.1.3 - fast_unavailable fails before dispatch with no model substitution. test: `tests/providers/capabilities/test_apply.py::test_fast_unavailable_no_dispatch`.

### 5.2 speed_mode on spawn surfaces (REST, MCP, dispatch) [category: code] (depends: 5.1)
`kind: deliverable`

Targets:
- `src/gobby/servers/routes/agent_spawn.py::AgentSpawnRequest`
- `src/gobby/mcp_proxy/tools/spawn_agent/_factory.py::*` — scope-reason: speed_mode threads through both spawn_agent and dispatch_batch tool signatures and per-suggestion coalescing
- `src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py::spawn_agent_impl`
- `src/gobby/agents/spawn_models.py::SpawnRequest`
- `src/gobby/agents/spawn_executor.py::execute_spawn`
- `src/gobby/cli/agents.py::spawn_agent_cmd`

Add `speed_mode: Literal["standard","fast"] = "standard"` to
`AgentSpawnRequest` (batch inherits via `spawns`), the `spawn_agent` /
`dispatch_batch` MCP tool signatures (per-suggestion override coalesced like
`reasoning_effort`), and `gobby agents spawn --fast`. In `spawn_agent_impl`,
resolve the route via 3.2 alongside `resolve_spawn_reasoning` (surface
`"spawn-cli"`), apply activations via 5.1, carry
`SpawnRequest.speed_resolution`, and include the typed speed result in the
spawn result payload. Request-scoped only: not added to
`LaunchDefaultsRequest`, agent definitions, or resume metadata (a resumed run
reverts to standard).

**Acceptance:**

- 5.2.1 - REST and MCP spawn accept speed_mode, default standard, and report the typed result. test: `tests/servers/routes/test_agent_spawn.py::test_spawn_speed_mode_contract`.
- 5.2.2 - Droid fast spawn launches with the fast selector via model_selector activation. test: `tests/mcp_proxy/tools/test_spawn_agent_speed.py::test_droid_fast_selector_spawn`.
- 5.2.3 - speed_mode is never persisted to launch defaults or resume metadata. test: `tests/mcp_proxy/tools/test_spawn_agent_speed.py::test_speed_mode_not_persisted`.

### 5.3 speed_mode on web chat and tool-chat [category: code] (depends: 5.1)
`kind: deliverable`

Targets:
- `src/gobby/servers/websocket/chat/_message_ingress.py::ChatMessageIngressMixin._handle_chat_message`
- `src/gobby/servers/websocket/chat/_streaming.py::*` — scope-reason: speed_mode threads per-send through the streaming pipeline beside reasoning_effort
- `src/gobby/servers/routes/llm.py::ChatCompletionsPayload`
- `src/gobby/ai/_tool_chat_contracts.py::ToolChatRequest`
- `src/gobby/ai/_tool_chat_service.py::*` — scope-reason: route resolution and activation application happen at candidate/binding selection

WebSocket `chat_message` gains optional `speed_mode` (per-send,
request-scoped, never stored on the chat session — each send without it is
standard). `ChatCompletionsPayload` (`extra="forbid"` — field must be added
explicitly) and frozen `ToolChatRequest` gain `speed_mode` with default
`standard`. Resolution before dispatch (surface `"app-server"` for the codex
web-chat/tool-chat backends, `"tool-chat"` otherwise); typed speed result in
the chat completion metadata and tool-chat response. `TextGeneratePayload`
and peer one-shot generation stay speed-less (no consumer; out of scope).

**Acceptance:**

- 5.3.1 - Per-send WS speed_mode applies for that turn only and resets to standard. test: `tests/servers/websocket/test_chat_speed_mode.py::test_per_send_not_sticky`.
- 5.3.2 - Codex tool-chat fast rides the request_parameter activation and reports fast_configured/fast_applied from tier echo. test: `tests/ai/test_tool_chat_speed.py::test_codex_tier_activation`.
- 5.3.3 - Unknown speed_mode values 422 on ChatCompletionsPayload. test: `tests/servers/routes/test_llm.py::test_chat_completions_rejects_bad_speed_mode`.

## D1 Non-contract speed surfaces (deferred to #19564)
`kind: deferred`

```yaml
deferral:
  task_ref: "#19564"
  reason: "User-facing speed controls (web per-send UI, /fast <message>, launch UI/CLI flags, native terminal hooks and provider-native commands, unavailable/degraded UX, cross-surface tests) are the dependent epic's scope; this plan delivers the schema, collectors, resolver, activation adapters, and wire contracts they consume."
  owner: "epic-19564"
  original_acceptance_items:
    - D1.1
```

#19564 already carries `deferred-from:19483:D1` provenance; reconcile that
label with the registered plan-id when this plan is registered. Also
explicitly out of this release: raw PTY interception, authenticated Claude
API enrichment, capability history, global speed state, and
backward-compatibility adapters. Native terminal commands retain each
provider's session-toggle semantics; Gobby-owned requests remain one-shot.

## V1 Verification
`kind: verification`

- Focused pytest per phase (`GOBBY_TEST_PROTECT=1 uv run pytest
  tests/providers/capabilities/ tests/storage/test_migration_contract.py
  tests/servers/routes/test_providers.py tests/agents/test_reasoning.py -v`),
  isolated test daemon state only; never the full suite.
- Scoped `uv run ruff check src/`, `uv run ruff format src/`,
  `uv run mypy src/`, and the test-types ratchet audit.
- Frontend: `web` vitest for `providerModels` mapping + updated harness
  mocks; live specs (`provider-picker-live`, `codex-model-switch-live`)
  against a dev daemon.
- Rust: `cargo test -p gobby-wiki`; rebuild + reinstall `gwiki`.
- End-to-end: start an isolated dev daemon → confirm cold-start seed rows,
  then a live refresh replacing them (health transitions
  stale→ok) → `GET /api/providers/models` shows matrix payload → spawn a
  droid agent with `speed_mode=fast` and verify the fast selector in argv and
  the typed result → request claude fast and verify pre-dispatch
  `fast_unavailable`.
- Registered-plan validation at execution time: `uv run gobby plans validate
  <plan-file>` after registering the artifact.

## M1 Task Manifest
`kind: manifest`

```yaml
- title: Add provider capability tables (capability-matrix migration + baseline)
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: '1.1.1: The capability-matrix migration creates the three tables
    and the baseline matches it. test: `tests/storage/test_migration_contract.py::test_provider_capability_tables_match_baseline`.

    1.1.2: Migration chain remains contiguous through the assigned slot. test: `tests/storage/test_migration_contract.py::test_postgres_migrations_preserve_known_post_baseline_sequence`.

    1.1.3: Routes cascade-delete with their capability row. test: `tests/storage/test_provider_capability_store.py::test_route_rows_cascade_on_capability_delete`.'
  labels:
  - covers:provider-capability-matrix:1.1:1.1.1
  - covers:provider-capability-matrix:1.1:1.1.2
  - covers:provider-capability-matrix:1.1:1.1.3
  tdd: true
  source_section: '1.1'
  implementation_domain: backend
- title: Typed capability domain and activation validation
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: '1.2.1: Typed domain round-trips all 1.1 fields incl. NULL-vs-empty
    efforts. test: `tests/providers/capabilities/test_models.py::test_supported_efforts_null_vs_empty_distinct`.

    1.2.2: Unknown activation kind rejected. test: `tests/providers/capabilities/test_activation.py::test_unknown_activation_kind_rejected`.

    1.2.3: Source-derived payloads cannot smuggle exec/env/path params. test: `tests/providers/capabilities/test_activation.py::test_activation_params_reject_non_string_and_disallowed_keys`.

    1.2.4: New handler kinds register without touching schema or resolver. test: `tests/providers/capabilities/test_activation.py::test_register_new_handler_kind`.'
  labels:
  - covers:provider-capability-matrix:1.2:1.2.1
  - covers:provider-capability-matrix:1.2:1.2.2
  - covers:provider-capability-matrix:1.2:1.2.3
  - covers:provider-capability-matrix:1.2:1.2.4
  tdd: true
  source_section: '1.2'
  implementation_domain: backend
- title: Capability store with atomic snapshot replacement
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  - '1.2'
  validation_criteria: '1.3.1: Replacement is atomic: a failing insert leaves prior
    rows intact. test: `tests/providers/capabilities/test_store.py::test_failed_replace_retains_last_good_rows`.

    1.3.2: Provider rows exist without any OpenRouter `model_metadata` row. test:
    `tests/providers/capabilities/test_store.py::test_capability_rows_independent_of_model_metadata`.

    1.3.3: Source failure marks health without touching model rows. test: `tests/providers/capabilities/test_store.py::test_source_failure_updates_health_only`.'
  labels:
  - covers:provider-capability-matrix:1.3:1.3.1
  - covers:provider-capability-matrix:1.3:1.3.2
  - covers:provider-capability-matrix:1.3:1.3.3
  tdd: true
  source_section: '1.3'
  implementation_domain: backend
- title: Collector protocol, registry, and snapshot validation
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  - '1.2'
  - '1.3'
  validation_criteria: '2.1.1: Registry dispatches by provider key. symbol: `register_collector`.
    file: `src/gobby/providers/capabilities/collectors/base.py`.

    2.1.2: Empty/malformed snapshots are rejected before write. test: `tests/providers/capabilities/test_collector_validation.py::test_empty_snapshot_rejected`.

    2.1.3: Fake provider + fake activation adapter integrate without editing schema
    or core resolver. test: `tests/providers/capabilities/test_extensibility.py::test_fake_provider_end_to_end`.'
  labels:
  - covers:provider-capability-matrix:2.1:2.1.1
  - covers:provider-capability-matrix:2.1:2.1.2
  - covers:provider-capability-matrix:2.1:2.1.3
  tdd: true
  source_section: '2.1'
  implementation_domain: backend
- title: Claude collector
  category: code
  task_type: feature
  depends_on:
  - '2.1'
  validation_criteria: "2.2.1: Alias\u2192canonical changes in fixtures propagate\
    \ to rows. test: `tests/providers/capabilities/collectors/test_claude.py::test_alias_to_canonical_mapping`.\n\
    2.2.2: Latency/context/output facts carry per-fact provenance with source URLs.\
    \ test: `tests/providers/capabilities/collectors/test_claude.py::test_fact_provenance`.\n\
    2.2.3: Layout drift in either required feed fails the snapshot. test: `tests/providers/capabilities/collectors/test_claude.py::test_malformed_table_fails_snapshot`."
  labels:
  - covers:provider-capability-matrix:2.2:2.2.1
  - covers:provider-capability-matrix:2.2:2.2.2
  - covers:provider-capability-matrix:2.2:2.2.3
  tdd: true
  source_section: '2.2'
  implementation_domain: backend
- title: Codex collector (local app-server)
  category: code
  task_type: feature
  depends_on:
  - '2.1'
  validation_criteria: '2.3.1: Same-selector fast tier becomes a fast route with request_parameter
    activation. test: `tests/providers/capabilities/collectors/test_codex.py::test_fast_tier_same_selector_route`.

    2.3.2: Protocol effort variants map into supported_efforts verbatim. test: `tests/providers/capabilities/collectors/test_codex.py::test_reasoning_effort_variants`.

    2.3.3: App-server unavailability fails the snapshot without dropping rows. test:
    `tests/providers/capabilities/collectors/test_codex.py::test_app_server_down_retains_last_good`.'
  labels:
  - covers:provider-capability-matrix:2.3:2.3.1
  - covers:provider-capability-matrix:2.3:2.3.2
  - covers:provider-capability-matrix:2.3:2.3.3
  tdd: true
  source_section: '2.3'
  implementation_domain: backend
- title: Droid collector (Factory docs, multipliers, fast pairing)
  category: code
  task_type: feature
  depends_on:
  - '2.1'
  validation_criteria: '2.4.1: Usage multipliers parse into route rows. test: `tests/providers/capabilities/collectors/test_droid.py::test_usage_multiplier_parsed`.

    2.4.2: Explicit Fast + matching standard entry pairs a fast route; suffix-only
    does not. test: `tests/providers/capabilities/collectors/test_droid.py::test_fast_pairing_requires_explicit_label_and_standard_match`.

    2.4.3: Missing context stays NULL and resolves via model_metadata. test: `tests/providers/capabilities/collectors/test_droid.py::test_context_falls_back_to_model_metadata`.'
  labels:
  - covers:provider-capability-matrix:2.4:2.4.1
  - covers:provider-capability-matrix:2.4:2.4.2
  - covers:provider-capability-matrix:2.4:2.4.3
  tdd: true
  source_section: '2.4'
  implementation_domain: backend
- title: Grok and Qwen collectors (local discovery)
  category: code
  task_type: feature
  depends_on:
  - '2.1'
  validation_criteria: '2.5.1: Grok/Qwen discovery yields standard-only routes. test:
    `tests/providers/capabilities/collectors/test_grok_qwen.py::test_standard_only_discovery`.

    2.5.2: Silent sources produce reasoning=unknown with NULL efforts. test: `tests/providers/capabilities/collectors/test_grok_qwen.py::test_unknown_reasoning_null_efforts`.'
  labels:
  - covers:provider-capability-matrix:2.5:2.5.1
  - covers:provider-capability-matrix:2.5:2.5.2
  tdd: true
  source_section: '2.5'
  implementation_domain: backend
- title: Bundled seed for remote-source providers
  category: code
  task_type: feature
  depends_on:
  - '1.3'
  validation_criteria: '2.6.1: Seed applies only to empty providers and marks health
    stale. test: `tests/providers/capabilities/test_seed.py::test_seed_only_when_empty`.

    2.6.2: A successful refresh atomically replaces seed rows and clears stale. test:
    `tests/providers/capabilities/test_seed.py::test_refresh_replaces_seed`.'
  labels:
  - covers:provider-capability-matrix:2.6:2.6.1
  - covers:provider-capability-matrix:2.6:2.6.2
  tdd: true
  source_section: '2.6'
  implementation_domain: backend
- title: Refresh coordinator and daemon wiring
  category: code
  task_type: feature
  depends_on:
  - '2.1'
  - '2.2'
  - '2.3'
  - '2.4'
  - '2.5'
  - '2.6'
  validation_criteria: '3.1.1: Startup serves stored rows without blocking on refresh.
    test: `tests/providers/capabilities/test_refresh.py::test_startup_nonblocking`.

    3.1.2: Network failure / empty / malformed responses retain last-good rows and
    mark health stale or error. test: `tests/providers/capabilities/test_refresh.py::test_failed_refresh_retains_last_good`.

    3.1.3: Refresh reruns on the 24-hour schedule after an immediate first pass. test:
    `tests/providers/capabilities/test_refresh.py::test_schedule_immediate_then_daily`.

    3.1.4: Atomic replacement: readers never observe a half-written provider. test:
    `tests/providers/capabilities/test_refresh.py::test_atomic_snapshot_swap`.'
  labels:
  - covers:provider-capability-matrix:3.1:3.1.1
  - covers:provider-capability-matrix:3.1:3.1.2
  - covers:provider-capability-matrix:3.1:3.1.3
  - covers:provider-capability-matrix:3.1:3.1.4
  tdd: true
  source_section: '3.1'
  implementation_domain: backend
- title: Capability resolver and typed speed results
  category: code
  task_type: feature
  depends_on:
  - '3.1'
  validation_criteria: '3.2.1: Context precedence resolves in the specified order
    with source tags. test: `tests/providers/capabilities/test_resolve.py::test_context_precedence_order`.

    3.2.2: Explicitly unsupported reasoning is rejected; unknown passes through as
    unverified. test: `tests/providers/capabilities/test_resolve.py::test_reasoning_tristate`.

    3.2.3: fast on a fast-route-less model/surface yields fast_unavailable before
    dispatch. test: `tests/providers/capabilities/test_resolve.py::test_fast_unavailable_pre_dispatch`.

    3.2.4: standard default; fast route resolution returns surface-filtered ordered
    activations. test: `tests/providers/capabilities/test_resolve.py::test_route_resolution_surface_filtering`.'
  labels:
  - covers:provider-capability-matrix:3.2:3.2.1
  - covers:provider-capability-matrix:3.2:3.2.2
  - covers:provider-capability-matrix:3.2:3.2.3
  - covers:provider-capability-matrix:3.2:3.2.4
  tdd: true
  source_section: '3.2'
  implementation_domain: backend
- title: Rewire spawn reasoning and retire the legacy catalog
  category: code
  task_type: feature
  depends_on:
  - '3.2'
  validation_criteria: '3.3.1: Spawn reasoning resolves through the matrix; unknown
    model passes through unverified instead of unsupported_model. test: `tests/agents/test_reasoning.py::test_unknown_model_passes_through_unverified`.

    3.3.2: `ProviderModelCatalog` and the JSON cache are gone from src. behavior:
    "no references to provider-model-catalog.json or ProviderModelCatalog remain"
    in `src/gobby/`.

    3.3.3: Transport mechanics (reasoning flag styles, sandbox) still drive CLI argv
    construction. test: `tests/agents/spawners/test_command_builder.py::test_reasoning_flag_styles_unchanged`.'
  labels:
  - covers:provider-capability-matrix:3.3:3.3.1
  - covers:provider-capability-matrix:3.3:3.3.2
  - covers:provider-capability-matrix:3.3:3.3.3
  tdd: true
  source_section: '3.3'
  implementation_domain: backend
- title: Replace the /api/providers/models response
  category: code
  task_type: feature
  depends_on:
  - '3.1'
  - '3.2'
  - '3.3'
  validation_criteria: '4.1.1: Route serves matrix rows with refresh health, reasoning
    metadata, routes, multipliers, and provenance per model. test: `tests/servers/routes/test_providers.py::test_models_response_matrix_shape`.

    4.1.2: Cold start (seed rows) serves claude/droid with health stale; a never-refreshed
    local-source provider serves empty models with state pending, not an error. test:
    `tests/servers/routes/test_providers.py::test_cold_start_seed_and_pending`.

    4.1.3: AGY, configured endpoints, and local generation groups keep composing.
    test: `tests/servers/routes/test_providers.py::test_agy_and_endpoint_groups_unchanged`.'
  labels:
  - covers:provider-capability-matrix:4.1:4.1.1
  - covers:provider-capability-matrix:4.1:4.1.2
  - covers:provider-capability-matrix:4.1:4.1.3
  tdd: true
  source_section: '4.1'
  implementation_domain: backend
- title: Frontend compatibility in providerModels.ts
  category: code
  task_type: feature
  depends_on:
  - '4.1'
  validation_criteria: '4.2.1: Catalog fetch validates and maps the new response;
    existing pickers render models and efforts. test: `web/src/lib/__tests__/providerModels.test.ts::maps_matrix_response`.

    4.2.2: Routes/refresh fields are typed and passed through untouched. test: `web/src/lib/__tests__/providerModels.test.ts::exposes_routes_and_refresh`.'
  labels:
  - covers:provider-capability-matrix:4.2:4.2.1
  - covers:provider-capability-matrix:4.2:4.2.2
  tdd: true
  source_section: '4.2'
  implementation_domain: frontend
- title: Update gwiki daemon contract string
  category: code
  task_type: feature
  depends_on:
  - '4.1'
  validation_criteria: '4.3.1: Contract string matches the new envelope and gwiki
    tests pass. test: `crates/gwiki/src/daemon.rs` unit tests via `cargo test -p gobby-wiki`.'
  labels:
  - covers:provider-capability-matrix:4.3:4.3.1
  tdd: true
  source_section: '4.3'
  implementation_domain: backend
- title: Update provider/model documentation
  category: docs
  task_type: feature
  depends_on:
  - '4.1'
  validation_criteria: '4.4.1: All five guides describe the new contract with no references
    to the retired catalog/cache. behavior: "matrix response and speed_mode documented"
    in `docs/guides/providers-and-models.md`.'
  labels:
  - covers:provider-capability-matrix:4.4:4.4.1
  tdd: false
  source_section: '4.4'
  assigned_agent: tech-writer
- title: Speed activation service and result reporting
  category: code
  task_type: feature
  depends_on:
  - '3.2'
  validation_criteria: '5.1.1: Ordered activations apply per surface; model_selector
    swaps selector, cli_config extends codex overrides, request_parameter sets payload
    fields. test: `tests/providers/capabilities/test_apply.py::test_activation_application_per_surface`.

    5.1.2: Provider-confirmed fallback yields fast_degraded with preserved output
    and reason. test: `tests/providers/capabilities/test_apply.py::test_fast_degraded_upgrade`.

    5.1.3: fast_unavailable fails before dispatch with no model substitution. test:
    `tests/providers/capabilities/test_apply.py::test_fast_unavailable_no_dispatch`.'
  labels:
  - covers:provider-capability-matrix:5.1:5.1.1
  - covers:provider-capability-matrix:5.1:5.1.2
  - covers:provider-capability-matrix:5.1:5.1.3
  tdd: true
  source_section: '5.1'
  implementation_domain: backend
- title: speed_mode on spawn surfaces (REST, MCP, dispatch)
  category: code
  task_type: feature
  depends_on:
  - '5.1'
  validation_criteria: '5.2.1: REST and MCP spawn accept speed_mode, default standard,
    and report the typed result. test: `tests/servers/routes/test_agent_spawn.py::test_spawn_speed_mode_contract`.

    5.2.2: Droid fast spawn launches with the fast selector via model_selector activation.
    test: `tests/mcp_proxy/tools/test_spawn_agent_speed.py::test_droid_fast_selector_spawn`.

    5.2.3: speed_mode is never persisted to launch defaults or resume metadata. test:
    `tests/mcp_proxy/tools/test_spawn_agent_speed.py::test_speed_mode_not_persisted`.'
  labels:
  - covers:provider-capability-matrix:5.2:5.2.1
  - covers:provider-capability-matrix:5.2:5.2.2
  - covers:provider-capability-matrix:5.2:5.2.3
  tdd: true
  source_section: '5.2'
  implementation_domain: backend
- title: speed_mode on web chat and tool-chat
  category: code
  task_type: feature
  depends_on:
  - '5.1'
  validation_criteria: '5.3.1: Per-send WS speed_mode applies for that turn only and
    resets to standard. test: `tests/servers/websocket/test_chat_speed_mode.py::test_per_send_not_sticky`.

    5.3.2: Codex tool-chat fast rides the request_parameter activation and reports
    fast_configured/fast_applied from tier echo. test: `tests/ai/test_tool_chat_speed.py::test_codex_tier_activation`.

    5.3.3: Unknown speed_mode values 422 on ChatCompletionsPayload. test: `tests/servers/routes/test_llm.py::test_chat_completions_rejects_bad_speed_mode`.'
  labels:
  - covers:provider-capability-matrix:5.3:5.3.1
  - covers:provider-capability-matrix:5.3:5.3.2
  - covers:provider-capability-matrix:5.3:5.3.3
  tdd: true
  source_section: '5.3'
  implementation_domain: backend
```
