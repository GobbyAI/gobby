# Rule-First Workflow Engine (Strangler Fig)

## Context

The workflow engine is overengineered. Three separate evaluation paths (step workflows, lifecycle workflows, named rule resolution) all implement the same pattern: `event + predicate → effect`. An 848-line lifecycle evaluator, a step transition engine with entry/exit actions, pipelines-inside-step-workflows (never tested) — all for a system where the only things in active use are:

1. **session-lifecycle.yaml** — a 472-line monolith bundling 7+ independent concerns that can't be individually toggled
2. **auto-task** — a stop gate that prevents agents from stopping until tasks are closed

Simple things should be simple. A pipeline that spawns a CLI, sends a prompt, and kills itself shouldn't require polling gymnastics. Enforcing "commit before close" shouldn't require understanding three evaluation paths. The current setup makes easy things hard and hard things impossible to debug.

**Goal**: Replace the overengineered evaluation paths with a flat `RuleEngine`. Rules are stateless event handlers: event comes in, conditions match, effect fires. No state machine, no transition engine, no 3-way merge. Individually toggleable, testable, debuggable.

**Key design decision**: Rules go in the existing `workflow_definitions` table as `workflow_type = 'rule'`, alongside workflows and pipelines. This gives us the entire existing CRUD infrastructure for free: HTTP API, MCP tools, CLI, Web UI, soft delete, bundled sync.

The legacy `rules` table, `RuleStore`, `rule_sync.py`, and `check_rules` resolution path are unused — they get removed. worker-safety.yaml and auto-task migrate to the new rule format. The step/lifecycle type distinction is dead — there are just definitions of different types sharing one table.

## Phase 1: Rule Model and Storage

### 1.1 Define RuleEffect model

**File**: `src/gobby/workflows/definitions.py`

```python
class RuleEvent(str, Enum):
    BEFORE_TOOL = "before_tool"
    AFTER_TOOL = "after_tool"
    BEFORE_AGENT = "before_agent"
    SESSION_START = "session_start"
    SESSION_END = "session_end"
    STOP = "stop"
    PRE_COMPACT = "pre_compact"

class RuleEffect(BaseModel):
    """What happens when a rule fires. Four primitive effect types."""
    type: Literal["block", "set_variable", "inject_context", "mcp_call"]

    # block — prevent the action
    reason: str | None = None
    tools: list[str] | None = None
    mcp_tools: list[str] | None = None
    command_pattern: str | None = None
    command_not_pattern: str | None = None

    # set_variable — update session/workflow state
    variable: str | None = None
    value: Any = None  # Supports expressions: "variables.get('x', 0) + 1"

    # inject_context — add text to system message
    template: str | None = None

    # mcp_call — call an MCP tool (replaces hardcoded execute_action)
    server: str | None = None
    tool: str | None = None
    arguments: dict[str, Any] | None = None
    background: bool = False  # Run asynchronously without blocking

class RuleDefinitionBody(BaseModel):
    """Stored as definition_json in workflow_definitions for workflow_type='rule'."""
    event: RuleEvent
    when: str | None = None
    match: dict[str, Any] | None = None
    effect: RuleEffect
    group: str | None = None
```

**Why only four effects:** Every hardcoded action in `ActionExecutor` decomposes into these primitives. Memory ops, task sync, handoff generation → `mcp_call`. State tracking, counters → `set_variable`. Observers → `set_variable` rules on specific events. The plugin system (`src/gobby/hooks/plugins.py`) is removed — MCP tools ARE the extension mechanism.

### 1.2 Storage: workflow_definitions table

No schema migration needed for the main table. Rules reuse existing columns:

| Column | Rule usage |
|---|---|
| `name` | Rule name (e.g., "require-task-before-edit") |
| `description` | Rule description |
| `workflow_type` | `'rule'` |
| `enabled` | Toggle individual rules |
| `priority` | Evaluation order (lower = first) |
| `sources` | JSON array of CLI sources |
| `definition_json` | Serialized `RuleDefinitionBody` |
| `tags` | JSON array for group/category filtering |
| `source` | `'bundled'` / `'custom'` |
| `project_id` | Project scoping |
| `deleted_at` | Soft delete |

Existing `LocalWorkflowDefinitionManager` CRUD works as-is. Existing MCP tools (`list_workflows`, `create_workflow`, `get_workflow`, etc.) work with `workflow_type='rule'` filtering.

### 1.3 Session-scoped overrides

**File**: `src/gobby/storage/migrations.py` — one small migration:

```sql
CREATE TABLE rule_overrides (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    rule_name TEXT NOT NULL,
    enabled BOOLEAN NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(session_id, rule_name)
);
```

Lets agents/users disable a bundled rule for a specific session without affecting others.

## Phase 2: RuleEngine Core

### 2.1 Build the RuleEngine

**New file**: `src/gobby/workflows/rule_engine.py`

Single-pass evaluation loop:
1. Load enabled rules from `workflow_definitions` where `workflow_type='rule'`, filtered by event type + source, sorted by priority
2. Apply session-scoped overrides from `rule_overrides`
3. Build eval context once (reuse `_build_eval_context` logic from `engine_context.py`)
4. Iterate rules in priority order — check `match`, evaluate `when`, apply effect
5. First block wins. Context/system_messages accumulate.
6. Rebuild eval context after any variable changes (for downstream rules in same pass)
7. Persist state once at end

Reuses existing infrastructure:
- `SafeExpressionEvaluator` for `when` condition evaluation and `set_variable` expressions
- `block_tools()` from `enforcement/blocking.py` for `block` effects (tool matching, command patterns)
- `LazyBool` thunks for expensive checks (task_has_commits, task_tree_complete, etc.)
- MCP proxy (`MCPClientManager`) for `mcp_call` effects
- Template rendering for `inject_context` templates

### 2.2 Wire into WorkflowHookHandler (dual evaluation)

**File**: `src/gobby/workflows/hooks.py`

```python
def evaluate(self, event):
    # New rules evaluate first
    rule_response = self.rule_engine.evaluate(event)
    if rule_response.decision == "block":
        return rule_response

    # Legacy lifecycle evaluation (handles not-yet-migrated concerns)
    legacy_response = self.engine.evaluate_all_lifecycle_workflows(event)

    # Merge: blocks win, context accumulates
    return merge_responses(rule_response, legacy_response)
```

Disagreement logging: when both engines return decisions, log any conflicts.

## Phase 3: Rule Sync and YAML Format

### Rule YAML format

**Directory**: `src/gobby/install/shared/rules/`

Each file contains one or more rules. File-level fields (`group`, `tags`, `sources`, `priority`) apply as defaults to all rules within. This extends the existing `worker-safety.yaml` pattern with the new columns.

```yaml
# tool-hygiene.yaml
group: tool-hygiene
tags: [enforcement, python]
sources: [claude, gemini, codex, antigravity, cursor, windsurf, copilot]

rules:
  require-uv:
    description: "Block naked python/pip - require uv run/uv pip"
    event: before_tool
    priority: 50
    when: "variables.get('require_uv')"
    effect:
      type: block
      tools: [Bash]
      command_pattern: "(?:^|[;&|])\\s*(?:sudo\\s+)?(?:python(?:3(?:\\.\\d+)?)?|pip3?)\\b"
      command_not_pattern: "(?:^|[;&|])\\s*(?:sudo\\s+)?uv\\s+"
      reason: |
        Use `uv run` or `uv pip` instead of running python/pip directly.
```

### Update bundled sync

