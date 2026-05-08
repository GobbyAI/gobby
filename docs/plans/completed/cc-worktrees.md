# Investigation: Claude Code vs Gobby Overlap — Simplification Plan

## Context

Claude Code has shipped built-in features that directly overlap with Gobby's multi-agent orchestration layer:

1. **`isolation: "worktree"`** on the Agent tool — creates `.claude/worktrees/` git worktrees for subagents
2. **Agent Teams** — `team_name`, `SendMessageTool`, `TeamCreateTool`, coordinator mode
3. **Built-in task system** — `TaskCreate/Update/List/Get` tools for coordinating work across agents

Gobby built these capabilities before Claude Code had them. Now that Claude Code ships them natively, we need to understand the overlap and decide what Gobby should keep, delegate, or simplify.

## Analysis: Feature-by-Feature Comparison

### 1. Worktree Isolation

| Aspect | Claude Code Native | Gobby |
|--------|-------------------|-------|
| **Location** | `.claude/worktrees/` | `~/.gobby/worktrees/{project}/` |
| **Creation** | `EnterWorktreeTool` or `isolation: "worktree"` on Agent | `WorktreeIsolationHandler` via `spawn_agent()` |
| **Branch naming** | Random slug or user-provided name | Task-aware: `task-{seq}-{slug}`, `agent/{timestamp}` |
| **Cleanup** | `ExitWorktreeTool` with keep/remove + `removeAgentWorktree()` on session done | `AgentLifecycleMonitor` + stale detection + CLI commands |
| **DB tracking** | None (ephemeral) | Full DB records: `worktrees` table with status, ownership, task linking |
| **Hook copying** | N/A (same process) | Copies `.claude/`, `.gemini/` hooks to worktree |
| **MCP patching** | N/A (same process) | Patches `.mcp.json` to point at main repo's gobby |
| **Clone option** | No (`isolation: "remote"` is different) | Yes — `CloneIsolationHandler` for full isolation |
| **Merge support** | Manual (user does git operations) | Built-in `merge_worktree`, `sync_worktree` tools |

**Key insight**: Claude Code's worktree is **ephemeral and process-scoped**. Gobby's is **persistent, DB-tracked, and lifecycle-managed**. They serve different needs:
- Claude Code: quick throwaway isolation for a single agent task
- Gobby: managed isolation with task linking, merge workflows, stale cleanup, and multi-provider support

### 2. Agent Spawning & Orchestration

