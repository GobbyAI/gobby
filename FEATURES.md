# Gobby Features

This catalog describes capabilities implemented in the `0.5.0` codebase. It is organized by
subsystem and covers behavior wired through daemon startup, CLI registration, HTTP and WebSocket
routes, MCP registries, hooks, schedulers, and concrete managers. Documentation, wiki pages, and
index metadata are discovery aids; executable source is authoritative.

## Daemon and platform runtime

- Local long-running control-plane daemon with phased startup for storage, services,
  orchestration, HTTP, and WebSocket servers.
- PostgreSQL hub initialization, machine identity, authentication token, encrypted secrets,
  DB-backed configuration, project resolution, and managed credentials.
- Active-daemon lease with heartbeat, standby promotion, stale-owner recovery, PID ownership, and
  graceful restart and shutdown.
- Startup recovery for agents, MCP connections, cron jobs, pipelines, memory projections,
  code-index projections, tmux sessions, hook inboxes, wiki watchers, and external issue sync.
- Optional subsystem degradation is surfaced through health and status while core runtime remains
  operational.

Sources: [`src/gobby/runner.py`](src/gobby/runner.py),
[`src/gobby/runner_init/`](src/gobby/runner_init/),
[`src/gobby/daemon_lease_control.py`](src/gobby/daemon_lease_control.py), and
[`src/gobby/servers/_app_routes.py`](src/gobby/servers/_app_routes.py).

## Coding-assistant integrations

Full hook, transcript, and web-chat integration exists for:

- Claude Code
- Codex CLI
- Qwen Code
- Grok CLI
- Factory Droid

Additional support includes:

- Shared ACP client and transport used by ACP-native providers.
- Provider-specific streaming chat backends.
- Native hook configuration installers and templates.
- Antigravity/AGY hook translation and installation. Its current scope excludes transcript parsing
  and web chat.

Sources: [`src/gobby/adapters/`](src/gobby/adapters/),
[`src/gobby/sessions/transcripts/`](src/gobby/sessions/transcripts/), and
[`src/gobby/servers/websocket/chat/backends/`](src/gobby/servers/websocket/chat/backends/).

## Hook transport and semantic events

- `ghook` provides sandbox-tolerant hook dispatch, durable inbox enqueueing, detached delivery,
  enqueue-only operation, diagnostics, source normalization, and daemon POST transport.
- Provider payloads normalize into common session, turn, tool, model, compaction, subagent,
  permission, notification, task, configuration, file, directory, worktree, and elicitation events.
- `before_agent` maps to semantic `turn_start`; `after_agent` and `stop` map to `turn_end`.
- `turn_end` acts as the cross-provider enforcement boundary and can keep a turn alive.
- Current event support includes `setup`, `user_prompt_expansion`, `post_tool_batch`,
  `message_display`, `directory_added`, display-content rewriting, and Grok
  permission/failure/subagent events.
- Hook processing performs session registration, edit attribution, transcript work, memory
  delivery, pending-message delivery, summary dispatch, webhooks, MCP effects, tool-outcome
  tracking, and code-index notifications.

Sources: [`crates/ghook/src/args.rs`](crates/ghook/src/args.rs),
[`src/gobby/hooks/events.py`](src/gobby/hooks/events.py),
[`src/gobby/hooks/hook_manager.py`](src/gobby/hooks/hook_manager.py), and
[`src/gobby/hooks/event_handlers/`](src/gobby/hooks/event_handlers/).

## Session tracking and transcripts

- Durable project- and machine-scoped sessions with UUIDs and human `#N` references.
- Tracks provider external IDs, branch, terminal and tmux identity, web-chat state, parent/child
  lineage, agent run, workflow state, task activity, edit history, sandbox policy, approved tools,
  and model.
- Tracks cumulative input, output, and cache tokens; context-window occupancy; turn, message, and
  tool counts; structured handoffs; archival summaries; feedback; and provenance.
- Lifecycle includes active, paused, handoff-ready, expired, tombstoned, and cleanup behavior.
- Incremental transcript ingestion, normalized messages, windowed and searchable reads, archives,
  gzip seek indexes, recovery, restore, and status inspection.
