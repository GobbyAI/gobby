Plan artifact: `.gobby/plans/gdaemon-web-session-cookie.md`

# Native browser authentication for the code graph

**Plan ID:** gdaemon-web-session-cookie

**Task:** #24659 Cookie plan. **Status:** fresh narrative draft, 2026-10-20;
awaiting fresh Security review, Plan Adversary consensus and Josh approval.
No historical stamp or approval applies to this revision. No expansion is authorized.

## Overview
`kind: framing`

A browser signed in with `gobby_session` must load the five code-graph reads through
hub gdaemon. Rust owns verification and route admission; TypeScript proves the real
browser flow. After the native proof passes, delete the Python routes and their dead
context/gateway methods, and reconcile the parity ledger and reference audit.
This is necessary: the current front door resolves API keys but no browser cookie.

This replaces the historical Python-oriented draft from scratch. It retains the
three explicit obligations from code-index-family D15/former 9.8: delete the five
Python reads and context methods (9.8.1); delete the unused gateway methods and two
version floors plus maintenance-floor wording (9.8.2); move the five ledger entries
out of proxied and point the five audit entries to native handlers (9.8.3).

## Constraints
`kind: framing`

- Planning is authorized now by Orchestrator gobby#17164, message
  `8d65292e-775d-4eb2-b8dc-9b5dc55719bf` (2026-10-20). Hub-node completion remains an
  implementation and expansion prerequisite. #21575 Hub mode remains open: P2
  #24266, P3 #24267, P4 #24592 and P5 #24593 were open at inspection. P1 #24265 is
  closed. No statement in this plan claims those remaining phases completed.
- The coordinator must verify #21575 and its required phases have landed before
  expansion. Phase P1 also carries #21575 as an enforceable external dependency.
- Family prerequisites remain open: #24665 P5 family seam, #24666 P6 graph reads,
  #24694 native search, #24710 Python mutation/path retirement and #24670 version
  floor extraction. The headings retain these external dependencies.
- No new Python runtime, tests, generators or helpers. Python edits are deletion-only,
  except minimal literal `MANAGED_BIN_VERSION_PINS` synchronization permitted by the
  Orchestrator's ruling above. Existing Python validation/generation commands may run.
- No new schema, session issuer, login UX, cookie name, cookie flags or auth migration.
  Existing login/logout remain authoritative. This plan only reads `auth_sessions`.
- Each commit changing a shipped Rust binary or its recursive dependencies bumps every
  affected shipped binary by 0.0.1, updates Cargo.lock and synchronizes existing pins.
  In this plan gdaemon depends on gruntime; gruntime is not independently shipped.
  Re-evaluate the recursive dependency graph at implementation tip. No fixed future
  version numbers. Promotion/restart/cutover belongs to Orchestrator alone.
- Production files remain below 1,000 lines. Prefer a focused new Rust cookie module
  and separate test files. No security feature is weakened to reduce line count.
- Targets for prerequisite-created files are new relative to this checkout. Before
  expansion the coordinator refreshes symbol scopes and the deletion proof against
  the landed prerequisites, validates again and obtains fresh reviews for changed
  bytes. The current draft's hash never certifies a later artifact.

## Decision Record
`kind: framing`

These are the proposed decisions for fresh Security, adversary and Josh approval;
historical accepted-a-c receipts are not approval of this design.

1. An unexpired session user may read exactly five GET routes: graph overview, file,
   symbol neighbors, blast radius and search. Preserve today's project visibility
   and payload rules; add no user-specific authorization model in this migration.
2. Only the hub/standalone front door queries its own PostgreSQL pool. Node doors
   relay the request to the hub; node-local cookie verification and credential
   forwarding through newly invented identity headers are excluded.
3. Scope verification to an exact method/path allowlist shared with the five native
   family rows. Proxied routes, mutations, lease admin, machines, node channel,
   runtime grants and browser WebSockets retain their existing auth contracts.
4. On the five reads, presence of Authorization selects the existing bearer path
   alone. Never fall back to a cookie after invalid, agent or non-bearer credentials.
   Absent Authorization, a valid session is required for browser admission.
5. Collect all Cookie header fields and all case-sensitive `gobby_session` pairs.
   Reject distinct duplicates, empty values, malformed session pairs and non-UTF8
   cookie headers on these five paths with 401 `missing_auth`; equal repeated values
   count as one. Do not URL-decode, unquote or trim token bytes beyond cookie-pair
   separator whitespace. Never log cookie bytes, bearer bytes or token hashes.
6. Query by lowercase SHA-256 token hash with `expires_at >= CURRENT_TIMESTAMP`.
   Query per request, with no positive or negative cache; logout/deletion and expiry
   affect the next request. Unknown/expired credentials yield 401. Missing resolver,
   pool acquisition failure, query failure or bounded timeout yields 503
   `auth_resolver_unavailable`, never Python fallback or an empty successful graph.
7. Carry `BrowserSession { user_id }` as a typed request extension. Always strip
   client identity headers before authentication. No cookie-derived identity header
   is sent to Python. Only five explicitly opted-in rows admit Caller::Browser;
   Caller::ALL continues to mean existing callers rather than widening all routes.
8. Require proof through the actual front door, through a relaying node, and through
   the TypeScript graph consumer before deletion. New tests are Rust/TypeScript.
   No production graph UI change is necessary: same-origin fetch already sends the
   browser cookie. Do not add a JavaScript-readable token or Authorization header.
9. Native claims deliberately exclude legacy break-glass/agent-token access to these
   five reads after Python retirement, matching family D15. They receive 401 on the
   five reads; unrelated routes keep their existing behavior. Security and Josh must
   approve this explicit caller loss with the rest of the plan.

## P1: Native browser admission (depends: #21575)
`kind: framing`

### 1.1 Verify the cookie only for the five graph reads [category: code] (depends: #24694, #24666)
`kind: deliverable`

