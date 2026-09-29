Plan artifact: `.gobby/plans/gdaemon-http-contract-corpus.md`

# Gobby 1.0 Stage 1 slice: HTTP contract corpus

**Plan ID:** gdaemon-http-contract-corpus

## Overview
`kind: framing`

This is the P3 slice (S1.5) of the Stage 1 front-door plan of record,
`.gobby/plans/gdaemon-front-door.md`. The root is the existing phase epic #21552
(HTTP contract corpus) under #21543. The slice is planned on its own so it can be
reviewed, approved, and expanded directly under #21552. Expanding the whole plan of
record would mint duplicate phase epics and stamp the P4 and P5 sections, which are
still unreviewed. The plan of record keeps a one-line pointer to this file.

The deliverable is a recorded request/response corpus for the proxied HTTP
surface. pytest and Rust both replay it, and it is the parity gate for every
Stage 2 native takeover. 3.1 defines the fixture format, the Python loader,
masking, recorder, and replay, and records the first corpus against the live e2e
daemon. 3.2 adds the Rust replay harness against `gdaemon serve` and the one
corpus family gdaemon authors itself: the typed 503 it returns while the backend
is down.

Section numbers 3.1 and 3.2 are kept from the plan of record so its changelog and
cross-references still read. The plan has one phase, P3. Expansion creates phase
sub-epics only for multi-phase plans (`src/gobby/tasks/expansion/_apply.py`), so
both deliverables expand as leaves directly under #21552.

## Constraints
`kind: framing`

- **P1 has landed.** These facts from P1 hold on `0.5.0` and are relied on below.
  - #23041 (1.1) added the bootstrap `front_door` block
    (`enabled`, `routes: {family: proxy | native | compare}`) and `backend_ports`
    in both languages.
  - #23043 (1.2) added `gdaemon serve`, the routing table, the WS splice, and the
    typed 503.
  - #23044 (1.4) made ghook treat the typed 503 as unreachable.
  - #23045 made the runner own the front door:
    `GobbyRunner` spawns `gdaemon serve` on the public ports, and Python binds
    `127.0.0.1` at public+100.
  - E2E daemons run in front-door mode. `GOBBY_TEST_GDAEMON=checkout` selects the
    worktree's `target/debug/gdaemon`, per `tests/fixtures/gdaemon_binary.py`.
- **No backward compatibility.** `schema_version` starts at 1. The API-key leaf
  of the plan of record (4.3) deletes the `X-Gobby-Local-Token` alias, re-records
  the auth cases, and bumps `schema_version` to 2. This slice does not record that
  alias.
- **Test isolation.** Python runs use
  `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1`
  and the isolated e2e daemon; no case touches the user's daemon. Rust runs are
  in-process and bind only `127.0.0.1:0`.
- **Rust conventions.** Load the `rust` skill before editing crates;
  `crates/CLAUDE.md` governs. This slice adds a test file only, so no binary
  is reinstalled.

## P3: HTTP contract corpus (S1.5, #21552)
`kind: framing`

**Goal**: a recorded request/response corpus for the proxied surface that pytest
and Rust both replay; the parity gate for every Stage 2 takeover.

### 3.1 Fixture format, Python recorder and replay, first corpus [category: test]
`kind: deliverable`

Targets:
- `tests/contracts/__init__.py`
- `tests/contracts/http_corpus.py`
- `tests/contracts/test_http_corpus.py`
- `tests/contracts/http/manifest.json`
- `tests/contracts/http/README.md`
- `tests/contracts/http/mask_vector.json`
- `tests/contracts/http/health_ok.json`
- `tests/contracts/http/config_schema.json`
- `tests/contracts/http/config_values.json`
- `tests/contracts/http/tasks_list.json`
- `tests/contracts/http/runtime_handshake_challenge.json`
- `tests/contracts/http/runtime_handshake.json`
- `tests/contracts/http/auth_missing_auth.json`
- `tests/contracts/http/auth_missing_grant.json`
- `tests/contracts/http/auth_forged_identity.json`

