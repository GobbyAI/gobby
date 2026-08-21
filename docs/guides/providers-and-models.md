# Providers And Models

Providers and models define which AI backends Gobby can use, which models are
available, and how the Web UI exposes model selection for chat and agents.

## Mental Model

Provider readiness and model capabilities are separate concerns.

Provider availability answers whether a backend can run now: is the CLI present,
is auth configured, does the local backend respond, and is the provider enabled?

The capability matrix answers which canonical model IDs, reasoning levels,
context limits, execution routes, and provenance are available for each
provider. Rows come from durable last-good snapshots populated by live
collectors or empty-store bundled seeds.

Do not infer provider from model name. Gobby tracks provider as explicit source,
chat state, or matrix key because providers can expose overlapping model
strings.

## Quick Start

Check provider health:

```bash
curl -sS http://localhost:60887/api/providers
```

Check the provider-model capability matrix:

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
mode, and model details come from provider collectors, configured local
backends, and bundled cold-start rows. Gemini-family model IDs remain available
through AGY and Droid; they are models, not a separate Gobby provider.

Auth modes are provider-specific. Examples include subscription auth, API-key
auth, and ADC-style auth for providers that support it.

## Capability Matrix

The matrix is the source of truth for provider-scoped model identity,
reasoning, context, execution routes, and fact provenance. Rows are keyed by
`(provider, canonical_model)` because the same model can expose different
capabilities through different providers. `CapabilityResolver` matches the
canonical ID or an explicit alias; it does not infer facts from model names.

`GET /api/providers/models` returns this envelope for matrix-backed providers:

```json
{
  "providers": [
    {
      "provider": "codex",
      "available": true,
      "models": [
        {
          "canonical_model": "example-model",
          "display_name": "Example Model",
          "aliases": [],
          "available": true,
          "hidden": false,
          "is_default": false,
          "context_length": {"value": null, "source": "unknown"},
          "max_output_tokens": {"value": null, "source": "unknown"},
          "latency_class": null,
          "reasoning": {
            "status": "unknown",
            "supported_efforts": null,
            "default_effort": null
          },
          "input_modalities": null,
          "supports_tools": null,
          "routes": {
            "standard": {
              "selector": "example-model",
              "available": true,
              "usage_multiplier": null,
              "throughput_multiplier": null,
              "latency_class": null,
              "activations": []
            },
            "fast": {
              "selector": "example-model",
              "available": true,
              "usage_multiplier": null,
              "throughput_multiplier": null,
              "latency_class": "fast",
              "activations": [
                {
                  "kind": "request_parameter",
                  "surface": "app-server",
                  "params": {"name": "serviceTier", "value": "priority"}
                }
              ]
            }
          },
          "provenance": {
            "selector": {
              "source_key": "app-server-model-list",
              "source_url": null,
              "observed_at": "2026-08-04T00:00:00+00:00"
            }
          }
        }
      ],
      "refresh": {
        "generation": 12,
        "sources": [
          {
            "source_key": "app-server-model-list",
            "source_url": null,
            "required": true,
            "state": "ok",
            "attempts": 4,
            "last_attempt_at": "2026-08-04T00:00:00+00:00",
            "last_success_at": "2026-08-04T00:00:00+00:00",
            "last_error": null
          }
        ]
      }
    }
  ]
}
```

Hidden rows stay in durable storage and are omitted from the HTTP response.
Unknown numeric facts use `{ "value": null, "source": "unknown" }`.
Reasoning `status` distinguishes `known`, `unsupported`, and `unknown`; a
`null` effort list means the source did not report the fact, while `[]` means it
explicitly reported no supported efforts.

### Refresh Health And Provenance

Each provider snapshot has an atomic `generation` and per-source health. Source
states are `pending`, `ok`, `stale`, or `error`. `attempts` and the attempt,
success, and error fields explain freshness without invalidating last-good
models. A failed collection records source health and preserves the previous
capability rows; a successful collection replaces the provider snapshot in one
transaction.

Capability collectors own provider-specific discovery for Claude, Codex, Droid,
Grok, and Qwen. On an empty database, bundled Claude and Droid snapshots provide
cold-start rows with `stale` source health and `bundled` provenance. Startup then
refreshes collectors concurrently, with a 30-second source timeout, and repeats
every 24 hours. Successful live facts retain their `source_key`, optional
`source_url`, and `observed_at` per field. AGY retains static response rows
pending #18653.

### Speed Routes And Results

