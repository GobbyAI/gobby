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
- `auth_mode` becomes restart-required registry configuration. Configurable
  `hub_backend` is removed.
- Dotted ConfigStore primary keys remain; no generalized scope columns.
- Public configuration includes daemon settings, UI preferences, global rule toggles,
  global approval policy, launch defaults, provider/model choices, feature flags,
  limits, schedules, and registered API-key references.
- Domain stores retain agents, individual rules and enabled flags, workflows, prompts,
  templates, and executable definitions.
- `auth.*` credential payloads and embedding lifecycle journals use restricted specs and
  are excluded from public schema, public values, YAML, and exports.
- Live is the default activation policy. Restart-required keys are `auth_mode`,
  `cors_origins`, `test_mode`, `database_concurrency.*`, `databases.*`, `telemetry.*`,
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
- The embedded baseline remains version 375 with its existing checksum. Schema evolution
  uses migration 376, the first entry in the currently empty `MIGRATIONS` chain
  (`crates/gcore/src/schema/assets.rs`). Adding it changes `root_hash()`,
  `latest_version`, and `latest_checksum`, so the release-pinned schema identity is
  regenerated in the same deliverable.
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
- 1.1.4 - `auth_mode` is absent from bootstrap and registered as restart-required. symbol: `BootstrapConfig`.

### 1.2 Add post-baseline migration 376 [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `crates/gcore/assets/schema/migrations/376_reactive_config_store.sql`
- `crates/gcore/assets/schema/catalog.manifest.json::*` — scope-reason: regenerated column catalog gains the revision table and column rows
- `crates/gcore/src/schema/assets.rs::*` — scope-reason: embed migration 376 and its checksum after immutable baseline 375
- `crates/gcore/src/schema/runner_tests.rs::*` — scope-reason: verify fresh and existing-hub application paths
- `src/gobby/storage/schema_expected_identity.json::*` — scope-reason: regenerated release-pinned identity advances latest version, latest checksum, and assets root hash
- `scripts/generate_schema_expected_identity.py`
- `crates/gcore/tests/schema_contract.rs::embedded_assets_publish_a_complete_schema_identity`
- `crates/gdaemon/tests/cli_contract.rs::version_json_reports_exact_schema_identity_contract`

Migration 376 creates singleton `config_state(id BOOLEAN PRIMARY KEY CHECK(id), revision
BIGINT NOT NULL)` with its single revision-zero row and adds `revision BIGINT NOT NULL
DEFAULT 0` to `config_store`. Grant `gobby_daemon_runtime` access to match the existing
`config_store` grant.

Keep `BASELINE_VERSION = 375`, `BASELINE_CHECKSUM`, and `baseline.sql` unchanged. Embed
376 as the first entry in `MIGRATIONS`; `runner.rs` enforces contiguity from
`BASELINE_VERSION + 1`. Retain `is_secret`; application writes derive it from the
registry.

Embedding a migration changes `root_hash()`, so regenerate the packaged schema identity
(`latest_version` 375 → 376, new `latest_checksum` and `assets_root_hash`) that
`gobby.storage.schema_contract` loads and `gobby install` asserts, and update both Rust
identity contract tests in the same change.

**Acceptance:**

- 1.2.1 - A fresh baseline-375 schema advances through migration 376. test: `crates/gcore/src/schema/runner_tests.rs::fresh_baseline_applies_reactive_config_migration`.
- 1.2.2 - An existing exact baseline-375 receipt advances to 376 without baseline checksum drift. test: `crates/gcore/src/schema/runner_tests.rs::existing_baseline_advances_to_376`.
- 1.2.3 - Migration reruns are receipt-safe and require no destructive authorization. test: `crates/gcore/src/schema/runner_tests.rs::reactive_config_migration_is_nondestructive`.
- 1.2.4 - Embedded assets and catalog describe the revision table and row column. file: `crates/gcore/assets/schema/catalog.manifest.json`.
- 1.2.5 - Regenerated schema identity reports latest version 376 and both identity contract tests pass. test: `crates/gdaemon/tests/cli_contract.rs::version_json_reports_exact_schema_identity_contract`.

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
- `tests/config/test_config_runtime.py`

