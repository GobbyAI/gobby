## Research Findings

### Research Questions

- What dependency- and file-overlap-based completion sequence covers the open work named in #25237?
- Where can hub-node live testing begin, what blocks Josh-ready parity, and which proposed gates conflict with the current graph?
- Which tasks are excluded, outside their expected 1.0 owner roots, stale, or insufficiently specified?

### Domain Context

Research spike: #25237 — Research: stepwise path from today to Gobby 1.0 completion. Snapshot: 2026-10-27 CT / 2026-10-28 UTC. Repository HEAD: `6ccbe174b692dfaf43ceedefa249f8be1033f938`. This is research evidence and a proposed operating reference; it is not an approved implementation plan.

**Result:** the current open-task graph admits an acyclic completion order. The conservative envelope below places **490 current tasks exactly once in 314 steps**, including four reserved, uncreated verification outcomes from #25236. It contains 349 tasks under #21542, 79 under #24818, 18 under #24820, 8 under #24821, 5 under #24822, 30 under the four explicitly queued external epics, and prerequisite #23603. There are 134 exclusions. The broad envelope preserves the brief's named roots; it does not establish that every unrelated root follow-up is a 1.0 shipping requirement.

**The early parity milestone remains conditional.** Requiring the entire #21571 operator-CLI task before hub-node readiness contradicts its whole-Stage-2 prerequisite. Parenting final readiness below #24593 would additionally create a cycle if that whole-task requirement is retained. The recommended repair is an independently completable node-CLI milestone slice, plus a final verification owner that does not block the hub-mode gate consumed by Cron. No tasks or edges were changed.

**Operator order:** finish and clean the eight existing teams first. Immediately after step #1, admit the six requested epic DevTeams: #23561, #23220, #22643, #22948, #24820, #24821. Their root task rows below are completion points after their children; they are not instructions to delay admission until that row. Continue admitting eligible hub-node work while those teams run. Runbooks preparation has scheduling priority, then the hub-node dependency closure, then the remaining port. Secondary machines may take noncritical work.

**Meaning of a step:** every task in the step completes, lands where applicable, closes through its authorized owner, and releases its task resources before the next step starts. Within a parallel set, implementation can overlap; integrations touching shared `Cargo.lock`, binary manifests, or version pins use a serial landing queue. Those release carriers were excluded from the production-source collision test, not declared disjoint. Step #1 preserves already-running concurrency. Subsequent sets have disjoint known production Targets; unknown inventories receive singleton steps. This conservative order is not a duration estimate or a claim of maximum throughput.

**Epics and expansion:** phase, stage and domain rows are completion gates. An unexpanded epic or an epic with residual obligations still owns all its criteria; it cannot be closed merely because its currently visible children finished. Planning/expansion that introduces leaves requires refreshing this reference. The sequence therefore gives a bounded current-ID ledger, not a pretense that unexpanded work has already been specified.

**Concurrency cap:** each step's width is an upper bound. The largest logical width is 23, including closure gates; runnable work can be fewer. New admissions are capped at **zero** after two consecutive five-minute loads over 60. Lift that hold after two consecutive readings below 36; equality and one reading do not change it. Outside a hold, admit at most the step's runnable width and the existing fleet's available capacity, with the six requested epic teams accounted for. This report introduces no new fixed fleet cap, admission threshold, or build limit. No host-load measurement was performed.

**Storage and review:** this Markdown report is the committed reference under `.gobby/plans/research/`. Per the Orchestrator’s ruling relayed by Triage, the evidence companions remain in this researcher’s agent workspace and are cited below; they are not committed or copied into the checkout. This promotion preserves the dated task snapshot; it is not a fresh inventory of live open work. The current task brief adds #25238 to the first drain step, so this revision moves it from former step #2 to step #1 and renumbers the remaining steps. Review and task closure remain with the authorized reviewer/manager or Orchestrator. Per memory `a5ae6d45`, only Orchestrator, Archivist and Assistant receive a one-line path/when-to-load reference; shared startup injection must not contain this report.

### Evidence & Sources

The complete evidence remains in the agent workspace at the literal paths below. Both the original dependency/target JSON and a lossless deterministic gzip copy are retained. These machine-local companions are not portable Git artifacts; preserve or relocate them through the owning coordinator before retiring this workspace.


**Primary evidence and method**

- `gobby-tasks:get_task(#25237)` supplied scope, criteria, the initial seven drain tasks, roots and named memories; its updated brief adds #25238 as the eighth drain task. `list_tasks(closed=false, view=compact)` followed every returned `next_offset`: 624 rows, 624 unique refs. The raw read snapshot is [25237-open-task-snapshot.json](/Users/josh/.gobby/agents_workspaces/d45545c5-ded5-4335-b115-0245752edacf/researcher/session-18257/25237-open-task-snapshot.json).
- 330 detailed task cards supplemented the compact snapshot; oversized cards' dependency tails were retrieved through `gobby-results:get_tool_result`. Both `blocked_by` and reverse `blocking` edges contributed. The selected graph has 685 explicit open dependency edges; hierarchy completion edges supplement them.
- Task Targets and 580 repository plan-section inventories supplied overlap evidence. Exact task titles or explicit plan/section references matched owners. 259 selected tasks have a resolved inventory entry, 231 of those nonempty. Missing inventories are not evidence of independent work.
- [25237-roadmap-evidence.json](/Users/josh/.gobby/agents_workspaces/d45545c5-ded5-4335-b115-0245752edacf/researcher/session-18257/25237-roadmap-evidence.json) records every selected task, its explicit and scheduled prerequisites, Targets and source, step, deferred overlap candidates, proposed edges and exclusions. It is the reproducible audit companion. A byte-identical JSON payload is retained as [25237-roadmap-evidence.json.gz](/Users/josh/.gobby/agents_workspaces/d45545c5-ded5-4335-b115-0245752edacf/researcher/session-18257/25237-roadmap-evidence.json.gz).
- Named decision memories fetched: `c3fa8eb8`, `9bf4bb37`, `217e0dc7`, `3ba20ade`, `0962f52e`; additional reference-visibility ruling `a5ae6d45`. `project_policy` supplied roots, role guidance, integration branch and launch defaults.
- `ROADMAP.md:118` describes the mode boundary; `ROADMAP.md:525` is decision 17, thin nodes with datastores staying on the hub. #17436 and #21575 task cards provide the current node and mode-gate interpretation. `docs/contracts/plan-coverage.md` defines target-overlap and expansion obligations.
- #25236 researcher message `5d6a9d5b-9d73-4db8-8398-144ce18478c6` supplies four proposed outcomes, eleven existing-task folds and the gate notes. Its peer-session file could not be read because `agent-workspace-read-scope` refused it. This report uses the complete durable message, not an assumed reading of the file.

**Dependency findings and proposed repairs**

1. **No existing selected-graph cycle was found.** All 490 tasks can be ordered with explicit prerequisites and child-before-parent completion. Closed prerequisites are treated as already satisfied. No dependency on the excluded #22978 subtree or #22225 is required by the selected current graph.
2. **Final-readiness composition can create a cycle.** If N1 is a child of #24593 and requires all of #21571, the wait chain is N1 → #21571 → #21544 → Cron/#25025 → #21575 → #24593 → N1. These arrows mean “waits for.” #25236 itself calls for reconciling the whole-Stage-2 gate for the node CLI subset. Keep the early #24604 transport proof under #24593; give the node CLI slice an independent completion boundary and keep final acceptance from blocking the hub-mode dependency needed by the port. Do not insert this hypothetical cycle into the DB.
3. **Node-local Skills currently pulls a substantial hub port into the milestone.** N2 requires #24994. Current edges continue through #24993, #24992, #24884, #24151, #21567, #21564/#24149, Tasks #21560 and runtime/config #21559. The proposed sequence honors this real chain; it does not pretend all node parity is available immediately after transport works. If the intended Python-hub milestone should precede that chain, an approved node-filesystem vertical slice with compatible Python-hub dispatch must replace the broad prerequisite. That is a task-boundary proposal, not evidence the slice already exists.
4. **Preserve the installer/proof direction.** #24657 currently waits for #24604; #24863 also waits for #24604. Making all of #24657 a prerequisite of #24604 would cycle. Early testing uses an isolated, explicitly enrolled pair. The final readiness packet separately requires Rust-only installation, update and recovery evidence.
5. **#24603's present criteria deliberately stop at warning toasts.** Its 4.8.4 criteria warn and start no shell/request for remote worktree open/new/open-dialog actions. That can satisfy the task while failing node parity. Fold concrete machine-correct actions into the existing owner before treating its completion as Josh-ready evidence; no drawn action may remain inert behind a warning.
6. **#25201 needs a readiness/activation gate.** It checks whether #24571's Rust write coordinator retains the node-remote write branch. The task currently waits on #24571 and blocks no consumer. Verify or repair it before accepting #24580 Native activation and #25217's remote-input proof. Its completion is included in the milestone prerequisite closure; proposed consumer edges need owner review.
7. **Nested task launch prerequisite:** #21560 waits for #21559, but nested #20822 has no open leaf blocker in the read card. The proposed order adds #20822 after #21559 so the authority-matrix implementation does not bypass the family ordering. The JSON labels this as a proposal, not a stored edge.
8. **Research report closure:** the original live rule denied researcher `claim_task`, while checkout writes required a claim. #25238 repaired that ownership flow and is now included in the first drain step, as required by the updated task brief. After its activation, this researcher successfully claimed #25237 to promote the already-delivered report. The proposed #25236/#25237 completion prerequisites still follow #25238. No product task or dependency was edited.
9. **Runbooks is not a whole-root barrier.** #24887 explicitly waits for #24604. Requiring every #24818 task to finish before any hub-node work would contradict this edge. The prep set prioritizes v4 launch, ownership, sandbox, review and lifecycle stability; later root follow-ups remain listed after the milestone.
10. **External ownership:** #25110 explicitly requires #23603 under #23591. Include that leaf, whose only blocker #23597 is closed, without pulling the whole spend/ledger epic into 1.0. #22643, #22948, #23220 and #23561 also lie outside the five roots but are explicitly authorized by the six-epic launch ruling. No task was reparented.

**Stale or potentially obsolete open work**

- #21546 still describes the retired transitional Python node and datastore tunnel. #17436 and ROADMAP decision 17 supersede that wording. Refresh its description before using it as an implementation brief.
- #23561 explicitly warns that its old leaf plans target v4-deleted close modules, and requires refreshing the plan before more work. #23566/#23567/#23569 need that owner review rather than blind replay. #22643/#23669 similarly require a staleness pass because parts already ship. Their completion rows preserve that remaining responsibility.
- #25132 still owns Reporter source headings while #25172 retires Reporter. Inspect the remaining consumers before deciding whether to repair, fold or retire it. #25146/#25179/#25218/#25238 and #24794/#25223/#25238 have overlapping workspace/research-flow obligations; current task ownership must prevent duplicate implementations.
- #19652 archives research, #22143 focused speech, #22340 messaging, #22946 rule-evaluation architecture, #25196 ECC research, and the #24822 Annotate subtree have no demonstrated dependency into the hub-node milestone in this snapshot. They remain in the broad named-root envelope, with secondary scheduling. Orchestrator should obtain a scope ruling through Assistant before turning them into mandatory 1.0 shipping gates. Their presence does not block #21542's own completion row.

**Hub-node testing slot from #25236**

Early transport proof is #24604 at step #94; node-origin checkout reads #24532 follow at step #95. These are capability tests, not the full parity milestone. Rust-only installer completion #24657 is step #108.

The reserved steps below sit after the current dependency closure for install/update, node duties, remote terminals, code-index parity/cleanup, node-local Skills and explicit operator delegation, and before the remaining scheduled port. They are conditional on the CLI-slice and ownership repairs above. The four outcomes are proposals with no task IDs; the Orchestrator owns filing and final placement.

- N2: shared state, sessions, tasks, memory and Skills live parity; depends on #24604, #24532 and #24994. Use real production entrypoints, isolated state, owner/project isolation, and node evidence. Python hub semantics may remain until their port activates; rerun then.
- N3: orchestration, messaging, rules and recovery live parity; depends on #24604, #24532, #25217 and runbooks v4 #24797/#24824, plus operational recovery evidence. Use real-provider runbooks/pipelines, cross-machine messages/wakes and machine-local effects.
- N4: paired standalone/hub/node latency evidence; depends on N2/N3, #24604 and #25217, plus installer evidence for install/update timings. Budgets are proposals requiring approval; correctness and recovery deadlines also matter. Keep raw redacted measurements and comparisons.
- N1: inventory, exact operator smoke checklist and readiness packet; depends on N2/N3/N4, #24604, #25217, #24878 and all required workflow folds. Scaffold early; accept only after execution. Require no missing, broken or untested required workflow, all five real providers, Rust-only node installation with no datastore credentials, and approved latency results.

Folds remain with existing owners: #24604 node-origin provider/lifecycle proof; #24521/#24532 disk-read evidence; #20202/#25217 remote attach/input/resize/drafts/recovery; #24597/#24601/#24603 workspace actions; #21566/#23275 MCP; #24994/#25000/#25001 local Skills/scripts; #24657/#24859–#24878 installer; #21571 node CLI slice; #24872/#24875/#25217 startup/update/recovery; #24661/#24662/#24664/#24668 code-index parity and cleanup; #21579 platform evidence. Windows scope and latency budgets go through Assistant to Josh; they are not approved policy.

