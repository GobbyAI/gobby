# Local Model Context Discovery

## Overview
`kind: framing`

**Plan ID:** `local-model-context-discovery`

**Root epic:** #19653.
Discover authoritative local context limits, retain their endpoint provenance,
and pass refreshed observations to existing synchronous context consumers.
Preserve #19653 and its original description and validation criteria verbatim.
The expansion compiler creates three phase sub-epics and eleven implementation
leaves under that root; it never creates a replacement root.

## Constraints
`kind: framing`

Work in an isolated worktree from local `0.5.0`. No full pytest suite, live model
loading, lifecycle changes, embedding migration, role configuration, new coding
restrictions, or new UI. Mock network endpoints and isolate test storage.
All leaves are backend code, feature tasks, with focused tests in the same leaf;
`implementation_domain: backend`, `tdd: false` for every routing decision.

An observation is scoped by machine, endpoint configuration fingerprint, exact
model ID and optional instance/digest. Local routes never consult OpenRouter.
Remote resolution and coverage auditing retain their current behavior. Each
request boundary refreshes metadata asynchronously, then passes the resulting
observation to synchronous resolution. Failure is a new unknown observation,
never permission to reuse a successful stale value.

Provider contracts verified against https://lmstudio.ai/docs/developer/rest/list,
https://docs.ollama.com/api/ps and the Ollama show-model-details API. LM Studio
architecture context is `max_context_length`; runtime context belongs to each
`loaded_instances[].config.context_length`. Ollama architecture uses the exact
`general.architecture` namespace in `/api/show` model_info; running context comes
from `/api/ps`. vLLM serving context is `max_model_len`. Generic loopback metadata
accepts exposed serving `context_length`, `context_window`, or `max_model_len`;
it never infers an architecture/runtime relationship from provider guesses.

Consumer sweep at local `0.5.0`: `gcode grep -w
'resolve_context_window|resolve_context_window_with_source|CapabilityResolver|excluded_providers'
src/gobby -m 150` located context resolution in capabilities/resolve.py,
llm/context_windows.py, routes/providers.py, chat_session_messages.py,
websocket/chat/backends/base.py and sessions/context_usage.py. Reasoning-only
consumers in agents/reasoning.py and ai/_reasoning.py remain unaffected.
`gcode grep -w 'ensure_local_model' src/gobby -l -m 20` located local_model.py,
resume_executor.py, spawn_agent/_generation_endpoint.py, chat_session.py and
websocket/chat/backends/codex.py. All context-consuming paths are assigned below.
Read-only model-ID consumers embedding_switch_service.py and cli/installers/embedding.py
retain the existing ID projection; their behavior is regression-checked by 1.4.
Existing files targeted below are under 850 lines except where a split is
explicitly assigned. All production files must remain below 1,000 lines.

## P1: Discovery
`kind: framing`

### 1.1 Define observations and effective-limit calculation [category: code]
`kind: deliverable`

Targets:
- `src/gobby/providers/capabilities/local_context.py`
- `tests/providers/capabilities/test_local_context.py`

Create immutable `LocalContextObservation` and `LocalContextInstance` dataclasses.
Observation fields: machine_id, endpoint_id, configuration_fingerprint, provider,
model_id, instance_id, digest, canonical_limit, runtime_limit, effective_limit,
observed_at (UTC), provenance (field-to-source paths), diagnostics (typed strings),
and instances (the complete eligible instance evidence). Expose strict
`positive_limit(value: object) -> int | None`, an observation builder, and
`effective_local_limit(observation, *overrides) -> int | None`.
Accept positive integers and ASCII decimal strings within PostgreSQL int32;
reject bool, floats, zero, negative, malformed and overflowing values.
Runtime evidence is mandatory. Canonical evidence is mandatory for native
LM Studio/Ollama and optional for serving-only vLLM/generic metadata. Reduce
applicable valid limits by minimum. Every eligible instance must have required
evidence; one missing instance limit makes aggregate context unknown. An explicit
instance ID restricts eligibility to that exact instance; missing selection is
unknown. Contradictory valid hard limits reduce conservatively with a diagnostic;
identity contradictions or malformed required evidence make the result unknown.
Overrides only reduce a verified value. Preserve raw limits and source paths.
Include lossless JSON-compatible serialization and strict deserialization.

