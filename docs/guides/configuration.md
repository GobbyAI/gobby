# Configuration Guide

Gobby uses a DB-backed daemon configuration for runtime settings, a small
bootstrap YAML file for pre-database startup settings, and project-local JSON
for repository verification and hook policy.

## Reactive runtime configuration contract

`ConfigRuntime` is the daemon's single runtime authority. It publishes immutable,
revisioned desired and active snapshots compiled from the typed registry and the
PostgreSQL `config_store`. Each operation captures one snapshot; consumers never
assemble configuration from individual rows.

Every persistent mutation is an atomic compare-and-swap against `config_state.revision`.
The response distinguishes the committed desired revision from activation state:
live keys activate in-process, restart keys remain pending until restart, and managed
keys change only through their named lifecycle action. A committed mutation remains
committed when local live activation fails and reports the failed-live keys.

## Configuration Sources

| Source | Scope | Purpose |
| --- | --- | --- |
| `~/.gobby/bootstrap.yaml` | Machine | Startup values needed before the PostgreSQL hub is open |
| `config_store` table in the PostgreSQL hub | Machine | Revisioned desired runtime overrides |
| `~/.gobby/mcp-servers.json` | Machine | Persistent downstream MCP server registry |
| `.gobby/project.json` | Project | Project identity, verification commands, and project hook settings |
| `~/.gobby/build.yaml` | Machine | Build lifecycle defaults |
| `<project>/.gobby/build.yaml` | Project | Build lifecycle defaults for one repository |

`~/.gobby/config.yaml` is an export/import artifact. Daemon startup reads the
bootstrap topology, registry defaults, and one coherent database snapshot.

## Runtime Configuration

`DaemonConfig` is the typed active projection. Registry defaults plus database
overrides produce its desired value; activation policy determines its active value.
Rows use canonical dotted keys such as `gobby-tasks.validation.candidates` and a
shared codec preserves scalar, list, object, and patterned-key values across Python,
Rust, HTTP, MCP, browser, and YAML surfaces.

Shared indexing behavior is configured under `indexing`. By default, `gcode`
and `gwiki` respect `.gitignore`, `.git/info/exclude`, and global git excludes:

```yaml
indexing:
  respect_gitignore: true
  extra_excludes:
    - generated
    - "*.snapshot"
```

For gcode code indexing, `extra_excludes` accepts component-name glob patterns.
These patterns extend the built-in exclusions; they cannot re-enable built-in
excluded paths.

Project wiki markdown for the default project scope lives under
`<project>/wiki/`. When `wiki/` is occupied by a non-vault path, resolution
falls back to `gobby-wiki/`, then `gobby-wiki-001/`..`gobby-wiki-999/` — the
same order every surface (daemon, `gwiki`, `gcode`) uses via the shared vault
resolver. Git tracks authored and generated Markdown there, while vault runtime
state stays local: `wiki/_meta/**`, `wiki/meta/health/**`, lock files, and
local Obsidian workspace JSON are ignored. Legacy `<project>/gobby-wiki` roots
in `wiki.roots` load as the sibling `<project>/wiki` vault.

### Bootstrap

`~/.gobby/bootstrap.yaml` contains only values needed before the hub database is
available:

```yaml
database_url: "postgresql://gobby:<generated-on-first-install>@localhost:60891/gobby"
postgres_pool:
  acquire_timeout_seconds: 5.0
  open_timeout_seconds: 30.0
  max_lifetime_seconds: 300.0
daemon_port: 60887
bind_host: "localhost"
websocket_port: 60888
ui_port: 60889
```

`database_url` is the sole PostgreSQL selector and the required connection
string for Gobby's Docker-managed container. PostgreSQL is the only runtime
hub. The host must be a local or loopback address; external PostgreSQL servers
are not supported.
Startup fails when the DSN or managed service configuration is missing.

`postgres_pool` configures the daemon's PostgreSQL client pool. All three values
must be positive. These bootstrap values are resolved before `config_store` is
available and are not overridden by config-store rows or `PGPOOL_*` environment
variables.

Root bootstrap stores the local PostgreSQL DSN directly in `database_url`.
`bootstrap.yaml` is written with mode `0600`; keep that permission so the DSN
stays owner-readable only. Gobby-generated helper bootstraps also use
`database_url`; `database_url_ref` values are no longer supported.

