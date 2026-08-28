# Gobby MCP Tools

Reference for the MCP surface exposed by the Gobby daemon: the native proxy
tools exposed at the top level, plus the 21 internal `gobby-*` registries
reached through `call_tool`.

The exact live surface drifts as the daemon evolves. Treat
`list_mcp_servers()` and `list_tools(server_name=...)` as the source of
truth. This guide names the stable authoring and operations surface, but a
fresh discovery call always wins on disagreement.

## Tool Surfaces

There are two ways to reach a tool from an MCP client:

1. **Native tools** — exposed directly by the proxy (e.g. `call_tool`,
   `list_tools`, `recommend_tools`). Call them directly.
2. **Internal registries** — `gobby-*` servers reached through the proxy
   (`call_tool(server_name="gobby-tasks", tool_name="create_task", ...)`).

External (downstream) MCP servers — `context7`, `github`, `linear`,
`playwright`, etc. — are also reached through `call_tool`, but with their
own non-`gobby-*` names.

## Progressive Discovery Pattern

For token efficiency, discovery is context-aware. A schema lookup creates a
current-context lease for that server/tool pair:

- **Known leased tool:** call `call_tool` directly.
- **Known unleased tool:** call `get_tool_schema` directly, then `call_tool`.
- **Unknown tool name:** call `list_tools`, then fetch the selected schema and call it.
- **Unknown server or registry:** call `list_mcp_servers`.

Call each discovery step as its own native tool; do not invoke `list_tools` or
`get_tool_schema` through `call_tool`.

```python
# Known tool without a current-context lease: inspect its schema directly
get_tool_schema(server_name="gobby-tasks", tool_name="create_task")

# Execute repeatedly while the lease remains current
call_tool("gobby-tasks", "create_task", {
    "title": "Fix bug",
    "category": "code",
    "validation_criteria": "Repro test fails on main and passes on the fix branch.",
})
```

Skip eager schema loading. Loading every schema upfront wastes 30–40K
tokens that the proxy will happily deliver lazily.

Leases and inventory observations are stored in PostgreSQL session variables,
so ordinary session resume and daemon restart preserve them. Context loss from
clear, compact, or a reconstructed resume clears only schema leases; inventory
observations remain available. Fetch a tool's schema again after such a reset.

`get_skill`, `list_skills`, and `search_skills` on `gobby-skills` are bootstrap
tools and bypass the schema gate. Call them directly through `call_tool`.

---

## Native Tools

These are wired in `src/gobby/mcp_proxy/server.py` (`create_mcp_server`) and
the stdio proxy wrapper in `src/gobby/mcp_proxy/stdio.py`.

### System

#### `status()`

Get current daemon status and health information. No arguments.

#### `list_mcp_servers()`

List all configured MCP servers and their connection state. No arguments.

### Tool Proxy

#### `call_tool(server_name, tool_name, arguments?)`

Execute a tool on any connected MCP server.

| Parameter | Type | Required | Description |
| :--- | :--- | :--- | :--- |
| `server_name` | string | Yes | Server name (e.g. `gobby-tasks`, `context7`) |
| `tool_name` | string | Yes | Name of the tool to execute |
| `arguments` | object | No | Tool-specific arguments |
| `session_id` | string | No | Caller session ref for task claims, verification evidence, and other session-scoped operations. |
| `project_id` | string | No | Project name or UUID for cross-project tool calls. Local `#N` refs stay scoped to the caller project; target cross-project sessions should use UUIDs. |

Routing: `gobby-*` is handled locally by the matching internal registry;
all others are proxied to the downstream MCP server.

#### `list_tools(server_name)`

Light metadata for tools on a single known server. Use this when the tool name
is unknown or when explicitly inspecting inventory. Use `list_mcp_servers()`
only when the server or registry is unknown.

#### `get_tool_schema(server_name, tool_name)`

Full `inputSchema` for a tool. Call it directly for a known unleased tool; no
prior inventory call is required. The lookup records a current-context lease.

#### `read_mcp_resource(server_name, resource_uri)`

Proxy a resource read on a downstream MCP server.

### Server Management

#### `add_mcp_server(...)`

Add a new MCP server to the current project.

| Parameter | Type | Required | Description |
| :--- | :--- | :--- | :--- |
| `name` | string | Yes | Unique server name |
| `transport` | string | Yes | `http`, `stdio`, `websocket`, or `sse` (accepted by validation; no transport implementation yet) |
| `url` | string | For http/ws/sse | Server URL |
| `headers` | object | No | Custom HTTP headers |
| `command` | string | For stdio | Command to run |
| `args` | array | No | Command arguments |
| `env` | object | No | Environment variables |
| `enabled` | boolean | No | Whether enabled (default: true) |

#### `remove_mcp_server(name)`

Remove a configured MCP server from the current project.

#### `import_mcp_server(...)`

Import server definitions from another project, a GitHub repo, an explicit
list, or a natural-language query against the recommendation index.

#### `init_project(name, project_path?)`

Stdio proxy helper that reports project initialization must be run through
the CLI (`gobby init`). It exists so MCP clients get a structured error
instead of silently attempting unsupported daemon-side initialization.

### Discovery

#### `recommend_tools(task_description, agent_id?, search_mode?)`

Suggest tools for a task. `search_mode` accepts `llm` (default), `semantic`,
or `hybrid`.

#### `search_tools(query, top_k?, min_similarity?, server_name?)`

