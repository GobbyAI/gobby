# Subagent Spawning System Plan

## Vision

Enable agents to spawn independent subagents from within a session. Subagents can use any LLM provider (Claude SDK, Gemini SDK, Codex SDK, LiteLLM) and follow deterministic step workflows. This transforms Gobby from a session tracker into an **agent orchestration layer**.

Key insight: **The parent agent doesn't need to implement subtasks itself** - it can delegate to specialized subagents that follow workflows, use different providers, and report results back.

Inspired by:

- Claude Code's Task tool (subagent spawning)
- Worktree parallelization patterns
- Multi-agent orchestration systems

---

## Use Cases

### Provider Diversity

Use the best model for each task:

- Gemini for research and web search
- Claude for code generation
- Codex for backend implementation
- OpenRouter for cost-effective subtasks

### Parallel Development

Spawn agents in separate worktrees:

- Frontend agent (Gemini) → `feature/ui`
- Backend agent (Codex) → `feature/api`
- Test agent (Claude) → `feature/tests`

### Workflow Enforcement

Subagent follows a workflow definition:

- Tool restrictions per step
- Exit conditions with validation
- Structured completion via `complete()` tool

### Cost Optimization

Delegate routine tasks to cheaper models while the orchestrator uses a more capable model.

---

## CLI Interface

```bash
# Start an agent (human-initiated)
uv run gobby agents start \
  --workflow workflow.yaml \
  --task gt-abc123 | next \
  --timeout 120 \
  --prompt "Implement the feature" \
  --session-context summary_markdown \
  --mode terminal \                    # in_process, terminal, embedded, headless
  --terminal auto \                    # auto, ghostty, iterm, gnome-terminal, etc.
  --provider claude \
  --cli claude

# List running agents
uv run gobby agents list

# Get agent status/result
uv run gobby agents status <agent-id>

# Cancel an agent
uv run gobby agents cancel <agent-id>

# Worktree management
uv run gobby worktrees create [--task TASK_ID] [--branch NAME] [--base BRANCH]
uv run gobby worktrees list [--status STATUS] [--project PROJECT]
uv run gobby worktrees show WORKTREE_ID
uv run gobby worktrees delete WORKTREE_ID [--force]
uv run gobby worktrees spawn WORKTREE_ID [--prompt "..."] [--mode terminal] [--terminal auto]
uv run gobby worktrees claim WORKTREE_ID
uv run gobby worktrees release WORKTREE_ID
uv run gobby worktrees sync WORKTREE_ID
uv run gobby worktrees stale [--hours N]
uv run gobby worktrees cleanup [--hours N] [--dry-run]
```

---

## MCP Interface

### gobby-agents

```python
# Start agent from within a session (parent agent calling)
start_agent(
    workflow="code-review.yaml",
    task="gt-abc123",  # or "next"
    timeout=120,  # 0 = infinite
    prompt="Review auth changes",
    session_context="summary_markdown",  # or session_id, transcript:10, file:path
    mode="in_process",  # in_process, terminal, embedded, headless
    terminal="auto",  # auto, ghostty, iterm, gnome-terminal, etc.
    provider=None,  # use workflow default
    cli=None,
    worktree_id=None,  # use existing worktree
)
```

#### Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `workflow` | string | Path to workflow YAML (required) |
| `task` | string | Task ID or "next" for auto-select |
| `timeout` | float | Seconds (0 = infinite) |
| `prompt` | string | The prompt to give the agent |
| `session_context` | string | How to pass context (see below) |
| `mode` | string | Execution mode (see below) |
| `terminal` | string | Terminal for terminal/embedded modes |
| `provider` | string | Override: claude, gemini, codex, litellm |
| `model` | string | Override model ID |
| `worktree_id` | string | Use existing worktree (terminal mode) |

#### Execution Modes

| Mode | Description | Daemon | Output |
|------|-------------|--------|--------|
| `in_process` | Run via SDK in daemon process | Blocks | Returns result |
| `terminal` | Spawn external terminal window | Non-blocking | Session handoff |
| `embedded` | Return PTY for UI attachment | Non-blocking | PTY handle |
| `headless` | Daemon captures output, no UI | Non-blocking | Session transcript |

#### Session Context Options

- `summary_markdown` - Use parent session's summary
- `session_id:<id>` - Load context from specific session
- `transcript:<n>` - Last N turns from parent
- `file:<path>` - Load markdown file as context

#### MCP Tools (gobby-agents)

| Tool | Description |
|------|-------------|
| `start_agent` | Start a subagent with workflow |
| `complete` | Signal completion with structured result |
| `list_agents` | List running async agents |
| `get_agent_result` | Get result from async agent |
| `cancel_agent` | Cancel a running agent |

### gobby-worktrees

Daemon-managed worktree registry for parallel agent development.

