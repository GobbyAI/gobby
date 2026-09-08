Plan artifact: `.gobby/plans/gcode-agentic-ask-pipeline.md`

# Implement a gcode-grounded agentic Ask pipeline

**Plan ID:** gcode-agentic-ask-pipeline

## Overview
`kind: framing`

Implement `gcode ask` as a thin client for a persisted Gobby pipeline that prepares commit-bound evidence, launches managed native investigation and review agents, permits one repair attempt, and publishes a verified answer or verified partial answer. This implements the design requested by #22006; that design task is not the implementation epic.

Evidence: [Graphify/gcode #21944](../../docs/evidence/wiki-bakeoff-code-2026-09/graphify-gcode.md), [CodeWiki #21945](../../docs/evidence/wiki-bakeoff-code-2026-09/codewiki.md), and the [frozen cohort matrix](../../docs/evidence/wiki-bakeoff-code-2026-09/matrix.md). The cohort has **14 questions, Q01-Q14**, not 16. Gcode's deterministic retrieval baseline was 6/14; neither comparator supplied native sourced Ask, so there is no comparable historical answer-accuracy score.

Measured requirements: improve on initial retrieval through follow-up queries; preserve exact values and policy precedence omitted by CodeWiki; verify exact source citations; avoid stale artifacts and machine-local links; preserve graph direction, provenance, bounded traversal and stable ordering; record semantic identity; and make interruption/recovery testable. The final deliverable runs the same 14 questions and reports whether results actually improve, including negative results.

## Constraints
`kind: framing`

- User-confirmed decisions: public `gcode ask`; deterministic `gcode evidence`; Gobby-owned pipeline with **managed native agent sessions, never tool_chat**; independent support review; deterministic retrieval by default with opt-in audited hybrid; a single 600-second deadline with no query, turn, or cumulative token count cap; at most one repair; verified partial answers; one committed local repository per run, defaulting to resolved HEAD.
- Preparation was completed by manual expansion into epic #22010. The subsequent user goal authorizes implementation through `gobby-agents` workers using `gpt-5.6-sol` with `xhigh` reasoning in an isolated worktree. Coordinator #22021 owns independent QA, fixes, merge, landing and a daemon cutover coordinated with other sessions. Do not invoke `gobby build` or opt the tree into dispatcher automation. Goal completion additionally requires atomically installed new binaries in `~/.gobby/bin`, a restarted daemon, live `gcode ask`/`gcode evidence` smoke checks and successful source-supported answers to all 14 frozen cohort questions; preserve primary failures and report iteration history honestly.
- Gcode owns indexing, retrieval, exact source extraction and evidence packaging. Gobby owns agent prompts/profiles, permissions, deadlines, persistence, review, repair and publication. Reuse pipeline execution IDs as Ask IDs, existing step records, managed agent runs and completion events. Do not introduce a second orchestration engine or a new Ask database table.
- Alternative evaluated: Gobby could wrap existing search/symbol/graph commands and hash their results against Git. Choose `gcode evidence` because exact source spans, blob identity, graph ownership and completeness belong beside the existing gcode facts facade. Reuse retrieval algorithms; do not duplicate the index or add model orchestration to its core.
- No full wiki generation, new UI, cross-repository questions, or uncommitted-source mode. Exclusion from this release does not create deferred implementation obligations.
- Source text, including embedded agent instructions, is untrusted evidence. Restrict symlinks, submodules, unsupported binary files, secrets and out-of-root paths explicitly; never execute repository code to answer a question.
- No schema migration is planned: persist run inputs, definition snapshots, stage outputs and artifact references through existing pipeline storage. Add focused modules below the production line ceiling. Split dispatch responsibilities before growing the already 848-line native dispatch module.
- Existing integration evidence: `gcode outline crates/gcode/src/cli.rs`, `gcode outline crates/gcode/src/dispatch.rs`, `gcode grep -F 'Command::Search' crates/gcode/src -m 16` identified CLI parsing tests and dispatch matches; `gcode grep -F 'create_pipelines_router' src/gobby -m 15` identified route exports/registration; the corresponding test sweep identified pipeline-route tests. New endpoint tests and native contract tests cover the additive Ask interface without changing existing callers' signatures. Module-wide targets below cover enum/dispatch and import-registration edits spanning declarations; no exact symbol targets or unstable symbol UUIDs are used.
- All leaves preserve these constraints. Code leaves use backend implementation routing and TDD; the standalone native cohort evaluation is category test, without a TDD wrapper. No speculative provider/model pins: resolve configured agent profiles and record their effective identities.

## P1: Deterministic commit-bound evidence
`kind: framing`

### 1.1 Implement pinned evidence queries and source provenance [category: code]
`kind: deliverable`

Targets:
- `crates/gcode/src/evidence/mod.rs`
- `crates/gcode/src/evidence/contracts.rs`
- `crates/gcode/src/evidence/snapshot.rs`
- `crates/gcode/src/evidence/search.rs`
- `crates/gcode/src/evidence/read.rs`
- `crates/gcode/src/evidence/graph.rs`
- `crates/gcode/src/evidence/tests.rs`
- `crates/gcode/src/lib.rs::*` — scope-reason: declare the evidence module beside existing public facts and CLI module declarations.

Provide a model-independent evidence library inside gcode, reusing its facts facade, lexical/exact search, visibility and graph primitives. The request has `schema_version=1`, a snapshot binding, `operation=search|read|graph`, operation-specific selectors, an optional continuation, and `max_bytes` defaulting to 16384. The snapshot binding includes project identity, resolved commit/tree OIDs and a canonical tracked-file inventory digest. Gobby prepares the managed snapshot in 2.1; this library verifies it before serving evidence.

Search selectors include lane (`symbol`, `literal`, `regex`, `content`, `lexical_symbol`, `hybrid`), query and optional repo-relative path/language/kind filters. Read selectors identify a path plus inclusive one-based line range, or an exact file-qualified symbol. Graph selectors choose callers, callees, usages, imports, directed path, or scoped view; include source/target selectors, direction and depth (default two). Reject incompatible or missing selectors and unsafe paths before accessing stores.

Read exact bytes from pinned Git blobs. A complete snapshot file-state inventory defines visibility: facts must match the manifest's path and content hash, and graph facts must have matching owner hashes. Never mix in unmatched live-parent facts, use stale byte-range clamping, or interpret incomplete index preparation as an empty repository. Reject missing Git objects, changed manifests and mismatches with typed errors. Report gitlinks, symlinks, binary and unsupported paths as explicit exclusions; do not traverse or fetch their targets.

Each source item contains `evidence_id`, repo-relative path, blob OID, content/excerpt hashes, qualified symbol name when available, one-based inclusive line bounds, zero-based half-open byte bounds and exact excerpt. Derive stable IDs from canonical snapshot/source/range/content data, never timestamps or run IDs. Graph items add typed relation, direction, endpoints, owner identity and extracted/inferred/unresolved provenance. Responses contain canonical request/binding, lane/tool/contract identity, items, `complete`, applied bounds, warnings and continuation. Canonical ordering breaks ties by stable path/range/identity. Paginate whole items; an oversized item returns a typed narrowing requirement, not clipped source. A continuation is bound to the same request and snapshot.

Deterministic mode uses no semantic lane. Hybrid is admitted only with verified effective embedding endpoint identity, model, dimension and index identity recorded in the response. Reject missing/changed identity and semantic failures explicitly; no silent route substitution. Distinguish a complete empty result, excluded scope, truncated traversal, and unavailable index/graph service. Search absence alone never proves a repository-wide negative fact.

**Acceptance:**
- 1.1.1 - Exact ranges, hashes and stable IDs resolve against pinned Git blobs; dirty/live-parent changes, stale offsets and unsafe paths cannot contaminate evidence. file: `crates/gcode/src/evidence/snapshot.rs`.
- 1.1.2 - All declared search/read/graph operations return deterministic whole-item pages with honest completeness, direction and provenance. file: `crates/gcode/src/evidence/mod.rs`.
- 1.1.3 - Focused evidence tests cover repeatability, continuation binding, rename/deletion, missing blobs, symlink/gitlink/binary exclusions, graph truncation and audited hybrid failure. test: `crates/gcode/src/evidence/tests.rs::test_pinned_evidence_contract`.

### 1.2 Expose the deterministic evidence CLI and contract [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `crates/gcode/src/cli.rs::*` — scope-reason: add evidence command parsing and classify it across command format/navigation predicates.
- `crates/gcode/src/dispatch.rs::*` — scope-reason: extract dispatch branches and integrate the new evidence command with typed errors and service selection.
- `crates/gcode/src/dispatch_navigation.rs`
- `crates/gcode/src/commands/evidence.rs`
- `crates/gcode/src/commands/mod.rs`
- `crates/gcode/src/lib.rs::*` — scope-reason: declare the extracted dispatch module and evidence command integration.
- `crates/gcode/tests/evidence.rs`
- `crates/gcode/tests/contract.rs::*` — scope-reason: extend existing native command contract assertions for the additive evidence command.
- `tests/contracts/gcode.contract.json::*` — scope-reason: regenerate the complete vendored native command contract after adding evidence.
- `docs/contracts/gcode-cli.md`

Expose `gcode evidence --request-json '<request>'` with the complete v1 request/response contract from 1.1 and JSON output. Existing global project selection resolves the managed snapshot context. This surface performs deterministic reads, never agent execution or automatic source mutation. The caller prepares/indexes snapshots before querying. Explicitly reject stale-admission bypasses for evidence.

Split navigation dispatch from `crates/gcode/src/dispatch.rs` into `crates/gcode/src/dispatch_navigation.rs` before adding evidence handling, retaining typed error behavior and staying below 1000 production lines. Keep CLI argument structs for growing new surfaces in focused modules when required. Refresh the vendored CLI contract through its normal generator and document input validation, provenance, pagination and error/exit behavior. Preserve existing commands' observable behavior and contract versioning conventions.

**Acceptance:**
- 1.2.1 - Evidence CLI invokes the deterministic library, reports typed admission/query failures and cannot dispatch a model or mutate source. file: `crates/gcode/src/commands/evidence.rs`.
- 1.2.2 - Native subprocess/contract checks pass and the vendored command contract matches the rebuilt CLI; dispatch remains below the ceiling. test: `crates/gcode/tests/evidence.rs::test_evidence_cli_contract`.
- 1.2.3 - Evidence invocation and source/provenance semantics are documented. file: `docs/contracts/gcode-cli.md`.

## P2: Gobby run state, validation and native-agent orchestration
`kind: framing`

### 2.1 Implement Ask snapshots, persisted runs and evidence admission [category: code] (depends: 1.2)
`kind: deliverable`

Targets:
- `src/gobby/ask/__init__.py`
- `src/gobby/ask/contracts.py`
- `src/gobby/ask/snapshots.py`
- `src/gobby/ask/storage.py`
- `src/gobby/ask/evidence.py`
- `src/gobby/ask/artifacts.py`
- `tests/ask/test_snapshots.py`
- `tests/ask/test_storage.py`
- `tests/ask/test_evidence.py`

Create typed Ask request, binding, evidence-reference and run-result models. Start inputs are question, project, commit ref (default HEAD), positive timeout_seconds (default 600), retrieval mode (default deterministic), investigator/reviewer profile identifiers and optional idempotency key. Resolve the commit and profile identities once. The pipeline execution ID is the run ID; use existing pipeline inputs/step outputs/status records, not a new database table or task lifecycle. Persist an immutable deadline_at and pipeline/profile snapshots before paid work.

Prepare one daemon-owned managed isolated checkout of the selected commit, with a complete gcode index file-state inventory and appropriate managed grant. Dirty input checkout files are excluded and original checkout refs/content are untouched. Retain the snapshot while a run can resume, release it through managed lifecycle cleanup after terminal publication/cancellation, and preserve the evidence/export artifacts. Do not depend on a mutable parent graph. A restart must verify manifest hashes before reusing source state; reconstruct only the same pinned snapshot if local snapshot files were lost.

Store artifacts under the existing machine-local Gobby state root, in `ask/<project-id>/<run-id>/`, with owner-only access. Pipeline state is authoritative for phase, ownership and artifact pointers. Use immutable artifact bodies and atomic manifest publication; stage checkpoints contain hashes and references, not another independent state machine. Persist each evidence invocation and response before returning it to an agent. Serialize checkpoint updates with the existing database transaction boundary so concurrent queries cannot lose evidence records.

Provide a run-scoped evidence admission function that fixes snapshot identity, retrieval mode and permitted operation; invokes installed gcode with structured argv, not a shell; and enforces result completeness/identity. Bound each subprocess by remaining stage time and page size, never by a total call count. Errors and timeouts remain in the record. Record effective identities and available usage without fabricating unknown telemetry. Redact credentials and deny sensitive/out-of-root paths before extraction; excluded content remains an explicit evidence limitation.

**Acceptance:**
- 2.1.1 - Run persistence pins source, profiles and one deadline; independent and idempotent starts behave correctly without creating tasks or a new run table. test: `tests/ask/test_storage.py::test_pinned_idempotent_run_persistence`.
- 2.1.2 - Isolated historical-commit preparation and managed cleanup preserve a dirty caller checkout; snapshot drift/loss is diagnosed without changing the commit. test: `tests/ask/test_snapshots.py::test_historical_snapshot_isolation_and_recovery`.
- 2.1.3 - Evidence responses are scoped and durably recorded before delivery, including concurrent requests, failures and provenance; more than 30 queries are allowed within time. test: `tests/ask/test_evidence.py::test_durable_scoped_evidence_admission`.

### 2.2 Implement claim validation and atomic verified publication [category: code] (depends: 2.1)
`kind: deliverable`

Targets:
- `src/gobby/ask/claims.py`
- `src/gobby/ask/validation.py`
- `src/gobby/ask/publication.py`
- `src/gobby/ask/export.py`
- `tests/ask/test_validation.py`
- `tests/ask/test_publication.py`
- `tests/ask/test_export.py`

Define answer sections as ordered references to claims. Each claim has id, classification (`direct|inferred|unknown`), statement, citations, premise_claim_ids and rationale. A citation references recorded evidence_id and exact subrange. Direct/inferred claims require citations; unknown entries describe unresolved questions and evidence limits. Direct means source explicitly establishes the statement; inferred means stated reasoning follows from cited/accepted premises; missing search hits cannot prove absence. Exhaustive/negative statements need evidence complete for their declared scope. Graph association cannot become a direct call-path assertion.

Deterministically validate schema, run/snapshot membership, paths, ranges, hashes, graph ownership and acyclic premise references against pinned blobs. Separately consume reviewer verdicts per claim and question coverage. A reviewer result includes draft hash, evidence manifest hash, accepted/rejected verdicts, classification/support diagnostics, missing question parts and rationale. Reviewer approval is an interpretive assessment, not a proof that follows from hash validity.

Publish only claim text that passes both deterministic validation and independent review; inferred claims also require accepted premises. Render Markdown solely from accepted claims and section structure, with deterministic unknown entries for unresolved parts; do not append unreviewed generated summaries. Outcomes are complete, partial or unknown. Missing mandatory review produces a failed run with retained evidence, never an unreviewed answer. Repair is orchestrated in 2.3; this module evaluates each immutable draft/review version.

Publish answer.json, answer.md, evidence records, exact cited source excerpts and an artifact manifest with hashes, request/binding/profile/tool identities, drafts, validation/review diagnostics and attempt history. Use relative links to bundled evidence plus explicit commit/path/range references, never machine-local absolute source links. Mermaid, when present, is rendered from recorded typed graph facts only. Atomic publication is idempotent; export never silently overwrites an existing destination. Replay checks stored hashes and re-renders without model calls. A fresh model run has a new identity and may produce different prose.

**Acceptance:**
- 2.2.1 - Fabricated/cross-run citations, altered hashes, stale ranges, unsupported direct labels and invalid premise graphs fail the appropriate validation/review gate. test: `tests/ask/test_validation.py::test_claim_validation_and_review_gates`.
- 2.2.2 - Complete/partial/unknown outcomes include only reviewed claims with accepted premises; absent review withholds publication. test: `tests/ask/test_publication.py::test_only_reviewed_claims_are_published`.
- 2.2.3 - Stored answer replay is byte-stable; exports have portable valid citation links, exact excerpts, provenance and collision protection. test: `tests/ask/test_export.py::test_portable_byte_stable_export`.

### 2.3 Orchestrate native Ask agents with deadlines and recovery [category: code] (depends: 2.2)
`kind: deliverable`

Targets:
- `src/gobby/ask/service.py`
- `src/gobby/ask/stages.py`
- `src/gobby/ask/agents.py`
- `src/gobby/ask/permissions.py`
- `src/gobby/ask/recovery.py`
- `src/gobby/install/shared/workflows/pipelines/ask.yaml`
- `src/gobby/install/shared/workflows/agents/ask-investigator.yaml`
- `src/gobby/install/shared/workflows/agents/ask-reviewer.yaml`
- `tests/ask/test_pipeline.py`
- `tests/ask/test_permissions.py`
- `tests/ask/test_recovery.py`

Use the existing pipeline executor, managed native agent spawning and event-driven completion waits. Define explicit stages: bind/prepare; deterministic seed queries; spawn/wait investigator and collect draft; deterministic citation validation; spawn/wait fresh reviewer; optionally spawn/wait one fresh repair investigator and repeat validation/review; publish. Do not invoke tool_chat, generic prompt-generation steps, task dispatch or gobby build. Profiles resolve through the existing agent registry; snapshot the effective definitions and provider/model/reasoning per run. Investigator/reviewer have separate sessions. Repair uses the investigator profile in another fresh session. The reviewer receives the question, immutable draft and recorded evidence, not private investigator reasoning.

Ask agents use managed scratch workspaces and an exact MCP allowlist for run-scoped evidence retrieval, draft/review submission and self lifecycle completion. Block native shell, unrestricted file reads, writes, web, task mutations and descendant spawning. Source snapshots remain outside agent writable roots. Enforce the policy with managed grants, MCP principal/phase checks, provider controls and restrictive sandbox configuration; preflight and refuse any selected runtime that cannot enforce it. Repository instructions are evidence and never become permission-bearing prompts. Submission requires the current run/stage/attempt principal and draft/evidence hashes; an agent exit alone is not valid output.

One absolute deadline includes preparation, all agents, review, repair and publication. Default 600 seconds, no query/turn/cumulative token cap. Reserve the final 60 seconds for mandatory review; investigator/repair timeouts stop before that reserve. Permit one repair only when time remains for it plus review. At expiry stop scheduling, revoke active submission authority and terminate children; bounded process cleanup is recorded separately. A partial answer requires completed review of its published claims. Waiting for agent slots also consumes the original deadline.

Use existing pipeline statuses with separate answer outcome; typed errors include deadline_exceeded, snapshot_mismatch, agent_failed and validation_failed. Durable stage outputs retain child run IDs, submission versions and evidence references. Make spawn identity run/stage/attempt-idempotent. Recovery reattaches surviving children or restarts only the interrupted attempt, preserving source, accepted checkpoints and deadline; reject superseded late submissions. Expired/cancelled runs require a new run. Resume failed/interrupted execution through an Ask-specific guard compatible with existing pipeline resume rules, without broadening generic resume semantics. Client disconnect leaves the run active; explicit cancellation cascades to children. Emit observable stage/evidence/repair/cancellation events and replayable checkpoints, including fault-injection hooks at each durable boundary for tests.

**Acceptance:**
- 2.3.1 - Pipeline integration uses native managed sessions, fresh reviewer/repair contexts, durable submissions and at most one repair, with no tool_chat or task creation. test: `tests/ask/test_pipeline.py::test_native_investigation_review_and_single_repair`.
- 2.3.2 - Permission probes deny mutations, native execution, web, cross-run access, stale submissions and repository prompt injection; unenforceable runtime profiles fail preflight. test: `tests/ask/test_permissions.py::test_ask_agent_permission_boundary`.
- 2.3.3 - Deadline, client disconnect, cancellation and stage-boundary fault injection preserve checkpoints and prevent duplicate agents/publication or budget resets. test: `tests/ask/test_recovery.py::test_deadline_and_stage_boundary_recovery`.

## P3: Public Ask surfaces and cohort comparison
`kind: framing`

### 3.1 Expose Ask through gcode, MCP and HTTP [category: code] (depends: 2.3)
`kind: deliverable`

Targets:
- `crates/gcode/src/cli.rs::*` — scope-reason: integrate Ask action parsing and its distinct format/navigation classification across the command enum.
- `crates/gcode/src/cli/ask.rs`
- `crates/gcode/src/dispatch.rs::*` — scope-reason: route Ask through daemon delegation and classify its typed lifecycle failures.
- `crates/gcode/src/dispatch_ask.rs`
- `crates/gcode/src/lib.rs::*` — scope-reason: declare the thin Ask dispatch/client modules alongside existing CLI declarations.
- `crates/gcode/src/ask_client.rs`
- `crates/gcode/tests/ask.rs`
- `crates/gcode/tests/contract.rs::*` — scope-reason: extend native contract assertions for all Ask actions.
- `tests/contracts/gcode.contract.json::*` — scope-reason: regenerate the complete vendored native command contract after adding Ask actions.
- `src/gobby/mcp_proxy/tools/ask.py`
- `src/gobby/mcp_proxy/registries.py::*` — scope-reason: import and register the Ask tool registry with the shared daemon Ask service.
- `src/gobby/servers/routes/ask.py`
- `src/gobby/servers/_app_routes.py::*` — scope-reason: import and register the new Ask HTTP router.
- `tests/mcp_proxy/tools/test_ask.py`
- `tests/servers/routes/test_ask.py`
- `docs/contracts/gcode-cli.md`
- `docs/contracts/ask.md`
- `docs/guides/ask.md`

Expose `gcode ask "<question>" [--commit REF] [--timeout-seconds N] [--retrieval deterministic|hybrid] [--background] [--format text|json]`. Reuse global project selection. Alternative mutually exclusive actions are `gcode ask --status RUN_ID`, `--resume RUN_ID`, `--cancel RUN_ID`, and `--export RUN_ID --output DIR`; reject contradictory start/control arguments. Default foreground prints the durable run identity and waits through event-driven completion. Default source is HEAD, timeout 600, retrieval deterministic; no query/turn-count option or hidden cap. Successful complete/partial/unknown records exit zero and expose outcome distinctly; failed/cancelled runs return typed nonzero results. A transport disconnect returns the resumable ID without cancelling the daemon run.

Keep native CLI behavior in focused modules. Move new Ask dispatch logic from `crates/gcode/src/dispatch.rs` to `crates/gcode/src/dispatch_ask.rs`, retaining the decomposition from 1.2. Gcode delegates lifecycle operations to Gobby without acquiring retrieval datastores for a status/cancel/export operation. Use authenticated existing daemon transport conventions, never a direct model fallback.

Register gobby-ask operations start_ask_run, get_ask_run, wait_for_ask_run, resume_ask_run, cancel_ask_run and export_ask_run. Add internal stage operations for prepare/seed/spawn/admit/validate/publish and agent-facing query_evidence, submit_answer and submit_review. Restrict internal stages to the owning pipeline and submissions to the active child principal. All entry points share AskService and authenticated project authorization.

HTTP routes: POST /api/ask/runs (202 plus run_id/status/deadline), GET /api/ask/runs/{id}, GET /api/ask/runs/{id}/wait, POST /api/ask/runs/{id}/resume, POST /api/ask/runs/{id}/cancel, GET /api/ask/runs/{id}/export. MCP wait and HTTP wait subscribe to existing completion events and recheck durable status after subscription to avoid lost wakeups. The run response carries pipeline status, stage, answer outcome, typed error, deadline, identities and artifact manifest reference. Export streams the existing immutable bundle; only the explicit CLI output destination creates local export files.

Document source binding, evidence/claim schemas, direct/inferred/unknown semantics, native-agent ownership, deadline and review reservation, repair, cancellation/recovery, provenance, security and replay. Refresh CLI contract and install changed native binaries through the existing atomic installer. Synchronize templates normally from the serving checkout and verify installed DB definitions; do not infer live enforcement from bundled YAML.

**Acceptance:**
- 3.1.1 - CLI/MCP/HTTP return the same run identity, status, outcome, deadline and error semantics; foreground waits are event-driven and disconnect does not cancel. test: `tests/servers/routes/test_ask.py::test_shared_run_contract_and_event_driven_wait`.
- 3.1.2 - Public and stage MCP operations enforce project/run/attempt authorization and progressive discovery. test: `tests/mcp_proxy/tools/test_ask.py::test_ask_authorization_and_discovery`.
- 3.1.3 - Native CLI parsing, daemon delegation, lifecycle/export behavior and regenerated contract pass against the rebuilt installed binary. test: `crates/gcode/tests/ask.rs::test_ask_cli_lifecycle_contract`.
- 3.1.4 - The complete Ask contract and operational guide document the agreed design and installed template/profile behavior. file: `docs/contracts/ask.md`.

### 3.2 Evaluate native Ask against all 14 frozen cohort questions [category: test] (depends: 3.1)
`kind: deliverable`

Targets:
- `docs/evidence/wiki-bakeoff-code-2026-09/ask_cohort.py`
- `docs/evidence/wiki-bakeoff-code-2026-09/ask_scoring.py`
- `docs/evidence/wiki-bakeoff-code-2026-09/test_ask_cohort.py`
- `docs/evidence/wiki-bakeoff-code-2026-09/ask-pipeline.md`

Build and execute a standalone native Ask evaluation, not a replacement for sibling implementation tests. Reuse the frozen matrix, prompts, gold source spans, corpus isolation and evidence boundaries. Q01-Q13 bind to 0216f1e33f05962d49467d95fe84609041c6dba8; Q14 binds to 8b24ac26699aac8b24254a647aa70b208287b492. Read the original report for baseline gold-span hits: Q01, Q07, Q10, Q11, Q12 and Q13. Preserve all original 14 question strings and their source commits; external answer keys must never be copied into the agent corpus or prompts.

Run one primary native Ask invocation per question with the same pinned investigator/reviewer profile pair, deterministic retrieval and 600-second whole-run budget, no query/turn-count cap. Freeze and report effective tool, provider/model/reasoning and source identities before execution. Run serially against isolated services/state, without touching the user's daemon database. Preserve every invocation, interruption and error; retries are separately identified and never replace the primary result. Agentic query reformulation is part of Ask, while the initial user question stays exact. A hybrid follow-up is a separately labelled optional experiment, never substituted for the primary deterministic result.

Score retrieval and answers separately. Retrieval reports frozen-gold-span overlap before and after follow-up queries, reciprocal rank where ranking exists, query count, scope/completeness and change from 6/14. Answer scoring reports gold-key coverage, exact values, source-supported claim precision, citation integrity, direct/inferred/unknown accuracy and honest abstention; map the cohort's A/ambiguous label to unknown with its reason. Verify the Q14 nine-path change and case-sensitive longest-prefix precedence, automatic/non-excluded scope, Hobby Supplies=2 and `Sleeves: `=4. Do not compare new answer accuracy to the historical 6/14 retrieval score.

Emit a per-question before/after table, raw result/evidence inventory with hashes, aggregate counts, latency and available usage, and an explicit conclusion: improved, unchanged, regressed or blocked. Claim retrieval improvement only above six supported questions; show lost baseline hits even if the total increases. A correct unknown is not a retrieval success. Claim answer quality only from gold-key and source review, not mere relevant filenames or valid hashes. Record all unsupported statements and missing exact values. A negative comparison is a valid evaluation result when faithfully reported; never rerun selectively or weaken gold criteria to manufacture improvement. Actual incorrect Ask behavior found by the evaluation follows the repository's found-work workflow.

**Acceptance:**
- 3.2.1 - Harness contract tests prove exactly 14 unchanged prompts, correct per-question commits, answer-key isolation, preserved primary attempts and separate retrieval/answer metrics. test: `docs/evidence/wiki-bakeoff-code-2026-09/test_ask_cohort.py::test_frozen_cohort_and_scoring_contract`.
- 3.2.2 - All 14 native primary runs are accounted for with immutable results/provenance, or explicit typed blocked/failure dispositions without fabricated measurements. file: `docs/evidence/wiki-bakeoff-code-2026-09/ask-pipeline.md`.
- 3.2.3 - The report compares supporting-span retrieval to 6/14, identifies baseline losses, scores answers independently, checks Q14 exact values and states whether the result is better. file: `docs/evidence/wiki-bakeoff-code-2026-09/ask-pipeline.md`.

## V1 Verification and rollout
`kind: verification`

Run focused native evidence/Ask/contract checks and protected Python tests for the owning leaf. Use uv for Python and isolated test DSNs; never run the full pytest suite. Verify deterministic evidence and answer replay, dirty/historical source isolation, independent native agent review, strict permissions, more than 30 queries within time, one repair, immutable deadlines and fault injection at every durable boundary. Rebuild/install Rust changes through the existing new-inode installer and verify native/vendored contracts. Validate the installed pipeline/profile rows after sync. Run the serial 14-question native evaluation only after implementation is complete.

Preparation completed: draft/expansion validation and server-derived M1 produced epic #22010 with three phase epics and seven leaves; compiled/applied expansion passed. The subsequent implementation goal supersedes the preparation-only stop. Execute leaves in dependency order through manually managed workers, perform coordinator QA, land the worktree through Gobby tools, coordinate a quiet daemon cutover and verify installed binaries/live commands. Account for all 14 primary cohort attempts and every repair iteration; finish only when a complete pinned 14-question run has source-supported successful answers. Dispatcher automation remains disabled and `gobby build` is not used.

## M1 Task Manifest
`kind: manifest`

```yaml
- title: Implement pinned evidence queries and source provenance
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: '1.1.1: Exact ranges, hashes and stable IDs resolve against
    pinned Git blobs; dirty/live-parent changes, stale offsets and unsafe paths cannot
    contaminate evidence. file: `crates/gcode/src/evidence/snapshot.rs`.

    1.1.2: All declared search/read/graph operations return deterministic whole-item
    pages with honest completeness, direction and provenance. file: `crates/gcode/src/evidence/mod.rs`.

    1.1.3: Focused evidence tests cover repeatability, continuation binding, rename/deletion,
    missing blobs, symlink/gitlink/binary exclusions, graph truncation and audited
    hybrid failure. test: `crates/gcode/src/evidence/tests.rs::test_pinned_evidence_contract`.'
  labels:
  - covers:gcode-agentic-ask-pipeline:1.1:1.1.1
  - covers:gcode-agentic-ask-pipeline:1.1:1.1.2
  - covers:gcode-agentic-ask-pipeline:1.1:1.1.3
  tdd: true
  source_section: '1.1'
  implementation_domain: backend
- title: Expose the deterministic evidence CLI and contract
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  validation_criteria: '1.2.1: Evidence CLI invokes the deterministic library, reports
    typed admission/query failures and cannot dispatch a model or mutate source. file:
    `crates/gcode/src/commands/evidence.rs`.

    1.2.2: Native subprocess/contract checks pass and the vendored command contract
    matches the rebuilt CLI; dispatch remains below the ceiling. test: `crates/gcode/tests/evidence.rs::test_evidence_cli_contract`.

    1.2.3: Evidence invocation and source/provenance semantics are documented. file:
    `docs/contracts/gcode-cli.md`.'
  labels:
  - covers:gcode-agentic-ask-pipeline:1.2:1.2.1
  - covers:gcode-agentic-ask-pipeline:1.2:1.2.2
  - covers:gcode-agentic-ask-pipeline:1.2:1.2.3
  tdd: true
  source_section: '1.2'
  implementation_domain: backend
- title: Implement Ask snapshots, persisted runs and evidence admission
  category: code
  task_type: feature
  depends_on:
  - '1.2'
  validation_criteria: '2.1.1: Run persistence pins source, profiles and one deadline;
    independent and idempotent starts behave correctly without creating tasks or a
    new run table. test: `tests/ask/test_storage.py::test_pinned_idempotent_run_persistence`.

    2.1.2: Isolated historical-commit preparation and managed cleanup preserve a dirty
    caller checkout; snapshot drift/loss is diagnosed without changing the commit.
    test: `tests/ask/test_snapshots.py::test_historical_snapshot_isolation_and_recovery`.

    2.1.3: Evidence responses are scoped and durably recorded before delivery, including
    concurrent requests, failures and provenance; more than 30 queries are allowed
    within time. test: `tests/ask/test_evidence.py::test_durable_scoped_evidence_admission`.'
  labels:
  - covers:gcode-agentic-ask-pipeline:2.1:2.1.1
  - covers:gcode-agentic-ask-pipeline:2.1:2.1.2
  - covers:gcode-agentic-ask-pipeline:2.1:2.1.3
  tdd: true
  source_section: '2.1'
  implementation_domain: backend
- title: Implement claim validation and atomic verified publication
  category: code
  task_type: feature
  depends_on:
  - '2.1'
  validation_criteria: '2.2.1: Fabricated/cross-run citations, altered hashes, stale
    ranges, unsupported direct labels and invalid premise graphs fail the appropriate
    validation/review gate. test: `tests/ask/test_validation.py::test_claim_validation_and_review_gates`.

    2.2.2: Complete/partial/unknown outcomes include only reviewed claims with accepted
    premises; absent review withholds publication. test: `tests/ask/test_publication.py::test_only_reviewed_claims_are_published`.

    2.2.3: Stored answer replay is byte-stable; exports have portable valid citation
    links, exact excerpts, provenance and collision protection. test: `tests/ask/test_export.py::test_portable_byte_stable_export`.'
  labels:
  - covers:gcode-agentic-ask-pipeline:2.2:2.2.1
  - covers:gcode-agentic-ask-pipeline:2.2:2.2.2
  - covers:gcode-agentic-ask-pipeline:2.2:2.2.3
  tdd: true
  source_section: '2.2'
  implementation_domain: backend
- title: Orchestrate native Ask agents with deadlines and recovery
  category: code
  task_type: feature
  depends_on:
  - '2.2'
  validation_criteria: '2.3.1: Pipeline integration uses native managed sessions,
    fresh reviewer/repair contexts, durable submissions and at most one repair, with
    no tool_chat or task creation. test: `tests/ask/test_pipeline.py::test_native_investigation_review_and_single_repair`.

    2.3.2: Permission probes deny mutations, native execution, web, cross-run access,
    stale submissions and repository prompt injection; unenforceable runtime profiles
    fail preflight. test: `tests/ask/test_permissions.py::test_ask_agent_permission_boundary`.

    2.3.3: Deadline, client disconnect, cancellation and stage-boundary fault injection
    preserve checkpoints and prevent duplicate agents/publication or budget resets.
    test: `tests/ask/test_recovery.py::test_deadline_and_stage_boundary_recovery`.'
  labels:
  - covers:gcode-agentic-ask-pipeline:2.3:2.3.1
  - covers:gcode-agentic-ask-pipeline:2.3:2.3.2
  - covers:gcode-agentic-ask-pipeline:2.3:2.3.3
  tdd: true
  source_section: '2.3'
  implementation_domain: backend
- title: Expose Ask through gcode, MCP and HTTP
  category: code
  task_type: feature
  depends_on:
  - '2.3'
  validation_criteria: '3.1.1: CLI/MCP/HTTP return the same run identity, status,
    outcome, deadline and error semantics; foreground waits are event-driven and disconnect
    does not cancel. test: `tests/servers/routes/test_ask.py::test_shared_run_contract_and_event_driven_wait`.

    3.1.2: Public and stage MCP operations enforce project/run/attempt authorization
    and progressive discovery. test: `tests/mcp_proxy/tools/test_ask.py::test_ask_authorization_and_discovery`.

    3.1.3: Native CLI parsing, daemon delegation, lifecycle/export behavior and regenerated
    contract pass against the rebuilt installed binary. test: `crates/gcode/tests/ask.rs::test_ask_cli_lifecycle_contract`.

    3.1.4: The complete Ask contract and operational guide document the agreed design
    and installed template/profile behavior. file: `docs/contracts/ask.md`.'
  labels:
  - covers:gcode-agentic-ask-pipeline:3.1:3.1.1
  - covers:gcode-agentic-ask-pipeline:3.1:3.1.2
  - covers:gcode-agentic-ask-pipeline:3.1:3.1.3
  - covers:gcode-agentic-ask-pipeline:3.1:3.1.4
  tdd: true
  source_section: '3.1'
  implementation_domain: backend
- title: Evaluate native Ask against all 14 frozen cohort questions
  category: test
  task_type: task
  depends_on:
  - '3.1'
  validation_criteria: '3.2.1: Harness contract tests prove exactly 14 unchanged prompts,
    correct per-question commits, answer-key isolation, preserved primary attempts
    and separate retrieval/answer metrics. test: `docs/evidence/wiki-bakeoff-code-2026-09/test_ask_cohort.py::test_frozen_cohort_and_scoring_contract`.

    3.2.2: All 14 native primary runs are accounted for with immutable results/provenance,
    or explicit typed blocked/failure dispositions without fabricated measurements.
    file: `docs/evidence/wiki-bakeoff-code-2026-09/ask-pipeline.md`.

    3.2.3: The report compares supporting-span retrieval to 6/14, identifies baseline
    losses, scores answers independently, checks Q14 exact values and states whether
    the result is better. file: `docs/evidence/wiki-bakeoff-code-2026-09/ask-pipeline.md`.'
  labels:
  - covers:gcode-agentic-ask-pipeline:3.2:3.2.1
  - covers:gcode-agentic-ask-pipeline:3.2:3.2.2
  - covers:gcode-agentic-ask-pipeline:3.2:3.2.3
  tdd: false
  source_section: '3.2'
  assigned_agent: backend-developer
```