Semantic similarity search across the tool index.

### Session Variables

#### `set_variable(name, value, session_id)`

Set a session-scoped variable readable by hooks, rules, and workflows.

#### `get_variable(session_id, name?)`

Read one session-scoped variable, or all variables for the session when
`name` is omitted.

---

## Internal Registries

Internal tools are reached through `call_tool(server_name="gobby-*", ...)`.
There is **no separate orchestration server** — automation crosses several
registries (`gobby-tasks`, `gobby-tasks-ops`, `gobby-workflows`,
`gobby-agents`, `gobby-worktrees`, `gobby-clones`, `gobby-merge`).

| Registry | Purpose | Tools |
| :--- | :--- | :--- |
| `gobby-tasks` | Task lifecycle, dependencies, claim/close, search, backup/restore, build observability | 36 |
| `gobby-tasks-ops` | Expansion runs, artifacts, stage transitions, PR/merge state, build, GitHub | 49 |
| `gobby-plans` | Plan-Coverage Contract registry | 8 |
| `gobby-profiles` | Build profile registry | 8 |
| `gobby-sessions` | Session lifecycle, handoffs, transcripts, tmux integration | 20 |
| `gobby-memory` | Persistent memory, embeddings, knowledge graph | 19 |
| `gobby-review-learning` | Review lesson recall/record | 3 |
| `gobby-workflows` | Workflows, rules, variables, agent definitions, pipelines | 47 |
| `gobby-agents` | Agent runtime, persona, P2P messaging, commands | 19 |
| `gobby-worktrees` | Git worktree isolation lifecycle | 17 |
| `gobby-clones` | Git clone isolation lifecycle | 13 |
| `gobby-merge` | Merge resolution, conflict prediction, branch protection | 12 |
| `gobby-config` | Runtime config get/set | 7 |
| `gobby-skills` | Skill discovery, install, hubs | 12 |
| `gobby-metrics` | Tool/rule/skill metrics, usage reports | 13 |
| `gobby-cron` | Scheduled triggers | 8 |
| `gobby-hub` | Cross-project queries | 5 |
| `gobby-voice` | Whisper STT vocabulary | 4 |
| `gobby-wiki` | Wiki search, ingest, research | 12 |
| `gobby-communications` | Channels, identities, messaging, event subscriptions | 16 |

---

## Task Management (`gobby-tasks`)

36 tools for the task lifecycle.

### CRUD and Claim

| Tool | Description |
| :--- | :--- |
| `create_task` | Create a new task. `claim=true` to auto-assign. `validation_criteria` required when `category="code"`. |
| `get_task` | Get task details, including dependencies. Accepts `#N`, path, or UUID. |
| `update_task` | Update task fields, including `isolation`, `assigned_agent`, and `additional_skills`. |
| `close_task` | Evaluate and close when ready. Use `preview=true` with `commit_sha` and `changes_summary`; blocked calls stay read-only and return repair actions. Escalated tasks require `override_justification` for deliberate closure. |
| `reopen_task` | Reopen a closed or escalated task. |
| `delete_task` | Delete a task. `cascade=true` removes subtasks and dependent tasks; `unlink=true` preserves dependents by removing links. |
| `list_tasks` | List tasks with filters. |
| `claim_task` | Claim a task for the current session. `force=true` overrides another session's claim. |
| `escalate_task` | Escalate a task for human intervention. Preserves the current stage state. |
| `de_escalate_task` | Return an escalated task to its preserved stage. |

For escalation recovery, `de_escalate_task` returns the task to its preserved
stage and `reopen_task` restores closed or escalated work. Alternatively,
`close_task` accepts a non-empty `override_justification` for a deliberate
terminal close. That path still runs deterministic gates 1-9, skips the bounded
criteria review, persists the justification, clears escalation, and resets the
validation failure count. Missing or whitespace-only justification returns
`task_escalated`.

`update_task` accepts `isolation` as `none`, `worktree`, or `clone` for future
dispatch. Retargeting to `worktree` fails when the task already has clone
artifacts, and retargeting to `clone` fails when the task already has worktree
artifacts. Use `clear_isolation_pair` when artifact cleanup is intended.

### Labels

| Tool | Description |
| :--- | :--- |
| `add_label` | Add a label to a task. |
| `remove_label` | Remove a label from a task. |

### Dependencies

| Tool | Description |
| :--- | :--- |
| `add_dependency` | Add a dependency between tasks. |
| `remove_dependency` | Remove a dependency. |
| `get_dependency_tree` | Show upstream blockers and downstream dependents. |
| `check_dependency_cycles` | Detect circular dependencies in the project. |

### Ready Work

| Tool | Description |
| :--- | :--- |
| `list_ready_tasks` | Open tasks with no unresolved blockers. |
| `list_blocked_tasks` | Tasks waiting on external dependencies. |
| `suggest_next_task` | AI suggestion based on priority, readiness, and complexity. `count` controls batch size. |

### Stage Manifest

| Tool | Description |
| :--- | :--- |
| `get_task_stages` | Return a task's stage manifest in position order. |
| `list_stages_registry` | Return all registered stage definitions. |
| `get_task_type_defaults` | Return the default manifest for a task type. |

### Build Observability

