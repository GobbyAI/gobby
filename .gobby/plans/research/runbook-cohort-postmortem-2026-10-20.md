# Runbook cohort post-mortem: land, close, delete

Researcher gobby#17248, 2026-10-20. Research only; the Proposal section is a proposal, not a plan.
Times are CDT unless marked.

## Research Questions

1. Why did landed tasks pile up waiting to close?
2. Why did worktrees and branches outlive their tasks?
3. What would make "land, close, delete the worktree" one step that always happens?

Starting hypothesis (Orchestrator 17164, 07:22 Oct 20, relayed by Assistant 15976): worktrees reused across tasks with stale records; rebased copies of commits leaving original branches behind; seats stopped mid-task; no reconciliation step after land and close.

## Short answers

1. Close was a relay of about ten hand-offs and nineteen decision points, and the daemon enforces none of the steps between landing and close. The relay needed live seats with specific authority, and the fleet's throttles, the one-task-per-seat rule, and session-bound receipt authority removed exactly those seats. Once one actor owned the whole chain (17164 from 12:22 Oct 19), the same tasks closed in hours: queue wait before the takeover was median 2.55 days, drain time after it median 0.19 days.
2. Nothing in the daemon connects landing or closing to the worktree registry. Deletion was a manual Orchestrator step for per-task trees only, seat trees were retained by policy, and the registry's branch and task labels went stale on every seat reuse, so the few deletions attempted were refused or unsafe. Rebased and review-split landings then broke the only merge proof the delete path accepts.
3. Make landing the trigger of a daemon-owned pipeline: land, credit validation from the integration branch, close, mark the tree merged, delete it, and record activation at the next restart. Human decisions shrink to the five the Assistant listed as genuinely Josh's.

## Domain Context

