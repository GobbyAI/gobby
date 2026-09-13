# Plans And Plan Mode

Gobby has two related plan systems: chat plan mode for approval-gated planning,
and DB-backed plan records for durable implementation or strategy artifacts.

## Mental Model

Plan mode is a live session state. It lets an agent investigate and choose the
right delivery workflow while blocking unrelated file edits until the user
approves or exits plan mode. The Web UI exposes this through plan approval
controls.

Plan records are durable database rows. They register a plan file, hash, kind,
state, root task reference, and generated coverage manifest. They let Gobby
validate that a plan maps to the tasks and files it is supposed to cover.

Use `/gobby plan` to investigate first. One independently closeable deliverable
expected to fit one implementation session routes to the existing task
workflow; multiple dependent deliverables route to a plan. Bugs, maintenance,
features, and refactors all use the same rule. Duration is only an estimate.

The plan route drafts and revises a Markdown artifact with the user. When the
provider cannot write it yet, the complete latest draft can survive compaction in
the existing structured session handoff only when all rendered sections fit the
shared 10,000 JSON-escaped character cap. If it cannot fit and writing is prohibited,
surface the preservation conflict before compaction; never truncate the draft,
repeatedly submit oversized content, or bypass permissions through another writer.
When writing is permitted, save the canonical draft and reference its project-relative
path in the handoff. Once materialized, the file is the
sole authority. Deterministic validation and explicit user approval are
mandatory; enhancement and taskless adversarial review are recommended but
optional.

Planning has three roles: the **planner** drafts and folds in changes, the
optional **plan-enhancer** proposes constructive Better/Bigger improvements,
and the optional **plan-adversary** reviews correctness and contract
compliance. Neither reviewer edits the plan. Base validation and human approval
remain the mandatory gates.

Use plan records to track a plan artifact through validation, archival, review,
and deletion.

## Quick Start

Validate a plan file:

```bash
uv run gobby plans validate PLAN_FILE -p <project-root>
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

- The agent investigates the repository before routing work.
- When project writes are allowed, the plan workflow writes the active
  `.gobby/plans/<slug>.md` artifact.
- When project writes are unavailable, the plan workflow stages the complete
  draft in `set_handoff(clear_session=false)` and restores it with argumentless
  `get_handoff`; it does not invent a scratch store, draft-storage tool,
  autosave path, or indirect writer.
- A conversational draft is not deterministically validated. It must be
  materialized before file validation, review, registration, or expansion.
- Other file writes are blocked by `block-edits-plan-mode.yaml`.
- Shell mutation policy is unchanged; redirections and heredoc writes still use
  the existing plan-mode shell restrictions.
- `/gobby plan` does not create planning epics, review anchors, or per-round
  review tasks while drafting or reviewing. Registration waits for the real
  implementation root used by manual expansion or build.
- User approval can exit plan mode and authorize execution.
- The UI can show an approval bar for the active plan state.

The relevant rule template is:

```text
src/gobby/install/shared/workflows/rules/plan-mode/block-edits-plan-mode.yaml
```

Rule templates are not runtime rules by themselves. Installed DB rules are the
source of truth after daemon startup and sync.

## Optional Enhancement

After materialization and base validation, `/gobby plan` offers an optional
enhancement loop. When selected, the parent session spawns
`plan-enhancer-taskless` (no `task_id`, `isolation="none"`) with the plan path,
round number, max rounds, and parent session id. The enhancer loads
`gobby:references/plan/enhancement.md` and standalone `proportionality`, then returns ranked Better/Bigger
suggestions to the parent via `send_message` and calls `end_agent_run`. It never
claims tasks, edits the plan file, or calls a review verdict.

The coordinator surfaces the ranked suggestions to the user (impact-vs-effort,
top first); the human is the scope gate. Accepted suggestions are folded into the
plan artifact only, the plan is re-validated, and the round is recorded as
`kind: enhancement` under `## V1 Plan Changelog`. The loop stops on
`converged: true`, all-declined, or the round cap. Suggestions are offers. If
the user also selects adversarial review, accepted changes pass that review.

## Optional Adversarial Review

