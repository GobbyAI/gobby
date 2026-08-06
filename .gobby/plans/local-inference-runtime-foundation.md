# Local Inference Runtime Foundation

## Overview
`kind: framing`

**Plan ID:** `local-inference-runtime-foundation`

This plan combines #19653 with the local-runtime foundation of #18866. It gives
Gobby authoritative, first-class configuration, discovery, capability metadata,
readiness preparation, and inference routing for LM Studio, Ollama, canonical
vLLM, vLLM-Metal, llama.cpp, and mlx-vlm while retaining generic
OpenAI-compatible endpoints.

The implementation ends at a capability-checked text, embedding, tool,
structured-output, or generic vision route. UI-TARS computer use remains a
separate dependent plan under #18866 and owns screenshot-to-action validation,
bounded-run approval, desktop permissions/control, cancellation, and recovery.

## Constraints
`kind: framing`

- Gobby configures and prepares a user's existing runtime installation. It may
  probe a configured endpoint, start a user-installed executable, and
  acquire/load an explicitly assigned model. It never installs a runtime,
  stops/restarts a service, unloads/evicts/deletes a model, or claims process
  ownership. A service or model that Gobby prepares stays running.
- Provider instances are shared configuration objects. Embedding and generation
  candidates independently select explicit `endpoint:<instance>/<model>`
  routes; existing ordered generation fallbacks remain ordered. A healthy
  configured endpoint is reused. No loopback/port scanning is allowed.
- Provider transport is chosen per operation, not by a global native/OpenAI
  toggle. Ollama uses native APIs for all supported operations. LM Studio uses
  native discovery, plain chat, vision, download, and load plus OpenAI-compatible
  embeddings, structured output, and custom tools. llama.cpp uses native
  discovery/readiness and embeddings plus OpenAI-compatible chat, tools,
  structured output, and vision. vLLM, vLLM-Metal, and mlx-vlm use their
  provider-native health/discovery surfaces and OpenAI-compatible inference.
  Generic endpoints remain OpenAI-compatible only.
- Transport selection is deterministic before sending an inference request.
  Once a request may have reached a runtime, Gobby returns that transport's
  result or failure and never replays it through another transport.
- The selected embedding route prepares eagerly during daemon startup. Failure
  degrades startup to keyword fallback and records a retry cooldown. Generation
  and generic vision routes prepare on first use. Concurrent preparation for the
  same route is single-flight and observable/cancellable.
- Launch is available only for explicitly configured loopback provider instances.
  It uses provider-generated argument vectors, direct process execution without
  a shell, a configured or detected executable, bounded extra arguments, and
  sanitized logs. An attach-only or remote endpoint receives health diagnostics
  without local launch attempts.
- Multi-model providers may load the assigned model. A fixed-model endpoint
  already serving another model yields a configuration conflict and remains
  untouched. Model acquisition follows the same eager/lazy trigger as its route.
- `context_length` remains the nullable canonical/model maximum.
  `runtime_context_length` is a separate nullable effective served/allocated
  limit. Missing, invalid, or unreachable facts remain `None`; every populated
  fact carries exact source provenance.
- Model routes persist a sanitized endpoint descriptor containing instance name,
  provider kind, and base URL. Credentials, URL userinfo, query, and fragment are
  never persisted or returned. Qwen's model-owned `baseUrl` is preserved.
- Native metadata and safe active probes determine nullable support for text,
  embeddings, tools, structured output, and image input. The foundation performs
  no screenshot-to-action probe.
- The first-run CLI and Settings UI consume the same backend contract for
  detection, assignment, activation, preparation progress, cancellation,
  health, conflicts, and install guidance.
- vLLM-Metal is recommended when detected on Apple Silicon; explicit provider
  selection always wins. Hugging Face remote-code execution defaults off and
  requires explicit opt-in.
- TGI is excluded, SGLang is deferred, and mlx-lm is omitted. There is no
  pre-0.5 compatibility shim: replace the configuration/schema directly.
- No touched hand-maintained production `.py`, `.ts`, `.tsx`, `.css`, `.rs`,
  `.js`, `.mjs`, `.cjs`, or `.sh` file may reach 1,000 lines. Provider adapters
  and readiness logic use focused modules.

## P1: Provider Configuration and Route Authority
`kind: framing`

### 1.1 Replace generation-owned endpoints with shared provider instances [category: code]
`kind: deliverable`

