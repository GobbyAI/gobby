# Gobby Roadmap

Gobby is a **local-first control plane for AI coding tools**: sessions + hooks + tasks + workflows + MCP at scale.

The 0.3.X line shipped the architecture. **0.4.0 is the "go loud" release** — the first version Gobby will be actively marketed, recommended, and supported in front of people who did not build it. Everything in 0.4.X ladders up to that bar.

Legend:

- ✅ Shipped
- 🚧 In progress
- 🗺️ Planned

---

## Guiding principles

- **Local-first by default** (your code and data stay on your machine)
- **Determinism beats vibes** (hooks + workflows + guardrails)
- **Progressive discovery everywhere** (tools, schemas, context)
- **Interoperability > lock-in** (plugins, adapters, open interfaces)

---

## The road to 0.4.0 — "Go Loud"

0.3.X is architecturally done: multi-provider web chat, task lifecycle v2, the memory FTS5 + YAKE stack, the rule engine, the MCP proxy, the orchestrator pipeline, and the communications platform are all in place. 0.4.X is about closing the gap between "works for the people who built it" and "shippable to strangers."

### 1. Finish code review and cleanup

- 🚧 Burn down the outstanding CodeRabbit review queue
- 🚧 Close the long tail of cleanup and tech-debt tasks in `gobby-tasks`
- 🚧 Zero-warning pass on `ruff`, `mypy --strict`, frontend lint, and type-check across `src/`, `tests/`, and `web/`
- 🗺️ Final audit for dead code, unused exports, and shims left behind by 0.3.X refactors

### 2. Flatten the migration — **breaking change**

Tables and columns have accumulated cruft across dozens of incremental migrations. Before 0.4.0 we're collapsing that history into a single "initial schema" migration and dropping or renaming columns from refactors that never cleaned up after themselves. (Precedent: 0.3.0 squashed v134–v171 into a baseline — this is the same move, applied to everything.)

- 🗺️ Audit every table and column against current code; drop what has no readers
- 🗺️ Fold the 0.0.1 → 0.3.X migration chain into a single seed migration
- 🗺️ One-shot export/import tool so users on 0.3.X can carry their data forward
- 🗺️ Upgrade guide published alongside the release

**Databases created before 0.4.0 will not be automatically upgradable after this lands.** The export/import tool is the supported path.

### 3. UI improvements and testing

- 🚧 Tailwind migration completion with consistent spacing, typography, and color tokens
- 🗺️ Expanded Playwright coverage for the Tasks, Sessions, Memory, and Chat tabs (building on the verification suite from 0.2.30)
- 🗺️ Accessibility pass: keyboard nav, focus rings, aria labels
- 🗺️ Dark-mode polish
- 🗺️ Hook and rule inspectors
- 🗺️ Visual workflow builder completion — validation, undo/redo, additional node types

### 4. Onboarding enhancements

- 🗺️ Extend the `gobby install` wizard to cover embeddings, local models, and first-project setup in one pass
- 🗺️ First-run web tour across Tasks, Sessions, Memory, and the MCP proxy
- 🗺️ Sample project with pre-seeded tasks, rules, and workflows
- 🗺️ `gobby doctor` — end-to-end diagnosis of hook, MCP, embedding, and service misconfiguration

### 5. Local AI integration testing

- 🗺️ End-to-end test matrix across Ollama, LM Studio, and llama.cpp covering embeddings, chat, task expansion, and memory recall
- 🗺️ Recommended model lists per tier (tiny, small, medium, large) with benchmarks
- 🗺️ Graceful degradation when local models are unavailable or misconfigured

### 6. Finalize bundled agents, workflows, rules, variables

- 🚧 Freeze the canonical set of bundled rule templates and session variables
- 🗺️ Finalize canonical agent definitions (developer, QA, QA-Dev, merge, planner)
- 🗺️ Finalize canonical pipelines (orchestrator, dev-loop, merge-worktree, spawn-*)
- 🗺️ "What ships in the box" guide plus a companion "how to override" guide

### 7. Communication integration E2E testing

Gobby's communication channels need to be verified end to end before users are told to rely on them for notifications and agent interaction. Adapters exist for most channels; they need a hardened test pass and a real-world shakedown.

#### Verify existing adapters

- 🗺️ **Slack** — channel posts, DMs, slash command round-trip, Block Kit rendering
- 🗺️ **Microsoft Teams** — channel posts, adaptive cards, proactive messaging via `ConversationReference`
- 🗺️ **Discord** — channel posts, slash commands, bot DMs, gateway reconnect
- 🗺️ **Telegram** — bot messages and callback round-trip
- 🗺️ **Email** — SMTP send + IMAP ingest round-trip, OAuth2 XOAUTH2, HTML multipart
- 🗺️ **SMS** — Twilio send + inbound webhook round-trip with signature verification
- 🗺️ Notification routing rules with per-channel opt-in / opt-out

#### Build + verify

- 🗺️ **WhatsApp** — adapter via Meta Cloud API, message round-trip, media attachments

