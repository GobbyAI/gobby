# Orchestrator v3: Design Discussion

> Design discussion captured 2026-03-02, session #2369.

## Problem Statement

The current orchestrator creates one worktree per task, producing N branches and N merge operations for an epic with N subtasks. This:

- Compounds merge conflict risk (N-way merge at completion)
- Creates unnecessary disk/git overhead
- Branches from `origin/base_branch` by default, missing unpushed local commits from other agents (bug #9538)
- Failed in coordinator testing due to the remote-branch-based worktree creation

## Industry Survey

Every tool uses worktree-per-agent. Nobody has solved shared-worktree parallel dispatch.

| Tool | Isolation Model | Merge Strategy |
|------|----------------|----------------|
| **Claude Code** | Worktree per agent/subagent (`--worktree` flag). Auto-cleaned on completion. | Per-agent branch merge |
| **Gastown** (Yegge) | Worktree per Polecat (worker). Mayor orchestrates assignment. | Refinery role handles merges |
| **Maestro** (Amini) | Worktree per session (up to 6 parallel). Physically separate directories. | Per-session merge |
| **GitHub Copilot** | VS Code spins up worktree per background agent. Mission Control dashboard. | Per-agent merge |
| **Gemini CLI** | **Known limitation**: can't access worktree paths (issue #12050). Tools restricted to workspace root. | N/A — clones are the workaround |

Gobby's `gobby-clones` was built specifically as the Gemini workaround. Clones are heavier (duplicate full repo) but give Gemini a workspace it can access.

**Key finding**: The file-based dependency analysis for smart parallel dispatch in a shared worktree has no prior art.

## Proposed Architecture

### Core Insight

The orchestrator should automate the judgment calls the user currently makes manually: "don't launch task B yet because task A is going to ruff the whole tree." Multiple agents already work in the same directory today — the orchestrator just needs to be smarter about what it assigns in parallel.

### Flow

```text
1. Epic arrives (task or plan)
        |
        v
2. PLANNING AGENT examines the epic/plan
   - Decides whether to break into subtasks
   - Creates task tree with suggested dependencies and parallel paths
   - Annotates each subtask with predicted affected_files
   (Replaces the current skill-based expansion)
        |
        v
3. DEPENDENCY ANALYSIS AGENT explores the repo
   - Reads the task tree + file annotations
   - Explores the actual codebase to validate/refine
   - Creates blocking dependencies based on file overlap
   - Catches nuanced cases a tool can't:
     * "Task A adds an API route, Task B modifies auth middleware that routes import"
     * "This task runs ruff format src/ — it's a serialization point"
   - Identifies parallel paths (groups of non-conflicting tasks)
        |
        v
4. ORCHESTRATOR dispatches work
   - Calls suggest_next_tasks (plural) for batch of parallelizable tasks
   - Dispatches multiple agents into the SAME worktree/clone simultaneously
   - Agents commit to the same branch independently
   - Git index.lock conflicts are transient (retry, not catastrophe)
   - File-level conflicts prevented by dependency analysis
        |
        v
5. Loop until epic complete
   - Re-run suggest_next_tasks each iteration
   - QA agent reviews completed tasks (fire-and-forget)
   - One merge at the end: epic branch -> main
```

### Isolation Model

One worktree/clone per epic. Multiple agents work in it simultaneously. This is how the user already works — just with smarter assignment.

- Agents working on non-overlapping files: dispatched in parallel
- Agents working on overlapping files: serialized via blocking dependencies
- Global operations (ruff format, test suite): treated as serialization points
- Git index.lock: handled by agent retry (already happens today)

## Key Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Expansion trigger | Orchestrator-driven (planning agent), not manual skill | Orchestrator owns the full lifecycle — expansion is part of orchestration |
| Dependency analysis | Agent (not MCP tool) | Needs codebase exploration and nuanced judgment. Tools can only do mechanical file overlap detection. |
| File annotation storage | Junction table `task_affected_files` | Need indexed file-task lookups for overlap queries. Follows `task_dependencies` pattern. |
| Dispatch model | Parallel agents in shared worktree | Already works today (user does it manually). File-based deps minimize conflicts. |
| Suggestion tool | New `suggest_next_tasks` (plural) | Returns batch of non-conflicting tasks. Different semantics from existing `suggest_next_task` (singular). |
| Pipeline template | Modify existing `orchestrator.yaml` | No other users — only the author has it |

## Components to Build

### 1. `task_affected_files` Table + Manager

- Junction table: `(task_id, file_path, annotation_source, created_at)`
- Indexes on both `task_id` and `file_path`
- `TaskAffectedFilesManager` following `TaskDependencyManager` pattern
- Key method: `find_overlapping_tasks(task_ids) -> {(task_a, task_b): [shared_files]}`
- `annotation_source`: 'expansion' | 'manual' | 'observed' (future: from git diff)

### 2. Planning Agent Definition

- New agent definition YAML (replaces skill-based expansion)
- Uses enhanced expansion prompt with `affected_files` field
- Creates task tree via `TaskTreeBuilder` (existing) with file annotations
- Suggests parallel paths alongside dependencies

### 3. Dependency Analysis Agent Definition

- New agent definition YAML
- Explores codebase to validate/refine file annotations from planning agent
- Creates blocking dependencies via `TaskDependencyManager` (existing)
- Identifies serialization points (global formatters, test suites)
- Handles nuanced cross-file dependencies (imports, shared state)

### 4. `suggest_next_tasks` (Plural) MCP Tool

- Returns batch of non-conflicting ready tasks
- Uses `TaskAffectedFilesManager.find_overlapping_tasks()` to filter
- Considers in-progress tasks' file annotations (don't dispatch overlapping work)
- Greedy selection: iterate by priority, add task if no file conflict with selected set
- Reuses scoring logic from existing `suggest_next_task` (extract shared helper)