Targets:
- `src/gobby/config/ai.py::GenerationEndpointConfig`
- `src/gobby/config/ai.py::GenerationConfig`
- `src/gobby/config/ai.py::AIConfig`
- `src/gobby/config/persistence.py::EmbeddingsConfig`
- `src/gobby/ai/endpoints.py::GenerationEndpointSelection`
- `src/gobby/ai/endpoints.py::parse_endpoint_model_selector`
- `src/gobby/ai/endpoints.py::resolve_generation_endpoint_selector`

Replace `ai.generation.endpoints` with `ai.providers`, a named registry of
`InferenceProviderConfig`. Each instance has:

- `provider`: one of `openai-compatible`, `lmstudio`, `ollama`, `vllm`,
  `vllm-metal`, `llama-cpp`, or `mlx-vlm`;
- required normalized `api_base` and optional `$secret:`-compatible `api_key`;
- optional `launch` with `executable: str | None`, `extra_args: list[str]`,
  `environment: dict[str, str]`, and `allow_remote_code: bool = false`; and
- optional `wire_api` (`chat-completions` or `responses`) accepted only for the
  generic `openai-compatible` provider. First-class providers choose their
  transport internally per operation.

Validate provider instance names with the existing endpoint slug rule. A
`launch` block is legal only when `api_base` resolves to loopback. Executable
and extra-argument values are data for direct argv construction, never a shell
command. Environment values support secret references and must be redacted.

Keep the public selector shape `endpoint:<instance>/<model>`, but require both
instance and model for provider-instance routes. Remove endpoint-owned default
models and manual `vision_extract`/`tool_chat` booleans. Resolve the selector
against `ai.providers` and return a selection containing the instance config and
selected model. Keep generation profile candidate order and timeout semantics
unchanged. Replace embedding `api_base`/`api_key` ownership with
`embeddings.endpoint: str | None`; `embeddings.model`, dimension, prefixes, and
catalog identity stay embedding-specific. Reject removed pre-0.5 shapes with a
direct validation error rather than translating them.

**Acceptance:**
- 1.1.1 - Config schema accepts every provider kind, attach-only instances, and
  loopback launch settings, while rejecting launch on remote URLs, provider-wide
  transport overrides, missing route models, and secret-bearing malformed URLs.
  test: `tests/config/test_ai.py`.
- 1.1.2 - Embeddings and generation candidates can select different models on
  one shared LM Studio or Ollama instance, and ordered generation fallback
  resolution remains stable. test: `tests/ai/test_endpoints.py`.
- 1.1.3 - `wire_api` is legal only on generic OpenAI-compatible instances and
  `allow_remote_code` defaults to false. test: `tests/config/test_ai.py`.
- 1.1.4 - Old `ai.generation.endpoints`, endpoint-owned model/capability flags,
  and embedding-owned endpoint credentials fail validation with actionable
  replacement paths. test: `tests/config/test_config_removed_keys.py`.

### 1.2 Persist canonical/runtime context and endpoint provenance [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `src/gobby/providers/capabilities/models.py::ModelRouteData`
- `src/gobby/providers/capabilities/models.py::ModelCapabilityData`
- `src/gobby/providers/capabilities/models.py::ModelRoute`
- `src/gobby/providers/capabilities/models.py::ModelCapability`
- `src/gobby/providers/capabilities/store.py::ProviderCapabilityStore._capability_row`
- `src/gobby/providers/capabilities/store.py::ProviderCapabilityStore._route_rows`
- `src/gobby/providers/capabilities/store.py::ProviderCapabilityStore._load_snapshots`
- `src/gobby/providers/capabilities/store.py::_model_capability`
- `src/gobby/providers/capabilities/store.py::_model_route`
- `crates/gcore/assets/schema/baseline.sql`

Add nullable `runtime_context_length`, `supports_text`,
`supports_embeddings`, and `supports_structured_output` to `ModelCapability`;
retain `context_length`, `supports_tools`, and `input_modalities` (`image`
indicates vision input). Add an immutable `EndpointDescriptor` to each
`ModelRoute` with nullable instance name plus required provider and sanitized
base URL. Extend typed-dict serialization and provenance maps for all new facts.

Extend `provider_model_capabilities` with nullable columns for the new facts and
extend `provider_model_routes` with nullable `endpoint_name`,
`endpoint_provider`, and `endpoint_api_base`. Store no API key or launch
environment. Update baseline-derived catalog/identity artifacts through the
repository's canonical schema generation workflow after the owning dirty-schema
work has landed; do not hand-edit generated hashes. No migration or compatibility
branch is required before 0.5.0.

Keep canonical and runtime context independent: a provider may populate either,
both, or neither. Reject non-positive values at the model boundary and preserve
`None` through JSON, SQL, and API serialization.