**Numbered completion sequence**

Each task appears once in this sequence. `waits` lists explicit open prerequisites, plus child completion obligations and the three marked proposal edges. A missing target inventory means singleton scheduling, not an assertion about the actual files. Detailed Targets and every held overlap are in the JSON companion. Steps containing only phase/domain completion gates can have zero implementation width.

**Step #1 — width 8; implementation/task-outcome width 8**

- #25218 — Block every agent from provider temp scratchpads; writes go to its assigned workspace under ~/.gobby/; no open prerequisite in the selected graph; singleton because target inventory is unbounded.
- #25220 — Cold start: Orchestrator opens the Assistant beside it, then a Support tab with Triage, Archivist, Security Reviewer, Release Manager, Recovery Steward and Consolidator; no open prerequisite in the selected graph; singleton because target inventory is unbounded.
- #25223 — Research runbook (Researcher + Research Reviewer); retire the older 'planning' pipeline; no open prerequisite in the selected graph; singleton because target inventory is unbounded.
- #25224 — Team teardown leaves epic leaf sub-worktrees, seats and branches behind, and runs before the team's seats end; no open prerequisite in the selected graph; singleton because target inventory is unbounded.
- #25232 — Interpreter-script writes lock a seat out of its own files: attribute a seat's script writes in its own task worktree; no open prerequisite in the selected graph; singleton because target inventory is unbounded.
- #25233 — Seat and startup fixes: Dev Manager seat roster, Release Manager batch path, non-blocking model-metadata fetch, query_readonly reference, sidebar sandbox lock placement, tab-opening seat pane title; no open prerequisite in the selected graph; singleton because target inventory is unbounded.
- #25235 — Consolidator applies approved folds: update_task and no-work superseded/moot closes; no open prerequisite in the selected graph; singleton because target inventory is unbounded.
- #25238 — Researcher owns its report task: create it, write the report, close it after Orchestrator review; no open prerequisite in the selected graph; singleton because target inventory is unbounded.
- Ordering: complete the eight already-running teams, including landing, closure and cleanup, before the six epic admissions.

**Step #2 — width 2; implementation/task-outcome width 2**

- #25236 — Research: task list to test hub-node live (node parity with hub and standalone); waits: #25238.
- #25237 — Research: stepwise path from today to Gobby 1.0 completion; waits: #25238.

**Step #3 — width 1; implementation/task-outcome width 1**

- #24780 — CI, Rust CI and Release Branch CI fail on every 0.5.0 push: tests/agents pytest failures, gcode projection_stale test, torch wheel, Rust E0599; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #4 — width 1; implementation/task-outcome width 1**

- #24794 — Research-spike closes require a commit that researchers in the main checkout cannot make; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #5 — width 1; implementation/task-outcome width 1**

- #25081 — Post-cutover verification of the #24824 DevTeam addendum: live pilot 8.6.4, installed-row parity, six-trigger workspace history; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #6 — width 1; implementation/task-outcome width 1**

- #25129 — Runbooks end every seat they started when the task closes, including fresh reviewers and planning seats; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #7 — width 1; implementation/task-outcome width 1**

- #25131 — Direct wakes to an idle Orchestrator composer are declined, so operator relays stall; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #8 — width 1; implementation/task-outcome width 1**

- #25133 — Landed project rules never reach the DB: cutover and gobby sync skip .gobby/workflows/rules; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #9 — width 1; implementation/task-outcome width 1**

- #25139 — A devteam whose first seat fails to spawn can't be relaunched: team_id needs a tab that was never created, and a fresh admit refuses the existing tree; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #10 — width 1; implementation/task-outcome width 1**

- #25145 — Isolated test daemons miss the 75 s health deadline at moderate load, so contract and e2e gates flake; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #11 — width 1; implementation/task-outcome width 1**

- #25146 — Agent workspace: pre-addendum seats get no binding, operator attachments refuse workspace files, and seat write-scope messages still say docs/ only; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #12 — width 1; implementation/task-outcome width 1**

- #25151 — Sandbox grant lifecycle: /clear successors and resumed seats miss write grants computed at spawn; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #13 — width 1; implementation/task-outcome width 1**

- #25152 — Sandboxed seats cannot run Rust builds: xcrun and clang temp and cache writes under the per-user TMPDIR are denied; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #14 — width 1; implementation/task-outcome width 1**

- #25172 — Rewrite agent definitions to the seat-Triage-Orchestrator topology, honor standing rulings, and remove reporter.yaml; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #15 — width 1; implementation/task-outcome width 1**

- #25173 — Exclude class-3 rules from the Orchestrator definition, per Josh's confirmed rule-class table; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #16 — width 1; implementation/task-outcome width 1**

- #25177 — Fixer and developer seats record TDD RED evidence in step records before implementing; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #17 — width 1; implementation/task-outcome width 1**

- #25178 — Simplify seat startup: drop duplicate required loads, move gclient to its event, list all owed loads in one refusal; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #18 — width 1; implementation/task-outcome width 1**

- #25179 — Agent workspaces: named paths, own read/write for every seat, Assistant and Orchestrator read all, Assistant loses docs/ write; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #19 — width 1; implementation/task-outcome width 1**

- #25183 — gclient.md reference covers the gclient CLI as well as MCP placement; waits: #25178; singleton because target inventory is unbounded.

**Step #20 — width 1; implementation/task-outcome width 1**

- #25198 — Epic review lets ready leaves land while a sibling is stuck or unstarted; only dependencies order landing; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #21 — width 1; implementation/task-outcome width 1**

- #25200 — Dev Manager roster names Triage as the Orchestrator, so direct Orchestrator rulings are held; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #22 — width 1; implementation/task-outcome width 1**

- #25199 — Rust test drift on 0.5.0: gcode privilege manifest misses calls and relations, and gcore public-boundary test expects a stale postgres feature line; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #23 — width 1; implementation/task-outcome width 1**

- #25228 — Orchestrator checks Triage when idle instead of waiting on it; Triage sends to the Orchestrator with wake=true; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #24 — width 1; implementation/task-outcome width 1**

- #25234 — Restarts and releases need only a heads-up to agents, no quiet-window wait; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #25 — width 13; implementation/task-outcome width 13**

- #24452 — Capability-token verification; no open prerequisite in the selected graph.
- #24571 — Python write client, Native composer ledger, and Native write wiring; no open prerequisite in the selected graph.
- #24572 — Native reconcile settler and the host exit proof; no open prerequisite in the selected graph.
- #24356 — The Python wrapper and its setting are gone; no open prerequisite in the selected graph.
- #24359 — `gmcp` is a published managed binary; no open prerequisite in the selected graph.
- #24674 — Overlay-mode index plan and apply; no open prerequisite in the selected graph.
- #24375 — Provider launch, exits and host reconciliation; no open prerequisite in the selected graph.
- #25207 — `terminal.relay` and `terminal.resize` in node-commands contract version 5; no open prerequisite in the selected graph.
- #24603 — gclient projects pane follows the machine filter; no open prerequisite in the selected graph.
- #24859 — `gdaemon node` and the no-toolchain harness; no open prerequisite in the selected graph.
- #23834 — Vector and graph pin scope with background recovery; no open prerequisite in the selected graph.
- #23565 — One ownership ledger; no open prerequisite in the selected graph.
- #23582 — Current-config eligibility for history protection; no open prerequisite in the selected graph.
- File-overlap ordering: defer #23576 until after #23834 because both target `src/gobby/code_index/_storage/files.py` and other shared paths.
- File-overlap ordering: defer #23231 until after #24603 because both target `crates/gclient/src/app/live.rs` and other shared paths.

**Step #26 — width 12; implementation/task-outcome width 10**

- #24453 — Managed challenge as a family route; waits: #24452.
- #24573 — Rust runtime route; waits: #24572.
- #24676 — Brokered access resolution and the typed refusal; waits: #24674.
- #24376 — Command dispatch over the node channel; waits: #24375.
- #24860 — `gobby node install` and `gobby node enroll` run the sibling gdaemon; waits: #24859.
- #23576 — Sync worker leaves projection completion to gcode; no open prerequisite in the selected graph.
- #23231 — Event-driven refetches and roster jobs; no open prerequisite in the selected graph.
- #23566 — The recorder records only content-changing writes; waits: #23565.
- #23569 — Claim transfers a predecessor's live pairs; waits: #23565.
- #23826 — P3: Persist and read a pinned index; waits: #23834; phase/domain completion gate.
- #23838 — Overlay phase name, documented contract, and benchmark; waits: #23834.
- #23573 — P3: Path-Aware Content Retention; waits: #23582; phase/domain completion gate.
- File-overlap ordering: defer #23579 until after #24676 because both target `crates/gcode/contract/gcode.contract.json` and other shared paths.

**Step #27 — width 9; implementation/task-outcome width 7**

- #24454 — Managed credential issuance; waits: #24453.
- #24574 — Python runtime client, Native settlement branches, pane binding, and terminal services; waits: #24573, #24571.
- #24677 — gcode node client for status, search-content and index; waits: #24676.
- #24377 — Node reports; waits: #24376.
- #24861 — `gobby node enroll`; waits: #24860.
- #23232 — Lifecycle and shell-adoption actions as chained jobs; waits: #23231.
- #23568 — Rule 1: gate 9 checks the task's live pairs per checkout; waits: #23569, #23565.
- #23827 — P4: Make unchanged overlay indexing cheap; waits: #23838; phase/domain completion gate.
- #23571 — P1: Stop New Drift; waits: #23576; phase/domain completion gate.
- File-overlap ordering: defer #25208 until after #24377 because both target `crates/gdaemon/src/nodes/agents/mod.rs`.
- File-overlap ordering: defer #23579 until after #24677 because both target `crates/gcode/src/dispatch.rs`.

**Step #28 — width 8; implementation/task-outcome width 8**

- #24455 — Managed rotation and the `grant.json` rewrite; waits: #24454.
- #24575 — Terminal inventory reads and HTTP handlers; waits: #24574.
- #24678 — `gobby-codeindex` family serving the node routes; waits: #24677.
- #24378 — Checkout registration; waits: #24377.
- #24862 — Rust remediation names `gobby node enroll`; waits: #24861.
- #23233 — Orphan inventory and destroy fan-out as jobs; waits: #23232.
- #23567 — Rule 4 refuses writes into another Gobby project; waits: #23568, #23566.
- #23579 — `gcode vector reconcile` dry-run; no open prerequisite in the selected graph.
- File-overlap ordering: defer #25208 until after #24378 because both target `crates/gdaemon/src/nodes/agents/mod.rs`.

**Step #29 — width 7; implementation/task-outcome width 7**

- #24456 — Native managed handshake; waits: #24455.
- #24576 — Native lane viewports from the Rust lease registry; waits: #24574, #24573, #24575.
- #24681 — Machine-explicit read paths; waits: #24678.
- #24379 — Codex prompt delivery; waits: #24378.
- #24864 — Release verification library; waits: #24862.
- #23234 — Launch and reconnect audit and WARN bracket; waits: #23233.
- #23580 — Reconcile apply under the maintenance lease; waits: #23579.
- File-overlap ordering: defer #25208 until after #24379 because both target `crates/gdaemon/src/nodes/agents/commands.rs` and other shared paths.

**Step #30 — width 9; implementation/task-outcome width 7**

- #24457 — Managed grant presentation; waits: #24456.
- #24577 — Native terminal WS connection and operation state machine; waits: #24574, #24576, #24575.
- #24682 — Node freshness; waits: #24681.
- #24380 — Node code-index preflight; waits: #24379, #24678.
- #24865 — `gobby node install` enrolls and promotes the bundle; waits: #24864.
- #23221 — P1: gclient loop never awaits the daemon; waits: #23231, #23232, #23233, #23234; phase/domain completion gate.
- #23235 — Daemon health from in-flight age; waits: #23234.
- #23572 — P2: Vector Reconcile; waits: #23579, #23580; phase/domain completion gate.
- #23583 — Guides for reconcile and path-aware retention; waits: #23582, #23580, #23579.
- File-overlap ordering: defer #25208 until after #24380 because both target `crates/gdaemon/src/nodes/agents/commands.rs` and other shared paths.

**Step #31 — width 8; implementation/task-outcome width 6**

- #24439 — P4: Managed principals; waits: #24452, #24453, #24454, #24455, #24456, #24457; phase/domain completion gate.
- #24458 — Native `GET /api/config/schema`; waits: #24457.
- #24578 — Bounded proxy relay and frame/lifecycle fanout; waits: #24577.
- #24683 — Node search, search-symbol and search-text; waits: #24682.
- #24383 — Node pane monitor; waits: #24380.
- #24866 — `gobby node install` stages the pinned SRT; waits: #24865.
- #23236 — Shed load while slow; waits: #23235.
- #23574 — P4: Documentation; waits: #23583; phase/domain completion gate.
- File-overlap ordering: defer #25208 until after #24383 because both target `crates/gdaemon/src/nodes/agents/mod.rs`.

**Step #32 — width 7; implementation/task-outcome width 6**

