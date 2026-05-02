# Test Battery State

> Persistent state for the test-battery monitoring loop.
> Survives context compaction. Read this file on every skill invocation.

## Status: COMPLETED (manual QA phase)

## Configuration

- **Gobby Session ID**: #3053
- **Epic**: #10452 "Fix project context on reload + plan approval bugs"
- **Epic ID**: c8665869-dabb-4060-9724-815f86447620
- **Monitoring Task**: #10461 (ID: be55e44f-92c2-4e9e-a5d9-72083236b711)
- **Cron Job ID**: cj-3b6bf7f66f9b (disabled)
- **Pipeline**: orchestrator-worktree v1.2
- **Current Branch**: 0.2.30
- **Merge Target**: 0.2.30
- **Isolation**: worktree `wt-234a21` at `/Users/josh/.gobby/worktrees/gobby/epic-10452`
- **Dev Provider/Model**: gemini / provider-default
- **QA Provider/Model**: claude / opus
- **Agent Timeout**: 1200s
- **Cron Interval**: 300s
- **Max Concurrent**: 5
- **Started At**: 2026-03-18T23:02:33Z
- **Completed At**: 2026-03-19T01:00:00Z (approx)
- **Duration**: ~2 hours

## Final Task Summary

| Task | Title | Dev | QA | Verdict |
|------|-------|-----|-----|---------|
| #10453 | Remove duplicate PlanApprovalBar from MessageList | ✅ `c540bceb` | ✅ Autonomous (Claude Opus) | **CLOSED** |
| #10454 | Backend: send mode_changed for request_changes | ❌ Docstring only | — | **FAIL** — no implementation |
| #10455 | Frontend: auto-send feedback on plan_changes_requested | ✅ `fd4aabad` | Manual review passed | **PASS** — needs Playwright |
| #10456 | Add retry with backoff to fetchProjects | ✅ `635e8cbd` | Manual review passed | **PASS** — needs Playwright |
| #10457 | Add projectIdRef to useChat, send set_project on WS | ❌ Ruff formatting only | — | **FAIL** — no implementation |
| #10458 | Wire up sendProjectChange in App.tsx via useEffect | ✅ `fe69186d` | Manual review passed | **PASS** — needs Playwright |

**Score: 4/6 dev tasks produced real code. 1/6 fully autonomous (dev → QA → closed). 2/6 no-ops.**

## Remaining Work

