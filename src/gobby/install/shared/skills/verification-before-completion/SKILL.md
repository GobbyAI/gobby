---
name: verification-before-completion
description: Use when about to claim work is complete, fixed, passing, approved, merged, ready, or handed off; requires fresh verification evidence before success claims, task closure, review approval, PR handoff, merge completion, or agent handoff.
version: "1.0.0"
category: core
triggers:
  - complete
  - fixed
  - passing
  - approved
  - ready
  - merged
  - verification
metadata:
  gobby:
    audience: all
    depth: 0
---

# Verification Before Completion

Use this skill before any statement or lifecycle transition that implies work is
done, correct, passing, approved, merged, or ready for someone else.

Core rule: fresh evidence first, success claim second.

## Fresh Evidence

Fresh evidence means evidence produced after the relevant change in the current
work session. A previous run, an assumption, a passing adjacent check, or another
agent's success report is not enough.

Before claiming success:

1. Identify the command, diff, review artifact, or delivery state that proves the claim.
2. Run or inspect the full evidence source.
3. Read the output and exit status.
4. Report the real state, including gaps when evidence does not prove success.

## Gobby Lifecycle Points

Apply this before:

- `close_task`, especially when passing `commit_sha`.
- `submit_for_review`, `approve_review`, and review handoffs.
- PR delivery handoff through PR-stage tools.
- Merge completion through merge-stage tools.
- Any statement that work is complete, fixed, passing, approved, merged, or ready.

Gobby's structural gates still do the enforcement work:

- Loading this skill is guidance only. It does not satisfy completion
  readiness by itself.
- `require-completion-readiness-evidence` blocks lifecycle success tools after
  edits or commits until fresh verification evidence is recorded.
- `require-error-triage-before-status` blocks status changes until touched-file
  lint, tests, type checks, or focused verification have been run and failures
  are fixed.
- Commit-linked `close_task` runs task validation. `skip_validation` is stripped
  when a commit is attached.
- `task-transitions` gives the pre-close checklist. Use it with this skill; this
  skill governs the evidence behind the words.

## What Counts

Use the relevant verification from `.gobby/project.json`, scoped to touched
files when the full suite would be wasteful. For review and delivery work,
evidence can also be a read diff, linked task validation, PR verdict, merge
result, or delivery-state record when that is the thing being claimed.

Successful shell validation commands are recorded automatically. For evidence
that cannot come from shell output, use `gobby-sessions:record_verification_evidence`
with `summary`, `evidence_type`, and `supports`.

Do not upgrade a partial check into a broad claim. Say "focused tests pass" when
only focused tests ran. Say "not verified" when evidence is missing.

## Red Flags

Stop and verify when you are about to write:

- "done", "fixed", "complete", "ready", "approved", "merged", or "passes"
- "should pass", "looks good", "probably fixed", or equivalent hedging
- a review approval without reading the required diff and validation evidence
- a merge completion note before reading the merge result

## Required Output Shape

When making a positive claim, include the evidence:

```text
Verified: uv run pytest tests/path/test_file.py -v passed.
```

When evidence is incomplete, say that directly:

```text
Changed the implementation. Not claiming tests pass yet; verification has not run.
```

Run the command. Read the output. Then claim the result.
