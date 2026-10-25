Plan artifact: `.gobby/plans/gdaemon-projects-files-docs-family.md`

# Gobby 1.0 Stage 2: Projects, checkout files, and hub documents

**Plan ID:** gdaemon-projects-files-docs-family

## Overview
`kind: framing`

Port #21585's complete service surface to a statically linked family crate,
`crates/gprojects` (package `gobby-projects`). This plan owns 36 existing
parity-ledger rows: 12 assigned solely to #21585, 19 Projects rows formerly
assigned only to #21568, four attachment rows shared with #21592, and
HTTP POST /api/nodes/checkouts transferred from #24151. It also
owns the once-only hub namespace and attachment-layout cutover.

The result is native project and checkout management, local checkout browsing,
hub USER.md and attachment access, migration, and the corresponding CLI/MCP
operations. Nodes access hub documents through the hub API and never open a
hub datastore or infer that another machine's checkout is local.

## Decision Record
`kind: framing`

The Orchestrator confirmed these dispositions through Triage on 2026-10-24,
recorded under planning task #24951:

- **D1, scope:** all 36 source rows above belong here. Coordinator review
  transfers HTTP POST /api/nodes/checkouts from #24151 to4.5/4.5.5 and6.3's
  ledger ownership update. The two additive routes in2.2/4.1 have separate
  native-extension entries. The eight GET/HEAD API-docs UI
  rows for /docs, /redoc and /openapi.json remain with #21589.