Add frozen `ConfigSnapshot`, `ConfigChange`, `ApplyFailure`, prepared-subscriber
interfaces, and `ConfigRuntime`. Snapshots contain revision, typed desired and active
projections, row revisions, pending-restart keys, and failed-live keys.

A dedicated `psycopg.AsyncConnection` uses `PostgresHubDatabase.conninfo`, stays outside
the configured pool, executes `LISTEN gobby_config_changed`, and is owned by
`ConfigRuntime`'s async lifecycle. Local commits reconcile immediately. Remote
notifications, revision gaps, and reconnects trigger full atomic reloads. Database
restart causes bounded reconnect backoff followed by full reload before healthy status.

Live activation uses a local prepare/commit protocol: every matching subscriber builds
replacement state first; any preparation failure disposes prepared replacements and
preserves all previous active state. Successful preparation permits no-fail reference
swaps. Failure records remain until a later operator mutation changes the affected key.

Restart changes update desired state while retaining old active values. Managed changes
are rejected before persistence.

**Acceptance:**

- 1.4.1 - Readers observe immutable single-revision snapshots. test: `tests/config/test_config_runtime.py::test_snapshot_swap_is_atomic`.
- 1.4.2 - Restart writes separate desired and active state and report pending keys. test: `tests/config/test_config_runtime.py::test_restart_policy_tracks_pending_keys`.
- 1.4.3 - Subscriber preparation failure performs no swap and no database write. test: `tests/config/test_config_runtime.py::test_apply_failure_preserves_local_last_good_state`.
- 1.4.4 - A second runtime receives remote revisions over the pool-exempt listener. test: `tests/config/test_config_runtime.py::test_remote_runtime_receives_revision_notification`.
- 1.4.5 - Listener reconnect performs a full reload before health recovery. test: `tests/config/test_config_runtime.py::test_listener_reconnect_reloads_snapshot`.

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

**Acceptance:**

- 2.1.1 - Schema and values expose public registry metadata and masked desired/active state. test: `tests/servers/routes/test_config_values_api.py::test_public_schema_and_values_contract`.
- 2.1.2 - PATCH enforces CAS, path validation, per-key unset, and managed activation. test: `tests/servers/routes/test_config_values_api.py::test_public_patch_contract`.
- 2.1.3 - Public reads, errors, and events contain no secret plaintext. test: `tests/servers/routes/test_config_values_api.py::test_public_surfaces_redact_secrets`.
- 2.1.4 - Reset and caller-supplied `is_secret` are absent. test: `tests/servers/routes/test_config_values_api.py::test_legacy_reset_and_secrecy_flags_are_removed`.

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

### 2.3 Replace MCP configuration tools [category: code] (depends: 1.4)
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

### 2.4 Make YAML a validate-first daemon-namespace replacement [category: code] (depends: 1.4)
`kind: deliverable`

Targets:
- `src/gobby/config/documents.py`
- `src/gobby/servers/routes/configuration_templates.py::*` — scope-reason: replace delete-before-validate template saves
- `src/gobby/servers/routes/configuration_import_export.py::*` — scope-reason: replace wholesale deletion and mixed-domain export
- `tests/servers/routes/test_config_yaml_replace.py`

Parse and validate the complete candidate before mutation. Reject bootstrap, restricted,
unknown, and managed keys; restore masked secret references; resolve references for
validation; run full Pydantic cross-field validation; then issue one CAS replacement for
`namespace=daemon`.

Omitted daemon settings restore defaults. UI preferences, credentials, operational data,
domain records, and bootstrap remain untouched. Export desired daemon configuration with
masks/references. Prompt/domain bundles remain separate.

**Acceptance:**

