# Task tree reorganization after the lane paradigm

Task #24783. Researcher gobby#17246, 2026-10-21. Integration tip `ac3182b54f` on `0.5.0`. Companion: `.gobby/plans/research/moot-tasks-after-astra-2026-10-21.md` (#24782).

## Summary

The open tree has 530 tasks under 12 roots. Seven roots are "Lane N" epics: #22691, #22801, #22949, #23378, #24093, #24094 and #24317. Together they hold 285 tasks. Two of them, #22949 and #24093, are both called "Lane 7".

Proposal:

- Retire all seven lane epics.
- Move each of their direct children into one of ten domain roots, G1–G10.
- Plan-expanded subtrees move whole.
- The 37 tasks ruled moot in #24782 close instead of moving.
- These roots stay as they are:
  - #21542, Gobby 1.0. It is a product epic and already contains #21575 Hub mode.
  - #18498, Post-0.5.0.
  - #22978, Triage. It is Josh's, and so is #22225 inside it.
  - #17438, Fleet management.
- 150 tasks move: 10 whole subtrees and 140 single leaves.
  - 149 are direct children of lane epics. Six of them are *verify* rows that move provisionally (see Reparent operations).
  - One is the root-level orphan #24763.
- 18 direct children close as moot instead of moving, and the two research spikes close. Six more *verify* rows move with their domain and close only if their check confirms they are moot.
- The ten domain epics need to be created by the Orchestrator.
- Timing matters: `project_policy.lanes` and the `lane-N` labels still route seats today. The move should land with runbook v4 §6.1, which retires the old runbooks, or after it.

## Current tree (counts per lane epic)

Counts are open tasks including the root, from a full `list_tasks(closed=false)` paging on 2026-10-21. The total is 530.

| Root | Title | Open tasks | Notes |
|---|---|---|---|
| #21542 | Gobby 1.0: gdaemon front door, strangler port, hub and node | 211 | Stage 2 #21544 has 174; Stage 3 #21545 has 4; Stage 4 #21546 has 28 (includes #21575 Hub mode and #17436 Node mode); 4 direct leaves. Listed in policy as a lane epic. |
| #21555 | API keys and node registration | 0 | Closed 2026-10-14, but still listed in `project_policy` lanes. |
| #21575 | Hub mode | (inside #21542) | A plan epic nested in Stage 4. Listed in policy as a lane epic. |
| #22691 | Lane 1 - Runbooks | 65 | 58 direct children; the #23561 subtree (5) and #23670 subtree (1). |
| #22801 | Lane 4 - Rust | 74 | Subtrees #23433, #23220, #23570, #24238 and #22948. |
| #22949 | Lane 7 - Planning/research | 6 | Not in `project_policy` lanes. |
| #23378 | Lane 6 - Everything else | 53 | Subtrees #23542, #23640 and #23591. |
| #24093 | Lane 7 - Stability and restart reliability | 63 | Subtree #24538 (10). |
| #24094 | Lane 8 - Client chrome and UI | 6 | |
| #24317 | Lane 9 - Remove gobby build | 19 | Only #23341 and its subtree. |
| #18498 | Post-0.5.0: research-derived enhancements | 14 | Not a lane. |
| #22978 | Triage: needs Josh's decision | 16 | Josh's. Includes #22225 Annotate. |
| #17438 | Fleet management: multi-daemon registry | 2 | Not a lane. |
| #24763 | Restore operator-launched panes and CLI sessions after a reboot | 1 | A root-level orphan. |

## Principles

1. **Lanes stop being parents.** Runbook v4 binds a DevTeam to one task and its own worktree (§3.1, Decision 2). §2.4 says "Do not rely on stale lane labels". Grouping should follow product area, not who staffs it. The `lane-N` labels are dropped as part of the same change.
2. **Plan-expanded subtrees move whole.** Examples: #23341, #23561, #23670, #23433, #23570, #23220, #24238, #23591 and #24538. Nothing is pulled out of a plan subtree. The only exceptions are moot leaves, which close.
3. **Moot means close, not move.** Rows in the #24782 report's "Moot once v4 lands" table close with a no-work disposition when their section lands. They are not reparented. Rows marked *verify* move with their domain until checked.
4. **Josh's subtrees are listed, not touched.** #22978 and everything in it, including #22225, stays.
5. **Product epics stay.** #21542 (with #21575 and #17436) and #18498 keep their shape.
6. **Least mechanism.**
   - Ten domain roots, each with at least five members.
   - One level of new epics only.
   - No per-lane "misc" buckets.
   - Leaves that already sit in a fitting product epic stay where they are.