**Granularity:** one deliverable. Past the three Python modules, every Target
is a fixture or document the recorder writes. The loader, masking, recorder,
and replay are one contract, and one test module proves it.

**Research context:**

Fixture format (`schema_version` 1): one case per JSON file under
`tests/contracts/http/`, for example:

```json
{
  "schema_version": 1,
  "name": "health_ok",
  "family": "health",
  "credential": "none",
  "request": {"method": "GET", "path": "/api/health", "query": {}, "headers": {}, "body": null},
  "response": {"status": 200, "headers": {"content-type": "application/json"}, "body": {"status": "@mask@", "hook_runtime": "@mask@"}},
  "mask": ["/response/body/status", "/response/body/hook_runtime"]
}
```

`manifest.json` has these fields:
- `schema_version`, the corpus version.
- `cases`, the case file names in order.
- `families`, with one entry per family:
  `{"parity": "proxy" | "native", "origin": "python" | "gdaemon"}`.
  - `parity` is the route backend the Rust harness replays the family under. The
    `health` family is `native`, because gdaemon implements it
    (`crates/gdaemon/src/front_door/routes.rs::FAMILIES`). Every other family in
    this corpus is `proxy`. Stage 2 flips a family to `native` when gdaemon gains
    a handler for it.
  - `origin: python` means the recorder captures the case from the live
    daemon.
  - `origin: gdaemon` means the case is authored from a gdaemon contract and
    has no Python counterpart. Python neither records nor replays it. 3.2
    adds the first such family (`front_door`).

The response-header allowlist, applied on both sides, is `cache-control`,
`content-type`, `retry-after`, `x-gobby-user-id`, `x-gobby-machine-id`, and
`x-gobby-key-id`. `mask` names
volatile fields by RFC 6901 JSON pointer. Both harnesses replace them with
`"@mask@"` before comparing, and the recorder applies the masks at write time
so recordings are deterministic. The loader rejects any case whose
`schema_version` differs from the manifest's.

Request reconstruction: a persisted case is an executable recipe, and replay
always starts from the committed file.
- `request` stores the exact method, path, query, and body that Python sends,
  with no `Authorization` or runtime-grant header. Every request value in the
  first corpus is a fixed non-secret constant, so no `mask` pointer enters
  `request`.
- `credential` names the header recipe that a test-local materializer in
  `http_corpus.py` adds immediately before Python sends:
  - `none` adds nothing;
  - `operator` adds `daemon_auth_headers(daemon_instance.gobby_home)`, an
    ordinary helper in `tests/e2e/conftest.py`;
  - `grant` adds `BoundaryHarness.grant_headers()` from the `boundary` fixture.
- Credentials per case:
  - `none`: `health_ok`, `runtime_handshake_challenge`, and `auth_missing_auth`.
  - `operator`: `config_schema`, `config_values`, `tasks_list`,
    `runtime_handshake`, and `auth_missing_grant`.
  - `grant`: `auth_forged_identity`.
- Normalization (secret redaction, then masks) applies only to the copy the
  recorder writes and the copy replay compares. The request that is sent is
  never normalized.

The Rust harness replays against a stub backend and needs no credential. No new
credential abstraction is added.

Secret redaction is mandatory. It runs before masking on every body the
recorder writes or replay compares, independent of `mask` and of whether a value
varies:
- Any object key named `dsn`, `password`, `api_key`, `token`,
  `deployment_token`, `payload_checksum`, `signature`, or `proof`, at any depth,
  has its value replaced with `"@secret@"`.
- That set covers the grant's capability credentials (`PostgresDirect.dsn`,
  `FalkorDirect.password`, and `QdrantDirect.api_key` in
  `src/gobby/runtime_grants/schema.py`), `GrantDeployment.token`, the grant's
  `payload_checksum` and `signature`, the top-level `deployment_token`, and the
  challenge `proof`.