**Acceptance:**
- 1.1.1 - Canonical 262144 and runtime 32768 resolve to 32768; lower overrides reduce it, larger overrides cannot increase it. test: `tests/providers/capabilities/test_local_context.py`.
- 1.1.2 - Multiple instances use the minimum, explicit selection uses that instance, and any missing required evidence yields unknown even with an override. test: `tests/providers/capabilities/test_local_context.py`.
- 1.1.3 - Invalid values, conflicting identities, contradictory limits and round-trip provenance are covered. test: `tests/providers/capabilities/test_local_context.py`.

### 1.2 Discover LM Studio context [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `src/gobby/providers/capabilities/local_context_lmstudio.py`
- `tests/providers/capabilities/test_local_context_lmstudio.py`

Create async `discover_lmstudio_context(client, identity, model_id, instance_id=None)`
using GET `/api/v1/models` with the configured credential and bounded timeout.
Parse only matching LLM entries, preserving canonical key and loaded-instance IDs.
Use max_context_length and loaded_instances config.context_length with 1.1's
builder. Exact model and instance matching must not collapse quantization variants.
Return unknown for unloaded, absent, ambiguous, malformed or unreachable models;
do not load a model. Preserve model digest/revision when exposed. Include every
eligible duplicate catalog entry rather than taking the first. HTTP/JSON failures
retain sanitized diagnostics and endpoint identity. Do not change discovery IDs.

**Acceptance:**
- 1.2.1 - Mock native metadata proves architecture/runtime distinction, loaded and unloaded cases, multiple instances and explicit instance selection. test: `tests/providers/capabilities/test_local_context_lmstudio.py`.
- 1.2.2 - Mixed embedding/LLM catalogs, duplicate identities, invalid values, contradictory metadata and endpoint failure retain independent typed observations. test: `tests/providers/capabilities/test_local_context_lmstudio.py`.

### 1.3 Discover Ollama context [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `src/gobby/providers/capabilities/local_context_ollama.py`
- `tests/providers/capabilities/test_local_context_ollama.py`

Create async `discover_ollama_context(client, identity, model_id)` using POST
`/api/show` with model and GET `/api/ps`. These are metadata-only requests.
Read canonical context from model_info's exact general.architecture namespace;
never choose an unrelated architecture's context key. Read runtime context from
every matching running record. Match exact names with only Ollama's documented
omitted `:latest` equivalence, and verify digests when both sides expose them.
Retain digest and per-field provenance. A show-only model, missing running
context, digest mismatch, failed endpoint or malformed metadata remains unknown.
Use 1.1's minimum rule; never use Modelfile num_ctx as proof of running context.

**Acceptance:**
- 1.3.1 - Mock show/ps metadata yields 32768 for 262144 canonical context and preserves digest and both source paths. test: `tests/providers/capabilities/test_local_context_ollama.py`.
- 1.3.2 - Multiple running records, missing ps evidence, unrelated architecture fields, digest changes and invalid values fail conservatively. test: `tests/providers/capabilities/test_local_context_ollama.py`.

### 1.4 Preserve vLLM serving limits and ID projections [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `src/gobby/providers/capabilities/local_context_vllm.py`
- `src/gobby/agents/local_model.py::*` — scope-reason: share metadata parsing between served-record retrieval and existing synchronous/asynchronous ID projections
- `tests/providers/capabilities/test_local_context_vllm.py`
- `tests/agents/test_local_model.py::*` — scope-reason: verify record parsing and all existing ID selection/projection behavior
- `tests/ai/test_embedding_switch_daemon_lifecycle.py::*` — scope-reason: regression-check embedding discovery's unchanged ID projection

