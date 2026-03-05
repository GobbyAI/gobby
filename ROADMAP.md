# Gobby Roadmap

Gobby is a **local-first control plane for AI coding tools**: sessions + hooks + tasks + workflows + MCP at scale.

Legend:

- ✅ Shipped
- 🚧 In progress
- 🗺️ Planned

---

## Guiding principles

- **Local-first by default** (your code/data stays on your machine)
- **Determinism beats vibes** (hooks + workflows + guardrails)
- **Progressive discovery everywhere** (tools, schemas, context)
- **Interoperability > lock-in** (plugins + adapters + open interfaces)

---

## Shipped

### MCP hub + progressive tool discovery

- ✅ Persistent daemon MCP server
- ✅ Downstream MCP proxy with progressive discovery (metadata → schema → call)
- ✅ Tool browsing/search utilities
- ✅ Dynamic MCP server management (add/remove/import)

### Sessions + handoffs

- ✅ Session tracking + local persistence
- ✅ Auto-compact, `/clear`, `/compact` with enhanced handoff context + injection
- ✅ Session handoff & digest overhaul (0.2.23)
- ✅ Session title synthesis (0.2.23)

### Hooks (determinism layer)

- ✅ Claude Code hook integration
- ✅ Gemini CLI hook integration
- ✅ Codex CLI: approval handler, context injection, app-server mode routing (0.2.13)
- ✅ Cursor, Windsurf, Copilot adapters (0.2.10)

### Tasks + TDD expansion

- ✅ `gobby-tasks` MCP: tasks, labels, dependencies, sync (`.gobby/tasks.jsonl`)
- ✅ Commit linking, validation gates, TDD expansion v2
- ✅ TF-IDF task search, Claude Code task interop
- ✅ Task status simplification (8 → 6 statuses), Gantt scheduling fields

### Rule engine

- ✅ Declarative rule enforcement (block, inject_context, set_variable, mcp_call)
- ✅ Named rule definitions with RuleStore (three-tier CRUD + bundled sync)
- ✅ 13 bundled rule groups
- ✅ SafeExpressionEvaluator replacing eval()
- ✅ Stop-gate & tool error recovery — hardcoded engine plumbing (0.2.23)

### Pipeline system

- ✅ PipelineExecutor with exec, prompt, invoke_pipeline, spawn_session, activate_workflow step types
- ✅ Approval gates, safe expression evaluator, result_variable, failure handling
- ✅ Lobster format import, WebSocket streaming
- ✅ CLI, MCP tools, and HTTP API endpoints
- ✅ Orchestrator pipeline with step workflow enforcement (0.2.23)
- ✅ Pipeline resume on daemon restart (0.2.23)

### Orchestration + agents

- ✅ Coordinator pipeline + developer/QA/merge agent trio (0.2.23)
- ✅ Autonomous SDK agent execution (0.2.23)
- ✅ Unified `spawn_agent` API with isolation: current, worktree, clone
- ✅ DB-backed agent registry with prompt fields and YAML export
- ✅ Tmux first-class agent spawning, auto terminal detection
- ✅ Inter-agent messaging: parent↔child message passing
- ✅ Conductor daemon, token budget tracking, review gates

### Workflows

- ✅ Observer engine with YAML-declared observers and behavior registry
- ✅ Multi-workflow support with concurrent instances per session
- ✅ Session variables, scoped variable MCP tools
- ✅ Unified workflow format (lifecycle + step YAMLs migrated)
- ✅ Legacy workflow system removal — WorkflowEngine, step/lifecycle, legacy digest (0.2.23)

### Memory

- ✅ `gobby-memory` MCP: lightweight, local, user-initiated memory (TF-IDF search)
- ✅ Memory v3–v4: backend abstraction, embedding persistence, lifecycle hooks, automated capture
- ✅ Memory v5: Qdrant vector store, LLM-powered dedup/extraction, KnowledgeGraphService, Mem0 fully removed (0.2.16)

### Web UI

