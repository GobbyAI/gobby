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

REQUIRED SKILL: task-transitions.

## Plan Mode Gate

Before any CodeRabbit triage, first check whether the session is already in
native Plan Mode. If it is not, you MUST enter native Plan Mode before reading
reports, verifying findings, creating or claiming tasks, or editing files. Native
Plan Mode is the runtime-provided planning state; session-level planning is the
assistant's internal planning posture and does not satisfy this gate.

Use `EnterPlanMode` when the runtime provides it. When no direct Plan Mode tool
is exposed, use `gobby-sessions.send_keys(session_id, keys, literal=true)` to
send the provider's native Gobby plan command:

- Codex: `$gobby plan\n`
- Claude, Gemini, or Qwen sessions whose installed router is slash-based:
  `/gobby plan\n`
- Unsupported or unknown provider: stop with a blocker instead of guessing.

After sending keys, verify that the target session is actually in native Plan
Mode before triage. Loading the `plan` skill or composing a plan in normal chat
does not satisfy this gate.

Plan Mode triage is read-only: ingest supplied findings and
`./reports/coderabbit-*.md`, verify the current code, decide `fixed` or
`no-fix`, and present the implementation plan; wait for plan approval before continuing
to implementation. If native Plan Mode blocks task creation, create or claim the
`gobby-tasks` task immediately after plan approval and before the first edit.

The Plan Mode output MUST include a finding-by-finding table. Each supplied
finding gets its own row with:

- Decision: `fix` or `no-fix`.
- Checked file, symbol, or behavior.
- Reason for the decision.

No finding may be grouped away, summarized into another row, or silently
omitted from the final plan.

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

1. If not already in native Plan Mode, enter Plan Mode with `EnterPlanMode` or
   the provider-aware `gobby-sessions.send_keys` fallback; verify native Plan
   Mode is active, then complete read-only triage before implementation.
2. Create or claim a `gobby-tasks` task before edits.
3. Ingest all supplied findings and every `./reports/coderabbit-*.md` file.
4. For each report, identify whether it contains actionable findings or only a
   CodeRabbit CLI failure such as `Too many files`.
5. Inspect current code for each finding before deciding.
6. Apply valid findings, including small nits, using normal repo patterns.
7. Document stale or invalid findings as `no-fix` decisions.
8. Delete processed `./reports/coderabbit-*.md` files after their contents are
   fixed or documented. Leave unrelated report artifacts alone.
9. Run focused validation for the touched areas, plus scoped lint/type checks
   when available.
10. Commit with the task ref as `[<project_name>-#<task_number>] <type>: <summary>`
    and close the task with `commit_sha`. Use the real project name in the task
    reference, such as `[gobby-#123]`; `project_name` is a placeholder, never a
    literal prefix.
    Allowed types: `fix`, `feat`, `refactor`, `chore`, `docs`, and `test`.

## Verification Discipline

Do not claim "CodeRabbit fixed" until validation has actually run. If validation
fails, fix the encountered failure before closing the task unless it genuinely
requires a separate architectural task.

When a report only records a CLI failure, document that it added no findings,
delete the processed report, and continue with validation for any code changes
from other findings.
