Plan artifact: `.gobby/plans/vllm-runtime-support.md`

# vLLM Runtime Support

**Plan ID:** vllm-runtime-support

## Overview
`kind: framing`

Add vLLM (canonical vllm-project/vllm and the vllm-metal Apple Silicon/MLX plugin)
as a first-class local generation runtime at parity with LM Studio and Ollama —
native protocol treatment, discovery, activation, web chat, and UI — and land the
multimodal (image-input) generation contract that UI-TARS (#20405) consumes.
Child of epic #18866; task #20404.

## Constraints
`kind: framing`

Decision Record (settled with user, 2026-08-17):

1. **Single `"vllm"` protocol value** covers canonical vLLM and vllm-metal; both
   serve the identical OpenAI-compatible API. Engine differences live in docs and
   detection only.
2. **Web chat transport**: Codex custom `[model_providers.<id>]` config overrides
   with `wire_api = "chat"`, generalizing the existing Responses mechanism in
   `codex_endpoint.py`. Namespaced provider id `gobby-vllm-<endpoint>` (Codex
   reserves `openai`, `ollama`, `lmstudio`). No `--oss`, no upstream Codex change.
3. **One vision path**: optional image inputs land on the `text_generate` core;
   per-adapter `describe_image` transports are deleted; `vision_extract` remains a
   named capability (grants, tighter timeout, routes intact — gcore/gwiki consume
   it) but becomes a prompt/routing preset over the multimodal core. The
   per-endpoint `vision_extract: bool` config field is replaced by probed
   `input_modalities` metadata.

Named defaults:

- Local model capability data stays live-probe; no CapabilityResolver collector
  for `endpoint:*` providers.
- Chat-completions activation probe added; no new doctor CLI surface.
- `model: auto` for vllm resolves via `GET /v1/models` iff exactly one model is
  served; multiple/none is an error. Gobby never loads, unloads, or launches
  vLLM (non-owning).
- Image inputs are supported only by adapters with proven image paths: local
  OpenAI-compatible including vllm, LM Studio and Ollama native transports
  (2.2 port), Claude SDK (2.5), and the Codex endpoint transport (2.5).
  Image eligibility is keyed on binding metadata (provider, endpoint
  presence, `wire_api`) — never on adapter class or spawn style: feature CLI
  providers (agy, droid, grok, qwen) and generic `provider=codex` bindings
  with no endpoint metadata are always skipped when a request carries images.

Non-goals: UI-TARS adapter (#20405, depends on this plan); embeddings served by
vLLM; retiring the `vision_extract` capability name or its Rust-facing contract;
Codex upstream `--local-provider` changes; managing vLLM server processes.
No backward compatibility is required anywhere (0.5.0 unshipped).

## P1: Protocol and Adapter Foundation
`kind: framing`

**Goal**: A `vllm` endpoint protocol exists end-to-end in config and the adapter
dispatch, with correct model-lifecycle semantics.

### 1.1 Add vllm protocol value and adapter dispatch [category: code]
`kind: deliverable`

Targets:
- `src/gobby/config/ai.py::GenerationEndpointConfig`
- `src/gobby/config/ai.py::GenerationConfig`
- `src/gobby/llm/local_provider_adapters.py::create_local_provider_adapter`
- `src/gobby/llm/local_provider_adapters.py::OpenAICompatibleLocalProviderAdapter`
- `tests/llm/test_local_provider_adapters.py::*` — scope-reason: new vllm adapter cases across existing dispatch/vision/json test groups

Extend `GenerationEndpointProtocol` in `src/gobby/config/ai.py`:

```python
GenerationEndpointProtocol = Literal["openai-compatible", "lmstudio", "ollama", "vllm"]
```

The existing validator (`wire_api == "responses"` requires `openai-compatible`)
stays; `vllm` is chat-completions only.

Add a `vllm` branch in `create_local_provider_adapter` that dispatches
directly to `OpenAICompatibleLocalProviderAdapter` — no subclass. vLLM's
entire inference surface is the OpenAI-compatible API (`/v1/chat/completions`,
`/v1/models`; auxiliary endpoints like `/health` and `/metrics` carry no
generation payloads), so there is no vLLM-specific wire behavior to
encapsulate: protocol-specific knowledge lives in protocol metadata and the
1.2/2.3/3.1 branches. A vLLM subclass is introduced only when a concrete
divergent wire behavior arrives. The generic adapter provides the real
`AsyncOpenAI` `.client` property (this makes `tool_chat` work for vllm),
OpenAI chat completions, `response_format` JSON fallback, and `image_url`
vision blocks.

**Acceptance:**

- 1.1.1 - `"vllm"` is a valid `GenerationEndpointProtocol` value and configures under `ai.generation.endpoints.<name>`. file: `src/gobby/config/ai.py`.
- 1.1.2 - `create_local_provider_adapter` returns an `OpenAICompatibleLocalProviderAdapter` with a non-None `client` for vllm endpoints; no vllm-specific adapter class exists. test: `tests/llm/test_local_provider_adapters.py::test_create_adapter_vllm`.
- 1.1.3 - `wire_api: responses` on a vllm endpoint is rejected by config validation. test: `tests/llm/test_local_provider_adapters.py::test_vllm_rejects_responses_wire`.

### 1.2 vLLM model lifecycle in ensure_local_model [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `src/gobby/agents/local_model.py::ensure_local_model`

Add a `vllm` case to `ensure_local_model` in `src/gobby/agents/local_model.py`.
vLLM serves the models loaded at server start and has no load/unload API:

- `model: auto` → `GET {api_base}/v1/models`; exactly one served model resolves
  to it; zero or multiple raise `LocalModelError` naming the served models.
- Explicit `model:` → verify present in `/v1/models`; absent raises
  `LocalModelError` (never attempt a load).
- No keep-alive, no swap logic, no conflict detection (server owns its models).

Factor the served-model lookup into a shared resolver
`resolve_vllm_served_model(endpoint)` beside `ensure_local_model` in
`src/gobby/agents/local_model.py`. It normalizes the endpoint origin — an
`api_base` configured with or without a trailing `/v1` yields exactly one
`{origin}/v1/models` discovery URL, never `/v1/v1/models` — applies the
exactly-one `auto` rule above, and returns the concrete served-model id.
Every path that emits a model id on the vllm wire resolves through it:
lifecycle pre-flight, 2.3 activation probes, text/JSON/tool/vision generation
dispatch, and the 4.1/4.2 Codex config-override transport. The literal
sentinel `auto` never appears in any wire request.

**Acceptance:**

- 1.2.1 - `model: auto` on a single-model vllm endpoint resolves to the served model; multi-model raises with the served list. symbol: `gobby.agents.local_model.ensure_local_model`.
- 1.2.2 - vllm endpoints never receive load/unload/keep-alive requests. behavior: "non-owning model lifecycle" in `src/gobby/agents/local_model.py`.
- 1.2.3 - The literal model value `auto` never reaches a vllm wire request on any path — generation, activation probes, tool_chat, or Codex override args; all resolve through the shared resolver. test: `tests/agents/test_local_model.py::test_vllm_auto_resolves_before_wire`.
- 1.2.4 - `api_base` values with and without a trailing `/v1` both produce a single normalized `{origin}/v1/models` URL. test: `tests/agents/test_local_model.py::test_vllm_models_url_normalization`.

## P2: Multimodal Generation Core and Vision Consolidation
`kind: framing`

**Goal**: One image-capable generation path; `vision_extract` becomes a preset
over it; capability claims are probe-verified.

### 2.1 Optional image inputs on the text-generation core [category: code] (depends: 1.1, 1.2)
`kind: deliverable`

Targets:
- `src/gobby/ai/_text_generation_contracts.py::TextGenerationRequest`
- `src/gobby/llm/local.py::LocalLLMProvider`
- `src/gobby/llm/local_provider_adapters.py::OpenAICompatibleLocalProviderAdapter`
- `src/gobby/ai/_text_generation_adapters.py::LocalTextGenerateAdapter`
- `src/gobby/ai/_text_generation_service.py::TextGenerationService`
- `src/gobby/llm/image_payloads.py::prepare_image_data`
- `src/gobby/servers/routes/llm.py::TextGeneratePayload`
- `src/gobby/servers/routes/llm.py::generate_text`
- `tests/llm/test_image_payloads.py::*` — scope-reason: data-URL decode, allowlist-rejection, and bound-enforcement cases across the normalization suite
- `tests/servers/routes/test_llm_routes.py::*` — scope-reason: route-level image acceptance and rejection cases across the generate endpoint suite

Add `images: list[str] | None = None` (file paths or data URLs) to
`TextGenerationRequest` in `src/gobby/ai/_text_generation_contracts.py`. Reuse
`prepare_image_data` from `src/gobby/llm/image_payloads.py` (5 MiB cap, mime
allowlist) to normalize inputs. Thread images through
`LocalTextGenerateAdapter` → `LocalLLMProvider.generate_text_result` →
`LocalProviderAdapter.generate_text_result`, rendered as OpenAI `image_url`
content blocks in `OpenAICompatibleLocalProviderAdapter` (the rendering already
exists in its `describe_image`; move it into the generate path). Candidate
selection in `TextGenerationService` applies an explicit image-routing
predicate when `request.images` is non-empty: a candidate is eligible only
when both its transport is in the image-eligible set and its
resolved model metadata carries `"image"` in `input_modalities`. The
image-eligible transport set is a growing allowlist keyed on binding
metadata — provider, endpoint presence, and `wire_api` — never on adapter
class, `AIAdapterStyle`, or the spawn-cold set (`CodexCLITextGenerateAdapter`,
`AIAdapterStyle.CLI`/`DAEMON`, and `_SPAWN_COLD_ADAPTER_STYLES` are all
non-keys, because generic `codex` shares an adapter class and style with the
image-capable responses-wire Codex endpoint): at 2.1 landing it admits
exactly local `openai-compatible` and `vllm` endpoint bindings; 2.2 adds
`lmstudio` and `ollama` endpoint bindings when their native shapes are
ported; 2.5 adds `provider=claude` (SDK) and responses-wire endpoint
bindings when their wire mappings land. Feature CLI providers (`agy`,
`droid`, `grok`, `qwen`) and generic `provider=codex` bindings with no
endpoint metadata are never eligible, in every phase. A transport never
enters the set before the deliverable
that proves its image path. vLLM model ids on this path resolve through the
1.2 resolver. An explicitly
selected endpoint/model that fails the predicate returns a deterministic
modality diagnostic instead of a provider error. The predicate governs normal
candidate selection only; 2.3 activation probes dispatch directly against the
target endpoint's adapter and never consult it.

The live HTTP boundary carries the same contract: `TextGeneratePayload` in
`src/gobby/servers/routes/llm.py` gains `images: list[str] | None`, validated
at the route by the `generate_text` handler and forwarded verbatim to
`TextGenerationRequest.images`.

Image ingress is bounded and canonical. Accepted forms are exactly two: an
absolute filesystem path readable by the daemon, or a `data:` URL whose MIME
type is on the `prepare_image_data` allowlist with valid base64 payload.
Normalization canonicalizes every input to (MIME type, base64 bytes) via
`prepare_image_data`, which this deliverable extends: today it accepts only
filesystem paths and silently remaps unknown MIME types to `image/png`; it
gains `data:` URL decoding, the absolute-path policy, and hard allowlist
rejection in place of the silent remap, and the count and aggregate bounds
below are enforced at this single normalization layer. Limits: at most 8
images per request, at most 5 MiB
decoded per image (the existing `prepare_image_data` cap), at most 24 MiB
decoded aggregate per request. An empty list is treated as absent. Malformed
data URLs, disallowed MIME types, invalid base64, relative or unreadable
paths, excessive count, and aggregate overflow each produce a deterministic
rejection naming the offending input; nothing is silently dropped.

**Acceptance:**

- 2.1.1 - `TextGenerationRequest` carries optional images; text-only requests are byte-identical to today's payloads. symbol: `gobby.ai._text_generation_contracts.TextGenerationRequest`.
- 2.1.2 - An image-bearing request against an OpenAI-compatible endpoint renders `image_url` content blocks. test: `tests/llm/test_local_provider_adapters.py::test_generate_with_images`.
- 2.1.3 - Image-bearing requests never route to candidates without image support. behavior: "modality-aware candidate filtering" in `src/gobby/ai/_text_generation_service.py`.
- 2.1.4 - Mixed-catalog routing: text-only models are skipped and an image-capable model is selected for image-bearing requests. test: `tests/ai/test_text_generation.py::test_image_routing_mixed_catalog`.
- 2.1.5 - An explicitly selected text-only endpoint/model with images returns a deterministic modality diagnostic. test: `tests/ai/test_text_generation.py::test_image_request_text_only_selection_diagnostic`.
- 2.1.6 - `POST /api/llm/generate` accepts `images`, forwards them to `TextGenerationRequest.images`, and an image-bearing route request reaches an image-capable endpoint end-to-end. test: `tests/servers/routes/test_llm_routes.py::test_generate_route_forwards_images`.
- 2.1.7 - Malformed data URLs, disallowed MIME, invalid base64, relative or unreadable paths, count over 8, and aggregate decoded size over 24 MiB are each rejected deterministically with a diagnostic naming the offending input. test: `tests/servers/routes/test_llm_routes.py::test_generate_route_image_rejections`.
- 2.1.8 - An image-bearing request never selects a feature CLI provider (agy/droid/grok/qwen) or a generic `codex` binding without endpoint metadata, and the predicate reads binding metadata rather than adapter class or spawn style. test: `tests/ai/test_text_generation.py::test_image_routing_skips_generic_codex`.

### 2.2 Collapse vision_extract onto the generation core [category: refactor] (depends: 2.1, 2.5)
`kind: deliverable`

Targets:
- `src/gobby/ai/vision.py::VisionExtractService`
- `src/gobby/ai/vision.py::LocalVisionExtractAdapter`
- `src/gobby/ai/vision.py::ClaudeVisionExtractAdapter`
- `src/gobby/llm/local_provider_adapters.py::create_local_provider_adapter`
- `src/gobby/llm/local_provider_adapters.py::LMStudioLocalProviderAdapter`
- `src/gobby/llm/local_provider_adapters.py::OllamaLocalProviderAdapter`
- `src/gobby/llm/local.py::LocalLLMProvider`
- `src/gobby/ai/_text_generation_service.py::TextGenerationService`
- `tests/ai/test_vision_extraction.py::*` — scope-reason: rewire every adapter test from describe_image mocks to generation-core mocks

Ordering inside this deliverable is fixed: first port each provider's native
image serialization out of its `describe_image` into the same adapter's
`generate_text_result` — LM Studio's `/api/v1/chat` input parts
(`{"type": "image", "data_url": ...}`) and Ollama's native image fields — so
image-bearing generate requests preserve each provider's proven wire shape.
Only then delete `describe_image` from the `LocalProviderAdapter` protocol and
all three adapter implementations in
`src/gobby/llm/local_provider_adapters.py`, and
`LocalLLMProvider.describe_image` in `src/gobby/llm/local.py`.
`LocalVisionExtractAdapter` calls the unified generation path with the
extraction prompt plus images; `ClaudeVisionExtractAdapter.extract` does the
same through the 2.5 Claude SDK image blocks — the depends edge on 2.5
exists precisely so `describe_image` deletion never precedes the
generate-path image mapping that replaces it. This deliverable extends the
2.1 image-eligible transport set with LM Studio and Ollama once their native
shapes are ported; that allowlist lives in `TextGenerationService` (the 2.1
predicate owner, listed in Targets), and extending it here is what makes the
ported shapes reachable.
`VisionExtractService` keeps its API, capability name, grant gate, routes, and
`VISION_TIMEOUT` semantics — external contract (gcore, gwiki) unchanged.
`CodexEndpointVisionExtractAdapter` keeps `run_turn(images=...)`.

**Acceptance:**

- 2.2.1 - No `describe_image` symbol remains in `src/gobby/llm/`; vision extraction executes through the generation core. file: `src/gobby/llm/local_provider_adapters.py`.
- 2.2.2 - `POST /api/llm/vision/extract` behavior and grant gating are unchanged for existing callers. test: `tests/ai/test_vision_extraction.py`.
- 2.2.3 - Image-bearing generate requests against LM Studio and Ollama serialize each provider's native image shape, matching the pre-port `describe_image` wire payloads. test: `tests/llm/test_local_provider_adapters.py::test_native_image_serialization_ported`.
- 2.2.4 - After this deliverable the image-eligible predicate in `TextGenerationService` admits lmstudio and ollama endpoint bindings; image-bearing requests route to them. test: `tests/ai/test_text_generation.py::test_image_allowlist_admits_native_transports`.

### 2.3 Chat-completions activation probe and modality metadata [category: code] (depends: 1.1, 1.2, 2.1, 2.2, 5.1)
`kind: deliverable`

Targets:
- `src/gobby/ai/endpoint_activation.py::probe_responses_endpoint`
- `src/gobby/ai/vision.py::_daemon_vision_extract_adapters`
- `src/gobby/servers/routes/providers.py::_responses_endpoint_models`
- `src/gobby/servers/routes/configuration_generation_endpoints.py::ActivateGenerationEndpointRequest`
- `src/gobby/servers/routes/configuration_generation_endpoints.py::register_generation_endpoint_routes`
- `src/gobby/config/values.py::ConfigValuesService.patch_flat`
- `src/gobby/config/documents.py::ConfigDocumentsService.replace_yaml`
- `src/gobby/config/ai.py::GenerationEndpointConfig`
- `src/gobby/ai/registry_builder.py::_generation_endpoint_vision_bindings`
- `src/gobby/ai/registry_builder.py::_generation_endpoint_text_bindings`
- `src/gobby/runtime_grants/service.py::_vision_extract_enabled`
- `tests/ai/test_endpoint_activation.py::*` — scope-reason: probe outcome-table, credential-mode, invalidation, and bootstrap cases across the activation suite

Add `probe_chat_completions_endpoint()` beside `probe_responses_endpoint` in
`src/gobby/ai/endpoint_activation.py`: text probe, JSON-mode probe, tool-call
probe, and a vision probe that sends a small inline image. The vision probe
dispatches directly against the endpoint's adapter — 2.1 image rendering for
openai-compatible/vllm, the 2.2-ported native shapes for lmstudio/ollama;
the depends edge on 2.2 exists so a native endpoint is never vision-probed
through a serialization it will not use in production — bypassing candidate
selection and its modality predicate: the probe is what creates the modality
evidence, so it cannot be gated on that evidence. The JSON-mode probe sends
`response_format=json_object` and succeeds only when parseable JSON returns
without the adapter's strip-and-retry fallback engaging; a fallback response
is a JSON-probe failure. Probes resolve vllm model ids through the 1.2
resolver; the resolved id is what evidence is recorded against.

Probe outcomes follow a fixed table:

| Probe | Skipped when | On failure |
|-------|--------------|------------|
| text | never | activation fails (fatal); no metadata or grants persist |
| JSON mode | never | degraded: `probed_json: false` persisted; JSON capability absent from metadata |
| tool call | endpoint `tool_chat: false` | degraded: `probed_tools: false` persisted; tool binding unavailable with a probe-failure reason via the 2.4 gate |
| vision | never | degraded: `"image"` absent from `input_modalities` (text-only) |

Degraded outcomes persist only the capabilities that succeeded; a failed
capability is removed from persisted metadata and from derived grants. A later
successful re-activation restores it; a failed re-probe leaves it absent.

Replace `vision_extract: bool` on `GenerationEndpointConfig` with
activation-owned, model-scoped probe evidence. The deletion is load-safe:
`GenerationEndpointConfig` strips `vision_extract` from stored documents
before validation (a pre-validation ignore under `extra=forbid`), so
existing YAML/DB endpoint configs written before this deliverable keep
loading after the field is gone — the stored value is discarded, never
migrated into evidence, and such an endpoint presents as probe-unknown
until activated. The evidence fields are: `probed_model: str`,
`input_modalities: list[str] | None`, `probed_json: bool`, and
`probed_tools: bool | None` (`None` when the tool probe was skipped via
`tool_chat: false`). `probed_model` records the resolved served-model id from
the 1.2 resolver — never the `auto` sentinel. The evidence applies only to
the model actually probed; other models served by the same endpoint stay
unknown (3.1 merges modalities only into the matching model entry, and
unknown models are never image-eligible or chip-labeled). Evidence is
consumed only where the live served catalog still lists `probed_model`: the
3.1 merge skips absent ids, and image dispatch re-validates through the 2.1
predicate, so a served-model swap under `model: auto` yields a deterministic
modality diagnostic rather than stale image routing. Any change to the
endpoint's identity — protocol, `wire_api`, `api_base`, `model`, or
`api_key` — clears the persisted evidence **in a shared identity-clearing
helper**, not in any single route: the helper lives in
`src/gobby/config/values.py` beside the module-level
`reject_unprobed_responses_endpoints` (which `src/gobby/config/documents.py`
already imports and calls on the import/replace prepare path) and unsets
`probed_model`, `input_modalities`, `probed_json`, and `probed_tools`
whenever a non-probe-verified mutation changes an identity field,
regardless of client payload. Identity change is a previous-versus-next
comparison, never payload-key presence: the helper compares the identity
fields in the anchored desired config at `expected_revision` (the same
snapshot `reject_unprobed_responses_endpoints` already reads) against the
post-mutation values after secret handling — a resubmitted `MASKED_SECRET`
api_key is the unchanged stored secret (`patch_flat` already skips the
masked write, and the import path restores masked secrets the same way),
while an api_key unset is a real identity change and clears. The settings
editor PATCHes the entire endpoint object on every save, resubmitting
every identity field with the api_key masked, so a helper keyed on which
endpoint keys appear in the mutation (the touched-key pattern
`reject_unprobed_responses_endpoints` itself uses) or one that
string-compares the mask against the stored secret would clear evidence
on every no-op Providers/Models save — including a timeout-only edit or a
resave of an unchanged endpoint. Both config-mutation paths call it:
`ConfigValuesService.patch_flat` (covering `PATCH /api/config/values` — the
settings editor's save path) and `ConfigDocumentsService.replace_yaml`
(covering `POST /api/config/import` and template application, which reach
`replace_namespace` without ever entering `patch_flat`). The helper skips
clearing when the mutation is probe-verified — `patch_flat`'s existing
`probe_verified=True` flag, which only the activate route sets: an
identity-changing activate writes the new identity fields and the
just-probed replacement evidence in the same patch, so an unconditional
helper would unset the evidence that patch is storing, and 2.3.8's
unknown-to-image-capable and 2.3.10's Responses-path evidence could never
persist after an identity edit. `replace_yaml` is never probe-verified. A
settings save without a follow-up activation never leaves stale modalities
visible; an exported document whose identity fields were edited offline
drops its evidence on re-import; an identity-changing activate persists
the replacement probe result; and a failed re-probe against the
replacement configuration leaves the evidence absent. Conversely, a
same-identity settings save (masked api_key, unchanged
protocol/`wire_api`/`api_base`/`model`) and a re-import of an unchanged
export both preserve the evidence. Invalidation on the
activate route alone, in a `GenerationEndpointConfig` validator that
cannot see the previous identity, in `patch_flat` alone (leaving the YAML
import/replace path stale), in an unconditional helper that also clears
probe-verified activate writes, or in a helper keyed on payload-key
presence or mask-versus-stored comparison that clears on same-identity
saves, does not satisfy 2.3.5.

Credentials are optional for local chat-completions activation: an endpoint
without `api_key` activates without an Authorization header; when a key is
configured the probes send it. Activation never hard-requires a key for
lmstudio, ollama, or vllm protocols.

Unpin `ActivateGenerationEndpointRequest` in
`src/gobby/servers/routes/configuration_generation_endpoints.py` (drop the
`openai-compatible`/`responses` Literals) and persist probe results.
`_generation_endpoint_vision_bindings` in `src/gobby/ai/registry_builder.py`
derives availability from probed modalities (config trust removed). Grants
derivation follows the same evidence: `_vision_extract_enabled` in
`src/gobby/runtime_grants/service.py` derives from persisted modalities
instead of the deleted `vision_extract` config field.

Every remaining reader of the deleted field migrates in this deliverable:
`probe_responses_endpoint` persists the same probed evidence shape on the
Responses path instead of reading and copying `vision_extract`;
`_daemon_vision_extract_adapters` in `src/gobby/ai/vision.py` builds vision
bindings from persisted evidence; `_responses_endpoint_models` in
`src/gobby/servers/routes/providers.py` emits `input_modalities` from that
evidence; and `_generation_endpoint_text_bindings` in
`src/gobby/ai/registry_builder.py` — which today copies
`endpoint.vision_extract` into text_generate binding metadata — drops that
key entirely (it is write-only; no consumer reads it, and image eligibility
flows through probe evidence and the 2.1 predicate, never binding
metadata), so registry construction succeeds for endpoints without the
field instead of raising `AttributeError` before vision bindings or grants
are derived. An endpoint with no probe evidence is unknown: no vision
binding, no image modality, no grant. The depends edge on 5.1 exists because the
settings editor is the last out-of-band writer of `vision_extract`
(`extra=forbid` would 422 its saves once the field is deleted); 5.1 removes
that toggle first.

**Acceptance:**

- 2.3.1 - `PUT /api/config/generation-endpoints/{name}/activate` probes lmstudio, ollama, and vllm chat-completions endpoints and persists `input_modalities`. file: `src/gobby/servers/routes/configuration_generation_endpoints.py`.
- 2.3.2 - `vision_extract` no longer exists as an endpoint config field; vision bindings require probed or advertised image modality. symbol: `gobby.ai.registry_builder._generation_endpoint_vision_bindings`.
- 2.3.3 - A vision probe failure yields a text-only activation, never a hard endpoint failure. test: `tests/ai/test_endpoint_activation.py::test_vision_probe_degrades_to_text`.
- 2.3.4 - On an endpoint serving two models with different modality support, probe evidence attaches only to the probed model; the other stays unknown in discovery and routing. test: `tests/ai/test_endpoint_activation.py::test_model_scoped_modalities_mixed_endpoint`.
- 2.3.5 - Changing protocol, `wire_api`, `api_base`, `model`, or `api_key` (including an api_key unset) clears persisted modality evidence via the shared identity-clearing helper on both non-probe-verified mutation paths: asserted through `PATCH /api/config/values` with no follow-up activation (`ConfigValuesService.patch_flat`), and through YAML import of an exported document whose identity fields were edited (`ConfigDocumentsService.replace_yaml`) — while a same-identity settings PATCH that resubmits the full endpoint object with a `MASKED_SECRET` api_key preserves the evidence, a re-import of an unchanged export preserves it, an identity-changing activate (`probe_verified=True`) persists its replacement probe evidence instead of clearing it, and a failed re-probe leaves no stale image capability anywhere. test: `tests/ai/test_endpoint_activation.py::test_identity_change_invalidates_modalities`.
- 2.3.6 - A keyless local endpoint activates without an Authorization header; a configured key is sent. test: `tests/ai/test_endpoint_activation.py::test_optional_credentials_activation`.
- 2.3.7 - The probe outcome table holds end-to-end: text failure is fatal; JSON/tool/vision failures degrade and persist `probed_json`/`probed_tools` false; `tool_chat: false` skips the tool probe (`probed_tools: None`); re-activation restores recovered capabilities in metadata and grants. test: `tests/ai/test_endpoint_activation.py::test_probe_outcome_table`.
- 2.3.8 - First activation of a fresh endpoint reaches its vision probe with no pre-existing modality metadata; unknown-to-image-capable and unknown-to-text-only transitions both persist correctly. test: `tests/ai/test_endpoint_activation.py::test_vision_probe_bootstrap`.
- 2.3.9 - Runtime grant derivation follows probe evidence: image-capable evidence enables vision_extract; degraded or cleared evidence removes it. test: `tests/runtime_grants/test_active_config_binding.py::test_vision_grant_follows_probe_evidence`.
- 2.3.10 - The Responses activation path persists the same probed evidence shape, and a Responses endpoint without evidence has no vision binding, no image modality, and no grant. test: `tests/ai/test_endpoint_activation.py::test_responses_path_evidence_migration`.
- 2.3.11 - A stored endpoint document carrying `vision_extract: true` loads successfully after the field's deletion and presents as probe-unknown — no vision binding, no image modality, no grant. test: `tests/ai/test_endpoint_activation.py::test_vision_extract_field_stripped_on_load`.
- 2.3.12 - Registry construction succeeds for a generation endpoint after the field's deletion, and text_generate binding metadata carries no `vision_extract` key. test: `tests/ai/test_capability_registry.py::test_text_bindings_drop_vision_extract_metadata`.

### 2.4 Honest tool_chat availability for clientless adapters [category: code] (depends: 1.1, 2.3)
`kind: deliverable`

Targets:
- `src/gobby/ai/_tool_chat_builder.py::_local_client_factory`
- `src/gobby/ai/registry_builder.py::_generation_endpoint_tool_bindings`

Pre-existing bug: lmstudio/ollama adapters expose `client=None`, yet
`tool_chat: true` registers an available binding that fails at dispatch inside
`_local_client_factory`. Gate tool_chat binding availability on the adapter
actually exposing a client (vllm and openai-compatible qualify; lmstudio/ollama
become unavailable with an explanatory reason). The binding is constructed by
`_generation_endpoint_tool_bindings` in `src/gobby/ai/registry_builder.py` —
that is where the availability rule lands. The gate reads three inputs:
config `tool_chat` (the user's skip latch, never overwritten by probes), the
adapter client, and 2.3's `probed_tools` evidence when present — evidence
`false` makes the binding unavailable with a probe-failure reason; absent
evidence (never activated) falls back to the client-and-config gate. The
depends edges exist because 2.3 owns the evidence shape and both
deliverables edit `registry_builder.py`, and because 2.4.1's vllm dispatch
assertion needs the 1.1 adapter.

**Acceptance:**

- 2.4.1 - `tool_chat` on an lmstudio/ollama endpoint reports unavailable with a reason instead of failing at dispatch; vllm endpoints dispatch successfully. test: `tests/ai/test_capability_registry.py::test_tool_chat_clientless_unavailable`.
- 2.4.2 - A failed tool probe (`probed_tools: false`) reports unavailable with the probe reason while config `tool_chat` stays untouched; absent evidence preserves the client-gated behavior. test: `tests/ai/test_capability_registry.py::test_tool_binding_probe_evidence_gate`.

### 2.5 Cloud transport image mapping: Claude SDK and Codex endpoint [category: code] (depends: 2.1)
`kind: deliverable`

Targets:
- `src/gobby/ai/_text_generation_adapters.py::ClaudeTextGenerateAdapter`
- `src/gobby/llm/claude.py::ClaudeLLMProvider`
- `src/gobby/llm/claude_sdk.py::ClaudeSDKClient`
- `src/gobby/ai/_text_generation_adapters.py::CodexCLITextGenerateAdapter`
- `src/gobby/ai/_text_generation_builder.py::_responses_text_generate_adapter_factory`
- `src/gobby/ai/_text_generation_service.py::TextGenerationService`

The 2.1 predicate declares Claude SDK and the Codex endpoint transport
image-eligible; this deliverable wires images into both wire requests so the
declaration is honest and cloud vision models are usable through the unified
path.

Claude SDK: `ClaudeTextGenerateAdapter.generate` forwards `request.images`
through `ClaudeLLMProvider.generate_text_result` into
`ClaudeSDKClient.generate_text_result`, which renders them as SDK image
content blocks (`{"type": "image", "source": {..., "data": <base64>}}`) via
`prepare_image_data` — the exact machinery `ClaudeSDKClient.describe_image`
uses today, moved into the generate flow.

Codex endpoint: `CodexCLITextGenerateAdapter.build_command` maps normalized
images to `codex exec -i/--image <FILE>` arguments; data-URL inputs are
materialized to temp files inside the adapter's per-call
`neutral_textgen_cwd()` directory (or an equivalent try/finally owned by
`CodexCLITextGenerateAdapter.generate`, which owns process lifetime), so the
decoded bytes are removed on every outcome — success, nonzero exit, timeout,
and cancellation. `_responses_text_generate_adapter_factory` passes images
through for responses-wire endpoints. This mirrors the proven Codex image
path (`run_turn(images=...)` in `src/gobby/ai/_tool_chat_codex.py`). Image
bytes never appear in argv — only file paths. This deliverable extends the
2.1 image-eligible transport set with Claude SDK and the Codex endpoint —
the allowlist lives in `TextGenerationService` (the 2.1 predicate owner,
listed in Targets), and the entries added here are keyed as `provider=claude`
and responses-wire endpoint bindings; generic `provider=codex` bindings with
no endpoint metadata stay ineligible.

**Acceptance:**

- 2.5.1 - An image-bearing request through the Claude SDK transport renders SDK image content blocks in the outgoing query. test: `tests/llm/test_claude.py::test_generate_text_with_images_renders_blocks`.
- 2.5.2 - An image-bearing request through a responses-wire Codex endpoint emits `--image` file arguments, materializes data-URL inputs to temp files with cleanup, and never places image bytes in argv. test: `tests/ai/test_text_generation.py::test_codex_endpoint_image_args`.
- 2.5.3 - Temp files materialized for data-URL images are removed on failure paths — nonzero exit, timeout, and cancellation — not only on success. test: `tests/ai/test_text_generation.py::test_codex_endpoint_image_tempfile_cleanup`.
- 2.5.4 - After this deliverable the image-eligible predicate admits `provider=claude` and responses-wire Codex endpoint bindings while an image-bearing generic `codex` binding without endpoint metadata stays skipped. test: `tests/ai/test_text_generation.py::test_image_allowlist_cloud_transports`.

## P3: Discovery and Modality Metadata
`kind: framing`

**Goal**: vLLM models are live-discovered with correct metadata; VLM
classification is fixed across all local runtimes.

### 3.1 Native vLLM model discovery [category: code] (depends: 1.1, 1.2, 2.3)
`kind: deliverable`

Targets:
- `src/gobby/servers/local_provider_models.py::discover_local_endpoint_model_group`
- `src/gobby/servers/local_provider_models.py::_local_model_entry`
- `src/gobby/servers/local_provider_models.py::_merge_default_model`
- `tests/servers/test_local_provider_models.py::*` — scope-reason: new vllm discovery fixtures alongside reworked lmstudio/ollama classification cases

Add a `vllm` branch in `discover_local_endpoint_model_group`
(`src/gobby/servers/local_provider_models.py`): `GET {origin}/v1/models`, treat
as `capability_checked` with `source: "live"`, read `max_model_len` into
`context_length` (`context_length_source: "provider_reported"`), and add
`"vllm": "vLLM"` to `LOCAL_PROVIDER_LABELS`. Health probe order: `/health`
then `/v1/models`. Modality for vllm models comes from persisted 2.3 probe
evidence merged into `_local_model_entry` **only for the entry matching
`probed_model`**; every other served model carries null modalities, and a
`probed_model` absent from the live served list merges nowhere. The
default picker option (`_merge_default_model`'s prepended
`endpoint:<name>` entry, `is_default: true`) aliases the endpoint's
configured model, and the copy is source-agnostic: after discovered
entries are assembled, the default option copies `input_modalities` from
the discovered entry it aliases — whether those came from 2.3 probe
evidence or from 3.2's advertised classification. Aliasing is per
protocol: for `protocol: vllm` the configured model — including
`model: auto` — resolves through the 1.2 resolver to a served id and
matches the entry with that `canonical_id` (never a literal comparison
against the `auto` sentinel; the resolver is consulted only for vllm
endpoints, never lmstudio/ollama discovery); for every other protocol
the alias is the entry whose `canonical_id` equals the configured model
— the match `_merge_default_model` already uses for verification. The
emission condition follows the same rule: a vllm default option is
prepended when the configured model resolves — `model: auto` with a
single served model resolves and is emitted — while an unresolvable
`auto` (resolver error on multiple served models) emits no default
option, because the existing `canonical_id == endpoint.model` arm can
never match the `auto` sentinel and an unserveable default must not be
offered. A default aliasing an entry with null modalities stays null. Without this copy, the preferred picker selection
(web's `getPreferredModelForProvider` prefers `is_default`) would stay
image-ineligible under 5.3's endpoint-option rule even after a successful
vision probe of the very model it aliases. Discovery
itself makes no modality guess. The `/v1/models` URL uses the 1.2 origin
normalization (the shared resolver helper), never a hand-built join.

**Acceptance:**

- 3.1.1 - A vllm endpoint's models appear as `source: "live"` entries with provider-reported context length. symbol: `gobby.servers.local_provider_models.discover_local_endpoint_model_group`.
- 3.1.2 - Discovery failure surfaces the endpoint group with a config-sourced fallback and an error, matching lmstudio/ollama behavior. test: `tests/servers/test_local_provider_models.py::test_vllm_discovery_error_fallback`.
- 3.1.3 - On a two-model vllm endpoint, probe-persisted modalities attach only to the probed model's entry and to the default `endpoint:<name>` option when its configured model resolves to `probed_model`; the other model's modalities are null, and a default resolving to the unprobed model stays null. A single-model vllm endpoint configured `model: auto` prepends the `endpoint:<name>` default option with modalities copied from the probed served id; a two-model endpoint configured `model: auto` (resolver error) emits no default option. test: `tests/servers/test_local_provider_models.py::test_vllm_modalities_probed_model_only`.

### 3.2 Classify VLMs instead of excluding them [category: code] (depends: 2.1, 3.1)
`kind: deliverable`

Targets:
- `src/gobby/servers/local_provider_models.py::_is_lmstudio_llm`
- `src/gobby/servers/local_provider_models.py::_local_model_entry`
- `src/gobby/servers/routes/providers.py::create_providers_router`

Fix `_is_lmstudio_llm` in `src/gobby/servers/local_provider_models.py`: accept
`type: "vlm"` (LM Studio's label for every vision-capable model) as an eligible
chat model, dropping the dead `"vision"` exclusion token. `_local_model_entry`
emits `input_modalities`: `["text","image"]` for LM Studio `vlm` types and for
Ollama models whose capabilities include `"vision"`; `["text"]` otherwise.
Advertised type/capability labels are a hint with one authority rule: when
2.3 probe evidence exists for a model (`probed_model` match), the evidence
wins — including a text-only degrade that contradicts an advertised `vlm` —
and models with neither evidence nor advertisement stay null (never
image-eligible, no chips). The default `endpoint:<name>` option follows
the same authority rule through 3.1's source-agnostic default-option copy
in `_merge_default_model`: whatever modalities the aliased discovered
entry carries — advertised, or probe-evidence-won including a text-only
degrade — copy onto the default option. For lmstudio/ollama the alias is
the entry whose `canonical_id` equals the configured model; no vLLM
resolver is involved. This section changes only the discovered-entry
classification; the copy itself lands in 3.1 and needs no edit here.
The local-group payloads in `src/gobby/servers/routes/providers.py` carry the
field (today only the Responses path emits modalities). Embedding, rerank, and
tts exclusions stay.

**Acceptance:**

- 3.2.1 - LM Studio `type: "vlm"` models appear in discovery tagged `input_modalities: ["text","image"]`. test: `tests/servers/test_local_provider_models.py::test_lmstudio_vlm_classified`.
- 3.2.2 - Ollama models with a `vision` capability carry image modality; `/api/providers/models` local entries expose `input_modalities`. file: `src/gobby/servers/routes/providers.py`.
- 3.2.3 - `/api/providers/models` `input_modalities` agree with the 2.1 image-routing predicate's decisions for vllm, lmstudio, and ollama catalogs, including each group's default `endpoint:<name>` option after an image-capable advertisement or probe of the model it resolves to. test: `tests/servers/test_local_provider_models.py::test_modalities_match_routing_predicate`.

## P4: Web Chat and Agent Transport
`kind: framing`

**Goal**: vLLM endpoints are web-chat and agent-spawn eligible through Codex
config-override transport, at parity with lmstudio/ollama.

### 4.1 Codex chat-wire transport for vllm endpoints [category: code] (depends: 1.1, 1.2, 3.2)
`kind: deliverable`

Targets:
- `src/gobby/ai/codex_endpoint.py::codex_endpoint_config_overrides`
- `src/gobby/agents/codex_oss.py::codex_oss_provider_for_local_endpoint`
- `src/gobby/servers/websocket/chat/runtime_manager.py::WebChatRuntimeManager`
- `src/gobby/servers/routes/providers.py::create_providers_router`
- `src/gobby/agents/codex_oss.py::codex_oss_launch_args`

Generalize `codex_endpoint_config_overrides` in `src/gobby/ai/codex_endpoint.py`
to emit chat-wire provider blocks:

```
-c model_providers.gobby-vllm-<endpoint>.name="vLLM (<endpoint>)"
-c model_providers.gobby-vllm-<endpoint>.base_url="<api_base>"
-c model_providers.gobby-vllm-<endpoint>.wire_api="chat"
-c model_provider="gobby-vllm-<endpoint>"
```

`WebChatRuntimeManager` builds the vllm endpoint backend with these overrides
instead of `--oss`; lmstudio/ollama keep `codex_oss_launch_args`. The
`model=` override always carries a concrete served-model id resolved through
the 1.2 resolver (web chat's `attach_session` pre-resolve included) —
skipping the warmup path never lets the literal `auto` reach the override
block. Replace the
`routable = provider_type in CODEX_OSS_LOCAL_PROVIDERS` gate in
`src/gobby/servers/routes/providers.py` with a per-protocol transport-strategy
lookup (oss for lmstudio/ollama, config-override for vllm, none for generic).
The strategy lives beside `codex_oss_provider_for_local_endpoint` in
`src/gobby/agents/codex_oss.py` so every caller — web-chat runtime, spawn
resolution, and resume — picks oss versus config-override from one helper.
The depends edge on 3.2 serializes this section's `providers.py` edits after
3.2's.
Add an endpoint-provider branch to `WebChatRuntimeManager.health()`. vllm
endpoints skip only load/swap/keep-alive warmup behavior (no load API;
server owns model residency) — the existing
`CodexWebChatBackend.attach_session` call to `ensure_local_model` stays in
place, because 1.2 makes that call a resolve-only pre-flight for vllm and
it is the path that resolves `model: auto` before the override block.
Skip-warmup never means omitting `ensure_local_model`.
API-key handling: pass through `env_key` only when the endpoint has an api_key,
mirroring the Responses mechanism — the provider block references only the
environment-variable name, the key is placed in the child-process environment,
and the secret never appears in argv, serialized `-c` values, or diagnostic
output. Whenever `env_key` is emitted, the same override set emits
`shell_environment_policy.exclude` for that variable (exactly as the
Responses helper does today), so the key is also invisible to shells Codex
spawns.

**Acceptance:**

- 4.1.1 - A vllm endpoint with an eligible chat model is web-chat routable and executes through Codex with config-override transport. symbol: `gobby.ai.codex_endpoint.codex_endpoint_config_overrides`.
- 4.1.2 - lmstudio/ollama web chat still uses `--oss --local-provider`; generic openai-compatible remains catalog-only. test: `tests/servers/test_local_llm.py::test_routable_transport_strategies`.
- 4.1.3 - `health()` reports endpoint-backend status instead of falling through to `unknown`. file: `src/gobby/servers/websocket/chat/runtime_manager.py`.
- 4.1.4 - Authenticated vllm endpoints keep `wire_api="chat"` with credentials referenced only via `env_key`: the key lands in the child-process environment, never in argv, serialized `-c` values, or diagnostics, and the override set excludes the variable via `shell_environment_policy.exclude`. test: `tests/servers/test_local_llm.py::test_vllm_env_key_credential_transport`.

### 4.2 Agent-spawn parity for vllm endpoints [category: code] (depends: 1.2, 4.1)
`kind: deliverable`

Targets:
- `src/gobby/agents/spawners/command_builder.py::build_cli_command`
- `src/gobby/mcp_proxy/tools/spawn_agent/_generation_endpoint.py::resolve_spawn_generation_endpoint`
- `src/gobby/agents/resume_executor.py::*` — scope-reason: the resume path independently selects the OSS provider today and must adopt the shared 4.1 transport strategy

Spawned Codex agents targeting a vllm endpoint receive the same
`-c model_providers.*` override block from `command_builder`
(`src/gobby/agents/spawners/command_builder.py`) instead of
`--oss --local-provider`. `build_cli_command` only emits what its caller
selects: `resolve_spawn_generation_endpoint` and the resume executor both
set `codex_oss_provider` today, so both switch to the shared 4.1 transport
strategy — vllm endpoints yield config-override args, never `--oss`.
Pre-flight uses the 1.2 lifecycle rules, and the `model=` override value
comes from the 1.2 resolver (`ensure_local_model` already runs pre-flight;
that symbol itself is 1.2's target, not this section's).

**Acceptance:**

- 4.2.1 - Spawning a Codex agent against `endpoint:<vllm>/<model>` emits config-override args and no `--oss` flag. file: `src/gobby/agents/spawners/command_builder.py`.
- 4.2.2 - Spawned-agent commands for authenticated vllm endpoints carry the key only in the child-process environment via `env_key`; argv and serialized `-c` values stay secret-free, and `shell_environment_policy.exclude` covers the variable. test: `tests/agents/test_command_builder.py::test_vllm_spawn_env_key_transport`.

## P5: Web UI
`kind: framing`

**Goal**: vLLM endpoints are configurable and legible across settings, pickers,
and badges.

### 5.1 Settings: schema-derived protocols and wire_api field [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `web/src/components/settings/sections/ProvidersModelsSection.tsx::GenerationEndpointEditor`
- `web/src/components/settings/sections/ProvidersModelsSection.tsx::GenerationGroup`
- `web/src/lib/providerModels.ts::getProviderDisplayName`
- `web/src/components/settings/sections/__tests__/ProvidersModelsSection.test.tsx::*` — scope-reason: protocol-option and wire_api assertions across the suite

Derive the protocol dropdown in `GenerationEndpointEditor`
(`web/src/components/settings/sections/ProvidersModelsSection.tsx`) from
`/api/config/schema` via the existing `enumOptionsAt()`
(`web/src/components/settings/configSchema.ts`), deleting the hand-mirrored
`ENDPOINT_PROTOCOL_OPTIONS` list. Add the missing `wire_api` select to the
editor (values from schema; hidden or disabled to `chat-completions` for
non-openai-compatible protocols per the daemon validator). Protocol
transitions are pinned: switching an existing endpoint to vllm immediately
writes `wire_api: "chat-completions"` into form state and the submitted
payload (a stale `responses` value must never survive the switch, even while
the control is hidden or disabled); switching back to openai-compatible
re-exposes the schema-provided choices. Add
`vllm: "vLLM"` to `ENDPOINT_LABELS` in `web/src/lib/providerModels.ts`.
The editor also deletes its `vision_extract` toggle: the field is removed by
2.3 (this section lands first — 2.3 depends on it), capability display moves
to the probed-evidence chips (5.3), and a payload still carrying
`vision_extract` would 422 against the daemon's `extra=forbid` config models
once the field is gone.

**Acceptance:**

- 5.1.1 - The protocol dropdown lists vllm sourced from the daemon schema, with no hardcoded protocol list remaining in the component. file: `web/src/components/settings/sections/ProvidersModelsSection.tsx`.
- 5.1.2 - The editor exposes wire_api and enforces chat-completions for vllm. test: `web/src/components/settings/sections/__tests__/ProvidersModelsSection.test.tsx`.
- 5.1.3 - Switching a protocol to vllm writes `wire_api: "chat-completions"` into the submitted payload and switching back restores schema-provided choices, asserted on the saved request payload rather than only the rendered select. test: `web/src/components/settings/sections/__tests__/ProvidersModelsSection.test.tsx`.
- 5.1.4 - The editor no longer renders or submits `vision_extract`; saved payloads for every protocol omit the field. test: `web/src/components/settings/sections/__tests__/ProvidersModelsSection.test.tsx`.

### 5.2 Icons and provider badges [category: code] (depends: 3.1)
`kind: deliverable`

Targets:
- `web/src/components/shared/SourceIcon.tsx::SourceIcon`
- `web/src/styles/base.css`
- `web/src/components/activity/SessionsTab.helpers.tsx::isLocalLegacyFallback`

Add a vllm branch to `SourceIcon` (`web/src/components/shared/SourceIcon.tsx`)
with a `.source-icon-vllm` selector in `web/src/styles/base.css`, and add
`"vllm"` to `LOCAL_LEGACY_PROVIDERS` in
`web/src/components/activity/SessionsTab.helpers.tsx` so vllm-backed sessions
classify as local. Follow `.impeccable.md` and the impeccable skill for the
icon treatment (deutan-safe, both themes).

**Acceptance:**

- 5.2.1 - vllm endpoint groups render a distinct icon in the provider picker and session badges classify vllm sessions as local. file: `web/src/components/shared/SourceIcon.tsx`.

### 5.3 Text/Image capability chips [category: code] (depends: 2.3, 3.2, 5.1, 5.2)
`kind: deliverable`

Targets:
- `web/src/components/chat/ProviderPicker.tsx::ProviderPicker`
- `web/src/lib/providerModels.ts::modelSupportsImageInput`
- `web/src/components/settings/sections/ProvidersModelsSection.tsx::GenerationGroup`
- `web/src/styles/base.css`
- `web/src/components/chat/__tests__/ProviderPicker.test.tsx::*` — scope-reason: chip rendering cases across probe-sourced and advertised modality fixtures
- `web/src/components/settings/sections/__tests__/ProvidersModelsSection.test.tsx::*` — scope-reason: endpoint-row chip assertions alongside existing group tests

Render compact Text/Image capability chips on the existing generation-endpoint
rows (`GenerationGroup`) and local-model rows (`ProviderPicker` model options)
from the already-returned `input_modalities` field — vLLM probe-persisted
metadata (2.3) and LM Studio/Ollama advertised metadata (3.2). Models with
null/absent modalities render no chips (no guessing).
`modelSupportsImageInput` in `web/src/lib/providerModels.ts` adopts the same
rule for **every endpoint-backed model option**, decided per option, never
per provider entry: an option is endpoint-backed exactly when its value or
its owning provider entry's name starts with `endpoint:` (the
`endpoint_provider` prefix). On those options, null or absent
`input_modalities` means not image-capable, matching 2.3.10 (a Responses
endpoint without evidence has no image modality) and 3.2. This covers
vllm, lmstudio, ollama, generic openai-compatible groups (provider
`endpoint:<name>`), and responses-wire options (value
`endpoint:<name>/<model>`). Entry-level keys cannot express the rule:
`/api/providers/models` appends responses-wire `endpoint:*` options into
the existing `provider=codex` entry alongside cloud collector models, and
available local groups also carry `execution_provider=codex` — a rule on
`provider=codex` or `execution_provider=codex` either strips image attach
from cloud Codex models that legitimately omit the field or leaves
endpoint options on the cloud fallback, and neither satisfies 5.3.3. The
missing-means-true fallback survives only for non-endpoint options (cloud
collector catalogs that never carry a modalities field); a provider-name
allowlist (vllm/lmstudio/ollama) fails the same way. The
depends edge on 5.1 serializes this section's `ProvidersModelsSection`
component and test edits after 5.1's; the depends edge on 5.2 serializes
this section's `web/src/styles/base.css` chip styles after 5.2's icon
selectors in the same file. No new registry,
endpoint, or configuration surface. Follow `.impeccable.md` and the impeccable
skill for the chip treatment (deutan-safe, both themes).

**Acceptance:**

- 5.3.1 - Endpoint and local-model rows render Text/Image chips from `input_modalities`, covering vllm probe metadata and LM Studio/Ollama advertised metadata — including the preferred default `endpoint:<name>` option after an image-capable probe or `vlm` advertisement of the model it resolves to, with the vllm default-option fixture configured `model: auto` on a single-model endpoint so the copy exercises 1.2 resolution rather than a literal id match, while a second unprobed model on the same endpoint renders none; null modalities render no chips. test: `web/src/components/chat/__tests__/ProviderPicker.test.tsx`.
- 5.3.2 - No new registry or configuration surface is introduced; chips read the existing `/api/providers/models` payload. file: `web/src/components/settings/sections/ProvidersModelsSection.tsx`.
- 5.3.3 - Image-attachment eligibility follows the chips per model option: an option whose value or owning provider starts with `endpoint:` and has null or absent `input_modalities` is not image-eligible — asserted for a responses-wire `endpoint:*` option with null modalities inside the `provider=codex` entry, a cloud Codex option in that same entry that omits the field and keeps the current fallback, a generic openai-compatible `endpoint:*` group with null modalities, and a default `endpoint:<name>` option carrying probe-copied `["text","image"]` modalities that is image-eligible. test: `web/src/lib/__tests__/providerModels.test.ts`.

## P6: Documentation
`kind: framing`

**Goal**: Operators can configure canonical vLLM and vllm-metal from docs alone.

### 6.1 vLLM and vllm-metal guides [category: docs] (depends: 1.2, 2.3, 3.1, 4.1, 4.2)
`kind: deliverable`

Targets:
- `docs/guides/llm-features.md`
- `docs/guides/providers-and-models.md`
- `docs/guides/system-requirements.md`

Extend `docs/guides/llm-features.md` with a vllm endpoint example (protocol,
wire_api, api_base `http://localhost:8000/v1`, api-key note), a vllm-metal
section (Apple Silicon/MLX install pointer, same protocol value), and the
non-owning boundary (Gobby never starts/stops vLLM). Include paired selector
examples — `model: auto` and an explicit endpoint model — stating that auto
succeeds only when `GET /v1/models` returns exactly one served model
(zero or multiple is an error naming the served models), plus a short
diagnostic step listing served model IDs before activation using the
normalized origin — `curl {origin}/v1/models`, e.g.
`curl http://localhost:8000/v1/models` — never `{api_base}/v1/models`, which
doubles the suffix when `api_base` already ends in `/v1`. Keep the non-owning startup boundary and API-key environment
handling in the same example. Retire the existing `vision_extract: true`
operator instruction in `docs/guides/llm-features.md` (2.3 deletes that
config field under `extra=forbid`; a copy-pasted `vision_extract:` key would
422 on save): describe vision availability as probe-derived — the
`vision_extract` capability follows probed or advertised image modality —
and keep the field out of every copy-pasteable endpoint YAML example. Update
`docs/guides/providers-and-models.md` web-chat backend list with the
config-override transport and `docs/guides/system-requirements.md` runtime
table.

**Acceptance:**

- 6.1.1 - A copy-pasteable vllm endpoint config with selector examples exists. file: `docs/guides/llm-features.md`.
- 6.1.2 - Web-chat backend docs describe the Codex config-override transport for vllm. file: `docs/guides/providers-and-models.md`.
- 6.1.3 - Docs state the `model: auto` exactly-one rule with its multi-model failure mode and a served-model listing diagnostic. file: `docs/guides/llm-features.md`.
- 6.1.4 - No guide instructs setting a `vision_extract` config field; vision availability is described via probed or advertised image modality, and no endpoint YAML example carries the key. file: `docs/guides/llm-features.md`.

## E1: End-to-End Verification
`kind: verification`

Fixture-driven tests are mandatory on every host and cover the full protocol
path (the wire behavior is identical across canonical vLLM and vllm-metal):
LM Studio VLM classification with a `type: "vlm"` fixture, Ollama vision
capability mapping with a `capabilities: ["completion","vision"]` fixture, and
vllm discovery/activation/generation against recorded `/v1/models` and
chat-completions fixtures.

Live-runtime verification is a two-row matrix. Each row stays **explicitly
unverified** until evidence — runtime and model versions, the exact commands
run, and per-check results — is recorded on matching hardware; fixture
coverage never marks a live row verified:

| Row | Hardware | Runtime | Evidence status |
|-----|----------|---------|-----------------|
| L1 | CUDA host | canonical vLLM | unverified until recorded |
| L2 | Apple Silicon | vllm-metal (MLX) | unverified until recorded |

Each live row records the same check sequence: configure
`ai.generation.endpoints.vllm-local`; health probe (`/health` then
`/v1/models`); model discovery listed as `source: "live"` in
`/api/providers/models`; activation persisting `input_modalities`; a text and
an image `POST /api/llm/generate` against `endpoint:vllm-local/<model>`;
`/api/llm/vision/extract` through the preset path; web chat on the vLLM group
via Codex config-override transport; and a Codex agent spawn against the
endpoint.

## Task Mapping
`kind: framing`

<!-- Updated after task creation -->
| Plan Item | Task Ref | Status |
|-----------|----------|--------|

## V1 Plan Changelog
`kind: verification`

<!-- Rounds appended by append_plan_changelog_round -->

**Round 1** `kind: enhancement`

- enhancer_run: 46887d45-41dc-45da-9fd0-67cbba039eb6
- enhancer_session: 16c8fa86-d136-48db-9f95-9fabc8ad4cb5
- converged: false
- suggestions_presented: 6
- accepted:
  - E1 / better / env_key credential-transport boundary pinned for web chat and agent spawn (4.1.4, 4.2.2)
  - E2 / better / explicit image-routing predicate with mixed-catalog tests and metadata consistency (2.1, 2.1.4–2.1.5, 3.2.3)
  - E3 / better / E1 restructured into a two-row live verification matrix with recorded evidence
  - E4 / better / wire_api protocol-transition behavior pinned on the submitted payload (5.1, 5.1.3)
  - E5 / better / docs gain model:auto exactly-one rule, failure mode, and served-model diagnostic (6.1, 6.1.3)
  - E6 / bigger / Text/Image capability chips from input_modalities (new 5.3)
- declined: none
- resolution_notes: All six suggestions accepted by the user and folded into P2, P3, P4, P5 (including new deliverable 5.3), P6, and the E1 verification matrix. No suggestions declined; round cap (1) reached.

**Round 1** `kind: verification`

- reviewer_run: 2f4e3ea8-bf6f-47e9-9ed7-f066162a97bf
- reviewer_session: 324fe24e-20cb-4f52-a14f-dde442314338
- verdict: needs_review
- findings:
- R1-F1-http-image-boundary / blocking / HTTP route omits the image contract — accepted; TextGeneratePayload and generate_text targets, route-level forwarding and tests added to 2.1
- R1-F2-model-scoped-modalities / blocking / endpoint-level modality evidence over-advertises every served model — accepted; probed_model-scoped evidence in 2.3, probed-model-only merge in 3.1
- R1-F3-image-input-bounds / blocking / unbounded image ingress — accepted; canonical accepted forms, 8-image count cap, 5 MiB per-image and 24 MiB aggregate limits, deterministic rejections in 2.1
- R1-F4-declared-image-transports / blocking / Claude SDK and Codex declared image-capable without wire mapping — accepted with expansion repair (user choice): new deliverable 2.5 wires SDK image blocks and codex exec --image argv
- R1-F5-native-vision-regression / blocking / vision_extract deletion regresses LM Studio/Ollama image shapes — accepted; port-then-delete ordering with native-serialization targets and tests in 2.2
- R1-F6-optional-activation-auth / blocking / activation hard-requires api_key — accepted; optional credentials with both-state tests in 2.3
- R1-F7-probe-state-invalidation / blocking / stale modality evidence survives identity changes — accepted; activation-owned evidence cleared on protocol/api_base/model/api_key change in 2.3
- R1-F8-auto-model-all-paths / blocking / auto sentinel can reach the wire and /v1 bases double to /v1/v1 — accepted; shared resolve_vllm_served_model resolver with origin normalization in 1.2
- R1-F9-activation-outcomes / blocking / probe outcomes undefined — accepted; fixed outcome table (text fatal; JSON/tool/vision degraded; tool_chat false skips; retry recovery) in 2.3
- R1-F10-dependency-order / blocking / missing producer dependencies and unordered shared-file edits — accepted; 2.3 gains 1.1, 3.1 gains 2.3, 3.2 gains 2.1 and 3.1 (serializing _local_model_entry), 6.1 depends on 1.2/2.3/3.1/4.1/4.2
- R1-F11-tool-chat-target / blocking / 2.4 targeted vision bindings instead of tool bindings — accepted with correction: the behavior lives in registry_builder.py::_generation_endpoint_tool_bindings (the adversary's fix mis-named _tool_chat_builder.py)
- R1-F12-grants-target / blocking / runtime grant derivation target missing from 2.3 — accepted; runtime_grants/service.py::_vision_extract_enabled target and grant-follows-evidence test added
- R1-F13-activation-modality-bootstrap / blocking / vision probe gated on the evidence it creates — accepted; probe-direct dispatch bypassing candidate eligibility plus bootstrap transition tests in 2.3
- R1-N1-empty-vllm-subclass / nit / behavior-free VLLMLocalProviderAdapter — accepted; 1.1 dispatches vllm directly to OpenAICompatibleLocalProviderAdapter
- resolution_notes: All 14 findings accepted by the user (13 blocking, 1 nit). Repairs applied to Constraints, 1.1, 1.2, 2.1, 2.2, 2.3, 2.4, 3.1, 3.2, 6.1, and new deliverable 2.5 (Claude SDK and Codex endpoint image transport; expansion shape chosen by the user over scope-narrowing to make cloud vision usable through the unified path). codex exec -i/--image support verified against the installed CLI; F11 target corrected to registry_builder.py after gcode verification. Round 1 of 5 finalized; the next round reviews the repaired plan.

```json plan-review-round
{"evidence_id":"1c5fed93-e68c-43da-8ea4-907a489c98be","plan_hash":"a05690bdf4b82d8ec4b0a5d6b44c12391824d2bda4959e3240b5358587ce401d","round_number":1,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"cddb9a57976cb04d87f6009390999346e65da8686cc27bf02ebbd5bdbf948ea8","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":6,"emitted_findings":14,"total":20},"evidence_id":"1c5fed93-e68c-43da-8ea4-907a489c98be","lanes":[{"candidate_count":4,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":5,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":11,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":14,"manifest_digest":"d905497a1d42b0c17a376b7cf16fa6debbcc8aab925b64c456673b591bc3c531","status":"valid"},"source_digest":"e57dd93f34d3da5b7b1ad4f8a3c58a2090270aca43894f93d60f16338902ab72","version":1},"findings":[{"category":"missing-requirement","check_key":"http-image-contract","description":"UI-TARS depends on live HTTP image generation, yet section 2.1 omits TextGeneratePayload and create_llm_router from Targets and never defines the HTTP image representation, validation, or forwarding.","finding_id":"R1-F1-http-image-boundary","fix":"Add the route payload and handler targets, specify the HTTP image field and validation semantics, map it to TextGenerationRequest.images, and require route-level image request tests.","location":"P2 / § 2.1","prevention":"Trace each acceptance input from public schema through route mapping to the internal request and add a boundary test.","principle":"A public acceptance path must expose and forward every input required by its downstream contract.","root_cause":"The plan adds images only to the internal request while its live HTTP acceptance path still uses a payload and route mapping without images.","section_id":"2.1","severity":"blocking"},{"category":"missing-requirement","check_key":"model-scoped-capability-metadata","description":"A vision probe for the configured model would cause every model returned by the vLLM endpoint to be advertised as image-capable, making the UI-TARS model contract inaccurate.","finding_id":"R1-F2-model-scoped-modalities","fix":"Store modality evidence per model, or apply the probe result only to the configured model while leaving other discovered models unknown; add a mixed-capability two-model fixture.","location":"P2 / § 2.3 and P3 / § 3.1","prevention":"Test capability discovery with two served models that have different modality support.","principle":"Capability evidence must be scoped to the resource actually probed.","root_cause":"The plan stores input_modalities on a multi-model endpoint and merges that value into every discovered model.","section_id":"2.3","severity":"blocking"},{"category":"unhandled-edge","check_key":"bounded-image-ingress","description":"Image normalization is underspecified for malformed data URLs, MIME handling, base64 decoding, inaccessible paths, excessive image counts, and aggregate decoded bytes.","finding_id":"R1-F3-image-input-bounds","fix":"Define accepted path and data-URL forms, canonical MIME/base64 output, allowed path behavior, maximum image count, per-image decoded-byte limit, aggregate-byte limit, and deterministic rejection tests.","location":"P2 / § 2.1","prevention":"Enumerate empty, malformed, oversized, excessive-count, and mixed-form image inputs in acceptance tests.","principle":"Binary request inputs need canonical normalization and explicit count and size limits at ingress.","root_cause":"The plan names paths and data URLs without defining path policy, decoded-size accounting, list bounds, or aggregate request bounds.","section_id":"2.1","severity":"blocking"},{"category":"missing-requirement","check_key":"image-transport-parity","description":"The Claude SDK and Codex paths are advertised as image-capable, yet the plan lacks concrete targets and payload mapping for their actual text-generation transports.","finding_id":"R1-F4-declared-image-transports","fix":"Name and target the Claude SDK and Codex text-generation paths, specify normalized image-part mapping for each, and add transport-specific request-shape tests.","location":"P2 / §§ 2.1–2.2","prevention":"Inventory each eligible transport and require one request-shape test for its image mapping.","principle":"Every transport declared capable of an input must define how that input reaches its wire request.","root_cause":"The plan declares Claude SDK and Codex image eligibility while targeting only the shared request contract and local OpenAI-compatible adapter path.","section_id":"2.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"vision-transport-migration","description":"LM Studio and Ollama currently use provider-specific image request shapes. Their classification as image-capable would regress once vision_extract is removed unless those shapes are ported first.","finding_id":"R1-F5-native-vision-regression","fix":"Port both providers' native image serialization into generate_text_result before deleting vision_extract, and require regression tests for LM Studio and Ollama image requests.","location":"P2 / § 2.2 with §§ 2.1, 2.3 and 3.2","prevention":"Compare old and new wire payloads for each provider before deleting a specialized path.","principle":"Deleting a specialized path requires a behavior-preserving migration for every existing implementation.","root_cause":"The plan removes vision_extract while leaving LM Studio and Ollama native image serialization absent from the unified generation path.","section_id":"2.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"optional-endpoint-auth","description":"An unauthenticated local vLLM endpoint cannot activate through the current credential precondition, despite the plan promising optional API keys.","finding_id":"R1-F6-optional-activation-auth","fix":"Specify optional credentials for vLLM/local activation, add authentication headers only when configured, and add startup and direct-activation tests for both credential states.","location":"P2 / § 2.3 and P4 / § 4.1","prevention":"Test activation with authenticated and unauthenticated endpoint configurations.","principle":"Authentication preconditions must match endpoint configuration semantics.","root_cause":"Activation currently requires an API key while the planned local vLLM configuration makes credentials optional.","section_id":"2.3","severity":"blocking"},{"category":"unhandled-edge","check_key":"probe-derived-state-freshness","description":"Settings changes can leave stale image capability metadata visible in model discovery and UI chips, including after a failed probe against the replacement endpoint.","finding_id":"R1-F7-probe-state-invalidation","fix":"Make modality metadata activation-owned, clear and reprobe it when protocol, api_base, model, or api_key changes, and test stale-state removal plus recovery.","location":"P2 / § 2.3 and P5 / §§ 5.1, 5.3","prevention":"For every persisted probe field, list its identity inputs and test mutation, failed reprobe, and recovery.","principle":"Derived capability state must be invalidated whenever its evidence identity changes.","root_cause":"The plan persists activation-owned modalities without a transition for protocol, base URL, model, or credential changes.","section_id":"2.3","severity":"blocking"},{"category":"unhandled-edge","check_key":"model-auto-resolution","description":"Direct request paths can send model `auto` to vLLM, and an api_base already ending in `/v1` can produce `/v1/v1/models` under the stated URL construction.","finding_id":"R1-F8-auto-model-all-paths","fix":"Define one normalized-origin served-model resolver shared by lifecycle, activation, text, tool, vision, and config-override paths; add coverage for base URLs with and without `/v1`.","location":"P1 / § 1.2 with §§ 2.1, 2.3, 2.4, 4.1 and 4.2","prevention":"Exercise every request entry point with model:auto and base URLs both with and without a trailing /v1.","principle":"A sentinel configuration value must resolve before every path that emits it on the wire.","root_cause":"The plan resolves model:auto inside local lifecycle setup while direct text, tool, vision, and activation paths can still use the literal sentinel; its discovery URL rule can also append /v1 twice.","section_id":"1.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"activation-outcome-table","description":"The plan leaves ambiguous whether activation succeeds after JSON, tool, or vision failure, how failed capabilities are removed, how tool_chat:false is skipped, and what a later successful retry restores.","finding_id":"R1-F9-activation-outcomes","fix":"Add a complete probe outcome table and tests defining fatal and degraded states, tool_chat:false skip behavior, failed-capability removal from metadata and grants, persisted status, and retry recovery.","location":"P2 / §§ 2.3–2.4","prevention":"Maintain an activation outcome table covering success, skipped, failed, persisted state, grants, and retry recovery for each probe.","principle":"Multi-probe activation requires an explicit state transition for every probe outcome.","root_cause":"The plan adds text, JSON, tool, and vision probes without defining fatal versus degraded outcomes, cleanup, or recovery.","section_id":"2.3","severity":"blocking"},{"category":"bad-sequencing","check_key":"dependency-consumer-order","description":"Activation can run before the vLLM adapter exists, model discovery can run before modality metadata exists, LM Studio/Ollama classification can precede the unified image contract, shared _local_model_entry edits are unordered, and docs can precede behaviors they describe.","finding_id":"R1-F10-dependency-order","fix":"Add explicit dependencies from 2.3 to 1.1, from 3.1 to 2.3, from 3.2 to 2.1, serialize 3.1 and 3.2, and make 6.1 depend on every documented behavior section.","location":"P2 / § 2.3; P3 / §§ 3.1–3.2; P6 / § 6.1","prevention":"For each deliverable, map consumed symbols and acceptance behaviors to their producer sections and shared-file writers.","principle":"A deliverable must depend on the sections that establish every contract it consumes and serialize shared-target edits.","root_cause":"Several dependency lists omit upstream adapter, multimodal, and metadata work while sections 3.1 and 3.2 both edit _local_model_entry.","section_id":"3.1","severity":"blocking"},{"category":"gobby-format","check_key":"target-symbol-correctness","description":"The targeted `_generation_endpoint_vision_bindings` symbol cannot implement the planned tool_chat availability rule; the behavior is built by `_generation_endpoint_tool_bindings`.","finding_id":"R1-F11-tool-chat-target","fix":"Replace the vision-binding target with the exact `_generation_endpoint_tool_bindings` target in `_tool_chat_builder.py` and include its focused availability tests.","location":"P2 / § 2.4","prevention":"Resolve the changed symbol in its file and inspect its callers before finalizing each Targets block.","principle":"Exact Targets must name the implementation symbol that the deliverable changes.","root_cause":"Section 2.4 targets vision bindings even though tool_chat availability is constructed in the tool binding builder.","section_id":"2.4","severity":"blocking"},{"category":"gobby-format","check_key":"target-inventory-completeness","description":"The implementing leaf would lack the runtime grant service target needed to apply probe-derived capability grants, leaving acceptance impossible from the section alone.","finding_id":"R1-F12-grants-target","fix":"Add the exact grant-derivation symbol in `src/gobby/runtime_grants/service.py` and focused grant tests to section 2.3 Targets.","location":"P2 / § 2.3","prevention":"Cross-check every imperative implementation statement against the section's exact Targets and tests.","principle":"A self-contained deliverable must list every production file its body explicitly requires changing.","root_cause":"Section 2.3 requires endpoint grant derivation changes while omitting runtime_grants/service.py from Targets.","section_id":"2.3","severity":"blocking"},{"category":"bad-sequencing","check_key":"modality-probe-bootstrap","description":"A fresh vLLM endpoint cannot reach its vision probe through the planned normal image path because it lacks the image modality flag required by that path.","finding_id":"R1-F13-activation-modality-bootstrap","fix":"Define a probe-only direct transport or explicit normal-eligibility bypass, then persist the outcome; test unknown-to-image-capable and failed-to-text-only transitions.","location":"P2 / §§ 2.1 and 2.3","prevention":"Trace first-activation state from unknown capability through probe dispatch and persisted eligibility.","principle":"A capability probe must have a bootstrap path independent of the capability state it establishes.","root_cause":"Normal image candidate selection requires image metadata, while the activation vision probe is the operation intended to create that metadata.","section_id":"2.3","severity":"blocking"},{"category":"over-engineering","check_key":"single-implementation-abstraction","description":"The empty vLLM subclass adds an abstraction with no current wire behavior or consumer beyond the existing OpenAI-compatible adapter.","finding_id":"R1-N1-empty-vllm-subclass","fix":"Dispatch `vllm` directly to `OpenAICompatibleLocalProviderAdapter` and let protocol metadata drive probing and discovery; introduce a vLLM subclass when a concrete divergent behavior arrives.","location":"P1 / § 1.1","prevention":"Require every new subclass to name its current divergent method or second concrete consumer.","principle":"A new adapter type must add present behavior, a boundary, or a concrete in-scope consumer.","root_cause":"The proposed VLLMLocalProviderAdapter is intentionally behavior-free and justified only as a future seam.","section_id":"1.1","severity":"nit"}],"reviewer_session":"324fe24e-20cb-4f52-a14f-dde442314338","round":1,"verdict":"needs_review"},"session_id":"e39817b3-864e-42b6-87a5-e67e18308b6f"}
```

**Round 2** `kind: verification`

- reviewer_run: eb080b8d-bc03-4d02-b3ca-20c4995dcf6d
- reviewer_session: 8d05aa99-6141-4b67-acc1-677e7cbf8449
- verdict: needs_review
- findings:
- R2-F1-image-allowlist-targets / blocking / 2.2 and 2.5 extend the 2.1 image-eligible allowlist without listing its owner — accepted; `TextGenerationService` added to 2.2 and 2.5 Targets with allowlist-admission acceptance items 2.2.4 and 2.5.4
- R2-F2-cli-spawn-image-eligibility / blocking / 2.1 CLI-spawn skip contradicts 2.5's image-eligible CLI transports — accepted; predicate re-keyed on binding metadata (provider, endpoint presence, wire_api; adapter class, AIAdapterStyle, and _SPAWN_COLD_ADAPTER_STYLES are non-keys) in Constraints and 2.1, generic provider=codex without endpoint metadata never eligible, new tests 2.1.8 and 2.5.4
- R2-F3-config-write-invalidation-path / blocking / identity invalidation targeted the activate route while settings save via PATCH /api/config/values — accepted with correction: the shared mutation layer is `ConfigValuesService.patch_flat` in `src/gobby/config/values.py` (the adversary named ConfigService; gcode verification corrected the class); target added to 2.3, invalidation respecified on patch_flat covering PATCH/activate/import, 2.3.5 re-asserted through PATCH with no follow-up activation
- R2-F5-base-css-unserialized / blocking / 5.2 and 5.3 both edit web/src/styles/base.css unserialized — accepted; 5.3 gains depends: 5.2 with the serialization rationale in the body (no critical-path lengthening)
- R2-F6-vision-extract-load-migration / blocking / deleting vision_extract under extra=forbid breaks stored-document loads — accepted; 2.3 specifies a pre-validation load strip so pre-existing YAML/DB documents keep loading as probe-unknown, new acceptance 2.3.11
- R2-N1-warmup-skip-wording / nit / skip-warmup could be read as omitting ensure_local_model — accepted; 4.1 pins the attach_session ensure_local_model call as resolve-only kept behavior, skip covers only load/swap/keep-alive
- resolution_notes: All 6 findings accepted by the coordinator under the user's unattended directive (5 blocking, 1 nit; votes recorded per-finding above). Three blocking findings (R2-F1, R2-F2, R2-F3) are fixer-induced defects causal to round-1 repairs R1-F4 and R1-F7. This round finalized on attempt 6: attempts 1-5 died to spawner/enforcement/runtime failures (their evidence rows were expired and count no round); 19 draft findings salvaged from those expired attempts were folded into the artifact as pre-review repairs before this round's evidence was prepared, and this round's adversary reviewed that already-repaired artifact — its 16 candidates dismissed 10 and emitted the 6 findings above. Mid-run the daemon restarted and the adversary's derive call failed DAEMON_UNAVAILABLE; the coordinator directed an in-flight retry and the same run produced the conclusive verdict with a valid 15-entry shadow manifest. Round 2 finalized; the next round reviews the repaired plan.

```json plan-review-round
{"evidence_id":"c4e7bd17-c812-4d57-a2ba-f842b3a585e2","plan_hash":"5665a7907d2c0d802c49b462c64682b82b80deac7a30f6f32ebc4c6eaa157b07","round_number":2,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"87a5ba44e58cdf853d27fd10249c71c976610361f81f8ca05daa21a257453b04","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":10,"emitted_findings":6,"total":16},"evidence_id":"c4e7bd17-c812-4d57-a2ba-f842b3a585e2","lanes":[{"candidate_count":4,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":6,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":6,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":15,"manifest_digest":"a1488140f70dd639bf3c368a4fd95c1a69bbda2805c5a04190381ae2ce71ba81","status":"valid"},"source_digest":"da85393c09ea4fed4a58ea5c07c135c0bcd310f40cfc5d71127b0dc385372de0","version":1},"findings":[{"category":"gobby-format","causal_finding_id":"R1-F4-declared-image-transports","causal_section_ids":["2.1","2.2","2.5"],"check_key":"target-inventory-completeness","description":"Sections 2.2 and 2.5 each say they extend the 2.1 image-eligible transport set when their wire mappings land, but the predicate is specified on TextGenerationService (a 2.1-only target). An agent who sees only 2.2 or 2.5 has no listed symbol to update, so LM Studio, Ollama, Claude SDK, and the Codex endpoint can land image serialization without ever becoming eligible.","finding_id":"R2-F1-image-allowlist-targets","fix":"Add `src/gobby/ai/_text_generation_service.py::TextGenerationService` (or the exact allowlist symbol 2.1 introduces) to 2.2 and 2.5 Targets, and require an acceptance item that the predicate admits the newly proven transport.","introduced_in_round":1,"location":"P2 / §§ 2.2 and 2.5","prevention":"When a later leaf extends a set defined in an earlier leaf, add that set's exact file-qualified Target to the extending section.","principle":"A self-contained deliverable must list every production file its body requires changing.","root_cause":"Round-1 repairs told 2.2 and 2.5 to extend the 2.1 image-eligible allowlist that lives in TextGenerationService, but those sections still omit that symbol.","section_id":"2.5","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"R1-F4-declared-image-transports","causal_section_ids":["2.1","2.5"],"check_key":"image-transport-parity","description":"2.1 image-ingress transport coverage is not consistent with the CLI transports 2.5 marks image-eligible. Candidate selection today classifies generic `codex` as AIAdapterStyle.DAEMON via CodexCLITextGenerateAdapter and `claude` as LLM_PROVIDER via ClaudeTextGenerateAdapter; both sit in the spawn-cold set. 2.5 then wires images into those same adapters and adds them to the allowlist, while Constraints/2.1 still say CLI-spawn is always skipped. A 2.1 implementer who treats CLI-spawn as spawn-cold will skip 2.5's transports; one who allowlists adapter class or DAEMON style will admit generic `codex` CLI-spawn. The plan never states the binding identity that includes a responses-wire Codex endpoint and Claude SDK while excluding provider=codex without endpoint metadata.","finding_id":"R2-F2-cli-spawn-image-eligibility","fix":"Define the 2.1 predicate keys explicitly: allow local openai-compatible/vllm (then 2.2 lmstudio/ollama, then 2.5 provider=claude and responses-wire endpoint bindings); always skip feature CLI providers agy/droid/grok/qwen and generic provider=codex with no endpoint metadata. Do not key off CodexCLITextGenerateAdapter, AIAdapterStyle.CLI, or _SPAWN_COLD_ADAPTER_STYLES. Add tests for image-bearing generic `codex` (skipped) versus a responses-wire Codex endpoint (eligible after 2.5) and Claude SDK (eligible after 2.5).","introduced_in_round":1,"location":"P2 / §§ 2.1 and 2.5","prevention":"Name the predicate key as binding metadata (provider, endpoint, wire_api), never adapter class or spawn-cold style, and test generic codex CLI-spawn versus a responses-wire endpoint.","principle":"Every transport declared image-capable must be distinguishable from the transports the plan permanently skips.","root_cause":"2.1 says CLI-spawn candidates are always skipped while 2.5 marks Claude SDK and CodexCLITextGenerateAdapter image-eligible, but those adapters are the spawn-cold claude/codex bindings (LLM_PROVIDER/DAEMON), and generic provider=codex uses the same adapter class and DAEMON style as a responses-wire Codex endpoint.","section_id":"2.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"R1-F7-probe-state-invalidation","causal_section_ids":["2.3","5.1"],"check_key":"probe-derived-state-freshness","description":"Identity invalidation is specified on the endpoints config routes and 2.3 targets ActivateGenerationEndpointRequest plus register_generation_endpoint_routes. The settings editor (5.1) saves via PATCH /api/config/values. patch_flat already has a sibling hook (_reject_unprobed_responses_endpoints) and is the only place that sees old versus new identity across PATCH, activate's patch_flat, and YAML import. Leaving invalidation on the activate route or a model_validator fails 2.3.5: a settings save that changes protocol, api_base, model, or api_key can keep probed_model/input_modalities visible.","finding_id":"R2-F3-config-write-invalidation-path","fix":"Target ConfigService.patch_flat (or the shared mutation/validation hook it calls) in 2.3. Specify that any identity-field change in the same patch unsets probed_model, input_modalities, probed_json, and probed_tools regardless of client payload. Test 2.3.5 through PATCH /api/config/values with no follow-up activate, and cover import/replace as an adjacent write path.","introduced_in_round":1,"location":"P2 / § 2.3","prevention":"For every persisted probe field, name the shared config-mutation hook (not only the activate route) and test PATCH /api/config/values, activate, and document import.","principle":"Derived capability state must be cleared on the mutation path that settings actually use.","root_cause":"2.3.5 requires a settings save without activation to drop stale modalities, but settings write PATCH /api/config/values through ConfigService.patch_flat as field-level keys. register_generation_endpoint_routes only implements activate PUT, and GenerationEndpointConfig cannot see the previous identity on a partial patch.","section_id":"2.3","severity":"blocking"},{"category":"bad-sequencing","check_key":"shared-file-serialization","description":"5.2 adds `.source-icon-vllm` in base.css; 5.3 adds chip styles in the same file. Those leaves can be claimed in parallel and will collide in the shared worktree.","finding_id":"R2-F5-base-css-unserialized","fix":"Add `(depends: 5.2)` on 5.3 (or the reverse) so the base.css edits serialize. 5.3 already waits on 3.2, which waits on 3.1, so depending on 5.2 does not lengthen the critical path.","location":"P5 / §§ 5.2 and 5.3","participating_section_ids":["5.2","5.3"],"prevention":"After adding a shared-file writer, list every other deliverable that names that file and add a depends edge.","principle":"Two deliverables that edit the same production file must have a depends edge.","root_cause":"5.2 and 5.3 both edit web/src/styles/base.css; 5.3 depends on 2.3, 3.2, and 5.1 but not 5.2.","section_id":"5.3","severity":"blocking"},{"category":"unhandled-edge","check_key":"config-field-retirement","description":"Existing endpoints persist `vision_extract` (especially when true). After 2.3 deletes the field, DaemonConfig/GenerationEndpointConfig validation will 422 or refuse to load those documents. 5.1 stops submitting the field on new saves but does not rewrite stored rows, so a daemon restart after 2.3 can fail before activation runs.","finding_id":"R2-F6-vision-extract-load-migration","fix":"In 2.3, ignore or drop `vision_extract` on load (pre-validator alias strip or one-shot rewrite) so existing endpoint documents keep loading; add a test that a stored `vision_extract: true` document loads as probe-unknown after the field is gone.","location":"P2 / § 2.3","participating_section_ids":["2.3","5.1"],"prevention":"When removing a persisted config field, specify how existing YAML/DB values are ignored or rewritten on load.","principle":"Deleting an extra=forbid config field requires a load-time strip or migration for stored documents.","root_cause":"2.3 deletes GenerationEndpointConfig.vision_extract under extra=forbid without a stored-config migration.","section_id":"2.3","severity":"blocking"},{"category":"gobby-format","check_key":"model-auto-resolution","description":"If the 4.1 agent interprets skip-warmup as omitting ensure_local_model, model:auto can reach start_thread or the override block. The section does not target attach_session, so the safe reading is: keep the existing pre-flight and let 1.2 make it a GET /v1/models.","finding_id":"R2-N1-warmup-skip-wording","fix":"State that vllm keeps the attach_session ensure_local_model call (1.2 makes it non-loading) and must not skip that block; only skip load/swap/keep-alive behavior that 1.2 already forbids.","location":"P4 / § 4.1","prevention":"When telling a leaf to skip warmup, name the exact symbol and say whether ensure_local_model stays.","principle":"A skip-warmup instruction must not delete the path that already resolves model:auto.","root_cause":"4.1 says vllm skips the warmup path, but CodexWebChatBackend.attach_session already calls ensure_local_model for chat-completions; after 1.2 that call is resolve-only for vllm.","section_id":"4.1","severity":"nit"}],"round_number":2,"verdict":"needs_review"},"session_id":"e39817b3-864e-42b6-87a5-e67e18308b6f"}
```

**Round 3** `kind: verification`

- reviewer_run: de1d53f9-7962-41f4-bf97-0e82199bc8ef
- reviewer_session: 6176fe4b-fb67-45c8-b273-da88745fc9e7
- verdict: needs_review
- findings:
- R3-F1-vision-extract-text-binding-reader / blocking / 2.3 Targets omit `_generation_endpoint_text_bindings` (registry_builder.py), which still copies `endpoint.vision_extract` into text_generate binding metadata — post-deletion registry build AttributeErrors.
- R3-F2-yaml-import-invalidation-path / blocking / R2-F3 repair claimed YAML import flows through `patch_flat`; it flows through `ConfigDocumentsService.replace_yaml` → `replace_namespace`, so identity edits via export/edit/re-import kept stale modality evidence. Fixer-induced (introduced_in_round: 2, causal_finding_id: R2-F3-config-write-invalidation-path, causal_section_ids: 2.3).
- R3-F3-null-modality-ui-eligibility / blocking / 5.3.3 scoped missing-means-false to a vllm/lmstudio/ollama name list, leaving responses-wire and generic openai-compatible endpoint rows with null modalities image-eligible via `modelSupportsImageInput`'s missing-means-true fallback (participating_section_ids: 5.3, 2.3, 3.2).
- R3-N1-docs-vision-extract-field / nit / 6.1 never retires the `vision_extract: true` operator instruction in docs/guides/llm-features.md, which 2.3 deletes under extra=forbid (participating_section_ids: 6.1, 2.3).
- resolution_notes: Unattended round — coordinator judged all 4 findings, one vote each. R3-F1 ACCEPT: verified `_generation_endpoint_text_bindings` writes `"vision_extract": endpoint.vision_extract` at registry_builder.py:366; the key is write-only (no `metadata["vision_extract"]` reader exists), so the repair drops it from text-binding metadata rather than migrating — added the Target, extended the remaining-reader inventory, added acceptance 2.3.12 (registry build succeeds, no key in text-binding metadata). R3-F2 ACCEPT: verified `replace_yaml` reaches `replace_namespace` without entering `patch_flat`, and that the module-level `reject_unprobed_responses_endpoints` (values.py:104) is already the shared-helper precedent called from both values.py and documents.py — rewrote the 2.3 invalidation paragraph onto a shared identity-clearing helper beside it, called from both `ConfigValuesService.patch_flat` and `ConfigDocumentsService.replace_yaml`; added the replace_yaml Target; 2.3.5 now asserts both PATCH-without-activation and import-of-edited-export paths. R3-F3 ACCEPT: verified `modelSupportsImageInput` (providerModels.ts:249) returns true for null `input_modalities` and even null provider; rewrote 5.3 body and 5.3.3 to key eligibility on endpoint catalog rows (any configured generation endpoint row regardless of provider name or wire protocol) with null/absent modalities never image-eligible, cloud collector catalogs keeping the fallback, and an explicit note that a provider-name allowlist does not satisfy 5.3.3. R3-N1 ACCEPT: verified docs/guides/llm-features.md:51 instructs `vision_extract: true`; 6.1 body now retires that sentence with probe-evidence language and acceptance 6.1.4 keeps the key out of every copy-pasteable YAML example. No declines, no deferrals. Run history: single attempt, clean protocol (155 tool calls, 219 turns) — the round-3 spawn prompt's DAEMON_UNAVAILABLE retry discipline was not needed.

```json plan-review-round
{"evidence_id":"d998d520-6815-43d6-824d-fdc81cc151b7","plan_hash":"fd759bebc88c6f01557bc21664035047246464a0d3bbbe8159894437a27fa519","round_number":3,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"be2e407524c6b3abc0cdd0e0209932902aeccded26a1edfcd8e089c0dca150d8","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":13,"emitted_findings":4,"total":17},"evidence_id":"d998d520-6815-43d6-824d-fdc81cc151b7","lanes":[{"candidate_count":3,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":7,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":7,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":15,"manifest_digest":"bb15169fe7ad037549cadc940a8601f53ab9c83adb3b08a579ac8c2eb5e1e2d7","status":"valid"},"source_digest":"a54bc335704a24bb1addbccdd04cbaba5190581ee3fdf0b5b775c55db5ac4375","version":1},"findings":[{"category":"gobby-format","check_key":"target-inventory-completeness","description":"Section 2.3 deletes GenerationEndpointConfig.vision_extract and says every remaining reader migrates in this deliverable, but Targets omit `_generation_endpoint_text_bindings` in `src/gobby/ai/registry_builder.py`, which still writes `endpoint.vision_extract` into text_generate binding metadata. After the field is gone, registry construction AttributeErrors before vision bindings or grants run.","finding_id":"R3-F1-vision-extract-text-binding-reader","fix":"Add `src/gobby/ai/registry_builder.py::_generation_endpoint_text_bindings` to 2.3 Targets. Migrate that metadata write to probe evidence (or drop the unused key) and add a test that registry build succeeds for an endpoint with no vision_extract field.","location":"P2 / § 2.3","participating_section_ids":["2.3"],"prevention":"Grep endpoint.<deleted_field> across src/ before deleting a GenerationEndpointConfig field and add every hit to Targets.","principle":"Every remaining reader of a deleted extra=forbid config field must be a listed Target of the deleting deliverable.","root_cause":"2.3 inventories vision-binding, activation, grants, and Responses model readers but not the text-generate binding builder that copies endpoint.vision_extract into metadata.","section_id":"2.3","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"R2-F3-config-write-invalidation-path","causal_section_ids":["2.3"],"check_key":"probe-derived-state-freshness","description":"2.3.5 requires identity changes to clear probed_model/input_modalities/probed_json/probed_tools and names YAML import/replace as an adjacent write path. The body asserts those writes all go through patch_flat. They do not: POST /api/config/import calls replace_yaml, which never enters patch_flat. Export, edit api_base/protocol/model/api_key, and re-import can persist stale image capability. This is a fixer-induced defect from R2-F3.","finding_id":"R3-F2-yaml-import-invalidation-path","fix":"Extract identity clearing into a shared helper beside reject_unprobed_responses_endpoints and call it from both patch_flat and replace_yaml. Target ConfigDocumentsService.replace_yaml in 2.3. Keep 2.3.5 on PATCH with no follow-up activate, and add an import/replace test that an exported document with an identity-field edit drops evidence.","introduced_in_round":2,"location":"P2 / § 2.3","prevention":"When a sibling hook already exists on both patch_flat and replace_yaml, put the new invariant in that shared helper and test PATCH plus import.","principle":"Derived capability state must be cleared on every mutation path that can change identity, not only the settings PATCH hook.","root_cause":"The R2-F3 repair placed invalidation on ConfigValuesService.patch_flat and claimed YAML import/replace flows through that path. Import uses ConfigDocumentsService.replace_yaml → replace_namespace and already calls reject_unprobed_responses_endpoints independently of patch_flat.","section_id":"2.3","severity":"blocking"},{"category":"unhandled-edge","check_key":"ui-modality-eligibility","description":"5.3 chips correctly render nothing for null modalities, but image-attachment eligibility keeps the cloud missing-means-true fallback for any row not named vllm/lmstudio/ollama. Responses-wire and generic openai-compatible endpoint rows with null modalities (2.3.10: no image modality, no grant) would still enable image attach in ChatMainColumn via modelSupportsImageInput.","finding_id":"R3-F3-null-modality-ui-eligibility","fix":"Rewrite 5.3.3 so any endpoint catalog row (vllm, lmstudio, ollama, openai-compatible, and responses-wire endpoint:* / execution_provider=codex endpoint models) treats null or absent input_modalities as not image-eligible. Keep missing-means-true only for cloud collectors that never carry the field. Add a test for a responses endpoint with null modalities.","location":"P5 / § 5.3 with § 2.3 and § 3.2","participating_section_ids":["5.3","2.3","3.2"],"prevention":"Define UI eligibility from the catalog contract: null or absent-on-an-endpoint-row is not image-capable; missing-means-true only when a cloud collector omits the field.","principle":"Unknown modality metadata must never be treated as image-capable on any endpoint catalog row.","root_cause":"5.3.3 applies missing-means-false only to a vllm/lmstudio/ollama name list, while modelSupportsImageInput still treats null input_modalities as true. 3.2 and 2.3.10 already say null/no-evidence is never image-eligible, including Responses endpoints.","section_id":"5.3","severity":"blocking"},{"category":"missing-requirement","check_key":"config-field-retirement","description":"docs/guides/llm-features.md still says vision extraction is only for endpoints configured with vision_extract: true. After 2.3 that field is gone (extra=forbid), so the 6.1-owned guide would tell operators to set a field that 422s on save.","finding_id":"R3-N1-docs-vision-extract-field","fix":"In 6.1, replace that sentence with probe-evidence language (vision_extract remains a capability name, availability follows probed or advertised image modality) and keep it out of copy-pasteable endpoint YAML.","location":"P6 / § 6.1","participating_section_ids":["6.1","2.3"],"prevention":"When a later docs leaf owns a file that still documents a field another deliverable deletes, add an acceptance item that rewrites that sentence.","principle":"A docs deliverable that owns a guide must not leave copy-paste instructions for a deleted config field.","root_cause":"6.1 extends llm-features.md for vllm examples but never retires the existing `vision_extract: true` operator instruction that 2.3 deletes.","section_id":"6.1","severity":"nit"}],"round_number":3,"verdict":"needs_review"},"session_id":"e39817b3-864e-42b6-87a5-e67e18308b6f"}
```

**Round 4** `kind: verification`

- reviewer_run: 4ec4f5db-0fba-4571-81b0-91aca0e98826
- reviewer_session: 402c702d-0c30-441a-be5b-af12435685d5
- verdict: needs_review
- findings:
- R4-F1-activate-identity-clears-probe-evidence / blocking / The 2.3 shared identity-clearing helper, specified to run on every identity-changing patch_flat mutation including the activate route's own writes, would unset the probe evidence the activate patch itself is storing — an identity-changing activate could never persist unknown-to-image-capable (2.3.8) or Responses-path (2.3.10) evidence. Fixer-induced by the round-3 R3-F2 repair.
- R4-F2-endpoint-option-discriminator / blocking / The 5.3 eligibility rule keyed on endpoint catalog rows (provider=codex / execution_provider=codex) cannot separate responses-wire endpoint options from cloud Codex models: /api/providers/models merges endpoint:* options into the same provider=codex entry as cloud collector models, and available local groups also carry execution_provider=codex. Fixer-induced by the round-3 R3-F3 repair.
- resolution_notes: Both findings accepted with code-verified rationale and repaired. R4-F1: 2.3 identity set now includes wire_api; the shared helper clears evidence only on non-probe-verified mutations, using patch_flat's existing probe_verified=True flag (set only by the activate route, verified at configuration_generation_endpoints.py:92-96 where activate writes protocol/wire_api/api_base/model/api_key in the same patch as probe results); replace_yaml is never probe-verified; 2.3.5 rewritten so PATCH-without-activate and import-of-edited-export still drop evidence while an identity-changing activate persists its replacement probe result and a failed re-probe leaves evidence absent; an unconditional helper that clears probe-verified writes is explicitly named as not satisfying 2.3.5. R4-F2: 5.3 eligibility is now decided per model option — an option is endpoint-backed exactly when its value or owning provider name starts with endpoint: (verified: endpoint_provider() prefix in src/gobby/ai/endpoints.py:42-43, and providers.py:390-394 merges _responses_endpoint_models into the provider=codex entry with entry-level execution_provider=codex); entry-level codex keys are explicitly rejected; 5.3.3 now requires fixtures for a null-modality endpoint:* option inside provider=codex, a cloud Codex option omitting the field keeping the fallback, and a generic openai-compatible endpoint:* group with null modalities.

```json plan-review-round
{"evidence_id":"d660727d-1ec7-4bf4-8ba6-af755847de07","plan_hash":"9aa6bae258b65daa8a521e49ddf07a31dd77b5dfc4856efc314f55b1b4f135f3","round_number":4,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"a82188685301cfc278e593771f6b3dbfdbaad72b375dedc90f1a6957586768d3","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":14,"emitted_findings":2,"total":16},"evidence_id":"d660727d-1ec7-4bf4-8ba6-af755847de07","lanes":[{"candidate_count":5,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":6,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":5,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":15,"manifest_digest":"72f92614e640367b012e16dbf6336184513a00e7380e9d36a84a9719ecc68475","status":"valid"},"source_digest":"f810e089f067d01a4da029ea935aa3e6e458c1c21a80804d0da64d2dc12beb87","version":1},"findings":[{"category":"unhandled-edge","causal_finding_id":"R3-F2-yaml-import-invalidation-path","causal_section_ids":["2.3"],"check_key":"probe-derived-state-freshness","description":"2.3.5 requires a shared identity-clearing helper on ConfigValuesService.patch_flat and ConfigDocumentsService.replace_yaml, and the body says patch_flat also covers the activate route's writes. Activate always persists identity fields together with probe results. On any identity-changing activate — the only legal mutation path for responses-wire endpoints — the helper would unset the evidence the same patch is trying to store, so 2.3.8/2.3.10 cannot persist unknown-to-image-capable or responses-path evidence after an identity edit.","finding_id":"R4-F1-activate-identity-clears-probe-evidence","fix":"Keep the helper beside reject_unprobed_responses_endpoints and call it from the same seams (patch_flat before CAS; replace_yaml _prepare). Skip clearing when probe_verified is true so activate replacement evidence survives. Add wire_api to the identity set. Extend 2.3.5 so PATCH-without-activate and import-of-edited-export still drop evidence, and an identity-changing activate persists the new probe result (including a failed re-probe leaving evidence absent).","introduced_in_round":3,"location":"P2 / § 2.3","prevention":"When a mutation hook runs on both settings PATCH and activate, specify that probe_verified activate writes persist replacement evidence; identity clearing applies only to non-activate PATCH and replace_yaml.","principle":"Activation-owned probe evidence must survive the activate write that creates it.","root_cause":"The R3-F2 helper is specified to run on patch_flat including the activate route's own writes and to unset probed_model/input_modalities/probed_json/probed_tools whenever identity fields change, regardless of client payload. Activate already writes protocol, api_base, model, and api_key in the same patch_flat as the new probe fields.","section_id":"2.3","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"R3-F3-null-modality-ui-eligibility","causal_section_ids":["5.3"],"check_key":"ui-modality-eligibility","description":"5.3.3 requires every endpoint catalog row with null or absent input_modalities to be not image-eligible, including responses-wire and generic openai-compatible rows, while cloud collector catalogs keep missing-means-true. An entry-level rule on provider=codex or execution_provider=codex cannot satisfy both clauses: it either disables image attach for cloud Codex models that omit the field, or it leaves generic openai-compatible endpoint rows on the cloud fallback. The unique discriminator is the model option (value or owning provider starting with endpoint:), which 5.3 mentions only as a parenthetical mixed with the misleading execution_provider=codex key.","finding_id":"R4-F2-endpoint-option-discriminator","fix":"Rewrite 5.3 and 5.3.3 so eligibility is per model option: if the option value or its owning provider starts with endpoint:, null or absent input_modalities means not image-eligible. Do not classify a whole provider=codex / execution_provider=codex entry. Keep missing-means-true only for non-endpoint options. Require tests for a responses-wire endpoint:* option with null modalities under provider=codex, a cloud Codex option without the field, and a generic openai-compatible endpoint:* group with null modalities.","introduced_in_round":3,"location":"P5 / § 5.3","prevention":"Inspect /api/providers/models grouping before defining a UI eligibility key. Require 5.3.3 fixtures for a null-modality endpoint:* option under provider=codex, a cloud Codex option that never carries the field, and a generic openai-compatible endpoint:* group with null modalities.","principle":"Image-eligibility must be decided on the model option that represents a generation endpoint, not on a parent catalog entry shared with cloud collectors.","root_cause":"The R3-F3 repair keys 5.3 on endpoint catalog rows and names execution_provider=codex as a responses-wire identifier. /api/providers/models appends responses-wire models into the existing provider=codex entry alongside cloud collector models; available local groups also set execution_provider=codex; generic openai-compatible groups have provider=endpoint:* and no execution_provider.","section_id":"5.3","severity":"blocking"}],"round_number":4,"verdict":"needs_review"},"session_id":"e39817b3-864e-42b6-87a5-e67e18308b6f"}
```

**Round 5** `kind: verification`

- reviewer_run: 01fc1b99-5802-4198-bfb3-bba28fb5b3bc
- reviewer_session: 6b9683e4-cb5e-4cab-a814-cf355c7d3bf2
- verdict: needs_review
- findings:
- R5-F1-identity-clear-same-save / blocking / §2.3 identity-clearing helper had no previous-versus-next change semantics: the settings editor resubmits the whole endpoint object with a MASKED_SECRET api_key on every save, so a touched-key or mask-vs-stored implementation would wipe probe evidence on every no-op save.
- R5-F2-default-option-modalities / blocking / the preferred default `endpoint:<name>` picker option carries no input_modalities, so round-4's per-option endpoint eligibility rule left the default selection image-ineligible even after a successful vision probe of the model it aliases.
- resolution_notes: Both findings accepted (coordinator vote, unattended mode; both fixer-induced — R5-F1 from the round-3 R3-F2 helper spec, R5-F2 from the round-4 R4-F2 eligibility rule). §2.3 now defines identity change as a previous-versus-next comparison against the anchored desired config after secret handling (masked api_key = unchanged; api_key unset = change), names the touched-key and mask-compare implementations as non-satisfying, adds same-identity-save and unchanged-re-import preserve cases, and extends 2.3.5 with those preserve assertions. §3.1 now copies probed modalities onto the default `endpoint:<name>` option when its configured model (including `auto`) resolves to `probed_model` via the 1.2 resolver, adds `_merge_default_model` as a Target, and extends 3.1.3; §3.2 applies the same rule to advertised modalities and extends 3.2.3; 5.3.1 and 5.3.3 gain default-option chip and image-eligibility fixtures.

```json plan-review-round
{"evidence_id":"01f121af-e989-41d2-9f1f-3a326b581fbc","plan_hash":"acd7dba634039b2ba985c800654ee230b005e5893263dd61de0d693b0cb6e80c","round_number":5,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"88b6df18c741ed98511f954cb604206743d4e5e96ced35c5ff7bfad2b7317d93","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":12,"emitted_findings":2,"total":14},"evidence_id":"01f121af-e989-41d2-9f1f-3a326b581fbc","lanes":[{"candidate_count":2,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":7,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":5,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":15,"manifest_digest":"fc4c307547d8655cfbfadaa6203b0e08ec5fe4d7f7d23360a7ca843a26877e4e","status":"valid"},"source_digest":"670fb94032a6a8593c9c1c2a759907438571e3cbc6a5afebb753a92a733a6f4c","version":1},"findings":[{"category":"unhandled-edge","check_key":"probe-derived-state-freshness","description":"Round-4 identity clearing still does not define how to detect a change against the mutation the settings editor actually sends. A helper keyed on identity fields present in the PATCH, or one that treats MASKED_SECRET as a new api_key, would drop probed_model/input_modalities/probed_json/probed_tools on every settings save — including a timeout-only edit or a resave of the same endpoint. 2.3.5 only asserts identity-changing PATCH and import drop evidence; it never asserts that a same-identity save or a re-import without identity edits keeps the replacement probe result. Adjacent variants: unset of api_key is a real identity change and must still clear; replace_yaml of an unchanged export must preserve.","finding_id":"R5-F1-identity-clear-same-save","fix":"Specify that the helper compares the previous desired identity to the post-patch identity after MASKED_SECRET skip and secret restore. Clear evidence only when protocol, wire_api, api_base, model, or the resolved api_key actually changes (including unset). A settings PATCH that resubmits the same identity, including a masked api_key, and a YAML re-import with no identity edit, must preserve evidence. Extend 2.3.5 with those preserve cases alongside the existing clear/activate cases.","location":"P2 / § 2.3 with § 5.1","participating_section_ids":["2.3","5.1"],"prevention":"For every persisted probe field, test a same-identity settings save (masked api_key, unchanged protocol/wire_api/api_base/model) that must preserve evidence, plus a real identity mutation and an api_key unset that must clear it.","principle":"Derived capability state must be invalidated only when its evidence identity actually changes.","root_cause":"2.3.5 tells the shared helper to clear probe evidence whenever identity fields change, but never specifies a previous-versus-next comparison after secret-mask handling. The settings editor PATCHes the entire `ai.generation.endpoints` object, so every save resubmits protocol, wire_api, api_base, model, and api_key. api_key arrives as MASKED_SECRET (`********`); patch_flat skips that sentinel for the write, but a helper that follows reject_unprobed_responses_endpoints' touched-key pattern — or that string-compares the mask to the stored secret — treats a no-op Providers/Models save as an identity change.","section_id":"2.3","severity":"blocking"},{"category":"unhandled-edge","check_key":"ui-modality-eligibility","description":"Web chat and the provider picker prefer the default local option (`getPreferredModelForProvider` picks `is_default`). After a successful vLLM vision probe, that default row still has null modalities, so 5.3 renders no chips and `modelSupportsImageInput` disables attach — even though the sibling discovered option `endpoint:<name>/<probed_id>` is image-capable and 2.1 would route images to the resolved model. Users who keep the default selection cannot attach images. The same gap applies to LM Studio `vlm` and Ollama vision advertisement via 3.2.","finding_id":"R5-F2-default-option-modalities","fix":"In 3.1 (and 3.2 for advertised labels), copy probe or advertised modalities onto the default `endpoint:<name>` option when that option represents the probed/advertised model (match on resolved served id, not the literal `auto` sentinel). Keep other served models null. Extend 3.1.3/3.2.3 and 5.3.1/5.3.3 so a fixture selects the default option after a successful image-capable probe or vlm advertisement and asserts chips plus image-eligibility, while a second unprobed model on the same endpoint stays unknown.","location":"P3 / § 3.1 with P5 / § 5.3 and P3 / § 3.2","participating_section_ids":["3.1","5.3","3.2"],"prevention":"When merging modalities into /api/providers/models, inventory every option shape: default endpoint:<name> alias, discovered endpoint:<name>/<id>, and responses-wire selectors. Require a fixture that the preferred default option shows chips and image-attach after a successful probe of that model.","principle":"A catalog option that aliases a probed or advertised model must carry that model's modalities; unknown applies only to options that were never probed or advertised.","root_cause":"3.1 merges probe evidence only into `_local_model_entry` for the probed model id. `_merge_default_model` separately prepends the preferred picker option (`value` `endpoint:<name>`, `is_default: true`, `canonical_id` equal to the configured model, which may still be `auto`) with no input_modalities. 3.2's advertised LM Studio/Ollama modalities use the same `_local_model_entry` path and miss that default alias. After the round-4 5.3 rule, an option whose value starts with `endpoint:` and has null/absent modalities is not image-eligible.","section_id":"3.1","severity":"blocking"}],"round_number":5,"verdict":"needs_review"},"session_id":"e39817b3-864e-42b6-87a5-e67e18308b6f"}
```

**Round 6** `kind: verification`

- reviewer_run: 05b5424c-8108-455b-903f-c547f428df10
- reviewer_session: 1dcd901c-4753-468b-ab15-a19dd8ad1a88
- verdict: needs_review
- findings:
- R6-F1-advertised-default-option-merge / blocking / round-5 default-option modality copy was specified vLLM/probe-only (1.2 resolver vs probed_model), so LM Studio vlm and Ollama vision advertisements never copy onto the preferred default `endpoint:<name>` option, leaving 3.2.3/5.3.1 default-option fixtures with no producing implementation (fixer-induced by R5-F2, round 5)
- R6-F2-auto-default-option-untested / blocking / the `model: auto` alias path was described but never forced by acceptance, and once 1.1 makes `vllm` a distinct protocol the current `canonical_id == endpoint.model` verification arm can never match the `auto` sentinel, dropping the default option entirely (fixer-induced by R5-F2, round 5)
- resolution_notes: Both findings accepted. 3.1 now specifies `_merge_default_model`'s copy as source-agnostic — the default option copies `input_modalities` from the discovered entry it aliases, whether probe-evidence or 3.2 advertised classification — with per-protocol alias matching (vllm resolves via the 1.2 resolver, including `model: auto`; every other protocol matches `canonical_id` to the configured model; the resolver is never consulted for lmstudio/ollama) and an explicit emission rule: resolved `auto` prepends the default option, unresolvable `auto` (multi-model resolver error) emits none. 3.1.3 gained literal `auto`-sentinel fixtures for both the single-model resolved case (modalities copied) and the two-model error case (no default option). 3.2 now points at 3.1's source-agnostic copy with the lmstudio/ollama canonical_id alias rule and states the copy needs no edit in 3.2. 5.3.1's vllm default-option chip fixture is now pinned to `model: auto` on a single-model endpoint so the UI case exercises 1.2 resolution rather than a literal id match.

```json plan-review-round
{"evidence_id":"d59dfdd6-f13c-4de2-8f62-440f8fcca23f","plan_hash":"bd01be55ab76779ffca053a28ebdcac9d1d800ae651ff89815bad072f1458b4a","round_number":6,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"f2b02fb1f531c1cd9ca0b0f0b690cadc516cbcbd4b443976f652f0e245c44adf","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":8,"emitted_findings":2,"total":10},"evidence_id":"d59dfdd6-f13c-4de2-8f62-440f8fcca23f","lanes":[{"candidate_count":1,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":5,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":4,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":15,"manifest_digest":"68cf3f94b5eaf8ea9e60e5cb7051e2a452189f66b0bc1ed157db12075cfd5179","status":"valid"},"source_digest":"095611732e8e37deb9fefc8184d525333c997f37605584b2be00092da7d13339","version":1},"findings":[{"category":"unhandled-edge","causal_finding_id":"R5-F2-default-option-modalities","causal_section_ids":["3.1","3.2","5.3"],"check_key":"ui-modality-eligibility","description":"Round-5 default-option modality copy is specified as a vLLM 1.2-resolver compare against probed_model. That does not copy LM Studio vlm or Ollama vision advertisements onto the default endpoint:<name> option. 3.2 requires those default-option modalities in 3.2.3 and 5.3.1 but does not target _merge_default_model, which is the only function that builds that option.","finding_id":"R6-F1-advertised-default-option-merge","fix":"In 3.1, specify _merge_default_model as source-agnostic: after discovered entries exist, copy the matching entry's input_modalities onto the default option when the default aliases that entry (vLLM model:auto via the 1.2 resolver; other protocols match canonical_id to the configured model). Keep 1.2 resolution gated on protocol vllm so LM Studio/Ollama discovery does not call the vLLM resolver. If 3.1 stays probe-only, add _merge_default_model to 3.2 Targets and state the advertised-copy rule there.","introduced_in_round":5,"location":"P3 / § 3.2 with § 3.1 and P5 / § 5.3","prevention":"When a later leaf extends a shared merge helper, either specify that helper as source-agnostic (copy the matching discovered entry's modalities) or add the helper to the extending leaf's Targets with an explicit non-vLLM match rule.","principle":"A catalog option that aliases a probed or advertised model must carry that model's modalities; unknown applies only to options that were never probed or advertised.","root_cause":"The R5-F2 repair tells 3.1's shared _merge_default_model to copy probed modalities when the 1.2 vLLM resolver matches probed_model, and tells 3.2 that advertised LM Studio/Ollama modalities follow the same rule. _merge_default_model today prepends a bare {value,label,canonical_id,is_default} dict and never calls _local_model_entry. 3.2 Targets are only _is_lmstudio_llm, _local_model_entry, and create_providers_router, so advertised modalities land on discovered rows and never on the preferred default option. 3.1's specified algorithm is probe/vLLM-only, so 3.2.3 and 5.3.1's vlm-advertisement default-option fixtures have no producing implementation.","section_id":"3.2","severity":"blocking"},{"category":"weak-testability","causal_finding_id":"R5-F2-default-option-modalities","causal_section_ids":["3.1","5.3"],"check_key":"model-auto-resolution","description":"The R5-F2 auto path is described in the 3.1 body but not forced by acceptance. 3.1.3 and 5.3.1 can be satisfied by an explicit configured model matching a discovered canonical_id. That never exercises model:auto, and after vllm is a distinct protocol the current prepend condition drops the default option for auto entirely.","finding_id":"R6-F2-auto-default-option-untested","fix":"Extend 3.1.3 so a single-model vllm endpoint with model:auto prepends endpoint:<name> whose modalities match the probed served id, and a two-model auto configuration (resolver error) does not invent image capability on the default option. Mirror the auto default-option case in 5.3.1. State that _merge_default_model must emit the default option when auto resolves, not only when canonical_id literally equals endpoint.model.","introduced_in_round":5,"location":"P3 / § 3.1 with P5 / § 5.3","prevention":"When repairing a sentinel-alias bug, require a fixture whose configured model is the literal auto sentinel and assert both that the default option is emitted and that it carries the resolved model's modalities.","principle":"A sentinel configuration value must resolve before every path that emits or aliases it, and the acceptance item that claims that resolution must fixture the sentinel.","root_cause":"R5-F2's distinctive failure is canonical_id equal to the configured model, which may still be auto, so a literal compare never copies probe evidence. 3.1.3 only requires a two-model fixture where the configured model resolves to probed_model; that passes with today's default_is_verified rule (canonical_id == endpoint.model) and an explicit configured model. After 1.1 adds protocol vllm, default_is_verified is no longer always-true (that is openai-compatible only), so model:auto does not even prepend endpoint:<name>. Constraints name auto as the default selector.","section_id":"3.1","severity":"blocking"}],"round_number":6,"verdict":"needs_review"},"session_id":"e39817b3-864e-42b6-87a5-e67e18308b6f"}
```

**Round 7** `kind: verification`

- reviewer_run: e3931fb9-940b-41a9-b9f4-45efe2407743
- reviewer_session: 3b7794c8-dc5f-4cf5-98e3-364ef8f49514
- verdict: approved
- findings:
- none — 8 candidates raised across the three coverage lanes, all dismissed on code evidence
- resolution_notes: No changes required. The round-6 repairs (source-agnostic default-option modality copy with per-protocol aliasing in 3.1/3.2; the `model: auto` emission rule and literal-auto fixtures in 3.1.3/5.3.1) were re-verified adversarially and held. Shadow manifest valid with 15 entries; server-derived M1 manifest applied via apply_plan_review_manifest.

```json plan-review-round
{"evidence_id":"feeb00a1-8f3f-4850-bf7f-84b752aeaa70","plan_hash":"7266c2e4fc6b309666cd16d76d214d07aabe70955db88c39e2dffffacd6a9cb0","round_number":7,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"71960d83ace8cb066dea16a44526647a76fe42f08b3b3eee6a6dd1dfbb3ce6b3","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":8,"emitted_findings":0,"total":8},"evidence_id":"feeb00a1-8f3f-4850-bf7f-84b752aeaa70","lanes":[{"candidate_count":2,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":3,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":3,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":15,"manifest_digest":"d0093bbb4f1a717a84e70f9475bbfe926d45fde834f9b1ff3d72c2b812e51f6a","status":"valid"},"source_digest":"981d4eb1a958fec205d68565a81fdad8526cfc8013b289c5891c33e226a1e763","version":1},"evidence_id":"feeb00a1-8f3f-4850-bf7f-84b752aeaa70","findings":[],"manifest_entries":[{"category":"code","depends_on":[],"implementation_domain":"backend","labels":["covers:vllm-runtime-support:1.1:1.1.1","covers:vllm-runtime-support:1.1:1.1.2","covers:vllm-runtime-support:1.1:1.1.3"],"source_section":"1.1","task_type":"feature","tdd":true,"title":"Add vllm protocol value and adapter dispatch","validation_criteria":"1.1.1: `\"vllm\"` is a valid `GenerationEndpointProtocol` value and configures under `ai.generation.endpoints.<name>`. file: `src/gobby/config/ai.py`.\n1.1.2: `create_local_provider_adapter` returns an `OpenAICompatibleLocalProviderAdapter` with a non-None `client` for vllm endpoints; no vllm-specific adapter class exists. test: `tests/llm/test_local_provider_adapters.py::test_create_adapter_vllm`.\n1.1.3: `wire_api: responses` on a vllm endpoint is rejected by config validation. test: `tests/llm/test_local_provider_adapters.py::test_vllm_rejects_responses_wire`."},{"category":"code","depends_on":["1.1"],"implementation_domain":"backend","labels":["covers:vllm-runtime-support:1.2:1.2.1","covers:vllm-runtime-support:1.2:1.2.2","covers:vllm-runtime-support:1.2:1.2.3","covers:vllm-runtime-support:1.2:1.2.4"],"source_section":"1.2","task_type":"feature","tdd":true,"title":"vLLM model lifecycle in ensure_local_model","validation_criteria":"1.2.1: `model: auto` on a single-model vllm endpoint resolves to the served model; multi-model raises with the served list. symbol: `gobby.agents.local_model.ensure_local_model`.\n1.2.2: vllm endpoints never receive load/unload/keep-alive requests. behavior: \"non-owning model lifecycle\" in `src/gobby/agents/local_model.py`.\n1.2.3: The literal model value `auto` never reaches a vllm wire request on any path — generation, activation probes, tool_chat, or Codex override args; all resolve through the shared resolver. test: `tests/agents/test_local_model.py::test_vllm_auto_resolves_before_wire`.\n1.2.4: `api_base` values with and without a trailing `/v1` both produce a single normalized `{origin}/v1/models` URL. test: `tests/agents/test_local_model.py::test_vllm_models_url_normalization`."},{"category":"code","depends_on":["1.1","1.2"],"implementation_domain":"backend","labels":["covers:vllm-runtime-support:2.1:2.1.1","covers:vllm-runtime-support:2.1:2.1.2","covers:vllm-runtime-support:2.1:2.1.3","covers:vllm-runtime-support:2.1:2.1.4","covers:vllm-runtime-support:2.1:2.1.5","covers:vllm-runtime-support:2.1:2.1.6","covers:vllm-runtime-support:2.1:2.1.7","covers:vllm-runtime-support:2.1:2.1.8"],"source_section":"2.1","task_type":"feature","tdd":true,"title":"Optional image inputs on the text-generation core","validation_criteria":"2.1.1: `TextGenerationRequest` carries optional images; text-only requests are byte-identical to today's payloads. symbol: `gobby.ai._text_generation_contracts.TextGenerationRequest`.\n2.1.2: An image-bearing request against an OpenAI-compatible endpoint renders `image_url` content blocks. test: `tests/llm/test_local_provider_adapters.py::test_generate_with_images`.\n2.1.3: Image-bearing requests never route to candidates without image support. behavior: \"modality-aware candidate filtering\" in `src/gobby/ai/_text_generation_service.py`.\n2.1.4: Mixed-catalog routing: text-only models are skipped and an image-capable model is selected for image-bearing requests. test: `tests/ai/test_text_generation.py::test_image_routing_mixed_catalog`.\n2.1.5: An explicitly selected text-only endpoint/model with images returns a deterministic modality diagnostic. test: `tests/ai/test_text_generation.py::test_image_request_text_only_selection_diagnostic`.\n2.1.6: `POST /api/llm/generate` accepts `images`, forwards them to `TextGenerationRequest.images`, and an image-bearing route request reaches an image-capable endpoint end-to-end. test: `tests/servers/routes/test_llm_routes.py::test_generate_route_forwards_images`.\n2.1.7: Malformed data URLs, disallowed MIME, invalid base64, relative or unreadable paths, count over 8, and aggregate decoded size over 24 MiB are each rejected deterministically with a diagnostic naming the offending input. test: `tests/servers/routes/test_llm_routes.py::test_generate_route_image_rejections`.\n2.1.8: An image-bearing request never selects a feature CLI provider (agy/droid/grok/qwen) or a generic `codex` binding without endpoint metadata, and the predicate reads binding metadata rather than adapter class or spawn style. test: `tests/ai/test_text_generation.py::test_image_routing_skips_generic_codex`."},{"assigned_agent":"backend-developer","category":"refactor","depends_on":["2.1","2.5"],"labels":["covers:vllm-runtime-support:2.2:2.2.1","covers:vllm-runtime-support:2.2:2.2.2","covers:vllm-runtime-support:2.2:2.2.3","covers:vllm-runtime-support:2.2:2.2.4"],"source_section":"2.2","task_type":"feature","tdd":false,"title":"Collapse vision_extract onto the generation core","validation_criteria":"2.2.1: No `describe_image` symbol remains in `src/gobby/llm/`; vision extraction executes through the generation core. file: `src/gobby/llm/local_provider_adapters.py`.\n2.2.2: `POST /api/llm/vision/extract` behavior and grant gating are unchanged for existing callers. test: `tests/ai/test_vision_extraction.py`.\n2.2.3: Image-bearing generate requests against LM Studio and Ollama serialize each provider's native image shape, matching the pre-port `describe_image` wire payloads. test: `tests/llm/test_local_provider_adapters.py::test_native_image_serialization_ported`.\n2.2.4: After this deliverable the image-eligible predicate in `TextGenerationService` admits lmstudio and ollama endpoint bindings; image-bearing requests route to them. test: `tests/ai/test_text_generation.py::test_image_allowlist_admits_native_transports`."},{"category":"code","depends_on":["1.1","1.2","2.1","2.2","5.1"],"implementation_domain":"backend","labels":["covers:vllm-runtime-support:2.3:2.3.1","covers:vllm-runtime-support:2.3:2.3.2","covers:vllm-runtime-support:2.3:2.3.3","covers:vllm-runtime-support:2.3:2.3.4","covers:vllm-runtime-support:2.3:2.3.5","covers:vllm-runtime-support:2.3:2.3.6","covers:vllm-runtime-support:2.3:2.3.7","covers:vllm-runtime-support:2.3:2.3.8","covers:vllm-runtime-support:2.3:2.3.9","covers:vllm-runtime-support:2.3:2.3.10","covers:vllm-runtime-support:2.3:2.3.11","covers:vllm-runtime-support:2.3:2.3.12"],"source_section":"2.3","task_type":"feature","tdd":true,"title":"Chat-completions activation probe and modality metadata","validation_criteria":"2.3.1: `PUT /api/config/generation-endpoints/{name}/activate` probes lmstudio, ollama, and vllm chat-completions endpoints and persists `input_modalities`. file: `src/gobby/servers/routes/configuration_generation_endpoints.py`.\n2.3.2: `vision_extract` no longer exists as an endpoint config field; vision bindings require probed or advertised image modality. symbol: `gobby.ai.registry_builder._generation_endpoint_vision_bindings`.\n2.3.3: A vision probe failure yields a text-only activation, never a hard endpoint failure. test: `tests/ai/test_endpoint_activation.py::test_vision_probe_degrades_to_text`.\n2.3.4: On an endpoint serving two models with different modality support, probe evidence attaches only to the probed model; the other stays unknown in discovery and routing. test: `tests/ai/test_endpoint_activation.py::test_model_scoped_modalities_mixed_endpoint`.\n2.3.5: Changing protocol, `wire_api`, `api_base`, `model`, or `api_key` (including an api_key unset) clears persisted modality evidence via the shared identity-clearing helper on both non-probe-verified mutation paths: asserted through `PATCH /api/config/values` with no follow-up activation (`ConfigValuesService.patch_flat`), and through YAML import of an exported document whose identity fields were edited (`ConfigDocumentsService.replace_yaml`) — while a same-identity settings PATCH that resubmits the full endpoint object with a `MASKED_SECRET` api_key preserves the evidence, a re-import of an unchanged export preserves it, an identity-changing activate (`probe_verified=True`) persists its replacement probe evidence instead of clearing it, and a failed re-probe leaves no stale image capability anywhere. test: `tests/ai/test_endpoint_activation.py::test_identity_change_invalidates_modalities`.\n2.3.6: A keyless local endpoint activates without an Authorization header; a configured key is sent. test: `tests/ai/test_endpoint_activation.py::test_optional_credentials_activation`.\n2.3.7: The probe outcome table holds end-to-end: text failure is fatal; JSON/tool/vision failures degrade and persist `probed_json`/`probed_tools` false; `tool_chat: false` skips the tool probe (`probed_tools: None`); re-activation restores recovered capabilities in metadata and grants. test: `tests/ai/test_endpoint_activation.py::test_probe_outcome_table`.\n2.3.8: First activation of a fresh endpoint reaches its vision probe with no pre-existing modality metadata; unknown-to-image-capable and unknown-to-text-only transitions both persist correctly. test: `tests/ai/test_endpoint_activation.py::test_vision_probe_bootstrap`.\n2.3.9: Runtime grant derivation follows probe evidence: image-capable evidence enables vision_extract; degraded or cleared evidence removes it. test: `tests/runtime_grants/test_active_config_binding.py::test_vision_grant_follows_probe_evidence`.\n2.3.10: The Responses activation path persists the same probed evidence shape, and a Responses endpoint without evidence has no vision binding, no image modality, and no grant. test: `tests/ai/test_endpoint_activation.py::test_responses_path_evidence_migration`.\n2.3.11: A stored endpoint document carrying `vision_extract: true` loads successfully after the field's deletion and presents as probe-unknown — no vision binding, no image modality, no grant. test: `tests/ai/test_endpoint_activation.py::test_vision_extract_field_stripped_on_load`.\n2.3.12: Registry construction succeeds for a generation endpoint after the field's deletion, and text_generate binding metadata carries no `vision_extract` key. test: `tests/ai/test_capability_registry.py::test_text_bindings_drop_vision_extract_metadata`."},{"category":"code","depends_on":["1.1","2.3"],"implementation_domain":"backend","labels":["covers:vllm-runtime-support:2.4:2.4.1","covers:vllm-runtime-support:2.4:2.4.2"],"source_section":"2.4","task_type":"feature","tdd":true,"title":"Honest tool_chat availability for clientless adapters","validation_criteria":"2.4.1: `tool_chat` on an lmstudio/ollama endpoint reports unavailable with a reason instead of failing at dispatch; vllm endpoints dispatch successfully. test: `tests/ai/test_capability_registry.py::test_tool_chat_clientless_unavailable`.\n2.4.2: A failed tool probe (`probed_tools: false`) reports unavailable with the probe reason while config `tool_chat` stays untouched; absent evidence preserves the client-gated behavior. test: `tests/ai/test_capability_registry.py::test_tool_binding_probe_evidence_gate`."},{"category":"code","depends_on":["2.1"],"implementation_domain":"backend","labels":["covers:vllm-runtime-support:2.5:2.5.1","covers:vllm-runtime-support:2.5:2.5.2","covers:vllm-runtime-support:2.5:2.5.3","covers:vllm-runtime-support:2.5:2.5.4"],"source_section":"2.5","task_type":"feature","tdd":true,"title":"Cloud transport image mapping: Claude SDK and Codex endpoint","validation_criteria":"2.5.1: An image-bearing request through the Claude SDK transport renders SDK image content blocks in the outgoing query. test: `tests/llm/test_claude.py::test_generate_text_with_images_renders_blocks`.\n2.5.2: An image-bearing request through a responses-wire Codex endpoint emits `--image` file arguments, materializes data-URL inputs to temp files with cleanup, and never places image bytes in argv. test: `tests/ai/test_text_generation.py::test_codex_endpoint_image_args`.\n2.5.3: Temp files materialized for data-URL images are removed on failure paths — nonzero exit, timeout, and cancellation — not only on success. test: `tests/ai/test_text_generation.py::test_codex_endpoint_image_tempfile_cleanup`.\n2.5.4: After this deliverable the image-eligible predicate admits `provider=claude` and responses-wire Codex endpoint bindings while an image-bearing generic `codex` binding without endpoint metadata stays skipped. test: `tests/ai/test_text_generation.py::test_image_allowlist_cloud_transports`."},{"category":"code","depends_on":["1.1","1.2","2.3"],"implementation_domain":"backend","labels":["covers:vllm-runtime-support:3.1:3.1.1","covers:vllm-runtime-support:3.1:3.1.2","covers:vllm-runtime-support:3.1:3.1.3"],"source_section":"3.1","task_type":"feature","tdd":true,"title":"Native vLLM model discovery","validation_criteria":"3.1.1: A vllm endpoint's models appear as `source: \"live\"` entries with provider-reported context length. symbol: `gobby.servers.local_provider_models.discover_local_endpoint_model_group`.\n3.1.2: Discovery failure surfaces the endpoint group with a config-sourced fallback and an error, matching lmstudio/ollama behavior. test: `tests/servers/test_local_provider_models.py::test_vllm_discovery_error_fallback`.\n3.1.3: On a two-model vllm endpoint, probe-persisted modalities attach only to the probed model's entry and to the default `endpoint:<name>` option when its configured model resolves to `probed_model`; the other model's modalities are null, and a default resolving to the unprobed model stays null. A single-model vllm endpoint configured `model: auto` prepends the `endpoint:<name>` default option with modalities copied from the probed served id; a two-model endpoint configured `model: auto` (resolver error) emits no default option. test: `tests/servers/test_local_provider_models.py::test_vllm_modalities_probed_model_only`."},{"category":"code","depends_on":["2.1","3.1"],"implementation_domain":"backend","labels":["covers:vllm-runtime-support:3.2:3.2.1","covers:vllm-runtime-support:3.2:3.2.2","covers:vllm-runtime-support:3.2:3.2.3"],"source_section":"3.2","task_type":"feature","tdd":true,"title":"Classify VLMs instead of excluding them","validation_criteria":"3.2.1: LM Studio `type: \"vlm\"` models appear in discovery tagged `input_modalities: [\"text\",\"image\"]`. test: `tests/servers/test_local_provider_models.py::test_lmstudio_vlm_classified`.\n3.2.2: Ollama models with a `vision` capability carry image modality; `/api/providers/models` local entries expose `input_modalities`. file: `src/gobby/servers/routes/providers.py`.\n3.2.3: `/api/providers/models` `input_modalities` agree with the 2.1 image-routing predicate's decisions for vllm, lmstudio, and ollama catalogs, including each group's default `endpoint:<name>` option after an image-capable advertisement or probe of the model it resolves to. test: `tests/servers/test_local_provider_models.py::test_modalities_match_routing_predicate`."},{"category":"code","depends_on":["1.1","1.2","3.2"],"implementation_domain":"backend","labels":["covers:vllm-runtime-support:4.1:4.1.1","covers:vllm-runtime-support:4.1:4.1.2","covers:vllm-runtime-support:4.1:4.1.3","covers:vllm-runtime-support:4.1:4.1.4"],"source_section":"4.1","task_type":"feature","tdd":true,"title":"Codex chat-wire transport for vllm endpoints","validation_criteria":"4.1.1: A vllm endpoint with an eligible chat model is web-chat routable and executes through Codex with config-override transport. symbol: `gobby.ai.codex_endpoint.codex_endpoint_config_overrides`.\n4.1.2: lmstudio/ollama web chat still uses `--oss --local-provider`; generic openai-compatible remains catalog-only. test: `tests/servers/test_local_llm.py::test_routable_transport_strategies`.\n4.1.3: `health()` reports endpoint-backend status instead of falling through to `unknown`. file: `src/gobby/servers/websocket/chat/runtime_manager.py`.\n4.1.4: Authenticated vllm endpoints keep `wire_api=\"chat\"` with credentials referenced only via `env_key`: the key lands in the child-process environment, never in argv, serialized `-c` values, or diagnostics, and the override set excludes the variable via `shell_environment_policy.exclude`. test: `tests/servers/test_local_llm.py::test_vllm_env_key_credential_transport`."},{"category":"code","depends_on":["1.2","4.1"],"implementation_domain":"backend","labels":["covers:vllm-runtime-support:4.2:4.2.1","covers:vllm-runtime-support:4.2:4.2.2"],"source_section":"4.2","task_type":"feature","tdd":true,"title":"Agent-spawn parity for vllm endpoints","validation_criteria":"4.2.1: Spawning a Codex agent against `endpoint:<vllm>/<model>` emits config-override args and no `--oss` flag. file: `src/gobby/agents/spawners/command_builder.py`.\n4.2.2: Spawned-agent commands for authenticated vllm endpoints carry the key only in the child-process environment via `env_key`; argv and serialized `-c` values stay secret-free, and `shell_environment_policy.exclude` covers the variable. test: `tests/agents/test_command_builder.py::test_vllm_spawn_env_key_transport`."},{"category":"code","depends_on":["1.1"],"implementation_domain":"frontend","labels":["covers:vllm-runtime-support:5.1:5.1.1","covers:vllm-runtime-support:5.1:5.1.2","covers:vllm-runtime-support:5.1:5.1.3","covers:vllm-runtime-support:5.1:5.1.4"],"source_section":"5.1","task_type":"feature","tdd":true,"title":"Settings: schema-derived protocols and wire_api field","validation_criteria":"5.1.1: The protocol dropdown lists vllm sourced from the daemon schema, with no hardcoded protocol list remaining in the component. file: `web/src/components/settings/sections/ProvidersModelsSection.tsx`.\n5.1.2: The editor exposes wire_api and enforces chat-completions for vllm. test: `web/src/components/settings/sections/__tests__/ProvidersModelsSection.test.tsx`.\n5.1.3: Switching a protocol to vllm writes `wire_api: \"chat-completions\"` into the submitted payload and switching back restores schema-provided choices, asserted on the saved request payload rather than only the rendered select. test: `web/src/components/settings/sections/__tests__/ProvidersModelsSection.test.tsx`.\n5.1.4: The editor no longer renders or submits `vision_extract`; saved payloads for every protocol omit the field. test: `web/src/components/settings/sections/__tests__/ProvidersModelsSection.test.tsx`."},{"category":"code","depends_on":["3.1"],"implementation_domain":"frontend","labels":["covers:vllm-runtime-support:5.2:5.2.1"],"source_section":"5.2","task_type":"feature","tdd":true,"title":"Icons and provider badges","validation_criteria":"5.2.1: vllm endpoint groups render a distinct icon in the provider picker and session badges classify vllm sessions as local. file: `web/src/components/shared/SourceIcon.tsx`."},{"category":"code","depends_on":["2.3","3.2","5.1","5.2"],"implementation_domain":"frontend","labels":["covers:vllm-runtime-support:5.3:5.3.1","covers:vllm-runtime-support:5.3:5.3.2","covers:vllm-runtime-support:5.3:5.3.3"],"source_section":"5.3","task_type":"feature","tdd":true,"title":"Text/Image capability chips","validation_criteria":"5.3.1: Endpoint and local-model rows render Text/Image chips from `input_modalities`, covering vllm probe metadata and LM Studio/Ollama advertised metadata — including the preferred default `endpoint:<name>` option after an image-capable probe or `vlm` advertisement of the model it resolves to, with the vllm default-option fixture configured `model: auto` on a single-model endpoint so the copy exercises 1.2 resolution rather than a literal id match, while a second unprobed model on the same endpoint renders none; null modalities render no chips. test: `web/src/components/chat/__tests__/ProviderPicker.test.tsx`.\n5.3.2: No new registry or configuration surface is introduced; chips read the existing `/api/providers/models` payload. file: `web/src/components/settings/sections/ProvidersModelsSection.tsx`.\n5.3.3: Image-attachment eligibility follows the chips per model option: an option whose value or owning provider starts with `endpoint:` and has null or absent `input_modalities` is not image-eligible — asserted for a responses-wire `endpoint:*` option with null modalities inside the `provider=codex` entry, a cloud Codex option in that same entry that omits the field and keeps the current fallback, a generic openai-compatible `endpoint:*` group with null modalities, and a default `endpoint:<name>` option carrying probe-copied `[\"text\",\"image\"]` modalities that is image-eligible. test: `web/src/lib/__tests__/providerModels.test.ts`."},{"assigned_agent":"tech-writer","category":"docs","depends_on":["1.2","2.3","3.1","4.1","4.2"],"labels":["covers:vllm-runtime-support:6.1:6.1.1","covers:vllm-runtime-support:6.1:6.1.2","covers:vllm-runtime-support:6.1:6.1.3","covers:vllm-runtime-support:6.1:6.1.4"],"source_section":"6.1","task_type":"feature","tdd":false,"title":"vLLM and vllm-metal guides","validation_criteria":"6.1.1: A copy-pasteable vllm endpoint config with selector examples exists. file: `docs/guides/llm-features.md`.\n6.1.2: Web-chat backend docs describe the Codex config-override transport for vllm. file: `docs/guides/providers-and-models.md`.\n6.1.3: Docs state the `model: auto` exactly-one rule with its multi-model failure mode and a served-model listing diagnostic. file: `docs/guides/llm-features.md`.\n6.1.4: No guide instructs setting a `vision_extract` config field; vision availability is described via probed or advertised image modality, and no endpoint YAML example carries the key. file: `docs/guides/llm-features.md`."}],"round_number":7,"routing_decisions":{"1.1":{"category":"code","implementation_domain":"backend","tdd":true},"1.2":{"category":"code","implementation_domain":"backend","tdd":true},"2.1":{"category":"code","implementation_domain":"backend","tdd":true},"2.2":{"assigned_agent":"backend-developer","category":"refactor","tdd":false},"2.3":{"category":"code","implementation_domain":"backend","tdd":true},"2.4":{"category":"code","implementation_domain":"backend","tdd":true},"2.5":{"category":"code","implementation_domain":"backend","tdd":true},"3.1":{"category":"code","implementation_domain":"backend","tdd":true},"3.2":{"category":"code","implementation_domain":"backend","tdd":true},"4.1":{"category":"code","implementation_domain":"backend","tdd":true},"4.2":{"category":"code","implementation_domain":"backend","tdd":true},"5.1":{"category":"code","implementation_domain":"frontend","tdd":true},"5.2":{"category":"code","implementation_domain":"frontend","tdd":true},"5.3":{"category":"code","implementation_domain":"frontend","tdd":true},"6.1":{"assigned_agent":"tech-writer","category":"docs","tdd":false}},"verdict":"approved"},"session_id":"e39817b3-864e-42b6-87a5-e67e18308b6f"}
```

## M1 Task Manifest
`kind: manifest`

```yaml
- title: Add vllm protocol value and adapter dispatch
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: '1.1.1: `"vllm"` is a valid `GenerationEndpointProtocol` value
    and configures under `ai.generation.endpoints.<name>`. file: `src/gobby/config/ai.py`.

    1.1.2: `create_local_provider_adapter` returns an `OpenAICompatibleLocalProviderAdapter`
    with a non-None `client` for vllm endpoints; no vllm-specific adapter class exists.
    test: `tests/llm/test_local_provider_adapters.py::test_create_adapter_vllm`.

    1.1.3: `wire_api: responses` on a vllm endpoint is rejected by config validation.
    test: `tests/llm/test_local_provider_adapters.py::test_vllm_rejects_responses_wire`.'
  labels:
  - covers:vllm-runtime-support:1.1:1.1.1
  - covers:vllm-runtime-support:1.1:1.1.2
  - covers:vllm-runtime-support:1.1:1.1.3
  tdd: true
  source_section: '1.1'
  implementation_domain: backend
- title: vLLM model lifecycle in ensure_local_model
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  validation_criteria: "1.2.1: `model: auto` on a single-model vllm endpoint resolves\
    \ to the served model; multi-model raises with the served list. symbol: `gobby.agents.local_model.ensure_local_model`.\n\
    1.2.2: vllm endpoints never receive load/unload/keep-alive requests. behavior:\
    \ \"non-owning model lifecycle\" in `src/gobby/agents/local_model.py`.\n1.2.3:\
    \ The literal model value `auto` never reaches a vllm wire request on any path\
    \ \u2014 generation, activation probes, tool_chat, or Codex override args; all\
    \ resolve through the shared resolver. test: `tests/agents/test_local_model.py::test_vllm_auto_resolves_before_wire`.\n\
    1.2.4: `api_base` values with and without a trailing `/v1` both produce a single\
    \ normalized `{origin}/v1/models` URL. test: `tests/agents/test_local_model.py::test_vllm_models_url_normalization`."
  labels:
  - covers:vllm-runtime-support:1.2:1.2.1
  - covers:vllm-runtime-support:1.2:1.2.2
  - covers:vllm-runtime-support:1.2:1.2.3
  - covers:vllm-runtime-support:1.2:1.2.4
  tdd: true
  source_section: '1.2'
  implementation_domain: backend
- title: Optional image inputs on the text-generation core
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  - '1.2'
  validation_criteria: '2.1.1: `TextGenerationRequest` carries optional images; text-only
    requests are byte-identical to today''s payloads. symbol: `gobby.ai._text_generation_contracts.TextGenerationRequest`.

    2.1.2: An image-bearing request against an OpenAI-compatible endpoint renders
    `image_url` content blocks. test: `tests/llm/test_local_provider_adapters.py::test_generate_with_images`.

    2.1.3: Image-bearing requests never route to candidates without image support.
    behavior: "modality-aware candidate filtering" in `src/gobby/ai/_text_generation_service.py`.

    2.1.4: Mixed-catalog routing: text-only models are skipped and an image-capable
    model is selected for image-bearing requests. test: `tests/ai/test_text_generation.py::test_image_routing_mixed_catalog`.

    2.1.5: An explicitly selected text-only endpoint/model with images returns a deterministic
    modality diagnostic. test: `tests/ai/test_text_generation.py::test_image_request_text_only_selection_diagnostic`.

    2.1.6: `POST /api/llm/generate` accepts `images`, forwards them to `TextGenerationRequest.images`,
    and an image-bearing route request reaches an image-capable endpoint end-to-end.
    test: `tests/servers/routes/test_llm_routes.py::test_generate_route_forwards_images`.

    2.1.7: Malformed data URLs, disallowed MIME, invalid base64, relative or unreadable
    paths, count over 8, and aggregate decoded size over 24 MiB are each rejected
    deterministically with a diagnostic naming the offending input. test: `tests/servers/routes/test_llm_routes.py::test_generate_route_image_rejections`.

    2.1.8: An image-bearing request never selects a feature CLI provider (agy/droid/grok/qwen)
    or a generic `codex` binding without endpoint metadata, and the predicate reads
    binding metadata rather than adapter class or spawn style. test: `tests/ai/test_text_generation.py::test_image_routing_skips_generic_codex`.'
  labels:
  - covers:vllm-runtime-support:2.1:2.1.1
  - covers:vllm-runtime-support:2.1:2.1.2
  - covers:vllm-runtime-support:2.1:2.1.3
  - covers:vllm-runtime-support:2.1:2.1.4
  - covers:vllm-runtime-support:2.1:2.1.5
  - covers:vllm-runtime-support:2.1:2.1.6
  - covers:vllm-runtime-support:2.1:2.1.7
  - covers:vllm-runtime-support:2.1:2.1.8
  tdd: true
  source_section: '2.1'
  implementation_domain: backend
- title: Collapse vision_extract onto the generation core
  category: refactor
  task_type: feature
  depends_on:
  - '2.1'
  - '2.5'
  validation_criteria: '2.2.1: No `describe_image` symbol remains in `src/gobby/llm/`;
    vision extraction executes through the generation core. file: `src/gobby/llm/local_provider_adapters.py`.

    2.2.2: `POST /api/llm/vision/extract` behavior and grant gating are unchanged
    for existing callers. test: `tests/ai/test_vision_extraction.py`.

    2.2.3: Image-bearing generate requests against LM Studio and Ollama serialize
    each provider''s native image shape, matching the pre-port `describe_image` wire
    payloads. test: `tests/llm/test_local_provider_adapters.py::test_native_image_serialization_ported`.

    2.2.4: After this deliverable the image-eligible predicate in `TextGenerationService`
    admits lmstudio and ollama endpoint bindings; image-bearing requests route to
    them. test: `tests/ai/test_text_generation.py::test_image_allowlist_admits_native_transports`.'
  labels:
  - covers:vllm-runtime-support:2.2:2.2.1
  - covers:vllm-runtime-support:2.2:2.2.2
  - covers:vllm-runtime-support:2.2:2.2.3
  - covers:vllm-runtime-support:2.2:2.2.4
  tdd: false
  source_section: '2.2'
  assigned_agent: backend-developer
- title: Chat-completions activation probe and modality metadata
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  - '1.2'
  - '2.1'
  - '2.2'
  - '5.1'
  validation_criteria: "2.3.1: `PUT /api/config/generation-endpoints/{name}/activate`\
    \ probes lmstudio, ollama, and vllm chat-completions endpoints and persists `input_modalities`.\
    \ file: `src/gobby/servers/routes/configuration_generation_endpoints.py`.\n2.3.2:\
    \ `vision_extract` no longer exists as an endpoint config field; vision bindings\
    \ require probed or advertised image modality. symbol: `gobby.ai.registry_builder._generation_endpoint_vision_bindings`.\n\
    2.3.3: A vision probe failure yields a text-only activation, never a hard endpoint\
    \ failure. test: `tests/ai/test_endpoint_activation.py::test_vision_probe_degrades_to_text`.\n\
    2.3.4: On an endpoint serving two models with different modality support, probe\
    \ evidence attaches only to the probed model; the other stays unknown in discovery\
    \ and routing. test: `tests/ai/test_endpoint_activation.py::test_model_scoped_modalities_mixed_endpoint`.\n\
    2.3.5: Changing protocol, `wire_api`, `api_base`, `model`, or `api_key` (including\
    \ an api_key unset) clears persisted modality evidence via the shared identity-clearing\
    \ helper on both non-probe-verified mutation paths: asserted through `PATCH /api/config/values`\
    \ with no follow-up activation (`ConfigValuesService.patch_flat`), and through\
    \ YAML import of an exported document whose identity fields were edited (`ConfigDocumentsService.replace_yaml`)\
    \ \u2014 while a same-identity settings PATCH that resubmits the full endpoint\
    \ object with a `MASKED_SECRET` api_key preserves the evidence, a re-import of\
    \ an unchanged export preserves it, an identity-changing activate (`probe_verified=True`)\
    \ persists its replacement probe evidence instead of clearing it, and a failed\
    \ re-probe leaves no stale image capability anywhere. test: `tests/ai/test_endpoint_activation.py::test_identity_change_invalidates_modalities`.\n\
    2.3.6: A keyless local endpoint activates without an Authorization header; a configured\
    \ key is sent. test: `tests/ai/test_endpoint_activation.py::test_optional_credentials_activation`.\n\
    2.3.7: The probe outcome table holds end-to-end: text failure is fatal; JSON/tool/vision\
    \ failures degrade and persist `probed_json`/`probed_tools` false; `tool_chat:\
    \ false` skips the tool probe (`probed_tools: None`); re-activation restores recovered\
    \ capabilities in metadata and grants. test: `tests/ai/test_endpoint_activation.py::test_probe_outcome_table`.\n\
    2.3.8: First activation of a fresh endpoint reaches its vision probe with no pre-existing\
    \ modality metadata; unknown-to-image-capable and unknown-to-text-only transitions\
    \ both persist correctly. test: `tests/ai/test_endpoint_activation.py::test_vision_probe_bootstrap`.\n\
    2.3.9: Runtime grant derivation follows probe evidence: image-capable evidence\
    \ enables vision_extract; degraded or cleared evidence removes it. test: `tests/runtime_grants/test_active_config_binding.py::test_vision_grant_follows_probe_evidence`.\n\
    2.3.10: The Responses activation path persists the same probed evidence shape,\
    \ and a Responses endpoint without evidence has no vision binding, no image modality,\
    \ and no grant. test: `tests/ai/test_endpoint_activation.py::test_responses_path_evidence_migration`.\n\
    2.3.11: A stored endpoint document carrying `vision_extract: true` loads successfully\
    \ after the field's deletion and presents as probe-unknown \u2014 no vision binding,\
    \ no image modality, no grant. test: `tests/ai/test_endpoint_activation.py::test_vision_extract_field_stripped_on_load`.\n\
    2.3.12: Registry construction succeeds for a generation endpoint after the field's\
    \ deletion, and text_generate binding metadata carries no `vision_extract` key.\
    \ test: `tests/ai/test_capability_registry.py::test_text_bindings_drop_vision_extract_metadata`."
  labels:
  - covers:vllm-runtime-support:2.3:2.3.1
  - covers:vllm-runtime-support:2.3:2.3.2
  - covers:vllm-runtime-support:2.3:2.3.3
  - covers:vllm-runtime-support:2.3:2.3.4
  - covers:vllm-runtime-support:2.3:2.3.5
  - covers:vllm-runtime-support:2.3:2.3.6
  - covers:vllm-runtime-support:2.3:2.3.7
  - covers:vllm-runtime-support:2.3:2.3.8
  - covers:vllm-runtime-support:2.3:2.3.9
  - covers:vllm-runtime-support:2.3:2.3.10
  - covers:vllm-runtime-support:2.3:2.3.11
  - covers:vllm-runtime-support:2.3:2.3.12
  tdd: true
  source_section: '2.3'
  implementation_domain: backend
- title: Honest tool_chat availability for clientless adapters
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  - '2.3'
  validation_criteria: '2.4.1: `tool_chat` on an lmstudio/ollama endpoint reports
    unavailable with a reason instead of failing at dispatch; vllm endpoints dispatch
    successfully. test: `tests/ai/test_capability_registry.py::test_tool_chat_clientless_unavailable`.

    2.4.2: A failed tool probe (`probed_tools: false`) reports unavailable with the
    probe reason while config `tool_chat` stays untouched; absent evidence preserves
    the client-gated behavior. test: `tests/ai/test_capability_registry.py::test_tool_binding_probe_evidence_gate`.'
  labels:
  - covers:vllm-runtime-support:2.4:2.4.1
  - covers:vllm-runtime-support:2.4:2.4.2
  tdd: true
  source_section: '2.4'
  implementation_domain: backend
- title: 'Cloud transport image mapping: Claude SDK and Codex endpoint'
  category: code
  task_type: feature
  depends_on:
  - '2.1'
  validation_criteria: "2.5.1: An image-bearing request through the Claude SDK transport\
    \ renders SDK image content blocks in the outgoing query. test: `tests/llm/test_claude.py::test_generate_text_with_images_renders_blocks`.\n\
    2.5.2: An image-bearing request through a responses-wire Codex endpoint emits\
    \ `--image` file arguments, materializes data-URL inputs to temp files with cleanup,\
    \ and never places image bytes in argv. test: `tests/ai/test_text_generation.py::test_codex_endpoint_image_args`.\n\
    2.5.3: Temp files materialized for data-URL images are removed on failure paths\
    \ \u2014 nonzero exit, timeout, and cancellation \u2014 not only on success. test:\
    \ `tests/ai/test_text_generation.py::test_codex_endpoint_image_tempfile_cleanup`.\n\
    2.5.4: After this deliverable the image-eligible predicate admits `provider=claude`\
    \ and responses-wire Codex endpoint bindings while an image-bearing generic `codex`\
    \ binding without endpoint metadata stays skipped. test: `tests/ai/test_text_generation.py::test_image_allowlist_cloud_transports`."
  labels:
  - covers:vllm-runtime-support:2.5:2.5.1
  - covers:vllm-runtime-support:2.5:2.5.2
  - covers:vllm-runtime-support:2.5:2.5.3
  - covers:vllm-runtime-support:2.5:2.5.4
  tdd: true
  source_section: '2.5'
  implementation_domain: backend
- title: Native vLLM model discovery
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  - '1.2'
  - '2.3'
  validation_criteria: '3.1.1: A vllm endpoint''s models appear as `source: "live"`
    entries with provider-reported context length. symbol: `gobby.servers.local_provider_models.discover_local_endpoint_model_group`.

    3.1.2: Discovery failure surfaces the endpoint group with a config-sourced fallback
    and an error, matching lmstudio/ollama behavior. test: `tests/servers/test_local_provider_models.py::test_vllm_discovery_error_fallback`.

    3.1.3: On a two-model vllm endpoint, probe-persisted modalities attach only to
    the probed model''s entry and to the default `endpoint:<name>` option when its
    configured model resolves to `probed_model`; the other model''s modalities are
    null, and a default resolving to the unprobed model stays null. A single-model
    vllm endpoint configured `model: auto` prepends the `endpoint:<name>` default
    option with modalities copied from the probed served id; a two-model endpoint
    configured `model: auto` (resolver error) emits no default option. test: `tests/servers/test_local_provider_models.py::test_vllm_modalities_probed_model_only`.'
  labels:
  - covers:vllm-runtime-support:3.1:3.1.1
  - covers:vllm-runtime-support:3.1:3.1.2
  - covers:vllm-runtime-support:3.1:3.1.3
  tdd: true
  source_section: '3.1'
  implementation_domain: backend
- title: Classify VLMs instead of excluding them
  category: code
  task_type: feature
  depends_on:
  - '2.1'
  - '3.1'
  validation_criteria: '3.2.1: LM Studio `type: "vlm"` models appear in discovery
    tagged `input_modalities: ["text","image"]`. test: `tests/servers/test_local_provider_models.py::test_lmstudio_vlm_classified`.

    3.2.2: Ollama models with a `vision` capability carry image modality; `/api/providers/models`
    local entries expose `input_modalities`. file: `src/gobby/servers/routes/providers.py`.

    3.2.3: `/api/providers/models` `input_modalities` agree with the 2.1 image-routing
    predicate''s decisions for vllm, lmstudio, and ollama catalogs, including each
    group''s default `endpoint:<name>` option after an image-capable advertisement
    or probe of the model it resolves to. test: `tests/servers/test_local_provider_models.py::test_modalities_match_routing_predicate`.'
  labels:
  - covers:vllm-runtime-support:3.2:3.2.1
  - covers:vllm-runtime-support:3.2:3.2.2
  - covers:vllm-runtime-support:3.2:3.2.3
  tdd: true
  source_section: '3.2'
  implementation_domain: backend
- title: Codex chat-wire transport for vllm endpoints
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  - '1.2'
  - '3.2'
  validation_criteria: '4.1.1: A vllm endpoint with an eligible chat model is web-chat
    routable and executes through Codex with config-override transport. symbol: `gobby.ai.codex_endpoint.codex_endpoint_config_overrides`.

    4.1.2: lmstudio/ollama web chat still uses `--oss --local-provider`; generic openai-compatible
    remains catalog-only. test: `tests/servers/test_local_llm.py::test_routable_transport_strategies`.

    4.1.3: `health()` reports endpoint-backend status instead of falling through to
    `unknown`. file: `src/gobby/servers/websocket/chat/runtime_manager.py`.

    4.1.4: Authenticated vllm endpoints keep `wire_api="chat"` with credentials referenced
    only via `env_key`: the key lands in the child-process environment, never in argv,
    serialized `-c` values, or diagnostics, and the override set excludes the variable
    via `shell_environment_policy.exclude`. test: `tests/servers/test_local_llm.py::test_vllm_env_key_credential_transport`.'
  labels:
  - covers:vllm-runtime-support:4.1:4.1.1
  - covers:vllm-runtime-support:4.1:4.1.2
  - covers:vllm-runtime-support:4.1:4.1.3
  - covers:vllm-runtime-support:4.1:4.1.4
  tdd: true
  source_section: '4.1'
  implementation_domain: backend
- title: Agent-spawn parity for vllm endpoints
  category: code
  task_type: feature
  depends_on:
  - '1.2'
  - '4.1'
  validation_criteria: '4.2.1: Spawning a Codex agent against `endpoint:<vllm>/<model>`
    emits config-override args and no `--oss` flag. file: `src/gobby/agents/spawners/command_builder.py`.

    4.2.2: Spawned-agent commands for authenticated vllm endpoints carry the key only
    in the child-process environment via `env_key`; argv and serialized `-c` values
    stay secret-free, and `shell_environment_policy.exclude` covers the variable.
    test: `tests/agents/test_command_builder.py::test_vllm_spawn_env_key_transport`.'
  labels:
  - covers:vllm-runtime-support:4.2:4.2.1
  - covers:vllm-runtime-support:4.2:4.2.2
  tdd: true
  source_section: '4.2'
  implementation_domain: backend
- title: 'Settings: schema-derived protocols and wire_api field'
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  validation_criteria: '5.1.1: The protocol dropdown lists vllm sourced from the daemon
    schema, with no hardcoded protocol list remaining in the component. file: `web/src/components/settings/sections/ProvidersModelsSection.tsx`.

    5.1.2: The editor exposes wire_api and enforces chat-completions for vllm. test:
    `web/src/components/settings/sections/__tests__/ProvidersModelsSection.test.tsx`.

    5.1.3: Switching a protocol to vllm writes `wire_api: "chat-completions"` into
    the submitted payload and switching back restores schema-provided choices, asserted
    on the saved request payload rather than only the rendered select. test: `web/src/components/settings/sections/__tests__/ProvidersModelsSection.test.tsx`.

    5.1.4: The editor no longer renders or submits `vision_extract`; saved payloads
    for every protocol omit the field. test: `web/src/components/settings/sections/__tests__/ProvidersModelsSection.test.tsx`.'
  labels:
  - covers:vllm-runtime-support:5.1:5.1.1
  - covers:vllm-runtime-support:5.1:5.1.2
  - covers:vllm-runtime-support:5.1:5.1.3
  - covers:vllm-runtime-support:5.1:5.1.4
  tdd: true
  source_section: '5.1'
  implementation_domain: frontend
- title: Icons and provider badges
  category: code
  task_type: feature
  depends_on:
  - '3.1'
  validation_criteria: '5.2.1: vllm endpoint groups render a distinct icon in the
    provider picker and session badges classify vllm sessions as local. file: `web/src/components/shared/SourceIcon.tsx`.'
  labels:
  - covers:vllm-runtime-support:5.2:5.2.1
  tdd: true
  source_section: '5.2'
  implementation_domain: frontend
- title: Text/Image capability chips
  category: code
  task_type: feature
  depends_on:
  - '2.3'
  - '3.2'
  - '5.1'
  - '5.2'
  validation_criteria: "5.3.1: Endpoint and local-model rows render Text/Image chips\
    \ from `input_modalities`, covering vllm probe metadata and LM Studio/Ollama advertised\
    \ metadata \u2014 including the preferred default `endpoint:<name>` option after\
    \ an image-capable probe or `vlm` advertisement of the model it resolves to, with\
    \ the vllm default-option fixture configured `model: auto` on a single-model endpoint\
    \ so the copy exercises 1.2 resolution rather than a literal id match, while a\
    \ second unprobed model on the same endpoint renders none; null modalities render\
    \ no chips. test: `web/src/components/chat/__tests__/ProviderPicker.test.tsx`.\n\
    5.3.2: No new registry or configuration surface is introduced; chips read the\
    \ existing `/api/providers/models` payload. file: `web/src/components/settings/sections/ProvidersModelsSection.tsx`.\n\
    5.3.3: Image-attachment eligibility follows the chips per model option: an option\
    \ whose value or owning provider starts with `endpoint:` and has null or absent\
    \ `input_modalities` is not image-eligible \u2014 asserted for a responses-wire\
    \ `endpoint:*` option with null modalities inside the `provider=codex` entry,\
    \ a cloud Codex option in that same entry that omits the field and keeps the current\
    \ fallback, a generic openai-compatible `endpoint:*` group with null modalities,\
    \ and a default `endpoint:<name>` option carrying probe-copied `[\"text\",\"image\"\
    ]` modalities that is image-eligible. test: `web/src/lib/__tests__/providerModels.test.ts`."
  labels:
  - covers:vllm-runtime-support:5.3:5.3.1
  - covers:vllm-runtime-support:5.3:5.3.2
  - covers:vllm-runtime-support:5.3:5.3.3
  tdd: true
  source_section: '5.3'
  implementation_domain: frontend
- title: vLLM and vllm-metal guides
  category: docs
  task_type: feature
  depends_on:
  - '1.2'
  - '2.3'
  - '3.1'
  - '4.1'
  - '4.2'
  validation_criteria: '6.1.1: A copy-pasteable vllm endpoint config with selector
    examples exists. file: `docs/guides/llm-features.md`.

    6.1.2: Web-chat backend docs describe the Codex config-override transport for
    vllm. file: `docs/guides/providers-and-models.md`.

    6.1.3: Docs state the `model: auto` exactly-one rule with its multi-model failure
    mode and a served-model listing diagnostic. file: `docs/guides/llm-features.md`.

    6.1.4: No guide instructs setting a `vision_extract` config field; vision availability
    is described via probed or advertised image modality, and no endpoint YAML example
    carries the key. file: `docs/guides/llm-features.md`.'
  labels:
  - covers:vllm-runtime-support:6.1:6.1.1
  - covers:vllm-runtime-support:6.1:6.1.2
  - covers:vllm-runtime-support:6.1:6.1.3
  - covers:vllm-runtime-support:6.1:6.1.4
  tdd: false
  source_section: '6.1'
  assigned_agent: tech-writer
```