| Tool | Description |
| :--- | :--- |
| `get_build_status` | Read compact task-tree build state, agents, mutexes, artifact health, events, and recent history. |
| `explain_dispatch` | Explain dispatcher eligibility and the action that would be chosen without mutating state. |
| `list_build_history` | Read recent `build_runs` and `build_history_events` rows for a task tree or build input. |

### Session Integration

| Tool | Description |
| :--- | :--- |
| `link_task_to_session` | Associate a task with a session. |
| `get_session_tasks` | Tasks linked to a session. |
| `get_task_sessions` | Sessions that touched a task. |

### Git Integration

| Tool | Description |
| :--- | :--- |
| `link_commit` | Link a git commit to a task. |
| `unlink_commit` | Unlink a commit from a task. |
| `auto_link_commits` | Auto-detect commits that mention task IDs in their messages. |
| `get_task_diff` | Combined diff for all commits linked to a task. |
| `update_observed_files` | Annotate a task's affected files from its linked commits. |

### Search

| Tool | Description |
| :--- | :--- |
| `search_tasks` | pg_search BM25 over titles, descriptions, and validation criteria. |

### Backup and Restore

| Tool | Description |
| :--- | :--- |
| `backup_tasks` | Back up current live task rows to deterministic JSONL. |
| `restore_tasks` | Explicitly restore task JSONL with non-destructive timestamp conflict handling. |

### Example: Task Workflow

```python
# 1. Find ready work
call_tool("gobby-tasks", "list_ready_tasks", {"limit": 5})

# 2. Create and claim a task
call_tool("gobby-tasks", "create_task", {
    "title": "Implement authentication",
    "priority": 1,
    "task_type": "feature",
    "category": "code",
    "validation_criteria": "Login flow works and has targeted test coverage.",
    "claim": True,
})

# 3. Inspect the task
call_tool("gobby-tasks", "get_task", {"task_id": "#123"})

# 4. Evaluate and close when ready
call_tool("gobby-tasks", "close_task", {
    "task_id": "#123",
    "commit_sha": "abc1234",
    "changes_summary": "Updated task validation flow and covered it with focused tests.",
    "preview": True,
})
# Repair every blocker and repeat the same call until closed=true.
```

The result reports each checklist item, resolved commit SHAs, transcript-derived
validation evidence, and the bounded criteria verdict. Blocked evaluations are
read-only; ready evaluations close in the same call.

---

## Task Operations (`gobby-tasks-ops`)

49 tools for expansion runs, sparse dispatch artifacts, stage transitions,
PR/merge delivery state, GitHub issues, and the shared `build_task` entry
point.

### Expansion Runs

| Tool | Description |
| :--- | :--- |
| `start_expansion_run` | Start a background expansion run for a task. |
| `get_expansion_run` | Status and stored data for a run. |
| `get_latest_expansion_run` | Most recent expansion run for a task. |
| `resume_expansion_run` | Resume a failed or interrupted run. |
| `cancel_expansion_run` | Cancel an active run. |
| `validate_expansion_run` | Validate a compiled or applied run. |
| `reset_expansion_output` | Delete generated output for a run. |
| `save_expansion_qa_result` | Persist QA findings on a run. |
| `check_expansion_qa_result` | Read QA findings stored on a run. |
| `run_expansion_qa_coverage` | Run plan coverage for QA, persist the manifest, and store artifact pointers. |
| `validate_plan_file` | Validate a Plan-Coverage Contract plan file. |

### Affected Files

| Tool | Description |
| :--- | :--- |
| `set_affected_files` | Set affected files for a task. |
| `get_affected_files` | Get affected files for a task. |
| `find_file_overlaps` | Detect file contention between tasks. |
| `wire_affected_files_from_run` | Re-apply affected files from a compiled expansion run. |

### Artifacts and Description

| Tool | Description |
| :--- | :--- |
| `set_artifact` | Set one task artifact pointer field. |
| `set_artifacts_atomic` | Atomically set multiple artifact pointer fields. |
| `get_artifacts` | Get task artifact pointer fields (worktree, clone, plan path, target branch, PR URL, etc.). |
| `clear_isolation_pair` | Clear the worktree or clone artifact pair. |
| `append_description_section` | Append an idempotent markdown section to a task description. |

### Stage Transitions

| Tool | Description |
| :--- | :--- |
| `initialize_task_manifest` | Initialize a task lifecycle manifest from task-type defaults or explicit stages. |
| `start_stage` | Transition a ready stage to in-progress. |
| `complete_stage` | Complete a stage according to its review policy. |
| `fail_stage` | Return a failed in-progress stage to ready or escalate after caps. |
| `submit_for_review` | Submit a stage for review. |
| `approve_review` | Approve review on a stage. |
| `reject_review` | Reject review on a stage. |
| `record_plan_enhancement` | Record a constructive plan-enhancement round on the `planning` stage. When suggestions exist, returns `needs_review` to `ready` for the planner **without** incrementing the adversary review budget; when converged or empty, leaves `needs_review` so adversary dispatch proceeds. |
| `add_stage` | Insert a future ready stage into a manifest. |
| `remove_stage` | Remove a future ready stage from a manifest. |

### Stage Registry

| Tool | Description |
| :--- | :--- |
| `set_task_type_defaults` | Replace a task type's default stage manifest. |
| `update_stage` | Update editable stage registry metadata. |
| `restore_stage` | Restore a bundled stage registry row. |
| `delete_stage` | Soft-delete an unused stage registry row. |

### PR and Merge Delivery