Create async `discover_vllm_context(client, identity, model_id)` and a pure served
record parser using the existing vllm_models_url normalization. Retain max_model_len
as a verified serving/runtime limit. Share that parser with `_vllm_served_model_ids`
and `vllm_served_model_ids`, retaining their list[str] return and current ordering,
deduplication and auto-selection errors. Preserve all raw serving records for
conservative duplicate handling. Missing max_model_len is unknown; no model-card
or OpenRouter fallback. Do not change server startup, health or model loading.

**Acceptance:**
- 1.4.1 - max_model_len survives parsing and positive-value validation, with unknown for missing/invalid serving metadata. test: `tests/providers/capabilities/test_local_context_vllm.py`.
- 1.4.2 - Existing list[str] projections, explicit/auto model selection, embeddings consumers and malformed catalog behavior remain unchanged. test: `tests/agents/test_local_model.py`.

### 1.5 Discover generic loopback context [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `src/gobby/providers/capabilities/local_context_generic.py`
- `tests/providers/capabilities/test_local_context_generic.py`

Create async `discover_generic_context(client, identity, model_id)` for configured
loopback OpenAI-compatible endpoints, including Responses endpoints whose model
metadata is exposed at their existing models URL. Treat explicit context_length,
context_window and max_model_len as serving limits only. Minimum all supplied
valid applicable fields and records; a supplied malformed field is unusable
evidence, not permission to select another larger field. An architecture-only
max_context_length never proves serving context. Unknown fields are ignored.
Support localhost, 127.0.0.0/8 and IPv6 loopback with URL parsing; no DNS guessing.
Generic remote endpoints remain on the existing remote route.

**Acceptance:**
- 1.5.1 - Mock loopback serving metadata yields a positive conservative limit without guessed provider semantics. test: `tests/providers/capabilities/test_local_context_generic.py`.
- 1.5.2 - Missing metadata, invalid values, non-loopback URLs and mixed model catalogs cannot fabricate local limits. test: `tests/providers/capabilities/test_local_context_generic.py`.

## P2: Capability integration
`kind: framing`

### 2.1 Persist endpoint-scoped observations and provenance [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `src/gobby/providers/capabilities/local_context_store.py`
- `src/gobby/providers/capabilities/models.py::*` — scope-reason: extend typed provenance data, serialization and deserialization together with optional structured local observation evidence
- `src/gobby/providers/capabilities/store.py::*` — scope-reason: preserve structured local evidence through the existing JSON provenance transaction and hydration helpers
- `tests/providers/capabilities/test_local_context_store.py`
- `tests/providers/capabilities/test_providers_capabilities_models.py::*` — scope-reason: prove provenance round trips alongside unchanged remote models
- `tests/providers/capabilities/test_store.py::*` — scope-reason: verify existing provider snapshot persistence with extended evidence

Implement `LocalContextStore` on the existing provider capability snapshot store.
Use a reserved local provider namespace keyed by machine ID and a hash of endpoint
identity/configuration. Preserve the endpoint name and sanitized base URL inside
the observation. Store each model/instance as an exact canonical identity; attach
the full serialized observation as optional typed evidence in the context fact's
JSON provenance. Extend FactProvenance's typed JSON contract, not a new SQL table or
an overloaded error string. Remote facts omit local evidence. Credential changes
affect the fingerprint; raw credentials never persist. Whole endpoint snapshots
replace atomically so removed models disappear. Persist unknown observations too.

**Acceptance:**
- 2.1.1 - Isolated storage round-trips all limits, instance/digest identity, timestamp, provenance and unknown diagnostics. test: `tests/providers/capabilities/test_local_context_store.py`.
- 2.1.2 - Identical model names on different machines/endpoints and local/remote routes never collide; replacement removes stale identities without changing other snapshots. test: `tests/providers/capabilities/test_local_context_store.py`.

### 2.2 Refresh observations from endpoint and CLI configuration [category: code] (depends: 1.2, 1.3, 1.4, 1.5, 2.1)
`kind: deliverable`

Targets:
- `src/gobby/providers/capabilities/local_context_refresh.py`
- `src/gobby/providers/capabilities/local_context_config.py`
- `tests/providers/capabilities/test_local_context_refresh.py`
- `tests/providers/capabilities/test_local_context_config.py`

