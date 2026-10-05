Plan artifact: `.gobby/plans/workspace-index-pin.md`

# Pin workspace indexes to the selected fork commit

**Plan ID:** workspace-index-pin
**Implementation root:** #23433 (workspace index pin and spawn cost)
**Status:** Accepted design; implementation proceeds in independently verifiable deliverables.

## R1 Decision record
`kind: framing`

A default worktree or local clone forks the caller checkout's local HEAD, including
unpushed commits, without fetching; a detached caller HEAD requires an explicit
base, because the recorded base branch is the workspace's landing target and
deletion-safety merge reference, and a detached HEAD names none. An explicit local branch selects its local tip;
an explicit remote branch fetches and selects its latest tip. The selected commit is
resolved before creating or refreshing a workspace. An existing branch may not
silently substitute another commit. A reused agent worktree rebases its work onto
the selected commit and receives a new pin; an integration workspace refresh keeps
its merged commits. External URL clones have no live ancestor index and cold index.

The pin is a workspace-owned snapshot of effective file selectors, each naming a
source project, path, and content hash. Only clean indexed files whose recorded Git
blob ID equals the selected commit's tree entry qualify. Dirty, untracked, ignored,
or mismatched ancestor paths become explicit child indexing gaps. A selected commit
with no matching live indexed checkout has no inherited rows and uses the cold path.
Ask continues to read the caller's live index; its commit IDs do not select an index.

The pin governs every read lane, including graph and vector, and remains a
content-GC root until the child overlay is purged. When a source projection moves,
stale graph and vector rows are excluded immediately, and the existing daemon
projection worker materializes only the missing pinned paths, at their pinned
versions, into the child's projection namespace.

## R2 Constraints and evidence
`kind: framing`

The 2026-10-04 observations were 34.9 s and 71.6 s in `code_index_index`, after a
15.3 s observation on 2026-09-28. The 2 s figure is a soft comparison target.
Memory 9644c698 requires a load-matched comparison before attributing a slowdown
to host load. Memory f1e1200c requires reused worktree conflicts to preserve
continuation ancestry and return a recoverable error.

Idle baseline, observed on #23433 at 2026-10-05 02:17 CDT (run
`d50ddf68-0066-4ce2-b39f-316bdbc36b41`): a Claude worktree spawn with base branch
`0.5.0`, forked at `084f759f15`, no other agents running, a healthy parent index at
that commit, and no content difference from the indexed parent commit. The
`spawn_agent` call took 33.9 s wall. `phase_timings_ms` recorded
`code_index_status` 979 ms, `code_index_index` 7,064 ms,
`code_index_search_content` 192 ms, `_preflight_srt` 1,234 ms, and every other
timed phase under 100 ms; the timed phases sum to 9.7 s, leaving about 24 s in no
timed phase. The daemon log timeline: 02:17:35 worktree start, 02:17:38 isolation
sidecar written, 02:17:58 MCP config written, 02:18:08 SRT verification,
02:18:09 Claude trust pre-approved and timings logged. The parent index's
`last_indexed_at` advanced to 07:18:54 UTC, after the spawn.

Attribution. The 7.064 s `code_index_index` on a zero-diff fork is this plan's
defect against Josh's 2026-10-05 contract that a new worktree should "copy, clone,
fork, or mirror" its fork commit's index: overlay reconcile runs full discovery,
compares against the parent's moving HEAD and status, and runs community
partitioning before its unchanged check (P3 and P4 remove each). The untimed 24 s
falls before `phase_timings_ms` exists: `spawn_agent` prepares isolation
before it creates the timing map (`src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py`,
observed by the enhancer with `gcode evidence`, excerpt hashes `77ee4ba3…` and
`e4911299…`). Between the sidecar write and the MCP config write, isolation repair
copies hooks, rewrites the marker, awaits the Python environment preseed, then
writes MCP config (`src/gobby/agents/isolation_repair.py`, excerpt `aea53db5…`);
the preseed runs `uv sync --offline --frozen --link-mode copy`
(`src/gobby/agents/python_env_seed.py`, excerpt `e612a216…`). The 20 s gap is
therefore most plausibly Python environment seeding, and worktree creation
explains most of the preceding 3 s. That attribution is inferred from source and
log order, not measured; this plan measures it. Attributing isolation cost is in
scope. Optimizing Python environment seeding is outside this index-pin plan.

The current checkout has migration 458 as its head. Observed 2026-10-05:
`git log --all --diff-filter=A --name-only --format= -- 'crates/gcore/assets/schema/migrations/459_*' 'crates/gcore/assets/schema/migrations/46*'`
returned no files, so no ref adds 459 or later. The Orchestrator assigned 459 to
#23439 (bigint session usage counters) and 460 to this plan:
`460_code_overlay_pins.sql`. A rerun later on 2026-10-05, after #23439 landed
`459_session_usage_bigint.sql`, found no 460 or later. If another migration lands first, the executor
repeats that all-ref sweep and takes the next free number.
The migration source requires its catalog, grant, schema-contract, CLI-contract,
and Python expected-identity carriers in the same deliverable.

The code index source is the PostgreSQL hub. The current overlay catalog resolves
inherited rows from the parent's current selectors, and overlay reconcile consults
the parent's HEAD and status. `refresh_project_communities` computes a partition
before its unchanged-result check. The 910-line clone Git module needs an extraction
before growth. Facts and entry points were checked with `gcode outline`, `gcode
grep`, and the supplied 2026-10-04 research note. All checks below are planned
unless explicitly marked observed.

## P1: Measure spawn isolation cost
`kind: framing`

### T1 Isolation subphase timing and wall residual
`kind: deliverable`
`category: code`
`implementation_domain: backend`

Targets:
- `src/gobby/agents/spawn_timing.py::*` — scope-reason: add isolation phase keys, spawn wall time, and the unattributed residual
- `src/gobby/agents/isolation_models.py::*` — scope-reason: SpawnConfig carries the spawn timing map into prepare_environment
- `src/gobby/agents/isolation_repair.py::*` — scope-reason: time each repair step into the supplied timing map
- `src/gobby/agents/isolation_worktree.py::*` — scope-reason: pass the SpawnConfig timing map into repair
- `src/gobby/agents/isolation_clone.py::CloneIsolationHandler.prepare_environment`
- `src/gobby/mcp_proxy/tools/spawn_agent/_worktree_reuse.py::*` — scope-reason: reused-worktree repair records the same subphases
- `src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py::*` — scope-reason: create the timing map at entry and move isolation preparation out
- `src/gobby/mcp_proxy/tools/spawn_agent/_isolation_prepare.py`
- `src/gobby/agents/spawn_models.py::*` — scope-reason: SpawnRequest carries the spawn start for the wall residual
- `src/gobby/mcp_proxy/tools/spawn_agent/_request.py::*` — scope-reason: pass the spawn start into SpawnRequest
- `src/gobby/agents/spawn_executor.py::*` — scope-reason: the timing log computes wall time and residual
- `tests/agents/test_spawn_isolation_timing.py`

**Research context:** Observed with `gcode outline` and source reads on
2026-10-05: `spawn_agent_impl`
(`src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py`) calls
`handler.prepare_environment(spawn_config)` before it creates
`phase_timings_ms`, so worktree creation and isolation repair never reach the
spawn timing log. `repair_isolation_environment`
(`src/gobby/agents/isolation_repair.py`) runs `_copy_cli_hooks`,
`ensure_project_json_for_isolation`, `preseed_isolated_python_environment`
(which runs `uv sync --offline --frozen --link-mode copy`),
`_patch_mcp_config_for_isolation`, and `apply_isolation_git_hygiene`, in that
order. Its callers are `WorktreeIsolationHandler` and `CloneIsolationHandler`
(two calls each), `spawn_agent/_worktree_reuse.py`, and one reuse site inside
`spawn_agent_impl`; `src/gobby/agents/isolation.py` only re-exports it.
`complete_spawn_phase_timings` (`src/gobby/agents/spawn_timing.py`) emits only
`SPAWN_PHASES` keys, and `spawn_executor.py` logs them in its `finally` block,
timing only its own span. R2 records the idle baseline this deliverable
explains: 33.9 s wall, 9.7 s timed, about 24 s untimed, with a 20 s gap between
the isolation sidecar write and the MCP config write.