- 2.4.1 - Invalid documents preserve rows, secrets, and revision. test: `tests/servers/routes/test_config_yaml_replace.py::test_invalid_document_has_no_side_effects`.
- 2.4.2 - Valid replacement changes only the daemon namespace in one revision. test: `tests/servers/routes/test_config_yaml_replace.py::test_daemon_replacement_is_scoped_and_atomic`.
- 2.4.3 - Omissions restore daemon defaults without clearing supplemental/domain state. test: `tests/servers/routes/test_config_yaml_replace.py::test_omissions_restore_only_daemon_defaults`.
- 2.4.4 - Export round-trips without plaintext secret disclosure. test: `tests/servers/routes/test_config_yaml_replace.py::test_masked_export_round_trip`.

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
- `web/src/hooks/__tests__/useConfiguration.revision.test.ts`
- `web/src/hooks/__tests__/useSettings.test.ts::*` — scope-reason: update persistence expectations
- `web/src/components/app/__tests__/useAppProjectSelection.test.tsx::*` — scope-reason: update project-selection persistence

Add one typed client carrying the latest revision. On 409, refetch server state, preserve
the unsaved local draft, and require explicit resubmission. Coalesce higher-revision
WebSocket events into one refetch. Remove specialized UI-setting, reset, launch-default,
and approval-setting writes.

**Acceptance:**

- 2.5.1 - Every browser mutation includes the current revision. test: `web/src/hooks/__tests__/useConfiguration.revision.test.ts::includes_revision_in_every_patch`.
- 2.5.2 - Conflict refresh preserves unsaved edits and requires resubmission. test: `web/src/hooks/__tests__/useConfiguration.revision.test.ts::preserves_draft_after_conflict`.
- 2.5.3 - UI preferences and project selection use universal paths. test: `web/src/hooks/__tests__/useSettings.test.ts::persists_settings_through_config_patch`.
- 2.5.4 - Higher WebSocket revisions trigger one coalesced refetch. test: `web/src/hooks/__tests__/useConfiguration.revision.test.ts::coalesces_config_revision_events`.

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
- Direct PostgreSQL fallback loads all machine-visible rows and revision in one
  repeatable-read snapshot.
- Registered runtime keys do not accept environment or `gcore.yaml` precedence.
- Secret references resolve only through the hub secret store.

Environment and `gcore.yaml` remain available only in explicit standalone mode with no
Gobby daemon/hub context. The gcode config module root is `crates/gcode/src/config.rs`;
both crates declare their new test submodule from the existing `config/tests.rs` file.

**Acceptance:**

- 2.6.1 - Generated Rust contract is byte-stable and current with the Python registry. test: `tests/config/test_runtime_config_contract.py::test_checked_in_contract_matches_registry`.
- 2.6.2 - Rust rejects machine keys absent from the generated contract. test: `crates/gcore/src/config/tests/runtime_contract.rs::rejects_unregistered_machine_key`.
- 2.6.3 - Gobby runtime mode ignores env/standalone precedence for registered keys. test: `crates/gcode/src/config/tests/runtime_contract.rs::gobby_mode_uses_registry_authority`.
- 2.6.4 - Direct hub fallback reads one revision-coherent snapshot and resolves secrets. test: `crates/gcode/src/config/tests/runtime_contract.rs::hub_fallback_reads_atomic_snapshot`.

## P3: Consumer Migration and Activation
`kind: framing`

### 3.1 Wire ConfigRuntime into startup [category: code] (depends: 1.4)
`kind: deliverable`

Targets:
- `src/gobby/runner.py::*` — scope-reason: add ConfigRuntime ownership and lifecycle
- `src/gobby/app_context.py::*` — scope-reason: add ConfigRuntime to the service context
- `src/gobby/runner_init/storage.py::init_storage_and_config`
- `tests/runner_init/test_config_runtime_startup.py`

Startup loads bootstrap topology, opens PostgreSQL, constructs the
registry/store/runtime, loads the initial snapshot, then starts notifications before
post-database services.

Add `config_runtime` to runner and `ServiceContainer`. Existing `config`, `config_store`,
and `runner.config_store` references remain only until their dependent migration leaves
complete; section 4.1 deletes them. No new consumer may use them.

**Acceptance:**