7. **Order of operations.**
   1. Close the moot tasks per #24782.
   2. Create G1–G10.
   3. Reparent.
   4. Remove the lane labels and the empty lane epics.
   5. Update `project_policy.lanes`, which becomes empty under v4 §6.1.

## Proposed tree

```
#21542  Gobby 1.0 (unchanged; includes #21575 Hub mode, #17436 Node mode)
G1  Task lifecycle, review and landing
      #23341 Remove gobby build (whole subtree)
      runbook-devteam-redesign plan root (when expanded)
      #23561 content-based ownership (whole subtree)
      task-tool leaves
G2  Rules, hooks and shell classification
G3  Seats, sessions and messaging (incl. operator channels)
      #22643 Handoff stop gates and pull order (plan root)
G4  Sandbox, isolation and worktrees
      #23670 Per-spawn SRT network profile (whole subtree, incl. #23678)
G5  Daemon stability and performance
      #24538 Bounded-run tool (surviving P2/P4 after P3 closes)
G6  CI, tests and release tooling
G7  Code index
      #23433 (whole subtree), #23570 (whole subtree), #22948 (whole subtree)
G8  Terminal, client and web UI
      #23220 gclient daemon resilience (whole subtree)
G9  Decisions, providers and usage
      #24238 decision classifier (whole subtree), #23591 usage ledger (whole subtree)
G10 Plans and memory tooling
#18498  Post-0.5.0 (unchanged)
#22978  Triage (Josh's; unchanged, includes #22225)
#17438  Fleet management (unchanged; see open question 2)
```

## Reparent operations

"From" is the current lane epic. Every row moves the named tasks as direct children of the "to" root. Subtrees marked "(tree)" move with all their descendants.

| Task | From | To | Reason |
|---|---|---|---|
| #23341 (tree) | #24317 | G1 | Plan epic for task-system removal; its lane epic retires |
| #23561 (tree) | #22691 | G1 | Ownership ledger feeds close invariant 3 (v4 Decision 4) |
| #24590, #24493 | #22691 | G1 | Gate 9 and ownership attribution survive as invariant 3 |
| #24197, #24399, #24623, #24728, #24732 | #22691 | G1 | land_commit, claim release, task diff, list_tasks |
| #22819 | #22949 | G1 | Task description append operation |
| #24730, #24743 | #24093 | G1 | claim_task session binding; list_tasks compact rows |
| #18647, #23722, #23891, #24048, #24168, #24177, #24190, #24257, #24286, #24297, #24298, #24299, #24309, #24311, #24396, #24397, #24419, #24560, #24588, #24634, #24640, #24772 | #22691 | G2 | Rule engine, guards, attribution and shell classifiers |
| #23756 | #23378 | G2 | require-dart-skill matcher bug (remains after #23851) |
| #22643 (plan root) | #22691 | G3 | Handoff stop gates are session behavior |
| #23373, #23818, #23895, #24262, #24304, #24316, #24643, #24720, #24722, #24773 | #22691 | G3 | Seat providers, wakes, seat definitions, coordination waits |
| #23669 | #22949 | G3 | Staleness check for #22643; travels with it |
| #22926, #24121, #24130, #24136, #24173, #24264, #24301, #24321, #24776, #24778 | #23378 | G3 | Handoff retrieval, session scoping, agent definitions, send_keys, operator channels |
| #24169, #24235, #24416, #24513, #24725, #24733, #24750, #24771, #24775, #24784, #24785 | #24093 | G3 | Restart-hold yield, seat recovery, wakes, Telegram |
| #23670 (tree) | #22691 | G4 | Spawn sandbox network profile; includes #23678 |
| #24485 | #22691 | G4 | Sandbox-safe descendant inventory |
| #22407 | #23378 | G4 | Droid ps shim |
| #23615, #23634, #24066, #24410, #24415, #24533, #24726, #24742 | #24093 | G4 | Worktree create and rollback, SRT trees, env leaks, security audit |
| #23359, #23370, #23468, #23616, #23738, #23776, #24051, #24132, #24157, #24170, #24212, #24308, #24407, #24413, #24534, #24536, #24539, #24618, #24626, #24745, #24766 | #24093 | G5 | Daemon event loop, startup and cutover, storage bloat, MCP output |
| #24538 (tree, minus moot P3 and #24552) | #24093 | G5 | Bounded-run launcher; subject to open question 3 in #24782 |
| #22360, #23902, #24228, #24288, #24406 | #22801 | G6 | Cross-platform CI, host build load, sandbox fixtures, release identity |
| #22923, #23711, #24106, #24125, #24303 | #23378 | G6 | Dev container, test hygiene, red CI |
| #24424, #24729, #24780 | #24093 | G6 | Test harness cost, env scrub, red CI |
| #23433 (tree), #23570 (tree), #22948 (tree), #23874, #24029, #24731 | #22801 | G7 | gcode indexing, pins, reconcile, evidence |
| #23220 (tree), #23438, #23445, #24324 | #22801 | G8 | gclient, gterm host, native panes |
| #23869 | #22691 | G8 | Operator WS input encoding |
| #24638, #24653, #24654, #24655, #23729 | #24094 | G8 | Client and Annotate UI; see open question 3 |
| #24763 | (root) | G8 | Restore operator panes after reboot |
| #24238 (tree), #22604 | #22801 | G9 | Decision classifier and its first consumer |
| #23591 (tree), #24306, #24781 | #23378 | G9 | Provider quota and usage, new CLI support research |
| #23768 | #22949 | G9 | Local TTS provider swap |
| #24479 | #22691 | G10 | expand-task registration check |
| #23470, #23731, #24128, #24515, #24774 | #23378 | G10 | Plan validator, coverage manifest, review-lesson memories |
| #22606 | #22949 | G10 | Memory community detection (blocked by #21563) |
| #24403, #24497 | #22691, #24093 | G1 | *Verify* rows: land-gate classifier and gate 10 recognition. They move provisionally (Principle 3) and close in G1 if their check confirms moot |
| #24135, #24398 | #22691 | G3 | *Verify* rows: developer step workflow and code-reviewer park check. They move provisionally (Principle 3) and close in G3 if their check confirms moot |
| #24723, #24727 | #24093 | G4 | *Verify* rows: close-reviewer SRT denials and sandbox writes. They move provisionally (Principle 3) and close in G4 if their check confirms moot |
| #24782, #24783 | #23378 | (close) | These research spikes; the Orchestrator closes them |

