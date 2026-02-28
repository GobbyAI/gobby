# Pipeline System — Issues Audit

Found during a deep exploration of the pipeline engine, rules, workflows, and related systems. Categorized by severity.

## Bugs

### 1. Webhook notifier signature mismatch

**Severity**: Runtime TypeError when webhooks are configured

The `ApprovalManager` in `gatekeeper.py` calls the webhook notifier with keyword arguments that don't match the `WebhookNotifier.notify_approval_pending` signature.

**Caller** (`src/gobby/workflows/pipeline/gatekeeper.py:101-109`):
```python
await self.webhook_notifier.notify_approval_pending(
    execution_id=execution.id,   # passes string ID
    step_id=step.id,
    token=token,
    message=message,
    pipeline=pipeline,
)
```

**Receiver** (`src/gobby/workflows/pipeline_webhooks.py:41-47`):
```python
async def notify_approval_pending(
    self,
    execution: PipelineExecution,  # expects PipelineExecution object
    pipeline: PipelineDefinition,
    step_id: str,
    token: str,
    message: str,
) -> None:
```

The caller passes `execution_id=execution.id` (a string) but the receiver expects `execution: PipelineExecution` (a dataclass). This would raise `TypeError: got an unexpected keyword argument 'execution_id'` at runtime. Currently masked because webhooks are rarely configured.

**Fix**: Align the call site to pass the full `execution` object, or align the receiver to accept `execution_id`.

---

### 2. `_ci_pipeline` template produces invalid PipelineStep

**Severity**: Would raise `ValueError` if the template were used to create a PipelineDefinition

**File**: `src/gobby/workflows/workflow_templates.py:167-172`

```python
{
    "id": "approval",
    "approval": {
        "message": "Deploy to production?",
        "approvers": ["admin"],
    },
    # No exec, prompt, mcp, invoke_pipeline, or activate_workflow
},
```

`PipelineStep.model_post_init` requires exactly one execution type. This step has none — only an approval gate. It would fail validation: `"PipelineStep requires at least one execution type"`.

Also, `approvers` is not a valid field on `PipelineApproval` (which has `required`, `message`, `timeout_seconds`).

**Fix**: Add a no-op exec step (e.g., `exec: "echo 'Approved'"`), set `approval.required: true`, and remove the invalid `approvers` field.

---

## Missing Safety Checks

### 3. No nested pipeline depth limit

**Severity**: Stack overflow on recursive/circular pipelines

**File**: `src/gobby/workflows/pipeline_executor.py:433-502`

`_execute_nested_pipeline` calls `self.execute()` recursively with no depth counter. A pipeline can invoke itself:

```yaml
name: infinite-loop
type: pipeline
steps:
  - id: recurse
    invoke_pipeline: infinite-loop
```

Or form cycles: A invokes B, B invokes A. No cycle detection exists.

**Fix**: Add a `depth` parameter to `execute()`, default 0, increment on nested calls, reject at a maximum (e.g., 10). For cycle detection, maintain a set of pipeline names in the call stack.

---

### 4. Approval timeout not enforced

**Severity**: Pipelines stuck indefinitely in WAITING_APPROVAL

**File**: `src/gobby/workflows/definitions.py:409`

`PipelineApproval.timeout_seconds` exists as a model field but is never checked anywhere — not in the gatekeeper, not in any background job, not in any cleanup routine. A pipeline waiting for approval stays in `WAITING_APPROVAL` forever.

**Fix**: Either implement a background task in `runner_maintenance.py` that checks for expired approvals, or remove the field and document that approval timeouts are not supported.

---

### 5. No daemon restart recovery for in-flight executions

**Severity**: Executions permanently stuck in `running` status after daemon restart

**File**: `src/gobby/mcp_proxy/tools/pipelines/_execution.py:13`

Background pipeline tasks are tracked in a module-level `_background_tasks: set[asyncio.Task]`. On daemon restart, these are lost. Executions remain in `running` status in the database with no process driving them forward.

**Fix**: Add a startup recovery routine that queries `pipeline_executions WHERE status = 'running'` and either resumes them or marks them as `failed` with an appropriate error message.

