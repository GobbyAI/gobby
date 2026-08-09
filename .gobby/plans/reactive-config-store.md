# Unify Runtime Configuration Behind a Reactive ConfigStore API

> **Plan ID:** reactive-config-store

## Overview
`kind: framing`

Make PostgreSQL ConfigStore the typed authority for registered post-database runtime
settings across Python and Rust Gobby runtime modes. Provide revisioned atomic writes,
immutable desired/active snapshots, secret-safe public interfaces, authenticated machine
configuration, cross-daemon reconciliation, and explicit live/restart/managed activation.

Final cutover removes `load_config`, `load_full_config_from_db`, `runner.config`,
`ServiceContainer.config`, direct ConfigStore access, and alternate Rust precedence for
registered Gobby runtime keys.

## Constraints
`kind: framing`

- Bootstrap owns only `daemon_port`, `bind_host`, `websocket_port`, `ui_port`,
  `services_bind_address`, `daemon_url`, `datastore_mode`, `database_url`, and
  `postgres_pool`.
- `auth_mode` is interim BootstrapConfig-owned: excluded from the runtime registry,
  written only by the installer, read by hook preflight from bootstrap, and captured by
  HTTP startup from `BootstrapConfig` rather than `ServiceContainer.config`. Plan
  `account-identity-machine-ownership` (#19650 §2.2) deletes the field entirely; this
  plan must not extend its surface.
- Configurable `hub_backend` is removed.
- Dotted ConfigStore primary keys remain; no generalized scope columns.
- Public configuration includes daemon settings, UI preferences, global rule toggles,
  global approval policy, launch defaults, provider/model choices, feature flags,
  limits, schedules, and registered API-key references.
- Domain stores retain agents, individual rules and enabled flags, workflows, prompts,
  templates, and executable definitions.
- `auth.*` credential payloads and embedding lifecycle journals use restricted specs and
  are excluded from public schema, public values, YAML, and exports.
- Live is the default activation policy. Restart-required keys are `cors_origins`,
  `test_mode`, `database_concurrency.*`, `databases.*`, `telemetry.*`,
  `memory.backend`, `websocket.enabled`, `ui.enabled`, `ui.mode`, and `ui.web_dir`.
- Structural `ai.embeddings.*` changes use the existing switch coordinator.
  `ai.embeddings.api_key` remains live.
- Apply failures preserve committed desired state and local last-good active state. They
  never generate compensating database writes.
- `/api/config/effective` and `/api/config/service-capabilities` remain separate
  authenticated Rust machine contracts. Machine configuration may contain resolved
  secrets.
- Public configuration reads, errors, events, YAML, and exports never contain secret
  plaintext.
- Schema evolution folds into baseline 375 directly. Through the 0.5.0 pre-release
  period, baseline 375 is the sole PostgreSQL schema authority and `MIGRATIONS` in
  `crates/gcore/src/schema/assets.rs` stays empty; numbered migrations resume only after
  0.5.0 ships. Commit `a3b56649a` already folded a former migration 376 back into the
  baseline, so adding one here would reverse an established convention.
- Editing `baseline.sql` changes `BASELINE_CHECKSUM` and `root_hash()`, so the catalog
  manifest, the packaged release-pinned identity, and both Rust identity contract tests
  are regenerated in the same deliverable. New DDL uses the baseline's idempotent guards
  (`IF NOT EXISTS`) so existing hubs re-apply cleanly and refresh their receipt.
- Retain `config_store.is_secret` as registry-derived integrity metadata; no destructive
  column drop.
- Multiple daemons connected to one remote hub receive revisions through a dedicated
  pool-exempt PostgreSQL notification connection.
- Remote-hub multi-daemon deployments require the existing shared-passphrase secret KEK
  posture; a daemon that cannot prove the shared KEK/DEK identity fails closed before
  ready.
- #19650 and #17769 retain auth identity/API-key ownership. Final raw-access deletion
  waits for their auth-owned consumers to use the restricted typed API.
- No wholesale reset API, durable change log, compatibility layer, or full pytest run.
- `config_store.py` splits into a thin facade, repository, and mutation modules. MCP
  config shrinks in place to three tools.
- Every touched production source remains below 1,000 lines as a standing implementation
  gate.
- The companion coverage ledger enumerates every acceptance item below. Each item maps to
  its source deliverable's single expected leaf; `owner_agent` is `backend-developer`
  except section 2.5, which is `frontend-developer`, and section 4.2, which is `qa-dev`.
  Its `plan_hash` is computed after final Markdown bytes are materialized.

## P1: Registry, Schema, Persistence, and Runtime
`kind: framing`

### 1.1 Compile the typed registry [category: code]
`kind: deliverable`

Targets:
- `src/gobby/config/registry.py`
- `src/gobby/config/app.py::DaemonConfig`
- `src/gobby/config/bootstrap.py::BootstrapConfig`
- `src/gobby/config/bootstrap.py::BootstrapConfig.to_config_dict`
- `tests/config/test_config_registry.py`

Add immutable `ConfigKeySpec`, `ConfigPatternSpec`, `ActivationPolicy`,
`ConfigVisibility`, and secrecy metadata. Compile ordinary daemon leaves from Pydantic
types/defaults and require explicit adapters for mapping leaves and supplemental
namespaces. Daemon leaf models live in `src/gobby/config/persistence.py` and are
introspected, not edited.

Register these dynamic families:

- `ai.generation.endpoints.{endpoint}.{field}`
- `ai.generation.profile_defaults.{profile}`
- `mcp_client_proxy.tool_timeouts.{tool}`
- `gobby_tasks.expansion.pattern_criteria.patterns.{pattern}`
- `gobby_tasks.expansion.pattern_criteria.detection_keywords.{pattern}`
- `verification_defaults.custom.{command}`
- `skills.hubs.{hub}.{field}`
- `context_window_overrides.{model_match}`
- `wiki.codewiki_project_scopes_by_name.{project_name}`
- `launch_defaults.{project_id}`

Register supported `ui_settings.*` fields, `rules.enforcement_enabled`,
`rules.aggregate_blocks`, `tool_approvals.global_rules`, restricted auth keys, and
restricted embedding lifecycle keys.

Registry startup rejects overlaps, unclassified mapping leaves, unknown public keys,
bootstrap/runtime ownership conflicts, and restricted-key exposure. Generate JSON Schema
metadata for namespace, secrecy, visibility, activation, and machine export.

Dynamic path segments use one canonical lossless codec with pinned bytes: a segment
encodes as UTF-8, every byte outside the unescaped alphabet `A-Z a-z 0-9 - _ ~` is
percent-encoded with uppercase hex digits, and `%` and `.` are always escaped. Decoding
rejects malformed and non-canonical input — truncated or non-hex escapes, lowercase
escape hex, and escapes of unescaped-alphabet bytes — and applies no Unicode
normalization, so distinct composed and decomposed sequences remain distinct. Registry
pattern matching, dotted ConfigStore keys, HTTP/MCP/YAML paths, browser state, and the
generated Rust contract all apply the same codec, so operator-controlled identifiers
containing dots, percent signs, slashes, spaces, or child-field-like text never change
logical paths or collide during flatten/unflatten or cross-language matching.

One shared adversarial codec vector set maps each logical segment to its exact
canonical encoded bytes and covers segments containing dots, percent signs,
already-encoded percent sequences, slashes, spaces, child-field-like text, accented and
CJK text, emoji, and distinct composed/decomposed sequences, plus malformed and
non-canonical encoded inputs with their required rejections. It is defined beside the
codec and reused verbatim by the surface tests in sections 1.1, 2.1, 2.3, 2.4, 2.5, and
2.6, so no representation boundary can pass while double-encoding, decoding early,
choosing a different safe alphabet or hex case, or splitting a segment structurally.

**Acceptance:**

- 1.1.1 - Every non-bootstrap daemon leaf resolves to exactly one spec. test: `tests/config/test_config_registry.py::test_every_daemon_leaf_has_one_spec`.
- 1.1.2 - Every mapping leaf has an explicit non-overlapping pattern adapter. test: `tests/config/test_config_registry.py::test_mapping_patterns_are_complete`.
- 1.1.3 - Public and machine schemas expose only their declared visibility classes. test: `tests/config/test_config_registry.py::test_visibility_partitions_are_disjoint`.
- 1.1.4 - The shared codec vector set produces its exact canonical encoded bytes, round-trips losslessly through every dynamic family and dotted storage without collisions, and every malformed or non-canonical input is rejected. test: `tests/config/test_config_registry.py::test_dynamic_segment_codec_round_trip`.

### 1.2 Extend baseline 375 with revisioned configuration state [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `crates/gcore/assets/schema/baseline.sql`
- `crates/gcore/assets/schema/catalog.manifest.json::*` — scope-reason: regenerated column catalog gains the revision table and column rows
- `crates/gcore/src/schema/assets.rs::*` — scope-reason: refresh BASELINE_CHECKSUM for the edited baseline while MIGRATIONS stays empty
- `crates/gcore/src/schema/runner.rs::*` — scope-reason: recognize the exact predecessor baseline@375 receipt as refreshable and replace it atomically
- `crates/gcore/src/schema/runner_tests.rs::*` — scope-reason: verify fresh apply and existing-hub re-apply paths
- `src/gobby/storage/schema_expected_identity.json::*` — scope-reason: regenerated release-pinned identity carries the new baseline checksum and assets root hash
- `scripts/generate_schema_expected_identity.py`
- `crates/gcore/tests/schema_contract.rs::embedded_assets_publish_a_complete_schema_identity`
- `crates/gdaemon/tests/cli_contract.rs::version_json_reports_exact_schema_identity_contract`

Add singleton `config_state(id BOOLEAN PRIMARY KEY CHECK(id), revision BIGINT NOT NULL)`
with its single revision-zero row, and add `revision BIGINT NOT NULL DEFAULT 0` to
`config_store`. Grant `gobby_daemon_runtime` access to match the existing `config_store`
grant. Use the baseline's idempotent guards so an existing hub re-applies cleanly.

Baseline 375 also gains the managed embedding-generation coordination state:
`embedding_generation_acks` — durable per-daemon acknowledgement and serving-lease rows
keyed by stable daemon-instance identity, carrying generation, committed revision,
acknowledgement, and lease expiry — and `embedding_projection_changes`, a
transactionally durable per-source change sequence with tombstones for the memory,
tool, and GitHub-issue embedding producers. Both tables receive least-privilege
`gobby_daemon_runtime` grants matching the existing `config_store` grant.

Keep `BASELINE_VERSION = 375` and leave `MIGRATIONS` empty. Refresh `BASELINE_CHECKSUM`
to the sha256 of the edited `baseline.sql`. The current runner classifies any
checksum-mismatched baseline-375 receipt as `CorruptPartial` and refuses with
"recreate from a verified backup", so this deliverable adds a predecessor-refresh path
to `classify_baseline_state`/`apply_locked`: exactly one recorded prior `baseline@375`
receipt checksum is recognized as refreshable, the idempotent baseline re-applies, and
the receipt row is replaced in the same transaction. Arbitrary checksum or filename
mismatches keep the existing `CorruptPartial` rejection.

Retain `is_secret`; application writes derive it from the registry.

Editing the baseline changes `root_hash()`, so regenerate the packaged schema identity
that `gobby.storage.schema_contract` loads and `gobby install` asserts. Regeneration
follows one deterministic order, because the identity generator defaults to the
*installed* `~/.gobby/bin/gdaemon` and the catalog freshness test only writes under an
explicit flag:

1. Update `BASELINE_CHECKSUM` for the edited `baseline.sql`.
2. Regenerate the catalog against an isolated PostgreSQL database with
   `UPDATE_GCORE_SCHEMA_MANIFEST=1`.
3. Build release `gdaemon`.
4. Run `uv run python scripts/generate_schema_expected_identity.py --gdaemon
   target/release/gdaemon` so the freshly built binary, never a stale installed one,
   produces the identity bytes.
5. Rerun catalog freshness without update mode plus both identity contract tests.

**Acceptance:**

- 1.2.1 - A fresh apply creates the revision table, seed row, and config_store column. test: `crates/gcore/src/schema/runner_tests.rs::fresh_baseline_creates_config_revision_state`.
- 1.2.2 - A hub holding the exact predecessor baseline@375 receipt re-applies, replaces its receipt with the new checksum in one transaction, and loses no data. test: `crates/gcore/src/schema/runner_tests.rs::existing_hub_reapplies_updated_baseline`.
- 1.2.3 - Re-apply is idempotent and requires no destructive authorization. test: `crates/gcore/src/schema/runner_tests.rs::config_revision_baseline_is_nondestructive`.
- 1.2.4 - Embedded assets and catalog describe the revision table and row column. file: `crates/gcore/assets/schema/catalog.manifest.json`.
- 1.2.5 - Regenerated schema identity matches the edited baseline and both identity contract tests pass. test: `crates/gdaemon/tests/cli_contract.rs::version_json_reports_exact_schema_identity_contract`.
- 1.2.6 - Arbitrary checksum or filename receipt mismatches still classify CorruptPartial and refuse. test: `crates/gcore/src/schema/runner_tests.rs::unrecognized_receipt_still_rejects`.
- 1.2.7 - A fresh apply creates the embedding acknowledgement/lease and projection-change tables with runtime-role grants. test: `crates/gcore/src/schema/runner_tests.rs::fresh_baseline_creates_embedding_coordination_state`.

### 1.3 Implement atomic revisioned mutations [category: code] (depends: 1.1, 1.2)
`kind: deliverable`

Targets:
- `src/gobby/storage/config_store.py::*` — scope-reason: replace raw persistence methods with a thin registry-backed facade
- `src/gobby/storage/config_repository.py`
- `src/gobby/storage/config_mutations.py`
- `tests/storage/test_revisioned_config_store.py`

Expose complete-snapshot reads, typed CAS patches, scoped namespace replacement, and
restricted internal mutation. Every effective mutation:

1. Locks `config_state`.
2. Compares mandatory `expected_revision`.
3. Builds and validates the complete prospective snapshot.
4. Updates overrides, secret payloads/references, derived `is_secret`, and row revisions
   atomically.
5. Advances the global revision exactly once.
6. Issues transaction-bound `pg_notify('gobby_config_changed', revision)`.

Unsets delete overrides. Same-value writes are no-ops. Secret rotation advances the
revision when the secret payload changes even if its reference string remains stable.
Startup reconciles registered `is_secret` metadata without changing effective values and
fails closed on unknown residual rows.

Revisions occupy the cross-language exact-integer domain. The checked increment refuses
to advance past 2^53−1 (`Number.MAX_SAFE_INTEGER`) with a typed `revision_exhausted`
failure that commits nothing, so every wire surface — HTTP, MCP, WebSocket, YAML
results, and browser state — exchanges revisions as exact JSON numbers that PostgreSQL,
Python, Rust, and JavaScript all represent losslessly. The `config_state` column stays
BIGINT; the ceiling is an application invariant of the single mutation path. Sections
2.1, 2.3, 2.4, and 2.5 close the domain at every interface: revision inputs validate as
strict integers in [0, 2^53−1], and the exhaustion failure crosses each wire as a
typed, non-retryable terminal result distinct from stale-revision conflict.

Complete-snapshot reads are single-snapshot coherent: `ConfigRepository` opens one
read-only `REPEATABLE READ` transaction before its first query and reads `config_state`,
every registered row, row revisions, and secret bindings inside it. A row revision above
the captured global revision is rejected as torn, so a writer committing between
snapshot queries can never produce a mixed snapshot whose metadata claims the wrong
revision.

**Acceptance:**

- 1.3.1 - Concurrent writers sharing an expected revision yield one commit and one typed conflict. test: `tests/storage/test_revisioned_config_store.py::test_compare_and_swap_serializes_writers`.
- 1.3.2 - Values, unsets, secret payloads, row revisions, global revision, and notification commit atomically. test: `tests/storage/test_revisioned_config_store.py::test_mutation_is_one_transaction`.
- 1.3.3 - Invalid candidates leave configuration, secrets, revision, and notifications untouched. test: `tests/storage/test_revisioned_config_store.py::test_invalid_candidate_has_no_side_effects`.
- 1.3.4 - No-op and secret-rotation behavior follows the effective-change rule. test: `tests/storage/test_revisioned_config_store.py::test_effective_change_controls_revision`.
- 1.3.5 - A paused reader with a concurrent committed writer returns a wholly old or wholly new snapshot, never a mix. test: `tests/storage/test_revisioned_config_store.py::test_snapshot_read_is_repeatable_read_coherent`.
- 1.3.6 - Startup repairs stale registry-derived `is_secret` metadata without changing any effective value or the revision. test: `tests/storage/test_revisioned_config_store.py::test_startup_secrecy_repair_preserves_values_and_revision`.
- 1.3.7 - Startup with an unknown residual ConfigStore row fails closed. test: `tests/storage/test_revisioned_config_store.py::test_unknown_residual_row_fails_closed`.
- 1.3.8 - An increment at the 2^53−1 ceiling returns typed `revision_exhausted` and commits nothing. test: `tests/storage/test_revisioned_config_store.py::test_revision_ceiling_returns_exhausted`.

### 1.4 Add ConfigRuntime and remote-daemon notifications [category: code] (depends: 1.3)
`kind: deliverable`

Targets:
- `src/gobby/config/runtime.py`
- `src/gobby/storage/config_notifications.py`
- `src/gobby/storage/hub/postgres.py::*` — scope-reason: expose a dedicated pool-exempt async connection factory that assumes the daemon runtime role
- `tests/config/test_config_runtime.py`

Add frozen `ConfigSnapshot`, `ConfigChange`, `ApplyFailure`, prepared-subscriber
interfaces, and `ConfigRuntime`. Snapshots contain revision, typed desired and active
projections, row revisions, pending-restart keys, and failed-live keys.

Snapshots additionally carry private, non-serializable desired and active secret
bindings with content fingerprints. Failed preparation preserves the previous active
binding, and machine and subscriber consumers resolve secrets through the active
binding rather than current storage, so a same-reference rotation whose activation
failed locally never exposes the unactivated payload through that daemon.

The configuration service boundary is async. Blocking repository reads and subscriber
constructor work run off-loop in two separately bounded capacity lanes — database work
and constructor/drain work — each with bounded admission, so constructor-lane
saturation can never starve listener reconciliation, PATCH completion, or shutdown.
Database work carries statement, lock, and connection timeouts plus server-side
cancellation, so a hung database call terminates at the database. Local commits hand
results back through an awaitable thread-safe handoff. A deadline expiry quarantines
and disposes the late synchronous result when it eventually completes; a constructor
that never returns is abandoned after its deadline — its lane slot is retired, its
keys record failed-live, and shutdown proceeds within its bound without waiting for
it.

Activation publishes one immutable runtime-active bundle containing the active snapshot
epoch and every replaceable service reference. Subscribers prepare a replacement bundle;
`ConfigRuntime` publishes a single pointer swap, and every request, policy decision, and
loop captures that pointer once per operation, so a multi-key revision is always
observed as one complete old or new epoch.

On a remote hub, `ConfigRuntime` verifies a non-secret KEK/DEK identity fingerprint
before reporting ready and fails closed on mismatch, so a daemon that cannot unwrap the
shared secret envelope never becomes healthy.

A dedicated `psycopg.AsyncConnection` comes from a pool-exempt hub factory that applies
`SET ROLE gobby_daemon_runtime` — matching the pool's least-privilege session identity —
before executing `LISTEN gobby_config_changed`, and is owned by `ConfigRuntime`'s async
lifecycle. The listener connection runs in autocommit mode: PostgreSQL activates a
subscription only at transaction commit, so acknowledgement is defined as confirmed
active subscription on the autocommit connection, never mere command completion inside
an open transaction. Local commits reconcile immediately. Remote notifications,
revision gaps, and reconnects trigger full atomic reloads. Database restart causes
bounded reconnect backoff followed by full reload before healthy status.

Startup and every reconnect close the lossy subscription window: the runtime awaits
connection establishment and `LISTEN` acknowledgement, then performs a full
revision-coherent reload before reporting ready. A revision committed between a snapshot
read and `LISTEN` acknowledgement therefore converges without waiting for a later
notification.

Reconciliation is idempotent by revision and monotonically serialized. One reconciliation
mutex plus a maximum-requested-revision watermark is shared by local commits, `LISTEN`
notifications, reconnect reloads, and subscriber registration. A notification whose
revision is less than or equal to the active snapshot revision is a no-op, one or more
newer notifications coalesce into one reload to the latest committed revision, and the
published revision is rechecked after every awaited I/O so a delayed older reload or
preparation result is discarded rather than published. A superseded preparation is
discarded through bounded cleanup: unfinished work is cancelled, every completed
replacement is disposed exactly once, no failed-live metadata is recorded for the
obsolete revision, and the maximum-requested-revision watermark reconciles afterward.
A local commit therefore
reconciles exactly once even though the pool-exempt listener also receives its own
`NOTIFY`, and no path can publish an older snapshot over a newer one or invoke
subscribers twice for one revision.

Subscriber registration is serialized with snapshot swapping. Registration returns the
current active projection and its revision, and any revision committed during
registration is reconciled before registration completes.

Subscriber work is bounded: preparation and old-resource disposal carry named
per-subscriber deadlines. A preparation timeout disposes completed replacements,
preserves all old active state, and records the affected keys as failed-live. The new
snapshot publishes before bounded old-resource drain, and a drain failure never rolls
back active state.

Live activation uses a local prepare/commit protocol: every matching subscriber builds
replacement state first; any preparation failure disposes prepared replacements and
preserves all previous active state. Successful preparation permits no-fail reference
swaps. Failure records remain until a later operator mutation changes the affected key.

First initialization is distinct from replacement: initial subscriber registration at
startup is a preparation transaction with no last-good state to preserve. A required
service whose first construction fails disposes every partially prepared replacement
and blocks readiness — the first runtime-active bundle publishes only after every
required subscriber prepares successfully. An optional capability whose first
construction fails publishes an explicitly unavailable slot with recoverable degraded
state and readiness proceeds. `ConfigRuntime` owns that degraded state and clears it
when a later mutation changing an affected key activates successfully.

Restart changes update desired state while retaining old active values. Managed changes
are rejected before persistence.

**Acceptance:**

- 1.4.1 - Readers observe immutable single-revision snapshots. test: `tests/config/test_config_runtime.py::test_snapshot_swap_is_atomic`.
- 1.4.2 - Restart writes separate desired and active state and report pending keys. test: `tests/config/test_config_runtime.py::test_restart_policy_tracks_pending_keys`.
- 1.4.3 - Post-commit preparation failure keeps desired state and revision committed, performs no active swap and no compensating write, and records failed-live metadata. test: `tests/config/test_config_runtime.py::test_apply_failure_preserves_local_last_good_state`.
- 1.4.4 - A second runtime receives remote revisions over the pool-exempt listener. test: `tests/config/test_config_runtime.py::test_remote_runtime_receives_revision_notification`.
- 1.4.5 - Listener reconnect performs a full reload before health recovery. test: `tests/config/test_config_runtime.py::test_listener_reconnect_reloads_snapshot`.
- 1.4.6 - Duplicate, current, and burst notifications reconcile at most once to the latest revision. test: `tests/config/test_config_runtime.py::test_notifications_are_idempotent_and_coalesced`.
- 1.4.7 - The pool-exempt listener runs under the daemon runtime role in autocommit mode and receives notifications, reconnects, and closes under it. test: `tests/config/test_config_runtime.py::test_listener_assumes_runtime_role`.
- 1.4.8 - A delayed older reload completing after a newer one is discarded and never published. test: `tests/config/test_config_runtime.py::test_out_of_order_reload_is_discarded`.
- 1.4.9 - Failed preparation after a same-reference secret rotation preserves the previous active secret binding and consumers never observe the unactivated payload. test: `tests/config/test_config_runtime.py::test_failed_apply_preserves_active_secret_binding`.
- 1.4.10 - Blocking repository or constructor work runs off-loop in its bounded lane, database work terminates through database-side timeouts and cancellation, and a late result arriving after its deadline is quarantined and disposed without stalling LISTEN or shutdown. test: `tests/config/test_config_runtime.py::test_blocking_work_is_bounded_and_quarantined`.
- 1.4.11 - A successful preparation superseded by a newer revision is disposed exactly once and records no failed-live metadata. test: `tests/config/test_config_runtime.py::test_superseded_preparation_is_disposed`.
- 1.4.12 - Snapshot and service references publish as one bundle pointer and no forced interleaving observes a mixed epoch. test: `tests/config/test_config_runtime.py::test_active_bundle_swap_is_atomic`.
- 1.4.13 - Remote-hub startup verifies the KEK/DEK identity fingerprint and a mismatched daemon fails closed before ready. test: `tests/config/test_config_runtime.py::test_kek_mismatch_fails_closed`.
- 1.4.14 - Failed-live metadata survives unrelated revisions and duplicate reconciliation, and clears only when a later mutation changes an affected key and its activation succeeds. test: `tests/config/test_config_runtime.py::test_failed_live_record_lifecycle`.
- 1.4.15 - Constructor-lane saturation by non-returning work leaves LISTEN reconciliation, PATCH completion, and shutdown within their bounds. test: `tests/config/test_config_runtime.py::test_lane_saturation_preserves_bounds`.
- 1.4.16 - Fresh-startup required-service preparation failure disposes partial replacements and blocks readiness; an optional-capability failure publishes an unavailable slot, and a later successful affected-key activation clears the degraded state. test: `tests/config/test_config_runtime.py::test_first_initialization_failure_semantics`.

## P2: Public and Machine Interfaces
`kind: framing`

### 2.1 Replace the public HTTP configuration API [category: code] (depends: 1.4)
`kind: deliverable`

Targets:
- `src/gobby/servers/routes/configuration_models.py::*` — scope-reason: replace save/reset models with revisioned request and response models
- `src/gobby/servers/routes/configuration_context.py::*` — scope-reason: expose ConfigRuntime to configuration routes
- `src/gobby/servers/routes/configuration_values.py::*` — scope-reason: replace duplicated validation and mutation behavior
- `src/gobby/servers/routes/configuration_secrets.py::*` — scope-reason: route registered secret fields through universal patch semantics
- `src/gobby/servers/routes/configuration_generation_endpoints.py::*` — scope-reason: migrate probe-gated endpoint activation to one revisioned typed mutation including its secret
- `src/gobby/servers/routes/configuration.py::create_configuration_router`
- `src/gobby/servers/websocket/broadcast.py::BroadcastMixin`
- `tests/servers/routes/test_config_values_api.py`

Provide:

- `GET /api/config/schema`
- `GET /api/config/values`
- `PATCH /api/config/values`

Values return revision, nested masked desired/active state, secret-set metadata,
pending-restart keys, and failed-live keys. PATCH accepts
`{expected_revision, values, unset}`. The registry owns validation, secrecy, visibility,
and activation.

Map stale revisions to 409 and validation errors to path-addressed 422 responses.
Revision inputs validate as strict integers in [0, 2^53−1]; negative, fractional,
non-numeric, and above-ceiling values are path-addressed 422 rejections. A mutation
refused by the checked-increment ceiling returns a distinct non-retryable
`revision_exhausted` error — a determinate permanent failure separate from the 409
conflict and the 5xx indeterminate-persistence class. Structural embedding keys return
`managed_activation_required` with `/api/embeddings/switch/start`. Remove reset and
caller-controlled secrecy.

Persistence and activation outcomes are distinct: when persistence succeeds but local
activation fails, PATCH returns committed success carrying the new revision plus
`failed_live_keys` and apply-status metadata, so callers never retry into an immediate
conflict. 5xx responses are reserved for indeterminate persistence.

Probe-gated generation-endpoint activation moves onto this surface: after probing, it
commits values and the endpoint secret as one revisioned typed mutation through the
universal service and performs no raw ConfigStore, secret-store, or runtime-config
writes.

Add one canonical `config_event` WebSocket message carrying only `{revision}`. Each
daemon emits it once after ConfigRuntime finishes reconciling a newly observed revision;
duplicate and current revisions emit nothing. Pending-restart and failed-live metadata
reach clients through the values refetch the event triggers, so the event body never
carries configuration content. Section 3.1 registers the publisher at startup.

**Acceptance:**

- 2.1.1 - Schema and values expose public registry metadata and masked desired/active state. test: `tests/servers/routes/test_config_values_api.py::test_public_schema_and_values_contract`.
- 2.1.2 - PATCH enforces CAS, path validation, per-key unset, and managed activation. test: `tests/servers/routes/test_config_values_api.py::test_public_patch_contract`.
- 2.1.3 - Public reads, errors, and events contain no secret plaintext. test: `tests/servers/routes/test_config_values_api.py::test_public_surfaces_redact_secrets`.
- 2.1.4 - Reset and caller-supplied `is_secret` are absent. test: `tests/servers/routes/test_config_values_api.py::test_legacy_reset_and_secrecy_flags_are_removed`.
- 2.1.5 - A newly reconciled revision emits exactly one revision-only `config_event` and duplicate revisions emit none. test: `tests/servers/routes/test_config_values_api.py::test_config_revision_event_contract`.
- 2.1.6 - A committed mutation whose local activation fails returns success with the new revision and failed-live metadata, never a retryable generic error. test: `tests/servers/routes/test_config_values_api.py::test_apply_failure_returns_committed_metadata`.
- 2.1.7 - Generation-endpoint activation commits one revisioned typed mutation including its secret and performs no raw writes. test: `tests/servers/routes/test_config_values_api.py::test_endpoint_activation_uses_typed_mutation`.
- 2.1.8 - HTTP paths round-trip the shared codec vector set with exact canonical bytes, without early decoding or structural splitting. test: `tests/servers/routes/test_config_values_api.py::test_http_round_trips_codec_vectors`.
- 2.1.9 - Revision inputs outside the strict integer domain are rejected and a ceiling-refused mutation returns the typed non-retryable `revision_exhausted` error. test: `tests/servers/routes/test_config_values_api.py::test_revision_domain_and_exhaustion_contract`.

### 2.2 Preserve the authenticated Rust machine contract [category: code] (depends: 1.4)
`kind: deliverable`

Targets:
- `src/gobby/servers/routes/configuration_effective.py::*` — scope-reason: rebuild every route helper on the active snapshot while preserving the flat envelope, runtime-token auth, and machine secret resolution
- `tests/servers/routes/test_configuration_effective_routes.py::*` — scope-reason: pin authenticated resolved-secret and capability contracts

Keep `/api/config/effective` returning flat dotted `{"config": {...}}` from the active
snapshot. Select keys through registry machine visibility and resolve allowed secret
references to plaintext for authenticated binaries through the snapshot's active secret
binding — never by re-resolving the reference from current storage — so machine output
returns only payloads that actually activated. Keep `Cache-Control: no-store` and
existing runtime-token authorization.

Keep `/api/config/service-capabilities`, agent-claims authorization, and response schema.
Build capabilities from the active snapshot. Every `DaemonConfig`-consuming helper in the
module — key selection, runtime overlays, capability construction, and route
registration — moves to snapshot input.

**Acceptance:**

- 2.2.1 - Effective config retains its flat envelope and resolves machine-visible secret references. test: `tests/servers/routes/test_configuration_effective_routes.py::test_effective_config_preserves_resolved_machine_contract`.
- 2.2.2 - Public-only and restricted-only keys are excluded from machine output. test: `tests/servers/routes/test_configuration_effective_routes.py::test_effective_config_uses_machine_visibility`.
- 2.2.3 - Effective config requires the runtime token and disables caching. test: `tests/servers/routes/test_configuration_effective_routes.py::test_effective_config_auth_and_cache_contract`.
- 2.2.4 - Service capabilities retain agent authorization and active-snapshot behavior. test: `tests/servers/routes/test_configuration_effective_routes.py::test_service_capabilities_use_active_snapshot`.
- 2.2.5 - Machine output serves the activated secret payload and never an unactivated rotated payload. test: `tests/servers/routes/test_configuration_effective_routes.py::test_machine_output_uses_active_secret_binding`.

### 2.3 Replace MCP configuration tools [category: code] (depends: 1.4, 2.1)
`kind: deliverable`

Targets:
- `src/gobby/mcp_proxy/tools/config.py::*` — scope-reason: shrink the registry to schema, values, and patch tools
- `tests/mcp_proxy/tools/test_config_values.py`

Retain the module and expose only `get_config_schema`, `get_config_values`, and
`patch_config_values`. Reuse the same service and errors as HTTP, including the
committed-with-apply-failure result: a patch that persists but fails local activation
returns success with the new revision and failed-live metadata. Every mutation requires
`expected_revision`. Revision-domain validation and the typed non-retryable
`revision_exhausted` result mirror the HTTP contract.

**Acceptance:**

- 2.3.1 - MCP and HTTP return equivalent schema, values, and patch results. test: `tests/mcp_proxy/tools/test_config_values.py::test_mcp_wraps_universal_config_service`.
- 2.3.2 - MCP patch requires revision and preserves secret/managed policies. test: `tests/mcp_proxy/tools/test_config_values.py::test_mcp_patch_requires_revision`.
- 2.3.3 - Raw get/set/delete/batch/list/default-seeding tools are absent. test: `tests/mcp_proxy/tools/test_config_values.py::test_legacy_config_tools_are_removed`.
- 2.3.4 - An MCP patch whose local activation fails reports committed success with failed-live metadata. test: `tests/mcp_proxy/tools/test_config_values.py::test_mcp_patch_reports_apply_status`.
- 2.3.5 - MCP tools round-trip the shared codec vector set byte-equivalently to HTTP. test: `tests/mcp_proxy/tools/test_config_values.py::test_mcp_round_trips_codec_vectors`.
- 2.3.6 - An MCP patch rejects out-of-domain revisions and maps the ceiling to the typed non-retryable `revision_exhausted` result. test: `tests/mcp_proxy/tools/test_config_values.py::test_mcp_revision_domain_and_exhaustion`.

### 2.4 Make YAML a validate-first daemon-namespace replacement [category: code] (depends: 1.4, 2.1)
`kind: deliverable`

Targets:
- `src/gobby/config/documents.py`
- `src/gobby/servers/routes/configuration_templates.py::*` — scope-reason: replace delete-before-validate template saves
- `src/gobby/servers/routes/configuration_import_export.py::*` — scope-reason: replace wholesale deletion and mixed-domain export
- `tests/servers/routes/test_config_yaml_replace.py`

Parse and validate the complete candidate before mutation. Reject bootstrap, restricted,
unknown, and managed keys; restore masked secret references; resolve references for
validation; run full Pydantic cross-field validation; then issue one CAS replacement for
`namespace=daemon`. Each replacement request carries `expected_revision` and reuses the
typed 409 conflict contract from universal PATCH, so a document edited against a stale
revision requires an explicit refetch before resubmission. A replacement that persists
but fails local activation reuses the committed-success contract with the new revision
and failed-live metadata. Replacement requests validate revisions in the same strict
integer domain, and a ceiling-refused replacement returns the typed non-retryable
`revision_exhausted` result rather than a conflict.

Omitted daemon settings restore defaults. UI preferences, credentials, operational data,
domain records, and bootstrap remain untouched. Export desired daemon configuration with
masks/references. Prompt/domain bundles remain separate.

**Acceptance:**

- 2.4.1 - Invalid documents preserve rows, secrets, and revision. test: `tests/servers/routes/test_config_yaml_replace.py::test_invalid_document_has_no_side_effects`.
- 2.4.2 - Valid replacement changes only the daemon namespace in one revision. test: `tests/servers/routes/test_config_yaml_replace.py::test_daemon_replacement_is_scoped_and_atomic`.
- 2.4.3 - Omissions restore daemon defaults without clearing supplemental/domain state. test: `tests/servers/routes/test_config_yaml_replace.py::test_omissions_restore_only_daemon_defaults`.
- 2.4.4 - Export round-trips without plaintext secret disclosure. test: `tests/servers/routes/test_config_yaml_replace.py::test_masked_export_round_trip`.
- 2.4.5 - A stale-revision replacement returns 409 and leaves rows, secrets, and the revision untouched. test: `tests/servers/routes/test_config_yaml_replace.py::test_stale_revision_replacement_is_rejected`.
- 2.4.6 - A replacement that persists but fails local activation reports committed success with failed-live metadata. test: `tests/servers/routes/test_config_yaml_replace.py::test_replacement_reports_apply_status`.
- 2.4.7 - YAML import and export round-trip the shared codec vector set with exact canonical bytes, without collisions or unintended structure. test: `tests/servers/routes/test_config_yaml_replace.py::test_yaml_round_trips_codec_vectors`.
- 2.4.8 - A replacement with an out-of-domain revision is rejected and a ceiling-refused replacement reports typed `revision_exhausted`. test: `tests/servers/routes/test_config_yaml_replace.py::test_yaml_revision_domain_and_exhaustion`.

### 2.5 Migrate browser configuration state [category: code] (depends: 2.1)
`kind: deliverable`

Targets:
- `web/src/api/config.ts`
- `web/src/hooks/useConfiguration.ts::useConfiguration`
- `web/src/hooks/useSettings.ts::fetchUISettings`
- `web/src/hooks/useSettings.ts::saveUISettings`
- `web/src/hooks/useSettings.ts::useSettings`
- `web/src/components/app/useAppProjectSelection.ts::useAppProjectSelection`
- `web/src/components/activity/MemoryTab.tsx::*` — scope-reason: replace direct configuration fetches and writes
- `web/src/hooks/useWebSocketEvent.ts::useWebSocketEvent`
- `web/src/components/settings/sections/configFields.tsx::*` — scope-reason: render activation class, pending-restart, and failed-live state on registered fields
- `web/src/components/settings/sections/MemoryKnowledgeSection.tsx::EmbeddingsGroup`
- `web/src/__tests__/App.test.tsx::*` — scope-reason: replace the pinned specialized UI-settings contract with universal client expectations
- `web/src/__tests__/config-authority-audit.test.ts`
- `web/src/components/settings/sections/__tests__/configFieldActivation.test.tsx`
- `web/src/hooks/__tests__/useConfiguration.revision.test.ts`
- `web/src/hooks/__tests__/useSettings.test.ts::*` — scope-reason: update persistence expectations
- `web/src/components/app/__tests__/useAppProjectSelection.test.tsx::*` — scope-reason: update project-selection persistence

Add one typed client carrying the latest revision. On 409, refetch server state, preserve
the unsaved local draft, and require explicit resubmission. The client models every
revision as a strict integer in [0, 2^53−1] and branches `revision_exhausted` away from
conflict handling: it renders a terminal non-retryable error and never refetches for
resubmission. Coalesce higher-revision WebSocket events into one refetch. Remove
specialized UI-setting, reset, launch-default, and approval-setting writes.

Render activation state from the schema and values payloads section 2.1 already returns:
each field shows its activation class, pending-restart keys show desired-versus-active
values, and failed-live keys show their apply status. Managed keys route to the supplied
managed action instead of generic PATCH. This deliverable adds no backend surface.

The client tracks the maximum observed configuration revision as a watermark. Every
WebSocket reconnect triggers a refetch, responses older than the rendered revision are
ignored, and a response revision below the watermark issues one trailing refetch, so a
`config_event` lost during disconnect or a higher event arriving during an in-flight
refetch never leaves the browser stale.

A TypeScript browser-authority audit rejects direct configuration fetches, specialized
setting writers, reset endpoints, and mutations outside the revision-carrying client
across `web/src`.

**Acceptance:**

- 2.5.1 - Every browser mutation includes the current revision. test: `web/src/hooks/__tests__/useConfiguration.revision.test.ts::includes_revision_in_every_patch`.
- 2.5.2 - Conflict refresh preserves unsaved edits and requires resubmission. test: `web/src/hooks/__tests__/useConfiguration.revision.test.ts::preserves_draft_after_conflict`.
- 2.5.3 - UI preferences and project selection use universal paths. test: `web/src/hooks/__tests__/useSettings.test.ts::persists_settings_through_config_patch`.
- 2.5.4 - Higher WebSocket revisions trigger one coalesced refetch. test: `web/src/hooks/__tests__/useConfiguration.revision.test.ts::coalesces_config_revision_events`.
- 2.5.5 - Fields render their activation class and show desired-versus-active values for pending-restart keys. test: `web/src/components/settings/sections/__tests__/configFieldActivation.test.tsx::renders_activation_class_and_pending_restart_state`.
- 2.5.6 - Failed-live keys surface apply status and managed keys route to the managed action. test: `web/src/components/settings/sections/__tests__/configFieldActivation.test.tsx::routes_managed_keys_and_shows_failed_live_status`.
- 2.5.7 - WebSocket reconnect refetches and converges on a mutation committed while disconnected. test: `web/src/hooks/__tests__/useConfiguration.revision.test.ts::refetches_on_reconnect`.
- 2.5.8 - A higher event during an in-flight refetch produces one trailing refetch and older responses never render. test: `web/src/hooks/__tests__/useConfiguration.revision.test.ts::watermark_triggers_trailing_refetch`.
- 2.5.9 - The browser-authority audit rejects direct fetches, specialized writers, reset calls, and revisionless mutations. test: `web/src/__tests__/config-authority-audit.test.ts::web_has_one_config_authority`.
- 2.5.10 - The typed client round-trips the shared codec vector set with exact canonical bytes through browser state without re-encoding drift. test: `web/src/hooks/__tests__/useConfiguration.revision.test.ts::round_trips_codec_vectors`.
- 2.5.11 - A `revision_exhausted` result renders as a terminal non-retryable state and triggers no refetch-resubmit loop. test: `web/src/hooks/__tests__/useConfiguration.revision.test.ts::exhausted_revision_is_terminal`.

### 2.6 Generate and consume the Rust runtime-config contract [category: code] (depends: 1.1, 2.2)
`kind: deliverable`

Targets:
- `scripts/generate_runtime_config_contract.py`
- `crates/gcore/assets/config/runtime_config_contract.json`
- `crates/gcore/src/config/runtime_contract.rs`
- `crates/gcore/src/config/mod.rs`
- `crates/gcore/src/config/tests.rs::*` — scope-reason: declare the runtime-contract test submodule alongside the existing submodules
- `crates/gcore/src/config/tests/runtime_contract.rs`
- `crates/gcore/src/ai/effective_config.rs::*` — scope-reason: replace the hardcoded MANAGED_CONFIG_KEYS allowlist with generated registry metadata
- `crates/gcode/src/config.rs::*` — scope-reason: declare the runtime-contract module from the gcode config module root
- `crates/gcode/src/config/context.rs::*` — scope-reason: fallback transaction fan-in resolving every service family from one captured snapshot
- `crates/gcode/src/config/services.rs::*` — scope-reason: replace per-key mixed-precedence service resolution
- `crates/gcode/src/config/layers.rs::*` — scope-reason: enforce runtime-mode-specific authority
- `crates/gcode/src/config/tests.rs::*` — scope-reason: declare the runtime-contract test submodule
- `crates/gcode/src/config/tests/runtime_contract.rs`
- `tests/config/test_runtime_config_contract.py`

Generate a deterministic checked-in JSON contract from registry machine specs, including
exact keys/patterns, defaults, secrecy, and activation metadata. Python CI regenerates and
byte-compares it. The contract and Rust pattern matching use the section 1.1 canonical
dynamic-segment codec, byte-identically with Python.

Rust effective-config validation consumes this asset instead of `MANAGED_CONFIG_KEYS`. In
Gobby daemon or hub mode:

- Daemon-served active config is authoritative when available.
- Direct PostgreSQL fallback opens one read-only `REPEATABLE READ` transaction in
  `Context::resolve_services`, captures `config_state.revision` and all machine-visible
  rows exactly once, loads every referenced secret ciphertext and key-envelope binding
  inside the same transaction before commit, resolves every service family from that
  immutable map, and returns the captured revision — so decrypted material always
  corresponds to the captured revision and a same-reference rotation committing during
  resolution never mixes payloads across revisions.
- Registered runtime keys do not accept environment or `gcore.yaml` precedence.
- Secret references resolve only through the hub secret store, from the bindings
  captured in the fallback snapshot.

Environment and `gcore.yaml` remain available only in explicit standalone mode with no
Gobby daemon/hub context. The gcode config module root is `crates/gcode/src/config.rs`;
both crates declare their new test submodule from the existing `config/tests.rs` file.

**Acceptance:**

- 2.6.1 - Generated Rust contract is byte-stable and current with the Python registry. test: `tests/config/test_runtime_config_contract.py::test_checked_in_contract_matches_registry`.
- 2.6.2 - Rust rejects machine keys absent from the generated contract. test: `crates/gcore/src/config/tests/runtime_contract.rs::rejects_unregistered_machine_key`.
- 2.6.3 - Gobby runtime mode ignores env/standalone precedence for registered keys. test: `crates/gcode/src/config/tests/runtime_contract.rs::gobby_mode_uses_registry_authority`.
- 2.6.4 - Direct hub fallback resolves service families and secret bindings from one revision-coherent snapshot, and a paused reader racing a same-reference rotation returns wholly old or wholly new material. test: `crates/gcode/src/config/tests/runtime_contract.rs::hub_fallback_reads_atomic_snapshot`.
- 2.6.5 - Rust and Python encode, match, and reject the shared codec vector set byte-identically, including its malformed and non-canonical inputs. test: `crates/gcode/src/config/tests/runtime_contract.rs::dynamic_segment_codec_matches_python`.
- 2.6.6 - Explicit standalone mode with no daemon or hub context honors environment and `gcore.yaml` precedence in their documented order. test: `crates/gcode/src/config/tests/runtime_contract.rs::standalone_mode_preserves_env_yaml_precedence`.

## P3: Consumer Migration and Activation
`kind: framing`

### 3.1 Wire ConfigRuntime into startup [category: code] (depends: 1.4, 2.1)
`kind: deliverable`

Targets:
- `src/gobby/runner.py::*` — scope-reason: add ConfigRuntime ownership and lifecycle
- `src/gobby/app_context.py::*` — scope-reason: add ConfigRuntime to the service context
- `src/gobby/runner_init/storage.py::init_storage_and_config`
- `tests/runner_init/test_config_runtime_startup.py`

Startup loads bootstrap topology, opens PostgreSQL, constructs the
registry/store/runtime, starts the listener and awaits confirmed subscription
activation on its autocommit connection, then performs the initial revision-coherent
load before constructing post-database services. A revision committed between
subscription activation and the initial load therefore converges without a later
notification. Service construction follows the section 1.4 first-initialization
contract: required-subscriber failure blocks readiness before the first bundle
publishes, and optional-capability failure surfaces recoverable degraded health that
`ConfigRuntime` owns and clears after a successful affected-key activation.

Startup also registers the section 2.1 `config_event` publisher against ConfigRuntime so
each reconciled revision reaches WebSocket clients exactly once.

Add `config_runtime` to runner and `ServiceContainer`. Existing `config`, `config_store`,
and `runner.config_store` references remain only until their dependent migration leaves
complete; section 4.1 deletes them. No new consumer may use them.

**Acceptance:**

- 3.1.1 - Startup constructs exactly one ConfigRuntime before post-database services. test: `tests/runner_init/test_config_runtime_startup.py::test_startup_constructs_one_runtime`.
- 3.1.2 - Runner and ServiceContainer expose the same ConfigRuntime instance. test: `tests/runner_init/test_config_runtime_startup.py::test_context_shares_runner_runtime`.
- 3.1.3 - Runtime notification lifecycle closes cleanly with daemon shutdown. test: `tests/runner_init/test_config_runtime_startup.py::test_runtime_closes_with_daemon`.
- 3.1.4 - Startup registers the config event publisher and one reconciled revision emits one event. test: `tests/runner_init/test_config_runtime_startup.py::test_startup_registers_config_event_publisher`.
- 3.1.5 - With a writer paused at the LISTEN-activation/reload boundary, a revision committed after subscription activation and before the initial reload converges before services construct, without a later notification. test: `tests/runner_init/test_config_runtime_startup.py::test_startup_closes_subscription_window`.
- 3.1.6 - Fresh startup with a failing required subscriber never reports ready, and a failing optional capability reports degraded then recovers after a successful affected-key activation. test: `tests/runner_init/test_config_runtime_startup.py::test_first_start_failure_and_recovery`.

### 3.2 Migrate generic policy consumers [category: code] (depends: 2.1, 3.1)
`kind: deliverable`

Targets:
- `src/gobby/servers/routes/rules.py::create_rules_router`
- `src/gobby/workflows/engine/core.py::*` — scope-reason: replace raw global-rule reads
- `src/gobby/servers/tool_approvals.py::get_global_approval_rules`
- `src/gobby/servers/tool_approvals.py::set_global_approval_rules`
- `src/gobby/servers/routes/configuration_tool_approvals.py::*` — scope-reason: remove specialized approval-setting routes
- `src/gobby/servers/routes/agent_spawn.py::create_agent_spawn_router`
- `src/gobby/servers/routes/configuration_ui_settings.py::*` — scope-reason: remove specialized UI-setting routes
- `src/gobby/mcp_proxy/tools/voice.py::*` — scope-reason: replace raw vocabulary persistence and in-memory config mutation with the typed API
- `src/gobby/servers/routes/attention.py::*` — scope-reason: replace services.config policy reads with runtime snapshots
- `src/gobby/servers/routes/sessions/core.py::*` — scope-reason: replace services.config policy reads with runtime snapshots
- `src/gobby/servers/routes/configuration_validation_detection.py::*` — scope-reason: replace ServiceContainer.config reads with runtime snapshots
- `tests/config/test_live_policy_consumers.py`

Read one typed snapshot per policy decision. Move global rules, approvals, launch
defaults, and UI preferences to generic PATCH. Preserve domain CRUD and per-domain
lifecycle behavior.

**Acceptance:**

- 3.2.1 - Rule evaluation observes live global toggles from one snapshot. test: `tests/config/test_live_policy_consumers.py::test_rules_use_runtime_snapshot`.
- 3.2.2 - Approval policy and launch defaults use typed registered paths. test: `tests/config/test_live_policy_consumers.py::test_approval_and_launch_defaults_are_registered`.
- 3.2.3 - Specialized setting writers disappear while domain CRUD remains. test: `tests/config/test_live_policy_consumers.py::test_only_specialized_setting_writers_are_removed`.
- 3.2.4 - Voice vocabulary persists through the typed API and attention/session routes read runtime snapshots. test: `tests/config/test_live_policy_consumers.py::test_voice_and_route_consumers_use_runtime`.
- 3.2.5 - Validation-detection preview reads the runtime snapshot instead of `ServiceContainer.config`. test: `tests/config/test_live_policy_consumers.py::test_validation_detection_uses_runtime_snapshot`.

### 3.3 Separate restart-bound topology consumers [category: code] (depends: 3.1)
`kind: deliverable`

Targets:
- `src/gobby/runner_init/servers.py::*` — scope-reason: separate bootstrap topology from restart-class runtime settings
- `src/gobby/servers/app_factory.py::*` — scope-reason: construct middleware from the startup active snapshot
- `src/gobby/servers/_app_ui.py::*` — scope-reason: keep UI server lifecycle restart-bound
- `src/gobby/servers/http.py::*` — scope-reason: capture auth_mode from BootstrapConfig at construction
- `tests/config/test_restart_config_consumers.py`

Daemon/WS/UI ports, bind addresses, service bind address, daemon URL, and database URL
come from bootstrap. CORS, test mode, WebSocket enablement, UI lifecycle, database
services, telemetry, and memory backend come from the startup active snapshot and do not
mutate running topology.

Database capacity uses two-stage startup. Bootstrap `postgres_pool` supplies connection
and acquisition policy plus a fixed minimal pool sufficient to load ConfigRuntime. After
the initial active snapshot, `database_concurrency.pool_max_size` and
`executor_max_workers` apply from that one revision — resizing the pool and sizing
executors before post-database services construct. Later `database_concurrency.*`
changes stay pending until the next startup, matching their restart-required
registration.

Interim `auth_mode` ownership moves fully to bootstrap: HTTP construction captures it
from `BootstrapConfig` instead of `ServiceContainer.config`, hook preflight keeps its
bootstrap read, the installer remains the only writer, and the runtime registry excludes
the key. #19650 §2.2 deletes the field; this deliverable only relocates the read it
would otherwise strand.

Cross-plan ordering is recorded as expansion edges, never prose alone: at expansion,
#19650 gains blocked-by dependencies on the leaves created from sections 1.2 and 3.3, so
the auth plan's overlapping bootstrap, HTTP, baseline, and identity edits cannot run
before or concurrently with them, and the section 4.3 deferral task depends on #19650.
The expansion handoff verifies the resulting cross-plan chain is acyclic before
dispatch.

**Acceptance:**

- 3.3.1 - Process topology reads only `BootstrapConfig`. test: `tests/config/test_restart_config_consumers.py::test_topology_uses_bootstrap_only`.
- 3.3.2 - Restart-class writes do not mutate running servers or middleware. test: `tests/config/test_restart_config_consumers.py::test_restart_changes_remain_pending`.
- 3.3.3 - Restart activates desired settings on the next startup snapshot. test: `tests/config/test_restart_config_consumers.py::test_restart_promotes_desired_to_active`.
- 3.3.4 - `auth_mode` resolves only from bootstrap across installer write, hook preflight, and HTTP construction, and is absent from the registry. test: `tests/config/test_restart_config_consumers.py::test_auth_mode_is_bootstrap_owned`.
- 3.3.5 - Startup sizes the pool and executors from the initial active revision after the fixed minimal bootstrap pool, and later concurrency changes stay pending until restart. test: `tests/config/test_restart_config_consumers.py::test_two_stage_pool_and_executor_sizing`.

### 3.4 Add live stateful service subscribers [category: code] (depends: 3.1, 3.3)
`kind: deliverable`

Targets:
- `src/gobby/runner_init/config_subscribers.py`
- `src/gobby/runner_init/services.py::*` — scope-reason: register cached-service replacement adapters
- `src/gobby/servers/http.py::*` — scope-reason: replace server.config access for stateful services
- `tests/config/test_stateful_config_subscribers.py`

Implement focused subscriber adapters for cached providers, model clients, embedding
clients, MCP proxy settings, chat limits, and every remaining constructor-captured live
setting. The consumer set is closed by a registry-key-to-consumer activation matrix:
expansion enumerates every live-activation registry key with its production consumers,
capture points, and subscriber or per-operation access path, and a coverage assertion
fails when a live key maps to no subscriber adapter and no declared per-operation
read.
Each adapter prepares its replacement entry for the section 1.4 runtime-active bundle
without publishing; `ConfigRuntime` commits the whole revision as one bundle-pointer
swap, and old in-flight work drains afterward. No consumer observes a mix of old and
new service references within one revision.

Registration uses the section 1.4 serialized handshake: each adapter hydrates from the
active projection ConfigRuntime returns at registration, and a revision committed during
registration reconciles before registration completes.

Adapters implement the section 1.4 work bounds: named preparation and disposal
deadlines, timeout disposal that preserves last-good services and records failed-live
keys, snapshot publication before bounded old-resource drain, and drain failures that
never roll back active state. Daemon shutdown cancels in-flight preparation and drain
work within the same bounds. Each adapter declares itself required or optional under
the section 1.4 first-initialization contract: a required adapter's first-construction
failure disposes partial state and blocks readiness, and a failed optional adapter
publishes an explicit unavailable slot that recovers on a later successful
affected-key activation.

**Acceptance:**

- 3.4.1 - Matching changes prepare all replacements before any swap. test: `tests/config/test_stateful_config_subscribers.py::test_prepare_precedes_every_swap`.
- 3.4.2 - Preparation failure disposes replacements and preserves all old services. test: `tests/config/test_stateful_config_subscribers.py::test_failed_prepare_keeps_last_good_services`.
- 3.4.3 - Successful swaps drain old in-flight clients. test: `tests/config/test_stateful_config_subscribers.py::test_successful_swap_drains_old_client`.
- 3.4.4 - API-key changes invalidate only dependent cached clients. test: `tests/config/test_stateful_config_subscribers.py::test_key_scoped_invalidation`.
- 3.4.5 - A revision committed during registration leaves the subscriber at the newest revision exactly once. test: `tests/config/test_stateful_config_subscribers.py::test_registration_race_resolves_to_latest_revision`.
- 3.4.6 - Preparation timeout disposes replacements, preserves last-good services, and records failed-live keys. test: `tests/config/test_stateful_config_subscribers.py::test_preparation_timeout_preserves_last_good`.
- 3.4.7 - Shutdown cancels in-flight preparation and drain within bounds and a drain failure never rolls back active state. test: `tests/config/test_stateful_config_subscribers.py::test_shutdown_cancels_subscriber_work`.
- 3.4.8 - Forced thread interleaving across a multi-key revision never observes a mixed service/snapshot epoch. test: `tests/config/test_stateful_config_subscribers.py::test_no_mixed_epoch_under_interleaving`.
- 3.4.9 - Every live-activation registry key resolves to a subscriber adapter or a declared per-operation read, and the matrix assertion fails on an unmapped key. test: `tests/config/test_stateful_config_subscribers.py::test_live_key_consumer_matrix_is_complete`.
- 3.4.10 - Adapter first-registration failure follows the declared contract: required blocks readiness with partial disposal, optional publishes an unavailable slot and recovers on later activation. test: `tests/config/test_stateful_config_subscribers.py::test_first_registration_failure_contract`.

### 3.5 Migrate loops and lifecycle consumers [category: code] (depends: 3.1, 3.4)
`kind: deliverable`

Targets:
- `src/gobby/runner_init/orchestration.py::*` — scope-reason: replace constructor-captured live configuration
- `src/gobby/runner_lifecycle.py::*` — scope-reason: replace runner.config reads with runtime or startup-captured snapshots
- `src/gobby/runner_lifecycle_agents.py::*` — scope-reason: replace runner.config reads with runtime snapshots
- `src/gobby/runner_lifecycle_periodic.py::*` — scope-reason: read one snapshot per loop iteration
- `src/gobby/runner_lifecycle_shutdown.py::*` — scope-reason: replace runner.config reads with runtime or startup-captured snapshots
- `src/gobby/runner_lifecycle_subsystems.py::*` — scope-reason: replace runner.config reads with runtime snapshots
- `src/gobby/runner_service_readiness.py::*` — scope-reason: replace runner.config readiness reads with runtime snapshots
- `src/gobby/servers/_app_lifecycle.py::*` — scope-reason: replace ServiceContainer.config access
- `src/gobby/servers/_app_routes.py::*` — scope-reason: replace route-construction config access
- `tests/config/test_runtime_loop_consumers.py`

Long-lived loops capture the runtime-active bundle pointer once per iteration and read
snapshot and services from that one epoch. Lifecycle and route initialization receive
either ConfigRuntime or an explicitly captured startup active projection according to
activation policy.

**Acceptance:**

- 3.5.1 - Periodic work uses one coherent snapshot per iteration. test: `tests/config/test_runtime_loop_consumers.py::test_periodic_iteration_uses_one_snapshot`.
- 3.5.2 - Live lifecycle consumers observe successful runtime swaps. test: `tests/config/test_runtime_loop_consumers.py::test_lifecycle_consumer_observes_live_change`.
- 3.5.3 - Restart-class lifecycle consumers retain startup active values. test: `tests/config/test_runtime_loop_consumers.py::test_lifecycle_consumer_retains_restart_value`.
- 3.5.4 - Runner lifecycle, shutdown, subsystem, and readiness modules read no `runner.config` attribute. test: `tests/config/test_runtime_loop_consumers.py::test_lifecycle_modules_use_runtime_access`.

### 3.6 Integrate managed embedding activation [category: code] (depends: 1.4, 2.1, 3.4)
`kind: deliverable`

Targets:
- `src/gobby/config/embedding_keys.py::*` — scope-reason: make canonical registry paths the only persisted namespace
- `src/gobby/ai/embedding_switch.py::*` — scope-reason: persist a completed-switch record that survives journal deletion
- `src/gobby/ai/embedding_switch_service.py::EmbeddingSwitchCoordinator`
- `src/gobby/ai/embedding_switch_runner.py::*` — scope-reason: replace fresh config loads throughout switch execution
- `src/gobby/cli/installers/embedding.py::*` — scope-reason: use typed runtime snapshots during installation
- `src/gobby/servers/routes/embeddings.py::create_embeddings_router`
- `src/gobby/storage/embedding_generation_state.py`
- `src/gobby/memory/vectorstore.py::*` — scope-reason: generation-pinned physical-target resolution and lease-gated serving
- `src/gobby/memory/services/indexing.py::*` — scope-reason: memory producer appends projection changes with tombstones in the mutation transaction
- `src/gobby/mcp_proxy/semantic_search.py::*` — scope-reason: tool producer appends projection changes and resolves generation-pinned targets
- `src/gobby/github_triage/issue_index.py::*` — scope-reason: issue producer appends projection changes with tombstones
- `tests/storage/test_embedding_switch_config_contract.py::*` — scope-reason: extend existing switch/config ownership tests
- `tests/ai/test_embedding_switch_daemon_lifecycle.py::*` — scope-reason: verify revisioned switch recovery

Persist only canonical `ai.embeddings.*`. Structural keys commit only through the switch
coordinator's restricted revisioned mutation. API-key rotation remains live and
invalidates embedding clients.

Managed structural revisions converge on every daemon through generation-pinned
physical targets, never through the mutable alias: the journal and the completed
record persist each generation's physical collection names, every runtime-active
bundle captures those physical targets at promotion, and embedding requests resolve
the captured physical target — a shared alias flip therefore never retargets a daemon
that has not yet promoted. A runtime observing a managed structural revision — locally
or over notification — verifies the completed switch, captures the generation's
physical targets once they are ready, and rebuilds dependent embedding clients.
Restart recovery resolves each journal phase from ConfigRuntime plus the journal, so a
crash at the flip or commit boundary converges to either the old or the new structural
state, never a mix.

Completion evidence survives journal cleanup: `complete_switch` currently deletes the
journal, so completion also persists a compact completed record — run ID, committed
revision, and the generation's physical collection targets per kind — that survives GC
and is retained until the next managed switch supersedes it. A remote daemon that
reloads after the journal is gone verifies the committed structural revision against
that record, including after reconnect.

A promoted generation contains every source mutation committed before promotion:
memory, tool, and GitHub-issue embedding producers append to the transactionally
durable `embedding_projection_changes` sequence — with tombstones for deletions — in
the same transaction as each source mutation. The switch captures a change-sequence
watermark at build enumeration and replays every later create, update, and tombstone
into the staged collections. A PostgreSQL-backed generation transition closes the
replay-to-commit window: once the transition state is durable, every producer projects
each mutation into both generations, directly or through replay, and the flip commits
only at a watermark covering every previously committed mutation. The completed record
binds the caught-up watermark and physical targets to the committed revision, and a
daemon promoting the new generation applies its own post-watermark changes before
acknowledging, so no daemon acknowledges a generation missing committed writes.

Old generations outlive the flip behind renewable serving leases: each daemon holds a
DB-authoritative `embedding_generation_acks` row keyed by its stable daemon-instance
identity, carrying the generation and committed revision it serves, its
acknowledgement, and a lease expiry it renews while serving. Embedding requests check
lease validity locally; a daemon that cannot confirm renewal — PostgreSQL or
notification connectivity lost — self-fences embedding requests before its lease
expires, even while HTTP and Qdrant remain reachable, so exclusion from GC always
implies inability to serve. Generation GC deletes an old physical collection only
after every unexpired lease acknowledges the committed revision and bounded in-flight
drains complete. A fenced or expired daemon reconciles to the committed generation
before serving embedding requests again, and a crash between the flip, commit,
reconcile, and GC boundaries always recovers to a coherent generation.

**Acceptance:**

- 3.6.1 - Generic interfaces reject structural embedding mutations. test: `tests/storage/test_embedding_switch_config_contract.py::test_structural_keys_require_switch`.
- 3.6.2 - Switch completion commits canonical values in one revision. test: `tests/ai/test_embedding_switch_daemon_lifecycle.py::test_switch_commit_is_one_revision`.
- 3.6.3 - Switch recovery reads ConfigRuntime instead of rebuilding configuration. test: `tests/ai/test_embedding_switch_daemon_lifecycle.py::test_switch_recovery_uses_runtime_snapshot`.
- 3.6.4 - API-key rotation is live and invalidates the embedding client. test: `tests/storage/test_embedding_switch_config_contract.py::test_api_key_rotation_is_live`.
- 3.6.5 - A remote runtime observing a managed structural revision verifies the journal, promotes by capturing the generation's physical targets, and rebuilds clients; crash recovery at any journal phase converges without a mixed state. test: `tests/ai/test_embedding_switch_daemon_lifecycle.py::test_managed_revision_converges_across_runtimes`.
- 3.6.6 - A remote daemon reloading after journal deletion verifies the persisted completed record and converges, including after reconnect. test: `tests/ai/test_embedding_switch_daemon_lifecycle.py::test_remote_catchup_after_journal_gc`.
- 3.6.7 - Generation GC waits for every unexpired lease's acknowledgement and bounded drains, never deletes a generation an unexpired lease still covers, and a daemon that cannot renew self-fences before expiry and reconciles before serving again. test: `tests/ai/test_embedding_switch_daemon_lifecycle.py::test_generation_gc_waits_for_acknowledgements`.
- 3.6.8 - A mutation committed after build enumeration — in-process or on a second daemon — is present in the promoted generation, deletions tombstone through replay, and no daemon acknowledges a generation missing its committed writes. test: `tests/ai/test_embedding_switch_daemon_lifecycle.py::test_write_catchup_replays_into_promoted_generation`.

### 3.7 Replace load_full_config_from_db and every caller [category: code] (depends: 1.4)
`kind: deliverable`

Targets:
- `src/gobby/cli/utils_config.py::load_full_config_from_db`
- `src/gobby/cli/runtime.py::_load_runtime_config`
- `src/gobby/cli/runtime.py::CliRuntime`
- `src/gobby/cli/__init__.py::cli`
- `src/gobby/cli/_install_prompts.py::*` — scope-reason: replace full DB config loading
- `src/gobby/cli/install.py::*` — scope-reason: replace full DB config loading
- `tests/cli/test_cli_runtime_config.py`

Replace the loader with `CliRuntime` access to one short-lived typed snapshot. Update the
click callback injection, installer reads, and prompt-install reads.

`src/gobby/cli/utils.py` re-exports both `load_config` and `load_full_config_from_db`;
section 3.8 owns that file and drops both re-exports in one edit, so this section must not
edit it.

**Acceptance:**

- 3.7.1 - Every known loader caller uses `CliRuntime`'s typed snapshot. test: `tests/cli/test_cli_runtime_config.py::test_full_loader_callers_use_cli_runtime`.
- 3.7.2 - The loader is no longer importable from its defining module. test: `tests/cli/test_cli_runtime_config.py::test_full_loader_is_not_exported`.
- 3.7.3 - CLI runtime closes its short-lived configuration resources. test: `tests/cli/test_cli_runtime_config.py::test_cli_runtime_closes_config_resources`.

### 3.8 Migrate bootstrap-oriented load_config callers [category: code] (depends: 1.4, 3.7)
`kind: deliverable`

Targets:
- `src/gobby/config/__init__.py`
- `src/gobby/cli/utils.py`
- `src/gobby/cli/utils_config.py::init_local_storage`
- `src/gobby/cli/_install_daemon.py::*` — scope-reason: use bootstrap process topology
- `src/gobby/cli/installers/service_common.py::*` — scope-reason: use bootstrap process topology
- `src/gobby/cli/postgres.py::*` — scope-reason: separate database bootstrap from runtime values
- `src/gobby/cli/qdrant.py::*` — scope-reason: use typed runtime values after database opening
- `src/gobby/cli/utils_process.py::*` — scope-reason: use bootstrap topology
- `src/gobby/storage/hub/runtime.py::*` — scope-reason: keep hub opening bootstrap-only
- `tests/cli/test_bootstrap_config_consumers.py`

Use `BootstrapConfig` for process topology and `CliRuntime` snapshots for post-database
values. Remove `load_config` and `load_full_config_from_db` imports and re-exports from
this group. `init_local_storage` still calls `deps.load_config()` and is migrated here.

**Acceptance:**

- 3.8.1 - Pre-database operations read only bootstrap fields. test: `tests/cli/test_bootstrap_config_consumers.py::test_pre_database_operations_use_bootstrap`.
- 3.8.2 - Post-database operations read one typed snapshot. test: `tests/cli/test_bootstrap_config_consumers.py::test_post_database_operations_use_runtime_snapshot`.
- 3.8.3 - Config package and CLI utilities no longer re-export either loader. test: `tests/cli/test_bootstrap_config_consumers.py::test_loader_reexports_are_removed`.

### 3.9 Migrate operational CLI and hook callers [category: code] (depends: 1.4, 3.7)
`kind: deliverable`

Targets:
- `src/gobby/cli/projects.py::*` — scope-reason: replace fresh config loading
- `src/gobby/cli/schema.py::*` — scope-reason: replace fresh config loading
- `src/gobby/cli/sessions.py::*` — scope-reason: replace fresh config loading
- `src/gobby/cli/tasks/_utils/config.py::*` — scope-reason: replace fresh config loading
- `src/gobby/cli/tasks/expand.py::*` — scope-reason: replace fresh config loading
- `src/gobby/hooks/factory.py::*` — scope-reason: replace fresh config loading
- `src/gobby/mcp_proxy/tools/sessions/_terminal_handoff.py::*` — scope-reason: replace fresh config loading
- `tests/cli/test_operational_config_consumers.py`

Route operational commands through `CliRuntime` or the daemon machine contract according
to their existing execution mode. Each command captures one revision.

**Acceptance:**

- 3.9.1 - Operational commands contain no fresh `load_config` call. test: `tests/cli/test_operational_config_consumers.py::test_operational_commands_use_runtime_authority`.
- 3.9.2 - Commands use one coherent revision for each operation. test: `tests/cli/test_operational_config_consumers.py::test_command_reads_one_revision`.
- 3.9.3 - Hooks use bootstrap-only or typed snapshot inputs according to lifecycle. test: `tests/cli/test_operational_config_consumers.py::test_hook_config_boundary`.

### 3.10 Migrate stdio and proxy callers [category: code] (depends: 1.4, 3.7)
`kind: deliverable`

Targets:
- `src/gobby/mcp_proxy/stdio.py::*` — scope-reason: replace dependency injection of load_config
- `src/gobby/mcp_proxy/stdio_daemon.py::*` — scope-reason: use bootstrap topology for port and URL resolution
- `src/gobby/mcp_proxy/stdio_proxy.py::*` — scope-reason: replace fresh config loading for tool timeouts
- `src/gobby/mcp_proxy/stdio_server.py::*` — scope-reason: replace fresh config loading
- `tests/mcp_proxy/test_stdio_config_runtime.py`

Inject bootstrap and runtime access explicitly into stdio dependency objects. Remove
`load_config` from dependency protocols and factories. `stdio_daemon` reads only
bootstrap-owned port and URL fields; `stdio_proxy` reads the registered
`mcp_client_proxy.tool_timeouts` family from a typed snapshot.

**Acceptance:**

- 3.10.1 - Stdio dependency factories no longer expose `load_config`. test: `tests/mcp_proxy/test_stdio_config_runtime.py::test_stdio_dependencies_use_runtime_access`.
- 3.10.2 - Daemon startup uses bootstrap topology and runtime snapshots at the correct boundary. test: `tests/mcp_proxy/test_stdio_config_runtime.py::test_stdio_daemon_config_boundary`.
- 3.10.3 - Proxy/server operations capture one runtime revision. test: `tests/mcp_proxy/test_stdio_config_runtime.py::test_stdio_operation_reads_one_revision`.

## P4: Mechanical Cutover and Integration
`kind: framing`

### 4.1 Remove alternate authorities and enforce the boundary [category: code] (depends: 2.3, 2.4, 2.5, 2.6, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9, 3.10)
`kind: deliverable`

Targets:
- `src/gobby/config/app.py::load_config`
- `src/gobby/storage/config_store.py::*` — scope-reason: delete remaining raw access after consumer migration
- `src/gobby/runner.py::*` — scope-reason: remove runner.config and runner.config_store
- `src/gobby/app_context.py::*` — scope-reason: remove ServiceContainer.config and ServiceContainer.config_store
- `tests/config/test_config_authority_audit.py`
- `tests/storage/test_config_store.py::*` — scope-reason: replace legacy raw-store expectations
- `tests/servers/routes/test_configuration_routes.py::*` — scope-reason: remove legacy route/reset expectations
- `docs/guides/configuration.md`

Delete every auth-independent alternate authority:

- `load_config`
- `load_full_config_from_db`
- `runner.config`, `runner.config_store`
- `ServiceContainer.config`, `ServiceContainer.config_store`
- Specialized setting writers
- Legacy MCP configuration tools

Raw ConfigStore access APIs survive this section only as the explicitly enumerated
auth-owned seam; section 4.3 deletes them.

Add a Python AST audit plus generated Rust-contract checks. The audit rejects raw Python
reads/writes outside the enumerated auth-owned seam, loader imports/re-exports, stale
mutable config fields, specialized writer routes, unregistered production keys,
registry-pattern gaps, generated-contract drift, and Gobby-mode Rust env/standalone
precedence. The audit enumerates the known migrated sites — runner lifecycle, shutdown,
subsystem, and readiness modules, voice MCP tools, attention and session routes, and the
generation-endpoint and validation-detection configuration routes — and the section 2.5
browser-authority audit runs in the same gate. The audit also rejects live-activation
registry keys absent from the section 3.4 consumer matrix, so a constructor-captured
live consumer without a subscriber or declared per-operation read fails the gate.

Update operator documentation for public, machine, bootstrap, revision, activation,
secret, YAML, and embedding contracts.

**Acceptance:**

- 4.1.1 - Python runtime code contains no alternate configuration authority or raw dotted access outside the enumerated auth-owned seam. test: `tests/config/test_config_authority_audit.py::test_python_runtime_has_one_config_authority`.
- 4.1.2 - Every Python and Rust Gobby-runtime key has one registry owner and a current generated contract entry. test: `tests/config/test_config_authority_audit.py::test_cross_language_registry_coverage`.
- 4.1.3 - Legacy loaders, mutable fields, routes, and MCP tools are absent. test: `tests/config/test_config_authority_audit.py::test_legacy_config_surfaces_are_absent`.
- 4.1.4 - Final operator behavior is documented. behavior: "Reactive runtime configuration contract" in `docs/guides/configuration.md`.

### 4.2 Add the two-daemon PostgreSQL convergence suite [category: test] (depends: 4.1)
`kind: deliverable`

Targets:
- `tests/integration/config/conftest.py`
- `tests/integration/config/test_reactive_config_multi_daemon.py`

Start two isolated daemon-runtime worker processes against one temporary PostgreSQL
schema. Each owns a separate pool and dedicated listener. Verify remote notification,
local immediate reconciliation, CAS conflict, restart pending state, secret redaction,
apply failure isolation, listener backend termination/reconnect, single-reconcile
idempotency, latest-snapshot convergence, generation lease fencing under PostgreSQL
partition, and switch write-race catch-up.

The workers use distinct daemon homes with the existing shared-passphrase KEK posture,
proving remote secret activation across homes, per-daemon secret-binding isolation on
failed activation, and fail-closed readiness for a wrong-key daemon.

Use isolated ports/state and never contact the user's running daemon.

**Acceptance:**

- 4.2.1 - A write through runtime A updates runtime B through PostgreSQL notification. test: `tests/integration/config/test_reactive_config_multi_daemon.py::test_remote_daemon_converges_after_commit`.
- 4.2.2 - Forced listener termination reconnects and reloads the latest revision. test: `tests/integration/config/test_reactive_config_multi_daemon.py::test_listener_restart_recovers_latest_snapshot`.
- 4.2.3 - Apply failure keeps the committed desired revision, only the failing daemon retains its old active state, and the other daemon independently activates the committed revision. test: `tests/integration/config/test_reactive_config_multi_daemon.py::test_apply_failure_is_process_local`.
- 4.2.4 - A local commit reconciles once and its own notification invokes no subscriber again. test: `tests/integration/config/test_reactive_config_multi_daemon.py::test_local_commit_reconciles_once`.
- 4.2.5 - A managed embedding switch on daemon A converges daemon B, and a crash at the flip or commit boundary recovers to a coherent structural state. test: `tests/integration/config/test_reactive_config_multi_daemon.py::test_managed_switch_converges_across_daemons`.
- 4.2.6 - Two daemons with distinct homes and the shared-passphrase posture converge on a remote API-key rotation. test: `tests/integration/config/test_reactive_config_multi_daemon.py::test_remote_secret_rotation_with_shared_kek`.
- 4.2.7 - A wrong-key daemon fails closed and never reports healthy. test: `tests/integration/config/test_reactive_config_multi_daemon.py::test_wrong_kek_daemon_fails_closed`.
- 4.2.8 - A same-reference rotation with one failing daemon proves per-daemon payload isolation and public redaction. test: `tests/integration/config/test_reactive_config_multi_daemon.py::test_same_reference_rotation_failure_isolation`.
- 4.2.9 - Concurrent cross-process writes sharing a stale expected revision yield exactly one commit and one typed conflict. test: `tests/integration/config/test_reactive_config_multi_daemon.py::test_cross_process_cas_conflict`.
- 4.2.10 - A restart-required change keeps desired and active state separated on both daemons until a worker restart activates it. test: `tests/integration/config/test_reactive_config_multi_daemon.py::test_restart_pending_state_across_daemons`.
- 4.2.11 - A daemon losing PostgreSQL connectivity while HTTP and Qdrant stay live self-fences embedding requests before lease expiry, GC proceeds safely, and the daemon reconciles and resumes serving after reconnect. test: `tests/integration/config/test_reactive_config_multi_daemon.py::test_partitioned_daemon_self_fences_before_gc`.
- 4.2.12 - Create, update, and delete races at the build, flip, config-commit, and promotion boundaries across two daemons converge with every committed mutation present in the promoted generation. test: `tests/integration/config/test_reactive_config_multi_daemon.py::test_switch_write_races_converge`.

### 4.3 Delete auth-owned raw configuration access [category: code] (depends: 4.1)
`kind: deferred`

Deletes the raw ConfigStore get/set/batch/delete/reset/default APIs once the #19650
auth consumers use the restricted typed API, then reruns the section 4.1 audit without
the auth-owned seam allowance (`tests/config/test_config_authority_audit.py`). The
original obligations: 4.3.1 — raw ConfigStore access APIs are absent and the audit
passes without the seam allowance; 4.3.2 — auth consumers read and write credentials
only through the restricted typed API.

This work is deferred because it is gated on external auth work that a manifest entry
cannot encode. The deferral task is created at expansion, parented under epic #19645 as
its tail work, labeled `deferred-from:reactive-config-store:4.3`, and blocked by both
#19650 and the leaf created from section 4.1. #17769 carries no blocker by explicit
decision: its consumers do not exist yet and are born onto the restricted typed API
this plan provides.

```yaml
deferral:
  task_ref: "#19982"
  reason: >-
    Final raw-access deletion is gated on #19650 auth consumers adopting the
    restricted typed API; an in-plan manifest entry cannot encode an external
    task dependency. Expansion created the epic-parented tail task #19982,
    blocked by #19650 and the section 4.1 leaf #19980.
  owner: "backend-developer"
  original_acceptance_items:
    - "4.3.1"
    - "4.3.2"
```

## V1 Plan Changelog
`kind: verification`

- Initial draft established registry, CAS, runtime, interface, consumer, and cutover
  phases.
- Review repair round 1:
  - Replaced phase dependencies with concrete leaf dependencies.
  - Preserved authenticated effective-config and service-capability contracts.
  - Added Rust registry generation and Gobby-mode precedence removal.
  - Added every uncovered loader re-export and caller.
  - Replaced hand-editing of the baseline with a numbered migration (reverted in round 3).
  - Specified the pool-exempt notification connection and remote-daemon consumer.
  - Removed compensating rollback in favor of local prepare/commit.
  - Split topology, stateful subscribers, loops, loader groups, and integration testing.
  - Named the companion coverage ledger and concrete module decomposition.
- Review repair round 2 (index-verified):
  - 1.2 gained the schema-identity fan-out — `schema_expected_identity.json`,
    `scripts/generate_schema_expected_identity.py`, and both Rust identity contract
    tests — plus acceptance item 1.2.5. Any change to the embedded schema assets changes
    `root_hash()`, which `gobby install` and two `cargo test` targets assert exactly.
  - 2.6 corrected Rust module paths: `crates/gcode/src/config/mod.rs` does not exist
    (the module root is `crates/gcode/src/config.rs`), and both crates' new test
    submodules must be declared from their existing `config/tests.rs`.
  - 2.2 widened to `configuration_effective.py::*`; five further `DaemonConfig`-consuming
    helpers in that module move to snapshot input.
  - 3.8 gained `src/gobby/cli/utils_config.py::init_local_storage`, an uncovered
    `load_config` caller that would have blocked 4.1.
  - 3.7 symbol-qualified `src/gobby/cli/__init__.py::cli`; `src/gobby/cli/utils.py` is now
    owned by 3.8 alone.
  - `catalog.manifest.json` and `schema_expected_identity.json` are symbol-indexed, so
    both use `::*` scope.
  - 4.1 `depends` gained 3.7. Ledger `owner_agent` for 4.2 corrected to `qa-dev`;
    `test-architect` is not an installed agent.
  - V2 corrected cargo package names to `gobby-code` and `gobby-daemon`.
- Review repair round 3 (convention correction):
  - 1.2 no longer adds migration 376. Through the 0.5.0 pre-release period baseline 375
    is the sole schema authority and `MIGRATIONS` stays empty; commit `a3b56649a`
    deleted a real migration 376 and folded it into the baseline, establishing that
    convention. The deliverable now edits `baseline.sql`, refreshes `BASELINE_CHECKSUM`,
    and relies on the runner's existing-hub re-apply path, with acceptance items
    rewritten accordingly.
  - V2 reordered: `gdaemon` is rebuilt before the identity generator runs, because
    `scripts/generate_schema_expected_identity.py` shells out to the installed binary.
    Added the catalog-manifest freshness test.
- No adversarial-review round has run.

**Round 1** `kind: enhancement`

- enhancer_run: a1ee360e-6f5d-43f9-91a4-33d18b89b843
- enhancer_session: 6c84743c-249a-4d58-ac63-712fa85e23f8
- converged: false
- suggestions_presented: 6
- accepted:
  - E1 / better / notification idempotency and coalescing in 1.4, proved in 4.2
  - E2 / better / assign the `config_event` WebSocket producer (narrowed form)
  - E3 / bigger / render activation, pending-restart, failed-live, and managed routing in 2.5
  - E4 / better / `expected_revision` and typed 409 on YAML replacement in 2.4
  - E6 / better / serialize subscriber registration against snapshot swaps in 1.4/3.4
- declined:
  - E5 / better / explicit ConfigRuntime readiness transitions in 1.4
- resolution_notes: >-
  E1 added the idempotency/coalescing paragraph to 1.4 plus acceptance 1.4.6 and 4.2.4;
  the pool-exempt listener receives its own `NOTIFY`, so the drafted design reconciled
  every local commit twice. E2 was accepted in narrowed form: the enhancer's envelope
  both emitted on failed-live-only changes and suppressed non-advancing revisions, which
  is self-contradictory for a revision-only body. 2.1 now defines a `config_event`
  carrying `{revision}` alone, emitted once per newly reconciled revision, with
  pending-restart and failed-live metadata riding the values refetch it triggers;
  acceptance 2.1.5 covers the contract and 3.1.4 covers startup registration, keeping the
  P2-contract/P3-wiring split the plan already uses so 2.5 keeps its single 2.1
  dependency. E3 extended 2.5 with `configFields.tsx` and
  `MemoryKnowledgeSection.tsx::EmbeddingsGroup` targets and acceptance 2.5.5/2.5.6; the
  managed-key half prevents a plain PATCH on `ai.embeddings.*` surfacing
  `managed_activation_required` as a raw error. E4 added `expected_revision` and the
  typed 409 to 2.4 plus acceptance 2.4.5; a CAS with no specified expected value is not a
  CAS. E6 added the serialized registration handshake to 1.4 (where ConfigRuntime owns
  it) and 3.4 (where adapters consume it) plus acceptance 3.4.5. E5 was declined: 1.4.5
  already asserts full reload before health recovery, and a reason-carrying readiness
  object implies a health surface this plan does not scope. Acceptance items went from 79
  to 87; the companion ledger and registry `plan_hash` were regenerated to match.

**Round 1** `kind: verification`

- reviewer_run: 44be3611-888f-4b9b-ad44-0796ea8f9e7a
- reviewer_session: 9f29efe8-b48a-4c38-86da-bcaed09b52e7
- verdict: needs_review
- findings:
  - APR1-001 / blocking / apply-failure-state-consistency / 1.4
  - APR1-002 / blocking / contract-producer-dependencies / 2.3
  - APR1-003 / blocking / external-cutover-blockers / 4.1
  - APR1-004 / blocking / cross-language-authority-audit / 4.1
  - APR1-005 / blocking / schema-identity-generation-path / 1.2
  - APR1-006 / blocking / rust-fallback-fanin-target / 2.6
  - APR1-007 / blocking / alternate-authority-adjacent-callers / 4.1
  - APR1-008 / blocking / auth-mode-ownership / 1.1
  - APR1-009 / blocking / baseline-receipt-refresh-path / 1.2
  - APR1-010 / blocking / listener-runtime-role / 1.4
  - APR1-011 / blocking / listen-startup-catchup / 3.1
  - APR1-012 / blocking / reconciliation-monotonic-serialization / 1.4
  - APR1-013 / blocking / subscriber-work-bounds / 3.4
  - APR1-014 / blocking / managed-multidaemon-activation / 3.6
  - APR1-015 / blocking / browser-event-catchup / 2.5
- resolution_notes: >-
  All 15 findings were verified against the repository and accepted; repairs are folded
  into this revision. Two coordinator amendments were presented and approved with the
  votes: APR1-008 lands in interim form (auth_mode stays BootstrapConfig-owned and out
  of the registry; #19650 §2.2 deletes the field, so this plan only relocates the
  HTTP read it would otherwise strand), and APR1-003 lands with the 4.1/4.3 split plus
  an enforceable expansion-time blocker on #19650 only — #17769 carries a typed
  deferral instead of a task-graph edge because its consumers do not exist yet and are
  born onto the restricted typed API this plan provides. Repairs: 1.4.3/4.2.3 rewritten
  (post-commit failure semantics); 2.1 added to 2.3/2.4/3.1 depends; 4.1 split with 4.3
  deletion leaf; browser-authority audit added to 2.5 (2.5.9) with App.test.tsx and the
  audit test as targets; 1.2 gained runner.rs predecessor-receipt refresh (1.2.2
  rewritten, 1.2.6 added) and the deterministic five-step identity regeneration order,
  which V2 now mirrors; 2.6 gained context.rs and the REPEATABLE READ fan-in; 3.2
  gained voice/attention/session-route targets and 3.2.4; 3.3 gained interim auth_mode
  ownership, the http.py capture target, and 3.3.4; 1.4 gained the runtime-role listener
  factory (1.4.7), monotonic serialization with watermark (1.4.8), subscription-window
  catch-up, and subscriber work bounds; 3.1 startup order now subscribes before the
  initial load (3.1.5); 3.4 gained deadlines/cancellation (3.4.6, 3.4.7); 3.6 gained
  journal-tied multi-daemon managed activation (3.6.5) and 4.2.5; 2.5 gained the
  revision watermark and reconnect refetch (2.5.7, 2.5.8). Acceptance items went from
  87 to 103; the ledger and registry plan_hash were regenerated to match.

```json plan-review-round
{"evidence_id":"d0659591-035c-460e-aae8-0c74a98a15b3","plan_hash":"e5567cf63afbad5d518c778cb83a7e68cb7b233f03f369b3647ec02f44d2c28c","round_number":1,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"49b9456f4870ef3ab9f049a9a43b31d753c73be8e1b790123bf0569d2d4e47fd","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":4,"emitted_findings":15,"total":19},"evidence_id":"d0659591-035c-460e-aae8-0c74a98a15b3","lanes":[{"candidate_count":4,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":7,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":8,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":22,"manifest_digest":"781e65d8454cdc5d329884d532f4ec741a3836b95cf8b8866d47093f8967a2de","status":"valid"},"source_digest":"224c4440552f5a21b38c0047b46b7c6ded483f62530e2c216d3eb393b66f57de","version":1},"findings":[{"category":"unhandled-edge","check_key":"apply-failure-state-consistency","description":"Acceptance 1.4.3 says subscriber preparation failure performs no database write, while the constraints require committed desired state to survive an apply failure without compensation. Acceptance 4.2.3 also reads as preventing the committed desired change and successful activation in the other process.","finding_id":"APR1-001","fix":"Rewrite 1.4.3 so desired state and revision remain committed, the failing daemon performs no active swap or compensating write, and failed-live metadata is recorded. Rewrite 4.2.3 so only the failing daemon retains its old active state while another daemon independently activates the committed revision when preparation succeeds.","location":"P1 §1.4 and P4 §4.2","prevention":"For each apply failure, state the database desired value and revision, the failing daemon active value, other daemon active values, emitted event, and failure metadata.","principle":"Every failure-path acceptance item must preserve the governing desired-state, revision, and per-process active-state transitions.","root_cause":"The acceptance text conflates pre-persistence validation failure with post-commit live-activation failure.","section_id":"1.4","severity":"blocking"},{"category":"bad-sequencing","check_key":"contract-producer-dependencies","description":"Section 2.3 reuses the HTTP service and errors, section 2.4 reuses the universal PATCH 409 contract, and section 3.1 registers the section 2.1 publisher, yet none depends on 2.1. Expansion can schedule each consumer before its contract exists.","finding_id":"APR1-002","fix":"Add `2.1` to the dependency lists of sections 2.3, 2.4, and 3.1.","location":"P2 §§2.3–2.4 and P3 §3.1","prevention":"For every cross-section reference, identify the producing leaf and add a direct or transitive dependency before manifest derivation.","principle":"A leaf that consumes a contract owned by another leaf must depend on the contract-producing leaf.","root_cause":"Sections 2.3, 2.4, and 3.1 retained their 1.4 dependency after they began consuming section 2.1 service, error, and event contracts.","section_id":"2.3","severity":"blocking"},{"category":"bad-sequencing","check_key":"external-cutover-blockers","description":"Raw-access deletion is required to wait for #19650 and #17769, but #19645 has no blockers, #19650 is open, and #17769 is blocked. Section 4.1 can therefore execute and delete auth-owned access before its consumers migrate.","finding_id":"APR1-003","fix":"Split 4.1 into an auth-independent audit leaf and a raw-access deletion leaf. Before expansion, add enforceable blockers from the cutover work to #19650 and #17769, name both refs in the deletion leaf, and keep final deletion acceptance on that blocked leaf.","location":"P4 §4.1","prevention":"Before expansion, verify that every external prerequisite named with wait/after language is represented by a task dependency or a deferred cutover leaf.","principle":"Destructive final cutover must be guarded by enforceable dependencies rather than prose.","root_cause":"The plan names auth prerequisites globally, while #19645 and the 4.1 manifest path carry no dependency on the open auth work.","section_id":"4.1","severity":"blocking"},{"category":"missing-requirement","check_key":"cross-language-authority-audit","description":"The root task requires every production configuration read/write path to migrate, but the authority audit cannot detect remaining browser direct reads, specialized writers, reset calls, or revisionless mutations. `web/src/__tests__/App.test.tsx` still pins the specialized UI-settings contract and is absent from Targets.","finding_id":"APR1-004","fix":"Add a TypeScript browser-authority audit target and acceptance item that rejects direct configuration fetches, specialized writers, reset endpoints, and mutations outside the revision-carrying client. Add `web/src/__tests__/App.test.tsx::*` to section 2.5 and include the audit in V2.","location":"P2 §2.5 and P4 §4.1","prevention":"Inventory and audit Python, Rust, TypeScript/browser, generated assets, and integration mocks whenever a legacy authority is removed.","principle":"A whole-repository authority cutover needs an exhaustive audit for every production language and client surface.","root_cause":"The final audit specifies Python AST and generated Rust checks but leaves browser TypeScript coverage to selected behavioral tests.","section_id":"4.1","severity":"blocking"},{"category":"weak-testability","check_key":"schema-identity-generation-path","description":"`generate_schema_expected_identity.py` prefers `~/.gobby/bin/gdaemon`, while `cargo build` produces `target/release/gdaemon`. The catalog test writes only with `UPDATE_GCORE_SCHEMA_MANIFEST`, and without a configured isolated database it skips. The stated sequence can therefore use stale identity and catalog bytes even though `root_hash()` includes the catalog.","finding_id":"APR1-005","fix":"Specify this deterministic order in 1.2/V2: update `BASELINE_CHECKSUM`; regenerate the catalog against an isolated PostgreSQL database with `UPDATE_GCORE_SCHEMA_MANIFEST=1`; build release `gdaemon`; run `uv run python scripts/generate_schema_expected_identity.py --gdaemon target/release/gdaemon`; then rerun catalog freshness without update mode and both identity contracts.","location":"P1 §1.2 and V2","prevention":"Trace each generated artifact to its actual writer, required environment, exact binary argument, and non-update verification command.","principle":"A regeneration recipe must select the newly built producer binary and invoke every write path whose bytes contribute to the asserted identity.","root_cause":"The recipe assumes `cargo build` changes the generator's default binary and treats a conditional freshness test as the catalog generator.","section_id":"1.2","severity":"blocking"},{"category":"traceability","check_key":"rust-fallback-fanin-target","description":"`Context::resolve_services` currently invokes independent resolver paths that perform per-key reads. The promised one-revision repeatable-read fallback cannot be implemented across service families without changing `crates/gcode/src/config/context.rs`, which is absent from section 2.6 Targets.","finding_id":"APR1-006","fix":"Add `crates/gcode/src/config/context.rs::*` with a scope reason for fallback transaction fan-in. Specify one read-only REPEATABLE READ transaction that captures `config_state.revision` and all machine-visible rows once, resolves every service from that immutable map, and returns the captured revision; extend the Rust test across multiple service families.","location":"P2 §2.6","prevention":"For each coherent-snapshot promise, trace every resolver to the highest shared caller and target that caller explicitly.","principle":"Exact Targets must include the fan-in where a promised cross-consumer transaction boundary is established.","root_cause":"The plan targets individual Rust service resolvers while omitting the context fan-in that calls them independently.","section_id":"2.6","severity":"blocking"},{"category":"traceability","check_key":"alternate-authority-adjacent-callers","description":"Current production consumers outside all exact Targets still read `runner.config` or write raw/specialized settings, including runner lifecycle/readiness modules, voice MCP tools, generation-endpoint routes, install/embedding key inventories, and session-start agent defaults. Section 4.1 would either break those paths or leave alternate authorities.","finding_id":"APR1-007","fix":"Add the verified runner lifecycle, shutdown, subsystem, readiness, voice, generation-endpoint, install-state, embeddings, and session-start modules to the appropriate sections 2.1/2.3/3.1–3.5. Make 4.1 depend on their migration and require its audit to enumerate these known sites before deleting fields and facade methods.","location":"P3 consumer leaves and P4 §4.1","prevention":"Run a class-wide caller/consumer sweep for each deleted symbol and assign every result to an exact earlier Target before final cutover.","principle":"A deletion leaf may run only after every constructor, reader, writer, and test seam for the deleted surface has an owned migration target.","root_cause":"The caller inventory stopped before several runner lifecycle/readiness modules and specialized raw writers.","section_id":"4.1","severity":"blocking"},{"category":"missing-requirement","check_key":"auth-mode-ownership","description":"`auth_mode` is currently parsed and written in bootstrap configuration and read by hooks before database access, but the plan excludes it from bootstrap ownership and never registers or migrates it. Registry conflict checks and final loader removal would leave an undefined trust-boundary input.","finding_id":"APR1-008","fix":"Retain `auth_mode` as a BootstrapConfig-owned field: add it to the bootstrap ownership list, exclude it from the runtime registry, keep installer persistence bootstrap-scoped, and make HTTP startup capture it from BootstrapConfig. Add ownership tests covering installer, hook preflight, and HTTP construction.","location":"Constraints, P1 §1.1, and P3 bootstrap migration","prevention":"Compare every BootstrapConfig field and pre-database consumer against the ownership list before registry compilation.","principle":"Every pre-database trust-boundary input must have one explicit owner and activation policy.","root_cause":"The exclusive BootstrapConfig list omits the existing `auth_mode` field without assigning or migrating its installer, hook, and HTTP consumers.","section_id":"1.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"baseline-receipt-refresh-path","description":"The existing-hub reapply acceptance cannot pass with the current repository. A previous checksum never reaches baseline application, and even a forced path would collide on the existing receipt. `crates/gcore/src/schema/runner.rs` is also absent from Targets.","finding_id":"APR1-009","fix":"Add `crates/gcore/src/schema/runner.rs::*` to 1.2. Recognize only the exact predecessor `baseline@375` receipt as refreshable, apply the idempotent baseline, replace/upsert that receipt in the same transaction, and continue rejecting arbitrary checksum or filename mismatches. Test successful predecessor refresh, receipt replacement, data preservation, and corrupt-receipt rejection.","location":"P1 §1.2","prevention":"Trace the exact old-receipt state through classification, DDL execution, and receipt commit before claiming in-place baseline refresh.","principle":"A claimed schema refresh path must be reachable from the current classifier and replace its existing receipt atomically.","root_cause":"The current runner classifies any checksum-mismatched baseline-375 receipt as `CorruptPartial`, and `apply_baseline` inserts rather than replaces the existing version-375 receipt.","section_id":"1.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"listener-runtime-role","description":"Opening the LISTEN connection directly from `conninfo` runs under the login role rather than the fixed daemon runtime role used by pooled connections. Tests with an owner DSN can pass while production least-privilege behavior remains untested.","finding_id":"APR1-010","fix":"Add the exact PostgreSQL adapter target needed to expose a dedicated async runtime connection factory, or apply and assert `SET ROLE gobby_daemon_runtime` before `LISTEN`. Test `current_user`, notification receipt, reconnect, and closure under the production runtime role.","location":"P1 §1.4 and P3 §3.1","prevention":"For every connection outside a configured pool, verify application name, role assumption, current-user assertion, timeout, and close behavior.","principle":"Pool-exempt database connections must preserve the daemon pool's least-privilege session identity.","root_cause":"`PostgresHubDatabase.conninfo` contains connection parameters but not the pool's `SET ROLE gobby_daemon_runtime` configure/check hooks.","section_id":"1.4","severity":"blocking"},{"category":"unhandled-edge","check_key":"listen-startup-catchup","description":"A revision committed after the initial snapshot read and before LISTEN acknowledgement is permanently missed until another revision happens. The daemon can start post-database services with stale configuration.","finding_id":"APR1-011","fix":"Require ConfigRuntime startup and reconnect to await connection establishment and LISTEN acknowledgement, then perform a full revision-coherent reload before readiness or consumer construction. Add a deterministic commit-in-the-window test that converges without a later notification.","location":"P1 §1.4, P3 §3.1, and P4 §4.2","prevention":"For every lossy notification channel, test a commit in each read/register and reconnect/register window.","principle":"Notification consumers must close the initial snapshot-to-subscription race before becoming ready.","root_cause":"Startup reads the snapshot first and registers LISTEN second without a post-LISTEN catch-up read.","section_id":"3.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"reconciliation-monotonic-serialization","description":"Concurrent reloads or preparations can complete out of order, publish an older snapshot, invoke subscribers twice, or emit stale `config_event` callbacks despite revision-based coalescing.","finding_id":"APR1-012","fix":"Specify one ConfigRuntime reconciliation mutex plus a maximum-requested-revision watermark shared by local commits, LISTEN notifications, reconnects, and registration. Recheck the published revision after awaited I/O, discard stale reload/preparation results, and add delayed out-of-order reload and registration-race tests.","location":"P1 §1.4 with P3 §§3.1/3.4","prevention":"Enumerate all reconciliation entry points and test delayed older work completing after newer work.","principle":"Every asynchronous publication path for revisioned state must share one monotonic serialization rule.","root_cause":"Only subscriber registration is explicitly serialized; local commits, notification reloads, reconnect reloads, and registration catch-up can overlap.","section_id":"1.4","severity":"blocking"},{"category":"unhandled-edge","check_key":"subscriber-work-bounds","description":"A hung provider constructor or client drain can stall PATCH completion, LISTEN progress, later revisions, and shutdown. After a successful swap, an old-resource drain failure also has no defined effect on the published snapshot.","finding_id":"APR1-013","fix":"Add named per-subscriber preparation and disposal deadlines. Preparation timeout must dispose completed replacements, preserve old active state, and record affected keys. Publish the new snapshot before bounded old-resource drain; drain failure must never roll back active state. Add timeout and shutdown-cancellation tests.","location":"P1 §1.4 and P3 §3.4","prevention":"For every subscriber phase, specify deadline, cancellation cleanup, active-state effect, failure metadata, and shutdown ownership.","principle":"One live subscriber must not be able to block global configuration convergence or daemon shutdown indefinitely.","root_cause":"Prepare-all and old-resource drain phases have no deadlines, cancellation contract, or post-swap drain failure policy.","section_id":"3.4","severity":"blocking"},{"category":"missing-requirement","check_key":"managed-multidaemon-activation","description":"When daemon A completes an embedding switch, daemon B receives the managed structural revision but has no specified authorization, active-state promotion, client invalidation, or recovery path. Generic rejection rules do not define how an already-committed managed revision converges.","finding_id":"APR1-014","fix":"Tie the committed configuration revision to the existing embedding lifecycle journal. Define how local and remote runtimes verify the completed switch, promote active structural values after shared aliases are ready, rebuild dependent clients, and recover each existing journal phase after restart. Add two-daemon switch convergence and crash-at-flip/commit-boundary tests.","location":"P1 §1.4, P3 §3.6, and P4 §4.2","prevention":"For every managed mutation, trace local commit, remote notification, remote active transition, restart recovery, and crash boundaries.","principle":"A shared managed revision needs deterministic activation and recovery semantics on every daemon that observes it.","root_cause":"The switch coordinator is daemon-local, while its structural configuration commit is hub-wide and the remote reconciliation path is unspecified.","section_id":"3.6","severity":"blocking"},{"category":"unhandled-edge","check_key":"browser-event-catchup","description":"A `config_event` emitted while the socket is disconnected is lost, and a higher event arriving during an in-flight refetch can be dropped by simple request coalescing. The browser can remain indefinitely stale.","finding_id":"APR1-015","fix":"Track the maximum observed configuration revision, refetch whenever the WebSocket reconnects, ignore responses older than the rendered revision, and issue a trailing refetch whenever a response revision is below the observed watermark. Add both race tests.","location":"P2 §§2.1/2.5","prevention":"Test mutation while disconnected and a higher event arriving during every in-flight refetch.","principle":"A client using a non-durable event stream must reconcile from authoritative revisioned state after disconnects and overlapping fetches.","root_cause":"The browser coalesces observed events but has no reconnect refetch or maximum-revision watermark.","section_id":"2.5","severity":"blocking"}],"reviewer_session":"9f29efe8-b48a-4c38-86da-bcaed09b52e7","round":1,"round_number":1,"verdict":"needs_review"},"session_id":"a05c50fe-9266-4c23-8944-e17d5ef6ffed"}
```

**Round 2** `kind: verification`

- reviewer_run: beb9e352-48d3-44d4-bc4c-a354b897aaf3
- reviewer_session: 85c4e853-aed5-4ca3-bbf5-00e978cf4c24
- verdict: needs_review
- findings:
- APR2-001/blocking/§4.3 external blocker and #17769 deferral were prose-only and unenforceable
- APR2-002/blocking/generation-endpoint and validation-detection routes were untargeted raw consumers
- APR2-003/blocking/no cross-plan edges ordered #19650 against the 1.2 and 3.3 leaves
- APR2-004/blocking/§§3.3 and 3.4 shared http.py::* with no dependency between them
- APR2-005/blocking/switch completion deletes the journal remote verification relied on
- APR2-006/blocking/same-reference secret rotation could expose an unactivated payload after failed apply
- APR2-007/blocking/Python snapshot reads lacked an isolation protocol
- APR2-008/blocking/async listener plus sync repository had no executor boundary or late-result policy
- APR2-009/blocking/dynamic key segments had no canonical lossless encoding
- APR2-010/blocking/multi-daemon remote hubs lacked a shared KEK posture and fail-closed readiness
- APR2-011/blocking/§3.3 assigned pool sizing to bootstrap, stranding registered database_concurrency keys
- APR2-012/blocking/superseded successful preparations were discarded without disposal
- APR2-013/blocking/multi-reference swaps plus separate snapshot publication allowed mixed epochs
- APR2-014/blocking/no committed-with-apply-failure response contract on HTTP/MCP/YAML
- resolution_notes: All 14 findings accepted with one user amendment: the §4.3 deferral
  task is created at expansion and parented under epic #19645 as tail work rather than
  free-floating. §4.3 converted to a kind: deferred section (dangling task_ref until
  expansion); §3.4 now depends on 3.3; §2.1 gained the generation-endpoints target,
  typed activation, and the committed-with-apply-failure contract mirrored in §§2.3/2.4;
  §3.2 gained the validation-detection target; §3.6 gained the embedding_switch.py
  target and a GC-surviving completed record; §1.1 gained the dynamic-segment codec with
  Rust parity in §2.6; §1.3 gained REPEATABLE READ snapshot reads; §1.4 gained secret
  bindings, the async executor boundary, superseded-preparation disposal, the
  runtime-active bundle, and KEK fingerprint readiness; §3.3 gained two-stage pool
  sizing and cross-plan expansion edges; §4.2 gained distinct-home KEK, rotation, and
  isolation tests. Acceptance items went from 103 to 121 deliverable items (4.3's two
  moved into the deferral); the ledger and registry plan_hash were regenerated to match.

```json plan-review-round
{"evidence_id":"8a8c3f53-0228-4c5c-bcd3-1fcca2683115","plan_hash":"1e1a682306d693d979ecb0c03db46af39bfa9fa831668858d9453c84c97fb6c6","round_number":2,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"552c862cc8180d050d9074dfc3a7e5e68959c80b8015424cf2aec73eb4054574","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":1,"emitted_findings":14,"total":15},"evidence_id":"8a8c3f53-0228-4c5c-bcd3-1fcca2683115","lanes":[{"candidate_count":3,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":2,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":10,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":23,"manifest_digest":"95e2896d04d4191b3cfd1a019e9d87201909d9d4883a0910896106a0369c1dc4","status":"valid"},"source_digest":"be8d2e3f4cbde146ada8250a68b77a330eb0f939f1daaebc5e020444d062a697","version":1},"findings":[{"category":"bad-sequencing","causal_finding_id":"APR1-003","causal_section_ids":["4.1","4.3"],"check_key":"external-cutover-blockers","description":"The derived 4.3 entry depends only on 4.1; it cannot encode #19650. The claimed #17769 typed deferral is also absent, so neither round-1 sequencing promise is enforceable.","finding_id":"APR2-001","fix":"Change 4.3 to a formal deferred section for 4.3.1/4.3.2 backed by a dedicated open raw-access-deletion task labeled `deferred-from:reactive-config-store:4.3` and blocked by #19650. Remove the #17769 typed-deferral claim because no current acceptance item belongs to its future consumers, then regenerate the ledger.","introduced_in_round":1,"location":"P4 §4.3","prevention":"For every external wait/after requirement, inspect the derived entry and the live prerequisite task for an actual dependency or valid deferral before approval.","principle":"Destructive cutover prerequisites must be represented by enforceable task state.","root_cause":"The repair describes an external blocker and a typed deferral in prose, while the manifest only supports internal section dependencies and the artifact contains no deferred section.","section_id":"4.3","severity":"blocking"},{"category":"traceability","causal_finding_id":"APR1-007","causal_section_ids":["3.2","4.1"],"check_key":"alternate-authority-adjacent-callers","description":"`configuration_generation_endpoints.py` still performs raw ConfigStore, secret, and runtime-config writes, while `configuration_validation_detection.py` still reads `ServiceContainer.config`; neither file is targeted.","finding_id":"APR2-002","fix":"Add `configuration_generation_endpoints.py::*` to 2.1 and migrate probe-gated activation to one revisioned typed mutation including secret handling. Add `configuration_validation_detection.py::*` to the generic snapshot-consumer leaf. Add focused acceptance tests and enumerate both modules in 4.1's audit.","introduced_in_round":1,"location":"P2 §2.1, P3 consumer migration, and P4 §4.1","prevention":"Expand each router/factory registration fan-out and assign every raw reader, writer, and mutable-config replacement to an exact target before cutover.","principle":"Final authority deletion must own every registered production reader and writer before the audit runs.","root_cause":"The round-1 caller repair added several adjacent modules but skipped two subroutes registered by the configuration router.","section_id":"4.1","severity":"blocking"},{"category":"bad-sequencing","causal_finding_id":"APR1-008","causal_section_ids":["Constraints","3.3"],"check_key":"cross-plan-interim-ownership-sequencing","description":"The plan says #19650 deletes `auth_mode` later, yet #19650 can run before or concurrently with 1.2/3.3 and edit the same files. The 4.3-only blocker does not establish the required earlier half of the sequence.","finding_id":"APR2-003","fix":"At expansion, make #19650 depend on the created 1.2 and 3.3 leaves, and make the dedicated deferred raw-access-deletion task from APR2-001 depend on #19650. Record those external edges in a typed expansion handoff and verify the resulting acyclic chain before dispatch.","introduced_in_round":1,"location":"P1 §1.2, P3 §3.3, and external plan #19650 §2.2","prevention":"For every interim ownership amendment, build the complete predecessor → external task → cleanup chain and inspect overlapping targets before expansion.","principle":"An interim owner and its later deleting plan need an acyclic, enforceable cross-plan sequence.","root_cause":"The approved interim auth_mode relocation and #19650 deletion touch the same bootstrap, HTTP, baseline, and identity surfaces without graph edges ordering those edits.","section_id":"3.3","severity":"blocking"},{"category":"bad-sequencing","causal_finding_id":"APR1-008","causal_section_ids":["3.3"],"check_key":"shared-target-serialization","description":"Sections 3.3 and 3.4 both target `src/gobby/servers/http.py::*` and both depend only on 3.1, so expansion may dispatch conflicting file-wide edits concurrently.","finding_id":"APR2-004","fix":"Add 3.3 to 3.4's dependencies so bootstrap/auth construction lands before stateful subscriber rewiring, then keep `http.py::*` ownership sequential.","introduced_in_round":1,"location":"P3 §§3.3–3.4","prevention":"After every target-list repair, sweep duplicate target paths and verify a dependency path or exact non-overlapping symbols for each pair.","principle":"Independent leaves with a shared file-wide target must be sequenced or partitioned.","root_cause":"Adding the interim HTTP auth-mode capture to 3.3 created a second `http.py::*` owner beside 3.4 without adding a dependency.","section_id":"3.4","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"APR1-014","causal_section_ids":["3.6","4.2"],"check_key":"managed-journal-retention","description":"A remote daemon can reload the committed structural revision after the switch journal has been deleted, leaving no run/revision evidence with which to perform the promised verification.","finding_id":"APR2-005","fix":"Add `src/gobby/ai/embedding_switch.py::*` to 3.6. Persist a compact completed record containing run ID, committed revision, and target aliases through GC and retain it until the next managed switch supersedes it. Test remote catch-up after journal cleanup and reconnect.","introduced_in_round":1,"location":"P3 §3.6 and P4 §4.2","prevention":"Trace managed evidence through local commit, GC, remote delayed reload, reconnect, and next-switch replacement; target every persistence model involved.","principle":"Remote managed activation needs durable evidence that survives missed notifications and coordinator cleanup.","root_cause":"The repair relies on the lifecycle journal for remote verification, while the existing completion path deletes that journal and the journal module is absent from 3.6 Targets.","section_id":"3.6","severity":"blocking"},{"category":"unhandled-edge","check_key":"secret-rotation-active-binding","description":"When secret rotation commits under the same reference and preparation fails locally, the old active reference resolves to new desired plaintext. The failing daemon can therefore use or return a payload it never activated.","finding_id":"APR2-006","fix":"Store private non-serializable desired and active secret bindings with content fingerprints in each coherent snapshot. Preserve the previous active binding on failed preparation, make machine/subscriber consumers use that binding, and add a two-daemon same-reference rotation failure test proving public redaction and per-daemon payload isolation.","location":"P1 §§1.3–1.4, P2 §2.2, and P4 §4.2","prevention":"For every secret transition, compare desired reference, desired payload version, active reference, active payload version, subscriber state, and machine output on success and failure.","principle":"Last-good active state must bind the exact secret payload that successfully activated.","root_cause":"Active state retains a stable secret reference, while same-reference rotation overwrites the referenced payload and machine output resolves it from current storage.","section_id":"1.4","severity":"blocking"},{"category":"unhandled-edge","check_key":"python-snapshot-isolation","description":"Under ordinary READ COMMITTED behavior, a writer can commit between the global-revision and row/secret queries, producing a mixed snapshot whose metadata claims the wrong revision.","finding_id":"APR2-007","fix":"Require `ConfigRepository` to open one read-only REPEATABLE READ transaction before its first query and read `config_state`, all registered rows, row revisions, and secret bindings inside it. Reject row revisions above the captured global revision and add the deterministic paused-reader race test.","location":"P1 §§1.3–1.4 and P3 §3.1","prevention":"Pause between every multi-query snapshot step, commit a concurrent writer, and prove the result is wholly old or wholly new.","principle":"A revisioned snapshot must read its revision, rows, row revisions, and secret bindings from one database snapshot.","root_cause":"Python complete-snapshot reads promise coherence without an isolation or locking protocol, unlike the explicit Rust REPEATABLE READ path.","section_id":"1.3","severity":"blocking"},{"category":"unhandled-edge","check_key":"config-sync-async-boundary","description":"A blocking read or constructor can stall LISTEN progress and PATCH, while timing out its wrapper cannot stop late synchronous work from leaking resources or extending shutdown.","finding_id":"APR2-008","fix":"Make the configuration service boundary async and route remaining blocking repository/constructor operations through the existing bounded database/work executor. Use an awaitable thread-safe handoff for local commits, quarantine and dispose late results, and test event-loop progress, worker-thread commits, and hung-constructor shutdown.","location":"P1 §1.4, P2 §§2.1/2.3, and P3 §§3.1/3.4","prevention":"For every sync/async seam, specify executor ownership, loop handoff, timeout cancellation, late completion disposal, and shutdown behavior.","principle":"Async readiness, deadlines, and shutdown guarantees require an explicit boundary around blocking storage and constructor work.","root_cause":"The plan combines an async listener with current synchronous repository and service-construction surfaces without defining off-loop execution, cross-thread signaling, or late-result quarantine.","section_id":"1.4","severity":"blocking"},{"category":"unhandled-edge","check_key":"dynamic-key-segment-codec","description":"Identifiers containing dots, percent signs, slashes, spaces, or child-field-like text can change logical paths or collide during flatten/unflatten and cross-language matching.","finding_id":"APR2-009","fix":"Define canonical UTF-8 percent-encoding for dynamic path segments, including `%` and `.`, and use it in registry matching, dotted storage, HTTP/MCP/YAML/browser paths, and Rust generation. Add collision and byte-equivalent Python/Rust round-trip tests for every dynamic family.","location":"P1 §§1.1/1.3, P2 interfaces, and P4 §4.1","prevention":"Round-trip every dynamic family through storage, HTTP/MCP, YAML, browser state, and generated Rust using adversarial delimiter-containing identifiers.","principle":"Dynamic identifiers need one lossless canonical encoding across every configuration representation.","root_cause":"Dotted primary keys and nested JSON/YAML treat dots structurally, while dynamic placeholders admit operator-controlled names and commands with no segment grammar or escaping.","section_id":"1.1","severity":"blocking"},{"category":"missing-requirement","check_key":"remote-secret-key-posture","description":"Separate daemon homes can attach to the same hub yet fail to unwrap the same DEK, breaking remote secret activation and authenticated machine output without a defined readiness failure.","finding_id":"APR2-010","fix":"Require the existing shared-passphrase KEK posture for remote-hub multi-daemon mode, verify a non-secret KEK/DEK identity fingerprint before ConfigRuntime becomes ready, and fail closed on mismatch. Extend 4.2 with distinct homes, successful remote API-key rotation, and a wrong-key worker that never becomes healthy.","location":"P1 runtime startup, P2 §2.2, and P4 §4.2","prevention":"Run remote-hub secret tests with distinct daemon homes and verify both correct-key convergence and wrong-key fail-closed readiness.","principle":"Every daemon sharing encrypted hub secrets must prove a common decrypting key posture before runtime readiness.","root_cause":"The existing secret envelope defaults to daemon-local KEK material, while multi-daemon convergence does not specify shared provisioning or mismatch behavior.","section_id":"4.2","severity":"blocking"},{"category":"missing-requirement","check_key":"bootstrap-runtime-pool-ownership","description":"Implementing the current prose either strands registered `database_concurrency.*` keys or requires reading them before the database capacity needed to load ConfigRuntime is established.","finding_id":"APR2-011","fix":"Specify two-stage startup: bootstrap `postgres_pool` supplies connection/acquisition policy and a fixed minimal pool; after the initial active snapshot, apply `database_concurrency.pool_max_size` and `executor_max_workers` from that one revision before post-database services construct. Test initial loading, coherent resize, and pending restart changes.","location":"Constraints and P3 §§3.1/3.3","prevention":"Map every bootstrap pool field and every runtime concurrency field to startup stage, owner, and activation point before consumer migration.","principle":"Bootstrap capacity must load runtime configuration, and the startup active revision must own final restart-bound capacity.","root_cause":"Section 3.3 assigns pool sizing to bootstrap even though `database_concurrency.pool_max_size` and executor capacity are registered restart-required runtime settings.","section_id":"3.3","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"APR1-012","causal_section_ids":["1.4"],"check_key":"superseded-preparation-cleanup","description":"A successful preparation for revision N that becomes stale when N+1 is requested can be discarded without closing its clients, sockets, or workers.","finding_id":"APR2-012","fix":"Route superseded preparation through bounded cleanup: cancel unfinished work, dispose every completed replacement exactly once, omit failed-live metadata for the obsolete revision, then reconcile the maximum watermark. Add an N/N+1 delayed-completion test.","introduced_in_round":1,"location":"P1 §1.4 and P3 §3.4","prevention":"Enumerate success, failure, timeout, cancellation, and superseded outcomes for every preparation and assert disposal ownership in each.","principle":"Every prepared resource that cannot publish must be disposed exactly once within bounds.","root_cause":"The monotonic repair discards stale preparation results, while cleanup is specified only for preparation failure and timeout.","section_id":"1.4","severity":"blocking"},{"category":"unhandled-edge","check_key":"active-epoch-atomicity","description":"A multi-key revision can expose a new snapshot with old services or a mixture of old and new service references even though all replacements were prepared first.","finding_id":"APR2-013","fix":"Introduce one immutable runtime-active bundle containing the active snapshot epoch and all replaceable service references. Subscribers prepare a replacement bundle and ConfigRuntime publishes one pointer; every request, policy decision, and loop captures that pointer once. Add forced thread-interleaving tests.","location":"P1 §1.4 and P3 §§3.4–3.5","prevention":"Force reader interleavings at every commit boundary and prove each operation observes one complete old or new epoch.","principle":"Configuration and constructor-captured services activated by one revision must become visible as one local epoch.","root_cause":"The plan swaps several mutable service references and publishes `ConfigSnapshot` separately, while synchronous consumers can read between those operations.","section_id":"3.4","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"APR1-001","causal_section_ids":["1.4","4.2"],"check_key":"postcommit-apply-response","description":"A local apply failure can commit desired state and revision, yet callers have no defined response. A generic error invites a retry that immediately conflicts and obscures that persistence succeeded.","finding_id":"APR2-014","fix":"Return normal committed success with the new revision plus `failed_live_keys`/apply-status metadata from HTTP, MCP, and YAML replacement when persistence succeeds but activation fails. Reserve 5xx for indeterminate persistence and add interface contract tests.","introduced_in_round":1,"location":"P1 §1.4 and P2 §§2.1/2.3/2.4","prevention":"For every mutation outcome, specify database state, active state, status code/result type, retry semantics, and client-visible metadata across all interfaces.","principle":"A mutation API must distinguish persistence outcome from local activation outcome.","root_cause":"The repaired post-commit failure state preserves the new desired revision, while HTTP, MCP, and YAML sections define only validation/conflict errors and no committed-with-apply-failure response.","section_id":"2.1","severity":"blocking"}],"reviewer_session":"85c4e853-aed5-4ca3-bbf5-00e978cf4c24","round":2,"round_number":2,"verdict":"needs_review"},"session_id":"a05c50fe-9266-4c23-8944-e17d5ef6ffed"}
```


**Round 3** `kind: verification`

- reviewer_run: 1b99055c-2500-4364-b4dd-d07316d1b8d7
- reviewer_session: 06d35258-3693-42a4-8286-05fc5bf786f4
- verdict: needs_review
- findings:
- APR3-001/blocking/§1.3 startup secrecy repair and unknown-row fail-closed had no acceptance items
- APR3-002/blocking/failed-live record retention and clearing were prose-only
- APR3-003/blocking/codec was tested at two of six promised representation boundaries
- APR3-004/blocking/explicit-standalone precedence had no positive regression test
- APR3-005/blocking/§3.4 consumer set was open-ended and §4.1 audited only authority usage
- APR3-006/blocking/§4.2 promised CAS-conflict and restart-pending scenarios it never tested
- APR3-007/nit/V2 omitted the §3.1 startup test file
- APR3-008/blocking/revision wire type and BIGINT domain were unspecified above 2^53
- APR3-009/blocking/deadlines could not stop non-returning synchronous work
- APR3-010/blocking/Rust fallback resolved secrets outside the REPEATABLE READ snapshot
- APR3-011/blocking/shared alias flip broke per-daemon generation epochs
- APR3-012/blocking/LISTEN acknowledgement was undefined without autocommit
- resolution_notes: All 12 findings accepted, two as user-approved lean variants.
  APR3-008 keeps JSON-number wire revisions and enforces a 2^53−1 ceiling with a
  typed `revision_exhausted` checked increment (1.3.8); the string-encoded wire
  contract was declined as disproportionate to an unreachable boundary. APR3-009
  lands as separated database/constructor capacity lanes with database-side
  timeouts and cancellation plus deadline abandonment (1.4.10 rewritten, 1.4.15);
  the killable-worker-process requirement was declined. Repairs: §1.3 gained
  startup secrecy-repair and unknown-row items (1.3.6/1.3.7) plus the revision
  ceiling (1.3.8); §1.4 gained the failed-live lifecycle item (1.4.14), lane
  saturation bounds (1.4.15), and autocommit LISTEN acknowledgement mirrored in
  §3.1 (3.1.5 rewritten to the activation/reload window); §1.1 defined the shared
  codec vector set, round-tripped across HTTP/MCP/YAML/browser/Rust (2.1.8,
  2.3.5, 2.4.7, 2.5.10, 2.6.5 reworded); §2.6 gained snapshot-bound secret
  capture (2.6.4 rewritten) and the explicit-standalone precedence regression
  (2.6.6); §3.4 closed its consumer set with the live-key activation matrix
  (3.4.9) enforced by the §4.1 audit; §3.6 was rewritten to generation-pinned
  physical targets with acknowledgement-gated GC (3.6.5 rewritten, 3.6.7); §4.2
  gained cross-process CAS and restart-pending scenarios (4.2.9/4.2.10); V2's P3
  group gained the §3.1 startup test file. Acceptance items went from 121 to 135;
  the ledger and registry plan_hash were regenerated to match.

```json plan-review-round
{"evidence_id":"6cb79c0b-3cb5-4f88-a164-72f47c4cb462","plan_hash":"18b5d0484c29187128f5cadb022ea82b54870182f05bfaff961346d00082e352","round_number":3,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"1074429300295fc26535c3fd486b53cfdb7f894bce712c9dc6de14814bb05d3d","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":8,"emitted_findings":12,"total":20},"evidence_id":"6cb79c0b-3cb5-4f88-a164-72f47c4cb462","lanes":[{"candidate_count":9,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":4,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":7,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":22,"manifest_digest":"6e58208b965fc9f5c49b1a97e454d46214c8784d61d45760809ddf400f769ccb","status":"valid"},"source_digest":"13f010becbf6486fa79e4b25bd1889074fbebdc53a8d96b06abb1cac1d393eca","version":1},"findings":[{"category":"weak-testability","check_key":"startup-integrity-acceptance","description":"On startup with stale `is_secret` metadata or an unknown residual ConfigStore row, an implementation may skip the repair or continue ready because none of 1.3.1–1.3.5 asserts either branch. All derived validation criteria can pass while secrecy metadata stays stale or an unregistered row remains authoritative.","finding_id":"APR3-001","fix":"Add section 1.3 acceptance items and focused tests proving registry-derived `is_secret` repair changes no effective value or revision, and proving any unknown residual row fails startup closed. Add both items to the companion ledger.","location":"P1 §1.3","prevention":"For each startup integrity branch, ledger a focused acceptance item covering trigger, state change, revision effect, and readiness outcome.","principle":"Every startup repair and fail-closed requirement must be represented in acceptance criteria and the coverage ledger.","root_cause":"Section 1.3 added startup secrecy-metadata reconciliation and unknown-row rejection only in implementation prose.","section_id":"1.3","severity":"blocking"},{"category":"weak-testability","check_key":"failed-live-record-lifecycle","description":"After revision N records a failed-live key, revision N+1 may change an unrelated key or a duplicate notification may reconcile N again. No acceptance item prevents either path from clearing the record while the daemon still serves the old active value, so public status can falsely report a healthy active configuration.","finding_id":"APR3-002","fix":"Add an acceptance item and focused test proving failed-live metadata survives unrelated revisions and duplicate/retry reconciliation, then clears only after a later operator mutation changes an affected key and activation succeeds. Add the item to the ledger.","location":"P1 §1.4","prevention":"For each durable or runtime-visible failure record, test create, unrelated update, duplicate/retry, affected-key update, and clear transitions.","principle":"Persistent failure metadata needs acceptance coverage for creation, retention, and clearing transitions.","root_cause":"The plan tests recording failed-live state but leaves its stated retention rule outside acceptance criteria.","section_id":"1.4","severity":"blocking"},{"category":"weak-testability","causal_finding_id":"APR2-009","causal_section_ids":["1.1","2.6"],"check_key":"dynamic-codec-interface-parity","description":"An identifier containing `.`, `%`, `/`, spaces, or child-field-like text can pass 1.1.4 and 2.6.5 while an HTTP/MCP/YAML/browser adapter double-encodes, decodes early, or treats the segment structurally. The plan can therefore satisfy every current acceptance item and still map one logical key to different persisted or client-visible paths.","finding_id":"APR3-003","fix":"Define shared canonical codec vectors and add acceptance coverage that round-trips them through dotted storage, HTTP, MCP, YAML import/export, browser state, and generated Rust matching. Update the affected ledger sections.","introduced_in_round":2,"location":"P1 §1.1 and P2 §§2.3–2.6","prevention":"Run one shared adversarial vector set through every encoder, decoder, flattening layer, and generated consumer whenever a path codec spans interfaces.","principle":"A canonical cross-representation codec must be tested at every representation boundary that promises to apply it.","root_cause":"The round-2 codec repair added registry round-trip and Python/Rust parity tests without pinning HTTP, MCP, YAML, browser, and dotted-storage transformations.","section_id":"1.1","severity":"blocking"},{"category":"weak-testability","check_key":"standalone-precedence-positive-regression","description":"The planned Rust layer rewrite can accidentally disable or reorder environment and `gcore.yaml` precedence in explicit standalone mode while 2.6.3 still passes, because that item checks only attached Gobby mode. This breaks the preserved fixed-standalone contract without failing the manifest criteria.","finding_id":"APR3-004","fix":"Add a Rust acceptance item and ledger entry proving that explicit standalone mode with no daemon or hub context still honors environment and `gcore.yaml` precedence, including their documented ordering.","location":"P2 §2.6","prevention":"For every mode split, include one positive acceptance test per retained mode and one negative cross-mode isolation test.","principle":"Preserved behavior needs a positive regression test when the same deliverable rewrites the controlling precedence layer.","root_cause":"Section 2.6 tests that attached Gobby mode ignores standalone sources but never tests the preserved explicit-standalone branch.","section_id":"2.6","severity":"blocking"},{"category":"traceability","check_key":"live-consumer-classification-closure","description":"A consumer can use the supported runtime snapshot once at construction for a live key, avoid every raw-authority pattern rejected by 4.1, and still never observe later revisions. Because neither a closed key-to-consumer matrix nor a lifecycle-aware audit is required, the root task’s complete consumer migration can pass with stale live consumers.","finding_id":"APR3-005","fix":"Replace the open-ended wording with a complete registry-key-to-consumer activation matrix, or add an exhaustive audit and acceptance item proving every live constructor-captured consumer has a subscriber and every restart-class consumer is explicitly startup-captured. Add the result to the ledger.","location":"P3 §3.4 and P4 §4.1","prevention":"Map every registered key to each production consumer, capture point, activation class, and subscriber or per-operation access path before cutover.","principle":"A runtime-consumer migration must enumerate a closed consumer set or provide an exhaustive lifecycle-aware audit.","root_cause":"Section 3.4 delegates an open-ended set to “other constructor-captured live settings,” while section 4.1 audits authority usage rather than activation-lifecycle correctness.","section_id":"3.4","severity":"blocking"},{"category":"weak-testability","check_key":"integration-scenario-acceptance-parity","description":"Cross-process CAS arbitration and restart-required desired/active separation may fail even while the unit tests in 1.3 and 3.3 pass. The dedicated two-daemon leaf says it verifies both, yet its derived validation criteria never run either scenario, so expansion can close the integration leaf without proving them.","finding_id":"APR3-006","fix":"Add explicit two-daemon acceptance items and tests for a concurrent stale-revision CAS conflict and for restart-required pending desired/active state across both daemons. Add both rows to the ledger.","location":"P4 §4.2","prevention":"Diff each integration scenario sentence against acceptance IDs and ledger rows before manifest derivation.","principle":"Every scenario promised by an integration deliverable must map to an explicit acceptance item.","root_cause":"The two-daemon scenario inventory names CAS conflict and restart-pending state, but 4.2.1–4.2.8 and the ledger omit both.","section_id":"4.2","severity":"blocking"},{"category":"weak-testability","check_key":"focused-validation-acceptance-closure","description":"`tests/runner_init/test_config_runtime_startup.py` carries all five section 3.1 acceptance items, yet no V2 command runs it. Leaf-level criteria still name the tests, so this is aggregate verification drift rather than missing behavioral coverage.","finding_id":"APR3-007","fix":"Add `tests/runner_init/test_config_runtime_startup.py` to the focused P3 pytest command.","location":"V2 Verification / P3 §3.1","prevention":"Generate or compare focused validation file lists against all `test:` artifact references in the ledger.","principle":"A focused aggregate validation command should execute every focused test file named by the plan’s acceptance criteria.","root_cause":"V2’s P3 pytest group omits the section 3.1 startup test file.","section_id":"3.1","severity":"nit"},{"category":"unhandled-edge","check_key":"revision-wire-integer-domain","description":"At revisions 9007199254740992 and 9007199254740993, JavaScript `Number` can collapse adjacent values. The browser may ignore a genuinely newer `config_event`, send a rounded `expected_revision`, and loop on CAS conflicts; at BIGINT_MAX the next mutation has no defined outcome.","finding_id":"APR3-008","fix":"Define revisions as canonical decimal strings on every public wire surface, parse them to bounded int64/bigint internally, reject non-canonical or out-of-range values, and use checked increment with a distinct `revision_exhausted` result. Add tests at 2^53−1, 2^53, adjacent values above 2^53, and BIGINT_MAX.","location":"P1 §§1.2–1.3 and P2 §§2.1–2.5","prevention":"For every persisted integer crossing JavaScript, test exact adjacent values at the safe-integer boundary and the storage maximum.","principle":"A cross-language revision token must round-trip exactly over its full persisted domain and define exhaustion.","root_cause":"PostgreSQL stores revisions as BIGINT while HTTP, MCP, WebSocket, YAML results, and TypeScript state leave the wire type and upper bound unspecified.","section_id":"2.5","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"APR2-008","causal_section_ids":["1.4"],"check_key":"sync-deadline-underlying-work","description":"A repository call or subscriber constructor can exceed its deadline and keep a worker occupied indefinitely. Repeating that branch can consume all bounded workers; disposal never runs for truly non-returning work, and LISTEN reloads, PATCH reconciliation, or shutdown can then exceed the guarantees asserted by 1.4.10 and 3.4.7.","finding_id":"APR3-009","fix":"Separate database and constructor/drain capacity with bounded admission. Require database statement/lock/connect timeouts plus connection cancellation, and run any constructor covered by a hard shutdown guarantee in a killable worker process with explicit result ownership. Add max-worker saturation tests proving LISTEN, PATCH, and shutdown retain their bounds.","introduced_in_round":2,"location":"P1 §1.4 and P3 §§3.3–3.4","prevention":"For every off-loop operation, specify admission bound, underlying cancellation mechanism, capacity isolation, late ownership, and shutdown behavior.","principle":"An async deadline bounds an await only when the underlying synchronous work has an enforceable termination and capacity policy.","root_cause":"The round-2 executor repair relies on timeout plus late-result disposal, which handles eventual completion but cannot stop non-returning synchronous work.","section_id":"1.4","severity":"blocking"},{"category":"unhandled-edge","check_key":"rust-secret-snapshot-binding","description":"Rust fallback can capture revision N and a stable secret reference, close or leave the snapshot boundary, then resolve plaintext after a same-reference rotation commits N+1. It returns N+1 secret material in a result claiming revision N, recreating the torn secret-binding path that sections 1.4 and 2.2 explicitly prevent for Python machine output.","finding_id":"APR3-010","fix":"Keep the REPEATABLE READ transaction alive through secret lookup and decryption, or load every referenced ciphertext and key-envelope binding into the immutable fallback map before commit. Extend 2.6.4 with a paused-reader same-reference rotation race.","location":"P2 §2.6","prevention":"Pause between reference capture and secret lookup, rotate the same reference concurrently, and require a wholly old or wholly new result.","principle":"A revision-coherent snapshot containing secret references must capture the referenced payload and envelope binding in the same database snapshot.","root_cause":"Section 2.6 explicitly captures revision and machine-visible config rows in REPEATABLE READ, then separately says secret references resolve through the hub secret store.","section_id":"2.6","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"APR1-014","causal_section_ids":["3.6","4.2"],"check_key":"managed-alias-epoch-isolation","description":"When daemon A flips a shared alias, daemon B can still hold the old model/dimension bundle while the same alias already selects the new physical collection. A crash before the config commit yields no revision event, and later GC can delete the old generation while a disconnected or failed daemon still relies on its promised last-good epoch. Local pointer swaps cannot make that shared alias transition atomic.","finding_id":"APR3-011","fix":"Persist generation-specific physical collection targets in the completed record and each runtime-active bundle, and make requests use the captured physical target rather than a mutable alias. Retain old generations until durable per-daemon revision acknowledgements and bounded in-flight drains prove they are unused; test every flip/commit/reconcile/GC boundary.","introduced_in_round":1,"location":"P3 §3.6 and P4 §4.2","prevention":"Walk pre-flip, partial-flip, post-flip/pre-commit, post-commit/pre-reconcile, failed-daemon, disconnect, drain, and GC boundaries for every shared selector.","principle":"Per-daemon last-good epochs require every shared resource selector to remain generation-stable until all consumers leave the old epoch.","root_cause":"The round-1 multi-daemon repair coordinates local active bundles through a globally mutable embedding alias and later GC.","section_id":"3.6","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"APR1-011","causal_section_ids":["1.4","3.1"],"check_key":"listen-subscription-activation","description":"PostgreSQL does not activate `LISTEN` issued inside a transaction until commit. If command completion is treated as acknowledgement, the runtime can reload and report ready while the subscription is still pending; a revision committed after that reload and before listener activation is lost. Acceptance 3.1.5 also describes the opposite read/ack ordering from the implementation prose, so it does not pin this window.","finding_id":"APR3-012","fix":"Require the dedicated listener connection to use autocommit for role setup and `LISTEN`, and define acknowledgement as confirmed subscription activation. Perform the coherent reload only afterward, then become ready. Rewrite 3.1.5 and add a failpoint test at the LISTEN-activation/reload boundary.","introduced_in_round":1,"location":"P1 §1.4 and P3 §3.1","prevention":"Test the LISTEN transaction commit, reload, and readiness boundaries with a writer paused at each interval.","principle":"A LISTEN startup barrier must acknowledge transactionally active subscription state before the catch-up reload begins.","root_cause":"The round-1 catch-up repair treats LISTEN command completion as acknowledgement without requiring autocommit or an explicit commit.","section_id":"3.1","severity":"blocking"}],"reviewer_session":"06d35258-3693-42a4-8286-05fc5bf786f4","round":3,"round_number":3,"verdict":"needs_review"},"session_id":"a05c50fe-9266-4c23-8944-e17d5ef6ffed"}
```


**Round 4** `kind: verification`

- reviewer_run: 2199ee2c-1371-48b0-8a6f-1dcbc40ff728
- reviewer_session: 835c10fd-d320-4405-97ec-ab99256efc03
- verdict: needs_review
- findings:
- APR4-001/blocking/liveness-window GC quorum excluded daemons without a serving lease or self-fence
- APR4-002/blocking/build enumeration raced ordinary writers with no watermark, tombstones, or replay
- APR4-003/blocking/codec pinned round-trips but left exact canonical bytes, multibyte vectors, and rejection open
- APR4-004/blocking/subscriber failure policy defined replacement only, never first initialization
- APR4-005/blocking/revision domain and typed exhaustion stopped at the storage path
- resolution_notes: All five findings accepted. §1.2 gained the
  embedding_generation_acks lease/acknowledgement table and the
  embedding_projection_changes tombstoned change sequence with runtime-role
  grants (1.2.7); §3.6 was rewritten to renewable serving leases with local
  self-fence before expiry, lease-gated GC (3.6.7 rewritten), and
  watermark-based write catch-up through a PostgreSQL-backed generation
  transition (3.6.8), with producer and vector-wrapper targets added and
  two-daemon partition and write-race scenarios in §4.2 (4.2.11/4.2.12);
  §1.1 pinned the codec's exact unescaped alphabet, uppercase hex, UTF-8
  bytes, and malformed-input rejection, and extended the vector set with
  multibyte and non-canonical entries asserted byte-exactly across all six
  surfaces (1.1.4, 2.1.8, 2.3.5, 2.4.7, 2.5.10, 2.6.5 reworded); §1.4
  gained the first-initialization contract — required blocks readiness,
  optional publishes an unavailable slot, ConfigRuntime owns and clears
  degraded state — mirrored in §3.1 and §3.4 (1.4.16, 3.1.6, 3.4.10);
  §§2.1/2.3/2.4/2.5 closed the revision domain with strict
  [0, 2^53−1] validation and a typed non-retryable revision_exhausted
  result branched away from conflict handling in the browser (2.1.9, 2.3.6,
  2.4.8, 2.5.11). Acceptance items went from 135 to 146; the ledger and
  registry plan_hash were regenerated to match.

```json plan-review-round
{"evidence_id":"305fd19b-6184-4799-a4e7-004039d9d6aa","plan_hash":"cef6053565fab08af6d0dec268aed27d5dea4fade1f6da268649d0cd5217a399","round_number":4,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"78866b2c7b78743f0c3fd3626a547db74890e30a46969d5cf0b22e1538617fdc","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":3,"emitted_findings":5,"total":8},"evidence_id":"305fd19b-6184-4799-a4e7-004039d9d6aa","lanes":[{"candidate_count":0,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":3,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":5,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":22,"manifest_digest":"a14e119fb5b36101c47958f51414393669e92b3394d3047a2e022ec5ead31f25","status":"valid"},"source_digest":"b24065dd764bea5db78439d75ea5ef32491339593819a11e4419f573e069496b","version":1},"findings":[{"category":"unhandled-edge","causal_finding_id":"APR3-011","causal_section_ids":["3.6"],"check_key":"managed-generation-lease-fencing","description":"A daemon can lose PostgreSQL and notification connectivity while retaining HTTP and Qdrant connectivity, keep serving its captured old physical target past the liveness window, be excluded from the quorum, and then access a collection another daemon deletes. The current Targets also omit durable acknowledgement/lease storage, runtime-role grants, and readiness/health serving gates.","finding_id":"APR4-001","fix":"Fold durable embedding-generation acknowledgements and DB-authoritative renewable leases into baseline 375, keyed by stable daemon-instance identity and carrying generation, revision, acknowledgement, and expiry. Grant least-privilege runtime access; renew while serving; self-fence embedding requests before lease expiry when renewal cannot be confirmed; let GC delete only after every unexpired lease acknowledges the generation and drains complete. Add ConfigRuntime, readiness, health, schema/catalog/identity Targets and a two-daemon PostgreSQL-loss test that leaves HTTP/Qdrant live, proves self-fencing, then proves reconnect and reconciliation.","introduced_in_round":3,"location":"P1 §1.2, P3 §3.6, and P4 §4.2","prevention":"For each quorum exclusion rule, test the member while its control-plane connection is down and its data-plane serving path remains live.","principle":"A daemon may be excluded from generation GC only after it is also unable to serve the excluded generation.","root_cause":"The round-3 liveness-window repair ages acknowledgements out of the GC quorum without a renewable serving lease or local self-fence.","section_id":"3.6","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"APR3-011","causal_section_ids":["3.6"],"check_key":"managed-generation-write-catchup","description":"The existing switch pages sources into staging while ProjectWriteFence grants ordinary writer admission and only coordinates one process. A mutation after enumeration can land only in the old generation; the plan specifies no durable watermark, tombstone, delta replay, dual-generation catch-up, or cross-daemon barrier. Every daemon can acknowledge the new generation and GC the old one while committed updates are absent from the promoted collections.","finding_id":"APR4-002","fix":"Add a transactionally durable embedding-projection change sequence with tombstones for memory, tool, and issue producers in baseline 375. Capture a build watermark, replay later mutations into staging through a PostgreSQL-backed generation transition, bind the caught-up watermark and physical targets to the completed record/config revision, and retain replay through live-daemon promotion. Target every memory/tool/issue writer plus the fence/vector wrappers, and add two-daemon create/update/delete races at every switch boundary.","introduced_in_round":3,"location":"P1 §1.2, P3 §3.6, and P4 §4.2","prevention":"Pause every producer after enumeration and at build, flip, config-commit, local-promotion, and remote-promotion boundaries; prove the promoted generation contains each raced create, update, and tombstone.","principle":"A promoted derived-data generation must include every authoritative source mutation committed before promotion.","root_cause":"Generation-pinned selectors and acknowledgement-gated GC protect readers, while build enumeration still races ordinary in-process and cross-daemon create, update, and delete writers.","section_id":"3.6","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"APR3-003","causal_section_ids":["1.1","2.1","2.3","2.4","2.5","2.6"],"check_key":"dynamic-codec-canonical-bytes","description":"Python, Rust, and TypeScript may choose different safe characters or percent-hex case and still pass their local round trips; the listed vectors also omit multibyte Unicode and malformed/non-canonical input. A path written by one surface can therefore have different primary-key bytes or fail matching on another while all current acceptance items pass.","finding_id":"APR4-003","fix":"Specify the exact unescaped alphabet, uppercase percent-hex form, UTF-8 byte encoding, and malformed/non-canonical decode rejection. Define shared logical-segment-to-encoded-byte fixtures covering pre-encoded percent text, accented and CJK text, emoji, and distinct composed/decomposed sequences. Make dotted storage, HTTP, MCP, YAML, browser, and Rust acceptance tests assert those exact bytes as well as round-trip and collision freedom.","introduced_in_round":3,"location":"P1 §1.1 and P2 §§2.1, 2.3, 2.4, 2.5, and 2.6","prevention":"Pin logical input, canonical encoded bytes, decoded output or rejection, and collision behavior at every storage and interface boundary.","principle":"A canonical cross-language storage codec requires one exact encoded byte sequence and one rejection policy for every accepted logical segment.","root_cause":"The round-3 repair added shared self-round-trip vectors while retaining the open-ended phrase “escapes at least” and omitting exact encoded bytes and UTF-8 multibyte cases.","section_id":"1.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"initial-subscriber-no-last-good","description":"On fresh startup, ConfigRuntime loads before post-database services and a subscriber constructor can fail with no prior service or active secret binding to preserve. The plan does not decide required versus optional service behavior, partial-constructor cleanup, first active-bundle publication, or ownership and clearing of the existing degraded-health marker. A daemon can report active desired values with a missing service or remain degraded after a later successful activation.","finding_id":"APR4-004","fix":"Define initial registration as a startup preparation transaction. Required-service failure disposes every partial replacement and blocks readiness before publishing the first bundle; optional-capability failure publishes an explicit unavailable slot and recoverable degraded state. Make ConfigRuntime own and clear that state after successful affected-key activation, target readiness and health consumers, and add fresh-start partial-failure plus later-recovery tests.","location":"P1 §1.4 and P3 §§3.1 and 3.4","prevention":"Test first construction, live replacement, restart, and reconnect as distinct subscriber lifecycle states, including cleanup, active projection, and health recovery.","principle":"A failure policy that preserves previous active state must separately define first initialization when no previous resource exists.","root_cause":"Preparation, timeout, and secret-binding failures are specified only for replacement of an existing active bundle; current startup constructors can instead leave service references absent and health state degraded.","section_id":"3.4","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"APR3-008","causal_section_ids":["1.3"],"check_key":"revision-domain-interface-closure","description":"HTTP, MCP, YAML, and browser models do not explicitly reject negative, fractional, or greater-than-2^53−1 revisions, and they do not map checked `revision_exhausted` to a typed terminal result. Section 2.1 reserves 5xx for indeterminate persistence, while the browser’s stale-revision branch refetches for resubmission; either generic path misclassifies a determinate permanent failure.","finding_id":"APR4-005","fix":"Keep JSON-number revisions and require strict integers in [0, 2^53−1] on every request, response, event, YAML result, and browser model. Define a distinct non-retryable `revision_exhausted` public error/result across HTTP, MCP, and YAML, branch it away from stale-revision refetch/resubmit in the browser, and add boundary tests at each interface.","introduced_in_round":3,"location":"P1 §1.3 and P2 §§2.1, 2.3, 2.4, and 2.5","prevention":"For every numeric token crossing a wire, test negative, fractional, maximum, above-maximum, exhaustion, and retry classification on every producer and consumer.","principle":"A bounded cross-language revision token needs strict input validation and one typed, non-retryable exhaustion outcome at every mutation interface.","root_cause":"The round-3 ceiling repair stops at the storage mutation path and acceptance 1.3.8; public and browser sections retain generic revision fields and stale-conflict behavior.","section_id":"2.1","severity":"blocking"}],"reviewer_session":"835c10fd-d320-4405-97ec-ab99256efc03","round":4,"round_number":4,"verdict":"needs_review"},"session_id":"a05c50fe-9266-4c23-8944-e17d5ef6ffed"}
```

**Human handoff** `kind: verification`

- The configured adversarial review cap of 4 rounds is reached with a
  needs_review verdict. All five round-4 findings were accepted and their
  repairs folded in above. Per the review-cap contract no further adversary
  rounds launch; the plan proceeds only through explicit human decision —
  continue interactively, hand off to build, or stop. Approval of the folded
  round-4 repairs rests with the human owner.


## V2: Verification
`kind: verification`

Before enhancement or adversarial review:

```bash
uv run gobby plans validate .gobby/plans/reactive-config-store.md
```

The companion ledger must exist, contain the final plan hash, enumerate every acceptance
item, and pass adversarial comparison against the Markdown before expansion.

Focused implementation validation:

```bash
GOBBY_TEST_PROTECT=1 uv run pytest \
  tests/config/test_config_registry.py \
  tests/storage/test_revisioned_config_store.py \
  tests/config/test_config_runtime.py \
  tests/config/test_runtime_config_contract.py -v

GOBBY_TEST_PROTECT=1 uv run pytest \
  tests/servers/routes/test_config_values_api.py \
  tests/servers/routes/test_configuration_effective_routes.py \
  tests/servers/routes/test_config_yaml_replace.py \
  tests/mcp_proxy/tools/test_config_values.py -v

GOBBY_TEST_PROTECT=1 uv run pytest \
  tests/config/test_live_policy_consumers.py \
  tests/config/test_restart_config_consumers.py \
  tests/config/test_stateful_config_subscribers.py \
  tests/config/test_runtime_loop_consumers.py \
  tests/runner_init/test_config_runtime_startup.py \
  tests/config/test_config_authority_audit.py -v

GOBBY_TEST_PROTECT=1 uv run pytest \
  tests/cli/test_cli_runtime_config.py \
  tests/cli/test_bootstrap_config_consumers.py \
  tests/cli/test_operational_config_consumers.py \
  tests/mcp_proxy/test_stdio_config_runtime.py -v

GOBBY_TEST_PROTECT=1 uv run pytest \
  tests/storage/test_embedding_switch_config_contract.py \
  tests/ai/test_embedding_switch_daemon_lifecycle.py \
  tests/integration/config/test_reactive_config_multi_daemon.py -v
```

Rust and embedded-schema validation. The regeneration order is deterministic because the
identity generator defaults to the *installed* `~/.gobby/bin/gdaemon` and the catalog
freshness test writes only under `UPDATE_GCORE_SCHEMA_MANIFEST`: update
`BASELINE_CHECKSUM`, regenerate the catalog against an isolated PostgreSQL database with
`UPDATE_GCORE_SCHEMA_MANIFEST=1`, build release `gdaemon`, generate the identity from
the freshly built binary via `--gdaemon target/release/gdaemon`, then rerun catalog
freshness without update mode and both identity contracts.

```bash
UPDATE_GCORE_SCHEMA_MANIFEST=1 cargo test -p gobby-core --test catalog_manifest_freshness
cargo build --release -p gobby-daemon -p gobby-code
uv run python scripts/generate_schema_expected_identity.py --gdaemon target/release/gdaemon
uv run python scripts/generate_runtime_config_contract.py --check
cargo test -p gobby-core schema::runner_tests
cargo test -p gobby-core config::tests::runtime_contract
cargo test -p gobby-core ai::effective_config
cargo test -p gobby-core --test schema_contract
cargo test -p gobby-core --test catalog_manifest_freshness
cargo test -p gobby-code config::
cargo test -p gobby-daemon --test cli_contract
```

Run focused Ruff, mypy, web Vitest, and web type-check commands for touched paths,
including the section 2.5 browser-authority audit
(`web/src/__tests__/config-authority-audit.test.ts`). Smoke the newly built `gdaemon`
against an isolated temporary schema and confirm the fresh apply, the
predecessor-receipt re-apply, and the corrupt-receipt rejection paths; do not reinstall
or restart the user's daemon during automated validation.

## M1 Task Manifest
`kind: manifest`

```yaml
- title: Compile the typed registry
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: '1.1.1: Every non-bootstrap daemon leaf resolves to exactly
    one spec. test: `tests/config/test_config_registry.py::test_every_daemon_leaf_has_one_spec`.

    1.1.2: Every mapping leaf has an explicit non-overlapping pattern adapter. test:
    `tests/config/test_config_registry.py::test_mapping_patterns_are_complete`.

    1.1.3: Public and machine schemas expose only their declared visibility classes.
    test: `tests/config/test_config_registry.py::test_visibility_partitions_are_disjoint`.

    1.1.4: The shared codec vector set produces its exact canonical encoded bytes,
    round-trips losslessly through every dynamic family and dotted storage without
    collisions, and every malformed or non-canonical input is rejected. test: `tests/config/test_config_registry.py::test_dynamic_segment_codec_round_trip`.'
  labels:
  - covers:reactive-config-store:1.1:1.1.1
  - covers:reactive-config-store:1.1:1.1.2
  - covers:reactive-config-store:1.1:1.1.3
  - covers:reactive-config-store:1.1:1.1.4
  tdd: true
  source_section: '1.1'
  implementation_domain: backend
- title: Extend baseline 375 with revisioned configuration state
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  validation_criteria: '1.2.1: A fresh apply creates the revision table, seed row,
    and config_store column. test: `crates/gcore/src/schema/runner_tests.rs::fresh_baseline_creates_config_revision_state`.

    1.2.2: A hub holding the exact predecessor baseline@375 receipt re-applies, replaces
    its receipt with the new checksum in one transaction, and loses no data. test:
    `crates/gcore/src/schema/runner_tests.rs::existing_hub_reapplies_updated_baseline`.

    1.2.3: Re-apply is idempotent and requires no destructive authorization. test:
    `crates/gcore/src/schema/runner_tests.rs::config_revision_baseline_is_nondestructive`.

    1.2.4: Embedded assets and catalog describe the revision table and row column.
    file: `crates/gcore/assets/schema/catalog.manifest.json`.

    1.2.5: Regenerated schema identity matches the edited baseline and both identity
    contract tests pass. test: `crates/gdaemon/tests/cli_contract.rs::version_json_reports_exact_schema_identity_contract`.

    1.2.6: Arbitrary checksum or filename receipt mismatches still classify CorruptPartial
    and refuse. test: `crates/gcore/src/schema/runner_tests.rs::unrecognized_receipt_still_rejects`.

    1.2.7: A fresh apply creates the embedding acknowledgement/lease and projection-change
    tables with runtime-role grants. test: `crates/gcore/src/schema/runner_tests.rs::fresh_baseline_creates_embedding_coordination_state`.'
  labels:
  - covers:reactive-config-store:1.2:1.2.1
  - covers:reactive-config-store:1.2:1.2.2
  - covers:reactive-config-store:1.2:1.2.3
  - covers:reactive-config-store:1.2:1.2.4
  - covers:reactive-config-store:1.2:1.2.5
  - covers:reactive-config-store:1.2:1.2.6
  - covers:reactive-config-store:1.2:1.2.7
  tdd: true
  source_section: '1.2'
  implementation_domain: backend
- title: Implement atomic revisioned mutations
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  - '1.2'
  validation_criteria: "1.3.1: Concurrent writers sharing an expected revision yield\
    \ one commit and one typed conflict. test: `tests/storage/test_revisioned_config_store.py::test_compare_and_swap_serializes_writers`.\n\
    1.3.2: Values, unsets, secret payloads, row revisions, global revision, and notification\
    \ commit atomically. test: `tests/storage/test_revisioned_config_store.py::test_mutation_is_one_transaction`.\n\
    1.3.3: Invalid candidates leave configuration, secrets, revision, and notifications\
    \ untouched. test: `tests/storage/test_revisioned_config_store.py::test_invalid_candidate_has_no_side_effects`.\n\
    1.3.4: No-op and secret-rotation behavior follows the effective-change rule. test:\
    \ `tests/storage/test_revisioned_config_store.py::test_effective_change_controls_revision`.\n\
    1.3.5: A paused reader with a concurrent committed writer returns a wholly old\
    \ or wholly new snapshot, never a mix. test: `tests/storage/test_revisioned_config_store.py::test_snapshot_read_is_repeatable_read_coherent`.\n\
    1.3.6: Startup repairs stale registry-derived `is_secret` metadata without changing\
    \ any effective value or the revision. test: `tests/storage/test_revisioned_config_store.py::test_startup_secrecy_repair_preserves_values_and_revision`.\n\
    1.3.7: Startup with an unknown residual ConfigStore row fails closed. test: `tests/storage/test_revisioned_config_store.py::test_unknown_residual_row_fails_closed`.\n\
    1.3.8: An increment at the 2^53\u22121 ceiling returns typed `revision_exhausted`\
    \ and commits nothing. test: `tests/storage/test_revisioned_config_store.py::test_revision_ceiling_returns_exhausted`."
  labels:
  - covers:reactive-config-store:1.3:1.3.1
  - covers:reactive-config-store:1.3:1.3.2
  - covers:reactive-config-store:1.3:1.3.3
  - covers:reactive-config-store:1.3:1.3.4
  - covers:reactive-config-store:1.3:1.3.5
  - covers:reactive-config-store:1.3:1.3.6
  - covers:reactive-config-store:1.3:1.3.7
  - covers:reactive-config-store:1.3:1.3.8
  tdd: true
  source_section: '1.3'
  implementation_domain: backend
- title: Add ConfigRuntime and remote-daemon notifications
  category: code
  task_type: feature
  depends_on:
  - '1.3'
  validation_criteria: '1.4.1: Readers observe immutable single-revision snapshots.
    test: `tests/config/test_config_runtime.py::test_snapshot_swap_is_atomic`.

    1.4.2: Restart writes separate desired and active state and report pending keys.
    test: `tests/config/test_config_runtime.py::test_restart_policy_tracks_pending_keys`.

    1.4.3: Post-commit preparation failure keeps desired state and revision committed,
    performs no active swap and no compensating write, and records failed-live metadata.
    test: `tests/config/test_config_runtime.py::test_apply_failure_preserves_local_last_good_state`.

    1.4.4: A second runtime receives remote revisions over the pool-exempt listener.
    test: `tests/config/test_config_runtime.py::test_remote_runtime_receives_revision_notification`.

    1.4.5: Listener reconnect performs a full reload before health recovery. test:
    `tests/config/test_config_runtime.py::test_listener_reconnect_reloads_snapshot`.

    1.4.6: Duplicate, current, and burst notifications reconcile at most once to the
    latest revision. test: `tests/config/test_config_runtime.py::test_notifications_are_idempotent_and_coalesced`.

    1.4.7: The pool-exempt listener runs under the daemon runtime role in autocommit
    mode and receives notifications, reconnects, and closes under it. test: `tests/config/test_config_runtime.py::test_listener_assumes_runtime_role`.

    1.4.8: A delayed older reload completing after a newer one is discarded and never
    published. test: `tests/config/test_config_runtime.py::test_out_of_order_reload_is_discarded`.

    1.4.9: Failed preparation after a same-reference secret rotation preserves the
    previous active secret binding and consumers never observe the unactivated payload.
    test: `tests/config/test_config_runtime.py::test_failed_apply_preserves_active_secret_binding`.

    1.4.10: Blocking repository or constructor work runs off-loop in its bounded lane,
    database work terminates through database-side timeouts and cancellation, and
    a late result arriving after its deadline is quarantined and disposed without
    stalling LISTEN or shutdown. test: `tests/config/test_config_runtime.py::test_blocking_work_is_bounded_and_quarantined`.

    1.4.11: A successful preparation superseded by a newer revision is disposed exactly
    once and records no failed-live metadata. test: `tests/config/test_config_runtime.py::test_superseded_preparation_is_disposed`.

    1.4.12: Snapshot and service references publish as one bundle pointer and no forced
    interleaving observes a mixed epoch. test: `tests/config/test_config_runtime.py::test_active_bundle_swap_is_atomic`.

    1.4.13: Remote-hub startup verifies the KEK/DEK identity fingerprint and a mismatched
    daemon fails closed before ready. test: `tests/config/test_config_runtime.py::test_kek_mismatch_fails_closed`.

    1.4.14: Failed-live metadata survives unrelated revisions and duplicate reconciliation,
    and clears only when a later mutation changes an affected key and its activation
    succeeds. test: `tests/config/test_config_runtime.py::test_failed_live_record_lifecycle`.

    1.4.15: Constructor-lane saturation by non-returning work leaves LISTEN reconciliation,
    PATCH completion, and shutdown within their bounds. test: `tests/config/test_config_runtime.py::test_lane_saturation_preserves_bounds`.

    1.4.16: Fresh-startup required-service preparation failure disposes partial replacements
    and blocks readiness; an optional-capability failure publishes an unavailable
    slot, and a later successful affected-key activation clears the degraded state.
    test: `tests/config/test_config_runtime.py::test_first_initialization_failure_semantics`.'
  labels:
  - covers:reactive-config-store:1.4:1.4.1
  - covers:reactive-config-store:1.4:1.4.2
  - covers:reactive-config-store:1.4:1.4.3
  - covers:reactive-config-store:1.4:1.4.4
  - covers:reactive-config-store:1.4:1.4.5
  - covers:reactive-config-store:1.4:1.4.6
  - covers:reactive-config-store:1.4:1.4.7
  - covers:reactive-config-store:1.4:1.4.8
  - covers:reactive-config-store:1.4:1.4.9
  - covers:reactive-config-store:1.4:1.4.10
  - covers:reactive-config-store:1.4:1.4.11
  - covers:reactive-config-store:1.4:1.4.12
  - covers:reactive-config-store:1.4:1.4.13
  - covers:reactive-config-store:1.4:1.4.14
  - covers:reactive-config-store:1.4:1.4.15
  - covers:reactive-config-store:1.4:1.4.16
  tdd: true
  source_section: '1.4'
  implementation_domain: backend
- title: Replace the public HTTP configuration API
  category: code
  task_type: feature
  depends_on:
  - '1.4'
  validation_criteria: '2.1.1: Schema and values expose public registry metadata and
    masked desired/active state. test: `tests/servers/routes/test_config_values_api.py::test_public_schema_and_values_contract`.

    2.1.2: PATCH enforces CAS, path validation, per-key unset, and managed activation.
    test: `tests/servers/routes/test_config_values_api.py::test_public_patch_contract`.

    2.1.3: Public reads, errors, and events contain no secret plaintext. test: `tests/servers/routes/test_config_values_api.py::test_public_surfaces_redact_secrets`.

    2.1.4: Reset and caller-supplied `is_secret` are absent. test: `tests/servers/routes/test_config_values_api.py::test_legacy_reset_and_secrecy_flags_are_removed`.

    2.1.5: A newly reconciled revision emits exactly one revision-only `config_event`
    and duplicate revisions emit none. test: `tests/servers/routes/test_config_values_api.py::test_config_revision_event_contract`.

    2.1.6: A committed mutation whose local activation fails returns success with
    the new revision and failed-live metadata, never a retryable generic error. test:
    `tests/servers/routes/test_config_values_api.py::test_apply_failure_returns_committed_metadata`.

    2.1.7: Generation-endpoint activation commits one revisioned typed mutation including
    its secret and performs no raw writes. test: `tests/servers/routes/test_config_values_api.py::test_endpoint_activation_uses_typed_mutation`.

    2.1.8: HTTP paths round-trip the shared codec vector set with exact canonical
    bytes, without early decoding or structural splitting. test: `tests/servers/routes/test_config_values_api.py::test_http_round_trips_codec_vectors`.

    2.1.9: Revision inputs outside the strict integer domain are rejected and a ceiling-refused
    mutation returns the typed non-retryable `revision_exhausted` error. test: `tests/servers/routes/test_config_values_api.py::test_revision_domain_and_exhaustion_contract`.'
  labels:
  - covers:reactive-config-store:2.1:2.1.1
  - covers:reactive-config-store:2.1:2.1.2
  - covers:reactive-config-store:2.1:2.1.3
  - covers:reactive-config-store:2.1:2.1.4
  - covers:reactive-config-store:2.1:2.1.5
  - covers:reactive-config-store:2.1:2.1.6
  - covers:reactive-config-store:2.1:2.1.7
  - covers:reactive-config-store:2.1:2.1.8
  - covers:reactive-config-store:2.1:2.1.9
  tdd: true
  source_section: '2.1'
  implementation_domain: backend
- title: Preserve the authenticated Rust machine contract
  category: code
  task_type: feature
  depends_on:
  - '1.4'
  validation_criteria: '2.2.1: Effective config retains its flat envelope and resolves
    machine-visible secret references. test: `tests/servers/routes/test_configuration_effective_routes.py::test_effective_config_preserves_resolved_machine_contract`.

    2.2.2: Public-only and restricted-only keys are excluded from machine output.
    test: `tests/servers/routes/test_configuration_effective_routes.py::test_effective_config_uses_machine_visibility`.

    2.2.3: Effective config requires the runtime token and disables caching. test:
    `tests/servers/routes/test_configuration_effective_routes.py::test_effective_config_auth_and_cache_contract`.

    2.2.4: Service capabilities retain agent authorization and active-snapshot behavior.
    test: `tests/servers/routes/test_configuration_effective_routes.py::test_service_capabilities_use_active_snapshot`.

    2.2.5: Machine output serves the activated secret payload and never an unactivated
    rotated payload. test: `tests/servers/routes/test_configuration_effective_routes.py::test_machine_output_uses_active_secret_binding`.'
  labels:
  - covers:reactive-config-store:2.2:2.2.1
  - covers:reactive-config-store:2.2:2.2.2
  - covers:reactive-config-store:2.2:2.2.3
  - covers:reactive-config-store:2.2:2.2.4
  - covers:reactive-config-store:2.2:2.2.5
  tdd: true
  source_section: '2.2'
  implementation_domain: backend
- title: Replace MCP configuration tools
  category: code
  task_type: feature
  depends_on:
  - '1.4'
  - '2.1'
  validation_criteria: '2.3.1: MCP and HTTP return equivalent schema, values, and
    patch results. test: `tests/mcp_proxy/tools/test_config_values.py::test_mcp_wraps_universal_config_service`.

    2.3.2: MCP patch requires revision and preserves secret/managed policies. test:
    `tests/mcp_proxy/tools/test_config_values.py::test_mcp_patch_requires_revision`.

    2.3.3: Raw get/set/delete/batch/list/default-seeding tools are absent. test: `tests/mcp_proxy/tools/test_config_values.py::test_legacy_config_tools_are_removed`.

    2.3.4: An MCP patch whose local activation fails reports committed success with
    failed-live metadata. test: `tests/mcp_proxy/tools/test_config_values.py::test_mcp_patch_reports_apply_status`.

    2.3.5: MCP tools round-trip the shared codec vector set byte-equivalently to HTTP.
    test: `tests/mcp_proxy/tools/test_config_values.py::test_mcp_round_trips_codec_vectors`.

    2.3.6: An MCP patch rejects out-of-domain revisions and maps the ceiling to the
    typed non-retryable `revision_exhausted` result. test: `tests/mcp_proxy/tools/test_config_values.py::test_mcp_revision_domain_and_exhaustion`.'
  labels:
  - covers:reactive-config-store:2.3:2.3.1
  - covers:reactive-config-store:2.3:2.3.2
  - covers:reactive-config-store:2.3:2.3.3
  - covers:reactive-config-store:2.3:2.3.4
  - covers:reactive-config-store:2.3:2.3.5
  - covers:reactive-config-store:2.3:2.3.6
  tdd: true
  source_section: '2.3'
  implementation_domain: backend
- title: Make YAML a validate-first daemon-namespace replacement
  category: code
  task_type: feature
  depends_on:
  - '1.4'
  - '2.1'
  validation_criteria: '2.4.1: Invalid documents preserve rows, secrets, and revision.
    test: `tests/servers/routes/test_config_yaml_replace.py::test_invalid_document_has_no_side_effects`.

    2.4.2: Valid replacement changes only the daemon namespace in one revision. test:
    `tests/servers/routes/test_config_yaml_replace.py::test_daemon_replacement_is_scoped_and_atomic`.

    2.4.3: Omissions restore daemon defaults without clearing supplemental/domain
    state. test: `tests/servers/routes/test_config_yaml_replace.py::test_omissions_restore_only_daemon_defaults`.

    2.4.4: Export round-trips without plaintext secret disclosure. test: `tests/servers/routes/test_config_yaml_replace.py::test_masked_export_round_trip`.

    2.4.5: A stale-revision replacement returns 409 and leaves rows, secrets, and
    the revision untouched. test: `tests/servers/routes/test_config_yaml_replace.py::test_stale_revision_replacement_is_rejected`.

    2.4.6: A replacement that persists but fails local activation reports committed
    success with failed-live metadata. test: `tests/servers/routes/test_config_yaml_replace.py::test_replacement_reports_apply_status`.

    2.4.7: YAML import and export round-trip the shared codec vector set with exact
    canonical bytes, without collisions or unintended structure. test: `tests/servers/routes/test_config_yaml_replace.py::test_yaml_round_trips_codec_vectors`.

    2.4.8: A replacement with an out-of-domain revision is rejected and a ceiling-refused
    replacement reports typed `revision_exhausted`. test: `tests/servers/routes/test_config_yaml_replace.py::test_yaml_revision_domain_and_exhaustion`.'
  labels:
  - covers:reactive-config-store:2.4:2.4.1
  - covers:reactive-config-store:2.4:2.4.2
  - covers:reactive-config-store:2.4:2.4.3
  - covers:reactive-config-store:2.4:2.4.4
  - covers:reactive-config-store:2.4:2.4.5
  - covers:reactive-config-store:2.4:2.4.6
  - covers:reactive-config-store:2.4:2.4.7
  - covers:reactive-config-store:2.4:2.4.8
  tdd: true
  source_section: '2.4'
  implementation_domain: backend
- title: Migrate browser configuration state
  category: code
  task_type: feature
  depends_on:
  - '2.1'
  validation_criteria: '2.5.1: Every browser mutation includes the current revision.
    test: `web/src/hooks/__tests__/useConfiguration.revision.test.ts::includes_revision_in_every_patch`.

    2.5.2: Conflict refresh preserves unsaved edits and requires resubmission. test:
    `web/src/hooks/__tests__/useConfiguration.revision.test.ts::preserves_draft_after_conflict`.

    2.5.3: UI preferences and project selection use universal paths. test: `web/src/hooks/__tests__/useSettings.test.ts::persists_settings_through_config_patch`.

    2.5.4: Higher WebSocket revisions trigger one coalesced refetch. test: `web/src/hooks/__tests__/useConfiguration.revision.test.ts::coalesces_config_revision_events`.

    2.5.5: Fields render their activation class and show desired-versus-active values
    for pending-restart keys. test: `web/src/components/settings/sections/__tests__/configFieldActivation.test.tsx::renders_activation_class_and_pending_restart_state`.

    2.5.6: Failed-live keys surface apply status and managed keys route to the managed
    action. test: `web/src/components/settings/sections/__tests__/configFieldActivation.test.tsx::routes_managed_keys_and_shows_failed_live_status`.

    2.5.7: WebSocket reconnect refetches and converges on a mutation committed while
    disconnected. test: `web/src/hooks/__tests__/useConfiguration.revision.test.ts::refetches_on_reconnect`.

    2.5.8: A higher event during an in-flight refetch produces one trailing refetch
    and older responses never render. test: `web/src/hooks/__tests__/useConfiguration.revision.test.ts::watermark_triggers_trailing_refetch`.

    2.5.9: The browser-authority audit rejects direct fetches, specialized writers,
    reset calls, and revisionless mutations. test: `web/src/__tests__/config-authority-audit.test.ts::web_has_one_config_authority`.

    2.5.10: The typed client round-trips the shared codec vector set with exact canonical
    bytes through browser state without re-encoding drift. test: `web/src/hooks/__tests__/useConfiguration.revision.test.ts::round_trips_codec_vectors`.

    2.5.11: A `revision_exhausted` result renders as a terminal non-retryable state
    and triggers no refetch-resubmit loop. test: `web/src/hooks/__tests__/useConfiguration.revision.test.ts::exhausted_revision_is_terminal`.'
  labels:
  - covers:reactive-config-store:2.5:2.5.1
  - covers:reactive-config-store:2.5:2.5.2
  - covers:reactive-config-store:2.5:2.5.3
  - covers:reactive-config-store:2.5:2.5.4
  - covers:reactive-config-store:2.5:2.5.5
  - covers:reactive-config-store:2.5:2.5.6
  - covers:reactive-config-store:2.5:2.5.7
  - covers:reactive-config-store:2.5:2.5.8
  - covers:reactive-config-store:2.5:2.5.9
  - covers:reactive-config-store:2.5:2.5.10
  - covers:reactive-config-store:2.5:2.5.11
  tdd: true
  source_section: '2.5'
  implementation_domain: frontend
- title: Generate and consume the Rust runtime-config contract
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  - '2.2'
  validation_criteria: '2.6.1: Generated Rust contract is byte-stable and current
    with the Python registry. test: `tests/config/test_runtime_config_contract.py::test_checked_in_contract_matches_registry`.

    2.6.2: Rust rejects machine keys absent from the generated contract. test: `crates/gcore/src/config/tests/runtime_contract.rs::rejects_unregistered_machine_key`.

    2.6.3: Gobby runtime mode ignores env/standalone precedence for registered keys.
    test: `crates/gcode/src/config/tests/runtime_contract.rs::gobby_mode_uses_registry_authority`.

    2.6.4: Direct hub fallback resolves service families and secret bindings from
    one revision-coherent snapshot, and a paused reader racing a same-reference rotation
    returns wholly old or wholly new material. test: `crates/gcode/src/config/tests/runtime_contract.rs::hub_fallback_reads_atomic_snapshot`.

    2.6.5: Rust and Python encode, match, and reject the shared codec vector set byte-identically,
    including its malformed and non-canonical inputs. test: `crates/gcode/src/config/tests/runtime_contract.rs::dynamic_segment_codec_matches_python`.

    2.6.6: Explicit standalone mode with no daemon or hub context honors environment
    and `gcore.yaml` precedence in their documented order. test: `crates/gcode/src/config/tests/runtime_contract.rs::standalone_mode_preserves_env_yaml_precedence`.'
  labels:
  - covers:reactive-config-store:2.6:2.6.1
  - covers:reactive-config-store:2.6:2.6.2
  - covers:reactive-config-store:2.6:2.6.3
  - covers:reactive-config-store:2.6:2.6.4
  - covers:reactive-config-store:2.6:2.6.5
  - covers:reactive-config-store:2.6:2.6.6
  tdd: true
  source_section: '2.6'
  implementation_domain: backend
- title: Wire ConfigRuntime into startup
  category: code
  task_type: feature
  depends_on:
  - '1.4'
  - '2.1'
  validation_criteria: '3.1.1: Startup constructs exactly one ConfigRuntime before
    post-database services. test: `tests/runner_init/test_config_runtime_startup.py::test_startup_constructs_one_runtime`.

    3.1.2: Runner and ServiceContainer expose the same ConfigRuntime instance. test:
    `tests/runner_init/test_config_runtime_startup.py::test_context_shares_runner_runtime`.

    3.1.3: Runtime notification lifecycle closes cleanly with daemon shutdown. test:
    `tests/runner_init/test_config_runtime_startup.py::test_runtime_closes_with_daemon`.

    3.1.4: Startup registers the config event publisher and one reconciled revision
    emits one event. test: `tests/runner_init/test_config_runtime_startup.py::test_startup_registers_config_event_publisher`.

    3.1.5: With a writer paused at the LISTEN-activation/reload boundary, a revision
    committed after subscription activation and before the initial reload converges
    before services construct, without a later notification. test: `tests/runner_init/test_config_runtime_startup.py::test_startup_closes_subscription_window`.

    3.1.6: Fresh startup with a failing required subscriber never reports ready, and
    a failing optional capability reports degraded then recovers after a successful
    affected-key activation. test: `tests/runner_init/test_config_runtime_startup.py::test_first_start_failure_and_recovery`.'
  labels:
  - covers:reactive-config-store:3.1:3.1.1
  - covers:reactive-config-store:3.1:3.1.2
  - covers:reactive-config-store:3.1:3.1.3
  - covers:reactive-config-store:3.1:3.1.4
  - covers:reactive-config-store:3.1:3.1.5
  - covers:reactive-config-store:3.1:3.1.6
  tdd: true
  source_section: '3.1'
  implementation_domain: backend
- title: Migrate generic policy consumers
  category: code
  task_type: feature
  depends_on:
  - '2.1'
  - '3.1'
  validation_criteria: '3.2.1: Rule evaluation observes live global toggles from one
    snapshot. test: `tests/config/test_live_policy_consumers.py::test_rules_use_runtime_snapshot`.

    3.2.2: Approval policy and launch defaults use typed registered paths. test: `tests/config/test_live_policy_consumers.py::test_approval_and_launch_defaults_are_registered`.

    3.2.3: Specialized setting writers disappear while domain CRUD remains. test:
    `tests/config/test_live_policy_consumers.py::test_only_specialized_setting_writers_are_removed`.

    3.2.4: Voice vocabulary persists through the typed API and attention/session routes
    read runtime snapshots. test: `tests/config/test_live_policy_consumers.py::test_voice_and_route_consumers_use_runtime`.

    3.2.5: Validation-detection preview reads the runtime snapshot instead of `ServiceContainer.config`.
    test: `tests/config/test_live_policy_consumers.py::test_validation_detection_uses_runtime_snapshot`.'
  labels:
  - covers:reactive-config-store:3.2:3.2.1
  - covers:reactive-config-store:3.2:3.2.2
  - covers:reactive-config-store:3.2:3.2.3
  - covers:reactive-config-store:3.2:3.2.4
  - covers:reactive-config-store:3.2:3.2.5
  tdd: true
  source_section: '3.2'
  implementation_domain: backend
- title: Separate restart-bound topology consumers
  category: code
  task_type: feature
  depends_on:
  - '3.1'
  validation_criteria: '3.3.1: Process topology reads only `BootstrapConfig`. test:
    `tests/config/test_restart_config_consumers.py::test_topology_uses_bootstrap_only`.

    3.3.2: Restart-class writes do not mutate running servers or middleware. test:
    `tests/config/test_restart_config_consumers.py::test_restart_changes_remain_pending`.

    3.3.3: Restart activates desired settings on the next startup snapshot. test:
    `tests/config/test_restart_config_consumers.py::test_restart_promotes_desired_to_active`.

    3.3.4: `auth_mode` resolves only from bootstrap across installer write, hook preflight,
    and HTTP construction, and is absent from the registry. test: `tests/config/test_restart_config_consumers.py::test_auth_mode_is_bootstrap_owned`.

    3.3.5: Startup sizes the pool and executors from the initial active revision after
    the fixed minimal bootstrap pool, and later concurrency changes stay pending until
    restart. test: `tests/config/test_restart_config_consumers.py::test_two_stage_pool_and_executor_sizing`.'
  labels:
  - covers:reactive-config-store:3.3:3.3.1
  - covers:reactive-config-store:3.3:3.3.2
  - covers:reactive-config-store:3.3:3.3.3
  - covers:reactive-config-store:3.3:3.3.4
  - covers:reactive-config-store:3.3:3.3.5
  tdd: true
  source_section: '3.3'
  implementation_domain: backend
- title: Add live stateful service subscribers
  category: code
  task_type: feature
  depends_on:
  - '3.1'
  - '3.3'
  validation_criteria: '3.4.1: Matching changes prepare all replacements before any
    swap. test: `tests/config/test_stateful_config_subscribers.py::test_prepare_precedes_every_swap`.

    3.4.2: Preparation failure disposes replacements and preserves all old services.
    test: `tests/config/test_stateful_config_subscribers.py::test_failed_prepare_keeps_last_good_services`.

    3.4.3: Successful swaps drain old in-flight clients. test: `tests/config/test_stateful_config_subscribers.py::test_successful_swap_drains_old_client`.

    3.4.4: API-key changes invalidate only dependent cached clients. test: `tests/config/test_stateful_config_subscribers.py::test_key_scoped_invalidation`.

    3.4.5: A revision committed during registration leaves the subscriber at the newest
    revision exactly once. test: `tests/config/test_stateful_config_subscribers.py::test_registration_race_resolves_to_latest_revision`.

    3.4.6: Preparation timeout disposes replacements, preserves last-good services,
    and records failed-live keys. test: `tests/config/test_stateful_config_subscribers.py::test_preparation_timeout_preserves_last_good`.

    3.4.7: Shutdown cancels in-flight preparation and drain within bounds and a drain
    failure never rolls back active state. test: `tests/config/test_stateful_config_subscribers.py::test_shutdown_cancels_subscriber_work`.

    3.4.8: Forced thread interleaving across a multi-key revision never observes a
    mixed service/snapshot epoch. test: `tests/config/test_stateful_config_subscribers.py::test_no_mixed_epoch_under_interleaving`.

    3.4.9: Every live-activation registry key resolves to a subscriber adapter or
    a declared per-operation read, and the matrix assertion fails on an unmapped key.
    test: `tests/config/test_stateful_config_subscribers.py::test_live_key_consumer_matrix_is_complete`.

    3.4.10: Adapter first-registration failure follows the declared contract: required
    blocks readiness with partial disposal, optional publishes an unavailable slot
    and recovers on later activation. test: `tests/config/test_stateful_config_subscribers.py::test_first_registration_failure_contract`.'
  labels:
  - covers:reactive-config-store:3.4:3.4.1
  - covers:reactive-config-store:3.4:3.4.2
  - covers:reactive-config-store:3.4:3.4.3
  - covers:reactive-config-store:3.4:3.4.4
  - covers:reactive-config-store:3.4:3.4.5
  - covers:reactive-config-store:3.4:3.4.6
  - covers:reactive-config-store:3.4:3.4.7
  - covers:reactive-config-store:3.4:3.4.8
  - covers:reactive-config-store:3.4:3.4.9
  - covers:reactive-config-store:3.4:3.4.10
  tdd: true
  source_section: '3.4'
  implementation_domain: backend
- title: Migrate loops and lifecycle consumers
  category: code
  task_type: feature
  depends_on:
  - '3.1'
  - '3.4'
  validation_criteria: '3.5.1: Periodic work uses one coherent snapshot per iteration.
    test: `tests/config/test_runtime_loop_consumers.py::test_periodic_iteration_uses_one_snapshot`.

    3.5.2: Live lifecycle consumers observe successful runtime swaps. test: `tests/config/test_runtime_loop_consumers.py::test_lifecycle_consumer_observes_live_change`.

    3.5.3: Restart-class lifecycle consumers retain startup active values. test: `tests/config/test_runtime_loop_consumers.py::test_lifecycle_consumer_retains_restart_value`.

    3.5.4: Runner lifecycle, shutdown, subsystem, and readiness modules read no `runner.config`
    attribute. test: `tests/config/test_runtime_loop_consumers.py::test_lifecycle_modules_use_runtime_access`.'
  labels:
  - covers:reactive-config-store:3.5:3.5.1
  - covers:reactive-config-store:3.5:3.5.2
  - covers:reactive-config-store:3.5:3.5.3
  - covers:reactive-config-store:3.5:3.5.4
  tdd: true
  source_section: '3.5'
  implementation_domain: backend
- title: Integrate managed embedding activation
  category: code
  task_type: feature
  depends_on:
  - '1.4'
  - '2.1'
  - '3.4'
  validation_criteria: "3.6.1: Generic interfaces reject structural embedding mutations.\
    \ test: `tests/storage/test_embedding_switch_config_contract.py::test_structural_keys_require_switch`.\n\
    3.6.2: Switch completion commits canonical values in one revision. test: `tests/ai/test_embedding_switch_daemon_lifecycle.py::test_switch_commit_is_one_revision`.\n\
    3.6.3: Switch recovery reads ConfigRuntime instead of rebuilding configuration.\
    \ test: `tests/ai/test_embedding_switch_daemon_lifecycle.py::test_switch_recovery_uses_runtime_snapshot`.\n\
    3.6.4: API-key rotation is live and invalidates the embedding client. test: `tests/storage/test_embedding_switch_config_contract.py::test_api_key_rotation_is_live`.\n\
    3.6.5: A remote runtime observing a managed structural revision verifies the journal,\
    \ promotes by capturing the generation's physical targets, and rebuilds clients;\
    \ crash recovery at any journal phase converges without a mixed state. test: `tests/ai/test_embedding_switch_daemon_lifecycle.py::test_managed_revision_converges_across_runtimes`.\n\
    3.6.6: A remote daemon reloading after journal deletion verifies the persisted\
    \ completed record and converges, including after reconnect. test: `tests/ai/test_embedding_switch_daemon_lifecycle.py::test_remote_catchup_after_journal_gc`.\n\
    3.6.7: Generation GC waits for every unexpired lease's acknowledgement and bounded\
    \ drains, never deletes a generation an unexpired lease still covers, and a daemon\
    \ that cannot renew self-fences before expiry and reconciles before serving again.\
    \ test: `tests/ai/test_embedding_switch_daemon_lifecycle.py::test_generation_gc_waits_for_acknowledgements`.\n\
    3.6.8: A mutation committed after build enumeration \u2014 in-process or on a\
    \ second daemon \u2014 is present in the promoted generation, deletions tombstone\
    \ through replay, and no daemon acknowledges a generation missing its committed\
    \ writes. test: `tests/ai/test_embedding_switch_daemon_lifecycle.py::test_write_catchup_replays_into_promoted_generation`."
  labels:
  - covers:reactive-config-store:3.6:3.6.1
  - covers:reactive-config-store:3.6:3.6.2
  - covers:reactive-config-store:3.6:3.6.3
  - covers:reactive-config-store:3.6:3.6.4
  - covers:reactive-config-store:3.6:3.6.5
  - covers:reactive-config-store:3.6:3.6.6
  - covers:reactive-config-store:3.6:3.6.7
  - covers:reactive-config-store:3.6:3.6.8
  tdd: true
  source_section: '3.6'
  implementation_domain: backend
- title: Replace load_full_config_from_db and every caller
  category: code
  task_type: feature
  depends_on:
  - '1.4'
  validation_criteria: '3.7.1: Every known loader caller uses `CliRuntime`''s typed
    snapshot. test: `tests/cli/test_cli_runtime_config.py::test_full_loader_callers_use_cli_runtime`.

    3.7.2: The loader is no longer importable from its defining module. test: `tests/cli/test_cli_runtime_config.py::test_full_loader_is_not_exported`.

    3.7.3: CLI runtime closes its short-lived configuration resources. test: `tests/cli/test_cli_runtime_config.py::test_cli_runtime_closes_config_resources`.'
  labels:
  - covers:reactive-config-store:3.7:3.7.1
  - covers:reactive-config-store:3.7:3.7.2
  - covers:reactive-config-store:3.7:3.7.3
  tdd: true
  source_section: '3.7'
  implementation_domain: backend
- title: Migrate bootstrap-oriented load_config callers
  category: code
  task_type: feature
  depends_on:
  - '1.4'
  - '3.7'
  validation_criteria: '3.8.1: Pre-database operations read only bootstrap fields.
    test: `tests/cli/test_bootstrap_config_consumers.py::test_pre_database_operations_use_bootstrap`.

    3.8.2: Post-database operations read one typed snapshot. test: `tests/cli/test_bootstrap_config_consumers.py::test_post_database_operations_use_runtime_snapshot`.

    3.8.3: Config package and CLI utilities no longer re-export either loader. test:
    `tests/cli/test_bootstrap_config_consumers.py::test_loader_reexports_are_removed`.'
  labels:
  - covers:reactive-config-store:3.8:3.8.1
  - covers:reactive-config-store:3.8:3.8.2
  - covers:reactive-config-store:3.8:3.8.3
  tdd: true
  source_section: '3.8'
  implementation_domain: backend
- title: Migrate operational CLI and hook callers
  category: code
  task_type: feature
  depends_on:
  - '1.4'
  - '3.7'
  validation_criteria: '3.9.1: Operational commands contain no fresh `load_config`
    call. test: `tests/cli/test_operational_config_consumers.py::test_operational_commands_use_runtime_authority`.

    3.9.2: Commands use one coherent revision for each operation. test: `tests/cli/test_operational_config_consumers.py::test_command_reads_one_revision`.

    3.9.3: Hooks use bootstrap-only or typed snapshot inputs according to lifecycle.
    test: `tests/cli/test_operational_config_consumers.py::test_hook_config_boundary`.'
  labels:
  - covers:reactive-config-store:3.9:3.9.1
  - covers:reactive-config-store:3.9:3.9.2
  - covers:reactive-config-store:3.9:3.9.3
  tdd: true
  source_section: '3.9'
  implementation_domain: backend
- title: Migrate stdio and proxy callers
  category: code
  task_type: feature
  depends_on:
  - '1.4'
  - '3.7'
  validation_criteria: '3.10.1: Stdio dependency factories no longer expose `load_config`.
    test: `tests/mcp_proxy/test_stdio_config_runtime.py::test_stdio_dependencies_use_runtime_access`.

    3.10.2: Daemon startup uses bootstrap topology and runtime snapshots at the correct
    boundary. test: `tests/mcp_proxy/test_stdio_config_runtime.py::test_stdio_daemon_config_boundary`.

    3.10.3: Proxy/server operations capture one runtime revision. test: `tests/mcp_proxy/test_stdio_config_runtime.py::test_stdio_operation_reads_one_revision`.'
  labels:
  - covers:reactive-config-store:3.10:3.10.1
  - covers:reactive-config-store:3.10:3.10.2
  - covers:reactive-config-store:3.10:3.10.3
  tdd: true
  source_section: '3.10'
  implementation_domain: backend
- title: Remove alternate authorities and enforce the boundary
  category: code
  task_type: feature
  depends_on:
  - '2.3'
  - '2.4'
  - '2.5'
  - '2.6'
  - '3.2'
  - '3.3'
  - '3.4'
  - '3.5'
  - '3.6'
  - '3.7'
  - '3.8'
  - '3.9'
  - '3.10'
  validation_criteria: '4.1.1: Python runtime code contains no alternate configuration
    authority or raw dotted access outside the enumerated auth-owned seam. test: `tests/config/test_config_authority_audit.py::test_python_runtime_has_one_config_authority`.

    4.1.2: Every Python and Rust Gobby-runtime key has one registry owner and a current
    generated contract entry. test: `tests/config/test_config_authority_audit.py::test_cross_language_registry_coverage`.

    4.1.3: Legacy loaders, mutable fields, routes, and MCP tools are absent. test:
    `tests/config/test_config_authority_audit.py::test_legacy_config_surfaces_are_absent`.

    4.1.4: Final operator behavior is documented. behavior: "Reactive runtime configuration
    contract" in `docs/guides/configuration.md`.'
  labels:
  - covers:reactive-config-store:4.1:4.1.1
  - covers:reactive-config-store:4.1:4.1.2
  - covers:reactive-config-store:4.1:4.1.3
  - covers:reactive-config-store:4.1:4.1.4
  tdd: true
  source_section: '4.1'
  implementation_domain: backend
- title: Add the two-daemon PostgreSQL convergence suite
  category: test
  task_type: feature
  depends_on:
  - '4.1'
  validation_criteria: '4.2.1: A write through runtime A updates runtime B through
    PostgreSQL notification. test: `tests/integration/config/test_reactive_config_multi_daemon.py::test_remote_daemon_converges_after_commit`.

    4.2.2: Forced listener termination reconnects and reloads the latest revision.
    test: `tests/integration/config/test_reactive_config_multi_daemon.py::test_listener_restart_recovers_latest_snapshot`.

    4.2.3: Apply failure keeps the committed desired revision, only the failing daemon
    retains its old active state, and the other daemon independently activates the
    committed revision. test: `tests/integration/config/test_reactive_config_multi_daemon.py::test_apply_failure_is_process_local`.

    4.2.4: A local commit reconciles once and its own notification invokes no subscriber
    again. test: `tests/integration/config/test_reactive_config_multi_daemon.py::test_local_commit_reconciles_once`.

    4.2.5: A managed embedding switch on daemon A converges daemon B, and a crash
    at the flip or commit boundary recovers to a coherent structural state. test:
    `tests/integration/config/test_reactive_config_multi_daemon.py::test_managed_switch_converges_across_daemons`.

    4.2.6: Two daemons with distinct homes and the shared-passphrase posture converge
    on a remote API-key rotation. test: `tests/integration/config/test_reactive_config_multi_daemon.py::test_remote_secret_rotation_with_shared_kek`.

    4.2.7: A wrong-key daemon fails closed and never reports healthy. test: `tests/integration/config/test_reactive_config_multi_daemon.py::test_wrong_kek_daemon_fails_closed`.

    4.2.8: A same-reference rotation with one failing daemon proves per-daemon payload
    isolation and public redaction. test: `tests/integration/config/test_reactive_config_multi_daemon.py::test_same_reference_rotation_failure_isolation`.

    4.2.9: Concurrent cross-process writes sharing a stale expected revision yield
    exactly one commit and one typed conflict. test: `tests/integration/config/test_reactive_config_multi_daemon.py::test_cross_process_cas_conflict`.

    4.2.10: A restart-required change keeps desired and active state separated on
    both daemons until a worker restart activates it. test: `tests/integration/config/test_reactive_config_multi_daemon.py::test_restart_pending_state_across_daemons`.

    4.2.11: A daemon losing PostgreSQL connectivity while HTTP and Qdrant stay live
    self-fences embedding requests before lease expiry, GC proceeds safely, and the
    daemon reconciles and resumes serving after reconnect. test: `tests/integration/config/test_reactive_config_multi_daemon.py::test_partitioned_daemon_self_fences_before_gc`.

    4.2.12: Create, update, and delete races at the build, flip, config-commit, and
    promotion boundaries across two daemons converge with every committed mutation
    present in the promoted generation. test: `tests/integration/config/test_reactive_config_multi_daemon.py::test_switch_write_races_converge`.'
  labels:
  - covers:reactive-config-store:4.2:4.2.1
  - covers:reactive-config-store:4.2:4.2.2
  - covers:reactive-config-store:4.2:4.2.3
  - covers:reactive-config-store:4.2:4.2.4
  - covers:reactive-config-store:4.2:4.2.5
  - covers:reactive-config-store:4.2:4.2.6
  - covers:reactive-config-store:4.2:4.2.7
  - covers:reactive-config-store:4.2:4.2.8
  - covers:reactive-config-store:4.2:4.2.9
  - covers:reactive-config-store:4.2:4.2.10
  - covers:reactive-config-store:4.2:4.2.11
  - covers:reactive-config-store:4.2:4.2.12
  tdd: false
  source_section: '4.2'
  assigned_agent: qa-dev
```
