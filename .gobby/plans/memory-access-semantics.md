# Memory access semantics, index dedupe, post-task review, and recall-signal retirement

**Plan ID:** memory-access-semantics

Plan artifact: `.gobby/plans/memory-access-semantics.md`

## Overview
`kind: framing`

Gobby delivers memories two ways: a rule-driven `<memory-index>` of up to five one-line
hits at five bounded moments (turn start, before `spawn_agent`, claiming `create_task`,
`claim_task`, `get_handoff`), and the agent's own `search_memories`. Agents fetch full text
with `get_memory`. Three defects were found while reviewing `docs/research/jev.md`:

- `memories.access_count` and `last_accessed_at` are incremented by the search service on
  every returned hit, including index surfacing that fires every turn
  (`src/gobby/memory/services/search.py`, through the debounced helper in
  `src/gobby/memory/services/_search_access.py` and the single writer
  `MemoryQueryMixin.update_access_stats` in `src/gobby/storage/memories_query.py`).
  `get_memory` records nothing. The counter measures exposure, yet maintenance dedupe and
  dream candidates treat it as use.
- Index de-duplication keys on `injected_memory_ids`, the set of ids whose one-line entry
  was already shown (`InjectionTrackingMixin._filter_and_track_new_memories` in
  `src/gobby/workflows/engine/injection_tracking.py`). A glance is treated as a read, and
  a memory skipped 30 turns ago cannot resurface when it becomes relevant.
- `review_task_memories` (`src/gobby/mcp_proxy/tools/memory_review.py`) never sees what
  the session actually read; it runs a fresh search over title plus change summary.
  Nothing records which memories a session or task loaded.

Separately, the recall-signal stack (nine hub tables, a JSONL log, the shadow judge, refit,
shrinkage, replay, ship gate, drift monitor, and the `gobby memory recall-signals` CLI;
about 8,100 source lines across 24 modules plus 6,200 test lines) is dormant: every flag
defaults off, no row has ever been written on this machine, and its cohort excludes the
index surfacing caller by design. It was built for the retired automatic-recall regime.

Outcome: `access` means an agent opened the memory; `surfaced` means it was shown; ranking
decay follows the later of edit and access; the index re-lists a shown-but-unread memory
after a configurable horizon and never re-lists one already in context; post-task review
starts from what the task actually read; the dormant telemetry stack and the
destructive-migration directive path are gone.

## Decision Record
`kind: framing`

