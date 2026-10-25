# Skills family debt pass — #24959

Baseline: main `0.5.0`, HEAD `2e7da824431fd49fa7161a5c197e4c2a2516e5e1`;
plan #24950 at `d26dba5591bac3c59e3b32b98bf767dcb6a6f815`.
Authority: memory `9066f1eb-9ae0-5539-9d8c-8c694e035a7f` and Josh's requirement
to record deliberately waived behavior. These are research proposals, not
approved changes or implemented fixes.

## Debt not to port

Keep all 54 public Skills surfaces. No whole-surface drop is recommended.
Preserve scope precedence, bounded ingestion, companion scanning, external-source
fail-closed scanning, lossless delivery, soft deletion and machine-local effects.

Every drop below needs the shared behavior-waiver mechanism proposed in S1.
The waiver IDs below are proposed identifiers, not existing approvals. Each
must cite the approved decision reference and this report, retain its surface
as `delegated`, and link old/new cases and native effect assertions. Until the
shared mechanism and decision exist, ordinary parity failures remain failures.
A prose note or broad corpus mask is insufficient. Amend Decision Record 7,
P0/0.1, the cited leaves/M1 entries, and P6/6.1–6.2 accordingly.

### D1. Drop omission-as-clear in HTTP updates

- **Python behavior/evidence:** `SkillUpdateRequest` defaults optional fields
  to `None` (`src/gobby/servers/routes/skills.py:103`). The handler forwards
  every field (`:636`, especially `:644`), through `SkillManager.update_skill`
  (`src/gobby/skills/manager.py:277`). Storage distinguishes `_UNSET` from
  explicit `None` (`src/gobby/storage/skills/_metadata.py:364`) and clears
  nullable fields on `None`. Thus a content-only edit also clears version,
  license, compatibility, allowed tools and metadata.
- **Why debt / call:** **Drop** destructive omission handling; it contradicts
  the plan's partial-update/null distinction and can remove runtime/level
  metadata during unrelated edits. Preserve absent fields; clear nullable
  fields only on explicit null. Keep nonnullable-field validation.
- **Amend:** 1.2.2, 0.1 and 4.1.15. Add content-only, explicit-null and empty
  object cases against populated records, preserving wire schema deliberately.
- **Parity record:** proposed waiver `skills.http-update-omitted-fields`,
  scoped to `HTTP PUT /api/skills/{skill_id}` and omitted nullable fields.
  Record old cleared versus new retained persisted state. Do not mask these
  fields from all update comparisons.

### D2. Drop caller-forgeable completion cursors

- **Python behavior/evidence:** cursors are unsigned base64 JSON
  (`src/gobby/mcp_proxy/tools/skills/_paging.py:42`). Decode validates shape,
  kind, hash and nonnegative offset (`:47`), without issuance/progression checks.
  `build_content_page` accepts an offset equal to content byte length (`:110`,
  `:117`), producing an empty completed page. Handlers record completion on
  that flag (`src/gobby/mcp_proxy/tools/skills/get_skill.py:409`, `:581`), with
  usage/session-variable effects in `:232`. An observed cursor supplies the
  identity/hash needed to change its offset to EOF.
- **Why debt / call:** **Drop** forged/cross-session completion as evidence of
  full delivery. This weakens instruction-load tracking integrity; it does not
  establish a separate content-authorization bypass or prove a client read bytes.
- **Amend:** 1.6 and 4.1.1–4.1.2. Bind issuance and continuation to caller
  session, content hash/identity, level, view and expected progression. Reuse a
  shared primitive if available; signing alone does not establish per-session
  progression. Test tampered EOF, skipped-prefix, wrong-session and replay cases.
- **Parity record:** proposed waiver `skills.instruction-cursor-progression`,
  covering forged/replayed `get_skill` and `get_skill_file` continuations.
  Assert rejection and no load-state effects. Ordinary issued cursors,
  Unicode, budgets and stale-content errors retain ordinary parity.

### D3. Drop partial parent/companion soft deletion

- **Python behavior/evidence:** `SkillMetadataMixin.delete_skill` commits the
  parent tombstone, then calls `host.delete_skill_files`
  (`src/gobby/storage/skills/_metadata.py:502`, `:528`). Companion deletion
  opens another transaction (`src/gobby/storage/skills/_files.py:574`). A
  failure leaves a deleted parent with live companion rows and no notification.
- **Why debt / call:** **Drop** partial deletion; keep soft-delete/restore
  semantics with one transaction. The plan already chooses this improved
  boundary; blanket Python parity still needs an explicit failure-case waiver.
- **Amend:** 1.2.3, 1.2.7 and 1.5. Inject companion-delete failure; assert
  parent/file visibility remains coherent and no event is published.
- **Parity record:** proposed waiver `skills.atomic-soft-delete`, limited to
  failure checkpoints of HTTP DELETE, MCP `remove_skill`, and CLI `skills
  remove` where they reach this storage path. Compare effects as well as
  responses. Successful deletion remains ordinary parity.