- #24459 — Native secret listing and deletion; waits: #24458.
- #24579 — WS operator input under the route action hold; waits: #24577, #24578.
- #24684 — Node grep; waits: #24683.
- #24384 — Node sandbox reaping and records; waits: #24383.
- #24867 — `gobby node install` installs claude hooks and the MCP entry; waits: #24866.
- #23222 — P2: daemon health and load shedding; waits: #23235, #23236; phase/domain completion gate.
- #23237 — Native inventory and fallback input authority in gterm; waits: #23234, #23236.
- File-overlap ordering: defer #25208 until after #24384 because both target `crates/gdaemon/src/nodes/agents/mod.rs`.

**Step #33 — width 7; implementation/task-outcome width 7**

- #24461 — Prompt routes: JSON save contract and native serving; waits: #24459.
- #24580 — Terminal family registration, Native activation, and end-to-end parity; waits: #24579.
- #24685 — Node symbol navigation and symbol-at; waits: #24684.
- #25208 — Node relay executor; waits: #24376, #25207.
- #24868 — `gobby node install` installs codex hooks, trust and the MCP entry; waits: #24867.
- #23238 — Read native inventory through the existing frame socket; waits: #23237.
- #23244 — Bounded HostClient round trips; waits: #23237.
- File-overlap ordering: defer #24597 until after #25208 because both target `crates/gdaemon/src/nodes/agents/commands.rs` and other shared paths.

**Step #34 — width 6; implementation/task-outcome width 6**

- #24462 — Native YAML template read and export; waits: #24461.
- #24686 — Node graph navigation; waits: #24685.
- #25209 — Node `terminal.resize` executor; waits: #25208.
- #24869 — `gobby node install` installs droid hooks and the MCP entry; waits: #24868.
- #23239 — Launch, render and type native panes with no daemon; waits: #23234, #23238.
- #23247 — Drop the undrained host event subscription; waits: #23244.
- File-overlap ordering: defer #24597 until after #25209 because both target `crates/gdaemon/src/nodes/agents/commands.rs`.

**Step #35 — width 6; implementation/task-outcome width 6**

- #24463 — Native active-state reads: values, UI settings and global tool approvals; waits: #24462.
- #24687 — Node `gcode graph` reports and views; waits: #24686.
- #25210 — Relay tokens and the hub relay endpoint; waits: #25209.
- #24597 — Node workspace.merge and workspace.remove handlers; waits: #24384.
- #24870 — `gobby node install` installs grok hooks and the MCP entry; waits: #24869.
- #23240 — Keep control on disconnect for direct-granted panes; waits: #23239.

**Step #36 — width 6; implementation/task-outcome width 6**

- #24464 — Per-key config validation; waits: #24463.
- #24688 — Node evidence; waits: #24687.
- #25211 — Remote terminal link over the node channel; waits: #25210.
- #24601 — Node checkout.status handler; waits: #24597, #24384.
- #24871 — `gobby node install` installs agy hooks and the MCP entry; waits: #24870.
- #23241 — Reconfirm the lease on reconnect; waits: #23240.

**Step #37 — width 8; implementation/task-outcome width 7**

- #24465 — Complete-candidate config validation; waits: #24464.
- #24690 — Machine-explicit projection selection; waits: #24688, #23834.
- #24521 — Node `checkout.read` handler; waits: #24601.
- #25212 — Owner-scoped hub-wide terminal inventory; waits: #24577, #24575, #25211.
- #24592 — P4: Remote workspaces and the machine-scoped project view; waits: #24597, #24601, #24603; phase/domain completion gate.
- #24872 — `gobby node install` starts the node service and waits for the hub; waits: #24871.
- #23242 — Direct re-attach before proxy fallback; waits: #23241.
- #23245 — Host grant reconciliation never holds the terminal lease lock; waits: #23241, #23244.

**Step #38 — width 6; implementation/task-outcome width 2**

- #24466 — Native `PATCH /api/config/values`; waits: #24465.
- #24691 — Node-local `/api/embeddings`; waits: #24690.
- #24517 — P1: Node checkout reads; waits: #24521; phase/domain completion gate.
- #24853 — P3: Node install from a bundle; waits: #24864, #24865, #24866, #24867, #24868, #24869, #24870, #24871, #24872; phase/domain completion gate.
- #23223 — P3: direct panes keep their host grant across daemon disconnects; waits: #23237, #23238, #23239, #23240, #23241, #23242; phase/domain completion gate.
- #23224 — P4: daemon terminal handlers off the event loop and off the serial lane; waits: #23244, #23245, #23247; phase/domain completion gate.
- File-overlap ordering: defer #25213 until after #24691 because both target `crates/gdaemon/src/serve.rs` and other shared paths.
- File-overlap ordering: defer #24873 until after #24691 because both target `crates/gdaemon/src/nodes/mod.rs`.

**Step #39 — width 4; implementation/task-outcome width 4**

- #24467 — Native `POST /api/config/secrets`; waits: #24466.
- #24692 — Node vector sync routes and `gcode vector sync-node`; waits: #24691.
- #25213 — Remote attach through the hub relay; waits: #24578, #24580, #25212.
- #24873 — The handshake carries the contract and the hub refuses skew both ways; waits: #24872.

**Step #40 — width 3; implementation/task-outcome width 3**

- #24468 — YAML 1.2 document reading on both sides; waits: #24467.
- #24693 — Node heartbeat vector sync; waits: #24692.
- #25214 — Remote terminal input and sizing through node commands; waits: #24579, #25213.
- File-overlap ordering: defer #24874 until after #24693 because both target `crates/gdaemon/src/nodes/mod.rs` and other shared paths.

**Step #41 — width 3; implementation/task-outcome width 3**

- #24469 — Native `PUT /api/config/template` and `POST /api/config/import`; waits: #24468.
- #25215 — Open another machine's terminal in its owning workspace; waits: #25214.
- #24874 — The hub resolves the release target; waits: #24873.
- File-overlap ordering: defer #24694 until after #24469 because both target `crates/gruntime/src/lib.rs`.

**Step #42 — width 2; implementation/task-outcome width 2**

- #24470 — Native generation-endpoint probe client; waits: #24469.
- #24875 — The node stages the target and activates it when idle; waits: #24874.
- File-overlap ordering: defer #24694 until after #24470 because both target `crates/gruntime/src/lib.rs`.

**Step #43 — width 2; implementation/task-outcome width 2**

- #24471 — Native `PUT /api/config/generation-endpoints/{endpoint_name}/activate`; waits: #24470.
- #24876 — The hub drives staging; waits: #24875.
- File-overlap ordering: defer #24694 until after #24471 because both target `crates/gruntime/src/lib.rs`.

**Step #44 — width 2; implementation/task-outcome width 2**

- #24472 — Native validation-command classifier; waits: #24471.
- #24877 — `gobby node list` and `gobby node update`; waits: #24876.
- File-overlap ordering: defer #24694 until after #24472 because both target `crates/gruntime/src/lib.rs`.

**Step #45 — width 2; implementation/task-outcome width 1**

- #24473 — Native `POST /api/config/validation-detection/preview`; waits: #24472.
- #24854 — P4: Hub-driven updates and version skew; waits: #24873, #24874, #24875, #24876, #24877; phase/domain completion gate.
- File-overlap ordering: defer #24694 until after #24473 because both target `crates/gruntime/src/lib.rs`.

**Step #46 — width 2; implementation/task-outcome width 1**

- #24440 — P5: Config routes; waits: #24458, #24459, #24461, #24462, #24463, #24464, #24465, #24466, #24467, #24468, #24469, #24470, #24471, #24472, #24473; phase/domain completion gate.
- #24474 — `runtime` defaults to native; waits: #24473.
- File-overlap ordering: defer #24694 until after #24474 because both target `crates/gruntime/src/lib.rs`.

**Step #47 — width 1; implementation/task-outcome width 1**

- #24475 — Corpus: `runtime` family cases replayed natively; waits: #24474; singleton because target inventory is unbounded.

**Step #48 — width 1; implementation/task-outcome width 1**

- #24476 — E2E: compare smoke, backend-kill fallback, flip and rollback; waits: #24475; singleton because target inventory is unbounded.

**Step #49 — width 2; implementation/task-outcome width 2**

- #24477 — Docs: family-crate template and the flip runbook; waits: #24476.
- #24694 — `code_index` family and `GET /graph/search`; waits: #24693.

**Step #50 — width 2; implementation/task-outcome width 1**

- #24441 — P6: Native default, parity evidence and the template docs; waits: #24474, #24475, #24476, #24477; phase/domain completion gate.
- #24695 — Overview, file, neighbors and blast-radius routes; waits: #24694.

**Step #51 — width 1; implementation/task-outcome width 1**

- #21559 — Config, runtime handshake, and grant issuing; waits: #24439, #24440, #24441; singleton because target inventory is unbounded.

**Step #52 — width 1; implementation/task-outcome width 1**

- #20822 — Enforce the task-transition authority matrix at the hub boundary in Rust gobby-tasks; waits: #21559; singleton because target inventory is unbounded.

**Step #53 — width 1; implementation/task-outcome width 1**

- #21561 — Tasks write path and lifecycle transitions in Rust; waits: #20822; singleton because target inventory is unbounded.

**Step #54 — width 1; implementation/task-outcome width 1**

- #21560 — Tasks family; waits: #21559, #21561; singleton because target inventory is unbounded.

**Step #55 — width 1; implementation/task-outcome width 1**

- #21562 — Sessions and transcripts; waits: #21560; singleton because target inventory is unbounded.

**Step #56 — width 1; implementation/task-outcome width 1**

- #25201 — RustWriteCoordinatorClient must keep hub-mode's remote write branch for node terminals; waits: #24571; singleton because target inventory is unbounded.

**Step #57 — width 1; implementation/task-outcome width 1**

- #21566 — External-MCP transport multiplexer as a delegated backend; waits: #21560; singleton because target inventory is unbounded.

**Step #58 — width 1; implementation/task-outcome width 1**

- #22946 — Plan rule evaluation off the main runner to increase agent concurrency; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #59 — width 1; implementation/task-outcome width 1**

- #24986 — Executable 54-row characterization corpus; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #60 — width 2; implementation/task-outcome width 2**

- #24987 — Native scoped repository and HTTP reads; waits: #24986.
- #24696 — Path route; waits: #24695.

**Step #61 — width 2; implementation/task-outcome width 2**

- #24988 — Atomic registry lifecycle and notifications; waits: #24987.
- #24697 — Graph clear and rebuild routes; waits: #24696.

**Step #62 — width 2; implementation/task-outcome width 2**

- #24989 — Bounded skill parsing and validation; waits: #24988.
- #24698 — Invalidate route; waits: #24697.

**Step #63 — width 2; implementation/task-outcome width 2**

- #24990 — Native safety scanner and scan route; waits: #24989.
- #24699 — Hub job seam and sync worker; waits: #24698, #23576.

**Step #64 — width 2; implementation/task-outcome width 2**

- #24991 — Companion-file snapshots and HTTP file operations; waits: #24990.
- #24700 — Maintenance pass: reconcile and reindex; waits: #24699.

**Step #65 — width 1; implementation/task-outcome width 1**

- #24701 — Symbol summaries and community labels; waits: #24700.

**Step #66 — width 1; implementation/task-outcome width 1**

- #24702 — Hourly prune and `POST /api/code-index/prune`; waits: #24701.

**Step #67 — width 1; implementation/task-outcome width 1**

- #24705 — Edit trigger entry point; waits: #24702.

**Step #68 — width 1; implementation/task-outcome width 1**

- #24706 — Nightly repair handler; waits: #24705.

**Step #69 — width 1; implementation/task-outcome width 1**

- #24703 — Content GC for projects only nodes index; waits: #24706.

**Step #70 — width 1; implementation/task-outcome width 1**

- #24704 — Node workspace removal releases its code-index rows; waits: #24703.

**Step #71 — width 1; implementation/task-outcome width 1**

- #24360 — OAuth 2.1 authorization for the HTTP transport; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #72 — width 1; implementation/task-outcome width 1**

- #23275 — `gobby-mcp` crate: native MCP transports with OAuth and CIMD; waits: #24356, #24359, #24360; singleton because target inventory is unbounded.

**Step #73 — width 1; implementation/task-outcome width 1**

- #24680 — E2E: node preflight through the hub routes; waits: #24678; singleton because target inventory is unbounded.

**Step #74 — width 1; implementation/task-outcome width 1**

- #24689 — E2E: node read commands through the hub routes; waits: #24688, #24680; singleton because target inventory is unbounded.

**Step #75 — width 1; implementation/task-outcome width 1**

- #24906 — Remote spawn: node-side external write grants; waits: #24376; singleton because target inventory is unbounded.

**Step #76 — width 1; implementation/task-outcome width 0**

- #24362 — P2: Node agent duties (#24148); waits: #24375, #24376, #24377, #24378, #24379, #24380, #24383, #24384, #24906; phase/domain completion gate.

**Step #77 — width 1; implementation/task-outcome width 1**

- #24148 — S2.7 node half: node-local agent spawn and worktrees in gdaemon; waits: #24362; singleton because target inventory is unbounded.

**Step #78 — width 1; implementation/task-outcome width 1**

- #24149 — S2.7 hub half: machine-scoped attention, roster, response routing and dispatch gate in Rust; waits: #21560; singleton because target inventory is unbounded.

**Step #79 — width 1; implementation/task-outcome width 1**

- #21564 — Attention, agents, dispatch, and worktrees with machine scoping; waits: #24149, #24148; singleton because target inventory is unbounded.

**Step #80 — width 1; implementation/task-outcome width 1**