**Acceptance:**
- 1.2.1 - Capability/route dict and SQL round trips preserve both context facts,
  all nullable capability facts, endpoint descriptors, and per-field provenance.
  test: `tests/providers/capabilities/test_store.py`.
- 1.2.2 - Endpoint sanitization removes credentials, query, and fragment before
  serialization or persistence and retains normalized scheme/host/port/path.
  test: `tests/providers/capabilities/test_models.py`.
- 1.2.3 - `None` remains unknown and zero/negative context values are never
  persisted as facts. test: `tests/providers/capabilities/test_models.py`.
- 1.2.4 - Fresh schema creation contains the new columns and schema catalog
  freshness/identity checks pass. behavior: focused gcore schema contract tests
  and generated-artifact freshness output in the task transcript.

### 1.3 Resolve authoritative local routes without loopback guessing [category: code] (depends: 1.2)
`kind: deliverable`

Targets:
- `src/gobby/servers/provider_model_discovery.py::discover_qwen_configured_models`
- `src/gobby/servers/provider_model_discovery.py::parse_acp_models`
- `src/gobby/providers/capabilities/collectors/qwen.py::_build_model`
- `src/gobby/providers/capabilities/collectors/qwen.py::_standard_route`
- `src/gobby/providers/capabilities/coverage.py::ModelMetadataCoverageAuditor.audit`

Carry each discovered model's explicit endpoint through parsing, Qwen
collection, `ModelRoute.endpoint`, and provider-model APIs. Preserve Qwen's
model-owned `baseUrl`; configured Gobby provider instances use their explicit
`api_base`. Resolve explicit model routes from their persisted descriptor and
never discover authority by scanning common loopback ports.

Apply metadata precedence per fact: exact provider-native observation, then a
safe active probe against the same endpoint, then generic OpenAI-compatible
metadata from that endpoint, then remote registry/alias metadata for genuinely
opaque remote models. An authoritative local route whose provider cannot expose
a fact keeps `None` and does not produce OpenRouter missing-metadata warnings.
Remote opaque models remain subject to the coverage auditor.

**Acceptance:**
- 1.3.1 - Two Qwen models with distinct `baseUrl` values retain distinct route
  descriptors and never collapse to `qwen:acp`. test:
  `tests/providers/capabilities/collectors/test_qwen.py`.
- 1.3.2 - Explicit Gobby/Qwen local routes resolve only their configured URLs;
  tests assert no blind loopback/port probe occurs. test:
  `tests/servers/test_provider_model_discovery.py`.
- 1.3.3 - Coverage audit suppresses remote-registry warnings only for models
  with authoritative local endpoint provenance; remote opaque models still
  report missing metadata. test: `tests/providers/capabilities/test_coverage.py`.

## P2: Focused Provider Adapters and Capability Collection
`kind: framing`

### 2.1 Decompose existing local-provider and embedding transport modules [category: refactor] (depends: P1)
`kind: deliverable`

Targets:
- `src/gobby/llm/local_provider_adapters.py::*` — scope-reason: split every existing adapter and shared helper into focused provider modules, then remove the monolithic source
- `src/gobby/llm/local_providers/contracts.py`
- `src/gobby/llm/local_providers/registry.py`
- `src/gobby/llm/local_providers/openai_compatible.py`
- `src/gobby/llm/local_providers/http.py`
- `src/gobby/ai/embeddings.py::*` — scope-reason: extract provider resolution, HTTP transport, retry, and cache helpers before readiness integration so the production file stays below the line ceiling
- `src/gobby/ai/embedding_transport.py`
- `src/gobby/ai/embedding_cache.py`
- `src/gobby/ai/_text_generation_builder.py::_daemon_text_generation_adapter_factories`
- `src/gobby/ai/_tool_chat_builder.py::_daemon_tool_chat_adapter_factories`

Create `src/gobby/llm/local_providers/` with focused `contracts.py`,
`registry.py`, `openai_compatible.py`, and `http.py` response utilities.
Create `src/gobby/ai/embedding_transport.py` and
`src/gobby/ai/embedding_cache.py` and
leave `EmbeddingService` as the stable façade. Move existing behavior without
changing request payloads, retry boundaries, output parsing, or candidate
selection. Update builders/imports atomically; remove the old adapter monolith.

All extracted modules must stay comfortably below 1,000 lines and expose typed
operation methods rather than a provider-wide client escape hatch. Preserve the
current test suite as characterization coverage for this structural step.

**Acceptance:**
- 2.1.1 - Existing text, JSON, tool, vision, and embedding characterization
  tests pass unchanged against the extracted modules. behavior: focused
  `tests/llm/` and `tests/ai/test_embeddings.py` runs in the task transcript.