- The grant's volatile non-secret fields are masked by pointer, keeping the
  key and replacing only the value. They are the time and generation fields
  (`issued_at`, `expires_at`, each capability's `valid_until` and
  `credential_generation`), the top-level `fencing_epoch`, the nested
  `/response/body/grant/deployment/fencing_epoch` (`GrantDeployment`), and
  `/response/body/grant/capabilities/postgres/role_name`, which the database
  derives from the deployment token, machine, project, and generation, so it
  changes across fresh fixture homes.
- Every grant field outside those two sets keeps its shape and value. The gate
  is replay of the committed file against a fresh fixture, where every
  volatile value differs from the recording.
- The Rust harness applies the same key set, and `mask_vector.json` gains a
  redaction case.

`mask_vector.json` is a shared vector: `{"input", "mask", "expected"}`. It
covers the following, and 3.2 asserts the Rust helper produces the same
`expected`:
- a nested object pointer;
- an array index;
- the `~0` and `~1` escapes;
- a pointer to an absent field, which is left absent and not created.

Modules:
- `tests/contracts/http_corpus.py` holds the loader, mask, compare, and
  `record_cases(client, cases, out_dir)` helpers. It uses the stdlib only
  (`json` plus a small pointer walker).
- `tests/contracts/test_http_corpus.py`:
  - binds every fixture it needs explicitly. `tests/contracts` is a sibling of
    `tests/e2e`, and importing a conftest module does not register its
    fixtures.
    - `daemon_instance`, `e2e_config`, and `e2e_project_dir` are rebound from
      `tests.e2e.conftest`, as `tests/mcp_proxy/test_annotate_mcp.py` does.
    - `boundary` is rebound from `tests.e2e.test_runtime_boundary` (line 869).
      It yields `BoundaryHarness` and needs only `daemon_instance` and
      `e2e_project_dir`.
    - A module-local `e2e_pre_daemon_setup(postgres_db, postgres_schema)`
      replaces the no-op in `tests/e2e/conftest.py` (line 920). It performs the
      identity and capability part of the boundary module's setup: the two
      schema GRANTs to `gobby_gcode_capability`, the test user insert with the
      `tests/fixtures/postgres.py` constants, and
      `_seed_identity_rows(postgres_db, E2E_MACHINE_ID, TEST_USER_ID)`. It sets
      no code-index or generation config and starts no summary LLM server. The
      boundary module's own `e2e_pre_daemon_setup` is never bound.
    - `postgres_db`, `postgres_schema`, and `postgres_database_url` come from
      `tests/fixtures/postgres.py`, as for every test.
  - replays every `origin: python` case from its committed file against the
    fixture and asserts equality after normalization. It is parametrized by case
    name and never replays an in-run recording. The fixture is consumed and not
    modified.
  - records when `GOBBY_RECORD_HTTP_CONTRACTS=1`: `test_record_http_contracts`
    is skipped unless that variable is set, and otherwise rewrites the case
    files in place. An environment switch avoids a `pytest_addoption` hook,
    which registers only when its conftest directory is on the command line.
- `tests/contracts/__init__.py` makes the directory a package, like its
  siblings, so `tests.contracts.http_corpus` imports.

First corpus: one pinned request and credential per case, all against the
isolated e2e daemon, which runs behind gdaemon.
- `health_ok`: `GET /api/health`, family `health`.
  - `health_check` (`src/gobby/servers/routes/admin/_health.py`, inside
    `create_health_router`) returns `status`, `degraded_services`,
    `hook_runtime`, and `install_dir`, plus `gterm_host` when present.
  - Mask whichever of these vary between two recordings.
- `config_schema` and `config_values`: `GET /api/config/schema` and
  `GET /api/config/values` (`src/gobby/servers/routes/configuration_values.py`),
  family `config`. Mask ports, paths, and generated identifiers.
