# Runbook redesign pilot results

Evidence for section 6.1 of [the runbook redesign plan](../runbook-devteam-redesign.md),
recorded on 2026-10-23 by the #24797 developer seat in worktree `task-23850`.

## Status

The isolated automation for 6.1.1 and 6.1.2 passes. Live activation has not
started. The Orchestrator owns every live step below, and the rollout is not
complete until those steps pass and are recorded here.

## Isolated evidence

Both tests run on the isolated test hub with temporary home, Git repositories
and worktrees. They take seats, grants and the close policy from fixtures rather
than the shipped agent definitions, per Josh's ruling against tests tied to
mutable definitions. The scoped run below passed 16 tests:

```bash
DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test \
  GOBBY_TEST_PROTECT=1 GOBBY_TEST_GDAEMON=checkout uv run pytest \
  tests/e2e/test_devteam_lifecycle.py tests/e2e/test_runbook_release_cohort.py \
  tests/agents/test_team_teardown.py tests/releases/test_release_batch.py -q
```

### 6.1.1 Single-task lifecycle

`tests/e2e/test_devteam_lifecycle.py::test_complete_single_task_team` runs one
code task through its four-seat team under a committed `.gobby/project.json`
close policy that requires test, implementation, review and closing records:

1. The Test Writer claims the task, commits tests and records the test step.
2. The Dev Manager hands the claim to the Developer through
   `hand_off_task_claim`, and the Developer records the implementation.
3. The Developer's own review is refused ("claimant cannot review its own
   task") although its fixture grant allows review writes. The Code Reviewer
   approves.
4. Landing by the Developer is refused `caller_is_claimant`, and by the
   reviewer `caller_not_task_manager`. The Dev Manager lands the reviewed SHA,
   which marks the tree merged.
5. Close without the closing record raises `TaskStepRecordsMissingError` for
   `closing`. The Dev Manager records it and closes.
6. The teardown pass ends no seat at the close instant. After the grace period
   it ends all four seats, expires their sessions, and deletes the tree,
   worktree row and branch. The task keeps its close fields and both commits,
   and the closed team refuses further spawns with `team_closed`.

### 6.1.2 Concurrent release cohort

`tests/e2e/test_runbook_release_cohort.py::test_concurrent_release_cohort`
runs two real `run_batch` calls with injected check, activation, smoke and
announcement ports. A probe runs on the event loop while the first check of
each batch holds the window:

| Phase | Observed |
| --- | --- |
| Batch 1, succeeds | New team C refused `release_window_active`. Team B's landing returns the same blocker. B's replacement execution rejoins its tree and spawns a seat. Every check ran on the pinned candidate. |
| After release | The baseline advances, the window is gone, C is admitted, and B lands and closes. |
| Batch 2, lint fails | New team D and C's landing wait, and C keeps working. The prior baseline stays and the window is released. One open `release-regression-<sha12>` task is filed. Closed tasks A and B keep their close time, SHA and commits. |
| Lapsed lease | A lease stored with an expiry in the past holds nothing: D is admitted and C lands. |

### First runs

Neither test found a product defect. Both cover behavior delivered in sections
2.1 to 4.2, with TDD enforcement off for this cohort, so neither has a RED.

- The lifecycle test's first run failed at admission. Its fixture
  `.gobby/project.json` lacked the project `id`, which isolation requires to
  write the tree's sidecar. Real project files carry it, so the fixture changed.
- The cohort test's first run failed on a test error: `Task` has no `status`
  field. The assertion now compares the close fields.

## Project policy reconcile

`.gobby/workflows/rules/project-operating-policy.yaml` changed in 55760b3d60:

| Version | Blob |
| --- | --- |
| Before (0.5.0) | `7192a2e4a1c9b34fb20e78770ba435c967e0c9a4` |
| After (this branch) | `aab3e18e7b8b8ad01cfa81abb1e43eabf01413bf` |

The change removes `heavy_run_cap`, `close_backlog_cap`, the Release Manager
heavy-slot grant and the `CARGO_BUILD_JOBS=3` text. It adds `workspace_layout`
and `release_cadence_minutes`, and renames the launch defaults to `devteam`
and `support`. Monitors, researcher and security reviewer move to
`role_launch_defaults`, and the task-close reviewer entry is removed. The
policy injected into this session on 2026-10-23 still carries the lane-tree
and heavy-cap lines, so the installed row is the 0.5.0 version.

## Pending activation steps

The Orchestrator runs each step after #24797 lands, and records its result
here:

1. Before sync, record the installed registry rows and the policy row hash.
2. Run normal `gobby sync`. It retires the deleted identities: the
   `lane-manager`, `inbox-manager` and `task-close-reviewer` agents, the
   lane-manager and task-close-reviewer procedures, the `team-lane` and
   `team-coordination` pipelines, and the `build-agent-safety` rule. Record the new hashes, and confirm the policy row
   matches blob `aab3e18e7b`.
3. Supersede memory f166c746. Its content already names task-scoped trees, but
   its rationale still defends the retained-seat workflow, and its manual
   `delete_worktree` procedure overlaps the daemon teardown of closed DevTeam
   trees.
4. Reconcile gate-only backlog ownership. #23817 needs nothing more: the
   Orchestrator closed it as obsolete on 2026-10-23 at 14:15 UTC.
5. Run one operator-visible live cohort under Release Manager authority, with
   global BEFORE and DAEMON BACK notices: one DevTeam task through landing,
   close and teardown, and one release batch.
6. Confirm that the retained standalone roles still launch.

Any failed safety or lifecycle case is fixed before the rollout counts as
complete.