- MCP tools expose session lookup, listing, statistics, usage, messages, search, summaries, handoff
  context, commit attribution, terminal capture and control, transcript recovery, and
  self-compaction.

Sources: [`src/gobby/storage/session_models.py`](src/gobby/storage/session_models.py),
[`src/gobby/storage/session_lifecycle.py`](src/gobby/storage/session_lifecycle.py), and
[`src/gobby/mcp_proxy/tools/sessions/`](src/gobby/mcp_proxy/tools/sessions/).

## Compaction and cross-session handoff

- Compaction preserves the existing session row and reactivates it after provider restart.
- Variables, workflows, task claims, agent-run ownership, parent linkage, and session identity
  survive the handoff.
- Clear creates a new session and can inject the previous session's summary.
- `set_handoff` supports terminal and web-chat sessions, verifies terminal ownership, refreshes
  summaries, records required skill reloads, and delivers a continuation command.
- Restart injects a bounded continuation summary and required or advisory skill instructions.
- Context-pressure observers use persisted occupancy, deduplicate guidance by compaction epoch, and
  support mid-turn or turn-start nudges.
- Missing or contradictory handoff identity produces structured diagnostics and safe fresh
  registration.

Sources:
[`src/gobby/hooks/event_handlers/_session_start/handoff.py`](src/gobby/hooks/event_handlers/_session_start/handoff.py),
[`src/gobby/mcp_proxy/tools/sessions/_terminal.py`](src/gobby/mcp_proxy/tools/sessions/_terminal.py),
and [`src/gobby/sessions/compact_continuation.py`](src/gobby/sessions/compact_continuation.py).

## Tasks and dependency management

- Rich task records: type, priority, hierarchy, dependencies, labels, validation criteria,
  commits, sessions, affected files, GitHub and Linear links, scheduling, isolation, automation,
  escalation, merge, delivery, and agent metadata.
- CRUD, search, reindexing, artifacts, comments, backup and restore, commit linking, file
  attribution, and session linking.
- Session-owned claims with conflict detection, delegated-child handling, takeover, and
  active-task synchronization.
- Parent/child trees and dependency graphs with cycle detection and upstream/downstream traversal.
- Ready and blocked queries plus ranked next-task recommendations.
- Build history, delivery status, stage state, validation history, and external synchronization.

Sources: [`src/gobby/storage/tasks/_models.py`](src/gobby/storage/tasks/_models.py),
[`src/gobby/mcp_proxy/tools/tasks/_factory.py`](src/gobby/mcp_proxy/tools/tasks/_factory.py),
[`src/gobby/mcp_proxy/tools/task_dependencies.py`](src/gobby/mcp_proxy/tools/task_dependencies.py),
and [`src/gobby/mcp_proxy/tools/task_readiness.py`](src/gobby/mcp_proxy/tools/task_readiness.py).

## Task validation and close gates

- Task closure evaluates a checklist rather than performing a raw status change.
- Gates can require leaf completion, linked commits, change summaries, fresh validation after the
  final edit, bounded criteria review, and affected-path justification.
- Validation categories become stale after later attributed edits.
- Latest definitive validation results determine readiness; unresolved failures block closure.
- Close preview exposes exact diagnostics before mutation.
- Deliberate override paths record their justification.
- Repeated criteria-review failures can trigger escalation.

Sources:
[`src/gobby/mcp_proxy/tools/tasks/_lifecycle_close.py`](src/gobby/mcp_proxy/tools/tasks/_lifecycle_close.py),
[`src/gobby/tasks/close_checklist.py`](src/gobby/tasks/close_checklist.py), and
[`src/gobby/mcp_proxy/tools/tasks/_lifecycle_validation.py`](src/gobby/mcp_proxy/tools/tasks/_lifecycle_validation.py).

## Plans, expansion, and TDD governance

- Persistent plan records with create, get, list, archive, and delete operations; content hashes;
  validation; coverage manifests; and regeneration.