Implement one async `LocalContextService.refresh(route)` dispatching to completed
provider collectors and persisting through 2.1. The route carries exact machine,
endpoint, selected model/instance, protocol and configuration fingerprint. Read
existing ai.generation.endpoints and existing CLI endpoint settings via their
current loaders, including Codex active model_provider, Claude Anthropic base URL,
Qwen selected OpenAI entry, and configured Grok/Droid local endpoint routes.
Do not add config fields or infer a provider solely from model names/ports.
Match a CLI endpoint to a configured endpoint by normalized URL/credentials when
available; otherwise use generic serving-only discovery. Preserve local/remote
classification even when discovery fails.

Coalesce overlapping refreshes for identical endpoint/configuration/model
selection with one in-flight asyncio task; different configurations never join.
Shield the shared task from individual caller cancellation. At refresh start,
invalidate the old observation and advance a per-endpoint generation token when
configuration/model identity changes. Before publication compare that token and
fingerprint; superseded results are discarded and return unknown to their callers.
Network I/O uses AsyncClient; offload synchronous persistence from the event loop.
No success TTL substitutes for a refresh at a generation/setup boundary. Failure
publishes unknown and a later success recovers. Process restart reloads provenance
but still requires fresh evidence before use.

**Granularity:** Configuration normalization supplies refresh identity; it has no
separate lifecycle. This leaf owns the single refresh/publication state machine.

**Acceptance:**
- 2.2.1 - Existing endpoint and CLI settings produce endpoint-scoped routes, distinguish local/remote models and redact credentials. test: `tests/providers/capabilities/test_local_context_config.py`.
- 2.2.2 - Endpoint failure/recovery, concurrent callers, waiter cancellation and event-loop responsiveness are covered with mocked I/O. test: `tests/providers/capabilities/test_local_context_refresh.py`.
- 2.2.3 - Configuration or model/instance changes during refresh discard stale results and invalidate old observations. test: `tests/providers/capabilities/test_local_context_refresh.py`.

### 2.3 Resolve local context without remote fallback [category: code] (depends: 2.1)
`kind: deliverable`

Targets:
- `src/gobby/providers/capabilities/resolve.py::*` — scope-reason: extend context result/source and local resolution entry while preserving remote and reasoning paths
- `src/gobby/llm/context_windows.py::*` — scope-reason: thread observations through public resolution wrappers, source mapping and local warning/marker handling
- `tests/providers/capabilities/test_resolve.py::*` — scope-reason: compare all local and remote precedence branches
- `tests/llm/test_context_window.py::*` — scope-reason: verify public wrappers, warnings, overrides and marker semantics
- `tests/providers/capabilities/collectors/test_providers_capabilities_collectors_droid.py::*` — scope-reason: retain existing remote resolver integration
- `tests/servers/routes/test_servers_routes_providers.py::*` — scope-reason: preserve remote capability projection behavior

Add explicit local route/observation input to CapabilityResolver.resolve_context
and the resolve_context_window wrappers. A known local route with absent, failed,
mismatched or superseded observation returns typed unknown without reading
model_metadata or aliases. An observation must match the passed route identity.
Use effective_local_limit for manual and route caps, preserving source provenance.
Add a local observation source mapping. Do not apply the remote model-name context
marker floor to local results. Suppress local unknown coverage/context warnings;
retain diagnostics on the observation. Remote callers preserve caller override,
route override, provider matrix, OpenRouter and alias precedence exactly.
Keep all I/O outside these synchronous methods.

**Acceptance:**
- 2.3.1 - Local verified limits clamp overrides and local unknowns never touch OpenRouter, aliases or model-marker floors. test: `tests/providers/capabilities/test_resolve.py`.
- 2.3.2 - Public context wrappers preserve remote precedence/warnings and return local unknown quietly with diagnostics available. test: `tests/llm/test_context_window.py`.

### 2.4 Replace provider exclusions with local-model exclusions [category: code] (depends: 2.1, 2.2)
`kind: deliverable`

