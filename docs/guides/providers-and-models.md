# Providers And Models

Providers and models define which AI backends Gobby can use, which models are
available, and how the Web UI exposes model selection for chat and agents.

## Mental Model

Provider availability and model catalogs are separate concerns.

Provider availability answers whether a backend can run now: is the CLI present,
is auth configured, does the local backend respond, and is the provider enabled?

The model catalog answers which model IDs, labels, context lengths, and metadata
are available for each provider. Catalog entries may come from static defaults,
live provider discovery, cached discovery, or config overrides.

Do not infer provider from model name. Gobby tracks provider as explicit source,
chat state, or catalog metadata because providers can expose overlapping model
strings.

## Quick Start

Check provider health:

```bash
curl -sS http://localhost:60887/api/providers
```

Check the model catalog:

```bash
curl -sS http://localhost:60887/api/providers/models
```

Open web chat and use the provider/model controls:

```text
http://localhost:60887/#chat
```

Inspect configured defaults in the Gobby configuration files and through the
Configuration page.

## Provider Configuration

Provider configuration is rooted under `llm_providers`. It supports provider
blocks for backends such as:

- `claude`
- `codex`
- `gemini`
- `qwen`

The provider routes also report availability for `droid` when present. Provider
blocks can include enabled state, default model, auth mode, strict JSON behavior,
and model overrides.

Auth modes are provider-specific. Examples include subscription auth, API-key
auth, and ADC-style auth for providers that support it.

## Model Catalog

The provider model catalog:

- Starts from shipped static model metadata.
- Discovers live models from provider CLIs or local compatible backends.
- Caches live discovery to `~/.gobby/provider-model-catalog.json`.
- Applies config overrides.
- Filters hidden models before returning data to the UI.
- Records whether entries came from live discovery, cache, static defaults, or a
  failed discovery path.

Context lengths are included for known shipped models. Unknown or dynamic models
may have less complete metadata.

## Web Chat Backends

The web chat provider controls use:

- `/api/providers` for provider availability.
- `/api/providers/models` for grouped model choices.
- Chat session state for selected provider, model, and reasoning effort.

Relevant UI owners include `ProviderPicker`,
`ChatInputModelControls`, `web/src/lib/providerModels.ts`, and
`web/src/hooks/useChat/*`.

## Local Model Warmup

Qwen can use OpenAI-compatible local backends when configured with OpenAI auth
type. The warmup helper resolves local endpoints such as:

- LM Studio on port `1234`.
- Ollama on port `11434`.

It reads Qwen settings from user and project settings files and can prepare the
model before a chat run. This is a warmup path, not the canonical model catalog.

## CLI

There is no single provider CLI command that owns all provider state. Use:

```bash
uv run gobby status
```

to see installed coding CLIs and model discovery status. Use provider CLIs
directly only when debugging that provider's own auth or installation.

## HTTP

Provider HTTP routes:

```text
GET /api/providers
GET /api/providers/models
```

These routes are the source for Web UI provider controls. They should return
explicit provider grouping and must not rely on model-name inference.

## MCP

There is no dedicated public provider-catalog MCP server. Agents encounter
providers through spawn/chat tools, workflow configuration, and session metadata.
When a task needs provider state, inspect the relevant server through progressive
discovery and prefer explicit provider fields over model-name parsing.

## File Locations

- `src/gobby/config/llm_providers.py`: provider configuration schema.
- `src/gobby/servers/routes/providers.py`: `/api/providers` and
  `/api/providers/models`.
- `src/gobby/servers/provider_models.py`: model catalog discovery and cache.
- `src/gobby/servers/websocket/chat/local_openai_warmup.py`: local model warmup.
- `web/src/components/chat/ProviderPicker.tsx`: provider picker UI.
- `web/src/components/chat/ChatInputModelControls.tsx`: model controls.
- `web/src/lib/providerModels.ts`: provider model API client.
- `web/src/hooks/useChat/`: chat state and provider selection.
- `~/.gobby/provider-model-catalog.json`: live model cache.

## See Also

- [web-ui.md](web-ui.md)
- [agents.md](agents.md)
- [configuration.md](configuration.md)
- [observability.md](observability.md)

_Last verified: 2026-05-08_