### D4. Drop compensating writes after pre-mutation refresh failures

- **Python behavior/evidence:** `SkillUpdater.update_skill` takes a backup
  before loading, then restores it on fetch/parse/validation/scan failures
  (`src/gobby/skills/updater.py:131`, `:230`, `:241`). `_restore_backup` writes
  metadata (`:326`), though these failures precede `_apply_update` (`:492`).
  Storage advances `updated_at` and notifies search on that write
  (`src/gobby/storage/skills/_metadata.py:364`). Results can report rollback
  even when acquisition never changed registry data.
- **Why debt / call:** **Drop** unnecessary compensating writes and misleading
  rollback fields on pre-write failures. They manufacture update effects and
  can overwrite a concurrent successful edit with the saved snapshot. Preserve
  scan safety, diagnostics and atomic metadata/companion replacement.
- **Amend:** 2.5, 4.1.14 and 5.2.3. Fetch/validate/scan before the write
  transaction; test concurrent edits and each pre-write failure. Define honest
  backup/rollback results instead of freezing Python's accidental flags.
- **Parity record:** proposed waiver `skills.failed-refresh-no-write`, scoped
  to failed source refreshes via MCP `update_skill` and CLI `skills update`.
  Enumerate timestamp/event and result-field deltas; assert original or newer
  committed state survives. A global timestamp mask must not hide a write.

### D5. Drop raw internal exception text; keep intentional domain errors

- **Python behavior/evidence:** unexpected create/update/file-write failures
  return `detail=str(e)` (`src/gobby/servers/routes/skills.py:298`, `:661`,
  `:804`). MCP load returns `_error("internal_error", str(exc))`
  (`src/gobby/mcp_proxy/tools/skills/get_skill.py:418`). The import route in
  the same file already returns generic `Internal server error` (`:474`).
- **Why debt / call:** **Drop** internal exception formatting as a public
  contract: filesystem/database/provider internals should remain private.
  **Keep** safe validation, conflict, not-found and scan messages/codes. No
  observed secret disclosure is claimed.
- **Amend:** 0.1, 1.1–1.5, 2.3–2.5, 4.1.15 and 6.1 through shared S3. Reuse
  envelope shapes; return stable safe unexpected-error details and log diagnostics.
- **Parity record:** proposed waiver `skills.internal-error-redaction`,
  limited to enumerated unexpected-failure cases and message/detail paths.
  Retain status/code/envelope and no-effect assertions; never waive all errors.

### D6. Drop blocking implementation; keep response and ordering contracts

- **Python behavior/evidence:** async HTTP create/update call synchronous
  manager/storage code (`src/gobby/servers/routes/skills.py:271`, `:636`),
  while file writes use `run_in_threadpool` (`:804`). Search's sync adapter
  creates an executor and blocks on `future.result()` inside a running event
  loop (`src/gobby/skills/search.py:214`).
- **Why debt / call:** **Drop** request-executor blocking; **keep** transaction
  ordering, freshness and success semantics. Planned native async services and
  shared pool already supply the mechanism; no new worker framework is needed.
- **Amend:** 1.1, 1.7 and 6.1. Verify bounded progress/cancellation with slow
  database/index work rather than reproducing serialized request stalls.
- **Parity record:** proposed waiver `skills.nonblocking-service-execution`
  records scheduling changes on affected HTTP/search surfaces. Response/state
  parity stays exact; progress checks are separate and suppress no semantic diff.

## Systemic issues

### S1. Shared behavior-waiver support is missing

Today's ledger and compare gates do **not** implement a cited behavior-level
waiver honored across parity checks. #24954 at `9c2de16b8e` adds useful notes
and exit-rule wording, without a machine-readable behavior-waiver contract.

**Evidence:**

- `scripts/generate_parity_ledger.py:149` permits a status string or exactly
  `status` and `note` (`:167`); statuses are `proxied`, `delegated`, or
  `dropped by decision #N` (`:176`). `tests/test_parity_ledger.py:186` rejects
  extra object fields. Notes are escaped prose, not comparator selectors.
- `render_ledger` keeps absent whole-surface drops (`:186`) and documents the
  exit rule (`:209`). A delegated row can cite a waiver in its note, but no
  consumer interprets that citation as permission for a particular difference.
- `crates/gdaemon/src/front_door/routes.rs:333` compares status, selected
  headers and JSON/bytes without waiver lookup. Non-GET/HEAD comparison calls
  `check_proxied` (`:186`); its default is `Skipped`
  (`crates/gcore/src/route_family.rs:39`).
- `crates/gdaemon/src/front_door/compare.rs:97` fails Diff/NativeError/Mismatch,
  but accepts nonempty all-Skipped reports. Such reports cannot prove readiness.
- `tests/contracts/http_corpus.py:123`, `:200`, `:260` provide JSON-pointer
  masks/replay. Masks are for volatile fields (`tests/contracts/http/README.md:45`),
  with no decision reference or old/new semantic assertion. Live compare does
  not consume them. They are not the requested waiver mechanism.

