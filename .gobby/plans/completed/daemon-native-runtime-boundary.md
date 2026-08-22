# Daemon-native runtime boundary for gcode and gwiki

> **Plan ID:** daemon-native-runtime-boundary

Plan artifact: `.gobby/plans/daemon-native-runtime-boundary.md`

## Overview
`kind: framing`

Epic #18902. Make `gcode` and `gwiki` authenticated clients of the active Gobby daemon while preserving efficient direct access to authorized project datastores. An authenticated handshake precedes feature-service construction; short-lived scoped grants carry deployment identity, schema identity, API version, capabilities, connection material, fencing epoch, and expiry; all AI execution routes through daemon contracts; standalone runtime configuration, local credential ownership, direct provider fallbacks, and daemon-optional feature modes are removed.

This plan discharges two inherited obligations: `deferred-from:codewiki-ownership-move:D1` (item D1.1 — handshake, grants, standalone-mode removal, daemon-mediated AI) and `deferred-from:wiki-output-design:D5` (item D5.1 — the handshake, grants, and daemon-mediated AI the narrative and cluster-naming passes consume). #18902 gates #17678, #19664, #19671, #19672 in the live task graph.

Foundation already shipped (credential-isolation epic #19543, closed 2026-08-04): `gobby-agent-v1` HMAC capability tokens with an enumerated route capability matrix (`src/gobby/servers/auth_service.py`), the v1 `ServiceCapabilityBundle` grant at `GET /api/config/service-capabilities`, scoped ephemeral Postgres roles with generation counters (`src/gobby/storage/managed_credentials.py`), and complete Rust daemon-AI contracts for all five modalities (`crates/gcore/src/ai/daemon/operations.rs`). This plan generalizes that machinery from daemon-spawned executions to every invocation, adds the missing fencing epoch and schema-identity carriage, and deletes the standalone/direct/Auto arms.

Decision record (settled 2026-08-09): D1 cached grant + renewal, one handshake protocol for all callers; D2 daemon-owned HMAC signing secret in the hub DB, daemon is sole verifier; D3 per-deployment fencing epoch bumped on lease acquisition, lease keying unified on `deployment_advisory_key()`; D4 grant-validity window during daemon outages (datastore ops work on an unexpired grant, AI always requires the live daemon, everything fails typed once the grant expires); D5 the v2 grant is the single source of capability truth, probe-based discovery is deleted; D6 scope is gcode + gwiki + gcore + daemon-side grant work only.

## Constraints
`kind: framing`

- **No backward compatibility.** 0.5.0 is unshipped. Removed flags, env vars, and modes are deleted outright. Contract snapshots regenerate whenever a surface is removed; the CLI contract version bumps (gcode 3→4, gwiki 16→17) land exactly once, in 4.1, after the last surface removal.
- **Named defaults** (settled, non-material): grant TTL 1 hour (matches `MAX_ROLE_LIFETIME`); renew-on-use once remaining TTL < 50%; interactive principal is a scoped Postgres role per (machine_id, project_id) with the same table-grant surface as managed roles; grant cache at `~/.gobby/grants/<deployment_token>/<project_id>.json`, 0600, atomic write; handshake is a POST (the agent-claims path rejects query parameters).
- **One grant reader.** Daemon-spawned runs receive a pre-materialized grant file referenced by `GOBBY_MANAGED_EXECUTION_BOOTSTRAP`; interactive runs acquire the same file format via handshake. One Rust reader consumes both.
- **Client-side validation is structural, not cryptographic.** Clients check expiry, deployment token, schema identity, and bundle version; the HMAC signature is verified only by the daemon when a grant is presented (renewal, brokered operations). No asymmetric crypto, no rotation ceremonies, no client-verifiable signatures — the future Rust daemon's issued-API-key model keeps the issuer-verifies trust shape. Every grant file additionally carries a canonical-payload integrity checksum that clients verify on every load from every source; the checksum detects corruption and is never an authorization trust boundary — an attacker who can rewrite the 0600 grant file can rewrite the binary, so local tampering sits outside this plan's threat model.
- **Non-goal: WebSockets.** gcode/gwiki stay HTTP-only request/response clients. WS pays off for resident processes needing server push; neither binary is resident. If a resident mode ever lands, SSE over :60887 is the first candidate. No WS grant-awareness, no WS capability matrix.
- **Non-goal: ghook and gdaemon.** ghook keeps fire-and-forget delivery with the write-ahead inbox; gdaemon keeps env-gated schema-identity enforcement. Neither performs the handshake.
- **Loopback-only first contact.** The grant handshake is a same-machine boundary: clients reject non-loopback `daemon_url()` endpoints typed before attaching any bearer, so no credential is ever presented to an endpoint whose deployment binding is not yet established. Remote access is the future hub-and-spoke boundary — a client daemon registering with the server daemon over an authenticated channel that establishes server identity before credentials flow — and is explicitly out of scope for this plan. Loopback locality is a precondition, never server proof: when no trusted endpoint→deployment binding exists, the client first runs a bearer-free challenge-response proving the listener knows the caller's own credential secret (1.3, 2.1) before any bearer or handshake request is sent.
- **Dormancy invariants preserved** (codewiki-ownership-move D3, owner #19665): `GET /api/wiki/code/status` stays 200 `{"enabled": false, "state": "disabled", "reason": "pending_wiki_redesign"}`; `POST /api/wiki/code/refresh` stays 409; nothing is re-enabled here.
- **Facade survival.** The grant boundary wraps `CodewikiFacts::open`/`read_connection` without widening the facade; #17678 later supersedes the facade with `gobby_core::code_facts::FactsBundle` behind the same grant-resolved connection path, so nothing in this plan may couple grant logic to `codewiki_facts` internals.
- **Symbol summaries stay automatic.** Per-symbol summaries remain Python daemon work (`src/gobby/code_index/summarizer.py`, maintenance loop); this plan does not move them. `gcode` keeps reading `code_symbols.summary`.
- **Test harnesses** mint grants through the handshake with the operator token, or write grant cache files directly via test helpers. `GOBBY_TEST_PROTECT=1` conventions unchanged.
- **Provenance labels stay.** #18902 remains open with both `deferred-from:` labels until child work closes; the coverage ledger companion (`daemon-native-runtime-boundary.coverage-ledger.yaml`) ships alongside this plan and is adversary-reviewed before expansion.
- **Schema authority is baseline 375.** Through the 0.5.0 pre-release period, `MIGRATIONS` in `crates/gcore/src/schema/assets.rs` stays empty and every schema change folds into `crates/gcore/assets/schema/baseline.sql` with idempotent guards (`IF NOT EXISTS`), following the `a3b56649a` convention. This plan adds no numbered migration.
- **Sequencing with #19645 (reactive ConfigStore).** That epic lands first: its 1.2 delivers the baseline-375 receipt-refresh path our baseline edit rides on, and its 2.2/2.6 rebuild the effective-config routes and the gcode config stack that our 1.2, 2.2, and 4.1 then supersede (deleting the service-capabilities route, the standalone arms, and their tests). Enforcement is the live epic-level `blocked-by` edge from #18902 onto #19645, recorded in the task graph by the plan coordinator; expansion compiles internal section dependencies only and synthesizes no external per-leaf edges. Because external ancestor blockers are not inherited by leaf dispatch eligibility (dispatch checks a leaf's own `blocked-by` edges and ancestor planning/expansion stages only), the coordinator records one additional `blocked-by` edge at expansion time: implementation leaf 1.1 onto #19645. Every other implementation leaf serializes behind 1.1 through the manifest's internal dependency chain, so expansion and leaf creation proceed while #19645 is open with no leaf dispatch-eligible before #19645 closes; dispatch additionally requires explicit `gobby build` opt-in (`allow_automation`). Schema-identity regeneration is serialized between the two epics: whichever lands second regenerates.
- **Grant presentation.** Brokered and AI requests attach the signed grant in an `X-Gobby-Runtime-Grant` header (base64url of the canonical JSON); the daemon verifies signature, deployment, epoch, schema, API contract, capability, and principal before any handler side effect. Offline direct datastore use trusts the 0600 cache file; HMAC verification happens only at daemon presentation.

## P1: Daemon grant foundation
`kind: framing`

**Goal**: The daemon owns a per-deployment fencing epoch, a grant signing secret, and a handshake surface that issues v2 grants to any authenticated caller.

### 1.1 Fencing epoch and lease identity unification [category: code]
`kind: deliverable`

Targets:
- `src/gobby/daemon_lease.py::*` — scope-reason: lease keying migrates from hashtext to deployment_advisory_key and acquisition gains the epoch bump
- `src/gobby/daemon_lease_control.py::*` — scope-reason: standby promotion and stale recovery must surface and bump the epoch
- `src/gobby/deployment.py::*` — scope-reason: epoch persistence joins deployment-identity derivation helpers
- `src/gobby/runner.py::*` — scope-reason: lease construction site passes deployment identity
- `crates/gcore/assets/schema/baseline.sql`
- `crates/gcore/src/schema/assets.rs::*` — scope-reason: BASELINE_CHECKSUM refreshes for the edited baseline while MIGRATIONS stays empty
- `crates/gcore/assets/schema/catalog.manifest.json::*` — scope-reason: regenerated column catalog gains the deployment_runtime table and column rows
- `src/gobby/storage/schema_expected_identity.json::*` — scope-reason: packaged expected identity regenerates for the edited baseline
- `crates/gcore/tests/schema_contract.rs::embedded_assets_publish_a_complete_schema_identity`
- `crates/gdaemon/tests/cli_contract.rs::version_json_reports_exact_schema_identity_contract`
- `crates/gcore/src/schema/runner.rs::*` — scope-reason: predecessor-receipt recognition advances to accept the #19645 baseline receipt as the exact upgrade predecessor
- `crates/gcore/src/schema/runner_tests.rs::*` — scope-reason: receipt classification tests cover fresh, pre-#19645, post-#19645, current, and mismatch states
- `tests/test_daemon_lease.py::*` — scope-reason: lease tests gain deployment-scoped epoch, per-acquisition secret rotation, and restore-replay coverage

Create table `deployment_runtime`: `deployment_token TEXT PRIMARY KEY`, `fencing_epoch BIGINT NOT NULL DEFAULT 0`, `grant_signing_secret TEXT NOT NULL`, `epoch_updated_at timestamptz`. This fold also owns the complete interactive-principal DDL that 1.3 consumes: the `gobby_agent_auth` owner-kind extension to `interactive`, the unique `(deployment_token, machine_id, project_id)` active binding, the issue-or-reuse SQL function, and the interactive credential-material store keyed `(deployment_token, machine_id, project_id, credential_generation)` that lets the daemon recover live-generation connection material after restart, with atomic replace-and-cleanup on rotation or revocation. All of it folds into baseline 375 with `IF NOT EXISTS` guards — no numbered migration (Constraints: schema authority) — and the baseline identity is sealed here before any regeneration artifact is produced; 1.3 adds no schema change. The baseline edit rides the receipt-refresh path #19645 1.2 lands first (ordering enforced by the epic-level #18902→#19645 `blocked-by` edge, Constraints). Because the schema runner recognizes exactly one predecessor receipt, this second rewrite of baseline 375 advances the receipt chain: `runner.rs` recognizes the #19645 baseline receipt as the exact predecessor, and `runner_tests.rs` classifies fresh installs, pre-#19645 hubs (refused with the upgrade path named), the #19645 receipt (upgraded), the current receipt, and arbitrary mismatches (refused). Regeneration is deterministic and ordered: update `BASELINE_CHECKSUM` in `assets.rs`; regenerate the catalog manifest against an isolated PostgreSQL database with `UPDATE_GCORE_SCHEMA_MANIFEST=1`; build release `gdaemon`; regenerate the packaged expected identity with the identity generator pointed at that release binary (exact command in E1); rerun catalog freshness in non-update mode plus both identity contract tests. `ActiveDaemonLease` migrates its advisory-lock keys from `hashtext('gobby-single-active-daemon-v1'), hashtext(current_database())` to `deployment_advisory_key("single-active-daemon")` so lease, epoch, and grant identity agree on `deployment_token(data_root)`. Every successful lease acquisition — initial start, standby promotion, stale-owner recovery — increments `fencing_epoch` atomically in the same transaction that observes the acquired lock, and the daemon caches the epoch it owns. The grant-signing secret rotates freshly and atomically (`secrets.token_urlsafe(32)`) in that same transaction on every acquisition: the epoch bump already invalidates outstanding grants at presentation, so rotation adds no operational cost, and it guarantees a database restored from backup can never resurrect an archived grant at daemon presentation — after restore-and-reacquire, old signatures fail against the fresh secret even where token and epoch repeat. Rotation protects the daemon presentation boundary only: an archived unexpired grant still authorizes offline direct datastore construction until its expiry (decision D4), and whether restored scoped-role, FalkorDB, or Qdrant credentials still connect is decided by each restored datastore — 6.1 covers the three direct variants separately. Operator-initiated invalidation is a daemon restart; no out-of-band regeneration path exists.

The active-daemon advisory lease is acquired exclusively by the supervising Python daemon. gcode, gwiki, and gcore contain no lease-acquisition code and never reference `deployment_advisory_key("single-active-daemon")`; feature clients obtain grants only. The E1 zero-match audit proves the client crates carry no advisory-lease reference, and the lease object exposes a liveness/ownership check (lease connection alive, owned epoch authoritative) that 5.1's pre-handler guard consumes. The owned epoch is also the effect-fencing authority: `deployment_runtime` is the row 5.1's in-transaction epoch validation reads, so a displaced daemon's stale transaction fails its epoch predicate instead of committing.

The credential-material store persists ciphertext only: connection material is sealed under the daemon's existing managed-credentials key envelope with AAD binding each record to its `(deployment_token, machine_id, project_id, credential_generation)` identity, so a record decrypts only for the exact principal and generation it was stored for. The daemon is the sole authorized reader; rotation and revocation atomically replace or delete records; retired generations are cleaned up; database backups therefore never contain plaintext connection material, preserving the credential-isolation invariant that plaintext exists only during issuance and runtime materialization.

**Acceptance:**

- 1.1.1 - `deployment_runtime` table exists with deployment token, monotonic fencing epoch, and signing secret; epoch increments exactly once per lease acquisition including promotion and stale recovery. file: `src/gobby/daemon_lease.py`.
- 1.1.2 - Lease advisory-lock keying uses `deployment_advisory_key`; the `hashtext` scheme is gone. symbol: `ActiveDaemonLease`.
- 1.1.3 - Two deployments sharing one database hold independent leases and epochs. test: `tests/test_daemon_lease.py::test_deployment_scoped_lease_and_epoch`.
- 1.1.4 - `deployment_runtime` DDL ships inside baseline 375 with idempotent guards; no numbered migration is added; `BASELINE_CHECKSUM`, the catalog manifest, and the packaged expected identity regenerate together in the stated order. file: `crates/gcore/assets/schema/baseline.sql`.
- 1.1.5 - gdaemon applies the refreshed baseline to fresh and existing hubs, and both embedded identity contract tests pass alongside Python expected-identity parity. test: `crates/gdaemon/tests/cli_contract.rs::version_json_reports_exact_schema_identity_contract`.
- 1.1.6 - Receipt classification recognizes the #19645 baseline receipt as the exact predecessor; fresh, predecessor, current, and arbitrary-mismatch states classify correctly. test: `crates/gcore/src/schema/runner_tests.rs::receipt_chain_advances_from_19645_baseline`.
- 1.1.7 - The complete interactive-principal and credential-material DDL is sealed in this fold before identity regeneration; 1.3 introduces no schema change. file: `crates/gcore/assets/schema/baseline.sql`.
- 1.1.8 - Only the supervising daemon acquires the active-daemon advisory lease; client crates contain zero advisory-lease references per the E1 audit. file: `src/gobby/daemon_lease.py`.
- 1.1.9 - The grant-signing secret rotates atomically with every lease acquisition and epoch bump; restore-and-reacquire rejects archived grants at daemon presentation, while offline direct authorization stays bounded by grant expiry. test: `tests/test_daemon_lease.py::test_signing_secret_rotates_on_acquisition`.
- 1.1.10 - The credential-material DDL is ciphertext-shaped: sealed columns carry ciphertext and AAD identity only, and no plaintext connection-material column exists in the baseline. file: `crates/gcore/assets/schema/baseline.sql`.

### 1.2 v2 grant bundle: schema, signing, and rejection matrix [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `src/gobby/runtime_grants/__init__.py`
- `src/gobby/runtime_grants/schema.py`
- `src/gobby/runtime_grants/signing.py`
- `src/gobby/runtime_grants/service.py`
- `src/gobby/servers/routes/configuration_effective.py::*` — scope-reason: v1 ServiceCapabilityBundle models and the service-capabilities grant surface are superseded by the v2 bundle
- `src/gobby/storage/schema_contract.py::*` — scope-reason: schema identity becomes readable by the grant issuer at runtime
- `tests/servers/routes/test_configuration_effective_routes.py::*` — scope-reason: service-capabilities contract tests pinned by #19645 are deleted with the route
- `tests/runtime_grants/test_rejection_matrix.py`
- `tests/runtime_grants/test_golden_vectors.py`
- `tests/runtime_grants/test_active_config_binding.py`

New package `src/gobby/runtime_grants/` owns the v2 bundle. Shape (Pydantic, `extra="forbid"`, frozen):

```json
{
  "version": 2,
  "api_contract": 1,
  "config_revision": 41,
  "deployment": {"token": "<16-hex>", "fencing_epoch": 7},
  "schema_identity": {"runner_protocol": 1, "baseline_version": 375, "baseline_checksum": "...", "latest_version": 375, "latest_checksum": "...", "assets_root_hash": "..."},
  "principal": {"kind": "interactive | agent_run | tool_chat", "machine_id": "...", "project_id": "...", "execution_id": null, "session_id": null},
  "capabilities": {
    "postgres": {"mode": "direct", "dsn": "<scoped-role DSN>", "role_name": "...", "credential_generation": 3, "valid_until": 1754700000},
    "falkordb": {"mode": "direct", "host": "...", "port": 6379, "password": "..."},
    "qdrant": {"mode": "brokered", "operations": [{"name": "...", "method": "POST", "path": "..."}]},
    "embed": {"mode": "daemon"},
    "text_generate": {"mode": "daemon"},
    "tool_chat": {"mode": "daemon"},
    "vision_extract": {"mode": "unavailable"},
    "audio_transcribe": {"mode": "unavailable"},
    "broker_operations": [{"name": "...", "method": "POST", "path": "..."}]
  },
  "issued_at": 0, "expires_at": 0,
  "payload_checksum": "<sha256 over the canonical JSON payload>",
  "signature": "<hmac-sha256 over the canonical JSON payload, keyed by deployment_runtime.grant_signing_secret>"
}
```

Capabilities are strict tagged unions discriminated on `mode`. `direct` variants carry the complete typed connection material a constructor needs — `postgres`: `dsn`, `role_name`, `credential_generation`, `valid_until`; `falkordb`: `host`, `port`, `password`; `qdrant`: `url`, `api_key` — because 2.2 deletes every other source of that material. `brokered` variants carry their exact `operations` list. `unavailable` variants carry nothing. AI modalities are `daemon | unavailable` and never carry secrets. `api_contract` is the explicit daemon API version; `schema_identity` version fields are integers matching `expected_schema_identity()`. Cross-language golden serialization vectors under `tests/runtime_grants/golden/` cover every variant of every capability; Python round-trips them here and Rust consumes the same bytes in 2.1.

Issuance binds to exactly one configuration revision: the grant service captures a single ConfigRuntime active-bundle pointer per issuance, derives every capability and secret field from that one revision, and signs the observed revision into the grant as the first-class `config_revision` field. A failed secret rotation or a concurrent active-bundle swap can never produce a grant mixing capabilities from one revision with secrets from another. Renewal after a later activation issues from the new active bundle; grants issued under prior revisions stay valid until expiry and are never retro-invalidated by configuration changes alone. `config_revision` rides the wire contract end-to-end: the golden vectors carry it, the Rust model requires it (2.1), and the 1.3 runtime-config response returns the active revision so clients can enforce grant/settings revision coherence.

Schema identity is sourced from `expected_schema_identity()` at issuance. `payload_checksum` — sha256 over the same canonical JSON bytes the signature covers — is a first-class serialized field so clients can detect corruption on any load without holding the signing secret; the golden vectors pin it. The service module implements the full rejection matrix for presented grants: expired, invalid signature, wrong deployment token, wrong schema identity, wrong API contract, wrong/absent capability for the attempted operation, stale fencing epoch, and explicitly revoked — each a typed error with a distinct machine-readable code. The v1 `ServiceCapabilityBundle` models and `GET /api/config/service-capabilities` are removed in favor of the handshake (1.3); because #19645 2.2 rebuilds and pins that route first, its service-capabilities contract tests in `tests/servers/routes/test_configuration_effective_routes.py` are deleted in the same change. `GET /api/config/effective` remains operator-only for the web UI and diagnostics.

**Acceptance:**

- 1.2.1 - v2 bundle model exists with API contract version, deployment identity, integer-versioned schema identity, principal, tagged-union datastore + AI capabilities carrying complete direct connection material, epoch, expiry, and HMAC signature. file: `src/gobby/runtime_grants/schema.py`.
- 1.2.2 - Signing uses the deployment's stored secret; verification is daemon-side only. symbol: `sign_grant`.
- 1.2.3 - Each rejection class (expired, bad signature, wrong deployment, wrong schema, wrong API contract, wrong capability, stale epoch, revoked) returns its own typed code. test: `tests/runtime_grants/test_rejection_matrix.py::test_each_rejection_class_is_typed`.
- 1.2.4 - The v1 bundle models and the `GET /api/config/service-capabilities` route are gone, including the service-capabilities contract tests introduced by #19645. file: `src/gobby/servers/routes/configuration_effective.py`.
- 1.2.5 - Golden serialization vectors exist for every capability variant and round-trip byte-identically in Python. test: `tests/runtime_grants/test_golden_vectors.py::test_grant_vectors_round_trip`.
- 1.2.6 - One issuance observes one active configuration revision; a grant never mixes capabilities and secrets across revisions, including under concurrent activation and failed rotation. test: `tests/runtime_grants/test_active_config_binding.py::test_single_revision_per_grant`.
- 1.2.7 - `config_revision` is a signed first-class grant field carrying the exact observed revision, present in every golden vector. test: `tests/runtime_grants/test_golden_vectors.py::test_config_revision_signed`.
- 1.2.8 - `payload_checksum` is a first-class serialized field over the canonical payload, present and pinned in every golden vector. test: `tests/runtime_grants/test_golden_vectors.py::test_payload_checksum_pinned`.

### 1.3 Handshake endpoint and interactive principals [category: code] (depends: 1.2)
`kind: deliverable`

Targets:
- `src/gobby/servers/routes/runtime_handshake.py`
- `src/gobby/storage/managed_credentials.py::*` — scope-reason: credential manager gains interactive (machine, project) principals alongside per-execution principals
- `src/gobby/servers/auth_service.py::*` — scope-reason: capability matrix gains the handshake route; grant-presenting requests validate against the rejection matrix
- `src/gobby/code_index/gcode_gateway.py::*` — scope-reason: daemon-spawned gcode receives a pre-materialized grant file instead of relying on bootstrap.yaml DSN inheritance
- `src/gobby/gwiki_gateway.py::*` — scope-reason: same grant materialization for daemon-spawned gwiki
- `src/gobby/servers/routes/__init__.py::*` — scope-reason: route package export gains the handshake and runtime-config modules
- `src/gobby/servers/_app_routes.py::*` — scope-reason: application router aggregation includes the handshake and runtime-config routers
- `src/gobby/servers/routes/runtime_config.py`
- `tests/servers/routes/test_wiki_code_routes.py::*` — scope-reason: dormant CodeWiki route regressions extend to pin exact status codes and payloads across the router and auth changes
- `src/gobby/utils/local_token.py::*` — scope-reason: capability-token claims gain a signed machine_id across issuance and verification
- `src/gobby/agents/constants.py::*` — scope-reason: agent-run token issuance stamps machine identity into the claims
- `src/gobby/ai/_managed_tool_chat_lease.py::*` — scope-reason: tool-chat token issuance stamps machine identity into the claims
- `tests/servers/routes/test_runtime_handshake.py`
- `tests/servers/routes/test_runtime_config.py`
- `tests/storage/test_managed_credentials.py::*` — scope-reason: credential tests gain interactive binding, restart reuse, ciphertext-at-rest, and rotation-drain coverage
- `src/gobby/agents/code_index.py::*` — scope-reason: the isolated-agent gcode launcher stops extracting scoped DSNs and writing per-run bootstrap.yaml; it materializes the same signed managed grant as the gateways
- `tests/agents/test_isolation.py::*` — scope-reason: isolation tests assert grant-file permissions, principal binding, and cleanup instead of bootstrap.yaml shape

`POST /api/runtime/handshake` (new route module, exported from the routes package and included in the application router aggregation) accepts the operator bearer or a `gobby-agent-v1` capability token, plus `{machine_id, project_id}` in the body. It always issues a current-epoch v2 grant: for capability-token callers it reuses the execution's existing scoped role; for operator-token callers it mints or reuses the (machine, project) interactive role via `ManagedCredentialManager` (TTL 1h, same `gobby_agent_auth` machinery, `owner_kind="interactive"`). Renewal is re-invocation of the same endpoint — no separate renew route. Grant-presenting requests (brokered operations) are validated through the 1.2 rejection matrix. All three managed launchers — the gcode gateway, the gwiki gateway, and the isolated-agent code-index launcher (`src/gobby/agents/code_index.py`) — pre-materialize a grant file for the child process and set `GOBBY_MANAGED_EXECUTION_BOOTSTRAP` to its path, replacing DSN inheritance from `bootstrap.yaml`; the isolation launcher's scoped-DSN extraction and per-run `bootstrap.yaml` generation are deleted, and the isolation tests assert grant-file permissions, principal binding, and cleanup.

First contact is server-proven before credentials flow: `POST /api/runtime/handshake/challenge` accepts a client nonce with no authorization attached and returns an HMAC over that nonce keyed by the caller's own credential secret — the operator token for interactive callers; for managed callers, the capability token's signature, which the daemon recomputes from the presented claims without storing tokens. The client verifies the proof against the secret it already holds before attaching any bearer to any request, and the endpoint→deployment binding persists only after a subsequent authenticated handshake succeeds. A substituted listener on the configured port therefore receives a nonce and nothing else.

Managed children authenticate their own refresh: every managed launcher installs a run-scoped capability token (`GOBBY_AGENT_API_TOKEN`) in the child launch envelope whose claims equal the materialized grant's principal and whose expiry covers the child's deadline. Managed-source refresh presents that token — a stale or rotated managed grant is never its own authenticator, and a managed principal has no operator-token fallback; absent or principal-mismatched envelope tokens reject typed.

Interactive principals get first-class schema support in `gobby_agent_auth` (the complete DDL — owner-kind extension, unique `(deployment_token, machine_id, project_id)` active binding so two deployments sharing one database never collide, issue-or-reuse SQL, and the credential-material store — is owned and sealed by 1.1; this deliverable consumes it): issuance is issue-or-reuse with the same rotation, revocation, reconciliation, and inventory surface as execution principals, and reuse is real across process and daemon lifetimes — the daemon recovers the live generation's connection material from the 1.1 credential-material store, so a second handshake within TTL (or after a daemon restart) returns the same DSN; rotation and revocation atomically replace or remove stored material. The store implementation honors the 1.1 at-rest contract: material is sealed ciphertext-only under the daemon key envelope with AAD identity binding, the daemon is the sole reader, and retired generations are cleaned up. Grant validity is bounded: `expires_at <= min(role VALID UNTIL, capability-token expiry when present)` — a grant never advertises access its underlying credential cannot honor. The daemon serializes issue/reuse per principal key, so concurrent handshakes for one principal cannot interleave role mutation or emit generation-inverted grants.

Rotation drains, revocation cuts: when rotation supersedes a credential generation, the predecessor role stays valid until the latest grant issued against that generation expires, so an unexpired cached grant keeps working through the D4 outage window it was promised. Immediate invalidation is reserved for explicit revocation, and its typed code surfaces only where an authoritative signal exists: presentation to a reachable daemon rejects with the stable revoked code, distinct from expiry. During outage, backend-enforced invalidation surfaces as the ordinary datastore-authorization error — an offline client holds only a cached grant and backend credentials and has no authoritative signal to distinguish revocation from rotation or password failure.

Granted principals bind to verified bearer claims — equal or narrower, never wider. The capability-token claim model (`AgentApiTokenClaims` in `src/gobby/utils/local_token.py`) gains a signed `machine_id`, stamped by every issuer — `issue_agent_api_token` at the agent-spawn site (`src/gobby/agents/constants.py`) and `issue_tool_api_token` in the managed tool-chat lease — and verified during handshake; an absent or mismatched claim rejects typed. Capability-token callers receive principal fields derived from the token's execution binding; body `machine_id`/`project_id` must equal the verified claims or the handshake rejects typed. Operator-token callers bind `machine_id` to the daemon's verified local machine identity and `project_id` to a project the daemon admits. A presented grant must match the presenting bearer's claims. When `GOBBY_MANAGED_EXECUTION_BOOTSTRAP` names a managed grant source, that source is authoritative and fail-closed: acquisition failure is a typed error, never a fallback handshake under a different principal. The handshake response also carries authenticated deployment metadata — deployment token and current fencing epoch — which 2.1 uses to bind daemon endpoints to deployment identity before cache selection.

`GET /api/runtime/config` (new `runtime_config.py` module, registered through the same package export and router aggregation) serves registered non-capability runtime settings — the daemon-served configuration the deleted effective-config client used to carry — from the active ConfigRuntime snapshot to grant-presenting callers, and returns the active `config_revision` alongside the settings so the 2.1 client can enforce revision coherence with its held grant; the v2 grant remains the sole authority for capabilities and connection material. Operator and agent-run callers receive identical values with precedence pinned by tests.

The router and auth changes cross the dormant CodeWiki boundary without changing it: `GET /api/wiki/code/status` stays 200 with the exact `{"enabled": false, "state": "disabled", "reason": "pending_wiki_redesign"}` payload and `POST /api/wiki/code/refresh` stays 409, pinned by extended regressions in `test_wiki_code_routes.py`.

**Acceptance:**

- 1.3.1 - Handshake issues v2 grants to both operator-token and capability-token callers and is registered in the agent capability matrix. file: `src/gobby/servers/routes/runtime_handshake.py`.
- 1.3.2 - Interactive principals are scoped Postgres roles keyed (machine_id, project_id), reused across handshakes within TTL, revocable and rotatable via the existing manager surface. symbol: `ManagedCredentialManager.issue`.
- 1.3.3 - A handshake after a daemon restart returns a grant with the bumped fencing epoch, and a prior-epoch grant presented for a brokered operation is rejected typed. test: `tests/servers/routes/test_runtime_handshake.py::test_epoch_bump_rejects_prior_grants`.
- 1.3.4 - All three managed launchers launch children with pre-materialized grant files; no child reads `bootstrap.yaml` for a DSN. file: `src/gobby/gwiki_gateway.py`.
- 1.3.5 - `gobby_agent_auth` represents interactive owners with a unique `(deployment_token, machine_id, project_id)` active binding; same-key reuse, cross-project isolation, and same-database cross-deployment independence are tested. test: `tests/storage/test_managed_credentials.py::test_interactive_binding_uniqueness`.
- 1.3.6 - Issued grants never advertise validity beyond the underlying role's `VALID UNTIL`, and concurrent handshakes for one principal serialize daemon-side. test: `tests/servers/routes/test_runtime_handshake.py::test_expiry_bounded_and_serialized`.
- 1.3.7 - The handshake router is exported, included in the built application, and registered with its intended auth dependency. test: `tests/servers/routes/test_runtime_handshake.py::test_route_registered_in_app`.
- 1.3.8 - Granted principals are equal to or narrower than verified bearer claims; body/claims mismatches and managed-source acquisition failures reject typed (fail-closed) for every bearer kind. test: `tests/servers/routes/test_runtime_handshake.py::test_bearer_claim_binding_matrix`.
- 1.3.9 - Interactive issue-or-reuse returns the same live-generation DSN across handshakes and daemon restarts; rotation and revocation atomically replace stored material. test: `tests/storage/test_managed_credentials.py::test_interactive_reuse_after_restart`.
- 1.3.10 - `GET /api/runtime/config` serves registered non-capability settings from the active configuration snapshot to grant-presenting callers, with operator and agent-run precedence pinned. test: `tests/servers/routes/test_runtime_config.py::test_grant_presenting_config_transport`.
- 1.3.11 - Dormant CodeWiki route outputs are byte-identical after the routing changes: status stays 200 with the pinned payload, refresh stays 409. test: `tests/servers/routes/test_wiki_code_routes.py::test_dormant_outputs_pinned`.
- 1.3.12 - Capability-token claims carry a signed machine_id from every issuer; the handshake verifies it and rejects absent or mismatched values typed. test: `tests/servers/routes/test_runtime_handshake.py::test_machine_claim_binding`.
- 1.3.13 - Rotation drains predecessor generations until the last grant issued against them expires; explicit revocation invalidates early with its own typed code distinct from expiry, surfaced at reachable-daemon presentation. test: `tests/storage/test_managed_credentials.py::test_rotation_drains_predecessor_generations`.
- 1.3.14 - Interactive credential material is stored ciphertext-only with AAD identity binding; plaintext never persists and retired generations are removed. test: `tests/storage/test_managed_credentials.py::test_credential_material_ciphertext_at_rest`.
- 1.3.15 - `GET /api/runtime/config` returns the active config_revision with its settings snapshot. test: `tests/servers/routes/test_runtime_config.py::test_config_revision_in_response`.
- 1.3.16 - The bearer-free challenge endpoint proves daemon knowledge of the caller's credential secret over a client nonce for both bearer kinds; no credential attaches before proof succeeds. test: `tests/servers/routes/test_runtime_handshake.py::test_challenge_proof_before_bearer`.
- 1.3.17 - Every managed launcher installs a run-scoped capability token whose claims equal the grant principal and whose expiry covers the child deadline; managed refresh authenticates with it, and absent or mismatched envelope tokens reject typed with no operator fallback. test: `tests/servers/routes/test_runtime_handshake.py::test_managed_refresh_envelope_token`.
- 1.3.18 - The isolated-agent launcher materializes a signed managed grant, generates no per-run bootstrap.yaml and no scoped DSN, and isolation tests assert grant-file permissions, principal binding, and cleanup. test: `tests/agents/test_isolation.py::test_grant_file_isolation`.

## P2: Rust grant client and gated construction
`kind: framing`

**Goal**: One gcore grant client acquires, caches, validates, and renews grants; both binaries construct feature services only from grant material.

### 2.1 gcore grant client: handshake, cache, renewal, typed errors [category: code] (depends: P1)
`kind: deliverable`

Targets:
- `crates/gcore/src/grant/mod.rs`
- `crates/gcore/src/grant/bundle.rs`
- `crates/gcore/src/grant/cache.rs`
- `crates/gcore/src/grant/handshake.rs`
- `crates/gcore/src/ai/effective_config.rs::*` — scope-reason: managed-bundle fetch/validate machinery is replaced by the grant client as the single config entry
- `crates/gcore/src/local_token.rs::*` — scope-reason: token resolution feeds the handshake instead of per-request bearer attachment for grant acquisition
- `crates/gcore/src/lib.rs::*` — scope-reason: crate root declares and exports the grant module
- `crates/gcore/Cargo.toml`
- `crates/gcode/Cargo.toml`
- `crates/gwiki/Cargo.toml`
- `crates/gcore/src/config/machine_config.rs`
- `crates/gcore/src/config/mod.rs::*` — scope-reason: config module root declares the new machine-config client module
- `crates/gcore/src/grant/tests.rs`
- `crates/gcore/src/ai/effective_config/tests.rs::*` — scope-reason: managed-bundle fetch tests and their gcore.yaml fixtures are deleted with the replaced machinery

New `gobby_core::grant` module, declared in `lib.rs` and available to both binaries without enabling AI features — the handshake HTTP client moves out of the `ai`-only dependency set (Cargo feature wiring in all three manifests), because grants gate PostgreSQL construction, not just AI. Feature propagation is explicit: the handshake client's HTTP dependency (`ureq`) moves into gcore's base non-optional dependency set; gcode gains a default-on `ai` feature forwarding `gobby-core/ai` (its manifest stops hardwiring `ai` into the dependency line), and gwiki's existing `ai`/`embeddings-http` features remain the only activators of the AI stack. `cargo build --no-default-features` compiles both binaries with grant acquisition and datastore construction intact, and `cargo tree` assertions prove the AI dependency stack is absent from both no-default graphs (commands in E1). `GrantBundle` deserializes the v2 schema with `#[serde(deny_unknown_fields)]` and consumes the 1.2 golden vectors byte-identically. `acquire(project_root)` resolves in order: `GOBBY_MANAGED_EXECUTION_BOOTSTRAP` grant file → cache file `~/.gobby/grants/<deployment_token>/<project_id>.json` → handshake against `daemon_url()` authenticated by `read_local_cli_token()`. Cache identity binds to the daemon that owns it, never to client-local filesystem state alone: the client persists a trusted endpoint→deployment binding, derived from the authenticated deployment metadata in every handshake response (1.3), alongside the grant caches. Endpoints are loopback-only (Constraints: loopback-only first contact): before attaching any bearer, the client rejects a non-loopback `daemon_url()` typed with `GrantError::RemoteEndpoint` — there is no credential-bearing first contact to a remote endpoint. Loopback is a precondition, never server proof: on any endpoint without a trusted binding, the client runs the 1.3 bearer-free challenge-response and verifies the daemon's proof against its own credential secret before attaching any bearer; a substituted loopback listener receives a nonce and no credential, and a fabricated grant it might return is never trusted because binding persistence requires the verified proof plus a subsequent authenticated handshake. For the default local endpoint, the expected deployment token still derives from canonical `GOBBY_HOME` with the same algorithm as Python's `deployment_token(data_root)` (cross-language golden-tested) and is verified against handshake metadata; for an overridden local endpoint (non-default port) with no trusted binding yet, handshake precedes cache selection and the returned deployment token selects the cache path. An endpoint whose advertised deployment changes rebinds only through a successful authenticated handshake; during daemon outage, cache selection uses the persisted trusted binding. The project UUID reads locally from `.gobby/project.json`.

Validation on every load binds the grant to its expected identity and source: `version == 2`, the canonical-payload checksum verifies (corruption fails `Malformed` before any construction, on every source — managed file, cache, and handshake response), wall-clock vs `expires_at`, schema identity equals the embedded `schema_identity()`, deployment token matches the cache path, project and machine match local state, and principal kind matches the source — a managed bootstrap file must carry an `agent_run`/`tool_chat` principal matching the execution identity and is consumed in place; only interactive grants are ever written to the cache, so managed grants never overwrite the interactive project cache. Renewal: when remaining TTL < 50% and the daemon is reachable, re-handshake and atomically rewrite the cache (0600, temp + rename) under a per-cache interprocess lock. Contention is bounded and never blocks a valid invocation: proactive renewal takes the lock zero-wait — when another process holds it, the invocation proceeds immediately on its unexpired grant while the holder renews; the holder re-reads the cache after acquisition before handshaking; lock, connect, and request share one bounded deadline; a crashed holder's stale lock is taken over by age. Mandatory refresh (expired grant with a reachable daemon, stale-epoch retry) shares the same deadline and surfaces `GrantError::Timeout` on exhaustion. Replacement is generation-aware and never overwrites a newer `credential_generation` with an older one. Refresh destination follows the source: interactive renewal locks and atomically rewrites the interactive cache under the interactive principal; managed-source refresh (stale-epoch or rotated-signature recovery under `GOBBY_MANAGED_EXECUTION_BOOTSTRAP`) locks and atomically replaces the managed grant file itself under the same execution principal, authenticated by the launch-envelope capability token (1.3) — a managed principal never falls back to the operator token, and an expired envelope or principal mismatch fails typed. A managed refresh never touches the interactive cache, never changes principal kind, and never downgrades generation — the two flows share the lock discipline and deadlines but own disjoint destinations. Outage policy (decision D4): an unexpired grant is authorization — datastore construction proceeds without a daemon roundtrip; expired grant + unreachable daemon yields `GrantError::DaemonRequired`. Every load also gates the daemon API contract: `api_contract` must equal the client's embedded expected contract on every acquisition path — managed file, cache, handshake response, offline load, and pre-request revalidation — so an old client refuses a newer daemon's grant before constructing services; the 1.2 golden vectors include old-client/new-grant cases. All failures are typed (`GrantError` enum: `DaemonRequired`, `Expired`, `SchemaMismatch`, `DeploymentMismatch`, `ApiContractMismatch`, `RemoteEndpoint`, `ConfigRevisionMismatch`, `Revoked`, `Timeout`, `Malformed`, `Io`), each with the stable CLI mapping from 2.2 — no silent fallback of any kind. `Revoked` is producible only from reachable-daemon presentation (1.3): offline paths never synthesize it.

A separate non-authorizing `inspect_cached_grant(project_root)` classifies the cache as absent/malformed/valid/expiring/expired without exposing connection material and without performing a handshake; diagnostic commands (4.2) use it instead of `acquire`.

A sibling machine-config client (new `crates/gcore/src/config/machine_config.rs`, declared in the config module root) fetches registered non-capability runtime settings from `GET /api/runtime/config` (1.3) with grant presentation, replacing the deleted effective-config fetch as the transport for daemon-served settings; 3.1's AiContext and generation-profile resolution consume it. Capabilities and connection truth stay grant-only. One invocation never mixes configuration revisions: the grant and its settings cache and replace as one revision-coherent unit — the settings response's `config_revision` must equal the held grant's; a mismatch (a later activation) triggers one synchronized re-handshake under the per-cache lock replacing both, mirroring the 5.1 stale-epoch recovery. Every interleaving has a bounded terminal outcome: if the refetched settings still disagree with the replaced grant's revision (back-to-back activations), the client preserves the prior coherent cached pair and fails typed `ConfigRevisionMismatch` with its stable CLI mapping — it never mixes revisions and never retries beyond the single synchronized re-handshake. During daemon outage the cached pair serves through grant expiry, so a cold start under outage reconstructs one coherent revision or fails typed.

**Acceptance:**

- 2.1.1 - Grant client resolves managed file → cache → handshake, validates structurally on every load, and caches at 0600 with atomic replace. file: `crates/gcore/src/grant/cache.rs`.
- 2.1.2 - Renewal triggers past half-TTL when the daemon is reachable and never blocks an invocation holding an unexpired grant. symbol: `GrantBundle`.
- 2.1.3 - Schema-identity mismatch between binary and grant fails typed before any datastore connection. test: `crates/gcore/src/grant/tests.rs::schema_mismatch_refuses_construction`.
- 2.1.4 - Expired grant with unreachable daemon yields `DaemonRequired`; unexpired grant with unreachable daemon permits datastore paths and refuses AI paths. test: `crates/gcore/src/grant/tests.rs::outage_window_semantics`.
- 2.1.5 - The grant module is exported from the crate root; both binaries build with `--no-default-features` with grant acquisition and datastore construction intact, and cargo-tree assertions prove the AI dependency stack is absent from those graphs. file: `crates/gcore/src/lib.rs`.
- 2.1.6 - Cache location derives from local state with cross-language deployment-token parity; loads validate deployment, project, machine, principal kind, and source; managed grants are never written to the interactive cache. test: `crates/gcore/src/grant/tests.rs::managed_grant_never_overwrites_interactive_cache`.
- 2.1.7 - Concurrent renewals serialize on the per-cache lock and never replace a newer credential generation with an older one. test: `crates/gcore/src/grant/tests.rs::concurrent_renewal_refuses_downgrade`.
- 2.1.8 - `inspect_cached_grant` classifies absent/malformed/valid/expiring/expired without authorizing construction or exposing secrets. test: `crates/gcore/src/grant/tests.rs::inspect_is_non_authorizing`.
- 2.1.9 - Rust deserializes the 1.2 golden vectors byte-identically for every capability variant. test: `crates/gcore/src/grant/tests.rs::golden_vectors_match_python`.
- 2.1.10 - Every acquisition path validates `api_contract` against the client's expected contract; mismatch yields `ApiContractMismatch` with its stable CLI mapping; golden vectors cover old-client/new-grant. test: `crates/gcore/src/grant/tests.rs::api_contract_gate`.
- 2.1.11 - Cache selection follows the trusted endpoint→deployment binding: local derivation verified against handshake metadata, handshake-before-cache on unbound endpoints, rebind only via authenticated handshake, persisted binding under outage. test: `crates/gcore/src/grant/tests.rs::endpoint_deployment_binding`.
- 2.1.12 - Renewal contention is bounded: zero-wait try-lock with immediate serve of the unexpired grant, shared deadline, post-acquisition re-read, stale-lock takeover, and typed `Timeout` on exhaustion. test: `crates/gcore/src/grant/tests.rs::bounded_renewal_contention`.
- 2.1.13 - The machine-config client fetches registered non-capability settings with grant presentation; capabilities and connection material remain grant-only. file: `crates/gcore/src/config/machine_config.rs`.
- 2.1.14 - Non-loopback daemon URLs are rejected typed (`RemoteEndpoint`) before any credential attaches; overridden local ports handshake normally. test: `crates/gcore/src/grant/tests.rs::remote_endpoint_refused_before_auth`.
- 2.1.15 - Managed-source refresh locks and replaces the managed grant file under the same execution principal; interactive refresh owns the interactive cache; the destinations never cross under concurrency. test: `crates/gcore/src/grant/tests.rs::refresh_destination_by_source`.
- 2.1.16 - Grant and machine-config settings cache and replace as one revision-coherent unit; a revision mismatch triggers exactly one synchronized re-handshake replacing both; a cold start under outage serves the cached pair or fails typed. test: `crates/gcore/src/grant/tests.rs::config_revision_coherence`.
- 2.1.17 - Every acquisition source verifies the canonical-payload checksum before construction; corrupted payloads and corrupted checksums fail `Malformed` for both offline sources. test: `crates/gcore/src/grant/tests.rs::corrupt_grant_refused_offline`.
- 2.1.18 - A second revision mismatch after the single re-handshake preserves the prior coherent pair and fails typed `ConfigRevisionMismatch`; a barrier test pins back-to-back activations. test: `crates/gcore/src/grant/tests.rs::config_revision_second_mismatch_terminal`.
- 2.1.19 - On endpoints without a trusted binding the challenge proof precedes any bearer; a substituted loopback listener receives neither bearer nor trusted binding nor accepted grant. test: `crates/gcore/src/grant/tests.rs::substituted_listener_gets_no_bearer`.
- 2.1.20 - Managed-source refresh authenticates with the launch-envelope token; expired envelopes and principal mismatches fail typed with no operator-token fallback. test: `crates/gcore/src/grant/tests.rs::managed_refresh_envelope_auth`.

### 2.2 Gate service construction; collapse DSN resolution [category: code] (depends: 2.1)
`kind: deliverable`

Targets:
- `crates/gcode/src/db/resolution.rs::*` — scope-reason: multi-source DSN resolution collapses to the grant client
- `crates/gcode/src/db/mod.rs::*` — scope-reason: connect paths take grant-resolved DSNs only
- `crates/gcode/src/config/context.rs::*` — scope-reason: Context::resolve acquires the grant before any service handle exists
- `crates/gcode/src/config/services.rs::*` — scope-reason: env-beats-daemon service layering and env DSN reads are removed
- `crates/gcode/src/config/layers.rs::*` — scope-reason: ServiceSource layers collapse to grant material
- `crates/gcode/src/codewiki_facts/mod.rs::*` — scope-reason: facade open/read_connection consume the grant-resolved context without widening the facade API
- `crates/gwiki/src/support/env.rs::*` — scope-reason: gwiki DSN resolution moves to the grant client, closing the managed-bootstrap asymmetry
- `crates/gwiki/src/support/postgres.rs::*` — scope-reason: connection construction takes grant-resolved DSNs
- `crates/gwiki/src/support/services.rs::*` — scope-reason: runtime service resolution is grant-gated
- `crates/gcore/src/bootstrap.rs::*` — scope-reason: database_url reading from bootstrap.yaml is removed from client crates; endpoint discovery remains
- `crates/gwiki/tests/code_engine_boundary.rs::*` — scope-reason: boundary assertions extend to forbid non-grant DSN resolution in the moved engine
- `crates/gcode/src/project.rs::*` — scope-reason: --project name resolution moves to an authenticated daemon lookup; no datastore query precedes a grant
- `crates/gcode/tests/grant_errors.rs`
- `src/gobby/servers/routes/code_index.py::*` — scope-reason: operator-only global prune trigger route joins the code-index route surface
- `src/gobby/code_index/prune.py::*` — scope-reason: global prune orchestration decomposes into a hub-record sweep plus per-project reconciliation children carrying project grants
- `tests/servers/routes/test_code_index_prune_route.py`
- `tests/servers/test_auth_service.py::*` — scope-reason: auth-matrix tests gain the operator-only projects/prune route assertions
- `crates/gcode/src/commands/status/prune.rs::*` — scope-reason: global prune implementation becomes a thin client of the operator-only daemon route
- `crates/gcode/src/commands/status/prune/inventory.rs::*` — scope-reason: direct-context prune inventory moves behind the daemon route or is deleted
- `crates/gcode/src/commands/status/prune/reconcile.rs::*` — scope-reason: direct-context reconciliation moves behind the daemon route or is deleted
- `crates/gcode/src/commands/status/prune/tests.rs::*` — scope-reason: prune tests rewrite against the daemon-client path
- `crates/gcode/src/commands/status/projects.rs::*` — scope-reason: global projects listing converts to the operator-only daemon listing client
- `crates/gcode/src/commands/status/shared.rs::*` — scope-reason: the shared direct-DSN service-context helper for global commands is deleted
- `crates/gcode/src/dispatch.rs::*` — scope-reason: global prune and projects dispatch routes to typed daemon clients

`resolve_database_url` and gwiki's `database_url_from_sources` are replaced by grant acquisition: the scoped-role DSN in `capabilities.postgres` is the only DSN either binary uses. Removed outright: `GCODE_DATABASE_URL`, `GWIKI_DATABASE_URL`, `GOBBY_POSTGRES_DSN`, `GOBBY_FALKORDB_{HOST,PORT,PASSWORD}`, `GOBBY_QDRANT_{URL,API_KEY}` env reads, `daemon_dsn()` fetching, and `bootstrap.yaml` `database_url` reads from client crates (`bootstrap.yaml` remains for daemon endpoint discovery only). FalkorDB/Qdrant connection material comes from the grant's capability entries, honoring `direct | brokered | unavailable`. Feature-service construction sites in both binaries fail typed before constructing anything when no valid grant resolves.

Every dispatch path is classified by how it obtains authorization scope before legacy resolution is deleted:

| Dispatch | Scope resolution | Datastore access |
|---|---|---|
| path / `.gobby/project.json` (default) | local project UUID → project grant | project-scoped role |
| `--project <name>` | authenticated daemon name→ID lookup, then handshake | project-scoped role |
| `gcode projects` | operator-only daemon broker operation | none (broker) |
| global `gcode prune` | operator-only daemon broker operation | none (broker) |
| `gcode prune --project <root>` | local project root → project grant | project-scoped role |
| outside any project, no `--project` | typed rejection before any datastore or non-operator daemon call | none |

Global prune has an explicit route and authorization contract: `POST /api/code-index/prune` is a new operator-only daemon route — grant-exempt by design, because deployment-wide maintenance is operator authority, the same class as `GET /api/config/effective`; capability-token and anonymous calls reject typed. The daemon decomposes global prune into a hub-level stale-project-record sweep under its own database authority plus per-project projection reconciliation through the existing gateway and prune orchestration (`src/gobby/code_index/prune.py`), where every spawned child carries an ordinary project-scoped managed grant — no cross-project grant principal exists. The decomposition is failure-isolated and idempotent: the daemon snapshots the target project set before any deletion; projections reconcile first and a stale project's hub records are deleted only after its graph and vector cleanup completes, so no deletion order can orphan a projection; child fan-out runs under a bounded concurrency limit with a per-child deadline; a failed or timed-out child records a durable per-project dirty/retry marker through the existing prune-dirty machinery instead of aborting the sweep; and the route returns a structured outcome listing completed, failed, and skipped projects. A retried prune converges — already-pruned projects are no-ops and dirty markers drive the remainder. Global `gcode prune` becomes a thin client of that route; `gcode prune --project` keeps the local project-grant path. `gcode projects` calls the existing operator-only projects listing route. The client implementations convert with the route: `commands/status/prune.rs` and its inventory/reconcile/tests submodules plus `commands/status/projects.rs` stop constructing direct PostgreSQL service contexts, the shared direct-DSN helper in `commands/status/shared.rs` is deleted, and `dispatch.rs` routes both global commands through the typed daemon clients while project-scoped dispatch keeps the grant path.

No command touches PostgreSQL, FalkorDB, or Qdrant before a grant resolves. Typed grant failures carry a stable public CLI contract: every `GrantError` and rejection-matrix class maps to a fixed JSON error code, human-readable message, and exit status in both binaries, pinned by contract snapshots and invocation tests.

**Acceptance:**

- 2.2.1 - gcode resolves its DSN exclusively through the grant client; the env/daemon/bootstrap/gcore.yaml resolution ladder is gone. file: `crates/gcode/src/db/resolution.rs`.
- 2.2.2 - gwiki resolves identically, including honoring managed grant files. file: `crates/gwiki/src/support/env.rs`.
- 2.2.3 - `CodewikiFacts` connections come from the grant-resolved context; the facade API is unchanged. symbol: `CodewikiFacts`.
- 2.2.4 - The boundary test additionally forbids env-DSN and bootstrap-DSN resolution inside the moved engine. test: `crates/gwiki/tests/code_engine_boundary.rs::moved_engine_uses_only_facade`.
- 2.2.5 - With no grant and no daemon, both binaries fail with the typed daemon-required error before touching any datastore. behavior: "daemon required" in `docs/guides/ai-configuration.md`.
- 2.2.6 - Default path/project-file dispatch resolves the local project UUID to a project grant; no pre-grant datastore access exists on any dispatch path. test: `crates/gcode/tests/grant_errors.rs::no_pregrant_datastore_access`.
- 2.2.7 - Every grant error class has a stable JSON code, message, and exit status in both CLIs. test: `crates/gcode/tests/grant_errors.rs::grant_errors_stable_contract`.
- 2.2.8 - `--project <name>` resolves through the authenticated daemon lookup and then the handshake; the lookup precedes every datastore touch. test: `crates/gcode/tests/grant_errors.rs::project_name_lookup_authenticated`.
- 2.2.9 - `gcode projects` dispatches to the operator-only daemon listing; capability-token and anonymous calls reject typed. test: `tests/servers/test_auth_service.py::test_projects_listing_operator_only`.
- 2.2.10 - Global `gcode prune` triggers `POST /api/code-index/prune` as an operator-only route; the daemon decomposes it into the hub-record sweep plus per-project children carrying project-scoped grants; capability-token calls reject typed. test: `tests/servers/routes/test_code_index_prune_route.py::test_global_prune_operator_only`.
- 2.2.11 - `gcode prune --project` resolves through the ordinary project-grant path. test: `crates/gcode/tests/grant_errors.rs::project_prune_uses_project_grant`.
- 2.2.12 - Dispatch outside any project with no `--project` rejects typed before any datastore access or non-operator daemon call. test: `crates/gcode/tests/grant_errors.rs::projectless_rejection`.
- 2.2.13 - Global prune snapshots before deleting, reconciles projections before hub-record removal, bounds child concurrency and deadlines, records durable dirty/retry markers on child failure or timeout, and returns structured per-project outcomes; an idempotent retry converges. test: `tests/servers/routes/test_code_index_prune_route.py::test_partial_failure_recovery`.
- 2.2.14 - The global prune and projects CLI implementations and the shared direct-DSN helper contain no direct datastore construction; both global commands dispatch through typed daemon clients. test: `crates/gcode/src/commands/status/prune/tests.rs::global_prune_uses_daemon_client`.

## P3: Daemon-only AI
`kind: framing`

**Goal**: Every AI operation routes through daemon contracts; capability truth comes from the grant; Direct/Auto arms, probes, and vendor-key harvesting are deleted.

### 3.1 Collapse gcore AI routing to daemon-only [category: code] (depends: 2.1)
`kind: deliverable`

Targets:
- `crates/gcore/src/ai/mod.rs::*` — scope-reason: AiRouting collapses to Daemon|Off; resolve/fallback/observed-route machinery is rewritten around grant capabilities
- `crates/gcore/src/ai/text.rs::*` — scope-reason: file deleted (direct text-generation arm)
- `crates/gcore/src/ai/embeddings.rs::*` — scope-reason: direct embedding arms removed; daemon path remains
- `crates/gcore/src/ai/vision.rs::*` — scope-reason: file deleted (direct vision arm)
- `crates/gcore/src/ai/transcription.rs::*` — scope-reason: file deleted (direct transcription arm)
- `crates/gcore/src/ai/generation/one_shot.rs::*` — scope-reason: direct one-shot generation arm deleted
- `crates/gcore/src/ai/generation/transport.rs::*` — scope-reason: DirectChatTransport is deleted; daemon transport remains
- `crates/gcore/src/ai/generation/profile.rs::*` — scope-reason: default_env_api_key vendor harvesting is deleted
- `crates/gcore/src/ai/probe.rs::*` — scope-reason: file deleted (probe-based capability discovery)
- `crates/gcore/src/ai_context.rs::*` — scope-reason: AiContext resolves modality availability from grant capabilities instead of probed/layered config
- `crates/gcore/src/config/types.rs::*` — scope-reason: AiRouting enum and FeatureCandidate direct-target parsing shrink to daemon-only forms
- `crates/gcore/src/config/daemon_source.rs::*` — scope-reason: local routing-override merging is removed; grant capabilities are authoritative
- `crates/gcore/src/ai/generation/mod.rs::*` — scope-reason: DirectChatTransport re-export and direct-generation module docs removed
- `crates/gcore/src/ai/generation/tool_loop.rs::*` — scope-reason: transport seam references the daemon transport only
- `crates/gcore/src/ai/generation/tier.rs::*` — scope-reason: gcore.yaml-backed tier lookup collapses to grant/daemon config
- `crates/gcore/src/ai/generation/tests/common.rs::*` — scope-reason: direct-transport test fixtures rewritten daemon-only
- `crates/gcore/src/ai/generation/tests/one_shot.rs::*` — scope-reason: direct/auto route tests deleted with the variants
- `crates/gcore/src/ai/generation/tests/transport.rs::*` — scope-reason: DirectChatTransport tests deleted
- `crates/gcore/src/ai/generation/tests/profile.rs::*` — scope-reason: vendor env-key tests deleted with default_env_api_key
- `crates/gcode/src/vector/code_symbols/embedding.rs::*` — scope-reason: direct embedding source arm removed; daemon embedding remains
- `crates/gcode/src/commands/vector.rs::*` — scope-reason: vector lifecycle keeps forced daemon routing without direct arms
- `crates/gcode/src/commands/embeddings_doctor.rs::*` — scope-reason: doctor reads modality availability from the grant
- `crates/gwiki/src/commands/generation_routes.rs::*` — scope-reason: DirectChatTransport construction replaced by the daemon transport
- `crates/gwiki/src/commands/code/text/generation/tool_loop.rs::*` — scope-reason: direct transport arm removed from the codewiki generation loop
- `crates/gcode/src/commands/search.rs::*` — scope-reason: search command surfaces the structured semantic-degradation warning during daemon outage
- `crates/gcode/src/search/mod.rs::*` — scope-reason: silent empty-source semantic degradation is replaced by the explicit degraded-lane contract
- `crates/gwiki/src/ai/clients.rs::*` — scope-reason: transcribe, one-shot, and vision route matches rewritten daemon-only
- `crates/gwiki/src/benchmark.rs::*` — scope-reason: direct semantic-embedding source arm removed
- `crates/gwiki/src/commands/ask/deep.rs::*` — scope-reason: direct-route arm and direct-fallback annotation removed
- `crates/gwiki/src/commands/ask/synthesis.rs::*` — scope-reason: direct-route synthesis arm and route labels removed
- `crates/gwiki/src/commands/citation_quality.rs::*` — scope-reason: routing gate collapses to Daemon|Off
- `crates/gwiki/src/commands/citation_quality/contradictions.rs::*` — scope-reason: direct-target derivation removed
- `crates/gwiki/src/commands/code/frontmatter.rs::*` — scope-reason: direct-fallback generation-status annotation removed
- `crates/gwiki/src/commands/code/tests/ai.rs::*` — scope-reason: direct-outcome tests rewritten daemon-only
- `crates/gwiki/src/commands/code/text/generation/one_shot.rs::*` — scope-reason: gwiki direct one-shot arm and direct-target resolution removed
- `crates/gwiki/src/commands/code/text/generation/routing.rs::*` — scope-reason: gcore.yaml-backed feature-route resolution collapses to daemon config
- `crates/gcode/src/config/services.rs::*` — scope-reason: AiRouting service-binding match collapses to the two-variant enum
- `crates/gwiki/src/ingest/audio.rs::*` — scope-reason: transcription route gating and direct arms collapse to Daemon|Off
- `crates/gwiki/src/ingest/file/dispatch.rs::*` — scope-reason: ingest dispatch routing checks collapse to the two-variant enum
- `crates/gwiki/src/ingest/image.rs::*` — scope-reason: image description routing match and Auto construction are removed
- `crates/gwiki/src/ingest/session/connections.rs::*` — scope-reason: session-connection AI gating drops Direct matching
- `crates/gwiki/src/ingest/session/summarize.rs::*` — scope-reason: session-summarize routing gate drops Direct matching
- `crates/gwiki/src/ingest/video/mod.rs::*` — scope-reason: video ingest routing gate collapses to Daemon|Off
- `crates/gwiki/src/sources/types.rs::*` — scope-reason: routing parser and route labels shrink to the two-variant enum
- `crates/gwiki/src/sources/tests.rs::*` — scope-reason: per-source routing-override tests rewrite for the collapsed enum
- `crates/gwiki/src/transcribe.rs::*` — scope-reason: degradation-reason mapping drops the Auto and Direct arms
- `crates/gwiki/src/vision.rs::*` — scope-reason: degradation-reason mapping drops the Auto and Direct arms
- `crates/gcore/src/ai/tests.rs`

Delete files: `crates/gcore/src/ai/text.rs`, `crates/gcore/src/ai/vision.rs`, `crates/gcore/src/ai/transcription.rs`, `crates/gcore/src/ai/generation/one_shot.rs` direct arms, `crates/gcore/src/ai/probe.rs` (paths listed as Targets are removed or reduced to daemon-only forms). `AiRouting` becomes `{Daemon, Off}`; `Off` is reachable only by explicit user opt-out (3.2). Modality availability reads `capabilities.{embed,text_generate,tool_chat,vision_extract,audio_transcribe}.mode` from the grant. `default_env_api_key` (ANTHROPIC/OPENAI/OPENROUTER/GROQ env harvesting) is deleted. An AI call with an unreachable daemon returns a typed error naming the modality; no fallback, no silent Off.

One pinned exception resolves the search/outage contract: hybrid `gcode search` under a daemon outage with an unexpired grant degrades explicitly — the lexical and graph lanes run over direct connections, the semantic lane (whose query embedding is a daemon AI call) is skipped, and the command emits a structured warning naming the degraded lane and its cause in both JSON (`warnings`) and human output. Today's silent empty-source degradation is deleted; explicit AI commands keep the typed hard failure. 6.1 pins the exact degraded output.

Every workspace consumer of the deleted variants and transports is owned here, from the full `gcode grep "AiRouting::Direct|AiRouting::Auto|DirectChatTransport" crates` inventory: gcode's direct embedding arm, vector/doctor paths, and service-binding match (`config/services.rs`); gwiki's AI clients (`ai/clients.rs`), benchmark embedding arm, ask deep/synthesis route arms, citation-quality gates and contradictions direct targets, code frontmatter fallback annotation, codewiki one-shot/routing/tool-loop generation paths, and generation-route constructors; gwiki's ingest routing surface — audio transcription gating, file dispatch, image description, session connections and summarize, video — plus the sources routing parser and labels (`sources/types.rs` and tests) and the transcribe/vision degradation-reason maps; gcore's generation module re-exports and their test submodules. Each consumer is classified as daemon-routed, Off-respecting, or deleted (gcode `symbols.rs` and gwiki `api.rs`/`cli/tests.rs` routing surfaces die with their 3.2 flag removals); the E1 zero-match audit proves no `AiRouting::Direct`, `AiRouting::Auto`, `DirectChatTransport`, or vendor-key reference survives in the workspace.

**Acceptance:**

- 3.1.1 - `AiRouting` has exactly Daemon and Off variants; Auto and Direct are unrepresentable. symbol: `AiRouting`.
- 3.1.2 - Direct transports, one-shot direct generation, and probe-based discovery are deleted. file: `crates/gcore/src/ai/probe.rs`.
- 3.1.3 - No client crate reads vendor API-key environment variables. test: `crates/gcore/src/ai/tests.rs::no_vendor_env_key_reads`.
- 3.1.4 - Modality gating reads grant capabilities; a grant lacking a modality yields the typed unavailable error without an HTTP roundtrip. test: `crates/gcore/src/ai/tests.rs::grant_gates_modalities`.
- 3.1.5 - Every workspace consumer of the removed variants and transports is migrated or deleted; the workspace zero-match audit for direct/auto routing, DirectChatTransport, and vendor key names passes. file: `crates/gcore/src/ai/generation/mod.rs`.
- 3.1.6 - Hybrid search during a daemon outage returns lexical and graph results with a structured semantic-degradation warning; the silent empty-source degrade path is gone; explicit AI commands still fail typed. test: `crates/gcode/src/commands/search.rs::outage_degrades_with_warning`.

### 3.2 Remove CLI routing surfaces; bump contracts; deterministic outline [category: code] (depends: 3.1)
`kind: deliverable`

Targets:
- `crates/gwiki/src/cli.rs::*` — scope-reason: --ai/--transcription-routing/--vision-routing/--text-routing/--require-ai flag families are removed across commands
- `crates/gwiki/src/cli/code.rs::*` — scope-reason: direct-target candidate flags (--ai-aggregate-candidate et al.) lose provider/api_base forms
- `crates/gwiki/src/daemon.rs::*` — scope-reason: probe-based endpoint discovery is deleted
- `crates/gwiki/src/contract.rs::*` — scope-reason: flag-surface removal regenerates the snapshot; the 16→17 version bump lands in 4.1
- `crates/gcode/src/cli.rs::*` — scope-reason: outline --summarize flag is removed
- `crates/gcode/src/commands/symbols.rs::*` — scope-reason: summarize_outline and its AI context resolution are deleted; outline is structural only
- `crates/gcode/src/dispatch.rs::*` — scope-reason: outline dispatch drops the summarize arm
- `crates/gcode/src/contract.rs::*` — scope-reason: flag-surface removal regenerates the snapshot; the 3→4 version bump lands in 4.1
- `crates/gwiki/tests/code_engine_boundary.rs::*` — scope-reason: AiRouting assertions update to the two-variant enum
- `docs/guides/ai-configuration.md::*` — scope-reason: routing contract documentation rewritten for daemon-only reality
- `crates/gwiki/src/api.rs::*` — scope-reason: programmatic API routing fields (transcription/vision/text routing options) removed with the flag families
- `crates/gwiki/src/cli/tests.rs::*` — scope-reason: routing-flag CLI tests deleted with the removed surfaces

`--no-ai` survives on gwiki as the explicit opt-out mapping to `AiRouting::Off`; every other routing flag is removed, including the programmatic mirrors — gwiki's `api.rs` options lose their transcription/vision/text routing fields and the routing-flag CLI tests go with the surfaces. `gcode outline` loses `--summarize` and its 1 MiB prompt path entirely — outline output is deterministic structure only. Conformance snapshots regenerate with the removed flag surface; the single final version bump and snapshot-parity pass land in 4.1 after the last CLI removal (Constraints). The boundary test's routing assertions track the collapsed enum. `docs/guides/ai-configuration.md` is rewritten: routing is daemon-only, capability truth is the grant, `--no-ai` is the only user-facing switch.

**Acceptance:**

- 3.2.1 - gwiki exposes no routing flags beyond `--no-ai` in its source and in-memory CLI surface; the final canonical snapshot, version bump, and mirror parity are owned solely by 4.1.5. file: `crates/gwiki/src/contract.rs`.
- 3.2.2 - `gcode outline` has no summarize surface in its source and in-memory CLI definition; the final canonical snapshot, version bump, and mirror parity are owned solely by 4.1.5. file: `crates/gcode/src/contract.rs`.
- 3.2.3 - gwiki's probe module is deleted and no status-route body parsing remains for availability decisions. file: `crates/gwiki/src/daemon.rs`.
- 3.2.4 - Documentation describes the daemon-only contract including outage semantics. behavior: "daemon-only AI routing" in `docs/guides/ai-configuration.md`.

## P4: Standalone removal
`kind: framing`

**Goal**: Standalone runtime configuration, local credential ownership, and daemon-optional feature modes cease to exist in the client crates.

### 4.1 Remove standalone mode and local credential ownership [category: code] (depends: P3, 2.2)
`kind: deliverable`

Targets:
- `crates/gcore/src/runtime_mode.rs::*` — scope-reason: file deleted (single runtime mode remains)
- `crates/gcore/src/provisioning/mod.rs::*` — scope-reason: module deleted whole — StandaloneConfig, gcore.yaml parsing, the LM-Studio derivation, and the embedded compose template; the Python daemon's installer surface is the sole provisioning owner
- `crates/gcore/src/setup.rs::*` — scope-reason: StandaloneSetup and standalone validators are deleted
- `crates/gcore/src/secrets.rs::*` — scope-reason: client-side KEK unwrap and $secret: resolution leave the client crates entirely
- `crates/gcode/src/commands/setup.rs::*` — scope-reason: gcode setup --standalone command surface is deleted
- `crates/gcode/src/setup/types.rs::*` — scope-reason: StandaloneSetupRequest types are deleted
- `crates/gcode/src/project.rs::*` — scope-reason: project resolution drops standalone-mode branches
- `crates/gwiki/src/support/config.rs::*` — scope-reason: HubPrimary None-degradation and gcore.yaml secret resolution are removed
- `crates/gcore/src/config/resolve.rs::*` — scope-reason: ConfigSource keeps its trait shape; standalone implementors and $secret:/env-pattern resolution arms are deleted
- `crates/gcode/src/cli.rs::*` — scope-reason: standalone setup flag family removed
- `crates/gcode/src/dispatch.rs::*` — scope-reason: setup command dispatch removed
- `crates/gcode/src/lib.rs::*` — scope-reason: setup module declarations and the StandaloneConfig service assertion are removed
- `crates/gcode/src/setup/contracts.rs::*` — scope-reason: standalone setup contract surface deleted with the setup tree
- `crates/gcode/src/setup/ddl.rs::*` — scope-reason: client-owned DDL provisioning deleted; schema application belongs to gdaemon
- `crates/gcode/src/setup/identifiers.rs::*` — scope-reason: standalone identifier helpers deleted with the setup tree
- `crates/gcode/src/setup/postgres.rs::*` — scope-reason: client-side Postgres provisioning deleted
- `crates/gcode/src/setup/tests.rs::*` — scope-reason: standalone setup tests deleted
- `crates/gwiki/src/commands/setup.rs::*` — scope-reason: gwiki standalone setup command deleted
- `crates/gwiki/src/commands/mod.rs::*` — scope-reason: command declarations drop the setup dispatch
- `crates/gwiki/src/cli.rs::*` — scope-reason: SetupArgs standalone forms removed
- `crates/gcore/src/provisioning/bootstrap.rs::*` — scope-reason: file deleted with the module
- `crates/gcore/src/provisioning/hub.rs::*` — scope-reason: file deleted; hub-identity probing dies with the 2.2 resolution collapse
- `crates/gcore/src/provisioning/docker.rs::*` — scope-reason: file deleted; Docker provisioning is Python-daemon authority
- `crates/gcore/src/provisioning/tests.rs::*` — scope-reason: deleted with the module, including the compose-template parity test
- `crates/gcore/assets/docker-compose.services.yml::*` — scope-reason: embedded compose template deleted with docker.rs; the Python daemon keeps its own copy
- `crates/gwiki/src/commands/code/tests.rs::*` — scope-reason: system-model fixture drops the deleted compose-asset path reference
- `crates/gcore/src/config/tests/daemon_source.rs::*` — scope-reason: StandaloneConfig and gcore.yaml fixtures deleted with the standalone source arms
- `crates/gcore/src/config/tests/indexing.rs::*` — scope-reason: StandaloneConfig fixtures deleted with the mode
- `crates/gcore/tests/runtime_mode_process.rs::*` — scope-reason: process-level runtime-mode tests deleted with the mode
- `crates/gcore/src/config/tests/ai.rs::*` — scope-reason: $secret: resolution tests deleted with client secret handling
- `crates/gcode/src/contract.rs::*` — scope-reason: final contract_version 3→4 bump lands here
- `crates/gwiki/src/contract.rs::*` — scope-reason: final contract_version 16→17 bump lands here
- `crates/gcode/contract/gcode.contract.json::*` — scope-reason: canonical snapshot regenerates once at the final version bump
- `crates/gwiki/contract/gwiki.contract.json::*` — scope-reason: canonical snapshot regenerates once at the final version bump
- `crates/gcode/tests/contract.rs::*` — scope-reason: pinned-contract mirror tracks the final snapshot
- `crates/gwiki/tests/cli_contract.rs::*` — scope-reason: pinned-contract mirror tracks the final snapshot
- `crates/gcode/README.md`
- `docs/guides/gcode-user-guide.md::*` — scope-reason: DSN ladder, gcore.yaml, and standalone setup documentation rewritten for grant-only resolution
- `docs/guides/gcode-development-guide.md::*` — scope-reason: runtime-mode and resolution-ladder sections rewritten
- `docs/guides/ai-daemon-contract.md::*` — scope-reason: standalone/direct namespace contract text rewritten daemon-only
- `crates/gcode/src/config/tests/runtime_contract.rs`
- `crates/gcore/src/lib.rs::*` — scope-reason: crate root drops the runtime_mode, setup, and secrets module declarations and the relocated provisioning exports
- `crates/gcode/src/setup.rs::*` — scope-reason: gcode setup module root deleted with its tree
- `crates/gwiki/src/api.rs::*` — scope-reason: Command::Setup variant and SetupOptions leave the programmatic API
- `crates/gwiki/src/cli/mapping.rs::*` — scope-reason: the From<SetupArgs> conversion and Command::Setup construction are deleted with the command
- `crates/gwiki/src/commands/project_admission.rs::*` — scope-reason: the exhaustive Command::Setup admission match arm is removed
- `crates/gwiki/src/lib.rs::*` — scope-reason: the SetupOptions public re-export leaves the crate root
- `crates/gwiki/src/setup.rs::*` — scope-reason: file deleted whole (GwikiStandaloneSetup implementation)
- `crates/gwiki/src/cli/tests.rs::*` — scope-reason: SetupArgs CLI construction tests are deleted with the command surface
- `crates/gcode/src/cli/tests.rs::*` — scope-reason: setup test module declarations leave the CLI test root
- `crates/gcode/src/cli/tests/setup.rs::*` — scope-reason: Command::Setup CLI tests are deleted with the surface
- `crates/gcode/src/test_env.rs::*` — scope-reason: the bootstrap.yaml fallback and run_standalone_setup provisioning path convert to the 6.1 grant-fixture harness
- `crates/gcore/tests/effective_config_process.rs::*` — scope-reason: the standalone runtime-mode process test dies with the mode
- `crates/gwiki/README.md`
- `docs/guides/gwiki-user-guide.md::*` — scope-reason: standalone setup, routing flags, and DSN variables leave the user guide
- `docs/guides/gwiki-development-guide.md::*` — scope-reason: standalone/dual-mode development flows rewritten grant-only
- `docs/guides/gcore-development-guide.md::*` — scope-reason: runtime-mode, secrets, and provisioning module documentation rewritten for the deleted surfaces
- `docs/guides/gwiki-daemon-web.md::*` — scope-reason: daemon-web page drops removed setup and routing prescriptions
- `docs/guides/codewiki.md::*` — scope-reason: codewiki guide drops removed env-variable and standalone prescriptions
- `docs/guides/code-index.md::*` — scope-reason: code-index guide drops removed DSN-variable and setup prescriptions
- `docs/contracts/gwiki-cli.md::*` — scope-reason: CLI contract doc tracks the final flag surface and version 17
- `docs/contracts/gcode-cli.md::*` — scope-reason: CLI contract doc tracks the final flag surface and version 4

`RuntimeMode` is deleted — there is one mode. `~/.gobby/gcore.yaml` is no longer read anywhere; `StandaloneConfig`, `apply_text_generation_defaults_from_embeddings`, `gcode setup --standalone` and its flag family, and gwiki's `SetupArgs` standalone forms are removed. The deletion is whole-tree, not central-types-only: gcode's `commands/setup.rs` plus the entire `setup/` module tree and its `lib.rs`/`dispatch.rs`/`cli.rs` declarations; gwiki's setup command, dispatch arm, and CLI args; and the entire gcore `provisioning/` module with nothing relocated — its only consumers are the standalone paths this plan deletes, Docker service provisioning is already owned by the Python daemon's installer surface (`src/gobby/cli/installers/`, template `src/gobby/data/docker-compose.services.yml`), the embedded compose template `crates/gcore/assets/docker-compose.services.yml` and its parity test are deleted with `docker.rs`, hub-identity probing dies with the 2.2 resolution collapse, and no Rust daemon-side consumer exists. gwiki's system-model test fixture drops its reference to the deleted compose asset. Module roots and public re-exports go with the implementations: gcore's `lib.rs` drops the `runtime_mode`, `setup`, and `secrets` declarations and the relocated provisioning exports; gcode's `setup.rs` module root is deleted with its tree; gwiki's programmatic API (`api.rs`) loses `Command::Setup` and `SetupOptions`. The reachability closure is complete across the workspace: gwiki's CLI mapping (`cli/mapping.rs` — the `From<SetupArgs>` conversion and `Command::Setup` construction), the `commands/project_admission.rs` exhaustive admission arm, the `lib.rs` `SetupOptions` re-export, `setup.rs` (`GwikiStandaloneSetup`) deleted whole, and the `cli/tests.rs` SetupArgs tests; gcode's CLI test seams (`cli/tests.rs`, `cli/tests/setup.rs`) and `test_env.rs`, whose `bootstrap.yaml` fallback and `run_standalone_setup` provisioning path convert to the 6.1 grant-fixture harness; and gcore's `effective_config_process.rs` standalone process test, which dies with the mode. No public surface keeps a standalone path compiled or reachable, and the E1 audit covers re-exports. Client crates lose `secrets.rs` KEK material handling; `$secret:` markers reaching a client are a grant-issuance bug and fail typed. The `ConfigSource` trait survives with daemon-served and grant-backed implementors only, preserving the seam for future daemon API adapters. Registered non-capability settings consumed by managed callers ride the 1.3 machine-config endpoint through the 2.1 client; deleting standalone resolution removes no daemon-served setting transport.

The single final contract landing happens here: gcode contract_version 3→4 and gwiki 16→17, snapshots regenerated once after the last flag removal, pinned-mirror parity green. The grant-error CLI contract (2.2) is included in the final snapshots. Every maintained page advertising a removed surface rewrites with it: the gcode and gwiki READMEs, the gcode/gwiki user and development guides, the gcore development guide, the gwiki daemon-web page, the codewiki and code-index guides, and the gcode/gwiki CLI contract docs stop advertising `GCODE_DATABASE_URL`/`GWIKI_DATABASE_URL`/`GOBBY_POSTGRES_DSN`, the resolution ladders, `gcore.yaml`, runtime modes, standalone setup, and direct/auto routing, and align with the final grant-only daemon-required contract. #19645's runtime-contract suite is reconciled: the `standalone_mode_preserves_env_yaml_precedence` regression and every mode-split standalone arm in `crates/gcode/src/config/tests/runtime_contract.rs` are deleted with the mode, while its registry-authority tests survive rewritten against grant-backed resolution. A workspace zero-match audit over client-crate sources covers the removed surfaces in qualified form — `gobby_core::runtime_mode` (module path and imports, not the bare word `RuntimeMode`, because gwiki's CodeWiki system model owns an unrelated `RuntimeMode` documentation enum that stays), `StandaloneConfig`, `gcore.yaml`, the removed env DSN variables, and `$secret:` (test fixtures migrate in 6.1; audit commands in E1).

**Acceptance:**

- 4.1.1 - gcore's `runtime_mode` module, `StandaloneConfig`, and every gcore.yaml read are gone from the client crates; gwiki's unrelated system-model `RuntimeMode` documentation enum is untouched. file: `crates/gcore/src/runtime_mode.rs`.
- 4.1.2 - No client crate contains KEK unwrap or `$secret:` resolution. file: `crates/gcore/src/secrets.rs`.
- 4.1.3 - `gcode setup` standalone surface is removed with its contract entries. file: `crates/gcode/src/commands/setup.rs`.
- 4.1.4 - `ConfigSource` retains its trait shape with grant-backed implementors. symbol: `ConfigSource`.
- 4.1.5 - Final contract versions (gcode 4, gwiki 17) land exactly once with regenerated snapshots and passing pinned-mirror parity. file: `crates/gcode/contract/gcode.contract.json`.
- 4.1.6 - Client-crate sources contain zero qualified references to the removed surfaces (`gobby_core::runtime_mode`, `StandaloneConfig`, `gcore.yaml`, env DSNs, `$secret:`) per the E1 audit, which is scoped to exclude gwiki's unrelated system-model RuntimeMode. file: `crates/gcore/src/runtime_mode.rs`.
- 4.1.7 - The gcode and gwiki READMEs, the maintained gwiki/gcore/codewiki/code-index guides, the gwiki daemon-web page, and the gcode/gwiki CLI contract docs describe only grant-based resolution and daemon-only AI; no maintained page advertises standalone setup, direct/auto routing, removed DSN variables, or client bootstrap.yaml credentials. file: `crates/gcode/README.md`.
- 4.1.8 - The standalone-precedence regression arm added by #19645 is deleted with the mode; surviving runtime-contract tests assert grant-backed authority. file: `crates/gcode/src/config/tests/runtime_contract.rs`.
- 4.1.9 - No module root or public re-export keeps a standalone surface compiled or reachable; gcore's crate root, gcode's setup module root, and gwiki's programmatic API drop their standalone declarations. file: `crates/gcore/src/lib.rs`.
- 4.1.10 - The gcore provisioning module, its embedded compose template, and their tests are deleted with nothing relocated; the Python installer surface remains the sole provisioning owner and the gwiki system-model fixture drops the deleted asset reference. file: `crates/gcore/src/provisioning/mod.rs`.
- 4.1.11 - No `Command::Setup` construction, `SetupOptions` conversion or export, exhaustive setup match, or `StandaloneSetup` implementation survives anywhere in the workspace; the gcode and gcore setup test seams convert to grant fixtures or die with the mode. file: `crates/gwiki/src/cli/mapping.rs`.

### 4.2 Remove MemoryWikiStore and daemon-optional wiki modes [category: code] (depends: 2.2)
`kind: deliverable`

Targets:
- `crates/gwiki/src/store/memory.rs::*` — scope-reason: file deleted (MemoryWikiStore removed)
- `crates/gwiki/src/store/types.rs::*` — scope-reason: WikiIndexStore trait keeps its shape; memory implementor references and hubless selection arms are removed
- `crates/gwiki/src/support/services.rs::*` — scope-reason: RuntimeServices::detached and shell-ready degradation are removed
- `crates/gwiki/src/commands/status.rs::*` — scope-reason: status reports grant/daemon state instead of shell-ready/memory modes
- `crates/gwiki/src/commands/trust.rs::*` — scope-reason: trust drops memory-mode reporting
- `crates/gwiki/src/store.rs::*` — scope-reason: module root drops the MemoryWikiStore export
- `crates/gwiki/src/commands/collect.rs::*` — scope-reason: collect pipeline becomes trait-generic over the grant-resolved store
- `crates/gwiki/src/commands/index.rs::*` — scope-reason: index pipelines construct the grant-resolved store instead of MemoryWikiStore
- `crates/gwiki/src/commands/session_sync.rs::*` — scope-reason: session sync migrates off the in-memory store
- `crates/gwiki/src/support/counts.rs::*` — scope-reason: count helpers take the store trait instead of the concrete memory type
- `crates/gwiki/src/support/graph.rs::*` — scope-reason: graph target mapping takes the store trait
- `crates/gwiki/src/support/scope.rs::*` — scope-reason: scope resolution takes the store trait
- `crates/gwiki/src/support/search.rs::*` — scope-reason: search paths take the store trait
- `crates/gwiki/src/indexer.rs::*` — scope-reason: indexer unit tests migrate to the test-only store fake
- `crates/gwiki/src/ingest/audio.rs::*` — scope-reason: ingest test modules migrate to the test-only fake and the file decomposes below the 1,000-line ceiling
- `crates/gwiki/src/ingest/audio/tests.rs`
- `crates/gwiki/tests/status_grant_state.rs`
- `crates/gwiki/src/ingest/document/tests.rs::*` — scope-reason: document ingest tests migrate to the fake
- `crates/gwiki/src/collect/tests.rs::*` — scope-reason: collect tests migrate to the fake
- `crates/gwiki/src/compile/tests.rs::*` — scope-reason: compile tests migrate to the fake
- `crates/gwiki/src/commands/refresh/tests.rs::*` — scope-reason: refresh tests migrate to the fake
- `crates/gwiki/src/store/test_fake.rs`
- `crates/gwiki/src/ingest/file/tests.rs::*` — scope-reason: file ingest tests migrate to the fake
- `crates/gwiki/src/ingest/git.rs::*` — scope-reason: git ingest test module migrates to the fake
- `crates/gwiki/src/ingest/image.rs::*` — scope-reason: image ingest test module migrates to the fake
- `crates/gwiki/src/ingest/mod.rs::*` — scope-reason: ingest module-root tests migrate to the fake
- `crates/gwiki/src/ingest/pdf/tests.rs::*` — scope-reason: pdf ingest tests migrate to the fake
- `crates/gwiki/src/ingest/session/redaction.rs::*` — scope-reason: redaction tests and their typed store helper migrate to the fake
- `crates/gwiki/src/ingest/session_archive/tests.rs::*` — scope-reason: session-archive tests and their MemoryWikiStore wrapper migrate to the fake
- `crates/gwiki/src/ingest/session_archive/tests/summary.rs::*` — scope-reason: summary tests migrate to the fake
- `crates/gwiki/src/ingest/url/tests.rs::*` — scope-reason: url ingest tests and their wrapper migrate to the fake
- `crates/gwiki/src/support/config.rs::*` — scope-reason: the support-config MemoryWikiStore test seam migrates to the fake

`MemoryWikiStore` is deleted; `WikiIndexStore` keeps its trait shape (the direct-adapter→daemon-adapter seam) with the Postgres implementor as sole production impl. Every concrete consumer has an explicit disposition: the collect, index, session-sync, and trust pipelines become trait-generic and run against the grant-resolved Postgres store; the counts, graph, scope, and search helpers take the trait instead of the concrete type; unit seams that genuinely need in-memory state use a `cfg(test)`-only `WikiIndexStore` fake landing as its own `store/test_fake.rs` target, which replaces MemoryWikiStore across the indexer, ingest, collect, compile, and refresh test modules. The consumer closure is exhaustive, from the workspace `MemoryWikiStore` inventory: beyond the pipelines and helpers above, the ingest test modules across audio, document, file, git, image, the ingest module root, pdf, session redaction, session-archive (including its summary submodule and typed wrapper), and url migrate to the fake, as does the support-config test seam. Deleting `store/memory.rs` compiles only after every constructor, wrapper field, and typed helper parameter has migrated. `gwiki status` reports grant state via the non-authorizing `inspect_cached_grant` (2.1) plus a bounded daemon reachability probe — valid/expiring/expired, deployment token, epoch, reachability. Diagnostic surfaces (status, help, contract output) never require successful grant acquisition. `"shell-ready"` / `mode: "memory"` outputs are gone; commands that formerly degraded to the memory store fail typed with daemon-required guidance.

`crates/gwiki/src/ingest/audio.rs` stands at 1,059 lines, over the repository's 1,000-line ceiling, so this leaf owns its decomposition: the embedded `#[cfg(test)]` module (roughly the file's back half) moves to `crates/gwiki/src/ingest/audio/tests.rs` via a `#[cfg(test)] mod tests;` declaration, and if the production half still meets the ceiling after 3.1's direct-arm deletions, markdown rendering helpers split into a sibling module. Every production file this leaf touches finishes below 1,000 lines.

**Acceptance:**

- 4.2.1 - `MemoryWikiStore` and every hubless fallback selection are deleted. file: `crates/gwiki/src/store/memory.rs`.
- 4.2.2 - `WikiIndexStore` trait shape is unchanged. symbol: `WikiIndexStore`.
- 4.2.3 - `gwiki status` surfaces grant validity, deployment token, epoch, and daemon reachability. test: `crates/gwiki/tests/status_grant_state.rs::status_reports_grant_state`.
- 4.2.4 - Every former MemoryWikiStore production consumer compiles against the trait with its stated disposition; unit tests use the test-only fake. file: `crates/gwiki/src/store.rs`.
- 4.2.5 - `gwiki status`, help, and contract output work with an expired or absent grant, reporting state via the non-authorizing inspector. test: `crates/gwiki/tests/status_grant_state.rs::expired_grant_reports_not_fails`.
- 4.2.6 - `audio.rs` finishes below 1,000 lines with its test module extracted; every production file touched by this leaf ends below the ceiling. file: `crates/gwiki/src/ingest/audio/tests.rs`.
- 4.2.7 - Every workspace `MemoryWikiStore` constructor, wrapper, and typed parameter is migrated — production to the trait, tests to the fake — before `store/memory.rs` is deleted; the workspace zero-match audit passes. file: `crates/gwiki/src/store/test_fake.rs`.

## P5: Identity binding and hardening
`kind: framing`

**Goal**: Every daemon call from the binaries is identity-bearing and the route matrix enforces it.

### 5.1 Bind identity on daemon AI and broker routes [category: code] (depends: 2.1, 3.1)
`kind: deliverable`

Targets:
- `src/gobby/servers/auth_service.py::*` — scope-reason: bind_identity flips to True on AI and broker routes now that all clients send identity headers
- `crates/gcore/src/ai/daemon/transport.rs::*` — scope-reason: every daemon request attaches principal identity headers from the grant
- `crates/gcode/src/savings.rs::*` — scope-reason: savings reporting becomes identity-bearing
- `crates/gcode/src/graph/code_graph/lifecycle.rs::*` — scope-reason: graph lifecycle broker calls attach grant identity
- `crates/gcore/src/ai/daemon/operations.rs::*` — scope-reason: all five modality operations present the signed grant with principal identity
- `crates/gcode/src/commands/embeddings_doctor.rs::*` — scope-reason: doctor requests are grant-presenting daemon calls
- `tests/servers/test_auth_service.py::*` — scope-reason: matrix tests gain identity binding, modality presentation, and live-lease guard coverage
- `crates/gcore/src/grant/tests.rs`

Every AI and broker request presents the signed grant in the `X-Gobby-Runtime-Grant` header (Constraints: grant presentation) — this is the single presentation mechanism for all five modality transports (`operations.rs`), the wiki/code broker calls, savings, graph lifecycle, and `embeddings_doctor`. `AuthService` verifies signature, deployment, epoch, schema, API contract, capability, and principal through the 1.2 rejection matrix before any handler side effect, for both bearer classes: identity is derived from the verified grant principal, never from caller-supplied headers, so forged machine/project headers under a valid operator token fail typed. On a stale-epoch or rotated-signature rejection the client synchronizes one re-handshake under the 2.1 per-cache lock, atomically replaces the cache, and retries exactly once; retry exhaustion surfaces the typed rejection. With no anonymous clients left, `bind_identity=False` entries for `POST /api/embeddings`, vision extract, voice transcribe, and the wiki/code broker routes flip to True; the comment justifying anonymous Rust callers is deleted.

Effectful handlers are fenced against live lease loss: a pre-handler guard validates active ownership in memory — the lease connection is alive and the cached epoch is the one it acquired (the 1.1 lease object exposes this liveness/ownership check) — before any side effect, and the daemon treats lease-connection loss as immediate loss of ownership, refusing effectful requests without waiting for asynchronous heartbeat handling. The advisory lock's session-bound lifetime makes the in-memory check authoritative with no per-request database roundtrip: a dead lease connection cannot still hold the lock. During standby-takeover overlap, the displaced daemon therefore rejects while the new owner serves.

The guard-to-effect window is fenced, not merely checked: every effectful hub-database write validates the owned fencing epoch against `deployment_runtime` inside the same transaction that performs the write, so a daemon whose lease died between the pre-handler guard and commit cannot commit — the successor's epoch bump fails the stale transaction's epoch predicate. Takeover cancels or drains predecessor in-flight work before the new owner serves effectful traffic. Non-transactional side effects (FalkorDB, Qdrant, broker fan-out) cannot carry a downstream-enforced epoch predicate; their residual window is bounded by immediate connection teardown on lease loss and documented as exactly that — no pretend enforcement.

**Acceptance:**

- 5.1.1 - All gcore daemon transports attach the signed grant and grant-derived identity. file: `crates/gcore/src/ai/daemon/transport.rs`.
- 5.1.2 - AI and broker capability-matrix rows require grant presentation and identity binding; anonymous calls 401. test: `tests/servers/test_auth_service.py::test_ai_routes_require_identity`.
- 5.1.3 - Savings and graph-lifecycle calls carry identity and succeed under the new matrix. symbol: `report_savings`.
- 5.1.4 - Each of the five modalities plus `embeddings_doctor` has its own presentation-binding test; forged identity headers under a valid operator token are rejected typed. test: `tests/servers/test_auth_service.py::test_modality_grant_presentation_matrix`.
- 5.1.5 - Stale-epoch presentation triggers exactly one synchronized re-handshake and retry; exhaustion yields the typed rejection. test: `crates/gcore/src/grant/tests.rs::stale_epoch_single_retry`.
- 5.1.6 - Every effectful route validates live lease ownership pre-handler; lease-connection loss immediately stops effectful service; a displaced daemon rejects during takeover overlap. test: `tests/servers/test_auth_service.py::test_effectful_requires_live_lease`.
- 5.1.7 - Effectful hub writes validate the owned epoch in-transaction; a lease lost between guard and commit cannot commit; takeover drains predecessor in-flight work before serving. test: `tests/servers/test_auth_service.py::test_in_transaction_epoch_fencing`.

## P6: End-to-end acceptance
`kind: framing`

**Goal**: The full boundary holds under restart, outage, expiry, and cross-deployment conditions.

### 6.1 Boundary end-to-end suite [category: test] (depends: P4, P5)
`kind: deliverable`

Targets:
- `tests/e2e/test_runtime_boundary.py`
- `crates/gcore/src/grant/tests.rs`
- `crates/gcode/tests/graph_standalone/support.rs::*` — scope-reason: fixture env injection migrates to grant files
- `crates/gcode/tests/projection_stale.rs::*` — scope-reason: direct-DSN env injection replaced by managed grant fixtures
- `crates/gcode/tests/projection_standalone.rs::*` — scope-reason: standalone smoke converts to the grant harness or is deleted
- `crates/gwiki/tests/common/mod.rs::*` — scope-reason: shared fixture provisions schema and grants without removed env vars
- `crates/gwiki/src/support/test_env.rs::*` — scope-reason: test environment helper mints grant files instead of DSN env
- `crates/gwiki/src/commands/citation_quality.rs::*` — scope-reason: EnvGuard DSN injection replaced by grant fixtures
- `crates/gwiki/src/librarian/tests.rs::*` — scope-reason: EnvGuard DSN injection replaced by grant fixtures
- `crates/gcode/src/config/tests.rs::*` — scope-reason: with_service_env FalkorDB/Qdrant injection migrates to grant fixtures
- `crates/gcore/src/config/tests.rs::*` — scope-reason: shared env-isolation lists drop the removed variable names
- `crates/gcore/src/config/tests/resolution.rs::*` — scope-reason: env-DSN and Falkor/Qdrant resolution tests deleted with the resolution arms
- `crates/gcore/src/falkor.rs::*` — scope-reason: live-read test gate migrates from removed env vars to grant fixtures
- `crates/gwiki/src/commands/graph.rs::*` — scope-reason: EnvGuard unset lists drop the removed variables
- `crates/gwiki/tests/cli_output.rs::*` — scope-reason: gcore.yaml fixture write removed with the config surface
- `tests/ai/test_tool_chat_tools.py::*` — scope-reason: passthrough-stripping assertions for removed DSN vars retire with the strip list
- `tests/code_index/test_gcode_phase7_contract.py::*` — scope-reason: static contract expectations pinning FalkorDB env layering, bootstrap.yaml, and standalone mode rewrite around signed grant capabilities
- `tests/code_index/test_gcode_storage_conformance.py::*` — scope-reason: conformance fixtures replace removed env and bootstrap injection with managed grant fixtures

A shared grant-fixture layer replaces every fixture path that 2.2 and 4.1 delete: isolated schema provisioning goes through gdaemon apply (never the removed `gcode setup` DDL), helpers mint grants via handshake with the operator token or write 0600 grant cache files directly, and command-under-test fixtures set `GOBBY_MANAGED_EXECUTION_BOOTSTRAP` to a pre-materialized grant file. Fixture conversion audits the complete removed-variable inventory — `GCODE_DATABASE_URL`, `GWIKI_DATABASE_URL`, `GOBBY_POSTGRES_DSN`, `GOBBY_FALKORDB_{HOST,PORT,PASSWORD}`, `GOBBY_QDRANT_{URL,API_KEY}`, and `gcore.yaml` fixture writes — across Python and Rust fixtures and helpers, including the gcode and gcore config test suites, gcore's falkor live-test gate, gwiki's graph EnvGuards and cli_output fixture, and the Python tool-chat passthrough assertions; each injection site converts to grant fixtures or the test is deleted. Daemon-side provisioning variables (the Python compose installers, hub backup, and CI stack) are container provisioning configuration outside this audit's scope — the E1 audit runs over `crates` sources only, where zero matches must remain across production and tests once fixtures migrate. The audit also owns the Python code-index contract suites: `tests/code_index/test_gcode_phase7_contract.py` and `test_gcode_storage_conformance.py` pin FalkorDB env layering, `bootstrap.yaml`, `GCODE_DATABASE_URL`, `GOBBY_POSTGRES_DSN`, and standalone-mode expectations today; both rewrite their static expectations around signed grant capabilities and run on managed grant fixtures. The declared cargo validation depends on these helpers.

Isolated test daemon (temporary state and ports, `GOBBY_TEST_PROTECT=1`) exercising: fresh handshake → gcode search + gwiki read succeed; daemon stop → both keep working on the unexpired grant, AI calls fail typed; grant expiry during outage → typed daemon-required from feature commands while diagnostics still report state; daemon restart → epoch bump, old grant rejected at presentation, re-handshake transparent on next invocation; second deployment against the same database → independent lease, epoch, and grants; daemon restart as operator invalidation → per-acquisition secret rotation plus epoch bump reject every outstanding grant at presentation. Added scenarios from review, each with its own acceptance item below: five-modality identity binding, concurrent renewal race, command-scope broker paths (projects, global prune with partial-failure retry convergence, project prune), diagnostics under expiry, degraded hybrid search with the pinned warning, dormant CodeWiki route stability, the persisted symbol-summary regression (daemon summarizer writes `code_symbols.summary`; gcode retrieval passes), backup-restore replay rejection at daemon presentation with separate direct-capability coverage per store, two-reachable-daemon takeover fencing including the guard-to-commit barrier, and rotation drain plus explicit revocation during outage.

**Acceptance:**

- 6.1.1 - The core six scenarios pass against an isolated daemon. test: `tests/e2e/test_runtime_boundary.py::test_runtime_boundary_scenarios`.
- 6.1.2 - No scenario leaves a binary in a fallback mode; every failure is the typed daemon-required or rejection-matrix error. behavior: "typed failure contract" in `docs/guides/ai-configuration.md`.
- 6.1.3 - No test injects any removed credential or endpoint variable (`GCODE_DATABASE_URL`, `GWIKI_DATABASE_URL`, `GOBBY_POSTGRES_DSN`, `GOBBY_FALKORDB_*`, `GOBBY_QDRANT_*`) or writes `gcore.yaml`; shared fixtures provision schema and grants through supported paths only. file: `crates/gwiki/tests/common/mod.rs`.
- 6.1.4 - Automatic daemon-side symbol summarization and gcode summary retrieval hold under the boundary. test: `tests/e2e/test_runtime_boundary.py::test_symbol_summary_regression`.
- 6.1.5 - All five AI modalities bind identity end-to-end under the grant boundary. test: `tests/e2e/test_runtime_boundary.py::test_modality_identity_binding`.
- 6.1.6 - Concurrent renewal across processes preserves the newest generation and never blocks a valid invocation. test: `tests/e2e/test_runtime_boundary.py::test_concurrent_renewal_race`.
- 6.1.7 - Broker-scope paths behave per the command-scope table: projects listing, global prune decomposition including partial-failure retry convergence, project prune, and capability-token rejection. test: `tests/e2e/test_runtime_boundary.py::test_broker_scope_paths`.
- 6.1.8 - Diagnostics report state under expired or absent grants without acquiring. test: `tests/e2e/test_runtime_boundary.py::test_diagnostics_under_expiry`.
- 6.1.9 - Hybrid search during outage degrades with the exact structured warning; explicit AI commands fail typed in the same window. test: `tests/e2e/test_runtime_boundary.py::test_search_degrades_with_warning`.
- 6.1.10 - Dormant CodeWiki routes return byte-identical outputs under the boundary. test: `tests/e2e/test_runtime_boundary.py::test_dormant_codewiki_unchanged`.
- 6.1.11 - Restoring the hub database from backup and reacquiring the lease rejects archived grants at daemon presentation via the fresh signing secret; offline direct authorization on an unexpired archived grant is exercised separately for the PostgreSQL, FalkorDB, and Qdrant direct variants and stays bounded by grant expiry. test: `tests/e2e/test_runtime_boundary.py::test_restore_replay_rejected`.
- 6.1.12 - During standby-takeover overlap the displaced daemon refuses effectful requests immediately while the new owner serves; dropping the lease after a successful pre-handler guard cannot produce a committed effect under the old epoch. test: `tests/e2e/test_runtime_boundary.py::test_takeover_fencing`.
- 6.1.13 - Rotation during outage drains the predecessor generation until issued-grant expiry; explicit revocation presented to a reachable daemon fails early with the typed revoked code, while outage-window backend invalidation surfaces as the ordinary datastore-authorization error. test: `tests/e2e/test_runtime_boundary.py::test_rotation_drain_and_revocation`.
- 6.1.14 - The gcode phase-7 contract and storage-conformance suites run green on grant fixtures with zero references to removed variables, client bootstrap.yaml credentials, or standalone mode. test: `tests/code_index/test_gcode_phase7_contract.py::test_contract_on_grant_fixtures`.

## E1 Verification
`kind: verification`

Rebuild and reinstall the workspace binaries (`~/.gobby/bin/{gcode,gwiki}`), restart the daemon, then the acceptance-owning suites: `uv run pytest tests/runtime_grants/ tests/servers/routes/test_runtime_handshake.py tests/servers/routes/test_runtime_config.py tests/servers/routes/test_wiki_code_routes.py tests/servers/routes/test_code_index_prune_route.py tests/servers/test_auth_service.py tests/test_daemon_lease.py tests/storage/test_managed_credentials.py tests/code_index/test_gcode_phase7_contract.py tests/code_index/test_gcode_storage_conformance.py tests/e2e/test_runtime_boundary.py -v`; `cargo test -p gobby-core -p gobby-code -p gobby-wiki`; `cargo test -p gobby-daemon --test cli_contract`; minimal-feature builds proving grant availability without AI features (`cargo build -p gobby-code --no-default-features && cargo build -p gobby-wiki --no-default-features` plus each binary's default set, with `cargo tree -p gobby-code --no-default-features` and `cargo tree -p gobby-wiki --no-default-features` asserting the AI dependency stack is absent); `uv run ruff check src/ && uv run mypy src/`; contract conformance via the daemon's snapshot tests.

Schema identity regenerates in the 1.1 order (BASELINE_CHECKSUM → catalog with `UPDATE_GCORE_SCHEMA_MANIFEST=1` against an isolated database → release `gdaemon` build → `uv run python scripts/generate_schema_expected_identity.py --gdaemon target/release/gdaemon` → non-update catalog freshness + both identity contract tests).

Zero-match audits over crate sources and tests (paths `crates/gcode crates/gcore crates/gwiki crates/gdaemon crates/ghook`, excluding the historical `crates/CHANGELOG.md`; fixtures already migrated by 6.1): `gcode grep -F "gobby_core::runtime_mode" <paths>` and `gcode grep -F "runtime_mode::" <paths>` (qualified forms — gwiki's unrelated system-model `RuntimeMode` stays), `gcode grep -w StandaloneConfig <paths>`, `gcode grep -F "gcore.yaml" <paths>`, `gcode grep "GCODE_DATABASE_URL|GWIKI_DATABASE_URL|GOBBY_POSTGRES_DSN|GOBBY_FALKORDB_|GOBBY_QDRANT_" <paths>`, `gcode grep -F "\$secret:" <paths>`, `gcode grep "AiRouting::Direct|AiRouting::Auto|DirectChatTransport" <paths>`, `gcode grep "ANTHROPIC_API_KEY|OPENAI_API_KEY|OPENROUTER_API_KEY|GROQ_API_KEY" <paths>`, and the lease-exclusivity audit `gcode grep -F "single-active-daemon" <paths>` (only the Python daemon acquires the advisory lease) — all empty.

Manual smoke: `gcode search` cold (handshake mints grant), `gobby stop` then `gcode search` again (grant window), `gwiki status` (grant state surface via the non-authorizing inspector), daemon restart then any gwiki AI command (epoch rotation transparent, single retry).

## V1 Plan Changelog
`kind: verification`

**Round 1** `kind: verification`

- reviewer_run: 32407716-46e1-4404-84fa-6ec61f5f2986
- reviewer_session: 5adb288f-c592-41e0-baab-fe5256411c5b
- verdict: needs_review
- findings:
- DNRB-R1-WIRE-CONTRACT / blocking / v2 grant omits API version, integer schema versions, and direct FalkorDB/Qdrant connection material
- DNRB-R1-SCHEMA-ASSETS / blocking / numbered migration would never apply; baseline-375 fold with identity/catalog regeneration required
- DNRB-R1-INTERACTIVE-SCHEMA / blocking / gobby_agent_auth cannot represent interactive principals; (machine, project) key collides across deployments
- DNRB-R1-PRINCIPAL-RENEWAL / blocking / renewed grants can outlive role VALID UNTIL; concurrent renewals can downgrade the cached generation
- DNRB-R1-HANDSHAKE-REGISTRATION / blocking / handshake route unregistered without routes/__init__.py and _app_routes.py targets
- DNRB-R1-GRANT-MODULE-REGISTRATION / blocking / grant module undeclared in lib.rs; HTTP client tied to AI-only Cargo features
- DNRB-R1-CACHE-BINDING / blocking / cache locator underspecified; managed grants could overwrite the interactive cache
- DNRB-R1-GRANT-PRESENTATION / blocking / no grant presentation on AI routes; forged headers under operator token; no stale-epoch recovery
- DNRB-R1-DIAGNOSTICS-ERRORS / blocking / diagnostics need a non-authorizing inspector; grant errors need stable CLI contracts
- DNRB-R1-COMMAND-SCOPE / blocking / --project name lookup, projects, and global prune touch datastores pre-grant
- DNRB-R1-PUBLIC-CONTRACTS / blocking / contract snapshots, mirrors, README, and guides missing from targets; version bump ordering wrong
- DNRB-R1-AI-BLAST-RADIUS / blocking / AiRouting/transport consumers across gcode and gwiki unowned
- DNRB-R1-STANDALONE-BLAST-RADIUS / blocking / setup trees, provisioning readers, and declarations unowned by the standalone removal
- DNRB-R1-MEMORY-BLAST-RADIUS / blocking / store.rs export and concrete MemoryWikiStore consumers unowned
- DNRB-R1-TEST-HARNESS / blocking / existing fixtures depend on removed setup/DSN paths; summary regression evidence missing
- resolution_notes: All 15 findings accepted and repaired. 1.1 moved to the baseline-375 fold with ordered identity/catalog regeneration; 1.2 gained tagged-union capabilities, api_contract, integer schema versions, a seventh rejection class, and golden vectors; 1.3 gained the interactive gobby_agent_auth schema, expiry bounds, issue/reuse serialization, and route registration; 2.1 gained lib.rs/Cargo wiring, the deterministic locator, source-bound validation, renewal locking, and inspect_cached_grant; 2.2 gained the command-scope table and stable grant-error CLI contracts; 3.1/3.2 own the full AI blast radius with the final contract bump moved to 4.1; 4.1 owns the complete standalone removal including #19645's standalone-precedence test reconciliation; 4.2 owns every MemoryWikiStore consumer with a test-only fake; 5.1 defines the single signed-grant presentation mechanism with bounded stale-epoch recovery; 6.1 owns the grant-fixture migration and added scenarios. Constraints gained schema-authority, #19645-sequencing, and grant-presentation entries.

```json plan-review-round
{"evidence_id":"b7895378-4d7b-4ab3-a60e-12879b4bebaa","plan_hash":"ba869b2e7db8593c220411d806280a9b252e2230ee3345a91e2936fdb1983432","round_number":1,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"75929893c84707d273fd950a380d8f34d9b85328aae4f375ad6f124735172b1b","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":11,"emitted_findings":15,"total":26},"evidence_id":"b7895378-4d7b-4ab3-a60e-12879b4bebaa","lanes":[{"candidate_count":8,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":10,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":8,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":11,"manifest_digest":"2415fbb62505a9099dae7ed5e4947ab7060e12fa97bbb58ca4facd23e0243094","status":"valid"},"source_digest":"fb99c287c852e65039a9e5d1156e6dbc5293f8708c6bb062795a41dc47a1545e","version":1},"findings":[{"category":"missing-requirement","check_key":"grant-wire-contract-completeness","description":"The v2 shape omits the daemon API version required by #18902, encodes schema versions as strings although the authoritative identity uses integers, and gives direct FalkorDB/Qdrant capabilities only a mode even though § 2.2 deletes every other source of their host/port/password or URL/API key.","finding_id":"DNRB-R1-WIRE-CONTRACT","fix":"Rewrite § 1.2 capabilities as strict tagged unions whose direct variants carry complete typed connection material, brokered variants carry exact operations, and unavailable variants carry no secrets. Add an explicit API-contract version, numeric schema-version fields, typed API-mismatch rejection, and Python↔Rust golden serialization tests for every variant.","location":"P1 / § 1.2","prevention":"Compare each grant requirement and each direct constructor input against the Python and Rust wire models, then run cross-language golden round trips.","principle":"A self-contained cross-language grant must carry every field required by its governing requirement and every direct constructor.","root_cause":"The bundle example was generalized from v1 capability modes without completing API-version and direct-datastore wire contracts.","section_id":"1.2","severity":"blocking"},{"category":"traceability","check_key":"schema-authority-path","description":"`crates/gcore/assets/schema/migrations/deployment_epoch.sql` would never be applied: `MIGRATIONS` is empty and states that pre-0.5.0 changes remain folded into baseline 375. Section 1.1 also omits `assets.rs`, baseline/catalog assets, `schema_expected_identity.json`, and freshness tests.","finding_id":"DNRB-R1-SCHEMA-ASSETS","fix":"Use the pre-0.5 baseline path: target `crates/gcore/assets/schema/baseline.sql`, `crates/gcore/src/schema/assets.rs`, `crates/gcore/assets/schema/catalog.manifest.json`, `src/gobby/storage/schema_expected_identity.json`, and schema identity/catalog freshness tests. Remove the unregistered migration target and add gdaemon apply/verify plus Python identity-parity acceptance.","location":"P1 / § 1.1","prevention":"Before planning a schema change, inspect the embedded migration registry, baseline policy, generated identity, catalog manifest, and freshness tests.","principle":"Schema work must follow the repository's active schema-authority path and target every embedded or generated asset that makes it executable.","root_cause":"The plan assumes a migrations directory that does not exist and omits the registry/identity artifacts that gdaemon actually embeds.","section_id":"1.1","severity":"blocking"},{"category":"missing-requirement","check_key":"interactive-principal-schema","description":"Current `OwnerKind`, `principal_bindings`, and issuance functions cannot represent `interactive`. A `(machine_id, project_id)` key also collides across two deployments sharing one database, undermining the plan's cross-deployment independence.","finding_id":"DNRB-R1-INTERACTIVE-SCHEMA","fix":"Specify the gobby_agent_auth schema changes in § 1.3: add interactive ownership, deployment token and machine/project identity, a unique `(deployment_token, machine_id, project_id)` active binding, issue-or-reuse SQL, and rotation/revocation/reconciliation/inventory behavior. Add same-key reuse, cross-project isolation, and same-database cross-deployment tests.","location":"P1 / §§ 1.1 and 1.3","prevention":"Trace every new principal kind through type aliases, CHECK constraints, binding keys, SECURITY DEFINER functions, role grants, rotation, revocation, reconciliation, and inventory.","principle":"A new principal kind needs an explicit datastore identity, uniqueness, authorization, and lifecycle contract.","root_cause":"The plan treats interactive issuance as a Python manager extension although the authoritative SQL schema accepts only execution/session-bound agent_run and tool_chat principals.","section_id":"1.3","severity":"blocking"},{"category":"unhandled-edge","check_key":"grant-credential-lifetime-and-generation","description":"A renewed grant can outlive the PostgreSQL role's `VALID UNTIL`, so direct access fails inside the advertised outage window. Two concurrent renewals can also let a slower older generation overwrite a newer cached DSN.","finding_id":"DNRB-R1-PRINCIPAL-RENEWAL","fix":"Require `grant.expires_at <= min(role_valid_until, bearer_expiry, owner_lifetime)`. Define daemon-side issue/reuse serialization, per-cache interprocess renewal locking with re-read, and generation-aware replacement that refuses downgrade. Add barrier-controlled concurrent renewal and original-role-expiry outage tests.","location":"P1 / § 1.3 and P2 / § 2.1","prevention":"For every renewal path, check expiry bounds, concurrent issuers, generation ordering, cache replacement, credential rotation, and owner revocation.","principle":"A grant's advertised validity and cached generation must stay within the underlying credential and owner lifetimes under concurrent renewal.","root_cause":"Renewal issues a fresh one-hour grant around a reused fixed-expiry role and atomic rename is mistaken for serialization.","section_id":"1.3","severity":"blocking"},{"category":"traceability","check_key":"http-route-registration-targets","description":"`runtime_handshake.py` is unreachable unless `src/gobby/servers/routes/__init__.py` exports it and `src/gobby/servers/_app_routes.py` includes it; neither file is targeted.","finding_id":"DNRB-R1-HANDSHAKE-REGISTRATION","fix":"Add both registry files to § 1.3 Targets, specify export and `include_router` wiring, and add an app-level test proving `POST /api/runtime/handshake` is registered with the intended auth dependency.","location":"P1 / § 1.3","prevention":"For each new route, trace module definition through package export, router include, app construction, and route-presence tests.","principle":"A new route module is incomplete until every centralized export and application registration site is owned by the deliverable.","root_cause":"The plan names the new module and auth matrix but misses the daemon's router aggregation sites.","section_id":"1.3","severity":"blocking"},{"category":"traceability","check_key":"rust-module-and-feature-registration","description":"`crates/gcore/src/lib.rs` does not declare `grant`, and the current HTTP clients are tied to optional AI features even though grants gate PostgreSQL construction. Section 2.1 targets neither `lib.rs` nor Cargo feature wiring.","finding_id":"DNRB-R1-GRANT-MODULE-REGISTRATION","fix":"Add `crates/gcore/src/lib.rs`, `crates/gcore/Cargo.toml`, and any gcode/gwiki Cargo feature changes to Targets. State that grant acquisition is always available to both binaries and add the exact minimal feature-combination builds to acceptance.","location":"P2 / § 2.1","prevention":"For each new Rust module, inspect `lib.rs`, Cargo features/dependencies, consumer Cargo manifests, and minimal-feature builds.","principle":"A new Rust module used by non-AI paths must be exported and available under every consumer feature combination.","root_cause":"The plan adds grant source files without tracing crate module declarations or optional HTTP dependencies.","section_id":"2.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"grant-cache-selection-and-binding","description":"`acquire(project_root)` does not state how it knows the deployment token before opening the cache. Its validation list omits project and principal binding, and it does not say that agent_run/tool_chat grants from `GOBBY_MANAGED_EXECUTION_BOOTSTRAP` must never overwrite the interactive project cache.","finding_id":"DNRB-R1-CACHE-BINDING","fix":"Specify the simple deterministic locator: derive the 16-hex deployment token from canonical `GOBBY_HOME` with the same algorithm as Python and read the project UUID locally. Validate deployment, project, machine, principal kind, and managed execution/session expectations. Cache only interactive grants; consume managed files in place. Add cross-language token golden tests, cache-substitution tests, and same-project/two-deployment offline tests.","location":"P2 / § 2.1","prevention":"Walk cold/offline lookup, same-project multi-deployment, file substitution, managed-file loading, interactive loading, and cache-write eligibility.","principle":"A cache must be locatable from pre-load state and every loaded grant must bind to the expected deployment, project, machine, principal kind, and execution source.","root_cause":"The cache path and structural checks were specified from fields inside the grant, while managed and interactive grants share one reader without source-specific validation.","section_id":"2.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"grant-presentation-identity-and-recovery","description":"Interactive machine/project headers remain spoofable under a valid operator token, prior-epoch grants have no presentation point on AI calls, and no stale-epoch handler performs the promised transparent re-handshake. The all-call inventory also misses `embeddings_doctor`, and generic tests can leave one of the five modalities unbound.","finding_id":"DNRB-R1-GRANT-PRESENTATION","fix":"Define one exact signed-grant presentation mechanism for every AI and broker request. AuthService must verify signature, deployment, epoch, capability, and principal before handler side effects and bind headers/body to that principal for both bearer classes. On stale epoch or rotated signature, synchronize one handshake, atomically replace cache, and retry once. Target all five modality transports plus `embeddings_doctor` and test each modality, forged operator headers, restart above half-TTL, and retry exhaustion. State explicitly that offline direct datastore use trusts the 0600 cache and that HMAC rejection occurs at daemon presentation.","location":"P5 / § 5.1 with §§ 1.2, 2.1, 3.1, and 6.1","prevention":"Trace authentication and identity for operator and capability callers across every daemon call, then test stale epoch, signature rotation, forged headers, retry count, and pre-handler rejection.","principle":"Identity headers become authoritative only when bound to an authenticated principal, and every stale or revoked presentation needs a bounded recovery transition.","root_cause":"The plan adds grant-derived headers without defining grant presentation on AI routes; operator bearer authentication currently bypasses the agent identity matrix.","section_id":"5.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"grant-diagnostics-and-cli-errors","description":"Offline `acquire` rejects an expired grant, yet `gwiki status` must report expired state. The claim that every command fails after expiry also conflicts with status/help/contract diagnostics, and neither CLI defines stable output for the new GrantError and rejection classes.","finding_id":"DNRB-R1-DIAGNOSTICS-ERRORS","fix":"Add a non-authorizing `inspect_cached_grant` API that returns absent/malformed/valid/expiring/expired without exposing connection material. Make status use it with a bounded reachability probe; exempt diagnostics/help/contract from construction failure. Define JSON code, message, and exit behavior for every grant error in both contract sources, snapshots, and CLI tests.","location":"P2 / § 2.1, P4 / § 4.2, and P6 / § 6.1","prevention":"Classify commands as authorizing or diagnostic and map every typed internal error through JSON code, text, exit status, snapshots, and invocation tests.","principle":"Diagnostic commands must inspect invalid state without authorizing service construction, and typed internal failures need stable public CLI contracts.","root_cause":"One enforcing acquisition API is assigned both authorization and diagnosis, while contract bumps omit the new grant error surface.","section_id":"4.2","severity":"blocking"},{"category":"missing-requirement","check_key":"pregrant-project-and-global-command-scope","description":"`--project <name>` queries PostgreSQL before a project ID exists, while `gcode projects` and global prune bypass normal Context construction and span projects. The planned handshake accepts one project ID and the managed-role surface is project-scoped.","finding_id":"DNRB-R1-COMMAND-SCOPE","fix":"Add a command-scope table to § 2.2. Resolve path/project-file identities locally; move name resolution to an authenticated daemon lookup; route projects/global prune through explicit operator-only daemon broker operations. Add no-pregrant-datastore tests for path, name, project-id, projects, and prune dispatch.","location":"P2 / § 2.2","prevention":"Classify every command by pre-grant inputs, project scope, datastore scope, and broker requirement before removing legacy resolution.","principle":"Every command must obtain an authorization scope before datastore access, and global operations cannot run under a single project-scoped role.","root_cause":"The plan collapses service construction without inventorying dispatch paths that resolve project identity from the database or operate across projects.","section_id":"2.2","severity":"blocking"},{"category":"traceability","check_key":"cli-contract-artifacts-and-doc-consumers","description":"All four contract JSON artifacts are absent from Targets, and § 4.1 removes additional setup flags after § 3.2's stated regeneration. Maintained gcode/gwiki guides and the gcode README still prescribe the DSN variables, config ladders, RuntimeMode, and standalone setup being deleted.","finding_id":"DNRB-R1-PUBLIC-CONTRACTS","fix":"Move final version bumps and regeneration into § 4.1 after every CLI removal. Target `crates/gcode/contract/gcode.contract.json`, `crates/gwiki/contract/gwiki.contract.json`, both `tests/contracts` mirrors, the gcode README, and maintained gcode/gwiki/gcore user/development guides; add exact snapshot-parity and removed-surface documentation checks.","location":"P3 / § 3.2 and P4 / § 4.1","prevention":"Inventory contract source, canonical snapshot, test mirror, README, user guide, development guide, and changelog for every removed flag/env/config surface.","principle":"Removed CLI/config surfaces require one final ordered contract regeneration and updates to every maintained user-facing source that advertises them.","root_cause":"The plan targets contract generators and one guide while omitting canonical snapshots, mirrors, later flag removal, and maintained guides.","section_id":"3.2","severity":"blocking"},{"category":"traceability","check_key":"ai-routing-exhaustive-consumers","description":"Representative omitted production consumers include gcode's direct embedding arm, gwiki AI clients and generation-route constructors, and `gcore::ai::generation::mod` re-exports. Current Targets allow compile failures and surviving Direct/Auto/provider paths.","finding_id":"DNRB-R1-AI-BLAST-RADIUS","fix":"Expand § 3.1 Targets to every production match/re-export/constructor found by the blast-radius inventory, including gcode vector embedding/lifecycle and doctor paths, gcore generation module/tests, and gwiki AI clients, generation, ask, graph/index, ingest, transcribe, vision, and support services. Classify each as daemon, Off, or deletion and add a workspace source audit.","location":"P3 / §§ 3.1 and 3.2","prevention":"Resolve the changed symbols first, then sweep exhaustive matches, re-exports, constructors, tests, and provider-key/direct-route strings workspace-wide.","principle":"Deleting enum variants and transports requires every exhaustive match, re-export, constructor, fake, and source audit to be owned.","root_cause":"Targets focus on gcore definitions and primary CLI flags without following their Rust consumers across gcode and gwiki.","section_id":"3.1","severity":"blocking"},{"category":"traceability","check_key":"standalone-removal-exhaustive-consumers","description":"Omitted production paths include gcode setup module declarations and DDL/Postgres helpers, gwiki's standalone setup command/implementation, and gcore provisioning hub/bootstrap readers. Deleting only the listed files leaves compile failures and forbidden credential/config paths.","finding_id":"DNRB-R1-STANDALONE-BLAST-RADIUS","fix":"Expand § 4.1 Targets to the complete gcode setup tree and declarations, gwiki setup command/implementation/dispatch/declarations, gcore provisioning bootstrap/hub/tests, and remaining effective-config/AI-context consumers. State which provisioning primitives move daemon-side and require a workspace-wide zero-match audit for removed configuration and secret readers.","location":"P4 / § 4.1","prevention":"Sweep RuntimeMode/StandaloneConfig/setup types plus every gcore.yaml, env DSN, bootstrap DSN, and secret-resolution read across production and tests.","principle":"Removing a runtime mode requires all constructors, module declarations, provisioning helpers, CLI surfaces, and credential readers to migrate together.","root_cause":"The deliverable lists central types but misses independent setup and provisioning ownership in all three crates.","section_id":"4.1","severity":"blocking"},{"category":"traceability","check_key":"memory-store-concrete-consumers","description":"`store.rs` exports MemoryWikiStore and production collect, index, session-sync, trust, scope, counts, graph, and search paths construct or inspect it. Current Targets guarantee unresolved imports and leave command behavior unspecified.","finding_id":"DNRB-R1-MEMORY-BLAST-RADIUS","fix":"Add `store.rs` and every concrete consumer to § 4.2 Targets. State per command whether it becomes Postgres-backed, trait-generic, brokered, or removed, and replace production memory fallback with an explicit test-only `WikiIndexStore` fake where tests still need an in-memory seam.","location":"P4 / § 4.2","prevention":"Sweep exports, constructors, enum/downcast matches, concrete fields, trait bounds, and test seams before deleting an implementation.","principle":"Deleting a concrete implementation requires a disposition for every constructor, concrete-field access, export, and test fake.","root_cause":"MemoryWikiStore was treated as one fallback selector although production algorithms directly construct and inspect it.","section_id":"4.2","severity":"blocking"},{"category":"weak-testability","check_key":"grant-boundary-test-fixture-migration","description":"gcode `test_env`, gwiki integration fixtures, and graph/projection tests depend on standalone setup or direct DSN variables that §§ 2.2 and 4.1 delete. The plan also lacks named regression evidence for automatic daemon-side symbol summarization and gcode summary retrieval.","finding_id":"DNRB-R1-TEST-HARNESS","fix":"Add a shared grant-fixture migration to § 6.1 Targets: isolated schema provisioning without removed CLI setup, handshake/grant-file helpers, `GOBBY_MANAGED_EXECUTION_BOOTSTRAP` command fixtures, and conversion or deletion of direct-DSN tests. Add the five-modality, concurrent renewal, scope, status, restart, and persisted-symbol-summary scenarios required by the earlier findings, then make the declared cargo validation depend on those helpers.","location":"P6 / § 6.1","prevention":"Inventory fixture constructors, env injection, standalone setup calls, isolated daemon lifecycle, and preserved-behavior regressions before declaring cargo/e2e validation.","principle":"A boundary rewrite must migrate existing fixtures away from removed setup and credential paths before its declared validation can run.","root_cause":"The plan names a new e2e file while existing Rust suites still provision standalone databases and inject direct DSN environment variables.","section_id":"6.1","severity":"blocking"}],"reviewer_session":"5adb288f-c592-41e0-baab-fe5256411c5b","round":1,"round_number":1,"verdict":"needs_review"},"session_id":"31d4187b-0498-4783-813b-d72be21b8bd1"}
```

**Round 2** `kind: verification`

- reviewer_run: 7ffad0ab-1528-4cbd-ac96-9481e9974915
- reviewer_session: b27115b9-cd0d-4b80-997f-78b70f3e9e2f
- verdict: needs_review
- findings:
- DNRB-R2-DISPATCH-ROW-COVERAGE / blocking / command-scope table rows lack per-row acceptance items
- DNRB-R2-CONFIGSTORE-BLOCKER / blocking / #19645 ordering was documentary; no live blocked-by edge existed
- DNRB-R2-BASELINE-PREDECESSOR / blocking / second baseline-375 rewrite omits runner receipt-chain ownership
- DNRB-R2-INTERACTIVE-DDL-OWNERSHIP / blocking / interactive DDL unowned by 1.1 before identity regeneration
- DNRB-R2-STANDALONE-SEQUENCING / blocking / 4.1 could delete paths before 2.2 replacements exist
- DNRB-R2-DORMANCY-REGRESSION / blocking / dormant CodeWiki outputs unpinned across router changes
- DNRB-R2-COVERAGE-LEDGER / blocking / companion coverage-ledger YAML absent
- DNRB-R2-FIXTURE-CREDENTIAL-AUDIT / blocking / fixture audit misses GOBBY_POSTGRES_DSN and Falkor/Qdrant vars
- DNRB-R2-E2E-ACCEPTANCE / blocking / added E2E scenarios lack individual acceptance items
- DNRB-R2-STANDALONE-MODULE-ROOTS / blocking / module roots and re-exports missing from 4.1 targets
- DNRB-R2-GLOBAL-BROKER / blocking / global prune had no route or authorization contract
- DNRB-R2-AUDIO-DECOMPOSITION / blocking / audio.rs at 1,059 lines without in-leaf decomposition
- DNRB-R2-MINIMAL-FEATURE-BUILDS / blocking / minimal-build proof missing feature propagation for both binaries
- DNRB-R2-HANDSHAKE-CLAIM-BINDING / blocking / body identity not bound to verified bearer claims
- DNRB-R2-REMOTE-CACHE-IDENTITY / blocking / cache identity derived from local state, not the selected daemon
- DNRB-R2-INTERACTIVE-CREDENTIAL-REUSE / blocking / issue-or-reuse unimplementable without recoverable material
- DNRB-R2-ACTIVE-CONFIG-BINDING / blocking / grant issuance not bound to one ConfigRuntime revision
- DNRB-R2-RENEWAL-DEADLINE / blocking / renewal lock/network behavior unbounded
- DNRB-R2-SEARCH-OUTAGE-CONTRACT / blocking / hybrid-search outage behavior contradictory between 3.1 and 6.1
- DNRB-R2-API-CONTRACT-VALIDATION / blocking / client-side api_contract gate and typed error missing
- DNRB-R2-MACHINE-CONFIG-TRANSPORT / blocking / managed callers lose non-capability config transport
- resolution_notes: All 21 findings accepted with four user-decided contracts. Global prune (11): operator-only grant-exempt route `POST /api/code-index/prune`; the daemon decomposes it into a hub-record sweep plus per-project children carrying project grants. Interactive reuse (16): daemon-side credential-material store keyed (deployment, machine, project, generation), DDL sealed in 1.1. Search outage (19): explicit lexical/graph degradation with a structured warning; silent empty-source degrade deleted. Machine config (21): separate grant-presenting `GET /api/runtime/config` endpoint plus a gcore machine-config client; the grant stays sole authority for capabilities and connections. Repairs: Constraints now state the live epic-level #18902→#19645 blocked-by edge (recorded in the task graph this round) as the enforcement mechanism; 1.1 owns the full interactive DDL, receipt-chain advance (runner.rs/runner_tests.rs), and seals the baseline before regeneration; 1.2 binds issuance to one active config revision; 1.3 gains the bearer-to-grant claim matrix, fail-closed managed source, deployment metadata in the handshake response, the runtime-config endpoint, and pinned dormancy regressions; 2.1 gains explicit feature propagation with no-default builds for both binaries, the trusted endpoint→deployment binding, bounded zero-wait renewal with typed Timeout, the ApiContractMismatch gate, and the machine-config client; 2.2 decomposes the command-scope table into per-row acceptance with the global-prune contract; 3.1 pins degraded hybrid search; 4.1 depends on 2.2 and owns module roots/re-exports; 4.2 owns the audio.rs decomposition; 6.1 audits the full removed-variable inventory and names one test per scenario; the coverage ledger was authored alongside this round.

```json plan-review-round
{"evidence_id":"d3e11e66-67df-4428-8aa4-f5f95108f91e","plan_hash":"097f2c81f2c824ff244e6aca41c86728cd3c577da8450d0754d156c3e970cd2c","round_number":2,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"ed8497fb439423c539b7bb54f07c71e1868b4d14a3601c17e80176c108d81dcc","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":6,"emitted_findings":21,"total":27},"evidence_id":"d3e11e66-67df-4428-8aa4-f5f95108f91e","lanes":[{"candidate_count":11,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":5,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":11,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":11,"manifest_digest":"85bbac1c8a2454f6973fa6d9c33dc2452d8bfcdef75b6fbcf5e922d78bc8cc6b","status":"valid"},"source_digest":"1daaf7cf740a731350b49d021eff5810c8e88120dddbdef20df9ef2ac23c39ee","version":1},"findings":[{"category":"gobby-format","check_key":"table-row-decomposition","description":"Projects, project prune, global prune, and projectless rejection lack independently traceable acceptance coverage.","finding_id":"DNRB-R2-DISPATCH-ROW-COVERAGE","fix":"Add one acceptance item per row, each naming its daemon route, Rust dispatch surface, authorization behavior, and test artifact.","location":"Phase 2 / § 2.2","prevention":"Count work-table rows against acceptance items and require one artifact-backed acceptance ID per row.","principle":"Each table row that defines independently implementable behavior needs a stable acceptance item.","root_cause":"Section 2.2 enumerates four command-scope behaviors but collapses them into one aggregate acceptance item.","section_id":"2.2","severity":"blocking"},{"category":"bad-sequencing","check_key":"external-cutover-blockers","description":"The promised ConfigStore ordering is documentary; leaves can start before open epic #19645 completes.","finding_id":"DNRB-R2-CONFIGSTORE-BLOCKER","fix":"Add a live blocked-by edge from epic #18902 to #19645 through the coordinator and state that epic-level edge as the enforcement mechanism. Remove the unsupported promise that expansion synthesizes per-leaf external edges.","location":"Constraints and Phase 1 / § 1.1","prevention":"Before expansion, verify every external prerequisite as a live epic-level blocked-by edge and confirm the expansion contract does not claim unsupported child inheritance.","principle":"Hard external prerequisites must be enforced by the task graph mechanism that expansion actually honors.","root_cause":"The plan states that #18902 sequences behind #19645, while the live #18902 graph has no blocked-by edge to #19645 and manifest expansion compiles only internal section dependencies.","section_id":"1.1","severity":"blocking"},{"category":"bad-sequencing","check_key":"baseline-receipt-refresh-chain","description":"Existing hubs carrying the #19645 receipt would become an unrecognized mismatch after this second baseline edit.","finding_id":"DNRB-R2-BASELINE-PREDECESSOR","fix":"Add `crates/gcore/src/schema/runner.rs` and `runner_tests.rs` targets, recognize exactly the #19645 baseline receipt as predecessor, and test fresh, pre-#19645, post-#19645, current, and arbitrary-mismatch cases.","location":"Phase 1 / § 1.1","prevention":"For each baseline fold, target receipt classification and tests for fresh, exact predecessor, current, and arbitrary mismatch states.","principle":"Every baseline rewrite must advance the exact predecessor receipt recognized by the schema runner.","root_cause":"Section 1.1 changes baseline 375 after #19645, whose runner recognizes exactly one earlier checksum, but omits runner and runner-test ownership.","section_id":"1.1","severity":"blocking"},{"category":"bad-sequencing","check_key":"baseline-fold-artifact-ownership","description":"Independent expansion leaves cannot place the interactive schema into the baseline before 1.1 seals its identity.","finding_id":"DNRB-R2-INTERACTIVE-DDL-OWNERSHIP","fix":"Move the complete interactive owner, binding, and function DDL plus schema tests into 1.1 before regeneration; leave 1.3 to consume the schema.","location":"Phase 1 / §§ 1.1 and 1.3","prevention":"Audit each baseline fold for complete DDL ownership before any identity artifact regeneration.","principle":"All DDL in a baseline identity must be owned before checksum, catalog, and packaged identity regeneration.","root_cause":"Section 1.3 says interactive-owner DDL rides the earlier 1.1 fold, although 1.1 neither specifies nor accepts that DDL and 1.3 lacks baseline identity targets.","section_id":"1.1","severity":"blocking"},{"category":"bad-sequencing","check_key":"cutover-consumer-dependencies","description":"Standalone config and client paths can be deleted before their daemon-native replacements exist.","finding_id":"DNRB-R2-STANDALONE-SEQUENCING","fix":"Make 4.1 depend on 2.2 and 3.2, or directly encode all equivalent replacement-producing dependencies.","location":"Phase 4 / § 4.1","prevention":"For every deletion deliverable, enumerate replacement producers and encode each as a direct or transitive dependency.","principle":"A destructive cutover must depend on every replacement path it consumes.","root_cause":"Section 4.1 depends only on Phase 3, which reaches 2.1 but does not guarantee broker dispatch from 2.2 or interactive acquisition from 3.2.","section_id":"4.1","severity":"blocking"},{"category":"weak-testability","check_key":"dormant-route-output-regression","description":"The plan can satisfy its listed tests while changing dormant CodeWiki route behavior.","finding_id":"DNRB-R2-DORMANCY-REGRESSION","fix":"Add artifact-backed acceptance and named regressions for each dormant CodeWiki route, including exact status codes and response payloads.","location":"Phase 1 / § 1.3 and Phase 6 / § 6.1","prevention":"Map every exact-output constraint to a named regression test and acceptance ID.","principle":"Constraints preserving exact dormant route outputs require explicit regression acceptance at every modified routing boundary.","root_cause":"Handshake and router changes cross the dormant CodeWiki boundary, yet no acceptance item pins the existing status and payload outputs.","section_id":"1.3","severity":"blocking"},{"category":"gobby-format","check_key":"coverage-ledger-parity","description":"Requirement-to-acceptance parity cannot be audited from the required ledger artifact.","finding_id":"DNRB-R2-COVERAGE-LEDGER","fix":"Create the companion coverage-ledger YAML, enumerate all governing requirements and acceptance mappings, and validate it before round 3.","location":"Plan-wide / Phase 6 / § 6.1","prevention":"Before review, verify the companion ledger exists and maps every governing requirement to sections and acceptance IDs.","principle":"An epic plan governed by the coverage contract must ship its adversary-reviewed companion ledger before expansion.","root_cause":"The plan promises a coverage ledger and the repository contract requires one, but `.gobby/plans/daemon-native-runtime-boundary.coverage-ledger.yaml` is absent.","section_id":"6.1","severity":"blocking"},{"category":"weak-testability","check_key":"removed-env-fixture-audit","description":"Tests may retain removed credential and endpoint configuration while still satisfying the stated audit.","finding_id":"DNRB-R2-FIXTURE-CREDENTIAL-AUDIT","fix":"Expand the fixture and environment audit to every removed PostgreSQL, FalkorDB, Qdrant, and service-specific variable across Python and Rust helpers.","location":"Phase 6 / § 6.1","prevention":"Derive fixture-audit assertions from the complete deleted-variable inventory.","principle":"A credential-surface removal must audit every deleted environment variable in production and test fixtures.","root_cause":"Acceptance forbids only GCODE_DATABASE_URL and GWIKI_DATABASE_URL although the cutover also removes GOBBY_POSTGRES_DSN and standalone Falkor/Qdrant variables.","section_id":"6.1","severity":"blocking"},{"category":"weak-testability","check_key":"scenario-to-acceptance-parity","description":"The generic E2E acceptance can pass while omitting several scenarios added during round-1 repair.","finding_id":"DNRB-R2-E2E-ACCEPTANCE","fix":"Add stable acceptance items and named E2E test functions for every modality, renewal race, broker-scope rejection, outage, and diagnostic scenario.","location":"Phase 6 / § 6.1","prevention":"Count promised scenarios and require one named test plus artifact-backed acceptance item for each.","principle":"Every promised E2E scenario needs a stable acceptance ID and named observable outcome.","root_cause":"Section 6.1 prose promises five modality, concurrent-renewal, broker-scope, and diagnostics scenarios without individual acceptance items.","section_id":"6.1","severity":"blocking"},{"category":"traceability","check_key":"deletion-root-reexports","description":"Standalone APIs can remain compiled and public after the listed implementation files are removed.","finding_id":"DNRB-R2-STANDALONE-MODULE-ROOTS","fix":"Add the relevant `lib.rs`, setup module roots, `api.rs`, and project-admission re-exports to Targets, then remove or replace every exported standalone surface.","location":"Phase 4 / § 4.1","prevention":"Run a repository-wide import and re-export sweep for every deleted module and place all surviving roots in Targets.","principle":"Deleting an implementation requires removing every module root and public re-export that keeps it compiled or reachable.","root_cause":"The target inventory omits gcore, gcode, and gwiki module roots and re-exports for standalone setup and project admission.","section_id":"4.1","severity":"blocking"},{"category":"missing-requirement","check_key":"global-broker-route-authorization","description":"Global prune has neither a daemon entry point nor a coherent grant scope, so its dispatch and authorization contract are undefined.","finding_id":"DNRB-R2-GLOBAL-BROKER","fix":"Target and specify the daemon prune route, capability, deployment-scoped operator principal or explicit operator-only grant exemption, Rust mapping, rejection behavior, and route/CLI tests.","location":"Phase 2 / § 2.2","prevention":"Trace each broker table row from CLI to route to service and verify its principal scope can issue the required grant.","principle":"Every brokered command needs a reachable route and an authorization principal capable of requesting it.","root_cause":"The plan declares global prune as a daemon operation, while no server prune route is targeted and all runtime grants are project-scoped.","section_id":"2.2","severity":"blocking"},{"category":"gobby-format","check_key":"source-size-threshold","description":"Executing 4.2 as written would violate the repository's source-size completion gate.","finding_id":"DNRB-R2-AUDIO-DECOMPOSITION","fix":"Add an explicit audio.rs decomposition to 4.2, name extracted modules and import changes, and require every touched production file to finish below 1,000 lines.","location":"Phase 4 / § 4.2","prevention":"Check current and projected line counts for every production target and add in-task decomposition whenever the ceiling is reached.","principle":"Any plan touching a hand-maintained production file at or above 1,000 lines must own decomposition in the same leaf.","root_cause":"`crates/gwiki/src/ingest/audio.rs` is 1,059 lines and section 4.2 changes it without a decomposition target.","section_id":"4.2","severity":"blocking"},{"category":"weak-testability","check_key":"minimal-feature-matrix","description":"The proposed proof cannot demonstrate that both gcode and gwiki compile without embedded AI or standalone service stacks.","finding_id":"DNRB-R2-MINIMAL-FEATURE-BUILDS","fix":"Define gcore/gcode/gwiki feature propagation and add `cargo tree` assertions plus `--no-default-features` builds for both gcode and gwiki.","location":"Phase 2 / § 2.1","prevention":"Audit Cargo feature edges and require feature-tree plus no-default builds for every affected binary.","principle":"Minimal-build claims require explicit feature propagation and proof for every shipped binary.","root_cause":"The plan names only a gcode no-default-features build while gcode and gwiki currently activate gcore/AI paths through their feature wiring.","section_id":"2.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"bearer-grant-principal-binding","description":"A caller can request a grant for body identity outside its bearer claims unless the plan defines derivation and equality checks.","finding_id":"DNRB-R2-HANDSHAKE-CLAIM-BINDING","fix":"Define a bearer-to-grant matrix: derive bound fields from verified claims, reject mismatches, require presented grants to match those claims, and make an existing managed bootstrap source authoritative on failure.","location":"Phase 1 / § 1.3","prevention":"Test cross-project, cross-owner, copied-cache, mismatched-body, and managed-source-failure paths for every bearer kind.","principle":"A granted principal must be equal to, or narrower than, the verified bearer principal.","root_cause":"Handshake authentication verifies token claims separately from caller-supplied machine_id/project_id fields, and managed acquisition fallback is not fail-closed.","section_id":"1.3","severity":"blocking"},{"category":"unhandled-edge","check_key":"daemon-cache-deployment-identity","description":"Remote endpoints can collide with or miss the correct grant cache because client-local filesystem identity does not identify the selected daemon.","finding_id":"DNRB-R2-REMOTE-CACHE-IDENTITY","fix":"Advertise deployment identity as authenticated daemon metadata and persist a trusted endpoint-to-deployment binding; perform handshake before cache selection when no trusted binding exists.","location":"Phase 2 / § 2.1","prevention":"Test local, overridden remote, first-contact, endpoint-change, and daemon-outage cache selection.","principle":"Cache identity must be obtained from the authenticated daemon endpoint that owns the credential generation.","root_cause":"The plan derives deployment_token from the client's local GOBBY_HOME before handshake, while daemon_url may select a different or remote daemon whose token hashes its own data root.","section_id":"2.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"credential-generation-reuse","description":"The daemon cannot fulfill interactive issue-or-reuse after restart from the storage contract described in the plan.","finding_id":"DNRB-R2-INTERACTIVE-CREDENTIAL-REUSE","fix":"Specify secure daemon-side materialization keyed by deployment, machine, project, and generation with atomic reuse and cleanup. An alternative complete contract may rotate every handshake and invalidate predecessor grants explicitly.","location":"Phase 1 / § 1.3","prevention":"Exercise issuance, reuse, daemon restart, rotation, revocation, and concurrent handshakes against one generation key.","principle":"Issue-or-reuse must recover connection material for the exact live credential generation.","root_cause":"The existing credential manager retains plaintext only at issuance and in per-execution bootstrap material, while the plan requires reusable interactive roles without specifying recoverable daemon-side material.","section_id":"1.3","severity":"blocking"},{"category":"unhandled-edge","check_key":"active-config-grant-snapshot","description":"A grant can mix desired/current configuration or capabilities with secrets from different active revisions.","finding_id":"DNRB-R2-ACTIVE-CONFIG-BINDING","fix":"Capture exactly one active-bundle pointer per issuance, build all capability and secret fields from it, sign its revision or epoch, and define expiry/renewal behavior across later revisions.","location":"Phase 1 / § 1.2","prevention":"Test same-reference secret rotation failure and concurrent active-bundle swaps while asserting one coherent signed revision.","principle":"One grant issuance must observe one ConfigRuntime active bundle and its matching active secret binding.","root_cause":"The replacement grant service lacks an explicit ConfigRuntime dependency, signed config revision, and policy for cached grants across failed or concurrent config activation.","section_id":"1.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"bounded-renewal-contention","description":"A normal invocation can block indefinitely while holding a valid grant or race multiple refreshes.","finding_id":"DNRB-R2-RENEWAL-DEADLINE","fix":"Use zero-wait or tightly bounded try-lock, immediately serve the unexpired grant while another process renews, share one deadline for mandatory refresh, re-read after acquisition, and surface a typed bounded failure.","location":"Phase 2 / § 2.1","prevention":"Test lock contention, crashed lock holder, slow handshake, simultaneous processes, stale epoch, and deadline exhaustion.","principle":"Proactive renewal of a still-valid grant must have bounded lock and network behavior.","root_cause":"The plan combines synchronous handshake, an interprocess lock, and stale-epoch retry while asserting non-blocking renewal, yet defines no try-lock policy, lock bound, request deadline, or stale-lock recovery.","section_id":"2.1","severity":"blocking"},{"category":"missing-requirement","check_key":"hybrid-search-outage-policy","description":"The plan leaves whether hybrid search fails or degrades unresolved, so implementers and tests can choose incompatible behaviors.","finding_id":"DNRB-R2-SEARCH-OUTAGE-CONTRACT","fix":"Choose one contract: typed command failure, or explicit lexical/graph degradation with structured warning. Add the search command and vector-search targets and test the exact selected outcome.","location":"Phase 3 / § 3.1","prevention":"Cross-check each outage acceptance against command-layer error handling and lower-level fallback behavior.","principle":"Each command needs one non-contradictory outage outcome across implementation and E2E acceptance.","root_cause":"Section 3.1 requires typed failure for unreachable daemon AI, while 6.1 requires gcode search to continue during daemon outage and current hybrid search degrades semantic failure to an empty source.","section_id":"3.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"client-api-contract-gate","description":"An old Rust client can accept a newly issued grant describing an incompatible daemon API.","finding_id":"DNRB-R2-API-CONTRACT-VALIDATION","fix":"Add expected API-contract validation, a distinct `ApiContractMismatch` error with public CLI mapping, and cross-language golden vectors across every acquisition path.","location":"Phase 2 / § 2.1","prevention":"Run old-client/new-grant vectors through managed-file, cache, handshake, offline, and live-request acquisition paths.","principle":"A client must reject an unsupported signed API contract before constructing services or dispatching requests.","root_cause":"The grant carries api_contract and the daemon validates it, while the enumerated Rust load checks and GrantError variants omit the client-side comparison.","section_id":"2.1","severity":"blocking"},{"category":"missing-requirement","check_key":"managed-machine-config-transport","description":"Managed gcode and gwiki callers lose daemon-served non-capability configuration after standalone resolution is deleted.","finding_id":"DNRB-R2-MACHINE-CONFIG-TRANSPORT","fix":"Retain an authenticated, grant-presenting machine-config endpoint and Rust client for registered non-capability values while keeping grant v2 authoritative for capabilities and connection truth; add operator and agent-run precedence tests.","location":"Phase 2 / § 2.1 and Phase 4 / § 4.1","prevention":"Inventory registered Rust runtime keys and trace each one to an authenticated managed-client transport and precedence test.","principle":"Removing a runtime configuration transport requires a replacement for every registered non-capability setting consumed by managed clients.","root_cause":"The cutover removes the authenticated effective-config client and keeps `/api/config/effective` operator-only, while grant v2 carries capabilities and connection data rather than the remaining registered runtime settings.","section_id":"2.1","severity":"blocking"}],"reviewer_session":"b27115b9-cd0d-4b80-997f-78b70f3e9e2f","round":2,"round_number":2,"verdict":"needs_review"},"session_id":"31d4187b-0498-4783-813b-d72be21b8bd1"}
```

**Round 3** `kind: verification`

- reviewer_run: 1b0a3b32-3560-4ded-81ad-d06a7365be13
- reviewer_session: d511fb8e-0438-4feb-bd1b-7f5611fe4c7c
- verdict: needs_review
- findings:
- DNRB-R3-LEASE-EXCLUSIVITY / blocking / no proof that only the daemon acquires the active-daemon advisory lease
- DNRB-R3-AI-BLAST-RADIUS-REMAINDER / blocking / gwiki/gcode production consumers of Direct/Auto/DirectChatTransport outside Targets
- DNRB-R3-FIXTURE-AUDIT-REMAINDER / blocking / fixture/helper injection sites for removed env vars and gcore.yaml unowned
- DNRB-R3-TEST-TARGETS / blocking / acceptance-named tests absent from owning Targets inventories
- DNRB-R3-AUDIT-SCOPE / blocking / repository-wide RuntimeMode zero-match collides with gwiki's unrelated system-model enum
- DNRB-R3-CREDENTIAL-AT-REST / blocking / credential-material store lacked an at-rest encryption contract
- DNRB-R3-PROVISIONING-DISPOSITION / blocking / provisioning helpers left as unresolved retain-or-relocate
- DNRB-R3-CONFIG-REVISION-TRANSPORT / blocking / grant/settings revision-equality contract missing end-to-end
- DNRB-R3-REMOTE-ENDPOINT-AUTH / blocking / bearer disclosed to unauthenticated remote endpoints at first contact
- DNRB-R3-RESTORE-REPLAY / blocking / backup restore can resurrect archived grants under repeated epoch and secret
- DNRB-R3-MACHINE-CLAIM / blocking / capability-token claims carry no machine_id to enforce the promised binding
- DNRB-R3-MANAGED-REFRESH / blocking / managed-source refresh destination and principal undefined
- DNRB-R3-ROTATION-OUTAGE / blocking / rotation can revoke predecessors inside the promised outage window
- DNRB-R3-LEASE-LOSS-FENCE / blocking / displaced daemon can serve effectful requests during takeover overlap
- DNRB-R3-PRUNE-RECOVERY / blocking / global prune lacked ordering, fan-out bounds, and partial-failure recovery
- resolution_notes: All 15 findings accepted (user: accept all, coordinator arms on 5/7/8/9/10/14). 5 narrows the E1 audit to qualified `gobby_core::runtime_mode` forms, leaving gwiki's unrelated system-model enum untouched. 7 resolves as whole-module deletion of gcore provisioning with nothing relocated — repo evidence shows its only consumers are the deleted standalone paths and the Python daemon's installer surface already owns Docker provisioning. 8 makes `config_revision` a signed first-class grant field carried through golden vectors, the runtime-config response, and a revision-coherent client cache pair. 9 makes first contact loopback-only with a new Constraints entry reserving remote access for the future hub-and-spoke registration boundary; `GrantError` gains `RemoteEndpoint`. 10 rotates the grant-signing secret atomically on every lease acquisition, folding operator invalidation into restart and defeating restore replay. 14 specifies an in-memory pre-handler live-lease guard, authoritative because the advisory lock dies with the lease connection. Repairs added acceptance items 1.1.8–1.1.10, 1.2.7, 1.3.12–1.3.15, 2.1.14–2.1.16, 2.2.13, 4.1.10, 5.1.6, and 6.1.11–6.1.13 (89 → 106), completed the test-target inventories across §§ 1.1–6.1, targeted the machine_id claim work at local_token.py and both issuers, and extended the fixture audit to the gcode/gcore config tests, the falkor live-test gate, gwiki's graph/cli_output fixtures, and the tool-chat passthrough assertions. Validating this round surfaced a real repo bug — the project's repo_path had been clobbered to an orphaned agent worktree, producing false INDEX_STALE errors — fixed in commit f99f96721 (task #19998) by refusing repo_path writes under the isolation roots. The user extended the review cap to 4; round 4 launches after this checkpoint in place of the cap handoff.

```json plan-review-round
{"evidence_id":"7c03ee65-9d86-4f3b-9721-558cd95ed702","plan_hash":"9678a85232d5c7d95005e73d0377720bd08965c1fd1edeeee87c5cd9aa85f779","round_number":3,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"dd4e161885741be1e6d7964eb7de6724efe685806c26c03aac669aec325c2249","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":0,"emitted_findings":15,"total":15},"evidence_id":"7c03ee65-9d86-4f3b-9721-558cd95ed702","lanes":[{"candidate_count":1,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":6,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":8,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":11,"manifest_digest":"178a1f67a9830552da9736ea1a5d7e4e55bbf1a04d96d312777facf4240fb96f","status":"valid"},"source_digest":"aa0608f428d56759e3e1c8ef56e467ba1d82d20f829af0f8ae243da620d90f68","version":1},"findings":[{"category":"missing-requirement","check_key":"daemon-lease-client-exclusivity","description":"Epic #18902 requires the active-daemon lease to remain exclusively daemon-owned, yet the plan never proves that gcode, gwiki, and gcore clients cannot acquire or reuse deployment_advisory_key(\"single-active-daemon\").","finding_id":"DNRB-R3-LEASE-EXCLUSIVITY","fix":"Add acceptance in 1.1 and an E2E or E1 zero-match proof that only the supervising Python daemon acquires the active-daemon advisory lease; feature clients obtain grants only.","location":"P1 / § 1.1, with E2E coverage in § 6.1","prevention":"For each single-owner lock, inventory every process class and require positive owner coverage plus negative acquisition audits for all non-owners.","principle":"A single-writer advisory lease needs one explicit acquisition owner and negative proof that every feature client remains outside that ownership boundary.","root_cause":"Lease acceptance covers epoch behavior and deployment scoping without specifying or auditing exclusive acquisition by the supervising daemon.","section_id":"1.1","severity":"blocking"},{"category":"traceability","check_key":"ai-routing-exhaustive-consumers","description":"Multiple production consumers of AiRouting::Direct, AiRouting::Auto, and DirectChatTransport remain outside the plan's Targets, including gwiki AI clients, generation and ingest paths, plus gcode symbols and service construction. Section 3.1 therefore cannot deliver its claimed exhaustive migration or compile independently.","finding_id":"DNRB-R3-AI-BLAST-RADIUS-REMAINDER","fix":"Expand 3.1 Targets to every production match, constructor, import, re-export, and affected test; classify each as daemon-only, Off-only, or deleted, move cross-section prerequisites into the correct dependency chain, and widen the zero-match audit to the full removed surface.","location":"P3 / § 3.1 through P4 / §§ 4.1–4.2","prevention":"Before approving a deletion, enumerate all symbol usages and textual variant references, assign every result to a section, then repeat the zero-match audit.","principle":"Deleting enum variants and transports requires exhaustive ownership of constructors, matches, imports, re-exports, and tests across the workspace.","root_cause":"The Targets inventory was repaired around core AI modules without sweeping the remaining gcode and gwiki production consumers.","section_id":"3.1","severity":"blocking"},{"category":"weak-testability","check_key":"removed-env-fixture-audit","description":"Active unowned test and helper sites still inject GOBBY_FALKORDB, QDRANT, gcore.yaml, and GWIKI_DATABASE_URL, including gcode and gcore config tests. The proposed final audit cannot establish that unsupported configuration paths are gone.","finding_id":"DNRB-R3-FIXTURE-AUDIT-REMAINDER","fix":"Add every matching test, helper, and fixture to exact Targets in the owning sections, convert or delete each injection site, and make E1 enumerate all removed names and paths.","location":"P6 / § 6.1, with owning work in §§ 3.1 and 4.1","prevention":"For every removed configuration name, inventory production reads, test writes, shared fixtures, command harnesses, and documentation examples before declaring a zero-match gate.","principle":"A removal audit is meaningful only when every production helper and test fixture that injects the removed surface is explicitly migrated or deleted.","root_cause":"The plan's final audit names removed variables while its Targets omit active fixture and helper injection sites.","section_id":"6.1","severity":"blocking"},{"category":"gobby-format","check_key":"target-inventory-completeness","description":"Required tests are absent from their owning Targets, including daemon lease, grant rejection/golden/active-binding, runtime handshake/config, managed credentials, Rust grant tests, auth/prune route tests, gcore AI tests, gwiki status tests, and their golden-vector artifacts.","finding_id":"DNRB-R3-TEST-TARGETS","fix":"Add each named test and golden artifact as an exact Target in its owning section, using bare paths only for new or indexed zero-symbol files and indexed symbols or justified wildcards for existing symbol-bearing files.","location":"§§ 1.1–1.3, 2.1–2.2, 3.1, 4.2, and 5.1","prevention":"Mechanically compare every file and test artifact referenced by acceptance items against the owning section's Targets before review.","principle":"Each expansion leaf must own every artifact named by its acceptance criteria because the implementing agent receives only that section.","root_cause":"Acceptance tests were added without adding their files or symbols to the corresponding Targets blocks.","section_id":"1.2","severity":"blocking"},{"category":"weak-testability","check_key":"zero-match-audit-specificity","description":"The planned repository-wide RuntimeMode zero-match assertion also matches gwiki's unrelated system-model RuntimeMode type, making the asserted empty result impossible after the intended gcore deletion.","finding_id":"DNRB-R3-AUDIT-SCOPE","fix":"Narrow the audit to the removed gcore module, qualified imports, and deleted variants, or explicitly target and rename the unrelated gwiki type if it is genuinely in scope.","location":"P4 / § 4.1 and E1","prevention":"Scope deletion audits to qualified imports, module paths, deleted variants, or an enumerated file set, and check generic-name collisions before adopting an empty-result assertion.","principle":"Zero-match audits must uniquely identify the removed contract and avoid unrelated domain symbols.","root_cause":"E1 uses a repository-wide word search for a generic type name.","section_id":"4.1","severity":"blocking"},{"category":"missing-requirement","check_key":"credential-material-at-rest","description":"The governing credential-isolation architecture allows plaintext only during issuance and runtime materialization, while this plan adds a persistent credential-material store without proving that invariant survives.","finding_id":"DNRB-R3-CREDENTIAL-AT-REST","fix":"Specify the existing KEK/DEK envelope, ciphertext-only storage, AAD binding to deployment, machine, project, and generation, least-privilege daemon access, plus backup, rotation, revocation, and cleanup tests.","location":"P1 / §§ 1.1 and 1.3","prevention":"Any design that persists secrets must state ciphertext format, encryption keys, AAD identity binding, authorized readers, rotation, backup, revocation, and deletion behavior.","principle":"Persisted credential material must preserve the governing plaintext-minimization and encryption-at-rest boundary.","root_cause":"The new credential-material store is specified as persistent storage without a representation, key hierarchy, binding, access, backup, or cleanup contract.","section_id":"1.1","severity":"blocking"},{"category":"traceability","check_key":"provisioning-relocation-disposition","description":"Section 4.1 leaves Docker and hub provisioning helpers as 'retained or relocated daemon-side' and later says to relocate them without naming destination modules, Cargo wiring, imports, or tests.","finding_id":"DNRB-R3-PROVISIONING-DISPOSITION","fix":"Choose one exact disposition per provisioning helper and add the destination file and symbol, feature and Cargo wiring, call-site and import migrations, and owning tests to 4.1.","location":"P4 / § 4.1","prevention":"For each removed module that contains retained behavior, record one exact destination and inventory its imports, feature wiring, call sites, and regression tests.","principle":"An execution plan must choose one disposition for every retained capability and identify its destination, wiring, consumers, and tests.","root_cause":"Docker and hub provisioning helpers are described with unresolved retain-or-relocate language.","section_id":"4.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"active-config-grant-snapshot","description":"A client can combine a grant issued from revision R1 with runtime settings fetched from R2, and the independent cache cannot reconstruct one coherent revision during daemon outage.","finding_id":"DNRB-R3-CONFIG-REVISION-TRANSPORT","fix":"Add config_revision to Python and Rust grant models and golden vectors; return it from runtime config; atomically cache grant plus matching settings through grant expiry; reject mismatches; replace both on renewal; add activation-race and cold-outage tests.","location":"P1 / §§ 1.2–1.3, P2 / § 2.1, and P6 / § 6.1","prevention":"Trace every versioned snapshot across serialization, transport, cache replacement, renewal, outage reuse, and concurrent activation tests.","principle":"One invocation must consume capabilities, secrets, and non-capability settings from one explicit configuration revision.","root_cause":"The prose says the revision is signed, while the strict grant shape, goldens, runtime-config response, and cache state omit a revision-equality contract.","section_id":"1.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"daemon-endpoint-first-contact-auth","description":"The remote first-contact path can disclose an operator/run bearer to a substituted daemon endpoint; the endpoint-to-deployment binding occurs only after that credential-bearing handshake.","finding_id":"DNRB-R3-REMOTE-ENDPOINT-AUTH","fix":"Constrain bootstrap and discovery to loopback or same-machine endpoints, reject non-loopback URLs before attaching auth headers, remove the remote first-contact promise, and test that remote HTTP receives no credentials while overridden local ports work.","location":"P1 / § 1.3 and P2 / § 2.1","prevention":"For each credential-bearing first-contact flow, establish transport and server authenticity before attaching authorization headers.","principle":"A bearer credential must never be presented to an endpoint whose server identity has not been authenticated.","root_cause":"Remote endpoint bootstrap sends the operator or run bearer before an authenticated deployment binding exists, while clients cannot validate the daemon's grant HMAC and plain HTTP remains accepted.","section_id":"2.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"fencing-restore-replay","description":"Restoring the database can repeat a prior epoch under the same deployment token and signing secret, resurrecting an archived unexpired grant.","finding_id":"DNRB-R3-RESTORE-REPLAY","fix":"Rotate the grant-signing secret freshly and atomically on every successful lease acquisition and epoch bump, then add a restore-and-reacquire test proving an archived grant is rejected.","location":"P1 / §§ 1.1–1.2 and P6 / § 6.1","prevention":"Exercise backup restore against every persisted fencing and signing component and prove archived capabilities remain invalid after reacquisition.","principle":"Fencing state restored from backup must never recreate an identity that validates a previously issued capability.","root_cause":"Deployment token, epoch, and signing secret can all roll back together, recreating a previously valid signed grant.","section_id":"1.1","severity":"blocking"},{"category":"missing-requirement","check_key":"bearer-grant-principal-binding","description":"The capability-token path cannot enforce the promised machine_id binding because its signed claim model and issuance call sites lack that field.","finding_id":"DNRB-R3-MACHINE-CLAIM","fix":"Add machine_id to the capability claim and every issuer call site, sign and verify it during handshake, reject absent and mismatched values, and add both rejection tests.","location":"P1 / § 1.3","prevention":"For every claim-to-request binding rule, inventory the claim schema, all issuers, all verifiers, absent-claim behavior, and mismatch tests.","principle":"An authorization equality check requires the asserted identity to exist in the signed claim issued by every supported bearer path.","root_cause":"The plan requires capability-token machine_id equality while AgentApiTokenClaims and its issuers carry no machine_id.","section_id":"1.3","severity":"blocking"},{"category":"unhandled-edge","check_key":"managed-grant-refresh-destination","description":"For stale or signature failures, the plan says to lock and atomically replace 'the cache' without defining where a managed-source grant is refreshed or which principal performs the replacement.","finding_id":"DNRB-R3-MANAGED-REFRESH","fix":"Define separate interactive and managed refresh flows: interactive renewal locks and rewrites the interactive cache; managed renewal locks and replaces the same managed file under the same principal. Add concurrency and destination tests for both.","location":"P1 / § 1.3, P2 / § 2.1, P5 / § 5.1, and P6 / § 6.1","prevention":"Model acquisition and refresh separately for each source and test concurrent refresh, destination integrity, principal preservation, and generation monotonicity.","principle":"Each credential source needs an explicit refresh state machine with a defined lock owner, destination, principal, and downgrade rule.","root_cause":"The plan prohibits managed grants from entering the interactive cache while later describing one generic locked cache replacement flow for every source.","section_id":"2.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"grant-credential-lifetime-and-generation","description":"A managed rotation can revoke the predecessor immediately, causing an otherwise unexpired cached grant to fail opaquely during the outage window the plan promises to support.","finding_id":"DNRB-R3-ROTATION-OUTAGE","fix":"Drain predecessor credentials until the latest issued grant for that generation expires, reserve early invalidation for explicit revocation with a stable typed error, and test rotation during outage plus explicit revocation.","location":"P1 / § 1.3, P2 / § 2.1, and P6 / § 6.1","prevention":"Align grant expiry with credential-generation drain and separately define explicit revocation's early-invalidation contract and typed error.","principle":"An unexpired offline capability must not outlive or ambiguously outlast the underlying credential generation it promises to use.","root_cause":"Cached-grant outage validity is specified independently from managed credential rotation and immediate predecessor revocation.","section_id":"1.3","severity":"blocking"},{"category":"unhandled-edge","check_key":"live-lease-loss-fencing","description":"After standby takeover, the old daemon can remain reachable briefly and accept a grant against its cached epoch before asynchronous lease-loss handling stops it.","finding_id":"DNRB-R3-LEASE-LOSS-FENCE","fix":"Add a pre-handler active-owner guard that validates the live lease session and authoritative epoch before effects, closes immediately on lease connection loss, and add a two-reachable-daemon takeover test.","location":"P1 / §§ 1.1–1.2, P5 / § 5.1, and P6 / § 6.1","prevention":"For every effectful handler, prove active lease ownership immediately before effects and test overlapping old and new endpoints during takeover.","principle":"A daemon that loses active ownership must close its effect boundary before a successor can make the old owner stale.","root_cause":"Request verification compares against cached epoch state while heartbeat-driven lease-loss shutdown is asynchronous.","section_id":"5.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"global-prune-partial-recovery","description":"Global prune can delete hub records before graph or vector cleanup completes, orphaning projections; child timeout and partial-failure behavior are unspecified.","finding_id":"DNRB-R3-PRUNE-RECOVERY","fix":"Specify snapshot-before-delete, bounded child concurrency and deadlines, durable dirty/retry records, structured completed/failed/skipped results, and timeout plus idempotent-retry E2E coverage.","location":"P2 / § 2.2 and P6 / § 6.1","prevention":"For every multi-resource destructive workflow, specify snapshot, ordering, idempotency key, concurrency bound, deadline, partial result, dirty marker, and retry test.","principle":"A global destructive operation needs idempotent ordering, bounded fan-out, durable partial-failure state, and retry semantics.","root_cause":"The plan names hub-record sweep and project children without defining snapshot order, failure isolation, deadlines, recovery records, or structured outcomes.","section_id":"2.2","severity":"blocking"}],"reviewer_session":"d511fb8e-0438-4feb-bd1b-7f5611fe4c7c","round":3,"round_number":3,"verdict":"needs_review"},"session_id":"31d4187b-0498-4783-813b-d72be21b8bd1"}
```

**Round 4** `kind: verification`

- reviewer_run: 657362d7-37be-4894-af91-e5b7d0f0afc6
- reviewer_session: 18ca1df3-30d8-45b5-9608-d2f6cc35e666
- verdict: needs_review
- findings:
- DNRB-R4-OFFLINE-SIGNATURE / blocking / offline grant loads authorize after structural checks alone
- DNRB-R4-BLOCKER-INHERITANCE / blocking / #19645 epic blocker not inherited by leaf dispatch eligibility
- DNRB-R4-CONTRACT-OWNERSHIP / blocking / 3.2 acceptance demanded snapshots owned exclusively by 4.1
- DNRB-R4-VERIFICATION-MATRIX / blocking / E1 omitted six acceptance-owning Python suites and the gdaemon contract test
- DNRB-R4-AUTH-DEPENDENCY / blocking / 5.1 lacked its 3.1 AiContext dependency edge
- DNRB-R4-CONFIG-REVISION-RACE / blocking / second revision mismatch after the single re-handshake had no terminal state
- DNRB-R4-GLOBAL-CLI-TARGETS / blocking / global prune/projects direct-DSN implementations untargeted
- DNRB-R4-ISOLATION-BOOTSTRAP / blocking / isolated-agent launcher still writes bootstrap.yaml DSNs untargeted
- DNRB-R4-AI-CONSUMERS / blocking / gwiki ingest/sources/transcribe/vision AiRouting consumers untargeted
- DNRB-R4-SETUP-CONSUMERS / blocking / gwiki setup mapping/admission/lib/setup.rs and gcode/gcore test seams untargeted
- DNRB-R4-MEMORY-FAKE-CONSUMERS / blocking / MemoryWikiStore test modules and the promised fake untargeted
- DNRB-R4-PUBLIC-DOCS / blocking / maintained gwiki/gcore docs still prescribe removed surfaces
- DNRB-R4-FIXTURE-CONTRACTS / blocking / Python code-index contract suites pin removed env/standalone behavior
- DNRB-R4-LOOPBACK-SERVER-AUTH / blocking / substituted loopback listener can harvest bearers at first contact
- DNRB-R4-LEASE-GUARD-TOCTOU / blocking / guard-to-effect race lets a displaced daemon commit under the old epoch
- DNRB-R4-MANAGED-REFRESH-AUTH / blocking / managed refresh had no authenticator for its replacement request
- DNRB-R4-OFFLINE-REVOCATION / blocking / typed offline revocation promised without an observable signal
- DNRB-R4-OFFLINE-RESTORE-REPLAY / blocking / absolute restore-replay rejection claim false for direct capabilities
- resolution_notes: All 18 findings accepted (user: agreed, with coordinator arms on 1/14/15). 1 lands as a first-class `payload_checksum` grant field verified by clients on every load from every source — integrity against corruption, never an authorization trust boundary; the HMAC stays daemon-only per Constraints, and local tampering is documented outside the threat model. 14 lands as a bearer-free challenge-response keyed by the caller's existing credential secret (operator token, or the capability token's daemon-recomputable signature) — server proof precedes any bearer with zero new key material, and endpoint bindings persist only after proof plus an authenticated handshake. 15 lands as epoch-scoped effect fencing: effectful hub writes validate the owned epoch against deployment_runtime in the same transaction, takeover drains predecessor in-flight work, and the unenforceable downstream-epoch-token clause for FalkorDB/Qdrant is declined with the residual window documented as connection-teardown-bounded. 17 and 18 narrow round-3 overpromises: typed Revoked is producible only at reachable-daemon presentation, and restore-replay rejection is a daemon-presentation guarantee with per-store direct-capability coverage. Repo verification corrected two adversary claims: crates/gwiki/src/code/types/ai.rs does not exist (the verified AiRouting consumer inventory replaces it) and project_admission lives at crates/gwiki/src/commands/project_admission.rs; the MemoryWikiStore inventory is larger than reported, including production typed parameters in support counts/graph/search. Repairs added acceptance items 1.2.8, 1.3.16–1.3.18, 2.1.17–2.1.20, 2.2.14, 4.1.11, 4.2.7, 5.1.7, and 6.1.14 (106 → 119), added 5.1's 3.1 dependency edge, made #19645 closure a hard expansion precondition, rewired 3.2/4.1 contract-snapshot ownership, and extended E1 with every acceptance-owning suite. This is the final round under the user-extended cap of 4; the human-handoff entry below records the cap state.

```json plan-review-round
{"evidence_id":"c7e605c9-21be-48ff-a5b3-e829bf2d04d2","plan_hash":"280f1f0dd56725775d96f7255af38a80fc85924ae94da41e547dae203a99b84e","round_number":4,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"c4f84744470d82ec6c34737993bdb61f087c2a2a3f130c018cc0b0cf50dce9d1","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":0,"emitted_findings":18,"total":18},"evidence_id":"c7e605c9-21be-48ff-a5b3-e829bf2d04d2","lanes":[{"candidate_count":6,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":7,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":5,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":11,"manifest_digest":"6a0dc2c3ad4aba489e4da523687231246512036263640d233a2c112120567c13","status":"valid"},"source_digest":"90f28c49a7ed5223af4ef158d07bb55bd6515eaa64afbfcfcc3edb4e1e437f69","version":1},"findings":[{"category":"missing-requirement","check_key":"client-offline-signature-verification","description":"Epic #18902 requires invalidly signed grants to be rejected, yet an offline cached or managed grant can construct direct datastore clients after structural validation alone. Corrupted payloads and signatures therefore remain authorizing on the outage path.","finding_id":"DNRB-R4-OFFLINE-SIGNATURE","fix":"Replace the daemon-only HMAC wire signature with a deployment-bound client-verifiable signature, pin its verification key in protected bootstrap state, verify every acquisition source before construction, and add corrupt-payload and corrupt-signature tests for both offline sources.","location":"P1 / § 1.2, P2 / §§ 2.1–2.2, and P6 / § 6.1","prevention":"Trace each rejection class through handshake, managed-file, cache, offline, renewal, and pre-request acquisition paths.","principle":"Every grant-consuming path must authenticate the signed payload before using its authority or connection material.","root_cause":"The daemon owns HMAC verification while offline cache and managed-file loads perform structural checks only.","section_id":"2.1","severity":"blocking"},{"category":"bad-sequencing","check_key":"ancestor-external-blocker-propagation","description":"The live #18902 → #19645 edge blocks the epic, yet generated leaves can become dispatchable after expansion because external ancestor blockers are not inherited by the leaf candidate filter.","finding_id":"DNRB-R4-BLOCKER-INHERITANCE","fix":"Make #19645 closure a hard precondition for applying or completing #18902 expansion, and state that no implementation leaf may be created or dispatched before that precondition passes.","location":"Constraints and all implementation sections","prevention":"For every external prerequisite, inspect expanded-leaf dispatch eligibility and prove the edge reaches each consumer.","principle":"A prerequisite must gate the executable leaves that consume it.","root_cause":"The plan assumes an epic external blocker is inherited by descendants, while dispatch evaluates direct blockers and a narrower ancestor-stage gate.","section_id":"1.1","severity":"blocking"},{"category":"traceability","check_key":"contract-regeneration-acceptance-ownership","description":"Acceptance 3.2.1 and 3.2.2 require regenerated snapshots, while 4.1 exclusively owns the final version bump, canonical JSON Targets, and exactly-once regeneration. The 3.2 leaf cannot satisfy its assigned acceptance independently.","finding_id":"DNRB-R4-CONTRACT-OWNERSHIP","fix":"Remove snapshot regeneration from 3.2.1 and 3.2.2, limiting them to source/in-memory CLI surface assertions; keep the version bump, canonical regeneration, mirror parity, and removed-surface checks solely in 4.1.5.","location":"P3 / § 3.2 and P4 / § 4.1","prevention":"Compare each acceptance verb and artifact with the owning section's Targets and the plan's exactly-once operations.","principle":"Each expanded leaf must own every artifact and action required by its acceptance items.","root_cause":"Regenerated-snapshot clauses remained in 3.2 after final regeneration ownership moved exclusively to 4.1.","section_id":"3.2","severity":"blocking"},{"category":"weak-testability","check_key":"acceptance-verification-matrix","description":"E1 omits the daemon-lease, managed-credential, runtime-config, dormant-wiki, global-prune, and auth-matrix Python suites, plus the focused gdaemon CLI identity contract. Material acceptance remains unexecuted by the declared gate.","finding_id":"DNRB-R4-VERIFICATION-MATRIX","fix":"Add focused commands for tests/test_daemon_lease.py, tests/storage/test_managed_credentials.py, tests/servers/routes/test_runtime_config.py, tests/servers/routes/test_wiki_code_routes.py, tests/servers/routes/test_code_index_prune_route.py, tests/servers/test_auth_service.py, and crates/gdaemon/tests/cli_contract.rs while retaining the repository's bounded-suite rule.","location":"E1, covering §§ 1.1, 1.3, 2.2, 5.1, and 6.1","prevention":"Build E1 mechanically from every test reference in deliverable acceptance, then deduplicate into bounded focused invocations.","principle":"Final verification must execute every acceptance-owning suite through focused, reproducible commands.","root_cause":"E1 names a narrow Python subset and three Rust packages without reconciling those commands with the acceptance test inventory.","section_id":"6.1","severity":"blocking"},{"category":"bad-sequencing","check_key":"ai-auth-context-dependency","description":"Manifest synthesis permits 3.1 and 5.1 to run in parallel, while 5.1 changes all modality operations to present the grant carried through the AiContext surface established by 3.1.","finding_id":"DNRB-R4-AUTH-DEPENDENCY","fix":"Add 3.1 to 5.1's dependencies, preserving 2.1 as the grant acquisition prerequisite, 3.1 as grant-bearing AiContext construction, and 5.1 as transport presentation and server enforcement.","location":"P3 / § 3.1 and P5 / § 5.1","prevention":"Trace each changed request context from construction through transport and add manifest edges for every producer-consumer seam.","principle":"A leaf must depend on the leaf that establishes the data shape and transport context it consumes.","root_cause":"5.1 depends only on grant acquisition in 2.1 although 3.1 owns grant-bearing AiContext and modality construction.","section_id":"5.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"active-config-grant-snapshot","description":"A configuration activation can occur after the one allowed re-handshake and before the replacement settings fetch, producing another revision mismatch. GrantError has no config-revision variant and the plan does not define whether the client retries, mixes revisions, or fails.","finding_id":"DNRB-R4-CONFIG-REVISION-RACE","fix":"After the single synchronized re-handshake, preserve the prior coherent cache and return a new ConfigRevisionMismatch error when the second settings revision still differs; add its stable CLI mapping and a barrier test with back-to-back activations.","location":"P1 / §§ 1.2–1.3, P2 / § 2.1, and P6 / § 6.1","prevention":"Place activation barriers before and after each request and enumerate stable, first-mismatch, second-mismatch, outage, and concurrent-reader outcomes.","principle":"A multi-request snapshot protocol needs a bounded terminal outcome for every interleaving.","root_cause":"Handshake and runtime-config are separate reads, while the plan specifies one recovery handshake and omits the second-mismatch state.","section_id":"2.1","severity":"blocking"},{"category":"traceability","check_key":"global-command-daemon-client-targets","description":"Global gcode prune and projects still construct direct PostgreSQL service contexts through prune.rs, projects.rs, shared.rs, and dispatch.rs. Those files are absent from 2.2 Targets, so the promised daemon-only global paths cannot land.","finding_id":"DNRB-R4-GLOBAL-CLI-TARGETS","fix":"Add crates/gcode/src/commands/status/prune.rs, projects.rs, shared.rs, prune/tests.rs, and the relevant crates/gcode/src/dispatch.rs symbols to 2.2; replace both global direct paths with typed daemon clients while keeping project-scoped grant execution separate.","location":"P2 / § 2.2","prevention":"For each brokered command, inventory dispatch, command implementation, shared resolver, direct constructor, and tests.","principle":"A daemon cutover must target both the new server route and every direct client implementation it replaces.","root_cause":"2.2 targets daemon prune services while omitting the global CLI implementations and shared direct-DSN helper.","section_id":"2.2","severity":"blocking"},{"category":"traceability","check_key":"managed-isolation-grant-bootstrap","description":"src/gobby/agents/code_index.py still extracts database_url, writes bootstrap.yaml, and launches gcode through a managed-bootstrap wrapper; neither this production path nor tests/agents/test_isolation.py is targeted.","finding_id":"DNRB-R4-ISOLATION-BOOTSTRAP","fix":"Target both files, materialize the same signed managed grant used by the gateways, delete scoped DSN and bootstrap.yaml generation, and update isolation tests to assert grant-file permissions, principal binding, and cleanup.","location":"P1 / § 1.3, P2 / § 2.2, and P6 / § 6.1","prevention":"Inventory all subprocess launchers, wrappers, preflights, and bootstrap writers for each removed credential surface.","principle":"A single grant-reader invariant must include every production launcher and preflight that materializes client credentials.","root_cause":"Gateway paths were migrated while the isolated-agent gcode preflight remained outside the blast-radius inventory.","section_id":"2.2","severity":"blocking"},{"category":"traceability","check_key":"ai-routing-exhaustive-consumers","description":"Unlisted gwiki production files still construct or exhaustively match AiRouting::Direct and AiRouting::Auto, including code/types/ai.rs, ingest image/session/video paths, sources, transcribe, and vision. The Daemon|Off collapse will not compile.","finding_id":"DNRB-R4-AI-CONSUMERS","fix":"Add every remaining production constructor and exhaustive match to 3.1 Targets, classify each path as Daemon, Off, or deletion, migrate its tests, and retain the workspace zero-match gate.","location":"P3 / § 3.1","prevention":"Resolve the changed enum, enumerate all textual and indexed usages workspace-wide, assign every result, then repeat the zero-match audit.","principle":"Deleting enum variants requires exhaustive ownership of constructors, matches, re-exports, and tests.","root_cause":"The repaired AI Target inventory still omits gwiki ingestion and source consumers of Direct and Auto.","section_id":"3.1","severity":"blocking"},{"category":"traceability","check_key":"standalone-removal-exhaustive-consumers","description":"gwiki cli/mapping.rs, project_admission.rs, lib.rs, and setup.rs retain Command::Setup construction, SetupOptions conversion/export, exhaustive matching, and StandaloneSetup implementation outside Targets; additional gcode/gcore setup test seams remain unowned.","finding_id":"DNRB-R4-SETUP-CONSUMERS","fix":"Add those gwiki files, delete setup.rs whole, and target crates/gcode/src/cli/tests.rs, cli/tests/setup.rs, test_env.rs, plus crates/gcore/tests/effective_config_process.rs for the corresponding removals.","location":"P4 / § 4.1","prevention":"Before deleting a module or command variant, enumerate imports, pub exports, From conversions, exhaustive matches, CLI tests, and process fixtures.","principle":"Whole-module deletion requires closure over module exports, conversions, constructors, exhaustive matches, and test seams.","root_cause":"Setup implementation files were targeted without the remaining gwiki/gcode/gcore consumers that make them reachable.","section_id":"4.1","severity":"blocking"},{"category":"traceability","check_key":"memory-store-test-consumers","description":"At least thirteen unlisted ingest and support test modules construct or wrap MemoryWikiStore, and no Target introduces the promised test-only WikiIndexStore fake. Deleting store/memory.rs will break test compilation.","finding_id":"DNRB-R4-MEMORY-FAKE-CONSUMERS","fix":"Add a concrete test-fake Target and every remaining consumer across ingest file/git/image/pdf/session/session_archive/url/video and support/config; migrate constructors and fields before deleting MemoryWikiStore.","location":"P4 / § 4.2","prevention":"Enumerate constructors, generic wrappers, support fixtures, and nested test modules before deleting a test-visible implementation.","principle":"Replacing a concrete fake with a trait-bound test fake requires every constructor and wrapper field to migrate before deletion.","root_cause":"4.2 promises a test-only fake while targeting only a small subset of MemoryWikiStore consumers.","section_id":"4.2","severity":"blocking"},{"category":"traceability","check_key":"cli-contract-artifacts-and-doc-consumers","description":"Maintained gwiki and gcore documentation still prescribes setup --standalone, direct/auto routing, removed DSN variables, bootstrap.yaml, and deleted runtime/setup modules.","finding_id":"DNRB-R4-PUBLIC-DOCS","fix":"Target and rewrite crates/gwiki/README.md; docs/guides/gwiki-user-guide.md, gwiki-development-guide.md, gcore-development-guide.md, gwiki-daemon-web.md, codewiki.md, and code-index.md; plus docs/contracts/gwiki-cli.md and gcode-cli.md, aligning all with the final grant-only daemon-required contract.","location":"P2 / § 2.2, P3 / § 3.2, and P4 / § 4.1","prevention":"For every removed command, flag, environment variable, and config path, sweep contract docs, READMEs, user guides, development guides, and daemon-web pages.","principle":"Removed public surfaces require updates to every maintained contract, README, user guide, and development guide that advertises them.","root_cause":"The documentation repair covered gcode-facing artifacts while leaving maintained gwiki and gcore pages outside Targets.","section_id":"4.1","severity":"blocking"},{"category":"weak-testability","check_key":"removed-env-fixture-audit","description":"tests/code_index/test_gcode_phase7_contract.py and test_gcode_storage_conformance.py still require FalkorDB environment layering, bootstrap.yaml, GCODE_DATABASE_URL, GOBBY_POSTGRES_DSN, and standalone mode, while neither suite is targeted or run by E1.","finding_id":"DNRB-R4-FIXTURE-CONTRACTS","fix":"Add both suites to Targets, rewrite their static expectations around signed grant capabilities, and mint managed grant fixtures in place of environment, standalone, and bootstrap injection.","location":"P2 / § 2.2 and P6 / § 6.1","prevention":"Search production, Rust tests, Python tests, fixtures, and static source-contract assertions for every removed name and file.","principle":"Removal validation must migrate every active fixture and static contract that injects the deleted configuration surface.","root_cause":"The crate-focused fixture audit excludes Python code-index suites that pin standalone and direct-environment behavior.","section_id":"6.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"daemon-endpoint-first-contact-auth","description":"A substituted process can own the configured loopback port while the daemon is absent. The client then sends the operator or run bearer over plain HTTP before authenticating that process, and the client cannot verify the fabricated HMAC grant it returns.","finding_id":"DNRB-R4-LOOPBACK-SERVER-AUTH","fix":"Add a bearer-free challenge-response using protected deployment bootstrap identity before sending operator or run credentials, persist endpoint bindings only after successful proof, and test that a substituted loopback listener receives neither bearer nor runtime grant.","location":"P1 / § 1.3 and P2 / § 2.1","prevention":"For every first-contact flow, test a substituted process at each permitted address and prove it receives no bearer before server proof succeeds.","principle":"Address locality does not authenticate the server process that receives a bearer credential.","root_cause":"The round-3 repair restricts first contact to loopback while the endpoint binding still derives from the credential-bearing response.","section_id":"2.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"live-lease-loss-fencing","description":"The lease connection can die after the pre-handler guard succeeds, releasing the advisory lock before the old handler mutates a datastore or calls an external broker. A successor can acquire a new epoch while the displaced daemon still performs the old effect.","finding_id":"DNRB-R4-LEASE-GUARD-TOCTOU","fix":"Define epoch-scoped effect fencing: datastore writes validate the current epoch in the same transaction, external requests carry downstream-enforced epoch/idempotency tokens, takeover cancels or drains predecessor in-flight work, and a barrier E2E drops the lease after guard success.","location":"P1 / § 1.1, P5 / § 5.1, and P6 / § 6.1","prevention":"Place a barrier between every ownership check and effect, drop the lease there, and prove the old epoch cannot commit or dispatch.","principle":"A fencing check must remain authoritative through the protected effect.","root_cause":"The round-3 repair checks lease liveness before handlers while effect commit or dispatch occurs after an unprotected race window.","section_id":"5.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"managed-grant-refresh-authenticator","description":"A stale or rotated managed grant cannot authenticate its own replacement, the managed grant file contains no run bearer, and current gcode/gwiki gateway child launches do not install request-scoped authentication state. \"Same execution principal\" is therefore unimplementable.","finding_id":"DNRB-R4-MANAGED-REFRESH-AUTH","fix":"Define the managed launch envelope to pass a separately scoped GOBBY_AGENT_API_TOKEN or tool token whose claims equal the grant principal and cover the child deadline; require it for managed refresh, forbid operator-token fallback, and test expiry and principal mismatch.","location":"P1 / § 1.3, P2 / § 2.1, P5 / § 5.1, and P6 / § 6.1","prevention":"For each acquisition source, record authenticator, claims, expiry, destination, fallback policy, and restart tests.","principle":"Each refresh state machine needs an explicit authenticator whose claims match the grant principal and lifetime.","root_cause":"The round-3 repair assigns managed refresh a destination and principal while omitting how the child authenticates the replacement request.","section_id":"2.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"grant-explicit-revocation-observability","description":"An offline client cannot learn daemon-side explicit revocation, and a PostgreSQL authentication/session failure cannot prove revocation rather than expiry, password failure, or transport error; direct FalkorDB/Qdrant paths offer no per-principal revocation signal. GrantError also lacks Revoked.","finding_id":"DNRB-R4-OFFLINE-REVOCATION","fix":"Reserve typed Revoked for requests presented to a reachable daemon, map backend-enforced invalidation during outage to the normal datastore-authorization error, remove the typed offline-revocation assertion from 6.1.13, and add the reachable-daemon Revoked mapping explicitly.","location":"P1 / §§ 1.2–1.3, P2 / § 2.1, and P6 / § 6.1","prevention":"For each revocation promise, identify the observer, authenticated state source, backend mapping, outage behavior, and ambiguity tests.","principle":"A stable typed error requires an authoritative signal that the consuming path can observe and distinguish.","root_cause":"The explicit-revocation repair promises a distinct offline result although direct clients possess only cached grants and backend credentials.","section_id":"1.3","severity":"blocking"},{"category":"unhandled-edge","check_key":"fencing-restore-replay","description":"Signing-secret rotation rejects archived grants at daemon presentation, yet offline clients do not verify HMAC and restored direct Qdrant/FalkorDB credentials are not bound to the new secret. The absolute restore-and-reacquire rejection claim is false for direct capabilities.","finding_id":"DNRB-R4-OFFLINE-RESTORE-REPLAY","fix":"Narrow 1.1.9 and the restore E2E to daemon presentation, explicitly state that offline direct authorization remains valid until grant expiry, and add separate restore coverage for PostgreSQL, FalkorDB, and Qdrant direct variants.","location":"P1 / §§ 1.1–1.2, P2 / § 2.1, and P6 / § 6.1","prevention":"Restore backups and exercise each brokered and direct capability separately before claiming archived-grant invalidation.","principle":"A replay defense must cover every authorization path named by its acceptance claim.","root_cause":"The round-3 repair rotates only daemon verification state while offline direct authorization intentionally ignores that state.","section_id":"1.1","severity":"blocking"}],"reviewer_session":"18ca1df3-30d8-45b5-9608-d2f6cc35e666","round":4,"round_number":4,"verdict":"needs_review"},"session_id":"31d4187b-0498-4783-813b-d72be21b8bd1"}
```

**Human handoff at review cap** `kind: verification`

The user-extended adversarial review cap (4 rounds) is reached with a `needs_review` verdict. All 18 final findings were individually voted and repaired above; no further adversary round launches. Continuation is an explicit human decision through the coordinator: continue interactively (further review requires the user to extend the cap again), hand off to build through the coordinator handoff derivation (`derive_plan_handoff_manifest` → `apply_plan_handoff_manifest` → expansion-mode validation → `gobby build`), or stop with this base-validated artifact in place. The #19645-closure expansion precondition in Constraints binds every continuation path.

## M1 Task Manifest
`kind: manifest`

```yaml
- title: Fencing epoch and lease identity unification
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: '1.1.1: `deployment_runtime` table exists with deployment token,
    monotonic fencing epoch, and signing secret; epoch increments exactly once per
    lease acquisition including promotion and stale recovery. file: `src/gobby/daemon_lease.py`.

    1.1.2: Lease advisory-lock keying uses `deployment_advisory_key`; the `hashtext`
    scheme is gone. symbol: `ActiveDaemonLease`.

    1.1.3: Two deployments sharing one database hold independent leases and epochs.
    test: `tests/test_daemon_lease.py::test_deployment_scoped_lease_and_epoch`.

    1.1.4: `deployment_runtime` DDL ships inside baseline 375 with idempotent guards;
    no numbered migration is added; `BASELINE_CHECKSUM`, the catalog manifest, and
    the packaged expected identity regenerate together in the stated order. file:
    `crates/gcore/assets/schema/baseline.sql`.

    1.1.5: gdaemon applies the refreshed baseline to fresh and existing hubs, and
    both embedded identity contract tests pass alongside Python expected-identity
    parity. test: `crates/gdaemon/tests/cli_contract.rs::version_json_reports_exact_schema_identity_contract`.

    1.1.6: Receipt classification recognizes the #19645 baseline receipt as the exact
    predecessor; fresh, predecessor, current, and arbitrary-mismatch states classify
    correctly. test: `crates/gcore/src/schema/runner_tests.rs::receipt_chain_advances_from_19645_baseline`.

    1.1.7: The complete interactive-principal and credential-material DDL is sealed
    in this fold before identity regeneration; 1.3 introduces no schema change. file:
    `crates/gcore/assets/schema/baseline.sql`.

    1.1.8: Only the supervising daemon acquires the active-daemon advisory lease;
    client crates contain zero advisory-lease references per the E1 audit. file: `src/gobby/daemon_lease.py`.

    1.1.9: The grant-signing secret rotates atomically with every lease acquisition
    and epoch bump; restore-and-reacquire rejects archived grants at daemon presentation,
    while offline direct authorization stays bounded by grant expiry. test: `tests/test_daemon_lease.py::test_signing_secret_rotates_on_acquisition`.

    1.1.10: The credential-material DDL is ciphertext-shaped: sealed columns carry
    ciphertext and AAD identity only, and no plaintext connection-material column
    exists in the baseline. file: `crates/gcore/assets/schema/baseline.sql`.'
  labels:
  - covers:daemon-native-runtime-boundary:1.1:1.1.1
  - covers:daemon-native-runtime-boundary:1.1:1.1.2
  - covers:daemon-native-runtime-boundary:1.1:1.1.3
  - covers:daemon-native-runtime-boundary:1.1:1.1.4
  - covers:daemon-native-runtime-boundary:1.1:1.1.5
  - covers:daemon-native-runtime-boundary:1.1:1.1.6
  - covers:daemon-native-runtime-boundary:1.1:1.1.7
  - covers:daemon-native-runtime-boundary:1.1:1.1.8
  - covers:daemon-native-runtime-boundary:1.1:1.1.9
  - covers:daemon-native-runtime-boundary:1.1:1.1.10
  tdd: true
  source_section: '1.1'
  implementation_domain: backend
- title: 'v2 grant bundle: schema, signing, and rejection matrix'
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  validation_criteria: '1.2.1: v2 bundle model exists with API contract version, deployment
    identity, integer-versioned schema identity, principal, tagged-union datastore
    + AI capabilities carrying complete direct connection material, epoch, expiry,
    and HMAC signature. file: `src/gobby/runtime_grants/schema.py`.

    1.2.2: Signing uses the deployment''s stored secret; verification is daemon-side
    only. symbol: `sign_grant`.

    1.2.3: Each rejection class (expired, bad signature, wrong deployment, wrong schema,
    wrong API contract, wrong capability, stale epoch, revoked) returns its own typed
    code. test: `tests/runtime_grants/test_rejection_matrix.py::test_each_rejection_class_is_typed`.

    1.2.4: The v1 bundle models and the `GET /api/config/service-capabilities` route
    are gone, including the service-capabilities contract tests introduced by #19645.
    file: `src/gobby/servers/routes/configuration_effective.py`.

    1.2.5: Golden serialization vectors exist for every capability variant and round-trip
    byte-identically in Python. test: `tests/runtime_grants/test_golden_vectors.py::test_grant_vectors_round_trip`.

    1.2.6: One issuance observes one active configuration revision; a grant never
    mixes capabilities and secrets across revisions, including under concurrent activation
    and failed rotation. test: `tests/runtime_grants/test_active_config_binding.py::test_single_revision_per_grant`.

    1.2.7: `config_revision` is a signed first-class grant field carrying the exact
    observed revision, present in every golden vector. test: `tests/runtime_grants/test_golden_vectors.py::test_config_revision_signed`.

    1.2.8: `payload_checksum` is a first-class serialized field over the canonical
    payload, present and pinned in every golden vector. test: `tests/runtime_grants/test_golden_vectors.py::test_payload_checksum_pinned`.'
  labels:
  - covers:daemon-native-runtime-boundary:1.2:1.2.1
  - covers:daemon-native-runtime-boundary:1.2:1.2.2
  - covers:daemon-native-runtime-boundary:1.2:1.2.3
  - covers:daemon-native-runtime-boundary:1.2:1.2.4
  - covers:daemon-native-runtime-boundary:1.2:1.2.5
  - covers:daemon-native-runtime-boundary:1.2:1.2.6
  - covers:daemon-native-runtime-boundary:1.2:1.2.7
  - covers:daemon-native-runtime-boundary:1.2:1.2.8
  tdd: true
  source_section: '1.2'
  implementation_domain: backend
- title: Handshake endpoint and interactive principals
  category: code
  task_type: feature
  depends_on:
  - '1.2'
  validation_criteria: '1.3.1: Handshake issues v2 grants to both operator-token and
    capability-token callers and is registered in the agent capability matrix. file:
    `src/gobby/servers/routes/runtime_handshake.py`.

    1.3.2: Interactive principals are scoped Postgres roles keyed (machine_id, project_id),
    reused across handshakes within TTL, revocable and rotatable via the existing
    manager surface. symbol: `ManagedCredentialManager.issue`.

    1.3.3: A handshake after a daemon restart returns a grant with the bumped fencing
    epoch, and a prior-epoch grant presented for a brokered operation is rejected
    typed. test: `tests/servers/routes/test_runtime_handshake.py::test_epoch_bump_rejects_prior_grants`.

    1.3.4: All three managed launchers launch children with pre-materialized grant
    files; no child reads `bootstrap.yaml` for a DSN. file: `src/gobby/gwiki_gateway.py`.

    1.3.5: `gobby_agent_auth` represents interactive owners with a unique `(deployment_token,
    machine_id, project_id)` active binding; same-key reuse, cross-project isolation,
    and same-database cross-deployment independence are tested. test: `tests/storage/test_managed_credentials.py::test_interactive_binding_uniqueness`.

    1.3.6: Issued grants never advertise validity beyond the underlying role''s `VALID
    UNTIL`, and concurrent handshakes for one principal serialize daemon-side. test:
    `tests/servers/routes/test_runtime_handshake.py::test_expiry_bounded_and_serialized`.

    1.3.7: The handshake router is exported, included in the built application, and
    registered with its intended auth dependency. test: `tests/servers/routes/test_runtime_handshake.py::test_route_registered_in_app`.

    1.3.8: Granted principals are equal to or narrower than verified bearer claims;
    body/claims mismatches and managed-source acquisition failures reject typed (fail-closed)
    for every bearer kind. test: `tests/servers/routes/test_runtime_handshake.py::test_bearer_claim_binding_matrix`.

    1.3.9: Interactive issue-or-reuse returns the same live-generation DSN across
    handshakes and daemon restarts; rotation and revocation atomically replace stored
    material. test: `tests/storage/test_managed_credentials.py::test_interactive_reuse_after_restart`.

    1.3.10: `GET /api/runtime/config` serves registered non-capability settings from
    the active configuration snapshot to grant-presenting callers, with operator and
    agent-run precedence pinned. test: `tests/servers/routes/test_runtime_config.py::test_grant_presenting_config_transport`.

    1.3.11: Dormant CodeWiki route outputs are byte-identical after the routing changes:
    status stays 200 with the pinned payload, refresh stays 409. test: `tests/servers/routes/test_wiki_code_routes.py::test_dormant_outputs_pinned`.

    1.3.12: Capability-token claims carry a signed machine_id from every issuer; the
    handshake verifies it and rejects absent or mismatched values typed. test: `tests/servers/routes/test_runtime_handshake.py::test_machine_claim_binding`.

    1.3.13: Rotation drains predecessor generations until the last grant issued against
    them expires; explicit revocation invalidates early with its own typed code distinct
    from expiry, surfaced at reachable-daemon presentation. test: `tests/storage/test_managed_credentials.py::test_rotation_drains_predecessor_generations`.

    1.3.14: Interactive credential material is stored ciphertext-only with AAD identity
    binding; plaintext never persists and retired generations are removed. test: `tests/storage/test_managed_credentials.py::test_credential_material_ciphertext_at_rest`.

    1.3.15: `GET /api/runtime/config` returns the active config_revision with its
    settings snapshot. test: `tests/servers/routes/test_runtime_config.py::test_config_revision_in_response`.

    1.3.16: The bearer-free challenge endpoint proves daemon knowledge of the caller''s
    credential secret over a client nonce for both bearer kinds; no credential attaches
    before proof succeeds. test: `tests/servers/routes/test_runtime_handshake.py::test_challenge_proof_before_bearer`.

    1.3.17: Every managed launcher installs a run-scoped capability token whose claims
    equal the grant principal and whose expiry covers the child deadline; managed
    refresh authenticates with it, and absent or mismatched envelope tokens reject
    typed with no operator fallback. test: `tests/servers/routes/test_runtime_handshake.py::test_managed_refresh_envelope_token`.

    1.3.18: The isolated-agent launcher materializes a signed managed grant, generates
    no per-run bootstrap.yaml and no scoped DSN, and isolation tests assert grant-file
    permissions, principal binding, and cleanup. test: `tests/agents/test_isolation.py::test_grant_file_isolation`.'
  labels:
  - covers:daemon-native-runtime-boundary:1.3:1.3.1
  - covers:daemon-native-runtime-boundary:1.3:1.3.2
  - covers:daemon-native-runtime-boundary:1.3:1.3.3
  - covers:daemon-native-runtime-boundary:1.3:1.3.4
  - covers:daemon-native-runtime-boundary:1.3:1.3.5
  - covers:daemon-native-runtime-boundary:1.3:1.3.6
  - covers:daemon-native-runtime-boundary:1.3:1.3.7
  - covers:daemon-native-runtime-boundary:1.3:1.3.8
  - covers:daemon-native-runtime-boundary:1.3:1.3.9
  - covers:daemon-native-runtime-boundary:1.3:1.3.10
  - covers:daemon-native-runtime-boundary:1.3:1.3.11
  - covers:daemon-native-runtime-boundary:1.3:1.3.12
  - covers:daemon-native-runtime-boundary:1.3:1.3.13
  - covers:daemon-native-runtime-boundary:1.3:1.3.14
  - covers:daemon-native-runtime-boundary:1.3:1.3.15
  - covers:daemon-native-runtime-boundary:1.3:1.3.16
  - covers:daemon-native-runtime-boundary:1.3:1.3.17
  - covers:daemon-native-runtime-boundary:1.3:1.3.18
  tdd: true
  source_section: '1.3'
  implementation_domain: backend
- title: 'gcore grant client: handshake, cache, renewal, typed errors'
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  - '1.2'
  - '1.3'
  validation_criteria: "2.1.1: Grant client resolves managed file \u2192 cache \u2192\
    \ handshake, validates structurally on every load, and caches at 0600 with atomic\
    \ replace. file: `crates/gcore/src/grant/cache.rs`.\n2.1.2: Renewal triggers past\
    \ half-TTL when the daemon is reachable and never blocks an invocation holding\
    \ an unexpired grant. symbol: `GrantBundle`.\n2.1.3: Schema-identity mismatch\
    \ between binary and grant fails typed before any datastore connection. test:\
    \ `crates/gcore/src/grant/tests.rs::schema_mismatch_refuses_construction`.\n2.1.4:\
    \ Expired grant with unreachable daemon yields `DaemonRequired`; unexpired grant\
    \ with unreachable daemon permits datastore paths and refuses AI paths. test:\
    \ `crates/gcore/src/grant/tests.rs::outage_window_semantics`.\n2.1.5: The grant\
    \ module is exported from the crate root; both binaries build with `--no-default-features`\
    \ with grant acquisition and datastore construction intact, and cargo-tree assertions\
    \ prove the AI dependency stack is absent from those graphs. file: `crates/gcore/src/lib.rs`.\n\
    2.1.6: Cache location derives from local state with cross-language deployment-token\
    \ parity; loads validate deployment, project, machine, principal kind, and source;\
    \ managed grants are never written to the interactive cache. test: `crates/gcore/src/grant/tests.rs::managed_grant_never_overwrites_interactive_cache`.\n\
    2.1.7: Concurrent renewals serialize on the per-cache lock and never replace a\
    \ newer credential generation with an older one. test: `crates/gcore/src/grant/tests.rs::concurrent_renewal_refuses_downgrade`.\n\
    2.1.8: `inspect_cached_grant` classifies absent/malformed/valid/expiring/expired\
    \ without authorizing construction or exposing secrets. test: `crates/gcore/src/grant/tests.rs::inspect_is_non_authorizing`.\n\
    2.1.9: Rust deserializes the 1.2 golden vectors byte-identically for every capability\
    \ variant. test: `crates/gcore/src/grant/tests.rs::golden_vectors_match_python`.\n\
    2.1.10: Every acquisition path validates `api_contract` against the client's expected\
    \ contract; mismatch yields `ApiContractMismatch` with its stable CLI mapping;\
    \ golden vectors cover old-client/new-grant. test: `crates/gcore/src/grant/tests.rs::api_contract_gate`.\n\
    2.1.11: Cache selection follows the trusted endpoint\u2192deployment binding:\
    \ local derivation verified against handshake metadata, handshake-before-cache\
    \ on unbound endpoints, rebind only via authenticated handshake, persisted binding\
    \ under outage. test: `crates/gcore/src/grant/tests.rs::endpoint_deployment_binding`.\n\
    2.1.12: Renewal contention is bounded: zero-wait try-lock with immediate serve\
    \ of the unexpired grant, shared deadline, post-acquisition re-read, stale-lock\
    \ takeover, and typed `Timeout` on exhaustion. test: `crates/gcore/src/grant/tests.rs::bounded_renewal_contention`.\n\
    2.1.13: The machine-config client fetches registered non-capability settings with\
    \ grant presentation; capabilities and connection material remain grant-only.\
    \ file: `crates/gcore/src/config/machine_config.rs`.\n2.1.14: Non-loopback daemon\
    \ URLs are rejected typed (`RemoteEndpoint`) before any credential attaches; overridden\
    \ local ports handshake normally. test: `crates/gcore/src/grant/tests.rs::remote_endpoint_refused_before_auth`.\n\
    2.1.15: Managed-source refresh locks and replaces the managed grant file under\
    \ the same execution principal; interactive refresh owns the interactive cache;\
    \ the destinations never cross under concurrency. test: `crates/gcore/src/grant/tests.rs::refresh_destination_by_source`.\n\
    2.1.16: Grant and machine-config settings cache and replace as one revision-coherent\
    \ unit; a revision mismatch triggers exactly one synchronized re-handshake replacing\
    \ both; a cold start under outage serves the cached pair or fails typed. test:\
    \ `crates/gcore/src/grant/tests.rs::config_revision_coherence`.\n2.1.17: Every\
    \ acquisition source verifies the canonical-payload checksum before construction;\
    \ corrupted payloads and corrupted checksums fail `Malformed` for both offline\
    \ sources. test: `crates/gcore/src/grant/tests.rs::corrupt_grant_refused_offline`.\n\
    2.1.18: A second revision mismatch after the single re-handshake preserves the\
    \ prior coherent pair and fails typed `ConfigRevisionMismatch`; a barrier test\
    \ pins back-to-back activations. test: `crates/gcore/src/grant/tests.rs::config_revision_second_mismatch_terminal`.\n\
    2.1.19: On endpoints without a trusted binding the challenge proof precedes any\
    \ bearer; a substituted loopback listener receives neither bearer nor trusted\
    \ binding nor accepted grant. test: `crates/gcore/src/grant/tests.rs::substituted_listener_gets_no_bearer`.\n\
    2.1.20: Managed-source refresh authenticates with the launch-envelope token; expired\
    \ envelopes and principal mismatches fail typed with no operator-token fallback.\
    \ test: `crates/gcore/src/grant/tests.rs::managed_refresh_envelope_auth`."
  labels:
  - covers:daemon-native-runtime-boundary:2.1:2.1.1
  - covers:daemon-native-runtime-boundary:2.1:2.1.2
  - covers:daemon-native-runtime-boundary:2.1:2.1.3
  - covers:daemon-native-runtime-boundary:2.1:2.1.4
  - covers:daemon-native-runtime-boundary:2.1:2.1.5
  - covers:daemon-native-runtime-boundary:2.1:2.1.6
  - covers:daemon-native-runtime-boundary:2.1:2.1.7
  - covers:daemon-native-runtime-boundary:2.1:2.1.8
  - covers:daemon-native-runtime-boundary:2.1:2.1.9
  - covers:daemon-native-runtime-boundary:2.1:2.1.10
  - covers:daemon-native-runtime-boundary:2.1:2.1.11
  - covers:daemon-native-runtime-boundary:2.1:2.1.12
  - covers:daemon-native-runtime-boundary:2.1:2.1.13
  - covers:daemon-native-runtime-boundary:2.1:2.1.14
  - covers:daemon-native-runtime-boundary:2.1:2.1.15
  - covers:daemon-native-runtime-boundary:2.1:2.1.16
  - covers:daemon-native-runtime-boundary:2.1:2.1.17
  - covers:daemon-native-runtime-boundary:2.1:2.1.18
  - covers:daemon-native-runtime-boundary:2.1:2.1.19
  - covers:daemon-native-runtime-boundary:2.1:2.1.20
  tdd: true
  source_section: '2.1'
  implementation_domain: backend
- title: Gate service construction; collapse DSN resolution
  category: code
  task_type: feature
  depends_on:
  - '2.1'
  validation_criteria: '2.2.1: gcode resolves its DSN exclusively through the grant
    client; the env/daemon/bootstrap/gcore.yaml resolution ladder is gone. file: `crates/gcode/src/db/resolution.rs`.

    2.2.2: gwiki resolves identically, including honoring managed grant files. file:
    `crates/gwiki/src/support/env.rs`.

    2.2.3: `CodewikiFacts` connections come from the grant-resolved context; the facade
    API is unchanged. symbol: `CodewikiFacts`.

    2.2.4: The boundary test additionally forbids env-DSN and bootstrap-DSN resolution
    inside the moved engine. test: `crates/gwiki/tests/code_engine_boundary.rs::moved_engine_uses_only_facade`.

    2.2.5: With no grant and no daemon, both binaries fail with the typed daemon-required
    error before touching any datastore. behavior: "daemon required" in `docs/guides/ai-configuration.md`.

    2.2.6: Default path/project-file dispatch resolves the local project UUID to a
    project grant; no pre-grant datastore access exists on any dispatch path. test:
    `crates/gcode/tests/grant_errors.rs::no_pregrant_datastore_access`.

    2.2.7: Every grant error class has a stable JSON code, message, and exit status
    in both CLIs. test: `crates/gcode/tests/grant_errors.rs::grant_errors_stable_contract`.

    2.2.8: `--project <name>` resolves through the authenticated daemon lookup and
    then the handshake; the lookup precedes every datastore touch. test: `crates/gcode/tests/grant_errors.rs::project_name_lookup_authenticated`.

    2.2.9: `gcode projects` dispatches to the operator-only daemon listing; capability-token
    and anonymous calls reject typed. test: `tests/servers/test_auth_service.py::test_projects_listing_operator_only`.

    2.2.10: Global `gcode prune` triggers `POST /api/code-index/prune` as an operator-only
    route; the daemon decomposes it into the hub-record sweep plus per-project children
    carrying project-scoped grants; capability-token calls reject typed. test: `tests/servers/routes/test_code_index_prune_route.py::test_global_prune_operator_only`.

    2.2.11: `gcode prune --project` resolves through the ordinary project-grant path.
    test: `crates/gcode/tests/grant_errors.rs::project_prune_uses_project_grant`.

    2.2.12: Dispatch outside any project with no `--project` rejects typed before
    any datastore access or non-operator daemon call. test: `crates/gcode/tests/grant_errors.rs::projectless_rejection`.

    2.2.13: Global prune snapshots before deleting, reconciles projections before
    hub-record removal, bounds child concurrency and deadlines, records durable dirty/retry
    markers on child failure or timeout, and returns structured per-project outcomes;
    an idempotent retry converges. test: `tests/servers/routes/test_code_index_prune_route.py::test_partial_failure_recovery`.

    2.2.14: The global prune and projects CLI implementations and the shared direct-DSN
    helper contain no direct datastore construction; both global commands dispatch
    through typed daemon clients. test: `crates/gcode/src/commands/status/prune/tests.rs::global_prune_uses_daemon_client`.'
  labels:
  - covers:daemon-native-runtime-boundary:2.2:2.2.1
  - covers:daemon-native-runtime-boundary:2.2:2.2.2
  - covers:daemon-native-runtime-boundary:2.2:2.2.3
  - covers:daemon-native-runtime-boundary:2.2:2.2.4
  - covers:daemon-native-runtime-boundary:2.2:2.2.5
  - covers:daemon-native-runtime-boundary:2.2:2.2.6
  - covers:daemon-native-runtime-boundary:2.2:2.2.7
  - covers:daemon-native-runtime-boundary:2.2:2.2.8
  - covers:daemon-native-runtime-boundary:2.2:2.2.9
  - covers:daemon-native-runtime-boundary:2.2:2.2.10
  - covers:daemon-native-runtime-boundary:2.2:2.2.11
  - covers:daemon-native-runtime-boundary:2.2:2.2.12
  - covers:daemon-native-runtime-boundary:2.2:2.2.13
  - covers:daemon-native-runtime-boundary:2.2:2.2.14
  tdd: true
  source_section: '2.2'
  implementation_domain: backend
- title: Collapse gcore AI routing to daemon-only
  category: code
  task_type: feature
  depends_on:
  - '2.1'
  validation_criteria: '3.1.1: `AiRouting` has exactly Daemon and Off variants; Auto
    and Direct are unrepresentable. symbol: `AiRouting`.

    3.1.2: Direct transports, one-shot direct generation, and probe-based discovery
    are deleted. file: `crates/gcore/src/ai/probe.rs`.

    3.1.3: No client crate reads vendor API-key environment variables. test: `crates/gcore/src/ai/tests.rs::no_vendor_env_key_reads`.

    3.1.4: Modality gating reads grant capabilities; a grant lacking a modality yields
    the typed unavailable error without an HTTP roundtrip. test: `crates/gcore/src/ai/tests.rs::grant_gates_modalities`.

    3.1.5: Every workspace consumer of the removed variants and transports is migrated
    or deleted; the workspace zero-match audit for direct/auto routing, DirectChatTransport,
    and vendor key names passes. file: `crates/gcore/src/ai/generation/mod.rs`.

    3.1.6: Hybrid search during a daemon outage returns lexical and graph results
    with a structured semantic-degradation warning; the silent empty-source degrade
    path is gone; explicit AI commands still fail typed. test: `crates/gcode/src/commands/search.rs::outage_degrades_with_warning`.'
  labels:
  - covers:daemon-native-runtime-boundary:3.1:3.1.1
  - covers:daemon-native-runtime-boundary:3.1:3.1.2
  - covers:daemon-native-runtime-boundary:3.1:3.1.3
  - covers:daemon-native-runtime-boundary:3.1:3.1.4
  - covers:daemon-native-runtime-boundary:3.1:3.1.5
  - covers:daemon-native-runtime-boundary:3.1:3.1.6
  tdd: true
  source_section: '3.1'
  implementation_domain: backend
- title: Remove CLI routing surfaces; bump contracts; deterministic outline
  category: code
  task_type: feature
  depends_on:
  - '3.1'
  validation_criteria: '3.2.1: gwiki exposes no routing flags beyond `--no-ai` in
    its source and in-memory CLI surface; the final canonical snapshot, version bump,
    and mirror parity are owned solely by 4.1.5. file: `crates/gwiki/src/contract.rs`.

    3.2.2: `gcode outline` has no summarize surface in its source and in-memory CLI
    definition; the final canonical snapshot, version bump, and mirror parity are
    owned solely by 4.1.5. file: `crates/gcode/src/contract.rs`.

    3.2.3: gwiki''s probe module is deleted and no status-route body parsing remains
    for availability decisions. file: `crates/gwiki/src/daemon.rs`.

    3.2.4: Documentation describes the daemon-only contract including outage semantics.
    behavior: "daemon-only AI routing" in `docs/guides/ai-configuration.md`.'
  labels:
  - covers:daemon-native-runtime-boundary:3.2:3.2.1
  - covers:daemon-native-runtime-boundary:3.2:3.2.2
  - covers:daemon-native-runtime-boundary:3.2:3.2.3
  - covers:daemon-native-runtime-boundary:3.2:3.2.4
  tdd: true
  source_section: '3.2'
  implementation_domain: backend
- title: Remove standalone mode and local credential ownership
  category: code
  task_type: feature
  depends_on:
  - '3.1'
  - '3.2'
  - '2.2'
  validation_criteria: '4.1.1: gcore''s `runtime_mode` module, `StandaloneConfig`,
    and every gcore.yaml read are gone from the client crates; gwiki''s unrelated
    system-model `RuntimeMode` documentation enum is untouched. file: `crates/gcore/src/runtime_mode.rs`.

    4.1.2: No client crate contains KEK unwrap or `$secret:` resolution. file: `crates/gcore/src/secrets.rs`.

    4.1.3: `gcode setup` standalone surface is removed with its contract entries.
    file: `crates/gcode/src/commands/setup.rs`.

    4.1.4: `ConfigSource` retains its trait shape with grant-backed implementors.
    symbol: `ConfigSource`.

    4.1.5: Final contract versions (gcode 4, gwiki 17) land exactly once with regenerated
    snapshots and passing pinned-mirror parity. file: `crates/gcode/contract/gcode.contract.json`.

    4.1.6: Client-crate sources contain zero qualified references to the removed surfaces
    (`gobby_core::runtime_mode`, `StandaloneConfig`, `gcore.yaml`, env DSNs, `$secret:`)
    per the E1 audit, which is scoped to exclude gwiki''s unrelated system-model RuntimeMode.
    file: `crates/gcore/src/runtime_mode.rs`.

    4.1.7: The gcode and gwiki READMEs, the maintained gwiki/gcore/codewiki/code-index
    guides, the gwiki daemon-web page, and the gcode/gwiki CLI contract docs describe
    only grant-based resolution and daemon-only AI; no maintained page advertises
    standalone setup, direct/auto routing, removed DSN variables, or client bootstrap.yaml
    credentials. file: `crates/gcode/README.md`.

    4.1.8: The standalone-precedence regression arm added by #19645 is deleted with
    the mode; surviving runtime-contract tests assert grant-backed authority. file:
    `crates/gcode/src/config/tests/runtime_contract.rs`.

    4.1.9: No module root or public re-export keeps a standalone surface compiled
    or reachable; gcore''s crate root, gcode''s setup module root, and gwiki''s programmatic
    API drop their standalone declarations. file: `crates/gcore/src/lib.rs`.

    4.1.10: The gcore provisioning module, its embedded compose template, and their
    tests are deleted with nothing relocated; the Python installer surface remains
    the sole provisioning owner and the gwiki system-model fixture drops the deleted
    asset reference. file: `crates/gcore/src/provisioning/mod.rs`.

    4.1.11: No `Command::Setup` construction, `SetupOptions` conversion or export,
    exhaustive setup match, or `StandaloneSetup` implementation survives anywhere
    in the workspace; the gcode and gcore setup test seams convert to grant fixtures
    or die with the mode. file: `crates/gwiki/src/cli/mapping.rs`.'
  labels:
  - covers:daemon-native-runtime-boundary:4.1:4.1.1
  - covers:daemon-native-runtime-boundary:4.1:4.1.2
  - covers:daemon-native-runtime-boundary:4.1:4.1.3
  - covers:daemon-native-runtime-boundary:4.1:4.1.4
  - covers:daemon-native-runtime-boundary:4.1:4.1.5
  - covers:daemon-native-runtime-boundary:4.1:4.1.6
  - covers:daemon-native-runtime-boundary:4.1:4.1.7
  - covers:daemon-native-runtime-boundary:4.1:4.1.8
  - covers:daemon-native-runtime-boundary:4.1:4.1.9
  - covers:daemon-native-runtime-boundary:4.1:4.1.10
  - covers:daemon-native-runtime-boundary:4.1:4.1.11
  tdd: true
  source_section: '4.1'
  implementation_domain: backend
- title: Remove MemoryWikiStore and daemon-optional wiki modes
  category: code
  task_type: feature
  depends_on:
  - '2.2'
  validation_criteria: "4.2.1: `MemoryWikiStore` and every hubless fallback selection\
    \ are deleted. file: `crates/gwiki/src/store/memory.rs`.\n4.2.2: `WikiIndexStore`\
    \ trait shape is unchanged. symbol: `WikiIndexStore`.\n4.2.3: `gwiki status` surfaces\
    \ grant validity, deployment token, epoch, and daemon reachability. test: `crates/gwiki/tests/status_grant_state.rs::status_reports_grant_state`.\n\
    4.2.4: Every former MemoryWikiStore production consumer compiles against the trait\
    \ with its stated disposition; unit tests use the test-only fake. file: `crates/gwiki/src/store.rs`.\n\
    4.2.5: `gwiki status`, help, and contract output work with an expired or absent\
    \ grant, reporting state via the non-authorizing inspector. test: `crates/gwiki/tests/status_grant_state.rs::expired_grant_reports_not_fails`.\n\
    4.2.6: `audio.rs` finishes below 1,000 lines with its test module extracted; every\
    \ production file touched by this leaf ends below the ceiling. file: `crates/gwiki/src/ingest/audio/tests.rs`.\n\
    4.2.7: Every workspace `MemoryWikiStore` constructor, wrapper, and typed parameter\
    \ is migrated \u2014 production to the trait, tests to the fake \u2014 before\
    \ `store/memory.rs` is deleted; the workspace zero-match audit passes. file: `crates/gwiki/src/store/test_fake.rs`."
  labels:
  - covers:daemon-native-runtime-boundary:4.2:4.2.1
  - covers:daemon-native-runtime-boundary:4.2:4.2.2
  - covers:daemon-native-runtime-boundary:4.2:4.2.3
  - covers:daemon-native-runtime-boundary:4.2:4.2.4
  - covers:daemon-native-runtime-boundary:4.2:4.2.5
  - covers:daemon-native-runtime-boundary:4.2:4.2.6
  - covers:daemon-native-runtime-boundary:4.2:4.2.7
  tdd: true
  source_section: '4.2'
  implementation_domain: backend
- title: Bind identity on daemon AI and broker routes
  category: code
  task_type: feature
  depends_on:
  - '2.1'
  - '3.1'
  validation_criteria: '5.1.1: All gcore daemon transports attach the signed grant
    and grant-derived identity. file: `crates/gcore/src/ai/daemon/transport.rs`.

    5.1.2: AI and broker capability-matrix rows require grant presentation and identity
    binding; anonymous calls 401. test: `tests/servers/test_auth_service.py::test_ai_routes_require_identity`.

    5.1.3: Savings and graph-lifecycle calls carry identity and succeed under the
    new matrix. symbol: `report_savings`.

    5.1.4: Each of the five modalities plus `embeddings_doctor` has its own presentation-binding
    test; forged identity headers under a valid operator token are rejected typed.
    test: `tests/servers/test_auth_service.py::test_modality_grant_presentation_matrix`.

    5.1.5: Stale-epoch presentation triggers exactly one synchronized re-handshake
    and retry; exhaustion yields the typed rejection. test: `crates/gcore/src/grant/tests.rs::stale_epoch_single_retry`.

    5.1.6: Every effectful route validates live lease ownership pre-handler; lease-connection
    loss immediately stops effectful service; a displaced daemon rejects during takeover
    overlap. test: `tests/servers/test_auth_service.py::test_effectful_requires_live_lease`.

    5.1.7: Effectful hub writes validate the owned epoch in-transaction; a lease lost
    between guard and commit cannot commit; takeover drains predecessor in-flight
    work before serving. test: `tests/servers/test_auth_service.py::test_in_transaction_epoch_fencing`.'
  labels:
  - covers:daemon-native-runtime-boundary:5.1:5.1.1
  - covers:daemon-native-runtime-boundary:5.1:5.1.2
  - covers:daemon-native-runtime-boundary:5.1:5.1.3
  - covers:daemon-native-runtime-boundary:5.1:5.1.4
  - covers:daemon-native-runtime-boundary:5.1:5.1.5
  - covers:daemon-native-runtime-boundary:5.1:5.1.6
  - covers:daemon-native-runtime-boundary:5.1:5.1.7
  tdd: true
  source_section: '5.1'
  implementation_domain: backend
- title: Boundary end-to-end suite
  category: test
  task_type: feature
  depends_on:
  - '4.1'
  - '4.2'
  - '5.1'
  validation_criteria: '6.1.1: The core six scenarios pass against an isolated daemon.
    test: `tests/e2e/test_runtime_boundary.py::test_runtime_boundary_scenarios`.

    6.1.2: No scenario leaves a binary in a fallback mode; every failure is the typed
    daemon-required or rejection-matrix error. behavior: "typed failure contract"
    in `docs/guides/ai-configuration.md`.

    6.1.3: No test injects any removed credential or endpoint variable (`GCODE_DATABASE_URL`,
    `GWIKI_DATABASE_URL`, `GOBBY_POSTGRES_DSN`, `GOBBY_FALKORDB_*`, `GOBBY_QDRANT_*`)
    or writes `gcore.yaml`; shared fixtures provision schema and grants through supported
    paths only. file: `crates/gwiki/tests/common/mod.rs`.

    6.1.4: Automatic daemon-side symbol summarization and gcode summary retrieval
    hold under the boundary. test: `tests/e2e/test_runtime_boundary.py::test_symbol_summary_regression`.

    6.1.5: All five AI modalities bind identity end-to-end under the grant boundary.
    test: `tests/e2e/test_runtime_boundary.py::test_modality_identity_binding`.

    6.1.6: Concurrent renewal across processes preserves the newest generation and
    never blocks a valid invocation. test: `tests/e2e/test_runtime_boundary.py::test_concurrent_renewal_race`.

    6.1.7: Broker-scope paths behave per the command-scope table: projects listing,
    global prune decomposition including partial-failure retry convergence, project
    prune, and capability-token rejection. test: `tests/e2e/test_runtime_boundary.py::test_broker_scope_paths`.

    6.1.8: Diagnostics report state under expired or absent grants without acquiring.
    test: `tests/e2e/test_runtime_boundary.py::test_diagnostics_under_expiry`.

    6.1.9: Hybrid search during outage degrades with the exact structured warning;
    explicit AI commands fail typed in the same window. test: `tests/e2e/test_runtime_boundary.py::test_search_degrades_with_warning`.

    6.1.10: Dormant CodeWiki routes return byte-identical outputs under the boundary.
    test: `tests/e2e/test_runtime_boundary.py::test_dormant_codewiki_unchanged`.

    6.1.11: Restoring the hub database from backup and reacquiring the lease rejects
    archived grants at daemon presentation via the fresh signing secret; offline direct
    authorization on an unexpired archived grant is exercised separately for the PostgreSQL,
    FalkorDB, and Qdrant direct variants and stays bounded by grant expiry. test:
    `tests/e2e/test_runtime_boundary.py::test_restore_replay_rejected`.

    6.1.12: During standby-takeover overlap the displaced daemon refuses effectful
    requests immediately while the new owner serves; dropping the lease after a successful
    pre-handler guard cannot produce a committed effect under the old epoch. test:
    `tests/e2e/test_runtime_boundary.py::test_takeover_fencing`.

    6.1.13: Rotation during outage drains the predecessor generation until issued-grant
    expiry; explicit revocation presented to a reachable daemon fails early with the
    typed revoked code, while outage-window backend invalidation surfaces as the ordinary
    datastore-authorization error. test: `tests/e2e/test_runtime_boundary.py::test_rotation_drain_and_revocation`.

    6.1.14: The gcode phase-7 contract and storage-conformance suites run green on
    grant fixtures with zero references to removed variables, client bootstrap.yaml
    credentials, or standalone mode. test: `tests/code_index/test_gcode_phase7_contract.py::test_contract_on_grant_fixtures`.'
  labels:
  - covers:daemon-native-runtime-boundary:6.1:6.1.1
  - covers:daemon-native-runtime-boundary:6.1:6.1.2
  - covers:daemon-native-runtime-boundary:6.1:6.1.3
  - covers:daemon-native-runtime-boundary:6.1:6.1.4
  - covers:daemon-native-runtime-boundary:6.1:6.1.5
  - covers:daemon-native-runtime-boundary:6.1:6.1.6
  - covers:daemon-native-runtime-boundary:6.1:6.1.7
  - covers:daemon-native-runtime-boundary:6.1:6.1.8
  - covers:daemon-native-runtime-boundary:6.1:6.1.9
  - covers:daemon-native-runtime-boundary:6.1:6.1.10
  - covers:daemon-native-runtime-boundary:6.1:6.1.11
  - covers:daemon-native-runtime-boundary:6.1:6.1.12
  - covers:daemon-native-runtime-boundary:6.1:6.1.13
  - covers:daemon-native-runtime-boundary:6.1:6.1.14
  tdd: false
  source_section: '6.1'
  assigned_agent: backend-developer
```