- Plan review and approval state integrated with task operations.
- Structured plans compile into task trees and dependency graphs.
- Expansion supports validation, deduplication, resumable runs, QA, and deterministic pipeline
  execution.
- TDD hook rules track test-file writes and can block the first implementation write until a test
  exists.
- Task criteria can require loading the TDD skill.
- Plan-mode rules govern allowed operations and handoff into execution.

Sources: [`src/gobby/mcp_proxy/tools/plans/`](src/gobby/mcp_proxy/tools/plans/),
[`src/gobby/tasks/expansion/`](src/gobby/tasks/expansion/),
[`src/gobby/install/shared/workflows/pipelines/expand-task.yaml`](src/gobby/install/shared/workflows/pipelines/expand-task.yaml),
and [`src/gobby/install/shared/workflows/rules/tdd-enforcement/`](src/gobby/install/shared/workflows/rules/tdd-enforcement/).

## Stages, review, and build orchestration

- Configurable task stage state machines with attempt caps, review-round caps, reviewer selectors,
  required, optional, or human review, terminal stages, and agent or pipeline dispatch.
- Bundled lifecycle stages cover ideation, research, architecture, PRD, planning, expansion,
  development, epic QA, PR, and merge.
- Operations include initialize, start, complete, fail, submit, approve, reject, mutate future
  stages, record enhancements, record PR verdicts and merge results, escalate, and close linked
  issues.
- Build orchestration resolves task, plan, or issue input references; chooses build profiles and
  workspace isolation; dispatches stage agents or pipelines; and tracks results.
- Build controls provide start, stop, resume, restart, clean, recovery, branch cleanup, delivery,
  and observability.
- Dispatcher leases, mutexes, write-set guards, active-agent limits, and workspace merge handling
  coordinate concurrent work.

Sources: [`src/gobby/storage/tasks/_stage_registry.py`](src/gobby/storage/tasks/_stage_registry.py),
[`src/gobby/mcp_proxy/tools/tasks/_stage_ops.py`](src/gobby/mcp_proxy/tools/tasks/_stage_ops.py),
[`src/gobby/build/`](src/gobby/build/), and [`src/gobby/dispatch/`](src/gobby/dispatch/).

## Agents and multi-agent orchestration

- Spawn from a prompt or reusable agent definition.
- Claude, Codex, Droid, Grok, Qwen, and AGY provider targets with provider fallback chains.
- Provider, model, reasoning, tools, skills, variables, rules, workflows, pipelines, personas, and
  task association.
- Shared-checkout, worktree, clone, and inherited isolation modes.
- Batch dispatch of non-conflicting task briefs within project concurrency limits.
- Agent result and capture retrieval, output waiting, run listing, capacity checks, statistics, and
  live lookup.
- Graceful stop, explicit successful termination, targeted kill, stale-helper cleanup,
  daemon-restart reconciliation, idle and stall recovery, task recovery, PTY capture, and parent
  result delivery.

Sources:
[`src/gobby/mcp_proxy/tools/spawn_agent/_factory.py`](src/gobby/mcp_proxy/tools/spawn_agent/_factory.py),
[`src/gobby/mcp_proxy/tools/agents.py`](src/gobby/mcp_proxy/tools/agents.py),
[`src/gobby/agents/lifecycle_monitor.py`](src/gobby/agents/lifecycle_monitor.py), and
[`src/gobby/agents/watchdog/`](src/gobby/agents/watchdog/).

## Agent and cross-session messaging

- Direct messages to sessions, agents, projects, builds, or broadcast audiences.
- Priorities, metadata, recipient wakeups, WebSocket delivery, and next-tool-call context injection.
- Agent completion results can automatically populate run results and notify parents.
- Message history and pending-delivery state persist in the hub.

Source: [`src/gobby/mcp_proxy/tools/agent_messaging.py`](src/gobby/mcp_proxy/tools/agent_messaging.py).

## Worktrees, clones, and merge assistance

- Worktree create, list, show, claim, release, link, sync, merge, push, abandon, reactivate,
  cleanup, and delete operations.
- Full-clone isolation with equivalent ownership, task linking, sync, merge, stale cleanup, and
  deletion.
