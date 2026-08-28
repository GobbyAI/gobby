# Gobby Session Feedback Research Inbox

This directory supports a bounded research trial for first-hand agent feedback about
Gobby sessions. Raw reports under `inbox/` are taskless and Git-ignored. Curated findings
belong in tracked reports created through the normal Gobby task workflow.

Most context epochs should produce no report. Task-close and pre-compact triggers share
one epoch-scoped acknowledgment, so an agent receives at most one request between
compactions. A useful report contains at most three specific observations grounded in
the current session. Suitable kinds are `friction`, `noise`, `surprise`,
`missing-affordance`, and `positive-signal`.

Use this shape:

```markdown
---
session_id: 11117
date: 2026-08-26
cli: codex
task_refs: [21013]
trigger: post-close
---

# Gobby Session Feedback

## Observation 1: Concise title

- Kind: friction
- Evidence: Specific rule, tool, message, or observed sequence.
- Impact: Extra turns, confusion, blocked progress, risk, or useful acceleration.
- Frequency: once | repeated
- Suggestion: Concrete improvement, when apparent.
- Disposition: research-signal | fixed-in-task #NNNNN | existing-task #NNNNN
```

A concrete defect is found work, never just an observation: create a claimed task under
the standing epic #21128 (`Gobby session feedback findings`, pass
`parent_task_id: "#21128"`) and fix it in the reporting session; hand it off to the
session that owns the surface; file it under the same epic labeled `needs-decision` or
`clean-window` only as a last resort. Every finding task lives under that epic so the
trial's findings can be reviewed and batched from one tree. Record the resolving task
and commit in the observation's `Disposition` line.
Durable project knowledge belongs in `gobby-memory`. Reports must omit secrets,
private user content, and transcript dumps.
