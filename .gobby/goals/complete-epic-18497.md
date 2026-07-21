---
goal: Complete 0.5.0 ship-blocker epic #18497
status: active
anchor: "#18497"
mode: solo
created: 2026-07-21
sessions: ["#9341"]
---

# Complete Epic #18497

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
- 2026-07-21 #9341 activation — user approved execution here; claimed #18497,
  set `auto_task_ref`, `goal_file`, and `additional_skills`, and reconciled 17
  remaining open descendants plus closed setup task #18667.
- 2026-07-21 #9341 iteration 1 — closed #17654 with commit `b70f1aaca`;
  42 focused and 101 lifecycle/integrity tests passed, Ruff and mypy passed,
  and test-quality reported zero issues. Close validation passed after rerunning
  gates with exit-code-correlated evidence; close-triggered compaction supplied
  the required boundary.
- 2026-07-21 #9341 iteration 2 — closed #17659 with commit `c08fa44bc`;
  4 focused composition tests and 133 dispatcher/observability tests passed;
  Ruff format/check, mypy, Bandit, and test-quality passed. Close validation
  passed after rerunning the long suite with exit-code-correlated evidence;
  close-triggered compaction supplied the required boundary.
- 2026-07-21 #9341 iteration 3 — closed #17662 with commit `c2a8402ce`;
  388 focused classifier, lifecycle, storage, migration, merge, stage, and build
  tests passed; Ruff format/check, mypy, Bandit, test-quality, and diff checks
  passed. Structured failure categories now distinguish retryable infrastructure
  failures from code/test verdicts and surface per-category build counts;
  close-triggered compaction supplied the required boundary.
- 2026-07-21 #9341 iteration 4 — closed #17665 with commit `b1fb1841b`;
  69 focused expansion and prompt tests passed; Ruff format/check, strict mypy,
  Bandit, package build, test-quality, JSON, and diff checks passed. Expansion
  context now surfaces related existing tests and the prompt selects test updates
  for covered behavior; close-triggered compaction supplied the required boundary.
- 2026-07-21 #9341 iteration 5 — closed #17673 with commits `4ce1b5917` and
  `ad2180201`; 136 focused tests passed, and the bundled workflow corpus now
  enforces exact MCP failure routes or an explicit stay policy plus satisfiable
  ordering guards. Ruff format/check, strict mypy, Bandit, package build, YAML
  schema, test-quality, and diff checks passed; close-triggered compaction supplied
  the required boundary.
- 2026-07-21 #9341 iteration 6 — closed #18490 with commits `592f4326b` and
  `9faa749dd`; replaced NULL-as-global memory scope with required project
  ownership plus explicit `is_global` visibility throughout PostgreSQL, services,
  secondary projections, APIs, sync, and the web UI. The exact acceptance suite
  passed with 1,273 backend tests and 29 frontend tests (2 backend tests skipped);
  Ruff, mypy, frontend type-check, and frontend lint also passed.
- 2026-07-21 #9341 iteration 7 — closed #18499 with commit `b4d6b5c19`;
  normalized `memory_type` to the four canonical enum values across write
  boundaries, storage migration, JSONL sync, and Qdrant payloads and filters.
  Broad focused validation passed with 1,338 tests and 2 skips; the final
  correlated acceptance run passed 392 tests, with Ruff, strict mypy, Bandit,
  test-quality audit, and diff checks also passing. Direct `compact_self` follows
  this bookkeeping commit as the required iteration boundary.
