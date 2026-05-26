# Refined Plan: Lobster-Compatible Pipeline System for Gobby

## Overview

Extend Gobby's workflow engine to support **typed pipelines** with explicit data flow between steps, approval gates with resume tokens, webhook notifications, and MCP tool exposure. This provides **feature parity+ with Lobster** (OpenClaw's workflow engine).

## Context: Why Pipelines First

This implementation enables future agent orchestration patterns. The current meeseeks pattern uses three layers:

```text
auto-task-claude.yaml (Claude parent)
    → spawns meeseeks.yaml (agent definition)
        → activates work-task-gemini.yaml (Gemini worker)
```

Once pipelines exist, orchestration patterns become simpler and more deterministic:

```yaml
# task-orchestration.yaml (future example)
type: pipeline

steps:
  - id: find_tasks
    exec: gobby task list --status=open --json

  - id: spawn_workers
    prompt: |
      For each task in $find_tasks.output, spawn a meeseeks worker.
      Use spawn_agent with isolation=worktree.
    tools: [mcp__gobby__call_tool]

  - id: wait_for_completion
    exec: gobby task wait {{ inputs.task_id }}

  - id: review
    approval:
      required: true
      message: "All tasks complete. Approve to finalize?"
```

**Key benefit**: Pipelines are deterministic - the orchestration logic is explicit and predictable, while workers remain interactive.

## Lobster Feature Parity Matrix

| Lobster Feature | Gobby Implementation | Status |
| ----------------- | --------------------- | -------- |
| Typed pipelines (JSON data flow) | `$step.output` references with JSON | Parity |
| Approval gates with resume tokens | `ApprovalRequired` exception + token | Parity |
| Deterministic execution | Sequential step execution | Parity |
| Local-first execution | PostgreSQL persistence | Parity |
| YAML/JSON workflow files | `.gobby/workflows/*.yaml` | Parity |
| CLI run/approve/reject | `gobby pipeline run/approve/reject` | Parity |
| `.lobster` file format | Native import + direct execution | Parity |
| Output envelope (ok/needs_approval/cancelled) | Same JSON response format | Parity |
| Composable pipelines | `invoke_pipeline` step type | **Parity+** |
| LLM-powered steps | `prompt` field with tool restrictions | **Parity+** |
| Webhook notifications | Configurable webhooks on approval | **Parity+** |
| MCP tool exposure | `expose_as_tool: true` | **Parity+** |
| Run from lifecycle/step workflows | `run_pipeline` action | **Parity+** |
| HTTP API for approvals | `/api/pipelines/approve/{token}` | **Parity+** |

## Key Design Decision: Pipelines as Third Workflow Type

After analyzing the codebase, pipelines should be implemented as a **third workflow type** (`type: "pipeline"`) alongside existing "lifecycle" and "step" types. This:

- Reuses `WorkflowLoader` (discovery, inheritance, caching)
- Maintains consistency with existing YAML patterns
- Enables gradual adoption without breaking existing workflows

However, pipelines need a **dedicated executor** because their execution model differs:

- Step workflows: event-driven state machine, reacts to hooks
- Pipelines: sequential execution with data flow, runs to completion or approval pause

---

## Architecture

Pipelines are built **inside the existing `workflows/` module** as a third workflow type, maximizing code reuse:

```text
src/gobby/
├── workflows/                    # EXTEND existing module
│   ├── definitions.py           # MODIFY: Add PipelineDefinition, PipelineStep models
│   ├── loader.py                # MODIFY: Parse type: pipeline workflows
│   ├── pipeline_executor.py     # NEW: PipelineExecutor (sequential execution)
│   ├── pipeline_state.py        # NEW: PipelineExecution, StepExecution models
│   ├── webhooks.py              # NEW: Webhook notification service
│   └── actions.py               # MODIFY: Add run_pipeline action
│
├── storage/
│   ├── migrations.py            # MODIFY: Add migration 80 for pipeline tables
│   └── pipelines.py             # NEW: LocalPipelineExecutionManager
│
├── mcp_proxy/
│   ├── registries.py            # MODIFY: Add pipelines registry setup
│   └── tools/pipelines/         # NEW: Pipeline MCP tools
│       ├── __init__.py          # create_pipelines_registry()
│       └── _execution.py        # run_pipeline, approve, reject, status
│
└── cli/
    ├── __init__.py              # MODIFY: Register pipelines group
    └── pipelines.py             # NEW: Pipeline CLI commands
```

