Plan artifact: `.gobby/plans/sessions-family.md`

# Gobby 1.0 Stage 2: native Sessions and transcripts family

**Plan ID:** sessions-family

## Overview
`kind: framing`

Port the Sessions and transcripts family (#21562, S2.5) from Python ownership to
native Rust services. Planning task #25253 owns this narrative. The bounded
inventory is every parity-ledger row whose status note cites #21562:
**63 rows: 11 CLI, 30 HTTP, 21 MCP, 1 WS**. The sources are
`docs/contracts/parity-ledger-status.d/sessions-agents.json` (61 rows),
`docs/contracts/parity-ledger-status.d/websocket-chat-voice-ui.json`
(`WS message stop_request`) and
`docs/contracts/parity-ledger-status.d/comms-cron-hub-nodes.json`
(`MCP gobby-hub:list_cross_project_sessions`). The rendered
`docs/contracts/parity-ledger.md` is generated. This plan does not expand work,
dispatch leaves or authorize a production restart.

The work is necessary: all 63 rows are proxied today and gdaemon has no session
implementation. Restraint stops at the existing seams: one `gobby-sessions`
library crate at `crates/gsessions`, injected into gdaemon's family vector the
way `RuntimeFamily` is; the shared gcore PostgreSQL pool; the existing
hub-to-node command channel; a background task inside the lease-scoped
`ActiveServices`, as token-event retention runs; and `gclient` command mode for
the CLI. No new
process, database, schema migration, queue or executable is added.

Outcome when every leaf closes. Every Sessions surface ends fully in Rust and
its Python implementation is deleted (Decision Record item 15). A deleted
Python surface is recorded as `dropped by decision #25253` with a note citing
its native evidence: `scripts/generate_parity_ledger.py` inventories live
Python registrations and keeps only dropped rows for absent surfaces
(Orchestrator ruling 2026-10-28, as the projects plan records removed
routes).

- 29 HTTP rows are served natively and their Python routes are deleted. 25
  switch at 7.2 and 7.3 deletes their Python routes; `GET /api/sessions`
  (ACP enrichment and resumability, 8.5) and the two ACP rows (8.4) switch
  and delete in their P8 leaves. `POST /api/sessions/{session_id}/release-composer`
  stays `proxied` with a #25276 owner note (Decision 6).
- 8 CLI verbs are served by `gclient sessions <verb>`, and 7.3 deletes their
  Python commands. `renumber` and `backfill-context-windows` are dropped with
  no replacement. The `gobby sessions` group keeps only #21565's
  `terminate-terminal`, so its row stays `proxied` with a #21565 owner note.
- 20 MCP rows get native handlers, descriptors and proven schema/result parity.
  #21570 activates them; 8.8 then deletes their Python tools.
  `gobby-sessions:release_composer` stays `proxied` with a #25276 owner note.
- `WS message stop_request` is served natively and its Python handler deleted
  by 8.7.
- One native extension, `DELETE /api/sessions/{session_id}`, serves
  `gclient sessions delete` under a waiver.

Native construction is passive in production until 7.2 switches the family.
Proxy and Compare remain rollback selections only until 7.3 deletes the Python
routes and loops; after that, rollback means the prior coherent release.
Gated surfaces follow in P8 without holding the family's activation.

## Decision Record
`kind: framing`

All rulings below came from the Orchestrator (gobby#17281) through Triage
(gobby#17480) on 2026-10-28.

1. **Resumability (S1).** Resumability is computed on the hub from five
   predicates, as `src/gobby/servers/routes/sessions/core.py::_compute_resumability`
   does: the caller's own session, sessions with an active agent run, sessions
   with an active pipeline execution, sessions active in web chat, and web-chat
   rows whose sandbox policy no longer matches. The owners answer through
   Decision 7's ports: agent runs through gagents, pipeline executions through
   #21567, and the last two through #21592's native web-chat registry. No
   transcript file is read. The ROADMAP "Ports and the proxied surface" note
   that lists `include_resumability` as a deferred boundary is corrected in
   7.4. The Orchestrator corrects the epic text; this plan names no task edit.
2. **Machine ownership (S2).** Hub rows are authoritative. The hub resolves a
   session's owning machine. A session owned by the serving machine is read
   in-process; a remote owner is reached through typed node commands in the next
   node-commands version after #21566's (Orchestrator: #25207 is version 5 and
   #21566 is version 6). This family owns those additions in one leaf, 3.8,
   ordered after `external-mcp-transport-multiplexer 3.1`
   (`.gobby/plans/external-mcp-transport-multiplexer.md` at `87d4d08d09`). An
   offline or unknown owner fails explicitly; no read falls back to the hub
   filesystem. The exact-machine check of
   `src/gobby/sessions/machine_scope.py::require_local_session_ownership` is kept
   as the routing test: where Python refuses a remote owner, the native
   service routes the request to it (Debt B9).
3. **CLI host (C1).** The 9 kept CLI rows ship as `gclient sessions <verb>` over
   the hub's public HTTP API, one path for hub, standalone and node. The native
   `DELETE /api/sessions/{session_id}` serves `delete`. 7.3 deletes the Python
   verbs; no Python forwarding layer survives (item 15), so their ledger rows
   become `dropped by decision #25253` with `gclient` evidence notes. The
   `gobby sessions` group keeps only `terminate-terminal`, whose row #21565
   owns; #21565's plan removes the group with that verb, and the S3.2 naming
   switch makes `gobby` the native binary. `summarize` needs notes, a code-only fallback and a
   file-only output that the generate-summary route lacks, so the native
   route accepts an optional request body for them (4.1, waiver
   `sessions.http-generate-summary.cli-request-options`, documented in
   `docs/contracts/sessions-native.md` by 7.4). The CLI leaves cite `node-mode 2.1` (operator HTTP helper) and
   `node-mode 2.7` (CLI inventory) in `.gobby/plans/node-mode.md` at
   `cbafbe253f` as cross-plan edges; the Orchestrator converts them to task
   edges at expansion.
4. **set_handoff (S4).** Python's tool only validates and stages; the hook
   path delivers. set_handoff is ported in 8.1 (validation and staging), 8.2
   (compact delivery and continuation) and 8.3 (the terminal clear path), all
   gated on #21565 (terminal runtime). 8.1 re-homes
   `src/gobby/agents/agent_workspaces.py::workspace_handoff_references` beside
   staging: it reads only the session variable `_agent_workspace_dir`, and
   set_handoff is its only production caller, so set_handoff carries no
   #21564 gate (Orchestrator ruling ORCH-REV-AF-5, recorded in
   `.gobby/plans/agents-family.md`). Web-chat delivery and the tool definition are 8.6, gated on
   #21592. The delivery modules that `src/gobby/hooks/terminal_handoff_delivery.py`
   imports (`_terminal_compaction.py`, `_terminal_handoff_delivery.py`, the
   delivery half of `_terminal_clear.py`, `src/gobby/sessions/compact_continuation.py`
   and `src/gobby/sessions/codex_compact_watch.py`) are Sessions services. The
   hooks plan's 5.10 and 7.2 (`.gobby/plans/s2-11-hub-hooks-rules.md`) call
   them through the Sessions seam; the Orchestrator accepted this ownership on
   2026-10-28.
5. **Background jobs (S5, C2.1, C3).** Sessions ports exactly two Python loops:
   stale-session expiry and transcript processing. The other sweeps in
   `src/gobby/sessions/lifecycle.py::SessionLifecycleManager` (soft-deleted
   definition purge, dream-hidden memory purge, the knowledge-graph queue,
   prompt-file cleanup) stay in Python with their owners. Processing is
   hub-orchestrated with a node-local parse (C3 option c): the hub's
   lease-scoped job selects pending sessions; a local session is parsed
   in-process; a remote owner receives `transcript.process` and replies with
   paged stats and token events. The hub writes the rows, broadcasts token
   events and generates summaries through #25097. A native node holds no
   database credential (node-mode Overview), so no node writes rows and no new
   node-to-hub route exists. Ownership switches once at the restart boundary
   from the bootstrap `front_door.routes.sessions` selection (Cron 4.1
   precedent): Native starts the native jobs and Python skips the two loops;
   Proxy and Compare keep Python as the owner until 7.3 deletes the Python
   Sessions loops. Expiry is hub-only, so the
   hub's selection decides it. Three expiry pieces write tables owned by
   epics that #21562 blocks: `src/gobby/storage/hook_receipts.py::retire_expired_session_hook_effects`
   (#24151), `src/gobby/storage/session_lifecycle.py::prune_stale_compact_workflow_instances`
   (#21567) and the `pending_interactions` update inside
   `session_lifecycle.py::cleanup_expired_session_state` (#21592). They are
   those families' pieces: they stay in Python's shared sweep in every mode
   (Orchestrator ruling 2026-10-28), so the switch-off is per step, and 7.3
   leaves them running. The hooks plan
   (`.gobby/plans/s2-11-hub-hooks-rules.md`, #24151), the Workflows, rules
   and pipelines plan (#25279 under #21567) and #21592 each carry an
   explicit edge naming its piece for its own Rust port. Processing follows each machine's own
   selection for the transcripts it holds: a node whose selection is not
   Native refuses `transcript.process`, so its Python runner stays the single
   owner of those sessions.
6. **Gated surfaces (C2.2).** ACP close/delete (8.4), web-chat resumability
   (8.5), web-chat handoff delivery (8.6) and `WS message stop_request` (8.7)
   are small leaves gated on #21592. Web-chat session creation is native here
   (2.1). These leaves run after activation (P8) and each claims only its own
   routes, so activation waits on none of those epics. `GET /api/sessions`
   is claimed only from 8.5: every listed ACP web-chat row carries an `acp`
   block that `src/gobby/sessions/acp_lifecycle.py::attach_acp_block` builds
   from the live web-chat runtime (`acp_backends`, `acp_session_capabilities`,
   `get_acp_session_info`), which #21592 owns, and resumability needs the same
   registry. 1.1 builds the list service, which the MCP `list_sessions` tool
   uses without that block.
   Composer release is not ported here: #24571 keeps the composer ledger in
   the Python write client (Orchestrator ruling 2026-10-15, recorded in
   `.gobby/plans/daemon-side-gterm-adoption-and-terminal-ws.md`), so
   `POST /api/sessions/{session_id}/release-composer` and
   `gobby-sessions:release_composer` stay `proxied`. Their owner note cites
   #25276, the native composer ledger plan the Orchestrator filed on
   2026-10-28.
7. **Cross-family data (C2.3, ROADMAP decision 16).** Orchestrator ruling
   2026-10-28, no exceptions: Sessions reads or writes another family's rows
   only through that family's crate API. Sessions SQL touches only Sessions
   tables. An owner returns an id set or an id-keyed map, and Sessions filters
   its own rows with `= ANY(...)`, so keyset pages keep working. Owner
   operations that join a Sessions write take the caller's transaction, so
   the effect stays atomic as Python's single transaction is. Two mechanisms:
   - **Direct dependency** for owners that precede Sessions and do not depend
     on it. Tasks: `gobby-tasks` (`.gobby/plans/tasks-family.md`; #21560
     blocks #21562). Projects: `gobby-projects`
     (`.gobby/plans/gdaemon-projects-files-docs-family.md` 1.2). Terminal rows:
     `gobby-terminals`. Prompts: `crates/gruntime/src/prompts.rs`. The AI
     generation service: `gobby-ai` (#25097). Where the owner crate lacks an
     operation, the first Sessions leaf that needs it adds one narrow public
     function to that crate, reading or writing only the owner's tables.
     That leaf names the function. This follows the Memory plan's re-export
     in `crates/gai/src/lib.rs` (`.gobby/plans/memory-family.md` Decision 6).
   - **Port traits** for owners that depend on Sessions or that #21562
     blocks: agent runs and worktrees (gagents depends on gsessions,
     `.gobby/plans/agents-family.md`), memories (gmemory depends on 2.4 per
     Decision 10), session skill loads (gskills,
     `.gobby/plans/gdaemon-skills-family.md` 1.6, writes session state),
     Workflows' audit log and agent step instances, pipeline executions
     (all #21567) and the web-chat registry (#21592). `crates/gsessions/src/ports.rs` declares each trait; gdaemon
     binds an adapter that calls the owner crate's public functions. The
     Tasks plan's `AssignmentNotifier` port is the precedent. Before an
     adapter lands, tests bind fixture adapters and production stays passive.
     The gagents port mirrors the three functions `agents-family 3.6`
     exports (ORCH-REV-AF-7): `run_rows(tx, selector)`,
     `move_run_lineage(tx, session_ids, successor_id)` and
     `containing_worktree_id(tx, project_id, workspace_path)`. 7.2 binds the
     gagents, gmemory and gskills adapters. It depends on `agents-family 3.6`
     and `gdaemon-skills-family 1.6` as cross-plan edges the Orchestrator
     converts at expansion, and on `memory-family 1.1`, which creates
     gmemory. Where an owner crate lacks a function an adapter needs, 7.2
     adds one narrow function there. Two Workflows reads are needed before
     any Workflows crate exists, because #21567 waits on #21562: 2.2's
     retained-reference check over `workflow_audit_log` and 2.4's
     step-scoped variables over `agent_step_instances`. For those, 7.2 binds
     transitional Rust adapters in gdaemon that run only those statements;
     gsessions never reads the tables. #25279's crate exports the functions,
     replaces the adapter bodies and deletes the adapters (Orchestrator
     ruling A, 2026-10-28). The pipeline and web-chat adapters land in
     their P8 leaves.
   Each consuming leaf names its foreign operations.
8. **Ownership boundaries (S6).** Sessions owns session variables, dirty-path
   grants and claim holds. `SessionVariableMutation` takes its explicit
   advisory-lock key from #25262.
9. **Browser admission and security review (S7).** 7.1 registers the Sessions
   HTTP rows for `Caller::Browser` through #24967's shared verifier and #24964's
   CORS/preflight boundary. The Orchestrator approved `security-review` on 7.1
   and on 2.5, which ports the two operator-only routes. The Orchestrator
   applies the label after expansion and verifies it before dispatch; cookie
   activation and operator-route activation wait for a Security Reviewer's
   approval of the exact implementation SHA.
10. **Public library API.** The crate exports the services other families
    consume: session registration and creation for Cron 2.4's cron parent
    sessions and the hooks family's node session registration
    (`.gobby/plans/s2-11-hub-hooks-rules.md` item 8). For the agents family
    (`.gobby/plans/agents-family.md` Decision 16, ORCH-REV-AF-3), 1.1
    exports `session_rows(tx, selector, lock)` and 2.1's `create_session`
    takes the child-session fields (parent session, agent run, depth and a
    pre-created external id); its ref resolution, lookup, status, delete
    and variable reads come from 1.1, 2.2 and 2.4. The rule-engine seam
    adapters (`.gobby/plans/rust-rule-evaluation.md` D1.2, which #21569
    composes) use key-scoped variable mutation, session lookup, stop signals
    and bounded transcript reads. That key-scoped writer is general: 2.4's
    bounded-list upsert and value set, under #25262's `SessionVariableMutation`
    key at priority 950, absorb the narrow writer that
    `.gobby/plans/memory-family.md` 6.1 planned in
    `crates/gmemory/src/session_vars.rs` (Orchestrator ruling 2026-10-28).
    Memory 6.1 depends on 2.4 as a cross-plan edge that the Orchestrator
    converts at expansion. The Tasks plan's 3.5 claim keys stay written by
    gtasks in the task row's transaction under the same lock. For the hooks
    plan's seam group (ruling C5 (a)),
    2.9 exports the hook-path row services and clear-run lineage and 4.3 exports
    live session message processing; `s2-11-hub-hooks-rules 7.1`
    (`.gobby/plans/s2-11-hub-hooks-rules.md`) composes both at the hooks
    flip, and receipts and staged effects stay with that plan's 3.4.
    Transports call these services; no transport owns business logic.
    MCP tools are exported as `gobby_core::mcp::ToolDefinition` lists, one for
    `gobby-sessions` and one for `gobby-hub`. `crates/gcore/src/mcp.rs::ToolFamily`
    yields one `ToolServer` and `ToolRegistry::new` rejects a second family
    for the same server (`RegistryError::DuplicateServer`), while both
    servers also carry other epics' tools (`gobby-sessions:send_keys`,
    `capture_output` and `terminate_terminal` belong to #21565, and
    `capture_baseline_dirty_files` to #23563). #21570 composes each server
    from its owners' lists when it activates the MCP rows.
11. **Session continuity writes.** `.gobby/plans/gobby-messaging.md` D4 assigned
    channel reads in compact continuation to #21562, but that plan (#22340) was
    folded into the unplanned #24173. 8.2 writes the continuation pull prompt
    through today's `inter_session_messages` contract, as
    `src/gobby/sessions/compact_continuation.py::persist_pull_prompt_message`
    does. The plan that replaces that table owns the migration of this writer.
    #24173's criterion 8 owns the native mailbox and the Tasks plan's
    `AssignmentNotifier` adapter (`.gobby/plans/tasks-family.md` Q11), so no
    Sessions leaf ports the mailbox.
12. **Native DELETE extension.** `DELETE /api/sessions/{session_id}` is a public
    native route with no Python counterpart. It gets waiver
    `sessions.http-delete.native-extension` and documentation in
    `docs/contracts/sessions-native.md`; it adds no ledger key, because the
    ledger inventories Python surfaces and this route replaces the Python CLI's
    direct database delete behind the existing `CLI gobby sessions delete` row.
13. **Dependencies.** Use installed workspace crates (Tokio, axum, serde,
    serde_json, the gcore pool). Gzip archives use `flate2` 1.1.9, already in
    `Cargo.lock` through `png` and `ureq` with no direct workspace user; 3.1 adds
    it as a direct `gobby-sessions` dependency without a new lock entry. No new
    external crate is authorized; a leaf that finds one necessary escalates
    through Triage before adding it.
14. **Epic edges.** #24151, #21567, #21583 and #25021 wait on #21562 today.
    At expansion the Orchestrator re-points those edges to leaf 7.4, so they
    wait on activation and the published ledger, never on the P8 leaves
    that #21592, #21565 and #21567 gate (Orchestrator ruling 2026-10-28).
    This plan names no task edit.
15. **No Python (operator ruling via the Orchestrator, 2026-10-28).** The
    port ends with every Sessions surface fully in Rust: no Python policy
    layer, loopback client, rollback route or direct-storage writer stays in
    a Sessions surface's path. Deletions:
    - 7.3 deletes the Python CLI verbs and dropped commands, the Python
      routes of the 25 rows that switch at 7.2, and Python's Sessions-owned
      expiry steps and transcript-processing loop with their storage
      functions.
    - 8.4, 8.5 and 8.7 each delete the Python route or handler they replace.
    - 8.8 deletes the Python MCP tools, set_handoff and the Python delivery
      and continuation modules once #21570 serves the tools and the hooks
      plan's native path calls the native delivery services.

    Pieces that belong to other families stay with them, each through an
    explicit edge naming it in the owning Rust plan:
    - `gobby sessions terminate-terminal` (#21565);
    - both release-composer rows (#25276);
    - `retire_expired_session_hook_effects` (the hooks plan, #24151);
    - `prune_stale_compact_workflow_instances` (#25279);
    - the `pending_interactions` update of `cleanup_expired_session_state`
      (#21592);
    - the transitional `workflow_audit_log` and `agent_step_instances`
      adapters that 7.2 binds (#25279 replaces and deletes them);
    - the wake-replay registrant of 2.2's status-notification bridge
      (`.gobby/plans/agents-family.md` D1.3);
    - the gagents port functions (`agents-family 3.6`).

    The Orchestrator converts these at expansion. Other families' Python
    modules that import the Python Sessions storage layer keep it until
    their own ports; 7.3 names each importing family's removal owner.

## Debt not to port
`kind: framing`

Debt-first rule (memory 9066f1eb): each Python accident below is either dropped
or corrected through an exact-surface #24963 waiver in
`docs/contracts/parity-waivers.json`, with native response/effect assertions.
No whole surface is masked or skipped.

| ID | Python behavior | Disposition | Waiver / owner |
| --- | --- | --- | --- |
| B1 | `gobby sessions renumber` rewrites durable `#N` session refs (`src/gobby/storage/sessions/_renumber.py::_RenumberMixin.renumber_project_sessions`). | Dropped: the CLI command, storage mixin and tests are deleted. | `dropped by decision #25253`; 7.3 |
| B2 | `gobby sessions backfill-context-windows` is a finished one-off repair (`src/gobby/sessions/context_usage.py::backfill_session_context_windows`). | Dropped with no data migration. | `dropped by decision #25253`; 7.3 |
| B3 | `GET /api/sessions?include_resumability=true` over-fetches `limit * 3`, filters in Python and disables the cursor. | Exact page with a working cursor. | `sessions.http-list.resumability-exact-page`; 8.5 |
| B4 | The stop routes use the raw path id: no `#N`/prefix resolution, and an unknown session reaches a foreign-key failure. | Resolve refs; unknown session returns 404. | `sessions.http-stop-post.ref-resolution`, `sessions.http-stop-get.ref-resolution`, `sessions.http-stop-delete.ref-resolution`; 2.3 |
| B5 | `get_session_commits` and the detail view's commit count compute session time bounds differently (naive versus offset timestamps). | One timeframe contract with offset ISO bounds, one implementation on the owning machine. | `sessions.mcp-get_session_commits.unified-time-bounds`, `sessions.http-get.unified-time-bounds`; 1.3 |
| B6 | `POST /api/sessions/bulk-move` reports per-row failures as untyped strings. | Typed per-row errors through #24970's classes. | `sessions.http-bulk-move.typed-row-errors`; 2.2 |
| B7 | `POST /api/sessions/update_status` writes an invalid transition without a locked check. | Locked validation; invalid transition returns 400. | `sessions.http-update-status.invalid-transition`; 2.2 |
| B8 | Broad catches in four MCP tools return raw exception text: `src/gobby/mcp_proxy/tools/sessions/_commits.py::mark_loop_complete`, `_messages.py::get_session_messages`, `_messages.py::search_session_messages` and `_registration.py::register_session`. | Unexpected errors return `Internal server error` with diagnostics logged privately; deliberate errors keep their text. `query_readonly` keeps its deliberate PostgreSQL error text. | `sessions.mcp-mark_loop_complete.internal-error-redaction`, `sessions.mcp-get_session_messages.internal-error-redaction`, `sessions.mcp-search_session_messages.internal-error-redaction`, `sessions.mcp-register_session.internal-error-redaction`; 5.1 |
| B9 | `src/gobby/sessions/machine_scope.py::require_local_session_ownership` refuses a session owned by another machine: 403 on generate-summary, an error envelope from the two MCP tools, exit status 1 from the CLI, and an unhandled 500 on the messages route. | The owning machine serves the request through Decision 2's node commands; an offline or unknown owner fails explicitly. | `sessions.http-generate-summary.remote-owner-served` (4.1), `sessions.mcp-get_session_commits.remote-owner-served` (1.3), `sessions.http-messages.remote-owner-served` (3.10), `sessions.mcp-get_session_messages.remote-owner-served` (3.7), `sessions.cli-summarize.remote-owner-served` (6.1) |
| B10 | `src/gobby/servers/routes/sessions/core.py::_compute_resumability` logs a failed agent-run or pipeline lookup at debug level and reports the session resumable. | The owner error propagates as the shared internal error. | `sessions.http-list.resumability-storage-errors` (fault vector only); 8.5 |
| B11 | `src/gobby/servers/websocket/handlers/core.py::_handle_stop_request` uses the raw `session_id` with no `#N`/prefix resolution and returns raw exception text. | Resolve refs; an unknown session returns a typed not-found error; unexpected errors return `Internal server error`. | `sessions.ws-stop_request.ref-resolution`; 8.7 |
| W1 | Two transcript-status implementations: the HTTP fallback shape and the MCP archive-only shape. | One status service; each transport keeps its shape. | No contract change; 3.6 |
| W2 | `src/gobby/autonomous/stop_registry.py::StopRegistry.acknowledge` and `StopRegistry.cleanup_stale` have no production caller. | Not ported. | No contract change; 2.3 |
| W3 | Session statistics swallow storage errors and report zero counts. | Unexpected storage errors propagate as the shared internal error. | `sessions.http-get.stats-storage-errors` (fault vector only); 1.3 |
| W4 | Python CLI verbs open the database directly (`delete_session` uses `SessionManager`). | Not ported: `gclient` uses the public HTTP API. | No contract change; 6.1 |
| W5 | Synchronous storage calls inside async handlers. | Not applicable to native async code. | None |
| W6 | `src/gobby/mcp_proxy/tools/sessions/_terminal_clear.py::execute_clear_session` has no production caller; delivery runs through `deliver_staged_clear_session`. | Not ported. | No contract change; 8.3 |
| W7 | `src/gobby/storage/session_lifecycle.py::_EMPTY_SESSION_PRUNE_REFERENCE_COLUMNS` names `pending_approvals`, a table the schema lacks, so the retained-reference guard skips it. | Not ported (Orchestrator ruling 2026-10-28). | No contract change; 2.2 |

KEEP (ported exactly): MCP HTTP-200 error envelopes; exact machine ownership;
rendered messages answer 413 when too large while the raw transcript streams;
variable read-modify-write applies installed defaults first (memory 38321642);
every set_handoff gate. Transcript parsing never runs on the request executor
(memory 58242391: transcripts reach 86 MB).

## Constraints
`kind: framing`

- Observed research baseline: HEAD `e9ce2f79c7`. Existing files and symbols
  below were inspected with `gcode outline`, `gcode symbol-at`,
  `gcode search-symbol` and bounded `gcode grep` searches. Every path under
  `crates/gsessions` and every test symbol in those files is new.
- macOS and Linux only. No latency gates; correctness, bounds and cancellation
  are the gates.
- Storage reuses the existing session, token-event, stop-signal, variable,
  dirty-edit-grant, claim-hold and message tables and the
  `gobby_query_readonly` role (migration 482). No schema change; therefore no
  derived-carrier migration targets.
- Transcript archives stay in each machine's `transcript_archive_dir`; #24968
  (`files_home` I/O) has no Sessions consumer.
- Transport errors map through #24970's seven classes and adapters. #24963 owns
  the waiver loader and readiness rules; every changed behavior cites its waiver
  ID and owning section.
- Rust work loads the `rust` skill and follows `crates/AGENTS.md`: family crate
  conventions, `cargo nextest run -p <package>`, `CARGO_BUILD_JOBS=3`, unit
  tests outside production files. Python checks use `uv run`; pytest uses
  `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test`
  with `GOBBY_TEST_PROTECT=1`; daemon tests use isolated state and ports. No
  full pytest run.
- Release: 7.2 is the family's shipping commit for P0–P7. Each P8 leaf is its own
  shipping commit. Shipping commits bump each changed published binary 0.0.1,
  update `Cargo.lock` and `src/gobby/install/version_pins.py::MANAGED_BIN_VERSION_PINS`,
  and follow `project_policy.release_guidance`. Only the Orchestrator delegates
  cutover. No push is authorized here.
- The deliverables form one linear dependency chain, so every shared Target
  path (`crates/gsessions/src/lib.rs`, `crates/gsessions/Cargo.toml`,
  `docs/contracts/parity-waivers.json`, `crates/gdaemon/src/serve.rs`, the status
  shards, `Cargo.lock`, the version pins) has an ordered owner pair.

Consumer evidence:

- `gcode grep -F 'cli.sessions' src tests -m 60` found the lazy registration
  `src/gobby/cli/__init__.py::_COMMAND_SOURCES` (`"sessions": ("sessions",
  "sessions")`) and test consumers `tests/cli/test_cli_sessions.py`,
  `tests/cli/test_cli_sessions_coverage.py`,
  `tests/cli/test_cli_sessions_termination.py`,
  `tests/cli/test_sessions_coverage.py`,
  `tests/mcp_proxy/tools/test_session_reporting.py` and
  `tests/sessions/test_machine_scoped_consumers.py`; 7.3 owns each.
  `src/gobby/cli/sessions_dirty_paths.py::approve_dirty_paths` is consumed by
  `src/gobby/cli/sessions.py` and `tests/workflows/test_dirty_edit_grants.py`.
- `gcode grep -w '_RenumberMixin'` found only `src/gobby/storage/sessions/_manager.py`;
  `renumber_project_sessions` consumers are the CLI, two
  `TestSessionManagerLifecycle` tests and
  `tests/storage/test_sessions_import.py::EXPECTED_PUBLIC_METHOD_SIGNATURES`.
  `backfill_session_context_windows` consumers are the CLI and two tests in
  `tests/sessions/test_context_usage.py`; `_coerce_positive_int` has nine other
  callers and stays.
- `crates/gdaemon/src/front_door/routes.rs::reports_native_routes_for_families_gdaemon_lacks`
  asserts that `sessions` is unimplemented against the static `FAMILIES` slice
  (`HealthFamily`, `TerminalFamily`). The family is pushed into serve's runtime
  vector like `RuntimeFamily` (`crates/gdaemon/src/serve.rs::run`), so the
  static slice and that test stay unchanged.
- `crates/gdaemon/src/front_door/routes.rs` is 712 lines and
  `crates/gdaemon/src/serve.rs` is 818 lines (first `#[cfg(test)]` at 670).
  `crates/gdaemon/src/node_commands.rs` is 979 lines; 3.8 places its additions
  in a new module. `src/gobby/cli/sessions.py` is 886 lines; 7.3 deletes it.

## Parity ownership
`kind: framing`

Statuses are exactly `proxied`, `delegated` or `dropped by decision #N`. A row
changes only after its owner's parity proof and 7.4 or its P8 leaf records it.
A row whose Python surface is deleted reads `dropped by decision #25253` with
a note citing its native evidence (Decision Record item 15).

| Ledger row | Implementation | Final status owner |
| --- | --- | --- |
| CLI gobby sessions | 6.1 | 7.4 (proxied, #21565) |
| CLI gobby sessions approve-dirty-paths | 6.1 | 7.4 |
| CLI gobby sessions backfill-context-windows | dropped (B2), 7.3 | 7.4 |
| CLI gobby sessions delete | 2.2, 6.1 | 7.4 |
| CLI gobby sessions list | 6.1 | 7.4 |
| CLI gobby sessions messages | 6.1 | 7.4 |
| CLI gobby sessions renumber | dropped (B1), 7.3 | 7.4 |
| CLI gobby sessions restore | 6.1 | 7.4 |
| CLI gobby sessions show | 6.1 | 7.4 |
| CLI gobby sessions stats | 6.1 | 7.4 |
| CLI gobby sessions summarize | 4.1, 6.1 | 7.4 |
| HTTP DELETE /api/sessions/{session_id}/stop | 2.3 | 7.4 |
| HTTP GET /api/sessions | 1.1, 8.5 | 8.5 |
| HTTP GET /api/sessions/usage | 1.2 | 7.4 |
| HTTP GET /api/sessions/{session_id} | 1.3 | 7.4 |
| HTTP GET /api/sessions/{session_id}/changes | 1.3 | 7.4 |
| HTTP GET /api/sessions/{session_id}/changes/diff | 1.3 | 7.4 |
| HTTP GET /api/sessions/{session_id}/messages | 3.10 | 7.4 |
| HTTP GET /api/sessions/{session_id}/stop | 2.3 | 7.4 |
| HTTP GET /api/sessions/{session_id}/token-events | 1.2 | 7.4 |
| HTTP GET /api/sessions/{session_id}/transcript | 3.10 | 7.4 |
| HTTP GET /api/sessions/{session_id}/transcript/status | 3.10 | 7.4 |
| HTTP POST /api/sessions/bulk-move | 2.2 | 7.4 |
| HTTP POST /api/sessions/find_by_terminal_context | 1.1 | 7.4 |
| HTTP POST /api/sessions/find_current | 1.1 | 7.4 |
| HTTP POST /api/sessions/register | 2.1 | 7.4 |
| HTTP POST /api/sessions/update_status | 2.2 | 7.4 |
| HTTP POST /api/sessions/update_summary | 2.2 | 7.4 |
| HTTP POST /api/sessions/web-chat | 2.1 | 7.4 |
| HTTP POST /api/sessions/{session_id}/acp/close | 8.4 | 8.4 |
| HTTP POST /api/sessions/{session_id}/acp/delete | 8.4 | 8.4 |
| HTTP POST /api/sessions/{session_id}/expire | 2.2 | 7.4 |
| HTTP POST /api/sessions/{session_id}/generate-summary | 4.1 | 7.4 |
| HTTP POST /api/sessions/{session_id}/release-composer | not ported (Decision 6) | 7.4 (proxied, #25276) |
| HTTP POST /api/sessions/{session_id}/rename | 2.2 | 7.4 |
| HTTP POST /api/sessions/{session_id}/restore-transcript | 3.10 | 7.4 |
| HTTP POST /api/sessions/{session_id}/stop | 2.3 | 7.4 |
| HTTP POST /api/sessions/{session_id}/variables/get | 2.4 | 7.4 |
| HTTP POST /api/sessions/{session_id}/variables/set | 2.4 | 7.4 |
| HTTP POST /api/sessions/{session_ref}/dirty-path-approvals | 2.5 | 7.4 |
| HTTP POST /api/sessions/{session_ref}/native-terminal-binding | 2.5 | 7.4 |
| MCP gobby-hub:list_cross_project_sessions | 2.7, 5.1 | 7.4 (proxied, #21570), then 8.8 |
| MCP gobby-sessions:feedback | 2.6, 5.1 | 7.4 (proxied, #21570), then 8.8 |
| MCP gobby-sessions:get_current_session | 1.1, 5.1 | 7.4 (proxied, #21570), then 8.8 |
| MCP gobby-sessions:get_handoff | 2.8, 5.1 | 7.4 (proxied, #21570), then 8.8 |
| MCP gobby-sessions:get_session | 1.3, 5.1 | 7.4 (proxied, #21570), then 8.8 |
| MCP gobby-sessions:get_session_commits | 1.3, 5.1 | 7.4 (proxied, #21570), then 8.8 |
| MCP gobby-sessions:get_session_messages | 3.7, 5.1 | 7.4 (proxied, #21570), then 8.8 |
| MCP gobby-sessions:get_transcript_status | 3.6, 5.1 | 7.4 (proxied, #21570), then 8.8 |
| MCP gobby-sessions:get_usage_breakdown | 1.2, 5.1 | 7.4 (proxied, #21570), then 8.8 |
| MCP gobby-sessions:hold_session_claims | 2.6, 5.1 | 7.4 (proxied, #21570), then 8.8 |
| MCP gobby-sessions:list_sessions | 1.1, 5.1 | 7.4 (proxied, #21570), then 8.8 |
| MCP gobby-sessions:mark_loop_complete | 2.2, 5.1 | 7.4 (proxied, #21570), then 8.8 |
| MCP gobby-sessions:query_readonly | 2.7, 5.1 | 7.4 (proxied, #21570), then 8.8 |
| MCP gobby-sessions:register_session | 2.1, 5.1 | 7.4 (proxied, #21570), then 8.8 |
| MCP gobby-sessions:release_composer | not ported (Decision 6) | 7.4 (proxied, #25276) |
| MCP gobby-sessions:release_session_claims_hold | 2.6, 5.1 | 7.4 (proxied, #21570), then 8.8 |
| MCP gobby-sessions:restore_session_transcript | 3.6, 5.1 | 7.4 (proxied, #21570), then 8.8 |
| MCP gobby-sessions:search_session_messages | 3.7, 5.1 | 7.4 (proxied, #21570), then 8.8 |
| MCP gobby-sessions:session_stats | 1.2, 5.1 | 7.4 (proxied, #21570), then 8.8 |
| MCP gobby-sessions:set_handoff | 8.1, 8.2, 8.3, 8.6 | 8.6 (proxied, #21570), then 8.8 |
| MCP gobby-sessions:set_title | 2.2, 5.1 | 7.4 (proxied, #21570), then 8.8 |
| WS message stop_request | 8.7 | 8.7 |
| Native extension DELETE /api/sessions/{session_id} | 2.2 | no ledger key; waiver and 7.4 docs |

## P0: Freeze the family contract
`kind: framing`

### 0.1 Executable 63-row characterization corpus [category: test] (depends: #24963, #24970)
`kind: deliverable`

Targets:
- `scripts/sessions_parity_corpus.py`
- `crates/gsessions/tests/fixtures/parity.json`
- `tests/contracts/test_sessions_parity_corpus.py`

**Research context:** Python entry points are the routers under
`src/gobby/servers/routes/sessions/` (`core.py`, `lifecycle.py`, `messages.py`,
`analytics.py`, `changes.py`, `acp.py`, `variables.py`, `dirty_paths.py`,
`terminal_binding.py`), the MCP registry modules under
`src/gobby/mcp_proxy/tools/sessions/` plus
`src/gobby/mcp_proxy/tools/hub.py` (`list_cross_project_sessions`), the CLI
group `src/gobby/cli/sessions.py::sessions` with
`src/gobby/cli/sessions_dirty_paths.py::approve_dirty_paths`, and
`src/gobby/servers/websocket/handlers/core.py` (`HandlerMixin._handle_stop_request`).
Existing fixtures to reuse: `tests/servers/routes/test_servers_routes_sessions_routes.py`,
`tests/servers/routes/test_session_changes_routes.py`,
`tests/servers/routes/test_session_variables.py`,
`tests/servers/routes/test_sessions_acp_routes.py`,
`tests/servers/routes/test_session_composer_release.py`,
`tests/mcp_proxy/tools/sessions/` and `tests/workflows/test_dirty_edit_grants.py`.
The collector runs isolated Python fixtures (temporary home, isolated test
database) and writes a versioned JSON corpus: method/path, command or tool;
arguments; seeded rows and files; expected status, envelope, body, exit code,
stdout/stderr and side effects; and only explicitly enumerated volatile-field
normalization. It records MCP input schemas and their hashes before any
registration changes. It freezes injected-fault vectors for every HTTP and MCP
row, the operator-identity and break-glass refusals of the two operator-only
routes, and ordinary and managed browser-cookie admission with credentialed CORS
and preflight for every HTTP row. Each debt item B3–B7 and W3 gets its Python
before-state and the exact waiver ID from `## Debt not to port`; this leaf
proves loader matching with test-only waiver entries and native-result doubles,
and each owning leaf adds its real waiver entry. B1/B2 rows are recorded as
drop cases with no native expectation. The remote-owner cases record Python's
explicit refusals for sessions owned by another machine.

**Granularity:** One corpus covers all 63 rows because readiness needs one
key-set predicate; the three targets are the collector, its output and its test.

**Acceptance:**

- 0.1.1 - The corpus key set equals the 63-row #21562 ledger predicate, every kept
  key has a success and a validation or failure case, and non-Sessions keys are
  rejected. test: `tests/contracts/test_sessions_parity_corpus.py::test_inventory_matches_sessions_ledger`.
- 0.1.2 - Two collections against reset isolated state give identical normalized
  fixtures, schemas, ordering, output, exit codes and effects.
  test: `tests/contracts/test_sessions_parity_corpus.py::test_collection_is_reproducible`.
- 0.1.3 - Every HTTP row records ordinary and managed browser-cookie admission,
  missing or invalid credentials and credentialed CORS/preflight; the two
  operator-only routes record verified-identity and loopback break-glass
  refusals. test: `tests/contracts/test_sessions_parity_corpus.py::test_admission_matrix_is_recorded`.
- 0.1.4 - Each B3–B7 and W3 case resolves to exactly one waiver ID and evaluates
  explicit native response and effect assertions; untouched cases stay ordinary
  Passed cases. test: `tests/contracts/test_sessions_parity_corpus.py::test_debt_waivers_are_behavior_scoped`.
- 0.1.5 - A skipped-only report, an unwaived Skipped case, a stale waiver ID or an
  unrelated injected regression fails readiness even beside a valid Waived case.
  test: `tests/contracts/test_sessions_parity_corpus.py::test_waiver_does_not_hide_unrelated_diff`.

Planned verification: the five tests with the protected Python prefix and the
isolated test database. Nothing has run during drafting.

### 0.2 Provider transcript golden corpus [category: test] (depends: 0.1)
`kind: deliverable`

Targets:
- `scripts/sessions_transcript_corpus.py`
- `crates/gsessions/tests/fixtures/transcripts/manifest.json`
- `tests/contracts/test_sessions_transcript_corpus.py`

**Research context:** The Python transcript stack is about 12,700 lines:
provider parsers in `src/gobby/sessions/transcripts/` (`claude.py`,
`claude_records.py`, `codex.py`, `codex_items.py`, `codex_file_changes.py`,
`droid.py`, `grok.py`, `agy.py`, `base.py`, `tool_activity.py`), normalization
`src/gobby/sessions/transcript_normalization.py`, rendering
`src/gobby/sessions/transcript_renderer.py`,
`src/gobby/sessions/transcript_render_blocks.py` and
`src/gobby/sessions/transcript_render_models.py`, windows
`src/gobby/sessions/transcript_window.py` and
`src/gobby/sessions/transcript_cursor.py`, indexing
`src/gobby/sessions/transcript_index.py`,
`src/gobby/sessions/transcript_index_sidecar.py` and
`src/gobby/sessions/transcript_index_resume.py`, gzip seeking
`src/gobby/sessions/gzip_seek_index.py`, search
`src/gobby/sessions/transcript_search.py`, status
`src/gobby/sessions/transcript_status.py`, archive
`src/gobby/sessions/transcript_archive.py` and the reader
`src/gobby/sessions/transcript_reader.py`. Existing provider fixtures live in
`tests/sessions/transcripts/fixtures/`. The collector feeds every fixture, plus
synthesized edge cases (Unicode line separators inside JSON strings, truncated
final line, unknown record types, interrupted turns, gzip archive with a seek
index, a transcript above the rendered-size limit), through the Python stack and
records parsed records, normalized records, message statistics
(`compute_message_stats`), token events, rendered window pages with cursors,
search hits, index-sidecar summaries and status results. It also records
provider path discovery over a fixture home, Python-written gzip archives with
their seek indexes and index sidecars, unmodeled observations, tail-cursor and
interrupt observations, and Codex compact boundaries. Native transcript leaves
replay this manifest; P0 adds no native code.

**Acceptance:**

- 0.2.1 - The manifest covers all five providers (claude, codex, droid, grok,
  agy) and each listed edge case, and every entry records parsed, normalized,
  statistics, token-event, window, search, index, status, path, archive,
  observation and cursor expectations.
  test: `tests/contracts/test_sessions_transcript_corpus.py::test_manifest_covers_providers_and_edges`.
- 0.2.2 - Two collections give byte-identical manifests.
  test: `tests/contracts/test_sessions_transcript_corpus.py::test_transcript_collection_is_reproducible`.

Planned verification: both tests with the protected Python prefix. Nothing has
run during drafting.

## P1: Native session reads
`kind: framing`

Lookup, usage and git views over hub rows. Every route stays proxied in
production until 7.2; tests drive the native router and services directly.

### 1.1 Sessions crate, ref resolution and lookup reads [category: code] (depends: 0.2)
`kind: deliverable`

Targets:
- `Cargo.toml`
- `Cargo.lock`
- `crates/gsessions/Cargo.toml`
- `crates/gsessions/src/lib.rs`
- `crates/gsessions/src/family.rs`
- `crates/gsessions/src/model.rs`
- `crates/gsessions/src/refs.rs`
- `crates/gsessions/src/store.rs`
- `crates/gsessions/src/lookup.rs`
- `crates/gsessions/src/http.rs`
- `crates/gsessions/src/ports.rs`
- `crates/gsessions/tests/common/mod.rs`
- `crates/gsessions/tests/lookup.rs`
- `crates/gprojects/src/lib.rs`
- `crates/gprojects/src/names.rs`
- `crates/gtasks/src/lib.rs`
- `crates/gtasks/src/session_refs.rs`
- `crates/gdaemon/Cargo.toml`
- `crates/gdaemon/src/serve.rs::*` — scope-reason: push the Sessions family into the runtime family vector in both cfg-gated run entry points, as RuntimeFamily is pushed
- `crates/gdaemon/src/front_door/mod.rs::*` — scope-reason: declare the Sessions composition module
- `crates/gdaemon/src/front_door/sessions.rs`
- `crates/gdaemon/tests/sessions_family.rs`

**Research context:**
- Python sources:
  - `src/gobby/servers/routes/sessions/core.py::list_sessions` takes
    `project_id`, `status`, `status_in`, `source`, `sources`, `machine_id`,
    `mode`, `model`, `exclude_subagents`, `include_resumability`,
    `current_session_id`, `session_seq_min`, `session_seq_max`,
    `task_ref_min`, `task_ref_max`, `task_ref_role`, `created_after`,
    `created_before`, `limit` (1..1000, default 100) and the keyset cursor
    `cursor_updated_at` + `cursor_id`.
  - `core.py::_bind_worktrees` sets each row's `worktree_id` only for rows of
    the serving machine with a workspace path, from that project's worktree
    roots; other rows get null.
  - Foreign reads in the list: `project_name` joins the projects table;
    `src/gobby/storage/sessions/_query.py::fetch_task_refs_by_session` loads
    claimed, created and closed task sequence numbers per session from the
    tasks table; the task-ref range filter is an `EXISTS` over tasks in the
    same file's list query (default role `claimed`).
  - `src/gobby/servers/routes/sessions/lifecycle.py::find_current_session` and
    `lifecycle.py::find_session_by_terminal_context` own the two lookup routes.
  - The MCP tools are `src/gobby/mcp_proxy/tools/sessions/_crud.py::list_sessions`
    and `_crud.py::get_current_session`.
  - Rows come from `src/gobby/storage/sessions/_manager.py::SessionManager`.
  - Routes resolve refs with
    `src/gobby/storage/session_resolution.py::resolve_session_reference`; the
    CLI uses `src/gobby/cli/utils_resolution.py::resolve_session_id`. Both
    accept `#N`, a bare number, a UUID and a unique prefix, scoped to a
    project.
- Family composition: `crates/gcore/src/route_family.rs::RouteFamily` supplies
  `name`, `prefixes`, `router`, `claims(parts)` and `check_proxied`.
  `crates/gruntime/src/lib.rs::RuntimeFamily` is the template, with its
  `NATIVE_ROUTES` table and `Caller` admission. `serve.rs::run` pushes
  `RuntimeFamily` into the family vector only when a pool exists; `SessionsFamily`
  is pushed the same way, so hub and standalone construct it and a node
  constructs none. The static `FAMILIES` slice and
  `crates/gdaemon/src/front_door/routes.rs::reports_native_routes_for_families_gdaemon_lacks`
  stay as they are.
- Claims: the family claims only implemented method/path pairs for API-key
  callers. Browser-cookie requests stay with the proxy until 7.1.
  `GET /api/sessions` is not claimed in any mode until 8.5, because every
  listed row carries the ACP block that
  `src/gobby/sessions/acp_lifecycle.py::attach_acp_block` reads from the live
  web-chat runtime (#21592). This leaf builds the list service and every
  filter; 8.5 adds the ACP block and resumability and claims the route.
- Foreign owners (Decision 7):
  - `crates/gprojects/src/names.rs` adds `project_names(conn, project_ids)`,
    an id-to-name map read from the projects table only.
  - `crates/gtasks/src/session_refs.rs` adds
    `task_refs_by_session(conn, session_ids)` (the per-role sequence lists)
    and `sessions_with_task_refs(conn, project_id, roles, min, max)` (a
    session-id set). The list query filters its own rows with
    `id = ANY(...)`, so keyset pages are unchanged.
  - Both crates' `lib.rs` export the new function. Each reads only its own
    tables. Cross-plan edges: `gdaemon-projects-files-docs-family 1.2`
    creates gprojects and `tasks-family 1.1` creates gtasks; the
    Orchestrator converts both at expansion.
  - `ports.rs` declares the gagents port trait (Decision 7):
    `run_rows`, `move_run_lineage` and `containing_worktree_id`. The list
    keeps the serving-machine check and calls `containing_worktree_id` for
    each local row. Tests bind a fixture adapter; 7.2 binds the real one.
- `store.rs` exports `session_rows(tx, selector, lock)` for the agents
  family (Decision 10). The selector is a set of session ids or a set of
  agent run ids matched on `sessions.agent_run_id`; `lock` takes
  `FOR UPDATE` on the selected rows.
- `crates/gdaemon/src/front_door/sessions.rs` builds the family from the shared
  gcore pool and the owner channel adapter that 1.3 fills in. Domain errors map
  through #24970's adapter; no local transport mapping is added.
- Public API: `lib.rs` exports the store, `resolve_session_ref` and the lookup
  services. Later families call these; transports never hold business logic.
- Test fixture: `crates/gsessions/tests/common/mod.rs` follows
  `crates/gruntime/tests/common/mod.rs::database_case` (protected isolated test
  hub, per-test schema). That fixture skips when `DATABASE_URL` is unset, so
  planned verification always sets it; a skipped DB case is not evidence.

**Granularity:** Creating the crate, its composition, the list service and
two lookup routes is one usable outcome. The production-file count passes six
because composition crosses the crate boundary and the list's foreign reads
need one narrow export each in gprojects and gtasks. The acceptance count
passes six because `session_rows` and the owner exports are the crate's
first public reads. Usage and git views are separate leaves.

**Acceptance:**

- 1.1.1 - The list service matches the corpus `GET /api/sessions` rows,
  without the ACP block and resumability, for every filter (including model,
  sequence, task-ref and creation-time bounds), limit bound and keyset cursor
  page. test: `crates/gsessions/tests/lookup.rs::list_service_matches_python`.
- 1.1.2 - `GET /api/sessions` is not claimed in any backend mode, with or
  without `include_resumability`.
  test: `crates/gsessions/tests/lookup.rs::list_route_is_not_claimed`.
- 1.1.3 - `HTTP POST /api/sessions/find_current` and
  `HTTP POST /api/sessions/find_by_terminal_context` match found, not-found and
  validation cases. test: `crates/gsessions/tests/lookup.rs::find_routes_match_python`.
- 1.1.4 - `#N`, number, UUID and prefix refs resolve as Python does within a
  project; ambiguous and unknown refs return the corpus errors.
  test: `crates/gsessions/tests/lookup.rs::session_refs_resolve_like_python`.
- 1.1.5 - The `list_sessions` and `get_current_session` services return the
  corpus MCP results for each argument case.
  test: `crates/gsessions/tests/lookup.rs::mcp_lookup_services_match_python`.
- 1.1.6 - Serve composes the family only when a pool exists, and browser-cookie
  requests are not claimed. test: `crates/gdaemon/tests/sessions_family.rs::family_composes_only_with_a_pool`.
- 1.1.7 - `session_rows` returns the rows selected by session ids and by
  agent run ids, and with `lock` set a second transaction's update of a
  selected row waits until the first commits.
  test: `crates/gsessions/tests/lookup.rs::session_rows_select_and_lock`.
- 1.1.8 - Listed rows take project names from `project_names`, task refs and
  the task-ref filter from the two gtasks functions, and `worktree_id` from
  the gagents port for serving-machine rows only, matching the corpus with a
  fixture port adapter.
  test: `crates/gsessions/tests/lookup.rs::list_foreign_reads_use_owner_apis`.

Planned verification:
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-sessions --test lookup`;
`cargo nextest run -p gobby-daemon --test sessions_family`;
`cargo clippy -p gobby-sessions -p gobby-daemon`. Nothing has run during
drafting.

### 1.2 Usage and token-event reads [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `crates/gsessions/src/lib.rs`
- `crates/gsessions/src/family.rs`
- `crates/gsessions/src/http.rs`
- `crates/gsessions/src/usage.rs`
- `crates/gsessions/tests/usage.rs`

**Research context:** `src/gobby/servers/routes/sessions/core.py::get_usage_breakdown`
and `core.py::get_session_token_events` own the two HTTP rows. The MCP tools
are `src/gobby/mcp_proxy/tools/sessions/_crud.py::session_stats` and
`_crud.py::get_usage_breakdown`. Both read hub rows only (sessions and the
token-event table that `crates/gdaemon/src/retention.rs` prunes); no transcript
is read. Grouping, time windows, model and provider buckets, zero-usage rows
and JSON nulls match the corpus.

**Acceptance:**

- 1.2.1 - `HTTP GET /api/sessions/usage` matches every grouping and window
  case. test: `crates/gsessions/tests/usage.rs::usage_breakdown_matches_python`.
- 1.2.2 - `HTTP GET /api/sessions/{session_id}/token-events` matches ordering,
  paging and unknown-session cases. test: `crates/gsessions/tests/usage.rs::token_events_match_python`.
- 1.2.3 - The `session_stats` and `get_usage_breakdown` services return the
  corpus MCP results. test: `crates/gsessions/tests/usage.rs::mcp_usage_services_match_python`.

Planned verification: the `usage` test binary with the 1.1 DB prefix. Nothing
has run during drafting.

### 1.3 Session detail, commits and changes [category: code] (depends: 1.2, #24521)
`kind: deliverable`

Targets:
- `crates/gsessions/src/lib.rs`
- `crates/gsessions/src/family.rs`
- `crates/gsessions/src/http.rs`
- `crates/gsessions/src/owner.rs`
- `crates/gsessions/src/ports.rs`
- `crates/gsessions/src/git_views.rs`
- `crates/gsessions/tests/git_views.rs`
- `crates/gtasks/src/session_refs.rs`
- `crates/gdaemon/src/front_door/sessions.rs`
- `docs/contracts/parity-waivers.json::*` — scope-reason: add the B5 and W3 Sessions waiver entries

**Research context:**
- Python sources:
  - `src/gobby/servers/routes/sessions/lifecycle.py::sessions_get` with
    `lifecycle.py::_get_session_stats` and `core.py::_get_commit_count`.
  - `src/gobby/mcp_proxy/tools/sessions/_crud.py::get_session`.
  - `src/gobby/mcp_proxy/tools/sessions/_commits.py::get_session_commits` and
    `_commits.py::_session_repo_cwd` (default `max_commits` 20).
  - `src/gobby/servers/routes/sessions/changes.py::session_changes` and
    `changes.py::session_change_diff` over `src/gobby/servers/session_changes.py`.
  - `src/gobby/sessions/machine_scope.py::require_local_session_ownership`
    refuses a session owned by another machine.
- Foreign reads in `_get_session_stats` (Decision 7): `tasks_closed` counts
  `session_tasks` rows with action `closed`; `memories_created` counts
  `memories` by `source_session_id`; `skills_used` counts distinct
  `session_skills` names. Native reads them through
  `closed_task_count(conn, session_id)`, added to
  `crates/gtasks/src/session_refs.rs`, and two port functions in `ports.rs`:
  `memories_created(session_id)` for gmemory and `skills_used(session_id)`
  for gskills, which owns `session_skills`
  (`.gobby/plans/gdaemon-skills-family.md` 1.6). Tests bind fixture
  adapters; 7.2 binds the real ones.
- Ownership seam: `owner.rs` defines the `OwnerChannel` trait the git views and
  transcript leaves share. The hub resolves the owning machine from the session
  row. The serving machine runs git in-process in the session's checkout. A
  remote owner is reached through the gdaemon adapter in `sessions.rs`, which
  calls `crates/gdaemon/src/nodes/registry.rs::Registry::command_sender` and
  `CommandSender::request` with the existing `checkout.read` command (`git log`,
  `rev-list`, `diff`, `status`), whose node handler is #24521. An unknown or
  offline owner returns the corpus refusal; nothing falls back to the hub
  filesystem.
- B5: one timeframe contract with offset ISO bounds from the session's
  `created_at` and end time serves both the detail view's commit count and
  `get_session_commits`. Waivers
  `sessions.mcp-get_session_commits.unified-time-bounds` and
  `sessions.http-get.unified-time-bounds` go into the shared waiver file, citing
  this section.
- W3: an unexpected storage error in the statistics query propagates as the
  shared internal error; waiver `sessions.http-get.stats-storage-errors`
  covers the fault vector only.

**Acceptance:**

- 1.3.1 - `HTTP GET /api/sessions/{session_id}` matches the corpus detail,
  statistics and not-found cases, with B5 bounds under its waiver.
  test: `crates/gsessions/tests/git_views.rs::session_detail_matches_python`.
- 1.3.2 - The `get_session` and `get_session_commits` services match the
  corpus, and commit counts agree with the listed commits for the same
  session. test: `crates/gsessions/tests/git_views.rs::commit_views_share_one_timeframe`.
- 1.3.3 - `HTTP GET /api/sessions/{session_id}/changes` and
  `HTTP GET /api/sessions/{session_id}/changes/diff` match the corpus for local
  sessions. test: `crates/gsessions/tests/git_views.rs::changes_match_python`.
- 1.3.4 - A remote owner is served through `checkout.read` with identical
  results, and an offline or unknown owner returns the explicit refusal.
  test: `crates/gsessions/tests/git_views.rs::remote_owner_uses_checkout_read`.
- 1.3.5 - An injected statistics storage fault returns the shared internal
  error and reports its exact W3 waiver.
  test: `crates/gsessions/tests/git_views.rs::stats_storage_errors_propagate`.
- 1.3.6 - Detail statistics take the closed-task count from gtasks and the
  memory and skill counts from the ports, matching the corpus with fixture
  adapters. test: `crates/gsessions/tests/git_views.rs::detail_counts_use_owner_apis`.

Planned verification: the `git_views` test binary with the 1.1 DB prefix, using
a temporary git checkout and a doubled owner channel. Nothing has run during
drafting.

## P2: Native session mutations
`kind: framing`

Registration, lifecycle writes, stop signals, variables, operator routes,
claim holds, handoff reads and read-only queries. Each leaf adds its services
to the public library API.

### 2.1 Registration, web-chat creation and session events [category: code] (depends: 1.3)
`kind: deliverable`

Targets:
- `crates/gsessions/src/lib.rs`
- `crates/gsessions/src/family.rs`
- `crates/gsessions/src/http.rs`
- `crates/gsessions/src/register.rs`
- `crates/gsessions/src/events.rs`
- `crates/gsessions/tests/register.rs`
- `crates/gdaemon/src/front_door/sessions.rs`
- `crates/gdaemon/src/serve.rs::*` — scope-reason: bind the Sessions event sink to the broadcast broker before serving starts

**Research context:**
- Python sources: `src/gobby/servers/routes/sessions/core.py::register_session`,
  `src/gobby/mcp_proxy/tools/sessions/_registration.py::register_session` and
  `core.py::create_web_chat_session` (the session row plus its
  `sandbox_policy_hash`). Storage is `src/gobby/storage/sessions/_crud.py` and
  `src/gobby/storage/sessions/_web_chat_crud.py`.
- Events: Python sends `session_event` frames through
  `src/gobby/servers/websocket/broadcast.py::BroadcastMixin.broadcast_session_event`,
  filtered by the session's project. A native write never reaches Python's
  in-process session-change listener, so `events.rs` defines a
  `SessionEventSink` that each committed write calls after commit. The
  gdaemon adapter publishes through
  `crates/gdaemon/src/websocket/broker.rs::BroadcastBroker::publish` with the
  filter `src/gobby/servers/websocket/native_publish.py::NativePublisher` sends
  for session events. The Tasks plan's bridge is the precedent: serve
  constructs the broker before the listeners bind and binds the sink once.
- Public API: `register_session` and `create_session` are exported for Cron
  2.4's cron parent sessions (`.gobby/plans/gdaemon-cron-family.md`) and for the
  hooks family's node session registration
  (`.gobby/plans/s2-11-hub-hooks-rules.md` item 8). `create_session` also
  takes the child-session fields the agents family sets when it spawns a
  run (Decision 10): parent session id, agent run id, agent depth and a
  pre-created external id, written in the caller's transaction.

**Acceptance:**

- 2.1.1 - `HTTP POST /api/sessions/register` matches create, re-register,
  validation and project-resolution cases, including emitted events.
  test: `crates/gsessions/tests/register.rs::register_matches_python`.
- 2.1.2 - `HTTP POST /api/sessions/web-chat` creates the row and policy hash
  the corpus records. test: `crates/gsessions/tests/register.rs::web_chat_creation_matches_python`.
- 2.1.3 - The `register_session` service returns the corpus MCP results.
  test: `crates/gsessions/tests/register.rs::mcp_register_service_matches_python`.
- 2.1.4 - Each committed write publishes one `session_event` with Python's
  project filter, and a rolled-back write publishes none.
  test: `crates/gsessions/tests/register.rs::events_publish_after_commit_only`.
- 2.1.5 - `create_session` with child fields writes the parent session,
  agent run, depth and pre-created external id in the caller's transaction,
  and a rolled-back caller leaves no row.
  test: `crates/gsessions/tests/register.rs::create_session_writes_child_fields`.

Planned verification: the `register` test binary with the 1.1 DB prefix and a
doubled event sink; `cargo nextest run -p gobby-daemon --test sessions_family`.
Nothing has run during drafting.

### 2.2 Lifecycle writes, native delete and status notifications [category: code] (depends: 2.1)
`kind: deliverable`

Targets:
- `crates/gsessions/src/lib.rs`
- `crates/gsessions/src/family.rs`
- `crates/gsessions/src/http.rs`
- `crates/gsessions/src/events.rs`
- `crates/gsessions/src/mutate.rs`
- `crates/gsessions/src/ports.rs`
- `crates/gsessions/tests/mutate.rs`
- `crates/gtasks/src/session_refs.rs`
- `docs/contracts/parity-waivers.json::*` — scope-reason: add the B6, B7 and native DELETE waiver entries
- `src/gobby/storage/sessions/status_notifications.py`
- `src/gobby/sessions/lifecycle.py::*` — scope-reason: start and stop the session status notification bridge with the hub's background jobs
- `tests/storage/sessions/test_status_notifications.py`

**Research context:**
- Python sources:
  - `src/gobby/servers/routes/sessions/lifecycle.py::update_session_status`,
    `lifecycle.py::expire_session`, `lifecycle.py::rename_session`,
    `lifecycle.py::bulk_move_sessions` and `lifecycle.py::_move_sessions`.
  - `src/gobby/servers/routes/sessions/analytics.py::update_session_summary`.
  - `src/gobby/mcp_proxy/tools/sessions/_handoff.py::set_title` and
    `src/gobby/mcp_proxy/tools/sessions/_commits.py::mark_loop_complete`.
  - Storage: `src/gobby/storage/sessions/_field_update.py::_FieldUpdateMixin`
    and `src/gobby/storage/sessions/_bulk_update.py`.
  - Session erasure: `src/gobby/cli/sessions.py::delete_session` resolves the ref and
    calls `src/gobby/storage/sessions/_identity_crud.py::delete`, which refuses
    a session with retained references, hard-deletes the row and emits
    `session_deleted` plus a `deleted` status transition.
- Retained references (`src/gobby/storage/session_lifecycle.py::session_has_retained_references`
  over `_EMPTY_SESSION_PRUNE_REFERENCE_COLUMNS`): child sessions are read
  from the Sessions table. The foreign references go through their owners
  (Decision 7):
  - tasks: `referenced_session_ids(conn, session_ids)`, added to
    `crates/gtasks/src/session_refs.rs`;
  - memories: the gmemory port;
  - agent runs: the gagents port's `run_rows` over the three session
    columns;
  - `workflow_audit_log`: the workflow-audit port.
  The `pending_approvals` entry names a table the schema lacks, so Python
  skips it and native omits it. `mutate.rs` exports the check for 4.2's
  empty-session prune.
- B7: `update_status` validates the transition under a row lock; an invalid
  transition returns 400 under `sessions.http-update-status.invalid-transition`.
  B6: bulk-move reports per-row failures through #24970's classes under
  `sessions.http-bulk-move.typed-row-errors`.
- Native DELETE: `DELETE /api/sessions/{session_id}` resolves the ref, returns
  409 with the retained-reference names, 404 for an unknown session, and
  deletes with the same events. It carries waiver
  `sessions.http-delete.native-extension`; 7.4 documents it.
- Status notifications (ruling C4 (a)): Python status listeners are in-process.
  Two live registrants depend on them:
  `src/gobby/events/wake_recovery.py` (wake replay on a transition to
  `paused`) and `src/gobby/runner_broadcasting.py::setup_session_status_communications`.
  Every native status write calls
  `pg_notify('gobby_session_status', <transition JSON>)` inside its write
  transaction. The new Python `status_notifications.py` holds one autocommit
  LISTEN connection, modelled on
  `src/gobby/storage/config_notifications.py::ConfigNotificationListener`,
  and dispatches each notification to the session manager's registered
  status-transition listeners. Python's own writes keep in-process dispatch,
  so no transition is dispatched twice. `SessionLifecycleManager` starts the
  bridge with its hub jobs and stops it on shutdown. No schema change.
- Retirement edge: the bridge is deleted when the last registrant ports.
  The wake-replay registrant retires with `.gobby/plans/agents-family.md`
  D1.3, and status communications with #21583 (Decision 15). The
  Orchestrator records those edges to this section.
- Public API: status update, expiry, rename and delete services; 4.1's expiry
  job calls the same expiry service.

**Granularity:** Eight rows share one row-locked mutation service and one event
and notification path; splitting them would duplicate both. The production
files are the mutation service, its routes, the shared event module, the
port declarations, the one gtasks read and the one Python bridge. Delete
cannot ship without its retained-reference check, which is the eighth
acceptance item.

**Acceptance:**

- 2.2.1 - `HTTP POST /api/sessions/update_status` matches valid transitions and
  returns 400 for an invalid one under its B7 waiver.
  test: `crates/gsessions/tests/mutate.rs::update_status_validates_transitions`.
- 2.2.2 - `HTTP POST /api/sessions/{session_id}/expire`,
  `HTTP POST /api/sessions/{session_id}/rename` and
  `HTTP POST /api/sessions/update_summary` match the corpus.
  test: `crates/gsessions/tests/mutate.rs::expire_rename_and_summary_match_python`.
- 2.2.3 - `HTTP POST /api/sessions/bulk-move` matches moved rows and reports
  typed per-row errors under its B6 waiver.
  test: `crates/gsessions/tests/mutate.rs::bulk_move_reports_typed_row_errors`.
- 2.2.4 - The `set_title` and `mark_loop_complete` services return the corpus
  MCP results. test: `crates/gsessions/tests/mutate.rs::mcp_mutation_services_match_python`.
- 2.2.5 - Native DELETE returns 404, 409 with retained-reference names, or
  success with `session_deleted`, and reports its waiver.
  test: `crates/gsessions/tests/mutate.rs::native_delete_matches_storage_delete`.
- 2.2.6 - Each native status write sends exactly one notification inside its
  transaction, and a rolled-back write sends none.
  test: `crates/gsessions/tests/mutate.rs::status_writes_notify_inside_the_transaction`.
- 2.2.7 - The Python bridge dispatches each notification once to every
  registered listener, reconnects after a dropped connection and stops cleanly.
  test: `tests/storage/sessions/test_status_notifications.py::test_bridge_dispatches_native_transitions_once`.
- 2.2.8 - The retained-reference check refuses a session referenced by a
  child session, a task, a memory, an agent run or a workflow audit row,
  reading each foreign table only through its owner function or fixture
  port adapter. test: `crates/gsessions/tests/mutate.rs::retained_references_use_owner_apis`.

Planned verification: the `mutate` test binary with the 1.1 DB prefix; the
bridge test with
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest`;
scoped Ruff and mypy on the two Python files. Nothing has run during drafting.

### 2.3 Stop signals [category: code] (depends: 2.2)
`kind: deliverable`

Targets:
- `crates/gsessions/src/lib.rs`
- `crates/gsessions/src/family.rs`
- `crates/gsessions/src/http.rs`
- `crates/gsessions/src/stop.rs`
- `crates/gsessions/tests/stop.rs`
- `docs/contracts/parity-waivers.json::*` — scope-reason: add the three B4 stop-route waiver entries

**Research context:** `src/gobby/servers/routes/sessions/analytics.py::stop_session`,
`analytics.py::get_stop_signal` and `analytics.py::clear_stop_signal` sit over
`src/gobby/autonomous/stop_registry.py::StopRegistry` and the
`session_stop_signals` table (foreign key to `sessions(id)`). They emit
`session_stop_signaled` and `session_stop_cleared` events. B4: the routes take
the raw path id, so a `#N` ref is never resolved and an unknown session reaches
a foreign-key failure. Native routes resolve refs and return 404 for an unknown
session under `sessions.http-stop-post.ref-resolution`,
`sessions.http-stop-get.ref-resolution` and
`sessions.http-stop-delete.ref-resolution`. W2: `StopRegistry.acknowledge` and
`StopRegistry.cleanup_stale` have no production caller and are not ported. The
stop-signal read is exported for the rule-engine seam adapters
(`.gobby/plans/rust-rule-evaluation.md` D1.2).

**Acceptance:**

- 2.3.1 - `HTTP POST /api/sessions/{session_id}/stop`,
  `HTTP GET /api/sessions/{session_id}/stop` and
  `HTTP DELETE /api/sessions/{session_id}/stop` match the corpus for known
  sessions, including events. test: `crates/gsessions/tests/stop.rs::stop_routes_match_python`.
- 2.3.2 - A `#N` ref resolves and an unknown session returns 404 under each B4
  waiver. test: `crates/gsessions/tests/stop.rs::stop_routes_resolve_refs`.

Planned verification: the `stop` test binary with the 1.1 DB prefix. Nothing
has run during drafting.

### 2.4 Session variables [category: code] (depends: 2.3, #25262)
`kind: deliverable`

Targets:
- `crates/gsessions/src/lib.rs`
- `crates/gsessions/src/family.rs`
- `crates/gsessions/src/http.rs`
- `crates/gsessions/src/variables.rs`
- `crates/gsessions/src/clear_lineage.rs`
- `crates/gsessions/src/ports.rs`
- `crates/gsessions/tests/variables.rs`

**Research context:**
- `src/gobby/servers/routes/sessions/variables.py::set_variable`,
  `variables.py::get_variable` and `variables.py::_bound_session_id` resolve the
  authenticated caller apart from the path's target session (verified agent
  claims). `_bound_session_id` follows a run's live binding through
  `src/gobby/sessions/clear_run_lineage.py::current_run_session_id`, which this
  leaf ports into the new `clear_lineage.rs`; 2.9 adds the rest of that module.
  The routes then call
  `src/gobby/mcp_proxy/tools/workflows/_variables.py::set_variable` and
  `_variables.py::get_variable`. Those apply `_variables.py::_coerce_value`,
  skill-list validation, reserved names
  (`src/gobby/workflows/reserved_variables.py::is_reserved_workflow_variable`),
  agent step instances and
  `src/gobby/workflows/state_manager.py::SessionVariableManager`.
- Read-modify-write applies installed variable defaults before the caller's
  change (memory 38321642).
- Locking: each mutation takes the explicit `SessionVariableMutation`
  advisory-lock key that #25262 assigns.
- Foreign reads and writes (Decision 7):
  - `current_run_session_id` reads the run through the gagents port's
    `run_rows`.
  - A `session_task` value resolves through gtasks' `refs.rs` `resolve_read`
    (`.gobby/plans/tasks-family.md` 1.1).
  - `scope: "step"` reads and merges the session's `agent_step_instances`
    row (`src/gobby/workflows/step_instances.py::AgentStepInstanceManager`),
    which Workflows owns. It goes through a step-instance port. At 7.2
    gdaemon binds a transitional Rust adapter that runs those two
    statements; #25279's crate exports them, replaces the adapter body and
    deletes the adapter (the Orchestrator's ruling A for owners that
    #21562 blocks).
- The gobby-workflows MCP variable tools belong to their own family and are not
  ported here. This leaf owns the HTTP rows and exports a general key-scoped
  variable read and mutation API: a value set and a bounded-list upsert for
  any non-reserved key, under the `SessionVariableMutation` key at priority
  950 (Decision 10). The rule-engine seam adapters
  (`.gobby/plans/rust-rule-evaluation.md` D1.2) and Memory 6.1 call it. It
  absorbs the narrow writer that Memory 6.1 planned in
  `crates/gmemory/src/session_vars.rs`.

**Acceptance:**

- 2.4.1 - `HTTP POST /api/sessions/{session_id}/variables/set` matches coercion,
  reserved-name, skill-list, caller-binding and default-layering cases.
  test: `crates/gsessions/tests/variables.rs::set_variable_matches_python`.
- 2.4.2 - `HTTP POST /api/sessions/{session_id}/variables/get` matches single,
  listing and missing-variable cases.
  test: `crates/gsessions/tests/variables.rs::get_variable_matches_python`.
- 2.4.3 - Concurrent mutations of one session serialize on the #25262 key and
  lose no write. test: `crates/gsessions/tests/variables.rs::concurrent_mutations_serialize`.
- 2.4.4 - The public API sets an arbitrary non-reserved key and upserts a
  bounded list without losing a concurrent upsert to the same list, and
  refuses a reserved key.
  test: `crates/gsessions/tests/variables.rs::general_writer_handles_arbitrary_keys`.
- 2.4.5 - Caller binding reads runs through the gagents port, `session_task`
  values resolve through gtasks, and step-scoped reads and writes go through
  the step-instance port, with fixture adapters.
  test: `crates/gsessions/tests/variables.rs::foreign_variable_paths_use_owner_apis`.

Planned verification: the `variables` test binary with the 1.1 DB prefix.
Nothing has run during drafting.

### 2.5 Operator-only dirty-path approvals and terminal binding [category: code] (depends: 2.4)
`kind: deliverable`

Targets:
- `crates/gsessions/src/lib.rs`
- `crates/gsessions/src/family.rs`
- `crates/gsessions/src/http.rs`
- `crates/gsessions/src/operator.rs`
- `crates/gsessions/tests/operator.rs`

**Research context:**
- `src/gobby/servers/routes/sessions/dirty_paths.py::approve_dirty_paths`
  requires `src/gobby/servers/auth_service.py::AuthService.verified_front_door_identity`
  (403 `Operator API key required` otherwise), resolves the ref (404), then
  calls `src/gobby/workflows/dirty_edit_grants.py::record_dirty_path_approval`
  with `approved_by` = `<user_id>/<key_id>` (400 on a validation error).
- `src/gobby/servers/routes/sessions/terminal_binding.py::attach_native_terminal`
  admits only `AuthService.break_glass_admits`: a loopback peer presenting the
  `x-gobby-break-glass` value that matches the `break_glass` file
  (`src/gobby/utils/break_glass.py::break_glass_matches`); otherwise 403
  `Local operator recovery key required`. It accepts only a Codex session (400)
  and calls `src/gobby/sessions/terminal_binding.py::repair_native_terminal_binding`.
- Native identity: the front door sets `x-gobby-user-id` and `x-gobby-key-id`
  after verifying an API key. `operator.rs` reads only those verified headers
  for the operator identity. It verifies break-glass itself from the
  connection's peer address and a constant-time comparison against the
  `break_glass` file under the daemon home; no native break-glass verifier
  exists today. A browser cookie never admits either route.
- Security: the Orchestrator applies `security-review` after expansion, and
  activation of these two routes at 7.2 waits for a Security Reviewer's
  approval of the exact implementation SHA.

**Acceptance:**

- 2.5.1 - `HTTP POST /api/sessions/{session_ref}/dirty-path-approvals` records
  approvals for a verified operator key and refuses a missing identity, a
  browser cookie, an unknown ref and invalid paths as the corpus records.
  test: `crates/gsessions/tests/operator.rs::dirty_path_approvals_require_operator_identity`.
- 2.5.2 - `HTTP POST /api/sessions/{session_ref}/native-terminal-binding`
  admits only a loopback peer with the matching break-glass value and repairs a
  Codex binding as Python does.
  test: `crates/gsessions/tests/operator.rs::terminal_binding_requires_loopback_break_glass`.
- 2.5.3 - A non-loopback peer, a wrong or empty break-glass value, a missing
  break-glass file and a spoofed identity header from an unauthenticated
  request are all refused. test: `crates/gsessions/tests/operator.rs::operator_routes_refuse_forged_credentials`.

Planned verification: the `operator` test binary with the 1.1 DB prefix and a
temporary daemon home. Nothing has run during drafting.

### 2.6 Claim holds and feedback [category: code] (depends: 2.5)
`kind: deliverable`

Targets:
- `crates/gsessions/src/lib.rs`
- `crates/gsessions/src/claim_hold.rs`
- `crates/gsessions/src/feedback.rs`
- `crates/gsessions/tests/claim_hold.rs`

**Research context:**
- Claim holds: `src/gobby/mcp_proxy/tools/sessions/_claim_hold.py::hold_session_claims`,
  `_claim_hold.py::release_session_claims_hold`, `_claim_hold.py::_authorize`
  and `_claim_hold.py::_held_by_other`, over
  `src/gobby/sessions/operator_claim_hold.py` and the upsert in
  `src/gobby/storage/sessions/_operator_claim_hold.py`.
- Feedback: `src/gobby/mcp_proxy/tools/sessions/_handoff.py::feedback`
  normalizes observations with
  `src/gobby/sessions/handoff.py::normalize_feedback_observations`. Its task
  and claimant resolution read task rows (ruling C2.3); native resolves them
  through gtasks' `refs.rs` `resolve_read`, whose row carries the claimant
  (Decision 7). It writes them with `handoff.py::write_feedback_batch` and merges
  `_gobby_feedback_epoch_submitted` into the caller's variables in the same
  transaction, through 2.4's variable service.
- These are MCP-only rows; 5.1 publishes the descriptors.

**Acceptance:**

- 2.6.1 - The `hold_session_claims` and `release_session_claims_hold` services
  match authorization, held-by-other and release cases.
  test: `crates/gsessions/tests/claim_hold.rs::claim_hold_services_match_python`.
- 2.6.2 - The `feedback` service matches normalization, invalid-feedback
  errors, written rows and the variable merge, all in one transaction, and
  resolves tasks and claimants only through gtasks.
  test: `crates/gsessions/tests/claim_hold.rs::feedback_service_matches_python`.

Planned verification: the `claim_hold` test binary with the 1.1 DB prefix.
Nothing has run during drafting.

### 2.7 Read-only queries and cross-project listing [category: code] (depends: 2.6)
`kind: deliverable`

Targets:
- `crates/gsessions/src/lib.rs`
- `crates/gsessions/src/readonly_query.rs`
- `crates/gsessions/tests/readonly_query.rs`

**Research context:**
- `src/gobby/mcp_proxy/tools/sessions/_readonly_query.py::query_readonly`
  accepts one statement (`_single_statement`), applies static and catalog-name
  refusals (`_static_refusal`, `_name_refusal`, `_load_catalog`) and runs it in
  `_run` under `SET ROLE gobby_query_readonly` (migration 482) with a row cap
  and a statement timeout.
- `src/gobby/mcp_proxy/tools/hub.py::list_cross_project_sessions` lists
  sessions across projects on the hub. Native takes project names from
  1.1's gprojects `project_names` (Decision 7).
- `query_readonly` runs the caller's own SQL under the read-only role. It is
  a query surface over the whole schema, so its catalog refusals stay and no
  owner API applies.
- Both are MCP-only rows; 5.1 publishes the descriptors.

**Acceptance:**

- 2.7.1 - The `query_readonly` service matches results, the row cap, the
  timeout and every refusal class, and a write attempt fails under the
  read-only role. test: `crates/gsessions/tests/readonly_query.rs::readonly_query_matches_python`.
- 2.7.2 - The `list_cross_project_sessions` service matches the corpus.
  test: `crates/gsessions/tests/readonly_query.rs::cross_project_listing_matches_python`.

Planned verification: the `readonly_query` test binary with the 1.1 DB prefix.
Nothing has run during drafting.

### 2.8 Handoff reads [category: code] (depends: 2.7)
`kind: deliverable`

Targets:
- `crates/gsessions/src/lib.rs`
- `crates/gsessions/src/handoff_read.rs`
- `crates/gsessions/tests/handoff_read.rs`

**Research context:**
- `src/gobby/mcp_proxy/tools/sessions/_handoff.py::get_handoff` has four
  modes:
  - With no argument it consumes the caller's one pending compact or clear
    handoff through `src/gobby/sessions/handoff.py::consume_pending_handoff`,
    honouring the dispatch gate, the failed-handoff marker and the turn-end
    pending marker in session variables.
  - `agent_run_id` idempotently reads a child run's `agent_end` handoff
    (`src/gobby/sessions/handoff_records.py::get_agent_end_handoff`) for its
    parent or that parent's clear successor
    (`src/gobby/sessions/clear_continuation.py::resolve_clear_successor`).
  - `failed_attempt_id` reads an undelivered failed handoff without delivery
    (`handoff.py::recover_failed_handoff`).
  - `reconcile_late_compact=true` delivers only after a matching, timely
    compact notification
    (`src/gobby/sessions/handoff_reconciliation.py::reconcile_late_compact_handoff`,
    including `is_legacy_codex_ambiguous_enter_failure`).
- Late-compact reconciliation scans the Codex rollout through
  `src/gobby/sessions/transcript_cursor.py::codex_compact_boundary_between`, a
  pool-free read of one local file. This leaf ports it into
  `handoff_read.rs` and runs it in-process when the serving machine owns the
  session. A remote owner answers through 3.8's `transcript.compact_boundary`
  command, which 3.9 serves; until 3.9 lands, a remote owner gets the explicit
  owner-unavailable refusal. The family is passive until 7.2, so no production
  caller sees that interim refusal.
- Python `set_handoff` keeps staging handoffs until 8.1, so the native reader
  reads the same rows and variables; the corpus covers each mode.
- Agent runs: the `agent_run_id` mode's run lookup and its descendant walk
  (depth below 5) read `agent_runs` through the gagents port's `run_rows`
  (Decision 7, `agents-family 3.6`).
- MCP-only row; 5.1 publishes the descriptor.

**Granularity:** One reader with four modes over one marker state machine;
staging stays in 8.1 and delivery in 8.2, 8.3 and 8.6.

**Acceptance:**

- 2.8.1 - The no-argument read consumes exactly one pending handoff, and a
  second read finds none. test: `crates/gsessions/tests/handoff_read.rs::pending_handoff_is_consumed_once`.
- 2.8.2 - `agent_run_id` reads are idempotent for the parent and its clear
  successor and refused for any other caller, with runs read through a
  fixture gagents port adapter.
  test: `crates/gsessions/tests/handoff_read.rs::agent_end_handoff_reads_match_python`.
- 2.8.3 - `failed_attempt_id` and `reconcile_late_compact` match the corpus,
  including late, untimely and mismatched notifications.
  test: `crates/gsessions/tests/handoff_read.rs::failed_and_late_handoffs_match_python`.
- 2.8.4 - The local compact-boundary scan returns the corpus boundaries,
  including a wrong external id, a non-regular file and records outside the
  window. test: `crates/gsessions/tests/handoff_read.rs::codex_compact_boundary_matches_corpus`.

Planned verification: the `handoff_read` test binary with the 1.1 DB prefix.
Nothing has run during drafting.

### 2.9 Hook seam session services [category: code] (depends: 2.8)
`kind: deliverable`

Targets:
- `crates/gsessions/src/lib.rs`
- `crates/gsessions/src/hook_rows.rs`
- `crates/gsessions/src/clear_lineage.rs`
- `crates/gsessions/tests/hook_rows.rs`
- `crates/gterminals/src/lib.rs::*` — scope-reason: declare the session-link module that holds the narrow terminal functions Sessions calls
- `crates/gterminals/src/session_links.rs`

**Research context:**
- Ruling C5 (a): the hooks plan's handlers reach session rows through
  `src/gobby/hooks/session_types.py::HookSessionManager`, implemented by
  `src/gobby/storage/sessions/_manager.py::SessionManager`. This leaf exports
  native services for every protocol method except the two cache helpers,
  `HookSessionManager.get_cached_session` and
  `HookSessionManager.cache_session_mapping`. Native callers validate each
  read against the row, as Python's
  `src/gobby/storage/sessions/_registration_cache.py` already re-reads it, so
  no cache is ported.
- Methods 1.1 and 2.2 already serve (`get`, `list`,
  `resolve_session_reference`, `update_title`, `update_summary`,
  `update_session_status`) are re-exported, never duplicated. The new services
  port:
  - registration and lookup: `register_session`, `get_session_id`,
    `lookup_session_id`, `find_by_external_id` and
    `find_by_external_id_any_project` (`_registration_cache.py`),
    `recover_session` (`src/gobby/storage/sessions/_registration_recovery.py`),
    `find_by_terminal_identity`, `find_parent` and
    `find_live_interactive_pane_owner`
    (`src/gobby/storage/sessions/_discovery.py`);
  - status: `update_status`, `update_status_if_non_terminal`
    (`src/gobby/storage/sessions/_field_update.py`), `mark_session_expired`
    with its `ContestedExpiryCause`, and `revive_expired_terminal_session`
    (`src/gobby/storage/sessions/_terminal_revival.py`);
  - terminal binding: `backfill_terminal_context`,
    `bind_native_clear_successor`, `rebind_resumed_terminal_session` and
    `update_terminal_pickup_metadata` (`src/gobby/storage/sessions/_terminal.py`;
    callers `src/gobby/hooks/event_handlers/_session_start/handoff.py` and
    `src/gobby/hooks/session_activation.py`);
  - counters: `update`, `update_usage`, `update_context_usage`
    (`src/gobby/storage/sessions/_usage.py`), `mark_had_edits` and
    `reset_transcript_processed`.
- Clear-run lineage: 2.4 ported
  `src/gobby/sessions/clear_run_lineage.py::current_run_session_id` into
  `clear_lineage.rs`; this leaf adds `clear_run_lineage.py::earlier_clear_session_id`
  and `clear_run_lineage.py::move_clear_run_lineage`, and exports all three.
  8.3's clear path calls the same functions.
- `move_clear_run_lineage` writes three owners' tables in one transaction.
  Native keeps that atomicity by passing the caller's transaction to each
  owner (Decision 7):
  - the two `agent_runs` updates go through the gagents port's
    `move_run_lineage(tx, session_ids, successor_id)`, which returns the
    moved run ids;
  - the `terminals` update (pending and live rows of each moved run) goes
    through `rebind_run_terminals(tx, run_id, successor_id)`, a narrow
    function this leaf adds to `crates/gterminals/src/session_links.rs`;
  - the `sessions` updates stay in `clear_lineage.rs`.
- Every status change goes through 2.2's notifying write path, and every
  committed write publishes through 2.1's event sink.
- Receipts and staged effects stay with the hooks plan's 3.4. The hooks plan
  composes these services at its native composition leaf,
  `s2-11-hub-hooks-rules 7.1` (`.gobby/plans/s2-11-hub-hooks-rules.md`), a
  prose cross-plan edge the Orchestrator converts at expansion.
- No ledger rows: these services have no transport. Parity cases are
  transcribed from the Python storage tests
  `tests/storage/sessions/test_contested_expiry.py`,
  `tests/storage/sessions/test_register_fallback.py`,
  `tests/storage/sessions/test_session_tmux_runtime.py` and
  `tests/storage/sessions/test_lifecycle_expiry.py`, with the same inputs and
  expected rows.

**Granularity:** One row-service module over one table plus the lineage
helpers; the protocol is a single seam with one consumer, so splitting it
would only spread the shared row lock and event path.

**Acceptance:**

- 2.9.1 - Registration and lookup services return the rows the Python storage
  tests expect, including recovery and cross-project external-id lookup.
  test: `crates/gsessions/tests/hook_rows.rs::registration_and_lookup_match_storage`.
- 2.9.2 - Status services match contested expiry, non-terminal-only updates
  and terminal revival, and each change notifies once.
  test: `crates/gsessions/tests/hook_rows.rs::status_services_match_storage`.
- 2.9.3 - Terminal binding services match clear-successor binding, resume
  rebinding, context backfill and pickup metadata.
  test: `crates/gsessions/tests/hook_rows.rs::terminal_binding_matches_storage`.
- 2.9.4 - Usage, context-usage, edit and transcript-reset counters match the
  storage results. test: `crates/gsessions/tests/hook_rows.rs::counter_updates_match_storage`.
- 2.9.5 - Clear-run lineage returns the current and earlier run sessions and
  moves lineage exactly as `clear_run_lineage.py` does, writing runs through
  the gagents port and terminals through `rebind_run_terminals` in one
  transaction; a failure in any owner's write rolls back all three.
  test: `crates/gsessions/tests/hook_rows.rs::clear_run_lineage_matches_python`.

Planned verification: the `hook_rows` test binary with the 1.1 DB prefix.
Nothing has run during drafting.

## P3: Machine-local transcripts
`kind: framing`

Transcript files live on the machine that ran the session. 3.1–3.7 port the
transcript stack as pool-free functions that take a path, the session's source
and external id, and an archive root, so the hub runs them in-process for its
own sessions and a node runs the same code in 3.9. Every transcript function
is synchronous; callers run it through `tokio::task::spawn_blocking` behind one
family-wide semaphore, never on the request executor (memory 58242391:
transcripts reach 86 MB). Services route by owner: the serving machine reads
in-process, keeping the exact-machine check of
`src/gobby/sessions/machine_scope.py::require_local_session_ownership`, and a
remote owner is reached through `OwnerChannel` (1.3). Until 3.8 maps the
transcript commands onto the wire, the gdaemon adapter answers remote
transcript requests with the explicit owner-unavailable refusal; tests use a
doubled channel. 3.8 adds the commands to the node-commands contract, 3.9
serves them on a node and 3.10 ports the HTTP routes.

### 3.1 Transcript sources, archives and the gzip seek index [category: code] (depends: 2.9)
`kind: deliverable`

Targets:
- `Cargo.lock`
- `crates/gsessions/Cargo.toml`
- `crates/gsessions/src/lib.rs`
- `crates/gsessions/src/transcript_paths.rs`
- `crates/gsessions/src/transcript_source.rs`
- `crates/gsessions/src/transcript_archive.rs`
- `crates/gsessions/src/gzip_seek.rs`
- `crates/gsessions/tests/transcript_sources.rs`

**Research context:**
- Python sources:
  - `src/gobby/sessions/transcript_paths.py::classify_transcript_path`,
    `transcript_paths.py::find_transcript_on_disk`,
    `transcript_paths.py::find_supplemental_transcripts_on_disk` and the
    per-provider rules in `transcript_paths.py::PROVIDER_TRANSCRIPT_SPECS`
    (Claude subagent transcripts, Droid parent epochs, Codex, Grok and AGY
    recovery).
  - `src/gobby/sessions/transcript_source.py::_resolve_effective_source` and
    `src/gobby/sessions/transcript_io.py::_iter_archive_lines` with its
    `DecompressionError`.
  - `src/gobby/sessions/transcript_archive.py::get_archive_dir`,
    `transcript_archive.py::backup_transcript` and
    `transcript_archive.py::restore_transcript`.
  - `src/gobby/sessions/gzip_seek_index.py::write_blocked_gzip_archive`,
    `gzip_seek_index.py::ensure_gzip_block_index`,
    `gzip_seek_index.py::iter_gzip_block_raw_lines` and
    `GZIP_BLOCK_INDEX_SCHEMA_VERSION`.
  - Limits: `src/gobby/sessions/transcript_limits.py::RENDERED_LIMIT_MAX` and
    `transcript_limits.py::FLAT_ROW_LIMIT_MAX`.
- Compatibility: Python processing owns archives on each machine until 7.2,
  so native archives and seek indexes keep Python's byte format and schema
  version, and each implementation reads the other's files. The 0.2 corpus
  carries Python-written archives.
- Archive root: hub and standalone read `session_lifecycle.transcript_archive_dir`
  from `config_store` through the pool, as
  `crates/gterminals/src/host.rs::HostOptions::from_pool` reads its keys; unset
  means `<GOBBY_HOME>/session_transcripts`. The key is not machine-exported
  (`machineExport: false` in
  `crates/gcore/assets/config/runtime_config_contract.json`), and a native node
  reads no hub config, so a node always uses its own
  `<GOBBY_HOME>/session_transcripts`.
- Gzip: `flate2` 1.1.9 is already locked; this leaf adds it as a direct
  dependency (Decision Record item 13). The seek index reads one member per
  block, so a seek never decompresses from the start.

**Acceptance:**

- 3.1.1 - Path classification and on-disk discovery match the corpus for all
  five providers over the fixture home, including Claude subagents and Droid
  parent epochs. test: `crates/gsessions/tests/transcript_sources.rs::provider_paths_match_corpus`.
- 3.1.2 - Effective source detection from a path and its first records matches
  the corpus. test: `crates/gsessions/tests/transcript_sources.rs::source_detection_matches_corpus`.
- 3.1.3 - Native backup writes a blocked archive and seek index that are
  byte-identical to the corpus's Python-written pair, and native reads the
  Python pair line-for-line.
  test: `crates/gsessions/tests/transcript_sources.rs::archives_share_python_format`.
- 3.1.4 - Restore matches Python for a present archive, a missing archive and
  an existing live file. test: `crates/gsessions/tests/transcript_sources.rs::restore_matches_python`.
- 3.1.5 - Seeking by offset returns the same raw lines as a full scan, and a
  corrupt or stale index is rebuilt.
  test: `crates/gsessions/tests/transcript_sources.rs::seek_index_matches_full_scan`.

Planned verification:
`CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-sessions --test transcript_sources`;
`cargo clippy -p gobby-sessions`. Nothing has run during drafting.

### 3.2 Parser base, Claude and Codex parsers [category: code] (depends: 3.1)
`kind: deliverable`

Targets:
- `crates/gsessions/src/lib.rs`
- `crates/gsessions/src/parser.rs`
- `crates/gsessions/src/protocol.rs`
- `crates/gsessions/src/tool_activity.rs`
- `crates/gsessions/src/claude.rs`
- `crates/gsessions/src/claude_records.rs`
- `crates/gsessions/src/codex.rs`
- `crates/gsessions/src/codex_items.rs`
- `crates/gsessions/tests/parsers_claude_codex.rs`

**Research context:**
- Base: `src/gobby/sessions/transcripts/base.py::BaseTranscriptParser`,
  `base.py::TranscriptParser`, `base.py::ParsedMessage`, `base.py::TokenUsage`,
  `base.py::ParseEvent`, `base.py::ParsedToolEvent`, `base.py::RawLine`,
  `base.py::decode_transcript_record` and the source registry
  `src/gobby/sessions/transcripts/__init__.py::get_parser`. Shared decoding
  lives in `src/gobby/sessions/transcript_protocol.py`,
  `src/gobby/sessions/transcript_parsing.py` and
  `src/gobby/sessions/transcript_tool_metadata.py::classify_tool` with
  `transcript_tool_metadata.py::extract_result_metadata`.
- Providers: `src/gobby/sessions/transcripts/claude.py::ClaudeTranscriptParser`
  (817 lines) with `claude_records.py`; `codex.py::CodexTranscriptParser`
  (882 lines) with `codex_items.py`, `codex_file_changes.py` and
  `codex.py::CodexNestedExecOutcome`. Each Rust provider is two files so both
  stay under the 1,000-line ceiling.
- Tool activity: `src/gobby/sessions/transcripts/tool_activity.py` (canonical
  tool names, commit-producing detection, ledger rendering,
  `tool_activity.py::render_tool_activity`).
- `parser.rs` holds the parser trait and the source registry; five providers
  implement it. A malformed record is logged and skipped exactly where Python
  skips it.

**Granularity:** The parser base, the shared protocol and tool-activity code
and the two providers that depend most on them form one testable outcome. The
production-file count passes six because each large provider needs two files;
Droid, Grok and AGY follow in 3.3.

**Acceptance:**

- 3.2.1 - Claude fixtures produce the corpus parsed records, token usage, tool
  events and adjustments. test: `crates/gsessions/tests/parsers_claude_codex.rs::claude_parser_matches_corpus`.
- 3.2.2 - Codex fixtures, including nested exec outcomes and file changes,
  produce the corpus results. test: `crates/gsessions/tests/parsers_claude_codex.rs::codex_parser_matches_corpus`.
- 3.2.3 - Tool classification, result metadata and tool-activity rendering
  match the corpus. test: `crates/gsessions/tests/parsers_claude_codex.rs::tool_activity_matches_corpus`.
- 3.2.4 - Unicode line separators inside strings, a truncated final line and
  unknown record types decode as Python decodes them.
  test: `crates/gsessions/tests/parsers_claude_codex.rs::record_decoding_edges_match_corpus`.

Planned verification: the `parsers_claude_codex` test binary; `cargo clippy
-p gobby-sessions`. Nothing has run during drafting.

### 3.3 Droid, Grok and AGY parsers [category: code] (depends: 3.2)
`kind: deliverable`

Targets:
- `crates/gsessions/src/lib.rs`
- `crates/gsessions/src/parser.rs`
- `crates/gsessions/src/droid.rs`
- `crates/gsessions/src/grok.rs`
- `crates/gsessions/src/agy.rs`
- `crates/gsessions/tests/parsers_droid_grok_agy.rs`

**Research context:** `src/gobby/sessions/transcripts/droid.py::DroidTranscriptParser`
(with `droid.py::droid_transcript_has_tool_id` and
`droid.py::droid_transcript_confirms_turn_cancellation`),
`src/gobby/sessions/transcripts/grok.py::GrokTranscriptParser` and
`src/gobby/sessions/transcripts/agy.py::AgyTranscriptParser` (with
`agy.py::read_current_user_prompt`) implement 3.2's trait. `parser.rs` gains
their registry entries, so an unknown source returns the corpus error.

**Acceptance:**

- 3.3.1 - Droid fixtures match the corpus, including tool-id lookups and
  confirmed turn cancellation. test: `crates/gsessions/tests/parsers_droid_grok_agy.rs::droid_parser_matches_corpus`.
- 3.3.2 - Grok fixtures match the corpus.
  test: `crates/gsessions/tests/parsers_droid_grok_agy.rs::grok_parser_matches_corpus`.
- 3.3.3 - AGY fixtures and the current-prompt read match the corpus.
  test: `crates/gsessions/tests/parsers_droid_grok_agy.rs::agy_parser_matches_corpus`.
- 3.3.4 - The registry returns the right parser for each of the five sources
  and the corpus error for an unknown one.
  test: `crates/gsessions/tests/parsers_droid_grok_agy.rs::parser_registry_matches_python`.

Planned verification: the `parsers_droid_grok_agy` test binary. Nothing has
run during drafting.

### 3.4 Normalization, rendering, statistics and observations [category: code] (depends: 3.3)
`kind: deliverable`

Targets:
- `crates/gsessions/src/lib.rs`
- `crates/gsessions/src/normalize.rs`
- `crates/gsessions/src/render.rs`
- `crates/gsessions/src/render_blocks.rs`
- `crates/gsessions/src/message_stats.rs`
- `crates/gsessions/src/observations.rs`
- `crates/gsessions/tests/render.rs`

**Research context:**
- `src/gobby/sessions/transcript_normalization.py::normalize_transcript_records`;
  `src/gobby/sessions/transcript_renderer.py::render_transcript` and
  `transcript_renderer.py::render_incremental`;
  `src/gobby/sessions/transcript_render_blocks.py`; the models in
  `src/gobby/sessions/transcript_render_models.py` (`RenderedMessage`,
  `RenderState`, `ContentBlock`, `RenderedToolCall`, `ToolResult`).
- Statistics: `src/gobby/sessions/message_stats.py::compute_message_stats`,
  `message_stats.py::merge_message_stats`,
  `message_stats.py::accumulate_message_stats` and
  `message_stats.py::empty_message_stats`.
- Observations: rendering reports unknown block types and tool names through
  `src/gobby/sessions/observation_tracker.py::ObservationTracker`, which
  `src/gobby/storage/unmodeled_observations.py::UnmodeledObservationStore`
  persists into `unmodeled_observations` and `unmodeled_observation_events`
  with bounded keys, sizes and depth and sensitive-key redaction. Native
  rendering collects the same bounded observations in memory. A caller holding
  the pool persists them through `observations.rs` with Python's bounds and
  redaction; a node returns them in its reply (3.8) and the hub persists them.

**Acceptance:**

- 3.4.1 - Normalized records match the corpus for every provider fixture.
  test: `crates/gsessions/tests/render.rs::normalization_matches_corpus`.
- 3.4.2 - Full and incremental rendering produce the corpus messages, blocks
  and tool calls, and incremental output equals full output.
  test: `crates/gsessions/tests/render.rs::rendering_matches_corpus`.
- 3.4.3 - Message statistics, including merge and accumulation, match the
  corpus. test: `crates/gsessions/tests/render.rs::message_stats_match_corpus`.
- 3.4.4 - Unknown block types and tool names produce the corpus observations,
  and persistence applies Python's bounds and redaction.
  test: `crates/gsessions/tests/render.rs::observations_match_python`.

Planned verification: the `render` test binary with the 1.1 DB prefix for the
observation store. Nothing has run during drafting.

### 3.5 Transcript index, sidecar and resume [category: code] (depends: 3.4)
`kind: deliverable`

Targets:
- `Cargo.lock`
- `crates/gsessions/Cargo.toml`
- `crates/gsessions/src/lib.rs`
- `crates/gsessions/src/index.rs`
- `crates/gsessions/src/index_sidecar.rs`
- `crates/gsessions/src/index_sidecar_codec.rs`
- `crates/gsessions/src/index_resume.rs`
- `crates/gsessions/tests/index.rs`

**Research context:**
- `src/gobby/sessions/transcript_index.py::TranscriptIndex`,
  `transcript_index.py::TranscriptIndexAppender`,
  `transcript_index.py::build_index_from_file`,
  `transcript_index.py::build_index_from_raw_lines`,
  `transcript_index.py::rebuild_and_persist_index` and
  `transcript_index.py::detect_source_bounded`.
- Sidecar: `src/gobby/sessions/transcript_index_sidecar.py` (998 lines) writes
  schema `INDEX_SCHEMA_VERSION` 6 to
  `<GOBBY_HOME>/cache/transcript-indexes/<sha256 of the absolute path>.gobby-index.json`
  (`transcript_index_sidecar.py::_sidecar_path`) under an exclusive `flock`
  on `<sidecar>.lock` (`transcript_index_sidecar.py::_sidecar_lock`). Entry
  points: `transcript_index_sidecar.py::load_index_sidecar`,
  `transcript_index_sidecar.py::persist_index_sidecar`,
  `transcript_index_sidecar.py::get_or_build_index` and
  `transcript_index_sidecar.py::discard_index_sidecar`. Native code uses the
  same path, schema version and lock, so Python and native share sidecars on
  one machine during the switch. The Rust port is two files: state and
  locking in `index_sidecar.rs`, payload encoding in `index_sidecar_codec.rs`.
- Locking uses `libc::flock`, as `crates/gdaemon/src/nodes/hooks/ledger.rs`
  does; `libc` is already locked and becomes a direct dependency here.
- Resume: `src/gobby/sessions/transcript_index_resume.py::hydrate_appender_from_index`
  and `transcript_index_resume.py::extend_index_from_file` extend an index
  from the last indexed offset.

**Acceptance:**

- 3.5.1 - Indexes built from the corpus fixtures match the recorded group
  boundaries, adjustments and activity counts.
  test: `crates/gsessions/tests/index.rs::index_matches_corpus`.
- 3.5.2 - Native and Python read each other's sidecars at schema 6, and a
  mismatched schema or source snapshot is rebuilt.
  test: `crates/gsessions/tests/index.rs::sidecars_are_shared_with_python`.
- 3.5.3 - Two concurrent writers serialize on the sidecar lock and leave one
  valid sidecar. test: `crates/gsessions/tests/index.rs::sidecar_writes_serialize`.
- 3.5.4 - Extending an index from its last offset equals rebuilding it from
  the whole file. test: `crates/gsessions/tests/index.rs::resume_equals_rebuild`.

Planned verification: the `index` test binary. Nothing has run during
drafting.

### 3.6 Transcript status and restore services [category: code] (depends: 3.5)
`kind: deliverable`

Targets:
- `crates/gsessions/src/lib.rs`
- `crates/gsessions/src/owner.rs`
- `crates/gsessions/src/transcript_status.rs`
- `crates/gsessions/tests/transcript_status.rs`

**Research context:**
- `src/gobby/sessions/transcript_status.py::get_transcript_status_for_session`
  with its availability, content-state and count helpers;
  `src/gobby/sessions/transcript_reader.py::TranscriptReader.get_transcript_status`.
- W1: the HTTP route `src/gobby/servers/routes/sessions/messages.py::transcript_status`
  returns the rich shape with its fallback, and
  `src/gobby/mcp_proxy/tools/sessions/_transcripts.py::get_transcript_status`
  returns the archive-only shape. One native status service feeds both; each
  transport keeps its shape.
- Restore: `_transcripts.py::restore_session_transcript` calls 3.1's restore on
  the owning machine and keeps the exact-machine check.
- `owner.rs` gains the transcript request and reply types for status and
  restore.

**Acceptance:**

- 3.6.1 - The status service yields the HTTP shape for live, archived,
  missing and partial transcripts as the corpus records.
  test: `crates/gsessions/tests/transcript_status.rs::http_status_shape_matches_corpus`.
- 3.6.2 - The `get_transcript_status` service yields the MCP archive-only
  shape from the same status. test: `crates/gsessions/tests/transcript_status.rs::mcp_status_shape_matches_corpus`.
- 3.6.3 - The `restore_session_transcript` service matches the corpus for
  local owners and reaches a remote owner through the doubled channel.
  test: `crates/gsessions/tests/transcript_status.rs::restore_service_matches_python`.

Planned verification: the `transcript_status` test binary with the 1.1 DB
prefix. Nothing has run during drafting.

### 3.7 Rendered windows, the reader and message search [category: code] (depends: 3.6)
`kind: deliverable`

Targets:
- `crates/gsessions/src/lib.rs`
- `crates/gsessions/src/owner.rs`
- `crates/gsessions/src/window.rs`
- `crates/gsessions/src/transcript_reader.rs`
- `crates/gsessions/src/search.rs`
- `crates/gsessions/tests/messages.rs`

**Research context:**
- Windows: `src/gobby/sessions/transcript_window.py::render_window` and
  `transcript_window.py::WindowResult` page rendered groups by index boundary
  in head or tail order.
- Reader: `src/gobby/sessions/transcript_reader.py::TranscriptReader` methods
  `get_messages`, `get_rendered_window`, `iter_rendered_windows`,
  `get_rendered_messages`, `count_messages`, `get_activity_counts` and
  `get_summary_records`, with live-path resolution and archive fallback in
  `TranscriptReader._resolve_windowable`. Summary records use
  `src/gobby/sessions/summary_transcripts.py::_read_transcript_window`.
- Search: `src/gobby/sessions/transcript_search.py::search_rendered_messages`.
  `src/gobby/mcp_proxy/tools/sessions/_messages.py::search_session_messages`
  scans one or many sessions (project, status and source filters) within
  `SEARCH_GROUP_BUDGET` rendered groups per call and returns an opaque
  `(session, group)` cursor (`_messages.py::_encode_cursor`,
  `_messages.py::_decode_cursor`). A remote session is searched on its owner
  with the remaining budget; the cursor format is unchanged.
- `_messages.py::get_session_messages` returns windowed rendered messages.
  `src/gobby/sessions/transcript_search_timing.py` is debug timing; native code
  records the same stages as `tracing` spans.
- `owner.rs` gains the raw-read, window and search request and reply types.

**Acceptance:**

- 3.7.1 - The `get_session_messages` service returns the corpus pages in head
  and tail order, with cursors, limits and the too-large error.
  test: `crates/gsessions/tests/messages.rs::message_windows_match_corpus`.
- 3.7.2 - The `search_session_messages` service matches single- and
  multi-session searches, budget truncation, cursor resume and the
  wrong-session cursor error. test: `crates/gsessions/tests/messages.rs::message_search_matches_python`.
- 3.7.3 - Activity counts and summary records match the corpus.
  test: `crates/gsessions/tests/messages.rs::reader_counts_and_summary_records_match`.
- 3.7.4 - A remote owner reached through the doubled channel returns
  identical pages and hits, and an offline owner returns the explicit refusal.
  test: `crates/gsessions/tests/messages.rs::remote_owner_reads_match_local`.

Planned verification: the `messages` test binary with the 1.1 DB prefix.
Nothing has run during drafting.

### 3.8 Transcript commands in the node-commands contract [category: code] (depends: 3.7, #21566)
`kind: deliverable`

Targets:
- `crates/gdaemon/src/node_commands.rs::*` — scope-reason: declare the transcript module, add one CommandFrame arm per transcript command and one ReplyResult arm per reply, and set CONTRACT_VERSION to the next version after #21566's
- `crates/gdaemon/src/node_commands/transcript.rs`
- `crates/gdaemon/src/node_commands/frame_dispatch.rs::command_frame`
- `crates/gdaemon/src/nodes/registry.rs::CommandReceiver::recv`
- `crates/gdaemon/tests/fixtures/node_commands/corpus.json::*` — scope-reason: add the seven transcript commands, their replies and malformed messages
- `crates/gdaemon/tests/node_command_parity.rs::*` — scope-reason: add the new version's transcript message list and round-trip test
- `crates/gdaemon/tests/node_internal_route.rs::*` — scope-reason: malformed transcript commands send nothing
- `src/gobby/hub/node_commands.py::*` — scope-reason: set CONTRACT_VERSION to the new version and import the transcript command and result models
- `src/gobby/hub/node_command_transcripts.py`
- `tests/hub/test_node_command_parity.py::*` — scope-reason: add the new version's transcript message list and round-trip test
- `tests/hub/test_node_command_corpus.py::*` — scope-reason: round-trip the new corpus messages
- `docs/contracts/node-commands.md`
- `crates/gsessions/src/owner.rs`
- `crates/gsessions/src/handoff_read.rs`
- `crates/gdaemon/src/front_door/sessions.rs`
- `crates/gdaemon/tests/sessions_owner_commands.rs`

**Research context:**
- Version: Orchestrator ruling S2. #25207 is version 5 and #21566 is version 6;
  this leaf adds its commands in the next version after #21566's and orders
  after `external-mcp-transport-multiplexer 3.1`
  (`.gobby/plans/external-mcp-transport-multiplexer.md` at `87d4d08d09`), a
  prose cross-plan edge the Orchestrator converts at expansion. That leaf is
  the carrier template. No crate version changes here; 7.2 is the shipping
  commit.
- Split: `crates/gdaemon/src/node_commands.rs` is 979 lines with no
  `#[cfg(test)]` module. The transcript command and reply types live in the
  new `crates/gdaemon/src/node_commands/transcript.rs`, and
  `node_commands.rs` gains only the module declaration and one enum arm per
  command and reply, as `node_commands/checkout_read.rs` does. Python models
  likewise live in the new `src/gobby/hub/node_command_transcripts.py`, so
  `src/gobby/hub/node_commands.py` (601 lines) gains only imports.
- Commands, each naming the session's source, external id and transcript path
  from the hub row:
  - `transcript.read`: a raw byte range (offset, length).
  - `transcript.window`: 3.7's rendered window request.
  - `transcript.search`: one session's search with the remaining group budget.
  - `transcript.status`: 3.6's status.
  - `transcript.restore`: 3.1's restore (the only mutating command).
  - `transcript.compact_boundary`: 2.8's Codex compact-boundary scan.
  - `transcript.process`: one page of parse results from an offset (message
    statistics, token events, unmodeled observations, the next offset and the
    source hash), backing up the archive on the node when the page reaches
    the end. 4.2's hub job consumes it.
- Every reply that would exceed 8 MiB
  (`crates/gdaemon/src/node_commands/checkout_read.rs` `REPLY_BUDGET`) becomes
  `result_too_large`; ranged reads and process pages size themselves under it.
  Channel errors keep their existing codes and stages. Each reply has a
  required key no other `ReplyResult` arm uses, so the untagged enum stays
  unambiguous.
- Hub side: `front_door/sessions.rs` maps `owner.rs`'s transcript requests
  onto these frames through
  `crates/gdaemon/src/nodes/registry.rs::CommandSender::request`, replacing
  the interim owner-unavailable answer. `handoff_read.rs` sends
  `transcript.compact_boundary` for a remote owner. A node connected at an
  older contract version gets the explicit owner-unavailable refusal.

**Granularity:** The contract carriers are one versioned wire change that
must land together; the hub mapping is its only consumer. Splitting by
command would bump the version once per leaf.

**Acceptance:**

- 3.8.1 - The seven transcript commands and their replies round-trip
  byte-identically in Rust at the new version.
  test: `crates/gdaemon/tests/node_command_parity.rs::transcript_commands_round_trip`.
- 3.8.2 - The same corpus messages round-trip through the Python models.
  test: `tests/hub/test_node_command_parity.py::test_transcript_commands_round_trip`.
- 3.8.3 - A malformed transcript command (unknown field, missing path) sends
  nothing to the node. test: `crates/gdaemon/tests/node_internal_route.rs::malformed_transcript_commands_send_nothing`.
- 3.8.4 - The hub adapter maps each owner request onto its frame, maps an
  oversized reply to `result_too_large` and refuses a node at an older
  version. test: `crates/gdaemon/tests/sessions_owner_commands.rs::owner_requests_map_to_transcript_frames`.
- 3.8.5 - `node_commands.rs` stays under 1,000 lines with the transcript types
  in their own module. file: `crates/gdaemon/src/node_commands/transcript.rs`.
- 3.8.6 - The contract documents the seven commands, the reply budget and the
  error mapping. behavior: "transcript.process" in `docs/contracts/node-commands.md`.

Planned verification:
`cargo nextest run -p gobby-daemon -E 'test(node_command) | binary(node_internal_route) | binary(sessions_owner_commands)'`
and the two Python parity tests with
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest`.
Nothing has run during drafting.

### 3.9 Node transcript command handler [category: code] (depends: 3.8, #24376)
`kind: deliverable`

Targets:
- `crates/gdaemon/src/nodes/agents/commands.rs`
- `crates/gdaemon/src/nodes/transcripts.rs`
- `crates/gdaemon/src/nodes/mod.rs::*` — scope-reason: declare the node transcript handler module
- `crates/gdaemon/tests/node_transcripts.rs`

**Research context:**
- #24376 creates `crates/gdaemon/src/nodes/agents/commands.rs::dispatch` and
  routes each command frame there; the multiplexer's 3.2 is the precedent for
  a non-run command. `dispatch` sends each `transcript.*` frame to
  `nodes/transcripts.rs`, which calls 3.1–3.7's pool-free functions on the
  blocking pool behind the shared semaphore. Transcript commands name no run,
  so they bypass the per-run queue.
- Path boundary: the node canonicalizes the requested path and serves it only
  when it lies under one of its provider transcript roots (3.1's provider
  specs over the node's home) or its `<GOBBY_HOME>/session_transcripts`
  archive root; anything else returns `forbidden`. A native node holds no
  database credential, so `transcript.process` returns parse results and the
  hub writes every row.
- Processing owner (Decision Record item 5): the handler receives the node's
  bootstrap `front_door.routes` selection for `sessions` when `nodes/mod.rs`
  builds it. Unless that selection is Native, `transcript.process` returns
  the explicit owner-unavailable refusal, so the node's Python runner stays
  the single processing owner; the read commands are unaffected.

**Acceptance:**

- 3.9.1 - Each read command returns the same result the hub computes locally
  for the same fixture file. test: `crates/gdaemon/tests/node_transcripts.rs::node_reads_match_local_results`.
- 3.9.2 - `transcript.process` pages a large fixture under the reply budget,
  and the concatenated pages equal one local parse.
  test: `crates/gdaemon/tests/node_transcripts.rs::process_pages_equal_local_parse`.
- 3.9.3 - A path outside the provider roots and the archive root, including
  one reached through a symlink, returns `forbidden`.
  test: `crates/gdaemon/tests/node_transcripts.rs::paths_outside_roots_are_forbidden`.
- 3.9.4 - `transcript.restore` restores on the node, and transcript commands do
  not wait behind a busy run's queue.
  test: `crates/gdaemon/tests/node_transcripts.rs::restore_runs_outside_run_queues`.
- 3.9.5 - A node whose bootstrap does not select Native for `sessions`
  refuses `transcript.process` and still serves the read commands.
  test: `crates/gdaemon/tests/node_transcripts.rs::process_requires_native_selection`.

Planned verification:
`cargo nextest run -p gobby-daemon --test node_transcripts`. Nothing has run
during drafting.

### 3.10 HTTP transcript routes [category: code] (depends: 3.9)
`kind: deliverable`

Targets:
- `crates/gsessions/src/lib.rs`
- `crates/gsessions/src/family.rs`
- `crates/gsessions/src/http.rs`
- `crates/gsessions/src/transcript_http.rs`
- `crates/gsessions/tests/transcript_routes.rs`

**Research context:**
- `src/gobby/servers/routes/sessions/messages.py::sessions_get_messages`
  returns rendered messages and answers 413 when the rendered result is too
  large (memory c9f5467c); `messages.py::transcript_status` serves 3.6's HTTP
  shape; `messages.py::get_transcript` streams the raw transcript, and
  `messages.py::_stream_archive` streams a decompressed archive;
  `messages.py::restore_transcript_endpoint` calls 3.6's restore.
- Remote raw streaming issues consecutive `transcript.read` ranges below the
  reply budget and writes them to the response in order; a lost owner
  mid-stream aborts the response.

**Acceptance:**

- 3.10.1 - `HTTP GET /api/sessions/{session_id}/messages` matches the corpus,
  including the 413 case. test: `crates/gsessions/tests/transcript_routes.rs::messages_route_matches_python`.
- 3.10.2 - `HTTP GET /api/sessions/{session_id}/transcript` streams the live
  file or the decompressed archive byte-for-byte, locally and through the
  doubled owner channel. test: `crates/gsessions/tests/transcript_routes.rs::raw_transcript_streams_match`.
- 3.10.3 - `HTTP GET /api/sessions/{session_id}/transcript/status` matches the
  corpus. test: `crates/gsessions/tests/transcript_routes.rs::transcript_status_route_matches_python`.
- 3.10.4 - `HTTP POST /api/sessions/{session_id}/restore-transcript` matches
  the corpus. test: `crates/gsessions/tests/transcript_routes.rs::restore_route_matches_python`.

Planned verification: the `transcript_routes` test binary with the 1.1 DB
prefix. Nothing has run during drafting.

## P4: Summaries, background jobs and live processing
`kind: framing`

4.1 ports summary generation. The generate-summary route, the CLI's
`summarize` verb, transcript processing and the hooks plan's summary dispatch
all use it. 4.2 ports the two background loops that Decision Record item 5
assigns to this family as one lease-scoped task, passive until 7.2 selects
Native. 4.3 ports live session message processing for the hooks seam;
nothing in this plan starts it.

### 4.1 Session summaries [category: code] (depends: 3.10, #25097)
`kind: deliverable`

Targets:
- `Cargo.lock`
- `crates/gsessions/Cargo.toml`
- `crates/gai/src/lib.rs`
- `crates/gsessions/src/lib.rs`
- `crates/gsessions/src/family.rs`
- `crates/gsessions/src/http.rs`
- `crates/gsessions/src/summaries.rs`
- `crates/gsessions/src/summary_context.rs`
- `crates/gsessions/src/summary_format.rs`
- `crates/gsessions/src/analyzer.rs`
- `crates/gsessions/src/analyzer_turns.rs`
- `crates/gsessions/src/ports.rs`
- `crates/gsessions/tests/summaries.rs`
- `crates/gtasks/src/session_refs.rs`
- `docs/contracts/parity-waivers.json::*` — scope-reason: add the generate-summary request-options and prompt-template waiver entries

**Research context:**
- Python sources:
  - `src/gobby/sessions/summarize.py::generate_session_summaries` is the
    entry that the route, the CLI and
    `src/gobby/hooks/session_summary_dispatcher.py` call. It joins one
    in-flight generation per session, can set `awaiting_handoff`, and writes
    a file only when asked; `write_file` defaults to false and the route never
    sets it.
  - `summarize.py::_generate_session_summary_core` checks provenance with
    `summarize.py::build_summary_source_context` and the source hash from
    `src/gobby/sessions/summary_context.py::_source_hash_payload`, reuses a
    current summary, and otherwise runs `summarize.py::_generate_transcript_summary`
    or `summarize.py::_generate_delivered_handoff_summary`.
  - `src/gobby/sessions/summary_generation.py::_generate_full_summary` loads
    and renders the prompt, calls the model, and validates the output with
    `src/gobby/sessions/summary_validity.py::summary_markdown_validation_error`.
    `summary_generation.py::_persist_summary_markdown` writes through
    `persist_summary_state` with the expected session row, so a concurrent
    row change wins over a stale summary.
  - `summary_context.py::_build_summary_prompt_context` gathers turns,
    claimed tasks (`summary_context.py::_get_claimed_tasks`), session
    memories (`summary_context.py::_get_session_memories`), git status and
    diff, and file changes. `src/gobby/sessions/summary_transcripts.py`,
    `src/gobby/sessions/summary_formatting.py` and
    `src/gobby/sessions/formatting.py` format them.
  - `src/gobby/sessions/analyzer.py::TranscriptAnalyzer` and
    `src/gobby/sessions/analyzer_turns.py` extract handoff context; 8.1 reuses
    the port.
  - `src/gobby/servers/routes/sessions/analytics.py::generate_session_summary`
    takes no body and answers 503, 404, 403 (another machine's session), 422
    or 500.
  - `src/gobby/cli/sessions.py::summarize_session` calls the model itself.
    It takes `--session-id` (default: the current active session),
    `--output db|file|all`, `--path` (default `.gobby/session_summaries/`) and
    notes. When generation fails it warns and builds a code-only summary from
    the analyzer context, `git status --short` and `git log -10`.
- Reads (Decision 7): claimed tasks come from gtasks, through a narrow
  `claimed_tasks(conn, session_id)` this leaf adds to
  `crates/gtasks/src/session_refs.rs`; session memories come from the gmemory
  port's `session_memories(session_id)`. Turns come from 3.7's reader and git
  status and diff from 1.3's git service, so a remote owner is served through
  `OwnerChannel` like every owner-routed read.
- Prompt: `summary_context.py::load_summary_prompt_template` loads
  `handoff/session_end` through
  `src/gobby/storage/prompts.py::LocalPromptManager.get_by_name` with no
  project and falls back to the configured prompt. Native loads it through
  `crates/gruntime/src/prompts.rs`, the prompts owner, with the same
  no-project lookup and fallback. `gdaemon-runtime-family 5.4` creates that
  module; the Orchestrator converts this cross-plan edge at expansion.
  `src/gobby/llm/prompt_rendering.py::render_summary_prompt` uses Jinja when
  the template contains `{{`, `{%` or `{#`, and `str.format` otherwise. The
  bundled template uses only `{{ name }}` placeholders, and no Rust template
  engine is in the lockfile. The summary formatter renders `{{ name }}`
  placeholders, where an unknown name renders empty as Jinja's default
  undefined does, and `str.format`'s `{name}` placeholders, where a missing
  name returns the corpus error. An override with any other Jinja syntax
  returns a typed validation error naming the template path and calls no
  model, under waiver `sessions.http-generate-summary.prompt-template-subset`.
  Memory-family Decision 12 makes the same choice.
- Model: the hub calls #25097's text-generation service in process. It passes
  the `session_summary` feature configuration
  (`src/gobby/config/sessions.py::SessionSummaryConfig`, read from
  `config_store` through the pool), Python's fixed system prompt, caller
  `sessions.summary` and the summary validator as the request's output
  validator (the `output_validator` field of
  `src/gobby/ai/_text_generation_contracts.py`). `crates/gai` exports only
  its family-push function, so this leaf gives `crates/gai/src/lib.rs` one
  re-export of the service unless memory-family 2.4 already has; there is
  never a second one. `gobby-sessions` gains `gobby-ai` as a path dependency.
- Request body (Decision Record item 3): the generate-summary route accepts
  an optional JSON body `{notes, fallback, persist}`. `notes` is appended as
  `src/gobby/cli/sessions.py::_append_summary_notes` does. `fallback: "code"`
  returns the code-only summary, built from the owning machine's checkout,
  when generation fails, with the warning in the response. `persist: false`
  skips the database write, so `gclient sessions summarize --output file`
  writes only its local file. A response to a request with a body also
  carries the handoff-context facts the CLI prints (active task, files
  modified, git commits, initial goal). A request without a body keeps
  Python's behavior exactly. Waiver: `sessions.http-generate-summary.cli-request-options`.

**Granularity:** Summary generation is one pipeline with one HTTP consumer,
and the analyzer and formatters exist only to feed it. The production-file
count passes six because the analyzer, its turn extraction and the formatters
stay in separate files to keep each under 1,000 lines.

**Acceptance:**

- 4.1.1 - `HTTP POST /api/sessions/{session_id}/generate-summary` without a
  body matches the corpus, including reuse of a current summary and the 404,
  422 and unavailable-model cases.
  test: `crates/gsessions/tests/summaries.rs::generate_summary_route_matches_python`.
- 4.1.2 - Transcript and delivered-handoff summaries build the same prompt
  context as Python for the fixture sessions, locally and through the doubled
  owner channel, reading tasks through gtasks, memories through a fixture
  gmemory adapter and the prompt through gruntime.
  test: `crates/gsessions/tests/summaries.rs::summary_context_matches_python`.
- 4.1.3 - Rendering matches Python for `{{ name }}` and `{name}` templates,
  and other Jinja syntax is refused without a model call under its waiver.
  test: `crates/gsessions/tests/summaries.rs::prompt_rendering_matches_python_subset`.
- 4.1.4 - The output validator rejects invalid summaries as Python's does, a
  concurrent row change wins over a stale summary, and concurrent requests
  for one session share one generation.
  test: `crates/gsessions/tests/summaries.rs::generation_guards_match_python`.
- 4.1.5 - The body's notes, code-only fallback and `persist: false` produce
  the CLI's results under their waiver.
  test: `crates/gsessions/tests/summaries.rs::cli_request_options_match_cli`.
- 4.1.6 - `TranscriptAnalyzer`'s handoff context matches Python for the 0.2
  corpus. test: `crates/gsessions/tests/summaries.rs::analyzer_matches_python`.

Planned verification: the `summaries` test binary with the 1.1 DB prefix
against a doubled text-generation service, and
`cargo clippy -p gobby-sessions -p gobby-ai`. Nothing has run during drafting.

### 4.2 Session expiry and transcript processing jobs [category: code] (depends: 4.1)
`kind: deliverable`

Targets:
- `crates/gsessions/src/lib.rs`
- `crates/gsessions/src/jobs.rs`
- `crates/gsessions/tests/jobs.rs`
- `crates/gterminals/src/session_links.rs`
- `crates/gdaemon/src/lib.rs`
- `crates/gdaemon/src/sessions_jobs.rs`
- `crates/gdaemon/src/state.rs::RuntimeState::run`
- `crates/gdaemon/src/state_services.rs::ActiveServices`
- `crates/gdaemon/src/state_services.rs::ActiveServices::start`
- `crates/gdaemon/src/state_services.rs::ActiveServices::stop`
- `crates/gdaemon/src/state_services.rs::explicit_drain_stops_host_default_stop_preserves_it`
- `crates/gdaemon/src/serve.rs::*` — scope-reason: pass the Sessions job context built beside the family into RuntimeState::run
- `crates/gdaemon/tests/sessions_jobs.rs`

**Research context:**
- Host: `crates/gdaemon/src/state_services.rs::ActiveServices::start` builds
  its own pool from `bootstrap.database_url` and spawns
  `crates/gdaemon/src/retention.rs::run` with a `watch` stop channel;
  `ActiveServices::stop` signals and awaits it. Retention takes a single
  owner per cycle with `DedicatedSession::try_session_lock`. The Sessions
  job follows that precedent in the new `crates/gdaemon/src/sessions_jobs.rs`,
  declared in `crates/gdaemon/src/lib.rs` beside `mod retention;`, with its
  own owner-lock name. It is spawned only when
  `bootstrap.front_door.routes` selects Native for `sessions`. The
  `explicit_drain_stops_host_default_stop_preserves_it` test builds an
  `ActiveServices` literal and gains the new fields.
- Job context: the job needs the owner channel and 4.1's summary service.
  Serve builds the hub node registry before it calls `RuntimeState::run`
  (`crates/gdaemon/src/serve.rs` line 264 and line 365), so it builds the
  Sessions job context beside the family and passes it through
  `RuntimeState::run` to `ActiveServices::start`. `RuntimeState::run`'s only
  caller is serve. A node has no database URL and starts no job.
- Expiry: `src/gobby/sessions/lifecycle.py::SessionLifecycleManager._expire_stale_sessions`
  runs, in order, `src/gobby/storage/session_lifecycle.py::pause_inactive_active_sessions`
  (`active_session_pause_minutes`), `session_lifecycle.py::expire_orphaned_handoff_sessions`
  (`HANDOFF_IN_FLIGHT_MINUTES`), `session_lifecycle.py::prune_stale_compact_workflow_instances`
  (`SESSION_REVIVAL_HORIZON_HOURS`), `session_lifecycle.py::cleanup_expired_session_state`,
  `session_lifecycle.py::expire_stale_sessions` (`stale_session_timeout_hours`),
  `src/gobby/storage/hook_receipts.py::retire_expired_session_hook_effects`,
  `session_lifecycle.py::expire_empty_sessions` (2 hours) and
  `session_lifecycle.py::prune_empty_sessions` (1 hour), every
  `expire_check_interval_minutes`.
- Native steps: the job runs the Sessions-owned steps in that order with
  Python's thresholds: pause, orphaned-handoff expiry, the session-variable
  delete of `cleanup_expired_session_state`, stale expiry, empty expiry and
  empty prune. Every status change the job writes sends 2.2's
  `gobby_session_status` notification in the same transaction.
- Foreign pieces stay in Python's shared sweep in every mode (Decision 5),
  and the native job runs none of them:
  - `prune_stale_compact_workflow_instances` (#25279);
  - `retire_expired_session_hook_effects` (the hooks plan's 3.4 table,
    #24151);
  - the `pending_interactions` update inside `cleanup_expired_session_state`,
    whose table the web-chat family owns (#21592).
  Each native step selects by the session row's own status and age, so
  splitting the cleanup from the `pending_interactions` update loses no
  correctness: each half is idempotent.
- Foreign reads (Decision 7):
  - `expire_orphaned_handoff_sessions` expires an `awaiting_handoff`
    terminal session only when its recorded native terminal has exited and
    it holds no pending or live terminal. Native asks gterminals through
    `released_handoff_sessions(tx, candidates)`, a narrow function this leaf
    adds to `crates/gterminals/src/session_links.rs`, and filters its update
    with `id = ANY(...)`.
  - `prune_empty_sessions` refuses retained sessions through 2.2's exported
    retained-reference check.
  - Status events carry `project_name` from 1.1's `project_names`.
- Processing: `src/gobby/sessions/transcript_processing.py::TranscriptProcessingMixin._process_pending_transcripts`
  reads machine-local pages from
  `src/gobby/storage/sessions/_transcript.py::_TranscriptMixin.get_pending_transcript_sessions`
  (expired, unprocessed, under the failure limit, in creation order) of
  `transcript_processing_batch_size`, every
  `transcript_processing_interval_minutes`. Per session it runs
  `TranscriptProcessingMixin._process_session_transcript`,
  `TranscriptProcessingMixin._persist_session_transcript`,
  `TranscriptProcessingMixin._persist_session_token_usage` and
  `TranscriptProcessingMixin._generate_artifacts_if_needed`, which skips the
  model for short or non-human sessions, then
  `_TranscriptMixin.mark_transcript_processed` with the source hash or
  `_TranscriptMixin.record_transcript_processing_failure`.
- Ownership (Decision Record item 5): the hub job pages the hub machine's
  sessions and parses them in-process on the blocking pool. It pages each
  connected node at the new contract version and drives `transcript.process`
  pages; a node at an older version, or one that refuses (3.9), is skipped
  and its sessions stay pending. The hub writes every row and broadcasts
  token events through 2.1's event sink.

**Granularity:** Expiry and processing share one host, one owner lock and one
lifecycle switch. The gdaemon targets are that host's composition, and
splitting the two loops would compose the host twice.

**Acceptance:**

- 4.2.1 - One expiry cycle applies the six Sessions-owned steps in Python's
  order with Python's thresholds, notifies every status change, and leaves
  workflow instances, hook effects and pending interactions untouched.
  test: `crates/gsessions/tests/jobs.rs::expiry_cycle_matches_python`.
- 4.2.2 - One processing cycle writes the same statistics, token events,
  summaries, processed marks and failure counts as Python for local
  fixtures and through doubled `transcript.process` pages.
  test: `crates/gsessions/tests/jobs.rs::processing_cycle_matches_python`.
- 4.2.3 - Sessions on an older-version node, or on one that refuses
  `transcript.process`, stay pending and do not stop the cycle.
  test: `crates/gsessions/tests/jobs.rs::unavailable_owners_stay_pending`.
- 4.2.4 - The job starts only when the bootstrap selects Native for
  `sessions`, runs on one daemon at a time under its owner lock, and stops
  with `ActiveServices::stop`.
  test: `crates/gdaemon/tests/sessions_jobs.rs::job_runs_only_when_native_and_single_owner`.
- 4.2.5 - Orphaned-handoff expiry reads terminals only through
  `released_handoff_sessions`, and the empty prune keeps every session the
  retained-reference check names.
  test: `crates/gsessions/tests/jobs.rs::expiry_foreign_reads_use_owner_apis`.

Planned verification: the `jobs` test binary with the 1.1 DB prefix,
`cargo nextest run -p gobby-daemon -E 'binary(sessions_jobs) | test(explicit_drain_stops_host_default_stop_preserves_it)'`
and `cargo clippy -p gobby-sessions -p gobby-daemon`. Nothing has run during
drafting.

### 4.3 Live session message processing [category: code] (depends: 4.2)
`kind: deliverable`

Targets:
- `crates/gsessions/src/lib.rs`
- `crates/gsessions/src/processor.rs`
- `crates/gsessions/src/processor_transcripts.rs`
- `crates/gsessions/src/processor_usage.rs`
- `crates/gsessions/src/tail_cursor.rs`
- `crates/gsessions/src/turn_interrupt.rs`
- `crates/gsessions/tests/processor.rs`

**Research context:**
- Python sources: `src/gobby/sessions/processor.py::SessionMessageProcessor`
  combines `src/gobby/sessions/processor_lifecycle.py::ProcessorLifecycleMixin`
  (`start`, `stop`, `register_session`, `unregister_session`,
  `flush_session`, `complete_turn` and `reconcile_codex_transcript`, with
  `processor_lifecycle.py::SessionFlushResult` and
  `processor_lifecycle.py::TurnCompletion`), the tail reads and interrupt
  reduction in `src/gobby/sessions/processor_transcripts.py`,
  `src/gobby/sessions/processor_usage.py`, `src/gobby/sessions/processor_stats.py`
  and the host protocol `src/gobby/sessions/processor_types.py::ProcessorHost`.
  `src/gobby/config/sessions.py::MessageTrackingConfig` supplies `enabled`,
  `poll_interval`, `debounce_delay`, `max_message_length` and
  `broadcast_enabled`.
- Cursors: `src/gobby/sessions/transcript_cursor.py::TranscriptTailCursor`,
  `transcript_cursor.py::CodexRolloutCursor`,
  `transcript_cursor.py::ClaudeTranscriptCursor`,
  `transcript_cursor.py::GrokEventsCursor`,
  `transcript_cursor.py::build_interrupt_observer` and
  `transcript_cursor.py::build_turn_settled_observer`, plus
  `src/gobby/sessions/transcript_interrupt.py`. Their Python consumers (hook
  events, handoff recovery, turn lifecycle, wake activity, the Codex compact
  watch and handoff reconciliation) move with the hooks plan's composition;
  this leaf exports the ports.
- Host: `processor_types.py::WebSocketServer` broadcasts token events and
  usage updates and feeds attached-session text to speech. The native host
  trait covers those callbacks and the hook callbacks; broadcasts go through
  2.1's event sink, and the composer supplies the speech feed (#21592 owns
  voice).
- Owners: a hub-local session's tail is read in-process on the blocking
  pool; a remote session's tail is read with ranged `transcript.read` (3.8).
  Losing an owner stops that session's tail only.
- Not started here: `s2-11-hub-hooks-rules 7.1` composes the processor at
  the hooks flip (Decision Record item 10), and Python's processor runs
  until then. 7.2 does not start it.

**Granularity:** Seven acceptance items exercise one processor state machine.
The cursors and interrupt reduction have no consumer outside it except the
hooks plan, which composes them through the same exports.

**Acceptance:**

- 4.3.1 - Start, stop, register and unregister match Python's processor
  lifecycle. test: `crates/gsessions/tests/processor.rs::processor_lifecycle_matches_python`.
- 4.3.2 - The Claude, Codex and Grok tail cursors emit Python's records for
  the 0.2 corpus, including partial lines and truncation.
  test: `crates/gsessions/tests/processor.rs::tail_cursors_match_python`.
- 4.3.3 - `flush_session` and `complete_turn` return Python's flush results
  and turn completions. test: `crates/gsessions/tests/processor.rs::flush_and_turn_completion_match_python`.
- 4.3.4 - The interrupt and turn-settled observers match Python.
  test: `crates/gsessions/tests/processor.rs::interrupt_observers_match_python`.
- 4.3.5 - Codex transcript reconciliation matches Python.
  test: `crates/gsessions/tests/processor.rs::codex_reconcile_matches_python`.
- 4.3.6 - Usage and statistics writes and the token-event and usage
  broadcasts match Python, honoring `broadcast_enabled`.
  test: `crates/gsessions/tests/processor.rs::usage_writes_and_broadcasts_match_python`.
- 4.3.7 - A remote session's tail through the doubled owner channel equals
  the local tail, and losing that owner stops only its session.
  test: `crates/gsessions/tests/processor.rs::remote_tail_matches_local`.

Planned verification: the `processor` test binary with the 1.1 DB prefix.
Nothing has run during drafting.

## P5: MCP tool definitions
`kind: framing`

5.1 gives the 19 MCP rows that P1–P3 serve native definitions, schemas and
result parity. #21570 activates them, so their rows stay `proxied` with
#21570 owner notes. `set_handoff` gains its definition in 8.6;
`release_composer` is not ported (Decision Record item 6).

### 5.1 Sessions MCP tool definitions [category: code] (depends: 4.3, #24883, #24970)
`kind: deliverable`

Targets:
- `crates/gsessions/Cargo.toml`
- `Cargo.lock`
- `crates/gsessions/src/lib.rs`
- `crates/gsessions/src/mcp.rs`
- `crates/gsessions/src/mcp_requests.rs`
- `crates/gsessions/tests/mcp.rs`
- `docs/contracts/parity-waivers.json::*` — scope-reason: add the four B8 internal-error-redaction waiver entries

**Research context:**
- Python sources: `src/gobby/mcp_proxy/tools/sessions/_factory.py::create_session_messages_registry`
  registers the `gobby-sessions` tools from the adjacent modules, and
  `src/gobby/mcp_proxy/tools/hub.py::create_hub_registry` registers
  `list_cross_project_sessions`. Each tool's name, description, brief, input
  schema and definition hash come from 0.1's corpus.
- Definitions (Decision Record item 10): `mcp.rs` exports two
  `gobby_core::mcp::ToolDefinition` lists over the services P1–P3 built: 18
  `gobby-sessions` tools (`feedback`, `get_current_session`, `get_handoff`,
  `get_session`, `get_session_commits`, `get_session_messages`,
  `get_transcript_status`, `get_usage_breakdown`, `hold_session_claims`,
  `list_sessions`, `mark_loop_complete`, `query_readonly`,
  `register_session`, `release_session_claims_hold`,
  `restore_session_transcript`, `search_session_messages`, `session_stats`,
  `set_title`) and one `gobby-hub` tool (`list_cross_project_sessions`).
  Request types in `mcp_requests.rs` derive `schemars::JsonSchema`, so
  `gobby-sessions` gains `schemars` (already locked through `gobby-core`).
  Read-only tools are marked read-only; tools that write rows, files or
  claims are not. No `ToolFamily`, server or registry is built here; the
  tests compose a test-only server to run gcore's registry checks.
- Handlers call the services directly, keep explicit arguments distinct from
  caller context, and translate errors once through #24970's MCP adapter
  into the existing HTTP-200 error envelopes. B8: unexpected errors in
  `mark_loop_complete`, `get_session_messages`, `search_session_messages`
  and `register_session` become `Internal server error` with diagnostics
  logged privately, under their four exact waivers, each with decision
  `.gobby/plans/sessions-family.md#5.1` and its exact tool surface.
  `query_readonly` keeps returning the server's error message, its
  deliberate behavior; the corpus compares the primary message.
- #24883's descriptor suite is `crates/gcore/tests/mcp_contract.rs`. As its
  consumer, this leaf runs the whole `--test mcp_contract` target; a
  `test(mcp_contract)` filter misses the two tests outside that module.

**Granularity:** Nineteen row items, the catalog item and the safe-error item
exercise one definition and dispatch adapter. Each tool's behavior and its
tests already live in the owning service leaf; splitting the adapter by tool
would create per-file wrappers.

**Acceptance:**

- 5.1.1 - `MCP gobby-sessions:feedback` matches the corpus.
  test: `crates/gsessions/tests/mcp.rs::feedback_matches_python`.
- 5.1.2 - `MCP gobby-sessions:get_current_session` matches the corpus.
  test: `crates/gsessions/tests/mcp.rs::get_current_session_matches_python`.
- 5.1.3 - `MCP gobby-sessions:get_handoff` matches each consume, agent-run
  and failed-attempt case. test: `crates/gsessions/tests/mcp.rs::get_handoff_matches_python`.
- 5.1.4 - `MCP gobby-sessions:get_session` matches the corpus.
  test: `crates/gsessions/tests/mcp.rs::get_session_matches_python`.
- 5.1.5 - `MCP gobby-sessions:get_session_commits` matches under B5's waiver.
  test: `crates/gsessions/tests/mcp.rs::get_session_commits_matches_python`.
- 5.1.6 - `MCP gobby-sessions:get_session_messages` matches the corpus.
  test: `crates/gsessions/tests/mcp.rs::get_session_messages_matches_python`.
- 5.1.7 - `MCP gobby-sessions:get_transcript_status` keeps W1's MCP shape.
  test: `crates/gsessions/tests/mcp.rs::get_transcript_status_matches_python`.
- 5.1.8 - `MCP gobby-sessions:get_usage_breakdown` matches the corpus.
  test: `crates/gsessions/tests/mcp.rs::get_usage_breakdown_matches_python`.
- 5.1.9 - `MCP gobby-sessions:hold_session_claims` matches the corpus.
  test: `crates/gsessions/tests/mcp.rs::hold_session_claims_matches_python`.
- 5.1.10 - `MCP gobby-sessions:list_sessions` matches the corpus.
  test: `crates/gsessions/tests/mcp.rs::list_sessions_matches_python`.
- 5.1.11 - `MCP gobby-sessions:mark_loop_complete` matches the corpus.
  test: `crates/gsessions/tests/mcp.rs::mark_loop_complete_matches_python`.
- 5.1.12 - `MCP gobby-sessions:query_readonly` matches results, row caps,
  timeouts and refusals. test: `crates/gsessions/tests/mcp.rs::query_readonly_matches_python`.
- 5.1.13 - `MCP gobby-sessions:register_session` matches the corpus.
  test: `crates/gsessions/tests/mcp.rs::register_session_matches_python`.
- 5.1.14 - `MCP gobby-sessions:release_session_claims_hold` matches the
  corpus. test: `crates/gsessions/tests/mcp.rs::release_session_claims_hold_matches_python`.
- 5.1.15 - `MCP gobby-sessions:restore_session_transcript` matches the
  corpus. test: `crates/gsessions/tests/mcp.rs::restore_session_transcript_matches_python`.
- 5.1.16 - `MCP gobby-sessions:search_session_messages` matches the corpus.
  test: `crates/gsessions/tests/mcp.rs::search_session_messages_matches_python`.
- 5.1.17 - `MCP gobby-sessions:session_stats` matches the corpus.
  test: `crates/gsessions/tests/mcp.rs::session_stats_matches_python`.
- 5.1.18 - `MCP gobby-sessions:set_title` matches the corpus.
  test: `crates/gsessions/tests/mcp.rs::set_title_matches_python`.
- 5.1.19 - `MCP gobby-hub:list_cross_project_sessions` matches the corpus.
  test: `crates/gsessions/tests/mcp.rs::list_cross_project_sessions_matches_python`.
- 5.1.20 - Names, descriptions, briefs, schemas, definition hashes and
  read-only marks match the frozen Python contract, gcore's registry accepts
  both lists, and malformed arguments call no service.
  test: `crates/gsessions/tests/mcp.rs::catalog_and_validation_match_python`.
- 5.1.21 - The four B8 tools return safe internal errors with no partial
  effects under their exact waivers, while deliberate errors keep their
  text. test: `crates/gsessions/tests/mcp.rs::unexpected_errors_are_redacted`.

Planned verification: the `mcp` test binary with the 1.1 DB prefix and
`CARGO_BUILD_JOBS=3 cargo test -p gobby-core --test mcp_contract`. Nothing has
run during drafting.

## P6: Native CLI
`kind: framing`

6.1 ports the nine kept CLI rows to `gclient sessions <verb>` over the hub's
public HTTP API (Decision Record item 3). 7.3 deletes the Python verbs.

### 6.1 gclient sessions verbs [category: code] (depends: 5.1)
`kind: deliverable`

Targets:
- `crates/gclient/src/command.rs::*` — scope-reason: declare the sessions module and route the sessions verb in dispatch_inner before workspace parsing
- `crates/gclient/src/command/help.rs::*` — scope-reason: add the sessions verb and its subcommands to the help table
- `crates/gclient/src/command/sessions.rs`
- `crates/gclient/tests/sessions_cli.rs`

**Research context:**
- Cross-plan edge: `node-mode 2.1` (`.gobby/plans/node-mode.md` at
  `cbafbe253f`) supplies the operator HTTP helper
  (`crates/gclient/src/command/api.rs`), the shared token resolver
  (`command::resolve_token`) and the exit contract (node-mode O1, O2, O4 and
  O5): 0 success, 1 daemon refusal, 2 usage, 3 connection or credential
  failure, with `gclient {verb}: ...` on stderr. The verbs extend the
  existing parser and help layout for non-workspace verbs that node-mode
  establishes. The Orchestrator converts the edge at expansion.
- Python sources: `src/gobby/cli/sessions.py::sessions`,
  `sessions.py::list_sessions`, `sessions.py::show_session`,
  `sessions.py::show_messages`, `sessions.py::delete_session`,
  `sessions.py::session_stats`, `sessions.py::summarize_session` and
  `sessions.py::restore_transcript`, plus
  `src/gobby/cli/sessions_dirty_paths.py::approve_dirty_paths`. Flags,
  output text, JSON shapes and exit codes come from 0.1's corpus.
- Route mapping:
  - `list`: `GET /api/sessions`. The blocked-attention column reads
    `GET /api/attention/roster`, whose entries carry `session_id`, `state`
    and `reason`, and aggregates them as
    `src/gobby/cli/sessions.py::_blocked_attention_by_session` does.
  - `show`: `GET /api/sessions/{session_id}`.
  - `messages`: `GET /api/sessions/{session_id}/messages`.
  - `delete`: the native `DELETE /api/sessions/{session_id}` (2.2). W4:
    the verb never opens the database.
  - `stats`: pages `GET /api/sessions` with the keyset cursor up to Python's
    10,000-row bound and counts by status and source.
  - `summarize`: `POST /api/sessions/{session_id}/generate-summary` with
    4.1's body. `--output file` sends `persist: false`; `file` and `all`
    write `session_<unix seconds>_<first 12 id characters>.md` under `--path`
    on the caller's machine, as `src/gobby/cli/sessions.py::summarize_session`
    names it, and the verb prints the handoff-context lines from the
    response.
  - `restore`: `POST /api/sessions/{session_id}/restore-transcript`.
  - `approve-dirty-paths`: `POST /api/sessions/{session_ref}/dirty-path-approvals`
    with only the bootstrap operator key, read through
    `gobby_core::local_token::read_api_key_at` as
    `src/gobby/utils/local_token.py::read_local_api_token` reads it. It sends
    no managed bearer and no identity headers, and a missing key fails
    before any request with Python's login hint.
- Project refs (`--project` on `list` and `stats`) and session refs resolve
  through the hub: the routes accept session refs (1.1), and project names
  resolve with the public projects API.
- `terminate-terminal` is not ported (#21565 owns it).

**Acceptance:**

- 6.1.1 - `CLI gobby sessions` group help and dispatch match the corpus.
  test: `crates/gclient/tests/sessions_cli.rs::sessions_group_matches_python`.
- 6.1.2 - `CLI gobby sessions list` matches filters, the attention column,
  text and JSON output. test: `crates/gclient/tests/sessions_cli.rs::list_matches_python`.
- 6.1.3 - `CLI gobby sessions show` matches text, JSON and missing-session
  output. test: `crates/gclient/tests/sessions_cli.rs::show_matches_python`.
- 6.1.4 - `CLI gobby sessions messages` matches the corpus.
  test: `crates/gclient/tests/sessions_cli.rs::messages_matches_python`.
- 6.1.5 - `CLI gobby sessions delete` calls the native route and matches the
  corpus output. test: `crates/gclient/tests/sessions_cli.rs::delete_matches_python`.
- 6.1.6 - `CLI gobby sessions stats` matches counts across more than one
  page. test: `crates/gclient/tests/sessions_cli.rs::stats_matches_python`.
- 6.1.7 - `CLI gobby sessions summarize` matches each `--output` mode, notes
  and the code-only fallback warning.
  test: `crates/gclient/tests/sessions_cli.rs::summarize_matches_python`.
- 6.1.8 - `CLI gobby sessions restore` matches the corpus.
  test: `crates/gclient/tests/sessions_cli.rs::restore_matches_python`.
- 6.1.9 - `CLI gobby sessions approve-dirty-paths` sends only the operator
  key, refuses without one, ignores managed credentials and reports the
  daemon's refusal. test: `crates/gclient/tests/sessions_cli.rs::approve_dirty_paths_matches_python`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-client --test sessions_cli`
against `crates/gclient/tests/mock_daemon/mod.rs`, and
`cargo clippy -p gobby-client`. Nothing has run during drafting.

## P7: Admission, activation and retirement
`kind: framing`

7.1 admits browser callers through the shared verifier. 7.2 is the family's
shipping commit: it makes Native the Sessions default, switches the Python
loops off and releases the binaries. 7.3 deletes Sessions-owned Python: the
CLI verbs and dropped commands, the routes of the 25 switched rows, and the
Python expiry and processing loops (Decision Record item 15). 7.4 records the
ledger and documentation.

### 7.1 Sessions browser admission through the shared verifier [category: code] (depends: 6.1, #24967, #24964)
`kind: deliverable`

Targets:
- `crates/gsessions/src/family.rs`
- `crates/gdaemon/src/front_door/sessions.rs`
- `crates/gdaemon/src/front_door/mod.rs::*` — scope-reason: register the Sessions browser-admission predicate in the existing FrontDoor::handle dispatch boundary, keeping the file-wide scope 1.1 uses
- `crates/gdaemon/tests/sessions_browser.rs`
- `docs/contracts/sessions-native.md`

**Research context:**
- #24967 owns cookie parsing, verification, credential precedence,
  revocation, caller class and selector extraction; #24964 owns the outer
  credentialed CORS and preflight boundary. This leaf consumes both and
  adds only the Sessions route registration and context projection: no
  verifier, session store, CORS handler or Python code. Node requests relay
  to the hub's verifier and create no node-local browser authority.
- Compatibility references:
  `src/gobby/servers/auth_service.py::AuthService.authenticate`,
  `src/gobby/servers/browser_sessions.py::browser_route_allowed` and
  `src/gobby/servers/middleware/auth.py::AuthMiddleware.dispatch`. Ordinary
  `gobby_session` cookies keep their current read and mutation admission.
  `browser-testing.` cookies keep the managed rules: live capability,
  loopback peer and host, Python's per-route allowance, and project and
  session selectors that match the verified managed session.
- Rows registered for `Caller::Browser`, exactly these 25 method and path
  pairs:
  - GET: `/api/sessions` without `include_resumability`,
    `/api/sessions/usage`, `/api/sessions/{session_id}`,
    `/api/sessions/{session_id}/changes`,
    `/api/sessions/{session_id}/changes/diff`,
    `/api/sessions/{session_id}/messages`,
    `/api/sessions/{session_id}/stop`,
    `/api/sessions/{session_id}/token-events`,
    `/api/sessions/{session_id}/transcript` and
    `/api/sessions/{session_id}/transcript/status`.
  - POST: `/api/sessions/bulk-move`, `/api/sessions/find_by_terminal_context`,
    `/api/sessions/find_current`, `/api/sessions/register`,
    `/api/sessions/update_status`, `/api/sessions/update_summary`,
    `/api/sessions/web-chat`, `/api/sessions/{session_id}/expire`,
    `/api/sessions/{session_id}/generate-summary`,
    `/api/sessions/{session_id}/rename`,
    `/api/sessions/{session_id}/restore-transcript`,
    `/api/sessions/{session_id}/stop`,
    `/api/sessions/{session_id}/variables/get` and
    `/api/sessions/{session_id}/variables/set`.
  - DELETE: `/api/sessions/{session_id}/stop`.
- Never browser-admitted: the two operator-only routes (2.5) and the native
  `DELETE /api/sessions/{session_id}`, which serves API-key callers only.
  The P8 rows register their own browser admission.
- Until this leaf ships with Security approval of its exact SHA, every
  Sessions cookie request stays proxied in every switch state. Once
  admitted, a verifier denial never falls through to a native handler.
- Security: the Orchestrator applies `security-review` to this leaf after
  expansion and checks the label before dispatch. Cookie activation waits
  for a Security Reviewer's approval of the exact implementation SHA.

**Acceptance:**

- 7.1.1 - Ordinary cookies reproduce Python's admission, payloads and errors
  on each of the 25 rows through the shared verifier only.
  test: `crates/gdaemon/tests/sessions_browser.rs::ordinary_cookie_routes_match_python`.
- 7.1.2 - Managed cookies keep loopback, live-capability, per-route and
  selector checks, and refused requests reach no Sessions effect.
  test: `crates/gdaemon/tests/sessions_browser.rs::managed_cookie_authority_matches_python`.
- 7.1.3 - The operator-only routes, the native DELETE and unlisted routes
  gain no cookie authority; API-key callers are unchanged.
  test: `crates/gdaemon/tests/sessions_browser.rs::browser_admission_is_exact`.
- 7.1.4 - Both cookie classes keep the observed credentialed CORS, preflight
  and refusal headers through #24964's boundary.
  test: `crates/gdaemon/tests/sessions_browser.rs::cors_and_preflight_match_python`.
- 7.1.5 - The security gate (label, pre-dispatch check, exact-SHA approval)
  is documented. behavior: "Sessions browser admission security gate" in `docs/contracts/sessions-native.md`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-daemon --test sessions_browser`
with isolated session and capability state and a counting Python backend
stub. Nothing has run during drafting.

### 7.2 Sessions activation and coherent release [category: code] (depends: 7.1, #24963)
`kind: deliverable`

Targets:
- `crates/gsessions/src/family.rs`
- `src/gobby/sessions/lifecycle.py::*` — scope-reason: skip the transcript-processing loop and the stale-session expiry step when this machine's bootstrap selects Native for sessions
- `crates/gdaemon/Cargo.toml`
- `crates/gcode/Cargo.toml`
- `crates/ghook/Cargo.toml`
- `crates/gclient/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::MANAGED_BIN_VERSION_PINS`
- `crates/gdaemon/tests/sessions_cutover.rs`
- `tests/sessions/test_lifecycle_native_owner.py`
- `tests/e2e/test_sessions_native.py`
- `docs/contracts/sessions-native.md`

Consumers unchanged:
- `src/gobby/cli/install_setup_gclient.py` — no-edit-reason: dynamically reads its bumped native version pin.
- `src/gobby/cli/install_setup_gcode.py` — no-edit-reason: dynamically reads its bumped native version pin.
- `src/gobby/cli/install_setup_gdaemon.py` — no-edit-reason: dynamically reads the bumped gdaemon pin.
- `src/gobby/cli/install_setup_ghook.py` — no-edit-reason: dynamically reads its bumped native version pin.
- `src/gobby/cli/install_setup_gmcp.py` — no-edit-reason: dynamically reads its unchanged native version pin.
- `src/gobby/cli/install_setup_gterm.py` — no-edit-reason: dynamically reads unchanged native version pins.
- `src/gobby/cli/install_setup_versions.py` — no-edit-reason: resolves every pin dynamically by binary name.
- `src/gobby/code_index/gcode_versions.py` — no-edit-reason: both gcode maintenance floors derive from the bumped gcode pin.
- `src/gobby/hooks/runtime_compat.py` — no-edit-reason: the hook compatibility floor derives from the bumped ghook pin.
- `src/gobby/install/bin_freshness_models.py` — no-edit-reason: enumerates the map dynamically.
- `src/gobby/install/distribution.py` — no-edit-reason: resolves minimum versions dynamically.
- `tests/cli/installers/test_grok_installer.py` — no-edit-reason: derives expected ghook versions from the map.
- `tests/cli/test_install_ghook.py` — no-edit-reason: derives expected versions from the map.
- `tests/cli/test_install_setup.py` — no-edit-reason: derives expected versions from the map.
- `tests/cli/test_install_setup_gclient.py` — no-edit-reason: derives expected versions from the map.
- `tests/cli/test_install_setup_gdaemon.py` — no-edit-reason: derives expected versions from the map.
- `tests/cli/test_install_setup_gmcp.py` — no-edit-reason: derives expected versions from the map.
- `tests/cli/test_install_setup_gterm.py` — no-edit-reason: derives expected versions from the map.
- `tests/code_index/test_gcode_gateway.py` — no-edit-reason: derives gcode expectations from the bumped pin.
- `tests/hooks/test_runtime_compat.py` — no-edit-reason: derives ghook expectations from the bumped pin.
- `tests/install/test_distribution.py` — no-edit-reason: dynamically parameterizes managed version expectations.
- `tests/install/test_version_pins.py` — no-edit-reason: already verifies each managed pin against its Cargo manifest.

**Research context:**
- Switch: `crates/gcore/src/route_family.rs::RouteFamily` supplies
  `default_backend`; this leaf makes Native the Sessions default. Proxy and
  Compare stay selectable through `front_door.routes` for rollback. The
  Sessions jobs (4.2) and the node's `transcript.process` (3.9) follow the
  effective selection.
- Python switch-off (Decision Record item 5): `src/gobby/config/bootstrap.py`
  types `front_door.routes` as `proxy`, `native` or `compare`.
  `SessionLifecycleManager.start` skips `_process_loop`, and
  `SessionLifecycleManager._sweep_shared_rows` skips `_expire_stale_sessions`,
  when this machine's bootstrap selects Native for `sessions`; an absent key
  means Native, matching the new family default, and a test pins both
  defaults together. The other sweeps, the knowledge-graph queue and
  prompt-file cleanup keep running. `src/gobby/runner_lifecycle_subsystems.py`
  needs no edit: its hub and node paths both call `start`. 2.2's status
  notification bridge keeps running in both modes.
- Readiness: consume #24963's shared manifest and readiness result. Every
  changed behavior needs its exact waiver ID and decision with passing native
  response and effect assertions, and at least one ordinary Passed case.
  Zero-Passed, all-Skipped, unwaived-Skipped, stale or unknown IDs and
  unmatched diffs fail activation. Compare runs safe reads only; mutating
  routes are tested against isolated cloned state.
- Gates: the two operator-only routes (2.5) and cookie traffic (7.1) activate
  only with their Security approvals for the exact SHA; until then they stay
  proxied and the runbook records the remainder.
- Release: `src/gobby/install/bin_release_identity.py::workspace_source_identity`
  hashes the root `Cargo.toml` for every managed binary, and 1.1 added a
  workspace member, so gdaemon, gcode and ghook each bump 0.0.1. gclient's
  own source changed in 6.1, and `UNPUBLISHED_MANAGED_BINS` still refuses
  promotion of a changed source without a bump, so gclient bumps 0.0.1 too.
  gmcp and gterm keep their versions. Bump manifests, `Cargo.lock` and pins
  together, rebuild, promote the coherent trio through
  `src/gobby/install/bin_set_coherence.py::promote_workspace_binary_set`,
  promote gclient through its component path, and read installed hashes
  after signing. Follow `project_policy.release_guidance`; only the
  Orchestrator delegates cutover. No push is authorized here.

**Granularity:** The targets and acceptance items belong to one activation
and release state machine; 7.3 and 7.4 record the retirement and ledger
separately.

**Acceptance:**

- 7.2.1 - Proxy, Compare and Native affect only Sessions; Compare runs each
  mutation once against isolated state, and rollback to Proxy preserves
  native writes. test: `crates/gdaemon/tests/sessions_cutover.rs::sessions_cutover_is_scoped_and_single_effect`.
- 7.2.2 - Readiness rejects zero-Passed, all-Skipped, unwaived-Skipped and
  unknown or stale waivers, and accepts the family's exact waivers with
  ordinary parity Passed. test: `crates/gdaemon/tests/sessions_cutover.rs::waiver_readiness_gates_activation`.
- 7.2.3 - With Native selected or the key absent, Python starts neither
  transcript processing nor stale-session expiry and keeps its other
  sweeps; with Proxy or Compare it keeps both loops.
  test: `tests/sessions/test_lifecycle_native_owner.py::test_native_selection_moves_both_loops`.
- 7.2.4 - With the Python backend unavailable, the 26 HTTP rows and the
  nine CLI verbs work natively on a hub and a standalone daemon, and remote
  transcript reads reach a node. test: `tests/e2e/test_sessions_native.py::test_rows_work_without_python_backend`.
- 7.2.5 - Shipping manifests, `Cargo.lock` and pins agree at the required
  patch increments. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.
- 7.2.6 - The runbook records build and installed hashes, compared cases,
  the rollback selection, the announcement, health, the Security approvals
  and native smoke evidence.
  behavior: "Sessions activation evidence" in `docs/contracts/sessions-native.md`.

Planned verification: `cargo nextest run -p gobby-daemon --test sessions_cutover`;
`tests/sessions/test_lifecycle_native_owner.py` and the e2e test with the
protected Python prefix; scoped Ruff and mypy; the version-pin test; the
release build and Orchestrator-directed cutover. These are execution gates,
not drafting results.

### 7.3 Python CLI forwarding and dropped surfaces [category: code] (depends: 7.2)
`kind: deliverable`

Targets:
- `src/gobby/cli/__init__.py::*` — scope-reason: point the lazy sessions registration at the forwarding module
- `src/gobby/cli/sessions_forwarding.py`
- `src/gobby/cli/sessions.py::*` — operation: delete — scope-reason: retire the Python sessions CLI implementation, which the forwarding group replaces
- `src/gobby/cli/sessions_dirty_paths.py::*` — operation: delete — scope-reason: retire the Python dirty-path approval command, which gclient serves
- `src/gobby/storage/sessions/_renumber.py::*` — operation: delete — scope-reason: drop renumbering (B1)
- `src/gobby/storage/sessions/_manager.py::*` — scope-reason: drop the renumber mixin from SessionManager's bases
- `src/gobby/sessions/context_usage.py::backfill_session_context_windows`
- `src/gobby/sessions/context_usage.py::ContextWindowBackfillResult`
- `tests/cli/test_cli_sessions.py::*` — operation: delete — scope-reason: callback tests of the retired module, rehomed to 0.1's corpus and 6.1
- `tests/cli/test_cli_sessions_coverage.py::*` — operation: delete — scope-reason: callback coverage tests of the retired module
- `tests/cli/test_sessions_coverage.py::*` — operation: delete — scope-reason: callback coverage tests of the retired module
- `tests/cli/test_cli_sessions_termination.py::*` — scope-reason: import terminate-terminal from the forwarding module
- `tests/mcp_proxy/tools/test_session_reporting.py::test_cli_session_reads_hydrate_claims`
- `tests/sessions/test_machine_scoped_consumers.py::test_session_filesystem_surfaces_refuse_remote_owner`
- `tests/storage/sessions/test_storage_sessions_lifecycle.py::TestSessionManagerLifecycle.test_renumber_project_sessions_dry_run_leaves_rows_unchanged`
- `tests/storage/sessions/test_storage_sessions_lifecycle.py::TestSessionManagerLifecycle.test_renumber_project_sessions_apply_is_project_scoped_and_tails_deleted`
- `tests/storage/test_sessions_import.py::EXPECTED_PUBLIC_METHOD_SIGNATURES`
- `tests/sessions/test_context_usage.py::test_backfill_bumps_under_counted_windows_and_recomputes_ratio`
- `tests/sessions/test_context_usage.py::test_backfill_dry_run_writes_nothing`
- `tests/workflows/test_dirty_edit_grants.py::test_approve_dirty_paths_cli_refuses_without_an_operator_key`
- `tests/workflows/test_dirty_edit_grants.py::test_approve_dirty_paths_cli_ignores_managed_credentials_without_an_operator_key`
- `tests/workflows/test_dirty_edit_grants.py::test_approve_dirty_paths_cli_sends_only_the_operator_key`
- `tests/workflows/test_dirty_edit_grants.py::test_approve_dirty_paths_cli_reports_the_daemon_refusal`
- `tests/cli/test_sessions_forwarding.py`

**Research context:**
- Decision Record item 3: `src/gobby/cli/sessions_forwarding.py` is a Click
  group with one subcommand per kept verb (`list`, `show`, `messages`,
  `delete`, `stats`, `summarize`, `restore`, `approve-dirty-paths`), so
  `generate_parity_ledger` keeps each CLI key. Each subcommand disables
  option parsing and runs `gclient sessions <verb>` with literal arguments,
  the working directory and inherited streams, resolved through
  `src/gobby/utils/native_bin.py::resolve_native_bin`, and exits with the
  native status; a missing binary keeps the existing diagnostic.
  `terminate-terminal` moves into the group unchanged with its
  `_call_session_tool` callback (#21565 owns it).
- Drops (Debt B1, B2): `renumber` and `backfill-context-windows` have no
  forwarding subcommand. The renumber mixin, the backfill function and its
  result type, and their tests are deleted with no data migration. Consumers
  are listed in `## Constraints`; `_coerce_positive_int` stays.
- The four dirty-path CLI tests move to 6.1.9. The CLI claim-hydration test
  is deleted because 1.1's corpus covers hydration through the routes. The
  remote-owner test drops the retired CLI surfaces and keeps the rest.
- Cross-plan edge: `node-mode 2.7` (`.gobby/plans/node-mode.md` at
  `cbafbe253f`) owns the CLI inventory; the Orchestrator converts the edge at
  expansion.

**Granularity:** One ownership switch: the forwarding group replaces the
Python implementation, and the deletions are that switch's cleanup.

**Acceptance:**

- 7.3.1 - Each kept verb forwards literal arguments, the working directory
  and the exit status to the resolved `gclient`, and the group imports no
  Sessions storage or business module.
  test: `tests/cli/test_sessions_forwarding.py::test_verbs_forward_to_gclient`.
- 7.3.2 - `gobby sessions renumber` and `gobby sessions backfill-context-windows`
  no longer exist, and no production import names the deleted modules or
  functions. test: `tests/cli/test_sessions_forwarding.py::test_dropped_surfaces_are_gone`.
- 7.3.3 - `terminate-terminal` keeps its Python behavior.
  test: `tests/cli/test_cli_sessions_termination.py::test_terminate_terminal_calls_session_tool`.

Planned verification: the changed Python tests with
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest`,
scoped Ruff and mypy, and a changed-test type audit. Nothing has run during
drafting.

### 7.4 Publish Sessions parity ownership [category: docs] (depends: 7.3)
`kind: deliverable`

Targets:
- `docs/contracts/parity-ledger-status.d/sessions-agents.json::*` — scope-reason: update only the Sessions rows that cite #21562 and keep every agents-family row
- `docs/contracts/parity-ledger-status.d/comms-cron-hub-nodes.json::*` — scope-reason: update only the list_cross_project_sessions note
- `docs/contracts/parity-ledger.md`
- `docs/contracts/sessions-native.md`
- `docs/guides/sessions.md`
- `ROADMAP.md`

**Research context:**
- `docs/contracts/parity-ledger.md` is generated by
  `scripts/generate_parity_ledger.py`, whose status check accepts `proxied`,
  `delegated` or a recorded `dropped by decision #N`.
- Statuses (## Parity ownership): 26 HTTP rows and nine CLI rows become
  `delegated`; `renumber` and `backfill-context-windows` become
  `dropped by decision #25253`; the 19 MCP rows that 5.1 covers stay
  `proxied` with notes naming their native handler, evidence and #21570 as
  activation owner. Each note keeps its exact waiver IDs. The two
  release-composer rows stay `proxied` with an owner note citing #25276
  (Decision Record item 6). The other P8 rows keep their current status for
  their leaves.
- `docs/contracts/sessions-native.md` documents the native DELETE extension
  (Decision Record item 12), the generate-summary request body (item 3),
  the family and job controls, rollback, and hub, node and standalone
  placement. `docs/guides/sessions.md` replaces the Python CLI references.
- ROADMAP: the "Ports and the proxied surface" note that lists
  `include_resumability` as a deferred boundary is corrected (Decision
  Record item 1); resumability is computed on the hub through Decision
  Record item 7's ports and lands in 8.5.

**Acceptance:**

- 7.4.1 - Exactly the owned rows change, with the statuses above and
  concrete evidence notes; no other family's row changes.
  behavior: "dropped by decision #25253" in `docs/contracts/parity-ledger-status.d/sessions-agents.json`.
- 7.4.2 - Regenerating the ledger is clean on a second run.
  behavior: "Sessions" in `docs/contracts/parity-ledger.md`.
- 7.4.3 - The native contract documents the DELETE extension and the
  summary request body. behavior: "cli-request-options" in `docs/contracts/sessions-native.md`.
- 7.4.4 - The ROADMAP no longer lists `include_resumability` as a deferred
  boundary. behavior: "include_resumability" in `ROADMAP.md`.
- 7.4.5 - Both release-composer rows stay `proxied` and their notes cite
  #25276 as owner. behavior: "#25276" in `docs/contracts/parity-ledger-status.d/sessions-agents.json`.

Planned verification: `uv run python scripts/generate_parity_ledger.py` twice
and a key-set diff against 0.1's frozen inventory. Nothing has run during
drafting.

## V1 Plan Changelog
`kind: framing`

2026-10-28 — Initial narrative by Plan Writer gobby#18288 records the
Orchestrator rulings S1–S7, B1–B11, W1–W6, C1–C5, the cross-family ruling
(ROADMAP decision 16) and the 63-row scope.
Enhancement and adversarial review have not started; no consensus is claimed.
