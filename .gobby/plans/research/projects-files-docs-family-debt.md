# Projects/files/docs family debt pass

Task: #24960. Research only, 2026-10-24 CDT. Plan reviewed:
`.gobby/plans/gdaemon-projects-files-docs-family.md`, stamped commit
`27fe518b3832fee07c3ee31d909e8e2194f95c81`. Checkout observed on main `0.5.0`
at `2e7da824431fd49fa7161a5c197e4c2a2516e5e1`.
Authority: memory `9066f1eb-9ae0-5539-9d8c-8c694e035a7f`, Josh's debt-first
and shared-fix ruling. This report proposes dispositions; it grants no waiver.
The Writer and Adversary must amend and restamp the plan before approval.

The port is necessary: the scoped services still depend on Python. Reuse the
plan's shared admission, storage and fence seams. Most major safety mechanisms
already have owners; the principal missing mechanism is an executable record of
intentional behavioral divergence.

## 1. Debt not to port

### D1. Path validation followed by pathname reopening

- **Python behavior/evidence:** `src/gobby/servers/routes/files.py:90-107`
  resolves a pathname and checks containment. Later operations independently
  stat/open it (`:231-250`, `:277-309`, `:322-344`, `:355-370`). More explicitly,
  `src/gobby/servers/routes/chat_attachments.py:342-362` opens a confined
  descriptor, closes it, then returns `FileResponse(path=...)` using the earlier
  stat result.
- **Debt:** replacing an ancestor after validation changes what a later open
  accesses. Attachment validation loses its held-descriptor protection before
  response streaming. This is a static race analysis, not a demonstrated exploit.
- **Call: DROP** the check-then-reopen behavior. Keep confinement, authorized
  in-checkout behavior, range/cache semantics and SVG protections. Hold the
  admitted root/parent/content descriptors through the actual operation; inspect
  directory entries without following an escaping link. Specify any additional
  symlink refusal separately rather than silently rejecting all in-root links.
- **Amend:** sections 3.3 and 4.2/4.3 (and corresponding M1 entries). Their
  descriptor requirements are already correct; extend characterization to a
  deterministic ancestor swap and require native refusal/no unauthorized I/O.
- **Parity record:** proposed behavior waiver `PFD-D1`, citing approved #24951
  amendment, exact routes and swap scenarios. Those routes remain `delegated`;
  the case verifier accepts the approved safe outcome and still fails ordinary
  read/header regressions. Do not run the vulnerable Python race against user
  files. Shared gate support is S1 below.

### D2. In-place checkout writes can destroy predecessor bytes

- **Python behavior/evidence:** `src/gobby/servers/routes/files.py:351-379`
  calls `target.write_text` in an executor; there is no staged publication or
  compensation. The `.git` and missing-parent checks precede that write.
- **Debt:** opening for write truncates the predecessor before completion;
  write failure can leave partial content even though HTTP returns failure.
  Cancelling the awaiting coroutine does not establish a completed disk outcome.
- **Call: DROP** destructive partial-write behavior. Keep original response
  path, UTF-8 byte size, parent requirement, permission/mode preservation and
  `.git` refusal. The plan's held-parent durable replacement is the right fix.
- **Amend:** 4.3.2 and its M1 entry: distinguish pre-publication failure from
  failure after replacement, and test short writes, rename/fsync failure and
  cancellation. A post-rename directory-fsync failure cannot casually promise
  that predecessor bytes remain; require a defined recoverable outcome.
- **Parity record:** `PFD-D2`, approved decision reference plus injected-failure
  cases for `HTTP POST /api/files/write`; retain its delegated ledger row.
  The verifier checks the new publication outcome, never repeats a live write.
  No broad response normalization; S1 applies.

### D3. A database transaction does not roll back project.json

- **Python behavior/evidence:** `src/gobby/servers/routes/projects.py:499-516`
  updates the row and calls two file savers inside `pm.db.transaction()`.
  `src/gobby/servers/tool_approvals.py:337-347` and
  `src/gobby/config/validation_detection.py:296-313` each read/modify/write the
  same `.gobby/project.json` directly. A second saver or DB commit can fail
  after a successful first file write.
- **Debt:** PostgreSQL rollback covers SQL, not earlier filesystem writes.
  Two independent rewrites also expose intermediate JSON and lost-update risk.
  Memory `fa16139d` correctly describes SQL rollback after a file exception; it
  does not prove filesystem rollback or crash atomicity.