**Why inside `workflows/`:**

- Pipelines ARE workflows - just a different execution model
- Reuses: `WorkflowLoader`, `ConditionEvaluator`, `TemplateEngine`, `ActionExecutor`
- `WorkflowDefinition.type` already supports multiple values - just add `"pipeline"`
- Single location for all workflow-related code
- Easier to share patterns between lifecycle/step/pipeline types

---

## Implementation Phases

### Phase 1: Data Models & Storage

**Files to create:**

- `src/gobby/workflows/pipeline_state.py` - Execution state dataclasses
- `src/gobby/storage/pipelines.py` - `LocalPipelineExecutionManager`

**Files to modify:**

- `src/gobby/workflows/definitions.py` - Add `PipelineDefinition`, `PipelineStep` models
- `src/gobby/storage/migrations.py` - Add migration 80:

```sql
CREATE TABLE pipeline_executions (
    id TEXT PRIMARY KEY,
    pipeline_name TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id),
    status TEXT NOT NULL DEFAULT 'pending',
    inputs_json TEXT,
    outputs_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    resume_token TEXT UNIQUE,
    session_id TEXT REFERENCES sessions(id) ON DELETE SET NULL,
    parent_execution_id TEXT REFERENCES pipeline_executions(id) ON DELETE CASCADE
);

CREATE TABLE step_executions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    execution_id TEXT NOT NULL REFERENCES pipeline_executions(id) ON DELETE CASCADE,
    step_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    started_at TEXT,
    completed_at TEXT,
    input_json TEXT,
    output_json TEXT,
    error TEXT,
    approval_token TEXT UNIQUE,
    approved_by TEXT,
    approved_at TEXT,
    UNIQUE(execution_id, step_id)
);

CREATE INDEX idx_pipeline_executions_project ON pipeline_executions(project_id);
CREATE INDEX idx_pipeline_executions_status ON pipeline_executions(status);
CREATE INDEX idx_pipeline_executions_resume_token ON pipeline_executions(resume_token);
CREATE INDEX idx_step_executions_execution ON step_executions(execution_id);
CREATE INDEX idx_step_executions_approval_token ON step_executions(approval_token);
```

### Phase 2: Parser & Loader Integration

**Files to modify:**

- `src/gobby/workflows/loader.py` - Extend to handle `type: pipeline`:

```python
# After parsing YAML, check type
if data.get('type') == 'pipeline':
    # Validate $step.output references
    self._validate_pipeline_references(data)
    return PipelineDefinition(**data)
```

**Loader additions:**

- `_validate_pipeline_references()` - Ensure steps only reference earlier steps
- `_extract_step_refs()` - Parse `$step_id.output` patterns from prompts/conditions
- `load_pipeline()` - Public method to load pipeline by name

**Reused infrastructure:**

- YAML parsing (already exists)
- Inheritance via `extends:` (already exists)
- Discovery and caching (already exists)
- `ConditionEvaluator` for step conditions
- `TemplateEngine` for prompt rendering

### Phase 3: Executor & Webhooks

**Files to create:**

- `src/gobby/workflows/pipeline_executor.py` - `PipelineExecutor` class
- `src/gobby/workflows/webhooks.py` - `WebhookNotifier` class

**Key components:**

