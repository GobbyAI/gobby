Plan artifact: `.gobby/plans/gdaemon-web-session-cookie.md`

# gdaemon verifies the web UI session cookie; Python's code-index web UI reads retire

**Plan ID:** gdaemon-web-session-cookie

**Task:** #24659 (cfc60361-8271-4419-ba7c-626d7bef1e03), parent family plan #21586 (`.gobby/plans/code-index-family.md`, D15 and the former section 9.8).
**Status:** Draft 5, 2026-10-18. Narrative only. Draft 2 closed the Plan Adversary's first NEEDS REPAIR verdict (gobby#16993). Draft 3 applied the follow-up's blocking items R1 to R4 and its nit. Draft 4 applied R5. Draft 5 applies R6, the Adversary's factual correction on the gdaemon dependency, and records the Security verdict on Draft 3. The Orchestrator accepted decisions (a) to (c) on 2026-10-18 at 12:06 CDT, and operator approval comes with the plan as a whole. Grok's Security review of Draft 3, gobby#17126, found no findings, and its verdict is recorded in V1. The Plan Adversary derives M1 after consensus, the Writer commits the rendered bytes, and the operator approves before any expansion.

## Overview
`kind: framing`

The web UI's code-graph view signs in with the `gobby_session` cookie
(`web/src/hooks/useCodeGraph.ts`). Only Python verifies that cookie, through
`AuthStore.session_user_id`. The code-index family's native routes therefore admit
API-key callers only, and Python keeps the five graph read routes that browser
callers use: overview, file, symbol neighbors, blast radius and search.

This plan moves cookie verification into gdaemon's front door, lets the code-index
family serve the five GET routes to browser sessions, proves the web UI's five
requests return the same shapes through gdaemon, and then deletes the Python routes,
the `CodeIndexContext` and `GcodeGateway` read methods that only they call, and the
five ledger rows that describe them. When this plan closes, the web UI's code-graph
view loads through gdaemon with a cookie and no Python code-index route remains for
browser callers.

## Constraints
`kind: framing`

- **Cookie callers outside the five routes keep reaching Python.** Python's
  `AuthStore.session_user_id` stays the verifier for every route gdaemon proxies.
  gdaemon acts on a verified cookie only where a gdaemon-served row claims it.
- **No backward compatibility.** 0.5.0 has not shipped. Each leaf lands as one commit,
  with no dual-accept window.
- **Family rules.** D15 settles the callers, D16 settles the projection operations
  (not touched here), and D17 and D18 settle the mutation routes and the trigger.
  `POST /api/code-index/prune` and the graph path, clear, rebuild and invalidate routes
  belong to family phases P8 and P9 (9.3 and 9.4), not to this plan.
- **Release.** `gdaemon` is a shipped binary, and this plan changes its crate in two
  leaves, 1.1 and 2.1. Each shipping commit bumps its patch version by 0.0.1, updates
  `Cargo.lock`, and updates the managed `gdaemon` pin in
  `src/gobby/install/version_pins.py`. At the 0.5.0 tip `6ff393368a` the crate and the
  pin both read 0.4.61, so 1.1 lands 0.4.62 and 2.1 lands 0.4.63, if the base does not
  move. `gruntime` and `gcodeindex` are not shipped binaries and take no bump of their own.
- **Monolith ceiling.** `src/gobby/code_index/gcode_gateway.py` is 969 lines at the
  0.5.0 tip (blob `e77a70429a3774b3325b2eb848f2a692b8093add`). Deliverable 4.3 shrinks
  it. Because it stays at or above 850 lines until then, deliverable 4.3 carries a
  pure-deletion proof rather than a split (see Research context in 4.3).
- **Dependencies on other work.** This plan depends on #24665 (P5, the family seam),
  #24666 (P6, graph read routes), #24694 (5.1, `GET /graph/search`), #24670 (1.1,
  version floors move to `gcode_versions.py`) and #24710 (9.4, graph path, clear,
  rebuild and invalidate routes deleted). Each is a `(depends: #N)` edge in the
  headings below.
- **Floor wording and cross-plan effect.** The gcode maintenance floor goes in 4.3.
  The release leaves in `remove-gobby-build.md` (lines 513 and 1887) and
  `node-install-enrollment.md` (lines 378 to 393) each raise `MIN_GCODE_PRUNE_BUDGET_VERSION`.
  Once 4.3 lands, those leaves have no floor to raise, and they must not recreate it.
  Coordination with those plans goes through the Orchestrator.

## Decision Record
`kind: framing`

Operator decisions. The Orchestrator accepted them on 2026-10-18 at 12:06 CDT, and
operator approval comes with the plan as a whole. The answers, quoted:

- **(a) Cookie scope.** "Browser sessions reach the five GET routes only. Any unexpired
  session user may read them, as Python does today. No mutation route, and no other
  route, admits a session."
- **(b) Verification path.** "gdaemon queries `auth_sessions` through its own pool, the
  same way `PostgresKeyResolver` queries `api_keys`."
- **(c) Plan ID.** "`gdaemon-web-session-cookie`, as the Plan ID above."

Mechanism decisions settled by this plan:

- The verified cookie travels as a request extension, `gruntime::BrowserSession { user_id }`,
  inserted by `AuthState::authenticate`. It never travels as an identity header.
  Identity headers are stripped from client input and reach Python, so a browser
  identity must not reach Python as a header.
- `AuthState::authenticate` takes the request, `&mut Request<Body>`, instead of the
  header map, so that it can insert the extension. Its two call sites in
  `FrontDoor::handle` pass the request. The lease-admin branch stays API-key-only: it
  still requires `x-gobby-key-id`, which a cookie never sets, so a cookie never admits there.
- `Caller` gains a `Browser` variant. `Caller::of` returns `Browser` when the extension
  is present. Family rows list the callers they serve.
- On gdaemon-served routes, `Authorization` decides the request alone. A request with an
  `Authorization` header never reads the cookie there, so the ambiguity rules below never
  apply to it. On proxied routes Python's handling is unchanged: a non-bearer
  `Authorization` header still reaches Python's cookie check (`auth_service.py` 399 to 409).
- Duplicate `gobby_session` values split by route:
  - Two or more distinct values in the request's `Cookie` headers insert the marker
    `gruntime::AmbiguousBrowserSession` and no `BrowserSession`.
  - On a gdaemon-served route, the front door answers `401 missing_auth` before the
    route's handler runs. Gdaemon-served routes are the lease-admin route, the machines
    API and the node channel from 1.1, and each native family row that admits
    `Caller::Browser` from 2.1. Ambiguous credentials fail closed.
  - On a proxied route, the request passes through unchanged, both `Cookie` headers
    intact, and Python decides as today. Python's verifier reads one value.
  - One value sent more than once is accepted as that value.
