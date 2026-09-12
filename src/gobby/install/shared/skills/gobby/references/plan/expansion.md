# Expanding approved plans
Load before expansion, task-tree QA, routing selection, cancellation or recovery.

## Discover and prerequisites
Use gobby-tasks-ops:get_latest_expansion_run/get_expansion_run and gobby-plans:get_plan. A typed plan must have approved, server-derived M1 and pass expansion-mode validation. Resolve a real target root and register the plan through [lifecycle](lifecycle.md); expansion may resolve a descendant request to its registered ancestor root. Conflicting registered paths require diagnosis, not an automatic reset.

## Run
Prefer installed expand-task pipeline via gobby-workflows:run_pipeline with task_id and optional plan_file. Inspect its installed definition before relying on defaults: bundled provider/model/wait/coverage settings may be overridden. The returned execution_id is asynchronous; retain it and yield for the durable completion notification, then inspect get_pipeline_status.
Direct start_expansion_run supports compile/apply and caller subscription. Default auto_apply is true; inspect schema before overriding. Durable expansion state lives on expansion_runs. The pipeline owns its internal wait and uses subscribe_caller false to avoid duplicate caller subscriptions.
Compile preserves one source section/manifest entry/leaf. Multi-phase roots gain phase sub-epics titled P<N>: <title>. Apply wires dependencies, stage policy, affected files and provenance, retaining parent automation/isolation settings. Dev-only expansion can finish without children.

## Routing and coverage
Typed code domain deterministically selects backend/frontend/fullstack developer unless an explicit privileged override exists. Docs route to tech-writer; inspect agent definitions for other supported categories and available routes. Ad-hoc selection uses concrete scope/files over weak label hints; prefer a matching specialist. If no confident available route exists, use backend-developer and record an Agent Selection rationale. Do not split typed source sections during expansion.
Validate compiled and applied output with validate_expansion_run. run_expansion_qa_coverage checks the DB tree against the contract; save_expansion_qa_result/check_expansion_qa_result handle stored QA outcomes. A completed run with failed coverage is unfinished. The installed expand-task pipeline runs coverage for every contract plan.
Use wire_affected_files_from_run when repairing missing scope annotations. validate_plan_file is a draft-file check, not expansion approval.
For placeholder deferrals, read checkpoints.deferral_task_map, replace matching refs in the canonical plan, validate, call gobby-plans:update_plan_hash with the registered plan_id, rerun run_expansion_qa_coverage to refresh coverage/task artifact pointers, and commit. Keep needs-planning hold/provenance and dependencies; a closed prerequisite does not authorize automatic execution of deferred work.

## Recovery and constraints
start reuses active runs by default. resume_expansion_run restarts interrupted/failed work; cancel_expansion_run cancels active work. Inspect status/error/checkpoints before choosing. reset_expansion_output and reset_output delete generated output; use only with explicit scope and established need, after checking descendants/ownership. force_new is not a routine retry.
Plan path conflicts and checkout-resolution errors must be resolved against the owning project/root. Never duplicate a tree to escape them, use removed save/execute-spec tools, or bypass task lifecycle with SQL/CLI mutations.
MCP is the agent surface. Task-expansion CLI commands are operator-only. Build profiles/stages are governed by build guidance.

See [Task expansion](../../../../../../../../docs/guides/task-expansion.md#expansion-runs).

_Last verified: 2026-09-12_