- **Call: DROP** partial cross-store success. Keep omitted/null/empty field
  semantics and existing validation until an explicit separate decision changes
  them. Merge both file changes into one validated replacement, serialize/check
  the observed file revision, and restore the predecessor on recoverable commit
  failure. If crash coherence is promised, require a durable recovery record;
  an in-memory compensator cannot provide it.
- **Amend:** 4.6.4 and M1: include failure of the second requested field,
  database commit failure after publication, concurrent updates and process loss.
  Define recovery before claiming the current unconditional coherent-state
  guarantee. Reuse 3.1/4.3 publication primitives rather than two new writers.
- **Parity record:** `PFD-D3` scopes the PUT/PATCH failure-state divergence and
  its recovery assertions under approved #24951. Rows stay delegated; state
  comparison must honor that waiver and verify both SQL and exact file bytes.
  S1 governs waiver enforcement. No duplicate mutation for comparison.

### D4. Corrupt or unreadable project metadata is treated as empty

- **Python behavior/evidence:**
  `src/gobby/servers/tool_approvals.py:310-334` returns `{}` for unreadable,
  malformed or non-object metadata. The analogous loader at
  `src/gobby/config/validation_detection.py:949-958` does likewise. Their savers
  above then replace the file with a payload derived from that empty object.
- **Debt:** a small approval-rule or validation edit can erase unrelated
  metadata after a transient read failure or corruption. Absence and inability
  to read are materially different states.
- **Call: DROP** silent overwrite after parse/read/type failure. Keep genuine
  missing-file initialization. Refuse the update with a stable typed error and
  leave all DB/file state unchanged. Reuse 5.4's explicit corrupt-input backup
  policy only for the repair command that intentionally owns recovery.
- **Amend:** 4.6 and M1, with malformed JSON, non-object JSON, unreadable file
  and unrelated-key preservation cases. 5.4 already recognizes corrupt-input
  recovery; make the ordinary mutation/refusal boundary explicit.
- **Parity record:** `PFD-D4`, exact PUT/PATCH bad-input scenarios and approved
  safe error/state contract; preserve delegated routes. S1's case waiver must
  replace the Python-success oracle for these scenarios only.

### D5. Git file visibility is parsed as newline-delimited text

- **Python behavior/evidence:** `src/gobby/servers/routes/files.py:148-163`
  runs `git ls-files` and `git ls-files --others --exclude-standard` without
  `-z`, then applies `strip().splitlines()`. `:166-189` compares those strings
  against actual relative entry names.
- **Debt:** quoted/newline/tab/backslash filenames can fail membership tests
  and disappear from the tree. The status endpoint already uses a NUL-aware
  porcelain parser (`:392-407`), so preserving the tree accident is unjustified.
- **Call: DROP** line-based filename parsing. Use NUL-delimited tracked and
  untracked output. Keep directories-first/case-insensitive order, `.git`
  hiding, ignore policy and the existing no-Git-info fallback. Handle filenames
  outside JSON's Unicode representation under an explicit tested policy.
- **Amend:** 4.2.1 and M1; include newline, tab, quote, backslash and Unicode
  filename cases. Keep 4.4's existing NUL-aware status and literal diff contract.
- **Parity record:** `PFD-D5` names only affected `/api/files/tree` filename
  cases and the approved complete inventory. Keep the row delegated. Compare
  the corrected expected entries; never waive arbitrary missing tree entries.
  S1 applies.

### D6. Host exception text becomes the HTTP error contract

- **Python behavior/evidence:** `src/gobby/servers/routes/files.py:315-317`
  returns `Failed to read file: {e}`; `:372-373` does the same for writes;
  `:396-397` includes the Git spawn exception in a 503. These can include
  absolute paths and platform-specific OS messages.
- **Debt:** exposes host details and makes exact Python/Rust/error parity
  depend on OS, runtime and temporary root. This also conflicts with 6.1's
  prohibition on user path secrets in compare reports.
- **Call: DROP** raw exception interpolation at the wire boundary. Keep
  intended status categories; send stable safe messages/codes and retain
  operational diagnostics in appropriately bounded internal logs.
- **Amend:** 4.2/4.3/4.4, 1.1 and M1 to reference the shared safe-error mapping
  in S4. Preserve specific useful public refusals such as traversal, missing
  parent and `.git` restrictions rather than replacing all errors with one 500.