- Provider hooks are installed into isolated workspaces.
- Merge tooling inspects Git state and conflict landscape, hydrates conflicts, checks branch
  protection, manages resolution locks, supports direct or assisted resolution, and exposes start,
  status, apply, and abort operations.
- Workspace ownership and task attribution survive agent lifecycle recovery.

Sources: [`src/gobby/mcp_proxy/tools/worktrees/`](src/gobby/mcp_proxy/tools/worktrees/),
[`src/gobby/mcp_proxy/tools/clones.py`](src/gobby/mcp_proxy/tools/clones.py), and
[`src/gobby/mcp_proxy/tools/merge.py`](src/gobby/mcp_proxy/tools/merge.py).

## Workflows

- YAML and DB workflow definitions with inheritance, versions, priorities, enablement,
  provider/source filters, settings, variables, rules, observers, and ordered steps.
- Typed variables can be session-shared and persisted across turns.
- Steps support native and MCP tool allowlists and blocklists, wildcard selectors, transitions,
  exit expressions, before/success/error MCP handlers, and error-stay behavior.
- Workflow instances persist execution state.
- CRUD, import, export, restore, reload, dry-run evaluation, structural validation, status
  inspection, rules, variables, and agent definitions.

Sources: [`src/gobby/workflows/definitions.py`](src/gobby/workflows/definitions.py),
[`src/gobby/mcp_proxy/tools/workflows/`](src/gobby/mcp_proxy/tools/workflows/), and
[`src/gobby/workflows/dry_run.py`](src/gobby/workflows/dry_run.py).

## Declarative rules and enforcement

Rules can trigger on session, turn, agent, model, tool, compaction, subagent, permission,
elicitation, notification, task, teammate, instruction, configuration, cwd, directory, file, and
worktree events.

Effects include:

- Block.
- Set variables.
- Inject context.
- Replace display content.
- Invoke MCP tools inline or in the background.
- Record observations.
- Rewrite tool input.
- Allow or deny permissions.
- Request retries.
- Set watch paths or worktree paths.
- Answer elicitations.
- Load skills.
- Run local commands.

Evaluation supports conditions, priorities, agent and audience scope, tool selectors, templating,
persisted variables, aggregate reasons, and workflow-step enforcement.

Bundled rules cover task-before-edit, close gates, epic completion, TDD, progressive discovery,
code-index use, memory lifecycle, compaction handoff, reviewer lifecycle, plan mode, monolith
prevention, destructive commands, full-test blocking, daemon controls, worker safety, and
exfiltration protection.

Rule templates are installation inputs. Installed DB rules are runtime authority.

Sources: [`src/gobby/workflows/definitions.py`](src/gobby/workflows/definitions.py),
[`src/gobby/workflows/engine/`](src/gobby/workflows/engine/), and
[`src/gobby/install/shared/workflows/rules/`](src/gobby/install/shared/workflows/rules/).

## Pipelines

- Typed sequential automation with input and output schemas and persisted execution state.
- Step kinds include shell execution, LLM prompts, MCP calls, nested pipelines, and event waits.
- Conditions, approval gates, timeouts, webhooks, tool restrictions, outputs, and optional MCP
  exposure.
- Run or detached execution, approve, reject, resume, reset from a failed step, cancel, status,
  history, and search.
- Heartbeats, stalled-run detection, restart recovery, and interrupted-execution reconciliation.
- Bundled task-expansion and merge/delivery pipelines.

Sources: [`src/gobby/workflows/definitions.py`](src/gobby/workflows/definitions.py),
[`src/gobby/workflows/pipeline/`](src/gobby/workflows/pipeline/), and
[`src/gobby/mcp_proxy/tools/workflows/_pipelines.py`](src/gobby/mcp_proxy/tools/workflows/_pipelines.py).

## Cron and system automation

- Cron, interval, and one-shot scheduling.
- User-facing actions for shell commands, agent spawning, and pipelines.
- Internal registered-handler and dispatcher actions.
- CRUD, enable and disable, manual execution, run history, timeout handling, and child-run
  tracking.