- 2.1.2 - No import references `gobby.llm.local_provider_adapters`, and the old
  source is removed. behavior: indexed reference search in the task transcript.
- 2.1.3 - Every touched hand-maintained production file is below 1,000 lines.
  behavior: scoped line-count audit in the task transcript.

### 2.2 Implement deterministic LM Studio and Ollama operation adapters [category: code] (depends: 2.1)
`kind: deliverable`

Targets:
- `src/gobby/llm/local_providers/lmstudio.py`
- `src/gobby/llm/local_providers/ollama.py`
- `src/gobby/llm/local_providers/registry.py`
- `src/gobby/ai/endpoint_activation.py::_probe_text`
- `src/gobby/ai/endpoint_activation.py::_probe_tool_context_and_resume`
- `src/gobby/ai/endpoint_activation.py::_probe_vision`

Implement one typed operation contract covering plain text/chat, embeddings,
custom tools, structured output, image input, model discovery, health,
acquisition, and load. Register a fixed transport for each operation at provider
activation:

- Ollama: native `/api/chat` for plain/tool/structured/image requests,
  `/api/embed` for embeddings, `/api/tags` and `/api/show` for discovery and
  canonical context, `/api/ps` for loaded/runtime context, and native pull/load
  semantics for preparation.
- LM Studio: native `/api/v1/chat` for plain chat and image requests and native
  `/api/v1/models` plus download/load endpoints for lifecycle facts; use
  OpenAI-compatible embeddings, structured output, and custom tools because the
  native chat contract cannot preserve arbitrary tool-loop history.

Capability activation performs safe minimal probes only where native metadata
cannot decide support. Record the chosen transport and exact source URL per
fact. Unsupported operations fail before send. A sent inference request is
never replayed on the alternate transport after timeouts, disconnects, 5xx, or
ambiguous parse failures.

**Acceptance:**
- 2.2.1 - Provider contract tests assert the operation-to-transport matrix and
  exact request/response mapping for native and OpenAI-compatible calls. test:
  `tests/llm/local_providers/test_lmstudio.py` and
  `tests/llm/local_providers/test_ollama.py`.
- 2.2.2 - An unsupported operation can select its declared alternate before
  sending, while an ambiguous post-send failure produces exactly one request.
  test: `tests/llm/local_providers/test_transport_selection.py`.
- 2.2.3 - LM Studio maps `max_context_length` to canonical context and loaded
  `config.context_length` to runtime context; Ollama maps `show` architecture
  context to canonical and `ps.context_length` to runtime, each with native
  provenance. test: provider fixture tests above.
- 2.2.4 - Text, tools, structured output, embeddings, and image support remain
  nullable until metadata or the matching probe establishes a result. test:
  `tests/ai/test_endpoint_activation.py`.

### 2.3 Implement vLLM, vLLM-Metal, llama.cpp, mlx-vlm, and generic adapters [category: code] (depends: 2.2)
`kind: deliverable`

Targets:
- `src/gobby/llm/local_providers/vllm.py`
- `src/gobby/llm/local_providers/llama_cpp.py`
- `src/gobby/llm/local_providers/mlx_vlm.py`
- `src/gobby/llm/local_providers/openai_compatible.py`
- `src/gobby/llm/local_providers/registry.py`

Add canonical vLLM and vLLM-Metal as distinct provider kinds sharing one vLLM
protocol implementation. Use OpenAI-compatible inference plus native health,
version, `/v1/models`, and provider metadata endpoints. Treat
`max_model_len` as runtime/effective context; leave canonical context unknown
unless a provider-native response explicitly distinguishes it.

Use llama.cpp native `/embeddings`, `/props`, and `/v1/models` for embeddings,
readiness, discovery, `meta.n_ctx_train` canonical context, and
`default_generation_settings.n_ctx` runtime context. Use its OpenAI-compatible
chat path for plain chat, tools, structured output, and multimodal chat so Gobby
does not invent raw-prompt chat-template semantics.

Use mlx-vlm's OpenAI-compatible inference endpoints and provider `/models`,
`/health`, and metrics. Populate runtime context only when the current server
returns an exact loaded-context field; otherwise keep both context facts
unknown. Generic OpenAI-compatible endpoints use only their configured wire API
and standard discovery. Apply the same pre-send selection and no ambiguous
replay rule from 2.2.

**Acceptance:**
- 2.3.1 - Contract fixtures prove inference and discovery paths for all five
  provider kinds, including shared protocol behavior and distinct labels for
  vLLM versus vLLM-Metal. test: focused files under
  `tests/llm/local_providers/`.