Taskless review uses `plan-adversary-taskless`. After project-aware validation, the parent calls `prepare_plan_review_round`,
passes the returned evidence ID, canonical path, clean deterministic sweep report,
round number, review cap, and parent session ID, then binds the spawned run with
`bind_evidence_run`. Spawn/bind failure expires the evidence; successful binding
is followed immediately by a structured `set_handoff(clear_session=false)`. The adversary loads
`gobby:references/plan/review.md` and standalone `proportionality`, returns structured findings or approval to
the parent, and calls `end_agent_run`. It does not claim or mutate Gobby tasks.
The adversary now also carries an `over-engineering` review dimension: mechanism
disproportionate to the goal is a finding, while ambition and net-new scope are
not.

Every review round is recorded in the plan under:

```markdown
## V1 Plan Changelog
`kind: verification`
```

Each round records reviewer run/session, verdict, findings, and resolution
notes. Keep prior rounds for audit. The reviewer reads an immutable snapshot,
completes three review lanes and returns server-validated coverage attestation;
it never writes the manifest. A rejection is appended and finalized with its
canonical result before the coordinator applies accepted typed repairs.

On user-accepted approval, the coordinator calls `apply_plan_review_manifest`,
then `append_plan_changelog_round`, `finalize_plan_review_evidence`, and
`checkpoint_plan_review_lesson_mint`. The daemon writes canonical V1 fences;
never hand-build them. Pending lesson mint blocks a subsequent review round.
If review is skipped, use `derive_plan_handoff_manifest` and
`apply_plan_handoff_manifest` with their exact returned hashes/digest instead.
These paths reject stale evidence rather than guessing a replacement manifest.

Before expansion, approved plans must carry `## M1 Task Manifest` and pass:

```bash
uv run gobby plans validate <plan-file> -p <project-root> --mode expansion
```

If adversarial review is skipped, the coordinator derives and applies the human
handoff manifest through the existing `gobby-plans` APIs; it never fabricates
review evidence. After user approval, offer either manual expansion or build:

```bash
uv run gobby build <plan-file> --planning-seed-state approved --completed-plan-review-rounds <N>
```

`/gobby plan references expansion` loads manual expansion guidance for debugging and
targeted reruns.

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

`create_plan` requires a real root task (`root_task_ref`, or `#N` inferred from a
`task-<N>-*` plan filename) and generates the initial coverage manifest. Do not
create a planning task only to register a draft. Manual expansion registers
against its real epic; `gobby build` creates or reuses the real root and
preserves its configured unattended stage sequence.

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
uv run gobby plans register PATH --root-task-ref '#42' --project <project>
uv run gobby plans validate PLAN_FILE -p <project-root>
uv run gobby plans archive PLAN_ID
uv run gobby plans review-evidence --help
uv run gobby plans review-runs '#42'
```

`review-runs` prints the expansion-QA handoff pointer
(`gobby-tasks-ops:run_expansion_qa_coverage`) for the planning task rather
than listing review runs.

Use the CLI for operator inspection. Agents should use the MCP tools for plan
lifecycle writes.

## HTTP

Plan mode is visible in chat/session behavior rather than a single public plan
route. The Web UI receives plan-mode state through session and chat state, then
renders plan approval controls such as `PlanApprovalActions`.

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

Review evidence uses `prepare_plan_review_round`, `get_plan_review_snapshot`,
`bind_evidence_run`, `expire_plan_review_evidence`, and `verify_plan_unchanged`.
Manifest derivation/application, coverage attestation, typed repairs, changelog
append/finalization, and lesson checkpoint tools share this service. Discover
unknown names with `list_tools`; fetch a known unleased schema directly.

Plan file edits still obey normal agent write rules. MCP plan records do not
override plan-mode restrictions on unrelated files.

## File Locations

- `src/gobby/mcp_proxy/tools/plans/`: `gobby-plans` MCP tools.
- `src/gobby/cli/plans.py`: operator CLI.
- `src/gobby/storage/plans.py`: plan persistence.
- `src/gobby/install/shared/workflows/rules/plan-mode/`: plan-mode rules.
- `web/src/components/chat/PlanApprovalActions.tsx`: UI approval controls.
- `web/src/components/app/useSessionReconciliation.ts`: chat/session reconciliation.
- `.gobby/plans/`: project plan artifacts and completed plans.

## See Also

- [spec-writing.md](spec-writing.md)
- [task-expansion.md](task-expansion.md)
- [tasks.md](tasks.md)
- [workflow-rules.md](workflow-rules.md)
- [tdd-enforcement.md](tdd-enforcement.md)

_Last verified: 2026-09-12_