Targets:
- `src/gobby/providers/capabilities/coverage.py::*` — scope-reason: replace provider-wide exclusion callback with model-scoped route exclusions throughout the auditor
- `src/gobby/runner_init/servers.py::*` — scope-reason: wire shared local observation service and replace startup exclusion derivation using normalized CLI routes
- `tests/providers/capabilities/test_providers_capabilities_refresh.py::*` — scope-reason: verify auditing of mixed local and remote catalogs and startup wiring
- `tests/providers/capabilities/test_local_context_coverage.py`

Replace excluded_providers with an exact (provider, model identity) exclusion
callback. Derive only known local model routes using 2.2 normalization; reserved
local snapshot namespaces are locally scoped by construction. One local CLI
selection must not suppress the provider's remaining remote catalog. A local
unknown remains excluded, independent of collector success; remote missing metadata
and missing alias targets still warn exactly as before. Wire the shared service
once into existing daemon capability construction without adding a lifecycle owner.

**Acceptance:**
- 2.4.1 - Mixed local/remote entries under one CLI provider exclude only local routes, including unknowns. test: `tests/providers/capabilities/test_local_context_coverage.py`.
- 2.4.2 - Remote missing metadata/alias warnings and recovery remain intact after local endpoint failure and recovery. test: `tests/providers/capabilities/test_providers_capabilities_refresh.py`.

## P3: Consumer integration
`kind: framing`

### 3.1 Integrate generation and chat consumers [category: code] (depends: 2.2, 2.3, 2.4)
`kind: deliverable`

Targets:
- `src/gobby/servers/local_provider_models.py::*` — scope-reason: project verified observations into LM Studio/Ollama/vLLM/generic model entries and preserve eligibility/default/ID contracts
- `src/gobby/llm/local.py::*` — scope-reason: refresh context at local generation request boundaries and carry it through existing execution requests
- `src/gobby/servers/websocket/chat/runtime_manager.py::*` — scope-reason: attach refreshed route context during session creation and endpoint selection
- `src/gobby/servers/websocket/chat/backends/base.py::*` — scope-reason: carry route/observation through shared session context resolution and model changes
- `src/gobby/servers/chat_session_messages.py::*` — scope-reason: use session-local observations in history and message context projections
- `src/gobby/servers/routes/providers.py::*` — scope-reason: expose stored local effective limits/provenance in existing provider catalog projections
- `tests/servers/test_local_provider_models.py::*` — scope-reason: verify all provider context projections and unchanged discovery behavior
- `tests/servers/test_local_llm.py::*` — scope-reason: verify generation refresh precedes execution
- `tests/servers/test_local_context_consumers.py`
- `tests/servers/routes/test_servers_routes_providers.py::*` — scope-reason: verify local versus remote capability response projections

Connect the completed refresh/resolution contracts at generation and chat setup
boundaries. Refresh before each text/JSON generation and before creating or
switching a local chat session; shared base resolution consumes its route and
observation without network calls. Preserve the public discovery function signature,
group/model IDs, labels, default fallback, modalities and eligibility behavior.
Attach serialized observation alongside existing entries and derive context_length
only from verified effective_limit. Failed refresh must not reuse catalog,
provider-reported or cached context as authoritative local evidence. Generation
continues under existing behavior with unknown context; add no eligibility policy.
Remote generation/chat setup must not incur local endpoint probing.

**Granularity:** This leaf is the generation/chat boundary integration of completed
contracts; collectors, storage, refresh races and resolution are already closeable
and owned by earlier leaves. Six production files carry that one dataflow.

**Acceptance:**
- 3.1.1 - Generation and chat setup await refresh and consume the new effective value or unknown after failure; remote routes stay unchanged. test: `tests/servers/test_local_context_consumers.py`.
- 3.1.2 - Catalog IDs/defaults/modalities/eligibility and existing group consumers are unchanged while context and provenance use observations. test: `tests/servers/test_local_provider_models.py`.
- 3.1.3 - Model switches and identical names at separate endpoints never inherit previous context in chat/history projections. test: `tests/servers/test_local_context_consumers.py`.

