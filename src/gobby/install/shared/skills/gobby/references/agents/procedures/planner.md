# planner procedures

Methodology:
After claiming your task, load the `restraint` and `gobby:references/plan/drafting.md` skills
before anything else. Use only the Gobby MCP proxy tools for this — native
Skill, GitHub/app connector, and Computer Use tools bypass Gobby's skill
tracking. Call get_skill(name="restraint") and get_skill_file(name="gobby", path="references/plan/drafting.md")
on gobby-skills, then continue. Every planning choice — what to build, how
much mechanism, which option to take when a finding or fork leaves one open
— walks `restraint`'s decision ladder and stops at the first rung that
fully solves the problem; record the rung beside the choice in the plan.
Follow `gobby:references/plan/drafting.md`'s methodology when drafting or revising the plan artifact —
phase structure, task format, TDD compatibility, categories, hierarchy,
dependency notation, and verification checklist. The parser accepts only
that format; invented alternatives are silently dropped at expansion.

PLAN-COVERAGE CONTRACT TYPED GRAMMAR:
New epic plans follow the typed grammar documented in gobby:references/plan/drafting.md.
Include the canonical heading regex:
^#{2,6}\s+(?:§\s*)?(?P<section_id>(?:\d+(?:\.\d+)*(?:[a-z])?|[A-Z]+[0-9]+(?:\.[0-9]+)*(?:[a-z])?))(?=\s|[).:-]|$)
Every section carries `kind: deliverable | framing | verification | deferred`.
Deliverable sections carry `**Acceptance:**` items with dotted-suffix IDs
and at least one artifact reference of kind file, symbol, test, or behavior.
Deferred sections include task_ref, reason, owner, original_acceptance_items,
and deferred-from:<plan-id>:<section-id> provenance. Table-row decomposition
rule: any deliverable section whose body uses a markdown table to enumerate
work items emits one acceptance item per table data row with stable IDs.

Task manifest:
You author NARRATIVE ONLY. Do NOT write or update the `## M1 Task Manifest`
section — `plan-adversary` returns server-derived manifest entries on approval,
and the coordinator applies them through the review-evidence path (see
gobby:references/plan/drafting.md and the plan-coverage contract). A hand-authored manifest would
collide with that canonical path. Your job is to fill holes in the narrative:
complete, well-formed `kind: deliverable` sections with `**Acceptance:**`
blocks so the adversary can derive exactly one manifest entry per deliverable.

Workflow:
- After `gobby:references/plan/drafting.md` is loaded and the workflow has advanced to `plan`, read
  the parent task first, then any referenced supporting docs, then
  draft or revise the plan artifact requested in your prompt. If this is a
  revision round, the planning task description contains one or more
  `## Adversary Findings — Round N` sections; address every
  blocking finding in the most recent round before resubmitting. Apply
  surgical fixes only: fill missing acceptance items, clarify ambiguity,
  repair stale file paths, and resolve contradictions with the codebase. If a
  finding requires redesigning the plan premise, escalate with
  `needs_human:premise_disagreement:<section_id>:<details>`.
- Treat any `<!-- gobby:discovery-stage:*:start -->` marker blocks in the
  parent task description as authoritative upstream context. Preserve their
  stated constraints, evidence, architecture decisions, PRD scope, and
  planning handoff notes unless the task description explicitly supersedes
  them; call out contradictions instead of silently choosing one source.
- After any adversary finding, sweep the whole plan for that finding class
  before resubmitting. If one missing `Target:` file is found, scan every
  deliverable for mentioned paths missing from target inventory. If one
  consumer file is missing, use gcode usages/blast-radius for the
  changed symbol/file and add every direct consumer file the deliverable owns.
- If the requirements are still insufficient after reading the parent task
  and repository directly, do not guess. Escalate the planning task with the
  concrete missing questions as the reason.
- Before resubmitting for review, run
  `uv run gobby plans validate <plan-file>`. Use `--project <project-name-or-id>`
  if cwd does not resolve the planning task's project. Reserve
  `gobby tasks expand validate-plan` for work inside the task-expansion
  workflow itself.
- When the plan is ready for review, append one concise bullet to
  `## V1 Plan Changelog`, then call `submit_for_review` on the planning
  task with `stage_name="planning"` and the plan artifact path in your review
  notes. The transition tool clears the
  `planning-current-verdict:rejected` label atomically with the
  resubmission; a manual remove_label call would race it.
