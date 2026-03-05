# Gobby Source Tree

> Updated: 2026-03-03

## Directory Structure

```
gobby/                                  # Project root
├── pyproject.toml                      # Project configuration, dependencies, build settings
├── README.md                           # Project overview with architecture diagram
├── CLAUDE.md                           # Claude Code development instructions
├── GEMINI.md                           # Gemini CLI instructions
├── AGENTS.md                           # Agent definitions reference
├── AUTH.md                             # Authentication & AI vendor policies
├── CONTRIBUTING.md                     # Contribution guidelines
├── GUIDING_PRINCIPLES.md              # Development philosophy
├── ROADMAP.md                         # Project roadmap
├── CHANGELOG.md                       # Release history
├── SECURITY.md                        # Security policy
├── LICENSE.md                         # Apache 2.0 License
│
├── src/gobby/                          # Source code (~505 Python files)
│   ├── __init__.py                     # Package init with version export
│   ├── runner.py                       # Daemon process entry point
│   ├── runner_broadcasting.py          # WebSocket event broadcasting wiring
│   ├── runner_maintenance.py           # Background maintenance jobs
│   ├── app_context.py                  # Application context (shared state)
│   ├── watchdog.py                     # Process watchdog
│   ├── paths.py                        # Path resolution utilities
│   │
│   ├── cli/                            # CLI commands (Click, ~29 modules)
│   │   ├── __init__.py                 # Main CLI group
│   │   ├── __main__.py                 # Entry point
│   │   ├── daemon.py                   # start, stop, restart, status
│   │   ├── agents.py                   # Agent management
│   │   ├── auth.py                     # Authentication commands
│   │   ├── clones.py                   # Clone management
│   │   ├── cron.py                     # Cron job management
│   │   ├── export_import.py            # Export/import operations
│   │   ├── extensions.py               # Extension management
│   │   ├── github.py                   # GitHub integration
│   │   ├── init.py                     # Project initialization
│   │   ├── install.py                  # Hook installation
│   │   ├── install_setup.py            # Installation helpers
│   │   ├── linear.py                   # Linear integration
│   │   ├── mcp.py                      # MCP commands
│   │   ├── mcp_proxy.py               # MCP proxy commands
│   │   ├── memory.py                   # Memory management
│   │   ├── merge.py                    # Merge commands
│   │   ├── pipelines.py               # Pipeline management
│   │   ├── projects.py                 # Project management
│   │   ├── rules.py                    # Rule management
│   │   ├── services.py                 # Service management
│   │   ├── sessions.py                 # Session management
│   │   ├── setup.py                    # Setup commands
│   │   ├── skills.py                   # Skill management
│   │   ├── sync.py                     # Sync commands
│   │   ├── ui.py                       # Web UI commands
│   │   ├── utils.py                    # CLI utilities
│   │   ├── worktrees.py               # Worktree management
│   │   └── installers/                 # Per-CLI hook installers (~14 modules)
│   │       ├── shared.py               # Shared installer logic
│   │       ├── claude.py               # Claude Code installer
│   │       ├── gemini.py               # Gemini CLI installer
│   │       ├── codex.py                # Codex CLI installer
│   │       ├── cursor.py               # Cursor installer
│   │       ├── windsurf.py             # Windsurf installer
│   │       ├── copilot.py              # Copilot installer
│   │       ├── antigravity.py          # Antigravity installer
│   │       ├── git_hooks.py            # Git hook installer
│   │       ├── ide_config.py           # IDE configuration
│   │       ├── mcp_config.py           # MCP configuration
│   │       ├── neo4j.py                # Neo4j installer
│   │       └── skill_install.py        # Skill installer
│   │
│   ├── adapters/                       # CLI-specific hook adapters (~7 modules)
│   │   ├── base.py                     # BaseAdapter ABC
│   │   ├── claude_code.py              # Claude Code adapter
│   │   ├── gemini.py                   # Gemini CLI adapter
│   │   ├── cursor.py                   # Cursor adapter
│   │   ├── windsurf.py                 # Windsurf adapter
│   │   ├── copilot.py                  # Copilot adapter
│   │   └── codex.py                    # Codex CLI adapter (in claude_code.py)
│   │
│   ├── agents/                         # Agent spawning and lifecycle (~20 modules)
│   │   ├── spawn.py                    # Agent spawner
│   │   ├── spawn_executor.py           # Spawn execution
│   │   ├── runner.py                   # AgentRunner process management
│   │   ├── runner_models.py            # Runner data models
│   │   ├── runner_tracking.py          # Runner state tracking
│   │   ├── runner_queries.py           # Runner query helpers
│   │   ├── definitions.py              # Agent definition models
│   │   ├── registry.py                 # Agent registry (DB-backed)
│   │   ├── isolation.py                # Worktree/clone isolation
│   │   ├── session.py                  # Agent session management
│   │   ├── context.py                  # Agent context injection
│   │   ├── lifecycle_monitor.py        # Agent lifecycle monitoring
│   │   ├── constants.py                # Agent constants
│   │   ├── sync.py                     # Agent sync
│   │   ├── codex_session.py            # Codex session handling
│   │   ├── gemini_session.py           # Gemini session handling
│   │   ├── dry_run.py                  # Dry run support
│   │   ├── sandbox.py                  # Sandbox execution
│   │   └── pty_reader.py               # PTY output reading
│   │
│   ├── hooks/                          # Hook event system (~16 modules)
│   │   ├── hook_manager.py             # Central HookManager coordinator
│   │   ├── events.py                   # HookEvent, HookResponse models
│   │   ├── hook_types.py               # Hook type enums
│   │   ├── skill_manager.py            # Skill discovery for hooks
│   │   ├── broadcaster.py              # Event broadcasting
│   │   ├── factory.py                  # Hook factory
│   │   ├── git.py                      # Git hook handling
│   │   ├── health_monitor.py           # Health monitoring
│   │   ├── mcp_dispatch.py             # MCP dispatch from hooks
│   │   ├── normalization.py            # Event normalization
│   │   ├── event_enrichment.py         # Event enrichment
│   │   ├── session_coordinator.py      # Session coordination
│   │   ├── session_lookup.py           # Session lookup
│   │   ├── verification_runner.py      # Hook verification
│   │   └── webhooks.py                 # Webhook dispatch
│   │
│   ├── servers/                        # HTTP and WebSocket servers (~54 modules)
│   │   ├── http.py                     # FastAPI HTTP server
│   │   ├── routes/                     # HTTP API routes (~18 modules)
│   │   │   ├── tasks.py               # Task API
│   │   │   ├── sessions.py            # Session API
│   │   │   ├── agents.py              # Agent API
│   │   │   ├── agent_spawn.py         # Agent spawn API
│   │   │   ├── rules.py               # Rule API
│   │   │   ├── workflows.py           # Workflow API
│   │   │   ├── memory.py              # Memory API
│   │   │   ├── pipelines.py           # Pipeline API
│   │   │   ├── skills.py              # Skills API
│   │   │   ├── configuration.py       # Configuration API
│   │   │   ├── cron.py                # Cron API
│   │   │   ├── projects.py            # Projects API
│   │   │   ├── files.py               # File browser API
│   │   │   ├── source_control.py      # Source control API
│   │   │   ├── dependencies.py        # Dependency API
│   │   │   ├── voice.py               # Voice API
│   │   │   └── auth.py                # Auth API
│   │   └── websocket/                  # WebSocket server (~9 modules)
│   │       ├── server.py               # WebSocket server
│   │       ├── broadcast.py            # BroadcastMixin
│   │       ├── handlers.py             # Message handlers
│   │       ├── models.py               # WebSocket models
│   │       ├── session_control.py      # Session control
│   │       ├── tmux.py                 # Tmux terminal WebSocket
│   │       ├── voice.py                # Voice WebSocket
│   │       └── auth.py                 # WebSocket auth
│   │
│   ├── mcp_proxy/                      # MCP proxy layer (~79 modules)
│   │   ├── server.py                   # FastMCP server implementation
│   │   ├── manager.py                  # MCPClientManager (connection pooling)
│   │   ├── instructions.py             # MCP server instructions (progressive discovery)
│   │   ├── registries.py               # Tool registries
│   │   ├── lazy.py                     # Lazy loading
│   │   ├── models.py                   # MCP models
│   │   ├── metrics.py                  # MCP metrics
│   │   ├── importer.py                 # Server importer
│   │   ├── daemon_control.py           # Daemon control tools
│   │   ├── actions.py                  # MCP actions
│   │   ├── schema_hash.py             # Schema hashing
│   │   ├── semantic_search.py          # Semantic search for tools
│   │   ├── stdio.py                    # Stdio transport helper
│   │   ├── tools/                      # Internal tool modules
│   │   │   ├── agents.py              # Agent tools
│   │   │   ├── agent_definitions.py   # Agent definition CRUD
│   │   │   ├── agent_messaging.py     # Agent messaging tools
│   │   │   ├── memory.py              # Memory tools
│   │   │   ├── clones.py              # Clone tools
│   │   │   ├── worktrees.py           # Worktree tools
│   │   │   ├── config.py              # Config tools
│   │   │   ├── cron.py                # Cron tools
│   │   │   ├── hub.py                 # Hub tools
│   │   │   ├── merge.py               # Merge tools
│   │   │   ├── metrics.py             # Metrics tools
│   │   │   ├── canvas.py              # Canvas tools
│   │   │   ├── voice.py               # Voice tools
│   │   │   ├── internal.py            # Internal utility tools
│   │   │   ├── task_dependencies.py   # Task dependency tools
│   │   │   ├── task_readiness.py      # Task readiness/suggestion tools
│   │   │   ├── task_sync.py           # Task sync tools
│   │   │   ├── task_validation.py     # Task validation tools
│   │   │   ├── tasks/                 # Task tool sub-modules
│   │   │   ├── sessions/              # Session tool sub-modules
│   │   │   ├── skills/                # Skill tool sub-modules
│   │   │   ├── workflows/             # Workflow tool sub-modules
│   │   │   ├── pipelines/             # Pipeline tool sub-modules
│   │   │   ├── orchestration/         # Orchestration tool sub-modules
│   │   │   ├── spawn_agent/           # Agent spawn tool sub-modules
│   │   │   └── plugins/               # Plugin tool sub-modules
│   │   └── transports/                 # MCP transports (~6 modules)
│   │       ├── base.py                # Base transport
│   │       ├── factory.py             # Transport factory
│   │       ├── http.py                # HTTP transport
│   │       ├── stdio.py               # Stdio transport
│   │       └── websocket.py           # WebSocket transport
│   │
│   ├── workflows/                      # Rule engine and workflow system (~30 modules)
│   │   ├── rule_engine.py              # RuleEngine (declarative enforcement)
│   │   ├── definitions.py              # Rule/workflow/agent definition models
│   │   ├── safe_evaluator.py           # Safe expression evaluator (AST-based)
│   │   ├── pipeline_executor.py        # PipelineExecutor
│   │   ├── pipeline_state.py           # Pipeline execution state
│   │   ├── pipeline_webhooks.py        # Pipeline webhook support
│   │   ├── agent_resolver.py           # Agent definition resolution
│   │   ├── loader.py                   # YAML workflow/rule loading
│   │   ├── loader_cache.py             # Loader caching
│   │   ├── loader_discovery.py         # Loader discovery
│   │   ├── loader_sync.py              # Loader sync
│   │   ├── loader_validation.py        # Loader validation
│   │   ├── selectors.py                # Rule selectors
│   │   ├── state_manager.py            # Workflow state management
│   │   ├── observers.py                # Observer engine
│   │   ├── templates.py                # Workflow templates
│   │   ├── workflow_templates.py       # Template management
│   │   ├── sync.py                     # Workflow sync
│   │   ├── hooks.py                    # Workflow hook integration
│   │   ├── task_actions.py             # Task-related actions
│   │   ├── task_claim_state.py         # Task claim state tracking
│   │   ├── summary_actions.py          # Summary actions
│   │   ├── condition_helpers.py        # Condition evaluation helpers
│   │   ├── constants.py                # Workflow constants
│   │   ├── dry_run.py                  # Dry run support
│   │   ├── git_utils.py                # Git utilities for workflows
│   │   ├── lobster_compat.py           # Lobster format compatibility
│   │   ├── webhook.py                  # Webhook support
│   │   └── webhook_executor.py         # Webhook execution
│   │
│   ├── sessions/                       # Session management (~15 modules)
│   │   ├── manager.py                  # SessionManager
│   │   ├── lifecycle.py                # Background jobs
│   │   ├── processor.py                # SessionMessageProcessor
│   │   ├── analyzer.py                 # Session analysis
│   │   ├── formatting.py               # Session formatting
│   │   ├── summarize.py                # Session summarization
│   │   ├── token_tracker.py            # Token tracking
│   │   └── transcripts/                # Transcript parsers (~7 modules)
│   │       ├── base.py                 # Base parser
│   │       ├── claude.py               # Claude transcript parser
│   │       ├── gemini.py               # Gemini transcript parser
│   │       ├── codex.py                # Codex transcript parser
│   │       ├── cursor.py               # Cursor transcript parser
│   │       └── hook_assembler.py       # Hook-based transcript assembly
│   │
│   ├── storage/                        # SQLite storage layer (~29 modules)
│   │   ├── database.py                 # LocalDatabase (connection management)
│   │   ├── migrations.py               # Schema migrations
│   │   ├── sessions.py                 # Session CRUD
│   │   ├── session_models.py           # Session models
│   │   ├── session_messages.py         # Session message storage
│   │   ├── session_lifecycle.py        # Session lifecycle storage
│   │   ├── session_resolution.py       # Session resolution
│   │   ├── session_tasks.py            # Session-task linking
│   │   ├── tasks.py                    # Task CRUD
│   │   ├── task_dependencies.py        # Task dependency storage
│   │   ├── memories.py                 # Memory storage
│   │   ├── agents.py                   # Agent storage
│   │   ├── agent_commands.py           # Agent command storage
│   │   ├── inter_session_messages.py   # Inter-session messaging
│   │   ├── workflow_definitions.py     # Workflow definition storage
│   │   ├── workflow_audit.py           # Workflow audit log
│   │   ├── pipelines.py               # Pipeline storage
│   │   ├── projects.py                 # Project storage
│   │   ├── prompts.py                  # Prompt storage
│   │   ├── secrets.py                  # Encrypted secrets storage
│   │   ├── config_store.py             # Configuration storage
│   │   ├── auth.py                     # Auth storage
│   │   ├── mcp.py                      # MCP server storage
│   │   ├── clones.py                   # Clone storage
│   │   ├── worktrees.py               # Worktree storage
│   │   ├── cron.py                     # Cron job storage
│   │   ├── cron_models.py             # Cron models
│   │   ├── compaction.py              # Compaction storage
│   │   └── merge_resolutions.py       # Merge resolution storage
│   │
│   ├── memory/                         # Persistent memory system (~11 modules)
│   │   ├── manager.py                  # MemoryManager
│   │   ├── vectorstore.py              # Qdrant-based VectorStore
│   │   ├── neo4j_client.py             # Neo4j knowledge graph client
│   │   ├── extractor.py                # LLM-powered fact extraction
│   │   ├── digest.py                   # Memory digest
│   │   ├── context.py                  # Memory context
│   │   ├── protocol.py                 # Memory protocol
│   │   └── services/                   # Memory services
│   │       ├── dedup.py                # LLM-based deduplication
│   │       ├── knowledge_graph.py      # Knowledge graph service
│   │       └── maintenance.py          # Memory maintenance
│   │
│   ├── llm/                            # Multi-provider LLM abstraction (~20 modules)
│   │   ├── service.py                  # LLMService manager
│   │   ├── base.py                     # Base provider
│   │   ├── factory.py                  # Provider factory
│   │   ├── resolver.py                 # Model resolver
│   │   ├── executor.py                 # Base executor
│   │   ├── cost_table.py               # Token cost tracking
│   │   ├── claude.py                   # Claude provider
│   │   ├── claude_cli.py               # Claude CLI integration
│   │   ├── claude_executor.py          # Claude executor
│   │   ├── claude_models.py            # Claude model definitions
│   │   ├── claude_streaming.py         # Claude streaming support
│   │   ├── sdk_compat.py               # Agent SDK compatibility patches
│   │   ├── gemini.py                   # Gemini provider
│   │   ├── gemini_executor.py          # Gemini executor
│   │   ├── codex.py                    # Codex provider
│   │   ├── codex_executor.py           # Codex executor
│   │   ├── openai_executor.py          # OpenAI executor
│   │   ├── litellm.py                  # LiteLLM provider
│   │   └── litellm_executor.py         # LiteLLM executor
│   │
│   ├── skills/                         # Skill management (~13 modules)
│   │   ├── loader.py                   # SkillLoader (filesystem, GitHub, ZIP)
│   │   ├── parser.py                   # SKILL.md parser
│   │   ├── sync.py                     # Bundled skill sync on startup
│   │   ├── manager.py                  # Skill manager
│   │   ├── search.py                   # Skill search (TF-IDF)
│   │   ├── injector.py                 # Skill injection
│   │   ├── formatter.py                # Skill formatting
│   │   ├── metadata.py                 # Skill metadata
│   │   ├── scaffold.py                 # Skill scaffolding
│   │   ├── scanner.py                  # Skill scanning
│   │   ├── updater.py                  # Skill updater
│   │   └── validator.py                # Skill validator
│   │
│   ├── tasks/                          # Task system (~11 modules)
│   │   ├── expansion.py                # TaskExpander (LLM-based decomposition)
│   │   ├── validation.py               # TaskValidator
│   │   └── prompts/                    # LLM prompts for expansion
│   │
│   ├── config/                         # Configuration (~18 modules)
│   │   ├── app.py                      # DaemonConfig (YAML config model)
│   │   ├── bootstrap.py                # Pre-DB bootstrap settings
│   │   ├── features.py                 # Feature flags
│   │   ├── llm_providers.py            # LLM provider config
│   │   ├── logging.py                  # Logging config
│   │   ├── mcp.py                      # MCP config
│   │   ├── sessions.py                 # Session config
│   │   ├── tasks.py                    # Task config
│   │   ├── skills.py                   # Skill config
│   │   ├── pipelines.py               # Pipeline config
│   │   ├── servers.py                  # Server config
│   │   ├── cron.py                     # Cron config
│   │   ├── voice.py                    # Voice config
│   │   ├── tmux.py                     # Tmux config
│   │   ├── watchdog.py                 # Watchdog config
│   │   ├── extensions.py               # Extension config
│   │   └── persistence.py              # Config persistence
│   │
│   ├── search/                         # Search engines (~8 modules)
│   │   ├── tfidf.py                    # TF-IDF search
│   │   ├── embeddings.py               # Embedding-based search
│   │   ├── unified.py                  # Unified search interface
│   │   ├── models.py                   # Search models
│   │   └── protocol.py                 # Search protocol
│   │
│   ├── autonomous/                     # Autonomous execution support (~4 modules)
│   │   ├── progress_tracker.py         # Progress tracking
│   │   ├── stop_registry.py            # Stop registry
│   │   └── stuck_detector.py           # Stuck detection
│   │
│   ├── sync/                           # Task/memory sync (~6 modules)
│   │   ├── tasks.py                    # Task sync (JSONL)
│   │   ├── memories.py                 # Memory sync
│   │   ├── github.py                   # GitHub sync
│   │   ├── linear.py                   # Linear sync
│   │   └── integrity.py                # Sync integrity checks
│   │
│   ├── prompts/                        # Prompt management (~4 modules)
│   │   ├── loader.py                   # Prompt loader
│   │   ├── models.py                   # Prompt models
│   │   └── sync.py                     # Prompt sync
│   │
│   ├── integrations/                   # External integrations (~3 modules)
│   │   ├── github.py                   # GitHub integration
│   │   └── linear.py                   # Linear integration
│   │
│   ├── clones/                         # Git clone management
│   │   └── git.py                      # Clone operations
│   │
│   ├── worktrees/                      # Git worktree management
│   │   └── git.py                      # Worktree operations
│   │
│   ├── scheduler/                      # Cron job scheduler (~3 modules)
│   │   ├── executor.py                 # Job executor
│   │   └── scheduler.py                # Scheduler engine
│   │
│   ├── voice/                          # Voice chat support
│   │   └── stt.py                      # Speech-to-text
│   │
│   ├── tools/                          # Tool utilities
│   │   └── summarizer.py               # Tool summarization
│   │
│   ├── utils/                          # Utilities (~15 modules)
│   │   ├── git.py                      # Git utilities
│   │   ├── daemon_client.py            # Daemon HTTP client
│   │   ├── machine_id.py               # Machine identification
│   │   ├── json_helpers.py             # JSON helpers
│   │   ├── logging.py                  # Logging utilities
│   │   ├── metrics.py                  # Metrics utilities
│   │   ├── validation.py               # Validation helpers
│   │   ├── id.py                       # ID generation
│   │   ├── fibonacci.py                # Fibonacci backoff
│   │   ├── project_context.py          # Project context helpers
│   │   ├── project_init.py             # Project initialization
│   │   ├── status.py                   # Status utilities
│   │   ├── version.py                  # Version utilities
│   │   └── dev.py                      # Dev utilities
│   │
│   ├── install/                        # Bundled assets
│   │   └── shared/                     # Shared rules, skills, workflows, agents
│   │       ├── rules/                  # 13 bundled rule groups (YAML)
│   │       ├── skills/                 # ~18 bundled skills
│   │       ├── workflows/              # 8 workflow definitions
│   │       ├── agents/                 # 10 agent definitions
│   │       └── variables/              # Default variable definitions
│   │
│   └── data/                           # Bundled data files
│       └── docker-compose.neo4j.yml    # Neo4j Docker setup
│
├── tests/                              # Test suite (~533 files)
│   ├── conftest.py                     # Pytest fixtures
│   ├── storage/                        # Storage layer tests
│   ├── mcp_proxy/                      # MCP proxy tests
│   ├── workflows/                      # Workflow and rule tests
│   ├── hooks/                          # Hook system tests
│   ├── agents/                         # Agent tests
│   ├── sessions/                       # Session tests
│   ├── adapters/                       # Adapter tests
│   ├── cli/                            # CLI tests
│   └── ...                             # memory, skills, search, etc.
│
├── web/                                # Web UI (React/TypeScript)
│
├── docs/                               # Documentation
│   ├── architecture/                   # Architecture docs
│   ├── guides/                         # User and developer guides (~24 guides)
│   ├── plans/                          # Implementation plans
│   ├── research/                       # Research documents
│   ├── references/                     # Reference material
│   ├── examples/                       # Example configurations
│   └── archive/                        # Archived documents
│
└── .github/                            # GitHub configuration
    └── workflows/                      # CI/CD pipelines
```