### 3.2 Integrate coding-session context consumers [category: code] (depends: 2.2, 2.3, 3.1)
`kind: deliverable`

Targets:
- `src/gobby/agents/local_model.py::*` — scope-reason: refresh after existing model selection/setup and expose its observation without changing lifecycle behavior
- `src/gobby/mcp_proxy/tools/spawn_agent/_generation_endpoint.py::*` — scope-reason: pass selected endpoint identity and context into existing spawn setup
- `src/gobby/agents/resume_executor.py::*` — scope-reason: refresh local route context before resume setup and retain the observation
- `src/gobby/servers/chat_session.py::*` — scope-reason: carry refreshed local identity into existing coding chat session setup
- `src/gobby/servers/websocket/chat/backends/codex.py::*` — scope-reason: preserve refreshed local observations at Codex session setup and model switching
- `src/gobby/sessions/context_usage.py::*` — scope-reason: consume persisted session-local observations before reported/catalog context and prevent remote fallback
- `tests/agents/test_local_model.py::*` — scope-reason: verify setup refresh order and existing local model behavior
- `tests/sessions/test_context_usage.py::*` — scope-reason: verify all session context sources with local/remote isolation
- `tests/servers/test_session_control.py::*` — scope-reason: preserve existing control/history consumers
- `tests/agents/test_local_context_setup.py`

Refresh the selected local route after existing setup/model selection and before
coding spawn/resume/session activation. Persist the route identity and serialized
observation in existing session metadata/variables so synchronous context_usage
can resolve it. A local flag with missing evidence returns unknown; it never uses
a remote same-name model, token event, manual override or registry as replacement
for missing runtime evidence. Verified reported limits may only reduce the fresh
local observation. Existing remote session event precedence stays unchanged.
Model/endpoint changes invalidate the prior session observation. Wire every
existing ensure_local_model caller and test the public setup paths; add no loading,
unloading, role config, 65536-token gate or lean coding profile.

**Granularity:** This leaf owns coding setup's transfer of already verified context
into session metadata. Existing spawn/resume lifecycle ownership does not change.

**Acceptance:**
- 3.2.1 - Spawn, resume and coding chat setup refresh the selected route before activation and persist matching provenance. test: `tests/agents/test_local_context_setup.py`.
- 3.2.2 - Local session context clamps reported/override values, stays unknown without runtime evidence and isolates endpoint/machine/model changes. test: `tests/sessions/test_context_usage.py`.
- 3.2.3 - Remote sessions preserve reported/catalog/OpenRouter behavior and existing local setup lifecycle remains unchanged. test: `tests/agents/test_local_context_setup.py`.

## V1 Verification
`kind: verification`

Original #19653 criteria map to provider discovery (1.2, 1.3, 1.5), capability
provenance (2.1), typed missing/invalid/unreachable unknowns (1.1, 2.2, 2.3),
local warning exclusion and remote OpenRouter coverage (2.4), and existing consumers
(3.1, 3.2). No original criterion is removed or superseded.

Run focused tests listed in each leaf with DATABASE_URL pointing at the isolated
test hub and GOBBY_TEST_PROTECT=1. Run Ruff format/check on changed Python and
applicable mypy checks against production modules. No full pytest suite or live
model loading. Validate the narrative, symbol/target coverage, granularity and
dependency graph before supported human-handoff manifest derive/apply and expansion.

## M1 Task Manifest
`kind: manifest`