### 5. Expansion Prompt Update

- Add `affected_files` field to subtask schema
- Add `parallel_group` field (suggested parallelization)
- Update rules to require file predictions for each subtask
- Existing file: `src/gobby/tasks/prompts/expand-task.md`

### 6. Tree Builder Update

- Wire `affected_files` from JSON nodes into `TaskAffectedFilesManager`
- ~10 lines in `_create_node()` — builder already reads arbitrary fields
- Existing file: `src/gobby/tasks/tree_builder.py`

### 7. Orchestrator Pipeline Update

- Remove per-task worktree creation
- Add epic worktree creation (first iteration only, `_worktree_id` persisted)
- Replace `suggest_next_task` with `suggest_next_tasks`
- Dispatch multiple agents per iteration (fire-and-forget into shared worktree)
- Single merge phase at epic completion
- Add planning agent + dependency agent steps before dispatch loop
- Existing file: `src/gobby/install/shared/workflows/orchestrator.yaml`

## Open Questions

### Q1: How does the dependency analysis agent interact with the task system?

Does it call MCP tools directly (add_dependency, update affected_files)? Or does it produce a JSON spec that the orchestrator pipeline applies? The former gives the agent more autonomy. The latter gives the pipeline more control.

### Q2: Should the planning agent be separate from expansion, or replace it?

Current expansion is a skill + LLM call. The planning agent would be a spawned agent that can explore the codebase during expansion. More expensive but produces better file annotations. Could the planning agent use the expansion prompt internally?

### Q3: How do we handle the ruff/formatter serialization problem?

Options:

- The dependency agent marks tasks with global formatter steps as serialization points
- Agents run formatters only on their own files (enforced by agent instructions)
- The last agent in a parallel batch runs the global format pass
- The orchestrator runs a format step between batches

### Q4: What happens when a task touches unexpected files?

File annotations are advisory predictions. If an agent modifies files not in its annotation:

- In the shared worktree model, this could conflict with parallel agents
- Post-hoc: update `task_affected_files` with `annotation_source='observed'` from git diff
- Should the orchestrator re-analyze dependencies mid-execution?

### Q5: How does this interact with clones (Gemini)?

Clones work the same way conceptually — one clone per epic, parallel agents inside it. The orchestrator shouldn't care whether the isolation context is a worktree or clone. The `spawn_agent` tool already supports both via `worktree_id` and `clone_id`.

### Q6: What's the minimum viable version?

Possible staging:

- **Stage 1**: Fix #9538 (use_local), modify orchestrator to reuse one worktree per epic, sequential dispatch. Parity with Gastown/Maestro.
- **Stage 2**: Add `task_affected_files` table, expansion prompt changes, tree builder wiring. Infrastructure for file-based analysis.
- **Stage 3**: Planning agent + dependency analysis agent. Smart dependency setup.
- **Stage 4**: `suggest_next_tasks` (plural) + parallel dispatch in shared worktree. The full vision.

## Known Bugs

- **#9538**: `create_worktree` MCP tool does not pass `use_local` to `WorktreeGitManager`. Worktrees always branch from remote, missing unpushed local commits.

## Critical Files

| File | Role |
|------|------|
| `src/gobby/mcp_proxy/tools/worktrees.py` | MCP worktree tools (bug #9538) |
| `src/gobby/worktrees/git.py` | Git worktree operations (`use_local` support exists) |
| `src/gobby/agents/isolation.py` | Isolation handlers (worktree/clone/none) |
| `src/gobby/mcp_proxy/tools/task_readiness.py` | `suggest_next_task` + readiness tools |
| `src/gobby/tasks/tree_builder.py` | Task tree creation from JSON |
| `src/gobby/tasks/prompts/expand-task.md` | Expansion prompt template |
| `src/gobby/storage/task_dependencies.py` | Dependency manager (pattern to follow) |
| `src/gobby/storage/migrations.py` | Schema migrations |
| `src/gobby/install/shared/workflows/orchestrator.yaml` | Pipeline template |
| `src/gobby/mcp_proxy/tools/spawn_agent/` | Agent spawning (supports worktree_id reuse) |
