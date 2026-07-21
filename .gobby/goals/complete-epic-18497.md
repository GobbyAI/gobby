---
goal: Complete 0.5.0 ship-blocker epic #18497
status: draft
anchor: "#18497"
mode: solo
created: 2026-07-21
sessions: ["#9341"]
---

## Objective

Complete and close every task in epic #18497, preserving each task's contract,
with focused validation and a linked commit for every changed leaf. Compact this
Codex session through `gobby-sessions:compact_self` between completed leaf tasks
so each iteration resumes from the durable goal document and live task state.

## Success Criteria

- [ ] Every descendant in the live #18497 task tree is closed; the 17 open
      descendants present at drafting are #14844, #15005, #17654, #17659,
      #17662, #17665, #17673, #18490, #18491, #18499, #18501, #18516, #18519,
      #18637, #18638, #18643, and #18656.
- [ ] Each changed leaf has focused validation evidence and a task-linked commit.
- [ ] `gobby-sessions:compact_self` separates completed leaf iterations; no next
      leaf starts before the compacted session resumes and reconciles live state.
- [ ] No descendant remains claimed, escalated, blocked, or awaiting review.
- [ ] No goal-owned worker, worktree, or clone remains active.
- [ ] Epic #18497 closes only after its live tree is complete.

## Non-Goals

- Work outside the live #18497 tree.
- Expanding or weakening a leaf's acceptance criteria to make it easier to close.
- Running the full pytest suite unless the user explicitly requests it.

## Procedure

1. On activation, claim #18497 and set `auto_task_ref` to `#18497`, `goal_file`
   to `.gobby/goals/complete-epic-18497.md`, and merge `goal` into
   `additional_skills` for session #9341.
2. Re-read this document after every compaction. Reconcile its Progress Log with
   `gobby-tasks:get_task`, the live descendants, and the advisory budgets; the
   database is authoritative.
3. Select exactly one actionable leaf with `gobby-tasks:suggest_next_task`
   scoped to #18497. Read its full contract, claim it, and complete the whole
   task using the least mechanism that satisfies its criteria.
4. After the final edit, run focused validation with `GOBBY_TEST_PROTECT=1` for
   pytest commands. Fix every encountered error, warning, test failure, lint
   failure, and type error. Do not run the full pytest suite.
5. Commit only the leaf's intended files using
   `[gobby-#<leaf>] <type>: <summary>`, review durable-memory candidates, then
   close the leaf through `gobby-tasks:close_task` with its commit SHA and a
   concrete changes summary.
6. Append one Progress Log entry with the iteration, leaf, validation, commit,
   and close outcome. Then call `gobby-sessions:compact_self` for session #9341
   as a top-level `call_tool.session_id`. If close-triggered compaction already
   interrupted the turn, treat that delivery as the required boundary. Never
   begin another leaf in the pre-compaction context.
7. After resume, return to step 2. When no actionable leaf remains, inspect all
   descendants directly. Resolve actionable blockers; escalate only decisions
   that genuinely require the user.
8. When the tree and all Success Criteria are complete, run final scoped checks,
   close #18497, set this document to `status: done`, append final evidence, and
   clear `auto_task_ref` and `goal_file`.

## Budgets

- max_iterations: 40
- max_active_agents: 1
- max_runtime: 72h

## Stop Conditions

- All Success Criteria met and #18497 closed -> mark `status: done`.
- Any budget exceeded -> mark `status: suspended`, record exact live state, set
  handoff context, clear goal variables, unclaim the anchor, and notify the user.
- External stop signal or user cancellation -> suspend using the same procedure.

## Escalation

- Escalate a blocking leaf only when a user decision is genuinely required;
  continue any other actionable leaf first.
- Never guess about destructive migrations, acceptance-criteria changes,
  external release state, credentials, or deleting user-owned worktree changes.

## Progress Log

- 2026-07-21 #9341 draft — verified open epic #18497 and its 17 open descendants;
  created solo compact-between-leaves execution contract.
