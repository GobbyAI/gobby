---
name: plan
description: Use when turning a request into an implementation task or a decision-complete plan.
version: "5.0.0"
category: core
triggers: plan, specification, requirements
metadata:
  gobby:
    audience: interactive
    depth: 0
---

# /gobby plan

Both `$gobby plan` and `/gobby plan` invoke this workflow. Interactive Plan Mode
also loads this skill on its first submitted prompt; route the work here after
investigating the request.

## Investigation, Routing, and Required Elicitation

1. Investigate the request and repository before choosing a route. Resolve
   discoverable facts through repository inspection, using `gcode` for code
   navigation. Do not ask the user for facts the repository can answer. Capture
   actionable findings as exploration happens: observed behavior, file-qualified
   symbols, call paths, helpers, fixtures, constraints, approach decisions, and
   observed versus planned checks. Carry them into the owning deliverable's
   Research context; retain them in the canonical draft or existing staging
   handoff rather than reconstructing them at the end.
2. Inventory independently closeable deliverables and their real dependency
   edges. A deliverable has one outcome, one bounded scope, and criteria that let
   it close without waiting for another deliverable.
   Apply this boundary inside the plan too: a deliverable section becomes exactly
   one implementation leaf. Split independently verifiable behaviors before
   drafting; shared files and a common feature name do not make work atomic.
3. Route one atomic, independently closeable deliverable expected to fit one
   focused agent session to the existing task workflow. Route multiple dependent
   deliverables to a plan. Apply the same boundary to bugs, maintenance, features,
   and refactors. Duration is an estimate, never the routing rule: a short change
   with dependent deliverables still needs a plan, while a high-risk atomic change
   may remain one task.
4. Load `restraint` and `elicit` for every Gobby planning request:

   ```text
   get_skill(name="restraint") on gobby-skills
   get_skill(name="elicit") on gobby-skills
   ```

5. Resolve every material decision before finalizing either route. Run its
   grill-me protocol before finalizing either route: ask one material decision at
   a time with a recommendation, apply `restraint`'s decision ladder, and present
   a confirmed Decision Record in plain conversation text.

## Common Path

Investigate first, map the deliverable graph, elicit material decisions, then use
exactly one route:

- An atomic deliverable becomes one concrete implementation task through the
  existing `tasks` workflow.
- Dependent deliverables become one plan whose authority is either the canonical
  project file or, while that write is unavailable, the complete latest draft in
  the existing structured session handoff.

## Topic Index

- **Atomic task or delivery-graph routing:** call
  `get_skill_file(name="plan", path="references/work-routing.md")`.
- **Plan drafting, provider write restrictions, materialization, or enhancement:**
  call `get_skill_file(name="plan", path="references/drafting-and-staging.md")`.
- **Optional adversarial review:** call
  `get_skill_file(name="plan", path="references/adversarial-review.md")`.
- **Review evidence, checkpoints, continuation, manifest, or recovery:** call
  `get_skill_file(name="plan", path="references/evidence-and-recovery.md")`.
- **Approved plan entering expansion or build:** call
  `get_skill_file(name="plan", path="references/build-handoff.md")`.

Load at most three references for one planning phase. Finish or discard the
current phase before loading handoff guidance.

## Boundaries

- The plan route creates no planning or per-round review tasks. Draft and review
  without creating task records for planning or per-round reviews. The atomic
  route may create the one real implementation task it is handing off. Do not
  create planning or per-round review tasks.
- Keep one authority. Use `.gobby/plans/<slug>.md` when the provider permits the
  project write; otherwise use only the existing structured handoff staging path.
- Never bypass provider write restrictions with shell redirection, an MCP write,
  a subagent, or another provider. Any `.md` under `.gobby/`, `.claude/`, or
  `.codex/` (CLI-owned artifact trees) is exempt from `require-task-before-edit`
  when the provider allows the write; that exemption grants no new provider
  capability.
- Enhancement and adversarial review are recommended and optional. File-based
  base validation is mandatory before review or approval, and explicit user
  approval remains mandatory before expansion.
- Do not emit `[TDD]`, `[IMPL]`, or `[REF]` tasks in a plan.
- Do not leave unanswered material questions in a finalized plan. Resolve them
  before approval or record them as explicit out-of-scope deferrals.
- Do not bypass expansion-mode validation before implementation handoff.