```python
class ApprovalRequired(Exception):
    """Raised when a step requires approval."""
    def __init__(self, execution_id: str, step_id: str, token: str, message: str): ...

class PipelineExecutor:
    def __init__(
        self,
        db: HubDatabase,
        execution_manager: LocalPipelineExecutionManager,
        llm_service: LLMService,
        template_engine: TemplateEngine,
        webhook_notifier: WebhookNotifier | None = None,
    ): ...

    async def execute(
        self,
        pipeline: PipelineDefinition,
        inputs: dict,
        execution_id: str | None = None,
        project_id: str | None = None,
    ) -> PipelineExecution: ...

    async def approve(self, token: str, approved_by: str = "user") -> PipelineExecution: ...
    async def reject(self, token: str, rejected_by: str = "user") -> PipelineExecution: ...
```

**Execution flow:**

1. Create/load execution record
2. Build context with inputs and completed step outputs
3. For each step:
   - Check condition (skip if false)
   - Check approval gate (raise `ApprovalRequired` if needed)
   - **Send webhook notification** for approval-pending state
   - Render prompt with Jinja2 + `$step.output` conversion
   - Execute via LLM with tool restrictions
   - Save step output to context
4. Resolve output references
5. Mark complete
6. **Send webhook notification** for completion/failure

**Step execution modes** (Parity+ with Lobster):

- **exec**: Run shell command, capture stdout/stderr as JSON
- **prompt**: Call LLM with prompt template and tool restrictions
- **invoke_pipeline**: Nested pipeline execution (composability)

### Phase 3b: Webhook Notifications

**Webhook configuration** (in pipeline YAML or global config):

```yaml
# Pipeline-level webhook config
webhooks:
  on_approval_pending:
    url: "https://api.example.com/approvals"
    method: POST
    headers:
      Authorization: "Bearer ${WEBHOOK_TOKEN}"
  on_complete:
    url: "https://api.example.com/completions"
  on_failure:
    url: "https://api.example.com/failures"
```

**WebhookNotifier class:**

```python
class WebhookNotifier:
    async def notify_approval_pending(
        self,
        execution: PipelineExecution,
        step_id: str,
        token: str,
        message: str,
    ) -> None: ...

    async def notify_complete(self, execution: PipelineExecution) -> None: ...
    async def notify_failure(self, execution: PipelineExecution, error: str) -> None: ...
```

**Webhook payload format:**

```json
{
  "event": "approval_pending",
  "execution_id": "pe-abc123",
  "pipeline_name": "code-review",
  "step_id": "human_review",
  "resume_token": "xyz789...",
  "message": "Review found 3 issues. Approve to proceed.",
  "approve_url": "https://gobby.local/api/pipelines/approve/xyz789",
  "reject_url": "https://gobby.local/api/pipelines/reject/xyz789",
  "timestamp": "2026-02-01T12:00:00Z"
}
```

### Phase 4: MCP Tool Exposure

**Files to create:**

- `src/gobby/mcp_proxy/tools/pipelines/__init__.py`
- `src/gobby/mcp_proxy/tools/pipelines/_execution.py`

**Files to modify:**

- `src/gobby/mcp_proxy/registries.py` - Add to `setup_internal_registries()`:

```python
if pipeline_executor is not None:
    from gobby.mcp_proxy.tools.pipelines import create_pipelines_registry
    pipelines_registry = create_pipelines_registry(
        loader=workflow_loader,
        executor=pipeline_executor,
        execution_manager=pipeline_execution_manager,
    )
    manager.add_registry(pipelines_registry)
```

**Tools to expose:**

- `run_pipeline(name, inputs, project_path)` - Execute a pipeline
- `approve_pipeline(token, approved_by)` - Approve pending step
- `reject_pipeline(token, rejected_by)` - Reject and cancel
- `get_pipeline_status(execution_id)` - Get execution status
- `list_pipelines(project_path)` - List available pipelines

**Dynamic tool generation** for `expose_as_tool: true` pipelines:

- Generate `pipeline_{name}` tools at startup
- Input schema derived from pipeline `inputs` definition

### Phase 5: CLI Commands

**Files to create:**

- `src/gobby/cli/pipelines.py`

**Files to modify:**

- `src/gobby/cli/__init__.py` - Register `pipelines` group

**Commands:**

