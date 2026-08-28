---
name: plan-review
description: Review a gobby plan document for missing requirements, bad sequencing, unhandled edge cases, weak testability, and traceability gaps. Use when asked to review or critique a plan.
version: "1.5.0"
category: methodology
internal: true
triggers: plan review, plan critique, adversarial review, plan audit
metadata:
  gobby:
    audience: all
    depth: 0
---

# plan-review — Gobby Plan Adversarial Review Methodology

Review the canonical plan artifact as a hostile implementer: verify deterministic gates first, then walk every branch for missing decisions, coverage, ordering, and recovery. Findings must be specific, repairable, and preserved across rounds.

## Common Procedure

1. Resolve and gate the canonical artifact.
2. Trace each requirement through targets, consumers, tasks, validation, and recovery.
3. Emit structured findings with repair class and exact plan location.
4. Approve only after a clean gate and complete handoff sequence.

## Topic Index

- **Artifact identity and deterministic Plan-Coverage gate:** call `get_skill_file(name="plan-review", path="references/deterministic-gate.md")`.
- **Review stance, branch walking, traceability, coverage, or proportionality:** call `get_skill_file(name="plan-review", path="references/traceability-and-coverage.md")`.
- **Finding schema, repair rules, or prior-round preservation:** call `get_skill_file(name="plan-review", path="references/findings-and-repairs.md")`.
- **Clean approval, manifest handoff, halt, escalation, or autonomous exit:** call `get_skill_file(name="plan-review", path="references/approval-and-handoff.md")`.

Load no more than three references in one review round. Keep the canonical artifact, validation output, findings ledger, and recovery instructions intact.