- #21567 — Workflows, rules, pipelines, build, and validation; waits: #21562, #21564, #22946; singleton because target inventory is unbounded.

**Step #81 — width 1; implementation/task-outcome width 1**

- #24151 — S2.11 hub half: hook ingress and rule engine in Rust; waits: #21567, #24521, #21562, #21566; singleton because target inventory is unbounded.

**Step #82 — width 1; implementation/task-outcome width 1**

- #24884 — Native policy, discovery, and dispatch gateway; waits: #24151, #21566.

**Step #83 — width 1; implementation/task-outcome width 1**

- #24992 — Paged instruction delivery and completed-load state; waits: #24884, #24991.

**Step #84 — width 1; implementation/task-outcome width 1**

- #24993 — Search, list projection and index freshness; waits: #24992, #24463.

**Step #85 — width 1; implementation/task-outcome width 1**

- #24994 — Native node-local skill filesystem boundary; waits: #24993.

**Step #86 — width 1; implementation/task-outcome width 1**

- #24995 — Bounded GitHub and ZIP source loading; waits: #24994.

**Step #87 — width 1; implementation/task-outcome width 1**

- #24996 — Native API hub providers; waits: #24995, #24463.

**Step #88 — width 1; implementation/task-outcome width 1**

- #24997 — GitHub collection, topic and Claude-plugin hubs; waits: #24996.

**Step #89 — width 1; implementation/task-outcome width 1**

- #24998 — Scanned installation and source updates; waits: #24997.

**Step #90 — width 1; implementation/task-outcome width 1**

- #24999 — Native bundled synchronization and default restoration; waits: #24998.

**Step #91 — width 1; implementation/task-outcome width 1**

- #25000 — Owned, atomic script-generation publication; waits: #24999.

**Step #92 — width 1; implementation/task-outcome width 1**

- #25001 — Dependency and browser runtime readiness; waits: #25000.

**Step #93 — width 1; implementation/task-outcome width 1**

- #25217 — Live hub-to-node remote attach proof; waits: #25215; singleton because target inventory is unbounded.

**Step #94 — width 1; implementation/task-outcome width 1**

- #24604 — Live hub-to-node proof with isolated daemons and real providers; waits: #24517, #24601, #24148, #24597; singleton because target inventory is unbounded.
- Named boundary: early hub-node transport/provider proof. This result alone does not establish node parity.

**Step #95 — width 1; implementation/task-outcome width 1**

- #24532 — Live node-origin checkout reads; waits: #24521, #24604; singleton because target inventory is unbounded.

**Step #96 — width 3; implementation/task-outcome width 1**

- #24519 — P3: Live proof; waits: #24532; phase/domain completion gate.
- #24593 — P5: Live hub-to-node proof; waits: #24604; phase/domain completion gate.
- #24863 — Python login retires and pair_node enrolls with the Rust verb; waits: #24604, #24861.

**Step #97 — width 1; implementation/task-outcome width 1**

- #23277 — Hook envelope carries edited-file content for disk-reading rules; waits: #24517, #24519; singleton because target inventory is unbounded.

**Step #98 — width 1; implementation/task-outcome width 1**

- #24150 — S2.11 node half: gdaemon node-local envelope ledger forwarding hooks to the hub; waits: #23277; singleton because target inventory is unbounded.

**Step #99 — width 1; implementation/task-outcome width 1**

- #21579 — Rust node duties; waits: #24148, #24150; singleton because target inventory is unbounded.

**Step #100 — width 1; implementation/task-outcome width 1**

- #17436 — Node mode: the per-machine daemon that holds no datastore credentials and runs only local duties; waits: #21579; singleton because target inventory is unbounded.

**Step #101 — width 1; implementation/task-outcome width 1**

- #21575 — Hub mode: the datastore owner that runs everything database-backed; waits: #24592, #24593; singleton because target inventory is unbounded.

**Step #102 — width 1; implementation/task-outcome width 1**

- #24856 — Every binary release publishes signed checksums; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #103 — width 1; implementation/task-outcome width 1**

- #24857 — gmcp release workflow; waits: #24856; singleton because target inventory is unbounded.

**Step #104 — width 1; implementation/task-outcome width 1**

- #24858 — gdaemon releases carry node bundles; waits: #24859, #24857; singleton because target inventory is unbounded.

**Step #105 — width 2; implementation/task-outcome width 0**

- #24851 — P1: Signed release publishing; waits: #24856, #24857, #24858; phase/domain completion gate.
- #24852 — P2: `gobby node` and enrollment; waits: #24859, #24860, #24861, #24862, #24863; phase/domain completion gate.

**Step #106 — width 1; implementation/task-outcome width 1**

- #24878 — Node setup, release and CLI docs; waits: #24877, #24863; singleton because target inventory is unbounded.

**Step #107 — width 1; implementation/task-outcome width 0**

- #24855 — P5: Documentation; waits: #24878; phase/domain completion gate.

**Step #108 — width 1; implementation/task-outcome width 1**

- #24657 — Rust-only node install and enrollment; waits: #24604, #24851, #24855, #24853, #24852, #24854; singleton because target inventory is unbounded.

**Step #109 — width 1; implementation/task-outcome width 1**

- #25127 — Orchestrator acts as the operator's hands on a hub or node, with explicit approval and no rule toggles; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #110 — hub-node live-test gate; width 1**

- N2 — Live node parity for shared state, sessions, tasks, memory and skills (task ID reserved; not created).
- Gate: Reserved proposed task; #24604, #24532 and #24994 complete. Node CLI/workspace/platform gaps must be resolved before final parity can pass.

**Step #111 — hub-node live-test gate; width 1**

- N3 — Live node orchestration, messaging, rules and recovery parity (task ID reserved; not created).
- Gate: Reserved proposed task; #24604, #24532, #25217 and v4 prerequisites complete. Keep its state/ports independent of the N2 rig.

**Step #112 — hub-node live-test gate; width 1**

- N4 — Measure and enforce the hub-node latency budget by workflow class (task ID reserved; not created).
- Gate: Reserved proposed task; N2 and N3 pass. Approved budgets and install/update evidence are required.

**Step #113 — hub-node live-test gate; width 1**

- N1 — Publish the node parity inventory, feature-parity smoke checklist and readiness packet (task ID reserved; not created).
- Gate: Reserved proposed task; N2/N3/N4 pass, #24878 complete, and every required fold including the node CLI slice is evidenced. This conditional boundary is the handoff to Josh's extensive manual smoke test.

**Step #114 — width 1; implementation/task-outcome width 1**

- #23669 — Staleness check and council pass on the handoff stop gates plan (#22643); no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #115 — width 1; implementation/task-outcome width 1**

- #22643 — Handoff stop gates and pull order; waits: #23669; singleton because target inventory is unbounded.

**Step #116 — width 1; implementation/task-outcome width 1**

- #24222 — Rerun the Graphify comparison on current gcode; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #117 — width 1; implementation/task-outcome width 1**

- #22948 — Code index: communities and Graphify parity; waits: #24222; singleton because target inventory is unbounded.

**Step #118 — width 1; implementation/task-outcome width 1**

- #23220 — gclient daemon resilience; waits: #23221, #23222, #23223, #23224; singleton because target inventory is unbounded.

**Step #119 — width 1; implementation/task-outcome width 1**

- #23561 — Implement approved content-based ownership rules for close gates and edit hooks; waits: #23565, #23566, #23567, #23568, #23569; singleton because target inventory is unbounded.

**Step #120 — width 1; implementation/task-outcome width 1**

- #23433 — Worktree spawns spend 35-72 s in code_index_index (ensure_isolation_code_index); waits: #23826, #23827; singleton because target inventory is unbounded.

**Step #121 — width 1; implementation/task-outcome width 1**

- #23584 — Hourly reconcile apply; waits: #23580; singleton because target inventory is unbounded.

**Step #122 — width 1; implementation/task-outcome width 1**

- #23570 — Implement approved code-index vector reconciliation and path-aware content retention; waits: #23571, #23572, #23573, #23574, #23584; singleton because target inventory is unbounded.

**Step #123 — width 1; implementation/task-outcome width 1**

- #24029 — gcode evidence commit-bound search reports complete_empty from a live index that differs from the bound commit; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #124 — width 23; implementation/task-outcome width 6**

- #24820 — Code index; waits: #23433, #23570, #24029; phase/domain completion gate.
- #23206 — P2: Milestone M2 — terminal WS and proxy relay [category: code] (depends: P1); waits: #24571, #24572, #24573, #24574, #24575, #24576, #24577, #24578, #24579, #24580, #25201; phase/domain completion gate.
- #24979 — P0: Freeze the family contract; waits: #24986; phase/domain completion gate.
- #24980 — P1: Native registry and content services; waits: #24987, #24988, #24989, #24990, #24991, #24992, #24993; phase/domain completion gate.
- #24981 — P2: Source acquisition and installation; waits: #24994, #24995, #24996, #24997, #24998, #24999; phase/domain completion gate.
- #24982 — P3: Native script preparation; waits: #25000, #25001; phase/domain completion gate.
- #25002 — Skills descriptor, schemas and MCP behavior; waits: #25001.
- #25112 — gcore Qdrant alias operations; no open prerequisite in the selected graph.
- #24662 — P2: Node read parity; waits: #24681, #24682, #24683, #24684, #24685, #24686, #24687, #24688, #24689; phase/domain completion gate.
- #24663 — P3: Projection sync per machine; waits: #24690; phase/domain completion gate.
- #24664 — P4: Node-local embedding; waits: #24691, #24692, #24693; phase/domain completion gate.
- #24665 — P5: `/api/code-index` family seam; waits: #24694; phase/domain completion gate.
- #24666 — P6: Graph read routes; waits: #24695, #24696; phase/domain completion gate.
- #24667 — P7: Graph lifecycle and invalidate routes; waits: #24697, #24698; phase/domain completion gate.
- #24668 — P8: Hub code-index jobs; waits: #24699, #24700, #24701, #24702, #24703, #24704, #24705, #24706; phase/domain completion gate.
- #24707 — `code_index` defaults to native; waits: #24704.
- #24885 — Python compatibility backend and per-server cutover; waits: #24884.
- #25202 — P1: Node-commands contract and node relay half; waits: #25207, #25208, #25209; phase/domain completion gate.
- #25203 — P2: Hub relay endpoint and remote link; waits: #25210, #25211; phase/domain completion gate.
- #25204 — P3: Hub terminal family; waits: #25212, #25213, #25214; phase/domain completion gate.
- #25205 — P4: gclient; waits: #25215; phase/domain completion gate.
- #24850 — Web terminal reports pane focus and its idle expiry; no open prerequisite in the selected graph.
- #23603 — Provider-neutral quota observations and persisted capacity reads; no open prerequisite in the selected graph.

**Step #125 — width 1; implementation/task-outcome width 1**

- #22360 — Cross-platform CI evidence: Windows cross-target clippy for the gterminal lint policy and any Linux/Windows-only checks; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #126 — width 1; implementation/task-outcome width 1**

- #24763 — Restore operator-launched panes and CLI sessions after a reboot; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #127 — width 1; implementation/task-outcome width 1**

- #24834 — Research: does Gobby send anything to projects we borrowed from or to Zig/Ghostty; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #128 — width 1; implementation/task-outcome width 1**

- #24924 — Release gterm: installed 0.1.14 is stale against 0.5.0 source; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #129 — width 1; implementation/task-outcome width 1**

- #25144 — Native WS publish to the gdaemon front door times out on every new generation, so every spawn_agent fails lifecycle publication; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #130 — width 1; implementation/task-outcome width 1**

- #25149 — Native batch text path types long text into Claude Code panes and can drop its head; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #131 — width 1; implementation/task-outcome width 1**

- #25187 — gclient: arrange and move-pane CLI verbs, plus a pane right-click 'Move to tab' menu item; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #132 — width 5; implementation/task-outcome width 1**

- #24821 — Terminal and client; waits: #25144, #24763, #24924, #25187, #22360, #24834, #25149; phase/domain completion gate.
- #24983 — P4: Publish the native internal-tool family; waits: #25002; phase/domain completion gate.
- #25003 — Native command tree and registry commands; waits: #25002.
- #24881 — P2: Native gateway and strangler; waits: #24884, #24885; phase/domain completion gate.
- #24846 — P3: Web terminal idle focus report; waits: #24850; phase/domain completion gate.

**Step #133 — width 1; implementation/task-outcome width 1**

- #25070 — Native memories collection rebuild seam for embedding switches; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #134 — width 1; implementation/task-outcome width 1**

- #21563 — Memory and search; waits: #21560, #25070; singleton because target inventory is unbounded.

**Step #135 — width 1; implementation/task-outcome width 1**

- #21565 — Daemon-side gterm adoption and terminal WS; waits: #23206; singleton because target inventory is unbounded.

**Step #136 — width 1; implementation/task-outcome width 1**

- #25004 — Native source and authoring CLI commands; waits: #25003.

**Step #137 — width 1; implementation/task-outcome width 1**

- #25005 — Native hub CLI and active configuration; waits: #24466, #25004.

**Step #138 — width 1; implementation/task-outcome width 1**

- #25006 — Forward the public CLI and retire Python callbacks; waits: #25005.

**Step #139 — width 1; implementation/task-outcome width 0**

- #24984 — P5: Native CLI callbacks; waits: #25003, #25004, #25005, #25006; phase/domain completion gate.

