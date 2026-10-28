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
   moves into `crates/gingress/src/ledger.rs`; the gdaemon node arm imports it.
   The hub claims every enveloped request in its own ledger root
   (`hooks/inbox/processed`). The hub keeps one STOP replay epoch file per
   machine; a node keeps its single file.
   - Rejected: a second, hub-only ledger.
   - The Rust hub inbox drain (3.5) calls the envelope handler in process.
     It is wired, and the Python drain deleted, in 7.13, so the two drains never
     run together.
6. **Compare soak for a mutating route.** For non-GET methods the front door
   proxies once and then calls `RouteFamily::check_proxied`, which sees only
   post-state. A new gcore default method, `RouteFamily::prepare_proxied`
   (no-op default), is awaited before the proxied leg. The `hooks` family runs
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
| 3 | `tdd_paths.py::_git_out` | `node-read`, `degrade-existing` | 2.10 |
| 4 | `code_navigation_recovery.py::_git_ignored` | `node-read`, `degrade-existing` | 2.6 |
| 5 | `_normalization_git.py::_positional_checkout_is_path` | `node-read`, `degrade-existing` | 2.4 |
| 6 | `project_context.py::resolve_hook_project_context` | node checkout rows, `fail-closed` | 5.2 |
| 7 | `WorkflowHookHandler._resolve_project_path` | node checkout row | 2.16 |
| 8 | `_path_scope.py` primitives, `classify_git_mutation` | `node-read`, `fail-closed` | 2.6 |
| 9 | `condition_helpers_paths.py::write_paths_within` | `node-read`, `fail-closed` | 2.8 |
| 10 | `enforcement/blocking.py::plan_write_paths_allowed` | `node-read`, `fail-closed` | 2.14 |
| 11 | `monolith_guard.py::_projection_for_path` | `node-read`, `fail-closed` | 2.9 |
| 12 | `monolith_guard.py::outstanding_monolith_paths` | `node-read` batched, `fail-closed` | 2.9 |
| 13 | `rust_test_evidence.py::_read_absolute_text` | `node-read`, `fail-closed` | 2.10 |
| 14 | `code_navigation_recovery.py::_verified_source_line_count`, `_ignore_decision` | `node-read`, `degrade-existing` | 2.6 |
| 15 | `task_claim_state.py::resolve_edit_pair` | `node-read`, `degrade-existing` | 2.9 |
| 16 | `_tool.py::_notify_code_index` | `skip` | 5.9 |
| 17 | `commit_guard.py::_live_path_owners` | owners on the event's machine only | 2.10 |
| 18 | `shell_execution.py::_bounded_read`, `_normalization_operands.py::_file_transfer_paths` | `node-read`, `fail-closed` | 2.6 |
| 19 | `_misc.py` worktree create and remove | `skip`, allow before any hub effect | 5.11 |
| 20 | Claude, Grok and Codex spill readers | `skip` | 4.2, 4.3, 4.4 |
| 21 | `proxy_hooks.py::_run_rtk_proxy` | `skip` | 2.13 |
| 22 | `run_command_effects.py::_apply_run_command` | `skip` | 2.13 |
| 23 | `found_work_gate.py::_terminal_failures` | `no-change` | 2.9 |
| 24 | `validation_detection.py::load_project_validation_detection` | `no-change` | 2.9 |
| 25 | `startup_claim_preflight.py::_resolve_or_register_session` | `no-change`, node envelope refused | 3.6 |
| 26 | `observer_plan_mode.py` Codex transcript read | `no-change` | 2.16 |
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
from the normalizer entry points 2.2-2.7 port: `tokenize_shell_command`,
`scan_shell_command` and `extract_heredoc_bodies` (`_normalization_shell`),
`decode_ansi_c_escape` (`_ansi_c`), `apply_path_scope_metadata`
(`_path_scope`), `normalize_tool_fields` (`_normalization_tools`),
`classify_git_mutation` (`_normalization_git`), `executable_command_subjects`
and `command_patterns_match` (`command_matching`),
`blocks_direct_provider_launch` (`provider_launch_guard`) and
`rule_matches_agent` (`selectors`). Command strings come from the corpus
events and the existing cases in `tests/workflows/test_command_matching.py`.

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
- one case per hardcoded behavior 2.14 and 2.15 list.

**Granularity:** one leaf with seven acceptance items. The case schema, the
exporter that is its oracle and the coverage checks are one artifact; a corpus
without its exporter cannot be re-recorded.

**Acceptance:**
- 1.1.1 - The manifest and every case load at `schema_version` 1, and a stale version is rejected. test: `tests/contracts/test_rules_corpus.py::test_loader_rejects_stale_schema_version`.
- 1.1.2 - All 15 effect types and both delivery classes appear in some case's expected effect log, using synthetic rule bodies only. test: `tests/contracts/test_rules_corpus.py::test_corpus_covers_effect_types_and_delivery`.
- 1.1.3 - Every required scenario case exists, including both F4 recheck cases. test: `tests/contracts/test_rules_corpus.py::test_corpus_has_required_scenarios`.
- 1.1.4 - The Python engine reproduces every case's `expect` block. test: `tests/contracts/test_rules_corpus.py::test_python_engine_replays_corpus`.
- 1.1.5 - Every hardcoded behavior 2.14 and 2.15 list has a case, and every response field appears set in some case. test: `tests/contracts/test_rules_corpus.py::test_corpus_covers_hardcoded_behavior_and_response_fields`.
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

## P3: Ingress envelope layer in `gobby-ingress`
`kind: framing`

Create the `hooks` family and the shared ledger, and port envelope
validation, claim and replay, receipts, the hub inbox drain and adapter
execution.

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