```yaml
- title: Define observations and effective-limit calculation
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: '1.1.1: Canonical 262144 and runtime 32768 resolve to 32768;
    lower overrides reduce it, larger overrides cannot increase it. test: `tests/providers/capabilities/test_local_context.py`.

    1.1.2: Multiple instances use the minimum, explicit selection uses that instance,
    and any missing required evidence yields unknown even with an override. test:
    `tests/providers/capabilities/test_local_context.py`.

    1.1.3: Invalid values, conflicting identities, contradictory limits and round-trip
    provenance are covered. test: `tests/providers/capabilities/test_local_context.py`.'
  labels:
  - covers:local-model-context-discovery:1.1:1.1.1
  - covers:local-model-context-discovery:1.1:1.1.2
  - covers:local-model-context-discovery:1.1:1.1.3
  tdd: false
  source_section: '1.1'
  implementation_domain: backend
- title: Discover LM Studio context
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  validation_criteria: '1.2.1: Mock native metadata proves architecture/runtime distinction,
    loaded and unloaded cases, multiple instances and explicit instance selection.
    test: `tests/providers/capabilities/test_local_context_lmstudio.py`.

    1.2.2: Mixed embedding/LLM catalogs, duplicate identities, invalid values, contradictory
    metadata and endpoint failure retain independent typed observations. test: `tests/providers/capabilities/test_local_context_lmstudio.py`.'
  labels:
  - covers:local-model-context-discovery:1.2:1.2.1
  - covers:local-model-context-discovery:1.2:1.2.2
  tdd: false
  source_section: '1.2'
  implementation_domain: backend
- title: Discover Ollama context
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  validation_criteria: '1.3.1: Mock show/ps metadata yields 32768 for 262144 canonical
    context and preserves digest and both source paths. test: `tests/providers/capabilities/test_local_context_ollama.py`.

    1.3.2: Multiple running records, missing ps evidence, unrelated architecture fields,
    digest changes and invalid values fail conservatively. test: `tests/providers/capabilities/test_local_context_ollama.py`.'
  labels:
  - covers:local-model-context-discovery:1.3:1.3.1
  - covers:local-model-context-discovery:1.3:1.3.2
  tdd: false
  source_section: '1.3'
  implementation_domain: backend
- title: Preserve vLLM serving limits and ID projections
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  validation_criteria: '1.4.1: max_model_len survives parsing and positive-value validation,
    with unknown for missing/invalid serving metadata. test: `tests/providers/capabilities/test_local_context_vllm.py`.

    1.4.2: Existing list[str] projections, explicit/auto model selection, embeddings
    consumers and malformed catalog behavior remain unchanged. test: `tests/agents/test_local_model.py`.'
  labels:
  - covers:local-model-context-discovery:1.4:1.4.1
  - covers:local-model-context-discovery:1.4:1.4.2
  tdd: false
  source_section: '1.4'
  implementation_domain: backend
- title: Discover generic loopback context
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  validation_criteria: '1.5.1: Mock loopback serving metadata yields a positive conservative
    limit without guessed provider semantics. test: `tests/providers/capabilities/test_local_context_generic.py`.

    1.5.2: Missing metadata, invalid values, non-loopback URLs and mixed model catalogs
    cannot fabricate local limits. test: `tests/providers/capabilities/test_local_context_generic.py`.'
  labels:
  - covers:local-model-context-discovery:1.5:1.5.1
  - covers:local-model-context-discovery:1.5:1.5.2
  tdd: false
  source_section: '1.5'
  implementation_domain: backend
- title: Persist endpoint-scoped observations and provenance
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  validation_criteria: '2.1.1: Isolated storage round-trips all limits, instance/digest
    identity, timestamp, provenance and unknown diagnostics. test: `tests/providers/capabilities/test_local_context_store.py`.

    2.1.2: Identical model names on different machines/endpoints and local/remote
    routes never collide; replacement removes stale identities without changing other
    snapshots. test: `tests/providers/capabilities/test_local_context_store.py`.'
  labels:
  - covers:local-model-context-discovery:2.1:2.1.1
  - covers:local-model-context-discovery:2.1:2.1.2
  tdd: false
  source_section: '2.1'
  implementation_domain: backend
- title: Refresh observations from endpoint and CLI configuration
  category: code
  task_type: feature
  depends_on:
  - '1.2'
  - '1.3'
  - '1.4'
  - '1.5'
  - '2.1'
  validation_criteria: '2.2.1: Existing endpoint and CLI settings produce endpoint-scoped
    routes, distinguish local/remote models and redact credentials. test: `tests/providers/capabilities/test_local_context_config.py`.

    2.2.2: Endpoint failure/recovery, concurrent callers, waiter cancellation and
    event-loop responsiveness are covered with mocked I/O. test: `tests/providers/capabilities/test_local_context_refresh.py`.

    2.2.3: Configuration or model/instance changes during refresh discard stale results
    and invalidate old observations. test: `tests/providers/capabilities/test_local_context_refresh.py`.'
  labels:
  - covers:local-model-context-discovery:2.2:2.2.1
  - covers:local-model-context-discovery:2.2:2.2.2
  - covers:local-model-context-discovery:2.2:2.2.3
  tdd: false
  source_section: '2.2'
  implementation_domain: backend
- title: Resolve local context without remote fallback
  category: code
  task_type: feature
  depends_on:
  - '2.1'
  validation_criteria: '2.3.1: Local verified limits clamp overrides and local unknowns
    never touch OpenRouter, aliases or model-marker floors. test: `tests/providers/capabilities/test_resolve.py`.

    2.3.2: Public context wrappers preserve remote precedence/warnings and return
    local unknown quietly with diagnostics available. test: `tests/llm/test_context_window.py`.'
  labels:
  - covers:local-model-context-discovery:2.3:2.3.1
  - covers:local-model-context-discovery:2.3:2.3.2
  tdd: false
  source_section: '2.3'
  implementation_domain: backend
- title: Replace provider exclusions with local-model exclusions
  category: code
  task_type: feature
  depends_on:
  - '2.1'
  - '2.2'
  validation_criteria: '2.4.1: Mixed local/remote entries under one CLI provider exclude
    only local routes, including unknowns. test: `tests/providers/capabilities/test_local_context_coverage.py`.

    2.4.2: Remote missing metadata/alias warnings and recovery remain intact after
    local endpoint failure and recovery. test: `tests/providers/capabilities/test_providers_capabilities_refresh.py`.'
  labels:
  - covers:local-model-context-discovery:2.4:2.4.1
  - covers:local-model-context-discovery:2.4:2.4.2
  tdd: false
  source_section: '2.4'
  implementation_domain: backend
- title: Integrate generation and chat consumers
  category: code
  task_type: feature
  depends_on:
  - '2.2'
  - '2.3'
  - '2.4'
  validation_criteria: '3.1.1: Generation and chat setup await refresh and consume
    the new effective value or unknown after failure; remote routes stay unchanged.
    test: `tests/servers/test_local_context_consumers.py`.

    3.1.2: Catalog IDs/defaults/modalities/eligibility and existing group consumers
    are unchanged while context and provenance use observations. test: `tests/servers/test_local_provider_models.py`.

    3.1.3: Model switches and identical names at separate endpoints never inherit
    previous context in chat/history projections. test: `tests/servers/test_local_context_consumers.py`.'
  labels:
  - covers:local-model-context-discovery:3.1:3.1.1
  - covers:local-model-context-discovery:3.1:3.1.2
  - covers:local-model-context-discovery:3.1:3.1.3
  tdd: false
  source_section: '3.1'
  implementation_domain: backend
- title: Integrate coding-session context consumers
  category: code
  task_type: feature
  depends_on:
  - '2.2'
  - '2.3'
  - '3.1'
  validation_criteria: '3.2.1: Spawn, resume and coding chat setup refresh the selected
    route before activation and persist matching provenance. test: `tests/agents/test_local_context_setup.py`.

    3.2.2: Local session context clamps reported/override values, stays unknown without
    runtime evidence and isolates endpoint/machine/model changes. test: `tests/sessions/test_context_usage.py`.

    3.2.3: Remote sessions preserve reported/catalog/OpenRouter behavior and existing
    local setup lifecycle remains unchanged. test: `tests/agents/test_local_context_setup.py`.'
  labels:
  - covers:local-model-context-discovery:3.2:3.2.1
  - covers:local-model-context-discovery:3.2:3.2.2
  - covers:local-model-context-discovery:3.2:3.2.3
  tdd: false
  source_section: '3.2'
  implementation_domain: backend
```