- **Parity record:** `PFD-D6` cites approved decision, exact error cases and
  approved status/body; routes stay delegated. It must not authorize stripping
  every `detail` or ignoring all 4xx/5xx differences. S1 applies.

### D7. Existing layout and namespaces are implementation debt already resolved

- **Python behavior/evidence:** attachment bytes currently resolve under
  `_personal/attachments/...` in
  `src/gobby/servers/chat_attachment_files.py:55-59`; the current content
  route is registered at `src/gobby/servers/routes/chat_attachments.py:319`.
  USER.md uses `/api/files/user-md` in
  `src/gobby/servers/routes/hub_files_proxy.py:33-63`.
- **Call: KEEP** the plan's D5/D6 decision to **DROP** old route names and
  locator layout at the one cutover. Keep retained bytes, lease/publication
  semantics, owner relay and wiki's existing ordinary 404. No extra alias or
  second attachment store is justified.
- **Amend:** retain 3.4/3.5/6.3 and their M1 entries; extend 1.1/6.1's mapping
  beyond namespace-only deltas to the separately approved D1-D6 case waivers.
- **Parity record:** 6.3 already requires each removed USER.md/attachment route
  source key to remain `dropped by decision #24951`, with canonical replacement
  rows delegated and exact tests cited. Record byte-layout changes as approved
  behavioral mappings on those replacement cases. Reconfirm provenance when
  #24951 is restamped; no surface is silently omitted. The eight API-docs UI
  rows remain #21589-owned; this report proposes no drop of them.

## 2. Systemic issues

### S1. Surface ledger and compare do not yet express behavioral waivers

**Evidence:** `docs/contracts/parity-ledger.md:3-15` defines only surface
status/note records and dropped-by-decision task references.
`crates/gcore/src/route_family.rs:51-60` exposes only
`Skipped`, `Verified`, `Mismatch(String)`.
`crates/gdaemon/src/front_door/compare.rs:18-40` has generic outcomes/records;
`:97-105` rejects Diff/NativeError/Mismatch, but does not reject Skipped.
Projects plan 1.1 says normalize only generated values; 6.1 permits only the
specified namespace/layout delta; 6.2 requires zero mismatches and accounted
coverage. A waiver written only in prose cannot satisfy that contract.

**Affected other families:** every family using shared RouteFamily/compare and
the parity-ledger exit gate: Runtime, Sessions/transcripts, Tasks, Memory,
Agents/attention/dispatch/worktrees, Terminal/WS, Workflows/rules/pipelines/build/
validation, MCP transport/front door, Skills, Cron, Comms, Code index,
Configuration, AI providers, Voice, Admin/metrics/telemetry/API docs, and WS chat.
Projects-specific scope is D1-D6 plus namespace/layout mapping D7. The Skills
debt pass #24959 is checking the shared mechanism too.

**One shared fix:** under #21572, define a single machine-readable approved
divergence registry keyed by exact surface + scenario + decision reference,
with old/new contract, permitted comparison fields/state and evidence test.
Have characterization, compare verification and final parity coverage consume
it. Report approved divergences with their reference, never as unexplained
matches/skips; reject missing/stale/unreferenced or overly broad waivers.
Require representative route/outcome coverage separately from `Report::passes`.
Keep surface removals in the existing ledger. This is a proposed shared fix,
not evidence that current gates already honor the D1-D6 waiver IDs.

**Plan impact:** 1.1, 6.1-6.3 and all corresponding M1 entries must depend on
the shared outcome once Triage/Orchestrator assigns its owner; do not invent
per-family normalizers or attach a fake task reference here.

### S2. Authentication success currently has two meanings across runtimes

**Evidence:** `crates/gdaemon/src/front_door/auth.rs:125-163` strips forged
identity headers and validates API keys, but returns `Ok(())` for absent
authorization and managed-token prefixes awaiting downstream handling.
`src/gobby/servers/browser_sessions.py:27-78` owns its key and active membership
in one Python process. An upstream `Ok(())` is therefore not proof of admitted
native authority. This is a transitional architecture risk, not a claim of a
demonstrated current bypass.

**Affected other families:** Skills, Cron, Comms, Sessions/transcripts, Tasks,
Memory, Agents/worktrees, Terminal/WS, Workflows, Code index, Configuration,
AI providers, Voice, Admin/API docs and WS chat, plus MCP front-door adapters
when they reuse HTTP admission. Each retains its own method/selector policy.