Changing bootstrap settings affects startup wiring. Restart the daemon after
editing this file.

Authentication is mandatory. Protected HTTP, MCP, memory, hook, and WebSocket
surfaces accept the install-scoped local token or a user-owned browser session;
there is no bootstrap or runtime switch that disables authentication.

Interactive first installation creates the canonical user, then assigns the
local machine UUID to that user in the same database transaction. A fresh
unattended install refuses account bootstrap and directs the operator to run an
interactive install. Startup requires exactly one installed user until account
selection exists.

Machine ownership is established only by installation or authenticated
enrollment. Hook and session ingress can refresh metadata for a known machine;
an unknown machine UUID is rejected and never claimed implicitly.

For multiple daemons sharing datastores across Tailscale, set each client to
`datastore_mode: remote`, keep its `bind_host` local, and point `database_url` at
the datastore hub. See [shared-stack.md](shared-stack.md) for exposure, secrets,
and upgrade requirements.

### Runtime Overrides

Use the `gobby-config` MCP server or HTTP configuration API for runtime changes.
Both transports call the same `ConfigValuesService` operations:

| Tool | Purpose |
| --- | --- |
| `get_config_schema` | Return the typed registry schema and field metadata |
| `get_config_values` | Return one coherent public desired/active snapshot |
| `patch_config_values` | Atomically set or unset values with revision CAS |

Example MCP calls:

```python
snapshot = call_tool("gobby-config", "get_config_values", {})
call_tool(
    "gobby-config",
    "patch_config_values",
    {
        "expected_revision": snapshot["revision"],
        "values": {"memory": {"enabled": True}},
        "unset": ["memory.crossref_max_links"],
    },
)
```

`get_config_values` exposes public registry entries only. Its response contains
`revision`, nested `desired` and `active` projections, `secret_set` metadata,
`pending_restart_keys`, and `failed_live_keys`. Secret payloads are always masked.

Every write supplies the revision from a cached snapshot. A stale revision returns
a retryable `revision_conflict`; refresh the snapshot before composing another
write. Successful responses report `committed`, `revision`, `changed_keys`, and
an `apply_status` of `applied`, `pending_restart`, `failed_live`, or
`reconcile_failed`. Persistence remains committed when live activation fails.

### HTTP Configuration API

The daemon exposes configuration routes under `/api/config`:

| Route | Purpose |
| --- | --- |
| `GET /api/config/schema` | Return the typed registry schema |
| `GET /api/config/values` | Return the public revisioned desired/active snapshot |
| `PATCH /api/config/values` | Apply a revisioned nested set/unset mutation |
| `GET /api/config/effective` | Return resolved active machine configuration |
| `GET /api/config/template` | Export daemon-owned desired values as masked YAML |
| `PUT /api/config/template` | Atomically replace daemon-owned desired values from YAML |
| `POST /api/config/export` | Alias the YAML export operation |
| `POST /api/config/import` | Alias the revisioned YAML replacement operation |
| `PUT /api/config/generation-endpoints/{name}/activate` | Probe and activate a named endpoint |
| `GET`, `POST`, `DELETE /api/config/secrets` | Manage named secret metadata and payloads |

`GET /api/config/effective` is an authenticated machine route. It requires the
local runtime bearer token, sets `Cache-Control: no-store`, reads one active
snapshot, resolves active secret references, and exports machine-visible keys.
Public-only and restricted-only keys stay outside this projection.

YAML is a daemon-only desired-override document. Replacement requires
`expected_revision`, validates the complete candidate before writing, preserves
masked secret references, restores omitted registry defaults, and commits the
daemon namespace in one CAS. Supplemental namespaces such as prompt overrides,
UI settings, and tool approvals remain intact. Managed keys use their lifecycle
route instead of YAML replacement.

### Environment And Secret Expansion

Configuration strings support secret and environment references:

```yaml
api_key: $secret:OPENAI_API_KEY
fallback_key: ${OPENAI_API_KEY}
local_key: ${OPENAI_API_KEY:-development-key}
```

`$secret:NAME` resolves only from the encrypted secrets store. `${VAR}` checks
the secrets resolver first when one is available, then falls back to the process
environment. `${VAR:-default}` uses the default when the value is unset or empty.
Public reads and YAML exports preserve secrecy through masks and `secret_set`
metadata. The authenticated effective route resolves only the active reference.

