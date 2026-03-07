<!-- markdownlint-disable MD024 -->


# Changelog

All notable changes to Gobby are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.27]

### Features
- Wired set_variable/get_variable into stdio MCP server (#9879)

---

## [0.2.26]

### Features

#### Native AST Code Indexing
- Added native AST-based code indexing via gobby-code server (#9813)
- Made gobby index standalone (no daemon required) (#9828)
- Made `gobby index` the default subcommand (#9829)
- Added post-commit git hook for incremental code indexing (#9833)
- Triggered initial code indexing on `gobby init` (#9835)
- Wired session-start auto-indexing via HTTP endpoint (#9834)
- Added staleness detection to search_symbols (#9832)
- Set code_index_available session variable on session start (#9838)
- Made agents aware of gobby-code (#9856)

#### Session Transcript Blob Storage
- Added session transcript blob storage and restore (#9845)

#### Orchestrator Enhancements
- Added orchestrator single-task support with event-driven loop (#9818)

#### Output Compression
- Added compression banner to compressed tool output (#9814)
- Expanded compress-bash-output patterns (vitest, jest, npx, pnpm, bun, uv, turbo, nx, webpack, vite)

#### Variable Management
- Promoted set_variable/get_variable to top-level MCP proxy tools (#9875)

### Fixes
- Hardened stdio proxy health check: 3x retries with 5s timeout before restarting daemon
- Increased default daemon health check timeout from 2s to 5s
- Restored heavy-handed progressive discovery enforcement (#9841)
- Reverted progressive discovery rules from inject_context to block (#9824)
- Added shlex_quote Jinja2 filter to prevent shell injection in rule templates (#9826)
- Skipped commit/validation requirements for epics with all children closed (#9846)
- Expired handoff_ready sessions correctly (#9847)
- Guarded against empty compression results and empty subprocess output (#9844)
- Fixed C# and Dart language support in code_index (#9843)
- Implemented 24 accepted CodeRabbit suggestions (#9839)
- Capitalized Gobby in compression banners and prevented stale session handoff (#9817)
- Suppressed vitest canvas and forwardRef console warnings (#9854)
- Corrected tool availability claims in default-web-chat prompt (#9766)
- Set code-index skill injectionFormat to full (#9857)
- Used task_type consistently in task serialization (#9862)
- Added outputs block to orchestrator pipeline for result propagation (#9863)
- Resolved 4 post-restart daemon warnings (#9873)
- Addressed CodeRabbit report findings across 7 files (#9865)
- Reworded error-triage stop gate to prompt confirmation not re-run (#9874)
- Used task_type consistently in web UI components (#9877)
- Fixed 11 failing tests across 3 root causes (#9876)

### Refactoring
- Renamed CLI group from code-index to index (#9823)
- Moved index_folder from MCP tool to CLI command (#9821)
- Removed success keys from skills, agents, and hub return dicts (#9765, #9764, #9763)
- Replaced manual WS with useWebSocketEvent in usePipelineExecutions (#9825)

### Docs
- Added code-index and tool-compression guides (#9848)
- Added gobby index CLI examples to code-index guide (#9849)
- Warned against wait_for_completion on orchestrator pipeline (#9864)

---

## [0.2.25]

### Features

#### Web UI Test Infrastructure
- Added web UI test infrastructure with initial test coverage (#9697–#9702)
- Added localStorage mock and test utility sample tests (#9698, #9701)
- Added CodeBlock and KanbanBoard tests, reaching 352 total (#9214)
- Full web UI test suite — 13 test files, 326 tests, CI/CD integration (#9214)

#### Progressive Discovery Overhaul
- Moved progressive discovery from enforcement to transparency (#9740)

#### Workflows & Pipelines
- Added wait_for_completion MCP tool to gobby-workflows (#9722)
- Added gobby-tests MCP server with token-efficient output (#9734)
- Added register_pipeline_continuation for agent dispatch → pipeline resume (#9658)
- Added pipeline eval tool for structured data extraction (#9662)
- Added fail_pipeline guard step tool (#9661)
- Added pipeline resume from failure point (#9660)

#### Agent System
- Added tmux agent completion detection via completion_registry (#9723)
- Added pipeline-worker agent definition with scoped rules (#9659)

#### Memory & Context
- Added pre-compact context preservation rule (#9632)
- Added session-end context preservation rule (#9631)
- Added memory sync to JSONL for git-native portability (#9643)
- Consolidated memory dedup and auto-delete with semantic similarity (#9642, #9674)
- Added memory capture nudge on user prompt (#9637)
- Added memory suggest after task close (#9636)
- Added memory review stop gate (#9676)
- Added digest-on-plan-turn-end rule (#9682)

#### Rule Engine
- Added agent_scope to rules for per-agent targeting (#9656)
- Added rewrite_input effect type for rule-based command rewriting (#9648)
- Added output compression rules (compress-bash-output, compress-mcp-output) (#9649)
- Added rule tagging and selector system for agent-scoped rule loading (#9650)
- Wired rule tags/selectors into agent definitions (#9651)
- Added reload_cache to sync bundled rules, agents, and variables (#9741)

### Fixes
- Fixed restart races with orphaned watchdog causing false 'already running' (#9739)
- Fixed run_check paths parameter replacing instead of appending (#9743)
- Wired dynamic timeout for run_check and added stale run cleanup (#9742)
- Reconciled claimed_tasks on STOP to prevent false positives (#9726, #9732)
- Detected frontend in subdirectories and updated verification on re-init (#9736)
- Fixed error triage stop gate hard-reset bug with 3x circuit breaker (#9713)
- Cleared claimed_tasks on reopen_task (#9711)
- Fixed Hub search silent failures, bad defaults, missing deps (#9718)
- Rendered wait step template expressions in StepRenderer (#9720)
- Added diagnostic logging and hardening for session variable persistence (#9719)
- Clarified error-triage rule for pre-existing failures (#9730)
- Rewrote ClawHub provider for clawhub CLI v0.7 (#9728)
- Fixed single worktree per epic with use_local clone support (#9571)
- Handled connection resets in e2e health check (#9605)

### Refactoring
- Consolidated MCP proxy servers — workflows as umbrella (#9730)
- Reorganized rule YAML into group directories (#9652–#9655)
- Split tool registration by domain into modular files (#9667–#9672)

---

## [0.2.24]

### Features
- Replaced static cost table with LiteLLM-backed DB cache (#9595)
- Added native SDK executors for Gemini and OpenAI autonomous mode (#9446)
- Added MCP integration to rule editor mcp_call effect (#9330)
- Supported multiple claimed tasks per session (#9537)
- Added shutdown source identification to daemon signal handler
- Added LLM-powered compact handoff summaries with DB enrichment (#9545)
- Consolidated session summary generation into single code path (#9541)
- Defaulted neo4j_url and neo4j_auth to match docker-compose (#9583)

### Fixes
- Applied CodeRabbit triage fixes across backend, frontend, and tests (#9568, #9570, #9579, #9585, #9589, #9594)
- Resolved mypy type errors in cli/sessions.py (#9593)
- Scoped pip-audit pre-push hook to main/dev branches only (#9581)
- Used skill_manager directly for skills count in health endpoint (#9582)
- Killed stray watchdog processes on gobby start (#9561)
- Logged signal name, PID, PPID, and stack trace on daemon shutdown (#9562)
- Added close_task and validation tools to 300s timeout list (#9560)
- Added idempotency guard to viewSession and call sites (#9548)
- Wrote session summary files to project .gobby/session_summaries/ (#9543)
- Passed use_local to WorktreeGitManager in create_worktree MCP tool (#9538)
- Removed full pytest from pre-push, fixed stale test expectations

### Chores
- Dead code cleanup from Gemini code health analysis (#9592)
- Leveled migrations.py to v133 baseline (#9591)
- Regenerated source-tree.md from actual filesystem (#9590)
- Updated ROADMAP.md, README.md, and created AUTH.md (#9588)
- Integrated orchestrator-v3 and v3-p2 into final plan (#9569)

---

## [0.2.23]

### Major Features

#### Coordinator Pipeline & Agent Trio
- Added coordinator pipeline with developer, QA, and merge agent trio for orchestrated multi-agent workflows (#9393)
- Added orchestrator pipeline with step workflow enforcement for structured pipeline execution (#9510)
- Added pipeline resume on daemon restart to recover in-flight pipelines (#9513)
- Created child sessions in pipeline executor for P2P messaging between agents (#9389)
- Wired session_manager in runner and injected parent_session_id into pipeline inputs (#9391)
- Simplified developer and QA agent workflows for Haiku reliability (#9424)
- Wired rule_definitions into rule engine with deterministic shutdown sequencing (#9407)
- Moved merge from main repo to worktree to eliminate unsafe main repo operations (#9457, #9461)
- Added fetch_after_merge, auto_link_commits, stash/pull handling, and push delegation to merge agent (#9454)
- Registered skipped steps in pipeline context for downstream condition evaluation (#9419)

#### Autonomous SDK Agent Execution
- Replaced headless/embedded spawn modes with unified autonomous mode (#9439)
- Wired autonomous SDK dispatch in spawn_executor (#9441)
- Added lifecycle monitor to detect completed/failed autonomous tasks (#9442)
- Captured SDK session ID for cross-mode resume (#9443)

#### Session Handoff & Digest Overhaul
- Added full session handoff pipeline with handoff_ready flag and context injection (#9325)
- Added cross-mode session handoff from terminal to web chat (#9482)
- Added web chat resume from terminal/autonomous sessions (#9445)
- Overhauled session digest pipeline with turn-by-turn records (#9378)
- Wired generate_session_boundary_summaries for session transitions (#9392)
- Added digest support for plan mode turns (#9481)
- Fixed digest capture of interrupted turns on next stop event (#9487)
- Fixed handoff source detection to use event source instead of rule-based variable (#9488, #9489)
- Restructured handoff prompts with real git data and proper word budgets (#9372, #9373)
- Renamed summary files to `{seq_num}-full.md` / `{seq_num}-compact.md` (#9374)
- Added max_age_minutes guard to find_parent() for handoff sessions (#9387)
- Fixed fallback to transcript when digest_markdown is empty for compact handoff (#9519)

#### Stop-Gate & Tool Error Recovery
- Promoted Tier 1 stop-gate rules to hardcoded engine plumbing (#9324)
- Added block agent stop after tool errors as built-in plumbing (#9311)
- Added consecutive tool block detection in rule engine (#9312)
- Added tool error recovery rule and wired hooks into spawned agents (#9298)
- Hardened tool error recovery from inject_context to stop block (#9301)
- Scoped consecutive block counter to same tool to prevent death spiral (#9319)
- Fixed stop-blocking after sibling tool errors with catastrophic failure bypass (#9318)
- Auto-cleared tool_block_pending on successful after_tool (#9302)
- Fixed fire stop rules for killed agent sessions (#9450)

#### Legacy Workflow System Removal
- Pruned legacy step/lifecycle workflow system (#9507)
- Removed dead WorkflowEngine and migrated observers to rule engine (#9435)
- Deleted dead workflow action modules and relocated survivors (#9504)
- Removed dead ActionHandler wrappers from workflow actions (#9499)
- Purged deprecated digest system and fixed memory-recall-on-prompt (#9402)
- Removed legacy transcript-based handoff path (#9404)

### Web UI

- Added smart tool call headers with contextual summaries (#9359)
- Added line numbers on Write blocks and unified diff on Edit blocks (#9356)
- Improved chat UI tool result rendering with better formatting (#9530)
- Fixed tool results not displaying in web UI (#9354)
- Fixed tool chain groups and tool call details expand by default (#9348, #9349)
- Fixed base64 images in tool results rendering as raw text (#9277)
- Fixed thinking blocks disappearing after streaming completes (#9282)
- Added mobile chat drawer improvements with session status dots (#9346)
- Combined MobileChatDrawer and SessionStatusBar on mobile (#9344)
- Added two-row mobile terminal toolbar layout (#9314)
- Added Shift+Tab/Shift+Enter to mobile toolbar with Ctrl+C/Ctrl+D labels (#9313)
- Added mobile terminal delete button, VIEW/EDIT badges, and ConfirmDialog (#9306)
- Fixed mobile terminal viewport height and keyboard autofill bar (#9308)
- Fixed mobile chat drawer expanding by default (#9347)
- Fixed plan approval modal flash and mode dial lag from artifact panel (#9345)
- Fixed project picker dropdown clipping on mobile — aligned right (#9205)
- Added clickable and dismissable agent sessions in sidebar (#9219)
- Added sidebar parity across Workflows page (#9309)
- Added Escape key handler to SidebarPanel (#9310)
- Added workflows UI bug fixes and improvements (#9327)
- Fixed workflows/pipeline editor crashes on dict-form invoke_pipeline (#9341)
- Aligned pipeline card buttons with agents/rules pattern (#9379)
- Replaced pipeline create dropdown with direct sidebar open (#9401)
- Replaced window.confirm with ConfirmDialog component (#9479)
- Fixed agent editor sidebar — YAML empty, extends dropped, skills not saved (#9350)
- Fixed context usage pie chart reporting inflated percentage on tool-heavy turns (#9275)
- Added DELETE endpoint for UI settings (#9238)
- Fixed template rules not visible in UI (#9264)
- Loaded agent definition as single source of truth in web chat (#9328)
- Made inject_context templates work with session data (#9331)
- Added colon-triggered inline autocomplete for slash commands (#9265)
- Fixed web UI slash command bugs (#9239)
- Fixed stale chat content persisting after session deleted (#9210)
- Fixed chat mode bleeding between sessions (#9221)
- Fixed plan/act mode desync on WebSocket connect race (#9269)
- Prevented mode reset on SDK session ID adoption in web chat (#9508)
- Fixed terminal session observation rendering raw JSON (#9248)
- Restored missing CSS from index.css monolith decomposition (#9261)
- Fixed terminal shortcut keys bar CSS — co-located styles (#9204)
- Removed mode indicator from status bar and mobile drawer (#9511)
- Filtered extends dropdown to only installed and enabled agents
- Updated session start display with cleaner layout and agent metadata (#9360)
- Showed injected skill names in session start output (#9290)

### Improvements

- Synthesized session title from first user prompt (#9501)
- Increased session title synthesis timeout from 10s to 30s (#9528)
- Added web chat / CLI hook parity: webhooks, broadcasting, inter-session messages (#9300)
- Added workflows reinstall CLI and auto-enable gobby-tagged templates (#9369)
- Removed TTS (ElevenLabs) while keeping STT (Whisper) (#9323)
- Separated TTS and STT enablement in voice config (#9303)
- Fixed STT voice flow — VAD thresholds, stuck state, error feedback (#9338)
- Moved TTS to frontend and capitalized API key config names (#9254)
- Combined discovery and discovering-tools into progressive-discovery skill (#9296)
- Added wait_for_command MCP tool and command-listener pipeline (#9245)
- Wired tool caching into MCP manager and status endpoint (#9289)
- Slimmed list_mcp_servers output and required category/validation_criteria on create_task (#9292, #9293, #9294)
- Injected default agent context into session start response (#9268)
- Enabled pipeline workflows for agent spawning and fixed pipeline-worker (#9268)
- Fixed /skills Run Skill — injected context via additionalContext (#9249)
- Scoped worker-safety rules to spawned agents only (#9383)
- Added is_spawned_agent guards to messaging rules (#9485)
- Denied ExitPlanMode when no plan file exists (#9271)
- Exempted plan mode markdown from require-task-before-edit (#9376)
- Propagated template changes to installed copies for agents and workflows (#9416)
- Added 'Rule enforced by Gobby' prefix to hardcoded auto-blocks (#9340)
- Removed redundant awaiting_tool_use rule (#9332)
- Fixed YAML folding artefact in command-mcp-tool-restriction that caused unconditional blocking (#9486)
- Graceful Neo4j/Docker handling on daemon startup (#9291)
- Fixed CLI Neo4j status sourced from daemon API instead of broken config (#9299)
- Fixed watchdog health check to use lightweight /api/admin/health endpoint (#9258)
- Route standardization, WebSocket broadcasting, and refresh button removal (#9218)
- Fixed stale CLI endpoint URLs missing /api/ prefix (#9266)
- Overrode VIRTUAL_ENV to empty in tmux -e flags (#9410)
- Prevented committing-changes skill from overriding agent close procedures (#9428)
- Made delete_worktree idempotent for pipeline cleanup resilience (#9429)
- Fixed pipeline step condition evaluation — stripped ${{ }} wrapper (#9256)
- Fixed pipeline reliability for task orchestration (#9380)
- Stopped marking every session as handoff_ready on exit (#9382)
- Persisted observer variable changes to DB and guarded create_task claim detection
- Preserved task claim variables across session compaction (#9502)
- Fixed auto-compaction context survival for autonomous and web chat (#9448)
- Fixed compact_markdown from containing summary content when LLM omits section break marker (#9490)
- Collected mcp_call effects even when override decisions apply (#9406)
- Fixed context-handoff rules not firing on /clear (#9304)
- Extended handoff parent backoff to 6m polling for /clear and /compact (#9484)
- Wired _dispatch_boundary_summaries and added backoff for /clear handoff race (#9483)
- Fixed int task_ids in task_tree_complete() stop-gate condition (#9453)
- Added BEFORE_AGENT to piggyback events and improved message formatting (#9222)
- Fixed context window percentage and removed dead conductor module (#9240)
- Fixed LiteLLM error leaking into web chat (#9297)
- Allowed developer and QA agents to create bug tasks for triaging
- Updated rollup 4.57.1 to 4.59.0 — fixed GHSA-mw96-cpmx-2vgc (#9261)

### Bug Fixes

- Fixed 47 failing tests and 3 errors across 10 root causes (#9342)
- Fixed 33 failing tests and 25 E2E errors on 0.2.23 branch (#9531)
- Resolved 15 pytest failures and 5 mypy errors (#9365)
- Added category field to all create_task calls in e2e tests (#9343)
- Resolved all mypy --strict errors in agent_spawn.py (#9322)
- Resolved 5 mypy errors across 4 files (#9491)
- Fixed missing PromptLoader._get_manager() method (#9523)
- Replaced nonexistent SessionSource.EMBEDDED with AUTONOMOUS_SDK (#9467)
- Fixed agent rule_definitions filtered by empty _active_rule_names and added IfExp to safe evaluator (#9407)
- Removed dangling ActionContext imports and stale WorkflowEngine docstrings (#9451)
- Removed stale WorkflowState references from test_tasks_coverage.py (#9518)
- Fixed prompt_text injection that silently broke session handoffs (#9357, #9362)
- Resolved mypy type errors in _handoff.py (#9384)
- Stripped trailing newlines from agent tree text fields (#9371)
- Fixed HeadlessResult/HeadlessSpawner imports in integration tests (#9459)
- Fixed CodeRabbit findings across multiple batches (#9234, #9285, #9288, #9405, #9512)
- Fixed worktree hook install to use project mode
- Added force: true to coordinator cleanup_worktree step (#9421)
- Handled dict-form invoke_pipeline in renderer and executor (#9358)
- Fixed missing pipeline validation in runner.prepare_run (#9268)

### Refactoring

- Decomposed websocket/chat.py into focused modules (#9526)
- Decomposed admin.py routes monolith into package (#9522)
- Decomposed storage/skills.py monolith into package (#9521)
- Decomposed _activate_default_agent into focused helpers (#9498)
- Moved description and task into agent tree block (#9367)

### Testing

- Raised CLI module test coverage to 80%+ across all 7 targets (#9493)

### Documentation

- Added unified SDK agent execution and session blob storage plans (#9375)
- Rewrote pipeline guide and created issues audit (#9364)
- Documented automatic context injection in send_message tool (#9243)

## [0.2.22]

### Major Features

#### CLI Session Observation & Bidirectional Messaging
- Added Phase 1 read-only session observation from CLI to Web UI (#9152)
- Added Phase 2 CLI sessions in chat sidebar with bidirectional messaging (#9161)
- Added terminal session filtering to show only active tmux panes using process liveness checks (#9194)
- Fixed terminal session observation, sidebar scroll expansion, and missing sessions (#9192, #9174, #9177)
- Hid internal cliSessions from chat sidebar Terminal Sessions list (#9226)

#### Agent System v3
- Removed SkillProfile, added variables sync, and created dedicated agents skill (#9085)
- Added web chat agent selection with scope-aware picker modal (#9187)
- Converted agent edit form to sliding sidebar panel with improved UX (#9184)
- Improved Agent Editor Form with 7 UX fixes including 60vh scrollable detail, Pydantic defaults, and variables/rules editor rendering (#9182, #9183)
- Added Launch Agent from Task Panel (#9176)
- Unified agent mode/isolation/provider/base_branch defaults to "inherit" (#9191)
- Fixed agent picker modal imports, deduplication, mobile layout, and icon sizing (#9195)
- Fixed agent rules selector and agent card UX (#9188)

#### Rule Engine Consolidation
- Supported multiple effects per rule and consolidated 36 rules into 8 (#9158)
- Consolidated variable stores: removed step_variables, used session_variables exclusively (#9233)
- Revamped rule templates: renamed auto_task_ref, deprecated stale rules, tuned stop gates
- Dropped underscore prefix from session variable names in templates (#9155)
- Moved initialize-session-defaults rule to deprecated (#9178)
- Renamed require-read-mail to notify-unread-mail, switching from block to inject_context
- Removed deprecated rule/skill templates and cleaned up stale files
- Added Jinja template support in require-server-listed-for-schema reason field (#9173)

#### Plan Mode Improvements
- Fixed plan mode stuck after ExitPlanMode timeout or UI toggle (#9143)
- Fixed plan mode detection false positive from conversation history (#9132)
- Fixed plan mode bash filter false positive on stderr redirection (#9118)
- Fixed plan file path not tracked with regex and fallback (#9120)
- Fixed ExitPlanMode fallback to fail closed instead of defaulting to approve
- Instructed agent to call ExitPlanMode when plan is complete (#9116)
- Fixed plan approval not prompting agent to proceed (#9172)

#### Canvas & Artifacts
- Fixed 6 A2UI canvas implementation bugs (#9110)
- Fixed canvas render_surface MCP proxy context threading (#9121)
- Added remaining canvas frontend files including panel, tests, skill, and hooks
- Fixed artifact panel mobile layout with full-width overlay on screens under 768px (#9162)

#### Skills System
- Completed skills template-to-installed pattern (#9100)
- Added skill filtering at serve time and updated default agent (#9085)
- Threaded database connection to recommend_skills_for_task for DB-backed skill loading (#9104)
- Fixed skill auto-injection pipeline and wired up default web agent (#9115)
- Fixed skill browser sending wrong command prefix (#9198)

### Web UI

- Decomposed index.css monolith into 16 component-scoped CSS files (#9190)
- Organized web/src/components/ into feature directories (#9230)
- Replaced flat slash command popup with modal browsers for skills and workflows (#9114)
- Sorted slash commands alphabetically in popup (#9122)
- Fixed slash command palette not scrolling on arrow key navigation (#9148)
- Added /restart slash command to web chat (#9145)
- Added tool call groupings in chat view (#9144)
- Loaded all session messages at once, removed Load More button (#9229)
- Loaded chat messages from DB on initial mount (#9150)
- Replaced RuleCreateModal with YAML editor for new rules (#9224)
- Replaced header status badge with arrow icon on mobile (#9201)
- Fixed mobile drawer on Chat and Sessions pages (#9199)
- Fixed CSS cascade and textarea height on mobile (#9199)
- Fixed terminal viewer not filling full height on mobile (#9199)
- Made mobile terminal drawer match chat UI style (#9200)
- Fixed missing 'No conversations' empty state CSS (#9202)
- Added missing agent-defs-btn base CSS styles (#9223)
- Added desktop CSS for terminal content container (#9227)
- Fixed UI bugs: z-index stacking, branch selector, chat deletion (#9165, #9166, #9167)
- Switched source filter to Installed after Install All (#9181)
- Aligned SkillsPage with workflows UI, added filter chips, removed Overview cards
- Added light theme color for templates overview card (#9105)
- Reset all chat state when deleting the active conversation (#9149)
- Prevented 3D knowledge graph crash on iOS/mobile (#9154)

### Improvements

- Consolidated handoff tools to set_handoff_context and get_handoff_context (#9112)
- Added get_inter_session_messages tool for P2P message visibility (#9108)
- Added require-read-mail rule to enforce mid-turn message awareness (#9107)
- Added multi-variable file support with Variables tab and consolidated YAML (#9175)
- Persisted chat mode to DB for session reconnection (#9131, #9146)
- Persisted selected project ID to config_store (#9134)
- Persisted user settings (font, model, theme) to config_store (#9133)
- Deprecated localStorage chat message storage in favor of DB (#9138)
- Removed localStorage cache for project selection (#9159)
- Pulled Gemini and Codex model lists from litellm (#9189)
- Used multi-step exploration heuristic in memory nudge (#9099)
- Normalized secret names to lowercase for case-insensitive upsert (#9163)
- Removed dead handoff action handlers (#9147)
- Updated agents SKILL.md with comprehensive gobby-agents tool docs (#9113)

### Bug Fixes

- Fixed block-stop-after-tool-block false positive causing API waste (#9164)
- Fixed _dispatch_mcp_calls silent drop when no event loop (#9186)
- Fixed 4 error log bugs degrading daemon functionality (#9179)
- Fixed rules YAML save, soft-delete blocking create, and workflow_type mutation (#9103)
- Fixed get_session_messages not resolving session references (#9109)
- Fixed require-read-mail review issues (#9111)
- Fixed memory-review-gate re-trigger on close_task (#9098)
- Fixed title synthesis race condition by sending db_session_id via WebSocket
- Fixed WebSocket errors and validation failures on page load (#9196)
- Fixed mypy type-arg errors in rule_engine, canvas, and chat_session_permissions (#9160)

### Code Quality

- Hardened E2E test isolation with HOME override, service paths, and leak detection (#9066)
- Set up vitest config and test script for web/ (#9151)
- Fixed stale test expectations in test_agent_definitions_v2.py (#9197)
- Fixed test_auto_task_rules.py to match non-deprecated rules (#9185)
- Fixed test failures from agent definition changes (#9123)
- Fixed uninstall tests to use fake home instead of real Path.home() (#9101)

### Documentation

- Added frontend design standard and style guide (#9153)
- Added 0.2.21 changelog with release notes (#9231)

## [0.2.21]

### Major Features

#### Declarative Rules Engine

- Build `RuleEngine` with single-pass evaluation loop (#8806)
- Add `RuleEvent`, `RuleEffect`, `RuleDefinitionBody` models (#8804)
- Add `rule_overrides` migration and rule-specific query helpers (#8805)
- Wire dual evaluation into `WorkflowHookHandler` (#8807)
- Extend bundled sync to handle rule YAML format (#8808)
- Create rule YAML files: session-defaults (#8809), worker-safety (#8810), tool-hygiene (#8811), plan-mode (#8812), progressive-discovery (#8813), task-enforcement (#8814), stop-gates (#8815), memory-lifecycle (#8817), context-handoff (#8818), auto-task (#8819)
- Create MCP rule tools: list, get, toggle, create, delete (#8820)
- Create HTTP API routes for rules (#8821)
- Add Rules tab with tabbed layout to WorkflowsPage (#8822)
- Create CLI rules commands with TDD tests (#8823)
- Wire `RuleEngine` into hook evaluation pipeline (#8946)
- Simplify Workflows tabs: Pipelines | Agents | Rules (#8947)
- Unify Workflow UI: consistent tabs with flat cards and full CRUD (#8949)
- Rules enforcement: global toggle, bundled exclusion, backend APIs (#8958)
- Template/installed/project source taxonomy (#8962, #8963)
- Use `build_condition_helpers()` in `RuleEngine._evaluate_condition` (#9007)
- Implement `observe` rule effect and migrate observations to session variables (#9014)
- Implement `is_plan_file()` for require-task-before-edit rule (#9003)
- Chat UI rules engine parity with Claude Code CLI (#9076)
- Add brief mode to `list_rules` MCP tool (#9074)

#### Agent System Overhaul

- Add `AgentDefinitionBody` model and `agent_scope` to `RuleDefinitionBody` (#8833)
- Create agent-specific rule YAML files with `agent_scope` filtering (#8834)
- Simplify `spawn_agent` with `workflow_definitions` agent lookup (#8835)
- Migrate `agent_definitions` table to `workflow_definitions` (#8836)
- Add P2P message columns and `agent_commands` table migration (#8828)
- Build `AgentCommandManager` and update `inter_session_messages` (#8829)
- Rewrite `agent_messaging.py` with P2P messaging and command tools (#8830)
- Create `messaging.yaml` push delivery rules (#8831)
- WebSocket broadcast events for agent messaging (#8832)
- Rename 'generic' agent definition to 'default' (#8974)
- Evolve `AgentDefinitionBody` schema with `AgentWorkflows` container (#8982)
- Consolidate `spawn_agent` factory and delete `_v2.py` (#9023)
- Fresh agent YAMLs, simplify sync, remove old definitions (#9023)
- Delete `AgentDefinition` model and all conversion code (#9023)
- Add agent definition CRUD tools to `gobby-agents` MCP server (#9024)
- Agent registry refactor, voice config, UI improvements, and test fixes

#### Plugin System Removal

- Remove plugin system: `plugins.py`, `PluginsConfig`, plugin routes (#8827)
- Remove `rules` table, `RuleStore`, `rule_sync.py`, `_resolve_check_rules` (#8826)
- Remove `lifecycle_evaluator.py` and simplify `WorkflowHookHandler` (#8825)
- Delete `session-lifecycle.yaml` and `auto-task.yaml` (#8824)
- Remove plugins CLI command group (#9080)
- Remove legacy `PluginsCard` from Dashboard UI (#9042)

#### Authentication

- Add basic auth system for web UI
- Fix auth password storage bug and add login UI (#9055)

### Web UI

- Unify Workflow UI: consistent tabs with flat cards and full CRUD (#8949)
- Bring Agent & Rule cards to feature parity with Pipeline cards (#8951)
- Source filter dropdown + bundled-to-template rename in frontend (#8962)
- Fix web chat showing different conversations on different devices (#8959)
- Adopt SDK `session_id` as `external_id` for web chat sessions (#8964)
- Move branch indicator to `ChatInput` toolbar and show branches (#8916)
- Make branch indicator always visible via eager API fetch (#8915)
- Match terminal top bar to chat UI top bar size and fonts (#8931)
- Clean up Memory page: remove overview cards, importance dropdown, add 24H chip (#8930)
- Code-split web UI: lazy-load pages and vendor chunk splitting (#9033)
- Fix Memory tab crash: add error boundaries and async WebGL error handling (#9032)
- Remove WHEN/EFFECT blocks from Rules cards + font consistency (#9035)
- Fix chat UI: stable buttons, markdown artifact editing, plan approval in panel (#9046)
- Fix horizontal scrollbar in chat message list (#9047)
- Fix `ExitPlanMode` timeout to fail-closed and fix `useChat` defaults (#9049)
- Fix `PlanApprovalBar` never showing due to `isStreaming` guard (#9048)
- Fix plan approval collision, artifact edit button, memory review gate, `/plan` command (#9050)
- Fix context pie chart showing 0% for missing SDK usage data (#9045)
- Add delete confirmation to Skills UI (#9041)
- Fix Drawbridge UI: agent cards, task totals, chat refresh, sidebar consistency (#9005)
- Fix redraw button CSS and add redraw functionality (#9012)
- Unified filter button with popover for Workflows page (#9078)
- Add "Hide Installed" toggle for Templates source filter (#9081)
- Fix agent card padding and enable delete for installed agents (#9096)
- Fix pipeline dropdown rendering in wrong DOM location (#9073)
- Extract tab components, add devMode Install, restore deleted templates (#8995)
- Fix rule detail/YAML/toggle for template rules (#8993)
- Fix Install button UX, watchdog restart loop, remove agent source chips (#9069, #9070)
- Bundled items as hidden templates with dev mode CRUD (#8955)
- Fix use-as-template: unique index prevented bundled+custom coexistence (#8956)

### Improvements

- Config-driven LLM provider/model selection for 4 callsites (#9044)
- Expose memory/task/session internal actions as MCP tools (#8816)
- Unify tool normalization across all CLI adapters (#9077)
- Add `skill-scanner` as required dependency and fix scanner wrapper (#9043)
- Add Whisper custom vocabulary (#9056)
- Fix session title synthesis: add `digest_and_synthesize` MCP tool + `mcp_call` dispatch (#8976)
- Fix GitHub tab to use app-level project selection (#8975)
- Prefer installed over template in rule lookups (#8993)
- Fix `install_from_template` preserving enabled state + rename from `use_as_template` (#8991)
- Fix template leaking through `get_by_name` + bare variable false positives (#8987)
- Prepend rule name to hook error messages (#8984)
- Fix bare name references in rule conditions (#8953)
- Fix rule engine variable persistence across evaluations (#8953)
- Fix stop-gate infinite loop: remove `before_agent` reset of `stop_attempts` (#8952)
- Reset `stop_attempts` on any tool call, not just native (#9075)
- Fix `require-uv` rule: extract command from `tool_input` fallback (#9004)
- Remove `plan_mode` guard from require-task-before-edit rule (#9020)
- Fix `SESSION_START` rules storing variables under `external_id` (#9001)
- Fix rule `mcp_call` effects lost in `WorkflowHookHandler.handle()` (#8994)
- Fix stop hook fails to block when agent has claimed task (#9072)
- Fix stop hook false positive when no task exists (#8978)
- Fix `close_task` failing to clear `task_claimed` session variables (#9064)
- Fix reversed dependencies in `create_task` `blocks`/`depends_on` (#8929)
- Fix `search_memories` crash on stale index references (#9038)
- Fix plan mode flow, sentence collision, task-close loop, and title length (#9037)
- Fix memory-review-gate never clears after `create_memory` (#9082)
- Fix missing type parameters for generic dict in `chat.py` (#9065)
- Fix wrong import path for `get_gobby_home` in `cli/auth.py` (#9058)
- Fix MCP tool discovery + add Whisper custom vocabulary (#9056)
- Replace bare `except: pass` with `logger.debug` across codebase
- Fix optional chaining for d3 forces, missing deps in hooks
- Fix slow daemon startup, broken playwright MCP, and RuntimeWarning spam

### Code Quality

- Decompose `spawn_agent.py` into package: 1016 lines to 5 files, max 540 lines (#8900)
- Decompose `runner.py`: extract broadcasting and maintenance modules (#8901)
- Extract `SessionControlMixin` from `chat.py`: 1153 to 774 lines (#8902)
- Decompose `install.py`: extract setup + deduplicate output: 1114 to 942 lines (#8903)
- Remove dead code: `block_tools`, `track_schema_lookup`, `track_discovery_step` (#8980)
- Remove dead code: `check_rules` and `on_error` fields (#8996)
- Restructure bundled rules/workflows into directories with deprecation support
- Workflows-V2 code review fixes (#8965)
- Add tests for MCP `call_tool` argument unwrapping in rule engine (#8985)

### Testing

- Fix 140 test failures in `tests/workflows/` (#9057)
- Fix pytest failures, errors, and warnings (#9068)
- Fix E2E tests touching production Qdrant and port mismatch (#9079)
- Fix stale E2E tests referencing removed servers and tools (#9083, #9084)
- Fix uninstall tests to use fake home instead of real `Path.home()` (#9101)
- Fix 8 mypy type errors across 5 files (#9019)
- Fix E2E test project ID leaking into production daemon (#9021)
- Triage stale tasks: close 14 already_implemented/duplicate tasks (#9008)

### Documentation

- Add `workflow-rules.md` authoring guide with variable safety section (#8981)
- Rewrite workflows and workflow-actions guides for rules model (#8838)
- Update agents, orchestration, and mcp-tools guides (#8839)
- Update docs, skills, and architecture for rules-first model (#8840)
- Document templates vs active enforcement (#9052)
- Add A2UI canvas platform plan (#8892)

## [0.2.20]

### Major Features

#### Global Hooks by Default

- Install hooks globally (`~/.gobby/hooks/`) by default instead of per-project (#8417)
- Consolidate per-CLI hook dispatchers into a single shared dispatcher
- Add project-hook cleanup to gemini, cursor, and windsurf installers (#8736)
- Prevent duplicate hook firing when installed globally and per-project (#8734)
- Add `-C/--path` option to init, install, and uninstall CLI commands (#8871)

#### Setup Wizard Rewrite

- Rewrite setup wizard in Ink (React CLI) with interactive prompts (#8749)
- Add Neo4j setup wizard step and daemon secret migration (#8765)
- Fix 5 setup wizard quality issues (#8855)

#### Session Intelligence

- Add rolling conversation digest for session intelligence (#8760)
- Add always-visible session status bar to chat UI (#8759)
- Fix terminal session title display using tmux_pane instead of parent_pid (#8865)

#### Tri-State Plan Mode

- Implement tri-state plan mode with configurable enforcement (#8743)
- Consolidate `plan_mode` boolean into `mode_level` numeric variable (#8755)
- Make chat mode selector per-conversation, default to Plan (#8867)
- Default backend chat mode to plan instead of bypass (#8868)
- Add configurable messages to enforcement actions and extract agent prompts (#8747)

#### Configuration Overhaul

- Replace `config.yaml` with `bootstrap.yaml` for runtime config (#8874)
- Extract MCP instructions into bundled prompt file (#8746)
- Move qdrant default path to `~/.gobby/services/qdrant/` (#8415)

### Features

- Add `gobby sync` CLI with git integrity verification for bundled content (#8767)
- Pipeline editor form replaces Workflow Builder (#8783)
- Move project selector to global header bar (#8766, #8768)
- Move mode selector between context pie and project selector (#8750, #8769)
- Session detail rewrite to match chat page look/feel (#8774)
- Unify sidebar and status bar backgrounds to `--bg-tertiary` (#8777)
- Remove session number suffix from Gobby label in chat messages (#8776)
- Harden A2UI canvas plan with CodeRabbit security recommendations (#8790)
- Document `-C/--path` option in README, CLI guide, and CONTRIBUTING (#8873)
- Add `GOBBY_HOOKS_DIR` env var to uninstall command (#8875)
- Convert absolute symlinks to relative for cross-machine compatibility (#8414)
- Soft-delete for agent and workflow definitions (#8793)
- Inter-agent messaging overhaul plan (#8794)
- Wrap sync `session_manager.get()` calls with `asyncio.to_thread` in async pipeline tools (#8675)
- Add 311 tests for routes and voice modules (#8859)
- Add tests for worktree/clone parity functions (#8888)

### Bug Fixes

- Fix individual uninstallers deleting shared global `hook_dispatcher.py` (#8889)
- Fix uninstall tests deleting real `~/.gobby/hooks/hook_dispatcher.py` (#8881)
- Fix wrong `hooks_dir` in windsurf global uninstall mode (#8850)
- Fix context pie chart: exclude output tokens from percentage (#8876)
- Fix `task_tree_complete()` chicken-and-egg deadlock in auto-task workflow (#8800)
- Fix 3 failing tests: cli_init output format, agents include_deleted, chat_session env (#8802)
- Fix pre-commit mypy to use `uv run` with full project env (#8869)
- Fix sentence collision in web chat after tool calls (#8785)
- Fix tool call argument and result JSON rendering (#8781)
- Syntax-highlight JSON in tool result fallback (#8780)
- Fix memory injection staleness + add Neo4j graph-augmented search (#8775)
- Fix plan/expand skills to preserve plan content during expansion (#8799)
- Fix `memories_processed` count to use `len(memories)` (#8678)
- Fix researcher `on_mcp_error` masking `send_to_parent` failure (#8677)
- Fix default limit mismatch in memory recall wrapper (#8852)
- Fix falsy-value bug in `sdk_compat.py` or-chain (#8851)
- Guard ToolTable against empty tools array (#8849)
- Remove dead `GOBBY_HOME` assignment and inline Path import (#8848)
- Fix flaky timing-dependent test in `test_pipeline_background_cleanup.py` (#8693)
- Fix invisible hover color on light themes in `chat/styles.css` (#8690)
- Fix single error state overwritten by multiple fetchers in `useSourceControl` (#8688)
- Add accessibility attributes to MobileSessionDrawer header toggle (#8687)
- Add AbortController to `useTerminal` agent fetch on unmount (#8686)
- Add AbortController unmount guard to `useDashboard` refresh (#8692)
- Add 'ready' to SegmentKey type in `TasksCard.tsx` (#8684)
- Include exception details in `worktrees.py` ValueError catch (#8683)
- Add timeout to `proc.wait()` in `_check_tmux_session_alive` (#8682)
- Track fire-and-forget health check tasks in `spawn_agent.py` (#8681)
- Add debug log for silent except in session_coordinator tmux cleanup (#8680)
- Add debug log for silently dropped audio chunks in `voice.py` (#8679)
- Pin `@playwright/mcp` to v0.0.68 in default MCP server config (#8676)
- Revert `@playwright/mcp` pin back to `@latest` (#8801)
- Increase watchdog startup health check timeout from 13s to 55s (#8798)
- Increase daemon startup health check timeout to 120s (#8732)
- Fix unawaited `VectorStore.count` coroutine in `get_stats` (#8733)
- Resolve migration 110 FK constraint and daemon health check timeout (#8413)
- Fix 27 mypy errors across 10 files (#8797)
- Fix `mode_level` comparison: wrap in `bool()` to satisfy mypy no-any-return (#8751)
- Remove duplicate session context injection from `on_session_start` (#8752)
- Sort slash commands alphabetically in chat UI (#8726)
- Derive session ref from session data as fallback (#8759)
- Fix 3 plan issues in `a2ui-canvas.md` (#8853)
- Fix test markers and tautological assertions (#8854)
- Fix 9 bugs from CodeRabbit report triage (#8842)
- Fix 6 Python code quality issues from CodeRabbit triage (#8856)
- Fix 7 frontend quality issues from CodeRabbit triage (#8857)
- Add missing `pytest.mark.asyncio` to neo4j/graph test files (#8843)
- Use `step.id` instead of array index as React key in PipelineEditor (#8844)
- Batch Neo4j per-entity MENTIONED_IN queries with UNWIND (#8845)
- Replace `execSync` with async exec in `Bootstrap.tsx` (#8847)
- Convert `findRepos()` from sync to async fs operations (#8846)

### Refactoring

- Remove gobby-orchestration, add worktree/clone parity (#8882)
- Extract SDK lifecycle into dedicated workflow (#8756)
- Decompose `chat_session.py` into helpers and permissions modules (#8788)
- Memoize MessageItem and ToolCallCards to fix input lag (#8782)
- Convert f-string logger calls to lazy `%s` formatting (#8858)
- Narrow bare excepts and add debug logging in `_resolve_and_set_project_context` (#8674)
- Bulk-load task metadata in `import_from_jsonl` to eliminate N+1 queries
- Rate limit handling, DigestConfig, and title refresh (#8787)
- Remove `.gemini/settings.json.example` (deprecated hook configurations)
- Move completed `orchestrator-refactor.md` plan to `docs/plans/completed/`

## [0.2.19]

### Major Features

#### Chat v2 Promoted to Primary Chat

Replaced the original chat UI with a fully redesigned experience built on the Claude Agent SDK.

- Promote Chat v2 to primary Chat tab, remove old Chat tab (#8440, #8456)
- Markdown rendering with `@tailwindcss/typography` and custom prose styles (#8425, #8426, #8427)
- Syntax highlighting and line numbers in tool call results (#8434)
- Streaming caret appears inline with text (#8428, #8429, #8430)
- Auto-synthesize chat title from first message (#8433)
- Inline session title rename via double-click (#8662)
- Show session ref in chat message header (#8664)
- Display Write/Edit tool call content with syntax highlighting (#8537)
- Split assistant text segments at tool call boundaries (#8595)
- Tool call cards display when `tool_status` arrives before assistant message (#8538)
- Filter system turns from title synthesis and improve auto-scroll (#8512)
- Simplify chat sidebar to stay open like sessions sidebar (#8716)
- Remove Recent CLI Sessions section from chat sidebar (#8717)

#### Chat Modes and Context Management

- Chat mode selector: Plan / Act / Full Auto (#8636, #8541)
- Convert mode selector to segmented button group (#8638)
- Act mode uses `accept_edits` behavior (#8637)
- Context usage indicator with pie chart (#8631)
- `/compact` slash command for context management (#8631)
- Fix context usage tracking — replace tokens per turn instead of accumulating (#8699, #8661)
- Cap conversation history to fit Claude's 10K `additionalContext` limit (#8634)
- Bump `additionalContext` limit from 9500 to 9950 (#8635)

#### Voice Chat Overhaul

- Replace push-to-talk with continuous listening via client-side VAD using `@ricky0123/vad-web` (#8437)
- Pause VAD while TTS is playing to prevent echo feedback loop (#8442)
- Debounce VAD resume after TTS playback (#8666)
- Decouple ElevenLabs TTS from text stream to fix sputtering (#8435)
- Switch to `eleven_flash_v2_5` model with auth headers for WebSocket streaming (#8444)
- Configurable `voice_settings` via VoiceConfig (speed, similarity, style) (#8449)
- Reduce VAD sensitivity to avoid false triggers (#8447)
- Serve VAD/ONNX assets locally, fall back to CDN for WASM (#8439, #8441)

#### Source Control / GitHub Tab

- New GitHub tab with source control UI — branches, PRs, worktrees, clones, CI/CD (#8422)
- Extract source-control CSS and fix Tailwind config (#8512)
- Source control component quality improvements (#8592, #8561, #8568)

#### Project-Agnostic Daemon

- Make daemon project-agnostic with per-session context resolution (#8601)
- Propagate `project_id` from web UI project selector to MCP context (#8488)
- Wire frontend project selector to send `set_project` WebSocket message (#8571, #8570)
- Inject GOBBY_SESSION_ID env var into web chat CLI subprocess (#8555)
- Inject session identity into web chat system prompt (#8572)
- Set cwd on web chat lifecycle HookEvent (#8557)

#### Pipeline System Improvements

- Make `session_id` required on `run_pipeline`, derive `project_id` from session (#8509)
- Pipeline-spawned agents default to headless mode (#8505)
- Thread `parent_session_id` through pipeline spawn steps (#8498, #8500)
- Forward `session_id` from `expose_as_tool` pipelines (#8500)
- Merge pipeline default inputs with caller-provided inputs (#8503)
- Pipeline error handler persists step failure to DB (#8502)
- Allow `cancel_run` for pending agents (#8507)
- Wire WebSocket events and tool proxy for lazily-created pipeline executors (#8607)
- Initialize pipeline executor and git manager at daemon startup (#8627)
- Always register `gobby-pipelines` and `gobby-clones` MCP servers at startup (#8630)

### Features

- Static model picker with Claude Opus/Sonnet/Haiku (#8655)
- Use shorthand model names and default to opus (#8551)
- Replace deprecated Claude 4.5 model IDs with 4.6 equivalents (#8534)
- Theme selector in settings — dark / light / system (#8647)
- Show 'session ended' overlay when tmux session dies (#8604)
- Terminal list trash button, PID in top bar (#8646)
- Show synthesized session title for terminals (#8654)
- Improve terminal default display names (#8653)
- Pin toggle to chats sidebar for desktop view (#8715)
- Continue CLI session in web chat UI (#8657)
- Mobile chat drawer for conversation sidebar (#8497)
- Mobile min-height for terminals and drawer navigation for sidebars (#8499)
- Move active agents below chats with terminal navigation (#8494)
- YAML editor modal and reorganize workflow card buttons (#8431)
- Configurable display limits for memory and knowledge graphs (#8485)
- `rebuild_crossrefs` and `rebuild_knowledge_graph` MCP tools (#8471)
- `rebuild-crossrefs` and `rebuild-graph` CLI commands (#8474)
- POST `/memories/graph/rebuild` for bulk KG extraction (#8470)
- Basic model-specific agent definitions (claude-cli-haiku/sonnet/opus, gemini-cli, researcher) (#8626)
- Configure VS Code terminal title during `gobby install` (#8420)
- Extract `_apply_debug_echo` helper and extend to all hook handlers (#8545)
- Migration 111 for `cfg__` secret name refactor (#8658)
- `add_total_input_tokens` to chat_session.py DoneEvent (#8701)

### Bug Fixes

- Handle unknown message types from Claude Agent SDK (`MessageParseError` on `rate_limit_event`) (#8450)
- Soft-delete chat sessions to avoid FK constraint failures (#8549)
- Add `mcp_servers={}` to internal SDK calls to prevent Canva MCP bleed (#8548)
- Block native Claude Code task/todo tools in SDK web UI sessions (#8649)
- Tool approval rejection returns `PermissionResultDeny` instead of Allow (#8530)
- Plan mode edit exemption only applies to plan files (#8633)
- Re-add TodoWrite to session-lifecycle `block_tools` (#8632)
- Wire `require_task_before_edit` into `block_tools` when clause (#8540)
- Pass `None` instead of empty dict for env in `ChatSession.start()` (#8712)
- Context usage pie chart showing 0% due to prompt caching (#8673)
- Agent result capture returns empty despite successful completion (#8629)
- Create workflow state when `claim_task` finds no existing row (#8656)
- Memory review gate self-clears to prevent infinite stop loop (#8643)
- Drop `cfg__` prefix from secret naming convention (#8641)
- TTS silent failure and config cleanup (#8640)
- Agent status display and pipeline executor wiring (#8631)
- Kill agent handles already-dead agents gracefully (#8628, #8608)
- Deliver spawn prompt to workflow templates for pipeline agents (#8694)
- Pass `session_id` to `build_cli_command` for Claude terminal agents (#8665)
- Isolation_ctx variable scoping in `spawn_agent` (#8660)
- Populate context usage tokens in web chat DoneEvent (#8639)
- `generate_text` raises on failure instead of returning error strings (#8438)
- `make_parent_session_id` optional for pipeline-spawned agents (#8506)
- Count tool calls and turns for terminal-spawned agents (#8504)
- Terminal-spawned agent lifecycle tracking (#8481)
- Create `agent_runs` DB record for terminal/embedded/headless spawns (#8457)
- Persist `agent_run_id` in terminal spawn path (#8473)
- Wire `llm_service`/`embed_fn` into MemoryManager and fix graph display (#8458)
- Migrate memory IDs from `mm-` prefix to UUID5 (#8459)
- Import synced memories before exporting on daemon startup (#8452)
- Don't pass `source_session_id` during memory import (#8454)
- Replace dagre+SVG with `react-force-graph-2d` in MemoryGraph (#8493)
- Batch `fetchall` in memory ID migration to prevent OOM (#8614)
- Restore terminal display of session ID on SessionStart (#8556)
- Hide `_global` project from UI and project selectors (#8423)
- Resolve default project for GitHub tab (#8424)
- Keep terminal sidebar pinned open on desktop (#8650)
- Restore Agent Terminals header in sidebar (#8648)
- Terminal titles in sidebar, title+ID in top bar (#8580)
- Use sans-serif font in session and nav sidebars (#8451)
- Center empty state text on sessions and terminals pages
- React hooks above early return in ArtifactSheetView (#8531)
- Rename `copyPaths` to `copyDirs` in worktree template (#8532)
- Pass `db` to WorkflowLoader in `create_workflows_registry` (#8535)
- Remove `gobby-task-sync` pre-commit hook to prevent infinite loop (#8596)
- Make `clear_secret` fully atomic in config_store (#8613)
- Add shutdown cleanup for background pipeline tasks (#8611)
- Add `-L gobby` socket flag to tmux health check (#8600)
- Update pipeline MCP tests to use `session_id` instead of `project_id` (#8610)
- Resolve 31 test failures and 16 PytestWarnings (#8668)
- Address 8 confirmed CodeRabbit findings from PR #204 (#8671)
- 6 deferred CodeRabbit findings (memory, pipeline, config, spawn, sdk_compat) (#8581)
- 9 critical CodeRabbit fixes + deferred task triage (#8579)
- 10 high-priority CodeRabbit findings (#8563)
- ~40 CodeRabbit findings across backend, frontend, config, and docs (#8472)
- Multiple frontend quality batches: accessibility, ARIA, error handling, sanitization (#8589, #8590, #8591, #8592)
- Frontend source control, React quality, chat improvements (#8558-#8562, #8567-#8568)
- Backend data integrity, exception narrowing, logging (#8559, #8558, #8624)

### Refactoring

- Delete `headless-lifecycle.yaml` and remove deprecated `discover_lifecycle_workflows` (#8574)
- Delete `detection_helpers.py` and inline into observers/engine/actions (#8468)
- Remove deprecated `require_active_task` action (#8542)
- Remove `spawn_session` dead code from pipeline system (#8511)
- Consolidate `delete_worktree` git deletion branching (#8615)
- Replace `public self.db` alias with property in SessionManager (#8612)
- Rename `chat-v2` to `chat` directory (#8456)
- Replace hardcoded colors with CSS variables (#8463)
- Remove unused `type: ignore` comment in sdk_compat.py (#8702)

### Performance

- Move tmux health check to background task (#8617)

### Documentation

- Harden a2ui-canvas.md design doc (12 CodeRabbit findings) (#8618)
- Add setup context to `mcp_call_tool` examples in Gemini workflow (#8623)
- Gemini worktree skill security warning and template path clarification (#8594)

### Tests

- Fix `test_spawn.py` mock asserts on correct method (#8582)
- Convert `@patch` decorator to context manager in test_config.py (#8616)
- Rename `discover_lifecycle_workflows` to `discover_workflows` in tests (#8651)
- Update test expectations for model assertions (#8543, #8547)
- Replace `MemorySyncConfig` with `MemoryBackupConfig` in tests (#8552)
- Remove `require_task_before_edit` and `protected_tools` from WorkflowConfig tests (#8554)
- Update `generate_text` tests to expect RuntimeError (#8553)
- Move inline uuid imports to module level in test files (#8467)

---

## [0.2.18]

### Breaking Changes

- **Database migration baseline raised to v107.** The minimum supported database version is now v107 (`_MIN_MIGRATION_VERSION = 107`). Databases created before v107 will raise `MigrationUnsupportedError` on startup. Recovery: backup `~/.gobby/gobby-hub.db`, delete it, and restart the daemon to recreate from the current baseline schema.

### Features

- **CRUD MCP tools for workflow/pipeline definitions** — `create_workflow`, `update_workflow`, `delete_workflow`, `export_workflow` tools with Pydantic validation, bundled-definition protection, and loader cache invalidation (#8402)
- **`get_pipeline` tool** with type filtering to retrieve pipeline details including steps, inputs, and outputs (#8404)
- **Pipeline CRUD wrappers** — `create_pipeline`, `update_pipeline`, `delete_pipeline`, `export_pipeline` with type-guarded routing so pipeline tools reject workflow definitions and vice versa (#8404)

### Bug Fixes

- Add type filtering to pipeline CRUD wrappers to prevent incorrect cross-type results (#8405)
- Fix chat UI issues — conversation titles, delete confirmation, `/clear` command, and config migration (#8406)
- Fix CLI workflow issues — variable commands, inspect output, manage commands, and test coverage gaps (#8407)
- Deduplicate memory entries in `.gobby/memories.jsonl` to reduce noisy recall (#8409)
- Add `httpx.RequestError` handler in workflow check command for consistent error reporting (#8409)
- Close DB connection in `_reset_state_manager_for_tests` before nulling globals (#8409)
- Standardize pipeline error returns to always include `success: false` flag (#8409)
- Narrow exception handling in workflow definition creation from broad `Exception` to `yaml.YAMLError` and `ValueError`/`TypeError` (#8409)
- Only include `session_id` in `deleteConversation` WebSocket payload when defined (#8409)

### Improvements

- Null-safe audit log rendering when `entry.result` is None
- Specific exception handlers in workflow CLI and pipeline modules
- Cache DB/state manager instances in workflow CLI helpers
- Pydantic v2 migration: `.dict()` → `.model_dump()` in workflow inspect
- Accept `.yml` extension for workflow imports
- Tighter daemon process detection in workflow reload
- Informative error message when daemon unreachable during reload
- Cache `value.lower()` in variable type parsing
- Wrap blocking DB calls with `asyncio.to_thread` in pipeline gatekeeper
- Preserve negative exit codes in pipeline exec steps
- Wrap blocking `spawn_agent` with `asyncio.to_thread` in pipeline handlers
- Expand sensitive variable filtering patterns in pipeline renderer
- Add `VoiceTranscriptionMessage` type with runtime validation in web chat

### Documentation

- Update workflow, pipeline, and Lobster migration guides to match current codebase (#8408)

### Tests

- Add 33-test suite for workflow/pipeline CRUD tools covering create, update, delete, export, type filtering, and error paths (#8402)

---

## [0.2.17] - 2026-02-15

### Features

#### Database-Backed Prompt Storage

Migrated prompt templates from filesystem to SQLite database with three-tier scope precedence (project > global > bundled).

- `prompts` table with scope-based precedence (migration 106) (#8352)
- `LocalPromptManager` with CRUD operations and bundled read-only enforcement (#8352)
- `sync_bundled_prompts()` for daemon startup sync from `.md` files (#8352)
- `PromptLoader` refactored to use database as sole runtime source (#8352)
- All 13 consumer files updated to pass `db` to `PromptLoader` (#8352)
- Configuration API routes updated for DB-backed list/detail/override/revert (#8352)
- Export/import updated to read/write from database (#8352)
- Filesystem prompt copying removed from installer (#8352)
- Shared `is_dev_mode()` utility added (#8352)

### Bug Fixes

- **pytest**: Update test expectations for database-backed prompt storage migration (#8353)
- **mypy**: Add None guards and return type annotations in prompt storage and loader (#8354)

### Documentation

- Update `__init__.py` docstring to match pyproject.toml description (#8351)

### Chores

- Bump version to 0.2.17 (#8350)

---

## [0.2.16] - 2026-02-15

### Major Features

#### Memory v5: Qdrant Vector Store + Knowledge Graph (Mem0 Removal)

Complete replacement of the Mem0 dependency with a native Qdrant-based vector store and LLM-powered knowledge graph pipeline.

- VectorStore class wrapping qdrant-client, replacing mem0ai dependency (#8240, #8241)
- Qdrant config fields in MemoryConfig, legacy mem0 fields removed (#8242, #8266)
- MemoryManager rewritten to use VectorStore with renamed methods matching MCP tools (#8244-#8246)
- VectorStore initialized in runner.py (#8247)
- Migration 103 to drop memory_embeddings table (#8248)
- Migration 104 to drop mem0_id column from memories (#8267)
- Removed decay_memories, dropped mem0_client from get_stats, added vector_count (#8249)
- Deleted legacy memory search files and cleaned up imports (#8250)
- generate_json() added to LLM providers for structured output (#8251)
- LLM prompt templates: fact extraction, dedup decision, entity extraction, relationship extraction, delete relations (#8252, #8253, #8257, #8258, #8259)
- Feature configs for memory LLM calls (#8254)
- DedupService for LLM-based memory deduplication, wired as fire-and-forget (#8255, #8256)
- KnowledgeGraphService with entity/relationship extraction (#8261)
- KnowledgeGraphService wired into MemoryManager (#8262)
- Write convenience methods added to Neo4jClient (#8260)
- search_knowledge_graph MCP tool replaces export_memory_graph (#8263)
- Standalone docker-compose.neo4j.yml and installer (#8264)
- CLI: --mem0 replaced with --neo4j (#8265)
- Removed importance field from memory extraction and cleaned up memory-v5 vestiges (#8243, #8284)
- Cleaned up all legacy mem0 imports, comments, and tests (#8268, #8301)

#### Visual Workflow Builder (Web UI)

New drag-and-drop workflow editor built with @xyflow/react.

- workflow_definitions table + bundled YAML import (migration 102) (#8188)
- LocalWorkflowDefinitionManager storage manager (#8189)
- DB-first lookup in WorkflowLoader + Runner wiring (#8190)
- HTTP API routes for workflow definitions CRUD (#8191)
- useWorkflows React data hook (#8192)
- WorkflowsPage list view component + CSS, wired into App.tsx workflows tab (#8193, #8194)
- DB query for list_workflows with filesystem fallback (#8195)
- Workflow templates API for New button (#8196)
- WorkflowBuilder canvas scaffold with toolbar and palette sidebar (#8199)
- Dagre auto-layout and definitionToFlow/flowToDefinition serialization (#8200)
- Shared node types registry and StepNode component (#8201)
- WorkflowPropertyPanel with dynamic form routing (#8205)
- CodeMirror ExpressionEditor for workflow expressions (#8208)
- Edge styling, workflow settings modal, variables/rules/exit condition editors (#8209)
- Save/load cycle for visual workflow builder (#8209)
- Distinct visual node components for workflow builder (#8283)

### Improvements

- background:true workflow action dispatch + transcript title re-synthesis (#8300)
- regex_search Jinja2 filter for MCP output extraction (#8293)
- Mobile chat drawer replacing sidebar on mobile (#8278)
- Click-outside-to-close for ConversationPicker sidebar (#8278)
- Rate limit backoff for embeddings service (#8299)
- License updated to Apache-2.0 in pyproject.toml (#8346)

### Bug Fixes

- Configure D3 force simulation to prevent KG node clumping (#8303)
- Fall back to neo4j_client for graph reads when kg_service unavailable (#8302)
- Add set-titles-string to user session tmux rename (#8297)
- Update tests for async memory manager migration (#8294)
- Web chat title synthesis + add delete chat (#8289)
- Use get_default_provider().generate_text() in pipeline prompt steps (#8290)
- Serialize CallToolResult in pipeline MCP steps (#8288)
- Wire template_engine and tool_proxy_getter into PipelineExecutor (#8287)
- Auto-task workflow ends when session_task is complete (#8280)
- Load session messages from API when switching conversations (#8281)
- Show resume placeholder when switching to sessions without local messages (#8279)
- Update MemoryConfig tests for removed legacy attributes (#8277)
- Keep conversation picker toggle visible on mobile (#8276)
- Use db_session_id in web chat lifecycle events (#8274)
- Drain stale SDK response events after chat interrupt (#8273)
- Lazy-load KnowledgeGraph to prevent crash over HTTP (#8271)
- Allow all hosts in Vite when bound to 0.0.0.0 (#8270)
- Make Vite dev server host configurable for Tailscale access (#8269)
- Reduce flakiness in background action error test (#8342)

### Documentation

- Database-backed prompt storage plan (#8304)

---

## [0.2.15] - 2026-02-15

### Major Features

#### Unified Workflow Engine

- Observer engine with YAML-declared observers and behavior registry (#8000-#8003)
- Built-in behaviors: task_claim_tracking, detect_plan_mode, mcp_call_tracking (#8001, #8002)
- Plugin support for custom observer behaviors (#8005)
- Unified evaluator: single evaluation loop replacing fragmented evaluators (#8084, #8085)
- Named rule definitions with RuleStore (three-tier CRUD, bundled sync, import loading) (#7992-#7995)
- Multi-workflow support: WorkflowInstanceManager, concurrent instances per session (#8078-#8080)
- Session variables: shared state visible across all workflow instances (#8081)
- Scoped variable MCP tools (get/set) (#8088)
- tool_rules field on WorkflowDefinition with lifecycle evaluation (#7998)
- Unified workflow format: lifecycle + step YAMLs migrated to single format (#8092, #8093)
- enabled/priority fields replace type field for workflow distinction (#8082, #8093)
- exit_when shorthand and expression-based exit conditions (#8006, #8007)
- SafeExpressionEvaluator replacing eval() in ConditionEvaluator (#7989, #7990)
- check_rules resolution logic in workflow engine (#7996)
- build_condition_helpers for SafeExpressionEvaluator (#7989)

#### Artifact System Removal

- Complete removal of artifact references from workflows, sessions, LLM, config, web UI, tests, and docs (#8046-#8054)
- Migration to drop artifact tables and update baseline schema (#8052)
- Remove ArtifactHandoffConfig (#8048)
- Remove artifact registration and wiring (#8047)

#### Strangler Fig Decomposition (Wave 3)

- workflow/loader.py → loader_cache.py, loader_discovery.py, loader_validation.py, loader_sync.py (#8155-#8158)
- workflow/engine.py → engine_models.py, engine_context.py, engine_transitions.py, engine_activation.py (#8163-#8166)
- memory/manager.py → services/embeddings.py, services/mem0_sync.py, services/graph.py, services/maintenance.py (#8159-#8162)
- cli/installers/shared.py → installers/mcp_config.py, skill_install.py, ide_config.py (#8167-#8169)
- runner.py → runner_models.py, runner_tracking.py, runner_queries.py (#8170-#8172)
- Remove re-exports from loader.py, engine.py, shared.py, runner.py — canonical imports only (#8175-#8179)

#### Config System

- DB-first config resolution — store config in SQLite instead of YAML (#8098)
- $secret:NAME config pattern for secrets-store-only resolution (#8071)
- Secrets store priority: secrets store first, env vars as fallback (#8070)
- Config write isolation + lightweight health endpoint (#8072)
- gobby-config MCP server for agent config access (#8141)

#### Terminal + Tmux

- Consolidated terminal spawners to tmux-only (#8063)
- Tmux window rename after title synthesis (#8114)
- TmuxPaneMonitor for detecting dead panes (#8062)
- Permanent set-titles-string and IDE terminal title auto-config (#8122)
- Show terminal title instead of tmux pane ID (#8149)
- Terminal rename via double-click (#8027)

### Improvements

- Async Mem0 queueing with background sync (#8145)
- Configurable mem0 client timeout, increased default to 90s (#8105, #8107)
- Skill usage tracking in get_skill() MCP handler (#8121)
- Skills-used tracking in session stats (#7982)
- Provider-dependent model selection in agent definitions (#7973)
- Running agents tracking on agents page (#7971)
- spawn_session and activate_workflow pipeline step types (#8091)
- result_variable and failure handling for run_pipeline action (#8090)
- Task status approved → review_approved + Gantt scheduling fields (#8135)
- Web UI accessible over Tailscale (#8212)
- Auto-start Vite dev server on daemon startup (#8133)
- File editor: save/cancel with confirmation, undo/redo (#8025)
- Agent definition editing from UI (#8028)
- Needs Review + In Review overview cards for tasks and memory (#8029, #8060)
- Standardized sidebar widths via CSS variable (#8020)
- Consolidate hook integration call sites (#8086)
- Standardize args → arguments in MCP layer (#8056)
- Remove hardcoded model aliases and resolve_model_id (#8185)
- Remove redundant context injection (progressive discovery + blind memory inject) (#8123)
- Replace skill list injection with discovery guide skill (#8118)
- Multi-workflow MCP activate/end workflow support (#8087)
- Workflow status query updated for multi-workflow (#8089)
- Session variables override priority in lifecycle evaluator (#8217)
- Merge session_variables into lifecycle evaluator context (#8214)
- Recently Done filter applies 24h cutoff (#8019)
- Remove approved from Recently Done — not a completed status (#8059)
- Improve scope toggle contrast and readability (#8021)
- Knowledge graph idle animation with manual camera rotation (#7976)

### Bug Fixes

- Baseline schema out of sync with BASELINE_VERSION + migration guard rejects valid DBs (#8137)
- Phase 2 config reload drops YAML settings + e2e test env leak (#8136)
- Port isolation for e2e tests + daemon startup cleanup (#8125)
- Revert _clear_port() — kills production daemon during e2e tests (#8131)
- Mypy arg-type errors in runner.py + suppress bandit B105 (#8138)
- Pre-push scripts missing -c pyproject.toml for bandit (#8139)
- Stale model default in TaskValidationConfig (#8144)
- Mem0 Docker setup — correct image, env vars, and secret injection (#8095, #8096)
- Resolve API keys from config before os.environ fallback (#8075)
- YAML observer type coercion, cache BehaviorRegistry, stale comments (#8065)
- Voice UI availability gating + config save persistence (#8064)
- AST evaluator compatibility for lifecycle YAML expressions (#7991)
- Guard against push-to-talk EOF error on short recordings (#8148)
- Show mem0 status in gobby start/restart output (#8130)
- Enable set-titles so tmux window rename propagates to terminal emulator (#8117)
- Replace curl healthcheck with python urllib in mem0 docker-compose (#8116)
- Reduce daemon log noise from five recurring error/warning sources (#8109)
- Memory page 2D graph flash and animation label disappearance (#8097)
- Prevent animation toggle from rebuilding graph nodes (#8099)
- Support space syntax for /gobby skill routing (#8069)
- Worktree test patches and memory session_id FK constraint (#8058, #8061)
- Eliminate all type: ignore comments across codebase (#8237)
- Align 37 failing tests with workflow enabled/type refactor (#8234)
- Use Protocol to properly type loader_sync.py mixin (#8236)
- Remove bad opus model alias and strip CLAUDECODE env var (#8183)
- Resolve 8 mypy errors across 4 files (#8129)
- Multiple rounds of CodeRabbit review fixes (#8101, #8104, #8110, #8112, #8113, #8215, #8216, #8220, #8221, #8225)

### Security

- Ignore CVE-2025-69872 in pip-audit (diskcache, no fix available) (#8181)

### Documentation

- Memory v5 plan (#8219)
- Strangler fig decomposition wave 2 plan (#8152)
- Workflow UI plan review and update (#8154)
- Party system v2 design + plan cleanup (#8150)
- Unified workflow architecture plan with implementation phases (#8066, #8067)
- Remove artifact references from documentation (#8054)
- Remove meeseeks reference from workflows skill (#8057)

---

## [0.2.14] - 2026-02-11

### Major Features

#### Web UI — Tasks Page

- Kanban board with 6-column status mapping, drag-and-drop between columns, swimlanes, priority board (#7548, #7551, #7573, #7572, #7574, #7553, #7552)
- Task tree view with react-arborist, search filtering, expand/collapse controls, drag-and-drop re-parenting (#7555, #7557, #7558, #7593)
- Dependency graph visualization using SVG and dagre layout (#7595)
- Gantt chart view with timeline, dependency arrows, drag-to-reschedule (#7575, #7576)
- Task detail slide-in panel with metadata, status actions, dependencies, validation (#7541-#7544)
- Task creation form with context-aware defaults, quick capture via Cmd+K (#7545, #7547, #7546)
- Assignee management with picker and joint ownership (#7589)
- Threaded comments with @mentions (#7590)
- Task handoff flow between humans and agents (#7592)
- Per-task cost/token tracking, result area, linked memories (#7577, #7580, #7581)
- Oversight mode selector, escalation/de-escalation views (#7568-#7571)
- Reasoning timeline, action feed, raw trace view, session transcript viewer (#7564-#7567)
- Activity pulse indicator, risk badges, capability scope, audit log (#7563, #7585, #7586, #7587)
- Overview cards, status strip, WebSocket event subscription (#7560-#7562)
- Role-based default views, per-task permission overrides (#7591, #7588)
- Task clone capability (#7583)
- Shared TaskBadges components (#7540)

#### Web UI — Memory Page

- Memory page with table, filters, form, detail components (#7481-#7486)
- Knowledge graph visualization — Neo4j 3D force-directed graph (#7952, #7955)
- Memory correction UI with inline edit and pin toggle (#7582)
- Learning-from-feedback confirmation preview (#7584)
- Graph view redesign with grid layout for disconnected nodes (#7680, #7682)
- Reorder memory views — knowledge first, list last (#7954)

#### Web UI — Sessions & Chat

- Session lineage tree view (#7457)
- On-demand AI summary generation (#7456)
- Rich session transcript component (#7455)
- Claude SDK web chat backend (#7304, #7305)
- Thinking indicator and slash commands (#7313)
- Mid-conversation model switching + dynamic model list (#7315)
- AskUserQuestion interactive UI with selection/submit (#7331, #7332, #7366-#7372)
- Bidirectional voice chat (#7961)
- Chat page split from sessions (#7493)
- Session list sidebar (#7440)

#### Web UI — Other Pages

- Cron Jobs page with two-panel layout (#7634-#7638)
- Configuration page with secrets, prompts, raw YAML (#7985)
- Skills page with CRUD, hub browsing, safety scanning (#7984)
- Unified Projects page replacing Files + Coming Soon (#7983)
- DB-backed agent registry + configuration catalog UI (#7959)
- Dashboard menu item, sidebar navigation, hamburger menu (#7406, #7408, #7437, #7500-#7502)
- File browser/viewer/editor (#7415)
- Tmux terminal session management with PTY relay (#7357)

#### Mem0 Integration

- Async Mem0 REST client (#7468)
- Docker-compose bundle for mem0 services (#7470)
- CLI install/uninstall --mem0 commands (#7471)
- Mem0 lifecycle utilities (#7473)
- Dual-mode MemoryManager operation (#7474)
- --mem0 flag for daemon commands (#7475)
- Remove old Mem0Backend, OpenMemory backend, SQLite backend in favor of StorageAdapter (#7476-#7478)

#### Memory Enhancements

- Memory embedding persistence layer and table migration (#7462, #7464)
- Hook embedding generation into CRUD lifecycle (#7466)
- Wire UnifiedSearcher into SearchCoordinator (#7465)
- Configurable search_backend options (#7463)
- Embedding reindex CLI command (#7467)
- Automated memory capture & retrieval in lifecycle workflows (#7962)

#### Cron Scheduler

- Storage foundation and config (#7618-#7624)
- Scheduler engine with executor and runner integration (#7625-#7627)
- CLI, HTTP, and MCP interfaces (#7628-#7633)

#### Coordinator Pipeline & Orchestration

- Coordinator pipeline + developer/QA step workflows (#7412)
- Dry-run parameter for orchestrate_ready_tasks (#7393, #7394)
- Sequential and parallel orchestrator integration tests (#7390, #7391)
- Failure scenario tests (#7392)
- Atomic slot reservation and list updates (#7382, #7383)
- Cleanup_environment for partial failure recovery (#7380)
- Restore original branch on merge failure (#7379)
- Configurable stuck_timeout workflow variable (#7387)
- Pre-register agent in RunningAgentRegistry before spawn (#7386)

#### Agent & Workflow Enhancements

- DB-backed agent registry with prompt fields and YAML export (#7959, #8008)
- Automatic interactive/autonomous mode via tmux focus (#7685)
- Auto terminal detection prefers tmux when installed (#7353)
- Tmux promoted to first-class agent spawning module (#7350)
- Skill slash command system rework (#7318)
- Generic command_pattern matching in block_tools + require_uv enforcement (#7314)
- Agent-type-aware skill discovery and injection (#7613-#7616)
- Headless lifecycle for web UI chat agent (#7507)
- Gobby-plugins internal MCP server (#7454)
- Personal workspace fallbacks + project filter for tasks (#7445)
- Skill profile replaced with typed SkillProfileConfig model (#7701)

#### Artifact System Enhancements (deprecated in 0.2.15)

- ArtifactsPage with sidebar and detail layout (#7610)
- Artifact type icons and badge styling (#7611)
- useArtifacts React hook (#7609)
- REST API router for artifacts (#7608)
- Write MCP tools, tag CRUD, title/task_id columns (#7597-#7605)
- Enhanced auto-capture with task inference and title generation (#7605)
- Export artifact CLI command (#7607)
- Diff and plan artifact types in classifier (#7604)

#### Code Decomposition (Strangler Fig Round 2)

- websocket.py to websocket/ package (auth, chat, handlers, broadcast) (#7100-#7104)
- claude.py to claude_models.py, claude_cli.py, claude_streaming.py (#7096-#7099)
- skills.py to metadata.py, scaffold.py, formatting.py (#7091-#7093)
- sessions.py to session_models.py, session_resolution.py, session_lifecycle.py (#7094-#7096)
- hook_manager.py to factory.py, session_lookup.py, event_enrichment.py (#7105-#7107)
- Orchestration tools extracted to standalone gobby-orchestration server (#7354)
- Standardize server to server_name across MCP proxy layer (#7355)

### Improvements

- Session activity stats: commits, tasks, memories, artifacts (#7661)
- Improve compact & session-end summary prompts (#7987)
- Coerce string booleans in set_variable (#7965)
- Plan mode detection moved from engine to YAML workflow actions (#7347)
- Replace memory extraction gate with soft suggestion (#7438, #7439)
- Lazy-loaded MCP servers show as pending instead of disconnected (#7302, #7303)
- Skip context injection on /resume (#7310)
- Use project root as cwd for ChatSession in dev mode (#7309)
- Simplify task statuses from 8 to 6 (#7674)
- Reset had_edits after close_task with linked commit (#7907)
- Remove old discovering-tools skill (#7654)
- Add orphan cleanup to spawn_ui_server (#7652)
- Use relative URLs in web UI for Tailscale remote access (#7651)
- Kanban column renames: Done to Approved, Closed consolidation (#7645-#7649)
- Remove dead GitHub/Linear MCP tool wrappers (#7352)
- Add GitHub/Linear/playwright as default proxied MCP servers (#7410)
- SDK-first fallback for session summaries (#7957)
- Paginated embeddings and migration transactions (#7770-#7773)
- Animate knowledge graph when idle (#7976)

### Bug Fixes

- Resolve 60+ pytest failures and 90+ static analysis issues (ruff, mypy, bandit, tsc) across multiple stabilization passes (#7656, #7658, #7679, #8010, #8011)
- Graph animation toggle placement and label preservation (#8023)
- Data bugs and UI polish from Drawbridge review (#8037)
- RegistryContext.resolve_project_filter references self.db instead of self.task_manager.db (#7988)
- Pass source_session_id in MCP create_memory tool (#7981)
- Resolve invalid model ID claude-haiku-4-5 for Anthropic API (#7684)
- MCP tool args not coerced to declared schema types (#7430)
- InternalToolRegistry schema generation broken by `from __future__ import annotations` (#7418)
- Prevent save_config from writing test paths to production config (#7505)
- Handle string tool_input in block_tools (#7345)
- Make migration 83 idempotent for deleted_at column (#7446)
- Keep BASELINE_VERSION at 81 for existing databases (#7442)
- Replace crypto.randomUUID() with fallback for non-secure contexts (#7307)
- 100+ web UI fixes: ARIA attributes, AbortController cleanup, keyboard navigation, error states, defensive JSON parsing, accessibility improvements

### Security

- Pin cryptography>=46.0.5 for CVE-2026-26007 (#7676)
- Nosec B104 for false-positive bandit findings (#7675)
- Resolve bandit security scan findings (#7655)
- Replace assert with runtime guards for bandit B101 (#7348)

### Documentation

- Drawbridge import enhancement plan (#8038)
- CLI auto-detection and model discovery plan (#8012)
- Workflow-engine-rules.md task mapping updates (#8009)
- Artifact system removal plan (#7960)
- Task workflows migration plan (#7416)
- Orchestration guide, replace meeseeks references (#7399, #7400)
- Guiding principles (GUIDING_PRINCIPLES.md) integrated into CLAUDE.md (#7342, #7343)
- Orchestrator production hardening plan (#7339)
- AskUserQuestion web chat plan (#7338, #7344)
- Web-chat-lifecycle plan (#7433)
- Mem0 integration guide, memory guide updates (#7488-#7491)
- Update artifacts guide (#7612)

### Testing

- Rewrite 21 test files for real coverage (78.67% to 82%) (#8040)
- Playwright E2E tests for file editor (#7429)
- Orchestration integration tests: sequential, parallel, failure scenarios (#7389-#7392)
- Extensive test fixture improvements and async mock fixes

### Internal

- Ruff format applied to 15 files
- Remove memu-py integration, switch memory backend to sqlite (#7426)
- Multiple code decomposition refactors (see Major Features)
- Task metadata syncs, dependency updates, deprecation cleanups

## [0.2.13] - 2026-02-08

### Major Features

#### Codex Adapter Enhancements

- Add approval handler to CodexAppServerClient (#6882)
- Add handle_approval_request to CodexAdapter (#6883)
- Add app-server mode routing for Codex hooks (#6885)
- Add context_prefix parameter to start_turn() (#6887)
- Extend translate_from_hook_response with context injection for Codex (#6888)

#### Web UI

- Scaffold web chat UI with React + Vite (#6776)
- Add MCP tool support to web chat + reorganize frontend (#6781)
- Add terminal panel with xterm.js (#6782)
- Add syntax highlighting for code blocks (#6783)
- Persist chat history to localStorage (#6784)
- Add settings panel with font size slider (#6779, #6780)
- Add chat interrupt and send-while-streaming (#7081)
- Add Escape key to stop streaming in chat input (#7113)
- Auto-start web UI with daemon (#7075)
- Fix SPA catch-all to exclude bare path segments (#7127)

#### Pipeline System

- Create PipelineExecutor with exec, prompt, and invoke step types (#6742, #6743, #6744, #6745)
- Add approval gate handling with approve/reject methods (#6746, #6747)
- Add pipeline CLI commands: list, show, run, status, approve, reject, history (#6754-#6758)
- Add pipeline MCP tools: run_pipeline, approve/reject, get_status (#6749-#6752)
- Add pipeline HTTP API endpoints (#6760-#6763)
- Add condition evaluation and template rendering (#6748, #6796)
- Create Lobster format importer with CLI import command (#6768-#6773)
- Support run_pipeline in workflow on_enter/on_exit and lifecycle triggers (#6766, #6767)
- Add WebSocket streaming for pipeline execution (#6798)
- Register gobby-pipelines MCP server in daemon (#6797)
- Add dynamic tool generation for expose_as_tool pipelines (#6716)
- Replace eval() with SafeExpressionEvaluator in pipeline conditions (#6795)

#### Async WorkflowLoader

- Make WorkflowLoader async with aiofiles (#7196)
- Add mtime-based cache invalidation to WorkflowLoader (#7122)

#### Shell Action for Workflows

- Add shell/run action to workflow system (#7020)
- Rename bash action to shell for cross-platform accuracy (#7041)
- Enrich shell action render context with session & project (#7166)

#### Inject Context Action

- Add multi-source inject_context action: skills, task_context, memories (#6642-#6645)
- Support array syntax for multi-source injection (#6646)

#### Handoff Improvements

- Feed structured HandoffContext into LLM summary path (#7171)
- Add get_git_diff_summary for actual code change context (#7170)
- Remove SummaryFileGenerator and add write_file support to generate_handoff (#7184)
- Enhance compact handoff with detailed activity and fixed paths (#6787)

#### Prompt Loader

- Migrate prompts from config.yaml to file-based PromptLoader (#6972)

#### Meeseeks-Box Workflow Redesign

- Redesign meeseeks-box workflow: unify merge/cleanup, remove wait_for_workers (#7269)
- Replace squash merge with regular merge to preserve worker commits (#7275)
- Restore merge_strategy variable with default 'merge' (#7276)

#### Workflow Engine Enhancements

- Add `when` conditional support to `on_enter` actions (#7261)
- Add `status_message` to WorkflowStep for user-visible transition output (#7284)
- Route workflow `call_mcp_tool` through ToolProxyService (#7279)
- Add `resume=True` to `activate_workflow` for step workflows (#6908)
- Add `internal` flag to WorkflowSpec for spawn_agent enforcement (#6908)

#### Agent/Workflow Dry-Run Evaluator

- Add dry-run evaluator for agents and workflows (#7280)

#### Transcript Capture for Non-Claude CLIs

- Add hook-based transcript assembly for Windsurf & Copilot (#7251)
- Fix session transcript capture for non-Claude CLIs (#7248)

#### Progressive Discovery Enforcement

- Enforce progressive discovery flow for MCP tool discovery (#7255)
- Merge discovering-tools skill into MCP instructions (#7254)

### Improvements

- Add pre-existing error triage enforcement to session lifecycle (#7235)
- Add CodeRabbit review report to pre-push-test.sh (#7231)
- Rename agents CLI `start` command to `spawn` (#7217)
- Add resolution_id to merge logger (#7215)
- Make meeseeks-box.yaml isolation-aware (clone, worktree, current) (#7183)
- Make Codex installer project-aware (#7182)
- Make validation max_retries configurable via config.yaml (#7180)
- Add bundled fallbacks, dev-mode symlinks, and resource export/import CLI (#7179)
- Pass model parameter through agent spawn chain (#7051)
- Add --model flag to Codex and Gemini in build_cli_command (#7163, #7067)
- Strip parent terminal env vars from spawned agent processes (#7050)
- Add orchestrator workflow enforcement to spawn_agent (#7041)
- Add project context to workflow engine for dynamic commit tags (#7045)
- Handle server_name='gobby' in tool proxy with auto-resolution (#7155)
- Deterministic auto-execution for step workflow on_enter actions (#7144)
- Increase turn limit to 100 and output token budget to 8000 (#7173)
- Update wait_for_task default timeout to 600s, poll_interval to 30s (#6917)
- Add WezTerm env vars to spawner cleanup list (#7064, #7139)
- Strengthen hook errors to be more directive and prevent LLM from stopping (#7119, #7125)
- Block AskUserQuestion when stop hook gives actionable directive (#7118)
- Add named-agent shorthand pattern to agents skill (#7124)
- Add TemplateRenderer protocol for template_engine type safety (#7157)
- Agent definition terminal supersedes caller/workflow terminal (#7065)
- Unify kill_agent for self and child termination (#6979-#6982)
- Add port validator to UIConfig (#7135)
- Verify UI server process is alive before writing PID file (#7128)
- Agent-driven proactive memory capture (#6661)
- Improve memory extraction prompt to reduce low-quality captures (#6655)
- Clean up dead memory code and enhance proactive memory (#7186)
- Add out_of_repo reason for closing tasks without commits (#6634)
- Block close_task skip_validation when commit is attached (#7062)
- Wipe closed metadata when task is reopened via update_task (#7053)
- Block update_task from closing/claiming tasks (#6825)
- Add activation gate step to work-task-gemini workflow (#6804)
- Skip memory/tool reset on Gemini auto-compress (#6730)
- Dev-mode symlinks for hook dispatcher files (#7190)
- Restore gobby agent context variables in hook dispatchers (#7188)
- Add skill hub registry integration (#6631)
- Add LLM-synthesized descriptions for GitHub collection skills (#6663)
- Remove hook-based skill injection in favor of inject_context (#6649)
- Require `changes_summary` on `close_task` for better change tracking (#7258)
- Allow `reopen_task` to work on any non-open status (#7260)
- Fix always-apply skill injection to include full content (#7245)
- Standardize MCP endpoint response format to 200 + success envelope (#7259)
- Return proper HTTP error codes (400/404/503) from MCP error responses (#7293)
- Add evaluate subcommands to agents and workflows skills (#7282)
- Generalize agent docs to reference any supported coding CLI (#7263, #7264, #7265)

### Bug Fixes

- Fix Cursor parser test to expect newline-joined content blocks (#7296)
- Fix skill_manager not wired to ActionContext in engine and lifecycle evaluator (#7238)
- Fix pre-existing test failures and lint warnings (#7233)
- Fix stale skill cache: sync_bundled_skills now updates changed skills (#7227)
- Fix agent name resolution and remove hardcoded meeseeks refs (#7225)
- Fix mypy type errors in loader.py and lifecycle_evaluator.py (#7224)
- Fix log errors in block_tools templates, artifact IDs, deprecated args, metrics (#7214)
- Fix multi-file issues: concurrency, async safety, type annotations (#7229)
- Fix ruff E402 and bandit B101 errors (#7213)
- Fix CVE-2026-0994 protobuf vulnerability (#7048)
- Fix lifecycle evaluator state race condition with atomic merge_variables (#7162)
- Fix _render_arguments to recursively render dict items in lists (#7160)
- Fix `_handle_self_mode` to allow lifecycle workflow coexistence (#7129)
- Block stop deterministically after tool block (#7131)
- Fix stop hook enforcement fail-open bugs (#7176)
- Fix lifecycle workflow appearing as step workflow in display (#7151)
- Fix streaming caret appearing on extra line break (#7120)
- Fix schema-check error to suggest correct server from unlocked_tools (#7117)
- Await cancelled chat task in disconnect cleanup (#7116)
- Fix file handle leak in spawn_ui_server (#7115)
- Guard deep attribute chain in _compose_session_response (#7083)
- Fix workflow lifecycle and task status handling (#7054, #7056, #7058, #7059)
- Fix kill_agent hook failure by adding get_tool_schema step (#7061)
- Fix out_of_repo bypassing session edit check in close_task (#7057)
- Fix spawn_agent orchestrator check reading from wrong source (#7049)
- Fix premature stop enforcement in workflow engine (#7043)
- Fix mode resolution for default workflow in spawn_agent (#7042)
- Fix on_mcp_success handlers not being processed (#6970)
- Fix workflow engine to deliver on_enter messages during step transitions (#6963)
- Fix Gemini adapter to extract inner MCP server/tool from gobby proxy calls (#6966)
- Fix clone isolation to auto-detect parent's current branch (#6959)
- Fix workflow evaluator to expose variables at top level (#6952)
- Fix workflow auto-transitions not firing after variable detection (#6951, #6936)
- Fix extra_read_paths PosixPath bug in spawn_agent (#6950)
- Fix meeseeks-box mcp_result_is_null transition bug (#6939)
- Fix session summary template rendering - use Jinja2 instead of str.format (#6947)
- Fix close_terminal not closing Gemini sessions (#6940)
- Fix sandbox to allow git operations in worktrees (#6935)
- Fix spawn_agent mode=self to set session_task variable (#6932, #6931, #6928)
- Fix assigned_task_id not persisting after workflow activation (#6808, #6810)
- Fix git_branch not passed to session when spawning workers in worktrees (#6820)
- Fix task ID comparison bugs - refs vs UUIDs (#6792)
- Fix workflow condition evaluation for YAML booleans (#6659)
- Fix PreToolUseInput validation error for before_tool_selection events (#6660)
- Fix memory deduplication bypass by removing project_id from ID generation (#6638)
- Fix memory backup export limit bug (#6635)
- Fix unlocked_tools not persisting when step workflow is active (#6829)
- Fix 7 issues: UI PID persistence, workflow YAML, CancelledError, tests (#7203)
- Fix async blocking, error handling, type hints across 7 files (#7202)
- Fix 10 test failures from async WorkflowLoader conversion (#7200)
- Fix 17 mypy type errors across 12 files (#7199)
- Fix 6 pre-existing test failures (#7197)
- Fix 12 issues across multiple files (#7177)
- Fix 6 of 8 remaining issues across multiple files (#7178)
- Fix meeseeks-box workflow engine bugs (#7240, #7241, #7242)
- Fix Gemini transcript parser crashes with 'list' object has no attribute 'get' (#7285)
- Fix on_transition placement and remove misplaced workflow refs (#7270)
- Fix default_workflow key mismatch in meeseeks agent YAMLs (#7277)
- Fix trailing newline in hook_dispatcher block reason output (#7257)
- Fix wait_for_workers to use blocking wait_for_task (#7266, #7268)
- Fix caplog propagation in state_actions warning test (#7237)
- Address 15 CodeRabbit review issues across 9 files (#7288)
- Resolve 5 mypy errors across engine.py, _session.py, and others (#7289, #7290, #7291, #7292)

### Security

- Fix 3 bandit B110 findings in dry_run.py (#7297)
- Fix CVE-2026-0994 protobuf vulnerability (#7048)
- Validate term_program before pgrep subprocess calls (#7154)
- Validate terminal context values before subprocess calls (#7153)
- Replace eval() with SafeExpressionEvaluator in pipeline conditions (#6795)
- Fix 2 bandit findings (B101, B110) (#7201)
- Add port validator to UIConfig (#7135)
- Replace hardcoded `/tmp` paths with `tempfile.gettempdir()` (#7287)

### Documentation

- Fix inconsistent agent name in meeseeks-e2e-testing.md (#7228)
- Add strangler-fig-decomposition plan for 5 oversized files (#7077)
- Update gobby-agents skill with current tools and patterns (#7046)
- Add pipeline guides and Lobster migration guide (#6772, #6773)
- Document name/variable precedence and add conflict warning (#7161)
- Update meeseeks agents and E2E testing docs (#6914-#6925)
- Fix commit prefix format in GEMINI.md and committing-changes skill (#7286)
- Update CONTRIBUTING.md to match current project state (#7278)
- Document `when` conditional actions and leaf task handling (#7262)
- Update agents skill to document internal workflow enforcement (#7281)

### Internal

- Fix CodeRabbit review suggestions across 7 batches (#7232)
- Set CodeRabbit review profile to assertive (#7230)
- Remove redundant workflow_loader guard in `_handle_self_mode` (#7226)
- Fix 41 code quality nitpicks across source, tests, and config (#7220)
- Remove deprecated parameters, legacy shims, and stale references (#7219)
- Fix stale use_semantic assertion in memory recall test (#7218)
- Fix 8 code quality issues across configs, workflows, and tests (#7216)
- Convert all hook dispatchers from blocking I/O to async (#7185, #7132, #7133)
- Code quality fixes across 14+ files (#7207)
- Add type hints, structured logging, and fix sync wrappers (#7206)
- Refactor session workflow conditional for readability (#7208)
- Multiple extraction/decomposition refactors (#7142, #7143, #7152)
- Shared mock_daemon_config fixture (#7139)
- Remove dead TodoWrite references from summary pipeline (#7168)
- Narrow except clauses in workflow parsing (#7148, #7149)
- Replace confusing self-assignment no-ops with pass (#7137, #7082)
- Clean up stale nosec bandit comments (#7073)
- Delete claiming-tasks skill and merge into MCP instructions (#7252, #7253)
- Add missing progressive discovery tools to all allowed_tools lists (#7273)
- Remove redundant blocked_tools/blocked_mcp_tools from worker workflows (#7271)
- Move spawn_agent/activate_workflow to blocked_mcp_tools (#7272)
- Remove invalid gobby-agents:get_tool_schema from shutdown allowed_mcp_tools (#7274)
- Unblock progressive discovery tools in wait_for_workers step (#7267)
- Add missing mock attributes to test_engine_extended fixture (#7256)
- Session-lifecycle cleanup and test_viz.py (#7239)

## [0.2.12] - 2025-02-05

### Major Features

#### Daemon Watchdog

- Add daemon watchdog for automatic restart on failure (#7034)

### Improvements

- Add success field to all MCP tool responses for consistent API (#7036, #7032, #7024, #7018)
- Add terminal field to AgentDefinition for per-agent terminal override (#7019)
- Configure meeseeks-claude.yaml for Claude+tmux testing (#7021)
- Clear VIRTUAL_ENV in all spawners to avoid uv warnings (#7015)

### Bug Fixes

- Fix Claude terminal spawn to use prepare_terminal_spawn (#7033)
- Fix end_workflow NOT NULL constraint - use __ended__ placeholder (#7031)
- Fix end_workflow preserving lifecycle variables (#7013)
- Fix test_search_skills_no_matches test isolation (#7026)
- Fix meeseeks E2E issues: transcript capture and VIRTUAL_ENV warning (#7012)
- Bump litellm to 1.81.7 to fix async cleanup warning

### Documentation

- Add production-ready workflows plan documentation (#7035)
- Update meeseeks-e2e-testing.md for both Gemini and Claude agents (#7023)
- Fix meeseeks.yaml docstring: worktree → clone (#7016)

### Internal

- Multiple meeseeks E2E test run merges (#7008, #7010)

## [0.2.11] - 2025-02-04

### Improvements

- Add `mode: self` to `spawn_agent` for in-session workflow activation (#6909)
- Make commit task ID format dynamic based on project name (#6904)
- Change task ID format from `[#N]` to `gobby#N` then to `gobby-#N` (#6898, #6902)

### Bug Fixes

- Fix 54 failing tests (#6912)
- Fix mypy no-any-return errors (#6910)
- Fix worktree tools: orphaned records and MCP type coercion (#6905)
- Fix transcript parsing crash when JSON line is not an object (#1)

### Documentation

- Document gobby-projects-v2 plan (#6907)
- Create gobby-plugins internal MCP server plan (#6903)

### Internal

- Improve task lifecycle tool enforcement (#6876, #6877)
- Remove `init_project` from stdio MCP server (#6897)
- Block workflow-restricted tools in MCP `call_tool` (#6896)
- Block 'review' status in `update_task` (#6875)
- Decouple gobby-skills and gobby-artifacts from task_manager (#6873)
- Exclude deprecated workflows from `gobby install` (#6911)

## [0.2.10] - 2025-02-01

### Major Features

#### Multi-CLI Adapter Support

- Add Cursor, Windsurf, and Copilot session support (#6140)
- Implement proper Copilot, Windsurf, and Cursor adapters (#6857-#6860)
- Add auto-installation for Cursor, Windsurf, Copilot (#6867)
- Add hook dispatchers for Cursor, Windsurf, Copilot
- Centralize `machine_id` in base adapter (#6842)

#### Pipeline System

- Add WebSocket streaming for pipeline execution (#6798)
- Register gobby-pipelines MCP server in daemon (#6797)
- Implement template rendering in pipeline `_render_step` (#6796)

#### Agent Improvements

- Add unified agent file with named workflows (#6847)
- Auto-detect current branch as `base_branch` (#6852)
- Load and persist inline workflows from agent definitions (#6849, #6850)
- Handle None name in inline workflow registration (#6848)
- Use `close_terminal` MCP tool for agent shutdown (#6839)
- Validate workflow exists before spawning (#6834)
- Use effective_workflow from agent definition (#6837)

### Bug Fixes

- Fix 76 mypy type errors and pytest collection issue (#6861)
- Fix 27 code quality issues from CodeRabbit review (#6856)
- Add missing `step_variables` column to test mock schema (#6854)
- Fix machine_id test mock paths and E2E workflow setup
- Resolve bandit and pip-audit security issues (#6862)
- Replace CursorAdapter stub with proper hooks implementation (#6863)
- Fix sandbox to pass `GOBBY_MACHINE_ID` env var (#6841)
- Include `~/.gobby/` in sandbox read paths for machine_id access (#6840)
- Allow Tailscale IP access in Vite config (#6843)

### Documentation

- Add Cursor, Windsurf, Copilot to CLI guides (#6868)
- Fix README CLI config to use `gobby install` (#6866)
- Update README with native CLI adapter details (#6865)
- Mark CLI adapters plan as complete (#6864)

### Internal

- Multiple test fixes for production code changes (#6869, #6871)
- Align test suite with production code changes

## [0.2.9] - 2025-01-30

### Improvements

- Skip CI on dev branch pushes - only run full CI on main and PRs (#6615)
- Add `debug_echo_context` toggle to echo additionalContext to terminal (#6606, #6607)
- Add artifact stats to `gobby status` display (#6585)
- Wire up artifact capture in hook manager (#6581)
- Wire up `gobby-artifacts` in internal registry (#1)
- Add `api_key` auth mode to `ClaudeLLMProvider` (#6568)
- Allow task management MCP calls in plan mode (#6592)
- Add trigger-based filtering to Gemini `on_pre_compact` (#6588)
- Add centralized `paths.py` utility for stable path resolution (#6598)

### Bug Fixes

- Fix hook validation order - schema check before commit check (#6614)
- Fix daemon stop/restart timeout by implementing proper shutdown (#6609)
- Fix mypy errors and test failures (#6613, #6605)
- Fix TYPE_CHECKING import and remove test_output.txt (#6610)
- Fix progressive discovery enforcement for `call_tool` (#6576)
- Fix response consistency in `get_tool_schema` (#6586)
- Fix `session_id` resolution, schema nesting, and worker model (#6578)
- Fix return value and exception handling in `_artifacts.py` (#6577)
- Fix TypeBox syntax, add security section, and fix SDK image content (#6575)
- Fix model consistency, error handling, and encoding issues (#6571)
- Fix workflow lifecycle safety and test async generator (#6570)
- Fix context accumulation, db lifecycle, and force parameter issues (#6566)
- Fix security and consistency issues in event handlers and workflow tools (#6556)
- Preserve lifecycle variables when activating step workflows (#6591)
- Fix test for `suggest_next_task` leaf task support (#6590)
- Fix type annotation inconsistency in `resolve_session_task_value` (#6595)
- Fix bandit B110 try-except-pass in `_tool.py` (#6593)
- Fix colon syntax in progressive discovery error message (#6579)
- Remove `os.environ` mutation from `_setup_litellm` (#6584)
- Fix bundled template test paths (#6616)

### Documentation

- Document lifecycle workflow blocking and hook data (#6589)
- Update artifacts docs to use `gobby-artifacts` server (#6580)

### Internal

- Remove service unpacking in HTTPServer, use ServiceContainer (#6611)
- Add `_mcp_db_manager` property and fix hook test (#6612)
- Legacy cleanup, Memory decomposition, and Runner DI
- Remove redundant context from compact handoff (#6608)
- Rename `_types.py` to `_resolution.py` in workflows (#6604)
- Add debug logging for invalid workflow files (#6603)
- Extract pre-created session and response composition helpers (#6602)
- Extract `_prepare_image_data` helper in `claude.py` (#6600)
- Extract `_format_summary_context` helper in `claude.py` (#6599)
- Refactor `end_workflow` to accept loader parameter (#6596)
- Update db docstring to match DatabaseProtocol type annotation (#6594)
- Remove `worker_model` from `auto-task-claude` workflow (#6582)
- Export `EDIT_TOOLS` from `event_handlers` package (#6569)
- Decompose `workflows.py` and `event_handlers.py` packages

## [0.2.8] - 2025-01-29

### Improvements

- Refactored Gemini spawn to use preflight+resume pattern (#6532)
- Applied sandbox config in Gemini terminal spawn (#6534)
- Added `session_ref` to `additionalContext` for hooks (#6546, #6547, #6548)
- Optimized hook metadata token usage with first-hook tracking (#6549)
- Disabled PreCompress hook handling for Gemini (#6533)
- Added database fallback to `kill_agent` for session lookup (#6455)
- `work-task-gemini` workflow now supports tasks without file edits (#6529)
- Excluded `.gobby/` files from `had_edits` tracking (#6541)
- Excluded `.gobby/` files from CI/CD and pre-commit triggers (#6543)
- Standardized `session_id` to accept `#N` format across all MCP tools (#6459)
- Refactored messaging tools to database-primary architecture (#6456)
- Added database fallback to `send_to_parent` for Gemini agents (#6451)
- CLI hooks now copied to worktrees and clones during isolation setup (#6448)
- Added `workflow_name` and `agent_depth` to session metadata cache (#6443)
- Extracted workflow activation into `_auto_activate_workflow` helper (#6441)
- Namespaced `gobby_context` key to avoid silent overwrites (#6438)
- Pass `parent_task_id` to `suggest_next_task` in find_work step (#6444)
- Updated meeseeks model to `gemini-3-pro-preview`

### Bug Fixes

- Fixed memory duplicates, NULL project_id, and JSONL export (#6553)
- Return graceful responses for hook errors instead of HTTP 500 (#6539)
- Fixed session ID vs external ID clarification in context injection (#6536)
- Fixed workflow instructions for Gemini tool calling (#6535)
- Fixed spawn_agent opening two terminal windows on macOS (#6531)
- Fixed template, error handling, and documentation issues (#6544)
- Fixed failing tests in test_cli.py and test_skill_sync.py (#6542)
- Fixed newline corruption in get_current_session references
- Fixed syntax error in session messages coverage test
- Fixed async lock, template conditionals, and workflow transitions (#6450)
- Fixed documentation drift, caching issues, and workflow config errors (#6460)
- Fixed async/blocking issues and code quality problems (#6458)
- Fixed 8 failing tests (#6464)
- Fixed session ID documentation and cleaned up legacy skills (#6478)
- Fixed Gemini spawn test env assertions for explicit validation (#6442)
- Fixed syntax errors in session CRUD tools

### Documentation

- Documented agent lifecycle and shutdown mechanisms (#6540)
- Added Codex app-server hook parity plan (#6552)
- Added personal workspace plan for project-optional tasks (#6545)
- Added remote access implementation plan (#6461)
- Added missing param docs to `register_session` docstring (#6440)

### Internal

- Multiple code quality fixes across files (#6462, #6463, #6465, #6466, #6469, #6470)
- Added pytest markers and test return type hints (#6524)
- Normalized timestamps to include timezone offsets (#6471)
- Replaced f-string logs with structured logging in hook dispatcher and workflow activation (#6439, #6447)
- Added `@pytest.mark.unit` decorator to `TestToolNameNormalization` (#6449)

## [0.2.7] - 2025-01-28

### Major Features

#### Unified Spawn Agent API

- New unified `spawn_agent` MCP tool replacing legacy spawn tools
- `IsolationHandler` abstraction with `CurrentIsolationHandler`, `WorktreeIsolationHandler`, and `CloneIsolationHandler`
- `SpawnExecutor` for unified spawn dispatch across all isolation modes
- `generic.yaml` agent and workflow definitions as reference implementations
- Deprecation warnings added to `spawn_agent_in_worktree`, `spawn_agent_in_clone`, and `start_agent`

#### Agent Sandboxing

- `SandboxConfig` models for agent spawning with network and filesystem controls
- Sandbox resolvers for CLI-specific configuration (Claude, Gemini, Codex)
- Sandbox params in `spawn_agent` MCP tool and `build_cli_command`
- `sandboxed.yaml` example agent demonstrating sandbox configuration
- Integration and unit tests for sandbox functionality

#### Project-Scoped Session References

- Database schema for project-scoped session sequential numbers
- CLI integration for displaying `#N` style session refs
- MCP tools and display updates for session refs
- Migration 77 for session seq_num backfill

#### Database Migration V2 Baseline

- Flattened baseline schema (BASELINE_SCHEMA_V2) for fresh installs
- `use_flattened_baseline` config option (defaults to True)
- `schema_dump.sql` for schema documentation
- Migration documentation updates

### Architecture Refactoring

#### Memory Module Decomposition

- New `ingestion/` package for memory ingestion pipeline
- New `services/` package with `CrossrefService`
- New `search/` package with `SearchCoordinator`
- `MemoryManager` updated as facade for extracted components

#### Session Tools Decomposition

- New `sessions/` package under `mcp_proxy/tools/`
- Extracted `_messages.py`, `_handoff.py`, `_crud.py`, `_commits.py` modules
- Created `_factory.py` for tool registry construction
- Deleted monolithic `session_messages.py`

#### Task Enforcement Decomposition

- New `enforcement/` package under `workflows/`
- Extracted `blocking.py`, `commit_policy.py`, `task_policy.py`, `handlers.py`
- Removed facade pattern in favor of direct imports

#### Codex Adapter Decomposition

- New `codex_impl/` package under `adapters/`
- Extracted `adapter.py`, `client.py`, `types.py`, `protocol.py`
- Removed `codex.py` facade

#### Other Refactoring

- Endpoints decomposition for `tools.py` (discovery, server, execution, registry)
- `ActionExecutor.register_defaults` refactored to use external handlers
- `safe_evaluator.py` module for workflow evaluation
- Extended `git_utils.py` with additional helpers
- Config module cleanup: removed deprecated prompt fields, added prompt_path pattern

### Bug Fixes

- Fixed `delete_task` recursion bug when parent task depends on children (#6366)
- Fixed config tests for `prompt_path` refactor (#6367)
- Fixed `mock_memory_manager.get_related` to use `AsyncMock` (#6368)
- Fixed mypy type errors in 4 files (#6365)
- Fixed pytest failures from config refactor and async/sync mismatches (#6362)
- Fixed task lifecycle tools returning `success: true` on errors (#6197)
- Fixed stop hook to use task ref and single newline (#6165)
- Fixed race conditions, error handling in various modules (#6326)
- Fixed import errors after enforcement refactor (#6347)

### Documentation

- Added `search.md` guide for unified search functionality (#6364)
- Added `configuration.md` guide for daemon configuration (#6363)
- Comprehensive documentation guide updates (#6350-#6361)
- Updated doctor skill with Phase 5 Security Audit section
- Created `usage` skill for token and cost reporting
- Added unified `spawn_agent` API design document
- Added sandboxing documentation
- Added migration documentation updates

### Improvements

- `/gobby` router skill and `/g` alias for quick skill access
- Skill category support (`core`, etc.) with `alwaysApply` frontmatter
- Model extraction from Claude and Gemini transcript messages
- Session message format updated to show Ref before ID
- Meeseeks-box spawn instructions include workflow activation prompt

### Internal

- Multiple code quality fixes across 50+ files
- Removed backward compatibility shims from config and spawn modules
- Cleaned up unused re-exports and deprecated parameters
- Added nosec B110 annotations for intentional exception silencing
- Reorganized plans directory and cleaned deprecated files

## [0.2.6] - 2025-01-26

### Major Features

#### Skills System (Complete Overhaul)

- New `gobby-skills` MCP server with full CRUD operations (`list_skills`, `get_skill`, `search_skills`, `install_skill`, `remove_skill`, `update_skill`)
- `SkillLoader` supporting filesystem, GitHub repos, and ZIP archive imports
- `SkillManager` coordinator with change notification pattern
- `SkillSearch` with TF-IDF backend and hybrid search (TF-IDF + embeddings)
- `EmbeddingProvider` abstraction for semantic search
- Full CLI command group (`gobby skills list/show/install/remove/update/new/validate/doc`)
- YAML frontmatter parser for SKILL.md files per [Agent Skills specification](https://agentskills.io)
- Skill directory structure support (scripts/, references/, assets/)
- Auto-sync bundled skills to database on daemon start
- Core skill injection into session-start hook
- Skill hints in hook error messages

#### Clone-Based Parallel Orchestration

- New `gobby-clones` MCP server for isolated parallel development
- `CloneGitManager` for git clone operations
- `spawn_agent_in_clone` tool for running agents in isolated clones
- `merge_clone` tool with conflict detection
- Clone storage layer with database migration
- Clone CLI commands (`gobby clones list/create/delete`)
- Updated `parallel-orchestrator` and `sequential-orchestrator` workflows

#### Conductor System (Autonomous Orchestration)

- `ConductorLoop` main daemon for autonomous task orchestration
- `TaskMonitor` for stale/blocked task detection
- `AgentWatcher` for stuck agent detection
- `AlertDispatcher` with logging and optional callme integration
- `ConductorConfig` for token budget management
- `SessionTokenTracker` with model column in sessions
- Conductor CLI commands (`gobby conductor start/stop/status`)
- Token metrics in `gobby-metrics` MCP server
- `TokenTracker` using LiteLLM pricing utilities

#### Inter-Agent Communication

- Inter-agent messaging MCP tools
- Blocking wait tools for task completion (`wait_for_task`, `wait_for_tasks`)
- `approve_and_cleanup` orchestration tool
- Handoff payload extended with `active_skills` field
- Session memory extraction workflow action

#### Task System Improvements

- Skill-based task expansion via `/gobby-expand` skill
- New expansion MCP tools replacing legacy `expand_task`
- `expansion_status` field in Task model
- `depends_on` parameter in `create_task`
- Dependency checking in `delete_task`
- Auto-claim task when creating with `session_id`
- Edit history tracking for task enforcement
- Configurable task lifecycle policies via workflow variables
- Lazy evaluation for `session_has_dirty_files` and `task_has_commits`

#### Unified Search

- Unified search abstraction with TF-IDF fallback
- Hybrid search combining TF-IDF and embeddings
- Semantic tool search via `search_tools`
- Automatic re-indexing on skill changes

### Breaking Changes

- __Default ports changed__ from 8765/8766 to 60887/60888 (GOBBY leetspeak)
- __Removed `no_commit_needed`__ escape hatch from `close_task` - commits are now required for tracked file changes
- __Legacy task expansion system removed__ - use skill-based expansion via `/gobby-expand`
- __Deprecated parameters removed__ from `SkillSearch`

### Improvements

- Progressive disclosure via MCP server instructions (`build_gobby_instructions()`)
- Standardized `HTTPException.detail` to dict format
- Auto-enrich MCP execution errors with tool schema
- Improved hook error message formatting for readability
- Block tools action to prevent Claude Code task ID collision
- Plan mode detection from UI-based activation
- Workflow shadowing visibility and error handling
- Priority display in active workflows
- Workflow activation prompt in meeseeks-box spawn instructions
- Agent spawning fixes (task_id resolution and terminal mode)
- Session ID injection into agent context
- Timestamps normalization in tasks.jsonl to RFC 3339 format
- Skill backup logic for existing installations
- CLI status output now shows Skills
- `/gobby` router skill and `/g` alias for quick access
- Top-level `alwaysApply` and `category` support in skill parser and storage

### Bug Fixes

- Fixed task_claimed state not cleared after close_task
- Fixed task_claimed incorrectly cleared on failed close_task
- Fixed premature task claim clearing
- Fixed orphaned tool_results in transcript handling
- Fixed async loop errors in skills and cleanup tests
- Fixed mypy type errors in skills module
- Fixed pytest markers, exit code assertions
- Fixed HTTP error handling, cache pricing, timeouts, and clone deletion
- Fixed MCP errors now properly signaled with isError flag
- Fixed os.close mock in test_spawn_handles_fork_error
- Fixed hook-specific output to only use valid hookEventName values
- Fixed session_id documentation in starting-sessions skill
- Fixed claim_task usage instead of update_task for claiming tasks
- Fixed indexing blocking in index_skills sync wrapper
- Fixed coroutine construction deferred to executor thread in sync search wrapper
- Fixed timezone handling and null safety in conductor monitors
- Fixed uncommitted changes check before requiring justification
- Fixed multiple caplog tests with enable_log_propagation fixture

### Documentation

- Updated ROADMAP.md with current status and planned work
- Added orchestration-v2.md with clone-based parallel agents
- Added comprehensive skills guide
- Added gobby-skills to Internal MCP Servers table
- Added Autonomous Task Orchestration section to CLAUDE.md
- Added Worktree Agent Mode section to GEMINI.md
- Created gobby-clones skill documentation
- Created gobby-merge skill documentation
- Added spawned agent protocol documentation
- Slimmed GEMINI.md and AGENTS.md to match CLAUDE.md structure
- Added skills reference folder for GitHub users
- Added PyPI installation instructions
- Updated error messages to recommend `close_task(commit_sha=...)`

### Testing

- Added E2E tests for autonomous mode and worktree merge
- Added E2E test for token budget throttling
- Added E2E test for parallel clone orchestration
- Added E2E test for sequential review loop
- Added E2E test for inter-agent messaging
- Comprehensive tests for skills install and remove commands
- Fixed 36+ failing tests across the test suite

### Internal

- Removed redundant imports and unused code
- Consolidated LLM calls through `LiteLLMExecutor`
- Changed migration log level from INFO to DEBUG
- Skip CI for docs-only changes
- Added nosec comments to suppress bandit warnings
- Multiple code quality fixes across 50+ files

## [0.2.5] - Previous Release

See git history for changes prior to 0.2.6.