#### MCP Tools (gobby-worktrees)

| Tool | Description |
|------|-------------|
| `create_worktree` | Create worktree, optionally linked to task |
| `list_worktrees` | List all worktrees with status and owning agents |
| `get_worktree` | Get worktree details including linked task |
| `claim_worktree` | Claim ownership for current session |
| `release_worktree` | Release ownership without deleting |
| `delete_worktree` | Delete worktree and its branch |
| `spawn_agent_in_worktree` | Launch Claude Code agent in worktree |
| `sync_worktree_from_main` | Rebase/merge from base branch |
| `detect_stale_worktrees` | Find inactive worktrees |
| `cleanup_stale_worktrees` | Delete stale worktrees |

```python
@mcp.tool()
def create_worktree(
    task_id: str | None = None,
    branch_name: str | None = None,  # Auto-generated from task if not provided
    base_branch: str = "main",
) -> dict:
    """
    Create a new worktree for isolated development.

    If task_id provided:
    - Branch name derived from task title (kebab-case)
    - Worktree linked to task for tracking
    - Task marked as in_progress

    Returns worktree path and branch name.
    """

@mcp.tool()
def spawn_agent_in_worktree(
    worktree_id: str,
    prompt: str | None = None,
    mode: str = "terminal",  # terminal, embedded, headless
    terminal: str = "auto",  # auto, ghostty, iterm, kitty, gnome-terminal, etc.
    workflow: str | None = None,
) -> dict:
    """
    Launch a new agent in the specified worktree.

    Modes:
    - terminal: Opens external terminal window (Ghostty, iTerm, etc.)
    - embedded: Returns PTY handle for UI attachment (xterm.js)
    - headless: Daemon captures output, no terminal visible

    Terminal selection is cross-platform:
    - macOS: ghostty, iterm, terminal.app, kitty, alacritty
    - Linux: ghostty, gnome-terminal, konsole, kitty, alacritty
    - Windows: windows-terminal, cmd, alacritty

    Agent session is linked to the worktree for tracking.
    """
```

---

## Flow Diagrams

### In-Process Agent Execution

```mermaid
sequenceDiagram
    participant Parent as Parent Agent (Claude Code)
    participant Daemon as Gobby Daemon
    participant Executor as AgentExecutor
    participant LLM as LLM Provider (Gemini/Claude/etc)
    participant MCP as MCP Proxy

    Parent->>Daemon: start_agent(workflow, prompt, task)
    Daemon->>Daemon: Create child session (depth=1)
    Daemon->>Daemon: Load workflow, initialize state
    Daemon->>Executor: run(prompt, tools, tool_handler)

    loop Agent Loop
        Executor->>LLM: Send prompt + tool schemas
        LLM-->>Executor: Response with tool_use
        Executor->>Daemon: tool_handler(tool_name, args)
        Daemon->>Daemon: Check workflow allows tool
        alt Tool Allowed
            Daemon->>MCP: call_tool(server, tool, args)
            MCP-->>Daemon: Tool result
            Daemon-->>Executor: ToolResult
        else Tool Blocked
            Daemon-->>Executor: Error: tool not allowed
        end
        Executor->>LLM: Tool result
    end

    LLM->>Executor: complete(output, artifacts)
    Executor-->>Daemon: AgentResult
    Daemon->>Daemon: Store result in session
    Daemon-->>Parent: AgentResult
```

### Terminal Mode (Interactive) Agent Execution

```mermaid
sequenceDiagram
    participant Parent as Parent Agent
    participant Daemon as Gobby Daemon
    participant Worktrees as gobby-worktrees
    participant Terminal as Terminal (Ghostty/iTerm)
    participant CLI as CLI (claude/gemini)
    participant Hooks as Hook System

    Parent->>Daemon: start_agent(workflow, mode=terminal, task_id)
    Daemon->>Daemon: Create child session (depth=1)
    Daemon->>Daemon: Store workflow in session metadata

    alt Worktree Requested
        Daemon->>Worktrees: create_worktree(task_id)
        Worktrees->>Worktrees: git worktree add
        Worktrees->>Worktrees: Link to task, set status=active
        Worktrees-->>Daemon: worktree_id, path
    end

    Daemon->>Worktrees: spawn_agent_in_worktree(worktree_id)
    Worktrees->>Terminal: Open new window
    Terminal->>CLI: Launch CLI in worktree
    Daemon-->>Parent: agent_id (async)

    CLI->>Hooks: session.start
    Hooks->>Daemon: Register session
    Daemon->>CLI: Inject workflow context

    loop CLI Execution
        CLI->>Hooks: before_tool
        Hooks->>Daemon: Check workflow
        Daemon-->>CLI: allow/block
    end

    CLI->>Hooks: session.end
    Hooks->>Daemon: Capture handoff context
    Daemon->>Daemon: Store result from handoff

    Parent->>Daemon: get_agent_result(agent_id)
    Daemon-->>Parent: AgentResult
```

