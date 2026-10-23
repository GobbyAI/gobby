# Task tree reorg, revised to Josh's 2026-10-21 rulings

Task #24817. Researcher gobby#17359, 2026-10-22 (UTC 2026-10-23). This revises `.gobby/plans/research/task-tree-reorg-proposal-2026-10-21.md` (#24783) to the rulings in memory a68f8994 and the critical path in memory c3fa8eb8. The moot list comes from `.gobby/plans/research/moot-tasks-after-astra-2026-10-21.md` (#24782).

## Summary

- The live tree has 520 open tasks under 11 roots. The Orchestrator should know about these changes since the 2026-10-21 proposal:
  - #23341 Remove gobby build closed at 2026-10-22 13:15Z. Its lane epic, #24317, closed at the same time and has no open children.
  - #18784, #21528 and #18580 in #18498 are closed. So are the research spikes #24782 and #24783.
  - About 20 tasks were filed after the proposal (#24787–#24816). Each has a row below.
  - **#17438 Fleet management is still open**, with one open descendant. Memory c3fa8eb8 and this task's card both say it was closed as superseded by #21542. The Orchestrator should close it; this pass does not touch it.
- The G1–G10 domain roots are gone. The rulings set the roots instead. Under B1, expanded plan epics stay top-level. Under B6, Rust and Python are kept apart, and Python work is limited to the critical path.
- Counts:
  - 183 open direct children of the seven lane epics, plus the root orphan #24763, make 184 rows.
  - 158 rows are in Operations and 26 are in the Moot batch. Each task appears exactly once.
  - 76 rows go to Rust port candidates, the deferred root, and 51 go to Runbooks and task system. Most of the loose Python backlog is parked in the deferred root.

## Decision rule for Python leaves

Every Python leaf was judged by this rule, which comes from B6 and c3fa8eb8:

1. **Runbooks and task system (active).** The task changes something the v4 runbooks run on:
   - seat definitions, seat lifecycle, handoff or coordination;
   - task tools;
   - the close invariants, in particular invariant 3 ownership;
   - landing;
   - DevTeam worktrees.

   A Python fix on the critical path also goes here: one that keeps seats or the hub working right now (hub credentials and backup, a red CI on 0.5.0, a rule that blocks routine seat commands). So does anything claimed or already in flight. A deferred root cannot hold live work.
2. **Rust port candidates (deferred).** Every other Python leaf goes here. That includes the shell-classifier and rule-engine bugs, which live in `src/gobby/hooks/` (Python). They are not `crates/ghook` work. The same goes for Python daemon-stability work that the gdaemon port replaces, plan tooling, and off-path features.

Rust leaves go to the Rust root for their crate. The language was taken from the paths in each task's description where its title was ambiguous: #24816, #24814, #24806, #24763, #23359 and `scan_function_program` in `src/gobby/hooks/_normalization_compound.py`.

## Roots

| Root | Lang | Scope | Create? |
|---|---|---|---|
| **Runbooks and task system** | Python | Single unexpanded tasks. Covers the v4 runbooks (#24797), seat definitions and lifecycle, handoff and coordination, task tools, close invariants, landing and DevTeam worktrees. Also holds the few critical-path Python fixes that keep seats and the hub working now. | Yes (Orchestrator) |
| **Rust port candidates** | Python (deferred) | Python tasks off the critical path. Each may become Rust work after the port. Not worked until the port. | Yes (Orchestrator) |
| **Code index** | Rust | `crates/gcode`: indexing, freshness, evidence, hub credentials as gcode sees them. | Yes |
| **Terminal and client** | Rust | `crates/gterminal*` and `crates/gclient`: panes, host lifecycle, client UI, pane restore. | Yes |
| **Hooks** | Rust | `crates/ghook`. B6 confirmed this root, but no open loose leaf targets ghook today. The Python hook-rule bugs are port candidates. | Create when the first leaf exists |
| **Daemon port** | Rust | `crates/gdaemon` work outside #21542. B6 confirmed this root, but no open loose leaf qualifies: the gdaemon bounded-run leaves #24550 and #24551 are dropped by A3. | Create when the first leaf exists |
| **Gobby Annotate** | Web/iOS | Per B3, holds #24653, #24654, #24655 and #23729. The Orchestrator does not move #22225 (see the note below). | Yes |

These stay top-level as they are (B1): #22643, #23561, #23670, #23433, #22948, #23220, #23570, #24238 and #23591. Two leaves join an epic that stays top-level: #23669 goes under #22643, and #22604 goes under #24238.

These are untouched: #21542 (c3fa8eb8), #18498 (B4), #22978 and #22225 (Josh's), and #17438 (see Summary).

**#22225 (Josh's):** the proposed op is #22225 from #22978 to Gobby Annotate. Only Josh makes this move. Until he does, Gobby Annotate holds the four leaves and #22225 stays under #22978.

The seven lane epics #22691, #22801, #22949, #23378, #24093, #24094 and #24317 retire once they are empty. #24317 is already closed. #22691 empties only after #24797 has moved to Runbooks and task system.

## Operations

Key:
- **RT** = Runbooks and task system.
- **PC** = Rust port candidates.
- **CI** = Code index.
- **TC** = Terminal and client.
- **AN** = Gobby Annotate.
- **top-level** = the epic loses its lane parent and becomes a root, keeping its whole subtree.
- A *fold* note means the scope overlaps another row. The Orchestrator folds it into the named task when it moves it.

### From #22691 Lane 1 - Runbooks (49)

| Task | Title (short) | Current parent | New parent | Reason |
|---|---|---|---|---|
| #24797 | v4 runbooks implementation (claimed) | #22691 | RT | The v4 work itself |
| #22643 | Handoff stop gates and pull order | #22691 | top-level | Expanded epic (B1). Trim the stale `gobby build` text |
| #23561 | Content-based ownership (epic) | #22691 | top-level | Expanded epic (B1). Invariant 3 |
| #23670 | Per-spawn SRT network profile (epic) | #22691 | top-level | Expanded epic (B1) |
| #22660 | Auto-clear role sessions between tasks | #22691 | RT | A4: extend to researcher and Admin seats |
| #24590 | Gate 9 live-interval attribution | #22691 | RT | Invariant 3 |
| #24493 | Rule 3 exemption: stale ownership vs foreign write | #22691 | RT | Invariant 3 attribution |
| #24197 | land_commit approval and overlap gates | #22691 | RT | Landing; re-scope to §2.4 |
| #24399 | Claim release for attribution-only claim | #22691 | RT | Task tools; check against §3.2 |
| #24623 | Claim-release history; stale comments | #22691 | RT | Task tools (claim history) |
| #24640 | Direct-commit guard leaves checkout dirty | #22691 | RT | Landing path |
| #24728 | get_task_diff stale blobs | #22691 | RT | Reviewers rely on it |
| #24732 | list_tasks candidate paging | #22691 | RT | Task tools |
| #24722 | send_message protocol-line rejection | #22691 | RT | Seat coordination; re-check against §3.5 roles |
| #24720 | Plan Adversary CONSENSUS latch | #22691 | RT | Planning seats kept by v4. #24776 duplicates it |
| #24643 | wait_for_coordination re-register | #22691 | RT | Coordination waits |
| #23895 | Carry a seat across CC re-ID | #22691 | RT | Seat lifecycle |
| #23818 | Held Codex /compact submission | #22691 | RT | Seat compaction and continuity |
| #24304 | Reviewer proof friction | #22691 | RT | Reviewers run tests (Decision 3) |
| #24806 | Reused-worktree spawn rebases linked SHAs | #22691 | RT | DevTeam worktrees; merge-only ruling |
| #24804 | command-word-expansion rule false positive | #22691 | RT | Blocks routine seat commands. It blocked this session. Fold #24809 into it |
| #24419 | prefer-gcode read guard outside root (parked, has commit) | #22691 | RT | Seat-blocking rule with work in flight |
| #24803 | Reporter procedure over skill ceiling | #22691 | (close) | Duplicate of #24790. Confirm the descriptions match, then close |
| #23373 | Grok composer rules for live wakes | #22691 | PC | Off-path seat provider |
| #24262 | Claude-config writer resets mode | #22691 | PC | Off-path Python |
| #24298 | Commit guard bypass forms | #22691 | PC | Python hook hardening |
| #24479 | expand-task unregistered plan | #22691 | PC | Plan tooling, off path |
| #24485 | Daemon-side descendant inventory | #22691 | PC | Its heavy-grant motivation is gone (§4.1) |
| #24588 | /usr/bin/time classifier | #22691 | PC | Python shell classifier |
| #24634 | grep\|sed pipeline classifier | #22691 | PC | Python shell classifier |
| #24772 | Approval guard slot hazard | #22691 | PC | Python hook hardening |
| #18647 | Protect enforcement variables | #22691 | PC | Python rule engine |
| #23722 | Option-taking wrapper writes | #22691 | PC | Python shell classifier |
| #23869 | Operator WS kitty Enter | #22691 | PC | Python WS input; may become gterm work |
| #23891 | Skill-gate denial wording | #22691 | PC | Python rule engine |
| #24048 | block-landing-override data match | #22691 | PC | Python rule engine |
| #24168 | git rm/mv glob attribution | #22691 | PC | Python shell attribution |
| #24177 | bash -l lastpipe attribution | #22691 | PC | Python shell attribution |
| #24190 | Step-scope get_variable default | #22691 | PC | Python workflow engine; step workflows rewritten |
| #24257 | NAME+=value prefix regexes | #22691 | PC | Python shell classifier |
| #24286 | xargs head reader | #22691 | PC | Python shell classifier |
| #24297 | scan_function_program rescans | #22691 | PC | `src/gobby/hooks/_normalization_compound.py` |
| #24299 | Provider launch guard loops | #22691 | PC | Python hook hardening |
| #24309 | no-remote-exec gaps | #22691 | PC | Python hook hardening |
| #24311 | Write classifier exec/tar | #22691 | PC | Python shell classifier |
| #24316 | Droid seats relaunch as exec | #22691 | PC | Off-path seat provider |
| #24396 | zsh -O stdin normalization | #22691 | PC | Python shell classifier |
| #24397 | no-remote-exec localhost; edit-write-recovery | #22691 | PC | Python rule false positives, not seat-blocking |
| #24560 | Arithmetic a[$(cmd)] classifier | #22691 | PC | Python shell classifier |

### From #22801 Lane 4 - Rust (18)

| Task | Title (short) | Current parent | New parent | Reason |
|---|---|---|---|---|
| #23433 | Worktree spawn code-index time (tree) | #22801 | top-level | Expanded (B1). Verify snapshot-only indexing first (plan line 40) |
| #22948 | Code index communities (epic) | #22801 | top-level | Expanded epic (B1) |
| #23220 | gclient daemon resilience (epic) | #22801 | top-level | Expanded epic (B1) |
| #23570 | Code-index vector reconciliation (epic) | #22801 | top-level | Expanded epic (B1) |
| #24238 | Decision classifier path (epic) | #22801 | top-level | Expanded epic (B1). Non-critical Rust (c3fa8eb8) |
| #22604 | Gate generated names through Jev choice | #22801 | #24238 | Blocked by #24244, which is inside #24238 |
| #23902 | Cargo and test-hub load research (parked by Josh) | #22801 | RT | Kept by v4 Decision 11. Keep its escalation |
| #24802 | gcode freshness lock wait | #22801 | CI | gcode |
| #23874 | gcode managed credential state | #22801 | CI | gcode |
| #24029 | gcode evidence complete_empty | #22801 | CI | gcode |
| #24731 | gcode evidence blob cache | #22801 | CI | gcode |
| #22360 | Windows clippy for gterminal | #22801 | TC | gterminal lint |
| #23438 | Codex terminal misbinding | #22801 | TC | Terminal binding |
| #23445 | send_text native pane drops head | #22801 | TC | gterm pane input |
| #24228 | gterminals fixtures in sandbox | #22801 | TC | gterminals |
| #24324 | Host epochs PID reuse | #22801 | TC | gterminal host |
| #24288 | Rust CI torch wheel on CPython 3.15 | #22801 | RT | Red CI on 0.5.0. Fold into #24780, which lists the same torch wheel failure |
| #24406 | bin_release_identity out-of-workspace dep | #22801 | PC | Python install tooling; no current trigger |

### From #22949 Lane 7 - Planning/research (4)

| Task | Title (short) | Current parent | New parent | Reason |
|---|---|---|---|---|
| #23669 | Staleness check on #22643 plan | #22949 | #22643 | Travels with its plan epic |
| #22819 | Atomic description append | #22949 | RT | Task tools |
| #22606 | Memory community detection | #22949 | PC | Blocked by #21563, a port item; becomes Rust work |
| #23768 | chatterbox-tts to crane | #22949 | PC | Off path |

### From #23378 Lane 6 - Everything else (32)

| Task | Title (short) | Current parent | New parent | Reason |
|---|---|---|---|---|
| #24817 | This research spike | #23378 | (close) | Closes when this report is delivered |
| #24794 | Research-spike close needs commit | #23378 | RT | Task tools, researcher close |
| #24811 | Docs: gcode --project sibling reads (has commit) | #23378 | RT | In flight |
| #22926 | Temporary notes outside checkout | #23378 | RT | Seat definitions |
| #24121 | Project-scoped handoff retrieval | #23378 | RT | Handoff |
| #24130 | Optional session_id skips scoping | #23378 | RT | Messaging and task-tool scoping |
| #24301 | Telegram callback to ended session | #23378 | RT | Operator decision routing that the Assistant uses |
| #24321 | get_agent_definition provider | #23378 | RT | Seat definitions |
| #24790 | Reporter procedure over skill ceiling | #23378 | RT | Role content. #24803 duplicates it |
| #24809 | command-word-expansion on path operands | #23378 | RT | Fold into #24804 (same rule) |
| #24303 | CI on 0.5.0 red | #23378 | RT | Landing needs green CI. Fold into #24780 |
| #24106 | tests/agents failures on 0.5.0 | #23378 | RT | Fold into #24780, which lists the tests/agents failures |
| #24776 | CONSENSUS on multi-line message | #23378 | (close) | Duplicate of #24720 (#24782 report) |
| #23591 | Provider quota and usage ledger (epic) | #23378 | top-level | Expanded epic (B1) |
| #22923 | Observability dev-container compose | #23378 | PC | Off path |
| #24306 | Research: Hermes/OpenClaw/Muse/OpenCode | #23378 | PC | Off path |
| #24515 | Expansion validation planned-move | #23378 | PC | Plan tooling |
| #24778 | send_keys shift_tab | #23378 | PC | Off path |
| #22407 | Droid ps shim removal | #23378 | PC | Waits on Factory |
| #23470 | Plan validator carrier set | #23378 | PC | Plan tooling |
| #23711 | SRT node_modules cleanup | #23378 | PC | Test hygiene |
| #23731 | Plan hash vs coverage manifest | #23378 | PC | Plan tooling |
| #23756 | require-dart-skill basename | #23378 | PC | Python rule; Gobby's own trigger is gone |
| #24125 | killpg reap settle | #23378 | PC | Test hygiene |
| #24128 | Review-learning hardening recovery | #23378 | PC | Off path |
| #24136 | CLI diagnostic probe recovery | #23378 | PC | Off path |
| #24264 | send_attachment destination redirect | #23378 | PC | Off-path comms hardening |
| #24173 | Plan: multi-channel comms | #23378 | PC | Off path |
| #24774 | plans validate lint consistency | #23378 | PC | Plan tooling |
| #24781 | Research: weekly usage per CLI account | #23378 | PC | Off path, next to #23591 |
| #24793 | Consumer-coverage lint mocks | #23378 | PC | Plan tooling |
| #24795 | Self-review gate ocr forms | #23378 | PC | Python rule |

### From #24093 Lane 7 - Stability and restart reliability (48)

| Task | Title (short) | Current parent | New parent | Reason |
|---|---|---|---|---|
| #24813 | Stop paths refuse dirty templates (claimed) | #24093 | RT | In flight; release restarts |
| #24816 | Managed grant re-handshake (claimed) | #24093 | RT | Critical path: hub credentials for live seats |
| #24132 | Runtime handshake grant failures | #24093 | RT | Same path as #24816. Fold if #24816's fix covers it |
| #24814 | Hub backup Qdrant checksum | #24093 | RT | Hub; a 36-minute backup blocks daemon starts |
| #24766 | Command-word-expansion regex hang (activation receipt) | #24093 | RT | In flight; landed fix awaiting close |
| #24780 | CI red on every 0.5.0 push | #24093 | RT | Landing needs green CI. Fold target for #24303, #24106 and #24288 |
| #24725 | Handoff delivery under cutover hold | #24093 | RT | Releases restart (Decision 10) |
| #24169 | Stop hook during restart hold | #24093 | RT | Releases restart |
| #24618 | Restart heavy fallback on one probe | #24093 | RT | Releases restart |
| #24730 | claim_task wrong session | #24093 | RT | Task tools |
| #24743 | list_tasks compact elision | #24093 | RT | Task tools. Fold with #24732 |
| #24733 | Status waits on flapping seats | #24093 | RT | Coordination waits |
| #24750 | Wait notices omit owner | #24093 | RT | Coordination waits |
| #24416 | Resumed seats not re-adopted into pane | #24093 | RT | Seat lifecycle |
| #24235 | Codex seats die on stale token | #24093 | RT | Seat recovery |
| #24792 | Literal-path script writes unattributed | #24093 | RT | Invariant 3 attribution |
| #24742 | WORKTREE_CREATE success on sidecar failure | #24093 | RT | DevTeam worktrees |
| #23615 | Worktree rollback ignores failed removal | #24093 | RT | DevTeam worktrees |
| #24787 | gcode grep blocks after restart | #24093 | CI | gcode |
| #23359 | Worker-thread GIL starvation | #24093 | PC | Python daemon; the gdaemon port replaces it |
| #24726 | GOBBY_SUPERVISOR_FD leak | #24093 | PC | Off path |
| #23370 | Retire transcript pickle codec | #24093 | PC | Off path |
| #24308 | Research: disk usage and writes | #24093 | PC | Off path |
| #24410 | SRT stopProviderTree descendants | #24093 | PC | Off path |
| #24424 | test_commit_guard setup cost | #24093 | PC | Test hygiene |
| #24533 | 0.5.0 pre-ship security audit | #24093 | PC | Off path |
| #24536 | Research: MCP output fields | #24093 | PC | Off path |
| #24539 | Remove MCP output fields | #24093 | PC | Off path (blocked by #24536) |
| #24626 | Stdio MCP reconnect | #24093 | PC | Off path |
| #24729 | Scrub ambient env in tests | #24093 | PC | Test hygiene |
| #24745 | Status/health follow-up | #24093 | PC | Off path |
| #23468 | ~30 s hook stall diagnosis | #24093 | PC | Python daemon |
| #23616 | unmodeled_observation_events bloat | #24093 | PC | Off path |
| #23634 | Project purge guard (blocked by #23615) | #24093 | PC | Off path |
| #23738 | tmux termination log noise | #24093 | PC | Off path |
| #23776 | Defer imports off HTTP path | #24093 | PC | Python daemon |
| #24051 | request_context backfill block | #24093 | PC | Python daemon |
| #24066 | falkor.sh under reviewer SRT | #24093 | PC | Off path |
| #24157 | feedback-review cron stuck | #24093 | PC | Off path |
| #24170 | Start and cutover failure paths | #24093 | PC | Python daemon start; gdaemon takes it over |
| #24212 | Comms init retry | #24093 | PC | Off path |
| #24407 | Composer ledger recalled history | #24093 | PC | Off path |
| #24413 | e2e orphan gdaemon; late exits | #24093 | PC | Test hygiene |
| #24415 | Droid ps shim in login zsh | #24093 | PC | Off path |
| #24513 | xAI Grok freeze watcher (parked by Josh) | #24093 | PC | Waits on upstream. Keep its escalation |
| #24771 | Telegram reaction on delivery | #24093 | PC | Off path |
| #24775 | Wake composer bypass docs/test | #24093 | PC | Off path |
| #24789 | Composer ledger DECXCPR | #24093 | PC | Off path |

### From #24094 Lane 8 - Client chrome and UI (6)

| Task | Title (short) | Current parent | New parent | Reason |
|---|---|---|---|---|
| #24815 | gclient scroll/type during outage (claimed) | #24094 | TC | gclient |
| #24638 | gclient sidebar agents without task | #24094 | TC | gclient |
| #24653 | Annotate Safari defects | #24094 | AN | B3 |
| #24654 | Research: iOS/macOS design skills | #24094 | AN | B3 |
| #24655 | Research: simulator builds with agents | #24094 | AN | B3 |
| #23729 | Impeccable pass on Annotate (parked by Josh) | #24094 | AN | B3. Keep its escalation and wt-task-23729 |

### From #24317 Lane 9 - Remove gobby build (0)

There are no open children. #24317 and #23341 are closed.

### Root orphans (1)

| Task | Title (short) | Current parent | New parent | Reason |
|---|---|---|---|---|
| #24763 | Restore operator panes after reboot | (root) | TC | Pane and session restore through gterm/gclient. Josh's requirement |

## Moot batch for 6.1

Per A6, these close all at once when runbook v4 §6.1 lands (the #24797 activation). They are not part of this pass and are not reparented. Each closes with a no-work disposition citing the v4 section.

Rows marked *verify* need their one check first. If a check shows a row is still live, it moves to RT instead of closing.

Three rows are dropped by a ruling rather than by v4: #24538 P2/P4, #24534 and #24617. They are batched here so the closes happen in one pass.

| Task | Current parent | Why | If verify fails |
|---|---|---|---|
| #24494 | #22691 | Gate 12 deleted (§1.2) | |
| #24748 | #22691 | Gate 12 deleted (§1.2) | |
| #23868 | #22691 | Gate 10 deleted (§1.1, §1.4) | |
| #24085 | #22691 | Close artifact resolution retired (§1.4) | |
| #24509 *verify* | #22691 | Preview uses the four-invariant evaluator (§1.1); re-time it | RT |
| #24295 | #22691 | Automatic close review retired (§1.3) | |
| #24031 | #22691 | Authority-transfer relays retire (§2.4) | |
| #24646 *verify* | #22691 | Atomic verdicts; receipt kinds removed (§2.3, §2.4) | RT |
| #24403 *verify* | #22691 | Activation receipt leaves landing (§2.4); confirm release still classifies activation | RT |
| #24656 | #22691 | Only the Assistant and Triage message the Orchestrator (§3.4) | |
| #24135 *verify* | #22691 | Definitions rewritten (§3.2, §3.5) | RT |
| #24398 *verify* | #22691 | Park relay replaced (§2.3, §3.5); confirm `code-reviewer.yaml` has no park block | RT |
| #23817 | #22949 | Superseded state task; the plan reconciles it against Decision 7 at §6.1 (plan line 40, §6.1 text) | |
| #23542 (tree: #23707, #23544) | #23378 | Reviewer landing replaced by Dev Manager landing (§2.4, §3.5) | |
| #23640 (tree: #23642) | #23378 | Close gate 8 gone (§1.1) | |
| #24779 | #23378 | `enforce-tdd-block.yaml` deleted (§1.2) | |
| #24777 | #23378 | `tdd_write_authorization.py` deleted (§1.2) | |
| #24791 | #23378 | New since #24782. Criterion command spans exist only for close-gate crediting, which v4 removes (§1.1, §1.4) | |
| #24538 (tree: #24545 with #24550–#24553; #24546 with #24555, #24556; #24547 with #24557) | #24093 | P3 Gate 10 reuse is moot (§1.1, §1.4). P2 launcher and P4 docs are dropped by A3 | |
| #24534 | #24093 | Research for the A3-dropped launcher | |
| #24617 | #24093 | Dropped by A2: reviewers ask the Orchestrator for host e2e runs. Also moot (§1.1, §4.1) | |
| #24498 | #24093 | Gate 12 deleted (§1.2) | |
| #24497 *verify* | #24093 | Gate 10 deleted; confirm it is not §1.4's found-work detection | RT |
| #24736 | #24093 | Preview contract replaced (§1.1) | |
| #24723 *verify* | #24093 | Close review retired (§1.3); confirm DevTeam reviewers do not hit the same denials | RT |
| #24727 *verify* | #24093 | Same as #24723 | RT |

These rows from the #24782 moot list are no longer in the batch:
- #18784, #21528 and #18580 are already closed.
- #22660 is kept by A4. It is in Operations, under RT.
- #21972 is Josh's, in #22978. It is listed only.

## Unresolved

None. Every row was decided from rulings A2–A6 and B1–B6 (memory a68f8994), the critical path (memory c3fa8eb8) and the v4 plan text. Three items are actions the Orchestrator performs, not open questions:

1. Close #17438, which is still open.
2. Confirm that #24803 duplicates #24790, then close #24803.
3. Make the fold edits named in Operations.