**File**: `src/gobby/workflows/loader_sync.py`

Extend bundled sync to handle rule YAML files from `src/gobby/install/shared/rules/`:
- Detect `type: rule` or presence of `rules` dict with `event`/`effect` fields → parse as rule group
- Each rule in the `rules` dict becomes a row in `workflow_definitions` with `workflow_type='rule'`
- File-level `group`, `tags`, `sources` are inherited by each rule as defaults
- Uses existing `LocalWorkflowDefinitionManager` upsert (same as workflow sync)

Legacy `rule_definitions` format (worker-safety.yaml) is migrated to the new rule format. The old `rules` table, `RuleStore`, `rule_sync.py`, and `check_rules` resolution in `engine_context.py` are removed.

### Session variable defaults

**New file**: `src/gobby/install/shared/rules/session-defaults.yaml`

```yaml
session_variables:
  chat_mode: bypass
  mode_level: 2
  unlocked_tools: []
  servers_listed: false
  listed_servers: []
  pre_existing_errors_triaged: false
  stop_attempts: 0
```

## Phase 4: Decompose session-lifecycle.yaml

Migrate one concern at a time. For each:
1. Create rule YAML file in `src/gobby/install/shared/rules/`
2. Remove the corresponding section from session-lifecycle.yaml
3. Restart daemon, verify behavior is identical via disagreement logs

### Migration order (simplest/most self-contained first):

| Order | Group | Rules | Key dependencies |
|-------|-------|-------|-----------------|
| 4a | tool-hygiene | 2 | reads `require_uv` variable |
| 4b | plan-mode | 5 | sets `plan_mode`, `mode_level` (read by many) |
| 4c | progressive-disclosure | 8 | reads/writes `unlocked_tools`, `servers_listed`, `listed_servers` |
| 4d | task-enforcement | 8 | reads `task_claimed`, `plan_mode`; tracks claim/release events |
| 4e | stop-gates | 11 | reads `_tool_block_pending`, `task_claimed`, `pre_existing_errors_triaged`, `stop_attempts` |
| 4f | memory-lifecycle | 10 | calls complex actions (memory_recall_with_synthesis, background digest) |
| 4g | context-handoff | 18 | calls complex actions (inject_context, generate_handoff) with templates |
| 4h | auto-task | 3 | **Critical** — enables overnight autonomous work. `task_tree_complete()`, `guide_continuation`. Migrate last, validate extensively before cutting over. |

Each group becomes its own YAML file under `src/gobby/install/shared/rules/`.

**Auto-task migration note**: auto-task.yaml is the only step workflow in active use. Its behavior maps 1:1 to rules: `task_tree_complete()` (already in eval context), `guide_continuation` (already an action), `suggest_next_task()` (already an MCP tool). The end-state rule set fully replaces auto-task — no functionality is lost.

### Concrete rule files

**Note on compatibility**: The 9 `block_tools` rules from session-lifecycle.yaml already match the current blocking model (tools, mcp_tools, when, reason, command_pattern). They could go in `workflow_definitions` as `workflow_type='rule'` today — the only gap is the evaluation path. Non-blocking effects (set_variable, inject_context, mcp_call) require the new `RuleEffect` model.

#### `tool-hygiene.yaml`

```yaml
group: tool-hygiene
tags: [enforcement, python]

rules:
  require-uv:
    description: "Block naked python/pip - require uv run/uv pip"
    event: before_tool
    priority: 50
    when: "variables.get('require_uv')"
    effect:
      type: block
      tools: [Bash]
      command_pattern: "(?:^|[;&|])\\s*(?:sudo\\s+)?(?:python(?:3(?:\\.\\d+)?)?|pip3?)\\b"
      command_not_pattern: "(?:^|[;&|])\\s*(?:sudo\\s+)?uv\\s+"
      reason: |
        Use `uv run` or `uv pip` instead of running python/pip directly.

  track-pending-memory-review:
    description: "Flag session for memory review after file edits or task close"
    event: after_tool
    priority: 90
    when: "event.data.get('tool_name') in ['Edit', 'Write', 'NotebookEdit'] or event.data.get('mcp_tool') == 'close_task'"
    effect:
      type: set_variable
      variable: pending_memory_review
      value: true
```

#### `plan-mode.yaml`

```yaml
group: plan-mode
tags: [state-tracking]

rules:
  detect-plan-mode-enter:
    description: "Set plan_mode=true when EnterPlanMode is called"
    event: after_tool
    priority: 10
    when: "event.data.get('tool_name') == 'EnterPlanMode'"
    effect:
      type: set_variable
      variable: plan_mode
      value: true

  set-mode-level-on-enter:
    description: "Set mode_level=0 (plan) when entering plan mode"
    event: after_tool
    priority: 11
    when: "event.data.get('tool_name') == 'EnterPlanMode'"
    effect:
      type: set_variable
      variable: mode_level
      value: 0

  detect-plan-mode-exit:
    description: "Set plan_mode=false when ExitPlanMode is called"
    event: after_tool
    priority: 10
    when: "event.data.get('tool_name') == 'ExitPlanMode'"
    effect:
      type: set_variable
      variable: plan_mode
      value: false

  restore-mode-level-on-exit:
    description: "Restore mode_level from chat_mode when exiting plan mode"
    event: after_tool
    priority: 11
    when: "event.data.get('tool_name') == 'ExitPlanMode'"
    effect:
      type: set_variable
      variable: mode_level
      value: "{{ {'plan': 0, 'accept_edits': 1, 'normal': 1}.get(variables.get('chat_mode', 'bypass'), 2) }}"

  reset-plan-mode-on-session-start:
    description: "Clear plan_mode on new/clear/compact sessions"
    event: session_start
    priority: 10
    when: "event.data.get('source') in ['clear', 'compact', 'startup']"
    effect:
      type: set_variable
      variable: plan_mode
      value: false
```

#### `progressive-disclosure.yaml`

