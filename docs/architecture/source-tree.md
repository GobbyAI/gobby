# Gobby Source Tree

> Updated: 2026-06-11

This document is a curated map of the repository. Within `src/gobby/` it lists
every top-level package with its purpose and names only key, stable files —
exhaustive per-file listings drift too quickly to maintain. Use
`gcode outline <path>` or `ls` for the current contents of any package.

## Repository Root

```text
gobby/                                  # Project root
├── pyproject.toml                      # Project configuration, dependencies, build settings
├── README.md                           # Project overview with architecture diagram
├── CLAUDE.md                           # Claude-specific wrapper importing AGENTS.md
├── AGENTS.md                           # Canonical agent instruction file (all CLIs)
├── CONTRIBUTING.md                     # Contribution guidelines
├── GUIDING_PRINCIPLES.md               # Design rationale behind the AGENTS.md working rules
├── ROADMAP.md                          # Project roadmap
├── CHANGELOG.md                        # Release history
├── SECURITY.md                         # Security policy
├── LICENSE                             # Apache 2.0 License
├── src/gobby/                          # Source code (see below)
├── tests/                              # Test suite (mirrors src/gobby/ by directory)
├── web/                                # Web UI (React/TypeScript)
├── docs/                               # Documentation
└── .github/workflows/                  # CI/CD pipelines
```

## src/gobby/ — Top-Level Modules

```text
src/gobby/
├── __init__.py                         # Package init with version export
├── runner.py                           # Daemon process entry point (GobbyRunner)
├── runner_broadcasting.py              # WebSocket event broadcasting wiring
├── runner_maintenance.py               # Background maintenance jobs
├── runner_lifecycle.py                 # Daemon lifecycle orchestration
├── runner_lifecycle_startup.py         # Startup phases
├── runner_lifecycle_shutdown.py        # Shutdown phases
├── runner_lifecycle_subsystems.py      # Subsystem wiring
├── runner_lifecycle_agents.py          # Agent lifecycle wiring
├── runner_lifecycle_periodic.py        # Periodic job wiring
├── app_context.py                      # Application context (shared state)
├── paths.py                            # Path resolution utilities
├── gwiki_gateway.py                    # gwiki CLI gateway
├── shutdown_intent.py                  # Shutdown intent tracking
└── system_automation.py                # System automation loops
```

## src/gobby/ — Packages