**Affected families:** every ledger group under #21572/#21573: Skills/Memory;
Projects/plans/stages; Comms/Cron/hub/nodes; daemon/config/auth/infra;
Git/workspaces; rules/workflows/pipelines; Sessions/Agents; Tasks;
WebSocket/chat/voice/UI; and remainder. HTTP live compare is directly affected;
MCP/CLI/WS parity collectors need the same decisions, not separate exception lists.

**One proposed shared fix:** an Orchestrator-owned parity-policy task under
#21572 adds one versioned waiver manifest consumed by ledger validation/rendering,
characterization/replay and compare readiness. Each entry carries stable ID,
exact existing surface keys, bounded case/request selector, approved decision
reference, rationale, expected old/new outcomes/effects and native test evidence.
Rows remain `delegated` and notes link validated waiver IDs. A whole surface
removed instead gets `dropped by decision #N`, retained after regeneration.

Reject missing decisions, duplicate/unknown surfaces, stale selectors and broad
exemptions. Record accepted divergence with its waiver ID; unmatched differences
still fail. Require inventoried cases to be matched, verified or explicitly
waived; skipped cases remain incomplete readiness evidence. Test one waived
path together with an unrelated regression that must fail. Make Skills consume
this shared fix before activating drops; no Skills-only exception framework.

### S2. Optional-field presence needs a shared native convention

**Evidence:** D1 shows nullable wire defaults collapsing storage's absent/null
distinction. Existing safer precedents include `model_fields_set` in
`src/gobby/servers/routes/tasks.py:360` and `model_dump(exclude_unset=True)` in
`agents.py:472`, `rules.py:310`, `pipeline_definitions.py:341` and
`variable_definitions.py:156` under the same routes directory. These precedents
are not evidence those families share D1's bug.

**Affected families:** Skills directly; update-contract conventions also affect
Tasks, Agents, rules/workflows/pipelines, variables/config, Projects, Cron,
Memory and Comms during the native port.

**One proposed shared fix:** establish absent/null/value request-field handling
at #21570's typed descriptor/schema boundary, with reusable contract vectors
for REST/MCP and repositories. Plain `Option<T>` must not silently merge
distinct wire states. Use a shared contract/test helper, not another schema
compiler. Audit each API's intended clearing rules; waive confirmed divergences
such as D1 without inventing drops for uninvestigated families.

### S3. Unexpected-error policy varies across families

**Evidence:** D5 also occurs in Memory
(`src/gobby/servers/routes/memory.py:310`) and config/admin
(`src/gobby/servers/routes/admin/_config.py:67`); Comms sanitizes unexpected
exceptions (`src/gobby/servers/routes/communications.py:97`). Intentional
public domain errors need a different policy from unexpected failures.

**Affected families:** confirmed Skills, Memory, config/admin and Comms.
The shared native HTTP/MCP convention also affects Projects, Cron, Tasks,
Sessions/Agents, Git/workspaces, rules/workflows/pipelines and nodes as they
port; no claim is made that every listed family currently leaks details.

**One proposed shared fix:** one native error-to-transport mapper and contract
vectors. Preserve typed public domain details, log unexpected diagnostics
privately, and return stable safe details using existing envelopes. Record
actual changed cases through S1; avoid per-family sanitizers or freezing
Python exception strings.

### Validation, uncertainty and found-work handoff

- Research used repository evidence; no live daemon HTTP/DB, secret reads,
  process control, source edits or plan edits. Navigation used `gcode`; the
  unindexed ledger generator was read directly. Concurrent index refreshes
  were reported: the writer should recheck lines at implementation baseline,
  especially in families being edited concurrently.
- Protected focused run:
  `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/test_parity_ledger.py tests/mcp_proxy/tools/skills/test_skill_delivery.py -q --no-cov`.
  **40 passed, 1 failed**: `test_committed_ledger_matches_current_public_inventory`.
- Scoped rerun of the same two files with
  `-k 'not committed_ledger_matches_current_public_inventory'` passed all
  **40** remaining tests. The excluded test is the node-only drift below,
  not a Skills failure or an accepted behavior waiver.
- Read-only render comparison isolated two new node POST registrations:
  `/api/nodes/runs/code-index-warning` and `/api/nodes/runs/prompt-delivery-failure`.
  Current render: 1118 rows / 2 unclassified; committed: 1116 / 0. No Skills
  row differs. Triage relayed the Orchestrator's confirmation: commit
  `50746bb408` (#24389) introduced the drift; repair #24962 under #21572 is
  assigned to a DevTeam. Found work is handed off; no foreign files were touched.
- D1–D4 are source-derived analyses. An in-memory reproduction command was
  blocked by research-scope shell enforcement and abandoned; runtime
  reproduction is not claimed. Existing delivery tests cover normal paging,
  malformed/wrong-tool/stale cursors and final-page tracking, not issuance
  integrity. No whole-surface removal or already-approved waiver is claimed.
- Route these proposed amendments through Writer and Adversary, then any
  operator decision through the Assistant. This seat created no shared task
  or implementation change.
