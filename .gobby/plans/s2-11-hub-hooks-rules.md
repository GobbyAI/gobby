Plan artifact: `.gobby/plans/s2-11-hub-hooks-rules.md`

# Gobby 1.0 Stage 2: S2.11 hub hook ingress and rule engine

**Plan ID:** s2-11-hub-hooks-rules

## Overview
`kind: framing`

#24151 is the hub half of S2.11. It moves hub hook handling from Python to
Rust: the rule engine, `POST /api/hooks/execute`, the five CLI adapters, the
hook event pipeline and its handlers, the node hook and report routes, and the
webhook routes. The node half (#24150) already serves node-local ingress in
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
   P7 adapter and flip deliverables carry family dependencies. No new Python
   route is added.
   - The one Python change outside deletion is the DR6 forward (7.11): from the
     flip until #21570, the Python MCP proxy sends before- and after-tool rule
     passes of Rust-owned sessions to a backend-only gdaemon pass endpoint. It
     is a Python client of a Rust endpoint, required by the DR6 session-ownership
     ruling, and is deleted by #21570.
3. **Two crates.** `gobby-workflows` (engine, normalization, engine seams) and
   `gobby-ingress` (one `hooks` RouteFamily). Each follows the gruntime pattern:
   `publish = false`, gobby-core features `postgres`, `postgres-pool` and
   `route-family`, and wiring in `crates/gdaemon/src/serve.rs::run` beside
   `RuntimeFamily` inside the pool-gated block. The `hooks` family claims
   `/api/hooks`, `/api/webhooks`, `/api/nodes/hooks`, `/api/nodes/receipts`,
   `/api/nodes/report`, `/api/nodes/runs` and `/api/nodes/detection-manifests`.
   `/api/nodes/checkouts` belongs to #21585/#24951 and stays unclaimed.
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
   - The Rust hub inbox drain (3.5) calls the envelope handler in process.
     It is wired, and the Python drain deleted, in 7.13, so the two drains never
     run together.
6. **Compare soak for a mutating route.** For non-GET methods the front door
   proxies once and then calls `RouteFamily::check_proxied`, which sees only
   post-state. A new gcore default method, `RouteFamily::prepare_proxied`
   (no-op default, 3.1), is awaited before the proxied leg. The `hooks` family runs
   the Rust pass in record mode against pre-state: no writes and no external
   effects, with an in-pass overlay. `check_proxied` then compares the decision,
   reason, context, system message, modified input and staged receipt fields.
   A pass that depends on an `mcp_call` result compares only up to that call.
7. **Python deletion boundary.** 7.13 deletes the route and the modules only
   ingress uses, each only when `gcode grep -w <symbol> -l -g 'src/**'` shows no
   surviving importer. `HookManager` and the event handlers stay (Decision 4).
8. **Node session registration.** Registering a node session that lacks its
   pre-created row (hub-mode Decision 18, the fourth use of the key-verified
   foreign-machine path) sits behind the sessions seam; its adapter is in 7.1
   (depends: #21562).
9. **Checkout reads.** The checkout-read seam trait and its local
   implementation live in `gobby-workflows`, because the engine is the main
   reader and `gobby-ingress` depends on it. The node implementation is a gdaemon
   adapter over the node channel (7.8, depends: #24521). Every Site Disposition
   row below keeps its outcome in Rust.
10. **Code-index trigger.** The native after-tool path calls
    `gobby_codeindex::jobs::trigger::EditTrigger::notify_file_changed` through
    `HubJobs::edit_trigger()` for the hub's own checkouts (5.9, depends:
    #24705). Node events skip it (Site row 16).
11. **Bundled YAML conformance.** Every bundled rule, agent, workflow and
    pipeline body must load in the Rust loader: schema parse, condition parse,
    template compile and pattern compile. The engine corpus (1.1) uses synthetic
    rule bodies that exercise engine features; it never asserts per-rule
    behavior.
12. **Carried decisions DR1-DR8** from rust-rule-evaluation, minus its latency
    gate: DR5 fails closed in the response; DR6 serializes passes per session
    in FIFO order and takes the shared advisory locks `SessionVariableMutation`
    (950) and `AgentStepInstanceMutation` (875); DR7 reads definitions at one
    revision per pass; DR8 records outcome counters.

## Debt not to port
`kind: framing`

Josh's port rule (2026-10-24): parity matches contracts, not accidents. Each
row names the Python behaviour, its evidence and the disposition.

| Python behaviour | Evidence | Disposition |
| --- | --- | --- |
| One hub-wide `.stop_replay_epoch` file | node-halves Constraints | Drop: one epoch per machine (Decision 5) |
| Two envelope ledgers (Python `envelope_dedupe.py`, Rust node `Ledger`) | `src/gobby/hooks/envelope_dedupe.py`, `crates/gdaemon/src/nodes/hooks/ledger.rs` | Drop: one shared ledger (Decision 5) |
| Python drain POSTs envelopes back through the front door with the operator token | `src/gobby/hooks/inbox.py::_post_envelope` | Drop: the Rust drain calls the envelope handler in process (3.5) |
| An 862-line route module | `src/gobby/servers/routes/mcp/hooks.py` | Drop: one Rust module per concern (P3) |
| Provider inferred from inner payload fields | memory a39d0411, #17173 | Keep the fix: `source` alone selects the adapter (4.1) |
| Seven `RuleEffect` types no bundled rule uses: `set_display_content`, `rewrite_input`, `set_permission_response`, `set_retry`, `set_watch_paths`, `set_worktree_path`, `set_elicitation` | `src/gobby/workflows/definitions.py::RuleEffect` (F2) | Keep: they are the rule-authoring schema, adapters already translate each field, and synthetic corpus cases pin them (2.12) |
| Per-bundled-rule behaviour tests | Josh, 2026-10-27 | Drop: schema and loader conformance only (Decision 11) |
| TDD-gate evidence code left after #23341 retired the gate | `src/gobby/workflows/tdd_paths.py` is gone; nothing in `src/` imports `src/gobby/workflows/rust_test_evidence.py` | Drop: not ported (Site rows 3 and 13) |

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
  crates; the gcore change in 7.10 bumps every gcore dependent.
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
| #21562 Sessions | blocks | 7.1, 7.12 |
| #21560 Tasks | blocks | 7.2 |
| #21564 Agents | blocks | 7.3 |
| #21566 MCP multiplexer | blocks | 7.4 |
| #21580 Skills | blocks | 7.5 |
| #21586 Code-index orchestration | blocks | 7.6 |
| #21559 Config | blocks | 7.7 |
| #24521 Node checkout.read handler | blocks | 7.8; moves down from #24151 to 7.8 at expansion |
| #24705 Edit trigger entry point | blocks | 5.9 |
| #21567 Workflows | blocked by P2 under B | set by the Orchestrator at expansion |
| #24884 Native MCP gateway | blocked by #24151 | unchanged; consumes `gobby-workflows` |
| #24713 Code-index 9.7 | blocked by #24151 | unchanged |
| #21569 S2.11 holder | blocked by #24151 | unchanged |
| #22946 rust-rule-evaluation | closes superseded under B | Orchestrator |
| #21570 MCP front door flip | deletes the DR6 forward | 7.11 |
| #21571 CLI | owns the `gobby webhooks` verbs | 7.14 records it |

## Site Disposition port
`kind: framing`

hook-envelope-file-facts Decision 12 hands this plan its 30-row Site
Disposition table (`.gobby/plans/hook-envelope-file-facts.md`). Each row keeps
its node-event outcome and its unanswered outcome in Rust. The owning
deliverable carries one acceptance item per row.

| # | Python site | Outcome kept | Owner |
| --- | --- | --- | --- |
| 1 | `adapter_execution.py::run_adapter_hook` | sets the delivery origin | 3.6 |
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
| 16 | `_tool.py::_notify_code_index` | `skip` | 5.9 |
| 17 | `commit_guard.py::_live_path_owners` | owners on the event's machine only | 2.10 |
| 18 | `shell_execution.py::_bounded_read`, `_normalization_operands.py::_file_transfer_paths` | `node-read`, `fail-closed` | 2.7 |
| 19 | `_misc.py` worktree create and remove | `skip`, allow before any hub effect | 5.11 |
| 20 | Claude, Grok and Codex spill readers | `skip` | 4.2, 4.3, 4.4 |
| 21 | `proxy_hooks.py::_run_rtk_proxy` | `skip` | 2.15 |
| 22 | `run_command_effects.py::_apply_run_command` | `skip` | 2.15 |
| 23 | `found_work_gate.py::_terminal_failures` | `no-change` | 2.12 |
| 24 | `validation_detection.py::load_project_validation_detection` | `no-change` | 2.12 |
| 25 | `startup_claim_preflight.py::_resolve_or_register_session` | `no-change`, node envelope refused | 3.6 |
| 26 | `observer_plan_mode.py` Codex transcript read | `no-change` | 2.18 |
| 27 | `terminal_context.py::_is_fresh_seat_tui` | `no-change`, returns before process inspection | 5.4 |
| 28 | Droid Notification wait branch | `skip` | 4.5 |
| 29 | Droid idle_prompt cancel branch | `skip` | 4.5 |
| 30 | AGY prompt backfill | `skip` | 4.6 |

## P1: Contract corpora
`kind: framing`

Freeze executable characterization of the Python engine, the ingress contract
clauses not yet recorded, and each adapter's translation before any Rust
replaces them. Python is the oracle until 7.13.

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

## P2: Rule engine in `gobby-workflows`
`kind: framing`

Port the definitions loader, normalization, condition language, helpers,
effects and the pass driver behind engine seams, replaying the 1.1 corpus.
The crate links into no binary until 7.9. Every deliverable edits
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
- **Bundled conformance (Decision 11).** `bundled_conformance.rs` reads every
  YAML file under `src/gobby/install/shared/workflows/rules/`, applies the sync
  defaults and the `{{ gobby_bin }}` substitution, and parses each body. It also
  parses the rule selectors and step workflows of every file under
  `src/gobby/install/shared/workflows/agents/` and every body under
  `src/gobby/install/shared/workflows/variables/`. Pipelines belong to #21567's
  loader. 2.13 extends this test to compilation.

**Granularity:** one leaf with seven production files, counting both
`Cargo.toml` files. The manifest, body, repository and cache form one loader;
the cache cannot be tested without the repository it validates.

**Acceptance:**
- 2.1.1 - The crate is a workspace member, clippy passes with `-D warnings`, and no other crate depends on it yet. file: `crates/gworkflows/Cargo.toml`.
- 2.1.2 - Every rule body in the 1.1 corpus parses, and a body Python rejects is skipped and counted while the rest of the pass loads. test: `crates/gworkflows/src/definitions/tests.rs::corpus_bodies_parse_and_rejects_are_counted`.
- 2.1.3 - The repository returns global rows plus same-project rows, deduped and ordered, and an event with no project id loads global rows only. test: `crates/gworkflows/tests/definitions_repo.rs::lists_global_and_same_project_rows_in_order`.
- 2.1.4 - Advancing the `rules` revision makes the next pass reload, and a failed revision read returns `DefinitionsUnavailable` without using the cache. test: `crates/gworkflows/tests/definitions_repo.rs::cache_reloads_on_revision_and_fails_closed`.
- 2.1.5 - Every bundled rule body, agent selector and step workflow, and variable body parses. test: `crates/gworkflows/tests/bundled_conformance.rs::every_bundled_body_parses`.

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
rules to any implementation. The node implementation is 7.8's.
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
`variables/merge.rs`. #24884's gateway, 7.11's pass endpoint and the ingress
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
adapters or the pipeline; 5.13 passes the real execution.

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
the proxied leg for non-GET/HEAD requests. No family overrides it until 7.10.

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
members. `family::HooksFamily` names `hooks`, claims the seven prefixes in
Decision 3, defaults to `Proxy`, and routes `POST /api/hooks/execute` to
`execute`. `execute::request` ports every pre-claim step above in Python's
order; the pipeline-readiness check reads a readiness flag the caller passes.
No binary links the crate until 7.9.

**Acceptance:**
- 3.2.1 - The family claims exactly the seven prefixes, defaults to proxy, and leaves `/api/nodes/checkouts` unclaimed. test: `crates/gingress/src/execute/tests.rs::family_claims_its_prefixes_only`.
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
  `drain_hook_inbox_barrier` before agent restart classification; 7.13 keeps
  that barrier working.

Port them as `inbox`. The drain claims each inbox file atomically, calls the
envelope handler in process with the envelope's headers and body, settles or
quarantines the file by the response, and prunes processed markers and
quarantine by age. It is built here and started in 7.13.

**Acceptance:**
- 3.5.1 - The drain executes each spooled envelope once through the in-process handler, settles it by response, and quarantines a 403 without replay. test: `crates/gingress/src/inbox/tests.rs::drain_settles_and_quarantines_like_python`.
- 3.5.2 - A file another drain has claimed is skipped, and a crashed claim is restored. test: `crates/gingress/src/inbox/tests.rs::inbox_claims_are_exclusive_and_restorable`.
- 3.5.3 - Inbox, temp-file and quarantine pruning follow Python's ages and bounds. test: `crates/gingress/src/inbox/tests.rs::pruning_matches_python`.

**Verification:** planned:
`cargo nextest run -p gobby-ingress` and
`cargo clippy -p gobby-ingress --all-targets -- -D warnings`.

## P4: CLI adapters
`kind: framing`

Port the hook event model and the Claude, Codex, Grok, Droid and AGY adapters,
replaying the 1.3 corpus.

## P5: Event pipeline and handlers
`kind: framing`

Port the `HookManager` pipeline and the event handlers over pipeline seams.

## P6: Node and webhook routes
`kind: framing`

Port the node hook, receipt, report and run routes and the webhook routes.

## P7: Native composition, flip and Python deletion
`kind: framing`

Implement each seam over its native family, compose the family in gdaemon,
soak in compare mode, flip to native default, record the ledger rows, and
delete the Python route and ingress-only modules.

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
