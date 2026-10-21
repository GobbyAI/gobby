# DevTeam with a worktree per epic

Research for #24796, an addendum to `.gobby/plans/runbook-devteam-redesign.md` (cohort v4). Researcher gobby#17246, 2026-10-21. Code was checked at integration tip `96037e9937`. Read-only against code. This report proposes work to start **after** the redesign plan is complete. It does not change, block or reorder Astra's v4 work.

## Summary

Josh's per-epic DevTeam needs little new mechanism once v4 lands. The v4 team is already "bound to one task, branch and tree". The per-epic variant is the same team, with an epic as its bound task. The current system already supports most of what that needs:

- **Claims:** an epic can be claimed. `claim_task` has no task-type check.
- **Edits and commits:** when the epic is the selected claim, it unlocks edits and commits and receives edit attribution.
- **Commit registration:** an unclaimed leaf accepts `link_commit`, and tag-based auto-linking, with no ownership check.
- **Landing:** `land_candidate` lands any task's linked, independently approved SHA. It does not refuse epics.

The redesign's four close invariants (Decision 4) fix the end-of-epic order. Invariant 4 (the linked commit must be an ancestor of `0.5.0`) means no leaf can close before the epic branch lands. Invariant 1 (children closed) means the epic closes last. The order is therefore:

1. Land the epic tip.
2. Batch-close the leaves.
3. Close the epic.
4. The existing 3.3 teardown runs.

This matches Josh's 13:40 CDT input: "tasks close in a batch at the end, after landing ... the new four gates and the reviewer's stamp. Blockers would unlock with registered commits."

Five pieces of new mechanism are needed, all small:

1. Team binding accepts an epic and keeps others out of its leaves.
2. Skill loading covers the epic's leaves.
3. A leaf's review independence uses the epic's claimant history.
4. Inside an active epic team, a blocker counts as resolved once it has a registered commit.
5. Procedure text for the phase order.

§5 gives a draft plan section, P7, ready to append.

## 1. The per-task design this builds on

From the redesign plan:

- **Team and binding (Decision 2, §3.1):** a DevTeam has a Test Writer, Developer, Code Reviewer and Dev Manager. It serves exactly one task in one registered branch and worktree. Binding is recorded in pipeline execution metadata before any seat spawns. There is one active team per task. A replacement seat must name the same team, task, worktree and role. Branch creation and task reassignment inside a bound tree are refused.
- **Test-first and review (Decision 3, Decision 5, §3.2):** the Test Writer commits tests before implementation. The reviewer checks criteria, diff and security, reruns the tests, and verifies the test-first history. The single task claim passes atomically within the team: Test Writer, then Developer, then Sol review. On rejection one, it returns to the original Developer and the same Sol reviewer. On rejection two, the Sol reviewer becomes claimant and fixer, and a fresh Opus reviews. On rejection three, it escalates to Triage. An editor or former claimant never approves.
- **Landing (Decision 8, §2.4):** the Dev Manager lands with another reviewer's exact-SHA approval. Disjoint integration changes land as a two-parent merge, and overlaps go to Triage.
- **Close and teardown (Decision 4, §1.1, §3.3):** close enforces four invariants:
  1. Children are closed.
  2. A linked commit or a no-work disposition exists.
  3. No task-attributed edits remain uncommitted.
  4. The work-bearing commit is an ancestor of `0.5.0`.

  A successful close ends every seat and safely removes the merged tree.
- **Release window (§4.1):** during a release window, landings and new team admissions wait. Existing teams keep working.

## 2. The per-epic variant

The epic-mode lifecycle runs in eight steps:

1. **Admission.**
   - The Orchestrator admits a DevTeam bound to an epic, using the same `devteam` runbook. Mode comes from the bound task: an epic with open descendants runs in epic mode, and anything else runs per task. No mode setting is needed.
   - Admission records the epic's open leaf set once, in the team record.
   - The tree and branch are named `wt-epic-<seq>`.
   - Admission claims the epic. No leaf is claimed at any point.
2. **Test Writer phase.**
   - The Test Writer writes tests for every leaf, in dependency order.
   - Each leaf gets its own commit(s), tagged `[gobby-#<leaf>] test: ...`, and linked to that leaf.
   - Commits that span leaves go to the epic.