```yaml
group: progressive-disclosure
tags: [enforcement, mcp]

rules:
  require-servers-listed:
    description: "Block list_tools without prior list_mcp_servers"
    event: before_tool
    priority: 20
    when: "variables.get('enforce_tool_schema_check') and not variables.get('servers_listed')"
    effect:
      type: block
      tools: ["mcp__gobby__list_tools"]
      reason: "Call list_mcp_servers() first to discover available servers, then retry list_tools."

  require-server-listed-for-schema:
    description: "Block get_tool_schema without prior list_tools for that server"
    event: before_tool
    priority: 21
    when: "variables.get('enforce_tool_schema_check') and not is_server_listed(tool_input)"
    effect:
      type: block
      tools: ["mcp__gobby__get_tool_schema"]
      reason: |
        Call list_tools(server_name="{{ tool_input.get('server_name', '') }}") first, then retry get_tool_schema.

  require-schema-before-call:
    description: "Block call_tool without prior get_tool_schema"
    event: before_tool
    priority: 22
    when: "variables.get('enforce_tool_schema_check') and tool_input.get('arguments') and not is_discovery_tool(tool_input.get('tool_name')) and not is_tool_unlocked(tool_input)"
    effect:
      type: block
      tools: ["mcp__gobby__call_tool"]
      reason: |
        Schema required. Call get_tool_schema("{{ tool_input.get('server_name', '') }}", "{{ tool_input.get('tool_name') }}") first, then retry.

  track-schema-lookup:
    description: "Record get_tool_schema calls to unlock tools"
    event: after_tool
    priority: 20
    when: "event.data.get('mcp_tool') == 'get_tool_schema'"
    effect:
      type: set_variable
      variable: unlocked_tools
      value: "variables.get('unlocked_tools', []) + [tool_input.get('server_name', '') + ':' + tool_input.get('tool_name', '')]"

  track-servers-listed:
    description: "Mark servers as listed after list_mcp_servers"
    event: after_tool
    priority: 21
    when: "event.data.get('mcp_tool') == 'list_mcp_servers'"
    effect:
      type: set_variable
      variable: servers_listed
      value: true

  track-listed-servers:
    description: "Record which servers have been listed via list_tools"
    event: after_tool
    priority: 22
    when: "event.data.get('mcp_tool') == 'list_tools'"
    effect:
      type: set_variable
      variable: listed_servers
      value: "variables.get('listed_servers', []) + [tool_input.get('server_name', '')]"

  reset-unlocked-tools:
    description: "Clear unlocked_tools on context loss"
    event: session_start
    priority: 80
    when: "event.data.get('source') in ['clear', 'compact'] or (event.data.get('source') == 'resume' and variables.get('pending_context_reset'))"
    effect:
      type: set_variable
      variable: unlocked_tools
      value: []

  reset-servers-listed:
    description: "Clear servers_listed on context loss"
    event: session_start
    priority: 81
    when: "event.data.get('source') in ['clear', 'compact'] or (event.data.get('source') == 'resume' and variables.get('pending_context_reset'))"
    effect:
      type: set_variable
      variable: servers_listed
      value: false

  reset-listed-servers:
    description: "Clear listed_servers on context loss"
    event: session_start
    priority: 82
    when: "event.data.get('source') in ['clear', 'compact'] or (event.data.get('source') == 'resume' and variables.get('pending_context_reset'))"
    effect:
      type: set_variable
      variable: listed_servers
      value: []
```

#### `task-enforcement.yaml`

```yaml
group: task-enforcement
tags: [enforcement, tasks]

rules:
  block-native-task-tools:
    description: "Block CC native task/todo tools - use gobby-tasks instead"
    event: before_tool
    priority: 30
    effect:
      type: block
      tools: [TaskCreate, TaskUpdate, TaskGet, TaskList, TodoWrite]
      reason: |
        CC native task tools are disabled. Use gobby-tasks MCP tools instead:
        - create_task(title, description, session_id, claim=True)
        - claim_task(task_id, session_id)
        - list_ready_tasks()

  require-task-before-edit:
    description: "Block file edits without active task"
    event: before_tool
    priority: 31
    when: "variables.get('require_task_before_edit') and not task_claimed and not (plan_mode and is_plan_file(tool_input.get('file_path', ''), source))"
    effect:
      type: block
      tools: [Edit, Write, NotebookEdit]
      reason: |
        You must create or claim a task before editing files.
        - create_task(title, description, session_id, claim=True)
        - claim_task(task_id, session_id)

  require-commit-before-close:
    description: "Require linked commit before close_task"
    event: before_tool
    priority: 32
    when: "variables.get('require_commit_before_close') and not task_has_commits and not tool_input.get('commit_sha') and tool_input.get('reason') not in ['already_implemented', 'obsolete', 'duplicate', 'wont_fix', 'out_of_repo']"
    effect:
      type: block
      mcp_tools: ["gobby-tasks:close_task"]
      reason: |
        A commit is required before closing this task.
        1. git commit -m "[{{ project.name }}-#N] description"
        2. close_task(task_id="#N", commit_sha="<sha>")

  block-skip-validation-with-commit:
    description: "Block skip_validation when commits exist"
    event: before_tool
    priority: 33
    when: "tool_input.get('skip_validation') and (task_has_commits or tool_input.get('commit_sha'))"
    effect:
      type: block
      mcp_tools: ["gobby-tasks:close_task"]
      reason: "skip_validation is not allowed when a commit is attached. Close without skip_validation."

  block-ask-during-stop-compliance:
    description: "Block AskUserQuestion when stop hook gave directive"
    event: before_tool
    priority: 34
    when: "variables.get('stop_attempts', 0) > 0 and task_claimed"
    effect:
      type: block
      tools: [AskUserQuestion]
      reason: |
        Do not ask — act on the hook directive:
        1. git commit -m "[{{ project.name }}-#N] description"
        2. close_task(task_id="#N", commit_sha="<sha>")
        3. Then stop.

  track-task-claim:
    description: "Set task_claimed when agent claims a task"
    event: after_tool
    priority: 30
    when: "event.data.get('mcp_tool') in ['claim_task', 'create_task'] and not event.data.get('error')"
    effect:
      type: set_variable
      variable: task_claimed
      value: true

  track-task-release:
    description: "Clear task_claimed when agent closes/releases task"
    event: after_tool
    priority: 31
    when: "event.data.get('mcp_tool') in ['close_task', 'release_task'] and not event.data.get('error')"
    effect:
      type: set_variable
      variable: task_claimed
      value: false
```

#### `stop-gates.yaml`

```yaml
group: stop-gates
tags: [enforcement, lifecycle]

rules:
  increment-stop-attempts:
    description: "Count consecutive stop attempts for escape hatch"
    event: stop
    priority: 10
    effect:
      type: set_variable
      variable: stop_attempts
      value: "variables.get('stop_attempts', 0) + 1"

  block-stop-after-tool-block:
    description: "Block stop when a tool was just blocked"
    event: stop
    priority: 20
    when: "variables.get('_tool_block_pending') and variables.get('stop_attempts', 0) < variables.get('max_stop_attempts', 3)"
    effect:
      type: block
      reason: "Do not stop. A tool was blocked — follow the instructions in the error message."

  require-error-triage:
    description: "Block stop until pre-existing errors are triaged"
    event: stop
    priority: 30
    when: "task_has_commits and not variables.get('pre_existing_errors_triaged') and variables.get('stop_attempts', 0) < variables.get('max_stop_attempts', 3)"
    effect:
      type: block
      reason: |
        Triage pre-existing issues before stopping. Create tasks for unrelated errors,
        or confirm none via set_variable(name="pre_existing_errors_triaged", value=true).

  memory-review-gate:
    description: "Block stop until memory review when significant work done"
    event: stop
    priority: 40
    when: "variables.get('pending_memory_review') and (variables.get('stop_attempts', 0) or 0) < 3"
    effect:
      type: block
      reason: |
        You've made significant changes this session. Before stopping,
        review and save valuable memories using create_memory on gobby-memory.

  require-task-close:
    description: "Block stop if task is still in_progress"
    event: stop
    priority: 50
    when: "variables.get('mode_level', 2) >= 1 and task_claimed and variables.get('stop_attempts', 0) < variables.get('max_stop_attempts', 3)"
    effect:
      type: block
      reason: |
        Task {{ task_ref }} is still in_progress. Commit and close_task().

  reset-stop-attempts-on-prompt:
    description: "Reset stop counter on new user prompt"
    event: before_agent
    priority: 10
    effect:
      type: set_variable
      variable: stop_attempts
      value: 0

  clear-tool-block-on-prompt:
    description: "Clear tool block flag on new user prompt"
    event: before_agent
    priority: 11
    effect:
      type: set_variable
      variable: _tool_block_pending
      value: false

  reset-error-triage-on-prompt:
    description: "Reset triage flag — agents must re-confirm each interaction"
    event: before_agent
    priority: 12
    effect:
      type: set_variable
      variable: pre_existing_errors_triaged
      value: false

  reset-stop-on-native-tool:
    description: "Reset stop counter on successful native tool use"
    event: after_tool
    priority: 80
    when: "not event.data.get('mcp_tool')"
    effect:
      type: set_variable
      variable: stop_attempts
      value: 0

  clear-tool-block-on-tool:
    description: "Clear tool block flag on successful tool use"
    event: after_tool
    priority: 81
    effect:
      type: set_variable
      variable: _tool_block_pending
      value: false
```