```bash
gobby pipeline list              # List available pipelines
gobby pipeline show <name>       # Show pipeline definition
gobby pipeline run <name> -i key=value  # Run pipeline
gobby pipeline status <id>       # Show execution status
gobby pipeline approve <token>   # Approve pending step
gobby pipeline reject <token>    # Reject and cancel
gobby pipeline history <name>    # Show execution history
gobby pipeline export <id>       # Export for replay/audit
```

### Phase 6: HTTP API Endpoints

**Files to modify:**

- `src/gobby/servers/http.py` - Add pipeline routes

**Endpoints** (for webhook approve/reject links):

```text
POST /api/pipelines/run
  Body: { "name": "...", "inputs": {...}, "project_id": "..." }
  Response: { "status": "ok"|"needs_approval", "execution_id": "...", ... }

GET  /api/pipelines/{execution_id}
  Response: Execution status and step details

POST /api/pipelines/approve/{token}
  Response: { "status": "ok"|"needs_approval", ... }

POST /api/pipelines/reject/{token}
  Response: { "status": "cancelled", ... }
```

These endpoints enable:

- Webhook-triggered approvals (click link in Slack/email to approve)
- External system integration
- Web UI for pipeline management (future)

### Phase 7: Workflow Integration (run_pipeline action)

**Files to modify:**

- `src/gobby/workflows/actions.py` - Add `run_pipeline` action handler

**New action for step/lifecycle workflows:**

```yaml
# In a lifecycle workflow trigger
triggers:
  on_after_tool:
    - when: "tool_name == 'close_task'"
      actions:
        - action: run_pipeline
          pipeline: code-review-pipeline
          inputs:
            task_id: "{{ tool_args.task_id }}"
          await: false  # Run async (don't block workflow)

# In a step workflow on_enter
steps:
  - name: review
    on_enter:
      - action: run_pipeline
        pipeline: pr-review
        inputs:
          branch: "{{ variables.feature_branch }}"
        await: true  # Block until pipeline completes or needs approval
```

**Action handler:**

```python
# In ActionExecutor._register_defaults()
@self.register("run_pipeline")
async def run_pipeline_action(
    ctx: ActionContext,
    pipeline: str,
    inputs: dict | None = None,
    await_completion: bool = False,
) -> dict:
    """Run a pipeline from within a workflow."""
    execution = await ctx.pipeline_executor.execute(
        pipeline=await ctx.workflow_loader.load_pipeline(pipeline),
        inputs=inputs or {},
        session_id=ctx.session_id,
        project_id=ctx.project_id,
    )

    if await_completion and execution.status == ExecutionStatus.WAITING_APPROVAL:
        # Store execution_id in workflow state for later resume
        ctx.state.variables["pending_pipeline"] = execution.id

    return {
        "execution_id": execution.id,
        "status": execution.status.value,
    }
```

**Use cases enabled:**

- Trigger code review pipeline when task closes
- Run TDD pipeline when entering "implement" step
- Chain pipelines from lifecycle events (pre-commit, post-push)
- Orchestrate complex workflows that mix event-driven + sequential execution

### Phase 8: Lobster Workflow Import

**Files to create:**

- `src/gobby/workflows/lobster_compat.py` - Lobster format compatibility

**Import capability:**

```python
class LobsterImporter:
    def import_file(self, path: Path) -> PipelineDefinition:
        """Import a .lobster file and convert to Gobby pipeline."""
        ...

    def convert_step(self, lobster_step: dict) -> PipelineStep:
        """Convert Lobster step syntax to Gobby step."""
        # Map: stdin: $step.stdout → input: $step.output
        # Map: command → exec step type
        # Map: approval → approval gate
        ...
```

**CLI command:**

```bash
gobby pipeline import path/to/workflow.lobster    # Import and save as .yaml
gobby pipeline run --lobster path/to/workflow.lobster  # Run directly
```

**Lobster → Gobby mapping:**

