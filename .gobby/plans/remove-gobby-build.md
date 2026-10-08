Plan artifact: `.gobby/plans/remove-gobby-build.md`

# Remove gobby build and the stage subsystem

**Plan ID:** remove-gobby-build

## Context
`kind: framing`

Josh's decision ac21fdc2 (Telegram, 2026-10-02, through the Assistant), verbatim: "We need to remove Gobby build." Epic #23341, **Remove gobby build**, owns this plan under #22691, **Lane 3 - Runbooks**. Its preconditions are closed: #23337, **Plan pane labels as the only seat name and remove automatic session titles**, and #22895, **Plan runbooks as existing pipelines with placed agent launch**. Runbooks are ordinary tagged pipelines that launch standing seats through `spawn_agent`; nothing in them enters `gobby build`, the dispatcher, or task stages.

Live state on 0.5.0 (2026-10-08):

- `gobby build` has three entry points that share `src/gobby/build/service.py`: the CLI command (`src/gobby/cli/build.py`), the MCP tools `build_task`, `build_stop`, `build_resume`, `build_clean` and `build_restart` (`src/gobby/mcp_proxy/tools/build.py`), and the HTTP routes under `/api/build` (`src/gobby/servers/routes/build.py`).
- Stage-manifest dispatch lives in `src/gobby/dispatch/`. The system automation loop (`src/gobby/system_automation.py`) drives it, and the cron executor still carries a dead `dispatcher` action. The `gobby:dispatcher` cron job is already listed in `REMOVED_AUTOMATION_JOB_NAMES` (`src/gobby/storage/cron.py`).
- Task stages exist only for build-entered tasks. `gobby build` materializes a manifest in `task_stage_states` from the stage registry (`src/gobby/install/shared/registry/stages.yaml`, synced to `task_stages_registry`). Expansion copies a manifest to children only when the parent already has stage rows (`derive_child_manifest_specs` in `src/gobby/storage/tasks/_stage_manifest.py`, called from `src/gobby/tasks/expansion/_apply.py`). Once build is gone, nothing writes a stage row.
- Build profiles (`src/gobby/install/shared/registry/build_profiles.yaml`, table `build_profiles`) shape only new build lifecycles.
- The spawn cap `max_active_agents` is read through `load_build_config` (`src/gobby/config/build.py`, file-based `build.yaml`, code default 20 in `src/gobby/dispatch/constants.py`) by `agent_slot_cap_refusal` in `src/gobby/mcp_proxy/tools/spawn_agent/_spawn_guards.py`.

End state: no `gobby build` command, tool or route; no dispatcher; no stage registry, stage manifest, stage verdict or review-transition tool, stage CLI, stage route or web Stages view; no build profiles; no definition that only stage dispatch spawned; and none of their tables or task columns (`allow_automation`, `unattended`, `checkout_mode`, `dispatch_failure_count`). The spawn cap survives as one daemon config field. Task-bound spawn serialization (`task_dispatch_mutex` and its runtime lease), task artifacts, `assigned_agent`, `implementation_domain`, `additional_skills`, `merge_in_progress` and the stored `state_bucket` survive, because spawn, worktree isolation, merge application, the plan CLI, manual expansion and the task queries read them.

Known break this plan removes (accepted by Josh, button adv-name-A, 2026-10-02): once #23339 landed, `plan-adversary` became the planning runbook's Adversary seat, but the seeded `planning` stage row still names it as `reviewer_agent`. Removing the stage registry removes that row.

Downstream: deferred section D1, **Batch spawn retirement**, of `.gobby/plans/spawn-network-override.md` waits on this plan's merge-agent retirement in 3.1 (Decision Record item 2). 1.1 moves `dispatch_batch` into its own module, so D1 deletes that module whole.

This plan supersedes `.gobby/plans/merge-orchestrator-removal.md`, which was never registered. The commit that first records this plan moves that file to `.gobby/plans/completed/` with `git mv`.

Historical records stay untouched: `.gobby/plans/completed/*`, `docs/plans/*`, `docs/research/*`, `docs/spikes/*`, `docs/reviews/*`, `docs/evidence/*`, `docs/archive/*`, `docs/audits/*`, the dated audit `docs/design/task-authority-audit.md`, the architecture records `docs/architecture/gobby-v1.0.0-roadmap.md`, `docs/architecture/repository-history-scrub.md` and `docs/architecture/interactive-persona-scope.md`, the README section "What shipped in 0.4.x", and the existing migration files under `crates/gcore/assets/schema/migrations/`. The capability audits under `docs/reference-audit/` are not history: the reference-library contract test checks them against the skill catalog and the source symbols they cite, so each leaf that deletes a cited file or symbol edits the audit in the same change.

## Decision Record
`kind: framing`

