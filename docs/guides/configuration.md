# Configuration Guide

Gobby uses YAML configuration for the daemon and JSON for project-specific settings. All options are validated via Pydantic with sensible defaults.

## Quick Start

```bash
# View current configuration
gobby config show

# Set a value
gobby config set daemon_port 60888

# Reset to defaults
gobby config reset
```

## Configuration Files

| File | Scope | Format | Purpose |
|------|-------|--------|---------|
| `~/.gobby/config.yaml` | User | YAML | Daemon configuration |
| `~/.gobby/.mcp.json` | User | JSON | MCP server registry |
| `.gobby/project.json` | Project | JSON | Project metadata and verification |

## Environment Variables

Config supports environment variable expansion:

```yaml
# Use VAR, unchanged if unset
api_key: ${OPENAI_API_KEY}

# Use VAR, or "default" if unset/empty
api_key: ${OPENAI_API_KEY:-sk-default}
```

---

## ~/.gobby/config.yaml Reference

### Daemon Settings

Core daemon configuration.

```yaml
# Port for daemon HTTP server (1024-65535)
daemon_port: 60887

# Health check interval in seconds (1.0-300.0)
daemon_health_check_interval: 10.0

# Enable test endpoints
test_mode: false

# SQLite database for cross-project queries
database_path: ~/.gobby/gobby-hub.db

# Use V2 baseline schema (v75) for new databases
use_flattened_baseline: true
```

### WebSocket Server

Real-time event streaming.

```yaml
websocket:
  enabled: true
  port: 60888                    # 1024-65535
  ping_interval: 30              # seconds
  ping_timeout: 10               # seconds
```

### Logging

```yaml
logging:
  level: info                    # debug, info, warning, error
  format: text                   # text, json

  # Log file paths
  client: ~/.gobby/logs/gobby.log
  client_error: ~/.gobby/logs/gobby-error.log
  hook_manager: ~/.gobby/logs/hook-manager.log
  mcp_server: ~/.gobby/logs/mcp-server.log
  mcp_client: ~/.gobby/logs/mcp-client.log

  # Rotation
  max_size_mb: 10
  backup_count: 5
```

### MCP Client Proxy

Tool discovery and recommendation.

```yaml
mcp_client_proxy:
  enabled: true
  connect_timeout: 30.0          # seconds
  proxy_timeout: 30              # seconds
  tool_timeout: 30               # seconds

  # Per-tool timeouts
  tool_timeouts:
    expand_task: 300.0
    validate_task: 120.0

  # Semantic search for tool discovery
  search_mode: llm               # llm, semantic, hybrid
  embedding_provider: openai
  embedding_model: text-embedding-3-small
  min_similarity: 0.3            # 0.0-1.0
  top_k: 10

  # Refresh settings
  refresh_on_server_add: true
  refresh_timeout: 300.0
```

### LLM Providers

Multi-provider LLM configuration.

```yaml
llm_providers:
  json_strict: true              # Strict JSON validation

  claude:
    models: claude-haiku-4-5,claude-sonnet-4-5,claude-opus-4-5
    auth_mode: subscription      # subscription, api_key, adc

  codex:                         # OpenAI
    models: gpt-4o-mini,gpt-5-mini,gpt-5
    auth_mode: subscription

  gemini:
    models: gemini-2.0-flash,gemini-2.5-pro
    auth_mode: subscription

  litellm:
    models: mistral-large,command-r-plus
    auth_mode: api_key

  # API keys (can use ${ENV_VAR} syntax)
  api_keys:
    OPENAI_API_KEY: ${OPENAI_API_KEY}
    MISTRAL_API_KEY: ${MISTRAL_API_KEY}
```

### Search

Unified search configuration. See [search.md](search.md) for details.

```yaml
search:
  mode: auto                     # tfidf, embedding, auto, hybrid
  embedding_model: text-embedding-3-small
  embedding_api_base: null       # For Ollama: http://localhost:11434/v1
  embedding_api_key: null        # Uses env if not set
  tfidf_weight: 0.4              # 0.0-1.0
  embedding_weight: 0.6          # 0.0-1.0
  notify_on_fallback: true
```

### Memory

Persistent memory system. See [memory.md](memory.md) for details.