Targets:
- `crates/gdaemon/src/front_door/auth.rs::*` — scope-reason: extend the existing resolver and request authentication boundary without changing key behavior.
- `crates/gdaemon/src/front_door/cookie.rs`
- `crates/gdaemon/src/front_door/mod.rs::FrontDoor::handle`
- `crates/gruntime/src/lib.rs::*` — scope-reason: declare the typed BrowserSession extension shared by front door and family.
- `crates/gcodeindex/src/family.rs`
- `crates/gdaemon/tests/browser_cookie.rs`
- `crates/gdaemon/tests/front_door.rs::*` — scope-reason: implement the required session method in FixedResolver and FailedResolver test doubles.
- `crates/gdaemon/tests/lease.rs::*` — scope-reason: implement the required session method in StandbyKeyResolver without granting browser lease access.
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::MANAGED_BIN_VERSION_PINS`

**Research context:** At the inspected checkout, AuthState::authenticate in
`crates/gdaemon/src/front_door/auth.rs` strips identity headers and returns immediately
without Authorization. `PostgresKeyResolver::resolve` owns a shared pool and an
existing five-second query timeout; reuse that pool and error response helpers.
Its session method is new and returns a typed optional user, separate from API-key
machine/key identity. Add a required resolver method and update Rust test doubles;
do not install a default silently masking missing session support.

`FrontDoor::handle` relays Node requests before backend authentication. Its backend
branch authenticates lease admin separately and ordinary requests before dispatch.
Keep lease-admin's call credential behavior unchanged. The ordinary call accepts a
mutable request and an allow-browser decision from the family allowlist. The new
cookie module owns parsing/hash/query logic; the front door owns HTTP decisions.
Repair and reuse the existing gruntime path_matches function: its current wildcard
branch consumes the first actual segment in the tuple match, then checks only later
segments. Count that consumed segment when deciding whether the remainder is
nonempty. This fixes flat files and hook-generated encoded nested paths without
adding a second matcher. Use this same corrected matcher for the auth predicate and
family claims. Cover flat a.rs, encoded src%2Fa.rs, literal nested src/a.rs and an
empty remainder; reject the empty remainder. Preserve the family's existing path
normalization and exact method matching.
`crates/gcodeindex/src/family.rs` is created by #24694 and contains NATIVE_ROUTES.
Expose a read-only browser-read predicate there; do not duplicate five path strings
in gdaemon. The predicate excludes GET paths that do not match a row and all HEAD,
OPTIONS and mutation methods. Version/pin changes ship with this source change.

The SQL query is `SELECT user_id::text FROM auth_sessions WHERE token_hash = $1 AND
expires_at >= CURRENT_TIMESTAMP`. Bound pool acquisition and query together to five
seconds. The hash uses `gobby_core::api_key_format::hash`; session tokens are opaque,
so do not pass them through API-key parsing. Existing `AuthStore.session_user_id`
in `src/gobby/storage/auth.py` is the compatibility reference, not an edit target.
Existing session row deletion is the revocation mechanism. The typed extension is
in-process only; clear any prior BrowserSession before authentication.

**Granularity:** seven acceptance items and more than six production Targets;
verifier, its request boundary,
shared extension/allowlist and release carriers must ship atomically. Route claim
activation is independently testable and stays in 1.2. No second session lifecycle
is introduced; the existing issuer/logout retain ownership.

Planned verification: `CARGO_BUILD_JOBS=3 cargo test -p gobby-daemon --test browser_cookie`.
Also run `CARGO_BUILD_JOBS=3 cargo test -p gobby-runtime browser_file_remainder_matches_hook_urls`.
Run database cases only against isolated fixture schemas in the test hub; a missing
test database is a failure for the acceptance run. Exact row-expiry boundary,
logout, malformed headers and resolver failure are table-driven Rust cases.

Consumers unchanged:
- `src/gobby/cli/install_setup_gclient.py` — no-edit-reason: reads the managed pin by key or compares it to its crate manifest; literal synchronization flows through without changing this consumer.
- `src/gobby/cli/install_setup_gcode.py` — no-edit-reason: reads the managed pin by key or compares it to its crate manifest; literal synchronization flows through without changing this consumer.
- `src/gobby/cli/install_setup_gdaemon.py` — no-edit-reason: reads the managed pin by key or compares it to its crate manifest; literal synchronization flows through without changing this consumer.
- `src/gobby/cli/install_setup_ghook.py` — no-edit-reason: reads the managed pin by key or compares it to its crate manifest; literal synchronization flows through without changing this consumer.
- `src/gobby/cli/install_setup_gmcp.py` — no-edit-reason: reads the managed pin by key or compares it to its crate manifest; literal synchronization flows through without changing this consumer.
- `src/gobby/cli/install_setup_gterm.py` — no-edit-reason: reads the managed pin by key or compares it to its crate manifest; literal synchronization flows through without changing this consumer.
- `src/gobby/cli/install_setup_versions.py` — no-edit-reason: reads the managed pin by key or compares it to its crate manifest; literal synchronization flows through without changing this consumer.
- `src/gobby/hooks/runtime_compat.py` — no-edit-reason: reads the managed pin by key or compares it to its crate manifest; literal synchronization flows through without changing this consumer.
- `src/gobby/install/bin_freshness_models.py` — no-edit-reason: reads the managed pin by key or compares it to its crate manifest; literal synchronization flows through without changing this consumer.
- `src/gobby/install/distribution.py` — no-edit-reason: reads the managed pin by key or compares it to its crate manifest; literal synchronization flows through without changing this consumer.
- `tests/cli/installers/test_grok_installer.py` — no-edit-reason: reads the managed pin by key or compares it to its crate manifest; literal synchronization flows through without changing this consumer.
- `tests/cli/test_install_ghook.py` — no-edit-reason: reads the managed pin by key or compares it to its crate manifest; literal synchronization flows through without changing this consumer.
- `tests/cli/test_install_setup.py` — no-edit-reason: reads the managed pin by key or compares it to its crate manifest; literal synchronization flows through without changing this consumer.
- `tests/cli/test_install_setup_gclient.py` — no-edit-reason: reads the managed pin by key or compares it to its crate manifest; literal synchronization flows through without changing this consumer.
- `tests/cli/test_install_setup_gdaemon.py` — no-edit-reason: reads the managed pin by key or compares it to its crate manifest; literal synchronization flows through without changing this consumer.
- `tests/cli/test_install_setup_gterm.py` — no-edit-reason: reads the managed pin by key or compares it to its crate manifest; literal synchronization flows through without changing this consumer.
- `tests/hooks/test_runtime_compat.py` — no-edit-reason: reads the managed pin by key or compares it to its crate manifest; literal synchronization flows through without changing this consumer.
- `tests/install/test_distribution.py` — no-edit-reason: reads the managed pin by key or compares it to its crate manifest; literal synchronization flows through without changing this consumer.
- `tests/install/test_version_pins.py` — no-edit-reason: reads the managed pin by key or compares it to its crate manifest; literal synchronization flows through without changing this consumer.

**Acceptance:**
- 1.1.1 - Valid cookies insert only the verified user's BrowserSession; absent, unknown, expired, deleted and malformed credentials cannot insert it. test: `crates/gdaemon/tests/browser_cookie.rs::session_verification_and_revocation`.
- 1.1.2 - All header fields and duplicate pairs obey Decision 5, including equal duplicates and distinct duplicates split across header fields. test: `crates/gdaemon/tests/browser_cookie.rs::cookie_ambiguity_fails_closed`.
- 1.1.3 - Authorization takes precedence; cookie-only requests never gain key/machine identity or access to other route classes; spoofed identity headers are removed. test: `crates/gdaemon/tests/browser_cookie.rs::credentials_do_not_cross_auth_boundaries`.
- 1.1.4 - Database failures and five-second total resolution timeout return 503 with no native handler or Python fallback; invalid cookies on the five reads return 401. test: `crates/gdaemon/tests/browser_cookie.rs::resolver_failure_is_unavailable`.
- 1.1.5 - Hash fixture, SQL expiry boundary and absence of cache match the documented contract. test: `crates/gdaemon/tests/browser_cookie.rs::postgres_cookie_contract`.
- 1.1.6 - Affected shipped Rust version, lockfile and existing pin advance together. file: `crates/gdaemon/Cargo.toml`, `Cargo.lock`, `src/gobby/install/version_pins.py`.
- 1.1.7 - The shared path_matches matcher counts the consumed wildcard remainder: a.rs, src%2Fa.rs and src/a.rs match the file row, empty remainder does not; auth predicate and family claims agree. test: `crates/gruntime/src/lib.rs::browser_file_remainder_matches_hook_urls`.

### 1.2 Claim cookie callers on exactly five native rows [category: code] (depends: 1.1, #24665, #24666)
`kind: deliverable`

Targets:
- `crates/gruntime/src/lib.rs::*` — scope-reason: add Caller::Browser classification without widening other native rows.
- `crates/gdaemon/src/front_door/mod.rs::FrontDoor::handle`
- `crates/gcodeindex/src/family.rs`
- `crates/gcodeindex/tests/family_browser.rs`
- `crates/gdaemon/tests/browser_cookie.rs`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::MANAGED_BIN_VERSION_PINS`