- `tasks_list`: `GET /api/tasks?project_id=00000000-0000-0000-0000-000000000e2e`
  (`E2E_PROJECT_ID`) with credential `operator`, family `tasks`. The query
  names the project, so `list_tasks` never falls back to ambient project
  resolution. That project has no tasks, so the recorded body is the empty
  listing.
- `runtime_handshake_challenge` and `runtime_handshake`: family
  `runtime_handshake`.
  - `POST /api/runtime/handshake/challenge` (a `_PUBLIC_PATHS` entry in
    `src/gobby/servers/middleware/auth.py`) with body
    `{"nonce": "AAAAAAAAAAAAAAAAAAAAAA"}`, a fixed 16-byte base64url nonce within
    `_CHALLENGE_NONCE_MAX_CHARS`. `challenge` rejects any `Authorization`
    (`credential_before_proof`), so the credential is `none`.
  - `POST /api/runtime/handshake` with credential `operator` and body
    `{"machine_id": E2E_MACHINE_ID, "project_id": E2E_PROJECT_ID,
    "session_id": "21000000-0000-4000-8000-00000000c0de"}`. The two ids are the
    constants `_handshake_grant` sends (`tests/e2e/test_runtime_boundary.py`);
    `HandshakeService.issue_for_operator` requires the admitted machine and
    project.
  - Both handlers return `Cache-Control: no-store`
    (`src/gobby/servers/routes/runtime_handshake.py`, `challenge` and
    `handshake`); the allowlist records it and replay pins it.
  - Redaction covers `proof`, `deployment_token`, and the grant secrets. The
    handshake masks `/response/body/fencing_epoch`,
    `/response/body/grant/deployment/fencing_epoch`,
    `/response/body/grant/capabilities/postgres/role_name`, and the grant's
    `issued_at`, `expires_at`, and each capability's `valid_until` and
    `credential_generation`.
- Family `auth`: every body is `{"error": <message>, "code": <code>}`, built by
  `AuthMiddleware.dispatch` in `src/gobby/servers/middleware/auth.py`. The
  codes come from `authenticate` in `src/gobby/servers/auth_service.py`.
  - All three cases send `POST /api/embeddings` with body `{"input": ["hi"]}`,
    the fixed JSON entry of `_MODALITY_ROUTES`
    (`tests/e2e/test_runtime_boundary.py`, line 207). It needs no request id or
    multipart normalization.
  - `auth_missing_auth`: credential `none`, so no bearer and no grant header.
    - Code `missing_auth`: `authenticate` returns it when no bearer is accepted
      (`auth_service.py`, line 278).
    - Message `_LOGIN_GUIDANCE` (`middleware/auth.py`, lines 50-53):
      "Authentication required. CLI clients need ~/.gobby/local_cli_token (run
      'gobby install' or 'gobby auth token --rotate'). Browsers: log in."
    - This one case is also the generic Authentication-required message:
      `_LOGIN_GUIDANCE_CODES` routes `missing_auth` to that text.
    - It must be recorded as `missing_auth`. An anonymous request can yield
      either code, so it must never be recorded as `missing_grant`
      (`tests/servers/test_auth_service.py`, line 931).
  - `auth_missing_grant`: credential `operator`, so the operator bearer only
    and no grant header.
    - Code `missing_grant` (`auth_service.py`, lines 282 and 284).
    - Message `_GRANT_REJECTION_MESSAGE`, which is "Request rejected".
  - `auth_forged_identity`: credential `grant` plus the persisted headers
    `X-Gobby-Machine-Id: forged-machine` and `X-Gobby-Project-Id: forged-project`,
    as in `test_modality_identity_binding`.
    - Code `forged_identity` (`auth_service.py`, lines 300 and 302).
    - Message "Request rejected".

Removed from the plan of record's first corpus:
- The typed 503 moves to 3.2. It is gdaemon-authored, and the e2e backend is
  always up, so Python cannot record it.
- The HTTP-200 internal-error envelope in `src/gobby/servers/exception_handlers.py`
  (`global_exception_handler`) is emitted only for an uncaught exception on a
  `/api/hooks` path; other paths get a 500. No deterministic live request raises
  one. The Stage 2 leaf that takes over the hooks family records its own error
  cases.