- 2.3.2 - Context mapping preserves canonical/runtime semantics and `None` for
  unavailable facts across vLLM, llama.cpp, and mlx-vlm. test:
  `tests/llm/local_providers/test_context_metadata.py`.
- 2.3.3 - Generic `responses` versus `chat-completions` selection remains
  configurable, while first-class provider transports reject that override.
  test: `tests/llm/local_providers/test_openai_compatible.py`.
- 2.3.4 - Fixed pre-send transport choice and single-request ambiguous failure
  behavior hold for every provider. test:
  `tests/llm/local_providers/test_transport_selection.py`.

### 2.4 Collect provider models and capability provenance through one discovery path [category: code] (depends: 2.3)
`kind: deliverable`

Targets:
- `src/gobby/servers/local_provider_models.py::*` — scope-reason: replace the LM Studio/Ollama-only branching with the complete provider discovery registry and per-field context/capability extraction
- `src/gobby/servers/routes/providers.py::_local_generation_model_groups`
- `src/gobby/servers/routes/providers.py::_local_generation_provider_entries`
- `src/gobby/servers/routes/providers.py::_configured_endpoints`
- `src/gobby/servers/routes/providers.py::_configured_endpoint_provider_entries`
- `src/gobby/servers/routes/providers.py::_matrix_model_entry`

Drive model listing through the adapter registry for all first-class provider
instances and generic endpoints. Return sanitized endpoint descriptors,
canonical/runtime context facts, exact per-field provenance, current health,
and nullable text/embedding/tool/structured/image support. Merge discovery rows
only when provider, endpoint authority, and provider model identity match.

The provider API must distinguish unreachable, reachable-with-no-models,
unsupported metadata, and malformed responses. One failing instance must not
hide healthy instances. Never fill unknown local facts from OpenRouter unless
the route is genuinely remote and opaque under 1.3's precedence rules.

**Acceptance:**
- 2.4.1 - `/api/providers/models` returns separate groups and sanitized route
  descriptors for every configured instance/provider kind. test:
  `tests/servers/routes/test_providers.py`.
- 2.4.2 - Discovery fixtures cover healthy, empty, unreachable, malformed, and
  partial-metadata responses without cross-instance suppression. test:
  `tests/servers/test_local_provider_models.py`.
- 2.4.3 - Capability API exposes both context facts and exact provenance while
  retaining `null` for unsupported/unknown facts. test:
  `tests/servers/routes/test_providers.py`.

## P3: Non-Owning Readiness Preparation
`kind: framing`

### 3.1 Add single-flight runtime and model preparation [category: code] (depends: P2)
`kind: deliverable`

Targets:
- `src/gobby/ai/inference_readiness/models.py`
- `src/gobby/ai/inference_readiness/service.py`
- `src/gobby/ai/inference_readiness/launch.py`
- `src/gobby/ai/inference_readiness/acquisition.py`
- `src/gobby/agents/local_model.py::*` — scope-reason: replace agent-specific load/unload and keep-alive ownership behavior with the shared non-owning readiness service
- `src/gobby/cli/services.py::ensure_local_embedding_service_ready`
- `src/gobby/cli/services.py::try_autoload_embedding_model`

Create `InferenceReadinessService.prepare(route, trigger)` with one in-flight
operation per normalized instance/model and states `checking`, `starting`,
`acquiring`, `loading`, `ready`, `degraded`, `conflict`, `cancelled`, and
`failed`. Expose immutable `InferencePreparationStatus` with instance, model,
state, nullable progress fraction, sanitized detail, error code, timestamps,
and cancellation availability. Cache failures with a bounded retry cooldown;
explicit retry and later demand may start a new attempt after cancellation or
cooldown.

Preparation order is probe endpoint, inspect served/loaded models, then act only
when required. A healthy endpoint is reused. Multi-model LM Studio/Ollama may
download/load the assigned model. A fixed-model vLLM, vLLM-Metal, llama.cpp, or
mlx-vlm endpoint advertising another model returns `conflict` without signals,
restart, or replacement.

For an unavailable loopback instance with `launch`, resolve the configured
executable or provider-standard detected executable and build safe argv from
provider, configured host/port, requested model, `extra_args`, and explicit
`allow_remote_code`. Launch directly without a shell, wait for readiness, close
startup pipes after bounded sanitized capture, and discard the process handle.
Do not register shutdown hooks or later signal the process. Provider templates
cover `lms server start`, `ollama serve`, `vllm serve` (both vLLM kinds),
`llama-server`, and `mlx_vlm.server`. Attach-only and remote endpoints return
health errors and install/config guidance without launch attempts.

