---
name: code-review
description: How to review code with open-code-review delegate mode. Use when asked to review code or a diff, to run an adversarial review, or when the commit gate or a review gate blocks.
version: "1.0.0"
category: engineering
triggers: code review, diff review, delegate review, review gate, adversarial review
tags:
  - gobby
metadata:
  gobby:
    audience: all
    depth: 0
---

# Code Review

Review local git state with open-code-review (`ocr`) delegate mode. OCR does the
deterministic work: which files are reviewable and which checklist applies to each
of them. You do the reviewing. OCR calls no LLM in this mode, so nothing is
configured on its side.

REQUIRED SKILL: review-learning.

## Preflight

```bash
ocr --version
```

If `ocr` is missing and `npm` is on PATH, install it and re-check:

```bash
npm i -g @alibaba-group/open-code-review
```

If `ocr` is still missing, use the fallback: `git diff --name-only <range>` (or
`git status --short --untracked-files=all` for the workspace) for scope and
`references/default-checklist.md` for rules. Never parse text output as JSON and
never invent fields a command did not print. If `--format json` fails with
`unknown flag: --format`, rerun without it and read the text output; any other
error is reported, not retried.

## Delegate Workflow

### 1. Scope

```bash
ocr delegate preview --format json                            # workspace mode
ocr delegate preview --format json --from <base> --to <head>  # range mode
ocr delegate preview --format json -c <sha>                   # commit mode
```

Workspace mode covers staged, unstaged, and untracked work. Range mode reviews a
branch against its base. Commit mode reviews one commit. Pass the task title and
description with `-b "<text>"` (under 8000 characters) so the context sits in the
output while you review. Add `--exclude <glob,glob>` only to drop generated files.

Output fields: `schema_version`, `mode`, `from`, `to`, `commit`, `merge_base`,
`background`, `reviewable_files[]` with `path`, `status`, `insertions`,
`deletions`, and `excluded_files[]` with the same fields plus `exclude_reason`.
Excluded files are not reviewed. When an excluded file carries the change under
review (a workflow rule, a skill, a documented contract), add it to the checklist
and review it against `references/default-checklist.md`.

### 2. Rules

```bash
ocr delegate rule --format json <path> [<path> ...]
```

Output is `groups[]` with `group_id`, `source`, `pattern`, `files`, and `rule`.
Files that share a checklist share a group. Project rules in
`.opencodereview/rule.json` take precedence over the built-in per-language docs:

```json
{"rules": [{"path": "src/**/*.py", "rule": "<text>", "merge_system_rule": true}]}
```

Fetch rules per batch when the change is large.

### 3. Diffs

| Mode | Command |
|------|---------|
| range | `git diff <merge_base>..<to> -- <path>` |
| commit | `git show <sha> -- <path>` |
| workspace, tracked | `git diff HEAD -- <path>` |
| workspace, untracked | read the file; all of it is new |

### 4. Review Each File

Build a checklist keyed by `(path, status)`. Workspace mode can list one path
twice when a staged deletion is followed by an untracked recreation. For each
entry:

1. Get its diff.
2. Apply its rule group plus any recalled review-learning lessons.
3. Review the changed lines with enough surrounding context to judge them. Use
   `gcode symbol-at path:line` and `gcode callers` when a change touches a
   contract other code depends on.
4. Mark the entry `reviewed`, or `skipped` with a concrete reason.

Batch large changes by shared rule group and diff size. Do not stop at the first
high-severity finding. Coverage is mandatory: every previewed entry ends reviewed
or explicitly skipped.

### 5. Findings

Every finding carries `path`, `start_line`, `end_line` (new-file numbering),
`severity`, `category`, `content`, and optional `suggestion_code`.

- Severity: `critical`, `high`, `medium`, `low`.
- Category: `bug`, `security`, `performance`, `maintainability`, `test`, `style`,
  `documentation`, `other`.
- Critical and high (bugs, security, data loss) always ship in the report and
  block the change. Medium (performance, error handling gaps, maintainability)
  is reported with context. Low is dropped unless clearly valuable.
- Discard likely false positives silently. A finding you cannot ground in the
  diff or tool output is not a finding.

## Reporting

Open with one summary line: `total_files`, `reviewed_files`, `skipped_files`
(each with its reason), and `coverage_rate`. Then a table grouped by severity:

| Location | Severity | Category | Finding | Relevant memory/lesson |
|----------|----------|----------|---------|------------------------|
| `path:start_line` | high | bug | ... | ... |

Preserve paths and line numbers exactly as observed. Do not auto-fix unless the
user asked for review and fix; then apply critical and high fixes directly,
describe medium fixes that need a decision, and skip low items unless trivial.
If `ocr` or `git` fails mid-review, report the failure. Never generate substitute
findings to fill the gap.

## Review-Learning Hooks

- Before finalizing material findings, call
  `gobby-review-learning.recall_review_context` with the findings and any
  proposed fix text. Its result fills the memory column. If local memory
  contradicts a generic recommendation, local memory wins unless current code
  disproves it.
- After a material reusable finding is confirmed by a verified fix or a concrete
  no-fix decision, call `gobby-review-learning.record_review_lesson` with
  `source_kind=agent_review`. Do not record raw leads.

## Execution Mode

Size the change first with `git status --short --untracked-files=all` and
`git diff --shortstat` (add `<base>...HEAD` for a branch). One or two reviewable
files: review inline. Larger or unclear: hand the review to a subagent that
follows this workflow and returns findings in the field shape above.

## Pre-Commit Self-Review

The `require-code-review-skill` gate blocks the first `git commit` of a session
until this skill is loaded. Run this self-review before that commit and before
every later commit in the session; it costs two `ocr` calls.

1. `ocr delegate preview --format json` (workspace mode). Intersect
   `reviewable_files` with `git diff --cached --name-only`; only staged paths
   count. If no staged path is reviewable, say so and retry the commit.
2. `ocr delegate rule --format json <staged reviewable paths>`.
3. Diff each path with `git diff --cached -- <path>` so only staged hunks are
   judged, not the rest of the working tree.
4. Review per step 4 above. Fix every critical and high finding, stage the fix,
   and re-run step 3 for the touched paths. Report medium findings in your reply.
5. Retry the commit.

## Topic Index

- Adversarial review, or a stop review gate that demands an `ALLOW:`/`BLOCK:`
  verdict: call
  `get_skill_file(name="code-review", path="references/adversarial-review.md")`
  and use its prompt templates.
- Fallback path with no `ocr`, or a previewed file whose rule group is missing:
  call
  `get_skill_file(name="code-review", path="references/default-checklist.md")`.