| Lobster Syntax | Gobby Equivalent |
| ---------------- | ------------------ |
| `stdin: $step.stdout` | `input: $step.output` |
| `stdin: $step.json` | `input: $step.output` (JSON is default) |
| `command: "..."` | `exec: "..."` step type |
| `approval: true` | `approval: { required: true }` |
| `condition: $step.approved` | `condition: "$step.approved"` |
| `args` (workflow inputs) | `inputs` section |

**Native .lobster execution:**

- Parser detects `.lobster` extension
- Auto-converts to internal `PipelineDefinition`
- No conversion step needed for users migrating from OpenClaw

### Phase 9: Documentation

**Files to create:**

- `docs/workflows/pipelines.md` - Pipeline workflow guide
- `docs/workflows/lobster-migration.md` - Migration guide from Lobster/OpenClaw

**Documentation structure:**

```markdown
# docs/workflows/pipelines.md
- Overview: What are pipeline workflows?
- Quick start: Your first pipeline
- YAML schema reference
- Step types: exec, prompt, invoke_pipeline
- Data flow: $step.output references
- Approval gates and resume tokens
- Webhook notifications
- MCP tool exposure (expose_as_tool)
- CLI reference
- HTTP API reference

# docs/workflows/lobster-migration.md
- Lobster compatibility overview
- Importing .lobster files
- Syntax mapping table
- Parity+ features (LLM steps, webhooks, MCP exposure)
- Running Lobster workflows directly
- Converting a Lobster workflow step-by-step
```

**Update existing docs:**

- `docs/workflows/README.md` - Add pipeline type to overview
- `CLAUDE.md` - Add pipeline commands to quick reference

---

## Refinements from Original Plan

1. **Project scoping**: Add `project_id` to executions (matches existing patterns)
2. **Session context**: Make `session_id` optional - pipelines can run standalone
3. **ID format**: Use `pe-{12hex}` for execution IDs (matches `ar-` pattern)
4. **Storage pattern**: Follow `LocalTaskManager` pattern with `from_row()`/`to_dict()`
5. **Progressive disclosure**: Pipelines discoverable via `list_pipelines`, full definition via `get_pipeline`
6. **Webhook notifications**: Send HTTP notifications on approval-pending, complete, failure
7. **Lobster output envelope**: Match `ok`/`needs_approval`/`cancelled` response format
8. **Step types**: Support `exec` (shell), `prompt` (LLM), `invoke_pipeline` (nested)

## Deferred Features (v2)

- **Parallel steps**: Adds significant complexity, defer (not in Lobster either)
- **Versioning**: Store definition hash with execution for replay fidelity

---

## Verification Plan

1. **Unit tests**: Parser validation, executor state transitions
2. **Integration tests**:
   - Create pipeline YAML in `.gobby/workflows/`
   - Run via CLI: `gobby pipeline run test-pipeline -i files='["a.py"]'`
   - Verify approval gate pauses execution
   - Resume via: `gobby pipeline approve <token>`
3. **MCP tests**:
   - Call `list_tools("gobby-pipelines")`
   - Call `run_pipeline` via MCP
   - Verify `waiting_approval` response
4. **End-to-end**: Run example TDD pipeline from original plan

---

## Critical Files Reference

| File | Purpose |
| ------ | --------- |
| `src/gobby/workflows/definitions.py` | Add PipelineDefinition, PipelineStep models |
| `src/gobby/workflows/loader.py` | Extend to load type: pipeline workflows |
| `src/gobby/workflows/engine.py` | Reference for execution patterns |
| `src/gobby/workflows/actions.py` | Add run_pipeline action, reference patterns |
| `src/gobby/workflows/evaluator.py` | Reuse ConditionEvaluator for step conditions |
| `src/gobby/storage/migrations.py` | Add migration 80 for pipeline tables |
| `src/gobby/mcp_proxy/registries.py` | Registry setup pattern |
| `src/gobby/mcp_proxy/tools/tasks/_factory.py` | Complex registry pattern reference |
| `src/gobby/cli/workflows.py` | CLI command patterns |
| `src/gobby/servers/http.py` | Add HTTP API routes |