Cancellation stops Gobby's current download/load/start wait and subscribers;
it never stops a service, kills a process, unloads a model, or deletes partial
runtime-managed model data. Remove LM Studio unload and Ollama keep-alive
ownership behavior from the old agent helper.

**Acceptance:**
- 3.1.1 - Concurrent callers for one route share one probe/start/acquisition,
  receive ordered state updates, and retry only after explicit retry or cooldown.
  test: `tests/ai/inference_readiness/test_service.py`.
- 3.1.2 - Provider argv is generated without a shell; executable/arguments/env
  stay separate, secrets are redacted, and remote-code flags appear only after
  explicit opt-in. test: `tests/ai/inference_readiness/test_launch.py`.
- 3.1.3 - Healthy reuse, multi-model load, fixed-model conflict, attach-only
  failure, remote failure, missing executable, acquisition failure, and timeout
  have distinct terminal status/error codes. test:
  `tests/ai/inference_readiness/test_service.py`.
- 3.1.4 - Cancellation leaves runtime services/models running and invokes no
  stop, kill, unload, eviction, or delete primitive. test:
  `tests/ai/inference_readiness/test_cancellation.py`.
- 3.1.5 - No daemon shutdown or agent teardown path retains ownership of a
  prepared runtime process/model. behavior: indexed call-site audit plus focused
  agent/runner tests in the task transcript.

### 3.2 Wire eager embeddings and lazy generation/vision to readiness [category: code] (depends: 3.1)
`kind: deliverable`

Targets:
- `src/gobby/runner_lifecycle_subsystems.py::_check_embedding_service`
- `src/gobby/ai/registry_builder.py::build_daemon_ai_capability_registry`
- `src/gobby/ai/registry_builder.py::_embedding_binding`
- `src/gobby/ai/registry_builder.py::_generation_endpoint_text_bindings`
- `src/gobby/ai/registry_builder.py::_generation_endpoint_vision_bindings`
- `src/gobby/ai/registry_builder.py::_generation_endpoint_tool_bindings`
- `src/gobby/ai/vision.py::LocalVisionExtractAdapter.extract`
- `src/gobby/servers/routes/embeddings.py::embedding_status`
- `src/gobby/servers/routes/llm.py::generate_text`
- `src/gobby/servers/routes/llm.py::chat_completions`
- `src/gobby/servers/routes/llm.py::extract_vision`

Prepare the configured embedding route during daemon subsystem startup before
vector-backed consumers initialize. A preparation failure records degraded
health and allows daemon startup with existing keyword fallbacks; it must not
abort startup. Embedding demand after cooldown retries readiness before the API
call.

Generation, tool-chat, and generic vision candidates call preparation only when
the candidate is selected for an actual request. A readiness conflict/failure
is a normal candidate failure, so the existing ordered fallback chain may move
to its next candidate before any inference was sent. Once inference is sent,
transport failures follow the no-replay rule.

Expose shared backend endpoints to list preparation state/history, stream or
poll progress, explicitly retry, and cancel an active preparation. Reuse the
same status objects in embedding/LLM health payloads and endpoint activation.
Responses must be credential-free and distinguish install guidance,
configuration conflict, runtime health, and model acquisition errors.

**Acceptance:**
- 3.2.1 - Startup eagerly prepares only the selected embedding route; failure
  yields a healthy daemon with degraded embedding status and keyword fallback.
  test: `tests/test_runner_lifecycle_subsystems.py`.
- 3.2.2 - Generation/tool/vision routes remain untouched at startup and prepare
  exactly once when selected on first request. test: focused service and
  `tests/servers/routes/test_llm.py` runs.
- 3.2.3 - Pre-inference readiness failure advances the existing candidate chain;
  an ambiguous inference failure does not retry another transport or duplicate
  the same candidate request. test: `tests/ai/test_text_generation.py` and
  `tests/ai/test_tool_chat.py`.
- 3.2.4 - Status, progress, retry, and cancel APIs share one sanitized state
  contract and cancellation leaves external runtime state untouched. test:
  `tests/servers/routes/test_inference_readiness.py`.

## P4: Setup UX and Installation Guidance
`kind: framing`

### 4.1 Unify first-run detection, provider setup, and route assignment [category: code] (depends: P3)
`kind: deliverable`

