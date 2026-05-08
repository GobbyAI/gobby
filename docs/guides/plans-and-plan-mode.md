# Plans And Plan Mode

Gobby has two related plan systems: chat plan mode for approval-gated planning,
and DB-backed plan records for durable implementation or strategy artifacts.

## Mental Model

Plan mode is a live session state. It lets an agent write or update the current
plan artifact while blocking unrelated file edits until the user approves or
exits plan mode. The Web UI exposes this through plan approval controls.

Plan records are durable database rows. They register a plan file, hash, kind,
state, root task reference, and generated coverage manifest. They let Gobby
validate that a plan maps to the tasks and files it is supposed to cover.

Use plan mode to produce and approve a plan. Use plan records to track a plan
artifact through validation, archival, review, and deletion.

## Quick Start

Validate registered plans:

```bash
uv run gobby plans validate
```

List plan records:

```bash
uv run gobby plans list
```

Archive a completed plan:

```bash
uv run gobby plans archive PLAN_ID
```

Agents should use `gobby-plans` MCP tools for plan lifecycle changes:

```text
list_tools(server_name="gobby-plans")
get_tool_schema(server_name="gobby-plans", tool_name="create_plan")
call_tool(server_name="gobby-plans", tool_name="create_plan", ...)
```

## Plan Mode

Plan mode is enforced through workflow/rule state. In plan mode:

- The agent may write the active plan artifact.
- Non-plan file writes are blocked by plan-mode write rules.
- User approval can exit plan mode and authorize execution.
- The UI can show an approval bar for the active plan state.

The relevant rule template is:

```text
src/gobby/install/shared/workflows/rules/plan-mode/block-writes-outside-plan-artifact.yaml
```

Rule templates are not runtime rules by themselves. Installed DB rules are the
source of truth after daemon startup and sync.

## Plan Records

Plan records are stored in the local database by `LocalPlanManager`. Key fields
include:

- `plan_id`
- `plan_path`
- `plan_hash`
- `plan_kind`, such as `implementation` or `strategy`
- `state`, such as `active` or `archived`
- `root_task_ref`
- coverage manifest metadata

Coverage manifests are generated under the project root and are removed when a
plan is archived or deleted.

## Archive And Delete

Archiving keeps history while moving the plan out of the active set. The MCP
`archive_plan` tool moves the file under:

```text
.gobby/plans/completed/
```

and removes the coverage manifest.

Deleting is a hard delete. `delete_plan` removes the plan row and coverage
manifest. Use delete only for invalid or accidental records; use archive for
completed work.

## CLI

The plans CLI includes:

```bash
uv run gobby plans list
uv run gobby plans show PLAN_ID
uv run gobby plans register PATH
uv run gobby plans validate
uv run gobby plans archive PLAN_ID
uv run gobby plans review-runs PLAN_ID
```

Use the CLI for operator inspection. Agents should use the MCP tools for plan
lifecycle writes.

## HTTP

Plan mode is visible in chat/session behavior rather than a single public plan
route. The Web UI receives plan-mode state through session and chat state, then
renders plan approval controls such as `PlanApprovalBar`.

When debugging plan mode from the browser, inspect session requests, chat events,
and plan approval state rather than only static plan files.

## MCP

`gobby-plans` owns agent-facing plan records:

- `create_plan`
- `get_plan`
- `list_plans`
- `archive_plan`
- `update_plan_hash`
- `regenerate_coverage_manifest`
- `delete_plan`
- `validate_plan`

Plan file edits still obey normal agent write rules. MCP plan records do not
override plan-mode restrictions on unrelated files.

## File Locations

- `src/gobby/mcp_proxy/tools/plans/`: `gobby-plans` MCP tools.
- `src/gobby/cli/plans.py`: operator CLI.
- `src/gobby/storage/plans.py`: plan persistence.
- `src/gobby/install/shared/workflows/rules/plan-mode/`: plan-mode rules.
- `web/src/components/chat/PlanApprovalBar.tsx`: UI approval control.
- `web/src/hooks/useSessionReconciliation.ts`: chat/session reconciliation.
- `.gobby/plans/`: project plan artifacts and completed plans.

## See Also

- [spec-writing.md](spec-writing.md)
- [task-expansion.md](task-expansion.md)
- [tasks.md](tasks.md)
- [workflow-rules.md](workflow-rules.md)
- [tdd-enforcement.md](tdd-enforcement.md)

_Last verified: 2026-05-08_
