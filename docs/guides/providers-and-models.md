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

Provider selection is configured on feature-specific routing fields such as
`chat.candidates`, `session_summary.candidates`, and
`gobby-tasks.validation.candidates`. Candidate values use `provider/model`
format for backends such as:

- `claude`
- `codex`
- `droid`
- `grok`
- `qwen`
- `agy`

Feature configs choose preferred candidate order; provider availability, auth
mode, and model details come from CLI discovery, local compatible backends, and
shipped catalog metadata. Gemini-family model IDs remain available through the
AGY and Droid catalogs; they are models, not a separate Gobby provider.

Auth modes are provider-specific. Examples include subscription auth, API-key
auth, and ADC-style auth for providers that support it.

## Model Catalog

The provider model catalog:

- Starts from shipped static model metadata.
- Discovers live models from provider CLIs or local compatible backends.
- Caches live discovery to `~/.gobby/provider-model-catalog.json`.
- Filters hidden models before returning data to the UI.
- Records whether entries came from live discovery, cache, static defaults, or a
  failed discovery path.

Context lengths are included for known shipped models. Unknown or dynamic models
may have less complete metadata.

## Capability Matrix

`src/gobby/servers/provider_model_capabilities.py` defines the immutable
`ProviderModelCapability` record and `build_capability_matrix()`. Each row is
keyed by `(provider, model_id)`, where `provider` is lowercased and `model_id`
is the normalized `canonical_id` when present, otherwise the normalized catalog
`value`. Provider is part of the key because the same model can expose different
reasoning or speed capabilities through different routes.

The matrix covers `claude`, `codex`, `droid`, `grok`, and `qwen`. Membership is
catalog-driven: a model must appear in that provider's current last-good
snapshot. AGY remains outside the matrix pending #18653, so AGY models retain
their existing provider-catalog behavior.

Each record contains:

| Field | Source and meaning |
|-------|--------------------|
| `provider` | Provider routing catalog; forms the first part of the key. |
| `model_id` | Catalog `canonical_id` or `value`, normalized; forms the second part of the key. |
| `supported_reasoning_efforts` | The provider catalog entry's `reasoning.supported_efforts`. An empty tuple means the catalog declares no efforts. |
| `context_limit` | Model-ID metadata supplied to `build_capability_matrix()` as `ModelMetadata.context_length`. `ProviderModelCatalog.all_capabilities()` builds this input from normalized context metadata on the current provider snapshots. Unknown context remains `null`. |
| `speed_multiplier` | Explicit provider-catalog metadata. Droid values come from the multiplier column in [Factory's model documentation](https://docs.factory.ai/models.md). Unknown multipliers remain `null`. |
| `speed_tier` | Derived by `SpeedTier.from_multiplier()`: multipliers greater than `1` are `fast`; all other values are `standard`. |

`ProviderModelCatalog.all_capabilities()` assembles the complete mapping.
`ProviderModelCatalog.capability_for(provider, model_id)` performs normalized
lookup and returns `None` when no matrix row exists.

### Declaring A Fast Variant

Declare an accelerated route as its own provider catalog entry. Set:

- `value` (and `canonical_id` when the provider uses one) to the route's actual
  model ID.
- `base_model_id` to the corresponding standard model ID.
- `speed_multiplier` to the documented acceleration multiplier.
- `reasoning.supported_efforts` to the efforts accepted by that route.

For Droid, these entries live in `DROID_MODEL_CATALOG` in
`src/gobby/servers/provider_model_defaults.py`. The currently quantified routes
are `gpt-5.5-fast` at `5.0`, `claude-opus-5-fast` at `4.0`, and
`gpt-5.3-codex-fast` at `1.4`. Other providers use the same fields in their own
routing catalogs when they expose a distinct accelerated route. Capability
assembly never infers fast status from a model-name suffix.

### Consumers

- `src/gobby/servers/routes/providers.py` uses `_with_model_capabilities()` to
  add `supported_reasoning_efforts`, `context_limit`, `speed_tier`, and
  `speed_multiplier` to matching models returned by
  `GET /api/providers/models`.
- `src/gobby/agents/reasoning.py` uses
  `resolve_spawn_reasoning()` and `ProviderModelCatalog.capability_for()` to
  validate a requested spawn reasoning effort. A present matrix row is
  authoritative; models without a row retain the provider-catalog fallback.
- A future `/fast` surface can select routes from `speed_tier` and
  `speed_multiplier`; the current consumers expose and validate the metadata.

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

- `src/gobby/config/feature_base.py`: feature routing candidate schema.
- `src/gobby/config/feature_candidate_defaults.py`: default feature candidates.
- `src/gobby/servers/routes/providers.py`: `/api/providers` and
  `/api/providers/models`, including capability-field enrichment.
- `src/gobby/servers/provider_models.py`: model catalog discovery, cache, and
  capability lookup.
- `src/gobby/servers/provider_model_capabilities.py`: capability record and
  matrix assembly.
- `src/gobby/servers/provider_model_defaults.py`: static provider catalogs and
  Droid fast-route declarations.
- `src/gobby/storage/model_metadata.py`: provider-independent model metadata.
- `src/gobby/agents/reasoning.py`: spawn reasoning validation.
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

_Last verified: 2026-08-04_
