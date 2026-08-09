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

**Acceptance:**

- 1.1.1 - Every non-bootstrap daemon leaf resolves to exactly one spec. test: `tests/config/test_config_registry.py::test_every_daemon_leaf_has_one_spec`.
- 1.1.2 - Every mapping leaf has an explicit non-overlapping pattern adapter. test: `tests/config/test_config_registry.py::test_mapping_patterns_are_complete`.
- 1.1.3 - Public and machine schemas expose only their declared visibility classes. test: `tests/config/test_config_registry.py::test_visibility_partitions_are_disjoint`.

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

**Acceptance:**

- 1.3.1 - Concurrent writers sharing an expected revision yield one commit and one typed conflict. test: `tests/storage/test_revisioned_config_store.py::test_compare_and_swap_serializes_writers`.
- 1.3.2 - Values, unsets, secret payloads, row revisions, global revision, and notification commit atomically. test: `tests/storage/test_revisioned_config_store.py::test_mutation_is_one_transaction`.
- 1.3.3 - Invalid candidates leave configuration, secrets, revision, and notifications untouched. test: `tests/storage/test_revisioned_config_store.py::test_invalid_candidate_has_no_side_effects`.
- 1.3.4 - No-op and secret-rotation behavior follows the effective-change rule. test: `tests/storage/test_revisioned_config_store.py::test_effective_change_controls_revision`.

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

A dedicated `psycopg.AsyncConnection` comes from a pool-exempt hub factory that applies
`SET ROLE gobby_daemon_runtime` — matching the pool's least-privilege session identity —
before executing `LISTEN gobby_config_changed`, and is owned by `ConfigRuntime`'s async
lifecycle. Local commits reconcile immediately. Remote notifications, revision gaps, and
reconnects trigger full atomic reloads. Database restart causes bounded reconnect
backoff followed by full reload before healthy status.

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
preparation result is discarded rather than published. A local commit therefore
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

Restart changes update desired state while retaining old active values. Managed changes
are rejected before persistence.

**Acceptance:**

- 1.4.1 - Readers observe immutable single-revision snapshots. test: `tests/config/test_config_runtime.py::test_snapshot_swap_is_atomic`.
- 1.4.2 - Restart writes separate desired and active state and report pending keys. test: `tests/config/test_config_runtime.py::test_restart_policy_tracks_pending_keys`.
- 1.4.3 - Post-commit preparation failure keeps desired state and revision committed, performs no active swap and no compensating write, and records failed-live metadata. test: `tests/config/test_config_runtime.py::test_apply_failure_preserves_local_last_good_state`.
- 1.4.4 - A second runtime receives remote revisions over the pool-exempt listener. test: `tests/config/test_config_runtime.py::test_remote_runtime_receives_revision_notification`.
- 1.4.5 - Listener reconnect performs a full reload before health recovery. test: `tests/config/test_config_runtime.py::test_listener_reconnect_reloads_snapshot`.
- 1.4.6 - Duplicate, current, and burst notifications reconcile at most once to the latest revision. test: `tests/config/test_config_runtime.py::test_notifications_are_idempotent_and_coalesced`.
- 1.4.7 - The pool-exempt listener runs under the daemon runtime role and receives notifications, reconnects, and closes under it. test: `tests/config/test_config_runtime.py::test_listener_assumes_runtime_role`.
- 1.4.8 - A delayed older reload completing after a newer one is discarded and never published. test: `tests/config/test_config_runtime.py::test_out_of_order_reload_is_discarded`.

## P2: Public and Machine Interfaces
`kind: framing`

### 2.1 Replace the public HTTP configuration API [category: code] (depends: 1.4)
`kind: deliverable`

Targets:
- `src/gobby/servers/routes/configuration_models.py::*` — scope-reason: replace save/reset models with revisioned request and response models
- `src/gobby/servers/routes/configuration_context.py::*` — scope-reason: expose ConfigRuntime to configuration routes
- `src/gobby/servers/routes/configuration_values.py::*` — scope-reason: replace duplicated validation and mutation behavior
- `src/gobby/servers/routes/configuration_secrets.py::*` — scope-reason: route registered secret fields through universal patch semantics
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
Structural embedding keys return `managed_activation_required` with
`/api/embeddings/switch/start`. Remove reset and caller-controlled secrecy.

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

