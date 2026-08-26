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

Concrete defects follow Rule 4 and should reference their resolving task and commit.
Durable project knowledge belongs in `gobby-memory`. Reports must omit secrets,
private user content, and transcript dumps.
