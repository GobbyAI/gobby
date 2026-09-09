---
name: handoff-discipline
description: Write concise, current-state session and agent handoffs. Load before set_handoff or cooperative end_agent_run, and when context-pressure guidance appears.
metadata:
  gobby:
    audience: all
---

# Handoff Discipline

Choose the boundary from the task state, then write a readable structured
handoff derived from the provider-native tracker.

| Situation | Required action |
| --- | --- |
| Planning or review reaches context pressure | `set_handoff(clear_session=false)` |
| Active task reaches context pressure | `set_handoff(clear_session=false)` |
| Root/coordinator has closed the current task and moves to another task or epic child | `set_handoff(clear_session=true)` |
| Spawned worker finishes or completes a blocker handoff | Structured `end_agent_run(...)` |

State what is true now and give the receiving coordinator concrete next actions.
Include decisions, blockers, commands, diagnostics, paths, impact, and references
only when they help the receiver continue.

Do not paste cumulative history, previous handoffs, raw logs, completed ledgers,
or artificial shorthand.

When a detailed progress log is useful, create or update a Markdown file and
include its path in the handoff's `references`. Reuse an existing relevant log
when available. Keep the detail in that file; do not paste it into the handoff or
try to compress it there. Progress logs are optional, not a required handoff artifact.

Use references to existing plans and evidence for cross-session history. Carry
forward only the still-active constraints and unresolved work needed to continue.
Before submitting, remove inherited history and completed ledgers from the draft.

`set_handoff` accepts at most 10,000 JSON-escaped characters of rendered handoff
content, including section headings and formatting. Oversized content is rejected
before staging; shorten it and retry, retaining current state and concrete next
actions and referencing existing evidence for detail. Submit any required survey
first through `gobby-sessions:feedback` (observations=[] is valid). Submission is
not human review. Call `set_handoff` last; its `clear_session` boolean is the boundary
control. `clear_session=true` is reserved for moving between tasks after the current
task closes. A spawned worker supplies nonblank `current_state` and at least one
nonblank coordinator action in `next_steps` before cooperative success or a
`task_blocker` exit. Forced kills and crashes may have no authored handoff.