| Tool | Description |
| :--- | :--- |
| `open_delivery_pr` | Push/reuse/open a delivery PR and persist PR metadata. |
| `record_pr_opened` | Persist PR metadata in delivery state. |
| `record_pr_state` | Record PR delivery state without mutating stage state. |
| `record_pr_verdict` | Persist PR verdict and advance the PR review state. |
| `record_merge_result` | Persist merge outcome and advance/fail merge stage. |
| `get_delivery_state` | Read PR and merge delivery state for a task. |
| `close_linked_github_issue` | Comment, label, and close the GitHub issue linked to a merged task. |

### GitHub Issues

| Tool | Description |
| :--- | :--- |
| `import_github_issues` | Import GitHub issues as Gobby tasks. |
| `link_task_to_github_issue` | Link an existing task to a GitHub issue. |

### Search

| Tool | Description |
| :--- | :--- |
| `reindex_tasks` | Force rebuild of the task search index. |

### Build

| Tool | Description |
| :--- | :--- |
| `build_task` | Start lifecycle automation for a plan, epic, or leaf. The MCP entry to the same shared service used by CLI `gobby build` and HTTP `POST /api/build`. |
| `build_stop` | Stop project-wide dispatcher ticks or task-scoped automation. |
| `build_resume` | Resume project-wide dispatcher ticks or task-scoped automation. |
| `build_clean` | Delete failed build artifacts for a task ref. |
| `build_restart` | Stop, clean, and resume task-scoped build automation. |

The read-only build observability tools (`get_build_status`,
`explain_dispatch`, `list_build_history`) live on `gobby-tasks`, not here.

`build_task` requires `input_ref` and accepts the MCP-native automation options
exposed by its schema: `quick`, `skip_stages`, `isolation` (`none`, `worktree`,
or `clone`), `workspace_backend` (`worktree` or `clone`), `clone`, `no_merge`,
`pr`, `stage`, `target_branch`, `agent`, `reset_expansion_output`,
`max_active_agents`, `max_retries`, `coordinator`, and `project_id`.
`workspace_backend` and `clone` remain compatibility shims; contradictory
inputs are rejected. See [orchestration.md](./orchestration.md) for the dispatch
model.

For cross-project builds, `project_id` is the target build project and
`coordinator` should be the coordinator session's full UUID. CLI
`--coordinator current` is the convenience path that resolves the caller
process `GOBBY_SESSION_ID`; direct MCP calls should pass the resolved UUID when
the coordinator belongs to another project.

```python
call_tool(
    "gobby-tasks-ops",
    "build_task",
    {
        "input_ref": "#14354",
        "project_id": "target-project-uuid",
        "coordinator": "484d3d51-980b-4bb5-8a93-b43c9cdccf7a",
    },
)
```

---

## Plan Registry (`gobby-plans`)

8 tools for the Plan-Coverage Contract.

| Tool | Description |
| :--- | :--- |
| `create_plan` | Register a new plan and emit its initial coverage manifest. |
| `get_plan` | Get a plan row by `plan_id` or root task ref. |
| `list_plans` | List plans with state, kind, and project filters. |
| `update_plan_hash` | Recompute a plan hash and regenerate coverage if it changed. |
| `regenerate_coverage_manifest` | Regenerate the managed coverage manifest. |
| `validate_plan` | Validate a plan file against the contract. |
| `archive_plan` | Archive a plan, move its file to `completed/`, and remove its coverage manifest. |
| `delete_plan` | Hard-delete a plan row and remove its managed coverage manifest. |

The `plan_kind` enum is `implementation` (parsed strict, manifest required)
or `strategy` (parsed permissive, no manifest). See
[`docs/contracts/plan-coverage.md`](../contracts/plan-coverage.md) for the
contract and [spec-writing.md](./spec-writing.md) for the authoring flow.

---

## Session Management (`gobby-sessions`)

19 tools for session lifecycle and context management.

| Tool | Description |
| :--- | :--- |
| `register_session` | Register a session with Gobby. Requires `external_id` and `source`. |
| `get_current_session` | Get YOUR current session ID — the correct way to look up your own session. |
| `get_session` | Get session details by ID. Accepts `#N`, UUID, or prefix. |
| `list_sessions` | List sessions with filters (not for finding your own session). |
| `session_stats` | Project-level session statistics. |
| `get_session_messages` | Get messages for a session. |
| `search_session_messages` | Search rendered transcript messages by substring. |
| `get_session_commits` | Git commits made during a session timeframe. |
| `get_usage_breakdown` | Token usage broken down by source and model over a period. |
| `set_handoff` | Set handoff context (agent-authored or auto-fallback). Optional `to_session` peer delivery. |
| `get_handoff` | Read handoff context from a session. |
| `get_handoff` | Wait for a session's `summary_markdown` to become available. |
| `mark_loop_complete` | Mark the autonomous loop as complete to prevent session chaining. |
| `capture_baseline_dirty_files` | Capture current dirty files as the session-aware commit-detection baseline. |
| `restore_session_transcript` | Restore a transcript from the gzip archive for CLI resume. |
| `get_transcript_status` | Check if a transcript archive exists and read its file stats. |
| `send_keys` | Send keystrokes to a session's tmux terminal. |
| `capture_output` | Capture the last N lines of a session's tmux terminal output. |
| `set_handoff` | Trigger context compaction in the calling session via the appropriate slash command. |

