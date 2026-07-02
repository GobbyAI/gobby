# AI Daemon Capability Contract

This contract defines the daemon AI routes consumed by the Rust CLIs. It aligns the CLI capability names in `crates/gcore/src/config.rs`, the probe routes in `crates/gcore/src/ai/probe.rs`, and the daemon-side provider registry.

## Capability/Transport Model (A2)

AI work is routed per capability. The shared capability names are stable wire values:

| Capability | Wire value | Primary CLI use | Daemon route family |
|---|---|---|---|
| Audio transcription | `audio_transcribe` | Speech to source-language text | `/api/voice/*` |
| Audio translation | `audio_translate` | Speech to translated text | `/api/voice/*` |
| Vision extraction | `vision_extract` | Image description and optional OCR text | `/api/llm/vision/*` |
| Text generation | `text_generate` | Prompted text generation | `/api/llm/*` |
| Embeddings | `embed` | Semantic vectors | No daemon status route in this contract |

Routing is selected per capability with `auto`, `daemon`, `direct`, or `off`.

- `daemon` sends requests to the daemon URL resolved from `~/.gobby/bootstrap.yaml` and includes the local CLI token.
- `direct` uses the configured local or OpenAI-compatible endpoint from the CLI process.
- `auto` probes daemon capability status first, then uses configured direct fallback if the capability is not advertised.
- `off` reports the capability unavailable.

CLI AI capability config resolves from daemon-supported `config_store` keys first, then standalone `~/.gobby/gcore.yaml`, then defaults. There is no `GOBBY_*` environment layer for AI capability config. Command flags may still override routing, provider, or model for one invocation.

`ai.text_generate.*` is the CLI standalone/direct namespace. The daemon does not accept `ai.text_generate.*` writes in `config_store`; daemon text generation resolves providers from the daemon runtime config instead. Named local daemon endpoints live under `ai.generation.local.endpoints.<name>` and are selected with providers such as `local:lm-studio`.

Daemon-side transports are implementation details behind a capability binding. `openai_compatible_http` means the daemon proxies to an OpenAI-compatible endpoint. `daemon_native` is reserved for daemon-native implementations. For audio, the daemon binding is `voice.openai_compatible_audio` with `provider`, `url`, `model`, optional `api_key`, `timeout_seconds`, `transcription_enabled`, and `translation_enabled`.

## Probe Routes Match The Contract

The status route is the availability source of truth. `GET /api/providers/models` is used only to enumerate provider and model choices; it must never make a capability routable by itself.

`crates/gcore/src/ai/probe.rs` probes these routes:

| Capability | Probe method | Probe path | Required advertisement |
|---|---:|---|---|
| `audio_transcribe` | `GET` | `/api/voice/status` | `transcription_enabled: true` |
| `audio_translate` | `GET` | `/api/voice/status` | `translation_enabled: true` |
| `vision_extract` | `GET` | `/api/llm/vision/status` | `vision_extract: true` or equivalent capability flag |
| `text_generate` | `GET` | `/api/llm/status` | `text_generate: true` or equivalent capability flag |
| `embed` | none | none | unavailable for daemon routing |

The daemon should return the canonical fields above. The CLI probe accepts these current status shapes:

- Audio transcription: `transcription_enabled`, `openai_compatible_audio.transcription_enabled`, or `voice.openai_compatible_audio.transcription_enabled`.
- Audio translation: `translation_enabled`, `openai_compatible_audio.translation_enabled`, or `voice.openai_compatible_audio.translation_enabled`.
- Vision extraction: `vision_extract`, `vision_extract_enabled`, `extraction_enabled`, `capabilities.vision_extract`, or `enabled`.
- Text generation: `text_generate`, `text_generate_enabled`, `generation_enabled`, `capabilities.text_generate`, or `enabled`.

A reachable status route whose body does not advertise the requested capability degrades that capability. For audio, `transcription_enabled=false` degrades only `audio_transcribe`, and `translation_enabled=false` degrades only `audio_translate`.

## Consumed Routes

### D1 Voice Transcription And Translation

`GET /api/voice/status`

The status body advertises voice capability support independently:

```json
{
  "openai_compatible_audio": {
    "provider": "local-audio",
    "url": "http://127.0.0.1:8000/v1",
    "model": "whisper-large-v3",
    "transcription_enabled": true,
    "translation_enabled": false
  }
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
| `project_id` | no | CLI project UUID when available; scopes the request to the current project. |

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
| `project_id` | no | CLI project UUID when available; scopes the request to the current project. |

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
  "provider": "local:lm-studio",
  "model": "Qwen3-Coder-30B-A3B-Instruct",
  "profile": "feature_low",
  "candidates": ["local:lm-studio/Qwen3-Coder-30B-A3B-Instruct", "claude/haiku"],
  "max_tokens": 128,
  "cwd": "/repo",
  "project_id": "3bf57fe7-2a0c-4074-8912-a83d9cd4df01"
}
```

Supported request fields are `prompt`, `system_prompt` or legacy `system`,
`provider`, `model`, `profile`, `candidates`, `max_tokens`, `project_id`, and
`cwd`. When `provider`, `model`, `profile`, and `candidates` are all omitted,
the daemon resolves `/api/llm/generate` through the `feature_low` default
candidates. Explicit `candidates` take precedence over `provider` and `model`;
explicit `provider` or `model` takes precedence over profile defaults.

Named local daemon generation endpoints use `local:<endpoint>` as the provider.
Candidate strings use `local:<endpoint>/<model>`. Bare `local` is reserved for
the daemon-owned local provider family and is unavailable for text generation.
Endpoint config is daemon-owned and shaped as
`ai.generation.local.endpoints.<name> = { api_base, model, api_key? }`; the
CLIs keep `ai.text_generate.*` for standalone/direct operation and pass
`provider`, `model`, `profile`, or `candidates` to daemon requests.

Response:

```json
{
  "text": "Index Pipeline Overview",
  "model": "Qwen3-Coder-30B-A3B-Instruct",
  "provider": "local:lm-studio"
}
```

### D4 Provider And Model Discovery

`GET /api/providers/models` is unchanged. It enumerates providers and models only:

```json
{
  "providers": [
    {
      "provider": "local:lm-studio",
      "available": true,
      "models": [
        { "value": "qwen/qwen3.6-35b-a3b", "label": "qwen/qwen3.6-35b-a3b" }
      ],
      "source": "config"
    }
  ]
}
```

The CLI may use this response to populate choices. It must still probe the per-capability status route before routing any AI request to the daemon.

## Per-Request Resolution

Each request resolves capability, provider, and model in this order:

1. Explicit request override from the CLI command or request options.
2. Daemon feature default for that capability.
3. Configured local or OpenAI-compatible fallback.
4. Off, with a capability-unavailable degradation.

When `routing=daemon`, the CLI forwards the requested capability where the route requires it, any resolved provider/model values, and `project_id` when available. The daemon owns final provider selection for daemon-routed work.

## Capability Error Semantics

Capability errors are typed. If a provider exists but does not support the requested capability, the daemon returns a capability error, not an unknown-provider error.

Minimum error payload:

```json
{
  "error": {
    "type": "capability_unavailable",
    "capability": "vision_extract",
    "provider": "ollama",
    "model": "qwen2.5-coder",
    "message": "provider exists but does not support vision_extract"
  }
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

Daemon-side writer:

- `cli/installers/embedding.py::_persist_embedding_config`

Daemon-side readers that must prefer `ai.embeddings.*`:

- `EmbeddingsConfig` in `config/persistence.py`
- `servers/http.py`
- `code_index/sync_worker.py`
- `ai/registry.py`
- `memory/vectorstore.py`
- `memory/.../knowledge_graph/code_linker.py`
- `cli/memory/indices.py`
- `utils/deps.py`
- `runner_init/storage.py`
- `search/models.py`
- `mcp_proxy/semantic_search.py`
- `configuration_values.py`

`configuration_ui_settings.py` writes only `ui_settings.*` and is outside the embedding writer scope.

The one-time `config_store` migration is daemon-owned and runs in the hub install or upgrade flow described in `hub-install-contract.md`. It renames existing `embeddings.*` rows to `ai.embeddings.*`, preserves values, and preserves `is_secret`. The CLIs must not rewrite daemon-owned `config_store` rows.

The embedding-config migration removes the same-window co-release requirement. Its dual-write and dual-read phases populate and prefer `ai.embeddings.*` before the 0.5.0 contract cut, so daemon D6 and gcode's matching cut can ship independently without a permanent shim.

## Memory And Residency

The daemon should serialize model loads or honor keep-alive settings so Whisper, multimodal generation, and embeddings are not all resident at once unless explicitly configured.

_Last verified: 2026-06-23_