`README.md` documents the fixture format, the manifest fields including
`origin`, the header allowlist, the mask rules, and the re-record procedure
(`GOBBY_RECORD_HTTP_CONTRACTS=1` with the isolated test hub). It also states
that a `schema_version` bump re-records every case.

Verification planned: `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/contracts/test_http_corpus.py -v`.

**Acceptance:**

- 3.1.1 - On a synthetic manifest, the loader rejects a case whose `schema_version` differs from the manifest's and skips `origin: gdaemon` families. test: `tests/contracts/test_http_corpus.py::test_loader_rejects_version_and_filters_origin`.
- 3.1.2 - The recorder writes every `origin: python` case deterministically: two recordings against one fixture produce identical bytes, and those bytes contain none of the daemon token, the encoded runtime grant, the nested grant token, or the `deployment_token`. test: `tests/contracts/test_http_corpus.py::test_recorder_is_deterministic`.
- 3.1.3 - Every first-corpus case replays equal from its committed file through the front-door e2e fixture with freshly injected credentials, including `Cache-Control: no-store` on both runtime-handshake cases, and the module collects and runs under the focused standalone command. test: `tests/contracts/test_http_corpus.py::test_case_replays_equal`.
- 3.1.4 - The Python mask helper produces the shared vector's `expected` output. test: `tests/contracts/test_http_corpus.py::test_mask_vector_matches_expected`.
- 3.1.5 - Every manifest family has a valid `parity` and `origin`, every listed case file exists, and every case's `family` is a manifest family. test: `tests/contracts/test_http_corpus.py::test_manifest_contract`.
- 3.1.6 - The README documents the format, the `credential` recipes, the redaction key set, the masks, and the re-record procedure. file: `tests/contracts/http/README.md`.
- 3.1.7 - A synthetic grant whose `dsn`, `password`, `api_key`, deployment `token`, `payload_checksum`, and `signature` are identical in two recordings writes none of them, and every non-secret grant field keeps its shape. test: `tests/contracts/test_http_corpus.py::test_secret_redaction_is_unconditional`.

### 3.2 Rust replay harness and the gdaemon-authored front_door family [category: test] (depends: 3.1)
`kind: deliverable`

Targets:
- `crates/gdaemon/tests/http_contracts.rs`
- `tests/contracts/http/front_door_backend_down.json`
- `tests/contracts/http/manifest.json`

**Research context:**

