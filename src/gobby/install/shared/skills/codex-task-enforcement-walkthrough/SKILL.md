---
name: codex-task-enforcement-walkthrough
description: "Manual walkthrough for an interactive Codex session to probe Gobby task enforcement, skill injection, stop behavior, and lifecycle misuse. Use when validating or debugging the Codex+Gobby task workflow end to end."
version: "1.0.0"
category: testing
triggers: codex walkthrough, codex task enforcement, task enforcement walkthrough, codex gate test, codex workflow test
metadata:
  gobby:
    audience: interactive
    depth: 0
---

# Codex Task-Enforcement Walkthrough

Use this skill to run a focused, manual validation pass inside a live Codex session.

This is not an automated app-server test and not a general Codex framework. It is a
single-purpose walkthrough for discovering what is broken, what is working, and why.

The `/gobby codex-task-enforcement-walkthrough` invocation is itself the first user prompt
of the session. Treat the current system/injected context as the startup observation surface.

## Goal

Walk the session through these behaviors in order:

1. Startup/default-agent observations
2. Edit blocked before task claim
3. `gobby tasks` CLI blocked and redirected to MCP
4. `task-creation` skill injection
5. Real task creation + claim
6. `python` skill injection
7. Scratch implementation + verification
8. `task-transitions` skill injection
9. Close blocked before commit
10. Stop/disengage pressure while task is still open
11. Memory and error-triage close gates
12. Forbidden lifecycle mutations

Maintain one running report for the entire walkthrough and capture evidence at each step.

## Ground Rules

- Start from a clean git worktree. If `git status --short` is not empty, record that as an
  environment blocker and stop. Do not mix this walkthrough with unrelated local changes.
- Use a disposable branch so the scratch commit is isolated.
- Do not fix Gobby itself during the walkthrough. First collect evidence. Only use the minimum
  workaround needed to continue to later checkpoints.
- Do not use `AskUserQuestion` to test stop behavior.
- Do not use the `gobby tasks` CLI for task lifecycle operations. Use `gobby-tasks` MCP.
- Use a real temporary Gobby task, not a simulated one.
- Keep the walkthrough narrow. Do not turn every mismatch into a new task during the run unless
  the user explicitly asks or the mismatch blocks the rest of the walkthrough.

## Current Baseline Expectations

Treat these as the expected current behavior unless observation proves otherwise:

- Default-agent identity/session metadata should already be present at startup.
- `task-creation` should **not** already be injected at session start.
- `task-creation` should inject after `get_tool_schema("gobby-tasks", "create_task")` or
  `get_tool_schema("gobby-tasks", "claim_task")`.
- `python` should inject on the first `.py` `Read`, not necessarily on the first `.py` write.
- `task-transitions` should inject after `get_tool_schema("gobby-tasks", "close_task")`
  or the related lifecycle tools.
- Bash use of `gobby tasks ...` should be blocked and redirected to MCP.
- Direct lifecycle mutations through `update_task` should be rejected.
- `mark_task_needs_review` and `mark_task_review_approved` should be blocked in the default
  interactive agent context.
- A claimed open task should resist stopping until it is explicitly resolved.

If actual behavior differs, record the difference. Do not silently adopt the new behavior.

## Report Artifact

Use a single report file:

```bash
STAMP=$(date +"%Y%m%d-%H%M%S")
REPORT="reports/codex-task-enforcement-$STAMP.md"
BRANCH="codex-task-enforcement/$STAMP"
SCRATCH_DIR="tmp_codex_task_enforcement"
mkdir -p reports
```

Create the disposable branch before the task walkthrough:

```bash
git status --short
git switch -c "$BRANCH"
```

If the worktree is not clean, stop and report the blocker instead of continuing.

## Report Template

Write this skeleton first, then append to it after each checkpoint:

```markdown
# Codex Task-Enforcement Walkthrough

## Session Metadata
- Date:
- Repo:
- Branch:
- Clean worktree at start:
- Gobby session ref / id:
- Codex session or external id:
- Startup default-agent context present:
- Task-creation already present at startup:

## Checkpoint Log

### 1. Startup Context
- Expected:
- Observed:
- Evidence:
- Classification:
- Likely source:

## Confirmed Correct Behaviors

## Broken Or Ambiguous Behaviors

## Follow-up Task Candidates
```

