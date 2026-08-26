# LLM Feature Routing

Gobby routes text-generation features through provider/model candidates. Keep config keys in
their external form; for task features that is `gobby-tasks`, matching the MCP server name.

## Profile Candidates

| Profile | Default candidates |
| --- | --- |
| `feature_low` | `claude/haiku`, `codex/gpt-5.6-luna` |
| `feature_mid` | `codex/gpt-5.6-terra`, `claude/sonnet` |
| `feature_high` | `codex/gpt-5.6-sol` (`xhigh` reasoning), `claude/opus` (`high` reasoning) |

Built-in defaults are cloud-only. Local runtimes are opt-in named endpoints and
should be explicit final fallbacks:

```yaml
ai:
  generation:
    endpoints:
      lm-studio:
        protocol: lmstudio
        wire_api: chat-completions
        api_base: http://localhost:1234
        model: google/gemma-4-26b-a4b-qat
        api_key: $secret:LM_STUDIO_KEY
      ollama:
        protocol: ollama
        wire_api: chat-completions
        api_base: http://localhost:11434
        model: qwen3
      vllm:
        protocol: vllm
        wire_api: chat-completions
        api_base: http://localhost:8000/v1
        model: auto
        api_key: $secret:VLLM_API_KEY
      generic:
        protocol: openai-compatible
        wire_api: chat-completions
        api_base: http://localhost:8080/v1
        model: local-model
    profile_defaults:
      feature_low:
        - claude/haiku
        - codex/gpt-5.6-luna
        - endpoint:lm-studio/google/gemma-4-26b-a4b-qat
        - endpoint:vllm
```

Feature candidates use `endpoint:<name>` for the endpoint default or
`endpoint:<name>/<model>` to pin a served id (slashes in the model id are
preserved). Selection skips to the next candidate when the endpoint is
unavailable or does not serve the model. Direct HTTP text generation uses
`provider="endpoint:<name>"` with `model="<model>"`.

Bare `endpoint` is invalid; selectors and providers must name an endpoint.
Vision extraction (`vision_extract`) is a capability-registry route, not an
endpoint config field. Named providers such as `endpoint:lm-studio` or
`endpoint:vllm` expose it only after an activation probe or advertised catalog
records `image` in `input_modalities`. A `vision_extract:` key on the endpoint
document 422s on save (`extra=forbid`).

## vLLM endpoints

Canonical vLLM and the vllm-metal Apple Silicon/MLX plugin share one Gobby
protocol: `vllm`. Gobby never starts, stops, loads, or unloads the server — start
it yourself, then point a named endpoint at the OpenAI-compatible API.

### Copy-pasteable config

```yaml
ai:
  generation:
    endpoints:
      vllm:
        protocol: vllm
        wire_api: chat-completions
        api_base: http://localhost:8000/v1
        model: auto
        api_key: $secret:VLLM_API_KEY
```

`wire_api` must be `chat-completions`. Omit `api_key` when the server is
unauthenticated. When the key is set, Codex web chat and agent spawn put the
resolved secret only in the child environment as `GOBBY_CODEX_ENDPOINT_API_KEY`
(`env_key`); it never appears on argv or in serialized `-c` override values.

### Tool calling (required for Codex web chat and agent spawn)

Start vLLM with `--enable-auto-tool-choice --tool-call-parser <parser>`
(`hermes` for Qwen models). The parser must match the model family — LM Studio
and Ollama infer this from the model template, vLLM does not. A server started
without these flags returns HTTP 400 on every request that carries `tools`.

Activate the endpoint with `tool_chat: true` (the default) so the activation
probe exercises a real tool call. A failed probe persists
`probed_tools: false`, logs a WARNING, returns the server error in the
activate response's `probe_diagnostics.tools`, and hides the endpoint from the
web-chat picker until a re-activation succeeds.

vLLM serves one model per process, so an embedding model runs as a separate
server on its own port; see the embeddings guidance in
[configuration.md](./configuration.md) for `--embedding-provider vllm` and
`gobby embeddings switch --provider vllm`.

Paired selectors for the same endpoint. Auto becomes the picker/candidate value
`endpoint:vllm` after the single served id is resolved:

```yaml
model: auto
```

Pin a served id (`endpoint:vllm/Qwen/Qwen2.5-7B-Instruct` as the candidate):

```yaml
model: Qwen/Qwen2.5-7B-Instruct
```

### `model: auto`

`model: auto` succeeds only when `GET /v1/models` returns exactly one served
model. Zero or multiple served models is an error that names them, for example
`model: auto requires exactly one served vLLM model; found 2: llama-3, mistral`.
Gobby never sends the literal `auto` on the wire.

List served ids against the normalized origin before activation. If
`api_base` is `http://localhost:8000/v1`, the origin is `http://localhost:8000`:

```bash
curl http://localhost:8000/v1/models
```