- A native row cannot see the ambiguity itself, because `Caller::of` returns no
  `Browser` for an ambiguous request. The 2.1 refusal therefore reads the code-index
  row list and asks whether the method and path admit browser callers, through
  `gruntime::admits_browser(rows, method, path)`.
- The token hash is `gobby_core::api_key_format::hash` (SHA-256, lowercase hex). It
  equals Python's `hash_token`.
- Expiry is `expires_at >= now()` in SQL. Python returns no user when `now > expires_at`.
- The lookup runs on every request that carries a cookie and no `Authorization` header.
  Proxied routes ignore its result. Gating the lookup by the route table, so that proxied
  routes skip it, is not taken. The route table would answer only the 2.1 refusal, and
  the lookup stays one query by token hash. A lookup error leaves the request
  unauthenticated, as Python's cookie check returns no user on an exception.
- No schema change. `auth_sessions` stays Python-owned, and gdaemon only reads it.
- Node doors relay to the hub, so verification runs on the hub door only.
- Loss under D15: Python's `_legacy_rejection` admits break-glass and `gobby-agent-v1.`
  agent tokens on these five routes. gdaemon passes agent tokens through, and the native
  rows admit Operator and Browser callers only. After 4.1, those callers get 404 on the
  five paths. No client in the repo sends agent tokens to these routes. The only consumer
  is `web/src/hooks/useCodeGraph.ts`, which sends the cookie.

## P1: Cookie verification in gdaemon
`kind: framing`

Deliverable 1.1 gives gdaemon the verifier and its version bump. Its only change to a
route's answer is the duplicate-cookie rule on gdaemon-served routes. Nothing in P1
changes a route's claim.

### 1.1 Verify the browser session cookie in the front door, and bump gdaemon [category: code]
`kind: deliverable`

Add a session lookup beside the key lookup. `KeyResolver` gains a defaulted method
`resolve_session`, which returns a future of `Result<Option<String>>` (the user id). The
default resolves to `Ok(None)`, so every existing resolver and test double compiles
unchanged. `PostgresKeyResolver` implements it with one query against `auth_sessions`.
`AuthState::authenticate` takes `&mut Request<Body>`. It removes the identity headers
first. Without an `Authorization` header it collects every `gobby_session` value from
the `Cookie` headers. Zero values does nothing. One distinct value is hashed, resolved,
and inserted as `BrowserSession` on success. Two or more distinct values insert
`AmbiguousBrowserSession`. The bearer path is unchanged.

After each `authenticate` call in `FrontDoor::handle`, the request is answered
`401 missing_auth` when it carries `AmbiguousBrowserSession` and its path is the
lease-admin route, the machines API or the node channel. The check sits before any
route handler runs.

The same change bumps `crates/gdaemon/Cargo.toml` by 0.0.1 and updates `Cargo.lock` and
the managed pin in `src/gobby/install/version_pins.py`, in the same commit.

**Granularity:** 1.1 carries ten acceptance items because one behavior, browser-session
admission at the front door, has its verifier, its duplicate-cookie split, its lease-admin
guard and its version bump in one commit. A split would ship a verifier without its
duplicate-cookie rule, or a gdaemon source change without its bump, which the shared
promotion guard refuses.

Targets:
- `crates/gdaemon/src/front_door/auth.rs::KeyResolver`
- `crates/gdaemon/src/front_door/auth.rs::PostgresKeyResolver`
- `crates/gdaemon/src/front_door/auth.rs::AuthState::authenticate`
- `crates/gdaemon/src/front_door/mod.rs::FrontDoor::handle` — the two `authenticate` call sites pass the request, and the ambiguous-cookie 401 applies on gdaemon-served routes.
- `crates/gruntime/src/lib.rs::*` — scope-reason: adds the public `BrowserSession { user_id }` struct and the `AmbiguousBrowserSession` marker that the front door inserts; `Caller` changes in 2.1.
- `crates/gdaemon/tests/front_door.rs::*` — scope-reason: adds the cookie-verification and duplicate-cookie cases.
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: updates the managed `gdaemon` pin to the bumped version.

Research context:
- `crates/gdaemon/src/front_door/auth.rs`: `ResolveFuture` (line 23), `KeyResolver` (26-28),
  `PostgresKeyResolver` (30-33), `PostgresKeyResolver::resolve` (45-90, query
  `SELECT k.user_id::text, m.id::text, k.id::text FROM api_keys k JOIN machines m ...
  WHERE k.key_hash = $1 AND k.revoked_at IS NULL`), `AuthState::authenticate`
  (115-153 at the tip, signature `headers: &mut HeaderMap`), `IDENTITY_HEADERS` (156-161),
  `missing_auth` (163-169).
- Call sites that change: `crates/gdaemon/src/front_door/mod.rs` line 145 (lease admin,
  which then checks `x-gobby-key-id` at line 148) and line 191 (every other request).
  No test calls `authenticate` directly.
- Ambiguity refusal sites: the lease-admin route is `admin_api::is_route` (line 144), the
  machines API is `machines_api::is_route` (line 199), and the node channel is the literal
  path `/api/nodes/channel` (line 194). All three follow the line 191 `authenticate` call
  or sit inside its branch.
- The version at the 0.5.0 tip: `crates/gdaemon/Cargo.toml` line 3 reads `0.4.61`, and
  `src/gobby/install/version_pins.py` line 9 pins `"gdaemon": "0.4.61"`. The bump is
  taken from the merged base when the leaf lands, not from this text.
- Python reference: `src/gobby/storage/auth.py::hash_token` (line 18, SHA-256 hex),
  `AuthStore.session_user_id` (line 72, `SELECT user_id, expires_at FROM auth_sessions
  WHERE token_hash = %s`, `None` once `utc_now() > expires_at`), and the cookie check in
  `src/gobby/servers/auth_service.py` (line 384), which returns no user on an exception.
- Consumers of the constructors stay unchanged: `AuthState::new` has callers in
  `crates/gdaemon/src/serve.rs` and the gdaemon tests, and `PostgresKeyResolver::new`
  is called from `serve.rs:156` and the tests. The trait default keeps them compiling.
- Rejected: a new resolver type wired through `AuthState::new`. It changes about ten
  constructor call sites for no gain, and the trait default covers the same ground.
- Rejected: identity headers for the cookie. Headers reach Python, and a forged
  header would become an identity there.

**Acceptance:**