Approach: create the timing map and record the spawn start at
`spawn_agent_impl` entry. Carry the map on `SpawnConfig` as a new
`phase_timings_ms` field with an empty default, so `prepare_environment` and
every `repair_isolation_environment` call record into it through the existing
`phase_timings_ms: MutableMapping[str, float] | None = None` idiom that
`ensure_isolation_code_index` uses. New keys: `isolation_prepare` for the whole
`prepare_environment` call, and its nested `isolation_hook_copy`,
`isolation_project_marker`, `python_env_seed`, `isolation_mcp_config`, and
`isolation_git_hygiene`. `SpawnRequest` gains the spawn start, and
`complete_spawn_phase_timings` adds `spawn_wall` and `unattributed`:
`unattributed` is wall time minus top-level phases, with the nested isolation
subphases left out of the sum so no time counts twice. `_implementation.py` is
912 lines, so move the `prepare_environment` call and the prepare-failure
response out of `spawn_agent_impl` into the new
`src/gobby/mcp_proxy/tools/spawn_agent/_isolation_prepare.py`, which also times
`isolation_prepare`. Handler selection stays in place: `spawn_agent_impl` and
`_worktree_reuse.py` keep calling `get_isolation_handler` and pass the handler to
the moved helper. Literal sweep, observed 2026-10-05: `gcode grep -F
get_isolation_handler tests/mcp_proxy` and `gcode grep -w prepare_environment
tests/mcp_proxy` hit `tests/mcp_proxy/tools/test_spawn_agent_impl_provider.py`,
`tests/mcp_proxy/tools/spawn_agent/test_event_loop.py`,
`tests/mcp_proxy/tools/spawn_agent/test_agy_gate.py`, and A1's spawn
error-handling test, which patch those module attributes and mock
`prepare_environment` on the returned handler; all keep working after the move,
and the error-handling test changes later under A1 only for its base-branch
assertions. This deliverable changes no isolation behavior and has no
dependencies; it lands first so every later deliverable is measured against an
attributed baseline. Rejected: deriving phases from log timestamps (neither
structured nor testable) and a context variable for the timing map (hidden
coupling where an explicit field already fits). Optimizing Python environment
seeding is outside this plan.

Planned checks: focused isolated pytest for
`tests/agents/test_spawn_isolation_timing.py` and `tests/agents/test_isolation.py`,
then one load-matched idle zero-diff Claude worktree spawn after landing whose
subphase breakdown and residual are recorded on the T1 leaf.

**Granularity:** Eleven production files change, but they carry one timing map
through one spawn path; splitting them would land phase keys with no writer or
writers with no log. The `_implementation.py` move is required by the
1,000-line ceiling.

Consumers unchanged:
- `tests/mcp_proxy/tools/test_spawn_agent_impl_provider.py` — no-edit-reason: It patches _implementation.get_isolation_handler and mocks prepare_environment on the returned handler, and handler selection stays in _implementation.py.
- `tests/mcp_proxy/tools/spawn_agent/test_event_loop.py` — no-edit-reason: It monkeypatches _implementation.get_isolation_handler, which still selects the handler passed to the moved helper.
- `tests/mcp_proxy/tools/spawn_agent/test_agy_gate.py` — no-edit-reason: It patches get_isolation_handler on _implementation, which keeps that lookup.

**Acceptance:**

- T1.1 - Spawn timing reports `isolation_prepare` and the five repair subphases for worktree, clone, and reused-worktree spawns. test: `tests/agents/test_spawn_isolation_timing.py::test_spawn_timing_attributes_isolation_repair`.
- T1.2 - The timing log reports `spawn_wall` and an `unattributed` residual equal to wall time minus top-level phases, excluding nested subphases. test: `tests/agents/test_spawn_isolation_timing.py::test_idle_baseline_breakdown_is_complete`.
- T1.3 - Prepare-failure responses are unchanged after the move, and `_implementation.py` stays below 1,000 lines. test: `tests/agents/test_spawn_isolation_timing.py::test_prepare_failure_response_unchanged_after_move`.

## P2: Select and record the fork commit
`kind: framing`

### A1 Worktree ref selection and refresh (depends: T1)
`kind: deliverable`
`category: code`
`implementation_domain: backend`

Targets:
- `src/gobby/worktrees/git/_lifecycle.py::create_worktree`
- `src/gobby/worktrees/git/manager.py::WorktreeGitManager.create_worktree`
- `src/gobby/worktrees/base_branch.py::*` — scope-reason: validate explicit ref forms and selected commit
- `src/gobby/worktrees/creation.py::*` — scope-reason: creation, cleanup, and result provenance share the selected commit
- `src/gobby/agents/isolation_models.py::*` — scope-reason: SpawnConfig keeps an omitted base as None through to the handler
- `src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py::*` — scope-reason: move requested-base resolution out and stop substituting the current branch or main
- `src/gobby/mcp_proxy/tools/spawn_agent/_isolation_prepare.py`
- `src/gobby/agents/isolation_worktree.py::*` — scope-reason: preparation and cleanup share fork provenance
- `src/gobby/agents/worktree_reuse.py::*` — scope-reason: refresh, conflict, and pin provenance share one continuation path
- `src/gobby/mcp_proxy/tools/worktrees/_create.py::*` — scope-reason: omitted base selects the caller's HEAD and remote-style refs are accepted
- `src/gobby/cli/worktrees.py::*` — scope-reason: CLI creation defaults to the caller's HEAD
- `src/gobby/servers/routes/source_control_worktrees.py::*` — scope-reason: client worktree creation forks the exact selected commit
- `src/gobby/hooks/event_handlers/_misc.py::*` — scope-reason: the worktree hook drops its clean-branch origin fallback
- `src/gobby/build/workspace_services.py::*` — scope-reason: integration worktree creation passes its selected commit without use_local
- `src/gobby/install/shared/skills/gobby/references/source-control/worktrees.md`
- `tests/worktrees/test_fork_commit.py`
- `tests/agents/test_worktree_fork_commit.py`
- `tests/mcp_proxy/tools/test_worktrees_create.py::*` — scope-reason: omitted versus explicit base cases
- `tests/hooks/test_misc_handlers.py::*` — scope-reason: hook exact-HEAD case
- `tests/servers/test_source_control_worktrees.py`
- `tests/worktrees/test_worktree_git.py::*` — scope-reason: creation requires a base ref and selected commit, and explicit remote refs are accepted
- `tests/worktrees/test_worktree_service.py::*` — scope-reason: wrapper calls drop use_local and keep the commit-SHA base refusal
- `tests/worktrees/test_creation.py::*` — scope-reason: direct creation passes the selected commit instead of use_local
- `tests/servers/routes/test_source_control_routes.py::*` — scope-reason: client creation asserts the selected commit instead of main with use_local False
- `tests/agents/test_isolation.py::*` — scope-reason: worktree handler tests resolve an omitted base once and keep an explicit main
- `tests/agents/test_isolation_base_capture.py::*` — scope-reason: base capture records the selected commit
- `tests/agents/test_isolation_project_json.py::*` — scope-reason: handler creation mocks drop use_local
- `tests/mcp_proxy/tools/spawn_agent/test_project_scope.py::*` — scope-reason: an omitted base reaches the handler as None and resolves against the target project
- `tests/mcp_proxy/tools/spawn_agent/test_error_handling.py::*` — scope-reason: spawn no longer resolves the current branch before the handler
- `tests/mcp_proxy/tools/spawn_agent/test_failure_cleanup.py::*` — scope-reason: handler creation mocks drop use_local