**Research context:** Existing gruntime Caller::of recognizes x-gobby-key-id only,
and RuntimeFamily::claims matches row method, path, caller and gate. #24694 extracts
claims_native for code_index reuse and creates operator-only family rows; #24666
adds native graph reads. Classify Browser only from the verified extension after
Operator precedence. Add Browser explicitly on overview, file, neighbors,
blast-radius and search rows; keep every other row's caller list unchanged.
Use the five-row predicate from 1.1 to reject unauthenticated/unsupported callers
with 401 before dispatch, avoiding claims=false proxy fallback on the retired reads.
The shared allowlist is the sole admission inventory. Keep gate evaluation,
project/machine visibility and all handler payloads unchanged. Existing API-key
requests retain the family contract. Changing gruntime affects gdaemon, so release
bookkeeping belongs in this commit too.

Planned verification: `CARGO_BUILD_JOBS=3 cargo test -p gobby-codeindex --test family_browser`
and the browser_cookie test binary. Tests compose real FrontDoor + CodeIndexFamily
with a backend stub counting calls: zero for native successes and auth refusals.

**Granularity:** seven production Targets are coupled by shared caller/row activation
and required release metadata; the same cookie lifecycle is owned by 1.1.

Consumers unchanged:
- `src/gobby/cli/install_setup_gclient.py` — no-edit-reason: reads the managed pin by key or compares it to its crate manifest; literal synchronization flows through without changing this consumer.
- `src/gobby/cli/install_setup_gcode.py` — no-edit-reason: reads the managed pin by key or compares it to its crate manifest; literal synchronization flows through without changing this consumer.
- `src/gobby/cli/install_setup_gdaemon.py` — no-edit-reason: reads the managed pin by key or compares it to its crate manifest; literal synchronization flows through without changing this consumer.
- `src/gobby/cli/install_setup_ghook.py` — no-edit-reason: reads the managed pin by key or compares it to its crate manifest; literal synchronization flows through without changing this consumer.
- `src/gobby/cli/install_setup_gmcp.py` — no-edit-reason: reads the managed pin by key or compares it to its crate manifest; literal synchronization flows through without changing this consumer.
- `src/gobby/cli/install_setup_gterm.py` — no-edit-reason: reads the managed pin by key or compares it to its crate manifest; literal synchronization flows through without changing this consumer.
- `src/gobby/cli/install_setup_versions.py` — no-edit-reason: reads the managed pin by key or compares it to its crate manifest; literal synchronization flows through without changing this consumer.
- `src/gobby/hooks/runtime_compat.py` — no-edit-reason: reads the managed pin by key or compares it to its crate manifest; literal synchronization flows through without changing this consumer.
- `src/gobby/install/bin_freshness_models.py` — no-edit-reason: reads the managed pin by key or compares it to its crate manifest; literal synchronization flows through without changing this consumer.
- `src/gobby/install/distribution.py` — no-edit-reason: reads the managed pin by key or compares it to its crate manifest; literal synchronization flows through without changing this consumer.
- `tests/cli/installers/test_grok_installer.py` — no-edit-reason: reads the managed pin by key or compares it to its crate manifest; literal synchronization flows through without changing this consumer.
- `tests/cli/test_install_ghook.py` — no-edit-reason: reads the managed pin by key or compares it to its crate manifest; literal synchronization flows through without changing this consumer.
- `tests/cli/test_install_setup.py` — no-edit-reason: reads the managed pin by key or compares it to its crate manifest; literal synchronization flows through without changing this consumer.
- `tests/cli/test_install_setup_gclient.py` — no-edit-reason: reads the managed pin by key or compares it to its crate manifest; literal synchronization flows through without changing this consumer.
- `tests/cli/test_install_setup_gdaemon.py` — no-edit-reason: reads the managed pin by key or compares it to its crate manifest; literal synchronization flows through without changing this consumer.
- `tests/cli/test_install_setup_gterm.py` — no-edit-reason: reads the managed pin by key or compares it to its crate manifest; literal synchronization flows through without changing this consumer.
- `tests/hooks/test_runtime_compat.py` — no-edit-reason: reads the managed pin by key or compares it to its crate manifest; literal synchronization flows through without changing this consumer.
- `tests/install/test_distribution.py` — no-edit-reason: reads the managed pin by key or compares it to its crate manifest; literal synchronization flows through without changing this consumer.
- `tests/install/test_version_pins.py` — no-edit-reason: reads the managed pin by key or compares it to its crate manifest; literal synchronization flows through without changing this consumer.