```text
src/gobby/
├── adapters/                           # CLI-specific hook adapters: claude_code.py,
│                                       #   agy.py, droid.py, grok.py, qwen.py,
│                                       #   codex_impl/ (package), ACP client modules,
│                                       #   base.py (BaseAdapter), capabilities.py
├── agents/                             # Agent spawning and lifecycle: spawn.py,
│                                       #   runner.py (AgentRunner), runner_queries.py,
│                                       #   lifecycle_monitor.py, kill.py, isolation.py,
│                                       #   session.py, spawners/, tmux/
│                                       #   (definition models: workflows/definitions.py)
├── ai/                                 # AI feature configuration support
├── autonomous/                         # Autonomous execution support (progress
│                                       #   tracking, stop registry, stuck detection)
├── build/                              # gobby build shared service (service.py is the
│                                       #   single core behind CLI, MCP, and HTTP build)
├── cli/                                # CLI commands (Click); entry point gobby.cli:cli
│                                       #   daemon.py, init.py, install.py, build.py,
│                                       #   plan.py/plans.py, tasks/, memory/, postgres*.py,
│                                       #   qdrant.py, secrets.py, worktrees.py, ...
│                                       #   installers/ (per-CLI + service installers)
├── clones/                             # Git clone management
├── code_index/                         # Code index (gcode) integration
├── communications/                     # Comms channels (Telegram etc.): manager.py,
│                                       #   adapters/
├── config/                             # Configuration models: app.py (DaemonConfig),
│                                       #   bootstrap.py (pre-DB settings), build.py,
│                                       #   features.py, sessions.py, tasks.py, ...
├── data/                               # Bundled data files: docker-compose.services.yml,
│                                       #   postgres-pgsearch/
├── dispatch/                           # State-driven task dispatch: rules.py (ordered
│                                       #   lifecycle rules), dispatcher.py (heartbeat)
├── events/                             # Event models and routing
├── github_triage/                      # GitHub issue triage service
├── hooks/                              # Hook event system: hook_manager.py (central
│                                       #   coordinator), events.py (HookEvent/Response),
│                                       #   hook_types/ (package), skill_manager.py,
│                                       #   webhooks.py, normalization.py
├── install/                            # Bundled assets and installers
│   ├── agy/ claude/ codex/ droid/ grok/ qwen/      # Per-CLI install assets
│   └── shared/                         # Bundled content synced to DB on startup
│       ├── config/ detection/ hooks/ prompts/ rules/ services/
│       ├── registry/                   # build_profiles.yaml, stages.yaml
│       ├── rules/build/                # Build rule group
│       ├── skills/                     # Bundled skills (SKILL.md dirs)
│       └── workflows/                  # review.yaml plus grouped workflow assets
│           ├── agents/                 # Bundled agent definitions (YAML)
│           ├── pipelines/              # Bundled pipelines (YAML)
│           ├── rules/                  # Bundled rule groups (YAML)
│           └── variables/              # Default variable definitions
├── integrations/                       # External integrations (github.py, linear.py)
├── llm/                                # LLM abstraction: service.py (LLMService),
│                                       #   claude.py, claude_cli.py, local.py (local
│                                       #   endpoints), model_registry.py, resolver.py,
│                                       #   factory.py, stream_json_parser.py
├── mcp_proxy/                          # MCP proxy layer: server.py (FastMCP),
│                                       #   manager.py (MCPClientManager), instructions.py
│   ├── tools/                          # Internal tool modules: tasks/, sessions/,
│   │                                   #   skills/, workflows/, worktrees/, plans/,
│   │                                   #   spawn_agent/, build.py, memory.py, merge*.py,
│   │                                   #   internal.py, wiki.py, communications.py, ...
│   └── transports/                     # HTTP, stdio, WebSocket transports
├── memory/                             # Persistent memory system: manager.py
│                                       #   (MemoryManager), vectorstore.py (Qdrant),
│                                       #   falkor_client.py, recall.py, scoring.py,
│                                       #   backends/, components/, dream/, services/
│                                       #   (services/knowledge_graph/ incl. extraction)
├── plans/                              # Plan registry and coverage tooling
├── project_verification/               # Project verification checks
├── prompts/                            # Prompt management (loader, models, sync)
├── providers/                          # Provider abstractions
├── review_learning/                    # Review learning system
├── runner_init/                        # Runner initialization helpers
├── savings/                            # Token savings tracking
├── scheduler/                          # Cron job scheduler (scheduler.py, executor.py)
├── search/                             # Search: keyword.py, embeddings.py, unified.py,
│                                       #   backends/
├── servers/                            # HTTP and WebSocket servers
│   ├── http.py                         # FastAPI HTTP server
│   ├── routes/                         # HTTP API routes: tasks.py, sessions/, agents.py,
│   │                                   #   build.py, chat.py, workflows.py, memory.py,
│   │                                   #   skills.py, projects.py, source_control.py,
│   │                                   #   github_triage.py, wiki.py, admin/, mcp/, ...
│   └── websocket/                      # WebSocket server: server.py, broadcast.py,
│                                       #   handlers/, chat/, voice/, tmux.py, auth.py
├── sessions/                           # Session lifecycle: lifecycle.py, processor.py
│                                       #   (SessionMessageProcessor), mailbox.py,
│                                       #   summarize.py, token_tracker.py
│   └── transcripts/                    # Parsers: claude.py, codex.py, droid.py,
│                                       #   grok.py, qwen.py
├── skills/                             # Skill management: loader.py (SkillLoader),
│                                       #   parser.py, sync.py, search.py, formatting.py,
│                                       #   hubs/
├── storage/                            # PostgreSQL hub storage; schema assets live in
│                                       #   crates/gcore/assets/schema/
│   ├── hub/                            # postgres.py (PostgresHubDatabase),
│   │                                   #   protocol.py (HubDatabase protocol), runtime.py
│   ├── sessions/ tasks/ agents/        # CRUD packages (_manager.py, _crud.py, ...)
│   └── *.py                            # plans.py, build_history.py, build_profiles.py,
│                                       #   memories.py, secrets.py, config_store.py,
│                                       #   token_events.py, spans.py, cron.py, ...
├── sync/                               # Task/memory JSONL backup/restore and issue import
├── tasks/                              # Task system: expansion/ + expansion_service.py
│                                       #   (ExpansionService), validation.py
│                                       #   (TaskValidator), state_semantics.py, prompts/
├── telemetry/                          # Logging/telemetry configuration
├── test_quality/                       # Test quality analysis
├── utils/                              # Utilities: git.py, daemon_client.py, sql.py,
│                                       #   session_refs.py, tool_summarizer.py, id.py
├── voice/                              # Voice chat support (stt.py)
├── wiki/                               # gwiki integration
├── workflows/                          # Rule engine and workflow system
│   ├── engine/                         # RuleEngine (engine/core.py)
│   ├── enforcement/ pipeline/          # Enforcement and pipeline subpackages
│   ├── definitions.py                  # Rule/workflow/agent definition models
│   ├── safe_evaluator.py               # Safe expression evaluator (AST-based)
│   ├── pipeline_executor.py            # PipelineExecutor
│   ├── loader*.py, sync_*.py           # YAML loading and DB sync
│   └── observer_*.py, templates.py     # Observers and templates
└── worktrees/                          # Git worktree management: git/ (manager and
                                        #   lifecycle), merge/ (merge operations)
```

