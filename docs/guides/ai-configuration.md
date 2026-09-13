# AI Configuration

Gcode uses grant-backed daemon configuration for AI and datastore access.
A live Gobby daemon issues the runtime grant; connection material comes
from that grant. An unavailable daemon cannot issue a new grant, and the
client does not fall back to a locally configured provider.

Capability bindings describe whether a daemon AI capability is available.
The shared Rust AI layer supports `daemon` and `off` routing; `auto` and
`direct` are not supported transport choices. Consumer commands expose only
the options in their own CLI contract. Gcode has no global `--no-ai` or
`--ai-aggregate-*` options.

## Search and embedding behavior

Gcode obtains embedding configuration from the daemon and uses the shared
embedding client. Hybrid search can preserve lexical results when an
optional semantic or graph service is unavailable, reporting degradation.
Commands requiring an unavailable service fail with the command's typed
error rather than inventing results.

An unexpired grant can still authorize its datastore access during a daemon
outage. It does not create a replacement AI service. Once the grant expires,
a new handshake is required.

Structural commands such as `gcode outline` do not generate prose. For
search, embedding configuration, and command options, see the
[gcode user guide](gcode-user-guide.md) and
[search guide](search.md).

## Changing the embedding model

Embedding identity changes use the daemon's staged switch coordinator. The
configuration schema marks these settings as managed; a generic configuration
patch cannot replace the switch workflow.

Operators can inspect choices and progress with:

```bash
uv run gobby embeddings catalog
uv run gobby embeddings doctor
uv run gobby embeddings switch --status
```

Start with `uv run gobby embeddings switch <catalog-key>`. Optional `--provider`
selects `ollama`, `lmstudio`, or `vllm`; otherwise the daemon detects the provider.
For vLLM, supply `--api-base` unless its endpoint is already configured. Starting
a switch returns an operation status; inspect `--status` to track completion.

Use `--resume` for an interrupted run. Use `--abort` to request cleanup before
the coordinator begins flipping active state. Once flipping starts, abort
returns `too_late` and the switch completes forward. A cleanup timeout means
cleanup is still running. Do not delete journals or rewrite configuration rows
to force recovery. Select only one of `--status`, `--resume`, and `--abort`.

The operator CLI delegates to `/api/embeddings/switch/status` (GET) and
`/start`, `/resume`, `/abort` (POST under the same switch prefix). The implementation
is `src/gobby/cli/embeddings.py` and
`src/gobby/ai/embedding_switch_service.py`; HTTP validation and conflict responses
are defined in `src/gobby/servers/routes/embeddings.py`. This is an operator/client
surface, not an additional MCP tool.

## Daemon profiles

Daemon text-generation features use feature profiles to select providers
and models. Profile selection does not choose a separate transport. The
shared agentic request contract also carries the caller's tool policy and
mutation permissions.

See [LLM features](llm-features.md) and
[providers and models](providers-and-models.md) for the surviving daemon
features and their model configuration.

_Last verified: 2026-09-12_