### 2.2 Preserve the authenticated Rust machine contract [category: code] (depends: 1.4)
`kind: deliverable`

Targets:
- `src/gobby/servers/routes/configuration_effective.py::*` — scope-reason: rebuild every route helper on the active snapshot while preserving the flat envelope, runtime-token auth, and machine secret resolution
- `tests/servers/routes/test_configuration_effective_routes.py::*` — scope-reason: pin authenticated resolved-secret and capability contracts

Keep `/api/config/effective` returning flat dotted `{"config": {...}}` from the active
snapshot. Select keys through registry machine visibility and resolve allowed secret
references to plaintext for authenticated binaries. Keep `Cache-Control: no-store` and
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

### 2.3 Replace MCP configuration tools [category: code] (depends: 1.4, 2.1)
`kind: deliverable`

Targets:
- `src/gobby/mcp_proxy/tools/config.py::*` — scope-reason: shrink the registry to schema, values, and patch tools
- `tests/mcp_proxy/tools/test_config_values.py`

Retain the module and expose only `get_config_schema`, `get_config_values`, and
`patch_config_values`. Reuse the same service and errors as HTTP. Every mutation requires
`expected_revision`.

**Acceptance:**

- 2.3.1 - MCP and HTTP return equivalent schema, values, and patch results. test: `tests/mcp_proxy/tools/test_config_values.py::test_mcp_wraps_universal_config_service`.
- 2.3.2 - MCP patch requires revision and preserves secret/managed policies. test: `tests/mcp_proxy/tools/test_config_values.py::test_mcp_patch_requires_revision`.
- 2.3.3 - Raw get/set/delete/batch/list/default-seeding tools are absent. test: `tests/mcp_proxy/tools/test_config_values.py::test_legacy_config_tools_are_removed`.

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
revision requires an explicit refetch before resubmission.

Omitted daemon settings restore defaults. UI preferences, credentials, operational data,
domain records, and bootstrap remain untouched. Export desired daemon configuration with
masks/references. Prompt/domain bundles remain separate.

**Acceptance:**

- 2.4.1 - Invalid documents preserve rows, secrets, and revision. test: `tests/servers/routes/test_config_yaml_replace.py::test_invalid_document_has_no_side_effects`.
- 2.4.2 - Valid replacement changes only the daemon namespace in one revision. test: `tests/servers/routes/test_config_yaml_replace.py::test_daemon_replacement_is_scoped_and_atomic`.
- 2.4.3 - Omissions restore daemon defaults without clearing supplemental/domain state. test: `tests/servers/routes/test_config_yaml_replace.py::test_omissions_restore_only_daemon_defaults`.
- 2.4.4 - Export round-trips without plaintext secret disclosure. test: `tests/servers/routes/test_config_yaml_replace.py::test_masked_export_round_trip`.
- 2.4.5 - A stale-revision replacement returns 409 and leaves rows, secrets, and the revision untouched. test: `tests/servers/routes/test_config_yaml_replace.py::test_stale_revision_replacement_is_rejected`.

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
the unsaved local draft, and require explicit resubmission. Coalesce higher-revision
WebSocket events into one refetch. Remove specialized UI-setting, reset, launch-default,
and approval-setting writes.

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
byte-compares it.

Rust effective-config validation consumes this asset instead of `MANAGED_CONFIG_KEYS`. In
Gobby daemon or hub mode:

- Daemon-served active config is authoritative when available.
- Direct PostgreSQL fallback opens one read-only `REPEATABLE READ` transaction in
  `Context::resolve_services`, captures `config_state.revision` and all machine-visible
  rows exactly once, resolves every service family from that immutable map, and returns
  the captured revision.
- Registered runtime keys do not accept environment or `gcore.yaml` precedence.
- Secret references resolve only through the hub secret store.

Environment and `gcore.yaml` remain available only in explicit standalone mode with no
Gobby daemon/hub context. The gcode config module root is `crates/gcode/src/config.rs`;
both crates declare their new test submodule from the existing `config/tests.rs` file.

**Acceptance:**