```yaml
memory:
  enabled: true
  backend: local                  # local (preferred), sqlite (alias for local), or null (testing)

  # Search
  search_backend: auto            # tfidf, text, embedding, auto, hybrid
  embedding_model: text-embedding-3-small
  embedding_weight: 0.6           # 0.0-1.0 (hybrid mode)
  tfidf_weight: 0.4               # 0.0-1.0 (hybrid mode)

  # Importance & Decay
  importance_threshold: 0.7       # 0.0-1.0
  decay_enabled: true
  decay_rate: 0.05                # 0.0-1.0
  decay_floor: 0.1                # 0.0-1.0

  # Cross-referencing
  auto_crossref: false
  crossref_threshold: 0.3         # 0.0-1.0
  crossref_max_links: 5

  access_debounce_seconds: 60

```

### Memory Sync

Export memories to filesystem for git tracking.

```yaml
memory_sync:
  enabled: true
  export_debounce: 5.0           # seconds
  export_path: .gobby/memories.jsonl
```

### Session Configuration

#### Context Injection

Context for subagent spawning.

```yaml
context_injection:
  enabled: true
  default_source: summary_markdown
    # Options: summary_markdown, compact_markdown, session_id:<id>,
    # transcript:<n>, file:<path>
  max_file_size: 51200           # bytes
  max_content_size: 51200        # bytes
  max_transcript_messages: 100
  truncation_suffix: "\n\n[truncated: {bytes} bytes remaining]"
  context_template: null         # Custom template with {{ context }}, {{ prompt }}
```

#### Session Summary

Auto-generated session summaries.

```yaml
session_summary:
  enabled: true
  provider: claude
  model: claude-haiku-4-5
  prompt: |                      # Jinja2 template
    Summarize this session...
  summary_file_path: ~/.gobby/session_summaries
```

#### Title Synthesis

Auto-generated session titles.

```yaml
title_synthesis:
  enabled: true
  provider: claude
  model: claude-haiku-4-5
  prompt: null                   # Custom template
```

#### Message Tracking

Session message processing.

```yaml
message_tracking:
  enabled: true
  poll_interval: 5.0             # seconds
  debounce_delay: 1.0            # seconds
  max_message_length: 10000
  broadcast_enabled: true
```

#### Session Lifecycle

```yaml
session_lifecycle:
  active_session_pause_minutes: 30
  stale_session_timeout_hours: 24
  expire_check_interval_minutes: 60
  transcript_processing_interval_minutes: 5
  transcript_processing_batch_size: 10
```

#### Compact Handoff

```yaml
compact_handoff:
  enabled: true
```

### Task Configuration

Task expansion, validation, and enrichment. See [tasks.md](tasks.md) for details.

```yaml
gobby-tasks:
  enabled: true
  show_result_on_create: false

  # File extraction for task validation
  file_extraction:
    file_extensions:
      - .py
      - .js
      - .ts
      - .go
      - .rs
      - .md
    known_files:
      - Makefile
      - Dockerfile
      - package.json
    path_prefixes:
      - src/
      - lib/
      - test/
      - tests/

  # Task enrichment
  enrichment:
    enabled: true
    provider: claude
    model: claude-3-5-haiku-latest
    enable_code_research: true
    enable_web_research: false
    enable_mcp_tools: false
    generate_validation: true

  # Task expansion (breaking down broad tasks)
  expansion:
    enabled: true
    provider: claude
    model: claude-opus-4-5
    prompt_path: null            # Custom prompt file
    system_prompt_path: null
    codebase_research_enabled: true
    research_model: null
    research_max_steps: 10
    research_system_prompt: "You are a senior developer..."
    web_research_enabled: true
    max_subtasks: 15
    default_strategy: auto       # auto, phased, sequential, parallel
    timeout: 300.0               # seconds
    research_timeout: 60.0

    pattern_criteria:
      patterns:
        strangler-fig: [...]
        tdd: [...]
      detection_keywords:
        strangler-fig: [migrate, legacy, ...]

  # Task validation
  validation:
    enabled: true
    provider: claude
    model: claude-opus-4-5
    system_prompt: "You are a QA validator..."
    criteria_system_prompt: "You are a QA engineer..."
    prompt_path: null
    criteria_prompt_path: null
    external_system_prompt_path: null
    external_spawn_prompt_path: null
    external_agent_prompt_path: null
    external_llm_prompt_path: null

    # Validation loop control
    max_iterations: 10
    max_consecutive_errors: 3
    recurring_issue_threshold: 3
    issue_similarity_threshold: 0.8

    # Build verification
    run_build_first: true
    build_command: null          # Auto-detected if null

    # External validator
    use_external_validator: false
    external_validator_model: null
    external_validator_mode: llm  # llm, agent, spawn

    # Escalation
    escalation_enabled: true
    escalation_notify: none      # webhook, slack, none
    escalation_webhook_url: null

    # Auto-generation
    auto_generate_on_create: true
    auto_generate_on_expand: true
```