Every model may expose `standard` and `fast` routes. A route supplies the exact
provider selector, availability, optional decimal `usage_multiplier` and
`throughput_multiplier`, optional `latency_class`, and ordered activation
descriptors. Supported activation kinds are `model_selector`, `cli_config`, and
`request_parameter`; each activation names the execution `surface` where it is
valid. Accelerated behavior is declared by the source and is never inferred
from a model-name suffix.

Spawn, WebSocket chat, chat-completions, and tool-chat requests accept
`speed_mode: "standard" | "fast"`; omission means `standard`. The field is
request-scoped and is not saved in launch defaults, resume metadata, or chat
session state. Agent CLI spawn exposes the same request as `--fast`.

Successful execution metadata includes:

```json
{
  "speed": {
    "requested": "fast",
    "effective": "fast",
    "status": "fast_applied",
    "reason": null
  }
}
```

`status` is `standard`, `fast_configured`, `fast_applied`,
`fast_unavailable`, or `fast_degraded`. Provider-echoed model or tier metadata
confirms `fast_applied`; a confirmed fallback preserves provider output and
reports `fast_degraded` with a reason. `fast_unavailable` is a typed
pre-dispatch error and performs no model substitution.

## Web Chat Backends

The web chat provider controls use:

- `/api/providers` for provider availability.
- `/api/providers/models` for grouped model choices.
- Chat session state for selected provider, model, and reasoning effort.
- Per-send `speed_mode`, which resets to `standard` for the next send.

Configured `ai.generation.endpoints` appear as `endpoint:<name>` groups. Web-chat
routability is protocol-specific and always requires the Codex CLI:

| Protocol | Transport | Picker |
| --- | --- | --- |
| `lmstudio`, `ollama` | Codex OSS (`--oss --local-provider`) | Shown when discovery is healthy with at least one eligible chat model |
| `vllm` | Codex config-override (`wire_api="chat"`, provider id `gobby-vllm-<endpoint>`) — not `--oss` | Same health + Codex CLI gate as OSS backends; `model: auto` is resolved before attach so the sentinel never reaches Codex. The server must run with `--enable-auto-tool-choice` and a model-matched tool-call parser; an endpoint whose activation tool probe failed (`probed_tools: false`) is hidden from the picker until re-activation succeeds |
| `openai-compatible` | none | Catalog-only: visible in Settings, hidden from the picker |

Unavailable groups stay in Settings and stay hidden from the picker.

Relevant UI owners include `ProviderPicker`,
`ChatInputModelControls`, `web/src/lib/providerModels.ts`, and
`web/src/hooks/useChat/*`.

## Local Model Warmup

Qwen can use OpenAI-compatible local backends when configured with OpenAI auth
type. The warmup helper resolves local endpoints such as:

- LM Studio on port `1234`.
- Ollama on port `11434`.

It reads Qwen settings from user and project settings files and can prepare the
model before a chat run. This warmup does not write capability rows.

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

There is no dedicated public capability-matrix MCP server. Agents encounter
providers through spawn/chat tools, workflow configuration, and session metadata.
When a task needs provider state, inspect the relevant server through progressive
discovery and prefer explicit provider fields over model-name parsing.

## File Locations

- `src/gobby/config/feature_base.py`: feature routing candidate schema.
- `src/gobby/servers/routes/providers.py`: `/api/providers` and the matrix
envelope returned by `/api/providers/models`.
- `src/gobby/providers/capabilities/models.py`: immutable capability, route,
source-health, activation, and provenance types.
- `src/gobby/providers/capabilities/collectors/`: provider-specific discovery.
- `src/gobby/providers/capabilities/store.py`: PostgreSQL snapshot storage.
- `src/gobby/providers/capabilities/refresh.py`: startup and periodic refresh.
- `src/gobby/providers/capabilities/seed.py`: empty-store cold-start rows.
- `src/gobby/providers/capabilities/resolve.py`: context, reasoning, and route
resolution.
- `src/gobby/providers/capabilities/apply.py`: surface activation and typed
speed result reporting.
- `src/gobby/storage/model_metadata.py`: provider-independent model metadata.
- `src/gobby/agents/reasoning.py`: spawn reasoning validation.
- `src/gobby/servers/websocket/chat/local_openai_warmup.py`: local model warmup.
- `web/src/components/chat/ProviderPicker.tsx`: provider picker UI.
- `web/src/components/chat/ChatInputModelControls.tsx`: model controls.
- `web/src/lib/providerModels.ts`: provider model API client.
- `web/src/hooks/useChat/`: chat state and provider selection.

## See Also

- [web-ui.md](web-ui.md)
- [agents.md](agents.md)
- [configuration.md](configuration.md)
- [llm-features.md](llm-features.md)
- [observability.md](observability.md)

_Last verified: 2026-08-20_