**Step #140 — width 1; implementation/task-outcome width 1**

- #21581 — Plans and the plan registry route family; waits: #21560; singleton because target inventory is unbounded.

**Step #141 — width 1; implementation/task-outcome width 1**

- #25014 — Characterize Cron behavior before takeover; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #142 — width 1; implementation/task-outcome width 1**

- #25015 — Durable bounded-output metadata schema; waits: #25014.

**Step #143 — width 1; implementation/task-outcome width 1**

- #25016 — Native schedule and value model; waits: #25015, #21559.

**Step #144 — width 1; implementation/task-outcome width 1**

- #25017 — Native job repository and system-row mutations; waits: #25016.

**Step #145 — width 2; implementation/task-outcome width 1**

- #25010 — P1: Portable schedule and storage contracts; waits: #25014, #25015, #25016, #25017; phase/domain completion gate.
- #25018 — Atomic scheduled and manual admission; waits: #25017.

**Step #146 — width 1; implementation/task-outcome width 1**

- #21584 — Source control, GitHub triage, and sync route family; waits: #21560; singleton because target inventory is unbounded.

**Step #147 — width 1; implementation/task-outcome width 1**

- #25043 — Executable parity inventory and isolated corpus; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #148 — width 1; implementation/task-outcome width 1**

- #25044 — Statically linked Projects family; waits: #25043.

**Step #149 — width 1; implementation/task-outcome width 0**

- #25037 — P1: Contract and family assembly; waits: #25043, #25044; phase/domain completion gate.

**Step #150 — width 1; implementation/task-outcome width 1**

- #25093 — Executable 19-row characterization corpus; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #151 — width 2; implementation/task-outcome width 1**

- #25084 — P0: Freeze the family contract; waits: #25093; phase/domain completion gate.
- #25094 — Crate, three family shells, configuration snapshot and status reads; waits: #25093.

**Step #152 — width 3; implementation/task-outcome width 3**

- #25095 — Native embedding generation; waits: #25094.
- #25108 — Web-chat provider health seam; waits: #25094.
- #25113 — Embedding catalog contract; waits: #25094.

**Step #153 — width 3; implementation/task-outcome width 2**

- #25085 — P1: Native families, configuration and capability reads; waits: #25094, #25095; phase/domain completion gate.
- #25096 — Feature-profile candidate contract; waits: #25095.
- #25114 — Switch journal, coordinator and routes; waits: #24466, #25113.

**Step #154 — width 1; implementation/task-outcome width 1**

- #25097 — Text-generation service and endpoint adapters; waits: #25096.

**Step #155 — width 1; implementation/task-outcome width 1**

- #25098 — CLI provider adapters; waits: #25097.

**Step #156 — width 1; implementation/task-outcome width 1**

- #25099 — Native text generation route; waits: #25098.

**Step #157 — width 2; implementation/task-outcome width 2**

- #25100 — Local endpoint context observations; waits: #25099.
- #25115 — Switch staging through provider APIs; waits: #25114, #25099.

**Step #158 — width 3; implementation/task-outcome width 2**

- #25086 — P2: Native text generation; waits: #25096, #25097, #25098, #25099, #25100; phase/domain completion gate.
- #25101 — Read-only gcode tool runtime and endpoint tool loop; waits: #25100.
- #25109 — Native provider list and model catalog; waits: #25108, #25100.

**Step #159 — width 2; implementation/task-outcome width 2**

- #25102 — Provider tool-chat styles; waits: #25101.
- #25110 — Native provider usage; waits: #23603, #25109.

**Step #160 — width 1; implementation/task-outcome width 1**

- #25103 — Native chat completions with managed tool credentials; waits: #25102, #24457, #24454.

**Step #161 — width 2; implementation/task-outcome width 1**

- #25087 — P3: Native tool chat; waits: #25101, #25102, #25103; phase/domain completion gate.
- #25104 — Native vision extraction; waits: #25103.

**Step #162 — width 1; implementation/task-outcome width 1**

- #25072 — Native Voice STT readiness probe for the LLM capability registry; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #163 — width 1; implementation/task-outcome width 1**

- #25105 — Full capability snapshot with Voice audio bindings; waits: #25072, #25104.

**Step #164 — width 1; implementation/task-outcome width 0**

- #25088 — P4: Vision and the full capability snapshot; waits: #25104, #25105; phase/domain completion gate.

**Step #165 — width 1; implementation/task-outcome width 1**

- #21588 — Voice route family; waits: #25072; singleton because target inventory is unbounded.

**Step #166 — width 1; implementation/task-outcome width 1**

- #21583 — Communications route family: Telegram and Slack; waits: #21588, #21562; singleton because target inventory is unbounded.

**Step #167 — width 1; implementation/task-outcome width 1**

- #21589 — Admin, metrics, traces, and telemetry route family; waits: #21560; singleton because target inventory is unbounded.

**Step #168 — width 1; implementation/task-outcome width 1**

- #21590 — Web UI static serving route family; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #169 — width 1; implementation/task-outcome width 1**

- #21591 — Install and setup route family; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #170 — width 1; implementation/task-outcome width 1**

- #21592 — WS chat route family; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #171 — width 1; implementation/task-outcome width 1**

- #22143 — Plan focused-session speech in gclient; waits: #21588; singleton because target inventory is unbounded.

**Step #172 — width 1; implementation/task-outcome width 1**

- #21569 — Hook ingress and the node-local envelope ledger; waits: #24151, #24150; singleton because target inventory is unbounded.

**Step #173 — width 1; implementation/task-outcome width 1**

- #25071 — Native tool_embeddings collection rebuild seam for embedding switches; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #174 — width 1; implementation/task-outcome width 1**

- #25116 — Switch build, flip and garbage collection; waits: #25071, #25112, #25070, #25115.

**Step #175 — width 1; implementation/task-outcome width 1**

- #21570 — MCP front door flip; waits: #21566, #21569, #25071; singleton because target inventory is unbounded.

**Step #176 — width 1; implementation/task-outcome width 1**

- #24708 — Corpus: `code_index` family cases replayed natively; waits: #24707, #24475; singleton because target inventory is unbounded.

**Step #177 — width 1; implementation/task-outcome width 1**

- #24709 — Delete Python's pruner; waits: #24708.

**Step #178 — width 1; implementation/task-outcome width 1**

- #24710 — Delete Python's graph path, clear, rebuild and invalidate routes; waits: #24709.

**Step #179 — width 1; implementation/task-outcome width 1**

- #24711 — Drop the pruner's tables; waits: #24710.

**Step #180 — width 1; implementation/task-outcome width 1**

- #24712 — Grants stop listing the projection operations; waits: #24711.

**Step #181 — width 1; implementation/task-outcome width 1**

- #22340 — Plan gobby-messaging: channel-based agent chatboard as a native gdaemon family; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #182 — width 1; implementation/task-outcome width 1**

- #24679 — Native handshake issues the remote grant shape; waits: #24712.

**Step #183 — width 1; implementation/task-outcome width 0**

- #24661 — P1: Node code-index routes; waits: #24674, #24676, #24677, #24678, #24679, #24680; phase/domain completion gate.

**Step #184 — width 1; implementation/task-outcome width 1**

- #23276 — gcode index writes from a node need hub HTTP routes; waits: #24661; singleton because target inventory is unbounded.

**Step #185 — width 1; implementation/task-outcome width 1**

- #24886 — Rebuild and version every affected shipped binary; waits: #24884, #24885; singleton because target inventory is unbounded.

**Step #186 — width 1; implementation/task-outcome width 1**

- #24932 — Pre-ship: rotate the gobby DB password and retire the temporary josh@gobby.ai web login; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #187 — width 1; implementation/task-outcome width 0**

- #24882 — P3: Coherent release proof; waits: #24886, #24932; phase/domain completion gate.

**Step #188 — width 1; implementation/task-outcome width 1**

- #24841 — Plan the Rust architecture for internal gobby-* MCP servers; waits: #24881, #24882; singleton because target inventory is unbounded.

**Step #189 — width 1; implementation/task-outcome width 1**

- #17769 — Per-user auth for shared daemons: user API keys + machine ownership + user-scoped secrets; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #190 — width 1; implementation/task-outcome width 1**

- #19652 — Research Docker-hosted centralized session transcript archives; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #191 — width 1; implementation/task-outcome width 1**

- #25216 — ROADMAP corrections for remote attach; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #192 — width 1; implementation/task-outcome width 0**

- #25206 — P5: Documentation and live proof; waits: #25216, #25217; phase/domain completion gate.

**Step #193 — width 1; implementation/task-outcome width 1**

- #20202 — Plan hub-wide terminal roster, attach routing, and capability tokens for remote gclient attach; waits: #25202, #25203, #25204, #25205, #25206; singleton because target inventory is unbounded.

**Step #194 — width 1; implementation/task-outcome width 1**

- #24659 — gdaemon verifies the web UI session cookie; retire Python's code-index web UI reads; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #195 — width 1; implementation/task-outcome width 1**

- #24910 — Browser-testing sign-in: per-object cross-project isolation on web UI routes; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #196 — width 1; implementation/task-outcome width 1**

- #24964 — Native front door: shared CORS and preflight handling for ported families; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #197 — width 1; implementation/task-outcome width 1**

- #24967 — Native browser session-cookie verifier (shared, implementation-only); no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #198 — width 2; implementation/task-outcome width 2**

- #25007 — Skills browser admission through the shared verifier; waits: #24967, #24964, #25006.
- #25045 — Integrate the shared browser authority; waits: #25044, #24967.

**Step #199 — width 2; implementation/task-outcome width 2**

- #25008 — Skills-only activation and coherent release; waits: #24841, #25007.
- #25046 — Reusable native admission and confinement; waits: #24967, #24964, #25045, #24452.

**Step #200 — width 1; implementation/task-outcome width 1**

- #25009 — Publish exact Skills parity ownership; waits: #25008; singleton because target inventory is unbounded.

**Step #201 — width 2; implementation/task-outcome width 0**

- #24985 — P6: Family activation and native evidence; waits: #25007, #25008, #25009; phase/domain completion gate.
- #25038 — P2: Shared native admission; waits: #25045, #25046; phase/domain completion gate.

**Step #202 — width 1; implementation/task-outcome width 1**

- #21580 — Skills route family; waits: #24985, #24982, #24984, #24983, #24980, #24981, #24979; singleton because target inventory is unbounded.

**Step #203 — width 1; implementation/task-outcome width 1**

- #24968 — Native files_home safe I/O capability (shared, implementation-only); no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #204 — width 2; implementation/task-outcome width 2**

- #25019 — Scheduler tick, reconciliation and shutdown; waits: #25018, #24968.
- #25047 — Native USER.md on shared files_home; waits: #24968, #25046.

**Step #205 — width 2; implementation/task-outcome width 2**

- #25020 — Bounded shell and handler action execution; waits: #24968, #25019.
- #25048 — Native attachment limits and publication; waits: #25047, #24968.

**Step #206 — width 2; implementation/task-outcome width 2**

- #25021 — Durable agent dispatch and cron parent sessions; waits: #25020, #21562, #21564.
- #25049 — Native attachment content and lease-safe deletion; waits: #25048, #24968.

**Step #207 — width 2; implementation/task-outcome width 2**

- #25022 — Durable pipeline dispatch; waits: #21567, #25021.
- #25050 — Native files migration and final attachment layout; waits: #25049, #24968.

**Step #208 — width 2; implementation/task-outcome width 2**

- #25023 — Native code-index nightly registration and invocation; waits: #24706, #25022.
- #25051 — Atomic namespace cutover and all current consumers; waits: #25050.

**Step #209 — width 2; implementation/task-outcome width 1**

- #25039 — P3: Hub documents and once-only attachment relocation; waits: #25047, #25048, #25049, #25050, #25051; phase/domain completion gate.
- #25052 — Native project inventory, detail and cross-project MCP listing; waits: #24592, #25051.

**Step #210 — width 1; implementation/task-outcome width 1**

- #25053 — Native local file tree, text and image reads; waits: #25052.

**Step #211 — width 1; implementation/task-outcome width 1**

- #25054 — Native checkout file writes; waits: #25053.

**Step #212 — width 1; implementation/task-outcome width 1**

- #25055 — Native local Git status and diff; waits: #25054.

**Step #213 — width 1; implementation/task-outcome width 1**

- #25056 — Native project initialization and checkout mutation; waits: #25055.

**Step #214 — width 1; implementation/task-outcome width 1**

- #25057 — Shared project metadata serialization and policy guards; waits: #25056, #24968.

**Step #215 — width 1; implementation/task-outcome width 1**

- #25058 — Native project updates and soft deletion; waits: #24968, #25057.

**Step #216 — width 2; implementation/task-outcome width 1**

- #25040 — P4: Projects and local checkout browsing; waits: #25052, #25053, #25054, #25055, #25056, #25057, #25058; phase/domain completion gate.
- #25059 — Shared cross-runtime project write fence; waits: #25058.

**Step #217 — width 1; implementation/task-outcome width 1**

- #25060 — Native lifecycle-safe hard purge; waits: #25059, #24454.

**Step #218 — width 1; implementation/task-outcome width 1**

- #25061 — Native Projects command group and ordinary verbs; waits: #25060.

**Step #219 — width 1; implementation/task-outcome width 1**

- #25068 — Native agent-token and runtime-grant admission seam (shared, implementation-only); waits: #24457, #25046, #24452; singleton because target inventory is unbounded.