Session handoff generation is CLI-driven (`gobby sessions summarize`);
the MCP surface focuses on context manipulation.

### Example: Session Handoff

```python
# Author a handoff in the current session
call_tool("gobby-sessions", "set_handoff", {
    "content": "Refactored auth/middleware.py; tests green; PR #123 open.",
    "set_handoff_ready": True,
})

# In a successor session, read the most recent handoff_ready context
call_tool("gobby-sessions", "get_handoff", {})
```

CLI context compaction does not create a successor: the compact restart
reactivates the same session row in place, and the caller may read its own
summary through `get_handoff` regardless of status.

---

## Memory (`gobby-memory`)

20 tools for persistent knowledge across sessions, including embeddings
and the optional FalkorDB knowledge graph.

### Core

| Tool | Description |
| :--- | :--- |
| `create_memory` | Create a new memory. |
| `get_memory` | Get a memory by ID. |
| `update_memory` | Update content or tags. |
| `delete_memory` | Delete a memory by ID. |
| `list_memories` | List memories with filters. |
| `search_memories` | Search by query and tags. |
| `review_task_memories` | Review memories related to a task closed by the calling session; reviewing every queued closure releases the post-close review gate. |
| `get_related_memories` | Memories linked via cross-references. |
| `memory_stats` | Statistics about the memory system. |

### Knowledge Graph and Embeddings

| Tool | Description |
| :--- | :--- |
| `search_knowledge_graph` | Search the FalkorDB knowledge graph for entities. |
| `rebuild_knowledge_graph` | Extract entities and relationships from all memories. |
| `rebuild_crossrefs` | Rebuild cross-references via semantic similarity. |
| `reindex_embeddings` | Regenerate embedding vectors for all memories. |

### Sessions

| Tool | Description |
| :--- | :--- |
| `judge_shadow_relevance` | Judge pending shadow-memory recall candidates after a completed turn. |

### Backup, Restore, and Maintenance

| Tool | Description |
| :--- | :--- |
| `restore_memories` | Explicitly restore memories from a JSONL backup without deleting absent or newer rows. |
| `backup_memories` | Back up current live memories to deterministic JSONL. |
| `memory_dream` | Review stale memories, apply a validated plan, and snapshot mutations. |
| `memory_dream_status` | Return status and summary for a dream run. |
| `memory_dream_revert` | Revert a dream run from its snapshots. |

### Example: Memory Operations

```python
# Store a memory
call_tool("gobby-memory", "create_memory", {
    "content": "This project uses pytest fixtures in conftest.py",
    "memory_type": "fact",
    "tags": ["testing", "pytest"],
})

# Search with tag filtering
call_tool("gobby-memory", "search_memories", {
    "query": "testing setup",
    "tags_all": ["testing"],
    "tags_none": ["deprecated"],
})
```

---

## Workflows, Rules, Pipelines, Agent Definitions (`gobby-workflows`)

The umbrella registry for domain definitions and pipeline execution.
Standalone rules, reusable variables, persona-capable agent definitions, and
pipelines live here. There is no generic definition CRUD surface; pick the
domain tool that matches the object you want. The table below lists the
stable primary surface; older pipeline-run query compatibility entries may
still appear in discovery.

### Evaluation And Runtime

| Tool | Description |
| :--- | :--- |
| `get_step_status` | Current session agent-step snapshot and live session variables. |
| `evaluate_pipeline` | Validate a pipeline definition without executing it. |
| `evaluate_agent` | Validate an agent definition and its nested step workflow without executing. |
| `reload_cache` | Clear the pipeline cache and re-sync bundled and imported definitions. |

### Rules

| Tool | Description |
| :--- | :--- |
| `list_rules` | List standalone rules. |
| `get_rule` | Get a standalone rule by name. |
| `create_rule` | Create a standalone rule. |
| `update_rule` | Update an existing standalone rule. |
| `toggle_rule` | Enable or disable a standalone rule. |
| `delete_rule` | Soft-delete a standalone rule. |

### Variables

| Tool | Description |
| :--- | :--- |
| `list_variables` | List variable definitions. |
| `get_variable_definition` | Get a variable definition by name. |
| `create_variable` | Create a new variable definition. |
| `update_variable` | Update a variable's value or description. |
| `delete_variable` | Soft-delete a variable definition. |
| `export_variable` | Export a variable as YAML. |

### Agent Definitions

| Tool | Description |
| :--- | :--- |
| `list_agent_definitions` | List agent definitions. |
| `get_agent_definition` | Get an agent definition by name. |
| `create_agent_definition` | Create a new agent definition. |
| `toggle_agent_definition` | Enable or disable an agent definition. |
| `delete_agent_definition` | Soft-delete an agent definition. |
| `update_agent_rules` | Add or remove rules from an agent's workflows. |
| `update_agent_variables` | Set or remove variables on an agent's workflows. |
| `update_agent_step_workflow` | Replace an agent's nested `step_workflow` object, or pass `None` to clear it. |

### Pipelines