---

## After 0.4 — the 0.5.X horizon

### Python → Rust migration

The hot paths of the daemon — MCP proxy, hook dispatcher, rule engine, storage layer — move to Rust. The CLI, web UI, and bundled content stay as-is; this is a runtime swap, not a redesign of the user-facing surface.

- 🗺️ Rust daemon skeleton with a parity test harness against the Python daemon
- 🗺️ Port the MCP proxy, hook dispatcher, rule engine, storage layer
- 🗺️ Python daemon kept available as a fallback during the transition
- 🗺️ **Planned completion: 0.5.X**

### Multi-daemon and cloud sync — Pro tier

Setting the stage for running Gobby across multiple machines with a shared control plane.

- 🗺️ Multi-daemon discovery and handshake protocol
- 🗺️ Fleet management (inventory, health, remote command)
- 🗺️ Opt-in, end-to-end encrypted cloud sync for tasks, memories, and session metadata
- 🗺️ Team workflows and shared task boards
- 🗺️ Enterprise hardening (auth, audit, compliance)
- 🗺️ **Planned for 0.5.X as a paid tier**

---

## Later

### Desktop and mobile apps

- 🗺️ Native desktop app with persistent tray and bundled daemon
- 🗺️ iOS app for observing sessions, reviewing tasks, and approving gates on the go

### Plugin ecosystem v2

- 🗺️ Dedicated MCP server for plugin management
- 🗺️ Plugin registry conventions and compatibility checks
- 🗺️ Community examples: integrations, workflows, hook packs

### Additional CLI support

- 🗺️ Aider
- 🗺️ Continue
- 🗺️ Amazon Q Developer CLI

### Starter packs

- 🗺️ Curated hook + workflow + task bundles by stack (Python / Node / Go / Rust)

---

## Shipped

### 0.3.X highlights

- ✅ **Multi-provider web chat** — Claude (SDK), Gemini (ACP), and Codex (CLI subprocess) under a unified `ChatSessionProtocol` with hold-open permission gating and session handoff (0.3.6)
- ✅ **Task lifecycle v2** — canonical task transitions, claimed-session persistence, transactional expansion pipeline (0.3.6)
- ✅ **Memory system upgrade** — FTS5 + YAKE keyword search, nomic `search_query` / `search_document` prefixes, score provenance, `gobby memory invalidate` fast clear + background rebuild (0.3.6)
- ✅ **Voice** — Kokoro replaced with Chatterbox voice cloning, lazy model loading with idle eviction (0.3.6)
- ✅ **Codex hooks adapter** — full `hooks.json` lifecycle parity with Claude Code: routing, tool normalization, `PreToolUse` honoring, transcript parsing (0.3.5–0.3.6)
- ✅ **Startup / CLI overhaul** — step-by-step progress, polled startup tracker, compact ready summary (0.3.6)
- ✅ **Model registry** — OpenRouter-backed context windows and cost tables replacing LiteLLM (0.3.5)
- ✅ **Embedding setup wizard** during `gobby install` with OpenAI-SDK embeddings against any compatible endpoint (0.3.5)
- ✅ **Communications platform** — adapter ABC, registry, rate limiter, router, storage; adapters for Slack, Teams, Discord, Telegram, Email, SMS, plus IntegrationsPage UI and user guide (0.2.30, 0.3.2)
- ✅ **Windows service support** via Task Scheduler (0.3.2)
- ✅ **Agent mode simplification** — agents always spawn via tmux; autonomous/interactive/in_process modes removed (0.3.4)
- ✅ **LiteLLM and llama-cpp-python fully removed**; embeddings now use the OpenAI SDK directly (0.3.5)
- ✅ **Cursor, Windsurf, Copilot, Antigravity provider references purged** — Gobby now targets Claude, Gemini, and Codex only (0.3.5)

### MCP hub + progressive tool discovery

- ✅ Persistent daemon MCP server
- ✅ Downstream MCP proxy with progressive discovery (metadata → schema → call)
- ✅ Tool browsing and search utilities; `search_tools` covers internal tools
- ✅ Dynamic MCP server management (add, remove, import)
- ✅ `project_id` parameter on `call_tool` for cross-project operations (0.3.6)
- ✅ Brave Search as a default MCP server during install (0.3.6)

### Sessions + handoffs

- ✅ Session tracking and local persistence across restarts and compactions
- ✅ Auto-compact, `/clear`, `/compact` with enhanced handoff context injection
- ✅ Session title synthesis
- ✅ `session_type` column so web chats are first-class alongside CLI sessions (0.3.6)
- ✅ Transcript renderer pipeline with `session_messages` table dropped (0.2.30)

### Hooks (determinism layer)

- ✅ Claude Code hook integration
- ✅ Gemini CLI hook integration with tool name normalization and cancel-turn handling
- ✅ Codex CLI hook integration with full `hooks.json` lifecycle parity

### Tasks + TDD expansion