#### `memory-lifecycle.yaml`

```yaml
group: memory-lifecycle
tags: [memory, lifecycle]

rules:
  memory-recall-on-prompt:
    description: "Search relevant memories on each user prompt"
    event: before_agent
    priority: 50
    effect:
      type: mcp_call
      server: gobby-memory
      tool: recall_with_synthesis
      arguments:
        limit: 5

  memory-background-digest:
    description: "Update digest and synthesize memories in background"
    event: before_agent
    priority: 51
    effect:
      type: mcp_call
      server: gobby-memory
      tool: background_digest_and_synthesize
      arguments:
        limit: 20
      background: true

  memory-capture-nudge:
    description: "Remind agent to save user preferences/facts"
    event: before_agent
    priority: 60
    when: "len((event.data.get('prompt') or '').strip()) >= 10 and not (event.data.get('prompt') or '').strip().startswith('/')"
    effect:
      type: inject_context
      template: |
        If the user just told you something worth remembering across sessions
        (a preference, fact, convention, or instruction), save it with
        create_memory on gobby-memory. If not, carry on.

  suggest-memory-after-close:
    description: "Nudge memory extraction after closing a task with commit"
    event: after_tool
    priority: 85
    when: "event.data.get('mcp_tool') == 'close_task' and ((event.data.get('tool_input') or {}).get('arguments') or {}).get('commit_sha')"
    effect:
      type: inject_context
      template: |
        Consider saving valuable memories from this task before stopping.
        Use `create_memory` via gobby-memory. If nothing new, no action needed.

  memory-sync-import:
    description: "Import memories from JSONL on session start"
    event: session_start
    priority: 90
    effect:
      type: mcp_call
      server: gobby-memory
      tool: sync_import

  memory-sync-export-on-end:
    description: "Export memories to JSONL on session end"
    event: session_end
    priority: 80
    effect:
      type: mcp_call
      server: gobby-memory
      tool: sync_export

  memory-extraction-on-end:
    description: "Extract memories from session on end"
    event: session_end
    priority: 70
    effect:
      type: mcp_call
      server: gobby-memory
      tool: extract_from_session
      arguments:
        max_memories: 5

  memory-extraction-on-compact:
    description: "Extract memories before context compaction"
    event: pre_compact
    priority: 70
    when: "event.source.value != 'gemini' or event.data.get('trigger') == 'manual'"
    effect:
      type: mcp_call
      server: gobby-memory
      tool: extract_from_session
      arguments:
        max_memories: 5

  memory-sync-export-on-compact:
    description: "Export memories to JSONL before compaction"
    event: pre_compact
    priority: 80
    when: "event.source.value != 'gemini' or event.data.get('trigger') == 'manual'"
    effect:
      type: mcp_call
      server: gobby-memory
      tool: sync_export

  reset-memory-tracking:
    description: "Reset memory injection tracking on context loss"
    event: session_start
    priority: 85
    when: "event.data.get('source') in ['clear', 'compact'] or (event.data.get('source') == 'resume' and variables.get('pending_context_reset'))"
    effect:
      type: set_variable
      variable: _memory_injected_ids
      value: []
```

#### `context-handoff.yaml`

```yaml
group: context-handoff
tags: [lifecycle, handoff]

rules:
  inject-previous-session-summary:
    description: "Inject previous session summary on clear"
    event: session_start
    priority: 50
    when: "event.data.get('source') == 'clear'"
    effect:
      type: inject_context
      source: previous_session_summary
      require: true
      template: |
        ## Previous Session Context
        *Injected by Gobby session handoff*

        {{ summary }}

  inject-compact-handoff:
    description: "Inject compact handoff after compaction"
    event: session_start
    priority: 50
    when: "event.data.get('source') == 'compact'"
    effect:
      type: inject_context
      source: compact_handoff
      require: true
      template: |
        ## Continuation Context
        *Injected by Gobby compact handoff*

        {{ handoff }}

  inject-skills-guide:
    description: "Inject always-apply skills on session start"
    event: session_start
    priority: 60
    when: "event.data.get('source') != 'resume'"
    effect:
      type: inject_context
      source: skills
      filter: always_apply
      template: "{{ skills_list }}"

  inject-task-context:
    description: "Inject active task context on session start"
    event: session_start
    priority: 61
    when: "event.data.get('source') != 'resume'"
    effect:
      type: inject_context
      source: task_context
      template: "{{ task_context }}"

  inject-error-triage-policy:
    description: "Inject pre-existing error triage policy"
    event: session_start
    priority: 62
    when: "event.data.get('source') != 'resume'"
    effect:
      type: inject_context
      template: |
        ## Pre-Existing Error/Warning/Failure Policy

        If you encounter ANY pre-existing issues during your work — errors,
        warnings, or failures unrelated to your changes — you MUST create
        a gobby task for each distinct issue before stopping.

  capture-baseline-dirty-files:
    description: "Capture baseline git status for commit detection"
    event: session_start
    priority: 40
    effect:
      type: mcp_call
      server: gobby-sessions
      tool: capture_baseline_dirty_files

  task-sync-import:
    description: "Import tasks from JSONL on session start"
    event: session_start
    priority: 91
    effect:
      type: mcp_call
      server: gobby-tasks
      tool: sync_import

  generate-session-end-handoff:
    description: "Generate session summary on end"
    event: session_end
    priority: 50
    effect:
      type: mcp_call
      server: gobby-sessions
      tool: generate_handoff
      arguments:
        include: [pending_tasks]
        prompt: handoff/session_end
        write_file: true

  task-sync-export-on-end:
    description: "Export tasks to JSONL on session end"
    event: session_end
    priority: 85
    effect:
      type: mcp_call
      server: gobby-tasks
      tool: sync_export

  extract-pre-compact-context:
    description: "Extract structured context before compaction"
    event: pre_compact
    priority: 50
    when: "event.source.value != 'gemini' or event.data.get('trigger') == 'manual'"
    effect:
      type: mcp_call
      server: gobby-sessions
      tool: extract_handoff_context

  generate-compact-handoff:
    description: "Generate LLM summary for compaction"
    event: pre_compact
    priority: 90
    when: "event.source.value != 'gemini' or event.data.get('trigger') == 'manual'"
    effect:
      type: mcp_call
      server: gobby-sessions
      tool: generate_handoff
      arguments:
        mode: compact
        prompt: handoff/compact
        write_file: true

  task-sync-export-on-compact:
    description: "Export tasks to JSONL before compaction"
    event: pre_compact
    priority: 85
    when: "event.source.value != 'gemini' or event.data.get('trigger') == 'manual'"
    effect:
      type: mcp_call
      server: gobby-tasks
      tool: sync_export

  clear-pending-context-reset:
    description: "Clear pending_context_reset flag after use"
    event: session_start
    priority: 99
    when: "variables.get('pending_context_reset')"
    effect:
      type: set_variable
      variable: pending_context_reset
      value: false

  set-pending-context-reset:
    description: "Flag context reset for Gemini resume flow"
    event: pre_compact
    priority: 40
    when: "event.source.value != 'gemini' or event.data.get('trigger') == 'manual'"
    effect:
      type: set_variable
      variable: pending_context_reset
      value: true

  reset-progressive-disclosure-on-compact:
    description: "Reset progressive disclosure state before compaction"
    event: pre_compact
    priority: 41
    when: "event.source.value != 'gemini' or event.data.get('trigger') == 'manual'"
    effect:
      type: set_variable
      variable: unlocked_tools
      value: []

  reset-servers-listed-on-compact:
    event: pre_compact
    priority: 42
    when: "event.source.value != 'gemini' or event.data.get('trigger') == 'manual'"
    effect:
      type: set_variable
      variable: servers_listed
      value: false

  reset-listed-servers-on-compact:
    event: pre_compact
    priority: 43
    when: "event.source.value != 'gemini' or event.data.get('trigger') == 'manual'"
    effect:
      type: set_variable
      variable: listed_servers
      value: []

  reset-memory-tracking-on-compact:
    event: pre_compact
    priority: 44
    when: "event.source.value != 'gemini' or event.data.get('trigger') == 'manual'"
    effect:
      type: set_variable
      variable: _memory_injected_ids
      value: []
```