For each later checkpoint, append a new `### N. ...` section with the same six fields.
Keep evidence snippets short and exact. Prefer copied block text, tool output summaries,
and commit/task refs over paraphrase.

## Failure Classification

Use one of these labels in the report:

| Classification | Use when |
|---|---|
| `hook-missing` | Expected block/injection context never appears |
| `wrong-trigger` | Feature exists but fires on the wrong event |
| `rule-mismatch` | Correct rule area, wrong allow/block outcome |
| `workflow-state-bug` | Task/session state disagrees with observed lifecycle behavior |
| `tool-surface-mismatch` | MCP tool works but the interactive Codex path cannot observe or drive it cleanly |
| `docs-gap` | Behavior is correct but surprising or easy to misuse |
| `blocked-by-previous` | Later checkpoint cannot be exercised because an earlier failure prevents it |

## Session ID Fallback

Prefer the Gobby session ref already shown in startup context, for example `#2549`.

If it is not visible, fetch it explicitly:

```python
get_tool_schema("gobby-sessions", "get_current_session")
call_tool("gobby-sessions", "get_current_session", {
    "external_id": "<codex external/session id>",
    "source": "codex"
})
```

Record both the Gobby session ref and the Codex external/session id in the report once known.

## Walkthrough Procedure

### 1. Startup Context

Before doing any other work, record:

- Whether default-agent identity/context is already present
- Whether a Gobby session ref is visible
- Whether `task-creation` is already present
- The current repo path and branch

Expected:

- Default-agent context present
- `task-creation` absent

If `task-creation` is already present, classify as `wrong-trigger` or `docs-gap` depending on the
surrounding context.

### 2. Edit Before Task Claim

Attempt to create the scratch file before any task is claimed:

- Target file: `tmp_codex_task_enforcement/fib.py`

Expected:

- The write/edit attempt is blocked by the task-before-edit rule

Record:

- The exact block reason
- Whether the attempted file write was prevented

If the write succeeds before claim, classify as `rule-mismatch`.

### 3. Native Task CLI Redirect

Attempt a Bash command using the human CLI:

```bash
uv run gobby tasks list
```

Expected:

- Blocked in Bash
- Redirects you to `gobby-tasks` MCP

Record the exact redirect text.

### 4. Task Schema Lookup And `task-creation`

Fetch schemas before calling task tools:

```python
get_tool_schema("gobby-tasks", "create_task")
get_tool_schema("gobby-tasks", "claim_task")
```

Expected:

- `task-creation` is injected here, not at startup

Record whether the skill appeared after the schema fetch and where it appeared.

### 5. Create And Claim The Real Temporary Task

Create a real task with `claim: true`:

```python
call_tool("gobby-tasks", "create_task", {
    "title": "Codex task-enforcement walkthrough scratch task",
    "description": "Manual validation task for Codex task enforcement using scratch fibonacci files.",
    "category": "code",
    "task_type": "task",
    "claim": true,
    "validation_criteria": "tmp_codex_task_enforcement/fib.py and tmp_codex_task_enforcement/test_fib.py exist, targeted pytest/ruff/mypy pass, and the close-task gates are exercised in order."
}, session_id="<gobby session ref>")
```

Record:

- The created task ref
- Whether the session is now task-claimed

If claim does not stick, classify as `workflow-state-bug`.

### 6. Forbidden Lifecycle Mutations While Claimed

Fetch the `update_task` schema, then probe the forbidden direct lifecycle paths before doing the
final close flow:

```python
get_tool_schema("gobby-tasks", "update_task")
call_tool("gobby-tasks", "update_task", {"task_id": "#N", "status": "closed"})
call_tool("gobby-tasks", "update_task", {"task_id": "#N", "status": "open"})
call_tool("gobby-tasks", "update_task", {"task_id": "#N", "status": "needs_review"})
call_tool("gobby-tasks", "update_task", {"task_id": "#N", "status": "review_approved"})
call_tool("gobby-tasks", "update_task", {"task_id": "#N", "assignee": "<gobby session ref>"})
```

Expected:

- `update_task` rejects lifecycle misuse with explicit redirect errors

### 7. Trigger `python` Skill, Then Implement Scratch Files

Trigger the current `python` injection path by reading an existing Python file first:

