---
name: plan
description: Adaptive /gobby plan workflow. Investigates first, recommends lightweight or full planning depth, requires decision elicitation, and preserves explicit human gates for artifact enhancement, adversarial review, and optional build handoff.
version: "4.1.2"
category: core
triggers: plan, specification, requirements
metadata:
  gobby:
    audience: interactive
    depth: 0
---

# /gobby plan

Both `$gobby plan` and `/gobby plan` invoke this workflow.

## Depth Selection and Required Elicitation

1. Investigate the request and repository before recommending a planning depth.
   Resolve discoverable facts with repository inspection, using `gcode` for code
   navigation. Do not ask the user for facts the repository can answer.
2. Classify the work kind before considering breadth or risk. Bug fixes and
   maintenance always recommend **Lightweight**, regardless of breadth, risk,
   affected subsystems, public API or schema involvement, or coordination needs.
   Full-depth candidates are limited to:
   - a complex new feature with multiple dependent deliverables across
     subsystems;
   - a complex refactor or subsystem rework that changes multiple components and
     their consumers; or
   - a broad migration or architecture/security-model rework with many consumers
     and a coordinated rollout.
3. Recommend **Full** only for those complex feature, refactor, rework, and
   migration candidates. Recommend **Lightweight** for every bug fix, maintenance
   change, localized feature or refactor, configuration change, and documentation
   change. Strong signals determine whether Gobby planning is offered; they do not
   promote bug fixes or maintenance to Full. Security or destructive risk,
   unresolved product decisions, multi-agent coordination, durable handoff, and a
   desire for lifecycle automation or adversarial review increase rigor within
   the chosen depth.
   Ask the user to choose between the two depths. If the user already selected a
   depth in response to the Plan Mode Consider prompt, honor that choice without
   asking again.
4. Load `restraint` and `elicit` for every Gobby plan:

```text
get_skill(name="restraint") on gobby-skills
get_skill(name="elicit") on gobby-skills
```

Run its grill-me protocol in both depths. Resolve discoverable facts through
repository inspection, ask one material decision at a time with a recommendation,
and finish with a confirmed Decision Record before drafting either plan. Every
recommendation you put to the user, and every choice you make unattended, walks
`restraint`'s decision ladder and names the rung it stopped at; the ladder
chooses among complete solutions only.

## Common Path

Select depth and elicit every decision that changes scope or architecture.
Lightweight plans stay in the conversation, remain artifact-free, and stop after
their compact checkpoint. Full plans preserve one canonical plan artifact and
pass through draft, checkpoint, enhancement, and adversarial review before
approval.

## Topic Index

- **Lightweight request:** call `get_skill_file(name="plan", path="references/lightweight.md")`. Do not load adversarial material.
- **Full draft or enhancement:** call `get_skill_file(name="plan", path="references/full-drafting.md")`.
- **Adversarial review after a Full draft checkpoint:** call `get_skill_file(name="plan", path="references/adversarial-review.md")`.
- **Review evidence, continuation, manifest, or recovery:** call `get_skill_file(name="plan", path="references/evidence-and-recovery.md")`.
- **Approved plan entering implementation:** call `get_skill_file(name="plan", path="references/build-handoff.md")`.

Load at most three references for one planning phase. Finish or discard the current phase before loading handoff guidance.

## Boundaries

- Do not create or claim tasks during plan drafting or plan review.
- Lightweight plans never create, update, validate, or hand off files under
  `.gobby/plans/`; those artifact operations belong exclusively to Full planning.
- Any `.md` under `.gobby/`, `.claude/`, or `.codex/` (CLI-owned artifact
  trees) is exempt from `require-task-before-edit` when Full planning creates an
  artifact.
- Enhancement (step 4.5) is advisory: apply only user-accepted suggestions, and
  never let it gate, approve, reject, or block the adversary review. The human
  is the scope gate.
- Do not emit `[TDD]`, `[IMPL]`, or `[REF]` tasks in the plan.
- Do not leave unanswered questions in the final plan. Resolve them before
  approval or record them as explicit out-of-scope deferrals.
- Do not bypass expansion-mode validation before build handoff.