**Acceptance:**
- 1.2.1 - Every five-row GET is claimed for a verified browser and returns native payload/error shapes; flat a.rs, encoded src%2Fa.rs and literal src/a.rs file paths work for browser and API-key callers, while an empty file remainder is rejected. Auth predicate and family matcher agree. POST, HEAD, runtime, node and unlisted paths gain no browser claim. test: `crates/gcodeindex/tests/family_browser.rs::only_graph_reads_claim_browser`.
- 1.2.2 - Unknown/expired/missing cookies and unsupported bearer callers receive 401 on the five reads with zero backend hits; valid API keys retain native behavior. test: `crates/gdaemon/tests/browser_cookie.rs::graph_reads_never_fall_back_to_python`.
- 1.2.3 - Existing caller lists and gates outside the five rows remain identical, including Caller::ALL. test: `crates/gcodeindex/tests/family_browser.rs::other_native_authority_is_unchanged`.
- 1.2.4 - Affected shipped Rust versions, lockfile and literal pins match the incremented merged-base versions. file: `crates/gdaemon/Cargo.toml`, `Cargo.lock`, `src/gobby/install/version_pins.py`.

## P2: Real browser and node proof
`kind: framing`

### 2.1 Prove the graph consumer through hub and node doors [category: test] (depends: 1.2)
`kind: deliverable`

Targets:
- `crates/gdaemon/tests/browser_graph.rs`
- `web/tests/code-graph-cookie.spec.ts`
- `web/tests/support/code-graph-fixture.ts`

**Research context:** `web/src/hooks/useCodeGraph.ts::useCodeGraph` performs five
same-origin fetches and returns null/empty on failures. Its consumer is
`web/src/components/code-graph/CodeGraphExplorer.tsx::CodeGraphExplorer`. Therefore
asserting only that the page renders can hide an auth/routing failure. Drive the
actual consumer and inspect all five network responses and visible graph/search
results. The hook and UI require no production edit. The new TypeScript support
file only consumes Rust-owned fixture metadata, creates
the isolated browser context and sets its HttpOnly cookie; Rust owns all process,
seeding and cleanup lifetimes. No API-route interception is allowed.
`web/playwright.config.ts` already sets testDir to tests and supports
PLAYWRIGHT_BASE_URL. Supply the isolated running fixture URL explicitly so Playwright
does not silently start a frontend against Josh's daemon.

Rust fixture creates temporary state, ephemeral ports, a test project/checkout and
machine-visible symbols plus graph edges; starts an isolated hub gdaemon and relaying
node; inserts an auth session through fixture SQL. Use test-hub PostgreSQL and
isolated Falkor data/graph identifiers; clean up only those identifiers on exit.
Enable code_index and graph through fixture configuration and index the fixture
with the Rust library. No production database, real login password, stored browser
profile or installed daemon mutation is used. Fail fixture setup when graph/database
services are unavailable; never skip required proof. Rust proof verifies direct hub
and node relay requests, cookie forwarding, logout and foreign-machine exclusion.
The Rust browser_graph acceptance process keeps seeded hub/node services alive
while it owns the browser proof. It reserves an ephemeral loopback frontend port,
then starts installed Vite from web with `npx vite --host 127.0.0.1 --port <port>
--strictPort` and GOBBY_DAEMON_PORT set to its fixture hub port. A bind race fails
setup rather than choosing the user's daemon. Wait at most 30 seconds for frontend
HTTP readiness and hub readiness. Then invoke `npx playwright test
tests/code-graph-cookie.spec.ts --workers=1` from web as a child with
PLAYWRIGHT_BASE_URL set to that Vite URL. This suppresses Playwright's own server;
the Rust process is the explicit frontend owner. Bound Playwright to 180 seconds
and the entire fixture lifetime to 300 seconds.

Pass one owner-only temporary JSON file through GOBBY_CODE_GRAPH_FIXTURE_FILE,
containing the project id, loopback frontend/hub/node URLs and fixture session token.
Do not pass credentials in process arguments or print the file contents. TypeScript
support validates metadata bounds and loopback URLs, sets the HttpOnly cookie for
the frontend origin in a new isolated context, and never exposes the token to page
JavaScript. Keep fixture state and metadata until Playwright completes. The browser
loads real CodeGraphExplorer and exercises overview, file (flat and nested),
neighbors, blast radius and search. Save exact commands and redacted test output.
On success, child failure, readiness timeout, panic or cancellation, terminate and
reap Vite/Playwright process groups, stop hub/node, remove the metadata file and
clean only fixture-owned database/graph identifiers. Rust cleanup guards and bounded
child waits own that teardown; TypeScript closes its browser context in finally.

Planned verification (one heavy slot): `CARGO_BUILD_JOBS=3 cargo test -p gobby-daemon
--test browser_graph -- --test-threads=1`. Its browser_fixture_lifecycle test runs
Vite and Playwright before Rust teardown. There is no independent second command
against a dead fixture. No Python harness, new helper binary or new dependency.