The seven lane epics #22691, #22801, #22949, #23378, #24093, #24094 and #24317 retire once they are empty. The "Lane 9" root #24317 empties when #23341 moves.

## Tasks ruled moot or deferred to the moot-list research

These close per `.gobby/plans/research/moot-tasks-after-astra-2026-10-21.md` and take no reparent operation. Rows marked *verify* are the exception: they have reparent rows above, move with their domain until checked, and close there only if the check confirms them moot. They are listed here for traceability, and the lane epics' retirement does not wait on them.

- **From #22691:** #24494, #24509, #24646, #24656, #24748, #23868, #24031, #24085, #24135 (*verify*), #24295, #24398 (*verify*), #24403 (*verify*), #22660. If kept after open question 4 in #24782, #22660 goes to G3.
- **From #22949:** #23817. If kept after open question 1 in #24782, it goes to G1.
- **From #23378:**
  - Subtrees: #23542 with #23707, #23544 and #23550; #23640 with #23642, #23644 and #23645.
  - Leaves: #24779 and #24777.
- **From #24093:**
  - #24538 P3: #24546, #24555 and #24556, plus #24552.
  - Leaves: #24497 (*verify*), #24498, #24617, #24723 (*verify*), #24727 (*verify*) and #24736.
- **Elsewhere:**
  - #18784 and #21528 sit in #18498 and close in place.
  - #21972 sits in #22978. It is Josh's to sweep.
- **Already moot at tip, trim only:** #24149, #21564, #21567 and the stage rows on #17436, #17769 and #17438. These keep their parents.
- **Trim, then move:** #22643 is not moot; only its stale `gobby build` text needs trimming. It moves as a plan root to G3 per its reparent row above.

## Open questions for Josh

1. **Shape:** ten domain roots (G1–G10), or hoist the plan epics to top level and leave the loose leaves in two or three broad roots? This proposal takes the domain roots, at one new level.
2. **#17438 Fleet management** (multi-daemon registry, PostgreSQL capacity across daemons): fold it into #21542 Stage 4 next to Hub and Node mode, or keep it as its own root?
3. **Annotate:** should #24653, #24654, #24655 and #23729 join #22225 in a "Gobby Annotate" epic? #22225 is yours and moves only on your word. Otherwise they stay as leaves in G8.
4. **#18498 Post-0.5.0:** keep it as a release-horizon bucket, or spread its 10 direct children (13 open descendants) across G1–G10 with a `post-0.5.0` label?
5. **Timing:** apply the reorg with runbook v4 §6.1, when lane routing retires, or earlier? Earlier means `project_policy.lanes` and the lane-N labels need a stopgap first.
6. **Domain names:** confirm the G1–G10 titles, or rename them, before the Orchestrator creates them.
