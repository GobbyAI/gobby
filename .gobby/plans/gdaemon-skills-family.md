Plan artifact: `.gobby/plans/gdaemon-skills-family.md`

# Gobby 1.0 Stage 2: native Skills family

**Plan ID:** gdaemon-skills-family

## Overview
`kind: framing`

Port the Skills family under #21580, within #21568, from Python ownership to
native Rust services. The bounded inventory is every ledger key containing
`skills` whose status note cites #21568: **54 rows: 20 CLI, 20 HTTP, 14 MCP**.
The source is `docs/contracts/parity-ledger-status.d/memory-skills.json`; the
rendered `docs/contracts/parity-ledger.md` is generated. This plan does not
expand work or authorize a production restart.

The work is necessary: the current ledger marks all 54 rows proxied, and the
existing Rust route registry has no Skills implementation. Restraint stops at
the existing family and internal-tool seams: one `gobby-skills` library crate
at `crates/gskills`, one injected service shared by REST, MCP, and native CLI,
the existing PostgreSQL pool, and the existing hub-to-node command channel.
No separate skills process, plugin framework, second database, schema compiler,
or new executable is required.

The `.gobby/plans/gdaemon-runtime-family.md` precedent supplies `RouteFamily`,
shared pool, active-config and secret-store contracts. The
`.gobby/plans/rust-internal-mcp-servers.md` plan owns typed descriptors,
policy/discovery, gateway, compatibility backend, and general per-server routing.
This plan owns skill-specific behavior and its own activation after #24841.

## Decision Record
`kind: framing`

1. The Orchestrator confirmed on 2026-10-24 that the complete 54-row inventory
   is in scope, with no enhancer seat. Writer gobby#17576 and Adversary
   gobby#17577 reach consensus; Triage is the coordination route.
2. The native library owns PostgreSQL skill/companion-file storage, parsing,
   validation, safety scanning, search, acquisition, hub adapters, paging, and
   materialization. Transports translate the existing wire contracts into
   service calls. Keep each module below 1,000 production lines.
3. #24883 owns the shared `ToolFamily`, descriptor, schema derivation, hash,
   caller context, and validation contract. Its Dev Manager confirmed that no
   Skills handler or registry is in flight. Consume the final landed contract;
   do not implement competing shared types in this family.
4. The Orchestrator expressly assigned Skills-specific compare/native
   activation to this plan after #24841 completes. #21570 retains the shared
   gateway, policy pass, and final deletion of the compatibility backend.
   Removing a still-needed Python registry or claiming another server native
   is outside this plan.
5. `gdaemon skills` is the native command dispatcher. The existing `gobby
   skills` entry forwards arguments during the transition; no new binary is
   introduced. The eventual global naming switch removes Python. This plan
   leaves only argument forwarding in the Skills launcher, with every Skills
   callback and business operation executed natively.
   That switch's cleanup inventory also includes 2.6's
   `src/gobby/skills/native_sync.py` adapter, its private native sync command,
   and the Python startup `skip_types={"skills"}` handoff; native startup
   remains the bundled publication owner.
6. Hub database rows remain authoritative. Hub handlers execute bounded
   filesystem effects on the caller's selected node; standalone uses the same
   local effect implementation. A node receives neither a registry nor a hub
   database credential. A missing machine or disconnected node fails explicitly;
   it never redirects an effect onto the hub filesystem.
7. Preserve the intentional 54-row contract, including CLI output/exit behavior,
   scope precedence, metadata, soft deletion, full/brief views, page limits,
   scan results, failure codes, update rollback, and materialization ownership.
   Preserve public names while replacing implementation ownership; add no
   backward-compatibility subsystem or alternate API generation. The debt
   dispositions below replace only explicitly cited behavior-level accidents;
   #24963 waivers and expected-native assertions gate every chosen divergence.
8. Use installed Rust dependencies first: `yaml_serde`, `reqwest`, `regex`,
   `fancy-regex`, `serde`, `schemars`, `clap`, Tokio, and existing gcore clients.
   ZIP decoding needs the `zip` crate, with default features disabled and
   deflate enabled; it is the single new external library authorized here.
   Commit its resolution in `Cargo.lock`. Native scanning uses committed
   ClawCare 0.9.0 rules and license attribution, including Gobby's POST-boundary
   correction; no Python interpreter remains in a native execution path.
9. #24967 owns the implementation-only native browser-cookie verifier for both ordinary
   AuthStore sessions and `browser-testing.` managed sessions. Skills 6.0
   registers only the exact 20 Skills HTTP rows through it. The Orchestrator
   applies `security-review` to that leaf immediately after expansion, verifies
   the persisted label before dispatch, and holds cookie activation until a
   Security Reviewer records an approved step for the exact implementation
   SHA. This approved expansion obligation needs no manifest-emitter change.

## Debt dispositions and shared conventions
`kind: framing`

Josh's debt-first ruling (memory `9066f1eb`) governs this revision. The
read-only report `.gobby/plans/research/skills-family-debt.md` at
`9d663c4a4a` supplies D1–D6 and S1–S3. Keep all 54 public surfaces; parity
matches their intentional contracts rather than accidental Python effects.
The six DROP dispositions below are chosen replacements, conditional on the
shared waiver gate and approval of this amended plan, not already-approved
divergences.

- **D1 DROP:** omission never clears stored fields. HTTP updates retain
  Absent/Clear/Set distinctions, validate the merged locked candidate, and
  preserve populated metadata during content-only or empty-object edits.
  `skills.http-update.omitted-fields` binds exactly
  `HTTP PUT /api/skills/{skill_id}`, owned by 1.2.
- **D2 DROP:** caller-forged, skipped-prefix, wrong-session and replayed
  continuation handles never establish completed delivery. Use server-issued,
  caller/generation-bound progression and bounded lifecycle state, including
  explicit expiry/capacity/reset outcomes.
  `skills.mcp-get-skill.cursor-progression` binds
  `MCP gobby-skills:get_skill`; `skills.mcp-get-skill-file.cursor-progression`
  binds `MCP gobby-skills:get_skill_file`, both owned by 1.6.
  **KEEP** issued sequential page bytes, Unicode/budget behavior, ordinary
  malformed/stale-content errors and the distinction between delivery coverage
  and proof that a client read bytes.
- **D3 DROP:** parent and companion soft deletion cannot commit separately.
  Failure leaves both visible states unchanged and emits no notification.
  `skills.http-delete.atomic-soft-delete` binds
  `HTTP DELETE /api/skills/{skill_id}`;
  `skills.mcp-remove-skill.atomic-soft-delete` binds
  `MCP gobby-skills:remove_skill`; `skills.cli-remove.atomic-soft-delete`
  binds `CLI gobby skills remove`, all owned by 1.2.
  **KEEP** successful soft deletion, restoration and conflict semantics.
- **D4 DROP:** failed acquisition/parse/validation/scan before a source-update
  write performs no compensating metadata write and reports no fictitious
  rollback. `skills.mcp-update-skill.failed-refresh-no-write` binds
  `MCP gobby-skills:update_skill`; `skills.cli-update.failed-refresh-no-write`
  binds `CLI gobby skills update`, both owned by 2.5.
  **KEEP** source provenance, full scanning, atomic replacement, prior/newer
  concurrent committed edits and honest post-write rollback outcomes.
- **D5 DROP:** only unexpected internal exception text is replaced with
  `Internal server error` in the existing detail/message/error field.
  Status, code, envelope, intentional domain messages and no-effect assertions
  stay required parity. No observed secret disclosure is claimed.
  The exact raw-exception surface inventory below is owned by its listed leaf;
  HTTP import already sanitizes unexpected errors and stays ordinary parity.
  CLI intentional diagnostics also remain parity; no CLI wildcard waiver exists.
- **D6 DROP:** synchronous request-executor stalls are not reproduced.
  `skills.http-create.nonblocking-execution` binds
  `HTTP POST /api/skills` and
  `skills.http-update.nonblocking-execution` binds
  `HTTP PUT /api/skills/{skill_id}`, owned by 1.2;
  `skills.http-search.nonblocking-execution` binds
  `HTTP GET /api/skills/search`, owned by 1.7.
  **KEEP** response/state semantics, transaction ordering, freshness and
  successful completion. Scheduling proof uses slow controlled work and
  cancellation; it suppresses no semantic comparison.

**D5 exact waiver inventory:**

| ID | Exact ledger surface | Owner |
| --- | --- | --- |
| `skills.http-list.internal-error-redaction` | `HTTP GET /api/skills` | 1.1 |
| `skills.http-get.internal-error-redaction` | `HTTP GET /api/skills/{skill_id}` | 1.1 |
| `skills.http-stats.internal-error-redaction` | `HTTP GET /api/skills/stats` | 1.1 |
| `skills.http-create.internal-error-redaction` | `HTTP POST /api/skills` | 1.2 |
| `skills.http-update.internal-error-redaction` | `HTTP PUT /api/skills/{skill_id}` | 1.2 |
| `skills.http-delete.internal-error-redaction` | `HTTP DELETE /api/skills/{skill_id}` | 1.2 |
| `skills.http-restore.internal-error-redaction` | `HTTP POST /api/skills/{skill_id}/restore` | 1.2 |
| `skills.http-move-to-project.internal-error-redaction` | `HTTP POST /api/skills/{skill_id}/move-to-project` | 1.2 |
| `skills.http-move-to-installed.internal-error-redaction` | `HTTP POST /api/skills/{skill_id}/move-to-installed` | 1.2 |
| `skills.http-scan.internal-error-redaction` | `HTTP POST /api/skills/scan` | 1.4 |
| `skills.http-list-files.internal-error-redaction` | `HTTP GET /api/skills/{skill_id}/files` | 1.5 |
| `skills.http-read-file.internal-error-redaction` | `HTTP GET /api/skills/{skill_id}/files/{file_path:path}` | 1.5 |
| `skills.http-write-file.internal-error-redaction` | `HTTP PUT /api/skills/{skill_id}/files/{file_path:path}` | 1.5 |
| `skills.http-export.internal-error-redaction` | `HTTP GET /api/skills/{skill_id}/export` | 1.5 |
| `skills.http-search.internal-error-redaction` | `HTTP GET /api/skills/search` | 1.7 |
| `skills.http-list-hubs.internal-error-redaction` | `HTTP GET /api/skills/hubs` | 2.3 |
| `skills.http-search-hubs.internal-error-redaction` | `HTTP GET /api/skills/hubs/search` | 2.4 |
| `skills.http-hub-install.internal-error-redaction` | `HTTP POST /api/skills/hubs/install` | 2.5 |
| `skills.http-restore-defaults.internal-error-redaction` | `HTTP POST /api/skills/restore-defaults` | 2.6 |
| `skills.mcp-get-skill.internal-error-redaction` | `MCP gobby-skills:get_skill` | 4.1 |
| `skills.mcp-get-skill-file.internal-error-redaction` | `MCP gobby-skills:get_skill_file` | 4.1 |
| `skills.mcp-get-skill-files.internal-error-redaction` | `MCP gobby-skills:get_skill_files` | 4.1 |
| `skills.mcp-install-skill.internal-error-redaction` | `MCP gobby-skills:install_skill` | 4.1 |
| `skills.mcp-list-hubs.internal-error-redaction` | `MCP gobby-skills:list_hubs` | 4.1 |
| `skills.mcp-list-skills.internal-error-redaction` | `MCP gobby-skills:list_skills` | 4.1 |
| `skills.mcp-materialize-skill-scripts.internal-error-redaction` | `MCP gobby-skills:materialize_skill_scripts` | 4.1 |
| `skills.mcp-move-skill-to-installed.internal-error-redaction` | `MCP gobby-skills:move_skill_to_installed` | 4.1 |
| `skills.mcp-move-skill-to-project.internal-error-redaction` | `MCP gobby-skills:move_skill_to_project` | 4.1 |
| `skills.mcp-remove-skill.internal-error-redaction` | `MCP gobby-skills:remove_skill` | 4.1 |
| `skills.mcp-restore-skill.internal-error-redaction` | `MCP gobby-skills:restore_skill` | 4.1 |
| `skills.mcp-search-hub.internal-error-redaction` | `MCP gobby-skills:search_hub` | 4.1 |
| `skills.mcp-search-skills.internal-error-redaction` | `MCP gobby-skills:search_skills` | 4.1 |
| `skills.mcp-update-skill.internal-error-redaction` | `MCP gobby-skills:update_skill` | 4.1 |

Each owner adds its entries to `docs/contracts/parity-waivers.json` and
depends on #24963. Entry fields are `id`, `surface`, `behaviour`,
`decision`, and `replacement`, all required; the shared schema, loader and
three consumers belong to #24963, never this family. IDs use stable
`<family>.<surface-slug>.<behaviour-slug>` names. Each `surface` is exactly
the key above; each `decision` cites this plan's owning section, for example
`.gobby/plans/gdaemon-skills-family.md#1.2`. The behaviour sentence and
replacement identify the bounded changed branch. Corpus/native vectors retain
the old Python outcome/effects and assert the chosen native outcome/effects;
overlapping corrections receive their separate behavior citations.

**S1 shared readiness:** ledger generation, corpus/replay and compare consume
the same #24963 manifest. Each changed behavior reports `Waived` with its
ID/decision and passing expected-native assertions, never a normalization mask
or `Skipped`. At least one ordinary `Passed` case is required. Zero-Passed,
all-Skipped, unwaived Skipped, unknown/stale waiver IDs, missing decisions or
unmatched differences fail readiness. Existing volatile-field normalization
must not hide D1 clearing, D3 partial deletion, D4 timestamp/event writes or
D5 error text. No whole-surface drop or Skills exception framework is introduced.

**S2 shared update convention:** HTTP/MCP/CLI adapters preserve field presence
as `Absent`, `Clear`, or `Set(value)` through typed decoding. Absent leaves
stored state unchanged; only explicit null can request Clear, and only a
field's nullable contract permits it. False, zero, empty strings, arrays and
objects remain Set values. Merge under one row lock, validate the complete
candidate, and write once. Plain `Option<T>` must not collapse absent/null.
Use existing serde/JSON key-presence facilities and shared request conventions;
do not add a schema compiler or a per-family update framework. API-specific
empty-update and nullable-field rules remain explicit in the owning leaf.

**S3 shared error convention:** validation, missing, forbidden, conflict,
capacity, unavailable and internal are distinct typed domain-error classes.
Existing HTTP/MCP/CLI envelopes are projected by explicit transport adapters;
reuse `gobby_core::mcp::ToolError` at the MCP boundary. Safe intentional
domain details stay public. Unexpected diagnostics are logged privately and
the existing public detail/message/error field receives the stable safe text;
no extra code/envelope field is invented. Only an exact #24963 waiver changes
an observed wire contract; this is not a family sanitizer framework.