#### `auto-task.yaml`

```yaml
group: auto-task
tags: [enforcement, autonomous]

rules:
  inject-autonomous-mode:
    description: "Inject autonomous task execution context when session_task is set"
    event: session_start
    priority: 30
    when: "variables.get('session_task')"
    effect:
      type: inject_context
      template: |
        ## Autonomous Task Execution Mode
        You are working on task {{ variables.session_task }}.
        Use suggest_next_task() to find available work.
        Task will be marked complete when all subtasks are closed.
        Do not wait for user confirmation to proceed.

  guide-task-continuation:
    description: "Prevent stop when task tree is incomplete"
    event: stop
    priority: 45
    when: "variables.get('session_task') and not task_tree_complete(variables.get('session_task')) and variables.get('stop_attempts', 0) < variables.get('premature_stop_max_attempts', 3)"
    effect:
      type: block
      reason: |
        Task has incomplete subtasks. Use suggest_next_task()
        and continue working. Do not wait for user confirmation to proceed.

  notify-task-tree-complete:
    description: "Notify when entire task tree is done"
    event: after_tool
    priority: 90
    when: "variables.get('session_task') and event.data.get('mcp_tool') == 'close_task' and task_tree_complete(variables.get('session_task'))"
    effect:
      type: inject_context
      template: |
        All tasks in the tree for {{ variables.session_task }} are complete.
        You may stop when ready.
```

## Phase 5: Interaction Surfaces

Rules reuse existing CRUD infrastructure from `LocalWorkflowDefinitionManager` (`workflow_type='rule'` filtering). New interaction surfaces are thin wrappers with rule-specific conveniences.

### 5.1 MCP Tools (Agent interaction)

**File**: `src/gobby/mcp_proxy/tools/workflows/rules.py`

Thin wrappers around existing `LocalWorkflowDefinitionManager` with rule-specific filters:

| Tool | Purpose | Wraps |
|------|---------|-------|
| `list_rules` | Discover active rules | `manager.list_definitions(workflow_type='rule')` + event/group/enabled filters via `definition_json` |
| `get_rule` | Full rule details | `manager.get_definition(name, workflow_type='rule')` |
| `toggle_rule` | Enable/disable a rule | `manager.update_definition(enabled=...)` or `rule_overrides` insert for session-scoped |
| `create_rule` | Create a custom rule | `manager.create_definition(workflow_type='rule', definition_json=...)` |
| `delete_rule` | Soft-delete a custom rule | `manager.soft_delete_definition()` (bundled protected) |

Agent use cases:
- **Diagnostics**: `list_rules(event="before_tool")` → see what would block a tool call
- **Temporary override**: `toggle_rule("require-task-before-edit", enabled=false, session_id="#1619")`
- **Project customization**: `create_rule(name="no-deprecated-imports", event="before_tool", ...)`

### 5.2 HTTP API (Web UI backend)

**File**: `src/gobby/servers/routes/rules.py`

Routes wrapping existing `LocalWorkflowDefinitionManager` with `workflow_type='rule'` filter:

```
GET    /api/rules                    — list definitions where workflow_type='rule', with event/group/enabled filters
POST   /api/rules                    — create rule definition
GET    /api/rules/:name              — get rule definition
PUT    /api/rules/:name              — update rule definition_json/columns
DELETE /api/rules/:name              — soft-delete rule (bundled protected)
PUT    /api/rules/:name/toggle       — update enabled flag
GET    /api/rules/groups             — aggregate by group from definition_json, count enabled
POST   /api/rules/:name/overrides    — create session-scoped override (rule_overrides table)
DELETE /api/rules/:name/overrides    — remove session-scoped override
GET    /api/rules/audit/:session_id  — query workflow_audit_log for rule evaluations
```

### 5.3 Web UI (Tabbed Workflows Page)

Unify all behavioral definitions under one page with tabs keyed by `workflow_type`:

```
┌─────────────────────────────────────────────────────────┐
│ Workflows                                                │
│ ┌────────┬──────────────┬───────────┬────────┐           │
│ │ Rules  │ Transitions  │ Pipelines │ Agents │           │
│ └────────┴──────────────┴───────────┴────────┘           │
│                                                           │
│ [Rules tab selected]                    [+ New Rule]      │
│ ┌─────────┬──────────┬──────────┬──────────────┐         │
│ │All (35) │Groups (7)│Events (7)│  Search      │         │
│ └─────────┴──────────┴──────────┴──────────────┘         │
│                                                           │
│ > task-enforcement (6 rules)              [toggle all]    │
│ ┌───────────────────────────────────────────────────┐     │
│ │ * require-task-before-edit     before_tool  [x]   │     │
│ │   Block Edit/Write without claimed task           │     │
│ │   when: not task_claimed and not plan_mode        │     │
│ ├───────────────────────────────────────────────────┤     │
│ │ * require-commit-before-close  before_tool  [x]   │     │
│ │   Require linked commit before close_task         │     │
│ │   when: not task_has_commits                      │     │
│ └───────────────────────────────────────────────────┘     │
│                                                           │
│ > progressive-disclosure (8 rules)        [toggle all]    │
│ └───────────────────────────────────────────────────┘     │
└─────────────────────────────────────────────────────────┘
```

**Tab mapping:**

| Tab | `workflow_type` | Content |
|-----|----------------|---------|
| Rules | `'rule'` | Grouped cards with event badges, toggle, `when` condition |
| Transitions | `'workflow'` | Step workflows (developer.yaml, coordinator.yaml) — stateful multi-step |
| Pipelines | `'pipeline'` | Deterministic automation sequences |
| Agents | (agent_definitions) | Simplified agent configs (model, prompt, tools) |

**Rules tab specifics:**
- Each card: name, event badge, enabled toggle, description, `when` condition (monospace), source badge
- Click to expand: full effect config, priority, tags, session overrides
- Group-level: collapse/expand, toggle all, enabled/total count
- Filters: event type, group, source (bundled/custom), text search

Extends existing WorkflowsPage with tab routing. Each tab reuses the same card layout components with type-specific rendering.

### 5.4 CLI Commands

**New file**: `src/gobby/cli/rules.py`

```bash
gobby rules list [--group GROUP] [--event EVENT] [--enabled/--disabled]
gobby rules show NAME
gobby rules enable NAME [--session ID]
gobby rules disable NAME [--session ID]
gobby rules import FILE
gobby rules export [--group GROUP]
gobby rules audit [--session ID]
```