- ✅ Chat with MCP tool support, voice chat (VAD), model switching, slash commands
- ✅ Tasks: kanban board, tree view, dependency graph, Gantt chart, detail panel, creation form
- ✅ Memory: table, filters, Neo4j 3D knowledge graph
- ✅ Sessions: lineage tree, transcript viewer, AI summary generation
- ✅ Cron Jobs, Configuration, Skills, Projects, Agent Registry pages
- ✅ File browser/editor with save/cancel, undo/redo
- ✅ Visual workflow builder with @xyflow/react (0.2.16)
- ✅ Smart tool headers, improved tool rendering (0.2.23)
- ✅ Mobile: drawer replacing sidebar, click-outside-to-close, conversation picker toggle (0.2.23)

### Skills system

- ✅ `gobby-skills` MCP: list, search, install, update, remove
- ✅ SKILL.md format (Agent Skills spec + SkillPort compatible)
- ✅ Core skills bundled, TF-IDF search, install from GitHub/local/ZIP
- ✅ Skill usage tracking in session stats

### Integrations + extensibility

- ✅ GitHub integration, Linear integration
- ✅ Plugin architecture (extensible domains/tools)

### Infrastructure

- ✅ DB-first config resolution, $secret:NAME pattern, encrypted secrets store
- ✅ Cron scheduler with CLI, HTTP, and MCP interfaces
- ✅ Code decomposition rounds 1–3 (strangler fig), canonical imports only
- ✅ Worktree creation + agent spawning primitives
- ✅ Terminal: consolidated tmux spawners, pane monitoring, title synthesis

---

## In progress — Orchestration v3

The current orchestrator creates one worktree per task, producing N branches and N merge operations. v3 fixes this and establishes a clean three-part mental model: **rules** (reactive enforcement), **agents** (intelligent workers with phased behavior), **pipelines** (deterministic orchestration).

Seven stages, each independently shippable:

1. **Single worktree per epic + bug fixes** — one worktree per epic instead of per-task, sequential dispatch, `use_local` for clones
2. **Agent system overhaul** — remove `extends`, agents absorb their step workflows, inline rule_definitions extracted to templates
3. **`task_affected_files` infrastructure** — file-based dependency analysis, overlap detection for parallel dispatch
4. **Expansion sub-pipeline** — research agent produces spec, mechanical builder creates tasks (hard boundary, no mixing)
5. **Parallel dispatch** — `suggest_next_tasks` returns batches of non-conflicting tasks, multiple agents in shared worktree
6. **Deterministic TDD enforcement** — rule-based (not prompt-based), block implementation writes until tests exist, per-file validation criteria
7. **Documentation rewrite** — three-part model: rules/agents/pipelines, delete and rewrite all workflow guides

See `docs/plans/orchestrator-v3-final.md` for full implementation details.

---

## Near term — UI fit & polish for v1 launch

- 🗺️ Mobile responsiveness pass
- 🗺️ Visual workflow builder completion (additional node types, validation, undo/redo)
- 🗺️ Hook inspector
- 🗺️ General UX polish pass

---

## After v1

### OpenTelemetry integration

- 🗺️ Tool call tracing (latency, success/error, payload size)
- 🗺️ Session timeline view (event stream: hooks fired, tools invoked, compactions, files changed)
- 🗺️ Replace custom logging/metrics with OpenTelemetry
- 🗺️ OTLP export + console fallback for local dev

### Ollama support

- 🗺️ Local model provider for chat, task expansion, summarization

---

## Future

### Pro / cloud features

- 🗺️ Fleet management (multi-machine coordination)
- 🗺️ Team workflows and shared task boards
- 🗺️ Enterprise hardening (auth, audit, compliance)

### Plugin ecosystem v2

- 🗺️ Dedicated MCP server for plugin management
- 🗺️ Plugin registry conventions + compatibility checks
- 🗺️ Community examples: integrations, workflows, hook packs

### Additional CLI support

- 🗺️ Aider
- 🗺️ Continue
- 🗺️ Amazon Q Developer CLI

### Additional memory adapters

- 🗺️ Stable Memory API (store/retrieve/summarize/evict)
- 🗺️ Additional vector DB adapters (Chroma, Weaviate, etc.)

### Starter packs

- 🗺️ Curated hook + workflow + task bundles by stack (Python/Node/Go/etc.)

---

## Explicit non-goals (unless proven necessary)

- Moving core execution to a hosted SaaS
- Forcing a single agent framework
- Hiding behavior behind "magic prompts"

Gobby wins by being the **boring, reliable system layer** under your AI tools.