### Agent Depth & Tool Filtering

```mermaid
flowchart TD
    A[Parent Session<br/>depth=0] -->|start_agent| B[Child Session<br/>depth=1]
    B -->|start_agent blocked| C{Workflow allows<br/>nested agents?}
    C -->|No| D[Block: max_depth exceeded]
    C -->|Yes, depth<max| E[Grandchild Session<br/>depth=2]

    subgraph "Tool Access"
        B --> F[complete ✓]
        B --> G[list_agents ✓]
        B --> H[start_agent ✗]
        B --> I[workflow tools ✓]
    end
```

### State Management Flow

```mermaid
flowchart LR
    subgraph Runtime
        A[Running Agents Dict]
    end

    subgraph PostgreSQL
        B[agent_runs table]
        C[sessions table]
        D[workflow_states table]
        E[worktrees table]
    end

    A -->|On complete| B
    A -->|Child session| C
    A -->|Workflow state| D
    A -->|Worktree link| E
```

---

## Architecture

### Components

```text
┌─────────────────────────────────────────────────────────────┐
│                      MCP Tool Call                           │
│  start_agent | complete | list_agents | get_agent_result    │
│  create_worktree | spawn_agent_in_worktree | ...            │
└─────────────────────────┬───────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────┐
│                     Agent Runner                             │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐  │
│  │   Session   │  │   Workflow  │  │   AgentExecutor     │  │
│  │   Manager   │  │   Engine    │  │   (per provider)    │  │
│  └─────────────┘  └─────────────┘  └─────────────────────┘  │
│                                                              │
│  ┌─────────────────────────────────────────────────────────┐│
│  │                  Worktree Manager                        ││
│  │   LocalWorktreeManager | WorktreeGitManager | Spawner   ││
│  └─────────────────────────────────────────────────────────┘│
└─────────────────────────┬───────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────┐
│                   LLM Providers                              │
│  Claude SDK | Gemini SDK | Codex SDK | LiteLLM              │
└─────────────────────────────────────────────────────────────┘
```

### Module Structure

```text
src/gobby/
├── llm/
│   └── executor.py           # AgentExecutor ABC + per-provider implementations
├── agents/
│   ├── __init__.py
│   ├── registry.py           # gobby-agents MCP tool definitions
│   ├── runner.py             # AgentRunner - orchestrates execution
│   └── session.py            # Child session creation/linking
├── worktrees/
│   ├── __init__.py
│   ├── manager.py            # WorktreeManager - coordinates worktree lifecycle
│   ├── git.py                # WorktreeGitManager - git operations
│   └── spawn.py              # Terminal spawning logic
├── storage/
│   └── worktrees.py          # LocalWorktreeManager - PostgreSQL CRUD
└── mcp_proxy/tools/
    ├── agents.py             # gobby-agents MCP tool implementations
    └── worktrees.py          # gobby-worktrees MCP tool implementations
```

### Data Models

#### agent_runs table

```sql
CREATE TABLE agent_runs (
    id TEXT PRIMARY KEY,              -- agent_id
    parent_session_id TEXT,
    child_session_id TEXT,
    workflow_name TEXT,
    provider TEXT,
    model TEXT,
    status TEXT,                      -- running, completed, timeout, error, cancelled
    prompt TEXT,
    result JSON,                      -- AgentResult on completion
    worktree_id TEXT,                 -- Link to worktree (if terminal mode)
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    FOREIGN KEY (parent_session_id) REFERENCES sessions(session_id),
    FOREIGN KEY (child_session_id) REFERENCES sessions(session_id),
    FOREIGN KEY (worktree_id) REFERENCES worktrees(id)
);
```

#### worktrees table

```sql
CREATE TABLE worktrees (
    id TEXT PRIMARY KEY,                    -- wt-{6 chars}
    project_id TEXT NOT NULL,
    task_id TEXT,                           -- Optional: linked gobby-task
    branch_name TEXT NOT NULL,
    worktree_path TEXT NOT NULL,            -- Absolute path
    base_branch TEXT DEFAULT 'main',
    agent_session_id TEXT,                  -- Current owning session
    status TEXT DEFAULT 'active',           -- active, stale, merged, abandoned
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    merged_at TEXT,
    FOREIGN KEY (project_id) REFERENCES projects(id),
    FOREIGN KEY (task_id) REFERENCES tasks(id),
    FOREIGN KEY (agent_session_id) REFERENCES sessions(id)
);

CREATE INDEX idx_worktrees_project ON worktrees(project_id);
CREATE INDEX idx_worktrees_status ON worktrees(status);
CREATE INDEX idx_worktrees_task ON worktrees(task_id);
```

