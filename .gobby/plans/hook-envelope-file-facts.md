Plan artifact: `.gobby/plans/hook-envelope-file-facts.md`

# Hook envelope carries edited-file facts for disk-reading rules

**Plan ID:** hook-envelope-file-facts

## Overview
`kind: framing`

This plan specifies #23277 (Hook envelope carries edited-file content for
disk-reading rules). That task is deferred section D5 of
`.gobby/plans/gdaemon-api-keys-nodes.md` (S1.4, #21555), and section D3 of the
plan of record `.gobby/plans/gdaemon-front-door.md`. The leaves expand under
#23277 (PD ruling, 2026-10-04 19:44).

**The problem.** Rule evaluation runs in the Python daemon. Today, 15 daemon
sites read the agent's working tree from the evaluating machine's disk while
they handle a hook (§Site Disposition). ghook never reads the edited file, and the envelope carries
no file fact. The daemon also resolves its checkout with its own machine id
(`src/gobby/workflows/hooks.py::WorkflowHookHandler._resolve_project_path`
calls `require_local_machine_id`). A hub that evaluates a node's hook therefore
reads the hub's own, stale checkout of the same project and silently returns
wrong answers. It does not crash.

**When the leaves close:**
- ghook attaches a `file_facts` field to every envelope whose tool input edits a
  guarded source file. The field holds each target's existence, line count, and
  content up to 256 KiB.
- The ghook inbox spool, which carries whole envelopes, is owner-only.
- The monolith projection and the Rust TDD test-only classification read
  `file_facts` before they read any disk.
- MultiEdit `edits[]` projects correctly.
- The daemon classifies every hook delivery as local-origin or foreign-origin.
  For a foreign-origin event, no site reads the evaluator's disk or git, and
  every gate that needed that answer fails closed with one named diagnostic.

Gates that need a node's git state or checkout stay unresolved for foreign
events. Deferred section D1 assigns them to S2.11 (#21569). Binding origin to
the authenticated key is deferred to D2, which is gated on the shared-token
cutover. Node-to-hub end-to-end verification is deferred to D3, which is gated
on the relay.

## Decision Record
`kind: framing`

Rulings by the PD (gobby#14972) on 2026-10-04 at 19:44, recorded in message
d02a4239:

1. **Scope A.** Deliver the edited-file facts for the monolith projection (site
   1) and the Rust TDD classification (site 3). Fix the MultiEdit/NotebookEdit
   projection gap. Add an origin guard so that no hook path resolves a local
   checkout or reads disk or git for a foreign-origin event. Every other site
   gets a typed disposition (§Site Disposition). Carrying git state in the
   envelope (option B) was rejected, because it would run git in ghook on
   every hook.
2. **Git-state gates fail closed for foreign-origin events** and show a named
   diagnostic. `ledger_reconcile` skips foreign-origin events, so it never
   releases ledger rows on the strength of the hub's disk.
3. **Envelope shape (approved).**
   - `file_facts` is a top-level envelope field, a sibling of `input_data`.
   - ghook carries it on every machine, not only on nodes.
   - The existing Python projection logic consumes it, so nothing is ported to
     Rust.
   - The v1 schema mirrors are updated in place. The field is optional and
     `schema_version` stays 1, following the `response_capability` precedent.
     0.5.0 has no compatibility constraint.
4. **D1.7 is intentional.** It is the corpus item from api-keys D1. The mapping
   and its evidence are under §D1.7 Mapping.
5. **Node git-state and disk gates get an owner.** Deferred section D1 assigns
   them to S2.11 (#21569), the hook ingress and node-local envelope ledger.
   *This deferral is a candidate fourth item for Choice 5's required set
   ("D3, D4 and D5 must all land before nodes count as supported",
   `.gobby/plans/research/api-key-bootstrap-options.md:546`). Josh confirms it
   when S2.11 is planned.*

Writer decisions, each resolving a question the PD asked to be answered in the
plan:

6. **Fact lookup order: envelope fact first, local disk second, unverified
   last.**
   - An envelope-carried path always uses its fact, on every machine, even when
     disk disagrees. This is the single code path.
   - Disk is read only for a path the envelope cannot carry, and only when the
     origin is local. That covers paths taken from shell commands
     (`canonical_file_paths`), ledger recounts, and in-process adapter events
     that have no envelope.
   - A foreign-origin path with no fact is unverified.
   - This refines the approved "never the disk" rule. Without it, Bash-derived
     monolith checks and web-chat sessions, which have no ghook, would lose
     their guard on the local machine.
7. **How origin is classified.** A delivery is foreign-origin when either holds:
   - `input_data.machine_id` is present and differs from the daemon's
     `gobby.utils.machine_id.get_machine_id()`;
   - `input_data.machine_id_error` is present, which ghook sets when it cannot
     read its own machine id (`crates/ghook/src/dispatch.rs::inject_machine_identity`).

   An envelope with neither is local. Test fixtures and in-process events are
   local. The machine id is self-reported until D2 binds it to the
   authenticated key.
   - ghook and the daemon read the same file, `<gobby_home>/machine_id`
     (`crates/gcore/src/machine.rs::read_local_machine_id`,
     `src/gobby/utils/machine_id.py::get_machine_id`), and ghook overwrites any
     provider-supplied `machine_id`. A local hook therefore always matches.
   - When the daemon cannot read its own id (`None` or `OSError`), an
     envelope that carries a `machine_id` is foreign. That direction fails
     closed.
   - A `machine_id_error` envelope already fails session lookup
     (`src/gobby/hooks/events.py::require_hook_machine_id`) before any gate
     runs. The foreign classification is a backstop for that case.
8. **Where the facts and origin live.** One delivery-scoped `ContextVar`
   (`src/gobby/hooks/hook_delivery.py`, new) holds both.
   - `run_adapter_hook` sets it. This follows the `worker_staging_scope`
     precedent (#21427): rule evaluation, which hops to the workflow runtime
     thread and the rule-engine executor, inherits it.
   - `file_facts` is popped from the payload before `adapter.handle_native`, so
     it never enters `HookEvent.data` or `HookEvent.metadata`. Those are
     broadcast, sent to webhooks, and templated.
   - Rejected alternatives:
     - A private `input_data` key, because adapters translate per provider.
     - A `HookEvent` attribute, because five `handle_native` overrides would
       each need it.
9. **Which hooks carry facts.**
   - ghook reads the provider tool input from the first present key of
     `tool_input`, `toolArgs`, `parameters`, `args` (`TOOL_INPUT_SOURCES`,
     `src/gobby/hooks/_normalization_tool_input.py`). JSON-string tool input
     is decoded first.
   - Inside it, ghook takes the path of every object that has both a path key
     and an edit-text key, plus `patch` headers.
   - Read payloads and `tool_response` objects are never walked. Read carries
     no edit key, and `tool_response` sits outside the tool input.
   - Both PreToolUse and PostToolUse carry facts for write tools. Each
     captures the file as it is on disk when the hook fires.
10. **Extension allow-list.** ghook captures only paths whose suffix is in
    `MONOLITH_SOURCE_EXTENSIONS` (`.py .ts .tsx .css .rs .js .mjs .cjs .sh`).
    - Config and secret files such as `.env`, `.yaml`, `.json` and `.toml` are
      never copied.
    - NotebookEdit targets (`.ipynb`) fall outside the guard: `is_monolith_guard_path`
      rejects them. So the "NotebookEdit projection gap" needs only a
      regression test, not a code change.
    - A Python parity test pins the Rust constants to the Python ones.
11. **Truncation (256 KiB per path, by byte size).**
    - Over the cap, the fact carries `line_count` and `truncated: true` and no
      `content`.
    - Monolith: projects by line delta from `line_count`. This is the existing
      `text is None` branch of `_apply_targeted_edit` and `_apply_patch_projection`.
    - `rust_test_evidence`: fails closed (no text, so the change is not
      test-only). This is the existing `on_disk is None` path.
    - `_record_successful_file_mutation`: reads only `relative_path`, so it is
      unaffected.
    - Nothing else reads facts.
    - There is no total per-envelope cap. A patch over many large files is rare
      and is bounded by the per-path cap.
    - `line_count` counts lines exactly as Python `str.splitlines()` counts the
      decoded text, so a truncated fact and the consumer's `_line_count` agree
      (1.1).
12. **Spool hardening covers both surfaces.**
    - Write `content` and Edit `old_string`/`new_string` are already persisted
      today, in the ghook inbox spool and in the inbox quarantine. Verified at
      `crates/ghook/src/transport.rs::enqueue_to`, which writes the whole
      envelope pretty-printed, and `src/gobby/hooks/inbox.py::_quarantine_file`,
      which byte-copies it.
    - On this machine those files are mode 0644 under 0755 directories. Only
      `~/.gobby` (0700) protects them, and no code enforces that mode.
    - 1.2 makes the inbox directory owner-only on every ghook write. That
      protects the existing `input_data` content and `file_facts` alike,
      including Python's quarantine copies beneath it.
    - The WebSocket `hook_event` broadcast and user-configured webhooks emit
      `input_data` today. They stay as they are because they are
      subscriber-opted, local-auth surfaces. `file_facts` never reaches them
      (decision 8).

## Constraints
`kind: framing`

- **Isolation.** Every pytest run uses
  `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1`
  from the worktree root, never the full suite. No leaf restarts the running
  daemon.
- **Rust.** Load the `rust` skill before editing `crates/`, where
  `crates/CLAUDE.md` governs.
  - `cargo build`, `clippy` and `nextest` are heavy keys. They run one at a
    time, and only on request to the Lane Manager.
  - Production line counts stop at `#[cfg(test)]`: `transport.rs` is 414
    lines, `dispatch.rs` 516, `envelope.rs` 87, and `diagnostics.rs` 352.
- **Activation.** ghook is in the stamped coherent set (`gcode`, `gdaemon`,
  `ghook`). Live activation belongs to the PD and follows the existing release
  process: crate patch bump, `Cargo.lock` and pins, `promote_workspace_binary_set`,
  then an announced restart. Until a promoted ghook sends `file_facts`, every
  envelope is a local envelope without facts and takes the local-disk path
  (decision 6). Nothing breaks in between.
- **Monolith ceiling.** These targeted files are at or over 850 lines and are
  split in their owning sections:
  - `src/gobby/workflows/hooks.py` (852), in 1.6;
  - `src/gobby/workflows/found_work_gate.py` (877), in 1.9.

  These large files are deliberately not targeted:
  - `commit_guard.py` (956), `safe_evaluator.py` (887),
    `condition_helpers.py` (996) and `inbox.py` (983) are not edited. The facts
    and origin reach their callees through the delivery `ContextVar`.
- **No logging of values.** No new code logs `file_facts`, file content, or
  paths taken from it. `hook_delivery.py` does not log.
- **Consumer sweeps.** Exact-symbol Targets were swept with `gcode grep -w <symbol> src tests`
  on `0.5.0` at `ae78ebdf00` (2026-10-04). They were re-verified on
  `e95a37ffeb` (2026-10-05) with project-aware `gobby plans validate`.
  - Since `ae78ebdf00`, no targeted file has changed.
  - The changed neighbours are `engine/effects.py` (one `resolve_uv_run`
    argument), `evaluation_runtime.py` (closes cancelled, unstarted
    coroutines) and `_normalization_shell.py` (ANSI-C quoting, parse-only).
    None of them reads disk or git for a hook.
  - Each deliverable lists its owned consumers in Targets or in Consumers
    unchanged.

## D1.7 Mapping
`kind: framing`

#23277 carries original item D1.7 from api-keys D5: "the corpus manifest and
every case are at `schema_version` 2 … both the Python loader/replay and the
Rust replay pass at that version". The wait was inherited because a new
envelope field might touch the corpus.

The evidence shows it does not:
- The corpus is `tests/contracts/http/manifest.json` plus 11 cases, all at
  `schema_version` 1.
- None of the 11 cases records `/api/hooks/*`. The routes are health, config,
  tasks, sessions, the runtime handshake and the embeddings auth cases.
- Neither loader validates request bodies against a schema:
  `tests/contracts/http_corpus.py::load_cases` checks versions, families and
  backends, and `crates/gdaemon/tests/http_contracts.rs::load_all_cases` checks
  versions.
- The Rust stub's exact-body match (`assert_request_unchanged`) applies only
  to recorded cases.
- Corpus `schema_version` is the case-format version. It is unrelated to the
  envelope's `SCHEMA_VERSION`.

Disposition:
- `file_facts` changes no corpus case, loader or replay, so 1.1–1.15 do not
  wait on D1's corpus bump.
- D1.7 itself stays owned by #23273 (Hub-side key validation, front-door
  identity, and shared-token cutover).
- This plan keeps a D1 dependency only where one is real: D2 binds origin to
  the authenticated key, which D1 introduces.
- V2 reruns both corpus suites unchanged, as the evidence that nothing here
  disturbs them.
- If S2.11 later adds a hook corpus family, those cases pin `file_facts`
  through the exact-body match. That is S2.11's concern (D1).

## Persistence Inventory
`kind: framing`

Every place a hook envelope or its `input_data` is written or emitted today
(verified on `0.5.0`), and what happens to `file_facts`:

| Surface | What it holds today | `file_facts` |
| --- | --- | --- |
| ghook inbox spool `~/.gobby/hooks/inbox/{c,n}-*.json` (`crates/ghook/src/transport.rs::enqueue_to`) | Whole envelope, including Write/Edit/patch text, until settled or replayed | Carried. Protected by 1.2's owner-only inbox directory. Removed by the existing settle or replay. |
| Delivery receipt overwrite (`crates/ghook/src/dispatch.rs::settle_delivered_inbox`) | Ids and generation only | Not carried |
| Inbox quarantine `inbox/quarantine/*` (`src/gobby/hooks/inbox.py::_quarantine_file`) | Byte copy of the envelope, kept 24 h | Carried, inside the 1.2 owner-only inbox |
| ghook malformed-stdin quarantine (`transport.rs::quarantine_malformed_at`) | Raw stdin base64 (unparsed, so no envelope) | Not carried. The 1.2 owner-only inbox covers it too. |
| HTTP POST body and inbox replay `_post_envelope` | In transit | Carried in transit. Popped before the adapter (decision 8). |
| Processed markers `~/.gobby/hooks/processed/` (`envelope_dedupe.py`) | Hook response, not input | Not carried |
| ghook failure artifacts (`crates/ghook/src/diagnostics.rs::FailureArtifact`) | Metadata plus up to 8 KiB of the daemon response body | Not carried. `hook_delivery.py` raises no exception that embeds a fact value. |
| Daemon DB rows (receipts, `pending_interactions`, `loop_progress`, `unmodeled_observation_events`, `workflow_audit_log`, `spans`, edit ledgers, `session_variables`) | Metadata, paths, key names, hashes. `pending_interactions` holds web-chat `arguments` only. | Never: facts never enter `HookEvent.data` or `metadata` |
| Daemon logs (`_hook_log_extra`, inbox, adapter timing) | Hook type, shape, ids, timing | Never logged |
| WebSocket `hook_event` broadcast (`src/gobby/hooks/broadcaster.py::broadcast_hook_event`), webhooks (`src/gobby/hooks/webhooks.py::_build_payload`) | `HookEvent.data` (and `metadata` for webhooks), so `input_data` content | Never: these surfaces serialize only data and metadata |
| Transcript archive (`sessions/transcript_archive.py::backup_transcript`) | The CLI's own transcript | Unrelated to envelopes |
| HTTP contract corpus | No hook cases | Unaffected (§D1.7 Mapping) |

1.3's acceptance pins the "never" rows with a test.

## Site Disposition
`kind: framing`

Typed dispositions apply to **foreign-origin** events. Local-origin behavior
is unchanged except where `envelope-fact` applies.

- **`envelope-fact`**: the site reads `file_facts` first, on every machine
  (decision 6).
- **`fail-closed`**: the gate blocks, and its reason carries
  `FOREIGN_ORIGIN_DIAGNOSTIC`.
- **`degrade-existing`**: the probe is skipped, and the site takes its existing
  no-answer path, which already fails closed (evidence cited).
- **`skip`**: an advisory step or side effect does not run for this event.
- **`no-change`**: the site receives `project_path=None` and needs no edit.

| # | Site (`0.5.0`) | Trigger | Disposition | Owner |
| --- | --- | --- | --- | --- |
| 1 | `src/gobby/workflows/monolith_guard.py::_projection_for_path`, via `projected_monolith_paths` | `require-decompose-monolith-before-threshold-write` (before_tool writes) | `envelope-fact`. A foreign uncarried path is reported unverifiable, which is `fail-closed`. | 1.4, 1.5 |
| 2 | `monolith_guard.py::outstanding_monolith_paths` | `require-monolith-resolution-before-commit`, `-before-task-transition`, `-before-turn-end` | `fail-closed`: every ledger guard path is reported unverifiable | 1.7 |
| 3 | `src/gobby/workflows/rust_test_evidence.py::rust_edit_is_test_writing` via `_read_absolute_text` | `enforce-tdd-block`, `enforce-tdd-track-tests` | `envelope-fact`. A foreign uncarried path takes `degrade-existing`: no text means not test-only. | 1.4 |
| 4 | `src/gobby/workflows/tdd_paths.py::tdd_path_identity`, `same_repo_worktree_root` (git identity) | `enforce-tdd-block` | `degrade-existing`: normalized-string identity, the existing `_GitLookupFailed` fallback | 1.8 |
| 5 | `src/gobby/workflows/commit_guard.py::foreign_dirty_edit_conflict` | `block-cross-session-foreign-dirty-edit` | `fail-closed` on a database ledger claim by another session, with no git confirmation and no release | 1.6 |
| 6 | `commit_guard.py::foreign_staged_commit_conflict` | `block-cross-session-foreign-staged-commit` | `fail-closed` on every parsed `git commit` | 1.6 |
| 7 | `src/gobby/workflows/code_review_scope.py::inspect_commit_review_scope` | `require-code-review-skill` | `degrade-existing`: not inspected, and the defaults `commit_has_reviewable_paths=True` and `session_owned_reviewable_paths=None` keep the gate armed | 1.6 |
| 8 | `src/gobby/workflows/ledger_reconcile.py::reconcile_edit_ledgers` | after_tool git activity | `skip`: no release. This follows from `project_path=None`, and 1.6 pins it with a test. | 1.6 |
| 9 | `src/gobby/hooks/code_navigation_recovery.py::_verified_source_line_count`, `_ignore_decision`, `_git_ignored` | `prefer-gcode-for-source-read`, `navigation_requires_index` | `degrade-existing`: line count unverified, so the read counts as broad and gets the existing redirect. Not treated as ignored. | 1.10 |
| 10 | `src/gobby/hooks/_path_scope.py::apply_path_scope_metadata`, `current_project_root` | normalization of write and navigation events | `degrade-existing`: no root found, so paths count as in-project and `canonical_repo_mutation` stays armed | 1.11 |
| 11 | `src/gobby/hooks/event_handlers/_tool.py::ToolEventHandlerMixin._record_successful_file_mutation` and `_resolve_repo_edit_paths`, `_paths_landed_before_edit`, `_notify_code_index` | PostToolUse edits | `envelope-fact` for the ledger path (`relative_path`). `skip` for the git-log landed check and the gcode index notify. | 1.12 |
| 12 | `src/gobby/workflows/engine/run_command_effects.py::RunCommandEffectsMixin._apply_run_command` (the impeccable `hook.mjs` spawn) | `impeccable/design-detector` | `skip`: advisory and documented fail-open. Audited as skipped. | 1.13 |
| 13 | `src/gobby/workflows/found_work_gate.py::FoundWorkStopAnalyzer._terminal_failures` (transcript derivation via `derive_transcript_evidence`), `validation_cover.py::surviving_path_failure`, `_foreign_owned_dirty_paths` | `enforce-found-work-ladder` (turn_end) | `fail-closed`: when the gate needs terminal evidence (no task disposition), it reports the diagnostic as a terminal failure. No transcript derivation, cover, clearance or git probe runs. | 1.9 |
| 14 | `src/gobby/hooks/project_context.py::ProjectIdResolver.resolve` (cwd-marker walk `get_project_context`, then `ensure_project_in_db` into `project_checkout_ingress.register_cwd_marker_checkout`: checkout validation, Cargo target linking, marker refresh) | hook ingress project resolution when no explicit or session project resolves (`resolve_hook_project_context`, `_session_end`, `_agent`, `_tool`) | `fail-closed`: the resolver raises the existing no-marker `ValueError` with `FOREIGN_ORIGIN_DIAGNOSTIC`, and the route's `_hook_exception_response` blocks critical hooks and degrades the rest. Explicit and session resolutions are database-only and unchanged. | 1.15 |
| 15 | `src/gobby/hooks/startup_claim_preflight.py::_resolve_or_register_session` (resolver call and direct `get_project_context` walk) | AGY pre-invocation startup claim, before the delivery scope exists | `skip`: a `machine_id_error` envelope takes no lease, like the existing foreign `machine_id` rejection, and the hook continues to row 14 | 1.15 |
| s1 | `src/gobby/workflows/engine/proxy_hooks.py::ProxyHooksMixin._run_rtk_proxy` | `rtk-command-rewrite` | `skip`: no rewrite, so the command runs as written | 1.14 |
| s2 | `src/gobby/config/validation_detection.py::load_project_validation_detection` (through `found_work_gate.py::resolve_stop_validation_config`) | found-work and validation detection | `no-change`: `project_path=None` gives the built-in config | — |
| s3 | `src/gobby/workflows/condition_helpers_paths.py::write_paths_within` (seat write scope) | `seat-write-scope` | `degrade-existing` through 1.11's `current_project_root` guard: no root, so relative allowed directories are unresolvable and the write is outside scope (the existing block) | 1.11 |
| s4 | `src/gobby/workflows/enforcement/blocking.py::plan_write_paths_allowed` (`realpath`) | plan-mode write gate | `no-change`: `project_path=None` skips the root comparison, so only plan artifacts and scratch paths pass | — |
| s5 | `src/gobby/workflows/observer_plan_mode.py` Codex transcript read | plan-mode observer | Out of scope: a transcript is not the working tree. It belongs to S2.11 with session ingestion (D1). | D1 |

## P1: Edited-file facts and origin-aware hook evaluation
`kind: framing`

**Goal**: rules that judge an edited file read what the editing machine saw.
A hook from another machine never reads this machine's disk or git, and every
gate that needed that answer fails closed with a named diagnostic.

### 1.1 ghook captures `file_facts` [category: code]
`kind: deliverable`

Targets:
- `crates/ghook/src/file_facts.rs`
- `crates/ghook/src/main.rs::*` — scope-reason: adds only the `mod file_facts;` declaration beside the existing module list
- `crates/ghook/src/envelope.rs::Envelope`
- `crates/ghook/src/envelope.rs::Envelope::new`
- `crates/ghook/src/dispatch.rs::build_dispatch_envelope`
- `crates/ghook/src/diagnostics.rs::envelope`
- `crates/ghook/schemas/inbox-envelope.v1.schema.json::*` — scope-reason: adds the optional `file_facts` property to the schema
- `schemas/inbox-envelope.v1.schema.json::*` — scope-reason: byte-identical public mirror of the same schema change
- `docs/guides/hook-schemas.md`
- `docs/guides/ghook-development-guide.md`

**Granularity:** one section. It has six acceptance items and one outcome:
ghook attaches `file_facts`. The production files are `file_facts.rs`
(new), a one-line `mod` in `main.rs`, `envelope.rs`, `dispatch.rs` and two
byte-identical schema copies. `diagnostics.rs::envelope` is a `#[cfg(test)]`
helper. Collection without the envelope field, or the field without
collection, verifies nothing on its own.

**Research context:**
- ghook builds every envelope in `crates/ghook/src/dispatch.rs::build_dispatch_envelope`.
  - That function calls `inject_machine_identity` and `terminal_context::inject`,
    then `Envelope::new(critical, hook_type, input_data, source, headers)`.
  - `Envelope` (`crates/ghook/src/envelope.rs`) serializes `schema_version`,
    `response_capability` (an `Option` with `skip_serializing_if`),
    `enqueued_at`, `critical`, `hook_type`, `input_data`, `source` and
    `headers`.
- Today ghook reads only `.gobby/project.json`
  (`project_root_from_workspace_paths`, `managed_context`) and never the edited
  file.
- `crates/ghook/src/diagnostics.rs::envelope` is a test helper that builds an
  exhaustive `Envelope { … }` literal, so it must name the new field. Every
  other construction goes through `Envelope::new`.
- Both schema copies have `additionalProperties: false`. `envelope.rs::inbox_envelope_schema_mirrors_are_byte_identical`
  keeps them byte-identical. No Python code loads either schema.
- The envelope fields are documented in two places: the "HTTP Request
  Envelope" section of `docs/guides/hook-schemas.md`, and the "Field
  Semantics" table of `docs/guides/ghook-development-guide.md`.
- Failure artifacts copy only `hook_type`, `source` and `critical` from the
  envelope (`crates/ghook/src/diagnostics.rs::FailureArtifact::from_context`),
  so facts never reach them.
- No request-body limit exists on the hook path. Searches of
  `src/gobby/servers`, `src/gobby/hooks` and `crates/` found no body cap and
  no `Limited` or `DefaultBodyLimit`. The per-path cap is the only bound.
- The Python consumers (1.4) decide the fact contract:
  - `monolith_guard._projection_for_path` resolves a raw path against the
    project root and filters with `is_monolith_guard_path` (repo-relative).
  - `rust_test_evidence._read_absolute_text` needs an absolute `.rs` path.
  - Both read text with `encoding="utf-8", errors="replace"`.
- Provider tool input keys come from `TOOL_INPUT_SOURCES = ("tool_input", "toolArgs", "parameters", "args")`
  (`src/gobby/hooks/_normalization_tool_input.py`), and a string value is JSON,
  except for apply_patch.
- apply_patch input is freeform patch text, never JSON
  (`src/gobby/hooks/_normalization_tools.py::normalize_tool_fields` skips the
  decode when the compact tool name is `applypatch`).
  - `_normalization_paths.py::_compact_tool_name` casefolds the tool name and
    keeps only alphanumerics.
  - `_extract_apply_patch_text` takes a raw string, or the first string among
    `_APPLY_PATCH_TEXT_FIELDS` (`command`, `patch`, `content`, `text`, `diff`).
  - `_parse_apply_patch_paths` strips each line, then matches
    `_APPLY_PATCH_FILE_RE` and `_APPLY_PATCH_MOVE_RE`.
  - `tests/hooks/test_normalization.py` pins a raw patch string normalizing to
    a canonical Write.
- Path transformations before the consumers: `_normalization_paths.py::_append_unique_path`
  strips canonical paths, and `condition_helpers.py::_normalize_condition_path`
  turns backslashes into slashes before `rust_edit_is_test_writing`.
- Line counts: `monolith_guard.py::_line_count` is `len(text.splitlines())`
  over text read with universal newlines. Python's `splitlines` breaks on
  `\n`, `\r`, `\r\n` (one boundary), `\v`, `\f`, `\x1c`, `\x1d`, `\x1e`,
  `\x85`, ` ` and ` `, and counts a final unterminated line.
- Edit-text keys are the union of the consumers' sets:
  - `_TARGETED_EDIT_KEYS` in `monolith_guard.py`;
  - `_NEW_TEXT_KEYS`, `_OLD_TEXT_KEYS`, `_CONTENT_KEYS` and `_NESTED_EDIT_KEYS`
    in `rust_test_evidence.py`.
- Path keys are `file_path`, `path`, `notebook_path` and `filePath`
  (`rust_test_evidence._EDIT_PATH_KEYS`).
- Extensions come from `monolith_guard.MONOLITH_SOURCE_EXTENSIONS`.

**Implementation:**
- Add `crates/ghook/src/file_facts.rs` with
  `pub fn collect(input_data: &Value, project_root: Option<&Path>) -> Option<BTreeMap<String, FileFact>>`
  (new).
- Constants (new):
  - `TOOL_INPUT_KEYS`, mirroring `TOOL_INPUT_SOURCES`;
  - `EDIT_TEXT_KEYS`: `content`, `old_string`, `new_string`, `old_str`,
    `new_str`, `oldStr`, `newStr`, `old_text`, `new_text`, `oldText`,
    `newText`, `old`, `new`, `replacement`, `edits`, `changes`;
  - `PATH_KEYS`;
  - `GUARDED_EXTENSIONS`, mirroring `MONOLITH_SOURCE_EXTENSIONS`;
  - `CONTENT_CAP_BYTES = 262_144`.
- Discovery:
  1. Take the first present tool-input key.
     - When the compact tool name (`input_data.tool_name`, casefolded,
       alphanumerics only) is `applypatch`, the patch text is a raw string
       value, or the first string among `command`, `patch`, `content`, `text`
       and `diff` of an object value. Take its header paths (step 4), then go
       to step 5.
     - Otherwise decode a string value as JSON, and return `None` if it is not
       an object.
  2. Walk only that value, objects and arrays, to depth 6.
  3. From each object that has a `PATH_KEYS` string and at least one
     `EDIT_TEXT_KEYS` key, take the path.
  4. From patch text (step 1, or a `patch` string found in step 2), strip
     each line and take each path after `*** Add File: `,
     `*** Update File: `, `*** Delete File: ` and `*** Move to: `, matching
     `_parse_apply_patch_paths`.
  5. Keep only paths whose lowercase suffix is in `GUARDED_EXTENSIONS`.
- Resolution:
  - The wire key is the raw path string exactly as it appeared. The
    filesystem path is that string after the consumers' lexical
    normalization: `trim()`, then backslashes to slashes.
  - An absolute path is used as-is.
  - A relative path joins `project_root` when present, otherwise
    `input_data.cwd`.
  - `relative_path` is the canonicalized path relative to the canonicalized
    project root, in POSIX form.
    - For a missing target, canonicalize its deepest existing ancestor, then
      append the missing suffix with lexical `.`/`..` normalization. A missing
      in-root target therefore keeps its repo-relative identity.
    - `null` means the path is outside the root or there is no root, never
      merely that the file is missing.
- Fact per path, keyed by the raw path string:
  `{ "relative_path": string|null, "exists": bool, "line_count": int, "content"?: string, "truncated": bool }`.
  - `exists` is true for a regular file, following symlinks, like Python's
    `is_file()`.
  - For a regular file, read it whole and decode it with
    `String::from_utf8_lossy`. `line_count` counts lines with the
    `str.splitlines()` boundaries listed above: `\r\n` is one boundary, and a
    final unterminated line counts.
  - Up to the cap, `content` is the decoded text. Over the cap, there is no
    `content` and `truncated` is true. The count is over the whole file either
    way, so no streaming decoder state is needed.
  - A missing path or a non-regular file gets `exists: false`,
    `line_count: 0` and `truncated: false`.
  - Read errors are treated as missing.
  - The function returns `None` when no path qualifies, so the field is
    omitted.
- `Envelope` gains `#[serde(skip_serializing_if = "Option::is_none")] pub file_facts: Option<BTreeMap<String, FileFact>>`.
  - `Envelope::new` sets `None`.
  - `build_dispatch_envelope` sets `envelope.file_facts = file_facts::collect(&input_data, project_root)`
    before it returns. `project_root` is the root already found for the
    project id (`project_root_from_workspace_paths`). The function gains that
    parameter or computes it once.
- Add a `file_facts` property to both schema copies: an object of objects with
  `required: [exists, line_count, truncated]` and
  `additionalProperties: false`. Leave it out of the top-level `required`.
- Document the field and its never-persisted, never-logged rule in
  `docs/guides/hook-schemas.md`. Add a `file_facts` row to the Field
  Semantics table in `docs/guides/ghook-development-guide.md`.
- No logging.

**Focused verification (planned):** run `cargo nextest run -p ghook` and
`cargo clippy -p ghook --all-targets -- -D warnings`. Both need a heavy key
from the Lane Manager.

**Acceptance:**

- 1.1.1 - Write, Edit, MultiEdit `edits[]` and apply_patch targets under every `TOOL_INPUT_SOURCES` key, including JSON-string tool input, a freeform apply_patch string, and each apply_patch object field (`command`, `patch`, `content`, `text`, `diff`), yield facts keyed by the raw path. A non-patch, non-JSON string yields no facts. test: `crates/ghook/src/file_facts.rs::collects_write_edit_multiedit_and_patch_targets`.
- 1.1.2 - Read payloads, `tool_response` content, and paths outside `GUARDED_EXTENSIONS`, such as `.env`, `.json` and `.ipynb`, yield no facts and omit the field. test: `crates/ghook/src/file_facts.rs::ignores_reads_responses_and_unguarded_extensions`.
- 1.1.3 - `line_count` equals Python `len(text.splitlines())` for empty, trailing-newline, LF, CRLF, CR and every other `splitlines` boundary. A file over 262,144 bytes, including one whose 1,000 lines are CR-separated, carries that count and `truncated: true` with no `content`. A missing path carries `exists: false`. test: `crates/ghook/src/file_facts.rs::caps_content_and_reports_missing_files`.
- 1.1.4 - A relative path resolves against the project root. A missing in-root target, including one under missing parent directories, keeps its `relative_path`. A raw key with surrounding whitespace or backslashes reads the normalized file. `relative_path` is null outside the root, including through an existing symlinked ancestor that points outside. test: `crates/ghook/src/file_facts.rs::resolves_relative_paths_against_project_root`.
- 1.1.5 - An envelope with `file_facts` validates against both byte-identical v1 schema copies, and one without it still validates. test: `crates/ghook/src/envelope.rs::envelope_with_file_facts_validates_against_v1_schema`.
- 1.1.6 - `build_dispatch_envelope` attaches facts for a PreToolUse Edit payload and omits the field for a SessionStart payload. test: `crates/ghook/src/dispatch.rs::dispatch_envelope_attaches_file_facts_for_edits`.

### 1.2 Owner-only hook inbox [category: code]
`kind: deliverable`

Targets:
- `crates/ghook/src/transport.rs::enqueue_to`
- `crates/ghook/src/transport.rs::quarantine_malformed_at`

**Research context:**
- `crates/ghook/src/transport.rs::enqueue_to` writes the whole envelope with
  `atomic_write`, whose `atomic_write_with` calls `fs::create_dir_all` under
  the default umask.
- `quarantine_malformed_at` writes raw stdin base64 twice under
  `inbox/quarantine/`.
- Python's `src/gobby/hooks/inbox.py::_quarantine_file` byte-copies envelopes
  into `inbox/quarantine/`, which sits beneath the inbox directory.
- Observed modes on this machine: `~/.gobby` 0700, but `hooks`, `inbox` and
  `inbox/quarantine` are 0755. No code enforces owner-only on `~/.gobby`
  (searched for `0o700` and `S_IRWXU`).
- Write `content` and Edit `old_string`/`new_string` already persist in these
  files today, and `file_facts` (1.1) joins them (Decision 12).
- One directory mode protects every file beneath it: envelopes, receipts,
  ghook quarantine and Python quarantine. That avoids touching
  `src/gobby/hooks/inbox.py`, which is 983 lines.
- Rejected alternative: per-file 0600 modes in `atomic_write`. They would
  change unrelated runtime-stamp and diagnostics writers, and still leave
  Python's quarantine copies exposed.
- Existing enqueue-failure handling (`crates/ghook/src/dispatch.rs`, the
  `direct_post_after_enqueue_failure` closure in the dispatch entry):
  - A normal run falls back to a direct POST with no spool copy, so the hook
    is still delivered while the daemon is up.
  - An `--enqueue-only` run records a failure artifact. It then fails open or
    emits the failure action, following
    `planned_shutdown::fail_open_after_enqueue_failure`.
- Sandboxed CLIs grant ghook write access to `$GOBBY_HOME/hooks/inbox`
  (`src/gobby/agents/sandbox_policy.py::previous_run_write_paths` documents
  the shared inbox grant). A mode change on that directory is a write on the
  granted path. If a
  sandbox still refuses it, the fallback above applies and no content is
  written to a directory that is not owner-only.

**Implementation:**
- Add `fn ensure_owner_only_dir(dir: &Path) -> Result<()>` (new, Unix: create
  the directory if absent, then `set_permissions(0o700)` when the mode differs;
  a no-op elsewhere).
- Call it on the inbox directory in `enqueue_to` before the write, and in
  `quarantine_malformed_at` before its writes.
- Treat a permission failure as an enqueue failure, so it takes the existing
  `direct_post_after_enqueue_failure` path above. Add no new fallback.

**Focused verification (planned):** run `cargo nextest run -p ghook transport`
with a heavy key.

**Acceptance:**

- 1.2.1 - `enqueue_to` leaves the inbox directory at mode 0700, including when it already existed as 0755. test: `crates/ghook/src/transport.rs::enqueue_makes_inbox_owner_only`.
- 1.2.2 - `quarantine_malformed_at` leaves the inbox directory at mode 0700. test: `crates/ghook/src/transport.rs::quarantine_malformed_makes_inbox_owner_only`.

### 1.3 Daemon delivery scope carries `file_facts` and hook origin [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `src/gobby/hooks/hook_delivery.py`
- `src/gobby/servers/routes/mcp/hooks.py::_normalize_hook_request`
- `src/gobby/hooks/adapter_execution.py::run_adapter_hook`
- `tests/hooks/test_hook_delivery.py`

Consumers unchanged:
- `tests/agents/test_spawn_executor.py` — no-edit-reason: It patches `run_adapter_hook`, whose signature is unchanged.
- `tests/hooks/test_staged_effects_receipt_route.py` — no-edit-reason: Same unchanged `run_adapter_hook` signature.
- `tests/servers/test_mcp_routes.py` — no-edit-reason: Same; envelopes without `file_facts` normalize as before.
- `tests/workflows/test_hook_evaluation_timeout.py` — no-edit-reason: Same unchanged `run_adapter_hook` signature.
- `tests/workflows/test_hook_saturation.py` — no-edit-reason: Same unchanged `run_adapter_hook` signature.

**Research context:**
- `src/gobby/servers/routes/mcp/hooks.py::_normalize_hook_request` copies only
  `hook_type`, `input_data` and `source` into the normalized payload. Every
  other top-level key is dropped today.
- `src/gobby/hooks/adapter_execution.py::run_adapter_hook` runs
  `adapter.handle_native(payload, hook_manager)` inside `run_adapter`, within
  `worker_staging_scope()`. Its comment records that rule evaluation hops to
  the workflow runtime thread and the rule-engine executor, and both inherit
  this context (#21427).
- Five adapters override `handle_native`: claude_code, codex hooks, droid, agy
  and acp. Placing the facts in the context avoids touching them.
- The inbox drain replays through the same HTTP route
  (`inbox.py::_post_envelope`), so replayed envelopes get the same scope.
- `HookEvent.data` is broadcast (`broadcaster.py::broadcast_hook_event`).
  `data` and `metadata` both go to webhooks (`webhooks.py::_build_payload`).
  That is why the facts must stay out of both (Decision 8).
- Other consumers of the normalized payload in the route read named fields
  only. Neither serializes the payload whole:
  - `src/gobby/hooks/startup_claim_preflight.py::preflight_agy_startup_claim_bounded`
    runs before `run_adapter_hook`;
  - `src/gobby/servers/routes/mcp/hook_hold_open.py::_maybe_hold_open` runs
    after it and stores only `tool_name` and `arguments`, or the `question`.

  `_hook_log_extra` logs `request_metadata`, which never holds facts.
- Local machine id: `gobby.utils.machine_id.get_machine_id()`. ghook stamps
  `input_data.machine_id` from the same `<gobby_home>/machine_id` file, or
  `machine_id_error` when it cannot (`dispatch.rs::inject_machine_identity`).
- Test fixtures stay local. Every test that posts an envelope with a
  `machine_id` through `/api/hooks/execute` matches the daemon id in one of
  three ways:
  - It pins `gobby.utils.machine_id._cached_machine_id` to that id
    (`tests/hooks/test_staged_effects_receipt_route.py`,
    `tests/servers/test_mcp_routes.py`; `tests/conftest.py::_stable_machine_identity`).
  - It writes the same id into the e2e daemon home (`tests/e2e/conftest.py`).
  - It mocks the hook manager, so no rule evaluates
    (`tests/servers/routes/test_hooks_agy_dispatch.py`).

  `tests/conftest.py::mock_machine_id` patches
  `gobby.utils.machine_id.get_machine_id`. No hook-route test uses it.
- Rejected alternatives:
  - A private `input_data` key, because adapters translate input per provider
    and would have to preserve it.
  - A new `HookEvent` field, because five `handle_native` overrides would need
    it.

**Implementation:**
- New module `src/gobby/hooks/hook_delivery.py` (new symbols):
  - `FileFact`, a frozen dataclass: `relative_path: str | None`, `exists`,
    `line_count`, `content: str | None`, `truncated`.
  - `HookDelivery`, a frozen dataclass: `origin_local: bool`,
    `origin_machine_id: str | None`, `facts: Mapping[str, FileFact]`.
  - `LOCAL_DELIVERY`, the default: local, no facts.
  - `_current: ContextVar[HookDelivery]`.
  - `hook_delivery_scope(payload)`, a context manager:
    - It pops `payload["file_facts"]` and parses it strictly. A non-mapping is
      ignored, and an entry with wrong types is dropped, never coerced.
    - It classifies origin per Decision 7. It calls
      `gobby.utils.machine_id.get_machine_id()` through the module at
      classification time, so `_cached_machine_id` pins and
      `patch("gobby.utils.machine_id.get_machine_id")` apply. An `OSError`
      or `None` counts as "no local id".
  - `current_hook_delivery()`.
  - `FOREIGN_ORIGIN_DIAGNOSTIC`, a format string: "This hook came from
    machine {machine}. This daemon cannot inspect that machine's checkout, so
    {gate} fails closed until node gates exist (deferred to S2.11, #21569)."
    - `HookDelivery.origin_label` fills `{machine}`. It is the machine id, or
      `"unidentified (machine_id_error=<code>)"` when ghook sent only an
      error code.
  - `fact_for(raw_path)` and `fact_text(raw_path)` look a fact up in this
    order:
    1. the exact raw path string as a key;
    2. a lexical alias: `normalize(s) = s.strip().replace("\\", "/")`
       compared with the normalized keys and the normalized non-null
       `relative_path` values. A unique fact matches. Aliases that reach
       differing facts return `None` (unverified), never a choice by map
       order.

    No lookup uses `resolve`, `realpath`, suffix matching or the evaluator's
    disk.
- `_normalize_hook_request` adds `"file_facts": payload.get("file_facts")` to
  the normalized payload only when present.
- `run_adapter_hook.run_adapter` enters `hook_delivery_scope(payload)` beside
  `worker_staging_scope()`. The pop happens before `handle_native`, so the
  adapter never sees the key.
- No rule reads facts yet. 1.4 adds the consumers.

**Focused verification (planned):**
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/hooks/test_hook_delivery.py tests/servers/test_mcp_routes.py tests/hooks/test_staged_effects_receipt_route.py -q`,
then `uv run ruff check`, `uv run mypy src/`, and the scoped `gobby test-types audit`.

**Acceptance:**

- 1.3.1 - `file_facts` reaches rule evaluation through the delivery context, also on the workflow runtime and rule-engine threads, and never appears in `HookEvent.data`, `HookEvent.metadata`, the broadcast payload or the webhook payload. test: `tests/hooks/test_hook_delivery.py::test_file_facts_reach_rules_but_not_event_payloads`.
- 1.3.2 - Origin is foreign when `machine_id` differs, when `machine_id_error` is present, or when the daemon has no local id and the payload carries one. It is local otherwise, including when the payload has no machine identity. The diagnostic names the machine id or the error code. test: `tests/hooks/test_hook_delivery.py::test_origin_classification`.
- 1.3.3 - Malformed `file_facts` entries are dropped without logging. test: `tests/hooks/test_hook_delivery.py::test_malformed_file_facts_are_dropped`.
- 1.3.4 - The Rust `TOOL_INPUT_KEYS`, `PATH_KEYS`, `EDIT_TEXT_KEYS` and `GUARDED_EXTENSIONS` constants in `crates/ghook/src/file_facts.rs` match the Python consumer key sets and `MONOLITH_SOURCE_EXTENSIONS`. test: `tests/hooks/test_hook_delivery.py::test_ghook_file_fact_constants_match_python`.
- 1.3.5 - `fact_for` finds a fact by exact raw key, then by a whitespace- or separator-normalized raw key or `relative_path`. Colliding aliases with differing facts return `None`. No lookup touches disk. test: `tests/hooks/test_hook_delivery.py::test_fact_lookup_aliases_are_lexical_and_unambiguous`.
### 1.4 Monolith projection and Rust TDD classification read `file_facts` [category: code] (depends: 1.3)
`kind: deliverable`

Targets:
- `src/gobby/workflows/monolith_guard.py::_projection_for_path`
- `src/gobby/workflows/monolith_guard.py::projected_monolith_paths`
- `src/gobby/workflows/rust_test_evidence.py::rust_edit_is_test_writing`
- `src/gobby/workflows/rust_test_evidence.py::_read_absolute_text`
- `tests/workflows/test_monolith_guard.py::*` — scope-reason: adds fact-sourced projection tests beside the existing disk-sourced ones
- `tests/workflows/test_rust_test_evidence.py::*` — scope-reason: adds fact-sourced classification tests

Consumers unchanged:
- `src/gobby/workflows/safe_evaluator.py` — no-edit-reason: `_projected_monolith_paths` calls `projected_monolith_paths` with an unchanged signature; facts arrive through the delivery context.
- `src/gobby/workflows/condition_helpers.py` — no-edit-reason: `_is_tdd_test_path` calls `rust_edit_is_test_writing(path, tool_input)` with an unchanged signature.
- `src/gobby/install/shared/workflows/rules/monolith-enforcement/require-same-session-decomposition.yaml` — no-edit-reason: The rule lists whatever paths `projected_monolith_paths` returns, including the unverifiable marker.

**Research context:**
- Facts and origin come from `src/gobby/hooks/hook_delivery.py` (1.3):
  - `current_hook_delivery()` returns a `HookDelivery` with `origin_local`
    and `facts`;
  - `fact_for(raw_path)` and `fact_text(raw_path)` look a fact up by exact
    raw path string, then by an unambiguous lexical alias of a key or
    `relative_path` (1.3.5);
  - a fact is `FileFact(relative_path, exists, line_count, content, truncated)`.
    `content` is `None` when the file is over the 256 KiB cap, and
    `relative_path` is `None` outside the project root.
- Monolith (`src/gobby/workflows/monolith_guard.py`):
  - `projected_monolith_paths(tool_input, project_path, event_data)` returns
    `[]` when `_project_root(project_path)` is `None`. For a foreign event,
    1.6 makes `project_path` `None`.
  - `_projection_for_path(root, raw_path)` reads disk and returns
    `_FileProjection(relative, count, count, text)`.
  - `_apply_targeted_edit` falls back to a line delta when `text` is `None`.
  - `_apply_patch_projection` always uses counts.
  - `_line_count` is `len(text.splitlines())`.
  - It is called from `safe_evaluator.py::_projected_monolith_paths` for the
    `require-decompose-monolith-before-threshold-write` rule.
- Rust (`src/gobby/workflows/rust_test_evidence.py`):
  - `rust_edit_is_test_writing(path, tool_input)` reads
    `_read_absolute_text(path)` and judges each change with
    `_change_is_test_only`.
  - `_before_and_after` applies the edit forward when `old` is found
    (PreToolUse) and in reverse when `new` is found (PostToolUse). Facts
    captured at either phase therefore keep today's semantics.
  - It is called from `condition_helpers.py::_is_tdd_test_path` for the
    `enforce-tdd-block` and `enforce-tdd-track-tests` rules.
- Lookup order is Decision 6: an envelope fact first on every machine, local
  disk only for an uncarried path with local origin, and unverified for an
  uncarried path with foreign origin.

**Implementation:**
- `monolith_guard`:
  - `_projection_for_path` first consults `current_hook_delivery()`:
    - A carried fact gives `text = fact.content`, a count of
      `_line_count(content)` or `fact.line_count` when truncated, and
      `relative = fact.relative_path`.
      - A fact whose `relative_path` is `None` lies outside the project, so the
        function returns `None`, as it does today for a path outside the root.
      - `is_monolith_guard_path(relative)` filters before a `_FileProjection`
        is built, exactly as on the disk path.
      - A fact with `exists: false` projects from `text=None` and a count of 0,
        like a missing file on disk.
    - Otherwise, a local origin reads disk as today.
    - Otherwise (foreign, uncarried) the projection is unverifiable. It is
      reported as `"<raw path> (unverifiable: foreign-origin hook)"` and counts
      as reaching the ceiling.
  - `projected_monolith_paths` no longer returns `[]` for a missing root when
    the delivery carries facts or is foreign. It projects from the facts and
    marks foreign uncarried paths.
- `rust_test_evidence._read_absolute_text` returns the carried fact's
  `content`, which is `None` when truncated or missing. It reads disk only
  when uncarried and the origin is local, and returns `None` when uncarried
  and the origin is foreign.
- Tests set the delivery through `hook_delivery_scope` with a synthetic
  payload. They need no ghook.

**Focused verification (planned):**
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/workflows/test_monolith_guard.py tests/workflows/test_rust_test_evidence.py -q`,
then `uv run ruff check`, `uv run mypy src/`, and the scoped `gobby test-types audit`.

**Acceptance:**

- 1.4.1 - A carried fact wins over disk content. Projection works without a project root, and a truncated fact projects by line delta from its `line_count`, so an over-cap fact at the ceiling blocks. A foreign, rootless Write or apply_patch Add File to a missing in-root `src/` file whose proposed content reaches 1,000 lines blocks without evaluator disk access. A fact with a null `relative_path` is skipped. test: `tests/workflows/test_monolith_guard.py::test_projection_prefers_envelope_facts`.
- 1.4.2 - A foreign uncarried guard path is reported unverifiable and blocks. A local uncarried path still reads disk. test: `tests/workflows/test_monolith_guard.py::test_uncarried_paths_follow_origin`.
- 1.4.3 - Rust test-only classification reads the carried content at both PreToolUse and PostToolUse, and fails closed for truncated or foreign uncarried paths. A raw absolute key with backslash separators or surrounding whitespace, passed through `normalize_tool_fields` and `condition_helpers._is_tdd_test_path`, still uses the carried content, with disk access forbidden for both origins. test: `tests/workflows/test_rust_test_evidence.py::test_classification_reads_envelope_facts`.

### 1.5 Monolith projection applies MultiEdit edits [category: code] (depends: 1.4)
`kind: deliverable`

Targets:
- `src/gobby/workflows/monolith_guard.py::_apply_change_projection`
- `tests/workflows/test_monolith_guard.py::*` — scope-reason: adds MultiEdit and notebook regression tests

**Research context:**
- `src/gobby/workflows/monolith_guard.py::_apply_change_projection` handles a
  top-level `content`, or the first pair in `_TARGETED_EDIT_KEYS`
  (`old_string/new_string`, `old_text/new_text`, `old/new`), with
  `replace_all`.
- `projected_monolith_paths` iterates `changes` lists, but MultiEdit's
  `edits: [{old_string, new_string, replace_all}]` under one `file_path` falls
  through. Only `_fallback_paths` then sees the path, and it is checked at its
  current count only. A MultiEdit that crosses 1,000 lines is not blocked
  today, even on a single machine.
- NotebookEdit targets `.ipynb`. `is_monolith_guard_path` rejects it because
  `.ipynb` is not in `MONOLITH_SOURCE_EXTENSIONS`. That half of the PD's
  "MultiEdit/NotebookEdit gap" needs only a regression test.
- `_apply_targeted_edit` already does exact replacement, with a line-delta
  fallback when the text is unknown.

**Implementation:**
- In `_apply_change_projection`, after the `content` check, apply each element
  of `change["edits"]` (when it is a list of mappings) in order with
  `_apply_targeted_edit`. Use the element's `old_string`/`new_string` and the
  other `_TARGETED_EDIT_KEYS` pairs, plus its `replace_all`, against the same
  projection.
- Sequential application matches MultiEdit semantics.
- Text comes from 1.4's fact lookup in `_projection_for_path` when carried.

**Focused verification (planned):**
`DATABASE_URL=… GOBBY_TEST_PROTECT=1 uv run pytest tests/workflows/test_monolith_guard.py -q`.

**Acceptance:**

- 1.5.1 - A MultiEdit whose sequential edits take a 990-line file to 1,000 or more lines is reported, and one that stays below 1,000 is not. test: `tests/workflows/test_monolith_guard.py::test_multiedit_edits_project_sequentially`.
- 1.5.2 - NotebookEdit `.ipynb` targets stay outside the guard. test: `tests/workflows/test_monolith_guard.py::test_notebook_targets_are_not_guarded`.

### 1.6 Foreign-origin events resolve no checkout and fail the git gates closed [category: code] (depends: 1.3)
`kind: deliverable`

Targets:
- `src/gobby/workflows/hooks.py::WorkflowHookHandler._evaluate_rules`
- `src/gobby/workflows/hooks.py::WorkflowHookHandler._resolve_project_path`
- `src/gobby/workflows/hook_gate_context.py`
- `tests/workflows/test_foreign_origin_gates.py`

Consumers unchanged:
- `tests/workflows/test_database_deadline_retry.py` — no-edit-reason: It drives `_resolve_project_path` through local events, whose behavior is unchanged.
- `tests/workflows/test_hooks.py` — no-edit-reason: Local-origin `_evaluate_rules` and `_resolve_project_path` behavior is unchanged. The moved gate block keeps its lazy imports, so existing `gobby.workflows.commit_guard.*` patch targets still apply.
- `tests/workflows/test_workflow_hooks.py` — no-edit-reason: Same as `test_hooks.py`.
- `tests/workflows/test_commit_guard.py` — no-edit-reason: It drives `_evaluate_rules` with local events and patches `gobby.workflows.commit_guard._active_foreign_path_owners`, which the moved block still resolves lazily from `commit_guard`.
- `tests/workflows/test_code_review_scope.py` — no-edit-reason: It drives `_evaluate_rules` with local events; the moved `inspect_commit_review_scope` call is unchanged for local origin.
- `tests/workflows/test_code_review_freshness.py` — no-edit-reason: Local-origin `_evaluate_rules`; `foreign_landing_merge` moves unchanged and is a database lookup.
- `tests/workflows/test_hook_evaluation_serialization.py` — no-edit-reason: Local-origin `_evaluate_rules`; its `gobby.workflows.hooks.monotonic` patch target stays in `hooks.py`.
- `tests/hooks/test_stop_handoff_pending.py` — no-edit-reason: Calls `_evaluate_rules` with local events, whose behavior is unchanged.
- `tests/mcp_proxy/services/test_tool_proxy_validation.py` — no-edit-reason: Same unchanged local-origin `_evaluate_rules`.
- `tests/servers/test_mcp_routes.py` — no-edit-reason: Same unchanged local-origin `_evaluate_rules`; its envelopes pin the daemon machine id.
- `tests/workflows/test_block_tools_after_handoff_compact.py` — no-edit-reason: Same unchanged local-origin `_evaluate_rules`.
- `tests/workflows/test_call_tool_provider_shapes.py` — no-edit-reason: Same unchanged local-origin `_evaluate_rules`.
- `tests/workflows/test_claim_reconciliation.py` — no-edit-reason: Same unchanged local-origin `_evaluate_rules`.
- `tests/workflows/test_evaluation_runtime.py` — no-edit-reason: Same unchanged local-origin `_evaluate_rules`.
- `tests/workflows/test_hook_evaluation_timeout.py` — no-edit-reason: Same unchanged local-origin `_evaluate_rules`.
- `tests/workflows/test_plan_mode_delivery.py` — no-edit-reason: Same unchanged local-origin `_evaluate_rules`.
- `tests/workflows/test_plan_mode_rules.py` — no-edit-reason: Same unchanged local-origin `_evaluate_rules`.
- `tests/workflows/test_task_enforcement_rules.py` — no-edit-reason: Same unchanged local-origin `_evaluate_rules`.
- `tests/workflows/test_tool_context_rehydration.py` — no-edit-reason: Same unchanged local-origin `_evaluate_rules`.
- `tests/workflows/test_turn_interrupt_stop_gates.py` — no-edit-reason: Same unchanged local-origin `_evaluate_rules`.
- `src/gobby/install/shared/workflows/rules/task-enforcement/block-cross-session-foreign-staged-commit.yaml` — no-edit-reason: `when: foreign_staged_commit_conflict` and `reason: "{{ foreign_staged_commit_conflict }}"` already block on any non-empty string.
- `src/gobby/install/shared/workflows/rules/task-enforcement/block-cross-session-foreign-dirty-edit.yaml` — no-edit-reason: Same pattern with `foreign_dirty_edit_conflict`.

**Research context:**
- `src/gobby/workflows/hooks.py::WorkflowHookHandler._evaluate_rules` (450
  lines; the file is 852 lines) works in this order:
  1. It resolves `worktree_root` with `resolve_git_worktree_root_async(event.cwd, …)`
     (git `rev-parse`).
  2. It calls `_resolve_project_path`, which uses `require_local_machine_id`
     and `require_root` and therefore resolves the evaluator's own checkout.
  3. Only when `project_path` is set, it computes three gate values:
     - `foreign_staged_commit_conflict` (`commit_guard.py::foreign_staged_commit_conflict`:
       `rev-parse`, `diff --cached`, `ls-files`);
     - `foreign_dirty_edit_conflict` (`commit_guard.py::foreign_dirty_edit_conflict`:
       DB claims via `_active_foreign_path_owners`, then git status via
       `_dirty_owned_paths_releasing_clean`, which also releases clean ledger
       rows);
     - `inspect_commit_review_scope` (`code_review_scope.py`, `git diff` and
       `ls-files`).
  4. It runs `reconcile_edit_ledgers` (git status plus releases) on after_tool
     git activity.
- The gate block builds one `eval_context` dict, about 65 lines, ending
  immediately before `pre_eval = deepcopy(variables)` (approximate hint: line
  585):
  - it initializes `foreign_dirty_edit_conflict`, `foreign_landing_merge` and
    `session_owned_reviewable_paths`;
  - it fills the two BEFORE_TOOL conflict strings (under
    `session_id and event.project_id and project_path`);
  - it fills `commit_has_reviewable_paths` and `session_owned_reviewable_paths`
    (under `project_path`);
  - it fills the AFTER_TOOL `foreign_landing_merge`.
- `src/gobby/workflows/code_review_freshness.py::is_foreign_landing_merge`
  reads the worktree and clone registries only, with no git. It is not a
  disk site and keeps its behavior for both origins.
- With `project_path=None`:
  - Both conflict strings stay empty, so the gates fail open.
  - The code-review defaults stay `True`/`None`, so that gate stays armed.
  - Reconcile is skipped.
- Read-only helpers reused from `src/gobby/workflows/commit_guard.py`:
  - `parse_git_commit_invocations` detects a commit;
  - `_canonical_mutation_paths` and `_active_foreign_path_owners` give DB
    ownership without git;
  - `_format_conflict_reason` builds the existing conflict text.
- `commit_guard.py` is 956 lines and is not edited.
- The origin comes from `current_hook_delivery()` (1.3).

**Implementation:**
- Move only the contiguous initialization and git/review gate block of
  `_evaluate_rules` out of `src/gobby/workflows/hooks.py` into the new
  `src/gobby/workflows/hook_gate_context.py`, as
  `async def git_gate_eval_context(db, event, *, session_id, project_path, variables, event_data) -> dict[str, Any]`
  (new). The block runs from `eval_context = {...}` through the AFTER_TOOL
  `is_foreign_landing_merge` assignment, ending immediately before
  `pre_eval = deepcopy(variables)`. That covers:
  - the dict initialization;
  - the BEFORE_TOOL `foreign_staged_commit_conflict` and
    `foreign_dirty_edit_conflict`;
  - `commit_has_reviewable_paths` and `session_owned_reviewable_paths`;
  - the AFTER_TOOL `foreign_landing_merge`.

  `foreign_landing_merge` moves unchanged. `_evaluate_rules` receives the
  returned dict and keeps augmenting it at the existing later sites.
  `pre_eval`, observer execution, ledger reconciliation, `has_dirty_files`,
  target-task state, found-work analysis and unclaimed-work analysis stay in
  `hooks.py`, in their current order.
  - Keep the lazy `from gobby.workflows.commit_guard import …`,
    `code_review_scope` and `code_review_freshness` imports inside the
    function, so existing test patch targets still resolve.
  - `_evaluate_rules` calls it. This split is the `production-size-growth`
    disposition for `hooks.py`.
- Foreign branch of `git_gate_eval_context`, when
  `not current_hook_delivery().origin_local`. It is checked before the
  `project_path` conditions, because a foreign event always has
  `project_path=None`:
  - `foreign_staged_commit_conflict` becomes
    `FOREIGN_ORIGIN_DIAGNOSTIC.format(gate="the git commit guard", …)` when
    `parse_git_commit_invocations` finds a commit in a BEFORE_TOOL shell
    command, and `""` otherwise.
  - `foreign_dirty_edit_conflict` becomes the diagnostic plus the existing
    owner text when `_active_foreign_path_owners` reports another session's
    claim on a canonical mutation path. It is `""` when nothing is claimed.
    There is no git call and no release.
  - The code-review values keep the armed defaults.
- `_resolve_project_path` returns `None` for a foreign-origin event, before
  any checkout lookup.
- `_evaluate_rules` skips `resolve_git_worktree_root_async` for a
  foreign-origin event.
  - With `project_path=None`, `reconcile_edit_ledgers` is skipped by its
    existing condition.
  - Downstream sites receive `None`: found-work, `outstanding_monolith_paths`
    and `plan_write_paths_allowed`.

**Focused verification (planned):**
`DATABASE_URL=… GOBBY_TEST_PROTECT=1 uv run pytest tests/workflows/test_foreign_origin_gates.py tests/workflows/test_hooks.py tests/workflows/test_workflow_hooks.py tests/workflows/test_commit_guard.py tests/workflows/test_code_review_scope.py tests/workflows/test_code_review_freshness.py tests/workflows/test_hook_evaluation_serialization.py tests/workflows/test_database_deadline_retry.py -q`.

**Acceptance:**

- 1.6.1 - A foreign-origin `git commit` blocks with `FOREIGN_ORIGIN_DIAGNOSTIC`, and no git subprocess runs. test: `tests/workflows/test_foreign_origin_gates.py::test_foreign_commit_fails_closed_without_git`.
- 1.6.2 - A foreign-origin write to a path that another session's ledger claims blocks with the diagnostic and releases nothing. An unclaimed path passes. test: `tests/workflows/test_foreign_origin_gates.py::test_foreign_write_on_claimed_path_fails_closed`.
- 1.6.3 - A foreign-origin event resolves no checkout and no git worktree root, even when this machine holds a checkout of the same project. test: `tests/workflows/test_foreign_origin_gates.py::test_foreign_event_resolves_no_local_checkout`.
- 1.6.4 - A foreign-origin after_tool git event skips `reconcile_edit_ledgers`, and the code-review gate stays armed. test: `tests/workflows/test_foreign_origin_gates.py::test_foreign_event_skips_reconcile_and_keeps_review_gate`.
- 1.6.5 - Local-origin gate values returned by `git_gate_eval_context`, including `foreign_landing_merge`, are byte-identical before and after the move, and the later `_evaluate_rules` augmentation runs in its existing order. test: `tests/workflows/test_foreign_origin_gates.py::test_local_gate_context_unchanged_by_move`.

### 1.7 Foreign-origin monolith ledger recount is unverifiable [category: code] (depends: 1.5)
`kind: deliverable`

Targets:
- `src/gobby/workflows/monolith_guard.py::outstanding_monolith_paths`
- `tests/workflows/test_monolith_guard.py::*` — scope-reason: adds the foreign-origin recount test

Consumers unchanged:
- `src/gobby/workflows/safe_evaluator.py` — no-edit-reason: `_outstanding_monolith_paths` calls `outstanding_monolith_paths(variables, project_path)` with an unchanged signature.
- `tests/workflows/test_command_position_patterns.py` — no-edit-reason: It patches `outstanding_monolith_paths` by name; the symbol stays in `monolith_guard`.

**Research context:**
- `src/gobby/workflows/monolith_guard.py::outstanding_monolith_paths(variables, project_path)`:
  - It returns `[]` when the root is `None`, so it fails open.
  - Otherwise it recounts every `task_edited_files` path from disk.
  - It backs three rules in `require-same-session-decomposition.yaml`: commit,
    task transition and turn end.
- The section follows 1.5 because both edit `monolith_guard.py` and
  `test_monolith_guard.py`. Its foreign branch runs before the root check, so
  it needs nothing from 1.6.
- The origin comes from `current_hook_delivery()` (1.3).

**Implementation:**
- For a foreign-origin delivery, return each ledger path that passes
  `is_monolith_guard_path` as `"<path> (unverifiable: foreign-origin hook)"`,
  with no disk read. Local behavior is unchanged.

**Focused verification (planned):**
`DATABASE_URL=… GOBBY_TEST_PROTECT=1 uv run pytest tests/workflows/test_monolith_guard.py -q`.

**Acceptance:**

- 1.7.1 - For a foreign-origin event, every guarded ledger path is reported unverifiable at commit, task transition and turn end, and no file is read. test: `tests/workflows/test_monolith_guard.py::test_foreign_outstanding_paths_are_unverifiable`.

### 1.8 Foreign-origin TDD path identity runs no git [category: code] (depends: 1.3)
`kind: deliverable`

Targets:
- `src/gobby/workflows/tdd_paths.py::tdd_path_identity`
- `src/gobby/workflows/tdd_paths.py::same_repo_worktree_root`
- `tests/workflows/test_tdd_gate_worktree_paths.py::*` — scope-reason: adds the foreign-origin identity test

Consumers unchanged:
- `src/gobby/workflows/condition_helpers.py` — no-edit-reason: It calls `tdd_path_identity` with an unchanged signature.
- `tests/workflows/engine/test_condition_git_off_loop.py` — no-edit-reason: Local-origin `tdd_path_identity` and `_git_identity` behavior is unchanged.

**Research context:**
- `src/gobby/workflows/tdd_paths.py`:
  - `tdd_path_identity` and `same_repo_worktree_root` call `_git_identity`
    (`lru_cache`, `git rev-parse --show-toplevel --git-common-dir`).
  - On `_GitLookupFailed` they fall back to normalized path strings.
  - The guard goes in the callers, because a check inside the cached function
    would cache a per-delivery answer.
- The origin comes from `current_hook_delivery()` (1.3).

**Implementation:**
- For a foreign-origin delivery, `tdd_path_identity` and
  `same_repo_worktree_root` take the existing `_GitLookupFailed` fallback
  without calling `_git_identity`.

**Focused verification (planned):**
`DATABASE_URL=… GOBBY_TEST_PROTECT=1 uv run pytest tests/workflows/test_tdd_gate_worktree_paths.py tests/workflows/engine/test_condition_git_off_loop.py -q`.

**Acceptance:**

- 1.8.1 - Foreign-origin TDD path identity uses the string fallback, and no git subprocess runs. test: `tests/workflows/test_tdd_gate_worktree_paths.py::test_foreign_origin_skips_git_identity`.

### 1.9 Foreign-origin found-work terminal evidence fails closed [category: code] (depends: 1.3)
`kind: deliverable`

Targets:
- `src/gobby/workflows/found_work_gate.py::FoundWorkStopAnalyzer._terminal_failures`
- `src/gobby/workflows/found_work_gate.py::_project_verification_commands`
- `src/gobby/workflows/found_work_gate.py::_project_command_prefix`
- `src/gobby/workflows/found_work_project_commands.py`
- `tests/workflows/test_found_work_gate.py::*` — scope-reason: adds the foreign-origin terminal-evidence test and the local regression test

**Research context:**
- `src/gobby/workflows/found_work_gate.py` (877 lines):
  - `FoundWorkStopAnalyzer.analyze` calls `_terminal_failures` only when the
    turn has no task disposition (no claimed task and no labeled deferral).
    It caches the result in `_found_work_terminal_validation_failures`.
  - `_terminal_failures` returns `()` when `not project_path`, so it fails
    open.
  - Otherwise it loads the session and the stop validation config, then calls
    `src/gobby/tasks/transcript_evidence.py::derive_transcript_evidence`. That
    function first calls `src/gobby/sessions/machine_scope.py::require_local_session_ownership`,
    which raises `RemoteSessionOwnershipError` for a session owned by another
    machine. `_terminal_failures` catches `TranscriptEvidenceUnavailable` and
    every other exception and returns `()`, so a remote-owned session fails
    open too. Its `repo_path` parameter is a `str`, and a foreign event has
    `project_path=None`.
  - It clears failures through `green_covers_failure` /
    `surviving_path_failure` (`validation_cover.py`: `os.path.lexists` plus
    `git ls-tree HEAD`) and `_foreign_owned_dirty_paths` (git status).
  - `_project_verification_commands` and `_project_command_prefix` (lines
    834–877) read `.gobby/project.json`. They are only called inside this
    file.
- Node transcript ingestion is deferred (D1.3).
- The origin comes from `current_hook_delivery()` (1.3).

**Implementation:**
- `_terminal_failures`: for a foreign-origin delivery, return
  `(FOREIGN_ORIGIN_DIAGNOSTIC.format(machine=…, gate="the found-work validation gate"),)`
  before the `not project_path` early return.
  - Load no session, config or transcript, and call no
    `derive_transcript_evidence`. Never pass `None` as its `repo_path`.
  - Never turn `RemoteSessionOwnershipError` or
    `TranscriptEvidenceUnavailable` into an empty foreign-origin result.
  - Run no cover, survival or foreign-dirty clearance probe.
  - A turn with a task disposition never reaches this call, so it is
    unaffected.
- Move `_project_verification_commands` and `_project_command_prefix` out of
  `src/gobby/workflows/found_work_gate.py` into the new
  `src/gobby/workflows/found_work_project_commands.py`, imported back by
  `found_work_gate`. This split is the `production-size-growth` disposition.

**Focused verification (planned):**
`DATABASE_URL=… GOBBY_TEST_PROTECT=1 uv run pytest tests/workflows/test_found_work_gate.py -q`.

**Acceptance:**

- 1.9.1 - For a foreign-origin turn end with no task disposition, including a session owned by another machine with no local transcript evidence, the found-work gate reports the diagnostic as a terminal failure and blocks. No session load, transcript derivation, cover, `lexists`, `ls-tree` or status probe runs. test: `tests/workflows/test_found_work_gate.py::test_foreign_origin_terminal_evidence_fails_closed`.
- 1.9.2 - Local-origin terminal failures, covers and clearances are unchanged after the helper move. test: `tests/workflows/test_found_work_gate.py::test_local_terminal_failures_unchanged_by_move`.

### 1.10 Foreign-origin source reads are unverified [category: code] (depends: 1.3)
`kind: deliverable`

Targets:
- `src/gobby/hooks/code_navigation_recovery.py::_verified_source_line_count`
- `src/gobby/hooks/code_navigation_recovery.py::_ignore_decision`
- `tests/hooks/test_code_navigation_recovery.py::*` — scope-reason: adds the foreign-origin navigation test

**Research context:**
- `src/gobby/hooks/code_navigation_recovery.py`:
  - `_verified_source_line_count` reads file bytes. On `None` (unverified) the
    read counts as broad and gets the existing gcode redirect.
  - `_ignore_decision` reads `.git` and `.gobby/gcode.json`, and `_git_ignored`
    runs `git check-ignore`.
- The origin comes from `current_hook_delivery()` (1.3).

**Implementation:**
- For a foreign-origin delivery, `_verified_source_line_count` returns `None`,
  and `_ignore_decision` returns "not ignored" without disk or git.

**Focused verification (planned):**
`DATABASE_URL=… GOBBY_TEST_PROTECT=1 uv run pytest tests/hooks/test_code_navigation_recovery.py -q`.

**Acceptance:**

- 1.10.1 - A foreign-origin source read is unverified and redirected, and no file, `.git` or `gcode.json` is read. test: `tests/hooks/test_code_navigation_recovery.py::test_foreign_origin_read_is_unverified`.

### 1.11 Foreign-origin normalization skips checkout probes [category: code] (depends: 1.3)
`kind: deliverable`

Targets:
- `src/gobby/hooks/_path_scope.py::apply_path_scope_metadata`
- `src/gobby/hooks/_path_scope.py::current_project_root`
- `tests/hooks/test_path_scope.py::*` — scope-reason: adds the foreign-origin probe and project-root tests

Consumers unchanged:
- `src/gobby/hooks/_normalization_canonical.py` — no-edit-reason: It calls `apply_path_scope_metadata` with an unchanged signature; the origin comes from the delivery context.
- `src/gobby/workflows/condition_helpers_paths.py` — no-edit-reason: It calls `current_project_root` with an unchanged signature; site s3 degrades through it.

**Research context:**
- `src/gobby/hooks/_path_scope.py::apply_path_scope_metadata` runs during
  daemon normalization (`_normalization_canonical.py`), which the adapters
  call from `handle_native`, inside 1.3's delivery scope. It probes `.git`
  files and `commondir` (`_git_common_dir`, `checkout_root`) and walks up for
  `.gobby/project.json` (`find_project_root`) to set `canonical_repo_mutation`.
  When no root is found, paths count as in-project, which is fail-closed.
- `current_project_root` falls back to `find_project_root`. The seat write
  scope (`condition_helpers_paths.py::write_paths_within`) uses it.
- `canonical_repo_mutation` stays armed for a foreign write with or without
  this section: a hub path that does not exist finds no root, and one that
  does exist resolves in-project. The section removes the probes.

**Implementation:**
- For a foreign-origin delivery (`current_hook_delivery()`):
  - `apply_path_scope_metadata` skips every `.git`, `commondir` and
    project-root probe and takes the existing no-root branch.
  - `current_project_root` returns only an explicit `project_path` from event
    data, with no `find_project_root` walk.

**Focused verification (planned):**
`DATABASE_URL=… GOBBY_TEST_PROTECT=1 uv run pytest tests/hooks/test_path_scope.py tests/hooks/test_normalization.py -q`.

**Acceptance:**

- 1.11.1 - Foreign-origin normalization performs no `.git`, `commondir` or project-root probe, and a write stays `canonical_repo_mutation`. test: `tests/hooks/test_path_scope.py::test_foreign_origin_skips_checkout_probes`.
- 1.11.2 - For a foreign-origin delivery, `current_project_root` returns only an explicit event `project_path` and walks no directory, so a seat write with a relative allowed directory is outside scope. test: `tests/hooks/test_path_scope.py::test_foreign_origin_project_root_is_explicit_only`.

### 1.12 Foreign-origin post-tool edits record fact paths without git or index [category: code] (depends: 1.3)
`kind: deliverable`

Targets:
- `src/gobby/hooks/event_handlers/_tool.py::ToolEventHandlerMixin._record_successful_file_mutation`
- `src/gobby/hooks/event_handlers/_tool.py::ToolEventHandlerMixin._resolve_repo_edit_paths`
- `tests/hooks/test_tool_handlers.py::*` — scope-reason: adds the foreign-origin ledger test

**Research context:**
- `src/gobby/hooks/event_handlers/_tool.py`:
  - `_record_successful_file_mutation` records the edit ledger.
  - `_resolve_repo_edit_paths` (`find_project_root` plus `resolve`, falling
    back to cwd) makes paths repo-relative.
  - `_paths_landed_before_edit` runs git log via `paths_committed_after_async`.
  - `_notify_code_index` triggers gcode indexing on the evaluating machine.
- Facts (1.1), served through 1.3's `current_hook_delivery().facts` and
  `fact_for(raw_path)`, carry `relative_path` for guarded edits. The ledger
  stores repo-relative paths.

**Implementation:**
- For a foreign-origin delivery:
  - `_resolve_repo_edit_paths` maps each path to its fact's `relative_path`
    when carried. Otherwise it keeps the raw path as given, with no
    `find_project_root` or `resolve`.
  - `_record_successful_file_mutation` skips `_paths_landed_before_edit` and
    `_notify_code_index`.

**Focused verification (planned):**
`DATABASE_URL=… GOBBY_TEST_PROTECT=1 uv run pytest tests/hooks/test_tool_handlers.py -q`.

**Acceptance:**

- 1.12.1 - A foreign-origin edit records the ledger under the fact's `relative_path` and runs neither git log nor index notify. test: `tests/hooks/test_tool_handlers.py::test_foreign_mutation_records_fact_path_without_git_or_index`.

### 1.13 Foreign-origin run_command effects do not spawn [category: code] (depends: 1.3)
`kind: deliverable`

Targets:
- `src/gobby/workflows/engine/run_command_effects.py::RunCommandEffectsMixin._apply_run_command`
- `tests/workflows/test_run_command_effect.py::*` — scope-reason: adds the foreign-origin skip test

Consumers unchanged:
- `src/gobby/workflows/engine/effects.py` — no-edit-reason: It dispatches `_apply_run_command` with an unchanged signature.

**Research context:**
- `src/gobby/workflows/engine/run_command_effects.py::RunCommandEffectsMixin._apply_run_command`
  spawns `node hook.mjs` (impeccable) with `cwd` set to the event cwd. It is
  documented as fail-open.

**Implementation:**
- For a foreign-origin delivery, `_apply_run_command` returns without
  spawning and records an audit entry with reason `foreign-origin`.

**Focused verification (planned):**
`DATABASE_URL=… GOBBY_TEST_PROTECT=1 uv run pytest tests/workflows/test_run_command_effect.py -q`.

**Acceptance:**

- 1.13.1 - A foreign-origin `run_command` effect does not spawn and is audited as skipped. test: `tests/workflows/test_run_command_effect.py::test_foreign_origin_skips_run_command_spawn`.

### 1.14 Foreign-origin shell commands skip the rtk rewrite [category: code] (depends: 1.3)
`kind: deliverable`

Targets:
- `src/gobby/workflows/engine/proxy_hooks.py::ProxyHooksMixin._run_rtk_proxy`
- `tests/workflows/test_proxy_hooks.py::*` — scope-reason: adds the foreign-origin skip test

**Research context:**
- `src/gobby/workflows/engine/proxy_hooks.py::ProxyHooksMixin._run_rtk_proxy`
  checks `linked_worktree_root(cwd)` and spawns rtk in cwd.

**Implementation:**
- For a foreign-origin delivery, `_run_rtk_proxy` returns the command
  unrewritten.

**Focused verification (planned):**
`DATABASE_URL=… GOBBY_TEST_PROTECT=1 uv run pytest tests/workflows/test_proxy_hooks.py -q`.

**Acceptance:**

- 1.14.1 - A foreign-origin shell command is not rtk-rewritten, and no rtk process starts. test: `tests/workflows/test_proxy_hooks.py::test_foreign_origin_skips_rtk_rewrite`.

### 1.15 Foreign-origin hook ingress resolves no project from local disk [category: code] (depends: 1.3)
`kind: deliverable`

Targets:
- `src/gobby/hooks/project_context.py::ProjectIdResolver.resolve`
- `src/gobby/hooks/startup_claim_preflight.py::_resolve_or_register_session`
- `tests/hooks/test_project_checkout_ingress.py::*` — scope-reason: adds the foreign-origin ingress tests
- `tests/hooks/test_startup_claim_preflight.py::*` — scope-reason: adds the `machine_id_error` preflight test

Consumers unchanged:
- `src/gobby/hooks/hook_manager.py` — no-edit-reason: `_resolve_project_id` delegates to `ProjectIdResolver.resolve` with an unchanged signature, and `resolve_hook_project_context`, `_session_end`, `_agent` and `_tool` reach the guard through it.
- `src/gobby/hooks/project_checkout_ingress.py` — no-edit-reason: `register_cwd_marker_checkout` is unchanged; a foreign delivery no longer reaches it.
- `tests/hooks/test_hook_extracted_helpers.py` — no-edit-reason: It drives `ProjectIdResolver.resolve` outside a delivery scope, so local behavior is unchanged.
- `tests/hooks/test_hook_manager.py` — no-edit-reason: `TestHookCheckoutIngress` uses local events, whose registration is unchanged.
- `tests/integration/test_project_checkout_identity.py` — no-edit-reason: Local overlay resolution through `ProjectIdResolver.resolve` is unchanged.
- `tests/storage/test_project_repo_path_isolation.py` — no-edit-reason: It calls `ensure_project_in_db` directly, which is unchanged.
- `src/gobby/hooks/session_lookup.py` — no-edit-reason: It passes the `resolve_project_id` callback to `resolve_hook_project_context` with an unchanged signature.
- `tests/hooks/test_hooks_manager.py` — no-edit-reason: It drives `HookManager._resolve_project_id` outside a delivery scope, so local resolution and the no-marker `ValueError` are unchanged.
- `tests/hooks/test_session_activation_reconciliation.py` — no-edit-reason: It patches `resolve_hook_project_context` by name, which is unchanged.

**Research context:**
- `src/gobby/hooks/project_context.py::resolve_hook_project_context` tries an
  explicit project id, then session and existing-session lookups, all
  database-only. It then calls `resolve_project_id(None, cwd)`.
- `ProjectIdResolver.resolve(None, cwd)` calls `get_project_context(cwd)`,
  which walks up from `cwd` for `.gobby/project.json` on the evaluator's
  disk. It then calls `ensure_project_in_db`, which delegates to
  `src/gobby/hooks/project_checkout_ingress.py::register_cwd_marker_checkout`.
  That validates a local checkout, links Cargo targets, registers the
  checkout for this machine and can rewrite the marker (`_refresh_stale_marker`).
  A foreign event whose cwd also exists on the hub therefore reads, and can
  mutate, the hub's checkout.
- Other hook callers reach the same method through
  `HookManager._resolve_project_id`: `_session_end.py`, `_agent.py` and
  `_tool.py` (`_notify_code_index` is skipped by 1.12).
- With no marker found, `resolve` raises `ValueError("No .gobby/project.json found …")`.
  The route catches it (`src/gobby/servers/routes/mcp/hooks.py`), and
  `hook_responses.py::_hook_exception_response` blocks critical hooks and
  degrades the rest.
- A skipped resolution is not a safe alternative. `hook_manager.py` returns
  `HookResponse(decision="allow")` for one before any rule runs, git gates
  included.
- ghook's `X-Gobby-Project-Id` header does not reach the hook thread:
  `adapter_execution.py::run_adapter_hook` submits `run_adapter` to
  `_HOOK_ADAPTER_EXECUTOR` without copying context, and `get_project_context(cwd)`
  ignores the middleware context var when a cwd is given. Mapping the header
  to a foreign project would need new plumbing (rejected; D1.4).
- `src/gobby/hooks/startup_claim_preflight.py::_resolve_or_register_session`
  runs in the route before `run_adapter_hook`, so outside the delivery scope.
  A foreign `machine_id` is already rejected (`MachineOwnershipMismatchError`).
  A `machine_id_error` envelope has no `machine_id`, so `require_local_machine_id`
  resolves the local machine, and `_resolve_project_id` calls the resolver,
  then `get_project_context(Path(workspace))`.

**Implementation:**
- `ProjectIdResolver.resolve`: after the explicit `project_id` return and the
  no-`cwd` personal fallback, for a foreign-origin delivery
  (`current_hook_delivery()`), raise `ValueError` with
  `FOREIGN_ORIGIN_DIAGNOSTIC.format(machine=…, gate="hook project resolution")`.
  - No `get_project_context`, `ensure_project_in_db`, checkout validation,
    Cargo target linking or marker refresh runs.
  - Explicit, session and existing-session resolutions are unchanged.
  - Outside a delivery scope the default delivery is local, so non-hook
    callers are unchanged.
- `_resolve_or_register_session`: when `_payload_field(payload, "machine_id_error")`
  is present, log the existing foreign-machine warning and return `None`
  before `require_local_machine_id`. No lease is taken, and the hook
  continues to the adapter, where the resolver guard applies.

**Focused verification (planned):**
`DATABASE_URL=… GOBBY_TEST_PROTECT=1 uv run pytest tests/hooks/test_project_checkout_ingress.py tests/hooks/test_startup_claim_preflight.py tests/hooks/test_hook_extracted_helpers.py tests/hooks/test_hook_manager.py -q`.

**Acceptance:**

- 1.15.1 - A foreign-origin hook whose cwd also exists on this machine, with no explicit or session project, raises the foreign-origin diagnostic. Spies on `get_project_context`, checkout validation, Cargo target linking, `register` and marker refresh are never called. Foreign explicit and session resolutions, and local cwd-marker ingress, are unchanged. test: `tests/hooks/test_project_checkout_ingress.py::test_foreign_origin_resolves_no_project_from_local_disk`.
- 1.15.2 - An AGY pre-invocation envelope with `machine_id_error` takes no startup-claim lease and reads no project marker. test: `tests/hooks/test_startup_claim_preflight.py::test_machine_id_error_takes_no_lease_and_reads_no_marker`.

## D1 Node git-state and disk gates run where the checkout lives (depends: 1.6, 1.7, 1.8, 1.9, 1.10, 1.11, 1.12, 1.13, 1.14, 1.15)
`kind: deferred`

Under scope A, every git-state or disk gate fails closed or degrades for a
foreign-origin event (sites 2, 4–10, 13–15, s1, s3 and s5). Node sessions
therefore cannot commit, close a task with edits, or end a turn that holds
guarded ledger paths or lacks a task disposition. A node hook that carries no
explicit project id cannot resolve its project (site 14), so its session never
registers and its critical hooks block. These gates need to be evaluated
against the node's own checkout. That is the node-local hook ingress and
envelope ledger of S2.11 (#21569). #23274 (Node channel, relay backend, and
`/api/machines`) owns the relay and channel, and has no gate item.

Obligations:
- D1.1: each gate in §Site Disposition gives a real answer for a node event.
- D1.2: the ledger reconciles against the node's git.
- D1.3: Codex transcript and session ingestion work for node sessions.
- D1.4: a node hook resolves its project from the node's own checkout
  identity (for example ghook's `X-Gobby-Project-Id`, validated by the hub),
  so node sessions register without the hub reading its own disk.

*Candidate fourth item for Choice 5's required set (D3, D4 and D5 before
nodes count as supported). Josh confirms it when S2.11 is planned.*

```yaml
deferral:
  task_ref: "TBD-at-expansion"
  reason: "Node git-state and disk gates need the node-local hook ingress designed in S2.11 (#21569); this plan fails them closed for foreign-origin events."
  owner: "gobby-1.0 stage 2 (S2.11, #21569)"
  original_acceptance_items:
    - D1.1
    - D1.2
    - D1.3
    - D1.4
```

## D2 Origin binds to the authenticated machine identity (depends: 1.3)
`kind: deferred`

Origin classification (Decision 7) trusts the self-reported
`input_data.machine_id`. The shared-token cutover, #23273 (Hub-side key
validation, front-door identity, and shared-token cutover; api-keys D1),
introduces key-bound machine identity at the front door. Once it lands:
- The delivery origin comes from the authenticated key's machine.
- A payload `machine_id` that disagrees with that identity fails closed.

This is the only real D1 dependency this plan keeps (§D1.7 Mapping).

```yaml
deferral:
  task_ref: "TBD-at-expansion"
  reason: "Trusted origin needs the front-door key identity that #23273 delivers."
  owner: "gobby-1.0 stage 1 (S1.4, #21555)"
  original_acceptance_items:
    - D2.1
```

D2.1: the origin is derived from the authenticated key's machine, and a
conflicting payload `machine_id` blocks with a diagnostic.

## D3 Node-to-hub end-to-end verification of `file_facts` (depends: 1.6, 1.7, 1.8, 1.9, 1.10, 1.11, 1.12, 1.13, 1.14, 1.15)
`kind: deferred`

Once #23274 relays a node's hooks to the hub, an e2e test on the
`tls=self-signed` hub-node pair proves the full path:
- A node Edit crossing 1,000 lines is blocked by the hub's monolith rule.
- A node Rust test-only edit opens the TDD gate.
- A node `git commit` fails closed with the diagnostic.
- The hub reads none of its own checkout.

```yaml
deferral:
  task_ref: "TBD-at-expansion"
  reason: "No node-to-hub hook path exists until the relay (#23274) lands."
  owner: "gobby-1.0 stage 1 (S1.4, #21555)"
  original_acceptance_items:
    - D3.1
```

D3.1: the hub-node e2e test above passes over the relay.

## V1 Plan Changelog
`kind: verification`

- 2026-10-04: First draft by L4b (gobby#15047) under the PD rulings of
  2026-10-04 19:44 (scope A, git gates fail closed, envelope shape approved,
  D1.7 mapped, and node gates deferred to S2.11). The 13-site inventory, the
  persistence sweep and the corpus check were verified read-only on `0.5.0`
  at `ae78ebdf00`. The draft is narrative only, with no M1.

## V2: Verification
`kind: verification`

After every leaf has landed:

1. Run the focused suites of 1.3 to 1.15 together against the test hub.
2. Run `cargo nextest run -p ghook` (heavy key).
3. Rerun both corpus suites unchanged, as the D1.7 evidence:
   - `DATABASE_URL=… GOBBY_TEST_PROTECT=1 uv run pytest tests/contracts/test_http_corpus.py -q`;
   - `cargo test -p gdaemon --test http_contracts` (heavy key).
4. After the PD's release activation, a local session's Edit on a guarded file
   carries `file_facts` in its inbox envelope (inspected through the test
   suite's spool fixture, never the live inbox), and the monolith rule still
   blocks a 1,000-line crossing.
5. The inbox directory is mode 0700 after the first post-activation hook.