- 2.6.1 - Generated Rust contract is byte-stable and current with the Python registry. test: `tests/config/test_runtime_config_contract.py::test_checked_in_contract_matches_registry`.
- 2.6.2 - Rust rejects machine keys absent from the generated contract. test: `crates/gcore/src/config/tests/runtime_contract.rs::rejects_unregistered_machine_key`.
- 2.6.3 - Gobby runtime mode ignores env/standalone precedence for registered keys. test: `crates/gcode/src/config/tests/runtime_contract.rs::gobby_mode_uses_registry_authority`.
- 2.6.4 - Direct hub fallback resolves multiple service families from one revision-coherent snapshot and resolves secrets. test: `crates/gcode/src/config/tests/runtime_contract.rs::hub_fallback_reads_atomic_snapshot`.

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
registry/store/runtime, starts the listener and awaits `LISTEN` acknowledgement, then
performs the initial revision-coherent load before constructing post-database services.
A revision committed in the read/subscribe window therefore converges without a later
notification.

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
- 3.1.5 - A revision committed between the initial read and LISTEN acknowledgement converges before services construct, without a later notification. test: `tests/runner_init/test_config_runtime_startup.py::test_startup_closes_subscription_window`.

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
- `tests/config/test_live_policy_consumers.py`

Read one typed snapshot per policy decision. Move global rules, approvals, launch
defaults, and UI preferences to generic PATCH. Preserve domain CRUD and per-domain
lifecycle behavior.

**Acceptance:**

- 3.2.1 - Rule evaluation observes live global toggles from one snapshot. test: `tests/config/test_live_policy_consumers.py::test_rules_use_runtime_snapshot`.
- 3.2.2 - Approval policy and launch defaults use typed registered paths. test: `tests/config/test_live_policy_consumers.py::test_approval_and_launch_defaults_are_registered`.
- 3.2.3 - Specialized setting writers disappear while domain CRUD remains. test: `tests/config/test_live_policy_consumers.py::test_only_specialized_setting_writers_are_removed`.
- 3.2.4 - Voice vocabulary persists through the typed API and attention/session routes read runtime snapshots. test: `tests/config/test_live_policy_consumers.py::test_voice_and_route_consumers_use_runtime`.

### 3.3 Separate restart-bound topology consumers [category: code] (depends: 3.1)
`kind: deliverable`

Targets:
- `src/gobby/runner_init/servers.py::*` — scope-reason: separate bootstrap topology from restart-class runtime settings
- `src/gobby/servers/app_factory.py::*` — scope-reason: construct middleware from the startup active snapshot
- `src/gobby/servers/_app_ui.py::*` — scope-reason: keep UI server lifecycle restart-bound
- `src/gobby/servers/http.py::*` — scope-reason: capture auth_mode from BootstrapConfig at construction
- `tests/config/test_restart_config_consumers.py`

Daemon/WS/UI ports, bind addresses, service bind address, daemon URL, database URL, and
pool sizing come from bootstrap. CORS, test mode, WebSocket enablement, UI
lifecycle, database services, telemetry, and memory backend come from the startup active
snapshot and do not mutate running topology.

Interim `auth_mode` ownership moves fully to bootstrap: HTTP construction captures it
from `BootstrapConfig` instead of `ServiceContainer.config`, hook preflight keeps its
bootstrap read, the installer remains the only writer, and the runtime registry excludes
the key. #19650 §2.2 deletes the field; this deliverable only relocates the read it
would otherwise strand.

**Acceptance:**

- 3.3.1 - Process topology reads only `BootstrapConfig`. test: `tests/config/test_restart_config_consumers.py::test_topology_uses_bootstrap_only`.
- 3.3.2 - Restart-class writes do not mutate running servers or middleware. test: `tests/config/test_restart_config_consumers.py::test_restart_changes_remain_pending`.
- 3.3.3 - Restart activates desired settings on the next startup snapshot. test: `tests/config/test_restart_config_consumers.py::test_restart_promotes_desired_to_active`.
- 3.3.4 - `auth_mode` resolves only from bootstrap across installer write, hook preflight, and HTTP construction, and is absent from the registry. test: `tests/config/test_restart_config_consumers.py::test_auth_mode_is_bootstrap_owned`.

### 3.4 Add live stateful service subscribers [category: code] (depends: 3.1)
`kind: deliverable`