- Background pipeline execution with session creation and persisted failure state.
- System jobs drive maintenance, wiki work, memory dreaming, indexing, cleanup, and recovery.

Sources: [`src/gobby/mcp_proxy/tools/cron.py`](src/gobby/mcp_proxy/tools/cron.py),
[`src/gobby/scheduler/executor.py`](src/gobby/scheduler/executor.py), and
[`src/gobby/storage/cron.py`](src/gobby/storage/cron.py).

## Persistent memory

- Project and global memories with types, tags, supersession, soft deletion, restore, project
  moves, and promote or demote operations.
- Hybrid recall combining keyword, Qdrant vector, and FalkorDB graph results.
- Automatic prompt recall, deferred and chunked overflow retrieval, duplicate suppression, capture
  nudges, and independent turn-end shadow-relevance judging.
- Semantic cross-references and entity knowledge graphs.
- Graph search, clustering, co-occurrence densification, rebuild, reconciliation, invalidation,
  and embedding reindex.
- Memory-dream hygiene runs with plan and apply, run inspection, and conflict-aware revert.
- JSONL backup and restore plus Markdown export.

Sources: [`src/gobby/memory/manager.py`](src/gobby/memory/manager.py),
[`src/gobby/memory/services/`](src/gobby/memory/services/),
[`src/gobby/mcp_proxy/tools/memory.py`](src/gobby/mcp_proxy/tools/memory.py), and
[`src/gobby/memory/dream/`](src/gobby/memory/dream/).

## Review learning

- Converts review findings into structured, reusable memory lessons.
- Recalls lessons by file path, finding class, check key, language, and repository context.
- Tracks decision, risk, prevention guidance, evidence, fingerprints, and verified fixes.
- Supports lesson recording, deduplication, promotion, retirement, and targeted retrieval.
- Review and QA stages consume prior lessons as context.

Sources: [`src/gobby/review_learning/service.py`](src/gobby/review_learning/service.py) and
[`src/gobby/mcp_proxy/tools/review_learning.py`](src/gobby/mcp_proxy/tools/review_learning.py).

## MCP proxy and progressive discovery

- One stdio-facing MCP server routes to Gobby's internal registries and downstream MCP servers.
- Downstream transports include stdio, HTTP, SSE, and WebSocket.
- Server inventory, lightweight tool inventory, on-demand schemas, context-scoped schema leases,
  and persisted inventory observations.
- Tool calls, resource reads, server add, remove, and import, semantic tool search,
  recommendations, and session variables.
- Persistent registry, lazy connections, cached inventories, health monitoring, reconnection,
  argument validation, normalized errors, and result offloading.
- Before and after-tool rule enforcement applies to proxied calls.
- Semantic embeddings support LLM, semantic, and hybrid recommendations.
- Usage, latency, success and failure, rule, and skill metrics.

Sources: [`src/gobby/mcp_proxy/server.py`](src/gobby/mcp_proxy/server.py),
[`src/gobby/mcp_proxy/services/tool_proxy.py`](src/gobby/mcp_proxy/services/tool_proxy.py),
[`src/gobby/mcp_proxy/transports/`](src/gobby/mcp_proxy/transports/), and
[`src/gobby/mcp_proxy/registries.py`](src/gobby/mcp_proxy/registries.py).

## Skills

- List, search, load full bodies and supporting files, install, update, remove, restore, and move
  between project and global scope.
- Semantic reindexing and validated runtime script materialization.
- External discovery through ClawdHub, SkillsMP, GitHub collections, GitHub topics, and Claude
  Plugins.
- Workflow rules can load skills at hook time.
- Agent definitions can require or preload skills.

Sources: [`src/gobby/mcp_proxy/tools/skills/`](src/gobby/mcp_proxy/tools/skills/),
[`src/gobby/skills/manager.py`](src/gobby/skills/manager.py), and
[`src/gobby/skills/hubs/`](src/gobby/skills/hubs/).

## Code intelligence and `gcode`

