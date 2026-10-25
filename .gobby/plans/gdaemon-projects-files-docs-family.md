Plan artifact: `.gobby/plans/gdaemon-projects-files-docs-family.md`

# Gobby 1.0 Stage 2: Projects, checkout files, and hub documents

**Plan ID:** gdaemon-projects-files-docs-family

## Overview
`kind: framing`

Port #21585's complete service surface to a statically linked family crate,
`crates/gprojects` (package `gobby-projects`). This plan owns 35 existing
parity-ledger rows: 12 assigned solely to #21585, 19 Projects rows formerly
assigned only to #21568, and four attachment rows shared with #21592. It also
owns the once-only hub namespace and attachment-layout cutover.

The result is native project and checkout management, local checkout browsing,
hub USER.md and attachment access, migration, and the corresponding CLI/MCP
operations. Nodes access hub documents through the hub API and never open a
hub datastore or infer that another machine's checkout is local.

## Decision Record
`kind: framing`

The Orchestrator confirmed these dispositions through Triage on 2026-10-24,
recorded under planning task #24951:

- **D1, scope:** all 35 rows above belong here. The eight GET/HEAD API-docs UI
  rows for /docs, /redoc and /openapi.json remain with #21589.
- **D2, wiki:** wiki retirement supersedes the historical /api/hub/wiki/*
  clause. Preserve ordinary 404 responses and retained bytes; recreate no wiki API.
- **D3, command entry:** native family operations are exposed by
  `gdaemon projects` and `gdaemon files`. Existing Click groups become
  argument/exit-code forwarding shims. The current MCP registry delegates
  list_all_projects to Rust. #21571 and #21570 later re-front these surfaces.
  These are transitional shims deleted at the naming switch; this plan adds
  no supervised-backend mode or permanent Python execution requirement.
- **D4, authentication:** Projects owns the bounded shared native admission
  seam because it is the first family to need it. API keys, the existing local
  credential, and confined browser cookies retain their authority and refusal
  rules. Other families can reuse this seam. Cookie traffic stays proxied until
  2.2 lands. Native sign-in UX is outside this family: existing issuance publishes
  the carrier; its own route must reach parity before Python's naming-switch removal.
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
- **D7, restraint:** the port is required because all 35 rows are currently
  proxied. Reuse RouteFamily, the async pool/transaction seam, existing native
  node relay, local-token verification, compare reporting, and contract harness.
  Stdlib/locked crates supply filesystem, HTTP, crypto and JSON work. Add no
  external dependency, generic migration framework, new credential, or transport.
  The shared admission carrier and cross-runtime purge fence are justified by
  current Python/Rust coexistence and trust/data-loss boundaries.

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

### 1.1 Executable parity inventory and isolated corpus [category: test]
`kind: deliverable`

Targets:
- `tests/contracts/projects_files_inventory.json`
- `tests/contracts/projects_files_cases.json`
- `tests/contracts/test_projects_files_corpus.py`
- `docs/contracts/projects-files-hub.md`

**Research context:** The ledger is generated from registered FastAPI/Click/MCP
surfaces and shard status maps. The 35-row inventory was read from
comms-cron-hub-nodes, git-workspaces, plans-stages-projects and
websocket-chat-voice-ui. Existing route oracles are create_projects_router,
create_files_router, create_hub_files_proxy_router and create_chat_attachments_router.
Existing focused fixtures live in test_projects_routes, test_files,
test_hub_files_proxy, test_servers_routes_chat_attachments and tests/cli.
The corpus is standalone cross-language characterization infrastructure, not
a duplicate TDD leaf.

Record all 35 exact source keys, their native replacement/unchanged key,
owning section and acceptance ID. Add stable fixtures for status, JSON,
headers, bytes and resulting database/filesystem state; normalize only generated
IDs, times and temporary roots, never errors or security decisions. Capture
the current Python contract before cutover and explicitly mark the planned
namespace/layout deltas. Use inert server services and an isolated hub.
The contract specifies the new namespaces, local-versus-hub routing,
credential rules, publication/deletion states and migration refusal/retry matrix.

**Acceptance:**
- 1.1.1 - The inventory contains exactly the 35 scoped source keys and a unique acceptance owner for each; API-docs UI rows remain excluded. test: `tests/contracts/test_projects_files_corpus.py::test_inventory_is_complete`.
- 1.1.2 - Characterization cases execute the existing Python handlers and record exact response/state outcomes, with security, failure and cancellation cases and no production I/O. test: `tests/contracts/test_projects_files_corpus.py::test_python_cases_match`.
- 1.1.3 - The contract specifies namespace replacements, node-write authority, layout, migration retry states and #21592/#21576 ownership. file: `docs/contracts/projects-files-hub.md`.

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
No handler executes Python. Routing: backend code; tdd true.

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

## P2: Shared native admission
`kind: framing`

### 2.1 Hub-authoritative confined browser sessions [category: code] (depends: 1.2)
`kind: deliverable`

Targets:
- `crates/gcore/assets/schema/migrations/478_browser_admission.sql`
- `crates/gcore/assets/schema/catalog.manifest.json::*` — scope-reason: regenerate the complete schema catalog identity carrier
- `crates/gcore/src/grant/bundle.rs::*` — scope-reason: refresh embedded schema identity and grant bundle carriers together
- `crates/gcore/tests/schema_contract.rs::*` — scope-reason: schema identity and admission table contract tests share this module
- `crates/gdaemon/tests/cli_contract.rs::*` — scope-reason: refresh the schema CLI identity contract
- `src/gobby/storage/schema_expected_identity.json::*` — scope-reason: regenerate the complete Python expected schema identity carrier
- `src/gobby/storage/browser_admission.py`
- `src/gobby/servers/browser_sessions.py::*` — scope-reason: replace the process-only active-session authority while preserving the entire public session API
- `src/gobby/servers/auth_service.py::*` — scope-reason: construct the browser authority with database and process-epoch ownership
- `tests/servers/test_browser_admission.py`
- `tests/servers/test_browser_sessions.py::*` — scope-reason: supply isolated authority storage to every browser-session constructor fixture
- `crates/gdaemon/Cargo.toml`
- `crates/gcode/Cargo.toml`
- `crates/ghook/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: shared-core identity changes require coherent managed pins

**Research context:** BrowserSessions.create/get/revoke currently use a random
per-process HMAC key, a bounded active map, 3,600-second expiry and 1,024-session
ceiling. AuthService.authenticate additionally checks managed capability liveness,
loopback peer/Host and the read-only browser allowlist. Middleware binds project
and session selectors. Publishing these exact issued authorities is required;
sharing signing material or trusting decoded cookie claims is insufficient.

Add the smallest shared carrier: a process-epoch row and active browser-session
rows keyed by SHA256 of the exact cookie, with user/project/session/run claims
and expiry. A new Python authority epoch atomically invalidates predecessor
sessions; issuance, eviction and revoke update the carrier before returning.
Keep the existing token format/HMAC issuance and API signatures. A failed
carrier write refuses issuance/revocation success. No raw cookie, bearer,
signing key or secret enters the table, logs or compare details.
Native verification reads membership, expiry and current epoch from the hub.
Python death alone leaves a previously issued unexpired session verifiable;
replacement invalidates it. Use the schema toolchain for catalog/grant/CLI/Python
identity carriers. No native sign-in UI or new login protocol is required.
Migration 478 is the next observed slot; rebase it to the next free append-only
slot if another migration lands first, retaining the same contract.
Routing: backend code; tdd true.

**Granularity:** more than six production targets are the required schema and
release carriers plus one authority lifecycle, not independent features.

**Acceptance:**
- 2.1.1 - Issue/lookup/evict/revoke and epoch replacement agree with the active-session contract, and no row stores raw credentials or signing keys. test: `tests/servers/test_browser_admission.py::test_authority_lifecycle_is_atomic`.
- 2.1.2 - Carrier failures never report a successful grant or revocation; restart invalidates predecessor cookies, while backend loss alone preserves valid published admission. test: `tests/servers/test_browser_admission.py::test_failure_and_restart_are_fail_closed`.
- 2.1.3 - All required schema identity carriers and affected managed version pins regenerate consistently. test: `crates/gcore/tests/schema_contract.rs::browser_admission_schema_is_consistent`.

**Verification:** planned: the isolated-hub pytest command for
tests/servers/test_browser_admission.py; schema contract nextest and the named
version-pin test. Run generated-carrier checks, not hand-edited hashes.
Collect the exact proposed Python node IDs:
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest --collect-only -q tests/servers/test_browser_admission.py::test_authority_lifecycle_is_atomic tests/servers/test_browser_admission.py::test_failure_and_restart_are_fail_closed`.

### 2.2 Reusable native admission and confinement [category: code] (depends: 2.1, #24452)
`kind: deliverable`

Targets:
- `crates/gdaemon/src/front_door/auth.rs::*` — scope-reason: share native admission while retaining API-key header sanitization
- `crates/gdaemon/src/front_door/admission.rs`
- `crates/gdaemon/src/nodes/admission_api.rs`
- `crates/gdaemon/src/nodes/mod.rs::*` — scope-reason: export the new admission handler in the existing native node module
- `crates/gdaemon/src/front_door/mod.rs::*` — scope-reason: bind verified admission once before family dispatch
- `crates/gdaemon/tests/project_admission.rs`
- `crates/gprojects/src/family.rs`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: keep managed release pins coherent with the binaries this deliverable changes

**Research context:** AuthState::authenticate removes caller-supplied identity
headers, resolves API keys and otherwise returns without admission. Python
AuthService.authenticate/_legacy_rejection owns the remaining trust boundary;
browser_route_allowed and browser_selectors_match own confinement. The existing
gcore local_token and loopback modules supply local credential/peer checks.
The native verifier consumes 2.1's authoritative carrier, not Python RPC.
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
A read-only browser token cannot upload/delete/write, even with a valid cookie.
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
cached-authority or anonymous fallback. The endpoint is internal to this shared
admission seam and enters the parity inventory as a new delegated route.

**Acceptance:**
- 2.2.1 - API keys, local credentials and managed callers receive the same admitted/refused outcomes as Python; forged identity headers and revoked principals are refused. test: `crates/gdaemon/tests/project_admission.rs::credentials_match_python`.
- 2.2.2 - Browser expiry, epoch/revoke, peer/Host, method and every project/session selector restriction survive Python loss; absent authority fails closed. test: `crates/gdaemon/tests/project_admission.rs::browser_confinement_is_native`.
- 2.2.3 - The shared verifier protects user docs, file reads and attachments, and cookie requests stay proxied until the admission dependency is installed. test: `crates/gdaemon/tests/project_admission.rs::family_never_skips_admission`.

**Verification:** planned:
`CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-daemon --test project_admission`.

## P3: Hub documents and once-only attachment relocation
`kind: framing`

### 3.1 Descriptor-safe files_home and native USER.md [category: code] (depends: 2.2)
`kind: deliverable`

Targets:
- `crates/gprojects/src/files_home.rs`
- `crates/gprojects/src/user_document.rs`
- `crates/gprojects/tests/user_document.rs`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: keep managed release pins coherent with the binaries this deliverable changes

**Research context:** paths.require_files_home/open_files_home_descendant/publish_files_home_descendant hold a root fd and no-follow ancestor identities. hub_files_proxy._read_user_md_content enforces 6,291,470 wire bytes and 1,048,576 decoded UTF-8 bytes. Its GET/PUT handlers map missing, remote and unsupported storage distinctly. The native family uses 2.2 admission; a node uses the existing authenticated hub relay.

Port held-root, per-component no-follow/openat operations with device/inode anchors, atomic temp-file replacement and directory fsync. Return 501 with WSL guidance on unsupported native Windows; never weaken confinement to path-only checks. Serve canonical GET/PUT /api/hub/user with existing content and error semantics. Hub-only storage is selected before any open; nodes forward and create no local profile copy. Legacy paths remain untouched until 3.5's single cutover. Routing: backend code; tdd true.

**Acceptance:**
- 3.1.1 - GET's source row HTTP GET /api/files/user-md is implemented as native GET /api/hub/user, including empty/missing profiles and remote forwarding. test: `crates/gprojects/tests/user_document.rs::get_matches_python`.
- 3.1.2 - PUT's source row HTTP PUT /api/files/user-md is implemented as native PUT /api/hub/user, with wire/decoded limits, JSON validation and durable content. test: `crates/gprojects/tests/user_document.rs::put_matches_python`.
- 3.1.3 - Symlink/root/ancestor swaps, write cancellation, fsync/rename failure and unsupported platforms cannot escape the held root or falsely report committed content. test: `crates/gprojects/tests/user_document.rs::storage_refusals_preserve_bytes`.

**Verification:** planned: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-projects --test user_document`.

### 3.2 Native attachment limits and publication [category: code] (depends: 3.1)
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

### 3.3 Native attachment content and lease-safe deletion [category: code] (depends: 3.2)
`kind: deliverable`

Targets:
- `crates/gprojects/src/attachments/content.rs`
- `crates/gprojects/src/attachments/delete.rs`
- `crates/gprojects/src/attachments/repository.rs`
- `crates/gprojects/tests/attachment_content_delete.rs`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: keep managed release pins coherent with the binaries this deliverable changes

**Research context:** get_attachment_content resolves a persisted record and a descriptor-safe locator before FileResponse. delete_attachment claims an unbound row, renews the 60-second lease, unlinks bytes in a shielded worker and token-CAS deletes the row. Bound attachments return conflict. published metadata and claim ownership distinguish readable content from incomplete uploads.

Serve canonical content with byte-range, conditional GET, MIME, safe disposition and security-header parity. Stream from the already validated open descriptor to close the path-reopen race. The authorized record determines project and path; a request cannot select another project's bytes through a forged locator. Port delete claim/renew/release/token-CAS and cancellation outcomes. Missing bytes count as already unlinked; other unlink failures release ownership and retain recoverable metadata. A stale deleter cannot remove a rebound/published or newly claimed row. Routing: backend code; tdd true.

**Acceptance:**
- 3.3.1 - The source row HTTP GET /api/chat/attachments/{attachment_id}/content becomes the native hub content route with 200/206/304, missing-record/bytes and range/header parity. test: `crates/gprojects/tests/attachment_content_delete.rs::content_matches_python`.
- 3.3.2 - The source row HTTP DELETE /api/chat/attachments/{attachment_id} becomes native hub deletion with bound/conflict, missing and ok outcomes. test: `crates/gprojects/tests/attachment_content_delete.rs::delete_matches_python`.
- 3.3.3 - Project/locator substitution, root swaps, concurrent bind/delete, lease expiry and cancellation do not expose or delete unauthorized bytes or rows. test: `crates/gprojects/tests/attachment_content_delete.rs::claims_and_descriptors_are_safe`.

**Verification:** planned: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-projects --test attachment_content_delete`.

### 3.4 Native files migration and final attachment layout [category: code] (depends: 3.3)
`kind: deliverable`

Targets:
- `crates/gprojects/src/migration.rs`
- `crates/gprojects/src/migration_publish.rs`
- `crates/gprojects/src/cli_files.rs`
- `crates/gprojects/tests/files_migration.rs`
- `crates/gdaemon/src/main.rs::*` — scope-reason: expose family-owned native files commands through the existing command dispatcher
- `src/gobby/cli/files.py::*` — scope-reason: replace the whole files group with transitional native forwarding
- `tests/cli/test_native_files_forwarding.py`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: keep managed release pins coherent with the binaries this deliverable changes

**Research context:** files_migrate.run_files_migrate performs discovery, prewalk, collision/identity checks, publication, baseline seeding and locator rewrites. _discover_attachments currently targets _personal/attachments, and attachment_relative_locator confirms that live layout. files.migrate is hub-local and refuses singleton conflicts. #21576 owns comms_attachments, which is not a source here.

Port the existing migration classes into a fixed Rust operation, extending the destination once to attachments/<project-id>/. Include the existing legacy personal/project roots and already provisioned _personal/attachments, plus retained wiki bytes without recreating wiki behavior. Preserve existing non-attachment destinations and recognized retry states. Before any mutation, prewalk all sources/destinations, refuse overlaps, divergent collisions, symlinks, special entries and root/ancestor identity changes. Gate family uploads/deletes with the migration's exclusive lock; hold the daemon singleton/offline lock for offline CLI use. Publish using rename or cross-device copy/fsync/hash verification; retire source only after durable destination. Commit locator rewrites only after bytes exist. A crash after publication is reconciled by content equality on rerun; never overwrite divergent data or roll a locator forward to absent bytes. The report retains migrated/skipped/rewritten/unrewritten details and typed partial failure. The existing Click shim forwards to gdaemon files migrate and propagates stdout/stderr/exit status. Routing: backend code; tdd true.

**Granularity:** more than six production targets form one offline migration lifecycle plus its necessary entrypoints and release carriers; publication and ordinary attachment I/O are separately owned.

**Acceptance:**
- 3.4.1 - The source row CLI gobby files delegates group/help/usage handling to Rust without Python migration logic. test: `tests/cli/test_native_files_forwarding.py::test_group_delegates_to_rust`.
- 3.4.2 - The source row CLI gobby files migrate delegates to the native hub-local migration and preserves report/error/exit semantics. test: `crates/gprojects/tests/files_migration.rs::command_matches_python`.
- 3.4.3 - Preflight collision/special-type/overlap/root-swap refusals leave all sources, destinations and database locators byte-for-byte unchanged. test: `crates/gprojects/tests/files_migration.rs::preflight_has_zero_mutations`.
- 3.4.4 - Same-device, cross-device and crash/rerun states converge to one verified final locator per attachment with no lost or duplicated divergent bytes. test: `crates/gprojects/tests/files_migration.rs::rerun_converges_after_each_boundary`.
- 3.4.5 - Retained wiki bytes remain available as archived bytes, no wiki API appears, and comms_attachments is neither read nor moved. test: `crates/gprojects/tests/files_migration.rs::scope_preserves_retired_and_downstream_data`.

**Verification:** planned: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-projects --test files_migration` and isolated pytest for tests/cli/test_native_files_forwarding.py.
Collect the exact proposed Python node ID:
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest --collect-only -q tests/cli/test_native_files_forwarding.py::test_group_delegates_to_rust`.

### 3.5 Atomic namespace cutover and all current consumers [category: code] (depends: 3.4)
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
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: keep managed release pins coherent with the binaries this deliverable changes

**Research context:** The bounded literal sweeps recorded above locate the profile path constant, remote probe, Python response URL and React consumers. MessageItem validates the old attachment prefix explicitly; changing server routes alone would break rendered attachment links. chat_attachment_files is still called by transitional cleanup/binding code, so all readers/writers must move with the migration.

After migration is proven, change every listed producer/consumer and contract to the canonical hub namespaces/layout in one release. Preserve the existing relay's hop-loop guard and bounded headers/body streaming; node requests reach the owner without local files_home access. Old /api/files/user-md and /api/chat/attachments paths must receive ordinary missing-route responses from the native dispatcher even while Python remains available; they must not fall through to old Python handlers. Do not install compatibility aliases. Keep the retired wiki path missing. Rendered content_url values change at serialization, so old stored conversation metadata is normalized through the canonical serializer rather than moving bytes again. Repeat exact-literal sweeps across src, web, tests and docs and account for any new hits. No visual design changes are involved. Routing: fullstack code; tdd true.

**Granularity:** the large consumer inventory is one inseparable namespace/layout release: independently shipping a producer or consumer would create broken URLs or a split byte owner. Publication, deletion and migration are already separate leaves; this leaf owns no new state machine.

**Acceptance:**
- 3.5.1 - Every enumerated profile/upload/download/delete consumer uses the canonical hub routes; browser rendered links, restore and proxy tests remain functional. test: `tests/contracts/test_hub_namespace_cutover.py::test_all_consumers_use_hub_namespace`.
- 3.5.2 - Old route paths and retired wiki paths return normal 404, and a node without local copies reads/writes hub USER.md and attachments through the authenticated owner relay. test: `tests/contracts/test_hub_namespace_cutover.py::test_cutover_has_one_owner_and_no_aliases`.
- 3.5.3 - Current Python cleanup/binding consumers and native publication/content/deletion all resolve the same final locator after migration, with no second byte migration. test: `tests/contracts/test_hub_namespace_cutover.py::test_mixed_runtime_consumers_share_layout`.

**Verification:** planned: isolated pytest for tests/contracts/test_hub_namespace_cutover.py and the listed focused Python tests, plus the listed Vitest files using the web package's existing test command.
Collect the exact proposed Python node IDs:
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest --collect-only -q tests/contracts/test_hub_namespace_cutover.py::test_all_consumers_use_hub_namespace tests/contracts/test_hub_namespace_cutover.py::test_cutover_has_one_owner_and_no_aliases tests/contracts/test_hub_namespace_cutover.py::test_mixed_runtime_consumers_share_layout`.

## P4: Projects and local checkout browsing
`kind: framing`

### 4.1 Native project inventory, detail and cross-project MCP listing [category: code] (depends: 3.5, #24265, #24592)
`kind: deliverable`

Targets:
- `crates/gprojects/src/projects/read.rs`
- `crates/gprojects/src/projects/repository.rs`
- `crates/gprojects/src/projects/mod.rs`
- `crates/gprojects/tests/project_reads.rs`
- `src/gobby/mcp_proxy/tools/hub.py::*` — scope-reason: delegate project listing while preserving the remaining hub registry
- `tests/mcp_proxy/tools/test_native_hub_projects.py`
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

**Verification:** planned: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-projects --test project_reads` and isolated pytest for tests/mcp_proxy/tools/test_native_hub_projects.py.
Collect the exact proposed Python node ID:
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest --collect-only -q tests/mcp_proxy/tools/test_native_hub_projects.py::test_listing_delegates_to_rust`.

### 4.2 Native local file tree, text and image reads [category: code] (depends: 4.1)
`kind: deliverable`

Targets:
- `crates/gdaemon/src/front_door/mod.rs::*` — scope-reason: dispatch local checkout operations before the node hub relay
- `crates/gdaemon/src/project_services.rs`
- `crates/gprojects/src/checkout_files.rs`
- `crates/gprojects/src/checkout_paths.rs`
- `crates/gprojects/tests/checkout_reads.rs`
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

**Acceptance:**
- 4.2.1 - The source row HTTP GET /api/files/tree matches directories-first case-insensitive order, gitignore visibility and error responses. test: `crates/gprojects/tests/checkout_reads.rs::tree_matches_python`.
- 4.2.2 - The source row HTTP GET /api/files/read matches bounds, UTF-8 replacement, binary/image flags, MIME and truncation metadata. test: `crates/gprojects/tests/checkout_reads.rs::read_matches_python`.
- 4.2.3 - The source row HTTP GET /api/files/image matches content/range/cache behavior and SVG security headers. test: `crates/gprojects/tests/checkout_reads.rs::image_matches_python`.
- 4.2.4 - Traversal, symlink swaps and cross-machine checkout substitutions fail before unauthorized I/O, and node mode opens no hub datastore. test: `crates/gprojects/tests/checkout_reads.rs::locality_and_paths_are_enforced`.

**Verification:** planned: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-projects --test checkout_reads`.

### 4.3 Native checkout file writes [category: code] (depends: 4.2)
`kind: deliverable`

Targets:
- `crates/gprojects/src/checkout_write.rs`
- `crates/gprojects/src/checkout_paths.rs`
- `crates/gprojects/tests/checkout_write.rs`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: keep managed release pins coherent with the binaries this deliverable changes

**Research context:** files.write_file uses the validated local checkout, refuses .git, requires an existing parent, writes UTF-8 and returns success/size/original path. It must remain a local operation even when this daemon is a node; 4.2 establishes the node-local family branch.

Port the write response and validation/error contract, including Unicode byte size, missing parent and .git component refusal. Use the held validated parent descriptor and a durable replace to avoid traversal races and partial successful writes. Retain file mode and existing permissions; do not create parent trees or redirect to hub document storage. Failed writes preserve old bytes. Routing: backend code; tdd true.

**Acceptance:**
- 4.3.1 - The source row HTTP POST /api/files/write writes only the admitted local checkout and returns the Python success/size/path shape. test: `crates/gprojects/tests/checkout_write.rs::write_matches_python`.
- 4.3.2 - .git, traversal, replaced parents, cross-machine roots and injected publication failures leave unauthorized or predecessor bytes unchanged. test: `crates/gprojects/tests/checkout_write.rs::refusals_and_failures_preserve_bytes`.

**Verification:** planned: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-projects --test checkout_write`.

### 4.4 Native local Git status and diff [category: code] (depends: 4.3)
`kind: deliverable`

Targets:
- `crates/gprojects/src/checkout_git.rs`
- `crates/gprojects/tests/checkout_git.rs`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: keep managed release pins coherent with the binaries this deliverable changes

**Research context:** files.git_status concurrently gets branch and NUL-delimited porcelain status and maps any unavailable result to 503. git_diff validates the local relative path, invokes git with --literal-pathspecs, HEAD and --, and returns diff/path. daemon_git and parse_porcelain_v1_z are the Python oracle; no new Git service is needed.

Use an argv-only bounded subprocess through Tokio, explicit checkout cwd and timeouts, preserving arbitrary filename bytes as the existing JSON contract permits. Parse porcelain rename/copy/NUL entries faithfully. Kill/reap timed-out children; never invoke a shell or accept option/pathspec injection. Keep both endpoints local on nodes. Routing: backend code; tdd true.

**Acceptance:**
- 4.4.1 - The source row HTTP GET /api/files/git-status matches branch/status mapping, rename/untracked cases and unavailable responses. test: `crates/gprojects/tests/checkout_git.rs::status_matches_python`.
- 4.4.2 - The source row HTTP GET /api/files/git-diff matches literal file diffs, path validation, missing HEAD and subprocess failure responses. test: `crates/gprojects/tests/checkout_git.rs::diff_matches_python`.
- 4.4.3 - Hostile filenames/arguments and timeout/cancellation cannot execute a shell, escape the checkout or leave a child running. test: `crates/gprojects/tests/checkout_git.rs::subprocess_boundaries_are_safe`.

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
- 4.5.5 - The registered native POST /api/nodes/checkouts works with Python unavailable, derives machine identity from the key, refuses body/header machine forgery, and cannot be served as a local node authority. test: `crates/gdaemon/tests/node_project_checkouts.rs::registration_is_native_and_key_bound`.

**Verification:** planned: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-projects --test project_checkouts` and `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-daemon --test node_project_checkouts`.

### 4.6 Native project updates and soft deletion [category: code] (depends: 4.5)
`kind: deliverable`

Targets:
- `crates/gprojects/src/projects/update.rs`
- `crates/gprojects/src/projects/repository.rs`
- `crates/gprojects/tests/project_updates.rs`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: keep managed release pins coherent with the binaries this deliverable changes

**Research context:** ProjectUpdate uses exclude_unset, separately validates ValidationDetectionConfig, and writes approval_rules/validation_detection only under an existing local checkout. apply_update keeps database and file writes in one run_db transaction; memory fa16139d records rollback when a file write fails. delete_project protects system names and delegates LocalProjectManager.soft_delete.

Port omitted/null/empty update semantics, uniqueness/protected-name checks and validation errors. Stage validated checkout-file replacements before committing database mutation; on any file failure roll back database changes and restore previous staged files. Apply changes on the owning machine only; do not treat a remote root as local. Soft delete retains the recovery window and cleanup eligibility without hard-deleting rows or bytes. Routing: backend code; tdd true.

**Acceptance:**
- 4.6.1 - The source row HTTP PUT /api/projects/{project_id} matches update/validation/no-op payloads and checkout-scoped file writes. test: `crates/gprojects/tests/project_updates.rs::put_matches_python`.
- 4.6.2 - The source row HTTP PATCH /api/projects/{project_id} preserves omitted fields and matches partial update behavior. test: `crates/gprojects/tests/project_updates.rs::patch_matches_python`.
- 4.6.3 - The source row HTTP DELETE /api/projects/{project_id} preserves protected/not-found guards and soft-delete response/state. test: `crates/gprojects/tests/project_updates.rs::soft_delete_matches_python`.
- 4.6.4 - Database and checkout-file failures preserve the pre-update coherent state, and foreign roots are never opened. test: `crates/gprojects/tests/project_updates.rs::update_rolls_back_as_a_unit`.

**Verification:** planned: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-projects --test project_updates`.

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

**Verification:** planned: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-projects --test projects_cli` and isolated pytest for tests/cli/test_native_projects_forwarding.py.
Collect the exact proposed Python node ID:
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest --collect-only -q tests/cli/test_native_projects_forwarding.py::test_all_verbs_delegate_to_rust`.

### 5.4 Native verification-command refresh [category: code] (depends: 5.3, #21587)
`kind: deliverable`

Targets:
- `crates/gprojects/src/verification/mod.rs`
- `crates/gprojects/src/verification/evidence.rs`
- `crates/gprojects/src/verification/candidates.rs`
- `crates/gprojects/src/verification/refresh.rs`
- `crates/gprojects/src/cli_projects.rs`
- `crates/gprojects/tests/verification_refresh.rs`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: keep managed release pins coherent with the binaries this deliverable changes

**Research context:** project_verification.refresh owns deterministic/AI modes, preview versus --fix, corrupted-project backup and atomic verification-file replacement. candidates.generate_candidates/select_best_candidates/is_safe_validation_command and evidence.collect_evidence supply bounded evidence, ranking and command safety. synthesis.synthesize_verification_commands uses the daemon generation service. #21587 owns the native AI-provider route consumed here; the dependency is explicit rather than retaining Python synthesis.

Port deterministic evidence/candidate generation and safe command classification as one verification outcome. Preserve --ai auto/on/off, profile/candidate overrides, schema/rejection limits and warning/JSON/diff behavior. Use the existing gcore daemon text-generation client against #21587's native generation route; never call Python or directly bypass the daemon's AI routing policy. auto falls back deterministically with a warning; on fails if AI is unavailable; off makes no AI request. Preview changes nothing. --fix atomically replaces only verification fields, preserves unrelated metadata/permissions and backs up malformed input as the current contract requires. Never execute generated commands during refresh. Routing: backend code; tdd true.

**Granularity:** the evidence, candidate and publication modules form one command's deterministic/AI pipeline. Their shared result and atomic file owner make a single leaf appropriate despite the production-target count.

**Acceptance:**
- 5.4.1 - The source row CLI gobby projects refresh-verification delegates entirely to Rust and preserves every mode, override, result and exit behavior. test: `crates/gprojects/tests/verification_refresh.rs::command_matches_python`.
- 5.4.2 - Bounded evidence and deterministic selection match Python vectors; unsafe or unsupported AI commands are rejected, and no candidate is executed. test: `crates/gprojects/tests/verification_refresh.rs::selection_and_ai_policy_match`.
- 5.4.3 - Preview, corrupt-input backup and injected write/cancellation failures preserve unrelated metadata and recoverable predecessor bytes. test: `crates/gprojects/tests/verification_refresh.rs::publication_is_atomic`.

**Verification:** planned: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-projects --test verification_refresh`.

## P6: Native default, complete parity and operational proof
`kind: framing`

### 6.1 Read/write compare verification and native default [category: code] (depends: 5.4)
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

Complete the per-route comparison registry: read legs compare normalized response headers/body; writes run once and verify resulting DB/filesystem state or preflight error against the native contract. For legacy namespace rows, map only the path/locator delta specified in 1.1, with original outcomes retained. Upload/migration/purge verifiers never duplicate external effects or consume body streams twice. Concurrent revision/lease changes are classified as skipped with a reason and cannot count as passing coverage. Redact content and credential values from reports; mismatches identify route and structural field only. After all handlers and admission pass, default this family to Native. Explicit Proxy/Compare settings remain pre-switch rollout tools; no automatic fail-open fallback or supervised-backend mode is added. After the naming switch the completed family requires no Python. Routing: backend code; tdd true.

**Acceptance:**
- 6.1.1 - Every claimed read and mutation has a tested compare/verifier path, with writes executed exactly once and concurrent/unavailable cases counted as skipped rather than matches. test: `crates/gprojects/tests/compare.rs::every_route_has_safe_verification`.
- 6.1.2 - Default Native dispatch serves all owned principals and methods; explicit pre-switch proxy/compare selection and unknown-family fallback remain correct. test: `crates/gdaemon/tests/projects_routing.rs::native_default_is_complete`.
- 6.1.3 - Compare logs/report contain no profile content, attachment bytes, credentials or user path secrets and reject an incomplete representative route window. test: `crates/gprojects/tests/compare.rs::reports_are_bounded_and_complete`.

**Verification:** planned: `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-projects --test compare` and focused gobby-daemon projects_routing nextest.

### 6.2 Cross-surface native parity and two-machine failure proof [category: test] (depends: 6.1)
`kind: deliverable`

Targets:
- `tests/e2e/test_projects_files_family.py`
- `tests/contracts/test_projects_files_corpus.py`
- `tests/contracts/projects_files_cases.json`
- `tests/contracts/projects_files_inventory.json`

**Research context:** 1.1 records the exact 35 source rows and planned namespace/layout deltas. The family now has native auth, service, migration and CLI/MCP adapters. Existing contract/E2E isolation fixtures provide temporary hub state and backend process control. This is a standalone end-to-end parity/failure suite, not another unit-test wrapper.

Replay the complete corpus against isolated native hub and node processes. Exercise all 35 row owners, renamed replacement routes, each admitted/refused principal class, migration reruns and current consumer serializers. Kill the Python backend after issuing test browser admission and prove local reads, hub docs, attachment publication/content/deletion, project operations and native command execution still respond correctly. For refused operations, assert the native typed outcome and no side effect. Test an independent node with no copied hub bytes and no datastore credentials. Run a bounded compare window containing every required method/outcome class; zero mismatches and zero unaccounted skips are required. Pre-switch rollback restores the same binary routing setting only; migrated byte layout is never reverted. No production daemon is touched.

**Acceptance:**
- 6.2.1 - All 35 inventory rows pass their named native parity acceptance, with explicit native replacement coverage for renamed routes. test: `tests/e2e/test_projects_files_family.py::test_all_owned_rows_have_native_parity`.
- 6.2.2 - Killing isolated Python leaves API-key/local/browser-admitted native operations serving and preserves refusal/transaction semantics. test: `tests/e2e/test_projects_files_family.py::test_family_survives_python_loss`.
- 6.2.3 - A second machine without local hub bytes or DB credentials accesses hub documents and only its own local checkout, refusing cross-machine root and identity forgery. test: `tests/e2e/test_projects_files_family.py::test_two_machine_ownership_is_correct`.
- 6.2.4 - Representative compare has zero mismatches and accounted coverage; pre-switch routing rollback uses the same binaries and never remigrates bytes. test: `tests/e2e/test_projects_files_family.py::test_compare_and_routing_rollback_are_safe`.

**Verification:** planned: the isolated-hub protected pytest command for tests/contracts/test_projects_files_corpus.py and tests/e2e/test_projects_files_family.py; obtain fleet admission for E2E.
Collect the exact proposed E2E node IDs in isolated state:
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest --collect-only -q tests/e2e/test_projects_files_family.py::test_all_owned_rows_have_native_parity tests/e2e/test_projects_files_family.py::test_family_survives_python_loss tests/e2e/test_projects_files_family.py::test_two_machine_ownership_is_correct tests/e2e/test_projects_files_family.py::test_compare_and_routing_rollback_are_safe`.

### 6.3 Ledger evidence and downstream cutover contract [category: docs] (depends: 6.2)
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

**Research context:** parity-ledger.md is generated by scripts/generate_parity_ledger.py. The supported success status is delegated, not an invented native status. Removed route keys stay as dropped-by-decision evidence; other keys must identify current registrations. D1 gives this plan 35 source keys. #21592 consumes the shared attachment service, while #21576 owns comms producers and their separate byte migration.

After 6.2 passes, record delegated status with exact native symbols/tests for every surviving source key and canonical replacement. Preserve removed namespace keys as dropped by decision #24951 with their replacement and proof; retain no proxied claim for completed owned service work. Regenerate the ledger and assert no new unclassified row. Record #21592's dependency on the shared service outcome in the contract for the coordinator to wire at expansion; do not expand/edit other task trees here. Document #21576's destination/API and strict exclusion of comms byte migration from this plan. Describe the announced migration/native flip, crash recovery and pre-naming routing rollback, and the naming-switch deletion of transitional shims with no supervised Python mode.

**Acceptance:**
- 6.3.1 - Each of the 35 exact source keys has proven delegated or explicit renamed-route decision status and a native replacement/test; no scoped key is silently omitted and no unrelated shard row changes ownership. test: `tests/contracts/test_projects_files_ledger.py::test_owned_rows_are_complete`.
- 6.3.2 - Regeneration yields the checked-in ledger, canonical new routes are classified and the eight API-docs UI rows remain #21589-owned. test: `tests/contracts/test_projects_files_ledger.py::test_generated_ledger_matches_inventory`.
- 6.3.3 - The contract states #21592's shared-service dependency, #21576's comms boundary, admission reuse, once-only migration and naming-switch shim removal. file: `docs/contracts/projects-files-hub.md`.

**Verification:** planned: `uv run python scripts/generate_parity_ledger.py`, isolated protected pytest for tests/contracts/test_projects_files_ledger.py and tests/test_parity_ledger.py.
Collect the exact proposed Python node IDs:
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest --collect-only -q tests/contracts/test_projects_files_ledger.py::test_owned_rows_are_complete tests/contracts/test_projects_files_ledger.py::test_generated_ledger_matches_inventory`.

## E1 Verification and review gates
`kind: verification`

Observed during planning: ledger inventory reads yielded exactly 35 scoped rows;
source/evidence reads resolved the named existing seams. Implementation,
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

## M1 Task Manifest
`kind: manifest`

```yaml
- title: Executable parity inventory and isolated corpus
  category: test
  task_type: feature
  depends_on: []
  validation_criteria: '1.1.1: The inventory contains exactly the 35 scoped source
    keys and a unique acceptance owner for each; API-docs UI rows remain excluded.
    test: `tests/contracts/test_projects_files_corpus.py::test_inventory_is_complete`.

    1.1.2: Characterization cases execute the existing Python handlers and record
    exact response/state outcomes, with security, failure and cancellation cases and
    no production I/O. test: `tests/contracts/test_projects_files_corpus.py::test_python_cases_match`.

    1.1.3: The contract specifies namespace replacements, node-write authority, layout,
    migration retry states and #21592/#21576 ownership. file: `docs/contracts/projects-files-hub.md`.'
  labels:
  - covers:gdaemon-projects-files-docs-family:1.1:1.1.1
  - covers:gdaemon-projects-files-docs-family:1.1:1.1.2
  - covers:gdaemon-projects-files-docs-family:1.1:1.1.3
  tdd: false
  source_section: '1.1'
  assigned_agent: developer
- title: Statically linked Projects family
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  validation_criteria: '1.2.1: Proxy/native/compare dispatch, longest-prefix ownership
    and unknown-method fallback are preserved for the registered Projects family.
    test: `crates/gprojects/tests/family.rs::dispatch_is_exact`.

    1.2.2: Hub/standalone prepare the native service; node mode prepares only local
    checkout operations and the existing hub relay, with no datastore connection.
    test: `crates/gprojects/tests/family.rs::mode_assembly_is_scoped`.

    1.2.3: Extracted assembly leaves every production file below 1,000 lines and keeps
    startup behavior unchanged. test: `crates/gprojects/tests/family.rs::composition_preserves_startup`.

    1.2.4: Release versions, lockfile and managed pins match the affected binary closure.
    test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.'
  labels:
  - covers:gdaemon-projects-files-docs-family:1.2:1.2.1
  - covers:gdaemon-projects-files-docs-family:1.2:1.2.2
  - covers:gdaemon-projects-files-docs-family:1.2:1.2.3
  - covers:gdaemon-projects-files-docs-family:1.2:1.2.4
  tdd: true
  source_section: '1.2'
  implementation_domain: backend
- title: Hub-authoritative confined browser sessions
  category: code
  task_type: feature
  depends_on:
  - '1.2'
  validation_criteria: '2.1.1: Issue/lookup/evict/revoke and epoch replacement agree
    with the active-session contract, and no row stores raw credentials or signing
    keys. test: `tests/servers/test_browser_admission.py::test_authority_lifecycle_is_atomic`.

    2.1.2: Carrier failures never report a successful grant or revocation; restart
    invalidates predecessor cookies, while backend loss alone preserves valid published
    admission. test: `tests/servers/test_browser_admission.py::test_failure_and_restart_are_fail_closed`.

    2.1.3: All required schema identity carriers and affected managed version pins
    regenerate consistently. test: `crates/gcore/tests/schema_contract.rs::browser_admission_schema_is_consistent`.'
  labels:
  - covers:gdaemon-projects-files-docs-family:2.1:2.1.1
  - covers:gdaemon-projects-files-docs-family:2.1:2.1.2
  - covers:gdaemon-projects-files-docs-family:2.1:2.1.3
  tdd: true
  source_section: '2.1'
  implementation_domain: backend
- title: Reusable native admission and confinement
  category: code
  task_type: feature
  depends_on:
  - '2.1'
  validation_criteria: '2.2.1: API keys, local credentials and managed callers receive
    the same admitted/refused outcomes as Python; forged identity headers and revoked
    principals are refused. test: `crates/gdaemon/tests/project_admission.rs::credentials_match_python`.

    2.2.2: Browser expiry, epoch/revoke, peer/Host, method and every project/session
    selector restriction survive Python loss; absent authority fails closed. test:
    `crates/gdaemon/tests/project_admission.rs::browser_confinement_is_native`.

    2.2.3: The shared verifier protects user docs, file reads and attachments, and
    cookie requests stay proxied until the admission dependency is installed. test:
    `crates/gdaemon/tests/project_admission.rs::family_never_skips_admission`.'
  labels:
  - covers:gdaemon-projects-files-docs-family:2.2:2.2.1
  - covers:gdaemon-projects-files-docs-family:2.2:2.2.2
  - covers:gdaemon-projects-files-docs-family:2.2:2.2.3
  tdd: true
  source_section: '2.2'
  implementation_domain: backend
- title: Descriptor-safe files_home and native USER.md
  category: code
  task_type: feature
  depends_on:
  - '2.2'
  validation_criteria: '3.1.1: GET''s source row HTTP GET /api/files/user-md is implemented
    as native GET /api/hub/user, including empty/missing profiles and remote forwarding.
    test: `crates/gprojects/tests/user_document.rs::get_matches_python`.

    3.1.2: PUT''s source row HTTP PUT /api/files/user-md is implemented as native
    PUT /api/hub/user, with wire/decoded limits, JSON validation and durable content.
    test: `crates/gprojects/tests/user_document.rs::put_matches_python`.

    3.1.3: Symlink/root/ancestor swaps, write cancellation, fsync/rename failure and
    unsupported platforms cannot escape the held root or falsely report committed
    content. test: `crates/gprojects/tests/user_document.rs::storage_refusals_preserve_bytes`.'
  labels:
  - covers:gdaemon-projects-files-docs-family:3.1:3.1.1
  - covers:gdaemon-projects-files-docs-family:3.1:3.1.2
  - covers:gdaemon-projects-files-docs-family:3.1:3.1.3
  tdd: true
  source_section: '3.1'
  implementation_domain: backend
- title: Native attachment limits and publication
  category: code
  task_type: feature
  depends_on:
  - '3.1'
  validation_criteria: '3.2.1: The source row HTTP GET /api/chat/attachments/limits
    becomes native GET /api/hub/chat/attachments/limits and returns the current configured
    max_file_bytes with existing fallback rules. test: `crates/gprojects/tests/attachment_upload.rs::limits_match_python`.

    3.2.2: The source row HTTP POST /api/chat/attachments becomes native POST /api/hub/chat/attachments
    with equivalent metadata/limits/MIME failures and canonical content_url. test:
    `crates/gprojects/tests/attachment_upload.rs::upload_matches_python`.

    3.2.3: Injected disk, database, rename/CAS and cancellation failures preserve
    recoverable rows/bytes; concurrent cleanup cannot publish an unowned or expired
    claim. test: `crates/gprojects/tests/attachment_upload.rs::publication_is_failure_atomic`.'
  labels:
  - covers:gdaemon-projects-files-docs-family:3.2:3.2.1
  - covers:gdaemon-projects-files-docs-family:3.2:3.2.2
  - covers:gdaemon-projects-files-docs-family:3.2:3.2.3
  tdd: true
  source_section: '3.2'
  implementation_domain: backend
- title: Native attachment content and lease-safe deletion
  category: code
  task_type: feature
  depends_on:
  - '3.2'
  validation_criteria: '3.3.1: The source row HTTP GET /api/chat/attachments/{attachment_id}/content
    becomes the native hub content route with 200/206/304, missing-record/bytes and
    range/header parity. test: `crates/gprojects/tests/attachment_content_delete.rs::content_matches_python`.

    3.3.2: The source row HTTP DELETE /api/chat/attachments/{attachment_id} becomes
    native hub deletion with bound/conflict, missing and ok outcomes. test: `crates/gprojects/tests/attachment_content_delete.rs::delete_matches_python`.

    3.3.3: Project/locator substitution, root swaps, concurrent bind/delete, lease
    expiry and cancellation do not expose or delete unauthorized bytes or rows. test:
    `crates/gprojects/tests/attachment_content_delete.rs::claims_and_descriptors_are_safe`.'
  labels:
  - covers:gdaemon-projects-files-docs-family:3.3:3.3.1
  - covers:gdaemon-projects-files-docs-family:3.3:3.3.2
  - covers:gdaemon-projects-files-docs-family:3.3:3.3.3
  tdd: true
  source_section: '3.3'
  implementation_domain: backend
- title: Native files migration and final attachment layout
  category: code
  task_type: feature
  depends_on:
  - '3.3'
  validation_criteria: '3.4.1: The source row CLI gobby files delegates group/help/usage
    handling to Rust without Python migration logic. test: `tests/cli/test_native_files_forwarding.py::test_group_delegates_to_rust`.

    3.4.2: The source row CLI gobby files migrate delegates to the native hub-local
    migration and preserves report/error/exit semantics. test: `crates/gprojects/tests/files_migration.rs::command_matches_python`.

    3.4.3: Preflight collision/special-type/overlap/root-swap refusals leave all sources,
    destinations and database locators byte-for-byte unchanged. test: `crates/gprojects/tests/files_migration.rs::preflight_has_zero_mutations`.

    3.4.4: Same-device, cross-device and crash/rerun states converge to one verified
    final locator per attachment with no lost or duplicated divergent bytes. test:
    `crates/gprojects/tests/files_migration.rs::rerun_converges_after_each_boundary`.

    3.4.5: Retained wiki bytes remain available as archived bytes, no wiki API appears,
    and comms_attachments is neither read nor moved. test: `crates/gprojects/tests/files_migration.rs::scope_preserves_retired_and_downstream_data`.'
  labels:
  - covers:gdaemon-projects-files-docs-family:3.4:3.4.1
  - covers:gdaemon-projects-files-docs-family:3.4:3.4.2
  - covers:gdaemon-projects-files-docs-family:3.4:3.4.3
  - covers:gdaemon-projects-files-docs-family:3.4:3.4.4
  - covers:gdaemon-projects-files-docs-family:3.4:3.4.5
  tdd: true
  source_section: '3.4'
  implementation_domain: backend
- title: Atomic namespace cutover and all current consumers
  category: code
  task_type: feature
  depends_on:
  - '3.4'
  validation_criteria: '3.5.1: Every enumerated profile/upload/download/delete consumer
    uses the canonical hub routes; browser rendered links, restore and proxy tests
    remain functional. test: `tests/contracts/test_hub_namespace_cutover.py::test_all_consumers_use_hub_namespace`.

    3.5.2: Old route paths and retired wiki paths return normal 404, and a node without
    local copies reads/writes hub USER.md and attachments through the authenticated
    owner relay. test: `tests/contracts/test_hub_namespace_cutover.py::test_cutover_has_one_owner_and_no_aliases`.

    3.5.3: Current Python cleanup/binding consumers and native publication/content/deletion
    all resolve the same final locator after migration, with no second byte migration.
    test: `tests/contracts/test_hub_namespace_cutover.py::test_mixed_runtime_consumers_share_layout`.'
  labels:
  - covers:gdaemon-projects-files-docs-family:3.5:3.5.1
  - covers:gdaemon-projects-files-docs-family:3.5:3.5.2
  - covers:gdaemon-projects-files-docs-family:3.5:3.5.3
  tdd: true
  source_section: '3.5'
  implementation_domain: fullstack
- title: Native project inventory, detail and cross-project MCP listing
  category: code
  task_type: feature
  depends_on:
  - '3.5'
  validation_criteria: '4.1.1: The source row HTTP GET /api/projects matches listing,
    machine scope, statistics and hidden/deleted rules natively. test: `crates/gprojects/tests/project_reads.rs::list_matches_python`.

    4.1.2: The source row HTTP GET /api/projects/{project_id} matches detail and not-found
    behavior natively. test: `crates/gprojects/tests/project_reads.rs::detail_matches_python`.

    4.1.3: The source row HTTP GET /api/projects/{project_id}/checkouts matches object-or-null
    local checkout behavior. test: `crates/gprojects/tests/project_reads.rs::checkout_matches_python`.

    4.1.4: The source row HTTP GET /api/files/projects matches checkout-shaped browser
    inventory, including Personal and no repo_path. test: `crates/gprojects/tests/project_reads.rs::file_inventory_matches_python`.

    4.1.5: The source row MCP gobby-hub:list_all_projects delegates to the native
    include_system-filtered cross-project query with its existing envelope. test:
    `tests/mcp_proxy/tools/test_native_hub_projects.py::test_listing_delegates_to_rust`.'
  labels:
  - covers:gdaemon-projects-files-docs-family:4.1:4.1.1
  - covers:gdaemon-projects-files-docs-family:4.1:4.1.2
  - covers:gdaemon-projects-files-docs-family:4.1:4.1.3
  - covers:gdaemon-projects-files-docs-family:4.1:4.1.4
  - covers:gdaemon-projects-files-docs-family:4.1:4.1.5
  tdd: true
  source_section: '4.1'
  implementation_domain: backend
- title: Native local file tree, text and image reads
  category: code
  task_type: feature
  depends_on:
  - '4.1'
  validation_criteria: '4.2.1: The source row HTTP GET /api/files/tree matches directories-first
    case-insensitive order, gitignore visibility and error responses. test: `crates/gprojects/tests/checkout_reads.rs::tree_matches_python`.

    4.2.2: The source row HTTP GET /api/files/read matches bounds, UTF-8 replacement,
    binary/image flags, MIME and truncation metadata. test: `crates/gprojects/tests/checkout_reads.rs::read_matches_python`.

    4.2.3: The source row HTTP GET /api/files/image matches content/range/cache behavior
    and SVG security headers. test: `crates/gprojects/tests/checkout_reads.rs::image_matches_python`.

    4.2.4: Traversal, symlink swaps and cross-machine checkout substitutions fail
    before unauthorized I/O, and node mode opens no hub datastore. test: `crates/gprojects/tests/checkout_reads.rs::locality_and_paths_are_enforced`.'
  labels:
  - covers:gdaemon-projects-files-docs-family:4.2:4.2.1
  - covers:gdaemon-projects-files-docs-family:4.2:4.2.2
  - covers:gdaemon-projects-files-docs-family:4.2:4.2.3
  - covers:gdaemon-projects-files-docs-family:4.2:4.2.4
  tdd: true
  source_section: '4.2'
  implementation_domain: backend
- title: Native checkout file writes
  category: code
  task_type: feature
  depends_on:
  - '4.2'
  validation_criteria: '4.3.1: The source row HTTP POST /api/files/write writes only
    the admitted local checkout and returns the Python success/size/path shape. test:
    `crates/gprojects/tests/checkout_write.rs::write_matches_python`.

    4.3.2: .git, traversal, replaced parents, cross-machine roots and injected publication
    failures leave unauthorized or predecessor bytes unchanged. test: `crates/gprojects/tests/checkout_write.rs::refusals_and_failures_preserve_bytes`.'
  labels:
  - covers:gdaemon-projects-files-docs-family:4.3:4.3.1
  - covers:gdaemon-projects-files-docs-family:4.3:4.3.2
  tdd: true
  source_section: '4.3'
  implementation_domain: backend
- title: Native local Git status and diff
  category: code
  task_type: feature
  depends_on:
  - '4.3'
  validation_criteria: '4.4.1: The source row HTTP GET /api/files/git-status matches
    branch/status mapping, rename/untracked cases and unavailable responses. test:
    `crates/gprojects/tests/checkout_git.rs::status_matches_python`.

    4.4.2: The source row HTTP GET /api/files/git-diff matches literal file diffs,
    path validation, missing HEAD and subprocess failure responses. test: `crates/gprojects/tests/checkout_git.rs::diff_matches_python`.

    4.4.3: Hostile filenames/arguments and timeout/cancellation cannot execute a shell,
    escape the checkout or leave a child running. test: `crates/gprojects/tests/checkout_git.rs::subprocess_boundaries_are_safe`.'
  labels:
  - covers:gdaemon-projects-files-docs-family:4.4:4.4.1
  - covers:gdaemon-projects-files-docs-family:4.4:4.4.2
  - covers:gdaemon-projects-files-docs-family:4.4:4.4.3
  tdd: true
  source_section: '4.4'
  implementation_domain: backend
- title: Native project initialization and checkout mutation
  category: code
  task_type: feature
  depends_on:
  - '4.4'
  validation_criteria: '4.5.1: The source row HTTP POST /api/projects/init initializes/reattaches
    with Python''s identity/marker/checkout behavior and event rule. test: `crates/gprojects/tests/project_checkouts.rs::init_matches_python`.

    4.5.2: The source row HTTP POST /api/projects/{project_id}/checkouts gives 201
    insert/200 retry and matching conflicts without restoring a deleted project. test:
    `crates/gprojects/tests/project_checkouts.rs::register_matches_python`.

    4.5.3: The source row HTTP POST /api/projects/{project_id}/checkouts/{machine_id}/rebind
    preserves machine scope, validation, index invalidation and event semantics. test:
    `crates/gprojects/tests/project_checkouts.rs::rebind_matches_python`.

    4.5.4: Node-key authority writes only its own machine rows, and marker/root/overlay
    conflicts or transaction failure leave no partial checkout or event. test: `crates/gprojects/tests/project_checkouts.rs::machine_authority_and_atomicity_hold`.

    4.5.5: The registered native POST /api/nodes/checkouts works with Python unavailable,
    derives machine identity from the key, refuses body/header machine forgery, and
    cannot be served as a local node authority. test: `crates/gdaemon/tests/node_project_checkouts.rs::registration_is_native_and_key_bound`.'
  labels:
  - covers:gdaemon-projects-files-docs-family:4.5:4.5.1
  - covers:gdaemon-projects-files-docs-family:4.5:4.5.2
  - covers:gdaemon-projects-files-docs-family:4.5:4.5.3
  - covers:gdaemon-projects-files-docs-family:4.5:4.5.4
  - covers:gdaemon-projects-files-docs-family:4.5:4.5.5
  tdd: true
  source_section: '4.5'
  implementation_domain: backend
- title: Native project updates and soft deletion
  category: code
  task_type: feature
  depends_on:
  - '4.5'
  validation_criteria: '4.6.1: The source row HTTP PUT /api/projects/{project_id}
    matches update/validation/no-op payloads and checkout-scoped file writes. test:
    `crates/gprojects/tests/project_updates.rs::put_matches_python`.

    4.6.2: The source row HTTP PATCH /api/projects/{project_id} preserves omitted
    fields and matches partial update behavior. test: `crates/gprojects/tests/project_updates.rs::patch_matches_python`.

    4.6.3: The source row HTTP DELETE /api/projects/{project_id} preserves protected/not-found
    guards and soft-delete response/state. test: `crates/gprojects/tests/project_updates.rs::soft_delete_matches_python`.

    4.6.4: Database and checkout-file failures preserve the pre-update coherent state,
    and foreign roots are never opened. test: `crates/gprojects/tests/project_updates.rs::update_rolls_back_as_a_unit`.'
  labels:
  - covers:gdaemon-projects-files-docs-family:4.6:4.6.1
  - covers:gdaemon-projects-files-docs-family:4.6:4.6.2
  - covers:gdaemon-projects-files-docs-family:4.6:4.6.3
  - covers:gdaemon-projects-files-docs-family:4.6:4.6.4
  tdd: true
  source_section: '4.6'
  implementation_domain: backend
- title: Shared cross-runtime project write fence
  category: code
  task_type: feature
  depends_on:
  - '4.6'
  validation_criteria: '5.1.1: A Python writer blocks Rust purge and a Rust writer
    blocks Python purge until drain; deleted/absent projects refuse new writers. test:
    `tests/projects/test_cross_runtime_write_fence.py::test_both_runtimes_share_exclusion`.

    5.1.2: Reentrancy, global writers, concurrent purge, timeout/cancellation and
    dropped connections preserve safety without leaked locks or pool contamination.
    test: `crates/gcore/tests/project_fence.rs::lock_lifecycle_is_safe`.

    5.1.3: Required shared-core binary bumps, maintenance floor, lockfile and managed
    pins agree. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

    5.1.4: With the minimum two-slot operation pool, concurrent fenced writers, a
    waiting purge, nested ownership, resize and cancellation make bounded progress
    without starving ordinary queries or leaking permits/connections. test: `crates/gcore/tests/project_fence.rs::small_pool_progress_is_bounded`.

    5.1.5: The Python pool uses the same reserve and releases capacity on failure/cancellation;
    concurrent writers and purge finish under a bounded saturation test. test: `tests/projects/test_cross_runtime_write_fence.py::test_small_pool_progress_is_bounded`.'
  labels:
  - covers:gdaemon-projects-files-docs-family:5.1:5.1.1
  - covers:gdaemon-projects-files-docs-family:5.1:5.1.2
  - covers:gdaemon-projects-files-docs-family:5.1:5.1.3
  - covers:gdaemon-projects-files-docs-family:5.1:5.1.4
  - covers:gdaemon-projects-files-docs-family:5.1:5.1.5
  tdd: true
  source_section: '5.1'
  implementation_domain: backend
- title: Native lifecycle-safe hard purge
  category: code
  task_type: feature
  depends_on:
  - '5.1'
  validation_criteria: '5.2.1: The source row HTTP POST /api/projects/{project_id}/purge
    matches purged/not_found/protected/failed statuses and dependent-resource cleanup
    without Python. test: `crates/gprojects/tests/project_purge.rs::purge_matches_python`.

    5.2.2: Active terminals, draining cron, derived writers and concurrent terminal
    insertion cannot race a destructive purge; timeouts leave retryable state. test:
    `crates/gprojects/tests/project_purge.rs::lifecycle_guards_hold`.

    5.2.3: Injected code/vector/graph/database failures and retries preserve tombstone/detach
    ordering and never delete another project''s resources or source checkout. test:
    `crates/gprojects/tests/project_purge.rs::failures_are_retryable_and_scoped`.

    5.2.4: A real isolated gcode child invalidates the intended soft-deleted and indexed-only
    targets through fresh project-scoped maintenance authority with Python down and
    no interactive grant; foreign project/machine claims are refused. test: `crates/gprojects/tests/project_purge.rs::maintenance_child_authority_is_scoped`.

    5.2.5: Success, spawn failure, child failure, timeout and cancellation revoke
    the invocation and remove private launch secrets; injected revoke failure records
    the native retry and prevents purge success or hub-row deletion. test: `crates/gprojects/tests/project_purge.rs::maintenance_child_teardown_is_bounded`.'
  labels:
  - covers:gdaemon-projects-files-docs-family:5.2:5.2.1
  - covers:gdaemon-projects-files-docs-family:5.2:5.2.2
  - covers:gdaemon-projects-files-docs-family:5.2:5.2.3
  - covers:gdaemon-projects-files-docs-family:5.2:5.2.4
  - covers:gdaemon-projects-files-docs-family:5.2:5.2.5
  tdd: true
  source_section: '5.2'
  implementation_domain: backend
- title: Native Projects command group and ordinary verbs
  category: code
  task_type: feature
  depends_on:
  - '5.2'
  validation_criteria: '5.3.1: The source row CLI gobby projects delegates help/group/usage
    to Rust with matching exit behavior. test: `crates/gprojects/tests/projects_cli.rs::group_usage_matches`.

    5.3.2: The source row CLI gobby projects list matches --all/--json filtering and
    text/JSON rendering. test: `crates/gprojects/tests/projects_cli.rs::list_matches_python`.

    5.3.3: The source row CLI gobby projects show resolves names/IDs and matches detail/error
    rendering. test: `crates/gprojects/tests/projects_cli.rs::show_matches_python`.

    5.3.4: The source row CLI gobby projects rename preserves uniqueness/sentinel
    rules and output. test: `crates/gprojects/tests/projects_cli.rs::rename_matches_python`.

    5.3.5: The source row CLI gobby projects delete preserves exact-name confirmation/protection
    and delegates soft deletion. test: `crates/gprojects/tests/projects_cli.rs::delete_matches_python`.

    5.3.6: The source row CLI gobby projects purge preserves confirmation and native
    purge outcomes. test: `crates/gprojects/tests/projects_cli.rs::purge_matches_python`.

    5.3.7: The source row CLI gobby projects rebind validates local root/marker, uses
    key-bound machine authority and retains the cache hint. test: `crates/gprojects/tests/projects_cli.rs::rebind_matches_python`.

    5.3.8: The source row CLI gobby projects repair preserves dry-run/fix/no-drift/conflict
    outcomes and never silently rebinds. test: `crates/gprojects/tests/projects_cli.rs::repair_matches_python`.

    5.3.9: The Click shims forward argv/stdout/stderr/exit status to Rust and execute
    no Python project business logic. test: `tests/cli/test_native_projects_forwarding.py::test_all_verbs_delegate_to_rust`.'
  labels:
  - covers:gdaemon-projects-files-docs-family:5.3:5.3.1
  - covers:gdaemon-projects-files-docs-family:5.3:5.3.2
  - covers:gdaemon-projects-files-docs-family:5.3:5.3.3
  - covers:gdaemon-projects-files-docs-family:5.3:5.3.4
  - covers:gdaemon-projects-files-docs-family:5.3:5.3.5
  - covers:gdaemon-projects-files-docs-family:5.3:5.3.6
  - covers:gdaemon-projects-files-docs-family:5.3:5.3.7
  - covers:gdaemon-projects-files-docs-family:5.3:5.3.8
  - covers:gdaemon-projects-files-docs-family:5.3:5.3.9
  tdd: true
  source_section: '5.3'
  implementation_domain: backend
- title: Native verification-command refresh
  category: code
  task_type: feature
  depends_on:
  - '5.3'
  validation_criteria: '5.4.1: The source row CLI gobby projects refresh-verification
    delegates entirely to Rust and preserves every mode, override, result and exit
    behavior. test: `crates/gprojects/tests/verification_refresh.rs::command_matches_python`.

    5.4.2: Bounded evidence and deterministic selection match Python vectors; unsafe
    or unsupported AI commands are rejected, and no candidate is executed. test: `crates/gprojects/tests/verification_refresh.rs::selection_and_ai_policy_match`.

    5.4.3: Preview, corrupt-input backup and injected write/cancellation failures
    preserve unrelated metadata and recoverable predecessor bytes. test: `crates/gprojects/tests/verification_refresh.rs::publication_is_atomic`.'
  labels:
  - covers:gdaemon-projects-files-docs-family:5.4:5.4.1
  - covers:gdaemon-projects-files-docs-family:5.4:5.4.2
  - covers:gdaemon-projects-files-docs-family:5.4:5.4.3
  tdd: true
  source_section: '5.4'
  implementation_domain: backend
- title: Read/write compare verification and native default
  category: code
  task_type: feature
  depends_on:
  - '5.4'
  validation_criteria: '6.1.1: Every claimed read and mutation has a tested compare/verifier
    path, with writes executed exactly once and concurrent/unavailable cases counted
    as skipped rather than matches. test: `crates/gprojects/tests/compare.rs::every_route_has_safe_verification`.

    6.1.2: Default Native dispatch serves all owned principals and methods; explicit
    pre-switch proxy/compare selection and unknown-family fallback remain correct.
    test: `crates/gdaemon/tests/projects_routing.rs::native_default_is_complete`.

    6.1.3: Compare logs/report contain no profile content, attachment bytes, credentials
    or user path secrets and reject an incomplete representative route window. test:
    `crates/gprojects/tests/compare.rs::reports_are_bounded_and_complete`.'
  labels:
  - covers:gdaemon-projects-files-docs-family:6.1:6.1.1
  - covers:gdaemon-projects-files-docs-family:6.1:6.1.2
  - covers:gdaemon-projects-files-docs-family:6.1:6.1.3
  tdd: true
  source_section: '6.1'
  implementation_domain: backend
- title: Cross-surface native parity and two-machine failure proof
  category: test
  task_type: feature
  depends_on:
  - '6.1'
  validation_criteria: '6.2.1: All 35 inventory rows pass their named native parity
    acceptance, with explicit native replacement coverage for renamed routes. test:
    `tests/e2e/test_projects_files_family.py::test_all_owned_rows_have_native_parity`.

    6.2.2: Killing isolated Python leaves API-key/local/browser-admitted native operations
    serving and preserves refusal/transaction semantics. test: `tests/e2e/test_projects_files_family.py::test_family_survives_python_loss`.

    6.2.3: A second machine without local hub bytes or DB credentials accesses hub
    documents and only its own local checkout, refusing cross-machine root and identity
    forgery. test: `tests/e2e/test_projects_files_family.py::test_two_machine_ownership_is_correct`.

    6.2.4: Representative compare has zero mismatches and accounted coverage; pre-switch
    routing rollback uses the same binaries and never remigrates bytes. test: `tests/e2e/test_projects_files_family.py::test_compare_and_routing_rollback_are_safe`.'
  labels:
  - covers:gdaemon-projects-files-docs-family:6.2:6.2.1
  - covers:gdaemon-projects-files-docs-family:6.2:6.2.2
  - covers:gdaemon-projects-files-docs-family:6.2:6.2.3
  - covers:gdaemon-projects-files-docs-family:6.2:6.2.4
  tdd: false
  source_section: '6.2'
  assigned_agent: developer
- title: Ledger evidence and downstream cutover contract
  category: docs
  task_type: docs
  depends_on:
  - '6.2'
  validation_criteria: '6.3.1: Each of the 35 exact source keys has proven delegated
    or explicit renamed-route decision status and a native replacement/test; no scoped
    key is silently omitted and no unrelated shard row changes ownership. test: `tests/contracts/test_projects_files_ledger.py::test_owned_rows_are_complete`.

    6.3.2: Regeneration yields the checked-in ledger, canonical new routes are classified
    and the eight API-docs UI rows remain #21589-owned. test: `tests/contracts/test_projects_files_ledger.py::test_generated_ledger_matches_inventory`.

    6.3.3: The contract states #21592''s shared-service dependency, #21576''s comms
    boundary, admission reuse, once-only migration and naming-switch shim removal.
    file: `docs/contracts/projects-files-hub.md`.'
  labels:
  - covers:gdaemon-projects-files-docs-family:6.3:6.3.1
  - covers:gdaemon-projects-files-docs-family:6.3:6.3.2
  - covers:gdaemon-projects-files-docs-family:6.3:6.3.3
  tdd: false
  source_section: '6.3'
  assigned_agent: tech-writer
```