Targets:
- `src/gobby/cli/_install_embedding_prompts.py::_select_embedding_provider`
- `src/gobby/cli/_install_embedding_prompts.py::_select_embedding_model`
- `src/gobby/cli/_install_embedding_prompts.py::_run_embedding_install`
- `src/gobby/cli/installers/embedding.py::install_embedding`
- `src/gobby/cli/installers/embedding.py::_setup_lmstudio`
- `src/gobby/cli/installers/embedding.py::_setup_ollama`
- `src/gobby/cli/installers/embedding.py::_persist_embedding_config`
- `src/gobby/cli/installers/embedding.py::_health_check_embedding`

Replace embedding-only local setup branching with a shared inference setup
backend consumed by CLI and web routes. Detect installed executables and healthy
configured/default endpoints for all first-class providers, return provider
kind/platform/path/version/readiness without modifying the machine, then let the
user create a provider instance and assign an embedding or generation route.

On Darwin arm64, recommend detected vLLM-Metal ahead of canonical vLLM while
preserving explicit selection. Provider setup never runs an installer. After
the user confirms an assigned route, activation may invoke the readiness
service, including model acquisition, with visible progress and cancellation.
Errors include the provider-specific guide anchor and a verification command.

Persist `ai.providers.<instance>` and the selected workload route atomically via
the config store/secret store. Store credentials as secret references. Keep
embedding dimension probing and semantic smoke validation after readiness.

**Acceptance:**
- 4.1.1 - Detection covers installed, missing, running, stopped, remote, and
  malformed configurations for every provider without launching/installing
  anything. test: `tests/cli/test_install_embedding_wizard.py`.
- 4.1.2 - Apple Silicon recommendation prefers detected vLLM-Metal; explicit
  canonical vLLM or another provider remains selected. test:
  `tests/cli/test_install_embedding_wizard.py`.
- 4.1.3 - Setup writes one shared provider instance plus an explicit
  instance/model assignment, secrets stay referenced, and failed activation
  does not persist a partial assignment. test:
  `tests/cli/installers/test_embedding.py`.
- 4.1.4 - CLI progress/cancel output reflects the shared readiness states and
  never claims Gobby installed or owns the runtime. test: CLI focused tests.

### 4.2 Add provider-instance and readiness controls to Settings [category: code] (depends: 4.1)
`kind: deliverable`

Targets:
- `web/src/components/settings/sections/ProvidersModelsSection.tsx::GenerationEndpointEditor`
- `web/src/components/settings/sections/ProvidersModelsSection.tsx::GenerationGroup`
- `web/src/components/settings/sections/ProvidersModelsSection.tsx::ProvidersModelsSection`
- `web/src/components/settings/sections/MemoryKnowledgeSection.tsx::EmbeddingsGroup`
- `web/src/components/settings/inference/ProviderInstanceEditor.tsx`
- `web/src/components/settings/inference/InferencePreparationStatus.tsx`

Replace the generation-endpoint editor with focused inference components so the
existing Settings sections stay below the line ceiling. Provider Models owns
shared provider-instance CRUD, detection, model discovery, activation, health,
conflicts, preparation progress, retry/cancel, and install-guide links. Memory &
Knowledge selects an explicit embedding instance/model from the same provider
data; generation profiles continue selecting ordered endpoint/model candidates.

Render canonical and runtime context separately with source provenance. Show
unknown as unknown, never as zero or an estimated value. Display remote/attach
only versus launch-enabled status and require explicit confirmation before
acquiring a missing assigned model. vLLM-Metal is a recommendation badge on
detected Apple Silicon, not an implicit override. Remote-code opt-in is an
explicit unchecked control with a security warning.

**Acceptance:**
- 4.2.1 - UI creates/edits every provider kind, assigns distinct embedding and
  generation models to one multi-model instance, and preserves fallback order.
  test: focused Vitest provider settings tests.
- 4.2.2 - Progress, cancellation, degraded startup, fixed-model conflict,
  attach-only failure, and install guidance render from backend states without
  optimistic ownership claims. test: focused component tests.
- 4.2.3 - Canonical/runtime context and provenance render independently, with
  null facts shown as unknown. test: provider settings component tests.
- 4.2.4 - ProvidersModelsSection and MemoryKnowledgeSection remain below 1,000
  lines after extraction. behavior: scoped line-count audit.

### 4.3 Publish platform-specific runtime installation and verification guide [category: docs] (depends: 4.1)
`kind: deliverable`

Targets:
- `docs/guides/local-inference-runtimes.md`
- `docs/guides/system-requirements.md`
- `docs/guides/configuration.md`