---

## Dead Code

### 6. `_lifecycle_template()` in workflow_templates.py

**Severity**: Harmless but misleading

**File**: `src/gobby/workflows/workflow_templates.py:61-90`

The lifecycle template uses the old `triggers` approach (`on_session_start`, `on_session_stop`, `before_tool`). The modern system uses **rules** (declarative, event-driven, stateless) for this functionality. The `triggers` field on `WorkflowDefinition` still parses but isn't meaningfully integrated with the current rule engine for new workflows.

The template is returned by `get_workflow_templates()` and would produce a workflow that appears valid but doesn't actually enforce anything useful.

**Fix**: Remove `_lifecycle_template()` from the templates list, or rewrite it to use the modern rule-based approach.

---

### 7. `spawn_session` documented but never implemented

**Severity**: Documentation-only (no code impact)

The old `docs/guides/pipelines.md` documented `spawn_session` as a pipeline step type with fields `cli`, `prompt`, `cwd`, `workflow_name`, `agent_depth`. This step type was never implemented:

- Not in `PipelineStep` model (`definitions.py`)
- Not in `pipeline/handlers.py`
- Not in `pipeline_executor.py`'s `_execute_step`
- No grep hits for `spawn_session` in the workflows directory

**Fix**: Removed in the rewritten docs. If session spawning from pipelines is desired, it can be achieved via an `mcp` step calling `gobby-agents:spawn_agent`.

---

## Design Limitations

These are architectural choices, not bugs. Documented here for awareness.

### 8. Nested pipeline outputs not propagated to parent

`_execute_nested_pipeline` returns `{pipeline, execution_id, status}` but not the nested pipeline's actual outputs. A downstream step in the parent pipeline cannot reference `$nested_step.output.some_field` from the inner pipeline's output map.

**Workaround**: Use `get_pipeline_status` in a subsequent MCP step to fetch nested outputs.

---

### 9. Type coercion is implicit and non-configurable

`StepRenderer._coerce_value()` auto-converts all rendered strings: `"600"` → `600`, `"true"` → `True`, `""` → `None`. There's no way to opt out per-field. If an MCP tool expects the string `"600"`, coercion will break it.

**Workaround**: Use explicit quoting tricks or avoid template expressions for string-valued MCP arguments.

---

### 10. No step retry mechanism

A failed step immediately fails the entire pipeline. There's no retry count, backoff, or `on_failure` fallback at the step level.

**Workaround**: Wrap unreliable commands in a shell retry loop within the `exec` string.

---

### 11. exec steps have no shell features

`execute_exec_step` uses `shlex.split` + `create_subprocess_exec` (no shell). This means no pipes (`|`), redirects (`>`), globs (`*`), or shell builtins. This is a deliberate security choice (prevents shell injection) but is not documented.

**Workaround**: Use `sh -c "command | pipe"` as the exec value to get shell features when needed.

---

## Near-Monolith Files

These files are approaching or exceeding the 1,000-line guideline from `CLAUDE.md`:

| File | Lines | Notes |
|------|-------|-------|
| `src/gobby/workflows/sync.py` | 887 | Workflow syncing logic — candidate for splitting sync-from-disk vs sync-to-db |
| `src/gobby/workflows/memory_actions.py` | 825 | Memory extraction/injection — could split by action type |

Neither is over the limit yet, but both are trending toward it.

---

## Deprecated but Not Dead

These use old naming but serve active purposes:

| Item | Location | Status |
|------|----------|--------|
| `WorkflowDefinition.type: Literal["lifecycle", "step"]` | `definitions.py:334` | **Repurposed.** Kept for backward-compat YAML loading. Use `enabled` field instead. |
| `__lifecycle__` / `__ended__` workflow names | `engine.py` | **Active.** Internal sentinel values, not user-facing. |
| Deprecated workflow YAMLs | `install/shared/workflows/deprecated/` | **Intentionally archived.** Sync logic skips `deprecated/` directory. |
| `match` field on `RuleDefinitionBody` | `definitions.py` | **Unused.** Parsed but never evaluated. |
