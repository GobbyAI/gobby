Plan artifact: `.gobby/plans/runbook-devteam-addendum.md`

# Runbooks addendum: epic teams, category reviewers and agent workspaces

**Plan ID:** runbook-devteam-addendum

## Context
`kind: framing`

Task #24810 (Plan: Runbooks addendum) under #22691. Josh, 2026-10-22 16:20 CDT via Assistant #15976: "The research we did for the Runbooks addendum yesterday should be turned into a plan."

This addendum runs after `.gobby/plans/runbook-devteam-redesign.md` (#24797) lands. It does not change, block or reorder #24797. It builds on the end state that plan delivers:
- role-owned step records and the dedicated writers (redesign 2.3, Decision 14);
- Dev Manager landing with the independent reviewer's exact-SHA record (2.4);
- the durable team record in pipeline execution metadata (3.1);
- the review loop (3.2) and close teardown (3.3);
- the shipped `devteam` runbook and the Dev Manager, Test Writer and Triage definitions (3.5).

The evidence inputs are `.gobby/plans/research/devteam-per-epic-2026-10-21.md` (#24796) and `.gobby/plans/research/agent-workspaces-2026-10-21.md` (#24801). Their proposals are evidence. Where the rulings below differ, the rulings win: Josh overrode the per-epic report's recommendation 1A (one Developer at a time) and its detach-by-revert path, and narrowed the workspace report's open reads.

Sources of authority, all final and encoded here without reopening:
- Memory ed664ba7 (ruling 1): parallel work inside one epic under the Dev Manager.
- Memory 3cb34179 (ruling 2): generic step-record close.
- Memory 904f11ae (Josh, 2026-10-28; #25298): single tasks and epic leaves share the golden path and review rounds. Each parallel leaf gets a Test Writer, Developer and Code Reviewer triplet in a sub-worktree; the leaf closes after merging into the epic worktree, then its pod tears down. The primary triplet and Dev Manager persist until the epic completes.
- Memory f3025551, corrected by Josh on 2026-10-27: a stuck leaf holds only itself and its dependents; ready siblings may land. Merge 0.5.0 into the epic branch, never rebase or squash. Its older reviewer-only ruling is superseded by 904f11ae.
- Memory 00d1e3fa (ruling 5): epic and leaf branch and worktree names.
- Memory ac82c140 (#24801 rulings 1-4): workspace reads, helper scripts, node sync and retention.
- Orchestrator #17281 rulings of 2026-10-22 on #24810 (Q1 escalation counter, Q2 workspace read scope and the persistent set), and Josh's two scope additions relayed the same day: category reviewers for docs and research, and the `devteam` runbook as a model for other teams.

No push is authorized by this plan. Operator-owned uncommitted work remains untouched.

## Decision Record
`kind: framing`

Numbering continues the redesign's Decisions 1-14.

15. **Epic mode.** A team bound to an epic with open descendants runs in epic mode; any other bound task runs the redesign's single-task flow. Mode comes from the bound task, with no setting. The same `devteam` runbook serves both. Every Decision 1-14 rule applies unchanged except as stated in Decisions 15-24. Ruling 1, verbatim: "I want to be able to parallelize work in the same epic. The Dev Manager would spawn x devs and handle sub-worktrees merging back into the epic worktree, where x is the maximum number of parallel tasks the epic permits based on dependencies. Only the primary dev agents would persist until the epic is complete."
16. **Claims.** The Dev Manager holds no claim, in leaf or epic mode (round-1 reconciliation with the landed #24797 `link_commit` and `land_commit` behavior). It merges 0.5.0, links the epic tip and lands without one: `link_commit` accepts unclaimed tasks, and `land_commit` refuses only a session that ever claimed the task. It closes leaves and the bound epic without one too. The live team binding, not a claim, stops the parent-close cascade at the bound epic. Each working seat claims the leaf it works: the Test Writer while writing a leaf's tests, a Developer while implementing or fixing a leaf. The Dev Manager moves a claim between its seats only with `hand_off_task_claim`, which needs the bound Dev Manager rather than a claim. Inside an active epic team only that team's seats may claim the epic's descendants. The per-epic report's "leaves are never claimed" (its rows 2-3 and §5) belonged to the overruled one-Developer design. Parallel developers each need their own claim for edit attribution and their own tip-bound implementation record, so ruling 1 requires leaf claims.
17. **Dev Pods and dispatch.** Josh's 2026-10-28 ruling supersedes the developer-only parallel unit. One DevTeam has a Dev Manager, Test Writer, Developer and Code Reviewer. A single-task team completes, lands and closes its task, then the daemon cleans up and tears down its seats; it cannot start Dev Pods. An epic keeps those primary seats until epic completion. The primary triplet handles serial leaves in the epic worktree. For a parallel leaf the Dev Manager runs `devteam` with `parent_team_id=<epic team UUID>`, which starts a separate pod with a Test Writer, Developer and Code Reviewer triplet in a sub-worktree. Each pod remains through its leaf's review, landing into the epic worktree and close, then the daemon cleans up and tears down that pod. The developer's `implementation_domain` selects skills, not extra primary seats. The Dev Manager may also spawn docs-reviewer, research-reviewer and researcher seats as needed.
18. **Names.** Ruling 5, verbatim: "2.5 Your suggestion". Josh amended it on 2026-10-22, relayed by the Orchestrator #17281: worktree names carry the task title. The epic branch is `epic/<epic_ref>/main` in worktree `wt-epic-<epic_ref>-<epic_title_slug>`. A parallel leaf branch is `epic/<epic_ref>/<leaf_ref>` in worktree `wt-epic-<epic_ref>-<leaf_ref>-<leaf_title_slug>`, branched from `epic/<epic_ref>/main`; the Dev Manager lands it with `land_commit`, and daemon-owned teardown deletes it after leaf close. A ref is the task's sequence number without `#`. A title slug follows the spawn default `task-<seq>-<title-slug>` rule in `isolation_models.py::generate_branch_name`: lowercase, spaces to hyphens, only letters, digits and hyphens kept, repeated hyphens collapsed, truncated to 40 characters with any trailing hyphen dropped (the spawn default's `_sanitize_branch_name` already drops it), and the `-<slug>` part omitted when the slug is empty. Branch names carry no slug.
19. **Merges.** After exact-tip approval and dependency checks, the unclaimed Dev Manager records the leaf's closing step, calls `land_commit` for its approved SHA and closes the leaf. `task_landing_target` routes primary epic leaves and pod leaves to epic main. Landing fast-forwards when possible; when the target moved on disjoint paths, it constructs a two-parent merge with `commit-tree` and fast-forwards to it. Shared changed paths or merge conflicts escalate to Triage; the manager leaves the task escalated. The daemon ends the closed leaf's pod and deletes its merged sub-worktree and branch. The manager performs no teardown. Before an epic-tip review it merges 0.5.0 into epic main. f3025551: "merge 0.5.0 into the epic branch; never rebase or squash." The reviewed leaf SHA remains its linked candidate; link the resulting epic tip to the epic for its own review.
20. **Intra-epic unlock.** Josh, 2026-10-21 13:40 CDT (recorded in ed664ba7): "intra-epic blockers unlock with registered commits." Ruling 4, verbatim: "Parallel tasks that feed a single blocker have to finish before the next step is unblocked." Inside an active epic team, a blocker counts as resolved once it is finished: `approved_task_step(blocker, records, "implementation")` returns a record. That is the latest implementation record in the blocker's current epoch, approved, and either naming its latest linked commit or carrying a no-work disposition while the blocker has no linked commits. A later rejected or superseding record, or a newer linked commit, makes an earlier approval stale. The Test Writer's earlier test commits are registered commits too, so linked commits alone would unlock every dependent after the test phase. The implementation record is the registered commit that marks the blocker finished. The Dev Manager merges a work-bearing blocker before dispatching its dependents; a no-work blocker has nothing to merge. Outside epic teams blocking is unchanged.
21. **Golden path and review rounds.** Josh's 2026-10-28 ruling applies to single tasks and epic leaves: Dev Manager → Test Writer → Code Reviewer → Developer → Code Reviewer → Dev Manager. Tests are reviewed before implementation; test-review rounds follow the single-task behavior. Each leaf gets its routed review at its exact tip, or an approved disposition without a commit for no-work. Implementation rejection one returns to the Developer and the same reviewer. Rejection two transfers the leaf claim to the reviewer as fixer and launches a fresh independent reviewer through the existing replacement flow, bound to that leaf's team or pod. Rejection three escalates the rejected task. The old epic-only ban on reviewer fixes and fresh reviewers is superseded. A ready epic tip receives its own exact-tip landing review after all merged work and its dependency closure are approved; unstarted or stuck unrelated leaves do not delay that review.

An epic-tip rejection is repaired through an open owning leaf, or a found-work leaf when the original leaf has already closed or no leaf owns the finding. The Dev Manager obtains that leaf through the established task-management route. Each assigned leaf follows the golden path and review rounds, then merges and closes. Unchanged leaves retain their records and landed work. Link the new epic tip and request the next epic-tip round; the epic's review counter carries on, and a third epic-tip rejection escalates under Decision 22.
22. **Three fails.** Reuse the redesign 2.3 per-task review counter. A leaf's third rejection escalates that leaf only; it does not escalate or hold the parent epic. The leaf and its dependents wait for an audited reset of that task's review epoch. A third rejection of the epic tip escalates the epic itself. An explicit epic hold gates pod admission and epic landing until the authorized reset or release. The Dev Manager holds no claim throughout (Decision 16).
23. **Dependency-only holds, never detach.** Josh, 2026-10-27: "A stuck task doesn't hold the epic." A stuck, escalated, externally blocked or unstarted leaf holds only itself and its dependents. Ready siblings may merge, close and land. An epic tip is ready when every merged leaf is approved and none depends on an unmerged, unlanded leaf; a merged leaf with an unmet dependency blocks that tip. Leaves are never backed out. Explicit epic holds remain distinct from leaf holds.
24. **Landing and close order.** Josh's 2026-10-28 ruling supersedes batch closure. After a leaf's exact-tip approval and dependency checks, the Dev Manager calls `record_closing_step`, then `land_commit` into the epic worktree, then `close_task`. The closing record comes first because a closed task refuses records. The daemon then ends the pod and cleans up its merged sub-worktree and branch; primary seats persist. No-work leaves receive an approved disposition review and are routed through Triage to the Orchestrator for close without landing. A ready epic tip may land while unrelated leaves remain open: merge 0.5.0 into epic main, link the tip, obtain the exact-tip approval and land under redesign 2.4. Once all descendants and final landing are complete, record the epic's closing step and close it; the daemon tears down the persistent primary seats and Dev Manager. Ruling 2 (3cb34179) still decides each leaf's required records through project policy and task type; the epic-tip record is the landing stamp.

The close covers the whole admitted descendant graph. A work-bearing leaf closes `done` with its linked commits. A no-work leaf closes with its approved disposition. Intervening phase epics are containers: team seats never claim them, and the ancestor cascade closes each once its children are closed, preserving a delivered-work outcome for a container that held work. A live team's bound epic stops the cascade, and the unclaimed Dev Manager closes it after final landing. A container with an explicit hold can prevent final epic closure; it does not stop unrelated ready leaves from landing. Closing a pod leaf queues that pod's teardown, while the primary team's teardown and epic folder's final commit wait for the bound epic's close.
25. **Category reviewers.** Josh, 2026-10-22: "code reviewer is correct when it involves writing docs for code, but we probably need a review agent for docs and for research." Two definitions are added. `docs-reviewer` reviews prose, user-facing and process docs. `research-reviewer` reviews research reports: it checks claims against cited sources and code evidence, and confirms that the question asked is the question answered. Each writes only the review record through the 2.3 writer and only reviews. Routing: tasks in categories code, config, refactor and test go to `code-reviewer`. Docs tasks go to `docs-reviewer`, except a docs task whose subject is code behavior, which goes to `code-reviewer`. Research tasks go to `research-reviewer`. `research` joins the `review_categories` default.

Reviewer bindings belong to the team or pod that owns the review. Admission seeds each scope's first binding from `reviewer_agent`; parallel pods may use the same reviewer definition without sharing the reviewer session. The Dev Manager routes each leaf at its first review and spawns a routed reviewer when that scope has none. Every binding passes the review-grant check. Rejection two replaces the review binding with a fresh independent seat while the former reviewer fixes the leaf. The epic-tip reviewer follows the precedence `code-reviewer`, then `docs-reviewer`, then `research-reviewer`, over the definitions that wrote the merged leaves' current approvals.
26. **Generic team contract.** Josh, 2026-10-22, verbatim: "That's the idea. Our DevTeam pipeline becomes a model for other teams, like an SEO team or a Docs team or a Research team." Team mechanics key on definition step grants and pipeline inputs, never on role names. A future team needs only new definitions and a pipeline, with no new close or landing code. Those teams are not planned here. P7 states the contract.
27. **Workspaces.** One daemon-committed Git repository per project at `~/.gobby/agents_workspaces/<project_id>/`, under Gobby home. #24801 ruling 1, verbatim: "I really only want the persistent sessions like Assistant to be able to see prior sessions, and only to recover from a crash or learn from mistakes made in prior versions. I don't want to pollute developer state on EpicDevPod for example." Orchestrator Q2 ruling: every session writes only its own folder. Persistent seats may also read their predecessors' folders, for crash recovery and learning from mistakes only. A predecessor folder sits under the caller's own `<definition>/`, belongs to a lineage that started before the caller's session (lower sequence number), and is bound as `_agent_workspace_dir` to no session in `LIVE_SESSION_STATUSES`. A live sibling of the same definition is therefore not a predecessor, and a resumed predecessor stops being one while it is live. Every other seat reads only its own session folder and its team's epic folder, enforced by a new read-path rule. A persistent seat's clear successor inherits its predecessor's folder; other seats get a fresh folder. The persistent set (Assistant, Orchestrator, Archivist, Release Manager, Triage) is a definition-level `persistent_workspace: true` flag, not a name list. Rulings 1.2 A, 1.3 A and 1.4 A: running an opaque helper script still needs a task claim; node sync uses Git bundles over the node channel with the hub as authority, built when the first seat runs on a node; everything is kept indefinitely.

## Existing work and dependency integration
`kind: framing`

Every phase heading carries `(depends: #24797)`, so expansion gives each leaf a `blocked-by` edge on #24797. Nothing here edits `.gobby/plans/runbook-devteam-redesign.md` or #24797's worktree. Where this plan extends a file #24797 creates, the extending section names that redesign section as its source.

Ownership boundaries:
- Research and planning tasks need implementation and closing records under redesign 2.3 whatever this plan does. Which seats write those for standalone work stays with #24797's grants. This plan adds only the review writer for research.
- The `project_policy` "One task/seat" text lives in tracked `.gobby/workflows/rules/project-operating-policy.yaml`. This plan does not edit live project policy. The epic-mode reading ("one task per team: a leaf, or an epic in epic mode") goes to the Orchestrator with the 8.6 pilot, because only the Orchestrator edits project policy.

## Constraints and evidence
`kind: framing`

Evidence was checked on 2026-10-22 against 0.5.0 `81f6a1fe80` and #24797's branch `task-23850` at `2f2b5b7427`. On that branch:
- `src/gobby/tasks/step_records.py` holds `_TIP_BOUND` = implementation, review and closing, `required_task_steps`, `_writer`, `_require_independent_reviewer` and the third-rejection escalation inside `record_task_step`.
- In the same file, `approved_task_step(task, records, step)` is the shared "current approval" contract. It takes the latest record per step in the task's current `review_epoch` (`current_task_steps`), requires `approved`, and accepts either a `commit_sha` matching the latest linked commit (for tip-bound steps) or a null `commit_sha` while the task has no linked commits. `StepSubmission.disposition` is `moot`, `superseded` or `wont_do`. `record_task_step` refuses a no-work approval on a task with linked commits ("linked work requires commit evidence"). Every `TaskStepRecord` stores its writer's `agent_definition`. `required_task_steps` returns no steps for epics.
- `src/gobby/tasks/close_invariants.py` passes `linked_commits` with a linked commit or a close reason in `NO_WORK_CLOSE_REASONS` (`moot`, `superseded`, `wont_do`) and fails `children_closed` while any child is open.
- `src/gobby/storage/tasks/_close.py` closes each ancestor whose last open child just closed, using that child's close reason. It stops at an ancestor that is claimed, carries a hold label, or is a non-epic with its own validation criteria.
- `src/gobby/config/tasks.py::TaskClosePolicyConfig` defaults `review_categories` to `["code", "docs"]` and validates against `VALID_CATEGORIES`, which includes `research`.
- `.gobby/project.json` sets `task_close_policy.enforce_step_records: true`, so the default change applies to Gobby.
- `src/gobby/storage/tasks/_queries.py::_external_blocker_exists_sql` feeds `projected_task_state_sql`; `src/gobby/storage/tasks/_blocking.py::_fetch_active_blocker_map` is the hydration site.
- `claim_task` does not refuse blocked tasks, and the spawn guard `active_task_response_if_blocked` concerns active runs, not dependency state.
- The `rules/tdd-enforcement/` directory is gone, so workspace helper scripts face no TDD gate.

Target scope rules:
- **Bare paths for files that exist only on `task-23850` or not yet.** The main index has no record of them. This session's managed gcode principal is frozen to the main checkout and returned `code_overlay_mismatch` for the #24797 worktree overlay, the visibility gap tracked by #20664.
  - On the branch, read with `git show task-23850:<path>`: `src/gobby/tasks/step_records.py` and `src/gobby/mcp_proxy/tools/tasks/_step_records.py`.
  - Not yet created; #24797's 3.1-3.5 create them, and this plan relies only on those sections' stated contracts: `src/gobby/agents/team_lifecycle.py`, `src/gobby/agents/team_review.py`, `src/gobby/agents/team_teardown.py`, `src/gobby/install/shared/workflows/pipelines/devteam.yaml`, `src/gobby/install/shared/workflows/agents/dev-manager.yaml`, `src/gobby/install/shared/workflows/agents/test-writer.yaml`, `src/gobby/install/shared/workflows/agents/triage.yaml` and `src/gobby/install/shared/skills/gobby/references/agents/procedures/dev-manager.md`.
- **`path::*` for symbols that exist only on the branch.** `src/gobby/config/tasks.py` and `src/gobby/storage/tasks/_queries.py` exist on main, but the target symbols live only on the branch (main still has a duplicate `_external_blocker_exists_sql` in `_aggregates.py`, which the branch consolidates). Before expansion, after #24797 lands, the Plan Writer refreshes these Targets to symbol scope and reruns consumer sweeps.
- **Corrected module names.** The per-epic report's draft names `review_verdicts.py`, `state_queries.py` and `_ancestor_gate.py`. They are `step_records.py`, `_queries.py` and `_blocking.py`.
- **Files near the ceiling.** Line counts on main: `src/gobby/sessions/clear_continuation.py` 864, `src/gobby/mcp_proxy/tools/sessions/_terminal.py` 816, `src/gobby/runner_init/orchestration.py` 811, `src/gobby/hooks/event_handlers/_agent.py` 787. This plan avoids `clear_continuation.py`: clear inheritance uses `src/gobby/hooks/event_handlers/_session_start/materialize.py::_bind_clear_successor` (568 lines). New logic goes in new modules, and the large files get only call sites. Production files stay below 1,000 lines.

Use isolated tests: `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest <focused targets>`. Never run the full suite. Each leaf runs `uv run ruff check`, `uv run ruff format --check` and `uv run mypy src/` on its changes. Template changes are confirmed in the installed registry rows after sync before any rule or definition is declared live.

## P7: Generic team contract (depends: #24797)
`kind: framing`

A team is a runbook pipeline bound to one task through the redesign 3.1 team record. Roles are defined by step grants, not names:
- **Manager:** holds no claim; it dispatches, merges, links the epic tip, lands, and writes the `closing` record. It is marked by the definition variable `team_manager: true`, which #24797 delivers with the seat-spawn exemption.
- **Makers:** write `test` and/or `implementation` records for the work they produce.
- **Independent reviewers:** write `review` records without editing the work they approve. On rejection two, a Code Reviewer becomes the leaf's fixer and a fresh independent reviewer takes its review binding. Independence applies to the resulting candidate. The team or pod binds reviewer seats for its own scope (Decisions 21 and 25).

Project policy (`test_categories`, `review_categories`) and task type decide which records a close needs (3cb34179). Landing (2.4) consumes the reviewer's record, and close checks the records' approval. Neither names a role.

Places that would otherwise be dev-specific, and their owners:
1. `src/gobby/workflows/engine/seat_transitions.py::advance_seat` and its candidate-binding check key reviewer and maker steps on the literal names `code-reviewer` and `developer`. Owner: 7.1.
2. `src/gobby/workflows/step_binding.py::release_completed_review` keys verdict recovery on `code-reviewer`. Owner: 7.1.
3. `src/gobby/mcp_proxy/tools/tasks/_session.py::link_task_to_session` requires a running `code-reviewer` seat for the `reviewing` link. Owner: 7.1.
4. The `devteam` runbook's reviewer seat is fixed to `code-reviewer`; it becomes the `reviewer_agent` input and the team's first reviewer binding, and epic mode adds a binding per routed reviewer definition. Owners: 7.2 (admission), 7.3 (pipeline) and 8.2 (epic reviewer spawns).
5. Epic dispatch spawns the team's implementation-grant definition recorded in the team record, not a hardcoded `developer`. `implementation_domain` routes developer skills; it never permits another live primary in the epic worktree. Owners: 8.1 and 8.2.
6. Workspace folders key on `_agent_type` and `persistent_workspace`, with no role list. Owners: 9.2 and 9.3.

Role names stay where they belong: in definitions, procedures and project policy. A future Docs, Research or SEO team supplies definitions with grants, a pipeline and procedures, and needs no new close or landing code.

### 7.1 Key reviewer and maker seat steps on step grants
`kind: deliverable`
`[category: code]`

Targets:
- `src/gobby/workflows/engine/seat_transitions.py::*` — scope-reason: replace the code-reviewer and developer name checks in advance_seat and candidate binding with step-grant checks
- `src/gobby/workflows/step_binding.py::*` — scope-reason: key release_completed_review on the review grant
- `src/gobby/mcp_proxy/tools/tasks/_session.py::*` — scope-reason: accept any running review-grant seat for the reviewing link
- `tests/workflows/test_grant_keyed_seats.py`

**Research context:** These three sites decide reviewer and maker seat behavior by `instance.agent_name` or `ar.agent_name`:
- `advance_seat` handles `developer` route_skills and submit-bounce, and `code-reviewer` verdict_ready on `get_task` or `record_review_step`.
- The candidate-bind check near the top of `seat_transitions.py` returns early unless `agent_name == "code-reviewer"`.
- `release_completed_review` (`step_binding.py`) recovers an interrupted verdict only for `code-reviewer`.
- `link_task_to_session`'s `reviewing` action (`_session.py`) requires a running `code-reviewer` agent run. `seat_messages.py::is_task_reviewer` reads that link.

A `docs-reviewer` or `research-reviewer` seat (7.3) would silently get no verdict binding, no recovery and no reviewing link, so this change is required by Decision 25, not speculative generality.

Resolve the session's definition with the branch's `src/gobby/tasks/lifecycle_access.py::session_agent_definition` (read-only reuse). Treat "review" in `task_step_writes` as the reviewer role and "implementation" as the maker role. The plan-adversary, log-monitor and metrics-monitor branches stay name-keyed: they are single-definition behaviors outside the team contract.

Refresh first: if #24797's 3.2-3.5 delivery retires one of these sites, that site needs no edit. Record which sites remain in the leaf's close summary. Rejected: a new `seat_kind` definition field, which would duplicate what `task_step_writes` already states.

**Acceptance:**
- 7.1.1 - A seat whose definition grants only `review` gets verdict binding, verdict recovery and the reviewing link whatever its name; a seat without the grant gets none. test: `tests/workflows/test_grant_keyed_seats.py::test_review_grant_drives_reviewer_steps`.
- 7.1.2 - A seat whose definition grants `implementation` gets the maker route_skills and submit-bounce steps; code-reviewer and developer behavior is unchanged. test: `tests/workflows/test_grant_keyed_seats.py::test_implementation_grant_drives_maker_steps`.

**Verification:** Planned: the focused test file, plus `tests/workflows/test_seat_step_loops.py` from the branch, rerun unchanged.

### 7.2 Route review by category at team admission (depends: 7.1)
`kind: deliverable`
`[category: code]`

Targets:
- `src/gobby/config/tasks.py::*` — scope-reason: add research to the TaskClosePolicyConfig review_categories default, a class that exists only on the redesign branch
- `crates/gcore/assets/config/runtime_config_contract.json::*` — scope-reason: derived config carrier, regenerated and confirmed unchanged because task close policy is not daemon config
- `src/gobby/agents/team_launch_inputs.py::*` — scope-reason: accept the reviewer_agent launch input
- `src/gobby/agents/runbook_seats.py::*` — scope-reason: refuse a reviewer_agent whose definition lacks the review step grant
- `src/gobby/agents/team_lifecycle.py`
- `tests/agents/test_review_routing.py`

**Research context:** On the branch, `TaskClosePolicyConfig.review_categories` defaults to `["code", "docs"]`, and its `validate_categories` checks against `src/gobby/storage/tasks/_models.py::VALID_CATEGORIES`. `research` is valid. Change the default to `["code", "docs", "research"]`. Because `.gobby/project.json` enforces step records with default categories, Gobby research tasks will need an approved review record. `required_task_steps` in `step_records.py` reads the policy, so no close code changes.

The `devteam` runbook gains a required `reviewer_agent` input. `team_launch_inputs.py` passes it through, and `runbook_seats.py` validates it at admission with one review-grant check, which 8.2's epic reviewer spawns reuse: the named definition must be enabled, installed and grant `review`. Admission records it as the first reviewer binding in the 3.1 team record (`team_lifecycle.py`, created by redesign 3.1). The bindings map each reviewer definition to its live seat run. A single-task team has exactly one binding and refuses a second definition. A replacement seat must have its binding's definition.

The admitting seat picks the reviewer with the routing table that 7.3 writes into the procedures. Code has no category map: routing stays definition-driven (Decision 26). Rejected:
- a Python category-to-reviewer map, which would be a dev-specific place;
- a new definition category field, which adds a second source of truth beside the procedure table and still cannot decide docs-about-code.

The config carrier lint requires the contract JSON as a Target. Regenerate it with the repository generator and expect no diff.

**Acceptance:**
- 7.2.1 - A default policy requires a review record for research tasks; code and docs are unchanged. test: `tests/agents/test_review_routing.py::test_research_requires_review_by_default`.
- 7.2.2 - Admission accepts a reviewer_agent with the review grant and records it as the team's first reviewer binding. It refuses a definition without the grant, a second reviewer definition on a single-task team, and a replacement whose definition differs from its binding. test: `tests/agents/test_review_routing.py::test_reviewer_agent_admission`.

**Verification:** Planned: the focused test file, `tests/tasks/test_step_records.py` and the runtime-config contract test.

### 7.3 Ship docs and research reviewers and the routing table (depends: 7.2)
`kind: deliverable`
`[category: config]`

Targets:
- `src/gobby/install/shared/workflows/agents/docs-reviewer.yaml`
- `src/gobby/install/shared/workflows/agents/research-reviewer.yaml`
- `src/gobby/install/shared/skills/gobby/references/agents/procedures/docs-reviewer.md`
- `src/gobby/install/shared/skills/gobby/references/agents/procedures/research-reviewer.md`
- `src/gobby/install/shared/skills/gobby/references/agents/procedures/orchestrator.md::*` — scope-reason: add the reviewer routing table
- `src/gobby/install/shared/skills/gobby/references/agents/procedures/dev-manager.md`
- `src/gobby/install/shared/workflows/pipelines/devteam.yaml`
- `src/gobby/install/shared/workflows/rules/roles/inject-seat-common.yaml::*` — scope-reason: add the two reviewers to the seat catalogue
- `src/gobby/install/shared/workflows/rules/roles/seat-spawn-policy.yaml::*` — scope-reason: add the two reviewers to every seat catalogue
- `src/gobby/install/shared/workflows/rules/roles/seat-no-ask-user-question.yaml::*` — scope-reason: add the two reviewers to the seat catalogue
- `docs/guides/agents.md::*` — scope-reason: document the two reviewer definitions
- `tests/agents/test_category_reviewer_definitions.py`

**Research context:** Model both definitions on the code reviewer as rewritten by redesign 3.5:
- read-only `blocked_tools`;
- `task_step_writes: [review]`;
- `provider: inherit`, with models from project policy, so the bundled definitions stay project-agnostic (memory 89ca3f12);
- the reviewer step workflow, which 7.1 makes grant-keyed;
- a `_seat_procedure` pointing at its procedure reference.

`docs-reviewer.md` checks prose, user-facing and process docs for accuracy against the system they describe, the task's criteria, audience fit and the project's writing rules. `research-reviewer.md` checks each claim against its cited sources and code evidence, using `gobby:references/code-index/evidence.md` and `gcode evidence`. It also confirms that the question asked is the question answered.

Both procedures state: only review; hand findings back to the maker; never edit. Write the record with `record_review_step` at the exact tip.

The routing table goes into `orchestrator.md` and `dev-manager.md` (a #24797 3.5 file):

| Task category | Reviewer |
| --- | --- |
| code, config, refactor, test | `code-reviewer` |
| docs, subject is code behavior | `code-reviewer` |
| docs, any other subject | `docs-reviewer` |
| research | `research-reviewer` |

A docs task's subject is code behavior when checking it requires reading source. It documents what an API, CLI command, MCP tool, rule, config key, agent definition, pipeline or runtime contract does, or its description or Targets name source files or symbols. Narrative, user-facing guides without such claims, plans and process or operating docs go to `docs-reviewer`. The admitting Orchestrator applies the table to the team's bound task, or for an epic passes the precedence winner over its open leaves. In epic mode the Dev Manager applies it per leaf at the leaf's first review and spawns any routed reviewer not yet bound (8.2, 8.5). The epic-tip reviewer follows the precedence `code-reviewer`, then `docs-reviewer`, then `research-reviewer`, over the definitions that wrote the leaves' current review approvals (Decision 25).

`devteam.yaml` (a #24797 3.5 file) takes the reviewer seat's agent from the `reviewer_agent` input. Add both names to the inline seat catalogues so the seat rules cover them.

**Acceptance:**
- 7.3.1 - Both definitions parse, sync, grant only `review`, block every edit tool, and load their procedure. test: `tests/agents/test_category_reviewer_definitions.py::test_reviewer_definitions_are_read_only_review_writers`.
- 7.3.2 - The Orchestrator and Dev Manager procedures carry the identical routing table and the docs-about-code rule. test: `tests/agents/test_category_reviewer_definitions.py::test_routing_table_matches_procedures`.
- 7.3.3 - The devteam runbook launches the reviewer named by `reviewer_agent`, and the seat rules match both new reviewers. test: `tests/agents/test_category_reviewer_definitions.py::test_runbook_and_seat_rules_cover_new_reviewers`.

**Verification:** Planned: template parsing, registry sync and runbook dry-run tests; then inspect the installed definition and rule rows after sync.

## P8: Epic-bound teams (depends: #24797)
`kind: framing`

Decisions 15-24 apply. The lifecycle runs in order:
1. Admit the team on the epic.
2. Dispatch dependency-ready leaves to the primary triplet or parallel Dev Pods.
3. Each leaf's Test Writer writes tests, then its reviewer reviews the tests.
4. The Developer implements, then the reviewer reviews the implementation.
5. Apply the same review rounds as a single task, including a fresh reviewer on rejection two.
6. For each approved delivered leaf, call `record_closing_step`, `land_commit` into the epic worktree, then `close_task`.
7. The daemon ends that leaf's pod and cleans up its merged sub-worktree and branch; primary seats persist.
8. Merge 0.5.0, link and review any dependency-complete ready epic tip.
9. Land that ready tip without waiting for unrelated stuck or unstarted leaves.
10. Close the epic and tear down its primary seats only after all descendants and final landing complete.

Per-leaf state is derived from tasks, records and Git, not stored in the team:
- in flight: a team seat holds the leaf's claim;
- finished: `approved_task_step(leaf, records, "implementation")` returns a record (Decision 20);
- merged: the leaf has no linked commits, or its tip is an ancestor of epic main;
- reviewed: `approved_task_step(leaf, records, "review")` returns a record.

A leaf is no-work when its finishing record carries a disposition and it has no linked commits. Dispatch considers every open non-epic descendant at any depth. Epic-tip readiness also checks closed leaves and prerequisites included in that tip. Epic-type descendants are containers that close through the cascade (Decision 24).

### 8.1 Bind a team to an epic and scope its leaf claims (depends: 7.2)
`kind: deliverable`
`[category: code]`

Targets:
- `src/gobby/agents/team_lifecycle.py`
- `src/gobby/agents/team_launch_inputs.py::*` — scope-reason: derive epic mode and the epic branch and worktree names from the bound task
- `src/gobby/agents/isolation_models.py::*` — scope-reason: carry an explicit worktree directory name in the spawn config and share the spawn default's title-slug rule
- `src/gobby/agents/isolation_worktree.py::*` — scope-reason: honor the explicit worktree directory name
- `src/gobby/agents/runbook_seats.py::*` — scope-reason: refuse overlapping epic and descendant team admissions
- `src/gobby/mcp_proxy/tools/tasks/_lifecycle_claim.py::*` — scope-reason: refuse descendant claims by sessions outside the active epic team
- `tests/agents/test_team_epic_mode.py`

**Research context:** Epic mode is derived from the bound task: an epic with open descendants. The 3.1 team record (`team_lifecycle.py`, new under redesign 3.1) gains the mode, its seat runs and domain metadata, and the maker definition each role uses. Domain metadata routes skills; the team still has only one live primary triplet, and parallel leaves use Dev Pods. The leaf set is every open non-epic descendant at any depth, read live; nothing snapshots it, so found-work leaves created under the epic join automatically. Admission refuses an epic any of whose non-epic descendants has open children, with a structured refusal naming them, because such a parent is neither a container the cascade closes nor a leaf a developer works; the Orchestrator fixes the topology and places found-work leaves under an epic-type descendant. Redesign 3.3 queues teardown from a committed close; the team lookup it uses here matches only the team's bound task, so a descendant leaf close or a cascaded container close never tears the team down.

`isolation_models.py::generate_branch_name` already lets an explicit `branch_name` win. But `isolation_worktree.py::WorktreeIsolationHandler._generate_worktree_path` derives the directory from the branch with `/` replaced by `-`, giving `epic-<seq>-main`, not Decision 18's `wt-epic-<seq>-<title-slug>`. Add an explicit directory name to the spawn config. The title-slug rule is inline in `generate_branch_name`; move it into one module-level function in `isolation_models.py` that `generate_branch_name` and the epic and leaf names both call, so the spawn default keeps its output and no copy of the rule exists. Epic admission passes branch `epic/<seq>/main` and directory `wt-epic-<seq>-<title-slug>` from the epic's title.

Admission refuses an epic any of whose descendants has an active team or a claim by a session outside the admitting team. It also refuses a task whose ancestor epic has an active team. `claim_task` on a descendant of an active epic team's epic succeeds only for a seat of that team; everyone else gets a structured refusal naming the team.

The Dev Manager holds no claim at admission, as Decision 16 requires. Report row 16 is the source of the overlap rules. Its row 8 (skills only for claimed tasks) needs no change, because the working seat now claims its leaf and `claimed_task_extra_skills.py::build_claimed_task_extra_skill_state` already loads that leaf's skills.

**Acceptance:**
- 8.1.1 - Admitting an epic with open descendants records epic mode and creates branch `epic/<seq>/main` in worktree `wt-epic-<seq>-<title-slug>`, the slug truncated to 40 characters as in the spawn default `task-<seq>-<title-slug>`; a title with no slug characters gives `wt-epic-<seq>`; the spawn default's branch name is unchanged; a leaf admission stays single-task. test: `tests/agents/test_team_epic_mode.py::test_epic_admission_names_and_mode`.
- 8.1.2 - Overlapping epic or descendant admissions are refused. test: `tests/agents/test_team_epic_mode.py::test_overlapping_admissions_refused`.
- 8.1.3 - Inside an active epic team, team seats can claim descendants and other sessions cannot. test: `tests/agents/test_team_epic_mode.py::test_descendant_claims_scoped_to_team`.
- 8.1.4 - Admission refuses an epic with a non-epic descendant that has open children; closing a descendant leaf or phase epic does not tear down the primary team. A pod closes with its bound leaf, while the primary team closes with its bound epic. test: `tests/agents/test_team_epic_mode.py::test_topology_and_bound_close_teardown`.

**Verification:** Planned: focused isolated tests, plus `tests/agents/test_team_admission.py` (redesign 3.1) and `tests/agents/test_isolation.py`, which covers `generate_branch_name`, rerun.

### 8.2 Dev Manager dispatches Dev Pods and merges sub-worktrees (depends: 8.1, 7.3)
`kind: deliverable`
`[category: code]`

Targets:
- `src/gobby/mcp_proxy/tools/spawn_agent/_factory.py::*` — scope-reason: derive and bind leaf sub-worktrees for a team manager's leaf spawns
- `src/gobby/agents/team_lifecycle.py`
- `tests/agents/test_team_epic_dispatch.py`

Consumers unchanged:
- `src/gobby/worktrees/deletion.py` — no-edit-reason: Daemon-owned team teardown reuses the existing merged_into ancestry check and dirty, live and unmerged refusals after ending the pod seats; no manager-driven deletion is added.

**Research context:** Josh's 2026-10-28 ruling, implemented under #25298, makes the parallel unit a Dev Pod launched by `run_pipeline(name="devteam", inputs={task: <leaf>, parent_team_id: <epic team UUID>, workspace: <workspace>, report_to: <manager session>, reviewer_agent: <routed reviewer>})`. It launches the Test Writer, Developer and Code Reviewer together in the leaf sub-worktree, bound to their own pod team with the epic as its parent. The tab is `Dev Pod - #<leaf-seq>`. Pass `parent_team_id`, never the epic UUID as `team_id`, which would join the parent instead of creating a pod. The primary triplet remains in the epic worktree for serial leaves. A single-task team cannot admit a pod. The manager can spawn developer, test-writer, code-reviewer, docs-reviewer, research-reviewer and researcher seats as needed within its task scope; a resume remains bounded to its own seats.

Admission enforces the parent team, open descendant leaf, dependency readiness and absence of a competing pod or claim. Maker and reviewer definitions must satisfy the team's grants. Review bindings are scoped to the owning team or pod; separate pods can each bind the same reviewer definition. Within one scope, a replacement takes an ended review seat or the seat being handed the fix after rejection two.

Primary seats remain until epic completion. Pod seats, including the leaf's reviewer, remain through implementation, review, merge and close of that leaf. A replacement reviewer joins the same scope and preserves independence from the fixer.

A primary spawn runs in the epic worktree. A sub-worktree spawn derives branch `epic/<seq>/<leaf>` and directory `wt-epic-<seq>-<leaf>-<leaf-title-slug>` from base `epic/<seq>/main`, using 8.1's directory-name support and shared title-slug function on the leaf's title, and binds the run to the team record.

Read-only reuse: daemon-owned teardown calls `src/gobby/worktrees/deletion.py`, which already checks merged ancestry and refuses dirty, live or unmerged trees.

Landing uses existing tools: after exact-tip approval and dependency checks, the unclaimed Dev Manager calls `record_closing_step`, then `land_commit`, then `close_task`. `src/gobby/tasks/land_commit.py::task_landing_target` routes the approved leaf SHA to epic main; `land_candidate` fast-forwards or constructs a two-parent merge when the target moved on disjoint paths. Shared changed paths or merge conflicts escalate to Triage; the manager leaves the task escalated. After leaf close, the daemon ends only that pod's seats, closes its tab and removes its merged worktree through the existing deletion path with `WorktreeDeletionRequest.merged_into` = `epic/<seq>/main`. Dirty, live and unmerged deletion refusals remain. The manager cleans nothing up itself.

**Acceptance:**
- 8.2.1 - An epic manager runs a Dev Pod with Test Writer, Developer and Code Reviewer on branch `epic/<seq>/<leaf>` in `wt-epic-<seq>-<leaf>-<leaf-title-slug>`, the slug truncated to 40 characters. Single-task admission, foreign leaves, blocked leaves and competing pods are refused. test: `tests/agents/test_team_epic_dispatch.py::test_manager_leaf_spawns_scoped`.
- 8.2.2 - After exact-tip approval, the manager records closing, lands the leaf into epic main and closes it. The daemon ends the pod and deletes its sub-worktree and branch with `merged_into` epic main; an unmerged sub-branch is refused. test: `tests/agents/test_team_epic_review.py::test_pod_lands_and_closes_against_epic_branch`, `tests/agents/test_team_epic_dispatch.py::test_merged_leaf_tree_cleanup`.
- 8.2.3 - Review bindings admit the routed code, docs or research reviewer within the owning team or pod, support independent replacement after rejection two, and refuse definitions without the review grant or unrelated duplicate bindings. test: `tests/agents/test_team_epic_dispatch.py::test_routed_reviewer_bindings`.

**Verification:** Planned: focused spawn-contract tests with fake provider launches and temporary Git repositories.

### 8.3 Intra-epic blockers resolve on finished leaves (depends: 8.1)
`kind: deliverable`
`[category: code]`

Targets:
- `src/gobby/storage/tasks/_queries.py::*` — scope-reason: treat a finished intra-epic blocker as resolved in _external_blocker_exists_sql, a symbol at this path only on the redesign branch
- `src/gobby/storage/tasks/_blocking.py::*` — scope-reason: apply the same predicate in _fetch_active_blocker_map
- `tests/storage/test_intra_epic_blockers.py`

**Research context:** On the branch, `_external_blocker_exists_sql` treats a `blocks` dependency as blocking while `blocker.closed_at IS NULL`, excluding the blocked task's own descendants. `projected_task_state_sql` projects `blocked` from it. `_fetch_active_blocker_map` applies the same rule during hydration. Both must stay one predicate (redesign 2.2's single read contract).

Add the Decision 20 predicate. A blocker is also resolved when:
- (a) blocker and blocked task share an ancestor epic bound to an active epic-mode team record (3.1); and
- (b) the blocker is finished: `approved_task_step(blocker, records, "implementation")` would return a record.

(b) mirrors that shared contract exactly. It takes the latest `implementation` record in the blocker's current `review_epoch` by sequence, never any historical approval, and requires `approved`. It then requires either a `commit_sha` matching the blocker's latest linked commit, or a null `commit_sha` (a no-work disposition) while the blocker has no linked commits. Step records are `task_comments` rows with author type `task_step` whose JSON body carries the step, status, epoch, sequence, commit and disposition (`step_records.py::_records`). The SQL predicate and the hydration path both implement (b), and both must agree with `approved_task_step` on the same rows. Resolve the team record's exact location in pipeline execution metadata from 3.1's delivered code. Test commits alone never satisfy (b), which is why linked commits are not the signal (Decision 20). Outside active epic teams blocking is unchanged. Rejected: a new task column, which would duplicate the record.

**Acceptance:**
- 8.3.1 - Inside an active epic team, a dependent unblocks once its blocker has a current approved implementation record at its tip, and stays blocked on test commits alone or when the record is stale. test: `tests/storage/test_intra_epic_blockers.py::test_finished_blocker_unblocks_inside_team`.
- 8.3.2 - Without an active epic team, or across epics, blocking is unchanged, and SQL projection and hydration agree. test: `tests/storage/test_intra_epic_blockers.py::test_predicate_parity_outside_teams`.
- 8.3.3 - A no-work blocker with an approved disposition and no linked commits unblocks its dependents. A blocker whose latest implementation record is rejected or superseded stays blocking despite an earlier approval at the same commit. SQL projection, hydration and `approved_task_step` agree on every case. test: `tests/storage/test_intra_epic_blockers.py::test_no_work_and_superseded_blockers`.

**Verification:** Planned: focused state-query tests against the isolated hub, plus redesign 2.2's state-contract parity test rerun.

### 8.4 Shared review rounds and dependency-only holds (depends: 8.2)
`kind: deliverable`
`[category: code]`

Targets:
- `src/gobby/agents/team_review.py`
- `src/gobby/agents/team_lifecycle.py`
- `tests/agents/test_team_epic_review.py`

**Research context:** Redesign 3.2's `team_review.py` (new under 3.2) runs the review loop from 2.3's review-step record as the durable trigger. In epic mode:
- Every leaf follows the single-task golden path, including test review before implementation. Rejection one returns to the Developer; rejection two transfers the claim to the reviewer as fixer and binds a fresh independent reviewer to that leaf's team or pod (Decision 21).
- Rejection three escalates the rejected leaf only. Its dependents wait; unrelated ready leaves continue. An audited reset starts a new epoch on that leaf without requiring an epic de-escalation or manager claim.
- An epic-tip rejection returns through an open owning leaf or a found-work leaf when the original leaf is already closed. After repair, approval and merge, review the newly linked epic tip. Unchanged leaves retain their records and the epic's rejection count carries on. Only the epic tip's own third rejection escalates the epic.
- `sync_epic_review` reports a ready tip when all merged leaves are approved and their dependencies have merged or landed. Unstarted, stuck, escalated or externally blocked unrelated leaves do not set the team held. A merged leaf whose dependency is unmet blocks that tip and names both leaves in its holds.
- Explicit epic holds continue to gate pod admission and landing. No leaf is detached or reverted. A merged leaf with its closing record releases its seat from submit and closes on epic-worktree landing (Decisions 23-24).

Resume reads this state from the record and never recounts transcripts. `step_records.py` stays team-agnostic.

**Acceptance:**
- 8.4.1 - Epic leaves use the single-task rounds: rejection one returns to the Developer; rejection two makes the reviewer the fixer and binds a fresh independent reviewer to the leaf's scope. test: `tests/agents/test_team_epic_review.py::test_epic_leaf_uses_single_task_fixer_and_fresh_reviewer`.
- 8.4.2 - A leaf's third rejection escalates only that leaf; the team remains unheld and ready siblings can land. An explicit epic hold still gates admission. test: `tests/agents/test_team_epic_review.py::test_third_rejection_holds_only_its_leaf`.
- 8.4.3 - With A and B approved and merged and C unstarted, the tip is ready and held is false. If merged B depends on open C, the tip is blocked and holds names B and C. No leaf is detached or reverted. test: `tests/agents/test_team_epic_review.py::test_ready_epic_tip_checks_merged_leaves_and_their_dependencies`.
- 8.4.4 - An epic-tip rejection returns through an open owning or found-work leaf. Unchanged leaves keep their records; the next epic-tip round approves the repaired tip as round two, preserving the earlier rejection count. test: `tests/agents/test_team_epic_review.py::test_epic_tip_rejection_repairs_through_leaf`.

**Verification:** Planned: state-transition tests with fake reviewer records, duplicate-verdict replay and restart recovery.

### 8.5 Epic-mode procedures and runbook binding (depends: 8.4, 8.3)
`kind: deliverable`
`[category: config]`

Targets:
- `src/gobby/install/shared/workflows/pipelines/devteam.yaml`
- `src/gobby/install/shared/skills/gobby/references/agents/procedures/dev-manager.md`
- `src/gobby/install/shared/skills/gobby/references/agents/procedures/orchestrator.md::*` — scope-reason: epic admission reviewer choice, topology refusal handling and found-work leaf placement
- `src/gobby/install/shared/workflows/agents/test-writer.yaml`
- `src/gobby/install/shared/workflows/agents/developer.yaml::*` — scope-reason: leaf claims, implementation records at the tip and merge-only branch updates
- `src/gobby/install/shared/skills/gobby/references/agents/procedures/code-reviewer.md::*` — scope-reason: per-leaf and epic-tip review records in epic mode
- `src/gobby/install/shared/skills/gobby/references/agents/procedures/docs-reviewer.md`
- `src/gobby/install/shared/skills/gobby/references/agents/procedures/research-reviewer.md`
- `docs/guides/runbooks.md`
- `tests/workflows/test_team_epic_runbook.py`
- `tests/skills/test_dev_manager_cleanup_guidance.py`

**Research context:** `devteam.yaml` (redesign 3.5) accepts an epic binding; mode stays derived (8.1). `dev-manager.md` (3.5) gains the epic procedure:
- the golden path per leaf: the primary or pod Test Writer claims the leaf, commits and links its tests; its Code Reviewer reviews those tests before the Developer implements; implementation review precedes the manager's merge and close;
- the dispatch rule (Decision 17), merges (Decision 19) and unlock order (Decision 20);
- per-leaf reviewer routing with the 7.3 table at each leaf's first review, spawning any routed reviewer not yet bound (Decision 25);
- the no-work path: a leaf found moot before any commit links to it receives an approved disposition review without a commit; the manager routes it through Triage to the Orchestrator for close; no test, implementation or closing record and no landing are required for this path;
- single-task review rounds, including reviewer-to-fixer and fresh-reviewer transition on rejection two, and epic-tip repair through open owning or found-work leaves (Decision 21);
- the hold rules (Decisions 22-23);
- the leaf close order (Decision 24): approve its exact tip, call `record_closing_step`, `land_commit` into the epic worktree, then `close_task` without a manager claim; the daemon ends the pod and cleans up its merged sub-worktree. Route approved no-work dispositions through Triage to the Orchestrator for close without landing;
- the ready-tip landing order: merge 0.5.0, link and review the exact epic tip, then land under 2.4 while unrelated leaves may remain open. Close the epic and tear down its primary seats only after all descendants and final landing complete.

`orchestrator.md` gains the epic admission rules: pass the precedence winner over the epic's open leaves as `reviewer_agent`; fix a topology that admission refuses for a non-epic parent with open children; and place requested found-work leaves under an epic-type descendant.

`developer.yaml` adds: claim the dispatched leaf; commit only that leaf's work, tagged and linked to it; write the implementation record at the leaf tip and submit the candidate while retaining the claim until manager transfer or task close; never rebase or squash after the first link. Landing conflicts are escalated to Triage by `land_commit`; the manager does not dispatch its own conflict-repair flow. `test-writer.yaml` (3.5) adds the per-leaf commit series.

Reviewer procedures require an independent review record at each leaf's exact tip, or an approved no-work disposition without a commit. The Code Reviewer also reviews tests before implementation and follows the single-task fixer transition on rejection two; the fresh reviewer, not the fixer, approves the resulting candidate. Epic-tip review requires approval of all merged work and its dependency closure, not completion of unrelated leaves. A leaf's test commit remains an ancestor of its implementation commits.

**Granularity:** These files jointly define one epic-mode launch and procedure contract that activates together. Splitting them would let a partially updated seat set launch.

**Acceptance:**
- 8.5.1 - Procedures state the golden path, `devteam(parent_team_id)` pod dispatch, merge-only updates, dependency unlocks, reviewer routing, no-work disposition, shared review rounds, dependency-only holds, closing-record-before-landing order, Triage-owned conflict recovery and daemon-owned teardown, with no detach path. test: `tests/agents/test_team_epic_review.py::test_pod_pipeline_launches_triplet_and_relaunches_in_pod_tab`, `tests/agents/test_team_epic_review.py::test_pod_lands_and_closes_against_epic_branch`, `tests/skills/test_dev_manager_cleanup_guidance.py::test_dev_manager_cleans_nothing_up_in_either_mode`.
- 8.5.2 - Single and epic DevTeams use the same primary seats and golden path; only an epic manager can run a Dev Pod for a parallel leaf. test: `tests/workflows/test_team_epic_runbook.py::test_runbook_binds_epic_or_leaf`.

**Verification:** Planned: template parsing, registry sync and runbook dry-run tests; installed rows inspected after sync.

### 8.6 Pilot one epic team (depends: 8.5, 9.4)
`kind: deliverable`
`[category: test]`

Targets:
- `tests/e2e/test_team_epic_lifecycle.py`
- `.gobby/plans/research/devteam-epic-pilot-results.md`

**Research context:** This proves Decisions 15-24 end to end in isolation, then once live. The isolated e2e covers a six-leaf epic with one nested phase epic:
- inside the phase epic, two parallel code leaves in different domains feed one dependent code leaf;
- directly under the root, a no-work leaf blocks a non-code docs leaf, and a research leaf stands alone.

It runs the golden path with a primary triplet and two sub-worktree Dev Pods, test review before implementation, `record_closing_step` before `land_commit` and `close_task`, daemon-owned pod cleanup, the intra-epic unlock including the no-work blocker, and category-routed review. Landing covers fast-forward and generated two-parent merge paths. Exercise rejection one returning to the Developer and rejection two handing the fix to the reviewer with a fresh independent reviewer. Ready epic tips land without waiting for unrelated leaves. Phase epics close as their children complete; final landing and root close trigger the primary team's teardown and epic workspace folder's final commit.

A second case drives a leaf to its third rejection and proves that only that leaf and its dependents wait for the audited reset; ready siblings continue and the epic remains unheld. Also prove that a merged leaf with an unmet dependency blocks its tip. A third case rejects an otherwise ready epic tip, repairs it through an open owning or found-work leaf, and lands on the second epic-tip round. The live pilot runs one real epic under Release Manager authority and records results in the pilot file, including the primary-team and per-pod lifetimes.

**Acceptance:**
- 8.6.1 - The six-leaf nested epic completes with a persistent primary triplet, parallel Dev Pods, test review, shared review rounds, category routing and no-work handling. Each pod closes and tears down after its leaf merges into the epic; the primary team tears down once after final epic close. test: `tests/e2e/test_team_epic_lifecycle.py::test_complete_epic_team`.
- 8.6.2 - An exhausted leaf holds only itself and its dependents; unrelated ready siblings land before reset. A merged leaf depending on open work blocks the tip. test: `tests/e2e/test_team_epic_lifecycle.py::test_ready_tip_lands_while_sibling_is_escalated`.
- 8.6.3 - A ready epic tip's rejection is repaired through an open owning or found-work leaf, and the next exact tip lands on the second epic-tip round. test: `tests/e2e/test_team_epic_lifecycle.py::test_epic_tip_rejection_repaired_then_lands`.
- 8.6.4 - The live pilot's outcome, timings and any findings are recorded. file: `.gobby/plans/research/devteam-epic-pilot-results.md`.

**Verification:** Planned: isolated e2e, then one live pilot.

## P9: Persistent agent workspaces (depends: #24797)
`kind: framing`

Decision 27 applies. The layout:
- `~/.gobby/agents_workspaces/<project_id>.git/`: the daemon-only Git directory.
- `~/.gobby/agents_workspaces/<project_id>/README.md`: written by the daemon.
- `<definition>/session-<seq>/`: one folder per session, or per clear lineage for a persistent seat.
- `epics/epic-<seq>/`: one folder per epic team.

`<definition>` is the protected `_agent_type`, or `default`. Because the anchor sits under Gobby home and outside every checkout's Git common directory, `src/gobby/hooks/_path_scope.py` classifies literal workspace writes as external. The task, dirty-path, attribution, close, monolith and commit rules therefore already ignore them (workspace report §3.1). Agents must name workspace paths literally.

### 9.1 Workspace repository and daemon committer
`kind: deliverable`
`[category: code]`

Targets:
- `src/gobby/agents/agent_workspaces.py`
- `src/gobby/runner_init/orchestration.py::*` — scope-reason: register the five-minute workspace commit cron in init_orchestration
- `src/gobby/mcp_proxy/tools/sessions/_terminal.py::*` — scope-reason: schedule a workspace commit in set_handoff before the marker is staged
- `src/gobby/hooks/event_handlers/_agent.py::*` — scope-reason: schedule a workspace commit in handle_pre_compact
- `src/gobby/hooks/event_handlers/_session_end.py::*` — scope-reason: schedule a final workspace commit in handle_session_end
- `tests/agents/test_agent_workspaces.py`

**Research context:** The new `agent_workspaces.py` owns:
- the anchor (`get_gobby_home()`, so `$GOBBY_HOME` is honored), keyed by the session's `project_id`;
- repository init with an explicit Git directory (`core.bare=false`, `core.worktree`), never `--separate-git-dir`, so the work tree has no `.git` and agent `git` commands there find no repository;
- one in-process lock per project and the commit step (`git add -A`, commit only when `status --porcelain` is non-empty, message `agents-ws: <trigger> <changed top-level folders>`);
- guards: regular files up to 1 MiB only, symlinks skipped and logged, a 30-second timeout, a weekly `git gc --auto` under the same lock;
- the daemon-written README.

It reuses `src/gobby/utils/daemon_git.py::daemon_git` and the checkpoint author identity in `src/gobby/agents/worktree_checkpoint.py` (`CHECKPOINT_AUTHOR_NAME`, `CHECKPOINT_AUTHOR_EMAIL`). Triggers schedule the async commit and return immediately. A failure is logged and never fails set_handoff, compaction or session end. The cron registration follows `src/gobby/memory/dream/cron.py::register_memory_dream_cron`, called from `init_orchestration`.

The large files get only a call site (`_terminal.py` 816, `orchestration.py` 811, `_agent.py` 787 lines). The clear-successor and epic-close triggers belong to 9.3 and 9.4. Retention is indefinite: nothing deletes folders or history (ruling 1.4 A).

**Acceptance:**
- 9.1.1 - The anchor is identical from the main checkout and a linked worktree, and the work tree holds no `.git`. test: `tests/agents/test_agent_workspaces.py::test_anchor_shared_and_gitless`.
- 9.1.2 - A commit runs only when the tree is dirty, skips oversized files and symlinks, and is serialized per project. test: `tests/agents/test_agent_workspaces.py::test_commit_only_when_dirty_with_guards`.
- 9.1.3 - A commit failure does not fail set_handoff, pre-compact or session end, and the cron trigger commits a dirty tree. test: `tests/agents/test_agent_workspaces.py::test_trigger_failures_are_non_blocking`.

**Verification:** Planned: focused tests with a temporary `GOBBY_HOME` and real Git.

### 9.2 Workspace write and read scope rules (depends: 9.1)
`kind: deliverable`
`[category: code]`

Targets:
- `src/gobby/workflows/condition_helpers_paths.py::*` — scope-reason: add the agent workspace write-dir, write-violation and read-violation helpers to PATH_CONDITION_HELPERS
- `src/gobby/workflows/condition_helpers_sessions.py::*` — scope-reason: bind the predecessor folder eligibility helper in session_condition_helpers
- `src/gobby/agents/agent_workspaces.py`
- `src/gobby/install/shared/workflows/rules/roles/agent-workspace-scope.yaml`
- `src/gobby/install/shared/workflows/rules/roles/seat-write-scope.yaml::*` — scope-reason: allow the researcher, assistant and archivist their own workspace folders
- `src/gobby/workflows/enforcement/blocking.py::*` — scope-reason: let plan_write_paths_allowed accept the session's workspace folders
- `src/gobby/workflows/engine/idle_declaration.py::*` — scope-reason: refuse agent workspace keys in native set_variable
- `tests/workflows/test_agent_workspace_scope.py`

**Research context:** The helpers read three daemon-owned variables: `_agent_workspace_dir`, `_agent_workspace_epic_dir` and `_agent_workspace_persistent`.
- `agent_workspace_write_dirs(variables)` returns the session folder and, when set, the epic folder.
- The new `agent-workspace-scope.yaml` holds two rules.
  - The write rule blocks any write under the anchor outside those dirs. It always blocks `<project_id>.git/` and any `.git` or `.gobby` segment inside the work tree.
  - The read rule enforces Decision 27. It allows the session folder, the epic folder and the root README. For persistent sessions it also allows eligible predecessor folders, never the whole `<definition>/` directory.
- Predecessor eligibility is resolved at read time, because a sibling can end, or a predecessor be resumed, mid-session. The new `agent_workspaces.py::predecessor_folder_readable` takes the caller and a folder. It is true only when the caller is persistent and the folder sits under the caller's `<definition>/`, has a lower sequence number than the caller's session, and is the `_agent_workspace_dir` of no session of that definition and project whose status is in `src/gobby/storage/sessions/_constants.py::LIVE_SESSION_STATUSES`. `src/gobby/workflows/condition_helpers_sessions.py::session_condition_helpers` already binds session-aware helpers to the evaluator's `SessionManager`, so it binds this one as well. The stateless path helpers stay in `condition_helpers_paths.py`.
- The read rule checks resolved paths for `canonical_tool_kind` read and search tools, and literal anchor-prefixed path tokens (absolute or `~/`) in shell commands. It is a hygiene boundary against state pollution, not a security boundary, as ruling 1.1's purpose states.

`seat-write-scope.yaml`'s three rules add `agent_workspace_write_dirs(variables)` to their allowed dirs; the archivist's rule ORs `write_paths_within` with its regex match. `blocking.py::plan_write_paths_allowed` also accepts those dirs. Native `set_variable` refuses `_agent_workspace_*` keys for the roles in `NATIVE_VARIABLE_ROLES`, so no session can repoint its own scope.

Opaque script execution still needs a claim (ruling 1.2 A); `require-task-before-edit`'s script branch is unchanged. Existing helpers to reuse: `_resolved_write_paths`, `write_paths_within` and `resolve_tool_path`.

**Acceptance:**
- 9.2.1 - Writes to the session's own folder and its epic folder pass for the researcher, assistant, archivist, developer and a plan-mode session; writes to another folder, the Git directory or a `.gobby` segment are blocked. test: `tests/workflows/test_agent_workspace_scope.py::test_write_isolation`.
- 9.2.2 - A non-persistent seat can read only its own folder, its epic folder and the README; a persistent seat can also read its definition's eligible predecessor folders, for both read tools and literal shell paths. test: `tests/workflows/test_agent_workspace_scope.py::test_read_scope_by_persistence`.
- 9.2.3 - A literal workspace write sets no `canonical_repo_mutation`, leaves no task edit-ledger entry, and native `set_variable` cannot change workspace keys. test: `tests/workflows/test_agent_workspace_scope.py::test_exemptions_and_variable_protection`.
- 9.2.4 - A persistent seat is refused a live same-definition sibling's folder, a later-started ended session's folder and a resumed predecessor's folder while it is live, and allowed an ended earlier predecessor's folder. A non-persistent seat is refused every predecessor folder. test: `tests/workflows/test_agent_workspace_scope.py::test_predecessor_eligibility`.

**Granularity:** Seven production files express one scope boundary over the same three daemon-owned variables. The write rule, the read rule, the role write-scope allowances and the variable protection must activate together, or a session could hold a folder it is not confined to, or read beyond the approved scope.

**Verification:** Planned: focused rule-engine tests with an isolated registry.

### 9.3 Session folders, the persistent flag and clear inheritance (depends: 9.2)
`kind: deliverable`
`[category: code]`

Targets:
- `src/gobby/workflows/agent_models.py::*` — scope-reason: add the persistent_workspace flag to AgentDefinitionBody
- `src/gobby/agents/agent_workspaces.py`
- `src/gobby/hooks/event_handlers/_session_start/materialize.py::*` — scope-reason: bind the session folder at activation and inherit it for persistent clear successors in _bind_clear_successor
- `src/gobby/mcp_proxy/tools/sessions/_terminal.py::*` — scope-reason: add the workspace folder to handoff references
- `src/gobby/install/shared/workflows/rules/roles/inject-agent-workspace.yaml`
- `src/gobby/install/shared/workflows/agents/assistant.yaml::*` — scope-reason: set persistent_workspace
- `src/gobby/install/shared/workflows/agents/orchestrator.yaml::*` — scope-reason: set persistent_workspace
- `src/gobby/install/shared/workflows/agents/archivist.yaml::*` — scope-reason: set persistent_workspace
- `src/gobby/install/shared/workflows/agents/release-manager.yaml::*` — scope-reason: set persistent_workspace
- `src/gobby/install/shared/workflows/agents/triage.yaml`
- `tests/agents/test_agent_workspace_lifecycle.py`

**Research context:** `AgentDefinitionBody` gains `persistent_workspace: bool = False`. The five Admin definitions set it; `triage.yaml` comes from redesign 3.5.

At session activation (`materialize.py::activate_materialized_session`), the new `bind_session_workspace` sets `_agent_workspace_dir` to `<definition>/session-<seq>/` and `_agent_workspace_persistent` from the definition, unless a clear successor already inherited a folder. `_bind_clear_successor` copies the predecessor's `_agent_workspace_dir` only when the successor's definition is persistent, and schedules a clear-binding commit (the sixth trigger). The folder keeps the lineage's first sequence number. Otherwise the successor gets a fresh folder. The predecessor's write access ends with its session.

Compaction and resume keep the same session and folder. A manual `/clear` without a marker is a new session with a new folder. Web chat clear (`clear_continuation.py::commit_web_chat_clear_successor`) carries no seat variables, so it gets a fresh folder with no change. set_handoff adds the folder to the handoff's references.

`inject-agent-workspace.yaml` holds two rules modeled on `inject-seat-common` and `reset-seat-common-on-context-loss`. One injects the literal path once per context epoch, with "write literal paths only", through `{{ variables.get('_agent_workspace_dir') }}`. For a persistent session it adds: "Predecessor folders under your definition are readable only to recover from a crash or learn from mistakes in prior sessions." The other re-arms it on clear or compact.

**Granularity:** Four production modules and six templates express one lifecycle owner, the session folder binding. The flag, binding, inheritance and injection are untestable apart.

**Acceptance:**
- 9.3.1 - A session gets its own folder at start and keeps it across compaction and resume; a manual clear gets a new folder. test: `tests/agents/test_agent_workspace_lifecycle.py::test_session_folder_binding`.
- 9.3.2 - A persistent seat's clear successor inherits the predecessor's folder; a non-persistent seat's successor gets a fresh folder. test: `tests/agents/test_agent_workspace_lifecycle.py::test_clear_inheritance_by_persistence`.
- 9.3.3 - Exactly the five Admin definitions set `persistent_workspace`, and the injection and reset rules deliver the path once per epoch, with the predecessor-read restriction for persistent sessions only. test: `tests/agents/test_agent_workspace_lifecycle.py::test_persistent_set_and_injection`.

**Verification:** Planned: focused session-start and clear-successor tests against the isolated hub.

### 9.4 Epic workspace folders (depends: 9.3, 8.4)
`kind: deliverable`
`[category: code]`

Targets:
- `src/gobby/agents/team_lifecycle.py`
- `src/gobby/agents/team_teardown.py`
- `src/gobby/agents/agent_workspaces.py`
- `tests/agents/test_agent_workspace_epics.py`

**Research context:** For epic-mode teams only, the team record sets `_agent_workspace_epic_dir` to `epics/epic-<seq>/` on every seat run, including every pod's Test Writer, Developer and Code Reviewer, and clears it when a run ends. A resumed seat gets it back only if the team still names it. `agent_workspaces.py` writes the epic README with its convention: one file per topic, append with Edit. At the bound epic's close (a cascaded phase-epic close never triggers it, per Decision 24), redesign 3.3's teardown (`team_teardown.py`, new under 3.3) schedules the final commit with message `epic-<seq>` and clears the variable on any surviving seat. Nothing in the workspace is a close invariant.

**Acceptance:**
- 9.4.1 - Every seat of an epic team, including every pod's Test Writer, Developer and Code Reviewer, can write the epic folder; sessions outside the team cannot. test: `tests/agents/test_agent_workspace_epics.py::test_epic_folder_scoped_to_team`.
- 9.4.2 - After epic close and teardown, the final `epic-<seq>` commit exists and later writes are refused. test: `tests/agents/test_agent_workspace_epics.py::test_epic_close_commit_and_lock`.

**Verification:** Planned: focused lifecycle tests with temporary repositories.

## D1 Node workspace sync (depends: 9.1)
`kind: deferred`

Ruling 1.3 A: node sync uses incremental Git bundles over the existing authenticated node channel, with the hub repository as authority. It is built when the first seat runs on a node, which is this deferral's trigger. The design to plan then (workspace report §6):
- a node daemon clone at the same anchor;
- a new `agent_workspace.sync` node-channel command carrying bundles at each commit trigger and on the timer;
- hub fetch and merge, with a return bundle;
- Git's `union` merge driver for epic-folder Markdown, set by the daemon in `info/attributes`;
- `<file>.conflict-<machine_id>` copies for any other conflict, with the Dev Manager notified;
- the AGENTS.md crate rules for the gdaemon command: version bump and promotion through `promote_workspace_binary_set`.

Until then, a seat on a node writes a node-local workspace.

```yaml
deferral:
  task_ref: "created-at-expansion"
  reason: "Ruling 1.3 A builds node sync when the first seat runs on a node; until that trigger, node seats keep node-local workspaces."
  owner: "orchestrator"
  original_acceptance_items:
    - D1.1
    - D1.2
```

- D1.1 - A node's workspace commits reach the hub repository as incremental bundles over the node channel, and the hub's missing commits return to the node.
- D1.2 - Concurrent epic-folder edits from two machines merge with the union driver; other conflicts keep both versions and notify the Dev Manager.

## V1 Plan Changelog
`kind: verification`

- 2026-10-22: Narrative drafted by the Lane 1 Plan Writer gobby#17344 from the two research reports, the five ruling memories and the Orchestrator's 2026-10-22 rulings and scope additions.
- 2026-10-22: Consensus between the Plan Writer gobby#17344 and the Plan Adversary gobby#17345 on re-review of 76cab11e6a757e2e6c3b12b83ef7df988f708aa1, after the review of 0860e011c3bbe48db46f5f2505605c59eb29d399. All five blocking findings were accepted and fixed. ADD-01: mixed-category epics get reviewer bindings keyed by definition in the team record, seeded from `reviewer_agent`, added by Dev Manager spawns under the shared review-grant check, with later rounds and the epic-tip precedence derived from records (Decision 25, 7.2, 8.2). ADD-02: nested phase epics close through the existing `_close.py` ancestor cascade, the claimed bound epic is closed by the Dev Manager, admission refuses non-epic parents with open children, and teardown keys on the bound epic only (Decision 24, 8.1, 9.4). ADD-03: finished means `approved_task_step` for the implementation step, which covers no-work dispositions and latest-record semantics; no-work leaves skip merge and close with their disposition (Decision 20, 8.3, 8.5). ADD-04: predecessor folders are limited to earlier lineages of the caller's definition bound to no live session, checked at read time (Decision 27, 9.2, 9.3). ADD-05: epic-tip rejections return through owning or Orchestrator-created found-work leaves under the epic's existing counter (Decision 21, 8.4). No open questions, unresolved decisions or proportionality findings remain.
- 2026-10-22: Revision for Josh's two pre-approval blockers, relayed by the Orchestrator #17281, after the M1 stamp at dbcdea06860d23b9c697605a3281d3d3ac557cb3, which this revision supersedes. (A) Module and test references follow #24797's generic names: `team_lifecycle.py`, `team_review.py`, `team_teardown.py` and `test_team_*`. "DevTeam" remains only for definitions, the `devteam` runbook, procedures and verbatim quotes. (B) #24797 owns every `lane-manager` removal and the `team_manager` seat-spawn exemption, so P7's lane-manager item, both gap notes and 8.2's seat-rule Targets and acceptance item are dropped. The stale M1 was removed for re-derivation.
- 2026-10-22: Renewed consensus between the Plan Writer gobby#17344 and the Plan Adversary gobby#17345 on delta re-review of 017af9e279bf4a4a73b7093c3f278faec9be4dfc, which applied Josh's blockers A and B. (A) Every active module and test reference uses `team_lifecycle.py`, `team_review.py`, `team_teardown.py` and `test_team_*`, matching the 3.1-3.5 names and ownership in #24797's branch plan. (B) 8.2 consumes #24797's `team_manager` exemption and owns only the epic spawn scope, merges and reviewer bindings; 7.3's catalogue edits add only `docs-reviewer` and `research-reviewer`. The ADD-01 to ADD-05 repairs remain intact, base validation passes, and no open questions, unresolved decisions or blocking findings remain.
- 2026-10-22: Revision for Josh's worktree-name amendment, relayed by the Orchestrator #17281, after the renewed M1 rendered from a3ad227ac8f6b534e6b21b12878b28801b7282d7 (digest b93aba02cc7839d5f462492126d3b3bb5eae853eb65ea1b333c3c7b26d9cd23f), which was never committed and is withdrawn for re-derivation. Decision 18, 8.1 and 8.2 now name the epic worktree `wt-epic-<seq>-<title-slug>` and a leaf sub-worktree `wt-epic-<seq>-<leaf>-<leaf-title-slug>`. The slug uses the spawn default's rule, which 8.1 moves out of `generate_branch_name` into one shared function. Branch names stay `epic/<seq>/main` and `epic/<seq>/<leaf>`. 8.1.1, 8.2.1 and 8.1's verification cover the slugs, truncation and the unchanged spawn default.
- 2026-10-22: Renewed consensus between the Plan Writer gobby#17344 and the Plan Adversary gobby#17345 on delta review of 234a9c700d4e4eb532240513fb98a1114a6288a4, which applied Josh's worktree-name amendment. The Adversary checked the slug rule against `isolation_models.py::generate_branch_name` and the trailing-hyphen strip in `_sanitize_branch_name`. Moving the rule into one shared function keeps the spawn default's output and covers the titled and empty-slug cases. No open questions, unresolved decisions or blocking findings remain.

- 2026-10-29: #25298 reconciles the active narrative and acceptance carriers with Josh's 2026-10-27 dependency-only holds ruling and 2026-10-28 DevTeam ruling (904f11ae). Parallel leaves use Dev Pods; tests are reviewed before implementation; single and epic leaves share review rounds; each leaf closes after landing into the epic worktree, then the daemon tears down its pod. Earlier dated consensus entries remain historical evidence and do not override these corrections. This documentation update adds no implementation or new plan expansion.
- 2026-10-29: Orchestrator gobby#17281 accepted six preexisting `production-size-growth` findings at approved code candidate `d21a360cd847e144f195ad196b686a71abede0a5` as historical target drift: `src/gobby/agents/team_lifecycle.py` (851 lines) in sections 7.2, 8.1, 8.2, 8.4 and 9.4; `src/gobby/runner_init/orchestration.py` (896 lines) in section 9.1. These sections describe already-delivered work. His ruling forbids decomposition or retargeting under this doc-only addendum; validation must introduce no findings beyond these six. Any future code growth goes through the monolith hook in its owning task.
- 2026-10-29: #25298 review-bounce repair aligns the active narrative with delivered `devteam(parent_team_id)` admission, `record_closing_step` → `land_commit` → `close_task`, Triage conflict escalation and daemon-owned teardown. It replaces the nonexistent procedure-test carrier, includes all pod seats in epic workspace scope and restores chronological changelog order. The integrated candidate merges current 0.5.0 and validates these corrections with the existing DevTeam tests.
- 2026-10-29: After merging 0.5.0 at `305094d57d73684c41323bf6d058d7a913b303b1`, Orchestrator gobby#17281 accepted a seventh `production-size-growth` finding in section 9.2: `src/gobby/workflows/condition_helpers_sessions.py` is 951 lines (901 in the merged main commit, plus the existing 50-line task delta). The five `team_lifecycle.py` findings now report 856 lines; `orchestration.py` remains 896. All seven are historical target drift, below the 1,000-line ceiling. His ruling remains no decomposition or retargeting; the integrated validation must report no findings beyond these seven.

## V2: Verification
`kind: verification`

These are completion gates for the implementation. Except for plan validation, none has run yet. Each runs after the leaves it names land, and each must hold before the epic closes.

- Plan validation uses `uv run gobby plans validate .gobby/plans/runbook-devteam-addendum.md -p <task-checkout>`. For the 2026-10-29 integrated correction, the accepted result is the seven historical size findings recorded in V1 and no new findings. Expansion validation is not run: this revision has no M1 and does not authorize a new expansion.
- Each leaf's focused tests, Ruff and mypy must pass on its final commit with the isolated test hub.
- After 7.3, 8.2, 8.5 and 9.3 land, the installed definition and rule rows must match the templates.
- The 8.6 isolated e2e must pass, and the live pilot results must be recorded in `.gobby/plans/research/devteam-epic-pilot-results.md`.
- After 9.4, a real session's workspace history must show commits from every trigger: timer, set_handoff, pre-compact, session end, clear binding and epic close.