- 3.1.1 - Startup constructs exactly one ConfigRuntime before post-database services. test: `tests/runner_init/test_config_runtime_startup.py::test_startup_constructs_one_runtime`.
- 3.1.2 - Runner and ServiceContainer expose the same ConfigRuntime instance. test: `tests/runner_init/test_config_runtime_startup.py::test_context_shares_runner_runtime`.
- 3.1.3 - Runtime notification lifecycle closes cleanly with daemon shutdown. test: `tests/runner_init/test_config_runtime_startup.py::test_runtime_closes_with_daemon`.

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
- `tests/config/test_live_policy_consumers.py`

Read one typed snapshot per policy decision. Move global rules, approvals, launch
defaults, and UI preferences to generic PATCH. Preserve domain CRUD and per-domain
lifecycle behavior.

**Acceptance:**

- 3.2.1 - Rule evaluation observes live global toggles from one snapshot. test: `tests/config/test_live_policy_consumers.py::test_rules_use_runtime_snapshot`.
- 3.2.2 - Approval policy and launch defaults use typed registered paths. test: `tests/config/test_live_policy_consumers.py::test_approval_and_launch_defaults_are_registered`.
- 3.2.3 - Specialized setting writers disappear while domain CRUD remains. test: `tests/config/test_live_policy_consumers.py::test_only_specialized_setting_writers_are_removed`.

### 3.3 Separate restart-bound topology consumers [category: code] (depends: 3.1)
`kind: deliverable`

Targets:
- `src/gobby/runner_init/servers.py::*` — scope-reason: separate bootstrap topology from restart-class runtime settings
- `src/gobby/servers/app_factory.py::*` — scope-reason: construct middleware from the startup active snapshot
- `src/gobby/servers/_app_ui.py::*` — scope-reason: keep UI server lifecycle restart-bound
- `tests/config/test_restart_config_consumers.py`

Daemon/WS/UI ports, bind addresses, service bind address, daemon URL, database URL, and
pool sizing come from bootstrap. Auth mode, CORS, test mode, WebSocket enablement, UI
lifecycle, database services, telemetry, and memory backend come from the startup active
snapshot and do not mutate running topology.

**Acceptance:**

- 3.3.1 - Process topology reads only `BootstrapConfig`. test: `tests/config/test_restart_config_consumers.py::test_topology_uses_bootstrap_only`.
- 3.3.2 - Restart-class writes do not mutate running servers or middleware. test: `tests/config/test_restart_config_consumers.py::test_restart_changes_remain_pending`.
- 3.3.3 - Restart activates desired settings on the next startup snapshot. test: `tests/config/test_restart_config_consumers.py::test_restart_promotes_desired_to_active`.

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

**Acceptance:**

- 3.4.1 - Matching changes prepare all replacements before any swap. test: `tests/config/test_stateful_config_subscribers.py::test_prepare_precedes_every_swap`.
- 3.4.2 - Preparation failure disposes replacements and preserves all old services. test: `tests/config/test_stateful_config_subscribers.py::test_failed_prepare_keeps_last_good_services`.
- 3.4.3 - Successful swaps drain old in-flight clients. test: `tests/config/test_stateful_config_subscribers.py::test_successful_swap_drains_old_client`.
- 3.4.4 - API-key changes invalidate only dependent cached clients. test: `tests/config/test_stateful_config_subscribers.py::test_key_scoped_invalidation`.

### 3.5 Migrate loops and lifecycle consumers [category: code] (depends: 3.1, 3.4)
`kind: deliverable`

Targets:
- `src/gobby/runner_init/orchestration.py::*` — scope-reason: replace constructor-captured live configuration
- `src/gobby/runner_lifecycle_periodic.py::*` — scope-reason: read one snapshot per loop iteration
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

**Acceptance:**

- 3.6.1 - Generic interfaces reject structural embedding mutations. test: `tests/storage/test_embedding_switch_config_contract.py::test_structural_keys_require_switch`.
- 3.6.2 - Switch completion commits canonical values in one revision. test: `tests/ai/test_embedding_switch_daemon_lifecycle.py::test_switch_commit_is_one_revision`.
- 3.6.3 - Switch recovery reads ConfigRuntime instead of rebuilding configuration. test: `tests/ai/test_embedding_switch_daemon_lifecycle.py::test_switch_recovery_uses_runtime_snapshot`.
- 3.6.4 - API-key rotation is live and invalidates the embedding client. test: `tests/storage/test_embedding_switch_config_contract.py::test_api_key_rotation_is_live`.

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