Landed front door (#23043):
- `gdaemon::serve::serve(listeners, &routes, shutdown)`
  (`crates/gdaemon/src/serve.rs`) runs the front door in-process over
  `PublicListener { listener, backend }`.
- `crates/gdaemon/tests/common/mod.rs::start_front_door` wraps it with an empty
  routes map.
- `common::refused_addr` returns a closed loopback port, and `common::TIMEOUT` is
  10s.
- `crates/gdaemon/tests/front_door.rs` holds the client pattern:
  `send`, a hyper client, and `http_body_util::BodyExt::collect`.

The harness declares `mod common;` for `TIMEOUT` and calls `serve` directly
with a routes map built from the manifest. `common` needs no edit.
`refused_addr` drops its listener before gdaemon dials, so another process can
take the port in that gap; the harness therefore does not use it.

Routing facts:
- `crates/gdaemon/src/front_door/routes.rs::FAMILIES` is `&[&HealthFamily]`.
  `RouteTable::new` gives each family its bootstrap `RouteBackend`, defaulting
  to proxy.
- `crates/gdaemon/src/front_door/health.rs::native_health` answers
  `/api/health` by forwarding to the backend (`proxy::forward`). It then sets
  `SERVED_BY_HEADER` (`x-gobby-served-by: gdaemon`).
- A native health replay therefore needs the stub backend running. The served-by
  header is outside the corpus allowlist, so the harness asserts it separately
  to prove the native path answered.

Typed 503 (`health.rs`):
- While the backend refuses connections, `unavailable` returns 503 with
  `Retry-After: 1`, `content-type: application/json`, and body
  `{"status":"unavailable","backend":{"state":"down","target":"127.0.0.1:<port>"}}`.
- `BackendState` is `Down` until the plan of record's 5.2 supervisor reports
  `Starting`.
- `crates/gdaemon/tests/front_door.rs::refused_backend_returns_typed_503` pins
  this body today on `/api/sessions`.

Harness (`crates/gdaemon/tests/http_contracts.rs`, a single file):
- It reads `tests/contracts/http/manifest.json` via
  `concat!(env!("CARGO_MANIFEST_DIR"), "/../../tests/contracts/http/manifest.json")`.
  `crates/gcode/tests/contract.rs` shows the precedent for relative paths.
- The Rust loader, mask, and compare helpers stay private to this file. They use
  `serde_json::Value::pointer_mut`, so there is no new dependency, and behave
  exactly like `tests/contracts/http_corpus.py`.
- For each `origin: python` case, it starts a case-driven hyper stub backend on
  `127.0.0.1:0` that records the request it receives and answers with the
  recorded status, allowlisted headers, and body. It then runs `serve` with
  `routes = {family: parity}`, sends the request through the front door,
  normalizes, and asserts equality. It also asserts that the stub received the
  case's method, path, query, body, and allowlisted request headers, so
  response equality cannot pass after request corruption.
- For `origin: gdaemon` cases, `routes` is empty, so the path takes the
  proxy fallback, and a private helper binds a
  `tokio::net::TcpSocket` on `127.0.0.1:0` without listening and holds it until
  after the assertion, so connections to that address are refused for the whole
  replay.

The new `front_door` family:
- `manifest.json` gains
  `"front_door": {"parity": "native", "origin": "gdaemon"}` and the case
  `front_door_backend_down.json`.
- The case is authored by hand from the body above:
  - request `GET /api/sessions`;
  - response 503 with `content-type` and `retry-after`;
  - mask `/response/body/backend/target`, because the port varies.
- `front_door` is not a `RouteFamily` in `FAMILIES`. The typed 503 applies to
  every proxied path, so the completeness precheck below does not consider this
  family.

Parity precheck, run before replay: a private function over the manifest and
`FAMILIES` returns every violation by name, and the suite fails on any.
- Every name in `FAMILIES` has at least one manifest case.
- Every name in `FAMILIES` has manifest parity `native`, so adding a native
  handler without flipping its family fails.
- `routes::unimplemented_families(FAMILIES, routes)`, applied to the manifest's
  parity map, is empty, so native parity on a family gdaemon lacks fails.
  `RouteTable::new` would otherwise proxy it silently. The one exemption is
  `front_door`, the synthetic `origin: gdaemon` family.

This keeps the Stage 2 gate closed on a forgotten parity flip or a missing
recording.

Verification planned: `cargo test -p gobby-daemon --test http_contracts`, then
the 3.1 pytest command to confirm the Python replay still skips the new
`origin: gdaemon` family.

**Acceptance:**

- 3.2.1 - Every `proxy`-parity case replays equal through `gdaemon serve` against the case-driven stub, and the stub received the case's request unchanged. test: `crates/gdaemon/tests/http_contracts.rs::proxy_families_replay_equal`.
- 3.2.2 - The `health` family replays equal under `native` routing and carries `x-gobby-served-by: gdaemon`. test: `crates/gdaemon/tests/http_contracts.rs::native_health_replays_equal`.
- 3.2.3 - The `front_door` family's typed 503 replays equal against a backend address held bound and non-listening through the replay. test: `crates/gdaemon/tests/http_contracts.rs::front_door_backend_down_replays_equal`.
- 3.2.4 - Rust masking produces the shared vector's `expected` output. test: `crates/gdaemon/tests/http_contracts.rs::mask_matches_python_vector`.
- 3.2.5 - Every family in `FAMILIES` has at least one manifest case with `native` parity, and no other family except `front_door` has `native` parity. test: `crates/gdaemon/tests/http_contracts.rs::every_native_family_has_corpus_cases`.
- 3.2.6 - On synthetic manifests, the precheck names a registered family with no case, a registered family with `proxy` parity, and an unregistered family with `native` parity. test: `crates/gdaemon/tests/http_contracts.rs::precheck_rejects_missing_or_mismatched_parity`.

## V1: Plan Changelog
`kind: framing`

- 2026-09-29: Sliced out of the plan of record's P3 under #23098 (option (a),
  PD-confirmed), rooted at #21552, and refreshed against the landed P1 code.
  - The typed 503 moves from 3.1 to 3.2 as the gdaemon-authored `front_door`
    family, replayed against a refused backend. 3.1 stays on the unchanged e2e
    `daemon_instance`.
  - The 401 cases cite the verified sources and collapse the generic message into
    `missing_auth`. Lane7 (gobby#14682) verified the codes and the
    `forged_identity` setup.
  - The internal-error envelope drops out, with no deterministic live trigger.
  - Native health forwards through gdaemon, so its replay keeps the stub backend
    and asserts the served-by header.
  - Recording uses an environment switch, and the Rust helpers live in the
    harness file.
- 2026-09-29: Enhancer pass (run f3bc0791); the PD accepted all five. E1 pins
  `Cache-Control: no-store` on both handshake cases. E2 keeps credentials out of
  fixtures behind a test-local materializer, with a recorded-bytes secret
  assertion. E3 pins the 401 cases to `POST /api/embeddings`. E4 names the loader
  and manifest tests and moves the README to 3.1.6. E5 holds the refused backend
  address bound and non-listening through the 3.2 replay.
- 2026-09-29: Adversary review (gobby#14579) at 02f0274, blocking findings
  HC-01 to HC-04 accepted. HC-01: cases are executable recipes with fixed
  non-secret request constants and a credential per case; normalization touches
  only the written and compared copies. HC-02: the test module binds its full
  fixture set, including a module-local identity setup. HC-03: key-based secret
  redaction runs regardless of volatility (3.1.7). HC-04: the precheck requires
  native parity for registered families, rejects it elsewhere (3.2.6), and the
  stub asserts the received request.
- 2026-09-29: Adversary recheck at dbc53f7. HC-02 to HC-04 resolved. The HC-01
  residual is accepted: the nested deployment `fencing_epoch` and the Postgres
  `role_name` are masked by pointer, only the enumerated volatile fields
  change, and `tasks_list` names `E2E_PROJECT_ID` in its query.
- 2026-09-29: Consensus with the Adversary (gobby#14579) at 9c519b3. HC-01 to
  HC-04 and the normalization residual are resolved, with no open design
  objection. The Adversary derives and applies M1 from these bytes.

## V2: Verification
`kind: verification`

After each leaf, and before the PD lands the branch:

```bash
DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/contracts/test_http_corpus.py -v
cargo test -p gobby-daemon --test http_contracts
uv run gobby plans validate .gobby/plans/gdaemon-http-contract-corpus.md -p /Users/josh/Projects/gobby
```

The Rust command runs only after 3.2 and needs PD clearance while a load breach
is active. Do not run the full pytest suite.

## M1 Task Manifest
`kind: manifest`

```yaml
- title: Fixture format, Python recorder and replay, first corpus
  category: test
  task_type: feature
  depends_on: []
  validation_criteria: '3.1.1: On a synthetic manifest, the loader rejects a case
    whose `schema_version` differs from the manifest''s and skips `origin: gdaemon`
    families. test: `tests/contracts/test_http_corpus.py::test_loader_rejects_version_and_filters_origin`.

    3.1.2: The recorder writes every `origin: python` case deterministically: two
    recordings against one fixture produce identical bytes, and those bytes contain
    none of the daemon token, the encoded runtime grant, the nested grant token, or
    the `deployment_token`. test: `tests/contracts/test_http_corpus.py::test_recorder_is_deterministic`.

    3.1.3: Every first-corpus case replays equal from its committed file through the
    front-door e2e fixture with freshly injected credentials, including `Cache-Control:
    no-store` on both runtime-handshake cases, and the module collects and runs under
    the focused standalone command. test: `tests/contracts/test_http_corpus.py::test_case_replays_equal`.

    3.1.4: The Python mask helper produces the shared vector''s `expected` output.
    test: `tests/contracts/test_http_corpus.py::test_mask_vector_matches_expected`.

    3.1.5: Every manifest family has a valid `parity` and `origin`, every listed case
    file exists, and every case''s `family` is a manifest family. test: `tests/contracts/test_http_corpus.py::test_manifest_contract`.

    3.1.6: The README documents the format, the `credential` recipes, the redaction
    key set, the masks, and the re-record procedure. file: `tests/contracts/http/README.md`.

    3.1.7: A synthetic grant whose `dsn`, `password`, `api_key`, deployment `token`,
    `payload_checksum`, and `signature` are identical in two recordings writes none
    of them, and every non-secret grant field keeps its shape. test: `tests/contracts/test_http_corpus.py::test_secret_redaction_is_unconditional`.'
  labels:
  - covers:gdaemon-http-contract-corpus:3.1:3.1.1
  - covers:gdaemon-http-contract-corpus:3.1:3.1.2
  - covers:gdaemon-http-contract-corpus:3.1:3.1.3
  - covers:gdaemon-http-contract-corpus:3.1:3.1.4
  - covers:gdaemon-http-contract-corpus:3.1:3.1.5
  - covers:gdaemon-http-contract-corpus:3.1:3.1.6
  - covers:gdaemon-http-contract-corpus:3.1:3.1.7
  tdd: false
  source_section: '3.1'
  assigned_agent: backend-developer
- title: Rust replay harness and the gdaemon-authored front_door family
  category: test
  task_type: feature
  depends_on:
  - '3.1'
  validation_criteria: '3.2.1: Every `proxy`-parity case replays equal through `gdaemon
    serve` against the case-driven stub, and the stub received the case''s request
    unchanged. test: `crates/gdaemon/tests/http_contracts.rs::proxy_families_replay_equal`.

    3.2.2: The `health` family replays equal under `native` routing and carries `x-gobby-served-by:
    gdaemon`. test: `crates/gdaemon/tests/http_contracts.rs::native_health_replays_equal`.

    3.2.3: The `front_door` family''s typed 503 replays equal against a backend address
    held bound and non-listening through the replay. test: `crates/gdaemon/tests/http_contracts.rs::front_door_backend_down_replays_equal`.

    3.2.4: Rust masking produces the shared vector''s `expected` output. test: `crates/gdaemon/tests/http_contracts.rs::mask_matches_python_vector`.

    3.2.5: Every family in `FAMILIES` has at least one manifest case with `native`
    parity, and no other family except `front_door` has `native` parity. test: `crates/gdaemon/tests/http_contracts.rs::every_native_family_has_corpus_cases`.

    3.2.6: On synthetic manifests, the precheck names a registered family with no
    case, a registered family with `proxy` parity, and an unregistered family with
    `native` parity. test: `crates/gdaemon/tests/http_contracts.rs::precheck_rejects_missing_or_mismatched_parity`.'
  labels:
  - covers:gdaemon-http-contract-corpus:3.2:3.2.1
  - covers:gdaemon-http-contract-corpus:3.2:3.2.2
  - covers:gdaemon-http-contract-corpus:3.2:3.2.3
  - covers:gdaemon-http-contract-corpus:3.2:3.2.4
  - covers:gdaemon-http-contract-corpus:3.2:3.2.5
  - covers:gdaemon-http-contract-corpus:3.2:3.2.6
  tdd: false
  source_section: '3.2'
  assigned_agent: backend-developer
```