**Acceptance:**
- 2.1.1 - Hub and relaying-node requests return fixture symbols/edges for all five reads with a valid cookie and zero backend graph-route hits; foreign machine symbols remain invisible. test: `crates/gdaemon/tests/browser_graph.rs::hub_and_node_cookie_graph_reads`.
- 2.1.2 - Real browser graph load, file expansion, neighbors, blast radius and search succeed through gdaemon; removing/revoking the session makes fresh requests fail with 401 and no stale-success assertion. test: `web/tests/code-graph-cookie.spec.ts::code_graph_uses_native_cookie_reads`.
- 2.1.3 - The Rust-owned Vite/Playwright lifecycle keeps isolated state/ports/data alive until browser completion, fails on missing prerequisites, and cleans all child processes/data on failure; response payload checks cannot pass against mocked routes or an empty graph. test: `crates/gdaemon/tests/browser_graph.rs::browser_fixture_lifecycle`.

## P3: Retire Python and reconcile public evidence
`kind: framing`

### 3.1 Delete the five routes and their context consumers atomically [category: refactor] (depends: 2.1, #24710, #24713)
`kind: deliverable`

Targets:
- `src/gobby/servers/routes/code_index.py::*` — operation: delete — scope-reason: prerequisites remove mutations/path; these five reads are the last routes.
- `src/gobby/servers/_app_routes.py::register_routes`
- `src/gobby/servers/routes/__init__.py::*` — scope-reason: delete only the code-index router import/export.
- `src/gobby/code_index/context.py::*` — scope-reason: delete four read methods and helpers/imports with no surviving callers.
- `tests/servers/routes/test_code_index_routes.py::*` — operation: delete — scope-reason: only tests deleted Python routes after prerequisite retirement.
- `tests/code_index/test_code_index_context.py::*` — scope-reason: delete tests and fixtures of the retired methods only.
- `tests/test_wiki_retirement_contract.py::test_retired_http_routes_use_normal_missing_route`
- `docs/contracts/parity-ledger-status.d/git-workspaces.json::*` — scope-reason: delete status keys for exactly the five retired graph reads.
- `docs/contracts/parity-ledger.md`
- `docs/reference-audit/code-index.json::*` — scope-reason: point five graph-read implementation records at native handlers.
- `tests/contracts/http/manifest.json::*` — scope-reason: retain cases and set code_index origin to gdaemon.
- `tests/contracts/http/README.md`
- `crates/gdaemon/tests/code_graph_retirement.rs`
- `crates/gdaemon/tests/http_contracts.rs::*` — scope-reason: transfer code_index cases to executable authenticated native replay while preserving other families.

**Research context:** `create_code_index_router` owns graph_overview, graph_file,
graph_symbol_neighbors, graph_blast_radius and graph_search. Delete that file only
after #24710 removes its path/mutation routes; remove register_routes' mount and
routes package re-export. Context methods to delete are graph_overview, graph_file,
graph_symbol_neighbors and graph_blast_radius. Search reads storage directly: do not
delete shared storage search methods. Remove _graph_project_root and
_require_graph_enabled only after their path/lifecycle consumers have been retired
and the literal caller sweep proves zero use. Keep CodeIndexContext's live service
properties. #24713 is ordered first because it still validates a graph_overview test
and edits the same context; it must not depend back on this plan.

The five status keys are HTTP GET /api/code-index/graph, /graph/file/{file_path:path},
/graph/symbol/{symbol_id}/neighbors, /graph/blast-radius and /graph/search. Remove the
keys with the Python routes; regenerate the committed ledger using existing
`uv run python scripts/generate_parity_ledger.py`. Retain exactly the two cases
supplied by #24708, code_index_search_disabled and
code_index_overview_disabled, unchanged in their JSON files; switch code_index origin
to gdaemon. These cover disabled-gate responses only. Successful reads for all five
routes remain the responsibility of 2.1, not five invented corpus cases.
Re-point graph_search's audit implementation to family/search.rs and the other four
to family/graph.rs with their actual indexed handler symbols after prerequisites
land. Refresh the audit's source-audit/rust-capability verification fields. Do not
claim future Rust symbols already exist in this checkout.

The existing Rust http_contracts replay owner must take over both cases when
origin changes: its current start_front_door uses no resolver and its backend-up
branch expects a proxy hit. Add a code_index-only branch composing the real
CodeIndexFamily against isolated disabled configuration and a fixed test credential
resolver. Inject that fixture's API-key credential into these two replay requests;
never use production credentials. Assert each stored status/body (503 with the
recorded route-specific detail), normalized headers and zero backend hits for both
up/down backend states. Keep every other family's replay path unchanged. Name the
new focused Rust test code_index_native_cases_replay_equal; reuse existing replay,
normalization and backend-stub helpers, not a generic new harness. The two disabled
cases do not require indexed data; configure the real family before its database
access gate and fail if the intended disabled state is not active.

The wiki retirement test currently asserts code-index clear exists in OpenAPI;
#24710 changes that assertion to search. Delete only that final obsolete code-index
presence assertion in test_retired_http_routes_use_normal_missing_route, preserving
all wiki missing-route assertions. Its no-edit disposition was incorrect. Add Rust
source-inventory checks plus the real routing proof from 2.1; do not add Python
replacement tests. Python test changes remain deletion-only.
Run existing focused Python regressions with the isolated DATABASE_URL and
GOBBY_TEST_PROTECT=1; use uv for all Python commands. Planned commands: `cargo test
-p gobby-daemon --test code_graph_retirement`; `uv run pytest tests/code_index/test_code_index_context.py
 tests/test_parity_ledger.py tests/test_wiki_retirement_contract.py -q` with those
required environment variables; `CARGO_BUILD_JOBS=3 cargo test -p gobby-daemon --test
http_contracts code_index_native_cases_replay_equal`; rerun 2.1.

**Granularity:** more than six production Targets. Router deletion, its mount,
context caller deletion and derived public records are one retirement transaction:
separate commits would retain dead imports or fail stale-ledger checks. Gateway
cleanup remains independently closeable in 3.2.

Consumers unchanged:
- `src/gobby/servers/app_factory.py` — no-edit-reason: continues calling register_routes for surviving routers without naming the deleted router.