### Workflow Engine

Step-based workflow enforcement. See [rules.md](rules.md) for details.

```yaml
workflow:
  enabled: true
  timeout: 0.0                   # seconds, 0 = no timeout
  require_task_before_edit: false
  protected_tools:
    - Edit
    - Write
    - Update
    - NotebookEdit
```

### Tool Recommendation

```yaml
recommend_tools:
  enabled: true
  provider: claude
  model: claude-sonnet-4-5
  prompt: null                   # Custom template
  hybrid_rerank_prompt_path: null
  llm_prompt_path: null
```

### Tool Summarizer

```yaml
tool_summarizer:
  enabled: true
  provider: claude
  model: claude-haiku-4-5
  prompt_path: null
  system_prompt_path: null
  server_description_prompt_path: null
  server_description_system_prompt_path: null
```

### Task Description Generation

```yaml
task_description:
  enabled: true
  provider: claude
  model: claude-haiku-4-5-20251001
  min_structured_length: 50
  prompt_path: null
  system_prompt_path: null
```

### MCP Server Import

```yaml
import_mcp_server:
  enabled: true
  provider: claude
  model: claude-haiku-4-5
  prompt_path: null
  github_fetch_prompt_path: null
  search_fetch_prompt_path: null
```

### Metrics

```yaml
metrics:
  list_limit: 10000              # Max items for counting, 0 = unbounded
```

### Verification Defaults

Default commands for project verification.

```yaml
verification_defaults:
  unit_tests: uv run pytest tests/ -v
  type_check: uv run mypy src/
  lint: uv run ruff check src/
  format: uv run ruff format --check src/
  integration: null
  security: null
  code_review: null
  custom: {}
```

### Skills

```yaml
skills:
  inject_core_skills: true
  core_skills_path: null
  injection_format: summary      # summary, full, none
```

### Conductor (Token Budget)

```yaml
conductor:
  daily_budget_usd: 50.0
  warning_threshold: 0.8         # 0.0-1.0
  throttle_threshold: 0.9        # 0.0-1.0
  tracking_window_days: 7
```

### Hook Extensions

#### WebSocket Broadcasting

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
```

#### HTTP Webhooks

```yaml
hook_extensions:
  webhooks:
    enabled: true
    default_timeout: 10.0        # 1.0-60.0
    async_dispatch: true

    endpoints:
      - name: slack-notify
        url: https://hooks.slack.com/services/...
        events:
          - session-end
          - task-completed
        headers:
          Content-Type: application/json
        timeout: 10.0            # 1.0-60.0
        retry_count: 3           # 0-10
        retry_delay: 1.0         # 0.1-30.0
        can_block: false
        enabled: true
```

#### Python Plugins

```yaml
hook_extensions:
  plugins:
    enabled: false               # Disabled by default for security
    plugin_dirs:
      - .gobby/plugins
    auto_discover: true

    plugins:
      my-plugin:
        enabled: true
        config:
          key: value
