# Working context: #21559 plan (gdaemon runtime family)

Plan to write: `.gobby/plans/gdaemon-runtime-family.md`, Plan ID `gdaemon-runtime-family`, root #21559.
Research at commit `97b1bbe055` on 0.5.0. Load ~52 (heavy hold at >54 twice); gcode is slow.

## Decisions sent to coordinator #15977 (awaiting answer)

- D1 config snapshot source: (a, rec) native reads desired from DB (`config_state.revision` + `config_store`
  rows + defaults/machineExport/secrecy from `crates/gcore/assets/config/runtime_config_contract.json`);
  (b) Python persists active snapshot (migration + carriers).
- D2 principal kinds (revised rec = b): native issues interactive only; capability-token bearers
  (`gobby-agent-v1.` agent_run/tool_chat/maintenance) forwarded to Python; managed challenge stays
  forwarded; deferred section moves managed issuance with S2.7 hub #24149.
- D3 compare policy (rec a): native leg only for GET/HEAD and family-declared side-effect-free routes;
  handshake uses a family verify hook on the proxied grant (HMAC, rejection matrix, recompute
  config_revision/capabilities/schema/deployment); outcomes appended to
  `~/.gobby/logs/front-door-compare.jsonl`; `gdaemon compare-report` summarizes per family.
- D4 /api/config scope (rec a): native GET `/api/config/effective` only; rest proxied under prefix;
  deferred section for the remainder until config activation leaves Python.

## Existing Rust facts

- Routing: `crates/gdaemon/src/front_door/routes.rs` has `RouteFamily` trait (name, prefixes,
  `router() -> Router<FrontDoorState>`), `FAMILIES` static (`HealthFamily`, stub `TerminalFamily`),
  `RouteTable::new(families, routes, state)` (applies `.fallback(proxy_fallback).with_state`),
  `RouteTable::dispatch` (Proxy/Native/Compare), `lookup` (longest whole-segment prefix),
  `unimplemented_families`, `compare` (collects body, `tokio::join!` native+proxy, byte-equal + status,
  `eprintln!` diff, returns proxied). Test `unclaimed_methods_and_paths_under_a_native_family_are_proxied`.
- `FrontDoorState` in `front_door/mod.rs` (native NativeRoutes, runtime Option<Arc<RuntimeState>>, target,
  backend_state, client, auth). RouteTable built per listener in `serve.rs::serve_with_runtime`.
  `serve.rs::run` calls `unimplemented_families(FAMILIES, ...)`.
- `RouteBackend` enum in `gobby_core::bootstrap` (Proxy|Native|Compare); bootstrap `front_door.routes` map.
- gterminals crate (`crates/gterminals`, package gobby-terminals) already inherits `[workspace.package]`
  (root Cargo.toml has it). It does not export RouteFamily; gdaemon `state_services.rs` builds
  `gobby_terminals::family::TerminalFamily::new(route, options, authority)`.
- Family-crate convention: `crates/AGENTS.md` "Daemon family crates" (crates/g<family> -> gobby-<family>,
  publish=false, members when epic starts, export one RouteFamily, own repositories on gcore pool).
- Pools in gdaemon: `PostgresKeyResolver::new` (auth.rs, max 2, own pool), `ActiveDaemonLease::new`
  (lease/mod.rs:91 own pool; `connect` uses `Object::take` + `RESET ROLE; SET statement_timeout='5s'`
  + application_name; advisory lock session), serve.rs:133 nodes pool (max 4), state_services.rs
  active pool (max 8) and terminals epoch pool. Task constraint: retarget auth + lease to shared pool.
- Lease: `ActiveDaemonLease::try_acquire` upserts `deployment_runtime` (deployment_token,
  fencing_epoch+1, grant_signing_secret 32-byte hex) -> `Fence { epoch, signing_secret }`;
  `fence()` accessor. Deployment token: `gobby_core::grant::deployment_token(home)`.
- Front-door auth (`front_door/auth.rs::AuthState::authenticate`): strips IDENTITY_HEADERS
  (x-gobby-user-id, x-gobby-machine-id, x-gobby-key-id, front-door secret), passes non-bearer and
  `gobby-agent-v1.` bearers through, resolves API keys via `KeyResolver` and inserts identity headers.
- Interactive challenge native: `front_door/challenge.rs::answer` (HMAC of nonce with key derived from
  bootstrap api_key); managed kind forwarded. Used in Node door (mod.rs:111); check mod.rs:170 for backend.
- gcore grant client: `crates/gcore/src/grant/bundle.rs` (GrantBundle, GRANT_VERSION=2,
  EXPECTED_API_CONTRACT=1, canonical_payload_bytes, payload_checksum, expected_schema_identity,
  parse_grant_json), `grant/mod.rs` (GrantError::from_presentation_http, RUNTIME_CONFIG_PATH),
  `grant/handshake.rs` (CHALLENGE_PATH, HANDSHAKE_PATH), acquisition.rs. No server signing in Rust.
- gcore config: `config/runtime_contract.rs` (is_machine_config_key, secrecy, CONTRACT_JSON with
  exactKeys default/activation/machineExport/visibility/secrecy + patterns), `config/resolve.rs`
  (decode_config_value, resolvers), `config/daemon_source.rs` (DaemonServedConfig).
  `crates/gcore/src/ai/effective_config.rs` EFFECTIVE_CONFIG_PATH consumer.
- `gcore/src/schema/runner.rs::auth_schema_for` is pub(super) (public -> gobby_agent_auth).
- gcore features: postgres, postgres-pool (tokio-postgres, deadpool), no axum. gdaemon has axum 0.8, hmac.
- Rust corpus harness `crates/gdaemon/tests/http_contracts.rs` (redact_secrets, apply_masks,
  normalized_response, stub backend; `every_native_family_has_corpus_cases`).

## Python behavior to reproduce

- `src/gobby/servers/routes/runtime_handshake.py`: challenge (credential_before_proof 401; interactive
  -> 400 interactive_challenge_is_native; managed needs signing key), handshake (401 unauthenticated;
  agent claims -> issue_for_agent; else front-door identity -> issue_for_operator with forwarded
  machine id or local machine id). Response `{grant, deployment_token, fencing_epoch}` + no-store.
  `grant_error_response`: 409 stale_epoch; 401 expired/credential_before_proof; else 403; body
  `{code, error: code, message}`.
- `runtime_config.py::get_runtime_config`: auth required, `X-Gobby-Runtime-Grant` header,
  decode_grant_header -> grants.present -> `{config_revision, settings: _machine_config_values}`.
- `configuration_effective.py::get_effective_config`: front-door identity or break-glass; body
  `{revision, config: _machine_config_values}`; `_machine_config_values` filters machine
  visibility/machineExport, drops secrets and unresolved `${...}`, expand_env_vars.
- `runtime_grants/service.py::GrantService.present` order: signature, checksum, deployment token,
  schema identity, api_contract/version, fencing epoch, expiry, revocation, capability.
  Codes: invalid_signature, wrong_deployment, wrong_schema, wrong_api_contract, stale_epoch, expired,
  revoked, wrong_capability. `issue` builds bundle from `runtime.capture()` snapshot;
  `capabilities_from_snapshot` (falkor/qdrant direct vs brokered via `_url_requires_broker`, embed
  daemon if embeddings model set, text_generate/tool_chat daemon, vision/audio flags, broker ops).
- `runtime_grants/signing.py`: canonical JSON (sort_keys, compact, ensure_ascii False) minus
  checksum/signature; checksum sha256 hex; signature HMAC-SHA256(secret.encode(), payload) hex.
- `runtime_grants/handshake.py::HandshakeService`: issue_for_operator (machine match, project admitted
  via `SELECT 1 FROM projects WHERE id=%s AND deleted_at IS NULL`, `_normalized_overlay_claim`),
  `_issue` per-principal lock, TTL = min(3600, token_exp-now, valid_until-now).
- `runner_init/servers.py::issue_grant_postgres` interactive branch -> `issue_interactive`.
- `storage/interactive_credentials.py::issue_interactive`: heartbeat_daemon(machine, lease dur) then one
  txn: `<auth>.issue_or_reuse_interactive_principal(token, daemon machine, project, session, expiry,
  password, overlay)`; reused -> `load_interactive_credential_material` + open_sealed; else
  `replace_interactive_credential_material` with seal; aad = `token:machine:project:generation`;
  DSN = daemon conninfo with user/password replaced (psycopg make_conninfo key=value form).
- Revocation: `revocation.py::is_revoked` (in-memory checksums/executions/interactive) +
  `_principal_revoked` -> `interactive_generation_revoked` SQL. Managed revoke via
  `<auth>.revoke_principal`; binding liveness via `managed_binding_states`.
- Secret sealing `storage/secrets.py`: `secret_key_material` row (kek_posture, kek_salt, kek_kdf_n/r/p,
  wrapped_dek); KEK = Fernet key from `~/.gobby/.secret_kek` or scrypt(GOBBY_SECRET_KEK_PASSPHRASE);
  DEK = Fernet decrypt; seal key = HKDF-SHA256(urlsafe_b64decode(DEK), info
  `gobby.secret-store.seal.v1`); seal = b64(nonce12 || AESGCM).
- Python middleware 401 body for missing auth: `{"error": "Authentication required. CLI clients need an
  API key in bootstrap.yaml and gdaemon (run 'gobby auth login'). Browsers: log in.", "code": "missing_auth"}`.

## Scope re-ruling research (2026-10-14, adversary #16491 RF-F1/RF-F2)

- #15977 ruled Option A (08:31Z): plan all 18 routes + managed challenge/handshake/
  presentation + durable managed revocation read (SQL fn migration + carriers); grant.json
  writing stays in Python launcher as a boundary fact; write-path verifier via
  check_proxied in soak; cross-plan deps for #21587-owned routes; revise D3/soak/Granularity.
- 08:4xZ correction sent (msg 3a6ff416): values/ui-settings/tool-approvals/validation-
  detection read Python ConfigRuntime ACTIVE snapshot (active values, pending_restart_keys,
  failed_live_keys); writes CAS on active revision, validate via pydantic
  DaemonConfig.model_validate (ConfigMutations._validate_candidate) + embedding admission +
  generation-endpoint probe evidence, then await reconcile_local_commit for apply_status;
  contract JSON has types only (449 keys, 11 patterns); prompt writes clear Python
  PromptLoader cache; gen-endpoint activate probes LLM (gobby.ai.endpoint_activation,
  #21587); validation-detection preview = 862-line classifier (#21567). Asked rulings R1
  (persist activation state table), R2 (contract constraints carrier + Rust validator +
  hand-ported model validators), R3 (cross-plan deps #21587/#21567), R4 (one plan vs split).
- Adversary #16491 findings so far: RF-F1/F2 (deferrals, ruled A), RF-F3 (3.3 KEK publish
  must port secrets.py::_publish_private_file: temp 0600 write+fsync, os.link no-clobber,
  unlink temp, fsync dir; extend 3.3.4 with incomplete-temp reader + pre-publish failure),
  RF-F4 (3.6 must target src/gobby/config/bootstrap.py::FrontDoorConfig docstring + carrier
  crates/gcore/assets/config/runtime_config_contract.json, regen expects unchanged bytes;
  3.6.2 checks Rust and Python docs), RF-F5 (3.2 server decode_grant_header must not use
  client parse_grant_json, which rejects api_contract/checksum early; decode base64url +
  serde_json GrantBundle structurally in presentation module, leave sig/checksum/contract to
  verify_static order; forward only truly malformed JSON/schema and principal ownership;
  add handler-level vectors: wrong checksum, signed wrong api_contract, multiply-invalid,
  backend down), RF-F6 (each Rust leaf shipping changed binary source owns a 0.0.1 patch
  bump from executor tip for affected shipped binaries: gdaemon; gcode+ghook on gcore
  changes; gclient/gterm only if released; targets their Cargo.toml,
  src/gobby/install/version_pins.py::MANAGED_BIN_VERSION_PINS, Cargo.lock; rebuild
  dependents; promote via coherent set only; note gcode maintenance floor derives from pin;
  acceptance checks versions/pins + cutover preflight; no release-only leaf), RF-F7 (3.3
  missing-envelope init must first run Python's `SELECT 1 FROM secrets LIMIT 1` guard
  (secrets.py::_get_dek); encrypted secrets present -> SecretError::Unavailable (503), no
  KEK/row writes; isolated DB regression seeded with secrets + no envelope). Adversary will
  re-review whole amended scope after R1.
- 08:40Z #15977 rulings: R1 -> Josh button (a reverse D1 publish active state / b active-state
  routes proxied permanently as stated end state); write NO R1 text yet. R2 yes (contract
  constraints carrier + hand-ported validators; shared valid/invalid corpus through both
  DaemonConfig.model_validate and Rust; specify validate-before-commit, CAS vs active
  revision, exact response when apply_status misses bound). R3: verified no plan leaf in
  #21587/#21567 -> classifier port + Rust probe client in scope. R4 one plan. Prompt cache
  invalidation pg_notify channel + Python listener in scope. Told #15977 writes/R2 exist only
  under R1(a).

## Repair evidence gathered (for drafting)

- RF-F3 sequence (src/gobby/storage/secrets.py::_publish_private_file, lines 162-188):
  temp `path.with_name(f".{name}.{pid}.{uuid4hex}.tmp")` opened O_WRONLY|O_CREAT|O_EXCL 0600,
  fchmod 0600, _write_all, fsync, close; `os.link(temp, path)` (FileExistsError -> return
  False); finally unlink temp (missing_ok) and _fsync_directory(parent) only if published.
  `_get_or_create_kek_file_key` (231-245): mkdir parent, try _read_kek_file, else
  Fernet.generate_key + publish, on loss re-read winner. `_initialize_envelope` (472-498):
  generate DEK, txn insert-if-absent, on loss load winner row (None -> RuntimeError) and
  unwrap. Planned tests: publish helper as pub(crate) fn; reader ignores leftover partial
  `.secret_kek.*.tmp`; unwritable dir -> error, no final, no temp; EEXIST keeps winner.
- RF-F7 guard: `SecretStore._get_dek` (519-529): row -> unwrap; else
  `SELECT 1 FROM secrets LIMIT 1` -> RuntimeError "Secret envelope key material is missing
  while encrypted secrets exist."; else init key-file posture.
- RF-F5: crates/gcore/src/grant/bundle.rs::parse_grant_json (261-292) probes api_contract
  (ApiContractMismatch), strips `managed_api_token` envelope field (blank -> Malformed),
  deserializes GrantBundle (deny_unknown_fields -> PayloadSkew), then
  verify_payload_checksum. Server decoder must instead base64url-decode (pad like Python
  handshake.py:60) + serde_json::from_slice::<GrantBundle> only; keep parse_grant_json
  unchanged. PrincipalKind {Interactive, AgentRun, ToolChat, Maintenance}, is_managed().

## Working plan (repairs, uncommitted until R1 + review done)
1. RF-F3/F7 in 3.3; RF-F5 in 3.2; RF-F4 in 3.6; RF-F6 version bumps in every Rust leaf.
2. RF-F1 managed: challenge, capability-token handshake (agent/maintenance), managed
   presentation, durable revocation read migration + carriers, check_proxied for managed.
3. GET /schema; secrets routes; prompts routes + invalidation channel; classifier port;
   probe client port.
4. After R1: write path + R2 validator (a) or end-state text (b); D3/soak/E1/Granularity;
   corpus + e2e cases; docs; changelog entry at consensus only.