#### sessions table additions

```sql
ALTER TABLE sessions ADD COLUMN agent_depth INTEGER DEFAULT 0;
ALTER TABLE sessions ADD COLUMN spawned_by_agent_id TEXT;
```

### AgentExecutor Interface

```python
class AgentExecutor(ABC):
    """Execute an agentic loop with tool calling."""

    @abstractmethod
    async def run(
        self,
        prompt: str,
        system_prompt: str | None,
        model: str | None,
        tools: list[ToolSchema],
        tool_handler: Callable[[str, dict], Awaitable[ToolResult]],
        max_turns: int = 10,
        timeout: float = 120.0,
    ) -> AgentResult:
        pass
```

Provider implementations:

- **ClaudeExecutor**: Refactor from `ClaudeLLMProvider.generate_with_mcp_tools()` (src/gobby/llm/claude.py:453-615)
- **GeminiExecutor**: Use Gemini function calling API
- **CodexExecutor**: Use Codex tool use API
- **LiteLLMExecutor**: OpenAI-compatible function calling

### The `complete` Tool

Subagents call this to explicitly return structured output:

```python
async def complete(
    output: str,                      # Summary/final message
    status: Literal["success", "partial", "blocked"] = "success",
    artifacts: dict[str, Any] = {},   # Structured outputs
    files_modified: list[str] = [],   # What changed
    next_steps: list[str] = [],       # Suggestions for parent
) -> NoReturn:
```

Workflow can define expected completion schema:

```yaml
exit_conditions:
  - type: tool_call
    tool: complete
    schema:
      output: string
      issues_found: integer
```

---

## Execution Modes

### In-Process (default)

1. Create child session linked to parent
2. Initialize workflow state for child session
3. Create AgentExecutor for provider
4. Run agent loop with workflow tool filtering via tool_handler
5. On `complete()` call or timeout, return AgentResult

### Terminal Mode (`--mode terminal`)

1. Create child session linked to parent
2. Set workflow in session metadata
3. Create or claim worktree via `gobby-worktrees`
4. Spawn terminal with CLI via `spawn_agent_in_worktree()`
5. CLI connects via hooks, picks up workflow
6. On session end, result captured via handoff
7. Worktree ownership released or retained based on task status

---

## Configuration

### Provider Config (~/.gobby/config.yaml)

```yaml
llm_providers:
  claude:
    enabled: true
    # subscription-based auth via CLI
  gemini:
    enabled: true
    auth_mode: api_key  # or adc
  codex:
    enabled: true
    auth_mode: subscription
  litellm:
    enabled: true
    api_base: https://openrouter.ai/api/v1
```

### Worktree Config (~/.gobby/config.yaml)

```yaml
worktrees:
  enabled: true
  base_path: ".worktrees"             # Relative to project root
  default_mode: "terminal"            # in_process, terminal, embedded, headless
  default_terminal: "auto"            # auto-detects based on platform:
                                      #   macOS: ghostty > iterm > kitty > terminal.app
                                      #   Linux: ghostty > kitty > gnome-terminal > alacritty
                                      #   Windows: windows-terminal > alacritty > cmd
  stale_threshold_hours: 24
  auto_cleanup: false                 # Auto-delete stale worktrees
  max_concurrent: 12                  # Max parallel worktrees
  branch_prefix: "agent/"             # Prefix for auto-generated branches
```

### Workflow-Level Provider Override

```yaml
name: code-review
type: step
settings:
  provider: gemini
  model: gemini-2.0-flash
  timeout: 120
  allow_provider_override: false  # Lock to workflow provider
```

### Override Hierarchy

1. CLI args (`uv run gobby agents start --provider`) - highest priority
2. MCP tool args (if `allow_provider_override: true` in workflow)
3. Workflow settings
4. config.yaml defaults - lowest priority

---

## Safety & Guardrails

### Agent Depth Tracking

```python
class Session:
    agent_depth: int = 0  # 0 = human-initiated, 1+ = spawned
    parent_session_id: str | None = None
    spawned_by_agent_id: str | None = None
```

### Default Depth Limit

- `max_agent_depth: 1` by default
- Subagents cannot start further subagents
- Workflow can opt-in with explicit config:

```yaml
name: orchestrator-workflow
settings:
  allow_nested_agents: true
  max_agent_depth: 2  # Allow one level of nesting
```

### Tool Filtering for Subagents

Subagents automatically have `start_agent` blocked unless workflow explicitly allows. They always have access to:

- `complete` - signal completion
- `list_agents` - see sibling agents (read-only)
- Workflow-allowed tools

### Timeout Enforcement

- In-process: `asyncio.wait_for` with timeout
- Terminal: Workflow can define `max_duration`, hooks enforce

