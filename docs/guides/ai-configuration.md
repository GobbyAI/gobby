# AI Configuration

Gobby CLI tools route AI by capability. Each capability can use the daemon,
a direct OpenAI-compatible HTTP endpoint, or stay off. Direct endpoints can be
cloud OpenAI-compatible APIs, a faster-whisper server, or a local text/vision
server such as LM Studio or Ollama.

AI settings resolve from daemon-supported `config_store` keys when the daemon
database is available, then `~/.gobby/gcore.yaml`, then defaults. `GOBBY_*`
environment variables are not an AI configuration layer. CLI flags still
override routing for one invocation. `ai.text_generate.*` remains the
CLI standalone/direct namespace; daemon text generation uses daemon provider
config such as `ai.generation.local.endpoints.<name>` plus request-level
`provider`, `model`, `profile`, or `candidates`.

For daemon route and probe semantics, see the
[AI Daemon Capability Contract](./ai-daemon-contract.md).

## Capability Matrix

| Capability | OpenAI API direct | Daemon | faster-whisper direct | LM Studio/Ollama direct |
| --- | --- | --- | --- | --- |
| Embeddings (`ai.embeddings`) | Supported through an OpenAI-compatible embeddings endpoint. | Not selected by `auto`; gcode embeddings use direct config. | Not applicable. | Supported when the local server exposes an OpenAI-compatible embeddings endpoint. |
| Audio transcription (`ai.audio_transcribe`) | Supported only if the endpoint implements OpenAI-compatible `/v1/audio/transcriptions`. | Supported through daemon voice transcription when available. | Supported and recommended for local STT. | Not supported; these servers serve text, vision, and sometimes embeddings, but not STT. |
| Audio translation (`ai.audio_translate`) | Supported only if the endpoint implements OpenAI-compatible `/v1/audio/translations`; otherwise gwiki transcribes first and uses text generation for segment translation. | Supported through daemon voice transcription/translation when available. | Supported when the server implements the translation endpoint. | Text translation can run here through `ai.text_generate`, but audio upload STT cannot. |
| Vision extraction (`ai.vision_extract`) | Supported through OpenAI-compatible chat completions with image input. | Supported through daemon vision extraction when available. | Not applicable. | Supported when the local model is multimodal and the server accepts OpenAI-compatible image chat input. |
| Text generation (`ai.text_generate`) | Supported through OpenAI-compatible chat completions. | Supported through daemon text generation when available. | Not applicable. | Supported; use the same multimodal model for text, vision, translation, and gcode outlines when possible. |

Important STT constraint: direct speech-to-text needs a server implementing
`/v1/audio/transcriptions`, and direct audio translation needs
`/v1/audio/translations` if you want the backend to translate audio directly.
LM Studio and Ollama can serve text, vision, and embeddings, but they are not
STT servers.

## Routing Keys

Set global defaults with `ai.routing`, then override individual capabilities:

```yaml
ai:
  routing: auto
  max_concurrency: 1
  keep_alive: 5m
  embeddings:
    routing: direct
    api_base: http://localhost:11434/v1
    model: nomic-embed-text
  audio_transcribe:
    routing: direct
    api_base: http://localhost:9000/v1
    model: whisper-large-v3
  vision_extract:
    routing: direct
    api_base: http://localhost:1234/v1
    model: qwen2.5-vl-7b-instruct
  text_generate:
    routing: direct
    api_base: http://localhost:1234/v1
    model: qwen2.5-vl-7b-instruct
```

Valid routing values are `auto`, `daemon`, `direct`, and `off`.

`ai.audio_translate` inherits `routing`, `transport`, `api_base`, `api_key`,
`model`, and `provider` from `ai.audio_transcribe` unless a translate-specific
key is set. `ai.audio_translate.target_lang` remains translate-specific.

When text generation routes through the daemon without an explicit
provider/model pair, requests carry a daemon feature profile (`feature_low`
unless configured). Set `ai.text_generate.profile` to change that default;
explicit `provider`/`model` keys take precedence and suppress the profile
entirely. Call sites can override the profile per request — `gcode codewiki`
sends a heavier profile for aggregate docs (see the
[codewiki guide](./codewiki.md)).

For direct authenticated endpoints, store the token in user-local
`~/.gobby/gcore.yaml` as `api_key`. Daemon-backed `config_store` values may use
`$secret:` references where the daemon runtime config supports them. CLI-only
env config sources reject `$secret:` placeholders instead of passing unresolved
secret names to providers.

Daemon-side agentic generation uses the same profile routing plus a tool policy
from the caller. `gcode codewiki` uses that path for aggregate handbook pages so
the daemon agent can investigate through read-only gcode tools before writing;
direct routes use the local provider-neutral tool loop when configured.

## Mixed Routing Example

This configuration sends audio to a faster-whisper server and images/text to a
local multimodal server:

```yaml
ai:
  max_concurrency: 1
  audio_transcribe:
    routing: direct
    api_base: http://localhost:9000/v1
    model: whisper-large-v3
  audio_translate:
    target_lang: en
  vision_extract:
    routing: direct
    api_base: http://localhost:1234/v1
    model: qwen2.5-vl-7b-instruct
  text_generate:
    routing: direct
    api_base: http://localhost:1234/v1
    model: qwen2.5-vl-7b-instruct
```

Run gwiki with per-invocation routing overrides:

```bash
gwiki ingest-file media/interview.mp4 \
  --translate \
  --transcription-routing direct \
  --vision-routing direct
```

With `--translate`, gwiki first tries the audio translation capability for
English translation. If it needs segment translation to another target
language, it uses `ai.text_generate`, so keep text generation pointed at the
same local multimodal endpoint when using a faster-whisper STT server.

## Privacy Path

Use `--no-ai` for an ingest that must not call the daemon, cloud APIs, or local
AI servers:

```bash
gwiki ingest-file media/private-recording.mp3 --no-ai
```

`--no-ai` forces embeddings, transcription, translation, vision, and text
generation routing to `off` for that command. gwiki still stores the source as
a raw asset and records degraded derived output where applicable.

## Model Budget

Target at most three resident models:

| Slot | Purpose | Guidance |
| --- | --- | --- |
| Embeddings | gcode/gwiki semantic vectors | One small embedding model or endpoint. |
| STT | Audio transcription and direct audio translation | One whisper/faster-whisper model served over OpenAI-compatible audio routes. |
| Multimodal | Vision, text translation, outlines, and text generation | One vision-capable chat model shared by `ai.vision_extract` and `ai.text_generate`. |

Keep `ai.max_concurrency` low, usually `1` on constrained machines, so chunks,
frames, transcription, and text calls share one limiter. Use `ai.keep_alive`
only with providers that understand it; do not send local-provider-only fields
to cloud OpenAI-compatible endpoints unless that endpoint documents support.

_Last verified: 2026-07-01_
