# Close verified work

Load before any task close, including no-work dispositions. Discover `close_task`
on `gobby-tasks`, `wait_for_agent` on `gobby-agents`, and post-close
`review_task_memories` on `gobby-memory`. Tool schemas own parameters.

Finish in this order:

1. Finish all edits; resolve every owned finding and verify applicable criteria.
2. Run focused validation after the final edit and follow it to definitive exit.
   Credit is judged per shell call. Direct commands, environment prefixes,
   leading `cd <dir> &&`, and `<check-a> && <check-b>` chains (each segment) are
   credited. Pipelines such as `| tail`, `;` command sequences, trailing output
   such as `echo`, fallbacks, backgrounding, subshells, and obscuring wrappers
   are not; one of them anywhere voids every check in that call. Recover with a
   direct rerun of each validation as its own call. Changed Python tests also
   require a clean `gobby test-types audit` against the test-types baseline with
   `--fail-on-new`, credited from one invocation whose explicit targets lexically
   cover every changed test: a file target covers itself, a directory target covers
   its descendants, `tests/` is the whole-tree fallback, a rename needs source and
   destination, and a deleted test is covered by auditing its parent directory.
   Coverage is judged per invocation and never unioned across runs, so one file per
   call never adds up. Beyond that baseline and `--fail-on-new`, only the
   output-only flags `--format`, `--output` and `--min-severity` keep credit.
3. Stage only task paths and commit with a task reference. Use
   `git commit --only -m '[<project_name>-#<task_number>] fix: describe the change' -- <task paths>`.
4. Call `close_task` once with `task_id`, `commit_sha`, `changes_summary`, and
   `preview=true`. Include exact validation commands and results in the summary.
5. Repair any deterministic blocker before retrying. If the response is
   `agentic_review_required`, register `wait_for_agent` with `validator_run_id`
   and yield. Do not poll or repeatedly call close while review is running. A
   headless run refuses that wait; follow the refusal's `retry_guidance` instead.
6. After `closed=true` or a closure notification, call `review_task_memories`
   with the task and summary; change memory only for valuable durable knowledge.

A blocked call reports the whole checklist at once. Fix every gate reported
`failed` before retrying; a gate reported `skipped` names the gate that blocked it
and is unevaluated, never satisfied.

`preview=true` closes when ready. The checklist requires criteria and summary,
linked commits for attributed edits, no uncommitted attributed files,
category-appropriate transcript validation, and one bounded criteria review.
Changed evidence earns a new review; retrying unchanged evidence reuses its verdict.
When `unlinked_tagged_commits` names additional commits, link those belonging to
the task before retrying. Otherwise use `link_commit` only to keep a task open.
On `task_scope_mismatch`, retry with a specific `scope_justification` explaining
why paths outside the declared Targets belong to the task.

Validation is drawn from claiming, closing, and worked-on sessions within their
link windows. Code/refactor/test require a clean test-category run; config
requires a clean validation command. Docs/planning/research/manual/no-edit closes
skip that command gate, but still owe their acceptance criteria and review.

Organizational parents can close after children finish; closing the last child
auto-closes eligible ancestors. A claimed parent or parent with linked commits
keeps its own work gates. Never report the epic complete while children remain.
Land and remove owned isolation workspaces, or explicitly hand them off through
workspace tools, before calling the task finished.

No-work reasons are `duplicate`, `already_implemented`, `wont_fix`, `obsolete`,
and `out_of_repo`. Explain the disposition; no commit is needed only when there
were no attributed edits. A deliberately closed escalated task requires a
meaningful `override_justification`; it skips only criteria review, not the other
gates. Do not escalate merely to get a close exception. `submit_close_review` is
validator-only and is never a shortcut for the implementing session.

Guide: [Close](../../../../../../../../docs/guides/tasks.md#close).

_Last verified: 2026-09-19_