## Code Statistics

| Metric | Value |
|--------|-------|
| **Source Python Files** | ~505 |
| **Test Python Files** | ~533 |
| **Top-Level Packages** | 33 |
| **Bundled Rule Groups** | 13 |
| **Bundled Skills** | 18 |
| **Bundled Agent Definitions** | 10 |
| **Bundled Workflow Definitions** | 8 |
| **LLM Providers** | 5 (Claude, Gemini, Codex/OpenAI, LiteLLM) |
| **CLI Adapters** | 6 (Claude Code, Gemini, Codex, Cursor, Windsurf, Copilot) |
| **Test Coverage Target** | 80% |

## Module Dependencies

```
cli/
├── config/
├── runner.py
│   ├── servers/http.py
│   │   ├── servers/routes/*
│   │   ├── adapters/*
│   │   └── hooks/hook_manager.py
│   │       └── workflows/rule_engine.py
│   ├── servers/websocket/
│   ├── mcp_proxy/server.py
│   │   ├── mcp_proxy/manager.py
│   │   └── mcp_proxy/tools/*
│   ├── agents/runner.py
│   └── sessions/manager.py
├── storage/database.py
│   └── storage/migrations.py
├── llm/service.py
│   └── llm/{claude,gemini,codex,litellm}.py
└── utils/*
```
