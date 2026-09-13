# AI Daemon Capability Contract

This contract defines the daemon AI routes consumed by the Rust CLIs. It aligns the CLI capability names in `crates/gcore/src/config.rs` with the daemon-side provider registry. Grant-backed capability flags are the CLI availability source of truth; the CLIs do not probe daemon status endpoints to choose a route.

## Capability/Transport Model (A2)

AI work is routed per capability. The shared capability names are stable wire values:

| Capability | Wire value | Primary CLI use | Daemon route family |
|---|---|---|---|
| Audio transcription | `audio_transcribe` | Speech to source-language text | `/api/voice/*` |
| Audio translation | `audio_translate` | Speech to translated text | `/api/voice/*` |
| Vision extraction | `vision_extract` | Image description and optional OCR text | `/api/llm/vision/*` |
| Text generation | `text_generate` | Prompted text generation | `/api/llm/*` |
| Embeddings | `embed` | Semantic vectors | `/api/embeddings` and `/api/embeddings/status` |

Routing is selected per capability as `daemon` or `off`.

- `daemon` sends requests to the daemon URL resolved from `~/.gobby/bootstrap.yaml` and includes the local CLI token and presented grant.
- `off` reports the capability unavailable.

CLI AI capability config resolves from daemon-served grant-backed keys. There is no client credential file and no `GOBBY_*` environment layer for AI capability config. There is no Auto or Direct route and no probe of daemon status endpoints to decide availability. Consumer flags are command-specific; gcode has no global `--no-ai` option.

The daemon does not accept `ai.text_generate.*` writes in `config_store`; daemon text generation resolves providers from the daemon runtime config instead. Named endpoints live under `ai.generation.endpoints.<name>` and are selected with providers such as `endpoint:lm-studio`. The removed `ai.generation.local.*` namespace and `local:` selectors are rejected.

Daemon-side transports are implementation details behind a capability binding. `openai_compatible_http` means the daemon proxies to an OpenAI-compatible endpoint. `daemon_native` is reserved for daemon-native implementations. For audio, `voice.openai_compatible_audio` is a list of bindings with unique provider IDs. Each binding has `provider`, `url`, `model`, optional `api_key`, `timeout_seconds`, `transcription_enabled`, and `translation_enabled`; see `OpenAICompatibleAudioBindingConfig` in `src/gobby/config/voice.py`.

## Capability Status Routes

The grant is the CLI availability source of truth. `GET /api/providers/models` is used only to enumerate provider and model choices; it must never make a capability routable by itself.

The daemon advertises capability support on these status routes:

| Capability | Probe method | Probe path | Required advertisement |
|---|---:|---|---|
| `audio_transcribe` | `GET` | `/api/voice/status` | `transcription_enabled: true` |
| `audio_translate` | `GET` | `/api/voice/status` | `translation_enabled: true` |
| `vision_extract` | `GET` | `/api/llm/vision/status` | `available: true` in the returned capability status object |
| `text_generate` | `GET` | `/api/llm/status` | `capabilities.text_generate.available: true` |
| `embed` | `GET` | `/api/embeddings/status` | Diagnostic status; the grant remains the availability authority |

Registry capability status objects contain `capability`, `available`, `state`,
`reason`, and `bindings`. `/api/llm/status` nests them under `capabilities`;
the vision status route returns one directly. These shapes are defined by
`AICapabilityRegistry` in `src/gobby/ai/registry.py` and the LLM route handlers.
The voice route supplies its two top-level audio availability booleans.
Do not treat loosely named boolean aliases as the wire contract.

Status bodies support diagnostics. They do not make a CLI capability routable or
replace its grant binding. Audio transcription and translation remain separate
capabilities; failure of one must not disable the other.

## Consumed Routes

### D1 Voice Transcription And Translation

`GET /api/voice/status`

The status body advertises voice capability support independently:

```json
{
  "transcription_enabled": true,
  "translation_enabled": false
}
```

`POST /api/voice/transcribe`

Request is multipart form data:

| Field | Required | Meaning |
|---|---:|---|
| `file` | yes | Audio file bytes. |
| `capability` | no | `audio_transcribe` or `audio_translate`; defaults to `audio_transcribe`. These are `AICapability` values, not `transcribe` or `translate`. |
| `provider` | no | Per-request provider override. |
| `model` | no | Per-request model override. |
| `language` | no | Source language hint. |
| `prompt` | no | Recognition prompt or vocabulary hint. |

Response:

```json
{
  "text": "hello world",
  "segments": [
    { "start": 0.0, "end": 1.2, "text": "hello world" }
  ],
  "language": "en",
  "model": "whisper-large-v3",
  "task": "transcribe"
}
```

`task` is the faster-whisper task value, `transcribe` or `translate`. `text` is the aggregate transcript in the structured response; the daemon must also surface faster-whisper `segments` and detected language instead of discarding them.

### D2 Vision Extraction

`GET /api/llm/vision/status`

The body must advertise `vision_extract` support at capability level.

`POST /api/llm/vision/extract`

Request is multipart form data:

| Field | Required | Meaning |
|---|---:|---|
| `file` | yes | Image file bytes. |
| `provider` | no | Per-request provider override. |
| `model` | no | Per-request model override. |
| `context` | no | Additional image-description context. |

Response:

```json
{
  "description": "A diagram of the index pipeline.",
  "ocr_text": "optional verbatim text",
  "model": "llava",
  "provider": "ollama"
}
```

`ocr_text` is optional and reserved for verbatim extracted text. If absent, the CLI treats it as `None` and renders description-only. `POST /api/chat/attachments` is upload ingress only; it is not a fallback for image description.

### D3 Text Generation

`GET /api/llm/status`

The body must advertise `text_generate` support at capability level.

`POST /api/llm/generate`

Request body:

```json
{
  "prompt": "Write a concise title.",
  "system_prompt": "Use project terminology.",
  "provider": "endpoint:lm-studio",
  "model": "Qwen3-Coder-30B-A3B-Instruct",
  "profile": "feature_low",
  "candidates": ["endpoint:lm-studio/Qwen3-Coder-30B-A3B-Instruct", "claude/haiku"],
  "max_tokens": 128,
  "cwd": "/repo"
}
```

Supported request fields are `prompt`, `system_prompt` or legacy `system`,
`provider`, `model`, `profile`, `candidates`, `max_tokens`, `reasoning_effort`,
`cwd`, `images`, `candidate_timeout_seconds`, `cli_candidate_timeout_seconds`
and `total_timeout_seconds`. `project_id` is not a supported request field and
must not be relied on for scoping. When `provider`, `model`, `profile`, and `candidates` are all omitted,
the daemon resolves `/api/llm/generate` through the `feature_low` default
candidates. Explicit `candidates` take precedence over `provider` and `model`;
explicit `provider` or `model` takes precedence over profile defaults.

Named daemon generation endpoints use `endpoint:<name>` as the provider.
Candidate strings require the explicit `endpoint:<name>/<model>` form.
Bare `endpoint` is invalid; a selector must name its endpoint.
Endpoint config is daemon-owned and shaped as
`ai.generation.endpoints.<name>`, including protocol, wire API, URL, model and
optional authentication. Activation owns the probed capability evidence. CLIs
pass `provider`, `model`, `profile`, or `candidates` to daemon requests.

Response:

```json
{
  "text": "Index Pipeline Overview",
  "model": "Qwen3-Coder-30B-A3B-Instruct",
  "provider": "endpoint:lm-studio"
}
```

### D4 Provider And Model Discovery

`GET /api/providers/models` exposes provider readiness plus the durable
provider-model capability matrix:

```json
{
  "providers": [
    {
"provider": "codex",
"available": true,
"models": [
  {
    "canonical_model": "example-model",
    "reasoning": {
      "status": "unknown",
      "supported_efforts": null,
      "default_effort": null
    },
    "context_length": { "value": null, "source": "unknown" },
    "max_output_tokens": { "value": null, "source": "unknown" },
    "provenance": {}
  }
],
"refresh": {
  "generation": 1,
  "sources": [
    {
      "source_key": "app-server-model-list",
      "state": "ok",
      "attempts": 1,
      "last_error": null
    }
  ]
}
}
]
}
```

Each canonical model carries reasoning facts, typed context/output facts, and
field provenance. There is no route or speed axis. Refresh source states are `pending`,
`ok`, `stale`, or `error`; collection failure preserves the prior generation's
models. Collectors refresh at daemon startup and every 24 hours. Bundled Claude
and Droid rows seed an empty store with stale health until a live refresh
succeeds. The CLI may use this response to populate choices. Capability availability
still comes from the grant, not from this enumeration route.

## Per-Request Resolution

Each request resolves capability, provider, and model in this order:

1. Explicit request override from the CLI command or request options.
2. Daemon feature default for that capability.
3. Off, with a capability-unavailable degradation.

When `routing=daemon`, the CLI forwards the requested capability where the route requires it and any resolved provider/model values. The daemon owns final provider selection for daemon-routed work. Voice and vision form endpoints do not accept a project selector.

Provider-model execution requests carry no speed parameter. Speed is model
selection: see [Providers And Models](providers-and-models.md#speed-is-model-selection-not-a-mode).

## Capability Error Semantics

Capability errors are typed. If a provider exists but does not support the requested capability, the daemon returns a capability error, not an unknown-provider error.

For generation and vision, HTTP 400 carries the following shape, defined by
`_capability_error_detail` in `src/gobby/servers/routes/llm.py`:

```json
{
  "code": "capability_unavailable",
  "capability": "vision_extract",
  "provider": "ollama",
  "model": "qwen2.5-coder",
  "reason": "provider exists but does not support vision_extract"
}
```

The CLI treats this as capability degradation for the requested capability. Unknown provider remains reserved for provider names absent from the daemon registry.

## D6 Embedding Config Namespace

Embedding config uses the canonical `ai.embeddings.*` namespace:

- `ai.embeddings.provider`
- `ai.embeddings.api_base`
- `ai.embeddings.model`
- `ai.embeddings.api_key`
- `ai.embeddings.query_prefix`
- `ai.embeddings.timeout_seconds`
- `ai.embeddings.dim`

Dimension is configured only with `ai.embeddings.dim`.

`EmbeddingsConfig` in `src/gobby/config/persistence.py` defines the model.
`src/gobby/config/registry.py` maps its Python source fields to the external
`ai.embeddings.*` keys. Consumers receive the resolved runtime configuration;
they must not implement alternate persistent namespaces or direct database writes.

Embedding identity changes use the managed embedding-switch operation. Consult
the public configuration schema for each key's activation policy and
[AI Configuration](ai-configuration.md) for the switch workflow. Ordinary
configuration patches cannot bypass a managed operation.

Schema changes run through the hub migration owner described in
[Hub Install Contract](hub-install-contract.md). Upgrade the native binaries and
schema identity pin as a coherent set. There is no ongoing dual-read or
dual-write embedding namespace transition.

## Memory And Residency

The daemon should serialize model loads or honor keep-alive settings so Whisper, multimodal generation, and embeddings are not all resident at once unless explicitly configured.

This is an unfulfilled coordination requirement, not a current runtime guarantee.
The Ollama generation adapter currently sends `keep_alive: -1`, and there is no
shared residency coordinator across voice, generation, and embeddings. Operators
must budget these services independently; the configuration library does not
introduce a new model-lifecycle operation to close this gap.

_Last verified: 2026-09-12_