These presence, error and readiness conventions match the Cron proposal
(#24952). No contrary convention was found in the current Projects plan
(#24951); subsequent conflicts go through Triage to the Orchestrator before
review rather than silently forking the convention. #24964 alone supplies
outer credentialed CORS/preflight. #24967 alone supplies the implementation
of both browser-cookie verifier classes. The #24659 planning/retirement root
is not a Skills implementation prerequisite.

## Constraints
`kind: framing`

- Observed research baseline: HEAD `3b3480dda67abc46c04977808c3e530b52fd8222`.
  Existing files and symbols below were inspected with `gcode outline`,
  `gcode symbol-at`, and bounded literal searches. Proposed paths under
  `crates/gskills` and proposed test symbols are new.
- Storage reuses the existing `skills`, `skill_files`, and `session_skills`
  tables and their grants. Do not alter the schema or manufacture a parallel
  registry. `UNIQUE NULLS NOT DISTINCT (name, project_id, source)` and the
  existing deterministic IDs remain part of the storage contract.
- Source interpretation preserves explicit GitHub forms, local-path precedence,
  ZIP validation, full companion-file scanning, and source-specific bounds.
  Preserve 1 MiB SKILL.md, 64 KiB frontmatter, 20 YAML aliases, 1 MiB per
  companion file, 10 MiB stored total, and 64 KiB hub stream chunks. A provider's
  stricter bound, such as GitHub Topic's 256 KiB SKILL.md cap, still applies.
- Semantic workflow enforcement stays in #24841's gateway. Skill handlers do
  not author raw provider `before_agent`, `after_agent`, or `stop` events and
  do not repeat `before_tool`/`after_tool` policy evaluation.
- `get_skill` is read-like for content but writes completed-load state. Its
  descriptor must be mutation-classified for compare safety. Reference loads
  also write `loaded_skill_references`. No production shadow call may record
  those effects twice. All mutations are compared on isolated cloned state.
- Source changes in this plan are built/tested before deployment. 6.1 is the
  shipping commit and owns release bumps and promotion; earlier commits are
  unshipped source commits. No same-version changed-source promotion occurs.
  Read the current `project_policy.release_guidance` at execution, honor the
  published/unpublished binary distinction, and use the installed-set contract.
- Heavy Rust commands require the Release Manager's heavy-work admission and
  `CARGO_BUILD_JOBS=3`. Use focused `cargo test -p ...` while #24949's nextest
  store repair is pending; record the substitution. No full pytest run.
- Python checks use `uv run`. Daemon tests use isolated temporary state, ports,
  and `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test`
  with `GOBBY_TEST_PROTECT=1`; never the operator's daemon database.

Consumer evidence: `gcode grep -F 'skills' docs/contracts/parity-ledger.md -m
100`, followed by its printed continuation, enumerated the 54 rows. The same
predicate over all status JSON files returned 54 keys. `gcode grep -F
'materialize_skill_scripts' src/ tests/ -m 30` located the MCP registration,
loading directives, and installed impeccable references; those references
remain consumers of the unchanged API. `gcode grep -F 'FAMILIES'
crates/gdaemon/src crates/gdaemon/tests -m 50` located serve composition and
test harness consumers; this plan adds an injected family instance in serve,
without adding a service field to `FrontDoorState` or changing the static
FAMILIES contract. `gcode usages FrontDoorState` and `gcode usages
MANAGED_BIN_VERSION_PINS` returned no calls; bounded literal import searches
therefore supplement those graph results. Final pin consumers are listed in 6.1.

## P0: Freeze the family contract
`kind: framing`

### 0.1 Executable 54-row characterization corpus [category: test] (depends: #24963)
`kind: deliverable`

Targets:
- `scripts/skills_parity_corpus.py`
- `crates/gskills/tests/fixtures/parity.json`
- `crates/gskills/tests/fixtures/safety.json`
- `tests/contracts/test_skills_parity_corpus.py`

**Research context:** Existing entry points are
`src/gobby/servers/routes/skills.py::create_skills_router`,
`src/gobby/cli/skills.py::skills`, and
`src/gobby/mcp_proxy/tools/skills/__init__.py::create_skills_registry`.
The status JSON has 20 CLI keys, 20 HTTP keys, and 14 MCP keys. Python fixtures
already exist in `tests/servers/routes/test_skills_routes.py`,
`tests/mcp_proxy/tools/skills/`, `tests/skills/`, and `tests/storage/`.
The installed ClawCare default set resolves to 39 rules. Generate a committed,
versioned JSON corpus from isolated Python fixtures: method/path or command/tool,
arguments, seeded rows/files, expected response/exit/effects, and only explicitly
enumerated volatile-field normalization. Capture schemas and hashes before
replacing registration. This is reusable characterization infrastructure;
each implementation leaf still owns its new tests.
The debt report at `9d663c4a4a` is the D1–D6 baseline. Keep its recorded Python
origin and concrete before/after state in the corpus. Consume #24963's one
`docs/contracts/parity-waivers.json` loader: intentional differences are
case-specific Waived assertions with the exact decision and required native
result/effects, never masks or Skipped. Freeze unexpected-fault vectors for the
33 D5 HTTP/MCP surfaces listed above; intentional validation/missing/conflict/
scan messages and already-sanitized HTTP import remain ordinary parity.
For D4 compare actual pre/post timestamps, search invalidations and events so
volatile normalization cannot conceal a compensating write. D6 progress cases
assert unchanged response/state plus independent scheduling/cancellation proof.
This first infrastructure leaf uses test-only waiver entries/native-result
doubles to verify shared-loader matching and expected-result evaluation, while
recording real isolated Python baselines. It does not require later native
implementations or publish their production entries. The owning leaves add
real shared-manifest entries and replay against real handlers; 6.1 gates the
completed family on those results. No dependency cycle is introduced.

**Acceptance:**

- 0.1.1 - Corpus inventory equals the exact 54-key ledger predicate, and every
  key has a success and representative validation/failure case; non-Skills
  keys are rejected. test: `tests/contracts/test_skills_parity_corpus.py::test_inventory_matches_skills_ledger`.
- 0.1.2 - Running the collector twice against reset isolated state gives the
  same normalized fixtures, schemas, ordering, stdout/stderr, exit codes, and
  side-effect assertions. test: `tests/contracts/test_skills_parity_corpus.py::test_collection_is_reproducible`.
- 0.1.3 - Safety fixtures cover all 39 effective rules, safe near-matches,
  companion files with arbitrary text extensions, POST versus postgres, and
  unavailable/erroring scanners. file: `crates/gskills/tests/fixtures/safety.json`.
- 0.1.4 - For each of the 20 HTTP rows, the corpus records ordinary and managed
  browser-cookie admission, body/query/header selector effects, missing/invalid
  credentials, cookie GET and mutation outcomes, and observed credentialed CORS
  and preflight behavior. Managed-cookie loopback and safe-method restrictions
  remain distinct from ordinary sessions.
  test: `tests/contracts/test_skills_parity_corpus.py::test_http_cookie_admission_matrix`.
- 0.1.5 - Harness fixtures resolve every D1–D6 changed behavior to one
  exact-surface test waiver and evaluate explicit native response/effect
  assertions; untouched success/domain-error cases remain Passed and no whole
  surface is removed. Actual native-handler replay belongs to each owning leaf.
  test: `tests/contracts/test_skills_parity_corpus.py::test_debt_waivers_are_behavior_scoped`.
- 0.1.6 - At least one ordinary Passed case is required; skipped-only or any
  unwaived Skipped, stale/missing waiver decision, and an unrelated injected
  regression fail readiness even beside a valid Waived case.
  test: `tests/contracts/test_skills_parity_corpus.py::test_waiver_does_not_hide_unrelated_diff`.

Planned verification: the five named tests with the protected Python test prefix;
collector invocations use temporary homes and an isolated test database. No
characterization test has been run during drafting.

## P1: Native registry and content services
`kind: framing`

### 1.1 Native scoped repository and HTTP reads [category: code] (depends: 0.1, #24443, #24963)
`kind: deliverable`

Targets:
- `Cargo.toml`
- `Cargo.lock`
- `crates/gskills/Cargo.toml`
- `crates/gskills/src/lib.rs`
- `crates/gskills/src/model.rs`
- `crates/gskills/src/store.rs`
- `crates/gskills/src/http.rs`
- `crates/gdaemon/Cargo.toml`
- `crates/gdaemon/src/serve.rs::*` — scope-reason: inject the family in both cfg-gated serve entry points without changing static FAMILIES
- `crates/gdaemon/src/front_door/mod.rs::*` — scope-reason: expose the Skills composition module in the front-door module boundary
- `crates/gdaemon/src/front_door/skills.rs`
- `crates/gskills/tests/registry.rs`
- `docs/contracts/parity-waivers.json`

**Research context:**
`src/gobby/storage/skills/_metadata.py::SkillMetadataMixin.get_by_name`,
`SkillMetadataMixin.list_skills`, and `SkillMetadataMixin.count_skills` own
precedence, deletion and filters. `src/gobby/storage/skills/_models.py` owns
serialization. `src/gobby/servers/routes/skills.py::skill_stats` adds counts and
usage. `crates/gcore/src/route_family.rs::RouteFamily` already provides family
composition and per-request claims; #24443 supplies one shared gcore pool.
Construct a new `SkillsFamily` carrying `Arc<SkillService>` inside serve's
existing family vector. The service receives that pool, configuration access,
event publisher and node-effect adapter. It lives in hub/standalone authority
modes only. REST filters, unknown IDs, scope precedence and JSON nulls match
the corpus. Claim only implemented method/path combinations while proxy remains
the default; do not absorb neighboring route prefixes.
Keep native PostgreSQL/provider work asynchronous with the existing pool,
timeouts and cancellation; D6 rejects synchronous waits on the request executor,
not ordering or successful response semantics. Use the shared typed error
classes validation/missing/forbidden/conflict/capacity/unavailable/internal and
existing transport envelopes. Safe domain errors remain exact; unexpected
diagnostics stay private and return `Internal server error` in the existing
detail field. This leaf adds only the D5 `skills.http-list`, `skills.http-get`
and `skills.http-stats` entries (suffix `.internal-error-redaction`) to the
shared waiver file, with decision citations to this section. #24963 owns the
loader/schema/readiness mechanics; no family exception registry is added.

**Granularity:** The crate, repository, service injection and three read
endpoints establish one usable registry-read outcome. The production-file
count exceeds six because composition crosses the existing crate boundary;
content lifecycle and source acquisition remain separate deliverables.

**Acceptance:**

- 1.1.1 - `HTTP GET /api/skills` matches scoped list/filter/order and response
  parity. test: `crates/gskills/tests/registry.rs::list_matches_python`.
- 1.1.2 - `HTTP GET /api/skills/{skill_id}` matches identity, not-found and
  deleted-row visibility. test: `crates/gskills/tests/registry.rs::get_matches_python`.
- 1.1.3 - `HTTP GET /api/skills/stats` preserves counts, usage and filters.
  test: `crates/gskills/tests/registry.rs::stats_matches_python`.
- 1.1.4 - Project, installed and template precedence, same-name conflicts,
  disabled skills, empty sets and transaction failures match Python; a node
  constructs no pool or registry. test: `crates/gskills/tests/registry.rs::scope_and_authority_match_python`.
- 1.1.5 - The three read routes retain status/envelope/domain-error parity;
  injected unexpected faults expose safe detail only, report their exact D5
  Waived citations and leave state unchanged.
  test: `crates/gskills/tests/registry.rs::unexpected_read_errors_are_safe_and_waived`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo test -p gobby-skills --test registry`;
`cargo check -p gobby-daemon`. No implementation check has run during drafting.

### 1.2 Atomic registry lifecycle and notifications [category: code] (depends: 1.1, #24963)
`kind: deliverable`

Targets:
- `crates/gskills/src/lib.rs`
- `crates/gskills/src/store.rs`
- `crates/gskills/src/lifecycle.rs`
- `crates/gskills/src/http.rs`
- `crates/gskills/tests/lifecycle.rs`
- `docs/contracts/parity-waivers.json`

**Research context:**
`src/gobby/storage/skills/_metadata.py::SkillMetadataMixin.create_skill`,
`SkillMetadataMixin.update_skill`, `SkillMetadataMixin.delete_skill`,
`SkillMetadataMixin.restore`, `SkillMetadataMixin.move_to_project`, and
`SkillMetadataMixin.move_to_installed` are the transaction owners.
`src/gobby/storage/skills/_files.py::SkillFilesMixin._restore_skill_files`
couples companions to restoration. The route's `_broadcast_skill` and
`src/gobby/skills/manager.py::SkillManager._on_skill_change` are consumers.
Port deterministic IDs, source/project uniqueness, bundled edit restrictions,
timestamps and companion deletion/restoration using the shared pool transaction.
Publish existing skill events after commit only and invalidate the shared
search state. Duplicate/conflicting moves must leave original rows intact.
No webhook, outbox or new lifecycle framework is needed.
For D1, `SkillUpdateRequest` currently defaults optional fields to None and the
HTTP adapter forwards every field, while storage distinguishes `_UNSET` from
explicit None. Decode key presence before typed projection using existing serde/
JSON facilities. Absent retains each stored field; explicit null clears only
version/license/compatibility/allowed_tools/metadata. Preserve existing no-clear
or validation outcomes for nonnullable fields, and preserve False/empty Set
values. Lock the row, merge and validate the whole candidate, then write once;
an empty object is a no-op returning the current record, with no timestamp/event
change. `skills.http-update.omitted-fields` records this deliberate replacement
for populated content-only/empty-object edits rather than masking metadata.

D3's Python parent tombstone and companion deletion use separate transactions
(`SkillMetadataMixin.delete_skill`, `SkillFilesMixin.delete_skill_files`). This
leaf commits both in one transaction; companion failure rolls back the parent
and emits no event. Add the three `.atomic-soft-delete` entries for HTTP delete,
MCP remove_skill and CLI remove listed in the debt dispositions. Keep successful
delete/restore and collision behavior. Adapter leaves consume the same decision.

Add `.nonblocking-execution` entries for `skills.http-create` and
`skills.http-update`: await the shared native pool instead of blocking a request
executor. Preserve all response/state ordering. Also add the six D5 HTTP mutation
entries owned here (create/update/delete/restore/move-to-project/move-to-installed),
with existing envelopes and safe domain messages intact. Every entry cites this
section and has a concrete old/new vector; no error-class wildcard is a waiver.

**Granularity:** The acceptance items represent operations of one registry
lifecycle. The same transaction boundary and companion-file invariants make a
single independently shippable outcome; search ranking and source installation
are separate leaves. Presence, delete-failure atomicity and nonblocking execution
are corrections to that same locked mutation/notification owner, not new services.

**Acceptance:**

- 1.2.1 - `HTTP POST /api/skills` preserves create validation and results.
  test: `crates/gskills/tests/lifecycle.rs::create_matches_python`.
- 1.2.2 - `HTTP PUT /api/skills/{skill_id}` preserves partial updates,
  metadata/null distinctions and bundled restrictions.
  test: `crates/gskills/tests/lifecycle.rs::update_matches_python`.
- 1.2.3 - `HTTP DELETE /api/skills/{skill_id}` preserves soft deletion and
  companion-file atomicity. test: `crates/gskills/tests/lifecycle.rs::delete_matches_python`.
- 1.2.4 - `HTTP POST /api/skills/{skill_id}/restore` restores the complete
  record with conflict/error parity. test: `crates/gskills/tests/lifecycle.rs::restore_matches_python`.
- 1.2.5 - `HTTP POST /api/skills/{skill_id}/move-to-project` preserves target
  validation and collision rollback. test: `crates/gskills/tests/lifecycle.rs::move_project_matches_python`.
- 1.2.6 - `HTTP POST /api/skills/{skill_id}/move-to-installed` preserves scope
  transfer and collisions. test: `crates/gskills/tests/lifecycle.rs::move_installed_matches_python`.
- 1.2.7 - Fault injection proves rollback publishes no event; successful
  writes publish one existing event and invalidate search once.
  test: `crates/gskills/tests/lifecycle.rs::commit_controls_notifications`.
- 1.2.8 - Populated content-only, empty-object, explicit-null, False and empty
  Set updates implement Absent/Clear/Set and the merged-candidate contract;
  only exact D1 cases are Waived and unrelated fields stay compared.
  test: `crates/gskills/tests/lifecycle.rs::partial_update_presence_is_lossless`.
- 1.2.9 - Injected companion-delete failure leaves parent/files coherent and
  emits no event; the HTTP/MCP/CLI D3 effects have individual shared citations.
  test: `crates/gskills/tests/lifecycle.rs::soft_delete_failure_is_atomic`.
- 1.2.10 - A held database future does not stall unrelated requests; cancellation
  is bounded and creates no mutation/event, with D6 response/state parity intact.
  test: `crates/gskills/tests/lifecycle.rs::slow_mutations_do_not_block_the_executor`.
- 1.2.11 - Unexpected faults on all six mutation routes preserve status/envelopes,
  redact diagnostics through the explicit adapters and report only their D5
  entries; safe validation/conflict messages stay Passed.
  test: `crates/gskills/tests/lifecycle.rs::unexpected_mutation_errors_are_safe_and_waived`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo test -p gobby-skills --test lifecycle`.

### 1.3 Bounded skill parsing and validation [category: code] (depends: 1.2)
`kind: deliverable`

Targets:
- `crates/gskills/Cargo.toml`
- `Cargo.lock`
- `crates/gskills/src/lib.rs`
- `crates/gskills/src/parser.rs`
- `crates/gskills/src/validation.rs`
- `crates/gskills/tests/parser.rs`

**Research context:**
`src/gobby/skills/parser.py::parse_frontmatter`, `parse_skill_text`,
`extract_audience_config`, and `validate_runtime_metadata` define parsing.
`src/gobby/skills/validator.py::SkillValidator.validate` defines errors and
warnings. `src/gobby/skills/limits.py` bounds trust-boundary input. Use locked
`yaml_serde`/libyaml support, with a bounded event pass for aliases before
deserializing. Preserve the current PyYAML scalar behavior by fixtures, body
trimming, audience filters, levels/default-level metadata, runtime constraints,
unknown metadata and validation messages. Reject oversized input before
unbounded allocation; YAML failures are typed validation failures. Do not reuse
the runtime-config YAML 1.2 policy for skill frontmatter where it differs.

**Acceptance:**

- 1.3.1 - Valid/invalid frontmatter, YAML scalars, CRLF, missing separators,
  audience/levels/runtime metadata and validation warnings match the frozen
  corpus. test: `crates/gskills/tests/parser.rs::parser_and_validator_match_python`.
- 1.3.2 - Byte, alias, recursion, name and metadata limits reject before
  unbounded expansion and preserve structured failures.
  test: `crates/gskills/tests/parser.rs::hostile_inputs_are_bounded`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo test -p gobby-skills --test parser`.

### 1.4 Native safety scanner and scan route [category: code] (depends: 1.3, #24963)
`kind: deliverable`

Targets:
- `crates/gskills/Cargo.toml`
- `Cargo.lock`
- `crates/gskills/src/lib.rs`
- `crates/gskills/src/scanner.rs`
- `crates/gskills/assets/clawcare-default.json`
- `crates/gskills/assets/CLAWCARE-LICENSE.txt`
- `crates/gskills/assets/CLAWCARE-NOTICE.txt`
- `crates/gskills/src/http.rs`
- `crates/gskills/tests/scanner.rs`
- `docs/contracts/parity-waivers.json`

**Research context:**
`src/gobby/skills/scanner.py::scan_skill_content`, `scan_parsed_skill`, and
`scan_served_content` wrap ClawCare 0.9.0's 39 deterministic rules.
`src/gobby/servers/routes/skills.py::_scan_skill_install` enforces HTTP 422
unsafe and 503 unavailable behavior. The MCP install path fails closed for
hub/GitHub/ZIP/URL input when scanning is unavailable; local ingestion retains
its explicit warning behavior. Port effective rule data with attribution,
category/remediation metadata, Python flags, match/line ordering and Gobby's
`POST\b` correction to a bounded native evaluator using existing `fancy-regex`.
ClawCare 0.9.0 is distributed as Apache-2.0, as recorded by the
[versioned package](https://pypi.org/project/clawcare/0.9.0/) and
[upstream repository](https://github.com/AgentSafety/ClawCare). Preserve the
license, applicable upstream notices, source-version/hash provenance and a
notice of Gobby's rule correction with the native rule asset.
Scan every persisted text companion. Rule loading, compilation, match-budget
or evaluation errors mean scanner unavailable, never safe. Preserve serve-scan
content-hash caching and high/critical blocking. Do not introduce an LLM scanner.
Unexpected scan-route failures use the existing status/envelope with safe
`Internal server error` text and private diagnostics; typed unsafe/unavailable
and validation failures keep their intentional details. Add the single-surface
entry `skills.http-scan.internal-error-redaction` for
`HTTP POST /api/skills/scan` to #24963's shared manifest with required
id/surface/behaviour/decision/replacement fields and decision
`.gobby/plans/gdaemon-skills-family.md#1.4`. Assert native errors/effects in
Waived vectors rather than masking text or skipping the case.

**Granularity:** The data, evaluator, cache and HTTP adapter form one scanner
contract across more than six production targets; acquisition and persistence
remain separate.

**Acceptance:**

- 1.4.1 - `HTTP POST /api/skills/scan` returns matching findings, severity,
  category, remediation, location and safe/unsafe decisions, with only elapsed
  duration normalized. test: `crates/gskills/tests/scanner.rs::scan_route_matches_python`.
- 1.4.2 - Every effective rule and safe near-match passes the committed
  scanner corpus, including arbitrary-extension companions and postgres.
  test: `crates/gskills/tests/scanner.rs::all_effective_rules_match_python`.
- 1.4.3 - Missing/invalid rules, exhausted matching budgets and evaluator
  errors fail closed on external install and serve; local behavior is explicit.
  test: `crates/gskills/tests/scanner.rs::scanner_failures_never_become_safe`.
- 1.4.4 - Unexpected scan-route failures retain status/envelope and safe text
  under the exact waiver; deliberate scan errors stay ordinary parity and no
  failure gains a safe result. test: `crates/gskills/tests/scanner.rs::scan_internal_errors_are_safe`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo test -p gobby-skills --test scanner`.

### 1.5 Companion-file snapshots and HTTP file operations [category: code] (depends: 1.4, #24963)
`kind: deliverable`

Targets:
- `crates/gskills/src/lib.rs`
- `crates/gskills/src/files.rs`
- `crates/gskills/src/http.rs`
- `crates/gskills/tests/files.rs`
- `docs/contracts/parity-waivers.json`

**Research context:**
`src/gobby/storage/skills/_files.py::SkillFilesMixin.get_skill_with_manifest`,
`SkillFilesMixin.get_skill_file_page`, `SkillFilesMixin.get_skill_file`, and
`SkillFilesMixin.update_skill_file` define snapshot and path semantics.
`src/gobby/servers/routes/skills.py::export_skill`, `list_skill_files`,
`read_skill_file` and `write_skill_file` are HTTP adapters. Preserve forward-slash
relative paths, the 1,024-byte path limit, prefix/after-path pagination,
text-only storage, deterministic companion IDs, and soft-delete visibility.
Use a consistent database snapshot for content plus manifests, never a
metadata read followed by unrelated file reads. File changes invalidate load
hashes and script generations. Exports preserve the current serialized shape.
Consume the atomic parent/companion soft-delete transaction from 1.2:
failed deletion leaves both visible states unchanged. Add the exact entries
`skills.http-list-files.internal-error-redaction` for
`HTTP GET /api/skills/{skill_id}/files`,
`skills.http-read-file.internal-error-redaction` for
`HTTP GET /api/skills/{skill_id}/files/{file_path:path}`,
`skills.http-write-file.internal-error-redaction` for
`HTTP PUT /api/skills/{skill_id}/files/{file_path:path}`, and
`skills.http-export.internal-error-redaction` for
`HTTP GET /api/skills/{skill_id}/export` to #24963's manifest. Each has the
required id/surface/behaviour/decision/replacement fields and decision
`.gobby/plans/gdaemon-skills-family.md#1.5`. Only unexpected internal text
changes to `Internal server error`; status/envelope and safe domain errors
stay parity. Log internal diagnostics privately; assert responses and absence
of file/event writes in Waived vectors, never masks or Skipped cases.

**Acceptance:**

- 1.5.1 - `HTTP GET /api/skills/{skill_id}/files` matches filters, ordering
  and paging. test: `crates/gskills/tests/files.rs::list_files_matches_python`.
- 1.5.2 - `HTTP GET /api/skills/{skill_id}/files/{file_path:path}` matches
  content and missing/path errors. test: `crates/gskills/tests/files.rs::read_file_matches_python`.
- 1.5.3 - `HTTP PUT /api/skills/{skill_id}/files/{file_path:path}` matches
  update semantics, limits and invalidation. test: `crates/gskills/tests/files.rs::write_file_matches_python`.
- 1.5.4 - `HTTP GET /api/skills/{skill_id}/export` preserves the full export
  payload. test: `crates/gskills/tests/files.rs::export_matches_python`.
- 1.5.5 - Concurrent edits cannot mix versions in a snapshot; traversal,
  invalid prefixes, oversized paths and deleted parents fail safely.
  test: `crates/gskills/tests/files.rs::snapshots_are_atomic_and_scoped`.
- 1.5.6 - Each of the four HTTP file/export surfaces has safe unexpected-error
  text and unchanged status/envelope under its exact waiver; failures leave
  content, visibility and notifications unchanged.
  test: `crates/gskills/tests/files.rs::file_internal_errors_are_safe`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo test -p gobby-skills --test files`.

### 1.6 Paged instruction delivery and completed-load state [category: code] (depends: 1.5, #24884, #24963)
`kind: deliverable`

Targets:
- `crates/gskills/src/lib.rs`
- `crates/gskills/src/delivery.rs`
- `crates/gskills/src/paging.rs`
- `crates/gskills/tests/delivery.rs`
- `docs/contracts/parity-waivers.json`

**Research context:**
`src/gobby/mcp_proxy/tools/skills/get_skill.py::get_skill`,
`get_skill_file_tool`, `get_skill_files_tool` and `record_completed_load`
combine snapshots, level projection, serve scanning and tracking.
`src/gobby/mcp_proxy/tools/skills/_paging.py::CursorState`, `decode_cursor` and
`build_content_page` define 15,000-byte response budgeting. Preserve Unicode
character boundaries, exact content bytes, brief/full projection, reference
manifests and limits. Valid issued sequential streams remain parity;
cursor-only continuations retain the initial view. The Python cursor is
caller-editable JSON and does not prove prefix delivery. Replace it with
opaque server-issued random handles using existing random/UUID primitives and
one hub-owned in-process chain map. Signing an arbitrary offset is insufficient.
Bind each chain to trusted caller, caller session/reset generation, resolved
tracking identity/generation, immutable content snapshot/hash, entry/reference
path, requested level, initial view and exact expected offset. Consume #24884's
trusted ToolContext generation from the hub compact/clear boundary record;
never accept a client epoch or create a second reset tracker. Sessionless
reads keep their existing no-tracking semantics.

Limit the map to 4,096 total live/finalizing chains and expire idle active
chains after fifteen minutes. Reject capacity before starting a new stream;
reclaim expired active chains without effects. Restart invalidates handles.
Changed content returns `stale_cursor`; malformed, altered, wrong-kind,
wrong-caller, replayed, expired and reset-invalidated handles return
`invalid_cursor`, with restart guidance. Capacity/finalization failures use
the existing unavailable envelope. An async per-chain transition permits one
consumer of an expected handle; failed authorization/validation leaves the
rightful handle usable. No std mutex is held across await. Single-page full
responses use the same completion fence as final continuation pages.

Completed entrypoints append once in first-load order to `loaded_skills` and
idempotent `session_skills`, and set the effective `<skill>_level`; completed
references append only to `loaded_skill_references`. Partial, failed and
listing reads record nothing. Resolve explicit target references separately
from caller context. Finalization runs in a service-owned task retained on
caller cancellation. Serialize completions per tracking session/generation,
including different levels, lock the hub boundary record in reset's lock order,
and recheck both bound generations. Apply the session_skills insert, ordered
loaded-set append and effective-level set in one database transaction;
references use their separate set without a level write. Confirmed rollback
restores the final handle for retry. Confirmed commit consumes it once; replay
cannot repeat effects. Unknown commit remains Finalizing, counts toward the
bound, and is reconciled by authoritative read-back under the same fence,
never by a blind write retry. A changed generation invalidates without
reapplying effects. Contradictory or unreconciled state returns unavailable
with no success/further writes and remains quarantined until resolution/reset;
never overwrite a newer level or a completed reset. No receipt table, schema,
service, configuration knob or dependency is introduced.

Observed `record_completed_load` makes three separate best-effort calls and
swallows tracking failures (excerpt_hash
`d63553c2450a7470b46bba12ab7834b29756a15f508f8dead0a47b1058fe08a0`).
`src/gobby/storage/sessions/_terminal.py::_TerminalMixin.record_skills_used`
already inserts with `ON CONFLICT (session_id, skill_name) DO NOTHING`
(excerpt_hash
`3e4be34e9f790fe60b57b06baf67838d78f9a9a91c93d9a70c808561dde5dba3`):
tracking has no incrementing counter or event receipt to preserve.
Retain intentional successful load ordering and scope, while replacing the
forged-completion and partial-finalization behavior. Coverage establishes
delivered pages, never proof that a client read them.

Add `skills.mcp-get-skill.cursor-progression` for
`MCP gobby-skills:get_skill` and
`skills.mcp-get-skill-file.cursor-progression` for
`MCP gobby-skills:get_skill_file` to #24963's shared manifest. Each required
id/surface/behaviour/decision/replacement entry cites
`.gobby/plans/gdaemon-skills-family.md#1.6` and covers the bounded progression,
lifetime and completion replacement above. Native vectors explicitly assert
bytes, errors and load effects as Waived; existing ordinary sequential,
malformed and stale-content cases stay Passed. Never mask or skip differences.

**Granularity:** The paging chain and completion fence are one instruction
delivery contract: neither can establish a load independently. Keep their
failure/race tests together despite more than six acceptance items.

**Acceptance:**

- 1.6.1 - Full/brief entrypoint and reference streams reconstruct exact
  snapshot content within every serialized response budget.
  test: `crates/gskills/tests/delivery.rs::pages_reconstruct_exact_content`.
- 1.6.2 - Cursor-only requests retain initial projection; valid sequential
  bytes, Unicode, ordinary invalid and stale-content errors retain parity,
  while the two exact waivers assert rejected forged/skipped-prefix handles.
  test: `crates/gskills/tests/delivery.rs::cursor_contract_matches_python`.
- 1.6.3 - Only final successful pages record ordered, deduplicated load
  identities and effective levels; references have separate tracking and
  context reset requires a fresh load. test: `crates/gskills/tests/delivery.rs::completion_controls_load_tracking`.
- 1.6.4 - Unsafe entrypoint/reference content and scanner failures deliver
  no instruction body or completed-load side effect.
  test: `crates/gskills/tests/delivery.rs::serve_scan_precedes_delivery`.
- 1.6.5 - Concurrent use of one handle permits exactly one advance/completion;
  wrong caller, altered identity/view/level/path and failed validation have no
  load effects and do not consume the rightful handle.
  test: `crates/gskills/tests/delivery.rs::cursor_progression_is_single_use_and_bound`.
- 1.6.6 - Expiry, capacity, restart and same-session compact/clear invalidate
  or refuse streams with the specified errors and no load effects; trusted
  generation, never a client epoch, fences post-reset reuse.
  test: `crates/gskills/tests/delivery.rs::cursor_lifetimes_are_bounded_and_reset_safe`.
- 1.6.7 - Single-page and paged completion share one ordered atomic tracking
  transaction; competing levels and reset cannot produce a mixed or stale load.
  test: `crates/gskills/tests/delivery.rs::completion_is_atomic_and_generation_fenced`.
- 1.6.8 - Caller cancellation retains owned finalization; confirmed rollback
  permits retry, confirmed commit consumes once, and uncertain commit is
  read-back reconciled or quarantined without success/extra writes or newer-level
  overwrite. test: `crates/gskills/tests/delivery.rs::completion_failures_reconcile_without_duplicate_effects`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo test -p gobby-skills --test delivery`.
Use isolated hub/session state, a controlled clock, concurrent continuations,
transaction fault injection and the real #24884 generation/reset seam.

### 1.7 Search, list projection and index freshness [category: code] (depends: 1.6, #24463, #24963)
`kind: deliverable`

Targets:
- `crates/gskills/src/lib.rs`
- `crates/gskills/src/search.rs`
- `crates/gskills/src/http.rs`
- `crates/gskills/tests/search.rs`
- `docs/contracts/parity-waivers.json`

**Research context:** `src/gobby/skills/search.py::SkillSearch.search_async`,
`SkillSearch._passes_filters`, `SkillSearch._build_search_content`, and
`SkillSearch._local_keyword_search` define ranking/filter behavior.
`src/gobby/mcp_proxy/tools/skills/search_skills.py::_SkillIndexer.ensure_fresh`
and `search_skills` define the 10,000-skill index ceiling and projection.
Reuse gcore's `sanitize_pg_search_query`, `rrf_merge`, embedding and Qdrant
clients where their semantics match; retain the Skills-specific local keyword
fallback, scoring, post-ranking filters, oversampling and capability/audience
selection rather than replacing them with a convenient unrelated score.
Consume active search configuration from #24463. Registry and companion changes
invalidate the same service index; restart can reconstruct it from authoritative
rows. Before each search, read the bounded visible-row snapshot and compute
a digest over IDs, versions/timestamps, scope, enable/delete state and indexed
content. Reuse an index only for the same digest; rebuild from that snapshot
otherwise. This catches independent Python/CLI writers during compare and
rollback without adding a notification service or trusting only process-local
callbacks. Surface existing degradation metadata and avoid holding DB transactions
across embedding requests. No new search daemon or embedding store.
The HTTP Python search path blocks its async request executor, whereas MCP
already awaits `search_async`/`ctx.run_db`. Native HTTP uses the shared async
pool/provider futures and bounded blocking work where needed. Slow search must
leave unrelated requests progressing; cancellation finishes bounded cleanup
and publishes no incomplete index. Add the exact entries
`skills.http-search.nonblocking-execution` and
`skills.http-search.internal-error-redaction`, each for
`HTTP GET /api/skills/search`, to #24963's shared manifest with all required
id/surface/behaviour/decision/replacement fields and decision
`.gobby/plans/gdaemon-skills-family.md#1.7`. Only scheduling and unexpected
internal text change; retain response/ranking/freshness and intentional
degradation/errors. Unexpected text becomes `Internal server error` in the
existing envelope, with diagnostics private. Assert each replacement and
effect in Waived vectors; no masks, Skipped case or MCP scheduling waiver.

**Acceptance:**

- 1.7.1 - `HTTP GET /api/skills/search` matches keyword/hybrid modes,
  filters, ordering, empty results and errors.
  test: `crates/gskills/tests/search.rs::http_search_matches_python`.
- 1.7.2 - Native list/search projections preserve audience, capability,
  enabled/scope/category/tag filters, brief metadata, top-k and fallback
  explanations. test: `crates/gskills/tests/search.rs::projections_and_ranking_match_python`.
- 1.7.3 - Create/update/delete/move/restore, independent DB writers,
  concurrent search and restart do not serve a stale scope or resurrect a
  deleted result. test: `crates/gskills/tests/search.rs::index_freshness_survives_mutation_and_restart`.
- 1.7.4 - Controlled slow HTTP search permits unrelated requests to progress;
  cancellation is bounded and leaves no incomplete index or state writes.
  test: `crates/gskills/tests/search.rs::slow_search_does_not_stall_requests`.
- 1.7.5 - HTTP search unexpected failures retain status/envelope with safe
  text under their exact waiver; deliberate degraded/domain outcomes retain
  parity and no failed query mutates the index.
  test: `crates/gskills/tests/search.rs::search_internal_errors_are_safe`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo test -p gobby-skills --test search`.

## P2: Source acquisition and installation
`kind: framing`

### 2.1 Native node-local skill filesystem boundary [category: code] (depends: 1.7)
`kind: deliverable`

Targets:
- `crates/gskills/src/lib.rs`
- `crates/gskills/src/local.rs`
- `crates/gskills/src/effects.rs`
- `crates/gdaemon/src/node_commands.rs::*` — scope-reason: split existing workspace payload types and add typed Skills command/reply variants
- `crates/gdaemon/src/node_commands/workspace.rs`
- `crates/gdaemon/src/node_commands/skills.rs`
- `crates/gdaemon/src/node_commands/frame_dispatch.rs::*` — scope-reason: dispatch the added typed Skills command and reply frames alongside existing variants
- `crates/gdaemon/src/nodes/channel.rs::*` — scope-reason: extend receive-frame dispatch, validation and failure handling for the native Skills command receiver
- `crates/gdaemon/src/nodes/skills.rs`
- `crates/gdaemon/src/nodes/mod.rs::*` — scope-reason: export the Skills node command receiver and connect it to node services
- `crates/gdaemon/src/front_door/skills.rs`
- `crates/gdaemon/tests/skills_node.rs`
- `crates/gdaemon/tests/fixtures/node_commands/corpus.json::*` — scope-reason: extend the indexed JSON command/reply corpus with Skills cases while preserving existing cases

**Research context:** `src/gobby/skills/loader.py::SkillLoader.load_skill`
and `_load_skill_files` load local roots.
`src/gobby/servers/routes/skills.py::_resolve_project_import_path` resolves
machine/project context. The native channel already has
`crates/gdaemon/src/nodes/registry.rs::Registry::command_sender` and
`CommandSender::request`; `crates/gdaemon/src/nodes/channel.rs::run_client`
currently consumes frames without executing Skills commands. Add a typed
`skills.local` command with enumerated read_snapshot/scaffold/write_document
operations, selected machine and bounded payloads; a later materialize operation
uses the same envelope. Hub authority validates project/caller selection before
sending. The node resolves roots against its project checkout and rejects
symlink/traversal escapes. Network sources are acquired on the hub; local
content is read on the selected node. CLI-local operations call the same local
implementation directly when standalone.

Every Skills effect carries a unique request ID, connection identity,
`deadline_unix_ms` and a positive `max_run_ms` capped at 600,000. The hub sets
both from the caller's remaining budget. On receipt the node rejects expired
deadlines and starts a monotonic timer bounded by both the remaining deadline
and max_run_ms; wall-clock skew can shorten or reject work, never remove the
monotonic bound. This is private transport metadata, not a new public MCP input
or configurable job system. The proposed `skills.cancel` control command names
an existing request ID on the same connection; repeated/unknown cancellations
are idempotent and cannot cancel another connection's work.

Keep a Skills-only supervisor for each outbound request in
`crates/gdaemon/src/nodes/skills.rs`. A caller drop closes its response receiver;
the supervisor observes closure, hub timeout or shutdown, sends cancellation
over the existing channel, and retains cleanup ownership until the node's
terminal reply. Do not depend on dropping `CommandSender::request`: observed
`PendingGuard::drop` only performs `45| entries.remove(&self.id);` in
`crates/gdaemon/src/nodes/registry.rs`, excerpt_hash
`3ab45311ec290fe6c87ac8eb2554fa833295c20a7c04d43760c17d298d6c3a34`.
Use existing Tokio oneshot/watch channels and tracked tasks; add no general job
queue or new cancellation dependency. If cancellation delivery/acknowledgement
fails, report cleanup-unconfirmed diagnostics, never success or a replay; the
node-enforced deadline still bounds work.

The node receiver tracks each Skills future and its cancellation signal within
the live connection. Deadline/cancel stops owned preparation work, drains it,
then emits its one terminal reply; cancellation acknowledgement follows drain.
Short atomic file publication either finishes or abandons its owned stage before
acknowledgement. Disconnect/shutdown cancels and drains all connection-owned
Skills work before reconnect. Unknown late replies are discarded. The next
connection never inherits a previous request or child process.

Split `crates/gdaemon/src/node_commands.rs` by moving its existing workspace
command/payload/result types into the new
`crates/gdaemon/src/node_commands/workspace.rs`, preserving public re-exports
and existing fixture compatibility. The observed production prefix is 979
lines, so adding variants without this split violates the growth heuristic.

**Granularity:** More than six production targets implement one bounded
hub-to-node filesystem boundary, including the required size repair. Script
publication, dependency preparation and remote acquisition remain separate.

**Acceptance:**

- 2.1.1 - Standalone and hub/node local snapshot/scaffold/document effects
  execute on the intended machine with no hub datastore credential on nodes.
  test: `crates/gdaemon/tests/skills_node.rs::effects_are_machine_scoped`.
- 2.1.2 - Wrong machine/project, disconnected nodes, timeout, malformed
  payloads, symlink escapes and oversized reads return typed failures without
  a hub-local fallback or automatic replay of writes.
  test: `crates/gdaemon/tests/skills_node.rs::invalid_effects_fail_closed`.
- 2.1.3 - The protocol split preserves existing wire fixtures and public
  type consumers, and every affected production file stays below 1,000 lines.
  test: `crates/gdaemon/tests/skills_node.rs::protocol_extension_preserves_existing_commands`.
- 2.1.4 - Caller drop and timeout cancel a held node effect over a healthy
  connection; expired commands, duplicate cancellation, late replies and
  disconnect/reconnect preserve connection/request ownership and drain work.
  test: `crates/gdaemon/tests/skills_node.rs::remote_effect_lifetime_is_bounded`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo test -p gobby-daemon --test skills_node`;
`CARGO_BUILD_JOBS=3 cargo test -p gobby-daemon --test node_command_corpus corpus_round_trips`.

### 2.2 Bounded GitHub and ZIP source loading [category: code] (depends: 2.1)
`kind: deliverable`

Targets:
- `crates/gskills/Cargo.toml`
- `Cargo.lock`
- `crates/gskills/src/lib.rs`
- `crates/gskills/src/source.rs`
- `crates/gskills/src/source_github.rs`
- `crates/gskills/src/source_zip.rs`
- `crates/gskills/tests/sources.rs`

**Research context:**
`src/gobby/skills/loader.py::SkillLoader.load_from_github`,
`SkillLoader.load_from_zip`, and `SkillLoader.clean_untrusted_skill`, plus
`src/gobby/skills/_loader_github.py` and `_loader_zip.py`, define acquisition.
The HTTP source predicates distinguish explicit URLs, owner/repo forms and
existing local paths. Use reqwest and the existing clone/checkout behavior
through argument-array Git commands, not a shell. ZIP uses bounded streaming
decoding, member-count/expanded-byte limits, no extraction outside a temporary
root, and no symlink or absolute/traversal members. Normalize all sources into
the same parsed snapshot; strip untrusted authority metadata exactly as today.
Retain branch/path provenance, binary exclusions, cleanup, cancellation and
source errors. No archive or fetch subprocess may execute downloaded content.

**Granularity:** GitHub and ZIP are two adapters of one bounded acquisition
pipeline, independently covered by fixtures; persistent install/update state
is owned by 2.5. More than six production targets include the dependency carrier.

**Acceptance:**

- 2.2.1 - Explicit/implicit GitHub forms, revisions/subpaths, local precedence,
  binary/companion selection and untrusted metadata match Python.
  test: `crates/gskills/tests/sources.rs::github_and_source_resolution_match_python`.
- 2.2.2 - ZIP contents and failures match the corpus, including decompression
  bombs, traversal, links, malformed archives, limits and cleanup.
  test: `crates/gskills/tests/sources.rs::zip_inputs_are_bounded_and_equivalent`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo test -p gobby-skills --test sources`,
using local HTTP/Git fixtures and no live third-party accounts.

### 2.3 Native API hub providers [category: code] (depends: 2.2, #24448, #24463, #24963)
`kind: deliverable`

Targets:
- `crates/gskills/src/lib.rs`
- `crates/gskills/src/hubs.rs`
- `crates/gskills/src/hubs_api.rs`
- `crates/gskills/src/http.rs`
- `crates/gskills/tests/hubs_api.rs`
- `docs/contracts/parity-waivers.json`

**Research context:**
`src/gobby/skills/hubs/manager.py::HubManager._create_provider`,
`HubManager.search_all`, and `resolve_hub_api_keys` own configuration and auth.
`src/gobby/skills/hubs/clawdhub.py` and `skillsmp.py` implement API providers.
Use an enum of the existing provider kinds, preserving configured names,
enabled/auth status, base URLs, query/limit handling and per-hub error isolation.
Consume #24463 active config and #24448 secret access; never expose secret
values in list/search/logs. Preserve streaming bounds, timeouts, redirects,
authentication errors, cache behavior and deterministic result projection.
The GitHub-backed provider variants become available through 2.4; do not
silently report unsupported configured providers as empty results.
Add `skills.http-list-hubs.internal-error-redaction`, bound exactly to
`HTTP GET /api/skills/hubs`, to #24963's shared manifest with all required
fields and decision `.gobby/plans/gdaemon-skills-family.md#2.3`. Only
unexpected internal text becomes `Internal server error`; existing
status/envelope and safe provider/domain errors remain parity. Log private
diagnostics and assert the replacement/no effects as Waived, never masked
or skipped. Use the shared typed error classes and explicit HTTP adapter.

**Acceptance:**

- 2.3.1 - `HTTP GET /api/skills/hubs` matches configured hub names, types and
  auth status without secrets. test: `crates/gskills/tests/hubs_api.rs::list_hubs_matches_python`.
- 2.3.2 - Clawdhub and SkillsMP query/download behavior matches fixture
  requests and responses, including auth, timeout, rate-limit, malformed and
  oversized payload failures. test: `crates/gskills/tests/hubs_api.rs::api_providers_match_python`.
- 2.3.3 - Unexpected list-hubs errors preserve status/envelope with safe
  text under the exact waiver and no secret values or state writes; intentional
  provider failures retain parity.
  test: `crates/gskills/tests/hubs_api.rs::hub_list_internal_errors_are_safe`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo test -p gobby-skills --test hubs_api`.

### 2.4 GitHub collection, topic and Claude-plugin hubs [category: code] (depends: 2.3, #24963)
`kind: deliverable`

Targets:
- `crates/gskills/src/lib.rs`
- `crates/gskills/src/hubs.rs`
- `crates/gskills/src/hubs_github.rs`
- `crates/gskills/src/hubs_plugins.rs`
- `crates/gskills/src/http.rs`
- `crates/gskills/tests/hubs_github.rs`
- `docs/contracts/parity-waivers.json`

**Research context:** `src/gobby/skills/hubs/github_collection.py`,
`github_topic.py`, and `claude_plugins.py` contain current adapters and tests
under `tests/skills/hubs/`. Preserve collection repo/branch/path discovery,
topic cache TTL and 256 KiB cap, plugin marketplace structure, nested skill
files and descriptions, including configured AI description generation through
the existing gcore AI service. Apply invisible-content filtering and all shared
stream bounds. Reuse 2.2 acquisition; cache keys include configuration and
revision. No provider subclass registration mechanism or live-API dependency in
tests. Wire combined search only when all existing provider kinds are served.
Add `skills.http-search-hubs.internal-error-redaction` for exactly
`HTTP GET /api/skills/hubs/search` to #24963's manifest with all required
fields and decision `.gobby/plans/gdaemon-skills-family.md#2.4`. Unexpected
route-level failures receive `Internal server error` in the existing envelope;
safe per-provider partial errors/status remain parity. Private diagnostics
use the shared typed-error convention. Waived vectors assert the safe response
and no unauthorized cache/state publication; no masks or Skipped cases.

**Granularity:** The three GitHub-shaped adapters share acquisition and
marketplace discovery, with separate fixtures. More than six production targets
are justified by keeping adapters below the production ceiling.

**Acceptance:**

- 2.4.1 - Collection, topic and Claude-plugin discovery/downloads preserve
  source IDs, paths, descriptions, metadata, authentication and bounds.
  test: `crates/gskills/tests/hubs_github.rs::repository_providers_match_python`.
- 2.4.2 - `HTTP GET /api/skills/hubs/search` preserves single/all-hub
  search, result limits, ordering and partial-error handling.
  test: `crates/gskills/tests/hubs_github.rs::combined_hub_search_matches_python`.
- 2.4.3 - Description generation and unavailable-generation behavior match
  the current configured contract; cache expiry and config changes refresh
  results. test: `crates/gskills/tests/hubs_github.rs::descriptions_and_cache_match_python`.
- 2.4.4 - Unexpected hub-search failures preserve status/envelope with safe
  text under the exact waiver, while intentional partial-provider outcomes
  retain parity and failed acquisition publishes no partial result cache.
  test: `crates/gskills/tests/hubs_github.rs::hub_search_internal_errors_are_safe`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo test -p gobby-skills --test hubs_github`.

### 2.5 Scanned installation and source updates [category: code] (depends: 2.4, #24963)
`kind: deliverable`

Targets:
- `crates/gskills/src/lib.rs`
- `crates/gskills/src/install.rs`
- `crates/gskills/src/update.rs`
- `crates/gskills/src/http.rs`
- `crates/gskills/tests/install.rs`
- `docs/contracts/parity-waivers.json`

**Research context:** `src/gobby/mcp_proxy/tools/skills/install_skill.py::install_skill`,
`src/gobby/skills/updater.py::SkillUpdater.update_skill`,
`SkillUpdater._scan_update`, `_create_backup`, `_restore_backup`,
and the HTTP `import_skill`/`install_from_hub` adapters define the pipeline.
Acquire and parse outside the transaction, scan the entire snapshot, then
persist metadata plus companion files atomically. Preserve source identity,
project-installed selection, duplicates, overwrite/update options, unchanged
results, file removals and search invalidation. Prepare fetch, parse,
validation and full-snapshot scan before any registry write. At the write
boundary lock/recheck the captured source snapshot; a conflicting update fails
without overwriting a newer commit. Pre-write failure performs no compensating
metadata restore, timestamp/event/index write or backup. Preserve the real
`SkillUpdateResult` fields: skill_id, skill_name, success, updated, error,
backup_created, rolled_back, skipped and skip_reason. Pre-write acquisition,
parse, validation, scan and conflict failures return success=False,
updated=False, backup_created=False and rolled_back=False. Create a backup
only at an actual write boundary; rolled_back=True requires confirmed rollback
of actual writes. Preserve the legitimate no-source `skipped` business result;
that field does not authorize a compare `Skipped` case. Failed transactional
writes retain the prior usable record/companions. Update-all reports per-skill
outcomes without turning partial failure into success. HTTP unsafe/unavailable
status codes and MCP local-warning versus external-fail-closed policy stay
transport-specific projections of the same scan result.

Add `skills.mcp-update-skill.failed-refresh-no-write` for
`MCP gobby-skills:update_skill` and `skills.cli-update.failed-refresh-no-write`
for `CLI gobby skills update` to #24963's shared manifest. Add
`skills.http-hub-install.internal-error-redaction` for
`HTTP POST /api/skills/hubs/install`; unexpected text alone becomes
`Internal server error` in its existing envelope, with private diagnostics.
Each entry has id/surface/behaviour/decision/replacement and decision
`.gobby/plans/gdaemon-skills-family.md#2.5`. Native fault/concurrency vectors
assert honest result flags and real before/after timestamps, events, metadata
and files under exact Waived citations. No timestamp mask, compensating write
or Skipped case hides failure effects. HTTP import already uses safe generic
unexpected text and keeps ordinary parity without a D5 waiver.

**Acceptance:**

- 2.5.1 - `HTTP POST /api/skills/import` matches local/GitHub/ZIP resolution,
  validation, full-surface scanning and transactional results.
  test: `crates/gskills/tests/install.rs::http_import_matches_python`.
- 2.5.2 - `HTTP POST /api/skills/hubs/install` matches source retrieval,
  auth, scan failures and installed metadata/files.
  test: `crates/gskills/tests/install.rs::http_hub_install_matches_python`.
- 2.5.3 - Install/update/update-all preserve provenance, changed/unchanged
  outcomes and atomic replacement; post-write rollback is reported only when
  confirmed, and concurrent source changes are rechecked under the row lock.
  test: `crates/gskills/tests/install.rs::updates_are_atomic_and_scanned`.
- 2.5.4 - Fetch/parse/validation/scan/conflict failures cause zero writes,
  timestamps, events, index changes or backups, return the specified false
  result flags, and preserve a newer concurrent edit under the two exact D4
  waivers. test: `crates/gskills/tests/install.rs::failed_refresh_never_writes_or_claims_rollback`.
- 2.5.5 - Unexpected hub-install errors keep status/envelope with safe text
  under the exact waiver and no partial writes; safe scan/domain errors and
  already-sanitized HTTP import retain parity.
  test: `crates/gskills/tests/install.rs::install_internal_errors_are_safe`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo test -p gobby-skills --test install`.

### 2.6 Native bundled synchronization and default restoration [category: code] (depends: 2.5, #24963)
`kind: deliverable`

Targets:
- `crates/gskills/src/lib.rs`
- `crates/gskills/src/bundled.rs`
- `crates/gskills/src/startup.rs`
- `crates/gskills/assets/bundled-skills.json`
- `scripts/build_bundled_skills_asset.py`
- `crates/gskills/src/http.rs`
- `crates/gskills/tests/bundled.rs`
- `docs/contracts/parity-waivers.json`
- `crates/gdaemon/src/front_door/skills.rs`
- `crates/gdaemon/src/serve.rs::*` — scope-reason: invoke native Skills dev synchronization after startup guards and before spawning the compatibility backend or serving Skills requests
- `crates/gdaemon/src/main.rs::Command`
- `crates/gdaemon/src/main.rs::main`
- `src/gobby/skills/native_sync.py`
- `src/gobby/sync_registry.py::SYNC_TARGETS`
- `src/gobby/runner_init/storage.py::run_startup_content_sync`
- `tests/runner_init/test_runner_init_storage.py::*` — scope-reason: preserve guarded dev startup and rule migration assertions while asserting Skills is skipped by the Python publisher
- `tests/test_runner_lifecycle.py::*` — scope-reason: update startup synchronization expectations for the single native Skills publisher
- `tests/skills/test_native_sync.py`
- `tests/skills/test_reference_migration.py::*` — scope-reason: direct Skills aggregation integration coverage uses the native bridge while preserving reference-migration assertions
- `crates/gdaemon/tests/skills_startup.rs`

Consumers unchanged:
- `src/gobby/cli/install_setup.py` — no-edit-reason: the first-install aggregator call keeps its signature and result fields while its Skills entry delegates natively.
- `src/gobby/cli/installers/shared.py` — no-edit-reason: the thin aggregator wrapper forwards the existing only/skip_types contract unchanged.
- `src/gobby/cli/sync.py` — no-edit-reason: explicit sync and reinstall retain their existing selection, integrity checks and aggregate result handling.
- `src/gobby/mcp_proxy/tools/workflows/_import.py` — no-edit-reason: reload keeps its selected types and aggregate result contract through the central Skills delegation.
- `tests/cli/installers/test_bundled_sync_logging.py` — no-edit-reason: its substituted SYNC_TARGETS and aggregate logging assertions retain the same tuple/result contract.
- `tests/cli/test_cli_sync_coverage.py` — no-edit-reason: content-type selection and substituted aggregator entries retain their current shape and behavior.
- `tests/mcp_proxy/tools/workflows/test_registry_surface.py` — no-edit-reason: the registry tuple retains the same Skills content-type name and callable contract.
- `tests/sync/test_integrity.py` — no-edit-reason: integrity type-name mapping is unchanged by the Skills callable's module path.

**Research context:** `src/gobby/skills/sync.py`,
`src/gobby/storage/skills/_bundled.py`, and the route
`src/gobby/servers/routes/skills.py::restore_defaults` own bundled templates.
The install-shared template/override contract and installed DB row determine
effective state. Generate a native embedded source asset from the existing
bundled skills and companions, with content hashes and no second hand-authored
copy. Port template synchronization, installed overrides, bundled content-size
validation, stale template cleanup and restore counts. Restore-defaults must
preserve user-installed/project skills and the established override policy.
The asset generator is build/test tooling; runtime restoration uses native
bytes and database transactions. Source edits require asset-freshness checks.
Add `skills.http-restore-defaults.internal-error-redaction` for exactly
`HTTP POST /api/skills/restore-defaults` to #24963's shared manifest with
id/surface/behaviour/decision/replacement and decision
`.gobby/plans/gdaemon-skills-family.md#2.6`. Unexpected internal text becomes
`Internal server error` in the existing status/envelope; intentional size,
source-integrity and override/domain diagnostics stay parity. Log private
diagnostics and assert the native response and unchanged rows/events in an
exact Waived vector, never masks or Skipped cases.

The lifecycle trigger is explicit. In hub/standalone authority modes,
serve composition calls the native bundled service directly, after database,
worktree and source-integrity preflight and before spawning the Python
compatibility backend or exposing Skills routes. It uses the existing
`src/gobby/utils/dev.py::is_dev_mode` contract: the current directory or an
ancestor is the Gobby source project. In dev mode it reads the checked, clean
installed source snapshot through the native loader and synchronizes it once.
This retains live-source development behavior rather than always substituting
possibly older embedded bytes. A repeated clean startup is idempotent. A node
never publishes, and non-dev startup performs no bundled Skills sync even on
an empty database. Installation is the separate first-seed trigger.

Port the effective source-integrity decision from
`src/gobby/sync/integrity.py::dirty_bundled_content_refusal` into the native
startup helper: dirty/untracked protected inputs and incomplete Git/manifest
checks refuse before any publish; ownership attribution remains diagnostic;
the existing `GOBBY_ALLOW_DIRTY_BUNDLED_CONTENT` override applies only when
exactly `1`. Preserve linked-worktree startup refusal. Native source reads use
the same verified install root, never the caller's arbitrary checkout.
Observed `src/gobby/runner_init/storage.py::bundled_content_refusal` states
`51| must precede every bundled publish in init_startup_content.`, excerpt_hash
`d11863b46039dc9071111495c6b427812c6a26933762fc9d553f01e7520385aa`;
`run_startup_content_sync` states `65| Non-dev startup skips the aggregator and user-template import.`,
excerpt_hash `3cfc4238f2108fda567cb553f7f312ebbe58215968cc065d21649cbe0977934d`.

At this ownership handoff, Python dev startup calls its existing aggregator
with `skip_types={"skills"}` while retaining other content sync and the always-run
rule-disposition migration. Native Skills startup publication owns this step
for all REST backend selections, including proxy rollback; route rollback does
not reassign bundled publication to a second process. This code ships with
6.1's native family release, so there is no installed intermediate state with
both publishers enabled. A compatibility-backend restart does not repeat native
startup sync.

Explicit install, sync, reinstall and workflow-reload calls keep their public
aggregator entry points. Change only the Skills row of the existing
`SYNC_TARGETS` to a thin `src/gobby/skills/native_sync.py` adapter. It invokes
the private native command `gdaemon skills-internal sync-bundled --json` via
the existing managed-binary resolver and subprocess conventions. Pass the
same supplied database identity from the existing `HubDatabase.conninfo`
contract through `GOBBY_DATABASE_URL` in the child
environment and the verified source root as a literal path argument; never
serialize credentials into command arguments or logs. The private command
opens the shared pool, verifies schema identity, calls the Rust bundled service
directly and exits with the existing aggregate result fields. It never starts
serve, calls the Python aggregator, or dispatches through the public Skills CLI,
so delegation cannot recurse and works with the daemon/Python backend absent.
Missing or incompatible native binaries fail explicitly without Python fallback.

For first install or explicit non-dev sync, use the embedded generated asset;
explicit dev sync uses the verified source snapshot. Keep existing caller-side
only/skip_types selection and dirty-source/force policy, with equivalent native
content/path validation before persistence. The strict dirty-source refusal is
the startup rule; an explicit operator sync has already selected authorized
types through its existing dev/force/integrity policy and must not be refused
by accidentally applying startup-only policy again. The private local command
is operator-only and is not exposed through the agent gateway. The central
adapter handles only Skills and
leaves the install/sync/workflow callers listed above as transitional no-edit
consumers. They retire with their owning families/global naming switch, not by
duplicating their implementation here.
The naming-switch cleanup inventory explicitly includes the
`src/gobby/skills/native_sync.py` adapter, the private `skills-internal`
command once no Python aggregator caller remains, and the Python startup
`skip_types={"skills"}` handoff. Native startup publication continues directly
through the Rust service after that cleanup.

**Granularity:** The production target count exceeds six because one bundled
publication owner crosses native startup, private install invocation and the
existing Python aggregator. These are direct triggers of the same idempotent
transactional service, with no new scheduler or independent registry lifecycle.
Source acquisition and external installation remain separate leaves.

**Acceptance:**

- 2.6.1 - `HTTP POST /api/skills/restore-defaults` matches counts, template
  content, companion restoration and override preservation.
  test: `crates/gskills/tests/bundled.rs::restore_defaults_matches_python`.
- 2.6.2 - Repeated synchronization is idempotent; asset hashes equal bundled
  sources, and configured bundled-size violations fail visibly.
  test: `crates/gskills/tests/bundled.rs::bundled_asset_and_sync_are_current`.

- 2.6.3 - Native dev startup synchronizes once before serving or launching
  Python; repeated startup is idempotent, dirty/unreadable inputs refuse before
  writes, the exact existing override is honored, and non-dev/node startup
  writes no bundled rows. Installed overrides remain intact.
  test: `crates/gdaemon/tests/skills_startup.rs::startup_policy_preserves_single_publisher`.
- 2.6.4 - A fresh non-dev installation and explicit sync/reinstall seed native
  rows through the private command with no running daemon/Python backend;
  only/skip_types, counts, warnings/errors and database identity are preserved,
  delegation cannot recurse, and missing binaries fail without a second
  publisher. test: `tests/skills/test_native_sync.py::test_explicit_aggregation_delegates_skills_once`.
- 2.6.5 - Unexpected restore-defaults errors retain status/envelope with
  safe text under the exact waiver and no partial rows/events; deliberate
  source-integrity/domain failures retain parity.
  test: `crates/gskills/tests/bundled.rs::restore_internal_errors_are_safe`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo test -p gobby-skills --test bundled`;
`CARGO_BUILD_JOBS=3 cargo test -p gobby-daemon --test skills_startup`;
the native-sync, reference-migration and affected runner tests with the protected
Python prefix; run the generator in check mode without mutating the operator's
registry. First-install tests use temporary state and the isolated test database.

## P3: Native script preparation
`kind: framing`

### 3.1 Owned, atomic script-generation publication [category: code] (depends: 2.6)
`kind: deliverable`

Targets:
- `crates/gskills/src/lib.rs`
- `crates/gskills/src/materialize.rs`
- `crates/gskills/src/script_cache.rs`
- `crates/gdaemon/src/node_commands/skills.rs`
- `crates/gdaemon/src/nodes/skills.rs`
- `crates/gskills/tests/materialize.rs`

**Research context:**
`src/gobby/skills/materialization.py::SkillScriptMaterializer.resolve`,
`_materialize_snapshot`, `_publish_scripts_generation`, `_publish_first_root`
and `_publish_warm_generation` own publication.
`src/gobby/skills/script_cache.py` owns filesystem locks, owner/provenance files,
stages, tombstones and live process identities. Port exact cache namespaces,
generation digests, ordered locking, single-flight coalescing, fsync/rename
publication, safe permissions, provenance and cleanup. Publish from a hub DB
snapshot onto the selected node through 2.1's typed command; return paths on
that node. Never overwrite an unowned directory, remove a live owner's stage,
or publish a partial generation. Cancellation releases owned resources and
preserves the last complete generation. Dependency/browser preparation follows
in 3.2, outside the registry transaction.

**Acceptance:**

- 3.1.1 - Cold/warm publication, changed companions, simultaneous callers,
  ownership checks and returned script paths match Python.
  test: `crates/gskills/tests/materialize.rs::generation_publication_matches_python`.
- 3.1.2 - Crash/cancellation checkpoints, stale stages, live process owners,
  symlinks and tombstones preserve complete generations and unrelated data.
  test: `crates/gskills/tests/materialize.rs::publication_failure_preserves_ownership`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo test -p gobby-skills --test materialize`.

### 3.2 Dependency and browser runtime readiness [category: code] (depends: 3.1)
`kind: deliverable`

Targets:
- `crates/gskills/src/lib.rs`
- `crates/gskills/src/materialize.rs`
- `crates/gskills/src/runtime.rs`
- `crates/gskills/src/process.rs`
- `crates/gskills/tests/runtime.rs`
- `crates/gdaemon/src/node_commands/skills.rs`
- `crates/gdaemon/src/nodes/skills.rs`
- `crates/gdaemon/tests/skills_runtime_cancellation.rs`

**Research context:**
`src/gobby/skills/materialization.py::_runtime_spec`, `_ensure_dependencies`,
`_ensure_browser`, `_run_owned_subprocess` and `_terminate_process_group` own
runtime preparation. `src/gobby/skills/script_cache.py::read_browser_cache_readiness`
defines readiness markers. Preserve Node floor resolution, package/lock hashes,
dependency staging, browser expectation/revision and cache markers, environment
projection, warnings, 180-second command timeout and five-second termination
grace. Commands run as argument arrays on the selected node. Cancellation kills
only the owned process group and waits for it; no detached install continues
after acknowledged cancellation. Wire materialization's preparation futures
to 2.1's connection/request cancellation signal and node-enforced deadline;
the request supervisor retains remote cleanup ownership if the hub tool future
is dropped. On deadline/cancel/disconnect send termination to the owned process
group, wait up to five seconds, escalate to kill if needed, and reap children
before the node terminal/cancel acknowledgement or reconnect. A healthy channel
continues serving pings and unrelated requests during preparation and drain.
Use deterministic fake Node/package/browser fixtures
for unit tests and one admitted isolated runtime smoke. No new package manager
or scheduler is introduced.

**Acceptance:**

- 3.2.1 - Ready/missing/old Node, lock changes, dependency failures and
  browser cache state produce matching readiness, warnings and environment.
  test: `crates/gskills/tests/runtime.rs::runtime_readiness_matches_python`.
- 3.2.2 - Timeout, disconnect and cancellation leave no owned child running,
  no false readiness marker, and a reusable prior complete generation.
  test: `crates/gskills/tests/runtime.rs::runtime_failures_drain_owned_processes`.
- 3.2.3 - With the hub/node channel healthy and a fake npm/browser child
  blocked, cancellation or timeout of the hub caller cancels/drains that child
  before acknowledgement; a following request succeeds. Disconnect/reconnect,
  duplicate cancel and cancellation-delivery failure are separately exercised,
  with deadline enforcement and explicit unconfirmed-cleanup diagnostics.
  test: `crates/gdaemon/tests/skills_runtime_cancellation.rs::hub_cancellation_drains_remote_runtime`.

**Granularity:** More than six production targets remain one runtime-readiness
outcome: the remote adapter consumes 2.1's already implemented request lifetime
and owns process draining, with integrated node/hub proof alongside local tests.

Planned verification: `CARGO_BUILD_JOBS=3 cargo test -p gobby-skills --test runtime`;
`CARGO_BUILD_JOBS=3 cargo test -p gobby-daemon --test skills_runtime_cancellation`.

## P4: Publish the native internal-tool family
`kind: framing`

### 4.1 Skills descriptor, schemas and MCP behavior [category: code] (depends: 3.2, #24883, #24963)
`kind: deliverable`

Targets:
- `crates/gskills/Cargo.toml`
- `Cargo.lock`
- `crates/gskills/src/lib.rs`
- `crates/gskills/src/mcp.rs`
- `crates/gskills/src/mcp_requests.rs`
- `crates/gskills/tests/mcp.rs`
- `docs/contracts/parity-waivers.json`

**Research context:**
`src/gobby/mcp_proxy/tools/skills/__init__.py::create_skills_registry`
registers the 14 public tools from the adjacent modules. Their exact input
schema, description, brief projection and hash come from 0.1's corpus.
Publish one family descriptor using #24883's landed `gobby-core` contract;
request types derive schemas normalized to JSON-object equality with Python.
Handlers invoke the already injected service, preserve explicit tool arguments
and caller-context distinction, and translate errors once. Inventory/schema
reads are read-only; tools with load tracking, database changes, acquisition,
cache publication or process effects are mutation-classified. Listing and
search never load instructions. The shared gateway supplies policy, discovery
leases, context injection, timeouts and result offloading; do not duplicate
those mechanisms. Registering and activating in gdaemon waits for 6.1.
The landed descriptor integration suite is
`crates/gcore/tests/mcp_contract.rs`. As a #24883 consumer, this leaf runs the
entire `--test mcp_contract` target, including the two tests outside the
similarly named module; the `-E 'test(mcp_contract)'` filter is insufficient.

Reuse `gobby_core::mcp::ToolError` for typed validation/missing/forbidden/
conflict/capacity/unavailable/internal projection into the existing MCP
envelope. Unexpected text alone becomes `Internal server error`; preserve
existing codes/status/envelope and safe deliberate errors, logging diagnostics
privately. Add these exact D5 entries to #24963's shared manifest, each bound
to the existing `MCP gobby-skills:<tool>` surface named by its slug:
`skills.mcp-get-skill.internal-error-redaction`,
`skills.mcp-get-skill-file.internal-error-redaction`,
`skills.mcp-get-skill-files.internal-error-redaction`,
`skills.mcp-install-skill.internal-error-redaction`,
`skills.mcp-list-hubs.internal-error-redaction`,
`skills.mcp-list-skills.internal-error-redaction`,
`skills.mcp-materialize-skill-scripts.internal-error-redaction`,
`skills.mcp-move-skill-to-installed.internal-error-redaction`,
`skills.mcp-move-skill-to-project.internal-error-redaction`,
`skills.mcp-remove-skill.internal-error-redaction`,
`skills.mcp-restore-skill.internal-error-redaction`,
`skills.mcp-search-hub.internal-error-redaction`,
`skills.mcp-search-skills.internal-error-redaction`, and
`skills.mcp-update-skill.internal-error-redaction`.
Each has id/surface/behaviour/decision/replacement, decision
`.gobby/plans/gdaemon-skills-family.md#4.1`, and its exact underscore tool
surface from the 14 acceptance items below. No wildcard surface is valid.
Fault vectors assert safe errors and zero partial effects as Waived rather
than masking or skipping the case. All fourteen tools have observed raw
unexpected catches, including search and materialization; this is no claim
that a diagnostic has actually disclosed a secret.

Keep catalog/schema/valid-result parity. The two content tools consume 1.6's
issued handles, trusted reset generation and atomic completion, citing only
`skills.mcp-get-skill.cursor-progression` or
`skills.mcp-get-skill-file.cursor-progression` for that changed behavior.
remove_skill consumes 1.2's parent/companion atomicity under
`skills.mcp-remove-skill.atomic-soft-delete`; update_skill consumes 2.5's
pre-write no-effect/honest-result contract under
`skills.mcp-update-skill.failed-refresh-no-write`. These entries are owned by
those service leaves, not duplicated here. The adapter retains optional-field
presence through Absent/Clear/Set decoding; empty/false values remain supplied
values, with field-specific domain validation. No schema, policy gateway,
waiver loader or exception framework is added in this family adapter.

**Granularity:** Fourteen public-row items, catalog and safe-error items exercise one
descriptor/dispatch adapter. Each behavior's storage/source/runtime state
machine and tests already live in its owning leaf; splitting the adapter by
tool name would create per-file wrappers rather than independent outcomes.

**Acceptance:**

- 4.1.1 - `MCP gobby-skills:get_skill` preserves valid paged/level/scan
  results and asserts 1.6's bound progression and atomic tracking under its
  exact waiver. test: `crates/gskills/tests/mcp.rs::get_skill_matches_python`.
- 4.1.2 - `MCP gobby-skills:get_skill_file` preserves valid reference pages
  and asserts 1.6's bound progression/separate tracking under its exact waiver.
  test: `crates/gskills/tests/mcp.rs::get_skill_file_matches_python`.
- 4.1.3 - `MCP gobby-skills:get_skill_files` matches the bounded file
  manifest contract. test: `crates/gskills/tests/mcp.rs::get_skill_files_matches_python`.
- 4.1.4 - `MCP gobby-skills:install_skill` matches scanned installation.
  test: `crates/gskills/tests/mcp.rs::install_skill_matches_python`.
- 4.1.5 - `MCP gobby-skills:list_hubs` matches hub/auth projection.
  test: `crates/gskills/tests/mcp.rs::list_hubs_matches_python`.
- 4.1.6 - `MCP gobby-skills:list_skills` matches filters and brief/full
  projection. test: `crates/gskills/tests/mcp.rs::list_skills_matches_python`.
- 4.1.7 - `MCP gobby-skills:materialize_skill_scripts` matches machine-local
  paths, readiness and environment. test: `crates/gskills/tests/mcp.rs::materialize_matches_python`.
- 4.1.8 - `MCP gobby-skills:move_skill_to_installed` preserves scope and
  collisions. test: `crates/gskills/tests/mcp.rs::move_installed_matches_python`.
- 4.1.9 - `MCP gobby-skills:move_skill_to_project` preserves target scope
  and validation. test: `crates/gskills/tests/mcp.rs::move_project_matches_python`.
- 4.1.10 - `MCP gobby-skills:remove_skill` preserves successful deletion
  and asserts atomic parent/companion failure with no events under its exact D3 waiver.
  test: `crates/gskills/tests/mcp.rs::remove_matches_python`.
- 4.1.11 - `MCP gobby-skills:restore_skill` preserves restoration/errors.
  test: `crates/gskills/tests/mcp.rs::restore_matches_python`.
- 4.1.12 - `MCP gobby-skills:search_hub` preserves provider search/errors.
  test: `crates/gskills/tests/mcp.rs::search_hub_matches_python`.
- 4.1.13 - `MCP gobby-skills:search_skills` preserves ranking and selection.
  test: `crates/gskills/tests/mcp.rs::search_skills_matches_python`.
- 4.1.14 - `MCP gobby-skills:update_skill` preserves source update outcomes
  and asserts no pre-write restore/timestamp/event/backup or fictitious rollback
  under its exact D4 waiver, including a newer concurrent edit.
  test: `crates/gskills/tests/mcp.rs::update_matches_python`.
- 4.1.15 - Tool inventory, descriptions, schemas, definition hashes,
  classifications and structured failures match the frozen Python contract;
  malformed arguments execute no service call.
  test: `crates/gskills/tests/mcp.rs::catalog_and_validation_match_python`.
- 4.1.16 - Each of the fourteen tools preserves status/code/envelope with
  safe unexpected-error text and no partial effects under its own exact D5
  waiver; intentional domain failures retain ordinary parity.
  test: `crates/gskills/tests/mcp.rs::all_tool_internal_errors_are_safe`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo test -p gobby-skills --test mcp`;
`CARGO_BUILD_JOBS=3 cargo test -p gobby-core --test mcp_contract`.

## P5: Native CLI callbacks
`kind: framing`

### 5.1 Native command tree and registry commands [category: code] (depends: 4.1)
`kind: deliverable`

Targets:
- `crates/gskills/src/lib.rs`
- `crates/gskills/src/cli.rs`
- `crates/gskills/src/cli_registry.rs`
- `crates/gdaemon/src/main.rs::Command`
- `crates/gdaemon/src/main.rs::main`
- `crates/gskills/tests/cli_registry.rs`

**Research context:** `src/gobby/cli/skills.py::skills`, `meta`, `list_skills`,
`show`, `enable`, `disable`, `meta_get`, `meta_set`, and `meta_unset` delegate
to `_skills_local.py` and `_skills_metadata.py`. Existing
`src/gobby/utils/native_bin.py::resolve_native_bin` resolves installed binaries;
5.4 reuses it for the forwarding launcher after all native commands exist.
Extend the existing native Command enum and
main dispatcher; put clap parsing and output formatting in the family crate.
Operator CLI registry reads/meta operations retain their current offline
standalone/hub-authority behavior through the same service and pool; nodes
use authenticated hub transport without a database credential. Read-only
formatting does not mutate loaded-skills state. Preserve flags, help tree,
JSON/text/null distinctions, metadata dotted paths and enable/disable semantics.

**Granularity:** Nine ledger rows share command parsing and registry projection;
all are callbacks over the same service. Source/scaffold and hub configuration
commands are independently delivered in 5.2 and 5.3. The production target count
reflects launcher/native composition rather than extra state owners.

**Acceptance:**

- 5.1.1 - `CLI gobby skills` matches group help and argument dispatch.
  test: `crates/gskills/tests/cli_registry.rs::skills_group_matches_python`.
- 5.1.2 - `CLI gobby skills meta` matches metadata group behavior.
  test: `crates/gskills/tests/cli_registry.rs::meta_group_matches_python`.
- 5.1.3 - `CLI gobby skills list` matches output, scope/filter flags and
  offline authority behavior. test: `crates/gskills/tests/cli_registry.rs::list_matches_python`.
- 5.1.4 - `CLI gobby skills show` matches text/JSON/error output.
  test: `crates/gskills/tests/cli_registry.rs::show_matches_python`.
- 5.1.5 - `CLI gobby skills enable` matches persisted state and errors.
  test: `crates/gskills/tests/cli_registry.rs::enable_matches_python`.
- 5.1.6 - `CLI gobby skills disable` matches persisted state and errors.
  test: `crates/gskills/tests/cli_registry.rs::disable_matches_python`.
- 5.1.7 - `CLI gobby skills meta get` matches dotted lookup and output.
  test: `crates/gskills/tests/cli_registry.rs::meta_get_matches_python`.
- 5.1.8 - `CLI gobby skills meta set` matches JSON/scalar coercion and
  writes. test: `crates/gskills/tests/cli_registry.rs::meta_set_matches_python`.
- 5.1.9 - `CLI gobby skills meta unset` matches missing-key and persistence
  behavior. test: `crates/gskills/tests/cli_registry.rs::meta_unset_matches_python`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo test -p gobby-skills --test cli_registry`.

### 5.2 Native source and authoring CLI commands [category: code] (depends: 5.1)
`kind: deliverable`

Targets:
- `crates/gskills/src/cli.rs`
- `crates/gskills/src/cli_sources.rs`
- `crates/gskills/src/scaffold.rs`
- `crates/gskills/tests/cli_sources.rs`

**Research context:** `src/gobby/cli/_skills_daemon.py::install_skill`,
`remove_skill`, `update_skill` and `_update_all_skills` supply daemon operations.
`src/gobby/cli/_skills_validation.py::validate_skill`,
`src/gobby/cli/_skills_scaffold.py::init_skills` and `create_skill`, and
`src/gobby/cli/_skills_local.py::generate_docs` own local authoring. Native
commands reuse 1.3 validation, 2.1 filesystem effects and 2.5 install/update.
Preserve .gobby/skills scaffold/config layout, existing-file protection,
description/template content, document formats/output destinations and CLI
stdout/stderr/exit codes. Local validate/init/new/doc retain offline operation;
daemon-dependent source operations preserve their connection failure behavior.
The shared scaffold functions serve both direct standalone and selected-node
effects. Do not run a Python module for any callback.
Consume 1.2's `skills.cli-remove.atomic-soft-delete` replacement: a failed
parent/companion deletion is atomic and emits no event. Consume 2.5's
`skills.cli-update.failed-refresh-no-write` replacement: prepare/scan before
writing, recheck the locked source, retain newer concurrent edits, and report
no backup/rollback for pre-write failures. The entries are supplied by those
owners in #24963's shared manifest. Retain existing output/exit behavior outside
those exact Waived assertions; no broad skip or timestamp/event normalization.

**Granularity:** Seven ledger rows are a single CLI adapter over previously
implemented source/local services; no new lifecycle is hidden in callbacks.

**Acceptance:**

- 5.2.1 - `CLI gobby skills install` matches flags, source outcomes and
  failure output. test: `crates/gskills/tests/cli_sources.rs::install_matches_python`.
- 5.2.2 - `CLI gobby skills remove` preserves successful deletion/output
  and asserts atomic parent/companion failure with no event under
  `skills.cli-remove.atomic-soft-delete` from 1.2.
  test: `crates/gskills/tests/cli_sources.rs::remove_matches_python`.
- 5.2.3 - `CLI gobby skills update` preserves one/all behavior and partial
  failure, with no pre-write timestamp/event/backup/restore and honest result
  flags under `skills.cli-update.failed-refresh-no-write` from 2.5; a failed
  refresh preserves a newer concurrent commit.
  test: `crates/gskills/tests/cli_sources.rs::update_matches_python`.
- 5.2.4 - `CLI gobby skills validate` matches validation warnings/errors,
  JSON and exits. test: `crates/gskills/tests/cli_sources.rs::validate_matches_python`.
- 5.2.5 - `CLI gobby skills init` matches idempotent scaffold behavior.
  test: `crates/gskills/tests/cli_sources.rs::init_matches_python`.
- 5.2.6 - `CLI gobby skills new` matches names/descriptions, created files
  and collision protection. test: `crates/gskills/tests/cli_sources.rs::new_matches_python`.
- 5.2.7 - `CLI gobby skills doc` matches formats, content and output-path
  handling. test: `crates/gskills/tests/cli_sources.rs::doc_matches_python`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo test -p gobby-skills --test cli_sources`.

### 5.3 Native hub CLI and active configuration [category: code] (depends: 5.2, #24466)
`kind: deliverable`

Targets:
- `crates/gskills/src/cli.rs`
- `crates/gskills/src/cli_hubs.rs`
- `crates/gskills/tests/cli_hubs.rs`

**Research context:** `src/gobby/cli/_skills_hubs.py::add_hub`,
`_validate_hub_options`, `_build_hub_config`, and `_store_hub_config` validate
provider options and update active config atomically.
`src/gobby/cli/_skills_daemon.py::search_hub` owns search output. Consume
#24466's complete-candidate configuration mutation service for hub additions;
do not write bootstrap YAML, bypass validation, or create a Skills-specific
config table. Reuse the providers from 2.3/2.4 and preserve provider type aliases,
required base URL/repo options, auth-secret references and missing-auth output.

**Acceptance:**

- 5.3.1 - `CLI gobby skills hub` matches group help/errors.
  test: `crates/gskills/tests/cli_hubs.rs::hub_group_matches_python`.
- 5.3.2 - `CLI gobby skills hub list` matches JSON/text and auth status.
  test: `crates/gskills/tests/cli_hubs.rs::hub_list_matches_python`.
- 5.3.3 - `CLI gobby skills hub add` matches validation, atomic config
  persistence and errors. test: `crates/gskills/tests/cli_hubs.rs::hub_add_matches_python`.
- 5.3.4 - `CLI gobby skills search` matches single/all-hub options, results
  and partial failures. test: `crates/gskills/tests/cli_hubs.rs::search_matches_python`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo test -p gobby-skills --test cli_hubs`.

### 5.4 Forward the public CLI and retire Python callbacks [category: code] (depends: 5.3)
`kind: deliverable`

Targets:
- `src/gobby/cli/skills.py::*` — scope-reason: replace the complete command implementation with a literal argument-forwarding Click entry point
- `src/gobby/cli/_skills_daemon.py::*` — operation: delete — scope-reason: retire the entire unused Python Skills daemon callback module
- `src/gobby/cli/_skills_hubs.py::*` — operation: delete — scope-reason: retire the entire unused Python Skills hub callback module
- `src/gobby/cli/_skills_local.py::*` — operation: delete — scope-reason: retire the entire unused Python Skills local callback module
- `src/gobby/cli/_skills_metadata.py::*` — operation: delete — scope-reason: retire the entire unused Python Skills metadata callback module
- `src/gobby/cli/_skills_scaffold.py::*` — operation: delete — scope-reason: retire the entire unused Python Skills scaffold callback module
- `src/gobby/cli/_skills_validation.py::*` — operation: delete — scope-reason: retire the entire unused Python Skills validation callback module
- `tests/cli/test_cli_skills_coverage.py::*` — scope-reason: replace obsolete Python callback mocks with native forwarding and corpus ownership checks
- `tests/cli/test_skills_cli.py::*` — scope-reason: migrate callback-specific expectations and direct helper imports to the frozen corpus and native CLI tests
- `tests/cli/test_skills_native_forwarding.py`

**Research context:** Literal searches for `gobby.cli.skills` and `_skills_`
found dynamic facade access in `_skills_local.py` and `_skills_metadata.py`,
patch targets throughout `tests/cli/test_cli_skills_coverage.py`, and direct
`_store_hub_config` imports plus callback patches in
`tests/cli/test_skills_cli.py`. Retaining those helpers would leave a second
Skills implementation. Once 5.1–5.3 implement every native command, replace the
public entry with a forwarding Click command that disables option interpretation
for forwarded arguments and calls the installed `gdaemon skills` with literal
arguments, cwd, global configuration and inherited streams. Reuse
`src/gobby/utils/native_bin.py::resolve_native_bin`. Preserve missing-binary
diagnostics and the exact native exit status. Rehome business expectations
into 0.1's corpus and the owning native tests before retiring Python-specific
mock tests; preserve launcher integration coverage. Sweep imports/re-exports
and dynamic patch strings before deleting the six unused callback modules.

**Granularity:** Seven production targets implement one ownership switch:
one forwarding launcher replaces the complete callback implementation. Whole-file
deletions are bounded cleanup in that same switch, with the preserved native
behavior already covered by 20 row-specific acceptance items.

**Acceptance:**

- 5.4.1 - The public launcher forwards literal arguments/cwd/config and exit
  status, including unknown options, Unicode, help and errors, to the resolved
  binary; it imports no Skills business/storage/parser module.
  test: `tests/cli/test_skills_native_forwarding.py::test_launcher_only_forwards`.
- 5.4.2 - Every retired callback expectation maps to the frozen corpus and a
  native row test; import/patch-string sweeps find no owned reference to deleted
  helpers. test: `tests/cli/test_skills_native_forwarding.py::test_retired_callbacks_have_native_coverage`.

Planned verification: focused forwarding/migrated CLI tests with the protected
Python prefix, all three native CLI integration files, scoped Ruff/mypy and a
changed-test type audit. The global Python executable naming switch stays with
its existing owner.

## P6: Family activation and native evidence
`kind: framing`

### 6.0 Skills browser admission through the shared verifier [category: code] (depends: 5.4, #24967, #24964)
`kind: deliverable`

Targets:
- `crates/gskills/src/http.rs`
- `crates/gdaemon/src/front_door/skills.rs`
- `crates/gdaemon/src/front_door/mod.rs::*` — scope-reason: register the Skills browser-admission predicate in the existing FrontDoor::handle dispatch boundary, retaining the file-wide scope used by 1.1
- `crates/gdaemon/tests/skills_browser.rs`
- `docs/contracts/skills-native.md`

**Research context:** The Orchestrator's shared-verifier ruling assigns all
cookie parsing, verification, credential precedence, revocation, caller-class
and selector extraction to implementation-only #24967. Consume its landed typed caller/selector
result and family admission seam. This leaf adds only the Skills route
registration and context projection; it adds no verifier, session store,
issuer, cache, general auth policy or new Python code. Node requests relay to
the hub's same verifier without creating node-local browser authority.

The compatibility references are
`src/gobby/servers/auth_service.py::AuthService.authenticate`,
`AuthService._legacy_rejection`, `AuthService._cookie_user`,
`src/gobby/servers/browser_sessions.py::browser_route_allowed`, and
`src/gobby/servers/middleware/auth.py::AuthMiddleware.dispatch`.
The observed authenticate excerpt_hash is
`5446a360254d1560903cc90fbf627e5ca101d65f38d32725d63d8fed11a9b79e`;
the middleware excerpt_hash is
`65ab26e9e827b274251c9a8fff4d58cdd22cdd3cefebf6c0acc267245831f5fb`.
Ordinary `gobby_session` cookies resolve through AuthStore and retain current
read/mutation admission. `browser-testing.` cookies additionally require live
managed capability, loopback peer and URL host, and safe-method admission;
POST/PUT/DELETE Skills mutations are refused with the existing 403 outcome.
Project/session headers, repeated query selectors and JSON body selectors must
match the verified managed session; preserve current body-media and malformed
JSON rejection. Carry the verified project context into Skills handlers rather
than accepting a caller-supplied replacement identity.

Use the existing Skills method/path inventory and its matcher for both
admission and claims, with explicit `Caller::Browser` registration. Match these
nine GET rows only: `/api/skills`, `/api/skills/{skill_id}`,
`/api/skills/stats`, `/api/skills/search`, `/api/skills/{skill_id}/files`,
`/api/skills/{skill_id}/files/{file_path:path}`,
`/api/skills/{skill_id}/export`, `/api/skills/hubs`, and
`/api/skills/hubs/search`. Ordinary-session admission also covers exactly these
eleven mutation rows: `POST /api/skills`, `PUT /api/skills/{skill_id}`,
`DELETE /api/skills/{skill_id}`, `POST /api/skills/{skill_id}/restore`,
`POST /api/skills/{skill_id}/move-to-project`,
`POST /api/skills/{skill_id}/move-to-installed`, `POST /api/skills/scan`,
`POST /api/skills/import`, `POST /api/skills/hubs/install`,
`POST /api/skills/restore-defaults`, and
`PUT /api/skills/{skill_id}/files/{file_path:path}`. Managed callers receive
only the existing permitted reads; a blanket Browser registration must never
turn their denied mutations into native successes. Preserve reserved-path
ordering, encoded file-path matching, and existing API-key caller behavior.
No sibling family, wildcard prefix, MCP/CLI method, WebSocket or unlisted
HEAD handler gains a Skills browser claim. Shared CORS handles OPTIONS
preflight before auth; it creates no Skills operation claim.

`src/gobby/servers/app_factory.py::create_app` installs outermost credentialed
CORS with configured exact/wildcard origins, all configured methods/headers,
and unauthenticated preflight. It also adds CORS headers to auth refusals.
Preserve that observed behavior through #24964's single outer native boundary,
including allowed-origin/method/header/credentials/max-age handling and
headers on auth refusals. The shared verifier is separately owned by #24967;
#24659's planning/retirement root is not an implementation dependency. Add no
family-local CORS/preflight handler. Do not invent
a stricter Origin/CSRF admission rule, a new token, or a new browser credential.
The isolated 0.1 HTTP corpus and the existing middleware fixtures supply the
route-by-route baseline for both cookie classes, including absent/disallowed
Origin, preflight and auth-failure response headers.

Before this leaf and its Security-approved exact SHA ship, every Skills cookie
request stays proxied to Python in all family switch states; document that
cookie remainder even if API-key requests use native handlers. Once admitted,
the shared verifier's denials never fall through to a native handler or a
second verifier. 6.1's full native/default switch is gated on this leaf and its
Security approval. Existing caller lists and other families remain unchanged.

**Expansion obligation:** Immediately after expansion, the Orchestrator applies
`security-review` to the leaf for source section 6.0 and checks the persisted
label before dispatch. Missing label holds dispatch. Cookie activation remains
held until a Security Reviewer records an approved step for the exact
implementation SHA; a later implementation change needs renewed approval.
This is the Orchestrator-confirmed labeling route because server-derived M1
emits coverage labels and accepts no arbitrary-label routing override. Keep M1
server-derived and make no emitter change.

**Acceptance:**

- 6.0.1 - Ordinary AuthStore cookies reproduce Python admission and payload/error
  outcomes on every one of the exact nine GET and eleven mutation rows, through
  the shared #24967 verifier only.
  test: `crates/gdaemon/tests/skills_browser.rs::ordinary_cookie_routes_match_python`.
- 6.0.2 - Managed browser-testing cookies preserve loopback safe-read admission,
  live-capability and project/session/body selector checks; all eleven mutations
  and off-loopback requests are refused before Skills effects.
  test: `crates/gdaemon/tests/skills_browser.rs::managed_cookie_authority_matches_python`.
- 6.0.3 - A caller with neither cookie nor key, invalid/expired cookies and
  unsupported identity headers gain no Skills authority; API-key callers and
  neighboring/unlisted routes retain their existing behavior.
  test: `crates/gdaemon/tests/skills_browser.rs::browser_admission_is_exact_and_fails_closed`.
- 6.0.4 - Both cookie classes retain observed credentialed CORS, preflight and
  refusal-header behavior through #24964's outer boundary, including max-age,
  pre-auth OPTIONS and safe denials, without new Origin/CSRF policy.
  test: `crates/gdaemon/tests/skills_browser.rs::cors_and_cookie_mutation_parity`.
- 6.0.5 - Until the approved leaf ships, cookie traffic remains proxied in every
  switch state with the remainder explicit; after activation, verified browser
  requests use native handlers and denied requests invoke no handler/fallback.
  test: `crates/gdaemon/tests/skills_browser.rs::cookie_takeover_waits_for_shared_verifier`.
- 6.0.6 - The activation record identifies the persisted `security-review` label,
  pre-dispatch check and Security Reviewer's approved step for the exact shipping
  SHA; absent or stale approval holds cookie activation.
  behavior: "Skills browser admission security gate" in `docs/contracts/skills-native.md`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo test -p gobby-daemon --test skills_browser`;
0.1's HTTP-cookie corpus test with the protected Python prefix. Rust integration
tests compose the actual front door, shared verifier and Skills handlers with
isolated session/capability state and a counting Python backend stub. Assert
exact route coverage and zero effects for denied mutations; no production
credentials, daemon or database is used.

### 6.1 Skills-only activation and coherent release [category: code] (depends: 6.0, #24841, #24963)
`kind: deliverable`

Targets:
- `crates/gskills/src/lib.rs`
- `crates/gskills/src/http.rs`
- `crates/gdaemon/src/front_door/skills.rs`
- `crates/gdaemon/src/serve.rs::*` — scope-reason: compose the completed Skills descriptor and backend state from the shared gateway at both cfg-gated serve entry points
- `crates/gdaemon/Cargo.toml`
- `crates/gcode/Cargo.toml`
- `crates/ghook/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::MANAGED_BIN_VERSION_PINS`
- `crates/gdaemon/tests/skills_cutover.rs`
- `tests/e2e/test_skills_native.py`
- `docs/contracts/skills-native.md`

Consumers unchanged:
- `src/gobby/cli/install_setup_gclient.py` — no-edit-reason: dynamically reads its unchanged native version pin.
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

**Research context:** #24841 owns the shared MCP gateway and per-server
strangler; this leaf consumes its landed registration interface and adds only
the `gobby-skills` descriptor. The coordinator confirmed this family owns its
activation. Register `skills` REST and `gobby-skills` MCP backend states using
the existing front_door.routes mechanism. Proxy remains selectable for rollback.
Compare safe read operations only; test mutating HTTP operations and
completed-load/script/source MCP tools against isolated cloned state, never by
duplicating production effects. Clear unwaived mismatches before native becomes
the family default. Consume #24963's shared manifest/readiness result:
each changed behavior requires its exact waiver ID/decision and passing native
response/effect assertions; at least one ordinary Passed case is mandatory.
Zero-Passed, all-Skipped, unwaived Skipped, stale/unknown IDs, missing decisions
and unmatched diffs fail activation. No metadata/timestamp/error mask hides
the D1–D6 replacements. Controlled slow create/update/search work must leave
unrelated requests progressing with bounded cancellation and unchanged semantic
results. This consumes the shared gate and the owning service tests rather
than adding another compare/waiver mechanism. Keep other internal server states unchanged.
The switch records the cookie remainder until 6.0 and the shared #24967 verifier
ship with Security approval for the exact SHA. Earlier native/compare selection
for API-key traffic never claims cookie traffic. Full native activation and the
backend-unavailable smoke require both cookie classes' route-by-route parity,
including managed-cookie mutation refusals; cookie takeover is never inferred
from API-key success alone.

The production closure changes gdaemon and the new family crate, with no gcore
source modification. `src/gobby/install/bin_release_identity.py::workspace_source_identity`
also hashes the root Cargo.toml for every managed binary, so the new workspace
member changes shared build identity. Verified source evidence includes
`113| "Cargo.toml",` in its Git file inventory and
`143| digest.update(hashlib.file_digest(source, "sha256").digest())` in the
per-file hashing loop, excerpt_hash
`4b932e2f05797d06bce8292284ef656bf46639755b3b220f0f3467c4dbb2b8f0`.
The root manifest is included independently of the binary's crate dependency
closure; this justifies the gcode/ghook bumps alongside gdaemon rather than
claiming their crate sources change. At the shipping commit bump each published
binary, currently gdaemon/gcode/ghook, exactly 0.0.1 above its immediately
preceding released version, update manifests/lock/pins, rebuild all affected
binaries including gclient/gmcp/gterm, and promote the coherent trio through
`promote_workspace_binary_set`. Unpublished binaries retain their versions and
use the existing component promotion paths. Verify the current published set
and source-identity closure against release guidance before promotion.
Only the Orchestrator delegates cutover, with a global announcement, quiet
window, successful source/schema/installed-set preflight, and health report.
Read installed hashes after signing/promotion. No push is authorized here.

**Granularity:** The target count and acceptance obligations belong to
one family-specific activation/release state machine. Shared gateway development
remains external; 6.2 separately records the final ledger evidence.

**Acceptance:**

- 6.1.1 - The native gateway serves the complete Skills catalog under its
  existing name with matching schemas, leases, caller context and exactly one
  semantic policy pass. test: `crates/gdaemon/tests/skills_cutover.rs::native_skills_uses_shared_gateway`.
- 6.1.2 - Proxy/compare/native states affect only Skills; compare executes
  every mutation and completed-load effect once, and rollback preserves data.
  test: `crates/gdaemon/tests/skills_cutover.rs::skills_cutover_is_scoped_and_single_effect`.
- 6.1.3 - With the Python backend unavailable, all 20 HTTP, 14 MCP and 20 CLI rows run
  natively; the transitional launcher performs forwarding only. Hub/node and
  standalone return correct machine-local paths and have no Python fallback.
  test: `tests/e2e/test_skills_native.py::test_all_54_rows_work_without_python_backend`.
- 6.1.4 - Native writes are readable after rollback to proxy; source/cache
  cancellation and restart preserve owned data and incomplete-load state.
  test: `tests/e2e/test_skills_native.py::test_restart_and_rollback_preserve_skills_state`.
- 6.1.5 - Shipping published-binary manifests/lock/pins agree at the required
  patch increment.
  test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.
- 6.1.6 - The runbook records exact build/installed hashes, compared cases,
  rollback command, announcement, health, cookie remainder/security approval
  and native smoke evidence without
  claiming the shared compatibility backend removed.
  behavior: "Skills activation evidence" in `docs/contracts/skills-native.md`.
- 6.1.7 - Readiness rejects zero-Passed/all-Skipped/unwaived-Skipped reports
  and unknown/stale/uncited or unasserted waivers; each D1–D6 replacement
  requires its exact shared entry, with ordinary parity still Passed.
  test: `crates/gdaemon/tests/skills_cutover.rs::waiver_readiness_gates_activation`.
- 6.1.8 - Controlled slow create/update/search requests permit unrelated
  native requests to progress; cancellation is bounded and produces no partial
  mutation/index publication or additional effects.
  test: `crates/gdaemon/tests/skills_cutover.rs::slow_skill_work_keeps_front_door_progressing`.

Planned verification: `CARGO_BUILD_JOBS=3 cargo test -p gobby-daemon --test skills_cutover`;
the two isolated e2e tests with the protected Python prefix; scoped Ruff/mypy
and changed-test type audit; targeted version-pin check; admitted release build
and Orchestrator-directed cutover. These are execution gates, not results
observed while drafting.

### 6.2 Publish exact Skills parity ownership [category: docs] (depends: 6.1)
`kind: deliverable`

Targets:
- `docs/contracts/parity-ledger-status.d/memory-skills.json::*` — scope-reason: update only the 54 indexed Skills status records and retain all memory-family records
- `docs/contracts/parity-ledger.md`
- `docs/contracts/skills-native.md`
- `docs/guides/skills.md`

**Research context:** `docs/contracts/parity-ledger.md` is generated by
`scripts/generate_parity_ledger.py`; only Skills keys citing #21568 are owned
here. Its status validation accepts `proxied`, `delegated`, or a recorded
`dropped by decision #N`, not `native`. After 6.1 proves native execution,
set exactly the frozen 54 Skills entries to status `delegated`. Preserve
memory-family rows in the shared status file. The observed generator guard is
`177| status not in {"proxied", "delegated"}` with a separate recorded-drop
alternative. Replace each owned note with its native implementation,
test/evidence reference and #21580 ownership, then regenerate the ledger.
Document native CLI, family/backend controls, paging/load tracking, provider
bounds, hub/node filesystem placement and operational rollback. The global
naming switch and shared MCP compatibility removal remain their existing owners.
The 20 HTTP rows become delegated only after cookie admission and Security
approval are complete; API-key-only native proof leaves their cookie remainder
documented as proxied and cannot certify this final ledger change.
The notes retain D1–D6 behavior-level waiver IDs and decisions from
`docs/contracts/parity-waivers.json`, shared-gate Passed/Waived evidence and
native response/effect assertions. All 54 surfaces remain delegated; there is
no dropped surface, broad exemption, masking rule or Skipped substitute for
these behavior changes. Name #24967 as verifier implementation and #24964 as
outer CORS/preflight owner; retain the cookie remainder until both and 6.0's
exact-SHA Security approval are complete. The #24659 root is not a family
implementation dependency.

**Acceptance:**

- 6.2.1 - Exactly the frozen 54 Skills keys have status `delegated` with concrete
  implementation/evidence and exact behavior-waiver notes, ordinary Passed
  cases and asserted Waived cases; no memory or other-family status changes.
  behavior: "54 delegated Skills parity rows" in `docs/contracts/parity-ledger-status.d/memory-skills.json`.
- 6.2.2 - Regeneration yields a clean second run and the native guide/runbook
  agrees with the verified 54-row behavior and ownership boundaries.
  behavior: "native Skills operations" in `docs/guides/skills.md`.

Planned verification: `uv run python scripts/generate_parity_ledger.py` followed
by the repository's focused ledger check and a key-set/diff assertion against
0.1's frozen inventory. Document actual checks after execution.

## E1 End-to-end verification
`kind: verification`

The acceptance items in 1.1, 1.2, 1.4, 1.5, 1.7, 2.3, 2.4, 2.5 and 2.6 own
all 20 HTTP rows; 4.1 owns all 14 MCP rows; 5.1, 5.2 and 5.3 own all 20 CLI
rows. 0.1 rejects missing or extra keys. 6.1 proves transport integration and
Python-backend independence; 6.2 records the resulting status. Shared target
files are dependency-ordered throughout. No plan section delegates a product
choice to its executor.
6.0 owns browser admission for the same 20 HTTP rows through #24967's one
verifier, preserving both cookie classes and #24964's outer credentialed CORS. The
Orchestrator's persisted security-review label check gates dispatch, and exact-SHA
Security approval gates cookie activation and the final delegated ledger state.
The #24963 shared readiness gate must require ordinary Passed cases and
exactly cited, native-asserted Waived cases; skipped-only/unwaived reports must
fail. D1–D6 waive only their named behaviors and retain their KEEP obligations.
The six changed-behavior contracts, concurrency/fault proofs and finalization
fence are planned execution gates; none is claimed implemented in this revision.

Draft validation command:
`uv run gobby plans validate .gobby/plans/gdaemon-skills-family.md -p /Users/josh/Projects/gobby`.
Execution verification for the #24883 descriptor consumer includes the complete
integration target: `CARGO_BUILD_JOBS=3 cargo test -p gobby-core --test mcp_contract`.
Use the target selector rather than `-E 'test(mcp_contract)'`, which omits two
tests outside the module.
Expansion validation is unrun for the narrative draft and becomes mandatory
after the Adversary derives and applies M1. No implementation tests, production
cutover or task expansion have been performed during planning.

## V1 Plan Changelog
`kind: framing`

2026-10-24 — Initial narrative records the Orchestrator-confirmed 54-row scope,
native CLI placement, shared-MCP ownership boundary and explicit prerequisite
edges. Awaiting validated adversarial review; no consensus is claimed yet.

2026-10-24 — Plan Writer gobby#17576 and Plan Adversary gobby#17577 reached
consensus after re-review of e36a75e9b02bb813891d12da570cc78136bddbc5.
SKILLS-01 is resolved by the connection-owned request supervisor, bounded
node deadline, explicit cancellation and drain-before-acknowledgement contract,
with healthy-channel remote-process cancellation coverage. SKILLS-02 is
resolved by guarded native dev-startup ownership and nonrecursive private
bundled-sync delegation for installation and explicit sync, preserving non-dev
startup and existing selection/override policy. Both findings were accepted;
no disagreement, unresolved decision or remaining finding remains. Fresh
project-aware base validation passed. Manifest derivation and operator approval
remain subsequent gates; no implementation or expansion is authorized here.

2026-10-24 — Plan Writer gobby#17576 and Plan Adversary gobby#17577 renewed
consensus on 4371f5dde6d1ac745e73c021f4ae6f8ff08bc979 after the
Orchestrator's revision request. The delegated ledger status, backend-unavailable
wording, verification separation, scoped pin criterion, naming-switch cleanup,
source-identity evidence and complete MCP-contract test target are resolved.
The new 6.0 admits exactly nine Skills GET and eleven mutation rows through
#24659's one verifier, preserving ordinary and managed cookie classes, selector
and loopback restrictions, current credentialed CORS, and the cookie proxy
remainder. The Orchestrator's persisted security-review label check gates
dispatch; Security approval for the exact SHA gates cookie activation. No
remaining finding, disagreement or unresolved decision remains. Base validation
passed; the superseded M1 was withdrawn whole for fresh derivation. Coordinator
review and Josh approval remain required before expansion, and the planning
task stays claimed/open until that approval.