Confirmed by Josh on 2026-09-20 through the elicit interview (gobby#14037):

1. **Access means fetch.** `access_count` and `last_accessed_at` increment only on
   `gobby-memory:get_memory`.
2. **Surfaced means shown.** New columns `surfaced_count` and `last_surfaced_at` on
   `memories`. The existing debounced search-path increment repoints to them and fires
   only for callers that deliver results to an agent or a person: index surfacing
   (`memory.surface`), agent `search_memories`, `review_task_memories`, the web search
   route (`http.memory.search`), and `cli.memory.recall`. Probe searches (create-time
   similarity, review-learning related lessons, the `memory.search` default) do not count.
3. **Migration.** Existing `access_count` copies into `surfaced_count`, `last_accessed_at`
   into `last_surfaced_at`; `access_count` resets to 0 and `last_accessed_at` to NULL.
4. **Temporal anchor.** Ranking decay uses `max(updated_at, last_accessed_at)` with the
   static half-life from config.
5. **Dedupe.** Permanent suppression keys on a new per-session `accessed_memory_ids` set.
   `injected_memory_ids` is renamed `surfaced_memory_ids` and becomes the short-horizon
   shown set: each id stamped with a per-session injection sequence, re-eligible after K
   further surfacings.
6. **K is config.** `memory.index_reshow_after_injections`, integer, default 5, registered
   in the config store like the other memory knobs.
7. **Reset.** Both sets reset on session start, clear, and compaction. Both existing
   lifecycle rules already clear `injected_memory_ids`; they clear the new variables too.
8. **Fetch tagging.** Each accessed id is recorded with the task claimed at fetch time, or
   untagged when none is claimed.
9. **Post-task review.** Candidates are the accessed ids tagged with the closing task plus
   untagged accessed ids, from the closing session and the session that closed the task,
   marked `source: accessed`; then the existing search candidates minus any already listed.
10. **Retirement.** Delete the hub tables, both telemetry flags and the log path, shadow
    judge, refit, shrinkage, replay, ship gate, drift, audit CLI, the usefulness-label
    contract doc, and their tests. Removed config keys raise on load. Keep
    `temporal_decay`/`undecay`, the static half-life field, and the search `caller`.
11. **Hygiene consumers.** Maintenance dedupe and dream candidates keep reading
    `access_count` / `last_accessed_at` unchanged; the inputs now mean fetches.
12. **Route.** Gobby plan artifact plus epic; this file is the authority.
13. **Dream gets both counters.** `surfaced_count` / `last_surfaced_at` join the dream
    candidate and curator payloads; the dream prompt's three `access_count` sentences are
    rewritten: surfaced measures exposure, access measures deliberate reads, high surfaced
    with zero access flags a misfiring `when:` clause or noise, both near zero over a long
    age may corroborate delete.
14. **Retire the destructive-migration directive path.** It was built for hub backup and
    restore to a new machine, never for migrations, and three of the four table-dropping
    migrations (421, 427, 439) already bypass it. Remove the runner's stamping and
    authorization branches, `gdaemon schema apply --destructive` with its epoch-lease and
    backup-manifest helpers, and the `schema-apply` maintenance campaign. Keep hub backup,
    restore, and the maintenance-epoch framework. `426_retire_legacy_wiki.sql` keeps its
    bytes (receipts are checksummed); once the directive means nothing its `IF EXISTS`
    drops execute harmlessly on fresh lineages.
15. **One rebuild and promote, at the end.** Partial epics never land as production
    binaries. Every Rust and asset change is verified in-leaf with cargo tests and
    `GOBBY_TEST_GDAEMON=checkout`; the coherent set is promoted once, in the live
    verification that closes the epic.

## Constraints
`kind: framing`

- `$PG` below means
  `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest`.
  Every leaf also runs `uv run ruff format src/`, `uv run ruff check src/`,
  `uv run mypy src/`, the scoped test-types audit for changed tests
  (`uv run gobby test-types audit <paths> --baseline .gobby/test-types-baseline.json --fail-on-new`),
  updates docs in the same commit, keeps every hand-maintained source file under 1,000
  lines, and leaves its own commit green.
- Test fixtures build the test schema with the installed `~/.gobby/bin/gdaemon`
  (`tests/conftest.py`, `tests/fixtures/gdaemon_binary.py`). From 2.1 until the live
  cutover, every pytest run that touches the hub prefixes `GOBBY_TEST_GDAEMON=checkout`
  after `cargo build -p gobby-daemon`.
- Do not restart the development daemon between 2.1 and the live cutover: once
  `src/gobby/storage/schema_expected_identity.json` is bumped, the restart preflight
  refuses the still-installed binary. The cutover is one announced
  `global` `send_message`, `cargo build --release`, `promote_workspace_binary_set` for
  `gcode`, `gdaemon`, `ghook`, a separate `gclient` promotion, and `gobby restart`.
- Dependency chain. Shared carriers (`docs/guides/memory.md`,
  `src/gobby/mcp_proxy/tools/memory.py`, `memory_surface.py`, `memory_review.py`, the
  reference files) appear in most leaves, so the chain is mostly linear: 1.1 → 1.2 →
  1.3 → 2.1 → 2.2 → {3.1, 3.2} → 3.3 → 3.4; 4.1 runs after 2.1 in parallel with 2.2–3.4.
  Only 3.1 ∥ 3.2 and 4.1 ∥ 2.2–3.4 have disjoint targets.
- New tests go in new modules where the existing module already exceeds 1,000 lines:
  `tests/memory/test_dream.py`, `tests/mcp_proxy/tools/test_memory_tools.py`, and
  `tests/memory/test_search_ranking.py`.
- Found work carried by this plan rather than fixed ad hoc: `injected_review_lesson_ids`
  is never reset although `docs/guides/memory.md` says it is (3.3); the deferred
  `search_memories` dedupe chain is unreachable because no bundled rule dispatches
  `search_memories` (3.3); `tests/memory/test_dream.py` hardcodes a 16-column INSERT with
  `strict=True` while `storage_journal._MEMORY_COLUMNS` is 19 wide (2.2);
  `references/memory/maintenance.md` says dedupe keeps the earliest row, contradicting
  `services/maintenance.py` (3.1).
- Load the `rust` skill before editing crates (2.1, 4.1). Regenerated files are never
  hand-edited: `crates/gcore/assets/config/runtime_config_contract.json` and
  `web/src/api/runtimeConfigCodecVectors.gen.ts` come from
  `scripts/generate_runtime_config_contract.py`;
  `src/gobby/storage/schema_expected_identity.json` from
  `scripts/generate_schema_expected_identity.py`.

## P1: Retire the recall-signal stack and register the re-show horizon
`kind: framing`

Retirement lands first so the later leaves never thread the new columns through modules
that are about to disappear. 1.1 removes the judge side (everything that reads the
shadow tables or runs the drift loop) while leaving the tree importable; 1.2 removes the
sink side, the storage layer, and the search-debug plumbing that exists only to feed it;
1.3 removes the thirteen config keys, adds `memory.index_reshow_after_injections`, and
regenerates the runtime contract once.

### 1.1 Retire the shadow judge, drift monitor, judge rule, judge tool, and recall-signals CLI [category: refactor]
`kind: deliverable`

Targets:
- `src/gobby/memory/shadow_relevance.py::*` — operation: delete — scope-reason: retire the entire shadow judge module
- `src/gobby/memory/recall_drift.py::*` — operation: delete — scope-reason: retire the entire drift evaluator
- `src/gobby/cli/memory/signals.py::*` — operation: delete — scope-reason: retire the `gobby memory recall-signals` command group
- `src/gobby/install/shared/workflows/rules/memory-lifecycle/judge-shadow-relevance-on-response.yaml::*` — operation: delete — scope-reason: retire the judge rule
- `src/gobby/cli/memory/main.py::*` — scope-reason: drop the import and `add_command` registration of the signals group
- `src/gobby/mcp_proxy/tools/memory.py::*` — scope-reason: drop the `judge_shadow_candidate_relevance` import and the `judge_shadow_relevance` tool registration
- `src/gobby/runner_maintenance/telemetry_loops.py::*` — scope-reason: delete `recall_drift_monitor_loop`
- `src/gobby/runner_maintenance/__init__.py::*` — scope-reason: drop the loop re-export
- `src/gobby/runner_lifecycle_periodic.py::*` — scope-reason: drop the loop registration, task creation, and cancel entry
- `src/gobby/runner_lifecycle_shutdown.py::*` — scope-reason: drop `_recall_drift_task` from the cancel list
- `src/gobby/runner.py::*` — scope-reason: drop the `_recall_drift_task` attribute
- `tests/memory/test_shadow_relevance_judge.py::*` — operation: delete — scope-reason: tests of the deleted judge
- `tests/memory/test_recall_drift.py::*` — operation: delete — scope-reason: tests of the deleted evaluator
- `tests/cli/test_cli_memory_signals.py::*` — operation: delete — scope-reason: tests of the deleted CLI group
- `tests/test_runner_recall_drift.py::*` — operation: delete — scope-reason: tests of the deleted loop wiring
- `tests/hooks/test_mcp_dispatch_async.py::*` — scope-reason: two cases dispatch `judge_shadow_relevance`; retarget them at a surviving memory tool
- `tests/workflows/test_rule_engine.py::*` — scope-reason: the judge-rule cases go
- `tests/mcp_proxy/tools/test_memory.py::*` — scope-reason: the judge-tool cases go
- `tests/workflows/test_memory_lifecycle_rules.py::*` — scope-reason: remove the judge rule from `MEMORY_RULES`
- `docs/guides/memory.md`
- `docs/guides/sessions.md`
- `docs/guides/cli-commands.md`
- `docs/contracts/session-boundary.md`
- `docs/reference-audit/memory.json::*` — scope-reason: remove the judge tool and the seven recall-signals CLI operations
- `src/gobby/install/shared/skills/gobby/references/memory/maintenance.md`
- `src/gobby/install/shared/skills/gobby/references/memory/search.md`

**Research context:** Observed consumers (gcode grep, 2026-09-20): `shadow_relevance` is
imported by `src/gobby/mcp_proxy/tools/memory.py` (import at line 36; tool
`judge_shadow_relevance_tool` at 747-767) and `src/gobby/cli/memory/signals.py`; `recall_drift` by
`src/gobby/cli/memory/signals.py` and `recall_drift_monitor_loop` in
`src/gobby/runner_maintenance/telemetry_loops.py` (near line 159-197), which is re-exported
by `runner_maintenance/__init__.py`, registered and started in
`runner_lifecycle_periodic.py` (the `loops` map, `runner._recall_drift_task`), cancelled in
`runner_lifecycle_shutdown.py`, and declared on `Runner` in `runner.py`. The storage
modules `src/gobby/storage/recall_shadow_*.py` stay until 1.2 because
`src/gobby/storage/recall_signals.py` (deleted in 1.2) imports all five; deleting them here
would break the daemon import chain through `memory/manager.py`. The bundled rule
`judge-shadow-relevance-on-response.yaml` dispatches the tool; `tests/docs/test_memory_guides.py`
checks the rules table in `docs/guides/memory.md` against the bundled rules, so the table
row goes with the rule. `docs/reference-audit/memory.json` enumerates the tool and seven
CLI operations; `tests/skills/test_reference_library.py::test_reference_contract_3_2_1` enforces parity
through `coverage_errors` in `tests/skills/reference_library_helpers.py`. The config fields these modules read (`shadow_relevance_judging`,
`recall_drift_*`) stay until 1.3 so mypy and config loading remain green after this leaf.
Rejected: deleting the storage layer here (import chain); keeping the CLI group with a
stub (nothing to audit once the tables go).

Verification: `$PG tests/workflows/test_memory_lifecycle_rules.py tests/workflows/test_rule_engine.py tests/hooks/test_mcp_dispatch_async.py tests/mcp_proxy/tools/test_memory.py tests/docs/test_memory_guides.py tests/skills/test_reference_library.py tests/cli/test_memory_cli.py tests/runner_maintenance -q`
plus `uv run gobby start --verbose` smoke is not required; `uv run python -c "import gobby.runner"` proves the import chain.

**Granularity:** more than six targets, one outcome: the judge side is removed as a unit
because every file listed either is the judge or exists only to call it.

**Acceptance:**

- 1.1.1 - The judge, drift evaluator, and CLI group modules no longer exist and nothing under `src/` imports them. file: `src/gobby/memory/shadow_relevance.py`. file: `src/gobby/memory/recall_drift.py`. file: `src/gobby/cli/memory/signals.py`.
- 1.1.2 - The memory tool registry no longer registers `judge_shadow_relevance` and the bundled rule that dispatched it is gone. file: `src/gobby/mcp_proxy/tools/memory.py`. file: `src/gobby/install/shared/workflows/rules/memory-lifecycle/judge-shadow-relevance-on-response.yaml`.
- 1.1.3 - The runner starts and shuts down without a drift-monitor loop. symbol: `recall_drift_monitor_loop`. behavior: `uv run python -c "import gobby.runner"` succeeds after the deletions.
- 1.1.4 - The rules table in `docs/guides/memory.md` and the reference audit match the bundled rules and tools. test: `tests/docs/test_memory_guides.py::test_memory_guide_lists_every_bundled_lifecycle_rule`. test: `tests/skills/test_reference_library.py::test_reference_contract_3_2_1`.
- 1.1.5 - The four deleted test modules are gone and the four edited test modules pass without the judge. behavior: the verification command above passes.

### 1.2 Retire the telemetry sinks, fit, gate, replay, storage layer, and search-debug plumbing [category: refactor] (depends: 1.1)
`kind: deliverable`

Targets:
- `src/gobby/memory/recall_constants.py::*` — operation: delete — scope-reason: fitted-constant resolution goes
- `src/gobby/memory/recall_fit.py::*` — operation: delete — scope-reason: labeled fit goes
- `src/gobby/memory/recall_fit_shrinkage.py::*` — operation: delete — scope-reason: shrinkage goes
- `src/gobby/memory/recall_refit.py::*` — operation: delete — scope-reason: refit goes
- `src/gobby/memory/recall_replay.py::*` — operation: delete — scope-reason: replay goes
- `src/gobby/memory/recall_ship_gate.py::*` — operation: delete — scope-reason: ship gate goes
- `src/gobby/memory/recall_ship_gate_run.py::*` — operation: delete — scope-reason: ship-gate runner goes
- `src/gobby/memory/recall_signal_log.py::*` — operation: delete — scope-reason: JSONL sink and outcome recorder go
- `src/gobby/storage/recall_signals.py::*` — operation: delete — scope-reason: hub sink store goes
- `src/gobby/storage/recall_shadow_signals.py::*` — operation: delete — scope-reason: shadow store goes
- `src/gobby/storage/recall_shadow_labels.py::*` — operation: delete — scope-reason: shadow labels go
- `src/gobby/storage/recall_shadow_gate.py::*` — operation: delete — scope-reason: shadow gate store goes
- `src/gobby/storage/recall_shadow_sampling.py::*` — operation: delete — scope-reason: shadow sampling goes
- `src/gobby/storage/recall_shadow_claim_transitions.py::*` — operation: delete — scope-reason: shadow claim transitions go
- `src/gobby/memory/services/_search_debug.py::*` — operation: delete — scope-reason: the debug snapshot exists only to feed the sink
- `docs/contracts/memory-usefulness-label.md`
- `src/gobby/memory/services/search.py::*` — scope-reason: static half-life and graph discount replace the constants object; drop the sink and constants kwargs, `_emit_search_debug`, and the `recall_request_id` thread
- `src/gobby/memory/services/_search_paths.py::*` — scope-reason: drop the `_emit_search_debug` protocol member and the `recall_request_id` parameters
- `src/gobby/memory/services/_search_models.py::*` — scope-reason: delete `SearchDebugHit` and `SearchDebugSnapshot`, keep `_Candidates`
- `src/gobby/memory/facade.py::*` — scope-reason: drop the `recall_request_id` parameter of the search entry point
- `src/gobby/memory/manager.py::*` — scope-reason: drop the constants, sink, and outcome-recorder construction; pass `cooccur_alpha=None, cooccur_support_cap=None`
- `src/gobby/mcp_proxy/tools/memory.py::*` — scope-reason: delete `_record_delivered_hits`, the `recall_request_id` minting, and the payload key
- `src/gobby/mcp_proxy/tools/memory_review.py::*` — scope-reason: drop `recall_request_id` minting and pass-through
- `src/gobby/mcp_proxy/tools/memory_surface.py::*` — scope-reason: drop `recall_request_id` pass-through
- `tests/memory/test_recall_constants.py::*` — operation: delete — scope-reason: tests of deleted module
- `tests/memory/test_recall_fit.py::*` — operation: delete — scope-reason: tests of deleted module
- `tests/memory/test_recall_refit.py::*` — operation: delete — scope-reason: tests of deleted module
- `tests/memory/test_recall_signal_log.py::*` — operation: delete — scope-reason: tests of deleted module
- `tests/storage/test_recall_signals.py::*` — operation: delete — scope-reason: tests of deleted store
- `tests/storage/test_recall_shadow_signals.py::*` — operation: delete — scope-reason: tests of deleted store
- `tests/storage/recall_signal_fixtures.py::*` — operation: delete — scope-reason: fixtures of deleted stores
- `tests/memory/test_recall_benchmark.py::*` — scope-reason: keep the synthetic `test_recall_benchmark_arms`, drop the labeled-fit half
- `tests/memory/test_search_ranking.py::*` — scope-reason: constants and debug-snapshot assertions go
- `tests/memory/test_memory_manager_1.py::*` — scope-reason: sink and constants construction cases go
- `tests/mcp_proxy/tools/test_memory_tools.py::*` — scope-reason: the `recall_request_id` payload assertion goes
- `tests/mcp_proxy/tools/test_memory_review.py::*` — scope-reason: `test_each_review_mints_its_own_recall_request_id` goes
- `tests/mcp_proxy/tools/test_memory_surface.py::*` — scope-reason: the `recall_request_id` assertion goes
- `docs/guides/memory.md`
- `docs/guides/observability.md`
- `src/gobby/install/shared/skills/gobby/references/memory/search.md`
- `.gobby/plans/hub-data-retention.md`
- `.gobby/plans/memory-surfacing.md`

**Research context:** Keeper edges observed 2026-09-20. `services/search.py` reads the
half-life from the constants object (near line 153) and the graph synthetic-similarity
discount (near line 403); replace with `float(self._config.temporal_decay_half_life_days)`
and `_GRAPH_SYNTHETIC_SIM_DISCOUNT` from `src/gobby/memory/services/_search_constants.py`
(read-only). The `SearchService` constructor takes `recall_constants` and
`search_debug_sink` kwargs (near lines 60-77); `manager.py` builds them from
`resolve_recall_constants(config)` and `make_recall_signal_sink(...)` (near lines 20-23,
105, 171-178) and sets `injection_outcome_recorder`, which only
`_record_delivered_hits` in `mcp_proxy/tools/memory.py` (lines 52-84, called at 270) consumes. `manager.py` passes `cooccur_alpha` / `cooccur_support_cap` from the
constants when `source == "fitted"` (near lines 307-315); the graph reader and writer
already fall back when they receive `None`. Keep the search `caller` parameter everywhere
(Decision 10); it gates the surfaced increment in 3.1. The `recall_request_id` thread runs
`facade.py` (near 197, 213) → `search.py` → `_search_paths.py`; the three tools mint one
per call (`memory.py` near 207/227/280, `memory_review.py` near 192-206,
`memory_surface.py` near 131). Drop the `"recall_request_id"` key from the
`search_memories` payload; `references/memory/search.md` tells agents to keep it for
diagnostics, so that sentence goes. `config/persistence.py` still declares the stack flags
after this leaf (1.3 removes them); nothing reads them once the modules are gone.
Active plans `.gobby/plans/hub-data-retention.md` (retention cohort of the recall tables)
and `.gobby/plans/memory-surfacing.md` (task 1.3 and six references) get a scope note
saying the stack was retired by this plan; research write-ups and frozen evidence dumps
stay untouched. Rejected: keeping `_search_debug.py` as a generic diagnostic (no consumer).

Verification: `$PG tests/memory/test_search_ranking.py tests/memory/test_recall_benchmark.py tests/memory/test_memory_manager_1.py tests/memory/test_manager_graph_search.py tests/mcp_proxy/tools/test_memory_tools.py tests/mcp_proxy/tools/test_memory_review.py tests/mcp_proxy/tools/test_memory_surface.py tests/storage/test_storage_memories.py tests/docs/test_memory_guides.py tests/skills/test_reference_library.py -q`

**Granularity:** more than six targets, one outcome: every module here is the sink or
exists only to feed it; the keeper edges are the minimal cuts that leave search intact.

**Acceptance:**

- 1.2.1 - The eight `memory/recall_*.py` modules, the seven `storage/recall_*.py` modules, `_search_debug.py`, and the usefulness-label contract no longer exist and nothing under `src/` imports them. file: `src/gobby/memory/recall_signal_log.py`. file: `src/gobby/storage/recall_signals.py`. file: `docs/contracts/memory-usefulness-label.md`.
- 1.2.2 - `SearchService` takes no constants object or debug sink; the half-life comes from `temporal_decay_half_life_days` and the graph discount from `_search_constants`. symbol: `SearchService`. file: `src/gobby/memory/services/search.py`.
- 1.2.3 - `MemoryManager` builds no sink, constants, or outcome recorder and the graph reader and writer receive `None` co-occurrence parameters. file: `src/gobby/memory/manager.py`.
- 1.2.4 - The `search_memories` payload, the review tool, and the surface tool carry no `recall_request_id`, and the search entry points accept none. file: `src/gobby/mcp_proxy/tools/memory.py`. file: `src/gobby/memory/facade.py`.
- 1.2.5 - Ranking behavior is unchanged: the synthetic benchmark arms still pass. test: `tests/memory/test_recall_benchmark.py::test_recall_benchmark_arms`.
- 1.2.6 - Docs and the two active plans no longer describe the stack as live. behavior: "recall-signal" prose in `docs/guides/memory.md` and `docs/guides/observability.md` describes only the retirement; scope notes in `.gobby/plans/hub-data-retention.md` and `.gobby/plans/memory-surfacing.md`.

### 1.3 Config: remove the stack keys, add the re-show horizon, regenerate the runtime contract [category: config] (depends: 1.2)
`kind: deliverable`
[domain: fullstack]

Targets:
- `src/gobby/config/persistence.py::*` — scope-reason: delete ten `MemoryConfig` fields and three validators, add `index_reshow_after_injections`, add the before-validator that rejects removed keys, reword the half-life description
- `src/gobby/config/sessions.py::*` — scope-reason: delete `MemoryUsefulnessConfig` and its `__all__` entry
- `src/gobby/config/app.py::*` — scope-reason: unmount `memory_usefulness`; add it to `reject_removed_session_title_config`
- `src/gobby/config/registry.py::*` — scope-reason: sweep persisted rows for the removed keys beside the embedding-key sweep
- `src/gobby/runner_init/config_subscribers.py::*` — scope-reason: drop the `memory_usefulness` route
- `crates/gcore/assets/config/runtime_config_contract.json::*` — scope-reason: regenerated by the contract generator
- `web/src/api/runtimeConfigCodecVectors.gen.ts::*` — scope-reason: regenerated by the contract generator
- `web/src/components/settings/sections/MemoryKnowledgeSection.tsx::*` — scope-reason: drop the two retired toggles
- `web/src/components/settings/sections/__tests__/MemoryKnowledgeSection.test.tsx::*` — scope-reason: fixture keys and toggle cases
- `web/tests/style-surfaces.spec.ts::*` — scope-reason: two retired toggle selectors
- `tests/config/test_memory_config.py::*` — scope-reason: `test_recall_signal_logging_defaults_off` goes; `hasattr` pin and default for the new key
- `tests/config/test_persistence.py::*` — scope-reason: removed-key rejection cases replace the stack-flag cases
- `tests/config/test_app_config.py::*` — scope-reason: default assertion for the new key; `memory_usefulness` rejection
- `tests/storage/test_config_store.py::*` — scope-reason: the removed-key sweep case
- `docs/guides/memory.md`
- `docs/guides/configuration.md`
- `docs/audits/configuration-audit.md`

**Research context:** The runtime config store is DB-backed and registry-driven: adding
the Pydantic field is the registration. `MemoryConfig` (`src/gobby/config/persistence.py`,
class near line 437) declares `recall_signal_logging`, `recall_signal_log_path`,
`recall_signal_log_max_mb`, `recall_signal_hub`, `shadow_relevance_judging`,
`use_fitted_recall_constants`, `fitted_recall_decision_path`,
`recall_drift_monitor_enabled`, `recall_drift_interval_hours`,
`recall_drift_accuracy_drop` (near lines 560-638) plus three validators (near 640-661);
`MemoryUsefulnessConfig` lives in `config/sessions.py` (near line 95), is mounted on
`DaemonConfig` in `config/app.py` (near lines 64, 287-289) and routed in
`runner_init/config_subscribers.py` (near line 234). `MemoryConfig` is `extra="ignore"`,
so removed keys need a `model_validator(mode="before")` on `MemoryConfig` that raises
naming the key, and `memory_usefulness` joins `reject_removed_session_title_config`
(`config/app.py`, near line 157) — precedent: the memory-recall removal. Persisted rows
survive silently otherwise (the failure recorded in
`.gobby/plans/completed/two-daemon-hub.md`); sweep them the way
`is_removed_embedding_config_store_key` (`config/embedding_keys.py`) is consumed from
`config/registry.py`: a `REMOVED_MEMORY_CONFIG_KEYS` constant next to `MemoryConfig`
feeds both the validator and the registry sweep. New field:
`index_reshow_after_injections: int = Field(default=5, ge=1, description=...)` near
`access_debounce_seconds` (near line 471). The registry auto-discovers it
(`_walk_daemon_model` in `config/registry.py`), activation falls through to LIVE, and the
`memory` root already has a live subscriber route, so no route edit. Also reword the
`temporal_decay_half_life_days` description ("since last update" becomes "since the later
of last update and last access") here so the contract regenerates once. Regenerate with
`python scripts/generate_runtime_config_contract.py`, which rewrites the gcore JSON and the
web codec vectors; `tests/config/test_runtime_config_contract.py` is byte-equality against
the generator. Precedent commit for a new memory knob: `4b8955a265` (field + regenerated
contract + consumer + default test + guide docs; nothing in Rust types, the audit matrix,
or the web sections). Web: `MemoryKnowledgeSection.tsx` renders the two retired toggles
(near lines 43-44, 193-206); its test fixtures (near 84-88, 200-201) and
`web/tests/style-surfaces.spec.ts` (near 795-796) name them. No web field is added for K
(precedent); a later audit-matrix row would also need `MEMORY_PATHS` and a
`NumberConfigField`, or `sections.coverage.test.ts` fails. Audit doc: reword the
`memory.temporal_decay_half_life_days` row (line 314) for the new anchor. The matrix carries
no row for the thirteen removed keys (none at plan time, commit `1c126374`, nor on HEAD), so
there is nothing to retire there;
`tests/config/test_config_authority_audit.py::test_audit_registration_claims_match_runtime_contract`
keeps the matrix honest. Set K with
`gobby-config:patch_config_values` (`{"values": {"memory": {"index_reshow_after_injections": 3}}}`).

Verification: `python scripts/generate_runtime_config_contract.py && git diff --stat crates/gcore/assets/config/ web/src/api/`;
`$PG tests/config tests/storage/test_config_store.py tests/runner_init -q`;
`cd web && npx vitest run src/components/settings/sections/__tests__/ src/api/__tests__/runtimeConfigSegments.test.ts`;
`cargo test --manifest-path crates/gcode/Cargo.toml runtime_contract`.

**Granularity:** more than six targets, one outcome: one contract regeneration covers the
removals and the addition, and the web toggles only exist for removed keys.

**Acceptance:**

- 1.3.1 - `MemoryConfig` has no recall-signal, shadow-judge, fitted-constant, or drift field, and loading a config that names one raises with the key in the message. symbol: `MemoryConfig`. test: `tests/config/test_persistence.py::test_removed_memory_keys_raise`.
- 1.3.2 - `DaemonConfig` has no `memory_usefulness` section and rejects one. symbol: `reject_removed_session_title_config`. test: `tests/config/test_app_config.py::test_memory_usefulness_section_rejected`.
- 1.3.3 - `memory.index_reshow_after_injections` defaults to 5, rejects values below 1, and appears in the regenerated contract. test: `tests/config/test_memory_config.py::test_index_reshow_after_injections_default`. test: `tests/config/test_runtime_config_contract.py::test_contract_matches_generator`.
- 1.3.4 - Persisted `config_store` rows for the thirteen removed keys are deleted at load. test: `tests/storage/test_config_store.py::test_removed_memory_keys_swept`.
- 1.3.5 - The web settings section renders neither retired toggle and its tests and style spec pass. file: `web/src/components/settings/sections/MemoryKnowledgeSection.tsx`. behavior: the vitest command above passes.
- 1.3.6 - Guides list the new key and the audit matrix's half-life row describes the new anchor. file: `docs/guides/memory.md`. file: `docs/guides/configuration.md`. file: `docs/audits/configuration-audit.md`.

## P2: Schema and column threading
`kind: framing`

One plain migration adds the surfaced columns, moves the existing counts across, and drops
the nine recall tables; the Python model layer then carries the new columns everywhere
`access_count` / `last_accessed_at` already travel.

### 2.1 Migration 452 and its schema carriers [category: code] (depends: 1.3)
`kind: deliverable`
[domain: backend]

Targets:
- `crates/gcore/assets/schema/migrations/452_memory_surfaced_stats_retire_recall_signals.sql`
- `crates/gcore/src/schema/assets.rs::*` — scope-reason: append the `EmbeddedMigration` entry for 452
- `crates/gcore/assets/schema/catalog.manifest.json::*` — scope-reason: add the two memories columns and remove the nine recall tables
- `crates/gcore/tests/schema_contract.rs::*` — scope-reason: bump the identity literals
- `crates/gcore/src/grant/bundle.rs::*` — scope-reason: bump the `latest_version` literal of the no-postgres fallback (line 218); no grant names a recall table
- `crates/gdaemon/tests/cli_contract.rs::*` — scope-reason: bump the identity literals
- `src/gobby/storage/schema_expected_identity.json::*` — scope-reason: regenerated identity pin
- `tests/fixtures/test_postgres_db_reset.py::*` — scope-reason: the RESTRICT-FK fixture pair used `recall_holdout_consumed -> recall_gate_runs`; repoint at `machines.owner_user_id -> users.id`
- `tests/storage/test_domain_tables_schema.py::*` — scope-reason: pin `memories.surfaced_count` and `memories.last_surfaced_at`

**Research context:** Schema authority is Rust: Python has no DDL
(`tests/storage/test_schema_contract.py`). Baseline is at 420; the newest committed migration is
449 (`449_workspace_default_project.sql`), 450 is #22740's
(`450_drop_session_heuristic_title.sql`, being built on epic-rust-gclient and landing first)
and 451 is the runbooks plan's (#22808), so this plan takes 452 (Program Director,
2026-09-24; the epic asked for the renumber at review). Templates: migration 436 (add columns, commit `faadba03d1`) and
`439_retire_linear_github_issue_bridge.sql` (multi-table drop as a plain migration). The
single file, in order: `ALTER TABLE memories ADD COLUMN surfaced_count integer DEFAULT 0,
ADD COLUMN last_surfaced_at timestamp with time zone;` then `UPDATE memories SET
surfaced_count = access_count, last_surfaced_at = last_accessed_at, access_count = 0,
last_accessed_at = NULL;` then one `DROP TABLE IF EXISTS ... RESTRICT` naming
`recall_gate_runs`, `recall_holdout_consumed`, `recall_injection_outcomes`,
`recall_shadow_audit_verdicts`, `recall_shadow_judge_state`,
`recall_shadow_prompt_snapshot`, `recall_signal_hits`, `recall_signal_requests`,
`recall_usefulness` (all nine are created by `baseline.sql` with 3 owned sequences, 7
indexes, and one RESTRICT FK from `recall_holdout_consumed` to `recall_gate_runs`, which
resolves inside a single statement). No `-- gobby:destructive` directive: the runner
stamps directive-marked migrations without executing them on every fresh lineage
(`crates/gcore/src/schema/runner.rs`, `stamps_destructive_migrations`, and
`runner_plan.rs`), which would leave the nine tables on every new install and every test
database; a plain migration executes right after the baseline on fresh lineages and at the
next apply on installed hubs. Carriers per the derived-carriers table: append the
`EmbeddedMigration` in `assets.rs`; insert `memories.last_surfaced_at` after
`memories.last_dreamed_at` (line 2420 on HEAD) and `memories.surfaced_count` after
`memories.source_type` (line 2444) in `catalog.manifest.json` with definitions
`"timestamp with time zone|timestamptz|YES||NEVER"` and `"integer|int4|YES|0|NEVER"`,
(the integer definition is the one `memories.access_count` carries at line 2368)
and remove the about 140 entries for the nine tables; bump the identity literals:
`crates/gcore/tests/schema_contract.rs` lines 21 and 24 (the version and the newest file
name), `crates/gcore/src/grant/bundle.rs` line 218 (`latest_version: 449` in the
`#[cfg(not(feature = "postgres"))]` fallback beside `GOLDEN_LATEST_CHECKSUM`, set by #22809
in `8039cebc41`; its guard `grant::tests::expected_schema_identity_tracks_catalog_head` runs
only under gcore's default features, memory 24090e86), and
`crates/gdaemon/tests/cli_contract.rs` line 58. The grant bundle names no recall table (the
nine appear only in `baseline.sql` and the catalog manifest), so nothing else in
`bundle.rs` changes; regenerate
`src/gobby/storage/schema_expected_identity.json` with
`python scripts/generate_schema_expected_identity.py`. `verify.rs` is not a carrier
(memories is not seed-managed). `crates/gcore/tests/catalog_manifest_freshness.rs` pins the
manifest against baseline plus migrations. The golden grant vectors under
`tests/runtime_grants/golden/` are fixture-pinned (`tests/runtime_grants/test_golden_vectors.py`):
run, do not edit. The shipped refit decision JSON in the user's home directory is a file,
not a table; nothing to drop. Known window: between this leaf and 3.1 the old search-path increment briefly
accrues exposure into the zeroed `access_count`; land 2.2 and 3.1 in the same session.

Verification: `cargo build -p gobby-daemon && cargo test --manifest-path crates/gcore/Cargo.toml --test schema_contract --test catalog_manifest_freshness && cargo test --manifest-path crates/gdaemon/Cargo.toml --test cli_contract`;
`cargo test -p gobby-core --lib grant::tests` without `--features postgres` (the no-postgres identity guard, memory 24090e86);
`GOBBY_TEST_GDAEMON=checkout $PG tests/storage/test_schema_contract.py tests/storage/test_schema_divergence.py tests/storage/test_domain_tables_schema.py tests/fixtures/test_postgres_db_reset.py tests/runtime_grants/test_golden_vectors.py tests/storage/test_storage_memories.py -q`;
`cmp <(python scripts/generate_schema_expected_identity.py --stdout) src/gobby/storage/schema_expected_identity.json` or the generator's own idempotence check.

**Granularity:** nine targets, one outcome: a migration and its required carriers are one
identity bump and cannot be split without a mixed installed set.

**Acceptance:**

- 2.1.1 - Migration 452 adds both columns, copies the counts across, resets access, and drops the nine tables in one plain file with no destructive directive. file: `crates/gcore/assets/schema/migrations/452_memory_surfaced_stats_retire_recall_signals.sql`.
- 2.1.2 - The catalog manifest lists `memories.surfaced_count` and `memories.last_surfaced_at` and no `recall_*` table, and the freshness test passes. file: `crates/gcore/assets/schema/catalog.manifest.json`. behavior: `cargo test --manifest-path crates/gcore/Cargo.toml --test catalog_manifest_freshness` passes.
- 2.1.3 - Every identity carrier agrees with the checkout binary. file: `src/gobby/storage/schema_expected_identity.json`. test: `tests/storage/test_schema_contract.py::test_expected_identity_matches_gdaemon`.
- 2.1.4 - The reset fixture's RESTRICT-FK pair is a surviving pair. test: `tests/fixtures/test_postgres_db_reset.py::test_reset_handles_restrict_fk_order`.
- 2.1.5 - The domain-table pin covers both new columns. test: `tests/storage/test_domain_tables_schema.py::test_memories_surfaced_columns`.

### 2.2 Thread surfaced_count and last_surfaced_at through storage, protocol, dream, CLI, and web [category: code] (depends: 2.1)
`kind: deliverable`
[domain: fullstack]

Targets:
- `src/gobby/storage/memories_models.py::*` — scope-reason: normalizer `optional`, dataclass fields, `from_row`, `to_dict`
- `src/gobby/storage/memories_crud.py::*` — scope-reason: both INSERT column lists gain the pair
- `src/gobby/storage/memories_vector_reindex.py`
- `src/gobby/storage/memories.py::*` — scope-reason: add the new mixin to `LocalMemoryManager`
- `src/gobby/storage/memories_query.py::*` — scope-reason: add `update_surfaced_stats` beside `update_access_stats`
- `src/gobby/memory/protocol.py::*` — scope-reason: the five protocol shapes that carry `access_count`
- `src/gobby/memory/backends/storage_adapter.py::*` — scope-reason: adapter field mapping
- `src/gobby/memory/services/repository.py::*` — scope-reason: repository field mapping
- `src/gobby/memory/facade.py::*` — scope-reason: add `record_memory_access`; delete `_update_access_stats`
- `src/gobby/memory/dream/models.py::*` — scope-reason: candidate and curator payload fields
- `src/gobby/memory/dream/candidates.py::*` — scope-reason: candidate serialization
- `src/gobby/memory/dream/storage_journal.py::*` — scope-reason: `_MEMORY_COLUMNS` snapshot/restore lists
- `src/gobby/cli/memory/crud.py::*` — scope-reason: `gobby memory show` prints a `Surfaced:` line
- `web/src/hooks/useMemory.ts::*` — scope-reason: `Memory` type and fixture shape
- `web/src/components/activity/memory/MemoryDetailPanel.tsx::*` — scope-reason: `Surfaced` row beside `Accesses`
- `web/src/hooks/__tests__/useMemory.test.ts::*` — scope-reason: fixtures gain the pair
- `web/src/components/activity/memory/__tests__/MemoryDetailPanel.test.tsx::*` — scope-reason: the `Surfaced` row case
- `tests/storage/test_storage_memories.py::*` — scope-reason: column enumerations and the new writer's test
- `tests/storage/test_datetime_models.py::*` — scope-reason: `last_surfaced_at` normalization
- `tests/memory/test_dream.py::*` — scope-reason: fix the 16-column fake-DB INSERT; payload fields
- `tests/memory/test_maintenance_cleanup.py::*` — scope-reason: fixture rows
- `tests/memory/test_async_db_offload.py::*` — scope-reason: `_update_access_stats` case retargets to `record_memory_access`
- `tests/memory/test_memory_manager_1.py::*` — scope-reason: `TestAccessStats` cases retarget to the new writers
- `tests/servers/routes/test_memory_routes.py::*` — scope-reason: response fixtures
- `tests/cli/test_memory_cli.py::*` — scope-reason: the `Surfaced:` line
- `tests/mcp_proxy/tools/test_memory_get_access.py`

**Research context:** Mirror every `access_count` / `last_accessed_at` site. Storage:
`memories_crud.py` INSERT column lists (near lines 273-278 and 379-383, literal 0);
`memories_models.py` normalizer `optional` (near 89-98), fields (near 114-115), `from_row`
(near 175-176; use `row.get("surfaced_count", 0)` / `row.get("last_surfaced_at")`
following the pattern near 178-188 so fake-DB rows without the column still load),
`to_dict` (near 205-206). Protocol and adapters: `memory/protocol.py` (near 162-163,
190-191, 219-222, 245-247, 274-275), `backends/storage_adapter.py` (near 202, 221-222),
`services/repository.py` (near 65-66). Dream: `dream/models.py` (near 89, 104, 107, 130,
133), `dream/candidates.py` (near 94, 97), `dream/storage_journal.py` `_MEMORY_COLUMNS`
(near 12-41; the snapshot/restore round-trip drops the pair otherwise). The single access
writer is `MemoryQueryMixin.update_access_stats` in `storage/memories_query.py` (near
102-124, `UPDATE memories SET access_count = access_count + 1, last_accessed_at = %s`);
add a sibling `update_surfaced_stats` writing `surfaced_count` / `last_surfaced_at`. The
facade's `_update_access_stats` (`memory/facade.py`, near 218-219) has only test callers
(`tests/memory/test_async_db_offload.py`, `tests/memory/test_memory_manager_1.py`
`TestAccessStats`, `tests/storage/test_storage_memories.py::test_update_access_stats`);
replace it with `record_memory_access(memory_id)` that calls the storage writer, which 3.2
consumes. HTTP returns `to_dict()` (automatic). CLI show is `cli/memory/crud.py` (near
187). Web: `useMemory.ts` (near 46-47, 83-84), `MemoryDetailPanel.tsx` (near 228-229).
Found work: `tests/memory/test_dream.py` (near 1720-1738) hardcodes a 16-column INSERT
with `strict=True` while `_MEMORY_COLUMNS` is 19 wide; fix or delete that fake-DB branch.
Decomposition: `src/gobby/storage/memories_crud.py` is 944 lines. Move the five
vector-reindex methods (`list_vector_reindex_ids`, `mark_vector_reindex_needed`,
`mark_vectors_reindexed`, `mark_vector_snapshot_reindexed`,
`reconcile_vector_snapshot_page`, about 117 lines) out of `memories_crud.py` into a new
`MemoryVectorReindexMixin(MemoryStoreBase)` in `src/gobby/storage/memories_vector_reindex.py`,
and add the mixin to the `LocalMemoryManager` bases in `src/gobby/storage/memories.py`
(precedent: `MemoryCrossRefMixin`, `MemoryDreamMixin`). The `tests/mcp_proxy/tools/test_memory_get_access.py`
module is created here empty of behavior only if needed for the writer test; otherwise 3.2
creates it.

Verification: `GOBBY_TEST_GDAEMON=checkout $PG tests/storage/test_storage_memories.py tests/storage/test_datetime_models.py tests/memory/test_dream.py tests/memory/test_maintenance_cleanup.py tests/memory/test_async_db_offload.py tests/memory/test_memory_manager_1.py tests/servers/routes/test_memory_routes.py tests/cli/test_memory_cli.py tests/mcp_proxy/tools -q`;
`cd web && npx vitest run src/hooks/__tests__/useMemory.test.ts src/components/activity/memory/__tests__/`.

**Granularity:** more than six targets, one outcome: a column pair is threaded through
every carrier of the existing pair; splitting by layer would leave round-trips
(dream journal, web fixtures) broken between leaves.

**Acceptance:**

- 2.2.1 - `Memory` carries `surfaced_count` and `last_surfaced_at` through `from_row`, `to_dict`, both INSERTs, the protocol shapes, the adapter, and the repository. symbol: `Memory`. test: `tests/storage/test_storage_memories.py::test_memory_round_trips_surfaced_stats`.
- 2.2.2 - `update_surfaced_stats` increments `surfaced_count` and sets `last_surfaced_at`, leaving `access_count` untouched. symbol: `MemoryQueryMixin.update_surfaced_stats`. test: `tests/storage/test_storage_memories.py::test_update_surfaced_stats`.
- 2.2.3 - `record_memory_access` on the facade increments `access_count` and sets `last_accessed_at`, and `_update_access_stats` no longer exists. symbol: `MemoryManagerFacadeMethods.record_memory_access`. test: `tests/memory/test_memory_manager_1.py::test_record_memory_access`.
- 2.2.4 - Dream candidates, curator payloads, and the journal round-trip carry both counters, and the fake-DB INSERT width matches `_MEMORY_COLUMNS`. file: `src/gobby/memory/dream/storage_journal.py`. test: `tests/memory/test_dream.py::test_journal_round_trip_keeps_surfaced_stats`.
- 2.2.5 - `gobby memory show` prints `Surfaced:` and the web detail panel renders a `Surfaced` row. test: `tests/cli/test_memory_cli.py::test_show_prints_surfaced_line`. file: `web/src/components/activity/memory/MemoryDetailPanel.tsx`.
- 2.2.6 - The vector-reindex methods live in `memories_vector_reindex.py`, `memories_crud.py` is under 850 lines, and `LocalMemoryManager` still exposes them. file: `src/gobby/storage/memories_vector_reindex.py`. symbol: `LocalMemoryManager`.

## P3: Access and surfaced semantics
`kind: framing`

With the columns in place: the search path counts surfacing, `get_memory` counts access
and records what the session read, the index dedupes on reads with a re-show horizon for
glances, and post-task review starts from the reads.

### 3.1 Increment split, caller gating, ranking anchor, and dream prompt [category: code] (depends: 2.2)
`kind: deliverable`
[domain: backend]

Targets:
- `src/gobby/memory/services/_search_access.py::*` — scope-reason: becomes the surfaced helper: reads `last_surfaced_at`, calls `update_surfaced_stats`, keeps the debounce knob
- `src/gobby/memory/services/search.py::*` — scope-reason: gate the increment on `SURFACED_CALLERS`
- `src/gobby/memory/services/_search_results.py::*` — scope-reason: decay anchors on `recency_anchor`
- `src/gobby/memory/scoring.py::*` — scope-reason: add `recency_anchor` beside `temporal_decay`
- `src/gobby/memory/services/maintenance.py::*` — scope-reason: the "Accessed" label wording
- `src/gobby/install/shared/prompts/memory/dream.md`
- `src/gobby/install/shared/workflows/agents/memory-curator.yaml::*` — scope-reason: cluster-member payload gains both counters and loses the label fields
- `tests/memory/test_scoring.py::*` — scope-reason: `recency_anchor` cases
- `tests/memory/test_search_access_gating.py`
- `tests/memory/test_maintenance_update.py::*` — scope-reason: label wording if asserted
- `docs/guides/memory.md`
- `src/gobby/install/shared/skills/gobby/references/memory/search.md`
- `src/gobby/install/shared/skills/gobby/references/memory/maintenance.md`

**Research context:** The debounced helper in `services/_search_access.py` (near lines
17-53) reads `memory.last_accessed_at` and calls `update_access_stats`; it becomes the
surfaced helper (read `last_surfaced_at`, call `update_surfaced_stats` from 2.2) and keeps
`access_debounce_seconds` as its knob (reword the description in a later config pass, not
here, to avoid a second contract regeneration). The search-path call in
`services/search.py` (near line 222) fires on every branch; gate it with
`SURFACED_CALLERS = {"memory.surface", "mcp_proxy.memory.search_memories",
"mcp_proxy.memory.review_task_memories", "http.memory.search", "cli.memory.recall"}`.
Excluded probes: `mcp_proxy.memory.create_memory.similar_existing`
(`mcp_proxy/tools/memory_write.py`), `review_learning.related_lessons`
(`src/gobby/review_learning/service.py`), and the `memory.search` default, which is
declared in five places that stay in sync (`facade.py`, `search.py`, `_search_paths.py`
twice, `_search_models.py`); none of them changes. Ranking: `_search_results.py` (near
78-92) calls `temporal_decay(mem.updated_at, half_life)`; replace the anchor with
`recency_anchor(mem.updated_at, mem.last_accessed_at)` (later of the two, None-safe), added
next to `temporal_decay` in `memory/scoring.py`. Edge decay in `knowledge_graph/reader.py`
is a different half-life and unchanged. Dream is the load-bearing consumer of Decision 11:
`prompts/memory/dream.md` reasons on `access_count` as "recall frequency measures
retrieval" in three sentences (near lines 36, 68, 100) and
`workflows/agents/memory-curator.yaml` ships `access_count` per cluster member (near line
44); after the split most rows have `access_count == 0`, so rewrite per Decision 13 and
ship `surfaced_count` / `last_surfaced_at` beside it; drop `useful_labels` /
`total_labels`, which were sourced from the dropped `recall_usefulness` table.
`services/maintenance.py` keeps the `(access_count, updated_at)` keep-decision (near
276-283); its "Accessed" label (near 172-173) becomes accurate. Found work:
`references/memory/maintenance.md` (near 35-37) says dedupe keeps the earliest row,
contradicting the code; reconcile the doc. Docs: `docs/guides/memory.md` ordering prose
(near 445-470) and `references/memory/search.md` (near 18-23) say decay follows the last
update. Rejected: a separate `surfaced` debounce key (one knob, one meaning).

Verification: `GOBBY_TEST_GDAEMON=checkout $PG tests/memory/test_scoring.py tests/memory/test_search_access_gating.py tests/memory/test_search_ranking.py tests/memory/test_dream.py tests/memory/test_maintenance_cleanup.py tests/memory/test_maintenance_update.py tests/agents/test_agents_sync.py tests/servers/routes/test_memory_routes.py tests/cli/test_memory_cli.py -q`

**Granularity:** more than six targets, one outcome: the increment repoint, the caller
gate, and the anchor are the three halves of "access means fetch" and cannot land
separately without a window where neither counter means anything.

**Acceptance:**

- 3.1.1 - A search from a caller in `SURFACED_CALLERS` increments `surfaced_count` and sets `last_surfaced_at` on returned hits, debounced by `access_debounce_seconds`, and never touches `access_count`. symbol: `SURFACED_CALLERS`. test: `tests/memory/test_search_access_gating.py::test_surfaced_callers_increment_surfaced_stats`.
- 3.1.2 - A probe caller (`memory.search` default, `create_memory.similar_existing`, `review_learning.related_lessons`) increments nothing. test: `tests/memory/test_search_access_gating.py::test_probe_callers_do_not_increment`.
- 3.1.3 - `recency_anchor` returns the later of `updated_at` and `last_accessed_at` and tolerates None, and a fetched older memory outranks an unfetched newer one at equal similarity. symbol: `recency_anchor`. test: `tests/memory/test_scoring.py::test_recency_anchor_prefers_later_access`. test: `tests/memory/test_search_access_gating.py::test_fetched_older_memory_outranks_unfetched_newer`.
- 3.1.4 - The dream prompt and curator payload carry both counters with the Decision 13 wording and no `useful_labels` / `total_labels`. file: `src/gobby/install/shared/prompts/memory/dream.md`. file: `src/gobby/install/shared/workflows/agents/memory-curator.yaml`.
- 3.1.5 - The guide, the search reference, and the maintenance reference describe the anchor and the dedupe keep-rule as the code implements them. file: `docs/guides/memory.md`. file: `src/gobby/install/shared/skills/gobby/references/memory/maintenance.md`.

### 3.2 get_memory records access and the fetching task [category: code] (depends: 2.2)
`kind: deliverable`
[domain: backend]

Targets:
- `src/gobby/mcp_proxy/tools/memory_session.py`
- `src/gobby/mcp_proxy/tools/memory.py::*` — scope-reason: `get_memory` becomes `async def get_memory(memory_id, session_id)`, records access, and upserts `accessed_memory_ids`
- `src/gobby/mcp_proxy/tools/memory_review.py::*` — scope-reason: use the shared `resolve_session`
- `src/gobby/mcp_proxy/tools/memory_surface.py::*` — scope-reason: use the shared `resolve_session`
- `src/gobby/mcp_proxy/tools/memory_write.py::*` — scope-reason: use the shared `resolve_claimed_task_id`
- `tests/mcp_proxy/tools/test_memory_get_access.py`
- `tests/mcp_proxy/tools/test_memory_tools.py::*` — scope-reason: the existing `get_memory` cases pass `session_id` and await
- `tests/mcp_proxy/tools/test_memory_review.py::*` — scope-reason: session resolution goes through the shared helper
- `tests/mcp_proxy/tools/test_memory_surface.py::*` — scope-reason: session resolution goes through the shared helper
- `tests/mcp_proxy/tools/test_memory_write_cap.py::*` — scope-reason: claimed-task resolution goes through the shared helper if patched by name

**Research context:** `get_memory` in `src/gobby/mcp_proxy/tools/memory.py` (lines
423-457) is a sync `def get_memory(memory_id: str)` with no session identity; it resolves
the id, loads through the facade, and returns a dict that already includes
`access_count`. Add a required `session_id: str` parameter: the proxy fills it
(`_inject_required_session_id_argument` in `src/gobby/mcp_proxy/services/tool_execution.py`)
and rule-dispatched calls get it from `src/gobby/hooks/dispatchers/mcp.py`; both are
read-only here. Session resolution is duplicated as `_resolve_session` in
`memory_review.py` (near 74-87) and `memory_surface.py` (near 42-55); the claimed-task
query lives in `memory_write.py` (near 124-131:
`LocalTaskManager(db).list_tasks(claimed_by_session_id=..., closed=False,
sort_by="updated_at", sort_order="desc")`, first id or None). Extract both into the new
`src/gobby/mcp_proxy/tools/memory_session.py` as `resolve_session(session_manager,
session_id)` and `resolve_claimed_task_id(db, session_id)`, and use them from all four
tools. Make `get_memory` `async def`, resolve and load as today, then `asyncio.to_thread`
the two writes: `facade.record_memory_access(memory_id)` (2.2), which needs no session and
runs on every successful load (Decision 1), and, only when `session_id` resolves,
`SessionVariableManager.upsert_bounded_list_variable(session_id, "accessed_memory_ids",
{"memory_id": ..., "task_id": ...}, identity={"memory_id": ..., "task_id": ...},
max_items=_ACCESSED_MEMORY_IDS_MAX)` (`src/gobby/workflows/state_manager.py`, near 340-373),
with `_ACCESSED_MEMORY_IDS_MAX = 1000` beside the tool. Identity is the
pair: the helper drops every stored item whose identity keys all match, so identity on
`memory_id` alone would replace task A's record when the same memory is fetched under task
B and Decisions 8-9 would lose A's provenance. With the pair a memory keeps one record per
fetching task (or one untagged record; `None` compares equal, so no special case), and a
repeat fetch under the same task refreshes that record. The bound exists because the helper
requires `max_items` and the variables blob is read on every surfacing (3.3); at the cap the
oldest record goes first. It sits far above what one epoch holds: every `get_memory` result
carries the full memory row into context, the context-pressure handoff fires long before a
thousand of them, and Decision 7 resets the set at compaction, so the compaction reset, not
the cap, is what bounds Decision 9's inputs; a memory whose record is gone is still reachable
through 3.4's search tier. Rejected: keeping every record whose task is still open (a task
query per fetch, an unbounded list for a long-open task, and the same compaction reset).
The write is direct, not staged: a
tool result the agent requested has reached it by definition. The result dict gains
`surfaced_count`; `access_count` stays. Rules cannot append to a set (`set_variable` only),
so the tracking write stays in Python. Rejected: recording access on the search path with
a flag (the whole point is that the two paths mean different things).

Verification: `GOBBY_TEST_GDAEMON=checkout $PG tests/mcp_proxy/tools/test_memory_tools.py tests/mcp_proxy/tools/test_memory_get_access.py tests/mcp_proxy/tools/test_memory_review.py tests/mcp_proxy/tools/test_memory_surface.py tests/mcp_proxy/tools/test_memory_write_cap.py tests/mcp_proxy/services -q`

**Acceptance:**

- 3.2.1 - `get_memory` requires `session_id`, is awaitable, and each call increments `access_count`, sets `last_accessed_at`, and returns both counters. test: `tests/mcp_proxy/tools/test_memory_get_access.py::test_get_memory_records_access`.
- 3.2.2 - The fetch appends `{memory_id, task_id}` to `accessed_memory_ids`, tagged with the task the session has claimed or `None`, bounded at `_ACCESSED_MEMORY_IDS_MAX` (1000) records with identity on the `(memory_id, task_id)` pair, so the same memory fetched under two tasks keeps both records. test: `tests/mcp_proxy/tools/test_memory_get_access.py::test_get_memory_records_accessed_id_with_claimed_task`. test: `tests/mcp_proxy/tools/test_memory_get_access.py::test_get_memory_untagged_without_claimed_task`. test: `tests/mcp_proxy/tools/test_memory_get_access.py::test_get_memory_keeps_a_record_per_task`.
- 3.2.3 - `resolve_session` and `resolve_claimed_task_id` are the only session and claimed-task resolvers in the memory tools; the review, surface, and write tools import them. file: `src/gobby/mcp_proxy/tools/memory_session.py`. symbol: `resolve_claimed_task_id`.
- 3.2.4 - A `get_memory` call whose session cannot be resolved still returns the memory and increments `access_count` (Decision 1), and writes no `accessed_memory_ids` record. test: `tests/mcp_proxy/tools/test_memory_get_access.py::test_get_memory_unresolved_session_records_access_without_tracking`.
- 3.2.5 - At the cap a fetch evicts the oldest record only, and the cap is the tool's `_ACCESSED_MEMORY_IDS_MAX` constant. symbol: `_ACCESSED_MEMORY_IDS_MAX`. test: `tests/mcp_proxy/tools/test_memory_get_access.py::test_accessed_memory_ids_evict_oldest_at_cap`.

### 3.3 Surfaced set, injection sequence, K delivery, reset rules, and the dead dedupe chain [category: code] (depends: 3.1, 3.2)
`kind: deliverable`
[domain: backend]

Targets:
- `src/gobby/workflows/engine/injection_tracking.py::*` — scope-reason: rename to `surfaced_memory_ids`, `"<id>@<seq>"` stamps, max-seq reader, `accessed_memory_ids` exclusion, K horizon
- `src/gobby/workflows/engine/delivery_formatting.py::*` — scope-reason: pass `reshow_after_injections` from the payload into the filter
- `src/gobby/hooks/receipt_effects.py::*` — scope-reason: add `staged_session_variable(name)` beside the staged set-value reader
- `src/gobby/mcp_proxy/tools/memory_surface.py::*` — scope-reason: take the config accessor and emit `reshow_after_injections`
- `src/gobby/mcp_proxy/tools/memory.py::*` — scope-reason: pass `_config` into `register_memory_surface_tools`
- `src/gobby/install/shared/workflows/rules/memory-lifecycle/reset-memory-tracking-on-start.yaml::*` — scope-reason: reset effects cover the four tracking variables
- `src/gobby/install/shared/workflows/rules/context-handoff/preserve-context-on-compact.yaml::*` — scope-reason: reset effects cover the four tracking variables
- `src/gobby/hooks/hook_manager.py::*` — scope-reason: delete `_dedup_memory_results`; move the ingress helpers out
- `src/gobby/hooks/hook_manager_ingress.py`
- `src/gobby/hooks/rule_evaluator.py::*` — scope-reason: delete `dedup_memory_results`, its `process_dispatch_results` branch, and `_committed_set_values` if unused
- `tests/workflows/test_memory_index_delivery.py::*` — scope-reason: dedupe and staging cases for the two sets and the horizon
- `tests/hooks/test_receipt_effects.py::*` — scope-reason: the staged sequence variable
- `tests/workflows/test_memory_lifecycle_rules.py::*` — scope-reason: reset-rule shape covers the four variables
- `tests/workflows/test_context_handoff_rules.py::*` — scope-reason: compact rule covers the four variables
- `tests/hooks/test_hook_manager_extra.py::*` — scope-reason: delete `TestDedupMemoryResults`
- `tests/hooks/test_grok_pending_context.py::*` — scope-reason: variable rename
- `tests/mcp_proxy/tools/test_memory_surface.py::*` — scope-reason: payload carries `reshow_after_injections`
- `tests/docs/test_memory_guides.py::*` — scope-reason: rule-row parity if the assertion names effects
- `docs/guides/memory.md`

**Research context:** Both lifecycle rules already clear `injected_memory_ids`:
`reset-memory-tracking-on-start.yaml` on clear, compact, and reset-resume, and
`preserve-context-on-compact.yaml` on `pre_compact` (both `variable: injected_memory_ids`,
near line 12). Decision 7 therefore means: replace that effect in both with
`surfaced_memory_ids: []`, `accessed_memory_ids: []`, `_memory_surface_seq: 0`, and
(found work) `injected_review_lesson_ids: []`, which is never reset today although
`docs/guides/memory.md` (near 635-639) says it is. A turn counter exists
(`parent_turn_seq`) but never increments for spawned agents, so the injection-sequence
horizon is the one that works for every session kind. Rules cannot append to a set
(`src/gobby/workflows/definitions.py`, `set_variable` only), so the tracking writes stay
in Python. Keep the staged set-variable protocol (`receipt_effects.py`,
`stage_append_set_variables` / `staged_append_set_values` / `apply_acknowledged_receipt`
replaying through `append_to_set_variable`): a payload that never reached the agent must
not suppress re-injection. Since #22708 (`d92c3afd40`, `ba2373562a`, `d9bd4195fd`) and
#22807 (`6cdbdd3213`) the surfacing call runs through `CachedMcpInjectionMixin`
(`src/gobby/workflows/engine/mcp_injections.py`): `surface_memories` is in
`_CACHED_INJECTION_TOOLS` (30-37), the inline wait is capped at `_INLINE_WAIT_CAP_SECONDS`
(10 s, the rules' `timeout_seconds`), a timed-out call finishes in the background and is
delivered on the session's next hook by `_deliver_late_mcp_injections` (267-301), and a
repeat of the same call within `_CACHE_TTL_SECONDS` (120 s) is served from the per-session
cache. Every delivery, inline (`_apply_cached_mcp_injection`, 131-223), cached and late,
goes through `_append_injected_mcp_result` (391-434) into `_format_memory_backed_result`,
`_format_memory_index_result`, and `_filter_and_track_new_memories`, so the stamp and
sequence logic lives in the filter and covers all three paths with one implementation (a
deferred path must apply every side effect of the primary path through the same helper).
A late or cached delivery is a surfacing: it advances the sequence like an inline one. Encode the stamp in the set value as `"<memory_id>@<seq>"`; the
reader keeps the max seq per id; growth stays epoch-bounded as today. The sequence
`_memory_surface_seq`: read the committed value plus staged, add one per surfacing that
reaches the formatter (whether or not any line renders), stage the new value under
`session_variables` in the same receipt payload. It is distinct from
`_memory_surface_turn_seq`, the once-per-parent-turn guard that
`surface-memories-on-turn-start.yaml` sets `on_receipt`, which stays. Filter in
`InjectionTrackingMixin._filter_and_track_new_memories`
(`src/gobby/workflows/engine/injection_tracking.py`, near 14-61): drop if any record in
`accessed_memory_ids` carries the id (a memory holds one record per fetching task, 3.2);
drop if it has a stamp with `seq_now - seq < K`; otherwise render
and stage `"<id>@<seq_now>"` (with K=5 a line stamped at seq 1 is dropped at seq 2-5 and
rendered at seq 6, the fifth further surfacing). K reaches the formatter through the tool payload: the
workflow engine has no daemon-config access, while the memory tool registry has
`_config()` (`mcp_proxy/tools/memory.py`, near line 137), so `register_memory_surface_tools`
(`memory_surface.py`, near 70-144) takes the accessor, `surface_memories` reads
`_config().memory.index_reshow_after_injections` and returns it as
`reshow_after_injections`, and `DeliveryFormattingMixin._format_memory_index_result`
(`delivery_formatting.py`, near 58-74) passes `result.get("reshow_after_injections", 5)`
into the filter. Out of scope, noted: `surface_memories` returns its top 5 before the
formatter filters, so a rendered index can be shorter than 5 (same as today); for
`memory.surface` the surfaced increment (3.1) therefore counts the returned top 5, debounced
per memory by `access_debounce_seconds` (default 60 s), including hits the formatter then
suppresses: it measures ranked delivery to the index, the mechanism Decision 2 names. Dead path,
delete: the deferred `search_memories` dedupe chain is unreachable since no bundled rule
dispatches `search_memories`: `HookManager._dedup_memory_results` (`hook_manager.py`,
839-841), `WorkflowRuleEvaluator.dedup_memory_results` (`rule_evaluator.py`, near
310-339) with its `process_dispatch_results` branch (near 23-77) and
`_committed_set_values` (near 374-378) if unused afterwards, and `TestDedupMemoryResults`
in `tests/hooks/test_hook_manager_extra.py` (near 248-452). Decomposition:
`src/gobby/hooks/hook_manager.py` is 947 lines. Move the session-ingress helpers
(`_record_machine_ingress`, `_recheck_pending_transcript`,
`_discard_pending_transcript_recheck`, `_record_session_activity_pulse`,
`_resolve_session_refs_in_tool_input`, `_try_resolve_session_field`; lines 240-283 and 706-772, about 110 lines) out
of `hook_manager.py` into a new `HookManagerIngressMixin` in
`src/gobby/hooks/hook_manager_ingress.py`, added to the `HookManager` bases beside
`HookManagerDispatchMixin` (`src/gobby/hooks/hook_manager_dispatch.py`, the precedent).
Docs: `docs/guides/memory.md` rule rows (near 590, parity-checked by
`tests/docs/test_memory_guides.py`) and the tracking-variable prose (near 635-639).
Rejected: a server-side exclusion list in `surface_memories` (later improvement); a
turn-based horizon (spawned agents never advance it).

Verification: `GOBBY_TEST_GDAEMON=checkout $PG tests/workflows/test_memory_index_delivery.py tests/workflows/test_memory_lifecycle_rules.py tests/workflows/test_context_handoff_rules.py tests/hooks/test_receipt_effects.py tests/hooks/test_hook_manager_extra.py tests/hooks/test_grok_pending_context.py tests/hooks tests/mcp_proxy/tools/test_memory_surface.py tests/docs/test_memory_guides.py -q`

**Granularity:** more than six targets, one outcome: the two sets, the sequence, the K
delivery, and the reset rules are one dedupe state machine; the dead chain and the
ingress move are the removals that make the owning files fit.

**Acceptance:**

- 3.3.1 - An id with any record in `accessed_memory_ids`, whichever task tagged it, is never rendered again in the epoch. test: `tests/workflows/test_memory_index_delivery.py::test_accessed_memory_never_reshown`.
- 3.3.2 - A shown-but-unread id is suppressed while `seq_now - seq < K` and rendered again on the K-th further surfacing (`seq_now - seq == K`), with the stamp refreshed. test: `tests/workflows/test_memory_index_delivery.py::test_surfaced_memory_reshown_after_horizon`.
- 3.3.3 - Stamps and the sequence are staged in the receipt and committed only on acknowledgement. test: `tests/hooks/test_receipt_effects.py::test_surface_seq_and_stamps_commit_on_ack`.
- 3.3.4 - `surface_memories` returns `reshow_after_injections` from `memory.index_reshow_after_injections` and the formatter uses it. test: `tests/mcp_proxy/tools/test_memory_surface.py::test_payload_carries_reshow_after_injections`. symbol: `DeliveryFormattingMixin._format_memory_index_result`.
- 3.3.5 - Both lifecycle rules reset `surfaced_memory_ids`, `accessed_memory_ids`, `_memory_surface_seq`, and `injected_review_lesson_ids`, and no rule or code path names `injected_memory_ids`. test: `tests/workflows/test_memory_lifecycle_rules.py::test_reset_rule_clears_memory_tracking_variables`. test: `tests/workflows/test_context_handoff_rules.py::test_compact_rule_clears_memory_tracking_variables`.
- 3.3.6 - `_dedup_memory_results`, `dedup_memory_results`, and `TestDedupMemoryResults` no longer exist. file: `src/gobby/hooks/rule_evaluator.py`. file: `tests/hooks/test_hook_manager_extra.py`.
- 3.3.7 - The ingress helpers live in `hook_manager_ingress.py`, `hook_manager.py` is under 850 lines, and the hook suite passes. file: `src/gobby/hooks/hook_manager_ingress.py`. behavior: `$PG tests/hooks -q` passes.

### 3.4 Post-task review lists accessed memories first [category: code] (depends: 3.3)
`kind: deliverable`
[domain: backend]

Targets:
- `src/gobby/mcp_proxy/tools/memory_review.py::*` — scope-reason: accessed tier before the search tier; `source` on every candidate
- `tests/mcp_proxy/tools/test_memory_review.py::*` — scope-reason: ordering, tagging, closing-session union, and dedupe cases
- `docs/guides/memory.md`
- `src/gobby/install/shared/skills/gobby/references/memory/post-task.md`

**Research context:** `review_task_memories(task_id, changes_summary, session_id)` in
`src/gobby/mcp_proxy/tools/memory_review.py` (near 129-246) resolves the task and session,
searches `title + summary` with `_CANDIDATE_LIMIT`, serializes candidates (near 216-226),
records the review (`_record_review`, near 48-67), and returns the shape near 239-246.
After the task and session resolve: read `accessed_memory_ids` from the calling session
and, when different, from `task.closed_in_session_id`; keep records whose `task_id` equals
the task or is `None`, one candidate per memory id at its first record's position (a memory
with a tagged and an untagged record lists once); load each with the facade and serialize
with `"source": "accessed"`. Then the existing search, minus ids already listed, with `"source": "search"`.
`candidate_ids` in the review record covers both tiers. There is no transitive descendant
lister (`_lineage_discovery.py` is one level); the calling session plus the closing
session covers the spawned-worker case without one. The `recall_request_id` minting was
removed in 1.2. Docs: `docs/guides/memory.md` post-task prose (near 277-290) and
`references/memory/post-task.md` (near 3-14). Rejected: a lineage walk (no lister; two
sessions cover the observed case).

Verification: `GOBBY_TEST_GDAEMON=checkout $PG tests/mcp_proxy/tools/test_memory_review.py tests/workflows/test_memory_lifecycle_rules.py tests/docs/test_memory_guides.py -q`

**Acceptance:**

- 3.4.1 - Accessed candidates tagged with the closing task or untagged come first with `source: accessed`, in fetch order. test: `tests/mcp_proxy/tools/test_memory_review.py::test_accessed_candidates_listed_first`.
- 3.4.2 - Accessed records tagged with another task are excluded, and a memory fetched under two tasks is found by each task's review. test: `tests/mcp_proxy/tools/test_memory_review.py::test_other_task_accessed_records_excluded`. test: `tests/mcp_proxy/tools/test_memory_review.py::test_memory_fetched_under_two_tasks_found_by_each_review`.
- 3.4.3 - Records from `task.closed_in_session_id` join those of the calling session when the two differ. test: `tests/mcp_proxy/tools/test_memory_review.py::test_closing_session_accessed_records_included`.
- 3.4.4 - Search candidates already listed as accessed are not repeated, and `candidate_ids` in the review record spans both tiers. test: `tests/mcp_proxy/tools/test_memory_review.py::test_search_tier_deduped_against_accessed`.
- 3.4.5 - The guide and the post-task reference describe the two tiers. file: `docs/guides/memory.md`. file: `src/gobby/install/shared/skills/gobby/references/memory/post-task.md`.

## P4: Destructive-directive retirement
`kind: framing`

Decision 14. Independent of P3; needs only the migration leaf so that the last
directive-free migration is already in the lineage.

### 4.1 Retire the destructive-migration directive path [category: code] (depends: 2.1)
`kind: deliverable`
[domain: backend]

Targets:
- `crates/gcore/src/schema/runner.rs::*` — scope-reason: delete `stamps_destructive_migrations`, the destructive branch, `stamp_receipt_only`, and the `destructive_authorized` thread
- `crates/gcore/src/schema/runner_plan.rs::*` — scope-reason: delete the destructive authorization check; keep the non-transactional check
- `crates/gcore/src/schema/runner_tests.rs::*` — scope-reason: delete the directive cases
- `crates/gdaemon/src/main.rs::*` — scope-reason: delete `--destructive`, the epoch and backup gating in `apply_schema`, `hold_open_maintenance_epoch_lease`, and `load_newest_backup_manifest[_from_root]`
- `crates/gdaemon/src/main/tests.rs::*` — scope-reason: delete the gating cases
- `src/gobby/cli/schema.py::*` — scope-reason: delete `validate_destructive_manifest`, the destructive branch of `apply_schema`, `_apply_verified_batch`, `_newest_manifest_path`, `_file_sha256`, `_SchemaApplyExecutor`, and the `schema-apply` campaign registration
- `src/gobby/storage/hub/runtime.py::*` — scope-reason: delete `apply_destructive_batch` if the schema CLI was its only caller
- `tests/cli/test_cli_schema.py::*` — scope-reason: trim to the plain apply
- `docs/guides/hub-install-contract.md`

**Research context:** The directive was built for hub backup and restore to a new
machine, never for migrations; only `426_retire_legacy_wiki.sql` carries it, and 421, 427,
439, and now 452 drop tables plainly. Rust: `crates/gcore/src/schema/runner.rs` computes
`stamps_destructive_migrations = is_fresh_lineage` in `BaselineState` (near 151-169) and
branches on the directive to `stamp_receipt_only` (near 643-660);
`runner_plan.rs` checks authorization (near 167-176; keep the non-transactional check
beside it). `crates/gdaemon/src/main.rs` declares `--destructive` (near 43, 70, 72),
gates `apply_schema` on a maintenance epoch and a backup manifest (near 109-145),
`hold_open_maintenance_epoch_lease` (near 214, sole caller near 146), and
`load_newest_backup_manifest` / `_from_root` (near 258-263); their tests sit in
`crates/gdaemon/src/main/tests.rs` (near 36-88) and `runner_tests.rs` (near 30-54, 1134,
1720-1885). Python: `src/gobby/cli/schema.py` has `validate_destructive_manifest` (near
54-87), the destructive branch of `apply_schema` (near 109-151), `_apply_verified_batch`,
`_newest_manifest_path`, `_file_sha256`, `_SchemaApplyExecutor`, and
`register_campaign_executor("schema-apply", ...)` (near 243);
`src/gobby/storage/hub/runtime.py` `apply_destructive_batch` (near 58) goes if no other
caller remains (sweep with `gcode usages` before deleting). Keep
`src/gobby/cli/hub_maintenance.py`, `src/gobby/storage/maintenance_epoch.py`,
`DestructiveBatch`, and hub backup and restore: the maintenance-epoch framework is the
backup/restore tool. `cli/schema.py` imports from `cli/hub_backup/_integrity`, `_manifest`,
`_stores`, and `hub_maintenance`, and nothing in those modules imports `schema.py`, so the
deletions cannot break restore at import time; the two `tests/cli/hub_backup/` modules in the
verification guard its behavior (`tests/cli/test_hub_maintenance.py` has no restore case).
426 keeps its bytes (receipts are checksummed); once the directive
means nothing, its `IF EXISTS` drops execute harmlessly on fresh lineages. Docs:
`docs/guides/hub-install-contract.md` (near line 51) describes the directive ceremony.
Load the `rust` skill first. Rejected: keeping the directive as a no-op comment marker
(a marker that means nothing invites the next misuse).

Verification: `cargo test --manifest-path crates/gcore/Cargo.toml schema && cargo test --manifest-path crates/gdaemon/Cargo.toml`;
`GOBBY_TEST_GDAEMON=checkout $PG tests/cli/test_cli_schema.py tests/storage/test_schema_contract.py tests/cli/test_hub_maintenance.py tests/cli/hub_backup/test_cli_hub_backup_cli.py tests/cli/hub_backup/test_verify.py -q`.

**Granularity:** nine targets, one outcome: the directive is one authorization path across
the runner, the daemon CLI, and the Python campaign; leaving any half makes the other
half unreachable code.

**Acceptance:**

- 4.1.1 - The runner executes a directive-marked migration like any other and never stamps a receipt without executing. file: `crates/gcore/src/schema/runner.rs`. behavior: `cargo test --manifest-path crates/gcore/Cargo.toml schema` passes with no `stamps_destructive_migrations` symbol.
- 4.1.2 - `gdaemon schema apply` has no `--destructive` flag and requires no maintenance epoch or backup manifest. file: `crates/gdaemon/src/main.rs`. behavior: `cargo test --manifest-path crates/gdaemon/Cargo.toml` passes.
- 4.1.3 - `gobby schema apply` has no destructive branch and no `schema-apply` campaign executor is registered. symbol: `apply_schema`. test: `tests/cli/test_cli_schema.py::test_apply_schema_plain`.
- 4.1.4 - Hub backup, restore, and the maintenance-epoch framework are untouched. behavior: the hub-maintenance CLI module, the `cli/hub_backup/` package, and `DestructiveBatch` are not in this leaf's diff and the three test modules pass unchanged. test: `tests/cli/hub_backup/test_cli_hub_backup_cli.py::TestRestore::test_restore_uses_explicit_target_and_verified_hub_artifact`. test: `tests/cli/hub_backup/test_verify.py::test_verify_postgres_restore_happy_path_drives_prod_image_without_ports_or_volumes`. test: `tests/cli/test_hub_maintenance.py::test_run_owns_open_backup_apply_verify_release_and_restart`.
- 4.1.5 - The install contract no longer describes a destructive-migration ceremony. file: `docs/guides/hub-install-contract.md`.

## P5: Verification
`kind: framing`

### 5.1 Live cutover and verification
`kind: verification`

After 1.1 through 4.1 land (Decision 15): announce with a `global` `send_message`,
confirm no live spawned worker or close validator, `cargo build --release` for the
workspace, `promote_workspace_binary_set` for `gcode`, `gdaemon`, `ghook`, promote
`gclient` separately, read `sha256` from `~/.gobby/bin/` (never from `target/release/`),
then `uv run gobby restart --wait` from the main checkout, which proves
`gdaemon schema plan` and applies 452. Then:

1. `~/.gobby/bin/gdaemon --version --json` reports `latest_version: 452`; the hub has
   `memories.surfaced_count` and no `recall_*` table.
2. `gobby-config:get_config_values` shows `memory.index_reshow_after_injections: 5` and
   none of the removed keys; a patch to a removed key is rejected.
3. In a fresh session: the turn-start index lists a memory; `get_memory` on it bumps
   `access_count` to 1 and sets `last_accessed_at`; no later index in the epoch re-lists
   it; an unfetched line from the same index (stamped at seq s) is absent from the next
   four turn-start indexes and re-appears on the fifth further surfacing (seq s + 5 with
   K=5, no other surfacing moment firing in between). `surfaced_count` climbs on index
   and `search_memories` returns only.
4. Claim a task, fetch two memories, close the task: `review_task_memories` returns those
   two first with `source: accessed`, then search candidates.
5. `gobby memory show <id>` prints both counters; the web memory detail panel shows
   `Surfaced` and `Accesses`.
6. A `pre_compact` event resets `surfaced_memory_ids`, `accessed_memory_ids`,
   `_memory_surface_seq`, and `injected_review_lesson_ids`.
7. Nightly dream (or `gobby memory dream --dry-run` if available) renders the curator
   payload with both counters and no `useful_labels`.

Per-leaf gates are the commands listed under each deliverable plus format, lint, mypy,
and the scoped test-types audit. Contract freshness:
`tests/config/test_runtime_config_contract.py` (1.3),
`crates/gcore/tests/catalog_manifest_freshness.rs` and the identity regeneration (2.1).