| Aspect | Claude Code Native | Gobby |
|--------|-------------------|-------|
| **Spawn mechanism** | `AgentTool.call()` in-process | `spawn_agent()` MCP tool → tmux session or asyncio task |
| **Providers** | Claude only | Claude, Gemini, Codex (multi-provider) |
| **Agent definitions** | Markdown files in `.claude/agents/` | YAML in DB (`workflow_definitions` table) with structured fields |
| **Step workflows** | None (agents are free-form) | Inline `steps` array with transitions, tool restrictions per step |
| **Depth limiting** | Hardcoded (agents can't spawn agents by default) | Configurable `max_agent_depth=5` with DB tracking |
| **Process tracking** | In-process task tracking | DB-backed `agent_runs` + in-memory `RunningAgentRegistry` + lifecycle monitor |
| **Background agents** | `run_in_background: true` | Interactive (tmux) or autonomous (asyncio) modes |
| **Coordinator mode** | `CLAUDE_CODE_COORDINATOR_MODE=1` | Orchestrator agent definition + pipeline |

**Key insight**: Claude Code's agent system is **Claude-only, in-process, and lightweight**. Gobby's is **multi-provider, process-isolated, and heavily lifecycle-managed**. The overlap is conceptual, not implementation.

### 3. P2P Messaging

| Aspect | Claude Code Native | Gobby |
|--------|-------------------|-------|
| **Mechanism** | `SendMessageTool` (in-process) | `send_message()` MCP tool → DB-backed `inter_session_messages` |
| **Scope** | Team members within same process tree | Any session in same project (cross-process) |
| **Commands** | None explicit | `send_command()` / `complete_command()` — structured parent→child commands |
| **Delivery** | Direct (shared memory) | Store-and-forward with hook-based injection |
| **Teams** | `TeamCreateTool` / `TeamDeleteTool` | No explicit team concept (sessions linked via `parent_session_id`) |

**Key insight**: Claude Code's messaging is **synchronous, in-process**. Gobby's is **asynchronous, cross-process, cross-provider**. Again, different domains.

### 4. Task System

| Aspect | Claude Code Native | Gobby |
|--------|-------------------|-------|
| **Tools** | `TaskCreate/Update/List/Get` | `gobby-tasks` MCP server (create, claim, close, validate) |
| **Scope** | Per-session todo list | Project-wide with dependency graphs, TDD expansion, validation gates |
| **Persistence** | Session-scoped (lost on exit) | DB + `.gobby/tasks.jsonl` (git-native sync) |
| **Validation** | None | Diff-based validation criteria checked on close |
| **Assignment** | Implicit (tool caller owns) | Explicit claim/release with session tracking |

**Key insight**: Gobby's task system is vastly more capable. Claude Code's is a simple todo list.

---

## Where Gobby Can Simplify

Despite the different scopes, there ARE areas where Gobby carries unnecessary complexity now that Claude Code has native capabilities:

### Note on Opportunities 1-4: No Redundancy to Eliminate

The two worktree systems are **completely separate code paths**:

- **Claude Code's native `isolation: "worktree"`**: Creates worktrees in `.claude/worktrees/`, managed entirely by Claude Code's internal Agent tool. Gobby never touches these.
- **Gobby's `spawn_agent(isolation="worktree")`**: Creates worktrees in `~/.gobby/worktrees/`, copies hooks via `_copy_cli_hooks()`, patches MCP config via `_patch_mcp_config_for_isolation()`. Only runs when Gobby's MCP tools are invoked.

There is no redundant hook copying or MCP patching — Gobby's isolation code only runs for Gobby-managed agents. Claude Code's native worktree doesn't go through Gobby at all. The initial analysis incorrectly assumed these paths overlapped.

### Opportunity 5: Subagent Rule Isolation

**Current state**: All `default`-tagged rules apply to native subagent tool calls (since subagents share the parent session's hooks). This blocks native task tools, requires gobby task claims before edits, and enforces memory/error/commit gates — all inappropriate for lightweight subagent work.

**Fix**: New `is_subagent` session variable toggled by SUBAGENT_START/STOP hooks. Gates 6 existing rules + 1 new rule:

- **Unblock**: native task tools (TaskCreate, TaskUpdate, TaskGet, TaskList, TodoWrite)
- **Block**: gobby-tasks MCP tools (subagents must not touch project-level tasks)
- **Disable**: require-task-before-edit, require-error-triage, require-memory-review, require-commit-before-status

See Phase 0 in "What CAN Be Simplified" for full implementation details.

---

## Revised Simplification Analysis

### The Two Systems Are Completely Separate

Claude Code's native `isolation: "worktree"` and Gobby's `spawn_agent(isolation="worktree")` are independent code paths that never interact:

- **Claude Code native**: `.claude/worktrees/`, in-process, ephemeral, no DB tracking
- **Gobby managed**: `~/.gobby/worktrees/`, tmux-spawned, DB-tracked, lifecycle-monitored

Gobby's hook copying (`_copy_cli_hooks`) and MCP patching (`_patch_mcp_config_for_isolation`) only run for Gobby-managed worktrees. There is no redundancy to remove.

### What CAN Be Simplified

#### Phase 0: Subagent Rule Isolation via `is_subagent` Variable

New session variable `is_subagent` (separate from `is_spawned_agent` which controls worker-safety rules). Toggled by SUBAGENT_START/STOP hooks. Controls which rules apply during native subagent execution.

**Core changes**:

1. **`src/gobby/install/shared/workflows/variables/gobby-default-variables.yaml`** — Add `is_subagent: false`.

2. **`src/gobby/hooks/event_handlers/_agent.py`** — In `handle_subagent_start()`, set `is_subagent = True` via `SessionVariableManager`. In `handle_subagent_stop()`, set it back to `False`.

**Rule changes** (6 existing rules + 1 new):

| Rule File | Change |
|-----------|--------|
| `task-enforcement/block-native-task-tools.yaml` | Add `when: "not variables.get('is_subagent')"` — unblock native task tools for subagents |
| `task-enforcement/require-task-before-edit.yaml` | Add `and not variables.get('is_subagent')` — subagents can edit without gobby task claim |
| `task-enforcement/require-error-triage.yaml` | Add `and not variables.get('is_subagent')` — subagents skip error triage gates |
| `task-enforcement/require-commit-before-status.yaml` | Add `and not variables.get('is_subagent')` — subagents skip commit-before-status gate |
| `memory-lifecycle/require-memory-review-before-status.yaml` | Add `and not variables.get('is_subagent')` — subagents skip memory review gate |
| **NEW**: `task-enforcement/block-gobby-tasks-subagent.yaml` | Block gobby-tasks MCP tools when `is_subagent` is true |

**New rule** (`block-gobby-tasks-subagent.yaml`):

```yaml
tags: [task-enforcement, enforcement, tasks, gobby, default]

rules:
  block-gobby-tasks-subagent:
    description: "Block gobby-tasks MCP tools for native subagents"
    event: before_tool
    enabled: true
    priority: 29
    when: "variables.get('is_subagent')"
    effects:
      - type: block
        mcp_tools:
          - "gobby-tasks:create_task"
          - "gobby-tasks:claim_task"
          - "gobby-tasks:close_task"
          - "gobby-tasks-ops:submit_for_review"
          - "gobby-tasks-ops:approve_review"
        reason: |
          Native subagents should use CC's native task tools (TaskCreate, TaskUpdate)
          for lightweight coordination. gobby-tasks is for project-level task management
          by the parent session.
```

**How it works**: SUBAGENT_START → `is_subagent = True` → native task tools unblocked, gobby-tasks blocked, edit/status gates disabled. SUBAGENT_STOP → `is_subagent = False` → everything reverts.

**Why a new variable**: `is_spawned_agent` controls worker-safety rules and is set at session creation for Gobby-managed agents. Different lifecycle, different semantics.

**SDK upgrade (deferred)**: Bump `claude-agent-sdk` to `>=0.1.55` is deferred — currently pinned `>=0.1.39,<=0.1.45` due to a breaking change in `query()` at >0.1.45 that requires investigation before upgrading. The latest SDK has richer `SubagentStartHookInput` (`agent_id`, `agent_type`) and `SubagentStopHookInput` (`agent_id`, `agent_transcript_path`, `agent_type`, `stop_hook_active`). The TypeScript SDK also has `WorktreeCreate`/`WorktreeRemove`, `TaskCompleted`, `TeammateIdle` events that may land in Python SDK soon — useful for Phase 0.5 delegation and worktree tracking. Phase 0 can proceed without the upgrade; richer input fields are a Phase 1+ enhancement.

Update `handle_subagent_start()` and `handle_subagent_stop()` in `_agent.py` to capture the richer input fields (`agent_type`, `agent_transcript_path`).

**Scope**: Claude Code only. Gemini CLI has no SubagentStart/SubagentStop hooks (confirmed in adapter: `gemini.py:19`). Codex CLI spawns agents as separate processes, not in-process subagents. The rules gated by `is_subagent` (native TaskCreate, etc.) are Claude Code-specific tools anyway.

**Additional fix — wire subagent hooks in web chat**:

The web chat session (`src/gobby/servers/websocket/chat/_session.py:231-246`) wires `_on_before_agent`, `_on_pre_tool`, `_on_post_tool`, `_on_pre_compact`, `_on_stop` — but **does NOT wire `_on_subagent_start` or `_on_subagent_stop`**. The `ChatSession` class has the fields and hook registration (`chat_session.py:403-435`), but the WebSocket session manager never connects them.

Fix at `src/gobby/servers/websocket/chat/_session.py` after line 246:

```python
session._on_subagent_start = lambda data: self._fire_lifecycle(
    conversation_id, HookEventType.SUBAGENT_START, data
)
session._on_subagent_stop = lambda data: self._fire_lifecycle(
    conversation_id, HookEventType.SUBAGENT_STOP, data
)
```

The autonomous runner (`spawn_executor.py:595-596`) already wires both callbacks correctly.

#### Phase 0.5: Native Agent Tool Delegation for Claude-to-Claude Spawns

For Claude provider agents, Gobby's `spawn_agent` could delegate to Claude Code's native Agent tool instead of launching a tmux session. The calling agent would be instructed to invoke `Agent(subagent_type="{name}", isolation="worktree", model="{model}")`.

**Implementation**:

1. **Compose prompt from agent definition** — Resolve the `AgentDefinitionBody`, compose its persona (role, goal, instructions, personality) into a single prompt string — the same pattern used when gobby-skills injects skill content via `inject_context`. No file creation needed.

2. **Return delegation response from `spawn_agent`** — Instead of launching tmux, return a response like:

   ```json
   {
     "delegation": "native_agent",
     "isolation": "worktree",
     "model": "{model}",
     "prompt": "{composed persona + user prompt}"
   }
   ```

   The calling agent reads this and invokes Claude Code's native Agent tool with the composed prompt.

3. **Track via SUBAGENT_START/STOP hooks** — When the native subagent starts, Gobby creates an `agent_runs` DB record and (if worktree) a `worktrees` DB record. On SUBAGENT_STOP, marks completion and can handle cleanup.

4. **Still create Gobby agent_runs record** — `spawn_agent` pre-creates the `agent_runs` record (status=pending) before returning the delegation. SUBAGENT_START transitions it to running.

**What this eliminates for Claude provider**:
- tmux session management
- `_copy_cli_hooks()` — native subagents inherit parent's MCP connections in-process
- `_patch_mcp_config_for_isolation()` — same reason
- Separate process tracking via `RunningAgentRegistry`
- `AgentLifecycleMonitor` polling for these agents

**What Gobby retains**:
- Agent definition resolution (DB-backed, structured YAML)
- Task linking (`agent_runs.task_id`)
- DB-tracked worktrees (created via SUBAGENT_START hook)
- Task-aware branch naming (passed via Agent tool's `name` parameter)
- Step workflows (injected as context into the agent definition)
- Rule enforcement (rules still fire on the subagent's tool calls via parent session hooks)

**Scope**: Claude provider only. Gemini/Codex agents still use tmux spawning (they don't have an equivalent native agent tool).

**Resolved questions**:

1. **Branch naming**: Agent tool's `name` parameter flows to `EnterWorktreeTool` as the slug (alphanumeric, dots, underscores, dashes, max 64 chars). Gobby can pass a task-formatted name like `task-42-fix-auth` through this parameter.

2. **Initial variables**: Via prompt injection. Native subagents share the parent session's hooks, so they operate under the parent's session variables and rule enforcement. No separate variable initialization needed.

3. **Step workflow enforcement**: Works naturally — native subagents are in-process, so their tool calls fire hooks on the parent session. Rules evaluate against parent's session variables. **Limitation**: step transitions (current_step, step_action_count) are session-wide, not scoped per subagent. Complex step workflows may not work correctly for concurrent native subagents.

4. **SUBAGENT_START metadata**: Only provides `agent_id`, `subagent_id`, `session_id`. For Phase 0 (rule isolation), this is sufficient — we only need to toggle `is_subagent` on the parent session. No `agent_runs` record, no `spawn_agent` involvement. Phase 0 is purely about Claude Code's native subagents working smoothly with Gobby's rule engine. The two-phase spawn pattern with `agent_runs` matching is a Phase 0.5 concern (future delegation feature, tracked as #11216).

#### Phase 1: Orphaned `.claude/worktrees/` Cleanup Rule (Opt-In)

A rule template (disabled by default) that detects and cleans stale `.claude/worktrees/` directories older than 24 hours. Users opt in by enabling the rule.

**New rule file**: `src/gobby/install/shared/workflows/rules/worktree-cleanup/cleanup-claude-worktrees.yaml`

```yaml
tags: [maintenance, gobby]
enabled: false  # Opt-in — syncs as disabled, users enable via DB

rules:
  cleanup-claude-worktrees:
    description: "Clean orphaned .claude/worktrees/ directories older than 24h"
    event: session_start
    enabled: false
    effects:
      - type: mcp_call
        server_name: gobby-worktrees
        tool_name: cleanup_claude_worktrees
        arguments:
          max_age_hours: 24
        background: true
```

**Implementation**: Add a `cleanup_claude_worktrees` tool to the worktrees MCP server that:
- Scans `.claude/worktrees/` in the project root
- Identifies directories with mtime > 24h
- Checks each for uncommitted changes (`git status --porcelain`)
- Deletes clean worktrees + their branches
- Logs but skips dirty worktrees

**Files**:
- `src/gobby/install/shared/workflows/rules/worktree-cleanup/cleanup-claude-worktrees.yaml` (new rule template, `enabled: false`)
- `src/gobby/mcp_proxy/tools/worktrees/` (new tool implementation)

#### Phase 2: Document the Two-System Architecture (Skill/Docs)

Create a skill explaining the relationship:

| Need | Use |
|------|-----|
| Quick throwaway isolation for Claude-only | Claude Code's `isolation: "worktree"` on Agent tool |
| Managed isolation with task linking, merge, cleanup | Gobby's `spawn_agent(isolation="worktree")` |
| Multi-provider agents | Gobby's `spawn_agent()` or pipelines with `dispatch_batch` |
| Simple in-process subagent | Claude Code's Agent tool (no isolation) |
| Orchestrated multi-step workflows | Gobby pipelines + agents |
| P2P cross-process messaging | Gobby's `send_message()` / `send_command()` |
| Agent Teams within single Claude session | Claude Code's native Team tools |

Key guidance:
- Claude Code's Agent tool is great for lightweight, disposable subagent work
- Gobby's agent system is for persistent, tracked, multi-provider orchestration
- Don't use both isolation systems simultaneously — pick one per agent spawn
- Gobby's task rules intentionally block Claude Code's native `TaskCreate` etc.

#### Phase 3: Evaluate Lightweight Claude Agent Mode (Future Research)

Tracked as task #11216.

---

## What NOT to Simplify

These Gobby features have no Claude Code equivalent and must stay:

1. **Multi-provider agent spawning** (Gemini, Codex) — Claude Code only spawns Claude
2. **Step workflows** with tool restrictions per step — no equivalent
3. **Pipeline executor** — deterministic sequential execution with approval gates
4. **Rule engine** — declarative event-driven enforcement
5. **P2P messaging** (cross-process, cross-provider) — Claude Code's is in-process only
6. **Clone isolation** — Claude Code has no clone equivalent
7. **Agent lifecycle monitor** — Claude Code's cleanup is process-scoped, Gobby's survives restarts
8. **DB-backed worktree tracking** — stale detection, merge workflows, task linking
9. **Task validation gates** — diff-based validation on close
10. **Memory system** — persistent facts across sessions

---

## Verification

Phase 1 (orphaned worktree cleanup):
1. Create a stale `.claude/worktrees/agent-*` directory manually
2. Run maintenance cycle — verify it's detected and cleaned
3. Create one with uncommitted changes — verify it's NOT cleaned without force
4. Run existing worktree tests: `uv run pytest tests/worktrees/ -v`

Phase 2 (documentation):
1. Verify skill is searchable via `search_skills(query="claude code worktree")`
2. Review content for accuracy against current Claude Code docs

## Summary

**The overlap between Claude Code and Gobby is conceptual, not functional.** Both systems have worktrees, agents, tasks, and messaging — but they serve fundamentally different scopes:

- **Claude Code**: Single-provider (Claude), in-process, ephemeral, lightweight
- **Gobby**: Multi-provider, cross-process, persistent, lifecycle-managed, rule-enforced

The two systems are **complementary, not redundant**. There is no significant code to remove from Gobby. The main action items are:
1. Clean up orphaned `.claude/worktrees/` from Claude Code's native agent tool
2. Document when to use which system
3. Future: investigate whether Gobby can leverage Claude Code's Agent tool for lightweight spawns