After auth-owned consumers have migrated through the coordinated auth tasks, delete:

- `load_config`
- `load_full_config_from_db`
- Raw ConfigStore get/set/batch/delete/reset/default APIs
- `runner.config`, `runner.config_store`
- `ServiceContainer.config`, `ServiceContainer.config_store`
- Specialized setting writers
- Legacy MCP configuration tools

Add a Python AST audit plus generated Rust-contract checks. The audit rejects raw Python
reads/writes, loader imports/re-exports, stale mutable config fields, specialized writer
routes, unregistered production keys, registry-pattern gaps, generated-contract drift, and
Gobby-mode Rust env/standalone precedence.

Update operator documentation for public, machine, bootstrap, revision, activation,
secret, YAML, and embedding contracts.

**Acceptance:**

- 4.1.1 - Python runtime code contains no alternate configuration authority or raw dotted access. test: `tests/config/test_config_authority_audit.py::test_python_runtime_has_one_config_authority`.
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
apply failure isolation, listener backend termination/reconnect, and latest-snapshot
convergence.

Use isolated ports/state and never contact the user's running daemon.

**Acceptance:**

- 4.2.1 - A write through runtime A updates runtime B through PostgreSQL notification. test: `tests/integration/config/test_reactive_config_multi_daemon.py::test_remote_daemon_converges_after_commit`.
- 4.2.2 - Forced listener termination reconnects and reloads the latest revision. test: `tests/integration/config/test_reactive_config_multi_daemon.py::test_listener_restart_recovers_latest_snapshot`.
- 4.2.3 - Apply failure in one process changes neither committed desired state nor the other process's active state. test: `tests/integration/config/test_reactive_config_multi_daemon.py::test_apply_failure_is_process_local`.

## V1 Plan Changelog
`kind: verification`

- Initial draft established registry, CAS, runtime, interface, consumer, and cutover
  phases.
- Review repair round 1:
  - Replaced phase dependencies with concrete leaf dependencies.
  - Preserved authenticated effective-config and service-capability contracts.
  - Added Rust registry generation and Gobby-mode precedence removal.
  - Added every uncovered loader re-export and caller.
  - Replaced baseline editing with non-destructive migration 376.
  - Specified the pool-exempt notification connection and remote-daemon consumer.
  - Removed compensating rollback in favor of local prepare/commit.
  - Split topology, stateful subscribers, loops, loader groups, and integration testing.
  - Named the companion coverage ledger and concrete module decomposition.
- Review repair round 2 (index-verified):
  - 1.2 gained the schema-identity fan-out — `schema_expected_identity.json`,
    `scripts/generate_schema_expected_identity.py`, and both Rust identity contract
    tests — plus acceptance item 1.2.5. Embedding migration 376 changes `root_hash()`,
    which `gobby install` and two `cargo test` targets assert exactly.
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
- No enhancement or adversarial-review round has run.

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

Rust and embedded-schema validation. Regenerate the schema identity before the Rust
contract tests, because migration 376 changes the assets root hash:

```bash
uv run python scripts/generate_schema_expected_identity.py
uv run python scripts/generate_runtime_config_contract.py --check
cargo test -p gobby-core schema::runner_tests
cargo test -p gobby-core config::tests::runtime_contract
cargo test -p gobby-core ai::effective_config
cargo test -p gobby-core --test schema_contract
cargo test -p gobby-code config::
cargo test -p gobby-daemon --test cli_contract
cargo build --release -p gobby-daemon -p gobby-code
```

Run focused Ruff, mypy, web Vitest, and web type-check commands for touched paths. Smoke
the newly built `gdaemon` against an isolated temporary schema and confirm migration 376
applies; do not reinstall or restart the user's daemon during automated validation.