| Tool | Description |
| :--- | :--- |
| `list_pipelines` | List pipeline definitions. |
| `get_pipeline` | Inspect a pipeline definition. |
| `create_pipeline` | Create a pipeline from YAML. |
| `update_pipeline` | Update a pipeline definition. |
| `delete_pipeline` | Soft-delete a pipeline definition. |
| `export_pipeline` | Export a pipeline as YAML. |
| `run_pipeline` | Start a pipeline and return its `execution_id`. |
| `resume_pipeline` | Resume a failed pipeline execution. |
| `approve_pipeline` | Resolve a pipeline approval gate (approve). |
| `reject_pipeline` | Resolve a pipeline approval gate (reject). |
| `cancel_pipeline` | Cancel a running pipeline execution and kill its agents. |
| `get_pipeline_status` | Inspect a pipeline execution and its steps. |
| `pipeline_eval` | Evaluate a structured data expression inside a running pipeline. |
| `fail_pipeline` | Mark the current pipeline as failed from inside a run. |

For broad pipeline-run discovery, prefer the CLI run-history surface:
`gobby pipelines runs list`, `gobby pipelines runs show`, and
pipeline-specific history commands. Use `get_pipeline_status` when you
already have an `execution_id`. Compatibility query tools may still appear
in live discovery so older clients do not break.

There is **no `wait_for_completion` MCP tool**. Start the run, persist its
`execution_id` or `run_id`, and resume from the daemon's durable
completion notification (or poll `get_pipeline_status` /
`get_agent_result` / `get_task`).

### Example: Run a Pipeline

```python
result = call_tool("gobby-workflows", "run_pipeline", {
    "name": "expand-task",
    "inputs": {"task_id": "#100"},
})

call_tool("gobby-workflows", "get_pipeline_status", {
    "execution_id": result["execution_id"],
})
```

---

## Agent Runtime (`gobby-agents`)

19 tools for spawning agents, applying personas, and inter-agent
coordination. Agent **definitions** live in `gobby-workflows`; this
registry is the runtime side.

### Spawning and Lifecycle

| Tool | Description |
| :--- | :--- |
| `spawn_agent` | Spawn a subagent. Supports `none`, `worktree`, or `clone` isolation. |
| `dispatch_batch` | Spawn multiple workers from task suggestions in parallel. |
| `apply_persona` | Apply a persona-capable agent definition to the current session — no child process. |
| `evaluate_spawn` | Dry-run evaluation of `spawn_agent`. |
| `can_spawn_agent` | Check whether an agent may be spawned (slot caps, depth limit, etc.). |
| `running_agent_stats` | Statistics about running agents. |

### Run Inspection

| Tool | Description |
| :--- | :--- |
| `get_agent_result` | Final result of a completed run. Terminal-capture results are bounded and include capture metadata. |
| `get_agent_capture` | Read the complete raw terminal capture in bounded Unicode-character pages. |
| `wait_for_agent` | Return the current status immediately. Active runs register a durable completion notification delivered through the parent inbox plus a live wake nudge. |
| `list_agent_runs` | Runs for a parent session. |
| `list_running_agents` | All currently running agents. |
| `get_running_agent` | Process state for a running agent. |
| `cancel_stale_helpers` | Cancel stale helper agent runs. |

### Termination

| Tool | Description |
| :--- | :--- |
| `stop_agent` | Mark a run cancelled without killing the process. |
| `kill_agent` | Terminate the agent process and close its terminal. |
| `end_agent_run` | Signal that the current run is complete and release its resources. |
| `unregister_agent` | Internal registry cleanup helper. |

### Messaging

| Tool | Description |
| :--- | :--- |
| `send_message` | Message a `session`, `agent`, `project`, `build`, or `all` target. |
| `get_inter_session_message` | Retrieve one complete message as its sender or recipient. |
| `get_inter_session_messages` | Read message history. |

The agent depth limit is 5; spawn requests beyond that are rejected.
Rule authors should treat `turn_start` and `turn_end` as the semantic
lifecycle events. Provider/runtime hooks such as `before_agent`,
`after_agent`, and `stop` are transport details. Agent termination is a
separate runtime transition and still requires `end_agent_run`.

`send_message` takes `from_session`, `target`, `content`, and optional
`target_id`. Use `target="session"` with a session ref, `target="agent"` with
an agent run id, `target="project"` with a project id or name, and
`target="build"` with a build run id, build input ref, or root task ref.
Use `target="all"` without `target_id` for every deliverable non-system
session except the sender.

### Example: Agent Spawning

```python
# Spawn an agent in worktree isolation
call_tool("gobby-agents", "spawn_agent", {
    "prompt": "Implement the login feature",
    "task_id": "#123",
    "parent_session_id": "<your_session_id>",
    "isolation": "worktree",
})

# Apply a persona to the current session instead of spawning
call_tool("gobby-agents", "apply_persona", {
    "agent": "backend-developer",
})
```

### Example: Inter-Agent Messaging

```python
# P2P message to one session
call_tool("gobby-agents", "send_message", {
    "from_session": "<your_session>",
    "target": "session",
    "target_id": "<target_session>",
    "content": "Task completed. All tests pass.",
})

# Fan out to active agents working in a build subtree
call_tool("gobby-agents", "send_message", {
    "from_session": "<your_session>",
    "target": "build",
    "target_id": "#123",
    "content": "Pause work before merge validation.",
})

```

---

## Worktrees (`gobby-worktrees`)

17 tools for git worktree isolation.