- ✅ `gobby-tasks` MCP: tasks, labels, dependencies, sync (`.gobby/tasks.jsonl`)
- ✅ Commit linking, validation gates, TDD expansion v2
- ✅ FTS5 task and skill search (replacing TF-IDF)
- ✅ Task lifecycle v2 — canonical transitions, transactional expansion (0.3.6)
- ✅ `gobby-tasks` MCP split into core + ops (0.3.6)
- ✅ Block `gobby tasks` CLI and redirect to the MCP server (0.3.6)

### Rule engine

- ✅ Declarative rule enforcement (block, inject_context, set_variable, mcp_call, load_skill)
- ✅ Named rule definitions with `RuleStore` (three-tier CRUD + bundled sync)
- ✅ `SafeExpressionEvaluator` replacing `eval()`
- ✅ Stop-gate and tool error recovery hard-wired into the engine
- ✅ `update_rule` MCP tool on `gobby-workflows` (0.3.6)
- ✅ `before_tool` rule enforcement on direct MCP calls (0.3.6)

### Pipeline system

- ✅ `PipelineExecutor` with `exec`, `prompt`, `invoke_pipeline`, `spawn_session`, `activate_workflow` step types
- ✅ Approval gates, result variables, failure handling, WebSocket streaming
- ✅ Pipeline resume on daemon restart
- ✅ Orchestrator pipeline with step workflow enforcement

### Orchestration + agents

- ✅ Unified `spawn_agent` API with isolation: current, worktree, clone
- ✅ DB-backed agent registry with prompt fields and YAML export
- ✅ Tmux first-class agent spawning with auto terminal detection
- ✅ Inter-agent messaging, conductor daemon, token budget tracking
- ✅ Orchestration v3 — tick-based pipeline, clone-based isolation, single clone per epic (0.2.28)
- ✅ Provider fallback rotation and stall detection (0.2.28)
- ✅ QA-Dev agent template — reviews and fixes in one pass (0.2.28)
- ✅ Persistent agent runtime state survives daemon restarts (0.2.28)

### Workflows

- ✅ Observer engine with YAML-declared observers and behavior registry
- ✅ Multi-workflow support with concurrent instances per session
- ✅ Unified workflow format (lifecycle + step YAMLs migrated)

### Memory

- ✅ `gobby-memory` MCP with semantic search and automated capture
- ✅ Qdrant vector store with LLM-powered dedup and extraction
- ✅ FTS5 + YAKE keyword search layered alongside semantic (0.3.6)
- ✅ Nightly memory cleanup pipeline

### Web UI

- ✅ Chat with MCP tool support, voice chat (VAD), model switching, slash commands
- ✅ Multi-provider chat across Claude, Gemini, and Codex (0.3.6)
- ✅ Tasks — kanban board, tree view, dependency graph, Gantt chart, detail panel
- ✅ Sessions — lineage tree, transcript viewer, AI summary generation
- ✅ Memory — table, filters, knowledge graph view
- ✅ Cron Jobs, Configuration, Skills, Projects, Agent Registry pages
- ✅ File browser and editor with save, cancel, undo, redo
- ✅ Code tab — file editor and code graph explorer
- ✅ Visual workflow builder (`@xyflow/react`)
- ✅ Trace viewer — `TracesPage`, `TraceWaterfall`, `TraceDetail`
- ✅ Playwright verification suite (0.2.30)

### Skills system

- ✅ `gobby-skills` MCP — list, search, install, update, remove
- ✅ SKILL.md format (Agent Skills spec + SkillPort compatible)
- ✅ Install from GitHub, local, or ZIP
- ✅ JIT Python and Rust skill injection on first file read (0.3.6)

### Integrations + extensibility

- ✅ GitHub integration, Linear integration
- ✅ Communications platform — Slack, Teams, Discord, Telegram, Email, SMS, gobby_chat (0.2.30, 0.3.2)
- ✅ Plugin architecture (extensible domains / tools)

### OpenTelemetry observability

- ✅ Tool call tracing (latency, success / error, payload size) (0.2.28)
- ✅ Metrics instruments for MCP calls, pipelines, tasks, hooks (0.2.28)
- ✅ OTLP gRPC export + Prometheus exporter (0.2.28)
- ✅ SQLite span storage with trace query API (0.2.28)

### Infrastructure

- ✅ DB-first config resolution, `$secret:NAME` pattern, encrypted secrets store
- ✅ Cron scheduler with CLI, HTTP, and MCP interfaces
- ✅ Native AST code indexing via `gobby-code` server (0.2.26)
- ✅ `gobby secrets` CLI with encrypted store (0.2.28)
- ✅ Windows service support via Task Scheduler (0.3.2)
- ✅ `set_config_batch` for atomic multi-key config updates (0.3.6)
- ✅ Local LLM provider with tier-based fallback and `gsqz` input compression (0.3.6)

---

## Explicit non-goals (unless proven necessary)

- Moving core execution to a hosted SaaS
- Forcing a single agent framework
- Hiding behavior behind "magic prompts"

Gobby wins by being the **boring, reliable system layer** under your AI tools.
