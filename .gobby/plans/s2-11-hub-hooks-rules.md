Plan artifact: `.gobby/plans/s2-11-hub-hooks-rules.md`

# Gobby 1.0 Stage 2: S2.11 hub hook ingress and rule engine

**Plan ID:** s2-11-hub-hooks-rules

## Overview
`kind: framing`

#24151 is the hub half of S2.11. It moves hub hook handling from Python to
Rust: the rule engine, `POST /api/hooks/execute`, the five CLI adapters, the
hook event pipeline and its handlers, the node hook replay, receipt and
detection-manifest routes, and the webhook routes. The node half (#24150) already serves node-local ingress in
gdaemon.

Two statically linked family crates carry the work.

- `crates/gworkflows` (package `gobby-workflows`) holds the rule engine and
  command normalization. Ingress, #21567 Workflows, #24884's native MCP gateway
  and the Python MCP proxy forward (Decision 2) all consume it.
- `crates/gingress` (package `gobby-ingress`) holds the `hooks` route family:
  envelope handling, the shared envelope ledger, the adapters, the pipeline,
  the handlers, and the node and webhook routes.

The plan ends with native default after a compare soak, the parity-ledger rows,
and deletion of the Python route and the ingress-only modules. Python
`HookManager` stays for the consumers that do not enter through the route
(Decision 4).

Josh's rulings apply. Bundled YAML (rules, pipelines, agents, workflows) is
validated against the engine schema and loader only, with no unit or
functional test per individual rule (2026-10-27 18:04 CDT). Hub-node work
covers macOS and Linux; Windows is #25249. No latency budget gates anything.
0.5.0 keeps no backward compatibility.

## Decision Record
`kind: framing`

1. **Scope includes the rule engine (Q1, option B).** This plan absorbs DR1-DR8
   and P1/P2 of `.gobby/plans/rust-rule-evaluation.md` and closes its open
   findings F2 and F4; #22946 closes as superseded. The Orchestrator and this
   writer recommend B. Q1 awaits Josh through Triage. Under option A, P2
   collapses to one deliverable that consumes `gobby-workflows` (depends:
   #21567), and #21567 keeps its edge on #24151. Under B, #21567 consumes this
   plan's P2 instead; the Orchestrator reverses that edge at expansion.
2. **Seams first (Q2, Orchestrator ruling).** #21562 Sessions and #21566 MCP
   stay hard prerequisites (Josh, decision B, 2026-10-12). The engine and
   ingress are built behind seam traits with in-memory test doubles. Only the
   P7 adapter deliverables carry family dependencies. No new Python route is
   added. Each owning family gets its own P7 adapter leaf, and the flip (7.15)
   waits on all of them, so #24151 flips last in Lane 5 (Orchestrator,
   2026-10-28).
   - The one Python change outside deletion is the DR6 forward (7.14): from the
     flip until #21570, the Python MCP proxy sends before- and after-tool rule
     passes of Rust-owned sessions to a backend-only gdaemon pass endpoint. It
     is a Python client of a Rust endpoint, required by the DR6 session-ownership
     ruling, and is deleted by #21570.
3. **Two crates.** `gobby-workflows` (engine, normalization, engine seams) and
   `gobby-ingress` (one `hooks` RouteFamily). Each follows the gruntime pattern:
   `publish = false`, gobby-core features `postgres`, `postgres-pool` and
   `route-family`, and wiring in `crates/gdaemon/src/serve.rs::run` beside
   `RuntimeFamily` inside the pool-gated block. The `hooks` family claims
   `/api/hooks`, `/api/webhooks`, `/api/nodes/hooks`, `/api/nodes/receipts` and
   `/api/nodes/detection-manifests`. `/api/nodes/checkouts` belongs to
   #21585/#24951, and `/api/nodes/report` and `/api/nodes/runs` belong to #21564
   (Decision 13); all three stay unclaimed.
   - Rejected: package `gobby-hooks`, already taken by ghook (`crates/ghook`).
   - Rejected: the family inside gdaemon. Family crates are the convention
     (crates/AGENTS.md).
   - Rejected: the engine inside `gobby-ingress`. It has four consumers.
4. **Session ownership (old DR6, extended to the whole pipeline).** At native
   default, CLI sessions that reach `POST /api/hooks/execute` are Rust-owned.
   Web chat (`src/gobby/servers/websocket/chat/_lifecycle.py`), ACP sessions
   (`src/gobby/adapters/acp_hook_adapter.py`, base of in-process Grok and AGY),
   the Codex app server (`src/gobby/adapters/codex_impl/app_server_adapter.py`)
   and the in-process runner stay Python-owned through Python `HookManager`
   until #21592 and #21564. No session is served by both runtimes.
5. **One envelope ledger.** `crates/gdaemon/src/nodes/hooks/ledger.rs::Ledger`
   moves into gcore as `crates/gcore/src/hook_ledger.rs` behind a new
   `hook-ledger` feature (3.1); the gdaemon node arm and `gobby-ingress` both
   import it. The hub claims every enveloped request in its own ledger root
   (`hooks/inbox`, Python-compatible files). The hub keeps one STOP replay
   epoch per machine; a node keeps its single file.
   - Rejected: a second, hub-only ledger.
   - Rejected: moving it into `gobby-ingress`. gdaemon would link the crate
     from P3 on, so every later P3-P6 deliverable would bump gdaemon. gcore is
     the shared dependency of both users, and one gcore release in 3.1 also
     carries Decision 6's seam.
   - The Rust hub inbox drain (3.5) sends each envelope through the front
     door's route table in process, so it follows the `hooks` route value and
     rollback to proxy needs no Python loop. 7.15 starts it and deletes the
     Python periodic drain and quarantine-retention loops, so two periodic
     drains never run together. The Python startup barrier
     (`src/gobby/runner_hook_replay.py`) stays and posts through the front door;
     per-file claims are exclusive.
6. **Compare soak for a mutating route.** For non-GET methods the front door
   proxies once and then calls `RouteFamily::check_proxied`, which sees only
   post-state. A new gcore default method, `RouteFamily::prepare_proxied`
   (no-op default, 3.1), is awaited before the proxied leg. The `hooks` family runs
   the Rust pass in record mode against pre-state: no writes and no external
   effects, with an in-pass overlay. `check_proxied` then compares the decision,
   reason, context, system message, modified input and staged receipt fields.
   A pass that depends on an `mcp_call` result compares only up to that call.
7. **Python deletion boundary.** 7.17 and 7.18 delete the routes and the modules only
   ingress uses, each only when `gcode grep -w <symbol> -l -g 'src/**'` shows no
   surviving importer. `HookManager` and the event handlers stay (Decision 4).
8. **Node session registration.** Registering a node session that lacks its
   pre-created row (hub-mode Decision 18, the fourth use of the key-verified
   foreign-machine path) sits behind the sessions seam. Its handler is 5.4
   and its adapter 7.2 (depends: #21562).
9. **Checkout reads.** The checkout-read seam trait and its local
   implementation live in `gobby-workflows`, because the engine is the main
   reader and `gobby-ingress` depends on it. The node implementation is a gdaemon
   adapter over the node channel (7.11, depends: #24521). Every Site Disposition
   row below keeps its outcome in Rust.
10. **Code-index trigger.** The native after-tool path calls
    `gobby_codeindex::jobs::trigger::EditTrigger::notify_file_changed` through
    `HubJobs::edit_trigger()` for the hub's own checkouts. 5.8 calls the
    seam, and 7.10 binds it (depends: #21586, #24705). Node events skip it (Site row 16).
11. **Bundled YAML conformance.** Every bundled rule, agent, workflow and
    pipeline body must load in the Rust loader: schema parse, condition parse,
    template compile and pattern compile. The engine corpus (1.1) uses synthetic
    rule bodies that exercise engine features; it never asserts per-rule
    behavior.
12. **Carried decisions DR1-DR8** from rust-rule-evaluation, minus its latency
    gate. Its flip gates D2.1-D2.9 land in P7, except D2.7, a py-spy GIL-share
    capture that the no-latency ruling drops; D2.8's tests prove single
    ownership (7.14). The carried decisions: DR5 fails closed in the response; DR6 serializes passes per session
    in FIFO order and takes the shared advisory locks `SessionVariableMutation`
    (950) and `AgentStepInstanceMutation` (875); DR7 reads definitions at one
    revision per pass; DR8 records outcome counters.
13. **Node report and run routes stay Python (Orchestrator ruling R, option A,
    2026-10-28).** `POST /api/nodes/report` and `/api/nodes/runs/*`, with the
    store of node-reported pane output, move to #21564, which owns every reader
    (`RemoteRunReconciler` under `AgentHealthMonitor`, `IdleCheckHandler`,
    `RemoteTerminalRuntime`). #21564 reassigns their ledger rows in its own
    ledger leaf, as #24951 did for checkouts. `GET
    /api/nodes/detection-manifests` stays here (6.2).

## Debt not to port
`kind: framing`

Josh's port rule (2026-10-24): parity matches contracts, not accidents. Each
row names the Python behaviour, its evidence and the disposition.

| Python behaviour | Evidence | Disposition |
| --- | --- | --- |
| One hub-wide `.stop_replay_epoch` file | node-halves Constraints | Drop: one epoch per machine (Decision 5) |
| Two envelope ledgers (Python `envelope_dedupe.py`, Rust node `Ledger`) | `src/gobby/hooks/envelope_dedupe.py`, `crates/gdaemon/src/nodes/hooks/ledger.rs` | Drop: one shared ledger (Decision 5) |
| Python drain loop POSTs envelopes back through the front door over HTTP | `src/gobby/hooks/inbox.py::_post_envelope`, `src/gobby/hooks/inbox_maintenance.py::drain_hook_inbox_loop` | Drop: the Rust drain dispatches through the route table in process, and 7.15 deletes the loop (Decision 5) |
| An 862-line route module | `src/gobby/servers/routes/mcp/hooks.py` | Drop: one Rust module per concern (P3) |
| Provider inferred from inner payload fields | memory a39d0411, #17173 | Keep the fix: `source` alone selects the adapter (4.1) |
| Seven `RuleEffect` types no bundled rule uses: `set_display_content`, `rewrite_input`, `set_permission_response`, `set_retry`, `set_watch_paths`, `set_worktree_path`, `set_elicitation` | `src/gobby/workflows/definitions.py::RuleEffect` (F2) | Keep: they are the rule-authoring schema, adapters already translate each field, and synthetic corpus cases pin them (2.12) |
| Per-bundled-rule behaviour tests | Josh, 2026-10-27 | Drop: schema and loader conformance only (Decision 11) |
| TDD-gate evidence code left after #23341 retired the gate | `src/gobby/workflows/tdd_paths.py` is gone; nothing in `src/` imports `src/gobby/workflows/rust_test_evidence.py` | Drop: not ported (Site rows 3 and 13); #25266 deletes the module and its two tests |

## Constraints
`kind: framing`

- Research baseline: main `0.5.0` checkout at `fe1f2dd1d0`. Executors
  re-resolve changed symbols against the implementation tip before editing.
- One deliverable is one independently verifiable outcome; its tests travel
  with it. Rust tests live in `<module>/tests.rs`. Every hand-maintained
  production file stays under 1,000 lines.
- A leaf that changes a released binary's recursive dependency closure bumps
  that binary by 0.0.1 with `Cargo.lock` and the managed version pins in the
  same commit (`src/gobby/install/version_pins.py`). gdaemon links both new
  crates from 7.1; the gcore change in 3.1 bumps every gcore dependent.
- The engine corpus replays recorded passes. That is engine testing; it is not
  per-rule testing (Decision 11).
- Rust tests run against the isolated test hub
  (`postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test`) and
  temporary roots, never the daemon database or real `~/.gobby` state.

## Cross-plan edges
`kind: framing`

Heading `(depends: #N)` marks carry the edges into this plan. The Orchestrator
sets the edges this plan cannot express in a heading at expansion.

| Task | Relation | Where |
| --- | --- | --- |
| #21562 Sessions | blocks | 7.2 |
| #21560 Tasks | blocks | 7.3 |
| #21564 Agents | blocks | 7.4; also receives the report and run rows and the pane store (Decision 13) |
| #21565 Terminals | blocks | 7.5 |
| #21580 Skills | blocks | 7.6 |
| #21566 MCP multiplexer | blocks | 7.7 |
| #21559 Config | blocks | 7.8 |
| #21585 Projects | blocks | 7.9 |
| #21586 Code-index orchestration | blocks | 7.10 |
| #24705 Edit trigger entry point | blocks | 7.10 |
| #24521 Node checkout.read handler | blocks | 7.11; moves down from #24151 to 7.11 at expansion |
| #25046 Reusable native admission | blocks | 7.12 |
| #25068 Agent-token and runtime-grant admission | blocks | 7.12 |
| #24149 S2.7 hub, leaf 4.1 (delegated rows survive Python removal) | blocks | 7.17; the Orchestrator binds the 4.1 leaf at expansion |
| #21567 Workflows | blocked by P2 under B | set by the Orchestrator at expansion |
| #24884 Native MCP gateway | blocked by #24151 | unchanged; consumes `gobby-workflows` |
| #24713 Code-index 9.7 | blocked by #24151 | unchanged |
| #21569 S2.11 holder | blocked by #24151 | unchanged |
| #22946 rust-rule-evaluation | closes superseded under B | Orchestrator |
| #21570 MCP front door flip | deletes the DR6 forward | 7.14 |
| #21571 CLI | owns the `gobby webhooks` verbs | 7.16 records it |

## Site Disposition port
`kind: framing`

hook-envelope-file-facts Decision 12 hands this plan its 30-row Site
Disposition table (`.gobby/plans/hook-envelope-file-facts.md`). Each row keeps
its node-event outcome and its unanswered outcome in Rust. The owning
deliverable carries one acceptance item per row.

| # | Python site | Outcome kept | Owner |
| --- | --- | --- | --- |
| 1 | `adapter_execution.py::run_adapter_hook` | sets the delivery origin | 5.12 |
| 2 | `daemon_git.py::DaemonGitService` callers | `node-read`, `fail-closed` per caller | 2.10 |
| 3 | `tdd_paths.py::_git_out` | retired with the TDD gate (#23341); not ported | — |
| 4 | `code_navigation_recovery.py::_git_ignored` | `node-read`, `degrade-existing` | 2.8 |
| 5 | `_normalization_git.py::_positional_checkout_is_path` | `node-read`, `degrade-existing` | 2.5 |
| 6 | `project_context.py::resolve_hook_project_context` | node checkout rows, `fail-closed` | 5.2 |
| 7 | `WorkflowHookHandler._resolve_project_path` | node checkout row | 2.19 |
| 8 | `_path_scope.py` primitives, `classify_git_mutation` | `node-read`, `fail-closed` | 2.7 |
| 9 | `condition_helpers_paths.py::write_paths_within` | `node-read`, `fail-closed` | 2.9 |
| 10 | `enforcement/blocking.py::plan_write_paths_allowed` | `node-read`, `fail-closed` | 2.16 |
| 11 | `monolith_guard.py::_projection_for_path` | `node-read`, `fail-closed` | 2.12 |
| 12 | `monolith_guard.py::outstanding_monolith_paths` | `node-read` batched, `fail-closed` | 2.12 |
| 13 | `rust_test_evidence.py::_read_absolute_text` | retired with the TDD gate (#23341); not ported | — |
| 14 | `code_navigation_recovery.py::_verified_source_line_count`, `_ignore_decision` | `node-read`, `degrade-existing` | 2.8 |
| 15 | `task_claim_state.py::resolve_edit_pair` | `node-read`, `degrade-existing` | 2.11 |
| 16 | `_tool.py::_notify_code_index` | `skip` | 5.8 |
| 17 | `commit_guard.py::_live_path_owners` | owners on the event's machine only | 2.10 |
| 18 | `shell_execution.py::_bounded_read`, `_normalization_operands.py::_file_transfer_paths` | `node-read`, `fail-closed` | 2.7 |
| 19 | `_misc.py` worktree create and remove | `skip`, allow before any hub effect | 5.10 |
| 20 | Claude, Grok and Codex spill readers | `skip` | 4.2, 4.3, 4.4 |
| 21 | `proxy_hooks.py::_run_rtk_proxy` | `skip` | 2.15 |
| 22 | `run_command_effects.py::_apply_run_command` | `skip` | 2.15 |
| 23 | `found_work_gate.py::_terminal_failures` | `no-change` | 2.12 |
| 24 | `validation_detection.py::load_project_validation_detection` | `no-change` | 2.12 |
| 25 | `startup_claim_preflight.py::_resolve_or_register_session` | `no-change`, node envelope refused | 5.12 |
| 26 | `observer_plan_mode.py` Codex transcript read | `no-change` | 2.18 |
| 27 | `terminal_context.py::_is_fresh_seat_tui` | `no-change`, returns before process inspection | 5.3 |
| 28 | Droid Notification wait branch | `skip` | 4.5 |
| 29 | Droid idle_prompt cancel branch | `skip` | 4.5 |
| 30 | AGY prompt backfill | `skip` | 4.6 |

## P1: Contract corpora
`kind: framing`

Freeze executable characterization of the Python engine, the ingress contract
clauses not yet recorded, and each adapter's translation before any Rust
replaces them. Python is the oracle until 7.17.

### 1.1 Rule-pass parity corpus and its Python exporter [category: test]
`kind: deliverable`

Targets:
- `tests/contracts/rules/manifest.json`
- `tests/contracts/rules/normalize_vectors.json`
- `tests/contracts/rules/README.md`
- `tests/contracts/rules_corpus.py`
- `tests/contracts/test_rules_corpus.py`

**Research context:**
- **Call surface.** `src/gobby/workflows/hooks.py::WorkflowHookHandler._evaluate_rules`
  calls `RuleEngine.evaluate(event, session_id, variables, eval_context,
  blocking_deadline)` (`src/gobby/workflows/engine/core.py`). It reads the staged
  payload from `response.metadata["_gobby_staged_effects"]` and merges the
  changed-key delta with `SessionVariableManager.merge_variables`
  (`src/gobby/workflows/state_manager.py`) under a `SessionVariableMutation`
  advisory lock. #23808 made that merge key-scoped.
- **Loading.** `RuleEngine._load_rules` calls `list_by_event` once per trigger
  event with `enabled=True` and the event's project id, dedupes rows by id, logs
  and skips parse failures, and sorts by `(priority, trigger_index, name)`. An
  event with no project id loads global rows only (#23866, `0328276aa4`).
- **Filtering.** `_filter_by_agent_scope`, `_filter_by_audience` and
  `_filter_by_active_rules`.
- **Effects.** `src/gobby/workflows/definitions.py::RuleEffect` accepts 15
  types. Bundled YAML uses eight: `block`, `set_variable`, `inject_context`,
  `mcp_call`, `observe`, `load_skill`, `run_command` and `proxy_hook`. The other
  seven are kept (Debt not to port, F2) and pinned here by synthetic cases.
- **Response.** `src/gobby/workflows/engine/evaluation.py::EvaluationMixin._assemble_response`
  fills these `HookResponse` fields (`src/gobby/hooks/events.py`): `decision`,
  `reason`, `context`, `context_parts`, `system_message`, `display_content`,
  `modified_input`, `modify_args`, `auto_approve`, `permission_decision`,
  `updated_permissions`, `retry`, `watch_paths`, `worktree_path`,
  `elicitation_action`, `elicitation_content`, `elicitation_error`,
  `trigger_action` and `metadata` (which carries `mcp_calls` and the staged
  payload).
- **Rewrite recheck (F4).** In `RuleEngine.evaluate`, a before-tool event whose
  input was rewritten by `_rewrite_input` or a `proxy_hook` re-runs agent tool
  enforcement, step tool enforcement and `_run_rule_loop(...,
  block_effects_only=True)` against the rewritten input. Non-block effects are
  not applied twice.
- **Seam-backed names.** The helpers and effects that reach other families are
  listed in `.gobby/plans/research/rule-evaluation-evidence-2026-10-09.md`
  (`#cross-family-seams-per-rule`).
- **Pattern to copy.** `tests/contracts/http_corpus.py` provides the
  schema-versioned manifest and case loading.

Build a schema-versioned corpus (`schema_version: 1`) of rule-pass cases, one
JSON file per case, listed in `manifest.json`. A case carries:
- synthetic rule bodies as `definition_json` with priority and project id;
- the project id, session facts and pre-pass variables;
- the normalized event the engine receives;
- an ordered seam script: each seam call with its arguments and its recorded
  response or failure;
- the `expect` block: every response field above, the eager variable delta,
  the staged `on_receipt` payload, the ordered effect log, the audit records
  and the DR8 outcome.

Rule bodies are synthetic. They exercise engine features; no case asserts the
behavior of a bundled rule (Decision 11). Both engines must produce each
case's `expect` block; no case carries a Rust-only expectation.

`rules_corpus.py` records cases. It seeds rows through `RuleDefinitionManager`
in the isolated test hub and runs `RuleEngine.evaluate`. Pytest monkeypatching
wraps each seam-backed helper and external effect executor in a recorder, so
no production Python changes. The exporter also writes `normalize_vectors.json`
from the normalizer entry points 2.3-2.8 port: `tokenize_shell_command`,
`scan_shell_command` and `extract_heredoc_bodies` (`_normalization_shell`),
`decode_ansi_c_escape` (`_ansi_c`), `apply_path_scope_metadata`
(`_path_scope`), `normalize_tool_fields` (`_normalization_tools`),
`classify_git_mutation` (`_normalization_git`), `executable_command_subjects`
and `command_patterns_match` (`command_matching`),
`blocks_direct_provider_launch` (`provider_launch_guard`) and
`rule_matches_agent` (`selectors`). It also records every module-level
function of a module 2.3-2.8 ports that another module calls (found with
`gcode callers`), so each normalization deliverable replays vectors of its own
modules. Command strings come from the corpus events and the existing cases in
`tests/workflows/test_command_matching.py`.

Required scenario cases:
- ordering by priority and trigger index;
- aggregate block;
- a `set_variable` read by a later rule;
- an `mcp_call` result read by a later rule;
- agent-scope, audience and active-rule-name filtering;
- two projects with different rows, and an event with no project id;
- a parse-failure row skipped;
- a condition error in a block rule and in a non-block rule;
- one failing seam call per seam kind (DR5);
- `on_receipt` staging, and an `acknowledge_variable` both eager and
  `on_receipt`;
- step-tool enforcement allow and block;
- the blocking deadline;
- a `proxy_hook` with handler `rtk` producing `modified_input`;
- the F4 recheck: a rewrite that a block rule then denies, and a rewrite whose
  non-block effects run once;
- a turn end with `HANDOFF_TURN_END_PENDING_VARIABLE` set, which allows;
- a hardcoded override with no rule installed (`include_rule_outputs=False`);
- one case per hardcoded behavior 2.16 and 2.17 list.

**Granularity:** one leaf with seven acceptance items. The case schema, the
exporter that is its oracle and the coverage checks are one artifact; a corpus
without its exporter cannot be re-recorded.

**Acceptance:**
- 1.1.1 - The manifest and every case load at `schema_version` 1, and a stale version is rejected. test: `tests/contracts/test_rules_corpus.py::test_loader_rejects_stale_schema_version`.
- 1.1.2 - All 15 effect types and both delivery classes appear in some case's expected effect log, using synthetic rule bodies only. test: `tests/contracts/test_rules_corpus.py::test_corpus_covers_effect_types_and_delivery`.
- 1.1.3 - Every required scenario case exists, including both F4 recheck cases. test: `tests/contracts/test_rules_corpus.py::test_corpus_has_required_scenarios`.
- 1.1.4 - The Python engine reproduces every case's `expect` block. test: `tests/contracts/test_rules_corpus.py::test_python_engine_replays_corpus`.
- 1.1.5 - Every hardcoded behavior 2.16 and 2.17 list has a case, and every response field appears set in some case. test: `tests/contracts/test_rules_corpus.py::test_corpus_covers_hardcoded_behavior_and_response_fields`.
- 1.1.6 - The Python functions reproduce every normalizer vector. test: `tests/contracts/test_rules_corpus.py::test_python_normalizer_replays_vectors`.
- 1.1.7 - The README documents the record and replay commands, the synthetic-rule rule, and the re-record obligation when the engine changes. file: `tests/contracts/rules/README.md`.

**Verification:** planned:
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/contracts/test_rules_corpus.py -v`.

### 1.2 Ingress contract cases not yet recorded [category: test]
`kind: deliverable`

Targets:
- `tests/contracts/hooks/manifest.json`
- `tests/contracts/hooks/README.md`
- `tests/contracts/hooks_corpus.py`
- `tests/contracts/test_hooks_corpus.py`
- `docs/contracts/hook-ingress.md`
- `tests/contracts/test_hook_ingress_doc.py::test_doc_states_frozen_contract`

**Research context:**
- **Already recorded by #24364.** `docs/contracts/hook-ingress.md` freezes the
  contract. `tests/contracts/http/hooks_execute_{ok,replay,below_floor,bad_schema}.json`
  are single-request HTTP cases (`schema_version` 2, family `hooks`, parity
  `proxy`, origin `python`), and
  `tests/contracts/test_hook_ingress_doc.py::test_doc_states_frozen_contract`
  checks the document.
- **Not yet recorded.** 403 `{"code":"hook_machine_mismatch"}`; the three 409
  bodies (`processing`, `processed`, `malformed_marker`); the STOP and
  `turn_end` 120-second block grace and its `.stop_replay_epoch` bound; the 503
  bodies `preflight_timeout`, `ingress_backpressure` (reasons
  `agent_run_identity_pending` and `daemon_not_ready`) and `adapter_timeout`;
  503 `{"detail":"HookManager not initialized"}`; and a receipt round trip.
- **DR5 mapping, not in the document.** A rule pass that raises
  `DatabaseOperationDeadlineExceeded` (`rule_pass_deadline`), a rule-row load
  failure (`rule_load_failure`), a STOP pass whose session variables cannot load
  (`stop_state_unavailable`), and a tool pass whose variables cannot load, which
  runs on empty variables and persists nothing (`tool_state_unavailable`).
- **Harness.** The HTTP corpus uses one function-scoped boundary daemon per
  case, so related requests must be one case. Forced failures run in process,
  as in
  `tests/servers/routes/test_hooks_agy_dispatch.py::test_ingress_retry_includes_retry_kind_discriminator`.

Add `tests/contracts/hooks/` with two case kinds:
- **`sequence`**: ordered requests against one boundary daemon, built the way
  `tests/contracts/test_http_corpus.py` builds it.
- **`envelope`**: one response forced in process with the technique above.

`hooks_corpus.py` imports `apply_masks`, `redact_secrets`, `credential_headers`
and `normalize_response` from `tests/contracts/http_corpus.py`.

Required sequence cases, for the Claude Code and Codex adapters: 403 machine
mismatch before any claim; a duplicate after success replayed with HTTP 200;
409 `processing` with a live lease; 409 `malformed_marker`; 409 `processed`
with no replayable response; a STOP block replayed inside the grace; a STOP
block re-evaluated after the grace and after an epoch bump; a synthetic block
rule and a synthetic turn-start context injection; and a response carrying
`_gobby_delivery_receipt`, followed by its acknowledgement file.

Required envelope cases: `preflight_timeout`, both `ingress_backpressure`
reasons, `adapter_timeout`, HookManager not initialized, and the four DR5
mappings recorded as observed.

Add a "Rule-pass failure responses" section to `docs/contracts/hook-ingress.md`
stating the four DR5 mappings, and extend the document test to require it.

**Acceptance:**
- 1.2.1 - The hooks manifest and every case load, and the loader rejects an unknown case kind. test: `tests/contracts/test_hooks_corpus.py::test_hooks_manifest_contract`.
- 1.2.2 - Every sequence case replays equal against the Python boundary daemon. test: `tests/contracts/test_hooks_corpus.py::test_sequence_case_replays_equal`.
- 1.2.3 - Every envelope case replays equal in process. test: `tests/contracts/test_hooks_corpus.py::test_envelope_case_replays_equal`.
- 1.2.4 - Every required sequence and envelope case exists. test: `tests/contracts/test_hooks_corpus.py::test_hooks_corpus_has_required_cases`.
- 1.2.5 - The contract document states the four DR5 failure responses, and its test requires the section. test: `tests/contracts/test_hook_ingress_doc.py::test_doc_states_frozen_contract`.

**Verification:** planned:
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/contracts/test_hooks_corpus.py tests/contracts/test_hook_ingress_doc.py tests/contracts/test_http_corpus.py -v`.

### 1.3 Adapter translation corpus [category: test]
`kind: deliverable`

Targets:
- `tests/contracts/adapters/manifest.json`
- `tests/contracts/adapters/README.md`
- `tests/contracts/adapters_corpus.py`
- `tests/contracts/test_adapters_corpus.py`

**Research context:**
- **Adapters on the route.** `src/gobby/adapters/claude_code.py::ClaudeCodeAdapter`,
  `src/gobby/adapters/codex_impl/hooks_adapter.py::CodexHooksAdapter` and
  `src/gobby/adapters/droid.py::DroidAdapter` extend
  `src/gobby/adapters/base.py::BaseAdapter`. `src/gobby/adapters/grok.py::GrokAdapter`
  and `src/gobby/adapters/agy.py::AgyAdapter` extend
  `src/gobby/adapters/acp_hook_adapter.py::ACPHookAdapter`. Each implements
  `translate_to_hook_event` and `translate_from_hook_response`.
- **Selection.** `source` alone selects the adapter (memory a39d0411, #17173).
- **Recorded payloads to reuse.** `tests/fixtures/provider_contracts/` holds
  recorded hook payloads per provider (`agy/hook-payloads.jsonl`,
  `droid/hook-payloads-0.219.0.jsonl`, `grok/hook-payloads.jsonl`,
  `claude/session-8944-post-tool-use.json`, `codex/*.json`), checked by
  `tests/adapters/test_provider_contract_fixtures.py`.
- **Node-origin branches.** Site rows 20 and 28-30 take a no-file branch for a
  node delivery.

Each case is one adapter call: a raw provider payload, the delivery origin and
the expected `HookEvent`, or a `HookResponse` and the expected provider output
JSON. The exporter replays the recorded fixtures above through each adapter
and adds synthetic payloads for every hook type the recorded set misses,
including the node-origin no-file branches.

**Acceptance:**
- 1.3.1 - The manifest and every case load, and the loader rejects an unknown adapter source. test: `tests/contracts/test_adapters_corpus.py::test_adapters_manifest_contract`.
- 1.3.2 - Every hook type each of the five adapters accepts has an inbound case, and every `HookResponse` field the adapter emits has an outbound case. test: `tests/contracts/test_adapters_corpus.py::test_adapters_corpus_covers_hook_types_and_fields`.
- 1.3.3 - The Python adapters reproduce every case. test: `tests/contracts/test_adapters_corpus.py::test_python_adapters_replay_corpus`.
- 1.3.4 - Node-origin cases exist for Site rows 20, 28, 29 and 30. test: `tests/contracts/test_adapters_corpus.py::test_adapters_corpus_has_node_origin_cases`.

**Verification:** planned:
`GOBBY_TEST_PROTECT=1 uv run pytest tests/contracts/test_adapters_corpus.py -v`.

### 1.4 Hook pipeline corpus and its Python exporter [category: test] (depends: 1.1)
`kind: deliverable`

Targets:
- `tests/contracts/pipeline/manifest.json`
- `tests/contracts/pipeline/README.md`
- `tests/contracts/pipeline_corpus.py`
- `tests/contracts/test_pipeline_corpus.py`

**Research context:**
- **Entry.** `src/gobby/hooks/hook_manager.py::HookManager` runs
  `_handle_after_daemon_ready`. Session start validates platform session
  metadata, resolves project context, runs the handler, then rules, then
  blocking webhooks. Other events resolve the session
  (`src/gobby/hooks/session_lookup.py`), gate terminal ingress
  (`validate_managed_agent_hook`, `session_end_targets_live_cli`), activate a
  deferred session (`activate_deferred_session`), apply Grok pending context,
  pulse activity, record machine ingress, resolve session refs in tool input,
  recheck the transcript, run rules (a block short-circuits), run blocking
  webhooks, then the handler, and complete the response.
- **Seam boundary.** `src/gobby/hooks/factory.py::HookManagerComponents` and
  `src/gobby/hooks/event_handlers/__init__.py::EventHandlers.__init__` name every
  collaborator: the session, session-task, task, agent-run, worktree and
  memory managers, the message processor, session coordinator, progress
  tracker, auto-link worker, skill manager, `call_tool`, workflow config,
  machine id, project resolution, code-index trigger, attention store,
  terminal manager and terminal runtime registry, plus the webhook dispatcher
  and broadcaster.

Each case carries an event, rule rows (usually none), and an ordered seam
script for every collaborator call with its recorded result; `expect` holds
the response and the ordered collaborator calls. A case may cite a parity
waiver id, and the Rust replay then checks the case's `waived_expect`
instead. `pipeline_corpus.py` records
cases by running `HookManager.handle` with every collaborator wrapped in a
recorder and the real rule engine on the isolated test hub, as 1.1 does.
Required cases: one per handler entry in `EventHandlers.get_handler_map` for
each event type it maps, session start for a new, resumed, cleared and
compacted session, a node session missing its row, a deferred activation, a
blocking webhook, a rule block that short-circuits the handler, and one
failing collaborator per seam kind.

**Acceptance:**
- 1.4.1 - The manifest and every case load, and the loader rejects an unknown collaborator name. test: `tests/contracts/test_pipeline_corpus.py::test_pipeline_manifest_contract`.
- 1.4.2 - Every handler-map entry and every required case exists. test: `tests/contracts/test_pipeline_corpus.py::test_pipeline_corpus_has_required_cases`.
- 1.4.3 - Python's `HookManager` reproduces every case's response and collaborator calls. test: `tests/contracts/test_pipeline_corpus.py::test_python_pipeline_replays_corpus`.

**Verification:** planned:
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/contracts/test_pipeline_corpus.py -v`.

## P2: Rule engine in `gobby-workflows`
`kind: framing`

Port the definitions loader, normalization, condition language, helpers,
effects and the pass driver behind engine seams, replaying the 1.1 corpus.
The crate links into no binary until 7.1. Every deliverable edits
`crates/gworkflows/src/lib.rs` and `crates/gworkflows/Cargo.toml`, so P2 runs
as a chain.

Shared rules for every P2 deliverable:
- Each seam trait call takes the project id and returns
  `Result<_, SeamUnavailable>`. `seams::memory` implements every trait from a
  corpus case's seam script and fails the test on an unscripted call.
- A Python module over 850 lines is split into two Rust files up front.
- A Python module is ported only where the engine reaches it; Python stays the
  oracle and is not edited in P2.
- Verification for every P2 deliverable:
  `GOBBY_SCHEMA_TEST_DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test cargo nextest run -p gobby-workflows`,
  `cargo fmt --check` and
  `cargo clippy -p gobby-workflows --all-targets -- -D warnings`.

### 2.1 Crate, rule definitions, revision cache and bundled-body parse [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `Cargo.toml`
- `Cargo.lock`
- `crates/gworkflows/Cargo.toml`
- `crates/gworkflows/src/lib.rs`
- `crates/gworkflows/src/definitions.rs`
- `crates/gworkflows/src/definitions/body.rs`
- `crates/gworkflows/src/definitions/repo.rs`
- `crates/gworkflows/src/definitions/cache.rs`
- `crates/gworkflows/src/definitions/tests.rs`
- `crates/gworkflows/src/event.rs`
- `crates/gworkflows/src/event/tests.rs`
- `crates/gworkflows/tests/bundled_conformance.rs`
- `crates/gworkflows/tests/definitions_repo.rs`

**Research context:**
- **Crate pattern.** `crates/gruntime/Cargo.toml` (package `gobby-runtime`):
  `publish = false`, workspace edition and lints, `gobby-core` with features
  `postgres`, `postgres-pool` and `route-family`.
- **Tables** (`crates/gcore/assets/schema/baseline.sql`): `rule_definitions`
  (`id`, `project_id`, `name`, `enabled`, `priority`, `definition_json` jsonb,
  `source`, `deleted_at`, timestamps), `definition_revisions` (`domain`,
  `revision`), `agent_definitions`, `agent_step_workflows`.
- **Python bodies.** `src/gobby/workflows/definitions.py::RuleDefinitionBody`,
  `RuleEffect` (15 types, `extra="forbid"`), `RuleTriggerEvent`,
  `VariableDefinitionBody` and `Observer`. Python's cache validates against the
  revision `RuleEngine._cached_rules` reads.
- **Bundled sync.** `src/gobby/workflows/sync_rules.py::_sync_rule_file` reads
  each YAML file's `rules` mapping and applies file-level `group`, `tags`,
  `sources` and `audience` defaults.
  `src/gobby/workflows/sync_rules.py::resolve_sync_placeholders` replaces
  `{{ gobby_bin }}`. Sync itself stays Python until #21567.
- **YAML crate.** `yaml_serde` 0.10.4 is already in `Cargo.lock` (used by
  gcore and gcode as `serde_yaml`).
- **Event model.** `src/gobby/hooks/events.py` (461 lines): `HookEvent`,
  `HookEventType`, `HookResponse` and `SessionSource`. The engine, the
  adapters and the pipeline all exchange these.

Add `crates/gworkflows` (package `gobby-workflows`) to the root members with
`gobby-core` (feature `postgres-pool`), `serde`, `serde_json`, `thiserror`,
`tokio` and `async-trait`, and `yaml_serde` as a dev-dependency.
- **`definitions::body`** mirrors `RuleDefinitionBody` and every `RuleEffect`
  type, accepting exactly the bodies Python accepts.
- **`definitions::repo`** lists enabled, undeleted rows per trigger event:
  global rows plus the event project's rows, deduped by id and sorted by
  `(priority, trigger_index, name)`. It reads `definition_revisions`.
- **`definitions::cache`** implements DR7. A parse failure is skipped, logged
  once per row and revision, and counted.
- **`event`** mirrors `HookEvent`, `HookEventType`, `HookResponse` and
  `SessionSource` with Python's JSON shape, so `gobby-ingress` reuses them.
- **Bundled conformance (Decision 11).** `bundled_conformance.rs` reads every
  YAML file under `src/gobby/install/shared/workflows/rules/`, applies the sync
  defaults and the `{{ gobby_bin }}` substitution, and parses each body. It also
  parses the rule selectors and step workflows of every file under
  `src/gobby/install/shared/workflows/agents/` and every body under
  `src/gobby/install/shared/workflows/variables/`. Pipelines belong to #21567's
  loader. 2.13 extends this test to compilation.

**Granularity:** one leaf with eight production files, counting both
`Cargo.toml` files, and six acceptance items. The manifest, body, repository
and cache form one loader that the cache cannot be tested without; the event
model is the type every loaded body is matched against.

**Acceptance:**
- 2.1.1 - The crate is a workspace member, clippy passes with `-D warnings`, and no other crate depends on it yet. file: `crates/gworkflows/Cargo.toml`.
- 2.1.2 - Every rule body in the 1.1 corpus parses, and a body Python rejects is skipped and counted while the rest of the pass loads. test: `crates/gworkflows/src/definitions/tests.rs::corpus_bodies_parse_and_rejects_are_counted`.
- 2.1.3 - The repository returns global rows plus same-project rows, deduped and ordered, and an event with no project id loads global rows only. test: `crates/gworkflows/tests/definitions_repo.rs::lists_global_and_same_project_rows_in_order`.
- 2.1.4 - Advancing the `rules` revision makes the next pass reload, and a failed revision read returns `DefinitionsUnavailable` without using the cache. test: `crates/gworkflows/tests/definitions_repo.rs::cache_reloads_on_revision_and_fails_closed`.
- 2.1.5 - Every bundled rule body, agent selector and step workflow, and variable body parses. test: `crates/gworkflows/tests/bundled_conformance.rs::every_bundled_body_parses`.
- 2.1.6 - Every 1.1 corpus event and expected response round-trips through `event` to the same JSON. test: `crates/gworkflows/src/event/tests.rs::corpus_events_round_trip`.

**Verification:** the shared P2 commands.

### 2.2 Checkout-read seam and local implementation [category: code] (depends: 2.1)
`kind: deliverable`

Targets:
- `crates/gworkflows/Cargo.toml`
- `crates/gworkflows/src/lib.rs`
- `crates/gworkflows/src/seams.rs`
- `crates/gworkflows/src/seams/checkout.rs`
- `crates/gworkflows/src/seams/memory.rs`
- `crates/gworkflows/src/seams/tests.rs`

**Research context:**
- **Python seam.** `src/gobby/hooks/checkout_reads.py` (319 lines):
  `delivery_scope` binds a `_NodeDelivery` in a context variable;
  `node_origin` and `event_machine_id` read it; `path_facts` returns
  `CheckoutFacts` of `PathFact` entries with a read budget; `run_git` and
  `run_git_async` run git on the event's checkout. Node reads send the
  `checkout.read` node command with a 5,000 ms file timeout. Per-request codes
  `invalid_payload`, `no_project_root` and `outside` fail only that request;
  any other failure latches the delivery, so every later read raises
  `CheckoutReadUnavailable(code, machine_id)`.
- **Ownership.** hook-envelope-file-facts Decision 12 and this plan's
  Decision 9.

Add `seams::checkout`: a `CheckoutReads` trait with `path_facts` and
`run_git`, a `CheckoutReadUnavailable` error carrying the code and machine id,
and a `DeliveryOrigin` (hub-local or node machine id). `LocalCheckout`
implements the trait over the filesystem and a git subprocess for the hub's
own checkouts. A `LatchedReads` wrapper applies the per-request and latch
rules to any implementation. The node implementation is 7.11's.
`seams::memory` gains a scripted `CheckoutReads`.

**Acceptance:**
- 2.2.1 - `LocalCheckout` returns the same path facts and git results as `checkout_reads.path_facts` and `run_git` on a temporary repository, including the read budget cut. test: `crates/gworkflows/src/seams/tests.rs::local_checkout_matches_python_facts`.
- 2.2.2 - A per-request code fails only that read, and any other failure latches every later read of the delivery. test: `crates/gworkflows/src/seams/tests.rs::latch_follows_python_codes`.
- 2.2.3 - `DeliveryOrigin` reports hub-local or the node machine id, and the scripted implementation fails the test on an unscripted read. test: `crates/gworkflows/src/seams/tests.rs::delivery_origin_and_scripted_reads`.

**Verification:** the shared P2 commands.

### 2.3 Shell lexing [category: code] (depends: 2.2)
`kind: deliverable`

Targets:
- `crates/gworkflows/src/lib.rs`
- `crates/gworkflows/src/normalize.rs`
- `crates/gworkflows/src/normalize/shell.rs`
- `crates/gworkflows/src/normalize/shell/scan.rs`
- `crates/gworkflows/src/normalize/ansi_c.rs`
- `crates/gworkflows/src/normalize/syntax.rs`
- `crates/gworkflows/src/normalize/stdin.rs`
- `crates/gworkflows/src/normalize/words.rs`
- `crates/gworkflows/src/normalize/tests.rs`
- `crates/gworkflows/tests/normalize_corpus.rs`

**Research context:** the Python modules, all in `src/gobby/hooks/`:
`_normalization_shell.py` (961 lines: `tokenize_shell_command`,
`scan_shell_command`, `extract_heredoc_bodies`), `_ansi_c.py` (129:
`decode_ansi_c_escape`, `opens_ansi_c_quote`), `_shell_syntax.py` (94),
`_provider_shell_syntax.py` (319), `_case_syntax.py` (279),
`_normalization_shell_stdin.py` (211) and `_shell_execution_words.py` (318).
No Rust equivalent exists in `crates/ghook` or `crates/gcore`.

Port them as `normalize`'s lexing layer with the same tokens, scan results and
dialect handling. `shell.rs` holds the tokenizer and token predicates;
`shell/scan.rs` holds `scan_shell_command`, heredoc extraction and the
redirection checks. `syntax.rs` holds the shell, provider-shell and `case`
syntax; `stdin.rs` and `words.rs` port the stdin and execution-word modules.

**Granularity:** one leaf with eight production files. Each ports one Python
module or one half of a split module; the shared vector replay tests them
together and they call each other.

**Acceptance:**
- 2.3.1 - Every vector recorded from the seven modules replays equal. test: `crates/gworkflows/tests/normalize_corpus.rs::shell_lexing_vectors_replay_equal`.
- 2.3.2 - Unterminated quotes, nested heredocs and invalid escapes produce the Python result without a panic. test: `crates/gworkflows/src/normalize/tests.rs::malformed_shell_input_matches_python`.

**Verification:** the shared P2 commands.

### 2.4 Shell segments and command structure [category: code] (depends: 2.3)
`kind: deliverable`

Targets:
- `crates/gworkflows/src/normalize.rs`
- `crates/gworkflows/src/normalize/segments.rs`
- `crates/gworkflows/src/normalize/compound.rs`
- `crates/gworkflows/src/normalize/redirects.rs`
- `crates/gworkflows/src/normalize/commands.rs`
- `crates/gworkflows/src/normalize/env.rs`
- `crates/gworkflows/src/normalize/cwd.rs`
- `crates/gworkflows/src/normalize/tests.rs`
- `crates/gworkflows/tests/normalize_corpus.rs`

**Research context:** the Python modules in `src/gobby/hooks/`:
`_normalization_segments.py` (270), `_normalization_compound.py` (308),
`_normalization_pipeline.py` (93), `_normalization_redirects.py` (283),
`_normalization_base_commands.py` (211), `_normalization_file_commands.py`
(103), `_normalization_bindings.py` (186), `_normalization_env.py` (153),
`_normalization_interpreter_segments.py` (59) and `_normalization_cwd.py`
(565). All are private modules; their module-level functions that other
modules call are the vector boundary (1.1).

Port them: `segments.rs` (segments, pipeline and interpreter segments),
`compound.rs`, `redirects.rs`, `commands.rs` (base and file commands, bindings),
`env.rs` and `cwd.rs`.

**Granularity:** one leaf with seven production files, one per Python module
group; the vectors test them together.

**Acceptance:**
- 2.4.1 - Every vector recorded from the ten modules replays equal. test: `crates/gworkflows/tests/normalize_corpus.rs::segment_vectors_replay_equal`.
- 2.4.2 - Compound commands, subshells and `cd` chains resolve the working directory as Python does. test: `crates/gworkflows/src/normalize/tests.rs::cwd_resolution_matches_python`.

**Verification:** the shared P2 commands.

### 2.5 Operands, paths, git, sed and perl [category: code] (depends: 2.4)
`kind: deliverable`

Targets:
- `crates/gworkflows/src/normalize.rs`
- `crates/gworkflows/src/normalize/operands.rs`
- `crates/gworkflows/src/normalize/paths.rs`
- `crates/gworkflows/src/normalize/git.rs`
- `crates/gworkflows/src/normalize/sed.rs`
- `crates/gworkflows/src/normalize/perl.rs`
- `crates/gworkflows/src/normalize/tests.rs`
- `crates/gworkflows/tests/normalize_corpus.rs`

**Research context:** the Python modules in `src/gobby/hooks/`:
`_normalization_operands.py` (730, including `_file_transfer_paths`),
`_normalization_paths.py` (351), `_normalization_git.py` (555, including
`classify_git_mutation` and `_positional_checkout_is_path`),
`_sed_arguments.py` (150), `_sed_expansion.py` (87), `_sed_program.py` (267),
`_normalization_sed.py` (131) and `_perl_arguments.py` (62).
- **Site row 5.** `_positional_checkout_is_path` reads the checkout to decide
  whether a `git checkout` operand is a path. A node event reads through the
  checkout seam; an unanswered read degrades to unknown (`None`).

Port them as `operands.rs`, `paths.rs`, `git.rs`, `sed.rs` (all four sed
modules) and `perl.rs`. Every checkout read goes through 2.2's
`CheckoutReads`.

**Acceptance:**
- 2.5.1 - Every vector recorded from the eight modules replays equal, including `classify_git_mutation`. test: `crates/gworkflows/tests/normalize_corpus.rs::operand_and_git_vectors_replay_equal`.
- 2.5.2 - Site row 5: a node `git checkout` operand reads through the seam, and an unanswered read classifies the operand as unknown. test: `crates/gworkflows/src/normalize/tests.rs::site_row_5_checkout_operand_degrades_to_unknown`.

**Verification:** the shared P2 commands.

### 2.6 Inline interpreter classification [category: code] (depends: 2.5)
`kind: deliverable`

Targets:
- `crates/gworkflows/Cargo.toml`
- `Cargo.lock`
- `crates/gworkflows/src/normalize.rs`
- `crates/gworkflows/src/normalize/python_source.rs`
- `crates/gworkflows/src/normalize/python_pipeline.rs`
- `crates/gworkflows/src/normalize/python_pipeline/ast_walk.rs`
- `crates/gworkflows/src/normalize/python_mutation.rs`
- `crates/gworkflows/src/normalize/inline_interpreter.rs`
- `crates/gworkflows/src/normalize/tests.rs`
- `crates/gworkflows/tests/normalize_corpus.rs`

**Research context:** `src/gobby/hooks/_python_pipeline_classifier.py` (750),
`_python_mutation_targets.py` (300) and `_inline_interpreter_classifier.py`
(187) walk Python `ast` trees. `provider_launch_guard.py` (2.8) also parses
inline scripts with `ast.parse`. rust-rule-evaluation decided
`rustpython-parser`; it is not yet in `Cargo.lock`.

`python_source.rs` wraps `rustpython-parser`, pinned at its newest release that
is at least two weeks old when 2.6 starts. Both this classifier and 2.8's
launch guard consume it.
- Rejected: a hand-written statement parser, which needs the full statement
  grammar.
- Rejected: treating unparsed inline Python as not read-only, which diverges
  from Python and blocks pipelines Python allows.

**Granularity:** one leaf with seven production files. The parser wrapper and
the three classifiers share one tree walk.

**Acceptance:**
- 2.6.1 - Every vector recorded from the three modules replays equal. test: `crates/gworkflows/tests/normalize_corpus.rs::inline_interpreter_vectors_replay_equal`.
- 2.6.2 - Python source that fails to parse yields the Python classifier's result for unparseable input. test: `crates/gworkflows/src/normalize/tests.rs::unparseable_python_matches_python`.

**Verification:** the shared P2 commands.

### 2.7 Path scope, shell execution and tool-field normalization [category: code] (depends: 2.6)
`kind: deliverable`

Targets:
- `crates/gworkflows/src/normalize.rs`
- `crates/gworkflows/src/normalize/path_scope.rs`
- `crates/gworkflows/src/normalize/shell_execution.rs`
- `crates/gworkflows/src/normalize/metadata.rs`
- `crates/gworkflows/src/normalize/mcp.rs`
- `crates/gworkflows/src/normalize/tools.rs`
- `crates/gworkflows/src/normalize/canonical.rs`
- `crates/gworkflows/src/normalize/canonical/outcome.rs`
- `crates/gworkflows/src/normalize/tests.rs`
- `crates/gworkflows/tests/normalize_corpus.rs`

**Research context:** `src/gobby/hooks/_path_scope.py` (329:
`apply_path_scope_metadata`), `shell_execution.py` (282: `_bounded_read`,
`path_execution`, `read_script_body`), `_normalization_metadata.py` (507),
`_normalization_mcp.py` (217), `_normalization_notifications.py` (64),
`_normalization_tool_input.py` (64), `_normalization_tools.py` (318:
`normalize_tool_fields`) and `_normalization_canonical.py` (875). The facade
`src/gobby/hooks/normalization.py` exports the public names, including
`normalize_tool_fields`, `normalize_mcp_fields`, `normalize_tool_outcome`,
`normalize_notification_input`, `is_shell_tool` and
`canonicalize_shell_tool_name`. Adapters, the engine and the event handlers
import the facade.
- **Site row 8.** `_path_scope` primitives and `classify_git_mutation` read the
  checkout for write, navigation and git events; unanswered fails closed by
  propagation.
- **Site row 18.** `shell_execution._bounded_read` and
  `_normalization_operands._file_transfer_paths` read script bodies and
  transfer paths; unanswered fails closed by propagation.

Port them, with `normalize`'s public API matching the facade's public names.
`canonical.rs` and `canonical/outcome.rs` split the 875-line canonical module
(tool metadata, then outcomes). This completes `normalize`; `gobby-ingress`
consumes it through this crate's API.

**Granularity:** one leaf with eight production files. Each ports one Python
module or one half of a split module; `normalize_tool_fields` vectors exercise
them together.

**Acceptance:**
- 2.7.1 - Every `apply_path_scope_metadata` and `normalize_tool_fields` vector, and every vector recorded from the eight modules, replays equal. test: `crates/gworkflows/tests/normalize_corpus.rs::tool_field_vectors_replay_equal`.
- 2.7.2 - Site row 8: a node write, navigation or git event reads path scope through the seam, and an unanswered read fails the normalization closed. test: `crates/gworkflows/src/normalize/tests.rs::site_row_8_path_scope_fails_closed`.
- 2.7.3 - Site row 18: script bodies and transfer paths of a node event read through the seam, and an unanswered read fails closed. test: `crates/gworkflows/src/normalize/tests.rs::site_row_18_script_reads_fail_closed`.

**Verification:** the shared P2 commands.

### 2.8 Command matching, selectors, launch guard and code navigation [category: code] (depends: 2.7)
`kind: deliverable`

Targets:
- `crates/gworkflows/Cargo.toml`
- `Cargo.lock`
- `crates/gworkflows/src/lib.rs`
- `crates/gworkflows/src/matching.rs`
- `crates/gworkflows/src/matching/command.rs`
- `crates/gworkflows/src/matching/patterns.rs`
- `crates/gworkflows/src/matching/selectors.rs`
- `crates/gworkflows/src/matching/provider_launch.rs`
- `crates/gworkflows/src/matching/provider_launch/inline.rs`
- `crates/gworkflows/src/matching/navigation.rs`
- `crates/gworkflows/src/matching/tests.rs`
- `crates/gworkflows/tests/matching_corpus.rs`

**Research context:**
- `src/gobby/workflows/engine/command_matching.py` (849:
  `command_patterns_match`, `executable_command_subjects`) with
  `command_matching_heredoc.py` (87) and `command_matching_uv.py` (207);
  `src/gobby/workflows/selectors.py` (235: `rule_matches_agent`);
  `src/gobby/hooks/provider_launch_guard.py` (968:
  `blocks_direct_provider_launch`); `src/gobby/hooks/code_navigation.py` (321)
  and `code_navigation_recovery.py` (640: `navigation_requires_index`,
  `_git_ignored`, `_verified_source_line_count`, `_ignore_decision`).
- **Regex dialect.** Rule patterns use Python `re`. Bundled rules use
  lookarounds the `regex` crate rejects. `regex` and `fancy-regex` are both in
  `Cargo.lock`.
- **Site row 4.** `_git_ignored`: node read; unanswered degrades to not
  ignored, so enforcement stays on.
- **Site row 14.** `_verified_source_line_count` and `_ignore_decision`: node
  read; unanswered leaves the line count unverified, so the read counts as
  broad, and a missing gcode config counts as empty.

Port them as `matching`. `matching::patterns` compiles each rule pattern once
per DR7 cache entry with `regex` when it compiles there and `fancy-regex`
otherwise; a pattern neither accepts is a counted parse failure. No pattern
compiles inside a pass. `provider_launch.rs` and `provider_launch/inline.rs`
split the 968-line guard; the inline-script half uses 2.6's `python_source`.

**Granularity:** one leaf with nine production files. The launch guard and
navigation checks are dependencies of command matching, and the shared vectors
test them together.

**Acceptance:**
- 2.8.1 - Every `command_patterns_match` and `executable_command_subjects` vector replays equal. test: `crates/gworkflows/tests/matching_corpus.rs::command_vectors_replay_equal`.
- 2.8.2 - Every `rule_matches_agent` vector replays equal. test: `crates/gworkflows/tests/matching_corpus.rs::selector_vectors_replay_equal`.
- 2.8.3 - Every `blocks_direct_provider_launch` vector replays equal, including inline Python scripts. test: `crates/gworkflows/tests/matching_corpus.rs::provider_launch_vectors_replay_equal`.
- 2.8.4 - Lookaround patterns match as Python's `re` does on the corpus strings, and compilation happens only on cache load. test: `crates/gworkflows/src/matching/tests.rs::patterns_compile_once_with_python_semantics`.
- 2.8.5 - Site row 4: an unanswered node `_git_ignored` read treats the path as not ignored. test: `crates/gworkflows/src/matching/tests.rs::site_row_4_ignore_check_keeps_enforcement`.
- 2.8.6 - Site row 14: an unanswered node line-count read counts the read as broad, and a missing gcode config counts as empty. test: `crates/gworkflows/src/matching/tests.rs::site_row_14_line_count_degrades`.

**Verification:** the shared P2 commands.

### 2.9 Condition language, pure helpers and query seams [category: code] (depends: 2.8)
`kind: deliverable`

Targets:
- `crates/gworkflows/src/lib.rs`
- `crates/gworkflows/src/condition.rs`
- `crates/gworkflows/src/condition/parse.rs`
- `crates/gworkflows/src/condition/value.rs`
- `crates/gworkflows/src/condition/eval.rs`
- `crates/gworkflows/src/condition/helpers.rs`
- `crates/gworkflows/src/condition/helpers/paths.rs`
- `crates/gworkflows/src/condition/helpers/shell.rs`
- `crates/gworkflows/src/condition/tests.rs`
- `crates/gworkflows/src/seams.rs`
- `crates/gworkflows/src/seams/memory.rs`
- `crates/gworkflows/tests/condition_corpus.rs`

**Research context:**
- **Grammar.** `src/gobby/workflows/safe_evaluator.py::SafeExpressionEvaluator`
  (751-line module) accepts `Expression`, `BoolOp` (`And`, `Or`), `BinOp`
  (`Add`, `Sub`, `Mod`, `FloorDiv`), `UnaryOp` (`Not`, `UAdd`, `USub`),
  `Compare` (`Eq`, `NotEq`, `Lt`, `LtE`, `Gt`, `GtE`, `In`, `NotIn`, `Is`,
  `IsNot`), `IfExp`, `Call`, `Attribute`, `Subscript`, `Name`, `Constant`,
  `List`, `Tuple`, `Dict`, `ListComp` and `GeneratorExp`. `and` and `or` return
  operands.
- **Helpers.** `src/gobby/workflows/safe_evaluator.py::build_condition_helpers`
  assembles the helper table from `condition_helpers.py` (773),
  `condition_helpers_paths.py` (188: `PATH_CONDITION_HELPERS`,
  `write_paths_within`), `condition_helpers_sessions.py` (602),
  `approval_brace_guard.py` (882: `approval_command_word_expansion`),
  `remote_fetch_guard.py` (364: `remote_fetch_execution`) with
  `remote_fetch_shell.py` (459) and `remote_fetch_preflight.py` (271),
  `regex_validation.py` (117), `assistant_response_matching.py` (116),
  `monolith_guard.py`, `task_claim_state.py`, `code_navigation_recovery.py` and
  `provider_launch_guard.py`. `TemplatingMixin._build_allowed_funcs`
  (`src/gobby/workflows/engine/templating.py`) adds more.
- **Site row 9.** `write_paths_within` (`seat-write-scope`): node read;
  unanswered returns `False`, so the write is outside scope.

Build `condition`:
- **Parser.** A hand-written parser for exactly the accepted subset, including
  Python string literal forms, reproducing
  `SafeExpressionEvaluator._normalize_expr` and rejecting every other node,
  f-strings and lambdas included. Rejected: a full Python parser crate for a
  subset of about 20 nodes.
- **Values.** A dynamic `Value` with Python truthiness, comparison, membership,
  indexing and the `Mod` and `FloorDiv` semantics the vectors pin.
- **Evaluator.** Applies DR5's per-rule rule for deterministic errors.
- **Pure helpers.** `condition::helpers` ports every helper with no seam;
  `helpers/paths.rs` ports the path helpers on 2.2's seam, and
  `helpers/shell.rs` ports the approval-brace and remote-fetch guards.
- **Query seams.** `seams` gains `TaskQueries`, `SessionQueries` (targets,
  pending messages, waits, stop signals), `AgentDefinitions`,
  `TranscriptQueries` and `CodeIndexQueries`, each with a scripted
  implementation. 2.10 and 2.11 implement the seam-backed helpers over them.

**Granularity:** one leaf with eleven production files. The parser, value model
and evaluator are one language the vectors exercise together; the pure helpers
and query seams belong here because conditions are their first callers.

**Acceptance:**
- 2.9.1 - Every condition the 1.1 cases evaluate replays to the same value or error class. test: `crates/gworkflows/tests/condition_corpus.rs::condition_vectors_replay_equal`.
- 2.9.2 - Every node outside the accepted subset is rejected at parse time, f-strings and lambdas included. test: `crates/gworkflows/src/condition/tests.rs::rejects_nodes_outside_the_subset`.
- 2.9.3 - `and` and `or` return operands, and truthiness matches Python for every value kind. test: `crates/gworkflows/src/condition/tests.rs::boolean_operators_return_operands`.
- 2.9.4 - A deterministic error matches in a block rule and not in a non-block rule, and a seam failure inside a helper is never read as `False`. test: `crates/gworkflows/src/condition/tests.rs::errors_follow_the_per_rule_rule`.
- 2.9.5 - Every helper name `build_condition_helpers` and `_build_allowed_funcs` define resolves, and an unscripted seam call fails the test. test: `crates/gworkflows/src/condition/tests.rs::every_helper_name_resolves`.
- 2.9.6 - Site row 9: an unanswered node read makes `write_paths_within` return `False`. test: `crates/gworkflows/src/condition/tests.rs::site_row_9_write_scope_fails_closed`.

**Verification:** the shared P2 commands.

### 2.10 Git, commit and review helpers [category: code] (depends: 2.9)
`kind: deliverable`

Targets:
- `crates/gworkflows/src/lib.rs`
- `crates/gworkflows/src/git.rs`
- `crates/gworkflows/src/git/service.rs`
- `crates/gworkflows/src/git/commits.rs`
- `crates/gworkflows/src/git/commit_guard.rs`
- `crates/gworkflows/src/git/commit_guard/conflicts.rs`
- `crates/gworkflows/src/git/review_scope.rs`
- `crates/gworkflows/src/git/ledger.rs`
- `crates/gworkflows/src/git/tests.rs`
- `crates/gworkflows/tests/git_helpers_corpus.rs`

**Research context:**
- **Python modules.** `src/gobby/utils/daemon_git.py::DaemonGitService`
  (`_execute`, `status`), `src/gobby/workflows/git_utils.py` (556),
  `git_commit_parsing.py` (453), `commit_guard.py` (863:
  `foreign_dirty_edit_conflict`, `foreign_staged_commit_conflict`,
  `_live_path_owners`) with `commit_guard_conflicts.py` (141),
  `code_review_scope.py` (607: `inspect_commit_review_scope`),
  `code_review_freshness.py` (157), `worktree_shell_attribution.py` (532),
  `ledger_reconcile.py` (140: `reconcile_edit_ledgers`), `task_dirty_state.py`
  (121) and `checkout_fingerprint.py` (31). Session and task facts come
  through 2.9's query seams.
- **Site row 2.** Every caller of `DaemonGitService` reads git through the
  checkout seam. Unanswered fails closed through each caller's git-failure
  branch: `commit_guard` blocks naming the detail, the review gate stays
  armed, the ledger releases nothing, the worktree root stays unresolved, and
  the landed check (`paths_committed_after_async`) keeps the attribution.
- **Site row 17.** `_live_path_owners` counts owners on the event's machine
  only.
- **Retired rows.** Site rows 3 (`tdd_paths.py`) and 13
  (`rust_test_evidence.py`) belonged to the TDD gate #23341 retired.
  `src/gobby/workflows/tdd_paths.py` is gone, and nothing in `src/` imports
  `src/gobby/workflows/rust_test_evidence.py`. Neither is ported.

Port the modules as `git`. `git::service` replaces `DaemonGitService` on 2.2's
`CheckoutReads::run_git`, so a hub event runs local git and a node event reads
through the seam. The 863-line commit guard splits into `commit_guard.rs` and
`commit_guard/conflicts.rs`.

**Granularity:** one leaf with nine production files. They share
`git::service` and one failure contract (Site row 2), which the corpus tests
together.

**Acceptance:**
- 2.10.1 - Every commit-guard, review-scope, ledger-reconcile and dirty-state case in the 1.1 corpus replays equal. test: `crates/gworkflows/tests/git_helpers_corpus.rs::git_helper_cases_replay_equal`.
- 2.10.2 - Site row 2: for a node event with an unanswered read, the commit guard blocks naming the detail, the review gate stays armed, the ledger releases nothing, the worktree root stays unresolved and the landed check keeps the attribution. test: `crates/gworkflows/src/git/tests.rs::site_row_2_git_failures_fail_closed_per_caller`.
- 2.10.3 - Site row 17: live path owners come only from the event's machine. test: `crates/gworkflows/src/git/tests.rs::site_row_17_owners_are_machine_scoped`.

**Verification:** the shared P2 commands.

### 2.11 Task, claim and edit-attribution helpers [category: code] (depends: 2.10)
`kind: deliverable`

Targets:
- `crates/gworkflows/src/lib.rs`
- `crates/gworkflows/src/claims.rs`
- `crates/gworkflows/src/claims/state.rs`
- `crates/gworkflows/src/claims/bindings.rs`
- `crates/gworkflows/src/claims/edits.rs`
- `crates/gworkflows/src/claims/tests.rs`
- `crates/gworkflows/src/condition/helpers.rs`
- `crates/gworkflows/tests/claims_corpus.rs`

**Research context:**
- **Python modules.** `src/gobby/workflows/task_claim_state.py` (670:
  `resolve_edit_pair`, `claims_without_edit_target`), `task_tool_bindings.py`
  (333), `task_claim_projection.py` (114), `claimed_task_extra_skills.py` (181:
  `missing_claimed_task_extra_skills`), `dirty_edit_grants.py` (233),
  `session_edit_ledger.py` (313), and the task helpers of
  `condition_helpers.py` (`task_tree_complete` and the other task-backed
  names), all over `TaskQueries` and `SessionQueries`.
- **Site row 15.** `resolve_edit_pair` (before- and after-tool edits,
  `commit_guard`): node read; unanswered degrades to the lexical path under
  the node's checkout row.

Port them as `claims` and register the task-backed helper names in
`condition::helpers`.

**Acceptance:**
- 2.11.1 - Every claim, binding, dirty-grant and edit-ledger case in the 1.1 corpus replays equal. test: `crates/gworkflows/tests/claims_corpus.rs::claim_cases_replay_equal`.
- 2.11.2 - Every task-backed condition helper replays equal, and a seam failure follows the per-rule rule. test: `crates/gworkflows/src/claims/tests.rs::task_helpers_match_python`.
- 2.11.3 - Site row 15: an unanswered node read resolves the edit pair to the lexical path under the node's checkout row. test: `crates/gworkflows/src/claims/tests.rs::site_row_15_edit_pair_degrades_to_lexical_path`.

**Verification:** the shared P2 commands.

### 2.12 Gate helpers: found work, validation, monolith, memory and session targets [category: code] (depends: 2.11)
`kind: deliverable`

Targets:
- `crates/gworkflows/src/lib.rs`
- `crates/gworkflows/src/gates.rs`
- `crates/gworkflows/src/gates/found_work.rs`
- `crates/gworkflows/src/gates/found_work/analysis.rs`
- `crates/gworkflows/src/gates/validation.rs`
- `crates/gworkflows/src/gates/monolith.rs`
- `crates/gworkflows/src/gates/sessions.rs`
- `crates/gworkflows/src/gates/tests.rs`
- `crates/gworkflows/src/condition/helpers.rs`
- `crates/gworkflows/tests/gates_corpus.rs`

**Research context:**
- **Python modules.** `src/gobby/workflows/found_work_gate.py` (890:
  `FoundWorkStopAnalyzer`, `_terminal_failures`) with
  `found_work_dispositions.py` (77); `validation_cover.py` (438);
  `src/gobby/config/validation_detection.py::load_project_validation_detection`;
  `monolith_guard.py` (420: `_projection_for_path`, `projected_monolith_paths`,
  `outstanding_monolith_paths`, `MONOLITH_SOURCE_EXTENSIONS`);
  `memory_review_conditions.py` (129); and `condition_helpers_sessions.py`
  (602: `send_keys_target_in_scope`, `send_message_target_allowed`,
  `spawn_target_allowed`, `network_override_allowed`) over `SessionQueries`.
- **Site rows.** 11: `_projection_for_path` via `projected_monolith_paths`
  (`require-decompose-monolith-before-threshold-write`) reads through the seam;
  unanswered lists the path with the diagnostic. 12:
  `outstanding_monolith_paths` (the three `require-monolith-resolution-*`
  guards) reads batched; unanswered lists each guard path with the diagnostic.
  23: `_terminal_failures` is `no-change`, pinned (file-facts Decision 15).
  24: `load_project_validation_detection` is `no-change`; its only hook-path
  caller is row 23, which discards the result for a node session.

Port them as `gates`. The 890-line found-work module splits into
`found_work.rs` (gate) and `found_work/analysis.rs` (transcript analysis).

**Granularity:** one leaf with eight production files. Each is one gate's
helper set, registered together in `condition::helpers` and tested by one
corpus replay.

**Acceptance:**
- 2.12.1 - Every found-work, validation, monolith, memory-review and session-target case in the 1.1 corpus replays equal. test: `crates/gworkflows/tests/gates_corpus.rs::gate_cases_replay_equal`.
- 2.12.2 - Site row 11: an unanswered projection read lists the path with the diagnostic. test: `crates/gworkflows/src/gates/tests.rs::site_row_11_projection_fails_closed`.
- 2.12.3 - Site row 12: one batched read serves every guard path, and an unanswered read lists each path with the diagnostic. test: `crates/gworkflows/src/gates/tests.rs::site_row_12_outstanding_paths_fail_closed`.
- 2.12.4 - Site row 23: terminal-failure analysis for a node session matches Python unchanged. test: `crates/gworkflows/src/gates/tests.rs::site_row_23_terminal_failures_unchanged`.
- 2.12.5 - Site row 24: a node session's validation-detection result is discarded as Python discards it. test: `crates/gworkflows/src/gates/tests.rs::site_row_24_validation_detection_discarded_for_node`.

**Verification:** the shared P2 commands.

### 2.13 Templates and compiled cache entries [category: code] (depends: 2.12)
`kind: deliverable`

Targets:
- `crates/gworkflows/Cargo.toml`
- `Cargo.lock`
- `crates/gworkflows/src/lib.rs`
- `crates/gworkflows/src/template.rs`
- `crates/gworkflows/src/template/tests.rs`
- `crates/gworkflows/src/definitions/cache.rs`
- `crates/gworkflows/tests/template_corpus.rs`
- `crates/gworkflows/tests/bundled_conformance.rs`

**Research context:**
- `src/gobby/workflows/templates.py::TemplateEngine` (126) renders Jinja with
  the custom filters `regex_search`, `regex_replace` and `shlex_quote`.
  `src/gobby/workflows/engine/templating.py::TemplatingMixin` (373) renders
  rule templates and evaluates conditions.
- Bundled templates use tags `set`, `if`, `elif`, `else` and `for`, filters
  `join`, `truncate`, `list`, `shlex_quote`, `int` and `tojson`, and `.get` and
  `.values` on mappings.
- `minijinja` is not in `Cargo.lock`; `shlex` and `regex` are.

Render with `minijinja` and `minijinja-contrib`'s `pycompat` unknown-method
callback (for `.get` and `.values`), each pinned at its newest release at
least two weeks old when 2.13 starts. Register `shlex_quote` on `shlex`, and
`regex_search` and `regex_replace` on 2.8's pattern compiler.
Undefined-variable and whitespace behavior follow the vectors.

A DR7 cache entry now compiles every condition, pattern and template of its
rows once. Extend `bundled_conformance.rs` so every bundled body becomes a
compiled cache entry (Decision 11).

**Acceptance:**
- 2.13.1 - Every template the 1.1 cases render produces Python's output byte for byte. test: `crates/gworkflows/tests/template_corpus.rs::template_vectors_render_equal`.
- 2.13.2 - The three custom filters match Python on quoting, regex groups and no-match input, and an undefined variable renders as `TemplateEngine` renders it. test: `crates/gworkflows/src/template/tests.rs::filters_and_undefined_match_python`.
- 2.13.3 - Every bundled rule body compiles into a cache entry: conditions parse, patterns compile and templates compile. test: `crates/gworkflows/tests/bundled_conformance.rs::every_bundled_body_compiles_into_a_cache_entry`.

**Verification:** the shared P2 commands.

### 2.14 Rule pass, effect plan and outcome counters [category: code] (depends: 2.13)
`kind: deliverable`

Targets:
- `crates/gworkflows/src/lib.rs`
- `crates/gworkflows/src/seams.rs`
- `crates/gworkflows/src/seams/memory.rs`
- `crates/gworkflows/src/engine.rs`
- `crates/gworkflows/src/engine/pass.rs`
- `crates/gworkflows/src/engine/response.rs`
- `crates/gworkflows/src/engine/effects.rs`
- `crates/gworkflows/src/engine/outcome.rs`
- `crates/gworkflows/src/engine/tests.rs`
- `crates/gworkflows/tests/rule_pass_corpus.rs`

**Research context:**
- **Python's pass.** `src/gobby/workflows/engine/evaluation.py` (967:
  `EvaluationMixin._run_rule_loop_pass`, `_assemble_response`),
  `effects.py` (506: `EffectsMixin._apply_effect`, `_apply_set_variable`),
  `event_utils.py` (172), `block_batching.py` (126),
  `src/gobby/workflows/evaluation_runtime.py` (263) and
  `src/gobby/workflows/block_audit.py` (130). Allow-audit lines go through
  `src/gobby/telemetry/rule_allow_audit.py::record_rule_evaluation`.
- **Aggregate blocking.** Only the first matching block runs its sibling
  effects; the reason follows `format_aggregated_block_reason`.
- **Effects.** All 15 `RuleEffect` types (Debt not to port keeps F2's seven).
  Python bridges `mcp_call` to the daemon loop through `_RuleLoopBridge`.

`engine::pass` takes the event, session facts, variables, project id and a
deadline, and returns a `PassResult` or a `PassError`. A `PassResult` carries
every response field 1.1 lists, the eager variable delta, the staged
`on_receipt` payload in the `_gobby_staged_effects` shape, the ordered effect
log, the audit records in `record_rule_evaluation`'s line schema, and the DR8
outcome. A `PassError` is `DefinitionsUnavailable` or `DeadlineExceeded` (DR5);
a seam failure inside a `when` follows the per-rule rule and does not end the
pass.
- `set_variable` updates the in-pass context, and an `mcp_call` result is
  visible to later rules.
- `seams` gains the effect seams `McpDispatch`, `SkillLoader` and
  `CommandRunner`. 2.15 implements the effects that use them.
- `engine::response` follows `_assemble_response`, including the
  `HANDOFF_TURN_END_PENDING_VARIABLE` allow, `acknowledge_variable` staging,
  and `include_rule_outputs=False` for a hardcoded override with no rule
  installed. The 967-line evaluation module splits into `pass.rs` and
  `response.rs`.
- The pass persists nothing; 2.19's driver commits the delta.
- `engine::outcome` keeps per-event-type counters behind
  `outcome_snapshot()` (DR8). No latency budget is tested.

**Granularity:** one leaf with eight production files and five acceptance
items. The pass, effect plan and counters share one state machine, the rule
loop, which only the corpus replay tests whole.

**Acceptance:**
- 2.14.1 - Every 1.1 case without step, lifecycle or observer behavior replays equal to `expect`: response fields, delta, staged payload, effect log, audit records and outcome. test: `crates/gworkflows/tests/rule_pass_corpus.rs::rule_loop_cases_replay_equal`.
- 2.14.2 - In aggregate blocking only the first matching block runs sibling effects, and the reason matches `format_aggregated_block_reason`. test: `crates/gworkflows/tests/rule_pass_corpus.rs::aggregate_block_runs_first_block_effects_only`.
- 2.14.3 - A seam failure inside a `when` follows the per-rule rule, a failed definitions read returns `DefinitionsUnavailable` with no delta, and deadline expiry returns `DeadlineExceeded` and commits nothing. test: `crates/gworkflows/tests/rule_pass_corpus.rs::failures_follow_dr5`.
- 2.14.4 - Each of F2's seven response-field effects sets its field as Python does. test: `crates/gworkflows/tests/rule_pass_corpus.rs::response_field_effects_match_python`.
- 2.14.5 - Each pass is counted once as `zero_match`, `matched_allow`, `mutating` or `block` under its event type. test: `crates/gworkflows/src/engine/tests.rs::outcome_counters_classify_each_pass`.

**Verification:** the shared P2 commands.

### 2.15 External effects [category: code] (depends: 2.14)
`kind: deliverable`

Targets:
- `crates/gworkflows/src/lib.rs`
- `crates/gworkflows/src/engine.rs`
- `crates/gworkflows/src/engine/external.rs`
- `crates/gworkflows/src/engine/external/run_command.rs`
- `crates/gworkflows/src/engine/external/proxy.rs`
- `crates/gworkflows/src/engine/external/injections.rs`
- `crates/gworkflows/src/engine/external/seats.rs`
- `crates/gworkflows/src/engine/external/tests.rs`
- `crates/gworkflows/tests/external_effects_corpus.rs`

**Research context:**
- **Python modules** in `src/gobby/workflows/engine/`: `run_command.py` (397)
  and `run_command_effects.py` (417: `RunCommandEffectsMixin._apply_run_command`,
  fail-open); `proxy_hooks.py` (438: `ProxyHooksMixin._run_proxy_hooks`,
  `_run_rtk_proxy`, 2 s default timeout, 64 KiB output cap, provider
  `supports_permission_neutral_rewrite`); `mcp_injections.py` (434);
  `seat_messages.py` (380); `seat_transitions.py` (426);
  `skill_load_guidance.py` (116); `delivery_formatting.py` (108);
  `injection_tracking.py` (166); `idle_declaration.py` (132).
- **Site row 21.** `_run_rtk_proxy` (`rtk-command-rewrite`) skips a node event;
  the command runs as written.
- **Site row 22.** `_apply_run_command` (`impeccable/design-detector`) skips a
  node event: advisory, documented fail-open.

Port them under `engine::external` over 2.14's effect seams. Provider
capabilities come in as a pass input from the adapter (4.1), so the crate does
not depend on `gobby-ingress`.

**Granularity:** one leaf with seven production files. Each is one effect
family dispatched from the same effect plan; the corpus replays them
together.

**Acceptance:**
- 2.15.1 - Every `run_command`, `proxy_hook`, MCP-injection, seat-message, seat-transition and skill-guidance case in the 1.1 corpus replays equal. test: `crates/gworkflows/tests/external_effects_corpus.rs::external_effect_cases_replay_equal`.
- 2.15.2 - A `run_command` failure, timeout or unparseable output never blocks, and a `proxy_hook` on a provider without permission-neutral rewrite changes nothing. test: `crates/gworkflows/src/engine/external/tests.rs::fail_open_rules_match_python`.
- 2.15.3 - Site row 21: a node event runs no rtk rewrite and keeps its command. test: `crates/gworkflows/src/engine/external/tests.rs::site_row_21_node_skips_rtk`.
- 2.15.4 - Site row 22: a node event runs no `run_command` effect. test: `crates/gworkflows/src/engine/external/tests.rs::site_row_22_node_skips_run_command`.

**Verification:** the shared P2 commands.

### 2.16 Step-workflow and agent tool enforcement [category: code] (depends: 2.15)
`kind: deliverable`

Targets:
- `crates/gworkflows/src/lib.rs`
- `crates/gworkflows/src/engine/pass.rs`
- `crates/gworkflows/src/step.rs`
- `crates/gworkflows/src/step/enforcement.rs`
- `crates/gworkflows/src/step/agent_tools.rs`
- `crates/gworkflows/src/step/completion.rs`
- `crates/gworkflows/src/step/repo.rs`
- `crates/gworkflows/src/step/plan_writes.rs`
- `crates/gworkflows/src/step/tests.rs`
- `crates/gworkflows/tests/step_enforcement_corpus.rs`

**Research context:**
- **Python modules.** `src/gobby/workflows/engine/enforcement_checks.py` (994:
  `EnforcementCheckMixin._check_step_tool_enforcement`,
  `_check_agent_tool_enforcement`), `enforcement_completion.py` (730:
  `_replay_bound_task_commits`), `enforcement_handlers.py` (193),
  `enforcement_audit.py` (82: `_audit_step_tool_call`), `enforcement.py` (60)
  and `step_tool_input.py` (64); `src/gobby/workflows/step_instances.py` (392),
  `step_context.py` (183) and `step_binding.py` (104: `release_lost_binding`,
  #23886); `src/gobby/workflows/enforcement/blocking.py` (743:
  `plan_write_paths_allowed`, `task_mutation_requires_tasks_skill`).
- **Locking.** The step check runs inside
  `db.transaction_immediate(AgentStepInstanceMutation(session_id))`. gcore's
  `LockTarget` (`crates/gcore/src/postgres_pool/transaction.rs`) takes Python's
  key strings; the priority is 875 (DR6).
- **Tables.** `agent_step_instances` and `agent_step_workflows`.
- **Lifecycle in the pass.** `_process_step_after_tool`,
  `_route_step_transition_notice`, the lost-binding rewind, and bound-commit
  replay on entering a step with `link_commit` handlers.
- **Site row 10.** `plan_write_paths_allowed` (plan-mode write gate) reads with
  the node's home and temp roots; unanswered returns `False`.

Port both checks and the step lifecycle as `step`. `step::repo` reads and
updates step instances on the gcore pool under the `AgentStepInstanceMutation`
key. The 994-line check module splits into `enforcement.rs` (step) and
`agent_tools.rs` (agent level). `engine::pass` calls the checks where
`RuleEngine.evaluate` does.

**Granularity:** one leaf with eight production files and five acceptance
items. Step enforcement, completion and the plan-write gate share one step
state machine and one lock.

**Acceptance:**
- 2.16.1 - Every step-tool and agent-tool enforcement case in the corpus replays equal, with step audit records matching `_audit_step_tool_call` field for field. test: `crates/gworkflows/tests/step_enforcement_corpus.rs::step_and_agent_cases_replay_equal`.
- 2.16.2 - Step mutations hold the advisory lock with Python's `AgentStepInstanceMutation` key and priority 875, and a concurrent Python holder blocks them. test: `crates/gworkflows/src/step/tests.rs::step_lock_matches_python_key`.
- 2.16.3 - After-tool transitions, transition notices, the lost-binding rewind and bound-commit replay replay equal. test: `crates/gworkflows/tests/step_enforcement_corpus.rs::step_lifecycle_cases_replay_equal`.
- 2.16.4 - Site row 10: the plan-mode gate reads with the node's home and temp roots, and an unanswered read returns `False`. test: `crates/gworkflows/src/step/tests.rs::site_row_10_plan_writes_fail_closed`.

**Verification:** the shared P2 commands.

### 2.17 Hardcoded pass lifecycle and the rewrite recheck [category: code] (depends: 2.16)
`kind: deliverable`

Targets:
- `crates/gworkflows/src/engine.rs`
- `crates/gworkflows/src/engine/pass.rs`
- `crates/gworkflows/src/engine/lifecycle.rs`
- `crates/gworkflows/src/engine/lifecycle/stop_gates.rs`
- `crates/gworkflows/src/engine/lifecycle/tests.rs`
- `crates/gworkflows/tests/lifecycle_corpus.rs`

**Research context:**
- **Python surface.** `src/gobby/workflows/engine/core.py::RuleEngine.evaluate`
  (953-line module) runs fixed logic around the rule loop:
  - `rules.enforcement_enabled` (false allows every event and runs no rule),
    `rules.aggregate_blocks` and `inject_survey_active`
    (`src/gobby/workflows/session_feedback_survey.py`), with project resolution
    through `_resolve_project_info`;
  - the Grok `POST_COMPACT` source fix and `_COMPACT_TURN_END_BYPASS_PENDING`;
  - the turn-end commit-review default, read through `TaskQueries`;
  - blocked-tool retry tracking (`_last_blocked_tool`,
    `register_blocked_attempt`, `max_consecutive_blocked_tool_attempts`) and
    recovery state (`src/gobby/workflows/engine/blocked_tool_recovery.py`, 288);
  - edit/write pending and `tool_block_pending` after a tool runs
    (`_manage_after_tool_recovery_state`);
  - the `POST_TOOL_BATCH` reset of `stop_attempts` and `_block_reasons_shown`;
  - auto-discover-servers on the first prompt (`servers_listed`), an `_proxy`
    self-call through `McpDispatch`;
  - the turn-end `stop_attempts` increment, which durable waits do not
    consume;
  - the `force_allow_stop`, `tool_block_pending` and `edit_write_pending` stop
    gates, where a claimed task suppresses `force_allow_stop`;
  - `_deliver_late_mcp_injections`, `_flush_pending_terminal_denial` and
    `_finalize_block_response`.
- **F4.** After a before-tool rewrite by `_rewrite_input` or a `proxy_hook`,
  `evaluate` re-runs agent tool enforcement, step tool enforcement and
  `_run_rule_loop(..., block_effects_only=True)` against the rewritten input,
  then delivers late MCP injections only on allow.

Port every item, and the F4 recheck, as `engine::lifecycle`, called from
`engine::pass` where `evaluate` calls each. The caller passes the three
config values and the project id and path as pass inputs. Each item is live
behavior that bundled rules and seat workflows rely on, so none is dropped.

**Acceptance:**
- 2.17.1 - Every hardcoded-behavior case in the 1.1 corpus replays equal. test: `crates/gworkflows/tests/lifecycle_corpus.rs::lifecycle_cases_replay_equal`.
- 2.17.2 - With enforcement disabled every event allows and no rule runs, and with aggregate blocking off the pass stops at the first block except on an interrupted turn end. test: `crates/gworkflows/tests/lifecycle_corpus.rs::config_switches_match_python`.
- 2.17.3 - Stop gates, attempt counters and the batch reset match Python, including a claimed task suppressing `force_allow_stop`, and repeated blocked calls reach `max_consecutive_blocked_tool_attempts` as Python counts them. test: `crates/gworkflows/tests/lifecycle_corpus.rs::stop_gates_and_counters_match_python`.
- 2.17.4 - F4: a rewritten input is rechecked by both tool enforcements and block effects only, and non-block effects run once. test: `crates/gworkflows/tests/lifecycle_corpus.rs::rewrite_recheck_runs_block_effects_only`.

**Verification:** the shared P2 commands.

### 2.18 Observers [category: code] (depends: 2.17)
`kind: deliverable`

Targets:
- `crates/gworkflows/src/lib.rs`
- `crates/gworkflows/src/observers.rs`
- `crates/gworkflows/src/observers/plan_mode.rs`
- `crates/gworkflows/src/observers/context_usage.rs`
- `crates/gworkflows/src/observers/mcp.rs`
- `crates/gworkflows/src/observers/commits.rs`
- `crates/gworkflows/src/observers/tests.rs`
- `crates/gworkflows/tests/observers_corpus.rs`

**Research context:**
- **Python modules.** `src/gobby/workflows/observers.py` (493),
  `observer_plan_mode.py` (470), `observer_context_usage.py` (505),
  `observer_mcp.py` (317), `observer_dispatch.py` (177), `observer_commits.py`
  (126), `observer_utils.py` (95) and `turn_interrupt_observer.py` (34).
  `src/gobby/workflows/hooks.py::WorkflowHookHandler._run_observers` runs them.
- **Site row 26.** The plan-mode observer's Codex transcript read is
  `no-change`: a transcript is not the working tree.

Port them as `observers`, dispatch and utilities included in `observers.rs`.
Transcript reads go through `TranscriptQueries`.

**Granularity:** one leaf with six production files, one per observer group,
run by one dispatcher and tested by one corpus replay.

**Acceptance:**
- 2.18.1 - Every observer case in the 1.1 corpus replays equal. test: `crates/gworkflows/tests/observers_corpus.rs::observer_cases_replay_equal`.
- 2.18.2 - Site row 26: the plan-mode observer reads the Codex transcript for a node session as Python does. test: `crates/gworkflows/src/observers/tests.rs::site_row_26_transcript_read_unchanged`.

**Verification:** the shared P2 commands.

### 2.19 Pass driver: per-session order, variables and project path [category: code] (depends: 2.18)
`kind: deliverable`

Targets:
- `crates/gworkflows/src/lib.rs`
- `crates/gworkflows/src/driver.rs`
- `crates/gworkflows/src/driver/evaluate.rs`
- `crates/gworkflows/src/driver/variables.rs`
- `crates/gworkflows/src/driver/variables/merge.rs`
- `crates/gworkflows/src/driver/tool_context.rs`
- `crates/gworkflows/src/driver/disposition.rs`
- `crates/gworkflows/src/driver/tests.rs`
- `crates/gworkflows/tests/driver_corpus.rs`

**Research context:**
- **Python driver.** `src/gobby/workflows/hooks.py::WorkflowHookHandler`
  (909-line module): `_reserve_eval_lock` and `_release_eval_lock` (per-session
  `_EvalLockState`), `_resolve_project_path`, `_handle_cancelled`,
  `_run_observers`, `_evaluate_rules` (528 lines), `evaluate_async`,
  `evaluate` and `handle`.
- **Variables.** `src/gobby/workflows/state_manager.py::SessionVariableManager`
  (879-line module: load, `merge_variables` under `SessionVariableMutation`,
  key-scoped since #23808), `reserved_variables.py` (69) and
  `variable_defaults.py` (54); table `session_variables` and
  `session_variable_defaults`.
- **Other.** `tool_context.py` (321), `delivery_disposition.py` (287) and
  `hook_project_path.py` (111).
- **Site row 7.** `_resolve_project_path` takes a node event's project path
  from its checkout row, never from a hub path resolve.

`driver` is the engine's public entry point. It serializes passes per session
in FIFO order (DR6), loads variables at one revision (DR7), runs observers and
the pass, and commits the delta under the `SessionVariableMutation` key with
priority 950, all through a `SessionVariables` seam whose gcore-pool
implementation lives here. The 528-line `_evaluate_rules` splits into
`evaluate.rs`; the variable manager splits into `variables.rs` and
`variables/merge.rs`. #24884's gateway, 7.14's pass endpoint and the ingress
pipeline (5.2) call `driver`.

**Granularity:** one leaf with eight production files and five acceptance
items. Ordering, variable load and merge, and disposition form one pass
lifecycle with one lock.

**Acceptance:**
- 2.19.1 - Every 1.1 case replays equal through `driver`, end to end. test: `crates/gworkflows/tests/driver_corpus.rs::every_case_replays_equal_through_the_driver`.
- 2.19.2 - Two concurrent passes of one session run in arrival order, and passes of different sessions run concurrently. test: `crates/gworkflows/src/driver/tests.rs::passes_serialize_per_session_in_fifo_order`.
- 2.19.3 - The delta merge holds the `SessionVariableMutation` key with priority 950, merges changed keys only, and a concurrent Python holder blocks it. test: `crates/gworkflows/src/driver/tests.rs::variable_merge_matches_python_lock_and_keys`.
- 2.19.4 - A cancelled pass commits nothing and returns Python's cancelled response. test: `crates/gworkflows/src/driver/tests.rs::cancelled_pass_commits_nothing`.
- 2.19.5 - Site row 7: a node event's project path comes from its checkout row. test: `crates/gworkflows/src/driver/tests.rs::site_row_7_project_path_from_checkout_row`.

**Verification:** the shared P2 commands.

## P3: Ingress envelope layer in `gobby-ingress`
`kind: framing`

Move the ledger into gcore, create the `hooks` family, and port request
validation, claim and replay, receipts and the hub inbox drain. The envelope
layer takes its execution step as an async closure, so P3 tests run without
adapters or the pipeline; 5.12 passes the real execution.

### 3.1 Shared gcore seams: hook ledger and pre-proxy compare [category: code] (depends: 2.19)
`kind: deliverable`

Targets:
- `Cargo.lock`
- `crates/gcore/Cargo.toml`
- `crates/gcore/src/lib.rs::*` — scope-reason: declares the feature-gated hook_ledger module
- `crates/gcore/src/hook_ledger.rs`
- `crates/gcore/src/hook_ledger/tests.rs`
- `crates/gcore/src/route_family.rs::RouteFamily`
- `crates/gdaemon/Cargo.toml`
- `crates/gdaemon/src/nodes/hooks/ledger.rs::*` — operation: delete — scope-reason: the whole module moves to crates/gcore/src/hook_ledger.rs
- `crates/gdaemon/src/nodes/hooks/mod.rs`
- `crates/gdaemon/src/nodes/hooks/ingress.rs::*` — scope-reason: import path changes only
- `crates/gdaemon/src/nodes/hooks/drain.rs::*` — scope-reason: import path changes only
- `crates/gdaemon/src/nodes/hooks/drain/tests.rs::*` — scope-reason: import path changes only
- `crates/gdaemon/src/nodes/hooks/receipts.rs::*` — scope-reason: import path changes only
- `crates/gdaemon/src/state.rs::NodeServices`
- `crates/gdaemon/tests/node_hooks.rs::*` — scope-reason: import path changes only
- `crates/gdaemon/src/front_door/routes.rs::*` — scope-reason: compare awaits the new seam and gains its test
- `crates/gcode/Cargo.toml`
- `crates/ghook/Cargo.toml`
- `crates/gclient/Cargo.toml`
- `crates/gmcp/Cargo.toml`
- `src/gobby/install/version_pins.py::*` — scope-reason: keep managed release pins coherent with the binaries this deliverable changes

**Research context:**
- **Ledger.** `crates/gdaemon/src/nodes/hooks/ledger.rs` (736 lines, a
  Python-compatible filesystem ledger) exports `Ledger` (`new`, `claim`,
  `marker`, `renew`, `finalize`, `release`, `release_previous_process`,
  `is_processed`, `is_active`, `clear_stale`, `mark_acknowledged`,
  `mark_processed`, `terminal_response`, `bump_stop_epoch`, `read_stop_epoch`,
  `prune`, `is_fresh`), `DirectoryPruneResult`,
  `envelope_timestamp_ms_from_inbox_path` and the constants
  `ENVELOPE_REPLAY_GRACE_SECONDS` (120), `ENVELOPE_PROCESSING_LEASE_TTL_SECONDS`,
  `PROCESSED_MARKER_RETENTION_SECONDS`, `PROCESSED_MARKER_PRUNE_MAX_ENTRIES`
  and `STOP_REPLAY_EPOCH_FILENAME`. It depends on `serde_json`, `sha2`, `time`
  and `uuid`; gcore already has all but `time`.
- **Ledger users.** `crates/gdaemon/src/nodes/hooks/{ingress,drain,receipts}.rs`,
  `drain/tests.rs`, `crates/gdaemon/src/state.rs::NodeServices` (field
  `hook_ledger`, built from `home.join("hooks/inbox")`) and
  `crates/gdaemon/tests/node_hooks.rs`. The Python counterpart is
  `src/gobby/hooks/envelope_dedupe.py` (`claim_envelope_processing`,
  `envelope_terminal_response`, `bump_stop_replay_epoch`,
  `read_stop_replay_epoch`, `prune_processed_envelope_markers`).
- **Compare.** `crates/gdaemon/src/front_door/routes.rs::compare` runs the
  native and proxied legs concurrently for GET and HEAD. For other methods it
  proxies once and then calls `RouteFamily::check_proxied`
  (`crates/gcore/src/route_family.rs`), logging to `front-door-compare.jsonl`
  through `crates/gdaemon/src/front_door/compare.rs`.
- **Release carriers.** gcore 0.10.21 is a path dependency of gcode, gdaemon,
  ghook, gclient, gmcp, gruntime and gterminals.

Move the ledger verbatim into `gcore::hook_ledger` behind feature
`hook-ledger` (adding optional `time`), with its inline test moved to
`hook_ledger/tests.rs`. Then make the STOP epoch per machine:
`bump_stop_epoch` and `read_stop_epoch` take an optional machine id.
`None` keeps `STOP_REPLAY_EPOCH_FILENAME`, which the node and the hub's own
machine use; `Some(id)` uses `.stop_replay_epoch.<machine_id>`.
`terminal_response` takes the envelope's machine id and checks that machine's
epoch. gdaemon enables the feature and imports from gcore.

Add `RouteFamily::prepare_proxied(&self, parts, request) -> Pin<Box<dyn
Future<Output = ()> + Send>>` with a no-op default. `compare` awaits it before
the proxied leg for non-GET/HEAD requests. No family overrides it until 7.13.

Bump gcore and every released dependent (gcode, gdaemon, ghook, gclient,
gmcp) by 0.0.1, update `Cargo.lock`, and keep `MANAGED_BIN_VERSION_PINS` in
step.

**Granularity:** one leaf with two outcomes, the ledger move and the compare
seam. Both change gcore, and one leaf keeps them to one release of every gcore
dependent.

**Acceptance:**
- 3.1.1 - The node arm's existing ledger, drain and ingress tests pass unchanged against `gcore::hook_ledger`, including the session-start epoch bump. test: `crates/gdaemon/tests/node_hooks.rs::ingress_session_start_bumps_epoch`.
- 3.1.2 - A per-machine epoch bump bounds STOP replays for that machine only, and `None` keeps the single-file layout Python reads. test: `crates/gcore/src/hook_ledger/tests.rs::stop_epoch_is_per_machine`.
- 3.1.3 - Compare awaits `prepare_proxied` before the proxied leg of a non-GET request, and the default does nothing. test: `crates/gdaemon/src/front_door/routes.rs::compare_awaits_prepare_before_proxied_leg`.
- 3.1.4 - gcore and its released dependents carry 0.0.1 bumps, and `Cargo.lock` and the managed pins agree. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

**Verification:** planned:
`cargo nextest run -p gobby-core --features hook-ledger -E 'test(hook_ledger)'`,
`cargo nextest run -p gobby-daemon -E 'test(node_hooks) | test(compare)'`,
`cargo clippy -p gobby-core -p gobby-daemon --all-targets -- -D warnings`.

### 3.2 `gobby-ingress` crate, `hooks` family and request validation [category: code] (depends: 3.1)
`kind: deliverable`

Targets:
- `Cargo.toml`
- `Cargo.lock`
- `crates/gingress/Cargo.toml`
- `crates/gingress/src/lib.rs`
- `crates/gingress/src/family.rs`
- `crates/gingress/src/execute.rs`
- `crates/gingress/src/execute/request.rs`
- `crates/gingress/src/execute/tests.rs`

**Research context:**
- **Route.** `src/gobby/servers/routes/mcp/hooks.py::create_hooks_router`
  (862 lines) defines `execute_hook`. Before any claim it parses JSON (an
  unparseable body answers the graceful allow `{"continue": true, "decision":
  "approve"}`), binds the platform session header
  (`_run_bound_platform_session`), requires `hook_type` and `source` (400),
  answers 503 `{"detail":"HookManager not initialized"}` when the pipeline is
  not ready, consumes pending delivery receipts (3.4), rejects an unsupported
  `source` (400, `SUPPORTED_HOOK_SOURCES`), answers below the capability floor
  with the graceful response (`envelope_has_hook_response_capability`,
  `src/gobby/hooks/runtime_compat.py`), and returns 403
  `{"code":"hook_machine_mismatch"}` from `_envelope_machine_mismatch`.
- **Contract.** `docs/contracts/hook-ingress.md` and the 1.2 corpus.
- **Family seam.** `crates/gcore/src/route_family.rs::RouteFamily` (`name`,
  `prefixes`, `router`, `default_backend` defaulting to `Proxy`, `claims`,
  `check_proxied`, 3.1's `prepare_proxied`). Pattern:
  `crates/gruntime/Cargo.toml`.
- **Door.** `crates/gdaemon/src/front_door/mod.rs` answers node-mode
  `/api/hooks/execute` in `Door::Node` before any routing table, so the
  `hooks` family only serves `Door::Backend` (standalone and hub).

Add `crates/gingress` (package `gobby-ingress`, `publish = false`) to the root
members. `family::HooksFamily` names `hooks`, claims the five prefixes in
Decision 3, defaults to `Proxy`, and routes `POST /api/hooks/execute` to
`execute`. `execute::request` ports every pre-claim step above in Python's
order; the pipeline-readiness check reads a readiness flag the caller passes.
No binary links the crate until 7.1.

**Acceptance:**
- 3.2.1 - The family claims exactly the five prefixes, defaults to proxy, and leaves `/api/nodes/checkouts`, `/api/nodes/report` and `/api/nodes/runs` unclaimed. test: `crates/gingress/src/execute/tests.rs::family_claims_its_prefixes_only`.
- 3.2.2 - Every 1.2 and HTTP-corpus case that ends before a claim (bad schema, below floor, unsupported source, missing fields, 403, HookManager not initialized) replays equal. test: `crates/gingress/src/execute/tests.rs::pre_claim_cases_replay_equal`.
- 3.2.3 - The machine check runs after the floor check and before any claim, and accepts the verified front-door identity's machine or, without one, the hub's machine. test: `crates/gingress/src/execute/tests.rs::machine_binding_precedes_claim`.

**Verification:** planned:
`cargo nextest run -p gobby-ingress` and
`cargo clippy -p gobby-ingress --all-targets -- -D warnings`.

### 3.3 Claim, replay, conflicts and the STOP grace [category: code] (depends: 3.2)
`kind: deliverable`

Targets:
- `crates/gingress/src/lib.rs`
- `crates/gingress/src/execute.rs`
- `crates/gingress/src/execute/envelope.rs`
- `crates/gingress/src/execute/tests.rs`
- `crates/gingress/tests/envelope_corpus.rs`

**Research context:**
- **Python flow** in `execute_hook`: with `X-Gobby-Envelope-Id`, claim
  (`claim_envelope_processing`); if not claimed, return a stored terminal
  response with HTTP 200 (`envelope_terminal_response`); otherwise read the
  marker (`read_envelope_marker`), reclaim a vanished or stale marker
  (`clear_stale_envelope_processing_marker`), or answer 409 `processing`,
  `processed` or `malformed_marker`. A claimed envelope renews its lease while
  executing and releases the claim on every retryable failure.
- **STOP grace.** STOP and `turn_end` block responses replay only within
  `ENVELOPE_REPLAY_GRACE_SECONDS` and at or after the machine's epoch (3.1).
- **Node parity.** `crates/gdaemon/src/nodes/hooks/ingress.rs::claim_or_replay`
  and `Renewal` do the same on a node.

`execute::envelope` wraps the execution closure: claim or replay, lease
renewal for the closure's lifetime, finalize on success, release on a
retryable failure. It reads the epoch of the envelope's `machine_id`.

**Acceptance:**
- 3.3.1 - Every 1.2 sequence case for duplicates, the three 409 bodies and stale-marker reclaim replays equal. test: `crates/gingress/tests/envelope_corpus.rs::duplicate_and_conflict_cases_replay_equal`.
- 3.3.2 - STOP and `turn_end` blocks replay inside the grace, and re-execute after the grace or after that machine's epoch bump, while another machine's bump changes nothing. test: `crates/gingress/tests/envelope_corpus.rs::stop_grace_is_bounded_per_machine`.
- 3.3.3 - The lease renews while the closure runs, and a retryable failure releases the claim so the next delivery executes. test: `crates/gingress/src/execute/tests.rs::lease_renews_and_retry_releases`.

**Verification:** planned:
`cargo nextest run -p gobby-ingress` and
`cargo clippy -p gobby-ingress --all-targets -- -D warnings`.

### 3.4 Delivery receipts [category: code] (depends: 3.3)
`kind: deliverable`

Targets:
- `crates/gingress/src/lib.rs`
- `crates/gingress/src/receipts.rs`
- `crates/gingress/src/receipts/replay.rs`
- `crates/gingress/src/receipts/staging.rs`
- `crates/gingress/src/receipts/tests.rs`
- `crates/gingress/src/execute.rs`
- `crates/gingress/tests/envelope_corpus.rs`

**Research context:**
- **Python modules.** `src/gobby/hooks/receipt_redelivery.py` (203:
  `DELIVERY_RECEIPT_FIELD`, `attach_delivery_receipt`,
  `release_receipt_for_response`, `ReceiptGuardedJSONResponse`,
  `receipt_guarded_response`), `replay_decision.py` (110:
  `acknowledge_and_apply_receipt`, `terminalize_below_floor_receipts`,
  `decide_replay`), `replay_fence.py` (98: `defer_live_hook`,
  `archive_superseded_hook`), `receipt_effects.py` (241:
  `STAGED_EFFECTS_FIELD`, `merge_staged_payloads`, `apply_acknowledged_receipt`
  and the worker staging scope) and
  `src/gobby/hooks/inbox.py::consume_pending_delivery_receipts`.
- **Contract.** A receipt needs a database and a resolved session. A failed
  write of a receipted response releases the receipt for redelivery. ghook
  replaces the envelope file with a `delivery-receipt` acknowledgement
  (`docs/contracts/hook-ingress.md`, "Receipt acknowledgement file").
- **Storage.** Receipts and staged effects are session rows; reads and writes
  go through 5.1's `Sessions` seam, scripted here.

Port them as `receipts`: attach a receipt to a response carrying staged
`on_receipt` effects, release it when the response body fails to write,
consume pending acknowledgement files before execution, and apply an
acknowledged receipt's staged effects once.

**Acceptance:**
- 3.4.1 - The 1.2 receipt round trip replays equal: the response carries `_gobby_delivery_receipt`, and the acknowledgement applies the staged effects once. test: `crates/gingress/tests/envelope_corpus.rs::receipt_round_trip_replays_equal`.
- 3.4.2 - A response whose body fails to write releases its receipt for redelivery. test: `crates/gingress/src/receipts/tests.rs::failed_write_releases_receipt`.
- 3.4.3 - Replay decisions, below-floor terminalization and the replay fence match Python on the recorded cases. test: `crates/gingress/src/receipts/tests.rs::replay_decisions_match_python`.

**Verification:** planned:
`cargo nextest run -p gobby-ingress` and
`cargo clippy -p gobby-ingress --all-targets -- -D warnings`.

### 3.5 Hub inbox drain and quarantine [category: code] (depends: 3.4)
`kind: deliverable`

Targets:
- `crates/gingress/src/lib.rs`
- `crates/gingress/src/inbox.rs`
- `crates/gingress/src/inbox/files.rs`
- `crates/gingress/src/inbox/maintenance.rs`
- `crates/gingress/src/inbox/tests.rs`

**Research context:**
- **Python drain.** `src/gobby/hooks/inbox.py` (819: `drain_hook_inbox_once`,
  `drain_hook_inbox_barrier`, `HookInboxBarrierResult`, and `_post_envelope`,
  which POSTs each envelope to the front-door `/api/hooks/execute` with the
  operator token), `inbox_envelopes.py` (208: `claim_inbox_file`,
  `restore_inbox_claim`, `quarantine_file`, `load_envelope`),
  `inbox_maintenance.py` (162: `prune_hook_inbox`,
  `prune_orphaned_inbox_temp_files`, `drain_hook_inbox_loop`),
  `inbox_lifecycle.py` (39) and `quarantine_retention.py` (166).
- **Quarantine rule.** A spooled envelope answered 403 `hook_machine_mismatch`
  is quarantined and never replayed.
- **Barrier user.** `src/gobby/runner_hook_replay.py` calls
  `drain_hook_inbox_barrier` before agent restart classification; 7.15 keeps
  that barrier working.

Port them as `inbox`. The drain claims each inbox file atomically, passes the
envelope's headers and body to a dispatch closure the caller supplies, settles
or quarantines the file by the response, and prunes processed markers and
quarantine by age. It is built here; 7.15 starts it with a closure over the
front door's route table.

**Acceptance:**
- 3.5.1 - The drain executes each spooled envelope once through the dispatch closure, settles it by response, and quarantines a 403 without replay. test: `crates/gingress/src/inbox/tests.rs::drain_settles_and_quarantines_like_python`.
- 3.5.2 - A file another drain has claimed is skipped, and a crashed claim is restored. test: `crates/gingress/src/inbox/tests.rs::inbox_claims_are_exclusive_and_restorable`.
- 3.5.3 - Inbox, temp-file and quarantine pruning follow Python's ages and bounds. test: `crates/gingress/src/inbox/tests.rs::pruning_matches_python`.

**Verification:** planned:
`cargo nextest run -p gobby-ingress` and
`cargo clippy -p gobby-ingress --all-targets -- -D warnings`.

## P4: CLI adapters
`kind: framing`

Port the hook payload types, provider capabilities and the Claude, Codex,
Grok, Droid and AGY adapters, replaying the 1.3 corpus. Adapters normalize
through `gobby-workflows`' `normalize` and exchange 2.1's `event` types.

Shared rules for every P4 deliverable:
- An adapter reads a provider spill or transcript file only for a hub-local
  delivery; a node delivery takes the no-file branch (Site rows 20, 28-30).
- Helpers an adapter imports from outside `src/gobby/adapters/` are ported into
  that adapter's module: they are small, local-file parsers used only there.
- Verification for every P4 deliverable: `cargo nextest run -p gobby-ingress`
  and `cargo clippy -p gobby-ingress --all-targets -- -D warnings`.

### 4.1 Payload types, capabilities and the adapter trait [category: code] (depends: 3.5)
`kind: deliverable`

Targets:
- `crates/gingress/Cargo.toml`
- `Cargo.lock`
- `crates/gingress/src/lib.rs`
- `crates/gingress/src/adapters.rs`
- `crates/gingress/src/adapters/hook_types.rs`
- `crates/gingress/src/adapters/capabilities.rs`
- `crates/gingress/src/adapters/degradation.rs`
- `crates/gingress/src/adapters/tests.rs`

**Research context:**
- `src/gobby/hooks/hook_types/` (about 1,500 lines: `enums.py`, `mappings.py`,
  `session.py`, `tool.py`, `lifecycle.py`, `agent.py`, `interactive.py`,
  `environment.py`, `base.py`) types each provider's payload.
- `src/gobby/adapters/base.py::BaseAdapter` (`translate_to_hook_event`,
  `translate_from_hook_response`, `handle_native`, `_hook_event_timestamp`,
  `_copy_platform_session_metadata`), with `normalize_adapter_response_reason`,
  `build_first_hook_session_metadata_lines` and
  `ADAPTER_EMPTY_BLOCK_REASON_SENTINEL`.
- `src/gobby/adapters/capabilities.py` (575: `get_provider_capabilities`,
  `supports_permission_neutral_rewrite` and the context channels),
  `degradation.py` (277) and `src/gobby/hooks/context_limits.py` (65).
- **Selection.** `source` alone selects the adapter (memory a39d0411, #17173).

Add `gobby-workflows` to the crate's dependencies. Port the payload types,
capabilities, degradation and context limits, and define the `Adapter` trait
with the three `BaseAdapter` operations plus `select(source)`. 2.15's
`proxy_hook` reads capabilities as a pass input from this module.

**Acceptance:**
- 4.1.1 - Every provider capability and degradation rule matches `get_provider_capabilities` and `degradation.py` for all five sources. test: `crates/gingress/src/adapters/tests.rs::capabilities_match_python`.
- 4.1.2 - `select` maps each supported `source` to its adapter and nothing else; inner payload field names never change the choice. test: `crates/gingress/src/adapters/tests.rs::source_alone_selects_the_adapter`.
- 4.1.3 - Shared response-reason normalization, the empty-block sentinel and first-hook session metadata lines match Python. test: `crates/gingress/src/adapters/tests.rs::base_helpers_match_python`.

**Verification:** the shared P4 commands.

### 4.2 Claude Code adapter [category: code] (depends: 4.1)
`kind: deliverable`

Targets:
- `crates/gingress/src/adapters.rs`
- `crates/gingress/src/adapters/claude.rs`
- `crates/gingress/src/adapters/claude/contract.rs`
- `crates/gingress/tests/adapters_corpus.rs`

**Research context:** `src/gobby/adapters/claude_code.py` (546:
`ClaudeCodeAdapter`, `is_action_first_reason`, `_saved_mcp_result`) and
`claude_contract.py` (286: `ClaudeDecisionStyle` and the hook contract).
- **Site row 20.** `_saved_mcp_result` reads a provider spill file; a node
  delivery skips the read and takes the unreadable-file branch.

**Acceptance:**
- 4.2.1 - Every Claude case in the 1.3 corpus replays equal, inbound and outbound. test: `crates/gingress/tests/adapters_corpus.rs::claude_cases_replay_equal`.
- 4.2.2 - Site row 20 (Claude): a node delivery reads no spill file and takes the unreadable-file branch. test: `crates/gingress/tests/adapters_corpus.rs::site_row_20_claude_node_skips_spill`.

**Verification:** the shared P4 commands.

### 4.3 Codex hooks adapter [category: code] (depends: 4.2)
`kind: deliverable`

Targets:
- `crates/gingress/src/adapters.rs`
- `crates/gingress/src/adapters/codex.rs`
- `crates/gingress/src/adapters/codex/items.rs`
- `crates/gingress/tests/adapters_corpus.rs`

**Research context:** `src/gobby/adapters/codex_impl/hooks_adapter.py` (420:
`CodexHooksAdapter`, `_command_execution_result`), `lifecycle_events.py` (96)
and `shared.py` (26), plus
`src/gobby/sessions/transcripts/codex_items.py::normalize_command_execution`
(106-line module) and
`src/gobby/sessions/reasoning_effort.py::observed_reasoning_effort` (41). The
Codex app-server adapter is Python-owned (Decision 4) and not ported.
- **Site row 20.** `_command_execution_result` reads a transcript or spill
  file; a node delivery skips it.

**Acceptance:**
- 4.3.1 - Every Codex case in the 1.3 corpus replays equal, inbound and outbound. test: `crates/gingress/tests/adapters_corpus.rs::codex_cases_replay_equal`.
- 4.3.2 - Site row 20 (Codex): a node delivery reads no command-execution file and takes the unreadable-file branch. test: `crates/gingress/tests/adapters_corpus.rs::site_row_20_codex_node_skips_spill`.

**Verification:** the shared P4 commands.

### 4.4 ACP hook base and Grok adapter [category: code] (depends: 4.3)
`kind: deliverable`

Targets:
- `crates/gingress/src/adapters.rs`
- `crates/gingress/src/adapters/acp.rs`
- `crates/gingress/src/adapters/grok.rs`
- `crates/gingress/tests/adapters_corpus.rs`

**Research context:** `src/gobby/adapters/acp_hook_adapter.py` (437:
`ACPHookAdapter`, base of `GrokAdapter` and `AgyAdapter`) with
`acp_tool_names.py` (53: `normalize_acp_tool_name`), and
`src/gobby/adapters/grok.py` (298: `GrokAdapter`, `_spilled_mcp_output`).
In-process ACP sessions stay Python-owned (Decision 4); only the hook
translation is ported.
- **Site row 20.** `_spilled_mcp_output` reads a spill file; a node delivery
  skips it.

**Acceptance:**
- 4.4.1 - Every Grok case in the 1.3 corpus replays equal, inbound and outbound. test: `crates/gingress/tests/adapters_corpus.rs::grok_cases_replay_equal`.
- 4.4.2 - Site row 20 (Grok): a node delivery reads no spill file and takes the unreadable-file branch. test: `crates/gingress/tests/adapters_corpus.rs::site_row_20_grok_node_skips_spill`.

**Verification:** the shared P4 commands.

### 4.5 Droid adapter [category: code] (depends: 4.4)
`kind: deliverable`

Targets:
- `crates/gingress/src/adapters.rs`
- `crates/gingress/src/adapters/droid.rs`
- `crates/gingress/src/adapters/droid/transcript.rs`
- `crates/gingress/tests/adapters_corpus.rs`

**Research context:** `src/gobby/adapters/droid.py` (453: `DroidAdapter`,
`_normalize_router_help`) and `droid_contract.py` (118), plus the functions it
imports from `src/gobby/sessions/transcripts/droid.py`
(`droid_transcript_has_tool_id`,
`droid_transcript_confirms_turn_cancellation`).
- **Site row 28.** The `Notification` wait branch (`permission_prompt`,
  `ask_user_question` and kin) reads the transcript; a node delivery takes the
  no-file result, so `wait_kind` stays unset.
- **Site row 29.** The idle_prompt cancel branch never converts a node
  delivery to `INTERRUPT` from the transcript.

**Acceptance:**
- 4.5.1 - Every Droid case in the 1.3 corpus replays equal, inbound and outbound. test: `crates/gingress/tests/adapters_corpus.rs::droid_cases_replay_equal`.
- 4.5.2 - Site row 28: a node wait notification leaves `wait_kind` unset. test: `crates/gingress/tests/adapters_corpus.rs::site_row_28_droid_node_wait_unset`.
- 4.5.3 - Site row 29: a node idle_prompt with a cancel reason does not become `INTERRUPT`. test: `crates/gingress/tests/adapters_corpus.rs::site_row_29_droid_node_no_interrupt`.

**Verification:** the shared P4 commands.

### 4.6 AGY adapter [category: code] (depends: 4.5)
`kind: deliverable`

Targets:
- `crates/gingress/src/adapters.rs`
- `crates/gingress/src/adapters/agy.rs`
- `crates/gingress/src/adapters/agy/contract.rs`
- `crates/gingress/tests/adapters_corpus.rs`

**Research context:** `src/gobby/adapters/agy.py` (296: `AgyAdapter`,
`_approval_denied_reason`) and `agy_contract.py` (244: `AgyHookContract`,
`agy_execution_number`), plus
`src/gobby/sessions/transcripts/agy.py::read_current_user_prompt` and
`src/gobby/skills/capability_routing.py::gobby_help_prefix`.
- **Site row 30.** `BEFORE_AGENT` prompt backfill reads the transcript; a node
  delivery takes no prompt from it.

**Acceptance:**
- 4.6.1 - Every AGY case in the 1.3 corpus replays equal, inbound and outbound. test: `crates/gingress/tests/adapters_corpus.rs::agy_cases_replay_equal`.
- 4.6.2 - Site row 30: a node `BEFORE_AGENT` delivery takes no prompt from the transcript. test: `crates/gingress/tests/adapters_corpus.rs::site_row_30_agy_node_no_backfill`.

**Verification:** the shared P4 commands.

## P5: Event pipeline and handlers
`kind: framing`

Port the `HookManager` pipeline and the event handlers over pipeline seams,
replaying the 1.4 corpus, then compose envelope, adapters and pipeline into
the route's execution step.

Shared rules for every P5 deliverable:
- Every call to another family goes through a 5.1 seam; P7 implements each
  seam over the owning family's native API.
- A Python module over 850 lines is split into two Rust files up front.
- Verification for every P5 deliverable:
  `GOBBY_SCHEMA_TEST_DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test cargo nextest run -p gobby-ingress`
  and `cargo clippy -p gobby-ingress --all-targets -- -D warnings`.

### 5.1 Pipeline seams and scripted implementations [category: code] (depends: 4.6, 1.4)
`kind: deliverable`

Targets:
- `crates/gingress/src/lib.rs`
- `crates/gingress/src/seams.rs`
- `crates/gingress/src/seams/scripted.rs`
- `crates/gingress/src/seams/tests.rs`
- `crates/gingress/tests/pipeline_corpus.rs`

**Research context:** the collaborators 1.4 records
(`src/gobby/hooks/factory.py::HookManagerComponents`,
`src/gobby/hooks/event_handlers/__init__.py::EventHandlers.__init__`), grouped
by owning family:
- **Sessions (#21562):** session rows, lookup and status, platform session
  binding, deferred activation, materialization, clear-run lineage, the
  message processor and transcripts, receipts and staged effects (3.4), and
  node session registration (Decision 8).
- **Tasks (#21560):** task reads and writes, session-task links, commit
  auto-link.
- **Agents (#21564):** agent runs and their lifecycle
  (`src/gobby/hooks/session_coordinator.py::SessionCoordinator`
  `start_agent_run`, `complete_agent_run`, `release_session_worktrees`),
  managed-agent validation, worktrees, progress tracking, attention metadata,
  pending messages and reservations.
- **Terminals (#21565):** terminal manager, runtime registry and composer
  ledger.
- **Skills (#21580):** router skills and skill content.
- **MCP (#21566):** internal and external tool calls (`call_tool`).
- **Config (#21559):** workflow config, rule switches, hook timeouts and
  webhook endpoints.
- **Projects (#21585):** project id from a cwd and checkout rows.
- **Code index (#21586):** index status, session summaries
  (`session_summary_dispatcher.py`) and the edit trigger (#24705).
- **Broadcast:** UI event fan-out (`src/gobby/hooks/broadcaster.py`), served by
  gdaemon's native WS transport (#21558, closed).

Define one trait per group, each call returning `Result<_, SeamUnavailable>`,
and a scripted implementation that replays a 1.4 case's collaborator script
and fails on an unscripted call. The engine's own seams stay in
`gobby-workflows`.

**Acceptance:**
- 5.1.1 - Every collaborator call in the 1.4 corpus maps to exactly one seam method. test: `crates/gingress/src/seams/tests.rs::every_recorded_collaborator_maps_to_a_seam`.
- 5.1.2 - The scripted seams replay a case's script in order and fail on an unscripted or out-of-order call. test: `crates/gingress/src/seams/tests.rs::scripted_seams_enforce_the_script`.

**Verification:** the shared P5 commands.

### 5.2 Pipeline orchestration and project context [category: code] (depends: 5.1)
`kind: deliverable`

Targets:
- `crates/gingress/src/lib.rs`
- `crates/gingress/src/pipeline.rs`
- `crates/gingress/src/pipeline/ordering.rs`
- `crates/gingress/src/pipeline/project.rs`
- `crates/gingress/src/pipeline/tests.rs`
- `crates/gingress/tests/pipeline_corpus.rs`

**Research context:**
- `src/gobby/hooks/hook_manager.py::HookManager._handle_after_daemon_ready`
  and `_complete_response` (823-line module), `hook_manager_dispatch.py`
  (124), `hook_manager_ingress.py` (152), `rule_evaluator.py` (348, the bridge
  to `WorkflowHookHandler`), `event_enrichment.py` (267), `session_binding.py`
  (30: `session_binds_event`), `fifo_lock.py` (61), `effect_deadline.py` (57)
  and `background_tasks.py` (38). The step order is 1.4's Entry list.
- `src/gobby/hooks/project_context.py::resolve_hook_project_context` (342) and
  `project_checkout_ingress.py` (130).
- **Site row 6.** Project resolution for a node event reads the node's
  checkout rows (database only); no row fails closed with the no-project
  error.

`pipeline` runs the steps in Python's order and calls 2.19's `driver` for
rule passes. A rule block short-circuits the handler; blocking webhooks run
after rules (5.11 supplies them).

**Acceptance:**
- 5.2.1 - The step order matches Python on every 1.4 case: session start runs handler, rules, webhooks; other events run lookup, gating, activation, rules, webhooks, handler. test: `crates/gingress/tests/pipeline_corpus.rs::pipeline_order_replays_equal`.
- 5.2.2 - A rule block short-circuits the handler and blocking webhooks as in Python. test: `crates/gingress/src/pipeline/tests.rs::rule_block_short_circuits`.
- 5.2.3 - Site row 6: a node event resolves its project from the node's checkout rows, and no row fails closed. test: `crates/gingress/src/pipeline/tests.rs::site_row_6_project_from_node_checkout_rows`.

**Verification:** the shared P5 commands.

### 5.3 Session lookup, activation, coordination and terminal context [category: code] (depends: 5.2)
`kind: deliverable`

Targets:
- `crates/gingress/src/lib.rs`
- `crates/gingress/src/sessions.rs`
- `crates/gingress/src/sessions/lookup.rs`
- `crates/gingress/src/sessions/activation.rs`
- `crates/gingress/src/sessions/coordinator.rs`
- `crates/gingress/src/sessions/terminal_context.rs`
- `crates/gingress/src/sessions/tests.rs`
- `crates/gingress/tests/pipeline_corpus.rs`

**Research context:** `src/gobby/hooks/session_lookup.py` (748),
`session_activation.py` (671), `session_materialize.py` (212:
`activate_deferred_session`), `session_ref_resolution.py` (48),
`session_types.py` (211), `codex_seat.py` (50), `session_coordinator.py`
(761: in-process registrations, cached agent messages and lookup locks; agent
run calls go to the Agents seam), `terminal_context.py` (488:
`enrich_terminal_context_with_cwd`, `_is_fresh_seat_tui`) and
`src/gobby/hooks/agent_run_ingress.py` (163: `validate_managed_agent_hook`,
`session_end_targets_live_cli`).
- **Site row 27.** For an event whose `machine_id` is not the hub's,
  `enrich_terminal_context_with_cwd` returns before any process inspection
  (hub-mode 2.2, #24272).

**Granularity:** one leaf with six production files. Lookup, activation,
coordination and terminal context resolve one session binding per event and
share the coordinator's lookup lock.

**Acceptance:**
- 5.3.1 - Every lookup, deferred-activation, managed-agent gating and session-ref case in the 1.4 corpus replays equal. test: `crates/gingress/tests/pipeline_corpus.rs::session_binding_cases_replay_equal`.
- 5.3.2 - Session lookups bind only rows owned by the event's machine. test: `crates/gingress/src/sessions/tests.rs::lookups_bind_machine_owned_rows_only`.
- 5.3.3 - Site row 27: a foreign-machine event skips process inspection, and a local fresh-seat check is unchanged. test: `crates/gingress/src/sessions/tests.rs::site_row_27_node_event_skips_process_inspection`.

**Verification:** the shared P5 commands.

### 5.4 Node session registration [category: code] (depends: 5.3)
`kind: deliverable`

Targets:
- `crates/gingress/src/sessions.rs`
- `crates/gingress/src/sessions/register.rs`
- `crates/gingress/src/sessions/tests.rs`
- `docs/contracts/parity-waivers.json::*` — scope-reason: adds one waiver entry for this divergence
- `docs/contracts/parity-ledger.md::*` — scope-reason: regenerated so the hooks row notes the waiver
- `tests/contracts/pipeline/manifest.json`

**Research context:**
- **hub-mode Decision 18.** Registering a node session that the hub did not
  pre-register is the fourth use of the key-verified foreign-machine path,
  owned by #24151. Until then Python refuses such a session (a `/clear`
  successor, or an operator's own CLI on the node) at registration.
- **Identity.** The verified front-door identity names the node machine (3.2's
  machine binding).
- **Waivers.** `docs/contracts/parity-waivers.json` and
  `scripts/parity_waivers.py` (#24963, closed) record behaviour-level
  divergences that compare reports honour. An entry carries `id` (three
  kebab segments), `surface` (an exact ledger key), `behaviour`, `decision`
  (a task ref or plan anchor) and `replacement`; a waived corpus case cites
  it, and `scripts/generate_parity_ledger.py` marks the row `Waived`.

A node-origin session start whose session has no row, carrying a verified
identity for that machine, registers the session for that machine through the
Sessions seam, then continues as a new session. Without a verified identity it
is refused as before. Record the divergence as waiver
`hooks.node-session.register` on surface `HTTP POST /api/hooks/execute`
with decision `#24151`. The 1.4 refusal case cites it and gains a
`waived_expect` with the registration calls. Regenerate the ledger.

**Acceptance:**
- 5.4.1 - A verified node session start with no row registers the session under the node's machine and proceeds. test: `crates/gingress/src/sessions/tests.rs::verified_node_session_without_row_registers`.
- 5.4.2 - Without a verified identity for that machine the start is refused, and no row is written. test: `crates/gingress/src/sessions/tests.rs::unverified_node_session_is_refused`.
- 5.4.3 - The waiver loads, the 1.4 refusal case cites it, and the regenerated ledger matches the committed one. test: `tests/test_parity_ledger.py::test_committed_ledger_matches_current_public_inventory`.

**Verification:** the shared P5 commands, and
`GOBBY_TEST_PROTECT=1 uv run pytest tests/test_parity_ledger.py -v`.

### 5.5 Session start: flow, context, profile and transcripts [category: code] (depends: 5.4)
`kind: deliverable`

Targets:
- `crates/gingress/src/lib.rs`
- `crates/gingress/src/handlers.rs`
- `crates/gingress/src/handlers/session_start.rs`
- `crates/gingress/src/handlers/session_start/flow.rs`
- `crates/gingress/src/handlers/session_start/context.rs`
- `crates/gingress/src/handlers/tests.rs`
- `crates/gingress/tests/pipeline_corpus.rs`

**Research context:** `src/gobby/hooks/event_handlers/_session_start/`:
`__init__.py` (217), `flow.py` (795: pre-created session lookup by external
id, terminal context and gobby session id; the STOP epoch bump through
`envelope_dedupe.bump_stop_replay_epoch`), `context.py` (272), `profile.py`
(109), `transcripts.py` (127), `in_place_compact.py` (42) and `types.py` (17);
`src/gobby/hooks/event_handlers/_base.py::EventHandlersBase` (314: turn
lifecycle helpers).

Port them. Session start bumps the STOP epoch of the event's machine through
3.1's per-machine API.

**Acceptance:**
- 5.5.1 - Every new, resumed, cleared and compacted session-start case in the 1.4 corpus replays equal. test: `crates/gingress/tests/pipeline_corpus.rs::session_start_flow_cases_replay_equal`.
- 5.5.2 - Session start bumps only its own machine's STOP epoch. test: `crates/gingress/src/handlers/tests.rs::session_start_bumps_own_machine_epoch`.

**Verification:** the shared P5 commands.

### 5.6 Session start: handoff, materialization, agents, claims and skills [category: code] (depends: 5.5)
`kind: deliverable`

Targets:
- `crates/gingress/src/handlers.rs`
- `crates/gingress/src/handlers/session_start.rs`
- `crates/gingress/src/handlers/session_start/handoff.rs`
- `crates/gingress/src/handlers/session_start/materialize.rs`
- `crates/gingress/src/handlers/session_start/agents.rs`
- `crates/gingress/src/handlers/session_start/runtime.rs`
- `crates/gingress/src/handlers/tests.rs`
- `crates/gingress/tests/pipeline_corpus.rs`

**Research context:** `src/gobby/hooks/event_handlers/_session_start/`:
`handoff.py` (514), `materialize.py` (591), `agents.py` (267), `claims.py`
(215) and `terminal_runtime.py` (274); `src/gobby/hooks/skill_manager.py`
(439: `HookSkillManager`, router skills read by
`EventHandlersBase._router_skills`).

**Granularity:** one leaf with six production files. Each ports one
session-start sub-flow that `flow.rs` calls in order; the corpus replays
them together.

**Acceptance:**
- 5.6.1 - Every handoff, materialization, agent activation, claim restore and router-skill case in the 1.4 corpus replays equal. test: `crates/gingress/tests/pipeline_corpus.rs::session_start_subflow_cases_replay_equal`.
- 5.6.2 - A compact successor with no compact row logs and proceeds as Python does. test: `crates/gingress/src/handlers/tests.rs::missing_compact_row_matches_python`.

**Verification:** the shared P5 commands.

### 5.7 Session end, completion, auto-link and summaries [category: code] (depends: 5.6)
`kind: deliverable`

Targets:
- `crates/gingress/src/handlers.rs`
- `crates/gingress/src/handlers/session_end.rs`
- `crates/gingress/src/handlers/responses.rs`
- `crates/gingress/src/handlers/tests.rs`
- `crates/gingress/tests/pipeline_corpus.rs`

**Research context:** `src/gobby/hooks/event_handlers/_session_end.py` (266),
`_session_responses.py` (247), `src/gobby/hooks/session_completion.py` (153),
`session_end_auto_link.py` (111: `SessionEndAutoLinkWorker`),
`session_summary_dispatcher.py` (106) and `session_summary_wiring.py` (33).
Summaries go through the Code index seam; auto-link through the Tasks seam.

**Acceptance:**
- 5.7.1 - Every session-end, completion, auto-link and summary-dispatch case in the 1.4 corpus replays equal. test: `crates/gingress/tests/pipeline_corpus.rs::session_end_cases_replay_equal`.
- 5.7.2 - A summary or auto-link seam failure never fails the session-end response. test: `crates/gingress/src/handlers/tests.rs::session_end_side_effects_fail_open`.

**Verification:** the shared P5 commands.

### 5.8 Tool handlers, outcomes, error tracking and the edit trigger [category: code] (depends: 5.7)
`kind: deliverable`

Targets:
- `crates/gingress/src/handlers.rs`
- `crates/gingress/src/handlers/tool.rs`
- `crates/gingress/src/handlers/tool/outcomes.rs`
- `crates/gingress/src/handlers/tool/errors.rs`
- `crates/gingress/src/handlers/tests.rs`
- `crates/gingress/tests/pipeline_corpus.rs`

**Research context:** `src/gobby/hooks/event_handlers/_tool.py` (762:
`ToolEventHandlerMixin`, `_notify_code_index`), `src/gobby/hooks/tool_outcomes.py`
(503: `hook_event_tool_outcome`), `tool_error_tracker.py` (458) and
`mcp_result.py` (13).
- **Code-index trigger (Decision 10).** Python calls
  `src/gobby/code_index/trigger.py::CodeIndexTrigger` from
  `_notify_code_index`. The Rust handler calls the Code index seam's
  `notify_file_changed(root, file)`; 7.10 implements it with
  `gobby_codeindex::jobs::trigger::EditTrigger::notify_file_changed` through
  `HubJobs::edit_trigger()` (#24705).
- **Site row 16.** A node event skips the trigger; node-halves 2.11 runs gcode
  on the node.

**Acceptance:**
- 5.8.1 - Every before-tool, after-tool, failure and error-tracking case in the 1.4 corpus replays equal. test: `crates/gingress/tests/pipeline_corpus.rs::tool_cases_replay_equal`.
- 5.8.2 - A hub-local edit notifies the trigger once with the repository root and relative path. test: `crates/gingress/src/handlers/tests.rs::hub_edit_notifies_the_trigger`.
- 5.8.3 - Site row 16: a node edit never notifies the trigger. test: `crates/gingress/src/handlers/tests.rs::site_row_16_node_edit_skips_trigger`.

**Verification:** the shared P5 commands.

### 5.9 Agent handlers, pending messages and Grok pending context [category: code] (depends: 5.8)
`kind: deliverable`

Targets:
- `crates/gingress/src/handlers.rs`
- `crates/gingress/src/handlers/agent.rs`
- `crates/gingress/src/handlers/agent/messages.rs`
- `crates/gingress/src/handlers/agent/grok_pending.rs`
- `crates/gingress/src/handlers/tests.rs`
- `crates/gingress/tests/pipeline_corpus.rs`

**Research context:** `src/gobby/hooks/event_handlers/_agent.py` (793),
`src/gobby/hooks/pending_messages.py` (154), `pending_message_reservations.py`
(88) and `grok_pending_context.py` (756). Messages and reservations go through
the Agents seam.

**Acceptance:**
- 5.9.1 - Every before-agent, after-agent, stop and pending-message case in the 1.4 corpus replays equal. test: `crates/gingress/tests/pipeline_corpus.rs::agent_cases_replay_equal`.
- 5.9.2 - A reserved pending message is delivered once and released on a failed delivery. test: `crates/gingress/src/handlers/tests.rs::pending_message_reservation_matches_python`.
- 5.9.3 - Grok pending context is applied once to the next Grok event, as in Python. test: `crates/gingress/src/handlers/tests.rs::grok_pending_context_applies_once`.

**Verification:** the shared P5 commands.

### 5.10 Misc and plan handlers, turn lifecycle and terminal handoff delivery [category: code] (depends: 5.9)
`kind: deliverable`

Targets:
- `crates/gingress/src/handlers.rs`
- `crates/gingress/src/handlers/misc.rs`
- `crates/gingress/src/handlers/turns.rs`
- `crates/gingress/src/handlers/handoff_delivery.rs`
- `crates/gingress/src/handlers/handoff_delivery/recovery.rs`
- `crates/gingress/src/handlers/tests.rs`
- `crates/gingress/tests/pipeline_corpus.rs`

**Research context:** `src/gobby/hooks/event_handlers/_misc.py` (777:
`MiscEventHandlerMixin.handle_worktree_create`, `handle_worktree_remove`),
`_plan.py` (64), `_session.py` (27), the turn and composer helpers in
`_base.py`, `src/gobby/hooks/terminal_handoff_delivery.py` (903) and
`handoff_dispatch_recovery.py` (191). Terminal calls go through the Terminals
seam.
- **Site row 19.** A node `WorktreeCreate` or `WorktreeRemove` answers allow
  before any hub effect.

**Acceptance:**
- 5.10.1 - Every misc, plan, compact, notification and terminal-handoff case in the 1.4 corpus replays equal. test: `crates/gingress/tests/pipeline_corpus.rs::misc_and_handoff_cases_replay_equal`.
- 5.10.2 - Turn waits, composer submits and handoff dispatch recovery match Python on the recorded cases. test: `crates/gingress/src/handlers/tests.rs::turn_lifecycle_and_recovery_match_python`.
- 5.10.3 - Site row 19: a node worktree event allows with no hub effect. test: `crates/gingress/src/handlers/tests.rs::site_row_19_node_worktree_allows_without_effect`.

**Verification:** the shared P5 commands.

### 5.11 MCP and webhook dispatch and the broadcaster [category: code] (depends: 5.10)
`kind: deliverable`

Targets:
- `crates/gingress/Cargo.toml`
- `Cargo.lock`
- `crates/gingress/src/lib.rs`
- `crates/gingress/src/dispatch.rs`
- `crates/gingress/src/dispatch/mcp.rs`
- `crates/gingress/src/dispatch/webhooks.rs`
- `crates/gingress/src/dispatch/broadcast.rs`
- `crates/gingress/src/dispatch/tests.rs`
- `crates/gingress/tests/pipeline_corpus.rs`

**Research context:** `src/gobby/hooks/dispatchers/mcp.py` (531),
`dispatchers/webhook.py` (211), `src/gobby/hooks/webhooks.py` (367:
`WebhookDispatcher`, blocking webhooks), `src/gobby/workflows/webhook.py`
(233: `RetryConfig`, `VALID_METHODS`, `DEFAULT_RETRY_STATUS_CODES`,
`MAX_RETRY_BACKOFF_SECONDS`) and `src/gobby/hooks/broadcaster.py` (461).
`reqwest` 0.12 is already in `Cargo.lock` (gcore).

Port them as `dispatch`. Webhook endpoints come from the Config seam; HTTP
delivery uses `reqwest` with Python's retry rules; broadcasts go through the
Broadcast seam.

**Acceptance:**
- 5.11.1 - Every MCP-dispatch, webhook and broadcast case in the 1.4 corpus replays equal. test: `crates/gingress/tests/pipeline_corpus.rs::dispatch_cases_replay_equal`.
- 5.11.2 - A blocking webhook's decision applies as in Python, and retries follow `RetryConfig` on a local test server. test: `crates/gingress/src/dispatch/tests.rs::webhooks_block_and_retry_like_python`.

**Verification:** the shared P5 commands.

### 5.12 Route execution: adapters, timeouts, AGY preflight and hold-open [category: code] (depends: 5.11)
`kind: deliverable`

Targets:
- `crates/gingress/src/lib.rs`
- `crates/gingress/src/execute.rs`
- `crates/gingress/src/execute/run.rs`
- `crates/gingress/src/execute/preflight.rs`
- `crates/gingress/src/execute/hold_open.rs`
- `crates/gingress/src/execute/tests.rs`
- `crates/gingress/tests/envelope_corpus.rs`

**Research context:**
- **Python.** `src/gobby/hooks/adapter_execution.py` (457: `run_adapter_hook`,
  `HOOK_ADAPTER_MAX_WORKERS`, `register_adapter_timeout_finalization`),
  `startup_claim_preflight.py` (551: `preflight_agy_startup_claim_bounded`,
  `rollback_agy_startup_claim`, `invalidate_agy_startup_claim`,
  `_resolve_or_register_session`), `health_gate.py` (170:
  `DaemonNotReadyError`), `agent_run_ingress.py`
  (`AgentRunIngressRetryableError`), `src/gobby/servers/routes/mcp/hook_hold_open.py`
  (209: `_maybe_hold_open`), `hook_responses.py` (217: `FAIL_SAFE_HOOK_TYPES`,
  `_graceful_error_response`, `_hook_exception_response`) and
  `src/gobby/config/hooks.py::HookTimeoutConfig`.
- **Retry bodies.** 503 `preflight_timeout`, `ingress_backpressure` (reasons
  `agent_run_identity_pending`, `daemon_not_ready`) and `adapter_timeout`. An
  adapter that outlives its timeout finalizes its envelope when it completes.
- **Failure rule.** A `CheckoutReadUnavailable` that propagates blocks a
  critical hook with the diagnostic in its reason and degrades any other hook
  (`_hook_exception_response`).
- **Site row 1.** `run_adapter_hook` sets the delivery origin for every
  delivery.
- **Site row 25.** `_resolve_or_register_session` keeps
  `require_local_machine_id`, so a node envelope is refused before any disk
  walk.

`execute::run` is the closure 3.3 wraps: select the adapter (4.1), translate,
run `pipeline` with the delivery origin set, translate the response, and
apply hold-open, timeouts and the graceful-error rules. This completes
`POST /api/hooks/execute`.

**Granularity:** one leaf with five production files and five acceptance
items. Adapter dispatch, timeouts, preflight and hold-open are one execution
step whose retry bodies the envelope corpus checks together.

**Acceptance:**
- 5.12.1 - Every 1.2 envelope case (the three retry kinds, both backpressure reasons, HookManager not initialized and the four DR5 mappings) and every 1.2 sequence case replays equal end to end. test: `crates/gingress/tests/envelope_corpus.rs::route_cases_replay_equal_end_to_end`.
- 5.12.2 - An adapter that outlives its timeout answers `adapter_timeout`, then finalizes its envelope when it completes. test: `crates/gingress/src/execute/tests.rs::timed_out_adapter_finalizes_later`.
- 5.12.3 - An unavailable checkout read blocks a critical hook with the diagnostic and degrades any other hook. test: `crates/gingress/src/execute/tests.rs::checkout_unavailable_blocks_critical_only`.
- 5.12.4 - Site row 1: every delivery runs with its delivery origin set, hub-local or the node's machine id. test: `crates/gingress/src/execute/tests.rs::site_row_1_delivery_origin_is_set`.
- 5.12.5 - Site row 25: an AGY startup preflight for a node envelope is refused before any disk walk. test: `crates/gingress/src/execute/tests.rs::site_row_25_node_preflight_refused`.

**Verification:** the shared P5 commands.

## P6: Node and webhook routes
`kind: framing`

Port the node hook replay and receipt routes, the detection-manifest route and
the webhook routes into the `hooks` family. `POST /api/nodes/report` and
`/api/nodes/runs/*` stay Python-served and move to #21564 (Decision 13). No HTTP
corpus case covers these routes, because the corpus has no node-key credential
recipe. Each Rust test mirrors the Python route test it names, and the 7.13
compare soak checks live parity. A handler reads the caller's machine from the
identity extension that 7.12's admission attaches; tests attach it directly.
Verification for every P6 deliverable is the shared P5 commands.

### 6.1 Node hook replay and receipt routes [category: code] (depends: 5.12)
`kind: deliverable`

Targets:
- `crates/gingress/src/lib.rs`
- `crates/gingress/src/family.rs`
- `crates/gingress/src/node_routes.rs`
- `crates/gingress/src/node_routes/tests.rs`

**Research context:**
- **Python.** `src/gobby/servers/routes/node_hooks.py` (136 lines).
  `ReplayRequest` takes `v` 1, `envelope_id`, `envelope`, `age_seconds` (finite,
  at least 0), `processed` and `live`, and forbids other fields.
  `ReceiptRequest` takes `v` 1, `op` (`ack` or `release`), `receipt_id` (UUID)
  and `delivery_generation` (at least 1). `_body` answers 422 `invalid_body` or
  `unsupported_version`. `_node_machine` answers 403 `node_identity_required`
  without a verified identity and 403 `machine_is_local` for the hub's own
  machine.
- **Replay.** `POST /api/nodes/hooks/replay` returns
  `decide_replay(envelope_id, envelope, age_seconds, processed, live, machine_id)`
  (3.4's `receipts::replay`).
- **Receipts.** `POST /api/nodes/receipts` looks up the receipt's session
  (`hook_receipt_effects` joined to `sessions`). When the node does not own that
  session, `ack` answers `{"acknowledged": false, "carrying_envelope_ids": []}`
  and `release` answers `{"released": false}`. Otherwise `release` calls
  `release_receipt`, and `ack` calls `acknowledge_and_apply_receipt` and
  returns the deduplicated original and current envelope ids.
- **Callers.** `crates/gdaemon/src/nodes/hooks/drain.rs` (`REPLAY`) and
  `crates/gdaemon/src/nodes/hooks/receipts.rs` (`ROUTE`).
- **Python tests.** `tests/servers/test_node_hook_routes.py::test_replay_route`
  and `::test_receipts_route`.

`node_routes` serves both routes in the family router over 3.4's receipt
functions. Session reads go through the Sessions seam and message delivery
through the Agents seam.

**Acceptance:**
- 6.1.1 - Replay validation and decisions match `test_replay_route`, including both 422 bodies. test: `crates/gingress/src/node_routes/tests.rs::replay_route_matches_python`.
- 6.1.2 - `ack` and `release` on a node-owned session match `test_receipts_route`, and a receipt for another machine's session gets the refusal body and changes nothing. test: `crates/gingress/src/node_routes/tests.rs::receipts_route_matches_python`.
- 6.1.3 - Without a caller machine both routes answer 403 `node_identity_required`, and the hub's own machine answers 403 `machine_is_local`, before any read. test: `crates/gingress/src/node_routes/tests.rs::node_identity_is_required`.

### 6.2 Detection-manifest route [category: code] (depends: 6.1)
`kind: deliverable`

Targets:
- `crates/gingress/src/family.rs`
- `crates/gingress/src/node_routes.rs`
- `crates/gingress/src/node_routes/manifests.rs`
- `crates/gingress/src/node_routes/tests.rs`

**Research context:**
`src/gobby/servers/routes/node_runs.py::create_node_runs_router` defines
`detection_manifests`. It applies `_node_machine`, reads
`SELECT provider_id, content FROM detection_manifests ORDER BY provider_id`,
and sets `ETag` to the quoted SHA-256 hex of the concatenated
`provider_id\nsha256(content)\n` lines. An `If-None-Match` list containing that
tag answers 304 with the `ETag`; otherwise 200
`{"manifests": [{"provider_id", "content"}]}` with the `ETag`. Python test:
`tests/servers/test_node_run_routes.py::test_detection_manifests_route`.

`node_routes::manifests` serves `GET /api/nodes/detection-manifests` from the
family's gcore pool.

**Acceptance:**
- 6.2.1 - The body, `ETag`, list-form `If-None-Match` 304 and both identity 403s match `test_detection_manifests_route`. test: `crates/gingress/src/node_routes/tests.rs::detection_manifests_match_python`.

### 6.3 Webhook list and test routes [category: code] (depends: 6.2)
`kind: deliverable`

Targets:
- `crates/gingress/src/lib.rs`
- `crates/gingress/src/family.rs`
- `crates/gingress/src/webhook_routes.rs`
- `crates/gingress/src/webhook_routes/tests.rs`

**Research context:** `src/gobby/servers/routes/mcp/webhooks.py` (167 lines).
- **List.** `GET /api/webhooks` without config answers
  `{"success": true, "enabled": false, "endpoints": []}`. Each endpoint carries
  `name`, `url`, `events`, `enabled`, `can_block`, `fail_closed`, `timeout` and
  `retry_count`.
- **Test.** `POST /api/webhooks/test` answers 400 `Invalid JSON body` or
  `Webhook name required`, and `{"success": false, "error": ...}` for
  `Configuration not available`, `Webhooks are disabled`,
  `Webhook not found: <name>` and `Webhook is disabled: <name>`. It posts a test
  event with the endpoint's timeout. A 2xx answers success with `status_code`
  and the response time; other outcomes answer `HTTP <code>`,
  `Request timed out`, `Request failed` or `Webhook test failed`.
- **Python tests.** `tests/servers/test_http_endpoints.py::TestWebhooksEndpoints`.

`webhook_routes` reads endpoints through the Config seam and posts through
5.11's webhook client.

**Acceptance:**
- 6.3.1 - The list body matches Python with and without config, including the failure-policy fields. test: `crates/gingress/src/webhook_routes/tests.rs::webhook_list_matches_python`.
- 6.3.2 - Every test-route error body matches Python, and a test against a local server reports status, timing, timeout and failure as Python does. test: `crates/gingress/src/webhook_routes/tests.rs::webhook_test_matches_python`.

## P7: Native composition, flip and Python deletion
`kind: framing`

Compose the family in gdaemon, bind each seam group to its owning family, bind
native admission, soak in compare mode, add the DR6 forward, flip to native
default, record the ledger rows, and delete the Python routes and the modules
only they use.

Shared rules for every P7 deliverable:
- Every leaf that changes gdaemon or a crate it links bumps gdaemon by 0.0.1
  with `Cargo.lock` and the managed pins (`src/gobby/install/version_pins.py`),
  so P7 runs as a chain.
- Seam adapters live in `crates/gdaemon/src/hooks/seams/<family>.rs`, each with
  `<family>/tests.rs` against the isolated test hub. An adapter maps every
  method of its 5.1 group, and the engine seams of the same family, to that
  family's public operation. A family failure returns `SeamUnavailable`.
- No agent reads or edits the live bootstrap. The operator sets each
  `front_door.routes.hooks` value, and the Orchestrator coordinates it at each
  cutover.
- Verification: `GOBBY_SCHEMA_TEST_DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test cargo nextest run -p gobby-daemon -p gobby-ingress`,
  `cargo clippy -p gobby-daemon -p gobby-ingress --all-targets -- -D warnings`
  and `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/install/test_version_pins.py`.

### 7.1 Compose the `hooks` family in gdaemon [category: code] (depends: 6.3)
`kind: deliverable`

Targets:
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `crates/gdaemon/src/lib.rs::*` — scope-reason: declare the hooks composition module
- `crates/gdaemon/src/hooks.rs`
- `crates/gdaemon/src/hooks/seams.rs`
- `crates/gdaemon/src/hooks/seams/local.rs`
- `crates/gdaemon/src/hooks/dispatch.rs`
- `crates/gdaemon/src/hooks/tests.rs`
- `crates/gdaemon/src/serve.rs::*` — scope-reason: register the hooks family beside RuntimeFamily in the pool-gated block
- `crates/gdaemon/tests/hooks_family.rs`
- `crates/gingress/src/family.rs`
- `src/gobby/install/version_pins.py::*` — scope-reason: keep managed release pins coherent with the binaries this deliverable changes

**Research context:**
- **Registration.** `crates/gdaemon/src/serve.rs::run` (818 lines) builds
  `RuntimeFamily` on the shared pool inside the pool-gated block, leaks it with
  `Box::leak` and pushes it onto `FAMILIES`. `unimplemented_families` only
  reports families that are absent. `RouteFamily::claims` returning false
  proxies a request unchanged whatever the backend.
- **Broadcast.** `crates/gdaemon/src/websocket/broker.rs::BroadcastBroker::publish`
  is the native WS fan-out (#21558, closed).
- **In-process dispatch.** `crates/gdaemon/src/front_door/mod.rs::FrontDoor::handle`
  runs authentication and the route table for one request.

`hooks::compose` builds `HooksFamily` and its seam bundle, and `serve::run`
calls it next to `RuntimeFamily` (one line, so serve.rs stays under 850). Every
5.1 group starts as an unbound implementation that returns `SeamUnavailable`.
Three are bound here:
- Broadcast, over `BroadcastBroker::publish`;
- the engine's `CommandRunner` (2.14), over a local process runner in
  `seams/local.rs`;
- hub-origin `CheckoutReads`, over 2.2's `LocalCheckout`. Node origin stays
  unbound until 7.11.
`SessionVariables` uses `gobby-workflows`' gcore-pool implementation.

`hooks::dispatch::LocalDispatch` sends a request through `FrontDoor::handle`
with a loopback peer and the local operator credential
(`gobby_core::local_token::read_api_key`), set once after the door is built.
7.7 and 7.15 use it so their requests reach whichever backend serves the route.

Until 7.12 binds admission, `HooksFamily::claims` returns false. Every request
then stays proxied, whatever the route value, while seam groups are unbound.

**Granularity:** one leaf with seven production files and four acceptance
items. Registration, the bundle, the local seams and in-process dispatch are
one composition step; nothing below it has a caller until it exists.

**Acceptance:**
- 7.1.1 - Hub and standalone doors register the family, and with `hooks` set to proxy, compare or native every hooks request reaches Python unchanged. test: `crates/gdaemon/tests/hooks_family.rs::family_registers_and_proxies_until_admission`.
- 7.1.2 - Broadcasts reach the native broker, the local runner runs a `run_command` case, and every unbound group returns `SeamUnavailable`. test: `crates/gdaemon/src/hooks/tests.rs::local_seams_bind_and_unbound_groups_refuse`.
- 7.1.3 - `LocalDispatch` sends a request through authentication and the route table, and it reaches Python when the route is proxied. test: `crates/gdaemon/src/hooks/tests.rs::local_dispatch_uses_the_front_door`.
- 7.1.4 - gdaemon carries this leaf's patch bump, lock and pin. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 7.2 Sessions seam adapter [category: code] (depends: 7.1, #21562)
`kind: deliverable`

Targets:
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `crates/gdaemon/src/hooks/seams.rs`
- `crates/gdaemon/src/hooks/seams/sessions.rs`
- `crates/gdaemon/src/hooks/seams/sessions/tests.rs`
- `src/gobby/install/version_pins.py::*` — scope-reason: keep managed release pins coherent with the binaries this deliverable changes

**Research context:** 5.1's Sessions group (session rows, lookup and status,
platform binding, deferred activation, materialization, clear-run lineage,
messages and transcripts, receipts and staged effects, and node session
registration from 5.4 and Decision 8). Engine seams: `SessionQueries` targets,
waits and stop signals, and `TranscriptQueries` (2.9). The owner is `gobby-sessions`
(`crates/gsessions`, `.gobby/plans/sessions-family.md`).

**Acceptance:**
- 7.2.1 - Every Sessions-group and Sessions-owned engine seam method binds to a `gobby-sessions` operation; a seam write is visible through the family's read API on the test hub, and a family failure returns `SeamUnavailable`. test: `crates/gdaemon/src/hooks/seams/sessions/tests.rs::sessions_seams_bind_every_method`.
- 7.2.2 - gdaemon carries this leaf's patch bump, lock and pin. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 7.3 Tasks seam adapter [category: code] (depends: 7.2, #21560)
`kind: deliverable`

Targets:
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `crates/gdaemon/src/hooks/seams.rs`
- `crates/gdaemon/src/hooks/seams/tasks.rs`
- `crates/gdaemon/src/hooks/seams/tasks/tests.rs`
- `src/gobby/install/version_pins.py::*` — scope-reason: keep managed release pins coherent with the binaries this deliverable changes

**Research context:** 5.1's Tasks group (task reads and writes, session-task
links, commit auto-link) and the engine's `TaskQueries` (2.9). The owner is
`gobby-tasks` (`crates/gtasks`, `.gobby/plans/tasks-family.md`).

**Acceptance:**
- 7.3.1 - Every Tasks-group and `TaskQueries` method binds to a `gobby-tasks` operation; a seam write is visible through the family's read API on the test hub, and a family failure returns `SeamUnavailable`. test: `crates/gdaemon/src/hooks/seams/tasks/tests.rs::tasks_seams_bind_every_method`.
- 7.3.2 - gdaemon carries this leaf's patch bump, lock and pin. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 7.4 Agents seam adapter [category: code] (depends: 7.3, #21564)
`kind: deliverable`

Targets:
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `crates/gdaemon/src/hooks/seams.rs`
- `crates/gdaemon/src/hooks/seams/agents.rs`
- `crates/gdaemon/src/hooks/seams/agents/tests.rs`
- `src/gobby/install/version_pins.py::*` — scope-reason: keep managed release pins coherent with the binaries this deliverable changes

**Research context:** 5.1's Agents group (agent runs and their lifecycle,
managed-agent validation, worktrees, progress, attention metadata, pending
messages and reservations), the engine's `AgentDefinitions` and the
pending-message reads of `SessionQueries` (2.9). The owner is `gobby-agents`
(`crates/gagents`, `.gobby/plans/agents-family.md`).

**Acceptance:**
- 7.4.1 - Every Agents-group, `AgentDefinitions` and pending-message method binds to a `gobby-agents` operation; a seam write is visible through the family's read API on the test hub, and a family failure returns `SeamUnavailable`. test: `crates/gdaemon/src/hooks/seams/agents/tests.rs::agents_seams_bind_every_method`.
- 7.4.2 - gdaemon carries this leaf's patch bump, lock and pin. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 7.5 Terminals seam adapter [category: code] (depends: 7.4, #21565)
`kind: deliverable`

Targets:
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `crates/gdaemon/src/hooks/seams.rs`
- `crates/gdaemon/src/hooks/seams/terminals.rs`
- `crates/gdaemon/src/hooks/seams/terminals/tests.rs`
- `src/gobby/install/version_pins.py::*` — scope-reason: keep managed release pins coherent with the binaries this deliverable changes

**Research context:** 5.1's Terminals group (terminal manager, runtime registry,
composer ledger). The owner is `gobby-terminals` (`crates/gterminals`), whose
native routes #21565 lands.

**Acceptance:**
- 7.5.1 - Every Terminals-group method binds to a `gobby-terminals` operation; a seam write is visible through the family's read API on the test hub, and a family failure returns `SeamUnavailable`. test: `crates/gdaemon/src/hooks/seams/terminals/tests.rs::terminals_seams_bind_every_method`.
- 7.5.2 - gdaemon carries this leaf's patch bump, lock and pin. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 7.6 Skills seam adapter [category: code] (depends: 7.5, #21580)
`kind: deliverable`

Targets:
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `crates/gdaemon/src/hooks/seams.rs`
- `crates/gdaemon/src/hooks/seams/skills.rs`
- `crates/gdaemon/src/hooks/seams/skills/tests.rs`
- `src/gobby/install/version_pins.py::*` — scope-reason: keep managed release pins coherent with the binaries this deliverable changes

**Research context:** 5.1's Skills group (router skills and skill content) and
the engine's `SkillLoader` (2.14). The owner is `gobby-skills` (`crates/gskills`,
`.gobby/plans/gdaemon-skills-family.md`).

**Acceptance:**
- 7.6.1 - Every Skills-group and `SkillLoader` method binds to a `gobby-skills` operation and returns the content the family serves on the test hub; a family failure returns `SeamUnavailable`. test: `crates/gdaemon/src/hooks/seams/skills/tests.rs::skills_seams_bind_every_method`.
- 7.6.2 - gdaemon carries this leaf's patch bump, lock and pin. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 7.7 MCP seam adapter [category: code] (depends: 7.6, #21566)
`kind: deliverable`

Targets:
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `crates/gdaemon/src/hooks/seams.rs`
- `crates/gdaemon/src/hooks/seams/mcp.rs`
- `crates/gdaemon/src/hooks/seams/mcp/tests.rs`
- `src/gobby/install/version_pins.py::*` — scope-reason: keep managed release pins coherent with the binaries this deliverable changes

**Research context:** 5.1's MCP group (`call_tool`) and the engine's
`McpDispatch` (2.14). Python's rule bridge (`src/gobby/hooks/factory.py`) and
MCP dispatcher (`src/gobby/hooks/dispatchers/mcp.py`) call tools with
`enforce_workflow=False`, so a rule-issued call runs no proxied rule pass. The
`/api/mcp/tools/call` route (`src/gobby/servers/routes/mcp/tools.py`) runs with
enforcement off by default.

Both seams send `POST /api/mcp/tools/call` through 7.1's `LocalDispatch`, so a
call reaches whichever backend serves the `mcp` family, Python today and
#21566 or #24884 later. Decision 2 keeps #21566 a hard prerequisite.

**Acceptance:**
- 7.7.1 - A rule `mcp_call` and a 5.11 MCP dispatch reach the tool through the front door with enforcement off and return its result; an unreachable backend returns `SeamUnavailable`. test: `crates/gdaemon/src/hooks/seams/mcp/tests.rs::mcp_seams_dispatch_through_the_front_door`.
- 7.7.2 - gdaemon carries this leaf's patch bump, lock and pin. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 7.8 Config seam adapter [category: code] (depends: 7.7, #21559)
`kind: deliverable`

Targets:
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `crates/gdaemon/src/hooks/seams.rs`
- `crates/gdaemon/src/hooks/seams/config.rs`
- `crates/gdaemon/src/hooks/seams/config/tests.rs`
- `src/gobby/install/version_pins.py::*` — scope-reason: keep managed release pins coherent with the binaries this deliverable changes

**Research context:** 5.1's Config group (workflow config, rule switches, hook
timeouts, webhook endpoints), served by the native config read #21559 lands.

**Acceptance:**
- 7.8.1 - Every Config-group method returns the value the native config read serves for the same test-hub state; a family failure returns `SeamUnavailable`. test: `crates/gdaemon/src/hooks/seams/config/tests.rs::config_seams_bind_every_method`.
- 7.8.2 - gdaemon carries this leaf's patch bump, lock and pin. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 7.9 Projects seam adapter [category: code] (depends: 7.8, #21585)
`kind: deliverable`

Targets:
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `crates/gdaemon/src/hooks/seams.rs`
- `crates/gdaemon/src/hooks/seams/projects.rs`
- `crates/gdaemon/src/hooks/seams/projects/tests.rs`
- `src/gobby/install/version_pins.py::*` — scope-reason: keep managed release pins coherent with the binaries this deliverable changes

**Research context:** 5.1's Projects group (project id from a cwd, checkout
rows). The owner is `gobby-projects` (`crates/gprojects`,
`.gobby/plans/gdaemon-projects-files-docs-family.md`).

**Acceptance:**
- 7.9.1 - Every Projects-group method binds to a `gobby-projects` operation, including node checkout rows; a family failure returns `SeamUnavailable`. test: `crates/gdaemon/src/hooks/seams/projects/tests.rs::projects_seams_bind_every_method`.
- 7.9.2 - gdaemon carries this leaf's patch bump, lock and pin. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 7.10 Code-index seam adapter [category: code] (depends: 7.9, #21586, #24705)
`kind: deliverable`

Targets:
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `crates/gdaemon/src/hooks/seams.rs`
- `crates/gdaemon/src/hooks/seams/code_index.rs`
- `crates/gdaemon/src/hooks/seams/code_index/tests.rs`
- `src/gobby/install/version_pins.py::*` — scope-reason: keep managed release pins coherent with the binaries this deliverable changes

**Research context:** 5.1's Code-index group (index status, session summaries,
the edit trigger) and the engine's `CodeIndexQueries` (2.9). The edit trigger
calls `gobby_codeindex::jobs::trigger::EditTrigger::notify_file_changed`
through `HubJobs::edit_trigger()` (#24705, Decision 10) for the hub's own
checkouts; 5.8 already skips node events (Site row 16).

**Acceptance:**
- 7.10.1 - Every Code-index-group and `CodeIndexQueries` method binds to a `gobby-codeindex` operation, and a hub-checkout edit reaches `EditTrigger::notify_file_changed`; a family failure returns `SeamUnavailable`. test: `crates/gdaemon/src/hooks/seams/code_index/tests.rs::code_index_seams_bind_every_method`.
- 7.10.2 - gdaemon carries this leaf's patch bump, lock and pin. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 7.11 Node checkout-read adapter [category: code] (depends: 7.10, #24521)
`kind: deliverable`

Targets:
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `crates/gdaemon/src/hooks/seams.rs`
- `crates/gdaemon/src/hooks/seams/checkout.rs`
- `crates/gdaemon/src/hooks/seams/checkout/tests.rs`
- `src/gobby/install/version_pins.py::*` — scope-reason: keep managed release pins coherent with the binaries this deliverable changes

**Research context:** 2.2's `CheckoutReads` for node origin, over the node
channel's `checkout.read` command (node-commands contract version 4, #24520
closed; handler #24521). 2.2's latch rules decide per-request versus latched
failures. Decision 9 keeps every Site Disposition outcome in Rust.

**Acceptance:**
- 7.11.1 - A node-origin read goes over the node channel and returns the handler's facts, and an unanswered or failed read follows 2.2's latch rules. test: `crates/gdaemon/src/hooks/seams/checkout/tests.rs::node_checkout_reads_follow_the_latch`.
- 7.11.2 - gdaemon carries this leaf's patch bump, lock and pin. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 7.12 Native admission binding for hook routes [category: code] (depends: 7.11, 1.2, #25046, #25068)
`kind: deliverable`

Targets:
- `crates/gingress/src/lib.rs`
- `crates/gingress/src/admission.rs`
- `crates/gingress/src/family.rs`
- `crates/gingress/tests/admission.rs`
- `docs/contracts/hook-ingress.md::*` — scope-reason: add the native admission security-gate record
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: keep managed release pins coherent with the binaries this deliverable changes

**Research context:**
- **Front door today.** `crates/gdaemon/src/front_door/auth.rs::AuthState::authenticate`
  resolves API keys into identity headers and passes operator and
  `gobby-agent-v1.` bearers through for Python to verify. ghook sends a managed
  agent token or the local operator credential
  (`crates/ghook/src/transport.rs::post_and_cleanup`). The Python drain and
  barrier use the local operator credential.
- **Python classes.** `src/gobby/servers/auth_service.py::_AGENT_CAPABILITY_MATRIX`
  admits agent tokens on `POST /api/hooks/execute` with `bind_identity` true and
  on `GET /api/webhooks` with `bind_identity` false. API keys, the local
  operator and ordinary `gobby_session` cookies keep Python's per-route
  permissions. Managed `browser-testing.` cookies keep
  `src/gobby/servers/browser_sessions.py::browser_route_allowed` (GET, HEAD and
  OPTIONS only), so every POST row refuses them before any effect.
- **Shared seams.** #25046 binds a verified `Admitted` value before family
  dispatch, and #25068 adds agent-token and runtime-grant admission. Sibling
  bindings: `.gobby/plans/memory-family.md` 8.1 and
  `.gobby/plans/gdaemon-ai-providers-family.md` 5.1.

`admission::AdmissionClass` declares the class of each of the six claimed
rows. `HooksFamily::claims` matches the bound `Admitted` value against the
row's class, replacing 7.1's false. It attaches the caller machine that 3.2.3
and P6 read. The crate never verifies a token or cookie itself. Unavailable
authority returns a typed 503, and shared CORS and preflight stay with #24964
through #25046.

**Expansion obligation:** immediately after expansion, the Orchestrator applies
`security-review` to this leaf and checks the persisted label before dispatch.
Native admission stays held until a Security Reviewer approves the exact
implementation SHA; a later change needs renewed approval.

**Acceptance:**
- 7.12.1 - Agent tokens with matching, mismatched, revoked and expired identity get Python's outcomes on the execute and webhook-list rows and are refused on the other four. test: `crates/gingress/tests/admission.rs::agent_token_rows_match_python`.
- 7.12.2 - API keys, the local operator and ordinary cookies keep Python's permissions on all six rows, managed cookies are refused on every POST before any effect, and node rows receive the key's machine. test: `crates/gingress/tests/admission.rs::operator_key_and_browser_classes_match_python`.
- 7.12.3 - Without a bound `Admitted` value a request stays proxied, unavailable authority returns 503, and the crate holds no verifier. test: `crates/gingress/tests/admission.rs::admission_fails_closed_without_shared_seam`.
- 7.12.4 - The contract records the persisted `security-review` label, the pre-dispatch check and the approved step for the exact SHA. behavior: "hooks admission security gate" in `docs/contracts/hook-ingress.md`.
- 7.12.5 - gdaemon carries this leaf's patch bump, lock and pin. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 7.13 Compare mode for the hook routes [category: code] (depends: 7.12)
`kind: deliverable`

Targets:
- `crates/gingress/src/lib.rs`
- `crates/gingress/src/family.rs`
- `crates/gingress/src/compare.rs`
- `crates/gingress/src/compare/record.rs`
- `crates/gingress/src/compare/tests.rs`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: keep managed release pins coherent with the binaries this deliverable changes

**Research context:**
- **Compare.** `crates/gdaemon/src/front_door/routes.rs::compare` runs both legs
  for GET and HEAD. For other methods it awaits 3.1's `prepare_proxied`, proxies
  once, then calls `check_proxied`, which returns `ProxiedCheck::Verified` or
  `Mismatch(pointer)`. Records go to `front-door-compare.jsonl`
  (`crates/gdaemon/src/front_door/compare.rs::Record`), and
  `gdaemon compare-report --since <ts>` passes only with no diff, native error,
  mismatch or unwaived skip (`Report::passes`).
- **Gates carried.** rust-rule-evaluation D2.3 (no unexplained diff in decision,
  reason, injected context or staged receipt fields), D2.5 (DR8 counters per
  route) and D2.6 (allow-audit lines keep `record_rule_evaluation`'s schema in
  `rule-allow-audit.jsonl`).

`HooksFamily` overrides both methods:
- **Execute.** `prepare_proxied` runs 5.12's execution on pre-state in record
  mode: every seam write and external effect is recorded, the claim is skipped
  and the driver commits nothing. The record is keyed by envelope id.
  `check_proxied` compares decision, reason, context, system message, modified
  input and staged receipt fields with Python's response. A pass whose later
  rules read an `mcp_call` result compares up to that call and is counted as
  partial.
- **Node replay.** `prepare_proxied` computes the decision on pre-state, and
  `check_proxied` compares it.
- **Receipts.** `check_proxied` verifies that the receipt row's post-state
  agrees with Python's body.
- **Webhook test.** `check_proxied` recomputes the config-determined error
  bodies and, for a delivery, checks the response shape and the named enabled
  endpoint.
- The GET rows use the door's concurrent compare.

`compare::record` appends one line per claimed execute to
`<home>/logs/hooks-compare.jsonl`: envelope id, event type, DR8 outcome class,
partial flag and verdict. That is the family's per-route observability (D2.5).
In native mode the family appends each pass's audit records to
`rule-allow-audit.jsonl` (D2.6). An explained difference is one recorded under a
#24963 waiver in `docs/contracts/parity-waivers.json`, such as 5.4's
`hooks.node-session.register`.

**Granularity:** one leaf with four production files and four acceptance
items. Record mode, the per-route checks and the outcome log are one compare
step that the soak reads together.

**Acceptance:**
- 7.13.1 - A record-mode execute writes nothing and runs no external effect, and its response fields compare equal to Python's on every 1.2 and 1.4 case. test: `crates/gingress/src/compare/tests.rs::record_mode_execute_matches_python_without_effects`.
- 7.13.2 - An `mcp_call`-dependent pass compares up to the call and is logged as partial, and a changed decision reports a JSON pointer mismatch. test: `crates/gingress/src/compare/tests.rs::partial_and_mismatch_are_reported`.
- 7.13.3 - Replay, receipt and webhook-test checks verify Python's responses and report a planted divergence; every execute line carries its DR8 class, and native audit lines parse with `record_rule_evaluation`'s schema. test: `crates/gingress/src/compare/tests.rs::post_route_checks_and_outcome_lines`.
- 7.13.4 - gdaemon carries this leaf's patch bump, lock and pin. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 7.14 DR6 pass endpoint and the Python proxy forward [category: code] (depends: 7.13)
`kind: deliverable`

Targets:
- `crates/gdaemon/src/hooks.rs`
- `crates/gdaemon/src/hooks/rule_pass.rs`
- `crates/gdaemon/src/front_door/mod.rs::*` — scope-reason: dispatch the loopback rule-pass route on hub and standalone doors and refuse it on the node door
- `crates/gdaemon/tests/hook_rule_pass.rs`
- `crates/gingress/src/pipeline.rs`
- `crates/gingress/src/pipeline/tests.rs`
- `src/gobby/mcp_proxy/services/rule_pass_forward.py`
- `src/gobby/mcp_proxy/services/result_handling.py::apply_before_tool_enforcement`
- `src/gobby/mcp_proxy/services/result_handling.py::apply_after_tool_workflow`
- `tests/mcp_proxy/services/test_rule_pass_forward.py`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: keep managed release pins coherent with the binaries this deliverable changes

**Research context:**
- **Rule.** rust-rule-evaluation DR6: one engine owns every pass of a session,
  including its pending tool context. Ownership follows how the session's hooks
  arrive. Python's proxy runs a persisting pass before and after every proxied
  tool call (`apply_before_tool_enforcement`, `apply_after_tool_workflow`).
  #21570 deletes the forward (Decision 2).
- **Loopback pattern.** `crates/gdaemon/src/nodes/internal_api.rs::is_route` is
  dispatched in `FrontDoor::handle`; the node door answers not-found. Admission
  requires a loopback peer and `x-gobby-backend-secret` matching
  `AuthState::backend_secret_matches`, before any body parsing
  (`.gobby/plans/s2-7-hub-attention-dispatch.md` 2.3).
- **Python client pattern.** `src/gobby/hub/node_client.py::NodeCommandClient`:
  daemon port from `load_bootstrap()`, `X-Gobby-Backend-Secret:
  front_door_secret()`, `trust_env=False`, no redirects.
- **Route value.** `src/gobby/config/bootstrap.py::FrontDoorConfig` `routes`.
  Python treats an absent family as proxied.

`POST /api/internal/hooks/rule-pass` takes the pass input the two Python sites
build (1.1's proxied-pass cases record it), runs 2.19's `driver` and returns
the full 1.1 response field set, `modified_input` included. Unknown or invalid
bodies get not-found or the shared validation error.

Ownership marker: the Rust pipeline merges `_rule_pass_owner: "rust"` into the
delta of the first pass it commits for a session. `rule_pass_forward` defines
`HOOKS_ROUTE_DEFAULT = "proxy"`, which 7.15 flips. Its `rust_owns_session`
returns true when the effective `hooks` value is native and the session's
variables carry the marker. Web chat, ACP, Codex app-server and in-process
runner sessions never get the marker, because their hooks never reach the
route (Decision 4). Both Python sites forward when it returns true, run no
Python pass, and apply the returned response as they apply Python's. Rollback
to proxy makes the check false, so Python runs the passes again.

Consumers unchanged:
- `src/gobby/mcp_proxy/services/tool_proxy.py` — no-edit-reason: Its wrappers call both functions, whose signatures and return shapes are unchanged.
- `tests/mcp_proxy/services/test_direct_tool_session_activation.py` — no-edit-reason: Its sessions carry no ownership marker, so the Python pass still runs.
- `tests/workflows/test_seat_rules.py` — no-edit-reason: Its sessions carry no ownership marker, so the Python pass still runs.

**Acceptance:**
- 7.14.1 - The endpoint returns every response field of a 1.1 proxied-pass case, and non-loopback peers, a missing or wrong secret and the node door get not-found before body parsing. test: `crates/gdaemon/tests/hook_rule_pass.rs::rule_pass_endpoint_is_loopback_only_and_complete`.
- 7.14.2 - D2.8: across every session kind, both pass sites and all three route values, exactly one engine runs each pass. test: `tests/mcp_proxy/services/test_rule_pass_forward.py::test_one_engine_per_session`.
- 7.14.3 - D2.8: a Rust-owned session's before- and after-tool passes forward to Rust, run none in Python, and apply every field, `modified_input` included. test: `tests/mcp_proxy/services/test_rule_pass_forward.py::test_rust_owned_passes_forward_with_every_field`.
- 7.14.4 - The first committed Rust pass of a session sets the ownership marker once. test: `crates/gingress/src/pipeline/tests.rs::first_rust_pass_marks_ownership`.
- 7.14.5 - gdaemon carries this leaf's patch bump, lock and pin. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 7.15 Native default and the Rust drain [category: code] (depends: 7.14)
`kind: deliverable`

Targets:
- `crates/gingress/src/family.rs`
- `crates/gdaemon/src/hooks.rs`
- `crates/gdaemon/tests/hooks_flip.rs`
- `src/gobby/mcp_proxy/services/rule_pass_forward.py`
- `src/gobby/hooks/inbox_maintenance.py::*` — operation: delete — scope-reason: the Rust hub drain replaces the Python drain loop and inbox pruning
- `src/gobby/hooks/quarantine_retention.py::*` — operation: delete — scope-reason: the Rust hub drain owns quarantine retention
- `src/gobby/runner_maintenance/messaging.py::drain_hook_inbox_loop`
- `src/gobby/runner_maintenance/messaging.py::hook_quarantine_retention_loop`
- `src/gobby/runner_maintenance/__init__.py::*` — scope-reason: drop the two loop re-exports
- `src/gobby/runner_lifecycle_periodic.py::_default_loops`
- `src/gobby/runner_lifecycle_periodic.py::start_periodic_tasks`
- `src/gobby/runner_lifecycle.py::run_daemon`
- `src/gobby/runner.py::*` — scope-reason: drop the two hook task attribute annotations from `GobbyRunner`
- `src/gobby/runner_init/storage.py::open_storage_and_config`
- `src/gobby/runner_lifecycle_shutdown.py::_cancel_periodic_tasks`
- `src/gobby/runner_lifecycle_shutdown.py::stop_restart_recovery`
- `src/gobby/runner_periodic_task_attrs.py`
- `tests/hooks/test_inbox_temp_reaper.py::*` — operation: delete — scope-reason: its subject, inbox_maintenance, is deleted
- `tests/hooks/test_envelope_marker_retention.py::*` — scope-reason: drop the cases that import inbox_maintenance
- `tests/hooks/test_inbox.py::*` — scope-reason: drop the maintenance and quarantine-retention cases and keep the barrier cases
- `tests/test_runner_bin_freshness.py::*` — scope-reason: drop the drain loop argument
- `tests/test_runner_maintenance_startup.py::*` — scope-reason: remove the quarantine-loop scheduling test
- `tests/test_runner_shutdown.py::*` — scope-reason: drop the quarantine task attribute from the shutdown fixture
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: keep managed release pins coherent with the binaries this deliverable changes

**Research context:**
- **Gates.** rust-rule-evaluation D2.1 (1.2 replays equal on the Rust route),
  D2.2 (every 409 and 503 body, DR5's mappings), D2.4 (`hooks: proxy` returns
  the route to Python with no other family affected) and D2.9 (the 1.1
  hardcoded-behavior and response-field cases replay equal at this commit).
- **Soak.** This leaf dispatches only after the operator records a passing
  `gdaemon compare-report --since <compare start>` for a `hooks: compare` soak.
  The window is the runtime family's operator rule
  (`.gobby/plans/gdaemon-runtime-family.md` Q2): at least 24 hours, measured
  once from the cutover that starts compare. The window also needs at least one
  execute from each of the five CLIs and one request on each node and webhook
  row. No restart resets the clock.
- **Python loops.** `runner_lifecycle_periodic.py::start_periodic_tasks` spawns
  `hook-inbox-drain` (`runner._hook_inbox_task`) and `hook-quarantine-retention`.
  `runner_lifecycle.py::run_daemon` passes `drain_hook_inbox_loop`.
  `runner_init/storage.py` initializes both task attributes, and
  `runner_lifecycle_shutdown.py` cancels them. Only
  `runner_maintenance/messaging.py` imports `inbox_maintenance` and
  `quarantine_retention`.
- **Barrier.** `src/gobby/runner_hook_replay.py` calls
  `inbox.drain_hook_inbox_barrier`, which posts through the front door. It stays.
  Per-file claims are exclusive (3.5.2), and a timeout fences unresolved
  sessions.

`HooksFamily::default_backend` returns `Native`, and `HOOKS_ROUTE_DEFAULT`
becomes `"native"` in the same commit. `hooks::compose` starts 3.5's drain and
retention with a closure that sends each envelope through `LocalDispatch`, so
the drain follows the route value and rollback needs no Python loop. The Python
drain and quarantine-retention loops and their modules go.
`runner_lifecycle_shutdown.py` has 916 lines, so this deliverable makes one
split: the attribute-name tuple in `_cancel_periodic_tasks` is the move to
`src/gobby/runner_periodic_task_attrs.py` as `PERIODIC_TASK_ATTRS`, without the
two hook loop names, and
`stop_restart_recovery` drops `_hook_inbox_task` from its tuple.

**Granularity:** one leaf with 15 production files and five acceptance items.
The default flip, the Rust drain start and the Python loop removal must land
together so exactly one periodic drain runs.

Consumers unchanged:
- `tests/test_runner_lifecycle_periodic.py` — no-edit-reason: It names no hook loop, and the remaining loops keep their keys and spawn order.
- `tests/test_runner_skill_maintenance.py` — no-edit-reason: It names no hook loop, and the remaining loops keep their keys.
- `tests/test_runner_workflow_audit_maintenance.py` — no-edit-reason: It names no hook loop, and the remaining loops keep their keys.
- `tests/test_runner_approval_timeout.py` — no-edit-reason: It names no hook loop, and `start_periodic_tasks` keeps its signature.
- `tests/test_runner_resource_monitor.py` — no-edit-reason: It names no hook loop, and `start_periodic_tasks` keeps its signature.
- `tests/test_runner_lifecycle.py` — no-edit-reason: It names no hook loop or hook task, and both called functions keep their signatures.
- `tests/test_runner_pid_file.py` — no-edit-reason: It names no hook loop, and `run_daemon` keeps its signature.
- `src/gobby/runner_init/__init__.py` — no-edit-reason: It re-exports `open_storage_and_config`, whose signature is unchanged.
- `tests/config/test_restart_config_consumers.py` — no-edit-reason: It reads no hook task attribute.
- `tests/runner_init/test_config_runtime_startup.py` — no-edit-reason: It reads no hook task attribute.
- `tests/runner_init/test_runner_init_storage.py` — no-edit-reason: It reads no hook task attribute.
- `tests/test_runner_project_recovery.py` — no-edit-reason: It reads no hook task attribute.
- `tests/test_asgi_chat_shutdown.py` — no-edit-reason: It patches `_cancel_periodic_tasks` by name, which stays.
- `tests/test_runner_model_metadata_refresh.py` — no-edit-reason: It cancels a task that stays in `PERIODIC_TASK_ATTRS`.
- `src/gobby/runner_rollback.py` — no-edit-reason: It awaits `stop_restart_recovery(runner)`, whose signature is unchanged.

**Acceptance:**
- 7.15.1 - D2.1 and D2.9: with no `hooks` entry, every 1.2 envelope and sequence case and every 1.1 hardcoded-behavior and response-field case replays equal through gdaemon. test: `crates/gdaemon/tests/hooks_flip.rs::native_default_replays_the_corpora`.
- 7.15.2 - D2.2: every 409 and 503 body and DR5's four mappings match through gdaemon. test: `crates/gdaemon/tests/hooks_flip.rs::native_error_paths_match`.
- 7.15.3 - D2.4: `hooks: proxy` returns the route and the Rust drain's deliveries to Python, and no other family's routing changes. test: `crates/gdaemon/tests/hooks_flip.rs::proxy_setting_rolls_back_route_and_drain`.
- 7.15.4 - The Rust drain and the Python barrier never deliver one file twice, and Python schedules no drain or quarantine loop. test: `tests/hooks/test_inbox.py::test_barrier_coexists_with_the_rust_drain`.
- 7.15.5 - gdaemon carries this leaf's patch bump, lock and pin. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 7.16 Parity-ledger rows [category: docs] (depends: 7.15)
`kind: deliverable`

Targets:
- `docs/contracts/parity-ledger-status.d/remainder.json::*` — scope-reason: mark HTTP POST /api/hooks/execute delegated
- `docs/contracts/parity-ledger-status.d/comms-cron-hub-nodes.json::*` — scope-reason: mark the five node and webhook rows delegated and name #21571 on the three CLI rows
- `docs/contracts/parity-ledger.md`

**Research context:** statuses are `proxied`, `delegated` or
`dropped by decision #N`; notes cite evidence
(`scripts/generate_parity_ledger.py::render_ledger`). Owned rows: `HTTP POST
/api/hooks/execute` (remainder shard) and `HTTP GET /api/webhooks`, `HTTP POST
/api/webhooks/test`, `HTTP GET /api/nodes/detection-manifests`, `HTTP POST
/api/nodes/hooks/replay` and `HTTP POST /api/nodes/receipts` (comms shard). The
`CLI gobby webhooks`, `list` and `test` rows stay `proxied` with #21571 as
owner. The report and run rows belong to #21564 (Decision 13) and are not
touched. Template: `.gobby/plans/gdaemon-projects-files-docs-family.md` 6.3.

**Acceptance:**
- 7.16.1 - The execute row is `delegated` with its native handler and 7.15's test as evidence. file: `docs/contracts/parity-ledger-status.d/remainder.json`.
- 7.16.2 - The five node and webhook rows are `delegated` with their P6 handlers and tests, and the three CLI rows name #21571. file: `docs/contracts/parity-ledger-status.d/comms-cron-hub-nodes.json`.
- 7.16.3 - The regenerated ledger matches the inventory. test: `tests/test_parity_ledger.py::test_committed_ledger_matches_current_public_inventory`.

### 7.17 Delete the Python hook route [category: refactor] (depends: 7.16)
`kind: deliverable`

Targets:
- `src/gobby/servers/routes/mcp/hooks.py::*` — operation: delete — scope-reason: the route is native
- `src/gobby/servers/routes/mcp/hook_hold_open.py::*` — operation: delete — scope-reason: only the deleted route used it
- `src/gobby/servers/routes/mcp/hook_responses.py::*` — operation: delete — scope-reason: only the deleted route used it
- `src/gobby/hooks/receipt_redelivery.py::*` — operation: delete — scope-reason: only the deleted route used it
- `src/gobby/hooks/startup_claim_preflight.py::*` — operation: delete — scope-reason: only the deleted route and receipt_redelivery used it
- `src/gobby/servers/routes/mcp/__init__.py::*` — scope-reason: drop the hooks router export
- `src/gobby/servers/routes/__init__.py::*` — scope-reason: drop the hooks router re-export
- `src/gobby/servers/_app_routes.py::register_routes`
- `src/gobby/servers/auth_service.py::_HOOKS_EXECUTE_PATH`
- `src/gobby/servers/auth_service.py::_AGENT_CAPABILITY_MATRIX`
- `tests/hooks/test_staged_effects_receipt_route.py::*` — scope-reason: delete or rehome cases by the rule below
- `tests/servers/routes/mcp/test_hook_cancelled_receipt_safety.py::*` — scope-reason: delete or rehome cases by the rule below
- `tests/servers/routes/mcp/test_hook_envelope_lease_release.py::*` — scope-reason: delete or rehome cases by the rule below
- `tests/servers/routes/mcp/test_hook_session_metadata.py::*` — scope-reason: delete or rehome cases by the rule below
- `tests/servers/routes/mcp_endpoints/test_execution_session_end_cleanup.py::*` — scope-reason: delete or rehome cases by the rule below
- `tests/servers/routes/test_hooks_agy_dispatch.py::*` — scope-reason: delete or rehome cases by the rule below
- `tests/servers/routes/test_hooks_droid_dispatch.py::*` — scope-reason: delete or rehome cases by the rule below
- `tests/servers/routes/test_hold_open_gate.py::*` — scope-reason: delete or rehome cases by the rule below
- `tests/servers/test_hook_machine_binding.py::*` — scope-reason: delete or rehome cases by the rule below
- `tests/servers/test_mcp_routes.py::*` — scope-reason: drop the hook-route and hold-open cases
- `tests/servers/test_native_ws_bridge.py::*` — scope-reason: drop the hold-open import
- `tests/servers/test_managed_clear_identity.py::*` — scope-reason: drive HookManager directly instead of the deleted route
- `tests/servers/test_http_endpoints.py::TestHooksEndpoints`
- `tests/servers/test_http_server.py::*` — scope-reason: delete TestHooksEndpoint and add the unmounted-route check
- `tests/servers/test_auth_service.py::test_hooks_route_requires_run_identity`
- `tests/communications/test_telegram_questions.py::*` — scope-reason: drop the hold-open import
- `tests/adapters/test_capabilities.py::*` — scope-reason: drop the hook_responses import
- `tests/hooks/test_node_origin_path_scope.py::*` — scope-reason: drop the hook_responses import
- `tests/hooks/test_node_origin_shell.py::*` — scope-reason: drop the hook_responses import
- `tests/hooks/test_pending_message_reservations.py::*` — scope-reason: drop the receipt_redelivery import
- `tests/hooks/test_receipt_session_identity.py::*` — scope-reason: delete or rehome cases by the rule below
- `tests/hooks/test_startup_claim_preflight.py::*` — operation: delete — scope-reason: its subject is deleted
- `tests/servers/websocket/chat/test_agy_backend.py::*` — scope-reason: drop the startup_claim_preflight import
- `docs/contracts/parity-ledger.md`

**Research context:** production importers come from
`gcode grep -w <symbol> -l -g 'src/**'` per Decision 7. `hooks.py` is imported
by `routes/mcp/__init__.py`; `hook_hold_open.py`, `hook_responses.py` and
`receipt_redelivery.py` only by `hooks.py`; and `startup_claim_preflight.py` by
`hooks.py` and `receipt_redelivery.py`. Kept because they have surviving
importers: `adapter_execution`
(`src/gobby/workflows/engine/mcp_injections.py`), `health_gate`,
`agent_run_ingress`, `runtime_compat`, `envelope_dedupe`, `replay_decision`,
`replay_fence`, `inbox`, `inbox_envelopes`, `inbox_lifecycle` and
`src/gobby/code_index/trigger.py` (Python handlers of Python-owned sessions
call it). The two `_AGENT_CAPABILITY_MATRIX` entries go with the routes they
admit: the execute entry here, the webhook entry in 7.18. A deleted surface's
`delegated` row stays in the ledger through #24149's leaf 4.1 (Cross-plan
edges); this leaf regenerates the ledger and checks it unchanged.

Test rule: delete a case whose subject is a deleted module or the deleted
route; its Rust port's test replaces it. Keep a case whose subject survives,
dropping only the deleted import or driving `HookManager.handle` directly.

Consumers unchanged:
- `src/gobby/servers/app_factory.py` — no-edit-reason: It calls `register_routes(app, server)`, whose signature is unchanged.
- `tests/test_wiki_retirement_contract.py` — no-edit-reason: It mounts the routes through the unchanged signature and checks only wiki and code-index paths.
- `tests/servers/routes/test_runtime_handshake.py` — no-edit-reason: It reads only the handshake entry of the matrix, which stays.

**Acceptance:**
- 7.17.1 - No module under `src/` imports a deleted module, and the app mounts no `/api/hooks` route. test: `tests/servers/test_http_server.py::test_hook_routes_are_not_mounted`.
- 7.17.2 - The rewritten managed-clear cases pass against `HookManager.handle`. test: `tests/servers/test_managed_clear_identity.py::test_managed_clear_identity_survives_hook_manager`.
- 7.17.3 - The ledger still lists the execute row as `delegated` and matches the inventory. test: `tests/test_parity_ledger.py::test_committed_ledger_matches_current_public_inventory`.

### 7.18 Delete the Python node and webhook routes [category: refactor] (depends: 7.17)
`kind: deliverable`

Targets:
- `src/gobby/servers/routes/node_hooks.py::*` — operation: delete — scope-reason: both node hook routes are native
- `src/gobby/servers/routes/mcp/webhooks.py::*` — operation: delete — scope-reason: both webhook routes are native
- `src/gobby/servers/routes/node_runs.py::*` — scope-reason: remove the detection-manifest handler and take `_node_machine` from the deleted module
- `src/gobby/servers/routes/mcp/__init__.py::*` — scope-reason: drop the webhooks router export
- `src/gobby/servers/routes/__init__.py::*` — scope-reason: drop the webhooks router re-export
- `src/gobby/servers/_app_routes.py::register_routes`
- `src/gobby/servers/auth_service.py::_AGENT_CAPABILITY_MATRIX`
- `tests/servers/test_node_hook_routes.py::*` — operation: delete — scope-reason: its routes are deleted
- `tests/servers/test_node_run_routes.py::*` — scope-reason: delete the detection-manifest case and add the surviving-routes check
- `tests/servers/test_http_endpoints.py::TestWebhooksEndpoints`
- `tests/servers/test_mcp_routes.py::*` — scope-reason: drop the webhook-route cases
- `docs/contracts/parity-ledger.md`

**Research context:** `node_hooks.py` is imported by `_app_routes.py` and
`node_runs.py`. `node_runs.py` imports `_node_machine`, which moves into it.
`webhooks.py` is imported by `routes/mcp/__init__.py`. The report and run
routes stay Python-served (Decision 13). The webhook `_AgentRoute("GET",
"/api/webhooks", False)` entry goes.

Consumers unchanged:
- `src/gobby/servers/app_factory.py` — no-edit-reason: It calls `register_routes(app, server)`, whose signature is unchanged.
- `tests/test_wiki_retirement_contract.py` — no-edit-reason: It mounts the routes through the unchanged signature and checks only wiki and code-index paths.
- `tests/servers/routes/test_runtime_handshake.py` — no-edit-reason: It reads only the handshake entry of the matrix, which stays.

**Acceptance:**
- 7.18.1 - The app mounts neither node hook route, the detection-manifest route nor `/api/webhooks`, and the report and run routes still answer. test: `tests/servers/test_node_run_routes.py::test_node_run_routes_survive_hook_route_removal`.
- 7.18.2 - The five rows stay `delegated` and the ledger matches the inventory. test: `tests/test_parity_ledger.py::test_committed_ledger_matches_current_public_inventory`.

## E1 Verification and review gates
`kind: verification`

Before each review, run
`uv run gobby plans validate .gobby/plans/s2-11-hub-hooks-rules.md -p /Users/josh/Projects/gobby`.
The narrative draft has no M1 and has not run expansion validation. After the
adversary derives the manifest, run the same base validation and
`uv run gobby plans validate .gobby/plans/s2-11-hub-hooks-rules.md -p /Users/josh/Projects/gobby --mode expansion`.
No planned check is claimed as validation evidence.

## V1 Plan Changelog
`kind: framing`

2026-10-28 — Initial narrative by Plan Writer gobby#18278 for planning task
#25256. No consensus or manifest is claimed.