| Tool | Description |
| :--- | :--- |
| `create_worktree` | Create a new git worktree. |
| `get_worktree` | Get worktree details. |
| `list_worktrees` | List worktrees with filters. |
| `claim_worktree` | Claim ownership for an agent session. |
| `release_worktree` | Release ownership. |
| `delete_worktree` | Delete a worktree (git plus DB record). |
| `sync_worktree` | Sync a worktree with the main branch. |
| `merge_worktree` | Merge a worktree's branch into its base. |
| `mark_worktree_merged` | Mark a worktree merged (ready for cleanup). |
| `abandon_worktree` | Mark a worktree abandoned. |
| `reactivate_worktree` | Reactivate a worktree without merging or deleting. |
| `push_branch` | Push a worktree branch to a remote branch. |
| `detect_stale_worktrees` | Find worktrees with no recent activity. |
| `cleanup_stale_worktrees` | Mark and optionally delete stale worktrees. |
| `get_worktree_stats` | Project worktree statistics. |
| `get_worktree_by_task` | Worktree linked to a specific task. |
| `link_task_to_worktree` | Link a task to an existing worktree. |

---

## Clones (`gobby-clones`)

13 tools for git clone isolation. Use clones when a worktree's shared
object database is too tight a coupling (e.g. parallel `git gc` work,
sandboxed dependencies).

| Tool | Description |
| :--- | :--- |
| `create_clone` | Create a new git clone. |
| `get_clone` | Get clone by ID. |
| `list_clones` | List clones with optional status filter. |
| `delete_clone` | Delete a clone and its files. |
| `sync_clone` | Sync a clone with its remote. |
| `merge_clone` | Merge a clone's branch into the target branch in the main repo. |
| `claim_clone` | Claim ownership for an agent session. |
| `release_clone` | Release ownership. |
| `get_clone_by_task` | Clone linked to a specific task. |
| `link_task_to_clone` | Link a task to an existing clone. |
| `get_clone_stats` | Counts by status for the project. |
| `detect_stale_clones` | Find clones with no recent activity. |
| `cleanup_stale_clones` | Mark and optionally delete stale clones. |

---

## Merge Operations (`gobby-merge`)

12 tools for merge resolution, conflict prediction, and cross-worktree
verification.

### Resolution Lifecycle

| Tool | Description |
| :--- | :--- |
| `merge_start` | Start a merge with AI-assisted conflict resolution. Requires `worktree_id` and `source_branch`. |
| `merge_status` | Status and conflict details for an active resolution. |
| `merge_resolve` | Resolve a specific conflict (optionally with AI). |
| `merge_apply` | Apply resolved conflicts and complete the merge. |
| `merge_abort` | Abort the merge and restore the previous state. |
| `inspect_merge_state` | Detect mid-merge / mid-cherry-pick / mid-rebase state and unresolved files. |

### Branch and Conflict Analysis

| Tool | Description |
| :--- | :--- |
| `probe_branch_protection` | Check whether the target branch should be delivered through a GitHub PR. |
| `analyze_merge_landscape` | List unmerged worktrees with branch, base, divergence stats, files touched, and recency. |
| `predict_conflicts` | Run `git merge-tree` simulations between worktree branches to predict conflicting pairs. |

### Worktree-Scoped Operations

| Tool | Description |
| :--- | :--- |
| `cherry_pick_into_worktree` | Cherry-pick one or more commits into a worktree. |
| `merge_subset` | Pull a subset of paths from another branch via `git checkout source -- <paths>`. |
| `verify_in_worktree` | Run an allowlisted verification command (test, build, typecheck, etc.) in a worktree. |

### Example: Merge Workflow

```python
# Start a merge
call_tool("gobby-merge", "merge_start", {
    "worktree_id": "6f1d2b3a-9c4e-4f5a-8b6c-7d8e9f0a1b2c",
    "source_branch": "feature/login",
})

# Check status
call_tool("gobby-merge", "merge_status", {"resolution_id": "<id>"})

# Resolve a conflict with AI
call_tool("gobby-merge", "merge_resolve", {
    "conflict_id": "<conflict_id>",
    "use_ai": True,
})

# Apply and complete
call_tool("gobby-merge", "merge_apply", {"resolution_id": "<id>"})
```

---

## Config (`gobby-config`)

7 tools for runtime config (the layered Pydantic config tree backed by
PostgreSQL hub overrides).

| Tool | Description |
| :--- | :--- |
| `get_config` | Get a config value by dotted key. |
| `get_config_section` | Get an entire section as a nested dict. |
| `set_config` | Set a config value by dotted key. |
| `set_config_batch` | Set multiple keys atomically. |
| `delete_config` | Delete a config override by dotted key. |
| `list_config_keys` | List all stored config keys, optionally filtered by prefix. |
| `ensure_defaults` | Populate missing keys from Pydantic defaults for a section. |

---

## Skills (`gobby-skills`)

12 tools for skill discovery, installation, and hub integration.

### Discovery

| Tool | Description |
| :--- | :--- |
| `list_skills` | List skills with light metadata. |
| `get_skill` | Full skill content by name or ID. |
| `get_skill_file` | Read a single file from a multi-file skill. |
| `search_skills` | Search skills by query. |

### Installation

| Tool | Description |
| :--- | :--- |
| `install_skill` | Install from a local path, GitHub URL, or ZIP archive. |
| `update_skill` | Refresh a skill from its source. |
| `remove_skill` | Soft-delete a skill. |
| `restore_skill` | Restore a soft-deleted skill. |
| `move_skill_to_project` | Move a skill to project scope. |
| `move_skill_to_installed` | Move a project-scoped skill back to installed scope. |