**One shared fix:** KEEP and reuse Projects 2.1/2.2's published admission carrier
and verified principal type, backed by Runtime #24452. Make native dispatch
require explicit admitted authority; separate unauthenticated continuation from
successful admission. Apply body bounds before buffering and preserve browser
selector confinement. Other families consume this seam rather than copy a
cookie verifier or interpret proxy identity headers themselves.

**Plan impact:** retain 2.1/2.2, add executor-visible consumer ownership and
downstream prerequisite references. Python-loss tests must exercise refusal
classes as well as valid cookies. Planned shared fix, not another service.

### S3. Process-local purge fencing cannot protect mixed-runtime writers

**Evidence:** `src/gobby/projects/write_fence.py:23-30` stores counters,
task ownership and an asyncio condition in process memory.
`src/gobby/runner_init/services.py:166,288,325,659,684,721` constructs/injects
that fence into memory/vector consumers;
`src/gobby/runner_init/orchestration.py:446` and
`src/gobby/runner_init/project_purge.py:66` consume it too.

**Affected other families:** Memory and Code index directly; AI/vector-derived
writers, Workflows/pipelines and any Agents/Tasks/Sessions/Comms producer that
writes project-derived state must participate before concurrent native purge.
This identifies the required writer contract; it does not claim every listed
producer currently calls this Python fence.

**One shared fix:** KEEP 5.1's shared advisory-lock protocol, public gcore fence
and Python API adapter, including its one-operation-connection reserve. Inventory
every derived writer and make its native port consume the same fence. Never
create a second family-local lock. Test a waiting purge, admitted writer queries,
minimum pool capacity, cancellation and both runtimes together.

**Plan impact:** retain 5.1/5.2 and M1; add the downstream writer-adoption rule
to 6.3's contract. Existing memory counters remain implementation details only
after shared exclusion is authoritative. No public surface drop is proposed.

### S4. Safe error mapping and failure-state characterization need one owner

**Evidence:** D2-D4/D6 show I/O errors, DB errors and publication states crossing
the same HTTP boundary. The current native auth mapper returns a structured
JSON rejection (`crates/gdaemon/src/front_door/auth.rs:173-198`), whereas Python
file handlers use HTTPException detail strings. Generic compare outcomes do
not define which representation is contractual.

**Affected other families:** all native HTTP families listed in S2; particularly
Skills and Configuration (config publication), Comms and WS chat (attachment
bytes/rows), Memory and Code index (derived-store cleanup), Cron and Runtime
(durable state plus external effects). CLI/MCP adapters must translate the same
service outcomes into their existing envelopes rather than leak anyhow/OS text.

**One shared fix:** define a bounded service-error taxonomy and one reusable
HTTP mapping plus cross-language error fixture harness. Retain intentional
route-specific public statuses/envelopes, map private OS/SQL details to stable
safe diagnostics, and assert side effects and recovery state in the same case.
Use existing publication/fence/lease operations; no general distributed
transaction framework. D3 additionally needs an explicit recoverable mixed
DB/file outcome before the plan can promise crash coherence.

**Plan impact:** 1.1/4.2-4.6/6.1 consume the common fixture/mapping contract;
route-specific acceptance keeps precise refusal cases. If any wire field
changes, S1 must carry the exact approved waiver. This report does not approve
a wholesale global error-envelope change or a new pagination API.

### Evidence limits and verification

- Used tracked skills, task/memory tools, Git read commands, and gcode
  outline/symbol-at/grep/tree. Source assertions above are static evidence;
  no live daemon HTTP/DB, credentials, production bytes or other sessions'
  resources were accessed. No tests, exploit races, migration or heavy builds
  were run. Proposed tests are implementation acceptance, not observed passes.
- Some gcode calls reported an ongoing index refresh and returned existing
  index contents. Re-resolve cited symbols at implementation tip. A missing
  index entry (notably the ledger generator) was not treated as source absence.
  The ledger's documented schema and observed Rust types support S1; #24959
  should consolidate any additional shared waiver support found elsewhere.
- No blanket debt finding is made for batched project statistics, live
  attachment limits, UTF-8 replacement/truncation, Git-unavailable visibility,
  attachment leases or soft-delete/purge ordering. Keep these explicit
  contracts unless evidence and an approved decision justify a change.
- Before approval, dispose D1-D7 and S1-S4, amend owning narrative/acceptance,
  re-derive M1 and restamp. Systemic work gets one shared owner through Triage;
  accepted findings must not remain unowned prose in individual family plans.