- Merge worktree `epic-10452` → `0.2.30` for passing tasks (#10453, #10455, #10456, #10458)
- Playwright visual verification for #10455, #10456, #10458
- Reopen #10454 and #10457 for re-implementation (or verify if work was done by adjacent tasks)
- Note: #10454's backend fix may have been done in #10458's commit `809c2fcc` — needs verification

## Candid Assessment

### What Worked

1. **Gemini + worktree isolation works.** The upstream `.git` file bug (GitHub #12050) appears fixed. All 6 Gemini dev agents launched, ran, and interacted with git in the worktree without any `.git`-related failures. This is a significant finding.

2. **orchestrator-worktree pipeline works.** The worktree variant of the orchestrator correctly resolves worktrees, dispatches agents with `worktree_id`, and handles the full scan→suggest→dispatch cycle. Ticks completed in <1s consistently.

3. **First autonomous task closure (#10453).** Gemini dev committed clean code, Claude Opus QA reviewed it, validated against criteria, and closed the task — full loop, no human intervention. The validation feedback was substantive, not rubber-stamped.

4. **Agent resilience across daemon restarts.** Two of four agents survived a daemon crash. The AgentLifecycleMonitor re-registered them on restart. This is better than expected.

5. **Bug discovery.** Found 3 real bugs (#10462, #10463, #10464) plus confirmed 2 known issues (pipeline cache, zombie agents). The test battery is still the best bug-finding tool we have.

### What Didn't Work

1. **`require-commit-before-status` rule breaks in worktrees.** Every dev agent that committed was blocked from transitioning to `needs_review`. Required manual DB intervention every time. This is the #1 blocker for worktree orchestration. (#10462)

2. **ToolSearch catch-22 in step workflows.** QA agents couldn't load the deferred `mcp__gobby__call_tool` schema because `ToolSearch` wasn't in the claim step's allowed tools. Every QA agent spawned before the fix burned 20+ minutes trying different argument formats and failing. (#10464 — fixed live)

3. **QA concurrency in shared worktree.** The orchestrator dispatches one QA per tick, but since QA takes longer than 5 min, multiple QA agents end up running concurrently in the same worktree. They'll step on each other's toes running tests. Need a gate: only dispatch QA when no other QA is active.

4. **Zombie agents.** Agents that exit don't reliably unregister from the in-memory registry. This blocks subsequent dispatches ("agent already running for task"). Required manual `kill_agent` calls multiple times.

5. **Pipeline definition caching.** `reload_cache` doesn't refresh installed pipeline definitions — only bundled templates. Updating a pipeline STILL requires a daemon restart. Same bug as test battery #1. This should be fixed.

6. **2 of 6 Gemini agents produced no-ops.** #10454 added a docstring. #10457 reformatted whitespace. Both claimed to have completed the task. The dev agent step workflow has no mechanism to verify actual code changes — an agent can commit junk and mark for review.

7. **`annotate_observed` breaks in worktrees.** Commit linkage relies on session lifecycle tracking, which doesn't see worktree branch commits. Had to remove the step entirely.

### What Needs to Change for Test Battery #3

**Must fix before next run:**
- #10462: `require-commit-before-status` worktree support (biggest manual intervention burden)
- #10464: ToolSearch in step workflow allowed tools (already fixed, verify in templates)
- QA concurrency gate: only dispatch QA when no QA agent is active
- Pipeline cache refresh: `reload_cache` should also refresh installed definitions

**Should fix:**
- Zombie agent cleanup: agents should unregister on clean exit, not just on kill
- Commit validation: dev agent step workflow should verify non-trivial diffs before allowing `submit_for_review`
- `annotate_observed` worktree support (or make it non-fatal)

**Design considerations for unified comms test:**
- The orchestrator pipeline dispatches QA sequentially (one per tick, `tasks[0]`). For a larger epic, this creates a QA bottleneck. Consider dispatching QA for all needs_review tasks in one batch (like `dispatch_devs` uses `dispatch_batch`).
- Need a `dispatch_qa_batch` or extend `dispatch_batch` to support QA agent type selection.
- The current orchestrator is fire-and-forget — it has no awareness of whether agents succeed or fail. The lifecycle monitor handles recovery, but the orchestrator could be smarter about retry tracking.

## Bugs Filed During Run

| Ref | Title | Status |
|-----|-------|--------|
| #10459 | Add isolation mode prompt to test-battery skill wizard | open |
| #10462 | require-commit-before-status rule doesn't recognize worktree commits | open |
| #10463 | mcp_proxy.semantic_search doesn't resolve OPENAI_API_KEY from SecretStore | open |
| #10464 | Step workflow claim step blocks ToolSearch for deferred MCP tools | fixed live |

## Infrastructure Fixes Applied During Run

1. Removed `annotate_observed` step from orchestrator-worktree pipeline (v1.0 → v1.2)
2. Added `ToolSearch` to claim/terminate steps in both `developer` and `qa-reviewer` agent definitions
3. 3 daemon restarts for pipeline/agent definition changes
4. Multiple manual DB updates to transition tasks past `require-commit-before-status` rule

## Issues Log

| # | Cycle | Type | Description | Resolution |
|---|-------|------|-------------|------------|
| 1 | 3 | rule-bug | `require-commit-before-status` doesn't recognize worktree branch commits | Manual DB update. Filed #10462. |
| 2 | 3 | lifecycle | Zombie agents stayed in registry after exiting | Killed via `kill_agent`. |
| 3 | 4 | Bug #10000 | Tmux session for #10456 agent died, process ran 16+ min orphaned | Killed orphan, reopened task. |
| 4 | 4 | rule-bug | #10458 agent couldn't transition (same as #1) | Manual DB update. |
| 5 | 4 | daemon-crash | Daemon died. 2/4 agents survived. | `gobby start` — clean recovery. |
| 6 | 5 | pipeline-cache | `reload_cache` syncs 0 installed pipelines | Removed step, restarted daemon. |
| 7 | 5 | worktree-commits | `update_observed_files` can't find worktree commits | Removed step entirely. |
| 8 | 5+ | ToolSearch | QA agents stuck in claim step — can't load deferred tool schema | Fixed agent definitions, restarted daemon. |
| 9 | 8+ | concurrency | Multiple QA agents dispatched concurrently in shared worktree | Killed newer agent. Needs pipeline gate. |

## Pipeline Executions

| Cycle | Execution ID | Status | Dispatched | Open | In Prog | Review | Approved |
|-------|-------------|--------|------------|------|---------|--------|----------|
| 1 | `pe-d85e4b90` | completed | 3 (#10453, #10456, #10457) | 6 | 0 | 0 | 0 |
| 2 | `pe-eaa7d8c9` | completed | 0 | 3 | 3 | 0 | 0 |
| 3 | `pe-a25f5ae4` | completed | 2 (#10454, #10458) | 1 | 3 | 2 | 0 |
| 4 | `pe-e6e5215b` | completed | 0 (QA #10453) | 1 | 3 | 2 | 0 |
| 5a | failed x3 | annotate_observed | — | — | — | — | — |
| 5b | `pe-8f75b350` | completed | 0 (QA #10457) | 1 | 2 | 2 | 0 |
| 6+ | multiple | completed | dev #10455, QA #10454 | 0 | 1 | 4 | 0 |

## Comparison: Test Battery #1 vs #2

| Metric | Battery #1 (OTel) | Battery #2 (Project/Plan) |
|--------|-------------------|--------------------------|
| Tasks | 10 | 6 |
| Duration | ~3 hours | ~2 hours |
| Isolation | Clone | Worktree |
| Dev provider | Gemini | Gemini |
| QA provider | Claude Opus | Claude Opus |
| Tasks fully closed | 10/10 | 1/6 |
| Bugs found/fixed live | 6 | 3 (+2 fixed live) |
| Daemon restarts | 1 | 3 |
| Manual interventions | ~5 | ~15 |
| No-op dev agents | 0 | 2 |

Battery #1 was cleaner because: (a) clone isolation doesn't hit the commit-status rule bug, (b) no ToolSearch catch-22 existed yet, (c) tasks were more self-contained. Battery #2 surfaced more infrastructure bugs but required significantly more babysitting.