## Phase 6: Retire Legacy Paths

Once all session-lifecycle concerns are migrated and disagreement logs show zero conflicts:

1. Delete session-lifecycle.yaml and auto-task.yaml
2. Remove `lifecycle_evaluator.py` (trigger/observer evaluation)
3. Simplify `WorkflowHookHandler.evaluate()` to call only RuleEngine
4. Remove dual-evaluation merge logic
5. Remove `rules` table, `RuleStore`, `rule_sync.py`, `_resolve_check_rules()`
6. Evaluate what remains of step workflow engine (`engine.py`) — currently unused, candidate for removal or simplification

## Key Files

| File | Change |
|------|--------|
| `src/gobby/workflows/definitions.py` | Add RuleEvent, RuleEffect, RuleDefinitionBody models |
| `src/gobby/storage/migrations.py` | Add `rule_overrides` table, drop `rules` table |
| `src/gobby/storage/workflow_definitions.py` | Add rule-specific query helpers (filter by event/group from `definition_json`) |
| `src/gobby/workflows/rule_engine.py` | **New** — single-pass rule evaluation (4 effect types) |
| `src/gobby/workflows/hooks.py` | Wire dual evaluation |
| `src/gobby/workflows/loader_sync.py` | Extend bundled sync to handle rule YAML → `workflow_definitions` rows |
| `src/gobby/mcp_proxy/tools/workflows/rules.py` | **New** — MCP tools wrapping `LocalWorkflowDefinitionManager` |
| `src/gobby/mcp_proxy/tools/memory/` | Expose `sync_import`, `sync_export`, `extract_from_session` as MCP tools |
| `src/gobby/mcp_proxy/tools/tasks/` | Expose `sync_import`, `sync_export` as MCP tools |
| `src/gobby/mcp_proxy/tools/sessions/` | Expose `generate_handoff`, `extract_handoff_context`, `capture_baseline_dirty_files` |
| `src/gobby/servers/routes/rules.py` | **New** — HTTP API wrapping `LocalWorkflowDefinitionManager` |
| `src/gobby/cli/rules.py` | **New** — CLI commands |
| `web/src/components/WorkflowsPage.tsx` | Add tabbed layout (Rules, Transitions, Pipelines, Agents) |
| `src/gobby/install/shared/rules/*.yaml` | 8 rule set files + session-defaults |
| `src/gobby/install/shared/workflows/session-lifecycle.yaml` | **Removed** after migration |
| `src/gobby/install/shared/workflows/auto-task.yaml` | **Removed** after migration |
| `src/gobby/storage/rules.py` | **Removed** |
| `src/gobby/workflows/rule_sync.py` | **Removed** |
| `src/gobby/workflows/actions.py` | **Removed** — decomposed into primitives |
| `src/gobby/hooks/plugins.py` | **Removed** |
| `src/gobby/config/extensions.py` | **Removed** (plugin config) |
| `src/gobby/workflows/engine_context.py` | Remove `_resolve_check_rules()` |
| `src/gobby/storage/agent_commands.py` | **New** — command lifecycle CRUD |
| `src/gobby/mcp_proxy/tools/agent_messaging.py` | Rewrite: P2P messaging + command tools |
| `src/gobby/install/shared/rules/messaging.yaml` | **New** — push delivery + command enforcement rules |

## Verification

1. **Unit tests**: Each Rule evaluates correctly in isolation — event matching, condition evaluation, effect execution
2. **Integration test**: Dual evaluation through a full session lifecycle (start, tool calls, stop), assert zero disagreements
3. **Disagreement logging**: Production logging in dual mode — any disagreement = bug to fix before retiring legacy
4. **Smoke test**: Restart daemon, run a Claude Code session, verify progressive disclosure, task-before-edit, stop gates all work identically
5. **UI test**: Browse rules page, toggle a rule, verify it takes effect on next hook event
6. **P2P messaging**: `send_message` between non-parent-child sessions in same project succeeds; different project fails
7. **Push delivery**: Send message, trigger hook on recipient, verify context injection and `delivered_at` set
8. **Command lifecycle**: `send_command` with `allowed_tools: ["Read", "Grep"]` → verify child can't use Edit/Write → `complete_command` restores normal rules
9. **Command exit condition**: Send command with exit condition, meet condition, verify auto-completion
10. **Agent rules**: Spawn agent with agent-scoped rules, verify rules are active; spawn different agent type, verify different rules apply
11. **Agent simplification**: Create agent definition with 12-field model, spawn via simplified `spawn_agent`, verify instructions injected and rules activated

## Phase 7: Inter-Agent Messaging

Replaces `docs/plans/inter-agent-messaging.md`. The current messaging system has 5 hierarchy-locked tools (`send_to_parent`, `send_to_child`, `broadcast_to_children`, `poll_messages`, `mark_message_read`) that only work between parent-child sessions and require polling. This simplifies to 3 capabilities with push delivery via rules.

### 7.1 Schema + Storage

**File**: `src/gobby/storage/migrations.py`

```sql
-- Extend inter_session_messages for P2P and push delivery
ALTER TABLE inter_session_messages ADD COLUMN message_type TEXT NOT NULL DEFAULT 'direct';
ALTER TABLE inter_session_messages ADD COLUMN metadata_json TEXT;
ALTER TABLE inter_session_messages ADD COLUMN delivered_at TEXT;
CREATE INDEX idx_ism_undelivered ON inter_session_messages(to_session, delivered_at)
    WHERE delivered_at IS NULL;

-- Command execution lifecycle
CREATE TABLE agent_commands (
    id TEXT PRIMARY KEY,
    from_session TEXT NOT NULL,
    to_session TEXT NOT NULL,
    command_text TEXT NOT NULL,
    allowed_tools TEXT,            -- JSON array or "all"
    allowed_mcp_tools TEXT,        -- JSON array of "server:tool" or "all"
    exit_condition TEXT,           -- Expression evaluated as rule `when`
    status TEXT NOT NULL DEFAULT 'pending',  -- pending|active|completed|cancelled
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    activated_at TEXT,
    completed_at TEXT
);
```

**Files**:
| File | Change |
|------|--------|
| `src/gobby/storage/inter_session_messages.py` | Add new columns to dataclass. Add `get_undelivered_messages(to_session)` and `mark_delivered(ids)` |
| `src/gobby/storage/agent_commands.py` | **New.** `AgentCommand` dataclass + `AgentCommandManager` CRUD |
| `src/gobby/storage/sessions.py` | Add `is_ancestor(ancestor_id, descendant_id)` for command security |

### 7.2 Unified `send_message` + `send_command`

**File**: `src/gobby/mcp_proxy/tools/agent_messaging.py` — rewrite from scratch.

| Tool | Description |
|------|-------------|
| `send_message(from_session, to_session, content, *, priority, metadata)` | P2P message to any session in same project. Auto-writes to `agent_runs.result` when recipient is sender's parent (preserves `get_agent_result` bridge). |
| `send_command(to_session, command_text, allowed_tools, allowed_mcp_tools, exit_condition)` | Directed execution (ancestor-only). Creates pending command. |
| `complete_command(result)` | Signal command completion, remove temporary rules, send result back to sender. |

**Security**: `send_message` validates same `project_id`. `send_command` validates ancestor relationship via `is_ancestor()`. Reject if target already has an active command.

### 7.3 Push Delivery via Rules

Instead of hardcoding push delivery in `EventEnricher`, add bundled rules:

```yaml
# messaging-rules.yaml
group: messaging
tags: [agents, messaging]

rules:
  deliver-pending-messages:
    description: "Inject undelivered messages on agent prompt"
    event: before_agent
    priority: 40
    effect:
      type: mcp_call
      server: gobby-agents
      tool: deliver_pending_messages
      arguments:
        token_budget: 2000

  activate-pending-command:
    description: "Activate pending command and set tool restrictions"
    event: before_agent
    priority: 5
    when: "has_pending_command(session_id)"
    effect:
      type: mcp_call
      server: gobby-agents
      tool: activate_command

  command-tool-restriction:
    description: "Block tools not allowed by active command"
    event: before_tool
    priority: 1
    when: "variables.get('_command_active')"
    effect:
      type: block
      tools: "{{ _command_blocked_tools }}"
      reason: |
        Command mode active. Only these tools are allowed: {{ variables._command_tools }}.
        Use complete_command(result) when done.

  command-exit-condition:
    description: "Auto-complete command when exit condition met"
    event: after_tool
    priority: 5
    when: "variables.get('_command_active') and variables.get('_command_exit_condition') and eval(_command_exit_condition)"
    effect:
      type: mcp_call
      server: gobby-agents
      tool: auto_complete_command
```

### 7.4 Command Lifecycle (via RuleEngine, not WorkflowEngine)

**Activation** (`activate_command` MCP tool):
1. Set session variables: `_command_active = true`, `_command_id`, `_command_tools`, `_command_mcp_tools`, `_command_exit_condition`, `_command_blocked_tools` (computed inverse of allowed)
2. Inject command text as context
3. Mark command `status = 'active'`, set `activated_at`
4. Send `command_ack` message back to sender

**Completion** (`complete_command` MCP tool):
1. Clear session variables: `_command_active = false`, etc.
2. Mark command `status = 'completed'`, set `completed_at`
3. Send result as message to `from_session`

**Edge cases**:
- New command while one active → reject
- Session ends with active command → cancel in `SessionCoordinator.complete_agent_run()`
- Daemon restart mid-command → variables are persisted, rules re-evaluate on next event

No workflow state snapshot/restore needed — command state lives in session variables, rules handle enforcement.

### Messaging files

| File | Change |
|------|--------|
| `src/gobby/storage/inter_session_messages.py` | Add P2P columns, delivery tracking |
| `src/gobby/storage/agent_commands.py` | **New** — command CRUD |
| `src/gobby/mcp_proxy/tools/agent_messaging.py` | Rewrite: `send_message`, `send_command`, `complete_command`, `deliver_pending_messages`, `activate_command`, `auto_complete_command` |
| `src/gobby/install/shared/rules/messaging.yaml` | **New** — 4 messaging rules |
| `src/gobby/servers/websocket/broadcast.py` | Add `agent_message` / `agent_command` event types |

## Phase 8: Simplified Agents

Current agent definitions have **29 columns**, 4 personality fields, named workflows maps with file refs + inline definitions, and spawn_agent_impl accepts 35+ parameters. This is massively overengineered.

Agents simplify to: **identity + provider + spawn config + rule set**. No inline workflows. No named workflow maps. Behavior is defined by rules, not embedded YAML.

### 8.1 Simplified Agent Definition

Current → Simplified:

| Keep | Drop | Collapse |
|------|------|----------|
| name | role | role + goal + personality + instructions → `instructions` |
| description | goal | sandbox_config fields → `sandbox: bool` |
| provider | personality | workflows map → `rules: list[str]` |
| model | default_workflow | |
| mode | sandbox_config | |
| timeout | sandbox_mode | |
| max_turns | sandbox_allow_network | |
| isolation | sandbox_extra_paths | |
| base_branch | skill_profile | |
| instructions | terminal (always auto) | |
| enabled | branch_prefix | |
| | scope (use source field) | |
| | lifecycle_variables (rules handle state via set_variable on session_start) | |
| | default_variables (dead code — prepared but never applied) | |

**29 columns → 12 fields** in `definition_json`.

```yaml
# agent: developer
name: developer
description: "Writes code, runs tests, commits"
instructions: |
  You are a developer agent. You write clean, tested code.
  Always run tests before committing. Never review your own code.
provider: gemini
model: gemini-2.5-pro
mode: terminal                   # terminal | embedded | headless
isolation: worktree              # current | worktree | clone
base_branch: main
timeout: 120.0
max_turns: 10
rules:                           # Rules activated when this agent runs
  - require-task-before-edit     # shared bundled rule
  - require-commit-before-close  # shared bundled rule
  - no-coderabbit                # agent-specific: can't run review tools
  - require-tests-pass           # agent-specific: must test before commit
```

Stored in `workflow_definitions` as `workflow_type = 'agent'` with `definition_json` containing the above. Appears in the Agents tab of the Workflows page.

### 8.2 Agent-Scoped Rules

Rules can be scoped to agents via `agent_scope` in `RuleDefinitionBody`:

```python
class RuleDefinitionBody(BaseModel):
    event: RuleEvent
    when: str | None = None
    match: dict[str, Any] | None = None
    effect: RuleEffect
    group: str | None = None
    agent_scope: list[str] | None = None  # Only active for these agent types
```

When an agent spawns, the RuleEngine loads:
1. Global rules (no `agent_scope`) — always active
2. Agent-scoped rules where `agent_scope` includes the agent's name
3. Rules explicitly listed in the agent's `rules` field

```yaml
# agent-rules/developer.yaml
group: developer-agent
agent_scope: [developer]

rules:
  no-coderabbit:
    description: "Developer can't run code review tools"
    event: before_tool
    effect:
      type: block
      mcp_tools: ["coderabbit:*"]
      reason: "Developers don't review their own code. Request a QA review."

  require-tests-pass:
    description: "Tests must pass before committing"
    event: before_tool
    when: "'git commit' in (event.data.get('command') or '')"
    effect:
      type: block
      tools: [Bash]
      command_pattern: "git\\s+commit"
      reason: "Run tests before committing. Use: uv run pytest"
```

```yaml
# agent-rules/qa.yaml
group: qa-agent
agent_scope: [qa]

rules:
  no-code-writing:
    description: "QA can't write production code"
    event: before_tool
    when: "not is_test_file(tool_input.get('file_path', ''))"
    effect:
      type: block
      tools: [Edit, Write]
      reason: "QA agents can only edit test files. Reassign code changes to a developer."
```

```yaml
# agent-rules/coordinator.yaml
group: coordinator-agent
agent_scope: [coordinator]

rules:
  no-code-writing:
    description: "Coordinator orchestrates, doesn't write code"
    event: before_tool
    effect:
      type: block
      tools: [Edit, Write, NotebookEdit]
      reason: "Coordinators orchestrate via send_command. Delegate code work to developer agents."
```

### 8.3 Future: Agents Replace Hardcoded Systems

Out of scope for this plan. **The implementing agent MUST create the following investigation tasks via `create_task` on gobby-tasks** before closing Phase 8:

1. **"Investigate: QA agent replacing TaskValidator"** — Can a QA agent with rules + instructions prompt replace `tasks/validation.py`'s hardcoded validation logic? What validation context gathering (git diffs, file patterns) would move to MCP tools?

2. **"Investigate: Developer agent TDD enforcement"** — Can developer agent rules enforce test-first workflow? What does `require-tests-pass` need to check (test existence, test passage, coverage)?

3. **"Investigate: Expander agent replacing task decomposition"** — Can an expander agent with an LLM prompt replace hardcoded task expansion? What expansion heuristics are worth preserving as rules vs. prompt instructions?