Do not call `{api_base}/v1/models` (`http://localhost:8000/v1/v1/models`) — that
doubles the suffix. `api_base` values with or without a trailing `/v1` both
resolve to `{origin}/v1/models`. Authenticated servers need
`-H "Authorization: Bearer <key>"`.

### vllm-metal (Apple Silicon)

On Apple Silicon, install the MLX plugin from the official guide:
[vllm-metal installation](https://docs.vllm.ai/projects/vllm-metal/en/latest/installation/).
Use the same Gobby protocol (`vllm`), the same `wire_api: chat-completions`, and
the same `api_base` shape. There is no `vllm-metal` protocol value.

## Feature Routes

| Feature | Config path | Default profile | Call path | Caller/use |
| --- | --- | --- | --- | --- |
| `session_summary` | `session_summary` | `feature_low` | `LLMService.call_feature(config.session_summary, ...)` | `sessions.summary`: session handoff summaries |
| `digest` | `digest` | `feature_low` | `LLMService.call_feature(config.digest, ...)` | `memory.turn_record`, `memory.title_synthesis`: rolling digest and title synthesis |
| `memory.kg` | `memory.kg` | `feature_low` | `LLMService.call_json_feature(config.memory.kg, ...)` | `memory.kg.extract_entities`, `memory.kg.extract_relationships`, `memory.kg.select_outdated_relations` |
| `memory.dream` | `memory.dream` | `feature_mid` | `LLMService.call_json_feature(config.memory.dream, ...)` | `memory.dream`: validated memory maintenance planning |
| `tool_summarizer` | `tool_summarizer` | `feature_low` | `LLMService.call_feature(config.tool_summarizer, ...)` | `tools.tool_summary`, `tools.server_description`: MCP tool/server summaries |
| `recommend_tools` | `recommend_tools` | `feature_mid` | `LLMService.call_feature(config.recommend_tools, ...)` | `mcp_proxy.recommendation.hybrid_rerank`, `mcp_proxy.recommendation.llm`: tool recommendations |
| `import_mcp_server` | `import_mcp_server` | `feature_low` | `LLMService.call_feature(config.import_mcp_server, ...)` | MCP server import synthesis |
| `pipelines.prompt_step` | `pipelines.prompt_step` | `feature_low` | `LLMService.call_feature(config.pipelines.prompt_step, ...)` | `workflows.pipeline.prompt_step`: pipeline prompt steps |
| `skill_description` | `skill_description` | `feature_low` | `LLMService.call_feature(config.skill_description, ...)` | `skills.github_collection.description`: skill description synthesis |
| `merge_resolution` | `merge_resolution` | `feature_mid` | `LLMService.call_feature(config.merge_resolution, ...)` | `worktrees.merge.resolve_hunks`, `worktrees.merge.resolve_full_file`: merge conflict resolution |
| `gobby-tasks.expansion` | `gobby-tasks.expansion` | `feature_high` | `LLMService.call_json_feature(config.gobby_tasks.expansion, ...)` | `tasks.expansion.compile`: task expansion compilation |
| `gobby-tasks.validation` | `gobby-tasks.validation` | `feature_mid` | `LLMService.call_json_feature(config.gobby_tasks.validation, ...)` | `tasks.validation`: task completion validation |
| `chat` | `chat` | `feature_high` | Text-generation service receives `config.chat` candidates | Chat session generation defaults |
| `code_index.symbol_summary.candidates` | `code_index.symbol_summary` | `feature_low` | `TextGenerationService.generate(...)` with `config.code_index.symbol_summary.candidates` | `code_index.symbol_summary`: symbol summaries |

## Outside Feature Helpers

`code_index.symbol_summary` does not call `LLMService.call_feature`. `SymbolSummarizer`
passes `code_index.symbol_summary.profile` and `code_index.symbol_summary.candidates` directly to
`TextGenerationService.generate(...)`.

`vision_extract` is capability-registry routed. The `/api/llm/vision/extract` route builds
`VisionExtractService`, selects an `AICapability.VISION_EXTRACT` binding, and invokes the
provider adapter. It is not routed through low/mid/high feature profiles. For generation
endpoints the binding exists only when probed or advertised `input_modalities` includes
`image` — there is no endpoint `vision_extract` config field.

File and image ingestion belongs to gwiki. MCP `wiki_attach` / `wiki_ingest`, HTTP
`/api/wiki/attach`, and `GwikiGateway.ingest_file` route those files through the gwiki
gateway/CLI path rather than memory storage or feature-profile helper calls.

## See Also

- [providers-and-models.md](providers-and-models.md) — web-chat backends, including the
  Codex config-override transport for vLLM
- [system-requirements.md](system-requirements.md) — local generation runtime table
- [configuration.md](configuration.md) — daemon and project configuration

_Last verified: 2026-08-20_