- /api/config non-effective routes (18): values GET/PATCH, schema, secrets GET/POST/DELETE,
  prompts GET list/GET/PUT/DELETE, template GET/PUT, tool-approvals/global GET, ui-settings GET,
  export POST, import POST, generation-endpoints/{name}/activate PUT,
  validation-detection/preview POST. Deps: most via gobby.config.values; secrets via
  storage.secrets + config_mutations + config_store; prompts via gobby.prompts.models;
  generation endpoints via gobby.ai.endpoint_activation (maybe #21587 family);
  validation-detection via gobby.config.validation_detection.
- Writes: storage/config_mutations.py emits pg_notify('gobby_config_changed', revision);
  ConfigRuntime._listen -> reconcile_revision activates any committed revision (plus
  _poll_revisions heal). Native writes can reuse this; activation stays Python consumer.
- D3 consequence: compare runs native leg for GET/HEAD only, so writes need a write-path
  soak check.
- Managed: HandshakeService.issue_for_agent (route runtime_handshake.py:166),
  issue_for_maintenance (maintenance.py in-process launch factory). grant.json rewrite is
  in-process (runner_init/servers.py::_rewrite_launch_grant). Revocation:
  ManagedCredentialStore.revoke -> SQL <auth>.revoke_principal, then in-memory
  GrantRevocationStore.revoke_execution. servers.py::_principal_revoked checks only
  interactive generations, so Rust needs a durable managed revocation read.

## Corpus

`tests/contracts/http/`: manifest families health(native), terminal_ws(native), config(proxy:
config_schema, config_values), tasks, runtime_handshake(proxy), runtime_challenge(native, gdaemon),
auth, front_door. README defines masks for runtime_handshake. Python harness
`tests/contracts/test_http_corpus.py` replays up cases against the e2e front-door daemon; check whether
its fixture can set `front_door.routes` (grep found none in tests/e2e).

## Draft structure (pending decisions)

P1 template: 1.1 RouteFamily seam to gcore (feature, `Router` with family state applied, instances
composed per listener, `native_for(&Parts)` predicate); 1.2 shared pool for auth resolver + lease +
family. P2: 2.1 compare mode (policy, JSON-aware equality, JSONL log, compare-report). P3 gruntime:
3.1 crate + config snapshot + capabilities + GET effective; 3.2 gcore grant sign/verify + rejection
matrix + DB revocation + GET runtime/config; 3.3 secret seal port with shared vector; 3.4 interactive
handshake; 3.5 handshake verify hook. P4: corpus cases + manifest, e2e backend-kill + flip/rollback,
docs. Deferred: managed issuance (S2.7 hub #24149), remaining /api/config routes, Python retirement.
Constraint: Stage 1 #23457/#23458/#23273 must land first; re-sweep serve.rs/state.rs/lease.

## Epoch 3 (post-compaction 2026-10-14 ~09:00Z)

- RF-F8 (#16491): 3.1 must target `crates/gcore/src/config/mod.rs::*` re-exporting the new
  accessor (+ returned type) via `gobby_core::config`; name accessor concretely. Accepted.
- #15977 rulings ~09:00Z: (1) managed handshake leaf PORTS rotation-time grant.json rewrite
  (rewrite_managed_grant_file + write_grant_file atomic private write + _issue_grant_capability),
  rotation+rewrite one step with predecessor-bootstrap restore; acceptance: failed native rewrite
  restores predecessor, grant.json never names revoked predecessor, parity vs Python rewrite.
  Boundary fact: materialize_managed_launch (launch-time) + rotate_due loop stay Python.
  (2) Identity: API-key only for operator; cookie traffic stays Python as END STATE (no deferral);
  soak write evidence via API-key client; gobby-agent-v1 bearers claimed on handshake +
  runtime/config with native verification; managed signing key = HMAC(bootstrap api_key,
  b"gobby-managed-token-v1") read by gdaemon at runtime; no agent/test reads live bootstrap.
- Route classification: R1-indep = GET schema (registry.json_schema(PUBLIC), static), GET secrets
  (list), DELETE secrets (no config mutation; embedding admission), prompts x4 (DB + PromptLoader
  cache; server.services.project_id), template GET + export POST (desired overrides YAML dump,
  yaml.safe_dump default_flow_style False sort_keys True). R1-dep = values GET/PATCH, ui-settings
  (active_values), tool-approvals (snapshot), secrets POST (_apply_internal config mutation when
  referenced), template PUT, import, gen-endpoint activate (+probe), validation-detection
  (config null -> active) + classifier.
- Release identity: bin_release_identity.workspace_source_identity = crate closure files excluding
  tests/benches/examples/docs dirs + root Cargo.toml + rust-toolchain + .cargo/config + locked deps.
  gcore/root-manifest change -> bump gdaemon+gcode+ghook; gruntime/gdaemon-only -> gdaemon.
  gclient/gterm in UNPUBLISHED_MANAGED_BINS -> not bumped. tests/install/test_version_pins.py::
  test_managed_bin_pins_match_crate_versions ties pins to Cargo versions. gcode floor:
  src/gobby/code_index/gcode_gateway.py::MIN_GCODE_GRAPH_VERSION = pin.
- Migration carriers (agy-full-integration.md precedent): migrations/<N>_*.sql; schema/assets.rs
  EmbeddedMigration; catalog.manifest.json; src/gobby/storage/schema_expected_identity.json
  (scripts/generate_schema_expected_identity.py, CI cmp); grant/bundle.rs + grant/tests.rs
  latest_version pins; gcore/tests/schema_contract.rs::embedded_assets_publish_a_complete_schema_identity;
  gdaemon/tests/cli_contract.rs; gcore/src/schema/runner_tests.rs::migrations_directory_exists_and_copy_agent_entry_is_registered;
  gcore/tests/catalog_manifest_freshness.rs::catalog_manifest_is_fresh_for_embedded_assets;
  tests/runtime_grants/golden/*.json; tests/storage/test_schema_contract.py. Latest migration 468.
- Managed revocation: Python in-memory only (GrantRevocationStore._executions after successful
  revoke_principal); DB truth principal_bindings.revoked_at (revoke_principal sets
  revocation_requested_at then revoked_at). New fn managed_principal_revoked(exec, gen) bool.
- Managed handshake path: auth_service._classify_agent_token (classify_agent_api_token HMAC w/
  managed_signing_key; _agent_capability_allows route matrix; _managed_capability_is_live:
  agent_runs.status in ACTIVE_AGENT_RUN_STATUSES or managed_execution_is_login_capable;
  _agent_identity_matches headers incl session ref resolution) -> issue_for_agent (claims==body
  machine/project; kind) -> _issue -> issue_grant_postgres managed branch: maintenance ->
  issue_maintenance; else get_live_binding_generation None -> issue, else
  reuse_current_during_drain or rotate (-> _rotate_if_generation -> _materialize_bootstrap +
  launch grant rewriter). DSN = managed_bootstrap_dsn(bootstrap_path).
- Managed challenge: gdaemon front_door/challenge.rs::answer forwards kind managed (pre-dispatch).
  Python: claims (_challenge_claims) -> _recompute_capability_signature(managed key, claims)
  (managed_token_signing_payload JSON sort_keys compact, 'gobby-agent-v1.'+b64) -> HMAC(secret, nonce) hex.

## Drafting progress (uncommitted edits in plan file, epoch 3)

DONE in plan: RF-F6 targets (crate Cargo.toml(s), version_pins.py::MANAGED_BIN_VERSION_PINS,
Cargo.lock) + "Release ownership (Constraints): ..." line + acceptance (test
tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions) in 1.1 (1.1.6, all
three), 1.2 (1.2.5 gdaemon), 2.1 (2.1.6), 3.1 (3.1.7 all three), 3.2 (3.2.7 all three), 3.3 (3.3.7),
3.4 (3.4.7), 3.5 (3.5.3). RF-F8 in 3.1: `contract_defaults() -> &'static [ContractDefault]`
{key, default, secret, machine_export}, target crates/gcore/src/config/mod.rs::*, 3.1.6; snapshot
keeps `overrides` (for 5.4 export). Table-driven claims in 3.1: `NATIVE_ROUTES: &[NativeRoute
{method, path (axum syntax), callers: &[Caller], gate}]`, Caller::Operator (x-gobby-key-id),
Caller::Agent (gobby-agent-v1 bearer; "4.2 adds it"); 3.1.3 test claims_follow_the_route_table.
RF-F5 in 3.2: structural decode_grant_header (pad+b64url+serde GrantBundle; drop top-level
managed_api_token str|null), row GET /api/runtime/config Operator gated interactive ("4.5 widens"),
3.2.5 malformed_and_missing_grant_headers_are_forwarded, 3.2.6
presentation_rejections_follow_python_order_without_the_backend (checksum->invalid_signature).
RF-F3/F7 in 3.3 (publish_private_file port; existing-secrets guard; 3.3.4 extended; 3.3.6
missing_envelope_with_existing_secrets_fails_closed). 3.4 row POST handshake Operator ("4.4 adds
Agent row"), 3.4.4 callerless_handshakes_are_not_claimed. 3.5 "operator" handshake.

TODO numbering plan: P4 managed = 4.1 revocation read (migration+carriers+Python
servers.py::_principal_revoked managed kinds), 4.2 agent-token verify + managed challenge
(Caller::Agent), 4.3 managed credential port (issue/issue_maintenance/reuse_current_during_drain/
rotate/_materialize_bootstrap + rotation-time grant.json rewrite w/ predecessor restore; parity vs
Python rewrite), 4.4 managed handshake route + managed check_proxied, 4.5 managed presentation.
P5 config = 5.1 GET schema (generator new flag --stdout-schema -> crates/gcore/assets/config/
public_config_schema.json; test in tests/config/test_runtime_config_contract.py; registry.py:291
json_schema(PUBLIC) deterministic; Python sets no cache-control), 5.2 secrets GET+DELETE
(embedding admission _assert_embedding_mutation_allowed config_store.py:479; delete 409/404 bodies),
5.3 prompts x4 + gobby_prompts_changed channel + Python listener (project_id =
server.services.project_id; parse_frontmatter YAML), 5.4 template GET + export POST (YAML emitter;
research yaml_serde vs PyYAML; advisor rec: pin Python _dump width unfolded, port 1.1 resolver,
cross-lang vector). R1 leaves after Josh. P6: 6.1 native default (old 3.6: + RF-F4 targets
src/gobby/config/bootstrap.py::FrontDoorConfig (docstring line 87 "Families absent from the map are
proxied") + crates/gcore/assets/config/runtime_config_contract.json::* regenerated unchanged via
scripts/generate_runtime_config_contract.py --stdout, test tests/config/
test_runtime_config_contract.py::test_checked_in_contract_matches_registry; gcore front_door.rs:90
doc; 6.1.2 checks both docs; RF-F6 all three), 6.2 corpus, 6.3 e2e (503 witness route must be
outside family since schema goes native), 6.4 docs. Framing: Overview, Constraints release
ownership text + boundary facts, DR D2/D4 rewrite + identity end state + envelope guard,
Granularity, delete D1/D2 sections, E1 V2-tense (memory da52a076: open with "completion gates...
none has run yet", must-statements).

## Epoch 4 research (2026-10-14 ~09:20Z, post-compaction)

- R1 still unanswered at 09:20Z. Restart for #24347 done 09:16Z (DAEMON BACK).
- Managed issuance (src/gobby/storage/managed_credentials.py, 957 lines -> do NOT target; 850 heuristic):
  issue(): _validate_expiry, heartbeat, password token_urlsafe(32), SQL issue_tool_principal(exec, session,
  machine, expiry, pw, path) for tool_chat else issue_principal(exec, owner_kind, session, agent_run, machine,
  expiry, pw, canonical path|None); scoped DSN = conninfo_to_dict(db.conninfo)+user/password/
  application_name=gobby-agent-<exec> -> make_conninfo; _materialize_bootstrap; on error revoke(gen,
  "issuance-rollback"); UniqueViolation -> CredentialAuthorizationError (-> claims_mismatch).
  issue_maintenance(): issue_maintenance_principal(exec, project, machine, expiry, pw, overlay); returns dsn.
  revoke(): loop revoke_principal(exec, gen) until >=0 or 5.0 s deadline (poll 0.05 s); error -> delete
  bootstrap (gen-checked) + revocation-retry.json (0600, sort_keys compact: credential_generation,
  failure_code, managed_execution_id, reason, recorded_at isoformat); <0 -> same w/ active_sessions_remaining;
  ok -> delete bootstrap, delete retry record, in-memory sink revoke_execution(exec, gen).
  get_live_binding_generation: managed_binding_states(machine) rows for exec with revocation_requested_at
  NULL and predecessor_drain_deadline NULL; exactly one -> generation.
  reuse_current_during_drain: draining row (deadline > now) required else None; len(bindings)==2 and
  _rotation_owner_is_live; one current; owner_kind/session/project/agent_run(exec if agent_run else None)/
  login_capable/expiry>now must match else CredentialAuthorizationError; read bootstrap.json O_RDONLY|
  O_NOFOLLOW|O_NONBLOCK, regular, 0600, uid; payload exec/gen/role/expires_at(fromisoformat)==; DSN dict ==
  scoped dsn dict; recheck bindings unchanged + owner live; returns bootstrap path. No heartbeat.
  rotate(): heartbeat; pred gen = get_live_binding_generation (None -> CredentialAuthorizationError);
  _rotate_if_generation None -> CredentialIssuanceError("lost binding race").
  _rotate_if_generation: owner live else None; if grant.json exists capture predecessor bootstrap bytes;
  rotate_principal_if_generation(exec, pred, expiry, pw) row None -> None; materialize successor bootstrap;
  if predecessor captured -> launch grant rewriter(credential, scoped_dsn). On error with successor: restore
  predecessor bootstrap (if captured) -> revoke(successor, "rotation-rollback") -> cancel_principal_rotation(
  exec, pred, succ); raise CredentialIssuanceError.
  _rotation_owner_is_live: bindings (expires_at datetime) for exec; one current, <=2 total; predecessor
  requested<deadline<=requested+5min, gen==cur-1, same owner_kind/agent_run_id/session_id/project_id/
  issuing_machine_id/code_overlay_project_id; maintenance -> login_capable; sessions status not expired/deleted
  (machine scoped); tool_chat -> True; agent_run -> agent_runs.status in (queued,pending,running).
  _materialize_bootstrap: runtime root <home>/runtime/managed-executions (0700 chmod), exec dir 0700;
  mkstemp .bootstrap-*.json, fchmod 0600, json sort_keys compact {database_url, credential_generation,
  expires_at isoformat, managed_execution_id, role_name}, fsync, os.replace. _restore_bootstrap: same with
  .bootstrap-rollback- prefix and raw bytes.
- issue_grant_postgres (runner_init/servers.py:409): expiry now+1h; maintenance requires execution_id
  (claims_mismatch); agent/tool require exec+session; None binding -> issue; else reuse or rotate; DSN for
  agent/tool = bootstrap.json database_url; CredentialAuthorizationError -> claims_mismatch; other ->
  credential_issuance_failed.
- _bind_runtime_grants: _rewrite_launch_grant(credential, scoped_dsn) -> launch.rewrite_managed_grant_file(
  bootstrap dir/grant.json, ...lease token/epoch/secret). _principal_revoked only interactive today.
- launch.py: rewrite_managed_grant_file: open grant.json, st_mode & 0o077 -> ValueError; GrantBundle
  model_validate_json (keeps managed_api_token); exec id must match; PostgresDirect(dsn, role, gen,
  valid_until=int(expiry)); model_copy deployment/capabilities.postgres/issued_at/expires_at; if
  managed_api_token present: key=read_managed_signing_key() else RuntimeError; token=_issue_grant_capability(
  unsigned, key, 86400); write_grant_file(path, sign_grant(unsigned, secret), managed_api_token=token).
  write_grant_file: mkdir 0700, mkstemp .grant-*.json, fchmod 0600, json.loads(model_dump_canonical()) +
  managed_api_token -> json.dumps(sort_keys, compact)+"\n", fsync, os.replace.
  _issue_grant_capability: tool_chat -> issue_tool_api_token(ttl max(1,ceil(86400))=86400, kind tool_chat,
  owner managed_execution_id); maintenance -> issue_maintenance_api_token(session_id=exec, kind maintenance,
  86400); agent_run -> issue_agent_api_token(ttl ceil(86400)+60=86460, kind agent_run, owner agent_run_id).
- local_token.py: token = "gobby-agent-v1." + b64url_nopad(json(sorted compact payload)) + "." +
  b64url_nopad(HMAC-SHA256(key, "gobby-agent-v1.<payload>")). payload keys exp,iat,machine_id,project_id,
  session_id + present agent_run_id/managed_execution_id/kind. classify: 3 parts & version else invalid_token;
  no key -> signing_key_unavailable; sig mismatch/bad b64/json/not dict -> capability_invalid; exactly one
  owner claim nonempty str; session/project/machine nonempty str; iat/exp int not bool; now>=exp ->
  capability_expired; kind None or nonempty str.
  Signing key = HMAC(api_key, b"gobby-managed-token-v1"); AuthService.managed_signing_key re-reads on
  (path, ino, mtime_ns, size) change.
- auth_service: _classify_agent_token = classify -> _agent_capability_allows (matrix: POST
  /api/runtime/handshake bind True; GET /api/runtime/config bind True) else route_not_permitted ->
  _managed_capability_is_live (agent_run_id -> agent_runs.status in active; managed_execution_id ->
  managed_execution_is_login_capable) else run_inactive -> _agent_identity_matches else identity_mismatch.
  identity: caller project = X-Gobby-Caller-Project-Id or X-Gobby-Project-Id; bind requires caller project
  and X-Gobby-Session-Id; project == claims; session == claim or resolve_session_reference(ref) == claim;
  owner headers X-Gobby-Agent-Run-Id / X-Gobby-Managed-Execution-Id must match claim kind; bind w/o owner
  header -> False.
  _legacy_rejection order: break-glass (X-Gobby-Break-Glass loopback) -> front-door identity -> bearer ->
  cookie. Middleware (servers/middleware/auth.py AuthMiddleware.dispatch) returns status (401) with
  {"error": msg, "code": code}; msg = login guidance for None/missing_auth/invalid_token/session_invalid else
  "Request rejected". /api/runtime/handshake/challenge is a public path.
- Routes: runtime_config GET: is_request_authenticated else 401 "Authentication required"; no grant 401
  "Runtime grant required"; decode error 400 "malformed grant"; present() rejection -> grant_error_response;
  NO bearer_matches_grant on this route. handshake POST: verified_agent_claims -> issue_for_agent(claims,
  body machine/project) (claims mismatch -> claims_mismatch; kind = claims.kind or agent_run/tool_chat;
  maintenance session None) -> _issue TTL min(3600, token_exp-now, valid_until-now) (min 1 each).
  challenge: Authorization -> 401 credential_before_proof; nonce b64 error 400 "invalid nonce"; interactive
  -> 400 interactive_challenge_is_native; managed w/o key -> 503 signing_key_unavailable; _challenge_claims
  (caller.claims over claims; session/project/machine nonempty str, iat/exp int, kind None|nonempty str else
  403 claims_mismatch); proof = hex HMAC(HMAC(key, "gobby-agent-v1."+b64(payload)), nonce).
- gdaemon front door: challenge::answer (pre-dispatch, backend + node doors) answers interactive, forwards
  managed unchanged (body re-attached); AuthState::authenticate passes gobby-agent-v1 bearers untouched (no
  identity headers). Agent requests reach table.dispatch.
- Revocation SQL: principal_bindings.revoked_at; managed_binding_states / managed_execution_is_login_capable
  pattern: LANGUAGE sql SECURITY DEFINER SET search_path 'pg_catalog','gobby_agent_auth'; OWNER
  gobby_agent_issuer; REVOKE ALL FROM PUBLIC; GRANT EXECUTE TO gobby_daemon_runtime.
- Migration-468 literals: crates/gcore/tests/schema_contract.rs:21,24; crates/gcore/src/schema/assets.rs:338-341;
  crates/gcore/src/grant/bundle.rs:218 (+ GOLDEN_LATEST_CHECKSUM/GOLDEN_ASSETS_ROOT_HASH consts);
  src/gobby/storage/schema_expected_identity.json; tests/runtime_grants/golden/*.json (5, signed; regen per
  tests/runtime_grants/test_golden_vectors.py docstring). grant/tests.rs and gdaemon cli_contract.rs compare
  dynamically.
- Crates: gdaemon has hmac 0.12, sha2 0.10, base64 0.22, rand 0.8, libc, tempfile, time 0.3 (formatting,
  parsing). gcore grant/cache.rs write_grant_file omits managed_api_token and deletes settings cache;
  write_bytes_atomic private; bundle.rs model_dump_canonical pub (sorted + "\n").
- Advisor (epoch 4): challenge as family route needs Caller::Public; gate Agent rows on session header ==
  claim (no resolver port); split issuance vs rotation; managed_generation_revoked on
  InteractiveCredentialMixin (interactive_credentials.py 428 lines); E1: compare proxies handshake POST, so
  native rotation gets no soak evidence -> e2e adds managed handshake + forced rotation under native default.

## Epoch 4 decisions (P4 design, settled with advisor; nothing written into the plan yet)

P4 = 7 leaves, linear after 3.5 (lib.rs shared):
- 4.1 Durable managed revocation read: migration 469_managed_principal_revoked.sql (next free number at
  executor tip; 469 at plan time) defining gobby_agent_auth.managed_principal_revoked(p_execution_id uuid,
  p_generation integer) RETURNS boolean = NOT EXISTS(binding with that exec+generation AND revoked_at IS
  NULL) (revocation_requested_at alone does not count; drain stays live); 466 boilerplate (LANGUAGE sql
  STABLE SECURITY DEFINER, SET search_path pg_catalog,gobby_agent_auth, OWNER gobby_agent_issuer, REVOKE
  PUBLIC, GRANT EXECUTE gobby_daemon_runtime). Carriers by literal only: assets.rs::* (EmbeddedMigration),
  catalog.manifest.json::* (UPDATE_GCORE_SCHEMA_MANIFEST=1 regen), schema_expected_identity.json::*
  (scripts/generate_schema_expected_identity.py), grant/bundle.rs::* (latest_version 468 literal +
  GOLDEN_LATEST_CHECKSUM + GOLDEN_ASSETS_ROOT_HASH consts), gcore/tests/schema_contract.rs::
  embedded_assets_publish_a_complete_schema_identity, tests/runtime_grants/golden/*.json x5 (re-sign per
  test_golden_vectors.py docstring). NOT grant/tests.rs, NOT cli_contract.rs, NOT runner_tests.rs (no
  literal; migrations_directory_exists... test does not exist). Python: add
  InteractiveCredentialMixin.managed_generation_revoked (src/gobby/storage/interactive_credentials.py, 428
  lines) and servers.py::_bind_runtime_grants _principal_revoked calls it for managed kinds (keep the
  non-int-generation early return False). Bumps gdaemon+gcode+ghook. Native rollback revokes reach Python
  presentation only through this read, so rotation leaves depend on 4.1.
- 4.2 Agent capability verification: gruntime src/agent.rs (classify port, payload encoder = Python
  json.dumps sort_keys compact ensure_ascii, openssl HMAC as 3.2, signing key read per request from
  <home>/bootstrap.yaml via gobby_core::bootstrap::parse_hub_database_bootstrap(..).api_key as
  front_door/challenge.rs does, liveness, identity), lib.rs Caller::Agent = Bearer gobby-agent-v1. AND no
  X-Gobby-Break-Glass AND X-Gobby-Session-Id absent or equal to the (unverified) payload session_id (ref
  spellings stay on Python, which resolves them; gcore sends canonical UUIDs). Failures -> 401 {"error":
  "Request rejected"|login guidance for invalid_token, "code"}. Python vector test for classifications.
- 4.3 Managed challenge family route: row POST /api/runtime/handshake/challenge for new Caller::Public (no
  Authorization header; only on this row; challenge::answer already forwards only managed). Reword 3.1 +
  3.1.3: "a caller not in the row's list is forwarded". Handler: claims (caller.claims over claims) ->
  403 claims_mismatch; key missing -> 503 {"detail":"signing_key_unavailable"}; proof hex HMAC(HMAC(key,
  "gobby-agent-v1."+b64(payload)), nonce); 200 {"proof"} no-store. check_proxied recomputes. Rejected:
  answering in challenge.rs pre-dispatch (no compare evidence, no proxy rollback).
- 4.4 Managed issuance (gruntime src/managed/mod.rs): issue/issue_maintenance/materialize bootstrap/revoke
  w/ 5 s/50 ms poll + revocation-retry.json + gen-checked delete/live generation; 23505 -> claims_mismatch;
  bootstrap vector vs Python bytes; expires_at isoformat via time 0.3 (gdaemon already locks it).
- 4.5 Managed rotation + grant.json rewrite (managed/rotation.rs + launch_grant.rs; mint in agent.rs):
  reuse_current_during_drain, rotate, owner liveness, predecessor capture only when grant.json exists,
  rollback order restore -> revoke(successor) -> cancel_principal_rotation; rewrite preconditions (0o077,
  exec id), keeps managed_api_token presence; own writer (not cache::write_grant_file); TTL agent 86460,
  tool/maintenance 86400; frozen-clock parity vector + Python drift test; acceptance: failed rewrite restores
  predecessor, grant.json never names revoked predecessor, parity.
- 4.6 Managed handshake route: Agent row POST /api/runtime/handshake; 422 on bad body; claims vs body
  machine/project -> claims_mismatch; no project admission; TTL min(3600, exp-now, valid_until-now);
  managed check_proxied in verify.rs.
- 4.7 Managed presentation: Agent row GET /api/runtime/config; gate widened to managed kinds; managed
  revocation through 4.1 read; Python route does not run bearer_matches_grant (runtime routes are not in
  _GRANT_ROUTES).
- P4 text says "the snapshot 3.1 serves", never desired/active (R1 insulation).
- E1/e2e: compare proxies handshake POST, so native rotation gets no soak evidence; e2e adds managed
  handshake + forced rotation under native default. DR "Family identity" bullet must drop agent-bearer from
  forwarded callers.

## P5 facts gathered (epoch 4)

- Route modules: src/gobby/servers/routes/configuration_{values,secrets,prompts,templates,import_export}.py.
  GET /schema in configuration_values.py:20 -> context.get_config_service().schema().
- Secrets: GET /secrets -> SecretStore.list() (global scope: SELECT id,name,category,description,created_at,
  updated_at,project_id FROM secrets WHERE project_id=GLOBAL ORDER BY name) -> SecretInfo.to_dict {id, name,
  category, description, created_at/updated_at datetime_to_required_iso, project_id, scope global|project}
  + "categories": sorted(VALID_CATEGORIES {general, llm, mcp_server, memory, integration}); 500 "Internal
  server error". DELETE /secrets/{name} (require_mutation_auth: is_request_authenticated else 401 detail
  "Authentication required. Supply the local runtime token or log in with configured web credentials."):
  existed=exists(name); ConfigStore.delete_named_secret: normalize_secret_name; embedding_mutation_context;
  if name == EMBEDDING_API_KEY_SECRET_NAME -> _assert_embedding_mutation_allowed (active switch run ->
  EmbeddingConfigMutationBlocked -> route's generic 500); referenced (find_persisted_secret_references)
  -> False; else SecretStore.delete (global scope). Not deleted: existed -> 409 "Secret '{name}' is still
  referenced" else 404 "Secret '{name}' not found"; ok -> {"ok": true}.
- Still to research: prompts routes (configuration_prompts.py lines 34/98/138/187), template GET
  (configuration_templates.py:20), export POST (configuration_import_export.py:23) + _dump YAML,
  config service schema() vs ConfigRegistry.json_schema(PUBLIC), find_persisted_secret_references,
  embedding_mutation_context, normalize_secret_name, datetime_to_required_iso.

## Epoch 5 progress (2026-10-14, uncommitted; plan validates exit 0 with warnings)

- WRITTEN in plan: P4 "Managed principals" framing + 4.1-4.7 per Epoch 4 decisions (4.1 also lifts
  `_principal_revoked` to module-level `_grant_principal_revoked` + functools.partial; 4.1 targets
  crates/gdaemon/tests/cli_contract.rs::* because the validator's derived-carrier table demands it;
  Python tests run with GOBBY_TEST_GDAEMON=checkout). 4.6 check_proxied: 200 for a token native rejects
  -> Mismatch("/authorization"). 3.1 caller text + 3.1.3 reworded (Public/Agent, per-caller builder that
  4.2/4.3 extend; both target tests/config.rs). 3.2 "4.7 widens"; 3.4 "4.6 adds the Agent row".
- RESTRUCTURED: old 3.6 -> 6.1 (depends 4.7; becomes 5.4 once P5 exists), old P4 4.1-4.3 -> 6.2-6.4
  under "## P6: Native default, parity evidence and the template docs". Deferred D1/D2 sections DELETED.
  P5 not yet written (insert between 4.7 and "## P6"); then 6.1 depends on 5.4.
- Lint lessons: body lines with an intent verb (add/create/delete/edit/.../rename/replace/update/wire)
  before a backticked filename require that file as a Target -> phrase runtime files passively.
- Validation WARNINGS to clear: MANAGED_BIN_VERSION_PINS consumers in every leaf (add a "Consumers
  unchanged:" inventory per leaf: install_setup_*.py, install_setup_versions.py, code_index/gcode_gateway.py,
  hooks/runtime_compat.py, ... get full list via `rtk recall dc6940338861` or re-validate);
  4.1 InteractiveCredentialMixin consumer src/gobby/storage/managed_credentials.py (Consumers unchanged:
  inherits the mixin; new method only).
- STILL TODO: P5 research (prompts x4 configuration_prompts.py:34/98/138/187; template GET
  configuration_templates.py:20; export POST configuration_import_export.py:23 + _dump; schema() source;
  find_persisted_secret_references; embedding_mutation_context; normalize_secret_name;
  datetime_to_required_iso), draft P5 5.1-5.4, fix 6.1 depends, 6.3 e2e additions (managed handshake with
  forced rotation under native default; 503 witness route outside the family since GET /api/config/schema
  goes native in 5.1), E1 rewrite (completion-gate tense, managed soak evidence: agent handshakes record
  `verified`, managed config reads `match`), framing rewrite (Overview: managed now native; Constraints
  boundary facts; DR: replace D2/D4, Family identity drops agent-bearer, managed challenge placement,
  session-ref gate; Granularity count 4.x/5.x/6.x; delete references to "Deferred"), V1 entry at consensus.
- R1 still unanswered (checked 2026-10-14 post-compaction).

## Epoch 6 (2026-10-14, post-compaction) P5 research

- #15977 ruling (prompt cache, supersedes 08:40Z): Option A. Delete PromptLoader._cache, _on_change, the
  notifier param, clear_cache, unused PromptChangeNotifier, both no-op route calls (route
  get_prompt_loader builds a NEW loader per call, configuration_context.py:106). No channel/listener.
  Condition: plan evidence lists every load()/render() site and confirms none loops. Acceptance: UI
  override via route seen by next load() on an existing long-lived loader, no restart.
  Verified sites (each renders once per operation before one LLM call): importer.py import_from_github
  /import_from_query/_render_import_prompt (227/287/318); recommendation.py _recommend_hybrid 183 (the
  `for c in candidates` is the candidate_list join before render), _recommend_llm 240; extraction.py
  extract_entities/extract_relationships/select_outdated_relations (123/174/263; loops are after render,
  over the LLM result); tool_summarizer.py 83/88 and 173/180; expansion/_compile.py _render_prompt from
  _invoke_llm_compile 285/286; summary_context.load_summary_prompt_template (new loader each call);
  feedback/service.py _distill 137 (render once, 2 LLM retries); dream/planner.py 228.
  Notifier refs: storage/prompts.py PromptChangeEvent/PromptChangeNotifier/ChangeListener/
  ChangeEventType/__all__/LocalPromptManager.__init__ notifier/_notify_change (+calls in create/update/
  delete_prompt); tests/prompts/test_prompts_loader.py test_cache_behavior/
  test_cache_invalidation_via_notifier; tests/storage/test_storage_prompts.py TestPromptChangeNotifier.
- Prompts routes (configuration_prompts.py): GET /prompts (limit default 500 ge 1, capped 1000; offset
  ge 0; list_prompts(project_id=services.project_id, enabled=True) ORDER BY name ASC, scope ASC;
  count_prompts COUNT DISTINCT name; dedupe first per name; override = any record scope global|project;
  category = name.split('/')[0] or "general"; response {prompts[{path,description,category,source
  overridden|bundled,has_override}], categories{cat:n}, total, limit, offset, count}; 500 "Internal
  server error"). GET /prompts/{path} get_by_name precedence project>global>bundled enabled only; 404
  "Prompt '{path}' not found"; bundled_content when override; variables via _normalize_variable_spec.
  PUT: parse_frontmatter (models.py:14, PyYAML safe_load, non-dict -> {}), description default "",
  version str(...) default "1.0", variables raw; body.strip() empty -> 400 "Prompt content must not be
  empty"; get_override -> update_prompt else create_prompt(scope project if project_id else global;
  id uuid5(_NS_PROMPTS, f"{name}:{scope}:{project_id or 'none'}") else uuid4). DELETE: delete_override
  -> 404 "No override for '{path}'"; ok {"ok": true}. gcore has yaml_serde 0.10.4 (serde_yaml alias).
- Template GET + export POST both = ConfigDocumentsService.export_yaml: reads ConfigRuntime in-process
  snapshot (503 runtime_unavailable if not ready), returns {revision: snapshot.revision, content:
  yaml.safe_dump(unflatten(desired_overrides filtered: registry-known, source_path not None, not
  RESTRICTED, not MANAGED; REFERENCE secrecy -> MASKED_SECRET unless None/""; structured references
  masked), default_flow_style=False, sort_keys=True)}. desired_overrides = stored.overrides of last
  reconciled revision -> reads the RUNTIME snapshot => R1-dependent (reclassify).
- GET /schema: ConfigValuesService.schema -> registry.json_schema(PUBLIC) (static, deterministic).
- Secrets DELETE: ConfigStore.delete_named_secret: normalize (strip+lower); txn immediate with advisory
  lock pg_advisory_xact_lock(hashtext('gobby.storage.config_mutations.EmbeddingSwitchJournalMutation:
  EmbeddingSwitchJournalMutation()')); if embedding api key secret -> _active_switch_run_id (SELECT
  value FROM config_store WHERE key=journal FOR UPDATE; malformed -> blocked) -> blocked raises ->
  generic 500; referenced (find_persisted_secret_references: all config_store values + mcp_servers
  env/headers (project-filtered if project_id) scanned for SECRET_REF_PATTERN, normalized) -> False;
  SecretStore.delete global scope.

## Epoch 6 state (end)

- #15977 rulings 2026-10-14: prompt cache Option A (delete cache/notifier; no channel); prompt PUT
  Option A (JSON {content, description?, version?, variables?}; omitted fields inherit from existing
  override else get_by_name; frontmatter-led content -> 400; shared corpus). Export/template GET stay
  R1-independent (snapshot revision = stored.revision; overrides = stored.overrides).
- WRITTEN (uncommitted): P5 5.1 schema (derived from contract; no new carrier), 5.2 secrets list+delete,
  5.3 prompt cache removal (Python), 5.4 prompt JSON contract + native routes, 5.5 template/export
  (contract gains daemonKey; pyyaml.rs emitter port; Python _dump width=math.inf). 6.1 depends 5.5.
  Validate exit 0 with warnings.
- Warnings to clear: MANAGED_BIN_VERSION_PINS consumers (every leaf); 4.1 managed_credentials.py;
  5.3 PromptLoader consumers (feedback/service.py, mcp_proxy/tools/sessions/_terminal.py,
  memory/dream/planner.py, knowledge_graph/extraction.py + service.py, sessions/summary_context.py,
  tests/...); 5.3/5.4 register_prompt_routes + SavePromptOverrideRequest consumer
  servers/routes/configuration.py. Full list: rerun validate.
- Plan Edit reported "file modified on disk since last read" -> check `git diff --stat` and that no
  other session edited the plan.
- Remaining: 6.2 config_schema.json scope-reason now wrong (schema is native in 5.1); 6.3 e2e additions;
  6.1 lacks version-bump targets though it edits gcore front_door.rs (check); framing rewrite; E1.

## Epoch 7 (2026-10-14 ~10:40Z) progress; plan uncommitted, validate exit 0 with NO warnings

- No foreign edits found (git diff scan: no M1, no stray refs). DAEMON BACK 10:30Z (PID 37123).
- DONE: "Consumers unchanged:" inventories (pins in 1.1-5.5 + 6.1; 4.1 managed_credentials.py;
  5.3 PromptLoader consumers + configuration.py; 5.4 configuration.py). Script:
  scratchpad/inventories.py (dedupes; rerunnable).
- DONE: front_door.rs doc-comment change moved from 6.1 into 1.1 (+1.1.7 file item); "until 3.6" -> 6.1.
  6.1 now gruntime-only: targets gdaemon Cargo.toml, pins, Cargo.lock; 6.1.2 = gdaemon bump.
- DONE (found defect, fixed): compare recorded diff for every claimed GET while backend restarts
  (proxy::forward returns gdaemon's own 503/502). 2.1 now targets proxy.rs::* (adds try_forward);
  "Python not reached" -> skipped with detail backend_unavailable|bad_gateway; 2.1.7 test
  compare.rs::unreachable_backend_records_skipped. D3 in DR should mention it (framing TODO).
- DONE: 5.5 window text: runtime_unavailable unreachable over HTTP (GobbyRunner.create returns after
  ConfigRuntime.start; shutdown drains uvicorn before config_runtime.close at
  runner_lifecycle_shutdown.py 619/717); only commit->reconcile remains, excluded as pending activation.
- DONE: 6.2 config_schema scope-reason (native via 5.1); research note: no recorded cases for other P5
  routes (prompt churn; template YAML embeds e2e terminal_host.socket_dir override, unmaskable).
- DONE: 6.3 managed fixture (agent run as test_stateless_ambient_session); compare phase adds P5 routes
  + managed challenge; kill phase: /api/sessions typed 503 witness + managed flow (challenge proof via
  challenge_proof; handshake#1 issues G1; materialize_managed_launch into execution dir; handshake#2
  rotates to G2 + rewrites grant.json; G2 presents). New 6.3.4.
- DONE: E1 rewritten (completion-gate tense; full pytest list; soak probe heredoc via DaemonClient,
  operator-run; per-route count over compare log; backend restarts allowed via 2.1 skip).
- TODO framing: Overview (managed + 10 config routes native; "part of /api/config"); Constraints:
  add "Release ownership" bullet (gcore/root manifest -> gdaemon+gcode+ghook; gruntime/gdaemon-only ->
  gdaemon; gclient/gterm unpublished never bumped) + boundary facts (materialize_managed_launch and
  rotate_due stay Python; cookie + break-glass + session-ref spellings stay Python as end state);
  DR: replace D2 (managed native ruling 2026-10-14) and D4 (Option A: 18 routes; P5 serves 9 + 3.1
  effective; write NO R1 text), D3 add skipped-on-unreachable, Family identity -> caller order
  (Operator key-id / Public challenge-only / gated Agent bearer / none), managed challenge placement
  (rejected: answering in challenge::answer), prompt rulings x2, Q1 "3.6"->6.1 twice; Granularity:
  24 deliverables (2+1+5+7+5+4), lib.rs chain 3.1-6.1, pins nearly all. Leave D1 verbatim (R1).
- TODO at consensus: V1 entry noting deferred sections removed (old entry mentions TBD-at-expansion).
- R1 still unanswered.

## Epoch 8 (2026-10-14, post-compaction): framing rewrite DONE, validate exit 0, no warnings

- Overview: lists P3/P4/P5 native surfaces (no R1 text). Constraints: rollback-target bullet widened
  to configuration*.py modules (5.3/5.4 edit two in place); new End-state boundary and Release
  ownership bullets. DR: header dated 10-13 + 10-14; D2 rewritten (managed native, rejected 10-13
  ruling); D3 + write-path verifiers + skipped-on-unreachable + API-key soak writes; D4 rewritten
  WITHOUT R1 text (no mention of the other eight routes or pg_notify; add with R1); Family identity
  = caller order; Q1 3.6 -> 6.1; Q2 + managed handshakes; new entries: managed challenge placement,
  prompt cache, prompt save contract. Granularity: 24 deliverables, sharing, size reasons.
- Dependency change: 5.3 has no dependency (shares no Target with earlier leaves); 5.4 depends on
  5.2, 5.3. P5 framing says so.
- R1 TODO when answered: D4 (other eight routes, pg_notify reuse, ConfigRuntime activation), D1,
  Overview route list, Granularity counts + size reasons, 6.2 config_values.json scope-reason,
  Constraints rollback bullet (under (b) configuration_values.py etc. stay Python-owned), R1 leaves.
- Advisor pass applied: rollback bullet made R1-neutral; Q2 derivation moved to D3; Granularity
  states hand-maintained file counts (1.1=7, 2.1=7, 3.2=8, 5.5=9) with reasons; Release ownership
  notes workspace_source_identity skips tests/benches/examples/docs paths, so 6.2 bumps nothing.

## Epoch 8 R1 research (Josh ruled R1 = (a): Python publishes active state; ALL config native)

- Coordinator ask: amend plan, no open questions/deferrals, commit, send #15977 + adversary #16491 SHA.
  #16787 (lane-2 adversary) holding f279144 re-review until amendment SHA; send it too.
- ConfigRuntime swaps `self._bundle` at runtime.py:189 (start), 285 (reprepare_subscriber), 370
  (register_subscriber), 508 (_reconcile_locked). `_revision_publisher` fires only in
  _reconcile_locked (runner_init/servers.py::register_config_event_publisher: endpoint health +
  websocket broadcast). Active publish must run on EVERY bundle swap (failed_live changes too).
- ConfigSnapshot (runtime_models.py): revision, row_revisions, pending_restart_keys,
  failed_live_keys {key: ApplyFailure(revision, subscriber, keys, message)}, desired/active
  values (flat, complete incl defaults = stored.values), desired/active overrides, desired/active
  bindings RuntimeSecretBinding(reference, plaintext, fingerprint). project_activation
  (runtime_activation.py:84): active_* keep previous for non-activated keys; pending via
  _pending_restart_keys (RESTART_REQUIRED keys whose value or binding fingerprint differ).
- Table key: deployment_token (src/gobby/deployment.py::deployment_token; gcore grant::deployment_token).
  Bindings: publish reference + fingerprint + SEALED plaintext (3.3 seal) — plaintext never in clear.
- values GET body = ConfigValuesService._snapshot_body (values.py:582): revision, desired, active
  (unflatten public projection, masks), secret_set, pending_restart_keys (public), failed_live_keys
  {key:{revision,subscriber}} (public). 503 runtime_unavailable if runtime not ready.
- PATCH (values.py:357 patch_flat): authorize; secrets -> SecretUpdate (MASKED skip; `$secret:`
  rejected); structured refs; _anchored_snapshot CAS vs ACTIVE snapshot.revision (409
  revision_conflict); generation-endpoint evidence rules (reject_unprobed_responses_endpoints,
  reject_unverified_generation_endpoint_evidence, clear_stale_generation_endpoint_probe_evidence);
  mutations.patch; then reconcile_local_commit -> _mutation_body (apply_status failed_live |
  pending_restart | applied); reconcile exception -> 200 apply_status reconcile_failed with
  failed_live_keys = changed keys -> {revision, subscriber "runtime"}. Unknown exception -> 500
  persistence_indeterminate.
- ConfigMutations._apply (storage/config_mutations.py:265): lock config_state revision, CAS,
  read rows, _scoped_patch, _validate_embedding_admission (journal key; 409
  embedding_mutation_blocked), _validate_candidate (per-key _validate_value = TypeAdapter strict
  JSON validate + dump mode json -> STORED CANONICAL FORM; message "Invalid value for
  configuration key {key!r}: {str(pydantic exc)}" — includes pydantic URL/version!; secret refs
  resolve via secret_store.get; then repository.runtime_candidate (DaemonConfig.model_validate);
  _validation_detail "loc: msg; ..."), _effective_changes, _write_changes, UPDATE config_state,
  pg_notify('gobby_config_changed', rev).
- Validators: ~80 @model/field_validator in src/gobby/config (persistence 17, ai 10, servers 6,
  app 5, features 4, feature_base 4, cron 4, validation_matchers 3, tasks 3, sessions 3, voice 2,
  validation_detection 2, ui 2, skills 2, logging 2, code_index 2, bin_freshness 2, system_loops,
  hooks, extensions, database_concurrency, communications 1 each) + telemetry/config.py 1.
- Template PUT + import POST: documents.py::replace_yaml -> _prepare: yaml.safe_load (PyYAML 1.1;
  error "Invalid YAML: {exc}" with PyYAML marks), mapping check, flatten_config, per-key
  registry checks (unknown, not daemon-owned, managed), secrets via _prepare_secret, structured
  refs, endpoint evidence rules (document=True), runtime_candidate validate, replace_namespace
  CAS; masked restoration needs snapshot.revision == expected. Rust YAML: only yaml_serde 0.10.4
  (libyaml-rs; no scalar style -> cannot do YAML 1.1 resolution); libyaml-rs API unsafe (forbid).
- Secrets POST: ConfigStore.set_named_secret (validates name, may _apply_internal config mutation
  when referenced) -> {"ok": true, "secret": info}; ValueError -> 400 detail; require_mutation_auth.
- ui-settings GET: active_values ui_settings.<11 keys> non-null. tool-approvals/global GET:
  active_values tool_approvals.global_rules -> sanitize (trim/dedupe) else defaults
  (tool:Read/Glob/Grep/Ls), default_rules, built_in_exemptions ("mcp:gobby*:*", "tool:Bash (gcode only)").
- gen-endpoint activate (configuration_generation_endpoints.py:43): desired secret/config;
  422s; GenerationConfig validate; probe via ai/endpoint_activation.py (576 lines): responses
  wire_api uses CodexAppServerClient (Codex CLI app-server subprocess!) + vision; chat completions
  via local provider adapters (+ vLLM served-model resolve); then patch_flat(probe_verified=True);
  response adds endpoint/provider/model/modalities/probe_diagnostics.
- validation-detection preview: config given -> ValidationDetectionConfig.model_validate (400
  str(exc)); else ACTIVE validation_detection; classify_validation_command
  (config/validation_detection.py 862 lines + validation_matchers.py 605 lines).
- Rust LLM infra exists in gcore/src/ai (generation transport, chat/completions via reqwest).
  No Rust Codex app-server client.
- #15977 rulings (2026-10-14): Q-A Option 1 (both sides YAML 1.2 core; Python _prepare uses SafeLoader
  subclass with resolvers matching yaml_serde; shared YAML->JSON corpus incl 1.1/1.2 divergence,
  anchors, merge keys, tags, malformed; 400 validation_error path ["content"] "Invalid YAML at line
  L, column C"; no compat path). Q-B Option 1 (Python _validate_value, gen-endpoint activate 422,
  validation-detection preview 400 switch to "<loc>: <msg>; ..."; Rust emits pydantic msg per
  constraint kind; R2 corpus asserts byte-equal messages AND stored canonical values).
- Advisor structure: new leaf 2.2 "active config state" (P2 renamed "Compare mode and active config
  state"); 2.2 takes migration 469, 4.1 -> next free (470 at plan time); publish on every bundle swap;
  no row -> 503 runtime_unavailable; both CAS checks in Python order (active snapshot first, then
  locked config_state); 3.1 desired SQL moves to write leaf; write wait polls row revision bounded by
  ConfigRuntime _CONFIRMING_READS/_CONFIRM_BACKOFF; timeout -> reconcile_failed body (UI handles it:
  web/src/api/config.ts:282,568). Sealed bindings via SecretStore.seal (secrets.py:720, AES-GCM, aad).
  fingerprint = sha256(b"\x01"+utf8) (runtime_projection.py::_secret_fingerprint). Classifier lookahead
  only in evidence fns (not preview path); fancy-regex + shlex 1.3.0 already locked.

## Epoch 8 R1 DESIGN (settled; nothing written into plan yet for R1 leaves)

- New leaf 2.2 "Active config state table" (P2 renamed "Compare mode and active config state"),
  depends 2.1; 3.1 depends 2.2. Migration 469_config_active_state.sql (4.1 renames to 470 + all its
  "469" literals/targets -> "next free, 470 at plan time"):
    CREATE TABLE public.config_active_state (deployment_token text PRIMARY KEY, revision bigint NOT
    NULL, state jsonb NOT NULL, published_at timestamptz NOT NULL DEFAULT now());
    REVOKE ALL ... FROM PUBLIC; GRANT SELECT, INSERT, UPDATE ... TO gobby_daemon_runtime;
  Carriers exactly as 4.1 (assets.rs MIGRATIONS, catalog.manifest.json regen, bundle.rs literals,
  schema_contract.rs, cli_contract.rs, schema_expected_identity.json, 5 golden grants re-signed);
  bumps gdaemon+gcode+ghook. Shared targets with 4.1 -> already ordered.
- state jsonb: desired_values, active_values, desired_overrides (flat maps, JSON values),
  pending_restart_keys (sorted list), failed_live_keys {key:{revision,subscriber}} (no message),
  desired_bindings + active_bindings {key:{reference,fingerprint,sealed}} where sealed =
  SecretStore.seal(plaintext utf8, aad=b"config-binding:{deployment_token}:{key}:{fingerprint}")
  or null when plaintext None. Plaintext never stored in clear.
- Python: ConfigRuntime gains ctor kwarg `active_state_publisher: Callable[[ConfigSnapshot],
  None] | None` run via `_run_db` after EVERY bundle swap (runtime.py:189 start, 285
  reprepare_subscriber, 370 register_subscriber, 508 _reconcile_locked). Built in
  runner_init/storage.py::open_storage_and_config (config_repository + runner.secret_store +
  deployment_token()). New repository method writes upsert (ON CONFLICT (deployment_token) DO
  UPDATE). Publish failure: log, remember unpublished; _poll_revisions tick republishes. close()
  leaves the row (backend-kill serves last state).
- Native read: no row -> 503 runtime_unavailable; message per Python route: values service
  "Configuration runtime is not ready"; context get_config_snapshot "Configuration runtime is still
  starting" (ui-settings, tool-approvals, validation-detection). Stale row from dead backend -> serve.
- P3 rework (D1 reversed): 3.1 reads row (effective revision = row revision; values = active_values)
  — 3.1.2/3.1.5 change (missing row -> 503); 3.1's desired_snapshot SQL moves to write leaf (rows
  under lock for validation); 3.4 capabilities from active values + active sealed bindings (open via
  3.3 seal port); 3.5 drop "D1 desired-vs-active divergence" text; 5.5 export reads
  desired_overrides from row, window text changes; 6.2 config_values.json -> native; every P3 test
  fixture seeds the row. D1 rewritten (rejected alt = desired-only reads). Overview/D4: all 18
  routes; Granularity counts. E1: new tests; soak probe adds PATCH witness on inert key reverted.
- Write path: CAS order = active row revision first (409 revision_conflict expected/actual=active),
  then SELECT revision FROM config_state FOR UPDATE (ConfigConflictError 409). Write wait: poll row
  revision >= committed, bound from ConfigRuntime._CONFIRMING_READS/_CONFIRM_BACKOFF (TODO read
  values); timeout -> Python reconcile_failed body (changed keys -> {revision, subscriber
  "runtime"}); UI handles reconcile_failed (web/src/api/config.ts:282,568).
- Remaining leaves to draft (P5 continues, after 5.5): values GET; ui-settings GET + tool-approvals
  GET; secrets POST; R2 validator (contract carrier constraints + ~80 hand-ported validators,
  module split validator/{mod,persistence,ai,servers,...}.rs; corpus asserts canonical dump bytes +
  messages; Python message change Q-B); values PATCH; YAML 1.2 shared parse (Python SafeLoader
  subclass + Rust yaml_serde; corpus) + template PUT + import POST; gen-endpoint activate + probe
  client (enumerate CodexAppServerClient methods used; chat-completions via gcore ai transport;
  vLLM served-model resolve); validation-detection preview + classifier port
  (classifier/{mod,matchers,...}.rs; shlex crate; ValidationDetectionConfig validate msg Q-B).
- TODO checks: require_mutation_auth callers (secrets POST gate); _CONFIRMING_READS values;
  web/ YAML normalization (evidence only); validators reachable from DaemonConfig only.

## Epoch 9 (2026-10-14, post-compaction) findings; advisor pass done; drafting next

- Routes: 19 /api/config routes (effective + 18). Remaining 9 after P5's nine: values GET, values
  PATCH, ui-settings GET, tool-approvals/global GET, secrets POST, template PUT, import POST,
  generation-endpoints/{endpoint_name}/activate is a PUT (not POST), validation-detection/preview POST.
- Write-wait bound: _CONFIRMING_READS=3/_CONFIRM_BACKOFF=0.05 bound only storage re-reads (~0.35 s);
  activation = serial _prepare_matching + _activate_many per matching subscriber under
  subscriber.prepare_timeout (default 5.0 runner_init/config_subscribers.py:87; 30.0 at
  runner_init/services.py:196). gdaemon proxy has NO request timeout; UI has none. Python awaits
  reconcile_local_commit unbounded. DECISION: native polls row every 50 ms for at most 65 s
  (2 x 30 s largest prepare_timeout for prepare+activate, + 5 s db statement timeout); timeout ->
  reconcile_failed body (committed true).
- Publisher placement: _reconcile_locked swap then _dispose_replaced then raise activation_failure
  -> publish immediately after `self._bundle = ...` assignment at all four sites (start 189,
  reprepare_subscriber 285, register_subscriber 370, _reconcile_locked 508).
- runtime.py 874 lines >= 850 -> production-size-growth: new bare .py target + named split.
  Plan: new src/gobby/config/runtime_active_state.py holds the publisher (state build, seal,
  upsert, unpublished retry flag); move `_dispose_late_result` (849-857) there? -> name the move.
- Secrets POST = ConfigStore.set_named_secret: embedding admission for embeddings_api_key; lock
  revision; referencing REFERENCE keys whose override references the name -> _apply_internal
  (CAS patch_internal, source "user", SecretUpdate(name, category, description)) then return
  info from secret_store.list; else canonical key (config_key_to_secret_name == name) ->
  validate_named_secret (= _validate_candidate with secret patch, allow_internal) then
  secret_store.set. No reconcile wait. ValueError -> 400 str(exc). Order after PATCH (reuses commit core).
- Validators: 94 registrations on 79 models reachable from DaemonConfig (scratchpad/validators.py).
  Per-key _validate_value uses BARE annotation (all 452 specs bare; Field constraints like
  max_active_agents Ge(1) are in model field metadata, enforced only by runtime_candidate =
  DaemonConfig.model_validate(sparse overrides nested via _runtime_path, secrets -> plaintext)).
  Keys whose annotation embeds models (per-key dump runs their validators, so stored bytes depend):
  hook_extensions.webhooks.endpoints (WebhookEndpointConfig.validate_url), ai.model_metadata_aliases
  (ModelMetadataAlias normalize_source_key, validate_target), validation_detection.wrapper_rules
  (ValidationCommandWrapper validate_id/label), validation_detection.custom_matchers
  (ValidationCommandMatcher validate_id/label/validate_bounded_inputs), voice.openai_compatible_audio
  (OpenAICompatibleAudioBindingConfig.validate_url), tool_approval.policies (ToolApprovalPolicy).
  Complete-candidate message: "Complete configuration candidate is invalid: {loc: msg; ...}"
  (_validation_detail, include_url False); error key = first loc resolving to registry key, else
  sole changed key; reference key -> "Secret configuration value is invalid".
  Q-B site 1: _validate_value message "Invalid value for configuration key {key!r}: {str(exc)}"
  -> use _validation_detail(exc) for ValidationError.
- cron: validate_nightly_repair_cron uses croniter.is_valid (expand); no Rust cron crate locked ->
  hand-port with corpus. tz: ZoneInfo; jiff 0.2.28 + jiff-tzdb locked (via gix-date) -> gruntime dep.
- Contract: generator scripts/generate_runtime_config_contract.py (unindexed; bare path) builds
  exactKeys/patterns from json_schema(PUBLIC|MACHINE). Put constraints in NEW top-level member
  `validation` {keys: {key: TypeAdapter(annotation).json_schema()}, patternFields, candidate:
  DaemonConfig.model_json_schema()} so 5.1 projection unchanged. 5.5 already edits generator.
- Contract entry fields: activation(live 399/restart_required 45/managed 5), default, description,
  key, machineExport, namespace, secrecy(none/reference), type (coarse), visibility, fields.
- deployment_token parity: gcore grant::deployment_token(home) = sha256(canonical path)[:16] same as
  src/gobby/deployment.py; plan 3.2 already uses it. No bootstrap read.
- runtime_unavailable messages: values.py:_snapshot + documents.py:165 "Configuration runtime is
  not ready"; configuration_context.get_config_snapshot "Configuration runtime is still starting"
  (effective, ui-settings, tool-approvals, validation-detection). Body = ConfigValuesError.public_body
  {"error":{code,message,path:[],retryable:true}} 503.
- 5.5 export must read row desired_overrides + revision (Python: snapshot desired_overrides); the
  "commit-to-reconcile divergence" paragraph goes away.
- Capabilities read snapshot.active (typed) + active_secret(key) -> 3.4 uses row active values +
  sealed active bindings.
- Leaf order decided: 2.2; P5 5.6 active reads (values/ui-settings/tool-approvals), 5.7 per-key
  validation, 5.8 complete-candidate validation, 5.9 values PATCH, 5.10 secrets POST, 5.11 YAML 1.2
  documents (template PUT + import), 5.12 gen-endpoint activate + probe client, 5.13
  validation-detection preview + classifier; 6.1 depends 5.13. Total 33 deliverables.

## Epoch 9 drafting progress (plan uncommitted; validate exit 0 WITH 2.2 consumer-coverage warnings)

DONE in plan:
- Overview: all 19 /api/config routes + active-state paragraph.
- DR: D1 rewritten (operator 2026-10-14, row; rejected desired reads + option b); D3 wording
  (P5 write, export or preview -> persisted state or native recomputation); D4 all nineteen;
  NEW entries after Prompt save contract: Write wait (50 ms poll, 65 s bound, reconcile_failed;
  rejected confirm-backoff bound), YAML documents (Q-A; NOTE status 422 not 400: every
  ConfigValuesError validation_error is 422 -> TELL #15977 in SHA message that the Q-A "400" in my
  proposal was wrong and plan keeps 422), Validation messages (Q-B), Config validation (R2: 5.7
  per-key `validation` contract member; 5.8 complete candidate, 94 validators, croniter hand-port,
  jiff tz).
- P2 renamed + framing para; 2.2 written (migration 469, carriers as 4.1, runtime_active_state.py
  doc+publisher, ConfigSnapshot desired_bindings/active_bindings properties,
  ConfigRepository.publish_active_state upsert, ConfigRuntime._publish_active at 4 swap sites,
  _unpublished_active retry under _reconcile_lock on poll tick, open_storage_and_config wiring;
  9 acceptance items 2.2.1-2.2.9).
- 3.1: depends 2.2; gcore runtime_contract.rs/mod.rs targets + contract_defaults REMOVED; config.rs
  = active_state(&Pool, token) + 503 runtime_unavailable "still starting" + machine_values over
  active_values using is_machine_config_key/is_secret_reference_key; research bullets replaced;
  3.1.2/3.1.5 rewritten; old 3.1.6 deleted, 3.1.7 -> 3.1.6.
- Removed conflicting consumer lines (2.2 servers.py; 5.3 storage.py).

TODO next (in order):
1. 2.2 consumer-coverage warnings: run scratchpad/inventories.py (adds "Consumers unchanged"
   blocks) or add lines for ConfigRuntime, ConfigSnapshot, ConfigRepository,
   open_storage_and_config consumers (full list: rtk recall 32ef13242865 / rerun validate).
   Also add 2.2 to Granularity size reasons (9 acceptance items; migration+carriers+first writer).
2. 3.4 text: line "capabilities from the desired snapshot" -> active state; capabilities.rs
   "against desired values and resolved secrets" -> row active_values + active_bindings opened via
   3.3 open_sealed with AAD config-binding:{token}:{key}:{fingerprint}.
3. 3.5: "fresh native computation from the desired snapshot (3.1, 3.4)" -> active state; replace
   "A D1 desired-versus-active divergence..." bullet.
4. 4.1: migration 469 -> 470 (filename target, text "469 is the next free number" -> "470 ... after
   2.2's 469"), 4.1.1 file path.
5. 5.5: step 1 reads row revision + desired_overrides, no row -> 503 "Configuration runtime is not
   ready"; replace "Python serves from its in-process snapshot..." paragraph; research bullet at
   "The snapshot is built with revision=stored.revision".
6. P5 framing: 18 routes, new leaves list; 6.1 depends 5.13; 6.2 config_values.json scope-reason
   -> replays against 5.6 native; research bullet on other P5 routes evidence.
7. Write leaves 5.6-5.13 (research each first; see Epoch 9 findings).
8. Granularity rewrite (33 deliverables; run scratchpad/targets.py for counts); E1 updates (new
   tests, soak probe PATCH witness on inert key reverted).
9. Validate clean (no warnings), commit plan only, send SHA+sha256 to #15977 and #16491 (mention
   422 correction). Arm wait_for_coordination(owner_session="#15977", reply=true).

## Epoch 10 progress (2026-10-14)

- 2.2 consumer warnings cleared: upsert moved into runtime_active_state.py
  (`ActiveStatePublisher(database, secret_store, token)`, hub transaction), ConfigRepository
  no longer targeted; 48-line consumer inventory added (scratchpad/consumers_22.py).
  NOTE: if a 5.x leaf targets tests/servers/routes/test_config_values_api.py,
  test_configuration_effective_routes.py, test_configuration_routes.py, servers/http.py,
  runtime_grants/service.py etc., remove that line from 2.2's inventory.
- 3.4: config_revision + capabilities from row; no row -> 503 "handshake service unavailable";
  capabilities.rs reads active_values + active_bindings via open_sealed AAD. 3.4.3 amended.
- 3.5: row read first; revision differs -> Skipped; new 3.5.3 moved_config_revision_skips,
  pin item -> 3.5.4. D1 bullet replaced.
- 4.1 migration -> 470. 5.5 step 1 reads row (no row 503 "not ready"), paragraph + research
  bullet replaced, 5.5.3 amended. 6.2 config_values scope-reason -> 5.6 native.
- Remaining: P5 framing, 6.1 depends 5.13, 6.2 research bullet on P5 evidence, leaves 5.6-5.13,
  Granularity, E1.

## Epoch 10 research for leaves 5.6-5.9 (plan text NOT yet written for these)

5.6 active reads (values GET, ui-settings GET, tool-approvals/global GET):
- values GET = ConfigValuesService._snapshot_body(runtime.snapshot): {revision, desired, active
  (unflatten_config of _public_projection), secret_set {key:{desired,active}} over union of
  reference keys, pending_restart_keys (public, sorted), failed_live_keys (public,
  sorted, {revision, subscriber})}. _public_projection: registry.resolve; Unknown -> skip; not
  PUBLIC -> skip; secrecy REFERENCE -> is_set = str startswith "$secret:", value MASKED
  "********" if set else null; structured reference fields -> mask_structured_references; else
  to_jsonable. Ambiguous resolve raises RegistryError -> uncaught -> global handler 500
  {"status":"error","message":"Internal server error","error_logged":true}
  (servers/exception_handlers.py::global_exception_handler; pool outage -> 503
  {"status":"error","message":"Hub temporarily unavailable"}). Unflatten conflict also -> that 500.
  No snapshot -> 503 runtime_unavailable "Configuration runtime is not ready".
  Native: needs gcore policy with visibility (contract entries carry `visibility`; gcore
  ExactKeySpec/PatternSpec parse only key/machineExport/secrecy/fields today; 5.5 adds
  daemonKey/activation/type + export_policy). 5.6 adds visibility parse + public projection
  policy; reuse 5.5 unflatten + MaskItems. 2.1 GET compare ignores JSON key order (plan line ~594).
- ui-settings: configuration_ui_settings.py UI_SETTINGS_KEYS order (fontSize, model, theme,
  readingDirection, defaultChatMode, sttEnabled, ttsEnabled, voiceInputMode,
  planPendingVariant, selectedProjectId, selectedProvider); active_values["ui_settings.<k>"]
  non-null only. No snapshot -> 503 "Configuration runtime is still starting". Storage errors
  -> 500 {"detail":"Internal server error"}.
- tool-approvals/global: servers/tool_approvals.py get_global_approval_rules(snapshot):
  active_values["tool_approvals.global_rules"] None -> DEFAULT_GLOBAL_APPROVAL_RULES
  (tool:Read, tool:Glob, tool:Grep, tool:Ls); list -> sanitize_approval_rules(str(item))
  (strip, drop empty, dedupe, keep order); else defaults. Registry annotation list[str]
  (registry.py:743) so items always str. Body {"rules", "default_rules", "built_in_exemptions":
  ["mcp:gobby*:*", "tool:Bash (gcode only)"]}. Storage error -> 500 "Failed to load approval
  rules"; other -> 500 "Internal server error"; no snapshot -> 503 "still starting".
- Evidence pattern: shared vectors file generated from Python (like 3.4 capability_vectors):
  Python test serves each vector snapshot through the 3 routes (TestClient, like
  tests/servers/routes/test_config_values_api.py) and asserts bodies; Rust asserts port.

5.7/5.8 validation carrier decision (supersedes JSON-schema idea in DR "Config validation"):
- Use pydantic CORE schema, not JSON schema (JSON schema loses strictness, validator
  positions, defaults factories). pydantic 2.12.5 / pydantic_core 2.41.5.
  Serializer prototype: scratchpad/core_schema_size.py (collapse json-or-python -> json_schema,
  lax-or-strict -> strict_schema; drop metadata/serialization/cls_repr/...; function ->
  "module.Qual.name" (bound classmethod: owner qualname + name); cls -> qualname; enum members
  -> values; default/default_factory -> to_jsonable_python(value / factory())).
  Sizes: per-key 521 schemas (exact+pattern fields, non-restricted) 64.5 KB; DaemonConfig 146 KB.
- Node types after collapse: any, bool, dataclass(-args,-field; 1 dataclass), default,
  definition-ref, definitions, dict, enum, float, function-after, function-before, int, list,
  literal, model, model-field(s), nullable, str, union. Unions only 2 shapes:
  Literal|int (6) and str|FeatureCandidateConfig|dict (17). Constraints: int ge/gt/le,
  float ge/gt/le, str min_length(6)/pattern(1)/strict(1), dict strict False.
- Functions: 80 distinct in candidate carrier; per-key carrier uses 17 (all also in candidate).
  List: scratchpad/validator_functions.txt (K=per-key, C=candidate). Includes pydantic
  path_validator (Path: str -> str(Path) normalization).
- Default factories 77 (dict/list/model ctors/lambdas/default_model_metadata_aliases/
  default_validation_detection_config); generator calls them; freshness test guards env-dependence.
- Per-key _validate_value: TypeAdapter(annotation).validate_json(to_json(value), strict=True)
  then dump_python(mode="json"); pattern keys with field_specs use the field annotation
  ("Unsupported patterned configuration key" if no field). _require_json_domain first.
  Messages "Invalid value for configuration key {key!r}: {exc}" (Q-B: _validation_detail for
  ValidationError). PydanticSerializationError/ValueError keep str(exc).
- Candidate: ConfigRepository.runtime_candidate(overrides, bindings): skip specs without
  source_path; reference keys with "$secret:" value -> binding plaintext (missing binding ->
  ConfigRepositoryError, reference mismatch -> TornConfigSnapshotError; both RuntimeError ->
  route 500 persistence_indeterminate); _assign_path by _runtime_path (source_path split + pattern
  placeholders); DaemonConfig.model_validate (PYTHON LAX mode). Rust interpreter needs strict
  (per-key) and lax (candidate) modes.
- _validate_candidate (config_mutations.py:398) order: unset (resolve, authorize) -> values
  (resolve, authorize, _validate_value, reference: _validate_reference + resolve plaintext via
  secret_store.get if binding new/changed else "Secret reference for {key!r} cannot be
  resolved"; structured refs validate) -> secrets (authorize, must be REFERENCE else "is not a
  secret", plaintext str, name = update.name or config_key_to_secret_name, canonical check) ->
  runtime_candidate. ValidationError -> detail/error_key (first loc resolving to registry key;
  else sole changed key); reference error key -> "Secret configuration value is invalid"
  (raised after); else "Complete configuration candidate is invalid: {detail}". ValueError ->
  "Complete configuration candidate is invalid: {exc}" key = sole changed key.
  _authorize (mutations): restricted -> "Configuration key {key!r} is restricted"; managed ->
  "... requires managed activation" (unless allow_internal).
- Restricted keys: gcore contract excludes them but snapshot_from_rows/runtime_candidate resolve
  ALL stored keys -> validation member needs full spec list incl. restricted (separate
  `validation.specs`, so exactKeys/patterns and 5.5/5.6 assumptions unchanged).
- JSON data model for writes: config_store.value is TEXT written as
  json.dumps(to_jsonable_python(value)) (", " ": " separators, ensure_ascii, dict insertion
  order, float repr). gdaemon serde_json has NO preserve_order/arbitrary_precision (cargo tree:
  default, float_roundtrip, raw_value, std) -> enabling them would unify into gdaemon. Decision:
  gruntime ordered exact JSON tree built via RawValue recursion (objects as Vec<(String, Json)>,
  numbers as literal text), Python-json.dumps serializer reusing 5.5 Python float repr.
  Body parse divergence: NaN/Infinity literals and lone-surrogate escapes fail serde_json ->
  documented 422 divergence (precedent 3.4 step 1).

5.9 values PATCH (values.py patch -> _flatten_values -> patch_flat; config_mutations._apply):
- _flatten_values: walk nested; dynamic_segment_follows -> decode_dynamic_segment else 422
  "Dynamic configuration segment is not canonically encoded: {exc}"; resolve key; unknown +
  mapping -> recurse; unknown scalar -> "Unknown configuration key".
- patch_flat: per key _authorize (values.py:521: unknown/not public/managed with action
  "/api/embeddings/switch/start", code managed_activation_required); REFERENCE: MASKED skip;
  non-str "Secret configuration value must be a string"; "$secret:" prefix "Secret references
  are not accepted as plaintext values"; else SecretUpdate(category general). Structured refs:
  _prepared_structured_references (restore_masked_structured_references +
  validate_structured_references, voice_secrets.py). Unset keys authorized. _anchored_snapshot
  (active snapshot revision != expected -> 409 revision_conflict with expected/actual).
  reject_unprobed_responses_endpoints, reject_unverified_generation_endpoint_evidence (values.py
  112/164; shared with documents, document=True variant), clear_stale_generation_endpoint_probe_
  evidence (207; identity fields protocol/wire_api/api_base/model, api_key vs resolved desired
  plaintext). mutations.patch(source "user") errors: Conflict 409; Exhausted
  "revision_exhausted"/"Configuration revision cannot be advanced" 422; ConfigValidationError ->
  validation_error, path ("values", *resolved path), secret key -> "Secret configuration value is
  invalid". Other exceptions (EmbeddingConfigMutationBlocked, repository errors) -> 500
  persistence_indeterminate. Then reconcile wait -> _mutation_body or reconcile_failed body.
- _apply: _validate_revision, _validate_patch_shape ("Configuration patch operations overlap:
  [...]"), lock: pg_advisory_xact_lock(hashtext('gobby.storage.config_mutations.
  EmbeddingSwitchJournalMutation:EmbeddingSwitchJournalMutation()')) (5.2 already uses), SELECT
  revision FROM config_state WHERE id = true FOR UPDATE, CAS, rows SELECT key, value, is_secret,
  revision FROM config_store ORDER BY key, snapshot_from_rows (decode JSON, row revision check,
  reference bindings via secret_store.get), embedding admission (structural embedding keys;
  journal key ai.embeddings.switch_run), _validate_candidate, _effective_changes (unset in
  overrides; value != revalidated stored value; secret binding ref/plaintext differs), none ->
  return (revision, {}), MAX_CONFIG_REVISION (2^53-1) -> exhausted, _write_changes (DELETE unset;
  _upsert values: INSERT ... ON CONFLICT (key) with json.dumps text, source, is_secret
  (registry_is_secret), revision, updated_at; secrets: secret_store.set(name, plaintext,
  category, description or f"Config secret for {key}") + upsert reference; release old
  bindings -> _delete_released_secrets via find_persisted_secret_references), UPDATE config_state
  SET revision, pg_notify('gobby_config_changed', rev).
- SecretStore.set: category in VALID_CATEGORIES; normalize_and_validate_secret_name; Fernet
  encrypt with DEK; INSERT INTO secrets (id uuid4, name, encrypted_value, category, description,
  project_id GLOBAL) ON CONFLICT (name, project_id) DO UPDATE ... RETURNING. 5.2 already has
  delete + reference scan; 3.3 has DEK + Fernet decrypt; fernet 0.2.2 locked (encrypt).

## Epoch 10 TODO (ordered; plan validates clean at this point)
1. Write leaf 5.6 (depends 5.5). Then research+write 5.7 (carrier + interpreter + 17 per-key
   validators + Q-B site 1 + ordered JSON), 5.8 (63 remaining validators + candidate assembly +
   error mapping), 5.9 (PATCH: write core, wait, check_proxied), 5.10 secrets POST
   (ConfigStore.set_named_secret; not yet re-read), 5.11 YAML docs (ConfigDocumentsService
   _prepare/replace/import; not yet read), 5.12 activation PUT + probe client
   (configuration_generation_endpoints.py, ai/endpoint_activation.py), 5.13 preview
   (configuration_validation_detection.py, validation_detection.py 862 lines -> split rule).
2. Amend DR "Config validation": core schema carrier in `validation` member incl. restricted
   specs; strict/lax modes; ordered exact JSON; NaN/surrogate 422 divergence.
3. Remove from 2.2's consumer inventory any file a 5.x leaf targets.
4. P5 framing (18 routes; 5.3 note), 6.1 depends 5.13, 6.2 research bullet on P5 evidence.
5. Granularity (33 deliverables, targets.py) incl. 2.2; E1 updates.
6. Validate clean, commit, send SHA+sha256 to #15977 and #16491 with 422 note; arm wait.

## Epoch 11 (2026-10-14, post-compaction)

- 5.6 WRITTEN (active_reads.rs; gcore public_policy; vectors; 6.1 now depends 5.6). Validate clean.
- Advisor nits for 5.6 to apply: name the four state value fields; failed_live "sorted".
- 5.7 carrier decisions (verified by probes in scratchpad):
  - Carrier = SIBLING generated file crates/gruntime/assets/runtime_config_validation.json written by
    scripts/generate_runtime_config_contract.py WITHOUT sort_keys (contract renders sort_keys=True,
    which would reorder model fields and dict defaults; and gcore embeds the contract in all 3 bins).
    gruntime include_str!s it -> 5.7 is gdaemon-only bump. DR "Config validation" must be amended.
  - refs carry id() suffix (`Cls:5228...`) -> generator strips to qualname; asserts uniqueness.
  - HOME/GOBBY_HOME change -> carrier identical after ref normalization (no env-dependent defaults).
  - extra behavior: 30 models forbid, 11 ignore, 45 default(ignore).
  - Union labels = SchemaValidator(choice).title: "literal['auto']","int" / "str",
    "FeatureCandidateConfig","dict[str,any]". Strict JSON: object item of str|FCC|dict -> dict wins
    (stored unchanged incl. extra keys). JSON-mode messages differ from python mode
    (list_type "valid array" vs "valid list"; model_type "Input should be an object").
  - Candidate runs on JSON-collapsed tree in lax mode: reachable candidate errors are constraint,
    missing, dict-key enum/literal ("[key]" loc), value_error; mode-dependent type errors unreachable
    because per-key validation precedes runtime_candidate in _validate_candidate.
  - Restricted specs: only 3 exact keys (ai.embeddings.switch_run, ai.embeddings.completed_switch,
    mcp.tool_embeddings.scoped_payload_version); patterns all public (11).
  - Keys in messages are canonical ASCII (SAFE_SEGMENT_BYTES) so {key!r} == 'key'.
  - URL validators share src/gobby/config/url_validation.py::validate_endpoint_url (urlsplit port).
  - Q-B site 1: tests/config/test_daemon_sandbox.py:67 and tests/storage/test_revisioned_config_store.py:829
    still match after change (msg text / "Invalid value" prefix kept).
  - candidate order: dict(snapshot.overrides) (rows ORDER BY key, DB collation) then unset pops,
    values/secrets assigned in patch order.
- 5.7 WRITTEN (per-key validation: sibling carrier crates/gruntime/assets/runtime_config_validation.json,
  json.rs ordered tree, interpreter strict JSON, messages.rs ERROR_TYPES, validate_value, Q-B site 1,
  17 ports in ports/{mod,urls,ai,validation_detection}.rs, corpus config_validation_corpus.json
  {json, errorTypes, perKey}). 6.1 depends 5.7. DR "Config validation" AMENDED (sibling carrier,
  80 functions/17 per-key, ordered JSON, NaN divergence, rejected JSON Schema + serde features).
  5.6 advisor nits applied. Validate clean.
- Probed (strict JSON): int<-5.0 int_type; int<-2**70 OK digits; float<-1 -> 1.0; float<-2**70 ->
  1.1805916207174113e+21 (json float repr = float.__repr__, NOT YAML variant); min_length 1
  "at least 1 character", 2 "characters"; ge 0.5 renders "0.5"; Path "a//b/./c/"->"a/b/c", ""->".",
  "../x/.." kept; dict order kept.
- 5.8 PLAN (next): candidate validation. Reuse interpreter in STRICT JSON mode (equivalence argument:
  every candidate input passed per-key strict validation of same bare type, or is an unvalidated
  carrier default, or a str plaintext secret -> lax vs strict outcomes identical; reachable errors
  mode-independent: constraints, missing, dict-key enum/literal "[key]", value_error). Assemble per
  runtime_candidate: skip specs w/o sourcePath; reference "$secret:" -> binding plaintext (missing ->
  ConfigRepositoryError, mismatch -> TornConfigSnapshotError: both 500 persistence_indeterminate);
  path = sourcePath split + decoded placeholders; _assign_path overwrites non-dict. Order:
  dict(snapshot.overrides) (rows ORDER BY key) then unset pop, values/secrets in patch order.
  DaemonConfig validate_by_alias+name (gobby-tasks). Ports: 63 remaining functions grouped by Python
  module (persistence, servers, sessions, features, tasks, cron, code_index (croniter port + jiff tz),
  logging, ui, voice, telemetry, app/DaemonConfig, ai Generation*, feature_base, hooks, comms,
  system_loops, skills, bin_freshness, database_concurrency). Error mapping (_validate_candidate):
  detail; error key = first loc (joined ".") resolving in specs, else sole changed key; reference key ->
  "Secret configuration value is invalid" raised after; else "Complete configuration candidate is
  invalid: {detail}"; ValueError -> same prefix + str(exc), key sole changed. _authorize, reference
  validation, structured refs (validate_structured_references) belong where? -> 5.8 ports whole
  _validate_candidate incl. authorize/reference/secrets loops (needs secret_store.get for new refs
  via 3.3). Separate fixture candidate_validation_corpus.json; Python test test_candidate_corpus.

## Epoch 12 (2026-10-14, post-compaction) 5.8 research DONE; nothing new written into plan yet

Scratchpad probes: ser_probe.py, fn_survey.py(.txt), fn_all.py(.txt = full source of 63 candidate fns),
fn_repr.py(.txt), fn_fields.py(.txt), nested_probe.py, tz_probe.py, cron_probe.py.

Facts:
- Custom serializers: only serialize_sequence_via_list (candidates Sequence -> list) and ser_path (Path ->
  str); both already covered by 5.7 dump rules. No computed_fields. OK.
- Production config_store writers: only ConfigMutations._upsert (+ reconcile_registry is_secret flag,
  migration 434 key rename). snapshot_from_rows does NOT revalidate stored values.
- runtime_candidate result is DISCARDED by _validate_candidate (called only for its exceptions).
- SecretStore.get normalizes name with normalize_secret_name (strip().lower(), no ValueError); stored
  names are ASCII (normalize_and_validate_secret_name grammar) so Rust to_lowercase finds same rows.
  SecretDecryptionError is RuntimeError -> 500. ValueError branch of _validate_candidate is effectively
  only pydantic ValidationError (ValueError subclass) — keep port of the branch anyway.
- _validation_error_key: resolve(loc joined "."), catches only UnknownConfigKeyError (Ambiguous ->
  RegistryError escapes -> 500). int loc segments str() ("0").
- Lax Python vs strict JSON gave identical errors on probes (removed local:, dict repr, set repr,
  casefold dup, workflow timeout 0). Corpus design: candidate cases are raw candidate TREES; Python
  test asserts DaemonConfig.model_validate(tree) == model_validate_json(json.dumps(tree), strict=True)
  outcome (proves lax==strict per case), plus `_validate_candidate` mutation cases (snapshot overrides
  + patch + secrets -> ConfigValidationError message/key or ok). Outcome only (no dump hash).
- ValidationError raised INSIDE a validator is re-located by pydantic-core: inner errors with outer
  loc prefix (e.g. ('chat','candidate') "Value error, 'local:x/y' uses the removed local: selector;
  replace it with endpoint:*"). Port error type needs Nested(Vec<LineError>).
- validate_feature_candidates: FCC.model_validate (LAX python) per item; message
  "feature candidates must use provider/model format: {', '.join(repr(c))}" -> needs Python repr of
  str and dict (e.g. {'candidate': 'a/b', 'zzz': 1}); FCC instance never invalid.
- populate_and_validate_candidates `_candidates_omitted` (only private attr / model_fields_set use) and
  DaemonConfig.apply_generation_profile_defaults: effect is rewriting candidates, which no later
  validator reads, and the candidate value is discarded; apply's validate_feature_candidates call
  re-validates already-accepted lists so cannot raise. DECISION: no native counterpart for that
  bookkeeping (state why); port the raising calls.
- Mutations that ARE read later: WorkflowConfig.validate_timeout (0 -> DEFAULT_WORKFLOW_TIMEOUT_SECONDS)
  read by DaemonConfig.validate_hook_timeout_order. CronConfig.validate_check_interval (<60 -> 60),
  clamp_candidate_timeouts: not read later (port anyway, faithful field validators are trivial).
- FOUND BUG (fix in Python, 5.8 target): ChatConfig.validate_default_mode message renders a Python SET
  `{valid}` -> order varies per process (str hash randomization); reachable (chat.default_mode is str).
  Fix: render sorted list like MemoryConfig.validate_backend. No test asserts the text.
- FOUND dead validator: SkillsConfig.validate_injection_format (field Literal['summary','full','none']
  rejects first; also set-repr message). Delete it in Python (5.8 target) -> functions 79.
- Python str semantics needed (ALSO BY 5.7 K ports — 5.7 GAP): casefold (ModelMetadataAlias.
  normalize_source_key stored value; HookTimeoutConfig.normalize_additional_context_limits keys+msg),
  lower (_normalize_reasoning_effort stored value; _normalize_audio_provider_id), strip/isspace,
  repr(str) ({candidate!r} in _parse_feature_candidate_label; validate_endpoint_names; voice provider
  ids), repr(dict/list/int/float/bool/None). PLAN: new crates/gruntime/src/pystr.rs created in 5.6
  (isspace strip, used by sanitize_approval_rules) and extended in 5.7 (lower via str::to_lowercase,
  casefold via NEW dep icu_casemap fold_string [no casefold crate locked; unicase has no fold API],
  repr with printability from icu_properties GeneralCategory (locked 2.1.2): non-printable = Cc Cf Cs
  Co Cn Zl Zp Zs except ' '). Documented divergence: code points whose case/category data changed
  after Unicode 15.1 (Python 3.13 = 15.1, 3.14.3 = 16.0; CI runs both; rustc 1.97) — corpus avoids them.
- Python 3.14.3 local, unicodedata 16.0.0. CI matrix 3.13 + 3.14.
- Timezone (CodeIndexConfig.validate_nightly_repair_timezone): tzdata pip is win32-only in uv.lock ->
  on mac/linux ZoneInfo uses system TZPATH only (default /usr/share/zoneinfo:/usr/lib/zoneinfo:
  /usr/share/lib/zoneinfo:/etc/zoneinfo; PYTHONTZPATH overrides, non-absolute entries dropped).
  Outcomes: path checks raise ValueError (NOT caught by validator -> value_error with exact text):
  "ZoneInfo keys may not be absolute paths, got: {k}", "ZoneInfo keys must be normalized relative
  paths, got: {k}" (normpath length change; "" -> this), "ZoneInfo keys must refer to subdirectories
  of TZPATH, got: {k}" (normpath("_/"+k) not startswith "_/"; "../x", "."); then first dir with
  isfile(dir/key) (FS case rules: macOS accepts "utc"); file not starting b"TZif" -> ValueError
  "Invalid TZif file: magic not found" (zone.tab, +VERSION); not found -> validator message
  "nightly_repair_timezone must be a valid IANA timezone"; NUL -> not found. OSError -> 500.
  DECISION: port zoneinfo lookup over same TZPATH files (no jiff). Amend DR (drop "through jiff").
- croniter 6.0.0: is_valid = expand() raising CroniterError -> False. expand 60 + _expand 244 lines
  (module 1015). Probes: "0 2 * * *" T, "@daily" T, "@reboot" F, "H 2 * * *" F (no hash_id),
  6-field T, 7-field (year) T, "61 2 * * *" F, "0 2 L * *" T, "0 2 * * 1#2" T, "0 2 15W * *" F,
  "?" T, names "mon-fri" T, "*/0" F, dow 8 F, dow 7 T, "0 2 31 2 *" T, padded/tab T, 4 fields F,
  "" F. Message on False: "nightly_repair_cron must be a valid cron expression". Hand-port
  croniter.expand validity (ports/cron.rs) with corpus covering every branch.
- Other K-side helper facts: url validators use strip() + urlsplit + scheme.lower().

5.8 TARGET SKETCH: validation/candidate.rs (assembly + _validate_candidate port + error mapping),
validation/secret_names.rs? (config_key_to_secret_name: _CONFIG_SECRET_NAMES falkordb, embedding
ai.embeddings.api_key name, sanitize [^a-z0-9_]+ -> _, strip _, sha256[:8], 200 limit),
ports/{persistence,servers,sessions,features,tasks,cron(croniter),code_index(tz),logging,ui,voice,
telemetry,app,generation,feature_base,hooks,comms,system_loops,skills,bin_freshness,
database_concurrency}.rs (group sensibly, e.g. ports/{numeric,app,ai,features,cron_expr,zoneinfo,
voice}.rs), tests/candidate.rs, fixtures/candidate_validation_corpus.json,
tests/config/test_candidate_validation_corpus.py, src/gobby/config/features.py::ChatConfig.
validate_default_mode, src/gobby/config/skills.py::SkillsConfig.validate_injection_format (delete),
carrier regenerated (functions 79) -> assets/runtime_config_validation.json + generator test.
Also amend 5.6 (pystr.rs) and 5.7 (pystr lower/casefold/repr, icu_casemap + icu_properties deps).

## Epoch 13 (2026-10-14, post-compaction) decisions; DAEMON BACK 11:38 CDT (hold lifted)

- DONE in plan: 5.7 port check scoped to walked schemas (5.7.5 amended); 5.6 pystr.rs target + strip text.
- FCC fields: candidate str, reasoning_effort str|None (before-validator), extra forbid, frozen -> lax==strict
  on JSON. FeatureDefaultConfig.profile is enum FeatureProfile -> FeatureProfile(profile) never raises.
  Probe (fcc_probe.txt): every case lax==strict.
- RE-RAISE BUG: validate_feature_candidates re-raises when "removed local: selector" in str(exc); str(ValidationError)
  includes input_value reprs (truncated): {"candidate":"a/b","x":"removed local: selector"} re-raises extra_forbidden
  at ('chat','x'); a long value hides it (truncation). FIX (5.8): catch ValidationError, re-raise iff any
  error["msg"] contains the text. Corpus: both input cases -> invalid list; propagating cases: local: label,
  no-slash label containing text, local: + bad reasoning_effort (two re-located errors). Re-located loc =
  outer loc + inner field (no list index). normalize_feature_candidate never raises; dedupe not ported.
- Rust std char::UNICODE_VERSION = (17,0,0) (rustc 1.97). icu_casemap 2.1.1 deps: icu_collections ~2.1.1,
  icu_locale_core 2.1.1, icu_properties ~2.1.1, icu_provider 2.1.1, writeable 0.6, zerovec 0.11.3 (all locked),
  NEW icu_casemap_data. icu_properties 2.1.2 compiled_data already on in gdaemon (via idna_adapter). Rejected unicase.
- WINDOWS: gdaemon ships windows (release-gdaemon.yml); Python daemon supports windows (service_windows.py).
  -> TZ DECISION CHANGED: pyproject adds tzdata unconditionally (locked 2026.1, marker was win32);
  CodeIndexConfig.validate_nightly_repair_timezone = membership in tzdata `zones` resource (598 names incl links;
  no posixrules/localtime); generator writes `timezones` + `tzdata` version into validation carrier; native =
  membership. Runtime resolve_local_timezone/ZoneInfo unchanged and loads every listed key on every host.
  Rejected: ZoneInfo lookup port (windows reads tzdata from Python env; FS case rules), jiff.
  Tests touching: tests/config/test_app_config.py:86 ("Not/AZone" still invalid).
- PATH DECISION (5.7): only Path key = memory_backup.backup_path; Windows stores backslashes (WindowsPath).
  Change annotation to str (default ".gobby/memories.jsonl"); MemoryBackupManager.__init__ wraps Path();
  cli/memory/export.py backup_memories/restore_memories pass str(); tests: test_persistence.py (341,360),
  test_app_config.py:1359, test_memory_cli.py:1024/1056, sync/test_memory_backup.py, sync/test_memory_sync.py.
  Contract type already "string". Removes pydantic path_validator -> counts: plan-time 80 (17 K);
  after 5.7 79 (16 K); after 5.8 (delete injection_format) 78; 5.8 ports 62.
- 5.7 AMENDED + validate clean (exit 0, no warnings): pystr lower/casefold/repr(str), icu deps, Unicode note,
  backup_path -> str (targets persistence.py::MemoryBackupConfig, sync/memories.py::MemoryBackupManager.__init__,
  cli/memory/export.py backup_memories/restore_memories, 5 test files ::*), contract target ::* (bytes unchanged),
  consumers unchanged app.py/runner.py/tests/sync/test_local_backup_paths.py, 5.7.8, 5.7.9. path_validator removed.
- RULE: any .py under src/gobby/config/ requires target crates/gcore/assets/config/runtime_config_contract.json::*
  (derived-carriers). 5.8 edits features.py, skills.py, feature_base.py, code_index.py -> add it (bytes unchanged).
- _validate_candidate exact (vc_src.txt): unset(resolve,authorize,pop both) -> values(resolve,authorize,
  _validate_value, REFERENCE: _validate_reference + secret_store.get(name w/o prefix) when binding new/changed,
  None -> "Secret reference for {key!r} cannot be resolved"; structured refs validate_structured_references ->
  ConfigValidationError(str(exc)); values[key]=candidate[key]=validated) -> secrets(resolve, authorize, not REFERENCE
  "Configuration key {key!r} is not a secret", non-str "Secret value for {key!r} must be a string", name=update.name
  or config_key_to_secret_name, _validate_reference(canonical=name None)) -> runtime_candidate. _resolve unknown ->
  ConfigValidationError "Unknown configuration key: {key}"; ambiguous -> RegistryError(ValueError) "Ambiguous
  configuration key: {key}" -> ValueError branch "Complete configuration candidate is invalid: {exc}" key=sole.
  In handler, _validation_error_key/_is_reference_key ambiguous -> escapes (internal). RuntimeErrors
  (ConfigRepositoryError missing binding, Torn, UnknownStoredConfigKeyError, secret store) -> internal.
  _validate_reference msgs: "Secret configuration key {key!r} requires a secret update or reference",
  "Secret reference for {key!r} is empty", "... is not canonical". validate_structured_references: non-list
  "{key} must be a list"; dict items field not in (None,"") and not fullmatch SECRET_REF_PATTERN ->
  "{key}[{i}].{field} must use a $secret:NAME reference; plaintext values cannot be persisted".
  config_key_to_secret_name: _CONFIG_SECRET_NAMES {"databases.falkordb.password":"falkordb_password"},
  ai.embeddings.api_key -> "embeddings_api_key", else "config_" + sub([^a-z0-9_]+,"_",key.lower()).strip("_")[:200-7-9]
  + "_" + sha256(key)[:8].
- Identity ports (no raise, effect unread): GenerationConfig.clamp_candidate_timeouts, DaemonConfig.apply_generation_
  profile_defaults, CronConfig.validate_check_interval (<60->60). FalkorConfig.validate_password calls
  validate_falkordb_password (raises). WorkflowConfig.validate_timeout 0 -> 24.0 read by validate_hook_timeout_order
  (constants BLOCKING 20.0, HOOK_TRANSPORT 30.0 rendered :g). _normalize_audio_provider_id strip().lower(),
  empty -> "openai_compatible_audio provider must not be empty"; reserved {"whisper"}.
- 5.8 file grouping (62): ports/persistence.rs (17), ai.rs +5 (AIConfig, Generation x4) +feature_base (1),
  app.rs (5 app), features.rs (4 features + voice + communications), schedules.rs (cron 4, code_index 2,
  system_loops 1, bin_freshness 2), services.rs (servers 6, sessions 3, tasks 3, logging 2, ui 2, telemetry 1,
  database_concurrency 1, hooks 1), cron.rs (croniter expand port).
- 5.8 WRITTEN (validate exit 0, no warnings): candidate.rs, ports/{persistence,app,features,schedules,services,cron}.rs
  + ai.rs, pystr JSON repr, schema.rs candidate walk, interpret.rs relocation, Python fixes (default_mode sorted,
  injection_format deleted, re-raise on msg, tzdata membership + pyproject/uv.lock), corpus {trees, mutations,
  secretNames}, 9 acceptance items 5.8.1-5.8.9, consumers ai.py/app.py. 6.1 depends 5.8. DR Config validation
  amended (78 fns/16 per-key; tzdata; Unicode divergence). Daemon restart for #23562 at ~12:06 CDT (await DAEMON BACK
  before commit/send).
- NEXT: 5.9 values PATCH (Epoch 10 notes 5.9 section + DR Write wait), 5.10 secrets POST, 5.11 YAML 1.2 docs,
  5.12 activation PUT + probe client, 5.13 preview + classifier (validation_detection.py 862 lines -> named split);
  then 2.2 consumer inventory cleanup, P5 framing (18 routes), 6.1 depends 5.13, 6.2 research bullet, Granularity
  (33 deliverables; leaves >6 items: 5.7 has 9, 5.8 has 9), E1. Commit after DAEMON BACK; send SHA+sha256 to #15977
  and #16491 noting: invalid YAML stays 422 (not 400); 5.8 widens tzdata marker to all platforms; 5.7 types
  backup_path as str.

## Epoch 14 (2026-10-14, post-compaction) 5.9 WRITTEN; validate NOT yet run (daemon down for #23562 restart)

- Restart hold extended (#15977): #23562 restart ~12:35 CDT; validate needs daemon task API (503 while down).
- 5.9 "Native PATCH /api/config/values" written before P6; 6.1 now depends 5.9. Modules: config_write.rs (commit core
  = _apply port + write wait; 5.10-5.12 reuse), write_rules.rs (prepare_structured masked restore, reject_unprobed_responses,
  reject_unverified_evidence, clear_stale_evidence), values_patch.rs (route, flatten, Python-text decode_dynamic_segment port
  over code points, error mapping, check_proxied + dry run). json.rs adds py_eq; secrets.rs adds set (Fernet encrypt);
  secret_routes.rs helpers pub(crate); candidate.rs adds desired_config(row) (returns validated tree); validation/mod.rs
  reads new carrier spec members `default` (from _json_defaults itself) + `identityField`.
- Python changes in 5.9: route parses body itself (json.loads + PatchConfigRequest.model_validate -> 422 {"detail":
  "invalid config patch request"}; PatchConfigRequest kept); patch_flat authorizes unset in sorted order;
  test_revision_domain_and_exhaustion_contract updated. 5.8 AMENDED: _validate_candidate iterates sorted(patch.unset)
  (target config_mutations.py::ConfigMutations._validate_candidate added).
- Facts: route except Exception -> 500 persistence_indeterminate (RegistryError too). Embedding structural keys are the
  only managed keys -> PATCH never reaches admission (EmbeddingConfigMutationBlocked unreachable; no fix). SecretStore
  set/delete/scan join ambient txn (hub/_ambient.py). _apply txn READ COMMITTED. launch_defaults.{project_id} only
  dict[str, object] key. Structured key: voice.openai_compatible_audio (api_key, identity provider).
- 5.9 acceptance 5.9.1-5.9.9 (nine -> Granularity note). Consumers unchanged added: pins block, mcp_proxy/tools/voice.py,
  configuration_generation_endpoints.py, servers/routes/configuration.py (validator may want more: tests/ai/
  test_endpoint_activation.py, tests/mcp_proxy/tools/test_voice.py, tests/config/test_live_policy_consumers.py).
- NEXT: when daemon back, bare validate; fix errors/warnings (likely: 2.2 inventory line for
  tests/servers/routes/test_config_values_api.py and configuration_values.py/values.py -> remove; consumer coverage).
  Then 5.10 secrets POST (ConfigStore.set_named_secret), 5.11 YAML 1.2 docs, 5.12 activation + probe client, 5.13 preview +
  classifier (validation_detection.py 862 lines -> named split); 6.1 depends 5.13; P5 framing 18 routes; 6.2 research
  bullet; Granularity (33 deliverables; 5.7/5.8/5.9 nine items each); E1. Commit after DAEMON BACK; send SHA+sha256 to
  #15977 and #16491 noting: invalid YAML stays 422; tzdata all platforms; backup_path str; PATCH body 422 detail change.

## Epoch 15 (2026-10-14, post-compaction) validate clean after 5.9 consumer fix; 5.10 research

- #23562 restart ran after handoff consumed; daemon healthy (/api/health 200); DAEMON BACK not yet received.
- Validate: one warning fixed (5.9 consumers unchanged + tests/ai/test_endpoint_activation.py, tests/mcp_proxy/tools/test_voice.py). Clean.
- SWEEP (body decoding): json.loads(bytes) auto-detects UTF-8 BOM (utf-8-sig), UTF-16/32, and decodes with
  surrogatepass. serde rejects all. Fix: self-parsed Python write routes decode strict UTF-8 first. Edited 5.4 save
  contract, 5.9 step 1, DR divergence bullet. 5.10+ say the same.
- 5.10 facts: route save_secret (configuration_secrets.py:124) body SaveSecretRequest {name str, value str, category str
  ="general", description str|None} extra ignore; ValueError -> 400 str(exc); other -> 500 {"detail":"Internal server
  error"}; require_mutation_auth 401 (unclaimed natively). ConfigStore.set_named_secret (config_store.py:378): normalize
  (strip+lower); txn = embedding_mutation_context (advisory lock); if name == embeddings_api_key -> _assert_embedding_
  mutation_allowed -> _active_switch_run_id (5.2 port) Blocked -> 500; read_revision lock; read_rows; snapshot_from_rows
  (UnknownStored/ConfigRepositoryError RuntimeError -> 500; ambiguous RegistryError(ValueError) -> 400 "Ambiguous
  configuration key: <key>"); referencing = overrides (ORDER BY key) whose find_secret_references (decoded walk, values
  not keys) contains name AND secrecy REFERENCE -> _apply_internal(ConfigPatch(secrets={k: SecretUpdate(plaintext,
  name, category, description)}), source user, expected snapshot.revision) [structural check unreachable] ->
  patch_internal -> _apply allow_internal; then info from secret_store.list global by name (None -> RuntimeError 500).
  Else canonical_key = first exact key spec REFERENCE with config_key_to_secret_name == name -> validate_named_secret
  (_validate_candidate secrets={key: SecretUpdate(plaintext)}, allow_internal). Then secret_store.set(name, plaintext,
  category, description, project None) -> RETURNING info. Response {"ok": true, "secret": info.to_dict()}. No reconcile wait.
- SecretStore.set: category check FIRST: ValueError f"Invalid category '{category}'. Must be one of: {VALID_CATEGORIES}"
  -> SET REPR, hash-order nondeterministic -> FOUND BUG, fix sorted(VALID_CATEGORIES). Tests matching "Invalid category"
  substring only (test_configuration_routes.py:520, test_secrets_store.py:407). Then normalize_and_validate_secret_name:
  "Invalid secret name: must start with a letter or underscore and contain only letters, digits, and underscores".
- Referencing no-change (same plaintext): no write, returns existing info (category/description unchanged). Description
  None + changed -> "Config secret for <key>" (last sorted referencing key wins). Mixed-case stored reference "$secret:Foo"
  -> rewritten "$secret:foo", old released but retained.
- 5.10 design: secret_routes.rs POST + check_saved; config_write.rs gains locked_snapshot(tx) (Ambiguous variant) +
  commit_in(tx,...); secrets.rs set returns Invalid(category msg | name msg); verify.rs; lib.rs; Python route self-parse
  (strict UTF-8, 422 {"detail": "invalid secret request"}); SecretStore.set sorted message; corpus secret_save_corpus.json;
  tests/servers/routes/test_secret_save_corpus.py; crates/gruntime/tests/secret_save.rs.
- DAEMON BACK 12:49 CDT (#23562 active at 8842c2e8e5). Hold released.
- 5.10 WRITTEN (validate clean): secrets POST; 42 SecretStore.set consumer lines (scratchpad/add_consumers.py generates
  lines from validate output). 6.1 depends 5.10. 5.9 amended: locked_snapshot(tx)/commit_in seam; Ambiguous{key}
  variant (PATCH maps with Internal); parse before route try. 5.4 amended: strict UTF-8 + parse before try.
- NEXT: 5.11 YAML docs (documents.py ConfigDocumentsService _prepare/replace_yaml/import), 5.12, 5.13.
- 5.11 RESEARCH (in progress; plan text NOT written). documents.py (474 lines) dumped to scratchpad/docs_src.txt:
  replace_yaml: _validate_revision (type int, 0..MAX else 422 validation_error "Expected revision must be an exact
  integer from 0 to 9007199254740991" path ["expected_revision"]); _runtime_snapshot (503 runtime_unavailable "not ready");
  _prepare (run_blocking); masked restore && snapshot.revision != expected -> 409 revision_conflict (expected/actual);
  mutations.replace_namespace(namespace="daemon", expected, patch) -> _apply with _scoped_patch (omitted namespace keys
  not MANAGED -> unset; outside -> "Keys are outside configuration namespace 'daemon': [..]"); errors: Conflict 409,
  Exhausted 422 revision_exhausted, ConfigValidationError 422 validation_error path key.split(".") (NO "content"
  prefix), EmbeddingConfigMutationBlocked 409 embedding_mutation_blocked path ["content"] retryable (unreachable:
  managed keys rejected in _prepare and excluded from omitted). Then _reconcile: failed/pending filtered to daemon
  keys (source_path not None), not public projection; reconcile exception -> reconcile_failed body.
  _prepare: yaml.safe_load -> "Invalid YAML: {exc}" ["content"] (DR: becomes "Invalid YAML at line L, column C");
  None -> {}; non-dict "YAML document must be a mapping"; _validate_mapping_keys DFS non-str key ->
  "Configuration keys must be strings" at ["content", *str(key).split(".")]; flatten_config(parsed, registry)
  ValueError -> ["content"] str(exc); per flat key: unknown "Unknown configuration key {key!r}"; source_path None or
  RESTRICTED "Configuration key {key!r} is not daemon-owned"; MANAGED "... requires managed activation" (all at
  ["content", *key.split(".")]); REFERENCE -> _prepare_secret (None/"" -> unset; non-str "Secret configuration value
  must be a string"; MASKED: desired ref None/"" -> unset; non-str "Masked secret has no persisted value"; not
  $secret: -> SecretUpdate(ref plaintext) restored=True; $secret: -> plaintext snapshot.desired_secret None ->
  "Persisted secret cannot be resolved"; value $secret:NAME -> resolve_secret(name) None or empty -> "Secret reference
  cannot be resolved"; plaintext -> SecretUpdate general); structured -> masked flag + restore_masked +
  validate_structured_references (ValueError -> _invalid_key). changed_values vs snapshot.desired_values (Python !=);
  omitted = desired_values keys - wire keys; reject_unverified(changed, omitted, paths, desired_values, document=True);
  reset = omitted | None-valued; clear_stale(values, desired, previous, unset=reset, secrets, probe_verified False) ->
  cleared keys None in validation/wire; reject_unprobed(changed, omitted, paths(changed+omitted), desired, document=True);
  runtime_candidate(validation_values) (ConfigRepositoryError, ValueError, TypeError) -> "Complete configuration
  candidate is invalid: {exc}" ["content"]. Return ConfigPatch(values, secrets) (cleared keys NOT in values!).
- yaml_serde 0.10.4 + libyaml-rs 0.3.0 sources under ~/.cargo/registry/src/index.crates.io-1949cf8c6b5b557f/.
  TODO read: scalar resolution (null/bool/int/float rules), explicit tags, merge `<<` (likely plain key -> Python
  subclass without merge resolver gives str "<<" -> flatten unknown key), aliases + repetition/recursion limits vs
  PyYAML (RecursionError -> 500?), error mark line/column (1-based?), duplicate keys (serde_yaml errors "duplicate
  entry"; PyYAML last wins) -> must specify. Also route file (template PUT + import POST) not yet read:
  servers/routes for "config/template" and "config/import"; flatten_config (config_store.py:520).

## Epoch 16 (2026-10-14, post-compaction) 5.11 research DONE; plan text not yet written

- Routes: PUT /api/config/template (configuration_templates.py::register_template_routes) and POST /api/config/import
  (configuration_import_export.py::register_import_export_routes); both call replace_yaml with ConfigDocumentRequest
  {expected_revision strict int 0..2^53-1, content str, extra forbid}; ConfigValuesError -> public_body; other ->
  500 persistence_indeterminate "Configuration persistence outcome is indeterminate". Test asserting FastAPI loc:
  tests/servers/routes/test_config_yaml_replace.py::test_yaml_revision_domain_and_exhaustion (must change).
- flatten_config (config_store.py:520): dynamic-segment check when registry.dynamic_segment_follows(prefix parts) ->
  ValueError "Dynamic configuration segment {key!r} under {repr(prefix)|<root>} is not canonically encoded: {exc}";
  dict value: registered leaf (resolve ok) -> leaf; non-empty -> recurse; empty -> leaf {}; ambiguous resolve in
  _is_registered_leaf -> RegistryError(ValueError) -> _prepare maps to 422 ["content"]; ambiguous in per-key loop -> 500.
- documents mode texts (values.py): reject_unprobed: msg "Responses endpoint '<seg>' must be probed and activated before
  YAML import", action "Probe and activate endpoint '<seg>' before importing YAML", path ["content", *path];
  reject_unverified: same msg as values mode, action "Re-import the exported evidence unchanged or activate endpoint
  '<seg>'". Documents order: reject_unverified -> clear_stale -> reject_unprobed (5.9 order differs).
- runtime_candidate(validation_values, {}): ConfigRepositoryError texts "Missing resolved secret binding for
  configuration key '<k>'" / "Secret configuration key '<k>' has an empty reference" (plaintext starting $secret:);
  ValidationError str(exc) has URLs -> site 4 of Validation messages (switch to _validation_detail). No validator
  DaemonConfig calls raises TypeError (only _loading.py:376).
- replace_namespace -> _apply(namespace="daemon"): _scoped_patch after locked snapshot: outside keys (no source_path)
  "Keys are outside configuration namespace 'daemon': [..]" (unreachable from documents); omitted = stored overrides
  that are daemon keys, not requested, not MANAGED -> added to unset. Restricted keys have no source_path.
- _reconcile filters pending/failed to daemon keys (source_path); ambiguous -> 500.
- yaml_serde 0.10.4: deserialize_any: tag starting "!" (local, "!" non-specific) -> visit_enum (our visitor errors at
  node mark); core tags bool/int/float/null strict parse (parse_bool True/true/TRUE..., parse_null null/Null/NULL/~ no
  empty) else invalid_value at mark; other tags -> string (any style); seq/map other tags ignored. Untagged plain ->
  visit_untagged_scalar: ""/null -> null; bool; visit_int u64,i64,u128,i128 (0x/0o/0b, one '+', digits_but_not_number);
  float via parse_f64 if !digits_but_not_number (.inf/.nan forms; Rust f64 grammar; finite only) else string.
  Depth 128 (RecursionLimitExceeded at collection mark); jumpcount > events*100 -> RepetitionLimitExceeded (no mark);
  UnknownAnchor at alias mark; MoreThanOneDocument (no mark; anything after doc 1 except stream end); parse error
  stored, raised when walk peeks past events; Location = mark+1; reader errors leave problem_mark 0 -> (1,1).
  Anchor-id quirk (BUG): id = anchors.len() on insert, so redefine-then-new-anchor aliases the newest node.
- Decision: split into 5.11 (YAML 1.2 semantics both sides: yaml12.py port over PyYAML events; yaml.rs; dumper quoting
  for 0o/exponent floats in _dump + pyyaml.rs; vectors) and 5.12 (native template PUT + import POST). Then 5.13
  generation-endpoint activation, 5.14 validation-detection preview. Renumber DR refs (5.12->5.13, 5.13->5.14), 5.9
  "5.10 through 5.12" -> 5.13, "document texts are 5.11's" -> 5.12's, 5.8 "5.9 through 5.13" -> 5.14.

## Epoch 16 progress: 5.11 WRITTEN (YAML 1.2 reading both sides), validate exit 0 no warnings; 6.1 depends 5.11
- Removed 5.10's consumer line for tests/servers/routes/test_config_yaml_replace.py (now a 5.11 `::*` Target).
- Advisor pass done. Kept: anchor-id quirk MIRRORED in Python (advisor suggested Python reject + documented
  divergence; I chose parity; raise in final advisor call). Advisor claim "cleared evidence keys not in values" is
  WRONG: clear_stale sets values[key]=None for cleared keys not in unset (values.py:207), so documents write null rows
  for evidence keys the document names; omitted evidence keys are unset (deleted) via _scoped_patch.
- 5.12 DESIGN (write next, before ## P6; 6.1 -> 5.12; later 5.13/5.14):
  Title "Native `PUT /api/config/template` and `POST /api/config/import`" (depends: 5.11). Targets:
  configuration_templates.py::register_template_routes; configuration_import_export.py::register_import_export_routes;
  documents.py::ConfigDocumentsService._prepare (site 4: catch ValidationError first -> ConfigMutations._validation_detail);
  contract json ::* bytes unchanged; tests/servers/routes/test_config_yaml_replace.py::* (scope-reason:
  test_yaml_revision_domain_and_exhaustion asserts new 422 {"detail": "invalid config document request"});
  tests/servers/routes/test_yaml_documents_corpus.py; crates/gruntime/tests/fixtures/yaml_documents_corpus.json;
  gruntime lib.rs, documents.rs (new), config_write.rs (replace_daemon), write_rules.rs (document texts),
  validation/candidate.rs (document candidate: no bindings; $secret: value -> "Secret configuration key '<k>' has an
  empty reference" | "Missing resolved secret binding for configuration key '<k>'"), verify.rs; tests/documents.rs;
  gdaemon Cargo.toml, pins, Cargo.lock.
  Steps: body self-parse (strict UTF-8, json.loads, ConfigDocumentRequest.model_validate, before try; 422 detail);
  1 active_state none -> 503 runtime_unavailable "Configuration runtime is not ready" retryable; 2 yaml::load
  (NonStringKey -> ["content", split]; else ["content"]); Null -> {}; non-object "YAML document must be a mapping";
  3 flatten_config port (dynamic segment msg "Dynamic configuration segment <repr> under <repr prefix|<root>> is not
  canonically encoded: <reason>" ["content"]; registered-leaf objects; empty unregistered {} leaf; ambiguous in flatten
  -> 422 "Ambiguous configuration key: <k>" ["content"]; repeated flat key first pos last value); 4 per-key loop
  (unknown "Unknown configuration key <repr>", no sourcePath/restricted "Configuration key <repr> is not daemon-owned",
  managed "... requires managed activation", all ["content", split]; ambiguous -> 500; reference -> _prepare_secret
  rules (see docs_src.txt 242-293); structured -> masked flag + restore/validate half of prepare_structured vs row
  desired_values; else value); 5 changed (py_eq vs desired_values incl defaults) / omitted; reject_unverified
  (document action "Re-import the exported evidence unchanged or activate endpoint '<seg>'") -> clear_stale(unset =
  omitted ∪ None-wire keys; writes None into values) -> reject_unprobed (document msg "Responses endpoint '<seg>' must
  be probed and activated before YAML import", action "Probe and activate endpoint '<seg>' before importing YAML");
  6 candidate -> 422 "Complete configuration candidate is invalid: <detail>" ["content"]; 7 masked && row.revision !=
  expected -> 409 revision_conflict expected/actual. Commit: config_write::commit with replace_daemon (after
  locked_snapshot: patch key without sourcePath -> Invalid "Keys are outside configuration namespace 'daemon': <repr
  sorted list>" no key (unreachable); stored sourcePath keys not named, not managed -> unset). Errors: Conflict 409
  (expected=request, actual=locked); Exhausted 422 revision_exhausted; Invalid -> 422 validation_error at key split
  (NO content prefix) or []; Blocked 409 embedding_mutation_blocked ["content"] retryable (unreachable); Ambiguous/
  Internal/DB -> 500 persistence_indeterminate "Configuration persistence outcome is indeterminate". Write wait (5.9)
  answering as _reconcile: pending/failed filtered to sourcePath keys; unknown skipped; ambiguous -> 500; timeout ->
  reconcile_failed. check_proxied: 200 skip if revision moved; else load+flatten content and check each changed key
  (named reference key row $secret: and secret==plaintext when plaintext given; structured row at body revision;
  evidence key null; other key dumps(validate_value); unnamed key no row) -> Mismatch("/changed_keys"); 422 skip if
  revision != expected else native dry run (steps before commit step 8) equal body; else Skipped. Corpus like 5.9's,
  each case through both routes. Acceptance: Python corpus; test_yaml_revision_domain_and_exhaustion; native corpus;
  replace unsets omitted daemon keys (managed/sourceless kept) + lock; check_proxied; contract bytes; gdaemon bump.
- Then 5.13 (generation-endpoint activation PUT + probe client) and 5.14 (validation-detection preview + classifier
  port, name split of validation_detection.py 862 lines). Renumber: DR Validation messages "three sites" -> four
  (add documents _prepare candidate 422 (5.12)), activation (5.13), preview (5.14); DR YAML documents bullet: mechanism
  = Yaml12Loader event-level port of yaml_serde loader/deserializer (not just resolvers), export quoting union,
  anchor quirk mirrored, >128-bit ints read as floats, residual parser differences = compare mismatches, refs
  (5.11, 5.12); 5.8 "5.9 through 5.13" (lines ~3346, ~3718) -> 5.14; 5.9 "5.10 through 5.12" (x2) -> 5.13;
  5.9 "The `document` texts are 5.11's" -> 5.12's. P5 framing, Granularity (35 deliverables), E1, 6.2 bullet.
- SHA message must also note: Python loader mechanism refinement (event-level port vs resolvers-only), site 4,
  renumbering, yaml_serde anchor-id defect mirrored.

## Epoch 17 (2026-10-14, post-compaction): 5.12 WRITTEN, validate exit 0 clean; 6.1 depends 5.12
- Cutover hold started ~13:45 CDT (#23563/#24223/#24412): draft+validate OK; commit + SHA send wait for DAEMON BACK.
- 5.12 "Native PUT /api/config/template and POST /api/config/import" inserted before ## P6 (7 acceptance items;
  needs Granularity justification). Targets incl write_rules.rs (new restore_structured split of prepare_structured),
  candidate.rs (document_candidate), config_write.rs (daemon scope), documents.rs, verify.rs.
- Advisor (this epoch): split activation into probe-client leaf + route leaf, and classifier port + preview route
  (=> 5.13 probe client, 5.14 activate PUT, 5.15 classifier, 5.16 preview; total 37 deliverables). Wire-level fakes:
  fake `codex` exe on PATH (exec + app-server JSON-RPC) and fake HTTP servers (openai-compatible, LM Studio, Ollama,
  vLLM /v1/models); fakes log argv/env subset/stdin/bodies; both sides' logs equal. Decide explicitly: free text in
  probe_diagnostics / probe 422 detail (str(exc) of SDK errors) = documented divergence vs classification equality;
  determinism (fake vision server decodes PNG, answers right color; Rust injectable sleep); check_proxied for activate
  (200 row check; pre-probe 422 dry run; probe 422 Skipped); probe_verified=True branch of patch_flat; execution model
  of gdaemon handlers (blocking vs async); compare mode: only Python probes.
- Activation route facts: ActivateGenerationEndpointRequest(extra forbid): expected_revision ConfigRevision,
  protocol (openai-compatible|lmstudio|ollama|vllm, default openai-compatible), wire_api (chat-completions|responses,
  default responses), api_base str, model str, api_key str|None, tool_chat bool=True. Handler order:
  encode_dynamic_segment(name); service.desired_secret(prefix.api_key) + desired_config() (ConfigValuesError body);
  responses && no key -> HTTPException 422 detail {code validation_error, path [api_key], message "Generation endpoint
  API key is required"}; GenerationEndpointConfig(...) + GenerationConfig(endpoints={name: ep}) ValueError -> 422
  detail str(exc) (site 2); _probe_config(desired, name, ep) = DaemonConfig.model_validate(dump with endpoint);
  probe_responses_endpoint | probe_chat_completions_endpoint; (EndpointActivationError, ValueError) -> 422 detail
  str(exc); values = 9 prefix keys (+api_key when request key given and != stored); patch_flat(probe_verified=True)
  ConfigValuesError body; 200 {**mutation, endpoint, provider ("codex" if responses else protocol), model
  "endpoint:<name>/<model>", input_modalities, probed_model, probed_json, probed_tools, probe_diagnostics}.
- endpoint_activation.py: chain under asyncio.timeout(ai.generation.timeout_seconds) -> "<Transport> endpoint
  activation timed out after {t:g} seconds"; _retry_activation 3 attempts, transient /\b(?:429|5\d\d)\b/, retry-after
  regex, delay min(ra,60) else 1.0; text probe fatal (_sanitized_activation_error: _AUTH_RE -> "<T> endpoint
  authentication failed; verify the configured secret" else "<T> endpoint activation failed: <redacted>"); json/tools/
  vision degraded -> diagnostics[json|tools|vision] = redacted str(exc). Responses: _probe_text/_probe_json via
  CodexCLITextGenerateAdapter(timeout 120, env codex_endpoint_env, overrides); tools via CodexAppServerClient
  start_thread(model, approval never, sandbox read-only) + 2 turns + resume in new process; vision via app-server
  ephemeral thread with image + CodexEndpointVisionExtractAdapter. Chat: _resolve_probe_model (vllm ->
  resolve_vllm_served_model); create_local_provider_adapter (openai-compatible|vllm -> OpenAI SDK adapter; lmstudio,
  ollama native; client None -> tools probe "Endpoint adapter has no client for tool probing"); text max_tokens 64;
  json generate_json(allow_fallback False); tools chat.completions.create with gobby_probe_tool + forced tool_choice;
  vision via generate_text_result(images). Colors red/green/blue secrets.choice; 64x64 PNG.
- DAEMON BACK 13:42 CDT (PID 61870, gcode 1.9.28): holds lifted; commits allowed.
- NEW STRUCTURE (advisor): 5.13 Rust probe client (no route; wire-level parity fakes), 5.14 native activate PUT,
  5.15 classifier port, 5.16 native validation-detection preview; 6.1 -> 5.16; 37 deliverables. Renumber: DR
  "Validation messages" sites: 1 _validate_value (5.7), 2 activation 422 (5.14), 3 preview 400 (5.16), 4 documents
  candidate 422 (5.12). 5.8 "5.9 through 5.13" -> "5.9 through 5.16"? (check which leaves use validate_candidate:
  5.9,5.10,5.12,5.14; say "the write leaves"). 5.9 "5.10 through 5.12" -> 5.10, 5.12 and 5.14.
- Codex facts: codex_endpoint_provider_id = "gobby_endpoint_<name>"; vllm provider "gobby-vllm-<name>";
  display name ("openrouter"->"OpenRouter", else replace -,_ by space, .title()); overrides (json.dumps quoting):
  model_provider, model, model_providers.<id>.name/base_url/env_key=GOBBY_CODEX_ENDPOINT_API_KEY/wire_api="responses",
  shell_environment_policy.exclude=["GOBBY_CODEX_ENDPOINT_API_KEY"], features.shell_snapshot=false; vllm variant
  (base_url = vllm_api_base; model omitted for auto; env_key/exclude only with key). codex_endpoint_env -> {KEY: key}
  (ValueError "Generation endpoint API key is unavailable; configure its referenced secret" if empty/$secret:).
  app_server_env: CODEX_HOME=<gobby_home>/codex-endpoints/<name> (mkdir) + key env (vllm keyless: home only).
  codex_event_text: delta str | item.content str | join block text/delta.
- CodexCLITextGenerateAdapter.build_command: [codex(which), --ask-for-approval, never, -c each override (model=
  dropped when request.model, then -c model=<json> appended), exec, --ephemeral, --ignore-user-config,
  --skip-git-repo-check, --ignore-rules, --sandbox, read-only, --output-last-message, <tmp>/last-message.txt, -];
  stdin = ONE_SHOT_DIRECTIVE system prompt + "\n\n" + prompt (_with_one_shot_directive, _compose_prompt);
  cwd neutral temp dir (neutral_textgen_cwd); env = os.environ + overrides, minus GOBBY_AGENT_API_TOKEN,
  GOBBY_HOOKS_DISABLED=1; timeout 120 -> RuntimeError "Codex CLI timed out after 120s: <shlex.join>"; nonzero ->
  "Codex CLI failed with exit code N: <stderr or stdout>"; result = last-message file stripped. Probes: text prompt
  "Reply with exactly GOBBY_K3_TEXT_OK." must contain token; json prompt 'Reply with JSON object {"ok": true} and
  nothing else.' json.loads(strip) dict.
- CodexAppServerClient: spawn [codex, app-server, -c ...], env as above, stdio JSON lines; initialize
  {clientInfo{name gobby-daemon, title Gobby Daemon, version 0.1.0}} (60 s request timeout), then notification
  "initialized" {}; start failure -> RuntimeError "Failed to start Codex app-server: <e>; stderr: <compact>" with
  env secrets (KEY/TOKEN/SECRET names) redacted. thread/start {model, approvalPolicy never, sandbox read-only,
  ephemeral?}; result.thread.modelProvider (default "openai") must == provider id else "Responses <phase> returned
  modelProvider=<repr>; expected <repr>". thread/resume {threadId}. turn/start {threadId, input [{type text,text},
  {type localImage,path}]}; events until turn/completed|turn/failed (error -> "Codex turn failed: <error>"; status
  error/failed) or thread/closed; incoming server requests without handler -> error -32601 "No handler registered
  for <method>"; error responses -> "RPC error <code>: <message>"; process exit -> ConnectionError. stop: terminate,
  wait 5 s, kill. _collect_turn: tool started/completed = item type not in {None, agentMessage, reasoning};
  text = deltas joined or completed joined. Tool probe prompts exact (see endpoint_activation.py:213-263).
  Vision probe: ephemeral thread + localImage probe.png in temp dir, then CodexEndpointVisionExtractAdapter
  (new app-server, ephemeral thread, prompt "Describe the solid-color image.", "Codex Responses vision extraction
  produced no text").
- Chat adapters: openai-compatible|vllm -> AsyncOpenAI(base_url api_base or vllm_api_base, api_key or "" (no auth
  header when empty), timeout 120/connect 5/read 120/write 30/pool 5, max_retries 0); POST <base>/chat/completions
  {model, messages[system "You are a helpful assistant." or given, user], max_tokens}; json adds response_format
  json_object (allow_fallback False in probe); blank content -> LLMProviderError "OpenAI-compatible provider at
  <repr base> using model <repr> returned blank content"; _parse_json_response ("Empty response from local LLM",
  "Failed to parse local LLM response as JSON: <exc>", fences stripped). Images: image_url data URLs before text.
  lmstudio: POST <origin>/api/v1/chat {model, input (str | [{type image,data_url},..,{type message,content}]),
  system_prompt, stream false, store false, max_output_tokens}, timeout 300, Authorization when key; text from
  output[type message].content | content|text|response. ollama: POST <origin>/api/chat {model, messages[system,
  user(+images base64)], stream false, keep_alive -1, options{num_predict}}; json adds format "json"; no auth.
  lmstudio/ollama client None -> tools probe "Endpoint adapter has no client for tool probing" (degraded).
  vLLM: GET vllm_api_base(api_base)+"/models" (origin = strip, rstrip /, drop trailing /v1) timeout 10; errors
  LocalModelError (Exception, NOT ValueError): timeout/connect/status/invalid JSON texts in local_model.py:436;
  ids from data|models list, id|model|name stripped, dedup; select: "auto" exactly one else "model: auto requires
  exactly one served vLLM model; found N: <ids|(none)>"; explicit must be served else "vLLM endpoint at <base> does
  not serve model <repr>. Served models: <..>". FOUND BUG: LocalModelError escapes route's except -> 500; route leaf
  catches it -> 422 (Python change in configuration_generation_endpoints.py).
- gdaemon is tokio/axum/hyper async (tokio "process" feature present); gcore reqwest is blocking only. RouteFamily
  check_proxied returns Pin<Box<dyn Future>> (plan line 400). Probe client should be async (tokio::process,
  hyper-util client or reqwest async? decide; gdaemon already has hyper + hyper-rustls client).
- Advisor decisions to make in 5.13/5.14: diagnostics/422 free text from SDK exceptions = documented divergence,
  parity asserts classification (which probe degraded; auth failed vs activation failed vs timed out) + exact text
  for our own messages; fake vision server decodes PNG and answers right color (no seam); Rust injectable sleep;
  check_proxied activate: 200 row check of written keys, pre-probe 422 dry run, probe 422 Skipped; compare mode:
  only Python probes; probe_verified=True branch of patch_flat must be read and ported (values_patch.rs/write_rules).

## Epoch 18 (2026-10-14, post-compaction): 5.13 WRITTEN, validate exit 0 clean; 6.1 depends 5.13
- 5.13 "Native generation-endpoint probe client" inserted (scratchpad leaf_513_head.md + pins_block.md + leaf_513_tail.md),
  5 acceptance items, 10 production targets (Granularity must justify). Stack: async reqwest 0.12 (json, rustls-tls, as
  gclient), tokio process/io-util/fs/time, png 0.17 (NoFilter), base64, rand OsRng, tempfile, libc (unix, SIGTERM/SIGKILL
  groups). Host {env, home, sleep} injection; probe(host, endpoint, timeout_seconds) -> ProbeOutcome | ProbeError(String).
  ONE shared stdlib fake: crates/gruntime/tests/fixtures/probe_fakes.py (codex shim via `uv run --quiet --no-project
  python ... codex <case dir> "$@"`; `serve` HTTP fake writes port file); logs compared as JSON values; images
  normalized to "<probe image>"; corpus crates/gruntime/tests/fixtures/endpoint_probe_corpus.json; {"prefix": ...} for
  library free text. pystr::format_g added. DR bullet "Endpoint probes" still TO ADD (5.13 references it).
- 5.13 FIX NEEDED: wire_api responses requires protocol openai-compatible (GenerationEndpointConfig.validate_endpoint),
  so _vllm_config_overrides, codex_vllm_provider_id and the vLLM keyless branch of codex_endpoint_app_server_env are
  unreachable from activation: remove them from 5.13's Codex list (state why).
- 5.14 FACTS (route configuration_generation_endpoints.py::register_generation_endpoint_routes, nested
  activate_generation_endpoint): order = encode_dynamic_segment(name); desired_secret(prefix.api_key) + desired_config()
  (no snapshot -> 503 runtime_unavailable "Configuration runtime is not ready", path [], retryable); api_key =
  request.api_key or stored; responses && !api_key -> 422 {"detail": {"code":"validation_error","path":["api_key"],
  "message":"Generation endpoint API key is required"}}; GenerationEndpointConfig(...) is OUTSIDE any try today ->
  ValidationError reaches global handler -> 500 {"status":"error","message":"Internal server error","error_logged":true}
  (FOUND BUG: fix by constructing inside try, catching pydantic ValidationError -> 422 detail
  ConfigMutations._validation_detail(exc) = site 2); GenerationConfig(endpoints={name: ep}) same try. Probe except adds
  LocalModelError. values = 9 keys (+ api_key when request.api_key and != stored); patch_flat(probe_verified=True) skips
  reject_unprobed, reject_unverified, clear_stale (values.py); response {**mutation, endpoint, provider ("codex" if
  responses else protocol), model "endpoint:<name>/<model>", input_modalities, probed_model, probed_json, probed_tools,
  probe_diagnostics}. ConfigValuesError -> public_body; other exceptions -> global 500 body above.
  _probe_config cannot fail (no DaemonConfig validator reads endpoints) -> native passes desired timeout_seconds directly.
- Site-2 messages (verified): "api_base: Value error, api_base must not be empty"; "api_base: Value error, api_base must
  use http/https"; "Value error, model must be set for generation endpoints"; "Value error, wire_api='responses' requires
  protocol='openai-compatible'"; "endpoints: Value error, generation endpoint names must match [a-z0-9][a-z0-9_-]*:
  'Bad'". api_base " http://x/ " validates to "http://x/". Native: validate via carrier model schemas
  (GenerationEndpointConfig, then GenerationConfig {endpoints:{name: ep}}) as 5.8's validate_feature_candidates does;
  format with 5.7's detail(errors).
- Request self-parse (new): strict UTF-8, json.loads, ActivateGenerationEndpointRequest.model_validate (extra forbid);
  failure -> 422 {"detail": "invalid generation endpoint activation request"}. Lax python-mode rules (verified):
  expected_revision ConfigRevision strict; protocol/wire_api exact literals (case-sensitive; defaults openai-compatible /
  responses); api_base, model str only; api_key str|null; tool_chat bool lax: true/false, int 0/1 (big ints fail),
  float 0.0/-0.0/1.0, strings case-insensitive exact (no strip) "0","off","f","false","n","no" / "1","on","t","true",
  "y","yes"; "" fails.
- Path: Starlette routes on the decoded path, so a %2F name -> Python 404: native claims() declines such a request
  (forward to Python). Decode the raw segment like urllib unquote (percent bytes, UTF-8 lossy).
- 5.14 design: route corpus uses 5.13 fakes (real probes, no seam): chat via fake HTTP server; one responses success
  via codex shim; RuntimeFamily::with_host(pool, home, host) test constructor (new builds Host::process(home)).
  check_proxied: 200 -> rebuild the written values from request + body (validated api_base, model, tool_chat,
  protocol, wire_api, 4 evidence fields from body; api_key reference when changed_keys has it) and run 5.9's 200-branch
  row check; 422 -> Skipped when row revision != expected_revision; else native pre-probe dry run (parse, row, api_key,
  site 2): dry-run failure body must equal Python's, dry-run pass -> Skipped (probe/patch 422); others Skipped.
  Targets: route py ::register_generation_endpoint_routes, tests/servers/routes/test_endpoint_activation_corpus.py,
  crates/gruntime/tests/fixtures/endpoint_activation_corpus.json, gruntime lib.rs, activation.rs (new), values_patch.rs
  (prepare with probe_verified), validation/mod.rs (model-schema entry if needed), verify.rs, tests/activation.rs,
  gdaemon Cargo.toml, pins, Cargo.lock. Existing tests/servers/routes/test_config_values_api.py activation tests send
  valid bodies (likely consumers-unchanged lines). No web caller of activate.
- Remaining after 5.14: 5.15 classifier port (validation_detection.py 862 lines, name split), 5.16 preview, sweep,
  DR "Endpoint probes" bullet + DR Validation messages four sites, Granularity 37, advisor, commit, send.

## Epoch 19 (2026-10-14, ~15:00 CDT): 5.14, 5.15, 5.16 WRITTEN; bare validate exit 0, no warnings; 6.1 depends 5.16
- 5.14 native activate PUT: gate declines decoded '/' segment; self-parse 422 "invalid generation endpoint activation
  request"; row read 503 not-ready; api_key 422; site 2 via validation::validate_model(cls, value) (NEW fn in
  validation/mod.rs; GenerationEndpointConfig then GenerationConfig); probe via 5.13 (no pool conn held); 9/10 values;
  values_patch patch_flat port with probe_verified true; 500 = global body; check_proxied (200 rebuild + 5.9 check; 422
  parse-fail compare / Skipped on revision move / dry run steps 1-3). Removed conflicting consumer lines: 5.9's
  configuration_generation_endpoints.py line and 5.14's test_config_values_api.py line.
- 5.15 classifier port: NO target on validation_detection.py (unchanged; no split needed). Builtins carrier
  crates/gruntime/assets/validation_detection_builtins.json = 3rd output of scripts/generate_runtime_config_contract.py
  (builtin matchers, default wrapper rules, uv options). classifier/{mod,shlex}.rs; pystr isalnum (L*/N* categories,
  verified 0 mismatches on 3.14.3/Unicode 16), isdigit (Nd + 128 code points in 20 ranges), rstrip, split_whitespace.
  Only first match's 4 fields ported; evidence fields not.
- 5.16 preview: self-parse 422 "invalid validation detection preview request"; config -> validate_model(...
  ValidationDetectionConfig) 400 detail (site 3); Python switches to model_validate_json(to_json(config), strict=True)
  (lax accepted "enabled": "yes"/1 which PATCH save rejects; only caller is settings ProjectsSessionsSection editor);
  no config -> active_values six keys; 503 "still starting"; check_proxied reruns native. Targets
  tests/config/test_live_policy_consumers.py::test_validation_detection_uses_runtime_snapshot (calls endpoint with model).
  Removed 2.2's consumer line for test_live_policy_consumers.py (conflict).
- Validation-message sites now: 1 _validate_value (5.7), 2 activate 422 (5.14), 3 preview 400 (5.16), 4 documents
  candidate 422 (5.12).
- RESTART HOLD announced ~15:00 CDT by #15977 (tip 969adcb231): no land/close/heavy runs until DAEMON BACK.
- REMAINING: sweep (DR Endpoint probes bullet; DR Validation messages 4 sites; DR YAML documents details; 5.8/5.9
  cross-refs "5.10 through 5.12" etc.; 2.2 consumer inventory; D4/P5 framing; 6.2; E1; Granularity 37 deliverables
  with per-leaf justification incl release trio); advisor (raise anchor-id mirror, clear_stale None, preview strictness
  change, no split for 5.15); commit plan only; send SHA+sha256 to #15977 and #16491 with change notes; arm wait.

## Epoch 20 (2026-10-14, after DAEMON BACK 14:53 CDT): sweep DONE, committed dedd973d29, sent; awaiting consensus
- Sweep: DR YAML documents (event-level loader, anchor mirror, >128-bit ints as floats, export quoting union, unpinned
  parser differences = E1 mismatches); DR Validation messages 4 sites; new DR "Endpoint probes"; 5.7/5.8/5.9 cross-refs;
  Constraints "P5 leaves edit Python route modules in place"; P5 framing lists all 18 routes; 6.2 parity list; 6.3
  compare phase exercises every P5 route (ui_settings.planPendingVariant PATCH pair, invalid-YAML 422 for template/
  import, pre-probe 422 activate, preview) + backend-kill P5 reads; E1 test list + soak probe for every P5 route;
  E1 4.2 exception for the probe's PATCH pair; Granularity 36 deliverables (37 was a miscount).
- Commits: 6b1eb45b21 (bulk), dedd973d29 (E1 4.2/4.3 fix). sha256 02a33fd705f41c3a3686b035299c1485ae8f04d16e7728f1847d06148f8ada3a.
- Advisor: the four raised items hold as written.
- Sent to #16491 (session EXPIRED, queued) and #15977 (asked it to resume #16491). Next: wait on #15977; at
  consensus add one dated V1 entry, commit, send SHA to adversary; commit its M1 bytes unchanged; send stamped plan.

## Epoch 21 (2026-10-14 ~15:35 CDT): adversary #16491 resumed (run 8567081c); RF-F9/RF-F10 repaired at a32fe50852
- #15977: when #16491 stamps, send #15977 the stamped revision for review BEFORE Josh; include Josh's heads-up on
  E1's ui_settings.planPendingVariant probe write in that presentation.
- RF-F9 (accepted path b): no-new-crate rule was the plan's own (550d7e7ff7); named exception icu_casemap 2.1.1 +
  icu_casemap_data 2.1.1 (icu_casemap was unlocked too; its other deps locked); E1 item 8 lock gate.
- RF-F10 (accepted): 5.13 uses nix 0.29 (Unix, default-features=false, feature signal -> process; locked via
  mac_address) killpg/kill + Pid; no libc; unsafe_code forbid inherited; planned check cargo clippy -p gobby-runtime.
- a32fe50852 sha256 cfeb61a17c835131927417c89d8f9fe5fbb7f329e616d5ea5286d9fe10e6e896; sent to #16491 and #15977.
- #16491 said its source/consumer spot checks were still running: more findings may come.