These tasks ensure the investigations happen in future sessions rather than being forgotten.

### 8.4 Orchestration: Pipelines Spawn Agents, Rules Enforce Behavior

Clean separation of concerns:
- **Rules**: What an agent CAN and CANNOT do (event-driven, stateless)
- **Pipelines**: What happens in what order (sequential, deterministic — spawn, wait, loop)
- **Agent definition**: Identity + provider + which rules apply

A coordinator pipeline spawns agents; each agent follows its rules:

```yaml
# coordinator-pipeline.yaml
type: pipeline
steps:
  - id: develop
    mcp:
      server: gobby-agents
      tool: spawn_agent
      arguments:
        agent: developer
        task_id: "{{ input.task_id }}"
  - id: wait-dev
    mcp:
      server: gobby-agents
      tool: wait_for_agent
      arguments:
        agent_run_id: "{{ steps.develop.output.run_id }}"
  - id: review
    mcp:
      server: gobby-agents
      tool: spawn_agent
      arguments:
        agent: qa
        task_id: "{{ input.task_id }}"
```

Orchestration primitives (spawn_agent, wait_for_agent, loop) stay as pipeline MCP steps — not rule effects. Rules are for behavior constraints, not execution sequencing.

### 8.5 Agent Spawning Simplification

Current `spawn_agent_impl` accepts 35+ parameters. Simplified to:

```python
async def spawn_agent(
    agent_name: str,          # Lookup in workflow_definitions
    prompt: str,              # What to do
    task_id: str | None,      # Optional task to claim
    # Everything else comes from agent definition
) -> AgentRun:
```

When spawning:
1. Load agent definition from `workflow_definitions` where `workflow_type='agent'`
2. Create child session with agent's `provider`/`model`
3. Inject agent's `instructions` as system prompt
4. Activate agent's `rules` (set `_agent_type` session variable → RuleEngine filters by `agent_scope`)
5. Inject `prompt` as first user message

### Agent files

| File | Change |
|------|--------|
| `src/gobby/workflows/definitions.py` | Add `agent_scope` to `RuleDefinitionBody` |
| `src/gobby/storage/workflow_definitions.py` | Agent CRUD via existing `workflow_type='agent'` |
| `src/gobby/install/shared/rules/agent-rules/*.yaml` | Agent-specific rule sets |
| `src/gobby/mcp_proxy/tools/spawn_agent.py` | Simplify: load definition → create session → activate rules |
| `src/gobby/agents/definitions.py` | Simplify `AgentDefinition` model (29 → 14 fields) |
| `src/gobby/storage/agent_definitions.py` | Migrate to `workflow_definitions` queries, then remove |

## Phase 9: Documentation Updates

Every phase produces documentation debt. Update docs as each phase ships, not as a batch at the end.

### Critical (rewrite/major update)

| File | Impact | When |
|------|--------|------|
| `docs/guides/workflows.md` (1,280 lines) | Complete rewrite — replace lifecycle/step with rules model | After Phase 4 |
| `docs/guides/agents.md` (569 lines) | Simplified agent model, new messaging tools | After Phase 8 |
| `docs/guides/workflow-actions.md` | Remove ActionExecutor actions, document 4 effect primitives | After Phase 2 |
| `docs/guides/webhooks-and-plugins.md` | Remove plugin development section entirely | After Phase 6 |
| `README.md` | Update architecture overview, workflow/agent/pipeline descriptions | After Phase 8 |

### High priority (significant sections)

| File | Impact | When |
|------|--------|------|
| `docs/architecture/architecture.md` | Update engine architecture, remove ActionExecutor | After Phase 6 |
| `docs/guides/mcp-tools.md` | New rule MCP tools, updated agent tools, P2P messaging tools | After Phase 5, 7 |
| `docs/guides/orchestration.md` | Updated agent spawning, inter-agent messaging | After Phase 7 |
| `docs/architecture/source-tree.md` | Reflect file removals/additions | After Phase 6 |
| `CLAUDE.md` | Update architecture section, directory structure | After Phase 8 |
| `CONTRIBUTING.md` | Update project structure for contributors | After Phase 8 |

### Skills (agent-facing documentation)

| File | Impact | When |
|------|--------|------|
| `src/gobby/install/shared/skills/workflows/SKILL.md` | Rewrite for rules-based model | After Phase 4 |
| `src/gobby/install/shared/skills/agents/SKILL.md` | Update for simplified agents | After Phase 8 |
| `src/gobby/install/shared/skills/gobby/SKILL.md` | General updates | After Phase 8 |

### Low priority

| File | Impact |
|------|--------|
| `docs/guides/pipelines.md` | Minor — pipelines unchanged |
| `docs/guides/cli-commands.md` | Update CLI examples for new `gobby rules` commands |
| `docs/examples/workflow-diagrams.md` | Update mermaid diagrams |
| `src/gobby/hooks/README.md` | Minor hook system updates |

## Relationship to `docs/plans/orchestrator-refactor.md`

The orchestrator-refactor plan is orthogonal — it asks "how do we coordinate multiple agents?" This plan answers "what can agents do?" The separation: pipelines handle orchestration (spawn/wait/loop), rules handle behavior (block/set/inject/call). A coordinator becomes a pipeline that spawns rule-constrained agents. The orchestrator-refactor's Option C (build orchestration into pipelines) aligns with this plan — pipeline steps call `spawn_agent` and `wait_for_agent` as MCP tools.

## What Gets Removed

- `rules` table (migration to drop)
- `src/gobby/storage/rules.py` (`RuleStore`) — replaced by `workflow_definitions`
- `src/gobby/workflows/rule_sync.py` — replaced by `loader_sync.py` rule handling
- `check_rules` resolution in `engine_context.py` (`_resolve_check_rules()`)
- `src/gobby/hooks/plugins.py` (829 lines) — plugin system removed, MCP tools are the extension mechanism
- `src/gobby/config/extensions.py` — plugin config removed
- `src/gobby/servers/routes/mcp/plugins.py` — plugin HTTP routes removed
- `src/gobby/workflows/actions.py` (`ActionExecutor`) — hardcoded actions decomposed into `set_variable` + `mcp_call` effects
- `src/gobby/install/shared/rules/worker-safety.yaml` — migrated to new rule format
- `src/gobby/install/shared/workflows/auto-task.yaml` — migrated to auto-task rules
- Old messaging tools (`send_to_parent`, `send_to_child`, `broadcast_to_children`, `poll_messages`, `mark_message_read`) — replaced by `send_message`, `send_command`, `complete_command`
- `src/gobby/storage/agent_definitions.py` — agents move to `workflow_definitions` as `workflow_type='agent'`
- `src/gobby/agents/definitions.py` — 29-field `AgentDefinition` simplified to 14-field model in `definition_json`
- `agent_definitions` table — migration to drop (data migrated to `workflow_definitions`)
- 15 agent definition columns (role, goal, personality, sandbox_config, sandbox_mode, sandbox_allow_network, sandbox_extra_paths, skill_profile, workflows, lifecycle_variables, default_variables, terminal, branch_prefix, default_workflow, scope)

## What This Doesn't Change

- Pipeline execution (`pipeline_executor.py`) — pipelines are a separate concern, simplified independently
- Hook infrastructure (HookManager, adapters, events)
- `workflow_definitions` table schema — no migration needed, rules use existing columns with `workflow_type='rule'`
- Step workflow engine (`engine.py`) — not in active use, left as-is for now (Phase 6 candidate for removal)