### Worktree Isolation

- Each agent works in its own worktree, protecting main branch
- Task-driven assignment ensures traceability
- Centralized daemon coordination prevents conflicts
- Stale worktrees detected and cleaned up automatically

### Workflow Exclusions for Worktree Agents

- Worktree agent sessions have `is_worktree: true` variable
- Excluded from `require_task_complete` enforcement (main session owns parent task)
- Can stop when their assigned subtask is complete
- Main session tracks overall epic progress

---

## Implementation Phases

### Phase 1: Core Infrastructure ✅ COMPLETED

> **Status**: Implemented and tested. Do not regenerate tasks for this phase.

- [x] Create `src/gobby/llm/executor.py` with `AgentExecutor` ABC
- [x] Create `ClaudeExecutor` by refactoring from `ClaudeLLMProvider.generate_with_mcp_tools()`
- [x] Create `src/gobby/agents/__init__.py` module
- [x] Create `src/gobby/agents/session.py` for child session creation
- [x] Add `agent_depth`, `spawned_by_agent_id` columns to sessions table (migration)
- [x] Create `agent_runs` table (migration)
- [x] Create `src/gobby/storage/agents.py` for agent_runs CRUD
- [x] Create `src/gobby/agents/runner.py` with `AgentRunner` class
- [x] Create `src/gobby/mcp_proxy/tools/agents.py` with MCP tool definitions
- [x] Register gobby-agents in `InternalRegistryManager`
- [x] Implement `start_agent` MCP tool (in-process mode)
- [x] Implement `complete` MCP tool
- [x] Implement `list_agents` MCP tool
- [x] Implement `get_agent_result` MCP tool
- [x] Implement `cancel_agent` MCP tool

### Phase 2: Workflow Integration ✅ COMPLETED

> **Status**: Implemented and tested. Do not regenerate tasks for this phase.

- [x] Load workflow definition for subagent
- [x] Initialize workflow state for child session
- [x] Implement tool_handler with workflow filtering
- [x] Handle `complete` tool as workflow exit condition
- [x] Integrate agent depth checking in workflow engine

### Phase 1.5: API Alignment & Context Injection ✅ COMPLETED

> **Status**: Implemented and tested. Do not regenerate tasks for this phase.

Bridge the gap between the current low-level implementation and the planned user-facing API. This phase updates `start_agent` to match the spec, implements context injection for subagents, and refactors the runner to enable terminal mode.

**Why now?** Adding more providers (Phase 3) before fixing the API creates technical debt. Fix the shape first.

#### Phase 1.5.1: Signature Alignment

Update `start_agent` MCP tool to match the planned API from the spec.

**Current signature (low-level):**

```python
start_agent(
    prompt, parent_session_id, project_id, machine_id, source,
    provider, model, workflow_name, system_prompt, max_turns, timeout, ...
)
```

**Target signature (user-facing):**

```python
start_agent(
    prompt: str,
    workflow: str | None = None,
    task: str | None = None,           # Task ID or "next"
    session_context: str = "summary_markdown",
    mode: str = "in_process",          # in_process, terminal, embedded, headless
    terminal: str = "auto",            # For terminal/embedded modes
    provider: str | None = None,
    model: str | None = None,
    worktree_id: str | None = None,
    timeout: float = 120.0,
    max_turns: int = 10,
)
```

- [x] Update `start_agent` signature in `src/gobby/mcp_proxy/tools/agents.py`
- [x] Infer `parent_session_id`, `project_id`, `machine_id` from request context
- [x] Add `mode` parameter (stub unsupported modes with `NotImplementedError`)
- [x] Add `session_context` parameter (default: `"summary_markdown"`)
- [x] Add `task` parameter for task-driven execution
- [x] Add `worktree_id` parameter for terminal mode
- [x] Add `terminal` parameter for terminal selection
- [x] Update `AgentConfig` dataclass to match

#### Phase 1.5.2: Context Resolver

Implement `ContextResolver` to fetch and format context for subagent injection.

**Context Sources:**

| Source | Format | Description |
|--------|--------|-------------|
| `summary_markdown` | Default | Parent session's `summary_markdown` field |
| `compact_markdown` | String | Parent session's `compact_markdown` (handoff context) |
| `session_id:<id>` | String | Summary from specific session by ID |
| `transcript:<n>` | String | Last N messages from parent session |
| `file:<path>` | String | Read file content (project-scoped) |

- [x] Create `src/gobby/agents/context.py` with `ContextResolver` class
- [x] Implement `resolve(source: str, session_id: str) -> str` method
- [x] Implement `_resolve_summary_markdown()` - parent session summary
- [x] Implement `_resolve_compact_markdown()` - parent session handoff
- [x] Implement `_resolve_session_id()` - lookup specific session
- [x] Implement `_resolve_transcript()` - fetch last N messages via `LocalSessionMessageManager`
- [x] Implement `_resolve_file()` - read file with security checks
- [x] Add unit tests in `tests/agents/test_context_resolver.py`