```

---

## ~/.gobby/.mcp.json Reference

MCP server registry.

```json
{
  "servers": [
    {
      "name": "filesystem",
      "enabled": true,
      "transport": "stdio",
      "command": "npx",
      "args": ["@anthropic-ai/filesystem-mcp"],
      "env": null,
      "description": "File system operations",
      "project_id": "global"
    },
    {
      "name": "api-server",
      "enabled": true,
      "transport": "http",
      "url": "https://api.example.com/mcp",
      "headers": {
        "Authorization": "Bearer ${API_TOKEN}"
      },
      "requires_oauth": false,
      "project_id": "global"
    }
  ]
}
```

### Transport Types

| Transport | Fields | Description |
|-----------|--------|-------------|
| `stdio` | `command`, `args`, `env` | Local process with stdin/stdout |
| `http` | `url`, `headers` | HTTP endpoint |
| `websocket` | `url`, `headers` | WebSocket connection |
| `sse` | `url`, `headers` | Server-Sent Events |

---

## .gobby/project.json Reference

Project-specific configuration.

```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "name": "my-project",
  "created_at": "2024-01-15T10:30:00Z",

  "verification": {
    "unit_tests": "npm test",
    "type_check": "npm run typecheck",
    "lint": "npm run lint",
    "format": "npm run format:check",
    "integration": "npm run test:integration",
    "security": "npm audit",
    "code_review": null,
    "custom": {
      "e2e": "npm run test:e2e"
    }
  },

  "hooks": {
    "pre-commit": {
      "run": ["lint", "format"],
      "fail_fast": true,
      "timeout": 60,
      "enabled": true
    },
    "pre-push": {
      "run": ["type_check", "unit_tests"],
      "fail_fast": false,
      "timeout": 300,
      "enabled": true
    },
    "pre-merge": {
      "run": ["integration"],
      "fail_fast": true,
      "timeout": 300,
      "enabled": true
    }
  }
}
```

---

## Feature Flags

Quick reference for enabling/disabling features:

| Feature | Config Key | Default |
|---------|-----------|---------|
| WebSocket Server | `websocket.enabled` | true |
| MCP Proxy | `mcp_client_proxy.enabled` | true |
| Memory System | `memory.enabled` | true |
| Session Summary | `session_summary.enabled` | true |
| Title Synthesis | `title_synthesis.enabled` | true |
| Message Tracking | `message_tracking.enabled` | true |
| Workflows | `workflow.enabled` | true |
| Task System | `gobby-tasks.enabled` | true |
| Task Enrichment | `gobby-tasks.enrichment.enabled` | true |
| Task Expansion | `gobby-tasks.expansion.enabled` | true |
| Task Validation | `gobby-tasks.validation.enabled` | true |
| Tool Recommendations | `recommend_tools.enabled` | true |
| Tool Summarizer | `tool_summarizer.enabled` | true |
| Task Descriptions | `task_description.enabled` | true |
| MCP Import | `import_mcp_server.enabled` | true |
| HTTP Webhooks | `hook_extensions.webhooks.enabled` | true |
| WebSocket Broadcasting | `hook_extensions.websocket.enabled` | true |
| Python Plugins | `hook_extensions.plugins.enabled` | **false** |
| Skills Injection | `skills.inject_core_skills` | true |
| Compact Handoff | `compact_handoff.enabled` | true |

---

## CLI Commands

### View Configuration

```bash
# Show full config
gobby config show

# Show specific section
gobby config show --section memory

# Show as YAML
gobby config show --format yaml
```

### Modify Configuration

```bash
# Set a value
gobby config set daemon_port 60888
gobby config set logging.level debug

# Reset to defaults
gobby config reset

# Reset specific section
gobby config reset --section memory
```

### Validate Configuration

```bash
# Check config is valid
gobby config validate

# Check and show warnings
gobby config validate --strict
```

---

## Type Validation Rules

All values are validated via Pydantic:

| Type | Constraint |
|------|------------|
| Port | 1024-65535 |
| Timeout | Positive float/int |
| Weight | 0.0-1.0 |
| Threshold | 0.0-1.0 |
| Count | Positive integer |
| Search mode | tfidf, embedding, auto, hybrid |
| Log level | debug, info, warning, error |
| Memory backend | local, sqlite (alias for local), null |
| Auth mode | subscription, api_key, adc |

---

## Best Practices

### Do

- Use environment variables for secrets: `${API_KEY}`
- Set appropriate timeouts for slow operations
- Enable `notify_on_fallback` to monitor search degradation
- Configure verification commands for your project
- Review feature flags to disable unused features

### Don't

- Commit API keys in config files
- Set extremely high limits (memory usage)
- Disable validation in production
- Use `test_mode: true` in production

## Troubleshooting

### Config not loading

1. Check YAML syntax: `python -c "import yaml; yaml.safe_load(open('~/.gobby/config.yaml'))"`
2. Verify file permissions
3. Check for invalid values with `gobby config validate`

### Feature not working

1. Verify feature is enabled in config
2. Check daemon logs: `tail -f ~/.gobby/logs/gobby.log`
3. Restart daemon: `gobby restart`

### MCP server not connecting

1. Check server is listed in `.mcp.json`
2. Verify `enabled: true`
3. Check connection settings match transport type
4. Review MCP client logs: `~/.gobby/logs/mcp-client.log`

## See Also

- [search.md](search.md) - Search configuration details
- [memory.md](memory.md) - Memory system details
- [rules.md](rules.md) - Rule engine configuration
- [webhooks-and-plugins.md](webhooks-and-plugins.md) - Extension development
