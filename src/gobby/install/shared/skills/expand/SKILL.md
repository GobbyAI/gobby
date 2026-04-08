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

Session identity is automatically provided via context — most task tools no longer require
an explicit `session_id` parameter. Tools like `set_variable` and `get_variable` still
require it — use the value from `Gobby Session ID:` in your system context.

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

2. **If file path**: Read file content, create root epic, and detect phases:

   ```python
   content = Read(file_path)
   plan_file_path = file_path

   # Extract first heading as title
   result = call_tool("gobby-tasks", "create_task", {
       "title": "<first_heading>",
       "description": "<overview section>",
       "task_type": "epic",
       "category": "code"
   })
   root_id = result["ref"]

   # Detect phases: look for ## Phase headings in the plan
   # Each ## Phase N: Name section becomes a sub-epic
   phases = extract_phases(content)  # List of {name, goal, tasks: [{title, category, ...}]}
   ```

3. **If task ref**: Get task details and check for existing children:

   ```python
   task = call_tool("gobby-tasks", "get_task", {"task_id": "<ref>"})
   children = call_tool("gobby-tasks", "list_tasks", {"parent_task_id": task_id})
   if children["tasks"]:
       # Prompt user for confirmation before re-expansion
       # Delete + recreate if confirmed (see re-expansion handling below)
   ```

### Phase 2: Create Phase Hierarchy

**Plans with multiple phases MUST create phase sub-epics.** Each `## Phase` section
becomes a sub-epic under the root. Single-phase plans skip this step.

```python
phase_refs = {}
seen_phase_numbers = set()
for phase in phases:
    phase_number = phase.get("number")
    if not isinstance(phase_number, int) or phase_number in seen_phase_numbers:
        raise ValueError(
            f"Invalid phase number for {phase.get('name') or phase.get('heading')}: {phase_number}"
        )
    seen_phase_numbers.add(phase_number)
    phase_epic = call_tool("gobby-tasks", "create_task", {
        "title": f"Phase {phase_number}: {phase['name']}",
        "task_type": "epic",
        "category": "code",
        "parent_task_id": root_id,
        "description": phase["goal"]
    })
    phase_refs[phase_number] = phase_epic["ref"]
```

**Wire cross-phase dependencies** — if Phase 2 depends on Phase 1:
```python
call_tool("gobby-tasks", "add_dependency", {
    "task_id": phase_refs[2],
    "depends_on": phase_refs[1]
})
```

### Phase 3: Expand Each Phase

For each phase sub-epic, build an expansion spec with only that phase's tasks,
then execute with TDD.

**Option A: Via pipeline** (preferred — spawns researcher for codebase analysis):
```python
for phase_num, phase_ref in phase_refs.items():
    result = None
    if not manual_expansion:
        for attempt in range(2):
            result = call_tool("gobby-workflows", "run_pipeline", {
                "name": "expand-task",
                "inputs": {"task_id": phase_ref, "plan_file": plan_file_path}
            })
            if result and not result.get("error"):
                break
            if result and "not found" not in str(result.get("error", "")).lower() and attempt == 0:
                continue
        if result and not result.get("error"):
            continue
```

**Option B: Direct spec** (when pipeline is unavailable or researcher fails):
```python
phase_results = []
for phase in phases:
    phase_ref = phase_refs[phase["number"]]

    # Build spec with only this phase's tasks
    # depends_on indices are LOCAL to this phase's subtask array
    spec = {
        "plan_file": plan_file_path,
        "subtasks": [
            {
                "id": task["id"],  # required unique ref within this phase
                "title": task["title"],
                "description": task["description"],
                "estimated_time": task.get("estimated_time", 30),
                "dependencies": task.get("dependencies", []),
                "category": task["category"],
                "validation": task["validation"],
                "status": task.get("status"),
                "assignee": task.get("assignee"),
                "metadata": task.get("metadata", {}),
                "tags": task.get("tags", []),
                "depends_on": task["local_depends_on"],  # Indices within THIS phase
                "affected_files": task["affected_files"],
                "priority": task.get("priority", 2)
            }
            for task in phase["tasks"]
        ]
    }

    phase_status = {"phase_number": phase["number"], "phase_ref": phase_ref, "success": False, "errors": []}

    call_tool("gobby-tasks-ops", "save_expansion_spec", {
        "task_id": phase_ref, "spec": spec
    })

    validation = call_tool("gobby-tasks-ops", "validate_expansion_spec", {
        "task_id": phase_ref
    })
    if not validation["valid"]:
        phase_status["errors"].append({"step": "validate_expansion_spec", "errors": validation["errors"]})
        phase_results.append(phase_status)
        continue

    execution = call_tool("gobby-tasks-ops", "execute_expansion", {
        "parent_task_id": phase_ref, "tdd": True
    })
    if execution.get("error"):
        phase_status["errors"].append({"step": "execute_expansion", "error": execution["error"]})
        phase_results.append(phase_status)
        continue

    call_tool("gobby-tasks-ops", "wire_affected_files_from_spec", {
        "parent_task_id": phase_ref
    })
    phase_status["success"] = True
    phase_results.append(phase_status)
```

**Wire cross-phase task dependencies** — after all phases are expanded, wire
dependencies between tasks in different phases (e.g., task 2.1 depends on task 1.2):
```python
# Example: a plan entry with "depends: Phase 1" should wire the first concrete
# task created in Phase 2 to the stored Phase 1 sub-epic ref.
phase_2_first_task_ref = "<task_in_phase_2>"
call_tool("gobby-tasks", "add_dependency", {
    "task_id": phase_2_first_task_ref,
    "depends_on": phase_refs[1]
})
```

Iterate over any expanded task whose plan entry used `depends: Phase N` and wire
that task to `phase_refs[N]` after expansion.

### Phase 4: Report

Show the hierarchical task tree:

```text
Created 3 phases, 12 tasks for #100 "Implement dark mode":

Phase 1: Theme Foundation (#101)
  [TEST] Phase 1: Write failing tests
  #102 [code] Add ThemeProvider component
  #103 [code] Create color token system
  [REF] Phase 1: Refactor with green tests

Phase 2: Component Migration (#104, depends: Phase 1)
  [TEST] Phase 2: Write failing tests
  #105 [code] Migrate Button component (depends: #103)
  #106 [code] Migrate Card component (depends: #103)
  [REF] Phase 2: Refactor with green tests

Use `suggest_next_task` to get the first ready task.
```

### Single-Task Expansion (no phases)

When expanding a single task ref (`#N`) instead of a plan file, skip Phase 2.
Invoke the pipeline directly on the task:

```python
result = call_tool("gobby-workflows", "run_pipeline", {
    "name": "expand-task",
    "inputs": {"task_id": "<task_ref>"}
})
```

Or fall back to direct spec if pipeline is unavailable.

### Re-expansion Handling

If the root epic already has children:
```python
backup = call_tool("gobby-tasks", "get_task", {"task_id": task_id})
print(f"Task has {len(children)} existing subtasks. Re-expansion deletes all.")
# Use AskUserQuestion for confirmation
call_tool("gobby-tasks", "delete_task", {"task_id": task_id, "cascade": True})
# Re-create root with preserved fields, then proceed with Phase 1
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
