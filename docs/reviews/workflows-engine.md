# Review: workflows engine + pipelines

- **Scope:** `src/gobby/workflows/` execution layer — `pipeline_executor.py`,
  `pipeline_heartbeat.py`, `pipeline_state.py`, `pipeline_webhooks.py`,
  `pipeline/renderer.py`, `pipeline/gatekeeper.py`, `pipeline/handlers.py`,
  `state_manager.py`, `step_context.py`, `dry_run.py`, `summary_actions.py`,
  `webhook_executor.py`, `webhook.py`, `sync_pipelines.py`, `constants.py`,
  `__init__.py`. Cross-seam reads into `storage/pipelines.py`,
  `storage/sessions/_field_update.py`, `runner_init/orchestration.py`,
  `runner_maintenance.py`, `system_automation.py`, `dispatch/dispatcher.py`,
  `servers/routes/pipelines.py`, `servers/routes/sessions/analytics.py`,
  `cli/pipelines.py`, `mcp_proxy/tools/workflows/`, and
  `communications/reactions.py` where this layer's contracts are honored or
  broken. **Split boundary:** the rule layer (engine/, enforcement/,
  safe_evaluator, definitions, loaders, sync_rules/sync_variables, observers,
  hooks.py) was reviewed in `workflows-rules.md` (#15775) and read here only as
  consumers.
- **Reviewer:** Claude Fable 5 — 7-agent parallel fan-out, all Blockers synthesizer-verified link-by-link against source.
- **Commit / branch:** `0.5.0` @ HEAD `8302107c8` (working tree clean at review time).
- **Summary:** 9 Blocker · 34 Important · 24 Nit — the pipeline approval gate
  is broken end-to-end (the approved action never runs, tokens replay forever,
  nested gates are swallowed), the heartbeat safety net actively corrupts
  healthy executions, and every lifecycle state transition is an unguarded
  last-writer-wins UPDATE. The sync wipe family found in the rules layer
  recurs here in `sync_pipelines.py`, in worse form.

## Findings

### [BLOCKER] Approving a gated step never executes the step's action — approval silently skips the gated work
- **Where:** `src/gobby/workflows/pipeline/gatekeeper.py:168-172` (approve marks step `COMPLETED`); `src/gobby/workflows/pipeline_executor.py:424-435` (resume skips `COMPLETED` steps); gate-before-action ordering at `pipeline_executor.py:506` (gate) → `:511` (`_execute_step`).
- **Failure mode:** (1) A step with `approval.required=true` hits `check_approval_gate` *before* its action runs; the gate sets step+execution `WAITING_APPROVAL` and raises `ApprovalRequired` (`gatekeeper.py:92-143`). (2) The operator approves; `approve_step` marks the step `StepStatus.COMPLETED` (`gatekeeper.py:168-172`) without ever running the action. (3) `PipelineExecutor.approve` (`pipeline_executor.py:793-834`) resumes `execute(execution_id=...)`; the resume loop sees `COMPLETED` and skips with `context["steps"][id] = {"output": None}` (`:424-435`). The step's `exec`/`prompt`/`mcp` action never executes — and `PipelineStep.model_post_init` (`definitions.py:587-607`) requires every step to carry exactly one action, so **every approval gate in every pipeline drops its action**. The pipeline then reports `completed`. Downstream steps reading `steps.<gated>.output` get `None` and their conditions fail closed, silently skipping more steps.
- **Why it matters:** This is the canonical use case (`docs/guides/pipelines.md:154`: approval is a gate "before execution"; the project's own fixture pairs `exec="echo deploy"` with "Approve deployment?"). Approving a deployment means the deployment does not happen, yet the pipeline reports success. Success-while-contract-violated.
- **Minimal fix:** `approve_step` should record `approved_by`/`approved_at` and reset the step to `PENDING` (clearing the token); the executor's gate check should consult `approved_at` (or an `approved` status) and proceed to `_execute_step` instead of re-raising. `approved_at` is currently write-only. Existing approve/resume tests use `MagicMock` managers and hand-fake the COMPLETED step's output, so they pin the bug — add an integration test asserting the gated action runs after approve.
- **Confidence:** high — ordering, approve path, and resume skip all verified; no alternate re-execution path exists.

### [BLOCKER] Approval tokens are never invalidated and approve/reject have no status preconditions — replay corrupts terminal executions
- **Where:** `src/gobby/workflows/pipeline/gatekeeper.py:163-179` (approve), `:199-223` (reject); `src/gobby/storage/pipelines.py:677-690` (`get_step_by_approval_token` — no status filter), `:640-642` (`update_step_execution` only ever *sets* `approval_token`; the sole clearing site is `reset_steps_from` `:875`, used only by the MCP resume tool), `:105-156` (`update_execution_status` — unconditional UPDATE, no terminal-state guard).
- **Failure mode:** Three verified corruptions: (1) **Reject-after-complete** — pipeline pauses, is approved, runs to `COMPLETED`; the token is still live; `reject(token)` finds the step, marks it `FAILED`, and flips the completed execution to `CANCELLED` (`gatekeeper.py:208-218`) — a successful terminal record destroyed. (2) **Approve-after-timeout/reject** — the expiry loop (`runner_maintenance.py:482-505`) and `reject_step` leave the token; a late `approve(token)` rewrites the step to `COMPLETED` with `approved_by` set on a cancelled execution — inconsistent audit state. (3) **Double-approve** — two concurrent `approve(token)` calls both pass the token lookup, both resume `execute()` for the same execution (no per-execution mutex; resume only rejects COMPLETED/CANCELLED at entry, `pipeline_executor.py:287-299`) — remaining side-effectful steps run twice. Also: re-resuming a WAITING_APPROVAL execution re-runs `check_approval_gate`, generating a *new* token that overwrites one already delivered via webhook/CLI.
- **Why it matters:** The token is the entire security boundary of the gate (tokens are broadcast in events, webhooks, and CLI output by design). Single-use and status preconditions are table stakes; without them the gate is replayable and terminal records are corruptible indefinitely. Found independently by three reviewers.
- **Minimal fix:** In `approve_step`/`reject_step`, perform the transition as one conditional UPDATE (`SET status=%s, approval_token=NULL ... WHERE id=%s AND status='waiting_approval'`) inside a transaction, treating zero rows as "token already consumed"; add a terminal-state guard (or expected-status CAS) to `update_execution_status`.
- **Confidence:** high.

### [BLOCKER] Nested pipeline `ApprovalRequired` and failures are swallowed into a COMPLETED parent step
- **Where:** `src/gobby/workflows/pipeline_executor.py:922-927` (`except Exception as e: return {"pipeline": ..., "error": str(e)}`); `ApprovalRequired(Exception)` at `pipeline_state.py:182`; parent failure detection checks only `exit_code` at `pipeline_executor.py:514`; step marked COMPLETED at `:544-548`.
- **Failure mode:** A child `execute()` hits an approval gate and raises `ApprovalRequired` (re-raised at `:604-606`). `_execute_nested_pipeline`'s `except Exception` (`:922`) catches it — `ApprovalRequired` subclasses `Exception` — and returns `{"pipeline": name, "error": "Approval required..."}`. The parent's failure check `step_output.get("exit_code", 0) != 0` sees no `exit_code`, so the `invoke_pipeline` step is marked `COMPLETED` and the parent keeps running — the child's approval gate is bypassed from the parent's perspective; a later approval resumes the child detached from the already-finished parent. The identical path converts child *failures* (failed child steps, depth-limit, cycle errors) into parent-step success.
- **Why it matters:** An enforcement gate silently bypassed plus partial failure reported as success — both at the Blocker bar.
- **Minimal fix:** `except ApprovalRequired: raise` before the broad handler, and treat an `"error"` key in `invoke_pipeline` step output as failure (mirroring the `exit_code` check).
- **Confidence:** high.

### [BLOCKER] Resuming a FAILED execution always crashes on `UNIQUE(execution_id, step_id)`
- **Where:** `src/gobby/workflows/pipeline_executor.py:411-417` (existing steps deliberately *not* loaded when `prior_status == FAILED`), `:454-464` (`create_step_execution` for "missing" steps); `storage/pipelines.py:552-590` (plain INSERT); `storage/postgres_baseline_schema.sql:1131` (`UNIQUE(execution_id, step_id)`).
- **Failure mode:** `execute(execution_id=X)` with prior status FAILED (not terminal per `:287`) → `existing_steps = {}` → first step: `create_step_execution` INSERTs a row that already exists from the failed attempt → `UniqueViolation` → generic handler re-marks the execution FAILED with the constraint error, clobbering the original failure diagnostics. Even surviving the INSERT, the safety net (`:561-570`) would still see the old FAILED rows and raise. Reachable by replaying a stale approval token after a later step failed, or via the documented resume API. The MCP `resume_pipeline` tool works only because it pre-resets rows to PENDING first (`mcp_proxy/tools/workflows/_pipeline_execution.py:410-430`) — it routes around the broken executor path.
- **Why it matters:** A documented, test-pinned behavior ("Failed executions re-execute all steps", `:412-414`) is 100% broken against real storage; the pinning test passes only because it mocks the execution manager.
- **Minimal fix:** In the FAILED-resume branch, reset existing rows (reuse `reset_steps_from` semantics) instead of skipping `existing_steps`, or make `create_step_execution` an upsert that resets the row.
- **Confidence:** high.

### [BLOCKER] Heartbeat probes the wrong session and the executor never refreshes `updated_at` — healthy >120s pipelines are marked FAILED mid-flight
- **Where:** `src/gobby/workflows/pipeline_heartbeat.py:203-216` (`_has_alive_agents` uses `execution.session_id`; NULL → False at `:209-210`), `:191-197` (unguarded FAILED write); `storage/pipelines.py:934-958` (stall scan: RUNNING + `updated_at` older than threshold; default 120s at `pipeline_heartbeat.py:114`); executor writes execution status only at start/approval/terminal (`pipeline_executor.py:318,565,574`) and `update_step_execution` touches only `step_executions`; the child session created at `:341-352` is **never written back** to the execution row, while the execution row stores the *caller* session (`:308-313`) and step-spawned agents parent to the *child* session (context `session_id` at `:402`); dispatcher-created executions often have `session_id=NULL` (`dispatch/dispatcher.py:678-707`).
- **Failure mode:** The automation loop ticks every 60s. Any agent-less stretch longer than 120s — exec steps default to a 300s timeout (`pipeline/handlers.py:121`), wait steps to 600s, prompt steps are LLM-bound; bundled `expand-task.yaml` waits up to 600s — gets flagged stalled. The liveness rescue then checks `list_by_parent(execution.session_id)` — the caller session (rarely has running agent rows) or NULL (instantly "dead") — never the pipeline child session where step agents actually live. The heartbeat writes `FAILED` while the executor coroutine is still running; the executor later silently overwrites FAILED→COMPLETED (no CAS), but during the window observers see a false failure: MCP `resume_pipeline` becomes callable on a live execution (requires FAILED), automation can re-dispatch duplicate work, and `completed_at` keeps the heartbeat's wrong timestamp via COALESCE.
- **Why it matters:** The safety net corrupts state for the mainline `gobby build` automation path (dispatcher → expand-task → 600s wait) under completely normal use. The existing test seeds agents directly under `execution.session_id` (`tests/workflows/test_pipeline_heartbeat.py:98-111`), so it never exercises the real topology.
- **Minimal fix:** Store the pipeline child session id on the execution row (or resolve descendants in `_has_alive_agents`); treat `session_id IS NULL` as "unknown, skip"; bump the parent execution's `updated_at` on each step transition; make the FAILED write a guarded CAS (`WHERE status='running' AND updated_at < cutoff`).
- **Confidence:** high — every link verified; the 60s loop plus 600s bundled waits make the timing routine.

### [BLOCKER] Pipeline lifecycle maintenance is pinned to the daemon's CWD project while dispatch is multi-project — executions elsewhere are orphaned forever
- **Where:** `src/gobby/runner_init/orchestration.py:111-119` (`LocalPipelineExecutionManager(db, project_id=runner.project_id)`, gated on `runner.project_id`), heartbeat construction raises and is swallowed when the manager is None (`orchestration.py:196-207`); all lifecycle sweeps filter `project_id = %s` (`storage/pipelines.py:934-958` stall scan; `interrupt_stale_running_executions` `:692-760` via `runner_lifecycle_subsystems.py:395`; approval-timeout expiry `:767-792` via `runner_maintenance.py:467-506`); dispatch enumerates **all** automation-enabled projects (`system_automation.py:497-528` `_dispatchable_project_ids`/`_dispatch_projects`) and the dispatcher inserts executions with the *task's* `project_id` (`dispatch/dispatcher.py:686-707` — `SELECT ..., project_id, 'pending', ... FROM tasks WHERE id = %s`).
- **Failure mode:** An execution in any project other than the daemon's CWD project gets no stall detection, is never marked INTERRUPTED/resumed after a daemon restart (stays RUNNING forever), and its approval gates never time out. If the daemon starts outside a project directory, `runner.project_id` is None → no execution manager → heartbeat init raises and is swallowed → **zero pipeline maintenance daemon-wide** while multi-project dispatch keeps creating executions.
- **Why it matters:** Data-loss class — in-flight state permanently orphaned after restart; the `resume_on_restart` contract is silently void for all but one project. The HTTP approve route already builds per-project executors (`servers/routes/pipelines.py:401-412`), confirming multi-project executions are expected.
- **Minimal fix:** Give the heartbeat and restart/expiry sweeps an unscoped (all-projects) manager or iterate `_dispatchable_project_ids`; don't gate heartbeat construction on `runner.project_id`.
- **Confidence:** high.

### [BLOCKER] Shared mutable default values in `SessionVariableManager` leak session state across sessions
- **Where:** `src/gobby/workflows/state_manager.py:186` (shallow `dict(...)` copy — value objects aliased), `:201-203` (populate path returns the cache dict itself), `:172` (`{**defaults, **session_vars}` layering); mutation vehicle `observer_mcp.py:44-49` (`variables.setdefault("loaded_skills", []) ... loaded.append(name)`); one long-lived manager serves all sessions (`hooks.py:106-110`); `loaded_skills` is a bundled enabled installed default with `value: []` (`install/shared/workflows/variables/gobby-default-variables.yaml:84-85`).
- **Failure mode:** Session A loads a skill; `setdefault` returns the **shared cached list** (the key is always present via layering, and installed defaults are never materialized into the session row), and the append mutates the cache in place. For the rest of the cache generation (10s TTL, refilled on next miss), every other session's `get_variables` returns A's loaded skills as a "default" — skill-gating rules for session B see A's skills as loaded (enforcement bypass). If B persists that key, the union — including A's skills — is written permanently into B's `session_variables` row. The same hazard class applies to every container-valued default (`claimed_tasks`, `verification_evidence`, `unlocked_tools`, `listed_servers`...); `loaded_skills` is the confirmed live instance.
- **Why it matters:** Cross-session state contamination, persisted to DB, that silently weakens enforcement.
- **Minimal fix:** Deep-copy default values on both return paths of `_get_variable_defaults` (or copy container values when layering in `get_variables`).
- **Confidence:** high — all four links verified.

### [BLOCKER] `generate_summary` persists unvalidated LLM output — an empty "success" clobbers a previous good summary
- **Where:** `src/gobby/workflows/summary_actions.py:713` (unconditional `update_summary(session_id, summary_markdown=summary_content)`), `:727` (`{"summary_generated": True}`); `storage/sessions/_field_update.py:267-268` (`COALESCE(%s, summary_markdown)` — `""` is not NULL, so it persists); route checks only `result.get("error")` (`servers/routes/sessions/analytics.py:100-101`).
- **Failure mode:** Session S has a good `summary_markdown` from the validated clear/compact path. `POST /api/sessions/{id}/generate-summary` calls this function; `llm_service.call_feature` succeeds at transport level but yields empty/whitespace text (adapters return `.strip()`-ed output that can legitimately be `""` — `ai/text_generation.py:573,634`). Line 713 writes `""` over the good summary; line 727 reports success with `summary_length: 0`. Downstream treats empty as "no summary": handoff injection skips it, `wait_for_summary` never completes on it. The archived working context is gone at the next clear/compact/resume. The sibling pipeline proves the repo treats this as an expected failure: `sessions/summarize.py:201-215` gates persistence with `is_summary_markdown_valid` plus a deterministic fallback — this entry point skips the guard entirely.
- **Why it matters:** Data loss of the session-continuity artifact, reported as success.
- **Minimal fix:** Gate the write with `is_summary_markdown_valid` (at minimum `summary_content.strip()`); on failure return `{"error": ...}` without calling `update_summary`.
- **Confidence:** high.

### [BLOCKER] One unreadable or schema-invalid pipeline template permanently soft-deletes its DB row — sync still reports `success: True`
- **Where:** `src/gobby/workflows/sync_pipelines.py:158-165` (ValidationError → `continue`, **no error recorded**), `:257-263` (read/parse failure records error but continues), `:168` (`on_disk.add` only after validation succeeds), `:265-279` (unconditional orphan pass soft-deletes every gobby-tagged pipeline row not in `on_disk`), `:196-219` (restore branch requires `definition_json != existing.definition_json` — after a *transient* failure the file is unchanged, so the row is skipped and stays soft-deleted forever), `:124` (`success: True`, never flipped).
- **Failure mode:** Direct inheritance of the `sync_rules.py` wipe (workflows-rules.md Blocker 4), in worse form: a `PipelineDefinition`/`WorkflowDefinition` schema change that invalidates bundled YAML leaves `result["errors"] == []` — the wipe is fully indistinguishable from success. The caller (`cli/installers/shared.py:257-259`) reads only `synced+updated` and never checks `success`/`errors`.
- **Why it matters:** Silent, permanent deletion of bundled pipelines on a routine sync, invisible end-to-end.
- **Minimal fix:** Record validation failures in `result["errors"]`; gate the orphan pass on `if not result["errors"]`; derive "still present on disk" from file existence rather than parse success; restore a soft-deleted managed row whenever its template exists on disk, regardless of content equality.
- **Confidence:** high.

### [IMPORTANT] No concurrency control on execution resume — the same execution can run twice; a reject during resume is silently dropped
- **Where:** `pipeline_executor.py:287-299` (only COMPLETED/CANCELLED rejected at entry; status never rechecked mid-loop); no lease/lock anywhere; racing entry points: HTTP approve (`servers/routes/pipelines.py:387-437`), MCP `resume_pipeline`, CLI.
- **Failure mode:** Two concurrent `approve(token)` calls both pass entry checks and run remaining `exec`/`mcp`/`prompt` steps concurrently (duplicate side effects; the loser hits UniqueViolation and marks FAILED while the winner marks COMPLETED — status flapping). Separately, a `reject(token)` arriving after `execute()` has read status and skipped the approved step is clobbered: the loop never rechecks execution-level CANCELLED and finally overwrites to COMPLETED — the rejection is lost with no error.
- **Minimal fix:** Atomic status CAS at resume entry (`UPDATE ... SET status='running' WHERE id=%s AND status IN (...) RETURNING id`); recheck execution status between steps (abort on CANCELLED). The repo already has the per-task mutex pattern.
- **Confidence:** high (structural).

### [IMPORTANT] Sync DB calls on the asyncio event loop throughout the executor and gatekeeper, despite `run_db` being wired
- **Where:** `pipeline_executor.py:290,308,318,416,456,469,490,521,544,561,565,574,636,824` (direct `execution_manager.*` inside async methods), `:343` (`session_manager.register` — git subprocess + DB); `gatekeeper.py:163-218` (`approve_step`/`reject_step` bypass the `_run_db` helper that `check_approval_gate` uses); `runner_maintenance.py:482-498` (expiry loop sync inside async).
- **Failure mode:** Every step transition blocks the daemon loop on Postgres round-trips; the runner wires `run_db=runner.db_executor.run` and the executor stores it but uses it for none of its own calls (gatekeeper: 1 of 3 methods).
- **Minimal fix:** Route the calls through `self.run_db`/`_run_db`.
- **Confidence:** high.

### [IMPORTANT] `approve()` swallows resume failures and returns a stale pre-resume execution
- **Where:** `pipeline_executor.py:828-830` (`except Exception: logger.error(...)`, fall through to `return execution`); HTTP route returns the stale status as 200.
- **Failure mode:** After `approve_step` succeeds, any resume failure (step error, terminal-status ValueError from token replay) is logged and discarded; the caller gets HTTP 200 `{"status": "waiting_approval"}` while the DB says FAILED. Operators get no signal the pipeline died immediately after their approval.
- **Minimal fix:** Re-fetch the execution after a failed resume (the `ApprovalRequired` branch at `:820-827` already does) or propagate a structured error.
- **Confidence:** high.

### [IMPORTANT] Resume ignores the `definition_json` snapshot — pipeline drift or deletion breaks resumes silently
- **Where:** Snapshot written at `pipeline_executor.py:301-313`; resume loads by name (`:804` `load_pipeline(execution.pipeline_name)`); silent no-op when the pipeline no longer loads (`:805` `if pipeline:` with no else).
- **Failure mode:** Pipeline deleted/renamed between pause and approve → approve marks the step COMPLETED but never resumes; the execution is stuck WAITING_APPROVAL forever and the caller gets a 200. Definition edited between pause and approve → resume executes the new step list against old step rows (renamed step_ids re-run unapproved work under a definition the operator never saw).
- **Minimal fix:** Reconstruct the definition from `execution.definition_json` on resume, falling back to the loader; treat load failure on resume as an error.
- **Confidence:** high on behavior; med on frequency.

### [IMPORTANT] `activate_workflow` steps fail silently — error dict marked COMPLETED
- **Where:** `pipeline_executor.py:730-735` (returns `{"error": "...not supported", ...}`), passes the `exit_code`-only check at `:514`, marked COMPLETED at `:544`.
- **Failure mode:** `activate_workflow` is schema-valid (`definitions.py:588-596`) but a runtime no-op recorded as success. Authors get no signal.
- **Minimal fix:** Reject at definition load time, or raise instead of returning an error dict.
- **Confidence:** high.

### [IMPORTANT] Pipeline webhooks are dead code — `WebhookNotifier` is never constructed; `notify_complete`/`notify_failure` have no call sites
- **Where:** `pipeline_webhooks.py:25-206` (whole class); no production construction site (`orchestration.py:120-130`, `app_context.py:209`, `cli/pipelines.py:67`, `hooks/factory.py:508` — zero `WebhookNotifier(` instantiations outside tests); `notify_complete`/`notify_failure` uncalled anywhere even if wired.
- **Failure mode:** `PipelineDefinition.webhooks` is a supported, documented schema field (`docs/guides/pipelines.md:95`); users who configure `on_approval_pending` get no notification — the pipeline sits in WAITING_APPROVAL until timeout. Silent contract violation.
- **Minimal fix:** Construct and pass the notifier in `init_orchestration`/`app_context`; invoke complete/failure notifiers at terminal transitions — or delete the feature from schema and docs.
- **Confidence:** high.

### [IMPORTANT] Heartbeat scan→write TOCTOU clobbers WAITING_APPROVAL / COMPLETED transitions
- **Where:** `pipeline_heartbeat.py:179-197` with `storage/pipelines.py:105-156` (blind UPDATE keyed on id).
- **Failure mode:** Between the stall scan and per-execution handling, the executor can transition to WAITING_APPROVAL or COMPLETED; the heartbeat then unconditionally writes RUNNING (touch path — erases WAITING_APPROVAL, desyncing execution status from the step row) or FAILED (overwrites COMPLETED, clobbering real outputs). A WAITING_APPROVAL execution flipped back to RUNNING becomes eligible for the next scan's FAILED branch — killing a gate a human was about to approve.
- **Minimal fix:** Conditional writes: `UPDATE ... WHERE id=%s AND status='running' AND updated_at < %s`.
- **Confidence:** high on mechanism; med on frequency.

### [IMPORTANT] `_has_alive_agents` fails closed — a transient DB error converts a slow pipeline into FAILED
- **Where:** `pipeline_heartbeat.py:214-216` (`except Exception: return False`); caller treats False as "truly dead" and marks FAILED.
- **Failure mode:** One query hiccup on `agent_runs` during a sweep destroys a live execution's status. The sibling `_is_session_alive` deliberately returns True on error ("err on side of caution") — the destructive branch is the one that requires positive evidence.
- **Minimal fix:** Return True on exception, or re-raise so the per-execution handler skips the item.
- **Confidence:** high.

### [IMPORTANT] Legacy `gobby:pipeline-heartbeat` cron job is neither retired nor handled — upgraded installs error every tick
- **Where:** `runner_init/orchestration.py:18` (`RETIRED_SYSTEM_CRON_JOBS` lists only `gobby:conductor-tick`); the handler is no longer registered anywhere; `scheduler/executor.py:301-305` raises on a missing handler; the name persists in `storage/cron.py:25` priorities. `PipelineHeartbeat.__call__`/`PipelineHeartbeatResult` (`pipeline_heartbeat.py:133-144`) describe a cron path production no longer uses (SystemAutomationLoop calls the methods directly).
- **Minimal fix:** Add `"gobby:pipeline-heartbeat"` to `RETIRED_SYSTEM_CRON_JOBS`; delete or re-justify `__call__` and fix the module docstring.
- **Confidence:** med-high (legacy auto-creation inferred from the priority-map remnant; no live upgraded DB inspected).

### [IMPORTANT] Stale-task candidate scan truncated at 100 rows with no ordering
- **Where:** `pipeline_heartbeat.py:339-346` (`limit=100`, no `order_by`).
- **Failure mode:** On a busy hub with >100 active-stage tasks, whether a stale claim is ever examined depends on the storage default sort; orphaned claims past the cap sit unrecovered indefinitely while the heartbeat reports clean ticks.
- **Minimal fix:** Paginate, or order by `updated_at ASC` so the stalest claims are always in the window.
- **Confidence:** med (truncation verified; impact depends on default ordering).

### [IMPORTANT] `check_stale_tasks` mutates stage state and releases claims without the task dispatch mutex
- **Where:** `pipeline_heartbeat.py:268-303` (stage transition + claim release as two non-atomic `_run_db` calls, no mutex), vs the dispatch contract (mutex before side effects).
- **Failure mode:** A dispatch action and a heartbeat recovery can interleave on the same task — heartbeat releases a claim between dispatch's decision and its agent-run creation, double-advancing stage state.
- **Minimal fix:** Acquire (or check) the task's dispatch mutex before recovery writes; perform submit+release in one transaction.
- **Confidence:** med.

### [IMPORTANT] PENDING executions have no recovery path — dispatcher-created rows leak if the daemon dies before the executor starts
- **Where:** `dispatch/dispatcher.py:686-707` (inserts `status='pending'`); stall scan and restart interruption both filter `status='running'` only (`storage/pipelines.py:934-958`, `:692-760`).
- **Failure mode:** A row created `pending` whose executor never ran stays `pending` forever, with the task's dispatch mutex `run_id` pointing at it — dispatch lifecycle for that task can wedge on a phantom run.
- **Minimal fix:** Include PENDING rows older than a threshold in the interruption/heartbeat sweeps.
- **Confidence:** med-high.

### [IMPORTANT] Pipeline renderer shares the unsandboxed Jinja2 engine flagged in the rules layer
- **Where:** `pipeline/renderer.py:175-184` (`render_string`) backed by `templates.py:69-80` (`jinja2.Environment`, not `SandboxedEnvironment`).
- **Failure mode:** Definition-authored template strings can reach arbitrary Python via attribute traversal. Mitigating: pipeline definitions already grant code execution via `exec` steps, so this is not a new privilege boundary here — but the engine is shared with the rules layer (where it *is* one), and any future caller rendering attacker-influenced template strings inherits RCE. Runtime data is substituted as values, not re-rendered (no second-order SSTI on the normal path).
- **Minimal fix:** Move `TemplateEngine` to `SandboxedEnvironment`, coordinated with the rules-layer fix.
- **Confidence:** high (fact); med (severity given threat model).

### [IMPORTANT] Undefined variables render to empty string in `exec`/`prompt` strings (fail-open), while MCP args fail closed
- **Where:** `pipeline/renderer.py:175-184` (default `Undefined`, `templates.py:69-80`) vs `:142-160`/`:216-218` (pure-expression path raises `Unknown variable`).
- **Failure mode:** `${{ inputs.missing }}` in an exec string silently becomes `""` — `rm -rf ${{ inputs.dir }}/x` becomes `rm -rf /x` — while the same typo in an MCP argument fails the step. Same mistake, opposite outcomes, on a code-executing surface.
- **Minimal fix:** `StrictUndefined` (or a logging undefined) for pipeline rendering.
- **Confidence:** high.

### [IMPORTANT] `workflow_instances` read-modify-write has no DB-level serialization — MCP tool path races hook path
- **Where:** `state_manager.py:39-75` (`save_instance` full-row upsert, no lock primitive); unlocked writers: `mcp_proxy/tools/workflows/_variables.py:136-144` (get → mutate → save during tool execution), `apply_persona.py:101-112`, `session_activation.py:585-604`; hook-side writers serialized only by the in-process per-session eval lock.
- **Failure mode:** With parallel tool calls (routine), tool A's instance write overlaps step processing for tool B: both load the same snapshot, both upsert; the last writer silently reverts the other's `instance.variables` or rolls `current_step` back across a transition — a step regression the engine won't re-attempt. `session_variables` mutations in the same file get `SessionVariableMutation` advisory locks; instances get nothing.
- **Minimal fix:** Add a `WorkflowInstanceMutation` lock target and route instance RMW through `transaction_immediate`, mirroring the session-variables pattern.
- **Confidence:** high on mechanism; med on frequency.

### [IMPORTANT] `session_variables` rows are never deleted — no FK cascade, `delete_variables` is dead code
- **Where:** `state_manager.py:369-374` (zero callers); `storage/postgres_baseline_schema.sql:667-671` (no `REFERENCES sessions`, contrast `workflow_instances` `ON DELETE CASCADE`).
- **Failure mode:** Sessions are genuinely deleted (ghost-session pruning); their variable rows are orphaned forever — unbounded growth, and stale state resurfaces if a session id is reused.
- **Minimal fix:** FK with `ON DELETE CASCADE` via migration, or wire `delete_variables` into delete/prune paths.
- **Confidence:** high.

### [IMPORTANT] `compact_continuation` mutates session variables outside the `SessionVariableMutation` advisory lock
- **Where:** `sessions/compact_continuation.py:342`, `:427-428` (whole-dict RMW under `transaction_immediate()` with no lock target) vs the contract at `state_manager.py:224,262,306,346`.
- **Failure mode:** Advisory locks only protect writers who acquire them; a compaction write interleaving with `merge_variables`/`append_to_set_variable` is a classic lost update — one side's whole-dict write erases the other's keys.
- **Minimal fix:** Pass `SessionVariableMutation(session_id=...)` in both helpers, or route through `SessionVariableManager`.
- **Confidence:** high on mechanism; med on overlap frequency.

### [IMPORTANT] `_get_variable_defaults` catches the wrong exceptions — one malformed variable row degrades hooks fleet-wide
- **Where:** `state_manager.py:198` (`except (json.JSONDecodeError, KeyError)`).
- **Failure mode:** The real malformed-payload failures are `TypeError` (`json.loads(None)`) and `AttributeError` (valid JSON scalar → `body.get`); either propagates out of `get_variables` into `_evaluate_rules`, which blocks STOP events and strips variables for other events — for every session, until the row is fixed. The sibling loader in hooks.py catches `AttributeError`, showing the mode is a known real shape.
- **Minimal fix:** `except (json.JSONDecodeError, TypeError, AttributeError)`.
- **Confidence:** high on code; med on trigger likelihood.

### [IMPORTANT] Variable mutation paths ignore the default layering that `get_variables` promises
- **Where:** `state_manager.py:243-334,336-367` (`append_to_set_variable*`, `claim_startup_context` read the raw row, not the layered view) vs the reader at `:147-172`.
- **Failure mode:** Latent today (current append targets have no defaults), but any future append to a variable seeded by a non-empty default (e.g. `listed_servers`, 17 entries) persists only the appended values; the layered read then shadows the seed with the truncated list, corrupting progressive-discovery state.
- **Minimal fix:** Seed `stored` from layered defaults when the row lacks the key, or document the raw-row contract on each mutator.
- **Confidence:** high (latent).

### [IMPORTANT] dry-run never validates condition expressions — the most load-bearing strings in a step workflow are invisible
- **Where:** `dry_run.py:226-372` (`_check_structure` validates only `transition.to`); real semantics: transition `when` fails **open** (broken → never transitions → agent stalls forever), `exit_condition` fails **closed** (broken → instantly completes the workflow), and the transition context exposes only `vars` (`enforcement.py:861`) so `variables.x`/`tool_input.y` raise and never fire. The module's own fixture encodes the dead pattern (`tests/workflows/test_dry_run.py:441` uses `variables.session_task` in a transition — runtime-broken, dry-run-blessed).
- **Failure mode:** "Predicts allow when real engine breaks" — the two real failure modes of step workflows (stalled agents, instantly-exiting workflows) get zero validation while the dry run reports `valid: true`.
- **Minimal fix:** `ast.parse` each condition via `SafeExpressionEvaluator._normalize_expr`; warn when top-level names aren't in that condition class's runtime context.
- **Confidence:** high.

### [IMPORTANT] dry-run skips `on_mcp_before` — the only handler list that can block tools at runtime
- **Where:** `dry_run.py:508` (loop covers `on_mcp_success`/`on_mcp_error` only); runtime `enforcement.py:549-606` executes `on_mcp_before` with exact matching and `action: block`.
- **Failure mode:** A typo'd server/tool in a *blocking guard* silently never matches → the guard never blocks → no dry-run finding. The checked/unchecked asymmetry indicates oversight.
- **Minimal fix:** Add `*step.on_mcp_before` to the loop.
- **Confidence:** high.

### [IMPORTANT] dry-run wildcard and colon-less ref handling diverges from runtime matching in both directions
- **Where:** `dry_run.py:525` (`if ":" not in ref: continue`), `:580` (`tool != "*"` exemption); runtime `enforcement.py:432-440,550,801`.
- **Failure mode:** (a) A colon-less `allowed_mcp_tools` entry can never match a runtime `server:tool` key — every MCP tool in that step is blocked at runtime; dry-run silently skips the ref. (b) `tool: "*"` in handler refs is exempted as a valid wildcard, but handler matching is exact equality — the handler never fires and dry-run suppresses exactly the warning that would catch it.
- **Minimal fix:** Warn on colon-less refs; drop the `"*"` exemption for handler refs.
- **Confidence:** high.

### [IMPORTANT] dry-run emits false `UNKNOWN_MCP_TOOL` for configured-but-not-connected servers
- **Where:** `dry_run.py:438-446,541,580`; seam: `server_registry.py:109-112` (all configured) vs `tool_inventory.py:58-65` (active connections only, failures → `[]`).
- **Failure mode:** Any configured-but-unconnected server yields an empty tool set, so every valid ref on it is flagged unknown while runtime allows it — trains users to ignore warnings.
- **Minimal fix:** Skip/downgrade tool-level checks when a server has no inventory.
- **Confidence:** high.

### [IMPORTANT] Production `evaluate_spawn` never passes `workflow_loader` — workflow validation silently dead in the spawn dry-run
- **Where:** `mcp_proxy/tools/agents.py:912-930` (the only production caller omits `workflow_loader`); gates `agents/dry_run.py:175,307` skip silently when None; every test injects a mock loader.
- **Failure mode:** The spawn dry-run returns `can_spawn: true` without ever checking the workflow it reports as resolved; no info item says the layer was skipped. Secondary: the call passes the external-only `_mcp_manager`, so if a loader were wired, all internal `gobby-*` refs would be falsely flagged.
- **Minimal fix:** Pass the runner's `WorkflowLoader` plus a combined inventory; emit a warning item when `workflow_loader is None`; add a wiring test at the MCP tool surface.
- **Confidence:** high.

### [IMPORTANT] dry-run trace and checks describe `on_enter`/`on_exit`/`on_transition` actions no runtime code executes
- **Where:** `dry_run.py:478-505,328-345,592-645`; exhaustive search shows the only reader of step `action.get("type")` is dry_run itself; activation paths only set `current_step`; the comment at `agents/spawn_executor.py:246` claiming prompt delivery "via on_enter" is stale.
- **Failure mode:** `gobby workflows check` tells users `call_mcp_tool: server:tool` runs on step entry; reality ignores it entirely — the inverse divergence direction, actively misleading during debugging. Two of the validator's richest checks validate this dead config while the live config (conditions, `on_mcp_before`) gets none.
- **Minimal fix:** Warn that these fields are not executed by the current engine (or annotate the trace), and reconcile with engine owners on which side is wrong.
- **Confidence:** high on runtime facts; med on intended design.

### [IMPORTANT] dry-run resolves a different definition than the runtime enforcer reads
- **Where:** dry-run: `dry_run.py:154` → `WorkflowLoader.load_workflow` (extends-merged, project-scoped); runtime: `enforcement.py:139-146` (`get_by_name` — no project_id, raw `definition_json`, no extends resolution).
- **Failure mode:** Latent (no bundled workflow uses `extends`; live step workflows are agent-inline rows), but any future `extends` or project-scoped override splits simulation from reality with no error anywhere.
- **Minimal fix:** Load through the same path in `_get_step_for_session`, or have dry-run warn on `extends`/shadowing.
- **Confidence:** med (mechanics verified; impact latent).

### [IMPORTANT] `generate_summary` runs sync DB and git subprocess I/O on the event loop — ~25s worst case on a user-facing route
- **Where:** `summary_actions.py:609,713` (sync psycopg), `:655-675` (five sync `subprocess.run` git calls, 5s timeout each — `git_utils.py:28-89`), `:254-257`; also `:560` (`update_title` sync, awaited from `runner_maintenance.py:336`).
- **Failure mode:** Awaited directly in the FastAPI handler; the git calls alone can block the daemon loop ~25s, stalling every hook/WebSocket/MCP request. The sibling path threads all of this.
- **Minimal fix:** `asyncio.to_thread`/`run_db` for DB and git helpers.
- **Confidence:** high.

### [IMPORTANT] `generate_summary` captures git context from the daemon's CWD, not the session's project
- **Where:** `summary_actions.py:655-675`; `git_utils.py:28-33,49-54,76-89` (no `cwd`); `get_git_diff_summary` has a `project_path` param the caller doesn't pass.
- **Failure mode:** A daemonized process (cwd `/` or launch dir) bakes another repo's status/commits/diff — or "Not a git repository" — into the stored summary; the LLM anchors the handoff on false facts. (Flagged from the rules surface in workflows-rules.md; this module is the bug site.)
- **Minimal fix:** Thread the session's project path through and pass `cwd=` to all four helpers.
- **Confidence:** high.

### [IMPORTANT] `generate_summary` transcript reader: one torn JSONL line aborts; ACP/Qwen `.json` transcripts unsupported; unbounded memory
- **Where:** `summary_actions.py:632-648` (strict per-line `json.loads`, whole file accumulated before the 100-turn cut).
- **Failure mode:** (1) A partially-written final line (live-appended transcripts) aborts the whole generation with HTTP 422 — the sibling `_read_transcript` skips malformed lines (`summarize.py:288-295`). (2) Single-JSON `.json` transcripts (dispatched explicitly elsewhere, `lifecycle.py:551-563`) fail line 1 or yield the wrong shape — on-demand summary broken for that CLI family. (3) Hundreds of MB held in memory before truncation.
- **Minimal fix:** Reuse `_read_transcript` (suffix dispatch + per-line tolerance); cap retained turns while streaming.
- **Confidence:** high (modes 1-2); med (mode 3 frequency).

### [IMPORTANT] No size bound on `generate_summary` prompt assembly — long sessions deterministically fail
- **Where:** `summary_actions.py:645,662,686-696` (turns, digest, context all unbounded; `format_turns_for_llm` truncates only tool results).
- **Failure mode:** Large pasted content or a multi-hundred-KB digest exceeds model context → LLM error → no summary for exactly the long, high-value sessions that most need a handoff. The sibling path caps everything at 24,000 chars.
- **Minimal fix:** Apply the summarize.py char caps to all three inputs.
- **Confidence:** high.

### [IMPORTANT] Empty/partially-installed bundled pipelines directory wipes ALL pipeline rows
- **Where:** `sync_pipelines.py:131-134` (guard checks only that the dir exists) + `:265-279` (unconditional orphan pass).
- **Failure mode:** An existing-but-empty `pipelines/` dir (botched install, packaging regression) yields `on_disk = {}` → every gobby-tagged pipeline row soft-deleted, `success: True`, `errors: []`. A retirement test relies on this exact behavior, codifying it.
- **Minimal fix:** Treat zero discovered template files as a no-op for orphan cleanup.
- **Confidence:** high.

### [IMPORTANT] `WebhookExecutor` (currently unwired) lacks SSRF protection, response size limits, and retry bounds
- **Where:** `webhook_executor.py:351-402` (`_make_request`: no scheme/host validation, redirects followed — a target can 30x to `http://169.254.169.254/`; `:387` `await response.text()` unbounded); `:258-274` (`_parse_retry_config` skips `webhook.py:43-44`'s 1-10 clamp — `max_attempts: 100000` yields a retry storm with uncapped exponential backoff). Two divergent `RetryConfig` parsers exist; the executor never consumes `webhook.py`'s validated model.
- **Failure mode:** Latent — repo-wide search shows `WebhookExecutor` referenced only by its own tests; the live surface is `pipeline_webhooks.WebhookNotifier` (httpx, redirects off). Becomes a Blocker the moment it's wired to any semi-trusted URL.
- **Minimal fix:** Validate scheme/host (reject private/loopback/link-local/metadata after DNS resolution), `allow_redirects=False`, cap body reads, parse retry config through `webhook.py.RetryConfig.from_dict` with a max-backoff cap — or delete the dead module.
- **Confidence:** high (code); med (reachability).

### [IMPORTANT] Cross-seam: reactions-driven pipeline approval calls `approve_step`/`reject_step` on a class that doesn't have them
- **Where:** `src/gobby/communications/reactions.py:117-128` — `pipelines = self._services.pipeline_execution_manager; await pipelines.approve_step(pipeline_run_id, step_id, session_id_str)`. `approve_step`/`reject_step` exist only on `ApprovalManager` (`gatekeeper.py:145,181`) with signature `(token, approved_by)` — not on `LocalPipelineExecutionManager`, and not with that arg shape.
- **Failure mode:** The call raises `AttributeError` at runtime, swallowed by the enclosing `except Exception` (`:130-131`) — an emoji-reaction approval silently no-ops with only a log line. Even if pointed at `ApprovalManager`, the arguments are wrong (run id + step id instead of token).
- **Why it matters:** The approval contract this area owns is broken at the consuming seam. Belongs to the communications leaf (#15787) for ownership, recorded here because the seam is this review's contract.
- **Minimal fix:** Route reactions through `PipelineExecutor.approve(token)`/`.reject(token)` with the step's stored approval token.
- **Confidence:** high — verified both the call site and the class surfaces.

### [IMPORTANT] Load-bearing pipeline contracts are pinned only by mocks — the suite encodes several Blockers as expectations
- **Where:** `tests/workflows/test_pipeline_resume.py` (all `MagicMock` managers; `test_failed_execution_reexecutes_all_steps:199-244` pins behavior that crashes on the real UNIQUE constraint; `test_approve_resumes_execution_and_runs_next_step:61-134` hand-fakes the approved step's output and never asserts the gated action ran); `test_pipeline_executor_approvals.py` (no replay/double-spend/RUNNING-resume coverage); `test_pipeline_heartbeat.py:98-111` (agents seeded under the wrong session — never the production topology); webhook tests are notifier-only (no wiring test); no sync_pipelines test for a malformed-but-present template sparing the row.
- **Failure mode:** Blockers 1, 4, and 5 pass the existing suite because mocks encode the bugs as expectations.
- **Minimal fix:** Executor↔storage integration tests (isolated test DB) for: approve→gated action runs; FAILED resume against existing rows; token replay/double-spend; nested `ApprovalRequired` propagation; heartbeat with child-session agent topology and NULL session; sync parse-failure sparing rows.
- **Confidence:** high.

### [NIT] Pipeline child session leaks on reject and approval-timeout
- **Where:** `pipeline_executor.py:599-601,660` (close only on completed/failed paths); `reject()` (`:836-842`) and the expiry loop never close the `pipeline-{execution_id}` session created at `:343`.
- **Note:** Violates the method's own "should not linger" contract; close in `reject()` and the timeout loop.

### [NIT] `_emit_event` catch-list lets callback bugs fail the pipeline
- **Where:** `pipeline_executor.py:144` — catches only `(ValueError, RuntimeError, OSError)`; a `TypeError`/`KeyError` from a broadcast callback marks the execution FAILED.

### [NIT] exec-step timeout configuration is dead code
- **Where:** `pipeline/handlers.py:121` reads `context["timeout_seconds"]`; the executor's context (`pipeline_executor.py:399-409`) never sets it — the documented configurable timeout is always 300s.

### [NIT] `PipelineEventCallback = Any`; `execute()` spans 445 lines; file at 969 lines
- **Where:** `pipeline_executor.py:40-41,220-664`. The interleaved resume branches and trailing safety net are where two Blockers hid; extract `_resume_state()` and `_run_single_step()`. One feature away from the monolith cap.

### [NIT] `INTERRUPTED` is documented non-terminal but stamps `completed_at`; the two interrupt paths disagree
- **Where:** `pipeline_state.py:36-37` vs `storage/pipelines.py:122-131` (stamps) vs `:744-758` (bulk interrupt doesn't). Resume preserves the stale `completed_at` via COALESCE — a "completed" timestamp on a running execution.

### [NIT] Webhook notifier maintainability
- **Where:** `pipeline_webhooks.py:160-173` (only POST/PUT; other methods silently no-op), `:196-202` (regex recompiled per call), `:32-39` (hardcoded `http://localhost:7778` default). Also `_expand_env_vars` (`:186-204`) expands arbitrary `${VAR}` from `os.environ` into headers — an env-exfiltration vector if a definition author controls header + URL; revisit when the notifier is actually wired.

### [NIT] Heartbeat small items
- **Where:** `pipeline_heartbeat.py:128` (function-local `import asyncio`); `:136-138,330-332` (stale-task candidates scanned twice per tick); `:179-189` (touch path counted as "handled", inflating `stalled_handled`).

### [NIT] `_filter_env` blocklist is incomplete
- **Where:** `pipeline/renderer.py:25-64` — `endswith` suffix matching misses vars literally named `PASSWORD`/`TOKEN`/`SECRET` and names like `SECRET_STUFF`/`GH_PAT`. False sense of protection; prefer the `allowed_env_keys` whitelist path.

### [NIT] `_coerce_value` mangles numeric-looking strings; CLI approvals record no actor
- **Where:** `renderer.py:186-215` (`"007"` → `7`, `"1e3"` → `1000.0`); `cli/pipelines.py:462,500` pass `approved_by=None`/`rejected_by=None` — no audit identity despite the column existing.

### [NIT] state_manager dead code: `delete_instance`, `set_enabled`, `delete_variables`
- **Where:** `state_manager.py:77-82,93-100,369-374` — zero production callers each; `set_enabled` silently no-ops on a missing row (codified by test).

### [NIT] `save_instance` timestamp/id fidelity
- **Where:** `state_manager.py:41,59-74` — `created_at` overwritten with `now` on every save; in-memory `updated_at` never refreshed; `ON CONFLICT` keeps the DB row's `id`, so a create-race loser holds a mismatched id.

### [NIT] Stale "BEGIN IMMEDIATE" docstrings
- **Where:** `state_manager.py:212,246` — backend is Postgres advisory locks, not SQLite.

### [NIT] `get_active_instances` has no stable ordering tiebreaker
- **Where:** `state_manager.py:30-37` (`ORDER BY priority ASC` only; both creation sites use priority=10) — a session with two step workflows gets nondeterministic first-match selection in enforcement.

### [NIT] Inconsistent JSONB row-shape defensiveness
- **Where:** `state_manager.py:158-166` (reader handles dict|str|bytes) vs `:225,262,306,346` and `_row_to_instance:121` (writers assume str). Correct today via the adapter's normalization; an adapter change crashes writers while the reader survives.

### [NIT] `append_to_set_variable` edge handling
- **Where:** `state_manager.py:264-269` — falsy scalar stored value silently discarded; `sorted(set(...))` raises on mixed-type/unhashable elements.

### [NIT] `constants.py` is dead weight with vacuous tests
- **Where:** `constants.py:9-12` — `PIPELINE_TEST_1..4` (retired conductor naming) have zero production users; the only consumer asserts the literals equal themselves; the docstring describes contents that don't exist.

### [NIT] dry-run `workflow_type` values contradict the dataclass contract and its tests
- **Where:** `dry_run.py:91` (comment: `"step"|"lifecycle"|"pipeline"`) vs `:192` (assigns `"enabled"`/`"on-demand"`); `tests/workflows/test_dry_run.py:529` still constructs `workflow_type="step"`.

### [NIT] Existing-but-malformed workflows reported as `WORKFLOW_NOT_FOUND`
- **Where:** `dry_run.py:167-176`; loader collapses parse failures to `None` (`loader.py:146-148`) — the one case where a precise diagnosis matters most says "not found".

### [NIT] Lifecycle-path and dead-end heuristics misdescribe runtime semantics
- **Where:** `dry_run.py:648-667` (follows `transitions[0]` unconditionally, appends undefined targets), `:291-312` (DEAD_END suppressed by substring match of step names inside `exit_condition`; only the literal last step treated as legitimate terminal).

### [NIT] `generate_summary` steps 2-3 sit outside any try
- **Where:** `summary_actions.py:651-680` — a parser exception surfaces as a generic 500 instead of the documented error dict.

### [NIT] `_write_summary_file` mode suffix drift
- **Where:** `summary_actions.py:236-245` (docstring promises `-full`/`-compact`) vs `:718-723` (`mode="clear"` → `{ref}-clear.md`). No code reads these files back.

### [NIT] Fire-and-forget `create_task` without a strong reference
- **Where:** `summary_actions.py:308` (tmux rename — cosmetic) and, more load-bearing at the seam, `hooks/session_summary_dispatcher.py:66` (the clear/compact summary task itself); asyncio holds only weak refs.

### [NIT] New `aiohttp.ClientSession` per retry attempt
- **Where:** `webhook_executor.py:369` — closed correctly, but pooling defeated; reuse across attempts.

### [NIT] `src/gobby/workflows/CLAUDE.md` drift extends to this area
- **Where:** `workflows/CLAUDE.md` — references `rule_engine.py` (doesn't exist; `engine/core.py`), a `StateManager` class (actual: `WorkflowInstanceManager`/`SessionVariableManager`), `sync.py` (actual: `sync_rules.py`/`sync_pipelines.py`/`sync_variables.py`), and lists `activate_workflow` among handled step types (runtime returns an error dict — see Important above). Feed to docs-accuracy leaves #15799–#15801.

## Systemic patterns

1. **Every lifecycle state transition is an unguarded last-writer-wins UPDATE.** `update_execution_status` and `update_step_execution` have no preconditions, no CAS, no terminal-state guards; there is no per-execution mutex. The executor, heartbeat, gatekeeper, expiry loop, and approval replay all race through the same blind writes. Five of the nine Blockers are manifestations. The repo already owns the cure (`task_dispatch_mutex` + guarded transitions); pipelines never adopted it.
2. **Token lifecycle is never closed.** `approval_token`/`resume_token` are written and never cleared (sole exception: the MCP tool's `reset_steps_from`). Approve/reject/timeout all leave live credentials behind.
3. **Broad `except Exception` converts failures into successes at seam boundaries**: nested pipeline (`pipeline_executor.py:922`), approve-resume (`:828`), reactions approval (`reactions.py:130`), heartbeat init (`orchestration.py:205-207`). The rules-layer review found fail-open at the hook boundary; the execution layer's variant is fail-silent-success.
4. **Sync DB/subprocess I/O on the event loop despite the `run_db` bridge being constructed and wired**: executor uses it for 0 of ~15 call sites, gatekeeper 1 of 3, summary_actions none, expiry loop none. The pattern recurs at every seam this review touched.
5. **Two divergent implementations of the same concern, one hardened and one not**: summary generation (validated summarize.py vs unguarded summary_actions), transcript reading (tolerant vs strict), retry config (clamped vs unclamped), webhook stacks (wired httpx notifier vs unwired aiohttp executor), dry-run semantics vs runtime engine. Drift, not design — consolidation fixes findings as a class.
6. **Mock-only tests encode bugs as expectations.** The approval/resume/heartbeat suites mock the exact storage semantics (UNIQUE constraints, token lifecycle, session topology) the bugs live in.
7. **Single-project managers injected into daemon-global services** (heartbeat, restart recovery, approval expiry) while dispatch is explicitly multi-project.
8. **Shared mutable cached state handed to mutating consumers** — one confirmed cross-session leak (`loaded_skills`); every container-valued default is the same hazard.

## Verified non-bugs (cleared — don't re-chase)

- **Token generation is sound**: `secrets.token_urlsafe(24)` = 192 bits, DB-unique; forgery infeasible — the problem is replay, not entropy.
- **No shell injection in exec steps**: `shlex.split` + `create_subprocess_exec` (no shell), timeout with kill (`handlers.py:114-161`).
- **Condition evaluation is fail-closed in the renderer**: broken conditions skip the step, never run it (`renderer.py:316-328`); `render_step` deep-copies (`model_copy(deep=True)`).
- **dry-run executes no side effects** — the cardinal sin is absent: loader read + pure inventory listing only.
- **`update_summary(None)` cannot null a summary** (COALESCE); `wait_for_summary` is deadline-bounded.
- **Inbound webhook approval is bearer-token auth, not unauthenticated advancement** (192-bit token by exact match); minor gap: `approved_by=None` on the HTTP route.
- **sync_pipelines is immune to the rules-layer sibling bugs #2/#3** (no caller-supplied directory/tag; both roots gathered in one pass).
- **Approval timeouts are enforced** (for the daemon's project) by `expire_approval_timeouts_loop`; the bug is post-expiry token replay, not missing expiry.
- **MCP `resume_pipeline` tool path is correct** (resets steps to PENDING, clears tokens, inside a transaction).
- **Cross-pipeline cycle detection works** (A→B→A blocked, depth-bounded); the gap is only that nested errors are swallowed (Blocker above).
- **No heartbeat double-fire from the automation loop itself** (ticks serialized under `_tick_lock`).
- **Hook-vs-hook instance mutation is serialized** in-process by the per-session eval lock; only cross-path (MCP tool) writers race.
- **JSON/datetime round-trip fidelity holds** through the Postgres adapter's normalization at every fetch path.
- **`%s` placeholders are correct** per repo convention (CLAUDE.md's `$N` mandate is stale doc drift).
