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

Any agent that verifies an injected lesson is obsolete must call
`gobby-review-learning.retire_review_lesson` with the injected `pattern_id`,
non-empty verification evidence, and the current `session_id`.

## Finding Payload

Pass the diagnostic and generalized lesson in one `finding` dict:

- Required for `confirmed` and `no-fix-policy`: non-empty `title` or `message`,
  plus non-empty `principle` or `prevention`.
- Preferred: `pattern_id`, `root_cause`.
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
stored as `non-promotable` and remains available only through ordinary memory
recall.

Broad lessons are still useful as memories. Review learning never creates or
updates Gobby tasks automatically.

## Plan Review Domain

`source_kind=plan_review` uses the reserved lesson classes `reviewer-miss` and
`fixer-induced-defect`. Treat this vocabulary as closed:

- `reviewer-miss`: a defect remained present across one or more completed
  adversary rounds.
- `fixer-induced-defect`: a revision made for an earlier finding introduced a
  distinct defect found in a later round.

Plan lessons use the class-scoped identity
`plan-review:<lesson_type>:<adversary-category>:<check_key>`. Supply the same
explicit `check_key` in the finding. Before minting, call `list_check_keys` with
`lesson_domain=plan` and the target class; reuse an existing key for the same
check. Mint a new key only when no existing key represents the check.

Canonical starter keys by adversary category are:

- `missing-requirement` → `requirement-coverage`
- `bad-sequencing` → `dependency-order`
- `unhandled-edge` → `edge-case-coverage`
- `weak-testability` → `acceptance-observability`
- `traceability` → `requirement-traceability`
- `over-engineering` → `proportionality`
- `gobby-format` → `plan-contract`

Every plan-domain finding uses `guardrail_target=checklist` and the synthetic
diagnostic anchor `rule_id=plan-review:<adversary-category>`. Omit a plan-file
`path`; section identity and durable evidence provide the location.

Evidence bundles are class-specific:

- `reviewer-miss` requires a non-empty `participating_section_ids` set naming
  every section involved in the defect, the earlier and approval evidence ids,
  and the number of completed rounds missed. Every participating section must
  be hash-unchanged between the cited finalized round and final approval.
- `fixer-induced-defect` requires a non-empty `causal_section_ids` set naming
  every section changed by the causal fix, `causal_finding_id`,
  `introduced_in_round`, and the causal and approval evidence ids. Every causal
  section must have changed between those finalized rounds.
- A dual-class lesson requires both complete bundles. An unproven class records
  nothing for that class; one proven class remains independently recordable.

Section sets contain ids only. Evidence services resolve and compare hashes
from immutable manifests. Reject unknown ids, empty class-required sets, and
partial cross-section proof.

The reviser records the lesson after final approval because the reviser owns the
changelog and causal history. The adversary emits typed attestations and never
calls `record_review_lesson` for its own plan round.

## Decisions

Use:

- `confirmed`: current code or a verified fix proves the finding.
- `no-fix-policy`: the correct outcome is to tune a rule, profile, checklist,
  or policy because the finding is intentionally rejected.
- `stale` or `invalid`: skip recording.

The required finding field groups apply only to `confirmed` and
`no-fix-policy`; `stale` and `invalid` remain no-op decisions.

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

## Lesson Delivery

Every `confirmed` or `no-fix-policy` occurrence is stored as project memory.
Workflow rules recall relevant lessons into future reviewer, planner, fixer,
and QA sessions, where each lesson's `prevention` text acts as contextual
checklist guidance. `stale` and `invalid` remain no-op decisions.

`guardrail_target` may record the review producer's suggested durable surface:
`helper`, `test`, `checklist`, `rule`, `workflow`, `pipeline`, `validation`, or
`tool-config`. It is metadata only and causes no task or repository mutation.

Create a normal Gobby task separately only after inspection identifies a
concrete artifact, owner, implementation approach, and independently verifiable
acceptance criteria. Recurrence count alone is evidence of importance, not a
decision-complete work item.

## Sibling Sweep

When a finding has `query_hints`, use gcode before fixing:

```bash
gcode search "<query_hints>"
gcode grep "<stable exact term>" src/ tests/ -m 50
```

Sweep sibling code for the same pattern before deciding the fix is complete.