- 1.1.1 - An unexpired `gobby_session` cookie resolves through `KeyResolver::resolve_session` to its `user_id`, and the request carries `BrowserSession { user_id }` in its extensions. test: `crates/gdaemon/tests/front_door.rs::cookie_session_resolves_unexpired_user`.
- 1.1.2 - An expired, unknown or empty cookie, and a request without one, carries no `BrowserSession` and proceeds as unauthenticated does today. test: `crates/gdaemon/tests/front_door.rs::cookie_session_rejects_expired_and_unknown`.
- 1.1.3 - A request with an `Authorization` header is decided by the bearer path alone, and its `gobby_session` cookie is ignored. test: `crates/gdaemon/tests/front_door.rs::bearer_takes_precedence_over_cookie`.
- 1.1.4 - Two distinct `gobby_session` values on the lease-admin route, the machines API or the node channel are answered `401 missing_auth` before the route's handler runs. test: `crates/gdaemon/tests/front_door.rs::duplicate_session_cookies_fail_closed_on_hub_routes`.
- 1.1.5 - Two distinct `gobby_session` values on a proxied route reach the backend with both `Cookie` values unchanged, and carry no `BrowserSession`. test: `crates/gdaemon/tests/front_door.rs::duplicate_session_cookies_pass_through_on_proxied_routes`.
- 1.1.6 - The same `gobby_session` value sent twice is accepted as that session. test: `crates/gdaemon/tests/front_door.rs::same_session_cookie_sent_twice_is_accepted`.
- 1.1.7 - Client identity headers are removed before the cookie branch runs, and no cookie-derived value is written to any identity header. test: `crates/gdaemon/tests/front_door.rs::cookie_session_sets_no_identity_headers`.
- 1.1.8 - `resolve_session` hashes the cookie with `api_key_format::hash` and filters `expires_at >= now()`. The Rust test pins the digest that Python's `hash_token` returns for a fixture token. test: `crates/gdaemon/tests/front_door.rs::session_hash_matches_python_hash_token`.
- 1.1.9 - The lease-admin route stays API-key-only: a request whose only credential is a `gobby_session` cookie is refused as before. test: `crates/gdaemon/tests/front_door.rs::cookie_never_admits_lease_admin`.
- 1.1.10 - `crates/gdaemon/Cargo.toml` and the `Cargo.lock` entry for gdaemon carry the merged-base version plus 0.0.1, and the managed `gdaemon` pin in `version_pins.py` matches. The existing pin test checks the pair. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

## P2: The code-index family serves browser sessions on five GET routes
`kind: framing`

P2 changes which callers the five native rows admit, and adds the front-door refusal
that ambiguous cookies need on those rows. It changes no handler.

### 2.1 Family claims browser-session callers on the five graph read rows [category: code] (depends: 1.1, #24665, #24666, #24694)
`kind: deliverable`

`Caller::of` gains the `Browser` branch. Each of the five native rows adds `Caller::Browser`
to its `callers` list. Those rows are overview, file, symbol neighbors, blast radius and
search. Path is not in the list, because the web UI never calls it. The method stays
`GET` on every row, so a session caller cannot mutate through any row. This leaf also
bumps gdaemon again, because its crate's recursive path dependencies change.

Ambiguous cookies on the five paths need a refusal at the front door. `Caller::of` sees
no browser caller on an ambiguous request, so no row claims it, and `table.dispatch`
would proxy it to Python, where it passes through. `FrontDoor::handle` therefore answers
`401 missing_auth` before `table.dispatch` when the request carries
`AmbiguousBrowserSession` and `gruntime::admits_browser(rows, method, path)` is true.
`rows` is the code-index family's public `NATIVE_ROUTES`. `admits_browser` returns true
when a row matching the method and path lists `Caller::Browser`. Any path that no row
admits to browser callers is unaffected.

The front door holds families only as `RouteFamily` trait objects, which expose no row
list, so it cannot read the code-index rows through the trait. It names `NATIVE_ROUTES`
through the code-index crate instead. gdaemon already depends on that crate, package
`gobby-codeindex`, because the family plan links it into gdaemon (D5 and 1.9.1, lines 230
and 1273 of `.gobby/plans/code-index-family.md`). So 2.1 adds no dependency. The edge
stays acyclic: `gcodeindex` depends on `gruntime`, and `gruntime` does not depend on
`gdaemon`.

Targets:
- `crates/gruntime/src/lib.rs::*` — scope-reason: adds the `Browser` variant to `Caller`, `Caller::of` reads the `BrowserSession` extension that 1.1 adds, and `admits_browser(rows, method, path)`, the row lookup the front door uses for the ambiguous-cookie 401.
- `crates/gruntime/tests/caller.rs` — new file, the browser-caller cases.
- `crates/gcodeindex/src/family.rs` — created by #24665; the NATIVE_ROUTES rows for the five GET reads gain the Browser caller, and `NATIVE_ROUTES` is `pub` for the front door's ambiguous-cookie lookup if #24665 has not made it so.
- `crates/gcodeindex/tests/family_claims.rs` — new file, the API-key claim cases.
- `crates/gdaemon/src/front_door/mod.rs::FrontDoor::handle` — adds the ambiguous-cookie refusal before `table.dispatch`.
- `crates/gdaemon/tests/front_door.rs::*` — scope-reason: adds the browser-session claim case and the ambiguous-cookie case on the five paths.
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: moves the managed `gdaemon` pin to the 2.1 version.

Research context:
- At the 0.5.0 tip, `crates/gruntime/src/lib.rs::Caller` (lines 17-21) has one variant,
  `Operator`, documented as "an API key the front door resolved: the request carries
  `x-gobby-key-id`". `Caller::of` (lines 26-31) returns `Option<Self>`. `NativeRoute`
  (35-40) holds `callers: &'static [Caller]`, and `NATIVE_ROUTES` (43-48) is the family's
  row list.
- `RuntimeFamily.claims` (`lib.rs` 113-123) calls `Caller::of(parts)` and matches
  `row.method`, `row.path`, `row.callers` and the optional gate. P5's `claims_native`
  moves that match into a shared `pub fn`, so one change covers every family.
- The five rows are: `GET /api/code-index/graph` (6.1), `GET /graph/file/*` (6.1),
  `GET /graph/symbol/*/neighbors` (6.1), `GET /graph/blast-radius` (6.1), and
  `GET /graph/search` (5.1, `crates/gcodeindex/src/family/search.rs`).
- `claims_native` and `family.rs` do not exist at the 0.5.0 tip. #24665 creates them.
  This deliverable's Targets resolve after #24665 lands.
- The front door holds families only as `RouteFamily` trait objects (`serve.rs` 169,
  `front_door/routes.rs` 40). `RouteFamily` (`crates/gcore/src/route_family.rs` 20-47)
  exposes name, prefixes, router, default_backend, claims and check_proxied, with no row list.
- Rejected: a `RouteFamily` method that exposes rows. `gcore` is a recursive dependency
  of gcode, gdaemon and ghook, so the change would add three release bumps for one consumer.