- Incremental tree-sitter indexing of files, hashes, symbols, signatures, docstrings, spans,
  calls, imports, and content chunks into PostgreSQL.
- Current parser count is 21: Python, JavaScript, TypeScript/TSX, Go, Rust, Java, Objective-C, C,
  C++, C#, Ruby, PHP, Swift, Kotlin, Scala, Lua, Dart, Elixir, Bash, JSON, and YAML.
- Exact indexed grep; BM25 symbol and content search; exact-first symbol lookup; and hybrid lexical,
  vector, and graph search.
- Path, language, symbol-kind, pagination, graph-neighbor, and token-budget filters.
- File outlines, symbol-by-ID, symbol-at-location, batch symbol retrieval, kinds, file tree, and
  repository outline.
- Callers, usages, imports, shortest paths, neighbors, file graphs, and transitive blast radius.
- Qdrant vector and FalkorDB graph projection sync, rebuild, clear, and orphan cleanup.
- Freshness checks, stale-project pruning, embedding diagnostics, daemon-triggered post-edit
  indexing, nightly refresh, and optional symbol summaries.
- Rust `gcode` owns indexing and the read-only `codewiki_facts` facade consumed by CodeWiki;
  Python [`src/gobby/code_index/`](src/gobby/code_index/) owns daemon-side indexing orchestration.

Sources: [`crates/gcode/src/cli.rs`](crates/gcode/src/cli.rs),
[`crates/gcode/src/commands/`](crates/gcode/src/commands/), and
[`src/gobby/code_index/`](src/gobby/code_index/).

## Wiki and research system

- Project and named-topic Markdown vaults with raw assets, manifests, knowledge pages, generated
  code docs, reports, provenance, citations, and health metadata.
- Local-file and URL ingestion, inbox collection, session transcript sync, source refresh, list,
  and remove, purge, and stale-project pruning.
- Document, PDF, HTML, text, audio, image, and video processing through configured transcription,
  translation, vision, and text-generation routes.
- Hybrid BM25, vector, and graph search with bounded snippets, attribution, citations, token
  budgets, and graph degradation handling.
- Thin-RAG and deep-evidence `ask`, optional synthesis, citation validation, and evidence assembly.
- Read, list, write, and delete pages; backlinks; unresolved-link suggestions; and compilation of
  research notes into concepts and topics.
- Audit, lint, normalization, trust, status, health, benchmarks, citation-quality reports,
  librarian proposals, upkeep, daily recaps, and review reports.
- Unified graph export, graph-context packs, and generated workflow and report bundles.
- `gwiki code` generation with scoped and incremental builds, Git-ref changes, grounded
  verification, citation repair, comparison, audience and prose controls, and purge.
- MCP, HTTP, CLI, and Activity-panel UI surfaces.

Sources: [`crates/gwiki/src/cli.rs`](crates/gwiki/src/cli.rs),
[`crates/gwiki/src/commands/`](crates/gwiki/src/commands/),
[`crates/gwiki/src/commands/code/`](crates/gwiki/src/commands/code/),
[`src/gobby/mcp_proxy/tools/wiki.py`](src/gobby/mcp_proxy/tools/wiki.py), and
[`src/gobby/wiki/`](src/gobby/wiki/).

## Communications

Configured adapter implementations exist for:

- Slack
- Telegram
- Discord
- Microsoft Teams
- Email
- SMS
- Gobby Chat

Capabilities include:

- Inbound and outbound messages, threads, attachments, reactions, rate limits, group policies,
  identity-to-session links, event subscriptions, proactive messages, and project routing.
- Telegram access controls, callbacks, topics, stickers, link previews, proxying, and native plan
  actions.
- Voice transcription, TTS delivery, and sticker or image understanding where configured.
- Channel CRUD, history, attachment sending, subscriptions, identities, and proactive-send MCP
  operations.

Sources: [`src/gobby/communications/`](src/gobby/communications/) and
[`src/gobby/mcp_proxy/tools/communications.py`](src/gobby/mcp_proxy/tools/communications.py).

## LLM providers, model routing, and voice

- Central LLM service with model registry, context-window metadata, prompt rendering, image
  payloads, local provider support, and provider fallback.