#### Phase 1.5.3: Error Handling

Define explicit behavior for context resolution failures.

| Error Case | Behavior |
|------------|----------|
| `session_id:<id>` not found | Raise `ContextResolutionError` with session ID |
| `transcript:<n>` with no messages | Return empty string (not an error) |
| `file:<path>` not found | Raise `ContextResolutionError` with path |
| `file:<path>` not readable | Raise `ContextResolutionError` with permission error |
| Content exceeds size limit | Truncate with `[truncated: X bytes]` suffix |
| Unknown source format | Raise `ContextResolutionError` |

- [x] Create `ContextResolutionError` exception class
- [x] Implement error handling for each source type
- [x] Add truncation logic with configurable limit
- [x] Add tests for all error cases

#### Phase 1.5.4: Security

Secure `file:<path>` context source against path traversal and abuse.

| Check | Description |
|-------|-------------|
| Project scope | Path must be within project directory |
| No traversal | Reject paths with `..` components |
| Symlink following | Only follow symlinks that resolve within project |
| Size limit | Default 50KB max (configurable) |
| File type | Text files only (reject binary) |

- [x] Implement path validation in `_resolve_file()`
- [x] Add `max_file_size` config option (default: 51200 bytes)
- [x] Add binary file detection (reject if not UTF-8)
- [x] Add tests for path traversal attempts
- [x] Add tests for symlink handling

#### Phase 1.5.5: Context Injection Format

Define how resolved context is prepended to the agent prompt.

```markdown
## Context from Parent Session
*Injected by Gobby subagent spawning*

{resolved_context}

---

## Task

{original_prompt}
```

- [x] Create `format_injected_prompt(context: str, prompt: str) -> str` function
- [x] Use markdown formatting with clear delimiters
- [x] Handle empty context gracefully (skip injection)
- [x] Add template to config for customization

#### Phase 1.5.6: Runner Refactor

Split `AgentRunner.run()` into `prepare_run()` + `execute_run()` to enable terminal mode.

**Why this split?**

- `prepare_run()` creates database state: child session, agent_run record, workflow state
- `execute_run()` runs the executor loop
- Terminal mode: calls `prepare_run()`, then spawns terminal that picks up from session via hooks
- Without split: terminal mode would duplicate all setup logic

```python
class AgentRunner:
    async def prepare_run(self, config: AgentConfig) -> AgentRunContext:
        """
        Prepare for agent execution (database setup).

        Creates:
        - Child session linked to parent
        - Agent run record
        - Workflow state (if workflow specified)

        Returns context for execute_run() or terminal spawn.
        """

    async def execute_run(
        self,
        context: AgentRunContext,
        config: AgentConfig,
        tool_handler: ToolHandler | None = None,
    ) -> AgentResult:
        """
        Execute the agent loop using prepared context.

        For in_process mode only. Terminal mode uses spawn instead.
        """

    async def run(self, config: AgentConfig, ...) -> AgentResult:
        """
        Full run (prepare + execute). Preserves existing behavior.
        """
        context = await self.prepare_run(config)
        return await self.execute_run(context, config, tool_handler)
```

- [x] Define `AgentRunContext` dataclass with session, run, workflow info
- [x] Extract setup logic from `run()` into `prepare_run()`
- [x] Extract execution logic into `execute_run()`
- [x] Update `run()` to call both (preserve existing behavior)
- [x] Add tests for `prepare_run()` isolation
- [x] Add tests for `execute_run()` with pre-prepared context

#### Phase 1.5.7: Terminal Mode Pickup (Design Only)

> **Note**: Implementation in Phase 4.3. This documents the design.

When `mode=terminal`, the daemon calls `prepare_run()` then spawns a terminal process. The spawned CLI picks up the prepared state via hooks:

1. Daemon calls `prepare_run()` → creates session with `workflow_name` in metadata
2. Daemon spawns terminal with `--session-id` or env var
3. CLI starts, triggers `session.start` hook
4. Hook reads session metadata, finds `workflow_name`
5. Hook activates workflow for the session
6. Agent works within workflow constraints
7. On `session.end`, handoff captured, result stored in `agent_runs`

- [x] Document pickup mechanism in workflow docs
- [x] Define session metadata fields for terminal pickup
- [x] Define environment variables for session context passing

#### Phase 1.5.8: Integration

Wire context injection into the agent spawning flow.

- [x] Integrate `ContextResolver` in `start_agent` tool
- [x] Call resolver before creating `AgentConfig`
- [x] Prepend resolved context to prompt
- [x] Add integration tests for full flow
- [x] Update `gobby-agents` tool documentation

