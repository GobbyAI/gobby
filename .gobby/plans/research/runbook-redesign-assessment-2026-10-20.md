# Runbook redesign assessment: Josh's modifications against the post-mortem

Researcher gobby#17248, 2026-10-20. Research only. Companion to
`runbook-cohort-postmortem-2026-10-20.md`. Facts come from code reading today
(four Explore passes), task records, memories, and the Oct 5 note
`close-review-retirement-2026-10.md` (#23513).

## Josh's modifications (verbatim summary)

0. Every task gets its own worktree. No lane worktrees.
1. Admin runbook launches Assistant, Orchestrator, Archivist, Release Manager (new role), Triage. Only Assistant and Triage message the Orchestrator.
2. No Lane Managers. No old Release Manager.
3. No task-close-reviewer. Code Reviewer reviews.
4. No TDD gate; dead code goes. Then: no close gates at all. Code Reviewer and Dev Manager hold the gates.
5. DevTeam runbook: Test Writer, Developer, Code Reviewer (security folded in), Dev Manager (team comms, idle checks, final checks, merge or escalate to Triage, delete tree and branch, close).
6. Finish gobby build removal, then task states open, blocked, in_progress, closed, escalated; claims pass along the team.
7. New Release Manager schedules restarts and releases. A task closes when it builds and passes tests; no binary required.

Josh's framing rule for all of it: "the issue isn't the job, it's the enforcement."

## Verdict in one paragraph

Every modification maps onto something that already exists; the gaps are enforcement points and four authority rules in code. The design removes the three structural causes from the post-mortem (relay with no daemon-owned step, session-bound receipt authority, deletion with no owner) as long as the Dev Manager's chain is tool calls, not messages, and each "only X does Y" has a refusal. Two things need a decision before drafting: whether close keeps three cheap invariants or none, and whether the Dev Manager or the Code Reviewer runs `land_commit`.

## Per modification

### 0. Per-task worktrees

Exists:
- `spawn_agent(checkout_mode="worktree")` with a task already creates `task-{seq}-{slug}` (`agents/isolation_worktree.py:55-196`, `isolation_models.py:86`) and seeds the venv.
- `mark_merged` defaults to `cleanup_days=0`; the hourly `cleanup_expired_isolation_loop` deletes merged, unoccupied, closed-task trees (`storage/worktrees.py:631-731`, `runner_maintenance/isolation.py:175-267`).
- `delete_worktree` refuses live, dirty, and unmerged trees (`worktrees/deletion.py`, `occupancy.py:14-56`).

Gaps:
- `land_commit` never calls `mark_merged`, so the hourly loop never fires for landed work. One call in `_record_and_notify` on the git-resolved tree fixes it and gives the Dev Manager's delete a backstop.
- Nothing stops `git switch -c` inside a task tree; that is how lane trees drifted. A command-pattern rule refusing branch creation inside a registered worktree makes one tree = one branch = one task.
- Seats cannot hop trees cleanly: the session's cwd and the tree's `agent_session_id` are set at spawn, and a session may not claim a tree for itself unless already bound (`tests/agents/test_frozen_workspace.py:177`). Per-task trees therefore mean per-task team spawns into the tree, with the Dev Manager as the persistent seat, or the registry drifts again.

Cost at the real scale (about 20 tasks in flight at 80 agents): 2.2 GB per tree, 20 trees, which is today's footprint (24 trees, 55 GB). Rust tasks build cold in each tree because Cargo targets are per checkout (`cargo_target.py:53-84`). Josh ruled no heavy cap, so concurrent cold builds are bounded only by `CARGO_BUILD_JOBS` per build. If that ever loads the machine, the no-cap answer is a per-project Cargo target instead of per-checkout, since cargo's own lock then serializes builds; #23902 already owns the concurrent-build load question.

Memory conflict to resolve: a2f129b6 (per-task trees, Josh corrected drift 10-05 and 10-09) and be54b416 (never per task, one tree per lane) both stand as Josh's preference. Whichever this decision confirms, the other must be superseded or the next Orchestrator re-drifts.

### 1. Admin runbook and Triage

Exists: `team-coordination.yaml` launches archivist, log-monitor, metrics-monitor, release-manager, inbox-manager, recovery-steward, consolidator. `inbox-manager.yaml` is Triage in all but name (5-minute digest, urgent forward with wake). No runbook launches the Assistant or the Orchestrator; the runbooks guide has operator surfaces launch them.

Enforcement gap: `send_message_targets` limits the mode (parent, session, project), enforced by `scope-spawned-agent-send-message.yaml` through `send_message_target_allowed` (`workflows/condition_helpers_sessions.py:80-105`). Nothing resolves the receiving session's role, so any seat with `session` mode reaches the Orchestrator. Least mechanism: one helper returning the target session's agent type, next to the existing helper, and one before_tool rule with a role matrix (orchestrator accepts assistant and triage; dev-manager accepts its team; otherwise block, naming the Triage ref in the refusal). A project variable holding the Orchestrator ref would need no code but goes stale on relaunch, which is the stale-record failure again.

Risk: Triage must never sit on the close path. Dev Manager closes autonomously; Triage carries escalations and digests only. Otherwise it is the Lane Manager relay with a new name.

### 2. No Lane Managers, no old Release Manager

Authority that must move, in code:
- `release_task_claim` accepts only the Orchestrator or a lane-manager on the same lane (`_claim_release.py:21-48`). Claim passing inside a team needs the Dev Manager there, or hand-off by `claim_task(expected_owner=...)` without release (storage allows it, `storage/tasks/_transitions.py:204-299`; the MCP layer raises a conflict unless `force` or a delegated run, `_lifecycle_claim.py:31-200`).
- Heavy-run cap: the old RM granted it. Josh ruled no heavy cap (2026-10-20), so the grant role goes with the old RM and no seat inherits it.

Nothing in code checks the LM release or the RM GO today, so removing them removes only procedure text (`lane-manager.md:13`, `release-manager.yaml:22-23`, `orchestrator.md:7,10`, `inbox-manager.yaml:43`, `developer.yaml:681-690`).

### 3. No task-close-reviewer

Footprint (code reading): about 4,000 source lines that exist only for gate 13 (`_lifecycle_close_orchestration.py` 993, `storage/task_close_reviews.py` 963, `close_verdict.py` 430, `agentic_close_review.py` 318, `_lifecycle_review_gate.py` 250, `close_review_delivery.py` 221, `tasks/validation.py` 157, the YAML and procedure), about 7,100 test lines, migrations 424, 425, 444, and ten shared files that reference `TASK_CLOSE_REVIEWER_AGENT`.

What it uniquely did (Oct 5 note, memories 012dc49b, a7d66506): judged the stored criteria text literally. Its INVALIDs in the drain were mostly criteria-wording mismatches, which the Code Reviewer can do in the same pass if `code-reviewer.md` says "judge the stored validation criteria, then the diff" and the Orchestrator keeps criteria text current when a method changes.

### 4. No close gates

Gate 12 footprint: six modules, about 2,760 source lines (`tasks/tdd_evidence.py` 960, `tdd_python_evidence.py` 804, `tdd_pytest_provenance.py` 521, three smaller) and 5,100 test lines. Not dead with it: `workflows/claimed_task_extra_skills.py:16` imports `task_requires_tdd` and `TDD_SKILL` from `tdd_evidence` and must move or go; `tasks/tdd_paths.py` is also imported by the edit-time check; the `tdd-enforcement` edit-time rules and `workflows/tdd_paths.py` (about 1,000 lines with tests) are a separate check. With a Test Writer seat the team structure replaces the edit-time rule, so it can go in the same pass.

The thirteen gates today (`_lifecycle_close.py:160-951`): task_exists, session_context, repository_path, children_closed, criteria_present, changes_summary_present, linked_commits, task_scope, uncommitted_task_edits, validation_commands, acceptance_artifacts, tdd_evidence, close_review.

Concern, stated once: three of these are not evidence gates but data invariants that keep the task record true, and they cost nothing:
- children_closed (epics cannot close over open leaves);
- linked commit present, or a named no-work reason;
- no uncommitted task-attributed edits (the Dev Manager deletes the tree right after close; the delete refuses a dirty tree, so without this check the result is an orphan tree, the post-mortem's cause 5).

One check is missing and cheap: the linked commit is an ancestor of 0.5.0. The Oct audit found 31 tasks closed VALID whose commits never landed. Under "merge then close" this is the only line that proves the order held. If Josh still wants zero checks, that is his call; the Dev Manager procedure then carries all four.

Everything else (criteria text, summary, scope, validation runs, acceptance artifacts, TDD, review) moves to the Code Reviewer and Dev Manager as Josh says.

### 5. DevTeam runbook

Exists: `team-lane.yaml` launches developer, code-reviewer, security-reviewer, researcher, lane-manager into an existing tree. `lane-manager.md` already has the idle sweep (find inactive seats, resume stalled). Idle primitives: `stall_suspected` after 600 s without progress (`storage/agents/_liveness.py:14,58-63`), `seat_idle` flag set on readiness and cleared at turn start, `wait_for_coordination` on session statuses.

Missing: `test-writer` and `dev-manager` definitions (lane-manager is the nearest base for the latter). `code-reviewer.md` has no security steps; `security-reviewer.yaml` lists them (auth, secrets, sandbox egress, injection, hook/proxy/daemon trust) and they fold into the reviewer procedure as a path-conditioned checklist. Agree with folding: the security reviewer served every lane as one seat and was never the bottleneck or the catch.

Landing authority, the decision point: `land_candidate` (`tasks/land_commit.py:378-466`) refuses the current claimant, any former claimant, and any caller without its own `independent_review_approval` for the exact SHA. If the claim passes through the Dev Manager, it is a former claimant and is refused. Two consistent options:
- Code Reviewer lands (as today; it holds the receipt), Dev Manager closes and deletes. No authority change.
- Dev Manager lands: `land_candidate` accepts a dev-manager caller when the reviewer's receipt exists for the SHA. One rule change, plus `landing_approval` for restart and cutover class dropped (modification 7).

TDD without a gate: the Test Writer's tests-only commit precedes the Developer's commit in git. That is the durable, non-transcript RED evidence Josh asked for in #24498; the Code Reviewer verifies it by running the tests at the test commit. No gate code needed.

Idle in the post-mortem came from throttles and relays, not unnoticed stalls. The Dev Manager's idle check matters less than the hand-back: when a task closes, the per-task team ends and the Dev Manager asks Triage for the next task, or the team is persistent and the tree-hop problem in modification 0 returns.

### 6. States and gobby build removal

Today the stored column is `state_bucket` (correction 2026-10-20: an earlier version of this line said there was none). It is refreshed by SQL triggers on `closed_at`, `escalated_at`, `is_escalated` and on stage-row changes through `compute_task_state_bucket` (`crates/gcore/assets/schema/baseline.sql:1633-1711, 5334-5342`), and `remove-gobby-build` 6.1 (#23861, migration 471) already rewrites that function to closed, escalated or ready; it must take the five-state ruling instead (ready becomes open, in_progress from the claim with the trigger extended to `claimed_by_session_id`, blocked computed or added). In Python, `projected_task_state` (`tasks/state_semantics.py:67-113`) returns closed if `closed_at`, else escalated if `escalated_at`, else the stage row's state (ready, in_progress, needs_review, review_approved, done), else ready. Blocked is computed from dependencies, escalation, and the `awaiting-human-review` label (`:195`). Stage rows came only from gobby build.

Josh's five states map onto existing columns with no schema change: open (no claimant), in_progress (claimant set), closed (`closed_at`), escalated (`escalated_at`), blocked (derived). Least mechanism is to change the projection and the `state_bucket` mirror to ignore stage rows. That does not wait for the schema drop; it waits only for P3 (definitions drop the stage tools) so nothing writes stage rows the projection ignores.

Build removal status (plan `.gobby/plans/remove-gobby-build.md`, epic #23341 under lane #24317): P1 and 2.1 to 2.3 landed (CLI, MCP, routes, web views gone); open are 2.4 and 2.5 (#23850, #23851, unblocked), P3 (#23852 to #23856), P4 (#23857, #23858, #24062), P5 (#23859, #23860), P6 (#23861, migration 471 drops stage tables plus `allow_automation`, `unattended`, `checkout_mode`, `dispatch_failure_count`). Thirteen leaves in one serial chain; `src/gobby/build` 6,632 lines and `src/gobby/dispatch` 6,883 lines remain.

#23817 (Josh's Oct 8 state list for an elicitation session: open, ready, blocked, claimed, escalated, deferred with follow-up date, review states) is now stale; the Orchestrator should replace its list with the five above or close it as merged here. "Deferred with a follow-up date" and "parked" have no home in the five; parks today are labels.

### 7. New Release Manager, close without a binary

Activation is not a close gate in code. No gate checks an `activation` receipt or a running commit; the only enforcement is `land_candidate` requiring a `landing_approval` naming "restart" for restart and cutover classes (`land_commit.py:457-458`), plus procedure text in five files. Dropping that line and rewriting the procedures gives "builds and passes tests, closes". The `activation` receipt kind, `transfer_task_authority`, and the creator/delegator receipt rule (`close_receipts.py:131-136`, post-mortem Q1 cause 3) can go with it.

What loses its owner: `Live:` criteria, which the close reviewer deferred as `pending_external`. Options: the Release Manager's restart checklist runs the live checks for tasks closed since the previous restart, or Live criteria are dropped. Decide before the definitions are written.

## Daemon instability causes in task history

Same JSON listing, 18,750 active and closed tasks, title families, 2026-10-20.

| Family | Tasks | By month |
|---|---|---|
| GIL and event loop (gil, event loop, main thread, starvation) | 86 | Jun 23, Jul 7, Aug 18, Sep 22, Oct 10 |
| Cargo, build load, heavy cap, load holds | 49 | Jul 5, Aug 7, Sep 7, Oct 22 |
| Daemon instability titled (crash, hang, health timeout, RSS, watchdog) | 82 | Feb 15, Jul 15, Sep 10, Oct 14 |
| PostgreSQL and pools (all postgres work) | 156 | |
| localhost and dial | 8 | |
| pytest and test-hub load | 10 | |

Among the 82 daemon-instability tasks, descriptions cite: PostgreSQL pool or sync DB 12, test-hub pytest 12, code index 9, GIL or event loop 7, localhost dial 7, cargo 3.

What the measurements inside those tasks say:
- #23359 (py-spy, 2026-10-02): GIL held 36 to 76 percent per minute, 2 to 36 percent on the loop thread. Top holders: postgres_pool row materialization, transcript decode and persist, embeddings parse, codex toml load, ConfigSnapshot. Health p90 1.92 s; 5 of 62 probes over 2 s returned DAEMON_UNAVAILABLE. Recorded as an observed association, not a proven cause. The 2026-10-06 15:43 stall (#23698, #23718) has no stack; py-spy could not read CPython 3.14 and #23718 is parked.
- Gate code on the loop was one GIL holder: #21729 (28 MB transcript parse at close), #23801 (first close preview after restart, 183 s with the GIL held), #23279 (SRT log decode). Removing transcript evidence removes this class.
- #24752 (2026-10-19): READY to GO waits of 607 s and 819 s for commands that ran 1.3 s and 1.7 s; the one-second CPU sample was 61 percent idle at load average 48; 57 per-worktree Cargo targets, 492 GB; CARGO_BUILD_JOBS=3 on 18 cores; nextest has no test-threads cap, so three runs can request 54 slots. Finding: admission delay, with CPU saturation unproven.
- #23902 (Josh-directed cargo efficiency research, 2026-10-09) is open and claimed; its report file does not exist yet.

Reading: the GIL family is the largest named cause and it is sync work on the loop (DB rows, transcripts, config loads), not thread contention. The cargo spike is October's and tracks the cohort; the one measurement found the queue, not the compiler. With the heavy cap gone, the one oversubscription risk left is nextest's default thread count; `test-threads` in `.config/nextest.toml` bounds it with no code. Per-task worktrees add no code-index load: Josh confirmed on 2026-10-20 that the per-tree index pass was fixed and worktree spawn now only creates a snapshot. #23433 (35 to 72 s per spawn) still shows open in the active list and is probably stale.

macOS build-host checks (2026-10-20, this machine): 18 CPUs (6 super, 12 performance), 128 GB. Cargo targets are already per checkout, so the build-directory lock only bites nested cargo in one tree (#23769, fixed by the nextest setup-script prebuild). Open-file limits are moot: gdaemon (`crates/gdaemon/src/serve.rs`, target 10,240), the Python runner and gterm raise the soft limit at start and workers inherit it. Spotlight indexes nothing under `~/.gobby` (dot directory, 0 items against 6,830 under the project) and no Time Machine destination is configured, so the #24752 Spotlight/Time Machine row closes. Gatekeeper stalls are on record (#22067, memory a379ac1a); `DevToolsSecurity` now reads enabled where #24752 recorded disabled, and that flag is distinct from the per-app Developer Tools permission. `Cargo.toml` has no dev/test debug override; `line-tables-only` is the unmeasured cheap experiment. With `CARGO_BUILD_JOBS=3`, three builds are 9 compile jobs; the oversubscription path is nextest's default 18 threads per run. At 20 tasks in flight no per-run `-j` fits 18 cores, so #23902 should measure N concurrent cold builds against one shared target per project.

## Enforcement table

| Rule in the design | Enforced by | Status |
|---|---|---|
| Only Assistant and Triage message the Orchestrator | new helper + before_tool rule on send_message | gap |
| Team seats message only the Dev Manager | same rule, role matrix | gap |
| Only Dev Manager merges, deletes, closes | `blocked_mcp_tools` and step `allowed_mcp_tools` in the new definitions; `land_candidate` role rule if DM lands | definitions not written |
| Only Code Reviewer approves | `independent_review_approval` refused from claimants and former claimants (`close_receipts.py:123-127`) | exists |
| Only Release Manager restarts | policy text today | needs a command-pattern rule by role |
| One tree per task, one branch per tree | `create_worktree` refuses a second tree per branch; nothing refuses `git switch -c` | half |
| Merge before close | nothing (31 closed-unlanded in Oct) | ancestor check in close, or procedure only |
| Merged trees get deleted | hourly loop, if `mark_merged` is called | `land_commit` never calls it |
| Claims pass along the team | `claim_task(expected_owner)` at storage; MCP conflict unless force; release restricted to ORCH/LM | small code change |

## Order that avoids blocking on the build chain

1. Definitions, procedures, rules, pipelines (no schema): dev-manager, test-writer, code-reviewer with security and criteria steps, Admin and DevTeam pipelines, the send_message role rule, the branch-creation rule, `_can_release` and `land_candidate` authority, `land_commit` calling `mark_merged`, drop the restart approval line.
2. Close: delete gates 10 to 13 and their storage; keep or drop the three invariants and the ancestor check per Josh's ruling.
3. States: projection to the five states after P3; the rest of #23341 (P4 to P6) continues in its lane without blocking the fleet.

## Decisions only Josh can make

1. Zero close checks, or the three invariants plus the ancestor check.
2. Who runs `land_commit`: Dev Manager or Code Reviewer.
3. Per-task team spawns (Dev Manager persistent) versus persistent teams hopping trees.
4. Owner of `Live:` criteria after close, or drop them.
5. Keep or drop the edit-time `tdd-enforcement` rules with gate 12.
6. Which of the two worktree memories is superseded.

Ruled already (2026-10-20): no heavy cap.

Rulings 2026-10-20 12:40 CDT (Josh via the Assistant, message from gobby#15976):

1. Three invariants plus the ancestor check stay, including the pushback on the cheap ones.
2. Dev Manager lands and closes. Researcher pushback sent: the branch must be current with 0.5.0 before final checks so the landed tree is the tested tree; the Dev Manager cannot delete the tree it stands in (`delete_worktree` refuses live trees), so `mark_merged` inside `land_commit` is the deletion path and the hourly loop finishes it after the tab ends; `land_candidate` must accept the dev-manager role when the reviewer's receipt exists for the SHA. Josh: implement `mark_merged` on `land_commit`.
3. Teams are ephemeral: the tab ends when the task closes. Memory writes and the close summary must precede close.
4. `Live:` criteria: Josh did not know them. Facts sent: a criterion line starting `Live:` (`criteria_contract.py:11`) is deferred as `pending_external` by the close reviewer for a coordinator to verify after restart; no role definition owns that step; 22 tasks ever carried one (21 closed; 19 created in September, 3 in October; the open one is the Oct 16 hub-to-node proof). Ruled (Josh in the pane, about 13:45 CDT): drop the `Live:` prefix; the Release Manager keeps a fixed post-restart smoke list.
5. A DevTeam always does TDD; with a Test Writer there is no TDD check. The `tdd-enforcement` edit-time rules go with gate 12.
6. Lanes do not exist after this; one worktree per task. The lane worktree memory is the superseded one.

Also ruled: Triage handles merge conflicts and escalations only; Dev Manager's chain approved; code refusal rules approved; Code Reviewer stays on reviews (reviewer-fixes question answered: a reviewer who edits becomes a claimant and `close_receipts.py:123-127` refuses its approval, so the Dev Manager swaps roles and a fresh reviewer spawns, or escalates). Ruled about 13:45 CDT: review loop is developer, review, developer fixes, second review by a reviewer the Dev Manager spawns with a stronger model, and on the third failed review the Dev Manager escalates to Triage; if the reviewer wrote the fix, the Dev Manager spawns a reviewer for the reviewer's code. Mechanism already present: `spawn_agent` takes `provider`, `model` and `reasoning_effort` per spawn (`mcp_proxy/tools/spawn_agent/_factory.py:352-361`), definitions may carry `model` and `reasoning_effort`, and `project_policy.launch_defaults.team-lane` already names `reviewer_model` claude-opus-5-5 at xhigh; a round-two model is one more launch-defaults entry, no duplicate definition. Gap: no daemon record of a rejected review exists (receipt kinds are approval, activation, landing_approval, landing; `round_number` lives only in the stage-review transitions that leave with gobby build), so the three-fail count needs a per-round verdict receipt if it is to be enforced rather than counted in the Dev Manager's transcript. Josh asked (about 14:00 CDT) for a review count on the task data model. Finding: `tasks` already carries `validation_status` (pending, valid, invalid, error), `validation_feedback` and `validation_fail_count integer DEFAULT 0` (`crates/gcore/assets/schema/baseline.sql:3885-3890`). Status and feedback are written only by gate 13's close finalization (`_lifecycle_close_finalization.py:443-444`); the fail count is incremented by `increment_validation_failure` (`storage/tasks/_transitions.py:573-642`), which also sets status and feedback and escalates atomically at a threshold (default 5), called from `account_criteria_verdict` (`_lifecycle_validation.py:427`) on the gate-13 path; resets are the operator CLI (`cli/tasks/_crud_detail.py:84`) and `build/restart_controls.py:34`. Correction 2026-10-20 (planner gobby#17257 caught it): the first version of this paragraph said the count was never incremented. All three columns go idle when gate 13 is deleted; the atomic incrementer is the piece to reuse at threshold 3, noting that at threshold it NULLs the claim and sets `is_escalated`, and refuses further increments while escalated. Proposal: the reviewer records each verdict through `record_close_receipt` (kinds today: activation, independent_review_approval, landing_approval; stored as `task_comments` rows with verdict LAND), adding one kind `independent_review_rejection`; the same transaction sets status to the verdict, feedback to the rejection reason, and increments the fail count. No migration if the names stay; rename to `review_*` in migration 471, which already drops four task columns. A rule at count 3 refuses a fourth reviewer spawn until `escalate_task`. Ruled yes (Josh, about 14:05 CDT): rejection receipt kind, daemon-written status, feedback and fail count in the receipt transaction, and the rename to `review_*` in migration 471. Collision check per memory 09ad0f77: no ref among 164 local and remote refs has ever added a migration numbered 471 or higher (`git log --all --diff-filter=A` on the migrations path), so 471 is free today; recheck when the migration is written. Rechecked 21:55 CDT after the cutover: 471 is now taken by `471_code_overlay_pins.sql` (#23831, landed tonight; live schema 471 per Orchestrator 17164's DAEMON BACK), so #23861's drop migration moves to 472 and the `review_*` rename and state-bucket change follow it at 473 or later; Josh's ruling stands with the number moved.; one Cargo target per project named `cargo-target-<project_name>` (drops the checkout level of `cargo_target.py:53-84`; builds serialize on cargo's build-dir lock during compile, dependencies compile once, alternating branches recompile workspace crates). Orchestrator on merge keep/toss: informed of outcomes, not the decider; semantic conflicts go to Josh via the Assistant. Ruled (Josh via planner gobby#17257, afternoon 2026-10-20, superseding the quiet-fleet rule): restarts and binary releases need no work stoppage; only `land_commit` waits during the Release Manager's window, so the no-live-worker, drain and park requirements leave AGENTS.md rules 4 and 12 and the seat and project policies, while the global announcement, new-inode coherent-set promotion, schema preflight and health verification stay. Code support checked 2026-10-20: close reviews persist across daemon restarts, and `_reconcile_task_close_reviews` (`runner_lifecycle_agents.py:164-330`) at startup restores a queued-but-unlaunched review, finishes an unbound launch as a `retryable_infrastructure` error for the caller to retry, re-subscribes the caller to a reviewer run still in progress, and enforces the durable deadline, which keeps counting through the outage (1200 s default, `close_review_validator_timeout_seconds`). Refinements (Josh via planner gobby#17257, evening 2026-10-20): a release window blocks landings and new DevTeam launches; existing teams keep working through testing, build, promotion and restart and may still replace a reviewer or recover; a successful close collapses every seat of the single-use DevTeam, commits, history and lessons survive, safe worktree cleanup follows, and teams are never recycled. Same evening Josh retired lanes entirely (Orchestrator 17164 relay): no lane tabs, no lane staffing limits, one developer per prepared task tree. Ruled (Josh in the pane, about 21:50 CDT): Triage also handles comms; agents message Triage, Triage synthesizes and updates the Orchestrator on an interval so it is not peppered with wake messages; the only agents that message the Orchestrator are the Assistant and Triage.

## Open tasks that exist only because of the gates

Source: `gobby tasks list --active --limit 1000 --json` on 2026-10-20, 530 active tasks, matched on full title and description, then read by hand. 42 are gate or evidence work; 20 of those are transcript-evidence work specifically. All of them retire with the gates.

Gate 10, transcript validation credit (12): #23868, #24636, #24497, #24538 (epic), #24546, #24555, #24556, #24552, #24085, #23370, #21528, #24617.

Gate 12, TDD RED evidence (5): #24494, #24565, #24746, #24748, #24498.

Gate 11, acceptance artifacts (1): #24541.

Gates 8 and 9, scope and attribution (8): #24590, #23561, #23568, #23644, #23645, #23640, #24613, #24168. These are hook-event attribution, not transcripts; the first three stay relevant only if close keeps the uncommitted-edits invariant.

Gate 13, close reviewer (7): #24295, #24727, #24723, #24728, #24736, #24509, #23438.

Receipts and landing gates (7): #24646, #24197, #24403, #24756, #23542, #18784, #21972.

gcode evidence index for close (2): #24029, #24731.

Closed tasks (same classifier over `--closed`, 18,220 closed tasks): 344 match gate-specific title terms (gate N, close gate, close review, receipts, land_commit, validation commands, acceptance artifacts, gcode evidence); 427 when titles naming TDD are included, which also catches the early-2026 TDD-sandwich expansion tasks; a further 438 mention gate failures only in their descriptions and were not read by hand. By creation month the 427 run 53, 8, 13, 4, 12, 8, 32, 50, 117, 130 from January to October 2026, so September and October hold 247 of them and October's 130 came in twenty days. With the 42 active, gate and evidence work is roughly 390 to 470 tasks by title alone.

What replaces transcript evidence in the DevTeam design: git order (test commit before implementation commit), the Code Reviewer's own run of the tests at both commits, and the Dev Manager's own build and test run in the task tree before merge. None of these parse another session's transcript.
