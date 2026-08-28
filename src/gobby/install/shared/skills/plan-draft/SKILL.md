---
name: plan-draft
description: Methodology for drafting a gobby plan document — phases, task format, TDD compatibility, categories, hierarchy, and dependency notation. Use when drafting or revising a plan artifact.
version: "1.2.1"
category: methodology
internal: true
triggers: plan drafting, plan format, plan specification
metadata:
  gobby:
    audience: all
    depth: 0
---

# plan-draft — Gobby Plan Drafting Methodology

Author the canonical plan narrative after depth and design decisions are settled. Every task must be implementation-ready, self-contained, dependency-ordered, and verifiable. The expansion compiler owns manifests; this skill owns narrative quality.

## Common Drafting Flow

1. Establish the Plan-Coverage grammar required by the plan.
2. Inventory targets, consumers, carriers, and ordering constraints.
3. Draft phases and deliverable tasks with category, goal, targets, implementation, validation, and exclusions.
4. Run deterministic verification and revise the whole plan after every finding.

## Topic Index

- **Plan-Coverage clauses or grammar:** call `get_skill_file(name="plan-draft", path="references/plan-coverage-grammar.md")`.
- **Targets, consumers, derived carriers, or shared-target ordering:** call `get_skill_file(name="plan-draft", path="references/targets-and-consumers.md")`.
- **Phase/task templates, categories, hierarchy, TDD, or granularity:** call `get_skill_file(name="plan-draft", path="references/task-structure.md")`.
- **Pre-presentation checks or revision rounds:** call `get_skill_file(name="plan-draft", path="references/verification-and-revision.md")`.

Load no more than three references for a drafting pass; run verification as a separate pass when the first three are already active. Preserve plan artifacts, validators, and revision evidence exactly.