- Rejected: a per-request ambiguity flag on the native rows. The row list already holds
  the method, the path and the callers, so one lookup answers the question without a
  second field to keep in step.

**Acceptance:**

- 2.1.1 - `Caller::of` returns `Caller::Browser` for a request carrying `BrowserSession`, and `Caller::Operator` for a request carrying `x-gobby-key-id`, as before. test: `crates/gruntime/tests/caller.rs::browser_session_maps_to_browser_caller`.
- 2.1.2 - A `GET` with a browser session to each of the five paths is served natively, and a browser session on any other path, or on any other method, reaches Python unchanged. test: `crates/gdaemon/tests/front_door.rs::browser_session_claims_only_listed_graph_reads`.
- 2.1.3 - An API-key caller's claims on every row are unchanged. test: `crates/gcodeindex/tests/family_claims.rs::api_key_callers_keep_listed_row_claims`.
- 2.1.4 - `crates/gdaemon/Cargo.toml`, `Cargo.lock` and the managed `gdaemon` pin carry the 1.1 version plus 0.0.1. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.
- 2.1.5 - Two distinct `gobby_session` values on each of the five GET paths are answered `401 missing_auth` before dispatch, and the same two values on a path that no row admits to browser callers reach Python with both `Cookie` values unchanged. test: `crates/gdaemon/tests/front_door.rs::duplicate_session_cookies_fail_closed_on_graph_reads`.

## P3: Parity proof before deletion
`kind: framing`

P3 proves that the web UI's five requests return the same shapes through gdaemon,
while Python still serves them. P4 deletes nothing until P3 is green.

### 3.1 The web UI code-graph view loads through gdaemon with a cookie [category: test] (depends: 2.1)
`kind: deliverable`

A new end-to-end file drives the five requests exactly as `useCodeGraph.ts` builds
them, with a `gobby_session` cookie minted through `AuthStore.create_session` against
the isolated test hub. Its bootstrap enables the code index through a `ConfigPatch`, as
`tests/e2e/test_restart_responsiveness.py` does (line 31), which overrides the disabled
value the harness sets at `tests/e2e/conftest.py` line 609. The file indexes one fixture
project with the installed `gcode`. Python's response for that project comes from the
Python backend port, the comparison origin, since the front door proxies to
`127.0.0.1:<port + 100>`. The file asserts that gdaemon's responses match Python's in
status and in JSON keys.

The file needs an authenticated FalkorDB, the same prerequisite as
`tests/e2e/test_memory_dream_gc_e2e.py` (lines 68 to 120): the `GOBBY_FALKORDB_HOST`,
`GOBBY_FALKORDB_PORT` and `GOBBY_FALKORDB_PASSWORD` variables, and a `falkordb:` block
written into the isolated daemon config (line 117). The memory test skips when all three
variables are unset (lines 80 to 84). The new file does not skip. It fails when any of
the variables is missing, when the FalkorDB is unreachable, or when authentication is
rejected, so a run without a graph backend cannot pass vacuously. The cases are not
narrowed without a re-stamp.

Targets:
- `tests/e2e/test_code_graph_session_cookie.py` — new file.

Research context:
- `web/src/hooks/useCodeGraph.ts` builds five URLs on `getBaseUrl()`:
  `/api/code-index/graph`, `/api/code-index/graph/file/{path}`,
  `/api/code-index/graph/symbol/{id}/neighbors`, `/api/code-index/graph/blast-radius`
  and `/api/code-index/graph/search`. Its fetch calls (lines 89 to 173) set no
  `credentials` option, so the browser sends the same-origin cookie.
- The e2e fixtures in `tests/e2e/test_daemon_auth.py` cover cookie sign-in. This file
  reuses them, and it runs against the isolated test hub, never the operator daemon.

**Acceptance:**

- 3.1.1 - Each of the five requests returns 200 through gdaemon for a valid cookie, with the same top-level JSON keys as Python's response from the Python backend port for the same project. test: `tests/e2e/test_code_graph_session_cookie.py::test_code_graph_views_load_through_gdaemon_with_cookie`.
- 3.1.2 - The same requests with no cookie, or with an expired cookie, receive the same 401 that Python answers today. test: `tests/e2e/test_code_graph_session_cookie.py::test_unauthenticated_graph_reads_stay_401`.

## P4: Retire Python's five read routes and their callers
`kind: framing`

P4 deletes what the five routes called and nothing else. Each deletion needs a caller
proof recorded in the leaf's close summary.

### 4.1 Delete the five Python graph read routes, their router and their ledger keys [category: code] (depends: 3.1, #24710)
`kind: deliverable`

After 9.3 and 9.4, `create_code_index_router` serves only the five read routes. The
module goes with them. Its one mount in `_app_routes.py` and its re-export in
`routes/__init__.py` go too. The five parity-ledger status keys for those routes go in
the same commit, and the generated ledger is regenerated, so the generator's stale-surface
check passes. The HTTP corpus's `code_index` family flips to gdaemon origin, so the corpus
stops recording the deleted routes.

Targets:
- `src/gobby/servers/routes/code_index.py::*` — operation: delete — scope-reason: after 9.3 and 9.4 the router serves only the five browser read routes, so the whole module goes.
- `src/gobby/servers/_app_routes.py::*` — scope-reason: drops the `create_code_index_router` import and its `include_router` call (line 82).
- `src/gobby/servers/routes/__init__.py::*` — scope-reason: drops the `create_code_index_router` re-export and its `__all__` entry (lines 13 and 56).
- `tests/servers/routes/test_code_index_routes.py::*` — operation: delete — scope-reason: its only remaining tests exercise the five deleted routes.
- `tests/servers/routes/test_code_index_retirement.py` — new file, the retirement, ledger and corpus cases.
- `docs/contracts/parity-ledger-status.d/git-workspaces.json::*` — scope-reason: drops the five graph keys.
- `docs/contracts/parity-ledger.md` — regenerated by `scripts/generate_parity_ledger.py`.
- `tests/contracts/http/manifest.json::*` — scope-reason: flips the `code_index` family to gdaemon origin.
- `tests/contracts/http/README.md` — states the new origin.

Research context:
- `create_code_index_router` (`src/gobby/servers/routes/code_index.py` 102-465) holds
  the five routes: `graph_overview` (107), `graph_file` (139), `graph_symbol_neighbors`
  (175), `graph_blast_radius` (213) and `graph_search` (295). `graph_path` (257) belongs to 9.4.
- `graph_search` reads `code_indexer.storage.search_symbols_fts` and
  `search_symbols_by_name` directly. Those storage methods stay, because the native
  search row needs them. Only the route goes.
- The remaining importer of `create_code_index_router` at the 0.5.0 tip is
  `tests/servers/routes/test_code_index_prune_route.py`. The prune route is deleted by
  9.3, so that file is gone before this leaf starts. Acceptance 4.1.3 checks it.