**Step #220 — width 1; implementation/task-outcome width 1**

- #25106 — Native admission binding and the #25068 contract; waits: #25105, #25068, #25046.

**Step #221 — width 1; implementation/task-outcome width 1**

- #25107 — ai family activation and coherent release; waits: #25112, #25106; singleton because target inventory is unbounded.

**Step #222 — width 2; implementation/task-outcome width 1**

- #25089 — P5: ai family admission and activation; waits: #25106, #25107; phase/domain completion gate.
- #25111 — providers family activation and gdaemon release; waits: #25110, #25107, #21592.

**Step #223 — width 2; implementation/task-outcome width 1**

- #25090 — P6: Native providers routes; waits: #25108, #25109, #25110, #25111; phase/domain completion gate.
- #25117 — Native embeddings command tree; waits: #25111, #25116.

**Step #224 — width 1; implementation/task-outcome width 1**

- #25118 — ai-embedding-switch activation and gdaemon release; waits: #25111, #25117; singleton because target inventory is unbounded.

**Step #225 — width 1; implementation/task-outcome width 1**

- #25119 — Forward the public embeddings CLI; waits: #25118.

**Step #226 — width 1; implementation/task-outcome width 0**

- #25091 — P7: Native embedding switch; waits: #25112, #25113, #25114, #25115, #25116, #25117, #25118, #25119; phase/domain completion gate.

**Step #227 — width 1; implementation/task-outcome width 1**

- #25120 — Publish AI family parity ownership; waits: #25119; singleton because target inventory is unbounded.

**Step #228 — width 1; implementation/task-outcome width 0**

- #25092 — P8: Ledger record; waits: #25120; phase/domain completion gate.

**Step #229 — width 1; implementation/task-outcome width 1**

- #21587 — AI providers, LLM routing, and local models route family; waits: #25085, #25084, #25086, #25087, #25088, #25089, #25090, #25091, #25092; singleton because target inventory is unbounded.

**Step #230 — width 1; implementation/task-outcome width 1**

- #25062 — Native verification-command refresh; waits: #25058, #25061, #21587.

**Step #231 — width 2; implementation/task-outcome width 1**

- #25041 — P5: Lifecycle-safe purge and native operator commands; waits: #25059, #25060, #25061, #25062; phase/domain completion gate.
- #25063 — Read/write compare verification and native default; waits: #24967, #24964, #25062.

**Step #232 — width 1; implementation/task-outcome width 1**

- #25064 — Cross-surface native parity and two-machine failure proof; waits: #25063; singleton because target inventory is unbounded.

**Step #233 — width 1; implementation/task-outcome width 1**

- #25065 — Ledger evidence and downstream cutover contract; waits: #25064; singleton because target inventory is unbounded.

**Step #234 — width 1; implementation/task-outcome width 0**

- #25042 — P6: Native default, complete parity and operational proof; waits: #25063, #25064, #25065; phase/domain completion gate.

**Step #235 — width 1; implementation/task-outcome width 1**

- #21585 — Projects, /api/files, and hub documents route family; waits: #25037, #25038, #25039, #25040, #25041, #25042; singleton because target inventory is unbounded.

**Step #236 — width 1; implementation/task-outcome width 1**

- #25024 — Native system-job bindings; waits: #21589, #21585, #25023, #21563.

**Step #237 — width 1; implementation/task-outcome width 1**

- #25025 — Passive hub runtime wiring and completion events; waits: #21575, #25024.

**Step #238 — width 2; implementation/task-outcome width 1**

- #25011 — P2: One hub scheduler and native action bindings; waits: #25018, #25019, #25020, #25021, #25022, #25023, #25024, #25025; phase/domain completion gate.
- #25026 — Cron family and job/run read routes; waits: #24967, #24964, #25025.

**Step #239 — width 1; implementation/task-outcome width 1**

- #25027 — Bounded raw-output retrieval; waits: #25026, #24968.

**Step #240 — width 1; implementation/task-outcome width 1**

- #25028 — Job mutations and immediate execution routes; waits: #25027.

**Step #241 — width 1; implementation/task-outcome width 1**

- #25029 — Native restart protection query and lifecycle consumers; waits: #25028.

**Step #242 — width 1; implementation/task-outcome width 1**

- #25030 — Native service contract for CLI and MCP owners; waits: #25029.

**Step #243 — width 1; implementation/task-outcome width 1**

- #25031 — Side-effect-safe comparison and native corpus replay; waits: #25030.

**Step #244 — width 2; implementation/task-outcome width 1**

- #25012 — P3: HTTP family and front-door service handoffs; waits: #25026, #25027, #25028, #25029, #25030, #25031; phase/domain completion gate.
- #25032 — Native default and single-owner scheduler cutover; waits: #24967, #25031.

**Step #245 — width 1; implementation/task-outcome width 1**

- #25033 — Native delivered-mailbox retention takeover; waits: #25032.

**Step #246 — width 1; implementation/task-outcome width 1**

- #25034 — Admit Cron browser routes through the shared verifier; waits: #24967, #24964, #25033.

**Step #247 — width 1; implementation/task-outcome width 1**

- #25035 — Delete Cron-owned Python and release the nightly cleanup boundary; waits: #25034.

**Step #248 — width 1; implementation/task-outcome width 1**

- #25036 — Parity ledger and operator handoff; waits: #25035; singleton because target inventory is unbounded.

**Step #249 — width 1; implementation/task-outcome width 0**

- #25013 — P4: Native takeover and Python retirement; waits: #25032, #25033, #25034, #25035, #25036; phase/domain completion gate.

**Step #250 — width 1; implementation/task-outcome width 1**

- #21582 — Cron and scheduler route family; waits: #25012, #25013, #25010, #25011; singleton because target inventory is unbounded.

**Step #251 — width 1; implementation/task-outcome width 1**

- #21568 — Remaining route families; waits: #21585, #21580, #21581, #21582, #21584, #21587, #21588, #22143, #21583, #21589, #21590, #21591, #21592; singleton because target inventory is unbounded.

**Step #252 — width 1; implementation/task-outcome width 1**

- #24713 — Delete what only the trigger and nightly repair used; waits: #24151, #24679, #21582.

**Step #253 — width 1; implementation/task-outcome width 0**

- #24669 — P9: Native default and Python retirement; waits: #24707, #24708, #24709, #24710, #24711, #24712, #24713; phase/domain completion gate.

**Step #254 — width 1; implementation/task-outcome width 1**

- #21586 — Migrate code-index orchestration and shared dependencies; waits: #24661, #24669, #24662, #24663, #24664, #24665, #24666, #24667, #24668; singleton because target inventory is unbounded.

**Step #255 — width 1; implementation/task-outcome width 0**

- #21544 — Stage 2: strangler absorption of the Python daemon behind the front door; waits: #21559, #21560, #21562, #21565, #24148, #24150, #24841, #21563, #21566, #21568, #22340, #23275, #23276, #24149, #21564, #21567, #24151, #21569, #21570, #21586; phase/domain completion gate.

**Step #256 — width 1; implementation/task-outcome width 1**

- #21571 — Operator verbs on the client; waits: #21544; singleton because target inventory is unbounded.

**Step #257 — width 1; implementation/task-outcome width 1**

- #21574 — Delete the Python daemon package (S3.2 deletion step); waits: #21544; singleton because target inventory is unbounded.

**Step #258 — width 1; implementation/task-outcome width 1**

- #21573 — Naming switch with Python removed: gclient becomes gobby and the Python daemon is deleted; waits: #21544, #21571, #21574; singleton because target inventory is unbounded.

**Step #259 — width 1; implementation/task-outcome width 0**

- #21545 — Stage 3: the gobby client takes the name and Python retires; waits: #21571, #21573; phase/domain completion gate.

**Step #260 — width 1; implementation/task-outcome width 1**

- #21576 — Move Telegram and comms attachments onto hub files_home; waits: #21585; singleton because target inventory is unbounded.

**Step #261 — width 1; implementation/task-outcome width 0**

- #21546 — Stage 4: hub and node live (story B); waits: #17436, #21575, #19652, #17769, #20202, #21576; phase/domain completion gate.

**Step #262 — width 1; implementation/task-outcome width 0**

- #21542 — Gobby 1.0: gdaemon front door, strangler port, hub and node; waits: #21544, #24657, #25236, #25237, #21545, #21546, #24659, #24964, #24967, #24968, #25068, #25127, #24910; phase/domain completion gate.

**Step #263 — width 1; implementation/task-outcome width 1**

- #22660 — Automatically clear persistent role sessions between tasks and restore persona on successor; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #264 — width 1; implementation/task-outcome width 1**

- #22819 — Add an atomic append operation for task description amendments; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #265 — width 1; implementation/task-outcome width 1**

- #23615 — Worktree creation rollback: report a failed removal, and block WORKTREE_CREATE when sidecar copy or hook install fails; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #266 — width 1; implementation/task-outcome width 1**

- #23818 — Prevent held Codex /compact submission before turn end; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #267 — width 1; implementation/task-outcome width 1**

- #23895 — Carry a seat across a Claude Code conversation re-ID; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #268 — width 1; implementation/task-outcome width 1**

- #23902 — Research why concurrent cargo builds and test-hub pytest load this machine, and fix the setup; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #269 — width 1; implementation/task-outcome width 1**

- #24121 — Restore read-only project-scoped handoff retrieval tools lost from #21792; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #270 — width 1; implementation/task-outcome width 1**

- #24130 — Recover #11235: optional session_id in suggest_next_task, get_messages, list_identities and search_skills skips caller-session scoping; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #271 — width 1; implementation/task-outcome width 1**

- #24132 — Runtime handshake still fails grant credential issuance for live agent runs (28 errors since 2026-10-08); no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #272 — width 1; implementation/task-outcome width 1**

- #24135 — Developer step workflow stays in the claim step after reclaiming a handed-off task, blocking wait_for_agent; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #273 — width 1; implementation/task-outcome width 1**

- #24197 — land_commit gates: approvals survive card changes and author holds, and overlap counts superseded candidates of landed tasks; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #274 — width 1; implementation/task-outcome width 1**

- #24235 — Codex seats die on a stale token after an operator re-sign-in; Gobby should recover them without a manual resume per seat; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #275 — width 1; implementation/task-outcome width 1**

- #24301 — Telegram button callback on an ended session's decision message still routes to the ended session; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #276 — width 1; implementation/task-outcome width 1**

- #24304 — Reviewer proof friction: nextest JUnit write fails in author worktrees, and require-task-before-edit refuses literal scratchpad writes; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #277 — width 1; implementation/task-outcome width 1**

- #24321 — get_agent_definition reports provider 'claude' for a stored None/'inherit' provider; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #278 — width 1; implementation/task-outcome width 1**

- #24403 — Land-gate classifier: crate test-only paths should be no-activation, not cutover; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #279 — width 1; implementation/task-outcome width 1**

- #24416 — Resumed seats are not re-adopted into their workspace pane; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #280 — width 1; implementation/task-outcome width 1**

- #24419 — prefer-gcode-for-source-read blocks reads outside the project root even after gcode reports invalid_path_scope; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #281 — width 1; implementation/task-outcome width 1**

- #24493 — Rule 3 operation exemption can't tell stale ownership from a foreign write made after a merge, rebase, cherry-pick or revert started; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #282 — width 1; implementation/task-outcome width 1**

- #24618 — Under host load, restart and stop pick the heavy fallback path on a single 1 s decision probe; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #283 — width 1; implementation/task-outcome width 1**

- #24623 — #24611 and #24610 review follow-ups: claim release leaves no session_tasks history; stale close-evaluation comments; developer submit wording; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #284 — width 1; implementation/task-outcome width 1**

- #24727 — DevTeam reviewer sandbox denials: cached settings.json rewrite, main-checkout .venv lock, bootstrap.yaml reads, shell rc reads, untargeted network-outbound; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #285 — width 1; implementation/task-outcome width 1**

- #24728 — get_task_diff serves stale per-file blobs instead of the candidate's final content; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #286 — width 1; implementation/task-outcome width 1**

- #24730 — claim_task without an outer session_id can succeed against the wrong session; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #287 — width 1; implementation/task-outcome width 1**

- #24732 — list_tasks candidate=true compact paging skips and elides rows across next_offset pages; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #288 — width 1; implementation/task-outcome width 1**

- #24733 — Status waits match a transient paused state on flapping seats; get_session summary previews go stale; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #289 — width 1; implementation/task-outcome width 1**

- #24750 — Coordination waits: notices omit the owner session, an expired reply wait is reused on re-register, and the first registration after a restart returns DAEMON_UNAVAILABLE; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #290 — width 1; implementation/task-outcome width 1**

- #24792 — Literal-path script writes are not attributed to the running session; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #291 — width 1; implementation/task-outcome width 1**

- #24814 — Hub backup: speed up Qdrant checksum serialization; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #292 — width 1; implementation/task-outcome width 1**

- #24830 — block-gobby-command-word-expansion false positive on read-only python heredoc with braces; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #293 — width 1; implementation/task-outcome width 1**

- #24843 — Pane focus wake gating; waits: #24846; singleton because target inventory is unbounded.

**Step #294 — width 1; implementation/task-outcome width 1**

- #24887 — Agents in auto mode get permission approvals through Telegram and other comms channels; waits: #24604; singleton because target inventory is unbounded.

