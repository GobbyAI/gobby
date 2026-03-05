---
name: expand
description: "Use when user asks to '/gobby expand', 'expand task', 'break down task', 'decompose task'. Expand a task into subtasks using the expand-task pipeline. Survives session compaction."
category: core
triggers: expand task, break down, subtask, decompose
metadata:
  gobby:
    audience: interactive
    depth: 0
---

# /gobby expand - Task Expansion Skill

Thin wrapper around the `expand-task` pipeline. Validates input, delegates to the pipeline,
and reports results. The pipeline spawns a researcher agent for codebase analysis, then
mechanically validates and executes the expansion.

## Input Formats

- `#N` - Task reference (e.g., `/gobby expand #42`)
- `path.md` - Plan file (creates root task first, e.g., `/gobby expand docs/plan.md`)

## Session Context

**IMPORTANT**: Use the `session_id` from your SessionStart hook context.

Look for `Gobby Session Ref:` or `Gobby Session ID:` in your system context:
```
Gobby Session Ref: #5
Gobby Session ID: <uuid>
```

**Note**: All `session_id` parameters accept #N, N, UUID, or prefix formats.

## Tool Schema Reminder

**First time calling a tool this session?** Use `get_tool_schema(server_name, tool_name)` before `call_tool` to get correct parameters. Schemas are cached per session—no need to refetch.

## Workflow

### Phase 0: Check for Resume

First, check if there's a pending expansion to resume:

```python
result = call_tool("gobby-tasks", "get_expansion_spec", {"task_id": "<ref>"})
if result.get("pending"):
    # Skip to Phase 3 — spec already saved, just needs execution
    print(f"Resuming expansion with {result['subtask_count']} subtasks")
    # Jump to Phase 3
```

If `pending=True`, skip to **Phase 3** immediately.

### Phase 1: Prepare

1. **Parse input**: Task ref (`#N`) or file path (`plan.md`)

2. **If file path**: Read file content, create root task:
   ```python
   content = Read(file_path)
   # Extract first heading as title
   result = call_tool("gobby-tasks", "create_task", {
       "title": "<first_heading>",
       "description": content,
       "task_type": "epic",
       "session_id": "<session_id>"
   })
   task_id = result["task"]["id"]
   ```

3. **Get task details**:
   ```python
   task = call_tool("gobby-tasks", "get_task", {"task_id": "<ref>"})
   ```

4. **Check for existing children** and handle re-expansion:
   ```python
   children = call_tool("gobby-tasks", "list_tasks", {"parent_task_id": task_id})
   if children["tasks"]:
       # IMPORTANT: Re-expansion will delete all existing subtasks.
       backup = call_tool("gobby-tasks", "get_task", {"task_id": task_id})

       # Prompt user for confirmation
       print(f"Task #{task_id} has {len(children['tasks'])} existing subtasks.")
       print("Re-expansion will delete all subtasks and their descendants.")
       # Use AskUserQuestion for confirmation

       # Delete parent cascades to children
       call_tool("gobby-tasks", "delete_task", {"task_id": task_id, "cascade": True})

       # Re-create the parent task with ALL preserved fields
       result = call_tool("gobby-tasks", "create_task", {
           "title": backup["title"],
           "description": backup["description"],
           "task_type": backup["type"],
           "priority": backup.get("priority"),
           "labels": backup.get("labels", []),
           "validation_criteria": backup.get("validation_criteria"),
           "category": backup.get("category"),
           "session_id": "<session_id>"
       })
       task_id = result["task"]["id"]
   ```

### Phase 2: Invoke Pipeline

Delegate to the `expand-task` pipeline. This spawns a researcher agent that explores
the codebase and produces a spec, then validates and executes it mechanically.

```python
result = call_tool("gobby-workflows", "run_pipeline", {
    "name": "expand-task",
    "session_id": "<session_id>",
    "inputs": {
        "task_id": "<task_ref>",
        "session_id": "<session_id>"
    },
    "wait": True,
    "wait_timeout": 600
})
```

If the pipeline completes successfully, skip to **Phase 4**.

If the pipeline fails at the validation step, report the validation errors
and ask the user how to proceed.

### Phase 3: Manual Execution (Resume Path)

If resuming from a pending spec (Phase 0) or the pipeline is not available,
execute the expansion directly:

```python
# Validate first
validation = call_tool("gobby-tasks", "validate_expansion_spec", {
    "task_id": "<ref>"
})
if not validation["valid"]:
    print(f"Spec validation failed: {validation['errors']}")
    # Ask user to fix or re-run expansion
    return

# Execute
result = call_tool("gobby-tasks", "execute_expansion", {
    "task_id": "<ref>",
    "session_id": "<session_id>"
})

# Wire affected files
call_tool("gobby-tasks", "wire_affected_files_from_spec", {
    "parent_task_id": "<ref>"
})
```

### Phase 4: Report

Show the created task tree with refs, dependencies, and description sizes:

```
Created 3 subtasks for #42 "Implement user authentication":

#43 [code] Add User model with password hashing
    dep: none
    validation: Tests pass. User model exists with hash_password method.

#44 [code] Implement login endpoint (depends on #43)
    dep: #43
    validation: Tests pass. POST /login returns JWT on valid credentials.

#45 [code] Add logout endpoint (depends on #44)
    dep: #44
    validation: Tests pass. POST /logout invalidates session.

Use `suggest_next_task` to get the first ready task.
```

## Error Handling

**Task not found**:
```
Error: Task #42 not found. Verify the task reference exists.
```

**Pipeline not available**: Fall back to Phase 3 (manual execution path).

**Validation failed**: Report errors from `validate_expansion_spec` and
ask the user how to proceed (fix spec, re-run researcher, or override).

**Session compaction recovery**:
If expansion was interrupted after the researcher saved the spec, the skill
detects the pending spec in Phase 0 and resumes from Phase 3 automatically.

## See Also

- [Task Expansion Guide](docs/guides/task-expansion.md) — How expansion works end-to-end
- [TDD Enforcement Guide](docs/guides/tdd-enforcement.md) — TDD sandwich pattern applied during expansion
- [Orchestrator Guide](docs/guides/orchestrator.md) — How the orchestrator invokes expansion