## Core Runtime Sections

This section lists the high-signal sections most operators tune. The complete
shape is the `DaemonConfig` schema from `GET /api/config/schema`.

### Daemon And Network

```yaml
daemon_port: 60887
bind_host: localhost
daemon_health_check_interval: 10.0
test_mode: false
cors_origins:
  - http://localhost:*
  - https://localhost:*

websocket:
  enabled: true
  port: 60888
  ping_interval: 30
  ping_timeout: 10

ui:
  enabled: false
  mode: auto
  port: 60889
  host: localhost
```

Ports must be between `1024` and `65535`. Timeouts and intervals must be
positive unless the field explicitly documents `0` as a special value.

### Logging And Telemetry

```yaml
logging:
  level: info
  format: text
  dir: ~/.gobby/logs
  max_size_mb: 10
  backup_count: 5
  llm_max_size_mb: 50
  llm_backup_count: 5
  runtime_max_size_mb: 50
  growth_warn_mb_per_interval: 100

telemetry:
  traces_enabled: true
  metrics_enabled: true
  exporter:
    otlp_endpoint: null
    otlp_protocol: grpc
    otlp_headers: {}
    prometheus_enabled: true
```

`logging.max_size_mb` and `logging.backup_count` control formatted daemon and
parser log rotation. `logging.llm_max_size_mb` and `logging.llm_backup_count`
independently control the higher-volume `llm.log` surface. The 50 MiB default
keeps more feature-call history without enlarging other logs.
`logging.runtime_max_size_mb` is a health threshold for the append-only
`runtime.log`; it does not truncate the file.
`logging.growth_warn_mb_per_interval` controls the warning threshold for total
log-directory growth between resource-monitor samples.

On Windows, quote `logging.dir` and use forward slashes, for example
`"C:/Users/name/.gobby/logs"`. Point an external collector's `GOBBY_LOG_DIR` at
the same absolute directory.

