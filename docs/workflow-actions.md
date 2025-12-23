# Workflow Actions Reference

This document provides a comprehensive reference for all available actions in the Gobby Workflow Engine (Sprint 6). Actions are the building blocks of workflows, executed in response to hooks (start, end, tool calls, etc.) or within phases.

## State Management

### `load_workflow_state`

Loads the persisted workflow state for the current session.
**Usage:** `on_session_start`

```yaml
- action: load_workflow_state
```

### `save_workflow_state`

Persists the current workflow state to the database.
**Usage:** `on_session_end`, `on_transition`

```yaml
- action: save_workflow_state
```

### `set_variable`

Sets a key-value pair in the workflow variables.
**Usage:** Any trigger

```yaml
- action: set_variable
  name: current_task_index
  value: 0
```

### `increment_variable`

Increments a numeric workflow variable.
**Usage:** Loop iterations

```yaml
- action: increment_variable
  name: retry_count
  amount: 1
```

## Context Injection

### `inject_context`

Injects text content into the next prompt sent to the agent.
**Usage:** `on_enter`, `on_prompt_submit`

```yaml
- action: inject_context
  source: previous_session_summary  # or 'handoff'
  template: |
    ## Previous Session
    {{ summary }}
```

**Sources:** `previous_session_summary`, `handoff`

### `inject_message`

Injects a direct message visible to the agent (system or user channel depending on implementation).
**Usage:** To give instructions

```yaml
- action: inject_message
  content: "You are now in PLANNING mode."
```

### `restore_context`

Restores context from a linked parent session.
**Usage:** `on_session_start`

```yaml
- action: restore_context
  source: parent_session_summary
```

## Session Lifecycle

### `find_parent_session`

Finds and links a parent session marked as `handoff_ready`.
**Usage:** `on_session_start`

```yaml
- action: find_parent_session
  filter:
    status: handoff_ready
```

### `mark_session_status`

Updates the status of the current or parent session.
**Usage:** `on_session_start` (expire parent), `on_session_end` (ready handoff)

```yaml
- action: mark_session_status
  target: parent  # or 'current_session'
  status: expired
```

### `generate_summary`

Generates a markdown summary of the session using an LLM and saves it to the session record.
**Usage:** `on_session_end`, or on-demand

```yaml
- action: generate_summary
  template: "Summarize this session..." # Optional custom prompt
```

### `generate_handoff`

**Legacy/Composite Action**. Generates a summary AND marks the session as `handoff_ready`.
**Usage:** `on_session_end`

```yaml
- action: generate_handoff
```

### `synthesize_title`

Generates a short title for the session based on the transcript.
**Usage:** `on_prompt_submit` (typically once)

```yaml
- action: synthesize_title
  when: "session.title == null"
```

## Artifacts & Files

### `capture_artifact`

Captures the path of a generated file into workflow state.
**Usage:** `on_exit`, `on_tool_result`

```yaml
- action: capture_artifact
  pattern: "**/*.plan.md"
  as: current_plan
```

### `read_artifact`

Reads the content of a captured artifact into a variable.
**Usage:** `on_enter`

```yaml
- action: read_artifact
  pattern: "{{ current_plan }}"
  as: plan_content
```

## Tasks (Beta)

### `persist_tasks`

Persists a list of tasks (dictionaries) to the Gobby Task System.
**Usage:** Plan decomposition

```yaml
- action: persist_tasks
  source: task_list.tasks  # variable containing list of dicts
```

### `write_todos`

Writes a list of todo strings to a file (default `TODO.md`).
**Usage:** UI mirroring

```yaml
- action: write_todos
  filename: "TODO.md"
```

### `mark_todo_complete`

Marks a todo item as complete in a markdown file.
**Usage:** Task completion

```yaml
- action: mark_todo_complete
  todo_text: "Implement feature X"
```

## Advanced

### `call_llm`

Calls an LLM with a prompt and stores the result in a variable.
**Usage:** Decomposition, analysis

```yaml
- action: call_llm
  prompt: "Analyze this code: {{ code }}"
  output_as: analysis_result
```

### `call_mcp_tool`

Invokes a tool on a connected MCP server.
**Usage:** External integrations

```yaml
- action: call_mcp_tool
  server_name: "github"
  tool_name: "create_issue"
  arguments:
    title: "Bug fix"
```

### `switch_mode`

Signals that the agent should switch its behavioral mode.
**Usage:** `on_enter`

```yaml
- action: switch_mode
  mode: plan
```