#### Phase 1.5 Configuration

```yaml
# ~/.gobby/config.yaml
agents:
  context_injection:
    enabled: true
    default_source: "summary_markdown"
    max_file_size: 51200           # 50KB
    max_transcript_messages: 100
    truncation_suffix: "\n\n[truncated: {bytes} bytes remaining]"

  context_template: |
    ## Context from Parent Session
    *Injected by Gobby subagent spawning*

    {{ context }}

    ---

    ## Task

    {{ prompt }}
```

---

### Phase 3: Multi-Provider Support ✅ COMPLETED

> **Status**: All AgentExecutor implementations complete.

Create additional AgentExecutor implementations for provider diversity.

- [x] Create `GeminiExecutor` using Gemini function calling
- [x] Create `LiteLLMExecutor` using OpenAI-compatible API
- [x] Create `CodexExecutor` with dual-mode support:
  - **api_key mode**: OpenAI API function calling with full tool injection
  - **subscription mode**: Codex CLI spawning (`codex exec --json`), no custom tools
- [x] Implement provider resolution (workflow → config → default)

### Phase 4: Worktree Management ✅ COMPLETED

> **Status**: Implemented and tested. Do not regenerate tasks for this phase.

Daemon-managed worktree registry with agent assignment, status tracking, and coordinated merging.

#### Phase 4.1: Worktree Storage Layer

- [x] Create database migration for `worktrees` table
- [x] Create `src/gobby/storage/worktrees.py` with `LocalWorktreeManager` class
- [x] Implement CRUD operations (create, get, update, delete, list)
- [x] Implement status transitions (active → stale → merged/abandoned)

#### Phase 4.2: Git Operations

- [x] Create `src/gobby/worktrees/git.py` with `WorktreeGitManager` class
- [x] Implement `create_worktree()` - git worktree add
- [x] Implement `delete_worktree()` - git worktree remove + branch delete
- [x] Implement `sync_from_main()` - rebase/merge from base branch
- [x] Implement `get_worktree_status()` - uncommitted changes, ahead/behind

#### Phase 4.3: Agent Spawning in Worktrees

Agent spawning supports three execution modes and cross-platform terminals:

**Execution Modes:**

| Mode | Description | Use Case |
|------|-------------|----------|
| `terminal` | Spawn external terminal window | CLI users, full terminal features |
| `embedded` | Return PTY handle for UI attachment | Web UI with xterm.js |
| `headless` | Daemon captures output, no terminal | Background agents, CI/CD |

**Cross-Platform Terminal Support:**

| Terminal | macOS | Linux | Windows |
|----------|-------|-------|---------|
| ghostty | `open -na ghostty --args -e` | `ghostty -e` | ❌ |
| iterm | AppleScript | ❌ | ❌ |
| terminal.app | AppleScript | ❌ | ❌ |
| gnome-terminal | ❌ | `gnome-terminal --` | ❌ |
| konsole | ❌ | `konsole -e` | ❌ |
| kitty | `open -na kitty --args` | `kitty -e` | ❌ |
| alacritty | `open -na alacritty --args -e` | `alacritty -e` | `alacritty -e` |
| windows-terminal | ❌ | ❌ | `wt.exe -d path` |
| cmd | ❌ | ❌ | `start cmd /k` |

**Implementation Tasks:**

- [x] Create `src/gobby/agents/spawn.py` with `TerminalSpawner` class
- [x] Implement `SpawnMode` enum (terminal, embedded, headless)
- [x] Implement macOS spawners (Ghostty, iTerm, Terminal.app, kitty)
- [x] Implement Linux spawners (Ghostty, gnome-terminal, konsole, kitty, alacritty)
- [x] Implement Windows spawners (Windows Terminal, cmd, alacritty)
- [x] Implement `auto` terminal detection (find first available)
- [x] Implement embedded mode PTY creation via `pty.openpty()` or node-pty bridge
- [x] Implement headless mode with output capture to session transcript
- [x] Pass initial prompt via environment variable or temp file
- [x] Register spawned session with daemon

#### Phase 4.4: MCP Tools (gobby-worktrees)

- [x] Create `src/gobby/mcp_proxy/tools/worktrees.py` with `WorktreeToolRegistry`
- [x] Register as `gobby-worktrees` internal server
- [x] Implement `create_worktree`
- [x] Implement `list_worktrees`
- [x] Implement `get_worktree`
- [x] Implement `claim_worktree`
- [x] Implement `release_worktree`
- [x] Implement `delete_worktree`
- [x] Implement `spawn_agent_in_worktree`
- [x] Implement `sync_worktree_from_main`
- [x] Implement `detect_stale_worktrees`
- [x] Implement `cleanup_stale_worktrees`