- **D2, wiki:** wiki retirement supersedes the historical /api/hub/wiki/*
  clause. Preserve ordinary 404 responses and retained bytes; recreate no wiki API.
- **D3, command entry:** native family operations are exposed by
  `gdaemon projects` and `gdaemon files`. Existing Click groups become
  argument/exit-code forwarding shims. The current MCP registry delegates
  list_all_projects to Rust. #21571 and #21570 later re-front these surfaces.
  These are transitional shims deleted at the naming switch; this plan adds
  no supervised-backend mode or permanent Python execution requirement.
- **D4, authentication (debt amendment):** #24967 solely owns the shared native
  ordinary-session and confined managed-browser cookie verifier, including its
  Security-reviewed authority mechanism. Projects 2.1 consumes that API and 2.2
  owns explicit verified native dispatch and node confinement. Reuse #24452 for
  managed capabilities. Cookie traffic stays proxied until these dependencies
  land. #24964 solely owns outer CORS/preflight; neither #24659 nor its Python
  retirement gates this family. Sign-in UX stays outside this family.
- **D5, namespaces:** USER.md becomes GET/PUT /api/hub/user; attachment routes
  become /api/hub/chat/attachments, including /limits and /{id}/content.
  /api/files remains the local checkout browser. Old user-md/attachment route
  rows retain decision provenance in the ledger, with explicit replacement rows.
- **D6, bytes:** final attachment locators are
  attachments/<project-id>/<id-prefix>/<attachment-id>/<safe-filename>.
  This plan moves existing chat attachment bytes once and rewrites their locators.
  #21576 alone migrates machine-local comms_attachments and its comms producers
  onto this established destination. #21592 consumes this family's attachment API;
  it does not create another storage implementation.
- **D7, restraint:** the port is required because all 36 source rows are currently
  proxied. Reuse RouteFamily, the async pool/transaction seam, existing native
  node relay, local-token verification, compare reporting, and contract harness.
  Stdlib/locked crates supply filesystem, HTTP, crypto and JSON work. Add no
  external dependency, generic migration framework, new credential, or transport.
The shared admission integrations and cross-runtime purge fence are justified by
current Python/Rust coexistence and trust/data-loss boundaries.

## Debt-first amendment record
`kind: framing`

Josh's ruling in memory 9066f1eb requires this pass before approval. Research
#24960, .gobby/plans/research/projects-files-docs-family-debt.md at
e5063a4e34, supplies the static evidence; proposed race/failure tests have not
run. Retain all 35 original surface owners, the transferred node-checkout row,
and all prior safety/functional obligations.

- D1 DROP pathname check-then-reopen. Sections 3.3/4.2/4.3 retain admitted
  descriptors through I/O; ordinary in-root links remain usable, escaping or
  swapped ancestors are refused. Keep authorized range/cache/SVG contracts.
- D2 DROP destructive partial writes. Section 4.3 preserves predecessor bytes
  before publication and defines complete-new-file/uncertain-durability recovery
  after rename. Keep UTF-8 size/path, mode, parent and .git rules.
- D3 DROP partial SQL/project.json success. Section 4.6 uses one merged
  replacement and a bounded durable recovery record/transaction witness; no
  claim that a SQL rollback alone restores filesystem state.
- D4 DROP treating unreadable/corrupt/non-object metadata as empty. Section
  4.6 refuses without mutation; genuine absence still initializes. Explicit
  repair remains 5.4's backup-and-recover operation.
- D5 DROP line-delimited Git tree membership. Section 4.2 uses NUL records,
  preserves representable names exactly and refuses non-UTF8 names explicitly.
  Keep ordering, ignore policy, Git-unavailable fallback and 4.4's status/diff.
- D6 DROP private OS/SQL exception strings at public boundaries. Preserve
  intentional status/envelope/refusal contracts; only exact cited waivers
  replace the listed accidental messages/failure states.
- D7 KEEP the approved once-only namespace/layout cutover, retained bytes,
  leases, owner relay and wiki 404. Section 3.5 adds its exact shared waivers;
  removed source rows retain dropped-by-decision provenance.

S1 belongs to #24963: docs/contracts/parity-waivers.json has required id,
surface, behaviour, decision and replacement fields. IDs below are stable
projects.<surface-slug>.<behaviour-slug>; surface is an exact ledger key.
The decision field is exactly a task ref #N or a canonical plan citation
.gobby/plans/<plan>.md#<section>; no task-plus-section prose is accepted.
Additive public routes without a Python counterpart use an exact
projects.<surface-slug>.native-extension entry. Their native contract tests
supply readiness; ledger/corpus/compare report native-extension with its
citation and never synthesize Python success evidence. Any other newly added
surface is explicitly non-ledger internal, or receives the same provenance.
Owning implementation leaves add their entries and depend on #24963. Ledger,
corpus/replay and compare consume that one registry. A waiver is a tested
replacement contract, never a mask or Skipped case. Readiness requires at least
one Passed case, case-specific Waived assertions/citations, zero mismatches and
no unwaived Skipped cases. Missing/stale/unreferenced waivers fail closed.

S2 convention: native handlers require explicit Admitted authority. Header
sanitization or unauthenticated continuation is not successful admission. #24967
owns cookies; #24452 owns managed-token verification; Projects 2.2 composes the
shared result with per-route policy and node checks. #24964 is the sole outer
CORS/preflight boundary, including failures; no route-local CORS implementation.

S3 owner: Projects 5.1 is the public gcore/Python shared purge-fence contract.
Every derived writer port adopts it before concurrent native purge; Memory,
Code index and AI/vector producers cannot retain a private native lock. Section
6.3 records adoption/dependency obligations for all downstream producers.

S4 owner: #24970 supplies the one shared native domain-error classification
and HTTP/MCP/CLI adapters for every family. Its seven classes are validation,
missing, forbidden, conflict, capacity, unavailable and internal, each with a
safe message and optional structured detail. Projects maps its domain outcomes
onto that API; it defines no error framework or transport mapping. Intentional
existing envelopes are declared through the shared adapters. Private diagnostics
stay in bounded redacted logs, never anyhow/OS/SQL strings on the wire. Exact
waivers alone change a public message/status/state. Shared corpus fixtures assert
both response and state/recovery. Updates use Absent/Clear/Set(value); null is
valid only under the field's nullable contract, and the merged candidate is
validated under one row lock. #24970 owns its public API and shared contract
documentation; this plan consumes them without choosing another owner crate.

## Constraints
`kind: framing`

- Research baseline: main 0.5.0 checkout at
  3b3480dda67abc46c04977808c3e530b52fd8222. Re-resolve changed symbols and
  migration numbering against the implementation tip before edits.
- A section is one independently verifiable implementation outcome. Its tests
  travel with it. Serial code dependencies also order shared release carriers.
  No leaf in this plan waits on #21571 or #21570, avoiding Stage 2/Stage 3 cycles.
- Every code deliverable below includes its release carriers. Bump each released
  binary affected by its recursive dependency closure by 0.0.1 from the executor's
  starting tip. gprojects/gdaemon-only changes bump gdaemon. gcore changes bump
  gdaemon, gcode and ghook, with gcode's maintenance floor kept equal. Never bump
  unpublished binaries merely because they compile. Update Cargo.lock and managed
  pins in that same commit. Promote through the coherent binary-set contract.
- The running installation changes only under an Orchestrator-directed release
  window, global announcement, and quiet window. Planning performs no live
  migration, restart, cutover, deployment, task expansion, or push.
- Production sources remain below 1,000 lines, including inline Rust tests.
  The current serve.rs has 817 lines; 1.2 extracts family assembly rather than
  growing it toward the ceiling. Other new modules are separated by lifecycle.
- Tests use the isolated PostgreSQL hub, temporary files_home/GOBBY_HOME,
  fake service peers and isolated daemons/ports. No real bootstrap, credentials,
  project bytes, Qdrant/FalkorDB data, or production API is test input.
- Use uv for Python. Focus pytest on named files. Heavy Rust/E2E work needs
  fleet admission and CARGO_BUILD_JOBS=3; no whole pytest suite.
  Use nextest normally. Per the Orchestrator's temporary #24949 ruling, a
  worktree denied .nextest/default uses focused cargo test with serialized
  database tests and records the substitution; never change sandbox settings.
- Initial drafts are narrative only. M1 belongs exclusively to the adversary.
  Base validation is required before review; expansion validation is unrun
  until the adversary applies M1.
- The two additive HTTP routes in2.2/4.1 require native-extension provenance.
  Canonical hub namespace and gdaemon command names replace named Python
  surfaces under their explicit contracts. Rust library capabilities, lock/
  recovery helpers and assembly exports are non-ledger internal surfaces.

## Research evidence
`kind: framing`

Observed navigation used gcode outlines and symbol-at on the route, CLI,
storage, migration and auth modules named in the owning sections. No proposed
implementation test has run. The following complete gcode evidence reads pin
particularly consequential facts:

- `tests/test_wiki_retirement_contract.py::test_retired_http_routes_use_normal_missing_route`,
  lines 33–54, excerpt_hash
  5fb861d97c0d35667138f6563db7838cfb714b401c91cebccc40f25cfddc0715:
  `53| assert not any(path.startswith("/api/wiki") for path in app.openapi()["paths"])`.
- `src/gobby/servers/chat_attachment_files.py::attachment_relative_locator`,
  lines 55–59, excerpt_hash
  183224488bd50f71971d4b34921faaa451adc76a7dd3f994e8ecabac0f7e94df:
  `59| return f"_personal/attachments/{safe_project}/{attachment_id[:2]}/{attachment_id}/{safe_name}"`.
- `src/gobby/servers/routes/files.py::_resolve_safe_path`, lines 90–107,
  excerpt_hash 1a7778b70ef8269220d8b452873a027c04c8a2ead748ce0c51c3dee02c7727d1:
  `105| if not target.is_relative_to(base):` and
  `106| raise HTTPException(403, "Path traversal not allowed")`.
- `src/gobby/servers/routes/projects.py::_build_project_response`,
  lines 224–245, excerpt_hash
  648f03bd058018425f42bfee52aaa0f086e432f156e9a62311ee65a06a685e74:
  `239| if checkout is None or not read_checkout_files:`.
  Remote views must retain this filesystem refusal.
- A gcode evidence graph-usages query for
  `crates/gdaemon/src/front_door/routes.rs::FAMILIES` returned complete_empty.
  Literal sweeps are therefore also required rather than treating that as proof
  of no consumers.

Observed literal sweeps:
`gcode grep -F 'USER_MD_PATH' src/ web/ tests/ -m 60` found files_home_http,
session-start/profile, hub_files_proxy and test_hub_files_proxy.
`gcode grep -F '/api/files/user-md' web/ src/ tests/ -m 80` additionally
found remote_preflight, intro overview/profile, test_remote_preflight_auth and
test_utils_daemon_client. `gcode grep -E 'user-md|api/wiki|api/chat/attachments'
web/src/ -m 65` found MessageItem, chatAttachments and the six test consumers
listed in 3.5. The source-side attachment sweep additionally found
storage/chat_attachments, the Python route and the files-home/AI contract docs.
These consumers are all owned by 3.5. No wiki runtime consumer was found;
the retirement test is retained. Re-run these bounded sweeps at implementation
and include newly landed consumers in the same owning outcome.

## P1: Contract and family assembly
`kind: framing`

### 1.1 Executable parity inventory and isolated corpus [category: test] (depends: #24963, #24970)
`kind: deliverable`

Targets:
- `tests/contracts/projects_files_inventory.json`
- `tests/contracts/projects_files_cases.json`
- `tests/contracts/test_projects_files_corpus.py`
- `docs/contracts/projects-files-hub.md`

**Research context:** The ledger is generated from registered FastAPI/Click/MCP
surfaces and shard status maps. The original 35-row inventory was read from
comms-cron-hub-nodes, git-workspaces, plans-stages-projects and
websocket-chat-voice-ui. Existing route oracles are create_projects_router,
create_files_router, create_hub_files_proxy_router and create_chat_attachments_router.
Existing focused fixtures live in test_projects_routes, test_files,
test_hub_files_proxy, test_servers_routes_chat_attachments and tests/cli.
The corpus is standalone cross-language characterization infrastructure, not
a duplicate TDD leaf.

Record all 36 exact source keys, including the node-checkout row transferred
from #24151 to4.5/4.5.5, their native replacement/unchanged key, owning section
and acceptance ID. Separately inventory the two additive routes in2.2/4.1 and
their exact native-extension entries. Add stable fixtures for status, JSON,
headers, bytes and resulting database/filesystem state; normalize only generated
IDs, times and temporary roots, never errors or security decisions. Capture
the current Python contract before cutover and explicitly mark the planned
namespace/layout deltas. Use inert server services and an isolated hub.
Consume #24963's single waiver loader and schema. D1-D7 replacements are
case-specific assertions with their stable IDs, exact source surfaces and
decision/replacement contracts; never normalize away errors, paths or state.
Use temporary roots and deterministic barriers for ancestor swaps, and injected
publication/commit failures rather than racing live Python against user files.
Common fixtures consume #24970's seven-class API and shared transport adapters,
then verify the declared existing route envelope
and all SQL/file effects. At least one real Passed case is required; exact
Waived cases must prove their replacement, and unwaived Skipped cases fail.
The contract specifies the new namespaces, local-versus-hub routing,
credential rules, publication/deletion states and migration refusal/retry matrix.

**Acceptance:**
- 1.1.1 - The inventory contains exactly 36 scoped source keys with unique acceptance owners, including the transferred node-checkout row, plus separately identified native-extension routes; API-docs UI rows remain excluded. test: `tests/contracts/test_projects_files_corpus.py::test_inventory_is_complete`.
- 1.1.2 - Characterization cases execute the existing Python handlers and record exact response/state outcomes, with security, failure and cancellation cases and no production I/O. test: `tests/contracts/test_projects_files_corpus.py::test_python_cases_match`.
- 1.1.3 - The contract specifies namespace replacements, node-write authority, layout, migration retry states and #21592/#21576 ownership. file: `docs/contracts/projects-files-hub.md`.
- 1.1.4 - Every D1-D7 divergence resolves an exact shared waiver and replacement assertion; missing citations, broad masks, all-skipped reports and unwaived skips fail while ordinary KEEP contracts remain exact. test: `tests/contracts/test_projects_files_corpus.py::test_waivers_are_specific_and_keep_cases_exact`.
- 1.1.5 - Additive route cases require exact native-extension entries and native tests, reject malformed decision citations or missing entries, and produce no invented Python parity result. test: `tests/contracts/test_projects_files_corpus.py::test_native_extensions_have_explicit_provenance`.

**Verification:** planned:
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/contracts/test_projects_files_corpus.py -v`.
Also collect both exact proposed node IDs before execution:
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest --collect-only -q tests/contracts/test_projects_files_corpus.py::test_inventory_is_complete tests/contracts/test_projects_files_corpus.py::test_python_cases_match`.

### 1.2 Statically linked Projects family [category: code] (depends: 1.1, #21558)
`kind: deliverable`

Targets:
- `Cargo.toml`
- `crates/gprojects/Cargo.toml`
- `crates/gprojects/src/lib.rs`
- `crates/gprojects/src/family.rs`
- `crates/gprojects/tests/family.rs`
- `crates/gdaemon/src/front_door/routes.rs::*` — scope-reason: family registration and route assembly share this routing module
- `crates/gdaemon/src/front_door/mod.rs::*` — scope-reason: attach family state and preserve both hub and node dispatch branches
- `crates/gdaemon/src/serve.rs::*` — scope-reason: extract native family construction from the listener assembly
- `crates/gdaemon/src/project_services.rs`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: keep managed release pins coherent with the binaries this deliverable changes

**Research context:** RouteFamily in gcore supplies prefixes/router/claims,
default_backend and check_proxied. gdaemon's RouteTable dispatches Proxy,
Native and Compare. RuntimeFamily is the existing statically linked precedent;
the crate convention is in crates/AGENTS.md. FrontDoor::handle authenticates
API keys before dispatch; the node branch currently relays ordinary HTTP.

Add the family crate and one shared service state built from the existing async
pool and injected filesystem/config roots. Split native family assembly from
crates/gdaemon/src/serve.rs into crates/gdaemon/src/project_services.rs.
Initially default to Proxy and claim only implemented method/path combinations.
Keep unknown methods/paths on the existing fallback; never shadow another family.
The family owns /api/projects, /api/files and the selected hub document paths;
attachment routing uses a narrower explicit subprefix than the future chat family.
Provide an exact method/path/principal inventory used by tests and compare
coverage. NativeUnavailable is a typed 503, never an automatic Python fallback.
No handler executes Python. Consume #24970's public shared error type and
HTTP/MCP/CLI adapters from family assembly onward; service code maps domain
outcomes to the seven classes and declares intentional existing envelopes.
No family error framework or independent transport table is introduced.
Routing: backend code; tdd true.

**Granularity:** assembly touches more than six production targets because the
new crate, composition and mandatory release metadata form one compileable seam;
handler outcomes are split into later leaves.

**Acceptance:**
- 1.2.1 - Proxy/native/compare dispatch, longest-prefix ownership and unknown-method fallback are preserved for the registered Projects family. test: `crates/gprojects/tests/family.rs::dispatch_is_exact`.
- 1.2.2 - Hub/standalone prepare the native service; node mode prepares only local checkout operations and the existing hub relay, with no datastore connection. test: `crates/gprojects/tests/family.rs::mode_assembly_is_scoped`.
- 1.2.3 - Extracted assembly leaves every production file below 1,000 lines and keeps startup behavior unchanged. test: `crates/gprojects/tests/family.rs::composition_preserves_startup`.
- 1.2.4 - Release versions, lockfile and managed pins match the affected binary closure. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

**Verification:** planned: `cargo check -p gobby-projects -p gobby-daemon`,
`CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-projects --test family`.

### 1.3 Shared files_home I/O capability contract
`kind: framing`

Implementation-only owner: #24968, outside the Projects and Cron plan roots.
This section specifies that existing task; it is not a deliverable, deferral
task or M1 entry. Consumers carry heading dependencies on #24968. Its approval
comes with this amended plan; neither family depends on the other's root or
retirement. Reuse the gprojects crate assembly and existing locked filesystem
facilities, with normal gdaemon version/lock/pin release carriers.

At expansion, the coordinator binds existing #24968 as blocked-by the concrete
implementation task generated from Projects section1.2, before admitting the
capability for work. Record that section-to-task mapping in the expansion
handoff and verify the edge; plan approval alone does not satisfy crate assembly.
Before any Projects or Cron #24968 consumer is admitted, verify that producer
edge and the consumer's blocked-by#24968 edge in the expansion graph. If either
is absent, hold capability and consumer admission and route the graph repair
through Triage;6.3's later evidence check cannot substitute for this gate.
Consumer tasks are then blocked by #24968. Create no1.3 task, no second crate
assembly owner, and no dependency on either whole plan root or retirement.

The owned implementation is crates/gprojects/src/files_home.rs, exported by
the crate's lib.rs with focused crates/gprojects/tests/files_home.rs tests.
Python references are paths.open_files_home_descendant,
replace_files_home_descendant, unlink_files_home_descendant and the attachment
descriptor streamer. Expose held-root descendant open; private sibling temp
creation and bounded raw-stream writes; file fsync/atomic replace/directory
fsync; descriptor streaming and exact byte ranges; conditional unlink against
the expected object identity. The caller retains row/run ownership and any
deletion lease/token-CAS; the filesystem capability never invents SQL authority.

Every files_home component is descriptor-relative, no-follow, with device/inode
root/ancestor anchors. Return the admitted open object, never a checked pathname
for later reopening. Private created bytes use0600 and private staging dirs0700.
Pre-publication failure/cancellation removes staging and preserves predecessor
bytes. After rename, complete bounded durability handling before releasing
ownership; return an explicit complete-object/uncertain-durability outcome on
directory-fsync failure, never claim old bytes survived. Unsupported native
Windows returns the existing WSL2/unsupported capability refusal. Use no new
files_home service, API, auth policy, namespace, migration or retirement work.

The #24968 verification contract covers root/symlink/ancestor swaps, exact
full/ranged raw bytes, mode0600, short writes/rename/fsync/cancellation boundaries,
identity-checked unlink, and no partial target or stale-object deletion.
Projects owns USER.md and attachment lifecycle above this capability. Cron
#24952 consumes it for complete raw shell-output streaming/read/unlink under
an isolated cron locator and its section2.2 run-owned retention; no duplicate
files_home layer. Contracts/fixtures are isolated and never access user state.

## P2: Shared native admission
`kind: framing`

### 2.1 Integrate the shared browser authority [category: code] (depends: 1.2, #24967)
`kind: deliverable`

Targets:
- `crates/gprojects/src/family.rs`
- `crates/gdaemon/src/project_services.rs`
- `crates/gdaemon/tests/projects_browser_admission.rs`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: keep managed release pins coherent with this integration

**Research context:** The debt report #24960 S2 distinguishes header sanitization
from admitted native authority. #24967 is the sole implementation owner of both
ordinary AuthStore session cookies and confined browser-testing cookies; its
Security-reviewed API exposes caller class and selectors. It owns verifier
internals, authority publication/revocation and any carrier/schema needed for
Python-loss operation. This family neither chooses a second carrier nor copies
cookie signing, token parsing or membership validation. #24659 and its Python
retirement are not prerequisites of this family.

Inject the #24967 verifier into the existing family service assembly. Preserve
ordinary-cookie per-route permissions and managed-browser loopback,
GET/HEAD/OPTIONS, expiry/revocation and project/session restrictions. Pass the
verified caller class/selectors to 2.2; no request identity header is authority.
On unavailable authority, fail closed with the existing safe unavailable
envelope. Until #24967 and this integration land, cookie traffic stays proxied.
No stricter Origin or CSRF rule is invented. Routing: backend code; tdd true.

**Acceptance:**
- 2.1.1 - Ordinary AuthStore cookies and confined managed-browser cookies enter the family only through #24967's verifier, with matching valid/expired/unknown/revoked outcomes and exposed caller class/selectors. test: `crates/gdaemon/tests/projects_browser_admission.rs::shared_verifier_is_the_only_cookie_authority`.
- 2.1.2 - Python loss preserves valid shared admission and refusal classes; absent authority returns safe unavailable, and family code contains no parallel cookie verifier/carrier. test: `crates/gdaemon/tests/projects_browser_admission.rs::python_loss_never_weakens_cookie_admission`.
- 2.1.3 - Caller-specific route policy remains distinct: ordinary-cookie mutations retain Python permissions while confined browser-testing mutations remain refused. test: `crates/gdaemon/tests/projects_browser_admission.rs::cookie_classes_keep_route_permissions`.

**Verification:** planned:
`CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-daemon --test projects_browser_admission`.

### 2.2 Reusable native admission and confinement [category: code] (depends: 2.1, #24452, #24963, #24964, #24967)
`kind: deliverable`

Targets:
- `crates/gdaemon/src/front_door/auth.rs::*` — scope-reason: share native admission while retaining API-key header sanitization
- `crates/gdaemon/src/front_door/admission.rs`
- `crates/gdaemon/src/nodes/admission_api.rs`
- `crates/gdaemon/src/nodes/mod.rs::*` — scope-reason: export the new admission handler in the existing native node module
- `crates/gdaemon/src/front_door/mod.rs::*` — scope-reason: bind verified admission once before family dispatch
- `crates/gdaemon/tests/project_admission.rs`
- `crates/gprojects/src/family.rs`
- `docs/contracts/parity-waivers.json`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: keep managed release pins coherent with the binaries this deliverable changes

**Research context:** AuthState::authenticate removes caller-supplied identity
headers, resolves API keys and otherwise returns without admission. Python
AuthService.authenticate/_legacy_rejection owns the remaining trust boundary;
browser_route_allowed and browser_selectors_match own confinement. The existing
gcore local_token and loopback modules supply local credential/peer checks.
The native adapter consumes 2.1's injected #24967 authority, not Python RPC.
Native managed capability verification is the runtime plan's #24452 API
(gdaemon-runtime-family section 4.2); reuse its signature, claims and durable
revocation checks rather than writing another token verifier. #24452 is an
external prerequisite, with no Projects back-edge. The current nodes/mod.rs
exports agents/channel/hooks/internal_api/machines_api/registry explicitly;
export the new admission_api module there and register its exact POST path in
FrontDoor::handle after API-key authentication and before generic dispatch.
The endpoint is hub-only; the node listener refuses it rather than relaying an
untrusted internal request. Existing family-local reads on a node still enter
the confined admission path before local I/O.

Expose a reusable verified-admission type to every native family: API-key
identity, local operator/break-glass authority under the existing loopback
rules, or a confined browser identity. Verify managed liveness for browser
sessions from current hub records; apply the existing method/path, Host/peer,
expiry, repeated query/header selectors and JSON-body project/session checks.
A confined browser-testing token cannot upload/delete/write; an ordinary
AuthStore cookie keeps its own Python per-route mutation permissions.
Represent the result explicitly as Admitted(verified caller and selectors),
Unauthenticated continuation, or Rejected. A native handler requires the
Admitted value; AuthState.authenticate's existing Ok(()) header sanitation is
not such a value. Preserve public/proxy continuation separately. Consume #24964
outside dispatch for preflight and CORS on success/refusal/unavailable responses.
Preserve existing managed bearer behavior for ordinary non-grant routes by
using native token verification and current revocation state; never accept an
unverified identity header. Apply body limits before buffering.
Missing/invalid credentials retain 401/403 semantics; unavailable authority
returns typed 503. Before this leaf, cookies are proxied. After it, all owned
principal classes can enter native handlers without Python. No unclaimed route's
auth policy changes. Routing: backend code; tdd true.

Node-local file handlers verify the existing local credential directly. For a
hub-issued key or confined cookie, the node sends a bounded verification request
to a native key-bound /api/nodes/admission endpoint over the existing hub HTTP
client. It carries the credential hash and normalized request selectors, never
file content or a new machine identity. The hub derives the node machine from
its verified key, checks current authority and returns only the scoped admission
result. The node checks that result against its own machine and requested
project before local I/O. Hub unavailability refuses the operation; there is no
cached-authority or anonymous fallback. This is a new public node-key-bound
surface, not a Python parity case. This leaf adds
projects.nodes-admission.native-extension under #24963, exact surface
HTTP POST /api/nodes/admission, decision
.gobby/plans/gdaemon-projects-files-docs-family.md#2.2. Its behaviour states
that it is additive; replacement records the hub-only, current-authority,
scoped-result and fail-closed contract above. Ledger/corpus/compare cite
native-extension provenance and require native contract tests alone, without
inventing a Python response. Domain refusals use #24970's shared adapters.

**Acceptance:**
- 2.2.1 - API keys, local credentials and managed callers receive the same admitted/refused outcomes as Python; forged identity headers and revoked principals are refused. test: `crates/gdaemon/tests/project_admission.rs::credentials_match_python`.
- 2.2.2 - Browser expiry, epoch/revoke, peer/Host, method and every project/session selector restriction survive Python loss; absent authority fails closed. test: `crates/gdaemon/tests/project_admission.rs::browser_confinement_is_native`.
- 2.2.3 - The shared verifier protects user docs, file reads and attachments, and cookie requests stay proxied until the admission dependency is installed. test: `crates/gdaemon/tests/project_admission.rs::family_never_skips_admission`.
- 2.2.4 - Missing, malformed, wrong-class, expired/revoked, forged-selector and Python-loss requests cannot convert unauthenticated continuation into native admission; shared outer preflight/CORS applies to success and errors. test: `crates/gdaemon/tests/project_admission.rs::admission_is_explicit_and_cors_is_outer`.
- 2.2.5 - POST /api/nodes/admission proves its exact native-extension contract, key-bound machine/current-authority scope and unavailable refusal; missing provenance or invented Python evidence fails readiness. test: `crates/gdaemon/tests/project_admission.rs::node_admission_extension_is_scoped`.

**Verification:** planned:
`CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-daemon --test project_admission`.

## P3: Hub documents and once-only attachment relocation
`kind: framing`

### 3.1 Native USER.md on shared files_home [category: code] (depends: 2.2, #24968)
`kind: deliverable`

Targets:
- `crates/gprojects/src/user_document.rs`
- `crates/gprojects/tests/user_document.rs`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: keep managed release pins coherent with the binaries this deliverable changes

**Research context:** paths.require_files_home/open_files_home_descendant/publish_files_home_descendant hold a root fd and no-follow ancestor identities. hub_files_proxy._read_user_md_content enforces 6,291,470 wire bytes and 1,048,576 decoded UTF-8 bytes. Its GET/PUT handlers map missing, remote and unsupported storage distinctly. The native family uses 2.2 admission; a node uses the existing authenticated hub relay.

Consume #24968's held-root descriptor I/O from section1.3; do not reimplement
safe open/replace/unlink/streaming. Preserve its no-follow/identity and explicit
publication outcomes. Return501 with WSL guidance on unsupported native Windows.
Serve canonical GET/PUT /api/hub/user with existing content and error semantics.
Hub-only storage is selected before any open; nodes forward and create no local
profile copy. Legacy paths remain untouched until 3.5's single cutover.
Routing: backend code; tdd true.

**Acceptance:**
- 3.1.1 - GET's source row HTTP GET /api/files/user-md is implemented as native GET /api/hub/user, including empty/missing profiles and remote forwarding. test: `crates/gprojects/tests/user_document.rs::get_matches_python`.
- 3.1.2 - PUT's source row HTTP PUT /api/files/user-md is implemented as native PUT /api/hub/user, with wire/decoded limits, JSON validation and durable content. test: `crates/gprojects/tests/user_document.rs::put_matches_python`.
- 3.1.3 - Symlink/root/ancestor swaps, write cancellation, fsync/rename failure and unsupported platforms cannot escape the held root or falsely report committed content. test: `crates/gprojects/tests/user_document.rs::storage_refusals_preserve_bytes`.

**Verification:** planned: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-projects --test user_document`.

### 3.2 Native attachment limits and publication [category: code] (depends: 3.1, #24968)
`kind: deliverable`

Targets:
- `crates/gprojects/src/attachments/mod.rs`
- `crates/gprojects/src/attachments/upload.rs`
- `crates/gprojects/src/attachments/repository.rs`
- `crates/gprojects/tests/attachment_upload.rs`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: keep managed release pins coherent with the binaries this deliverable changes

**Research context:** create_chat_attachments_router.upload_attachment resolves project identity and delegates to publish_uploaded_attachment. The latter stages bounded chunks, validates MIME/UTF-8/disk space, creates an unpublished row with a token, durably publishes bytes and CAS-marks the row. chat_attachment_lease defines 900-second upload leases. resolve_server_attachment_limits reads live settings, defaulting to 100,000,000 bytes.

Implement the same publication state machine in Rust under /api/hub/chat/attachments. Reuse the existing tables/lease fields and 3.1 descriptor-safe I/O. Validate UUIDs and deleted/sentinel project rules, filename UTF-8 byte limits, MIME declaration/sniff compatibility and streamed size including chunked requests. Snapshot active attachment limits once per request. Native locators use attachments/<project-id>/; there is no user-supplied absolute path. Shield the final publication/CAS outcome and compensate on cancellation or failure; response URLs always use the canonical hub prefix. Expose these operations as the shared API for #21592 and #21576. Routing: backend code; tdd true.

**Acceptance:**
- 3.2.1 - The source row HTTP GET /api/chat/attachments/limits becomes native GET /api/hub/chat/attachments/limits and returns the current configured max_file_bytes with existing fallback rules. test: `crates/gprojects/tests/attachment_upload.rs::limits_match_python`.
- 3.2.2 - The source row HTTP POST /api/chat/attachments becomes native POST /api/hub/chat/attachments with equivalent metadata/limits/MIME failures and canonical content_url. test: `crates/gprojects/tests/attachment_upload.rs::upload_matches_python`.
- 3.2.3 - Injected disk, database, rename/CAS and cancellation failures preserve recoverable rows/bytes; concurrent cleanup cannot publish an unowned or expired claim. test: `crates/gprojects/tests/attachment_upload.rs::publication_is_failure_atomic`.

**Verification:** planned: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-projects --test attachment_upload`.

### 3.3 Native attachment content and lease-safe deletion [category: code] (depends: 3.2, #24963, #24968)
`kind: deliverable`

Targets:
- `crates/gprojects/src/attachments/content.rs`
- `crates/gprojects/src/attachments/delete.rs`
- `crates/gprojects/src/attachments/repository.rs`
- `crates/gprojects/tests/attachment_content_delete.rs`
- `docs/contracts/parity-waivers.json`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: keep managed release pins coherent with the binaries this deliverable changes

**Research context:** get_attachment_content resolves a persisted record and a descriptor-safe locator before FileResponse. delete_attachment claims an unbound row, renews the 60-second lease, unlinks bytes in a shielded worker and token-CAS deletes the row. Bound attachments return conflict. published metadata and claim ownership distinguish readable content from incomplete uploads.

Serve canonical content with byte-range, conditional GET, MIME, safe disposition and security-header parity. Stream from the already validated open descriptor to close the path-reopen race. The authorized record determines project and path; a request cannot select another project's bytes through a forged locator. Port delete claim/renew/release/token-CAS and cancellation outcomes. Missing bytes count as already unlinked; other unlink failures release ownership and retain recoverable metadata. A stale deleter cannot remove a rebound/published or newly claimed row. Routing: backend code; tdd true.

D1's Python oracle opens a safe descriptor, closes it, then reopens a pathname
through FileResponse. Add shared waiver
projects.attachment-content.held-descriptor for exact surface
HTTP GET /api/chat/attachments/{attachment_id}/content, decision
.gobby/plans/gdaemon-projects-files-docs-family.md#3.3. Replacement: retain the
admitted content descriptor through streaming; an ancestor-swap fixture must
serve only the admitted object or refuse with the existing safe403, never open
replacement/foreign bytes. Keep ordinary 200/206/304, range/cache/MIME/SVG and
lease behavior exact. This leaf adds the entry through #24963's schema/loader.

**Acceptance:**
- 3.3.1 - The source row HTTP GET /api/chat/attachments/{attachment_id}/content becomes the native hub content route with 200/206/304, missing-record/bytes and range/header parity. test: `crates/gprojects/tests/attachment_content_delete.rs::content_matches_python`.
- 3.3.2 - The source row HTTP DELETE /api/chat/attachments/{attachment_id} becomes native hub deletion with bound/conflict, missing and ok outcomes. test: `crates/gprojects/tests/attachment_content_delete.rs::delete_matches_python`.
- 3.3.3 - Project/locator substitution, root swaps, concurrent bind/delete, lease expiry and cancellation do not expose or delete unauthorized bytes or rows. test: `crates/gprojects/tests/attachment_content_delete.rs::claims_and_descriptors_are_safe`.
- 3.3.4 - A deterministic check-to-stream ancestor swap proves projects.attachment-content.held-descriptor's replacement and citation, while normal range/cache cases remain Passed rather than waived. test: `crates/gprojects/tests/attachment_content_delete.rs::held_descriptor_waiver_is_narrow`.

**Verification:** planned: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-projects --test attachment_content_delete`.

### 3.4 Native files migration and final attachment layout [category: code] (depends: 3.3, #24963, #24968)
`kind: deliverable`

Targets:
- `crates/gprojects/src/migration.rs`
- `crates/gprojects/src/migration_publish.rs`
- `crates/gprojects/src/cli_files.rs`
- `crates/gprojects/tests/files_migration.rs`
- `docs/contracts/parity-waivers.json`
- `crates/gdaemon/src/main.rs::*` — scope-reason: expose family-owned native files commands through the existing command dispatcher
- `src/gobby/cli/files.py::*` — scope-reason: replace the whole files group with transitional native forwarding
- `tests/cli/test_native_files_forwarding.py`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: keep managed release pins coherent with the binaries this deliverable changes

**Research context:** files_migrate.run_files_migrate performs discovery, prewalk, collision/identity checks, publication, baseline seeding and locator rewrites. _discover_attachments currently targets _personal/attachments, and attachment_relative_locator confirms that live layout. files.migrate is hub-local and refuses singleton conflicts. #21576 owns comms_attachments, which is not a source here.

Port the existing migration classes into a fixed Rust operation, extending the destination once to attachments/<project-id>/. Include the existing legacy personal/project roots and already provisioned _personal/attachments, plus retained wiki bytes without recreating wiki behavior. Preserve existing non-attachment destinations and recognized retry states. Before any mutation, prewalk all sources/destinations, refuse overlaps, divergent collisions, symlinks, special entries and root/ancestor identity changes. Gate family uploads/deletes with the migration's exclusive lock; hold the daemon singleton/offline lock for offline CLI use. Publish using rename or cross-device copy/fsync/hash verification; retire source only after durable destination. Commit locator rewrites only after bytes exist. A crash after publication is reconciled by content equality on rerun; never overwrite divergent data or roll a locator forward to absent bytes. The report retains migrated/skipped/rewritten/unrewritten details and typed partial failure. The existing Click shim forwards to gdaemon files migrate and propagates stdout/stderr/exit status. Routing: backend code; tdd true.

This leaf adds projects.files-migrate.final-layout under #24963 for exact
surface CLI gobby files migrate, decision .gobby/plans/gdaemon-projects-files-docs-family.md#3.4. Replacement: only
chat-attachment destination strings in published/report entries and the matching
persisted locators change from _personal/attachments/... to the final
attachments/<project-id>/<prefix>/<id>/<filename> layout. Report field names,
class IDs, unrelated class destinations, refusal/error/exit semantics and
retry/collision/no-loss behavior remain exact. The case assertion checks both
reported destinations and resulting bytes/locators; it is a cited Waived
replacement, never a namespace mask or Skipped readiness case. Migration's
ordinary report field named skipped retains its own data meaning and is not a
compare-readiness outcome.

**Granularity:** more than six production targets form one offline migration lifecycle plus its necessary entrypoints and release carriers; publication and ordinary attachment I/O are separately owned.

**Acceptance:**
- 3.4.1 - The source row CLI gobby files delegates group/help/usage handling to Rust without Python migration logic. test: `tests/cli/test_native_files_forwarding.py::test_group_delegates_to_rust`.
- 3.4.2 - CLI gobby files migrate preserves report/error/exit semantics except the exact projects.files-migrate.final-layout destination/locator replacement, which proves final bytes and retains all unrelated classes and retry/refusal outcomes. test: `crates/gprojects/tests/files_migration.rs::command_matches_python`.
- 3.4.3 - Preflight collision/special-type/overlap/root-swap refusals leave all sources, destinations and database locators byte-for-byte unchanged. test: `crates/gprojects/tests/files_migration.rs::preflight_has_zero_mutations`.
- 3.4.4 - Same-device, cross-device and crash/rerun states converge to one verified final locator per attachment with no lost or duplicated divergent bytes. test: `crates/gprojects/tests/files_migration.rs::rerun_converges_after_each_boundary`.
- 3.4.5 - Retained wiki bytes remain available as archived bytes, no wiki API appears, and comms_attachments is neither read nor moved. test: `crates/gprojects/tests/files_migration.rs::scope_preserves_retired_and_downstream_data`.
- 3.4.6 - Corpus/replay/compare/ledger consume the same exact migration-layout waiver and reject missing citation, broad report normalization or loss of ordinary class/error/exit coverage. test: `crates/gprojects/tests/files_migration.rs::layout_waiver_is_case_specific`.

**Verification:** planned: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-projects --test files_migration` and isolated pytest for tests/cli/test_native_files_forwarding.py.
Collect the exact proposed Python node ID:
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest --collect-only -q tests/cli/test_native_files_forwarding.py::test_group_delegates_to_rust`.

### 3.5 Atomic namespace cutover and all current consumers [category: code] (depends: 3.4, #24963)
`kind: deliverable`

Targets:
- `crates/gprojects/src/family.rs`
- `src/gobby/files_home_http.py::*` — scope-reason: publish the canonical hub document route constants
- `src/gobby/servers/routes/hub_files_proxy.py::*` — scope-reason: route the entire profile API at its canonical hub namespace
- `src/gobby/servers/routes/chat_attachments.py::*` — scope-reason: change the attachment router prefix and its internal route consumers together
- `src/gobby/storage/chat_attachments.py::*` — scope-reason: canonicalize attachment response URLs across current callers
- `src/gobby/servers/chat_attachment_files.py::*` — scope-reason: align all current attachment locator readers and writers with the migrated layout
- `src/gobby/hooks/event_handlers/_session_start/profile.py::*` — scope-reason: use the canonical hub profile path for reads and writes
- `src/gobby/cli/installers/remote_preflight.py::*` — scope-reason: probe the canonical authenticated profile path
- `web/src/lib/chatAttachments.ts::*` — scope-reason: cut over all upload/limit/delete and content URL helpers
- `web/src/components/chat/MessageItem.tsx::*` — scope-reason: attachment URL validation must accept the canonical hub prefix
- `web/src/components/chat/__tests__/ChatInput.attachments.test.tsx::*` — scope-reason: align attachment request fixtures and assertions
- `web/src/components/chat/__tests__/MessageItem.test.tsx::*` — scope-reason: align rendering fixtures with canonical content URLs
- `web/src/hooks/__tests__/useChat.proxy.test.ts::*` — scope-reason: align attachment response fixtures
- `web/src/hooks/__tests__/useContinuationRestore.test.ts::*` — scope-reason: align restored attachment fixtures
- `web/src/lib/__tests__/chatAttachments.test.ts::*` — scope-reason: assert canonical upload/download/delete paths
- `tests/servers/routes/test_hub_files_proxy.py::*` — scope-reason: exercise canonical profile routing and owner forwarding
- `tests/servers/routes/test_servers_routes_chat_attachments.py::*` — scope-reason: exercise canonical routes and locators
- `tests/cli/test_remote_preflight_auth.py::*` — scope-reason: assert the canonical authenticated probe
- `tests/utils/test_utils_daemon_client.py::*` — scope-reason: assert canonical hop and stream behavior
- `src/gobby/install/shared/skills/gobby/references/intro/overview.md`
- `src/gobby/install/shared/skills/gobby/references/intro/profile.md`
- `docs/architecture/hub-owned-files-home.md`
- `docs/guides/ai-daemon-contract.md`
- `tests/contracts/test_hub_namespace_cutover.py`
- `docs/contracts/parity-waivers.json`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: keep managed release pins coherent with the binaries this deliverable changes

**Research context:** The bounded literal sweeps recorded above locate the profile path constant, remote probe, Python response URL and React consumers. MessageItem validates the old attachment prefix explicitly; changing server routes alone would break rendered attachment links. chat_attachment_files is still called by transitional cleanup/binding code, so all readers/writers must move with the migration.

After migration is proven, change every listed producer/consumer and contract to the canonical hub namespaces/layout in one release. Preserve the existing relay's hop-loop guard and bounded headers/body streaming; node requests reach the owner without local files_home access. Old /api/files/user-md and /api/chat/attachments paths must receive ordinary missing-route responses from the native dispatcher even while Python remains available; they must not fall through to old Python handlers. Do not install compatibility aliases. Keep the retired wiki path missing. Rendered content_url values change at serialization, so old stored conversation metadata is normalized through the canonical serializer rather than moving bytes again. Repeat exact-literal sweeps across src, web, tests and docs and account for any new hits. No visual design changes are involved. Routing: fullstack code; tdd true.

D7 retains this cutover and all byte/lease/relay guarantees. This leaf adds
the following #24963 entries, each citing .gobby/plans/gdaemon-projects-files-docs-family.md#3.5 as its decision:
projects.user-get.hub-namespace for HTTP GET /api/files/user-md;
projects.user-put.hub-namespace for HTTP PUT /api/files/user-md;
projects.attachment-upload.hub-namespace-layout for HTTP POST /api/chat/attachments;
projects.attachment-limits.hub-namespace for HTTP GET /api/chat/attachments/limits;
projects.attachment-content.hub-namespace-layout for
HTTP GET /api/chat/attachments/{attachment_id}/content; and
projects.attachment-delete.hub-namespace-layout for
HTTP DELETE /api/chat/attachments/{attachment_id}.
Each replacement names its exact canonical /api/hub/user or
/api/hub/chat/attachments route, old-route ordinary404, and preserved payload/
status/security semantics. The three layout entries additionally assert the
single attachments/<project-id>/<prefix>/<id>/<filename> locator and retained
bytes with no second migration. Removed keys retain dropped-by-decision#24951
provenance; replacement rows remain delegated. These are explicit Waived cases,
not path masks or Skipped comparisons; unchanged functional cases still Pass.

**Granularity:** the large consumer inventory is one inseparable namespace/layout release: independently shipping a producer or consumer would create broken URLs or a split byte owner. Publication, deletion and migration are already separate leaves; this leaf owns no new state machine.

**Acceptance:**
- 3.5.1 - Every enumerated profile/upload/download/delete consumer uses the canonical hub routes; browser rendered links, restore and proxy tests remain functional. test: `tests/contracts/test_hub_namespace_cutover.py::test_all_consumers_use_hub_namespace`.
- 3.5.2 - Old route paths and retired wiki paths return normal 404, and a node without local copies reads/writes hub USER.md and attachments through the authenticated owner relay. test: `tests/contracts/test_hub_namespace_cutover.py::test_cutover_has_one_owner_and_no_aliases`.
- 3.5.3 - Current Python cleanup/binding consumers and native publication/content/deletion all resolve the same final locator after migration, with no second byte migration. test: `tests/contracts/test_hub_namespace_cutover.py::test_mixed_runtime_consumers_share_layout`.
- 3.5.4 - All six named namespace/layout entries validate against #24963 and prove exact old404/new-contract/retained-byte assertions without waiving other outcomes or adding aliases. test: `tests/contracts/test_hub_namespace_cutover.py::test_cutover_waivers_preserve_functional_contracts`.

**Verification:** planned: isolated pytest for tests/contracts/test_hub_namespace_cutover.py and the listed focused Python tests, plus the listed Vitest files using the web package's existing test command.
Collect the exact proposed Python node IDs:
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest --collect-only -q tests/contracts/test_hub_namespace_cutover.py::test_all_consumers_use_hub_namespace tests/contracts/test_hub_namespace_cutover.py::test_cutover_has_one_owner_and_no_aliases tests/contracts/test_hub_namespace_cutover.py::test_mixed_runtime_consumers_share_layout`.

## P4: Projects and local checkout browsing
`kind: framing`

### 4.1 Native project inventory, detail and cross-project MCP listing [category: code] (depends: 3.5, #24265, #24592, #24963)
`kind: deliverable`

This leaf adds projects.projects-inventory.native-extension under #24963 for
exact surface HTTP GET /api/projects/_inventory, decision
.gobby/plans/gdaemon-projects-files-docs-family.md#4.1. Behaviour states that
the HTTP surface is additive; replacement is the admitted, include_system-
filtered query contract below with the existing MCP envelope and scope.
Native tests supply HTTP readiness with native-extension provenance; Python
parity still covers the existing MCP surface, never a synthesized HTTP oracle.
Domain refusals map onto #24970 and its shared transport adapters.

Targets:
- `crates/gprojects/src/projects/read.rs`
- `crates/gprojects/src/projects/repository.rs`
- `crates/gprojects/src/projects/mod.rs`
- `crates/gprojects/tests/project_reads.rs`
- `src/gobby/mcp_proxy/tools/hub.py::*` — scope-reason: delegate project listing while preserving the remaining hub registry
- `tests/mcp_proxy/tools/test_native_hub_projects.py`
- `docs/contracts/parity-waivers.json`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: keep managed release pins coherent with the binaries this deliverable changes

**Research context:** create_projects_router.list_projects/get_project/get_checkout and files.list_projects use _projects_to_responses, batched stats and checkout joins. _build_project_response suppresses config-file reads on remote machines. hub.list_all_projects/_query_projects supports cross-project inventory with the include_system filter. Hub-mode #24265 established the key-bound node-write path; #24592 owns the current machine-scoped view contract.

Port the shared query/serialization once, including hidden/deleted/sentinel behavior, Personal display name, task/session statistics, null checkout, and machine filtering. A verified foreign-machine read uses checkout rows only and never reads that root on the hub. /api/files/projects remains the local-browser-shaped inventory. Add GET /api/projects/_inventory?include_system=<bool> as the native list service endpoint, registered before the dynamic project-id route for the transitional MCP registry adapter; it preserves existing tool arguments/result/error envelope, with caller/project scope intact. The adapter invokes Rust once and contains no SQL or Python project service logic. Routing: backend code; tdd true.

**Acceptance:**
- 4.1.1 - The source row HTTP GET /api/projects matches listing, machine scope, statistics and hidden/deleted rules natively. test: `crates/gprojects/tests/project_reads.rs::list_matches_python`.
- 4.1.2 - The source row HTTP GET /api/projects/{project_id} matches detail and not-found behavior natively. test: `crates/gprojects/tests/project_reads.rs::detail_matches_python`.
- 4.1.3 - The source row HTTP GET /api/projects/{project_id}/checkouts matches object-or-null local checkout behavior. test: `crates/gprojects/tests/project_reads.rs::checkout_matches_python`.
- 4.1.4 - The source row HTTP GET /api/files/projects matches checkout-shaped browser inventory, including Personal and no repo_path. test: `crates/gprojects/tests/project_reads.rs::file_inventory_matches_python`.
- 4.1.5 - The source row MCP gobby-hub:list_all_projects delegates to the native include_system-filtered cross-project query with its existing envelope. test: `tests/mcp_proxy/tools/test_native_hub_projects.py::test_listing_delegates_to_rust`.
- 4.1.6 - GET /api/projects/_inventory proves admission, include_system filtering, route precedence and exact native-extension provenance without invented Python HTTP evidence. test: `crates/gprojects/tests/project_reads.rs::inventory_extension_has_native_contract`.

**Verification:** planned: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-projects --test project_reads` and isolated pytest for tests/mcp_proxy/tools/test_native_hub_projects.py.
Collect the exact proposed Python node ID:
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest --collect-only -q tests/mcp_proxy/tools/test_native_hub_projects.py::test_listing_delegates_to_rust`.

### 4.2 Native local file tree, text and image reads [category: code] (depends: 4.1, #24963, #24970)
`kind: deliverable`

Targets:
- `crates/gdaemon/src/front_door/mod.rs::*` — scope-reason: dispatch local checkout operations before the node hub relay
- `crates/gdaemon/src/project_services.rs`
- `crates/gprojects/src/checkout_files.rs`
- `crates/gprojects/src/checkout_paths.rs`
- `crates/gprojects/tests/checkout_reads.rs`
- `crates/gprojects/src/errors.rs`
- `crates/gprojects/src/lib.rs`
- `docs/contracts/parity-waivers.json`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: keep managed release pins coherent with the binaries this deliverable changes

**Research context:** files._require_project_checkout_root applies require_local_machine_id/require_root. _resolve_safe_path constrains resolved paths; list_directory uses git ls-files visibility and hides .git. read_file reads max_size+1 with a 1 MiB default/16 MiB bound, UTF-8 replacement and binary/image metadata. serve_image applies SVG sandbox and nosniff headers.

Resolve this daemon's registered checkout only; refuse remote/sentinel/unregistered roots. Port directory visibility/order, metadata/MIME tables, text truncation and image content/range headers. Prevent traversal and symlink escape, and keep a validated descriptor through streaming. A node serves its local checkout browser through its local native family branch instead of relaying a node-local path to the hub. Hub documents remain on the hub relay. Missing/invalid query parameters retain Python's status/body contract. Routing: backend code; tdd true.

On a node, resolve its own checkout row through the native machine-scoped hub
project API using its existing machine-bound key, then validate the local marker
and root before opening it. The request never supplies an absolute root. An
unavailable hub or absent/mismatched checkout fails closed; no new path cache or
node datastore is introduced. Wire this local branch before the existing relay
in FrontDoor::handle and assemble it through project_services.

D1 replaces _resolve_safe_path followed by stat/open with held-root/parent/
content descriptor operations through the entire read/stream. Preserve safe
in-root symlinks with bounded descriptor-relative link resolution (40 hops,
outside-root targets refused); do not silently introduce a blanket link ban.
Deterministic barriers swap an admitted ancestor before enumeration/stat/read/
stream and assert only admitted objects or safe403, never foreign I/O. Add
projects.files-tree.held-descriptor, projects.files-read.held-descriptor and
projects.files-image.held-descriptor for the exact HTTP GET /api/files/tree,
/api/files/read and /api/files/image surfaces, respectively; decisions cite
.gobby/plans/gdaemon-projects-files-docs-family.md#4.2 and replacements state that descriptor/refusal contract.

D5 replaces _get_git_tracked_files' strip/splitlines with git ls-files -z and
git ls-files --others --exclude-standard -z, parsed as raw NUL records. Newline,
tab, quote, backslash and Unicode names retain exact membership. If an entry
cannot be represented as valid UTF-8 JSON, refuse that tree response with
422 {"detail":"Filename is not valid UTF-8"}; never omit or lossily collide
names. Add projects.files-tree.nul-membership and
projects.files-tree.non-utf8-refusal for HTTP GET /api/files/tree, decision
.gobby/plans/gdaemon-projects-files-docs-family.md#4.2. Preserve directories-first/case-insensitive ordering, .git hiding,
ignore policy and the existing no-Git-information fallback.

D6's projects.files-read.safe-io-error entry targets HTTP GET /api/files/read,
decision .gobby/plans/gdaemon-projects-files-docs-family.md#4.2: host read failures keep500 and the detail envelope but
return exactly {"detail":"Failed to read file"}, without interpolated paths or
OS text. errors.rs contains only thin domain-to-#24970 mapping functions;
reuse its seven-class type and shared HTTP/MCP/CLI adapters, declaring the
intentional existing route envelope through that API. Define no family error
class framework, transport table or parallel adapter. Preserve useful traversal/
missing/bounds refusals and keep private causes in bounded redacted logs.
Later mutation/Git operations use the same shared owner. All waivers use #24963.

**Granularity:** root admission, local-node assembly, descriptor reads and their
shared safe-error adapter form this checkout-read capability; additional module
exports and release/waiver carriers are required integration targets. Tree,
text and image assertions remain individually identified below.

**Acceptance:**
- 4.2.1 - HTTP GET /api/files/tree keeps ordering/ignore/fallback contracts and proves exact NUL filename membership plus the cited non-UTF8 refusal waivers for newline/tab/quote/backslash/Unicode and invalid-byte cases. test: `crates/gprojects/tests/checkout_reads.rs::tree_matches_python`.
- 4.2.2 - The source row HTTP GET /api/files/read matches bounds, UTF-8 replacement, binary/image flags, MIME and truncation metadata. test: `crates/gprojects/tests/checkout_reads.rs::read_matches_python`.
- 4.2.3 - The source row HTTP GET /api/files/image matches content/range/cache behavior and SVG security headers. test: `crates/gprojects/tests/checkout_reads.rs::image_matches_python`.
- 4.2.4 - Traversal, deterministic ancestor swaps and cross-machine substitutions cannot cause foreign I/O; all three held-descriptor waivers are proved while ordinary in-root links still work and nodes open no datastore. test: `crates/gprojects/tests/checkout_reads.rs::locality_and_paths_are_enforced`.
- 4.2.5 - Only projects.files-read.safe-io-error changes the listed host-error detail; useful public refusals and every normal read/image/header case remain exact, and no private diagnostic leaks. test: `crates/gprojects/tests/checkout_reads.rs::safe_error_waiver_keeps_public_refusals`.
- 4.2.6 - All seven domain-error classes and declared existing envelopes use #24970's shared type and HTTP/MCP/CLI adapters; no transport exposes OS/SQL/driver details or uses a family-local error framework. test: `crates/gprojects/tests/checkout_reads.rs::domain_errors_use_shared_adapters`.

**Verification:** planned: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-projects --test checkout_reads`.

### 4.3 Native checkout file writes [category: code] (depends: 4.2, #24963)
`kind: deliverable`

Targets:
- `crates/gprojects/src/checkout_write.rs`
- `crates/gprojects/src/checkout_paths.rs`
- `crates/gprojects/tests/checkout_write.rs`
- `docs/contracts/parity-waivers.json`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: keep managed release pins coherent with the binaries this deliverable changes

**Research context:** files.write_file uses the validated local checkout, refuses .git, requires an existing parent, writes UTF-8 and returns success/size/original path. It must remain a local operation even when this daemon is a node; 4.2 establishes the node-local family branch.

Port the write response and validation/error contract, including Unicode byte size, missing parent and .git component refusal. Use the held validated parent descriptor and a durable replace to avoid traversal races and partial successful writes. Retain file mode and existing permissions; do not create parent trees or redirect to hub document storage. Before publication, failures preserve old bytes. Routing: backend code; tdd true.

This leaf adds three entries for exact surface HTTP POST /api/files/write,
each with decision .gobby/plans/gdaemon-projects-files-docs-family.md#4.3. projects.files-write.held-descriptor (D1)
retains the admitted parent through staging/rename, including the same safe
in-root link policy as 4.2. A safe link write updates the admitted target while
preserving the link entry; lexical/resolved .git guards both apply.
projects.files-write.atomic-publication (D2)
replaces truncating write_text: write all UTF-8 bytes to a private sibling,
preserve mode, fsync it, then rename through the held parent and fsync the
directory. Short write, staging fsync or rename failure preserves predecessor
bytes and removes the temporary file. Rename is the visibility commit point.
If directory fsync fails after rename, keep the complete replacement and return
503 {"detail":"File durability could not be confirmed"}; no false promise of
old bytes. A crash can recover either complete version. A subsequent request
re-reads/revalidates the actual current version; no blind compensation or
automatic duplicate write occurs. Cancellation before rename cleans staging;
after rename, shield bounded completion/fsync and report the actual outcome.
projects.files-write.safe-io-error (D6) maps pre-publication host failures to
500 {"detail":"Failed to write file"}; useful public refusals remain unchanged.
Reuse the held-root publication order from section1.3 with the admitted checkout
root/parent operations and 4.2's safe-error convention; do
not create a second writer framework. Waived fixtures prove each exact new
state/status, while ordinary success/size/path cases remain Passed.

**Acceptance:**
- 4.3.1 - The source row HTTP POST /api/files/write writes only the admitted local checkout and returns the Python success/size/path shape. test: `crates/gprojects/tests/checkout_write.rs::write_matches_python`.
- 4.3.2 - .git, traversal, swapped parents and foreign roots cause no unauthorized I/O; short-write/staging-fsync/rename failures preserve predecessor bytes under the exact D1/D2/D6 waivers. test: `crates/gprojects/tests/checkout_write.rs::refusals_and_failures_preserve_bytes`.
- 4.3.3 - Post-rename directory-fsync failure and cancellation yield a defined complete-version outcome, safe503 when durability is unknown, no partial content, and correct subsequent revalidation. test: `crates/gprojects/tests/checkout_write.rs::publication_commit_point_is_explicit`.

**Verification:** planned: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-projects --test checkout_write`.

### 4.4 Native local Git status and diff [category: code] (depends: 4.3, #24963)
`kind: deliverable`

Targets:
- `crates/gprojects/src/checkout_git.rs`
- `crates/gprojects/tests/checkout_git.rs`
- `docs/contracts/parity-waivers.json`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: keep managed release pins coherent with the binaries this deliverable changes

**Research context:** files.git_status concurrently gets branch and NUL-delimited porcelain status and maps any unavailable result to 503. git_diff validates the local relative path, invokes git with --literal-pathspecs, HEAD and --, and returns diff/path. daemon_git and parse_porcelain_v1_z are the Python oracle; no new Git service is needed.

Use an argv-only bounded subprocess through Tokio, explicit checkout cwd and timeouts, preserving arbitrary filename bytes as the existing JSON contract permits. Parse porcelain rename/copy/NUL entries faithfully. Kill/reap timed-out children; never invoke a shell or accept option/pathspec injection. Keep both endpoints local on nodes. Routing: backend code; tdd true.

Keep the existing NUL-aware status parser and literal diff contract. D6 adds
projects.files-git-status.safe-spawn-error for HTTP GET /api/files/git-status
and projects.files-git-diff.safe-spawn-error for HTTP GET /api/files/git-diff,
both citing .gobby/plans/gdaemon-projects-files-docs-family.md#4.4. Only host spawn failures use the replacement
503 {"detail":"Git unavailable"}; retain every intentional public validation,
missing-HEAD and non-host command-result envelope. Reuse 4.2's typed service
errors and route adapters; no raw exception/path/argv secrets enter responses
or compare logs. The entries belong to #24963's shared registry.

**Acceptance:**
- 4.4.1 - The source row HTTP GET /api/files/git-status matches branch/status mapping, rename/untracked cases and unavailable responses. test: `crates/gprojects/tests/checkout_git.rs::status_matches_python`.
- 4.4.2 - The source row HTTP GET /api/files/git-diff matches literal file diffs, path validation, missing HEAD and subprocess failure responses. test: `crates/gprojects/tests/checkout_git.rs::diff_matches_python`.
- 4.4.3 - Hostile filenames/arguments and timeout/cancellation cannot execute a shell, escape the checkout or leave a child running. test: `crates/gprojects/tests/checkout_git.rs::subprocess_boundaries_are_safe`.
- 4.4.4 - Both exact spawn-error waivers return the safe503 body with no host details, while NUL status, literal diff, missing HEAD and useful refusal cases stay unwaived. test: `crates/gprojects/tests/checkout_git.rs::safe_spawn_errors_preserve_git_contracts`.

**Verification:** planned: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-projects --test checkout_git`.

### 4.5 Native project initialization and checkout mutation [category: code] (depends: 4.4, #24265)
`kind: deliverable`

Targets:
- `crates/gprojects/src/projects/initialize.rs`
- `crates/gprojects/src/projects/checkouts.rs`
- `crates/gprojects/src/projects/repository.rs`
- `crates/gprojects/tests/project_checkouts.rs`
- `crates/gdaemon/src/nodes/checkouts_api.rs`
- `crates/gdaemon/src/nodes/mod.rs::*` — scope-reason: export the native checkout handler after the admission export owned by 2.2
- `crates/gdaemon/src/front_door/mod.rs::*` — scope-reason: register the key-bound checkout route in the shared front-door dispatch module after 2.2
- `crates/gdaemon/tests/node_project_checkouts.rs`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: keep managed release pins coherent with the binaries this deliverable changes

**Research context:** projects.init_project calls initialize_project and distinguishes an existing checkout before broadcasting checkout_registered. register_checkout validates root/marker and returns 201 on creation or 200 on same-root retry. rebind_checkout preserves soft deletion, validates machine/root/marker and broadcasts checkout_rebound. LocalProjectCheckoutManager has unique-root, sentinel and overlay guards and clears index state on rebind. nodes._register_checkout is the existing key-bound foreign-machine path.

The exact foreign-machine oracle is
src/gobby/servers/routes/nodes.py::_register_checkout, called by
create_nodes_router's checkout POST. FrontDoor::handle currently relays ordinary
node HTTP and only recognizes selected hub node handlers; nodes/mod.rs requires
explicit exports. Add checkouts_api as a hub-only native POST /api/nodes/checkouts
handler backed by the same gprojects checkout service, export it from nodes/mod.rs,
and dispatch it in FrontDoor::handle after verified API-key authentication and
before the generic family/proxy fallback. 2.2 precedes this section transitively
through 4.4, ordering both shared export/dispatch targets. The existing node
caller continues to use this key-bound hub endpoint; the hub derives machine_id
from that key, and validates marker claims without opening the node's filesystem.
The node listener rejects this hub-only registration endpoint locally.

Coordinator ownership transfer: HTTP POST /api/nodes/checkouts moves from
#24151 to this Projects outcome, with acceptance4.5.5. #24151's task description
already records its release of the row. This existing Python surface belongs
in the 36-source inventory and retains real parity evidence; it is not an
additive native extension. Section6.3 reassigns its ledger owner and records
this plan's implementation/test provenance, removing the former owner.

**Granularity:** the shared checkout transaction and its public and node-key
entries are one atomic registration outcome. The additional export, dispatch,
test and release targets close that route's complete call path; they do not
introduce another checkout owner or an independently closable behavior.

Port initialization's marker/config seeding and checkout registry transaction, including identity/name attach safeguards and isolated-worktree rejection. Reuse gcore project marker readers. Local mutations validate normalized absolute root, on-disk marker and this daemon's machine; a node registers only through the existing authenticated /api/nodes/checkouts semantics. Port that handler to the shared native checkout service without accepting body-supplied machine identity or checking node paths on the hub. Rebind clears machine/project index state and retains deletion status. Emit native WebSocket project events once after commit, never on rolled-back or same-root retries. Routing: backend code; tdd true.

**Acceptance:**
- 4.5.1 - The source row HTTP POST /api/projects/init initializes/reattaches with Python's identity/marker/checkout behavior and event rule. test: `crates/gprojects/tests/project_checkouts.rs::init_matches_python`.
- 4.5.2 - The source row HTTP POST /api/projects/{project_id}/checkouts gives 201 insert/200 retry and matching conflicts without restoring a deleted project. test: `crates/gprojects/tests/project_checkouts.rs::register_matches_python`.
- 4.5.3 - The source row HTTP POST /api/projects/{project_id}/checkouts/{machine_id}/rebind preserves machine scope, validation, index invalidation and event semantics. test: `crates/gprojects/tests/project_checkouts.rs::rebind_matches_python`.
- 4.5.4 - Node-key authority writes only its own machine rows, and marker/root/overlay conflicts or transaction failure leave no partial checkout or event. test: `crates/gprojects/tests/project_checkouts.rs::machine_authority_and_atomicity_hold`.
- 4.5.5 - The transferred source row HTTP POST /api/nodes/checkouts has this unique Projects owner, works with Python unavailable, derives machine identity from the key, refuses body/header machine forgery, and cannot be served as local node authority. test: `crates/gdaemon/tests/node_project_checkouts.rs::registration_is_native_and_key_bound`.

**Verification:** planned: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-projects --test project_checkouts` and `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-daemon --test node_project_checkouts`.

### 4.5a Shared project metadata serialization and policy guards [category: code] (depends: 4.5, #24968)
`kind: deliverable`

Targets:
- `src/gobby/config/project_metadata.py`
- `src/gobby/config/project_validation.py`
- `src/gobby/utils/durable_file.py::*` — scope-reason: expose bounded locking of a securely opened descriptor while preserving the path wrapper
- `src/gobby/config/validation_detection.py::*` — scope-reason: extract project-file policy helpers and retain their import facade
- `src/gobby/config/tasks.py::load_project_task_close_policy`
- `src/gobby/utils/project_context.py::*` — scope-reason: guard policy snapshots and both sides of isolation metadata copy and repair
- `src/gobby/utils/project_init.py::*` — scope-reason: serialize marker read/merge/publication and expected-identity cleanup
- `src/gobby/servers/tool_approvals.py::*` — scope-reason: guard approval load/save/clear/migrate and the async wrapper
- `src/gobby/servers/chat_session_permissions.py::*` — scope-reason: map unavailable policy to the existing approval-required decision
- `src/gobby/servers/websocket/chat/backends/codex.py::*` — scope-reason: refuse provider permission when async project-policy loading fails
- `src/gobby/servers/websocket/chat/backends/droid_permissions.py::*` — scope-reason: refuse provider permission when async project-policy loading fails
- `src/gobby/servers/routes/mcp/hook_hold_open.py::*` — scope-reason: deny autoapproval when guarded policy is unavailable
- `src/gobby/servers/routes/projects.py::*` — scope-reason: propagate recovery refusal through existing route envelopes
- `src/gobby/cli/extensions.py::*` — scope-reason: guard hook status and serialize enable/disable field merges
- `src/gobby/project_verification/evidence.py::*` — scope-reason: collect verification policy from a guarded snapshot
- `src/gobby/project_verification/refresh.py::*` — scope-reason: guard the legacy field writer until native replacement
- `src/gobby/storage/projects.py::ensure_personal_project_identity`
- `src/gobby/mcp_proxy/tools/merge_verification.py::*` — scope-reason: refuse merge verification on unavailable project policy
- `src/gobby/tasks/transcript_outcomes.py::*` — scope-reason: refuse policy-dependent evidence credit on unavailable metadata
- `src/gobby/tasks/step_records.py::*` — scope-reason: propagate unavailable task-close policy through close refusal
- `src/gobby/hooks/verification_runner.py::*` — scope-reason: fail verification when hooks or verification policy is unavailable
- `src/gobby/cli/release.py::*` — scope-reason: stop release verification when guarded project commands are unavailable
- `src/gobby/agents/spawn_cache_policy.py::*` — scope-reason: prevent sandbox-policy fallback after recovery refusal
- `src/gobby/agents/spawn_executor_providers.py::*` — scope-reason: reject launch preparation when source or main-checkout sandbox policy is unavailable
- `src/gobby/workflows/found_work_gate.py::*` — scope-reason: extract project policy resolution and preserve a blocking gate on recovery refusal
- `src/gobby/workflows/found_work_project_config.py`
- `crates/gcore/assets/config/runtime_config_contract.json::*` — scope-reason: regenerate the required config carrier without adding a runtime option
- `crates/gprojects/src/projects/metadata_lock.rs`
- `crates/gprojects/src/projects/mod.rs`
- `crates/gprojects/Cargo.toml`
- `crates/gprojects/tests/project_metadata_barrier.rs`
- `tests/contracts/test_project_metadata_barrier.py`
- `tests/contracts/project_metadata_probe.py`
- `tests/config/test_validation_detection.py::*` — scope-reason: preserve helper imports and ordinary overrides after extraction
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: keep managed releases coherent with the native guard

**Research context:** The PA-D01 sweep found policy reads in
tool_approvals.load_project_approval_rules/_load_project_payload,
validation_detection.load_project_validation_detection/_load_project_payload,
tasks.load_project_task_close_policy, project_context.get_project_context/
_build_and_set_project_context, verification evidence, and
found_work_gate._project_verification_commands. Consumers include chat approval,
asynchronous Codex/Droid permission callbacks, hook autoapproval, merge
verification, transcript evidence and close/stop gates. Shared project-context
policy also feeds verification_runner, release and spawn sandbox preparation.
Writers include approval save/clear/migrate, validation save/clear, project_init
marker/field writers, project_context isolation copy/repair, hooks_enable/
hooks_disable, ensure_personal_project_identity and refresh._write_verification.
Identity-only readers such as gcore project::read_project_id do not consume the
fields changed by4.6; their existing identity contract stays unchanged. Evidence
inventory: `gcode grep -F 'project.json' src/gobby -m 100` and its continuation,
plus identifier sweeps for approval, validation and project-context helpers.
Recheck this bounded inventory at implementation tip before4.6 activation.
The approval-loader citation is tool_approvals.py lines281–284,
`281| data = json.loads(project_file.read_text(encoding="utf-8"))` followed by
`284| return []`, excerpt_hash
028db786a35a12df762d701ebe853f93f0ccd7daeaab2dc8e2ae5a147aabbd3d,
from `gcode evidence` on load_project_approval_rules. The recovery error must
never enter that ordinary fallback branch.

Atomic rename can expose a candidate before SQL commit. Reuse
durable_file.exclusive_file_lock and its bounded platform locking, exposing a
descriptor-taking form with the path wrapper delegating. Python project_metadata
and native metadata_lock implement one concrete protocol: securely open
`.gobby/.project.json.lock` relative to the admitted held metadata directory,
without following symlinks; validate a regular owner-only0600 sidecar and keep
its inode stable. Never unlink/replace it. Use identical BSD flock exclusive
locks on supported POSIX runtimes; native code reuses the locked libc dependency.
No new package, runtime option or general transaction framework is needed.
Acquire within the remaining operation deadline capped at two seconds; release
on cancellation/process exit. Blocking filesystem work stays off async loops.

Hold the lock across actual open/read/parse or the complete read/merge/durable
publication. Check `.gobby/.project-update-recovery/` under that lock, never
before it. Pending evidence, an invalid lock/journal object or lock deadline
raises a distinct recovery-required error. No caller converts it to missing
metadata, empty rules, absent verification or global/default policy. The recovery
directory is0700 and records0600. This leaf recognizes evidence and refuses;
4.6 alone interprets journals and resolves witnesses. Lock order is metadata
flock then project row lock; never wait for flock while holding a row lock.
File-only Python writers merge owned fields under the same exclusive lock,
preserve unrelated keys and refuse pending recovery without a SQL transaction.
In particular, save_project_approval_rules and save_project_validation_detection
use a strict read-for-merge path: malformed JSON, a non-object JSON root or an
unreadable existing file refuses before staging or publication. They never
substitute an empty payload and overwrite the corrupt file. Approval/validation
clear and migration helpers use the same strict path. Return a safe invalid-
metadata or unavailable-metadata failure through the existing boundary; preserve
original bytes and permissions, with no journal or SQL mutation. The explicitly
specified identity/verification repair operations retain their own guarded
repair contracts; a policy-field save cannot activate them.
Missing metadata keeps each operation's existing initialization contract;
ordinary no-ops create no metadata file. Pass an already-held guard explicitly
through nested helpers to avoid recursive flock acquisition.

Guard get_project_context and _build_and_set_project_context before constructing
policy-bearing context. Completed request snapshots contain committed bytes;
new requests cannot populate or reuse a recovery-failed snapshot.
get_verification_config/get_hooks_config/get_project_sandbox_config preserve
typed outputs and propagate the distinct refusal. Isolation copies release a
guarded source snapshot before taking the destination lock; destination writes
recheck their own state. Reversed copies cannot deadlock or copy an uncommitted
candidate. Personal identity repair and project_init expected-id cleanup refuse
pending recovery instead of repairing or deleting its candidate.

At policy boundaries, normal chat approval requires approval. Hook hold-open
returns its existing deny envelope with safe reason project_metadata_unavailable,
never autoapproving through session/global rules. Intentional bypass/plan modes
remain unchanged. Merge verification returns its tool-error envelope; transcript
attribution records no successful evidence; task close and stop remain blocked
with a safe metadata-unavailable diagnostic. HTTP config materialization returns
503 {"detail":"Project update recovery required"}. Existing command errors,
exit conventions and ordinary valid/null/empty behavior remain exact. There is
no second recovery engine or exception-swallowing fallback.

Codex and Droid permission callbacks also return their existing safe refusal
on the async loader's recovery error; they cannot bypass the shared gate through
provider-specific allowlists. Verification runner/release fail instead of
reporting no configured hooks or successful verification. Spawn cache and
provider preparation propagate the unavailable-policy error before launch,
without falling back to a weaker sandbox/default or reusing a failed snapshot.

Split src/gobby/config/validation_detection.py by moving its project-file
load/save/clear/resolve functions and private payload reader to new
src/gobby/config/project_validation.py. Keep models/defaults in the original
module and re-export current helper names through a cycle-free facade; the
original finishes below850 lines. Split src/gobby/workflows/found_work_gate.py
by moving resolve_stop_validation_config, _project_verification_commands,
_project_command_prefix and their private category map to new
src/gobby/workflows/found_work_project_config.py; retain the public resolver
import and finish the original below850. All production files stay below1,000.

**Granularity:** these integrations form one metadata-visibility outcome: any
unguarded policy consumer or cooperating writer breaks it. The extractions are
required size boundaries. Journal interpretation remains the independently
testable4.6 lifecycle. Routing: backend code; tdd true.

Consumers unchanged:
- `tests/servers/routes/test_projects_routes.py` — no-edit-reason: preserved helper imports and ordinary routes remain compatible; recovery coverage is in the barrier suite.
- `tests/servers/test_machine_scoped_projects.py` — no-edit-reason: loader monkeypatch names and checkout scope remain unchanged.
- `src/gobby/cli/install.py` — no-edit-reason: invokes the same identity helper; the helper refuses before any candidate repair, and this caller consumes no metadata policy.
- `src/gobby/cli/install_files_home.py` — no-edit-reason: invokes the same guarded identity helper without interpreting recovery evidence or policy fields.
- `src/gobby/files_migrate.py` — no-edit-reason: the existing identity-helper call stays serialized inside that helper; migration owns no journal interpretation or policy decision.
- `tests/cli/test_install_coverage.py` — no-edit-reason: ordinary identity initialization behavior and helper signature remain unchanged.
- `tests/cli/test_install_prompts.py` — no-edit-reason: ordinary identity initialization behavior and helper signature remain unchanged.
- `tests/cli/test_cli_install.py` — no-edit-reason: the identity-helper mock and ordinary install contract retain their signature and behavior.
- `tests/cli/test_install_front_door.py` — no-edit-reason: identity-helper monkeypatches remain valid; recovery refusal is tested by the dedicated barrier suite.
- `tests/storage/test_personal_remote_init.py` — no-edit-reason: existing local/remote identity vectors retain their contract; new journal-state cases are in the barrier suite.
- `tests/storage/test_project_manager.py` — no-edit-reason: existing identity vectors retain their contract and helper signature.

**Acceptance:**
- 4.5a.1 - Python/native use the same stable secure sidecar, bounded deadline and lock order, without check/open gaps, link substitution, async-loop stalls or recursive-lock deadlocks. test: `crates/gprojects/tests/project_metadata_barrier.rs::python_native_lock_protocol_matches`.
- 4.5a.2 - Every inventoried policy reader refuses pending recovery/timeout without exposing candidate fields or substituting empty/default policy. test: `tests/contracts/test_project_metadata_barrier.py::test_all_policy_readers_refuse_pending_recovery`.
- 4.5a.3 - Chat requires approval, hook and provider callbacks refuse, merge/transcript/close/stop/verification cannot pass unavailable policy, and sandbox resolution refuses before launch; stated ordinary modes and envelopes remain exact. test: `tests/contracts/test_project_metadata_barrier.py::test_policy_boundaries_fail_closed`.
- 4.5a.4 - Approval/validation/hook/verification/init writers preserve unrelated keys, refuse journals and retain cleanup/missing/no-op behavior; save_project_approval_rules and save_project_validation_detection refuse malformed, non-object or unreadable JSON without overwriting corrupt bytes, changing permissions or staging mutation. test: `tests/contracts/test_project_metadata_barrier.py::test_cooperating_writers_merge_under_one_lock`.
- 4.5a.5 - Isolation copies and personal identity repair cannot consume/overwrite/delete a pending candidate; reversed copies terminate without deadlock. test: `tests/contracts/test_project_metadata_barrier.py::test_copy_and_identity_repair_obey_barrier`.
- 4.5a.6 - Specified extractions preserve helper imports, config carrier parity and ordinary overrides; both originals finish below850 and every production file below1,000 lines. test: `tests/contracts/test_project_metadata_barrier.py::test_helper_facades_and_size_boundaries`.
- 4.5a.7 - Native journal-producing mutations remain disabled until all guard integrations are active; pending recovery cannot reactivate an unguarded legacy writer. test: `crates/gprojects/tests/project_metadata_barrier.rs::activation_requires_all_consumers_guarded`.

**Verification:** planned: isolated pytest for metadata-barrier and validation
configuration suites, scoped Ruff/mypy and config-contract checks; native
project_metadata_barrier tests use the Python probe through `uv run` and pipe
barriers against temporary state. No live daemon or real project registration.

### 4.6 Native project updates and soft deletion [category: code] (depends: 4.5a, #24963, #24968)
`kind: deliverable`

Targets:
- `crates/gprojects/src/projects/update.rs`
- `crates/gprojects/src/projects/repository.rs`
- `crates/gprojects/src/projects/update_recovery.rs`
- `crates/gprojects/src/projects/read.rs`
- `crates/gprojects/src/projects/mod.rs`
- `crates/gprojects/src/projects/metadata_lock.rs`
- `crates/gprojects/src/family.rs`
- `crates/gprojects/tests/project_updates.rs`
- `crates/gcore/assets/schema/migrations/478_project_file_mutations.sql`
- `crates/gcore/assets/schema/catalog.manifest.json::*` — scope-reason: regenerate the recovery-witness schema identity
- `crates/gcore/src/grant/bundle.rs::*` — scope-reason: regenerate the embedded schema identity
- `crates/gcore/tests/schema_contract.rs::*` — scope-reason: verify the project-only recovery witness and schema identity
- `crates/gdaemon/tests/cli_contract.rs::*` — scope-reason: keep CLI schema identity coherent
- `src/gobby/storage/schema_expected_identity.json::*` — scope-reason: regenerate the Python schema identity carrier
- `docs/contracts/parity-waivers.json`
- `crates/gdaemon/Cargo.toml`
- `crates/gcode/Cargo.toml`
- `crates/ghook/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: keep all schema-affected managed versions coherent

**Research context:** ProjectUpdate uses exclude_unset and separately validates
ValidationDetectionConfig. update_project.apply_update places SQL and two
project.json savers in one transaction. Memory fa16139d establishes SQL rollback
when a saver raises; it does not restore an earlier file publication. #24960
D3/D4 identifies two independent file rewrites and loaders that return empty
metadata after read/parse/type failure. Reuse 4.3's checkout publication and
section1.3's durability order, retaining their distinct root admission rules,
and 4.2's safe service-error convention, not a new general transaction engine.

Represent field presence as Absent/Clear/Set(value), preserving the existing
field-specific omitted/null/empty and validation contracts. Explicit null for
approval_rules or validation_detection remains a no-op; empty lists and valid
empty config objects retain their existing clear/default semantics. A request
without a non-null file-field change does not inspect, initialize or journal
project.json; SQL-only name changes and true no-ops keep their own contract.
For an actual metadata change, under one project
row lock, load the observed metadata version, apply both requested file fields
to one candidate, preserve unrelated keys, and validate the complete candidate
before staging. No SQL or file mutation occurs if either requested field fails.
Name uniqueness, protected names, local-machine ownership and soft-delete guards
remain exact. Missing metadata permits initialization; malformed/non-object JSON
returns422 {"detail":"Project metadata is invalid"}, and unreadable metadata
returns503 {"detail":"Project metadata is unavailable"}. Neither substitutes {}.
Only 5.4's explicit repair operation backs up and repairs corrupt input.

D3 recovery is bounded to project configuration. Before publication, durably
write a private0700 staging directory with a0600 recovery record containing a
fresh operation UUID, predecessor/candidate hashes, exact predecessor/candidate
bytes and the missing-file distinction. Compare the observed file revision
again before the single held-parent replacement; external change refuses as
conflict instead of overwriting unknown bytes. In the SQL transaction that
updates the project, insert the operation UUID/project/candidate hash into a
small project_file_mutations commit-witness table. Its schema/permissions use
the normal toolchain and identity carriers; migration478 is rebased to the next
free append-only slot at implementation tip. No distributed transaction service.

Export update_recovery from projects/mod.rs and route read.rs configuration
materialization through that same recovery path. Reuse4.5a's descriptor-held
`.gobby/.project.json.lock` and `.gobby/.project-update-recovery/` protocol.
Take the bounded metadata flock before the parent-row lock and hold it across
recovery, read/parse, publication, SQL commit and journal cleanup. Python policy
readers and file-only writers use the same flock and refuse pending evidence;
they never interpret witnesses or expose a candidate. Guarded native reads and
updates resolve any journal first. On ordinary rollback or uncertain commit, query the
witness on a fresh bounded transaction after the prior transaction settles.
A committed witness means finish the complete candidate; absent witness means
restore the predecessor (or original absence), only if the current file matches
the recorded before/after version. Unknown external bytes or unavailable
authority retain the journal and return503 {"detail":"Project update recovery required"};
no new config mutation is admitted. Fsync the recovered file/directory. Remove
and fsync the journal before deleting its witness; a leftover witness is harmless
and can be reclaimed by the next guarded operation. Crash at any publication,
commit or cleanup boundary is recoverable before coherent service state is
exposed. Arbitrary direct filesystem editors are not a cross-store reader API.

Before enabling native mutations/CLI updates, prove4.5a's guard is active in all
cooperating consumers and writers, drain legacy update writers and
route PUT/PATCH exclusively to this service through the existing family backend
selection. Native commands refuse updates until that native ownership is active.
While a journal exists, routing rollback cannot reactivate Python mutation;
retain native recovery or return safe unavailable. No Python/native dual writer
is permitted. 6.1/6.2 verify that handoff. Soft delete keeps the recovery window
and cleanup eligibility without hard-deleting rows, files or pending recovery
evidence. Routing: backend code; tdd true.

This leaf adds #24963 entries projects.projects-put.cross-store-recovery and
projects.projects-patch.cross-store-recovery for exact HTTP PUT and HTTP PATCH
/api/projects/{project_id}, plus projects.projects-put.metadata-refusal and
projects.projects-patch.metadata-refusal for those same surfaces. Each decision
is .gobby/plans/gdaemon-projects-files-docs-family.md#4.6. Replacement fields
state the full D3 journal/witness/coherent-recovery or D4 safe422/503/no-mutation
contract above. KEEP cases remain exact, including accepted null/no-op/empty
semantics; no waiver silently widens or narrows field validation.

**Granularity:** the journal, SQL witness, one file publication, recovery and
mandatory schema/release carriers are one recoverable project-update lifecycle.
Each failure boundary is identified below; soft deletion retains its existing
small service path and does not become a separate recovery owner.4.5a owns guard
integration; this leaf owns the sole native recovery/publication service also
consumed by5.4's verification-field writer.

**Acceptance:**
- 4.6.1 - HTTP PUT /api/projects/{project_id} keeps valid update/validation/no-op/null/empty contracts and owning-checkout scope, except the exact cited D3/D4 failure-state replacements. test: `crates/gprojects/tests/project_updates.rs::put_matches_python`.
- 4.6.2 - HTTP PATCH /api/projects/{project_id} preserves omitted fields and validates the merged locked candidate with the same exact D3/D4 waivers. test: `crates/gprojects/tests/project_updates.rs::patch_matches_python`.
- 4.6.3 - HTTP DELETE /api/projects/{project_id} preserves protected/not-found guards, soft-delete response/state and pending recovery evidence. test: `crates/gprojects/tests/project_updates.rs::soft_delete_matches_python`.
- 4.6.4 - A second-field error, pre/post-publication failure, ambiguous/failed SQL commit and concurrent update preserve or recover one coherent complete state with correct witness/journal ordering and no foreign-root I/O. test: `crates/gprojects/tests/project_updates.rs::update_rolls_back_as_a_unit`.
- 4.6.5 - Process loss at every journal/publication/commit/cleanup boundary recovers before guarded reads or mutation, and unknown external edits or unavailable authority retain evidence with safe503. test: `crates/gprojects/tests/project_updates.rs::crash_recovery_is_durable_and_bounded`.
- 4.6.6 - Missing metadata initializes; malformed, non-object or unreadable metadata refuses unchanged, preserves unrelated keys, and cannot activate the repair-command policy. test: `crates/gprojects/tests/project_updates.rs::metadata_refusal_waivers_are_narrow`.
- 4.6.7 - Pause after rename before SQL commit, then fail or kill the writer: actual Python approval/validation/close consumers block or refuse until witness-based recovery, never approving or crediting the uncommitted candidate. test: `crates/gprojects/tests/project_updates.rs::mixed_runtime_consumers_wait_for_recovery`.
- 4.6.8 - Python field writers, native updates and verification publication serialize without lost unrelated keys; lock timeout/cancellation retains evidence and cannot cause false success or a gate pass. test: `crates/gprojects/tests/project_updates.rs::all_metadata_writers_share_serialization`.

**Verification:** planned: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-projects --test project_updates`,
the isolated schema contract and managed version-pin checks. Inject real process
loss only into isolated temporary state; no live daemon or user files.

## P5: Lifecycle-safe purge and native operator commands
`kind: framing`

### 5.1 Shared cross-runtime project write fence [category: code] (depends: 4.6)
`kind: deliverable`

Targets:
- `crates/gcore/src/project_fence.rs`
- `crates/gcore/src/postgres_pool/mod.rs::*` — scope-reason: pool-owned bounded fence checkout admission must share capacity across every fence instance
- `crates/gcore/src/lib.rs::*` — scope-reason: export the shared cross-family fence API
- `src/gobby/storage/hub/postgres.py::*` — scope-reason: add the pool-owned fence connection lease while preserving ordinary transaction and resize contracts
- `src/gobby/projects/write_fence.py::*` — scope-reason: preserve writer/global_writer/exclusive APIs while sharing their authority across processes
- `src/gobby/runner_init/services.py::*` — scope-reason: pass the hub database at both fence-construction sites
- `tests/projects/test_write_fence.py::*` — scope-reason: provide isolated shared lock storage to every fence fixture
- `tests/memory/test_indexing_service.py::*` — scope-reason: provide shared lock storage to the indexing fence fixtures
- `tests/mcp_proxy/test_semantic_search.py::*` — scope-reason: provide shared lock storage to the semantic-search fence fixture
- `tests/projects/test_cross_runtime_write_fence.py`
- `crates/gcore/tests/project_fence.rs`
- `crates/gdaemon/Cargo.toml`
- `crates/gcode/Cargo.toml`
- `crates/ghook/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: keep all affected shared-core managed versions coherent

**Research context:** ProjectWriteFence currently coordinates derived writers and purge in one asyncio process, including reentrant writers, global writers, timeout and cancellation. Native purge cannot safely use a separate in-process lock while Python writers remain active. The existing fenced vector store and purge service consume this API without needing a new caller protocol.

Implement a shared PostgreSQL advisory-lock contract in gcore and the existing Python fence API. Use namespaced stable keys for a project and global-writer gate. Writers hold shared project locks; global writers hold the shared global gate; purge acquires exclusive global then exclusive project in a fixed order. Pin a dedicated pooled connection for lock lifetime; cancellation releases or discards it before reuse. Preserve task-level reentrancy and deleted/absent-project admission checks. Serialize purges at the global gate as the minimum correct cross-runtime mechanism; do not invent a lock service or lease table. Existing Python consumers keep their API and gain shared exclusion. Every future native derived writer uses this same public gcore fence. Routing: backend code; tdd true.

The pin is a lease from the existing operation pool, not a second pool or an
unbounded side connection. Native PoolSettings.max_size is at least two and
PostgresHubDatabase owns the Python pool and its resize API. Add pool-owned
admission using the existing Tokio/asyncio semaphore mechanisms: lock holders
and acquisitions waiting for an advisory lock together consume at most
max_size minus one leases. One connection remains available for admitted work's
ordinary queries, including fetch_all_project_memories. Acquire this permit
before checking out a lock connection; never hold an operation connection while
waiting for the permit. All fence instances sharing a pool share this cap.
Nested ownership reuses the task's lease/connection, including global admission,
so nesting never waits for another capacity permit it already blocks. Release
locks/connection before the permit on every exit. Resize/shutdown must drain or
fail within the existing deadline before reducing capacity below live leases;
never silently eliminate the operation reserve. This uses no new service,
connection-budget setting or configuration knob.

**Granularity:** the two runtime implementations and release carriers form one cross-process lock contract. Purge itself remains a separate outcome.

**Acceptance:**
- 5.1.1 - A Python writer blocks Rust purge and a Rust writer blocks Python purge until drain; deleted/absent projects refuse new writers. test: `tests/projects/test_cross_runtime_write_fence.py::test_both_runtimes_share_exclusion`.
- 5.1.2 - Reentrancy, global writers, concurrent purge, timeout/cancellation and dropped connections preserve safety without leaked locks or pool contamination. test: `crates/gcore/tests/project_fence.rs::lock_lifecycle_is_safe`.
- 5.1.3 - Required shared-core binary bumps, maintenance floor, lockfile and managed pins agree. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.
- 5.1.4 - With the minimum two-slot operation pool, concurrent fenced writers, a waiting purge, nested ownership, resize and cancellation make bounded progress without starving ordinary queries or leaking permits/connections. test: `crates/gcore/tests/project_fence.rs::small_pool_progress_is_bounded`.
- 5.1.5 - The Python pool uses the same reserve and releases capacity on failure/cancellation; concurrent writers and purge finish under a bounded saturation test. test: `tests/projects/test_cross_runtime_write_fence.py::test_small_pool_progress_is_bounded`.

**Verification:** planned: isolated pytest for tests/projects/test_cross_runtime_write_fence.py and focused gobby-core project_fence nextest.
Collect both exact proposed Python node IDs:
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest --collect-only -q tests/projects/test_cross_runtime_write_fence.py::test_both_runtimes_share_exclusion tests/projects/test_cross_runtime_write_fence.py::test_small_pool_progress_is_bounded`.

### 5.2 Native lifecycle-safe hard purge [category: code] (depends: 5.1, #24454)
`kind: deliverable`

Targets:
- `crates/gprojects/src/projects/purge.rs`
- `crates/gprojects/src/projects/purge_rows.rs`
- `crates/gprojects/tests/project_purge.rs`
- `crates/gprojects/Cargo.toml`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: keep managed release pins coherent with the binaries this deliverable changes

**Research context:** ProjectPurgeService._purge_project soft-deletes, refuses active terminals, resolves cleaners before disabling jobs, drains active cron runs, then exclusively invalidates code, clears vectors/graph and deletes hub rows. _delete_hub_rows appends embedding-ledger tombstones in batches of 100, detaches foreign references and rechecks terminals under a parent-row lock. _drain_cron_runs is a bounded state query, not a Python-only cancellation API.

The code child also crosses a credential boundary:
src/gobby/projects/purge.py::ProjectPurgeService._maintenance_env enters
src/gobby/runtime_grants/maintenance.py::HandshakeMaintenanceLaunchFactory.open_async;
its open method calls HandshakeService.issue_for_maintenance and
materialize_managed_launch, then revokes the invocation and removes its secrets.
src/gobby/runner_init/servers.py::maintenance_target_admitted permits an indexed
project ID or a soft-deleted project even after checkout release. A normal
interactive grant is intentionally refused for that target. The native runtime
plan's #24454 (Managed credential issuance) supplies issue_maintenance, revoke,
private bootstrap materialization, scoped DSN and issuance rollback; its
prerequisites supply signing, capability verification and the native challenge.
Those APIs are planned external dependencies, not existing gprojects symbols.
Reuse them through a gobby-runtime crate dependency, with no duplicate issuer,
database role schema, token verifier or grant framework.

Port this exact sequence with gcore's pool, fence and existing native datastore clients. Enumerate managed active/staged/aliased Qdrant collections and strict project graph cleanup using the current ProjectVectorCleaner and graph cleaner contracts; invalidate code through the installed native gcode command. Read/disable/drain/delete project cron rows using their current schema and bounded deadline; no cron scheduler port is required. Preserve projection tombstones, cross-project detach rules, terminal guards and transaction boundaries. External cleanup failure leaves the project soft-deleted and retryable; never delete the parent first. Repeated purge is safe and gives existing status/error shapes. This leaf does not remove project source checkouts or comms byte storage. Routing: backend code; tdd true.

Inside purge.rs, port only the per-invocation maintenance launch orchestration:
admit the exact indexed-or-soft-deleted target, bind the local daemon machine
and a fresh execution UUID with no session/checkout, and call #24454's issuer.
Reuse the runtime grant signer and capability format to materialize a private
0700 temporary launch directory and 0600 grant.json, with the matching scoped
bootstrap and maintenance bearer expiring at the bounded child deadline.
Pass only the explicit managed execution/project/bootstrap environment to the
installed gcode invalidate command; clear inherited interactive/agent grant
variables and never borrow the daemon's ambient grant. Bound argv, output and
runtime with the existing subprocess facility. On success, spawn/child failure,
timeout or cancellation, stop and reap the child, revoke that invocation's
credential generation through the native API, and remove its temporary secrets.
Keep cancellation-safe teardown alive until cleanup completes or the existing
bounded revoke failure contract records a durable retry; a cleanup failure is
a purge failure and cannot report success or advance to parent-row deletion.
Retry records contain no secrets and reuse #24454's format. Exercise the real
installed gcode against isolated state with Python unavailable and no ambient
interactive grant, not only a fake child which ignores its environment.

**Acceptance:**
- 5.2.1 - The source row HTTP POST /api/projects/{project_id}/purge matches purged/not_found/protected/failed statuses and dependent-resource cleanup without Python. test: `crates/gprojects/tests/project_purge.rs::purge_matches_python`.
- 5.2.2 - Active terminals, draining cron, derived writers and concurrent terminal insertion cannot race a destructive purge; timeouts leave retryable state. test: `crates/gprojects/tests/project_purge.rs::lifecycle_guards_hold`.
- 5.2.3 - Injected code/vector/graph/database failures and retries preserve tombstone/detach ordering and never delete another project's resources or source checkout. test: `crates/gprojects/tests/project_purge.rs::failures_are_retryable_and_scoped`.
- 5.2.4 - A real isolated gcode child invalidates the intended soft-deleted and indexed-only targets through fresh project-scoped maintenance authority with Python down and no interactive grant; foreign project/machine claims are refused. test: `crates/gprojects/tests/project_purge.rs::maintenance_child_authority_is_scoped`.
- 5.2.5 - Success, spawn failure, child failure, timeout and cancellation revoke the invocation and remove private launch secrets; injected revoke failure records the native retry and prevents purge success or hub-row deletion. test: `crates/gprojects/tests/project_purge.rs::maintenance_child_teardown_is_bounded`.

**Verification:** planned: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-projects --test project_purge`.

### 5.3 Native Projects command group and ordinary verbs [category: code] (depends: 5.2)
`kind: deliverable`

Targets:
- `crates/gprojects/src/cli_projects.rs`
- `crates/gprojects/src/cli_project_repair.rs`
- `crates/gprojects/tests/projects_cli.rs`
- `crates/gdaemon/src/main.rs::*` — scope-reason: expose family-owned native projects commands
- `src/gobby/cli/projects.py::*` — scope-reason: replace the Projects command group with transitional native forwarding
- `tests/cli/test_native_projects_forwarding.py`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: keep managed release pins coherent with the binaries this deliverable changes

**Research context:** cli/projects.py resolves names/IDs, lists/shows checkout-shaped payloads, requires exact-name confirmation for destructive commands, protects sentinel names, rebinds with a cache hint and repairs cwd marker/checkout drift. resolve_rebind_project/resolve_refresh_root establish local root selection. D3 assigns these operations here; #21571 later re-fronts them and is not a prerequisite.

Implement gdaemon projects group/help, list/show, rename/delete/purge/rebind/repair using the native service once. Use HTTP to the selected hub for database-owned operations, and local marker validation on the executing machine; nodes never connect to PostgreSQL. Reuse the existing key-bound checkout route for registration and use existing PATCH for rename and POST checkout registration for repair; add no generic operator-action endpoint. Preserve flags, confirmation text, JSON/text shapes, stderr and exit codes. A repair dry run is read-only; --fix registers only a valid missing checkout and never silently rebinds. Click parses/forwards argv and process streams/status only; it has no SQL, filesystem mutation or AI logic. Routing: backend code; tdd true.

**Granularity:** nine acceptance items deliberately enumerate the existing group and verbs so no parity row disappears. One thin command dispatcher consumes already implemented services; verification synthesis is split into 5.4.

**Acceptance:**
- 5.3.1 - The source row CLI gobby projects delegates help/group/usage to Rust with matching exit behavior. test: `crates/gprojects/tests/projects_cli.rs::group_usage_matches`.
- 5.3.2 - The source row CLI gobby projects list matches --all/--json filtering and text/JSON rendering. test: `crates/gprojects/tests/projects_cli.rs::list_matches_python`.
- 5.3.3 - The source row CLI gobby projects show resolves names/IDs and matches detail/error rendering. test: `crates/gprojects/tests/projects_cli.rs::show_matches_python`.
- 5.3.4 - The source row CLI gobby projects rename preserves uniqueness/sentinel rules and output. test: `crates/gprojects/tests/projects_cli.rs::rename_matches_python`.
- 5.3.5 - The source row CLI gobby projects delete preserves exact-name confirmation/protection and delegates soft deletion. test: `crates/gprojects/tests/projects_cli.rs::delete_matches_python`.
- 5.3.6 - The source row CLI gobby projects purge preserves confirmation and native purge outcomes. test: `crates/gprojects/tests/projects_cli.rs::purge_matches_python`.
- 5.3.7 - The source row CLI gobby projects rebind validates local root/marker, uses key-bound machine authority and retains the cache hint. test: `crates/gprojects/tests/projects_cli.rs::rebind_matches_python`.
- 5.3.8 - The source row CLI gobby projects repair preserves dry-run/fix/no-drift/conflict outcomes and never silently rebinds. test: `crates/gprojects/tests/projects_cli.rs::repair_matches_python`.
- 5.3.9 - The Click shims forward argv/stdout/stderr/exit status to Rust and execute no Python project business logic. test: `tests/cli/test_native_projects_forwarding.py::test_all_verbs_delegate_to_rust`.
- 5.3.10 - CLI configuration updates require 4.6's sole native mutation owner and journal recovery, refusing while legacy mutation routing remains active; no direct SQL/file bypass is possible. test: `crates/gprojects/tests/projects_cli.rs::updates_require_native_recovery_owner`.

**Verification:** planned: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-projects --test projects_cli` and isolated pytest for tests/cli/test_native_projects_forwarding.py.
Collect the exact proposed Python node ID:
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest --collect-only -q tests/cli/test_native_projects_forwarding.py::test_all_verbs_delegate_to_rust`.

### 5.4 Native verification-command refresh [category: code] (depends: 5.3, 4.6, #21587)
`kind: deliverable`

Targets:
- `crates/gprojects/src/verification/mod.rs`
- `crates/gprojects/src/verification/evidence.rs`
- `crates/gprojects/src/verification/candidates.rs`
- `crates/gprojects/src/verification/refresh.rs`
- `crates/gprojects/src/projects/update_recovery.rs`
- `crates/gprojects/src/projects/metadata_lock.rs`
- `crates/gprojects/src/cli_projects.rs`
- `crates/gprojects/tests/verification_refresh.rs`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: keep managed release pins coherent with the binaries this deliverable changes

**Research context:** project_verification.refresh owns deterministic/AI modes, preview versus --fix, corrupted-project backup and atomic verification-file replacement. candidates.generate_candidates/select_best_candidates/is_safe_validation_command and evidence.collect_evidence supply bounded evidence, ranking and command safety. synthesis.synthesize_verification_commands uses the daemon generation service. #21587 owns the native AI-provider route consumed here; the dependency is explicit rather than retaining Python synthesis.

Port deterministic evidence/candidate generation and safe command classification as one verification outcome. Preserve --ai auto/on/off, profile/candidate overrides, schema/rejection limits and warning/JSON/diff behavior. Use the existing gcore daemon text-generation client against #21587's native generation route; never call Python or directly bypass the daemon's AI routing policy. auto falls back deterministically with a warning; on fails if AI is unavailable; off makes no AI request. Preview changes nothing. Never execute generated commands during refresh. Routing: backend code; tdd true.

Preview evidence and --fix start from4.5a's guarded snapshot. Candidate generation
and AI calls run outside the metadata lock. At publication use4.6's shared native
publication/recovery owner: acquire its bounded flock before the project row
lock, resolve pending journal/witness state, re-read current metadata, then merge
only verification fields while retaining unrelated keys/permissions. Intervening
policy changes survive instead of being replaced by the earlier evidence snapshot.
--fix is the sole explicit corrupt-input repair path; durably back up the exact
malformed bytes under this guard before replacement. A recovery journal never
counts as corruption. After recovery, existing file-only durable publication
suffices; create no second SQL witness or journal interpreter for verification.
Unavailable recovery authority uses the existing safe command-error/exit
convention. No lock spans AI/network waits.

**Granularity:** the evidence, candidate and publication modules form one command's deterministic/AI pipeline. Their shared result and atomic file owner make a single leaf appropriate despite the production-target count.

**Acceptance:**
- 5.4.1 - The source row CLI gobby projects refresh-verification delegates entirely to Rust and preserves every mode, override, result and exit behavior. test: `crates/gprojects/tests/verification_refresh.rs::command_matches_python`.
- 5.4.2 - Bounded evidence and deterministic selection match Python vectors; unsafe or unsupported AI commands are rejected, and no candidate is executed. test: `crates/gprojects/tests/verification_refresh.rs::selection_and_ai_policy_match`.
- 5.4.3 - Preview, corrupt-input backup and injected write/cancellation failures preserve unrelated metadata and recoverable predecessor bytes. test: `crates/gprojects/tests/verification_refresh.rs::publication_is_atomic`.
- 5.4.4 - Refresh racing with native updates and Python field merges retains verification and policy keys; a paused/crashed update cannot feed preview/--fix an uncommitted snapshot, and the sole owner recovers or safely refuses pending evidence. test: `crates/gprojects/tests/verification_refresh.rs::refresh_and_policy_updates_share_recovery`.

**Verification:** planned: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-projects --test verification_refresh`.

## P6: Native default, complete parity and operational proof
`kind: framing`

### 6.1 Read/write compare verification and native default [category: code] (depends: 5.4, #24963, #24964, #24967)
`kind: deliverable`

Targets:
- `crates/gprojects/src/family.rs`
- `crates/gprojects/src/compare.rs`
- `crates/gprojects/tests/compare.rs`
- `crates/gdaemon/tests/projects_routing.rs`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: keep managed release pins coherent with the binaries this deliverable changes

**Research context:** RouteFamily.check_proxied and gdaemon compare/report are already present. Runtime-family precedent compares GET/HEAD through a read-only native leg and verifies writes against committed state; it never executes both writes. Namespace changes and destructive migration/purge require an explicit verifier rather than a second invocation.

Complete the per-route comparison registry using #24963's single registry and
outcome semantics. Read legs compare only permitted generated-value normalization;
writes run once and verify resulting DB/filesystem state or a preflight error.
Every D1-D7 replacement resolves its exact projects.* waiver, source surface,
decision and asserted replacement from the owning section. A Waived result must
prove that replacement in the isolated native corpus and the corresponding
single-execution verifier; a citation alone is insufficient. Ordinary KEEP
responses, headers and state remain Passed requirements. Never mask an error,
path or field to manufacture equality. Upload/migration/purge and failure-case
verification do not repeat writes or consume streams twice. Concurrent revision/
lease changes may record Skipped diagnostically, but unwaived Skipped fails
readiness; no debt waiver represents concurrency as a successful skipped case.
Require at least one Passed case plus representative method/outcome coverage,
valid case-specific Waived assertions and zero mismatches/unwaived skips.
The two additive surfaces in2.2/4.1 instead require exact native-extension
entries and their native contract proofs; they never run a fictitious Python
leg or manufacture a Passed parity case. They cannot satisfy the requirement
for at least one real Python/native Passed case elsewhere in the family.
Redact content, credentials and host paths; report structural locations only.

Before activating native project-config mutations, drain legacy writers, recover
any journal and establish 4.6's sole native writer through the existing family
backend selection. CLI update paths require that ownership. A pre-switch routing
rollback preserves native recovery or safely refuses config writes; it never
restarts an unsafe Python writer against a pending journal. Other rollout
selection keeps its existing Proxy/Compare contract. After all handlers, #24967
admission and #24964 outer CORS pass, default the family to Native. No fail-open
fallback or supervised-backend mode is added; naming-switch completion requires
no Python. Routing: backend code; tdd true.

**Acceptance:**
- 6.1.1 - Every read/mutation has single-execution verification, all exact D1-D7 waivers prove their replacement, and concurrent/unavailable unwaived Skipped results fail readiness rather than becoming matches. test: `crates/gprojects/tests/compare.rs::every_route_has_safe_verification`.
- 6.1.2 - Default Native dispatch serves all owned principals and methods; explicit pre-switch proxy/compare selection and unknown-family fallback remain correct. test: `crates/gdaemon/tests/projects_routing.rs::native_default_is_complete`.
- 6.1.3 - Compare logs/report contain no profile content, attachment bytes, credentials or user path secrets and reject an incomplete representative route window. test: `crates/gprojects/tests/compare.rs::reports_are_bounded_and_complete`.
- 6.1.4 - Zero-Passed/all-skipped reports, missing or stale waiver citations and over-broad replacements fail; ordinary KEEP cases remain exact, and config-writer handoff/rollback preserves recovery ownership. test: `crates/gprojects/tests/compare.rs::waiver_readiness_and_writer_handoff_are_enforced`.
- 6.1.5 - Both additive endpoints require exact native-extension provenance and native tests; missing entries or fabricated Python successes fail compare readiness. test: `crates/gprojects/tests/compare.rs::native_extensions_have_no_python_oracle`.

**Verification:** planned: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-projects --test compare` and focused gobby-daemon projects_routing nextest.

### 6.2 Cross-surface native parity and two-machine failure proof [category: test] (depends: 6.1, #24963)
`kind: deliverable`

Targets:
- `tests/e2e/test_projects_files_family.py`
- `tests/contracts/test_projects_files_corpus.py`
- `tests/contracts/projects_files_cases.json`
- `tests/contracts/projects_files_inventory.json`

**Research context:** 1.1 records the 36 source rows and two additive routes and planned namespace/layout deltas. The family now has native auth, service, migration and CLI/MCP adapters. Existing contract/E2E isolation fixtures provide temporary hub state and backend process control. This is a standalone end-to-end parity/failure suite, not another unit-test wrapper.

Replay the complete corpus against isolated native hub and node processes. Exercise all 36 source owners and both native extensions, renamed replacement routes, both shared cookie classes and every admitted/refused principal, migration reruns and current consumer serializers. Kill Python after issuing test admission and prove native local/hub/attachment/project/command behaviour, including refusals and #24964 CORS. Inject deterministic ancestor swaps, short writes/fsync/rename/SQL failures, process loss, malformed/unreadable metadata and unusual filename cases. Each D1-D7 divergence proves its exact #24963 Waived replacement and citation; KEEP cases remain exact Passed obligations. No live vulnerable Python race or duplicate mutation is used. Test an independent node with no hub bytes or DB credentials. Representative readiness requires at least one Passed, zero mismatches, no unwaived Skipped and all required method/outcome classes. Rollback uses the same binaries and never remigrates bytes or reactivates unsafe config mutation while recovery is pending. No production daemon is touched.

**Acceptance:**
- 6.2.1 - All 36 source rows satisfy their named parity acceptance; both additive routes prove native-extension contracts, with explicit native replacement coverage for renamed routes. test: `tests/e2e/test_projects_files_family.py::test_all_owned_rows_have_native_parity`.
- 6.2.2 - Killing isolated Python leaves API-key/local/browser-admitted native operations serving and preserves refusal/transaction semantics. test: `tests/e2e/test_projects_files_family.py::test_family_survives_python_loss`.
- 6.2.3 - A second machine without local hub bytes or DB credentials accesses hub documents and only its own local checkout, refusing cross-machine root and identity forgery. test: `tests/e2e/test_projects_files_family.py::test_two_machine_ownership_is_correct`.
- 6.2.4 - Representative compare has zero mismatches and accounted coverage; pre-switch routing rollback uses the same binaries and never remigrates bytes. test: `tests/e2e/test_projects_files_family.py::test_compare_and_routing_rollback_are_safe`.
- 6.2.5 - The debt matrix proves every cited replacement under injected failure/process loss and rejects masks, all-skipped readiness and unwaived divergence while keeping ordinary contracts exact. test: `tests/e2e/test_projects_files_family.py::test_debt_waivers_assert_recovery_and_keeps`.

**Verification:** planned: the isolated-hub protected pytest command for tests/contracts/test_projects_files_corpus.py and tests/e2e/test_projects_files_family.py; obtain fleet admission for E2E.
Collect the exact proposed E2E node IDs in isolated state:
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest --collect-only -q tests/e2e/test_projects_files_family.py::test_all_owned_rows_have_native_parity tests/e2e/test_projects_files_family.py::test_family_survives_python_loss tests/e2e/test_projects_files_family.py::test_two_machine_ownership_is_correct tests/e2e/test_projects_files_family.py::test_compare_and_routing_rollback_are_safe`.

### 6.3 Ledger evidence and downstream cutover contract [category: docs] (depends: 6.2, #24963, #24970)
`kind: deliverable`

Targets:
- `docs/contracts/parity-ledger-status.d/comms-cron-hub-nodes.json::*` — scope-reason: classify the owned hub project listing and new native seam routes
- `docs/contracts/parity-ledger-status.d/git-workspaces.json::*` — scope-reason: classify every owned files row and canonical hub document replacement
- `docs/contracts/parity-ledger-status.d/plans-stages-projects.json::*` — scope-reason: classify the nineteen Projects CLI and HTTP rows
- `docs/contracts/parity-ledger-status.d/websocket-chat-voice-ui.json::*` — scope-reason: classify the four shared attachment source rows and canonical replacements
- `docs/contracts/parity-ledger.md`
- `docs/contracts/projects-files-hub.md`
- `docs/guides/configuration.md`
- `docs/architecture/hub-owned-files-home.md`
- `tests/contracts/test_projects_files_ledger.py`

**Research context:** parity-ledger.md is generated by scripts/generate_parity_ledger.py. The supported success status is delegated, not an invented native status. Removed route keys stay as dropped-by-decision evidence; other keys must identify current registrations. The original 35 source keys plus the transferred node-checkout row give 36 source owners. #21592 consumes the shared attachment service, while #21576 owns comms producers and their separate byte migration.

After 6.2 passes, record delegated status with exact native symbols/tests for every surviving source key and canonical replacement. Preserve removed namespace keys as dropped by decision #24951 with their replacement and proof; retain no proxied claim for completed owned service work. Regenerate the ledger and assert no new unclassified row. Record #21592's dependency on the shared service outcome in the contract for the coordinator to wire at expansion; do not expand/edit other task trees here. Document #21576's destination/API and strict exclusion of comms byte migration from this plan. Describe the announced migration/native flip, crash recovery and pre-naming routing rollback, and the naming-switch deletion of transitional shims with no supervised Python mode.

Explicitly reassign HTTP POST /api/nodes/checkouts in the comms-cron-hub-nodes
shard: replace the old #24151 ownership note with Projects #21585, this plan's
decision #24951, implementation section4.5 and acceptance4.5.5 plus its native
symbol/test evidence. #24151's recorded transfer is the authority; it retains
no duplicate checkout-registration owner. Include the row in the 36-source
inventory and generated ledger. Record the two exact native-extension entries
from2.2/4.1 in the appropriate shard rows with delegated implementation evidence
and native-extension provenance; the shared consumers report that provenance
without a Python parity leg. Every decision value uses the #24963 canonical
task-ref or plan-path#section grammar.

Publish the D1-D7 disposition/waiver ID and exact replacement inventory using
#24963's shared registry; no family-local waiver file or skipped-only proof.
Document S2's explicit admitted-authority convention with #24967/#24452 and
#24964, and S4's #24970-owned classification/shared adapters and
Absent/Clear/Set merged validation. For S3, inventory current Python fence
injection at runner_init/services.py and consumers in runner_init/orchestration.py,
runner_init/project_purge.py and memory/vector services. Every native derived
writer port (Memory, Code index, AI/vector, and relevant Agents/Tasks/Sessions/
Workflows/Comms producers) consumes 5.1's public gcore fence before concurrent
native purge; downstream implementation dependencies bind to that capability,
never a private lock or this planning root. Retain the minimum-pool reserve and
cross-runtime cancellation/progress test contract. Record the shared files_home
capability boundary for Cron: #24968 supplies descriptor-safe raw-stream I/O;
Cron owns its isolated locator and run-retention lifecycle. Coordinate external
edges after approval without expanding another task tree here.
Specifically, record and verify the concrete1.2 implementation-task to existing
#24968 producer edge and each #24968 consumer edge from section1.3. Approval or
a reference to the Projects root cannot substitute for that dependency graph.

**Acceptance:**
- 6.3.1 - Each of the 36 exact source keys has proven delegated or explicit renamed-route decision status and a native replacement/test; no scoped key is silently omitted and no unrelated shard row changes ownership. test: `tests/contracts/test_projects_files_ledger.py::test_owned_rows_are_complete`.
- 6.3.2 - Regeneration yields the checked-in ledger, canonical new routes are classified and the eight API-docs UI rows remain #21589-owned. test: `tests/contracts/test_projects_files_ledger.py::test_generated_ledger_matches_inventory`.
- 6.3.3 - The contract states #21592's shared-service dependency, #21576's comms boundary, admission reuse, once-only migration and naming-switch shim removal. file: `docs/contracts/projects-files-hub.md`.
- 6.3.4 - Ledger/replay/compare cite the same exact D1-D7 entries, native extensions, #24970 ownership and downstream fence/files_home contracts; malformed/stale citations or unwaived skips cannot close the family. test: `tests/contracts/test_projects_files_ledger.py::test_debt_and_shared_capability_ownership_is_complete`.
- 6.3.5 - The node-checkout source row has exactly the transferred Projects owner and4.5.5 proof, both additive routes have exact native-extension citations/native tests, and no #24151 or fabricated Python ownership remains. test: `tests/contracts/test_projects_files_ledger.py::test_checkout_transfer_and_extensions_have_single_owners`.

**Verification:** planned: `uv run python scripts/generate_parity_ledger.py`, isolated protected pytest for tests/contracts/test_projects_files_ledger.py and tests/test_parity_ledger.py.
Collect the exact proposed Python node IDs:
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest --collect-only -q tests/contracts/test_projects_files_ledger.py::test_owned_rows_are_complete tests/contracts/test_projects_files_ledger.py::test_generated_ledger_matches_inventory`.

## E1 Verification and review gates
`kind: verification`

Observed during original research: ledger reads yielded 35 scoped source rows;
coordinator review then transferred the existing node-checkout row from #24151,
giving 36 current source owners plus two separately identified additive routes.
Source/evidence reads resolved the named existing seams. Implementation,
migration, runtime parity and E2E commands above are planned checks.

Before each review, run:
`uv run gobby plans validate .gobby/plans/gdaemon-projects-files-docs-family.md -p /Users/josh/Projects/gobby`.
The narrative draft has no M1 and has not run expansion validation.
After adversary derivation, run the same base validation and:
`uv run gobby plans validate .gobby/plans/gdaemon-projects-files-docs-family.md -p /Users/josh/Projects/gobby --mode expansion`.
Any narrative edit after derivation requires a fresh adversary-derived manifest.
The coordinator presents the stamped bytes to Josh; explicit operator approval
precedes expansion.

Executors also run scoped Rust format/clippy and focused nextest checks,
Python Ruff/mypy for changed production paths, changed-test type audit, and
the existing frontend type/lint/Vitest checks for 3.5's consumer changes.
Generated schema, ledger and version carriers are checked from their owning
deliverables. No planned success is claimed as validation evidence.

## V1 Plan Changelog
`kind: framing`

2026-10-24 — Initial narrative by Plan Writer gobby#17571 for planning task
#24951. Scope, wiki retirement, transitional native command entry and shared
admission ownership follow the coordinator's confirmed Decision Record.
No consensus or manifest is claimed in this initial draft.

2026-10-24 — Writer accepted adversary findings PA-001 through PA-004 and
repaired Python test collection, native node export/dispatch ownership,
project-scoped maintenance-child authority and bounded fence pool progress.
Runtime prerequisites #24452 and #24454 reuse the existing capability and
managed-credential contracts. Review of the revised bytes is pending.

2026-10-24 — Narrative consensus reached between Plan Writer gobby#17571
and Plan Adversary gobby#17574 after independent re-review of
60b737b81ed0859b357ba00409dab3f12b624431. PA-001, PA-002 and PA-003 are
resolved; PA-004's pool-capacity clarification and saturation tests are accepted
and verified in the narrative. Both seats confirm complete scoped ownership,
native node routing, reuse of #24452/#24454, bounded maintenance-child teardown
and fence progress. No findings or governing decisions remain open. The
adversary will derive M1 from the committed bytes containing this entry;
operator approval still gates task expansion.

2026-10-24 — The prior consensus and M1 stamp at
27fe518b3832fee07c3ee31d909e8e2194f95c81 are superseded for Josh's debt-first
review (memory 9066f1eb; research #24960). Withdraw the entire derived M1 block
under the authorized recovery route; narrative debt amendments and renewed
adversary consensus precede fresh derivation and operator presentation.

2026-10-24 — Debt-first amendments disposition D1-D6 as narrow DROP
replacements and D7 as the retained approved cutover, all through #24963's
shared waiver registry. S2 consumes #24967 with explicit native admission;
#24964 owns CORS; S3 retains one shared fence; S4 aligns the common safe-error
and presence conventions. Native project-config recovery gains a durable
journal/witness contract. Prior consensus remains superseded pending re-review.

2026-10-24 — Writer accepted PA-D01 through PA-D03 from review of94fbc91.
Add a prerequisite metadata barrier for service policy readers and cooperating
writers, including isolation/identity helpers; verification refresh reuses its
lock and the sole recovery engine. Add the exact migration-layout waiver and
expansion-time1.2-to-#24968 producer edge. Combined validation and renewed
adversary review are required before consensus or a fresh M1 stamp.

2026-10-24 — Renewed debt-amendment consensus reached between Plan Writer
gobby#17571 and Plan Adversary gobby#17574 after independent re-review of
ba44dd5d86296d644eaf2210883ff51e180d4950. PA-D01, PA-D02 and PA-D03 are
resolved. Both seats confirm the shared metadata barrier precedes the sole
recovery engine, verification refresh shares serialization, the CLI layout
replacement has its exact waiver, and the existing files capability has its
concrete assembly prerequisite. No blockers or governing decisions remain open.
The adversary derives fresh M1 from the committed bytes containing this entry;
Josh's approval still gates expansion.

2026-10-24 — Coordinator review requires one amendment round: bind shared
error ownership to #24970, prohibit corrupt-input file-only saves, register the
two native extensions, transfer the node-checkout ledger row, verify capability
producer ordering before consumer admission, and use canonical waiver citations.
Withdraw the entire superseded M1 from526ed38 under memory f5577ae0 before
narrative repair; renewed adversary consensus and derivation are required.

2026-10-24 — Applied all coordinator amendments: #24970 supplies shared error
classification/adapters; policy-field saves refuse corrupt/non-object/unreadable
metadata unchanged; two additive HTTP routes have exact native-extension entries;
the node-checkout transfer yields36 source owners;1.3 verifies producer/consumer
edges before admission; waiver decisions use canonical task or plan citations.
Fresh validation and adversary recheck precede renewed consensus and M1.

2026-10-25 — Coordinator-amendment consensus reached between Plan Writer
gobby#17571 and Plan Adversary gobby#17574 after independent re-review of
cba091db4584315ab1d4abd6ce94d66cd7227612 and fresh base validation after the
shared index refresh. Both seats confirm all coordinator rulings are resolved:
#24970 owns shared errors/adapters, corrupt policy-field saves refuse unchanged,
native extensions have exact provenance, the node-checkout row has its Projects
owner, capability edges are verified before admission, and waiver decisions use
canonical citations. No findings or governing decisions remain open. The
adversary derives fresh M1 from the committed bytes containing this entry;
Josh's approval still gates expansion.