## Tests

```text
tests/                                  # Mirrors src/gobby/ by directory
├── conftest.py                         # Pytest fixtures (incl. postgres_db)
├── fixtures/                           # Shared fixtures (postgres.py, ...)
├── storage/  mcp_proxy/  workflows/    # Per-subsystem test packages
├── hooks/  agents/  sessions/
├── adapters/  cli/
└── ...                                 # memory, skills, search, dispatch, etc.
```

## Docs

```text
docs/
├── architecture/                       # Architecture docs (this directory)
├── guides/                             # User and developer guides
├── contracts/                          # Contracts (e.g., plan-coverage.md)
├── plans/                              # Implementation plans
├── research/                           # Research documents
├── reviews/                            # Review findings
├── runbooks/                           # Operational runbooks
├── examples/                           # Example configurations
└── archive/                            # Archived documents
```

## Code Statistics

Counts as of 2026-06-11; expect drift. Regenerate with the commands shown.

| Metric | Value | Regenerate with |
|--------|-------|-----------------|
| **Source Python Files** | ~1,170 | `find src/gobby -name '*.py' -not -path '*__pycache__*' \| wc -l` |
| **Test Python Files** | ~1,340 | `find tests -name '*.py' -not -path '*__pycache__*' \| wc -l` |
| **Top-Level Packages** | 39 | `ls src/gobby/` |
| **Bundled Rule Groups** | 22 | `ls src/gobby/install/shared/workflows/rules/ src/gobby/install/shared/rules/build/` |
| **Bundled Skills** | ~59 | `ls -d src/gobby/install/shared/skills/*/ \| wc -l` |
| **Bundled Agent Definitions** | 23 | `ls src/gobby/install/shared/workflows/agents/*.yaml \| wc -l` |
| **Bundled Workflow Definitions** | 3 (+7 pipelines) | `ls src/gobby/install/shared/workflows/*.yaml` |
| **LLM Providers** | Claude (API + CLI), local endpoints | `ls src/gobby/llm/` |
| **CLI Adapters** | 6 (Claude Code, AGY, Codex, Droid, Grok, Qwen) | `ls src/gobby/adapters/` |
| **Guides** | 47 | `ls docs/guides/*.md \| wc -l` |
| **Test Coverage Target** | 80% | enforced in CI and pre-push |

## Module Dependencies

```text
cli/
├── config/
├── runner.py
│   ├── servers/http.py
│   │   ├── servers/routes/*
│   │   ├── adapters/*
│   │   └── hooks/hook_manager.py
│   │       └── workflows/engine/ (RuleEngine)
│   ├── servers/websocket/
│   ├── mcp_proxy/server.py
│   │   ├── mcp_proxy/manager.py
│   │   └── mcp_proxy/tools/*
│   ├── dispatch/dispatcher.py
│   │   └── dispatch/rules.py
│   ├── agents/runner.py
│   └── sessions/ (lifecycle, processor)
├── build/service.py
├── storage/hub/postgres.py             # Schema SQL: crates/gcore/assets/schema/
├── llm/service.py
│   └── llm/{claude,claude_cli,local}.py
└── utils/*
```

_Last verified: 2026-06-11_