Targets:
- `src/gobby/runner_init/config_subscribers.py`
- `src/gobby/runner_init/services.py::*` — scope-reason: register cached-service replacement adapters
- `src/gobby/servers/http.py::*` — scope-reason: replace server.config access for stateful services
- `tests/config/test_stateful_config_subscribers.py`

Implement focused subscriber adapters for cached providers, model clients, embedding
clients, MCP proxy settings, chat limits, and other constructor-captured live settings.
Each adapter prepares replacements without publishing, commits via no-fail reference swap,
and drains old in-flight work.

Registration uses the section 1.4 serialized handshake: each adapter hydrates from the
active projection ConfigRuntime returns at registration, and a revision committed during
registration reconciles before registration completes.

Adapters implement the section 1.4 work bounds: named preparation and disposal
deadlines, timeout disposal that preserves last-good services and records failed-live
keys, snapshot publication before bounded old-resource drain, and drain failures that
never roll back active state. Daemon shutdown cancels in-flight preparation and drain
work within the same bounds.

**Acceptance:**

- 3.4.1 - Matching changes prepare all replacements before any swap. test: `tests/config/test_stateful_config_subscribers.py::test_prepare_precedes_every_swap`.
- 3.4.2 - Preparation failure disposes replacements and preserves all old services. test: `tests/config/test_stateful_config_subscribers.py::test_failed_prepare_keeps_last_good_services`.
- 3.4.3 - Successful swaps drain old in-flight clients. test: `tests/config/test_stateful_config_subscribers.py::test_successful_swap_drains_old_client`.
- 3.4.4 - API-key changes invalidate only dependent cached clients. test: `tests/config/test_stateful_config_subscribers.py::test_key_scoped_invalidation`.
- 3.4.5 - A revision committed during registration leaves the subscriber at the newest revision exactly once. test: `tests/config/test_stateful_config_subscribers.py::test_registration_race_resolves_to_latest_revision`.
- 3.4.6 - Preparation timeout disposes replacements, preserves last-good services, and records failed-live keys. test: `tests/config/test_stateful_config_subscribers.py::test_preparation_timeout_preserves_last_good`.
- 3.4.7 - Shutdown cancels in-flight preparation and drain within bounds and a drain failure never rolls back active state. test: `tests/config/test_stateful_config_subscribers.py::test_shutdown_cancels_subscriber_work`.

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

Long-lived loops read one active snapshot at iteration boundaries. Lifecycle and route
initialization receive either ConfigRuntime or an explicitly captured startup active
projection according to activation policy.

**Acceptance:**

- 3.5.1 - Periodic work uses one coherent snapshot per iteration. test: `tests/config/test_runtime_loop_consumers.py::test_periodic_iteration_uses_one_snapshot`.
- 3.5.2 - Live lifecycle consumers observe successful runtime swaps. test: `tests/config/test_runtime_loop_consumers.py::test_lifecycle_consumer_observes_live_change`.
- 3.5.3 - Restart-class lifecycle consumers retain startup active values. test: `tests/config/test_runtime_loop_consumers.py::test_lifecycle_consumer_retains_restart_value`.
- 3.5.4 - Runner lifecycle, shutdown, subsystem, and readiness modules read no `runner.config` attribute. test: `tests/config/test_runtime_loop_consumers.py::test_lifecycle_modules_use_runtime_access`.

### 3.6 Integrate managed embedding activation [category: code] (depends: 1.4, 2.1, 3.4)
`kind: deliverable`

Targets:
- `src/gobby/config/embedding_keys.py::*` — scope-reason: make canonical registry paths the only persisted namespace
- `src/gobby/ai/embedding_switch_service.py::EmbeddingSwitchCoordinator`
- `src/gobby/ai/embedding_switch_runner.py::*` — scope-reason: replace fresh config loads throughout switch execution
- `src/gobby/cli/installers/embedding.py::*` — scope-reason: use typed runtime snapshots during installation
- `src/gobby/servers/routes/embeddings.py::create_embeddings_router`
- `tests/storage/test_embedding_switch_config_contract.py::*` — scope-reason: extend existing switch/config ownership tests
- `tests/ai/test_embedding_switch_daemon_lifecycle.py::*` — scope-reason: verify revisioned switch recovery

