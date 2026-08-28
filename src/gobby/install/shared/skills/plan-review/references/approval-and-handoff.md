# Approval and handoff

## Manifest Handoff on Approval

`## M1 Task Manifest` is the typed bridge between deliverable sections and the
leaves the deterministic compiler emits. The adversary records reviewed routing
decisions. `derive_plan_review_manifest` generates canonical entries, including
titles, source sections, exact covers labels, and validation criteria. Each
criteria string contains every covered acceptance item in source order as
`<item-id>: <full acceptance text>`.

See `docs/contracts/plan-coverage.md` (§ "Task Manifest") for the entry schema
and parser-enforced invariants. This skill covers the adversary's handoff
responsibility; the coordinator owns compare-and-apply.

### Plan Identity Precondition

Before any manifest handoff or approval, verify the plan text outside fenced code
blocks contains a real `**Plan ID:** <id>` marker. Missing, blank, or literal
`unknown` Plan IDs are blocking findings because they generate
`covers:unknown:*` labels. Reject any existing or generated manifest that
contains a label beginning `covers:unknown:` before approving the plan.

### Sequence on Clean Review

When no blocking findings remain (zero findings or only nits):

1. Re-check the Plan Identity Precondition above.
2. Record routing decisions for each deliverable: category, task type,
dependencies, TDD, and assigned agent or implementation domain.
3. Call `derive_plan_review_manifest` with those decisions. Treat typed
diagnostics as rejection evidence. Never summarize or hand-author server-owned
labels or validation criteria.
4. Call `validate_plan_review_coverage`, then return `verdict: approved` with
the exact routing decisions, derived `manifest_entries`, and canonical
`coverage_attestation`. Entry count alone is insufficient.

### Plan-File Write Scope

The adversary never writes the plan file. `apply_plan_review_manifest`
re-derives entries from the evidence snapshot and routing decisions, rejects
any differing payload, revalidates freshness, and performs the only manifest
write. Rejection returns typed findings plus shadow-manifest diagnostics; typed
`repairs` on those findings are payload, written only by the coordinator's
`apply_plan_review_repairs` after the vote and the finalized checkpoint. The
coordinator owns `## V1 Plan Changelog`, writing round entries only through
`append_plan_changelog_round`, and the planner owns revisions.

## Escalation Policy

Findings carry one severity:

- `blocking` — plan expansion must wait for a repair.
- `nit` — useful non-blocking guidance.

Escalate **only when context is insufficient or a true human-intervention blocker exists**.
For routine revision rounds, return a non-approval verdict instead:

- If ≥1 `blocking` finding after the second pass → return
  `verdict: needs_review` with formatted findings.
- If only `nit` findings remain → return `verdict: approved`.
- If zero findings after the second pass → approve cleanly.

Non-blocking nits never trigger escalation on their own.

---

## Halt Conditions

Preparation rejects a missing, empty, or structurally invalid plan before the
adversary runs. Requirements context that remains unresolved after direct
repository and task inspection becomes a blocking `missing-requirement`
finding carrying the specific questions the plan must answer.

Do **not** approve a plan you do not understand. When in doubt, emit blocking
findings with your specific unanswered questions rather than manufacturing
findings or rubber-stamping.

## Autonomous Exit

When running as spawned `plan-adversary-taskless`, send the exact JSON result
to the parent first. Legacy `plan-adversary` runs finish the stage-native
verdict first (`approve_review`, `reject_review`, or `escalate_task`), then send
the exact JSON result returned or assembled for that verdict to the parent.
`send_message` durably binds that result to the run. In both paths, call
`end_agent_run` on `gobby-agents` with **no arguments** only after delivery.