**Acceptance:**
- 3.1.1 - Obligation 9.8.1: all five Python handlers and router file/mount/export plus the four context reads and newly dead helpers are absent; all five native cookie reads still load. test: `crates/gdaemon/tests/code_graph_retirement.rs::python_graph_reads_are_retired`.
- 3.1.2 - Obligation 9.8.3: five status keys leave proxied, generated ledger matches inventory, and five audit records point to existing native handlers with fresh verification. test: `crates/gdaemon/tests/code_graph_retirement.rs::ledger_and_audit_name_native_graph_handlers`.
- 3.1.3 - Both existing disabled search/overview corpus JSON cases remain unchanged at gdaemon origin and execute through the real native family with an isolated fixed credential, matching stored status/body and zero backend hits; other families keep their replay behavior. test: `crates/gdaemon/tests/http_contracts.rs::code_index_native_cases_replay_equal`.
- 3.1.4 - No imports/callers of deleted handlers/context methods remain; existing live-context and wiki retirement regressions plus native browser proof pass after deletion. behavior: "Rust/TypeScript browser proof remains green after Python retirement".

### 3.2 Delete unused gateway methods and obsolete floors [category: refactor] (depends: 3.1, #24670)
`kind: deliverable`

Targets:
- `src/gobby/code_index/gcode_gateway.py::*` — operation: delete-lines — base-blob: 8f304739353c234a988f2bf06d4891444c355fe0 — lines: 30-31, 419-421, 423-445, 447-457, 459-469, 471-489, 491-512, 514-540, 542-546, 548-552, 554-576, 578-600, 602-606, 608-612, 614-626, 677-714, 968 — scope-reason: pure deletion of fifteen retired methods and obsolete floor definitions/export at the inspected checkout.
- `src/gobby/code_index/gcode_versions.py`
- `tests/code_index/test_gcode_gateway.py::*` — scope-reason: delete tests/imports for retired methods and floors without adding Python.
- `tests/code_index/test_gcode_versions.py`
- `AGENTS.md`
- `crates/AGENTS.md`
- `.gobby/workflows/rules/project-operating-policy.yaml::*` — scope-reason: delete the gcode maintenance-floor release sentence.
- `crates/gdaemon/tests/code_graph_retirement.rs`

**Research context:** The 969-line gateway's current method ranges are enumerated by
`gcode outline src/gobby/code_index/gcode_gateway.py --offset 32`. Delete exactly
checked_version, graph_sync_file, graph_overview, graph_file, graph_neighbors,
graph_blast_radius, symbol_path, graph_clear, graph_rebuild, vector_sync_file,
vector_clear, vector_rebuild, prune, maintenance_index and
prune_project_for_maintenance. Keep private _checked_version and _ensure_version
while live incremental_index/nightly_repair/invalidate consumers need them; a public
property deletion does not imply deleting private version checking.

#24709 (ancestor of #24710) removes maintenance/pruner consumers. The family seam's
prerequisites remove sync_worker; 3.1 removes read consumers. Confirm these facts at
the implementation tip by exact identifier sweep across src/, tests/ and crates/;
fix any owned residual consumer through deletion or native use, never Python glue.
The pre-expansion freshness gate rebinds this pure-deletion proof after #24670 moves
floors to gcode_versions.py. That gate must update the hash/ranges and re-review the
result; this proof is not permission to apply stale line ranges. The gcode_versions module/test Targets apply to the files created by #24670; if its
extraction supplies no separate test file, create none. Remove only
MIN_GCODE_PRUNE_BUDGET_VERSION and GCODE_ALLOW_MISSING_INDEXED_FILE_VERSION, their
imports/re-exports and obsolete tests. Keep MIN_GCODE_GRAPH_VERSION and live checking.
Delete maintenance-floor wording in root/crates AGENTS and project operating rule.
The coordinator must reconcile the installed rule row using the rule-management
surface as part of activation; templates are not live config. Other plans' release
leaves must not recreate the removed floor. No direct SQL or backup edits.

Planned checks: Rust code_graph_retirement tests; existing focused
`uv run pytest tests/code_index/test_gcode_gateway.py -q` with isolated DATABASE_URL
and GOBBY_TEST_PROTECT=1. The Rust retirement test reads source inventory and verifies
absence of the named public methods/floors and retention of live gateway functions.
It is a regression check paired with surviving functional Python tests, not a claim
that text absence alone proves routing. No new Python test or runtime is introduced.

**Granularity:** more than six Targets, all one dead gateway API cleanup and its
floor carriers; the prerequisite-owned worker/pruner state machines are excluded.

**Acceptance:**
- 3.2.1 - Obligation 9.8.2: all fifteen named dead gateway methods are absent, caller sweeps are recorded and surviving functional gateway tests pass. test: `crates/gdaemon/tests/code_graph_retirement.rs::gateway_retains_only_live_entry_points`.
- 3.2.2 - Both obsolete floors and their imports/exports/tests are deleted; MIN_GCODE_GRAPH_VERSION and live version checks remain; gateway stays below 1,000 lines. test: `crates/gdaemon/tests/code_graph_retirement.rs::obsolete_gateway_floors_are_absent`.
- 3.2.3 - Root/crates AGENTS and project operating rule no longer prescribe the maintenance floor; activation records the installed-row reconciliation and cross-plan release notification. file: `AGENTS.md`, `crates/AGENTS.md`, `.gobby/workflows/rules/project-operating-policy.yaml`.

## V3: Verification and review gates
`kind: verification`

Observed research: clean prepared checkout f983bc43f03d5cbf691c860b419e758eb1cb2c98,
base 28cce0f0de; current task/dependency cards read through MCP on 2026-10-20.
The six preserved commits remain historical Git evidence. Their prior Security
verdict and M1 are not evidence for these bytes. No historical RED is asserted.

Source evidence: `gcode evidence` read of AuthState::authenticate is complete,
excerpt hash `3d0de92eedf12c7e483b4f09c799de1f7ae9add649fc584f6cf205386e0cdf0d`;
`crates/gdaemon/src/front_door/auth.rs`, lines 129–131 return immediately when
Authorization is absent, proving the missing cookie branch. Navigation inspected FrontDoor::handle, Caller, RuntimeFamily::claims,
AuthStore.session_user_id, useCodeGraph and gateway/context outlines. Literal sweeps
`gcode grep` for graph_sync_file, prune_project_for_maintenance and maintenance_index
found sync_worker, prune, maintenance and gateway/context/test consumers; none are
claimed absent today. The bounded production sweep of the fifteen-name regex found
context.py, gcode_gateway.py, maintenance.py, prune.py and sync_worker.py. Required
implementation sweeps cover all names, imports and test strings after prerequisites.