Persist only canonical `ai.embeddings.*`. Structural keys commit only through the switch
coordinator's restricted revisioned mutation. API-key rotation remains live and
invalidates embedding clients.

Managed structural revisions converge on every daemon. The committed configuration
revision is tied to the existing embedding lifecycle journal: a runtime observing a
managed structural revision — locally or over notification — verifies the completed
switch against the journal, promotes active structural values only after the shared
aliases are ready, and rebuilds dependent embedding clients. Restart recovery resolves
each journal phase from ConfigRuntime plus the journal, so a crash at the flip or commit
boundary converges to either the old or the new structural state, never a mix.

**Acceptance:**

- 3.6.1 - Generic interfaces reject structural embedding mutations. test: `tests/storage/test_embedding_switch_config_contract.py::test_structural_keys_require_switch`.
- 3.6.2 - Switch completion commits canonical values in one revision. test: `tests/ai/test_embedding_switch_daemon_lifecycle.py::test_switch_commit_is_one_revision`.
- 3.6.3 - Switch recovery reads ConfigRuntime instead of rebuilding configuration. test: `tests/ai/test_embedding_switch_daemon_lifecycle.py::test_switch_recovery_uses_runtime_snapshot`.
- 3.6.4 - API-key rotation is live and invalidates the embedding client. test: `tests/storage/test_embedding_switch_config_contract.py::test_api_key_rotation_is_live`.
- 3.6.5 - A remote runtime observing a managed structural revision verifies the journal, promotes after aliases are ready, and rebuilds clients; crash recovery at any journal phase converges without a mixed state. test: `tests/ai/test_embedding_switch_daemon_lifecycle.py::test_managed_revision_converges_across_runtimes`.

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
subsystem, and readiness modules, voice MCP tools, attention and session routes — and
the section 2.5 browser-authority audit runs in the same gate.

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
idempotency, and latest-snapshot convergence.

Use isolated ports/state and never contact the user's running daemon.

**Acceptance:**

- 4.2.1 - A write through runtime A updates runtime B through PostgreSQL notification. test: `tests/integration/config/test_reactive_config_multi_daemon.py::test_remote_daemon_converges_after_commit`.
- 4.2.2 - Forced listener termination reconnects and reloads the latest revision. test: `tests/integration/config/test_reactive_config_multi_daemon.py::test_listener_restart_recovers_latest_snapshot`.
- 4.2.3 - Apply failure keeps the committed desired revision, only the failing daemon retains its old active state, and the other daemon independently activates the committed revision. test: `tests/integration/config/test_reactive_config_multi_daemon.py::test_apply_failure_is_process_local`.
- 4.2.4 - A local commit reconciles once and its own notification invokes no subscriber again. test: `tests/integration/config/test_reactive_config_multi_daemon.py::test_local_commit_reconciles_once`.
- 4.2.5 - A managed embedding switch on daemon A converges daemon B, and a crash at the flip or commit boundary recovers to a coherent structural state. test: `tests/integration/config/test_reactive_config_multi_daemon.py::test_managed_switch_converges_across_daemons`.

### 4.3 Delete auth-owned raw configuration access [category: code] (depends: 4.1)
`kind: deliverable`

Targets:
- `src/gobby/storage/config_store.py::*` — scope-reason: delete the remaining auth-owned raw access seam
- `tests/config/test_config_authority_audit.py`

Delete the raw ConfigStore get/set/batch/delete/reset/default APIs once the #19650
auth consumers use the restricted typed API. At expansion this leaf gains an enforceable
task-graph blocker on #19650; prose alone does not gate execution. #17769 carries no
blocker by explicit decision: its consumers do not exist yet and are born onto the
restricted typed API this plan provides (typed deferral, see V1 Round 1).

**Acceptance:**

- 4.3.1 - Raw ConfigStore access APIs are absent and the audit runs without the auth-owned seam allowance. test: `tests/config/test_config_authority_audit.py::test_no_raw_config_store_access_remains`.
- 4.3.2 - Auth consumers read and write credentials only through the restricted typed API. test: `tests/config/test_config_authority_audit.py::test_auth_consumers_use_restricted_typed_api`.

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