**Research context:** Agent spawns already select the caller's current branch
and preserve unpushed commits: `src/gobby/agents/isolation_worktree.py` replaces
a default `main` with the current branch and chooses the local ref when it has
unpushed commits (enhancer `gcode evidence`, excerpt `1067bfea…`). The remaining
clean-branch path in `src/gobby/worktrees/git/_lifecycle.py::create_worktree`
fetches and selects `origin/<branch>` (excerpt `bf8e75ed…`). Direct MCP, CLI,
and client-route creation still default to `main`, and the MCP surface in
`src/gobby/mcp_proxy/tools/worktrees/_create.py` rejects remote-style refs
(excerpt `beecb577…`). The worktree hook in
`src/gobby/hooks/event_handlers/_misc.py` uses the current branch but falls back
to origin when clean (excerpt `6b9c7c6c…`). `sync_reused_worktree_to_base`
already returns a recoverable conflict.

Omission is lost before any handler runs. `spawn_agent_impl` turns an omitted
base into the target checkout's current branch, or `main` when that lookup fails
or HEAD is detached (`src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py`,
Adversary `gcode evidence` excerpt `cf9d9ec9…`), `SpawnConfig.base_branch` is a
required `str` (`src/gobby/agents/isolation_models.py`, `256e205b…`), and both
handlers then replace `main` with the current branch, so an explicit local `main`
is indistinguishable from an omitted base. The `WorktreeGitManager.create_worktree`
wrapper and `_lifecycle.create_worktree` default to `main` with `use_local=False`
(`src/gobby/worktrees/git/manager.py`, `99fe63fa…`). The workspace record refuses
a commit SHA as its base branch (`base_branch_is_commit_sha`,
`tests/worktrees/test_worktree_service.py::test_spawn_worktree_create_refuses_unreferenced_sha_base`).

