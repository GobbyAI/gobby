---
name: live-session
description: "Run an authorized interactive work session under one live-session task. Use for live start <scope>, live done, and skills that need a multi-round task lifecycle."
version: "1.0.0"
category: core
triggers: live session, live start, live done
metadata:
  gobby:
    audience: interactive
    depth: 0
---

# /gobby live-session

Own the task lifecycle for interactive, multi-round work. Loading this skill is
the authorization required to apply the `live-session` label. The label exempts
the session from the two open-task turn-end gates only; commit, validation,
dirty-file, criteria-review, and close gates still apply.

## Invocation

- `/gobby live-session start <scope>` or `live start <scope>` — start or
  resume one umbrella task.
- `/gobby live-session done` or `live done` — validate, commit changed work,
  and close the umbrella task.

This workflow is restricted to a root interactive terminal session. Spawned,
automated, and web-chat sessions cannot add or remove the authorization label.

## Live Start

1. Read the session's `claimed_tasks` variable and fetch every referenced task
   from `gobby-tasks`. If any claimed task lacks `live-session`, refuse to
   start. Never mix an ordinary claim with a live-session claim.
2. Reuse the session's existing open `live-session` task when there is exactly
   one. When no task is claimed, list open `live-session` tasks matching the
   scope:
   - Reuse an unclaimed matching task by calling `claim_task`.
   - Refuse a matching task owned by another live session.
   - Create a task when no reusable match exists.
3. Create the umbrella task with:
   - title `Live session — <scope>`;
   - `category="code"`, `implementation_domain="fullstack"`;
   - `labels=["live-session"]`, `claim=true`;
   - validation criteria naming the scope, touched behavior, and relevant
     project verification commands.
4. Call `update_task` with `allow_automation=false` and `isolation="none"`.
   Task creation defaults keep `unattended=false`; verify all three values on
   the returned task before editing.
5. Merge `live-session` into the session's `additional_skills` variable so a
   compacted continuation reloads this workflow.

## During The Session

- Keep one umbrella task claimed across every round.
- Attribute edits and commits to that task through the normal task workflow.
- Checkpoint commits may reference the same task. Keep the task open until
  `live done`.
- Ending a turn is safe only while every session claim carries `live-session`.
  A later ordinary claim restores both stop gates immediately.

## Live Done

1. Fetch every claimed task. Refuse completion if the set is empty, mixed, or
   contains more than the one live-session umbrella task.
2. Review task-attributed paths and reconcile unfinished work.
3. Run the smallest complete validation batch for the touched behavior. Record
   every command and result for the close gate.
4. Inspect task-attributed Git status:
   - Changes present: stage only attributed files and create one task-linked
     commit. Use the final checkpoint commit when earlier checkpoints already
     contain all work.
   - No changes: skip commit creation. Never manufacture an empty commit.
5. Review durable memories and create, update, or delete only valuable durable facts.
6. Call `close_task` once with the task ref, `changes_summary`, `preview=true`,
   and the commit SHA when one exists. Repair any returned gate failure before
   retrying.
7. Remove `live-session` from `additional_skills` after the task closes.

## Expired Session Recovery

Daemon recovery owns abandoned live-session claims. It releases a claim whose
task-attributed paths are clean and escalates dirty or indeterminate claims
with the expired session reference and attributed paths. A resumed session
must fetch task state again before attempting `live start`.