`telemetry.exporter.otlp_endpoint` sends in-process spans from Gobby. External
log collection uses independent `filelog` receiver paths and an independent
collector backend exporter. See [Observability](observability.md#log-files) for
the eight-file taxonomy, rotation behavior, Prometheus queries, privacy notes,
and the tested collector deployment.

### MCP Proxy

```yaml
mcp_client_proxy:
  enabled: true
  connect_timeout: 30.0
  proxy_timeout: 30
  tool_timeout: 30
  tool_timeouts: {}
  search_mode: llm
  min_similarity: 0.3
  top_k: 10
  refresh_on_server_add: true
  refresh_timeout: 300.0
```

`search_mode` accepts `llm`, `semantic`, or `hybrid`. Embedding model settings
live in `embeddings`, shared by memory, skills, code index, and semantic tool
search.

### LLM Feature Routing

```yaml
chat:
  profile: feature_high
  candidates:
    - codex/gpt-5.6-sol@xhigh
    - claude/opus@high
gobby-tasks:
  validation:
    profile: feature_mid
```

Profiles take the enum values `feature_low`, `feature_mid`, and `feature_high`.
Feature configs select ordered `provider/model` candidates. Provider auth and
model discovery come from the provider CLIs, local compatible backends, shipped
catalog metadata, and secrets managed by the relevant provider integration.

### Storage, Embeddings, And Memory

```yaml
databases:
  qdrant:
    url: http://localhost:6333
    api_key: null
    port: 6333
    collection_prefix: code_symbols_
  falkordb:
    host: 127.0.0.1
    port: 16379
    password: null
    graph_name: gobby_kg
    graph_search: true
    graph_min_score: 0.5
    rrf_k: 60

embeddings:
  model: nomic-embed-text
  dim: 768
  api_base: null
  api_key: null

memory:
  enabled: true
  backend: local
  auto_crossref: false
  crossref_threshold: 0.3
  crossref_max_links: 5
  access_debounce_seconds: 60
```

The full `gobby install` and the `gobby install embedding` component accept
`--embedding-url`, `--embedding-provider`, `--embedding-model`, and
`--embedding-dim` to override the bundled provider defaults. Provider identity for a custom URL comes from fingerprinting the
server: Ollama answers `GET /api/tags`, LM Studio answers `GET /api/v1/models`,
vLLM's `/v1/models` entries carry `owned_by: "vllm"`, and any other reachable
`/v1/models` endpoint routes as generic `openai-compatible`. Pass
`--embedding-provider` to override that identification for custom endpoints.

When `--embedding-dim` is omitted alongside a custom URL or model, the
installer probes `/v1/embeddings` on the target endpoint to detect the dim. If
only the endpoint changed and the LM Studio or Ollama default model is still in
use, a failed probe falls back to that provider's default dim and the health
check verifies it. Custom models and generic `openai-compatible` endpoints must
probe successfully or pass `--embedding-dim` explicitly.

Embedding structure is managed configuration. Change `ai.embeddings.model`,
`dim`, `api_base`, `query_prefix`, or `catalog_key` through
`gobby embeddings switch` or `/api/embeddings/switch/*`. The switch lifecycle
stages new physical collections, records a durable journal, and flips the active
generation atomically; generation leases keep collection names pinned during
projection replay. `ai.embeddings.api_key` follows the normal live secret policy
and can be updated through the revisioned values surface.

The default is `nomic-embed-text-v1.5@f16` (768-dim, ~137M params) — a safe
choice for any local hardware. For users with capable local hardware,
`Qwen3-Embedding-4B` (2560-dim, 4B params) is recommended: it is significantly
stronger on MTEB and instruction-aware. Tradeoffs: roughly 3.3× the vector
storage and a slower embed step. Example install:

```bash
gobby install embedding --embedding-url http://localhost:1234/v1 \
                        --embedding-model text-embedding-qwen3-embedding-4b
# --embedding-dim is auto-detected from the endpoint; pass 2560 to skip the probe.
```

A vLLM (or vllm-metal) embedding server is operator-started and serves one
model per process, so it always runs on its own port, separate from any vLLM
generation endpoint. The served model is resolved live from `/v1/models` and
the dim is always probed, never defaulted:

```bash
gobby install embedding --embedding-provider vllm --embedding-url http://localhost:8323/v1
# or switch an existing installation to a vllm-served catalog model:
gobby embeddings switch qwen3-0.6b-q8 --provider vllm --api-base http://localhost:8323/v1
```

`memory.backend` accepts `local` or `null`. Qdrant and FalkorDB connection
settings are required shared infrastructure even when embeddings are disabled;
memory-specific behavior lives under `memory`. Production startup requires the
managed Qdrant URL and FalkorDB credentials to be configured and healthy.

### Sessions

```yaml
session_summary:
  enabled: true
  profile: feature_low

digest:
  enabled: true
  profile: feature_low
  timeout: 30

message_tracking:
  enabled: true
  poll_interval: 5.0
  debounce_delay: 1.0
  max_message_length: 10000
  broadcast_enabled: true

session_lifecycle:
  active_session_pause_minutes: 30
  stale_session_timeout_hours: 24
  expire_check_interval_minutes: 60
  transcript_processing_interval_minutes: 5
  transcript_processing_batch_size: 10
  transcript_archive_dir: ~/.gobby/session_transcripts
```

### Tasks And Workflows

```yaml
gobby-tasks:
  enabled: true
  show_result_on_create: false
  expansion:
    enabled: true
    profile: feature_high
    default_strategy: auto
    timeout: 300.0
    research_timeout: 60.0
  validation:
    enabled: true
    profile: feature_mid
    max_retries: 3
    max_iterations: 10
    run_build_first: true
    build_command: null

workflow:
  enabled: true
  timeout: 90.0
  debug_echo_context: false

hooks:
  adapter_timeout: 105.0
  provider_timeout: 120
```

Task lifecycle automation is stage-manifest based. Docs leaf work can run inside
a parent epic's isolation context. Agent process termination is separate from
task lifecycle completion; agent runs still release resources through
`gobby-agents:end_agent_run`.

Rule authors should target semantic workflow events such as `turn_start`,
`turn_end`, `before_tool`, and `after_tool`. Provider runtime events are adapter
details below that authoring API. See [rules.md](./rules.md) for the complete
rule model.

Hook deadlines must stay strictly ordered:
`workflow.timeout < hooks.adapter_timeout < hooks.provider_timeout`. All three
values must be positive. Changes require a
daemon restart. A `hooks.provider_timeout` change also requires `gobby install`
to rewrite provider settings. Qwen stores the provider value in milliseconds;
Claude caps `SessionEnd` at 60 seconds; Codex keeps its enqueue-only `SessionEnd`
hook at 3 seconds. AGY manages its own timeout contract.

### Code Index

```yaml
code_index:
  enabled: true
  maintenance_interval_seconds: 3600
  maintenance_index_timeout_seconds: 900
  nightly_repair_enabled: true
  nightly_repair_cron: "0 2 * * *"
  nightly_repair_timezone: null
  nightly_repair_timeout_seconds: 28800
  nightly_repair_concurrency: 1
  maintenance_log_file: ~/.gobby/logs/code-index-maintenance.log
  missing_root_purge_observations: 3
  embedding_enabled: true
  graph_enabled: true
  symbol_summary:
    enabled: true
    profile: feature_low
    candidates: []
    batch_size: 20
    max_concurrency: 2
    max_tokens: 100
  sync_worker_interval_seconds: 5.0
  sync_worker_projection_timeout_seconds: 300.0
  sync_worker_batch_size: 50
  sync_worker_breaker_failure_threshold: 5
  sync_worker_breaker_backoff_seconds: 30.0
sync_worker_breaker_max_backoff_seconds: 900.0
```

`nightly_repair_*` schedules `gcode repair`. A changed indexer version uses the
configured timeout for its one-time full reindex; normal runs reconcile pending
local imports and graph projection drift.

gcode owns the supported language and content-extension set. Configure additional
path exclusions with `indexing.extra_excludes`.

### Hooks And Webhooks

```yaml
hook_extensions:
  websocket:
    enabled: true
    broadcast_events:
      - session-start
      - session-end
      - pre-tool-use
      - post-tool-use
    include_payload: true
  webhooks:
    enabled: true
    endpoints: []
    default_timeout: 10.0
    async_dispatch: true
```

Webhook endpoints support custom headers, retries, and blocking behavior through
the `can_block` flag. Failures remain fail-open by default; set `fail_closed: true`
on a blocking endpoint when transport or HTTP failures must block the action.

### Tool Approval And Chat

```yaml
tool_approval:
  enabled: false
  default_policy: auto
  policies: []

chat:
  profile: feature_high
  default_mode: plan
```

Tool approval policies use glob-style `server_pattern` and `tool_pattern`
entries with policy values `auto`, `approve_once`, or `always_ask`.

## MCP Server Registry

Downstream MCP servers are stored in `~/.gobby/mcp-servers.json` and synchronized into
daemon state by the MCP manager. The file has a top-level `servers` array:

```json
{
  "servers": [
    {
      "name": "filesystem",
      "enabled": true,
      "transport": "stdio",
      "command": "npx",
      "args": ["@anthropic-ai/filesystem-mcp"],
      "env": null
    },
    {
      "name": "api-server",
      "enabled": true,
      "transport": "http",
      "url": "https://api.example.com/mcp",
      "headers": {
        "Authorization": "Bearer ${API_TOKEN}"
      },
      "requires_oauth": false
    }
  ]
}
```

Supported transports are `stdio`, `http`, and `websocket` (`sse` entries are
accepted by registry validation but have no transport implementation and cannot
connect). `stdio` servers use `command`, `args`, and `env`. Network transports
use `url` and optional `headers`. `project_id` may be omitted; it defaults to
the global project UUID.

## Project Configuration

`.gobby/project.json` binds a repository to a Gobby project and stores
repository-local verification and hook settings:

```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "name": "my-project",
  "created_at": "2026-01-15T10:30:00Z",
  "verification": {
    "unit_tests": "uv run pytest tests/ -v",
    "type_check": "uv run mypy src/ --no-incremental",
    "lint": "uv run ruff check src/",
    "format": "uv run ruff format --check src/",
    "integration": null,
    "security": null,
    "code_review": null,
    "custom": {
      "frontend_tests": "cd web && npm test"
    }
  },
  "hooks": {
    "pre-commit": {
      "run": ["lint", "format"],
      "fail_fast": true,
      "timeout": 120,
      "enabled": true
    },
    "pre-push": {
      "run": ["type_check", "unit_tests"],
      "fail_fast": false,
      "timeout": 1800,
      "enabled": true
    }
  }
}
```

Verification commands are project-owned and should use the repository's normal
toolchain. For Python projects in this repo, use `uv run` commands. Gobby hooks
read this file through the project context utilities; task expansion also uses
the verification section when generating validation criteria.

The committed schema contains only repository-portable fields:

| Field | Purpose |
| --- | --- |
| `id` | Stable project UUID shared by every clone |
| `name` | Project display name |
| `created_at` | Project creation timestamp |
| `verification` | Named repository verification commands |
| `validation_detection` | Optional custom validation-command matchers |
| `hooks` | Repository hook policy |

Commit `.gobby/project.json`. Gobby strips `linear_team_id`,
`linear_project_id`, `parent_project_id`, and `parent_project_path` whenever it
updates the file. Linear bindings live in the local `projects` database row.
Worktree and clone isolation writes those parent fields to a gitignored
`.gobby/isolation.json` sidecar in the isolated checkout; tracked
`.gobby/project.json` stays byte-for-byte as git checked it out.

Task and memory state are database-owned and must stay untracked.
`gobby tasks backup` and `gobby memory backup` write machine-local snapshots to
`~/.gobby/backups/<project-uuid>/tasks.jsonl` and
`~/.gobby/backups/<project-uuid>/memories.jsonl`. The pre-push hook refreshes
these snapshots without staging or committing them. Explicit task restore and
memory restore commands remain available for disaster recovery and migration.

## Build Defaults

Build defaults are loaded from `~/.gobby/build.yaml`, then
`<project>/.gobby/build.yaml`, then CLI/MCP/HTTP request flags:

```yaml
max_active_agents: 10
```

`max_active_agents` is the supported build-file setting and caps concurrent
agents for the project. Per-request CLI, MCP, and HTTP values override the file
setting for that build.

### Packaging Diagnostics

`GOBBY_SKIP_UI_BUILD=1` skips rebuilding web assets during `uv build` and uses
the already staged `src/gobby/ui/web/dist/` files. This is useful for packaging
diagnostics when the UI assets are known current. It is unsafe for release
builds unless those staged assets were freshly built and verified.

## Validation Rules

All config updates are validated with Pydantic before they are accepted. Common
constraints include:

| Type | Constraint |
| --- | --- |
| Port | `1024` through `65535` |
| Positive timeout | Greater than `0` |
| Workflow timeout | Greater than `0` |
| Hook timeout policy | `workflow < adapter < provider` |
| Weight or threshold | `0.0` through `1.0` |
| MCP search mode | `llm`, `semantic`, or `hybrid` |
| Provider auth mode | `subscription`, `api_key`, or `adc` |
| Memory backend | `local` or `null` |
| Build isolation | `none`, `worktree`, or `clone` |

## Troubleshooting

### Daemon Uses The Wrong Port

Check `~/.gobby/bootstrap.yaml` first. The HTTP, WebSocket, and UI ports are
bootstrap values because the daemon needs them before the database is fully
loaded.

### Runtime Setting Does Not Stick

List DB overrides with `gobby-config:list_config_keys`. If a key is absent, the
daemon is using the Pydantic default. If a key is present but behavior did not
change, restart the daemon; the HTTP config save route reports
`requires_restart: true` for config updates.

Hook timeout changes report this restart requirement explicitly. When
`hooks.provider_timeout` changes, rerun `gobby install` after restarting so the
Claude, Codex, Qwen, Droid, and Grok client configurations receive the new
outer deadline.

### Secret Is Masked Or Missing

Masked secret values are intentionally skipped on save. Re-enter the secret
value through the web UI or call `set_config` with `is_secret=true` for a
secret-like key.

### MCP Server Does Not Connect

Check `~/.gobby/mcp-servers.json` for the server entry, transport-specific fields, and
`enabled: true`. For generated or imported servers, refresh the MCP registry
after changing server definitions.

## See Also

- [cli-commands.md](./cli-commands.md) - CLI command reference
- [dispatch.md](./dispatch.md) - Build lifecycle and dispatcher behavior
- [memory.md](./memory.md) - Memory configuration and operations
- [observability.md](./observability.md) - Logs, metrics, traces, and collector deployment
- [rules.md](./rules.md) - Rule engine configuration
- [search.md](./search.md) - Search and embedding behavior
- [webhooks-and-plugins.md](./webhooks-and-plugins.md) - Extension development

_Last verified: 2026-07-20_