- Fleet: 80+ concurrent seats on runbooks, lanes 1 to 9, integration branch 0.5.0. Seats hold one task at a time; the author hands a reviewer-LANDed task off with escalate_task and moves on.
- Close pipeline: gates 1 to 12 deterministic, gate 13 an automatic close review by a spawned task-close-reviewer. Gate 10 credits validation runs only from session transcripts inside the claimant's evidence window. Gate 12 (TDD RED evidence) is hard-skipped since #24760 (commit 5e04e5bd16, 17:40 Oct 19).
- Receipts: independent_review_approval (reviewer, exact SHA), landing_approval and activation (accepted only from the task's creator or delegator session; never the claimant), landing (written by land_commit). Activation class comes from paths: src/ and pyproject are restart-class, crates/ is cutover-class.
- Worktrees: seat trees lane-N-<name>[-K], "retained per seat, never per task"; the next task runs git switch -c on the same tree. delete_worktree(merged_into="0.5.0") needs a live-free, clean tree whose attached branch is an ancestor of 0.5.0. Task and worktree lifecycle are decoupled by design (#12626, memory c6eff715).
- Timeline: Oct 13 20:01 15977 diagnosed "a merged worktree never empties out" and proposed per-task trees; Oct 17 Josh set the 20-close cap "to prevent worktree drift"; Oct 19 08:37 Josh: "I'm just confused why we can't land and close at the same time"; 11:54 "we only have 4 active agents. this is unacceptable"; 12:02 "39 tasks waiting to close"; 12:22 15977 handed the close queue to 17164; 12:32 15977 ended ("It failed to orchestrate"); 12:58 cap abolished; 13:32 test slots retired; 16:08 activation windows retired; Oct 20 01:59 broadened cleanup; 14:37 UTC 17164's drain record ends.

## Evidence and Findings

### Q1. Why landed tasks piled up

Ranked by weight of evidence.

1. Close is a relay with no daemon-owned step. The code map counts 19 decision points between reviewer LAND and worktree gone; 13 need a named actor (reviewer, author, Orchestrator, Lane Manager, Release Manager). The Lane Manager release and Release Manager close GO exist only as messages; no daemon code checks them. 15977, 12:10 Oct 19: every step between reviewer approval and close waits on a coordinator message, about ten minutes per round trip. The census (n=46) shows the same tasks that waited days before the takeover closing in hours after it: clean passes took 12 to 20 minutes end to end under 17164.
2. Landed tasks had to re-prove themselves. Gate 10 credits runs only inside the claimant's window and only after the last task edit (memory 5731bb6a), so every landed task needed a reclaim, a proof tree, a fresh pytest and type audit. 17164 hit 42 Gate 10 failures in the drain, on nearly every already-landed task at first preview ("Changed Python sources have related tests with no credited fresh passing pytest target"). Gate 12 rejected genuine historical REDs (#24554, #24270's nine) until Josh had it skipped. Gate 7 demanded an explicit SHA on multi-landing tasks; gate 8 demanded scope_justification on landed paths. About eight backlog items were fixes to the gates themselves; the host-harness e2e for #24617 was never built.
3. Authority was bound to sessions that no longer existed. landing_approval and activation accept only the creator or delegator session and never the claimant (close_receipts.py:78). Creators were ended pipeline sessions, so Josh was asked for landing approvals three times on SHAs already carrying both LANDs. 17164's workaround was about 130 claim_task calls and 41 escalate/de-escalate pairs: release, transfer_task_authority, landing_approval, reclaim, per landing. The Release Manager is the designated activation recorder (release-manager.yaml:23) but is normally neither creator nor delegator, so that hand-off probably fails; not verified at runtime.
4. Activation batched into restart windows. Restart-class landings waited for an Orchestrator-run quiet window with no live close reviewer: odd-hour two-hour windows from 08:34 Oct 19, quiet hours 04:45 to 06:45, protected memory-dream runs. #24721 measured about 2,700 task-minutes across 23 land-to-activation cycles. 17164 ran about 22 restart or cutover commands during the drain. The census counts five tasks blocked on an activation window.
5. Throttles idled the fleet. The 20-close cap, three test slots with two reserved, the heavy cap, close slots 1 to 3, and a load hold misread as "lift below 36" instead of 60 left 65 of 80 agents idle. 15977 compacted 30 to 35 times a day and did hands-on work itself (simulator captures, a 625 GB cache cleanup, research closes); its last delete_worktree was Oct 17 20:39. The list_tasks paging bug #24732 hid the backlog for about 11 hours.
6. Dependencies turned backlog into starvation. Josh's 08:41 Oct 19 rule that blockers must close before dependents unlock left 49 blocked leaves; eight of the 46 waited on another task's gate fix; the 35-ref #24604 chain stayed at 6 closed, 29 open through the whole drain.
7. Review loops multiplied landings. An INVALID gate 13 verdict (six confirmed in the drain) or a reviewer BOUNCE sent the task back through fix, new LAND pair, new landing approval, next restart, retest. 23 of 46 tasks landed more than once; #23808 ten times, #24652 eight, #23865 seven. A stale review-prompt fingerprint discarded a valid 11/11 verdict on #23229 after a description edit.
8. No closer. One task per seat plus the hand-off rule meant authors had moved on; five of 46 had no closer (#24589, #24435 among them). Close needs the owner's or a linked session's transcript, so an ended author degraded the evidence.

Census blocker categories (n=46): dependency on another gate fix 8, review bounce 6, activation window 5, no candidate 5, no closer 5, operator park 4, dirty worktree 3, gate 10 coverage 3, host-harness e2e 2, container 2, gate 12 RED 1, LM/RM release 1, push and CI 1.

### Q2. Why worktrees and branches outlived their tasks

1. No code path links land or close to the registry. land_commit writes a landing receipt and a message, then stops (_record_and_notify). commit_close clears session variables only. land_commit never calls mark_merged, so the hourly isolation cleanup (find_expired: status merged, cleanup_after passed, task closed and unclaimed) never fires for lane work. cleanup_successful_merge_artifacts is wired only to the dispatcher merge stage, which the seat flow does not use.
2. Deletion had no owner and no trigger. The orchestrator procedure (orchestrator.md:17) makes delete_worktree a manual step for per-task trees; seat trees are never deleted. 15977 made zero delete_worktree calls on Oct 18 and 19 while 20+ tasks closed. 17164 did the deletions by hand during the drain: 39 worktrees and 204 branch names lifetime, with 55 delete_worktree call cells.
3. Seat reuse made the registry wrong. git switch -c on a seat tree updates neither branch_name nor task_id; adopted rows carry task_id=None; most rows had no task link. Deletion trusted the stale branch (17164, Oct 20: it "either refuses or can delete wrong registered merged branch"). Observed refusals: "Not a valid object name refs/heads/lane-1-runbooks-1" (registry branch no longer in git), detached seats (lane-1-runbooks-16, -5, lane-4-rust-2, lane-5-hub-mode-2, lane-7-stability), lane-5-24584-receipt-sweep actually on lane-5-24352-call-tool with 13 dirty paths, and worktree_in_use against 17164's own stale seat bindings. Fixing this path took 13 commits, 7 landings, 12 approvals and 2 bounces (#24761, cb8efcefa0, b0053582a5, 7e0257619d).
4. Rebased and review-split landings broke the merge proof. Every merged check is git merge-base --is-ancestor on refs/heads/<branch>. land_commit preserves ancestry (fast-forward or merge parent), but a rebased, cherry-picked or review-split copy lands a different SHA and the original branch tip is never an ancestor, so delete refuses with branch_not_merged_into_target and receipts on the old SHA stop matching the linked commit. 17164's census: 301 branches, 182 ancestors of 0.5.0, 119 not. The 13 synthetic review-split branches 0.4.111 to 0.4.125 needed archive tags, a rule disable, and git branch -D at 09:27 Oct 20; eight trees were byte-equal to integration ancestors and still unprovable.
5. Multiplication. Multiple landings (23 of 46), per-close proof trees (17164 created eleven close-*-17164 trees and deleted them), seats stopped mid-task by the finish-and-stop, the Oct 19 crash and the ~/.gobby/files deletion (#24755) leaving dirty trees (#24746, #24756, #24529), orphan test processes (32 Node) and a provider launch folder keeping trees "live".
6. Guards refused, nobody reconciled. The guards correctly refuse dirty, unmerged and live trees. With no owner for the follow-up, "retention became accumulation" (17164, 07:23 Oct 20). The gclient sidebar lists DB rows, so branch deletion alone changed nothing Josh could see (08:50 screenshot: "still have all of these").

Counts: 63 trees at 12:22 Oct 19 to 27 now; 301 local branches to 152 after cleanup to 139 now; 16 cohort trees remain, 7 for closed tasks, 5 dirty or ahead; about 17 task branches survive with no worktree. Fast-forward landings leave no merge commit, so git alone undercounts landings.

Verdict on the four-cause hypothesis: reused worktrees with stale records, supported; rebased copies, direct; seats stopped mid-task, real but small (three named trees); no reconciliation after land and close, strongly supported. The hypothesis omits the structural cause: deletion had no trigger and no owner in code, and receipt and activation gating held trees open after close (the Archivist's inferred fifth cause).

### Q3. One step

Josh, 08:37 Oct 19: "I'm just confused why we can't land and close at the same time." 15977, 08:39: "make land and close one call"; 12:10: the daemon runs the rest of the pipeline itself, coordinators only handle exceptions. Nothing in the code map contradicts this; everything after LAND is messages and manual steps.

Decisions that must still reach Josh (Assistant 15976): the exact push SHA; discarding unique unlanded work; parks and wont_fix; acceptance-criteria changes; destructive-git overrides. Decisions that never should have: landing approvals for SHAs with both LANDs; activation timing against cron; load thresholds; test slots; queue-count questions.

## Proposal: landing as the trigger

One daemon-owned pipeline, started by the landing receipt. Each step is mechanical or lands in one named inbox.

1. Land. land_commit stays the single landing path and keeps ancestry (fast-forward or merge parent). Forbid landing a SHA that is not the reviewed tip; when base_update_required forces a rebase, the author's branch ref is reset to the rebased tip before landing, so the branch always ends as an ancestor.
2. Mark merged. On success land_commit resolves the source worktree from git (the tree whose HEAD or attached branch is the candidate), not from the registry row, and calls mark_merged. The registry row's branch_name is refreshed from git at that moment. This alone makes the existing hourly find_expired cleanup fire for lane work.
3. Credit validation from the integration branch. For a landed task, gate 10 credits one validation run on the main checkout at or after the landed SHA (LandedCandidateCredit already encodes this: candidate ancestor of tip, run HEAD between candidate and tip, clean checkout). The daemon runs that proof once per landing batch under the load policy. Drop the claimant-window and owner-transcript requirements for landed tasks; the evidence is the run, not a transcript.
4. Close as a daemon job. The daemon calls close_task as the system closer with the landing receipt as the gate 7 candidate, then the automatic gate 13 review. VALID closes; INVALID reopens into one inbox (the author seat if live, else a close-fix lane) with the task reclaimed automatically. Live: criteria become a post-activation check the daemon runs, not a coordinator verification.
5. Role-bound receipts. landing_approval for freeze, restart and overlap becomes a policy evaluation the daemon can make (freeze file, activation class, overlap set) with one operator decision only when the policy says so; the reviewer's independent_review_approval on the exact SHA is the human approval. activation is recorded by the daemon at startup for every landed SHA that is an ancestor of the running commit. No receipt depends on a creator or delegator session being alive.
6. Delete on close. commit_close queues verified deletion of the merged tree and branch: no live session, clean, branch an ancestor of 0.5.0. A refusal becomes a labeled finding on the task, never silence. Under the per-seat policy, delete the branch and reset the seat tree to 0.5.0; or adopt 15977's Oct 13 per-task trees, which is what makes "delete the worktree" a literal step.
7. Alarms (from the Archivist): landed with no close after N minutes; landed with no activation after the next restart; registry row whose task is closed, branch an ancestor, seat dead; a close event that carries tree and branch cleanup status.

Least mechanism. Three code changes carry most of the value: (a) land_commit marks the git-resolved worktree merged and triggers a daemon close attempt credited by LandedCandidateCredit; (b) receipt authority by role or by daemon startup instead of by creator/delegator session; (c) commit_close queues verified deletion and surfaces refusals. Two policy changes need no code: retire Lane Manager release and Release Manager GO as close gates, since code never checked them; per-task trees or branch-only cleanup for seat trees.

What this removes: ten hand-offs, the claim/release/transfer dance, landing approvals on double-LANDed SHAs, per-task restart scheduling, and the manual delete step. What stays human: the five Josh decisions, INVALID verdict repairs, and dirty or unmerged trees that a seat abandoned.

## Risks

- Daemon-run close drops the author-transcript evidence model. The credited run must be a real run on the landed tree; the scheduler for those runs meets the same load caps that idled the fleet. Mitigation: one proof per landing batch, not per task.
- Automatic deletion of a tree with uncommitted work destroys it. Keep the dirty and live refusals; make the refusal an owned finding with the task label.
- Gate 12 is hard-skipped today (#24760). Restoring it inside an automatic pipeline needs RED evidence captured at review time; close time is too late.
- Forbidding out-of-band rebases changes the reviewer workflow for review-split branches.
- Role-bound receipt authority widens who can approve restart-class landings; the freeze file and the activation class policy are the remaining guard.
- Evidence gaps: landing timestamps for most pre-takeover tasks were not extracted; reviewer round-trip counts are regex matches over message text; the Release Manager activation refusal is inferred from code, not observed; the Archivist's #24739 ledger conflict resolves by sequence (INVALID review 00:52 Oct 20, own fix, VALID close 03:11).

## Sources

- Task #24761 record (result ac1fe5aa); .gobby/reports/close-queue-2026-10-19.md; close-drain-17164-live.md; close-drain-17164-current.md; close-drain-17164-state.json.
- Transcripts: ORCH 15977 (timeline), 17164 Codex rollout 2026-10-18T17-00-36 (50,001 records; per-task table, 42 gate 10 failures, 130 claims, 55 delete cells, 314 apply_patch), Archivist 16016 and Assistant 15976 interviews (messages 92c31aa5, 0abb23e9).
- Memories eac4a674, a85a52a0, 9215aba2, c6eff715, 5731bb6a, 36fe666a, 1738c0cc, 73b5e14e, 114d86b6.
- Code: _lifecycle_close.py (gates, _TEMPORARILY_SKIP_TDD_EVIDENCE line 92), _lifecycle_close_finalization.py (commit_close), tasks/land_commit.py (land_candidate, _record_and_notify), close_receipts.py (record_close_receipt line 78), close_integration_credit.py (LandedCandidateCredit), worktrees/deletion.py and worktrees/git/_lifecycle.py (delete path, ancestry refusal), storage/worktrees.py (find_expired), runner_maintenance/isolation.py (hourly cleanup), build/controls.py (merge-stage cleanup), procedures orchestrator.md, code-reviewer.md, lane-manager.md, agents developer.yaml, release-manager.yaml.
- Policy: project_policy variable; installed orchestrator definition; rule orchestrator-close-drain-authority; .gobby/workflows/rules/project-operating-policy.yaml line 266.
- Census of the 46-task cohort (27 closed; waits, blockers, landing counts) and the live snapshot: 27 git worktrees, 139 local branches.