3. **Developer phase.**
   - The claim passes to the Developer, as in §3.2, which implements every leaf in dependency order with one leaf per commit series.
   - Commits are linked to their leaf.
   - Inside the team, a leaf counts as unblocked once each blocking sibling has a registered commit (Josh's ruling).
4. **Review phase.**
   - Starts only after the Developer has finished the whole epic.
   - The Sol reviewer reviews each leaf against its criteria, diff and test-first history at the current epic tip.
   - It records one 2.3 verdict per leaf. That verdict is the leaf's "reviewer's stamp".
5. **Rework.**
   - Rejected leaves go back through Decision 5's ladder, counted per leaf (see §3 row 9 for who may then review).
   - Every rework commit is linked to its leaf.
6. **Landing approval.**
   - Requires every leaf in the leaf set to have a `valid` stamp, or to have been detached under §4.3.
   - An independent reviewer then records one exact-SHA approval on the **epic** for the final tip, and the tip is linked to the epic.
7. **Land.** The Dev Manager lands the epic tip through `land_candidate`, with the epic as the task.
8. **Close.**
   - The Dev Manager batch-closes each leaf (four invariants, plus a check that the leaf's stamp is `valid`), then closes the epic.
   - Epic close triggers the existing 3.3 teardown.

## 3. Where the system assumes a per-leaf claim

Verdict key:
- **Reuse:** works as-is.
- **Small:** a bounded change to an existing module or text.
- **New:** a new mechanism.
- **Retired:** removed by v4 or remove-gobby-build; no per-epic action needed.

Evidence hashes come from `gcode evidence` range reads at `96037e9937`.

| # | Assumption | Where | Per-epic verdict |
|---|---|---|---|
| 1 | Close invariant 1, children closed | v4 §1.1 | **Reuse.** It forces leaves to close before the epic. |
| 2 | Close invariant 2, linked commit or no-work disposition | v4 §1.1; `link_commit` in `src/gobby/mcp_proxy/tools/task_commits.py` | **Reuse.** `require_claim_authority` returns early for an unclaimed task (`_authorization.py:109-111`, `a0ff0250…`). The commit-path ownership check also returns when the task has no owner (`commit_ownership.py:22-25`, `8378967e…`), and so does auto-link (`commit_ownership.py:79-80`). Leaves get commits from `link_commit` or from `[gobby-#leaf]` tags. A leaf the team found needs no work closes with a no-work reason. |
| 3 | Close invariant 3, no uncommitted task-attributed edits | `src/gobby/workflows/task_claim_state.py` | **Reuse.** Edits go to the selected claim (`active_task_id_for_edit`, `task_claim_state.py:302-304`, `0145745f…`), which is the epic. Leaves carry no attributed edits, and the epic's close enforces the invariant for the whole tree. |
| 4 | Close invariant 4, linked commit is an ancestor of `0.5.0` | v4 §1.1 | **Reuse, plus a procedure rule.** Leaf commits become ancestors when the epic tip lands. The epic branch is updated by merging `0.5.0` into it, never by rebase or squash, after the first leaf link. Rewriting history would orphan the linked leaf SHAs, and invariant 4 would then refuse those leaves loudly. |
| 5 | Edit and commit unlock requires a claim | rules `require-task-before-edit`, `require-task-before-commit` | **Reuse.** Both test `task_claimed` or the selected claim, and neither special-cases epics. |
| 6 | Stop hook holds a claimant's turn | rule `stop-gates/require-task-close.yaml` | **Reuse.** It exempts spawned agents (`is_spawned_agent`) and durable waits, so persistent team seats are not held. |
| 7 | TDD and RED evidence per claimed task | retained-RED gates; `claimed_task_tdd_obligations` | **Retired** by v4 §1.2. The per-epic rule is procedural only: the reviewer checks that each leaf's test commit is an ancestor of its implementation commits. One commit series per leaf makes this mechanical. |
| 8 | Task-carried skills load only for claimed tasks | `build_claimed_task_extra_skill_state` (`claimed_task_extra_skills.py:43-52`, `583594ef…`) iterates `claimed_tasks` | **Small.** For a claimed epic bound to an epic-mode team, take the union of the open leaves' `additional_skills` and source-work category. Without this, leaf skills never load. |
| 9 | Review verdict independence ("editors and former claimants cannot approve") | v4 §2.3, `review_verdicts.py` | **Small.** A leaf has no claimants, so a per-leaf independence check would pass for anyone. For a leaf under an epic-mode team, use the epic's claimant and editor set. Edits attribute to the epic, so independence is tracked at epic level. Once the Sol reviewer has fixed any leaf, it may no longer stamp other leaves or approve the epic, and the fresh Opus reviewer takes over. This is stricter than needed, but it needs no per-leaf attribution. |
| 10 | Landing approval per exact SHA, per task | `land_candidate` (`land_commit.py:420-431`, `13251ced…`); v4 §2.4 | **Reuse, with the epic as the landed task.** Landing requires the SHA in the task's commits plus an `INDEPENDENT_REVIEW_APPROVAL` for that exact SHA. Neither `land_candidate` nor `_landing.py` refuses an epic. Link the final tip to the epic and approve it there. |
| 11 | Landing approval reasons and the activation receipt | `close_receipts.py`, `landing_policy.py` | **Retired** by v4 §2.4. Schema carriers and project-sync checks stay and apply unchanged. |
| 12 | Review rejection count and third-rejection escalation | v4 §2.3 | **Reuse per leaf.** `review_fail_count` and escalation apply to the leaf. Clearing the claim is a no-op because a leaf has none. The epic keeps its claim, and §4.3 handles the escalated leaf. |
| 13 | Found work belongs to the claimed task | `found_work_gate.py`, AGENTS.md rule 4 | **Reuse.** The epic claim arms the gate. A fix is committed to the leaf it affects, or to the epic, and named in that task's close summary. |
| 14 | Worktree and branch naming `task-<seq>-<slug>` | `generate_branch_name` (`isolation_models.py:95-96`, `436e26e2…`) | **Reuse.** An explicit `branch_name` wins, so admission passes `wt-epic-<seq>`. The worktree path derives from the branch name. |
| 15 | Worktree cleanup on close | v4 §3.3 | **Reuse.** Teardown keys on the bound task's close, which is the epic. |
| 16 | One active team per task; refuse reassignment in a bound tree | v4 §3.1 | **Small.** The binding admits an epic and records its leaf set. Admission is refused in two cases: a leaf whose ancestor epic has an active team, and an epic any of whose descendants already has an active team or claim. `claim_task` on a leaf inside an active epic-mode team is refused, so nothing splits attribution. |
| 17 | Claim passes through the review loop | v4 §3.2 | **Reuse, with the epic claim.** Team metadata records phase and per-leaf review round instead of one round. That record is the existing `devteam_lifecycle.py` state, extended. |
| 18 | Single task per seat | `project_policy` "One task/seat includes pending candidates" | **Small (text).** It becomes "one task (a leaf, or an epic in epic mode) per team". |
| 19 | Dependency blocks until the blocker closes | `_fetch_active_blocker_map` (`_blocking.py:41-43`, `65e06518…`: `blocker.closed_at IS NULL`); v4 §2.2 state queries | **Small (Josh's ruling).** A blocker is also resolved when (a) blocker and blocked share an epic ancestor that holds an active epic-mode claim, and (b) the blocker has at least one linked commit. Every blocked authority in §2.2's state contract must apply the same predicate. Outside epic teams nothing changes: an unlanded commit elsewhere is not on the dependent's branch. |
| 20 | Release lease blocks landings and new admissions | v4 §4.1 | **Reuse.** A long-lived epic team keeps working during a window, and only its single landing waits. |
| 21 | Stage-era epic gates (`_ancestor_gate.py`, `_epic_gate.py`, `epic_qa`) | remove-gobby-build | **Retired.** The standalone `epic-reviewer` survives (remove-gobby-build 3.4.1) and could add one cross-leaf coherence pass, but per-epic mode does not require it. |
| 22 | Decision 2 and §3.1 text say "exactly one task" | v4 Decision Record | **Small (plan text).** Add a Decision 14 defining epic mode (draft in §5). |

No row needs new mechanism. The small rows are 8, 9, 16, 17, 18, 19 and 22, plus procedure text.

## 4. Mixed domains, dependencies, partial failure and leaf close

### 4.1 Frontend and backend developer selection

`implementation_domain` (`backend`, `frontend` or `fullstack`) is required on every code task. Today it is only task metadata:
- Only one `developer.yaml` exists.
- Nothing selects a developer by domain.
- v4's DevTeam has a single Developer role.

Recommendation:
- One Developer seat is active in the tree at a time. Two concurrent developers in one tree would trip the shared-worktree dirty-path rules for no gain.
- The Dev Manager orders leaves by dependency, then groups consecutive leaves of the same domain.
- When the domain changes, the Manager launches a replacement Developer seat (§3.1 replacement, same role) with the domain as a launch input. A frontend seat loads `impeccable` and `.impeccable.md`, per AGENTS.md "Design Context". A `fullstack` leaf takes whichever seat is current.

There is no separate frontend or backend definition, and no concurrency.

### 4.2 Dependencies inside the epic

Each phase runs leaves in topological order of their `blocks` dependencies. A dependent unblocks inside the team once its blocker has a registered commit (row 19). The Test Writer's phase satisfies that for tests; the Developer phase then works on a branch that already contains the blocker's code.

A cycle inside the epic is a planning defect, and the Dev Manager sends it to Triage.

### 4.3 Partial failure

Three cases:
- **External blocker.** A leaf blocked by a task outside the epic is skipped in each phase, and the team continues. If it is still blocked when every other leaf is stamped, Triage chooses between waiting and detaching.
- **Escalated leaf.** On the third rejection, the leaf escalates through the existing 2.3 path. Triage either grants an audited reset, after which the team continues, or detaches the leaf.
- **Detach.**
  1. The Developer reverts that leaf's commits on the epic branch. One commit series per leaf makes this a clean revert.
  2. The Orchestrator reparents the leaf out of the epic, which is a task edit.
  3. The rest of the epic lands. The detached leaf later gets its own per-task team.

Reverting matters because the Test Writer committed that leaf's tests up front, and landing them without an implementation would put failing tests on `0.5.0`.

Partial landing of an epic branch without a revert is not supported.

### 4.4 How leaves close

Leaves close individually, in a batch, after the epic lands. Neither the epic nor any leaf is closed by an epic-level cascade. Each leaf close passes the four invariants:
- children closed (trivial for a leaf);
- a linked commit;
- no attributed edits;
- the commit is an ancestor of `0.5.0`, now true after landing.

The Dev Manager closes a leaf only when its latest stamp is `valid`. The epic closes last and runs teardown.

Under Decision 4, review judgment is not a close invariant, so the stamp check lives in the Dev Manager procedure. Making it a mechanical close gate would be new mechanism and is not recommended. Ruling 2 below asks Josh to confirm.

## 5. Recommended addendum (draft, to append after the redesign completes)

The draft follows the plan's grammar. It depends on 6.1, so nothing here touches v4 execution.

```markdown
## P7: Epic-bound DevTeams
`kind: framing`

Decision 14 (addendum, Josh 2026-10-21): A DevTeam may bind to an epic instead of a leaf. The team claims only the epic, persists across every open leaf in it, works in `wt-epic-<seq>`, and runs phases across the whole leaf set: tests for every leaf, implementation of every leaf, review of every leaf, rework, one landing of the epic tip, then batch leaf closes and epic close. Leaves are never claimed; commits are registered on them. Inside the team a blocker counts as resolved once it has a registered commit. Every Decision 1-13 rule applies unchanged with "task" read as the bound epic, except as stated here.

### 7.1 Bind a DevTeam to an epic (depends: 6.1)
`kind: deliverable`
`[category: code]`

Targets:
- `src/gobby/agents/devteam_lifecycle.py::*` — scope-reason: record epic mode, the leaf set, phase and per-leaf review round in the team record
- `src/gobby/agents/team_launch_inputs.py::*` — scope-reason: derive epic mode from the bound task and pass `wt-epic-<seq>` as branch_name
- `src/gobby/mcp_proxy/tools/runbook_seat_tools.py::*` — scope-reason: refuse overlapping epic and leaf team admissions
- `src/gobby/mcp_proxy/tools/tasks/_lifecycle_claim.py::*` — scope-reason: refuse a leaf claim inside an active epic-mode team
- `src/gobby/workflows/claimed_task_extra_skills.py::*` — scope-reason: include open leaves' skills and source-work category for a claimed epic-mode epic
- `src/gobby/tasks/review_verdicts.py::*` — scope-reason: judge leaf verdict independence against the epic's claimant and editor set
- `src/gobby/storage/tasks/state_queries.py::*` — scope-reason: treat an intra-epic blocker with a registered commit as resolved while the epic team is active
- `src/gobby/storage/tasks/_blocking.py::*` — scope-reason: apply the same blocker predicate in hydration
- `tests/agents/test_devteam_epic_mode.py`

**Research context:** `.gobby/plans/research/devteam-per-epic-2026-10-21.md` maps every per-leaf-claim assumption; rows 8, 9, 16, 17 and 19 are this leaf. Claims, edit attribution, unclaimed-leaf `link_commit`, tag auto-link, landing and the four close invariants already work with an epic as the bound task and need no change. Mode is derived from the bound task (an epic with open descendants), not a new setting. The blocker predicate requires a shared epic ancestor holding an active epic-mode claim plus at least one linked commit on the blocker; outside epic teams blocking is unchanged.

**Acceptance:**
- 7.1.1 - An epic admission claims only the epic, creates `wt-epic-<seq>`, records the leaf set, and refuses overlapping leaf or epic admissions and leaf claims. test: `tests/agents/test_devteam_epic_mode.py::test_epic_admission_and_exclusion`.
- 7.1.2 - Leaf verdicts reject any session in the epic's claimant or editor set; leaf skills load for the epic claimant. test: `tests/agents/test_devteam_epic_mode.py::test_leaf_independence_and_skills`.
- 7.1.3 - An intra-epic dependent unblocks when its blocker gets a registered commit during an active epic team, and not otherwise. test: `tests/agents/test_devteam_epic_mode.py::test_registered_commit_unblocks_inside_epic`.

**Verification:** Focused isolated unit and state-query tests; the state contract parity test from 2.2 reruns.

### 7.2 Epic-mode procedures (depends: 7.1)
`kind: deliverable`
`[category: config]`

Targets:
- `src/gobby/install/shared/workflows/pipelines/devteam.yaml::*` — scope-reason: accept an epic binding
- `src/gobby/install/shared/skills/gobby/references/agents/procedures/dev-manager.md::*` — scope-reason: phase order, domain-grouped Developer replacement, landing the epic tip, stamp-checked batch leaf close, detach path
- `src/gobby/install/shared/workflows/agents/test-writer.yaml::*` — scope-reason: one tagged commit series per leaf
- `src/gobby/install/shared/workflows/agents/developer.yaml::*` — scope-reason: per-leaf commits, merge-not-rebase after the first link, domain launch input
- `src/gobby/install/shared/skills/gobby/references/agents/procedures/code-reviewer.md::*` — scope-reason: per-leaf stamp and one epic tip approval
- `docs/guides/runbooks.md::*` — scope-reason: document epic mode
- `tests/workflows/test_devteam_epic_runbook.py`

**Research context:** Report §2 and §4 give the procedure. The stamp check is procedural (Decision 4 keeps review judgment out of close invariants).

**Acceptance:**
- 7.2.1 - Procedures specify phase order, per-leaf commits, merge-only branch updates, stamp-checked batch close after landing, and the detach path. test: `tests/workflows/test_devteam_epic_runbook.py::test_epic_procedures`.

**Verification:** Template parsing, registry sync and runbook dry-run tests.

### 7.3 Pilot one epic (depends: 7.2)
`kind: deliverable`
`[category: test]`

Targets:
- `tests/e2e/test_devteam_epic_lifecycle.py`
- `.gobby/plans/research/devteam-epic-pilot-results.md`

**Acceptance:**
- 7.3.1 - A three-leaf mixed-domain epic with one intra-epic dependency runs tests, implementation, review with one rejection, one landing, batch leaf close, epic close and teardown. test: `tests/e2e/test_devteam_epic_lifecycle.py::test_complete_epic_team`.
- 7.3.2 - A leaf escalated on third rejection is detached by revert and the rest of the epic lands. test: `tests/e2e/test_devteam_epic_lifecycle.py::test_detach_escalated_leaf`.

**Verification:** Isolated e2e, then one live epic pilot under Release Manager authority with results recorded.
```

## Rulings for Josh (through the Assistant)

1. **Mixed domains.** Should one Developer seat at a time, replaced when the domain changes (recommended), be the rule? The alternative is concurrent frontend and backend developers in one tree.
2. **Reviewer's stamp.** Should it be a Dev Manager procedure check (recommended, consistent with Decision 4), or a fifth mechanical close gate for leaves?
3. **Independence granularity.** Once the Sol reviewer has fixed any leaf, it stops stamping the epic's other leaves (recommended, row 9). The alternative is per-leaf edit attribution so it could keep reviewing untouched leaves, which is new mechanism.
4. **Escalated or blocked leaf.** Should the default be detach-by-revert so the rest lands (recommended), or should the whole epic wait?
5. **Naming.** Should both branch and tree be `wt-epic-<seq>` (as written), or only the tree, with branch `epic-<seq>`?

## Method

- Read the redesign plan in full at 0.5.0 `96037e9937`.
- Read the claim, authorization, commit-ownership, auto-link, landing, edit-attribution, blocker and skill-loading code with `gcode outline`, `gcode symbol-at` and `gcode grep`.
- Read the task-enforcement and stop-gate rule templates.
- Checked remove-gobby-build for `epic-reviewer` retention.
- Took evidence hashes from `gcode evidence` working-tree range reads.

Not verified: v4 modules that do not exist yet (`devteam_lifecycle.py`, `review_verdicts.py`, `state_queries.py`). Their behavior is taken from the plan text.
