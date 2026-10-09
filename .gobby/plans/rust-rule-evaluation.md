Plan artifact: `.gobby/plans/rust-rule-evaluation.md`

# Rule evaluation in Rust: the gobby-workflows rule engine

**Plan ID:** rust-rule-evaluation

## Overview
`kind: framing`

Rule evaluation runs inside the Python daemon process today.
`EvaluationMixin._run_rule_loop` (`src/gobby/workflows/engine/evaluation.py`) hands
each pass to the 16-thread `rule-loop` pool, which runs it under a fresh
`asyncio.run`. Across 155 retained GIL captures, not one rule sample was taken on the
asyncio loop thread. "Off the main runner" therefore means out of the Python process.
The gain is removing rule work's share of the GIL, and, when the hooks route moves
with it, the rest of the hook path's share. The loop thread itself gets no relief.

This plan ports the rule engine to Rust as the first module of the `gobby-workflows`
family crate, `crates/gworkflows`. Rules leave Python only when
`POST /api/hooks/execute` moves to the Rust daemon under S2.11 (#21569). P1 freezes the
two contracts the port must match: a rule-pass parity corpus and a hooks/execute
contract corpus. P2 builds the engine behind seam traits with in-memory test
implementations. P1 and P2 have no unmet Stage 2 dependency, so they start now under
ROADMAP decision 21. **They give no GIL relief by themselves.** Relief starts at the
D2 flip. The deferred sections record the work that waits on other Stage 2 families:
seam adapters, the hooks route flip with its gates and GIL acceptance, the remaining
three consumers, and deleting the Python engine.

Parent epic: #21567 (S2.9) under #21544. Planning task: #22946. The Orchestrator
gobby#14972 approved decisions DR1-DR4 on 2026-10-08 at 19:46 CDT. Josh approves the
exact reviewed revision before expansion, and nothing here is implemented before that
approval.

## Evidence and uncertainty
`kind: framing`

All figures come from `.gobby/plans/research/rule-evaluation-evidence-2026-10-09.md`.
That file embeds each script and its verbatim output, and lists its inputs under
`#inputs`. No load was generated and no instrumentation was added to collect them.

| Finding | Figure | Evidence section |
| --- | --- | --- |
| Rule work share of GIL samples, per capture | mean 6.3%, p50 5.6%, p90 11.2%, max 18.4% | `#gil-share-by-bucket` |
| Rule samples on the asyncio loop thread | 0 of 40,028 | `#gil-share-by-bucket` |
| Other hook-path work share, per capture | mean 22.6%, p90 31.3%; 13,737 samples on the loop thread | `#gil-share-by-bucket` |
| Inside rule work | per-pass loop scaffolding 25.6%, shell normalization 24.4%, database pool 16.0%; rule logic proper (conditions 12.6%, command matching 6.8%, templates 3.8%, selectors 3.7%) 26.9% | `#gil-share-by-bucket` |
| Inside other hook-path work | database pool 33.4%, session activation 14.2%, inbox and envelope 14.2%, hook manager 12.9%, normalization 3.5% | `#gil-share-by-bucket` |
| Rule samples with no caller frame | 63.4%; 5,602 carry only the shared `workflows/hooks.py` frame; 7 carry hook-route frames; none carries an MCP proxy, web chat, or agent runner frame | `#rule-gil-share-by-caller` |
| Step-workflow tool enforcement | 11.3% of rule samples; step instance lookups 5.5% | `#engine-component-shares-non-exclusive` |
| Passes with a matched allowing rule that also matched a mutating rule | `before_tool` 97.4% of 50,339; `after_tool` 99.6% of 25,656; `before_agent` 100% of 20,790; `stop` 89.6% of 983; `session_start` 100% of 943; `pre_compact` 100% of 610 | `#matched-rule-passes-in-the-allow-audit` |
| `rtk-command-rewrite` alone | without it, `before_tool` passes fall to 14,970 (87.7% mutating), so at least 35,369 Bash calls matched only that rule | `#matched-rule-passes-in-the-allow-audit` |
| Turn-start rule latency | `before_agent` p90 229 ms, p99 2,548 ms; `surface-memories-on-turn-start` (an `mcp_call`) matches 95% of turn starts | `#matched-rule-passes-in-the-allow-audit` |
| Rules needing a cross-family seam | 44 of 219 bundled rules; 175 need only the event and session variables | `#cross-family-seams-per-rule` |
| Condition and template surface | 54 helper names plus 19 added by templating; 130 Jinja blocks with tags `set`, `if`, `elif`, `else`, `for` | `#condition-and-template-surface` |
| Shell normalization surface | six `src/gobby/hooks/` modules, 2,664 lines; `provider_launch_guard.py` (762 lines) parses inline Python with `ast.parse` | `#shell-normalization-surface`, `#repository-counts` |
| Delivery and patterns | 17 `on_receipt` declarations in 9 rule files; 28 rule files use regex lookarounds | `#repository-counts` |
| `mcp_call` targets | `gobby-memory` 5, `gobby-review-learning` 5, `gobby-workflows` `run_pipeline` 1, `gobby-skills` `list_hubs` 1 | `#repository-counts` |
| ghook on a 503 retry or a failed delivery | continues the host CLI for every tool hook, because no CLI marks tool hooks critical | `#repository-counts` |

Uncertainty, stated so no later section overclaims:

1. **The zero-match share is unmeasured.** The allow audit records only rules whose
   rule-level `when` matched and that did not block. The slow-hook phase logs ended
   with #22866 (last record 2026-10-02 10:33). `/api/admin/metrics` needs the daemon
   credential, which the research seat may not read. No source counts the events no
   rule matches. DR8 adds this measurement.
2. **How rule work splits between its four consumers is unmeasured.** Pool threads
   carry no caller frame. The plan therefore promises no per-consumer GIL figure, and D2
   measures what the hooks flip removes.
3. **Captures are not load-controlled.** PIDs and load are not recorded per file, and
   the window mixes #23359's before, after, and light-load runs. Per-capture
   percentages are the comparable unit, not absolute sample counts.
4. **The mutating share is an upper bound.** Effect-level `when` clauses are invisible
   in the log, so a matched rule counts as mutating when any of its effects could
   mutate.
5. **Seam counts are textual.** A rule needs a seam when its definition text names a
   seam-backed helper or effect.

## Decision Record
`kind: framing`

The Orchestrator gobby#14972 approved DR1-DR4 on 2026-10-08 19:46 CDT. DR5-DR8 follow
from them and from the task's correctness requirements.

- **DR1: Crate.** The engine is the first module of `crates/gworkflows`, package
  `gobby-workflows`: the one crate for the S2.9 family (rules, workflows, pipelines,
  build, validation), per ROADMAP decision 16. **This departs from
  `crates/AGENTS.md` in two ways, stated plainly.** First, the crate is created
  before #21567 starts, which decision 21 permits for dependency-ready work. Second,
  it exports no `RouteFamily` yet. The rule and workflow routes arrive with the rest
  of #21567, and the crate is not a placeholder, because it ships the engine.
  Rejected alternatives:
  - A rules-only `crates/grules`. It splits one ROADMAP family across two crates,
    against decision 16.
  - The engine in `gcore`. That puts workflow semantics into every binary that links
    `gobby-core`.
  - The engine in the hooks family crate. Rules have four consumers, and the hooks
    route is only one of them.
- **DR2: Flip boundary.** Rules leave Python only when `POST /api/hooks/execute`
  moves to the Rust daemon at S2.11. There is no rules-only RPC. Evidence: rule work
  is 6.3% of GIL samples per capture, against 22.6% for the rest of the hook path.
  Rejected alternatives:
  - **A rules-only RPC from Python to Rust.** The 22.6% hook-path share stays in
    Python. Every pass gains a process hop. The 44 seam-needing rules would need
    mid-pass callbacks into Python, including the `mcp_call` on 95% of turn starts.
  - **A Python worker process.** The task prohibits it, and it adds IPC around the
    same Python work.
  - **A no-match fast path in ghook.** ghook runs inside the SRT sandbox with no
    datastore credentials (ROADMAP decision 9). It could answer only events no rule
    matches, and that share is unmeasured. Of passes where any rule matched, 87.7% to
    100% also matched a mutating rule.
  - **Faster Python predicates.** Condition evaluation is 12.6% of rule samples, about
    0.8% of all GIL samples.
  - **A PyO3 normalizer.** It stays in-process, so it still contends for the GIL. It
    adds a native extension to Python packaging for at most 24.4% of 6.3%, about 1.5%
    of samples.
  - **Free-threaded CPython.** That is a separate runtime decision outside Stage 2.
- **DR3: What starts early.** P1 and P2 start now. Their tests run on in-memory
  seams, and **they give no GIL relief before D2**. Two earlier #21569 pieces are
  independent, so they land here too: the hooks/execute contract corpus (1.2) and the
  shell normalizer (2.2), which hook ingress reuses. The envelope ledger cannot move
  early. Decision 9 keeps it node-local until the hook-route port, and its finalize
  step depends on the execution outcome. Moving it alone would need a cross-process
  commit between the ledger and Python execution, to save the inbox and envelope
  share: 14.2% of hook-path samples.
- **DR4: Seam traits.** Every input the engine does not own is a trait in
  `gobby-workflows`, with an in-memory implementation that tests script from the 1.1
  corpus. The engine owns `rule_definitions`, `definition_revisions` reads,
  `agent_step_instances`, and `agent_step_workflows`; everything else is a seam. D1
  composes the native adapters where the engine meets its consumers. An adapter for a
  family that is not native at the flip calls that family's existing backend route
  through the front door's backend client. That is the strangler path, not a new
  Python surface. Rejected alternatives:
  - **A shared trait crate now.** Decision 16 expects one for the
    sessions/tasks/rules triangle. It gets created when a second consumer needs it.
  - **Depending on the family crates directly.** Most do not exist yet, so the early
    start would wait on them, and Cargo would see cycles where families call back
    into validation.
- **DR5: Fail closed inside the response, with no snapshot.** The engine never
  allows an event because state is missing or stale.
  - **No snapshot exists.** DR7 checks definitions every pass, and variables and
    seam state are read live. Nothing is allowed on cached state, because there is
    none.
  - **Where fail-closed can live.** On a 503 retry other than `adapter_timeout`,
    and on any failed delivery of a tool hook, ghook continues the host CLI. No CLI
    marks its tool hooks critical (evidence `#repository-counts`). A 503 therefore
    can never fail a tool event closed; only a delivered block decision can. The
    engine keeps Python's rules, which put the closed decision in the response:
    - **Condition failures.** A seam failure or deterministic error inside a rule's
      `when` makes a rule with a `block` effect match, and a rule without one not
      match (`TemplatingMixin._evaluate_condition` in
      `src/gobby/workflows/engine/templating.py`). Python's helpers raise seam
      failures into that same handler, so the rule covers them.
    - **Session state that cannot be loaded.** A `stop` event blocks with Python's
      "Could not load session state. Try again." response. Any other event runs on
      empty variables and persists nothing, as `WorkflowHookHandler._evaluate_rules`
      does. The caller owns this path (D2).
    - **Pass-level errors.** Only the pass deadline and a failed definitions read
      leave the pass as errors. The route maps each to the response Python returns
      for the same condition; 1.2 records them.
  - **Python's absent-manager fallbacks are not ported.** Python helpers return
    `False` when a manager is absent, for example `task_tree_complete` in
    `src/gobby/workflows/condition_helpers.py`. In Rust a missing adapter is a
    construction error, so a block rule can never silently fail to fire for lack of
    one.
  - **Parse failures** are skipped and logged as Python does, and also counted.
  - Rejected alternatives:
    - **A 503 retry on every seam failure.** ghook would continue the CLI, so a seam
      outage would allow every tool call a block rule would have stopped. That is
      less closed than Python.
    - **Blocking every event on any seam failure.** A seam outage would live-lock
      every agent, the failure the ghook comment on the retry path guards against.
  - Changing ghook's availability-first edge is outside this plan. No decision here
    is less closed than Python's.
- **DR6: Dual-engine window.** The Python engine keeps serving the MCP proxy, web
  chat, and in-process agent runner until each flips (D3-D5). Until D6 deletes it,
  every rule template change re-records the 1.1 corpus, and the change passes both
  engines. Both engines take the same advisory locks: `SessionVariableMutation`
  (priority 950) and `AgentStepInstanceMutation` (priority 875), in
  `src/gobby/storage/hub/protocol.py`. gcore's `LockTarget` contract requires
  Python's key strings, so a Rust pass and a Python pass on one session serialize
  exactly as two Python passes do today.
- **DR7: Definition cache.** The engine caches parsed rules and compiled patterns per
  `(events, project_id)`, alongside the `rules` revision they were loaded under.
  Every pass reads `definition_revisions` for `rules`, `agents`, and
  `agent_step_workflows` in one indexed query. The revision is read before rows are
  loaded, so a write that commits during a load forces the next pass to reload. A
  failed revision read fails the pass with `DefinitionsUnavailable`; the cache is
  never used unvalidated.
  Rejected alternative: mirroring Python's `DefinitionRevisionListener`, a
  LISTEN/NOTIFY listener with a 30-second poll fallback
  (`src/gobby/storage/definitions/notifications.py`). While its connection is down,
  a cached rule set can be stale for up to the poll interval. The per-pass read has
  no stale window and needs no listener.
- **DR8: The measurement this plan adds.** The pass classifies each event as
  `zero_match`, `matched_allow`, `mutating`, or `block`, and counts per event type.
  D2 exports the counters through the hooks family's per-route observability. They
  supply, during the Compare soak and before Native, the zero-match denominator the
  evidence lacks.

## Constraints
`kind: framing`

- **Authorization.** Block decisions, rendered reasons, agent scoping
  (`rule_matches_agent` in `src/gobby/workflows/selectors.py`), audience filtering,
  and active-rule-name filtering match Python through the 1.1 corpus. One deliberate
  change: an event with no project id loads global rows only. That is the documented
  contract of `RuleEngine._load_rules`, which Python currently breaks (#23866).
- **Effect order.** Rules run sorted by `(priority, trigger_index, name)`.
  - Each rule's effects run in order.
  - A `set_variable` is visible to later rules in the same pass, and so is an
    `mcp_call` result.
  - Aggregate blocking: only the first matching block runs its sibling effects and
    records metrics. Later rules are probed read-only for block predicates and
    reasons, and `format_aggregated_block_reason`
    (`src/gobby/workflows/engine/blocked_tool_recovery.py`) composes the reason.
- **Delivery.** Effects carry `delivery: eager | on_receipt`
  (`src/gobby/workflows/definitions.py`); 17 on_receipt declarations appear across 9
  bundled rule files.
  - Eager variable changes leave the pass as the delta of changed keys, which the
    caller merges exactly as `SessionVariableManager.merge_variables` does, including
    claim reconciliation on session start and turn end.
  - On_receipt changes leave as the `_gobby_staged_effects` payload
    (`STAGED_EFFECTS_FIELD` in `src/gobby/hooks/receipt_effects.py`). They apply only
    when the receipt is acknowledged, and the ledger owns that path (#21569).
- **Retries and restarts.** Persistent writes commit once, at the end of the pass.
  External effects (`mcp_call`, `run_command`, `load_skill`, `proxy_hook`) run in rule
  order mid-pass, so a retried envelope can run them again. A Python pass that hits
  its deadline already does this today. Duplicate delivery after success returns 409,
  which 1.2 pins.
- **Multi-project isolation.** Seam calls take the project id explicitly, and the
  definition cache keys on it. Global rows apply to every project, and project rows
  only to their own project, which 1.1 and 2.1 pin.
- **Code bounds.** P1 and P2 change no Python production file: P1 adds test files and
  a contract document, and P2 adds a Rust crate. `gdaemon` does not link
  `gobby-workflows` before D2, so nothing in P1 or P2 needs a daemon restart or an
  install.
- **Crate conventions.** Each Rust file stays under 1,000 lines. Unit tests live in
  `<module>/tests.rs` (`crates/AGENTS.md`). Tests that need PostgreSQL use
  `GOBBY_SCHEMA_TEST_DATABASE_URL` against the isolated `*_test` hub, as
  `crates/gcore/src/postgres_pool/tests.rs` does. A skipped database test does not
  satisfy a gate.
- **Out of scope.** No interim Python process service, no disposable Python refactor,
  and no new instrumentation in the Python runner.

## P1: Contract corpora
`kind: framing`

**Goal**: freeze what the Rust engine and the Rust hooks route must reproduce, recorded
from the Python implementation, which is the oracle until D6.

### 1.1 Rule-pass parity corpus and its Python exporter [category: test]
`kind: deliverable`

Targets:
- `tests/contracts/rules/manifest.json`
- `tests/contracts/rules/normalize_vectors.json`
- `tests/contracts/rules/README.md`
- `tests/contracts/rules_corpus.py`
- `tests/contracts/test_rules_corpus.py`

Research context:

- **Call surface.** `WorkflowHookHandler._evaluate_rules` (`src/gobby/workflows/hooks.py`)
  calls `RuleEngine.evaluate(event, session_id, variables, eval_context,
  blocking_deadline)` (`src/gobby/workflows/engine/core.py`). It reads the staged
  payload from `response.metadata["_gobby_staged_effects"]`, then merges the
  changed-key delta with `SessionVariableManager.merge_variables`
  (`src/gobby/workflows/state_manager.py`). That merge runs under a
  `SessionVariableMutation` advisory lock.
- **Loading.** `RuleEngine._load_rules` calls `list_by_event` once per trigger event,
  with `enabled=True` and the event's project id. It dedupes rows by id, logs and
  skips parse failures, and sorts by `(priority, trigger_index, name)`.
  `list_definition_rows` (`src/gobby/storage/definitions/_shared.py`) adds no project
  condition when the project id is `None` (#23866).
- **Filtering and effects.** Filtering is `_filter_by_agent_scope`,
  `_filter_by_audience`, and `_filter_by_active_rules`. The effect types are
  `block`, `set_variable`, `inject_context`, `mcp_call`, `load_skill`,
  `run_command`, `observe`, and `proxy_hook`.
- **Seam-backed names.** The helpers and effects that reach other families are
  listed in the evidence file's `#cross-family-seams-per-rule`.
- **Bundled bodies.** `sync_bundled_rules` (`src/gobby/workflows/sync_rules.py`)
  writes bundled templates to `rule_definitions.definition_json`. The exporter syncs
  into the isolated test hub and reads the rows back, so case bodies are exactly
  what a daemon stores.
- **Existing corpus pattern.** `tests/contracts/http_corpus.py` provides the
  schema-versioned manifest and case loading to copy.

Build a schema-versioned corpus (`schema_version: 1`) of rule-pass cases, one JSON
file per case, listed in `manifest.json`. A case carries:
- the rule bodies it loads, as synced `definition_json` with priority and project id;
- the project id, session facts, and pre-pass variables;
- the normalized event the engine receives;
- an ordered seam script: each seam call with its arguments and recorded response or
  failure;
- the `expect` block: decision, rendered reason, injected context, the eager variable
  delta, the staged on_receipt payload, the ordered effect log, the audit records,
  and the DR8 outcome.

A case whose Rust result is meant to differ from Python's also carries
`expect_rust` and names its reason. Only #23866 may appear there; DR5 keeps Python's
failure semantics, so seam-failure cases need no divergence.

`rules_corpus.py` records cases. For each case it seeds rows through
`RuleDefinitionManager` in the isolated test hub, then runs
`RuleEngine.evaluate`. During that run, pytest monkeypatching wraps each
seam-backed helper and external effect executor in a recorder, so no production
Python changes. The exporter also writes `normalize_vectors.json`, recorded from the
Python functions:
- `tokenize_shell_command`, `scan_shell_command`, and `extract_heredoc_bodies`
  (`_normalization_shell`);
- `decode_ansi_c_escape` (`_ansi_c`);
- `apply_path_scope_metadata` (`_path_scope`);
- `normalize_tool_fields` (`_normalization_tools`);
- `executable_command_subjects` and `command_patterns_match` (`command_matching`);
- `blocks_direct_provider_launch` (`provider_launch_guard`);
- `rule_matches_agent` (`selectors`).

The command strings come from the corpus events plus the existing cases in
`tests/workflows/test_command_matching.py`.

Required scenario cases:
- ordering by priority and trigger index;
- aggregate block;
- a `set_variable` read by a later rule;
- an `mcp_call` result read by a later rule;
- agent-definition scoping, audience, and active-rule-name filtering;
- two projects with different project rows;
- an event with no project id (#23866);
- a parse-failure row skipped;
- a condition error in a block rule and in a non-block rule;
- one failing seam call per seam kind (DR5);
- on_receipt staging;
- step-tool enforcement allow and block;
- the blocking deadline.

**Granularity:** one leaf with eight acceptance items. The case schema, the exporter
that is its oracle, and the coverage checks are one artifact. A corpus without its
exporter cannot be re-recorded, and DR6 needs re-recording.

**Acceptance:**

- 1.1.1 - The manifest and every case load at `schema_version` 1, and a stale version is rejected. test: `tests/contracts/test_rules_corpus.py::test_loader_rejects_stale_schema_version`.
- 1.1.2 - Every one of the 219 bundled rules is matched in at least one case, whatever its bundled `enabled` flag. test: `tests/contracts/test_rules_corpus.py::test_corpus_matches_every_bundled_rule`.
- 1.1.3 - Every effect type and both delivery classes appear in some case's expected effect log. test: `tests/contracts/test_rules_corpus.py::test_corpus_covers_effect_types_and_delivery`.
- 1.1.4 - Every required scenario case exists. test: `tests/contracts/test_rules_corpus.py::test_corpus_has_required_scenarios`.
- 1.1.5 - The Python engine reproduces every case's `expect` block. test: `tests/contracts/test_rules_corpus.py::test_python_engine_replays_corpus`.
- 1.1.6 - Every `expect_rust` block names #23866, and no other reason appears. test: `tests/contracts/test_rules_corpus.py::test_divergences_name_their_decision`.
- 1.1.7 - The Python functions reproduce every normalizer vector. test: `tests/contracts/test_rules_corpus.py::test_python_normalizer_replays_vectors`.
- 1.1.8 - The README documents the record and replay commands and the DR6 re-record obligation. file: `tests/contracts/rules/README.md`.

### 1.2 hooks/execute contract corpus and contract document [category: test]
`kind: deliverable`

Targets:
- `tests/contracts/hooks/manifest.json`
- `tests/contracts/hooks/README.md`
- `tests/contracts/hooks_corpus.py`
- `tests/contracts/test_hooks_corpus.py`
- `docs/contracts/hooks-execute.md`

Research context:

- **Envelopes.** `execute_hook` inside `create_hooks_router`
  (`src/gobby/servers/routes/mcp/hooks.py`) builds every response envelope:
  - 409 `{"status": "malformed_marker", "reason": ...}`;
  - 409 duplicate envelope previously processed;
  - 503 `{"status": "retry", "retry_kind": ...}`, where `retry_kind` is one of:
    - `preflight_timeout`;
    - `ingress_backpressure`, with reason `agent_run_identity_pending` or
      `daemon_not_ready`;
    - `adapter_timeout`.
- **Where retries are forced today.** `tests/servers/routes/test_hooks_agy_dispatch.py`
  forces the retry kinds in-process, for example
  `test_ingress_retry_includes_retry_kind_discriminator`.
- **Harness limit.** The HTTP contract corpus (`tests/contracts/http/`,
  `schema_version` 2) uses a function-scoped boundary daemon, one per case, and has
  no hooks cases. A sequence of related requests therefore has to be one case.
- **Receipt acknowledgments** travel through inbox files
  (`_consume_inbox_delivery_receipt` in `src/gobby/hooks/inbox.py`). The ledger port
  owns them (#21569).

Add `tests/contracts/hooks/` with two kinds of case:
- **`sequence`.** Ordered requests against one boundary daemon, the way
  `tests/contracts/test_http_corpus.py` builds it.
- **`envelope`.** One response forced in-process with the technique of
  `test_hooks_agy_dispatch.py`.

`hooks_corpus.py` imports `apply_masks`, `redact_secrets`, `credential_headers`,
and `normalize_response` from `tests/contracts/http_corpus.py` rather than copying
them.

Required sequence cases, each for the Claude Code and Codex adapters:
- an event no rule matches;
- a bundled block;
- an injected turn-start context;
- a response carrying staged receipt fields;
- a duplicate envelope after success (409);
- a malformed marker (409).

Required envelope cases:
- `preflight_timeout`;
- both `ingress_backpressure` reasons;
- `adapter_timeout`;
- `rule_pass_deadline`: whatever Python returns when the rule pass raises
  `DatabaseOperationDeadlineExceeded`, recorded as observed;
- `rule_load_failure`: whatever Python returns when loading rule rows raises,
  recorded as observed;
- `stop_state_unavailable`: the `stop` block Python returns when session variables
  cannot load;
- `tool_state_unavailable`: a tool event whose session variables cannot load, which
  runs on empty variables and persists nothing.

`docs/contracts/hooks-execute.md` states:
- the request fields and the response per adapter;
- each status code and envelope;
- duplicate and retry semantics, including that a retried envelope re-runs the
  pass and its external effects;
- ghook's disposition of each failure (the CLI continues on a retry for every tool
  hook);
- the DR5 mapping of `DeadlineExceeded` and `DefinitionsUnavailable` to the
  `rule_pass_deadline` and `rule_load_failure` responses, and of session-state load
  failure to the two `*_state_unavailable` responses.

D2 replays this corpus against the Rust route.

**Acceptance:**

- 1.2.1 - The hooks manifest and every case load, and the loader rejects an unknown case kind. test: `tests/contracts/test_hooks_corpus.py::test_hooks_manifest_contract`.
- 1.2.2 - Every sequence case replays equal against the Python boundary daemon. test: `tests/contracts/test_hooks_corpus.py::test_sequence_case_replays_equal`.
- 1.2.3 - Every envelope case replays equal in-process. test: `tests/contracts/test_hooks_corpus.py::test_envelope_case_replays_equal`.
- 1.2.4 - Every required sequence and envelope case exists. test: `tests/contracts/test_hooks_corpus.py::test_hooks_corpus_has_required_cases`.
- 1.2.5 - The contract document states every envelope, the retry semantics, ghook's failure disposition, and the DR5 mapping. file: `docs/contracts/hooks-execute.md`.

## P2: gobby-workflows rule engine
`kind: framing`

**Goal**: a Rust rule engine that replays the 1.1 corpus equal, behind seam traits with
in-memory implementations, linked into no binary until D2. Every deliverable edits
`crates/gworkflows/src/lib.rs` and `crates/gworkflows/Cargo.toml`, so P2 runs as a
chain.

### 2.1 Crate, rule definitions repository, and revision-checked cache [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `Cargo.toml`
- `Cargo.lock`
- `crates/gworkflows/Cargo.toml`
- `crates/gworkflows/src/lib.rs`
- `crates/gworkflows/src/rules.rs`
- `crates/gworkflows/src/rules/definition.rs`
- `crates/gworkflows/src/rules/repo.rs`
- `crates/gworkflows/src/rules/cache.rs`
- `crates/gworkflows/src/rules/tests.rs`
- `crates/gworkflows/tests/corpus_parse.rs`
- `crates/gworkflows/tests/definitions_repo.rs`

Research context:

- **Workspace.** Root `Cargo.toml` members are `crates/gcode`, `crates/gcore`,
  `crates/gclient`, `crates/gdaemon`, `crates/ghook`, `crates/gterminal`, and
  `crates/gterminals`.
- **Pool.** gcore's async pool (feature `postgres-pool`) exposes `Pool::get`,
  `Pool::transaction(lock, f)`, `FromRow`, `query_as`, and `query_opt_as`
  (`crates/gcore/src/postgres_pool/`).
- **Tables.** `rule_definitions` columns are `id`, `project_id`, `name`, `enabled`,
  `priority`, `definition_json` (jsonb), `source`, `deleted_at`, and timestamps.
  `definition_revisions` has `domain` and `revision`
  (`crates/gcore/assets/schema/baseline.sql`). Rule bodies are JSON, so the crate
  needs no YAML parser.
- **Python behavior.** Python validates bodies with `RuleDefinitionBody`
  (`src/gobby/workflows/definitions.py`). Its rule cache validates against the
  in-process revision `RuleEngine._cached_rules` reads.

Add `crates/gworkflows` to the root members:
- package `gobby-workflows`, `publish = false`, workspace version;
- dependencies `gobby-core` (feature `postgres-pool`), `serde`, `serde_json`,
  `thiserror`, `tokio`, and `async-trait`, all already in `Cargo.lock`.

The crate is built as follows:
- **`rules::definition`** mirrors `RuleDefinitionBody`. It accepts exactly the
  bodies Python accepts, as the corpus proves.
- **`rules::repo`** lists enabled, undeleted rows per trigger event: global rows,
  plus that project's rows when the event has a project id. It dedupes by id and
  sorts by `(priority, trigger_index, name)`. It also reads `definition_revisions`.
- **`rules::cache`** implements DR7.
- **Parse failures** are skipped, logged once per row and revision, and counted.
- **No route family.** The crate exports no `RouteFamily` and does not depend on
  `gdaemon`; DR1 states this deviation.

**Granularity:** one leaf with seven production files, counting both `Cargo.toml`
files. The workspace entry, the crate manifest, and the repository, definitions, and
cache modules form one loader. The cache cannot be tested without the repository it
validates.

**Acceptance:**

- 2.1.1 - The crate is a workspace member, `cargo clippy -p gobby-workflows --all-targets -- -D warnings` passes, and no other crate depends on it. file: `crates/gworkflows/Cargo.toml`.
- 2.1.2 - Every rule body in the 1.1 corpus parses. test: `crates/gworkflows/tests/corpus_parse.rs::every_corpus_rule_body_parses`.
- 2.1.3 - A body Python rejects is skipped and counted, and the rest of the pass still loads. test: `crates/gworkflows/tests/corpus_parse.rs::rejected_bodies_are_skipped_and_counted`.
- 2.1.4 - The repository returns global rows plus same-project rows, deduped and ordered by `(priority, trigger_index, name)`. test: `crates/gworkflows/tests/definitions_repo.rs::lists_global_and_same_project_rows_in_order`.
- 2.1.5 - An event with no project id loads global rows only. test: `crates/gworkflows/tests/definitions_repo.rs::event_without_project_loads_global_rows_only`.
- 2.1.6 - Advancing the `rules` revision makes the next pass reload, and a failed revision read returns `DefinitionsUnavailable` without using the cache. test: `crates/gworkflows/tests/definitions_repo.rs::cache_reloads_on_revision_and_fails_closed`.

### 2.2 Shell normalization [category: code] (depends: 2.1)
`kind: deliverable`

Targets:
- `crates/gworkflows/Cargo.toml`
- `Cargo.lock`
- `crates/gworkflows/src/lib.rs`
- `crates/gworkflows/src/normalize.rs`
- `crates/gworkflows/src/normalize/shell.rs`
- `crates/gworkflows/src/normalize/shell/scan.rs`
- `crates/gworkflows/src/normalize/segments.rs`
- `crates/gworkflows/src/normalize/ansi_c.rs`
- `crates/gworkflows/src/normalize/path_scope.rs`
- `crates/gworkflows/src/normalize/python_source.rs`
- `crates/gworkflows/src/normalize/python_pipeline.rs`
- `crates/gworkflows/src/normalize/python_pipeline/ast_walk.rs`
- `crates/gworkflows/src/normalize/tools.rs`
- `crates/gworkflows/src/normalize/tests.rs`
- `crates/gworkflows/tests/normalize_corpus.rs`

Research context:

- **Python surface.** Six `src/gobby/hooks/` modules make up the engine's shell
  analysis, 2,664 lines in all:
  - `_normalization_shell` (970 lines): `tokenize_shell_command`,
    `scan_shell_command`, `extract_heredoc_bodies`;
  - `_normalization_segments` (111);
  - `_ansi_c` (105): `decode_ansi_c_escape`;
  - `_path_scope` (289): `apply_path_scope_metadata`;
  - `_python_pipeline_classifier` (992);
  - `_normalization_tools` (197): `normalize_tool_fields`.
- **Cost.** This is 24.4% of rule GIL samples (evidence `#gil-share-by-bucket`).
- **Importers.** `command_matching.py`, `condition_helpers_paths.py`, `evaluation.py`,
  `run_command.py`, and four other modules import them. Hook ingress uses the same
  modules.
- **Out of scope here.** Ingress canonicalization (`_normalization_canonical.py`)
  stays with #21569, which reuses this module.
- **No Rust equivalent.** `crates/ghook` has none, and neither does `crates/gcore`.

Port the six modules as `normalize`, with the same token, segment, scope, and
classification results. The two near-ceiling Python modules are split up front:
- `shell.rs` holds the tokenizer and token predicates; `shell/scan.rs` holds
  `scan_shell_command`, heredoc extraction, and the redirection checks.
- `python_pipeline.rs` holds the classification entry points and allow-lists;
  `python_pipeline/ast_walk.rs` holds the tree walk.

**Python source parsing.** The classifier walks Python `ast` trees, and
`provider_launch_guard.py` calls `ast.parse` on inline scripts (evidence
`#repository-counts`). `python_source.rs` wraps `rustpython-parser`, pinned at its
newest release that is at least two weeks old when 2.2 starts. Both the classifier
port and 2.3's launch-guard port consume it. Rejected alternatives:
- **A hand-written statement parser.** It needs the full statement grammar.
- **Treating unparsed inline Python as not read-only.** It diverges from Python and
  blocks pipelines Python allows.

`normalize` is public, and the hooks family consumes it through this crate's API at
D2 (DR3). It has no seams and no I/O beyond the cwd and project-root values the
caller passes.

**Granularity:** one leaf with eleven production files. Each file ports one Python
module or one half of a split module. The shared vector replay tests them together,
and the modules call each other.

**Acceptance:**

- 2.2.1 - Every tokenizer, scanner, heredoc, and ANSI-C vector in `normalize_vectors.json` replays equal. test: `crates/gworkflows/tests/normalize_corpus.rs::shell_vectors_replay_equal`.
- 2.2.2 - Every path-scope and tool-field vector replays equal. test: `crates/gworkflows/tests/normalize_corpus.rs::path_scope_and_tool_vectors_replay_equal`.
- 2.2.3 - Every Python pipeline classification vector replays equal. test: `crates/gworkflows/tests/normalize_corpus.rs::pipeline_classification_vectors_replay_equal`.
- 2.2.4 - Unterminated quotes, nested heredocs, and invalid escapes produce the Python result, not a panic. test: `crates/gworkflows/src/normalize/tests.rs::malformed_input_matches_python_and_never_panics`.

### 2.3 Command matching and agent selectors [category: code] (depends: 2.2)
`kind: deliverable`

Targets:
- `crates/gworkflows/Cargo.toml`
- `Cargo.lock`
- `crates/gworkflows/src/lib.rs`
- `crates/gworkflows/src/matching.rs`
- `crates/gworkflows/src/matching/command.rs`
- `crates/gworkflows/src/matching/selectors.rs`
- `crates/gworkflows/src/matching/patterns.rs`
- `crates/gworkflows/src/matching/provider_launch.rs`
- `crates/gworkflows/src/matching/tests.rs`
- `crates/gworkflows/tests/matching_corpus.rs`

Research context:

- **Python surface.** `command_patterns_match` and `executable_command_subjects` in
  `src/gobby/workflows/engine/command_matching.py` (807 lines), plus
  `rule_matches_agent` in `src/gobby/workflows/selectors.py`.
- **Cost.** Command matching is 6.8% and selectors 3.7% of rule GIL samples.
- **Regex dialect.** Rule-authored patterns use the Python `re` dialect. 28 bundled
  rule files use lookarounds such as `(?<=[;&|]` and `(?=.*DATABASE_URL=`, which the
  `regex` crate rejects. `fancy-regex` and `regex` are already in `Cargo.lock`.

Port command matching and selectors as `matching`, and port
`src/gobby/hooks/provider_launch_guard.py` (762 lines) as `matching::provider_launch`,
on 2.2's `python_source` parser. Command matching imports it, and the condition helper
`blocks_direct_provider_launch` calls it.

**Granularity:** one leaf with six production files. The launch guard is a
dependency of command matching, and the shared vectors test them together.

`matching::patterns` compiles each rule-authored pattern once per DR7 cache entry:
- with `regex` when it compiles there;
- otherwise with `fancy-regex`;
- and a pattern neither accepts is a parse failure counted under 2.1's rule.

No pattern compiles inside a pass.

**Acceptance:**

- 2.3.1 - Every `command_patterns_match` and `executable_command_subjects` vector replays equal. test: `crates/gworkflows/tests/matching_corpus.rs::command_vectors_replay_equal`.
- 2.3.2 - Every `rule_matches_agent` vector replays equal. test: `crates/gworkflows/tests/matching_corpus.rs::selector_vectors_replay_equal`.
- 2.3.3 - Every `blocks_direct_provider_launch` vector replays equal, including inline Python scripts. test: `crates/gworkflows/tests/matching_corpus.rs::provider_launch_vectors_replay_equal`.
- 2.3.4 - Every pattern in the bundled rules compiles, lookaround patterns match as Python's `re` does on the corpus strings, and compilation happens only on cache load. test: `crates/gworkflows/src/matching/tests.rs::bundled_patterns_compile_once_with_python_semantics`.

### 2.4 Condition language, helpers, and query seams [category: code] (depends: 2.3)
`kind: deliverable`

Targets:
- `crates/gworkflows/src/lib.rs`
- `crates/gworkflows/src/condition.rs`
- `crates/gworkflows/src/condition/parse.rs`
- `crates/gworkflows/src/condition/value.rs`
- `crates/gworkflows/src/condition/eval.rs`
- `crates/gworkflows/src/condition/helpers.rs`
- `crates/gworkflows/src/condition/tests.rs`
- `crates/gworkflows/src/seams.rs`
- `crates/gworkflows/src/seams/memory.rs`
- `crates/gworkflows/tests/condition_corpus.rs`

Research context:

- **Accepted grammar.** `SafeExpressionEvaluator` (`src/gobby/workflows/safe_evaluator.py`,
  903 lines) accepts these nodes:
  - `Expression`;
  - `BoolOp` (`And`, `Or`);
  - `BinOp` (`Add`, `Sub`, `Mod`, `FloorDiv`);
  - `UnaryOp` (`Not`, `UAdd`, `USub`);
  - `Compare` (`Eq`, `NotEq`, `Lt`, `LtE`, `Gt`, `GtE`, `In`, `NotIn`, `Is`,
    `IsNot`);
  - `IfExp`, `Call`, `Attribute`, `Subscript`, `Name`, `Constant`, `List`, `Tuple`,
    `Dict`, `ListComp`, and `GeneratorExp`.
- **Semantics.** `and` and `or` return operands, not booleans.
- **Helpers.** `build_condition_helpers` returns 54 names, builtins included.
  `TemplatingMixin._build_allowed_funcs` (`src/gobby/workflows/engine/templating.py`)
  adds 19 more.
- **Seam-backed helpers:**
  - tasks: `task_tree_complete` and six others in
    `src/gobby/workflows/condition_helpers.py`;
  - sessions: `send_keys_target_in_scope`, `send_message_target_allowed`,
    `spawn_target_allowed`, and `network_override_allowed` in
    `condition_helpers_sessions.py`;
  - pending messages, waits, and stop signals;
  - transcript, code index, and monolith-path helpers.
- **Cost.** Condition evaluation is 12.6% of rule GIL samples.

Build `condition`:
- **Parser.** A hand-written parser for exactly the accepted subset, including
  Python string literal forms. It reproduces `SafeExpressionEvaluator._normalize_expr`
  and rejects every other node, f-strings included. Rejected alternative: a full
  Python parser crate, which brings a whole grammar for a subset of about 20 nodes.
- **Values.** A dynamic `Value` with Python truthiness, comparison, membership,
  indexing, and the `Mod` and `FloorDiv` semantics the vectors pin.
- **Evaluator.** Applies DR5's rule for deterministic errors.
- **Helpers.** The pure helpers are ported in `condition::helpers`.

Every seam-backed helper calls a query trait in `seams`:
- `TaskQueries`;
- `SessionQueries`, covering targets, pending messages, waits, and stop signals;
- `AgentDefinitions`;
- `TranscriptQueries`;
- `CodeIndexQueries`;
- `WorkspaceFiles`, for monolith paths.

Every trait call takes the project id and returns `Result<_, SeamUnavailable>`.
`seams::memory` implements every trait from a case's seam script and fails the test
on a call the script does not contain.

**Granularity:** one leaf with ten acceptance-relevant files. The parser, value model,
and evaluator form one language that the vectors exercise together. The query seams
belong here because the helpers are their first callers.

**Acceptance:**

- 2.4.1 - Every condition the 1.1 cases evaluate replays to the same value or the same error class. test: `crates/gworkflows/tests/condition_corpus.rs::condition_vectors_replay_equal`.
- 2.4.2 - Every node outside the accepted subset is rejected at parse time, f-strings and lambdas included. test: `crates/gworkflows/src/condition/tests.rs::rejects_nodes_outside_the_subset`.
- 2.4.3 - `and` and `or` return operands, and truthiness matches Python for every value kind. test: `crates/gworkflows/src/condition/tests.rs::boolean_operators_return_operands`.
- 2.4.4 - A deterministic error matches in a block rule and does not match in a non-block rule. test: `crates/gworkflows/src/condition/tests.rs::expression_errors_follow_the_per_rule_rule`.
- 2.4.5 - A seam failure inside a helper is never read as `False`. It makes a block rule match and a non-block rule not match, as Python's handler does. test: `crates/gworkflows/src/condition/tests.rs::seam_failure_follows_the_per_rule_rule`.
- 2.4.6 - All 73 helper names resolve, and an unscripted seam call fails the test. test: `crates/gworkflows/src/condition/tests.rs::every_helper_name_resolves_and_unscripted_calls_fail`.

### 2.5 Templates [category: code] (depends: 2.4)
`kind: deliverable`

Targets:
- `crates/gworkflows/Cargo.toml`
- `Cargo.lock`
- `crates/gworkflows/src/lib.rs`
- `crates/gworkflows/src/template.rs`
- `crates/gworkflows/src/template/tests.rs`
- `crates/gworkflows/tests/template_corpus.rs`

Research context:

- **Python behavior.** `TemplateEngine` (`src/gobby/workflows/templates.py`) renders
  Jinja and registers the custom filters `regex_search`, `regex_replace`, and
  `shlex_quote`.
- **What the bundled rules use.** 130 Jinja blocks:
  - tags `set`, `if`, `elif`, `else`, and `for`;
  - filters `join`, `truncate`, `list`, `shlex_quote`, `int`, and `tojson`;
  - calls to `.get` and `.values` on mappings.
- **Cost.** Templates are 3.8% of rule GIL samples.
- **Dependencies.** `minijinja` is not in `Cargo.lock`. `shlex` and `regex` are.

Render with `minijinja` plus `minijinja-contrib`'s `pycompat` unknown-method
callback, which supplies `.get` and `.values`. Pin each at its newest release that is
at least two weeks old when 2.5 starts. Register `shlex_quote` (on `shlex`) and
`regex_search` and `regex_replace` (on 2.3's pattern compiler). Undefined-variable and
whitespace behavior follow the vectors. Each template compiles once per DR7 cache
entry.

**Acceptance:**

- 2.5.1 - Every template the 1.1 cases render produces Python's output byte for byte. test: `crates/gworkflows/tests/template_corpus.rs::template_vectors_render_equal`.
- 2.5.2 - The three custom filters match Python on quoting, regex groups, and no-match input. test: `crates/gworkflows/src/template/tests.rs::custom_filters_match_python`.
- 2.5.3 - An undefined variable renders as Python's `TemplateEngine` renders it. test: `crates/gworkflows/src/template/tests.rs::undefined_variables_match_python`.

### 2.6 Rule pass, effect plan, effect seams, and outcome counters [category: code] (depends: 2.5)
`kind: deliverable`

Targets:
- `crates/gworkflows/src/lib.rs`
- `crates/gworkflows/src/seams.rs`
- `crates/gworkflows/src/seams/memory.rs`
- `crates/gworkflows/src/engine.rs`
- `crates/gworkflows/src/engine/pass.rs`
- `crates/gworkflows/src/engine/effects.rs`
- `crates/gworkflows/src/engine/outcome.rs`
- `crates/gworkflows/src/engine/tests.rs`
- `crates/gworkflows/tests/rule_pass_corpus.rs`
- `crates/gworkflows/tests/rule_pass_budget.rs`

Research context:

- **Python's pass.** `EvaluationMixin._run_rule_loop_pass`
  (`src/gobby/workflows/engine/evaluation.py`) runs it. It filters by the body's
  `tools`, evaluates `when` through `TemplatingMixin._evaluate_condition`, and
  applies effects through `EffectsMixin._apply_effect` and `_apply_set_variable`
  (`src/gobby/workflows/engine/effects.py`).
- **Audit.** It records allow-audit lines with `record_rule_evaluation`
  (`src/gobby/telemetry/rule_allow_audit.py`).
- **Aggregate blocking.** Only the first matching block runs its sibling effects.
- **Bridging.** Python bridges `mcp_call` back to the daemon loop through
  `_RuleLoopBridge`.
- **Effect carriers per rule.** `block` 151, `set_variable` 49, `inject_context` 18,
  `mcp_call` 12, `load_skill` 4, `run_command` 2, `observe` 1, `proxy_hook` 1.
- **Cost.** Per-pass loop scaffolding is 25.6% of rule GIL samples. The Rust pass is
  an ordinary async function, so none of that cost carries over.

`engine::pass` takes the event, session facts, variables, project id, and a
deadline. It returns a `PassResult` or a `PassError`.

A `PassResult` carries:
- the decision, rendered reason, and injected context;
- the eager variable delta;
- the staged on_receipt payload, in the `_gobby_staged_effects` shape;
- the ordered effect log;
- the audit records, in `record_rule_evaluation`'s line schema;
- the DR8 outcome.

A `PassError` is `DefinitionsUnavailable` or `DeadlineExceeded` (DR5). A seam
failure inside a `when` follows DR5's per-rule rule and does not end the pass.

Pass behavior:
- **Order.** The pass follows the Constraints section's effect order exactly.
  `set_variable` updates the in-pass context, and an `mcp_call` result is visible to
  later rules.
- **External effects.** `mcp_call`, `load_skill`, `run_command`, and `proxy_hook`
  go through the effect seams `McpDispatch`, `SkillLoader`, and `CommandRunner`,
  which this deliverable adds to `seams`.
- **Persistence.** The pass persists nothing; the caller commits the delta (D1).
- **Counters.** `engine::outcome` keeps per-event-type counters behind
  `RuleEngine::outcome_snapshot()` (DR8).

**Granularity:** one leaf with seven acceptance items and seven production files. The
pass, effect plan, and counters share one state machine: the pass's rule loop. The
corpus replay can only test them together.

**Acceptance:**

- 2.6.1 - Every 1.1 case replays equal to `expect`, or to `expect_rust` where present: decision, reason, context, delta, staged payload, effect log, audit records, and outcome. test: `crates/gworkflows/tests/rule_pass_corpus.rs::every_case_replays_equal`.
- 2.6.2 - In aggregate blocking, only the first matching block runs sibling effects and records metrics, and the reason matches `format_aggregated_block_reason`. test: `crates/gworkflows/tests/rule_pass_corpus.rs::aggregate_block_runs_first_block_effects_only`.
- 2.6.3 - A `set_variable` and an `mcp_call` result are visible to later rules in the same pass. test: `crates/gworkflows/tests/rule_pass_corpus.rs::in_pass_writes_are_visible_to_later_rules`.
- 2.6.4 - A seam failure inside a `when` follows the per-rule rule and the pass continues. A failed definitions read returns `DefinitionsUnavailable` with no delta and no staged payload. test: `crates/gworkflows/tests/rule_pass_corpus.rs::seam_and_definition_failures_follow_dr5`.
- 2.6.5 - Deadline expiry returns `DeadlineExceeded` and commits nothing. test: `crates/gworkflows/tests/rule_pass_corpus.rs::deadline_expiry_commits_nothing`.
- 2.6.6 - Each pass is counted exactly once as `zero_match`, `matched_allow`, `mutating`, or `block` under its event type. test: `crates/gworkflows/src/engine/tests.rs::outcome_counters_classify_each_pass`.
- 2.6.7 - On the release profile with in-memory seams, the p99 `before_tool` pass over the corpus's full bundled rule set takes 1 ms or less, and the test prints p50 and p99 per event type. test: `crates/gworkflows/tests/rule_pass_budget.rs::before_tool_pass_p99_within_budget`.

The 1 ms ceiling is derived, because no Python per-pass baseline exists: the allow
audit times rules, not passes.
- The `before_tool` per-rule median is 0.006 ms (evidence
  `#matched-rule-passes-in-the-allow-audit`).
- 146 enabled rows trigger on `before_tool` (evidence `#inputs`).
- A pass whose every rule cost that median would take about 0.88 ms.

The Rust pass must stay at or below that cost while evaluating every rule. This is a
regression gate, not a speed claim against Python.

### 2.7 Step-workflow and agent tool enforcement [category: code] (depends: 2.6)
`kind: deliverable`

Targets:
- `crates/gworkflows/src/lib.rs`
- `crates/gworkflows/src/engine/pass.rs`
- `crates/gworkflows/src/step.rs`
- `crates/gworkflows/src/step/enforcement.rs`
- `crates/gworkflows/src/step/repo.rs`
- `crates/gworkflows/src/step/tests.rs`
- `crates/gworkflows/tests/step_enforcement_corpus.rs`

Research context:

- **Python surface.** `RuleEngine.evaluate` calls
  `EnforcementCheckMixin._check_step_tool_enforcement` and
  `_check_agent_tool_enforcement` (`src/gobby/workflows/engine/enforcement_checks.py`,
  993 lines).
- **Locking.** The step check runs `_check_step_tool_enforcement_locked` inside
  `db.transaction_immediate(AgentStepInstanceMutation(session_id))`.
- **Audit.** `EnforcementAuditMixin._audit_step_tool_call` writes step audit rows.
- **Tables.** `agent_step_instances` and `agent_step_workflows`
  (`crates/gcore/assets/schema/baseline.sql`) belong to this family.
- **Cost.** Step enforcement is 11.3% of rule GIL samples and step instance lookups
  5.5%.
- **Lock contract.** gcore's `LockTarget`
  (`crates/gcore/src/postgres_pool/transaction.rs`) requires Python's key strings.

Port both checks as `step`:
- `step::repo` reads and updates step instances on the gcore pool, under a
  `LockTarget` with `AgentStepInstanceMutation`'s key string and priority 875 (DR6).
- `step::enforcement` returns the same block response or continue decision.
- Step audit records go into the `PassResult` audit list.

`engine::pass` calls the checks where `RuleEngine.evaluate` does.

**Acceptance:**

- 2.7.1 - Every step-tool enforcement case in the corpus replays equal. test: `crates/gworkflows/tests/step_enforcement_corpus.rs::step_cases_replay_equal`.
- 2.7.2 - Every agent-level tool enforcement case replays equal. test: `crates/gworkflows/tests/step_enforcement_corpus.rs::agent_tool_cases_replay_equal`.
- 2.7.3 - Step mutations hold the advisory lock with Python's `AgentStepInstanceMutation` key and priority 875, and a concurrent Python holder blocks them. test: `crates/gworkflows/src/step/tests.rs::step_lock_matches_python_key`.
- 2.7.4 - Step audit records match `_audit_step_tool_call` field for field. test: `crates/gworkflows/tests/step_enforcement_corpus.rs::step_audit_records_match`.

## D1 Seam adapters at the hooks family composition point (depends: 2.7)
`kind: deferred`

D2 needs a native adapter behind every seam, composed in the hooks family crate. That
crate depends on `gobby-workflows` and on each family it adapts, which keeps Cargo
acyclic (DR4).

Deferred obligations, each with its owner:
- **D1.1** `TaskQueries` over the tasks family service trait (S2.4, #21560).
- **D1.2** `SessionQueries`, `TranscriptQueries`, and the eager-delta commit. The
  commit runs under `SessionVariableMutation`'s key and priority 950 and keeps claim
  reconciliation on session start and turn end. Owner: the sessions family (S2.5,
  #21562).
- **D1.3** `AgentDefinitions` and the spawn and send-target checks, over the agents
  family (S2.7, #21564).
- **D1.4** `McpDispatch` over the external-MCP multiplexer (S2.10, #21566).
  - Internal targets not yet native call the backend's existing MCP tool-call route:
    `gobby-memory` (5 rules), `gobby-review-learning` (5), `gobby-workflows`
    `run_pipeline` (1), and `gobby-skills` `list_hubs` (1).
- **D1.5** `SkillLoader` (skills family #21580) and `CodeIndexQueries` (#21586). Each
  calls its backend route while its family is not native.
- **D1.6** `CommandRunner` and `WorkspaceFiles`, which are local and in-process.
  - Files edited on a node are front-door D3's envelope-content requirement.

```yaml
deferral:
  task_ref: "TBD-at-expansion"
  reason: "Adapters wrap other Stage 2 families' public APIs, which do not exist yet; they compose in the hooks family crate that S2.11 creates."
  owner: "#21569 hook ingress and envelope ledger (S2.11)"
  original_acceptance_items:
    - D1.1
    - D1.2
    - D1.3
    - D1.4
    - D1.5
    - D1.6
```

## D2 Hooks route flip with the Stage 1 gates and GIL acceptance (depends: 1.2, 2.7)
`kind: deferred`

The hooks family serves `POST /api/hooks/execute` natively, running the
`gobby-workflows` pass with D1's adapters. It is selected by `front_door.routes` with
the values `proxy`, `compare`, and `native`.

Deferred obligations, under #21543's per-boundary gates:
- **D2.1** Fixture parity: the 1.2 corpus replays equal against the Rust route.
- **D2.2** Error-path parity: every 409 and 503 envelope, and DR5's mappings. The
  pass deadline and a failed definitions read map to `rule_pass_deadline` and
  `rule_load_failure`. Session-state load failure maps to `stop_state_unavailable` and
  `tool_state_unavailable`.
- **D2.3** A Compare soak with no unexplained diff in decision, reason, injected
  context, or staged receipt fields.
  - In Compare, the Rust pass runs its effect seams in record mode, so no effect runs
    twice.
  - A pass whose later rules read an `mcp_call` result is compared up to that call
    and counted as partial.
- **D2.4** Route-scoped rollback: setting `hooks: proxy` returns the route to Python
  with no other family affected.
- **D2.5** Per-route observability, including DR8's outcome counters, which report
  the zero-match share.
- **D2.6** Allow-audit lines keep `record_rule_evaluation`'s schema in
  `rule-allow-audit.jsonl`, so the evidence scripts still run.
- **D2.7** GIL acceptance. A `py-spy-record` capture of the Python backend under
  ordinary traffic with `hooks: native` has zero samples carrying
  `servers/routes/mcp/hooks` or `hooks/hook_manager` frames. It reports the remaining
  rule-bucket share against the 6.3% per-capture baseline; that residual belongs to
  D3-D5.

```yaml
deferral:
  task_ref: "TBD-at-expansion"
  reason: "The flip is the S2.11 hook-route port; it needs sessions, the MCP multiplexer, and S2.9 native first (ROADMAP edges S2.11 <- S2.5, S2.9, S2.10)."
  owner: "#21569 hook ingress and envelope ledger (S2.11)"
  original_acceptance_items:
    - D2.1
    - D2.2
    - D2.3
    - D2.4
    - D2.5
    - D2.6
    - D2.7
```

## D3 MCP proxy consumer (depends: 2.7)
`kind: deferred`

The MCP proxy evaluates rules for tool calls through
`apply_before_tool_enforcement` and `apply_after_tool_workflow`
(`src/gobby/mcp_proxy/services/result_handling.py`, called from `tool_proxy.py`).
When the MCP front door flips, those calls run the Rust pass.

- **D3.1** The MCP family evaluates before- and after-tool rules with the
  `gobby-workflows` pass, and its corpus cases replay equal.
- **D3.2** The Python MCP proxy no longer evaluates rules.

```yaml
deferral:
  task_ref: "TBD-at-expansion"
  reason: "Owned by the S2.12 MCP front door flip, which follows S2.11."
  owner: "#21570 MCP front door flip (S2.12)"
  original_acceptance_items:
    - D3.1
    - D3.2
```

## D4 Web chat consumer (depends: 2.7)
`kind: deferred`

Web chat evaluates rules in `src/gobby/servers/websocket/chat/_lifecycle.py` and
applies acknowledged receipts there.

- **D4.1** The WS chat family evaluates rules with the `gobby-workflows` pass and
  applies acknowledged receipts with the same staged-payload contract.
- **D4.2** Python chat no longer calls `workflow_handler.evaluate`.

```yaml
deferral:
  task_ref: "TBD-at-expansion"
  reason: "Owned by the WS chat route family under S2.13."
  owner: "#21592 WS chat route family (S2.13)"
  original_acceptance_items:
    - D4.1
    - D4.2
```

## D5 In-process agent runner consumer (depends: 2.7)
`kind: deferred`

The in-process agent runner reaches the rule engine through
`agent_runner.workflow_handler` (`src/gobby/servers/_app_lifecycle.py`).

- **D5.1** The agents family's runner evaluates rules with the `gobby-workflows`
  pass.
- **D5.2** Python's runner wiring no longer passes a workflow handler.

```yaml
deferral:
  task_ref: "TBD-at-expansion"
  reason: "Owned by the agents family port."
  owner: "#21564 attention, agents, dispatch, and worktrees (S2.7)"
  original_acceptance_items:
    - D5.1
    - D5.2
```

## D6 Delete the Python rule engine (depends: 2.7)
`kind: deferred`

After D2-D5, nothing in Python constructs `RuleEngine`.

- **D6.1** Delete `src/gobby/workflows/engine/` and the Python helper and normalizer
  modules that only it used. Hook ingress normalization moved with D2.
- **D6.2** A sweep finds no `RuleEngine`, `SafeExpressionEvaluator`, or
  `_RULE_LOOP_EXECUTOR` under `src/`.
- **D6.3** The 1.1 exporter becomes a fixture maintenance tool, and DR6's
  dual-engine obligation ends.
- **D6.4** A `py-spy-record` capture shows no rule-bucket samples.

```yaml
deferral:
  task_ref: "TBD-at-expansion"
  reason: "Possible only after the last consumer flips (D2-D5)."
  owner: "#21567 workflows, rules, pipelines, build, and validation (S2.9)"
  original_acceptance_items:
    - D6.1
    - D6.2
    - D6.3
    - D6.4
```

## V1 Plan Changelog
`kind: framing`

- 2026-10-08 CDT (2026-10-09 UTC): First draft by the Lane 7 Plan Writer gobby#15677
  for #22946, written after re-measurement. The evidence is in
  `.gobby/plans/research/rule-evaluation-evidence-2026-10-09.md`, which replaces the
  2026-09-26 figures LM7 ruled stale. DR1-DR4 were approved by the Orchestrator
  gobby#14972 at 19:46 CDT. DR5-DR8 were added for fail-closed seams, the
  dual-engine window, the definition cache, and the zero-match measurement. The
  project-isolation finding is filed as #23866 and pinned as a recorded divergence in
  1.1 and 2.1.
- 2026-10-08 CDT: Pre-review amendment to the first draft (1f442a416c).
  - **DR5 rewritten.** ghook continues the host CLI on a 503 retry and on any failed
    delivery of a tool hook. A retry therefore cannot fail a tool event closed, so the
    engine keeps Python's per-rule and session-state failure rules in the response.
    `PassError` becomes `DefinitionsUnavailable` or `DeadlineExceeded`, and 1.2 adds
    three recorded responses.
  - **Evidence.** The repository counts and ghook's disposition are now in the
    evidence file under `#repository-counts`.
  - **Python source parsing.** It is decided as `rustpython-parser` in 2.2, and
    `provider_launch_guard.py` is ported in 2.3.
  - **File splits** for the two near-ceiling modules are fixed up front.
  - **Other fixes.** 2.6.7's ceiling is derived, 2.1, 2.2, and 2.3 have Granularity
    notes, and V2's planning-time check is recorded as observed.

## V2: Verification
`kind: verification`

These are completion gates for the implementation. Except for plan validation, none
has run yet. Each runs after the leaves it names land, and each must hold before the
P1 and P2 leaves close.

Observed on this draft:

- `uv run gobby plans validate .gobby/plans/rust-rule-evaluation.md -p /Users/josh/Projects/gobby`
  exited 0 in standard mode on 2026-10-08 CDT, run by the Writer gobby#15677. This is
  the only planning-time check.

Implementation gates (not run; the files they test do not exist yet):

- After 1.1 and 1.2:
  `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/contracts/test_rules_corpus.py tests/contracts/test_hooks_corpus.py tests/contracts/test_http_corpus.py`
  must pass.
- After each P2 leaf:
  `GOBBY_SCHEMA_TEST_DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test cargo nextest run -p gobby-workflows`
  must pass with no database test skipped.
- After 2.6: `cargo test --release -p gobby-workflows --test rule_pass_budget` must
  pass.
- After every P2 leaf, `cargo fmt --check` and
  `cargo clippy -p gobby-workflows --all-targets -- -D warnings` must pass, and every
  file under `crates/gworkflows/` must stay under 1,000 lines.
- `uv run ruff check tests/contracts/` and the test-types audit over the new test files
  must pass.
- Before D2 starts, any rule template change must re-record the 1.1 corpus, and both
  the Python replay (1.1.5) and the Rust replay (2.6.1) must pass (DR6).
- `gdaemon` must not depend on `gobby-workflows` until D2, and no `~/.gobby/bin/`
  binary changes during P1 or P2.