Add one authoritative guide explaining that users install runtimes outside
Gobby, then configure the executable/endpoint and workload route in first-run
CLI or Settings. Include official links, platform constraints, disk/model-size
warnings, verification, default endpoint, and Gobby configuration examples:

- LM Studio: app download at `https://lmstudio.ai/download`, CLI documentation
  at `https://lmstudio.ai/docs/cli`, then verify `lms --help` and start/configure
  the local server through LM Studio.
- Ollama: platform downloads/quickstart at `https://docs.ollama.com/quickstart`
  (including the official Linux install command where applicable), then verify
  `ollama -v` and `ollama list`.
- canonical vLLM: official quickstart at
  `https://docs.vllm.ai/en/latest/getting_started/quickstart/`, recommend an
  isolated Python environment and documented `uv pip install vllm
  --torch-backend=auto`, then verify `vllm --version`.
- vLLM-Metal: Apple Silicon-only official guide at
  `https://docs.vllm.ai/projects/vllm-metal/en/latest/installation/`, show the
  official installer/venv activation and verify `vllm --version`; explain why
  Gobby recommends it only when detected on Darwin arm64.
- llama.cpp: official install guide at
  `https://github.com/ggml-org/llama.cpp/blob/master/docs/install.md`, including
  `brew install llama.cpp` on macOS and official Windows/source alternatives,
  then verify `llama-server --version`.
- mlx-vlm: Apple Silicon install/server instructions from
  `https://github.com/Blaizzy/mlx-vlm`, use an isolated environment with
  `python -m pip install -U mlx-vlm`, then verify `mlx_vlm.server --help`.

Document provider operation coverage, eager embedding versus lazy generation/
vision preparation, fixed-model conflicts, attach-only remote behavior,
cancellation boundaries, and explicit `allow_remote_code` risk. State that
assigned-model preparation may download tens or hundreds of gigabytes and that
Gobby never unloads/deletes it afterward.

**Acceptance:**
- 4.3.1 - Guide provides official platform-appropriate install and verification
  instructions for all six first-class runtimes and generic endpoint
  configuration. file: `docs/guides/local-inference-runtimes.md`.
- 4.3.2 - System requirements and configuration guide link to the new guide and
  show the final `ai.providers`, embedding route, and generation candidate
  shapes. file: `docs/guides/system-requirements.md` and
  `docs/guides/configuration.md`.
- 4.3.3 - Documentation clearly states Gobby's start-only readiness boundary,
  acquisition size/cancellation semantics, vLLM-Metal recommendation rule, and
  remote-code opt-in. behavior: docs link/style validation in the task
  transcript.

## P5: Cross-Provider Contract Verification
`kind: framing`

### 5.1 Add a cross-provider parity and safety regression harness [category: test] (depends: P3)
`kind: deliverable`

Targets:
- `tests/ai/local_provider_contract_fixtures.py`
- `tests/ai/test_local_provider_contract_matrix.py`
- `tests/ai/test_inference_readiness_contract_matrix.py`

Build deterministic HTTP/subprocess fixtures for every provider kind and run the
same operation and readiness scenarios through the public adapter/readiness
contracts. The inference matrix covers discovery, plain text, embeddings,
tools, structured output, image input, canonical/runtime context, provenance,
unsupported operations, malformed metadata, and ambiguous post-send failure.
The readiness matrix covers healthy reuse, missing executable, start, model
acquisition/load, progress, cancellation, cooldown retry, remote/attach-only
failure, fixed-model conflict, and shutdown non-ownership.

Fixtures must record request count, URL, sanitized headers/body, argv/env,
state transitions, and forbidden lifecycle calls. They use isolated local test
servers/process fakes and never contact a user's daemon or installed runtime.

**Acceptance:**
- 5.1.1 - One parameterized matrix covers all seven provider kinds and asserts
  the locked per-operation transport table. test:
  `tests/ai/test_local_provider_contract_matrix.py`.
- 5.1.2 - Every ambiguous inference failure records one outbound request and no
  cross-transport replay. test:
  `tests/ai/test_local_provider_contract_matrix.py`.
- 5.1.3 - Readiness parity asserts eager/lazy trigger behavior, single-flight,
  conflict safety, cancellation, cooldown, and zero stop/restart/unload/delete/
  ownership calls. test:
  `tests/ai/test_inference_readiness_contract_matrix.py`.
- 5.1.4 - Focused Python, Ruff, mypy, frontend Vitest/typecheck, schema contract,
  and plan-relevant integration validations pass without running the full pytest
  suite. behavior: validation commands recorded in implementation task
  transcripts.

## V1 Plan Changelog
`kind: verification`

No enhancement or adversarial-review rounds have been run.