#### Phase 4.5: Terminal Mode Integration

- [x] Update `start_agent` to support `mode=terminal` with worktrees
- [x] Store workflow in session metadata for hook pickup
- [x] Capture result from session handoff
- [x] Link worktree status to agent run status

### Phase 5: CLI Commands ✅ COMPLETED

> **Status**: Implemented and tested. Do not regenerate tasks for this phase.

Add CLI command groups for agents and worktrees.

#### Phase 5.1: Agent CLI

- [x] Add `gobby agents` command group to cli.py
- [x] Implement `gobby agents start`
- [x] Implement `gobby agents list`
- [x] Implement `gobby agents status`
- [x] Implement `gobby agents cancel`

#### Phase 5.2: Worktree CLI

- [x] Add `gobby worktrees` command group to cli.py
- [x] Implement `gobby worktrees create`
- [x] Implement `gobby worktrees list`
- [x] Implement `gobby worktrees show`
- [x] Implement `gobby worktrees delete`
- [x] Implement `gobby worktrees spawn`
- [x] Implement `gobby worktrees claim`
- [x] Implement `gobby worktrees release`
- [x] Implement `gobby worktrees sync`
- [x] Implement `gobby worktrees stale`
- [x] Implement `gobby worktrees cleanup`

### Phase 6: State Management ✅ COMPLETED

> **Status**: Implemented and tested. Do not regenerate tasks for this phase.

- [x] Implement in-memory running agents dict with thread safety
- [x] Persist completed agents to `agent_runs` table
- [x] Add worktree context to session handoff
- [x] Link worktree status to task status changes
- [x] Add WebSocket events for agent and worktree changes

### Phase 7: Testing ✅ COMPLETED

> **Status**: 470 tests passing. All tests implemented.

- [x] Unit tests for AgentExecutor implementations (all providers)
- [x] Unit tests for AgentRunner
- [x] Unit tests for child session creation
- [x] Unit tests for LocalWorktreeManager
- [x] Unit tests for WorktreeGitManager
- [x] Integration tests for in-process agent execution
- [x] Integration tests for workflow tool filtering
- [x] Integration tests for terminal mode with worktrees
- [x] Integration tests for worktree lifecycle
- [x] Fix `test_rejects_outside_project` test failure (error message mismatch)

### Phase 8: Documentation ✅ COMPLETED

> **Status**: All documentation tasks completed.

- [x] Update CLAUDE.md with gobby-agents section
- [x] Update CLAUDE.md with gobby-worktrees section
- [x] Create agent workflow examples
- [x] Document provider configuration
- [x] Document safety guardrails
- [x] Document worktree management patterns

---

## Decisions

| # | Question | Decision | Rationale |
|---|----------|----------|-----------|
| 1 | **Worktree auto-creation** | Yes - auto-create if doesn't exist | Reduces friction for parallel development |
| 2 | **Provider override hierarchy** | CLI > MCP args (if allowed) > workflow > config | Flexibility for ad-hoc use, control for strict workflows |
| 3 | **CLI selection for terminal mode** | Always configurable via `cli` param | Terminal mode uses native CLIs; in-process mode uses SDK providers |
| 4 | **Default agent depth** | max_depth=1 (no nesting by default) | Prevent recursive spawning; workflows can opt-in |
| 5 | **Completion mechanism** | Explicit `complete()` tool call | Structured output, workflow can define schema |
| 6 | **Naming** | `start_agent` not `spawn_agent` | Matches CLI `gobby agents start` |
| 7 | **State persistence** | PostgreSQL for all state (agents + worktrees) | Consistent with Gobby architecture, daemon-level coordination |
| 8 | **Tool availability in subagents** | `complete` always, `start_agent` blocked by default | Safety first, opt-in for orchestration workflows |
| 9 | **Worktree storage** | PostgreSQL `worktrees` table | Centralized registry, consistent with other managers |
| 10 | **Worktree ownership** | Session-based claiming | Track which agent owns which worktree |
| 11 | **Stale detection** | Configurable threshold (default 24h) | Prevent worktree sprawl |
| 12 | **Task-worktree linking** | Optional but encouraged | Traceability without forcing overhead |

---

## Future Enhancements

- **Cross-agent dependencies**: `start_agent(depends_on=[agent_a, agent_b])`
- **Agent pools**: Pre-warmed agents for faster spawning
- **Fleet management**: Remote agent spawning via "mothership"
- **Agent templates**: Pre-configured agent definitions for common patterns
- **Cost tracking**: Track LLM costs per agent run
- **Visual orchestration**: UI for agent workflow visualization
- **Intelligent merge resolution**: AI-powered conflict resolution for worktree merges (see POST_MVP_ENHANCEMENTS.md Phase 1)
