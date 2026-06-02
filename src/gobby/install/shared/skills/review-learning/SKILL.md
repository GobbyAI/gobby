---
name: review-learning
description: Use when review, QA, CI, static-analysis, or test-fixer work should recall or record reusable review-signal lessons.
version: "1.0.0"
category: core
triggers:
  - review learning
  - review lesson
  - record_review_lesson
  - recall_review_context
metadata:
  gobby:
    audience: all
    depth: 0
---

# Review Learning

Use this skill when a review producer, QA reviewer, linter fixer, or test fixer
needs to preserve a verified finding as reusable project knowledge.

The tool server is `gobby-review-learning`.

## Required Timing

Before finalizing review triage decisions, call
`gobby-review-learning.recall_review_context` with the findings and any proposed
fix text. The finding table must include a `Relevant memory/lesson` column.

If recalled memory contradicts a reviewer recommendation, local memory wins
unless current code disproves it. Example: if a review suggests `$1` SQL
placeholders but memory says Gobby psycopg storage uses `%s`, reject or rewrite
the recommendation.

After a confirmed reusable outcome, call
`gobby-review-learning.record_review_lesson`. Do not record raw leads.

## Finding Payload

Pass the diagnostic and generalized lesson in one `finding` dict:

- Required: `title` or `message`.
- Preferred: `pattern_id`, `principle`, `root_cause`, `prevention`.
- Optional: `query_hints`, `lesson_type`, `finding_fingerprint`,
  `guardrail_target`, `rule_id`, `rule_url`, `severity`, `path`,
  `start_line`, `end_line`, `symbol`, `suggestion`, `diagnostic_format`.
- `diagnostic_format` should be `raw`, `sarif`, `rdjson`, or
  `review_comment`.

Seed `lesson_type` examples: `durable-writes`, `sql-placeholders`,
`session-scope`, `task-lifecycle`, `validation-gates`, `workflow-verdicts`,
`memory-recall`, `idempotency`, and `test-isolation`. These are examples, not
an enum.

Use `pattern_id` when you can generalize the issue. Without it, the service
derives from `lesson_type` plus `principle`; without either, the lesson is
stored as `non-promotable` and will not create guardrail tasks.

## Decisions

Use:

- `confirmed`: current code or a verified fix proves the finding.
- `no-fix-policy`: the correct outcome is to tune a rule, profile, checklist,
  or policy because the finding is intentionally rejected.
- `stale` or `invalid`: skip recording.

For `ci_check`, `static_analysis`, and `test_failure`, record only after a
verified fix exists in `evidence` such as `commit`, `commit_sha`,
`verified_fix_ref`, or `fix_ref`. A raw failure with no verified fix must not
create a lesson.

## Evidence

`evidence` must include the concrete proof used for the decision: commit SHA,
changed files, validation command, review link, or no-fix rationale. For CI,
static-analysis, and test fixes, include the verified fix reference.

Pass `session_id` when available. The service stores the lesson in that
session's project and preserves `source_session_id`.

## Promotion Ladder

The service records a lesson memory first. It creates or updates a guardrail
implementation task only when thresholds cross:

- `confirmed`, first occurrence: memory only.
- `confirmed`, second occurrence: `test` target by default.
- `confirmed`, third or later occurrence: `validation` target by default.
- `confirmed`, high risk: `rule` target immediately.
- `no-fix-policy`, first occurrence: memory only.
- `no-fix-policy`, second or later occurrence: only `checklist` or `tool-config`.
- `stale` or `invalid`: no-op.

`guardrail_target` may be `helper`, `test`, `checklist`, `rule`, `workflow`,
`pipeline`, `validation`, or `tool-config`. The task is not the guardrail; it is
the evidence-backed work item to build or update the guardrail.

## Sibling Sweep

When a finding has `query_hints`, use gcode before fixing:

```bash
gcode search "<query_hints>"
gcode grep "<stable exact term>" src/ tests/ -m 50
```

Sweep sibling code for the same pattern before deciding the fix is complete.