The default-path delta is to resolve the caller checkout's `HEAD^{commit}` once
and fork that exact commit, eliminating the clean-branch fetch and its race.
Omission stays `None` from the request through `SpawnConfig`
(`base_branch: str | None`; an agent definition's `inherit` still means omitted),
and `spawn_agent_impl` no longer resolves a branch. Each handler resolves an
omitted base exactly once per spawn, with the target project's git manager, to the
caller checkout's `HEAD^{commit}`, and records the attached branch name as the
workspace base branch. An explicit ref, including `main`, is never replaced by the
current branch. An omitted base on a detached HEAD returns a recoverable
`detached_head_requires_base` error before any side effect; it never falls back to
`main`, and it never records a SHA as the base branch. The selected commit
already fixes the fork point, so the refusal rests on the workspace lifecycle
rather than on the public SHA-base check. Merge start defaults its target to the
stored base branch, and deletion's merge check reads it, including the stale
cleanup before reuse. A detached HEAD names no branch for either. Falling back to
`main` would land the detached commits in `main`, and recording the SHA would
leave no branch to land into or to prove a merge against. Bare explicit refs are
local only; `origin/<name>` and `refs/remotes/origin/<name>` are explicit remote
refs that fetch only that ref and record `<name>` as the base branch.
`_lifecycle.create_worktree` and its `WorktreeGitManager` wrapper require the
recorded base branch and the selected commit and drop their `main` and
`use_local` defaults; the ref form alone decides local versus remote, so every
caller, including the integration path in `src/gobby/build/workspace_services.py`
and the `create_worktree` MCP tool, stops passing `use_local`. Align the MCP, CLI,
client-route, and hook surfaces with that rule. Reuse the existing
`get_local_commit` in `src/gobby/worktrees/git/_branch.py` unchanged; add no
second branch-resolution abstraction. Reject an existing branch whose tip would
change the selection. A reused agent worktree rebases onto the selected commit.
Integration workspace refresh keeps its current merged-commit behavior and is
re-pinned under B2.

`_implementation.py` is 913 lines, so move the requested-base resolution (the
explicit argument, the agent definition's default, and `inherit` as omitted) out
of `spawn_agent_impl` into T1's
`src/gobby/mcp_proxy/tools/spawn_agent/_isolation_prepare.py`, returning `None`
for an omitted base; the current-branch and `main` substitution is deleted, not
moved.

Consumer sweep, observed 2026-10-05: `gcode grep -w create_worktree
src/gobby/worktrees/ src/gobby/agents/ tests/worktrees/ -m 55`, `gcode grep -F
create_worktree src/ tests/`, and `gcode grep -E 'use_local|shallow' tests/`.
The tests in Targets assert the old `main`, `use_local`, current-branch
substitution, or remote-rejection contract
(`test_create_rejects_remote_base_before_side_effects` becomes an acceptance
case). The `create_worktree` hits in `tests/cli/test_cli_worktrees_coverage.py`,
`tests/cli/test_worktrees_cli.py`, `tests/cli/test_worktrees_coverage.py`,
`tests/e2e/test_worktrees_e2e.py`, and
`tests/integration/test_worktree_lifecycle.py` only build stored `Worktree`
fixtures with `base_branch="main"`, which remains a valid recorded branch.
`src/gobby/agents/resume_metadata.py` keeps its `base_branch: str` parameter,
because handlers always report a branch name. Planned check: isolated focused
pytest for local HEAD, unpushed commits, detached HEAD, explicit local `main`
while on another branch, explicit local and remote refs, mismatched existing
branch, reuse conflict, and each public creation surface, plus every migrated
test file above.

**Granularity:** These entry points share one selected-commit contract;
splitting them would leave a creation surface with different fork semantics.
The acceptance items are one rule checked at each surface that applies it. The
test count is migration of existing expectations the contract changes, not
independent work.

**Acceptance:**

- A1.1 - Default worktrees fork the caller's local HEAD without fetch, including unpushed commits. test: `tests/worktrees/test_fork_commit.py::test_default_uses_caller_head_without_fetch`.
- A1.2 - Explicit local and remote refs resolve to one commit, with remote fetch only for remote selection. test: `tests/worktrees/test_fork_commit.py::test_explicit_refs_select_commit`.
- A1.3 - Existing branches cannot silently change the selected commit. test: `tests/worktrees/test_fork_commit.py::test_existing_branch_mismatch_is_rejected`.
- A1.4 - Reused worktrees rebase onto the selected commit and report recoverable conflicts. test: `tests/agents/test_worktree_fork_commit.py::test_refresh_preserves_workspace_ancestry`.
- A1.5 - Public creation surfaces distinguish an omitted base, which uses the caller's HEAD, from an explicit local `main`. test: `tests/mcp_proxy/tools/test_worktrees_create.py::test_omitted_base_uses_project_head_and_explicit_main_stays_main`.
- A1.6 - The worktree hook forks the exact local HEAD without an origin fallback. test: `tests/hooks/test_misc_handlers.py::TestWorktreeHandlers::test_worktree_create_uses_exact_local_head`.
- A1.7 - Client worktree creation forks the exact selected commit. test: `tests/servers/test_source_control_worktrees.py::test_create_client_worktree_uses_exact_selected_commit`.
- A1.8 - An omitted agent base reaches the handler as `None` and resolves the target checkout's HEAD once; a detached HEAD fails recoverably without side effects, and an explicit local `main` on another branch forks local `main`. test: `tests/agents/test_worktree_fork_commit.py::test_omitted_base_survives_to_one_head_resolution`.

### A2 Local clone selection and Git module extraction (depends: A1)
`kind: deliverable`
`category: code`
`implementation_domain: backend`

Targets:
- `src/gobby/clones/git.py::*` — scope-reason: extract merge implementation and route local/remote clone creation
- `src/gobby/clones/merge.py`
- `src/gobby/mcp_proxy/tools/_clones_creation.py::*` — scope-reason: clone tool arguments and result carry selected commit
- `src/gobby/agents/isolation_clone.py::CloneIsolationHandler.prepare_environment`
- `src/gobby/install/shared/skills/gobby/references/source-control/clones.md`
- `tests/clones/test_fork_commit.py`
- `tests/agents/test_clone_fork_commit.py`
- `tests/agents/test_isolation.py::*` — scope-reason: clone handler tests expect a local clone of the selected commit instead of a shallow remote clone
- `tests/mcp_proxy/tools/test_mcp_proxy_tools_clones.py::*` — scope-reason: the clone tool's default path clones locally instead of calling shallow_clone

**Research context:** `CloneGitManager.create_clone` currently follows the
remote shallow-clone path and the MCP clone tool defaults `base_branch` to main.
Use the caller's local repository as the clone source for default and local-ref
forks, preserve the exact selected commit, and use a fetched remote tip for an
explicit remote ref. An external URL has no matching local indexed checkout and
keeps the remote clone path. The clone handler follows A1's omission rule: an
omitted base resolves the caller's `HEAD^{commit}` once, a detached HEAD fails
recoverably, and an explicit `main` is never replaced by the current branch. The
handler and the clone tool stop choosing between local and shallow remote clones
by unpushed-commit detection, and the clone tool drops its `use_local` argument;
`CloneGitManager.create_clone` keeps its `use_local` and `shallow` parameters for
the external-URL path, so its manager-level tests in `tests/clones/test_git.py`
and `tests/clones/test_git_extended.py` are unchanged.
`tests/agents/test_isolation.py` asserts `use_local=False` and `shallow=True`
for a clean clone (Adversary excerpt `5014b80a…`), and
`tests/mcp_proxy/tools/test_mcp_proxy_tools_clones.py` asserts `shallow_clone`
for the default path; both migrate. Split `CloneGitManager.merge_branch` and its
private helpers from the 910-line `git.py` into the new `merge.py`, preserving the
manager's public method and tests. Planned check: focused clone and agent pytest
for all sources, the migrated tests, and the extraction's merge behavior.

**Granularity:** The extraction keeps the touched production module below the
1,000-line ceiling and belongs in this clone lifecycle change.

**Acceptance:**

- A2.1 - Default clones include caller-local unpushed commits and record the selected commit. test: `tests/clones/test_fork_commit.py::test_local_clone_uses_caller_head`.
- A2.2 - Explicit local/remote clone refs select their resolved tips; external URL clones report a cold-index source. test: `tests/clones/test_fork_commit.py::test_clone_ref_sources`.
- A2.3 - The extracted merge path preserves merge behavior and keeps the Git module below 1,000 lines. test: `tests/clones/test_fork_commit.py::test_extracted_merge_path`.
- A2.4 - Agent clones resolve an omitted base once, keep an explicit `main`, and fail recoverably on a detached HEAD. test: `tests/agents/test_clone_fork_commit.py::test_clone_omission_and_detached_head`.

## P3: Persist and read a pinned index
`kind: framing`

### B1 Pin schema, marker, and clean Git blob selectors (depends: A1, A2)
`kind: deliverable`
`category: code`
`implementation_domain: backend`

Targets:
- `crates/gcore/assets/schema/migrations/460_code_overlay_pins.sql`
- `crates/gcore/assets/schema/catalog.manifest.json::*` — scope-reason: generated schema catalog entries change together
- `crates/gcore/src/grant/bundle.rs::*` — scope-reason: grant the exact pin-table read and write surface
- `crates/gcore/tests/schema_contract.rs::*` — scope-reason: verify migration, constraints, and grants
- `crates/gdaemon/tests/cli_contract.rs::*` — scope-reason: verify installed schema identity and CLI contract
- `src/gobby/storage/schema_expected_identity.json::*` — scope-reason: generated schema identity fields change together
- `crates/gcore/src/project.rs::*` — scope-reason: parse and validate base commit and ancestor checkout identity
- `src/gobby/utils/project_context.py::*` — scope-reason: persist and read the isolation marker's pin provenance
- `crates/gcode/src/index/api/file_state.rs::*` — scope-reason: one compare-and-set writer sets or clears a selector's Git blob ID
- `crates/gcode/src/index/indexer/file.rs::*` — scope-reason: record blob IDs with clean selectors
- `crates/gcode/src/index/indexer/pipeline.rs::*` — scope-reason: record blob state for fresh, adopted, and skipped primary selectors
- `crates/gcode/src/index/indexer/overlay.rs::*` — scope-reason: record blob state for overlay-owned selectors so nested pins can inherit them
- `crates/gcode/src/index/indexer/tests/facts.rs::*` — scope-reason: verify selector blob writes across write outcomes
- `tests/utils/test_isolation_pin_marker.py`

**Research context:** `code_indexed_file_states` selects the current content
version and `code_indexed_files` owns immutable content facts. Add a nullable Git
blob ID to a clean tracked selector and an overlay-owned base table keyed by
machine, overlay project, and path with source project and content hash. Its FK
must keep the source content version valid. Each pin row also carries B4's
projection state: graph and vector projected flags, default false, with
per-target attempt timestamps. Store `base_commit` and the ancestor
checkout path/identity in the isolation marker, validating partial markers.
Dirty or untracked content never receives a qualifying blob ID.

Selector blob ownership. The existing selector writers are
`api::file_state::upsert_file_state`, which inserts or updates only the content
hash, and `adopt_file_state`, which also writes selectors (Adversary excerpts
`bb083c90…` and `5fa77874…`); `PostgresCodeFactSink::upsert_file`
(`crates/gcode/src/index/indexer/sink.rs`, `dde7a41a…`) calls the first for fresh
writes, `pipeline.rs` calls `adopt_file_state` for adopted primary selectors, and
`overlay.rs` calls it for overlay selectors. The test-only Python
`CodeIndexFileStorageMixin.upsert_file` and raw test fixtures also update
selectors. Migration 460 adds a `BEFORE UPDATE OF content_hash` trigger on
`code_indexed_file_states` that clears a carried-over blob ID whenever the content
hash changes, following the existing `trg_chat_attachments_*` trigger pattern;
that single guard covers every writer, so no writer can leave a blob ID naming
other content. A new `api::file_state::set_selector_git_blob` sets or clears the
blob ID for one selector only while it still names the given content hash.
`pipeline.rs` calls it for fresh, adopted, and skipped primary selectors and
`overlay.rs` for overlay-owned selectors, passing the blob ID only for clean
tracked files and `None` otherwise. A skipped selector back-fills a missing blob
ID once its unchanged file becomes clean and tracked, and keeps an existing one,
because a Git blob ID is a function of content. `upsert_file_state`,
`adopt_file_state`, their 30-odd test call sites, `IndexedFile`, and the
`CodeFactSink` trait keep their signatures, so `sink.rs` is unchanged.

Update every schema carrier with migration 460 (459 belongs to #23439; if another
migration lands first, repeat the R2 all-ref sweep and take the next free number).
Planned checks: schema contracts, marker tests, and `cargo test -p gobby-code` for
selector writes, the trigger, and each write outcome.

**Granularity:** Schema, grants, generated catalog, expected identity, marker,
and selector storage form one atomic data contract. The carrier count is required
by the repository's schema contract, not independent implementation work.

Consumers unchanged:
- `crates/gcode/src/index/indexer/sink.rs` — no-edit-reason: PostgresCodeFactSink::upsert_file keeps calling upsert_file_state, and the migration trigger clears a stale blob ID on its content-hash updates.

**Acceptance:**

- B1.1 - Migration and all carriers expose an overlay-owned pin table with source-version FK and scoped grants. test: `crates/gcore/tests/schema_contract.rs::code_overlay_pin_schema_contract`.
- B1.2 - Clean tracked file selectors store Git blob IDs; dirty and untracked selectors do not qualify. test: `crates/gcode/src/index/indexer/tests/facts.rs::clean_selector_records_git_blob`.
- B1.3 - Isolation markers carry selected commit and ancestor checkout identity with validated completeness. test: `tests/utils/test_isolation_pin_marker.py::test_marker_requires_complete_pin_provenance`.
- B1.4 - Fresh, adopted, skipped, and overlay-owned selectors set, back-fill, or clear the blob ID, and any content-hash change clears a carried-over blob ID. test: `crates/gcode/src/index/indexer/tests/facts.rs::selector_blob_follows_every_write_outcome`.

### B2 Create an effective pin and persist indexing gaps (depends: B1)
`kind: deliverable`
`category: code`
`implementation_domain: backend`

Targets:
- `crates/gcode/src/commands/pin.rs`
- `crates/gcode/src/commands/mod.rs::*` — scope-reason: expose the pin command
- `crates/gcode/src/cli.rs::*` — scope-reason: parse pin command arguments
- `crates/gcode/src/dispatch.rs::*` — scope-reason: route the pin command
- `crates/gcode/src/visibility/catalog.rs::*` — scope-reason: pin derives the effective catalog view
- `src/gobby/agents/code_index.py::*` — scope-reason: preflight and pin invocation share one gcode runtime
- `src/gobby/worktrees/creation.py::*` — scope-reason: direct creation and cleanup share pin lifecycle
- `src/gobby/mcp_proxy/tools/_clones_creation.py::*` — scope-reason: pin after direct clone creation
- `src/gobby/build/workspace_services.py::*` — scope-reason: integration workspace creation and refresh re-pin their merged commit
- `crates/gcode/src/commands/pin/tests.rs`
- `tests/agents/test_code_index_pin.py`

**Research context:** A pin is copied from the effective selector view, not just
the ancestor's owned rows. For every candidate, compare its recorded Git blob
ID to `git ls-tree` at the selected commit and exclude ancestor dirty,
untracked, ignored, and mismatched paths. Copy qualifying selectors into
overlay-owned base rows and persist every tracked gap for child indexing. A
matching live indexed checkout is required; an explicit ref without one starts
with no inherited selectors. Pin replacement must be atomic.

Each path has one pin owner. Agent worktree and clone creation and reuse write
complete marker provenance under A1 and A2;
`src/gobby/agents/code_index.py::ensure_isolation_code_index` is the single
agent-path owner: it reads that marker and runs pin immediately before
`gcode index`, including after a reused-worktree rebase. Direct worktree
creation pins in `src/gobby/worktrees/creation.py`, direct clone creation pins
in `src/gobby/mcp_proxy/tools/_clones_creation.py`, and integration workspace
creation and refresh re-pin in `src/gobby/build/workspace_services.py`, keeping
the integration workspace's merged commits. Individual isolation handlers never
invoke pin. A failed pin never falls through to the moving parent's rows. Two
failure classes differ. A semantic selection failure, such as a missing or
unreadable ancestor checkout, an unresolvable selected commit, or a failed
`git ls-tree`, durably records a cold pin with no inherited selectors, and the
child indexes locally. A storage failure, such as a database, grant, or timeout
error on the pin transaction, cannot be trusted to write that cold pin either,
and the prior pin rows survive the rolled-back replacement; after a
reused-worktree rebase they name the old commit. Such a failure, including a
failed cold-pin write after a semantic failure, returns a recoverable error
before any index, read, or spawn success: the agent spawn, direct creation, or
integration refresh fails with the pin error and never proceeds on the prior pin.
Planned checks: Rust PostgreSQL tests for clean, stale, dirty, and nested-overlay
selectors, plus focused Python call-path, pin-failure, and storage-failure tests.

**Granularity:** Pin selection and atomic replacement are one lifecycle
transaction; the Python callers exercise that same command at creation. Nine
production files change because each creation path gets exactly one pin call;
splitting callers from the command would ship a pin nothing invokes.

**Acceptance:**

- B2.1 - The pin copies clean effective selectors, including inherited rows from an author overlay, at the selected commit. test: `crates/gcode/src/commands/pin/tests.rs::pin_inherits_effective_selectors`.
- B2.2 - Dirty, untracked, ignored, and mismatched files are excluded and tracked gaps persist for indexing. test: `crates/gcode/src/commands/pin/tests.rs::pin_records_only_safe_selectors_and_gaps`.
- B2.3 - No matching live indexed checkout produces a cold pin; re-pin atomically replaces prior rows. test: `crates/gcode/src/commands/pin/tests.rs::pin_cold_and_replacement`.
- B2.4 - Agent spawns pin from the marker in `ensure_isolation_code_index` immediately before `gcode index`, and isolation handlers never invoke pin. test: `tests/agents/test_code_index_pin.py::test_agent_preflight_pins_marker_before_index`.
- B2.5 - A semantic pin-selection failure records a cold pin and indexes locally, never exposing moving-parent rows. test: `tests/agents/test_code_index_pin.py::test_pin_failure_forces_cold_index_without_parent_fallthrough`.
- B2.6 - Direct worktree creation, direct clone creation, and integration refresh each pin the selected commit before returning, and integration refresh keeps its merged commits. test: `tests/agents/test_code_index_pin.py::test_direct_paths_pin_before_return`.
- B2.7 - A pin storage failure, including a failed cold-pin write, returns a recoverable error before index, read, or spawn success and never proceeds on the prior pin. test: `tests/agents/test_code_index_pin.py::test_pin_storage_failure_blocks_success`.

### B3 Scoped PostgreSQL and BM25 reads (depends: B2)
`kind: deliverable`
`category: code`
`implementation_domain: backend`

Targets:
- `crates/gcode/src/visibility.rs::*` — scope-reason: shared source-project, path, and hash visibility predicates
- `crates/gcode/src/visibility/catalog.rs::*` — scope-reason: effective tree and kind catalog read pinned selectors
- `crates/gcode/src/db/queries.rs::*` — scope-reason: scope BM25 and symbol SQL before limits
- `crates/gcode/src/commands/search.rs::*` — scope-reason: apply visibility before ranked search limits
- `crates/gcode/src/commands/search/scoped_fetch.rs`
- `crates/gcode/src/commands/symbols.rs::*` — scope-reason: symbol and tree results use effective selectors
- `crates/gcode/src/codewiki_facts/scope.rs::*` — scope-reason: codewiki facts use pinned visibility
- `crates/gcode/src/visibility/tests.rs::*` — scope-reason: add pinned read cases to existing fixtures
- `crates/gcode/src/cli/tests/search.rs::*` — scope-reason: verify scoped BM25 and symbol result limits

**Research context:** Facts are keyed by source project, file path, and content
hash. Overlay-owned file states shadow pinned base rows; tombstones hide paths.
Inherited rows come only from pins: an overlay with no pin rows, including a cold
pin, inherits nothing and never reads the parent's current selectors.
Resolve each inherited fact by its exact pinned source version for BM25, symbol,
tree, import, and codewiki reads. Apply scope in SQL before `LIMIT`, so stale
parent rows cannot consume a result window. Split `crates/gcode/src/commands/search.rs`
by moving scoped fetch into `crates/gcode/src/commands/search/scoped_fetch.rs`
while changing its ranked retrieval path. Planned
check: isolated PostgreSQL Rust tests after parent re-index, plus search limit
and overlay-of-overlay cases.

**Granularity:** The SQL and shared visibility predicates are one read contract
used by all text and symbol lanes; projection reads are separately owned by B4.

**Acceptance:**

- B3.1 - Parent changes cannot alter pinned BM25, symbol, tree, import, or codewiki facts. test: `crates/gcode/src/visibility/tests.rs::pinned_reads_survive_parent_reindex`.
- B3.2 - Visibility is applied before search limits. test: `crates/gcode/src/cli/tests/search.rs::pinned_visibility_precedes_limit`.
- B3.3 - Overlay states and tombstones shadow pinned rows. test: `crates/gcode/src/visibility/tests.rs::overlay_shadowing_of_pins`.

### B4 Vector and graph pin scope with background recovery (depends: B3)
`kind: deliverable`
`category: code`
`implementation_domain: backend`

Targets:
- `crates/gcode/src/commands/vector.rs::*` — scope-reason: constrain semantic results to pinned symbol versions and route `vector sync-file` for an unowned overlay path to its pin
- `crates/gcode/src/commands/graph/lifecycle.rs::*` — scope-reason: route `graph sync-file` for an unowned overlay path to its pin before missing-file handling
- `crates/gcode/src/commands/graph/reads.rs::*` — scope-reason: graph reads use the pinned effective graph
- `crates/gcode/src/graph/code_graph/read/relationships.rs::*` — scope-reason: relationship queries filter source versions
- `crates/gcode/src/projection/sync.rs::*` — scope-reason: route an unowned overlay path's graph and vector sync through its pin
- `crates/gcode/src/projection/sync/pinned.rs`
- `crates/gcode/src/db/queries.rs::*` — scope-reason: read graph facts at an exact source version
- `crates/gcode/src/vector/code_symbols/repository.rs::*` — scope-reason: fetch symbols at an exact source version
- `crates/gcode/src/commands/search/scoped_fetch.rs`
- `crates/gcode/src/projection/sync/tests.rs::*` — scope-reason: recovery tests use existing projection fixtures
- `crates/gcode/src/cli/tests/projection.rs::*` — scope-reason: verify degraded and recovered search
- `crates/gcode/tests/projection_stale.rs::*` — scope-reason: binary-level proof that both sync-file commands reach the pinned helper
- `src/gobby/code_index/_storage/files.py::*` — scope-reason: enumerate pending pinned projection work per overlay project
- `src/gobby/code_index/sync_worker.py::*` — scope-reason: the existing worker pass drives pinned recovery
- `tests/code_index/test_sync_worker_pins.py`

**Research context:** Graph and vector projections currently hold only a source
project's current per-path version. PostgreSQL facts become available before
projection completion. R1 makes graph and vector availability part of the pin
contract, so filtering alone is incomplete: it prevents stale answers but loses
graph and vector results once a parent projection moves.

Nothing schedules that recovery today. `ProjectionSyncRequest` carries only a
project ID and path lists, `pending_after_code_fact_write` only builds a pending
status, and `sync_after_index` runs synchronously after an index pass
(`crates/gcode/src/projection/sync.rs`, Adversary excerpts `1e757710…` and
`1a311306…`). The daemon's background worker
(`src/gobby/code_index/sync_worker.py::_sync_pass`) scans each indexed project's
own selectors through `CodeIndexFileStorageMixin.get_pending_sync_files`
(`c3f742db…`), so a pin alone never becomes work. Rust `sync_graph_file` and
`VectorProjectionState::sync_file` read the current selector for
`ctx.project_id` and path (`372db2f1…`), never a pinned version. The worker's
`_sync_file` marks and requeues the facts row by `IndexedFile.id`, which for a
pinned path is the source project's row, so pin work cannot reuse it.

Recovery reuses that worker and adds no queue. The pin row is the durable work
identity: machine, overlay project, path, source project, and content hash, with
B1's per-target projected flags and attempt timestamps. A pin row is pending for
a target when its flag is false, the source project's selector no longer names
the pinned version, no child-owned selector or tombstone shadows the path, and
the attempt is outside the existing failure cool-off. A new storage method in
`src/gobby/code_index/_storage/files.py` lists that work per overlay project, and
`_sync_pass` processes it after the project's own pending files through a
separate pinned branch that calls the existing `GcodeGateway.graph_sync_file` and
`vector_sync_file` with the child root and path, so `gcode_gateway.py` (950
lines) is unchanged. Those gateway methods run `gcode graph sync-file` and
`gcode vector sync-file`, whose entry points never reach the batch helpers in
`projection/sync.rs`: `sync_file_graph` in
`crates/gcode/src/commands/graph/lifecycle.rs` and `sync_file` in
`crates/gcode/src/commands/vector.rs` return a missing-indexed-file result as soon
as the child has no owned row for the path (Adversary excerpts `080e92dd…` and
`ef3ea5c5…`). Both entry points keep their existing per-file lock. When marking
the attempt finds no child-owned row, each calls the new
`crates/gcode/src/projection/sync/pinned.rs` before that missing-file handling. The
helper returns nothing when the path has no live, unshadowed pin row, so an
unpinned missing path keeps today's skipped or error result. The batch
`sync_graph_file` and `VectorProjectionState::sync_file` in `projection/sync.rs`
reuse the same helper, which adds no gateway API. The helper reads graph facts and
symbols at the exact pinned source project, path, and content hash, through
exact-version variants of `read_graph_file_facts` (`crates/gcode/src/db/queries.rs`)
and `fetch_symbols_for_file`
(`crates/gcode/src/vector/code_symbols/repository.rs`). It writes them into the
child overlay project's projection namespace, keyed by that content hash. It then
marks the pin row's target flag with a compare-and-set that matches the same
source project and content hash, with no owned selector or tombstone for the
path.

Fencing follows from that compare-and-set. A re-pin replaces the rows atomically
with fresh flags, so a completion for the old version matches nothing and is
discarded. An owned selector or tombstone that appears mid-sync shadows the pin,
so its completion is discarded too. A purge deletes the rows, so a late completion
also matches nothing. Projected rows that a discarded completion left in the
child namespace are never served, because reads admit only versions named by a
live pin or owned selector, and the next pending pass for the current version
replaces that path's child projection. Reads use the source namespace while the
source selector still names the pinned version, and the child namespace once the
pin's flag is set. Until then they exclude the path and report a degraded
projection state while text and BM25 search stay available. Planned check: Rust
projection tests that move one ancestor path, verify immediate text reads,
observe eventual graph and vector recovery under the child namespace without
rebuilding unchanged paths, and discard stale completions; focused Python
worker tests that a pin alone schedules recovery; and binary-level tests that run
both sync-file commands on a pinned overlay path. The vector run uses a fake
daemon collaborator that serves the handshake, effective config, and Qdrant, the
same pattern as the existing concurrent vector grant test, and proves full
recovery. CI has no FalkorDB lane (retired in #21045), so the graph run stops at
the backend boundary. It proves that the pinned helper ran, that the pin's graph
attempt was recorded and its flag stayed unset, and that the source facts row is
untouched. The graph write itself is the unchanged `code_graph::sync_file_graph`
call, and the helper-level tests cover selection and fencing.

**Granularity:** Projection reconciliation has one background lifecycle and two
projection backends; shared recovery state keeps their failure handling coherent.
The Python worker branch and the Rust pinned sync are the two halves of one
scheduled lifecycle; either alone ships work nothing performs.

**Acceptance:**

- B4.1 - Graph and vector results grant access only to source versions named by pins or child-owned selectors. test: `crates/gcode/src/cli/tests/projection.rs::projection_reads_respect_pin_versions`.
- B4.2 - A moved source projection reprojects only the affected pinned paths, at their exact pinned versions, into the child namespace. test: `crates/gcode/src/projection/sync/tests.rs::reproject_only_moved_pinned_paths`.
- B4.3 - PostgreSQL/BM25 remain usable while projections recover and search reports degraded state. test: `crates/gcode/src/cli/tests/projection.rs::text_search_survives_projection_recovery`.
- B4.4 - A completion for a re-pinned, shadowed, or purged pin is discarded and its projected rows are never admitted by reads. test: `crates/gcode/src/projection/sync/tests.rs::stale_pinned_completion_is_discarded`.
- B4.5 - The daemon worker schedules pinned recovery from pin rows alone, skips shadowed paths, and leaves the source facts row's sync flags untouched. test: `tests/code_index/test_sync_worker_pins.py::test_worker_schedules_pinned_recovery`.
- B4.6 - Under their existing file locks, `gcode vector sync-file` and `gcode graph sync-file` with `--allow-missing-indexed-file` reach the pinned helper for an unowned pinned overlay path. Vector recovers the pinned version into the child namespace and sets the pin's vector flag. Graph records the pin's graph attempt. Neither touches the source facts row, and an unpinned missing path still returns the skipped-missing-indexed-file payload. test: `crates/gcode/tests/projection_stale.rs::pinned_sync_file_routes_through_cli`.

### B5 Pin-aware retention and overlay purge (depends: B3)
`kind: deliverable`
`category: code`
`implementation_domain: backend`

Targets:
- `crates/gcode/src/commands/status/content_gc.rs::*` — scope-reason: pinned source versions are referenced for pruning
- `crates/gcode/src/index/indexer/lifecycle.rs::invalidate`
- `crates/gcode/src/commands/status/content_gc/tests.rs::*` — scope-reason: exercise pin retention with existing GC fixtures
- `crates/gcode/src/commands/status/prune/tests.rs::*` — scope-reason: exercise overlay purge, invalidation, and source retention

**Research context:** Old content versions currently age out after the normal
unreferenced-content period, and `content_gc.rs` owns the only production
delete of content facts. R1 makes a live pin a content-GC root. Pins join the
existing reachability predicate as a content reference, including when the
source project is otherwise stale; add no new retention policy or TTL.

Purge ownership. The stale-project sweep in
`crates/gcode/src/commands/status/prune.rs` locks each stale project and calls
`invalidate_project_locked` (`crates/gcode/src/commands/status/invalidate.rs`),
which delegates to `crates/gcode/src/index/indexer/lifecycle.rs::invalidate`;
that one transaction deletes the project's communities, file states, and project
state (Adversary excerpts `46176df1…` and `724c9e65…`).
`prune/reconcile.rs` only aggregates sweep reports and is not a target.
`invalidate` also deletes, in the same transaction, the pin rows the invalidated
project owns as the overlay, and never rows where it is only the source project,
so a descendant's pins keep their source versions reachable. Normal pruning
collects released versions on the next pass. The same transaction serves the
manual `gcode invalidate`, so invalidating an overlay drops its pin and its next
index is cold until the next pin: agent preflight re-pins before indexing, and a
direct workspace re-pins with `gcode pin`. An overlay with no pin rows inherits
nothing; it never falls back to the parent's current selectors. In an
overlay-of-overlay chain, purging an intermediate overlay must not collect a
source version a live descendant still pins. Planned check: focused Rust
PostgreSQL GC/prune tests for retained, released, invalidated, and
overlay-of-overlay versions.

Consumers unchanged:
- `crates/gcode/src/commands/status/invalidate.rs` — no-edit-reason: invalidate_project_locked keeps calling indexer::invalidate, whose transaction now also removes the owned pin rows.
- `crates/gcode/src/index/indexer.rs` — no-edit-reason: The pub use re-export of lifecycle::invalidate is unchanged.

**Acceptance:**

- B5.1 - Prune retains pinned versions and their source-project facts while any child pin exists. test: `crates/gcode/src/commands/status/content_gc/tests.rs::pinned_version_survives_prune`.
- B5.2 - Child purge and invalidation remove only the pins the child owns, in the invalidation transaction, and later prune can collect unreferenced source facts. test: `crates/gcode/src/commands/status/prune/tests.rs::purged_overlay_releases_pins`.
- B5.3 - Purging an intermediate overlay cannot collect a source version still pinned by a live descendant. test: `crates/gcode/src/commands/status/prune/tests.rs::nested_pin_chain_preserves_source_version`.

## P4: Make unchanged overlay indexing cheap
`kind: framing`

### C1 Candidate-only reconciliation and import resolution (depends: B2, B3)
`kind: deliverable`
`category: code`
`implementation_domain: backend`

Targets:
- `crates/gcode/src/index/indexer/overlay.rs::*` — scope-reason: candidate selection, gaps, discovery, and reconcile share one path
- `crates/gcode/src/index/indexer/local_imports.rs::*` — scope-reason: retain unchanged import providers during candidate resolution
- `crates/gcode/src/index/import_resolution/context.rs::*` — scope-reason: construct candidate-only import context
- `crates/gcode/src/freshness.rs::*` — scope-reason: remove parent moving-HEAD freshness condition
- `crates/gcode/src/index/indexer/tests/overlay.rs::*` — scope-reason: verify candidate sets and fallback
- `crates/gcode/src/index/indexer/tests/facts.rs::*` — scope-reason: verify import-provider retention

**Research context:** Current overlay reconcile performs full discovery and
compares with the parent's moving HEAD/status. The normal candidate set is child
status, persisted pin gaps, `git diff --name-only base_commit HEAD`, and every
path the child owns a selector or tombstone for. The existing
`overlay_reconcile_candidates` already adds the overlay's owned paths
(`crates/gcode/src/index/indexer/overlay.rs`, Adversary excerpt `439ebbd3…`).
Without them, a dirty file reverted to its base content, or an untracked file
indexed and then deleted, drops out of status and diff while its owned selector
or tombstone keeps shadowing the pin. Owned paths are bounded by the child's own
divergence. `overlay_reconcile_action` receives the pinned base row in place of
the parent row, so its existing arms handle every transition out of divergence
with no new mechanism: content equal to the pin inherits and deletes the owned
row, a missing file with no pinned row deletes the owned row, a missing pinned
file is tombstoned, and a tombstoned path that reappears with pinned content
inherits and clears the tombstone. Pinned rows are tracked at `base_commit` by
construction (B2 excludes ignored and untracked paths), so Git status or the diff
reports their deletion. The current branch that adds parent rows Git cannot
report, such as ignored or explicitly indexed parent paths, is removed with the
parent comparison. Route those files directly without a full discovery walk; use
full discovery for `--full` or Git failure. Build import resolution only when a
candidate needs it, while retaining unchanged providers so local imports still
resolve. Drop the parent timestamp freshness trigger. Planned check: isolated
Rust tests for no change, one file, source movement, revert to base, deletion of
an owned untracked file, reappearance of a tombstoned path, Git failure, and
import-provider continuity.

**Granularity:** Reconcile candidate selection and import providers must share
one candidate set; separating them would allow an incomplete index pass.

**Acceptance:**

- C1.1 - No-change and one-file overlays avoid full discovery and ignore parent HEAD movement. test: `crates/gcode/src/index/indexer/tests/overlay.rs::reconcile_uses_child_candidates_only`.
- C1.2 - Full mode and Git failure use complete discovery. test: `crates/gcode/src/index/indexer/tests/overlay.rs::reconcile_falls_back_to_full_discovery`.
- C1.3 - Candidate import resolution retains unchanged providers. test: `crates/gcode/src/index/indexer/tests/facts.rs::candidate_imports_keep_unchanged_providers`.
- C1.4 - Without full discovery, a reverted dirty file, a deleted owned untracked file, and a reappearing tombstoned path each stop shadowing the pin. test: `crates/gcode/src/index/indexer/tests/overlay.rs::owned_rows_leave_divergence_without_discovery`.

### C2 Pre-Leiden community input signature (depends: T1)
`kind: deliverable`
`category: code`
`implementation_domain: backend`

Targets:
- `crates/gcode/src/communities.rs::*` — scope-reason: compare the stored input signature before build_partition
- `crates/gcode/src/communities/partition.rs::*` — scope-reason: compute the input signature and drop the unused post-partition signature
- `crates/gcode/src/communities/refresh_tests.rs::*` — scope-reason: assert unchanged runs never invoke Leiden
- `crates/gcode/src/communities/partition_tests.rs::*` — scope-reason: signature tests move from the partition to its inputs
- `crates/gcode/src/communities/remap/tests.rs::*` — scope-reason: partition fixtures drop the removed signature field

**Research context:** `refresh_project_communities`
(`crates/gcode/src/communities.rs`) loads imports and runs `build_partition`
before it compares the stored partition signature, so the stored value,
`{LABEL_ALGORITHM_VERSION}:{partition_signature}`, is computed after Leiden and an
early skip needs a signature of the partition inputs. The value lives in
`code_indexed_project_states.partition_signature`, written only by
`ReplaceTxn::commit` (`crates/gcode/src/db/communities.rs`, Adversary excerpt
`6256324b…`). Reuse that column with a versioned input-signature contract: the
stored value becomes `input-v1:{LABEL_ALGORITHM_VERSION}:{sha256}`, hashed over
the sorted import identity and rows that `load_project_imports` returns, and
computed before `build_partition`. A match skips Leiden and the replace. A
mismatch, including any stored value from the post-partition contract, runs
Leiden once and commits the input signature. No schema change is needed.
`ReplaceTxn::commit` and `seed_from_parent` are unchanged: seeding copies prior
communities but never the signature, so a first overlay run still runs Leiden.
`Partition::partition_signature` has no other reader and is removed. Trade-off:
a run whose inputs changed but whose partition is identical now rewrites its
community rows through the existing `ReplaceTxn` path; it runs Leiden either
way. Rejected (enhancer E5, Orchestrator ruling 2026-10-05): seeding the child's
communities from the ancestor at pin time and skipping refresh when C1 reports
zero change. The existing Overlay `seed_from_parent` copies the parent's current
partition, which can include dirty or moved-HEAD content, so a skip would serve
communities that do not match the pinned inputs; the input signature is computed
from the child's effective inputs and stays correct for cold and first runs.
Planned check: focused Rust community tests proving zero Leiden calls for
unchanged inputs and one Leiden call for changed inputs or a legacy value.

Consumers unchanged:
- `crates/gcode/src/db/communities.rs` — no-edit-reason: ReplaceTxn::commit stores the signature string it receives, and seed_from_parent never copies a signature.

**Acceptance:**

- C2.1 - An unchanged index run performs no Leiden pass. test: `crates/gcode/src/communities/refresh_tests.rs::unchanged_inputs_skip_leiden`.
- C2.2 - Changed inputs, or a stored post-partition value, run Leiden once and store the versioned input signature. test: `crates/gcode/src/communities/refresh_tests.rs::changed_or_legacy_signature_runs_leiden_once`.

### C3 Overlay phase name, documented contract, and benchmark (depends: C1, C2, B4, B5)
`kind: deliverable`
`category: code`
`implementation_domain: backend`

Targets:
- `src/gobby/agents/code_index.py::*` — scope-reason: phase timing and gcode preflight share one function path
- `src/gobby/agents/spawn_timing.py::*` — scope-reason: replace obsolete spawn phase key
- `tests/agents/test_code_overlay_index_timing.py`
- `docs/guides/code-index.md`
- `docs/guides/gcode-development-guide.md`

**Research context:** Rename `code_index_index` to `code_overlay_index` in the
spawn writer (`src/gobby/agents/code_index.py`) and the `SPAWN_PHASES` catalog
(`src/gobby/agents/spawn_timing.py`, which T1 already extends); the Adversary's
`code_index_index` literal sweep finds only those two files. Document pinned
workspace semantics, the cold fallback, projection recovery, and Ask's
live-index scope. Planned check: Python phase tests; then a load-matched idle
zero-diff spawn using T1's timing, reporting wall time, every named phase,
isolation subphases, and the residual against R2's 33.9 s and 7.064 s baseline,
plus measured pin, one-file overlay, and cold index passes against the 34.9 s and
71.6 s observations. The 2 s target is comparative. No spawn improvement is
claimed without that measured breakdown.

**Granularity:** The rename, guides, and benchmark close the end-to-end
performance contract after every mechanism has landed; the benchmark depends on
C2's signature and on the pinned read and retention paths it measures.

**Acceptance:**

- C3.1 - Spawn timing reports `code_overlay_index`. test: `tests/agents/test_code_overlay_index_timing.py::test_spawn_phase_uses_overlay_name`.
- C3.2 - Guides state the workspace pin and Ask live-index contracts. file: `docs/guides/code-index.md`.
- C3.3 - A load-matched zero-diff spawn records wall time, every named phase, isolation subphases, and the residual against the 33.9 s and 7.064 s idle baseline, and pin, one-file, and cold measurements are recorded against the 34.9 s and 71.6 s observations. behavior: "benchmarked workspace indexing" in `docs/guides/gcode-development-guide.md`.

## V1 Plan Changelog
`kind: verification`

- 2026-10-04: Initial draft under #23433 (`9c8ef7912f`), from the 2026-10-04
  research note and Josh's 2026-10-05 design contract.
- 2026-10-05: Enhancer pass for #23443 (Writer gobby#15400; run `f2fd1c3f`,
  `plan-enhancer-taskless-old`). Five suggestions; the Orchestrator gobby#14972
  accepted all as the Writer recommended. E1: B2 names one pin owner per path
  (`ensure_isolation_code_index` for agent spawns, `creation.py`,
  `_clones_creation.py`, and `workspace_services.py`, moved from A1), and a pin
  failure forces a cold index (B2.4-B2.6). E2: A1 is rewritten against current
  code, since agent spawns already fork the current branch with unpushed commits;
  the delta is one exact `HEAD^{commit}` and aligned MCP, CLI, route, and hook
  surfaces (A1.4 narrowed, A1.5-A1.7 added, `_branch.py` dropped). E3: R2 records
  and attributes the #23433 idle baseline (7.064 s zero-diff `code_index_index`;
  about 24 s untimed, most plausibly Python environment seeding during isolation
  repair), and new first deliverable T1 measures isolation subphases and the wall
  residual; C2.4 requires the measured breakdown. E4: R1 makes the pin govern
  graph and vector reads and act as a GC root; B4 reuses the existing projection
  sync request (B4.4) and B5 covers nested pin chains (B5.3). E5: the core
  (pin-time community seeding with a zero-change skip) is rejected because
  `seed_from_parent` copies the parent's current, possibly dirty partition; its
  side edit drops `docs/plans/gcode-ask-fix.md` from C2. Migration renumbered
  459 to 460 on the Orchestrator's ruling (459 belongs to #23439), after an
  all-ref `git log --all --diff-filter=A` sweep found no 459 or later.
- 2026-10-05: Adversary review of `d8f49d4f6f` (Adversary gobby#15401, Writer
  gobby#15400); all six blocking findings and two clarifications accepted.
  PIN-A1: C1's candidate set keeps owned selector and tombstone paths and feeds
  `overlay_reconcile_action` the pinned row (C1.4). PIN-A2: B4 schedules recovery
  through the existing daemon worker with the pin row as durable work identity,
  exact-version fact reads, a child projection namespace, and compare-and-set
  fencing (B4.4 rewritten, B4.5 added; B1's pin rows carry projection state).
  PIN-A3: B1 owns selector blob writes through one compare-and-set writer and a
  migration trigger that clears a stale blob ID (B1.4). PIN-A4: A1 keeps an
  omitted base as `None` to one handler HEAD resolution, refuses a detached HEAD
  without an explicit base, and drops the `main` and `use_local` defaults (A1.8,
  A2.4). PIN-A5: existing tests that encode the old selection contract became A1
  and A2 Targets, with sweeps and no-edit inventories recorded; T1 keeps handler
  selection in place. PIN-A6: B5 targets `lifecycle.rs::invalidate`, the actual
  purge transaction, and drops `prune.rs` and `prune/reconcile.rs`. B2 separates
  semantic pin failures (cold pin) from storage failures (recoverable error,
  B2.7). C2 is split: C2 reuses `partition_signature` for a versioned pre-Leiden
  input signature, and C3 carries the phase rename, guides, and benchmark.
- 2026-10-05: Adversary recheck of `b1c1db10ef` (Adversary gobby#15401, Writer
  gobby#15400). PIN-A1, PIN-A3, PIN-A5, PIN-A6 and the B2 and C2 clarifications
  are resolved. PIN-A2 follow-through: the worker's gateway runs the `graph
  sync-file` and `vector sync-file` CLI entry points, which bypassed the batch
  helpers, so both now call the pinned helper under their existing file locks
  before missing-file handling, B4 targets `commands/graph/lifecycle.rs`, and B4.6
  proves the route through the binary. PIN-A7: C2 depends on T1, so the timing
  baseline still lands first after the C2/C3 split. PIN-A4 follow-through: R1 and
  A1 state the lifecycle reason for the detached-HEAD refusal. The stored base
  branch is merge start's default target and the deletion merge reference.
  Orchestrator ruling (gobby#14972): keep the refusal, so an omitted base on a
  detached caller HEAD returns `detached_head_requires_base`, with no fallback to
  `main` and no SHA recorded, and the caller passes an explicit base. Forking the
  detached commit with a NULL base branch is rejected because it spreads
  mechanism across about 30 base-branch consumers.

## V2: Verification
`kind: verification`

Each behavior deliverable runs its focused isolated-hub pytest with
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test`
and `GOBBY_TEST_PROTECT=1`, plus focused `cargo test -p gobby-code` and schema
contract tests where Rust or DDL changed. Final checks include Ruff, mypy on
`src/`, the changed-test type and quality audits, and suppression ratchet.
Never run the full pytest suite. After crate changes, rebuild and promote the
coherent Rust binary set with `promote_workspace_binary_set`; coordinate any
daemon restart with a global notice and a quiet window. Record exact commands,
results, and load-matched timings on the task deliverables. T1 lands first; its
idle zero-diff breakdown is the baseline, and no deliverable claims a spawn
improvement without a load-matched breakdown (wall, named phases, isolation
subphases, residual) against it.