- The five keys in `docs/contracts/parity-ledger-status.d/git-workspaces.json` (lines
  138 to 158) are the graph, blast-radius, file, search and neighbors routes. The
  `graph/path` key belongs to 9.4. The status values the generator accepts are `proxied`,
  `delegated` and `dropped by decision #N` (`scripts/generate_parity_ledger.py` 176 to 180).
  `render_ledger` (186 to 195) raises on a stale key whose status is not a drop, so the
  keys must leave in the same commit as the Python surfaces.
- `tests/contracts/http/manifest.json` records `code_index` cases with `origin: python`.
  The corpus replay module `tests/contracts/http_corpus.py` records `up` cases only for
  python-origin families (line 97), so the flip keeps a re-record off the deleted routes.

**Acceptance:**

- 4.1.1 - `GET /api/code-index/graph` with a valid cookie is answered by gdaemon, and no Python route is mounted for the five paths. test: `tests/servers/routes/test_code_index_retirement.py::test_code_index_read_routes_are_not_registered`.
- 4.1.2 - No module imports `gobby.servers.routes.code_index`, and `src/gobby/servers/routes/code_index.py` is absent. test: `tests/servers/routes/test_code_index_retirement.py::test_no_module_imports_code_index_router`.
- 4.1.3 - Each deleted route handler and each helper it used has zero callers in `src/` and `tests/` at the leaf's commit, and the check is recorded in the close summary. test: `tests/servers/routes/test_code_index_retirement.py::test_deleted_route_helpers_have_no_callers`.
- 4.1.4 - The five graph keys are absent from the status file, and the committed ledger matches the generator's output. test: `tests/servers/routes/test_code_index_retirement.py::test_parity_ledger_drops_graph_keys`.
- 4.1.5 - The `code_index` corpus family has gdaemon origin, and no manifest case names a deleted route. test: `tests/servers/routes/test_code_index_retirement.py::test_code_index_corpus_origin_is_gdaemon`.

### 4.2 Delete CodeIndexContext's read methods that no caller reaches [category: code] (depends: 4.1)
`kind: deliverable`

Targets:
- `src/gobby/code_index/context.py::CodeIndexContext.graph_overview`
- `src/gobby/code_index/context.py::CodeIndexContext.graph_file`
- `src/gobby/code_index/context.py::CodeIndexContext.graph_symbol_neighbors`
- `src/gobby/code_index/context.py::CodeIndexContext.graph_blast_radius`
- `tests/code_index/test_code_index_context.py::*` — scope-reason: drops the four graph-read delegation tests.

Research context:
- `CodeIndexContext.graph_overview` (context.py 135) through `graph_blast_radius` (159)
  each resolve the project root and delegate to `GcodeGateway`. `graph_path` (179)
  belongs to 9.4, and this leaf leaves it alone.
- `CodeIndexContext` is a consumer-facing module (D15 keeps it), so the file stays and
  only the four read methods go.

**Acceptance:**

- 4.2.1 - `gcode usages` for each of the four symbols returns no caller at the leaf's commit, and the result is recorded in the close summary. test: `tests/code_index/test_code_index_context.py::test_context_has_no_graph_read_methods`.
- 4.2.2 - `CodeIndexContext` keeps every method that still has a caller, and the focused file `tests/code_index/test_code_index_context.py` passes. test: `tests/code_index/test_code_index_context.py::test_context_keeps_live_methods`.

### 4.3 Delete GcodeGateway's methods that only deleted code called, and the version floors [category: code] (depends: 4.2, #24670)
`kind: deliverable`

This leaf removes the gateway methods that only the deleted routes and context methods
called, plus the two version floors whose only readers were the pruner and the
gateway's prune path. It moves nothing. It also removes the floor wording from the
three documents that state it.

Targets:
- `src/gobby/code_index/gcode_gateway.py::*` — operation: delete-lines — base-blob: e77a70429a3774b3325b2eb848f2a692b8093add — lines: 30-31, 419-421, 423-445, 447-457, 459-469, 471-489, 491-512, 514-540, 542-546, 548-552, 554-576, 578-600, 602-606, 608-612, 614-626, 677-714, 968 — scope-reason: deletes the fifteen methods named in Research context, the two version floors and their __all__ entry at the 0.5.0 tip; the proof is re-grounded after #24670 (see Research context).
- `src/gobby/code_index/gcode_versions.py` — created by #24670; the two floors leave it.
- `crates/AGENTS.md` — drops the gcode maintenance floor sentence (line 110).
- `AGENTS.md` — drops the gcode maintenance version floor phrase (line 238).
- `.gobby/workflows/rules/project-operating-policy.yaml::*` — scope-reason: drops the floor sentence in the crate-release rule (line 279).
- `tests/code_index/test_gcode_gateway.py::*` — scope-reason: drops the tests of the deleted methods and floors.
- `tests/code_index/test_gcode_versions.py`

Research context:
- The fifteen methods, by their 0.5.0 tip ranges (`gcode_gateway.py`): `checked_version`
  (419-421), `graph_sync_file` (423-445), `graph_overview` (447-457), `graph_file`
  (459-469), `graph_neighbors` (471-489), `graph_blast_radius` (491-512), `symbol_path`
  (514-540), `graph_clear` (542-546), `graph_rebuild` (548-552), `vector_sync_file`
  (554-576), `vector_clear` (578-600), `vector_rebuild` (602-606), `prune` (608-612),
  `maintenance_index` (614-626) and `prune_project_for_maintenance` (677-714). The two
  floors are at 30-31, and the `__all__` entry for `MIN_GCODE_PRUNE_BUDGET_VERSION` is at
  line 968. Each range sits between kept lines, as the proof grammar requires. The
  floors' readers are `GCODE_ALLOW_MISSING_INDEXED_FILE_VERSION` at lines 442 and 573 and
  `MIN_GCODE_PRUNE_BUDGET_VERSION` at lines 695 and 698, all inside deleted ranges.
- `gcode_gateway.py` is 969 lines at the 0.5.0 tip and sits above the 850-line growth
  lint, so base validation rejects exact symbol targets on it. The file entry is the
  pure-deletion proof in Targets, grounded on the tip blob.
