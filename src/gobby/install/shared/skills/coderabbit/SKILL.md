---
name: coderabbit
description: Use when processing CodeRabbit review comments, CodeRabbit CLI reports, or `$gobby coderabbit [findings]` requests.
version: "1.0.0"
category: core
triggers:
  - coderabbit
  - CodeRabbit findings
  - CodeRabbit report
  - "$gobby coderabbit"
metadata:
  gobby:
    audience: all
    depth: 0
---

# CodeRabbit

Use this skill for `$gobby coderabbit [findings]`, pasted CodeRabbit comments,
and files matching `./reports/coderabbit-*.md`.

REQUIRED SKILL: verification-before-completion.

## Contract

CodeRabbit findings are leads, not patches. Verify each item against current code
before changing files. Fix only findings that still apply. Include nits when they
are valid.

For every finding, keep a decision record:

- `fixed`: current code confirms the problem and the diff addresses it.
- `no-fix`: current code does not match the finding, the suggested path no
  longer exists, the behavior is already correct, or the suggestion is harmful.

Every `no-fix` decision needs a short reason with the checked file, symbol, or
behavior. Do not silently drop stale comments.

## Workflow

1. Create or claim a `gobby-tasks` task before edits.
2. Ingest all supplied findings and every `./reports/coderabbit-*.md` file.
3. For each report, identify whether it contains actionable findings or only a
   CodeRabbit CLI failure such as `Too many files`.
4. Inspect current code for each finding before deciding.
5. Apply valid findings, including small nits, using normal repo patterns.
6. Document stale or invalid findings as `no-fix` decisions.
7. Delete processed `./reports/coderabbit-*.md` files after their contents are
   fixed or documented. Leave unrelated report artifacts alone.
8. Run focused validation for the touched areas, plus scoped lint/type checks
   when available.
9. Commit with the task ref and close the task with `commit_sha`.

## Verification Discipline

Do not claim "CodeRabbit fixed" until validation has actually run. If validation
fails, fix the encountered failure before closing the task unless it genuinely
requires a separate architectural task.

When a report only records a CLI failure, document that it added no findings,
delete the processed report, and continue with validation for any code changes
from other findings.