Orchestrator rulings (gobby#14972) on the Writer's recommendations. They are recorded here for Josh's review, and his approval of this plan settles them. Items 1 and 6 date from 2026-10-06; items 2 to 5 date from 2026-10-08 and replace the earlier wording on the same questions.

1. **R1, boundary (2026-10-06).** The stage subsystem goes with the build. That covers the registry, `task_stage_states`, stage verdict and review-transition tools and every stage read surface. Stage history is dropped with its tables and is not archived. Reason: 0.5.0 has no compatibility to keep, and read surfaces would serve a state machine nothing can enter. Rejected: (i) keep stage storage and its read surfaces, which leaves about sixty reader files serving rows nothing writes; (ii-b) export the rows to JSONL before the drop, which is new mechanism with no consumer.
2. **Supersession (2026-10-08 09:34 CT).** This plan supersedes `merge-orchestrator-removal` (#23460, **Plan the merge-orchestrator removal with no replacement merge orchestration**) in full. That plan was never registered, so it is archived by file move, not by plan archival. Its P1, **Agentless PR and merge stages**, is moot because the stages go here. Its P2 obligations land in 1.2, 2.2 and 3.1. 1.2 deletes the spawn guard's parent-orchestrator exemption. 2.2 deletes the definition and the merge-landscape descriptions, with the split they need. 3.1 retires `merge-worker` with its references, the merge-campaigns guidance and the sweep. Each leaf owns its tests. Its dispatch prompt builder key needs no edit because 2.5 deletes the dispatch package, and its dispatch-mutex-release join is moot because 3.5 deletes that module. Deferred section D1 of `spawn-network-override` waits on 3.1.
3. **Merge agents (2026-10-08 09:46 CT, option A).** `merge-orchestrator` retires in 2.2 and `merge-worker` in 3.1, and the `record_merge_result` and `record_pr_verdict` tools retire with the stage tools in 3.5. Both tools exist to move stage state: `record_pr_verdict` moves the `pr` stage or escalates, and `record_merge_result` completes the `merge` stage. Its other two effects need no replacement: terminal cleanup already releases the dispatch mutex at run end, and its build-artifact cleanup goes with the build package. Operators and seats keep landing a workspace with `merge_worktree` or `merge_clone`. Rejected (option B): keep `merge-worker` as a standalone agent that reports its merge SHA through `end_agent_run`. #23460's ruling 5 does not bind this plan.
4. **Stage-only definitions (2026-10-08 10:00 CT, option A, the last scope ruling).** 3.2 deletes `planner` and all four `*-old` plan definitions: `plan-adversary-old`, `plan-adversary-taskless-old`, `plan-enhancer-old` and `plan-enhancer-taskless-old`. 3.3 deletes `analyst`, `architect`, `product-manager`, `expansion-qa`, `qa-reviewer`, `doc-reviewer` and `trajectory-monitor`. The deprecated `qa-dev` definition, whose only verdict is a development-stage approval, goes with them. 3.4 strips the stage-tool text from the kept `epic-reviewer`, `tech-writer`, `plan-writer`, `plan-enhancer`, `plan-adversary` and `task-close-reviewer` (the 2026-10-08 inventory found no stage-tool text left in `researcher` or `developer`, and found some in `task-close-reviewer`). The `ideate`, `architecture` and `prd` skills stay and lose their stage wording. Rejected (option B): strip the stage text from the eight definitions and keep them. Definitions Josh can hand-spawn from FleetView today that this plan removes: `analyst`, `architect`, `product-manager`, `planner`, `expansion-qa`, `qa-reviewer`, `doc-reviewer`, `trajectory-monitor`, `merge-orchestrator`, `merge-worker`, `plan-adversary-old`, `plan-adversary-taskless-old`, `plan-enhancer-old` and `plan-enhancer-taskless-old`.
5. **Schema drop is the final deliverable (2026-10-08).** Per AGENTS.md rules 7 and 10, 6.1 is one gcore migration with its baseline, seed and catalog carriers, one crate rebuild and one cutover. `task_dispatch_mutex`, `assigned_agent`, `implementation_domain` and `additional_skills` stay. Every MCP tool schema and response that exposes a dropped task column loses it, for example `allow_automation` and `checkout_mode` on `create_task`, `update_task` and `get_task`.
6. **Stage reconcile is out of scope (2026-10-06).** The stage rows on #19585, **M0: shared datastores bridge and two-machine acceptance**, and #19590, **P4: Validation**, are correct while #19600 is open, and the rows on closed #19587-#19589 are history. This plan resets nothing on them; the R1 drop removes every stage row.

Writer decisions, settled by Josh's approval of this plan:

7. **Spawn cap: move it.** `max_active_agents` becomes a top-level `DaemonConfig` field (default 20). Josh, verbatim (terminal 2026-10-03, through the Assistant): "if we have a config home for it and the work is minimal we can add it, I'm just tired of delays." The field is minimal: `CONFIG_REGISTRY` derives keys from `DaemonConfig`, and the regenerated `runtime_config_contract.json` rides the gcore rebuild that 6.1 already requires. The project-level `build.yaml` override retires with `gobby.config.build`. The local `build.yaml` sets 100, so the operator sets `max_active_agents` to 100 at activation. Rejected: removing the cap and `agent_slot_cap_refusal`. Josh's #23329 ruling only exempts runbooks from slot enforcement, and his later words keep the cap when a home exists.
8. **`dispatch_batch` moves out of the spawn factory.** The factory has 876 lines, so 1.1 moves `dispatch_batch` and its batch-only helpers into a new module that reads the new cap; nothing else in the tool changes. D1 then deletes that module whole. Rejected: keeping a reader named for a project, with an unused project argument, only to leave the factory untouched.
9. **R7, plan review evidence: keep the store, retire the spawned-round binding.** The Orchestrator ruled on 2026-10-08 at 12:42 CT that this plan settles the review-evidence round tools itself: each tool that has no bundled caller once the `*-taskless-old` definitions go is removed in the same phase-3 leaf, and each tool that keeps a caller is named here and stays. The caller check on 2026-10-08 found:
   - `bind_evidence_run` attaches a spawned reviewer run to prepared evidence. Its only callers are the dispatcher's planning spawn, which 2.5 deletes, and the taskless round in `references/plan/review.md`, which 3.2 rewrites. `plan-adversary` lists it only as a blocked tool. 3.2 removes the MCP tool, its audit entry, its blocked-tool entry and its tests.
   - `prepare_plan_review_round` stays because it opens every static-seat round: `bind_static_review_seats` takes the evidence ID it returns, and `references/plan/review.md` directs both calls. `bind_static_review_seats` and `get_plan_review_snapshot` stay for the same rounds, and `expire_plan_review_evidence` stays for them and for `references/plan/repair.md`. The manifest, coverage, changelog, finalize, repair and lesson-mint tools serve the same rounds.
   - The `plan_review_evidence` table and `PlanReviewEvidenceService` stay unchanged. The service's own `bind_evidence_run`, the `dispatch_run_id` column and the branches that read it remain as the run-bound mode's inert remainder, with only test callers. The stage-bound mode (`task_id` plus `stage`) loses its only automated caller, dispatch spawn, but stays callable through `prepare_plan_review_round`. The table's `stage` column is a plain string with no key into the stage tables, so the 6.1 drop leaves it valid. The two stage-tool writers, `record_plan_enhancement` and `record_pr_verdict`, go with the stage tools in 3.5.
   - `backfill_plan_review_lessons` also goes in 3.5. It re-runs the lesson mint that `approve_review` performs after a plan-stage approval, and with `approve_review` gone there is no first mint to repeat. `mint_plan_review_lessons` in `gobby.review_learning.recorders` then has only test callers and stays as part of the same inert remainder.

   Rejected: removing the run-bound and stage-bound modes. That rewrites the 999-line evidence service, its store and its table, which is review-evidence redesign rather than build removal.
10. **`state_bucket` stays and narrows.** The stored column feeds the task query sites. 6.1 rewrites `compute_task_state_bucket` to return `closed`, `escalated` or `ready`, drops the stage trigger and its refresh function with `task_stage_states`, narrows the check constraint and re-buckets every row. Rejected: dropping the column, which rewrites every query site for no behavioral gain.
11. **`dispatch_failure_count` goes.** Its writers are the dispatcher and the failed-run branch of `gobby.agents.task_recovery`, which runs only when `projected_task_state` returns `in_progress`, a state only a stage row produces. Every surviving task already takes the release path, so dropping the column changes no surviving behavior. Rejected: keeping a column with no writer.
12. **`task_lifecycle_events` stays.** The idle watchdog audit in `gobby.agents.watchdog.recovery` and `repair_closed_candidate` in `gobby.storage.tasks._lifecycle` write rows that are not stage transitions. Only the build-event helpers and the stage-transition writers go.
13. **Epic review without stages.** Once the stage tools go, no epic has an `epic_qa` stage, so `epic-reviewer` reviews open and closed epics on one path, the one its closed-epic branch already uses. It calls no stage tool and never closes the epic. Approve delivers the `## Epic Findings` block in the `end_agent_run` handoff. Request changes files each blocking finding as a remediation task under the epic; on a closed epic it files remediation tasks or calls `reopen_task`. Needs discussion calls `escalate_task` with a `needs_human:` reason. Every outcome ends with `end_agent_run`, and the `/gobby review` caller acts on the verdict. Spawn already claims an open, unclaimed epic for the reviewer, and the definition's claim step still refuses a spawn without a task. The default `require-step-completion` stop gate holds a spawned reviewer's turn until `end_agent_run`, so the reviewer terminal-verdict rule goes with its other two agents. Rejected: a new verdict tool or a stage-free review state, which adds mechanism for a verdict the handoff already carries.

## Constraints
`kind: framing`

- **Coordinated activation.** 1.1 and 2.4 change the runtime config contract carrier (2.5 regenerates it and expects no change), and 6.1 adds a schema migration. Both are embedded in gcore, so the installed coherent set (`gcode`, `gdaemon`, `ghook`) must be rebuilt and promoted through `promote_workspace_binary_set` before the next daemon start. The Orchestrator owns one rebuild, promotion and restart after 6.1 lands, with global notices and outside quiet hours (04:45-06:45 CT). No executor restarts the daemon or promotes binaries.
- **Green tree per leaf.** Each deliverable removes importers and registrations before or together with the modules they import, and owns every test its change breaks. Deletions follow the phase order: survivors leave build and dispatch (P1); the web UI, the build entry surfaces, the dispatcher drivers and then the build and dispatch packages go (P2); the merge agents, the stage-only definitions and then the stage tools, CLI and routes go (P3); the stage and column readers outside task storage go, then task storage's stage and column code (P4); guidance and docs follow (P5); the schema drop is last (P6).
- **Large files.** A deliverable that edits a hand-maintained production file at 850 lines or more names the split or move in its own body, or removes lines under a `delete-lines` proof with the file's base blob. A file edited under a proof takes no other edit in this plan: 1.2 holds the proof for `spawn_agent/_implementation.py`, 3.5 holds the proofs for `workflows/hooks.py` and `_expansion_registry.py`, and 4.1 holds the proof for `mcp_proxy/tools/tasks/_crud.py`. Files deleted whole carry `operation: delete` on every Target entry.
- **No backward compatibility.** Removed tools, routes, commands and columns get no shims, aliases or deprecation paths. Stored rows that only these surfaces read are dropped.
- **Historical records stay.** Completed plans, reviews, evidence and existing migrations are history and are not edited.
- **No build during execution.** Nobody starts `gobby build` while this plan executes. 2.2 deletes the agent the PR and merge stages spawn before 2.3 removes the entry surfaces.
- **Tests.** Executors run focused pytest with `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1`, never the full suite, and never heavy runs during quiet hours.

## P1: Survivors leave build and dispatch
`kind: framing`

The spawn path is the only surviving code that imports `gobby.config.build` and `gobby.dispatch`. This phase moves those imports out first, then drops the spawn behaviors that only dispatcher-started runs reach, so P2 and P3 can delete both packages and their agents without touching spawn.

### 1.1 Spawn cap and spawn mutex leave build and dispatch
`kind: deliverable`
`[category: code]`

Targets:
- `src/gobby/config/app.py::*` — scope-reason: add the top-level `max_active_agents` field to `DaemonConfig`
- `crates/gcore/assets/config/runtime_config_contract.json::*` — scope-reason: regenerated derived carrier for the new `DaemonConfig` key
- `tests/contracts/http/config_schema.json::*` — scope-reason: re-recorded public config schema response with the new key
- `tests/contracts/http/config_values.json::*` — scope-reason: re-recorded config values response with the new key's desired and active values
- `src/gobby/storage/tasks/_runtime_mutex.py::*` — scope-reason: becomes the home of `DISPATCH_TTL_SECONDS` beside `RuntimeDispatchMutex`
- `src/gobby/mcp_proxy/tools/spawn_agent/_spawn_guards.py::*` — scope-reason: read the cap from live daemon config and import the mutex and TTL from storage
- `src/gobby/mcp_proxy/tools/spawn_agent/_factory.py::*` — scope-reason: move the batch tool out and register it through the new module
- `src/gobby/mcp_proxy/tools/spawn_agent/_dispatch_batch.py`
- `src/gobby/mcp_proxy/tools/spawn_agent/_request.py::*` — scope-reason: receives the two string helpers that the factory and the batch module share
- `docs/guides/configuration.md`
- `docs/reference-audit/config.json::*` — scope-reason: the `build-defaults` anchor becomes `spawn-cap`
- `tests/mcp_proxy/tools/spawn_agent/test_spawn_guards.py::*` — scope-reason: cap and mutex source tests move to daemon config and storage
- `tests/agents/test_runbook_seats.py::*` — scope-reason: monkeypatch the renamed cap reader
- `tests/agents/watchdog/test_exited_terminal_cleanup.py::*` — scope-reason: patch the renamed cap reader
- `tests/mcp_proxy/tools/test_agents.py::*` — scope-reason: monkeypatch the renamed cap reader
- `tests/mcp_proxy/tools/test_parallel_dispatch.py::*` — scope-reason: patch the cap reader where the batch module imports it

Add `max_active_agents: int = Field(default=20, ge=1, description=...)` to `DaemonConfig` in `src/gobby/config/app.py` as a top-level scalar, next to `clones_dir` and `worktrees_dir`.
`CONFIG_REGISTRY` derives keys by walking `DaemonConfig` (`_walk_daemon_model` in `gobby.config.registry`), so no registry entry is hand-written.
Regenerate `crates/gcore/assets/config/runtime_config_contract.json` with the contract generator.
The generator command is `uv run python scripts/generate_runtime_config_contract.py`.
The HTTP contract corpus records full `/api/config/schema` and `/api/config/values` responses, and `test_case_replays_equal` compares them exactly. Re-record it against the isolated test hub with `GOBBY_RECORD_HTTP_CONTRACTS=1 DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/contracts/test_http_corpus.py -k test_record_http_contracts` (the corpus README's command), record twice to confirm the output is stable, and keep only the `config_schema.json` and `config_values.json` changes. Then run `tests/contracts/test_http_corpus.py` without the variable.

In `_spawn_guards.py`, replace `max_active_agents_for_project(project_path)` with a new `configured_max_active_agents() -> int`.
It reads the live config through `get_app_context()`: when the container's `config_runtime` is present and ready, it returns `config_runtime.capture().snapshot.active.max_active_agents`, the same capture pattern that `pipeline_config_resolver` uses in `gobby.app_context`.
It returns the field default (20) when the container or runtime is absent or not ready.
`agent_slot_cap_refusal` keeps its signature and counting and calls the new reader.
`reserve_agent_slot` and `slot_cap_response` keep their signatures, so their callers, the spawn implementation (967 lines) and the agents query tools, are unchanged.

Move `DISPATCH_TTL_SECONDS = 600` out of `gobby.dispatch.constants` into `src/gobby/storage/tasks/_runtime_mutex.py`.
`_spawn_guards.py` imports `DISPATCH_TTL_SECONDS`, `RuntimeDispatchMutex` and `DispatchMutexUnavailableError` from `gobby.storage.tasks._runtime_mutex`, not from `gobby.dispatch.constants` or `gobby.dispatch.mutex`.
The `gobby.dispatch` package itself is untouched here; 2.5 deletes it.

Split the spawn factory (876 lines): move the `dispatch_batch` tool out of `create_spawn_agent_registry` into the new module `src/gobby/mcp_proxy/tools/spawn_agent/_dispatch_batch.py`.
The new module exposes `register_dispatch_batch(registry, *, spawn_agent, ...)`, which takes the factory's `spawn_agent` closure and the services the batch body already reads, and registers the tool with its current name, description, parameters and result shape.
The batch-only helpers `_coalesce_string`, `_coalesce_bool`, `_coalesce_number` and `_suggestion_task_description` move with it.
`_non_empty_string` and `_first_string` serve both modules, so they move to `src/gobby/mcp_proxy/tools/spawn_agent/_request.py` as `non_empty_string` and `first_string`, and both modules import them from there.
`create_spawn_agent_registry` calls `register_dispatch_batch` once after it defines `spawn_agent`.
In the moved body, the concurrency bound becomes `configured_max_active_agents()`; the project-path lookup that existed only to feed the old per-project reader goes.

`docs/guides/configuration.md`: "## Build Defaults" becomes "## Spawn Cap", which documents `max_active_agents` as a top-level daemon config key (default 20, minimum 1) that caps active agents per project.
The section drops the two `build.yaml` files and the per-request overrides, which retire with `gobby.config.build`.
"### Packaging Diagnostics" becomes a `##` section, because it covers `uv build` and not the spawn cap.
In `docs/reference-audit/config.json`, the `build-defaults` anchor becomes `spawn-cap`, because the reference-library contract fails on an audited anchor the guide no longer has.

**Research context:**

- `agent_slot_cap_refusal` (`_spawn_guards.py`) counts active agents per project with `_count_active_agents` and refuses at the cap. `slot_cap_response` serves `can_spawn_agent` in the agents query tools, and `reserve_agent_slot` wraps every spawn in the spawn implementation. Both reach the cap through `agent_slot_cap_refusal`, and `dispatch_batch` calls `max_active_agents_for_project` directly for its semaphore bound.
- Test patches of `max_active_agents_for_project`: `tests/agents/test_runbook_seats.py`, `tests/agents/watchdog/test_exited_terminal_cleanup.py`, `tests/mcp_proxy/tools/spawn_agent/test_spawn_guards.py`, `tests/mcp_proxy/tools/test_agents.py` and `tests/mcp_proxy/tools/test_parallel_dispatch.py` (it patches the factory's import).
- Tests that drive `dispatch_batch` through the registry (`tests/mcp_proxy/tools/spawn_agent/test_factory.py`, `test_initial_variables.py`, `test_spawn_worktree_reference_resolution.py`) patch `_factory.get_project_context`, `_factory._load_agent_body` and `_factory.spawn_agent_impl`. Those names stay in the factory and the batch tool still calls the factory's `spawn_agent` closure, so these tests are unchanged. No test imports the moved string or batch helpers.
- The spawn mutex: `TaskSpawnLease.acquire` in `_spawn_guards.py` takes `RuntimeDispatchMutex` with `action_kind="spawn_agent"` and `ttl_seconds=DISPATCH_TTL_SECONDS` for every task-bound spawn. `src/gobby/dispatch/mutex.py` only re-exports `gobby.storage.tasks._runtime_mutex`.
- `gobby.config.build.load_build_config` has no other surviving reader. Its other readers are the `gobby.build` package and `gobby.dispatch.dispatcher`, which 2.5 deletes together with `gobby.config.build`.
- `tests/config/test_runtime_config_contract.py::test_checked_in_contract_matches_registry` checks that the checked-in contract matches the registry.
- Size: the spawn factory has 876 lines, so editing it needs a named split (Decision Record item 8). The spawn implementation has 967 lines and stays untouched because the reader keeps the guard signatures.
- Rejected: threading `daemon_config` through `reserve_agent_slot` and `slot_cap_response`. That would edit the spawn implementation and the agents query tools, both large, for no behavioral gain over the live-config capture.
- Planned checks: focused pytest on the five test Targets, `tests/mcp_proxy/tools/spawn_agent/test_factory.py`, `tests/mcp_proxy/tools/spawn_agent/test_initial_variables.py`, `tests/config/test_runtime_config_contract.py`, `tests/config/test_config_authority_audit.py` and `tests/skills/test_reference_library.py`; `uv run ruff check` and `uv run mypy` on the touched modules. Config corpus replay: `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/contracts/test_http_corpus.py -k "test_case_replays_equal and config"`.

**Acceptance:**

- 1.1.1 - `DaemonConfig` has a `max_active_agents` field with default 20 and minimum 1, and the regenerated contract lists the key. symbol: `DaemonConfig`. test: `tests/config/test_runtime_config_contract.py::test_checked_in_contract_matches_registry`.
- 1.1.2 - A spawn reaches the configured cap from live daemon config, not from `build.yaml`, and the field default applies without a runtime. symbol: `configured_max_active_agents`. test: `tests/mcp_proxy/tools/spawn_agent/test_spawn_guards.py::test_slot_cap_reads_daemon_config_max_active_agents`.
- 1.1.3 - `_spawn_guards.py` imports nothing from `gobby.config.build` or `gobby.dispatch`, and `DISPATCH_TTL_SECONDS` lives in `gobby.storage.tasks._runtime_mutex`. file: `src/gobby/mcp_proxy/tools/spawn_agent/_spawn_guards.py`. test: `tests/mcp_proxy/tools/spawn_agent/test_spawn_guards.py::test_spawn_guards_import_no_build_or_dispatch`.
- 1.1.4 - `dispatch_batch` is registered from its own module and bounds its concurrency with `configured_max_active_agents()`. symbol: `register_dispatch_batch`. test: `tests/mcp_proxy/tools/test_parallel_dispatch.py::test_dispatch_batch_respects_configured_cap`.
- 1.1.5 - The configuration guide documents `max_active_agents` under Spawn Cap, no longer describes the per-project build config file, and its audit pins the new anchor. behavior: "max_active_agents" in `docs/guides/configuration.md`. behavior: "build-defaults" absent from `docs/reference-audit/config.json`. test: `tests/skills/test_reference_library.py::test_reference_contract_3_2_1`.
- 1.1.6 - The recorded config schema and values responses carry `max_active_agents`, and the config corpus cases replay equal. test: `tests/contracts/test_http_corpus.py::test_case_replays_equal`.

### 1.2 Spawn drops its dispatch-only hooks (depends: 1.1) [category: code]
`kind: deliverable`

Targets:
- `src/gobby/mcp_proxy/tools/spawn_agent/_spawn_guards.py::*` — scope-reason: delete the parent-orchestrator exemption and the two parameters that only fed it
- `src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py::*` — operation: delete-lines — base-blob: 5e4905b39ade1458a086be85e98f83eb253b618c — lines: 196-198, 611-612, 684-685 — scope-reason: drop the plan-gate prompt append, the planning code-index arguments and the exemption arguments to the task admission guard
- `src/gobby/tasks/expansion/_plan_gate.py::*` — scope-reason: gate only plan-adversary and delete the symbol-repair channel
- `src/gobby/mcp_proxy/tools/spawn_agent/_code_index.py::*` — scope-reason: delete the planning-stage required mode
- `src/gobby/agents/spawn_executor_providers.py::*` — scope-reason: delete the required-mode failure branch
- `tests/mcp_proxy/tools/spawn_agent/test_mcp_proxy_tools_spawn_agent_dedup.py::*` — scope-reason: replace the parent-orchestrator exemption test with a duplicate-refusal test
- `tests/tasks/test_plan_gate.py::*` — scope-reason: the gate covers plan-adversary only and has no repair channel
- `tests/mcp_proxy/tools/spawn_agent/test_error_handling.py::*` — scope-reason: delete the planning code-index tests and call the narrowed preflight signature
- `tests/agents/test_spawn_executor.py::*` — scope-reason: delete the required-mode test and run the credential test in best-effort mode

Three spawn behaviors exist only for runs the dispatcher starts. This leaf removes all three, so later leaves can delete the dispatcher and its agents without touching the spawn implementation (967 lines). Every edit to that file is a pure deletion, and one proof covers all three.

Parent-orchestrator exemption: in `_spawn_guards.py`, delete `_is_parent_merge_orchestrator_run` and `_run_string_attr`, its only helper. The loop in `active_task_spawn_blocker` then returns the first active run, so a second active run on the same task is always reported. Drop the `requested_agent_name` and `parent_session_id` parameters from `active_task_spawn_blocker`, `active_task_response_if_blocked` and `admit_task_spawn`, which only passed them down to the exemption. The proof deletes the two matching keyword arguments from the `admit_task_spawn` call (lines 684-685).

Plan-gate repair channel: in `_plan_gate.py`, `PLANNING_AGENTS` becomes `frozenset({"plan-adversary"})`. Delete `PLAN_REPAIR_AGENTS`, the branch that returns a successful `prompt_append` payload when symbol validation fails, and `_symbol_repair_prompt`. A failed validation for `plan-adversary` keeps its structured refusal. The `_validate_plan_for_agent_spawn` docstring names only `plan-adversary` and drops the prompt-append return. The proof deletes the spawn implementation's `prompt_append` consumer (lines 196-198); the refusal check above it stays.

Planning code-index requirement: in `_code_index.py`, delete `_PLANNING_CODE_INDEX_AGENTS` and `_requires_planning_code_index`. `code_index_preflight_mode` drops its `agent_name` and `initial_variables` parameters and returns `"best_effort"` for a worktree or clone checkout outside the `docs` category, else `None`. The proof deletes the two matching keyword arguments from its call (lines 611-612). In `spawn_executor_providers.py`, delete the `mode == "required"` branch of `_prepare_managed_code_index`, which fails the spawn with `planner_code_index_unavailable`; a preflight failure always records the warning.

Tests: in `test_mcp_proxy_tools_spawn_agent_dedup.py`, replace `test_merge_worker_spawn_ignores_parent_merge_orchestrator_run` with `test_merge_worker_spawn_refuses_active_same_task_run`, which spawns a `merge-worker` on a task whose active run belongs to the requester's parent session and expects the refusal. In `test_plan_gate.py`, `test_planning_agents_constant` asserts `frozenset({"plan-adversary"})`; delete `test_plan_enhancer_spawn_against_malformed_plan_returns_structured_failure`, `test_repair_agent_spawn_receives_missing_index_diagnostics`, `test_planner_spawn_against_malformed_plan_returns_structured_failure` and `test_spawn_agent_impl_injects_symbol_repair_diagnostics`; every remaining test that passes `planner` as the agent name passes `plan-adversary`, and `test_planning_agent_spawn_rejects_missing_plan_id` keeps only its `plan-adversary` parameter. In `test_error_handling.py`, delete `test_planning_code_index_preflight_is_required`, `test_planning_agents_with_main_context_require_code_index_preflight` and `test_planning_code_index_failure_blocks_spawn_before_execute`, and `test_isolated_code_index_preflight_is_deferred` calls the narrowed signature. In `test_spawn_executor.py`, delete `test_required_managed_code_index_preflight_fails_closed`; `test_managed_code_index_preflight_uses_issued_credential` sets `code_index_preflight_mode="best_effort"`.

**Research context:**

- Exemption chain: `active_task_spawn_blocker` is called only by `active_task_response_if_blocked`, which only `admit_task_spawn` calls, and the spawn implementation is the only caller of `admit_task_spawn`. The exemption lets a `merge-orchestrator` run's `merge-worker` share its task; only the dispatcher's PR and merge stages start `merge-orchestrator`.
- The repair channel serves `planner` and `plan-enhancer-old`, which only the dispatcher's planning stage spawns with a task plan artifact; 3.2 deletes both definitions. `plan-adversary` is the planning runbook's Adversary seat and keeps its refusal gate.
- `_requires_planning_code_index` fires only when `initial_variables["stage_name"]` is `"planning"`, which only the dispatcher's spawn sets. `"required"` is the only mode that `_prepare_managed_code_index` handles differently.
- One proof per file: the plan coverage contract requires every other owner of a file to depend on a deletion proof's deliverable, so two proofs on one file cannot coexist. This leaf owns every deletion from the spawn implementation, and no other leaf targets that file. The blob is the 0.5.0 file at plan time.
- Between this leaf and the agents' deletion, a `merge-worker` that a build's `merge-orchestrator` spawns on its own task is refused, and a `planner` spawn on a plan whose symbol validation fails starts without diagnostics. The Constraints rule out starting a build while this plan executes.
- Planned checks: focused pytest on the four test Targets and `tests/mcp_proxy/tools/spawn_agent/`; ruff, format check and mypy on the changed source files.

**Acceptance:**

- 1.2.1 - A second active run on the same task is refused even when the requester is a `merge-worker` spawned by that run's session. test: `tests/mcp_proxy/tools/spawn_agent/test_mcp_proxy_tools_spawn_agent_dedup.py::test_merge_worker_spawn_refuses_active_same_task_run`.
- 1.2.2 - The plan gate covers `plan-adversary` only and has no repair channel. symbol: `PLANNING_AGENTS`. test: `tests/tasks/test_plan_gate.py::test_planning_agents_constant`.
- 1.2.3 - A `plan-adversary` spawn against a malformed plan still returns a structured refusal. test: `tests/tasks/test_plan_gate.py::test_plan_adversary_spawn_against_malformed_plan_returns_structured_failure`.
- 1.2.4 - An isolated spawn's code-index preflight is best effort, and a preflight failure records a warning instead of failing the spawn. symbol: `code_index_preflight_mode`. test: `tests/agents/test_spawn_executor.py::test_best_effort_preflight_records_warning_without_operator_credentials`.

## P2: Build and dispatcher removal
`kind: framing`

The web UI stops calling build, stage-registry and profile endpoints first. Then the build entry surfaces go, then everything that drives or wakes the dispatcher, and last the build and dispatch packages themselves. Stage storage and the task stage projection survive this phase; P4 removes them.

### 2.1 Web drops the build, profile and stage-registry views [category: code]
`kind: deliverable`

Targets:
- `web/src/components/activity/StagesTab.tsx::*` — operation: delete — scope-reason: the Stages tab reads only the stage registry and build profiles
- `web/src/components/activity/stages/ProfileDetailPanel.tsx::*` — operation: delete — scope-reason: Stages tab panel
- `web/src/components/activity/stages/ProfilesList.tsx::*` — operation: delete — scope-reason: Stages tab panel
- `web/src/components/activity/stages/StageDetailPanel.tsx::*` — operation: delete — scope-reason: Stages tab panel
- `web/src/components/activity/stages/StagesList.tsx::*` — operation: delete — scope-reason: Stages tab panel
- `web/src/components/activity/stages/StagesTabActions.ts::*` — operation: delete — scope-reason: Stages tab actions
- `web/src/components/activity/stages/StagesTabData.ts::*` — operation: delete — scope-reason: Stages tab data loader
- `web/src/components/activity/stages/__tests__/StagesTab.test.tsx::*` — operation: delete — scope-reason: tests the deleted tab
- `web/src/hooks/useStagesRegistry.ts::*` — operation: delete — scope-reason: reads the stage registry endpoint
- `web/src/hooks/__tests__/useStagesRegistry.test.ts::*` — operation: delete — scope-reason: tests the deleted hook
- `web/src/lib/__tests__/stageActions.test.ts::*` — operation: delete — scope-reason: covers only the removed stage move and advance helpers
- `web/src/components/activity/__tests__/useTasksTabFilters.test.tsx::*` — operation: delete — scope-reason: covers only the removed stage filter
- `web/src/components/activity/ActivityPanel.tsx::*` — scope-reason: drop the Stages tab import and case
- `web/src/components/activity/ActivityPanelTabs.tsx::*` — scope-reason: drop the Stages tab union member and entry
- `web/src/components/activity/TasksTabActions.ts::*` — scope-reason: drop the build start, quick build, control and retry helpers
- `web/src/components/activity/useTasksTabMenuActions.ts::*` — scope-reason: drop the four build menu handlers
- `web/src/components/activity/TaskQuickMenu.tsx::*` — scope-reason: drop the Build, Build Quick, Stop and Resume items and the build-state read
- `web/src/components/activity/TasksTab.tsx::*` — scope-reason: drop the stage-registry hook, stage query parameter, stage filter and build props
- `web/src/components/activity/useTasksTabFilters.ts::*` — scope-reason: keep the status filters and drop the stage half
- `web/src/components/activity/TasksTabFilters.tsx::*` — scope-reason: drop the Stage filter section and its props
- `web/src/components/activity/taskFieldRouting.ts::*` — scope-reason: drop the stage field family
- `web/src/types/tasks.ts::*` — scope-reason: drop `BuildState` and `build_state`
- `web/src/lib/stageActions.ts::*` — scope-reason: drop the unused stage move and advance helpers
- `web/src/__tests__/styleRatchet.test.ts::*` — scope-reason: stop reading the deleted stage files
- `web/src/components/activity/__tests__/activityToolbarTemplate.test.ts::*` — scope-reason: stop listing the deleted tab file
- `web/src/components/activity/__tests__/TaskQuickMenu.test.tsx::*` — scope-reason: drop the build menu cases
- `web/src/components/activity/__tests__/QuickMenu.test.tsx::*` — scope-reason: drop the build props
- `web/src/components/activity/__tests__/TasksTab.test.tsx::*` — scope-reason: drop the stage-filter case and the registry mocks
- `web/src/components/activity/__tests__/TasksTab.events.test.tsx::*` — scope-reason: drop the build-event cases and the registry mocks
- `web/src/components/activity/__tests__/TasksTab.filters.test.tsx::*` — scope-reason: drop the stage filter cases
- `web/src/components/activity/__tests__/TasksTab.setup.ts::*` — scope-reason: drop the build-state fixture and the registry mock
- `web/src/components/activity/__tests__/taskFieldRouting.test.ts::*` — scope-reason: drop the stage family case
- `web/tests/style-surfaces.spec.ts::*` — scope-reason: drop the stage-registry and profile mocks and the Stages selector
- `web/tests/web-chat-restore-plan.spec.ts::*` — scope-reason: drop the stage-registry mock

This leaf removes every web caller of the build, profile and stage-registry endpoints before 2.3 deletes them. It does not touch the task stage projection (`current_stage`, `stages`, `allow_automation`); 4.1 removes those reads together with their backend fields.

Delete the Stages tab: `StagesTab.tsx`, the `stages/` panels, actions, data loader and test, and the `useStagesRegistry` hook and its test. In `ActivityPanel.tsx` drop the tab import and its `"stages"` case; in `ActivityPanelTabs.tsx` drop the union member and the tab entry. A saved `stages` tab falls back through the panel's existing valid-tab check, so the panel hook and the command palette need no edit.

Remove the build controls: in `TasksTabActions.ts` the build start, quick build, build control and retry helpers; in `useTasksTabMenuActions.ts` the four build handlers; in `TaskQuickMenu.tsx` the Build, Build Quick, Stop and Resume items and the `build_state` read; in `TasksTab.tsx` the build props. Drop `BuildState` and `build_state` from `web/src/types/tasks.ts`.

Remove the stage filter: in `TasksTab.tsx` the `useStagesRegistry` call, the `stage` query parameter and the stage filter; in `useTasksTabFilters.ts` the stage half (status filters stay); in `TasksTabFilters.tsx` the Stage section and its props; in `taskFieldRouting.ts` the stage family. In `stageActions.ts` drop the stage move and advance helpers, which have no caller once the tab is gone; the `LifecycleTask` type stays for 4.1.

Test edits follow the source: drop the cases and mocks named in the Targets. `styleRatchet.test.ts` and `activityToolbarTemplate.test.ts` read deleted files by path, so they change in this leaf.

**Granularity:** one outcome, no web caller of an endpoint that 2.3 deletes. The Stages tab, the build controls and the stage filter share `TasksTab.tsx` and its tests, so separate leaves would edit the same files for one check.

**Research context:**

- Web inventory (2026-10-06): no web runtime code calls the per-task stage routes. The build, profile and stage-registry endpoints have callers only in the files above.
- `taskState.ts` `getTaskDisplayState` derives the review states from `current_stage.state`. That read needs a backend replacement first, so it belongs to 4.1, not here.
- Large files: no production web file in this leaf reaches 850 lines (`TasksTab.tsx` 809, `ActivityPanel.tsx` 681). The tests at or above 850 lines are exempt.
- Planned checks, run from `web/`: `npx vitest run src/components/activity src/lib src/hooks src/__tests__/styleRatchet.test.ts`, `npm run type-check`, `npm run lint:js`, and `npx playwright test tests/style-surfaces.spec.ts tests/web-chat-restore-plan.spec.ts`.

**Acceptance:**

- 2.1.1 - The Activity panel offers no Stages tab, and no web source imports the stage-registry hook. file: `web/src/components/activity/ActivityPanelTabs.tsx`. behavior: "stages" tab absent from `web/src/components/activity/ActivityPanelTabs.tsx`.
- 2.1.2 - The task quick menu offers no build actions, and no web source calls a build or profile endpoint. file: `web/src/components/activity/TaskQuickMenu.tsx`. behavior: "/api/build" absent from `web/src/components/activity/TasksTabActions.ts`.
- 2.1.3 - The Tasks tab filters by status only, and the task type has no build state. file: `web/src/types/tasks.ts`. behavior: "build_state" absent from `web/src/types/tasks.ts`.
- 2.1.4 - The web unit tests, type check, lint and the two named Playwright specs pass. file: `web/src/__tests__/styleRatchet.test.ts`.

### 2.2 Merge-orchestrator retires (depends: 1.2) [category: code]
`kind: deliverable`

Targets:
- `src/gobby/install/shared/workflows/agents/merge-orchestrator.yaml::*` — operation: delete — scope-reason: Decision Record item 3 retires the definition; only the dispatcher's PR and merge stages spawn it, and it loads the build coordination reference that 2.3 deletes
- `src/gobby/mcp_proxy/tools/merge_landscape.py::*` — scope-reason: move the verification-command helpers out and rewrite the docstring and two tool descriptions
- `src/gobby/mcp_proxy/tools/merge_verification.py`
- `src/gobby/install/shared/skills/gobby/references/source-control/merge-campaigns.md::*` — scope-reason: delete the merge-orchestrator campaign clause
- `tests/agents/test_merge_orchestrator_contract.py::*` — operation: delete — scope-reason: the definition it pins is gone
- `tests/integration/test_merge_orchestrator.py::*` — operation: delete — scope-reason: renamed to the landscape-primitives file below
- `tests/integration/test_merge_landscape_primitives.py`
- `tests/agents/test_merge_lifecycle.py::*` — scope-reason: delete the two merge-orchestrator tests
- `tests/dispatch/test_rules_stage_native.py::*` — scope-reason: drop the deleted YAML from the scanned list
- `tests/skills/test_removed_wait_tool_guidance.py::*` — scope-reason: drop the deleted YAML parameter
- `tests/workflows/test_planner_grammar_prompt.py::*` — scope-reason: drop the merge-orchestrator mapping entry
- `tests/workflows/test_spawn_scope_rules.py::*` — scope-reason: delete the bundled merge-orchestrator spawn-scope test
- `tests/workflows/test_workflows_agent_definitions.py::*` — scope-reason: drop merge-orchestrator from the provider table and the definition list, and delete its no-work close test
- `tests/workflows/test_retired_bundled_definitions.py::*` — scope-reason: list merge-orchestrator as a retired agent

Delete `merge-orchestrator.yaml`. It has to go before 2.3: it loads the build skill's coordination reference, which 2.3 deletes, and `test_reference_contract_4_2_2` checks that every reference a bundled definition names exists. 1.2 already removed the spawn guard's parent-orchestrator exemption.

Split `src/gobby/mcp_proxy/tools/merge_landscape.py` (907 lines): move the verification-command helpers (`_verification_environment`, `_is_env_assignment`, `_reject_verification_env_key`, `_consume_env_assignments`, `_normalize_verification_command`, `_reject_git_verification_args`, `_looks_like_test_execution`, `_has_test_scope`, `_reject_unscoped_test_command`, `_reject_verification_command`) into the new module `merge_verification.py`. The three entry points that `verify_in_worktree` calls become public names, `verification_environment`, `normalize_verification_command` and `reject_verification_command`; the rest stay private there. Then rewrite the module docstring and the `analyze_merge_landscape` and `verify_in_worktree` descriptions without the removed agent ("Used to survey a merge campaign before planning", "Used as a post-merge verification gate").

In `merge-campaigns.md` step 3, delete the clause "or coordinate a campaign with `merge-orchestrator`"; 3.1 rewrites the rest of the worker campaign.

Tests: rename `tests/integration/test_merge_orchestrator.py` to `test_merge_landscape_primitives.py` with `git mv`, delete `test_orchestrator_yaml_loads`, and rewrite its docstring. Delete `test_merge_orchestrator_contract.py`. In `test_merge_lifecycle.py`, delete `test_merge_orchestrator_uses_stage_native_merge_result_tool` and `test_merge_orchestrator_instructions_do_not_reference_removed_lifecycle_tools`. Add `merge-orchestrator` to `RETIRED_AGENTS` in `test_retired_bundled_definitions.py`. In `test_spawn_scope_rules.py`, delete `test_bundled_merge_orchestrator_may_spawn_merge_workers`. In `test_workflows_agent_definitions.py`, drop the `merge-orchestrator` provider row and list entry and delete `test_merge_orchestrator_no_work_close_waits_for_reviewer`. Drop the YAML from the parameter list in `test_removed_wait_tool_guidance.py`, from the scanned paths in `test_bundled_merge_assets_do_not_reference_removed_lifecycle_tools`, and the `merge-orchestrator` entry from the mapping in `test_reference_contract_4_2_2`.

**Research context:**

- Decision Record item 3: `record_pr_verdict` only moves the `pr` stage or escalates, and `record_merge_result` writes the merge stage, releases the dispatch mutex (terminal cleanup already releases it at run end) and calls the build package's merge-artifact cleanup. Nothing else needs the orchestrator's verdict path.
- Sync removal: `sync_bundled_agents` soft-deletes installed Gobby-tagged rows missing from disk, so the installed merge-orchestrator row goes at the next sync.
- `merge_landscape.py` helpers are used only inside that file. `tests/mcp_proxy/tools/test_merge_landscape.py` imports only `_active_merge_resolution_payload` and `register_merge_landscape_tools` and patches `os.killpg` in the module, which stays valid. `docs/reference-audit/source-control.json` cites only the public merge-landscape tools, which stay in the file; its one mention of the agent is a dated installed-row observation, not a citation, so the audit is unchanged.
- Dispatch side: `PROMPT_BUILDERS` keeps its `merge-orchestrator` key and `tests/dispatch/test_dispatch_prompts.py` still passes, because both read builder keys, not definitions. The stage registry rows that name the agent need no definition: sync parses the reviewer selector and never resolves agent names. Both go with their packages in 2.5 and 4.2, and `_dispatch_mutex_release.py` keeps its merge join until 3.5 deletes it.
- A build that reaches the PR or merge stage between this leaf and 2.3 would fail to spawn the deleted agent. The Constraints rule out starting a build while this plan executes.
- Arbitrary `merge-orchestrator` strings in tests that load no definition (`tests/storage/test_agent_run_live_stats.py`, `tests/workflows/test_agent_workflow_completion.py`) stay; they name a workflow, not the bundled agent.
- Planned checks: focused pytest on every test Target plus `tests/mcp_proxy/tools/test_merge_landscape.py` and `tests/dispatch/test_dispatch_prompts.py`; ruff, format check and mypy on the changed source files.

**Acceptance:**

- 2.2.1 - The bundled definitions no longer include `merge-orchestrator`. test: `tests/workflows/test_retired_bundled_definitions.py::test_retired_agent_yaml_is_absent_from_active_and_deprecated_bundles`.
- 2.2.2 - `verify_in_worktree` still rejects unscoped test commands after the helper move. test: `tests/mcp_proxy/tools/test_merge_landscape.py::test_verify_in_worktree_rejects_unscoped_test_commands`.
- 2.2.3 - No bundled definition, skill reference or merge-landscape tool description names merge-orchestrator. file: `src/gobby/mcp_proxy/tools/merge_landscape.py`. behavior: "merge-orchestrator" absent from `src/gobby/install/shared/skills/gobby/references/source-control/merge-campaigns.md`.

### 2.3 Build entry surfaces and the build skill capability go (depends: 2.1, 2.2) [category: code]
`kind: deliverable`

Targets:
- `src/gobby/cli/build.py::*` — operation: delete — scope-reason: the `gobby build` command
- `src/gobby/cli/_build_daemon.py::*` — operation: delete — scope-reason: build CLI daemon client; its two error helpers move to the new module below
- `src/gobby/cli/_build_options.py::*` — operation: delete — scope-reason: build CLI options
- `src/gobby/cli/_build_output.py::*` — operation: delete — scope-reason: build CLI output
- `src/gobby/cli/profiles.py::*` — operation: delete — scope-reason: the `gobby profiles` command
- `src/gobby/cli/_daemon_errors.py`
- `src/gobby/cli/__init__.py::*` — scope-reason: drop the `build` and `profiles` lazy command entries
- `src/gobby/cli/cron.py::*` — scope-reason: import the daemon error helpers from their new module
- `src/gobby/cli/pipelines.py::*` — scope-reason: import the daemon error helpers from their new module
- `src/gobby/cli/tasks/repair.py::*` — operation: delete — scope-reason: the `gobby tasks repair-lifecycle` command, which repairs only stage manifests and build events
- `src/gobby/cli/tasks/main.py::*` — scope-reason: drop the `repair-lifecycle` registration
- `src/gobby/tasks/lifecycle_repair.py::*` — operation: delete — scope-reason: the repair service behind `repair-lifecycle`; it reads build history and stage manifests
- `src/gobby/mcp_proxy/tools/build.py::*` — operation: delete — scope-reason: the five build MCP tools
- `src/gobby/mcp_proxy/tools/profiles.py::*` — operation: delete — scope-reason: the `gobby-profiles` MCP server
- `src/gobby/mcp_proxy/tools/tasks/_build_observability.py::*` — operation: delete — scope-reason: `get_build_status`, `explain_dispatch` and `list_build_history`
- `src/gobby/mcp_proxy/registries.py::*` — scope-reason: drop the profiles registry block
- `src/gobby/mcp_proxy/tools/tasks/_ops_factory.py::*` — scope-reason: drop the build registry merge and the subclass that only exposed the `build_task` schema
- `src/gobby/mcp_proxy/tools/tasks/_factory.py::*` — scope-reason: drop the observability registry merge
- `src/gobby/mcp_proxy/services/tool_proxy_constants.py::*` — scope-reason: drop the `gobby-profile` server alias
- `src/gobby/servers/routes/build.py::*` — operation: delete — scope-reason: the `/api/build` routes
- `src/gobby/servers/routes/profiles.py::*` — operation: delete — scope-reason: the `/api/profiles` routes
- `src/gobby/servers/routes/__init__.py::*` — scope-reason: drop the build and profile router exports
- `src/gobby/servers/_app_routes.py::*` — scope-reason: stop including the build and profile routers
- `src/gobby/install/shared/skills/gobby/references/build/overview.md` — operation: delete
- `src/gobby/install/shared/skills/gobby/references/build/starting.md` — operation: delete
- `src/gobby/install/shared/skills/gobby/references/build/profiles.md` — operation: delete
- `src/gobby/install/shared/skills/gobby/references/build/stages.md` — operation: delete
- `src/gobby/install/shared/skills/gobby/references/build/coordination.md` — operation: delete
- `src/gobby/install/shared/skills/gobby/references/build/monitoring.md` — operation: delete
- `src/gobby/install/shared/skills/gobby/references/build/recovery.md` — operation: delete
- `src/gobby/install/shared/skills/gobby/catalog.json::*` — scope-reason: drop the build capability and the folded `build` and `build-coordinator` skills
- `src/gobby/install/shared/skills/gobby/references/intro/overview.md`
- `src/gobby/install/shared/skills/gobby/references/tasks/backups.md`
- `src/gobby/install/shared/workflows/rules/build-coordinator/require-build-coordinator-for-gobby-build.yaml::*` — operation: delete — scope-reason: gates `gobby build` on the deleted coordination reference
- `docs/reference-audit/build.json::*` — operation: delete — scope-reason: the audit of the deleted build capability
- `docs/reference-audit/plan.json::*` — scope-reason: drop the delegated build operation
- `docs/reference-audit/tasks.json::*` — scope-reason: drop the delegated build operations and the `repair-lifecycle` command entry
- `docs/reference-audit/intro.json::*` — scope-reason: drop the scope note that routes profile tools to the build audit
- `tests/cli/test_cli_build.py::*` — operation: delete — scope-reason: tests the deleted command
- `tests/cli/test_build_output.py::*` — operation: delete — scope-reason: tests the deleted output module
- `tests/cli/test_tasks_repair_lifecycle.py::*` — operation: delete — scope-reason: tests the deleted repair command
- `tests/tasks/test_lifecycle_repair.py::*` — operation: delete — scope-reason: tests the deleted repair service
- `tests/build/test_cli_daemon_timeout.py::*` — operation: delete — scope-reason: tests the deleted daemon client
- `tests/build/test_build_stop.py::*` — operation: delete — scope-reason: drives the deleted build tools; the package half goes with 2.5
- `tests/build/test_retry_caps.py::*` — operation: delete — scope-reason: drives the deleted build tools; the package half goes with 2.5
- `tests/build/test_target_branch.py::*` — operation: delete — scope-reason: drives the deleted build tools; the package half goes with 2.5
- `tests/build/test_build_surface_cleanup.py::*` — operation: delete — scope-reason: drives the deleted build surfaces
- `tests/mcp_proxy/tools/test_build_observability.py::*` — operation: delete — scope-reason: tests the deleted observability tools
- `tests/mcp_proxy/tools/test_build_stage_caps.py::*` — operation: delete — scope-reason: tests the deleted build tool
- `tests/mcp_proxy/tools/test_mcp_proxy_tools_build.py::*` — operation: delete — scope-reason: tests the deleted build tools
- `tests/servers/routes/test_build_route_stage_caps.py::*` — operation: delete — scope-reason: tests the deleted build route
- `tests/servers/routes/test_servers_routes_build.py::*` — operation: delete — scope-reason: tests the deleted build routes
- `tests/skills/test_build_skill.py::*` — operation: delete — scope-reason: tests the deleted build capability
- `tests/skills/test_build_coordinator_skill.py::*` — operation: delete — scope-reason: tests the deleted coordination reference and rule
- `tests/skills/scenarios/build-coordinator/unattended-build-coordination.yaml::*` — operation: delete — scope-reason: scenario for the deleted coordination reference
- `tests/skills/test_skill_tdd_harness.py::*` — scope-reason: delete the test that runs the deleted build-coordinator scenario
- `tests/skills/test_capability_catalog.py::*` — scope-reason: the folded-skill count drops from 29 to 27
- `tests/skills/test_reference_library.py::*` — scope-reason: drop the build capability cases and scenario operations
- `tests/mcp_proxy/test_registries.py::*` — scope-reason: stop expecting `build_task` in the task ops registry
- `tests/mcp_proxy/tools/test_read_only_classification.py::*` — scope-reason: drop the two read-only build observability tools
- `tests/workflows/test_task_enforcement_rules.py::*` — scope-reason: drop the build tools from the read-only and non-interactive tool lists
- `tests/workflows/test_agent_monitoring_rules.py::*` — scope-reason: drop the build-coordinator rule cases
- `tests/framing_corpus.py::*` — scope-reason: drop the deleted rule name
- `tests/skills/test_removed_wait_tool_guidance.py::*` — scope-reason: drop the deleted coordination reference from the updated-skill list and its parameter

Delete the `gobby build` and `gobby profiles` commands with their helper modules, the five build MCP tools, the `gobby-profiles` MCP server, the three build observability tools, and the `/api/build` and `/api/profiles` routes. Remove their registrations: the `build` and `profiles` entries in the CLI lazy command map, the profiles block in `registries.py`, the build and observability merges in the task tool factories, the `gobby-profile` alias in `tool_proxy_constants.py`, and the router exports and `include_router` calls. In `_ops_factory.py`, the `_TaskOpsToolRegistry` subclass exists only to expose the `build_task` schema, so it goes and the factory uses `InternalToolRegistry`.

`cron.py` and `pipelines.py` import `_daemon_error_detail` and `_daemon_error_message` from `_build_daemon.py`. Move both helpers unchanged into the new module `_daemon_errors.py` and point the two imports at it.

Delete the `gobby tasks repair-lifecycle` command and its service `gobby.tasks.lifecycle_repair`. The service only removes or reseeds stage manifests and records build-history events, and once build is gone nothing creates either. `src/gobby/cli/tasks/main.py` drops the command's import and registration, the `tasks.json` audit drops its command entry, and `src/gobby/install/shared/skills/gobby/references/tasks/backups.md` drops `repair-lifecycle` from its list of operator-only procedures.

The build skill capability goes with its tools, because the reference-library contract checks every capability audit against the source it cites. Delete the seven `references/build/` topics and `docs/reference-audit/build.json`. In `catalog.json` drop the `build` capability block and the folded `build` and `build-coordinator` entries; the folded `build-rule` entry is unrelated rule authoring and stays. In the intro topic's `overview.md` drop the sentence that routes `gobby-profiles` to the build profiles reference. In the `plan.json` and `tasks.json` audits drop the `build` lists under `delegated_operations`, and in `intro.json` drop the scope note about profile tools. Delete the `require-build-coordinator-for-gobby-build` rule, which gates `gobby build` on the deleted coordination reference.

Test edits follow the source. `test_skill_tdd_harness.py` deletes `test_build_coordinator_turns_manual_coordination_into_build_fixes`, which runs the deleted scenario. The four mixed files under `tests/build/` drive the deleted tools as well as `gobby.build`, and 2.5 deletes that directory, so they go whole here.

**Granularity:** the CLI, MCP and HTTP entry points share `gobby.build.service`, and the build skill capability's audit cites all three. The reference-library contract fails while the capability names a deleted tool, so the surfaces and the capability go in one change.

**Research context:**

- Entry-surface inventory (2026-10-06): no edited production file in this leaf reaches 850 lines (`registries.py` 604, `cron.py` 407). The deleted modules are `build.py` 585, `_build_daemon.py` 318, `profiles.py` 267, the MCP `build.py` 406 and `profiles.py` 279, `_build_observability.py` 116, the route `build.py` 543 and `profiles.py` 194.
- No `gobby tasks` subcommand calls build. The `build_stop` and `build_resume` strings in task CLI, de-escalation and resume metadata are history reasons, not tool calls; 2.4 and P4 own them.
- `gobby.storage.build_profiles` stays until 2.5: `gobby.build.profiles` imports it.
- `gobby.tasks.lifecycle_repair` (563 lines) imports `gobby.storage.build_history`, which 2.5 deletes, so it cannot wait for the stage leaves. Its command module has 65 lines and `src/gobby/cli/tasks/main.py` has 256.
- `_dispatcher_tick.py` is not an entry surface; its only importer is the stage review module, and 2.4 removes both the module and those calls.
- Remaining build-tool and profile names outside history after this leaf: the docs guides (5.2) and `CHANGELOG.md`, which is history.
- Planned checks: focused pytest on every edited test Target, `tests/skills/`, `tests/cli/test_cron_cli.py`, `tests/cli/test_cli_pipelines.py` and `tests/cli/test_pipelines_coverage.py`; ruff, format check and mypy on the changed source files.

**Acceptance:**

- 2.3.1 - The CLI has no `build`, `profiles` or `tasks repair-lifecycle` command, and `gobby cron` and `gobby pipelines` still report daemon errors. symbol: `_daemon_error_message`. test: `tests/cli/test_cron_cli.py::TestCronRun::test_run_daemon_rejection`.
- 2.3.2 - No build, profile or build-observability MCP tool is registered. file: `src/gobby/mcp_proxy/registries.py`. test: `tests/mcp_proxy/test_registries.py::test_setup_tasks_ops_registry_omits_legacy_front_half_tick`.
- 2.3.3 - The daemon serves no `/api/build` or `/api/profiles` route. file: `src/gobby/servers/_app_routes.py`. behavior: "build_router" absent from `src/gobby/servers/_app_routes.py`.
- 2.3.4 - The gobby skill catalog has no build capability, and the reference-library contract passes. file: `src/gobby/install/shared/skills/gobby/catalog.json`. test: `tests/skills/test_reference_library.py::test_reference_contract_3_2_1`.

### 2.4 Nothing drives or wakes the dispatcher (depends: 2.3) [category: code]
`kind: deliverable`

Targets:
- `src/gobby/system_automation.py::*` — scope-reason: keep the stale-claim sweep and pipeline maintenance; drop project dispatch, safe build-claim recovery and the orphan dispatch-mutex sweep
- `src/gobby/scheduler/executor.py::*` — scope-reason: drop the `dispatcher` cron action and its heartbeat ticks
- `src/gobby/storage/cron.py::*` — scope-reason: drop the `gobby:dispatcher` priority and the `dispatcher` action type, and move the job-name policy out
- `src/gobby/storage/cron_job_policy.py`
- `src/gobby/scheduler/scheduler.py::*` — scope-reason: import `is_removed_automation_job` from the new policy module
- `src/gobby/servers/routes/cron.py::*` — scope-reason: import `is_removed_automation_job` from the new policy module
- `src/gobby/storage/cron_models.py::*` — scope-reason: drop the `dispatcher` action type
- `src/gobby/storage/cron_children.py::*` — scope-reason: drop the `dispatcher` child action type
- `src/gobby/config/system_loops.py::*` — scope-reason: describe the automation loop without task dispatch
- `crates/gcore/assets/config/runtime_config_contract.json::*` — scope-reason: regenerated derived carrier for the reworded loop descriptions
- `tests/contracts/http/config_schema.json::*` — scope-reason: re-recorded public config schema response with the reworded loop description
- `src/gobby/hooks/event_handlers/_dispatch.py::*` — operation: delete — scope-reason: handles only stage-pipeline terminal events, which only the dispatcher creates
- `src/gobby/runner_broadcasting.py::*` — scope-reason: drop the stage-pipeline terminal forwarding to the deleted handler
- `src/gobby/agents/agent_cleanup.py::*` — scope-reason: drop the dispatcher tick after agent cleanup
- `src/gobby/agents/terminal_cleanup.py::*` — scope-reason: drop the build merge-artifact cleanup after agent exit
- `src/gobby/sessions/mailbox.py::*` — scope-reason: drop the `build` message target, the cross-project build-coordinator allowance and the recipient helpers only they used
- `src/gobby/mcp_proxy/tools/agent_messaging.py::*` — scope-reason: drop the `build` target from the `send_message` schema, checks and description
- `src/gobby/workflows/agent_models.py::*` — scope-reason: drop `build` from the send-message target literal
- `src/gobby/install/shared/skills/gobby/references/agents/messaging.md`
- `src/gobby/install/shared/AGENTS.md`
- `AGENTS.md`
- `src/gobby/servers/routes/tasks.py::*` — scope-reason: drop the derived `build_state` field
- `src/gobby/storage/tasks/_dispatcher_wake.py::*` — operation: delete — scope-reason: wakes the dispatcher on task close and stage transitions
- `src/gobby/storage/tasks/_lifecycle.py::*` — scope-reason: drop the dispatcher wake on close
- `src/gobby/storage/tasks/_stage_state_transitions.py::*` — scope-reason: drop the dispatcher wake on stage transitions and move two module helpers out
- `src/gobby/storage/tasks/_stage_transition_rules.py`
- `src/gobby/storage/tasks/_stage_states.py::*` — scope-reason: import the two moved helpers from their new module
- `src/gobby/mcp_proxy/tools/tasks/_dispatcher_tick.py::*` — operation: delete — scope-reason: schedules dispatcher ticks after stage verdicts
- `src/gobby/mcp_proxy/tools/tasks/_stage_review.py::*` — scope-reason: drop the dispatcher tick calls and the build-coordinator signoff relay
- `src/gobby/mcp_proxy/tools/tasks/_stage_ops.py::*` — scope-reason: drop the build merge-artifact cleanup call
- `src/gobby/telemetry/logging.py::*` — scope-reason: drop the build and dispatch automation log namespaces
- `tests/scheduler/test_dispatch_executor.py::*` — operation: delete — scope-reason: covers only the dispatcher cron action
- `tests/hooks/event_handlers/test_dispatch.py::*` — operation: delete — scope-reason: covers only the deleted handler
- `tests/e2e/test_build_dispatcher_autonomy.py::*` — operation: delete — scope-reason: drives the dispatch loop
- `tests/build/test_dispatcher_stage_wake.py::*` — operation: delete — scope-reason: covers the deleted wake helper
- `tests/mcp_proxy/tools/tasks/test_dispatcher_tick_handoff.py::*` — operation: delete — scope-reason: covers the deleted tick helper
- `tests/mcp_proxy/tools/test_review_signoff_coordinator.py::*` — operation: delete — scope-reason: covers only the deleted signoff relay
- `tests/mcp_proxy/tools/tasks/test_notification_background_tasks.py::*` — scope-reason: delete the two signoff relay cases and their import
- `tests/mcp_proxy/tools/test_agent_messaging.py::*` — scope-reason: drop the build target from the schema and description checks and delete the two build-coordinator cases
- `tests/scheduler/test_system_automation_loop.py::*` — scope-reason: drop the project-dispatch cases and the dispatch snapshot keys
- `tests/agents/test_agent_cleanup.py::*` — scope-reason: drop the dispatcher tick patches
- `tests/agents/test_terminal_cleanup.py::*` — scope-reason: drop the merge-artifact cleanup cases and patches
- `tests/agents/test_lifecycle_monitor_extra.py::*` — scope-reason: drop the merge-artifact cleanup patch
- `tests/sessions/test_mailbox.py::*` — scope-reason: drop the build target case and the cross-project coordinator cache cases, and reject `build` as an unknown target
- `tests/telemetry/test_logging.py::*` — scope-reason: drop the build runner log-surface parameter

This leaf removes every caller that schedules, wakes or ticks the dispatcher, and every import of a `gobby.build` or `gobby.dispatch` helper outside those packages, so 2.5 can delete them.

The automation loop in `system_automation.py` keeps `sweep_stale_claims` and pipeline maintenance. Delete the per-project dispatch queue and everything that feeds it (`schedule_project_dispatch`, `dispatch_project_once`, `_dispatch_projects`, `_dispatchable_project_ids`, `_project_automation_enabled`, `PROJECT_DISPATCH_TIMEOUT_SECONDS` and the constructor's project and dispatch-count fields), the safe build-claim recovery and the orphan dispatch-mutex sweep. Expired dispatch mutexes need no sweep: acquire takes over an expired lease. The maintenance summary drops its `safe_claims_*` and `orphan_mutexes_released` keys, the tick summary drops `projects` and `dispatch`, and the snapshot drops `dispatch_count`, `pending_projects` and `project_dispatch_timeout_seconds`. The constructor and `set_services` signatures stay, so the runner wiring is unchanged.

The cron executor drops the `dispatcher` action branch, `_execute_dispatcher` and `_dispatcher_heartbeat_ticks`. `src/gobby/storage/cron.py`, `cron_models.py` and `cron_children.py` drop `"dispatcher"` from their action-type literals, and `cron.py` drops the `gobby:dispatcher` priority. `REMOVED_AUTOMATION_JOB_NAMES` keeps `gobby:dispatcher`, so a leftover row stays hidden and purged.

Split `src/gobby/storage/cron.py` (867 lines): move the job-name policy, `REMOVED_AUTOMATION_JOB_NAMES`, `CODEWIKI_NIGHTLY_JOB_PREFIX`, `RETIRED_AUTOMATION_JOB_NAME_PREFIXES`, `CRON_JOB_NAME_PRIORITIES`, `DEFAULT_CRON_JOB_PRIORITY`, `is_removed_automation_job` and `_cron_job_priority` (renamed `cron_job_priority`), into the new module `cron_job_policy.py`. `cron.py`, `scheduler.py` and the cron routes import them from there.

`system_loops.py` rewords the two descriptions that mention task dispatch: the `enabled` field reads "Enable daemon-owned stale-claim sweeps and pipeline maintenance." and the model reads "Stale-claim sweep and pipeline maintenance automation loop." The runtime config contract is regenerated with `uv run python scripts/generate_runtime_config_contract.py`, and the HTTP config corpus is re-recorded with 1.1's command, which changes only `config_schema.json`.

Hidden consumers:

- Delete the stage-pipeline terminal handler `gobby.hooks.event_handlers._dispatch` and, in `runner_broadcasting.py`, the forwarding block in `broadcast_pipeline_event` with `PipelineTerminalPayload`, `_dispatch_pipeline_terminal_event` and `RunDbHook`.
- `agent_cleanup.py` drops the dispatcher tick after cleanup.
- `terminal_cleanup.py` deletes `cleanup_merged_task_artifacts_after_agent_exit` and its call block, including the `already_implemented` branch, which only cleans build artifacts and build branches.
- The `send_message` `build` target goes. It resolves a build run ID, a build input ref or a root task ref through `build_runs`, then fans out to every active agent run in that task's subtree, and only build coordination used it. In `mailbox.py`, `MESSAGE_TARGETS` drops `build` and `resolve_target` ends with the `agent` branch. Delete `_validate_build_sender`, `_resolve_build_target`, `_resolve_build_run_root_task`, `_build_recipient_session_ids`, `_agent_selector_metadata`, `_allows_cross_project_build_coordinator`, `_allows_cached_cross_project_build_coordinator`, `_task_project_id`, the cross-project cache field with its two constants, and the imports only they used (`gobby.build.coordinator`, `gobby.storage.build_history`, `resolve_task_reference`, `TaskNotFoundError`, `OrderedDict`, `time`). `_agent_recipient_session_ids` already has no caller, and once the build query goes `_dedupe_agent_recipient_rows` and `_dedupe` serve only it, so all three go; `_select_agent_recipient` stays for the agent target. `_resolve_agent_target` drops its cross-project allowance block and passes `allow_cross_project=False`, so an agent-target send into another project gets the same refusal as any other non-session direct send.
- `agent_messaging.py` drops `build` from the `target` literal and from the two target sets that require `target_id` and default `project_id`. Its description's first sentence lists "global, project, parent, session, or agent", the `target_id` sentence ends after "target='agent' (agent run id)", and the wake sentence reads "global and project fanout remains queued without live wakes". `agent_models.py` drops `build` from `SendMessageTarget`.
- The messaging guidance follows the tool: in `references/agents/messaging.md`, "session/run/build identity" becomes "session or run identity", "Targets `session`, `agent`, and `build`" becomes "Targets `session` and `agent`", and "`project`, `global`, and `build` fanout" becomes "`project` and `global` fanout". `src/gobby/install/shared/AGENTS.md` makes the same two edits in its Session Messaging Contract, and root `AGENTS.md` rule 12 makes the fanout edit.
- `routes/tasks.py` drops the `derive_build_state` import, `_apply_build_state` with its call in the list route, and the `build_state` assignment in `get_task`; 2.1 already removed the web reader.
- Delete `_dispatcher_wake.py`. `_lifecycle.py` drops the wake loop in `close_task`, and `_stage_state_transitions.py` drops `_wake_dispatcher` and its two calls. The wake import is lazy and both callers swallow its errors, so without this edit every close or stage move on an automation-enabled row would log an import failure once 2.5 deletes `gobby.build`.
- Delete `_dispatcher_tick.py`. `_stage_review.py` drops its four tick calls and deletes the build-coordinator signoff relay: `_relay_signoff_to_build_coordinator_sync`, which messages the coordinator recorded on the task's newest build run, `_schedule_signoff_relay` and their calls in `approve_review` and `reject_review`, together with the `summary_allows_cross_project_coordinator` import and the lazy `gobby.storage.build_history` import. `_stage_ops.py` drops the build merge-artifact cleanup import and call; Decision Record item 3 already covers what that cleanup did.
- `telemetry/logging.py` removes `gobby.dispatch` and `gobby.build` from `_AUTOMATION_NAMESPACES`.

Split `_stage_state_transitions.py` (854 lines): move the module functions `illegal` and `terminal_after_done` into the new module `_stage_transition_rules.py`. `_stage_state_transitions.py` and `_stage_states.py` import them from there. 4.2 deletes all three modules.

Messaging and relay tests: in `test_mailbox.py`, delete `test_build_target_only_includes_active_agents_in_task_subtree`, `test_agent_cross_project_auth_cache_uses_ttl_and_skips_missing_task`, `test_agent_cross_project_auth_cache_invalidates_missing_sender`, `test_agent_cross_project_auth_cache_is_bounded` and the `BuildHistoryStorage` import, and parametrize `test_rejects_unknown_target` over `"workspace"` and `"build"`. In `test_agent_messaging.py`, `test_send_message_schema_documents_target_parameters` expects the five remaining targets and the phrase "global and project fanout remains queued without live wakes"; delete `test_send_message_build_target_uses_context_project_for_coordinator` and `test_send_message_agent_target_allows_cross_project_coordinator`. Delete `test_review_signoff_coordinator.py`, and in `test_notification_background_tasks.py` delete `test_signoff_relay_runs_synchronously`, `test_signoff_relay_surfaces_non_database_errors` and the `_schedule_signoff_relay` import.

**Granularity:** one outcome, no module outside `gobby.build` and `gobby.dispatch` imports either package or reads build history. Each caller edit is small, and a partial removal leaves 2.5 blocked on the rest. The two splits are the size rule's required moves.

**Research context:**

- Dispatcher-consumer inventory (2026-10-06). `hooks/event_handlers/_dispatch.py` handles only `stage-pipeline:*` mutex rows, and only `gobby.dispatch.stage_pipeline` creates them.
- `list_automation_candidates` (`gobby.storage.tasks._automation`) stays here: the dispatcher still imports it until 2.5, and 4.2 deletes it with the epic and ancestor gates.
- Large files: `src/gobby/storage/cron.py` (867 lines) and `_stage_state_transitions.py` (854 lines) are at or above the 850-line threshold, so each carries a split above. The policy move takes about 25 lines out of `cron.py`; its only outside importers are the scheduler and the cron routes. The two moved stage helpers are about 25 lines; their only outside importer is `_stage_states.py` (395 lines).
- The `"dispatcher"` lease-holder strings in `gobby.agents.lifecycle_reconciliation` and `gobby.runner_lifecycle_agents` name a mutex holder, not the dispatcher module, and stay.
- Build-history readers outside the two packages (2026-10-08): `mailbox.py` (the `build` target and the coordinator allowance), the `_stage_review.py` signoff relay, the build observability tools and routes that 2.3 deletes, and `gobby.tasks.lifecycle_repair`, which 2.3 also deletes. After this leaf, 2.5 can delete `gobby.storage.build_history`. No bundled definition lists `build` in `send_message_targets`, and no skill or rule sends to it.
- `mailbox.py` has 809 lines and `agent_messaging.py` 487, so neither needs a split.
- Planned checks: focused pytest on every edited test Target, `tests/scheduler/`, `tests/storage/test_cron*.py`, `tests/hooks/`, `tests/sessions/`, `tests/mcp_proxy/tools/test_agent_messaging.py` and `tests/config/test_runtime_config_contract.py`; ruff, format check and mypy on the changed source files. Config corpus replay: `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/contracts/test_http_corpus.py -k "test_case_replays_equal and config"`.

**Acceptance:**

- 2.4.1 - The automation loop sweeps stale claims and runs pipeline maintenance without dispatching projects, and its snapshot has no dispatch keys. symbol: `SystemAutomationLoop`. test: `tests/scheduler/test_system_automation_loop.py::test_pipeline_maintenance_uses_single_stale_task_scan`.
- 2.4.2 - The cron executor and cron storage accept no `dispatcher` action type. file: `src/gobby/storage/cron_models.py`. behavior: "dispatcher" absent from `src/gobby/storage/cron_models.py`.
- 2.4.3 - Closing a task and moving a stage schedule no dispatcher wake. symbol: `close_task`. behavior: "wake_dispatcher_for_task_change" absent from `src/gobby/storage/tasks/_lifecycle.py`.
- 2.4.4 - Outside `gobby.build` and `gobby.dispatch`, no production module imports either package. file: `src/gobby/system_automation.py`. behavior: "gobby.build" absent from `src/gobby/mcp_proxy/tools/tasks/_stage_review.py`.
- 2.4.5 - `send_message` rejects `target="build"` as an unknown target, and its schema lists five targets. test: `tests/sessions/test_mailbox.py::TestMailboxBroadcast::test_rejects_unknown_target`. test: `tests/mcp_proxy/tools/test_agent_messaging.py::TestSendMessage::test_send_message_schema_documents_target_parameters`.
- 2.4.6 - The loop descriptions name no task dispatch, and the config corpus cases replay equal. behavior: "automation dispatch" absent from `tests/contracts/http/config_schema.json`. test: `tests/contracts/test_http_corpus.py::test_case_replays_equal`.


### 2.5 Build and dispatch packages and build profiles go (depends: 2.4) [category: code]
`kind: deliverable`

Targets:
- `src/gobby/config/build.py::*` — operation: delete — scope-reason: the file-based build config; 1.1 moved its only surviving reader
- `crates/gcore/assets/config/runtime_config_contract.json::*` — scope-reason: regenerate after the config module deletion; expected unchanged because the build config is not part of `DaemonConfig`
- `src/gobby/storage/build_profiles.py::*` — operation: delete — scope-reason: build profile storage, read only by the deleted build package and its sync
- `src/gobby/storage/build_history.py::*` — operation: delete — scope-reason: build-run history storage; after 2.3 and 2.4 only the build and dispatch packages read it
- `src/gobby/install/shared/registry/build_profiles.yaml::*` — operation: delete — scope-reason: bundled build profiles
- `src/gobby/runner_init/storage.py::*` — scope-reason: drop the build profile sync at startup
- `src/gobby/sync_registry.py::*` — scope-reason: drop the build profile sync entry
- `src/gobby/sync/integrity.py::*` — scope-reason: drop the build profile registry category
- `src/gobby/cli/sync.py::*` — scope-reason: drop `build_profiles` from the workflows sync category
- `src/gobby/tasks/isolation.py::*` — scope-reason: define the checkout-mode literal locally instead of importing it from the build config
- `src/gobby/telemetry/health_metrics.py::*` — scope-reason: drop the `dispatcher` automation outcome family, whose only recorder is the deleted dispatcher
- `AGENTS.md`
- `CLAUDE.md`
- `tests/agents/test_bundled_agent_contract.py`
- `tests/storage/tasks/test_runtime_dispatch_mutex.py`
- `tests/tasks/test_is_escalated_first_class.py`
- `tests/config/test_config_build.py::*` — operation: delete — scope-reason: tests the deleted build config
- `tests/config/test_build_surface.py::*` — operation: delete — scope-reason: tests the deleted build config
- `tests/storage/test_storage_build_profiles.py::*` — operation: delete — scope-reason: tests the deleted profile storage
- `tests/storage/test_build_history.py::*` — operation: delete — scope-reason: tests the deleted history storage
- `tests/skills/test_plan_draft_stage_list.py::*` — operation: delete — scope-reason: checks the dispatch guide against the deleted skippable-stage list
- `tests/config/test_removed_config_fields.py::*` — scope-reason: drop the build config cases
- `tests/cli/installers/test_bundled_sync_logging.py::*` — scope-reason: drop the build profile sync patch
- `tests/sync/test_integrity.py::*` — scope-reason: drop the build profile category cases
- `tests/telemetry/test_health_metrics.py::*` — scope-reason: drop the dispatcher outcome cases
- `tests/agents/test_backend_ingress.py::*` — scope-reason: drop the dispatch spawn cases
- `tests/agents/test_spawn_native_only.py::*` — scope-reason: drop the dispatch spawn cases
- `tests/storage/hub/test_postgres_placeholder_remap.py::*` — scope-reason: drop the build lifecycle cases
- `tests/storage/tasks/test_hierarchy_cycle_guards.py::*` — scope-reason: drop the build observability subtree case
- `tests/storage/test_stage_review_findings.py::*` — scope-reason: drop the dispatch spawn cases
- `tests/tasks/test_task_backup.py::*` — scope-reason: drop the dispatch context reload case
- `tests/workflows/test_agent_resolver.py::*` — scope-reason: drop the dispatch skill composition cases
- `tests/workflows/test_agent_workflow_runtime_cleanup.py::*` — scope-reason: drop the dispatch action and dispatcher cases
- `tests/worktrees/test_worktree_service.py::*` — scope-reason: drop the build workspace service cases
- `src/gobby/build/__init__.py::*` — operation: delete — scope-reason: the build package goes
- `src/gobby/build/branch_cleanup.py::*` — operation: delete — scope-reason: the build package goes
- `src/gobby/build/claim_recovery.py::*` — operation: delete — scope-reason: the build package goes
- `src/gobby/build/control_artifacts.py::*` — operation: delete — scope-reason: the build package goes
- `src/gobby/build/control_runtime.py::*` — operation: delete — scope-reason: the build package goes
- `src/gobby/build/controls.py::*` — operation: delete — scope-reason: the build package goes
- `src/gobby/build/coordinator.py::*` — operation: delete — scope-reason: the build package goes
- `src/gobby/build/dispatch_tick.py::*` — operation: delete — scope-reason: the build package goes
- `src/gobby/build/input_resolution.py::*` — operation: delete — scope-reason: the build package goes
- `src/gobby/build/lifecycle.py::*` — operation: delete — scope-reason: the build package goes
- `src/gobby/build/lifecycle_state.py::*` — operation: delete — scope-reason: the build package goes
- `src/gobby/build/observability.py::*` — operation: delete — scope-reason: the build package goes
- `src/gobby/build/options.py::*` — operation: delete — scope-reason: the build package goes
- `src/gobby/build/plan_lifecycle.py::*` — operation: delete — scope-reason: the build package goes
- `src/gobby/build/profiles.py::*` — operation: delete — scope-reason: the build package goes
- `src/gobby/build/project_controls.py::*` — operation: delete — scope-reason: the build package goes
- `src/gobby/build/project_state.py::*` — operation: delete — scope-reason: the build package goes
- `src/gobby/build/restart_controls.py::*` — operation: delete — scope-reason: the build package goes
- `src/gobby/build/results.py::*` — operation: delete — scope-reason: the build package goes
- `src/gobby/build/resume_lifecycle.py::*` — operation: delete — scope-reason: the build package goes
- `src/gobby/build/runtime_hooks.py::*` — operation: delete — scope-reason: the build package goes
- `src/gobby/build/service.py::*` — operation: delete — scope-reason: the build package goes
- `src/gobby/build/stage_manifest.py::*` — operation: delete — scope-reason: the build package goes
- `src/gobby/build/target_branch.py::*` — operation: delete — scope-reason: the build package goes
- `src/gobby/build/task_lifecycle.py::*` — operation: delete — scope-reason: the build package goes
- `src/gobby/build/validation.py::*` — operation: delete — scope-reason: the build package goes
- `src/gobby/build/workspace_common.py::*` — operation: delete — scope-reason: the build package goes
- `src/gobby/build/workspace_git.py::*` — operation: delete — scope-reason: the build package goes
- `src/gobby/build/workspace_recovery.py::*` — operation: delete — scope-reason: the build package goes
- `src/gobby/build/workspace_services.py::*` — operation: delete — scope-reason: the build package goes
- `src/gobby/build/workspaces.py::*` — operation: delete — scope-reason: the build package goes
- `src/gobby/dispatch/AGENTS.md` — operation: delete
- `src/gobby/dispatch/CLAUDE.md` — operation: delete
- `src/gobby/dispatch/__init__.py::*` — operation: delete — scope-reason: the dispatch package goes
- `src/gobby/dispatch/_planning_enhancement.py::*` — operation: delete — scope-reason: the dispatch package goes
- `src/gobby/dispatch/_rule_actions.py::*` — operation: delete — scope-reason: the dispatch package goes
- `src/gobby/dispatch/_rule_merge.py::*` — operation: delete — scope-reason: the dispatch package goes
- `src/gobby/dispatch/_rule_state.py::*` — operation: delete — scope-reason: the dispatch package goes
- `src/gobby/dispatch/actions.py::*` — operation: delete — scope-reason: the dispatch package goes
- `src/gobby/dispatch/agent_counts.py::*` — operation: delete — scope-reason: the dispatch package goes
- `src/gobby/dispatch/audit.py::*` — operation: delete — scope-reason: the dispatch package goes
- `src/gobby/dispatch/constants.py::*` — operation: delete — scope-reason: the dispatch package goes
- `src/gobby/dispatch/context.py::*` — operation: delete — scope-reason: the dispatch package goes
- `src/gobby/dispatch/daemon_resume.py::*` — operation: delete — scope-reason: the dispatch package goes
- `src/gobby/dispatch/discovery_artifacts.py::*` — operation: delete — scope-reason: the dispatch package goes
- `src/gobby/dispatch/dispatcher.py::*` — operation: delete — scope-reason: the dispatch package goes
- `src/gobby/dispatch/lease_cleanup.py::*` — operation: delete — scope-reason: the dispatch package goes
- `src/gobby/dispatch/merge_recovery.py::*` — operation: delete — scope-reason: the dispatch package goes
- `src/gobby/dispatch/mutex.py::*` — operation: delete — scope-reason: the dispatch package goes
- `src/gobby/dispatch/prompts.py::*` — operation: delete — scope-reason: the dispatch package goes
- `src/gobby/dispatch/results.py::*` — operation: delete — scope-reason: the dispatch package goes
- `src/gobby/dispatch/rules.py::*` — operation: delete — scope-reason: the dispatch package goes
- `src/gobby/dispatch/skill_composition.py::*` — operation: delete — scope-reason: the dispatch package goes
- `src/gobby/dispatch/spawn.py::*` — operation: delete — scope-reason: the dispatch package goes
- `src/gobby/dispatch/spawn_actions.py::*` — operation: delete — scope-reason: the dispatch package goes
- `src/gobby/dispatch/spawn_artifacts.py::*` — operation: delete — scope-reason: the dispatch package goes
- `src/gobby/dispatch/spawn_completion.py::*` — operation: delete — scope-reason: the dispatch package goes
- `src/gobby/dispatch/spawn_errors.py::*` — operation: delete — scope-reason: the dispatch package goes
- `src/gobby/dispatch/stage_pipeline.py::*` — operation: delete — scope-reason: the dispatch package goes
- `src/gobby/dispatch/workspace_merge.py::*` — operation: delete — scope-reason: the dispatch package goes
- `src/gobby/dispatch/write_set_guard.py::*` — operation: delete — scope-reason: the dispatch package goes
- `tests/build/__init__.py` — operation: delete
- `tests/build/test_build_state.py::*` — operation: delete — scope-reason: tests the deleted build package
- `tests/build/test_child_merge_repair.py::*` — operation: delete — scope-reason: tests the deleted build package
- `tests/build/test_claim_recovery.py::*` — operation: delete — scope-reason: tests the deleted build package
- `tests/build/test_clean_branches.py::*` — operation: delete — scope-reason: tests the deleted build package
- `tests/build/test_ingress_surface_parity.py::*` — operation: delete — scope-reason: tests the deleted build package
- `tests/build/test_input_resolution.py::*` — operation: delete — scope-reason: tests the deleted build package
- `tests/build/test_observability.py::*` — operation: delete — scope-reason: tests the deleted build package
- `tests/build/test_restart_controls.py::*` — operation: delete — scope-reason: tests the deleted build package
- `tests/build/test_resume_lifecycle.py::*` — operation: delete — scope-reason: tests the deleted build package
- `tests/build/test_workspace_git.py::*` — operation: delete — scope-reason: tests the deleted build package
- `tests/build_pipeline/__init__.py` — operation: delete
- `tests/build_pipeline/test_automation_readiness.py::*` — operation: delete — scope-reason: tests the deleted build pipeline
- `tests/build_pipeline/test_build_pipeline_build_profiles.py::*` — operation: delete — scope-reason: tests the deleted build pipeline
- `tests/build_pipeline/test_build_pipeline_cascade.py::*` — operation: delete — scope-reason: tests the deleted build pipeline
- `tests/build_pipeline/test_build_pipeline_service.py::*` — operation: delete — scope-reason: tests the deleted build pipeline
- `tests/build_pipeline/test_build_resolves_manifest.py::*` — operation: delete — scope-reason: tests the deleted build pipeline
- `tests/build_pipeline/test_controls.py::*` — operation: delete — scope-reason: tests the deleted build pipeline
- `tests/dispatch/__init__.py` — operation: delete
- `tests/dispatch/test_actions_surface.py::*` — operation: delete — scope-reason: tests the deleted dispatch package
- `tests/dispatch/test_agent_definition_view.py::*` — operation: delete — scope-reason: tests the deleted dispatch package
- `tests/dispatch/test_bundled_agent_contract.py::*` — operation: delete — scope-reason: moves to `tests/agents/test_bundled_agent_contract.py`
- `tests/dispatch/test_constants.py::*` — operation: delete — scope-reason: tests the deleted dispatch package
- `tests/dispatch/test_daemon_resume.py::*` — operation: delete — scope-reason: tests the deleted dispatch package
- `tests/dispatch/test_delivery_chain.py::*` — operation: delete — scope-reason: tests the deleted dispatch package
- `tests/dispatch/test_dispatch_actions.py::*` — operation: delete — scope-reason: tests the deleted dispatch package
- `tests/dispatch/test_dispatch_prompts.py::*` — operation: delete — scope-reason: tests the deleted dispatch package
- `tests/dispatch/test_dispatcher.py::*` — operation: delete — scope-reason: tests the deleted dispatch package
- `tests/dispatch/test_is_escalated_first_class.py::*` — operation: delete — scope-reason: moves to `tests/tasks/test_is_escalated_first_class.py`
- `tests/dispatch/test_merge_rule.py::*` — operation: delete — scope-reason: tests the deleted dispatch package
- `tests/dispatch/test_mutex.py::*` — operation: delete — scope-reason: moves to `tests/storage/tasks/test_runtime_dispatch_mutex.py`
- `tests/dispatch/test_no_agent_paths.py::*` — operation: delete — scope-reason: tests the deleted dispatch package
- `tests/dispatch/test_planning_enhancement.py::*` — operation: delete — scope-reason: tests the deleted dispatch package
- `tests/dispatch/test_pr_full_walk.py::*` — operation: delete — scope-reason: tests the deleted dispatch package
- `tests/dispatch/test_pr_rules.py::*` — operation: delete — scope-reason: tests the deleted dispatch package
- `tests/dispatch/test_pr_to_merge_advance.py::*` — operation: delete — scope-reason: tests the deleted dispatch package
- `tests/dispatch/test_reload_candidate_includes_stages.py::*` — operation: delete — scope-reason: tests the deleted dispatch package
- `tests/dispatch/test_retry_neutral_bounds.py::*` — operation: delete — scope-reason: tests the deleted dispatch package
- `tests/dispatch/test_review_rules_no_agent.py::*` — operation: delete — scope-reason: tests the deleted dispatch package
- `tests/dispatch/test_rules.py::*` — operation: delete — scope-reason: tests the deleted dispatch package
- `tests/dispatch/test_rules_stage_native.py::*` — operation: delete — scope-reason: tests the deleted dispatch package
- `tests/dispatch/test_runtime_dispatch_mutex.py::*` — operation: delete — scope-reason: tests the deleted dispatch package
- `tests/dispatch/test_services_daemon_config.py::*` — operation: delete — scope-reason: tests the deleted dispatch package
- `tests/dispatch/test_skill_composition.py::*` — operation: delete — scope-reason: tests the deleted dispatch package
- `tests/dispatch/test_spawn_actions_cleanup.py::*` — operation: delete — scope-reason: tests the deleted dispatch package
- `tests/dispatch/test_spawn_isolation.py::*` — operation: delete — scope-reason: tests the deleted dispatch package
- `tests/dispatch/test_terminal_non_merge.py::*` — operation: delete — scope-reason: tests the deleted dispatch package
- `tests/dispatch/test_workspace_merge.py::*` — operation: delete — scope-reason: tests the deleted dispatch package

Delete the `gobby.build` and `gobby.dispatch` packages, including `src/gobby/dispatch/AGENTS.md` and its `CLAUDE.md` shim, the file-based build config, and the test directories `tests/build/`, `tests/build_pipeline/` and `tests/dispatch/`. After 2.4 no production module outside the packages imports them.

Three tests in `tests/dispatch/` do not test dispatch, so they move with `git mv`:

- `test_bundled_agent_contract.py` moves to `tests/agents/test_bundled_agent_contract.py` unchanged. It checks that every MCP tool a bundled agent names is registered.
- `test_mutex.py` moves to `tests/storage/tasks/test_runtime_dispatch_mutex.py` and imports `RuntimeDispatchMutex` from `gobby.storage.tasks._runtime_mutex` instead of the deleted re-export.
- `test_is_escalated_first_class.py` moves to `tests/tasks/test_is_escalated_first_class.py` and drops the deleted dispatch directory from its scanned sources.

Build profiles go with the package that reads them. Delete `gobby.storage.build_profiles` and the bundled `build_profiles.yaml`, drop the profile sync from `src/gobby/runner_init/storage.py` and `sync_registry.py`, the `build_profiles` registry category from `src/gobby/sync/integrity.py`, and `build_profiles` from the workflows category in `src/gobby/cli/sync.py`. The `build_profiles` table stays until 6.1 drops it. Build history goes the same way: delete `gobby.storage.build_history`, whose `build_runs` and `build_history_events` tables also stay until 6.1. `src/gobby/tasks/isolation.py` defines `CheckoutMode = Literal["none", "worktree", "clone"]` locally. `health_metrics.py` drops the `dispatcher` entry from `_AUTOMATION_OUTCOMES`.

Root instructions: in `AGENTS.md`, drop the `uv run gobby build` development command and the "Dispatch" architecture fact, which points at the deleted `src/gobby/dispatch/AGENTS.md`. In `CLAUDE.md`, drop `src/gobby/dispatch/` from the nested instruction files. 5.2 handles the rest of the root guidance.

Mixed test files drop only their build and dispatch cases; their other cases stay. In `tests/workflows/test_agent_workflow_runtime_cleanup.py` that case is `test_submit_for_review_handoff_terminates_worker_and_unblocks_reviewer_dispatch`, which runs the dispatcher heartbeat; the `SpawnAgentAction`, `stage_ops` and `_stage_test_helpers` imports that only it uses go with it.

**Granularity:** the two packages import each other, so neither can go first, and build profiles go with their only reader. Most Targets are whole-file deletions.

**Research context:**

- Package inventory (2026-10-06, `git ls-files`): 109 files remain in the five directories after 2.3 and 2.4. `tests/build/test_ingress_surface_parity.py` and `tests/dispatch/test_terminal_non_merge.py` import neither package but assert stage-cap ingress and dispatcher terminal-close contracts, so they go with the directories.
- `gobby.config.build` is file-based (`build.yaml`) and is not part of `DaemonConfig`, so its deletion changes no runtime config contract carrier.
- `gobby.build.profiles` was the only package reader of `gobby.storage.build_profiles`; `gobby.storage.tasks._stage_registry` reads the `build_profiles` table by SQL, not through the module, until 4.2 deletes it.
- `tests/skills/test_plan_draft_stage_list.py` compares `docs/guides/dispatch.md` with the skippable-stage list in the deleted build config; 5.2 deletes that guide.
- Planned checks: focused pytest on every edited and moved test Target, `tests/sync/`, `tests/config/` and `tests/docs/test_claude_md_contract_section.py`; `uv run python -c "import gobby.runner"`; ruff, format check and mypy on `src/`.

**Acceptance:**

- 2.5.1 - `src/gobby/build`, `src/gobby/dispatch`, `src/gobby/config/build.py` and `src/gobby/storage/build_history.py` no longer exist, and the daemon modules import without them. file: `src/gobby/runner_init/storage.py`. behavior: "build_profiles" absent from `src/gobby/runner_init/storage.py`.
- 2.5.2 - The bundled agent contract still passes from its new location. test: `tests/agents/test_bundled_agent_contract.py::test_bundled_agent_mcp_references_match_registered_tool_inventory`.
- 2.5.3 - The runtime dispatch mutex tests pass against the storage module. test: `tests/storage/tasks/test_runtime_dispatch_mutex.py::test_acquire_link_release_round_trip`.
- 2.5.4 - Registry sync and its integrity check know no build profile category. file: `src/gobby/sync/integrity.py`. test: `tests/sync/test_integrity.py::TestGetDirtyContentTypes::test_content_type_dirs_matches_sync_targets`.
- 2.5.5 - The root instructions point at no dispatch package. file: `CLAUDE.md`. behavior: "src/gobby/dispatch" absent from `AGENTS.md`.

## P3: Merge agents, stage-only definitions and stage tools
`kind: framing`

With the dispatcher gone, nothing spawns the remaining merge agent or the stage-only definitions, and nothing reads a stage verdict. This phase retires `merge-worker`, then the stage-only definitions and the stage text in kept ones, then the stage tools, review transitions, stage CLI and stage routes. Stage storage still exists after this phase; P4 removes its readers and the storage itself.

### 3.1 Merge-worker retires (depends: 2.5) [category: code]
`kind: deliverable`

Targets:
- `src/gobby/install/shared/workflows/agents/merge-worker.yaml::*` — operation: delete — scope-reason: Decision Record item 3 retires the definition; only the dispatcher's merge stage and the deleted merge-orchestrator spawned it
- `src/gobby/install/shared/skills/gobby/references/source-control/merge-campaigns.md::*` — scope-reason: the campaign lands each workspace directly instead of dispatching a worker
- `tests/agents/test_merge_lifecycle.py::*` — operation: delete — scope-reason: after 2.2 every remaining case pins the deleted definition
- `tests/workflows/test_workflows_agent_definitions.py::*` — scope-reason: drop the merge-worker provider row and delete its native-delegation test
- `tests/skills/test_removed_wait_tool_guidance.py::*` — scope-reason: the merge campaign no longer waits on or reads a worker result
- `tests/skills/test_skill_tdd_harness.py::*` — scope-reason: drop the merge-expert capture scenario parameter
- `tests/skills/scenarios/merge-expert/page-terminal-capture.yaml::*` — operation: delete — scope-reason: the scenario pages a terminal merge-worker result that no longer exists
- `tests/workflows/test_retired_bundled_definitions.py::*` — scope-reason: list merge-worker as a retired agent

Delete `merge-worker.yaml`. After 2.2 and 2.5 nothing names it except its own tests and the merge-campaigns guidance.

Rewrite the worker parts of `merge-campaigns.md` for direct landing by the operator or the calling seat:

- Step 3 becomes: land each selected workspace in plan order with `merge_worktree` or `merge_clone`, using the confirmed target and source. It drops the installed-definition check and the `spawn_agent` field guidance.
- Step 4 starts "Use `merge_worktree`/`merge_clone` for final landing" and replaces the managed-worker sentence with "Resolve through the merge tools; do not synthesize manual contents through file-reading bypasses."
- Step 5, the `wait_for_agent` subscription and terminal-result reading, is deleted.
- Old step 6 becomes step 5: "Verify with the scoped `verify_in_worktree` command and record each result in the campaign report." The `record_merge_result` call goes; the rest of the sentence stays.
- In the active-resolution paragraph, delete the sentence about the no-progress redispatch cap, which only applied to an orchestrator run. In the cherry-pick paragraph, delete the clause "and a restricted worker needs an allowed recovery path".

Tests: delete `test_merge_lifecycle.py` and the merge-expert scenario. In `test_workflows_agent_definitions.py`, drop the `merge-worker` row from the provider table and delete `test_merge_worker_blocks_native_delegation_tools`. In `test_removed_wait_tool_guidance.py`, drop the merge-campaigns parameter from `WAKE_DRIVEN_GUIDANCE` and `CAPTURE_GUIDANCE`. In `test_skill_tdd_harness.py`, `test_coordinator_skills_page_bounded_terminal_captures` keeps only the `plan` parameter. Add `merge-worker` to `RETIRED_AGENTS` in `test_retired_bundled_definitions.py`.

**Research context:**

- Remaining `merge-worker` strings after this leaf and where they go: `src/gobby/mcp_proxy/tools/tasks/_dispatch_mutex_release.py` and `tests/mcp_proxy/tools/tasks/test_record_merge_result.py` go with the stage tools in 3.5, and the `spawnable_agents` example in `docs/guides/agents.md` is rewritten in 5.2. The dated installed-row observation in `docs/reference-audit/source-control.json` is evidence, not a citation, and stays.
- Tests that use `merge-worker` as an arbitrary agent or workflow name and load no definition stay: `tests/hooks/test_provider_launch_guard.py`, `tests/hooks/test_session_coordinator.py`, `tests/mcp_proxy/tools/spawn_agent/test_factory.py`, `test_initial_variables.py`, `test_mcp_proxy_tools_spawn_agent_dedup.py`, `tests/mcp_proxy/tools/test_agent_live_stats.py`, `tests/mcp_proxy/tools/test_agents.py`, `tests/storage/test_agent_run_live_stats.py`, `tests/workflows/test_dry_run_tool_gates.py` and `tests/workflows/test_spawn_scope_rules.py`.
- `docs/reference-audit/source-control.json` cites `merge-campaigns.md` for the merge tools that stay, and for `record_merge_result` and `record_pr_verdict`, whose entries 3.5 removes with the tools. The reference-library contract checks that each cited reference file and implementation symbol exists; it does not require the reference to name the tool, so this leaf's rewrite leaves the audit valid.
- `test_wait_guidance_is_wake_driven` requires `wait_for_agent` in Markdown guidance it scans, and `test_terminal_result_guidance_pages_capture_metadata` requires completion wording. A campaign that lands directly does neither, so both parameters go.
- `sync_bundled_agents` soft-deletes the installed merge-worker row at the next sync.
- Planned checks: focused pytest on `tests/workflows/test_workflows_agent_definitions.py`, `tests/skills/test_removed_wait_tool_guidance.py`, `tests/skills/test_skill_tdd_harness.py`, `tests/skills/test_reference_library.py` and `tests/agents/test_bundled_agent_contract.py`.

**Acceptance:**

- 3.1.1 - No bundled definition named `merge-worker` exists, and the bundled agent contract passes without it. test: `tests/workflows/test_retired_bundled_definitions.py::test_retired_agent_yaml_is_absent_from_active_and_deprecated_bundles`. test: `tests/agents/test_bundled_agent_contract.py::test_bundled_agent_mcp_references_match_registered_tool_inventory`.
- 3.1.2 - The merge-campaigns guidance lands workspaces directly and names no worker, wait or `record_merge_result`. file: `src/gobby/install/shared/skills/gobby/references/source-control/merge-campaigns.md`. behavior: "merge-worker" absent from `src/gobby/install/shared/skills/gobby/references/source-control/merge-campaigns.md`.
- 3.1.3 - The reference-library contract passes after the rewrite. test: `tests/skills/test_reference_library.py::test_reference_contract_3_2_1`.

### 3.2 Planning definitions retire with the spawned review round (depends: 3.1) [category: code]
`kind: deliverable`

Targets:
- `src/gobby/install/shared/workflows/agents/planner.yaml::*` — operation: delete — scope-reason: Decision Record item 4; only the dispatcher's planning stage spawned it
- `src/gobby/install/shared/workflows/agents/plan-adversary-old.yaml::*` — operation: delete — scope-reason: Decision Record item 4; the stage-bound adversary only the dispatcher spawned
- `src/gobby/install/shared/workflows/agents/plan-adversary-taskless-old.yaml::*` — operation: delete — scope-reason: Decision Record item 4; replaced by the plan-adversary seat
- `src/gobby/install/shared/workflows/agents/plan-enhancer-old.yaml::*` — operation: delete — scope-reason: Decision Record item 4; the stage-bound enhancer only the dispatcher spawned
- `src/gobby/install/shared/workflows/agents/plan-enhancer-taskless-old.yaml::*` — operation: delete — scope-reason: Decision Record item 4; replaced by the plan-enhancer seat
- `src/gobby/install/shared/workflows/rules/review-learning/inject-planner-lessons.yaml::*` — operation: delete — scope-reason: scoped only to the deleted planner
- `src/gobby/install/shared/workflows/rules/review-learning/inject-plan-enhancer-lessons.yaml::*` — operation: delete — scope-reason: scoped only to the two deleted enhancers
- `src/gobby/install/shared/workflows/rules/review-learning/inject-plan-reviewer-lessons.yaml::*` — scope-reason: scope only plan-adversary
- `src/gobby/install/shared/workflows/rules/memory-lifecycle/guard-plan-memory-writes.yaml::*` — scope-reason: name only plan-adversary in the agent list
- `src/gobby/install/shared/workflows/rules/plan-mode/reset-plan-mode-on-session-start.yaml::*` — scope-reason: drop the planner exception
- `src/gobby/install/shared/skills/gobby/references/plan/review.md::*` — scope-reason: delete the spawned taskless round and open static-seat rounds with prepare_plan_review_round
- `src/gobby/install/shared/skills/gobby/references/plan/enhancement.md::*` — scope-reason: delete the taskless enhancer launch
- `src/gobby/install/shared/skills/gobby/references/plan/repair.md::*` — scope-reason: the spawn and bind failure case is gone
- `src/gobby/mcp_proxy/tools/plans/review_evidence.py::*` — scope-reason: remove the bind_evidence_run MCP tool per Decision Record item 9
- `src/gobby/install/shared/workflows/agents/plan-adversary.yaml::*` — scope-reason: drop bind_evidence_run from the blocked MCP tools
- `docs/reference-audit/plan.json::*` — scope-reason: drop the bind_evidence_run capability entry
- `tests/agents/test_plan_adversary_internal_research_definition.py::*` — operation: delete — scope-reason: pins only deleted definitions
- `tests/agents/test_plan_adversary_loads_plan_review.py::*` — operation: delete — scope-reason: pins only plan-adversary-old
- `tests/agents/test_plan_adversary_manifest.py::*` — operation: delete — scope-reason: pins only plan-adversary-old
- `tests/agents/test_plan_adversary_no_edits_on_reject.py::*` — operation: delete — scope-reason: pins only plan-adversary-old and planner
- `tests/agents/test_plan_adversary_self_check.py::*` — operation: delete — scope-reason: pins only plan-adversary-old
- `tests/agents/test_plan_adversary_taskless_definition.py::*` — operation: delete — scope-reason: pins only plan-adversary-taskless-old
- `tests/agents/test_plan_enhancer_agents.py::*` — operation: delete — scope-reason: pins only the two deleted enhancers
- `tests/agents/test_planner_loads_plan_draft.py::*` — operation: delete — scope-reason: pins only planner
- `tests/skills/test_plan_adversary_rejection.py::*` — operation: delete — scope-reason: pins only plan-adversary-old
- `tests/agents/test_agents_sync.py::*` — scope-reason: delete the taskless adversary sync test and drop planner from the real-bundle sync
- `tests/agents/test_discovery_agents.py::*` — scope-reason: delete the plan-adversary-old and planner tests
- `tests/agents/test_planner_worktree_restrictions.py::*` — scope-reason: drop the five deleted definitions from the checked list
- `tests/agents/test_plan_seat_definitions.py::*` — scope-reason: the adversary no longer blocks a removed tool
- `tests/mcp_proxy/test_plans_tools.py::*` — scope-reason: drop bind_evidence_run from the registered tool list
- `tests/mcp_proxy/tools/test_plan_review_evidence_errors.py::*` — scope-reason: drop the bind_evidence_run cases
- `tests/mcp_proxy/test_stage_review_schema.py::*` — scope-reason: delete the parity test that reads the taskless adversary
- `tests/skills/test_plan_review_skill.py::*` — scope-reason: the repository-access test stops reading deleted definitions
- `tests/skills/test_plan_skill_delegated_mode.py::*` — scope-reason: delete the taskless launch test
- `tests/skills/test_plan_skill_grammar.py::*` — scope-reason: drop the planner and plan-adversary-old surfaces
- `tests/skills/test_review_learning_skill.py::*` — scope-reason: stop reading planner and the taskless adversary, and drop the taskless and bind_evidence_run phrases
- `tests/workflows/test_memory_lifecycle_rules.py::*` — scope-reason: the plan memory guard names only plan-adversary
- `tests/workflows/test_planner_grammar_prompt.py::*` — scope-reason: delete the planner prompt tests and drop four mapping entries
- `tests/workflows/test_retired_bundled_definitions.py::*` — scope-reason: list the retired definitions and rules, and delete the planner plan-mode test
- `tests/workflows/test_review_learning_rules.py::*` — scope-reason: drop the deleted lesson rules and the taskless scope
- `tests/workflows/test_step_enforcement.py::*` — scope-reason: stop reading the two deleted taskless definitions
- `tests/workflows/test_workflows_agent_definitions.py::*` — scope-reason: drop five provider rows and delete the planner verdict-label test

**Granularity:** one outcome, no bundled definition, rule or guidance names a planning definition that only the dispatcher or the taskless round spawned. Once the five definitions go, the rules and guidance that name them fail their own tests, and the taskless round guidance is the last caller of `bind_evidence_run`, so the tool goes in the same change. Most Targets are whole-file deletions.

Delete the five definitions. Each `*-old` definition and `planner` is spawned only by the dispatcher's planning stage or by the taskless round in the plan guidance, and the planning runbook's seats (`plan-writer`, `plan-enhancer`, `plan-adversary`) replace them.

Rules:

- Delete `inject-planner-lessons` and `inject-plan-enhancer-lessons`, whose `agent_scope` names only deleted definitions.
- `inject-plan-reviewer-lessons` keeps `agent_scope: [plan-adversary]`.
- In `guard-plan-memory-writes`, the `_agent_type` list keeps only `'plan-adversary'`; the `plan_mode` branch stays.
- In `reset-plan-mode-on-session-start`, delete the clause `and variables.get('_agent_type') != 'planner'`. `plan-writer` also sets `plan_mode` and was never exempt, so no surviving definition changes behavior.

Guidance in `references/plan/`:

- `review.md`: delete the paragraph that starts "Call gobby-plans:prepare_plan_review_round immediately before spawning". The static-round paragraph then starts "Open a review round with gobby-plans:prepare_plan_review_round, then bind it once with gobby-plans:bind_static_review_seats:", followed by the existing text from "the evidence owner (the session that prepared the round)". Its sentence "The binding is immutable and exclusive with a run binding." becomes "The binding is immutable." In the next paragraph, "Taskless reviewers never mutate task state; stage-native reviewers use only their authorized verdict transitions." becomes "Reviewers never mutate task state." The sentence "The evidence rounds above belong to spawned taskless reviewers, gobby build stages, and static-seat rounds." becomes "The evidence rounds above are static-seat rounds." In "Coordinator and recovery", "Existing delegated/unattended authority permits coordinator votes with rationale." becomes "Existing delegated authority permits coordinator votes with rationale.", and the paragraph that starts "Spawned taskless reviewers deliver the exact JSON" becomes "The adversary sends the exact JSON result to the Writer and coordinator seats through send_message."
- `enhancement.md`: delete the paragraph that starts "Outside the runbook, the coordinator spawns plan-enhancer-taskless-old".
- `repair.md`: the evidence-recovery bullet "Spawn/bind failure: expire_plan_review_evidence with accurate spawn_failed evidence." becomes "Abandoned static round: expire_plan_review_evidence through a live bound seat, as [review](review.md) describes." Its second sentence stays.

Review evidence (Decision Record item 9): in `src/gobby/mcp_proxy/tools/plans/review_evidence.py`, delete the `bind_evidence_run` closure and its `registry.register` call. `PlanReviewEvidenceService` is unchanged. In `plan-adversary.yaml`, delete `"gobby-plans:bind_evidence_run"` from `blocked_mcp_tools`. In `docs/reference-audit/plan.json`, delete the `bind_evidence_run` entry.

Tests:

- Delete the nine test files above that pin only deleted definitions.
- `test_agents_sync.py`: delete `test_taskless_adversary_syncs_grok_xhigh`; `test_sync_with_real_bundled_agents` drops `planner` from its expected names.
- `test_discovery_agents.py`: delete `test_plan_adversary_documents_task_skill_gate_exclusion` and `test_planner_treats_discovery_markers_as_authoritative_context`.
- `test_planner_worktree_restrictions.py`: `PLANNERS` keeps `plan-writer`, `plan-enhancer` and `plan-adversary`.
- `test_plan_seat_definitions.py`: `test_adversary_blocks_all_evidence_round_tools` drops `bind_evidence_run`.
- `test_plans_tools.py`: `test_plan_tool_schemas_and_happy_path` drops `bind_evidence_run` from its tool list.
- `test_plan_review_evidence_errors.py`: `test_bound_round_prepare_returns_structured_error` drops its `bind_evidence_run` metadata assertion, and `test_review_evidence_write_boundaries_return_structured_expected_errors` drops its `bind_evidence_run` case.
- `test_stage_review_schema.py`: delete `test_finding_schema_parity_with_adversary_contracts` and the two path constants only it reads. `test_reject_review_uses_shared_finding_schema` stays until 3.5.
- `test_plan_review_skill.py`: `test_review_prompts_have_direct_repository_and_task_access` loses its agent parameter and definition read and keeps its assertion on the review reference.
- `test_plan_skill_delegated_mode.py`: delete `test_taskless_launch_checkpoints_before_waiting`.
- `test_plan_skill_grammar.py`: delete `PLANNER` and `ADVERSARY`, and drop the `planner` and `plan-adversary-old` surfaces from `test_table_row_decomposition_rule_documented`.
- `test_review_learning_skill.py`: `test_plan_skill_documents_parallel_review_contract` drops the `plan-adversary-taskless-old` and `bind_evidence_run` phrases. `test_review_producers_do_not_reference_review_learning` drops its `planner` producer. `test_plan_loop_recording_contract` drops its `planner` read and assertion. `test_interactive_approval_sequence` drops its taskless definition read and the two assertions on it.
- `test_memory_lifecycle_rules.py`: `TestGuardPlanMemoryWrites::test_condition_covers_planning_contexts_without_recall_gate` checks only `'plan-adversary'`, and the `planning_context` parameters of `TestGuardPlanMemoryWritesEngine::test_plan_memory_write_blocks_once_then_allows_retry` become `plan_mode` and `_agent_type` `plan-adversary`.
- `test_planner_grammar_prompt.py`: delete `PLANNER`, `_planner_prompt`, `test_planner_prompt_contains_grammar`, `test_planner_authors_narrative_only_not_the_manifest` and `test_planner_changelog_uses_v1_section_id`. `test_reference_contract_4_2_2` drops the `planner`, `plan-adversary-taskless-old`, `plan-enhancer-old` and `plan-enhancer-taskless-old` entries.
- `test_retired_bundled_definitions.py`: add the five definitions to `RETIRED_AGENTS` and the two deleted rules to `RETIRED_RULES`, and delete `test_planner_enables_surviving_plan_mode_write_guard`.
- `test_review_learning_rules.py`: `CLASS_INJECTION_RULE_FILES` drops the two deleted rule files, and `test_class_injection_agent_scoping` drops the two deleted rules and the taskless scope.
- `test_step_enforcement.py`: `test_zsh_quoting_guidance_contract` keeps its Bash reference assertions and drops the adversary definition read. Delete `test_bundled_plan_enhancer_enhance_step_reads_memory_but_never_writes` and `_PLAN_ENHANCER_TASKLESS`.
- `test_workflows_agent_definitions.py`: drop the five rows from `test_build_smoke_agent_runtime_mappings` and delete `test_planner_relies_on_review_handoff_to_clear_rejected_verdict_label`.

**Research context:**

- Caller check for Decision Record item 9 (2026-10-08, gcode): `bind_evidence_run` is called by `src/gobby/dispatch/spawn.py` (deleted in 2.5) and named by `references/plan/review.md` and `plan-adversary.yaml`'s `blocked_mcp_tools`. `prepare_plan_review_round` is also called by the dispatcher, but the static-seat round needs it. The bundled agent contract test checks `blocked_mcp_tools` only inside step workflows, so the top-level blocked entry is removed here because it names a removed tool, not because a test fails.
- Service-level tests keep calling `PlanReviewEvidenceService.bind_evidence_run` to build run-bound rows (`tests/plans/`, `tests/review_learning/test_round_diff.py`, `tests/storage/stage_review_helpers.py`, `tests/skills/test_review_learning_skill.py::_bind_run`). They stay unchanged, as Decision Record item 9 keeps the service.
- No stage registry check resolves a default agent name to a definition: the `planning` row's `default_agent: planner` stays valid as a string until 4.2 removes the registry.
- Tests that use these names as arbitrary agent types and load no definition stay: `tests/agents/test_backend_ingress.py`, `test_lifecycle_monitor.py`, `test_lifecycle_monitor_extra.py`, `test_spawn_executor.py`, `tests/agents/watchdog/test_completed_turn_mcp_gate.py`, `tests/mcp_proxy/tools/spawn_agent/test_spawn_guards.py`, `tests/mcp_proxy/tools/test_agent_live_stats.py`, `test_apply_persona.py`, `tests/servers/websocket/chat/test_servers_websocket_chat_session.py`, `tests/workflows/test_agent_definitions_v2.py`, `test_agent_models.py`, `test_plan_mode_rules.py`, `test_seat_rules.py`, `test_step_snapshot_semantics.py` and the two `_agent_type` cases in `test_step_enforcement.py`.
- `sync_bundled_agents` and `sync_bundled_rules` soft-delete the installed rows of deleted definitions and rules at the next sync.
- Docs that name these definitions (`docs/contracts/plan-coverage.md`, `docs/guides/plans-and-plan-mode.md`, `cli-commands.md`, `dispatch.md`, `http-endpoints.md`, `task-expansion.md`) change in 5.2.
- Planned checks: focused pytest on every edited test Target plus `tests/agents/test_bundled_agent_contract.py`, `tests/skills/test_reference_library.py`, `tests/plans/` and `tests/agents/test_plan_seat_definitions.py`; ruff, format check and mypy on `src/gobby/mcp_proxy/tools/plans/review_evidence.py`.

**Acceptance:**

- 3.2.1 - No bundled definition named `planner`, `plan-adversary-old`, `plan-adversary-taskless-old`, `plan-enhancer-old` or `plan-enhancer-taskless-old` exists. test: `tests/workflows/test_retired_bundled_definitions.py::test_retired_agent_yaml_is_absent_from_active_and_deprecated_bundles`.
- 3.2.2 - The planner and plan-enhancer lesson rules are gone, and the plan memory guard names only `plan-adversary`. test: `tests/workflows/test_retired_bundled_definitions.py::test_retired_rules_are_absent_from_bundled_templates`. test: `tests/workflows/test_memory_lifecycle_rules.py::TestGuardPlanMemoryWrites::test_condition_covers_planning_contexts_without_recall_gate`.
- 3.2.3 - gobby-plans no longer registers `bind_evidence_run`, and its other review-evidence tools keep their schemas. test: `tests/mcp_proxy/test_plans_tools.py::test_plan_tool_schemas_and_happy_path`.
- 3.2.4 - The review guidance opens a static-seat round with `prepare_plan_review_round`, names no taskless reviewer, and teaches no stage verdict or spawned-run delivery. behavior: "plan-adversary-taskless-old" absent from `src/gobby/install/shared/skills/gobby/references/plan/review.md`. behavior: "approve_review" absent from `src/gobby/install/shared/skills/gobby/references/plan/review.md`. behavior: "end_agent_run" absent from `src/gobby/install/shared/skills/gobby/references/plan/review.md`. test: `tests/skills/test_review_learning_skill.py::test_plan_skill_documents_parallel_review_contract`.
- 3.2.5 - The reference-library contract and the bundled agent contract pass. test: `tests/skills/test_reference_library.py::test_reference_contract_3_2_1`. test: `tests/agents/test_bundled_agent_contract.py::test_bundled_agent_mcp_references_match_registered_tool_inventory`.

### 3.3 Discovery and review stage definitions retire (depends: 3.2) [category: code]
`kind: deliverable`

Targets:
- `src/gobby/install/shared/workflows/agents/analyst.yaml::*` — operation: delete — scope-reason: Decision Record item 4; only the dispatcher's ideation stage spawned it
- `src/gobby/install/shared/workflows/agents/architect.yaml::*` — operation: delete — scope-reason: Decision Record item 4; only the dispatcher's architecture stage spawned it
- `src/gobby/install/shared/workflows/agents/product-manager.yaml::*` — operation: delete — scope-reason: Decision Record item 4; only the dispatcher's prd stage spawned it
- `src/gobby/install/shared/workflows/agents/expansion-qa.yaml::*` — operation: delete — scope-reason: Decision Record item 4; only the dispatcher's expansion review spawned it
- `src/gobby/install/shared/workflows/agents/qa-reviewer.yaml::*` — operation: delete — scope-reason: Decision Record item 4; only the dispatcher's development review spawned it
- `src/gobby/install/shared/workflows/agents/doc-reviewer.yaml::*` — operation: delete — scope-reason: Decision Record item 4; only the dispatcher's docs review spawned it
- `src/gobby/install/shared/workflows/agents/trajectory-monitor.yaml::*` — operation: delete — scope-reason: Decision Record item 4; only the dispatcher's pr review spawned it
- `src/gobby/install/shared/workflows/agents/deprecated/qa-dev.yaml::*` — operation: delete — scope-reason: Decision Record item 4; its only verdict is a development-stage approval
- `src/gobby/install/shared/workflows/rules/review-learning/inject-qa-reviewer-lessons.yaml::*` — operation: delete — scope-reason: scoped only to the deleted qa-reviewer
- `src/gobby/install/shared/workflows/rules/worker-safety/no-push-for-workers.yaml::*` — scope-reason: scope only developer
- `src/gobby/agents/sync.py::*` — scope-reason: the discovery repair sets keep only researcher
- `src/gobby/mcp_proxy/tools/tasks/_artifacts.py::*` — scope-reason: tool descriptions stop naming deleted agents
- `src/gobby/mcp_proxy/tools/tasks/_ops_factory.py::*` — scope-reason: the artifact registry comment stops naming deleted agents
- `src/gobby/tasks/expansion/_deferrals.py::*` — scope-reason: the docstring names plan coverage instead of expansion-qa
- `src/gobby/install/shared/workflows/pipelines/expand-task.yaml::*` — scope-reason: the description names plan coverage instead of expansion-qa
- `tests/agents/test_discovery_agents.py::*` — operation: delete — scope-reason: after 3.2 every remaining agent case pins analyst, architect or product-manager
- `tests/skills/test_discovery_methodology_skills.py`
- `tests/agents/test_doc_reviewer_definition.py::*` — operation: delete — scope-reason: pins only doc-reviewer
- `tests/agents/test_qa_reviewer_definition.py::*` — operation: delete — scope-reason: pins only qa-reviewer
- `tests/agents/test_trajectory_monitor_definition.py::*` — operation: delete — scope-reason: pins only trajectory-monitor
- `tests/workflows/test_expansion_qa_coverage_call.py::*` — operation: delete — scope-reason: loads expansion-qa at module level
- `tests/storage/tasks/test_stage_registry_default_agent_fk.py::*` — operation: delete — scope-reason: syncs the bundle and asserts the three discovery definitions are enabled
- `tests/agents/test_test_architect_definition.py::*` — scope-reason: delete the architect definition test
- `tests/agents/test_agents_sync.py::*` — scope-reason: the repair tests use researcher, and the real-bundle sync drops five names
- `tests/workflows/test_block_update_task_rule.py::*` — scope-reason: the task-editor list loses the discovery definitions
- `tests/workflows/test_agent_workflow_completion.py::*` — scope-reason: delete the expansion-qa and qa-reviewer workflow cases
- `tests/skills/test_epic_review_skill.py::*` — scope-reason: stop reading qa-reviewer
- `tests/skills/test_review_learning_skill.py::*` — scope-reason: drop the qa-reviewer and trajectory-monitor producers
- `tests/workflows/test_review_learning_rules.py::*` — scope-reason: drop the qa-reviewer lesson rule
- `tests/workflows/test_worker_safety_rules.py::*` — scope-reason: the push guard scopes only developer
- `tests/workflows/test_workflows_agent_definitions.py::*` — scope-reason: drop the deleted definitions' rows and tests
- `tests/workflows/test_retired_bundled_definitions.py::*` — scope-reason: list the retired definitions and rule

**Granularity:** one outcome, no bundled definition, rule or tool description names a discovery or review definition that only the dispatcher spawned. The eight definitions go together because their tests share files, and the rule and description edits only remove names the deletions leave dangling. Most Targets are whole-file deletions.

Delete the eight definitions. After 2.5 no code spawns them, and `researcher`, `epic-reviewer`, `tech-writer` and `task-close-reviewer` stay. Git drops the empty `agents/deprecated/` directory; the rule sync and the template hash code only skip that path segment and do not require it.

Rules: delete `inject-qa-reviewer-lessons`. `no-push-for-workers` keeps `agent_scope: [developer]`, and its description becomes "Block git push for developer agents". The reviewer terminal-verdict rule keeps naming `qa-reviewer` and `doc-reviewer` until 3.4 deletes it; its test builds variables directly and loads neither definition.

Source text:

- `src/gobby/agents/sync.py`: `_DISCOVERY_PLACEHOLDER_AGENTS` and `_PRE_MARKER_SWEPT_AGENTS` keep only `"researcher"`. Both sets act only on rows whose template is still bundled, so the three deleted names are dead entries.
- `_artifacts.py`: the module docstring's second sentence becomes "These tools record task artifact pointers and append audit sections." The registry description and the five tool descriptions drop "for merge, expansion-qa, and epic-reviewer agents" (or its wrapped equivalent) and keep the rest of each sentence. `_ops_factory.py`'s comment becomes "# Merge artifact mutation tools".
- `_deferrals.py`: "expansion-qa reports a dangling one as ``task_missing``" becomes "plan coverage reports a dangling one as ``task_missing``" (`src/gobby/plans/deferral.py` emits that code).
- `expand-task.yaml`: step 6 becomes "Run plan coverage for every".

Tests:

- Delete the six test files above. First move `test_discovery_methodology_skill_exists`, the only case in `tests/agents/test_discovery_agents.py` that reads no definition, into the new `tests/skills/test_discovery_methodology_skills.py`. The new module keeps `SKILLS_DIR` and `_skill_text`, parametrizes over the skill and section pairs `ideate` and "Discovery Brief", `architecture` and "Architecture Brief", and `prd` and "Product Reference Document", and keeps the four assertions on `name:`, `internal: true`, the section heading and "methodology".
- `test_test_architect_definition.py`: delete `test_architect_loads_test_architecture_methodology`, the `_agent` and `_step` helpers and the imports only they use. The removed-standalone check and the architecture skill test stay.
- `test_agents_sync.py`: `test_sync_enables_legacy_discovery_placeholder` writes and checks a `researcher` placeholder instead of `analyst`. `test_sync_repairs_agents_swept_before_the_origin_marker` uses `names = ("researcher",)`. `test_sync_with_real_bundled_agents` drops `qa-reviewer`, `doc-reviewer`, `analyst`, `architect` and `product-manager` from its expected names.
- `test_block_update_task_rule.py`: `TASK_EDITORS` becomes `("orchestrator", "assistant")`. Delete `test_discovery_definitions_write_their_marker_block` and `MARKER_BLOCK`, which only it uses.
- `test_agent_workflow_completion.py`: delete `test_expansion_qa_verdict_terminalizes_generated_step_workflow`, `test_qa_reviewer_stale_get_task_result_transitions_to_terminate`, `_register_expansion_qa_workflow` and `_register_qa_reviewer_workflow`. `_register_bundled_agent_workflow` keeps its other callers.
- `test_epic_review_skill.py`: `test_two_class_epic_recording` drops the `qa-reviewer.yaml` read and checks only the skill and `epic-reviewer.yaml`.
- `test_review_learning_skill.py`: `test_review_producers_do_not_reference_review_learning` drops the `qa-reviewer` and `trajectory-monitor` producers.
- `test_review_learning_rules.py`: `CLASS_INJECTION_RULE_FILES` drops `inject-qa-reviewer-lessons.yaml`. `test_class_injection_agent_scoping` drops the `inject-qa-reviewer-lessons` entry, the dispatcher's `qa-miss` early return and the `qa-miss` branch of the context assertion, so every remaining scoped agent asserts `<review-guidance>`.
- `test_worker_safety_rules.py`: `test_agent_scope_persists_through_sync` expects `["developer"]`.
- `test_workflows_agent_definitions.py`: `test_build_smoke_agent_runtime_mappings` drops the `analyst`, `architect`, `product-manager`, `qa-reviewer` and `doc-reviewer` rows. `test_claim_guidance_accounts_for_spawn_preclaim` drops `analyst`, `architect`, `product-manager` and `trajectory-monitor` from `agents_with_updated_claim_steps`. Delete `test_architect_requires_architecture_and_test_architecture_sections` and `test_qa_reviewer_records_review_verdict_without_closing_task`. `test_qa_and_epic_reviewers_check_tdd_required_evidence` becomes `test_epic_reviewer_checks_tdd_required_evidence`: it drops the `qa-reviewer` read, the two `qa` texts from its loop and the two `qa`-only assertions.
- `test_retired_bundled_definitions.py`: add the eight names to `RETIRED_AGENTS` (its test checks both the active and the `deprecated/` path) and `inject-qa-reviewer-lessons` to `RETIRED_RULES`.

**Research context:**

- Source names left after this leaf: `src/gobby/install/shared/registry/stages.yaml` names five of these definitions as stage defaults and reviewers, and 4.2 deletes it; no registry check resolves those strings to definitions. `src/gobby/dispatch/` is gone in 2.5. The `ideate`, `architecture`, `prd` and `proportionality` skills name the deleted definitions in their framing, and 5.1 rewords them per Decision Record item 4. `docs/reference-audit/build.json` holds a dated installed-row observation, which is evidence and stays.
- Tests that use these names as arbitrary agent types, registry names or selector strings and load no definition stay: `tests/agents/test_lifecycle_monitor_watchdog_diagnostics.py`, `test_lifecycle_monitor_watchdog_idle_recovery.py`, `test_resume_metadata.py`, `test_spawn_executor.py`, `tests/cli/test_agents_steps.py`, `tests/hooks/test_agent_events_coverage.py`, `test_session_activation_reconciliation.py` (it creates its own `qa-reviewer` row), `tests/mcp_proxy/services/test_direct_tool_session_activation.py`, `tests/mcp_proxy/tools/test_agent_live_stats.py`, `test_apply_persona.py`, `test_memory_tools.py`, `test_spawn_agent_impl_provider.py`, `tests/review_learning/test_feedback_loop_e2e.py`, `tests/storage/tasks/test_review_tools_call_site_audit.py`, `tests/tasks/test_expansion_qa_coverage.py`, `tests/workflows/test_step_context.py`, `test_plan_mode_rules.py`, `test_task_enforcement_rules.py`, `test_reviewer_terminal_verdict_rules.py` and `web/src/lib/__tests__/taskNormalization.test.ts`.
- Owned elsewhere: `tests/mcp_proxy/tools/tasks/test_record_pr_verdict.py` and `test_stage_tools_registered.py` go with the stage tools in 3.5; the stage registry and stage state tests go in P4; `tests/e2e/test_build_dispatcher_autonomy.py` and the dispatcher case in `tests/workflows/test_agent_workflow_runtime_cleanup.py` go in 2.5.
- `sync_bundled_agents` and `sync_bundled_rules` soft-delete the installed rows at the next sync.
- Planned checks: focused pytest on every edited test Target plus `tests/agents/test_bundled_agent_contract.py`, `tests/skills/test_reference_library.py` and `tests/mcp_proxy/tools/test_tasks_ops_artifacts.py`; ruff, format check and mypy on the four edited Python sources.

**Acceptance:**

- 3.3.1 - No bundled definition named `analyst`, `architect`, `product-manager`, `expansion-qa`, `qa-reviewer`, `doc-reviewer`, `trajectory-monitor` or `qa-dev` exists, in the active or the deprecated bundle. test: `tests/workflows/test_retired_bundled_definitions.py::test_retired_agent_yaml_is_absent_from_active_and_deprecated_bundles`.
- 3.3.2 - The qa-reviewer lesson rule is gone, and the push guard scopes only `developer`. test: `tests/workflows/test_retired_bundled_definitions.py::test_retired_rules_are_absent_from_bundled_templates`. test: `tests/workflows/test_worker_safety_rules.py::TestWorkerSafetySync::test_agent_scope_persists_through_sync`.
- 3.3.3 - Bundled sync installs the surviving definitions and repairs only `researcher` placeholders. test: `tests/agents/test_agents_sync.py::TestSyncBundledAgents::test_sync_with_real_bundled_agents`. test: `tests/agents/test_agents_sync.py::TestSyncBundledAgents::test_sync_enables_legacy_discovery_placeholder`.
- 3.3.4 - The bundled agent contract passes without the eight definitions. test: `tests/agents/test_bundled_agent_contract.py::test_bundled_agent_mcp_references_match_registered_tool_inventory`.
- 3.3.5 - The discovery methodology skills keep their contract test after their definitions go. test: `tests/skills/test_discovery_methodology_skills.py::test_discovery_methodology_skill_exists`.

### 3.4 Kept definitions drop the stage tools (depends: 3.3) [category: code]
`kind: deliverable`

Targets:
- `src/gobby/install/shared/workflows/agents/epic-reviewer.yaml::*` — scope-reason: Decision Record item 13; one review path for open and closed epics
- `src/gobby/install/shared/workflows/review.yaml::*` — scope-reason: the review pipeline prompt describes the single path
- `src/gobby/install/shared/skills/gobby/references/review/epic.md::*` — scope-reason: drop the stage discovery and stage-transition wording
- `src/gobby/install/shared/skills/gobby/references/review/outcomes.md::*` — scope-reason: the verdict mapping uses the handoff, remediation tasks and escalation
- `src/gobby/install/shared/workflows/rules/reviewer-lifecycle/terminal-verdict-after-validation.yaml::*` — operation: delete — scope-reason: Decision Record item 13; all three scoped reviewers are gone or no longer call a verdict tool
- `src/gobby/install/shared/workflows/rules/AGENTS.md::*` — scope-reason: the group table drops the deleted groups and recounts review-learning
- `src/gobby/install/shared/workflows/agents/tech-writer.yaml::*` — scope-reason: the docs leaf closes through close_task only
- `src/gobby/install/shared/workflows/agents/plan-writer.yaml::*` — scope-reason: drop blocked stage tools
- `src/gobby/install/shared/workflows/agents/plan-enhancer.yaml::*` — scope-reason: drop blocked stage tools
- `src/gobby/install/shared/workflows/agents/plan-adversary.yaml::*` — scope-reason: drop blocked stage tools
- `src/gobby/install/shared/workflows/agents/task-close-reviewer.yaml::*` — scope-reason: drop blocked stage tools
- `tests/workflows/test_reviewer_terminal_verdict_rules.py::*` — operation: delete — scope-reason: tests only the deleted rule
- `tests/agents/test_epic_reviewer_definition.py::*` — scope-reason: pin the single review path
- `tests/agents/test_tech_writer_definition.py::*` — scope-reason: the handoff completes on close_task only
- `tests/workflows/test_workflows_agent_definitions.py::*` — scope-reason: the epic-reviewer, review-skill and tech-writer tests stop pinning stage tools
- `tests/workflows/test_agent_workflow_completion.py::*` — scope-reason: delete the epic_qa completion case
- `tests/skills/test_review_skill.py::*` — scope-reason: pin the new verdict mapping
- `tests/workflows/test_close_validator_allowlist.py::*` — scope-reason: the close reviewer no longer blocks the stage tools
- `tests/workflows/test_retired_bundled_definitions.py::*` — scope-reason: list the three retired verdict rules

**Granularity:** one outcome, no kept definition or review guidance calls, blocks or describes a stage tool, so 3.5 can unregister them. The bundled agent contract fails on any step-level blocked tool that is not registered, so every kept definition changes before 3.5, and the terminal-verdict rule goes because its last spawned reviewer stops calling a verdict tool here.

`epic-reviewer.yaml` (Decision Record item 13):

- `description` becomes "Epic-level reviewer. Reviews the approved plan, aggregate implementation diff, and child validation evidence of an open or closed epic."
- In `prompts.agent`, the "Closed-epic review:" paragraph becomes a "Verdict:" paragraph: "Open and closed epics follow one review path. Deliver the `## Epic Findings` verdict block in the end_agent_run handoff. For blocking findings, create remediation tasks under the epic, or reopen a closed epic with reopen_task when its delivered work must be reworked. Escalate with a reason beginning `needs_human:` when human judgment is required. Then terminate with end_agent_run."
- The Workflow bullets from "If the implementation is correct, call complete_stage" through "Terminate only by calling end_agent_run." become: "If the implementation is correct, deliver an approve verdict block in the end_agent_run handoff.", "If changes are needed, create a remediation task under the epic for every blocking finding, citing its subtasks, and deliver a request_changes verdict block.", "If human judgment is required, escalate with a reason beginning `needs_human:`." and "Terminate only by calling end_agent_run."
- The Boundaries bullet "Your verdict tools are complete_stage, fail_stage, and escalate_task. ..." becomes "Your verdict is the `## Epic Findings` block, remediation tasks and escalate_task. Never call close_task or de_escalate_task; the `/gobby review` caller acts on the verdict." The bullet "Terminate with end_agent_run in the terminate step" becomes "Terminate with end_agent_run from the review step".
- `step_workflow`: delete `exit_condition` and the `review_complete` variable. The `claim` step keeps its tools, hooks and transition; its status message ends "skip the claim and continue to review." `load_skill` has one transition, `to: review` when `all(skill_loaded(skill) for skill in vars.required_skills)`. Delete the `closed_review` and `terminate` steps.
- The `review` step keeps its first ten status lines (plan, diff, subtree and TDD evidence). The last three lines become "Deliver the gobby:references/review/epic.md verdict block in the end_agent_run handoff. For blocking findings, create remediation tasks under the epic, or call reopen_task on a closed epic whose delivered work must be reworked. Escalate with a `needs_human:` reason when human judgment is required. Finish by calling end_agent_run." Its `blocked_mcp_tools` become `gobby-tasks:close_task`, `gobby-tasks:de_escalate_task`, `gobby-agents:spawn_agent` and `gobby-agents:kill_agent`. Delete its `on_mcp_success` and `transitions`.

Review guidance:

- `review.yaml`: the description's last clause "independent of gobby build" goes. The prompt's last four lines become "Deliver the Epic Findings verdict in your end_agent_run handoff. File remediation tasks under the epic for blocking findings, or reopen a closed epic whose delivered work must be reworked."
- `references/review/epic.md`, State and recovery: the first paragraph becomes "An open epic review must respect its owner. If another session owns the work, coordinate through `gobby-agents:send_message` instead of taking its claim." The second paragraph becomes "Open and closed epics share one review path. Skip claiming a closed epic. Deliver the structured findings; blocking findings require remediation tasks under the epic or an explicit reopening of a closed epic. Never call `close_task` from epic review. Closure and delivery belong to the owning lifecycle flow."
- `references/review/outcomes.md`: line 4 becomes "Read the epic's task state first; load [evidence](evidence.md)." The paragraphs from "For an open epic whose `epic_qa` stage is in progress" through "Review never closes the epic itself." become: "Open and closed epics follow one path. The reviewer delivers the block in its `end_agent_run` handoff and maps the verdict:", the bullets "Approve → the block is the whole verdict.", "Request changes → create a remediation task under the epic for every blocking finding, citing the blocking descendant. On a closed epic whose delivered work must be reworked, `gobby-tasks:reopen_task` replaces the remediation tasks." and "Needs discussion → `gobby-tasks:escalate_task` with a `needs_human:` reason naming the concrete decision.", then "A delegated reviewer owns its verdict and terminates through its agent workflow; the launcher does not repeat it. Review never closes the epic itself, and the `/gobby review` caller acts on the verdict."

Rules: delete `terminal-verdict-after-validation.yaml` (rules `reviewer-terminal-verdict-track-successful-validation`, `reviewer-terminal-verdict-clear-on-verdict` and `reviewer-terminal-verdict-block-turn-end`). In `src/gobby/install/shared/workflows/rules/AGENTS.md`, delete the `build-coordinator` row (2.3 deleted that group) and the `reviewer-lifecycle` row, and set the `review-learning` count to 2 (3.2 and 3.3 deleted three of its five rules).

Other kept definitions:

- `tech-writer.yaml`: the Handoff paragraph drops "Inspect the assigned task's stage manifest. If the current development row has no review gate," (the paragraph starts "Commit the documentation changes, call close_task with preview=true") and drops its last sentence, "If review is enabled, commit the changes and call submit_for_review ...". The `implement` status message becomes "Implement the docs leaf. Commit changes, then call close_task with preview=true, commit_sha, and changes_summary, repair deterministic blockers, then repeat with preview=false. Wait once on reviewer_run_id when review is required, then read the task after delivery." followed by its unchanged parent-message lines. Delete the `submit_for_review` hook from `implement.on_mcp_success`.
- `plan-writer.yaml`, `plan-enhancer.yaml`, `plan-adversary.yaml` and `task-close-reviewer.yaml`: delete every `gobby-tasks-ops:submit_for_review`, `approve_review`, `reject_review`, `complete_stage`, `fail_stage` and `record_plan_enhancement` entry from their top-level and step-level `blocked_mcp_tools`. Nothing else changes.

Tests:

- Delete `test_reviewer_terminal_verdict_rules.py`.
- `test_epic_reviewer_definition.py`: `test_three_outcomes` asserts the `review` step has no `on_mcp_success` and the agent prompt contains `## Epic Findings`, "remediation tasks" and "`needs_human:`". `test_success_path_uses_complete_stage_for_in_progress_epic_qa` becomes `test_review_step_ends_the_run_without_stage_tools`: the step names are `claim`, `load_skill` and `review`; `step_workflow` has no `exit_condition`; the `review` blocked tools equal the four above; and "complete_stage", "fail_stage", "epic_qa" and "terminal-verdict" appear in neither the prompt nor any step status message. `test_loads_required_skills_before_review` expects the single `review` transition. `test_closed_epic_routes_to_post_hoc_review_with_reopen_permission` becomes `test_closed_epic_reviews_on_the_review_step_with_reopen_permission` and reads the `review` step instead of `closed_review`.
- `test_workflows_agent_definitions.py`: `test_epic_reviewer_loads_skill_reads_files_and_terminates_cleanly` stops reading the `terminate` step, expects `{"gobby-tasks:close_task", "gobby-tasks:de_escalate_task"}` as a subset of the `review` blocked tools and asserts `gobby-agents:end_agent_run` is not blocked there. `test_epic_review_skill_allows_docs_epic_plan_substitute` replaces its five stage assertions with `"epic_qa" not in skill_text` and `"complete_stage" not in skill_text`. `test_tech_writer_loads_methodology_skill_after_claim` asserts `"close_task" in implement["status_message"]` and `"submit_for_review" not in implement["status_message"]`.
- `test_agent_workflow_completion.py`: delete `test_epic_review_complete_stage_success_transitions_to_terminate`.
- `test_review_skill.py`: `test_epic_review_references_pin_routing_and_verdict_mapping` replaces its four outcome terms with "Approve → the block is the whole verdict", "Request changes → create a remediation task under the epic" and "Needs discussion → `gobby-tasks:escalate_task` with a `needs_human:` reason", and asserts "complete_stage" is absent from `outcomes`.
- `test_tech_writer_definition.py`: `test_handoff_transitions_to_end_agent_run_termination` expects `{"gobby-tasks:close_task"}` as a subset of the success tools.
- `test_close_validator_allowlist.py`: `LIFECYCLE_MUTATION_TOOLS` drops the five `gobby-tasks-ops` entries.
- `test_retired_bundled_definitions.py`: add the three verdict rules to `RETIRED_RULES`.

**Research context:**

- The terminal-verdict rule's test builds its variables directly and loads no definition, so it stayed valid through 3.3. Its only other mentions are the three definitions this phase changes or deletes and `docs/reviews/hooks.md`, which is history.
- Spawn claims an open, unclaimed epic for the child (`src/gobby/mcp_proxy/tools/spawn_agent/_execution.py`), and the claim step's `_claim_step_requires_task` guard in `_spawn_guards.py` still refuses a task-less spawn, so the claim step stays. `review.yaml` already spawns with `allow_closed_task: true`.
- The dry-run's dead-end-step warning on `review` matches the warning `closed_review` raises today; it is a warning, not a failure.
- `references/review/evidence.md` and `references/review/overview.md` keep generic stage wording that 5.1 rewrites with `references/tasks/reviews.md`.
- Synthetic step workflows in `test_agent_workflow_completion.py` that name `approve_review` as a test tool stay; they register their own workflow and need no registered tool.
- Planned checks: focused pytest on every edited test Target plus `tests/agents/test_bundled_agent_contract.py`, `tests/agents/test_plan_seat_definitions.py`, `tests/workflows/test_seat_definitions.py`, `tests/skills/test_epic_review_skill.py` and `tests/skills/test_reference_library.py`.

**Acceptance:**

- 3.4.1 - `epic-reviewer` reviews open and closed epics on one `review` step that blocks no stage tool, and its run ends with `end_agent_run`. test: `tests/agents/test_epic_reviewer_definition.py::test_review_step_ends_the_run_without_stage_tools`. test: `tests/agents/test_epic_reviewer_definition.py::test_closed_epic_reviews_on_the_review_step_with_reopen_permission`.
- 3.4.2 - The verdict mapping names the handoff, remediation tasks and escalation, and no stage tool. test: `tests/skills/test_review_skill.py::test_epic_review_references_pin_routing_and_verdict_mapping`.
- 3.4.3 - The reviewer terminal-verdict rules are gone. test: `tests/workflows/test_retired_bundled_definitions.py::test_retired_rules_are_absent_from_bundled_templates`.
- 3.4.4 - `tech-writer` hands off through `close_task` only. test: `tests/agents/test_tech_writer_definition.py::test_handoff_transitions_to_end_agent_run_termination`.
- 3.4.5 - No bundled definition names a stage tool. behavior: "complete_stage", "fail_stage", "submit_for_review", "approve_review", "reject_review" and "record_plan_enhancement" absent from `src/gobby/install/shared/workflows/agents/`. test: `tests/agents/test_bundled_agent_contract.py::test_bundled_agent_mcp_references_match_registered_tool_inventory`.

### 3.5 Stage tools, review transitions, stage CLI and stage routes go (depends: 3.4) [category: code]
`kind: deliverable`

Targets:
- `src/gobby/mcp_proxy/tools/tasks/_stage_ops.py::*` — operation: delete — scope-reason: registers the stage manifest and verdict tools, `record_pr_verdict`, `record_merge_result` and `backfill_plan_review_lessons`
- `src/gobby/mcp_proxy/tools/tasks/_stage_review.py::*` — operation: delete — scope-reason: registers the review transition tools and `record_plan_enhancement`
- `src/gobby/mcp_proxy/tools/tasks/_stage_read.py::*` — operation: delete — scope-reason: registers the read-only stage tools
- `src/gobby/mcp_proxy/tools/tasks/_stage_registry_ops.py::*` — operation: delete — scope-reason: registers the stage registry configuration tools
- `src/gobby/mcp_proxy/tools/tasks/_dispatch_mutex_release.py::*` — operation: delete — scope-reason: only the deleted stage and review tools import it
- `src/gobby/mcp_proxy/tools/tasks/_plan_review_backfill.py::*` — operation: delete — scope-reason: re-runs the approval lesson mint that only `approve_review` performs
- `src/gobby/mcp_proxy/tools/tasks/_plan_review_approval.py::*` — operation: delete — scope-reason: the approval lesson mint that only `approve_review` calls
- `src/gobby/mcp_proxy/tools/tasks/_ops_factory.py::*` — scope-reason: drop the stage ops and stage registry merges
- `src/gobby/mcp_proxy/tools/tasks/_factory.py::*` — scope-reason: drop the stage read merge
- `src/gobby/servers/routes/stages.py::*` — operation: delete — scope-reason: the `/api/stages` registry and task-type default routes
- `src/gobby/servers/routes/tasks_stage_routes.py::*` — operation: delete — scope-reason: the per-task stage read and PATCH routes
- `src/gobby/servers/routes/__init__.py::*` — scope-reason: drop the stages router export
- `src/gobby/servers/_app_routes.py::*` — scope-reason: stop including the stages router
- `src/gobby/servers/routes/tasks.py::*` — scope-reason: stop registering the per-task stage routes
- `src/gobby/cli/stages.py::*` — operation: delete — scope-reason: the `gobby stages` command group
- `src/gobby/cli/tasks/stages.py::*` — operation: delete — scope-reason: the `gobby tasks stages` and `gobby tasks advance` commands
- `src/gobby/cli/tasks/review.py::*` — operation: delete — scope-reason: the `gobby tasks review` command
- `src/gobby/cli/__init__.py::*` — scope-reason: drop the `stages` lazy command entry
- `src/gobby/cli/tasks/main.py::*` — scope-reason: drop the stages, advance and review registrations
- `src/gobby/workflows/enforcement/blocking.py::*` — scope-reason: the task-ops mutation set drops the three review tools
- `src/gobby/workflows/hooks.py::*` — operation: delete-lines — base-blob: 19638420d44a56d4b023d953f46804de2c3484ef — lines: 507-509 — scope-reason: the commit-gate tool set drops the three review tools
- `src/gobby/storage/tasks/_updates.py::*` — scope-reason: the update_task refusal hints name only surviving tools
- `src/gobby/tasks/expansion_qa_coverage.py::*` — scope-reason: coverage results drop the stage review action
- `src/gobby/mcp_proxy/tools/tasks/_expansion_registry.py::*` — operation: delete-lines — base-blob: 44055d98e20864e941d4d0f62fcea65521bfe9f2 — lines: 222-238, 307, 349, 412-419, 759 — scope-reason: drop the spawned-caller probe and the internal stage pipeline flag
- `src/gobby/install/shared/workflows/rules/task-enforcement/block-needs-review-interactive.yaml::*` — operation: delete — scope-reason: its only rule blocks the three review tools for interactive sessions
- `src/gobby/install/shared/workflows/rules/task-enforcement/require-commit-before-status.yaml::*` — scope-reason: drop the three review tools
- `src/gobby/install/shared/workflows/rules/monolith-enforcement/require-same-session-decomposition.yaml::*` — scope-reason: the task-transition rule drops the three review tools
- `src/gobby/install/shared/workflows/rules/AGENTS.md::*` — scope-reason: the task-enforcement rule count drops to 19
- `src/gobby/install/shared/skills/gobby/references/plan/enhancement.md::*` — scope-reason: the autonomous enhancer no longer records through `record_plan_enhancement`
- `docs/reference-audit/tasks.json::*` — scope-reason: drop the stage tool, stage CLI and backfill entries
- `docs/reference-audit/plan.json::*` — scope-reason: drop the `record_plan_enhancement` and backfill entries and the delegated review tools
- `docs/reference-audit/review.json::*` — scope-reason: drop the five stage verdict and review transition entries
- `docs/reference-audit/source-control.json::*` — scope-reason: drop the `record_merge_result` and `record_pr_verdict` entries
- `docs/reference-audit/integrations.json::*` — scope-reason: the shared surfaces drop `record_merge_result`
- `tests/mcp_proxy/tools/tasks/test_add_stage.py::*` — operation: delete — scope-reason: tests a deleted tool
- `tests/mcp_proxy/tools/tasks/test_complete_stage.py::*` — operation: delete — scope-reason: tests a deleted tool
- `tests/mcp_proxy/tools/tasks/test_fail_stage.py::*` — operation: delete — scope-reason: tests a deleted tool
- `tests/mcp_proxy/tools/tasks/test_get_task_stages.py::*` — operation: delete — scope-reason: tests a deleted tool
- `tests/mcp_proxy/tools/tasks/test_get_task_type_defaults.py::*` — operation: delete — scope-reason: tests a deleted tool
- `tests/mcp_proxy/tools/tasks/test_list_stages_registry.py::*` — operation: delete — scope-reason: tests a deleted tool
- `tests/mcp_proxy/tools/tasks/test_lifecycle_tools.py::*` — operation: delete — scope-reason: both cases drive the deleted stage ops registry
- `tests/mcp_proxy/tools/tasks/test_record_merge_result.py::*` — operation: delete — scope-reason: tests a deleted tool
- `tests/mcp_proxy/tools/tasks/test_record_merge_result_stub.py::*` — operation: delete — scope-reason: tests a deleted tool
- `tests/mcp_proxy/tools/tasks/test_record_pr_verdict.py::*` — operation: delete — scope-reason: tests a deleted tool
- `tests/mcp_proxy/tools/tasks/test_remove_stage.py::*` — operation: delete — scope-reason: tests a deleted tool
- `tests/mcp_proxy/tools/tasks/test_review_tools_server_placement.py::*` — operation: delete — scope-reason: pins the deleted review tools to gobby-tasks-ops
- `tests/mcp_proxy/tools/tasks/test_stage_tools.py::*` — operation: delete — scope-reason: tests the deleted stage tools
- `tests/mcp_proxy/tools/tasks/test_stage_tools_registered.py::*` — operation: delete — scope-reason: tests the deleted stage tools
- `tests/mcp_proxy/tools/tasks/test_stage_tools_server_placement.py::*` — operation: delete — scope-reason: pins the deleted stage tools to their servers
- `tests/mcp_proxy/tools/tasks/test_start_stage.py::*` — operation: delete — scope-reason: tests a deleted tool
- `tests/mcp_proxy/tools/test_approve_review.py::*` — operation: delete — scope-reason: tests a deleted tool
- `tests/mcp_proxy/tools/test_reject_review.py::*` — operation: delete — scope-reason: tests a deleted tool
- `tests/mcp_proxy/tools/test_record_plan_enhancement.py::*` — operation: delete — scope-reason: tests a deleted tool
- `tests/mcp_proxy/tools/test_plan_review_backfill.py::*` — operation: delete — scope-reason: tests a deleted tool
- `tests/mcp_proxy/tools/test_plan_review_approval.py::*` — operation: delete — scope-reason: tests the deleted approval mint
- `tests/mcp_proxy/test_stage_review_schema.py::*` — operation: delete — scope-reason: after 3.2 it pins only the deleted `reject_review` schema
- `tests/cli/test_tasks_review_command.py::*` — operation: delete — scope-reason: tests the deleted command
- `tests/cli/test_tasks_advance_command.py::*` — operation: delete — scope-reason: its contract requires the deleted stages command module
- `tests/cli/test_tasks_stages_command.py::*` — operation: delete — scope-reason: its contract requires the deleted stages command module
- `tests/cli/test_stage_cli_monolith_compliance.py::*` — operation: delete — scope-reason: its contract requires the deleted stage and review command modules
- `tests/servers/websocket/test_stage_broadcast.py::*` — operation: delete — scope-reason: drives the deleted per-task stage PATCH route
- `tests/servers/routes/test_stage_routes.py::*` — scope-reason: delete the registry and PATCH route cases
- `tests/servers/routes/test_tasks_routes.py::*` — scope-reason: delete the three review transition route cases
- `tests/mcp_proxy/tools/test_task_lifecycle_coverage.py::*` — scope-reason: delete the review transition tool classes
- `tests/mcp_proxy/tools/tasks/test_create_task.py::*` — scope-reason: delete the `initialize_task_manifest` cases
- `tests/mcp_proxy/tools/tasks/test_checkout_unresolved_envelopes.py::*` — scope-reason: delete the `submit_for_review` case
- `tests/mcp_proxy/tools/test_read_only_classification.py::*` — scope-reason: drop the three read-only stage tools
- `tests/mcp_proxy/tools/test_tasks_registry_edge_cases.py::*` — scope-reason: delete the two stage tool schema cases
- `tests/mcp_proxy/services/test_direct_tool_session_activation.py::*` — scope-reason: build the interactive-only block inline instead of loading the deleted rule
- `tests/workflows/test_task_enforcement_rules.py::*` — scope-reason: drop the removed tools and the deleted rule's cases
- `tests/workflows/test_monolith_guard.py::*` — scope-reason: the task-transition rule names two tools
- `tests/framing_corpus.py::*` — scope-reason: drop the deleted rule name
- `tests/workflows/test_retired_bundled_definitions.py::*` — scope-reason: list the deleted rule
- `tests/agents/test_bundled_agent_contract.py`
- `tests/tasks/test_expansion_qa_coverage.py::*` — scope-reason: coverage results carry no review action
- `tests/workflows/test_expansion_qa_rejection.py::*` — scope-reason: assert pass state and failures instead of a review action
- `tests/mcp_proxy/tools/tasks/test_mcp_proxy_tools_tasks_expansion.py::*` — scope-reason: the start schema has no stage pipeline flag
- `tests/skills/test_plan_enhance_skill.py::*` — scope-reason: drop the two `record_plan_enhancement` terms
- `tests/e2e/test_sequential_review_loop.py::*` — scope-reason: delete the review approval case

**Granularity:** one outcome, nothing can enter, move or read a stage through a dedicated tool, command or route. The four tool modules share helpers and factories, the stage CLI and routes call the same storage, and the rules, gates, hints and audits that name the review tools fail their own tests once the tools are unregistered. Most Targets are whole-file deletions.

This leaf removes the dedicated stage surfaces. Stage fields and filters on general task surfaces (the `gobby tasks list` stage options, the task list route's stage parameters and the stage manifest in task payloads) are readers, and 4.1 removes them.

MCP tools (Decision Record items 1, 3 and 9):

- Delete `_stage_ops.py`, `_stage_review.py`, `_stage_read.py` and `_stage_registry_ops.py`. That unregisters the 19 stage tools: `initialize_task_manifest`, `start_stage`, `complete_stage`, `fail_stage`, `add_stage`, `remove_stage`, `record_pr_verdict` and `record_merge_result`; `submit_for_review`, `approve_review`, `reject_review` and `record_plan_enhancement`; `get_task_stages`, `list_stages_registry` and `get_task_type_defaults`; and `update_stage`, `restore_stage`, `delete_stage` and `set_task_type_defaults`.
- `_stage_ops.py` also registers `backfill_plan_review_lessons` from `_plan_review_backfill.py`. It re-runs the lesson mint that `approve_review` performs through `_plan_review_approval.py`, so both modules go and it is the twentieth removed tool (Decision Record item 9).
- Delete `_dispatch_mutex_release.py`; only the deleted tools import it. Terminal cleanup still releases the dispatch mutex at run end.
- `_ops_factory.py`: delete the `create_stage_ops_registry` and `create_stage_registry_ops_registry` imports, their two `merge_from` calls and the two comments above them. `_factory.py`: delete the `create_stage_read_registry` import, its `merge_from` call and its comment, and the comment "# Merge lifecycle tools (review stage transitions live in gobby-tasks-ops)." becomes "# Merge lifecycle tools.".
- `_escalation_coordinator.py`, `_notifications.py` and `_lifecycle_status.py` keep other callers and stay. The stage options of `de_escalate_task` in `_lifecycle_status.py` go in 4.1.

HTTP routes:

- Delete `src/gobby/servers/routes/stages.py` and `src/gobby/servers/routes/tasks_stage_routes.py`.
- `routes/__init__.py` drops the `create_stages_router` import and its `__all__` entry. `_app_routes.py` drops the import and `app.include_router(create_stages_router(server))`.
- `src/gobby/servers/routes/tasks.py` drops the `register_task_stage_routes` import and call, and its module docstring drops "stage-transition, ". `_stage_view` and `_stage_views_for_tasks` stay for the list route until 4.1.

CLI:

- Delete `src/gobby/cli/stages.py`, `src/gobby/cli/tasks/stages.py` and `src/gobby/cli/tasks/review.py`.
- `cli/__init__.py` drops the `"stages"` lazy entry. `cli/tasks/main.py` drops the `review_cmd`, `stages_cmd` and `advance_cmd` imports and their three `tasks.add_command` calls.

Rules:

- Delete `block-needs-review-interactive.yaml`.
- `require-commit-before-status`: `mcp_tools` keeps `gobby-tasks:close_task` and `gobby-tasks:de_escalate_task`.
- `require-monolith-resolution-before-task-transition` in `require-same-session-decomposition.yaml`: `mcp_tools` keeps the same two tools, and the description becomes "Block task completion while owned source is over budget".
- `rules/AGENTS.md`: the `task-enforcement` count becomes 19.

Gates and hints:

- `blocking.py`: in `TASK_MUTATION_TOOLS_BY_SERVER`, the `gobby-tasks-ops` set keeps `set_landing_freeze` and `land_commit`.
- `workflows/hooks.py` (859 lines): the delete-lines proof removes lines 507-509, the three `gobby-tasks-ops` review identities in the commit-gate tool set, which keeps `gobby-tasks:close_task` and `gobby-tasks:de_escalate_task`. The file ends at 856 lines, and no other leaf in this plan edits it.
- `_updates.py`: the legacy-state hint becomes "Use close_task, reopen_task, or escalate_task instead." and the stage-or-ownership hint becomes "Use claim_task, release_task_claim, escalate_task, de_escalate_task, close_task, or reopen_task instead." The refused field lists change in P4.

Expansion coverage:

- `expansion_qa_coverage.py`: delete `_review_action`, the `is_spawned_agent` parameter of `run_expansion_qa_coverage`, the `review_action` value with its key in `qa_result` and in the returned dict, and the `review_action` key in the `qa_result` of `_fail_plan_hash_drift`. Every action it built named `approve_review` or `reject_review` for an expansion stage. Results keep `passed`, `failures`, the manifest path and the scope.
- `_expansion_registry.py` (890 lines): one delete-lines proof removes `_current_session_is_spawned` with its trailing blank lines (222-238; `_resolve_current_session` keeps three callers), the internal `stage_pipeline_mode` parameter of `start_expansion_run` (307), its pass-through (349), its schema property (412-419) and the `is_spawned_agent=` argument (759). The file ends at 862 lines, and no other leaf in this plan edits it.
- `_expansion_runtime.py` keeps its `stage_pipeline_mode` parameters, which now always take their defaults, until 4.1.

Guidance: in `references/plan/enhancement.md`, the sentence "Autonomous enhancer uses gobby-tasks-ops:record_plan_enhancement with round_number, converged, and suggestions serialized as strings (the registered tool accepts a string array, not structured suggestion objects), then delivers its result and ends its agent run with the required structured handoff." becomes "Autonomous enhancer delivers its result and ends its agent run with the required structured handoff." The next sentence stays, and the paragraph's last sentence becomes "Its budget is independent of adversarial review." The failures paragraph after it stays.

Capability audits (findings and evidence records stay as history):

- `tasks.json`: delete the 16 operation entries from `get_task_stages` through `set_task_type_defaults`, the `gobby tasks advance`, `gobby tasks review` and `gobby tasks stages` CLI entries, and the `record_plan_enhancement`, `backfill_plan_review_lessons`, `record_pr_verdict` and `record_merge_result` lines in `delegated_operations`.
- `plan.json`: delete the `record_plan_enhancement` and `backfill_plan_review_lessons` operation entries and the `approve_review` and `reject_review` lines in its delegated `tasks` operations.
- `review.json`: delete the `complete_stage`, `fail_stage`, `submit_for_review`, `approve_review` and `reject_review` operation entries.
- `source-control.json`: delete the `record_merge_result` and `record_pr_verdict` operation entries.
- `integrations.json`: drop `record_merge_result` from its shared surfaces.

Tests:

- Delete the 27 test files above.
- `test_stage_routes.py`: delete `test_routes_registered`, `test_stages_module_has_no_zero_route_router_global`, every `test_patch_*` case, `test_stage_transition_broadcasts`, `test_move_to_stage_broadcasts_full_stage_payload` and the `stages` import. The six `test_list_*` cases build rows through the storage helpers and stay until 4.1.
- `test_tasks_routes.py`: delete `test_submit_for_review`, `test_approve_review` and `test_reject_review`. `_start_current_stage` keeps its other callers.
- `test_task_lifecycle_coverage.py`: delete `TestMarkTaskReviewApproved`, `TestMarkTaskNeedsReview` and `_create_stage_ops_registry`. `_make_stage_state` and its stage imports stay until 4.2.
- `test_create_task.py`: delete `test_initialize_task_manifest_persists_explicit_cap`, `test_initialize_task_manifest_rejects_unknown_stage` and the `create_stage_ops_registry` import, and the `stage_row` import if nothing else uses it.
- `test_checkout_unresolved_envelopes.py`: delete `test_submit_for_review_returns_checkout_unresolved`.
- `test_read_only_classification.py`: `TASK_READ_ONLY_TOOLS` drops `get_task_stages`, `list_stages_registry` and `get_task_type_defaults`.
- `test_tasks_registry_edge_cases.py`: delete `test_stage_mutation_tools_have_precise_output_schema` and `test_set_task_type_defaults_rejects_invalid_payload`.
- `test_direct_tool_session_activation.py`: `_load_interactive_review_block_rule` becomes `_load_interactive_only_block_rule`, which creates a rule named `interactive-only-close-block` with `event: before_tool`, `when: "not variables.get('is_spawned_agent')"` and one block effect on `gobby-tasks:close_task`. The two tests become `test_direct_mcp_tool_reconciles_spawned_session_before_rules` and `test_direct_mcp_tool_still_blocks_interactive_session`; both call `gobby-tasks` `close_task` with `{"task_id": "#1"}`, and the second asserts `interactive-only-close-block` in the error. Imports only the old helper used go.
- `test_task_enforcement_rules.py`: delete `TASK_REVIEW_OPERATIONS` and its two uses (the `gobby-tasks-ops` parameter list keeps `LANDING_OPERATIONS`, and `test_real_registry_inventory_matches_independent_classification` drops it from its union). `READ_ONLY_TASK_TOOLS` drops the three stage read tools. `NON_INTERACTIVE_TASK_OPS` drops `add_stage`, `backfill_plan_review_lessons`, `complete_stage`, `delete_stage`, `fail_stage`, `initialize_task_manifest`, `record_merge_result`, `record_plan_enhancement`, `record_pr_verdict`, `remove_stage`, `restore_stage`, `set_task_type_defaults`, `start_stage` and `update_stage`. The expected task-enforcement rule set drops `block-needs-review-interactive`. `test_spawned_worker_keeps_assigned_lifecycle_operations_available` drops its `review-available` parameter, and its server name is always `gobby-tasks`. Delete `TestBlockNeedsReviewInteractive`.
- `test_monolith_guard.py`: `test_bundled_rules_cover_commit_transitions_turn_end_and_required_guidance` expects `{"gobby-tasks:close_task", "gobby-tasks:de_escalate_task"}`.
- `tests/framing_corpus.py`: drop `block-needs-review-interactive`.
- `test_retired_bundled_definitions.py`: add `block-needs-review-interactive` to `RETIRED_RULES`.
- `test_bundled_agent_contract.py`: `REMOVED_LIFECYCLE_TOOLS` adds the 19 stage tools and `backfill_plan_review_lessons`.
- `test_expansion_qa_coverage.py`: `test_hash_prefixed_root_ref_matches_registry_written_manifest` asserts `result["qa_result"]["scope"]["root_task"] == _ROOT_REF` instead of the review action argument. Delete `test_interactive_coverage_persists_inapplicable_review_action` and `test_spawned_coverage_persists_legacy_review_action_shape`.
- `test_expansion_qa_rejection.py`: `test_missing_row_triggers_rejection` asserts `result["passed"] is False` and one failure with `section_id` `A1`, `item_id` `A1.1`, `status` `invalid`, `detail` "artifact src/example.py was not referenced" and `leaves` `["#13244"]`. `test_zero_missing_invalid_triggers_approval` asserts `result["passed"] is True`, an empty `result["qa_result"]["failures"]` and no `review_action` key.
- `test_mcp_proxy_tools_tasks_expansion.py`: `test_start_expansion_schema_accepts_reset_output` asserts `"stage_pipeline_mode" not in schema["properties"]`.
- `test_plan_enhance_skill.py`: `test_completion_uses_supported_tools_and_typed_handoff` checks "end_agent_run requires current_state and next_steps", "independent of adversarial review" and "Do not record a failed/truncated run as converged".
- `test_sequential_review_loop.py`: delete `TestReviewStepE2E::test_review_task_can_be_approved`.

**Research context:**

- Inventory (2026-10-08, `rg` over `src/` and `tests/` for the 20 tool names and the deleted module paths): every production importer of the deleted modules is a Target above, except `_stage_ops.py`'s `gobby.build.controls` import and `_stage_review.py`'s dispatcher tick and coordinator relay, which 2.4 removes, and the `_stage_ops` case in `test_agent_workflow_runtime_cleanup.py`, which 2.5 deletes with the dispatcher.
- Storage-level stage callers stay until P4: `task_manager.stage_states` and the manager's review transitions in `tests/storage/`, `tests/storage/test_stage_review_findings.py`, `tests/review_learning/test_round_diff.py`, `tests/mcp_proxy/tools/test_hub.py`, `tests/integration/test_hub_query.py`, `tests/servers/routes/test_agent_spawn_routes.py`, `tests/workflows/test_condition_helpers*.py`, `test_pipeline_heartbeat*.py`, `tests/agents/test_lifecycle_monitor*.py` and the expansion apply tests. `src/gobby/agents/task_recovery.py`, `tasks/expansion/_reset.py` and `workflows/pipeline_heartbeat.py` call stage storage methods, not the tools.
- Tests that name these tools as arbitrary strings, synthetic handlers or absence checks stay: `tests/workflows/test_handler_route_lint.py`, `test_bundled_agent_lifecycle_copy.py`, `test_step_enforcement.py`, `test_stop_gates_rules.py`, `test_agent_workflow_completion.py`, `tests/storage/tasks/test_review_tools_call_site_audit.py`, `tests/test_plan_prose_review_tool_descriptions.py`, `tests/mcp_proxy/services/test_tool_proxy_validation.py`, `tests/agents/test_prompt_detector.py`, `tests/ai/test_embedding_switch_runner.py`, `tests/mcp_proxy/tools/test_apply_persona.py`, `tests/hooks/test_session_activation_reconciliation.py` and `tests/mcp_proxy/tools/test_tasks_crud_coverage.py`.
- `FINDING_ITEM_SCHEMA` in `src/gobby/plans/review_findings.py` loses its production caller, the `reject_review` findings parameter, in this leaf. `render_rejection_section` in the same module keeps a caller in `storage/tasks/_review_transitions.py`, so 4.2 deletes the dead schema and rendering code together.
- `review_learning.recorders.mint_plan_review_lessons` stays with only test callers, as part of the stage-bound evidence remainder that Decision Record item 9 keeps.
- After 3.4 no kept definition or `SKILL.md` names any of the 20 tools (`rg`, 2026-10-08), so the substring scan behind `REMOVED_LIFECYCLE_TOOLS` passes.
- The web tool-block editor's placeholder text names `submit_for_review`; 4.1 changes it with the other web readers.
- Large files: `workflows/hooks.py` (859 lines) and `_expansion_registry.py` (890 lines) each take one delete-lines proof here. Neither contains stage, column or dispatch vocabulary that a later leaf removes. No other production Target reaches 850 lines (`blocking.py` 653, `expansion_qa_coverage.py` 533, `src/gobby/servers/routes/tasks.py` 526, `_updates.py` 412).
- `sync_bundled_rules` soft-deletes the installed `block-needs-review-interactive` row at the next sync.
- Planned checks: focused pytest on every edited test Target plus `tests/agents/test_bundled_agent_contract.py`, `tests/skills/test_reference_library.py`, `tests/mcp_proxy/test_registries.py`, `tests/workflows/test_close_validator_allowlist.py` and `tests/cli/`; ruff, format check and mypy on `src/`; `uv run python -c "import gobby.runner"`.

**Acceptance:**

- 3.5.1 - gobby-tasks and gobby-tasks-ops register none of the 20 removed tools, and their registered inventory matches the independent classification. test: `tests/workflows/test_task_enforcement_rules.py::TestRequireTasksSkillForMutations::test_real_registry_inventory_matches_independent_classification`. test: `tests/mcp_proxy/tools/test_read_only_classification.py::test_task_ops_read_only_classification_is_exact`.
- 3.5.2 - No bundled definition or skill names a removed tool, and every tool a definition names is registered. test: `tests/agents/test_bundled_agent_contract.py::test_bundled_agent_and_skill_assets_do_not_reference_removed_lifecycle_tools`. test: `tests/agents/test_bundled_agent_contract.py::test_bundled_agent_mcp_references_match_registered_tool_inventory`.
- 3.5.3 - The daemon serves no `/api/stages` route and no per-task stage route, and the CLI has no stage or review command. behavior: "create_stages_router" absent from `src/gobby/servers/_app_routes.py`. behavior: "register_task_stage_routes" absent from `src/gobby/servers/routes/tasks.py`. behavior: "stages" absent from `src/gobby/cli/__init__.py`. file: `src/gobby/cli/tasks/main.py`.
- 3.5.4 - The interactive review block is gone, and the commit and monolith gates block only `close_task` and `de_escalate_task`. test: `tests/workflows/test_retired_bundled_definitions.py::test_retired_rules_are_absent_from_bundled_templates`. test: `tests/workflows/test_monolith_guard.py::test_bundled_rules_cover_commit_transitions_turn_end_and_required_guidance`.
- 3.5.5 - Expansion coverage reports pass state and failures without a stage review action, and the start tool has no stage pipeline flag. test: `tests/workflows/test_expansion_qa_rejection.py::test_missing_row_triggers_rejection`. test: `tests/mcp_proxy/tools/tasks/test_mcp_proxy_tools_tasks_expansion.py::test_start_expansion_schema_accepts_reset_output`.
- 3.5.6 - The capability audits cite no deleted module or command, and the reference library passes. test: `tests/skills/test_reference_library.py::test_reference_contract_3_2_1`.

## P4: Stage readers and task storage
`kind: framing`

After P3 no tool, command or route writes a stage row, but the task projection, list filters, CLI and web views, hub queries, session summaries, expansion, agent recovery and the pipeline heartbeat still read or write stage state, and the four automation columns still surface on task tools, routes, the CLI, the web UI and the task export. 4.1 removes every such reader and writer outside `gobby.storage.tasks`. 4.2 then deletes stage storage and the columns' storage code. The tables and columns stay until 6.1 drops them.

### 4.1 Nothing outside task storage reads or writes stages or the automation columns (depends: 3.5) [category: code]
`kind: deliverable`

Targets:
- `src/gobby/tasks/state_semantics.py::*` — scope-reason: the task projection drops the current stage, merge readiness and the automation columns
- `src/gobby/tasks/isolation.py::*` — operation: delete — scope-reason: validates only the task checkout-mode column
- `src/gobby/mcp_proxy/tools/tasks/_crud.py::*` — operation: delete-lines — base-blob: 71dd27d4e0e992fec61e284c887957527b1bda9c — lines: 38, 533-534, 618-626, 720-730, 765, 793-797, 800, 815, 832-836 — scope-reason: update_task drops the automation fields and list_tasks drops the stage filter
- `src/gobby/mcp_proxy/tools/tasks/_formatters.py::*` — scope-reason: task payloads drop the current stage and the automation columns
- `src/gobby/mcp_proxy/tools/tasks/_search.py::*` — scope-reason: search_tasks drops the stage filter
- `src/gobby/mcp_proxy/tools/tasks/_lifecycle_status.py::*` — scope-reason: de_escalate_task drops its two stage options
- `src/gobby/mcp_proxy/tools/tasks/_expansion_runtime.py::*` — scope-reason: drop the stage pipeline mode
- `src/gobby/mcp_proxy/tools/task_readiness.py::*` — scope-reason: drop the in-progress stage query
- `src/gobby/mcp_proxy/tools/hub.py::*` — scope-reason: cross-project task rows drop the stage join
- `src/gobby/mcp_proxy/tools/spawn_agent/_runtime.py::*` — scope-reason: drop the dispatch stage-state run metadata
- `src/gobby/servers/routes/tasks.py::*` — scope-reason: task payloads, list parameters and update fields drop stages and the automation columns
- `src/gobby/servers/routes/tasks_assignment.py::*` — scope-reason: the assignment payload drops the current stage
- `src/gobby/servers/routes/tasks_lifecycle_routes.py::*` — scope-reason: the de-escalation request drops its two stage fields
- `src/gobby/servers/routes/admin/_stats.py::*` — scope-reason: task stats drop the stage join and the two review states
- `src/gobby/servers/routes/admin/_health.py::*` — scope-reason: task state counts drop the two review states
- `src/gobby/cli/tasks/_stage_filters.py::*` — operation: delete — scope-reason: the `tasks list` stage filter
- `src/gobby/cli/tasks/crud.py::*` — scope-reason: drop the stage list options, the checkout-mode option and the de-escalation stage flags
- `src/gobby/cli/tasks/_crud_common.py::*` — scope-reason: drop the stage display helper and the checkout-mode choice
- `src/gobby/cli/tasks/_crud_detail.py::*` — scope-reason: task detail drops the stage and merge-ready lines
- `src/gobby/cli/tasks/_crud_listing.py::*` — scope-reason: listing and stats drop the stage filter and stage counts
- `src/gobby/cli/tasks/_crud_mutations.py::*` — scope-reason: update drops checkout mode and de-escalation drops its stage flags
- `src/gobby/cli/tasks/_crud_services.py::*` — scope-reason: drop the isolation validator service
- `src/gobby/cli/tasks/_utils/listing.py::*` — scope-reason: drop the stage grouping key
- `src/gobby/cli/tasks/_utils/rendering.py::*` — scope-reason: the state letter comes from the projected state
- `src/gobby/cli/tasks/ai.py::*` — scope-reason: the suggestion line shows the projected state
- `src/gobby/cli/tasks/search.py::*` — scope-reason: search rows show the projected state
- `src/gobby/agents/task_recovery.py::*` — scope-reason: drop the stage failure branch and the dispatch failure count
- `src/gobby/agents/resume_metadata.py::*` — scope-reason: drop the dispatch stage-state field
- `src/gobby/agents/lifecycle_reconciliation.py::*` — scope-reason: drop the dispatch stage-context check
- `src/gobby/hooks/session_lookup.py::*` — scope-reason: the state label stops reading the current stage
- `src/gobby/hooks/event_handlers/_session_responses.py::*` — scope-reason: the state label and claimed-task query stop reading stages
- `src/gobby/sessions/formatting.py::*` — scope-reason: the task state stops reading the current stage
- `src/gobby/plans/_task_record_state.py::*` — scope-reason: the record state stops reading the current stage
- `src/gobby/plans/deferral.py::*` — scope-reason: the active state set drops the stage states
- `src/gobby/workflows/observers.py::*` — scope-reason: the claimed-task query drops the stage filter
- `src/gobby/workflows/pipeline_heartbeat.py::*` — scope-reason: stale recovery releases the claim without stage moves
- `src/gobby/storage/attention.py::*` — scope-reason: attention rows drop the stage join
- `src/gobby/sync/tasks.py::*` — scope-reason: the task export and import drop the automation columns
- `src/gobby/tasks/expansion/_apply.py::*` — scope-reason: expansion stops copying stage manifests and automation columns to children
- `src/gobby/tasks/expansion/_common.py::*` — scope-reason: drop the stage manifest helpers
- `src/gobby/tasks/expansion/_compile.py::*` — scope-reason: drop the stage spec compilation
- `src/gobby/tasks/expansion/_reset.py::*` — scope-reason: drop the parent expansion stage completion
- `src/gobby/tasks/expansion_service.py::*` — scope-reason: drop the parent expansion stage alias
- `web/src/lib/stageActions.ts::*` — operation: delete — scope-reason: the stage types and helpers
- `web/src/types/tasks.ts::*` — scope-reason: the task type drops stages and the automation columns and takes the base fields
- `web/src/lib/taskState.ts::*` — scope-reason: display states drop the stage review states
- `web/src/lib/taskNormalization.ts::*` — scope-reason: normalization drops stage rows and the automation columns
- `web/src/components/activity/TasksTabModel.ts::*` — scope-reason: drop the stage colour and the review states
- `web/src/components/activity/TasksTabData.ts::*` — scope-reason: drop the stage fields
- `web/src/components/activity/TasksTab.tsx::*` — scope-reason: task fetches stop requesting stage rows
- `web/src/components/activity/TaskTreeRow.tsx::*` — scope-reason: drop the stage pill
- `web/src/components/activity/taskdetail/TaskDetailRelationships.tsx::*` — scope-reason: drop the review states
- `web/src/components/activity/taskdetail/TaskDetailTrace.tsx::*` — scope-reason: drop the automation section
- `web/src/components/activity/taskdetail/taskDetailFormat.ts::*` — scope-reason: drop the review state and stage name
- `web/src/components/tasks/TaskBadges.tsx::*` — scope-reason: drop the review state badges
- `web/src/components/agents/AgentToolBlocksEditor.tsx::*` — scope-reason: the placeholder names a registered tool
- `web/src/lib/__tests__/taskState.test.ts::*` — scope-reason: drop the review display state cases
- `web/src/lib/__tests__/taskNormalization.test.ts::*` — scope-reason: drop the stage and column cases
- `web/src/components/tasks/__tests__/TaskBadges.test.tsx::*` — scope-reason: drop the review badge cases
- `web/src/components/activity/__tests__/fixtures.ts::*` — scope-reason: fixtures drop stages and the automation columns
- `web/src/components/activity/__tests__/TasksTab.setup.ts::*` — scope-reason: shared fixtures drop the current stage and the review states
- `web/src/components/activity/__tests__/TasksTab.test.tsx::*` — scope-reason: fixtures and request expectations drop stages and the review states
- `web/src/components/activity/__tests__/TasksTab.filters.test.tsx::*` — scope-reason: request expectations drop stage rows and merge readiness
- `web/src/components/activity/__tests__/TasksTab.events.test.tsx::*` — scope-reason: event fixtures drop the current stage and the review states
- `web/src/components/activity/__tests__/QuickMenu.test.tsx::*` — scope-reason: fixtures drop the current stage
- `web/src/components/activity/__tests__/TaskQuickMenu.test.tsx::*` — scope-reason: fixtures drop the current stage
- `web/src/components/activity/__tests__/useTaskInlineEdit.test.tsx::*` — scope-reason: fixtures drop the current stage
- `tests/cli/test_tasks_list_stage_filter.py::*` — operation: delete — scope-reason: its contract requires the deleted stage filter module
- `tests/mcp_proxy/tools/tasks/test_list_tasks_current_stage_filter.py::*` — operation: delete — scope-reason: tests the removed list filter
- `tests/servers/routes/test_stage_routes.py::*` — operation: delete — scope-reason: after 3.5 it holds only the list-route stage filter cases
- `tests/workflows/test_pipeline_heartbeat_stage_native.py::*` — operation: delete — scope-reason: pins the stage moves this leaf removes
- `tests/agents/test_lifecycle_monitor_stage_native.py::*` — operation: delete — scope-reason: pins the stage failure branch this leaf removes
- `tests/storage/tasks/test_automation_candidates_states.py::*` — operation: delete — scope-reason: imports the deleted active stage set and pins the automation candidates that 4.2 deletes
- `tests/storage/tasks/test_lifecycle_enums.py::*` — scope-reason: the serializers drop the automation columns
- `tests/storage/test_storage_tasks.py::*` — scope-reason: the projection imports and the stage and automation column cases go
- `tests/tasks/test_serialize_task_state.py::*` — scope-reason: the projection has no current stage, merge readiness or automation columns
- `tests/storage/tasks/test_task_model_stage_native.py::*` — scope-reason: the serialized state has no current stage
- `tests/cli/test_tasks_cli.py::*` — scope-reason: drop the stage filter, stage display and checkout-mode cases
- `tests/mcp_proxy/tools/tasks/test_get_task_response_shape.py::*` — scope-reason: the payload has no stage or automation columns
- `tests/mcp_proxy/tools/tasks/test_mcp_proxy_tools_tasks_search.py::*` — scope-reason: drop the stage filter cases
- `tests/mcp_proxy/tools/test_tasks_crud_coverage.py::*` — scope-reason: drop the automation field and stage filter cases
- `tests/mcp_proxy/tools/test_tasks_schema_coverage.py::*` — scope-reason: the schemas drop the removed fields
- `tests/mcp_proxy/tools/test_task_readiness.py::*` — scope-reason: in-progress work comes from claimed tasks
- `tests/mcp_proxy/tools/test_hub.py::*` — scope-reason: hub rows have no stage
- `tests/integration/test_hub_query.py::*` — scope-reason: hub rows have no stage
- `tests/servers/routes/test_tasks_routes.py::*` — scope-reason: drop the stage list, payload and checkout-mode cases
- `tests/sessions/test_sessions_formatting.py::*` — scope-reason: task state has no stage branch
- `tests/workflows/test_pipeline_heartbeat.py::*` — scope-reason: stale recovery releases the claim
- `tests/agents/test_lifecycle_monitor.py::*` — scope-reason: recovery has no stage failure branch
- `tests/agents/test_lifecycle_monitor_extra.py::*` — scope-reason: recovery has no stage failure branch or dispatch failure count
- `tests/agents/test_resume_metadata.py::*` — scope-reason: resume metadata has no stage state
- `tests/tasks/expansion/test_tasks_expansion_apply.py::*` — scope-reason: expansion copies no stage manifest
- `tests/mcp_proxy/tools/tasks/test_mcp_proxy_tools_tasks_expansion.py::*` — scope-reason: expansion runs have no stage pipeline mode
- `tests/tasks/test_task_backup.py::*` — scope-reason: the export has no automation columns
- `tests/cli/test_validation_cli.py::*` — scope-reason: de-escalation has no stage flags
- `tests/mcp_proxy/test_validation_mcp_tools.py::*` — scope-reason: de_escalate_task has no stage options and stand-in tasks have no stage fields
- `tests/mcp_proxy/test_annotate_mcp.py::*` — scope-reason: the task payload has no automation column
- `tests/hooks/test_session_events_coverage.py::*` — scope-reason: claimed-task queries pass no stage filter and labels come from the projection
- `tests/hooks/test_session_lookup_metadata.py::*` — scope-reason: the state label reads no stage
- `tests/servers/routes/test_agent_spawn_routes.py::*` — scope-reason: spawn routes see no review stage
- `tests/plans/test_coverage.py::*` — scope-reason: the record state reads no stage
- `tests/tasks/expansion/test_apply_dev_only.py::*` — scope-reason: the parent expansion stage helper is gone
- `tests/mcp_proxy/tools/test_parallel_dispatch.py::*` — scope-reason: in-progress files come from claimed tasks
- `tests/workflows/test_stop_gates_rules.py::*` — scope-reason: the active stage set is gone
- `tests/workflows/test_hooks.py::*` — scope-reason: turn-end claim rebuilds read no stage
- `tests/workflows/test_condition_helpers_task_state.py::*` — scope-reason: the task state helper reads no stage
- `tests/servers/test_attention_roster.py::*` — scope-reason: attention rows have no task stage
- `tests/agents/test_attention_metadata.py::*` — scope-reason: attention rows have no task stage

**Granularity:** one outcome, no code outside `gobby.storage.tasks` reads or writes a stage or an automation column. The task projection in `state_semantics.py` feeds every payload, CLI view, session summary and web type in this list, so changing it without its readers breaks them, and the readers share the list filters that end in storage's `current_stage_state` parameter.

The stage projection (Decision Record item 1):

- `state_semantics.py`: delete `ACTIVE_STAGE_STATES`, `_stage_position`, `current_stage`, `current_stage_state`, `is_task_merge_ready` and `_current_stage_payload`. `projected_task_state` returns `closed`, `escalated` or `ready`. `is_task_actionable` returns whether the task exists, is open and is not escalated. `serialize_task_state` drops `current_stage`, `is_merge_ready`, `allow_automation`, `unattended` and `checkout_mode`. The module docstring becomes "Shared task state semantics for task projections."
- The state label helpers in `hooks/session_lookup.py`, `_session_responses.py`, `sessions/formatting.py` and `plans/_task_record_state.py` drop their current-stage branches and fall through to `ready` or their default. `plans/deferral.py`: `_ACTIVE_TASK_STATES` becomes `{"ready", "escalated"}`.
- Claimed-task queries in `_session_responses.py`, `workflows/observers.py` and `pipeline_heartbeat.py` drop `current_stage_state=list(ACTIVE_STAGE_STATES)`. For a task with no stage rows that filter already matched every row, so the result is unchanged.
- `task_readiness.py`: the `current_stage_state="in_progress"` query goes; `in_progress_tasks` starts empty and collects the actionable claimed tasks.
- `hub.py`: delete `_current_stage_join_sql`, the stage and registry joins and the five `current_stage_*` columns in `_query_tasks`. `_task_state_from_row` drops `current_stage` and `is_merge_ready`, and `is_claimed` becomes an owner on an open, unescalated task. Until now `is_claimed` also required an active stage, so stage-less claimed tasks reported unclaimed. The `state` filter documentation lists `ready`, `closed` and `escalated`.
- `storage/attention.py`: the attention query drops the current-stage lateral join and its `task_stage` column, and the row model drops `task_stage`.
- `admin/_stats.py`: delete `_current_stage_join_sql`, the join and the stage predicate in the ready-task query, and the `needs_review` and `review_approved` counters. `src/gobby/servers/routes/admin/_health.py` drops the same two states from its state list and its zeroed counts.

Task tools, routes and CLI:

- `_crud.py` (969 lines): the delete-lines proof removes the isolation import (38), the `allow_automation` and `checkout_mode` parameters of `update_task` (533-534), their handling (618-626), their schema properties (720-730), the `current_stage_state` parameter of `list_tasks` (765), its comma split (793-797), its two pass-throughs (800, 815) and its schema property (832-836). The file ends at 933 lines, and no other leaf in this plan edits it.
- `_formatters.py` drops `current_stage` and the three automation columns from the task payloads. `_search.py` drops the `current_stage_state` parameter, its comma split, its pass-through and its schema property.
- `_lifecycle_status.py`: `de_escalate_task` drops `reset_stage_attempts` and `restore_stage_from_history` with their docstring lines, pass-throughs and schema properties. `tasks_lifecycle_routes.py` drops the same two request fields and pass-throughs.
- `src/gobby/servers/routes/tasks.py`: drop the `StageState`, `stage_state_view` and isolation imports, `_stage_view`, `_stage_views_for_tasks`, `_normalize_stage_filters`, the list route's `current_stage_state`, `stage`, `stage_state` and `include_stages` parameters with their handling, and the `allow_automation` and `checkout_mode` update fields with the checkout validation. `tasks_assignment.py` drops `task_stage` from its payload and derives the task status from `is_closed` and `is_escalated`.
- CLI: delete `_stage_filters.py`. `crud.py` drops the `--stage`, `--state` and `--group stage` list options, the `--checkout-mode` update option, the de-escalation `--reset-stage-attempts` and `--restore-stage-from-history` flags and the isolation service. `_crud_common.py` drops `current_stage_display` and `CHECKOUT_MODE_CHOICE`. `_crud_detail.py` drops the stage and merge-ready lines. `_crud_listing.py` drops the stage filter parameters and the stage and merge-ready counts from stats. `_crud_mutations.py` drops the checkout-mode update and the two de-escalation flags. `_crud_services.py` drops `validate_task_isolation_artifacts`. `src/gobby/cli/tasks/_utils/listing.py` drops `_stage_key`, and `src/gobby/cli/tasks/_utils/rendering.py` maps the projected state to its letter, with no review letters. `ai.py` and `search.py` print the projected state.
- Delete `gobby.tasks.isolation`; `_crud.py`, `src/gobby/servers/routes/tasks.py` and the CLI service were its only importers.

Agents, expansion and recovery:

- `task_recovery.py`: delete the branch of `recover_task_from_terminal_agent` that counts dispatch failures and fails the current stage, together with `_fail_current_stage`, `_stage_name`, the failure threshold read, the `dispatch_failure_count` protocol member and the `current_stage` import. That branch runs only when `projected_task_state` returns `in_progress`, so every run already takes the release path (Decision Record item 11).
- `spawn_agent/_runtime.py` stops copying `stage_state` from the initial variables into run metadata, `resume_metadata.py` drops its `stage_state` field, and `lifecycle_reconciliation.py` deletes `has_dispatch_stage_context` and its guard. Only dispatch spawn set that variable.
- `pipeline_heartbeat.py`: delete `_submit_current_stage_for_review` and `_recover_abandoned_stage`; `_recover_stale_task` releases the claim and returns `release`. The `check_stale_tasks` docstring drops its stage step.
- Expansion: `_apply.py` stops deriving child stage manifests and stops copying the automation columns to children; `_common.py`, `_compile.py` and `_reset.py` drop their stage helpers; `expansion_service.py` drops `_complete_parent_expansion_stage_if_current`; `_expansion_runtime.py` deletes `_is_stage_pipeline_expansion_run` and the `stage_pipeline_mode` parameters, and runs the non-pipeline branch.
- `sync/tasks.py` drops the three automation columns from the export and import rows.

Web UI (load `impeccable` and `.impeccable.md` first):

- Delete `stageActions.ts`. `LifecycleTask`, without `stages` and `dispatch_failure_count`, moves into `web/src/types/tasks.ts` as the base of `GobbyTask`, which drops `current_stage`, `stages`, `allow_automation`, `unattended`, `checkout_mode`, `dispatch_failure_count` and `stageState`.
- `taskState.ts`: `TaskDisplayState` is `ready`, `in_progress`, `blocked` or `closed`, where `in_progress` means claimed. Delete `deriveCurrentStage`, `getCanonicalStageName`, `is_merge_ready` and the review entries in the label, colour, tone, glyph and soft-colour maps. `taskNormalization.ts` drops the stage row types and validators and the column fields. `TasksTabModel.ts` drops `getStageStateColor` and the review states; `TaskTreeRow.tsx` drops the stage pill; `TaskDetailRelationships.tsx`, `taskDetailFormat.ts` and `TaskBadges.tsx` drop the review states and the stage name; `TaskDetailTrace.tsx` drops its automation section; `TasksTabData.ts` drops the stage fields.
- `TasksTab.tsx`: the list and subtask fetches drop `include_stages=1`.
- `AgentToolBlocksEditor.tsx`: the placeholder becomes "e.g. gobby-tasks:close_task".

Tests:

- Delete the six test files above. Each remaining test Target drops the cases and fixture fields for the removed behavior; tests that build stage rows through storage helpers to exercise a removed reader drop those cases.
- After this leaf, no test Target in this list calls `task_manager.stage_states`, `initialize_task_manifest`, `submit_for_review`, `set_stage_state` or another `_stage_test_helpers` function, imports a removed `state_semantics` name, passes `stages=` or an automation column to `Task`, or passes a stage filter or automation column to task storage, because 4.2 deletes all of them.
- `test_lifecycle_enums.py` keeps `assigned_agent`, `implementation_domain` and `additional_skills` in its field and serializer checks, deletes `test_isolation_is_str_enum_with_dispatch_values`, and stops reading `CheckoutMode`. `test_storage_tasks.py` drops `_start_current_stage`, the `current_stage_state` import, the stage-state rows of its projection table and the stage filter and stage count cases.
- `test_session_lookup_metadata.py` deletes `test_task_context_uses_stage_native_state`. `test_agent_spawn_routes.py` deletes `test_spawn_web_chat_preserves_review_status` and `_submit_for_development_review`; `test_spawn_web_chat_does_not_steal_claimed_review_task` keeps its claimed task and drops the review setup and stage assertion. `test_hooks.py`'s `test_turn_end_rebuilds_review_claims_for_after_agent` drops `set_stage_state` and the `current_stage_state` assertion, and its claim rebuild assertions stay. `test_condition_helpers_task_state.py` deletes the two current-stage cases and their stage helper. `test_parallel_dispatch.py`'s `test_in_progress_files_excluded` serves its in-progress task from the claimed-task query.
- The web activity tests drop `current_stage`, `is_merge_ready` and the review states from fixtures, and the `include_stages=1` request expectations.

**Research context:**

- Inventory (2026-10-08): a sweep of `src/` and `tests/` for the stage and column identifiers outside files deleted by earlier leaves. Identifier matches in `terminals/`, `skills/materialization.py`, `hooks/verification_runner.py`, `workflows/engine/command_matching.py`, `install/bin_freshness_*.py` and `hooks/event_enrichment.py` are git staging or pipeline stages, not task stages. `checkout_mode` in the spawn, dry-run, agent and session-change modules is the agent checkout mode and stays.
- Session-task actions `needs_review` and `review_approved` in `storage/session_tasks.py` and `sessions/handoff_summary.py` classify recorded rows and stay.
- Stage storage, `_stage_test_helpers.py` and the stage storage tests stay for 4.2, which deletes them with the storage. The test Targets of this leaf stop calling them here.
- Identifier matches that stay: `needs_review` verdicts in plan review evidence, the agent `checkout_mode` keys, the `.status` mocks in `test_spawn_agent_impl_provider.py`, the dict in `test_result_offload.py` and the mock attribute in `test_safe_evaluator.py`.
- Large files: `_crud.py` (969 lines) takes its delete-lines proof here. No other production Target reaches 850 lines.
- Planned checks: focused pytest on every test Target plus `tests/mcp_proxy/tools/tasks/`, `tests/cli/`, `tests/servers/routes/` and `tests/tasks/`; ruff, format check and mypy on `src/`; `npm run typecheck`, `npm run lint` and the vitest files above in `web/`.

**Acceptance:**

- 4.1.1 - The task projection has no current stage, merge readiness or automation column, and projected state is closed, escalated or ready. test: `tests/tasks/test_serialize_task_state.py::test_new_shape`. file: `src/gobby/tasks/state_semantics.py`. behavior: "current_stage" absent from `src/gobby/tasks/state_semantics.py`.
- 4.1.2 - Task tools, routes and the CLI accept no stage filter and no automation field. behavior: "current_stage_state" absent from `src/gobby/mcp_proxy/tools/tasks/_crud.py`. behavior: "include_stages" absent from `src/gobby/servers/routes/tasks.py`. behavior: "checkout_mode" absent from `src/gobby/cli/tasks/crud.py`. test: `tests/mcp_proxy/tools/tasks/test_get_task_response_shape.py::test_no_legacy_fields`.
- 4.1.3 - Agent recovery and the pipeline heartbeat release stale claims without stage moves or dispatch failure counts. test: `tests/workflows/test_pipeline_heartbeat.py::test_stale_task_with_terminal_agent_run_recovered`. behavior: "dispatch_failure_count" absent from `src/gobby/agents/task_recovery.py`.
- 4.1.4 - The web task model has no stage types or automation columns, and the web checks pass. file: `web/src/types/tasks.ts`. behavior: "current_stage" absent from `web/src/types/tasks.ts`. behavior: "include_stages" absent from `web/src/components/activity/TasksTab.tsx`. test: `web/src/lib/__tests__/taskState.test.ts::taskState helpers`.

### 4.2 Task storage drops stages and the automation columns (depends: 4.1) [category: code]
`kind: deliverable`

Targets:
- `src/gobby/storage/tasks/_stage_hydration.py::*` — operation: delete — scope-reason: stage storage goes
- `src/gobby/storage/tasks/_stage_manifest.py::*` — operation: delete — scope-reason: stage storage goes
- `src/gobby/storage/tasks/_stage_registry.py::*` — operation: delete — scope-reason: stage storage goes
- `src/gobby/storage/tasks/_stage_registry_loader.py::*` — operation: delete — scope-reason: stage storage goes
- `src/gobby/storage/tasks/_stage_reviewer_selector.py::*` — operation: delete — scope-reason: stage storage goes
- `src/gobby/storage/tasks/_stage_state_manifest_ops.py::*` — operation: delete — scope-reason: stage storage goes
- `src/gobby/storage/tasks/_stage_state_mutex.py::*` — operation: delete — scope-reason: stage storage goes
- `src/gobby/storage/tasks/_stage_state_rows.py::*` — operation: delete — scope-reason: stage storage goes
- `src/gobby/storage/tasks/_stage_state_transitions.py::*` — operation: delete — scope-reason: stage storage goes
- `src/gobby/storage/tasks/_stage_transition_rules.py` — operation: delete
- `src/gobby/storage/tasks/_stage_states.py::*` — operation: delete — scope-reason: stage storage goes
- `src/gobby/storage/tasks/_stage_types.py::*` — operation: delete — scope-reason: stage storage goes
- `src/gobby/storage/tasks/_stage_views.py::*` — operation: delete — scope-reason: stage storage goes
- `src/gobby/storage/tasks/_stage_utils.py::*` — operation: delete — scope-reason: the close helpers move out and the stage helpers go
- `src/gobby/storage/tasks/_build_cascade.py::*` — operation: delete — scope-reason: cascades the automation columns and stage manifests
- `src/gobby/storage/tasks/_review_transitions.py::*` — operation: delete — scope-reason: the stage review transitions
- `src/gobby/storage/tasks/_review_round_result.py::*` — operation: delete — scope-reason: used only by the review transitions
- `src/gobby/storage/tasks/_plan_enhancement.py::*` — operation: delete — scope-reason: records plan enhancement on a stage
- `src/gobby/storage/tasks/_epic_gate.py::*` — operation: delete — scope-reason: the stage gate of automation candidates
- `src/gobby/storage/tasks/_ancestor_gate.py::*` — operation: delete — scope-reason: the stage gate of automation candidates
- `src/gobby/install/shared/registry/stages.yaml::*` — operation: delete — scope-reason: the bundled stage registry
- `src/gobby/storage/tasks/_close.py`
- `src/gobby/storage/tasks/__init__.py::*` — scope-reason: drop the stage, build cascade and checkout-mode exports
- `src/gobby/storage/tasks/_manager.py::*` — scope-reason: drop the stage managers, manifest setup, build cascade, stage filters and automation column parameters
- `src/gobby/storage/tasks/_models.py::*` — scope-reason: the task model drops the automation columns and the stage attributes
- `src/gobby/storage/tasks/_updates.py::*` — scope-reason: updates drop the automation column parameters
- `src/gobby/storage/tasks/_queries.py::*` — scope-reason: listing and ready queries drop the stage join, stage filters and hydration
- `src/gobby/storage/tasks/_read.py::*` — scope-reason: reads drop stage hydration
- `src/gobby/storage/tasks/_search.py::*` — scope-reason: search drops the stage filter
- `src/gobby/storage/tasks/_aggregates.py::*` — scope-reason: counts drop the stage join and filter
- `src/gobby/storage/tasks/_automation.py::*` — scope-reason: drop automation candidates and keep the stale claim sweep
- `src/gobby/storage/tasks/_transitions.py::*` — scope-reason: reopen and release drop the stage reset and the automation columns
- `src/gobby/storage/tasks/_transitions_facade.py::*` — scope-reason: drop the review transitions, plan enhancement and stage options
- `src/gobby/storage/tasks/_de_escalation.py::*` — scope-reason: de-escalation drops its stage options
- `src/gobby/storage/tasks/_lifecycle_events.py::*` — scope-reason: drop the build-event helpers
- `src/gobby/storage/tasks/_runtime_mutex.py::*` — scope-reason: the runtime lease drops the stage snapshot check
- `src/gobby/storage/tasks/_lifecycle.py::*` — scope-reason: docstrings stop naming review states
- `src/gobby/runner_init/storage.py::*` — scope-reason: startup stops syncing the stage registry
- `src/gobby/plans/review_findings.py::*` — scope-reason: drop the rejection renderer and finding schema that only review transitions used
- `tests/storage/tasks/_stage_test_helpers.py::*` — operation: delete — scope-reason: builds stage rows
- `tests/storage/tasks/stage_test_helpers.py::*` — operation: delete — scope-reason: builds stage rows
- `tests/storage/stage_review_helpers.py::*` — operation: delete — scope-reason: builds stage review rounds
- `tests/storage/tasks/test_aggregates_stage_native.py::*` — operation: delete — scope-reason: tests deleted stage storage
- `tests/storage/tasks/test_search_stage_native.py::*` — operation: delete — scope-reason: tests deleted stage storage
- `tests/storage/tasks/test_cascade_build_state.py::*` — operation: delete — scope-reason: tests the deleted build cascade
- `tests/storage/tasks/test_storage_tasks_cascade.py::*` — operation: delete — scope-reason: tests the deleted build cascade
- `tests/storage/tasks/test_epic_descendant_gate.py::*` — operation: delete — scope-reason: tests the deleted epic gate
- `tests/storage/tasks/test_epic_workspace_bound.py::*` — operation: delete — scope-reason: tests a deleted stage transition bound
- `tests/storage/tasks/test_escalation_preserves_stage.py::*` — operation: delete — scope-reason: tests deleted stage storage
- `tests/storage/tasks/test_manager_exposes_stage_managers.py::*` — operation: delete — scope-reason: tests deleted stage storage
- `tests/storage/tasks/test_stage_manifest_derivation.py::*` — operation: delete — scope-reason: tests deleted stage storage
- `tests/storage/tasks/test_stage_registry.py::*` — operation: delete — scope-reason: tests deleted stage storage
- `tests/storage/tasks/test_stage_state_machine.py::*` — operation: delete — scope-reason: tests deleted stage storage
- `tests/storage/tasks/test_stage_states.py::*` — operation: delete — scope-reason: tests deleted stage storage
- `tests/storage/tasks/test_stage_states_concurrency.py::*` — operation: delete — scope-reason: tests deleted stage storage
- `tests/storage/tasks/test_stage_states_manifest_mutation.py::*` — operation: delete — scope-reason: tests deleted stage storage
- `tests/storage/tasks/test_review_tools_no_legacy_writes.py::*` — operation: delete — scope-reason: tests the deleted review transitions
- `tests/storage/tasks/test_review_tools_pre_phase3_audit.py::*` — operation: delete — scope-reason: tests the deleted review transitions
- `tests/storage/tasks/test_plan_enhancement.py::*` — operation: delete — scope-reason: tests the deleted plan enhancement module
- `tests/storage/tasks/test_close_task_in_txn.py::*` — operation: delete — scope-reason: its contract pins the stage cascade parameter
- `tests/storage/test_stage_registry_loader.py::*` — operation: delete — scope-reason: tests the deleted registry loader
- `tests/storage/test_stage_review_findings.py::*` — operation: delete — scope-reason: tests the deleted review transitions
- `tests/test_startup_seeds_stage_registry.py::*` — operation: delete — scope-reason: tests the deleted registry sync
- `tests/cli/test_build_stage_flags.py::*` — operation: delete — scope-reason: its contract requires the deleted stage manifest spec
- `tests/storage/test_task_search.py::*` — scope-reason: drop the stage filter cases and stage helpers
- `tests/storage/tasks/test_close_eligible_ancestors.py::*` — scope-reason: import from the close module and drop the two stage manifest cases
- `tests/storage/tasks/test_closed_parent_guard.py::*` — scope-reason: import from the close module
- `tests/storage/tasks/test_legacy_validation_criteria_close.py::*` — scope-reason: import from the close module and drop the cascade case
- `tests/storage/tasks/test_reopen_build_state.py::*` — scope-reason: drop the stage reset and automation cases
- `tests/storage/tasks/test_atomic_task_mutations.py::*` — scope-reason: drop the stage manifest and review transition setup
- `tests/storage/tasks/test_list_tasks_hierarchy_paging.py::*` — scope-reason: drop the stage hydration patches
- `tests/storage/tasks/test_ready_query_pagination.py::*` — scope-reason: drop the stage hydration patches
- `tests/storage/tasks/test_readiness_equivalence.py::*` — scope-reason: drop the stage state cases
- `tests/storage/tasks/test_storage_tasks_manager.py::*` — scope-reason: drop the manifest setup cases
- `tests/storage/tasks/test_sweep_stale_claims.py::*` — scope-reason: drop the automation column and candidate setup
- `tests/storage/tasks/test_lifecycle_events.py::*` — scope-reason: drop the build-event cases
- `tests/storage/tasks/test_validation_exit_ramp.py::*` — scope-reason: drop the manifest setup
- `tests/storage/tasks/test_hierarchy_cycle_guards.py::*` — scope-reason: drop the build cascade and manifest cases
- `tests/storage/test_manager_surface_parity.py::*` — scope-reason: drop the build cascade case
- `tests/storage/test_datetime_models.py::*` — scope-reason: row fixtures drop the automation columns
- `tests/mcp_proxy/tools/test_task_lifecycle_coverage.py::*` — scope-reason: drop the stage state helper
- `tests/review_learning/test_round_diff.py::*` — scope-reason: drop the stage approval case
- `tests/workflows/test_condition_helpers.py::*` — scope-reason: drop the stage helper and its cases
- `tests/mcp_proxy/tools/test_claim_task.py::*` — scope-reason: task fixtures drop stages
- `tests/mcp_proxy/tools/test_task_structured_errors.py::*` — scope-reason: task fixtures drop stages
- `tests/mcp_proxy/tools/tasks/test_close_task_flow.py::*` — scope-reason: task fixtures drop stages
- `tests/mcp_proxy/tools/tasks/test_mcp_close_checklist.py::*` — scope-reason: task fixtures drop stages
- `tests/tasks/test_close_checklist.py::*` — scope-reason: task fixtures drop stages
- `tests/workflows/test_observers_detection.py::*` — scope-reason: task fixtures drop stages
- `tests/mcp_proxy/tools/tasks/test_create_task.py::*` — scope-reason: drop the stage row import and assertion
- `tests/agents/test_lifecycle_monitor_watchdog_diagnostics.py::*` — scope-reason: setup drops the stage manifest and review submission
- `tests/plans/test_review_repairs_validation.py::*` — scope-reason: drop the cases for the deleted rejection renderer

**Granularity:** one outcome, task storage holds no stage state and no automation column. `LocalTaskManager` exposes the stage managers, the manifest setup, the build cascade and the column parameters together, and `_manager.py`, `_queries.py` and `_models.py` each carry both, so a split would edit the same files twice for one check.

Move the close helpers out of `_stage_utils.py` into the new module `src/gobby/storage/tasks/_close.py`, and point `_manager.py` and `_transitions.py` at it. `_manager.py` (882 lines) then loses its stage and column code and ends near 775 lines. The move carries `_LEGACY_VALIDATION_CRITERIA`, `_TERMINAL_PARENT_CLOSE_REASONS`, `_session_exists`, `_close_task_in_txn`, `_close_eligible_ancestors`, `close_eligible_parent_chain` and `_schedule_ancestor_epic_archive`, with `_now` replaced by `utc_now`. Three things stay behind with the stage code:

- `_close_task_in_txn` stops calling `_complete_terminal_delivery_stage_for_close` and drops the `cascade_descendants` parameter and `_cascade_close_descendants`. Only `complete_stage` passed `True`.
- `close_eligible_parent_chain` drops the unfinished-stage guard, so a parent with no open child closes as it does today without a manifest.
- `_is_terminal_delivery_stage` goes.

Stage storage:

- Delete the twenty modules above and the bundled `stages.yaml`. `src/gobby/runner_init/storage.py` drops the `StageRegistryLoader` sync, and `__init__.py` drops the `_build_cascade`, `CheckoutMode`, `_stage_registry`, `_stage_states` and `_stage_types` exports.
- `_manager.py`: drop the `_build_cascade`, stage and hydration imports, the `stage_states` and `stages_registry` properties, `initialize_task_manifest`, `cascade_build_state_to_subtree`, the stage parameters of `list_tasks`, `count_tasks` and `search_tasks`, and the hydration call.
- `_queries.py`: delete `_current_stage_state_filter_sql` and `_current_stage_join_sql`; `list_tasks` drops `current_stage_state`, `stages` and `stage_state` with their clauses and hydration; both branches of `_ready_tasks_cte_sql` drop the stage join and predicate. `_aggregates.py` deletes `_current_stage_join_sql` and `_stage_state_filter_clause` and drops the stage parameters of `count_tasks`. `_search.py` drops `current_stage_state` and deletes `_search_with_stage_state`, `_search_postgres_with_stage_state`, `_append_common_filters`, `_append_stage_filter` and `_add_param`. `_read.py` drops its four hydration calls.
- `_transitions_facade.py` deletes `submit_for_review`, `approve_review`, `reject_review` and `record_plan_enhancement` with their imports. `_de_escalation.py` keeps `de_escalate_task` without `reset_stage_attempts` and `restore_stage_from_history`, and deletes `_WORK_ATTEMPT_ESCALATION_SUFFIXES`, the four stage helpers and the `_current_stage_row` import; the facade's `de_escalate_task` drops the same two options.
- `_automation.py` deletes `_is_unattended`, `is_blocked_by_deps`, `list_automation_candidates` and the gate and hydration imports, and keeps `HOLD_LABELS`, `has_hold_label`, `release_task_claim` and `sweep_stale_claims`. 2.4 removed the only production caller of the candidates.
- `_runtime_mutex.py` deletes `CandidateLoader`, `RuntimeStageSnapshotState`, `_ACTIONABLE_STAGE_STATES`, `DispatchCandidateChangedError`, the `expected_stage_*` and `candidate_loader` fields, the snapshot check in `__enter__` and the snapshot methods and functions. `RuntimeDispatchMutex`, `DispatchMutexUnavailableError` and `DISPATCH_TTL_SECONDS` stay for the spawn guard and task recovery, which pass no snapshot. `_dispatch_mutex.py` stays.
- `_lifecycle_events.py` deletes `BUILD_EVENT_REASON` with its comment, `has_build_event` and `tasks_with_build_event` (Decision Record item 12). `_lifecycle.py` docstrings stop naming review states.
- `review_findings.py` deletes `_STRING_SCHEMA`, `_SECTION_SET_SCHEMA`, `FINDING_ITEM_SCHEMA`, `render_rejection_section` and `_render_repair`, whose only caller was `reject_review`.

Automation columns:

- `_models.py`: delete `CheckoutMode` and the `allow_automation`, `unattended`, `checkout_mode` and `dispatch_failure_count` fields with their `__post_init__`, `from_row`, `to_dict` and `to_brief` handling, and the `stages` and `current_stage` attributes.
- `_updates.py` drops the `dispatch_failure_count`, `allow_automation`, `unattended`, `yolo` and `checkout_mode` parameters, their SQL and their pass-throughs, and its refused-field class "stage or ownership fields" becomes "ownership fields". `_manager.update_task` drops the same parameters.
- `_transitions.py`: `release_task_claim` stops resetting `dispatch_failure_count`. `reopen_task` drops the `allow_automation` check, the current-stage reset, `dispatch_failure_count=0` and the reset call, and keeps its dispatch lease and agent run checks. Delete `_active_build_automation_message`, `_current_stage_row` and `reset_current_non_ready_stage`. The refusal reads "Task {ref} has an active agent run or dispatch lease; stop it before reopening it."

Tests:

- Delete the 25 test files above. The close tests import from `gobby.storage.tasks._close`. `test_close_eligible_ancestors.py` deletes `test_parent_with_unfinished_stages_survives_its_last_child` and `test_parent_with_a_finished_manifest_still_closes`; `test_legacy_validation_criteria_close.py` deletes `test_forced_cascade_closes_legacy_descendants`; `test_reopen_build_state.py` deletes `test_reopen_resets_inactive_open_non_ready_stage` and `test_reopen_blocks_allow_automation_with_build_stop_instruction`, and its lease and agent run cases expect the new refusal.
- `test_round_diff.py` deletes `test_approval_evidence_finalization` and the stage review helper imports. `test_manager_surface_parity.py` deletes `test_subtree_cascade_serializes_overlapping_subtrees`. `test_review_repairs_validation.py` deletes its two rejection renderer cases. `test_task_lifecycle_coverage.py` deletes `_make_stage_state` and its stage imports.
- The other test Targets drop stage setup, stage hydration patches, `stages=` fixture fields and automation column fields; their remaining assertions stay.

**Research context:**

- Inventory (2026-10-08): a sweep of `src/` and `tests/` for every symbol, table and parameter above after the 4.1 edits. Outside the files above, `stages=` and `.stages` in `tests/mcp_proxy/tools/spawn_agent/` are `SimpleNamespace` and mock attributes, `handoffs.stages` in `tests/agents/test_spawn_executor_placement_bind.py` is pipeline handoff state, `task_stage_states` in `tests/scripts/test_schema_diff.py` is fixture SQL and `stages.yaml` in `tests/plans/test_semantic_lint.py` is plan text; all stay.
- `tests/phase2_stage_contract_helpers.py` stays for `tests/plans/test_strategy_plan_mcp_split_headers.py`, which pins a completed plan.
- Large files: `_manager.py` (882 lines) takes the move above. No other production Target reaches 850 lines (`_transitions.py` 708, `_queries.py` 589).
- Planned checks: focused pytest on every test Target plus `tests/storage/` and `tests/mcp_proxy/tools/tasks/`; ruff, format check and mypy on `src/`.

**Acceptance:**

- 4.2.1 - Task storage has no stage modules, stage managers or bundled stage registry, and startup syncs none. behavior: "stages_registry" absent from `src/gobby/storage/tasks/_manager.py`. behavior: "StageRegistryLoader" absent from `src/gobby/runner_init/storage.py`. test: `tests/storage/tasks/test_storage_tasks_manager.py::test_create_task_is_metadata_only`.
- 4.2.2 - Closing the last open child closes its eligible ancestors through the close module, with no stage guard. file: `src/gobby/storage/tasks/_close.py`. test: `tests/storage/tasks/test_close_eligible_ancestors.py::test_last_sibling_close_closes_parent`. test: `tests/storage/tasks/test_close_eligible_ancestors.py::test_three_level_last_leaf_closes_phase_and_epic`.
- 4.2.3 - The task model and update path carry no automation column. behavior: "allow_automation" absent from `src/gobby/storage/tasks/_models.py`. behavior: "checkout_mode" absent from `src/gobby/storage/tasks/_updates.py`. test: `tests/storage/tasks/test_storage_tasks_manager.py::test_update_task_persists_normalized_validation_criteria`.
- 4.2.4 - Reopen refuses an active agent run or dispatch lease without naming the build. behavior: "gobby build stop" absent from `src/gobby/storage/tasks/_transitions.py`. test: `tests/storage/tasks/test_reopen_build_state.py::test_reopen_blocks_active_dispatch_mutex`. test: `tests/storage/tasks/test_reopen_build_state.py::test_reopen_blocks_active_agent_run`.

## P5: Guidance and docs
`kind: framing`

After P4 no code path builds, dispatches or reads a stage, but bundled skills and the guides still teach `gobby build`, stage manifests, stage reviews and the automation columns. 5.1 removes that guidance from the bundled skills; 5.2 removes it from the guides, the contracts, the root README and the root instructions. Dated plans, research, spikes and audits stay as history (Context).

### 5.1 Bundled skills teach no build, stage or automation column (depends: 4.2) [category: docs]
`kind: deliverable`

Targets:
- `src/gobby/install/shared/skills/gobby/references/tasks/reviews.md` — operation: delete
- `src/gobby/install/shared/skills/gobby/catalog.json::*` — scope-reason: drop the tasks `reviews` topic
- `src/gobby/install/shared/skills/gobby/references/tasks/overview.md`
- `src/gobby/install/shared/skills/gobby/references/tasks/live-work.md`
- `src/gobby/install/shared/skills/gobby/references/review/evidence.md`
- `src/gobby/install/shared/skills/gobby/references/review/overview.md`
- `src/gobby/install/shared/skills/gobby/references/plan/overview.md`
- `src/gobby/install/shared/skills/annotate/SKILL.md`
- `src/gobby/install/shared/skills/tech-writer/SKILL.md`
- `src/gobby/install/shared/skills/ideate/SKILL.md`
- `src/gobby/install/shared/skills/architecture/SKILL.md`
- `src/gobby/install/shared/skills/prd/SKILL.md`
- `src/gobby/install/shared/skills/research/SKILL.md`
- `src/gobby/install/shared/skills/proportionality/SKILL.md`
- `tests/skills/test_live_session_skill.py::*` — scope-reason: drop the three automation column phrases
- `tests/skills/test_plan_skill_delegated_mode.py::*` — scope-reason: delete the unattended build sequence test
- `tests/skills/test_tasks_skill.py::*` — scope-reason: the tasks overview lists no `reviews` topic

**Granularity:** one outcome, no bundled skill teaches a removed surface. The catalog topic and its file go together because the reference-library contract requires the catalog topics to match the files on disk.

- Delete `src/gobby/install/shared/skills/gobby/references/tasks/reviews.md`, which documents only stage manifests, stage reviews and the stage CLI, and drop its `reviews` topic from the tasks capability in `catalog.json`. 3.5 already dropped the audit entries that cite it, and 3.4 dropped the link from the review outcomes reference.
- `tasks/overview.md`: the topic list drops `reviews`, and "For plan expansion use `$gobby plan`; for dispatch use `$gobby build`." becomes "For plan expansion use `$gobby plan`."
- `tasks/live-work.md`: delete the sentence "Set `allow_automation=false` and `checkout_mode="none"` through `update_task`; confirm `unattended=false` and the returned settings before editing." The next sentence stays.
- `review/evidence.md`: the first paragraph ends "artifact readers; load [source control](../source-control/overview.md) for implementation diffs."
- `review/overview.md`: "`$gobby review <epic-ref>` selects an epic review, independent of `gobby build`." becomes "`$gobby review <epic-ref>` selects an epic review."
- `plan/overview.md`: delete the two sentences "Unattended `gobby build` retains its stage-manifest sequence, installed review policy and configured round counts. Do not inject interactive menus into it."
- `annotate/SKILL.md`: "it does not claim tasks, implement changes, enable dispatch, or run `gobby build`." becomes "it does not claim tasks or implement changes."
- `tech-writer/SKILL.md`: check 5 becomes "Architecture references align with current `CLAUDE.md` and `AGENTS.md` (plan-coverage contract)."
- The methodology skills stay (Decision Record item 4) and stop naming deleted definitions and stages. Descriptions: `ideate` "Internal methodology for ideation.", `architecture` "Internal methodology for architecture work.", `prd` "Internal methodology for a Product Reference Document.", `research` "Internal methodology for research.", each followed by its existing "Produces ..." sentence, where `research` ends "for architecture and PRD work". The role lines become "Use this skill for ideation work.", "Use this skill for architecture work. It covers both marker sections the architecture work owns:", "Use this skill for PRD work." and "Use this skill when acting as the `researcher` agent." In `ideate/SKILL.md`, "downstream stages can rely on" becomes "downstream work can rely on". The discovery marker blocks and the methodology sections stay.
- `proportionality/SKILL.md`: the loader note and the leaf altitude name `task-close-reviewer`, which loads this skill, in place of the deleted `qa-reviewer`.

Tests:

- `test_live_session_skill.py`: `test_live_session_skill_defines_complete_lifecycle_and_recovery` drops "`allow_automation=false`", '`checkout_mode="none"`' and "`unattended=false`" from its expected phrases.
- `test_plan_skill_delegated_mode.py`: delete `test_unattended_build_retains_stage_native_sequence`.
- `test_tasks_skill.py`: `test_core_is_compact_and_keeps_creation_and_exact_close_sequence` checks the topics `creation`, `implementation` and `closing`.

**Research context:**

- Inventory (2026-10-08): `p5/sweep_5.py` over `src/gobby/install/shared/skills/`, `docs/`, the root `AGENTS.md`, `CLAUDE.md` and `README.md` for the build, stage, dispatch, review-state and deleted-definition names. Earlier leaves own the build capability and catalog build entries (2.3), `references/agents/messaging.md` (2.4), `intro/overview.md` (2.3), `plan/enhancement.md` (3.2, 3.5), `plan/review.md` (3.2), `review/epic.md` and `review/outcomes.md` (3.4) and `merge-campaigns.md` (2.2, 3.1).
- Matches that stay: `dispatcher` and `build` in the kotlin, scala, dart and javascript skills (language build tools and event dispatchers), `admin/installation`, `integrations/plugins`, `rules/effects` and the `impeccable` "unattended" wording.
- The discovery skills keep their marker-block steps; `tests/skills/test_discovery_methodology_skills.py::test_discovery_methodology_skill_exists` (from 3.3) checks only the name, `internal: true`, the section heading and the word "methodology".
- Planned checks: focused pytest on the three test Targets plus `tests/skills/test_reference_library.py`, `tests/skills/test_capability_routing.py`, `tests/skills/test_capability_catalog.py` and `tests/skills/test_discovery_methodology_skills.py`.

**Acceptance:**

- 5.1.1 - The tasks capability has no `reviews` topic, and the reference library passes. behavior: "reviews.md" absent from `src/gobby/install/shared/skills/gobby/catalog.json`. test: `tests/skills/test_reference_library.py::test_reference_contract_3_2_1`.
- 5.1.2 - The live-work guidance sets no automation column. behavior: "allow_automation" absent from `src/gobby/install/shared/skills/gobby/references/tasks/live-work.md`. test: `tests/skills/test_live_session_skill.py::test_live_session_skill_defines_complete_lifecycle_and_recovery`.
- 5.1.3 - No skill names `gobby build` or a deleted definition as its user. behavior: "gobby build" absent from `src/gobby/install/shared/skills/gobby/references/plan/overview.md`. behavior: "qa-reviewer" absent from `src/gobby/install/shared/skills/proportionality/SKILL.md`. behavior: "analyst" absent from `src/gobby/install/shared/skills/ideate/SKILL.md`. test: `tests/skills/test_discovery_methodology_skills.py::test_discovery_methodology_skill_exists`.

### 5.2 Guides, contracts and root docs teach no build, stage or dispatcher (depends: 5.1) [category: docs]
`kind: deliverable`

Targets:
- `docs/guides/dispatch.md` — operation: delete
- `README.md`
- `AGENTS.md`
- `docs/architecture/source-tree.md`
- `docs/contracts/plan-coverage.md`
- `docs/guides/README.md`
- `docs/guides/orchestration.md`
- `docs/guides/tasks.md`
- `docs/guides/task-expansion.md`
- `docs/guides/workflows-overview.md`
- `docs/guides/cli-commands.md`
- `docs/guides/http-endpoints.md`
- `docs/guides/agents.md`
- `docs/guides/pipelines.md`
- `docs/guides/plans-and-plan-mode.md`
- `docs/guides/spec-writing.md`
- `docs/guides/worktrees.md`
- `docs/guides/configuration.md`
- `docs/guides/cron-scheduler.md`
- `docs/guides/observability.md`
- `docs/guides/search.md`
- `docs/guides/tdd-enforcement.md`
- `docs/reference-audit/tasks.json::*` — scope-reason: drop the removed task guide anchors and rename the HTTP tasks anchor
- `docs/reference-audit/plan.json::*` — scope-reason: rename the HTTP and spec-writing anchors
- `docs/reference-audit/agents.json::*` — scope-reason: rename the HTTP agents anchor
- `docs/reference-audit/pipelines.json::*` — scope-reason: rename the pipelines boundary anchor
- `docs/reference-audit/rules.json::*` — scope-reason: drop the workflows-overview dispatch anchor
- `docs/reference-audit/source-control.json::*` — scope-reason: rename the worktrees isolation anchor

**Granularity:** one outcome, no guide, contract or root document teaches a removed surface. The audit files go with the guides because the reference-library contract fails on any audited anchor the guides no longer have.

Delete `docs/guides/dispatch.md`. Its inbound links all sit in files this deliverable edits: `README.md`, `docs/guides/README.md`, `cli-commands.md`, `configuration.md`, `cron-scheduler.md`, `orchestration.md`, `task-expansion.md` and `worktrees.md`. 2.5 deleted the stage-list skill test, its only test reader.

Root documents:

- `README.md`: the hero command becomes `gobby install`, and "That's the loop. Hand Gobby a task, walk away, come back to a PR." becomes "Every detected CLI shares one daemon: sessions, task ledger, memory and hook-time rules." The next paragraph becomes "Behind that one command: a task ledger with validation gates and commit-linked close, spawned agents in isolated worktrees, hook-time rules, and handoffs across sessions and CLIs. If something goes off the rails, Gobby stops and escalates instead of merging garbage." "Gobby built Gobby" and "What shipped in 0.4.x" stay as release history. Section 1 is retitled "### 1. Hook-time rules", loses the paragraph that starts "Gobby splits the runtime in two", starts its rule paragraph "Inside a session or spawned worker", and loses "That's the only way `gobby build` gets to "hands-off" without lying about it." In section 2, "The repo you're reading was built through its own build loop. 5K+ commits. 15K+ tasks. 0.4.x was assembled by spawned agents working through staged manifests, with the dispatcher routing review and merge." becomes "The repo you're reading was built with Gobby. 5K+ commits. 15K+ tasks.", and the test-bed sentence names "hooks, isolation, or task lifecycle" and ends "shows up in the next morning's work." Delete the section "## How `gobby build` actually works" with its trailing rule. The guide list drops the `dispatch.md` line; `orchestration.md` reads "agents, isolation, spawn cap" and `workflows-overview.md` reads "rules, agents, pipelines". Quick start: "Then either start interactive work in your CLI of choice — Gobby will track it quietly — or hand it a task and let the build loop run:" becomes "Then start work in your CLI of choice; Gobby tracks it quietly. Create a task for an agent to claim:", and the code block drops `gobby build '#<id>'`.
- `AGENTS.md`: delete the Agent Task Workflow bullet "Hand a stage to review with `gobby-tasks-ops` tools such as `submit_for_review(stage_name=...)`." 2.4 and 2.5 made the other edits.
- `docs/architecture/source-tree.md`: drop the `build/` and `dispatch/` package entries, the `registry/` line (2.5 and 4.2 deleted both files), `build_history.py` and `build_profiles.py` from the storage line, and `dispatch/dispatcher.py`, `dispatch/rules.py` and `build/service.py` from the runtime tree.

Contracts:

- `docs/contracts/plan-coverage.md`: delete list item 8, which names the `expansion-qa` agent definition. "The evidence-round protocol in this contract remains the path for spawned taskless reviewers and `gobby build` stage reviews." becomes "The evidence-round protocol in this contract serves the static-seat rounds." "Manual expansion registers against its real epic; `gobby build` creates or reuses its real root and preserves its established unattended stage-manifest sequence." becomes "Manual expansion registers against its real epic." "An approving `plan-adversary-taskless-old` run" becomes "An approving `plan-adversary` review". The enhancement paragraph opens "A constructive `plan-enhancer` pass is recommended and optional for interactive planning." and keeps its sentences from "It loads". The `needs_review` checkpoint in the M1 section names plan evidence and stays. In the parse-mode table, "before every adversary spawn" becomes "before every review round" and "taskless adversary/coordinator post-approval self-check" becomes "adversary/coordinator post-approval self-check". "parses in `draft` mode before each taskless adversary spawn." becomes "parses in `draft` mode before each review round."
- `docs/contracts/prompt-style.md` stays: its examples are labelled as the fleet before that contract.

Guides:

- `orchestration.md` is rewritten around spawned agent work. The intro reads "Gobby orchestration is the runtime coordination around spawned agent work: agents claim tasks, work in isolation and close through the task lifecycle. This guide explains how tasks, agents, isolation and the spawn cap fit together." Delete Build Lifecycle, Active Surfaces, Build State, Dispatch Actions and Stage Reviews. Agent Work opens "Spawned workers operate through MCP lifecycle tools", and step 5 becomes "Calls `close_task` with the commit SHA." Runtime Tables drops `task_stage_states`; `task_dispatch_mutex` reads "Short-lived per-task leases that serialize task-bound spawns" and `task_lifecycle_events` reads "Append-only audit of task lifecycle changes". Lifecycle Events drops its dispatch paragraph. Agent Slot Cap reads "`spawn_agent` refuses a spawn when the project already has `max_active_agents` active agents. The daemon config key defaults to 20; see [Configuration](./configuration.md#spawn-cap). `can_spawn_agent` reports the same check, and `dispatch_batch` bounds its concurrency with the same value." Related Guides drops Dispatch.
- `tasks.md`: the intro drops "stage manifests" and "review state". The Current Model diagram keeps the task, dependency, owner session, linked commit, closed and escalated nodes and drops the manifest and stage-state nodes. The state list becomes `ready` (open and not escalated), `closed` and `escalated`, with "A claim records the owner session." The lifecycle tools sentence names `claim_task` and `close_task`. Delete the stage close paragraph and its two examples, the "## Stage Manifests" section, readiness item 2 (renumber the rest), the `current_stage_state` filter in the `list_tasks` example ("Use `list_tasks` for metadata filters:"), and the CLI lines `gobby tasks list --stage development --state in_progress`, `gobby tasks stages`, `gobby tasks advance`, the three `gobby tasks review` lines and `gobby tasks repair-lifecycle`. The search examples use "close validation" in place of "stage manifest". Both `changes_summary` examples read "Refreshed the task guide for the MCP-first task flow." The CLI reference drops `[--stage NAME --state STATE]` and `[--checkout-mode MODE]`, its comment reads "# Dependencies and labels", and the `MODE` paragraph goes. Automation Notes opens "Lifecycle automation and workflow rules operate on semantic events".
- `task-expansion.md`: the intro drops "stage manifests" and its dispatch sentence becomes "For spawned agent work after expansion, see [Orchestration](./orchestration.md)." The diagram's tree node reads "Task tree<br/>dependencies" and the `DISPATCH` node goes. Flow step 4 drops "initializes child stage manifests," and step 6 goes. The apply list drops items 1 and 10, and item 8 becomes "Copies the parent's target branch to created leaves." Delete "Build controls enhancement through `--plan-enhancement-rounds`; inspect the selected profile and stage settings in the [orchestration guide](./orchestration.md#build-lifecycle)." The sequence participant becomes `plan-enhancer`. Delete "### Via Lifecycle Automation". See Also drops Dispatch, and Orchestration reads "Spawned agent work and isolation".
- `workflows-overview.md`: the intro ends "a rule, an agent definition, or a pipeline." The Mental Model has four layers: drop the Dispatch row, say "four" in both places, and the completion-ID bullet names agents and pipelines. Delete "### Dispatch". Decision Guide: the spawn row reads "| Spawn child workers for ready tasks | Pipeline | Runbooks launch seats through `spawn_agent` |", the heartbeat row goes, and the approval row reads "Run a deterministic approval sequence". Event Flow step 5 opens "Agents, pipelines, or explicit callers use MCP tools". Runtime state drops "task stage manifests,".
- `cli-commands.md`: the command table drops the `build`, `profiles` and `stages` rows, and `cron` reads "Manage scheduled jobs.". Delete "## Build Automation" with "### Profiles And Stage Defaults", and "### Stages And Review". Task listing drops `gobby tasks stages TASK` and the `--stage` and `--state` options; `tasks update` drops `--checkout-mode`; maintenance drops `gobby tasks repair-lifecycle`.
- `cli-commands.md` wording: "Agents use `gobby-tasks` MCP lifecycle tools for claims and closure, and `gobby-tasks-ops` for authorized stage review transitions." becomes "Agents use `gobby-tasks` MCP lifecycle tools for claims and closure." "The global collaboration profile is `USER.md`, not a build profile." becomes "The global collaboration profile is `USER.md`."
- `http-endpoints.md`: the not-exhaustive note reads "(for example `/api/llm`, `/api/embeddings`, and chat attachments)". "## Tasks And Stages" becomes "## Tasks" and drops the stage, stage registry and task-type default rows. The PATCH paragraph ends its field list at `validation_criteria` and drops the `checkout_mode` and retargeting sentences. "## Agents And Build Automation" becomes "## Agents" and drops the `/api/build` rows, the `POST /api/build` and build control paragraphs and the build profile route table.
- `agents.md`: the model example's `fallback_agent` is `"developer"`. The step-workflow example's `on_mcp_success` names server `gobby-tasks`, tool `close_task` and variable `task_closed`, and its transition reads `when: "vars.task_closed"`. The messaging paragraph drops the `build` target from both sentences. The spawn-scope example row is `[developer]`. "(build dispatch, close validation)" becomes "(close validation)", and "and dispatch provides the concrete worktree or clone context" becomes "and the spawn provides the concrete worktree or clone context". Related Guides: Orchestration reads "agent work, isolation and the spawn cap".
- `pipelines.md`: the cross-link reads "For spawned agent work, see [Orchestration](./orchestration.md)." Delete "Use dispatch when you need to advance task lifecycle stages." The example `reviewer_agent` is `"task-close-reviewer"`. "## Dispatch Boundary" becomes "## When To Use Pipelines": delete its first paragraph and the "Use dispatch for:" list, and its last paragraph drops the clause about dispatch rules. Related Guides: Orchestration reads "spawned agent work and isolation".
- `plans-and-plan-mode.md`: the roles read "the **plan-writer** drafts and folds in changes, the optional **plan-enhancer** proposes". Optional Enhancement opens "After materialization and base validation, `/gobby plan` offers an optional enhancement loop. When selected, the planning runbook's live `plan-enhancer` seat loads `gobby:references/plan/enhancement.md` and standalone `proportionality`, then sends ranked Better/Bigger suggestions to the Writer through `send_message`. It never claims tasks, edits the plan file, or calls a review verdict." Optional Adversarial Review opens "Open a review round with `prepare_plan_review_round`, then bind it once with `bind_static_review_seats`:" followed by the existing text from "the evidence owner names", with "the binding is immutable and exclusive with a run binding" shortened to "the binding is immutable". "After user approval, offer either manual expansion or build:" becomes "After user approval, offer manual expansion.", and the build command block goes. "The evidence-round protocol above stays the contract for spawned taskless reviewers and `gobby build` stage reviews." becomes "The evidence-round protocol above stays the contract for static-seat rounds." "Manual expansion registers against its real epic; `gobby build` creates or reuses the real root and preserves its configured unattended stage sequence." becomes "Manual expansion registers against its real epic." "enhancement and taskless adversarial review are recommended but optional" becomes "enhancement and adversarial review are recommended but optional". In Optional Adversarial Review, "returns structured findings or approval to the parent, and calls `end_agent_run`." becomes "sends structured findings or approval to the Writer and coordinator seats through `send_message`."
- `spec-writing.md`: delete "Use `gobby build <plan-file> --checkout-mode none` to start lifecycle automation from an approved plan file." "## Build And Lifecycle Notes" becomes "## Lifecycle Notes" and loses its first paragraph. Its docs sentence becomes "Docs-category leaf tasks route to `tech-writer` and may run inside the parent epic's isolation context."
- `worktrees.md`: "## Worktrees And Task Automation" becomes "## Worktrees And Task Isolation". Its first paragraph becomes "Spawned agents can select `none`, `worktree`, or `clone` isolation. Task artifacts are the source of truth for the worktree ID, clone ID, and target branch." See Also drops `dispatch.md`.
- `configuration.md`: delete "Task lifecycle automation is stage-manifest based." See Also drops `dispatch.md`.
- `cron-scheduler.md`: "Handler and legacy dispatcher actions are internal implementation paths, not general agent creation choices. Use `gobby build` for task dispatch." becomes "Handler actions are internal implementation paths, not general agent creation choices." "Internal dispatcher and pipeline-heartbeat automation" becomes "Internal pipeline-heartbeat automation". The System Automation paragraph reads "Pipeline-heartbeat automation lives in `SystemAutomationLoop`, controlled by `system_loops.automation.enabled` and `system_loops.automation.interval_seconds`. The loop runs daemon-owned maintenance and pipeline heartbeat checks without creating cron history rows." The legacy-row paragraph stays. The source list reads "daemon-owned maintenance and heartbeat loop". See Also drops `dispatch.md`.
- `observability.md`: `automation.log` covers "Scheduler, system automation, and pipeline-heartbeat behavior"; the metric bullet drops the `dispatcher` outcomes; "individual scheduler and dispatcher decisions" becomes "individual scheduler decisions".
- `search.md`: drop both `current_stage_state` example lines and "stage state," from the filter list.
- `tdd-enforcement.md`: "`qa-reviewer` checks TDD-required leaves before approval." becomes "Leaf close review (`task-close-reviewer`) checks TDD-required leaves before approval."
- `docs/guides/README.md`: delete "It is separate from the build profiles managed by `gobby profiles`." and the `dispatch.md` row. `orchestration.md` reads "Spawned agent work, isolation, and the spawn cap"; `cron-scheduler.md` reads "Cron jobs, run history, and scheduler execution". The reading path reads "Read [orchestration.md](orchestration.md) for spawned agent work." and "Read [cron-scheduler.md](cron-scheduler.md) for scheduled pipeline work."

Audits (each anchor follows its heading):

- `tasks.json`: drop `stage-manifests` (`tasks.md`) and `stages-and-review` (`cli-commands.md`); `tasks-and-stages` (`http-endpoints.md`) becomes `tasks`.
- `plan.json`: `tasks-and-stages` becomes `tasks`, `agents-and-build-automation` becomes `agents`, and `build-and-lifecycle-notes` (`spec-writing.md`) becomes `lifecycle-notes`.
- `agents.json`: `agents-and-build-automation` becomes `agents`.
- `pipelines.json`: `dispatch-boundary` becomes `when-to-use-pipelines`.
- `rules.json`: drop `dispatch` (`workflows-overview.md`).
- `source-control.json`: `worktrees-and-task-automation` becomes `worktrees-and-task-isolation`.

**Research context:**

- Inventory (2026-10-08): `p5/sweep_5.py` over `docs/`, the root `AGENTS.md`, `CLAUDE.md` and `README.md`, then a case-insensitive `dispatch` and `heartbeat` pass over the guides it hit. Earlier leaves own `configuration.md`'s spawn cap section (1.1), the messaging rule in `AGENTS.md` (2.4), the build command and dispatch fact in `AGENTS.md` and `CLAUDE.md` (2.5) and every audit entry that cites a deleted tool or file (2.3, 3.2, 3.5).
- Matches that stay: hook dispatchers (`ghook` guides, `hook-schemas.md`, `rules.md`, the README CLI table), webhook dispatchers, the `unattended` wording in `secrets.md` and `sandboxing.md`, `dispatch_batch`, `task_dispatch_mutex` and the runbook "dispatch mutex" in `agents.md`, `gcode-development-guide.md`'s `build`, and the history files named in Context.
- Anchors: the reference-library contract checks every audited anchor against the guide headings and link-checks every audited guide (`documentation_errors` in `tests/skills/reference_library_helpers.py`). `http-endpoints.md` has no other `## Tasks` or `## Agents` heading, so the renamed anchors are unique. No skill or guide links to a removed anchor after 2.3 deletes the build references and this deliverable deletes the `task-expansion.md` link.
- No test reads the edited prose except the audits. `tests/servers/routes/mcp_endpoints/test_template_routes.py` reads `http-endpoints.md` only for the MCP template routes, and `tests/docs/test_claude_md_contract_section.py` pins plan-coverage terms this deliverable keeps.
- Planned checks: `tests/skills/test_reference_library.py`, `tests/docs/`, `tests/servers/routes/mcp_endpoints/test_template_routes.py`, `tests/skills/test_capability_routing.py` and a Mermaid render of the edited `tasks.md`, `task-expansion.md` and `orchestration.md` diagrams.

**Acceptance:**

- 5.2.1 - The dispatch guide is gone, and the reference library passes with every audit anchor following its renamed or removed heading. behavior: "dispatch.md" absent from `docs/guides/README.md`. behavior: "tasks-and-stages" absent from `docs/reference-audit/tasks.json`. test: `tests/skills/test_reference_library.py::test_reference_contract_3_2_1`.
- 5.2.2 - The root README and instructions teach no build loop or stage review. behavior: "let the build loop run" absent from `README.md`. behavior: "submit_for_review" absent from `AGENTS.md`.
- 5.2.3 - The task, CLI and HTTP guides document no stage manifest, stage command or build route. behavior: "## Stage Manifests" absent from `docs/guides/tasks.md`. behavior: "gobby tasks advance" absent from `docs/guides/cli-commands.md`. behavior: "/api/build" absent from `docs/guides/http-endpoints.md`.
- 5.2.4 - The orchestration guide documents the spawn cap as daemon config. behavior: "max_active_agents" in `docs/guides/orchestration.md`. behavior: "dispatch.md" absent from `docs/guides/orchestration.md`.
- 5.2.5 - The plan contract and planning guide name no taskless or `-old` definition and no spawned reviewer run. behavior: "taskless-old" absent from `docs/contracts/plan-coverage.md`. behavior: "taskless adversary" absent from `docs/contracts/plan-coverage.md`. behavior: "enhancer-old" absent from `docs/guides/plans-and-plan-mode.md`. behavior: "end_agent_run" absent from `docs/guides/plans-and-plan-mode.md`.

## P6: Schema drop
`kind: framing`

After P5 nothing reads or writes the build and stage tables or the automation columns. 6.1 drops them in one gcore migration with its derived carriers (Decision Record items 5 and 10 to 12). The Orchestrator then runs the single rebuild, promotion and restart (Constraints).

### 6.1 Migration 465 drops the build and stage tables and the automation columns (depends: 5.2) [category: code]
`kind: deliverable`

Targets:
- `crates/gcore/assets/schema/migrations/465_retire_build_and_stages.sql`
- `crates/gcore/src/schema/assets.rs::*` — scope-reason: register migration 465
- `crates/gcore/assets/schema/catalog.manifest.json::*` — scope-reason: regenerated derived carrier without the dropped objects
- `crates/gcore/assets/schema/seed.manifest.json::*` — scope-reason: drop the two stage seed tables
- `crates/gcore/src/grant/bundle.rs::*` — scope-reason: the embedded schema identity moves to 465
- `crates/gcore/src/schema/verify.rs::*` — scope-reason: drop the live-mutable fields of the two stage seed tables
- `crates/gcore/src/schema/runner_tests.rs::*` — scope-reason: the 460 test applies through 460 only, and a new test covers 465
- `crates/gcore/tests/schema_contract.rs::*` — scope-reason: the latest embedded asset is 465
- `crates/gdaemon/tests/cli_contract.rs::*` — scope-reason: the reported latest version is 465
- `src/gobby/storage/schema_expected_identity.json::*` — scope-reason: regenerated identity projection
- `tests/contracts/http/runtime_handshake.json::*` — scope-reason: the handshake reports the new identity
- `tests/runtime_grants/golden/brokered_datastores.json::*` — scope-reason: the grant fixture carries the new identity
- `tests/runtime_grants/golden/direct_datastores.json::*` — scope-reason: the grant fixture carries the new identity
- `tests/runtime_grants/golden/old_client_new_grant.json::*` — scope-reason: the grant fixture carries the new identity
- `tests/runtime_grants/golden/payload_skew_unknown_field.json::*` — scope-reason: the grant fixture carries the new identity
- `tests/runtime_grants/golden/unavailable_datastores.json::*` — scope-reason: the grant fixture carries the new identity
- `scripts/flatten_schema.py`
- `scripts/schema_diff.py`
- `scripts/schema_sql_lexer.py`
- `tests/fixtures/postgres.py::*` — scope-reason: the seed docstring drops the stage tables
- `tests/fixtures/test_postgres_db_reset.py::*` — scope-reason: the seed revert case mutates a projects row

**Granularity:** one outcome, the schema holds no build or stage table and no automation column. The migration, its registration and every carrier that hashes or lists the embedded assets change together, because the gcore contract tests fail on any partial set.

`crates/gcore/assets/schema/baseline.sql` stays frozen; the migration carries the whole drop. `465_retire_build_and_stages.sql`:

```sql
DROP TABLE task_stage_states;
DROP TABLE task_type_default_stages;
DROP TABLE task_stages_registry;
DROP TABLE build_history_events;
DROP TABLE build_runs;
DROP TABLE build_profiles;
DROP FUNCTION refresh_task_state_bucket_from_stage();
DROP INDEX idx_tasks_dispatch_scan;
ALTER TABLE tasks
    DROP COLUMN allow_automation,
    DROP COLUMN unattended,
    DROP COLUMN checkout_mode,
    DROP COLUMN dispatch_failure_count;
CREATE OR REPLACE FUNCTION compute_task_state_bucket(p_task_id uuid) RETURNS text
    LANGUAGE sql STABLE
    AS $$
    SELECT CASE
        WHEN t.closed_at IS NOT NULL THEN 'closed'
        WHEN t.escalated_at IS NOT NULL OR COALESCE(t.is_escalated, FALSE) IS TRUE THEN 'escalated'
        ELSE 'ready'
    END
    FROM tasks t
    WHERE t.id = p_task_id
$$;
ALTER TABLE tasks DROP CONSTRAINT tasks_state_bucket_check;
UPDATE tasks SET state_bucket = 'ready'
 WHERE state_bucket NOT IN ('ready', 'closed', 'escalated');
ALTER TABLE tasks ADD CONSTRAINT tasks_state_bucket_check
    CHECK (state_bucket = ANY (ARRAY['ready'::text, 'closed'::text, 'escalated'::text]));
```

Dropping `task_stage_states` drops its three bucket triggers before their function goes. Dropping the columns drops their check constraints (`tasks_allow_automation_check`, `tasks_unattended_check`, `tasks_checkout_mode_check`). A task whose bucket came from a stage row is open and not escalated, so `ready` is its recomputed bucket; the `UPDATE` touches only those rows, and no `tasks` trigger fires on `state_bucket`. `refresh_task_state_bucket` and the two `tasks_state_bucket_*` triggers stay. `task_lifecycle_events` and `task_dispatch_mutex` stay (Decision Record items 5 and 12).

Carriers, following the 462 and 464 commits:

- `assets.rs` appends an `EmbeddedMigration` for version 465 with its filename, the file's SHA-256 checksum and `include_str!`.
- `seed.manifest.json` drops the `task_stages_registry` and `task_type_default_stages` keys; `projects` and `sessions` stay. `verify.rs`'s `is_live_mutable_seed_field` drops the same two arms.
- Regenerate `catalog.manifest.json` with `UPDATE_GCORE_SCHEMA_MANIFEST=1 GOBBY_SCHEMA_TEST_DATABASE_URL=<isolated test hub> cargo test -p gobby-core --test catalog_manifest_freshness catalog_manifest_is_fresh_for_embedded_assets`, then rerun it without `UPDATE_GCORE_SCHEMA_MANIFEST`.
- Read the new identity from `cargo run -p gobby-daemon --bin gdaemon -- schema version --json`. `bundle.rs` takes the new latest checksum, assets root hash and `latest_version: 465`; `schema_contract.rs` asserts version 465 and filename `465_retire_build_and_stages.sql`; `cli_contract.rs` asserts `latest_version` 465. Regenerate `schema_expected_identity.json` with `uv run python scripts/generate_schema_expected_identity.py --gdaemon target/debug/gdaemon`, and update the identity fields in `runtime_handshake.json` and the five golden grant files.
- `runner_tests.rs`: `checkout_mode_migration_preserves_modes_and_agent_settings` replaces both of its full `SchemaRunner::new(...).apply()` calls with `SchemaRunner::with_migrations_for_test(&mut client, "public", &MIGRATIONS[..=migration_index])`, so it applies through 460 and still reads `checkout_mode`. New test `build_and_stage_retirement_rebuckets_tasks_and_drops_tables` applies through 464, inserts a task with an `in_progress` stage row (its bucket becomes `in_progress`), applies every migration, and asserts the six tables and four columns are gone, `refresh_task_state_bucket_from_stage()` is gone, the task's bucket is `ready`, and an `in_progress` bucket violates the check.
- `scripts/flatten_schema.py`'s `_SEED_TABLES` drops the two stage tables. `scripts/schema_diff.py` drops `_STAGE_REGISTRY_DEFINITION_COLUMNS` and the two stage `SeedTableSpec` entries.
- Split `scripts/schema_diff.py`: move `_split_sql_statements`, `_split_top_level_commas` and the scanners they call (`_skip_line_comment`, `_skip_block_comment`, `_skip_single_quoted_string`, `_skip_double_quoted_identifier`, `_dollar_quote_tag_at`, `_is_identifier_start` and `_is_identifier_continuation`, lines 82-203 and 218-250 today) unchanged into the new module `scripts/schema_sql_lexer.py`. `scripts/schema_diff.py` imports the two splitters back through the `if __package__` import form that `scripts/flatten_schema.py` uses, so every importer of `scripts.schema_diff` keeps its imports. With the stage removals the file ends near 710 lines.
- `tests/fixtures/postgres.py`: the canonical seed docstring names `projects` and `sessions`. `test_postgres_db_reset.py::test_seed_rows_survive_reset` mutates the `name` of a seeded `projects` row in place of the `task_type_default_stages` position, asserts the reset restores it, and its docstring and its `postgres_canonical_seed` assertion name `projects`.

**Research context:**

- Carrier set from the 462 (`6926cd6352`) and 464 (`5596ae1394`) commits. The latest migration is 464. `crates/gdaemon/Cargo.toml` and `src/gobby/install/version_pins.py` are version bumps that 462 did without, so 6.1 leaves them to the Orchestrator's activation.
- Inventory (2026-10-08): `p6/sweep_6.py` over `src/`, `tests/`, `scripts/` and `crates/gcore/` for the dropped tables, columns, index and functions, minus earlier-leaf Targets, plus `git grep` over `crates/`, `scripts/`, `tests/fixtures/`, `tests/contracts/` and `tests/runtime_grants/` for the four column names. `tests/scripts/test_schema_diff.py` stays because its `task_stage_states` hits are synthetic fixture SQL.
- Large files: `scripts/schema_diff.py` (896 lines) needs the split above. `scripts/` is outside the code index, and the delete-lines proof needs a `::*` Target, which the validator refuses for an unindexed file. The moved block has no caller in `schema_diff.py` outside the two splitters, and its test module imports none of the moved names. `runner_tests.rs` is a test module and `verify.rs` has 722 lines.
- Planned checks: `GOBBY_SCHEMA_TEST_DATABASE_URL=<isolated test hub> cargo test -p gobby-core` and `cargo test -p gobby-daemon --test cli_contract`; focused pytest on `tests/fixtures/test_postgres_db_reset.py`, `tests/scripts/`, `tests/storage/test_schema_contract.py`, `tests/runtime_grants/` and `tests/contracts/`; ruff and format check on `scripts/schema_diff.py`, `scripts/schema_sql_lexer.py` and `scripts/flatten_schema.py`.

**Acceptance:**

- 6.1.1 - Migration 465 drops the six build and stage tables, the four automation columns and the stage bucket function, and re-buckets stage-derived tasks to `ready`. file: `crates/gcore/assets/schema/migrations/465_retire_build_and_stages.sql`. test: `crates/gcore/src/schema/runner_tests.rs::build_and_stage_retirement_rebuckets_tasks_and_drops_tables`.
- 6.1.2 - The 460 migration test still proves its column rename by applying through 460 only. test: `crates/gcore/src/schema/runner_tests.rs::checkout_mode_migration_preserves_modes_and_agent_settings`.
- 6.1.3 - The embedded identity, the catalog manifest and every identity carrier agree on 465. test: `crates/gcore/tests/schema_contract.rs::embedded_assets_publish_a_complete_schema_identity`. test: `crates/gcore/tests/catalog_manifest_freshness.rs::catalog_manifest_is_fresh_for_embedded_assets`. test: `crates/gdaemon/tests/cli_contract.rs::version_json_reports_exact_schema_identity_contract`.
- 6.1.4 - Seed verification and the test reset know only the `projects` and `sessions` seed tables. behavior: "task_stages_registry" absent from `crates/gcore/assets/schema/seed.manifest.json`. test: `tests/fixtures/test_postgres_db_reset.py::test_seed_rows_survive_reset`.

## V2: Verification
`kind: verification`

These are completion gates for the implementation. Except for plan validation, none has run yet. Each runs after its leaves land, and each must hold before the epic closes.

- Plan validation is the only planning-time check: `uv run gobby plans validate .gobby/plans/remove-gobby-build.md -p <project-root>` must report no issue, and the expansion-mode run must pass before expansion.
- Each leaf's focused pytest, run with `GOBBY_TEST_PROTECT=1` and the isolated test hub, must pass for every test Target and planned check it names.
- `uv run ruff check src/`, `uv run ruff format --check src/` and `uv run mypy src/` must pass after every code leaf.
- The web checks named in 2.1 and 4.1 must pass after those leaves.
- `GOBBY_SCHEMA_TEST_DATABASE_URL=<isolated test hub> cargo test -p gobby-core` and `cargo test -p gobby-daemon --test cli_contract` must pass after 6.1.
- `tests/skills/test_reference_library.py::test_reference_contract_3_2_1` must pass after every leaf that edits a skill, guide or audit.
- After 6.1 lands, the Orchestrator's rebuild, promotion and restart must start the daemon at schema 465, and `gobby status` must report healthy.
- A final sweep of `src/`, `tests/`, `web/src/`, `crates/gcore/src/` and the live guides for `gobby build`, `task_stage_states`, `allow_automation` and `submit_for_review` must find only the history named in Context.

## V1 Plan Changelog
`kind: framing`

- 2026-10-08: First draft by Plan Writer gobby#15528.
- 2026-10-08: Writer repairs for Plan Adversary gobby#15401 F1 (3.2 review guidance, plus the matching contract and guide text in 5.2) and F2 (3.3 keeps the discovery methodology skill test).
- 2026-10-08: Writer repair for F3: 1.1 and 2.4 re-record the HTTP config corpus, and 2.4 names its new loop descriptions.
