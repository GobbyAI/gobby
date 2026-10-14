Plan artifact: `.gobby/plans/gdaemon-runtime-family.md`

# Gobby 1.0 Stage 2: runtime family (config, runtime handshake, grant issuing)

**Plan ID:** gdaemon-runtime-family

## Overview
`kind: framing`

Epic #21559 is the first Stage 2 takeover and the template for every later route
family. gdaemon serves `/api/runtime/*` and `/api/config/*` routes from a new
statically linked family crate, `crates/gruntime` (package `gobby-runtime`). Python
stays behind it as the proxy target and the rollback path. The plan also makes the
front door's `Compare` backend real. Compare mode records outcomes durably, and
`gdaemon compare-report` turns a soak window into a pass/fail verdict.

At close, Rust serves:

- the runtime handshake and grant presentation for interactive `gcode` principals
  (P3) and for managed principals, meaning agent runs, tool chat and maintenance
  (P4), including the managed challenge and the rotation-time `grant.json` rewrite;
- all nineteen `/api/config` routes: `GET /api/config/effective` (3.1) and the
  eighteen P5 routes, which are the public schema, the four prompt routes, secret
  listing, saving and deletion, the YAML template read and save, import and export,
  configuration values and their patches, UI settings, global tool approvals,
  generation-endpoint activation and the validation-detection preview.

Python keeps activating configuration and publishes its active state to a table
(2.2) that every native config read serves. Native writes commit under Python's
lock and revision rules, and the existing change notification starts Python's
activation.

`native` is the built-in default for the `runtime` family, the operator's daemon
runs on that default, and `proxy` is the documented fallback. The three exit
criteria in #21559 become this template:

1. A compare soak with zero mismatches over a representative window.
2. Killing the Python backend leaves the delegated routes serving.
3. Flipping the routing table, and rolling it back, needs no redeploy.

## Constraints
`kind: framing`

- **Stage gate.** The #21559 epic is `blocked-by` Stage 1 (#21543) in the task
  graph, so no leaf starts before Stage 1 closes. Stage 1 leaves #23457, #23458 and
  #23273 touch `crates/gdaemon/src/serve.rs`, `crates/gdaemon/src/state.rs` and
  `crates/gdaemon/src/lease/mod.rs`. Executors re-resolve those Targets against
  the post-Stage-1 tree before editing.
- **No backward compatibility.** 0.5.0 has not shipped.
- **Python stays as the rollback target.** Python keeps every route under
  `src/gobby/servers/routes/` that the family ports, and serves every request the
  native family does not claim. P5 leaves edit Python route modules in place, and
  no leaf deletes a route. Deleting the ported routes belongs to the Python
  retirement stage. This plan defers no acceptance obligation to that stage.
- **End-state boundary.** These stay in Python as the end state, with no later
  leaf or stage owing a port:
  - launch-time managed credential creation
    (`src/gobby/runtime_grants/launch.py::materialize_managed_launch`) and the
    background rotation loop
    (`src/gobby/storage/managed_credentials.py::ManagedCredentialManager.rotate_due`);
  - cookie-authenticated traffic, loopback break-glass admission and session
    reference spellings such as `#42`, which the family never claims (Decision
    Record, Family identity).
- **Release ownership.** Each leaf bumps by 0.0.1, from the executor's starting
  tip, every shipped binary whose crate or recursive path dependencies it changes,
  and updates `src/gobby/install/version_pins.py::MANAGED_BIN_VERSION_PINS` and
  `Cargo.lock` with it (`tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`).
  - A change to gcore or the root manifest bumps `gdaemon`, `gcode` and `ghook`.
  - A change confined to gruntime or gdaemon bumps `gdaemon` only, because
    gruntime is in gdaemon's release closure alone.
  - `gclient` and `gterm` are unpublished (`UNPUBLISHED_MANAGED_BINS`), so no leaf
    bumps them.
  - The source identity skips every path with a `tests`, `benches`, `examples` or
    `docs` component (`src/gobby/install/bin_release_identity.py::workspace_source_identity`),
    so a leaf that changes only crate test files, such as 6.2, bumps nothing.
- **Running daemon.** A crate change goes live only after `uv run gobby cutover`.
  A routing flip edits `front_door.routes` in `~/.gobby/bootstrap.yaml` and restarts
  gdaemon with the same binaries. That restart gets a `global` announcement and a
  quiet window first.
- **Heavy work.** Every granted cargo run sets `CARGO_BUILD_JOBS=3`. Scope runs with
  `-p` (`gobby-core`, `gobby-daemon`, `gobby-runtime`).
- **Dependencies.** No new external crate enters `Cargo.lock`, with one named
  exception: `icu_casemap` 2.1.1 and its compiled data `icu_casemap_data` 2.1.1,
  which 5.7's `casefold` port needs and no locked crate provides. A leaf that adds
  any other package to the lock fails E1's repo gates. axum (0.8,
  `default-features = false`) enters gcore only behind the new `route-family`
  feature. Crypto uses `openssl` (a direct gcore dependency, vendored) and `fernet`
  0.2.2 (already locked).
- **Secrets never leave the process.** Neither the compare log nor any error body
  carries a response body value. Mismatch details name a JSON pointer only.
- **Monolith ceiling.** Every targeted Rust file stays under 1,000 lines, counting
  inline `#[cfg(test)]` modules. New tests go in `tests/` files or in
  `<module>/tests.rs`.

## Decision Record
`kind: framing`

These rulings came from Orchestrator #15977 on 2026-10-13 and 2026-10-14, with
operator approval under stage2-plan:now. Entries marked "operator" are the
operator's own answers. A 2026-10-14 ruling replaces the 2026-10-13 one it names.

- **D1, config source (operator, 2026-10-14).** Python publishes its active
  configuration state, and native reads it. On every active-bundle swap,
  `ConfigRuntime` upserts one `config_active_state` row for the deployment (2.2).
  The row holds the revision, desired and active values, desired overrides,
  pending-restart keys, failed-live keys, and secret bindings sealed under the
  daemon envelope. Every native config read serves that row, so native and Python
  agree on revision and values while an activation is pending or a key has failed
  live. Native writes commit to `config_store` and `config_state` under Python's
  lock and compare-and-set rules, then send `pg_notify('gobby_config_changed')`.
  Activation stays with `ConfigRuntime`, and a native write waits for the row to
  reach its revision (Write wait).
  Rejected: native reads of the desired configuration from `config_store`, the
  2026-10-13 ruling, which diverges from Python while an activation is pending; and
  keeping the active-state routes on Python permanently, which leaves Python owning
  config routes at close.
- **D2, principal kinds (2026-10-14).** Rust issues and presents both principal
  kinds. Interactive principals land in P3. Managed principals land in P4:
  - the managed challenge (4.3);
  - capability-token (`gobby-agent-v1.`) handshakes for agent_run, tool_chat and
    maintenance, with native token verification (4.2, 4.6);
  - managed issuance, rotation and grant presentation (4.4, 4.5, 4.7).

  The native handshake ports the rotation-time `grant.json` rewrite: the rewrite,
  its private atomic write and the grant-capability minting. Rotation and rewrite
  stay one step, and a failed rewrite restores the predecessor bootstrap, so
  `grant.json` never names a revoked predecessor (4.5). gdaemon reads the managed
  signing key from the bootstrap at runtime; no agent or test reads the live
  `~/.gobby/bootstrap.yaml`. Revocation becomes durable through a SQL read that
  both sides use (4.1). Launch-time creation and the background rotation loop stay
  in Python (Constraints, End-state boundary).
  Rejected: the 2026-10-13 ruling, which kept managed principals on Python and
  moved managed issuance to a later stage. The operator's no-deferrals policy rules
  out a second version.
- **D3, compare policy.** The native leg runs only for GET/HEAD requests the family
  claims. A claimed request with any other method is proxied once. The family then
  checks the proxied response: for a handshake, it verifies the grant Python
  issued; for a P5 write, the export or the preview, it compares the result with
  the persisted state or a native recomputation. These checks are the write-path
  verifiers the soak counts.
  A claimed request that never reaches Python records `skipped`, with detail
  `backend_unavailable` or `bad_gateway`, and runs no comparison (2.1), so a
  backend restart inside a soak window records no mismatch. Outcomes go to
  `~/.gobby/logs/front-door-compare.jsonl`, and `gdaemon compare-report` summarizes
  them. Soak write evidence comes from an API-key client (E1). Because P4 makes
  managed handshakes native, E1 also requires managed handshakes from ordinary
  agent runs in the Q2 window.
- **D4, `/api/config` scope (2026-10-14).** All nineteen `/api/config` routes are
  native: `GET /api/config/effective` from 3.1 and the other eighteen in P5. Native
  writes reuse `pg_notify('gobby_config_changed')`, and activation stays with
  `ConfigRuntime` (D1). Route scope widens; family identity does not.
  Rejected: the 2026-10-13 ruling, which kept every route except
  `GET /api/config/effective` proxied and deferred the rest.
- **Seam location.** The `RouteFamily` trait moves to gcore behind feature
  `route-family`, generic over the router state, so a family crate can implement it
  without a gdaemon dependency cycle.
  - It gains `claims(&Parts) -> bool` (default `true`) and `check_proxied(...)`
    (default `Skipped`).
  - The routing table consults `claims` before choosing native, proxy or compare.
    An unclaimed request is proxied unchanged.
  Rejected: wrapping the family inside gdaemon, as gterminals does. That leaves later
  family crates unable to export the seam #21559 asks for.
- **Family identity.** One family, `runtime`, with prefixes `/api/runtime` and
  `/api/config`. It claims a request only when the method and path match a
  `NATIVE_ROUTES` row and the request's caller is one that row lists (3.1). The
  caller resolves in this order:
  1. `x-gobby-key-id`, which AuthState inserts for an API-key request, is
     `Operator` (3.1).
  2. A `gobby-agent-v1.` bearer is `Agent` when the request has no
     `X-Gobby-Break-Glass` header and its `X-Gobby-Session-Id` is absent or equals
     the token's session (4.2).
  3. A request with no `Authorization` header is `Public` (4.3). Only the managed
     challenge row lists it, and Python's challenge path ignores cookies and
     break-glass headers too.
  4. Any other request has no caller and is forwarded.

  Cookie traffic, loopback break-glass admission and session reference spellings
  therefore stay on Python as the end state, and Python's auth semantics and error
  bodies stay authoritative for them (2026-10-14: D4 widens route scope, not
  family identity).
- **Flip mechanics.** Routes are read once at gdaemon start. Flip and rollback are a
  bootstrap edit plus a gdaemon restart with the same binaries, which needs no
  redeploy. Rejected: hot reload of the route table, since nothing requires it.
- **Q1, end state (operator, 2026-10-13).** `native` becomes the built-in default
  for `runtime`, and the operator's daemon is on native at close. `proxy` stays as
  the rollback setting, documented as the fallback.
  - Each family declares its default with `RouteFamily::default_backend()`
    (default `Proxy`). A `front_door.routes` entry overrides it.
  - `RuntimeFamily` returns `Native` (6.1). A machine that registers no runtime
    family, such as a node, keeps proxying.
  - 6.1 lands with the live bootstrap set to `runtime: compare`, so the cutover
    that activates it starts the soak and does not go native unsoaked. Clearing
    the entry after the soak passes is the close. Rollback is `runtime: proxy`
    plus a gdaemon restart.
  - No agent reads or edits `~/.gobby/bootstrap.yaml`. The operator runs the
    route commands in E1, and Orchestrator #15977 coordinates them with the
    operator at each cutover or restart.
- **Q2, soak window (operator, 2026-10-13).** At least 24 hours, at least one
  config read and one interactive handshake per active project, and no pending
  config activation.
- **Process-lifetime family.** gdaemon builds the runtime family once in
  `serve::run`, on the shared pool, and leaks it with `Box::leak`. It then has the
  same `'static` lifetime as `FAMILIES`, and `RouteTable::new` keeps its signature
  for every existing test caller.
- **Envelope initialization.** Native code initializes the secret envelope (key-file
  posture only) when no `secret_key_material` row exists, as Python's
  `_initialize_envelope` does with an insert-if-absent race. Without this, a fresh
  install whose backend never started could not issue.
- **Managed challenge placement (2026-10-14).** The managed challenge is a family
  route listed for `Public` (4.3). `challenge::answer` keeps answering
  `interactive` challenges before dispatch and forwards only `managed` ones.
  Rejected: answering the managed challenge in `challenge::answer`, which would
  give the soak no compare evidence for it and `runtime: proxy` no fallback.
- **Prompt cache (2026-10-14, replacing the 2026-10-14 08:40Z ruling).**
  `PromptLoader` loses its cache, its change hook, the notifier parameter,
  `clear_cache`, the unused `PromptChangeNotifier` and both no-op route calls, and
  `load()` runs one indexed SELECT by name (5.3). An override written through the
  route reaches the next `load()` on a long-lived loader with no restart. No
  `load()` call site loads per item in a loop or more often than its LLM call
  (5.3, Research context).
  Rejected: a new `pg_notify` invalidation channel with a Python listener, which
  keeps a cache that no call site needs.
- **Prompt save contract (2026-10-14).** `PUT /api/config/prompts/{path}` takes
  JSON `{content, description?, version?, variables?}` on both sides (5.4). An
  omitted field inherits from the record being overridden: the existing override,
  else the effective record from `get_by_name`. This fixes the wipe of variable
  specs by a body-only UI save. Content that opens with a frontmatter block, found
  by `parse_frontmatter`'s delimiter scan, gets a 400 that names the JSON fields.
  The route has no YAML parser, and bundled sync keeps `parse_frontmatter`
  unchanged.
  Rejected: parsing frontmatter natively, which no locked crate supports without
  unsafe code, and keeping the PUT on Python permanently (5.4, Research context).
- **Write wait (2026-10-14).** A native write that commits a new revision polls
  its deployment's `config_active_state` row every 50 ms until the row's revision
  reaches the committed one, for at most 65 seconds. It then answers from the row
  as `ConfigValuesService._mutation_body` does. On timeout it returns Python's
  `reconcile_failed` body: `committed: true`, the changed keys, empty
  `pending_restart_keys`, and each changed key under `failed_live_keys` as
  `{"revision": <committed>, "subscriber": "runtime"}`. The web client already
  handles that status (`web/src/api/config.ts`).
  - The bound: `ConfigRuntime` prepares and then activates each matching subscriber
    one at a time, each step under that subscriber's `prepare_timeout`. The largest
    is 30 seconds (`src/gobby/runner_init/services.py`), so 60 seconds covers both
    steps for it, and 5 more cover the reconcile's database read at its 5-second
    statement timeout.
  - Python's own route waits without a bound, and neither gdaemon's proxy nor the
    web client sets a request timeout.

  Rejected: a bound from `ConfigRuntime._CONFIRMING_READS` and `_CONFIRM_BACKOFF`,
  which time Python's storage re-reads (about 0.35 seconds), not activation.
- **YAML documents (2026-10-14).** Both sides read saved YAML as YAML 1.2 core
  (5.11). Rust parses with `yaml_serde` 0.10.4, which is already locked. Python's
  `ConfigDocumentsService._prepare` loads with `Yaml12Loader`, a `yaml.SafeLoader`
  subclass that keeps PyYAML's reader, scanner and parser and ports `yaml_serde`'s
  event loader and deserializer over the same parser events, because PyYAML's
  composer and constructors differ from `yaml_serde` beyond its resolvers.
  - `yaml_serde`'s anchor numbering, which lets the aliases of a redefined anchor
    name replay a newer anchor's node, is a defect kept on both sides so the corpus
    has one answer. Native cannot see anchors through serde to reject them.
  - A decimal integer beyond `u128` and `i128` reads as a float, as `yaml_serde`'s
    `visit_int` reads it.
  - Export quotes every string that either the 1.1 or the 1.2 reading would take
    for another type, so every export reads back unchanged under both.
  - A shared YAML-to-JSON corpus covers every YAML 1.1/1.2 divergence, anchors,
    merge keys, tags and malformed input. A difference between PyYAML's parser and
    libyaml's that the corpus does not pin, such as which of two errors a long
    document reports, is a compare-mode mismatch that E1 counts.

  Invalid YAML returns `validation_error` with path `["content"]` and message
  `Invalid YAML at line L, column C`, at the 422 status every `ConfigValuesError`
  validation error already carries. 0.5.0 is unshipped, so the YAML 1.1 behavior
  change needs no compatibility path.
  Rejected: a new YAML crate, and keeping the document routes on Python, which
  contradicts D1.
- **Validation messages (2026-10-14).** Python moves four sites to the stable
  `<loc>: <msg>; ...` form that `ConfigMutations._validation_detail` already builds
  from pydantic errors without input or URL:
  1. `ConfigMutations._validate_value` (5.7);
  2. the generation-endpoint activation 422 (5.14);
  3. the validation-detection preview 400 (5.16);
  4. the document candidate 422 in `ConfigDocumentsService._prepare` (5.12).

  Rust emits pydantic's `msg` text for each constraint kind it enforces, and the
  shared corpora assert byte-equal messages and stored canonical values.
  Rejected: keeping pydantic's `str(exc)` text, which embeds a version-specific
  documentation URL and the input value.
- **Config validation (2026-10-14).** Native writes validate exactly as Python
  does, in two layers, from one generated carrier:
  - The carrier is `crates/gruntime/assets/runtime_config_validation.json`, a
    second output of the contract generator
    `scripts/generate_runtime_config_contract.py`. It holds pydantic's core
    schemas (pydantic 2.12.5, pydantic-core 2.41.5) for every registry spec,
    including the three restricted keys, and for `DaemonConfig`, plus the
    qualified name of every validator function they call: 78 distinct functions,
    16 of them reached from per-key schemas, once 5.7 types
    `memory_backup.backup_path` as `str` and 5.8 deletes the dead
    `SkillsConfig.validate_injection_format` (80 at plan time). It is a sibling of
    `runtime_config_contract.json` rather than a member, because the contract
    renders with sorted keys, which would reorder the model fields and dict
    defaults the stored bytes depend on, and gcore embeds the contract in every
    binary.
  - Per key (5.7): Rust validates each written value against its key's schema
    with pydantic's strict JSON semantics and dumps the canonical form Python
    stores.
  - Complete candidate (5.8): Rust assembles the candidate as
    `ConfigRepository.runtime_candidate` does and validates it against
    `DaemonConfig`'s schema. Hand ports cover every named function, including
    croniter's expression check. The time-zone check becomes membership in the
    `tzdata` package's zone list on both sides, with `tzdata` installed on every
    platform, because Python's `ZoneInfo` lookup depends on the host's files and,
    on Windows, on Python's own environment.
  - Python's Unicode tables (15.1 on Python 3.13, 16.0 on 3.14) differ from Rust's
    (17.0) and ICU4X 2.1's, so a code point assigned or changed after 15.1 is a
    documented divergence in case mapping and `repr` (5.7).
  - The generator fails on any node type or node key the interpreter does not
    implement, and gruntime refuses a carrier that names a function without a
    port, so a new constraint kind or validator cannot pass unchecked.
  - Native request bodies parse into an ordered tree that keeps integer digits,
    and stored values serialize as Python's `json.dumps` does (5.7). `NaN`,
    `Infinity` and lone-surrogate escapes, which `json.loads` accepts, are a
    documented 422 divergence on native writes. Python's write routes that parse
    their own body decode it as strict UTF-8 first, because `json.loads` on bytes
    also accepts a UTF-8 byte-order mark, UTF-16 and UTF-32. Such a body is then 422
    on both sides.

  A shared valid/invalid corpus runs through `ConfigMutations._validate_value`,
  `DaemonConfig.model_validate` and the Rust validator, and asserts acceptance,
  messages and stored bytes.
  Rejected: pydantic's JSON Schema, which loses strictness, validator positions
  and default factories; enabling `serde_json`'s `preserve_order` or
  `arbitrary_precision`, which would change every `serde_json` user in gdaemon;
  and proxying writes to Python for validation, which leaves Python owning config
  writes (D1).
- **Endpoint probes (2026-10-14).** Generation-endpoint activation probes the
  endpoint before it persists it, through the Codex CLI, the Codex app-server or an
  OpenAI-compatible, LM Studio, Ollama or vLLM server. Parity is proved at the wire
  (5.13): Python's probe chain and the native chain run one scenario corpus against
  one shared set of fakes, and the fakes' logs, the outcomes and the retry delays
  must be equal.
  - Gobby's own failure texts match byte for byte. A text that embeds an exception
    raised inside httpx, the OpenAI SDK, `json` or process spawning keeps gobby's
    prefix, only the prefix is compared, and the embedded library text is a
    documented divergence.
  - Classification matches exactly: which probe failed or degraded, and whether a
    failure is an authentication failure, a retryable status or a timeout.
  - Compare mode never probes natively. `check_proxied` checks Python's write
    against the stored rows and dry-runs only the checks before the probe (5.14).

  Rejected: an injected probe seam in the route tests, which would leave the wire
  traffic unproved, and comparing library exception text, which differs between
  httpx and `reqwest` for the same failure.

**Granularity:** Thirty-six deliverables, one per independently testable outcome:

- P1: seam (1.1) and pool (1.2);
- P2: compare mode (2.1) and the active config state table (2.2);
- P3: crate and effective config (3.1), presentation (3.2), secret store (3.3),
  interactive handshake (3.4) and the proxied-grant check (3.5);
- P4: durable revocation (4.1), capability-token verification (4.2), managed
  challenge (4.3), managed issuance (4.4), rotation with the `grant.json` rewrite
  (4.5), managed handshake (4.6) and managed presentation (4.7);
- P5: schema (5.1), secret listing and deletion (5.2), prompt loads (5.3), prompt
  routes (5.4), YAML template and export (5.5), active-state reads (5.6), per-key
  validation (5.7), candidate validation (5.8), values PATCH (5.9), secret save
  (5.10), YAML 1.2 reading (5.11), template save and import (5.12), the endpoint
  probe client (5.13), endpoint activation (5.14), the validation-command
  classifier (5.15) and the validation-detection preview (5.16);
- P6: native default (6.1), corpus (6.2), e2e (6.3) and docs (6.4).

Shared Targets force a near-linear order. Every leaf that changes shipped crate
source shares `Cargo.lock`, `crates/gdaemon/Cargo.toml` and the version pins
(Constraints, Release ownership). `serve.rs` is shared by 1.2, 2.1 and 3.1, and
`crates/gruntime/src/lib.rs` by every leaf from 3.1 through 6.1 except 4.1, 4.5,
5.3 and 5.8. Only 5.3, a Python-only refactor, sits off the chain.

The counts below include `src/gobby/install/version_pins.py`, which every crate
leaf targets with `Cargo.toml` and `Cargo.lock`. These leaves carry more than six
acceptance items or target more than six hand-maintained production files, and
none splits further:

- 1.1 (seven items, seven files) moves the trait and adds its new methods in one
  change, so every implementor compiles at each commit.
- 2.1 (seven items, seven files): its report reads only the log its policy writes.
- 2.2 (nine items, seven files): its migration lands with its derived carriers
  (catalog manifest, schema identity and golden vectors), or the schema identity
  tests fail between leaves, and with the Python publisher that writes the table,
  so no leaf ships a table nothing writes.
- 3.2 (seven items, eight files): its gcore presentation module lands with its
  first consumer, the native `GET /api/runtime/config`, so the module is never
  unused between leaves.
- 3.2, 3.3 and 3.4 (seven items each) each port one behavior with its vectors and
  failure cases.
- 4.1 (seven items): its migration and derived carriers land together, as 2.2's
  do.
- 4.5 (seven items) keeps rotation and the rewrite one step (D2).
- 5.4 (seven items): its Python and Rust sides share one JSON contract, and a split
  would leave compare mode mismatching between leaves.
- 5.5 (nine files): its two routes share one PyYAML-compatible emitter and one
  export policy, and the policy reads the contract field that the leaf's generator
  change adds.
- 5.6 (seven files): its three routes read one published row through one public
  projection, and the gcore `public_policy` and shared export helpers it adds
  exist only for that projection.
- 5.7 (nine items, eighteen files): the interpreter, the ports the per-key schemas
  call and the carrier land together, because gruntime refuses a carrier that
  names a function without a port and the generator fails on a node the
  interpreter lacks (Config validation). Its Python edits (site 1 and
  `backup_path` as `str`) change the answers the shared corpus pins.
- 5.8 (nine items, twenty files): the same coupling holds for the `DaemonConfig`
  schema and the remaining ports, and its Python edits (the sorted default-mode
  message, the deleted `validate_injection_format` and the `tzdata` zone check)
  change the answers its corpus pins.
- 5.9 (nine items, fourteen files): the commit core, the write rules and the write
  wait land with the first route that uses them, so none is unused between leaves,
  and the carrier members `default` and `identityField` land with their reader.
- 5.10 (eight files): its Python edits, the body self-parse and the sorted
  category message, are the answers its corpus pins.
- 5.11 (seven items): Python's loader and Rust's reader share one vector corpus,
  and either half alone leaves the corpus failing.
- 5.12 (seven items, ten files): its two routes share one handler, one dry run
  and one corpus, and the corpus pins Python's site 4 and self-parse.
- 5.13 (seven files): one `probe` entry point runs both wire APIs under one
  timeout and retry policy, and one fake set and corpus prove them; a split would
  land the entry point with one wire API missing.
- 5.14 (seven files): its two Python fixes sit in the handler its corpus runs, and
  `validate_model` and the probe-verified write path have no other caller yet.

## P1: Family seam and shared pool
`kind: framing`

### 1.1 `RouteFamily` seam in gcore with per-request claims [category: code]
`kind: deliverable`

Targets:
- `crates/gcore/Cargo.toml`
- `crates/gcore/src/lib.rs::*` — scope-reason: declare the feature-gated `route_family` module beside the existing module list; no existing symbol changes
- `crates/gcore/src/route_family.rs`
- `crates/gdaemon/Cargo.toml`
- `crates/gdaemon/src/front_door/routes.rs::*` — scope-reason: the trait leaves this file; `Entry`, `RouteTable::new`, `RouteTable::dispatch`, `FAMILIES`, `TerminalFamily` and the in-file tests adopt the gcore trait and the claims gate
- `crates/gdaemon/src/front_door/health.rs::*` — scope-reason: `HealthFamily` implements the gcore trait; only the import and impl header change
- `crates/gdaemon/tests/http_contracts.rs::*` — scope-reason: `parity_violations` and `precheck_rejects_missing_or_mismatched_parity` name the trait with its state parameter
- `crates/gterminals/src/lib.rs::*` — scope-reason: the module doc comment says `RouteFamily` lives in gdaemon; correct it to gcore
- `crates/gcore/src/bootstrap/front_door.rs::*` — scope-reason: the `FrontDoorBootstrap` doc comment says absent families are proxied; it becomes "take their family's default"
- `crates/gcode/Cargo.toml`
- `crates/ghook/Cargo.toml`
- `src/gobby/install/version_pins.py::MANAGED_BIN_VERSION_PINS`
- `Cargo.lock`

Release ownership (Constraints): this leaf changes gcore, so it bumps `gdaemon`,
`gcode` and `ghook` by 0.0.1 from the executor's starting tip.

Move `pub trait RouteFamily` out of `crates/gdaemon/src/front_door/routes.rs` into a
new `crates/gcore/src/route_family.rs`, compiled under a new gcore feature
`route-family = ["dep:axum"]`. Add axum to gcore as
`axum = { version = "0.8", default-features = false, optional = true }`. Enable the
feature from `crates/gdaemon/Cargo.toml`'s `gobby-core` dependency, which today
enables `postgres` and `postgres-pool`.

The trait:

```rust
pub trait RouteFamily<S>: Send + Sync {
    fn name(&self) -> &'static str;
    fn prefixes(&self) -> &'static [&'static str];
    fn router(&self) -> axum::Router<S>;
    /// The backend used when `front_door.routes` has no entry for this family.
    fn default_backend(&self) -> crate::bootstrap::RouteBackend { crate::bootstrap::RouteBackend::Proxy }
    /// False sends the request to the proxy unchanged, whatever the backend.
    fn claims(&self, _parts: &axum::http::request::Parts) -> bool { true }
    /// Compare mode, claimed non-GET/HEAD request: check the proxied response against
    /// the request that produced it. `request` is the body bytes compare already buffered.
    fn check_proxied<'a>(&'a self, _parts: &'a Parts, _request: &'a [u8], _status: StatusCode, _response: &'a [u8])
        -> Pin<Box<dyn Future<Output = ProxiedCheck> + Send + 'a>> { Box::pin(async { ProxiedCheck::Skipped }) }
}
pub enum ProxiedCheck { Skipped, Verified, Mismatch(String) } // Mismatch carries a JSON pointer or reason, never values
```

A family with its own state returns `Router::new().route(..).with_state(own_state)`
for any `S`. This works because axum 0.8's `with_state` yields `Router<S2>`.

`routes.rs` changes:

- It re-exports the trait (`pub use gobby_core::route_family::RouteFamily;`).
- `FAMILIES` becomes `&[&'static dyn RouteFamily<FrontDoorState>]`.
- `RouteTable::new` takes `&[&'static dyn RouteFamily<FrontDoorState>]`. Existing
  callers pass `FAMILIES`.
- `RouteTable::new` resolves each entry's backend as
  `routes.get(family.name()).copied().unwrap_or_else(|| family.default_backend())`
  in place of today's hard-coded `unwrap_or(RouteBackend::Proxy)`. Every existing
  family keeps the `Proxy` default, so routing is unchanged until 6.1.
- `Entry` stores the `&'static dyn` family next to its compiled router.
- `RouteTable::dispatch`, for `Native` and `Compare`, splits the request into parts
  and calls `entry.family.claims(&parts)`. `false` goes to `proxy::forward`
  unchanged.

`HealthFamily` (`crates/gdaemon/src/front_door/health.rs`) keeps
`Router<FrontDoorState>` and only re-points its import. The gdaemon stub
`TerminalFamily` stays.

The default now lives on the trait, so the gcore `FrontDoorBootstrap` doc comment
changes from "Families absent from the map are proxied" to "Families absent from the
map take their family's default (`RouteFamily::default_backend`)". Python's
`src/gobby/config/bootstrap.py::FrontDoorConfig` docstring is not targeted: any
target on that file requires the runtime config contract's derived carriers, and its
parser behavior is unchanged (an absent `routes` still parses to an empty map).
`docs/guides/configuration.md` (6.4) is the authoritative statement of each
family's default.

Research context:
- Today the trait is `RouteFamily { name, prefixes, router() -> Router<FrontDoorState> }`
  (`routes.rs::RouteFamily`, lines 26–34). `RouteTable::new` applies
  `.fallback(proxy_fallback).with_state(state.clone())` to each family router.
  Unclaimed paths and methods already fall back to the proxy
  (`unclaimed_methods_and_paths_under_a_native_family_are_proxied`). This
  deliverable adds header-level claiming on top of that path-level fallback.
- Consumers swept with `gcode grep -w -E 'RouteFamily|FAMILIES|RouteTable|unimplemented_families' crates/`.
  `RouteTable::new(FAMILIES, …)` callers keep compiling unchanged:
  - `crates/gdaemon/src/serve.rs`;
  - `crates/gdaemon/tests/front_door.rs` (5 sites);
  - `tests/lease.rs`;
  - `tests/nodes.rs` (2 sites);
  - the `routes.rs` in-file tests.

  `tests/http_contracts.rs` spells `&[&dyn RouteFamily]` and must add
  `<FrontDoorState>`.
- gcore has no axum today. The ghook and gcode graphs must not gain it, so check
  with `cargo tree -p gobby-hooks -i axum`, which must report nothing.
- Planned checks:
  - `cargo nextest run -p gobby-daemon -E 'test(routes) | test(http_contracts)'`;
  - `cargo clippy -p gobby-core --features route-family`;
  - `cargo clippy -p gobby-daemon`.

Consumers unchanged:
- `src/gobby/cli/install_setup_gclient.py` — no-edit-reason: reads only the `gclient` pin, which no leaf of this plan bumps.
- `src/gobby/cli/install_setup_gcode.py` — no-edit-reason: reads the `gcode` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_ghook.py` — no-edit-reason: reads the `ghook` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_gterm.py` — no-edit-reason: reads only the `gterm` pin, which no leaf of this plan bumps.
- `src/gobby/cli/install_setup_versions.py` — no-edit-reason: looks a pin up by the binary name it is given.
- `src/gobby/code_index/gcode_gateway.py` — no-edit-reason: derives `MIN_GCODE_GRAPH_VERSION` from the `gcode` pin at import.
- `src/gobby/hooks/runtime_compat.py` — no-edit-reason: derives `MINIMUM_GHOOK_VERSION_FOR_SUPPORTED_SCHEMA` from the `ghook` pin at import.
- `src/gobby/install/bin_freshness_models.py` — no-edit-reason: iterates every pin, so the bumped values flow through.
- `src/gobby/install/distribution.py` — no-edit-reason: looks a pin up by binary name.
- `tests/cli/installers/test_grok_installer.py` — no-edit-reason: reads the `ghook` pin instead of a version literal.
- `tests/cli/test_install_ghook.py` — no-edit-reason: builds its fixtures from the `ghook` pin instead of a version literal.
- `tests/cli/test_install_setup.py` — no-edit-reason: reads the `gcode` pin instead of a version literal.
- `tests/cli/test_install_setup_gclient.py` — no-edit-reason: reads the `gclient` pin instead of a version literal.
- `tests/cli/test_install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin instead of a version literal.
- `tests/cli/test_install_setup_gterm.py` — no-edit-reason: reads the `gterm` and `gclient` pins instead of version literals.
- `tests/code_index/test_gcode_gateway.py` — no-edit-reason: reads the `gcode` pin instead of a version literal.
- `tests/hooks/test_runtime_compat.py` — no-edit-reason: compares the derived floor to the `ghook` pin instead of a version literal.
- `tests/install/test_distribution.py` — no-edit-reason: reads pins by binary name; its `0.1.0` literal is a fake `gcode` output, not a pin.
- `tests/install/test_version_pins.py` — no-edit-reason: compares each pin to its crate manifest, which is this leaf's bump check.

**Acceptance:**

- 1.1.1 - gcore exposes `RouteFamily<S>` and `ProxiedCheck` behind `route-family`, with defaults `default_backend = Proxy`, `claims = true` and `check_proxied = Skipped`, and `cargo tree -p gobby-hooks -i axum` is empty. file: `crates/gcore/src/route_family.rs`.
- 1.1.2 - Under a `Native` or `Compare` backend, a request the family does not claim reaches the proxy unchanged and never the family router. test: `crates/gdaemon/src/front_door/routes.rs::unclaimed_requests_are_proxied_under_every_backend`.
- 1.1.3 - The existing routing behaviors still hold. test: `crates/gdaemon/src/front_door/routes.rs::health_family_honors_proxy_native_and_compare`.
- 1.1.4 - The corpus parity precheck compiles against the generic trait and passes. test: `crates/gdaemon/tests/http_contracts.rs::every_native_family_has_corpus_cases`.
- 1.1.5 - A family with no `front_door.routes` entry takes its `default_backend()`, and an entry overrides it. test: `crates/gdaemon/src/front_door/routes.rs::absent_route_takes_the_family_default`.
- 1.1.6 - `gdaemon`, `gcode` and `ghook` are each one patch version above the executor's starting tip in their `Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`, so this leaf's cutover passes the release-identity preflight. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.
- 1.1.7 - The gcore `FrontDoorBootstrap` doc states that absent families take their family's default. file: `crates/gcore/src/bootstrap/front_door.rs`.

### 1.2 One gdaemon pool for key resolution, the lease and hub node routes [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `crates/gdaemon/src/serve.rs::*` — scope-reason: `run` builds the shared pool once and passes it to the key resolver, `RuntimeState::prepare` and `HubNodes`; two cfg-gated functions share the name `run`
- `crates/gdaemon/src/front_door/auth.rs::PostgresKeyResolver::new`
- `crates/gdaemon/src/lease/mod.rs::ActiveDaemonLease::new`
- `crates/gdaemon/src/state.rs::RuntimeState::prepare`
- `crates/gdaemon/tests/front_door.rs::postgres_resolver_and_resolverless_door_fail_closed`
- `crates/gdaemon/tests/nodes.rs::*` — scope-reason: two harness sites build `PostgresKeyResolver` from the test database URL
- `crates/gdaemon/tests/lease.rs::*` — scope-reason: every `ActiveDaemonLease::new` site passes a pool
- `crates/gdaemon/tests/lifecycle.rs::*` — scope-reason: the lease helper passes a pool
- `crates/gdaemon/tests/run_modes.rs::*` — scope-reason: `RuntimeState::prepare` calls pass `None` for the pool
- `crates/gdaemon/examples/backend_supervisor_fixture.rs::*` — scope-reason: the fixture lease passes a pool
- `crates/gdaemon/Cargo.toml`
- `src/gobby/install/version_pins.py::MANAGED_BIN_VERSION_PINS`
- `Cargo.lock`

Release ownership (Constraints): this leaf changes gdaemon only, so it bumps
`gdaemon` by 0.0.1 from the executor's starting tip.

#21559 requires the key resolver and the lease to move onto one shared gcore pool,
with their transaction boundaries preserved. In non-node mode, `serve::run` builds
one `gobby_core::postgres_pool::Pool` from `bootstrap.database_url` when the URL is
present, before the resolver is built:

- `max_size` 8;
- `application_name` `gobby-gdaemon`;
- `acquire_timeout` 5 s.

Wire the pool through:

- `PostgresKeyResolver::new(pool: Pool)` replaces `new(database_url)`. Keep the
  per-call `pool.get()`, the 5 s query timeout and the `last_touch` throttle.
- `RuntimeState::prepare(bootstrap, home, secret, pool: Option<Pool>)` keeps both
  refusals. With `None` and no database URL it still fails with "native runtime
  ownership requires bootstrap database_url". It passes the pool to
  `ActiveDaemonLease::new(pool, machine_id, deployment_token, mode)`.
- `ActiveDaemonLease::connect` keeps `deadpool_postgres::Object::take`,
  `RESET ROLE; SET statement_timeout = '5s'` and the lease application name. The
  taken connection leaves the pool, so the advisory-lock session stays dedicated.
- The hub `HubNodes { db }` uses the same pool. Its own `Pool::build`
  ("gobby-gdaemon-nodes", max 4) goes away.

`ActiveServices` (`state_services.rs`) keeps its tenure-scoped pools. It is outside
#21559's constraint.

Research context:
- Pools today:
  - `auth.rs::PostgresKeyResolver::new` builds max 2, "gobby-gdaemon-key-resolver";
  - `lease/mod.rs::ActiveDaemonLease::new` builds max 2, "gobby-gdaemon-lease-connect";
  - `serve.rs::run` builds max 4, "gobby-gdaemon-nodes", after `RuntimeState::prepare`.

  The gcore pool's `SessionHook::establish` runs `SET ROLE gobby_daemon_runtime` on
  every pooled session. The lease resets it on its taken connection.
- Consumers of the changed constructors:
  - `ActiveDaemonLease::new`: `state.rs`, `tests/lease.rs` (lines 179 and 433),
    `tests/lifecycle.rs:35` and `examples/backend_supervisor_fixture.rs:25`.
  - `PostgresKeyResolver::new`: `serve.rs:69`, `tests/front_door.rs:238` and
    `tests/nodes.rs` (lines 800 and 1935).
  - `RuntimeState::prepare`: `serve.rs:123` and `tests/run_modes.rs` (lines 64
    and 81).

  The node-mode refusal tests must still refuse before any connection. Build their
  pool lazily from a syntactically valid URL; `Pool::build` does not connect.
  `postgres_resolver_and_resolverless_door_fail_closed` builds its pool from an
  unreachable URL such as `postgresql://test@127.0.0.1:1/test` instead of an invalid
  DSN.
- Planned checks: `cargo nextest run -p gobby-daemon --test lease --test lifecycle --test run_modes --test front_door --test nodes`.

Consumers unchanged:
- `src/gobby/cli/install_setup_gclient.py` — no-edit-reason: reads only the `gclient` pin, which no leaf of this plan bumps.
- `src/gobby/cli/install_setup_gcode.py` — no-edit-reason: reads the `gcode` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_ghook.py` — no-edit-reason: reads the `ghook` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_gterm.py` — no-edit-reason: reads only the `gterm` pin, which no leaf of this plan bumps.
- `src/gobby/cli/install_setup_versions.py` — no-edit-reason: looks a pin up by the binary name it is given.
- `src/gobby/code_index/gcode_gateway.py` — no-edit-reason: derives `MIN_GCODE_GRAPH_VERSION` from the `gcode` pin at import.
- `src/gobby/hooks/runtime_compat.py` — no-edit-reason: derives `MINIMUM_GHOOK_VERSION_FOR_SUPPORTED_SCHEMA` from the `ghook` pin at import.
- `src/gobby/install/bin_freshness_models.py` — no-edit-reason: iterates every pin, so the bumped values flow through.
- `src/gobby/install/distribution.py` — no-edit-reason: looks a pin up by binary name.
- `tests/cli/installers/test_grok_installer.py` — no-edit-reason: reads the `ghook` pin instead of a version literal.
- `tests/cli/test_install_ghook.py` — no-edit-reason: builds its fixtures from the `ghook` pin instead of a version literal.
- `tests/cli/test_install_setup.py` — no-edit-reason: reads the `gcode` pin instead of a version literal.
- `tests/cli/test_install_setup_gclient.py` — no-edit-reason: reads the `gclient` pin instead of a version literal.
- `tests/cli/test_install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin instead of a version literal.
- `tests/cli/test_install_setup_gterm.py` — no-edit-reason: reads the `gterm` and `gclient` pins instead of version literals.
- `tests/code_index/test_gcode_gateway.py` — no-edit-reason: reads the `gcode` pin instead of a version literal.
- `tests/hooks/test_runtime_compat.py` — no-edit-reason: compares the derived floor to the `ghook` pin instead of a version literal.
- `tests/install/test_distribution.py` — no-edit-reason: reads pins by binary name; its `0.1.0` literal is a fake `gcode` output, not a pin.
- `tests/install/test_version_pins.py` — no-edit-reason: compares each pin to its crate manifest, which is this leaf's bump check.

**Acceptance:**

- 1.2.1 - `serve::run` builds exactly one pool, used by the resolver, the lease and hub node routes, and neither `auth.rs` nor `lease/mod.rs` calls `Pool::build`. file: `crates/gdaemon/src/serve.rs`.
- 1.2.2 - The lease still holds a dedicated advisory-lock session that drop and failed transactions release. test: `crates/gdaemon/tests/lease.rs::failed_transaction_and_drop_release_the_dedicated_session`.
- 1.2.3 - Epoch and signing secret still rotate together on acquisition. test: `crates/gdaemon/tests/lease.rs::epoch_and_secret_rotate_together`.
- 1.2.4 - A resolver on an unreachable pool fails closed. test: `crates/gdaemon/tests/front_door.rs::postgres_resolver_and_resolverless_door_fail_closed`.
- 1.2.5 - `gdaemon` is one patch version above the executor's starting tip in its `Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`, so this leaf's cutover passes the release-identity preflight. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

## P2: Compare mode and active config state
`kind: framing`

2.1 makes compare mode record outcomes. 2.2 gives native config reads their source:
Python's published active configuration state (D1).

### 2.1 Compare policy, compare log and `gdaemon compare-report` [category: code] (depends: 1.2)
`kind: deliverable`

Targets:
- `crates/gdaemon/src/front_door/routes.rs::*` — scope-reason: `compare` and `dispatch` apply the method policy, value equality for JSON and the proxied check, and record every outcome
- `crates/gdaemon/src/front_door/compare.rs`
- `crates/gdaemon/src/front_door/proxy.rs::*` — scope-reason: adds `try_forward`, which returns the connect or upstream error instead of building the 503 or 502; `forward` wraps it with an unchanged signature and responses
- `crates/gdaemon/src/front_door/mod.rs::*` — scope-reason: `FrontDoorState` gains `compare_log: Option<Arc<CompareLog>>`, which `FrontDoorState::new` sets to `None`; the module declares `compare`
- `crates/gdaemon/src/serve.rs::*` — scope-reason: `run` opens the compare log under the Gobby home and `serve_with_runtime` sets it on every listener's state
- `crates/gdaemon/src/main.rs::*` — scope-reason: add `Command::CompareReport { since }` and dispatch it in `main`
- `crates/gdaemon/tests/compare.rs`
- `crates/gdaemon/Cargo.toml`
- `src/gobby/install/version_pins.py::MANAGED_BIN_VERSION_PINS`
- `Cargo.lock`

Release ownership (Constraints): this leaf changes gdaemon only, so it bumps
`gdaemon` by 0.0.1 from the executor's starting tip.

Compare-mode policy for a claimed request (after the 1.1 claims gate):

- **GET or HEAD:**
  1. Run both legs as today (`tokio::join!`) and return the proxied response.
  2. They match when the statuses are equal, the `content-type` and
     `cache-control` headers are equal, and the bodies are equal. When both are
     `application/json`, bodies compare by parsed `serde_json::Value`, which
     ignores key order; otherwise they compare byte for byte.
  3. Record `match` or `diff`. A diff's detail is the first differing JSON pointer,
     or `status`, `header:<name>` or `bytes`.
  4. A native leg that errors or fails to collect is recorded as `native_error`
     and never changes the returned response.
- **Any other method:**
  1. Keep the request body bytes `compare` already buffers, then proxy once with a
     clone of them (`Bytes` clones share the buffer; the body is never read twice).
  2. Collect the proxied body and call
     `entry.family.check_proxied(&parts, &request_body, status, &response_body)`.
  3. Record `verified`, `mismatch` (detail from the check) or `skipped`.
  4. Return the proxied response rebuilt from the collected bytes.
- **Python not reached.** Both branches call `proxy::try_forward`. When it fails,
  Python gave no answer to compare. The request returns the response `forward`
  builds today (the typed 503 for a connect failure, else the 502), skips the GET
  comparison and `check_proxied`, and records `skipped` with detail
  `backend_unavailable` or `bad_gateway`. A backend that is starting, killed or
  restarting therefore never records a `diff` or `mismatch`.

An unclaimed request is proxied and not recorded.

`compare.rs` owns the compare log:

- **Record.** One JSON object per line:
  `{"ts": <unix seconds>, "family", "method", "path", "outcome", "native_status", "proxied_status", "detail"}`.
  The path carries no query string and the record carries no body values.
- **Writer.** `CompareLog::open(home)` appends to
  `<home>/logs/front-door-compare.jsonl`, creating `logs/` if needed. It writes
  each line with a single `write_all` on an `O_APPEND` file behind a `Mutex`. A
  failed write logs once to stderr and never fails the request.
- **Report.** `pub fn report(path, since: Option<i64>) -> Result<Report>` counts
  outcomes per family within the window and records `first_ts` and `last_ts`. A
  malformed line is an error.

`gdaemon compare-report [--since <unix-seconds>]`:

- reads `<gobby_home()>/logs/front-door-compare.jsonl`;
- prints the report as JSON;
- exits 1 when the window holds any `diff`, `native_error` or `mismatch`, and also
  exits 1 for an empty window, since an empty window proves nothing;
- exits 0 otherwise.

`serve::run` opens the log once, where `home` is in scope, and
`serve_with_runtime` sets it on each `FrontDoorState`.

Research context:
- Today `routes.rs::compare` already joins both legs and returns the proxied
  response. It compares status plus raw bytes and `eprintln!`s diffs only. It runs
  the native leg for every method. For a POST handshake that would issue a second
  interactive credential, racing Python's
  `issue_or_reuse_interactive_principal`, which D3 forbids. It also records nothing
  on a match, so a soak has no denominator.
- `proxy::forward` maps a connect error to `health::unavailable` (the typed 503) and
  any other client error to `health::bad_gateway` (502). Compare sees only the
  built response, so without `try_forward` a backend restart inside a soak would
  record a `diff` for every claimed GET (native 200 against gdaemon's own 503).
  `forward` keeps its callers (`proxy_fallback`, `RouteTable::dispatch`) unchanged.
- `FrontDoorState::new(backend, BackendState, auth)` has about a dozen test callers
  across `crates/gdaemon/tests/` and `routes.rs`. They stay unchanged because the
  new field defaults to `None`, and a `None` log records nothing.
- `main.rs` uses clap `Command { Schema, Serve }`, and `gobby_core::gobby_home()`
  resolves `GOBBY_HOME`.
- Planned checks:
  - `cargo nextest run -p gobby-daemon --test compare -E 'test(routes)'`;
  - `cargo clippy -p gobby-daemon`.

Consumers unchanged:
- `src/gobby/cli/install_setup_gclient.py` — no-edit-reason: reads only the `gclient` pin, which no leaf of this plan bumps.
- `src/gobby/cli/install_setup_gcode.py` — no-edit-reason: reads the `gcode` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_ghook.py` — no-edit-reason: reads the `ghook` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_gterm.py` — no-edit-reason: reads only the `gterm` pin, which no leaf of this plan bumps.
- `src/gobby/cli/install_setup_versions.py` — no-edit-reason: looks a pin up by the binary name it is given.
- `src/gobby/code_index/gcode_gateway.py` — no-edit-reason: derives `MIN_GCODE_GRAPH_VERSION` from the `gcode` pin at import.
- `src/gobby/hooks/runtime_compat.py` — no-edit-reason: derives `MINIMUM_GHOOK_VERSION_FOR_SUPPORTED_SCHEMA` from the `ghook` pin at import.
- `src/gobby/install/bin_freshness_models.py` — no-edit-reason: iterates every pin, so the bumped values flow through.
- `src/gobby/install/distribution.py` — no-edit-reason: looks a pin up by binary name.
- `tests/cli/installers/test_grok_installer.py` — no-edit-reason: reads the `ghook` pin instead of a version literal.
- `tests/cli/test_install_ghook.py` — no-edit-reason: builds its fixtures from the `ghook` pin instead of a version literal.
- `tests/cli/test_install_setup.py` — no-edit-reason: reads the `gcode` pin instead of a version literal.
- `tests/cli/test_install_setup_gclient.py` — no-edit-reason: reads the `gclient` pin instead of a version literal.
- `tests/cli/test_install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin instead of a version literal.
- `tests/cli/test_install_setup_gterm.py` — no-edit-reason: reads the `gterm` and `gclient` pins instead of version literals.
- `tests/code_index/test_gcode_gateway.py` — no-edit-reason: reads the `gcode` pin instead of a version literal.
- `tests/hooks/test_runtime_compat.py` — no-edit-reason: compares the derived floor to the `ghook` pin instead of a version literal.
- `tests/install/test_distribution.py` — no-edit-reason: reads pins by binary name; its `0.1.0` literal is a fake `gcode` output, not a pin.
- `tests/install/test_version_pins.py` — no-edit-reason: compares each pin to its crate manifest, which is this leaf's bump check.

**Acceptance:**

- 2.1.1 - A claimed GET in Compare mode returns the proxied response and records `match`, or `diff` with a pointer and no values. test: `crates/gdaemon/tests/compare.rs::get_compare_records_match_and_diff_without_values`.
- 2.1.2 - JSON bodies that differ only in key order record `match`. test: `crates/gdaemon/tests/compare.rs::json_bodies_compare_by_value`.
- 2.1.3 - A claimed POST in Compare mode never reaches the native router, the backend and `check_proxied` both receive the original request bytes, and the family check's outcome is recorded. test: `crates/gdaemon/tests/compare.rs::non_get_requests_run_only_the_proxied_leg`.
- 2.1.4 - A failing native leg records `native_error` and the caller still receives the proxied response. test: `crates/gdaemon/tests/compare.rs::native_failure_returns_the_proxied_response`.
- 2.1.5 - The report counts per family within `--since` and fails on any diff, native_error or mismatch, and on an empty window. test: `crates/gdaemon/tests/compare.rs::report_fails_on_mismatch_and_empty_window`.
- 2.1.6 - `gdaemon` is one patch version above the executor's starting tip in its `Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`, so this leaf's cutover passes the release-identity preflight. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.
- 2.1.7 - In Compare mode, a claimed GET and a claimed POST whose backend refuses connections each return the typed 503 and record `skipped` with detail `backend_unavailable`, never `diff` or `mismatch`. test: `crates/gdaemon/tests/compare.rs::unreachable_backend_records_skipped`.

### 2.2 Active config state table [category: code] (depends: 2.1)
`kind: deliverable`

Targets:
- `crates/gcore/assets/schema/migrations/469_config_active_state.sql`
- `crates/gcore/src/schema/assets.rs::MIGRATIONS`
- `crates/gcore/assets/schema/catalog.manifest.json::*` — scope-reason: regenerated; its tables gain `config_active_state`
- `crates/gcore/src/grant/bundle.rs::*` — scope-reason: the non-postgres `expected_schema_identity` literal `latest_version` and the `GOLDEN_LATEST_CHECKSUM` and `GOLDEN_ASSETS_ROOT_HASH` constants take the new latest migration's identity
- `crates/gcore/tests/schema_contract.rs::embedded_assets_publish_a_complete_schema_identity`
- `crates/gdaemon/tests/cli_contract.rs::*` — scope-reason: the contract names it a derived carrier of the embedded schema assets; its schema assertions read the embedded identity and must pass against the new migration, editing any that pins a literal
- `src/gobby/storage/schema_expected_identity.json::*` — scope-reason: regenerated; `latest_version`, `latest_checksum` and `assets_root_hash` change
- `tests/runtime_grants/golden/brokered_datastores.json::*` — scope-reason: the signed schema identity changes, so the payload checksum and signature are recomputed
- `tests/runtime_grants/golden/direct_datastores.json::*` — scope-reason: the signed schema identity changes, so the payload checksum and signature are recomputed
- `tests/runtime_grants/golden/old_client_new_grant.json::*` — scope-reason: the signed schema identity changes, so the payload checksum and signature are recomputed
- `tests/runtime_grants/golden/payload_skew_unknown_field.json::*` — scope-reason: the signed schema identity changes, so the payload checksum and signature are recomputed
- `tests/runtime_grants/golden/unavailable_datastores.json::*` — scope-reason: the signed schema identity changes, so the payload checksum and signature are recomputed
- `src/gobby/config/runtime_active_state.py`
- `src/gobby/config/runtime.py::ConfigRuntime`
- `src/gobby/config/runtime_models.py::ConfigSnapshot`
- `src/gobby/runner_init/storage.py::open_storage_and_config`
- `tests/config/test_config_active_state.py`
- `crates/gdaemon/Cargo.toml`
- `crates/gcode/Cargo.toml`
- `crates/ghook/Cargo.toml`
- `src/gobby/install/version_pins.py::MANAGED_BIN_VERSION_PINS`
- `Cargo.lock`

Release ownership (Constraints): this leaf changes gcore's embedded schema assets, so
it bumps `gdaemon`, `gcode` and `ghook` by 0.0.1 from the executor's starting tip.

Add migration `469_config_active_state.sql`. 469 is the next free number at plan
time; the executor takes the next free number at its starting tip and uses it in
the filename and in every carrier below.

```sql
-- Python's active configuration state, published on every active-bundle swap.
CREATE TABLE public.config_active_state (
    deployment_token text PRIMARY KEY,
    revision bigint NOT NULL,
    state jsonb NOT NULL,
    published_at timestamptz NOT NULL DEFAULT now()
);
REVOKE ALL ON public.config_active_state FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE ON public.config_active_state TO gobby_daemon_runtime;
```

Regenerate the derived carriers exactly as 4.1 lists them; none is hand-edited
except the literals named there: `assets.rs::MIGRATIONS`, `catalog.manifest.json`,
the `bundle.rs` literals, the `schema_contract.rs` assertions,
`schema_expected_identity.json` and the five re-signed golden grants.

**The state document.** `src/gobby/config/runtime_active_state.py` adds
`active_state_document(snapshot, *, secret_store, deployment_token) -> dict[str, object]`:

```json
{
  "desired_values": {"<key>": <value>},
  "active_values": {"<key>": <value>},
  "desired_overrides": {"<key>": <value>},
  "pending_restart_keys": ["<key>"],
  "failed_live_keys": {"<key>": {"revision": 7, "subscriber": "<name>"}},
  "desired_bindings": {"<key>": {"reference": "$secret:<name>", "fingerprint": "<hex>", "sealed": "<base64>"}},
  "active_bindings": {"<key>": {"reference": "$secret:<name>", "fingerprint": "<hex>", "sealed": null}}
}
```

- The value maps are the snapshot's flat `desired_values`, `active_values` and
  `desired_overrides`, each passed through `to_jsonable_python`.
- `pending_restart_keys` is sorted. `failed_live_keys` carries each
  `ApplyFailure`'s `revision` and `subscriber` and drops its `message`, which no
  route returns.
- Each binding's `sealed` is
  `secret_store.seal(plaintext.encode(), aad=f"config-binding:{deployment_token}:{key}:{fingerprint}".encode())`,
  or `null` when the plaintext is `None`. Plaintext never reaches the table.
- `ConfigSnapshot` gains read-only properties `desired_bindings` and
  `active_bindings` that return its private binding maps, so the document reads
  them without touching private attributes.

`ActiveStatePublisher(database, secret_store, deployment_token)` is callable with a
snapshot. It builds the document and writes it in one hub transaction
(`with database.transaction() as conn:`), so `ConfigRepository` is unchanged:

```sql
INSERT INTO config_active_state (deployment_token, revision, state, published_at)
VALUES (%s, %s, %s::jsonb, now())
ON CONFLICT (deployment_token) DO UPDATE SET
    revision = EXCLUDED.revision, state = EXCLUDED.state, published_at = EXCLUDED.published_at
```

**Publishing.** `ConfigRuntime.__init__` gains
`active_state_publisher: Callable[[ConfigSnapshot], None] | None = None`.
`ConfigRuntime._publish_active(snapshot)` returns at once when the publisher is
`None`; otherwise it runs the publisher through `_run_db`. Success clears `_unpublished_active`; `CancelledError` propagates; any other
exception logs a warning and sets `_unpublished_active = True`. It is awaited
immediately after each `self._bundle = RuntimeActiveBundle(...)` assignment, with
the new bundle's snapshot:

1. `start`, before `_ready` is set;
2. `reprepare_subscriber`;
3. `register_subscriber`;
4. `_reconcile_locked`, before `_dispose_replaced` and before
   `raise activation_failure.error`, so a failed-live activation reaches the row.

Swaps 2–4 already hold `_reconcile_lock`. `_poll_revisions` retries on each tick:
when `_unpublished_active` is set it takes `_reconcile_lock` and publishes the
current bundle's snapshot, so a retry never overwrites a newer swap. `close()`
leaves the row, and native routes serve it while the backend is down.

`open_storage_and_config` builds
`ActiveStatePublisher(runner.database, runner.secret_store, deployment_token())`
and passes it to `ConfigRuntime`.

`runtime.py` has 874 lines, over the contract's 850-line decomposition heuristic.
The document builder and the publisher are split into the new
`src/gobby/config/runtime_active_state.py`, so `runtime.py` gains only the constructor argument,
`_publish_active`, the four calls and the retry.

Research context:
- Swap sites: `src/gobby/config/runtime.py::ConfigRuntime.start`,
  `.reprepare_subscriber`, `.register_subscriber` and `._reconcile_locked`. The
  existing `_revision_publisher` fires only in `_reconcile_locked` and only after a
  successful activation, so it cannot carry failed-live state.
- `ConfigSnapshot` (`src/gobby/config/runtime_models.py`) holds `revision`,
  `pending_restart_keys`, `failed_live_keys`, the flat value and override maps, and
  `RuntimeSecretBinding(reference, plaintext, fingerprint)` maps.
  `runtime_projection.py::_secret_fingerprint` computes the fingerprint.
- `SecretStore.seal` and `.open_sealed` (`src/gobby/storage/secrets.py`) are the
  format 3.3 ports. `src/gobby/deployment.py::deployment_token` and gcore's
  `grant::deployment_token(home)` derive the same key from the Gobby home.
- `ConfigRuntime._poll_revisions` ticks every `revision_poll_interval` (30 seconds).
- Migration 468 is the table-grant precedent (`REVOKE ALL ... FROM PUBLIC`, then
  `GRANT SELECT, INSERT, UPDATE ... TO gobby_daemon_runtime`).
- This leaf's Python tests run with `GOBBY_TEST_GDAEMON=checkout` after
  `cargo build -p gobby-daemon`, as 4.1's do, until the cutover installs the
  migration.
- Planned checks:
  - `GOBBY_SCHEMA_TEST_DATABASE_URL=<test-dsn> cargo nextest run -p gobby-core --features postgres --test schema_contract --test catalog_manifest_freshness`;
  - `GOBBY_TEST_GDAEMON=checkout DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/config/test_config_active_state.py tests/config/test_config_runtime.py tests/runtime_grants/test_golden_vectors.py -v`.

Consumers unchanged:
- `crates/gcore/src/grant/tests.rs` — no-edit-reason: it compares grants against `expected_schema_identity()` at run time and holds no migration literal.
- `tests/config/test_config_runtime.py` — no-edit-reason: it builds `ConfigRuntime` without a publisher, which the new keyword defaults to `None`.
- `src/gobby/agents/code_index.py` — no-edit-reason: Uses `ConfigSnapshot`'s existing constructor and members; this leaf only adds two read-only properties.
- `src/gobby/agents/isolation_code_index.py` — no-edit-reason: Uses `ConfigSnapshot`'s existing constructor and members; this leaf only adds two read-only properties.
- `src/gobby/agents/spawn.py` — no-edit-reason: Uses `ConfigSnapshot`'s existing constructor and members; this leaf only adds two read-only properties.
- `src/gobby/app_context.py` — no-edit-reason: Uses `ConfigRuntime` through members this leaf keeps, and any construction omits `active_state_publisher`, which defaults to `None`.
- `src/gobby/config/runtime_activation.py` — no-edit-reason: Uses `ConfigSnapshot`'s existing constructor and members; this leaf only adds two read-only properties.
- `src/gobby/config/runtime_contracts.py` — no-edit-reason: Uses `ConfigSnapshot`'s existing constructor and members; this leaf only adds two read-only properties.
- `src/gobby/config/runtime_projection.py` — no-edit-reason: Uses `ConfigSnapshot`'s existing constructor and members; this leaf only adds two read-only properties.
- `src/gobby/runner.py` — no-edit-reason: Uses `ConfigRuntime` through members this leaf keeps, and any construction omits `active_state_publisher`, which defaults to `None`; calls or replaces `open_storage_and_config`, whose signature is unchanged.
- `src/gobby/runner_init/__init__.py` — no-edit-reason: Calls or replaces `open_storage_and_config`, whose signature is unchanged.
- `src/gobby/runtime_grants/service.py` — no-edit-reason: Uses `ConfigSnapshot`'s existing constructor and members; this leaf only adds two read-only properties.
- `src/gobby/servers/http.py` — no-edit-reason: Uses `ConfigSnapshot`'s existing constructor and members; this leaf only adds two read-only properties.
- `src/gobby/servers/tool_approvals.py` — no-edit-reason: Uses `ConfigSnapshot`'s existing constructor and members; this leaf only adds two read-only properties.
- `src/gobby/servers/websocket/server.py` — no-edit-reason: Uses `ConfigRuntime` through members this leaf keeps, and any construction omits `active_state_publisher`, which defaults to `None`.
- `src/gobby/workflows/engine/core.py` — no-edit-reason: Uses `ConfigSnapshot`'s existing constructor and members; this leaf only adds two read-only properties.
- `tests/agents/test_isolation.py` — no-edit-reason: Uses `ConfigSnapshot`'s existing constructor and members; this leaf only adds two read-only properties.
- `tests/ai/test_embedding_switch_daemon_lifecycle.py` — no-edit-reason: Uses `ConfigRuntime` through members this leaf keeps, and any construction omits `active_state_publisher`, which defaults to `None`.
- `tests/config/test_config_runtime_config_resolution.py` — no-edit-reason: Uses `ConfigSnapshot`'s existing constructor and members; this leaf only adds two read-only properties.
- `tests/config/test_restart_config_consumers.py` — no-edit-reason: Uses `ConfigRuntime` through members this leaf keeps, and any construction omits `active_state_publisher`, which defaults to `None`; calls or replaces `open_storage_and_config`, whose signature is unchanged.
- `tests/config/test_runtime_loop_consumers.py` — no-edit-reason: Uses `ConfigSnapshot`'s existing constructor and members; this leaf only adds two read-only properties.
- `tests/config/test_stateful_config_subscribers.py` — no-edit-reason: Uses `ConfigRuntime` through members this leaf keeps, and any construction omits `active_state_publisher`, which defaults to `None`.
- `tests/config_runtime_helpers.py` — no-edit-reason: Uses `ConfigSnapshot`'s existing constructor and members; this leaf only adds two read-only properties.
- `tests/hooks/test_hook_manager_factory_injection.py` — no-edit-reason: Uses `ConfigSnapshot`'s existing constructor and members; this leaf only adds two read-only properties.
- `tests/integration/config/conftest.py` — no-edit-reason: Uses `ConfigRuntime` through members this leaf keeps, and any construction omits `active_state_publisher`, which defaults to `None`; uses `ConfigSnapshot`'s existing constructor and members; this leaf only adds two read-only properties.
- `tests/integration/config/test_config_cross_seam.py` — no-edit-reason: Uses `ConfigRuntime` through members this leaf keeps, and any construction omits `active_state_publisher`, which defaults to `None`.
- `tests/llm/test_context_window.py` — no-edit-reason: Uses `ConfigSnapshot`'s existing constructor and members; this leaf only adds two read-only properties.
- `tests/mcp_proxy/test_mcp_proxy_runtime_config_resolution.py` — no-edit-reason: Uses `ConfigRuntime` through members this leaf keeps, and any construction omits `active_state_publisher`, which defaults to `None`; uses `ConfigSnapshot`'s existing constructor and members; this leaf only adds two read-only properties.
- `tests/mcp_proxy/tools/test_voice.py` — no-edit-reason: Uses `ConfigSnapshot`'s existing constructor and members; this leaf only adds two read-only properties.
- `tests/runner_init/test_config_runtime_startup.py` — no-edit-reason: Uses `ConfigRuntime` through members this leaf keeps, and any construction omits `active_state_publisher`, which defaults to `None`; calls or replaces `open_storage_and_config`, whose signature is unchanged.
- `tests/runner_init/test_runner_init_storage.py` — no-edit-reason: Calls or replaces `open_storage_and_config`, whose signature is unchanged.
- `tests/runtime_grants/support.py` — no-edit-reason: Uses `ConfigSnapshot`'s existing constructor and members; this leaf only adds two read-only properties.
- `tests/runtime_grants/test_service_identity_snapshot.py` — no-edit-reason: Uses `ConfigSnapshot`'s existing constructor and members; this leaf only adds two read-only properties.
- `tests/servers/conftest.py` — no-edit-reason: Uses `ConfigRuntime` through members this leaf keeps, and any construction omits `active_state_publisher`, which defaults to `None`; uses `ConfigSnapshot`'s existing constructor and members; this leaf only adds two read-only properties.
- `tests/servers/routes/test_agent_spawn_routes.py` — no-edit-reason: Uses `ConfigRuntime` through members this leaf keeps, and any construction omits `active_state_publisher`, which defaults to `None`; uses `ConfigSnapshot`'s existing constructor and members; this leaf only adds two read-only properties.
- `tests/servers/routes/test_configuration_effective_routes.py` — no-edit-reason: Uses `ConfigRuntime` through members this leaf keeps, and any construction omits `active_state_publisher`, which defaults to `None`.
- `tests/servers/routes/test_configuration_routes.py` — no-edit-reason: Uses `ConfigRuntime` through members this leaf keeps, and any construction omits `active_state_publisher`, which defaults to `None`; uses `ConfigSnapshot`'s existing constructor and members; this leaf only adds two read-only properties.
- `tests/servers/routes/test_hold_open_gate.py` — no-edit-reason: Uses `ConfigRuntime` through members this leaf keeps, and any construction omits `active_state_publisher`, which defaults to `None`.
- `tests/servers/routes/test_indexing_config_routes.py` — no-edit-reason: Uses `ConfigRuntime` through members this leaf keeps, and any construction omits `active_state_publisher`, which defaults to `None`.
- `tests/servers/routes/test_rules_routes.py` — no-edit-reason: Uses `ConfigRuntime` through members this leaf keeps, and any construction omits `active_state_publisher`, which defaults to `None`; uses `ConfigSnapshot`'s existing constructor and members; this leaf only adds two read-only properties.
- `tests/servers/routes/test_runtime_config.py` — no-edit-reason: Uses `ConfigRuntime` through members this leaf keeps, and any construction omits `active_state_publisher`, which defaults to `None`.
- `tests/servers/routes/test_servers_routes_sessions_routes.py` — no-edit-reason: Uses `ConfigRuntime` through members this leaf keeps, and any construction omits `active_state_publisher`, which defaults to `None`.
- `tests/servers/websocket/test_server.py` — no-edit-reason: Uses `ConfigSnapshot`'s existing constructor and members; this leaf only adds two read-only properties.
- `tests/test_runner_lifecycle.py` — no-edit-reason: Uses `ConfigRuntime` through members this leaf keeps, and any construction omits `active_state_publisher`, which defaults to `None`.
- `tests/test_runner_project_recovery.py` — no-edit-reason: Calls or replaces `open_storage_and_config`, whose signature is unchanged.
- `tests/workflows/test_rule_engine.py` — no-edit-reason: Uses `ConfigSnapshot`'s existing constructor and members; this leaf only adds two read-only properties.
- `tests/workflows/test_rule_engine_aggregate_blocks.py` — no-edit-reason: Uses `ConfigSnapshot`'s existing constructor and members; this leaf only adds two read-only properties.
- `tests/workflows/test_session_feedback_rules.py` — no-edit-reason: Uses `ConfigSnapshot`'s existing constructor and members; this leaf only adds two read-only properties.
- `src/gobby/cli/install_setup_gclient.py` — no-edit-reason: reads only the `gclient` pin, which no leaf of this plan bumps.
- `src/gobby/cli/install_setup_gcode.py` — no-edit-reason: reads the `gcode` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_ghook.py` — no-edit-reason: reads the `ghook` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_gterm.py` — no-edit-reason: reads only the `gterm` pin, which no leaf of this plan bumps.
- `src/gobby/cli/install_setup_versions.py` — no-edit-reason: looks a pin up by the binary name it is given.
- `src/gobby/code_index/gcode_gateway.py` — no-edit-reason: derives `MIN_GCODE_GRAPH_VERSION` from the `gcode` pin at import.
- `src/gobby/hooks/runtime_compat.py` — no-edit-reason: derives `MINIMUM_GHOOK_VERSION_FOR_SUPPORTED_SCHEMA` from the `ghook` pin at import.
- `src/gobby/install/bin_freshness_models.py` — no-edit-reason: iterates every pin, so the bumped values flow through.
- `src/gobby/install/distribution.py` — no-edit-reason: looks a pin up by binary name.
- `tests/cli/installers/test_grok_installer.py` — no-edit-reason: reads the `ghook` pin instead of a version literal.
- `tests/cli/test_install_ghook.py` — no-edit-reason: builds its fixtures from the `ghook` pin instead of a version literal.
- `tests/cli/test_install_setup.py` — no-edit-reason: reads the `gcode` pin instead of a version literal.
- `tests/cli/test_install_setup_gclient.py` — no-edit-reason: reads the `gclient` pin instead of a version literal.
- `tests/cli/test_install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin instead of a version literal.
- `tests/cli/test_install_setup_gterm.py` — no-edit-reason: reads the `gterm` and `gclient` pins instead of version literals.
- `tests/code_index/test_gcode_gateway.py` — no-edit-reason: reads the `gcode` pin instead of a version literal.
- `tests/hooks/test_runtime_compat.py` — no-edit-reason: compares the derived floor to the `ghook` pin instead of a version literal.
- `tests/install/test_distribution.py` — no-edit-reason: reads pins by binary name; its `0.1.0` literal is a fake `gcode` output, not a pin.
- `tests/install/test_version_pins.py` — no-edit-reason: compares each pin to its crate manifest, which is this leaf's bump check.

**Acceptance:**

- 2.2.1 - The migration creates `config_active_state` keyed by `deployment_token`, not accessible to PUBLIC, with select, insert and update for `gobby_daemon_runtime`. file: `crates/gcore/assets/schema/migrations/469_config_active_state.sql`.
- 2.2.2 - Each of the four swap sites publishes the new snapshot, and a failed-live activation in `_reconcile_locked` is published before its error propagates. test: `tests/config/test_config_active_state.py::test_every_bundle_swap_publishes`.
- 2.2.3 - The document carries the snapshot's values, sorted pending keys and failed-live revision and subscriber, and each binding's `sealed` opens with `SecretStore.open_sealed` under its AAD while no plaintext appears in the document. test: `tests/config/test_config_active_state.py::test_document_seals_bindings`.
- 2.2.4 - A failed publish leaves the runtime serving, and the next poll tick republishes the current snapshot under the reconcile lock. test: `tests/config/test_config_active_state.py::test_failed_publish_retries_on_poll`.
- 2.2.5 - On the test hub, publishing twice for one deployment leaves one row holding the second revision and state. test: `tests/config/test_config_active_state.py::test_publish_upserts_one_row_per_deployment`.
- 2.2.6 - The embedded identity publishes the new latest migration. test: `crates/gcore/tests/schema_contract.rs::embedded_assets_publish_a_complete_schema_identity`.
- 2.2.7 - The catalog manifest matches the embedded assets. test: `crates/gcore/tests/catalog_manifest_freshness.rs::catalog_manifest_is_fresh_for_embedded_assets`.
- 2.2.8 - The re-signed golden grants round-trip under the new identity. test: `tests/runtime_grants/test_golden_vectors.py::test_grant_vectors_round_trip`.
- 2.2.9 - `gdaemon`, `gcode` and `ghook` are each one patch version above the executor's starting tip in their `Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`, so this leaf's cutover passes the release-identity preflight. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

## P3: `gobby-runtime` family crate
`kind: framing`

### 3.1 `gobby-runtime` crate and native `GET /api/config/effective` [category: code] (depends: 2.2)
`kind: deliverable`

Targets:
- `Cargo.toml`
- `Cargo.lock`
- `crates/gruntime/Cargo.toml`
- `crates/gruntime/src/lib.rs`
- `crates/gruntime/src/config.rs`
- `crates/gruntime/tests/config.rs`
- `crates/gdaemon/Cargo.toml`
- `crates/gdaemon/src/serve.rs::*` — scope-reason: `run` builds the runtime family on the shared pool, leaks it once, and composes it after `FAMILIES` for `unimplemented_families` and every listener's `RouteTable::new`
- `crates/gcode/Cargo.toml`
- `crates/ghook/Cargo.toml`
- `src/gobby/install/version_pins.py::MANAGED_BIN_VERSION_PINS`

Release ownership (Constraints): this leaf changes the root manifest's `members`, so
it bumps `gdaemon`, `gcode` and `ghook` by 0.0.1 from the executor's starting tip.

Create the crate and register it:

- Create `crates/gruntime` with package `gobby-runtime`, lib `gobby_runtime` and
  `publish = false`. It takes `version`, `edition`, `rust-version`, `license` and
  `repository` from `[workspace.package]` and `[lints] workspace = true`.
- Add `crates/gruntime` to the root `members`. `[workspace.package]` already exists.
- Dependencies:
  - `gobby-core` with features `postgres`, `postgres-pool` and `route-family`;
  - `axum` 0.8 (`default-features = false`);
  - `serde`, `serde_json`, `tokio` and `uuid`, at the versions gdaemon already uses.
- `crates/gdaemon/Cargo.toml` depends on `gobby-runtime` by path.

`lib.rs` exports `RuntimeFamily`:

- `RuntimeFamily::new(pool: Pool, home: PathBuf)`;
- `name()` returns `"runtime"`;
- `prefixes()` returns `&["/api/runtime", "/api/config"]`;
- `router<S>()` returns its routes with an internal `Arc<RuntimeState { pool, home }>`
  applied;
- `claims(parts)` is table-driven. `lib.rs` holds
  `const NATIVE_ROUTES: &[NativeRoute]`, where
  `NativeRoute { method, path, callers: &[Caller], gate: Option<fn(&Parts) -> bool> }`
  and `path` uses axum 0.8 route syntax (`{name}` for one segment, `{*rest}` for
  the remainder). `claims` is true when one row matches the method and path, the
  request's caller is in `callers`, and `gate` (when present) holds.
  - `Caller::Operator` means the request carries `x-gobby-key-id`.
  - 4.2 adds `Caller::Agent` (a gated `gobby-agent-v1.` bearer) and 4.3 adds
    `Caller::Public` (no `Authorization` header), which only the managed-challenge
    row lists.
  - A request that resolves to no caller is never claimed, and a caller the row
    does not list is forwarded. Cookie-only, break-glass and unauthenticated
    requests therefore reach Python on every row that does not list `Public`.

  3.1 registers one row: `GET /api/config/effective` for `Operator`. This leaf's
  test iterates the table and builds one request per `Caller` variant. A later
  leaf adds rows without editing the test, and a leaf that adds a `Caller` variant
  (4.2, 4.3) extends only the test's per-caller request builder.
- The routes `router()` registers are exactly the table's rows.

The front door strips client-sent identity headers and inserts them only for a
resolved API key (`auth.rs::AuthState::authenticate`, `IDENTITY_HEADERS`).

`config.rs`:

- `active_state(&Pool, token: &str) -> Result<Option<ActiveState>>` runs
  `SELECT revision, state FROM config_active_state WHERE deployment_token = $1`
  with `token = gobby_core::grant::deployment_token(home)`. `ActiveState` holds
  `revision` and deserializes 2.2's state document: `desired_values`,
  `active_values`, `desired_overrides`, `pending_restart_keys`, `failed_live_keys`,
  `desired_bindings` and `active_bindings`. Bindings stay sealed until a caller
  opens one through 3.3.
- No row means Python has not published for this deployment. The handler returns
  503 `{"error": {"code": "runtime_unavailable", "message": "Configuration runtime is still starting", "path": [], "retryable": true}}`,
  the body `ConfigurationRouteContext.get_config_snapshot` raises. A row left by a
  stopped backend is served, because it is the last active state.
- `machine_values(&ActiveState)` mirrors
  `src/gobby/servers/routes/configuration_effective.py::_machine_config_values`
  over `active_values`:
  1. Walk keys in sorted order and keep `gobby_core::config::is_machine_config_key(key)`.
  2. Skip null values and any key for which `is_secret_reference_key(key)` holds.
  3. Stringify: a string is kept as is; anything else uses Python `json.dumps`
     defaults (`", "` and `": "` separators, `ensure_ascii`).
  4. Expand `${VAR}` and `${VAR:-default}` from gdaemon's environment with
     `src/gobby/config/_loading.py::expand_env_vars` semantics.
  5. Drop values still containing `$secret:` or `${…}`.

The handler returns 200 with `{"revision": <row revision>, "config": {…}}`,
`Cache-Control: no-store` and `application/json`. A database failure returns 500
`{"detail": "Internal server error"}`, matching the Python route.

In `serve::run`, build the family when the shared pool exists. Make it `'static`
with `Box::leak(Box::new(RuntimeFamily::new(pool.clone(), home.to_owned())))` and
pass `FAMILIES` plus it to `unimplemented_families` and `RouteTable::new`.

Research context:
- Python serves the active `ConfigRuntime` snapshot
  (`ConfigurationRouteContext.get_config_snapshot`, then `snapshot.active_values`),
  which 2.2 publishes, so native and Python read the same revision and values (D1).
- Nine exact keys are machine-exported today: `ai.embeddings.dim`, `.model`,
  `.query_prefix`, `.routing` and `.timeout_seconds`; `databases.falkordb.host` and
  `.port`; `databases.qdrant.url`; and `indexing.respect_gitignore`. All have secrecy
  `none`, and no pattern key is exported, so `is_machine_config_key` suffices.
  `is_machine_config_key` and `is_secret_reference_key` are already public in
  `crates/gcore/src/config/mod.rs`, and the row carries complete values, so gcore
  needs no change.
- The `crates/AGENTS.md` family-crate convention applies: one exported family, rows
  and repositories on the gcore pool.
- Planned checks:
  - `cargo nextest run -p gobby-runtime --test config`, which is DB-backed through
    the test-database URL the existing gdaemon DB tests use;
  - `cargo clippy -p gobby-runtime`.

Consumers unchanged:
- `src/gobby/cli/install_setup_gclient.py` — no-edit-reason: reads only the `gclient` pin, which no leaf of this plan bumps.
- `src/gobby/cli/install_setup_gcode.py` — no-edit-reason: reads the `gcode` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_ghook.py` — no-edit-reason: reads the `ghook` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_gterm.py` — no-edit-reason: reads only the `gterm` pin, which no leaf of this plan bumps.
- `src/gobby/cli/install_setup_versions.py` — no-edit-reason: looks a pin up by the binary name it is given.
- `src/gobby/code_index/gcode_gateway.py` — no-edit-reason: derives `MIN_GCODE_GRAPH_VERSION` from the `gcode` pin at import.
- `src/gobby/hooks/runtime_compat.py` — no-edit-reason: derives `MINIMUM_GHOOK_VERSION_FOR_SUPPORTED_SCHEMA` from the `ghook` pin at import.
- `src/gobby/install/bin_freshness_models.py` — no-edit-reason: iterates every pin, so the bumped values flow through.
- `src/gobby/install/distribution.py` — no-edit-reason: looks a pin up by binary name.
- `tests/cli/installers/test_grok_installer.py` — no-edit-reason: reads the `ghook` pin instead of a version literal.
- `tests/cli/test_install_ghook.py` — no-edit-reason: builds its fixtures from the `ghook` pin instead of a version literal.
- `tests/cli/test_install_setup.py` — no-edit-reason: reads the `gcode` pin instead of a version literal.
- `tests/cli/test_install_setup_gclient.py` — no-edit-reason: reads the `gclient` pin instead of a version literal.
- `tests/cli/test_install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin instead of a version literal.
- `tests/cli/test_install_setup_gterm.py` — no-edit-reason: reads the `gterm` and `gclient` pins instead of version literals.
- `tests/code_index/test_gcode_gateway.py` — no-edit-reason: reads the `gcode` pin instead of a version literal.
- `tests/hooks/test_runtime_compat.py` — no-edit-reason: compares the derived floor to the `ghook` pin instead of a version literal.
- `tests/install/test_distribution.py` — no-edit-reason: reads pins by binary name; its `0.1.0` literal is a fake `gcode` output, not a pin.
- `tests/install/test_version_pins.py` — no-edit-reason: compares each pin to its crate manifest, which is this leaf's bump check.

**Acceptance:**

- 3.1.1 - `crates/gruntime` builds as workspace member `gobby-runtime` (publish false, workspace version) and gdaemon links it. file: `crates/gruntime/Cargo.toml`.
- 3.1.2 - `GET /api/config/effective` with an API-key identity returns Python's projection of the published active configuration with `no-store`. test: `crates/gruntime/tests/config.rs::effective_config_matches_python_projection`.
- 3.1.3 - `claims` follows `NATIVE_ROUTES`: every row is claimed for each listed caller with its gate satisfied; the same request with no caller, or with a caller not in the row's list, is forwarded; and a method or path no row lists (such as `DELETE /api/runtime/config` or `GET /api/config/no-such-route`) is forwarded. test: `crates/gruntime/tests/config.rs::claims_follow_the_route_table`.
- 3.1.5 - With no `config_active_state` row for the deployment the route returns Python's 503 `runtime_unavailable` body, and a database failure returns 500. test: `crates/gruntime/tests/config.rs::missing_row_is_unavailable_and_database_failure_fails`.
- 3.1.4 - Values that still hold `$secret:` or `${…}` after expansion are omitted. test: `crates/gruntime/tests/config.rs::unresolved_values_are_omitted`.
- 3.1.6 - `gdaemon`, `gcode` and `ghook` are each one patch version above the executor's starting tip in their `Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`, so this leaf's cutover passes the release-identity preflight. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 3.2 Grant signing and presentation in gcore; native `GET /api/runtime/config` [category: code] (depends: 3.1)
`kind: deliverable`

Targets:
- `crates/gcore/src/grant/mod.rs::*` — scope-reason: declare and re-export the `presentation` module
- `crates/gcore/src/grant/presentation/mod.rs`
- `crates/gcore/src/grant/presentation/tests.rs`
- `crates/gcore/src/schema/runner.rs::auth_schema_for`
- `crates/gcore/src/schema/mod.rs`
- `crates/gruntime/src/lib.rs`
- `crates/gruntime/src/config.rs`
- `crates/gruntime/src/grant.rs`
- `crates/gruntime/tests/grant.rs`
- `crates/gdaemon/Cargo.toml`
- `crates/gcode/Cargo.toml`
- `crates/ghook/Cargo.toml`
- `src/gobby/install/version_pins.py::MANAGED_BIN_VERSION_PINS`
- `Cargo.lock`

Release ownership (Constraints): this leaf changes gcore, so it bumps `gdaemon`,
`gcode` and `ghook` by 0.0.1 from the executor's starting tip.

`gcore::grant::presentation` holds the server half #21559 names:

- `sign_grant(grant, secret) -> Result<GrantBundle>` sets `payload_checksum`
  (existing `payload_checksum`) and `signature`. The signature is the hex
  HMAC-SHA256 with key `secret.as_bytes()` over `canonical_payload_bytes`, using
  `openssl::sign::Signer` with `PKey::hmac`.
- `signature_matches` compares with `openssl::memcmp::eq`.
- `decode_grant_header(&str) -> Result<GrantBundle, Malformed>` is structural only.
  It pads and base64url-decodes the header as
  `src/gobby/runtime_grants/handshake.py::decode_grant_header` does, then runs
  `serde_json::from_slice::<GrantBundle>`. It does not call the client's
  `bundle.rs::parse_grant_json`, which rejects a wrong `api_contract` before
  deserializing, strips the client-only `managed_api_token` envelope field and
  verifies the checksum. On the server those are `verify_static`'s checks, in
  Python's order. `parse_grant_json` stays unchanged for its client callers.
  Python's `GrantBundle` accepts a top-level `managed_api_token` (string or null)
  and excludes it from the signed payload, so the decoder removes that key first and
  treats any other type there as malformed. Anything the decoder rejects is
  forwarded, so Python's answer stays authoritative for it.
- `Rejection` codes: `invalid_signature`, `wrong_deployment`, `wrong_schema`,
  `wrong_api_contract`, `stale_epoch`, `expired`, `revoked`, `wrong_capability`,
  `claims_mismatch`, `credential_issuance_failed`, `credential_before_proof`.
- `Rejection::status()` returns 409 for `stale_epoch`; 401 for `expired` and
  `credential_before_proof`; 403 otherwise. `Rejection::body()` returns
  `{"code", "error": code, "message"}`.
- `verify_static(grant, &PresentationContext { token, fencing_epoch, signing_secret }, now)`
  runs Python's order: signature, checksum, deployment token, schema identity
  (`expected_schema_identity`), api_contract and version, fencing epoch, expiry. The
  caller then checks revocation, then capability.
- Make `schema::runner::auth_schema_for` `pub` and re-export it from
  `schema/mod.rs`.

`gruntime/src/grant.rs`:

- `fence(&Pool, token)` runs
  `SELECT fencing_epoch, grant_signing_secret FROM deployment_runtime WHERE deployment_token = $1`
  per request, as `src/gobby/runtime_grants/lease_view.py` does. The token comes
  from `gobby_core::grant::deployment_token(home)`.
- `interactive_revoked(...)` runs
  `SELECT * FROM <auth>.lookup_interactive_principal($token, $daemon_machine, $project, $generation)`.
  No row or a non-null `revoked_at` means revoked, as in
  `src/gobby/storage/interactive_credentials.py::InteractiveCredentialMixin.interactive_generation_revoked`.
  `<auth>` is `auth_schema_for(current_schema())`. The daemon machine comes from
  `gobby_core::machine::read_machine_id_from_home(home)`.
- The `GET /api/runtime/config` handler returns
  `{"config_revision": snapshot.revision, "settings": machine_values(snapshot)}` with
  `no-store`. Rejections use `Rejection::status()` and `Rejection::body()`.

This deliverable adds one `NATIVE_ROUTES` row: `GET /api/runtime/config` for
`Operator`, gated on an `X-Gobby-Runtime-Grant` header that `decode_grant_header`
accepts and whose principal kind is `interactive`. A missing header or a header that
is not base64url JSON in the `GrantBundle` shape is forwarded, so Python keeps its
401 `Runtime grant required` and 400 `malformed grant` answers. A header that
decodes but fails a check (wrong checksum, wrong `api_contract`, bad signature) is
claimed, and the handler returns that check's rejection as Python does. 4.7 widens
the gate to managed principal kinds.

Research context:
- Python reference:
  - `src/gobby/runtime_grants/service.py::GrantService.present` (check order);
  - `src/gobby/runtime_grants/signing.py` (canonical JSON minus checksum and
    signature; sha256 checksum; HMAC signature);
  - `src/gobby/servers/routes/runtime_handshake.py::grant_error_response` (status
    mapping);
  - `src/gobby/servers/routes/runtime_config.py::create_runtime_config_router`
    (route body);
  - `src/gobby/runtime_grants/handshake.py::decode_grant_header` (padding).
- Revocation:
  - The in-memory `GrantRevocationStore` checksum set has no production caller;
    `GrantService.revoke` is unused.
  - Interactive revocation is DB truth through `lookup_interactive_principal`;
    `runner_init/servers.py` sets `_principal_revoked` for exactly that.
  - Managed revocation is in-memory in Python today; 4.1 adds its durable read.
- `src/gobby/runtime_grants/schema.py::GrantBundle` (`extra="forbid"`) gives
  `version`, `api_contract`, `payload_checksum` and `signature` defaults and accepts
  any integer `api_contract`, so a wrong contract reaches `GrantService.present` and
  is rejected there as `wrong_api_contract` (403), never as a 400.
- The Rust client half lives in `crates/gcore/src/grant/bundle.rs`:
  `canonical_payload_bytes`, `payload_checksum`, `GRANT_VERSION`,
  `EXPECTED_API_CONTRACT`, `expected_schema_identity` and `parse_grant_json`.
  `GrantError::from_presentation_http` already parses the rejection body shape.
- `auth_schema_for` consumers stay unchanged:
  - `crates/gcore/src/schema/runner_tests.rs` and `crates/gcore/src/schema/verify.rs`
    make visibility-only use of it;
  - `crates/gcore/src/schema/runner.rs` calls it itself.
- Signing parity uses a Python-generated vector. Produce one grant with
  `uv run python -c` calling `gobby.runtime_grants.signing.sign_grant` and a fixed
  secret, then commit it inline as test constants.
- Planned checks:
  - `cargo nextest run -p gobby-core -E 'test(presentation)'`;
  - `cargo nextest run -p gobby-runtime --test grant`.

Consumers unchanged:
- `crates/gcore/src/schema/runner_tests.rs` — no-edit-reason: it already imports `auth_schema_for` through `super::runner`; widening visibility changes nothing.
- `crates/gcore/src/schema/verify.rs` — no-edit-reason: it calls `auth_schema_for(schema)` with the same signature.
- `src/gobby/cli/install_setup_gclient.py` — no-edit-reason: reads only the `gclient` pin, which no leaf of this plan bumps.
- `src/gobby/cli/install_setup_gcode.py` — no-edit-reason: reads the `gcode` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_ghook.py` — no-edit-reason: reads the `ghook` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_gterm.py` — no-edit-reason: reads only the `gterm` pin, which no leaf of this plan bumps.
- `src/gobby/cli/install_setup_versions.py` — no-edit-reason: looks a pin up by the binary name it is given.
- `src/gobby/code_index/gcode_gateway.py` — no-edit-reason: derives `MIN_GCODE_GRAPH_VERSION` from the `gcode` pin at import.
- `src/gobby/hooks/runtime_compat.py` — no-edit-reason: derives `MINIMUM_GHOOK_VERSION_FOR_SUPPORTED_SCHEMA` from the `ghook` pin at import.
- `src/gobby/install/bin_freshness_models.py` — no-edit-reason: iterates every pin, so the bumped values flow through.
- `src/gobby/install/distribution.py` — no-edit-reason: looks a pin up by binary name.
- `tests/cli/installers/test_grok_installer.py` — no-edit-reason: reads the `ghook` pin instead of a version literal.
- `tests/cli/test_install_ghook.py` — no-edit-reason: builds its fixtures from the `ghook` pin instead of a version literal.
- `tests/cli/test_install_setup.py` — no-edit-reason: reads the `gcode` pin instead of a version literal.
- `tests/cli/test_install_setup_gclient.py` — no-edit-reason: reads the `gclient` pin instead of a version literal.
- `tests/cli/test_install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin instead of a version literal.
- `tests/cli/test_install_setup_gterm.py` — no-edit-reason: reads the `gterm` and `gclient` pins instead of version literals.
- `tests/code_index/test_gcode_gateway.py` — no-edit-reason: reads the `gcode` pin instead of a version literal.
- `tests/hooks/test_runtime_compat.py` — no-edit-reason: compares the derived floor to the `ghook` pin instead of a version literal.
- `tests/install/test_distribution.py` — no-edit-reason: reads pins by binary name; its `0.1.0` literal is a fake `gcode` output, not a pin.
- `tests/install/test_version_pins.py` — no-edit-reason: compares each pin to its crate manifest, which is this leaf's bump check.

**Acceptance:**

- 3.2.1 - `sign_grant` reproduces a Python-signed grant's checksum and signature byte for byte, and `signature_matches` accepts it. test: `crates/gcore/src/grant/presentation/tests.rs::signature_matches_python_vector`.
- 3.2.2 - Each rejection code maps to its status and `{code, error, message}` body. test: `crates/gcore/src/grant/presentation/tests.rs::rejection_matrix_maps_codes_to_statuses`.
- 3.2.3 - `verify_static` reports the first failing check in Python's order. test: `crates/gcore/src/grant/presentation/tests.rs::checks_run_in_python_order`.
- 3.2.4 - `GET /api/runtime/config` with a valid interactive grant returns `{config_revision, settings}`, a stale epoch returns 409, and a revoked generation returns 403 `revoked`. test: `crates/gruntime/tests/grant.rs::runtime_config_presents_interactive_grants`.
- 3.2.5 - Missing grant headers and headers that are not base64url JSON in the `GrantBundle` shape are forwarded, and a top-level `managed_api_token` does not make a header malformed. test: `crates/gruntime/tests/grant.rs::malformed_and_missing_grant_headers_are_forwarded`.
- 3.2.6 - Through the handler, a validly signed grant with a wrong `payload_checksum` returns 403 `invalid_signature` (Python's checksum check raises `InvalidGrantSignature`), a correctly signed grant with a wrong `api_contract` returns 403 `wrong_api_contract`, a grant failing several checks reports only the first, and every one of them is still answered while the Python backend is down. test: `crates/gruntime/tests/grant.rs::presentation_rejections_follow_python_order_without_the_backend`.
- 3.2.7 - `gdaemon`, `gcode` and `ghook` are each one patch version above the executor's starting tip in their `Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`, so this leaf's cutover passes the release-identity preflight. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 3.3 Secret-store port: envelope, secret reads and credential sealing [category: code] (depends: 3.2)
`kind: deliverable`

Targets:
- `crates/gruntime/Cargo.toml`
- `crates/gruntime/src/lib.rs`
- `crates/gruntime/src/secrets.rs`
- `crates/gruntime/tests/secrets.rs`
- `crates/gruntime/tests/fixtures/secret_seal_vector.json`
- `tests/storage/test_secret_seal_vector.py`
- `crates/gdaemon/Cargo.toml`
- `src/gobby/install/version_pins.py::MANAGED_BIN_VERSION_PINS`
- `Cargo.lock`

Release ownership (Constraints): gruntime is in gdaemon's release closure, so this
leaf bumps `gdaemon` by 0.0.1 from the executor's starting tip.

`secrets.rs` reads and writes Python's envelope format through `openssl` and
`fernet`:

- **Key material.** Load the `secret_key_material` row: `kek_posture`, `kek_salt`,
  `kek_kdf_n`, `kek_kdf_r`, `kek_kdf_p`, `wrapped_dek`.
- **KEK.**
  - Posture `key_file` uses the Fernet key in `<home>/.secret_kek`, read as
    `src/gobby/storage/secrets.py::_read_kek_file` reads it.
  - Posture `scrypt_passphrase` derives
    `urlsafe_b64(scrypt(GOBBY_SECRET_KEK_PASSPHRASE, salt, n, r, p, 32))` with
    `openssl::pkcs5::scrypt`.
- **DEK.** `Fernet(KEK).decrypt(wrapped_dek)`.
- **Secret reads.** `secret(name)` looks up the global row as
  `SecretStore._lookup_secret_row` does and returns
  `Fernet(DEK).decrypt(encrypted_value)`.
- **Sealing.**
  - Seal key: HKDF-SHA256 with ikm `urlsafe_b64decode(DEK)`, no salt, info
    `gobby.secret-store.seal.v1`, length 32.
  - `seal(plaintext, aad)` returns `b64(nonce12 ‖ AES-256-GCM(ciphertext ‖ tag))`.
  - `open_sealed` rejects tokens shorter than 28 bytes and any AAD mismatch.
- **Missing row, existing secrets.** Before any initialization, run Python's guard
  from `SecretStore._get_dek`: `SELECT 1 FROM secrets LIMIT 1`. When a row exists,
  return `SecretError::Unavailable` without creating or replacing the KEK file and
  without inserting key material. A missing envelope beside encrypted secrets is
  corruption, and a fresh DEK would make those secrets undecryptable.
- **Missing row, no secrets, key-file posture.** Initialize as
  `SecretStore._initialize_envelope` and `_get_or_create_kek_file_key` do:
  1. Create `<home>` if needed, then read `<home>/.secret_kek` if it exists.
  2. Otherwise publish a new Fernet key with `publish_private_file`, a port of
     `secrets.py::_publish_private_file`:
     - open `<home>/.<name>.<pid>.<uuid4 hex>.tmp` with
       `O_WRONLY | O_CREAT | O_EXCL` and mode 0600, `fchmod` it to 0600, write all
       bytes, `fsync`, close;
     - `link(temp, final)`, which never replaces an existing file; `EEXIST` means
       another initializer won, so read the winner's file;
     - always unlink the temp file, and `fsync` the directory only when this call
       published.

     A reader therefore sees either no KEK file or a complete one, never a partial
     write; leftover `.secret_kek.*.tmp` files are ignored.
  3. Generate a DEK, wrap it, and insert the key material only if absent.
  4. On a lost race, re-read the row and unwrap the winner. A missing winner row is
     an error.

  A `scrypt_passphrase` envelope is never initialized natively.
- **Unwrap failure.** Becomes `SecretError::Unavailable`, which handlers map to 503
  `secret_key_unavailable`.

The shared vector (`tests/fixtures/secret_seal_vector.json`) holds:

- a key-file KEK;
- the wrapped DEK row;
- a Python-sealed token with its AAD and plaintext;
- a Rust-sealed token with its AAD and plaintext;
- a Python-encrypted `secrets` value.

Each half is generated once and committed; the tests only read the committed bytes:

- Python's half (KEK, wrapped DEK, Python-sealed token, encrypted secret) comes from
  one `uv run python -c` call into `src/gobby/storage/secrets.py::SecretStore` with
  a fixed key-file KEK.
- Rust's half comes from
  `crates/gruntime/tests/secrets.rs::generate_rust_seal_vector`, an `#[ignore]` test
  that unwraps the committed DEK, seals a fixed plaintext and AAD, and writes the
  token into the fixture. The executor runs it once with `--run-ignored only`,
  then commits the fixture.

Each language then opens the other's token from the committed file.

Research context:
- The Python source of truth is `src/gobby/storage/secrets.py`:
  - `SecretStore._kek_fernet`, `_unwrap_dek`, `_seal_aes_key`, `seal`,
    `open_sealed` and `get`;
  - `_publish_private_file` (temp name, `O_EXCL` 0600, `fsync`, `os.link`, unlink,
    directory `fsync` on publish), `_get_or_create_kek_file_key` (re-read the
    winner after a lost publish) and `_get_dek` (the existing-secrets guard and its
    `RuntimeError` "Secret envelope key material is missing while encrypted secrets
    exist.");
  - `_derive_scrypt_fernet_key` with `SCRYPT_N = 2**14`, `SCRYPT_R = 8` and
    `SCRYPT_P = 1`;
  - `_AES_GCM_NONCE_LEN = 12` and `_AES_GCM_MIN_TOKEN_LEN = 28`;
  - `POSTURE_KEY_FILE = "key_file"`.
- `fernet` 0.2.2 is a non-optional gcore dependency that no Rust code uses today
  (`crates/gcore/Cargo.toml` comment). `openssl` 0.10 (vendored) provides HKDF
  (`pkey_ctx`), AES-GCM (`symm::encrypt_aead`) and scrypt. Neither adds a crate to
  `Cargo.lock`.
- `gobby_daemon_runtime`, the pool's session role, has SELECT, INSERT, UPDATE and
  DELETE on `secret_key_material` (baseline grant).
- Planned checks:
  - `cargo nextest run -p gobby-runtime --test secrets`;
  - `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/storage/test_secret_seal_vector.py -v`.

Consumers unchanged:
- `src/gobby/cli/install_setup_gclient.py` — no-edit-reason: reads only the `gclient` pin, which no leaf of this plan bumps.
- `src/gobby/cli/install_setup_gcode.py` — no-edit-reason: reads the `gcode` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_ghook.py` — no-edit-reason: reads the `ghook` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_gterm.py` — no-edit-reason: reads only the `gterm` pin, which no leaf of this plan bumps.
- `src/gobby/cli/install_setup_versions.py` — no-edit-reason: looks a pin up by the binary name it is given.
- `src/gobby/code_index/gcode_gateway.py` — no-edit-reason: derives `MIN_GCODE_GRAPH_VERSION` from the `gcode` pin at import.
- `src/gobby/hooks/runtime_compat.py` — no-edit-reason: derives `MINIMUM_GHOOK_VERSION_FOR_SUPPORTED_SCHEMA` from the `ghook` pin at import.
- `src/gobby/install/bin_freshness_models.py` — no-edit-reason: iterates every pin, so the bumped values flow through.
- `src/gobby/install/distribution.py` — no-edit-reason: looks a pin up by binary name.
- `tests/cli/installers/test_grok_installer.py` — no-edit-reason: reads the `ghook` pin instead of a version literal.
- `tests/cli/test_install_ghook.py` — no-edit-reason: builds its fixtures from the `ghook` pin instead of a version literal.
- `tests/cli/test_install_setup.py` — no-edit-reason: reads the `gcode` pin instead of a version literal.
- `tests/cli/test_install_setup_gclient.py` — no-edit-reason: reads the `gclient` pin instead of a version literal.
- `tests/cli/test_install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin instead of a version literal.
- `tests/cli/test_install_setup_gterm.py` — no-edit-reason: reads the `gterm` and `gclient` pins instead of version literals.
- `tests/code_index/test_gcode_gateway.py` — no-edit-reason: reads the `gcode` pin instead of a version literal.
- `tests/hooks/test_runtime_compat.py` — no-edit-reason: compares the derived floor to the `ghook` pin instead of a version literal.
- `tests/install/test_distribution.py` — no-edit-reason: reads pins by binary name; its `0.1.0` literal is a fake `gcode` output, not a pin.
- `tests/install/test_version_pins.py` — no-edit-reason: compares each pin to its crate manifest, which is this leaf's bump check.

**Acceptance:**

- 3.3.1 - Rust opens the Python-sealed token and decrypts the Python-encrypted secret from the committed vector. test: `crates/gruntime/tests/secrets.rs::opens_python_sealed_vector`.
- 3.3.2 - Python opens the Rust-sealed token from the committed vector. test: `tests/storage/test_secret_seal_vector.py::test_python_opens_rust_sealed_vector`.
- 3.3.3 - DEK unwrap works for the key-file and scrypt postures against Python-written rows. test: `crates/gruntime/tests/secrets.rs::unwraps_both_postures`.
- 3.3.4 - Two concurrent key-file initializations converge on one DEK and a 0600 KEK file; a leftover partial `.secret_kek.*.tmp` is ignored by the reader; and a publish that fails before the link (an unwritable directory) leaves neither a final nor a temp file. test: `crates/gruntime/tests/secrets.rs::concurrent_envelope_initialization_converges`.
- 3.3.5 - A wrong AAD or a truncated token fails to open. test: `crates/gruntime/tests/secrets.rs::open_rejects_wrong_aad_and_truncation`.
- 3.3.6 - On an isolated database seeded with an encrypted `secrets` row and no `secret_key_material` row, initialization returns `SecretError::Unavailable`, and the KEK file and both tables are unchanged. test: `crates/gruntime/tests/secrets.rs::missing_envelope_with_existing_secrets_fails_closed`.
- 3.3.7 - `gdaemon` is one patch version above the executor's starting tip in its `Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`, so this leaf's cutover passes the release-identity preflight. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 3.4 Native interactive handshake and grant capabilities [category: code] (depends: 3.3)
`kind: deliverable`

Targets:
- `crates/gruntime/src/lib.rs`
- `crates/gruntime/src/handshake.rs`
- `crates/gruntime/src/capabilities.rs`
- `crates/gruntime/tests/handshake.rs`
- `crates/gruntime/tests/capabilities.rs`
- `crates/gruntime/tests/fixtures/capability_vectors.json`
- `tests/runtime_grants/test_capability_vectors.py`
- `crates/gdaemon/Cargo.toml`
- `src/gobby/install/version_pins.py::MANAGED_BIN_VERSION_PINS`
- `Cargo.lock`

Release ownership (Constraints): gruntime is in gdaemon's release closure, so this
leaf bumps `gdaemon` by 0.0.1 from the executor's starting tip.

This deliverable adds one `NATIVE_ROUTES` row: `POST /api/runtime/handshake` for
`Operator`. The front door never sets `x-gobby-key-id` beside a `gobby-agent-v1.`
bearer, so an agent handshake has no `Operator` caller; 4.6 adds the `Agent` row.
The handler mirrors
`src/gobby/servers/routes/runtime_handshake.py::handshake` together with
`src/gobby/runtime_grants/handshake.py::HandshakeService.issue_for_operator`:

1. Parse `{machine_id, project_id, session_id?, code_overlay_project_id?}` and reject
   unknown fields. As in `runtime_handshake.py::HandshakeRequest`, `machine_id` and
   `project_id` are required strings, and the two optional fields may be absent or
   `null` (`tests/e2e/test_hub_node_pair.py` sends `"session_id": null`). An invalid
   body returns 422 `{"detail": "invalid handshake request"}`.
   This is a documented divergence from FastAPI's validation list; gcore never sends
   such a body.
2. Require `machine_id` to equal the `x-gobby-machine-id` identity header.
3. Admit the project with `SELECT 1 FROM projects WHERE id = $1 AND deleted_at IS NULL`.
4. Normalize the overlay claim as `_normalized_overlay_claim` does: it must be a
   UUID and must differ from the project.

   Steps 2–4 reject with 403 `claims_mismatch`.
5. Run `SELECT <auth>.heartbeat_daemon($daemon_machine, interval '2 minutes')`.
6. In one transaction:
   - Call `<auth>.issue_or_reuse_interactive_principal(token, daemon_machine, project, session, now + 1h, password, overlay)`.
     The password is 32 random bytes as base64url without padding.
   - When `reused`, load and open the sealed material.
   - Otherwise seal the password and store it.
   - Follow `interactive_credentials.py::_load_interactive_password`,
     `_store_interactive_password` and `_interactive_aad` exactly.
7. Build the DSN from the bootstrap `database_url`, with user and password replaced,
   in libpq key=value form, as `_interactive_dsn` does.
8. Issue the grant:
   - `PostgresDirect { dsn, role_name, credential_generation, valid_until }`;
   - TTL `min(3600, valid_until − now)` and at least 1;
   - `config_revision` and capabilities from 2.2's published active state, read
     with 3.1's `config.rs::active_state`. With no row the handler returns 503
     `{"detail": "handshake service unavailable"}`, Python's body while its
     handshake service is absent;
   - `GrantBundle { version 2, api_contract 1, config_revision, deployment {token, fencing_epoch}, schema_identity, principal {interactive, machine, project, execution_id: null, session_id, overlay}, capabilities, issued_at, expires_at }`;
   - `sign_grant`.
9. Respond 200
   `{"grant": …, "deployment_token": …, "fencing_epoch": …}` with `no-store`.

Failure mapping:

- Overlay SQLSTATE 23503 or 22023 maps to `claims_mismatch`, as
  `_raise_if_overlay_rejected` does.
- Any other issuance failure returns 403 `credential_issuance_failed`.
- A missing fence or an unavailable secret store returns 503.

`capabilities.rs` ports `src/gobby/runtime_grants/service.py::capabilities_from_snapshot`
against the row's `active_values` and `active_bindings`, as Python reads
`snapshot.active` and `snapshot.active_secret`. A binding's plaintext is 3.3's
`open_sealed(sealed, aad = "config-binding:{deployment_token}:{key}:{fingerprint}")`,
and a binding whose `sealed` is null has none:

- **falkordb.** Host and port come from config. The password comes from the
  secret-reference binding for `databases.falkordb.password` (via 3.3), else a
  plaintext value. Mode is `direct`, `brokered` or `unavailable`, following
  `_falkor_capability`.
- **qdrant.** URL and `api_key` follow `_qdrant_capability` and
  `_url_requires_broker`.
- **embed.** Enabled when the embeddings model is set and resolved.
- **text_generate, tool_chat.** Always daemon.
- **vision_extract.** Enabled when any generation endpoint's input modalities
  include `image`.
- **audio_transcribe.** Enabled when voice is enabled with STT, or any
  OpenAI-compatible audio binding has transcription enabled.
- **broker_operations.** In order: embed, falkor operations, qdrant operations.

Map each typed path to its flat contract key the way
`config_repository.py::ConfigRepository._runtime_path` does.

`tests/fixtures/capability_vectors.json` holds config-value sets with the expected
capability JSON from Python. The Python test asserts the vectors still match
`capabilities_from_snapshot`, and the Rust test asserts the port matches them.

Research context:
- Python issuance:
  - `src/gobby/runner_init/servers.py::issue_grant_postgres`, interactive branch,
    with a 1 h expiry;
  - `src/gobby/storage/interactive_credentials.py::InteractiveCredentialMixin.issue_interactive`;
  - `src/gobby/storage/managed_credentials.py`, where `heartbeat` uses
    `DAEMON_LEASE_DURATION` = 2 minutes;
  - `src/gobby/runtime_grants/handshake.py::HandshakeService._issue` (TTL);
  - `src/gobby/runtime_grants/service.py::GrantService.issue` (bundle fields). It
    captures the active bundle (`self.runtime.capture()`) and takes
    `config_revision` from that snapshot, which 2.2 publishes on every swap.
- In-process state:
  - Python's per-principal `threading.Lock` is process-local. The SQL function's
    advisory transaction lock gives the same guarantee across processes.
  - `remember_interactive_grant_expiry` and `rotate_interactive` have no production
    callers, so native issuance needs no in-memory expiry tracking.
- `gobby_daemon_runtime` holds EXECUTE on the auth functions (baseline grants).
- Planned checks:
  - `cargo nextest run -p gobby-runtime --test handshake --test capabilities`;
  - `DATABASE_URL=… GOBBY_TEST_PROTECT=1 uv run pytest tests/runtime_grants/test_capability_vectors.py -v`.

Consumers unchanged:
- `src/gobby/cli/install_setup_gclient.py` — no-edit-reason: reads only the `gclient` pin, which no leaf of this plan bumps.
- `src/gobby/cli/install_setup_gcode.py` — no-edit-reason: reads the `gcode` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_ghook.py` — no-edit-reason: reads the `ghook` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_gterm.py` — no-edit-reason: reads only the `gterm` pin, which no leaf of this plan bumps.
- `src/gobby/cli/install_setup_versions.py` — no-edit-reason: looks a pin up by the binary name it is given.
- `src/gobby/code_index/gcode_gateway.py` — no-edit-reason: derives `MIN_GCODE_GRAPH_VERSION` from the `gcode` pin at import.
- `src/gobby/hooks/runtime_compat.py` — no-edit-reason: derives `MINIMUM_GHOOK_VERSION_FOR_SUPPORTED_SCHEMA` from the `ghook` pin at import.
- `src/gobby/install/bin_freshness_models.py` — no-edit-reason: iterates every pin, so the bumped values flow through.
- `src/gobby/install/distribution.py` — no-edit-reason: looks a pin up by binary name.
- `tests/cli/installers/test_grok_installer.py` — no-edit-reason: reads the `ghook` pin instead of a version literal.
- `tests/cli/test_install_ghook.py` — no-edit-reason: builds its fixtures from the `ghook` pin instead of a version literal.
- `tests/cli/test_install_setup.py` — no-edit-reason: reads the `gcode` pin instead of a version literal.
- `tests/cli/test_install_setup_gclient.py` — no-edit-reason: reads the `gclient` pin instead of a version literal.
- `tests/cli/test_install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin instead of a version literal.
- `tests/cli/test_install_setup_gterm.py` — no-edit-reason: reads the `gterm` and `gclient` pins instead of version literals.
- `tests/code_index/test_gcode_gateway.py` — no-edit-reason: reads the `gcode` pin instead of a version literal.
- `tests/hooks/test_runtime_compat.py` — no-edit-reason: compares the derived floor to the `ghook` pin instead of a version literal.
- `tests/install/test_distribution.py` — no-edit-reason: reads pins by binary name; its `0.1.0` literal is a fake `gcode` output, not a pin.
- `tests/install/test_version_pins.py` — no-edit-reason: compares each pin to its crate manifest, which is this leaf's bump check.

**Acceptance:**

- 3.4.1 - An identified operator handshake returns a signed interactive grant whose payload fields equal Python's for the same database state. test: `crates/gruntime/tests/handshake.rs::interactive_handshake_issues_signed_grant`.
- 3.4.2 - A repeat handshake reuses the principal and returns the stored password, and a fresh issuance stores material that Python opens. test: `crates/gruntime/tests/handshake.rs::reused_principal_returns_the_sealed_password`.
- 3.4.3 - A machine mismatch, an unadmitted project and a bad overlay return 403 `claims_mismatch`, other issuance failures return 403 `credential_issuance_failed`, and with no published active state the handshake returns 503 `handshake service unavailable`. test: `crates/gruntime/tests/handshake.rs::handshake_rejections_match_python_codes`.
- 3.4.4 - Cookie-only and unauthenticated handshakes are not claimed and reach Python unchanged. test: `crates/gruntime/tests/handshake.rs::callerless_handshakes_are_not_claimed`.
- 3.4.5 - The Rust capability port matches every Python-generated vector. test: `crates/gruntime/tests/capabilities.rs::capabilities_match_python_vectors`.
- 3.4.6 - The committed vectors still match Python's `capabilities_from_snapshot`. test: `tests/runtime_grants/test_capability_vectors.py::test_vectors_match_python_capabilities`.
- 3.4.7 - `gdaemon` is one patch version above the executor's starting tip in its `Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`, so this leaf's cutover passes the release-identity preflight. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 3.5 Compare check for proxied handshake grants [category: code] (depends: 3.4)
`kind: deliverable`

Targets:
- `crates/gruntime/src/lib.rs`
- `crates/gruntime/src/verify.rs`
- `crates/gruntime/tests/verify.rs`
- `crates/gdaemon/Cargo.toml`
- `src/gobby/install/version_pins.py::MANAGED_BIN_VERSION_PINS`
- `Cargo.lock`

Release ownership (Constraints): gruntime is in gdaemon's release closure, so this
leaf bumps `gdaemon` by 0.0.1 from the executor's starting tip.

`RuntimeFamily::check_proxied` handles a claimed operator `POST /api/runtime/handshake`
whose proxied status is 200. Any other status returns `Skipped`. For a 200 it
parses `{grant, deployment_token, fencing_epoch}` and returns `Verified` only when
all of the following hold:

- `verify_static` passes against the current fence;
- the top-level `deployment_token` and `fencing_epoch` equal the grant's;
- the principal equals the request's claims: kind `interactive`, and the same
  machine, project, session and overlay;
- every capability except `postgres` equals a fresh native computation from the
  published active state (2.2, 3.1, 3.4);
- `expires_at ≤ issued_at + 3600` and `expires_at ≤ postgres.valid_until`.

The row is read first. When its `revision` differs from the grant's
`config_revision`, an activation landed between Python's issuance and the check,
the capabilities cannot be recomputed for the grant's revision, and the check
returns `Skipped`. Otherwise it returns `Mismatch(<first differing JSON pointer>)`
when any condition fails, never values. The
project, session and overlay claims come from the `request` bytes that 1.1's
`check_proxied` signature carries and 2.1 passes from compare's single buffered
copy; the machine comes from the `x-gobby-machine-id` header on `parts`. An
unparseable request body returns `Mismatch("/request")`.

Research context:
- D3 rules this check because a native leg would issue a second credential. Python
  and Rust sign with the same `deployment_runtime.grant_signing_secret`, so
  signature verification is a real cross-implementation check. The capability
  recomputation validates 3.4's port on live traffic.
- Python issues from its active bundle and 2.2 publishes that bundle on every swap,
  so both sides read one revision. A swap between issuance and the check moves the
  row; the check skips rather than compare a different revision, and the config GET
  compares (3.1, 5.6) own the row's freshness.
- Planned check: `cargo nextest run -p gobby-runtime --test verify`.

Consumers unchanged:
- `src/gobby/cli/install_setup_gclient.py` — no-edit-reason: reads only the `gclient` pin, which no leaf of this plan bumps.
- `src/gobby/cli/install_setup_gcode.py` — no-edit-reason: reads the `gcode` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_ghook.py` — no-edit-reason: reads the `ghook` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_gterm.py` — no-edit-reason: reads only the `gterm` pin, which no leaf of this plan bumps.
- `src/gobby/cli/install_setup_versions.py` — no-edit-reason: looks a pin up by the binary name it is given.
- `src/gobby/code_index/gcode_gateway.py` — no-edit-reason: derives `MIN_GCODE_GRAPH_VERSION` from the `gcode` pin at import.
- `src/gobby/hooks/runtime_compat.py` — no-edit-reason: derives `MINIMUM_GHOOK_VERSION_FOR_SUPPORTED_SCHEMA` from the `ghook` pin at import.
- `src/gobby/install/bin_freshness_models.py` — no-edit-reason: iterates every pin, so the bumped values flow through.
- `src/gobby/install/distribution.py` — no-edit-reason: looks a pin up by binary name.
- `tests/cli/installers/test_grok_installer.py` — no-edit-reason: reads the `ghook` pin instead of a version literal.
- `tests/cli/test_install_ghook.py` — no-edit-reason: builds its fixtures from the `ghook` pin instead of a version literal.
- `tests/cli/test_install_setup.py` — no-edit-reason: reads the `gcode` pin instead of a version literal.
- `tests/cli/test_install_setup_gclient.py` — no-edit-reason: reads the `gclient` pin instead of a version literal.
- `tests/cli/test_install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin instead of a version literal.
- `tests/cli/test_install_setup_gterm.py` — no-edit-reason: reads the `gterm` and `gclient` pins instead of version literals.
- `tests/code_index/test_gcode_gateway.py` — no-edit-reason: reads the `gcode` pin instead of a version literal.
- `tests/hooks/test_runtime_compat.py` — no-edit-reason: compares the derived floor to the `ghook` pin instead of a version literal.
- `tests/install/test_distribution.py` — no-edit-reason: reads pins by binary name; its `0.1.0` literal is a fake `gcode` output, not a pin.
- `tests/install/test_version_pins.py` — no-edit-reason: compares each pin to its crate manifest, which is this leaf's bump check.

**Acceptance:**

- 3.5.1 - A Python-issued grant for the request records `verified`. test: `crates/gruntime/tests/verify.rs::python_issued_grant_verifies`.
- 3.5.2 - A tampered capability, a wrong epoch, a bad signature or a principal mismatch records `mismatch` with the field pointer and no values. test: `crates/gruntime/tests/verify.rs::mismatches_name_the_field_without_values`.
- 3.5.3 - A grant whose `config_revision` differs from the published row's revision records `skipped`. test: `crates/gruntime/tests/verify.rs::moved_config_revision_skips`.
- 3.5.4 - `gdaemon` is one patch version above the executor's starting tip in its `Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`, so this leaf's cutover passes the release-identity preflight. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

## P4: Managed principals
`kind: framing`

Orchestrator #15977 ruled on 2026-10-14 that the family serves managed principals
natively: the managed challenge, capability-token (`gobby-agent-v1.`) handshakes for
agent_run, tool_chat and maintenance, and managed grant presentation. Native code
verifies capability tokens itself. Launch-time creation
(`src/gobby/runtime_grants/launch.py::materialize_managed_launch`) and the background
rotation loop (`src/gobby/storage/managed_credentials.py::ManagedCredentialManager.rotate_due`)
stay in Python as the end state (Constraints). Both sides coordinate through the
database: the SQL issuer functions serialize each execution with advisory locks and
generation compare-and-set, and 4.1 makes revocation durable.

The leaves are linear: 4.1 shares `Cargo.lock` and the version pins with every
leaf, and 4.2 through 4.7 share `crates/gruntime/src/lib.rs`.

### 4.1 Durable managed revocation read [category: code] (depends: 3.5)
`kind: deliverable`

Targets:
- `crates/gcore/assets/schema/migrations/470_managed_principal_revoked.sql`
- `crates/gcore/src/schema/assets.rs::MIGRATIONS`
- `crates/gcore/assets/schema/catalog.manifest.json::*` — scope-reason: regenerated; its `functions` list gains `managed_principal_revoked`
- `crates/gcore/src/grant/bundle.rs::*` — scope-reason: the non-postgres `expected_schema_identity` literal `latest_version` and the `GOLDEN_LATEST_CHECKSUM` and `GOLDEN_ASSETS_ROOT_HASH` constants take the new latest migration's identity
- `crates/gcore/tests/schema_contract.rs::embedded_assets_publish_a_complete_schema_identity`
- `crates/gdaemon/tests/cli_contract.rs::*` — scope-reason: the contract names it a derived carrier of the embedded schema assets; its schema assertions read the embedded identity and must pass against the new migration, editing any that pins a literal
- `src/gobby/storage/schema_expected_identity.json::*` — scope-reason: regenerated; `latest_version`, `latest_checksum` and `assets_root_hash` change
- `tests/runtime_grants/golden/brokered_datastores.json::*` — scope-reason: the signed schema identity changes, so the payload checksum and signature are recomputed
- `tests/runtime_grants/golden/direct_datastores.json::*` — scope-reason: the signed schema identity changes, so the payload checksum and signature are recomputed
- `tests/runtime_grants/golden/old_client_new_grant.json::*` — scope-reason: the signed schema identity changes, so the payload checksum and signature are recomputed
- `tests/runtime_grants/golden/payload_skew_unknown_field.json::*` — scope-reason: the signed schema identity changes, so the payload checksum and signature are recomputed
- `tests/runtime_grants/golden/unavailable_datastores.json::*` — scope-reason: the signed schema identity changes, so the payload checksum and signature are recomputed
- `src/gobby/storage/interactive_credentials.py::InteractiveCredentialMixin`
- `src/gobby/runner_init/servers.py::*` — scope-reason: the nested `_principal_revoked` in `_bind_runtime_grants` becomes module-level `_grant_principal_revoked`, which `_bind_runtime_grants` binds with `functools.partial`
- `tests/storage/test_managed_revocation_read.py`
- `crates/gdaemon/Cargo.toml`
- `crates/gcode/Cargo.toml`
- `crates/ghook/Cargo.toml`
- `src/gobby/install/version_pins.py::MANAGED_BIN_VERSION_PINS`
- `Cargo.lock`

Release ownership (Constraints): this leaf changes gcore's embedded schema assets, so
it bumps `gdaemon`, `gcode` and `ghook` by 0.0.1 from the executor's starting tip.

Python records managed revocation in memory only.
`ManagedCredentialManager.revoke` calls the process-local
`GrantRevocationStore.revoke_execution(execution, generation)` after
`revoke_principal` succeeds, and `_bind_runtime_grants`'s `_principal_revoked`
answers only interactive grants. Native revocation (4.4's issuance rollback and
4.5's rotation rollback) never reaches that in-memory set, so a managed grant
presented to Python could outlive its credential. This leaf makes the database the
truth for both implementations.

Add migration `470_managed_principal_revoked.sql`. 470 is the next free number at
plan time, after 2.2's 469; the executor takes the next free number at its starting
tip and uses it in the filename and in every carrier below.

```sql
-- A managed generation is revoked once its binding is revoked or gone. A requested
-- revocation alone does not count: a draining predecessor stays usable.
CREATE FUNCTION gobby_agent_auth.managed_principal_revoked(p_execution_id uuid, p_generation integer)
RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path TO 'pg_catalog', 'gobby_agent_auth'
AS $$
    SELECT NOT EXISTS (
        SELECT 1 FROM gobby_agent_auth.principal_bindings pb
        WHERE pb.managed_execution_id = p_execution_id
          AND pb.credential_generation = p_generation
          AND pb.revoked_at IS NULL
    )
$$;
ALTER FUNCTION gobby_agent_auth.managed_principal_revoked(uuid, integer) OWNER TO gobby_agent_issuer;
REVOKE ALL ON FUNCTION gobby_agent_auth.managed_principal_revoked(uuid, integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION gobby_agent_auth.managed_principal_revoked(uuid, integer) TO gobby_daemon_runtime;
```

Regenerate the derived carriers; none is hand-edited except the literals named here:

1. `assets.rs::MIGRATIONS` gains the `EmbeddedMigration` entry: version, filename,
   the file's sha256 checksum and its `include_str!`.
2. `catalog.manifest.json`:
   `UPDATE_GCORE_SCHEMA_MANIFEST=1 GOBBY_SCHEMA_TEST_DATABASE_URL=<test-dsn> cargo nextest run -p gobby-core --features postgres -E 'test(catalog_manifest_is_fresh_for_embedded_assets)'`.
3. `bundle.rs`: the non-postgres branch's `latest_version` literal and the two
   golden constants take the values `SchemaIdentityContract::embedded()` now
   reports.
4. `schema_contract.rs`: the latest version and filename assertions.
5. `schema_expected_identity.json`:
   `uv run python scripts/generate_schema_expected_identity.py --gdaemon <this checkout's target/debug/gdaemon>`.
6. The five golden grants: recompute each `payload_checksum` and re-sign with
   `GOLDEN_SECRET` (`tests/runtime_grants/support.py`), as the
   `tests/runtime_grants/test_golden_vectors.py` docstring prescribes.

Python reads the function:

- `InteractiveCredentialMixin.managed_generation_revoked(self, *, execution_id: UUID, generation: int) -> bool`
  runs `SELECT {self.auth_schema}.managed_principal_revoked(%s::uuid, %s::integer) AS revoked`.
  It returns true when no row comes back or `revoked` is true, reading the column
  with the module's `_row_value`. It sits on the mixin because
  `interactive_credentials.py` has 428 lines while `managed_credentials.py` has 957,
  over the contract's 850-line decomposition heuristic. `ManagedCredentialManager`
  inherits the mixin, so the method is reachable wherever `interactive_generation_revoked` is.
- Module-level `_grant_principal_revoked(credentials, grant) -> bool` replaces the
  nested `_principal_revoked`:
  - a non-integer `credential_generation` returns false, as today;
  - `interactive` keeps today's `interactive_generation_revoked` call;
  - `agent_run`, `tool_chat` and `maintenance` with an `execution_id` call
    `managed_generation_revoked(execution_id=UUID(execution_id), generation=generation)`;
  - anything else returns false.

  `_bind_runtime_grants` passes
  `functools.partial(_grant_principal_revoked, credentials)` to
  `revocations.set_principal_revoked`.

Research context:
- `src/gobby/runtime_grants/revocation.py::GrantRevocationStore.is_revoked` checks the
  checksum set, the in-memory execution set, the in-memory interactive set, then the
  `_principal_revoked` callback. `revoke_execution` has one caller, after a
  successful `revoke_principal`, which sets `revoked_at`. The durable read is
  therefore a superset of the in-memory set.
- `revocation_requested_at` marks a draining predecessor that stays usable until
  `predecessor_drain_deadline` (migration 466, `managed_binding_states`). Only
  `revoked_at` ends a generation.
- The function copies migration 466's boilerplate: `SECURITY DEFINER`, the pinned
  `search_path`, owner `gobby_agent_issuer`, and execute for `gobby_daemon_runtime`
  only. The runner rewrites `gobby_agent_auth` to the hub's auth schema, which the
  catalog shows as `$auth_schema`.
- The carriers come from a literal sweep for the current latest migration
  (`gcode grep -F '468' crates/gcore src/gobby/storage tests/runtime_grants`).
- Python tests pick their gdaemon through `tests/fixtures/gdaemon_binary.py`. The
  installed binary lacks the migration until cutover, so this leaf's Python tests run
  with `GOBBY_TEST_GDAEMON=checkout` after `cargo build -p gobby-daemon`.
- Planned checks:
  - `GOBBY_SCHEMA_TEST_DATABASE_URL=<test-dsn> cargo nextest run -p gobby-core --features postgres --test schema_contract --test catalog_manifest_freshness`;
  - `GOBBY_TEST_GDAEMON=checkout DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/storage/test_managed_revocation_read.py tests/runtime_grants/test_golden_vectors.py -v`.

Consumers unchanged:
- `crates/gcore/src/grant/tests.rs` — no-edit-reason: it compares grants against `expected_schema_identity()` at run time and holds no migration literal.
- `src/gobby/storage/managed_credentials.py` — no-edit-reason: `ManagedCredentialManager` inherits the mixin; this leaf adds one method and changes no existing member.
- `src/gobby/cli/install_setup_gclient.py` — no-edit-reason: reads only the `gclient` pin, which no leaf of this plan bumps.
- `src/gobby/cli/install_setup_gcode.py` — no-edit-reason: reads the `gcode` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_ghook.py` — no-edit-reason: reads the `ghook` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_gterm.py` — no-edit-reason: reads only the `gterm` pin, which no leaf of this plan bumps.
- `src/gobby/cli/install_setup_versions.py` — no-edit-reason: looks a pin up by the binary name it is given.
- `src/gobby/code_index/gcode_gateway.py` — no-edit-reason: derives `MIN_GCODE_GRAPH_VERSION` from the `gcode` pin at import.
- `src/gobby/hooks/runtime_compat.py` — no-edit-reason: derives `MINIMUM_GHOOK_VERSION_FOR_SUPPORTED_SCHEMA` from the `ghook` pin at import.
- `src/gobby/install/bin_freshness_models.py` — no-edit-reason: iterates every pin, so the bumped values flow through.
- `src/gobby/install/distribution.py` — no-edit-reason: looks a pin up by binary name.
- `tests/cli/installers/test_grok_installer.py` — no-edit-reason: reads the `ghook` pin instead of a version literal.
- `tests/cli/test_install_ghook.py` — no-edit-reason: builds its fixtures from the `ghook` pin instead of a version literal.
- `tests/cli/test_install_setup.py` — no-edit-reason: reads the `gcode` pin instead of a version literal.
- `tests/cli/test_install_setup_gclient.py` — no-edit-reason: reads the `gclient` pin instead of a version literal.
- `tests/cli/test_install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin instead of a version literal.
- `tests/cli/test_install_setup_gterm.py` — no-edit-reason: reads the `gterm` and `gclient` pins instead of version literals.
- `tests/code_index/test_gcode_gateway.py` — no-edit-reason: reads the `gcode` pin instead of a version literal.
- `tests/hooks/test_runtime_compat.py` — no-edit-reason: compares the derived floor to the `ghook` pin instead of a version literal.
- `tests/install/test_distribution.py` — no-edit-reason: reads pins by binary name; its `0.1.0` literal is a fake `gcode` output, not a pin.
- `tests/install/test_version_pins.py` — no-edit-reason: compares each pin to its crate manifest, which is this leaf's bump check.

**Acceptance:**

- 4.1.1 - The migration defines `managed_principal_revoked(uuid, integer)` owned by `gobby_agent_issuer`, not executable by PUBLIC and executable by `gobby_daemon_runtime`. file: `crates/gcore/assets/schema/migrations/470_managed_principal_revoked.sql`.
- 4.1.2 - On the test hub, a live generation and a draining predecessor (revocation requested, `revoked_at` null) read not revoked, while a revoked generation and an unknown generation read revoked. test: `tests/storage/test_managed_revocation_read.py::test_managed_generation_revoked_follows_revoked_at`.
- 4.1.3 - `_grant_principal_revoked` reports a managed grant revoked once its binding is revoked in the database, with Python's in-memory set untouched, and its interactive answer is unchanged. test: `tests/storage/test_managed_revocation_read.py::test_principal_callback_reads_managed_revocation_from_the_database`.
- 4.1.4 - The embedded identity publishes the new latest migration. test: `crates/gcore/tests/schema_contract.rs::embedded_assets_publish_a_complete_schema_identity`.
- 4.1.5 - The catalog manifest matches the embedded assets. test: `crates/gcore/tests/catalog_manifest_freshness.rs::catalog_manifest_is_fresh_for_embedded_assets`.
- 4.1.6 - The re-signed golden grants round-trip under the new identity. test: `tests/runtime_grants/test_golden_vectors.py::test_grant_vectors_round_trip`.
- 4.1.7 - `gdaemon`, `gcode` and `ghook` are each one patch version above the executor's starting tip in their `Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`, so this leaf's cutover passes the release-identity preflight. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 4.2 Capability-token verification [category: code] (depends: 4.1)
`kind: deliverable`

Targets:
- `crates/gruntime/src/lib.rs`
- `crates/gruntime/src/agent.rs`
- `crates/gruntime/tests/agent.rs`
- `crates/gruntime/tests/config.rs`
- `crates/gruntime/tests/fixtures/agent_token_vectors.json`
- `tests/runtime_grants/test_agent_token_vectors.py`
- `crates/gdaemon/Cargo.toml`
- `src/gobby/install/version_pins.py::MANAGED_BIN_VERSION_PINS`
- `Cargo.lock`

Release ownership (Constraints): gruntime is in gdaemon's release closure, so this
leaf bumps `gdaemon` by 0.0.1 from the executor's starting tip.

`agent.rs` ports the capability-token half of
`src/gobby/servers/auth_service.py::AuthService._classify_agent_token`.

1. **Signing key.** `signing_key(home)` reads `<home>/bootstrap.yaml` per request
   with a 5 s timeout and takes `api_key` through
   `gobby_core::bootstrap::parse_hub_database_bootstrap`, as
   `crates/gdaemon/src/front_door/challenge.rs::answer` does. The key is
   HMAC-SHA256 with key `api_key` bytes over `b"gobby-managed-token-v1"`
   (`src/gobby/utils/local_token.py::derive_managed_signing_key`). A missing file,
   a parse failure or an absent `api_key` gives `None`. Python re-reads the file
   whenever its identity changes (`AuthService.managed_signing_key`), so a
   per-request read keeps key rotation equivalent. gdaemon reads the bootstrap at
   run time; tests write their own bootstrap into a temporary home.
2. **Classification.** `classify(token, key: Option<&[u8]>, now) -> Result<AgentClaims, AgentRejection>`
   ports `local_token.py::classify_agent_api_token` in its order:
   1. Split on `.` into at most three parts. Fewer than three parts, or a first
      part other than `gobby-agent-v1`, is `invalid_token`.
   2. No key is `signing_key_unavailable`.
   3. The signature must equal HMAC-SHA256(key, `<part0>.<part1>`) as received,
      compared with `openssl::memcmp::eq`. The payload must be a JSON object.
      Failures, including base64 and UTF-8 errors, are `capability_invalid`.
      Segments decode as Python's `_urlsafe_decode` does: pad to a multiple of four,
      then `base64.urlsafe_b64decode`, which discards characters outside the
      alphabet.
   4. Exactly one of `agent_run_id` and `managed_execution_id` is present, and it is
      a nonempty string; `session_id`, `project_id` and `machine_id` are nonempty
      strings; `iat` and `exp` are JSON integers, never booleans. Otherwise
      `capability_invalid`.
   5. `now >= exp` is `capability_expired`.
   6. `kind`, when present and not null, must be a nonempty string, otherwise
      `capability_invalid`.
3. **Minting.** `mint(claims, key) -> String` builds the payload as
   `managed_token_signing_payload` does: `exp`, `iat`, `machine_id`, `project_id`
   and `session_id`, plus whichever of `agent_run_id`, `managed_execution_id` and
   `kind` is not null. It serializes as Python's
   `json.dumps(sort_keys=True, separators=(",", ":"))`, with ASCII escaping. The
   token is `gobby-agent-v1.` + base64url-nopad(payload) + `.` +
   base64url-nopad(HMAC-SHA256(key, `gobby-agent-v1.<payload segment>`)). 4.3's
   proof and 4.5's grant capability use this encoder.
4. **Liveness.** This ports `AuthService._managed_capability_is_live`:
   - an `agent_run_id` claim needs `SELECT status FROM agent_runs WHERE id = $1`
     to return a status in `gobby.storage.agents.ACTIVE_AGENT_RUN_STATUSES`
     (`queued`, `pending`, `running`);
   - a `managed_execution_id` claim needs
     `SELECT <auth>.managed_execution_is_login_capable($1)` to be true.

   A malformed id or a database error is not live. Failure is `run_inactive`.
5. **Identity.** This ports `auth_service.py::_agent_identity_matches` with
   `bind_identity` true, because both runtime routes bind in `_AGENT_ROUTES`:
   - The caller project is `X-Gobby-Caller-Project-Id`, else `X-Gobby-Project-Id`.
     It and `X-Gobby-Session-Id` are required, and the project must equal the claim.
   - The session header must equal the claim exactly. The caller gate below
     forwards every other spelling.
   - `X-Gobby-Agent-Run-Id` requires an `agent_run_id` claim and
     `X-Gobby-Managed-Execution-Id` requires a `managed_execution_id` claim. The
     supplied owner must equal the claimed owner, and a request with no owner
     header fails.

   Failure is `identity_mismatch`.
6. **Rejections.** `verify(state, parts, now)` runs classification, liveness and
   identity in that order. A failure returns 401 `{"error": <message>, "code": <code>}`,
   as `src/gobby/servers/middleware/auth.py::AuthMiddleware.dispatch` does. The
   message is `_LOGIN_GUIDANCE` for `invalid_token` and `"Request rejected"`
   otherwise.

`lib.rs` resolves a request's caller in this order: `x-gobby-key-id` present is
`Operator`. Otherwise, `Authorization` splits once on whitespace (Python's
`split(maxsplit=1)`) into a scheme equal to `bearer`, ignoring case, and a token
starting with `gobby-agent-v1.`. That request is `Agent` only when both of these
hold:

- it has no `X-Gobby-Break-Glass` header;
- `X-Gobby-Session-Id` is absent or byte-equal to the `session_id` in the token's
  payload segment, decoded without verification.

Any other request has no caller. The unverified decode only selects the caller;
`verify` checks every claimed agent request. Python resolves session references
such as `#42` or UUID prefixes (`resolve_session_reference`), and gcore always sends
the canonical session UUID from its claims. Reference spellings therefore stay on
Python as the end state, and the resolver is not ported. Loopback break-glass
admission also stays on Python (`AuthService.break_glass_admits`).

This leaf adds no `NATIVE_ROUTES` row; 4.3, 4.6 and 4.7 do. It extends the
per-caller request builder in 3.1's `claims_follow_the_route_table` with `Agent`.

`tests/fixtures/agent_token_vectors.json` is generated once by `uv run python -c`
with a fixed `api_key` and a frozen `time.time`, then committed. It holds:

- one token each from `issue_agent_api_token`, `issue_tool_api_token` and
  `issue_maintenance_api_token`, including a non-ASCII session id that pins ASCII
  escaping;
- mutated tokens with Python's classification at the fixed `now`: a wrong version,
  two segments, a tampered payload, a tampered signature, a signature with an
  embedded non-alphabet character, a missing `iat`, a boolean `exp`, two owner
  claims, an empty `kind`, and an expired token whose `kind` is also invalid (Python
  reports `capability_expired` first).

Research context:
- Python reference: `src/gobby/utils/local_token.py` (`classify_agent_api_token`,
  `_issue_managed_api_token`, `managed_token_signing_payload`, `_urlsafe_decode`)
  and `src/gobby/servers/auth_service.py` (`_classify_agent_token`,
  `_legacy_rejection`, `_agent_identity_matches`, `_managed_capability_is_live`).
- `_legacy_rejection` checks break-glass, then the front-door identity, then the
  bearer, then the session cookie. The front door strips client identity headers
  and never sets `x-gobby-key-id` beside a `gobby-agent-v1.` bearer
  (`crates/gdaemon/src/front_door/auth.rs::AuthState::authenticate`), so `Operator`
  and `Agent` never overlap.
- `crates/gcore/src/grant/handshake.rs::parse_capability_token` is the client-side
  parse and is not the server classifier.
- Planned checks:
  - `cargo nextest run -p gobby-runtime --test agent --test config`;
  - `DATABASE_URL=… GOBBY_TEST_PROTECT=1 uv run pytest tests/runtime_grants/test_agent_token_vectors.py -v`.

Consumers unchanged:
- `src/gobby/cli/install_setup_gclient.py` — no-edit-reason: reads only the `gclient` pin, which no leaf of this plan bumps.
- `src/gobby/cli/install_setup_gcode.py` — no-edit-reason: reads the `gcode` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_ghook.py` — no-edit-reason: reads the `ghook` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_gterm.py` — no-edit-reason: reads only the `gterm` pin, which no leaf of this plan bumps.
- `src/gobby/cli/install_setup_versions.py` — no-edit-reason: looks a pin up by the binary name it is given.
- `src/gobby/code_index/gcode_gateway.py` — no-edit-reason: derives `MIN_GCODE_GRAPH_VERSION` from the `gcode` pin at import.
- `src/gobby/hooks/runtime_compat.py` — no-edit-reason: derives `MINIMUM_GHOOK_VERSION_FOR_SUPPORTED_SCHEMA` from the `ghook` pin at import.
- `src/gobby/install/bin_freshness_models.py` — no-edit-reason: iterates every pin, so the bumped values flow through.
- `src/gobby/install/distribution.py` — no-edit-reason: looks a pin up by binary name.
- `tests/cli/installers/test_grok_installer.py` — no-edit-reason: reads the `ghook` pin instead of a version literal.
- `tests/cli/test_install_ghook.py` — no-edit-reason: builds its fixtures from the `ghook` pin instead of a version literal.
- `tests/cli/test_install_setup.py` — no-edit-reason: reads the `gcode` pin instead of a version literal.
- `tests/cli/test_install_setup_gclient.py` — no-edit-reason: reads the `gclient` pin instead of a version literal.
- `tests/cli/test_install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin instead of a version literal.
- `tests/cli/test_install_setup_gterm.py` — no-edit-reason: reads the `gterm` and `gclient` pins instead of version literals.
- `tests/code_index/test_gcode_gateway.py` — no-edit-reason: reads the `gcode` pin instead of a version literal.
- `tests/hooks/test_runtime_compat.py` — no-edit-reason: compares the derived floor to the `ghook` pin instead of a version literal.
- `tests/install/test_distribution.py` — no-edit-reason: reads pins by binary name; its `0.1.0` literal is a fake `gcode` output, not a pin.
- `tests/install/test_version_pins.py` — no-edit-reason: compares each pin to its crate manifest, which is this leaf's bump check.

**Acceptance:**

- 4.2.1 - `classify` returns Python's classification for every committed vector. test: `crates/gruntime/tests/agent.rs::classification_matches_python_vectors`.
- 4.2.2 - `mint` reproduces the committed Python tokens byte for byte. test: `crates/gruntime/tests/agent.rs::mint_reproduces_python_tokens`.
- 4.2.3 - A finished agent run and a managed execution that is not login-capable return 401 `run_inactive`, and missing or mismatched project, session or owner headers return 401 `identity_mismatch`, each with Python's message. test: `crates/gruntime/tests/agent.rs::liveness_and_identity_follow_python`.
- 4.2.4 - A session-reference spelling, a break-glass header, a non-bearer scheme and an operator identity never resolve to `Agent`. test: `crates/gruntime/tests/agent.rs::agent_caller_gate_forwards_what_python_owns`.
- 4.2.5 - The committed vectors still match Python's classifier and issuers. test: `tests/runtime_grants/test_agent_token_vectors.py::test_vectors_match_python_classification`.
- 4.2.6 - `gdaemon` is one patch version above the executor's starting tip in its `Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`, so this leaf's cutover passes the release-identity preflight. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 4.3 Managed challenge as a family route [category: code] (depends: 4.2)
`kind: deliverable`

Targets:
- `crates/gruntime/src/lib.rs`
- `crates/gruntime/src/challenge.rs`
- `crates/gruntime/src/verify.rs`
- `crates/gruntime/tests/challenge.rs`
- `crates/gruntime/tests/config.rs`
- `crates/gdaemon/Cargo.toml`
- `src/gobby/install/version_pins.py::MANAGED_BIN_VERSION_PINS`
- `Cargo.lock`

Release ownership (Constraints): gruntime is in gdaemon's release closure, so this
leaf bumps `gdaemon` by 0.0.1 from the executor's starting tip.

`lib.rs` adds `Caller::Public`: a request with no `Authorization` header and no
`x-gobby-key-id`. Only this leaf's row lists it, so cookie-only, break-glass and
unauthenticated requests still match no listed caller on every other row and are
forwarded. The leaf extends 3.1's per-caller request builder with `Public`.

Row: `POST /api/runtime/handshake/challenge` for `Public`.
`crates/gdaemon/src/front_door/challenge.rs::answer` runs before dispatch on both the
backend and node doors. It rejects an `Authorization` header
(`credential_before_proof`), bounds the body (64 KiB, 10 s), denies unknown fields,
rejects a nonce over 44 characters or not base64url (padded or not), answers
`interactive` itself, rejects unknown kinds, and forwards only `managed` with the
body re-attached. The family route therefore sees managed challenges only.

`challenge.rs` mirrors `src/gobby/servers/routes/runtime_handshake.py::challenge` for
the managed kind, in Python's order:

1. Parse the body with the same shape. A failure, which `answer` makes unreachable
   in production, returns 422 `{"detail": "invalid challenge request"}`. Decode the
   nonce as `answer` does.
2. With no signing key (4.2), return 503 `{"detail": "signing_key_unavailable"}`.
3. Select claims as `_challenge_claims` does: `caller.claims` when present, else the
   top-level `claims`.
   - Absent claims, a `kind` that is present but not a nonempty string, a
     `session_id`, `project_id` or `machine_id` that is not a nonempty string, or an
     `iat` or `exp` that is not a non-boolean integer each return 403
     `claims_mismatch` through `Rejection::status()` and `Rejection::body()`.
   - Non-string `agent_run_id` and `managed_execution_id` values are dropped.
4. `secret` = HMAC-SHA256(key, `gobby-agent-v1.` + base64url-nopad(payload)), with
   4.2's encoder over the claims (`handshake.py::_recompute_capability_signature`).
   The proof is the lowercase hex HMAC-SHA256(secret, nonce).
5. Respond 200 `{"proof": <hex>}` with `Cache-Control: no-store`.

`verify.rs` adds the compare check. For a claimed challenge whose proxied status is
200, recompute the proof from the request bytes. Return `Verified` when it equals
the proxied `proof`, otherwise `Mismatch("/proof")`. Any other status returns
`Skipped`.

Research context:
- Rejected: answering the managed challenge in `challenge::answer` before dispatch.
  That would give the soak no compare evidence for it and `runtime: proxy` no
  fallback.
- Python's challenge path is public (`servers/middleware/auth.py::_PUBLIC_PATHS`), so
  cookies and break-glass headers do not change its answer.
- Planned check: `cargo nextest run -p gobby-runtime --test challenge --test config`.

Consumers unchanged:
- `src/gobby/cli/install_setup_gclient.py` — no-edit-reason: reads only the `gclient` pin, which no leaf of this plan bumps.
- `src/gobby/cli/install_setup_gcode.py` — no-edit-reason: reads the `gcode` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_ghook.py` — no-edit-reason: reads the `ghook` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_gterm.py` — no-edit-reason: reads only the `gterm` pin, which no leaf of this plan bumps.
- `src/gobby/cli/install_setup_versions.py` — no-edit-reason: looks a pin up by the binary name it is given.
- `src/gobby/code_index/gcode_gateway.py` — no-edit-reason: derives `MIN_GCODE_GRAPH_VERSION` from the `gcode` pin at import.
- `src/gobby/hooks/runtime_compat.py` — no-edit-reason: derives `MINIMUM_GHOOK_VERSION_FOR_SUPPORTED_SCHEMA` from the `ghook` pin at import.
- `src/gobby/install/bin_freshness_models.py` — no-edit-reason: iterates every pin, so the bumped values flow through.
- `src/gobby/install/distribution.py` — no-edit-reason: looks a pin up by binary name.
- `tests/cli/installers/test_grok_installer.py` — no-edit-reason: reads the `ghook` pin instead of a version literal.
- `tests/cli/test_install_ghook.py` — no-edit-reason: builds its fixtures from the `ghook` pin instead of a version literal.
- `tests/cli/test_install_setup.py` — no-edit-reason: reads the `gcode` pin instead of a version literal.
- `tests/cli/test_install_setup_gclient.py` — no-edit-reason: reads the `gclient` pin instead of a version literal.
- `tests/cli/test_install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin instead of a version literal.
- `tests/cli/test_install_setup_gterm.py` — no-edit-reason: reads the `gterm` and `gclient` pins instead of version literals.
- `tests/code_index/test_gcode_gateway.py` — no-edit-reason: reads the `gcode` pin instead of a version literal.
- `tests/hooks/test_runtime_compat.py` — no-edit-reason: compares the derived floor to the `ghook` pin instead of a version literal.
- `tests/install/test_distribution.py` — no-edit-reason: reads pins by binary name; its `0.1.0` literal is a fake `gcode` output, not a pin.
- `tests/install/test_version_pins.py` — no-edit-reason: compares each pin to its crate manifest, which is this leaf's bump check.

**Acceptance:**

- 4.3.1 - A managed challenge returns Python's proof for the same key, nonce and claims, with `no-store`. test: `crates/gruntime/tests/challenge.rs::managed_proof_matches_python`.
- 4.3.2 - A missing signing key returns 503 `signing_key_unavailable` before claims are examined, and each invalid claim returns 403 `claims_mismatch`. test: `crates/gruntime/tests/challenge.rs::challenge_rejections_follow_python_order`.
- 4.3.3 - In compare mode a Python proof records `verified` and a wrong proof records `mismatch` at `/proof`. test: `crates/gruntime/tests/challenge.rs::proxied_proof_is_recomputed`.
- 4.3.4 - `claims_follow_the_route_table` covers `Public`, and the challenge row is the only row that lists it. test: `crates/gruntime/tests/config.rs::claims_follow_the_route_table`.
- 4.3.5 - `gdaemon` is one patch version above the executor's starting tip in its `Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`, so this leaf's cutover passes the release-identity preflight. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 4.4 Managed credential issuance [category: code] (depends: 4.3)
`kind: deliverable`

Targets:
- `crates/gruntime/Cargo.toml`
- `crates/gruntime/src/lib.rs`
- `crates/gruntime/src/managed/mod.rs`
- `crates/gruntime/tests/managed.rs`
- `crates/gruntime/tests/fixtures/managed_bootstrap_vector.json`
- `tests/storage/test_managed_bootstrap_vector.py`
- `crates/gdaemon/Cargo.toml`
- `src/gobby/install/version_pins.py::MANAGED_BIN_VERSION_PINS`
- `Cargo.lock`

Release ownership (Constraints): gruntime is in gdaemon's release closure, so this
leaf bumps `gdaemon` by 0.0.1 from the executor's starting tip.

`managed/mod.rs` ports issuance and revocation from
`src/gobby/storage/managed_credentials.py::ManagedCredentialManager`:

- **Expiry.** Port `_validate_expiry`: the expiry must be after `issued_at = now`
  and within `MAX_ROLE_LIFETIME`.
- **`issue`** (agent_run and tool_chat) takes execution, owner kind, session and
  agent run, and runs in this order:
  1. 3.4's `heartbeat_daemon` call.
  2. Password: 32 random bytes as base64url without padding
     (`secrets.token_urlsafe(32)`).
  3. tool_chat calls
     `<auth>.issue_tool_principal(execution, session, daemon_machine, expiry, password, NULL)`.
     agent_run calls
     `<auth>.issue_principal(execution, owner_kind, session, agent_run, daemon_machine, expiry, password, NULL)`.
  4. Build the scoped DSN with 3.4's DSN builder plus
     `application_name=gobby-agent-<execution>` (`_scoped_dsn`).
  5. Materialize `bootstrap.json`.

  After the issuer returned a row, any later failure revokes that generation with
  reason `issuance-rollback`. SQLSTATE 23505 (unique violation) becomes
  `claims_mismatch`, as `CredentialAuthorizationError` does in
  `src/gobby/runner_init/servers.py::issue_grant_postgres`. Every other failure is
  `credential_issuance_failed`.
- **`issue_maintenance`** calls
  `<auth>.issue_maintenance_principal(execution, project, daemon_machine, expiry, password, NULL)`
  with the same heartbeat, DSN, bootstrap and rollback steps. Every failure is
  `credential_issuance_failed`; Python maps no SQLSTATE here.
- **`materialize_bootstrap`** ports `_materialize_bootstrap`:
  1. Use `<home>/runtime/managed-executions` and `<execution>/` under it, each a
     directory with mode 0700.
  2. A temporary `.bootstrap-*.json` opens in the execution directory with
     mode 0600.
  3. Write sorted compact JSON with no trailing newline:
     `{credential_generation, database_url, expires_at, managed_execution_id, role_name}`.
     `expires_at` is Python's `datetime.isoformat()` of the UTC expiry: microseconds
     only when nonzero, then `+00:00`. Format it with `time` 0.3, already locked
     through gdaemon.
  4. `fsync`; `bootstrap.json` is then renamed into place. On failure, the temporary
     file is deleted.
- **`revoke(execution, generation, reason)`** ports `revoke`:
  1. Loop on `<auth>.revoke_principal(execution, generation)`, polling every 50 ms
     until the result is non-negative or 5 s pass.
  2. An error, or a final negative result (`active_sessions_remaining`), deletes the
     bootstrap only when its `credential_generation` matches (`_delete_bootstrap`).
     It then writes `revocation-retry.json` (mode 0600; sorted compact
     `{credential_generation, failure_code, managed_execution_id, reason, recorded_at}`)
     for Python's `reconcile` to retry.
  3. Success deletes the matching bootstrap and the retry record.
- **`live_generation(execution)`** ports `get_live_binding_generation`. It selects
  `<auth>.managed_binding_states(daemon_machine)` rows for the execution with null
  `revocation_requested_at` and null `predecessor_drain_deadline`. Exactly one row
  gives its generation; otherwise `None`.

Python's in-memory `revoke_execution` sink has no native counterpart; 4.1's durable
read replaces it. A retry record written natively is picked up by Python's
`ManagedCredentialManager.reconcile`.

`tests/fixtures/managed_bootstrap_vector.json` holds Python's `bootstrap.json` bytes
and retry-record bytes for a fixed execution, generation, base DSN, expiry and
`recorded_at`, generated once with `uv run python -c` and committed. The expiries
cover both a whole second and a nonzero microsecond.

Research context:
- `_validate_expiry`, `issue`, `issue_maintenance`, `revoke`,
  `get_live_binding_generation`, `_scoped_dsn`, `_materialize_bootstrap`,
  `_delete_bootstrap`, `_write_retry_record` and `reconcile` are the Python source
  of truth, with `REVOCATION_DRAIN_TIMEOUT_SECONDS` and `REVOCATION_POLL_SECONDS`.
- `issue_grant_postgres` passes `now + 1 h` as the expiry and no project path.
  Through the handshake, a maintenance principal never carries an overlay
  (`HandshakeService.issue_for_agent` sets none).
- `gobby_daemon_runtime` holds EXECUTE on the issuer functions (baseline grants), as
  3.4's interactive issuance relies on.
- Planned checks:
  - `cargo nextest run -p gobby-runtime --test managed`;
  - `DATABASE_URL=… GOBBY_TEST_PROTECT=1 uv run pytest tests/storage/test_managed_bootstrap_vector.py -v`.

Consumers unchanged:
- `src/gobby/cli/install_setup_gclient.py` — no-edit-reason: reads only the `gclient` pin, which no leaf of this plan bumps.
- `src/gobby/cli/install_setup_gcode.py` — no-edit-reason: reads the `gcode` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_ghook.py` — no-edit-reason: reads the `ghook` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_gterm.py` — no-edit-reason: reads only the `gterm` pin, which no leaf of this plan bumps.
- `src/gobby/cli/install_setup_versions.py` — no-edit-reason: looks a pin up by the binary name it is given.
- `src/gobby/code_index/gcode_gateway.py` — no-edit-reason: derives `MIN_GCODE_GRAPH_VERSION` from the `gcode` pin at import.
- `src/gobby/hooks/runtime_compat.py` — no-edit-reason: derives `MINIMUM_GHOOK_VERSION_FOR_SUPPORTED_SCHEMA` from the `ghook` pin at import.
- `src/gobby/install/bin_freshness_models.py` — no-edit-reason: iterates every pin, so the bumped values flow through.
- `src/gobby/install/distribution.py` — no-edit-reason: looks a pin up by binary name.
- `tests/cli/installers/test_grok_installer.py` — no-edit-reason: reads the `ghook` pin instead of a version literal.
- `tests/cli/test_install_ghook.py` — no-edit-reason: builds its fixtures from the `ghook` pin instead of a version literal.
- `tests/cli/test_install_setup.py` — no-edit-reason: reads the `gcode` pin instead of a version literal.
- `tests/cli/test_install_setup_gclient.py` — no-edit-reason: reads the `gclient` pin instead of a version literal.
- `tests/cli/test_install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin instead of a version literal.
- `tests/cli/test_install_setup_gterm.py` — no-edit-reason: reads the `gterm` and `gclient` pins instead of version literals.
- `tests/code_index/test_gcode_gateway.py` — no-edit-reason: reads the `gcode` pin instead of a version literal.
- `tests/hooks/test_runtime_compat.py` — no-edit-reason: compares the derived floor to the `ghook` pin instead of a version literal.
- `tests/install/test_distribution.py` — no-edit-reason: reads pins by binary name; its `0.1.0` literal is a fake `gcode` output, not a pin.
- `tests/install/test_version_pins.py` — no-edit-reason: compares each pin to its crate manifest, which is this leaf's bump check.

**Acceptance:**

- 4.4.1 - `issue` and `issue_maintenance` each leave a live binding whose scoped DSN logs in as the issued role, with a 0600 `bootstrap.json` in a 0700 execution directory. test: `crates/gruntime/tests/managed.rs::issuance_materializes_a_private_bootstrap`.
- 4.4.2 - Native `bootstrap.json` and retry-record bytes equal the committed Python bytes. test: `crates/gruntime/tests/managed.rs::bootstrap_bytes_match_python`.
- 4.4.3 - A duplicate agent binding returns `claims_mismatch`, and a failure after the issuer returned a row revokes that generation. test: `crates/gruntime/tests/managed.rs::issuance_failures_roll_back_and_map_codes`.
- 4.4.4 - A revocation blocked by active sessions writes the retry record and deletes only a bootstrap of the same generation. test: `crates/gruntime/tests/managed.rs::blocked_revocation_records_a_retry`.
- 4.4.5 - The committed vector still matches Python's writers. test: `tests/storage/test_managed_bootstrap_vector.py::test_vector_matches_python_bootstrap_writers`.
- 4.4.6 - `gdaemon` is one patch version above the executor's starting tip in its `Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`, so this leaf's cutover passes the release-identity preflight. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 4.5 Managed rotation and the `grant.json` rewrite [category: code] (depends: 4.4)
`kind: deliverable`

Targets:
- `crates/gruntime/src/managed/mod.rs`
- `crates/gruntime/src/managed/rotation.rs`
- `crates/gruntime/src/managed/launch_grant.rs`
- `crates/gruntime/tests/rotation.rs`
- `crates/gruntime/tests/fixtures/managed_grant_rewrite_vector.json`
- `tests/runtime_grants/test_managed_grant_rewrite_vector.py`
- `crates/gdaemon/Cargo.toml`
- `src/gobby/install/version_pins.py::MANAGED_BIN_VERSION_PINS`
- `Cargo.lock`

Release ownership (Constraints): gruntime is in gdaemon's release closure, so this
leaf bumps `gdaemon` by 0.0.1 from the executor's starting tip.

Orchestrator #15977 ruled (2026-10-14) that rotation and the rewrite stay one step,
with the predecessor restored on failure. `rotation.rs` ports, from
`ManagedCredentialManager`:

- **`reuse_current_during_drain`.**
  1. A draining predecessor row (`predecessor_drain_deadline` after now) must
     exist, otherwise `None`.
  2. Exactly two bindings must exist, the owner must be live
     (`_rotation_owner_is_live`), and exactly one binding must be current.
  3. The current binding's owner kind, session, project, agent run (the execution
     for agent_run, otherwise null), login capability and future expiry must match
     the request; a mismatch is `claims_mismatch`.
  4. Read `bootstrap.json` with `O_RDONLY | O_NOFOLLOW | O_NONBLOCK`. It must be a
     regular file with mode 0600 owned by this uid.
  5. Its execution, generation, role and parsed `expires_at` must equal the
     binding, and its `database_url` must parse to the same key/value set as the
     scoped DSN.
  6. Re-read the bindings: they must be unchanged and the owner still live.

  The result is the existing bootstrap. There is no heartbeat.
- **`owner_is_live`** ports `_rotation_owner_is_live`:
  - at most two bindings, exactly one current;
  - a predecessor with `requested < deadline <= requested + 5 min`, generation one
    below current, and the same owner kind, agent run, session, project, issuing
    machine and overlay;
  - then by owner kind: maintenance needs login capability; agent_run needs an
    active agent run; tool_chat needs nothing more; and the session must not be
    expired or deleted on this machine.
- **`rotate(execution, expiry)`**:
  1. Heartbeat.
  2. The predecessor is `live_generation`; none is `claims_mismatch`.
  3. Port `_rotate_if_generation`. The owner must be live. When the execution's
     `grant.json` exists, capture the predecessor `bootstrap.json` bytes. Call
     `<auth>.rotate_principal_if_generation(execution, predecessor, expiry, password)`;
     no row means the binding race was lost, which is `credential_issuance_failed`.
     Materialize the successor bootstrap.
  4. When a predecessor was captured, rewrite `grant.json`.
  5. If any step after the successor exists fails, roll back in this order:
     restore the predecessor bootstrap bytes (`_restore_bootstrap`, temporary prefix
     `.bootstrap-rollback-`), revoke the successor with reason `rotation-rollback`,
     then call
     `<auth>.cancel_principal_rotation(execution, predecessor, successor)`. The
     result is `credential_issuance_failed`.

`launch_grant.rs` ports `src/gobby/runtime_grants/launch.py::rewrite_managed_grant_file`
and its writer, called with the fence from 3.2:

1. Open `<execution dir>/grant.json`. Any group or other permission bit
   (`st_mode & 0o077`) fails. Parse it with 3.2's structural decoder, keeping
   whether `managed_api_token` was present.
2. The grant's `execution_id` must equal the execution.
3. Replace `deployment {token, fencing_epoch}`, `capabilities.postgres`
   (`PostgresDirect {dsn, role_name, credential_generation, valid_until = expiry seconds}`),
   `issued_at` (the successor's issue time in whole seconds) and `expires_at`
   (expiry seconds). Leave every other field unchanged.
4. When the old file carried `managed_api_token`, mint a successor capability with
   4.2's `mint` and key, with `iat = now`:
   - agent_run: owner `agent_run_id`, TTL 86460 (86400 plus the 60 s grace);
   - tool_chat: owner `managed_execution_id`, kind `tool_chat`, TTL 86400;
   - maintenance: owner `managed_execution_id`, kind `maintenance`, TTL 86400, with
     `session_id` set to the execution id.

   This ports `_issue_grant_capability` with `AGENT_TOKEN_MAX_TTL_SECONDS`. No key is
   an error, which triggers the rollback.
5. `sign_grant` with the fence secret, then write it the way `write_grant_file`
   does:
   - The directory gets mode 0700, and a temporary `.grant-*.json` gets
     mode 0600.
   - Write the canonical sorted compact JSON of the signed grant, with
     `managed_api_token` inserted when minted, followed by `"\n"`.
   - `fsync`; `grant.json` is then renamed into place.

   gcore's `grant::cache::write_grant_file` is not reused. It drops
   `managed_api_token` and deletes the settings cache, which the launch grant must
   keep.

`tests/fixtures/managed_grant_rewrite_vector.json` holds an input `grant.json` for
each kind and Python's rewritten bytes for fixed inputs: execution, DSN, role,
generation, issue time, expiry, fence, signing secret, `api_key`, and a frozen clock.
The Python test regenerates them through `rewrite_managed_grant_file` under the
same frozen clock and asserts equality, so drift in either language fails.

Research context:
- `rotate`, `_rotate_if_generation`, `reuse_current_during_drain`,
  `_rotation_owner_is_live` and `_restore_bootstrap` are the Python source of
  truth. `_bind_runtime_grants._rewrite_launch_grant` passes the lease token,
  epoch and secret plus the credential's issue time and expiry.
- Python captures the predecessor only when `grant.json` exists. A bare
  `bootstrap.json` execution rotates without a rewrite.
- `rotate_principal_if_generation` and `cancel_principal_rotation` take the
  per-execution advisory transaction lock, so a native rotation and Python's
  `rotate_due` cannot both succeed for one generation.
- Planned checks:
  - `cargo nextest run -p gobby-runtime --test rotation`;
  - `DATABASE_URL=… GOBBY_TEST_PROTECT=1 uv run pytest tests/runtime_grants/test_managed_grant_rewrite_vector.py -v`.

Consumers unchanged:
- `src/gobby/cli/install_setup_gclient.py` — no-edit-reason: reads only the `gclient` pin, which no leaf of this plan bumps.
- `src/gobby/cli/install_setup_gcode.py` — no-edit-reason: reads the `gcode` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_ghook.py` — no-edit-reason: reads the `ghook` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_gterm.py` — no-edit-reason: reads only the `gterm` pin, which no leaf of this plan bumps.
- `src/gobby/cli/install_setup_versions.py` — no-edit-reason: looks a pin up by the binary name it is given.
- `src/gobby/code_index/gcode_gateway.py` — no-edit-reason: derives `MIN_GCODE_GRAPH_VERSION` from the `gcode` pin at import.
- `src/gobby/hooks/runtime_compat.py` — no-edit-reason: derives `MINIMUM_GHOOK_VERSION_FOR_SUPPORTED_SCHEMA` from the `ghook` pin at import.
- `src/gobby/install/bin_freshness_models.py` — no-edit-reason: iterates every pin, so the bumped values flow through.
- `src/gobby/install/distribution.py` — no-edit-reason: looks a pin up by binary name.
- `tests/cli/installers/test_grok_installer.py` — no-edit-reason: reads the `ghook` pin instead of a version literal.
- `tests/cli/test_install_ghook.py` — no-edit-reason: builds its fixtures from the `ghook` pin instead of a version literal.
- `tests/cli/test_install_setup.py` — no-edit-reason: reads the `gcode` pin instead of a version literal.
- `tests/cli/test_install_setup_gclient.py` — no-edit-reason: reads the `gclient` pin instead of a version literal.
- `tests/cli/test_install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin instead of a version literal.
- `tests/cli/test_install_setup_gterm.py` — no-edit-reason: reads the `gterm` and `gclient` pins instead of version literals.
- `tests/code_index/test_gcode_gateway.py` — no-edit-reason: reads the `gcode` pin instead of a version literal.
- `tests/hooks/test_runtime_compat.py` — no-edit-reason: compares the derived floor to the `ghook` pin instead of a version literal.
- `tests/install/test_distribution.py` — no-edit-reason: reads pins by binary name; its `0.1.0` literal is a fake `gcode` output, not a pin.
- `tests/install/test_version_pins.py` — no-edit-reason: compares each pin to its crate manifest, which is this leaf's bump check.

**Acceptance:**

- 4.5.1 - Rotation issues the successor and rewrites `grant.json` with the successor credential and a fresh capability, and the predecessor drains. test: `crates/gruntime/tests/rotation.rs::rotation_rewrites_the_launch_grant`.
- 4.5.2 - A failed native rewrite (a `grant.json` with mode 0644) restores the predecessor `bootstrap.json` bytes, revokes the successor and cancels the rotation, and `grant.json` never names a revoked predecessor. test: `crates/gruntime/tests/rotation.rs::failed_rewrite_restores_the_predecessor`.
- 4.5.3 - The native rewrite equals Python's rewritten bytes for agent_run, tool_chat and maintenance. test: `crates/gruntime/tests/rotation.rs::rewrite_matches_python_bytes`.
- 4.5.4 - The committed rewrite vector still matches Python's `rewrite_managed_grant_file`. test: `tests/runtime_grants/test_managed_grant_rewrite_vector.py::test_vector_matches_python_rewrite`.
- 4.5.5 - During a drain, a request matching the current binding reuses its bootstrap, an identity mismatch is `claims_mismatch`, and a bootstrap that is not a private regular file is refused. test: `crates/gruntime/tests/rotation.rs::drain_reuse_follows_python`.
- 4.5.6 - A lost generation race yields `credential_issuance_failed` and leaves the winner's binding and bootstrap untouched. test: `crates/gruntime/tests/rotation.rs::lost_generation_race_changes_nothing`.
- 4.5.7 - `gdaemon` is one patch version above the executor's starting tip in its `Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`, so this leaf's cutover passes the release-identity preflight. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 4.6 Native managed handshake [category: code] (depends: 4.5)
`kind: deliverable`

Targets:
- `crates/gruntime/src/lib.rs`
- `crates/gruntime/src/handshake.rs`
- `crates/gruntime/src/verify.rs`
- `crates/gruntime/tests/managed_handshake.rs`
- `crates/gdaemon/Cargo.toml`
- `src/gobby/install/version_pins.py::MANAGED_BIN_VERSION_PINS`
- `Cargo.lock`

Release ownership (Constraints): gruntime is in gdaemon's release closure, so this
leaf bumps `gdaemon` by 0.0.1 from the executor's starting tip.

Row: `POST /api/runtime/handshake` for `Agent`, beside 3.4's `Operator` row. The
handler first runs 4.2's `verify`, then mirrors
`src/gobby/runtime_grants/handshake.py::HandshakeService.issue_for_agent` and
`issue_grant_postgres`'s managed branches:

1. Parse the body as 3.4 does; an invalid body returns 422.
2. The claims' `machine_id` and `project_id` must equal the body's, otherwise 403
   `claims_mismatch`. There is no project admission and no overlay for agents.
3. The kind is `claims.kind`, else `agent_run` when `agent_run_id` is set, else
   `tool_chat`. Any kind outside agent_run, tool_chat and maintenance is
   `claims_mismatch`. The principal's execution is the owner claim; its session is
   the claim's, except null for maintenance.
4. The expiry is now + 1 h.
   - Maintenance calls `issue_maintenance` (4.4).
   - agent_run and tool_chat with no live generation call `issue`.
   - Otherwise try `reuse_current_during_drain`, and `rotate` when it returns
     `None` (4.5).

   The DSN is the scoped DSN, or the reused bootstrap's `database_url`.
5. Issue as 3.4's step 8 does, with this principal. The TTL is
   `min(3600, max(1, token exp − now), max(1, valid_until − now))`
   (`HandshakeService._issue`). Capabilities come from the snapshot 3.1 serves.
6. Respond as 3.4 does, with `no-store`. Failure codes map as 4.4 and 4.5 define.

`verify.rs` extends `check_proxied` to a claimed agent handshake whose proxied
status is 200. It returns `Verified` only when all of these hold:

- `verify_static` passes against the current fence;
- the top-level token and epoch equal the grant's;
- the principal equals the one step 3 derives from the token's verified claims;
- `config_revision`, and every capability except `postgres`, equal a fresh native
  computation;
- `expires_at <= issued_at + 3600`, `expires_at <= postgres.valid_until` and
  `expires_at <= token exp`;
- `managed_principal_revoked(execution, generation)` (4.1) is false.

Otherwise it returns `Mismatch(<pointer>)`. A 200 for a token that native
classification rejects returns `Mismatch("/authorization")`, which exposes a
signing-key divergence between the two implementations. Any other status returns
`Skipped`.

Research context:
- Python reference: `src/gobby/servers/routes/runtime_handshake.py::handshake`
  (agent branch via `verified_agent_claims`), `HandshakeService.issue_for_agent`
  and `_issue`, and the managed branches of
  `src/gobby/runner_init/servers.py::issue_grant_postgres`.
- Compare mode proxies this POST (D3), so native issuance and rotation get no soak
  evidence. 6.3's e2e exercises them on the native default.
- Planned check: `cargo nextest run -p gobby-runtime --test managed_handshake`.

Consumers unchanged:
- `src/gobby/cli/install_setup_gclient.py` — no-edit-reason: reads only the `gclient` pin, which no leaf of this plan bumps.
- `src/gobby/cli/install_setup_gcode.py` — no-edit-reason: reads the `gcode` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_ghook.py` — no-edit-reason: reads the `ghook` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_gterm.py` — no-edit-reason: reads only the `gterm` pin, which no leaf of this plan bumps.
- `src/gobby/cli/install_setup_versions.py` — no-edit-reason: looks a pin up by the binary name it is given.
- `src/gobby/code_index/gcode_gateway.py` — no-edit-reason: derives `MIN_GCODE_GRAPH_VERSION` from the `gcode` pin at import.
- `src/gobby/hooks/runtime_compat.py` — no-edit-reason: derives `MINIMUM_GHOOK_VERSION_FOR_SUPPORTED_SCHEMA` from the `ghook` pin at import.
- `src/gobby/install/bin_freshness_models.py` — no-edit-reason: iterates every pin, so the bumped values flow through.
- `src/gobby/install/distribution.py` — no-edit-reason: looks a pin up by binary name.
- `tests/cli/installers/test_grok_installer.py` — no-edit-reason: reads the `ghook` pin instead of a version literal.
- `tests/cli/test_install_ghook.py` — no-edit-reason: builds its fixtures from the `ghook` pin instead of a version literal.
- `tests/cli/test_install_setup.py` — no-edit-reason: reads the `gcode` pin instead of a version literal.
- `tests/cli/test_install_setup_gclient.py` — no-edit-reason: reads the `gclient` pin instead of a version literal.
- `tests/cli/test_install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin instead of a version literal.
- `tests/cli/test_install_setup_gterm.py` — no-edit-reason: reads the `gterm` and `gclient` pins instead of version literals.
- `tests/code_index/test_gcode_gateway.py` — no-edit-reason: reads the `gcode` pin instead of a version literal.
- `tests/hooks/test_runtime_compat.py` — no-edit-reason: compares the derived floor to the `ghook` pin instead of a version literal.
- `tests/install/test_distribution.py` — no-edit-reason: reads pins by binary name; its `0.1.0` literal is a fake `gcode` output, not a pin.
- `tests/install/test_version_pins.py` — no-edit-reason: compares each pin to its crate manifest, which is this leaf's bump check.

**Acceptance:**

- 4.6.1 - An agent_run, a tool_chat and a maintenance handshake each return a signed grant whose principal and TTL equal Python's for the same token and database state. test: `crates/gruntime/tests/managed_handshake.rs::agent_handshakes_issue_managed_grants`.
- 4.6.2 - A second handshake on a live binding rotates (or reuses during a drain) and rewrites `grant.json`. test: `crates/gruntime/tests/managed_handshake.rs::repeat_handshake_rotates_or_reuses`.
- 4.6.3 - A body that disagrees with the claims, or a non-managed kind, returns 403 `claims_mismatch`. test: `crates/gruntime/tests/managed_handshake.rs::claims_must_match_the_body`.
- 4.6.4 - In compare mode a Python-issued managed grant records `verified`, and a grant naming a revoked generation records `mismatch`. test: `crates/gruntime/tests/managed_handshake.rs::proxied_managed_grants_are_checked`.
- 4.6.5 - `gdaemon` is one patch version above the executor's starting tip in its `Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`, so this leaf's cutover passes the release-identity preflight. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 4.7 Managed grant presentation [category: code] (depends: 4.6)
`kind: deliverable`

Targets:
- `crates/gruntime/src/lib.rs`
- `crates/gruntime/src/grant.rs`
- `crates/gruntime/tests/managed_presentation.rs`
- `crates/gdaemon/Cargo.toml`
- `src/gobby/install/version_pins.py::MANAGED_BIN_VERSION_PINS`
- `Cargo.lock`

Release ownership (Constraints): gruntime is in gdaemon's release closure, so this
leaf bumps `gdaemon` by 0.0.1 from the executor's starting tip.

Row: `GET /api/runtime/config` for `Agent`. The handler runs 4.2's `verify`, then
3.2's presentation. The gate on both this row and 3.2's `Operator` row widens from
`interactive` to every principal kind: any header `decode_grant_header` accepts is
claimed.

Revocation becomes `grant::revoked(...)`:

- `interactive` keeps `interactive_revoked`;
- `agent_run`, `tool_chat` and `maintenance` call
  `<auth>.managed_principal_revoked(execution, generation)` (4.1);
- a grant with no integer `credential_generation` is not revoked, as in Python.

Python's presentation route does not compare the bearer with the grant: the runtime
routes are not in `_GRANT_ROUTES`, so `bearer_matches_grant` never runs there. The
native route matches that, and an agent may present any grant that verifies.

Research context:
- `src/gobby/servers/routes/runtime_config.py::create_runtime_config_router`
  authenticates, decodes and presents. 4.2 reproduces the agent bearer checks the
  middleware runs first.
- Planned check: `cargo nextest run -p gobby-runtime --test managed_presentation`.

Consumers unchanged:
- `src/gobby/cli/install_setup_gclient.py` — no-edit-reason: reads only the `gclient` pin, which no leaf of this plan bumps.
- `src/gobby/cli/install_setup_gcode.py` — no-edit-reason: reads the `gcode` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_ghook.py` — no-edit-reason: reads the `ghook` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_gterm.py` — no-edit-reason: reads only the `gterm` pin, which no leaf of this plan bumps.
- `src/gobby/cli/install_setup_versions.py` — no-edit-reason: looks a pin up by the binary name it is given.
- `src/gobby/code_index/gcode_gateway.py` — no-edit-reason: derives `MIN_GCODE_GRAPH_VERSION` from the `gcode` pin at import.
- `src/gobby/hooks/runtime_compat.py` — no-edit-reason: derives `MINIMUM_GHOOK_VERSION_FOR_SUPPORTED_SCHEMA` from the `ghook` pin at import.
- `src/gobby/install/bin_freshness_models.py` — no-edit-reason: iterates every pin, so the bumped values flow through.
- `src/gobby/install/distribution.py` — no-edit-reason: looks a pin up by binary name.
- `tests/cli/installers/test_grok_installer.py` — no-edit-reason: reads the `ghook` pin instead of a version literal.
- `tests/cli/test_install_ghook.py` — no-edit-reason: builds its fixtures from the `ghook` pin instead of a version literal.
- `tests/cli/test_install_setup.py` — no-edit-reason: reads the `gcode` pin instead of a version literal.
- `tests/cli/test_install_setup_gclient.py` — no-edit-reason: reads the `gclient` pin instead of a version literal.
- `tests/cli/test_install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin instead of a version literal.
- `tests/cli/test_install_setup_gterm.py` — no-edit-reason: reads the `gterm` and `gclient` pins instead of version literals.
- `tests/code_index/test_gcode_gateway.py` — no-edit-reason: reads the `gcode` pin instead of a version literal.
- `tests/hooks/test_runtime_compat.py` — no-edit-reason: compares the derived floor to the `ghook` pin instead of a version literal.
- `tests/install/test_distribution.py` — no-edit-reason: reads pins by binary name; its `0.1.0` literal is a fake `gcode` output, not a pin.
- `tests/install/test_version_pins.py` — no-edit-reason: compares each pin to its crate manifest, which is this leaf's bump check.

**Acceptance:**

- 4.7.1 - An agent presenting its managed grant receives `{config_revision, settings}`, and after the generation is revoked it receives 403 `revoked`. test: `crates/gruntime/tests/managed_presentation.rs::managed_grants_present_until_revoked`.
- 4.7.2 - A finished run's bearer returns 401 `run_inactive` before presentation. test: `crates/gruntime/tests/managed_presentation.rs::inactive_runs_are_rejected_before_presentation`.
- 4.7.3 - An operator presenting a managed grant is claimed and presented. test: `crates/gruntime/tests/managed_presentation.rs::operator_rows_claim_every_principal_kind`.
- 4.7.4 - `gdaemon` is one patch version above the executor's starting tip in its `Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`, so this leaf's cutover passes the release-identity preflight. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

## P5: Config routes
`kind: framing`

P5 ports the eighteen `/api/config` routes besides `GET /api/config/effective`
(D4):

- reads: the public schema (5.1), secret listing (5.2), the prompt list and detail
  (5.4), the YAML template (5.5), and the values, UI settings and global tool
  approvals (5.6), which serve 2.2's published row;
- writes: secret deletion (5.2), prompt save and deletion (5.4), the values PATCH
  (5.9), secret save (5.10), template save and import (5.12) and endpoint
  activation (5.14);
- POSTs that write nothing: the YAML export (5.5) and the validation-detection
  preview (5.16).

Each route leaf adds `NATIVE_ROUTES` rows for `Operator` only. Reads compare under
2.1's GET policy. Writes and the two read-only POSTs are proxied once in compare
mode, and `check_proxied` checks the result against the persisted state or a native
recomputation. These checks are the write-path verifiers the soak counts. A native
write commits under Python's lock and revision rules and waits for Python's
activation to publish its revision (D1, Write wait).

Five leaves add no route and give the route leaves after them a shared port: 5.7
and 5.8 the validation Python applies before a write, 5.11 YAML 1.2 reading,
5.13 the endpoint probe chain and 5.15 the validation-command classifier.

5.3 is a Python-only refactor that shares no Target with any earlier leaf, so it has
no dependency. 5.4 builds on it and on 5.2.

### 5.1 Native `GET /api/config/schema` [category: code] (depends: 4.7)
`kind: deliverable`

Targets:
- `crates/gcore/src/config/runtime_contract.rs::*` — scope-reason: adds `public_config_schema()`, built once from the raw contract JSON beside the private `contract()`; the typed specs are unchanged
- `crates/gcore/src/config/mod.rs::*` — scope-reason: the `pub use runtime_contract::{…}` list adds `public_config_schema`; the other re-exports and their consumers stay unchanged
- `crates/gruntime/src/lib.rs`
- `crates/gruntime/src/config.rs`
- `crates/gruntime/tests/config.rs`
- `tests/config/test_runtime_config_contract.py::*` — scope-reason: adds `test_public_schema_derives_from_contract`; the existing tests are unchanged
- `crates/gdaemon/Cargo.toml`
- `crates/gcode/Cargo.toml`
- `crates/ghook/Cargo.toml`
- `src/gobby/install/version_pins.py::MANAGED_BIN_VERSION_PINS`
- `Cargo.lock`

Release ownership (Constraints): this leaf changes gcore, so it bumps `gdaemon`,
`gcode` and `ghook` by 0.0.1 from the executor's starting tip.

`public_config_schema() -> &'static serde_json::Value` parses `CONTRACT_JSON` as an
untyped value once (the typed `RuntimeConfigContract` drops the metadata fields)
and builds what `ConfigRegistry.json_schema(ConfigVisibility.PUBLIC)` returns:

- `{"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object", "additionalProperties": false, "properties": P}`;
- `P` maps each `exactKeys` entry whose `visibility` is `public` from its `key` to the
  entry without the `key` member, and each `patterns` entry whose `visibility` is
  `public` from its `pattern` to the whole entry.

Row: `GET /api/config/schema` for `Operator`. The handler in `config.rs` returns 200
`application/json` with that value and no `Cache-Control`, as Python's
`JSONResponse` does. It has no failure path; nor does the Python route.

Research context:
- Python: `src/gobby/servers/routes/configuration_values.py::register_value_routes`
  calls `ConfigValuesService.schema`, which returns
  `CONFIG_REGISTRY.json_schema(ConfigVisibility.PUBLIC)`. `_schema_metadata` emits
  exactly the fields a contract entry carries.
- Verified at plan time: the projection above, computed from the checked-in
  contract, equals `json_schema(PUBLIC)` (458 properties: 447 public exact keys and
  11 patterns). The contract holds no restricted key (its visibilities are `machine`
  and `public`), and `test_checked_in_contract_matches_registry` keeps it fresh.
- Rejected: a separately generated schema carrier. The existing contract already
  carries every field.
- Planned checks:
  - `cargo nextest run -p gobby-runtime --test config -E 'test(schema)'`;
  - `DATABASE_URL=… GOBBY_TEST_PROTECT=1 uv run pytest tests/config/test_runtime_config_contract.py -v`.

Consumers unchanged:
- `src/gobby/cli/install_setup_gclient.py` — no-edit-reason: reads only the `gclient` pin, which no leaf of this plan bumps.
- `src/gobby/cli/install_setup_gcode.py` — no-edit-reason: reads the `gcode` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_ghook.py` — no-edit-reason: reads the `ghook` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_gterm.py` — no-edit-reason: reads only the `gterm` pin, which no leaf of this plan bumps.
- `src/gobby/cli/install_setup_versions.py` — no-edit-reason: looks a pin up by the binary name it is given.
- `src/gobby/code_index/gcode_gateway.py` — no-edit-reason: derives `MIN_GCODE_GRAPH_VERSION` from the `gcode` pin at import.
- `src/gobby/hooks/runtime_compat.py` — no-edit-reason: derives `MINIMUM_GHOOK_VERSION_FOR_SUPPORTED_SCHEMA` from the `ghook` pin at import.
- `src/gobby/install/bin_freshness_models.py` — no-edit-reason: iterates every pin, so the bumped values flow through.
- `src/gobby/install/distribution.py` — no-edit-reason: looks a pin up by binary name.
- `tests/cli/installers/test_grok_installer.py` — no-edit-reason: reads the `ghook` pin instead of a version literal.
- `tests/cli/test_install_ghook.py` — no-edit-reason: builds its fixtures from the `ghook` pin instead of a version literal.
- `tests/cli/test_install_setup.py` — no-edit-reason: reads the `gcode` pin instead of a version literal.
- `tests/cli/test_install_setup_gclient.py` — no-edit-reason: reads the `gclient` pin instead of a version literal.
- `tests/cli/test_install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin instead of a version literal.
- `tests/cli/test_install_setup_gterm.py` — no-edit-reason: reads the `gterm` and `gclient` pins instead of version literals.
- `tests/code_index/test_gcode_gateway.py` — no-edit-reason: reads the `gcode` pin instead of a version literal.
- `tests/hooks/test_runtime_compat.py` — no-edit-reason: compares the derived floor to the `ghook` pin instead of a version literal.
- `tests/install/test_distribution.py` — no-edit-reason: reads pins by binary name; its `0.1.0` literal is a fake `gcode` output, not a pin.
- `tests/install/test_version_pins.py` — no-edit-reason: compares each pin to its crate manifest, which is this leaf's bump check.

**Acceptance:**

- 5.1.1 - `GET /api/config/schema` returns `public_config_schema()`: every public exact key and public pattern, no `key` member, inside the draft 2020-12 wrapper. test: `crates/gruntime/tests/config.rs::schema_serves_the_public_contract_projection`.
- 5.1.2 - The same projection, computed in Python from the checked-in contract, equals `CONFIG_REGISTRY.json_schema(ConfigVisibility.PUBLIC)`. test: `tests/config/test_runtime_config_contract.py::test_public_schema_derives_from_contract`.
- 5.1.3 - `gdaemon`, `gcode` and `ghook` are each one patch version above the executor's starting tip in their `Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`, so this leaf's cutover passes the release-identity preflight. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 5.2 Native secret listing and deletion [category: code] (depends: 5.1)
`kind: deliverable`

Targets:
- `crates/gruntime/src/lib.rs`
- `crates/gruntime/src/secret_routes.rs`
- `crates/gruntime/src/verify.rs`
- `crates/gruntime/tests/secret_routes.rs`
- `crates/gdaemon/Cargo.toml`
- `src/gobby/install/version_pins.py::MANAGED_BIN_VERSION_PINS`
- `Cargo.lock`

Release ownership (Constraints): gruntime is in gdaemon's release closure, so this
leaf bumps `gdaemon` by 0.0.1 from the executor's starting tip.

Rows: `GET /api/config/secrets` and `DELETE /api/config/secrets/{name}`, both for
`Operator`. The global scope is `GLOBAL_PROJECT_ID`
(`00000000-0000-0000-0000-000000000002`).

**Listing** mirrors `configuration_secrets.py::list_secrets` and `SecretStore.list`:

1. Run `SELECT id, name, category, description, created_at, updated_at, project_id FROM secrets WHERE project_id = $1 ORDER BY name`
   with the global scope. The order is the database's, as in Python.
2. Each row becomes `{id, name, category, description, created_at, updated_at, project_id, scope}`.
   Timestamps use the isoformat 4.4 defines for `expires_at` (UTC, microseconds only
   when nonzero, then `+00:00`). `project_id` is the column's string form, and
   `scope` is `global` when it equals the global scope, else `project`.
3. Respond `{"secrets": [...], "categories": ["general", "integration", "llm", "mcp_server", "memory"]}`.
   A database error returns 500 `{"detail": "Internal server error"}`.

**Deletion** mirrors `delete_secret` and `ConfigStore.delete_named_secret`. The
normalized name is the path name stripped and lowercased (`normalize_secret_name`).

1. `existed`: a global row with the normalized name exists.
2. In one transaction:
   1. `SELECT pg_advisory_xact_lock(hashtext('gobby.storage.config_mutations.EmbeddingSwitchJournalMutation:EmbeddingSwitchJournalMutation()'))`.
      The key is the one `advisory_lock_keys` derives for
      `EmbeddingSwitchJournalMutation()`, so native and Python deletions and config
      mutations serialize on one lock.
   2. When the normalized name is `embeddings_api_key`, read
      `SELECT value FROM config_store WHERE key = 'ai.embeddings.switch_run' FOR UPDATE`.
      No row, a JSON `null`, or a JSON string that itself decodes to `null` means no
      switch is active. Any other value (an active run id or a malformed journal)
      blocks the deletion, which returns 500 `{"detail": "Internal server error"}`,
      as Python's generic handler does for `EmbeddingConfigMutationBlocked`.
   3. Collect references:
      - every `config_store.value`, scanned as raw text;
      - every `mcp_servers.env` and `.headers`, decoded from `jsonb` and walked
        through object values and array elements (object keys are not scanned),
        scanning each string.

      A match of `\$secret:([A-Za-z_][A-Za-z0-9_]*)` (`SECRET_REF_PATTERN`) is
      normalized and collected. A referenced name is not deleted.
   4. Otherwise `DELETE FROM secrets WHERE name = $1 AND project_id = $2` with the
      global scope; the deletion happened when a row was removed.
3. Respond:
   - deleted: 200 `{"ok": true}`;
   - not deleted and `existed`: 409 `{"detail": "Secret '<name>' is still referenced"}`;
   - otherwise: 404 `{"detail": "Secret '<name>' not found"}`;
   - any error: 500 `{"detail": "Internal server error"}`.

   `<name>` is the decoded path segment as received, not normalized.

`verify.rs` routes a claimed proxied `DELETE /api/config/secrets/{name}` to
`secret_routes::check_deleted`, which reads the state after Python answered:

- 200 is `Verified` when no global row has the normalized name, else
  `Mismatch("/ok")`;
- 409 is `Verified` when the global row exists and the reference scan still finds
  the name, else `Mismatch("/detail")`;
- 404 is `Verified` when no global row exists, else `Mismatch("/detail")`;
- any other status is `Skipped`.

A secret or reference written between Python's commit and the check can produce a
false mismatch. E1 counts every mismatch, so the soak reports it instead of hiding
it.

Research context:
- Python: `src/gobby/servers/routes/configuration_secrets.py::register_secret_routes`,
  `src/gobby/storage/secrets.py::SecretStore.list`, `.exists`, `._lookup_secret_row`,
  `.delete` and `.find_persisted_secret_references`, plus
  `src/gobby/storage/config_store.py::ConfigStore.delete_named_secret`,
  `._assert_embedding_mutation_allowed`, `._active_switch_run_id` and
  `_journal_run_id`.
- `config_store.value` is `text`, and `mcp_servers.env` and `.headers` are `jsonb`
  (`crates/gcore/assets/schema/baseline.sql`). psycopg returns the `jsonb` columns as
  Python objects, so `find_secret_references` walks them structurally.
- `require_mutation_auth` admits every authenticated request, and a claimed
  `Operator` request is authenticated by construction.
- Planned check: `cargo nextest run -p gobby-runtime --test secret_routes`.

Consumers unchanged:
- `src/gobby/cli/install_setup_gclient.py` — no-edit-reason: reads only the `gclient` pin, which no leaf of this plan bumps.
- `src/gobby/cli/install_setup_gcode.py` — no-edit-reason: reads the `gcode` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_ghook.py` — no-edit-reason: reads the `ghook` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_gterm.py` — no-edit-reason: reads only the `gterm` pin, which no leaf of this plan bumps.
- `src/gobby/cli/install_setup_versions.py` — no-edit-reason: looks a pin up by the binary name it is given.
- `src/gobby/code_index/gcode_gateway.py` — no-edit-reason: derives `MIN_GCODE_GRAPH_VERSION` from the `gcode` pin at import.
- `src/gobby/hooks/runtime_compat.py` — no-edit-reason: derives `MINIMUM_GHOOK_VERSION_FOR_SUPPORTED_SCHEMA` from the `ghook` pin at import.
- `src/gobby/install/bin_freshness_models.py` — no-edit-reason: iterates every pin, so the bumped values flow through.
- `src/gobby/install/distribution.py` — no-edit-reason: looks a pin up by binary name.
- `tests/cli/installers/test_grok_installer.py` — no-edit-reason: reads the `ghook` pin instead of a version literal.
- `tests/cli/test_install_ghook.py` — no-edit-reason: builds its fixtures from the `ghook` pin instead of a version literal.
- `tests/cli/test_install_setup.py` — no-edit-reason: reads the `gcode` pin instead of a version literal.
- `tests/cli/test_install_setup_gclient.py` — no-edit-reason: reads the `gclient` pin instead of a version literal.
- `tests/cli/test_install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin instead of a version literal.
- `tests/cli/test_install_setup_gterm.py` — no-edit-reason: reads the `gterm` and `gclient` pins instead of version literals.
- `tests/code_index/test_gcode_gateway.py` — no-edit-reason: reads the `gcode` pin instead of a version literal.
- `tests/hooks/test_runtime_compat.py` — no-edit-reason: compares the derived floor to the `ghook` pin instead of a version literal.
- `tests/install/test_distribution.py` — no-edit-reason: reads pins by binary name; its `0.1.0` literal is a fake `gcode` output, not a pin.
- `tests/install/test_version_pins.py` — no-edit-reason: compares each pin to its crate manifest, which is this leaf's bump check.

**Acceptance:**

- 5.2.1 - Listing returns the global secrets in database order with Python's timestamp format (whole-second and fractional cases) and the sorted categories. test: `crates/gruntime/tests/secret_routes.rs::listing_matches_python_shape`.
- 5.2.2 - Deletion returns 200 for an unreferenced secret, 409 for one referenced from `config_store` or from an `mcp_servers` header value, and 404 for a missing name. A reference that appears only as a `jsonb` object key does not block deletion. test: `crates/gruntime/tests/secret_routes.rs::deletion_follows_references`.
- 5.2.3 - Deleting `embeddings_api_key` during an active embedding switch returns 500 and deletes nothing, and a native deletion waits on the advisory lock a Python mutation holds. test: `crates/gruntime/tests/secret_routes.rs::embedding_switch_blocks_key_deletion`.
- 5.2.4 - A proxied deletion records `verified` when the state matches the status and `mismatch` when it does not. test: `crates/gruntime/tests/secret_routes.rs::proxied_deletions_are_checked`.
- 5.2.5 - `gdaemon` is one patch version above the executor's starting tip in its `Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`, so this leaf's cutover passes the release-identity preflight. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 5.3 Prompt loads read the database every time [category: refactor]
`kind: deliverable`

Targets:
- `src/gobby/prompts/loader.py::PromptLoader`
- `src/gobby/storage/prompts.py::*` — scope-reason: deletes `PromptChangeEvent`, `PromptChangeNotifier`, `ChangeListener` and `ChangeEventType` with their `__all__` entries, and `LocalPromptManager`'s `notifier` parameter, `_notifier` and `_notify_change` with its three calls
- `src/gobby/servers/routes/configuration_context.py::*` — scope-reason: deletes `ConfigurationRouteContext.get_prompt_loader` and its `PromptLoader` import, which lose their only callers
- `src/gobby/servers/routes/configuration_prompts.py::register_prompt_routes`
- `tests/prompts/test_prompts_loader.py::*` — scope-reason: deletes `test_cache_behavior` and `test_cache_invalidation_via_notifier`; the rendering tests are unchanged
- `tests/storage/test_storage_prompts.py::TestPromptChangeNotifier`
- `tests/servers/routes/test_configuration_prompts.py`

`PromptLoader` keeps no cache. Its `_cache` field, `_on_change`, `clear_cache` and
the `notifier` constructor parameter go away, and `load()` calls `_get_record` on
every call: one indexed `SELECT` by name, the query `exists()` already runs. The
prompt routes drop their `get_prompt_loader().clear_cache()` calls. Those calls
cleared a loader built fresh per request, so they invalidated nothing.

`PromptChangeNotifier` and its event types are deleted. Nothing in `src/`
constructs a notifier, so `LocalPromptManager._notify_change` never fired.

Native prompt writes (5.4) are then visible to every Python loader on its next
`load()`, with no invalidation channel or listener (Orchestrator #15977's ruling
of 2026-10-14).

`tests/servers/routes/test_configuration_prompts.py` mounts the configuration
routes on an isolated database and holds the ruling's acceptance test.

Research context:
- Long-lived loaders that cached until a restart:
  - `src/gobby/tasks/expansion_service.py` (`prompt_loader`);
  - `src/gobby/mcp_proxy/importer.py` (`_loader`);
  - `src/gobby/mcp_proxy/services/recommendation.py` (`_loader`);
  - `src/gobby/utils/tool_summarizer.py` (module `_loader`);
  - `src/gobby/memory/manager.py`, whose loader the knowledge-graph extraction holds.
- Every `load()`/`render()` call site was checked against the ruling's condition.
  Each renders once per operation, before that operation's single LLM call:
  - `importer.py`: `import_from_github`, `import_from_query` and `_render_import_prompt`;
  - `recommendation.py`: `_recommend_hybrid` and `_recommend_llm`. In
    `_recommend_hybrid`, the `for c in candidates` loop only builds the candidate
    list before the single render.
  - `memory/services/knowledge_graph/extraction.py`: `extract_entities`,
    `extract_relationships` and `select_outdated_relations`. Their loops run after
    the render, over the LLM result.
  - `tool_summarizer.py`: `_summarize_description_with_llm` and
    `generate_server_description`;
  - `tasks/expansion/_compile.py::_render_prompt`: two renders per
    `_invoke_llm_compile`;
  - `sessions/summary_context.py::load_summary_prompt_template`, which already
    builds a new loader per call;
  - `feedback/service.py::_distill`: one render and up to two LLM attempts;
  - `memory/dream/planner.py::_call_llm_planner`.

  None loads per item in a loop, so none needs a cache.
- Planned check: `DATABASE_URL=… GOBBY_TEST_PROTECT=1 uv run pytest tests/prompts/test_prompts_loader.py tests/storage/test_storage_prompts.py tests/servers/routes/test_configuration_prompts.py -v`.

Consumers unchanged:
- `src/gobby/tasks/expansion_service.py` — no-edit-reason: constructs `PromptLoader(db=...)` without a notifier and calls `render`, both unchanged.
- `src/gobby/mcp_proxy/importer.py` — no-edit-reason: same constructor and `render` calls.
- `src/gobby/mcp_proxy/services/recommendation.py` — no-edit-reason: same constructor and `render` calls.
- `src/gobby/utils/tool_summarizer.py` — no-edit-reason: same constructor and `render` calls.
- `src/gobby/memory/manager.py` — no-edit-reason: same constructor; it hands the loader to extraction unchanged.
- `src/gobby/feedback/service.py` — no-edit-reason: constructs `PromptLoader(db=...)` without a notifier and calls `load` or `render`, which keep their signatures.
- `src/gobby/mcp_proxy/tools/sessions/_terminal.py` — no-edit-reason: constructs `PromptLoader(db=...)` without a notifier and calls `load` or `render`, which keep their signatures.
- `src/gobby/memory/dream/planner.py` — no-edit-reason: constructs `PromptLoader(db=...)` without a notifier and calls `load` or `render`, which keep their signatures.
- `src/gobby/memory/services/knowledge_graph/extraction.py` — no-edit-reason: takes a `PromptLoader` argument and calls `render`, which keeps its signature.
- `src/gobby/memory/services/knowledge_graph/service.py` — no-edit-reason: passes its `PromptLoader` argument through to extraction unchanged.
- `src/gobby/sessions/summary_context.py` — no-edit-reason: constructs `PromptLoader(db=...)` without a notifier and calls `load` or `render`, which keep their signatures.
- `tests/mcp_proxy/tools/sessions/test_terminal.py` — no-edit-reason: constructs `PromptLoader(db=...)` without a notifier and calls `load` or `render`, which keep their signatures.
- `tests/memory/test_dream.py` — no-edit-reason: patches `PromptLoader.render` and constructs `PromptLoader(db=...)`, both unchanged.
- `tests/prompts/test_delete_relations_prompt.py` — no-edit-reason: constructs `PromptLoader(db=...)` without a notifier and calls `load` or `render`, which keep their signatures.
- `tests/prompts/test_entity_extraction_prompt.py` — no-edit-reason: constructs `PromptLoader(db=...)` without a notifier and calls `load` or `render`, which keep their signatures.
- `tests/prompts/test_relationship_extraction_prompt.py` — no-edit-reason: constructs `PromptLoader(db=...)` without a notifier and calls `load` or `render`, which keep their signatures.
- `tests/prompts/test_untrusted_content_prompts.py` — no-edit-reason: constructs `PromptLoader(db=...)` without a notifier and calls `load` or `render`, which keep their signatures.
- `src/gobby/servers/routes/configuration.py` — no-edit-reason: calls `register_prompt_routes(router, context)`, whose signature is unchanged.

**Acceptance:**

- 5.3.1 - A long-lived `PromptLoader` that already loaded a prompt returns the new content on its next `load()` after an override is written through `PUT /api/config/prompts/{path}`, with no restart. test: `tests/servers/routes/test_configuration_prompts.py::test_route_override_reaches_a_long_lived_loader`.
- 5.3.2 - `PromptLoader` has no cache, `clear_cache` or notifier parameter, and `PromptChangeNotifier` no longer exists. file: `src/gobby/prompts/loader.py`.

### 5.4 Prompt routes: JSON save contract and native serving [category: code] (depends: 5.2, 5.3)
`kind: deliverable`

Targets:
- `src/gobby/servers/routes/configuration_prompts.py::register_prompt_routes`
- `src/gobby/servers/routes/configuration_models.py::SavePromptOverrideRequest`
- `tests/servers/routes/test_configuration_prompts.py`
- `crates/gruntime/src/lib.rs`
- `crates/gruntime/src/prompts.rs`
- `crates/gruntime/src/verify.rs`
- `crates/gruntime/tests/prompts.rs`
- `crates/gruntime/tests/fixtures/prompt_route_corpus.json`
- `crates/gdaemon/Cargo.toml`
- `src/gobby/install/version_pins.py::MANAGED_BIN_VERSION_PINS`
- `Cargo.lock`

Release ownership (Constraints): gruntime is in gdaemon's release closure, so this
leaf bumps `gdaemon` by 0.0.1 from the executor's starting tip.

This leaf changes the Python save contract and ports all four routes.

**Save contract** (Orchestrator #15977, 2026-10-14). Python and Rust both apply it.

1. The body is a JSON object:
   - `content`, a string, is required;
   - `description` and `version`, strings, are optional;
   - `variables`, an object or `null`, is optional.

   Unknown members are ignored, as pydantic does. Any other shape returns 422
   `{"detail": "invalid prompt request"}`. The Python route parses the body itself,
   decoding it as strict UTF-8 before `json.loads`, so FastAPI's validation list
   never appears and both implementations answer alike. The parse runs before the
   handler's `try`, whose generic branch would otherwise answer 500.
2. Content that opens with a frontmatter block returns 400
   `{"detail": "Prompt content must not start with frontmatter; send description, version and variables as JSON fields"}`.
   A block is found by `parse_frontmatter`'s delimiter scan alone:
   - the content starts with `---`;
   - its first line strips to `---`;
   - a later line strips to `---`.

   "Strip" means Python's `str.strip()`, whose whitespace set is Rust's
   `char::is_whitespace` plus U+001C through U+001F. No YAML is parsed.
3. The body is `content.strip()`. An empty body returns 400
   `{"detail": "Prompt content must not be empty"}`.
4. An omitted field inherits from the record being overridden: the existing
   override (`get_override`), else the effective record (`get_by_name`). With
   neither, the defaults are `""`, `"1.0"` and `null`. A present field replaces the
   inherited value, and explicit `"variables": null` clears it. Variables are stored
   as `jsonb`; an empty object is stored as `NULL`
   (`json.dumps(variables) if variables else None`).
5. An existing override is updated (`update_prompt`: description, content, version,
   variables and `updated_at`). Otherwise a row is inserted with:
   - scope `project` when the daemon has a project, else `global`;
   - id `uuid5(uuid5(NAMESPACE_URL, "gobby:prompts"), f"{name}:{scope}:{project_id or 'none'}")`,
     or a random v4 id when that id is taken (`create_prompt`).
6. Respond 200 `{"ok": true}`. Any other failure returns 500
   `{"detail": "Internal server error"}`.

This fixes the wipe: the UI sends `{content}` only (`web/src/api/config.ts::savePrompt`),
and Python used to reset description, version and variables on every UI save. A
variable that lost its default then failed under `StrictUndefined`.

**Native routes.** Four rows, each for `Operator`: `GET /api/config/prompts`,
`GET /api/config/prompts/{*path}`, `PUT /api/config/prompts/{*path}` and
`DELETE /api/config/prompts/{*path}`. The path is percent-decoded, as Starlette
decodes it before routing (`encodeURIComponent` sends `/` as `%2F`).

- **Daemon project.** `RuntimeFamily::new` resolves it once, as Python's
  `runner_init/services.py::_init_project_context` does with
  `utils/project_context.py::get_project_context(Path.cwd())`:
  1. Canonicalize the process cwd.
  2. Walk up to the first directory holding `.gobby/project.json`.
  3. Read it as a JSON object. A missing or unreadable file, or a non-object,
     means no project.
  4. A falsy `id` means no project, and a string `id` is the project. Any other
     truthy `id` makes the prompt routes return 500, because Python's `str()` of
     it is never a valid uuid and its queries fail.

  `gobby_core::project::find_project_root` and `read_project_id` are not reused,
  since they also accept `.gobby/gcode.json` and Python does not. gdaemon spawns
  the backend without `current_dir` (`crates/gdaemon/src/lifecycle/backend.rs`), so
  both processes resolve from one directory. The project id binds as text and
  is cast `::uuid` in SQL, so a malformed id fails as Python's does.
- **List.** `limit` and `offset` are optional query values; the last occurrence
  wins. When present, each must match `^[0-9]+$`, and `limit` must be at least 1.
  Otherwise both implementations return 422 `{"detail": "invalid prompt list query"}`;
  the Python route moves to this manual parse in this leaf. Defaults are 500 and 0;
  the page limit is `min(limit, 1000)`. The handler then runs `list_prompts` and
  `count_prompts`' SQL (`enabled = TRUE`, the daemon project or `project_id IS NULL`,
  `ORDER BY name ASC, scope ASC`, `COUNT(DISTINCT name)`).
  - The first row per name supplies its description. A name is overridden when any
    of its rows has scope `global` or `project`.
  - Prompts are sorted by name in code-point order (Rust `String` order).
  - `category` is the text before the first `/`, else `general`.
  - The response is `{prompts: [{path, description, category, source, has_override}], categories, total, limit, offset, count}`.
- **Detail.** `get_by_name` precedence (project, then global, then bundled; enabled
  only) returns 404 `{"detail": "Prompt '<path>' not found"}` when absent.
  `bundled_content` is the first bundled row's content when the record is an
  override, else `null`. `variables` maps each spec through
  `_normalize_variable_spec`: a `null` spec becomes
  `{"type": "str", "required": false, "default": null}`; an object takes `type`,
  `required` and `default` with defaults `"str"`, `false` and `null` only when the
  member is absent; any other value becomes the default of a `str` spec.
- **Save.** The contract above, with `get_override`/`get_by_name` SQL ported as
  written.
- **Delete.** No override returns 404 `{"detail": "No override for '<path>'"}`, and
  a deleted row returns `{"ok": true}`. A row gone by delete time also returns 404,
  as `delete_prompt` returning `False` does.

`verify.rs` routes claimed proxied prompt writes to `prompts::check_proxied`:

- **PUT 200:** for each field the request sets, the stored override must equal it,
  and the content must equal the stripped content. Otherwise
  `Mismatch("/<field>")`. Omitted fields are not checked, since their pre-write
  source is overwritten by the write.
- **PUT 400 or 422:** `Verified` when native validation rejects the same body with
  the same detail, else `Mismatch("/detail")`.
- **DELETE 200 or 404:** `Verified` when no override remains for the path, else
  `Mismatch("/ok")` or `Mismatch("/detail")`.
- Any other status is `Skipped`.

`tests/fixtures/prompt_route_corpus.json` is the shared corpus. Each case holds
seeded `prompts` rows, one request, and the expected status, body and resulting
override row. It covers:

- a body-only save over a bundled prompt with variables;
- a body-only save over an existing override;
- an explicit `variables` replacement and an explicit `null`;
- a frontmatter-led body, and one led by `\x1c---`;
- an empty body and an invalid body;
- list paging: default, capped and invalid;
- detail with and without an override;
- deletes with and without an override.

Python runs every case through the FastAPI routes, and Rust through the native
router.

Research context:
- Python: `configuration_prompts.py::register_prompt_routes` and
  `_normalize_variable_spec`, and `storage/prompts.py::LocalPromptManager`
  (`get_by_name`, `get_bundled`, `get_override`, `delete_override`,
  `update_prompt`, `create_prompt`, `list_prompts`, `count_prompts`).
- `prompts.project_id` is `uuid`, `variables` is `jsonb`, and `description` is
  `text NOT NULL` (`baseline.sql`).
- Bundled sync keeps `src/gobby/prompts/models.py::parse_frontmatter` unchanged; the
  route uses only its delimiter rule.
- Rejected:
  - parsing frontmatter natively. That needs YAML 1.1 scalar style and raw text,
    which `yaml_serde` hides. `libyaml-rs` exposes only an unsafe API, and the
    workspace sets `unsafe_code = "forbid"`.
  - keeping PUT on Python permanently. The JSON contract removes the parser from
    both sides instead.
- Planned checks:
  - `cargo nextest run -p gobby-runtime --test prompts`;
  - `DATABASE_URL=… GOBBY_TEST_PROTECT=1 uv run pytest tests/servers/routes/test_configuration_prompts.py -v`.

Consumers unchanged:
- `web/src/api/config.ts` — no-edit-reason: `savePrompt` already sends `{content}`, which now inherits the record's metadata.
- `src/gobby/servers/routes/configuration.py` — no-edit-reason: calls `register_prompt_routes(router, context)` and re-exports `SavePromptOverrideRequest`; both names and the call are unchanged.
- `src/gobby/cli/install_setup_gclient.py` — no-edit-reason: reads only the `gclient` pin, which no leaf of this plan bumps.
- `src/gobby/cli/install_setup_gcode.py` — no-edit-reason: reads the `gcode` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_ghook.py` — no-edit-reason: reads the `ghook` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_gterm.py` — no-edit-reason: reads only the `gterm` pin, which no leaf of this plan bumps.
- `src/gobby/cli/install_setup_versions.py` — no-edit-reason: looks a pin up by the binary name it is given.
- `src/gobby/code_index/gcode_gateway.py` — no-edit-reason: derives `MIN_GCODE_GRAPH_VERSION` from the `gcode` pin at import.
- `src/gobby/hooks/runtime_compat.py` — no-edit-reason: derives `MINIMUM_GHOOK_VERSION_FOR_SUPPORTED_SCHEMA` from the `ghook` pin at import.
- `src/gobby/install/bin_freshness_models.py` — no-edit-reason: iterates every pin, so the bumped values flow through.
- `src/gobby/install/distribution.py` — no-edit-reason: looks a pin up by binary name.
- `tests/cli/installers/test_grok_installer.py` — no-edit-reason: reads the `ghook` pin instead of a version literal.
- `tests/cli/test_install_ghook.py` — no-edit-reason: builds its fixtures from the `ghook` pin instead of a version literal.
- `tests/cli/test_install_setup.py` — no-edit-reason: reads the `gcode` pin instead of a version literal.
- `tests/cli/test_install_setup_gclient.py` — no-edit-reason: reads the `gclient` pin instead of a version literal.
- `tests/cli/test_install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin instead of a version literal.
- `tests/cli/test_install_setup_gterm.py` — no-edit-reason: reads the `gterm` and `gclient` pins instead of version literals.
- `tests/code_index/test_gcode_gateway.py` — no-edit-reason: reads the `gcode` pin instead of a version literal.
- `tests/hooks/test_runtime_compat.py` — no-edit-reason: compares the derived floor to the `ghook` pin instead of a version literal.
- `tests/install/test_distribution.py` — no-edit-reason: reads pins by binary name; its `0.1.0` literal is a fake `gcode` output, not a pin.
- `tests/install/test_version_pins.py` — no-edit-reason: compares each pin to its crate manifest, which is this leaf's bump check.

**Acceptance:**

- 5.4.1 - A UI-style body-only save keeps the overridden record's variables, description and version, and a save that sets `variables` replaces them. test: `tests/servers/routes/test_configuration_prompts.py::test_body_only_save_keeps_metadata`.
- 5.4.2 - A frontmatter-led body returns 400 naming the JSON fields, in both implementations. test: `tests/servers/routes/test_configuration_prompts.py::test_frontmatter_led_body_is_rejected`.
- 5.4.3 - Python's routes produce every corpus case's status, body and stored row. test: `tests/servers/routes/test_configuration_prompts.py::test_python_routes_match_the_corpus`.
- 5.4.4 - The native routes produce every corpus case's status, body and stored row. test: `crates/gruntime/tests/prompts.rs::native_routes_match_the_corpus`.
- 5.4.5 - The daemon project resolves from the cwd's `.gobby/project.json` only, and a project-scoped override wins over global and bundled rows in list and detail. test: `crates/gruntime/tests/prompts.rs::daemon_project_scopes_prompts`.
- 5.4.6 - Proxied prompt writes record `verified` when the stored row matches and `mismatch` when it does not. test: `crates/gruntime/tests/prompts.rs::proxied_prompt_writes_are_checked`.
- 5.4.7 - `gdaemon` is one patch version above the executor's starting tip in its `Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`, so this leaf's cutover passes the release-identity preflight. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 5.5 Native YAML template read and export [category: code] (depends: 5.4)
`kind: deliverable`

Targets:
- `scripts/generate_runtime_config_contract.py`
- `crates/gcore/assets/config/runtime_config_contract.json::*` — scope-reason: regenerated; every `exactKeys` and `patterns` entry gains `daemonKey`
- `crates/gcore/src/config/runtime_contract.rs::*` — scope-reason: the specs parse `daemonKey`, `activation` and `type`, and the new public `export_policy` classifies a stored key
- `crates/gcore/src/config/mod.rs::*` — scope-reason: re-exports `export_policy` and `ExportPolicy`
- `src/gobby/config/documents.py::ConfigDocumentsService._dump`
- `tests/config/test_runtime_config_contract.py::*` — scope-reason: adds `test_no_restricted_key_matches_a_contract_pattern`
- `tests/config/test_yaml_export_vectors.py`
- `crates/gruntime/src/lib.rs`
- `crates/gruntime/src/export.rs`
- `crates/gruntime/src/pyyaml.rs`
- `crates/gruntime/src/verify.rs`
- `crates/gruntime/Cargo.toml`
- `crates/gruntime/tests/export.rs`
- `crates/gruntime/tests/fixtures/yaml_export_vectors.json`
- `crates/gdaemon/Cargo.toml`
- `crates/gcode/Cargo.toml`
- `crates/ghook/Cargo.toml`
- `src/gobby/install/version_pins.py::MANAGED_BIN_VERSION_PINS`
- `Cargo.lock`

Release ownership (Constraints): this leaf changes gcore, so it bumps `gdaemon`,
`gcode` and `ghook` by 0.0.1 from the executor's starting tip.

Rows: `GET /api/config/template` and `POST /api/config/export`, both for
`Operator`. Both return what `ConfigDocumentsService.export_yaml` returns:
`{"revision": <revision>, "content": <yaml>}`.

**Contract carrier.** The generator adds `daemonKey` (the spec's `source_path is not
None`) to every entry. The contract already carries `activation`, `secrecy`,
`type` and `fields`. Twenty registered specs have no source path (`ui_settings.*`,
among others), and no other contract field implies it. The regenerated codec
vectors are unchanged.

**`export_policy(key)`** mirrors `_dump`'s per-key checks:

1. Resolve the key as `ConfigRegistry.resolve` does: an exact entry, else the one
   matching pattern. More than one match is `Ambiguous`, which Python raises as
   `RegistryError` and the handler returns as 500. No match means skip; Python
   skips unknown keys, and restricted keys are absent from the contract.
2. Skip when `daemonKey` is false or `activation` is `managed`. Restricted keys never
   reach this step.
3. Effective secrecy follows `config_key_secrecy`: the exact entry's secrecy; for a
   pattern, the pattern's own secrecy when it is not `none`, else the matched
   `{field}`'s secrecy. `reference` gives `MaskScalar`.
4. Otherwise an exact entry whose `type` is the string `array` and that has
   `reference` fields gives `MaskItems(fields)` (`config_structured_reference_fields`).
5. Otherwise `Plain`.

`tests/config/test_runtime_config_contract.py::test_no_restricted_key_matches_a_contract_pattern`
pins step 1's assumption: no `RESTRICTED` spec key matches a contract pattern.

**`export.rs`:**

1. Read 2.2's row through 3.1's `active_state` (`revision` and
   `desired_overrides`). With no row it returns 503
   `{"error": {"code": "runtime_unavailable", "message": "Configuration runtime is not ready", "path": [], "retryable": true}}`,
   the body `ConfigDocumentsService` raises without a snapshot. A read failure
   returns 500
   `{"error": {"code": "persistence_indeterminate", "message": "Configuration export failed", "path": [], "retryable": false}}`.
2. Apply `export_policy` to each override:
   - `MaskScalar` turns a value other than `null` or `""` into `"********"`
     (`MASKED_SECRET`);
   - `MaskItems` copies the list, and in each object item masks each listed field
     whose value is not `null` or `""`.
3. Unflatten as `storage/config_store.py::unflatten_config` does: sort by
   (dot count, key) and split on `.`. A scalar that conflicts with a nested key
   returns the same 500 body.
4. Emit with `pyyaml.rs`.

**`pyyaml.rs`** ports PyYAML 6.0.3's `safe_dump(data, default_flow_style=False, sort_keys=True)`
for JSON values. This leaf pins Python's `_dump` to `width=math.inf`, so neither
side folds lines. The port covers:

- **Resolver.** `yaml/resolver.py`'s implicit resolvers (bool, float, int, merge,
  null, timestamp, value) with their first-character dispatch, as `regex`
  patterns. `regex` is already in `Cargo.lock`; gruntime adds it as a dependency.
- **Representer.** `SafeRepresenter`:
  - `null` and the booleans `true`/`false`;
  - integers in decimal;
  - floats as `represent_float` writes them (`.nan`, `.inf`, `-.inf`, else Python
    `repr` lowercased, with `.0` inserted before `e` when the repr has no `.`);
  - mapping keys sorted in code-point order.

  JSON-decoded values are distinct objects, so no anchors are emitted.
- **Emitter.** `yaml/emitter.py` in block style:
  - nested mappings indent by 2;
  - a sequence under a mapping key is indentless (`- item` at the key's column);
  - a mapping inside a sequence starts on the dash line;
  - empty collections are `{}` and `[]`;
  - an empty root mapping dumps as `{}\n`.

  Scalar style follows `analyze_scalar` and `choose_scalar_style`, including the
  simple-key rules for keys. A string stays plain only when it resolves to `str` as
  plain text; otherwise it is single-quoted when allowed, else double-quoted. With
  `allow_unicode` false, every non-ASCII or non-printable character forces double
  quotes. The writers `write_plain`, `write_single_quoted` (quote doubling and
  line-break doubling) and `write_double_quoted` (`ESCAPE_REPLACEMENTS`, then
  `\x`, `\u` or `\U` by code point) are ported.

`tests/fixtures/yaml_export_vectors.json` holds `{input, yaml}` pairs generated
once from Python. The cases cover:

- plain strings, and strings that resolve to other types: `yes`, `no`, `on`, `off`,
  `null`, `~`, `true`, `0755`, `1.0`, `1_000`, `12:30`, `2024-01-01`, `=` and `<<`;
- leading and trailing spaces, `#`, `: ` and `- `;
- multi-line, non-ASCII, control-character and empty strings, and a 200-character
  string;
- nested lists of mappings and empty collections;
- the floats `0.1`, `1e20`, `-0.0` and `1e-7`, and a 20-digit integer.

The Python test asserts that each `yaml` equals the pinned `_dump`'s output for the
case and that `yaml.safe_load(yaml)` returns `input`. The Rust test asserts byte
equality.

`verify.rs` routes a claimed proxied `POST /api/config/export`. On 200, it compares
the body's `content` with a native export of the same revision: `Verified` when the
bytes are equal, else `Mismatch("/content")`. When the body's revision differs
from native's, or on any other status, it returns `Skipped`. `GET
/api/config/template` compares under 2.1's GET policy.

Python exports its active bundle's snapshot, which 2.2 publishes on every swap, so
both sides export one revision and one override set. A swap between the proxied
export and the native recomputation changes the revision, and the check skips.
Python's `runtime_unavailable` 503 is not reachable over HTTP: `GobbyRunner.create`
returns only after `ConfigRuntime.start()` reaches its initial epoch, and shutdown
begins the uvicorn drain (`runner_lifecycle_shutdown.py`) before
`config_runtime.close`. Native returns the same body only before Python's first
publish for the deployment.

Research context:
- Python: `src/gobby/servers/routes/configuration_templates.py::register_template_routes`,
  `configuration_import_export.py::register_import_export_routes` and
  `src/gobby/config/documents.py::ConfigDocumentsService.export_yaml` and `._dump`.
- The snapshot is built with `revision=stored.revision` and
  `desired_overrides=stored.overrides` (`config/runtime_activation.py::project_activation`,
  `config/runtime.py`). It is persisted state at the reconciled revision, which
  2.2 publishes as the row's `revision` and `desired_overrides`.
- `src/gobby/config/registry.py`: `config_key_secrecy`,
  `config_structured_reference_fields` and `ConfigPatternSpec.match`. gcore's
  `PatternSpec::matches` already decodes dynamic segments.
- `src/gobby/config/voice_secrets.py::mask_structured_references` and
  `src/gobby/config/secret_mask.py::MASKED_SECRET`.
- PyYAML 6.0.3 is the locked version.
- Planned checks:
  - `cargo nextest run -p gobby-runtime --test export`;
  - `DATABASE_URL=… GOBBY_TEST_PROTECT=1 uv run pytest tests/config/test_yaml_export_vectors.py tests/config/test_runtime_config_contract.py -v`.

Consumers unchanged:
- `web/src/api/runtimeConfigCodecVectors.gen.ts` — no-edit-reason: the generator's codec vectors do not read the new `daemonKey` member, so regeneration leaves them byte-identical; `test_checked_in_web_codec_vectors_match_registry` proves it.
- `src/gobby/cli/install_setup_gclient.py` — no-edit-reason: reads only the `gclient` pin, which no leaf of this plan bumps.
- `src/gobby/cli/install_setup_gcode.py` — no-edit-reason: reads the `gcode` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_ghook.py` — no-edit-reason: reads the `ghook` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_gterm.py` — no-edit-reason: reads only the `gterm` pin, which no leaf of this plan bumps.
- `src/gobby/cli/install_setup_versions.py` — no-edit-reason: looks a pin up by the binary name it is given.
- `src/gobby/code_index/gcode_gateway.py` — no-edit-reason: derives `MIN_GCODE_GRAPH_VERSION` from the `gcode` pin at import.
- `src/gobby/hooks/runtime_compat.py` — no-edit-reason: derives `MINIMUM_GHOOK_VERSION_FOR_SUPPORTED_SCHEMA` from the `ghook` pin at import.
- `src/gobby/install/bin_freshness_models.py` — no-edit-reason: iterates every pin, so the bumped values flow through.
- `src/gobby/install/distribution.py` — no-edit-reason: looks a pin up by binary name.
- `tests/cli/installers/test_grok_installer.py` — no-edit-reason: reads the `ghook` pin instead of a version literal.
- `tests/cli/test_install_ghook.py` — no-edit-reason: builds its fixtures from the `ghook` pin instead of a version literal.
- `tests/cli/test_install_setup.py` — no-edit-reason: reads the `gcode` pin instead of a version literal.
- `tests/cli/test_install_setup_gclient.py` — no-edit-reason: reads the `gclient` pin instead of a version literal.
- `tests/cli/test_install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin instead of a version literal.
- `tests/cli/test_install_setup_gterm.py` — no-edit-reason: reads the `gterm` and `gclient` pins instead of version literals.
- `tests/code_index/test_gcode_gateway.py` — no-edit-reason: reads the `gcode` pin instead of a version literal.
- `tests/hooks/test_runtime_compat.py` — no-edit-reason: compares the derived floor to the `ghook` pin instead of a version literal.
- `tests/install/test_distribution.py` — no-edit-reason: reads pins by binary name; its `0.1.0` literal is a fake `gcode` output, not a pin.
- `tests/install/test_version_pins.py` — no-edit-reason: compares each pin to its crate manifest, which is this leaf's bump check.

**Acceptance:**

- 5.5.1 - Native export bytes equal every committed vector. test: `crates/gruntime/tests/export.rs::emitter_matches_python_vectors`.
- 5.5.2 - Each vector equals the pinned Python `_dump` output and round-trips through `yaml.safe_load`. test: `tests/config/test_yaml_export_vectors.py::test_vectors_match_python_dump`.
- 5.5.3 - Export skips unknown, restricted, sourceless and managed keys, masks reference secrets and structured reference fields, returns 500 for an ambiguous key or a scalar/nested conflict, and returns Python's 503 `runtime_unavailable` body with no published row. test: `crates/gruntime/tests/export.rs::export_filters_and_masks_like_python`.
- 5.5.4 - No restricted key matches a contract pattern. test: `tests/config/test_runtime_config_contract.py::test_no_restricted_key_matches_a_contract_pattern`.
- 5.5.5 - A proxied export of the native revision records `verified`, and different content records `mismatch`. test: `crates/gruntime/tests/export.rs::proxied_exports_are_checked`.
- 5.5.6 - `gdaemon`, `gcode` and `ghook` are each one patch version above the executor's starting tip in their `Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`, so this leaf's cutover passes the release-identity preflight. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 5.6 Native active-state reads: values, UI settings and global tool approvals [category: code] (depends: 5.5)
`kind: deliverable`

Targets:
- `crates/gcore/src/config/runtime_contract.rs::*` — scope-reason: the specs parse `visibility`, and the new public `public_policy` classifies a key for the values projection
- `crates/gcore/src/config/mod.rs::*` — scope-reason: re-exports `public_policy` and `PublicPolicy`
- `crates/gruntime/src/lib.rs`
- `crates/gruntime/src/export.rs`
- `crates/gruntime/src/active_reads.rs`
- `crates/gruntime/src/pystr.rs`
- `crates/gruntime/tests/active_reads.rs`
- `crates/gruntime/tests/fixtures/active_read_vectors.json`
- `tests/servers/routes/test_active_read_vectors.py`
- `crates/gdaemon/Cargo.toml`
- `crates/gcode/Cargo.toml`
- `crates/ghook/Cargo.toml`
- `src/gobby/install/version_pins.py::MANAGED_BIN_VERSION_PINS`
- `Cargo.lock`

Release ownership (Constraints): this leaf changes gcore, so it bumps `gdaemon`,
`gcode` and `ghook` by 0.0.1 from the executor's starting tip.

Rows: `GET /api/config/values`, `GET /api/config/ui-settings` and
`GET /api/config/tool-approvals/global`, each for `Operator`. All three read 2.2's
row through 3.1's `active_state` and compare under 2.1's GET policy, which ignores
JSON key order.

**`public_policy(key)`** in gcore mirrors `ConfigValuesService._public_projection`
and `._is_public_key`:

1. Resolve the key as 5.5's `export_policy` step 1 does. More than one match is
   `Ambiguous`; no match means `Skip`. Restricted keys are absent from the
   contract, and Python skips them as not public.
2. `Skip` when the entry's `visibility` is not `public`. The contract's only other
   value is `machine`.
3. `Reference` when the effective secrecy (5.5's step 3) is `reference`.
4. `MaskItems(fields)` under 5.5's step 4 condition.
5. Otherwise `Plain`.

Unlike `export_policy`, this policy never skips sourceless or managed keys:
`_public_projection` checks only visibility, so `ui_settings.*` and managed keys
appear in the values body.

5.6 makes `export.rs`'s unflatten and `MaskItems` masking `pub(crate)`, and
`active_reads.rs` calls them.

`crates/gruntime/src/pystr.rs` is new. It holds the Python `str` semantics the
native ports reproduce, starting with `isspace` and the argument-free `strip`
built on it. 5.7 adds case mapping and `repr`.

**`GET /api/config/values`** returns `ConfigValuesService._snapshot_body` over the
row:

```json
{
  "revision": 7,
  "desired": {"<nested>": "..."},
  "active": {"<nested>": "..."},
  "secret_set": {"<key>": {"desired": true, "active": false}},
  "pending_restart_keys": ["<key>"],
  "failed_live_keys": {"<key>": {"revision": 7, "subscriber": "<name>"}}
}
```

1. Project `desired_values` and `active_values` separately through
   `public_policy`:
   - `Skip` drops the key.
   - `Reference` sets `is_set` when the value is a string that starts with
     `$secret:`. The projected value is `"********"` when set, else `null`, and
     `secret_set` records `is_set` for that side.
   - `MaskItems` applies 5.5's item masking.
   - `Plain` copies the value.
2. Unflatten each projection with 5.5's unflatten.
3. `secret_set` covers the sorted union of both sides' reference keys. A side that
   lacks the key reports `false`.
4. `pending_restart_keys` keeps the public keys, sorted. `failed_live_keys` keeps
   the public keys sorted by key, each with the row's `revision` and `subscriber`.

With no row the route returns 503
`{"error": {"code": "runtime_unavailable", "message": "Configuration runtime is not ready", "path": [], "retryable": true}}`,
the body `ConfigValuesService._snapshot` raises.

An `Ambiguous` key or an unflatten conflict returns 500
`{"status": "error", "message": "Internal server error", "error_logged": true}`.
Python raises `RegistryError` or `ValueError` there, outside `ConfigValuesError`,
and `servers/exception_handlers.py::global_exception_handler` writes that body. A
row read failure returns the same body.

**`GET /api/config/ui-settings`** walks
`configuration_ui_settings.py::UI_SETTINGS_KEYS` in its order: `fontSize`,
`model`, `theme`, `readingDirection`, `defaultChatMode`, `sttEnabled`,
`ttsEnabled`, `voiceInputMode`, `planPendingVariant`, `selectedProjectId`,
`selectedProvider`. For each name it reads `active_values["ui_settings.<name>"]`
and includes the name when the value is present and not `null`.

- With no row it returns 3.1's 503 `Configuration runtime is still starting` body,
  which `ConfigurationRouteContext.get_config_snapshot` raises.
- A row read failure returns 500 `{"detail": "Internal server error"}`, the body
  Python's `_UI_SETTING_STORAGE_ERRORS` branch returns.

**`GET /api/config/tool-approvals/global`** ports
`servers/tool_approvals.py::get_global_approval_rules`:

1. Read `active_values["tool_approvals.global_rules"]`.
2. A missing or `null` value, or a value that is not a list, gives the defaults
   `["tool:Read", "tool:Glob", "tool:Grep", "tool:Ls"]`
   (`DEFAULT_GLOBAL_APPROVAL_RULES`).
3. A list goes through `sanitize_approval_rules`:
   - strip each item of the characters Python's `str.isspace` matches. That set
     is Rust's `char::is_whitespace` plus U+001C–U+001F, which `str::trim` keeps,
     so the port calls `pystr::strip`;
   - drop empty items and repeats;
   - keep first-seen order.

   The registry annotates the key `list[str]`, so every stored item is a string.

The body is `{"rules": [...], "default_rules": [<the four defaults>], "built_in_exemptions": ["mcp:gobby*:*", "tool:Bash (gcode only)"]}`.

- With no row it returns 3.1's 503 `still starting` body.
- A row read failure returns 500 `{"detail": "Failed to load approval rules"}`, the
  body of Python's `_APPROVAL_STORAGE_ERRORS` branch.

`crates/gruntime/tests/fixtures/active_read_vectors.json` holds cases generated once
from Python. Each case has a `revision`, a `state` with four fields of 2.2's
document (`desired_values`, `active_values`, `pending_restart_keys` and
`failed_live_keys`), and the expected `values`, `ui_settings` and `tool_approvals`
bodies. The cases cover:

- unknown, machine-visibility and restricted keys;
- a pattern key and a dynamic-segment key;
- a reference key that is set on one side only, and one unset on both;
- a structured key with reference fields;
- non-public pending and failed-live keys;
- `null` and absent UI settings;
- approval rules that are `null`, the empty list, and a list with padding,
  U+001F padding, empty items and repeats.

The Python test builds a `ConfigSnapshot` from each case's maps. It asserts that
2.2's `active_state_document` reproduces the case's `state` value fields, and that
each route served through `TestClient` returns the case's bodies. Its fake runtime
and context follow `tests/servers/routes/test_config_values_api.py`'s
`_FakeRuntime` and `_FakeContext`. The Rust test seeds each `state` as the row and
asserts the native bodies are equal as JSON values.

Research context:
- Python: `src/gobby/servers/routes/configuration_values.py` (`get_config_values`),
  `src/gobby/config/values.py::ConfigValuesService._snapshot_body`,
  `._public_projection`, `._failed_live` and `._is_public_key`.
- `src/gobby/servers/routes/configuration_ui_settings.py::register_ui_setting_routes`
  and `src/gobby/servers/routes/configuration_tool_approvals.py`
  (`get_global_tool_approval_rules`, with `BUILT_IN_EXEMPTION_LABELS` and
  `DEFAULT_GLOBAL_APPROVAL_RULES` from `src/gobby/servers/tool_approvals.py`).
- The contract's 449 exact keys carry `visibility` `public` (447) or `machine` (2).
  gcore's `ExactKeySpec` and `PatternSpec` do not parse it today.
- Python reads the active bundle's snapshot, and 2.2 publishes it on every swap,
  so both sides serve one revision.
- Planned checks:
  - `cargo nextest run -p gobby-runtime --test active_reads`;
  - `DATABASE_URL=… GOBBY_TEST_PROTECT=1 uv run pytest tests/servers/routes/test_active_read_vectors.py -v`.

Consumers unchanged:
- `src/gobby/cli/install_setup_gclient.py` — no-edit-reason: reads only the `gclient` pin, which no leaf of this plan bumps.
- `src/gobby/cli/install_setup_gcode.py` — no-edit-reason: reads the `gcode` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_ghook.py` — no-edit-reason: reads the `ghook` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_gterm.py` — no-edit-reason: reads only the `gterm` pin, which no leaf of this plan bumps.
- `src/gobby/cli/install_setup_versions.py` — no-edit-reason: looks a pin up by the binary name it is given.
- `src/gobby/code_index/gcode_gateway.py` — no-edit-reason: derives `MIN_GCODE_GRAPH_VERSION` from the `gcode` pin at import.
- `src/gobby/hooks/runtime_compat.py` — no-edit-reason: derives `MINIMUM_GHOOK_VERSION_FOR_SUPPORTED_SCHEMA` from the `ghook` pin at import.
- `src/gobby/install/bin_freshness_models.py` — no-edit-reason: iterates every pin, so the bumped values flow through.
- `src/gobby/install/distribution.py` — no-edit-reason: looks a pin up by binary name.
- `tests/cli/installers/test_grok_installer.py` — no-edit-reason: reads the `ghook` pin instead of a version literal.
- `tests/cli/test_install_ghook.py` — no-edit-reason: builds its fixtures from the `ghook` pin instead of a version literal.
- `tests/cli/test_install_setup.py` — no-edit-reason: reads the `gcode` pin instead of a version literal.
- `tests/cli/test_install_setup_gclient.py` — no-edit-reason: reads the `gclient` pin instead of a version literal.
- `tests/cli/test_install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin instead of a version literal.
- `tests/cli/test_install_setup_gterm.py` — no-edit-reason: reads the `gterm` and `gclient` pins instead of version literals.
- `tests/code_index/test_gcode_gateway.py` — no-edit-reason: reads the `gcode` pin instead of a version literal.
- `tests/hooks/test_runtime_compat.py` — no-edit-reason: compares the derived floor to the `ghook` pin instead of a version literal.
- `tests/install/test_distribution.py` — no-edit-reason: reads pins by binary name; its `0.1.0` literal is a fake `gcode` output, not a pin.
- `tests/install/test_version_pins.py` — no-edit-reason: compares each pin to its crate manifest, which is this leaf's bump check.

**Acceptance:**

- 5.6.1 - Native `GET /api/config/values` returns each vector's Python body, including masked and unset reference keys, masked structured fields, and public-only pending and failed-live keys. test: `crates/gruntime/tests/active_reads.rs::values_match_python_vectors`.
- 5.6.2 - Native `GET /api/config/ui-settings` and `GET /api/config/tool-approvals/global` return each vector's Python bodies, including default rules for `null` and the `str.isspace` strip. test: `crates/gruntime/tests/active_reads.rs::ui_settings_and_approvals_match_python_vectors`.
- 5.6.3 - Each vector matches Python's three routes and 2.2's published value fields. test: `tests/servers/routes/test_active_read_vectors.py::test_vectors_match_python_routes`.
- 5.6.4 - With no published row the values route returns the `not ready` 503 and the other two the `still starting` 503. An ambiguous key or unflatten conflict returns the global 500 body, and a row read failure returns each route's Python 500 body. test: `crates/gruntime/tests/active_reads.rs::missing_row_and_failures_match_python`.
- 5.6.5 - `gdaemon`, `gcode` and `ghook` are each one patch version above the executor's starting tip in their `Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`, so this leaf's cutover passes the release-identity preflight. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 5.7 Per-key config validation [category: code] (depends: 5.6)
`kind: deliverable`

Targets:
- `scripts/generate_runtime_config_contract.py`
- `crates/gruntime/assets/runtime_config_validation.json`
- `crates/gcore/assets/config/runtime_config_contract.json::*` — scope-reason: regenerated after the `backup_path` change, which must leave its bytes unchanged
- `tests/config/test_runtime_config_contract.py::*` — scope-reason: adds `test_checked_in_validation_carrier_matches_registry`
- `src/gobby/storage/config_mutations.py::ConfigMutations._validate_value`
- `src/gobby/config/persistence.py::MemoryBackupConfig`
- `src/gobby/sync/memories.py::MemoryBackupManager.__init__`
- `src/gobby/cli/memory/export.py::backup_memories`
- `src/gobby/cli/memory/export.py::restore_memories`
- `tests/config/test_persistence.py::*` — scope-reason: adds `test_backup_path_is_stored_as_written` and compares `backup_path` as a string
- `tests/config/test_app_config.py::*` — scope-reason: the default `backup_path` assertion compares a string
- `tests/cli/test_memory_cli.py::*` — scope-reason: the backup and restore assertions compare `backup_path` as a string
- `tests/sync/test_memory_backup.py::*` — scope-reason: builds `MemoryBackupConfig` with string paths
- `tests/sync/test_memory_sync.py::*` — scope-reason: builds `MemoryBackupConfig` with string paths
- `tests/config/test_config_validation_corpus.py`
- `crates/gruntime/tests/fixtures/config_validation_corpus.json`
- `crates/gruntime/Cargo.toml`
- `crates/gruntime/src/lib.rs`
- `crates/gruntime/src/pyyaml.rs`
- `crates/gruntime/src/json.rs`
- `crates/gruntime/src/pystr.rs`
- `crates/gruntime/src/validation/mod.rs`
- `crates/gruntime/src/validation/schema.rs`
- `crates/gruntime/src/validation/interpret.rs`
- `crates/gruntime/src/validation/messages.rs`
- `crates/gruntime/src/validation/ports/mod.rs`
- `crates/gruntime/src/validation/ports/urls.rs`
- `crates/gruntime/src/validation/ports/ai.rs`
- `crates/gruntime/src/validation/ports/validation_detection.rs`
- `crates/gruntime/tests/validation.rs`
- `crates/gdaemon/Cargo.toml`
- `src/gobby/install/version_pins.py::MANAGED_BIN_VERSION_PINS`
- `Cargo.lock`

Release ownership (Constraints): gruntime is in gdaemon's release closure, so this
leaf bumps `gdaemon` by 0.0.1 from the executor's starting tip.

This leaf adds no route. It gives the native write leaves from 5.9 on a port of
`ConfigMutations._validate_value`, which validates one written key and returns the
canonical value Python stores (Config validation).

**The carrier.** `scripts/generate_runtime_config_contract.py` gains a second output,
`crates/gruntime/assets/runtime_config_validation.json`, rendered with
`json.dumps(carrier, ensure_ascii=False, indent=2)` and no `sort_keys`, so model
fields and dict defaults keep their declaration order. The existing contract render
is unchanged. The carrier holds:

- `pydantic` and `pydanticCore`: the versions it was generated with, 2.12.5 and
  2.41.5 at plan time.
- `specs`: every `CONFIG_REGISTRY` spec in registry order, including the three
  restricted keys. Each entry has `key` (the exact key or the pattern), `pattern`,
  `visibility`, `activation`, `secrecy`, `sourcePath`, `placeholders`,
  `referenceFields` (`config_structured_reference_fields`), `schema` (the core
  schema of `TypeAdapter(spec.annotation)`), and `fields`: an ordered array of
  `{name, secrecy, schema}` for a pattern's field specs.
- `candidate`: `DaemonConfig.__pydantic_core_schema__` (5.8 reads it).
- `functions`: the sorted qualified names of every validator function the
  schemas call. There were 80 at plan time, and 79 remain once this leaf's
  `backup_path` change removes pydantic's `path_validator`.

Each core schema is serialized by these rules:

1. `json-or-python` becomes its `json_schema` and `lax-or-strict` its
   `strict_schema`, the branches pydantic takes for JSON input in strict mode.
2. Drop `metadata`, `serialization`, `config`, `custom_init`, `root_model`,
   `post_init`, `generic_origin`, `cls_repr` and `computed_fields`. A `model` node
   keeps its config's `validate_by_alias` and `validate_by_name` as
   `validateByAlias` and `validateByName`.
3. `ref` and `schema_ref` become the class's qualified name without pydantic's
   `:<id>` suffix. The generator asserts the names stay unique.
4. `function` becomes the function's qualified name: for a bound classmethod, its
   owner's `module.qualname` plus the method name. `cls` becomes its qualified
   name, and enum `members` become their values.
5. A `default_factory` is called once and stored as `default`. Every `default`
   passes through `to_jsonable_python`.
6. `model-fields` `fields` becomes an ordered array of `{name, ...field}`.
7. Each `union` choice becomes `{label, schema}`. `label` is
   `SchemaValidator(choice).title`, the location segment pydantic writes for
   that choice: `literal['auto']` and `int`; `str`, `FeatureCandidateConfig` and
   `dict[str,any]`.

The generator fails on any node `type` or node key outside the set
`schema.rs` implements, so a new constraint kind cannot pass unchecked. That set
is the plan-time inventory:

- `any`, `bool`, `dataclass`, `dataclass-args`, `dataclass-field`, `default`,
  `definition-ref`, `definitions`, `dict`, `enum`, `float`, `function-after`,
  `function-before`, `int`, `list`, `literal`, `model`, `model-field`,
  `model-fields`, `nullable`, `str` and `union`;
- the constraints `int` and `float` `ge`, `gt` and `le`; `str` `min_length`,
  `pattern` and `strict`; and `dict` `strict`;
- `model-field` `validation_alias`, `serialization_alias` and
  `serialization_exclude`, and `model-fields` `extra_behavior` (30 models
  `forbid`, the rest `ignore`).

`test_checked_in_validation_carrier_matches_registry` regenerates the carrier and
asserts the checked-in bytes are equal, as `test_checked_in_contract_matches_registry`
does for the contract. Generating under a different `HOME` and `GOBBY_HOME` gave
identical bytes at plan time (once the `ref` ids are removed), so no default
depends on the environment; the freshness test keeps that true.

**`json.rs`** holds the JSON value Python's request parsing produces:

- `Json` is `Null`, `Bool`, `Int(String)`, `Float(f64)`, `Str`, `Array` or
  `Object(Vec<(String, Json)>)`.
- `parse(bytes)` follows `json.loads`:
  - objects keep key order; a repeated key keeps its first position and its last
    value;
  - an integer literal keeps its digits with any sign, so integers beyond 64 bits
    survive. `-0` becomes `0`;
  - any other number is a float parsed with `str::parse::<f64>`, which rounds
    like Python's `float()`. `1e400` becomes infinity, which `validate_value`
    then rejects as Python does.
- It parses the raw text through `serde_json::value::RawValue` recursion, so no
  new `serde_json` feature reaches gdaemon; gdaemon's build already enables
  `raw_value`, and gruntime's manifest names it. `NaN`, `Infinity`, `-Infinity`
  and escapes that decode to a lone surrogate are accepted by `json.loads` but
  not here. A body carrying one returns the documented 422 divergence each write
  leaf names, after 3.4 step 1's precedent.
- `dumps(&Json)` writes `json.dumps`'s defaults:
  - `", "` and `": "` separators;
  - `ensure_ascii`: `\"`, `\\`, `\n`, `\r`, `\t`, `\b` and `\f`, other code points
    below U+0020 and every non-ASCII code point as `\uXXXX`, with astral code
    points as surrogate pairs;
  - floats as `float.__repr__` writes them (`1.0`, `1.1805916207174113e+21`).
    5.5's `pyyaml.rs` computes that repr before `represent_float`'s YAML edits;
    this leaf splits that step out as a `pub(crate)` function both call.

**The interpreter** (`schema.rs` parses the carrier into node types once;
`interpret.rs` validates). It reproduces pydantic-core 2.41.5 validating JSON input
with `strict=True`. A call-level `strict` overrides a node's own `strict`, so the
one `str` `strict` node and the `dict` `strict: false` nodes are both strict here.

- `bool`, `int`, `str`, `list` and `dict` accept only their JSON type. `float`
  accepts a JSON integer or float and yields a float, so `1` becomes `1.0`.
- `int` and `float` check `ge`, `gt` and `le`. `Int` compares by sign, digit count
  and digits.
- `str` checks `min_length` in code points and `pattern` with the `regex` crate,
  which is the engine pydantic-core uses for both plan-time patterns
  (`^[^/\\]+$`).
- `literal` matches by JSON type and value. `enum` accepts a member value.
- `nullable` accepts `null`. `default` supplies its `default` for a missing field
  without validating it, as pydantic does.
- `model` and `dataclass` accept an object. Fields validate in declaration order,
  by alias or name as the node's flags allow. A missing field without a default
  is `missing`; an extra key is `extra_forbidden` under `forbid` and dropped under
  `ignore`.
- `dict` validates each key with `keys_schema` (location segment `[key]`) and each
  value under its key, and keeps input order.
- `definition-ref` resolves through the enclosing `definitions`.
- `function-before` runs the port on the raw input, then the inner schema;
  `function-after` runs the inner schema, then the port on its output.
- `union` uses smart mode: every choice is tried, and the success with the
  highest exactness wins (exact, then strict), ties going to the earlier choice.
  For an object, pydantic-core rates a `dict` choice above a model choice, so an
  object item of `str | FeatureCandidateConfig | dict` stays a plain dict: at plan
  time `{"provider": "claude", "model": "sonnet", "zzz": 1}` stored unchanged.
  When every choice fails, the errors of all choices are reported, each under its
  `label`.

The validated value dumps as `dump_python(mode="json")` does: a model or dataclass
becomes an object of all its fields in declaration order, without
`serialization_exclude` fields and keyed by field name, and enums become their
values.

**`memory_backup.backup_path` becomes `str`.** It is the only `Path`-typed config
field. Pydantic builds a `Path`, which is a `WindowsPath` on Windows, and stores
`str(path)`, so Python's stored bytes differ by platform: `a/b` is stored as
`a\b` on Windows, and gdaemon and the Python daemon both ship for Windows.

- `MemoryBackupConfig.backup_path` becomes `str` with default
  `".gobby/memories.jsonl"`, so every platform stores the string as written.
- `MemoryBackupManager.__init__` wraps it in `Path`, which resolves relative and
  absolute values as before.
- `backup_memories` and `restore_memories` pass `str(path)`.
- The tests that build the config with a `Path` or compare it to one switch to
  strings.

The change removes pydantic's `path_validator` from the carrier. The leaf
regenerates `crates/gcore/assets/config/runtime_config_contract.json` because it
edits a model under `src/gobby/config/`. The contract carries only the key's coarse
JSON type and default, `string` and `.gobby/memories.jsonl` for both `Path` and
`str`, so the regenerated bytes must equal the checked-in bytes. gcore therefore
does not change, and the schema route's output is unchanged.
Rejected: a Windows branch in the dump that reproduces `PureWindowsPath`'s
normalization, which keeps platform-dependent stored bytes for one key.

**`pystr.rs`** (5.6) gains the Python `str` operations the per-key ports call:

- `lower` is `str::to_lowercase`: the full lowercase mapping with final sigma,
  as `str.lower` does.
- `casefold` is `icu_casemap`'s `CaseMapper::fold_string`: full case folding, as
  `str.casefold` does (`ß` folds to `ss`).
- `repr` writes Python's `repr` of a string:
  - single quotes, unless the string contains `'` and no `"`;
  - a backslash, the chosen quote, tab, newline and carriage return written as
    `\\`, `\'` or `\"`, `\t`, `\n` and `\r`;
  - every other non-printable code point as `\xhh`, `\uhhhh` or `\Uhhhhhhhh`.

  A code point is non-printable when its general category is `Cc`, `Cf`, `Cs`,
  `Co`, `Cn`, `Zl`, `Zp` or `Zs`, except the space. `icu_properties` supplies the
  category.

gruntime's manifest adds `icu_casemap` 2.1.1 and `icu_properties` 2.1.2, both on
ICU4X's 2.1 line, which gdaemon already builds through `idna_adapter` with
`compiled_data` on. `icu_properties` is already locked. `icu_casemap` 2.1.1 and its
compiled data, `icu_casemap_data` 2.1.1, enter the lock as the plan's one named
exception to the dependency constraint (Constraints, Dependencies). Every other
dependency of `icu_casemap` (`icu_collections`, `icu_locale_core`,
`icu_properties`, `icu_provider`, `potential_utf`, `writeable` and `zerovec`) is
already locked. Rejected: `unicase`, the only locked case crate, which has no
folding API, and hand-porting Unicode's case-folding table, which nothing would
keep in step with the Unicode version.

Python's Unicode tables are version 15.1 on Python 3.13 and 16.0 on 3.14, Rust's
standard library uses 17.0, and ICU4X 2.1 carries its own data. A code point
assigned or changed after Unicode 15.1 can therefore validate differently, which is
a documented divergence; the corpus uses no such code point.

**Messages** (`messages.rs`). Each error carries pydantic's `type`, `loc` and the
`msg` pydantic-core 2.41.5 writes for JSON input. Constraint values render as
Python's `repr`. The module's `ERROR_TYPES` names every type it emits:

- `bool_type`, `int_type`, `float_type`, `string_type`, `list_type` ("Input
  should be a valid array"), `dict_type` and `model_type` ("Input should be an
  object"), `literal_error`, `enum`, `missing`, `extra_forbidden`;
- `greater_than`, `greater_than_equal`, `less_than_equal`, `string_too_short`
  (with pydantic's singular "character" at 1) and `string_pattern_mismatch`;
- `value_error` ("Value error, <message>"), `assertion_error` ("Assertion
  failed, <message>"), and each custom type a port raises.

`detail(errors)` ports `ConfigMutations._validation_detail`: each error is
`<loc joined by ".">: <msg>`, or the bare `msg` when `loc` is empty, joined by
`"; "`.

**`validate_value(key, value)`** in `validation/mod.rs` ports `_validate_value`:

1. Resolve the key over the carrier's `specs`, as `ConfigRegistry.resolve` does.
   A pattern spec with fields uses the schema of the matched `{field}`; a missing
   field fails with `Unsupported patterned configuration key '<key>'`.
2. Port `_require_json_domain`: an infinite float fails with
   `Invalid value for configuration key '<key>': non-finite floats are not valid JSON values`.
3. Validate and dump. A failure gives
   `Invalid value for configuration key '<key>': <detail>`.

Keys are canonical storage keys drawn from gcore's `SAFE_SEGMENT_BYTES`, so
Python's `{key!r}` is always `'<key>'`.

**Python, validation-messages site 1.** `ConfigMutations._validate_value` catches
`ValidationError` before `ValueError` and formats it with
`ConfigMutations._validation_detail(exc)`. `PydanticSerializationError` and other
`ValueError`s keep `str(exc)`. The `Invalid value for configuration key` prefix stays.

**Ports.** `ports/mod.rs` maps each qualified name to a Rust function. Loading the
carrier walks the schemas gruntime validates against and fails when one of them
names a function without a port. This leaf walks only the `specs` schemas and ports
the 16 functions they call. 5.8 adds the `candidate` walk and the remaining ports,
after which the check covers the whole `functions` list.

- `ports/urls.rs` ports `src/gobby/config/url_validation.py`'s
  `validate_endpoint_url` and `validate_optional_endpoint_url`. That includes the
  `urllib.parse.urlsplit` rules they read: unsafe-byte removal, scheme, netloc,
  hostname, username and password, and the `port` property. It backs
  `WebhookEndpointConfig.validate_url`, `HubConfig.validate_base_url`,
  `OpenAICompatibleAudioBindingConfig.validate_url` and
  `GenerationEndpointConfig.validate_api_base`.
- `ports/ai.rs`: `GenerationEndpointConfig.validate_endpoint` and
  `.strip_removed_vision_extract`; `ModelMetadataAlias.normalize_source_key` and
  `.validate_target`; `FeatureCandidateConfig.validate_candidate_label`,
  `.coerce_legacy_candidate` and `.normalize_reasoning_effort`.
- `ports/validation_detection.rs`: `ValidationCommandWrapper.validate_id` and
  `.validate_label`; `ValidationCommandMatcher.validate_id`, `.validate_label` and
  `.validate_bounded_inputs`.

A `ValueError` raised in Python becomes `value_error` with the same text.

**Corpus.** `crates/gruntime/tests/fixtures/config_validation_corpus.json` holds:

- `json`: request texts with their `json.dumps(json.loads(text))` output, or
  `null` where `parse` rejects them;
- `errorTypes`: the `ERROR_TYPES` list;
- `perKey`: cases of `{key, value, validators, stored, message}`. `stored` is
  `json.dumps` of `_validate_value`'s result and `message` is the
  `ConfigValidationError` text; exactly one is set. `validators` names the
  functions the case exercises.

The cases include:

- an integer for a float field (`1.0`), `5.0` for an int field, an integer beyond
  64 bits, `1e400`;
- non-ASCII and control-character strings, which test `ensure_ascii`;
- values that exercise `pystr`: `ß` and `İ` under `casefold` and `lower`, a final
  sigma, and a rejected candidate label whose `repr` holds both quote kinds, a
  control character and an astral code point;
- a model-valued key with omitted fields, whose dump fills defaults in field
  order;
- a dict default whose order is not sorted;
- one failing case per union shape;
- one case per constraint kind;
- an accepting and a rejecting case for every per-key function.

The Python test asserts each case against `_validate_value` and each `json` entry
against `json.loads` and `json.dumps`. It also asserts that the failing cases
together raise every type in `errorTypes`, and that every function the per-key
schemas call has an accepting and a rejecting case. The Rust test asserts byte
equality for every case and every `json` entry, and that `ERROR_TYPES` equals
`errorTypes`.

Research context:
- `src/gobby/storage/config_mutations.py`: `ConfigMutations._validate_value`
  validates with `TypeAdapter(annotation).validate_json(to_json(value), strict=True)`
  and returns `dump_python(..., mode="json")`. Every registry annotation is bare:
  `Field` constraints sit in model field metadata, so only the candidate (5.8)
  enforces them. Also `_validation_detail` and `_require_json_domain`.
- `ConfigMutations._upsert` stores `json.dumps(to_jsonable_python(value))` in
  `config_store.value` (text).
- Plan-time probes:
  - per-key schemas serialize to 64.5 KB, `DaemonConfig` to 146 KB;
  - 80 distinct functions, 17 from per-key schemas, before this leaf removes
    `path_validator`;
  - `memory_backup.backup_path` is the only `Path`-typed field, and
    `MemoryBackupManager` (`src/gobby/sync/memories.py`) is its only runtime
    reader;
  - two union shapes;
  - one `validation_alias` (`DaemonConfig.gobby_tasks`, `gobby-tasks`);
  - four `serialization_exclude` fields, all on `DaemonConfig`, which is never
    dumped for storage.
- In JSON mode pydantic writes "Input should be a valid array" and "Input should
  be an object" where Python input gives "valid list" and "valid dictionary".
- gdaemon's `serde_json` features are `default`, `float_roundtrip`, `raw_value`
  and `std`. `preserve_order` or `arbitrary_precision` would change every
  `serde_json` user in gdaemon through feature unification.
- Existing assertions that survive the message change:
  `tests/config/test_daemon_sandbox.py` matches pydantic's `msg` text, and
  `tests/storage/test_revisioned_config_store.py` matches the `Invalid value`
  prefix.
- Planned checks:
  - `cargo nextest run -p gobby-runtime --test validation`;
  - `DATABASE_URL=… GOBBY_TEST_PROTECT=1 uv run pytest tests/config/test_config_validation_corpus.py tests/config/test_runtime_config_contract.py tests/storage/test_revisioned_config_store.py tests/config/test_daemon_sandbox.py tests/config/test_persistence.py tests/config/test_app_config.py tests/cli/test_memory_cli.py tests/sync/test_memory_backup.py tests/sync/test_memory_sync.py -v`.

Consumers unchanged:
- `src/gobby/cli/install_setup_gclient.py` — no-edit-reason: reads only the `gclient` pin, which no leaf of this plan bumps.
- `src/gobby/cli/install_setup_gcode.py` — no-edit-reason: reads the `gcode` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_ghook.py` — no-edit-reason: reads the `ghook` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_gterm.py` — no-edit-reason: reads only the `gterm` pin, which no leaf of this plan bumps.
- `src/gobby/cli/install_setup_versions.py` — no-edit-reason: looks a pin up by the binary name it is given.
- `src/gobby/code_index/gcode_gateway.py` — no-edit-reason: derives `MIN_GCODE_GRAPH_VERSION` from the `gcode` pin at import.
- `src/gobby/hooks/runtime_compat.py` — no-edit-reason: derives `MINIMUM_GHOOK_VERSION_FOR_SUPPORTED_SCHEMA` from the `ghook` pin at import.
- `src/gobby/install/bin_freshness_models.py` — no-edit-reason: iterates every pin, so the bumped values flow through.
- `src/gobby/install/distribution.py` — no-edit-reason: looks a pin up by binary name.
- `tests/cli/installers/test_grok_installer.py` — no-edit-reason: reads the `ghook` pin instead of a version literal.
- `tests/cli/test_install_ghook.py` — no-edit-reason: builds its fixtures from the `ghook` pin instead of a version literal.
- `tests/cli/test_install_setup.py` — no-edit-reason: reads the `gcode` pin instead of a version literal.
- `tests/cli/test_install_setup_gclient.py` — no-edit-reason: reads the `gclient` pin instead of a version literal.
- `tests/cli/test_install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin instead of a version literal.
- `tests/cli/test_install_setup_gterm.py` — no-edit-reason: reads the `gterm` and `gclient` pins instead of version literals.
- `tests/code_index/test_gcode_gateway.py` — no-edit-reason: reads the `gcode` pin instead of a version literal.
- `tests/hooks/test_runtime_compat.py` — no-edit-reason: compares the derived floor to the `ghook` pin instead of a version literal.
- `tests/install/test_distribution.py` — no-edit-reason: reads pins by binary name; its `0.1.0` literal is a fake `gcode` output, not a pin.
- `tests/install/test_version_pins.py` — no-edit-reason: compares each pin to its crate manifest, which is this leaf's bump check.
- `src/gobby/config/app.py` — no-edit-reason: declares `DaemonConfig.memory_backup` as `MemoryBackupConfig`, which the `backup_path` type change leaves unchanged.
- `src/gobby/runner.py` — no-edit-reason: names `MemoryBackupManager` only in a type annotation.
- `tests/sync/test_local_backup_paths.py` — no-edit-reason: builds `MemoryBackupConfig()` with its default, a relative path the manager resolves as before.

**Acceptance:**

- 5.7.1 - The checked-in validation carrier equals the generator's output, and the generator fails on a node type, node key or union shape the interpreter does not implement. test: `tests/config/test_runtime_config_contract.py::test_checked_in_validation_carrier_matches_registry`.
- 5.7.2 - Every per-key corpus case matches `_validate_value`: stored bytes, or a message in the `<loc>: <msg>` form. test: `tests/config/test_config_validation_corpus.py::test_per_key_corpus_matches_python`.
- 5.7.3 - The corpus raises every type in `errorTypes`, and every per-key function has an accepting and a rejecting case. test: `tests/config/test_config_validation_corpus.py::test_corpus_covers_error_types_and_functions`.
- 5.7.4 - Native `validate_value` gives byte-equal stored values and messages for every per-key case, and `ERROR_TYPES` equals `errorTypes`. test: `crates/gruntime/tests/validation.rs::per_key_corpus_matches_python`.
- 5.7.5 - Every function the per-key schemas name has a port, and a carrier whose `specs` schemas name an unported function fails to load. test: `crates/gruntime/tests/validation.rs::per_key_functions_have_ports`.
- 5.7.6 - `json.rs` parses and dumps every `json` entry as Python does: key order, repeated keys, big integers, float repr and `ensure_ascii`; it rejects `NaN`, `Infinity` and lone surrogates. test: `crates/gruntime/tests/validation.rs::json_tree_matches_python_vectors`.
- 5.7.7 - `gdaemon` is one patch version above the executor's starting tip in its `Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`, so this leaf's cutover passes the release-identity preflight. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.
- 5.7.8 - `memory_backup.backup_path` validates as a string and is stored as written on every platform, and `MemoryBackupManager` resolves relative and absolute values as before. test: `tests/config/test_persistence.py::test_backup_path_is_stored_as_written`.
- 5.7.9 - The regenerated gcore contract equals the checked-in bytes, so this leaf leaves gcore unchanged. test: `tests/config/test_runtime_config_contract.py::test_checked_in_contract_matches_registry`.

### 5.8 Complete-candidate config validation [category: code] (depends: 5.7)
`kind: deliverable`

Targets:
- `scripts/generate_runtime_config_contract.py`
- `crates/gruntime/assets/runtime_config_validation.json`
- `crates/gcore/assets/config/runtime_config_contract.json::*` — scope-reason: regenerated after this leaf's model edits, which must leave its bytes unchanged
- `pyproject.toml`
- `uv.lock`
- `src/gobby/config/features.py::ChatConfig.validate_default_mode`
- `src/gobby/config/skills.py::SkillsConfig.validate_injection_format`
- `src/gobby/config/feature_base.py::validate_feature_candidates`
- `src/gobby/config/code_index.py::CodeIndexConfig.validate_nightly_repair_timezone`
- `src/gobby/storage/config_mutations.py::ConfigMutations._validate_candidate`
- `tests/config/test_candidate_validation_corpus.py`
- `crates/gruntime/tests/fixtures/candidate_validation_corpus.json`
- `crates/gruntime/src/pystr.rs`
- `crates/gruntime/src/validation/mod.rs`
- `crates/gruntime/src/validation/schema.rs`
- `crates/gruntime/src/validation/interpret.rs`
- `crates/gruntime/src/validation/candidate.rs`
- `crates/gruntime/src/validation/ports/mod.rs`
- `crates/gruntime/src/validation/ports/ai.rs`
- `crates/gruntime/src/validation/ports/persistence.rs`
- `crates/gruntime/src/validation/ports/app.rs`
- `crates/gruntime/src/validation/ports/features.rs`
- `crates/gruntime/src/validation/ports/schedules.rs`
- `crates/gruntime/src/validation/ports/services.rs`
- `crates/gruntime/src/validation/ports/cron.rs`
- `crates/gruntime/tests/candidate.rs`
- `crates/gdaemon/Cargo.toml`
- `src/gobby/install/version_pins.py::MANAGED_BIN_VERSION_PINS`
- `Cargo.lock`

Release ownership (Constraints): gruntime is in gdaemon's release closure, so this
leaf bumps `gdaemon` by 0.0.1 from the executor's starting tip. The regenerated gcore
contract keeps its bytes, so gcore does not change.

This leaf adds no route. It gives the native write leaves from 5.9 on a port of
`ConfigMutations._validate_candidate`, which checks a whole prospective config
before a write commits (Config validation).

**`validate_candidate(patch, snapshot, secrets, allow_internal)`** in
`validation/candidate.rs` ports `_validate_candidate`. Its inputs mirror Python's:

- `snapshot.overrides`: the stored `(key, value)` pairs in the order of
  `SELECT key, value, is_secret, revision FROM config_store ORDER BY key`, which
  the write leaves read under their lock;
- `snapshot.bindings`: each reference key's `(reference, plaintext)`;
- `patch`: `values` and `secrets` in request order, and `unset`;
- `secrets`: a reader for one secret by name, which the write leaves back with
  3.3's `secret(name)`.

It returns `Ok { values, references }`, `Invalid { message, key }` (a
`ConfigValidationError`), or `Internal`, the failures Python raises as
`RuntimeError`, which each write leaf maps to its route's body.

1. Copy `overrides` and `bindings`. Every key resolves as `ConfigMutations._resolve`
   does: an unknown key is `Invalid` `Unknown configuration key: <key>`, and an
   ambiguous one raises `RegistryError`, which step 7 handles.
2. For each `unset` key, in sorted order: resolve, authorize, then drop it from both
   copies. Python's loop moves from the `frozenset`'s hash order, which changes
   between processes, to `sorted(patch.unset)`, so the reported key is the same on
   both sides when several unset keys fail.
   Authorization ports `ConfigMutations._authorize`. Unless `allow_internal` is
   set, a restricted key is `Configuration key '<key>' is restricted`, and a managed
   key is `Configuration key '<key>' requires managed activation`.
3. For each `values` entry: resolve, authorize, then run 5.7's `validate_value`.
   For a reference key (secrecy `reference`, per field for a pattern):
   - port `ConfigMutations._validate_reference`: a value that is not a string
     starting with `$secret:` is
     `Secret configuration key '<key>' requires a secret update or reference`, and
     an empty name is `Secret reference for '<key>' is empty`;
   - when the key has no binding or its binding names another reference, read the
     name through `secrets` after `SecretStore.get`'s normalization (`pystr::strip`,
     then `pystr::lower`). No secret is `Secret reference for '<key>' cannot be resolved`;
     otherwise the plaintext becomes the key's binding.

   A key with structured reference fields ports
   `src/gobby/config/voice_secrets.py::validate_structured_references`: a value that
   is not a list is `<key> must be a list`, and an object item whose field is neither
   `null`, `""` nor a full match of the `SECRET_REF_PATTERN` 5.2 scans for is
   `<key>[<i>].<field> must use a $secret:NAME reference; plaintext values cannot be persisted`.
   The validated value is recorded in `values` and assigned in the candidate, where
   an existing key keeps its position and a new key goes last.
4. For each `secrets` entry: resolve and authorize. A key that is not a reference
   key is `Configuration key '<key>' is not a secret`, and a plaintext that is not a
   string is `Secret value for '<key>' must be a string`. The name is the update's
   name or `config_key_to_secret_name(key)`, and the reference `$secret:<name>` must
   pass `_validate_reference`, which with no explicit name also requires the
   canonical name: `Secret reference for '<key>' is not canonical`. The reference is
   recorded and assigned, and the plaintext becomes the binding.
5. Assemble the candidate as `ConfigRepository.runtime_candidate` does. Walk the
   candidate pairs in order and skip specs without a `sourcePath`. A reference key
   whose value is a `$secret:` string takes its binding's plaintext; a missing
   binding or one with another reference is `Internal`, as Python's
   `ConfigRepositoryError` and `TornConfigSnapshotError` are. The path is the
   `sourcePath` segments followed by the key's matched placeholders, and the value
   is assigned as `_assign_path` does: a missing or non-object segment becomes a new
   object.
6. Validate the tree against the carrier's `candidate` schema with 5.7's
   interpreter.
7. Map failures as `_validate_candidate` does:
   - A validation failure gives `detail` (5.7) and an error key: the first error
     whose `loc`, joined by `.` with list indexes as digits, resolves to a registry
     key, else the only changed key when the patch changes exactly one. When that
     key is a reference key, the result is `Secret configuration value is invalid`
     for that key. Otherwise it is
     `Complete configuration candidate is invalid: <detail>`.
   - An ambiguous key in steps 2 to 4 is the same prefix followed by
     `Ambiguous configuration key: <key>`, keyed to the only changed key.
   - An ambiguous key met while choosing the error key escapes Python's handler, so
     it is `Internal`.

**`config_key_to_secret_name`** ports the Python function:
`databases.falkordb.password` is `falkordb_password`, `ai.embeddings.api_key` is
`embeddings_api_key`, and any other key is `config_` plus the lowercased key with
each run outside `[a-z0-9_]` replaced by `_` and `_` stripped from both ends, cut
to 184 characters, then `_` and the first eight hex digits of the key's SHA-256.

**The candidate schema.** `schema.rs` now parses the carrier's `candidate` member,
and loading walks it, so every name in `functions` must have a port. The carrier
also gains `tzdata` (the package version) and `timezones` (below).

**Ports.** Each Python `ValueError` becomes `value_error` with the same text, and
the ports reuse 5.7's `urls.rs` and `pystr.rs`. `pystr.rs` adds `repr` of a JSON
value as Python writes the matching object: `None`, `True`, `False`, integer
digits, 5.7's float repr, `[a, b]` and `{'k': v}` in key order.

- `ports/ai.rs` adds `AIConfig.validate_model_metadata_aliases`,
  `GenerationConfig.validate_endpoint_names`, `.validate_profile_defaults`,
  `.reject_removed_local_config` and `.clamp_candidate_timeouts`, and
  `FeatureDefaultConfig.populate_and_validate_candidates`.
  - `validate_feature_candidates` validates each item against
    `FeatureCandidateConfig`'s schema from the carrier. A failure whose message
    contains `removed local: selector` propagates: its errors are re-located
    under the enclosing location with the item's field (no list index), so
    `{"candidate": "local:x/y"}` under `chat` fails at `chat.candidate`. Other
    failing items are collected, and any collected item fails with
    `feature candidates must use provider/model format: <reprs joined by ", ">`.
  - `populate_and_validate_candidates` validates only explicit candidates. The
    static defaults it also validates are constants that pass. Its omitted-candidate
    bookkeeping, the deduplication and `DaemonConfig.apply_generation_profile_defaults`
    only rewrite candidate lists that no later validator reads, and
    `_validate_candidate` discards the validated config. They get identity ports,
    as does `clamp_candidate_timeouts`, which only lowers two timeouts no later
    validator reads and logs.
- `ports/persistence.rs`: `EmbeddingsConfig.validate_api_base` and
  `.validate_dim`; `FalkorConfig.validate_password` and `.validate_score`;
  `MemoryConfig.validate_backend`, `.validate_cluster_expansion`,
  `.validate_cluster_min_cluster_size`, `.validate_cluster_min_samples`,
  `.validate_edge_half_life`, `.validate_half_life`, `.validate_positive_links`,
  `.validate_probability` and `.reject_removed_keys`;
  `MemoryDreamConfig.validate_confidence`, `.validate_positive_float` and
  `.validate_positive_int`; `QdrantConfig.validate_url`.
- `ports/app.rs`: `DaemonConfig.validate_health_check_interval`,
  `.validate_hook_timeout_order`, `.validate_port`,
  `.reject_removed_session_title_config` and `.apply_generation_profile_defaults`.
  `validate_hook_timeout_order` reads `workflow.timeout` after
  `WorkflowConfig.validate_timeout` has turned `0` into 24.0, because a
  `function-after` port's output replaces the field value. The constants it renders
  with `:g` are 20.0 and 30.0.
- `ports/features.rs`: `ChatConfig.cap_attachment_total_by_file_product` and
  `.validate_default_mode`; `MetricsConfig.validate_list_limit`;
  `ToolResultOffloadConfig.validate_size_relationships`;
  `VoiceConfig.validate_openai_compatible_audio_provider_ids`;
  `CommunicationsConfig.validate_webhook_base_url`.
- `ports/schedules.rs`: `CronConfig.validate_check_interval`,
  `.validate_max_concurrent`, `.validate_running_timeout` and
  `.validate_stale_run_grace`; `CodeIndexConfig.validate_nightly_repair_cron` and
  `.validate_nightly_repair_timezone`; `AutomationLoopConfig.validate_interval`;
  `BinFreshnessConfig.validate_non_negative_seconds` and
  `.validate_positive_seconds`.
- `ports/services.rs`: `MCPClientProxyConfig.validate_connect_timeout`,
  `.validate_min_similarity`, `.validate_timeout` and `.validate_top_k`;
  `WebSocketSettings.validate_port` and `.validate_positive`;
  `ContextHandoffConfig.validate_threshold_order`;
  `MessageTrackingConfig.validate_positive`; `SessionLifecycleConfig.validate_positive`;
  `TaskValidationConfig.validate_escalation_webhook_url` and
  `.validate_positive_int`; `WorkflowConfig.validate_timeout`;
  `LoggingSettings.validate_positive` and `.validate_positive_float`;
  `UIConfig.validate_mode` and `.validate_port`;
  `TelemetrySettings.validate_sample_rate`;
  `DatabaseConcurrencyConfig.validate_concurrency_value`;
  `HookTimeoutConfig.normalize_additional_context_limits`, which strips and
  casefolds each provider key with `pystr`.
- `ports/cron.rs` ports the validity half of croniter 6.0.0's `croniter.expand`,
  which `croniter.is_valid` runs: field counts 5, 6 and 7; the `@yearly`,
  `@annually`, `@monthly`, `@weekly`, `@daily`, `@midnight` and `@hourly` aliases;
  lists, ranges and `*/n` steps with `n` of at least 1; month and day names in any
  case; `L`, `#` and `?` where croniter accepts them; each field's bounds, with day
  of week 7 accepted; and whitespace splitting. `@reboot`, `H` hashing without a
  hash id and `W` are rejected, as croniter rejects them.

That is 62 ports here and 16 in 5.7, the 78 names in `functions`.

**Python changes.**

- `ChatConfig.validate_default_mode` renders its choices sorted, as
  `MemoryConfig.validate_backend` does. It rendered a Python set, so the message
  order changed with each process's string hashing.
- `SkillsConfig.validate_injection_format` is deleted. Its field is
  `Literal["summary", "full", "none"]`, which rejects every value the validator
  would.
- `validate_feature_candidates` catches `ValidationError` and re-raises only when
  one of its errors' `msg` contains `removed local: selector`. It tested
  `str(exc)`, which includes pydantic's truncated input display: at plan time
  `{"candidate": "a/b", "x": "removed local: selector"}` re-raised as
  `extra_forbidden`, while the same text inside a longer value was truncated away.
- `CodeIndexConfig.validate_nightly_repair_timezone` accepts exactly the names in
  the `tzdata` package's `zones` resource, read once; the message is unchanged.
  `pyproject.toml` lists `tzdata` for every platform, widening its lock entry from
  the `win32` marker (2026.1 at plan time, 598 names including links such as
  `US/Pacific`). The generator writes the same list to the carrier. At run time
  `ZoneInfo` loads every listed name on every host: it reads the system zone files
  first, and the package is now always installed. Rejected: porting `ZoneInfo`'s
  lookup, because on Windows Python reads the package from its own environment,
  which gdaemon cannot locate, and on macOS the case-insensitive filesystem accepts
  `utc`; and `jiff`, whose database release and case-insensitive lookup differ.

**Lax and strict.** Python validates the candidate in lax Python mode and Rust in
strict JSON mode. Their outcomes agree on every candidate this leaf assembles:

- each stored value passed 5.7's strict per-key validation of the same annotation
  when written (`ConfigMutations._upsert` is the only writer), and its JSON dump
  revalidates identically in both modes;
- each secret plaintext is a string;
- `FeatureCandidateConfig`'s fields are strings, so its lax Python validation
  inside `validate_feature_candidates` matches strict JSON.

The corpus test asserts that agreement for every tree case.

**Corpus.** `crates/gruntime/tests/fixtures/candidate_validation_corpus.json` holds:

- `trees`: `{tree, validators, detail}`, where `detail` is 5.7's detail of
  `DaemonConfig.model_validate(tree)`'s errors, or `null` when the tree is valid;
- `mutations`: `{overrides, bindings, secrets, patch, allowInternal, outcome}`,
  where `outcome` is `ok` with `values` and `references`, `invalid` with `message`
  and `key`, or `internal`;
- `secretNames`: `[key, name]` pairs, including both special keys, a dynamic key
  with encoded segments and a key long enough to cut.

The cases include:

- an accepting and a rejecting tree for every port that can raise, and a tree
  showing the effect of every port whose output a later validator reads;
- the five `validate_feature_candidates` cases: the input-value and truncated
  texts, which both become format errors after the fix; a `local:` label; a label
  without `/` that contains the text; and a `local:` label with a bad
  `reasoning_effort`, which re-locates two errors;
- one cron tree per family above and per rejected form, and timezone trees for a
  canonical name, a link name, `utc`, a path and an empty string;
- a mutation per message in steps 1 to 4, per failure kind in step 7 and per
  `Internal` cause, plus accepted mutations with a new secret, a changed reference
  and an unchanged binding.

The Python test asserts each tree's `detail` from `model_validate` and that
`model_validate_json(json.dumps(tree), strict=True)` gives the same detail, each
mutation against `ConfigMutations._validate_candidate` with the real registry and
a fake secret store, each cron tree against `croniter.is_valid` (so a croniter
upgrade shows up as a fixture mismatch), and each `secretNames` pair. The Rust test
asserts byte-equal results for every case.

Research context:
- `src/gobby/storage/config_mutations.py`: `ConfigMutations._validate_candidate`,
  `._authorize`, `._validate_reference`, `._resolve`, `._is_reference_key`,
  `._validation_error_key` and `config_key_to_secret_name`; `RegistryError` is a
  `ValueError` and `UnknownConfigKeyError` a `KeyError`
  (`src/gobby/config/registry.py`).
- `src/gobby/storage/config_repository.py`: `ConfigRepository.runtime_candidate`,
  `._runtime_path`, `._secret_reference` and `_assign_path`.
  `runtime_candidate`'s returned `DaemonConfig` is discarded by its only caller.
- Plan-time probes: 63 functions are reached only from the candidate schema;
  pydantic re-locates a `ValidationError` raised inside a validator under the
  validator's location; `FeatureDefaultConfig.profile` is the `FeatureProfile`
  enum, so `FeatureProfile(profile)` cannot raise; `normalize_feature_candidate`
  never raises.
- `croniter` is 6.0.0 in `uv.lock`; `is_valid` returns `False` when `expand` raises
  `CroniterError`.
- gdaemon and the Python daemon both ship for Windows
  (`.github/workflows/release-gdaemon.yml`,
  `src/gobby/cli/installers/service_windows.py`).
- Planned checks:
  - `cargo nextest run -p gobby-runtime --test candidate --test validation`;
  - `DATABASE_URL=… GOBBY_TEST_PROTECT=1 uv run pytest tests/config/test_candidate_validation_corpus.py tests/config/test_runtime_config_contract.py tests/config/test_app_config.py -v`.

Consumers unchanged:
- `src/gobby/cli/install_setup_gclient.py` — no-edit-reason: reads only the `gclient` pin, which no leaf of this plan bumps.
- `src/gobby/cli/install_setup_gcode.py` — no-edit-reason: reads the `gcode` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_ghook.py` — no-edit-reason: reads the `ghook` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_gterm.py` — no-edit-reason: reads only the `gterm` pin, which no leaf of this plan bumps.
- `src/gobby/cli/install_setup_versions.py` — no-edit-reason: looks a pin up by the binary name it is given.
- `src/gobby/code_index/gcode_gateway.py` — no-edit-reason: derives `MIN_GCODE_GRAPH_VERSION` from the `gcode` pin at import.
- `src/gobby/hooks/runtime_compat.py` — no-edit-reason: derives `MINIMUM_GHOOK_VERSION_FOR_SUPPORTED_SCHEMA` from the `ghook` pin at import.
- `src/gobby/install/bin_freshness_models.py` — no-edit-reason: iterates every pin, so the bumped values flow through.
- `src/gobby/install/distribution.py` — no-edit-reason: looks a pin up by binary name.
- `tests/cli/installers/test_grok_installer.py` — no-edit-reason: reads the `ghook` pin instead of a version literal.
- `tests/cli/test_install_ghook.py` — no-edit-reason: builds its fixtures from the `ghook` pin instead of a version literal.
- `tests/cli/test_install_setup.py` — no-edit-reason: reads the `gcode` pin instead of a version literal.
- `tests/cli/test_install_setup_gclient.py` — no-edit-reason: reads the `gclient` pin instead of a version literal.
- `tests/cli/test_install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin instead of a version literal.
- `tests/cli/test_install_setup_gterm.py` — no-edit-reason: reads the `gterm` and `gclient` pins instead of version literals.
- `tests/code_index/test_gcode_gateway.py` — no-edit-reason: reads the `gcode` pin instead of a version literal.
- `tests/hooks/test_runtime_compat.py` — no-edit-reason: compares the derived floor to the `ghook` pin instead of a version literal.
- `tests/install/test_distribution.py` — no-edit-reason: reads pins by binary name; its `0.1.0` literal is a fake `gcode` output, not a pin.
- `tests/install/test_version_pins.py` — no-edit-reason: compares each pin to its crate manifest, which is this leaf's bump check.
- `src/gobby/config/ai.py` — no-edit-reason: `GenerationConfig.validate_profile_defaults` calls `validate_feature_candidates` and propagates its errors, which the re-raise change does not alter for its callers.
- `src/gobby/config/app.py` — no-edit-reason: `DaemonConfig.apply_generation_profile_defaults` calls `validate_feature_candidates` on lists `validate_profile_defaults` already accepted.

**Acceptance:**

- 5.8.1 - Every tree case's `detail` matches `DaemonConfig.model_validate`, and strict JSON validation of the same tree gives the same detail. test: `tests/config/test_candidate_validation_corpus.py::test_trees_match_lax_and_strict_python`.
- 5.8.2 - Every mutation case matches `ConfigMutations._validate_candidate`'s accepted values and references, its `ConfigValidationError` message and key, or its runtime failure, and every `secretNames` pair matches `config_key_to_secret_name`. test: `tests/config/test_candidate_validation_corpus.py::test_mutations_match_validate_candidate`.
- 5.8.3 - The corpus has an accepting and a rejecting tree for every raising port, a tree for every port whose output a later validator reads, every cron family and rejected form checked against `croniter.is_valid`, and every listed failure kind. test: `tests/config/test_candidate_validation_corpus.py::test_corpus_covers_ports_and_failures`.
- 5.8.4 - The default-mode message lists sorted choices, `validate_injection_format` is gone, the `local:` re-raise reads only error messages, and the timezone check accepts exactly the `tzdata` zone names. test: `tests/config/test_candidate_validation_corpus.py::test_python_validator_changes`.
- 5.8.5 - Native candidate validation gives each tree case's detail byte for byte. test: `crates/gruntime/tests/candidate.rs::trees_match_python`.
- 5.8.6 - Native `validate_candidate` gives each mutation case's outcome and each `secretNames` pair. test: `crates/gruntime/tests/candidate.rs::mutations_match_python`.
- 5.8.7 - Every name in the carrier's `functions` has a port, and a carrier naming an unported function fails to load. test: `crates/gruntime/tests/candidate.rs::all_functions_have_ports`.
- 5.8.8 - The regenerated gcore contract equals the checked-in bytes, so this leaf leaves gcore unchanged. test: `tests/config/test_runtime_config_contract.py::test_checked_in_contract_matches_registry`.
- 5.8.9 - `gdaemon` is one patch version above the executor's starting tip in its `Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`, so this leaf's cutover passes the release-identity preflight. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 5.9 Native `PATCH /api/config/values` [category: code] (depends: 5.8)
`kind: deliverable`

Targets:
- `scripts/generate_runtime_config_contract.py`
- `crates/gruntime/assets/runtime_config_validation.json`
- `crates/gcore/assets/config/runtime_config_contract.json::*` — scope-reason: regenerated after this leaf's `values.py` edit, which must leave its bytes unchanged
- `src/gobby/config/values.py::ConfigValuesService.patch_flat`
- `src/gobby/servers/routes/configuration_values.py::register_value_routes`
- `tests/servers/routes/test_config_values_api.py::test_revision_domain_and_exhaustion_contract`
- `tests/servers/routes/test_values_patch_corpus.py`
- `crates/gruntime/tests/fixtures/values_patch_corpus.json`
- `crates/gruntime/src/lib.rs`
- `crates/gruntime/src/json.rs`
- `crates/gruntime/src/secrets.rs`
- `crates/gruntime/src/secret_routes.rs`
- `crates/gruntime/src/validation/mod.rs`
- `crates/gruntime/src/validation/candidate.rs`
- `crates/gruntime/src/config_write.rs`
- `crates/gruntime/src/write_rules.rs`
- `crates/gruntime/src/values_patch.rs`
- `crates/gruntime/src/verify.rs`
- `crates/gruntime/tests/values_patch.rs`
- `crates/gdaemon/Cargo.toml`
- `src/gobby/install/version_pins.py::MANAGED_BIN_VERSION_PINS`
- `Cargo.lock`

Release ownership (Constraints): gruntime is in gdaemon's release closure, so this
leaf bumps `gdaemon` by 0.0.1 from the executor's starting tip. The regenerated gcore
contract keeps its bytes, so gcore does not change.

Row: `PATCH /api/config/values` for `Operator`. Compare mode proxies it once and
runs `check_proxied` (D3).

This leaf also lands the commit core every native config write uses.
`config_write.rs` ports `ConfigMutations._apply` and the write wait, and
`write_rules.rs` holds the request rules value and document writes share: masked
structured-reference restoration and the generation-endpoint probe-evidence rules.
5.10 reuses the commit core, 5.12 reuses both, and 5.14 reaches both through this
leaf's `patch_flat`.

**Request body.** Python's route stops taking `PatchConfigRequest` as a FastAPI body
parameter and parses the body itself, as 5.4's prompt save does, so both
implementations answer a malformed body alike:

1. Decode the body as strict UTF-8, parse it with `json.loads` and validate it
   with `PatchConfigRequest.model_validate`. The body is an object with a required
   `expected_revision`, a strict integer from 0 to 2^53 − 1 (`ConfigRevision`; a
   boolean or a float fails), an optional `values` object and an optional `unset`
   array of strings, whose repeats collapse. Any other member fails.
2. Any failure returns 422 `{"detail": "invalid config patch request"}`. FastAPI's
   validation list, which echoes the input, no longer appears. The parse runs
   before the route's `try`, whose `except Exception` would otherwise answer 500.

Native parses with 5.7's `json.rs` and applies the same rules. A body carrying `NaN`,
`Infinity` or a lone surrogate gets this 422 natively, while Python decodes it and
answers per value: the documented divergence (Config validation).

**Flatten** ports `ConfigValuesService._flatten_values`. It walks `values` in request
order and joins segments with `.`:

1. When a pattern puts a dynamic segment right after the current prefix, the
   segment must decode. That test ports `ConfigRegistry.dynamic_segment_follows`
   and `_pattern_prefix_matches`, which also decodes each placeholder segment of the
   prefix. A failure is 422 `validation_error`
   `Dynamic configuration segment is not canonically encoded: <reason>` at path
   `["values", ...prefix, segment]`.
2. Resolve the joined key with 5.8's resolver over the carrier's `specs`, which
   include the restricted keys. The gcore contract omits them, and Python resolves
   them here and then reports them as not public. A known key records its value
   and its segment path. An unknown key whose value is an object is walked; any
   other unknown key is 422 `validation_error` `Unknown configuration key` at its
   path. An ambiguous key returns the route's 500.

`<reason>` is the `ValueError` text of
`registry_key_encoding.decode_dynamic_segment`:

- `Dynamic config segment must not be empty`;
- `Truncated percent escape in dynamic config segment`;
- `Percent escapes must use uppercase hexadecimal digits`;
- `Dynamic config segment is not canonically encoded`;
- `Dynamic config segment is not valid UTF-8`;
- `Dynamic config segment uses a noncanonical escape`.

`values_patch.rs` ports that function over code points, as Python scans. gcore's
`decode_dynamic_segment` scans bytes, so `%é` is a hexadecimal error there and a
truncated escape in Python, and it reports both the unsafe-character case and the
noncanonical-escape case as `NonCanonical`. gcore stays unchanged.

**Prepare** ports `ConfigValuesService.patch_flat` with `probe_verified` false:

1. For each flat key in order, authorize as `ConfigValuesService._authorize` does:
   - unknown: 422 `validation_error` `Unknown configuration key`;
   - visibility other than `public`: 422 `validation_error`
     `Configuration key is not public`;
   - managed: 422 `managed_activation_required`
     `Configuration key requires managed activation` with `action`
     `/api/embeddings/switch/start`.

   Each error's path is `["values", ...path]`.
2. A reference key (5.8's secrecy, per field for a pattern) skips the value
   `"********"`. A non-string value is 422 `validation_error`
   `Secret configuration value must be a string`, and a string starting with
   `$secret:` is 422 `validation_error`
   `Secret references are not accepted as plaintext values`. A plaintext becomes a
   secret update with category `general`.
3. A key with structured reference fields goes through
   `write_rules::prepare_structured`. Any other key becomes a value update.
4. Authorize each `unset` key, in sorted order.
5. Read the published row with 3.1's `active_state`, as
   `ConfigValuesService._anchored_snapshot` reads the runtime snapshot:
   - no row: 503 `runtime_unavailable` `Configuration runtime is not ready`;
   - a revision other than `expected_revision`: 409 `revision_conflict`
     `Configuration revision is stale`, path `["expected_revision"]`, with
     `expected_revision` and `actual_revision`.
6. Run `write_rules::reject_unprobed_responses`, then
   `reject_unverified_evidence`, then `clear_stale_evidence`, against the row.
7. Commit through `config_write::commit` with source `user` and no internal access,
   then wait (Write wait below).

**Python change.** `patch_flat` authorizes the unset keys in sorted order. `unset` is
a `frozenset`, whose iteration order follows string hashing and changes between
processes, so with two failing unset keys Python reported either one.

**`write_rules.rs`.**

- `prepare_structured(key, value, path, fields, identity)` ports
  `ConfigValuesService._prepared_structured_references`. When the value is a list
  holding an object whose reference field is `"********"`, it first reads the row as
  Prepare step 5 does, with the same 503 and 409, then ports
  `voice_secrets.restore_masked_structured_references` against the row's
  `desired_values[key]`:
  - a value that is not a list: `<key> must be a list`;
  - every object item, persisted or incoming, needs a non-empty string identity:
    `<key>[<i>].<identity> must be a non-empty string`;
  - a persisted object item maps `(identity, field)` to the first value that fully
    matches `SECRET_REF_PATTERN`;
  - two incoming items with one identity:
    `<key> incoming items have duplicate <identity> <repr>`, with `pystr::repr`;
  - a masked field with no mapped reference:
    `<key>[<i>].<field> is masked but no persisted secret reference exists; provide $secret:NAME`.
    Otherwise the field takes the reference.

  The value then passes 5.8's `validate_structured_references`. Each failure is 422
  `validation_error` with its text at `["values", ...path]`. At plan time one key
  has structured reference fields: `voice.openai_compatible_audio`, with field
  `api_key` and identity `provider`.
- `reject_unprobed_responses(values, unset, paths, desired, document)` ports
  `reject_unprobed_responses_endpoints`. It walks the sorted segments that follow
  `ai.generation.endpoints.` in the request's flat keys and unset keys. `wire_api`
  comes from the request, else from the desired endpoint the decoded segment names
  (the raw segment when it does not decode). `responses` is 422 `probe_required`
  `Responses endpoint '<segment>' must be saved through /api/config/generation-endpoints/<segment>/activate`,
  with that path as `action`, at the `wire_api` key's path or
  `["values", "ai", "generation", "endpoints", <segment>]`. `'<segment>'` is
  `pystr::repr`. The `document` texts are 5.12's.
- `reject_unverified_evidence(values, paths, previous, document)` ports
  `reject_unverified_generation_endpoint_evidence`. A request key under
  `ai.generation.endpoints.` whose field is `probed_model`, `input_modalities`,
  `probed_json` or `probed_tools`, and whose value is neither `null` nor equal under
  `json::py_eq` to the row's `desired_values[key]`, is 422 `probe_required`
  `Probe evidence '<field>' for endpoint '<segment>' is activation-owned; save it through /api/config/generation-endpoints/<segment>/activate`
  with the same `action`.
- `clear_stale_evidence(...)` ports `clear_stale_generation_endpoint_probe_evidence`
  and `_generation_endpoint_identity_changed`. For each desired endpoint in order,
  the identity changed when:
  - `protocol`, `wire_api`, `api_base` or `model` is unset, or set to a value not
    equal to the desired one;
  - `api_key` is unset while the desired key is set;
  - a secret update for `api_key` differs from the desired plaintext;
  - `api_key` is set to `null` or `""` while the desired key is set, or to a value
    equal to neither the stored reference nor the desired plaintext.

  A changed endpoint gets its four evidence keys set to `null`, except keys being
  unset. An unstored evidence key counts as changed, so those `null` rows are
  written and listed in `changed_keys`.

The first two rules read the request's flat values, masked keys included, as Python
does.

**The desired config.** Two rules read `snapshot.desired`, a `DaemonConfig`, rather
than flat values: an endpoint's unstored `protocol` and `wire_api` take model
defaults, `api_base` is the stripped value `validate_api_base` returns, and `api_key`
is the resolved plaintext. `candidate.rs` gains `desired_config(row)`, which runs
5.8's steps 5 and 6 on the row's `desired_overrides` and desired bindings, opened as
3.4 opens active bindings, and returns the interpreter's validated tree instead of
discarding it. The rules read `protocol`, `wire_api`, `api_base`, `model` and
`api_key` of each entry under `ai.generation.endpoints`. Those fields pass through
real ports, and none of 5.8's identity ports writes an endpoint field, so the values
equal `snapshot.desired`'s. A desired tree that fails validation returns the route's
500.

**`config_write::commit(patch, expected_revision, source, allow_internal)`** ports
`ConfigMutations._apply` with no namespace, inside one READ COMMITTED transaction, as
`transaction_immediate` opens it:

1. Overlapping operations (values and unset, values and secrets, or secrets and
   unset) are `Invalid` `Configuration patch operations overlap: <repr>`, with
   `pystr::repr` of the sorted keys and no key (`_validate_patch_shape`).
2. Take 5.2's advisory lock, then run
   `SELECT revision FROM config_state WHERE id = true FOR UPDATE`. A missing row or
   a revision outside 0 to 2^53 − 1 is `Internal`. Any other revision than
   `expected_revision` is `Conflict { expected, actual }`.
3. Read `SELECT key, value, is_secret, revision FROM config_store ORDER BY key` and
   build the snapshot as `ConfigRepository.snapshot_from_rows` does. Each key
   resolves: an unknown key is `Internal`, and an ambiguous one is
   `Ambiguous { key }`, Python's `RegistryError`, which the PATCH maps with
   `Internal`. Its row revision lies between 0 and the global
   revision (else `Internal`), and its value parses with `json.rs` (else
   `Internal`). A reference key's value is a `$secret:` string with a non-empty
   name (else `Internal`), and its binding is `(reference, secret(name))`. `values`
   is the carrier's key defaults overlaid with the overrides. Python never stores a
   value `json.rs` rejects, since `_require_json_domain` refuses non-finite floats.
4. Embedding admission ports `_validate_embedding_admission` for source `user`. A
   patch touching `ai.embeddings.api_base`, `.catalog_key`, `.dim`, `.model` or
   `.query_prefix` is `Blocked` while `ai.embeddings.switch_run` holds a value other
   than `null`: `Embedding switch <run_id> is active; config mutation is blocked`
   when the value is an object with a non-empty string `run_id`, else
   `Malformed embedding switch journal`.
5. Run 5.8's `validate_candidate`.
6. Find the effective changes, as `_effective_changes` does:
   - an unset key that has an override;
   - a value key absent from `values`, or whose stored or default value, run
     through 5.7's `validate_value`, differs under `json::py_eq` from the validated
     value;
   - a secret key with no binding, or whose binding differs in reference or
     plaintext.

   A `validate_value` failure here is `Invalid`, as in 5.8.
7. No change returns `Ok { revision, changed: [] }` and writes nothing. A revision of
   2^53 − 1 is `Exhausted`.
8. Write at the next revision, as `_write_changes` does:
   - each changed unset key, sorted: `DELETE FROM config_store WHERE key = $1`, and
     release its binding;
   - each changed value key, sorted: upsert, and release a binding whose reference
     differs from the new value;
   - each changed secret key, sorted: `secrets::set` with the update's category and
     its description or `Config secret for <key>`, upsert the reference, and
     release a replaced binding.

   The upsert is `_upsert`'s `INSERT ... ON CONFLICT (key) DO UPDATE` with
   `json::dumps` text, the source, `is_secret` (true for secrecy `reference`), the
   revision and the current time. A released name, normalized as
   `normalize_secret_name` does, is deleted through 5.2's global delete unless 5.2's
   reference scan, run in this transaction after the writes, still finds it.
9. `UPDATE config_state SET revision = $1 WHERE id = true`, then
   `SELECT pg_notify('gobby_config_changed', '<revision>')`, then commit.

`config_write` exposes two pieces for the later write leaves: `locked_snapshot(tx)`,
steps 2 and 3 on an open transaction, and `commit_in(tx, patch, expected_revision,
source, allow_internal)`, steps 1 through 9 without opening or committing it.
`commit` opens the transaction, runs `commit_in` and commits.

`ConfigMutations._delete_released_secrets` passes the retained values to
`find_persisted_secret_references`, which also scans `config_store` inside the open
transaction. Those values are this write's own rows, so 5.2's scan alone covers
them. Python's `SecretStore.set`, `.delete` and the scan join the ambient transaction
(`storage/hub/_ambient.py::enter_transaction`), so native runs them in the write
transaction too.

`secrets::set` ports `SecretStore.set` for the global scope: the category must be in
`VALID_CATEGORIES`; the name is normalized and checked as
`normalize_and_validate_secret_name` does; `Fernet(DEK).encrypt` uses the `fernet`
crate, which 3.3 only decrypts with; and Python's
`INSERT INTO secrets ... ON CONFLICT (name, project_id) DO UPDATE ... RETURNING` runs
with a random UUIDv4 id. Fernet tokens carry a timestamp and a random IV, so tests
compare decrypted plaintexts, never ciphertext. 5.2's deletion helpers become
`pub(crate)`.

`ai.embeddings.api_base`, `.catalog_key`, `.dim`, `.model` and `.query_prefix` are the
contract's only managed keys, so a PATCH fails at authorization before admission
runs. 5.10, 5.12 and 5.14 reach admission through this core, and a PATCH maps
`Blocked` as Python's route maps `EmbeddingConfigMutationBlocked`: to the 500 below.

**`json::py_eq`** compares two `Json` values as Python's `==` compares what
`json.loads` returns. `null` equals only `null`. `true` and `false` equal `1` and
`0`. Integers compare by value with each other and exactly with floats, so an
integral float equals the integer with the same digits. Floats compare as `f64`.
Strings compare by code points and arrays by element. Objects compare by key set
and per-key value, ignoring order.

**Write wait** (Write wait): after `commit`, poll `active_state` every 50 ms until
the row's revision reaches the result's revision, for at most 65 seconds. The bound
is a parameter so tests can shorten it. Then respond 200 as
`ConfigValuesService._mutation_body` does:

```json
{"committed": true, "revision": 8, "changed_keys": ["<sorted>"], "apply_status": "applied", "pending_restart_keys": [], "failed_live_keys": {}}
```

`pending_restart_keys` and `failed_live_keys` are 5.6's public projections of that
row. `apply_status` is `failed_live` when a failed-live key remains, else
`pending_restart` when a pending key remains, else `applied`. A no-change result
finds the row already at its revision. A timeout or a row read error returns Python's
`reconcile_failed` body.

**Errors** follow `patch_flat` and the route:

- `Conflict`: 409 `revision_conflict` `Configuration revision is stale` with the
  locked revision as `actual_revision`.
- `Exhausted`: 422 `revision_exhausted` `Configuration revision cannot be advanced`,
  path `["expected_revision"]`.
- `Invalid { message, key }`: 422 `validation_error` at `["values", ...]` the key's
  Flatten path, else its `.` split, or at `["values"]` with no key. The message is
  `Secret configuration value is invalid` when the key is a reference key.
- `Internal`, `Blocked`, an ambiguous key, a desired tree that fails validation and
  any database or secret-store failure: 500
  `{"error": {"code": "persistence_indeterminate", "message": "Configuration persistence outcome is indeterminate", "path": [], "retryable": false}}`,
  the body of the route's `except Exception`. Python's `RegistryError` takes that
  path too, not the global handler's.

Every other error body is `ConfigValuesError.public_body`:
`{"error": {code, message, path, retryable}}` plus `action`, `expected_revision` and
`actual_revision` when set. `retryable` is true for the 409s and the 503.

**`check_proxied`.** `verify.rs` routes a claimed proxied PATCH to
`values_patch::check_proxied`, which reads `config_state`, `config_store` and the
named secrets in one read-only transaction:

- 200: `Skipped` when `config_state`'s revision differs from the body's `revision`,
  since a later write landed. Otherwise each changed key must match its stored row:
  - an unset key has no row;
  - a reference key's row holds `$secret:<config_key_to_secret_name(key)>`, and
    `secret` of that name returns the request's plaintext;
  - a structured key's row carries the body's revision;
  - a key the request does not name holds `null`, a cleared probe field;
  - any other key's row holds `json::dumps(validate_value(key, value))` at the
    body's revision.

  The first failing key gives `Mismatch("/changed_keys")`.
- 422: `Skipped` when `config_state`'s revision differs from the request's
  `expected_revision`. Otherwise a native dry run, every step before commit step 8
  with no lock and no write, must return the same body, else `Mismatch` at the first
  differing pointer.
- Any other status: `Skipped`.

The checks compare persisted state only, not `apply_status`, which a later swap at
the same revision can change.

**Carrier.** The generator adds two members to each `specs` entry:

- `default`, present when the spec has a default, taken from
  `storage/config_repository.py::_json_defaults(CONFIG_REGISTRY)` itself, the
  defaults that fill `ConfigReadSnapshot.values`;
- `identityField`, the name `config_structured_identity_field` returns, for a spec
  with `referenceFields`.

A spec with reference fields but not exactly one identity field fails generation,
where Python raises `RegistryError` at the request.

**Corpus.** Each case in `crates/gruntime/tests/fixtures/values_patch_corpus.json`
holds:

- `before`: the `config_state` revision, the `config_store` rows, global secrets as
  name and plaintext, and the published row (revision, `desired_values`,
  `desired_overrides`, desired bindings with plaintext, pending and failed-live
  keys);
- `request`: the raw body text;
- `published`: the pending and failed-live keys the row carries once it reaches the
  commit's revision, or `null` for a reconcile that fails;
- `expected`: status and body; the resulting rows as `key`, `value` text,
  `is_secret`, `revision` and `source`, without `updated_at`; the resulting secrets
  as name and plaintext; and the `pg_notify` payload or `null`.

The cases cover:

- every error named under Request body, Flatten, Prepare, `write_rules` and Errors,
  including two failing unset keys and a masked structured item whose row read
  returns 409 before a later key's 422;
- accepted patches: a plain value, nested `values`, a dynamic segment, a value equal
  to its default, a new secret, a changed and an unchanged secret plaintext, a
  masked secret, masked and plaintext structured items, an identity change that
  writes four `null` evidence rows, and an unset whose released secret is deleted
  and one whose secret another row still references;
- a `launch_defaults.{project_id}` value (`dict[str, object]`) equal to the stored
  one only under `py_eq`: reordered members, `1` for `1.0` and `true` for `1`;
- `applied`, `pending_restart`, `failed_live` and `reconcile_failed` responses.

The Python test builds a fresh database per case, as
`tests/storage/test_revisioned_config_store.py`'s `revision_db` fixture does, and
seeds `before`. It sends the request through `register_value_routes` with real
`ConfigMutations` and `SecretStore`. The fake runtime's snapshot is built from the
published row as 5.6's test builds one. Its `reconcile_local_commit` returns the
committed revision with `published`'s keys, or raises when `published` is `null`.
The test asserts `expected`. The Rust test seeds the same state, publishes the
post-commit row when the commit's `pg_notify` arrives, as `ConfigRuntime` would, and
asserts equal bodies, rows and secrets.

Research context:
- Python: `src/gobby/servers/routes/configuration_values.py::register_value_routes`
  (`patch_config_values`) and
  `src/gobby/servers/routes/configuration_models.py::PatchConfigRequest`.
  `src/gobby/config/values.py`: `ConfigValuesService.patch`, `.patch_flat`,
  `._flatten_values`, `._authorize`, `._anchored_snapshot`,
  `._prepared_structured_references`, `._mutation_body`, `._is_secret_key`,
  `ConfigValuesError.public_body`, and the three evidence functions.
- `src/gobby/storage/config_mutations.py`: `ConfigMutations._apply`,
  `._validate_patch_shape`, `._validate_embedding_admission`, `._effective_changes`,
  `._write_changes`, `._delete_released_secrets`, `._upsert` and `_journal_run_id`.
  `src/gobby/storage/config_repository.py`: `read_revision`, `read_rows`,
  `snapshot_from_rows`, `_complete_values`, `_json_defaults` and
  `registry_is_secret`.
- `src/gobby/storage/secrets.py::SecretStore.set`, `.delete` and
  `.find_persisted_secret_references`; `src/gobby/storage/secret_names.py`.
- `src/gobby/config/voice_secrets.py::restore_masked_structured_references` and
  `_structured_identity`; `src/gobby/config/registry.py`:
  `ConfigRegistry.dynamic_segment_follows`, `_pattern_prefix_matches` and
  `config_structured_identity_field`; `registry_key_encoding.decode_dynamic_segment`.
- `transaction_immediate` keeps PostgreSQL's default READ COMMITTED; only
  `ConfigRepository`'s snapshot reads set REPEATABLE READ READ ONLY.
- `launch_defaults.{project_id}` is the only public key whose annotation admits
  mixed JSON types.
- Planned checks:
  - `cargo nextest run -p gobby-runtime --test values_patch`;
  - `DATABASE_URL=… GOBBY_TEST_PROTECT=1 uv run pytest tests/servers/routes/test_values_patch_corpus.py tests/servers/routes/test_config_values_api.py tests/config/test_runtime_config_contract.py -v`.

Consumers unchanged:
- `src/gobby/cli/install_setup_gclient.py` — no-edit-reason: reads only the `gclient` pin, which no leaf of this plan bumps.
- `src/gobby/cli/install_setup_gcode.py` — no-edit-reason: reads the `gcode` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_ghook.py` — no-edit-reason: reads the `ghook` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_gterm.py` — no-edit-reason: reads only the `gterm` pin, which no leaf of this plan bumps.
- `src/gobby/cli/install_setup_versions.py` — no-edit-reason: looks a pin up by the binary name it is given.
- `src/gobby/code_index/gcode_gateway.py` — no-edit-reason: derives `MIN_GCODE_GRAPH_VERSION` from the `gcode` pin at import.
- `src/gobby/hooks/runtime_compat.py` — no-edit-reason: derives `MINIMUM_GHOOK_VERSION_FOR_SUPPORTED_SCHEMA` from the `ghook` pin at import.
- `src/gobby/install/bin_freshness_models.py` — no-edit-reason: iterates every pin, so the bumped values flow through.
- `src/gobby/install/distribution.py` — no-edit-reason: looks a pin up by binary name.
- `tests/cli/installers/test_grok_installer.py` — no-edit-reason: reads the `ghook` pin instead of a version literal.
- `tests/cli/test_install_ghook.py` — no-edit-reason: builds its fixtures from the `ghook` pin instead of a version literal.
- `tests/cli/test_install_setup.py` — no-edit-reason: reads the `gcode` pin instead of a version literal.
- `tests/cli/test_install_setup_gclient.py` — no-edit-reason: reads the `gclient` pin instead of a version literal.
- `tests/cli/test_install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin instead of a version literal.
- `tests/cli/test_install_setup_gterm.py` — no-edit-reason: reads the `gterm` and `gclient` pins instead of version literals.
- `tests/code_index/test_gcode_gateway.py` — no-edit-reason: reads the `gcode` pin instead of a version literal.
- `tests/hooks/test_runtime_compat.py` — no-edit-reason: compares the derived floor to the `ghook` pin instead of a version literal.
- `tests/install/test_distribution.py` — no-edit-reason: reads pins by binary name; its `0.1.0` literal is a fake `gcode` output, not a pin.
- `tests/install/test_version_pins.py` — no-edit-reason: compares each pin to its crate manifest, which is this leaf's bump check.
- `src/gobby/mcp_proxy/tools/voice.py` — no-edit-reason: calls `patch_flat` without `unset`, so the sorted authorization order does not reach it.
- `src/gobby/servers/routes/configuration.py` — no-edit-reason: registers `register_value_routes` with an unchanged signature.
- `tests/ai/test_endpoint_activation.py` — no-edit-reason: every unset key its `patch_flat` calls pass authorizes, so the sorted order leaves its outcomes unchanged.
- `tests/mcp_proxy/tools/test_voice.py` — no-edit-reason: replaces `patch_flat` with an `AsyncMock` and asserts its arguments only.

**Acceptance:**

- 5.9.1 - Every corpus case matches Python's PATCH route: status, body, resulting rows, secrets and notification. test: `tests/servers/routes/test_values_patch_corpus.py::test_cases_match_python_route`.
- 5.9.2 - Python answers a boolean, float, negative or out-of-range `expected_revision` with 422 `{"detail": "invalid config patch request"}`, and an exhausted revision with the `revision_exhausted` body. test: `tests/servers/routes/test_config_values_api.py::test_revision_domain_and_exhaustion_contract`.
- 5.9.3 - Native PATCH gives every corpus case's status, body, rows, secrets and notification. test: `crates/gruntime/tests/values_patch.rs::cases_match_python`.
- 5.9.4 - A native write waits on the advisory lock a Python mutation holds, a stale locked revision returns 409 with the locked revision, a no-change patch writes and notifies nothing, and a released secret is deleted only when no row references it. test: `crates/gruntime/tests/values_patch.rs::commit_serializes_and_releases_like_python`.
- 5.9.5 - The write wait answers from the published row, and with no publish inside the bound it returns the `reconcile_failed` body. test: `crates/gruntime/tests/values_patch.rs::write_wait_answers_or_reports_reconcile_failed`.
- 5.9.6 - `check_proxied` verifies a Python-committed PATCH and a Python 422, reports a tampered stored value as a mismatch, and skips when the revision moved. test: `crates/gruntime/tests/values_patch.rs::check_proxied_verifies_python_writes`.
- 5.9.7 - The regenerated validation carrier, with `default` and `identityField`, equals the checked-in bytes. test: `tests/config/test_runtime_config_contract.py::test_checked_in_validation_carrier_matches_registry`.
- 5.9.8 - The regenerated gcore contract equals the checked-in bytes, so this leaf leaves gcore unchanged. test: `tests/config/test_runtime_config_contract.py::test_checked_in_contract_matches_registry`.
- 5.9.9 - `gdaemon` is one patch version above the executor's starting tip in its `Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`, so this leaf's cutover passes the release-identity preflight. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 5.10 Native `POST /api/config/secrets` [category: code] (depends: 5.9)
`kind: deliverable`

Targets:
- `src/gobby/servers/routes/configuration_secrets.py::register_secret_routes`
- `src/gobby/storage/secrets.py::SecretStore.set`
- `tests/servers/routes/test_secret_save_corpus.py`
- `crates/gruntime/tests/fixtures/secret_save_corpus.json`
- `crates/gruntime/src/lib.rs`
- `crates/gruntime/src/secret_routes.rs`
- `crates/gruntime/src/secrets.rs`
- `crates/gruntime/src/config_write.rs`
- `crates/gruntime/src/verify.rs`
- `crates/gruntime/tests/secret_save.rs`
- `crates/gdaemon/Cargo.toml`
- `src/gobby/install/version_pins.py::MANAGED_BIN_VERSION_PINS`
- `Cargo.lock`

Release ownership (Constraints): gruntime is in gdaemon's release closure, so this
leaf bumps `gdaemon` by 0.0.1 from the executor's starting tip.

Row: `POST /api/config/secrets` for `Operator`. Compare mode proxies it once and runs
`check_proxied` (D3). `require_mutation_auth` admits every authenticated request,
and a claimed `Operator` request is authenticated by construction (5.2).

**Request body.** Python's `save_secret` stops taking `SaveSecretRequest` as a
FastAPI body parameter and parses the body itself, as 5.9 does:

1. Decode the body as strict UTF-8, parse it with `json.loads` and validate it with
   `SaveSecretRequest.model_validate`. The body is an object with string `name` and
   `value`, an optional string `category` (default `general`) and an optional
   `description`, a string or `null`. Unknown members are ignored.
2. Any failure returns 422 `{"detail": "invalid secret request"}`.

The parse runs before the handler's `try`. Its `except ValueError` would otherwise
answer 400 with the decoder's text, since `JSONDecodeError`, `UnicodeDecodeError`
and pydantic's `ValidationError` are all `ValueError`s. Native parses with 5.7's
`json.rs` and applies the same rules. A `NaN`, `Infinity` or lone-surrogate escape
anywhere in the body is the documented 422 divergence (Config validation).

**Save** ports `ConfigStore.set_named_secret` in Python's order, inside one READ
COMMITTED transaction:

1. Normalize the name as `normalize_secret_name` does: `pystr::strip`, then
   `pystr::lower`.
2. Take 5.2's advisory lock. When the normalized name is `embeddings_api_key`, run
   5.2's embedding-switch check; an active or malformed journal is `Blocked`.
3. Read `config_write::locked_snapshot`.
4. Find the referencing keys: stored reference keys (secrecy `reference`, per field
   for a pattern) whose value holds a `SECRET_REF_PATTERN` match whose lowercased
   name equals the normalized name. Python walks every stored value with
   `SecretStore.find_secret_references` and then keeps reference keys. A reference
   key's stored value is always a `$secret:` string, so scanning that string finds
   the same keys.
5. With referencing keys, run `config_write::commit_in` with source `user`,
   internal access, the locked revision as `expected_revision`, and one secret
   update per referencing key carrying the plaintext, the normalized name, the
   request's category and its description. This ports `ConfigStore._apply_internal`
   and `ConfigMutations.patch_internal`, whose nested transaction joins the open
   one, so the revision check cannot conflict. `_apply_internal`'s managed-key
   check never fires, because no reference key is a structural embedding key.
   Then read the global secret row with the normalized name, as Python picks it
   from `SecretStore.list`; no row is `Internal`.
   - With an unchanged plaintext nothing is written, and the existing row comes
     back with its category and description even when the request names others.
   - A change writes the secret through `secrets::set` once per referencing key, in
     sorted order, each with the request's description or `Config secret for <key>`.
     With no description in the request, the last key's text stays.
   - A stored reference that is not lowercase, such as `$secret:Foo`, is rewritten
     as `$secret:foo`. Its old binding is released, and the reference scan keeps
     the secret.
6. Otherwise find the exact reference key whose `config_key_to_secret_name` equals
   the normalized name, taking the first in the carrier's `specs` order as Python
   takes the first in `key_specs`. When one exists, run 5.8's `validate_candidate`
   on the snapshot with internal access and one secret update for that key carrying
   the plaintext and no name (`ConfigMutations.validate_named_secret`). Then call
   `secrets::set` with the request's category and description, which may be
   `null`. The row it returns is the result.
7. Commit and respond 200 `{"ok": true, "secret": {...}}`, with the secret in 5.2's
   row shape. Python returns without waiting for a reconcile, and so does native.

**`secrets::set` errors.** `SecretStore.set` checks the category first, then the
name, and raises `ValueError`s that the route answers with 400:

- `Invalid category '<category>'. Must be one of: ['general', 'integration', 'llm', 'mcp_server', 'memory']`;
- `Invalid secret name: must start with a letter or underscore and contain only letters, digits, and underscores`.

`set` returns them as `Invalid` with that text. It runs last on each path, so a bad
category or name fails only where Python's does: after candidate validation, and on
the referencing path only when the plaintext changed. With an unchanged plaintext
nothing is written and the response is 200.

**Python change.** `SecretStore.set` renders the categories as
`sorted(VALID_CATEGORIES)`. The message rendered the `set` itself, whose order
follows string hashing and changes between processes, the same fault as 5.8's
`ChatConfig.validate_default_mode` fix.

**Errors** follow the route's two handlers:

- 400 `{"detail": "<text>"}` for every Python `ValueError`: `Invalid` from
  `validate_candidate` or `commit_in` (a reference key's failure reads
  `Secret configuration value is invalid`), `Ambiguous { key }` from
  `locked_snapshot` as `Ambiguous configuration key: <key>`, and `set`'s two
  messages.
- 500 `{"detail": "Internal server error"}` for every other failure, Python's
  `RuntimeError`s and database errors: `Internal`, `Blocked`, `Conflict`
  (unreachable under the lock), `Exhausted` (a referencing write at revision
  2^53 − 1), the missing row after a write, and secret-store failures.

**`check_proxied`.** `verify.rs` routes a claimed proxied
`POST /api/config/secrets` to `secret_routes::check_saved`:

- 200: `Verified` when the global row with the normalized name exists, its `id`
  equals the body's `secret.id`, and 3.3's `secret` of that name returns the
  request's `value`; else `Mismatch("/secret")`.
- 400 or 422: native runs the request inside a transaction it always rolls back.
  `Verified` when it returns the same status and body, else `Mismatch("/detail")`.
  Python's failed transaction rolled back too, so native sees the state Python saw.
- Any other status: `Skipped`.

A write between Python's answer and the check can produce a false mismatch, which
E1 counts (5.2).

**Corpus.** Each case in `crates/gruntime/tests/fixtures/secret_save_corpus.json`
holds:

- `before`: the `config_state` revision, the `config_store` rows, and the global
  secrets as name, plaintext, category and description;
- `request`: the raw body bytes, as text or base64 for the non-UTF-8 cases;
- `expected`: status and body, with `secret`'s `id`, `created_at` and `updated_at`
  left out; the resulting rows as in 5.9; the resulting secrets as name, plaintext,
  category and description; and the `pg_notify` payload or `null`.

Both tests assert that the omitted `id` and timestamps equal the stored row's,
formatted as 5.2 formats them, and that an overwritten secret keeps its `id`. The
cases cover:

- invalid bodies: invalid UTF-8, a UTF-8 byte-order mark, UTF-16, text that is not
  JSON, an array, a missing `value`, a numeric `name` and a `null` category; and a
  valid body with an unknown member;
- a new secret whose name normalizes (`"  New_Key "`), with the default category
  and a `null` description, and an overwrite of an unreferenced secret;
- an invalid name; an invalid category on the plain path (400), on the referencing
  path with a changed plaintext (400, nothing written) and with an unchanged
  plaintext (200, nothing written);
- the canonical name of `databases.falkordb.password` with a valid and an invalid
  plaintext (400 `Secret configuration value is invalid`, nothing stored);
- the referencing path: a changed plaintext with and without a description, two
  referencing keys, an invalid plaintext for a bound `databases.falkordb.password`,
  a mixed-case stored reference, and an unchanged plaintext;
- a name referenced only from a `voice.openai_compatible_audio` item, whose key is
  not a reference key, so the save takes the plain path;
- `embeddings_api_key` during an active embedding switch (500, nothing written) and
  with no switch;
- a referencing write at revision 2^53 − 1 (500).

The Python test builds a fresh database per case, as 5.9's corpus test does, and
seeds `before`. It sends the request through `register_secret_routes` with a context
whose secret store is a real `SecretStore` and whose auth service admits the
request, and asserts `expected`. The Rust test seeds the same state and asserts the
native route gives the same results.

Research context:
- Python: `src/gobby/servers/routes/configuration_secrets.py::register_secret_routes`
  (`save_secret`, `require_mutation_auth`),
  `src/gobby/servers/routes/configuration_models.py::SaveSecretRequest`, and
  `src/gobby/storage/config_store.py::ConfigStore.set_named_secret`,
  `._apply_internal` and `._assert_embedding_mutation_allowed`.
- `src/gobby/storage/config_mutations.py::ConfigMutations.patch_internal` passes
  `allow_internal=True` and no namespace, and `.validate_named_secret` runs
  `_validate_candidate` with internal access.
- `src/gobby/storage/secrets.py::SecretStore.set`, `.list` and
  `.find_secret_references`; `src/gobby/storage/secret_names.py`;
  `src/gobby/config/registry.py::config_key_secrecy`.
- `RegistryError` is a `ValueError`. `ConfigMutationError` and
  `ConfigRepositoryError` are `RuntimeError`s.
- `src/gobby/storage/hub/_ambient.py::enter_transaction` reuses the open
  transaction for the same database adapter and takes the advisory lock again on
  it, so Python's nested `_apply` runs inside `set_named_secret`'s transaction.
- Planned checks:
  - `cargo nextest run -p gobby-runtime --test secret_save`;
  - `DATABASE_URL=… GOBBY_TEST_PROTECT=1 uv run pytest tests/servers/routes/test_secret_save_corpus.py tests/servers/routes/test_configuration_routes.py tests/storage/test_secrets_store.py -v`.

Consumers unchanged:
- `src/gobby/cli/install_setup_gclient.py` — no-edit-reason: reads only the `gclient` pin, which no leaf of this plan bumps.
- `src/gobby/cli/install_setup_gcode.py` — no-edit-reason: reads the `gcode` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_ghook.py` — no-edit-reason: reads the `ghook` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_gterm.py` — no-edit-reason: reads only the `gterm` pin, which no leaf of this plan bumps.
- `src/gobby/cli/install_setup_versions.py` — no-edit-reason: looks a pin up by the binary name it is given.
- `src/gobby/code_index/gcode_gateway.py` — no-edit-reason: derives `MIN_GCODE_GRAPH_VERSION` from the `gcode` pin at import.
- `src/gobby/hooks/runtime_compat.py` — no-edit-reason: derives `MINIMUM_GHOOK_VERSION_FOR_SUPPORTED_SCHEMA` from the `ghook` pin at import.
- `src/gobby/install/bin_freshness_models.py` — no-edit-reason: iterates every pin, so the bumped values flow through.
- `src/gobby/install/distribution.py` — no-edit-reason: looks a pin up by binary name.
- `tests/cli/installers/test_grok_installer.py` — no-edit-reason: reads the `ghook` pin instead of a version literal.
- `tests/cli/test_install_ghook.py` — no-edit-reason: builds its fixtures from the `ghook` pin instead of a version literal.
- `tests/cli/test_install_setup.py` — no-edit-reason: reads the `gcode` pin instead of a version literal.
- `tests/cli/test_install_setup_gclient.py` — no-edit-reason: reads the `gclient` pin instead of a version literal.
- `tests/cli/test_install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin instead of a version literal.
- `tests/cli/test_install_setup_gterm.py` — no-edit-reason: reads the `gterm` and `gclient` pins instead of version literals.
- `tests/code_index/test_gcode_gateway.py` — no-edit-reason: reads the `gcode` pin instead of a version literal.
- `tests/hooks/test_runtime_compat.py` — no-edit-reason: compares the derived floor to the `ghook` pin instead of a version literal.
- `tests/install/test_distribution.py` — no-edit-reason: reads pins by binary name; its `0.1.0` literal is a fake `gcode` output, not a pin.
- `tests/install/test_version_pins.py` — no-edit-reason: compares each pin to its crate manifest, which is this leaf's bump check.
- `src/gobby/servers/routes/configuration.py` — no-edit-reason: registers `register_secret_routes` with an unchanged signature.
- `tests/servers/routes/test_configuration_routes.py` — no-edit-reason: posts valid secret bodies only, and its invalid-category test matches the `Invalid category` prefix the sorted message keeps.
- `tests/storage/test_secrets_store.py` — no-edit-reason: matches only the `Invalid category` prefix the sorted message keeps.
- `src/gobby/cli/_install_embedding_prompts.py` — no-edit-reason: does not read the text of `SecretStore.set`'s invalid-category error, the only behavior this leaf changes in it.
- `src/gobby/cli/_install_prompts.py` — no-edit-reason: does not read the text of `SecretStore.set`'s invalid-category error, the only behavior this leaf changes in it.
- `src/gobby/cli/_install_state.py` — no-edit-reason: does not read the text of `SecretStore.set`'s invalid-category error, the only behavior this leaf changes in it.
- `src/gobby/cli/installers/compose_env.py` — no-edit-reason: does not read the text of `SecretStore.set`'s invalid-category error, the only behavior this leaf changes in it.
- `src/gobby/cli/installers/falkor.py` — no-edit-reason: does not read the text of `SecretStore.set`'s invalid-category error, the only behavior this leaf changes in it.
- `src/gobby/cli/installers/shared.py` — no-edit-reason: does not read the text of `SecretStore.set`'s invalid-category error, the only behavior this leaf changes in it.
- `src/gobby/cli/mcp_proxy.py` — no-edit-reason: does not read the text of `SecretStore.set`'s invalid-category error, the only behavior this leaf changes in it.
- `src/gobby/cli/secrets.py` — no-edit-reason: does not read the text of `SecretStore.set`'s invalid-category error, the only behavior this leaf changes in it.
- `src/gobby/communications/manager.py` — no-edit-reason: does not read the text of `SecretStore.set`'s invalid-category error, the only behavior this leaf changes in it.
- `src/gobby/mcp_proxy/client_manager/server_registry.py` — no-edit-reason: does not read the text of `SecretStore.set`'s invalid-category error, the only behavior this leaf changes in it.
- `src/gobby/mcp_proxy/importer.py` — no-edit-reason: does not read the text of `SecretStore.set`'s invalid-category error, the only behavior this leaf changes in it.
- `src/gobby/mcp_proxy/manager.py` — no-edit-reason: does not read the text of `SecretStore.set`'s invalid-category error, the only behavior this leaf changes in it.
- `src/gobby/mcp_proxy/oauth.py` — no-edit-reason: does not read the text of `SecretStore.set`'s invalid-category error, the only behavior this leaf changes in it.
- `src/gobby/mcp_proxy/oauth_keepalive.py` — no-edit-reason: does not read the text of `SecretStore.set`'s invalid-category error, the only behavior this leaf changes in it.
- `src/gobby/mcp_proxy/services/server_mgmt.py` — no-edit-reason: does not read the text of `SecretStore.set`'s invalid-category error, the only behavior this leaf changes in it.
- `src/gobby/mcp_proxy/sync_servers.py` — no-edit-reason: does not read the text of `SecretStore.set`'s invalid-category error, the only behavior this leaf changes in it.
- `src/gobby/mcp_proxy/sync_templates.py` — no-edit-reason: does not read the text of `SecretStore.set`'s invalid-category error, the only behavior this leaf changes in it.
- `src/gobby/runner.py` — no-edit-reason: does not read the text of `SecretStore.set`'s invalid-category error, the only behavior this leaf changes in it.
- `src/gobby/runner_init/mcp_stack.py` — no-edit-reason: does not read the text of `SecretStore.set`'s invalid-category error, the only behavior this leaf changes in it.
- `src/gobby/skills/hubs/manager.py` — no-edit-reason: does not read the text of `SecretStore.set`'s invalid-category error, the only behavior this leaf changes in it.
- `src/gobby/storage/config_repository.py` — no-edit-reason: does not read the text of `SecretStore.set`'s invalid-category error, the only behavior this leaf changes in it.
- `src/gobby/storage/config_store.py` — no-edit-reason: does not read the text of `SecretStore.set`'s invalid-category error, the only behavior this leaf changes in it.
- `src/gobby/storage/mcp_secrets.py` — no-edit-reason: does not read the text of `SecretStore.set`'s invalid-category error, the only behavior this leaf changes in it.
- `tests/ai/test_endpoint_activation.py` — no-edit-reason: does not read the text of `SecretStore.set`'s invalid-category error, the only behavior this leaf changes in it.
- `tests/cli/test_cli_secrets.py` — no-edit-reason: does not read the text of `SecretStore.set`'s invalid-category error, the only behavior this leaf changes in it.
- `tests/communications/test_communications_manager.py` — no-edit-reason: does not read the text of `SecretStore.set`'s invalid-category error, the only behavior this leaf changes in it.
- `tests/e2e/test_runtime_boundary.py` — no-edit-reason: does not read the text of `SecretStore.set`'s invalid-category error, the only behavior this leaf changes in it.
- `tests/integration/config/conftest.py` — no-edit-reason: does not read the text of `SecretStore.set`'s invalid-category error, the only behavior this leaf changes in it.
- `tests/integration/config/test_config_cross_seam.py` — no-edit-reason: does not read the text of `SecretStore.set`'s invalid-category error, the only behavior this leaf changes in it.
- `tests/mcp_proxy/test_oauth.py` — no-edit-reason: does not read the text of `SecretStore.set`'s invalid-category error, the only behavior this leaf changes in it.
- `tests/mcp_proxy/test_oauth_keepalive.py` — no-edit-reason: does not read the text of `SecretStore.set`'s invalid-category error, the only behavior this leaf changes in it.
- `tests/mcp_proxy/test_oauth_setup.py` — no-edit-reason: does not read the text of `SecretStore.set`'s invalid-category error, the only behavior this leaf changes in it.
- `tests/mcp_proxy/test_registries.py` — no-edit-reason: does not read the text of `SecretStore.set`'s invalid-category error, the only behavior this leaf changes in it.
- `tests/mcp_proxy/test_sync_servers.py` — no-edit-reason: does not read the text of `SecretStore.set`'s invalid-category error, the only behavior this leaf changes in it.
- `tests/servers/test_auth_middleware.py` — no-edit-reason: does not read the text of `SecretStore.set`'s invalid-category error, the only behavior this leaf changes in it.
- `tests/storage/test_managed_credentials.py` — no-edit-reason: does not read the text of `SecretStore.set`'s invalid-category error, the only behavior this leaf changes in it.
- `tests/storage/test_revisioned_config_store.py` — no-edit-reason: does not read the text of `SecretStore.set`'s invalid-category error, the only behavior this leaf changes in it.
- `tests/storage/test_secret_set_atomic.py` — no-edit-reason: does not read the text of `SecretStore.set`'s invalid-category error, the only behavior this leaf changes in it.
- `tests/storage/test_secrets.py` — no-edit-reason: does not read the text of `SecretStore.set`'s invalid-category error, the only behavior this leaf changes in it.
- `tests/storage/test_storage_mcp.py` — no-edit-reason: does not read the text of `SecretStore.set`'s invalid-category error, the only behavior this leaf changes in it.
- `tests/utils/test_deps.py` — no-edit-reason: does not read the text of `SecretStore.set`'s invalid-category error, the only behavior this leaf changes in it.

**Acceptance:**

- 5.10.1 - Every corpus case matches Python's secrets POST: status, body, resulting rows, secrets and notification. test: `tests/servers/routes/test_secret_save_corpus.py::test_cases_match_python_route`.
- 5.10.2 - The native secrets POST gives every corpus case's status, body, rows, secrets and notification. test: `crates/gruntime/tests/secret_save.rs::cases_match_python`.
- 5.10.3 - A native save waits on the advisory lock a Python mutation holds, and a referencing save whose `set` fails leaves the rows, the secrets and the revision unchanged and sends no notification. test: `crates/gruntime/tests/secret_save.rs::save_serializes_and_rolls_back`.
- 5.10.4 - `check_saved` verifies a Python-committed save and a Python 400 or 422, reports a changed plaintext or `id` as a mismatch, and skips other statuses. test: `crates/gruntime/tests/secret_save.rs::proxied_saves_are_checked`.
- 5.10.5 - `gdaemon` is one patch version above the executor's starting tip in its `Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`, so this leaf's cutover passes the release-identity preflight. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 5.11 YAML 1.2 document reading on both sides [category: code] (depends: 5.10)
`kind: deliverable`

Targets:
- `src/gobby/config/yaml12.py`
- `src/gobby/config/documents.py::ConfigDocumentsService._prepare`
- `src/gobby/config/documents.py::ConfigDocumentsService._validate_mapping_keys`
- `src/gobby/config/documents.py::ConfigDocumentsService._dump`
- `crates/gcore/assets/config/runtime_config_contract.json::*` — scope-reason: regenerated after this leaf's `src/gobby/config/` edits, which must leave its bytes unchanged
- `tests/config/test_yaml12_vectors.py`
- `tests/config/test_yaml_export_vectors.py`
- `tests/servers/routes/test_config_yaml_replace.py::*` — scope-reason: adds `test_import_reads_yaml_1_2`
- `crates/gruntime/Cargo.toml`
- `crates/gruntime/src/lib.rs`
- `crates/gruntime/src/yaml.rs`
- `crates/gruntime/src/pyyaml.rs`
- `crates/gruntime/tests/yaml12.rs`
- `crates/gruntime/tests/fixtures/yaml12_vectors.json`
- `crates/gruntime/tests/fixtures/yaml_export_vectors.json`
- `crates/gdaemon/Cargo.toml`
- `src/gobby/install/version_pins.py::MANAGED_BIN_VERSION_PINS`
- `Cargo.lock`

Release ownership (Constraints): gruntime is in gdaemon's release closure, so this
leaf bumps `gdaemon` by 0.0.1 from the executor's starting tip. The regenerated gcore
contract keeps its bytes, so gcore does not change.

This leaf adds no route. It moves both sides to the YAML 1.2 reading that the YAML
documents decision fixes. Python's import parse and Rust's `yaml_serde` parse return
the same value or the same error for every document in a shared vector corpus, and
export quotes every string that either reading would take for another type. 5.12
serves the document routes on it.

**Python loader.** `src/gobby/config/yaml12.py` adds `Yaml12Loader(yaml.SafeLoader)`.
It keeps PyYAML's reader, scanner and parser. It replaces composition, resolution
and construction with a port of `yaml_serde` 0.10.4's `Loader::next_document`
(`loader.rs`) and `deserialize_any` (`de.rs`) over the same parser events. Resolvers
alone cannot match `yaml_serde`, because PyYAML's composer and constructors also
differ from it:

- they apply merge keys;
- they build `!!binary`, `!!timestamp` and `!!set` nodes as Python types;
- they reject a redefined anchor;
- they have no depth or alias limit, so a self-referencing alias builds a cyclic
  object whose flattening raises `RecursionError`, a 500.

`yaml12.load(content)` runs three steps:

1. **Collect** the first document's events as `next_document` does:
   - stream and document start events are skipped, and the first document end
     stops collection;
   - an anchor gets as its id the number of distinct anchor names already
     recorded, and the id maps to the anchoring event's index;
   - an alias maps to its anchor's current id, and an unknown anchor stops
     collection with an error at the alias's mark;
   - a parse error stops collection and is kept;
   - an empty stream collects one null event at the stream end's mark.
2. **Walk** from the first event as `deserialize_any` walks:
   - An alias counts one jump. More than 100 jumps per collected event fails with
     `repetition limit exceeded`. Otherwise the walk replays the anchored node from
     its index.
   - A node whose tag starts with `!`, a local tag or the non-specific `!`, fails
     at its mark.
   - A scalar tagged `tag:yaml.org,2002:bool`, `:int`, `:float` or `:null` must
     parse with `yaml_serde`'s `parse_bool`, `visit_int`, `parse_f64` or
     `parse_null`, else it fails at its mark. A scalar with any other tag is its
     text as a string. An untagged plain scalar resolves as `plain_scalar` below
     does, and any other untagged scalar is a string.
   - A sequence or mapping ignores a tag that does not start with `!`. Entering one
     while 128 are open fails at its mark (`recursion_check`).
   - A mapping key that is a sequence or mapping fails at its mark. A key that
     resolves to anything but a string stops the walk with `NonStringKeyError`.
     The error carries Python's `str()` of the key: `None`, `True`, `False`, the
     integer's digits or the float's `repr`. A repeated string key keeps its first
     position and its last value.
   - Reading past the collected events raises the kept parse error.
3. **Finish.** Raise the kept parse error, if there is one. Then read on: any
   event after the first document other than the stream end, or a parse error
   there, fails with
   `deserializing from YAML containing more than one document is not supported`.

The anchor numbering is a `yaml_serde` defect, kept on both sides so the corpus has
one answer. A new anchor reuses the number of a redefined name, and every alias of
that name then replays the newer anchor's node. Native cannot see anchors through
serde, so it cannot reject them, and Python reproduces the numbering.

Non-string keys stop the load at the first one in document order. Python's
`_validate_mapping_keys` instead searched the finished tree, where a later repeated
key could hide one. The new rule is the same on both sides, and Python no longer
builds a dict whose keys `1`, `1.0` and `true` collide.

`yaml12.plain_scalar(text)` ports `visit_untagged_scalar` and the functions it
calls: `parse_null`, `parse_bool`, `visit_int` with `parse_unsigned_int` and
`parse_negative_int`, `digits_but_not_number`, and `parse_f64`. In short:

- `""`, `null`, `Null`, `NULL` and `~` are `None`. `true`, `True`, `TRUE`, `false`,
  `False` and `FALSE` are booleans.
- An integer has at most one sign, then `0x`, `0o` or `0b` digits, or decimal
  digits with no leading zero before another digit, no underscores, and a value
  within `u128` or `i128`, the widest types `visit_int` tries. A larger decimal
  reads as a float.
- A float is `.inf`, `.Inf` or `.INF` with an optional sign, `.nan`, `.NaN` or
  `.NAN`, or finite text that `str::parse::<f64>` accepts after one optional `+`.
  That grammar is `[+-]?(D+ | D+ '.' D* | D* '.' D+)([eE][+-]?D+)?` with ASCII
  digits. The port matches it before calling `float()`, which also accepts
  underscores, surrounding whitespace and non-ASCII digits.
- Everything else is a string.

So `yes`, `no`, `on` and `off` are strings, `0o17` is 15, `017`, `1_000`, `12:30`
and `2024-01-01` are strings, `1e5` is a float, and `<<` is a plain key that merges
nothing, with or without `!!merge`.

**Messages.** `yaml12.YamlError(ValueError)` carries one of four messages:

- `Invalid YAML at line L, column C`, with the 1-based line and column of the
  failing mark: PyYAML's `problem_mark` for a parse error, else the node's or the
  alias's mark;
- `Invalid YAML at line 1, column 1` for PyYAML's `ReaderError`, a character YAML
  forbids, whose libyaml counterpart leaves its mark unset;
- `Invalid YAML: repetition limit exceeded`;
- `Invalid YAML: deserializing from YAML containing more than one document is not supported`.

The last two have no mark on either side.

PyYAML checks the whole text for forbidden characters before parsing, while libyaml
checks as it reads. A long document with an earlier error can therefore report
different errors. That case, and any other difference between PyYAML's parser and
libyaml's that the corpus does not pin, is a compare-mode mismatch that E1 counts.

**`_prepare`.** `ConfigDocumentsService._prepare` loads with `yaml12.load`:

- `NonStringKeyError` is 422 `validation_error`
  `Configuration keys must be strings` at `["content", *str(key).split(".")]`, as
  `_validate_mapping_keys` reported it;
- any other `YamlError` is 422 `validation_error` with its message at
  `["content"]`.

`_validate_mapping_keys` and the loop's `isinstance(key, str)` check become
unreachable and are deleted.

**Export.** The 1.2 reading would turn some exported strings into other types:
`0o17` becomes 15, and `1e5`, `1.5e5` and `1E+5` become floats, because PyYAML's
1.1 resolver leaves them plain. `yaml12.Yaml12SafeDumper(yaml.SafeDumper)`
overrides `resolve`. A plain scalar that `SafeDumper`'s resolver reads as a string
but `plain_scalar` reads as another type resolves to that type's tag, so the
emitter quotes it. Strings the 1.1 resolver already quotes (`yes`, `0755`,
`12:30`) stay quoted, so every export still reads back unchanged through
`yaml.safe_load`. `_dump` dumps with it. `pyyaml.rs` keeps a string plain only when
both its resolver and `yaml.rs`'s `plain_scalar` read it as a string.

**`yaml.rs`.** `load(text)` deserializes with `serde_yaml::from_str` into a private
type whose `Deserialize` builds 5.7's `json::Json`. gruntime adds
`serde_yaml = { package = "yaml_serde", version = "0.10.4" }`, as gcore names it.

- Unit and none are `Null`, booleans are `Bool`, and the four integer visits are
  `Int` with the value's digits. `f64` is `Float`, so `.inf` and `.nan` survive for
  `validate_value` to reject. Strings are `Str`.
- Sequences are `Array`. Mappings are `Object`, keeping a repeated key's first
  position and last value.
- Keys go through a seed that accepts scalars only. A collection key fails, so
  `yaml_serde` marks the key node. A non-string scalar key records Python's
  `str()` rendering, with floats through 5.7's repr, and fails; `load` then returns
  `NonStringKey(rendering)`.
- An enum visit, a tag starting with `!`, fails, so `yaml_serde` marks the node.

An error with a `location()` becomes `Invalid YAML at line L, column C`. Any other
becomes `Invalid YAML: ` followed by the error's text. `plain_scalar(text)` ports
`visit_untagged_scalar`'s classification for `pyyaml.rs`, because `yaml_serde`
keeps it crate-private.

**Vectors.** `crates/gruntime/tests/fixtures/yaml12_vectors.json` holds
`{yaml, expected}` pairs. `expected` is one of three forms: the JSON value, with
non-finite floats as `{"$float": "inf"}`, `"-inf"` or `"nan"`;
`{"error": "<message>"}`; or `{"nonStringKey": "<rendering>"}`. The cases cover:

- each YAML 1.1/1.2 difference: `yes`, `No`, `on`, `OFF`, `y`, `0o17`, `017`,
  `0b101`, `0x1F`, `-0x1F`, `+12`, `1_000`, `12:30`, `2024-01-01`, `=`, `1e5`,
  `1.5e5`, `.5`, `5.`, `1e999`, `inf`, `nan`, `+.inf`, `-.Inf`, `.NaN`, `-0`,
  `007`, and the largest integer and the next one for both `u128` and `i128`;
- null, boolean and string forms: an empty value, `~`, `NULL`, `tRUE`, quoted
  `'true'` and `"1"`, and literal and folded blocks;
- tags: `!!str 123`, `!!int "12"`, `!!int abc`, `!!float 1`, `!!bool yes`,
  `!!null ""`, `!!binary`, `!!timestamp`, `!!set`, `!!omap`, `!!str` on a mapping,
  `!foo`, `! 12`, a verbatim core tag and an undefined tag handle;
- anchors: scalar and mapping aliases, an alias inside an anchored node, a
  self-referencing alias, an unknown alias, a redefined anchor followed by a new
  one, and an expansion over the repetition limit;
- merge keys: `<<: *base` and `!!merge <<: *base`, both plain `<<` keys;
- keys: a repeated key; `1`, `true`, `~`, `1.5` and `.inf` keys; a sequence key and
  a mapping key;
- depth: 128 nested collections, and 129;
- documents: empty text, a comment alone, `---` alone, an explicit `...` end, and
  two documents;
- malformed input: an unclosed flow sequence, `a: b: c`, a tab indent, a bad
  indent, a leading `@`, a forbidden control character and an invalid escape.

The Python test asserts that `yaml12.load` gives each `expected`, and the Rust test
asserts that `yaml::load` does.

5.5's `yaml_export_vectors.json` gains the strings `0o17`, `1e5`, `1.5e5`, `1E+5`
and `.5e3`, all now quoted, and is regenerated from the new `_dump`. 5.5's Python
test also asserts that each vector reads back through `yaml12.load`.

Research context:
- `yaml_serde` 0.10.4 (`~/.cargo/registry/src/*/yaml_serde-0.10.4/src`):
  - `loader.rs`: `Loader::next_document`;
  - `de.rs`: `deserialize_any`, `visit_scalar`, `visit_untagged_scalar`,
    `visit_int`, the `parse_*` functions, `digits_but_not_number`, `jump` (the
    100-per-event limit), `recursion_check` (depth 128), and
    `DeserializerFromEvents::de` (parse error after the walk, then the
    more-than-one-document check);
  - `error.rs`: `Location::from_mark` adds 1 to libyaml's zero-based line and
    column.
- Merge keys apply only through `Value::apply_merge` (`value/mod.rs`), which
  `deserialize_any` never calls.
- libyaml-rs 0.3.0's `yaml_parser_set_reader_error` (`reader.rs`) sets no mark.
- PyYAML 6.0.3:
  - `yaml/parser.py` resolves tag handles as libyaml does: `!foo` stays `!foo`,
    `!!int` becomes `tag:yaml.org,2002:int`, and `!` stays `!`;
  - `yaml/composer.py` rejects a redefined anchor;
  - `yaml/constructor.py`'s `SafeConstructor.flatten_mapping` applies merges;
  - `yaml/reader.py` checks the whole string for forbidden characters on
    construction.
- Python: `src/gobby/config/documents.py::ConfigDocumentsService._prepare`,
  `._validate_mapping_keys` and `._dump`.
- gcore and gcode already depend on `yaml_serde` 0.10.4 as `serde_yaml`.
- Planned checks:
  - `cargo nextest run -p gobby-runtime --test yaml12 --test export`;
  - `DATABASE_URL=… GOBBY_TEST_PROTECT=1 uv run pytest tests/config/test_yaml12_vectors.py tests/config/test_yaml_export_vectors.py tests/servers/routes/test_config_yaml_replace.py tests/config/test_runtime_config_contract.py -v`.

Consumers unchanged:
- `src/gobby/cli/install_setup_gclient.py` — no-edit-reason: reads only the `gclient` pin, which no leaf of this plan bumps.
- `src/gobby/cli/install_setup_gcode.py` — no-edit-reason: reads the `gcode` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_ghook.py` — no-edit-reason: reads the `ghook` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_gterm.py` — no-edit-reason: reads only the `gterm` pin, which no leaf of this plan bumps.
- `src/gobby/cli/install_setup_versions.py` — no-edit-reason: looks a pin up by the binary name it is given.
- `src/gobby/code_index/gcode_gateway.py` — no-edit-reason: derives `MIN_GCODE_GRAPH_VERSION` from the `gcode` pin at import.
- `src/gobby/hooks/runtime_compat.py` — no-edit-reason: derives `MINIMUM_GHOOK_VERSION_FOR_SUPPORTED_SCHEMA` from the `ghook` pin at import.
- `src/gobby/install/bin_freshness_models.py` — no-edit-reason: iterates every pin, so the bumped values flow through.
- `src/gobby/install/distribution.py` — no-edit-reason: looks a pin up by binary name.
- `tests/cli/installers/test_grok_installer.py` — no-edit-reason: reads the `ghook` pin instead of a version literal.
- `tests/cli/test_install_ghook.py` — no-edit-reason: builds its fixtures from the `ghook` pin instead of a version literal.
- `tests/cli/test_install_setup.py` — no-edit-reason: reads the `gcode` pin instead of a version literal.
- `tests/cli/test_install_setup_gclient.py` — no-edit-reason: reads the `gclient` pin instead of a version literal.
- `tests/cli/test_install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin instead of a version literal.
- `tests/cli/test_install_setup_gterm.py` — no-edit-reason: reads the `gterm` and `gclient` pins instead of version literals.
- `tests/code_index/test_gcode_gateway.py` — no-edit-reason: reads the `gcode` pin instead of a version literal.
- `tests/hooks/test_runtime_compat.py` — no-edit-reason: compares the derived floor to the `ghook` pin instead of a version literal.
- `tests/install/test_distribution.py` — no-edit-reason: reads pins by binary name; its `0.1.0` literal is a fake `gcode` output, not a pin.
- `tests/install/test_version_pins.py` — no-edit-reason: compares each pin to its crate manifest, which is this leaf's bump check.

**Acceptance:**

- 5.11.1 - `yaml12.load` gives every vector's value, error message or non-string key. test: `tests/config/test_yaml12_vectors.py::test_vectors_match_loader`.
- 5.11.2 - `yaml::load` gives every vector's value, error message or non-string key. test: `crates/gruntime/tests/yaml12.rs::vectors_match_python`.
- 5.11.3 - An import through `replace_yaml` stores `on` as a string, and a non-string key and malformed YAML return their 422 bodies. test: `tests/servers/routes/test_config_yaml_replace.py::test_import_reads_yaml_1_2`.
- 5.11.4 - Each export vector equals `_dump`'s output and reads back unchanged through both `yaml.safe_load` and `yaml12.load`, including the strings only YAML 1.2 reads as numbers. test: `tests/config/test_yaml_export_vectors.py::test_vectors_match_python_dump`.
- 5.11.5 - Native export bytes equal every regenerated vector. test: `crates/gruntime/tests/export.rs::emitter_matches_python_vectors`.
- 5.11.6 - The regenerated gcore contract equals the checked-in bytes, so this leaf leaves gcore unchanged. test: `tests/config/test_runtime_config_contract.py::test_checked_in_contract_matches_registry`.
- 5.11.7 - `gdaemon` is one patch version above the executor's starting tip in its `Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`, so this leaf's cutover passes the release-identity preflight. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 5.12 Native `PUT /api/config/template` and `POST /api/config/import` [category: code] (depends: 5.11)
`kind: deliverable`

Targets:
- `src/gobby/servers/routes/configuration_templates.py::register_template_routes`
- `src/gobby/servers/routes/configuration_import_export.py::register_import_export_routes`
- `src/gobby/config/documents.py::ConfigDocumentsService._prepare`
- `crates/gcore/assets/config/runtime_config_contract.json::*` — scope-reason: regenerated after this leaf's `documents.py` edit, which must leave its bytes unchanged
- `tests/servers/routes/test_config_yaml_replace.py::*` — scope-reason: `test_yaml_revision_domain_and_exhaustion` asserts the new 422 detail body
- `tests/servers/routes/test_yaml_documents_corpus.py`
- `crates/gruntime/tests/fixtures/yaml_documents_corpus.json`
- `crates/gruntime/src/lib.rs`
- `crates/gruntime/src/documents.rs`
- `crates/gruntime/src/config_write.rs`
- `crates/gruntime/src/write_rules.rs`
- `crates/gruntime/src/validation/candidate.rs`
- `crates/gruntime/src/verify.rs`
- `crates/gruntime/tests/documents.rs`
- `crates/gdaemon/Cargo.toml`
- `src/gobby/install/version_pins.py::MANAGED_BIN_VERSION_PINS`
- `Cargo.lock`

Release ownership (Constraints): gruntime is in gdaemon's release closure, so this
leaf bumps `gdaemon` by 0.0.1 from the executor's starting tip. The regenerated gcore
contract keeps its bytes, so gcore does not change.

Rows: `PUT /api/config/template` and `POST /api/config/import`, both for `Operator`.
Compare mode proxies each once and runs `check_proxied` (D3). Python serves both
with `ConfigDocumentsService.replace_yaml`, and native serves both with one handler
in `documents.rs`.

**Request body.** Each Python route stops taking `ConfigDocumentRequest` as a
FastAPI body parameter and parses the body itself, as 5.9 does:

1. Decode the body as strict UTF-8, parse it with `json.loads` and validate it with
   `ConfigDocumentRequest.model_validate`. The body is an object with a required
   `expected_revision` (`ConfigRevision`, as in 5.9) and a required string
   `content`. Any other member fails.
2. Any failure returns 422 `{"detail": "invalid config document request"}`. The
   parse runs before the route's `try`, whose `except Exception` would otherwise
   answer 500.

Native parses with 5.7's `json.rs` and applies the same rules. `NaN` and `Infinity`
fail both members' types on both sides. A lone-surrogate escape inside `content` is
the documented divergence (Config validation): Python decodes it and PyYAML's reader
rejects the character with 5.11's `Invalid YAML at line 1, column 1`, while native
answers the 422 detail body.

**Handler.** Native ports `replace_yaml` and `_prepare` in Python's order:

1. Read the published row with 3.1's `active_state`. No row is 503
   `runtime_unavailable` `Configuration runtime is not ready`, retryable, as
   `_runtime_snapshot` answers.
2. Load `content` with 5.11's `yaml::load`. `NonStringKey(rendering)` is 422
   `validation_error` `Configuration keys must be strings` at `["content", ...]` the
   rendering split on `.`, and any other error is 422 `validation_error` with its
   message at `["content"]`. `Null` becomes an empty object. Any other non-object
   is 422 `validation_error` `YAML document must be a mapping` at `["content"]`.
3. Flatten as `flatten_config` does with the registry. Objects are walked in
   document order, and segments are joined with `.`:
   - When a pattern puts a dynamic segment right after the current prefix (5.9's
     `dynamic_segment_follows` port), the key must decode with 5.9's
     `decode_dynamic_segment` port. A failure is 422 `validation_error`
     `Dynamic configuration segment <key> under <prefix> is not canonically encoded: <reason>`
     at `["content"]`. `<key>` is `pystr::repr` of the key, `<prefix>` is
     `pystr::repr` of the joined prefix or `<root>` at the top level, and
     `<reason>` is one of 5.9's six texts.
   - An object whose joined key resolves to a registered key is a leaf
     (`_is_registered_leaf`). Any other non-empty object is walked, and an empty
     one is the leaf `{}`. Every other value is a leaf.
   - A joined key that repeats keeps its first position and takes the last value.
   - An ambiguous key met in the registered-leaf test is 422 `validation_error`
     `Ambiguous configuration key: <key>` at `["content"]`, as `_prepare` maps
     flatten's `ValueError`s.
4. For each flat key in order, resolve over the carrier's `specs` (5.8), as
   `_prepare`'s loop does:
   - An unknown key is 422 `validation_error` `Unknown configuration key <key>`.
     A spec without `sourcePath`, or with visibility `restricted`, is
     `Configuration key <key> is not daemon-owned`. A managed key is
     `Configuration key <key> requires managed activation`. `<key>` is
     `pystr::repr`, and each path is `["content", ...]` the key split on `.`
     (`_invalid_key`). An ambiguous key returns the route's 500.
   - A reference key (5.8's secrecy, per field for a pattern) follows the secret
     rules below.
   - A key with structured reference fields first records whether a list item
     holds `"********"` in a reference field. Its value then runs through
     `write_rules::restore_structured` against the row's `desired_values[key]`. A
     failure is 422 `validation_error` with its text at `["content", ...]` the key's
     split.
   - Any other key's value goes into the patch values, the validation values and
     the wire values.
5. Compute the changed values: wire values whose key is absent from the row's
   `desired_values` or differs under `json::py_eq`. The omitted keys are the
   `desired_values` keys absent from the wire values. Then run the evidence rules in
   Python's document order, which differs from the PATCH's (5.9):
   1. `write_rules::reject_unverified_evidence` over the changed values, with the
      document action `Re-import the exported evidence unchanged or activate endpoint '<segment>'`
      and path `["content", ...]` the key's split;
   2. `write_rules::clear_stale_evidence` with the omitted keys plus the wire keys
      whose value is `null` as the unset set, the secret updates and 5.9's
      `desired_config`. Each cleared key becomes `null` in the patch values, the
      validation values and the wire values;
   3. `write_rules::reject_unprobed_responses` over the changed values and the
      omitted keys, with the document message
      `Responses endpoint '<segment>' must be probed and activated before YAML import`,
      the action `Probe and activate endpoint '<segment>' before importing YAML`, and
      path `["content", ...]` the `wire_api` key's split or
      `["content", "ai", "generation", "endpoints", <segment>]`.

   `'<segment>'` is `pystr::repr`. `write_rules`' `document` flag selects these texts.
6. Validate the candidate with `candidate::document_candidate` (below). A failure is
   422 `validation_error` `Complete configuration candidate is invalid: <text>` at
   `["content"]`.
7. When step 4 restored a masked secret or a masked structured field and the row's
   revision is not `expected_revision`, return 409 `revision_conflict`
   `Configuration revision is stale` at `["expected_revision"]`, with
   `expected_revision` and the row's revision as `actual_revision`. A restoration
   reads the row, so it must share the commit's revision.
8. Commit the patch through `config_write::commit` with the daemon scope (below),
   source `user` and no internal access. Then wait as 5.9's Write wait does, and
   answer as `_reconcile` does.

**Secret rules** port `_prepare_secret` for a reference key's value:

- `null` or `""`: the validation and wire values are `null`, and the patch gets no
  value, so the scoped commit unsets the key.
- Not a string: `Secret configuration value must be a string`.
- `"********"` reads the row's `desired_values[key]`:
  - `null`, `""` or absent: the validation value is `null`, the wire value is that
    value, and the patch gets no value;
  - not a string: `Masked secret has no persisted value`;
  - a string without the `$secret:` prefix: a secret update with that plaintext and
    category `general`, with the string as the validation and wire value; this is a
    restoration;
  - a `$secret:` reference: the row's desired binding plaintext for the key, else
    `Persisted secret cannot be resolved`; the patch value and wire value are the
    reference and the validation value is the plaintext; this is a restoration.
- A string starting with `$secret:`: read the name through 3.3's `secret` after
  `SecretStore.get`'s normalization (`pystr::strip`, then `pystr::lower`). An empty
  name or no secret is `Secret reference cannot be resolved`. Otherwise the patch
  value and wire value are the reference and the validation value is the plaintext.
- Any other string: a secret update with category `general`, with the string as the
  validation and wire value.

Each failure is 422 `validation_error` at `["content", ...]` the key's split.

**`write_rules::restore_structured(key, value, desired, fields, identity)`** is the
restoration and `validate_structured_references` half of 5.9's
`prepare_structured`, which now reads the row and then calls it. The document handler
already holds the row, so it calls `restore_structured` directly.

**`candidate::document_candidate(values)`** ports
`ConfigRepository.runtime_candidate(overrides, {})`, which `configuration_context`
wires into the documents service: 5.8's steps 5 and 6 over the validation values in
their order, with no bindings. A reference key whose value is a `$secret:` string
fails with `Secret configuration key '<key>' has an empty reference` when the name is
empty, else with `Missing resolved secret binding for configuration key '<key>'`.
Such a value reaches the candidate only when a resolved or persisted plaintext itself
starts with `$secret:`. A validation failure gives 5.7's `detail`.

**Python, validation-messages site 4.** `_prepare` catches pydantic's
`ValidationError` from the candidate before its other exceptions and formats it with
`ConfigMutations._validation_detail(exc)`. `ConfigRepositoryError`, other
`ValueError`s and `TypeError` keep `str(exc)`. The
`Complete configuration candidate is invalid: ` prefix stays.

**Daemon scope.** `config_write::commit` and `commit_in` take a scope. The daemon
scope ports `ConfigMutations._scoped_patch` for namespace `daemon` and runs after
5.9's step 3, the locked snapshot, and before step 4, embedding admission:

- A requested key whose spec has no `sourcePath` is `Invalid`
  `Keys are outside configuration namespace 'daemon': <keys>`, with `pystr::repr` of
  the sorted keys and no key. Step 4 of the handler already rejects such keys, so
  this cannot fire from a document.
- Every stored override whose spec has a `sourcePath`, that the patch does not name,
  and that is not managed joins `unset`.

A stored key that resolves ambiguously is `Ambiguous { key }`. 5.9's PATCH and
5.10's save keep the unscoped commit.

**Errors** follow `replace_yaml` and the route:

- `Conflict`: 409 `revision_conflict` `Configuration revision is stale` at
  `["expected_revision"]`, with the request's `expected_revision` and the locked
  revision as `actual_revision`.
- `Exhausted`: 422 `revision_exhausted` `Configuration revision cannot be advanced`
  at `["expected_revision"]`.
- `Invalid { message, key }`: 422 `validation_error` with the message at the key
  split on `.`, with no `content` prefix, or at `[]` with no key. A reference key's
  failure reads `Secret configuration value is invalid`, as 5.8 maps it.
- `Blocked`: 409 `embedding_mutation_blocked` with its text at `["content"]`,
  retryable. Managed keys fail in step 4 and never join `unset`, so a document never
  touches an admission key.
- `Internal`, `Ambiguous` and every other ambiguous key, a desired tree that fails
  validation, and any database or secret-store failure: 5.9's 500
  `persistence_indeterminate` body. Python's `RegistryError` and `RuntimeError`s
  escape `replace_yaml` to the route's `except Exception`.

Every other error body is `ConfigValuesError.public_body`, as in 5.9, and `retryable`
is true for the 409s and the 503.

**Answer.** The 200 body is 5.9's, with the filter `_reconcile` applies: the pending
and failed-live keys keep only keys whose spec has a `sourcePath`. An unknown key is
dropped, and an ambiguous one returns the 500. A timeout or a row read error returns
5.9's `reconcile_failed` body.

**`check_proxied`.** `verify.rs` routes a claimed proxied document request to
`documents::check_proxied`, which reads `config_state`, `config_store` and the named
secrets in one read-only transaction:

- 200: `Skipped` when `config_state`'s revision differs from the body's `revision`.
  Otherwise the request's `content` is loaded and flattened as handler steps 2 and 3
  do, and each key in the body's `changed_keys` must match its stored row:
  - a key the document does not name has no row, except that a probe-evidence key
    may hold `null`, a cleared field;
  - a named reference key with a plaintext value holds
    `$secret:<config_key_to_secret_name(key)>`, and `secret` of that name returns the
    plaintext; with a `$secret:` value it holds that reference; with `"********"` it
    has no row or holds a `$secret:` reference; with `null` or `""` it has no row;
  - a named structured key's row carries the body's revision;
  - a named probe-evidence key holds `null` or
    `json::dumps(validate_value(key, value))`;
  - any other named key's row holds `json::dumps(validate_value(key, value))` at the
    body's revision.

  The first failing key gives `Mismatch("/changed_keys")`.
- 422: `Skipped` when `config_state`'s revision differs from the request's
  `expected_revision`. Otherwise a native dry run, every handler step before 5.9's
  commit step 8 with no lock and no write, must return the same body, else
  `Mismatch` at the first differing pointer.
- Any other status: `Skipped`.

**Corpus.** Each case in `crates/gruntime/tests/fixtures/yaml_documents_corpus.json`
holds `before`, `request` (the raw body text), `published` and `expected`, as 5.9's
corpus does. The cases cover:

- invalid bodies: invalid UTF-8, a UTF-8 byte-order mark, text that is not JSON, an
  array, a missing `content`, a numeric `content`, an unknown member, and a boolean,
  float, negative and out-of-range `expected_revision`;
- each error under Handler, Secret rules, Daemon scope and Errors: no published row;
  malformed YAML and a non-string key; a list document; a non-canonical dynamic
  segment at the root and under a prefix; unknown, restricted, sourceless and managed
  keys; each secret-rule failure; a structured item with a duplicate identity and
  one with a masked field and no persisted reference; unverified evidence; an
  unprobed responses endpoint, named and omitted; a candidate failure from a
  validator, from a `$secret:` plaintext with an empty name and from one with a name;
  a masked restoration against a stale row; a stale locked revision; an exhausted
  revision;
- accepted documents: an empty document that unsets every unmanaged daemon override
  while a managed override stays; the document export returns; one value given by
  nested and by dotted keys; a repeated key; a registered object leaf and an empty
  object; a plaintext secret, a `$secret:` reference and masked secrets of both
  stored kinds; masked and plaintext structured items; an endpoint identity change
  that writes `null` evidence rows; a key equal to its default;
- `applied`, `pending_restart` with a sourceless pending key filtered out,
  `failed_live` and `reconcile_failed` answers.

The Python test builds a fresh database per case, as 5.9's corpus test does, and
seeds `before`. It builds `ConfigDocumentsService` as `configuration_context` does,
with real `ConfigMutations`, `ConfigRepository.runtime_candidate` and `SecretStore`,
and with 5.9's fake runtime. It sends each case through `register_template_routes`'
PUT and, on a fresh seed, through `register_import_export_routes`' POST, and asserts
`expected` for both. The Rust test does the same through the native handler on both
routes.

Research context:
- Python: `src/gobby/servers/routes/configuration_templates.py::register_template_routes`
  (`save_config_template`),
  `src/gobby/servers/routes/configuration_import_export.py::register_import_export_routes`
  (`import_config`, `_indeterminate_response`) and
  `src/gobby/servers/routes/configuration_models.py::ConfigDocumentRequest`
  (`extra="forbid"`). `src/gobby/servers/routes/configuration_context.py` builds the
  service with `runtime_candidate=lambda overrides: repository.runtime_candidate(overrides, {})`
  and `resolve_secret=secret_store.get`.
- `src/gobby/config/documents.py`: `ConfigDocumentsService.replace_yaml`,
  `._runtime_snapshot`, `._prepare`, `._prepare_secret`,
  `._prepare_structured_references`, `._has_masked_structured_reference`,
  `._reconcile`, `._is_daemon_key` and `._invalid_key`.
- `src/gobby/storage/config_store.py::flatten_config` and `_is_registered_leaf`;
  `src/gobby/config/registry.py::ConfigRegistry.resolve` raises `RegistryError`
  `Ambiguous configuration key: <key>`, a `ValueError`.
- `src/gobby/storage/config_mutations.py`: `ConfigMutations.replace_namespace`,
  `._apply`, `._scoped_patch` and `._key_in_namespace`;
  `src/gobby/storage/config_repository.py::ConfigRepository.runtime_candidate` and
  `._secret_reference`.
- `src/gobby/config/values.py`: `reject_unverified_generation_endpoint_evidence`,
  `clear_stale_generation_endpoint_probe_evidence` and
  `reject_unprobed_responses_endpoints` with `document=True`. `clear_stale` writes
  `None` into `values` for each cleared key outside the unset set.
- `replace_yaml` maps `ConfigConflictError`, `ConfigRevisionExhaustedError`,
  `ConfigValidationError` and `EmbeddingConfigMutationBlocked`; everything else
  reaches the route's `except Exception`.
- Planned checks:
  - `cargo nextest run -p gobby-runtime --test documents`;
  - `DATABASE_URL=… GOBBY_TEST_PROTECT=1 uv run pytest tests/servers/routes/test_yaml_documents_corpus.py tests/servers/routes/test_config_yaml_replace.py tests/config/test_runtime_config_contract.py -v`.

Consumers unchanged:
- `src/gobby/servers/routes/configuration.py` — no-edit-reason: registers `register_template_routes` and `register_import_export_routes` with unchanged signatures.
- `src/gobby/cli/install_setup_gclient.py` — no-edit-reason: reads only the `gclient` pin, which no leaf of this plan bumps.
- `src/gobby/cli/install_setup_gcode.py` — no-edit-reason: reads the `gcode` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_ghook.py` — no-edit-reason: reads the `ghook` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_gterm.py` — no-edit-reason: reads only the `gterm` pin, which no leaf of this plan bumps.
- `src/gobby/cli/install_setup_versions.py` — no-edit-reason: looks a pin up by the binary name it is given.
- `src/gobby/code_index/gcode_gateway.py` — no-edit-reason: derives `MIN_GCODE_GRAPH_VERSION` from the `gcode` pin at import.
- `src/gobby/hooks/runtime_compat.py` — no-edit-reason: derives `MINIMUM_GHOOK_VERSION_FOR_SUPPORTED_SCHEMA` from the `ghook` pin at import.
- `src/gobby/install/bin_freshness_models.py` — no-edit-reason: iterates every pin, so the bumped values flow through.
- `src/gobby/install/distribution.py` — no-edit-reason: looks a pin up by binary name.
- `tests/cli/installers/test_grok_installer.py` — no-edit-reason: reads the `ghook` pin instead of a version literal.
- `tests/cli/test_install_ghook.py` — no-edit-reason: builds its fixtures from the `ghook` pin instead of a version literal.
- `tests/cli/test_install_setup.py` — no-edit-reason: reads the `gcode` pin instead of a version literal.
- `tests/cli/test_install_setup_gclient.py` — no-edit-reason: reads the `gclient` pin instead of a version literal.
- `tests/cli/test_install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin instead of a version literal.
- `tests/cli/test_install_setup_gterm.py` — no-edit-reason: reads the `gterm` and `gclient` pins instead of version literals.
- `tests/code_index/test_gcode_gateway.py` — no-edit-reason: reads the `gcode` pin instead of a version literal.
- `tests/hooks/test_runtime_compat.py` — no-edit-reason: compares the derived floor to the `ghook` pin instead of a version literal.
- `tests/install/test_distribution.py` — no-edit-reason: reads pins by binary name; its `0.1.0` literal is a fake `gcode` output, not a pin.
- `tests/install/test_version_pins.py` — no-edit-reason: compares each pin to its crate manifest, which is this leaf's bump check.

**Acceptance:**

- 5.12.1 - Every corpus case matches Python's template PUT and import POST: status, body, resulting rows, secrets and notification. test: `tests/servers/routes/test_yaml_documents_corpus.py::test_cases_match_python_routes`.
- 5.12.2 - Python answers a boolean, float, negative or out-of-range `expected_revision` with 422 `{"detail": "invalid config document request"}`, and an exhausted revision with the `revision_exhausted` body. test: `tests/servers/routes/test_config_yaml_replace.py::test_yaml_revision_domain_and_exhaustion`.
- 5.12.3 - The native template PUT and import POST give every corpus case's status, body, rows, secrets and notification. test: `crates/gruntime/tests/documents.rs::cases_match_python`.
- 5.12.4 - A native document replace waits on the advisory lock a Python mutation holds, unsets every stored override that has a `sourcePath`, is not named and is not managed, and keeps managed and sourceless overrides. test: `crates/gruntime/tests/documents.rs::replace_scopes_to_daemon_keys`.
- 5.12.5 - `check_proxied` verifies a Python-committed document and a Python 422, reports a tampered stored value as a mismatch, and skips when the revision moved. test: `crates/gruntime/tests/documents.rs::check_proxied_verifies_python_documents`.
- 5.12.6 - The regenerated gcore contract equals the checked-in bytes, so this leaf leaves gcore unchanged. test: `tests/config/test_runtime_config_contract.py::test_checked_in_contract_matches_registry`.
- 5.12.7 - `gdaemon` is one patch version above the executor's starting tip in its `Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`, so this leaf's cutover passes the release-identity preflight. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 5.13 Native generation-endpoint probe client [category: code] (depends: 5.12)
`kind: deliverable`

Targets:
- `crates/gruntime/Cargo.toml`
- `crates/gruntime/src/lib.rs`
- `crates/gruntime/src/pystr.rs`
- `crates/gruntime/src/probe/mod.rs`
- `crates/gruntime/src/probe/codex.rs`
- `crates/gruntime/src/probe/app_server.rs`
- `crates/gruntime/src/probe/chat.rs`
- `crates/gruntime/tests/endpoint_probe.rs`
- `crates/gruntime/tests/fixtures/endpoint_probe_corpus.json`
- `crates/gruntime/tests/fixtures/probe_fakes.py`
- `tests/ai/test_endpoint_probe_corpus.py`
- `crates/gdaemon/Cargo.toml`
- `src/gobby/install/version_pins.py::MANAGED_BIN_VERSION_PINS`
- `Cargo.lock`

Release ownership (Constraints): gruntime is in gdaemon's release closure, so this
leaf bumps `gdaemon` by 0.0.1 from the executor's starting tip. No Python
production file changes.

This leaf ports the probe chain that
`PUT /api/config/generation-endpoints/{endpoint_name}/activate` runs before it
persists an endpoint. It adds no route; 5.14 calls it. The chain talks to the
endpoint through the Codex CLI, the Codex app-server, or an OpenAI-compatible,
LM Studio, Ollama or vLLM server, so parity is proved at the wire: Python's chain
and the native chain run one scenario corpus against one set of fakes, and the
fakes' logs must be equal (Fakes and corpus).

**Interface (`probe/mod.rs`).**

- `ProbeEndpoint { name, protocol, wire_api, api_base, model, api_key, tool_chat }`
  is the validated endpoint 5.14 builds. `api_base` is the stripped value
  `validate_api_base` returns, and `api_key` is the resolved plaintext or `None`.
- `Host { env, home, sleep }` carries what Python reads from the process:
  - `env` is the base environment, ordered pairs of `OsString`. Children inherit
    it, and `codex` resolves on its `PATH`.
  - `home` is the Gobby home that `get_gobby_home()` returns.
  - `sleep` is the retry sleep, an
    `Arc<dyn Fn(Duration) -> Pin<Box<dyn Future<Output = ()> + Send>> + Send + Sync>`.

  `Host::process(home)` takes `std::env::vars_os()` and `tokio::time::sleep`. Tests
  build their own, which is how they inject the fake `codex` and record sleeps
  without touching the test process's environment.
- `probe(host, endpoint, timeout_seconds) -> Result<ProbeOutcome, ProbeError>`
  runs the Responses chain when `wire_api` is `responses`, else the
  chat-completions chain. `timeout_seconds` is the candidate config's
  `ai.generation.timeout_seconds`.
- `ProbeOutcome { probed_model, input_modalities, probed_json, probed_tools, diagnostics }`
  mirrors `EndpointActivationResult`: `probed_tools` is `None` when `tool_chat` is
  false, and `diagnostics` maps `json`, `tools` and `vision` to the degraded
  probes' messages in that order.
- `ProbeError(String)` carries every failure. Python's route answers each one with
  422 `{"detail": <message>}`: `EndpointActivationError`, `ValueError` and, after
  5.14's fix, `LocalModelError`.

**Chain.** `probe` ports `probe_responses_endpoint` and
`probe_chat_completions_endpoint` in `src/gobby/ai/endpoint_activation.py`.

The Responses chain first ports `codex_endpoint_env`, outside the deadline. With no
key, or a key that starts with `$secret:`, it fails with
`Generation endpoint API key is unavailable; configure its referenced secret`.

Both chains then run under one deadline of `timeout_seconds`.
`Duration::try_from_secs_f64` gives the deadline; a value too large for `Duration`
runs with no deadline, as Python's `asyncio.timeout` then never fires. On expiry the
chain fails with `<Transport> endpoint activation timed out after <t> seconds`.
`<Transport>` is `Responses` or `Chat-completions`, and `<t>` is
`pystr::format_g(timeout_seconds)`. Inside the deadline:

1. The chat chain resolves the model first (vLLM catalog under Chat adapters). A
   catalog failure is a `ProbeError` with the catalog's text, unsanitized, as
   Python lets `LocalModelError` escape the chain.
2. The text probe is fatal. Its failure becomes the sanitized error below.
3. The JSON probe is degraded: a failure sets `probed_json` false and records
   `diagnostics["json"]`.
4. With `tool_chat` true, the tools probe is degraded the same way, setting
   `probed_tools` false and recording `diagnostics["tools"]`. With `tool_chat`
   false, `probed_tools` is `None` and the probe does not run.
5. The vision probe is degraded. Success gives `input_modalities`
   `["text", "image"]`; failure gives `["text"]` and records
   `diagnostics["vision"]`.

`probed_model` is `endpoint.model` for Responses and the resolved model for chat.

Every probe runs through the port of `_retry_activation`:

- at most three attempts;
- a failure is retried only when its text matches `\b(?:429|5\d\d)\b`;
- the delay is `min(<n>, 60.0)` when the text matches
  `retry-after\s*[:=]\s*(\d+(?:\.\d+)?)` (case-insensitive), else 1.0 seconds,
  awaited through `host.sleep`.

`_redact` replaces each occurrence of a non-empty `api_key` with `[REDACTED]`.
`_sanitized_activation_error` returns
`<Transport> endpoint authentication failed; verify the configured secret` when the
redacted text matches `\b(?:401|403)\b|auth(?:entication|orization)?|api key`
(case-insensitive), else `<Transport> endpoint activation failed: <redacted text>`.
A degraded probe's diagnostic is its redacted text, and `tracing::warn!` logs it as
`_degraded_probe` does. The patterns use the `regex` crate, whose `\b` is Unicode
aware, as Python's is for `str` patterns. `regex` is already a gruntime dependency
(5.5).

**Probe failure texts.** A failure carries one of two kinds of text:

- Gobby's own messages, which must match byte for byte. Examples:
  `Responses text probe returned an unexpected response`,
  `Codex CLI failed with exit code 3: <stderr>`, `RPC error <code>: <message>`,
  `Responses thread start returned modelProvider='openai'; expected 'gobby_endpoint_x'`,
  `Endpoint adapter has no client for tool probing`, every `LocalModelError` text
  except the connect error, and the vision texts. The vision text quotes
  `text.strip()[:80]` with `pystr::repr`, cutting at 80 code points.
- A message that embeds an exception raised inside httpx, the OpenAI SDK, `json` or
  process spawning. These keep gobby's prefix, and the embedded text is a
  documented divergence (Endpoint probes).

Classification still matches on both sides. Native writes HTTP status failures with
the status code in the text, so the authentication, retry and retry-after patterns
reach the same verdict:

- the OpenAI-compatible client writes `Error code: <status> - <body text>`, as the
  SDK's `APIStatusError` does, except that the SDK renders a JSON body as a Python
  `repr`;
- LM Studio and Ollama write httpx's `raise_for_status` text,
  `<Client error|Server error|Redirect response|Informational response> '<status> <reason>' for url '<url>'`
  followed by its `For more information check:` line, with the canonical reason
  phrase.

A transport error's text is the `reqwest` error with its URL removed
(`without_url`), followed by its source chain joined with `: `. That keeps a host
name containing `auth` from reaching the authentication pattern.

A reply that carries `NaN` or `Infinity` parses under `json.loads` but not under
5.7's `json::parse`, so natively it fails the JSON probe. This extends the
documented `NaN` divergence (Config validation).

**Codex (`probe/codex.rs`)** ports `src/gobby/ai/codex_endpoint.py`:

- `codex_endpoint_provider_id` is `gobby_endpoint_<name>`.
- `codex_endpoint_display_name` gives `OpenRouter` for `openrouter`. For any other
  name it replaces `-` and `_` with spaces, then uppercases each letter that does
  not follow a letter. That equals `str.title` here, because
  `_GENERATION_ENDPOINT_NAME_RE` (`^[a-z0-9][a-z0-9_-]*$`) admits only ASCII.
- `codex_endpoint_config_overrides` quotes each value with `json::dumps` of a
  string, as `json.dumps` does.
- `codex_endpoint_env` and `codex_endpoint_app_server_env`. `CODEX_HOME` is
  `<home>/codex-endpoints/<name>`, created with its parents.
- `codex_event_text`.

The vLLM branches (`_vllm_config_overrides`, `codex_vllm_provider_id` and the keyless
vLLM environment) are not ported. `GenerationEndpointConfig.validate_endpoint`
requires `protocol` `openai-compatible` for `wire_api` `responses`, so activation
never reaches them.

It also ports the CLI path, `CodexCLITextGenerateAdapter.build_command` and
`.generate` with `_run_cli_text_generation_command`, for the text and JSON probes:

1. `codex` resolves as `shutil.which` does: the first entry of the host
   environment's `PATH` that holds an executable regular file named `codex`. With
   none, the probe fails with `Codex CLI not found in PATH`.
2. The argv is `build_command`'s, with the probe's `model` override.
3. stdin is `ONE_SHOT_DIRECTIVE`, a blank line and the prompt, as
   `_with_one_shot_directive` and `_compose_prompt` build it.
4. The working directory is a fresh temporary directory. It also holds
   `last-message.txt`.
5. The environment is the host environment plus the overrides, without
   `GOBBY_AGENT_API_TOKEN`, with `GOBBY_HOOKS_DISABLED=1`.
6. The child runs in its own process group (`process_group(0)`), the native
   equivalent of `start_new_session`.
7. After 120 seconds, the child is cleaned up and the probe fails with
   `Codex CLI timed out after 120s: <command>`. `<command>` is `shlex.join` of the
   argv: an argument made only of ASCII letters, digits and `@%+=:,./-` stays bare,
   an empty one becomes `''`, and any other is single-quoted with each `'` written
   as `'"'"'`.
8. A nonzero exit fails with `Codex CLI failed with exit code <n>: <stderr or stdout>`.
   Output is decoded with `from_utf8_lossy`, which matches
   `bytes.decode(errors="replace")`, and passed through `pystr::strip`.
9. The result is `last-message.txt`, stripped.

Cleanup ports `_cleanup_cli_process`: send `SIGTERM` to the group, wait 2 seconds,
send `SIGKILL` to the group, wait 2 seconds. When the deadline drops the probe
future, the child's guard runs the same cleanup on a spawned task, as Python's
cancellation path does. The group signals use `nix::sys::signal::killpg` with
`nix::unistd::Pid`. gruntime inherits the workspace's `unsafe_code = "forbid"`
(3.1), so it makes no raw `libc` call. Rejected: `libc::killpg`, which needs an
`unsafe` block that the inherited lint refuses.

The text probe sends `Reply with exactly GOBBY_K3_TEXT_OK.` and requires the token
in the reply. The JSON probe sends `Reply with JSON object {"ok": true} and nothing else.`
and requires an object, as `_probe_text` and `_probe_json` do.

**App-server (`probe/app_server.rs`)** ports the slice of `CodexAppServerClient`
that the probes use. The sources are `src/gobby/adapters/codex_impl/client.py`,
`client_lifecycle.py`, `client_rpc.py`, `client_api.py` and
`client_notifications.py`.

- **Start.** Spawn `codex app-server` with `-c` per override and stdio pipes. The
  environment follows the CLI rules with `codex_endpoint_app_server_env` as the
  overrides. Send `initialize` with `clientInfo`
  `{"name": "gobby-daemon", "title": "Gobby Daemon", "version": "0.1.0"}`, then the
  `initialized` notification. A spawn or initialize failure fails with
  `Failed to start Codex app-server: <text>; stderr: <stderr>`, with each
  environment value whose name contains `KEY`, `TOKEN` or `SECRET` redacted.
- **Messages.** Each message is one line of `json::dumps`, with request ids counting
  from 1 per process. Each request waits up to 60 seconds for its response. An error
  response fails with `RPC error <code>: <message>`, and a process exit fails the
  pending requests.
- **Incoming requests.** A request the server sends gets the `-32601` error
  `No handler registered for <method>`, since the probes register no handler.
- **Threads.** `thread/start` takes `model`, approval policy `never`, sandbox
  `read-only` and, for vision, `ephemeral`. `thread/resume` takes the thread id.
- **Turns.** `turn/start` takes the thread id and its text and `localImage` input.
  Events are read until `turn/completed`, `turn/failed` or `thread/closed`, as
  `run_turn` does; a failed turn fails with `Codex turn failed: <error>`.
- **Stop.** `SIGTERM` through `nix::sys::signal::kill`, a 5-second wait, then
  `SIGKILL`. The guard also stops the process when the deadline drops the future.

It also ports the probes built on that client:

- `_collect_turn` and `_assert_thread_provider`.
- The tools probe, `_probe_tool_context_and_resume`, with its three prompts,
  `_PROBE_TOKEN` and its four failure texts. It covers a tool turn, a second-turn
  context check, then a resume in a new process.
- The vision probe, `_probe_vision`. It runs an ephemeral thread whose turn carries
  the probe image and checks the reply. It then runs
  `CodexEndpointVisionExtractAdapter.extract` in a further app-server process, with
  the prompt `Describe the solid-color image.` That step fails with
  `Codex Responses vision extraction produced no text` on an empty reply.

**Chat adapters (`probe/chat.rs`)** port `create_local_provider_adapter` and the
three adapters in `src/gobby/llm/local_provider_adapters.py`, with each one's
`generate_text_result` and `generate_json`:

- **`openai-compatible` and `vllm`** use `OpenAICompatibleLocalProviderAdapter`.
  - It sends `POST <base>/chat/completions`. `<base>` is `api_base`, or
    `vllm_api_base(api_base)` for vLLM.
  - It adds `Authorization: Bearer <key>` only for a non-empty key.
  - Timeouts are 5 seconds to connect and 120 seconds to read. It makes no retries
    and follows up to 20 redirects, as the SDK's httpx client does.
  - Images become `image_url` data-URL parts placed before the text.
  - JSON mode sends `response_format` `json_object` with no fallback.
  - A blank reply and a JSON parse failure keep their texts. `_strip_json_fences`
    and `_parse_json_response` are ported.
- **`lmstudio`** uses `LMStudioLocalProviderAdapter`: `POST <origin>/api/v1/chat`
  with the `input`, `system_prompt`, `stream`, `store` and `max_output_tokens`
  members, a 300-second timeout and `_lmstudio_text`.
- **`ollama`** uses `OllamaLocalProviderAdapter`: `POST <origin>/api/chat` with
  `messages`, `stream`, `keep_alive`, `options.num_predict`, base64 `images` and
  `format: "json"` for JSON, a 300-second timeout and `_ollama_text`.

LM Studio and Ollama follow no redirects, and both answer a non-object body with
their own text. The tools probe ports `_probe_chat_tools`:

- for OpenAI-compatible endpoints it sends one `chat/completions` request with
  `_CHAT_PROBE_TOOL`, the forced `tool_choice` and `max_tokens` 64, and fails with
  `Chat-completions tool probe returned no tool call` when the first choice has no
  tool call;
- LM Studio and Ollama have no client, so the probe fails with
  `Endpoint adapter has no client for tool probing`.

`_probe_chat_text`, `_probe_chat_json` and `_probe_chat_vision` keep their prompts,
system prompts and `max_tokens` 64.

The vLLM catalog ports `vllm_api_base`, `vllm_models_url`, `vllm_served_model_ids`,
`parse_vllm_served_records(...).model_ids()` and `select_vllm_served_model` in
`src/gobby/agents/local_model.py`. It sends `GET` to the models URL with a
10-second timeout, no redirects and the key when set. Its timeout, status,
invalid-JSON, `auto` and not-served texts match byte for byte. The connect error
embeds transport text.

**Probe image (`probe/mod.rs`).** `_probe_color` picks red, green or blue with
`rand`'s `OsRng`; `rand` 0.8 is already locked. `_solid_color_png` becomes a
64×64 8-bit RGB PNG written by the `png` crate (0.17, already locked through
gterminal) with filter `NoFilter`. The bytes may differ from Python's `zlib`
output, and the fakes check the decoded pixels, not the bytes. The image is written
as `probe.png` in a fresh temporary directory, where Python uses the prefix
`gobby-endpoint-vision-`. `_check_vision_probe_reply` and
`_vision_probe_reply_matches` are ported: a reply passes when the set of probe-color
words among its lowercase `[a-z]+` runs is exactly the expected color.

**`pystr.rs`** gains `format_g(f64)`, Python's `format(x, "g")` for finite values.
It uses six significant digits. When the decimal exponent lies in −4 to 5 it writes
fixed notation, else `<mantissa>e<sign><two or more digits>`, with trailing zeros
and a trailing point removed. `timeout_seconds` has only `gt=0`, so `1200`, `0.5`
and `1e+07` are all reachable.

**Dependencies.** gruntime's manifest adds:

- `reqwest` 0.12 with `default-features = false` and `json` and `rustls-tls`, as
  gclient already declares it;
- `png` 0.17, `base64` 0.22, `rand` 0.8 and `tempfile` 3;
- `nix` 0.29 for Unix, with `default-features = false` and feature `signal`,
  which enables `process`; 0.29.0 is locked through `mac_address`;
- tokio's `process`, `io-util`, `fs` and `time` features.

All are in `Cargo.lock` already, so the lock changes only gruntime's dependency
list and gdaemon's version. Rejected: a hyper client, which needs hand-written
redirect and timeout handling that `reqwest` already provides, and gcore's
blocking `reqwest`, which would block a tokio worker for up to 300 seconds.

**Fakes and corpus.** `crates/gruntime/tests/fixtures/probe_fakes.py` is one
standard-library program both test suites run, so equal logs show equal client
behavior:

- `probe_fakes.py codex <case dir> <argv...>` is the fake Codex. Each test writes a
  shim named `codex` into a temporary `bin` directory:

  ```sh
  #!/bin/sh
  exec uv run --quiet --no-project python <repo>/crates/gruntime/tests/fixtures/probe_fakes.py codex <case dir> "$@"
  ```

  Python puts that directory first on `PATH` with `monkeypatch`. Rust puts it in
  `Host.env`.
- `probe_fakes.py serve <case dir>` is the fake HTTP server. It serves on
  `127.0.0.1:0`, writes its port to `<case dir>/port`, and the test points
  `api_base` at it.

Each case's `scenario` scripts the fakes. The n-th `codex` process takes entry n of
`codex`, and the n-th HTTP request takes entry n of `http`:

- An `exec` entry gives a delay, `stdout`, `stderr`, an exit code and the
  `last_message` the fake writes to the `--output-last-message` path.
- An `app-server` entry gives steps in order. Each step names the method it expects
  next. For a request it gives a delay, notifications to send first, server
  requests to send first, and a `result` or an `error`.
- An `http` entry gives a delay, a status, headers and a JSON or text body.

In scripted text, `{color}` becomes the color decoded from the probe image in that
request, or `none` when no valid probe image arrived.

The fakes append to `<case dir>/log.jsonl`:

- each `codex` process's argv after the program name, with the
  `--output-last-message` value reduced to its file name;
- its environment subset: `GOBBY_CODEX_ENDPOINT_API_KEY`, `CODEX_HOME` relative to
  the case's home, `GOBBY_HOOKS_DISABLED`, and whether `GOBBY_AGENT_API_TOKEN` is
  set;
- its stdin text;
- each JSON-RPC line received, parsed;
- each HTTP request's method, path, `Authorization` header and parsed body.

A string that is a probe image becomes `"<probe image>"`, or `"<invalid image>"`
when it is not a 64×64 8-bit RGB PNG of one probe color. That covers a path to a
PNG file, a `data:image/png;base64,` URL and a bare base64 PNG. Each fake process
also writes its pid to `<case dir>/pids`.

Each case in `crates/gruntime/tests/fixtures/endpoint_probe_corpus.json` holds:

- `endpoint`, `timeout_seconds`, `base_env` (`GOBBY_AGENT_API_TOKEN` set) and
  `scenario`;
- `expected`:
  - the outcome, either the five `ProbeOutcome` fields or an error text;
  - the log;
  - the retry delays.

A text that embeds library exception text is written as `{"prefix": "<gobby's prefix>"}`,
and only the prefix is compared; any other text is compared exactly.

The cases cover:

- for each protocol and wire API:
  - full success;
  - each probe's degraded failure, with `tool_chat` true and false;
  - the fatal text failure with an authentication status and with another
    status;
- a Codex exit failure and a missing `codex`;
- an app-server RPC error, a failed turn, a wrong `modelProvider` at start and at
  resume, an incoming server request and an app-server that exits during a turn;
- a 429 retried after `retry-after: 2`, a 503 retried after the 1.0-second
  default, and three transient failures that exhaust the attempts;
- a vision reply naming two colors, the wrong color and no text;
- vLLM `auto` with one, zero and two served models, an explicit model not served,
  each catalog failure, and a keyless vLLM endpoint;
- an API key echoed in an error, redacted;
- a chain timeout with `timeout_seconds` 1 against a fake step that sleeps 5
  seconds.

The Python test runs `probe_responses_endpoint` or
`probe_chat_completions_endpoint` with a `DaemonConfig` that holds the endpoint and
`timeout_seconds`, and sets each process input through `monkeypatch`:

- `PATH` and `GOBBY_AGENT_API_TOKEN` in the environment;
- `gobby.ai.codex_endpoint.get_gobby_home`;
- `endpoint_activation.asyncio`, replaced by a proxy whose `sleep` records the
  delay and yields with `sleep(0)`.

The Rust test runs `probe` with a `Host` built from the same inputs and a recording
`sleep`. Both assert the outcome, the log and the delays. Both then wait up to 10
seconds for every pid in `pids` to exit, which covers the timeout case's cleanup.
The g-format vectors in the corpus come from Python's `format(x, "g")` and run
through `pystr::format_g`.

Research context:
- `src/gobby/ai/endpoint_activation.py`: `probe_responses_endpoint`,
  `probe_chat_completions_endpoint`, `_retry_activation`, `_probe_text`,
  `_probe_json`, `_collect_turn`, `_assert_thread_provider`,
  `_probe_tool_context_and_resume`, `_probe_vision`, `_redact`, `_degraded_probe`,
  `_sanitized_activation_error`, `_resolve_probe_model`, `_probe_chat_text`,
  `_probe_chat_json`, `_probe_chat_tools`, `_probe_chat_vision`, `_probe_color`,
  `_solid_color_png`, `_VISION_PROBE_PROMPT`, `_vision_probe_reply_matches`,
  `_check_vision_probe_reply` and `_CHAT_PROBE_TOOL`.
- `src/gobby/ai/codex_endpoint.py` (all functions);
  `src/gobby/ai/_text_generation_adapters.py`:
  `CodexCLITextGenerateAdapter`, `_run_cli_text_generation_command` and
  `_cleanup_cli_process` (`_CLI_PROCESS_CLEANUP_TIMEOUT_SECONDS` is 2.0);
  `src/gobby/ai/_text_generation_helpers.py`: `ONE_SHOT_DIRECTIVE`,
  `_with_one_shot_directive`, `_compose_prompt` and `_decode`;
  `src/gobby/utils/spawn.py::create_session_exec`.
- `src/gobby/adapters/codex_impl/`: `client.py::CodexAppServerClient` (default
  `codex_command` `"codex"`, no `experimental_api`), `client_lifecycle.py`
  (`start`, `stop`), `client_rpc.py` (`next_request_id`, `send_request` with
  `"jsonrpc": "2.0"`, `handle_incoming_request`, `read_loop`), `client_api.py` and
  `client_notifications.py`. `src/gobby/ai/vision.py::CodexEndpointVisionExtractAdapter`.
- `src/gobby/llm/local_provider_adapters.py` (timeouts in `_LOCAL_OPENAI_TIMEOUT`,
  `_LOCAL_OPENAI_MAX_RETRIES` 0); the OpenAI SDK 2.15.0 sets
  `follow_redirects=True` and formats `Error code: <status> - <body>`. httpx's
  `AsyncClient` follows no redirects by default.
- `src/gobby/agents/local_model.py`: `LocalModelError` (an `Exception`, not a
  `ValueError`), `vllm_served_model_ids`, `select_vllm_served_model` and
  `resolve_vllm_served_model`.
- `src/gobby/config/ai.py`: `_GENERATION_ENDPOINT_NAME_RE` and
  `GenerationConfig.timeout_seconds` (default 1200.0, `gt=0`).
- gdaemon is tokio, axum and hyper. gclient already uses async `reqwest` with
  `json` and `rustls-tls`, and gdaemon's tests already run `uv run python`.
- Planned checks:
  - `cargo nextest run -p gobby-runtime --test endpoint_probe`;
  - `cargo clippy -p gobby-runtime`, which compiles gruntime under the inherited
    `unsafe_code = "forbid"`;
  - `GOBBY_TEST_PROTECT=1 uv run pytest tests/ai/test_endpoint_probe_corpus.py -v`.

Consumers unchanged:
- `src/gobby/cli/install_setup_gclient.py` — no-edit-reason: reads only the `gclient` pin, which no leaf of this plan bumps.
- `src/gobby/cli/install_setup_gcode.py` — no-edit-reason: reads the `gcode` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_ghook.py` — no-edit-reason: reads the `ghook` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_gterm.py` — no-edit-reason: reads only the `gterm` pin, which no leaf of this plan bumps.
- `src/gobby/cli/install_setup_versions.py` — no-edit-reason: looks a pin up by the binary name it is given.
- `src/gobby/code_index/gcode_gateway.py` — no-edit-reason: derives `MIN_GCODE_GRAPH_VERSION` from the `gcode` pin at import.
- `src/gobby/hooks/runtime_compat.py` — no-edit-reason: derives `MINIMUM_GHOOK_VERSION_FOR_SUPPORTED_SCHEMA` from the `ghook` pin at import.
- `src/gobby/install/bin_freshness_models.py` — no-edit-reason: iterates every pin, so the bumped values flow through.
- `src/gobby/install/distribution.py` — no-edit-reason: looks a pin up by binary name.
- `tests/cli/installers/test_grok_installer.py` — no-edit-reason: reads the `ghook` pin instead of a version literal.
- `tests/cli/test_install_ghook.py` — no-edit-reason: builds its fixtures from the `ghook` pin instead of a version literal.
- `tests/cli/test_install_setup.py` — no-edit-reason: reads the `gcode` pin instead of a version literal.
- `tests/cli/test_install_setup_gclient.py` — no-edit-reason: reads the `gclient` pin instead of a version literal.
- `tests/cli/test_install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin instead of a version literal.
- `tests/cli/test_install_setup_gterm.py` — no-edit-reason: reads the `gterm` and `gclient` pins instead of version literals.
- `tests/code_index/test_gcode_gateway.py` — no-edit-reason: reads the `gcode` pin instead of a version literal.
- `tests/hooks/test_runtime_compat.py` — no-edit-reason: compares the derived floor to the `ghook` pin instead of a version literal.
- `tests/install/test_distribution.py` — no-edit-reason: reads pins by binary name; its `0.1.0` literal is a fake `gcode` output, not a pin.
- `tests/install/test_version_pins.py` — no-edit-reason: compares each pin to its crate manifest, which is this leaf's bump check.

**Acceptance:**

- 5.13.1 - Python's probe chains give every corpus case's outcome, fake log and retry delays, and leave no fake process running. test: `tests/ai/test_endpoint_probe_corpus.py::test_cases_match_python_probes`.
- 5.13.2 - The corpus's g-format vectors equal Python's `format(x, "g")`. test: `tests/ai/test_endpoint_probe_corpus.py::test_g_format_vectors_match_python`.
- 5.13.3 - The native probe chain gives every corpus case's outcome, fake log and retry delays, and leaves no fake process running, including after the deadline drops it. test: `crates/gruntime/tests/endpoint_probe.rs::cases_match_python`.
- 5.13.4 - `pystr::format_g` gives every g-format vector. test: `crates/gruntime/tests/endpoint_probe.rs::format_g_matches_python`.
- 5.13.5 - `gdaemon` is one patch version above the executor's starting tip in its `Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`, so this leaf's cutover passes the release-identity preflight. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 5.14 Native `PUT /api/config/generation-endpoints/{endpoint_name}/activate` [category: code] (depends: 5.13)
`kind: deliverable`

Targets:
- `src/gobby/servers/routes/configuration_generation_endpoints.py::register_generation_endpoint_routes`
- `tests/servers/routes/test_endpoint_activation_corpus.py`
- `crates/gruntime/tests/fixtures/endpoint_activation_corpus.json`
- `crates/gruntime/src/lib.rs`
- `crates/gruntime/src/activation.rs`
- `crates/gruntime/src/validation/mod.rs`
- `crates/gruntime/src/values_patch.rs`
- `crates/gruntime/src/verify.rs`
- `crates/gruntime/tests/activation.rs`
- `crates/gdaemon/Cargo.toml`
- `src/gobby/install/version_pins.py::MANAGED_BIN_VERSION_PINS`
- `Cargo.lock`

Release ownership (Constraints): gruntime is in gdaemon's release closure, so this
leaf bumps `gdaemon` by 0.0.1 from the executor's starting tip.

Row: `PUT /api/config/generation-endpoints/{endpoint_name}/activate` for
`Operator`, with a `gate` (3.1). Compare mode proxies it once and runs
`check_proxied` (D3), so in compare mode only Python probes.

**Path.** Starlette matches `{endpoint_name}` against the percent-decoded path, so
a raw segment such as `a%2Fb` decodes to `a/b`, matches no route, and Python answers
404. The row's `gate` declines a request whose decoded segment contains `/`, and
Python answers it. Native decodes the raw segment as `urllib.parse.unquote` does:
percent escapes become bytes, and the bytes decode as UTF-8 with each invalid
sequence replaced by U+FFFD. The decoded name is the `endpoint` Python sees.
gcore's `encode_dynamic_segment`, whose safe set equals Python's, gives the encoded
segment, and `<prefix>` below is `ai.generation.endpoints.<encoded>`.

**Request body.** Python's handler stops taking `ActivateGenerationEndpointRequest`
as a FastAPI body parameter and parses the body itself, as 5.9 does:

1. Decode the body as strict UTF-8, parse it with `json.loads` and validate it with
   `ActivateGenerationEndpointRequest.model_validate`, which forbids unknown members.
2. Any failure returns 422
   `{"detail": "invalid generation endpoint activation request"}`.

The parse runs first, where FastAPI's body validation ran. Native parses with 5.7's
`json.rs` and applies pydantic's Python-mode rules for each member:

- `expected_revision`: `ConfigRevision`, a strict integer from 0 to 2^53 − 1;
- `protocol`: exactly `openai-compatible` (the default), `lmstudio`, `ollama` or
  `vllm`, case-sensitive; `wire_api`: exactly `chat-completions` or `responses`
  (the default);
- `api_base` and `model`: strings, both required; `api_key`: a string or `null`
  (the default);
- `tool_chat` (default `true`): `true` or `false`; the integers `0` and `1`; the
  floats `0.0`, `-0.0` and `1.0`; or a string that equals, ignoring ASCII case and
  with no stripping, one of `0`, `off`, `f`, `false`, `n`, `no` (false) or `1`,
  `on`, `t`, `true`, `y`, `yes` (true). Anything else fails, including `""` and
  `2`.

No member accepts `NaN` or `Infinity`, so both parses reject them. A lone-surrogate
escape in a string member is the documented 422 divergence (Config validation).

**Activate** ports `activate_generation_endpoint` in Python's order:

1. Read the published row with 3.1's `active_state`. No row is 503
   `runtime_unavailable` `Configuration runtime is not ready`, the body
   `ConfigValuesService._snapshot` raises. From the row take the stored key, the
   plaintext of `<prefix>.api_key`'s desired binding opened as 5.9's
   `desired_config` opens bindings (`ConfigSnapshot.desired_secret`), and the
   desired tree from 5.9's `candidate::desired_config`. The read releases its pool connection before the
   probe, so no connection is held while the probe runs.
2. The key is the request's `api_key` when it is a non-empty string, else the
   stored key. A `responses` request with no key is 422
   `{"detail": {"code": "validation_error", "path": ["api_key"], "message": "Generation endpoint API key is required"}}`.
3. Validate the endpoint (Endpoint validation below).
4. Run 5.13's `probe(host, endpoint, timeout_seconds)` with the validated endpoint
   and the desired tree's `ai.generation.timeout_seconds`. Python's `_probe_config`
   revalidates the desired config with this endpoint added. No `DaemonConfig`
   validator reads `ai.generation.endpoints`, and `clamp_candidate_timeouts` lowers
   only the two candidate timeouts, so that revalidation cannot fail and leaves
   `timeout_seconds` unchanged. A `ProbeError` is 422 `{"detail": <message>}`.
5. Build the flat values, in this order: `<prefix>.protocol`, `.wire_api`,
   `.api_base` (the validated value), `.model`, `.tool_chat`, `.probed_model`,
   `.input_modalities`, `.probed_json` and `.probed_tools`, from the endpoint and
   the `ProbeOutcome`. Add `<prefix>.api_key` with the request's key when that key
   is a non-empty string not equal to the stored key.
6. Run `values_patch.rs`'s port of `patch_flat` with `probe_verified` true, no
   unset keys, each key's `.` split as its path, and the request's
   `expected_revision`. `probe_verified` true skips Prepare step 6, its three
   evidence rules, as `patch_flat` skips them. Authorization, the `$secret:`
   plaintext check, the anchored row read and its 503 and 409, the commit and the
   write wait run as 5.9 describes.
7. Respond 200 with the write's body followed by the activation members:

   ```json
   {"committed": true, "revision": 8, "changed_keys": ["<sorted>"], "apply_status": "applied", "pending_restart_keys": [], "failed_live_keys": {}, "endpoint": "<name>", "provider": "codex", "model": "endpoint:<name>/<model>", "input_modalities": ["text", "image"], "probed_model": "<model>", "probed_json": true, "probed_tools": true, "probe_diagnostics": {}}
   ```

   `endpoint` is the decoded name. `provider` is `codex` for `responses`, else the
   request's `protocol`. `model` uses the request's `model`, which validation does
   not change. `probe_diagnostics` is the outcome's `diagnostics`. A
   `reconcile_failed` write body is followed by the same members.

A client that disconnects drops the handler future. 5.13's drop guard then stops
the probe's children, and an open write transaction rolls back, so nothing partial
persists.

**Endpoint validation** ports the two constructors the route calls, through
`validation::validate_model(cls, value)`. That function validates a JSON value
against the `model` node of `DaemonConfig`'s carrier schema whose `cls` is the
given qualified name, as 5.8's `validate_feature_candidates` port finds
`FeatureCandidateConfig`. Every node of one class carries one schema.

1. Validate `{protocol, wire_api, api_base, api_key, model, tool_chat}` against
   `gobby.config.ai.GenerationEndpointConfig`. Its `validate_api_base` strips
   `api_base` (5.7's `urls.rs`), and its `validate_endpoint` port gives
   `model must be set for generation endpoints` and
   `wire_api='responses' requires protocol='openai-compatible'`.
2. Validate `{"endpoints": {<name>: <the validated endpoint>}}` against
   `gobby.config.ai.GenerationConfig`, whose `validate_endpoint_names` port rejects
   a name outside `[a-z0-9][a-z0-9_-]*`.

A failure is 422 `{"detail": <5.7's detail(errors)>}`. The texts the corpus pins
are:

- `api_base: Value error, api_base must not be empty`;
- `api_base: Value error, api_base must use http/https`;
- `Value error, model must be set for generation endpoints`;
- `Value error, wire_api='responses' requires protocol='openai-compatible'`;
- `endpoints: Value error, generation endpoint names must match [a-z0-9][a-z0-9_-]*: 'Bad'`.

An `api_base` of `" http://x/ "` validates to `"http://x/"`, and that stripped
value is probed and stored.

**Python change** (`register_generation_endpoint_routes`):

- The self-parse under Request body.
- Validation messages (Decision Record), site 2: the `GenerationEndpointConfig` and
  `GenerationConfig` constructors run inside one `try` that catches pydantic's
  `ValidationError` and answers 422 with
  `{"detail": ConfigMutations._validation_detail(exc)}`. Today the
  `GenerationEndpointConfig` constructor sits outside the `try`, so an invalid
  endpoint (an empty model, a blank or non-HTTP `api_base`, or `responses` with
  another protocol) reaches `global_exception_handler` and returns 500. The
  `GenerationConfig` failure answered 422 with pydantic's `str(exc)`.
- The probe's `except` adds `LocalModelError`. `resolve_vllm_served_model` raises
  it, it is not a `ValueError`, and today a vLLM catalog failure returns 500.

**Errors.**

- 422 `{"detail": ...}`: the body parse, the missing key, Endpoint validation and
  every `ProbeError`.
- `ConfigValuesError.public_body` with its status for the write's errors: 503
  `runtime_unavailable`, 409 `revision_conflict`, and 422 `validation_error` and
  `revision_exhausted`.
- 500 `{"status": "error", "message": "Internal server error", "error_logged": true}`
  for `Internal`, `Blocked`, `Ambiguous`, a desired tree that fails validation, and
  database or secret-store failures. The activate route catches only
  `ConfigValuesError` around `patch_flat`, so Python's `RuntimeError`s and
  `RegistryError` reach `global_exception_handler`, unlike the PATCH route's
  `persistence_indeterminate` 500.

**`check_proxied`.** `verify.rs` routes a claimed proxied activation to
`activation::check_proxied`, which never probes:

- 200: parse the request as the route does. A request native cannot parse, which
  only a lone-surrogate escape reaches, is `Mismatch("")`. Otherwise rebuild the
  written values: `protocol`, `wire_api`, `model` and `tool_chat` from the request;
  the validated `api_base`; `probed_model`, `input_modalities`, `probed_json` and
  `probed_tools` from the body; and the request's key as `<prefix>.api_key`'s
  plaintext when the body's `changed_keys` lists that key. Then run 5.9's 200
  check on those values against the body's `revision` and `changed_keys`.
- 422: a body that fails the parse needs no state, and native's 422 must equal
  Python's. Otherwise `Skipped` when `config_state`'s revision differs from the
  request's `expected_revision`. Else run Activate steps 1 to 3 as a dry run. A
  result equal to Python's status and body is `Verified`. Passing all three is
  `Skipped`, since the 422 came from the probe or the write. Any other result is
  `Mismatch` at the first differing pointer.
- Any other status: `Skipped`.

**Corpus.** Each case in
`crates/gruntime/tests/fixtures/endpoint_activation_corpus.json` holds:

- `before`, `published` and `expected`'s rows, secrets and notification, as in
  5.9's corpus;
- `segment`, the raw path segment, and `request`, the raw body as text or base64;
- `base_env` and `scenario`, as in 5.13's corpus;
- `expected` adds the fakes' log, as 5.13 records it, and the status and body.

`{base}` in a request or an expected text becomes the fake server's
`http://127.0.0.1:<port>`. No case scripts a retryable status, so no test sleeps,
and every expected text is gobby's own, so bodies compare exactly. Library text
parity is 5.13's. The cases cover:

- invalid bodies: invalid UTF-8, text that is not JSON, an array, a missing
  `model`, an unknown member, `expected_revision` `true`, `protocol` `"VLLM"`, and
  `tool_chat` `""` and `2`;
- accepted `tool_chat` spellings `"Off"`, `"T"`, `1.0` and `0`;
- no published row (503);
- `responses` with no key (422), and with `""` and a stored key, which probes with
  the stored key through the `codex` shim;
- each Endpoint validation text, with a stripped `api_base` stored;
- probe failures: an authentication status on the chat text probe, a `$secret:` key
  on `responses`, and a vLLM `auto` model with two served models (formerly 500);
- writes: `tool_chat` false (`probed_tools` `null`), degraded JSON and vision
  probes with their diagnostics, a changed request key (secret written and
  reference stored), a key equal to the stored one (no `api_key` value), a stale
  `expected_revision` after the probe ran (409), revision 2^53 − 1 (422
  `revision_exhausted`), a `$secret:` key on a chat endpoint (422 after the probe),
  and a `reconcile_failed` write;
- one Responses success through the `codex` shim.

The Python test builds a fresh database per case and seeds `before`, as 5.9's
corpus test does. It installs the fakes as 5.13's Python test does: the `codex`
shim first on `PATH`, `GOBBY_AGENT_API_TOKEN`, and a patched
`gobby.ai.codex_endpoint.get_gobby_home`. It then sends the request through
`register_generation_endpoint_routes`, with a context whose `ConfigValuesService`
uses real `ConfigMutations` and `SecretStore` and 5.9's fake runtime, and asserts
`expected`. The Rust test seeds the same state, builds the family with
`RuntimeFamily::with_host(pool, home, host)`, whose host carries the shim's
`PATH`, publishes the post-commit row as 5.9's Rust test does, and asserts the same
results. `RuntimeFamily::new(pool, home)` builds `Host::process(home)`.

Research context:
- Python: `src/gobby/servers/routes/configuration_generation_endpoints.py`:
  `register_generation_endpoint_routes` (`activate_generation_endpoint`),
  `ActivateGenerationEndpointRequest` and `_probe_config`.
- `src/gobby/config/values.py`: `ConfigValuesService.desired_secret`,
  `.desired_config`, `._snapshot` and `.patch_flat`, whose `probe_verified` skips
  `reject_unprobed_responses_endpoints` and
  `reject_unverified_generation_endpoint_evidence`, and makes
  `clear_stale_generation_endpoint_probe_evidence` return at once.
  `src/gobby/config/runtime_models.py::ConfigSnapshot.desired_secret` reads the
  desired bindings.
- `src/gobby/ai/endpoint_activation.py::_activation_result` copies the endpoint and
  sets only the four evidence fields, so the stored `model` and `api_base` are the
  validated endpoint's.
- `src/gobby/config/ai.py`: `GenerationEndpointConfig.validate_endpoint` and
  `.validate_api_base`; `GenerationConfig.validate_endpoint_names` and
  `.clamp_candidate_timeouts`. `model` is not stripped.
- `src/gobby/config/registry_key_encoding.py::encode_dynamic_segment` and gcore's
  `config::runtime_contract::encode_dynamic_segment` share one safe set.
- `src/gobby/servers/exception_handlers.py::global_exception_handler` writes the
  500 body.
- `crates/gdaemon/src/lifecycle/backend.rs` starts the Python backend with
  gdaemon's environment, adding `config.environment` (empty in production) and
  removing two descriptor variables, so `Host::process` sees the `PATH` Python
  sees.
- `web/src` has no caller of the activate route.
- Planned checks:
  - `cargo nextest run -p gobby-runtime --test activation`;
  - `DATABASE_URL=… GOBBY_TEST_PROTECT=1 uv run pytest tests/servers/routes/test_endpoint_activation_corpus.py tests/servers/routes/test_config_values_api.py tests/ai/test_endpoint_activation.py -v`.

Consumers unchanged:
- `src/gobby/cli/install_setup_gclient.py` — no-edit-reason: reads only the `gclient` pin, which no leaf of this plan bumps.
- `src/gobby/cli/install_setup_gcode.py` — no-edit-reason: reads the `gcode` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_ghook.py` — no-edit-reason: reads the `ghook` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_gterm.py` — no-edit-reason: reads only the `gterm` pin, which no leaf of this plan bumps.
- `src/gobby/cli/install_setup_versions.py` — no-edit-reason: looks a pin up by the binary name it is given.
- `src/gobby/code_index/gcode_gateway.py` — no-edit-reason: derives `MIN_GCODE_GRAPH_VERSION` from the `gcode` pin at import.
- `src/gobby/hooks/runtime_compat.py` — no-edit-reason: derives `MINIMUM_GHOOK_VERSION_FOR_SUPPORTED_SCHEMA` from the `ghook` pin at import.
- `src/gobby/install/bin_freshness_models.py` — no-edit-reason: iterates every pin, so the bumped values flow through.
- `src/gobby/install/distribution.py` — no-edit-reason: looks a pin up by binary name.
- `tests/cli/installers/test_grok_installer.py` — no-edit-reason: reads the `ghook` pin instead of a version literal.
- `tests/cli/test_install_ghook.py` — no-edit-reason: builds its fixtures from the `ghook` pin instead of a version literal.
- `tests/cli/test_install_setup.py` — no-edit-reason: reads the `gcode` pin instead of a version literal.
- `tests/cli/test_install_setup_gclient.py` — no-edit-reason: reads the `gclient` pin instead of a version literal.
- `tests/cli/test_install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin instead of a version literal.
- `tests/cli/test_install_setup_gterm.py` — no-edit-reason: reads the `gterm` and `gclient` pins instead of version literals.
- `tests/code_index/test_gcode_gateway.py` — no-edit-reason: reads the `gcode` pin instead of a version literal.
- `tests/hooks/test_runtime_compat.py` — no-edit-reason: compares the derived floor to the `ghook` pin instead of a version literal.
- `tests/install/test_distribution.py` — no-edit-reason: reads pins by binary name; its `0.1.0` literal is a fake `gcode` output, not a pin.
- `tests/install/test_version_pins.py` — no-edit-reason: compares each pin to its crate manifest, which is this leaf's bump check.
- `src/gobby/servers/routes/configuration.py` — no-edit-reason: registers `register_generation_endpoint_routes` with an unchanged signature.

**Acceptance:**

- 5.14.1 - Every corpus case matches Python's activate PUT: status, body, rows, secrets, notification and fake log, with an invalid endpoint and a vLLM catalog failure answered 422. test: `tests/servers/routes/test_endpoint_activation_corpus.py::test_cases_match_python_route`.
- 5.14.2 - The native activate PUT gives every corpus case's status, body, rows, secrets, notification and fake log. test: `crates/gruntime/tests/activation.rs::cases_match_python`.
- 5.14.3 - A segment whose decoding contains `/` is not claimed, and a percent-encoded name is claimed and written under its encoded segment. test: `crates/gruntime/tests/activation.rs::path_segments_route_like_python`.
- 5.14.4 - `check_proxied` verifies a Python-committed activation and a pre-probe Python 422, reports a tampered evidence row and a differing 422 body as mismatches, skips a probe 422 and a moved revision, and starts no fake. test: `crates/gruntime/tests/activation.rs::proxied_activations_are_checked`.
- 5.14.5 - `gdaemon` is one patch version above the executor's starting tip in its `Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`, so this leaf's cutover passes the release-identity preflight. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 5.15 Native validation-command classifier [category: code] (depends: 5.14)
`kind: deliverable`

Targets:
- `scripts/generate_runtime_config_contract.py`
- `crates/gruntime/assets/validation_detection_builtins.json`
- `tests/config/test_validation_detection_corpus.py`
- `crates/gruntime/tests/fixtures/validation_detection_corpus.json`
- `crates/gruntime/src/lib.rs`
- `crates/gruntime/src/pystr.rs`
- `crates/gruntime/src/classifier/mod.rs`
- `crates/gruntime/src/classifier/shlex.rs`
- `crates/gruntime/tests/classifier.rs`
- `crates/gdaemon/Cargo.toml`
- `src/gobby/install/version_pins.py::MANAGED_BIN_VERSION_PINS`
- `Cargo.lock`

Release ownership (Constraints): gruntime is in gdaemon's release closure, so this
leaf bumps `gdaemon` by 0.0.1 from the executor's starting tip. No Python
production file changes.

This leaf ports the part of `classify_validation_command` that
`POST /api/config/validation-detection/preview` reports: the first match's
`matcher_id`, `label`, `categories` and `languages`. It adds no route; 5.16 calls
it. The evidence fields of `ValidationCommandMatch` (`normalized_command`, the
wrapper chain, segment counts and `evidence_requires_confirmation`) and
`normalize_validation_evidence_command` feed task validation, which stays in
Python, so they are not ported. `src/gobby/config/validation_detection.py`
(862 lines) and `validation_matchers.py` do not change.

**Builtins carrier.** The contract generator
`scripts/generate_runtime_config_contract.py` gains a third output,
`crates/gruntime/assets/validation_detection_builtins.json`:

- `builtin_matchers`: `builtin_validation_matchers()`, each dumped with
  `model_dump(mode="json")`, in order;
- `default_wrapper_rules`: `default_validation_wrapper_rules()`, dumped the same
  way, in order;
- `uv_run_options_with_values`: `_UV_RUN_OPTIONS_WITH_VALUES`, which
  `_iter_wrapper_rules` gives a recognized `uv run` wrapper.

gruntime embeds the file with `include_str!` and parses it once. The checked-in
file must equal the generator's output, as 5.7's validation carrier must, so an
edit to a builtin matcher or wrapper fails that test until the carrier is
regenerated. Rejected: hand-copying the 485-line `builtin_validation_matchers`
into Rust, which nothing would keep in step.

**Interface (`classifier/mod.rs`).**

- `Config` holds the six `ValidationDetectionConfig` fields, deserialized from a
  tree 5.16 has validated: `enabled`, `builtin_matchers_enabled`,
  `disabled_builtin_matcher_ids`, `recognized_wrappers`, `wrapper_rules` and
  `custom_matchers`.
- `first_match(command, config) -> Option<Match>`, where
  `Match { matcher_id, label, categories, languages }`.

**`first_match`** ports `_iter_validation_matches` up to its first result:

1. A command that `pystr::strip` empties, or `enabled` false, gives `None`.
2. The wrapper rules are the carrier's defaults, then `wrapper_rules`, then one
   `prefix` rule per recognized wrapper, as `_iter_wrapper_rules` builds them. The
   recognized wrappers pass through `_unique_strings`: `pystr::strip` each, drop
   empty ones and repeats, keep first-seen order. A wrapper whose `safe_split` is
   `["uv", "run"]` strips the carrier's `uv_run_options_with_values`.
3. `parse_shell_command` (below) splits the command into segments.
4. Each segment goes through `_normalize_segments`, and each non-empty normalized
   argv is tried against the matchers in `_iter_matchers` order: the carrier's
   builtins when `builtin_matchers_enabled`, skipping disabled ones and ids in
   `disabled_builtin_matcher_ids`, then the enabled `custom_matchers`. The first
   matcher `_matcher_matches_segment` accepts gives the result.

The ports, all from `validation_detection.py`:

- `_normalize_segments`: strip leading environment assignments, stop at depth 8,
  apply one wrapper rule and recurse into each unwrapped segment.
- `_apply_wrapper_rule`: candidates from `_matching_wrapper_prefixes`, ordered by
  prefix token count descending, then rule index, with a stable sort; the first
  that `_unwrap_matched_rule` accepts wins.
- `_consume_wrapper_prefix`, which skips options between prefix words for a rule
  that declares value-taking options.
- `_unwrap_matched_rule` for the `prefix`, `delimiter` and `command_string` kinds,
  with the `nice` and `env` rules special-cased by id through `_unwrap_nice_tokens`
  and `_unwrap_env_tokens`. A one-token `command_string` reparses that token with
  `parse_shell_command`. The `nice` patterns `[+-]?\d+` and `(?:-n[+-]?|-)\d+` are
  anchored with `\A` and `\z`, since Python calls `re.fullmatch`; `regex`'s `\d` is
  Unicode-aware, as Python's is for `str` patterns.
- `_matcher_matches_segment`, with the `python-dependency-audit` case through
  `_pip_audit_has_nonchecking_option`, and the helpers `_tokens_include_arg`,
  `_starts_with_command_prefix`, `_matches_command_token`,
  `_strip_env_assignments`, `_strip_wrapper_options` and
  `_looks_like_env_assignment`.

Python indexes these strings by code point, so the ports work on `char`s.

**Shell lexing (`classifier/shlex.rs`)** ports `src/gobby/config/shell_lexing.py`:

- `read_token` ports `shlex.shlex.read_token` from CPython's `Lib/shlex.py` in
  POSIX mode: escape `\`, quotes `'` and `"`, `"` as the only escaped quote, no
  commenters and `whitespace_split` on. A quoted empty string yields `""`. An
  unterminated quote or a trailing escape is the `ValueError` the callers catch.
  It runs in the three configurations the module uses:
  - `parse_shell_command`: punctuation characters `;&|<>` and newline, whitespace
    space, tab and carriage return. A run of punctuation characters is one token.
  - `_parse_heredoc_delimiter`: the same punctuation, with the default whitespace,
    which adds newline.
  - `safe_split` (`shlex.split`): no punctuation characters, default whitespace.
- `parse_shell_command`: strip heredoc bodies, tokenize (an error gives no
  segments), then split on `SHELL_SEGMENT_SEPARATORS`, recording each operator.
  A token matching `^(?:[<>]+|[<>]&|&[<>])$` is a redirection: it drops a
  preceding token that `pystr::isdigit` accepts, and skips the next token.
- `_strip_heredoc_bodies`, `_heredoc_openers` and `_parse_heredoc_delimiter`, with
  `pystr::rstrip` for the terminator line.
- `safe_split`: `shlex.split`, falling back on an error to Python's
  `str.split()`, which `pystr::split_whitespace` ports with the whitespace set of
  5.6's `pystr::strip`.

Rejected: the `shlex` crate, which has no punctuation-character mode and follows
POSIX shell quoting rather than Python's `shlex`.

**`pystr.rs`** gains:

- `isalnum`: non-empty, and every code point's general category is `Lu`, `Ll`,
  `Lt`, `Lm`, `Lo`, `Nd`, `Nl` or `No`, from `icu_properties`. For every code
  point Python's `str.isalnum` equals that test (checked on Python 3.14.3,
  Unicode 16.0).
- `isdigit`: non-empty, and every code point is `Nd` or one of the 128 code
  points Python's `str.isdigit` adds, in these ranges: U+00B2–U+00B3, U+00B9,
  U+1369–U+1371, U+19DA, U+2070, U+2074–U+2079, U+2080–U+2089, U+2460–U+2468,
  U+2474–U+247C, U+2488–U+2490, U+24EA, U+24F5–U+24FD, U+24FF, U+2776–U+277E,
  U+2780–U+2788, U+278A–U+2792, U+10A40–U+10A43, U+10E60–U+10E68,
  U+11052–U+1105A and U+1F100–U+1F10A.
- `rstrip` and `split_whitespace`, over the whitespace set of `strip`.

Rejected: `char::is_alphanumeric` and `char::is_numeric`, which test the
`Alphabetic` and `Numeric` properties. `Alphabetic` includes combining marks that
Python's `isalnum` rejects.

**Corpus.** `crates/gruntime/tests/fixtures/validation_detection_corpus.json`
holds three case lists:

- `classify`: a command, a config object and the expected `Match` or `null`;
- `lex`: an input with the expected `parse_shell_command` segments and operators,
  or the expected `safe_split` tokens;
- `str_classes`: strings with the expected `isalnum` and `isdigit`.

The cases cover:

- each builtin matcher, through its first prefix with the arguments its
  `required_args_*` lists need, and once with a forbidden and a non-executing
  argument, including the `--arg=value` spelling;
- each `_pip_audit_has_nonchecking_option` branch: `--`, a forbidden long option,
  an ambiguous and an unknown prefix, value options with and without `=`,
  `--desc on`, a bare path, `-` and short bundles such as `-vs` and `-of json`;
- each default wrapper rule: `uv --directory x run`, `uv run --with y`, `npx -p`,
  `npm exec`, `timeout 5 --`, `env A=1`, `env -i` (refused), `nice -n 5`,
  `nice -5`, `nice -n x` (refused), the `rust-token-killer` forms and
  `bash -c 'cd x && pytest'`;
- nesting past depth 8, leading assignments including `1A=x` and `_=x` (not
  assignments) and a non-ASCII name, a path command `/usr/bin/pytest` and a
  trailing `/`;
- compound commands whose first segment is not a validation command;
- config variations: `enabled` false, `builtin_matchers_enabled` false, disabled
  ids, a disabled custom matcher, a custom matcher with a path prefix, custom
  `delimiter` and `command_string` rules, and recognized wrappers with repeats,
  padding and `"uv  run"`;
- an empty and a whitespace-only command;
- lexing: single and double quotes, `\$` and `\"` inside double quotes, a
  backslash-newline, `''`, `&&`, `||`, `|&`, `;;`, `&&|`, `>&`, `2>&1`, `²>x`,
  `<<<`, a newline separator, `#` mid-command, an unterminated quote, heredocs
  with `<<EOF`, `<<-EOF` and a tab-indented terminator, `<<'EOF'` and a `#`
  comment before `<<`, and `safe_split` fallbacks over U+001C and U+3000;
- `str_classes`: ASCII, Latin-1 letters, a combining mark, CJK, Arabic-Indic
  digits, superscripts, a circled digit and a Roman numeral.

Every case uses code points assigned before Unicode 15.1 (Config validation). The
Python test validates each config with `ValidationDetectionConfig.model_validate`
and runs `classify_validation_command`, `parse_shell_command`, `safe_split`,
`str.isalnum` and `str.isdigit`. The Rust test runs `first_match`, the lexer and
`pystr`, and both assert every case.

Research context:
- `src/gobby/config/validation_detection.py`: `classify_validation_command`,
  `_iter_validation_matches`, `_iter_wrapper_rules`, `_iter_matchers`,
  `_builtin_matchers`, `default_validation_wrapper_rules`, `_wrapper_rule` and the
  ports listed above. `_wrapper_id_suffix` only names recognized-wrapper rules,
  whose ids the preview never reports.
- `src/gobby/config/validation_matchers.py::builtin_validation_matchers` and
  `ValidationCommandMatcher` (the matcher fields; `id` and `label` are stripped).
- `src/gobby/config/shell_lexing.py`: `parse_shell_command`, `safe_split`,
  `_split_tokens`, the heredoc helpers, `SHELL_SEGMENT_SEPARATORS` and
  `_SHELL_REDIRECTION_RE`.
- `icu_properties` 2.1.2 is already a gruntime dependency (5.7).
- Planned checks:
  - `cargo nextest run -p gobby-runtime --test classifier`;
  - `GOBBY_TEST_PROTECT=1 uv run pytest tests/config/test_validation_detection_corpus.py tests/config/test_runtime_config_contract.py -v`.

Consumers unchanged:
- `src/gobby/cli/install_setup_gclient.py` — no-edit-reason: reads only the `gclient` pin, which no leaf of this plan bumps.
- `src/gobby/cli/install_setup_gcode.py` — no-edit-reason: reads the `gcode` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_ghook.py` — no-edit-reason: reads the `ghook` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_gterm.py` — no-edit-reason: reads only the `gterm` pin, which no leaf of this plan bumps.
- `src/gobby/cli/install_setup_versions.py` — no-edit-reason: looks a pin up by the binary name it is given.
- `src/gobby/code_index/gcode_gateway.py` — no-edit-reason: derives `MIN_GCODE_GRAPH_VERSION` from the `gcode` pin at import.
- `src/gobby/hooks/runtime_compat.py` — no-edit-reason: derives `MINIMUM_GHOOK_VERSION_FOR_SUPPORTED_SCHEMA` from the `ghook` pin at import.
- `src/gobby/install/bin_freshness_models.py` — no-edit-reason: iterates every pin, so the bumped values flow through.
- `src/gobby/install/distribution.py` — no-edit-reason: looks a pin up by binary name.
- `tests/cli/installers/test_grok_installer.py` — no-edit-reason: reads the `ghook` pin instead of a version literal.
- `tests/cli/test_install_ghook.py` — no-edit-reason: builds its fixtures from the `ghook` pin instead of a version literal.
- `tests/cli/test_install_setup.py` — no-edit-reason: reads the `gcode` pin instead of a version literal.
- `tests/cli/test_install_setup_gclient.py` — no-edit-reason: reads the `gclient` pin instead of a version literal.
- `tests/cli/test_install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin instead of a version literal.
- `tests/cli/test_install_setup_gterm.py` — no-edit-reason: reads the `gterm` and `gclient` pins instead of version literals.
- `tests/code_index/test_gcode_gateway.py` — no-edit-reason: reads the `gcode` pin instead of a version literal.
- `tests/hooks/test_runtime_compat.py` — no-edit-reason: compares the derived floor to the `ghook` pin instead of a version literal.
- `tests/install/test_distribution.py` — no-edit-reason: reads pins by binary name; its `0.1.0` literal is a fake `gcode` output, not a pin.
- `tests/install/test_version_pins.py` — no-edit-reason: compares each pin to its crate manifest, which is this leaf's bump check.

**Acceptance:**

- 5.15.1 - The checked-in builtins carrier equals the generator's output. test: `tests/config/test_validation_detection_corpus.py::test_checked_in_builtins_match_python`.
- 5.15.2 - Python's classifier, `parse_shell_command`, `safe_split`, `str.isalnum` and `str.isdigit` give every corpus case's result. test: `tests/config/test_validation_detection_corpus.py::test_cases_match_python_classifier`.
- 5.15.3 - `first_match`, the native lexer and `pystr` give every corpus case's result. test: `crates/gruntime/tests/classifier.rs::cases_match_python`.
- 5.15.4 - `gdaemon` is one patch version above the executor's starting tip in its `Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`, so this leaf's cutover passes the release-identity preflight. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 5.16 Native `POST /api/config/validation-detection/preview` [category: code] (depends: 5.15)
`kind: deliverable`

Targets:
- `src/gobby/servers/routes/configuration_validation_detection.py::register_validation_detection_routes`
- `tests/config/test_live_policy_consumers.py::test_validation_detection_uses_runtime_snapshot`
- `tests/servers/routes/test_validation_detection_preview_corpus.py`
- `crates/gruntime/tests/fixtures/validation_detection_preview_corpus.json`
- `crates/gruntime/src/lib.rs`
- `crates/gruntime/src/validation_preview.rs`
- `crates/gruntime/src/verify.rs`
- `crates/gruntime/tests/validation_preview.rs`
- `crates/gdaemon/Cargo.toml`
- `src/gobby/install/version_pins.py::MANAGED_BIN_VERSION_PINS`
- `Cargo.lock`

Release ownership (Constraints): gruntime is in gdaemon's release closure, so this
leaf bumps `gdaemon` by 0.0.1 from the executor's starting tip.

Row: `POST /api/config/validation-detection/preview` for `Operator`. Compare mode
proxies it once and runs `check_proxied` (D3).

**Request body.** Python's handler stops taking `ValidationDetectionPreviewRequest`
as a FastAPI body parameter and parses the body itself, as 5.9 does:

1. Decode the body as strict UTF-8, parse it with `json.loads` and validate it with
   `ValidationDetectionPreviewRequest.model_validate`. The body is an object with a
   string `command` and an optional `config`, an object or `null` (the default).
   Unknown members are ignored.
2. Any failure returns 422 `{"detail": "invalid validation detection preview request"}`.

Native parses with 5.7's `json.rs` and applies the same rules. A `NaN`, `Infinity`
or lone-surrogate escape anywhere in the body is the documented 422 divergence
(Config validation).

**Preview:**

1. With a `config`, validate it with 5.14's
   `validation::validate_model("gobby.config.validation_detection.ValidationDetectionConfig", config)`.
   A failure is 400 `{"detail": <5.7's detail(errors)>}`.
2. Without one, read the published row with 3.1's `active_state`. No row is 3.1's
   503 `Configuration runtime is still starting` body, which
   `ConfigurationRouteContext.get_config_snapshot` raises. Build the object
   `{<field>: active_values["validation_detection.<field>"]}` for the six fields
   and validate it as step 1 does. Stored values are validated dumps, so this
   cannot fail; a failure, like a row read error, returns 500
   `{"status": "error", "message": "Internal server error", "error_logged": true}`,
   the body Python's `global_exception_handler` writes.
3. Deserialize the validated tree into 5.15's `classifier::Config` and run
   `first_match` on `command`.
4. Respond 200 `{"matched": false}`, or
   `{"matched": true, "matcher_id": ..., "label": ..., "categories": [...], "languages": [...]}`.

**Python change** (`register_validation_detection_routes`):

- The self-parse under Request body.
- A supplied `config` validates with
  `ValidationDetectionConfig.model_validate_json(to_json(config), strict=True)`, the
  way `ConfigMutations._validate_value` validates a stored value. The only caller,
  `web/src/components/ValidationDetectionEditor.tsx` in the settings section
  `ProjectsSessionsSection`, previews the `validation_detection` subtree it saves
  through `PATCH /api/config/values`. Today's lax `model_validate` accepts
  `"enabled": "yes"` or `1`, which that save rejects, so the preview approved
  configs that could not be saved. Strict validation also makes native's strict
  interpreter (5.7) the exact port.
- Validation messages (Decision Record), site 3: the 400 detail becomes
  `ConfigMutations._validation_detail(exc)` instead of `str(exc)`.

**`check_proxied`.** `verify.rs` routes a claimed proxied preview to
`validation_preview::check_proxied`:

- 200, 400 or 422: run the native preview on the request. The same status and body
  is `Verified`; a native 503 is `Skipped`; anything else is `Mismatch` at the
  first differing pointer. A preview without `config` reads the row at check time,
  so an activation between Python's answer and the check can give a false
  mismatch, which E1 counts (5.2).
- Any other status: `Skipped`.

**Corpus.** Each case in
`crates/gruntime/tests/fixtures/validation_detection_preview_corpus.json` holds the
published row or `null`, the raw body as text or base64, and the expected status
and body. The cases cover:

- invalid bodies: invalid UTF-8, text that is not JSON, an array, a missing
  `command`, a numeric `command`, and `config` as an array and as a string; and a
  valid body with an unknown member;
- config failures: `"enabled": "yes"` and `"enabled": 1` (accepted before this
  leaf), an unknown member, a blank matcher id, `bounded_inputs` without
  `languages`, a wrapper `kind` outside the literal, and `recognized_wrappers` as a
  string (`recognized_wrappers: Input should be a valid array`);
- a padded matcher id and label, reported stripped;
- no `config`: a published row with a custom matcher and a disabled builtin, and no
  row (503);
- matched and unmatched commands with a supplied config.

The Python test builds the snapshot from the published row as 5.6's test does,
sends the body through `register_validation_detection_routes` and asserts the
expected status and body. The Rust test seeds the same row and asserts the native
route's.

`tests/config/test_live_policy_consumers.py::test_validation_detection_uses_runtime_snapshot`
calls the endpoint with a `ValidationDetectionPreviewRequest`. It now passes a
Starlette `Request` carrying the JSON body, and still asserts one snapshot read.

Research context:
- Python: `src/gobby/servers/routes/configuration_validation_detection.py`:
  `register_validation_detection_routes` (`preview_validation_detection`) and
  `ValidationDetectionPreviewRequest`.
- `src/gobby/storage/config_mutations.py::ConfigMutations._validate_value` runs
  `adapter.validate_json(to_json(value), strict=True)`, and
  `._validation_detail` builds the site-3 text.
- `src/gobby/servers/routes/configuration_context.py::ConfigurationRouteContext.get_config_snapshot`
  raises the `still starting` 503.
- The registry splits `validation_detection` into six keys:
  `validation_detection.enabled`, `.builtin_matchers_enabled`,
  `.disabled_builtin_matcher_ids`, `.recognized_wrappers`, `.wrapper_rules` and
  `.custom_matchers`.
- `web/src/api/config.ts::previewValidationDetection` sends `{command, config}`.
- Planned checks:
  - `cargo nextest run -p gobby-runtime --test validation_preview`;
  - `DATABASE_URL=… GOBBY_TEST_PROTECT=1 uv run pytest tests/servers/routes/test_validation_detection_preview_corpus.py tests/servers/routes/test_configuration_routes.py tests/config/test_live_policy_consumers.py -v`.

Consumers unchanged:
- `src/gobby/cli/install_setup_gclient.py` — no-edit-reason: reads only the `gclient` pin, which no leaf of this plan bumps.
- `src/gobby/cli/install_setup_gcode.py` — no-edit-reason: reads the `gcode` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_ghook.py` — no-edit-reason: reads the `ghook` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_gterm.py` — no-edit-reason: reads only the `gterm` pin, which no leaf of this plan bumps.
- `src/gobby/cli/install_setup_versions.py` — no-edit-reason: looks a pin up by the binary name it is given.
- `src/gobby/code_index/gcode_gateway.py` — no-edit-reason: derives `MIN_GCODE_GRAPH_VERSION` from the `gcode` pin at import.
- `src/gobby/hooks/runtime_compat.py` — no-edit-reason: derives `MINIMUM_GHOOK_VERSION_FOR_SUPPORTED_SCHEMA` from the `ghook` pin at import.
- `src/gobby/install/bin_freshness_models.py` — no-edit-reason: iterates every pin, so the bumped values flow through.
- `src/gobby/install/distribution.py` — no-edit-reason: looks a pin up by binary name.
- `tests/cli/installers/test_grok_installer.py` — no-edit-reason: reads the `ghook` pin instead of a version literal.
- `tests/cli/test_install_ghook.py` — no-edit-reason: builds its fixtures from the `ghook` pin instead of a version literal.
- `tests/cli/test_install_setup.py` — no-edit-reason: reads the `gcode` pin instead of a version literal.
- `tests/cli/test_install_setup_gclient.py` — no-edit-reason: reads the `gclient` pin instead of a version literal.
- `tests/cli/test_install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin instead of a version literal.
- `tests/cli/test_install_setup_gterm.py` — no-edit-reason: reads the `gterm` and `gclient` pins instead of version literals.
- `tests/code_index/test_gcode_gateway.py` — no-edit-reason: reads the `gcode` pin instead of a version literal.
- `tests/hooks/test_runtime_compat.py` — no-edit-reason: compares the derived floor to the `ghook` pin instead of a version literal.
- `tests/install/test_distribution.py` — no-edit-reason: reads pins by binary name; its `0.1.0` literal is a fake `gcode` output, not a pin.
- `tests/install/test_version_pins.py` — no-edit-reason: compares each pin to its crate manifest, which is this leaf's bump check.
- `src/gobby/servers/routes/configuration.py` — no-edit-reason: registers `register_validation_detection_routes` with an unchanged signature.
- `tests/servers/routes/test_configuration_routes.py` — no-edit-reason: its preview tests post valid bodies whose configs pass strict validation.

**Acceptance:**

- 5.16.1 - Every corpus case matches Python's preview: status and body, with lax booleans in `config` answered 400. test: `tests/servers/routes/test_validation_detection_preview_corpus.py::test_cases_match_python_route`.
- 5.16.2 - The native preview gives every corpus case's status and body. test: `crates/gruntime/tests/validation_preview.rs::cases_match_python`.
- 5.16.3 - `check_proxied` verifies Python's 200, 400 and 422 answers, reports a differing body as a mismatch, and skips a native 503 and other statuses. test: `crates/gruntime/tests/validation_preview.rs::proxied_previews_are_checked`.
- 5.16.4 - A preview without `config` reads the runtime snapshot once and classifies with its active config. test: `tests/config/test_live_policy_consumers.py::test_validation_detection_uses_runtime_snapshot`.
- 5.16.5 - `gdaemon` is one patch version above the executor's starting tip in its `Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`, so this leaf's cutover passes the release-identity preflight. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

## P6: Native default, parity evidence and the template docs
`kind: framing`

### 6.1 `runtime` defaults to native [category: code] (depends: 5.16)
`kind: deliverable`

Targets:
- `crates/gruntime/src/lib.rs`
- `crates/gruntime/tests/family.rs`
- `crates/gdaemon/Cargo.toml`
- `src/gobby/install/version_pins.py::MANAGED_BIN_VERSION_PINS`
- `Cargo.lock`

Release ownership (Constraints): gruntime is in gdaemon's release closure, so this
leaf bumps `gdaemon` by 0.0.1 from the executor's starting tip.

This resolves Q1 in the Decision Record. `RuntimeFamily` overrides
`default_backend()` to return `RouteBackend::Native`. With no
`front_door.routes.runtime` entry, `RouteTable::new` (1.1) then serves the family
natively. `routes.runtime: proxy` or `compare` still overrides it. 1.1 already
rewrote the gcore `FrontDoorBootstrap` doc comment to say absent families take their
family's default, and 6.4 documents `native` as `runtime`'s default.

Landing constraint: leaves cut over as they land, so the cutover that activates
6.1 would put the operator's daemon on native before the soak. Before that
cutover, Orchestrator #15977 coordinates with the operator, who runs E1's
"set compare" command. The cutover's gdaemon start then begins the E1 soak.

Research context:
- Today `routes.rs::RouteTable::new` resolves an absent family to
  `RouteBackend::Proxy` inline; 1.1 moves that default onto the trait.
- `crates/gdaemon/src/serve.rs::run` registers `RuntimeFamily` only when the shared
  pool exists (3.1). A node builds no pool, so it registers no runtime family and
  keeps proxying. `unimplemented_families` checks only explicit route entries, so
  a default needs no change there.
- `src/gobby/runner_init/terminal_wiring.py` reads only `routes.get("terminal_ws")`,
  so Python's consumers are unaffected.
- Consumers that become native-path coverage under the default, with no edits:
  `tests/e2e/test_runtime_boundary.py` and `tests/e2e/test_hub_node_pair.py`.
  Both handshake with the bootstrap API key, so the family claims them.
- Planned check: `cargo nextest run -p gobby-runtime --test family`.

Consumers unchanged:
- `tests/e2e/test_runtime_boundary.py` — no-edit-reason: its operator handshakes and grant presentations reach the native family by default and must pass unchanged; E1 runs it.
- `tests/e2e/test_hub_node_pair.py` — no-edit-reason: its node-key handshake reaches the native family on the hub by default and must pass unchanged; E1 runs it.
- `tests/config/test_bootstrap.py` — no-edit-reason: parsing is unchanged, and an absent `routes` still parses to an empty map.
- `src/gobby/cli/install_setup_gclient.py` — no-edit-reason: reads only the `gclient` pin, which no leaf of this plan bumps.
- `src/gobby/cli/install_setup_gcode.py` — no-edit-reason: reads the `gcode` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_ghook.py` — no-edit-reason: reads the `ghook` pin by key at run time, so the bumped value flows through.
- `src/gobby/cli/install_setup_gterm.py` — no-edit-reason: reads only the `gterm` pin, which no leaf of this plan bumps.
- `src/gobby/cli/install_setup_versions.py` — no-edit-reason: looks a pin up by the binary name it is given.
- `src/gobby/code_index/gcode_gateway.py` — no-edit-reason: derives `MIN_GCODE_GRAPH_VERSION` from the `gcode` pin at import.
- `src/gobby/hooks/runtime_compat.py` — no-edit-reason: derives `MINIMUM_GHOOK_VERSION_FOR_SUPPORTED_SCHEMA` from the `ghook` pin at import.
- `src/gobby/install/bin_freshness_models.py` — no-edit-reason: iterates every pin, so the bumped values flow through.
- `src/gobby/install/distribution.py` — no-edit-reason: looks a pin up by binary name.
- `tests/cli/installers/test_grok_installer.py` — no-edit-reason: reads the `ghook` pin instead of a version literal.
- `tests/cli/test_install_ghook.py` — no-edit-reason: builds its fixtures from the `ghook` pin instead of a version literal.
- `tests/cli/test_install_setup.py` — no-edit-reason: reads the `gcode` pin instead of a version literal.
- `tests/cli/test_install_setup_gclient.py` — no-edit-reason: reads the `gclient` pin instead of a version literal.
- `tests/cli/test_install_setup_gdaemon.py` — no-edit-reason: reads the `gdaemon` pin instead of a version literal.
- `tests/cli/test_install_setup_gterm.py` — no-edit-reason: reads the `gterm` and `gclient` pins instead of version literals.
- `tests/code_index/test_gcode_gateway.py` — no-edit-reason: reads the `gcode` pin instead of a version literal.
- `tests/hooks/test_runtime_compat.py` — no-edit-reason: compares the derived floor to the `ghook` pin instead of a version literal.
- `tests/install/test_distribution.py` — no-edit-reason: reads pins by binary name; its `0.1.0` literal is a fake `gcode` output, not a pin.
- `tests/install/test_version_pins.py` — no-edit-reason: compares each pin to its crate manifest, which is this leaf's bump check.

**Acceptance:**

- 6.1.1 - `RuntimeFamily::default_backend()` returns `Native`. 1.1.5 proves an absent entry takes the default and an entry overrides it, and 6.3 proves it end to end. test: `crates/gruntime/tests/family.rs::runtime_defaults_to_native`.
- 6.1.2 - `gdaemon` is one patch version above the executor's starting tip in its `Cargo.toml`, `Cargo.lock` and `MANAGED_BIN_VERSION_PINS`, so this leaf's cutover passes the release-identity preflight. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 6.2 Corpus: `runtime` family cases replayed natively [category: test] (depends: 6.1)
`kind: deliverable`

Targets:
- `tests/contracts/http/manifest.json::*` — scope-reason: the `config` and `runtime_handshake` family entries collapse into one native `runtime` family
- `tests/contracts/http/runtime_handshake.json::*` — scope-reason: its cases move to the `runtime` family with grant masks unchanged
- `tests/contracts/http/config_schema.json::*` — scope-reason: its case moves to the `runtime` family and replays against 5.1's native route
- `tests/contracts/http/config_values.json::*` — scope-reason: its case moves to the `runtime` family and replays against 5.6's native route
- `tests/contracts/http/config_effective.json`
- `tests/contracts/http/runtime_config.json`
- `tests/contracts/http/README.md`
- `tests/contracts/test_http_corpus.py::e2e_pre_daemon_setup`
- `crates/gdaemon/tests/http_contracts.rs::*` — scope-reason: the registered-family list adds a `RuntimeFamily` built on a lazily constructed pool, so the parity precheck sees `runtime`

Manifest and cases:

- The manifest replaces families `config` and `runtime_handshake` with
  `runtime: {parity: native, origin: python}`. `runtime_challenge` stays native with
  origin gdaemon.
- Cases `runtime_handshake`, `config_schema` and `config_values` set
  `"family": "runtime"`.
- Add `config_effective`: `GET /api/config/effective`, credential `operator`.
- Add `runtime_config`: `GET /api/runtime/config`, credential `grant`.

Record both new cases from Python, with all routes proxied:
`GOBBY_RECORD_HTTP_CONTRACTS=1 … pytest tests/contracts/test_http_corpus.py -k record`.
Commit the recorded bytes, with `runtime_config` masking `config_revision` the way
`runtime_handshake` masks its epoch.

`e2e_pre_daemon_setup` also takes `e2e_home_dir` and `e2e_config`, so it runs after
the bootstrap exists. When `GOBBY_RECORD_HTTP_CONTRACTS=1` is set, it appends
`routes: {runtime: proxy}` under `front_door` in the isolated test home's
`<home>/bootstrap.yaml`, so recording captures Python. Otherwise it adds nothing:
under 6.1's native default, every case replays against the native family on the
real e2e database.

In the Rust harness, `parity_violations` takes the registered families as `FAMILIES`
plus a `gobby_runtime::RuntimeFamily::new(Pool::build(<valid unreachable URL>)?, tmp)`.
The harness has no key resolver, so the family never claims there. Its cases replay
through the proxy stub, and the Python harness carries native parity. The README
records that split for database-backed families.

Research context:
- `tests/contracts/http_corpus.py::CREDENTIALS` already includes `grant`, and
  `BoundaryHarness.grant_headers` supplies it.
- The Rust harness's `replay_in_declared_backend_state` sets
  `routes = {family: parity}` and calls `serve`, which composes only `FAMILIES`.
- The e2e bootstrap (`tests/e2e/conftest.py::e2e_config`) writes
  `front_door: enabled: true` with no routes. Every family is proxied today, and
  after 6.1 `runtime` is native there with no fixture change.
- The other P5 routes get no recorded case. A recorded write would change the state
  later cases replay against, the prompt list and detail change with the bundled
  prompt set, and the template and export embed the e2e fixture's temporary
  `terminal_host.socket_dir` inside a YAML string, which a JSON-pointer mask cannot
  reach. Their parity evidence is each leaf's vectors or corpus run through both
  implementations (5.2, 5.4, 5.5, 5.6, 5.9, 5.10, 5.12, 5.14 and 5.16) and 6.3's
  compare phase, which requires `match` or `verified` for each of them against
  Python on the e2e daemon.
- Planned checks:
  - `DATABASE_URL=… GOBBY_TEST_PROTECT=1 uv run pytest tests/contracts/ -v`;
  - `cargo nextest run -p gobby-daemon --test http_contracts`.

**Acceptance:**

- 6.2.1 - The manifest declares `runtime` native with origin python, owns the handshake, config and new cases, and no longer lists `config` or `runtime_handshake`. file: `tests/contracts/http/manifest.json`.
- 6.2.2 - `runtime_config` is recorded from Python and committed. file: `tests/contracts/http/runtime_config.json`.
- 6.2.3 - Every corpus case replays equal against the e2e daemon on the default `runtime` backend (native). test: `tests/contracts/test_http_corpus.py::test_case_replays_equal`.
- 6.2.4 - The Rust parity precheck passes with `runtime` registered. test: `crates/gdaemon/tests/http_contracts.rs::every_native_family_has_corpus_cases`.
- 6.2.5 - `config_effective` is recorded from Python and committed. file: `tests/contracts/http/config_effective.json`.

### 6.3 E2E: compare smoke, backend-kill fallback, flip and rollback [category: test] (depends: 6.2)
`kind: deliverable`

Targets:
- `tests/e2e/test_runtime_family.py`

The new e2e module uses `daemon_instance` and an operator client, authenticated with
the bootstrap API key the way `test_http_corpus._credentials` is. Each phase sets or
clears `front_door.routes.runtime` in the isolated instance's `bootstrap.yaml` (a
temporary test home, never `~/.gobby`), then calls `stop()` and `restart()`. Each
restart runs the same gdaemon command, so no rebuild occurs.

**Managed fixture.** While the backend is up, the module creates a running agent run
the way `tests/e2e/test_stateless_ambient_session.py::test_daemon_binds_schema_lease_to_child_identity_env`
does: register the test project, `SessionManager.register` a parent and a child
session, then `LocalAgentRunManager.create` and `start`. The signing key is
`read_managed_signing_key(<home>/bootstrap.yaml)` on the isolated home, and the
run's capability token is `issue_agent_api_token` for that run with that key.

1. **Compare.**
   1. With `compare` set, run an operator handshake, `GET /api/runtime/config` with
      that grant, and `GET /api/config/effective`.
   2. Exercise every P5 route with the operator client. "The current revision" is
      `revision` from a fresh `GET /api/config/values`.
      - `GET` `/api/config/schema`, `/api/config/secrets`, `/api/config/prompts`,
        one prompt's detail, `/api/config/template`, `/api/config/values`,
        `/api/config/ui-settings` and `/api/config/tool-approvals/global`;
      - `POST /api/config/export`;
      - create a scratch secret through `POST /api/config/secrets`, then `DELETE` it;
      - pick a prompt whose list entry has `has_override` false, `PUT` its detail
        `content` as a body-only JSON save, then `DELETE` the override;
      - `PATCH /api/config/values` at the current revision, setting the UI-only key
        `ui_settings.planPendingVariant` to `runtime-family-probe`, then a second
        PATCH that restores it: `unset` when its desired value was `null`, else its
        previous value;
      - `PUT /api/config/template` and `POST /api/config/import` at the current
        revision with `content` `"a: [\n"`, each answered 422 `validation_error`
        at `["content"]`, which writes nothing;
      - `PUT /api/config/generation-endpoints/runtime_family_probe/activate` at the
        current revision with `wire_api` `responses`, `api_base`
        `http://127.0.0.1:9/v1`, `model` `probe` and no `api_key`, answered 422
        `Generation endpoint API key is required` before any probe;
      - `POST /api/config/validation-detection/preview` with
        `{"command": "uv run pytest"}`.
   3. Send a managed challenge for the run's claims, with no `Authorization` header.
   4. `<home>/logs/front-door-compare.jsonl` holds `match` for every GET, `verified`
      for the handshake, the challenge, the export, the preview and every P5 write in
      step 1.2, and no `diff`, `native_error` or `mismatch` records.
   5. `gdaemon compare-report` (`instance.command[0]` with `GOBBY_HOME`) exits 0.
2. **Backend kill on the default.**
   1. Clear the `runtime` entry so the family runs on its native default, restart,
      then SIGKILL the Python backend child
      (`psutil.Process(instance.pid).children()`).
   2. While `/api/health` reports the backend not running, the three interactive
      runtime routes (handshake, grant presentation, effective config) and every
      P5 GET in step 1.2 return 200.
   3. `GET /api/sessions`, which no native family claims, returns the typed 503 that
      `tests/contracts/http/front_door_backend_down.json` pins.
   4. With the backend still down, run the managed flow natively:
      1. A managed challenge for the run's claims returns the proof that Python's
         `challenge_proof(nonce, kind="managed", signing_key=key, claims=claims)`
         computes. That proves gdaemon reads the managed signing key from the
         bootstrap at run time.
      2. A handshake with the run's bearer and identity headers returns 200. With no
         live generation, it issues generation G1 under
         `<home>/runtime/managed-executions/<run id>/`.
      3. The test writes the launch grant from that response the way
         `src/gobby/agents/spawn.py` does:
         `materialize_managed_launch(grant, dest_dir=<execution dir>, signing_key=key, deadline_seconds=3600)`.
      4. A second handshake finds G1 live with no draining predecessor, so it
         rotates to G2 and rewrites `grant.json`. The rewritten file names G2 in
         `capabilities.postgres.credential_generation`, and its `managed_api_token`
         passes `verify_agent_api_token` with the key.
      5. `GET /api/runtime/config` with the G2 grant and the run's bearer returns 200.
3. **Rollback to the proxy fallback.**
   1. Set `proxy` and restart.
   2. The same kill makes the handshake return the typed 503, which proves the route
      is proxied again.
4. **Return to the default.** Clear the entry and restart; the handshake returns 200
   while the backend is killed again.

Research context:
- `tests/e2e/conftest.py::DaemonInstance.restart` re-launches the original command
  with isolated state.
- Health is native-capable and reports backend state. A refused or standby backend
  short-circuits to 503 before dispatch (`front_door/mod.rs::FrontDoor::handle`). A
  `Down` or `Starting` backend still reaches native families.
- `psutil` is a project dependency.
- `ui_settings.planPendingVariant` is a public `str | None` registry key
  (`src/gobby/config/registry.py::_supplemental_key_specs`) that only the web client
  reads, so the PATCH pair changes no daemon behavior.
- A failed step leaves nothing to restore: the instance runs on a temporary home
  that `tests/e2e/conftest.py::e2e_config` deletes and a test-hub schema that
  `tests/fixtures/postgres.py::postgres_schema` drops at teardown. E1 4.3's
  probe restores in `finally` because it writes the live daemon.
- The document requests and the activation are witnessed by 422 answers that write
  nothing and, for the activation, start no probe. The 200 branches of their checks
  are proved by 5.12.5 and 5.14.4.
- Step 2 covers reads only. A native write commits and then waits for Python's
  activation to publish its revision (Decision Record, Write wait), so with the
  backend down it answers `reconcile_failed` after the bound, as D1 intends.
- Compare mode proxies every POST (D3), so native managed issuance, rotation and the
  `grant.json` rewrite get no soak evidence; step 2.4 is their end-to-end evidence
  on a real daemon. The crate tests in 4.4 through 4.7 cover their failure paths.
- In production, `src/gobby/agents/spawn.py` issues through Python's
  `ManagedCredentialManager.issue` and then calls `materialize_managed_launch` with
  `dest_dir=credential.bootstrap_path.parent`. Step 2.4 issues natively instead,
  which 4.6 step 4 serves when no generation is live, and writes the launch grant
  into the same execution directory.
- `src/gobby/servers/routes/runtime_handshake.py::challenge` computes the managed
  proof with `challenge_proof`, which the test calls directly with the isolated key.
- Planned check: `DATABASE_URL=… GOBBY_TEST_PROTECT=1 uv run pytest tests/e2e/test_runtime_family.py -v`.

**Acceptance:**

- 6.3.1 - Compare mode serves Python's responses and records `match` for every claimed GET, including the P5 reads, and `verified` for the handshake, the managed challenge, the export, the preview and every P5 write, with zero mismatches, and `compare-report` exits 0. test: `tests/e2e/test_runtime_family.py::test_compare_mode_records_zero_mismatches`.
- 6.3.2 - With no `runtime` entry (the native default), the interactive runtime routes and the P5 reads serve 200 while the backend is killed, and `GET /api/sessions` returns the typed 503. test: `tests/e2e/test_runtime_family.py::test_native_routes_survive_backend_kill`.
- 6.3.3 - Rollback to `proxy` and return to the default take only a bootstrap edit and a gdaemon restart with the same binaries. test: `tests/e2e/test_runtime_family.py::test_route_flip_and_rollback_need_no_redeploy`.
- 6.3.4 - With the backend killed on the native default, a managed challenge returns Python's proof for the isolated key, a first agent handshake issues G1, a second rotates to G2 and rewrites the launch `grant.json` with G2 and a valid capability, and the G2 grant presents. test: `tests/e2e/test_runtime_family.py::test_managed_handshake_and_rotation_survive_backend_kill`.

### 6.4 Docs: family-crate template and the flip runbook [category: docs] (depends: 6.3)
`kind: deliverable`

Targets:
- `crates/AGENTS.md`
- `docs/guides/configuration.md`

In `crates/AGENTS.md`, rewrite "Daemon family crates" to describe the template as
built:

- the generic gcore `RouteFamily<S>` behind `route-family`;
- a family as a process-lifetime value built in `serve::run` on the shared pool;
- `claims` for header-level delegation;
- `check_proxied` for non-idempotent routes;
- `default_backend` for each family's default when `front_door.routes` has no entry;
- the compare-log outcomes;
- the corpus split between the Rust stub replay and the Python native replay.

In `docs/guides/configuration.md`:

- Replace "absent = proxy" in the `front_door` example and prose: an absent family
  takes its family's default, which is `proxy` for every family except `runtime`,
  whose default is `native`.
- Document `front_door.routes.runtime` and the operator runbook, with E1's route
  commands:
  1. Set `compare` and restart gdaemon with an announcement.
  2. Soak.
  3. Run `gdaemon compare-report --since <start>`.
  4. Clear the entry to return to the native default.
  5. Fallback: set `proxy` and restart gdaemon when the native family misbehaves.

**Acceptance:**

- 6.4.1 - "Daemon family crates" documents the seam, the claims and proxied-check hooks, the compare log and the corpus split. file: `crates/AGENTS.md`.
- 6.4.2 - The configuration guide states the per-family default (native for `runtime`) and documents the compare, soak, clear-to-default and `proxy` fallback runbook with `compare-report` and the route commands. file: `docs/guides/configuration.md`.

## E1 End-to-end verification
`kind: verification`

These are completion gates for the implementation. Except for plan validation
(`uv run gobby plans validate .gobby/plans/gdaemon-runtime-family.md`), none has run
yet. Each runs after the leaves it names land, and each must hold on `0.5.0`, with
freshly built and installed binaries, before #21559 closes.

1. **Crates.** These must pass:
   - `cargo nextest run -p gobby-core -E 'test(presentation)'`;
   - `GOBBY_SCHEMA_TEST_DATABASE_URL=<test-dsn> cargo nextest run -p gobby-core --features postgres --test schema_contract --test catalog_manifest_freshness`;
   - `cargo nextest run -p gobby-runtime`, which runs every gruntime test file the
     leaves name;
   - `cargo nextest run -p gobby-daemon`, including `compare`, `http_contracts`,
     `lease` and `nodes`.
2. **Python, corpus and e2e.** With
   `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1`,
   `uv run pytest` must pass over:
   - `tests/contracts/`;
   - `tests/e2e/test_runtime_family.py`, `tests/e2e/test_runtime_boundary.py` and
     `tests/e2e/test_hub_node_pair.py`, whose handshakes now run on the native
     default;
   - `tests/storage/test_secret_seal_vector.py`,
     `tests/storage/test_managed_bootstrap_vector.py`,
     `tests/storage/test_managed_revocation_read.py` and
     `tests/storage/test_storage_prompts.py`;
   - `tests/runtime_grants/test_capability_vectors.py`,
     `tests/runtime_grants/test_agent_token_vectors.py`,
     `tests/runtime_grants/test_golden_vectors.py` and
     `tests/runtime_grants/test_managed_grant_rewrite_vector.py`;
   - `tests/config/test_runtime_config_contract.py`,
     `tests/config/test_yaml_export_vectors.py`,
     `tests/config/test_config_active_state.py`,
     `tests/config/test_config_validation_corpus.py`,
     `tests/config/test_candidate_validation_corpus.py`,
     `tests/config/test_yaml12_vectors.py`,
     `tests/config/test_validation_detection_corpus.py`,
     `tests/config/test_live_policy_consumers.py` and
     `tests/config/test_persistence.py`;
   - `tests/ai/test_endpoint_probe_corpus.py`;
   - `tests/prompts/test_prompts_loader.py`;
   - `tests/servers/routes/test_configuration_prompts.py`,
     `tests/servers/routes/test_active_read_vectors.py`,
     `tests/servers/routes/test_values_patch_corpus.py`,
     `tests/servers/routes/test_config_values_api.py`,
     `tests/servers/routes/test_secret_save_corpus.py`,
     `tests/servers/routes/test_config_yaml_replace.py`,
     `tests/servers/routes/test_yaml_documents_corpus.py`,
     `tests/servers/routes/test_endpoint_activation_corpus.py`,
     `tests/servers/routes/test_validation_detection_preview_corpus.py` and
     `tests/servers/routes/test_configuration_routes.py`;
   - `tests/install/test_version_pins.py`.

   Until the cutover installs 4.1's migration, the run sets
   `GOBBY_TEST_GDAEMON=checkout` after `cargo build -p gobby-daemon`.
3. **Route commands.** No agent reads or edits `~/.gobby/bootstrap.yaml`. The
   operator runs these from the main checkout. They go through the existing locked,
   atomic writer `src/gobby/config/bootstrap_io.py::update_bootstrap_yaml`, so no
   new CLI is added:
   - Set `runtime` to `compare` (or `proxy` for rollback):

     ```bash
     uv run python -c 'import sys; from gobby.config.bootstrap_io import bootstrap_path, update_bootstrap_yaml; update_bootstrap_yaml(bootstrap_path(), lambda d: d.update(front_door={**(d.get("front_door") or {}), "routes": {**((d.get("front_door") or {}).get("routes") or {}), "runtime": sys.argv[1]}}))' compare
     ```

   - Clear the entry so `runtime` takes its native default:

     ```bash
     uv run python -c 'from gobby.config.bootstrap_io import bootstrap_path, update_bootstrap_yaml; update_bootstrap_yaml(bootstrap_path(), lambda d: ((d.get("front_door") or {}).get("routes") or {}).pop("runtime", None))'
     ```

   Orchestrator #15977 coordinates each command with the operator at the cutover or
   restart that applies it. gdaemon reads routes only at start.
4. **Soak (exit criterion 1).**
   1. Before the cutover that activates 6.1, the operator must run the set command
      with `compare`; that cutover's gdaemon start begins the soak. If 6.1 is
      already live, the operator sets `compare` and the Orchestrator restarts
      gdaemon with a `global` announcement.
   2. The window (Q2) must last at least 24 hours of ordinary `gcode` and agent
      traffic, with at least one config read and one interactive handshake per
      active project and managed handshakes from ordinary agent runs. It must hold
      no pending config activation, which includes the interval between a config
      commit and Python's reconcile of it. The one exception is each soak probe
      run's PATCH pair (step 3), whose activations the probe itself waits out:
      Python answers each PATCH only after its reconcile of that commit, and the
      probe fails on any `apply_status` other than `applied`. That includes
      `reconcile_failed`, which `ConfigValuesService.patch_flat` answers with HTTP
      200 and `committed: true`. A backend restart inside the window is allowed,
      because requests that cannot reach Python record `skipped` (2.1).
   3. Write evidence uses an API-key client (Orchestrator #15977, 2026-10-14). Once
      inside the window, the operator must run this soak probe from the main
      checkout. `DaemonClient` reads the bootstrap API key in the operator's own
      process. The probe exercises every P5 route and changes nothing it does not
      restore:
      - it deletes only the scratch secret and the override it creates, and it
        overrides only a prompt that has no override, with that prompt's current
        content;
      - it sets the UI-only key `ui_settings.planPendingVariant`, which no daemon
        code reads, and restores it, as 6.3 does;
      - its document and activation requests are rejected with 422 before any
        write, and the activation before any probe.

      Restoration must not depend on the probe reaching the line after a write
      (RF-F11):
      - Before sending each write, the probe records the state its cleanup restores
        and registers that cleanup in `pending`. A write that commits and then
        fails, such as a PATCH answered with `reconcile_failed`, still owes its
        cleanup.
      - Each cleanup undoes only the probe's own change. The secret DELETE accepts
        404. The override is deleted only while it holds the content the probe
        wrote. The variant is restored at the current revision, and only while it
        still holds `runtime-family-probe`. A run that finds that value already set
        stops before its PATCH.
      - A cleanup leaves `pending` only after it succeeds. When the probe fails,
        `finally` runs every cleanup still pending, each in its own `try`, and
        prints `restored <route>` or an `UNRESOLVED CLEANUP` line naming the route,
        the state left behind and a recovery command. The original failure still
        propagates. The prompt's and the variant's recovery commands repeat their
        cleanup's check; the prompt's compares the override with
        `bundled_content`, which the probe's override equals.
      - The probe prints `soak probe passed` only after every check and every
        cleanup has succeeded.

      The operator must first run the probe once with each value of
      `RUNTIME_FAMILY_PROBE_FAIL`: `secret`, `prompt` and `patch`. The probe then
      raises right after the named write answers, when that write has committed.
      For `patch`, that is the state a committed PATCH answered with
      `reconcile_failed` leaves. Each forced run must exit nonzero with
      `forced failure after` in its traceback, print `restored` with the named
      write's cleanup route, and print no `UNRESOLVED CLEANUP` line. The operator
      must then run the probe without the variable, and it must print
      `soak probe passed`.

      ```bash
      uv run python - <<'EOF'
      import json, os, shlex, time
      from gobby.paths import get_gobby_home
      from gobby.utils.daemon_client import DaemonClient

      client = DaemonClient(timeout=90)
      start = int(time.time())
      fail = os.environ.get("RUNTIME_FAMILY_PROBE_FAIL")  # secret, prompt or patch
      key, marker = "ui_settings.planPendingVariant", "runtime-family-probe"
      pending = {}  # label -> (route, state, cleanup, recovery), registered before its write is sent

      def call(method, path, body=None, status=200, label=None):
          response = client.call_http_api(path, method, body)
          if label is not None and label == fail:
              raise RuntimeError(f"forced failure after {method} {path} answered {response.status_code}")
          assert response.status_code == status, (method, path, response.status_code, response.text)
          return response

      def values():
          body = call("GET", "/api/config/values").json()
          return body["revision"], (body["desired"].get("ui_settings") or {}).get("planPendingVariant")

      def patch(revision, change, label=None):
          body = call("PATCH", "/api/config/values", {"expected_revision": revision, **change}, label=label).json()
          assert body["apply_status"] == "applied", body

      def recovery(code):
          prefix = "from gobby.utils.daemon_client import DaemonClient; c = DaemonClient(timeout=90); "
          return "uv run python -c " + shlex.quote(prefix + code + "; print(r.status_code, r.text)")

      def settle(label):
          pending[label][2]()
          del pending[label]

      def probe():
          reads = [
              "/api/config/schema", "/api/config/secrets", "/api/config/prompts", "/api/config/template",
              "/api/config/values", "/api/config/ui-settings", "/api/config/tool-approvals/global",
          ]
          for path in reads:
              call("GET", path)
          name = "runtime_family_soak_probe"
          secret = f"/api/config/secrets/{name}"
          assert all(s["name"] != name for s in call("GET", "/api/config/secrets").json()["secrets"])

          def drop_secret():
              response = client.call_http_api(secret, "DELETE")
              assert response.status_code in (200, 404), (response.status_code, response.text)

          pending["secret"] = (
              f"DELETE {secret}", "the scratch secret may exist", drop_secret,
              recovery(f'r = c.call_http_api("{secret}", "DELETE")'),
          )
          call("POST", "/api/config/secrets", {"name": name, "value": "probe"}, label="secret")
          settle("secret")
          prompt = next(p["path"] for p in call("GET", "/api/config/prompts").json()["prompts"] if not p["has_override"])
          detail = f"/api/config/prompts/{prompt}"
          content = call("GET", detail).json()["content"]

          def drop_override():
              current = call("GET", detail).json()
              if current["has_override"] and current["content"] == content:
                  call("DELETE", detail)

          pending["prompt"] = (
              f"DELETE {detail}", "an override holding the bundled content may exist", drop_override,
              recovery(
                  f'd = c.call_http_api("{detail}", "GET").json(); '
                  'assert d["has_override"] and d["content"] == d["bundled_content"], d; '
                  f'r = c.call_http_api("{detail}", "DELETE")'
              ),
          )
          call("PUT", detail, {"content": content}, label="prompt")
          settle("prompt")
          call("POST", "/api/config/export")
          revision, previous = values()
          assert previous != marker, f"{key} still holds {marker!r} from an earlier probe"
          restore = {"unset": [key]} if previous is None else {"values": {"ui_settings": {"planPendingVariant": previous}}}

          def restore_variant():
              current_revision, current = values()
              if current == marker:
                  patch(current_revision, restore)

          pending["patch"] = (
              "PATCH /api/config/values", f"{key} may hold {marker!r} instead of {previous!r}", restore_variant,
              recovery(
                  'v = c.call_http_api("/api/config/values", "GET").json(); '
                  f'assert (v["desired"].get("ui_settings") or {{}}).get("planPendingVariant") == "{marker}", v; '
                  f'r = c.call_http_api("/api/config/values", "PATCH", {{"expected_revision": v["revision"], **{json.dumps(restore)}}})'
              ),
          )
          patch(revision, {"values": {"ui_settings": {"planPendingVariant": marker}}}, label="patch")
          settle("patch")
          for method, path in [("PUT", "/api/config/template"), ("POST", "/api/config/import")]:
              call(method, path, {"expected_revision": values()[0], "content": "a: [\n"}, status=422)
          activate = "/api/config/generation-endpoints/runtime_family_soak_probe/activate"
          endpoint = {"wire_api": "responses", "api_base": "http://127.0.0.1:9/v1", "model": "probe"}
          call("PUT", activate, {"expected_revision": values()[0], **endpoint}, status=422)
          call("POST", "/api/config/validation-detection/preview", {"command": "uv run pytest"})
          log = get_gobby_home() / "logs" / "front-door-compare.jsonl"
          records = [json.loads(line) for line in log.read_text().splitlines()]
          seen = {(r["method"], r["path"], r["outcome"]) for r in records if r["ts"] >= start}
          writes = [
              ("POST", "/api/config/secrets"),
              ("DELETE", secret),
              ("PUT", detail),
              ("DELETE", detail),
              ("POST", "/api/config/export"),
              ("PATCH", "/api/config/values"),
              ("PUT", "/api/config/template"),
              ("POST", "/api/config/import"),
              ("PUT", activate),
              ("POST", "/api/config/validation-detection/preview"),
          ]
          want = {("GET", path, "match") for path in [*reads, detail]} | {
              (method, path, "verified") for method, path in writes
          }
          assert want <= seen, sorted(want - seen)

      try:
          probe()
      finally:
          for label, (route, state, cleanup, command) in list(pending.items()):
              try:
                  cleanup()
                  print("restored", route)
              except Exception as exc:
                  print(f"UNRESOLVED CLEANUP {route}: {state}; cleanup failed with {exc!r}; recover with: {command}")
      print("soak probe passed")
      EOF
      ```

   4. After the window, `gdaemon compare-report --since <window start>` must exit 0.
   5. The per-route count over the window must show nonzero `verified` for
      `POST /api/runtime/handshake/challenge`, which the family claims only for
      managed challenges (4.3), and for `POST /api/runtime/handshake`, and nonzero
      `match` for `GET /api/runtime/config` and `GET /api/config/effective`. The
      count reads only the compare log:

      ```bash
      uv run python -c 'import collections, json, sys; from gobby.paths import get_gobby_home; since = int(sys.argv[1]); log = get_gobby_home() / "logs" / "front-door-compare.jsonl"; counts = collections.Counter((r["method"], r["path"], r["outcome"]) for r in map(json.loads, log.read_text().splitlines()) if r["ts"] >= since); [print(n, *key) for key, n in sorted(counts.items())]' <window start>
      ```
5. **Flip to the native default (exit criterion 3, live flip).** The operator must
   run the clear command, and the Orchestrator must restart gdaemon with a `global`
   announcement and the same binaries. The live daemon then serves `runtime` on its
   native default, which is the close state (Q1).
6. **Backend kill (exit criterion 2).**
   1. On the native default, the Orchestrator sends a `global` announcement, waits
      for a quiet window with no live spawned worker or close validator, as for a
      restart, then kills the Python backend.
   2. Until the supervisor restarts the backend, `gcode` must still handshake and
      read effective config, a managed agent handshake must still return a grant,
      and `GET /api/config/values` must still return 200.
7. **Rollback (exit criterion 3, rollback).** 6.3.3 must demonstrate rollback to
   `proxy` and return to the default with the same binaries on an isolated daemon.
   The live daemon is not rolled back at close. In operation, the fallback is the
   set command with `proxy` plus a gdaemon restart, as 6.4 documents.
8. **Repo gates.** These must pass:
   - `uv run ruff format src/ && uv run ruff check src/ && uv run mypy src/`;
   - `uv run gobby test-types audit tests/ --baseline .gobby/test-types-baseline.json --fail-on-new`;
   - `cargo fmt --check`, and `cargo clippy` for `gobby-core` (with
     `route-family`), `gobby-runtime` and `gobby-daemon`;
   - `Cargo.lock` gains no `[[package]]` over the plan's starting tip except
     `icu_casemap` and `icu_casemap_data` 2.1.1 (Constraints, Dependencies).

   Every hand-maintained production file must stay under 1,000 lines.

## V1 Plan Changelog
`kind: framing`

- 2026-10-13: First draft by Plan Writer #16764. Decisions D1–D4 were ruled by
  Orchestrator #15977 on 2026-10-13.
- 2026-10-13: Consensus between Plan Writer #16764 and Plan Adversary #16787 on
  the revision at 3cc65d0. The adversary's six findings were accepted and repaired:
  `check_proxied` receives the buffered request bytes (PA-1); the presentation
  module uses the directory form (PA-2); 3.1's claims test covers only
  `/api/config` paths and unidentified callers, and 3.2 and 3.4 extend `claims`
  (PA-3); the desired snapshot keeps `_read_coherent`'s empty-store and
  missing-state rules, with new item 3.1.5 (PA-4); both deferrals use
  `TBD-at-expansion` ledgers with their own items D1.1, D1.2 and D2.1 (PA-5); and
  4.1.5 names `config_effective.json` (PA-6). No enhancer edits were received
  before consensus.
- 2026-10-14: Consensus between Plan Writer #16764 and Plan Adversary #16491 on
  the revision at 6b5e3d58eb. It supersedes the 2026-10-13 consensus, whose
  deferral ledgers left the plan under Josh's no-deferral policy. The plan now
  carries the operator's D1 reversal (Python publishes the active config state,
  and all config is native) and Orchestrator #15977's 2026-10-14 rulings, each
  recorded in the Decision Record. The adversary's eleven findings were accepted
  and repaired:
  - RF-F1 and RF-F2: no deferrals. All 18 P5 config routes, the managed challenge,
    the capability-token handshake, managed issuance, rotation with the
    `grant.json` rewrite, grant presentation and the durable revocation read are
    planned natively in P4 and P5.
  - RF-F3: 3.3 publishes the KEK the way `_publish_private_file` does.
  - RF-F4: the native-default statement moved to 1.1's `FrontDoorBootstrap` doc
    comment and 6.4's guide.
  - RF-F5: 3.2 decodes the grant header structurally.
  - RF-F6: every leaf that changes a shipped binary's source owns its 0.0.1 bump
    and the managed pins (Constraints, Release ownership).
  - RF-F7: 3.3 runs `_get_dek`'s existing-secrets guard before creating an
    envelope.
  - RF-F8: 5.1, 5.5 and 5.6 re-export their public accessors through
    `crates/gcore/src/config/mod.rs`.
  - RF-F9: `icu_casemap` and `icu_casemap_data` 2.1.1 are the one named exception
    to the lock rule, with E1's lock gate.
  - RF-F10: 5.13 signals process groups through the locked `nix`, with
    `unsafe_code` still forbidden.
  - RF-F11: E1's soak probe registers each cleanup before its write and restores in
    `finally` without clobbering other changes. It reports unresolved cleanups
    with a recovery command, asserts `apply_status` `applied`, and is exercised by
    a forced failure after each write.

  No enhancer edits were received before consensus.
