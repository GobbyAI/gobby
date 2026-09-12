---
name: plan-draft
description: Methodology for drafting a gobby plan document — phases, task format, TDD compatibility, categories, hierarchy, and dependency notation. Use when drafting or revising a plan artifact.
version: "1.3.0"
category: methodology
internal: true
triggers: plan drafting, plan format, plan specification
metadata:
  gobby:
    audience: all
    depth: 0
---

# plan-draft — Gobby Plan Drafting Methodology

Author the plan narrative after scope and design decisions are settled. Every
task must be implementation-ready, self-contained, dependency-ordered, and
verifiable. The expansion compiler owns manifests; this skill owns narrative
quality.

## Draft Authority Boundary

When the provider can write project files, author the canonical plan at
`.gobby/plans/<slug>.md`. When writes are unavailable, the `plan` coordinator
may supply the complete latest draft from the existing structured handoff for
narrative work. That staged conversation has not passed deterministic validation.
Do not create a scratch store, draft-storage tool, autosave path,
planning task, or indirect write to evade provider permissions.

Materialize the complete staged draft when writes become available. From that
point the canonical file is the sole authority: read and revise it rather than
maintaining parallel conversational content. File-based validators, review,
registration, and expansion begin only after materialization.

## Common Drafting Flow

1. Establish the Plan-Coverage grammar required by the plan.
2. Inventory targets, consumers, carriers, and ordering constraints. Capture what
   inspection reveals as you go, then carry the findings into each owning
   deliverable's Research context. Copy shared findings needed by each executor;
   preserve actionable conclusions rather than search transcripts.
3. Call `get_skill_file(name="plan-draft", path="references/task-structure.md")`
   and apply its Task Granularity Guidelines to every proposed deliverable before
   drafting. One section becomes one leaf; expansion does not subdivide oversized
   sections.
4. Draft phases and deliverable tasks with category, goal, targets, Research
   context, implementation, validation, and exclusions. Follow the Research
   Context contract in `docs/contracts/plan-coverage.md` proportionally to the work.
5. Recheck granularity after adding acceptance cases or consumers, then run
   deterministic verification and revise the whole plan after every finding.

## Topic Index

- **Plan-Coverage clauses or grammar:** call `get_skill_file(name="plan-draft", path="references/plan-coverage-grammar.md")`.
- **Targets, consumers, derived carriers, or shared-target ordering:** call `get_skill_file(name="plan-draft", path="references/targets-and-consumers.md")`.
- **Phase/task templates, categories, hierarchy, TDD, or granularity:** call `get_skill_file(name="plan-draft", path="references/task-structure.md")`.
- **Pre-presentation checks or revision rounds:** call `get_skill_file(name="plan-draft", path="references/verification-and-revision.md")`.

Load no more than three references for a drafting pass; run verification as a separate pass when the first three are already active. Preserve plan artifacts, validators, and revision evidence exactly.
