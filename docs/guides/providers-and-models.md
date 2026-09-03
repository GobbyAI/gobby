# Providers And Models

Providers and models define which AI backends Gobby can use, which models are
available, and how the Web UI exposes model selection for chat and agents.

## Mental Model

Provider readiness and model capabilities are separate concerns.

Provider availability answers whether a backend can run now: is the CLI present,
is auth configured, does the local backend respond, and is the provider enabled?

The capability matrix answers which canonical model IDs, reasoning levels,
context limits, and provenance are available for each provider. Rows come from
durable last-good snapshots populated by live collectors or empty-store bundled
seeds.

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
reasoning, context, and fact provenance. Rows are keyed by
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
          "provenance": {
            "display_name": {
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
Grok, Qwen, and AGY. On an empty database, bundled Claude, Droid, and AGY snapshots
provide cold-start rows with `stale` source health and `bundled` provenance. Startup then
refreshes collectors concurrently, with a 30-second source timeout, and repeats
every 24 hours. Successful live facts retain their `source_key`, optional
`source_url`, and `observed_at` per field. The AGY collector reads live
`agy --output-format json models` data; the bundled seed remains the fallback when
that 6.3 collector cannot produce a fresh snapshot.

### Speed Is Model Selection, Not A Mode

Gobby has no speed axis. Of the six supported CLIs only Droid offers an
invocation-time speed choice, and there it is an ordinary model id:

| CLI | Version checked | Speed control at invocation |
| --- | --- | --- |
| droid | 0.190.0 | `-m/--model <id>-fast` — fast is a **model id**, not a mode |
| codex | 0.149.1 | None. `service_tier` is a session/app-server parameter, absent from the `config.toml` key set, so `-c` cannot carry it to a spawned CLI |
| claude | 2.1.258 | None. `/fast` is a session slash command; `--effort` is reasoning |
| grok | 1.0.13 | None |
| qwen | 0.22.0 | None |
| agy | 1.1.24 | None |

Droid's `-fast` ids — `claude-opus-5-fast`, `gpt-5.5-fast`,
`gpt-5.3-codex-fast`, `glm-5.2-fast` — are therefore ordinary selectable models.
They appear in `/api/providers/models` and reach `--model` like any other id.
Collectors never fold a `-fast` id into a base model and never infer accelerated
behavior from a model-name suffix.

No request surface takes a speed parameter: spawn, WebSocket chat,
chat-completions, and tool-chat carry model and reasoning effort only.

Claude Code's `/fast` is the one in-session speed toggle, and it belongs to the
user. `gobby-sessions:send_keys` refuses any payload whose first token is
`/fast` with `send_keys_speed_command_forbidden`; `/faster` and the like still
deliver. Two limits are worth stating rather than papering over: an agent can
still reach a pane through `Bash` (`tmux send-keys`), and payload matching is
defeatable by splitting keystrokes across calls. This closes the obvious path
and the one an agent would actually take; the general problem belongs to the
agent-hardening work landing before 0.5.0.

## Web Chat Backends

The web chat provider controls use:

- `/api/providers` for provider availability.
- `/api/providers/models` for grouped model choices.
- Chat session state for selected provider, model, and reasoning effort.

Configured `ai.generation.endpoints` appear as `endpoint:<name>` groups. Web-chat
routability is protocol-specific and always requires the Codex CLI:

| Protocol | Transport | Picker |
| --- | --- | --- |
| `lmstudio`, `ollama` | Codex OSS (`--oss --local-provider`) | Shown when discovery is healthy with at least one eligible chat model |
| `vllm` | Codex config-override (`wire_api="responses"`, provider id `gobby-vllm-<endpoint>`) — not `--oss` | Same health + Codex CLI gate as OSS backends; `model: auto` is resolved before attach so the sentinel never reaches Codex. The server must run with `--enable-auto-tool-choice` and a model-matched tool-call parser; an endpoint whose activation tool probe failed (`probed_tools: false`) is hidden from the picker until re-activation succeeds |
| `openai-compatible` | none | Catalog-only: visible in Settings, hidden from the picker |

Unavailable groups stay in Settings and stay hidden from the picker.

Relevant UI owners include `ProviderPicker`,
`ChatInputModelControls`, `web/src/lib/providerModels.ts`, and
`web/src/hooks/useChat/*`.

## Qwen and Local Endpoints

Qwen models come from ACP discovery, like Grok. When the active Qwen model uses a
loopback-backed `modelProviders` entry, Qwen is excluded provider-wide from metadata
coverage, matching Codex and Claude. Discovery uses only Qwen's ACP model catalog;
Gobby performs zero settings-file model discovery, post-discovery loopback filtering,
or special local-model warmup.

Configured local generation endpoints remain separate `endpoint:<name>` providers.
Qwen web chat uses the normal ACP attach path.

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
- `src/gobby/providers/capabilities/models.py`: immutable capability,
source-health, and provenance types.
- `src/gobby/providers/capabilities/collectors/`: provider-specific discovery.
- `src/gobby/providers/capabilities/store.py`: PostgreSQL snapshot storage.
- `src/gobby/providers/capabilities/refresh.py`: startup and periodic refresh.
- `src/gobby/providers/capabilities/seed.py`: empty-store cold-start rows.
- `src/gobby/providers/capabilities/resolve.py`: context and reasoning
resolution.
- `src/gobby/storage/model_metadata.py`: provider-independent model metadata.
- `src/gobby/agents/reasoning.py`: spawn reasoning validation.
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

_Last verified: 2026-08-30_