Planning-time command: `uv run gobby plans validate
.gobby/plans/gdaemon-web-session-cookie.md -p /Users/josh/.gobby/worktrees/gobby/wt-task-24659`.
The candidate report supplies its actual result; no implementation tests have run.
Observed base validation: exit 0, three phases, no warnings after consumer-inventory
repair. `git diff --check` also exits 0. Narrative has no M1; expansion validation is intentionally unrun until a fresh
server-derived manifest exists. This plan contains five deliverables and no deferrals.

The coordinator routes fresh Security review of parsing, credential precedence,
read authority, expiry/revocation, relay behavior, outage handling and caller loss;
then Plan Adversary consensus on the exact bytes. No seat fabricates either verdict.
The adversary/coordinator applies server-derived M1 using the approved plan workflow.
Josh must approve the stamped bytes before expansion. Hub-node prerequisite and
fresh symbol/proof validation remain additional expansion gates; approval never
waives them. New code leaves 1.1/1.2 use TDD with actual Rust RED/GREEN evidence;
2.1 is a standalone integration proof; deletion leaves are refactor with focused
regressions. All heavy tests require a Lane Manager slot, CARGO_BUILD_JOBS=3, isolated
state and exact command/result receipts. Never run the full pytest suite.

## V1 Plan Changelog
`kind: framing`

2026-10-21 UTC — Agreed narrative consensus. Writer gobby#17269 and Plan Adversary
gobby#17272 agree on narrative commit 0d8d18b29a85760c703b88cfb8dc558391c115f4,
plan SHA256 72c9a3e1ff36cbdd78482cebfc853474a8c19649b9e80a2b0fd7e85ffad52187.
Adversary approval message bd9d26fb-305a-4e37-b020-4a262cb38536 resolves
COOKIE-PA-01 through COOKIE-PA-04 with no remaining findings or product questions.
Fresh Security plan-only LAND receipt 4d14dea4-86bb-4273-8deb-356f84046f26,
message aa89c2fb-a6f5-448f-819b-13e61f42e90f, approves those same narrative bytes
with no findings. This entry and the verification-heading renumbering are metadata
only; all five deliverables, decisions, acceptance items and prerequisite gates are
unchanged. The adversary must verify this committed entry before its formal
EVENT=CONSENSUS and server-derived M1 stamp. Security must confirm the metadata-only
revision. Josh approval of the stamped plan remains required before expansion;
actual hub-node completion and refreshed pre-expansion validation remain mandatory.

2026-10-20: fresh Rust/TypeScript design under Orchestrator ruling
8d65292e-775d-4eb2-b8dc-9b5dc55719bf. Replaces historical draft/M1; preserves obligations
9.8.1–9.8.3. No fresh Security/adversary/operator approval is claimed. Expansion,
landing, activation and task closure remain Orchestrator-owned.

2026-10-20 correction: accepted all four findings from Plan Adversary gobby#17272,
message c9f61602-e5ea-4895-a7bb-0eca7a312ab9, on historical candidate e244f4f3:
COOKIE-PA-01 repairs the reused wildcard matcher and URL boundary coverage;
COOKIE-PA-02 deletes the wiki contract's obsolete code-index presence assertion;
COOKIE-PA-03 transfers the two existing corpus cases to authenticated native Rust
replay; COOKIE-PA-04 gives the Rust acceptance process ownership of Vite/Playwright
through cleanup. No new deliverable or Python runtime/test is added. Security LAND
receipt fff30c9b-115c-423b-9acb-25616349ceb1 certifies only the preceding candidate;
this correction requires fresh Security/adversary review before Josh approval.

Correction validation: canonical worktree command above exits 0 with three phases
and no warning, skipped check or checkout mismatch. The YAML Target remains
symbol-bearing in this checkout: gcode outline resolves project-operating-policy;
gcode evidence range 92–95 is complete with no warnings, excerpt hash
bac25eb57444a458cf55e71524d26cdcb835793e3eac29382f3fba7f133285ec.
No index mutation was necessary to obtain this result; the adversary's earlier
checkout/index diagnostics are preserved as limitations of that earlier run.

## M1 Task Manifest
`kind: manifest`