- Re-grounding route, agreed by the Orchestrator (relayed 2026-10-18, 12:45 CDT). #24670
  moves the two floors into `gcode_versions.py` and edits this file's layout before this
  leaf starts. After #24670 closes and before 4.3 is claimed, the Writer re-grounds the
  proof on the gateway blob then at tip, with ranges from `gcode outline`, and deletes the
  two floors from `gcode_versions.py` by name. The re-grounding changes the plan's bytes,
  so it first withdraws any M1 that exists: the Writer removes the whole `## M1 Task
  Manifest` block together with its preceding blank line, and commits that removal with
  one dated entry in V1. The removal diff touches only the M1 block and adds only the
  changelog lines. The Plan Adversary (gobby#16993) then derives and applies a fresh M1
  and re-stamps the re-grounded proof before 4.3 is claimed. If no M1 exists at
  re-grounding time, the Writer records the dated V1 entry and nothing is withdrawn.
- The floor wording sits in three places. `crates/AGENTS.md` (line 110) and root
  `AGENTS.md` (line 238) each say to keep the managed pins and the gcode maintenance floor
  in step. The project rule at line 279 of `project-operating-policy.yaml` says a gcode
  release keeps `MIN_GCODE_PRUNE_BUDGET_VERSION` equal to the crate version. Each sentence
  goes. The bundled rule templates under `src/gobby/install/shared/` carry no such sentence.

**Acceptance:**

- 4.3.1 - Each of the fifteen named methods has no caller in `src/`, `tests/` or `crates/` at the leaf's commit, and the caller check is recorded in the close summary. test: `tests/code_index/test_gcode_gateway.py::test_gateway_keeps_no_route_only_methods`.
- 4.3.2 - `gcode_versions.py` no longer defines `MIN_GCODE_PRUNE_BUDGET_VERSION` or `GCODE_ALLOW_MISSING_INDEXED_FILE_VERSION`, and the floor test that read them is removed with them. test: `tests/code_index/test_gcode_versions.py::test_removed_floors_are_absent`.
- 4.3.3 - `crates/AGENTS.md`, root `AGENTS.md` and `project-operating-policy.yaml` no longer state a gcode maintenance version floor. file: `crates/AGENTS.md`, `AGENTS.md`, `.gobby/workflows/rules/project-operating-policy.yaml`.
- 4.3.4 - `gcode_gateway.py` stays under 1,000 lines after the deletion and keeps every method and error class that a surviving caller uses. test: `tests/code_index/test_gcode_gateway.py::test_surviving_gateway_methods_keep_their_callers`.

### 4.4 Re-point the audit entries at the native handlers [category: docs] (depends: 2.1, 4.1)
`kind: deliverable`

The parity-ledger rows leave in 4.1, because their status keys go there. This leaf
keeps only the audit re-point.

Targets:
- `docs/reference-audit/code-index.json::*` — scope-reason: re-points the five `create_code_index_router` entries at the native handlers.
- `tests/contracts/test_code_graph_ledger_rows.py` — rewritten to check the audit entries only.

Research context:
- `docs/reference-audit/code-index.json` line 755 names `create_code_index_router.graph_overview`,
  and four further entries name the same router.

**Acceptance:**

- 4.4.1 - No audit entry names `create_code_index_router`, and each of the five entries names its native handler (`family/graph.rs` or `family/search.rs`). test: `tests/contracts/test_code_graph_ledger_rows.py::test_audit_points_at_native_handlers`.

## E1 End-to-end verification
`kind: verification`

- The e2e file in 3.1 runs green through gdaemon before any deletion in P4 lands.
- The web UI's `useCodeGraph.ts` loads its five views through gdaemon with a cookie, and the same requests without a cookie answer 401.
- The focused runs named in each acceptance item pass. The full pytest suite does not run.

## V2: Verification
`kind: verification`

These are completion gates for the implementation. Except for plan validation, none has run yet. Each runs after the leaves land, and each must hold before the epic closes.

- Plan validation with `uv run gobby plans validate .gobby/plans/gdaemon-web-session-cookie.md -p <project root>` is the only planning-time check, and it must pass before review.
- The Security review is by Grok, gobby#17126. It reviews this draft's cookie verifier, including the duplicate-cookie split in 1.1 and the ambiguous-cookie refusal on the five paths in 2.1, before consensus. Its verdict is recorded in the consensus entry of V1, which precedes the Plan Adversary's M1 derivation, so one M1 covers the verdict. A finding that changes 1.1 or 2.1 reopens the narrative for a fresh review. The review lands before operator approval.
- Every deleted Python route must have zero callers at the leaf's commit, and each leaf must record the caller check in its close summary.
- `gcode_gateway.py` must stay under 1,000 lines after deliverable 4.3.
- The gdaemon version must advance by 0.0.1 in each shipping commit, 1.1 and 2.1, with `Cargo.lock` and the managed pin updated.
- `tests/test_parity_ledger.py::test_committed_ledger_matches_current_public_inventory` and `tests/contracts/test_http_corpus.py::test_case_replays_equal` must pass at 4.1's commit. Both are existing regression gates, and they are not acceptance items of this plan.

## V1 Plan Changelog
`kind: framing`

- 2026-10-18, Draft 1 (lane-4 developer seat, session 39fa53a8). Narrative written from the 0.5.0 tip. Decisions (a) to (c) recorded with recommended choices. No M1; the Adversary derives it after consensus.
- 2026-10-18, Draft 2 (lane-4 developer seat, session 39fa53a8). Applies the Plan Adversary's NEEDS REPAIR (gobby#16993, message d5f60fd0). B1: decisions (a) to (c) quoted and accepted by the Orchestrator at 12:06 CDT; Security reviewer Grok, gobby#17126, 2026-10-18. B2: `authenticate` takes the request, and the `FrontDoor::handle` call sites are targets. B3: duplicate cookies split by route, fail closed on gdaemon-served routes, pass through on proxied routes. B4: deliverable 1.2 removed; the bumps move into 1.1 and 2.1, with the managed pin. B5: the five keys leave the status file in 4.1, and 4.4 keeps the audit re-point only. B6: the HTTP corpus flips in 4.1. B7: 3.1 enables the index through `ConfigPatch`, indexes a fixture project, and compares against the Python backend port; a gap stops the leaf. B8(i): the fifteen methods and the `__all__` entry are named. B8(ii): the re-grounding route, agreed by the Orchestrator at 12:45 CDT, is recorded in 4.3. B9: all three floor-wording locations are targets, and the cross-plan release leaves are recorded in Constraints. N1: the agent-token loss is recorded under D15. N2: the new test files are targets. Version observed at the 0.5.0 tip `6ff393368a`: crate and pin both 0.4.61, not the 0.4.62 the Adversary's message cites.
- 2026-10-18, Draft 3 (lane-4 developer seat, session 39fa53a8). Applies the Plan Adversary's follow-up to Draft 2 (gobby#16993, message 41c87a52). R1: the 4.3 re-grounding route withdraws any M1 first, by removing the whole `## M1 Task Manifest` block, and then records a dated V1 entry. R2: 3.1 takes the FalkorDB prerequisite and config write of `tests/e2e/test_memory_dream_gc_e2e.py`, and fails rather than skips when the variables are missing; the stop-and-report branch is removed. The memory test skips only when all three variables are unset, which the new file does not copy. R3: `FrontDoor::handle` refuses an ambiguous cookie on the five GET paths with `401 missing_auth` before `table.dispatch`, through `gruntime::admits_browser`, with acceptance 2.1.5; 1.1.4 keeps the three routes that exist at 1.1. Gating the session lookup by the route table is not taken. R4: the Security review by gobby#17126 is taken on Draft 3 before consensus and recorded in the consensus entry, not after the M1 derivation. Nit (restraint): 1.1.10 and 2.1.4 use the existing `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`; the new pin test and its targets are dropped. Version unchanged from Draft 2: the crate and pin read 0.4.61 at the tip, and the 0.4.62 the Adversary cited came from the main checkout.
- 2026-10-18, Draft 4 (lane-4 developer seat, session 39fa53a8). Applies the Plan Adversary's second pass (gobby#16993), R5. `gruntime::admits_browser` takes the row slice, `admits_browser(rows, method, path)`, because gruntime cannot read gcodeindex's rows without a cycle. `FrontDoor::handle` passes the code-index family's public `NATIVE_ROUTES`, and gdaemon takes a path dependency on `gcodeindex` to name it. `RouteFamily` is unchanged. 2.1's scope adds `NATIVE_ROUTES` visibility if #24665 has not made it public. 2.1.5 is unchanged. The Security verdict from gobby#17126 is not yet recorded; it goes into the consensus entry, which is committed before the Adversary derives M1.
- 2026-10-18, Draft 5, the consensus candidate (lane-4 developer seat, session 39fa53a8). Folds the Adversary's R6 and Grok's Security verdict on Draft 3 (29bdbca230) into one commit, so one M1 covers both. R6: 2.1 adds no dependency on `gcodeindex`. gdaemon already depends on package `gobby-codeindex`, linked by the family plan (D5 and 1.9.1), and the `crates/gdaemon/Cargo.toml` target is the version bump only. This corrects the dependency statement in the Draft 4 entry. Security verdict, gobby#17126, dated 2026-10-18: FINDINGS none. Accepted: a proxied request with two distinct `gobby_session` values passes through unchanged. Python reads the last pair, and gdaemon does not authorize a proxied route from the cookie. The collector counts every pair, so a last-wins map cannot hide one. Observed, not a finding: on proxied routes a non-bearer `Authorization` header still reaches Python's cookie check (`auth_service.py` 399 to 409). The 1.1.3 rule covers gdaemon-served routes, and the Decision Record says so.

## M1 Task Manifest
`kind: manifest`

```yaml
- title: Verify the browser session cookie in the front door, and bump gdaemon
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: '1.1.1: An unexpired `gobby_session` cookie resolves through
    `KeyResolver::resolve_session` to its `user_id`, and the request carries `BrowserSession
    { user_id }` in its extensions. test: `crates/gdaemon/tests/front_door.rs::cookie_session_resolves_unexpired_user`.

    1.1.2: An expired, unknown or empty cookie, and a request without one, carries
    no `BrowserSession` and proceeds as unauthenticated does today. test: `crates/gdaemon/tests/front_door.rs::cookie_session_rejects_expired_and_unknown`.

    1.1.3: A request with an `Authorization` header is decided by the bearer path
    alone, and its `gobby_session` cookie is ignored. test: `crates/gdaemon/tests/front_door.rs::bearer_takes_precedence_over_cookie`.

    1.1.4: Two distinct `gobby_session` values on the lease-admin route, the machines
    API or the node channel are answered `401 missing_auth` before the route''s handler
    runs. test: `crates/gdaemon/tests/front_door.rs::duplicate_session_cookies_fail_closed_on_hub_routes`.

    1.1.5: Two distinct `gobby_session` values on a proxied route reach the backend
    with both `Cookie` values unchanged, and carry no `BrowserSession`. test: `crates/gdaemon/tests/front_door.rs::duplicate_session_cookies_pass_through_on_proxied_routes`.

    1.1.6: The same `gobby_session` value sent twice is accepted as that session.
    test: `crates/gdaemon/tests/front_door.rs::same_session_cookie_sent_twice_is_accepted`.

    1.1.7: Client identity headers are removed before the cookie branch runs, and
    no cookie-derived value is written to any identity header. test: `crates/gdaemon/tests/front_door.rs::cookie_session_sets_no_identity_headers`.

    1.1.8: `resolve_session` hashes the cookie with `api_key_format::hash` and filters
    `expires_at >= now()`. The Rust test pins the digest that Python''s `hash_token`
    returns for a fixture token. test: `crates/gdaemon/tests/front_door.rs::session_hash_matches_python_hash_token`.

    1.1.9: The lease-admin route stays API-key-only: a request whose only credential
    is a `gobby_session` cookie is refused as before. test: `crates/gdaemon/tests/front_door.rs::cookie_never_admits_lease_admin`.

    1.1.10: `crates/gdaemon/Cargo.toml` and the `Cargo.lock` entry for gdaemon carry
    the merged-base version plus 0.0.1, and the managed `gdaemon` pin in `version_pins.py`
    matches. The existing pin test checks the pair. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.'
  labels:
  - covers:gdaemon-web-session-cookie:1.1:1.1.1
  - covers:gdaemon-web-session-cookie:1.1:1.1.2
  - covers:gdaemon-web-session-cookie:1.1:1.1.3
  - covers:gdaemon-web-session-cookie:1.1:1.1.4
  - covers:gdaemon-web-session-cookie:1.1:1.1.5
  - covers:gdaemon-web-session-cookie:1.1:1.1.6
  - covers:gdaemon-web-session-cookie:1.1:1.1.7
  - covers:gdaemon-web-session-cookie:1.1:1.1.8
  - covers:gdaemon-web-session-cookie:1.1:1.1.9
  - covers:gdaemon-web-session-cookie:1.1:1.1.10
  tdd: true
  source_section: '1.1'
  implementation_domain: backend
- title: Family claims browser-session callers on the five graph read rows
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  validation_criteria: '2.1.1: `Caller::of` returns `Caller::Browser` for a request
    carrying `BrowserSession`, and `Caller::Operator` for a request carrying `x-gobby-key-id`,
    as before. test: `crates/gruntime/tests/caller.rs::browser_session_maps_to_browser_caller`.

    2.1.2: A `GET` with a browser session to each of the five paths is served natively,
    and a browser session on any other path, or on any other method, reaches Python
    unchanged. test: `crates/gdaemon/tests/front_door.rs::browser_session_claims_only_listed_graph_reads`.

    2.1.3: An API-key caller''s claims on every row are unchanged. test: `crates/gcodeindex/tests/family_claims.rs::api_key_callers_keep_listed_row_claims`.

    2.1.4: `crates/gdaemon/Cargo.toml`, `Cargo.lock` and the managed `gdaemon` pin
    carry the 1.1 version plus 0.0.1. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

    2.1.5: Two distinct `gobby_session` values on each of the five GET paths are answered
    `401 missing_auth` before dispatch, and the same two values on a path that no
    row admits to browser callers reach Python with both `Cookie` values unchanged.
    test: `crates/gdaemon/tests/front_door.rs::duplicate_session_cookies_fail_closed_on_graph_reads`.'
  labels:
  - covers:gdaemon-web-session-cookie:2.1:2.1.1
  - covers:gdaemon-web-session-cookie:2.1:2.1.2
  - covers:gdaemon-web-session-cookie:2.1:2.1.3
  - covers:gdaemon-web-session-cookie:2.1:2.1.4
  - covers:gdaemon-web-session-cookie:2.1:2.1.5
  tdd: true
  source_section: '2.1'
  implementation_domain: backend
- title: The web UI code-graph view loads through gdaemon with a cookie
  category: test
  task_type: feature
  depends_on:
  - '2.1'
  validation_criteria: '3.1.1: Each of the five requests returns 200 through gdaemon
    for a valid cookie, with the same top-level JSON keys as Python''s response from
    the Python backend port for the same project. test: `tests/e2e/test_code_graph_session_cookie.py::test_code_graph_views_load_through_gdaemon_with_cookie`.

    3.1.2: The same requests with no cookie, or with an expired cookie, receive the
    same 401 that Python answers today. test: `tests/e2e/test_code_graph_session_cookie.py::test_unauthenticated_graph_reads_stay_401`.'
  labels:
  - covers:gdaemon-web-session-cookie:3.1:3.1.1
  - covers:gdaemon-web-session-cookie:3.1:3.1.2
  tdd: false
  source_section: '3.1'
  assigned_agent: developer
- title: Delete the five Python graph read routes, their router and their ledger keys
  category: code
  task_type: feature
  depends_on:
  - '3.1'
  validation_criteria: '4.1.1: `GET /api/code-index/graph` with a valid cookie is
    answered by gdaemon, and no Python route is mounted for the five paths. test:
    `tests/servers/routes/test_code_index_retirement.py::test_code_index_read_routes_are_not_registered`.

    4.1.2: No module imports `gobby.servers.routes.code_index`, and `src/gobby/servers/routes/code_index.py`
    is absent. test: `tests/servers/routes/test_code_index_retirement.py::test_no_module_imports_code_index_router`.

    4.1.3: Each deleted route handler and each helper it used has zero callers in
    `src/` and `tests/` at the leaf''s commit, and the check is recorded in the close
    summary. test: `tests/servers/routes/test_code_index_retirement.py::test_deleted_route_helpers_have_no_callers`.

    4.1.4: The five graph keys are absent from the status file, and the committed
    ledger matches the generator''s output. test: `tests/servers/routes/test_code_index_retirement.py::test_parity_ledger_drops_graph_keys`.

    4.1.5: The `code_index` corpus family has gdaemon origin, and no manifest case
    names a deleted route. test: `tests/servers/routes/test_code_index_retirement.py::test_code_index_corpus_origin_is_gdaemon`.'
  labels:
  - covers:gdaemon-web-session-cookie:4.1:4.1.1
  - covers:gdaemon-web-session-cookie:4.1:4.1.2
  - covers:gdaemon-web-session-cookie:4.1:4.1.3
  - covers:gdaemon-web-session-cookie:4.1:4.1.4
  - covers:gdaemon-web-session-cookie:4.1:4.1.5
  tdd: true
  source_section: '4.1'
  implementation_domain: backend
- title: Delete CodeIndexContext's read methods that no caller reaches
  category: code
  task_type: feature
  depends_on:
  - '4.1'
  validation_criteria: '4.2.1: `gcode usages` for each of the four symbols returns
    no caller at the leaf''s commit, and the result is recorded in the close summary.
    test: `tests/code_index/test_code_index_context.py::test_context_has_no_graph_read_methods`.

    4.2.2: `CodeIndexContext` keeps every method that still has a caller, and the
    focused file `tests/code_index/test_code_index_context.py` passes. test: `tests/code_index/test_code_index_context.py::test_context_keeps_live_methods`.'
  labels:
  - covers:gdaemon-web-session-cookie:4.2:4.2.1
  - covers:gdaemon-web-session-cookie:4.2:4.2.2
  tdd: true
  source_section: '4.2'
  implementation_domain: backend
- title: Delete GcodeGateway's methods that only deleted code called, and the version
    floors
  category: code
  task_type: feature
  depends_on:
  - '4.2'
  validation_criteria: '4.3.1: Each of the fifteen named methods has no caller in
    `src/`, `tests/` or `crates/` at the leaf''s commit, and the caller check is recorded
    in the close summary. test: `tests/code_index/test_gcode_gateway.py::test_gateway_keeps_no_route_only_methods`.

    4.3.2: `gcode_versions.py` no longer defines `MIN_GCODE_PRUNE_BUDGET_VERSION`
    or `GCODE_ALLOW_MISSING_INDEXED_FILE_VERSION`, and the floor test that read them
    is removed with them. test: `tests/code_index/test_gcode_versions.py::test_removed_floors_are_absent`.

    4.3.3: `crates/AGENTS.md`, root `AGENTS.md` and `project-operating-policy.yaml`
    no longer state a gcode maintenance version floor. file: `crates/AGENTS.md`, `AGENTS.md`,
    `.gobby/workflows/rules/project-operating-policy.yaml`.

    4.3.4: `gcode_gateway.py` stays under 1,000 lines after the deletion and keeps
    every method and error class that a surviving caller uses. test: `tests/code_index/test_gcode_gateway.py::test_surviving_gateway_methods_keep_their_callers`.'
  labels:
  - covers:gdaemon-web-session-cookie:4.3:4.3.1
  - covers:gdaemon-web-session-cookie:4.3:4.3.2
  - covers:gdaemon-web-session-cookie:4.3:4.3.3
  - covers:gdaemon-web-session-cookie:4.3:4.3.4
  tdd: true
  source_section: '4.3'
  implementation_domain: backend
- title: Re-point the audit entries at the native handlers
  category: docs
  task_type: feature
  depends_on:
  - '2.1'
  - '4.1'
  validation_criteria: '4.4.1: No audit entry names `create_code_index_router`, and
    each of the five entries names its native handler (`family/graph.rs` or `family/search.rs`).
    test: `tests/contracts/test_code_graph_ledger_rows.py::test_audit_points_at_native_handlers`.'
  labels:
  - covers:gdaemon-web-session-cookie:4.4:4.4.1
  tdd: false
  source_section: '4.4'
  assigned_agent: tech-writer
```