**Step #295 — width 1; implementation/task-outcome width 1**

- #24888 — Sandbox the Assistant pane; Telegram and other comms channels keep working; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #296 — width 1; implementation/task-outcome width 1**

- #24896 — Code Reviewer procedures: read a Security BOUNCE with get_inter_session_message; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #297 — width 1; implementation/task-outcome width 1**

- #24897 — Reviewer spawn into a developer worktree rebases the branch and changes the candidate SHA; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #298 — width 1; implementation/task-outcome width 1**

- #24898 — Control-event parsing: trailing punctuation on a SHA fails silently, and plan-review CONSENSUS advances only on a single-line message; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #299 — width 1; implementation/task-outcome width 1**

- #24904 — Agent-definition tests: spec validity only; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #300 — width 1; implementation/task-outcome width 1**

- #24930 — require-code-review-skill treats git help and usage invocations as mutations; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #301 — width 1; implementation/task-outcome width 1**

- #24933 — Linking a reviewer to a new unit drops its reviewing rows for parked candidates, so a later BOUNCE on the parked unit fails is_task_reviewer; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #302 — width 1; implementation/task-outcome width 1**

- #24947 — Planning tasks cannot close without the Orchestrator: plan-writer and plan-adversary definitions cannot write the steps their close requires; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #303 — width 1; implementation/task-outcome width 1**

- #25069 — Seat sandbox blocks process listing (ps, pgrep, pkill: sysmond unreachable) although hooks allow column-safe ps; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #304 — width 1; implementation/task-outcome width 1**

- #25132 — Reporter source sections don't match the Archivist digest headings; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #305 — width 1; implementation/task-outcome width 1**

- #25134 — Operator-approved cleanup: restore #24894's rejected test files and delete #25128's probe file; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #306 — width 1; implementation/task-outcome width 1**

- #25196 — Research: what in everything-claude-code (ECC) can Gobby adopt?; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #307 — width 1; implementation/task-outcome width 1**

- #25197 — Software factory follow-ups: definition topology, Orchestrator rule classes, TDD RED records, seat startup, workspace scope, gclient reference; waits: #25172, #25179, #25198, #25218, #25224, #25173, #25177, #25178, #25183, #25200; singleton because target inventory is unbounded.

**Step #308 — width 1; implementation/task-outcome width 1**

- #25221 — Daemon commits agent workspaces nightly to a private GobbyAI GitHub repository; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #309 — width 1; implementation/task-outcome width 0**

- #24818 — Runbooks and task system; waits: #23902, #25129, #25131, #25134, #25146, #25220, #25228, #25232, #25233, #25234, #25238, #24416, #24419, #24493, #24618, #24623, #24727, #24728, #24730, #24732, #24733, #24750, #24780, #24792, #24794, #24814, #24843, #24887, #24888, #24897, #24904, #25081, #25133, #25151, #25152, #25197, #25199, #25221, #25223, #25235, #22660, #22819, #23615, #23818, #23895, #24121, #24130, #24132, #24135, #24197, #24235, #24301, #24304, #24321, #24403, #24830, #24896, #24898, #24930, #24933, #24947, #25069, #25132, #25139, #25145, #25196; phase/domain completion gate.

**Step #310 — width 1; implementation/task-outcome width 1**

- #23729 — Impeccable pass on Gobby Annotate: theme contrast and button surfaces; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #311 — width 1; implementation/task-outcome width 1**

- #24653 — Gobby Annotate Safari: zoom, drag and pointer-mode defects from Josh's device check, plus toolbar orientation and visible-area capture; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #312 — width 1; implementation/task-outcome width 1**

- #24654 — Research: popular GitHub design skills focused on iOS and macOS, for Gobby Annotate's next design pass; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #313 — width 1; implementation/task-outcome width 1**

- #24655 — Research: how iOS developers securely run simulator builds with AI coding agents, best practices with citations; no open prerequisite in the selected graph; singleton because target inventory is unbounded.

**Step #314 — width 1; implementation/task-outcome width 0**

- #24822 — Gobby Annotate; waits: #24653, #24654, #24655, #23729; phase/domain completion gate.

**Excluded open tasks**

These 134 snapshot rows have no completion placement above. Exclusion changes no task state.