- Provider capability collectors for Claude, Codex, Droid, Grok, and Qwen.
- Feature profiles and explicit provider, model, and reasoning candidate chains.
- Runtime provider metadata refresh and generation endpoints.
- Speech-to-text HTTP endpoint with capability detection and guarded lazy loading.
- Text-to-speech providers, Chatterbox support, sentence buffering, and text normalization.
- Persistent voice vocabulary add, remove, list, and clear tools.

Sources: [`src/gobby/llm/`](src/gobby/llm/),
[`src/gobby/providers/capabilities/`](src/gobby/providers/capabilities/),
[`src/gobby/voice/`](src/gobby/voice/), and
[`src/gobby/servers/routes/voice.py`](src/gobby/servers/routes/voice.py).

## Projects, configuration, and installation

- Stable project identity in `.gobby/project.json`.
- Project initialize, list, show, update, rename, soft delete, purge, repair, and
  verification-command refresh.
- Typed daemon configuration with PostgreSQL-backed dotted-key overrides and batch updates.
- Validation, defaults, encrypted secret expansion, masking, export and import, templates,
  prompts, UI settings, and tool-approval policies.
- Separate bootstrap, runtime, project verification, build-profile, and MCP-server configuration
  surfaces.
- Installers configure services, hooks, MCP clients, tmux, PostgreSQL, Qdrant, FalkorDB, gcode,
  gwiki, ghook, and supported coding assistants.
- Operator CLI covers daemon, service, auth, secrets, datastores, projects, sessions, tasks,
  builds, agents, workspaces, memory, workflows, rules, pipelines, integrations, testing, backup,
  and diagnostics.

Sources: [`src/gobby/cli/__init__.py`](src/gobby/cli/__init__.py),
[`src/gobby/config/`](src/gobby/config/),
[`src/gobby/storage/projects.py`](src/gobby/storage/projects.py), and
[`src/gobby/cli/installers/`](src/gobby/cli/installers/).

## HTTP, WebSocket, and web UI

- FastAPI routers expose auth, admin, agents, builds, chat, attachments, sessions, memory, tasks,
  stages, code index, cron, MCP, hooks, webhooks, pipelines, files, GitHub triage, projects,
  profiles, providers, skills, LLM, embeddings, voice, configuration, workflows, rules, source
  control, traces, metrics, observations, wiki, and communications.
- WebSocket server handles streaming chat, live events, traces, and connected-client state.
- Current web app uses Chat as its sole page surface.
- Operational UI lives in Activity tabs: sessions, terminal, tasks, MCP, agents, stages and
  profiles, skills, memory, integrations, wiki, rules, plans, changes, files, pipelines, and cron.
- Project selection, authentication, provider and model selection, attachments, streaming
  responses, file operations, and activity-detail workflows.

Sources: [`src/gobby/servers/_app_routes.py`](src/gobby/servers/_app_routes.py),
[`src/gobby/servers/websocket/server.py`](src/gobby/servers/websocket/server.py),
[`web/src/App.tsx`](web/src/App.tsx), and
[`web/src/components/activity/ActivityPanel.tsx`](web/src/components/activity/ActivityPanel.tsx).

## GitHub, Linear, Git, and source control

- Repository status, branches, checkout, history, diffs, pull requests, checks, issues, CI runs,
  worktrees, and clones through HTTP and UI.
- GitHub repository linking, issue import and deduplication, task synchronization, PR creation,
  and unlinking.
- Automated issue triage with HMAC validation, idempotent delivery records, semantic duplicate
  detection, structured judgment, task and build creation, labels, comments, recovery, and
  merge-time issue closure.
- Linear team and project linking, discovery, issue import and create, task synchronization, and
  project-wide bidirectional sync.
- Installed Git hooks run named verification stages.
- Pre-push refreshes task and memory snapshots and handles wiki publication.

Sources: [`src/gobby/servers/routes/source_control.py`](src/gobby/servers/routes/source_control.py),
[`src/gobby/github_triage/`](src/gobby/github_triage/),
[`src/gobby/integrations/`](src/gobby/integrations/), and
[`src/gobby/hooks/verification_runner.py`](src/gobby/hooks/verification_runner.py).

