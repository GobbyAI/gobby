Plan artifact: `.gobby/plans/runbook-devteam-addendum.md`

# Runbooks addendum: epic DevTeams, category reviewers and agent workspaces

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
- Memory f3025551 (rulings 3 and 4): the reviewer only reviews; three fails escalate the epic; a stuck leaf holds the epic; merge 0.5.0 into the epic branch, never rebase or squash.
- Memory 00d1e3fa (ruling 5): epic and leaf branch and worktree names.
- Memory ac82c140 (#24801 rulings 1-4): workspace reads, helper scripts, node sync and retention.
- Orchestrator #17281 rulings of 2026-10-22 on #24810 (Q1 escalation counter, Q2 workspace read scope and the persistent set), and Josh's two scope additions relayed the same day: category reviewers for docs and research, and the DevTeam as a model for other teams.

No push is authorized by this plan. Operator-owned uncommitted work remains untouched.

## Decision Record
`kind: framing`

Numbering continues the redesign's Decisions 1-14.

15. **Epic mode.** A DevTeam bound to an epic with open descendants runs in epic mode; any other bound task runs the redesign's single-task flow. Mode comes from the bound task, with no setting. The same `devteam` runbook serves both. Every Decision 1-14 rule applies unchanged except as stated in Decisions 15-24. Ruling 1, verbatim: "I want to be able to parallelize work in the same epic. The Dev Manager would spawn x devs and handle sub-worktrees merging back into the epic worktree, where x is the maximum number of parallel tasks the epic permits based on dependencies. Only the primary dev agents would persist until the epic is complete."
16. **Claims.** The Dev Manager holds the epic claim for the team's life. It uses that claim for merge commits, 0.5.0 merges, linking the epic tip and landing. Each working seat claims the leaf it works: the Test Writer while writing a leaf's tests, a Developer while implementing or fixing a leaf, and the Dev Manager while batch-closing it. Inside an active epic team only that team's seats may claim the epic's descendants. The per-epic report's "leaves are never claimed" (its rows 2-3 and §5) belonged to the overruled one-Developer design. Parallel developers each need their own claim for edit attribution and their own tip-bound implementation record, so ruling 1 requires leaf claims.
17. **Developers and dispatch.** The developer names in ruling 1 ("backend-developer or frontend-developer or fullstack-developer") are the single `developer` definition launched with the leaf's `implementation_domain`; no new definitions (Orchestrator confirmation, 2026-10-22). The Dev Manager keeps at most one primary developer per domain, launched on first need and kept until the epic completes; a leaf without a domain uses the `fullstack` primary. The dispatch rule: when exactly one leaf is ready and no leaf is in flight, that leaf's domain primary works it in the epic worktree. Otherwise each ready leaf gets its own sub-worktree developer, which ends after recording its implementation. x is therefore the ready width the dependency graph allows at that moment.
18. **Names.** Ruling 5, verbatim: "2.5 Your suggestion". The epic branch is `epic/<epic_ref>/main` in worktree `wt-epic-<epic_ref>`. A parallel leaf branch is `epic/<epic_ref>/<leaf_ref>` in worktree `wt-epic-<epic_ref>-<leaf_ref>`, branched from `epic/<epic_ref>/main`; the Dev Manager merges it back and deletes it. A ref is the task's sequence number without `#`.
19. **Merges.** The Dev Manager merges a finished sub-branch into epic main with a merge commit, only while the epic worktree is clean, then deletes the sub-worktree and branch. On a conflict it aborts and dispatches a developer to merge epic main into the sub-branch, then retries. Before the final review it merges 0.5.0 into epic main. f3025551: "merge 0.5.0 into the epic branch; never rebase or squash." Merge commits link to the epic, never to a leaf, so each leaf's tip-bound records stay at its own tip.
20. **Intra-epic unlock.** Josh, 2026-10-21 13:40 CDT (recorded in ed664ba7): "intra-epic blockers unlock with registered commits." Ruling 4, verbatim: "Parallel tasks that feed a single blocker have to finish before the next step is unblocked." Inside an active epic team, a blocker counts as resolved once it is finished: `approved_task_step(blocker, records, "implementation")` returns a record. That is the latest implementation record in the blocker's current epoch, approved, and either naming its latest linked commit or carrying a no-work disposition while the blocker has no linked commits. A later rejected or superseding record, or a newer linked commit, makes an earlier approval stale. The Test Writer's earlier test commits are registered commits too, so linked commits alone would unlock every dependent after the test phase. The implementation record is the registered commit that marks the blocker finished. The Dev Manager merges a work-bearing blocker before dispatching its dependents; a no-work blocker has nothing to merge. Outside epic teams blocking is unchanged.
21. **Review at the end.** Ruling 1: "give dev agents more freedom in their worktree and do the hard review at the end, before the entire epic with all of its commits is merged into the main checkout." Ruling 3, verbatim: "2.3 A. Reviewer only reviews in this runbook. It hands back to developers to fix. 3 fails = escalate epic." Review starts when every leaf is finished (Decision 20) and every work-bearing leaf is merged. Each leaf goes to its routed reviewer (Decision 25), which writes the leaf's review record at its tip; a no-work leaf's review approves its disposition with no commit. Then the epic-tip reviewer writes one review record on the epic at the epic tip, which is the landing stamp. A rejected leaf goes back to a developer under the dispatch rule. Epic mode drops the redesign 3.2 step in which the Sol reviewer becomes claimant and fixer on the second rejection. The fresh Opus review existed only to review that fixer's work, so the same reviewer seat reviews every round: a leaf's later rounds go to the seat of the definition that wrote its earlier review records (`TaskStepRecord.agent_definition`).

An epic-tip rejection is repaired through leaves, because developers work only leaves and the reviewer never fixes. The Dev Manager assigns each finding to the open leaf whose work it concerns. When no leaf owns it, for example a defect from the 0.5.0 merge, the Dev Manager asks the Orchestrator over `send_message` for a found-work leaf under the epic (only the Orchestrator edits tasks), and that leaf joins the live leaf set (8.1). Each assigned leaf then runs the normal path: tests if its category requires them, a developer under the dispatch rule, merge, and its routed leaf review. Leaves whose tips did not move keep their current records. The landing order then repeats from the 0.5.0 merge: link the new epic tip and request the next epic-tip round. The epic's review counter carries on, and a third epic-tip rejection escalates under Decision 22.
22. **Three fails.** Orchestrator Q1 ruling: the redesign 2.3 per-task review counter is reused unchanged and no new counter is added. Inside an active epic team, a leaf's third rejection, or the epic-tip review's third rejection, also escalates the parent epic for a Triage audited reset. Escalating the epic uses the same claim-releasing escalation path as 2.3. The Dev Manager therefore loses the epic claim, and with it every merge, dispatch, review round and landing, until Triage resets the rejected task (a new review epoch) and de-escalates the epic. The Dev Manager then reclaims the epic. In-flight developers may finish and record their implementations meanwhile.
23. **Holds, never detach.** Ruling 4, verbatim: "2.4 B was correct." A stuck leaf, whether escalated or blocked by a task outside the epic, holds the whole epic. Leaves are never backed out. The report's detach-by-revert path (§4.3) is not built.
24. **Landing and close order.** Ruling 1 and Josh's 13:40 input: leaves "close in a batch at the end, after landing" through the four close invariants plus the reviewer's stamp. The order: merge 0.5.0 into epic main; link the epic tip to the epic; the epic-tip reviewer approves that exact tip; the Dev Manager lands it under redesign 2.4; the Dev Manager writes each leaf's closing record and closes the leaves bottom-up; it closes the epic; redesign 3.3 teardown runs. Ruling 2 (3cb34179) decides each leaf's required records through project policy and task type. Epics need no records, so the epic-tip review record is the landing stamp, not a close gate.

The close covers the whole admitted descendant graph. A work-bearing leaf closes `done` with its linked commits. A no-work leaf closes with its disposition as the close reason, which `NO_WORK_CLOSE_REASONS` accepts. Intervening phase epics are containers: team seats never claim them, and `_close.py`'s ancestor cascade closes each one when its last child closes, with that child's reason. So inside each container the Dev Manager closes no-work leaves before work-bearing ones, and the container closes `done` whenever it held any work. The Dev Manager's claim stops the cascade at the bound epic, which it then closes with its linked tip. A container the cascade leaves open because it carries a hold label holds the bound epic's close like a stuck leaf (Decision 23), and the Dev Manager reports it to the Orchestrator. Teardown and the epic folder's final commit key on the bound epic's close only; no descendant close queues them.
25. **Category reviewers.** Josh, 2026-10-22: "code reviewer is correct when it involves writing docs for code, but we probably need a review agent for docs and for research." Two definitions are added. `docs-reviewer` reviews prose, user-facing and process docs. `research-reviewer` reviews research reports: it checks claims against cited sources and code evidence, and confirms that the question asked is the question answered. Each writes only the review record through the 2.3 writer and only reviews. Routing: tasks in categories code, config, refactor and test go to `code-reviewer`. Docs tasks go to `docs-reviewer`, except a docs task whose subject is code behavior, which goes to `code-reviewer`. Research tasks go to `research-reviewer`. `research` joins the `review_categories` default.

The team record holds reviewer bindings, one live seat per reviewer definition. Admission seeds the first binding from the `reviewer_agent` launch input; the admitting Orchestrator routes the bound task, or for an epic passes the precedence winner over its open leaves. A single-task team keeps exactly that binding. In epic mode the Dev Manager routes each leaf at its first review, and when the route names a definition with no binding yet it spawns that reviewer (8.2), adding a binding. Every binding passes the same review-grant check at admission and at spawn. A replacement seat must have its binding's definition. The epic-tip reviewer follows the precedence `code-reviewer`, then `docs-reviewer`, then `research-reviewer`, over the definitions that wrote the leaves' current review approvals. Its binding therefore already exists.
26. **Generic team contract.** Josh, 2026-10-22, verbatim: "That's the idea. Our DevTeam pipeline becomes a model for other teams, like an SEO team or a Docs team or a Research team." Team mechanics key on definition step grants and pipeline inputs, never on role names. A future team needs only new definitions and a pipeline, with no new close or landing code. Those teams are not planned here. P7 states the contract.
27. **Workspaces.** One daemon-committed Git repository per project at `~/.gobby/agents_workspaces/<project_id>/`, under Gobby home. #24801 ruling 1, verbatim: "I really only want the persistent sessions like Assistant to be able to see prior sessions, and only to recover from a crash or learn from mistakes made in prior versions. I don't want to pollute developer state on EpicDevPod for example." Orchestrator Q2 ruling: every session writes only its own folder. Persistent seats may also read their predecessors' folders, for crash recovery and learning from mistakes only. A predecessor folder sits under the caller's own `<definition>/`, belongs to a lineage that started before the caller's session (lower sequence number), and is bound as `_agent_workspace_dir` to no session in `LIVE_SESSION_STATUSES`. A live sibling of the same definition is therefore not a predecessor, and a resumed predecessor stops being one while it is live. Every other seat reads only its own session folder and its team's epic folder, enforced by a new read-path rule. A persistent seat's clear successor inherits its predecessor's folder; other seats get a fresh folder. The persistent set (Assistant, Orchestrator, Archivist, Release Manager, Triage) is a definition-level `persistent_workspace: true` flag, not a name list. Rulings 1.2 A, 1.3 A and 1.4 A: running an opaque helper script still needs a task claim; node sync uses Git bundles over the node channel with the hub as authority, built when the first seat runs on a node; everything is kept indefinitely.

## Existing work and dependency integration
`kind: framing`

Every phase heading carries `(depends: #24797)`, so expansion gives each leaf a `blocked-by` edge on #24797. Nothing here edits `.gobby/plans/runbook-devteam-redesign.md` or #24797's worktree. Where this plan extends a file #24797 creates, the extending section names that redesign section as its source.

Ownership boundaries:
- The rename of `lane-manager` and `inbox-manager` in the seat catalogues under `src/gobby/install/shared/workflows/rules/roles/` belongs to #24797. The Plan Writer reported the gap to the Orchestrator on 2026-10-22 as found work. This plan adds only its own names (`docs-reviewer`, `research-reviewer`) and the team-manager spawn exemption.
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
  - Not yet created; #24797's 3.1-3.5 create them, and this plan relies only on those sections' stated contracts: `src/gobby/agents/devteam_lifecycle.py`, `src/gobby/agents/devteam_review.py`, `src/gobby/agents/devteam_teardown.py`, `src/gobby/install/shared/workflows/pipelines/devteam.yaml`, `src/gobby/install/shared/workflows/agents/dev-manager.yaml`, `src/gobby/install/shared/workflows/agents/test-writer.yaml`, `src/gobby/install/shared/workflows/agents/triage.yaml` and `src/gobby/install/shared/skills/gobby/references/agents/procedures/dev-manager.md`.
- **`path::*` for symbols that exist only on the branch.** `src/gobby/config/tasks.py` and `src/gobby/storage/tasks/_queries.py` exist on main, but the target symbols live only on the branch (main still has a duplicate `_external_blocker_exists_sql` in `_aggregates.py`, which the branch consolidates). Before expansion, after #24797 lands, the Plan Writer refreshes these Targets to symbol scope and reruns consumer sweeps.
- **Corrected module names.** The per-epic report's draft names `review_verdicts.py`, `state_queries.py` and `_ancestor_gate.py`. They are `step_records.py`, `_queries.py` and `_blocking.py`.
- **Files near the ceiling.** Line counts on main: `src/gobby/sessions/clear_continuation.py` 864, `src/gobby/mcp_proxy/tools/sessions/_terminal.py` 816, `src/gobby/runner_init/orchestration.py` 811, `src/gobby/hooks/event_handlers/_agent.py` 787. This plan avoids `clear_continuation.py`: clear inheritance uses `src/gobby/hooks/event_handlers/_session_start/materialize.py::_bind_clear_successor` (568 lines). New logic goes in new modules, and the large files get only call sites. Production files stay below 1,000 lines.

Use isolated tests: `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest <focused targets>`. Never run the full suite. Each leaf runs `uv run ruff check`, `uv run ruff format --check` and `uv run mypy src/` on its changes. Template changes are confirmed in the installed registry rows after sync before any rule or definition is declared live.

## P7: Generic team contract (depends: #24797)
`kind: framing`

A team is a runbook pipeline bound to one task through the redesign 3.1 team record. Roles are defined by step grants, not names:
- **Manager:** holds the bound task's claim, dispatches and lands, and writes the `closing` record. It is marked by the definition variable `team_manager: true`.
- **Makers:** write `test` and/or `implementation` records for the work they produce.
- **Independent reviewers:** write only `review` records and never edit. 2.3's `_require_independent_reviewer` applies to any definition holding the review grant. The team record binds one seat per reviewer definition the team's routing needs (Decision 25).

Project policy (`test_categories`, `review_categories`) and task type decide which records a close needs (3cb34179). Landing (2.4) consumes the reviewer's record, and close checks the records' approval. Neither names a role.

Places that would otherwise be dev-specific, and their owners:
1. `src/gobby/workflows/engine/seat_transitions.py::advance_seat` and its candidate-binding check key reviewer and maker steps on the literal names `code-reviewer` and `developer`. Owner: 7.1.
2. `src/gobby/workflows/step_binding.py::release_completed_review` keys verdict recovery on `code-reviewer`. Owner: 7.1.
3. `src/gobby/mcp_proxy/tools/tasks/_session.py::link_task_to_session` requires a running `code-reviewer` seat for the `reviewing` link. Owner: 7.1.
4. The `devteam` runbook's reviewer seat is fixed to `code-reviewer`; it becomes the `reviewer_agent` input and the team's first reviewer binding, and epic mode adds a binding per routed reviewer definition. Owners: 7.2 (admission), 7.3 (pipeline) and 8.2 (epic reviewer spawns).
5. The seat-spawn exemption names `lane-manager`; it becomes `team_manager`. Owner: 8.2.
6. Epic dispatch spawns the team's implementation-grant definition recorded in the team record, not a hardcoded `developer`. `implementation_domain` only routes developer skills and primaries. Owners: 8.1 and 8.2.
7. Workspace folders key on `_agent_type` and `persistent_workspace`, with no role list. Owners: 9.2 and 9.3.

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
- `src/gobby/agents/devteam_lifecycle.py`
- `tests/agents/test_review_routing.py`

**Research context:** On the branch, `TaskClosePolicyConfig.review_categories` defaults to `["code", "docs"]`, and its `validate_categories` checks against `src/gobby/storage/tasks/_models.py::VALID_CATEGORIES`. `research` is valid. Change the default to `["code", "docs", "research"]`. Because `.gobby/project.json` enforces step records with default categories, Gobby research tasks will need an approved review record. `required_task_steps` in `step_records.py` reads the policy, so no close code changes.

The `devteam` runbook gains a required `reviewer_agent` input. `team_launch_inputs.py` passes it through, and `runbook_seats.py` validates it at admission with one review-grant check, which 8.2's epic reviewer spawns reuse: the named definition must be enabled, installed and grant `review`. Admission records it as the first reviewer binding in the 3.1 team record (`devteam_lifecycle.py`, created by redesign 3.1). The bindings map each reviewer definition to its live seat run. A single-task team has exactly one binding and refuses a second definition. A replacement seat must have its binding's definition.

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

`devteam.yaml` (a #24797 3.5 file) takes the reviewer seat's agent from the `reviewer_agent` input. Add both names to the inline seat catalogues so the seat rules cover them. The `lane-manager` rename gap in those catalogues stays with #24797.

**Acceptance:**
- 7.3.1 - Both definitions parse, sync, grant only `review`, block every edit tool, and load their procedure. test: `tests/agents/test_category_reviewer_definitions.py::test_reviewer_definitions_are_read_only_review_writers`.
- 7.3.2 - The Orchestrator and Dev Manager procedures carry the identical routing table and the docs-about-code rule. test: `tests/agents/test_category_reviewer_definitions.py::test_routing_table_matches_procedures`.
- 7.3.3 - The devteam runbook launches the reviewer named by `reviewer_agent`, and the seat rules match both new reviewers. test: `tests/agents/test_category_reviewer_definitions.py::test_runbook_and_seat_rules_cover_new_reviewers`.

**Verification:** Planned: template parsing, registry sync and runbook dry-run tests; then inspect the installed definition and rule rows after sync.

## P8: Epic-bound DevTeams (depends: #24797)
`kind: framing`

Decisions 15-24 apply. The lifecycle runs in order:
1. Admit the team on the epic.
2. Write tests for every leaf in dependency order.
3. Dispatch leaves.
4. Merge finished sub-branches.
5. Review each leaf.
6. Return rejected leaves to developers.
7. Merge 0.5.0.
8. Link and review the epic tip.
9. Land.
10. Batch-close the leaves, close the epic, and tear down.

Per-leaf state is derived from tasks, records and Git, not stored in the team:
- in flight: a team seat holds the leaf's claim;
- finished: `approved_task_step(leaf, records, "implementation")` returns a record (Decision 20);
- merged: the leaf has no linked commits, or its tip is an ancestor of epic main;
- reviewed: `approved_task_step(leaf, records, "review")` returns a record.

A leaf is no-work when its finishing record carries a disposition and it has no linked commits. The leaf set is every open non-epic descendant at any depth. Epic-type descendants are containers that close through the cascade (Decision 24).

### 8.1 Bind a team to an epic and scope its leaf claims (depends: 7.2)
`kind: deliverable`
`[category: code]`

Targets:
- `src/gobby/agents/devteam_lifecycle.py`
- `src/gobby/agents/team_launch_inputs.py::*` — scope-reason: derive epic mode and the epic branch and worktree names from the bound task
- `src/gobby/agents/isolation_models.py::*` — scope-reason: carry an explicit worktree directory name in the spawn config
- `src/gobby/agents/isolation_worktree.py::*` — scope-reason: honor the explicit worktree directory name
- `src/gobby/agents/runbook_seats.py::*` — scope-reason: refuse overlapping epic and descendant team admissions
- `src/gobby/mcp_proxy/tools/tasks/_lifecycle_claim.py::*` — scope-reason: refuse descendant claims by sessions outside the active epic team
- `tests/agents/test_devteam_epic_mode.py`

**Research context:** Epic mode is derived from the bound task: an epic with open descendants. The 3.1 team record (`devteam_lifecycle.py`, new under redesign 3.1) gains the mode, its seat runs keyed by role and domain, and the maker definition each role uses. The leaf set is every open non-epic descendant at any depth, read live; nothing snapshots it, so found-work leaves created under the epic join automatically. Admission refuses an epic any of whose non-epic descendants has open children, with a structured refusal naming them, because such a parent is neither a container the cascade closes nor a leaf a developer works; the Orchestrator fixes the topology and places found-work leaves under an epic-type descendant. Redesign 3.3 queues teardown from a committed close; the team lookup it uses here matches only the team's bound task, so a descendant leaf close or a cascaded container close never tears the team down.

`isolation_models.py::generate_branch_name` already lets an explicit `branch_name` win. But `isolation_worktree.py::WorktreeIsolationHandler._generate_worktree_path` derives the directory from the branch with `/` replaced by `-`, giving `epic-<seq>-main`, not ruling 5's `wt-epic-<seq>`. Add an explicit directory name to the spawn config. Epic admission passes branch `epic/<seq>/main` and directory `wt-epic-<seq>`.

Admission refuses an epic any of whose descendants has an active team or a claim by a session outside the admitting team. It also refuses a task whose ancestor epic has an active team. `claim_task` on a descendant of an active epic team's epic succeeds only for a seat of that team; everyone else gets a structured refusal naming the team.

The Dev Manager claims the epic at admission, as Decision 16 requires. Report row 16 is the source of the overlap rules. Its row 8 (skills only for claimed tasks) needs no change, because the working seat now claims its leaf and `claimed_task_extra_skills.py::build_claimed_task_extra_skill_state` already loads that leaf's skills.

**Acceptance:**
- 8.1.1 - Admitting an epic with open descendants records epic mode and creates branch `epic/<seq>/main` in worktree `wt-epic-<seq>`; a leaf admission stays single-task. test: `tests/agents/test_devteam_epic_mode.py::test_epic_admission_names_and_mode`.
- 8.1.2 - Overlapping epic or descendant admissions are refused. test: `tests/agents/test_devteam_epic_mode.py::test_overlapping_admissions_refused`.
- 8.1.3 - Inside an active epic team, team seats can claim descendants and other sessions cannot. test: `tests/agents/test_devteam_epic_mode.py::test_descendant_claims_scoped_to_team`.
- 8.1.4 - Admission refuses an epic with a non-epic descendant that has open children; closing a descendant leaf or a cascaded phase epic queues no teardown, and closing the bound epic does. test: `tests/agents/test_devteam_epic_mode.py::test_topology_and_bound_close_teardown`.

**Verification:** Planned: focused isolated tests, plus `tests/agents/test_devteam_admission.py` (redesign 3.1) rerun.

### 8.2 Dev Manager dispatches leaf developers and merges sub-worktrees (depends: 8.1, 7.3)
`kind: deliverable`
`[category: code]`

Targets:
- `src/gobby/mcp_proxy/tools/spawn_agent/_factory.py::*` — scope-reason: derive and bind leaf sub-worktrees for a team manager's leaf spawns
- `src/gobby/agents/devteam_lifecycle.py`
- `src/gobby/install/shared/workflows/agents/dev-manager.yaml`
- `src/gobby/install/shared/workflows/rules/roles/seat-spawn-policy.yaml::*` — scope-reason: let team managers spawn their own team's seats
- `src/gobby/install/shared/workflows/rules/roles/inject-seat-common.yaml::*` — scope-reason: state the team-manager spawn exception in seat guidance
- `tests/workflows/test_seat_rules.py::*` — scope-reason: update the seat guidance spawn assertion
- `tests/agents/test_devteam_epic_dispatch.py`

**Research context:** Ruling 1 grants the Dev Manager spawn authority. `seat-spawn-policy.yaml` rule `seat-no-spawn` gets an exception: a session whose definition sets the workflow variable `team_manager: true` may call `spawn_agent`. Seats cannot set that variable themselves, because native `set_variable` is refused outside `idle_declaration.py::NATIVE_VARIABLE_ROLES`. `dev-manager.yaml` (a #24797 3.5 file) sets it. Line 26 of `inject-seat-common.yaml` becomes "No spawns except Assistant, Orchestrator, automated close review, and a team manager spawning its own team's seats." `tests/workflows/test_seat_rules.py` asserts the old sentence and changes with it.

The factory enforces scope, extending 3.1's team-identity check at every seat spawn. A team manager may spawn only:
- a maker: for an open descendant of its bound epic, with the team record's maker definition for that role, for a leaf that has no in-flight team seat;
- a reviewer: a definition passing 7.2's review-grant check, which adds a binding when the definition has none and otherwise replaces only that binding's ended seat. A spawn for a definition whose bound seat is live is refused.

Bindings are keyed by definition, so a replacement always has its binding's definition. Reviewer seats run in the epic worktree and stay until the epic completes, like primaries.

A primary spawn runs in the epic worktree. A sub-worktree spawn derives branch `epic/<seq>/<leaf>` and directory `wt-epic-<seq>-<leaf>` from base `epic/<seq>/main` (8.1's directory-name support), and binds the run to the team record.

A sub-worktree developer ends with `end_agent_run` after recording its implementation. Merging uses existing tools, so no merge tool is added: the Dev Manager runs `git merge --no-ff` in the clean epic worktree under its epic claim, links the merge commit to the epic, and deletes the sub-worktree through the existing deletion path with `WorktreeDeletionRequest.merged_into` = `epic/<seq>/main` (`src/gobby/worktrees/deletion.py`), which checks ancestry against that ref. Deletion's dirty, live and unmerged refusals stay.

**Acceptance:**
- 8.2.1 - A team manager spawns a primary in the epic worktree and a sub-worktree developer on the derived leaf branch and directory; spawns outside its epic, with a non-team definition, or for an in-flight leaf are refused. test: `tests/agents/test_devteam_epic_dispatch.py::test_manager_leaf_spawns_scoped`.
- 8.2.2 - Seats without `team_manager` stay blocked from spawning, and the seat guidance names the exception. test: `tests/workflows/test_seat_rules.py::test_team_manager_spawn_exception`.
- 8.2.3 - After a no-ff merge into epic main, the sub-worktree and branch delete with `merged_into` epic main; an unmerged sub-branch is refused. test: `tests/agents/test_devteam_epic_dispatch.py::test_merged_leaf_tree_cleanup`.
- 8.2.4 - In an epic team with code, non-code docs and research leaves, the manager spawns `code-reviewer`, `docs-reviewer` and `research-reviewer` seats, each recorded as a binding, and respawning an ended reviewer fills its own definition's binding. A definition without the review grant and a second live seat for a bound definition are refused. test: `tests/agents/test_devteam_epic_dispatch.py::test_routed_reviewer_bindings`.

**Verification:** Planned: focused spawn-contract tests with fake provider launches and temporary Git repositories, plus `tests/workflows/test_seat_rules.py` rerun.

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

### 8.4 Epic review loop without a fixer and epic escalation (depends: 8.2)
`kind: deliverable`
`[category: code]`

Targets:
- `src/gobby/agents/devteam_review.py`
- `src/gobby/agents/devteam_lifecycle.py`
- `tests/agents/test_devteam_epic_review.py`

**Research context:** Redesign 3.2's `devteam_review.py` (new under 3.2) runs the review loop from 2.3's review-step record as the durable trigger. In epic mode:
- No reviewer-to-fixer transition and no fresh Opus reviewer (Decision 21).
- Each rejection returns the leaf to a developer under the dispatch rule, and the bound seat of the definition that wrote the leaf's earlier review records reviews the next round.
- An epic-tip rejection is not escalated before its third round. The loop returns it to the Dev Manager, which assigns each finding to an owning leaf or requests a found-work leaf from the Orchestrator (Decision 21). Once every assigned leaf is finished, merged and re-reviewed, the loop accepts the next epic-tip round on the new linked tip. Leaves whose tips did not move keep their records, and the epic's rejection count carries on.
- When it observes a third-rejection escalation of a leaf in the team, or of the epic itself, it escalates the parent epic idempotently through the same claim-releasing escalation 2.3 uses, with a reason naming the exhausted task (Decision 22).
- The team record marks the team held. While held, a team-manager spawn (8.2) and a new review round are refused.
- After Triage's audited reset (`src/gobby/storage/tasks/_de_escalation.py::de_escalate_task`, which starts a new epoch on the exhausted task) and the epic's de-escalation, the Dev Manager reclaims the epic and the hold lifts.
- An externally blocked leaf holds the epic the same way, without escalation (Decision 23); there is no detach path.

Resume reads this state from the record and never recounts transcripts. `step_records.py` stays team-agnostic.

**Acceptance:**
- 8.4.1 - In epic mode, rejections go back to developers and the same reviewer reviews every round; no seat changes role. test: `tests/agents/test_devteam_epic_review.py::test_reviewer_never_fixes_in_epic_mode`.
- 8.4.2 - A leaf's or the epic tip's third rejection escalates the epic once, holds the team, and lifts after the audited reset and re-claim. test: `tests/agents/test_devteam_epic_review.py::test_third_rejection_holds_epic`.
- 8.4.3 - An externally blocked leaf holds landing, and no leaf is detached or reverted. test: `tests/agents/test_devteam_epic_review.py::test_blocked_leaf_holds_epic`.
- 8.4.4 - After every leaf is approved, an epic-tip rejection returns to an owning leaf. Only that leaf's records go stale and are rewritten, and the next epic-tip round approves the new tip as round two, with the earlier epic rejection still counted. test: `tests/agents/test_devteam_epic_review.py::test_epic_tip_rejection_repairs_through_leaf`.

**Verification:** Planned: state-transition tests with fake reviewer records, duplicate-verdict replay and restart recovery.

### 8.5 Epic-mode procedures and runbook binding (depends: 8.4, 8.3)
`kind: deliverable`
`[category: config]`

Targets:
- `src/gobby/install/shared/workflows/pipelines/devteam.yaml`
- `src/gobby/install/shared/skills/gobby/references/agents/procedures/dev-manager.md`
- `src/gobby/install/shared/skills/gobby/references/agents/procedures/orchestrator.md::*` — scope-reason: epic admission reviewer choice, topology refusal handling and found-work leaf placement
- `src/gobby/install/shared/workflows/agents/test-writer.yaml`
- `src/gobby/install/shared/workflows/agents/developer.yaml::*` — scope-reason: leaf claims, implementation records at the tip, merge-only branch updates and the conflict-merge job
- `src/gobby/install/shared/skills/gobby/references/agents/procedures/code-reviewer.md::*` — scope-reason: per-leaf and epic-tip review records in epic mode
- `src/gobby/install/shared/skills/gobby/references/agents/procedures/docs-reviewer.md`
- `src/gobby/install/shared/skills/gobby/references/agents/procedures/research-reviewer.md`
- `docs/guides/runbooks.md`
- `tests/workflows/test_devteam_epic_runbook.py`

**Research context:** `devteam.yaml` (redesign 3.5) accepts an epic binding; mode stays derived (8.1). `dev-manager.md` (3.5) gains the epic procedure:
- the test phase: the Test Writer claims each leaf in dependency order in the epic worktree, commits tests tagged `[gobby-#<leaf>] test:`, links them and releases;
- the dispatch rule (Decision 17), merges (Decision 19) and unlock order (Decision 20);
- per-leaf reviewer routing with the 7.3 table at each leaf's first review, spawning any routed reviewer not yet bound (Decision 25);
- the no-work path: a leaf found moot before any commit links to it gets each record its category requires written approved with that disposition and no commit; its routed reviewer reviews the disposition; nothing merges;
- review at the end, and the epic-tip rejection repair through owning or found-work leaves (Decision 21);
- the hold rules (Decisions 22-23);
- the landing order (Decision 24): merge 0.5.0, link the tip to the epic, the epic-tip review, land under 2.4, then for each leaf bottom-up claim it, write its closing record and close it (no-work leaves first in each container, with their disposition as the close reason), letting the cascade close phase epics; then close the epic.

`orchestrator.md` gains the epic admission rules: pass the precedence winner over the epic's open leaves as `reviewer_agent`; fix a topology that admission refuses for a non-epic parent with open children; and place requested found-work leaves under an epic-type descendant.

`developer.yaml` adds: claim the dispatched leaf; commit only that leaf's work, tagged and linked to it; write the implementation record at the leaf tip and release; never rebase or squash after the first link. A conflict-merge job merges epic main into the sub-branch. `test-writer.yaml` (3.5) adds the per-leaf commit series.

All three reviewer procedures add the epic rule: review only; one record per leaf at its tip, or with the disposition and no commit for a no-work leaf; and the epic-tip record only when every leaf has a current approval and the tip is linked. The reviewer's procedure text never asks it to fix (ruling 3). Report row 7 stays procedural: a leaf's test commit must be an ancestor of its implementation commits.

**Granularity:** These files jointly define one epic-mode launch and procedure contract that activates together. Splitting them would let a partially updated seat set launch.

**Acceptance:**
- 8.5.1 - The procedures state the phase order, dispatch rule, merge-only updates, unlock order, per-leaf routing and reviewer spawns, the no-work path, the epic-tip repair, hold rules, landing order, nested close order and the Orchestrator's epic admission rules, with no detach or reviewer-fix path. test: `tests/workflows/test_devteam_epic_runbook.py::test_epic_procedures`.
- 8.5.2 - The devteam runbook dry-runs bound to an epic and to a leaf with the same seat catalogue. test: `tests/workflows/test_devteam_epic_runbook.py::test_runbook_binds_epic_or_leaf`.

**Verification:** Planned: template parsing, registry sync and runbook dry-run tests; installed rows inspected after sync.

### 8.6 Pilot one epic team (depends: 8.5, 9.4)
`kind: deliverable`
`[category: test]`

Targets:
- `tests/e2e/test_devteam_epic_lifecycle.py`
- `.gobby/plans/research/devteam-epic-pilot-results.md`

**Research context:** This proves Decisions 15-24 end to end in isolation, then once live. The isolated e2e covers a six-leaf epic with one nested phase epic:
- inside the phase epic, two parallel code leaves in different domains feed one dependent code leaf;
- directly under the root, a no-work leaf blocks a non-code docs leaf, and a research leaf stands alone.

It runs the test phase, a primary and two sub-worktree developers, no-ff merges and sub-tree cleanup, the intra-epic unlock including the no-work blocker, and per-leaf review by all three routed reviewers with one rejection returned to a developer. It then runs the 0.5.0 merge, epic-tip review, landing, batch leaf close with the phase epic closed by the cascade, root epic close, one teardown and the epic workspace folder's final commit.

A second case drives a leaf to its third rejection and proves that the epic holds and recovers after the audited reset. A third case rejects the epic tip after every leaf is approved, repairs it through one leaf, and lands on the second epic-tip round. The live pilot runs one real epic under Release Manager authority and records results in the pilot file. The file also carries the Orchestrator-owned "one task per team" policy wording for the Orchestrator to apply.

**Acceptance:**
- 8.6.1 - The six-leaf nested epic completes from admission to teardown with parallel sub-worktrees, a no-work blocker, three routed reviewers, one rejection, one landing, closes in leaf, phase-epic and root order, and exactly one teardown. test: `tests/e2e/test_devteam_epic_lifecycle.py::test_complete_epic_team`.
- 8.6.2 - A third leaf rejection holds the epic until the audited reset, and then the epic lands. test: `tests/e2e/test_devteam_epic_lifecycle.py::test_escalated_leaf_holds_then_lands`.
- 8.6.3 - An epic-tip rejection after every leaf is approved is repaired through a leaf, and the epic lands on the second epic-tip round. test: `tests/e2e/test_devteam_epic_lifecycle.py::test_epic_tip_rejection_repaired_then_lands`.
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
- `src/gobby/agents/devteam_lifecycle.py`
- `src/gobby/agents/devteam_teardown.py`
- `src/gobby/agents/agent_workspaces.py`
- `tests/agents/test_agent_workspace_epics.py`

**Research context:** For epic-mode teams only, the team record sets `_agent_workspace_epic_dir` to `epics/epic-<seq>/` on every seat run, sub-worktree developers included, and clears it when a run ends. A resumed seat gets it back only if the team still names it. `agent_workspaces.py` writes the epic README with its convention: one file per topic, append with Edit. At the bound epic's close (a cascaded phase-epic close never triggers it, per Decision 24), redesign 3.3's teardown (`devteam_teardown.py`, new under 3.3) schedules the final commit with message `epic-<seq>` and clears the variable on any surviving seat. Nothing in the workspace is a close invariant.

**Acceptance:**
- 9.4.1 - Every seat of an epic team, sub-worktree developers included, can write the epic folder; sessions outside the team cannot. test: `tests/agents/test_agent_workspace_epics.py::test_epic_folder_scoped_to_team`.
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

## V2: Verification
`kind: verification`

These are completion gates for the implementation. Except for plan validation, none has run yet. Each runs after the leaves it names land, and each must hold before the epic closes.

- Plan validation must pass: `uv run gobby plans validate .gobby/plans/runbook-devteam-addendum.md -p /Users/josh/Projects/gobby`. This is the only planning-time check.
- Each leaf's focused tests, Ruff and mypy must pass on its final commit with the isolated test hub.
- After 7.3, 8.2, 8.5 and 9.3 land, the installed definition and rule rows must match the templates.
- The 8.6 isolated e2e must pass, and the live pilot results must be recorded in `.gobby/plans/research/devteam-epic-pilot-results.md`.
- After 9.4, a real session's workspace history must show commits from every trigger: timer, set_handoff, pre-compact, session end, clear binding and epic close.