```yaml
- title: Verify the cookie only for the five graph reads
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: '1.1.1: Valid cookies insert only the verified user''s BrowserSession;
    absent, unknown, expired, deleted and malformed credentials cannot insert it.
    test: `crates/gdaemon/tests/browser_cookie.rs::session_verification_and_revocation`.

    1.1.2: All header fields and duplicate pairs obey Decision 5, including equal
    duplicates and distinct duplicates split across header fields. test: `crates/gdaemon/tests/browser_cookie.rs::cookie_ambiguity_fails_closed`.

    1.1.3: Authorization takes precedence; cookie-only requests never gain key/machine
    identity or access to other route classes; spoofed identity headers are removed.
    test: `crates/gdaemon/tests/browser_cookie.rs::credentials_do_not_cross_auth_boundaries`.

    1.1.4: Database failures and five-second total resolution timeout return 503 with
    no native handler or Python fallback; invalid cookies on the five reads return
    401. test: `crates/gdaemon/tests/browser_cookie.rs::resolver_failure_is_unavailable`.

    1.1.5: Hash fixture, SQL expiry boundary and absence of cache match the documented
    contract. test: `crates/gdaemon/tests/browser_cookie.rs::postgres_cookie_contract`.

    1.1.6: Affected shipped Rust version, lockfile and existing pin advance together.
    file: `crates/gdaemon/Cargo.toml`, `Cargo.lock`, `src/gobby/install/version_pins.py`.

    1.1.7: The shared path_matches matcher counts the consumed wildcard remainder:
    a.rs, src%2Fa.rs and src/a.rs match the file row, empty remainder does not; auth
    predicate and family claims agree. test: `crates/gruntime/src/lib.rs::browser_file_remainder_matches_hook_urls`.'
  labels:
  - covers:gdaemon-web-session-cookie:1.1:1.1.1
  - covers:gdaemon-web-session-cookie:1.1:1.1.2
  - covers:gdaemon-web-session-cookie:1.1:1.1.3
  - covers:gdaemon-web-session-cookie:1.1:1.1.4
  - covers:gdaemon-web-session-cookie:1.1:1.1.5
  - covers:gdaemon-web-session-cookie:1.1:1.1.6
  - covers:gdaemon-web-session-cookie:1.1:1.1.7
  tdd: true
  source_section: '1.1'
  implementation_domain: backend
- title: Claim cookie callers on exactly five native rows
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  validation_criteria: '1.2.1: Every five-row GET is claimed for a verified browser
    and returns native payload/error shapes; flat a.rs, encoded src%2Fa.rs and literal
    src/a.rs file paths work for browser and API-key callers, while an empty file
    remainder is rejected. Auth predicate and family matcher agree. POST, HEAD, runtime,
    node and unlisted paths gain no browser claim. test: `crates/gcodeindex/tests/family_browser.rs::only_graph_reads_claim_browser`.

    1.2.2: Unknown/expired/missing cookies and unsupported bearer callers receive
    401 on the five reads with zero backend hits; valid API keys retain native behavior.
    test: `crates/gdaemon/tests/browser_cookie.rs::graph_reads_never_fall_back_to_python`.

    1.2.3: Existing caller lists and gates outside the five rows remain identical,
    including Caller::ALL. test: `crates/gcodeindex/tests/family_browser.rs::other_native_authority_is_unchanged`.

    1.2.4: Affected shipped Rust versions, lockfile and literal pins match the incremented
    merged-base versions. file: `crates/gdaemon/Cargo.toml`, `Cargo.lock`, `src/gobby/install/version_pins.py`.'
  labels:
  - covers:gdaemon-web-session-cookie:1.2:1.2.1
  - covers:gdaemon-web-session-cookie:1.2:1.2.2
  - covers:gdaemon-web-session-cookie:1.2:1.2.3
  - covers:gdaemon-web-session-cookie:1.2:1.2.4
  tdd: true
  source_section: '1.2'
  implementation_domain: backend
- title: Prove the graph consumer through hub and node doors
  category: test
  task_type: feature
  depends_on:
  - '1.2'
  validation_criteria: '2.1.1: Hub and relaying-node requests return fixture symbols/edges
    for all five reads with a valid cookie and zero backend graph-route hits; foreign
    machine symbols remain invisible. test: `crates/gdaemon/tests/browser_graph.rs::hub_and_node_cookie_graph_reads`.

    2.1.2: Real browser graph load, file expansion, neighbors, blast radius and search
    succeed through gdaemon; removing/revoking the session makes fresh requests fail
    with 401 and no stale-success assertion. test: `web/tests/code-graph-cookie.spec.ts::code_graph_uses_native_cookie_reads`.

    2.1.3: The Rust-owned Vite/Playwright lifecycle keeps isolated state/ports/data
    alive until browser completion, fails on missing prerequisites, and cleans all
    child processes/data on failure; response payload checks cannot pass against mocked
    routes or an empty graph. test: `crates/gdaemon/tests/browser_graph.rs::browser_fixture_lifecycle`.'
  labels:
  - covers:gdaemon-web-session-cookie:2.1:2.1.1
  - covers:gdaemon-web-session-cookie:2.1:2.1.2
  - covers:gdaemon-web-session-cookie:2.1:2.1.3
  tdd: false
  source_section: '2.1'
  assigned_agent: developer
- title: Delete the five routes and their context consumers atomically
  category: refactor
  task_type: feature
  depends_on:
  - '2.1'
  validation_criteria: '3.1.1: Obligation 9.8.1: all five Python handlers and router
    file/mount/export plus the four context reads and newly dead helpers are absent;
    all five native cookie reads still load. test: `crates/gdaemon/tests/code_graph_retirement.rs::python_graph_reads_are_retired`.

    3.1.2: Obligation 9.8.3: five status keys leave proxied, generated ledger matches
    inventory, and five audit records point to existing native handlers with fresh
    verification. test: `crates/gdaemon/tests/code_graph_retirement.rs::ledger_and_audit_name_native_graph_handlers`.

    3.1.3: Both existing disabled search/overview corpus JSON cases remain unchanged
    at gdaemon origin and execute through the real native family with an isolated
    fixed credential, matching stored status/body and zero backend hits; other families
    keep their replay behavior. test: `crates/gdaemon/tests/http_contracts.rs::code_index_native_cases_replay_equal`.

    3.1.4: No imports/callers of deleted handlers/context methods remain; existing
    live-context and wiki retirement regressions plus native browser proof pass after
    deletion. behavior: "Rust/TypeScript browser proof remains green after Python
    retirement".'
  labels:
  - covers:gdaemon-web-session-cookie:3.1:3.1.1
  - covers:gdaemon-web-session-cookie:3.1:3.1.2
  - covers:gdaemon-web-session-cookie:3.1:3.1.3
  - covers:gdaemon-web-session-cookie:3.1:3.1.4
  tdd: false
  source_section: '3.1'
  assigned_agent: developer
- title: Delete unused gateway methods and obsolete floors
  category: refactor
  task_type: feature
  depends_on:
  - '3.1'
  validation_criteria: '3.2.1: Obligation 9.8.2: all fifteen named dead gateway methods
    are absent, caller sweeps are recorded and surviving functional gateway tests
    pass. test: `crates/gdaemon/tests/code_graph_retirement.rs::gateway_retains_only_live_entry_points`.

    3.2.2: Both obsolete floors and their imports/exports/tests are deleted; MIN_GCODE_GRAPH_VERSION
    and live version checks remain; gateway stays below 1,000 lines. test: `crates/gdaemon/tests/code_graph_retirement.rs::obsolete_gateway_floors_are_absent`.

    3.2.3: Root/crates AGENTS and project operating rule no longer prescribe the maintenance
    floor; activation records the installed-row reconciliation and cross-plan release
    notification. file: `AGENTS.md`, `crates/AGENTS.md`, `.gobby/workflows/rules/project-operating-policy.yaml`.'
  labels:
  - covers:gdaemon-web-session-cookie:3.2:3.2.1
  - covers:gdaemon-web-session-cookie:3.2:3.2.2
  - covers:gdaemon-web-session-cookie:3.2:3.2.3
  tdd: false
  source_section: '3.2'
  assigned_agent: developer
```