## Persistence, portability, and recovery

- PostgreSQL hub with pooled and bounded transactions, savepoints, advisory locks, safe updates,
  migrations, and after-commit callbacks.
- Qdrant vector projections and FalkorDB code, wiki, and memory graphs.
- Deterministic atomic task and memory JSONL snapshots under
  `~/.gobby/backups/<project-uuid>/`.
- Explicit validated backup and restore through CLI and MCP.
- `gobby pack` and `unpack` migrate configuration, transcripts, vector state, Docker volumes, and
  PostgreSQL dumps, with dry-run and exclusions.
- Hub backup captures PostgreSQL, Qdrant, FalkorDB, managed volumes, rule audit logs, and machine
  identity.
- Backup manifests, artifact verification, scratch restore checks, checksums, dump readability,
  extension, role, and schema verification, and explicit restore targets.

Sources: [`src/gobby/storage/hub/`](src/gobby/storage/hub/),
[`src/gobby/sync/`](src/gobby/sync/), [`src/gobby/cli/pack.py`](src/gobby/cli/pack.py), and
[`src/gobby/cli/hub_backup/`](src/gobby/cli/hub_backup/).

## Observability and diagnostics

- Lightweight health and comprehensive status endpoints.
- Uptime, CPU, memory, threads, file descriptors, background tasks, datastore health, MCP latency,
  DB saturation, provider models, and subsystem counts.
- Prometheus-style metrics and persistent event analytics.
- Tool, rule, skill, session, token, latency, success and failure, retention, archive, and
  time-series reporting.
- Rotating logs, subsystem-specific logs, parser error logs, JSON and OpenTelemetry formatting,
  local span storage, and live trace broadcasting.
- Observations, token usage, cache usage, and savings reporting.
- Hook-runtime compatibility and degraded-service diagnostics.

Sources: [`src/gobby/servers/routes/admin/_health.py`](src/gobby/servers/routes/admin/_health.py),
[`src/gobby/telemetry/`](src/gobby/telemetry/), and
[`src/gobby/mcp_proxy/tools/metrics.py`](src/gobby/mcp_proxy/tools/metrics.py).

## Test and project-verification tooling

- `gobby test-quality audit` scans Python, Rust, JavaScript, and TypeScript-family tests for weak
  or missing assertions, sleeps, TODOs, unconditional skips, weak xfails, and mock-heavy tests.
- Fingerprinted count-aware baselines, suppressions with reasons, severity gates, text and JSON
  reports, and fail-on-new ratcheting.
- `gobby test-types audit` runs mypy across selected test paths and normalizes diagnostics into the
  same baseline model.
- Project verification stages support formatting, lint, types, unit, integration, security, code
  review, and custom commands.
- Test protection isolates subprocess tests from the live daemon and real user state.

Sources: [`src/gobby/test_quality/`](src/gobby/test_quality/),
[`src/gobby/test_types/`](src/gobby/test_types/), and
[`src/gobby/project_verification/`](src/gobby/project_verification/).

## Stale and transitional documentation claims

- Current gcode parser count is **21**; references to 18 languages are stale.
- Current web UI is the Chat surface plus Activity tabs; multi-page dashboard and navigation
  descriptions are stale.
- Current Linear CLI retains `sync-all`; guide-only `--import`, `--create-missing`, and
  `--auto-sync` flags are omitted.
- AGY currently provides hook translation and installation support.
- Claude Plugins currently act as a skill-import source. A general
  `.codex-plugin/plugin.json` runtime was not found.
- TDD enforcement exists through rules and required skills. A universal fixed
  red/green/refactor task-node generator was not found.
- `gwiki code` owns manual CodeWiki generation; production-vault scheduling remains dormant while
  the wiki redesign is pending. `gcode` supplies indexing and the `codewiki_facts` facade.
- Multi-machine Pro, Rust-daemon replacement, and other roadmap-only capabilities are excluded.