```text
Read src/gobby/__init__.py
```

Expected:

- `python` skill injects on this `.py` read

Then create the scratch files:

- `tmp_codex_task_enforcement/fib.py`
- `tmp_codex_task_enforcement/test_fib.py`

Use a small recursive implementation. Minimum expected behavior:

- `fib_sequence(0) == []`
- `fib_sequence(1) == [1]`
- `fib_sequence(5) == [1, 1, 2, 3, 5]`
- Negative input raises `ValueError`

If `python` does not inject until write or never injects, classify the result accordingly.

### 8. Run Verification

Run targeted checks on the scratch directory:

```bash
uv run pytest tmp_codex_task_enforcement/test_fib.py -v
uv run ruff check tmp_codex_task_enforcement
uv run mypy tmp_codex_task_enforcement
```

Record pass/fail and the key failure lines if anything breaks.

### 9. Trigger `task-transitions`

Fetch the close-task schema:

```python
get_tool_schema("gobby-tasks", "close_task")
```

Expected:

- `task-transitions` injects here

### 10. Attempt Close Without Commit

Attempt to close the task before committing:

```python
call_tool("gobby-tasks", "close_task", {
    "task_id": "#N",
    "changes_summary": "Added scratch fibonacci implementation and tests for Codex task-enforcement walkthrough."
}, session_id="<gobby session ref>")
```

Expected:

- Blocked by commit-related enforcement

Record the exact gate text.

### 11. Probe Stop/Disengage Behavior

Do not use `AskUserQuestion`.

Instead, tell the user plainly that the next step needs one stop attempt, for example:

```text
Please send one short stop request now so I can capture the stop-hook behavior, then I will continue the walkthrough.
```

Expected:

- The stop/disengage attempt is blocked or redirected because the claimed task is still open

Record the exact stop-hook or directive text.

If the user does not provide a stop attempt, mark this checkpoint `blocked-by-previous` or
`untested` in the report and continue.

### 12. Commit And Retry Close

Commit the scratch files only:

```bash
git add tmp_codex_task_enforcement/fib.py tmp_codex_task_enforcement/test_fib.py
git commit -m "[gobby-#N] feat: add codex task enforcement walkthrough scratch files"
```

Retry close with the commit SHA:

```python
call_tool("gobby-tasks", "close_task", {
    "task_id": "#N",
    "commit_sha": "<commit sha>",
    "changes_summary": "Added scratch fibonacci implementation and tests for Codex task-enforcement walkthrough."
}, session_id="<gobby session ref>")
```

Expected current order:

1. Memory review gate
2. Error-triage gate

If the order differs, record it as a mismatch but continue.

### 13. Clear Memory Gate

Clear the memory gate directly:

```python
set_variable(name="memory_review_completed", value=true, session_id="<gobby session ref>")
```

Retry the same `close_task` call.

Expected:

- Now blocked by the error-triage gate

### 14. Clear Error Gate And Close

Clear the error gate:

```python
set_variable(name="errors_resolved", value=true, session_id="<gobby session ref>")
```

Retry the same `close_task` call again.

Expected:

- Task closes successfully

Record the final outcome and task status.

### 15. Interactive Review Actions After Close

After the main close flow is complete, confirm the interactive session still rejects the
pipeline-style review actions:

```python
get_tool_schema("gobby-tasks", "mark_task_needs_review")
get_tool_schema("gobby-tasks", "mark_task_review_approved")
call_tool("gobby-tasks", "mark_task_needs_review", {"task_id": "#N"}, session_id="<gobby session ref>")
call_tool("gobby-tasks", "mark_task_review_approved", {"task_id": "#N"}, session_id="<gobby session ref>")
```

Expected:

- `mark_task_needs_review` is blocked in interactive mode
- `mark_task_review_approved` is blocked in interactive mode

## After The Walkthrough

Update the report with:

- A short list of behaviors that worked exactly as expected
- A short list of broken or ambiguous behaviors
- Likely source files or rules when you can identify them confidently
- Follow-up task candidates, but do not create them unless the user asks

Then summarize the results to the user in plain language:

- What passed
- What failed
- What was surprising but currently expected
- Which issue looks highest leverage to fix first

Leave the disposable branch in place unless the user explicitly asks for cleanup.