| Task | Title | Reason |
| --- | --- | --- |
| #22075 | Detect provider usage outages across all providers and fall back to the next candidate | deferred-by-josh; preserve operator deferral |
| #24839 | Post-1.0: multi-key release signing trust ring and key recovery | explicit post-1.0 release trust-ring work |
| #20203 | Define hosted terminal-relay privacy stance | hosted Story C privacy sidequest; #21542 says sidequests never block 1.0 |
| #25185 | Versioning for every factory surface: definitions, rules, skills and references, runbooks and pipelines | deferred-by-josh; preserve operator deferral |
| #25184 | Recursive self-improvement loop: seat friction becomes fixes to definitions, rules, skills, runbooks and the loop itself | deferred-by-josh; preserve operator deferral |
| #18498 | Post-0.5.0: research-derived enhancements (triaged keeps) | sidequest enhancement root outside the requested roots |
| #21311 | Session-feedback loop v2 | sidequest enhancement root outside the requested roots |
| #21313 | File gated rule/skill/template proposal artifacts from guidance-gap clusters | sidequest enhancement root outside the requested roots |
| #21314 | Add a task_refs column to session_feedback and populate it at intake | sidequest enhancement root outside the requested roots |
| #21315 | Add feedback review run revert and threshold-triggered runs | sidequest enhancement root outside the requested roots |
| #17684 | list_tools tool-count steer: hint toward search_tools/recommend_tools when a server exceeds the selection-accuracy threshold | sidequest enhancement root outside the requested roots |
| #23105 | Plan the Fieldy review-only voice loop and durable Plan/Defer queue | sidequest enhancement root outside the requested roots |
| #18996 | Export daemon traces through standard OTLP configuration | sidequest enhancement root outside the requested roots |
| #22978 | Triage: needs Josh's decision | excluded Triage subtree; no selected-task prerequisite points here (includes #22225) |
| #19880 | Design elevation: bring web UI surfaces to the .impeccable.md meta on consolidated primitives | excluded Triage subtree; no selected-task prerequisite points here (includes #22225) |
| #20151 | Author installer-wizard redesign plan (idempotent reconciler, machine-role fork) | excluded Triage subtree; no selected-task prerequisite points here (includes #22225) |
| #20201 | Plan Gobby plugin system on the public CLI and daemon API | excluded Triage subtree; no selected-task prerequisite points here (includes #22225) |
| #21312 | Add an EDV-style verify stage that mints lesson memories from feedback clusters | excluded Triage subtree; no selected-task prerequisite points here (includes #22225) |
| #23428 | Run the remaining retained CodeRabbit post-push passes p007-p013 for ae78ebdf00 | excluded Triage subtree; no selected-task prerequisite points here (includes #22225) |
| #23718 | Prove the root cause of the 2026-10-06 15:43 event-loop stall from a captured runner stack | excluded Triage subtree; no selected-task prerequisite points here (includes #22225) |
| #18708 | Wire up CLA tooling for external contributions | excluded Triage subtree; no selected-task prerequisite points here (includes #22225) |
| #20997 | Plan gobby-backup: Rust external hub-backup runner with scheduling | excluded Triage subtree; no selected-task prerequisite points here (includes #22225) |
| #21295 | Add bundled MCP template checklist to the install wizard | excluded Triage subtree; no selected-task prerequisite points here (includes #22225) |
| #21972 | Make close validation policies configurable and language-aware | excluded Triage subtree; no selected-task prerequisite points here (includes #22225) |
| #22447 | Decide the Windows cross toolchain for the gobby-terminal vt-engine clippy step | excluded Triage subtree; no selected-task prerequisite points here (includes #22225) |
| #22832 | A2UI decisions surface served over Tailscale (replaces claude.ai-hosted decisions docs) | excluded Triage subtree; no selected-task prerequisite points here (includes #22225) |
| #23952 | Plan OS-level isolation so sandboxed agents cannot read other processes' environments | excluded Triage subtree; no selected-task prerequisite points here (includes #22225) |
| #22225 | Implement Gobby Annotate browser capture and managed MCP integration | excluded Triage subtree; no selected-task prerequisite points here (includes #22225) |
| #22719 | Fieldy voice loop: review-only task queue with Plan/Defer decisions | excluded Triage subtree; no selected-task prerequisite points here (includes #22225) |
| #23591 | Provider quota, per-call usage ledger, and per-session and per-task spend (plan #23099) | only #23603 is a required external leaf; this ancestor/sibling is not a prerequisite |
| #23595 | P4: Provider-neutral usage-limit detection and operator alerts | only #23603 is a required external leaf; this ancestor/sibling is not a prerequisite |
| #23604 | Provider-neutral quota edges to the operator channel | only #23603 is a required external leaf; this ancestor/sibling is not a prerequisite |
| #23813 | Caught-up live transcript quota collection | only #23603 is a required external leaf; this ancestor/sibling is not a prerequisite |
| #23815 | Prompt spawned-terminal hints and verified-source fail-fast | only #23603 is a required external leaf; this ancestor/sibling is not a prerequisite |
| #23816 | Interactive and placed-seat quota observation | only #23603 is a required external leaf; this ancestor/sibling is not a prerequisite |
| #23593 | P2: The ledger records calls, subagents, and reported runs | only #23603 is a required external leaf; this ancestor/sibling is not a prerequisite |
| #23598 | Per-call identity and `api_calls` | only #23603 is a required external leaf; this ancestor/sibling is not a prerequisite |
| #23599 | Claude subagent calls enter the parent ledger | only #23603 is a required external leaf; this ancestor/sibling is not a prerequisite |
| #23600 | Provider-reported run totals | only #23603 is a required external leaf; this ancestor/sibling is not a prerequisite |
| #23596 | P5: Three surfaces, documented | only #23603 is a required external leaf; this ancestor/sibling is not a prerequisite |
| #23605 | Observability guide and skill references | only #23603 is a required external leaf; this ancestor/sibling is not a prerequisite |
| #23594 | P3: The ledger interface | only #23603 is a required external leaf; this ancestor/sibling is not a prerequisite |
| #23601 | `UsageLedgerStore`, `get_usage_ledger`, and `/api/admin/usage/ledger` | only #23603 is a required external leaf; this ancestor/sibling is not a prerequisite |
| #23602 | `gobby tokens ledger` and `gobby tokens quota` | only #23603 is a required external leaf; this ancestor/sibling is not a prerequisite |
| #24238 | Decision classifier path: System One-style adapter across decision providers | classifier work outside roots; its selected consumer #22075 is deferred |
| #24242 | P4: Documentation | classifier work outside roots; its selected consumer #22075 is deferred |
| #24249 | Decision capability guide rows | classifier work outside roots; its selected consumer #22075 is deferred |
| #24241 | P3: Consumers | classifier work outside roots; its selected consumer #22075 is deferred |
| #24246 | MCP tool reranking through Noul | classifier work outside roots; its selected consumer #22075 is deferred |
| #24247 | Found-work confirmation cascade | classifier work outside roots; its selected consumer #22075 is deferred |
| #24248 | Agent-facing `gobby-decisions:evaluate` MCP tool | classifier work outside roots; its selected consumer #22075 is deferred |
| #24253 | Amend decision-classifier-path: defer openai-decisions (gpt-6-luna) until OpenAI Decisions API GA | classifier work outside roots; its selected consumer #22075 is deferred |
| #24285 | OpenAI Decisions API wire after snapshot identity | classifier work outside roots; its selected consumer #22075 is deferred |
| #25163 | Finish the Jev choice-gate rollout: D/G/J study with carry, Josh's ratings, ship, V2 and Box consumer evidence | classifier work outside roots; its selected consumer #22075 is deferred |
| #24250 | Research proven use cases where small decision/classifier models reliably replace LLM calls | classifier work outside roots; its selected consumer #22075 is deferred |
| #24819 | Rust port candidates | deferred Rust-port-candidates root; no required inbound prerequisite |
| #23359 | Reduce worker-thread GIL starvation of the daemon event loop | deferred Rust-port-candidates root; no required inbound prerequisite |
| #24726 | Daemon-internal GOBBY_SUPERVISOR_FD leaks into agent seat environments | deferred Rust-port-candidates root; no required inbound prerequisite |
| #22606 | Memory community detection over memory_crossrefs | deferred Rust-port-candidates root; no required inbound prerequisite |
| #22923 | Include observability compose in the repository dev-container example | deferred Rust-port-candidates root; no required inbound prerequisite |
| #23370 | Retire the transcript evidence pickle codec | deferred Rust-port-candidates root; no required inbound prerequisite |
| #23373 | Add Grok composer rules to its detection manifest so Grok seats regain confirmed-empty live wakes | deferred Rust-port-candidates root; no required inbound prerequisite |
| #24262 | Shared Claude-config writer resets an existing config's mode to 0600 on atomic replace | deferred Rust-port-candidates root; no required inbound prerequisite |
| #24298 | Commit guard ownership check: foreign-commit bypasses via case-folded Git names, trap/env -S/xargs execution, and zsh allexport option forms | deferred Rust-port-candidates root; no required inbound prerequisite |
| #24306 | Research: plan Gobby support for Hermes, OpenClaw, Muse and OpenCode CLIs | deferred Rust-port-candidates root; no required inbound prerequisite |
| #24308 | Research: cut disk usage and disk writes from logging, watchdogs and retained artifacts | deferred Rust-port-candidates root; no required inbound prerequisite |
| #24410 | SRT stopProviderTree: reparented double-fork or setsid descendants stay unmonitored after monitor loss | deferred Rust-port-candidates root; no required inbound prerequisite |
| #24424 | Test harness: test_commit_guard setup costs ~8s per node, so the file needs about 70 minutes serially | deferred Rust-port-candidates root; no required inbound prerequisite |
| #24479 | expand-task refuses an unregistered plan before applying the tree | deferred Rust-port-candidates root; no required inbound prerequisite |
| #24485 | Seats cannot confirm a stopped validation run left no descendants: add a daemon-side descendant inventory | deferred Rust-port-candidates root; no required inbound prerequisite |
| #24515 | Expansion validation fails UNRESOLVED on an executed section's planned-move exact target | deferred Rust-port-candidates root; no required inbound prerequisite |
| #24533 | 0.5.0 pre-ship security audit: provider sandbox profiles, macOS service access and shell write classification | deferred Rust-port-candidates root; no required inbound prerequisite |
| #24588 | Shell write classifier: /usr/bin/time -l <cmd> is blocked as unresolved scope | deferred Rust-port-candidates root; no required inbound prerequisite |
| #24626 | Stdio MCP server stops reconnecting after a call timeout: lightspeed returned "Connection closed" from 05:48 to past 06:05 CDT with no reconnect attempts | deferred Rust-port-candidates root; no required inbound prerequisite |
| #24634 | Read-only grep\|sed pipeline with variable expansion over a sandbox-runtime path is blocked as a provider launch and an unresolved-scope write | deferred Rust-port-candidates root; no required inbound prerequisite |
| #24729 | Scrub ambient GOBBY_TERMINAL_ROUTE and GOBBY_SUPERVISOR_FD in host-shutdown and runner-lifecycle tests | deferred Rust-port-candidates root; no required inbound prerequisite |
| #24745 | Status/health follow-up: SRT rendered-status assertion, retiring-pid liveness past 30 s, #24292 exception type | deferred Rust-port-candidates root; no required inbound prerequisite |
| #24772 | Approval guard: refuse non-builtin git subcommands when a raw word carries a slot hazard | deferred Rust-port-candidates root; no required inbound prerequisite |
| #24778 | send_keys: add a shift_tab named key so Codex collaboration modes can be cycled from Gobby | deferred Rust-port-candidates root; no required inbound prerequisite |
| #24833 | Web approval card labels an execution error as "Rejected" | deferred Rust-port-candidates root; no required inbound prerequisite |
| #18647 | Protect enforcement session variables from direct agent writes | deferred Rust-port-candidates root; no required inbound prerequisite |
| #22407 | Remove the Droid ps shim when Factory fixes factory-execute-supervisor | deferred Rust-port-candidates root; no required inbound prerequisite |
| #23468 | Diagnose the ~30 s shared daemon hook stall at 04:37:32-04:38:08 CT | deferred Rust-port-candidates root; no required inbound prerequisite |
| #23470 | Plan validator's migration carrier set misses assets.rs, runtime_handshake.json and runtime_grants golden fixtures | deferred Rust-port-candidates root; no required inbound prerequisite |
| #23616 | Measure unmodeled_observation_events bloat after the volume fix and own any REINDEX or VACUUM FULL it justifies | deferred Rust-port-candidates root; no required inbound prerequisite |
| #23634 | Project purge guard, soft-deleted checkout admission and tool_schema_hashes CASCADE (#22959 decision a) | deferred Rust-port-candidates root; no required inbound prerequisite |
| #23711 | SRT runtime tests leave node_modules trees that pytest cannot clean (Errno 66 garbage warnings) | deferred Rust-port-candidates root; no required inbound prerequisite |
| #23722 | Shell writes behind option-taking wrappers (nice, env -i, timeout, sudo, xargs) bypass require-task-before-edit | deferred Rust-port-candidates root; no required inbound prerequisite |
| #23731 | Plan hash advances in the DB without regenerating the coverage manifest | deferred Rust-port-candidates root; no required inbound prerequisite |
| #23738 | Agent health logs 'Managed tmux termination failed' for an already-terminal native run | deferred Rust-port-candidates root; no required inbound prerequisite |
| #23756 | require-dart-skill gate fires on Gobby's own .gobby/build.yaml | deferred Rust-port-candidates root; no required inbound prerequisite |
| #23768 | Replace chatterbox-tts with crane for local TTS | deferred Rust-port-candidates root; no required inbound prerequisite |
| #23776 | Defer claude_agent_sdk/mcp.types and the app_context/memory chain off the daemon HTTP import path | deferred Rust-port-candidates root; no required inbound prerequisite |
| #23869 | Operator WS input misses kitty-encoded Enter as a composer submit | deferred Rust-port-candidates root; no required inbound prerequisite |
| #23891 | Step-enforcement skill-gate denial says all declared skills are loaded while required references are still missing | deferred Rust-port-candidates root; no required inbound prerequisite |
| #24048 | block-landing-override fires when the override variable name appears only as data in a command | deferred Rust-port-candidates root; no required inbound prerequisite |
| #24051 | Remove the unreachable wrapper backfill block in request_context.py; trace the stale sender PID on UserPromptSubmit envelopes | deferred Rust-port-candidates root; no required inbound prerequisite |
| #24066 | falkor.sh heredoc fails under the reviewer SRT sandbox, failing test_pre_push_retains_managed_falkor_fallback | deferred Rust-port-candidates root; no required inbound prerequisite |
| #24125 | Settle orphan reaping before the final killpg check in test_timeout_reaps_process_group | deferred Rust-port-candidates root; no required inbound prerequisite |
| #24128 | Recover #19004 CodeRabbit packet 06 review-learning hardening that a2b779f60f removed | deferred Rust-port-candidates root; no required inbound prerequisite |
| #24136 | Recover #11243: CLI diagnostic probe after SDK text-generation failure | deferred Rust-port-candidates root; no required inbound prerequisite |
| #24157 | Cron job gobby:feedback-review stuck since 2026-09-25: enabled but next_run_at null after one failure | deferred Rust-port-candidates root; no required inbound prerequisite |
| #24168 | Write attribution records git rm/git mv glob operands as literal paths | deferred Rust-port-candidates root; no required inbound prerequisite |
| #24170 | Daemon start and cutover failure paths: leave no launched process behind, and no raw traceback on a bad workspace pin | deferred Rust-port-candidates root; no required inbound prerequisite |
| #24173 | Plan: multi-channel communications core, then Slack and Discord adapters | deferred Rust-port-candidates root; no required inbound prerequisite |
| #24177 | Shell attribution: bash -l/-i/--login/--rcfile/--init-file keep pipeline_subshell though startup files can enable lastpipe | deferred Rust-port-candidates root; no required inbound prerequisite |
| #24190 | Step-scope get_variable reports a definition-defaulted key as missing until the next instance save | deferred Rust-port-candidates root; no required inbound prerequisite |
| #24212 | Communications channel init at daemon startup has no retry after a transient connect error | deferred Rust-port-candidates root; no required inbound prerequisite |
| #24257 | Remaining NAME=value prefix regexes miss NAME+=value; route them through the shared #24144 assignment pattern | deferred Rust-port-candidates root; no required inbound prerequisite |
| #24264 | Generic send_attachment lets a caller redirect delivery via metadata platform_destination | deferred Rust-port-candidates root; no required inbound prerequisite |
| #24286 | Broad-read guard: xargs head readers fed by stdin redirect are unclassified | deferred Rust-port-candidates root; no required inbound prerequisite |
| #24297 | Shell scanner: scan_function_program fragment rescans diverge from a full scan, and cached ShellToken objects are spliced twice | deferred Rust-port-candidates root; no required inbound prerequisite |
| #24299 | Provider launch guard: zsh repeat and multi-name for loops bypass the guard | deferred Rust-port-candidates root; no required inbound prerequisite |
| #24309 | no-remote-exec: pre-existing executed-fetch gaps (compound pipes, zsh =(), sort/tar options) | deferred Rust-port-candidates root; no required inbound prerequisite |
| #24311 | Shell write classifier: exec prefix, bsdtar/gtar names and tar create/append fail open | deferred Rust-port-candidates root; no required inbound prerequisite |
| #24316 | Resumed interactive Droid seats relaunch as droid exec | deferred Rust-port-candidates root; no required inbound prerequisite |
| #24396 | Shell normalization rejects zsh -O stdin and over-reports an unknown-scope write | deferred Rust-port-candidates root; no required inbound prerequisite |
| #24397 | Rule false positives: no-remote-exec blocks a localhost health poll; edit-write-recovery blames daemon-unavailable proxy failures | deferred Rust-port-candidates root; no required inbound prerequisite |
| #24406 | bin_release_identity._crate_closure raises ValueError for an out-of-workspace path dependency | deferred Rust-port-candidates root; no required inbound prerequisite |
| #24407 | Composer ledger: recalled history text is not marked draft; stale write_coordinator docstring | deferred Rust-port-candidates root; no required inbound prerequisite |
| #24413 | Test and process hygiene: e2e leaves a gdaemon orphaned; native-handle exits report late | deferred Rust-port-candidates root; no required inbound prerequisite |
| #24415 | Droid ps shim is lost in login zsh: path_helper moves /bin ahead of the cargo shell overlay | deferred Rust-port-candidates root; no required inbound prerequisite |
| #24513 | Check xAI upstream for the Grok post-compaction freeze fix, then remove the #24417 watcher | deferred Rust-port-candidates root; no required inbound prerequisite |
| #24560 | Shell write-scope classifier: bash arithmetic evaluates a variable's value, so a[$(cmd)] runs cmd in commands classified as reads | deferred Rust-port-candidates root; no required inbound prerequisite |
| #24771 | Telegram: daemon acknowledges an inbound operator DM with an emoji reaction on delivery | deferred Rust-port-candidates root; no required inbound prerequisite |
| #24774 | plans validate: consumer-coverage lint reports warnings inconsistently across code-index refresh state | deferred Rust-port-candidates root; no required inbound prerequisite |
| #24775 | Wake composer gate: document and test the operator-comms bypass of draft and unknown composer states | deferred Rust-port-candidates root; no required inbound prerequisite |
| #24781 | Research: daemon-tracked weekly usage remaining per supported CLI account, Archivist reporting, and multi-account balancing | deferred Rust-port-candidates root; no required inbound prerequisite |
| #24789 | Composer ledger misses DECXCPR, ANSI-mode DECRPM and replies split across writes | deferred Rust-port-candidates root; no required inbound prerequisite |
| #24793 | Plan consumer-coverage lint misses test files that mock a Target module | deferred Rust-port-candidates root; no required inbound prerequisite |
| #24795 | Self-review gate ignores uv run ocr and cwd-prefixed ocr calls | deferred Rust-port-candidates root; no required inbound prerequisite |
| #24915 | Make dirty-path approval subprocess-timeout tests load-tolerant | outside requested/queued roots and no selected-task prerequisite |
| #24936 | Rebuild the web wiki UI (page reader removed in #21816), reusing MermaidBlock for diagrams | outside requested/queued roots and no selected-task prerequisite |
| #24934 | MermaidBlock has no production importer: delete it and the mermaid dependency, or wire it into the wiki UI | outside requested/queued roots and no selected-task prerequisite |

**Validation performed**

- Snapshot: 624 rows and 624 unique refs. Selected: 490; excluded: 134; union covers the snapshot exactly once.
- Sequence: 490 current tasks, 490 unique placements, no duplicate placement. Four additional reserved steps represent proposed outcomes, not existing task IDs.
- Every modeled prerequisite precedes its consumer; zero dependency-order violations. Existing graph plus parent completion obligations is acyclic.
- Zero known production-Target collisions within post-drain parallel sets. Twenty-two ready-task overlap holds are recorded in the companion. Shared release carriers require serialized integration; incomplete inventories conservatively serialize tasks.
- No code edits, task edits/creation, daemon DB/HTTP access, secret-file reads, process kills or other-session resource changes. No product tests or host-load measurements were run. Research data consistency was checked, not product readiness.

### Risks

- **Scope is broader than proven shipping necessity.** Named-root secondary work stays visible, but several tasks lack a demonstrated 1.0 requirement. A scope decision is needed before this conservative envelope becomes a mandatory shipping checklist.
- **The parity gate is conditional.** Node CLI task slicing, the #24603 warning-only behavior, final-suite parenting, Windows support and latency approval remain real blockers. The numbered placeholder must not be interpreted as a completed or approved test plan.
- **Task boundaries are still coarse.** 1.0 contains unexpanded epics. New leaves, refreshed v4 plans, accepted folds or changed dependencies require recomputing the sequence; 314 is a snapshot-specific count.
- **File overlap evidence is incomplete.** Singleton ordering protects unknown inventories. The eight already-running teams and shared release-carrier landings require their existing coordination. No claim of optimal parallelism is supported.
- **Live state changes.** Task closure and source changes after the read snapshot can invalidate ordering and acceptance evidence. Re-read the next step before dispatch, preserve Josh's deferrals and update the reference after completion.
- **Promotion and approval are separate.** This report is being committed under the repaired researcher workflow. The linked SHA and implementation/closing records do not constitute an approved review; the reviewing team or Orchestrator must supply that review and own closure.