### Hubs

| Tool | Description |
| :--- | :--- |
| `list_hubs` | List configured skill hubs. |
| `search_hub` | Search across configured hubs. |

---

## Metrics (`gobby-metrics`)

13 tools for tool, rule, and skill metrics, plus token usage and time-series
reports.

### Tool Metrics

| Tool | Description |
| :--- | :--- |
| `get_tool_metrics` | Call count, success rate, and latency for tools. |
| `get_top_tools` | Top tools by usage, success rate, or latency. |
| `get_failing_tools` | Tools with high failure rates. |
| `get_tool_success_rate` | Success rate for a specific tool. |

### Reset and Retention

| Tool | Description |
| :--- | :--- |
| `reset_metrics` | Reset metrics for a project, server, or specific tool. |
| `reset_tool_metrics` | Admin reset for a specific tool. |
| `cleanup_old_metrics` | Delete metrics older than the retention period. |
| `get_retention_stats` | Retention and age statistics. |

### Reports

| Tool | Description |
| :--- | :--- |
| `get_usage_report` | Token usage for a time period. |
| `get_session_tools` | Per-tool call breakdown for a session. |
| `get_rule_metrics` | Rule evaluation stats — fire counts, block vs allow, latency. |
| `get_skill_metrics` | Skill search and invocation stats. |
| `get_metrics_timeseries` | Time-bucketed metrics for dashboard charts. |

---

## Cron (`gobby-cron`)

8 tools for scheduled triggers backed by the cron scheduler.

| Tool | Description |
| :--- | :--- |
| `list_cron_jobs` | List cron jobs with project and enabled filters. |
| `create_cron_job` | Create a new cron job. |
| `get_cron_job` | Get a job by ID. |
| `update_cron_job` | Update a job's configuration. |
| `toggle_cron_job` | Toggle enabled state. |
| `delete_cron_job` | Delete a job and its run history. |
| `run_cron_job` | Trigger an immediate run, bypassing the schedule. |
| `list_cron_runs` | List run history for a job. |

---

## Communications (`gobby-communications`)

16 tools for channel administration, delivery, identities, and event
subscriptions.

| Tool | Description |
| :--- | :--- |
| `send_message` | Send a message through a configured channel. |
| `send_attachment` | Send a workspace attachment through a configured channel. |
| `list_channels` | List configured channels. |
| `get_messages` | List persisted inbound or outbound messages. |
| `add_channel` | Add and initialize a channel. |
| `remove_channel` | Remove a channel. |
| `set_channel_project` | Bind a responder channel to a project. |
| `send_proactive_message` | Send to an explicit platform conversation. |
| `link_identity` | Link a platform identity to a session. |
| `list_identities` | List linked platform identities. |
| `unlink_identity` | Remove a platform identity's session link. |
| `create_event_subscription` | Create a project-scoped, session-scoped, or explicit-global subscription. |
| `list_event_subscriptions` | List subscriptions with channel, scope, enabled, and event-pattern filters. |
| `get_event_subscription` | Get one subscription by ID. |
| `update_event_subscription` | Partially update one subscription by ID. |
| `delete_event_subscription` | Delete one subscription by ID. |

`create_event_subscription` infers project scope from the calling session when
`project` and `global_scope` are omitted. Telegram responder sessions therefore
inherit the responder project configured on their channel. Use
`global_scope=true` explicitly for global routing. Outbound subscription scope
remains independent from the channel's responder-project binding.

---

## Hub (Cross-Project) (`gobby-hub`)

5 tools for queries that span every initialized project.

| Tool | Description |
| :--- | :--- |
| `list_all_projects` | All initialized Gobby projects with names and repo paths. |
| `list_cross_project_tasks` | Tasks across all projects. |
| `list_cross_project_sessions` | Recent sessions across all projects. |
| `hub_stats` | Aggregate hub statistics. |
| `get_machine_id` | The daemon's machine identifier. |

---

## Voice (`gobby-voice`)

4 tools for the Whisper STT vocabulary used by voice chat.

| Tool | Description |
| :--- | :--- |
| `add_vocab` | Add terms to the vocabulary. |
| `remove_vocab` | Remove terms from the vocabulary. |
| `list_vocab` | List current terms and the prompt. |
| `clear_vocab` | Clear all vocabulary terms. |

---

## Error Handling

Successful internal-tool calls return the tool's raw payload — the proxy
strips any top-level `"success": true` marker, and there is no `result`
wrapper key. Payload shapes vary per tool (for example, `create_task` returns
`{"id", "seq_num", "ref"}`).

**Failure:**

```json
{
  "success": false,
  "error": "Error message"
}
```

Some failure paths add an `error_code` field (proxy blocked/error responses)
or, for import/config tools, an `error_type` field.

When a `call_tool` invocation fails parameter validation, the error always
includes the current schema for the target tool and idempotently retains its
lease, so callers can correct and retry without an extra schema round-trip.

---

## See Also

- [cli-commands.md](./cli-commands.md) — CLI command reference
- [tasks.md](./tasks.md) — Task system guide
- [sessions.md](./sessions.md) — Session management guide
- [memory.md](./memory.md) — Memory system guide
- [rules.md](./rules.md) — Rule engine guide
- [orchestration.md](./orchestration.md) — Dispatch and automation model
- [code-index.md](./code-index.md) — `gcode` for code search and retrieval

_Last verified: 2026-08-14_
