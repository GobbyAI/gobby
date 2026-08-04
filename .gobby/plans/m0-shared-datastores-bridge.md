# M0 Bridge: Shared Datastores, Per-Machine Daemons

**Plan ID:** m0-shared-datastores-bridge

## Overview
`kind: framing`

Run the Docker datastores (PostgreSQL/Qdrant/FalkorDB) on the always-on AMD Linux box,
exposed on the tailnet; every client machine runs a full local Gobby daemon pointed at
the shared datastores. Tasks, memory, and session metadata become shared across
machines; execution, hooks, worktrees, and transcripts stay machine-local because each
machine keeps its own daemon. This is the bridge milestone (M0) of
`.gobby/plans/shared-remote-stack.md` (root epic #17488): it delivers the pack-up-the-
laptop-and-resume-elsewhere scenario for task/memory continuity before the worker
architecture (#17435/#17437/#17436) lands. The machine-scoping work is a genuine
pull-forward of #17437.

Client one-time setup: copy `~/.gobby/.secret_kek` and `~/.gobby/local_cli_token` from
the hub install (shared wrapped-DEK; shared `auth.api_token_hash` in config_store).

## Constraints
`kind: framing`

- 0.5.0 unshipped: no backward-compatibility shims; one-shot migrations are fine.
- Default behavior unchanged: `datastore_mode` absent means `local`, which is exactly
  today's semantics including the loopback-only `database_url` validation.
- The tailnet is the trust boundary: Tailscale ACLs restricting the three datastore
ports to Josh's devices are a documented requirement. Qdrant API key is explicitly
deferred (config plumbing exists Rust-side when needed), so datastore exposure is
limited to loopback or one concrete Tailscale interface address; wildcard binds are
refused rather than exposing unauthenticated Qdrant outside that boundary.
- Paths stay opaque strings; no daemon-host normalization of another machine's paths.
- M0 client machines should use identical project checkout paths (`/Users/josh/...` on
  both Macs); `projects.repo_path` is intentionally untouched until #17435/#17437.
- Hook machine_id fallback bugs (`_session_start/flow.py:445,791`,
  `hooks/session_lookup.py:214`, `_session_end.py:30`) are dormant in M0 because hooks
  only ever POST to the machine-local daemon; they are early-#17435 scope, not M0.
- Ordering invariant (second-machine safety barrier): 2.1, 2.2, 2.3, 2.4, and 3.1
  must all land and validate before a second machine points at the shared database.
  Without 2.2-2.4 a second daemon can kill the first machine's agent runs, process
  its transcripts, delete its workspace records, and fail its in-flight cron runs.
  The 4.1 e2e setup and the 3.2 runbook order follow this same barrier.

## P1: Remote datastore topology
`kind: framing`

**Goal**: A client daemon can start against remote datastores; the hub can expose its
Docker stack on the tailnet without hand-editing generated files.

### 1.1 Add datastore_mode bootstrap key and remote-DSN support [category: code]
`kind: deliverable`

Targets:
- `src/gobby/config/bootstrap.py::BootstrapConfig`
- `src/gobby/config/bootstrap.py::BootstrapConfig.to_config_dict`
- `src/gobby/config/bootstrap.py::load_bootstrap`
- `src/gobby/config/bootstrap.py::_validate_managed_database_url`
- `src/gobby/config/app.py::DaemonConfig`
- `src/gobby/cli/daemon.py::start`
- `src/gobby/cli/daemon.py::stop`
- `src/gobby/cli/daemon.py::restart`
- `src/gobby/cli/daemon.py::_do_stop`
- `src/gobby/cli/installers/compose_env.py::resolve_compose_runtime`
- `src/gobby/install/shared/config/bootstrap.yaml::*` — scope-reason: add the datastore topology key to the installed bootstrap template
- `src/gobby/install/bundled_content_manifest.json::*` — scope-reason: regenerate the authenticated bundled-content inventory after changing the installed bootstrap template
- `crates/gcore/src/bootstrap.rs::*` — scope-reason: add an adjacent parser-tolerance test for the new bootstrap key

Add top-level bootstrap key `datastore_mode: Literal["local", "remote"] = "local"`:

- Parse in `load_bootstrap()`: absent → `local`; unknown value → hard
  `BootstrapConfigError` (fail loud). Emit in `to_config_dict()` so `DaemonConfig`
  carries it.
- `_validate_managed_database_url` (`bootstrap.py:200`) runs only when
  `datastore_mode == "local"`. In `remote` mode still require `postgres(ql)://` scheme
  and a full DSN with credentials and hostname; loopback hostnames in remote mode are a
  hard error pointing the user back to `local` (a mode that lies about topology is
  worse than a failed parse).
- `gobby start` skips `_services_start` and `gobby stop` skips `_services_stop` in
  remote mode — a client machine must never run compose against a stack it does not
  own. The skip lives in the shared helpers (`_do_stop`, `_services_start`), not only
  in the public `start`/`stop` commands, because `restart` calls `_do_stop` directly
  and bypasses the public stop wrapper. Any Docker/compose dependency validation on
  the startup path is topology-aware: remote mode requires no Docker at all. Log one
  line stating remote mode and the target host.
- `compose_env.resolve_compose_runtime` raises a clear "this machine is in
  datastore_mode: remote; compose management runs on the hub" error in remote mode.
- Document the key in the installed template
  `src/gobby/install/shared/config/bootstrap.yaml`, regenerate
  `src/gobby/install/bundled_content_manifest.json`, and run the existing bundled-tree
  integrity check against the committed inventory.
- `crates/gcore/src/bootstrap.rs`: confirm unknown-key tolerance (serde ignores
  unknown fields); add a `reads_bootstrap_with_datastore_mode` unit test mirroring
  `reads_bootstrap_with_auth_mode`.

The daemon startup readiness gate (`runner_service_readiness.py:26`) needs no change:
it already reads endpoints from `runner.config.databases.*`, which in M0 point at the
tailnet host via config_store (see 1.2).

**Acceptance:**

- 1.1.1 - `datastore_mode` parses with `local` default and errors on unknown values. test: `tests/config/test_bootstrap.py::test_datastore_mode_parsing`.
- 1.1.2 - Remote mode accepts a non-loopback postgresql DSN that local mode rejects. test: `tests/config/test_bootstrap.py::test_remote_mode_allows_nonloopback_database_url`.
- 1.1.3 - Remote mode rejects loopback DSNs with an actionable error. test: `tests/config/test_bootstrap.py::test_remote_mode_rejects_loopback_database_url`.
- 1.1.4 - `gobby start`/`gobby stop` skip compose management in remote mode. test: `tests/cli/test_daemon_remote_mode.py::test_start_skips_services_in_remote_mode`.
- 1.1.5 - `datastore_mode` flows bootstrap → DaemonConfig. symbol: `gobby.config.bootstrap.BootstrapConfig`.
- 1.1.6 - Rust bootstrap reader tolerates the new key. test: `crates/gcore/src/bootstrap.rs` unit test `reads_bootstrap_with_datastore_mode`.
- 1.1.7 - `gobby restart` in remote mode performs no compose interaction and requires no Docker (shared `_do_stop`/start path, not just the public commands). test: `tests/cli/test_daemon_remote_mode.py::test_restart_skips_services_in_remote_mode`.
- 1.1.8 - The regenerated bundled-content manifest authenticates the edited bootstrap template and matches a fresh bundled-tree inventory. test: `tests/install/test_bundled_content_manifest.py::test_bundled_content_manifest_matches_tree`.

### 1.2 Compose bind-address knob, published host, and gobby datastores expose [category: code]
`kind: deliverable`

Targets:
- `src/gobby/data/docker-compose.services.yml::*` — scope-reason: parameterize datastore port bindings across all three compose services
- `crates/gcore/assets/docker-compose.services.yml::*` — scope-reason: keep the embedded compose template byte-identical with the Python template
- `src/gobby/cli/installers/compose_env.py::resolve_compose_runtime`
- `src/gobby/cli/installers/falkor.py::_update_config`
- `src/gobby/cli/installers/falkor.py::_refresh_unified_compose`
- `src/gobby/cli/installers/qdrant.py::_update_config`
- `src/gobby/cli/installers/qdrant.py::install_qdrant`
- `src/gobby/cli/installers/postgres.py::install_postgres`
- `src/gobby/cli/installers/managed_services_lock.py`
- `src/gobby/cli/__init__.py::*` — scope-reason: root CLI groups are imported and registered here; the new datastores group must be added to the registry
- `src/gobby/cli/daemon.py::start`
- `src/gobby/cli/daemon.py::_services_start`
- `src/gobby/cli/daemon.py::_services_stop`
- `src/gobby/cli/datastores.py`
- `src/gobby/config/bootstrap.py::BootstrapConfig`

Parameterize the hub-side bind address and make exposure survive reinstalls:

- Both compose template copies (they must stay byte-identical): change every hardcoded
  `"127.0.0.1:${PORT}:..."` binding for postgres, qdrant (HTTP + gRPC), and falkordb to
  `"${GOBBY_SERVICES_BIND_ADDRESS:-127.0.0.1}:${PORT}:..."`. The FalkorDB browser port
  stays loopback-only.
- The hub-side compose bind address must be readable before PostgreSQL is up
  (config_store lives inside PostgreSQL, and compose needs the bind address to start
  PostgreSQL — storing it only in config_store strands the hub on loopback after a
  container stop, container loss, or host reboot). Persist the bind address as a
  hub-machine-local bootstrap key `services_bind_address` in `~/.gobby/bootstrap.yaml`;
  `resolve_compose_runtime` injects `GOBBY_SERVICES_BIND_ADDRESS` from that bootstrap
  key (default `127.0.0.1`); process-env override continues to work because
  compose_env merges `canonical | dict(os.environ)`. The client-facing dial endpoint
  `databases.published_host` (DNS name clients dial, e.g. `gobby-box.tailnet.ts.net`)
  stays in shared config_store — clients read it after PostgreSQL is reachable, so no
  bootstrap-order hazard exists on that side.
- The public `gobby start` cold-start path loads `BootstrapConfig` before constructing
  `CliRuntime` or resolving DB-backed `DaemonConfig`. In local mode it performs
  topology-aware dependency checks and `_services_start` from bootstrap-only values,
  waits for PostgreSQL, and only then opens runtime configuration and applies
  migrations. Remote mode skips the local compose step and proceeds directly to the
  remote database path.
- `install_falkordb._update_config` (`falkor.py:158-163`) and the qdrant installer's
  `_update_config` currently hardcode `127.0.0.1`/`http://localhost:6333` into the
  SHARED config_store — a hub reinstall would clobber every client's endpoints. Both
  must derive host/url from `databases.published_host`, falling back to localhost when
  unset.
- New hub-side command `gobby datastores expose --bind <addr> --host <name>`, ordered
so a failure cannot leave clients pointed at unreachable endpoints: (1) validate
inputs (`--bind` accepts IPv4 loopback or a concrete local Tailscale IPv4 interface
address and rejects IPv6 and wildcard addresses; `--host` accepts a DNS name and
rejects IP literals while Qdrant authentication is deferred); (2) stage: write the
machine-local bootstrap bind key and re-run compose up with the candidate binding;
(3) health-check all three services on the new binding;
(4) commit: only then rewrite the shared dial endpoints — `databases.published_host`,
`databases.qdrant.url` → `http://<host>:<qdrant_port>`, `databases.falkordb.host` →
`<host>` — and reassert the `unless-stopped` restart policy. On any staging or
health-check failure, restore the prior bind value and compose state and leave the
shared endpoint keys untouched. Idempotent.
- One hub-machine-local managed-services lifecycle lock, with a 30-second acquisition
  timeout and actionable holder diagnostics, encloses every conflicting transition:
  bootstrap snapshot/write, compose stage/start/stop/installer refresh, health checks,
  atomic endpoint `set_many`, and rollback. `expose`, local `_services_start` and
  `_services_stop`, and datastore installer refreshes all acquire this same lock.
  Re-entrant calls share the held lock context; they never take a second lock.
- No Qdrant API key in this deliverable (deferred; documented in 3.2). No hand edits to
  the installed compose file are ever required — the knob lives in env/config_store, so
  `_refresh_unified_compose` (`falkor.py:118`) template overwrites are harmless.

**Acceptance:**

- 1.2.1 - Both compose templates use the bind-address variable and remain identical. test: `tests/cli/test_compose_bind_address.py::test_templates_parameterized_and_identical`.
- 1.2.2 - `resolve_compose_runtime` injects `GOBBY_SERVICES_BIND_ADDRESS` from `BootstrapConfig.services_bind_address` without opening PostgreSQL. symbol: `gobby.cli.installers.compose_env.resolve_compose_runtime`.
- 1.2.3 - FalkorDB/Qdrant installers derive config endpoints from `databases.published_host` and no longer hardcode localhost. test: `tests/cli/test_compose_bind_address.py::test_installers_respect_published_host`.
- 1.2.4 - `gobby datastores expose` persists keys, rewrites endpoint config, and is idempotent; IPv4 loopback, concrete local Tailscale IPv4, and DNS published hosts are accepted, while IPv6 bind/published-host literals and both wildcards are refused before configuration or compose state changes. test: `tests/cli/test_datastores_expose.py::test_expose_sets_keys_and_endpoints`.
- 1.2.5 - Expose command exists and is registered through the root CLI group in `src/gobby/cli/__init__.py`; `gobby datastores expose --help` resolves via CliRunner. test: `tests/cli/test_datastores_expose.py::test_expose_registered_at_root`.
- 1.2.6 - Cold start after exposure: with PostgreSQL down, the public `gobby start` command loads bootstrap first, revives PostgreSQL on the tailnet address, then resolves DB-backed runtime configuration, proven by an expose → stop → public-start sequence. test: `tests/cli/test_datastores_expose.py::test_cold_start_reads_bind_from_bootstrap`.
- 1.2.7 - Expose failure injection (compose failure or readiness timeout) restores the prior bind/compose state and leaves shared endpoint keys unchanged. test: `tests/cli/test_datastores_expose.py::test_expose_failure_restores_prior_state`.
- 1.2.8 - Deterministic interleavings of expose/expose, expose/start, expose/stop, and expose/installer refresh serialize under one bounded lifecycle lock; a losing transition cannot roll back or commit over the winner. test: `tests/cli/test_datastores_expose.py::test_managed_services_transitions_are_serialized`.
- 1.2.9 - Address validation covers IPv4 loopback, IPv6 loopback, Tailscale IPv4, Tailscale IPv6, `0.0.0.0`, `::`, DNS hosts, and IP-literal published hosts, with accepted values round-tripped through Compose and endpoint serialization. test: `tests/cli/test_datastores_expose.py::test_expose_address_contract`.

### 1.3 Remote-mode gobby install with reachability preflight [category: code] (depends: 1.1, 1.2)
`kind: deliverable`

Targets:
- `src/gobby/cli/install.py::install`
- `src/gobby/cli/_install_daemon.py::_run_install_preflight`
- `src/gobby/cli/installers/remote_preflight.py`
- `src/gobby/cli/installers/postgres.py::install_postgres`
- `src/gobby/cli/installers/qdrant.py::install_qdrant`
- `src/gobby/cli/installers/falkor.py::install_falkordb`

`gobby install` on a machine whose bootstrap.yaml says `datastore_mode: remote`:

- Skips postgres/qdrant/falkordb provisioning and all compose interaction. In
  particular `install_postgres` must not take its existing-bootstrap branch
  (`postgres.py:253-270`) that reuses the remote `database_url` as local compose
  credentials — that would provision a pointless local Postgres seeded with the hub's
  credentials.
- Resolves `datastore_mode` BEFORE `_run_install_preflight` runs and selects a remote
  preflight profile that omits the Docker-daemon and compose checks entirely — the
  current full-install preflight hard-requires a running Docker daemon
  (`_install_daemon.py::_run_install_preflight`), which would fail a Docker-free
  client before any remote logic executes.
- The remote flow lives in a new focused helper module
  `src/gobby/cli/installers/remote_preflight.py`; `install.py` (at 939 lines, 61 below
  the 1,000-line ceiling) stays thin orchestration and must remain below the ceiling
  after this change.
- Runs a remote reachability preflight instead: connect to `database_url`
  (`SELECT 1`), read `databases.qdrant.url` / `databases.falkordb.*` from the shared
  config_store, then Qdrant health check and authenticated FalkorDB PING. Failures
  produce per-service errors naming the expected source of truth (hub `gobby
  datastores expose`, Tailscale ACLs, copied `.secret_kek` for the FalkorDB
  `$secret:` password).
- Every probe is bounded: 3-second connect timeout, 5-second query/read timeout, and
  one 15-second overall preflight deadline covering DNS and all three services. M0
  performs no automatic retries. Timeout or cancellation closes every partially
  opened PostgreSQL, Qdrant, and FalkorDB client and reports the timed-out service and
  phase. Refused, dropped, half-open, stalled-response, authentication-failed, and
  slow-success paths are deterministic test cases.
- Detects missing `~/.gobby/.secret_kek` / `~/.gobby/local_cli_token` and prints the
  copy-from-hub instruction rather than generating fresh ones (a fresh KEK cannot
  unwrap the shared DEK; a fresh token would not match the shared hash — reuse, never
  rotate, from the client side).
- Existing local-mode install behavior is untouched.

**Acceptance:**

- 1.3.1 - Remote-mode install performs no compose/provisioning calls. test: `tests/cli/test_cli_install.py::test_remote_mode_skips_datastore_provisioning`.
- 1.3.2 - Preflight failure for each of the three services yields an actionable per-service error. test: `tests/cli/test_cli_install.py::test_remote_mode_preflight_errors`.
- 1.3.3 - Missing KEK/token files produce copy-from-hub guidance and never regenerate. test: `tests/cli/test_cli_install.py::test_remote_mode_kek_token_guidance`.
- 1.3.4 - Remote-mode install succeeds through preflight with the Docker executable and daemon entirely unavailable. test: `tests/cli/test_cli_install.py::test_remote_mode_install_without_docker`.
- 1.3.5 - `src/gobby/cli/install.py` and the new helper module are each below the 1,000-line ceiling after the change. behavior: "install.py remains thin orchestration under the line ceiling" in `src/gobby/cli/install.py`.
- 1.3.6 - Remote preflight enforces the 3-second connect, 5-second operation, and 15-second overall deadlines with zero retries and client cleanup across refused, dropped, half-open, stalled, authentication-failed, and slow-success cases. test: `tests/cli/test_cli_install.py::test_remote_mode_preflight_deadlines`.

## P2: Machine scoping for shared-database safety
`kind: framing`

**Goal**: Two daemons on one database cannot damage each other's filesystem-backed
state or lifecycle.

### 2.1 Migration: machine_id on worktrees, clones, agent_runs, cron_runs [category: code]
`kind: deliverable`

Targets:
- `src/gobby/storage/migrations/372_machine_scope.sql`
- `src/gobby/storage/postgres_baseline_schema.sql`
- `src/gobby/storage/worktrees.py::*` — scope-reason: update the model, writer, row mapping, and machine-scoped uniqueness surfaces together
- `src/gobby/storage/clones.py::*` — scope-reason: update the model, writer, row mapping, and machine-scoped path surfaces together
- `src/gobby/storage/agents/_manager.py::*` — scope-reason: stamp machine ownership across agent-run creation paths
- `src/gobby/storage/agents/_lifecycle.py::*` — scope-reason: the agent_runs INSERT lives here, behind the _manager facade; machine_id must be written at insert
- `src/gobby/storage/agents/_models.py::*` — scope-reason: agent-run dataclasses gain machine_id with from_row and serialization round-trip
- `src/gobby/storage/cron_models.py::*` — scope-reason: CronRun model, from_row, and public serialization gain machine_id
- `src/gobby/storage/cron_runs.py::*` — scope-reason: add machine ownership to the cron-run model and creation paths
- `src/gobby/agents/isolation_worktree.py::*` — scope-reason: propagate local machine ownership through worktree isolation setup
- `src/gobby/agents/isolation_clone.py::*` — scope-reason: propagate local machine ownership through clone isolation setup

The migration file is new. Slot allocation (serialized revalidation 2026-08-03):
slots 354-371 are occupied on disk (369_scoped_agent_authorization.sql,
370_runtime_function_execute.sql, and 371_managed_credential_lifecycle.sql);
this M0 leaf reserves slot **372**. This plan revision is the serialized
reservation; the provider-capability migration remains unassigned until the
next serialized slot decision. Independent "next free number" probing is
prohibited. Sequencing: this leaf gates #19422 → #19423
(filename-aware bookkeeping) → #19424 (flatten migrations into the regenerated
baseline), so it lands as a numbered pre-flatten migration by construction;
after #19424 the rule is NO new numbered migrations until 0.5.0 ships — were
any M0 schema remnant to land post-flatten, it goes directly into the
regenerated baseline instead.
Prerequisite: gcore-schema-authority 2.18/2.19 (machines.id UUID PK;
sessions.machine_id nullable UUID FK) are applied before this migration
runs — machine scoping is UUID-native from the start.

Schema changes (baseline + migration kept in sync per the repo's migration-contract
rules):

- `worktrees`: ADD `machine_id UUID NOT NULL REFERENCES machines(id)`. Backfill
only from the authoritative UUID-keyed `sessions.machine_id` join via
`agent_session_id`; an unresolved or NULL-machine owner aborts the
migration with diagnostics instead of guessing.
Replace `idx_worktrees_path` UNIQUE(worktree_path) → UNIQUE(machine_id,
worktree_path); replace `idx_worktrees_branch` UNIQUE(project_id, branch_name) →
UNIQUE(project_id, branch_name, machine_id). Branch uniqueness is per-machine
because local git branches do not sync between machines.
- `clones`: ADD `machine_id UUID NOT NULL REFERENCES machines(id)` (same
authoritative-session backfill and fail-closed handling); `idx_clones_path` →
UNIQUE(machine_id, clone_path).
- `agent_runs`: ADD `machine_id UUID NOT NULL REFERENCES machines(id)` (same
authoritative-session backfill and fail-closed handling); new index
`(machine_id, status)`.
- `cron_runs`: ADD `machine_id UUID NOT NULL REFERENCES machines(id)`; backfill
through the run's authoritative session ownership and abort on an unresolved
row.

Legacy-row conversion matrix (the backfill must terminate successfully on every
legal pre-M0 row shape, verified against the current baseline schema):

- `worktrees`/`clones`: `agent_session_id` is nullable (`ON DELETE SET NULL`),
  so detached workspace rows are legal. Linked rows backfill through
  `sessions.machine_id`; rows with NULL or unresolvable `agent_session_id` (or
  a NULL-machine session) abort the migration with per-row diagnostics.
- `agent_runs`: resolve through `child_session_id`, falling back to
  `parent_session_id`; unresolvable rows abort with diagnostics.
- `cron_runs`: the table has NO session linkage (only nullable `agent_run_id`,
  `pipeline_execution_id`, and a `scheduler_owner` string), so a pure
  session-join backfill cannot resolve ordinary historical rows. Resolve
  through the agent-run or pipeline child when linked; the migration requires
  drained cron state (no pending/running rows — abort listing them otherwise);
  remaining terminal telemetry rows follow the operator disposition below.
- Remediation path (documented in the 3.2 runbook, never guessed in-migration):
  the abort diagnostics name every unresolved row; the operator either deletes
  stale workspace/telemetry rows or assigns them to the pre-M0 hub machine via
  a runbook-provided SQL snippet (historically truthful in a single-daemon
  database), then re-runs the migration to success. Rollback on abort leaves
  the schema untouched.

Writers stamp `get_machine_id()` at creation: worktree create
(`src/gobby/storage/worktrees.py:118`), clone create (`src/gobby/storage/clones.py:137`),
agent_run create (INSERT in `src/gobby/storage/agents/_lifecycle.py:114`, reached
through the `_manager.py` facade), cron run create
(`src/gobby/storage/cron_runs.py:34`, scheduler passes it). Dataclasses (`Worktree`,
clone and agent-run models) gain the field.

**Acceptance:**

- 2.1.1 - Migration adds `UUID NOT NULL REFERENCES machines(id)` columns, swaps unique indexes, backfills only through the per-table conversion matrix, and aborts with row diagnostics rather than assigning a guessed or sentinel machine when ownership cannot be resolved. file: `src/gobby/storage/migrations/372_machine_scope.sql`.
- 2.1.2 - Baseline schema matches the migrated shape. file: `src/gobby/storage/postgres_baseline_schema.sql`.
- 2.1.3 - Same (project, branch) worktree can exist for two machine_ids; same path string can exist for two machine_ids. test: `tests/storage/test_worktrees.py::test_worktree_uniqueness_is_machine_scoped`.
- 2.1.4 - Worktree/clone/agent-run/cron-run creation stamps the local machine_id, and the models round-trip it through from_row and serialization. test: `tests/storage/test_machine_scope_writers.py::test_creation_paths_stamp_machine_id`.
- 2.1.5 - Every legal legacy row shape is covered: linked rows convert; NULL-session worktrees/clones, unresolvable agent_runs, undrained cron state, and ownerless terminal cron rows each abort with row diagnostics; the documented remediation then yields a successful rerun. test: `tests/storage/test_machine_scope_migration.py::test_legacy_shapes_convert_or_abort_with_remediation`.

### 2.2 Scope agent lifecycle and workspace readers by machine_id [category: code] (depends: 2.1)
`kind: deliverable`

Targets:
- `src/gobby/storage/agents/_queries.py::*` — scope-reason: machine-scope every lifecycle query that can inspect local process state
- `src/gobby/storage/agents/_cleanup.py::*` — scope-reason: machine-scope stale-running and stale-pending SQL
- `src/gobby/storage/agents/_termination.py::*` — scope-reason: machine-scope pending termination candidate selection
- `src/gobby/agents/agent_cleanup.py::*` — scope-reason: pass the local machine through stale cleanup call-throughs
- `src/gobby/agents/lifecycle_monitor.py::*` — scope-reason: thread the local machine id through all lifecycle query call sites
- `src/gobby/agents/agent_health.py::*` — scope-reason: health probes are local-resource consumers
- `src/gobby/agents/idle_check_handler.py::*` — scope-reason: idle and restart reconciliation is local-resource work
- `src/gobby/agents/local_model.py::*` — scope-reason: local capacity counts use the machine-scoped active API
- `src/gobby/agents/task_recovery.py::*` — scope-reason: recovery may inspect local tmux and workspace state
- `src/gobby/runner_lifecycle_agents.py::*` — scope-reason: replay and memory-watchdog lifecycle work is local-machine-only
- `src/gobby/servers/websocket/tmux.py::*` — scope-reason: tmux streaming is a local-resource consumer
- `src/gobby/build/control_runtime.py::*` — scope-reason: build control deliberately retains global active-run visibility through the explicit global API
- `src/gobby/build/observability.py::*` — scope-reason: build observability deliberately retains global active-run visibility through the explicit global API
- `src/gobby/mcp_proxy/tools/agents_query_tools.py::*` — scope-reason: metadata queries use the explicit global API
- `src/gobby/mcp_proxy/tools/spawn_agent/_spawn_guards.py::*` — scope-reason: task-level spawn invariants remain global
- `src/gobby/servers/routes/agents.py::*` — scope-reason: shared agent metadata routes use the explicit global API
- `src/gobby/storage/tasks/_transitions.py::*` — scope-reason: task transition checks must see active runs on every machine
- `src/gobby/storage/worktrees.py::*` — scope-reason: machine-scope claim, lookup, stale discovery, and cleanup surfaces
- `src/gobby/storage/clones.py::*` — scope-reason: machine-scope claim, lookup, stale discovery, and cleanup surfaces
- `src/gobby/dispatch/daemon_resume.py::*` — scope-reason: restrict daemon-stop resume candidates to local tmux and workspace state
- `src/gobby/runner_maintenance/isolation.py::*` — scope-reason: the recurring isolation maintenance loop deletes expired workspaces and prunes records whose paths are missing on local disk; both sweeps must be local-machine-only
- `src/gobby/hooks/event_handlers/_misc.py::MiscEventHandlerMixin.handle_worktree_remove`

Every reader that inspects local processes/tmux or touches the local filesystem must
see only rows owned by this machine:

- Replace ambiguous `list_active` with two explicit APIs:
  `list_active_for_machine(machine_id, ...)` for local-resource work and
  `list_active_global(...)` for shared metadata invariants. Health, lifecycle,
  memory-watchdog/replay, idle/restart, websocket tmux, local capacity, and task
  recovery callers use the machine-scoped API. Build control/observability, shared
  agent metadata, spawn/task guards, and task-transition checks use the global API.
  No default or compatibility alias remains. Same machine scoping applies to
  `cleanup_stale_running_runs`, `cleanup_stale_pending_runs`,
  `list_termination_candidates`, `reconcile_pending_terminations`,
  `refresh_active_run_dispatch_mutexes`, and `list_daemon_stop_resume_candidates`
  (consumed by `dispatch/daemon_resume.py:105` — resume metadata references local
  tmux sessions and paths).
- Storage protocols, host protocols, and test fakes gain the same explicit local/global
  signatures. Remote rows receive no process probe, tmux lookup, termination attempt,
  stale transition, or cleanup mutation.
- Worktree/clone stale finders and cleanup (`storage/worktrees.py:472,506,541`;
  `storage/clones.py:512,554,602`) filter `machine_id = local`; filesystem deletion
  paths (including hook-triggered deletion at `hooks/event_handlers/_misc.py:367`)
  refuse rows whose machine_id differs from the local machine.
- The recurring isolation maintenance loop (`runner_maintenance/isolation.py`):
  expired-workspace cleanup and the missing-path record sweeps
  (`_delete_missing_worktree_records`/`_delete_missing_clone_records`) operate only
  on `machine_id = local` rows — another machine's paths are always "missing" on
  this disk, and an unscoped sweep deterministically deletes the other daemon's
  live workspace records.
- Claim/reuse/path-lookup surfaces (`claim`, `claim_if_available`, `get_by_path`,
  `get_by_branch`) are machine-scoped so daemon B never claims or reuses A's
  workspace records.
- Time-based session pause/expire (`storage/session_lifecycle.py::pause_inactive_active_sessions` and `::expire_stale_sessions`) deliberately
  stays global: pausing a stale session belonging to the asleep machine is correct
  and keeps the pack-up scenario clean. Add a comment pinning this decision.
- `src/gobby/agents/lifecycle_monitor.py` is 967 lines before this leaf. Check its
  projected edit before implementation and keep every touched production source below
  1,000 lines; decompose reconciliation into a focused sibling module inside this leaf
  if the projection or result reaches the ceiling.

**Acceptance:**

- 2.2.1 - Lifecycle monitor queries are machine-scoped. symbol: `gobby.storage.agents._queries.list_active`.
- 2.2.2 - A running agent row owned by another machine is never marked dead/stale by the local monitor. test: `tests/agents/test_lifecycle_monitor.py::test_monitor_ignores_other_machines_runs`.
- 2.2.3 - Stale-cleanup and deletion refuse other machines' worktree/clone rows. test: `tests/storage/test_worktrees.py::test_cleanup_scoped_to_local_machine`.
- 2.2.4 - Claim/reuse never selects another machine's workspace records. test: `tests/storage/test_worktrees.py::test_claim_scoped_to_local_machine`.
- 2.2.5 - Two-daemon regression: B's isolation maintenance (expired cleanup and missing-path sweeps) leaves A's rows and filesystem untouched. test: `tests/runner_maintenance/test_isolation_machine_scope.py::test_missing_path_sweep_ignores_remote_rows`.
- 2.2.6 - Stale-running, stale-pending, and pending-termination sweeps pass local machine identity through storage and cleanup orchestration; remote rows receive no probe or mutation. test: `tests/agents/test_lifecycle_monitor.py::test_cleanup_and_termination_ignore_other_machine_runs`.
- 2.2.7 - Every active-run caller is pinned to an explicit machine-scoped or global API, with cross-machine regressions proving local consumers ignore remote resources and build/task invariants retain global visibility. test: `tests/storage/agents/test_active_run_scope.py::test_local_and_global_active_run_apis`.
- 2.2.8 - `src/gobby/agents/lifecycle_monitor.py` and every new focused sibling remain below the 1,000-line ceiling. behavior: "lifecycle machine scoping stays below the production line ceiling" in `src/gobby/agents/lifecycle_monitor.py`.

### 2.3 Scope session background consumers to local-machine sessions [category: code]
`kind: deliverable`

Targets:
- `src/gobby/storage/sessions/_transcript.py::_TranscriptMixin.get_pending_transcript_sessions`
- `src/gobby/sessions/summarize.py::generate_session_summaries`
- `src/gobby/sessions/analyzer.py::TranscriptAnalyzer`
- `src/gobby/agents/idle_check_handler.py::*` — scope-reason: idle session and agent checks share local-resource ownership boundaries
- `src/gobby/sessions/transcript_paths.py::find_transcript_on_disk`
- `src/gobby/hooks/event_handlers/_session_start/transcripts.py::derive_transcript_path`
- `src/gobby/tasks/transcript_evidence.py::*` — scope-reason: ownership-bearing caller of the on-disk fallback scanner; must pass the session's owner machine into the refusal boundary
- `src/gobby/hooks/event_handlers/_session_start/flow.py::*` — scope-reason: ownership-bearing caller wiring session machine identity into derive_transcript_path
- `src/gobby/sessions/summary_generation.py::generate_summary`
- `src/gobby/servers/routes/sessions/analytics.py::generate_session_summary`
- `src/gobby/sessions/machine_scope.py`
- `src/gobby/sessions/transcript_reader.py::TranscriptReader`
- `src/gobby/agents/watchdog/transcript_resolver.py::WatchdogTranscriptResolver`
- `src/gobby/cli/sessions.py::create_handoff`
- `src/gobby/mcp_proxy/tools/sessions/_commits.py::*` — scope-reason: session commit inspection resolves local workspaces and invokes git
- `src/gobby/mcp_proxy/tools/sessions/_summary_metadata.py::*` — scope-reason: compact-summary metadata resolves local transcript/workspace sources

`sessions.machine_id` already exists (a nullable `UUID REFERENCES machines(id)`
after the gcore-schema-authority identity cutover), so this is query-scoping
only — `machine_id = local` filtering excludes NULL-machine rows by
construction:

- `get_pending_transcript_sessions` (`storage/sessions/_transcript.py:21` — confirmed
  unscoped today) filters `machine_id = local`; daemon B must not attempt to process
  transcript files that live on machine A's disk.
- Summarizer, analyzer, and idle-watchdog session selection restrict to local-machine
  sessions wherever the operation opens `session.transcript_path` or the session's
  workspace.
- On-disk fallback scanners (`sessions/transcript_paths.py::find_transcript_on_disk`,
  `_session_start/transcripts.py::derive_transcript_path`) currently receive no
  ownership identity at all (source + external_id only), so the refusal needs a
  signature change: they gain owner-identity inputs (session owner machine_id and
  local machine_id, or the session record) and return None on mismatch BEFORE any
  glob or path probe. Their ownership-bearing production callers
  (`tasks/transcript_evidence.py`, `_session_start/flow.py`) pass the identity
  through — this closes the misattach hazard where daemon B globs its own
  `~/.claude`/`~/.codex` and attaches the wrong local file to a remote session.
- `TranscriptReader` and `WatchdogTranscriptResolver` pass owner and local machine
  identity to the same fallback boundary. A mismatch is rejected before stored-path
  `stat`, cache lookup, globbing, or message extraction; `TranscriptReader` never
  persists a re-derived path to a remote-owned shared session row.
- Central `require_local_session_ownership` is the first operation in every session
  surface that touches transcripts, workspaces, or repositories. CLI `create_handoff`,
  the session commits MCP tool, and compact summary metadata call it before any `Path`
  probe or git command and return an explicit remote-owner refusal. Stored metadata
  reads that require no local resource remain global.
- The on-demand summary path is scoped like the background one: the HTTP analytics
  route (`servers/routes/sessions/analytics.py::generate_session_summary`) calls
  `sessions/summary_generation.py::generate_summary`, which today fetches any
  session and opens its transcript/workspace; it must reject sessions whose
  machine_id is not local before any filesystem access.
- Read-only metadata surfaces (session list APIs, wiki recaps over stored summaries)
  stay global — sharing is the point of M0.

**Acceptance:**

- 2.3.1 - Pending-transcript selection is machine-scoped. symbol: `gobby.storage.sessions._transcript.get_pending_transcript_sessions`.
- 2.3.2 - A remote-machine session is never processed, summarized, or watchdogged locally, and `transcript_processed` remains untouched by the non-owning daemon. test: `tests/sessions/test_machine_scoped_consumers.py::test_remote_sessions_skipped`.
- 2.3.3 - Fallback transcript scanners return None for remote-machine sessions. test: `tests/sessions/test_machine_scoped_consumers.py::test_fallback_scan_refuses_remote_sessions`.
- 2.3.4 - The on-demand summary route refuses a remote-machine session before touching the filesystem. test: `tests/sessions/test_machine_scoped_consumers.py::test_on_demand_summary_refuses_remote_sessions`.
- 2.3.5 - TranscriptReader and watchdog fallback reject remote ownership before stored-path stat, cache, glob, or persistence; remote rows remain unchanged. test: `tests/sessions/test_machine_scoped_consumers.py::test_reader_and_watchdog_refuse_remote_fallback`.
- 2.3.6 - CLI handoff, session commits MCP, and compact summary metadata refuse remote-owned sessions before filesystem or git access. test: `tests/sessions/test_machine_scoped_consumers.py::test_session_filesystem_surfaces_refuse_remote_owner`.

### 2.4 Scope cron reconcile and stale sweeps by machine_id [category: code] (depends: 2.1)
`kind: deliverable`

Targets:
- `src/gobby/storage/cron_children.py::_fail_remaining_active_runs`
- `src/gobby/storage/cron_runs.py::*` — scope-reason: machine-scope stale-run sweeps and concurrency counting alongside the model changes in 2.1
- `src/gobby/scheduler/scheduler.py::*` — scope-reason: pass local machine ownership through startup reconcile, stale sweeps, admission, and concurrency accounting

Occurrence claiming is already cross-daemon safe (`_create_scheduled_run` claims under
`transaction_immediate(lock=CronRunAdmission())` with an `expected_next_run_at` CAS,
`scheduler/scheduler.py:155-181`) — a job fires once, on whichever machine's heartbeat
wins; document that. The sweeps are not safe:

- `_fail_remaining_active_runs` (`storage/cron_children.py:187`) fails ALL
pending/running cron_runs at every scheduler startup — with two daemons, restarting
B deterministically kills A's in-flight runs. Scope to
`machine_id = local`.
- `fail_stale_running_runs` (`storage/cron_runs.py:220`) excludes only locally-tracked
  run ids, so B times out A's legitimately long runs. Same scoping.
- `count_running` (concurrency slots) counts per-machine so one machine's running jobs
  do not starve the other's slots.
- Recurring maintenance loops (`runner_maintenance_recurring.py`: metrics cleanup,
  memory reconcile) double-run across daemons but are idempotent reconciliation —
  accepted for M0, noted in 3.2.

**Acceptance:**

- 2.4.1 - Startup reconcile fails only runs whose `machine_id` exactly matches the local machine. symbol: `gobby.storage.cron_children._fail_remaining_active_runs`.
- 2.4.2 - Restarting one daemon leaves the other machine's in-flight cron runs untouched. test: `tests/scheduler/test_cron_machine_scope.py::test_restart_does_not_fail_remote_runs`.
- 2.4.3 - Stale-run timeout and concurrency counting are machine-scoped. test: `tests/scheduler/test_cron_machine_scope.py::test_stale_sweep_and_slots_scoped`.

## P3: Guards and runbook
`kind: framing`

**Goal**: Version skew fails loudly; the topology is a documented, supported path.

### 3.1 Migration lockstep guard [category: code]
`kind: deliverable`

Targets:
- `src/gobby/storage/hub/postgres.py::PostgresHubDatabase.apply_migrations`
- `src/gobby/storage/migrations.py::MigrationRunner`
- `src/gobby/storage/migrations.py::MigrationUnsupportedError`
- `src/gobby/storage/migrations.py::latest_known_version`

Today `MigrationRunner.apply_pending` (`storage/migrations.py:122`) is advisory-locked
and CAS-safe against concurrent migrators but silently ignores recorded versions it
does not know — an older build runs happily against a newer schema.

`PostgresHubDatabase.apply_migrations` delegates the complete decision to one new
`MigrationRunner` lock-owning orchestration API. It acquires one dedicated session
advisory lock before bookkeeping initialization and holds it without interruption
through: schema-head inspection; newer-schema refusal; baseline presence decision and
first-write application; migration discovery; applied/pending revalidation; chain and
destructive checks; every transactional and non-transactional application; and final
head verification. Under that lock, `SELECT MAX(version) FROM schema_migrations`; if
it exceeds `latest_known_version()`, raise fatal `MigrationUnsupportedError`: "hub
schema is vX but this gobby build knows vY — update gobby on this machine."

The enclosing session lock replaces per-migration advisory-lock acquisition so it
cannot self-deadlock, while version-row CAS remains defense in depth. Lock release is
guaranteed on success, refusal, baseline failure, transactional failure, and
non-transactional failure. Every CLI and the daemon reach the
database through `runtime_hub_database(apply_migrations=True)`
(`storage/hub/runtime.py:17`), so one check covers all entry points. Roll-forward (DB
older than code) already works. Log code/db schema versions at startup. Update the
version-pinned migration-contract tests per the repo's baseline rules.

Scope boundary, stated plainly: this is a startup-time guard. An already-running
older daemon is not re-checked mid-flight — continuous schema re-validation is
worker-architecture (#17435) machinery, not M0. The supported M0 upgrade procedure is
therefore stop-the-world, documented as a hard runbook rule in 3.2 and exercised in
4.1 item 7: stop every daemon, update every machine, start one designated migrator,
verify it is healthy, then start the remaining daemons.

**Acceptance:**

- 3.1.1 - Startup against a newer-than-known schema fails with the actionable error and performs no writes, with bookkeeping, head inspection, baseline decision, pending discovery/revalidation, and all migration application enclosed by one advisory lock. test: `tests/storage/test_migration_lockstep.py::test_newer_schema_fails_closed`.
- 3.1.2 - Guard lives on the shared runtime path. symbol: `gobby.storage.hub.postgres.PostgresHubDatabase.apply_migrations`.
- 3.1.3 - Barrier-controlled old/new build races at head inspection, pending discovery, baseline first write, transactional application, non-transactional application, and the no-pending exit all serialize; the older build cannot report success against a newer head. test: `tests/storage/test_migration_lockstep.py::test_enclosing_lock_serializes_every_schema_decision`.

### 3.2 Shared-datastores runbook [category: docs]
`kind: deliverable`

Target: `docs/guides/shared-stack.md`

Rewrite the guide: remote Postgres moves from "not supported" (current lines 8-10) to
the supported `datastore_mode: remote` path. Contents: hub setup (`gobby install`,
`gobby datastores expose --bind <ts-ip> --host <name>`, Tailscale ACLs restricting
60891/6333/16379, container restart policy); client setup (same gobby version, copy
`.secret_kek` + `local_cli_token`, bootstrap.yaml keys, remote-mode `gobby install`
preflight, `gobby start`); the M0 boundary stated plainly (execution/worktrees/
transcripts per-machine; push before packing up; identical checkout paths recommended;
transcript continuity arrives with #17435); the pack-up claim handoff (task claims are
session-bound and survive a daemon stop, so release or hand off claims before packing
up, or reclaim on the second machine through the explicit force-reclaim contract after
verifying the first machine is stopped — 4.1 item 3 exercises exactly this
transition); the stop-the-world upgrade protocol from 3.1 (stop all daemons → update
all machines → start one designated migrator → start the rest) as a hard rule; the
machine-scoping migration's remediation path (abort diagnostics → operator SQL
disposition of unresolved legacy rows → rerun, per 2.1); version-lockstep expectation;
deferred items (Qdrant API key with ACLs mandatory meanwhile, hook machine_id
fallbacks → #17435, Postgres `max_connections` sizing note for two daemons + CLI
churn).

**Acceptance:**

- 3.2.1 - Guide documents hub exposure, client setup, M0 boundaries, and deferrals as above. file: `docs/guides/shared-stack.md`.
- 3.2.2 - The unsupported-remote-Postgres claim is gone and replaced by the `datastore_mode: remote` contract. behavior: "remote datastores supported path" in `docs/guides/shared-stack.md`.

## P4: Validation
`kind: framing`

**Goal**: Prove the two-machine topology safe and useful before it becomes the daily
driver.

### 4.1 Two-machine end-to-end acceptance [category: test] (depends: P1, P2, P3)
`kind: deliverable`

Target: `tests/e2e/test_shared_datastores_m0.py`, plus a manual checklist appended to
`docs/guides/shared-stack.md`

Automated where possible (two isolated daemons with distinct GOBBY_HOME/machine_id
against one isolated temporary stack containing PostgreSQL, Qdrant, and FalkorDB,
with separate test namespaces/configuration, explicit readiness checks, and
`GOBBY_TEST_PROTECT=1`; setup honors the second-machine safety barrier — the second
daemon connects only after 2.1-2.4 and 3.1 behavior is in place), manual checklist
for the pieces that need real machines:

1. Hub exposure: containers healthy; binds only on the tailnet address; ports
   unreachable from a non-ACL device (manual).
2. Client startup: both daemons pass the readiness gate against the remote stack; both
   machine_ids appear in `machines` (note: registration currently happens on session
   create/hook ingress via `LocalMachineManager.upsert_seen` — trigger a session or
   add a startup upsert as a one-line nicety).
3. Task continuity (the pack-up scenario): create/claim on A; stop A; on B, perform
   the supported ownership transition — explicit release/handoff before A shut down,
   or force-reclaim through the task-claim contract after verifying A is stopped —
   and assert the claim owner is B's session before B continues and completes the
   task; restart A and observe convergence with no claim conflict. Memory
   round-trip: store on A, recall (vector + graph) on B.
4. Session metadata: A's sessions visible on B with A's machine_id; B never processes
   A's transcripts; no misattached local files.
5. Isolation safety: same project+branch worktree on both machines without unique
   violation; an agent running in tmux on A is never marked dead by B; B's stale
   cleanup never touches A's rows or filesystem.
6. Cron: an interval job fires exactly once per occurrence with both daemons up;
   restarting B does not fail A's in-flight run.
7. Lockstep: newer migration applied from one machine → the stale machine fails
   clean; the runbook's stop-the-world upgrade protocol, followed step by step,
   never hits the guard.
8. Concurrency smoke: parallel CLI activity on both machines; observe Postgres
   connection count against `postgres_pool` × 2 daemons (default max_connections 100).

**Acceptance:**

- 4.1.1 - Automated two-daemon e2e covers items 3-7 against an isolated temporary PostgreSQL/Qdrant/FalkorDB stack, including the cross-daemon vector-and-graph memory round-trip against those provisioned services. test: `tests/e2e/test_shared_datastores_m0.py::test_two_machine_continuity_and_isolation`.
- 4.1.2 - Manual checklist for items 1-2 and 8 ships in the runbook. behavior: "M0 acceptance checklist" in `docs/guides/shared-stack.md`.

## V1 Plan Changelog

`kind: verification`

**Round 1** `kind: verification`

- reviewer_run: c574eab2-a9d1-44aa-9114-32f2fa0e7808
- reviewer_session: d11a4716-a54c-4a5f-bd57-0bbb6b34288b
- verdict: needs_review
- findings:
- F-M0-SLOT-FLATTEN / blocking / stale 369 migration literal, slot contention, flatten-chain gate unencoded
- F-LEGACY-MACHINE-BACKFILL / blocking / NOT NULL backfill cannot resolve legal legacy rows (NULL-session workspaces, sessionless cron_runs)
- F-PACK-UP-CLAIM-HANDOFF / blocking / e2e pack-up scenario lacks a legal claim-ownership transition
- F-MACHINE-MODEL-TARGETS / blocking / agent-run insert/models/CronRun serialization surfaces missing from Targets
- F-WORKSPACE-MAINTENANCE-SCOPE / blocking / isolation maintenance missing-path sweeps can delete another machine's records
- F-TRANSCRIPT-FALLBACK-OWNERSHIP / blocking / fallback scanners lack owner-identity inputs; callers untargeted
- F-DATASTORES-CLI-REGISTRY / blocking / root CLI registration (cli/__init__.py) untargeted for the datastores group
- F-INSTALL-DECOMPOSITION / blocking / install.py at 939 lines with no decomposition boundary
- F-REMOTE-LIFECYCLE-COMPOSE / blocking / restart bypasses public stop via _do_stop; Docker validation not topology-aware
- F-COLD-START-BIND-SOURCE / blocking / bind address stored inside the database it is needed to start
- F-EXPOSE-ATOMICITY / blocking / expose persists shared endpoints before compose/health can fail
- F-REMOTE-INSTALL-PREFLIGHT / blocking / full-install preflight hard-requires Docker before the remote branch
- F-REMOTE-PREFLIGHT-DEPENDENCY / blocking / 1.3 consumes 1.2's endpoints without depending on 1.2
- F-SECOND-MACHINE-BARRIER / blocking / connection barrier gated only 2.1/3.1, omitting 2.2-2.4
- F-UPGRADE-LOCKSTEP-PROTOCOL / blocking / startup-only head check leaves running-daemon skew window
- F-REMOTE-SUMMARY-ROUTE / blocking / on-demand summary route opens any session's transcript unscoped
- resolution_notes: Unattended round (operator pre-approved). All 16 findings accepted after
  independent code verification (nullable agent_session_id and sessionless cron_runs confirmed
  in the baseline schema; restart/_do_stop, isolation.py missing-path sweeps, Docker preflight,
  scanner signatures, analytics route call, install.py=939 lines, and root CLI registry all
  confirmed on disk). Fifteen repaired as proposed. F-UPGRADE-LOCKSTEP-PROTOCOL accepted
  modified: head check serialized under the migration advisory lock plus a stop-the-world
  upgrade protocol in the 3.2 runbook exercised by 4.1 item 7; the proposed continuous
  runtime-role pool recheck is worker-architecture (#17435) machinery and was not adopted.
  Repairs landed in this revision across Constraints, 1.1, 1.2, 1.3, 2.1, 2.2, 2.3, 3.1,
  3.2, and 4.1; base validation clean.

```json plan-review-round
{"evidence_id":"82c237f7-cea1-404d-b6ba-08ce9b288dcf","plan_hash":"dccebbd2f323f6d0fa75ee4104c43946bf7241789e87202645c1148a9c249991","round_number":1,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"1c3f8f259fd8a645c8e79b0f7b12b6753d1bde60045175056f592c2cb0d7d0df","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":4,"emitted_findings":16,"total":20},"evidence_id":"82c237f7-cea1-404d-b6ba-08ce9b288dcf","lanes":[{"candidate_count":4,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":8,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":8,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":10,"manifest_digest":"6c03c8479f509e0078db97cfeaa5a9b80c982308bef2ee53fa8855471d49640b","status":"valid"},"source_digest":"77c2b1b0a23036cbcbbd8fdf76acd3fa5e1bf218df4eeaafc0c59799d9d297e9","version":1},"findings":[{"category":"bad-sequencing","check_key":"migration-slot-flatten-order","description":"`src/gobby/storage/migrations/369_machine_scope.sql` is impossible as written. Concurrent credential work may consume 371+, and M0 must land before #19422 gates filename bookkeeping and the #19424 flatten.","finding_id":"F-M0-SLOT-FLATTEN","fix":"Allocate through the serialized migration authority at landing, replace every 369 literal with the exact reserved pre-flatten filename, make the 2.1 leaf block #19422 → #19423 → #19424, and state that post-#19424 schema edits go to the regenerated baseline until 0.5.0 ships.","location":"P2 / § 2.1","prevention":"Before plan approval, reconcile disk, live MAX(version), concurrent allocations, and the flatten dependency chain in every migration target and acceptance reference.","principle":"A numbered migration must use a serialized free slot and land before the baseline flatten that consumes it.","root_cause":"The concrete target and acceptance still name 369 even though 369 and 370 are occupied; the generic recheck clause neither reserves a slot nor rewrites those artifacts or encodes the #19422 → #19423 → #19424 gate.","section_id":"2.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"authoritative-legacy-owner-conversion","description":"The stated authoritative-session backfill cannot resolve legal existing rows. Aborting protects integrity yet can leave an ordinary populated database permanently unable to apply M0.","finding_id":"F-LEGACY-MACHINE-BACKFILL","fix":"Add a per-table conversion matrix. Define authoritative joins for linked rows; require verified operator mapping or deliberate cleanup for detached workspaces; drain active cron work and specify verified mapping or explicit disposal of ownerless historical runs. Test rollback, diagnostics, remediation, and rerun.","location":"P2 / § 2.1","prevention":"Seed every currently legal legacy shape and require both fail-closed diagnostics and a documented successful rerun path.","principle":"Every NOT NULL ownership migration needs a total conversion or an explicit successful remediation path for every legal legacy row shape.","root_cause":"Detached worktrees and clones can lack `agent_session_id`, while ordinary cron rows have no `session_id` and can remain without agent or pipeline children.","section_id":"2.1","severity":"blocking"},{"category":"bad-sequencing","check_key":"pack-up-claim-transfer","description":"The e2e says create/claim on A, stop A, then continue/complete on B without naming release, handoff, or verified force-reclaim behavior.","finding_id":"F-PACK-UP-CLAIM-HANDOFF","fix":"Specify one supported transition in § 3.2 and § 4.1: release/handoff before shutdown, or verify A is stopped and use the existing explicit force-reclaim contract. Assert the owner changes to B before B continues and completes.","location":"P4 / § 4.1","prevention":"For every resume-on-another-machine acceptance path, trace ownership from the original session through handoff, reclaim, and completion.","principle":"A cross-machine continuation scenario must include a legal ownership transition before the second session edits or completes the task.","root_cause":"Stopping daemon A does not release the task's session claim, and B's normal atomic claim conflicts with A's surviving owner.","section_id":"4.1","severity":"blocking"},{"category":"traceability","check_key":"machine-scope-writer-model-targets","description":"The declared target set cannot implement or serialize all four promised `machine_id` fields.","finding_id":"F-MACHINE-MODEL-TARGETS","fix":"Add scoped targets for `src/gobby/storage/agents/_lifecycle.py`, `src/gobby/storage/agents/_models.py`, and `src/gobby/storage/cron_models.py`; require `machine_id` across insert, `from_row`, and public serialization surfaces with constructor/round-trip tests.","location":"P2 / § 2.1","prevention":"For each new persisted field, inventory INSERT/UPDATE paths, constructors, row mappers, serializers, fakes, and tests before finalizing Targets.","principle":"Targets must include the concrete writer, hydration, and serialization symbols required by the promised data-shape change.","root_cause":"`storage/agents/_manager.py` is a facade; agent-run inserts live in `_lifecycle.py`, agent models live in `_models.py`, and `CronRun` serialization lives in `cron_models.py`.","section_id":"2.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"workspace-maintenance-machine-filter","description":"The plan scopes named stale finders and claims while omitting a live maintenance path that can remove another machine's workspace records.","finding_id":"F-WORKSPACE-MAINTENANCE-SCOPE","fix":"Add `src/gobby/runner_maintenance/isolation.py` and its storage call surfaces to Targets, make filesystem consumers local-machine-only by default, and add a two-machine regression proving B leaves A's rows and paths untouched.","location":"P2 / § 2.2","prevention":"Sweep list/get/get_by_task/update/delete consumers and test a non-owner daemon against owner-only filesystem state.","principle":"Every consumer that interprets a shared path against local disk must filter by machine ownership before filesystem checks or deletion.","root_cause":"Recurring isolation maintenance uses global worktree and clone lists, treats another machine's paths as missing locally, and can delete those shared rows.","section_id":"2.2","severity":"blocking"},{"category":"traceability","check_key":"transcript-fallback-owner-input","description":"The promised remote-machine fallback refusal cannot be implemented reliably within the listed files.","finding_id":"F-TRANSCRIPT-FALLBACK-OWNERSHIP","fix":"Target `tasks/transcript_evidence.py` and `_session_start/flow.py`; require `owner_machine_id` and `local_machine_id` at the scanner boundary, refuse mismatches before filesystem access, and test canonical-session reuse and task-evidence fallback.","location":"P2 / § 2.3","prevention":"Trace every fallback scanner to the caller that owns the session record and include both API and caller changes in Targets and tests.","principle":"A filesystem refusal must receive authoritative owner identity before any glob, path probe, or fallback lookup.","root_cause":"`find_transcript_on_disk` and `derive_transcript_path` lack session ownership inputs, while ownership-bearing production callers are absent from Targets.","section_id":"2.3","severity":"blocking"},{"category":"traceability","check_key":"root-cli-command-registration","description":"Creating `src/gobby/cli/datastores.py` alone cannot satisfy acceptance 1.2.5.","finding_id":"F-DATASTORES-CLI-REGISTRY","fix":"Add a scoped `src/gobby/cli/__init__.py` target for import and registration, plus a CliRunner test proving `gobby datastores expose --help` resolves through the root CLI.","location":"P1 / § 1.2","prevention":"For every new root command, trace module creation through import, `add_command`, help resolution, and invocation.","principle":"A new root CLI group requires an explicit registry target and a root-level resolution test.","root_cause":"Root commands are imported and registered in `src/gobby/cli/__init__.py`, which is absent from Targets.","section_id":"1.2","severity":"blocking"},{"category":"gobby-format","check_key":"production-file-line-ceiling","description":"The deliverable has only 60 lines of headroom and no executable decomposition boundary.","finding_id":"F-INSTALL-DECOMPOSITION","fix":"Add a focused remote-datastore install/preflight helper module target, keep `install.py` as thin orchestration, and make sub-1,000-line post-change counts an acceptance condition.","location":"P1 / § 1.3","prevention":"Record current and projected line counts for every production target and add decomposition targets before expansion when the ceiling is at risk.","principle":"Planned changes must keep every touched hand-maintained production source below the 1,000-line ceiling.","root_cause":"`src/gobby/cli/install.py` is already 939 lines, and the plan adds mode branching, credential guidance, three service probes, and diagnostics without a helper-module target.","section_id":"1.3","severity":"blocking"},{"category":"unhandled-edge","check_key":"remote-lifecycle-compose-closure","description":"Implementing only the stated public start/stop skips can still require Docker during remote startup or invoke compose from remote restart.","finding_id":"F-REMOTE-LIFECYCLE-COMPOSE","fix":"Make dependency validation and `_do_stop` topology-aware, include restart/shared helper targets, and test Docker-free remote start plus remote stop/restart with Docker flags while asserting zero compose calls.","location":"P1 / § 1.1","prevention":"Trace start, stop, restart, shared helpers, dependency checks, and Docker flags for each topology mode.","principle":"Remote topology must disable managed-compose behavior across every shared lifecycle entry point.","root_cause":"Startup dependency validation runs before configuration, while restart shares `_do_stop` and can bypass the public stop wrapper.","section_id":"1.1","severity":"blocking"},{"category":"bad-sequencing","check_key":"bind-source-before-postgres","description":"After `gobby stop --docker`, container loss, or recovery, the hub cannot recover the tailnet bind from the planned source and may strand clients on loopback.","finding_id":"F-COLD-START-BIND-SOURCE","fix":"Persist the hub-owned bind address in `bootstrap.yaml` or another durable machine-local pre-Postgres source; keep shared dial endpoints in config_store. Add expose→stop/remove→cold-start coverage that verifies the tailnet bind and remote readiness.","location":"P1 / § 1.2","prevention":"For every cold-start parameter, verify its source is available after container stop, loss, and host reboot.","principle":"Configuration needed to start PostgreSQL must be readable before PostgreSQL is available.","root_cause":"`databases.bind_address` is stored only in PostgreSQL `config_store`, while `_services_start` starts PostgreSQL before it can read that store.","section_id":"1.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"expose-failure-atomicity","description":"A port collision, invalid interface, compose failure, or readiness timeout can leave every daemon reading endpoints that were never made reachable.","finding_id":"F-EXPOSE-ATOMICITY","fix":"Specify validate→stage compose with candidate overrides→health-check→atomically commit shared endpoints. If early persistence is unavoidable, capture and restore all prior local and shared values on every failure. Add failure-injection tests.","location":"P1 / § 1.2","prevention":"Enumerate validation, staging, health, commit, timeout, nonzero exit, and rollback outcomes for multi-system transitions.","principle":"A command spanning shared configuration and external container state needs a defined commit point and complete failure recovery.","root_cause":"The plan persists bind and endpoint values before fallible compose recreation and health checks, with no rollback contract.","section_id":"1.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"remote-install-docker-preflight","description":"A Docker-free client can fail before reaching the remote reachability checks even though remote mode manages no local datastore stack.","finding_id":"F-REMOTE-INSTALL-PREFLIGHT","fix":"Resolve `datastore_mode` before `_run_install_preflight`, use a remote preflight profile that omits Docker and compose checks, and add tests with the Docker executable and daemon unavailable.","location":"P1 / § 1.3","prevention":"Place topology resolution before prerequisite selection and test each excluded local dependency as unavailable.","principle":"Remote installation must select topology before running topology-specific prerequisites.","root_cause":"The current full-install preflight requires a running Docker daemon before the plan's remote branch skips local provisioning.","section_id":"1.3","severity":"blocking"},{"category":"bad-sequencing","check_key":"remote-endpoint-producer-dependency","description":"Expansion may run § 1.3 before hub exposure, causing deterministic probes of localhost defaults on the client.","finding_id":"F-REMOTE-PREFLIGHT-DEPENDENCY","fix":"Make § 1.3 depend on both § 1.1 and § 1.2, and make its acceptance setup execute successful hub exposure before any client preflight.","location":"P1 / § 1.3","prevention":"Build a producer-consumer dependency map for every new config key and endpoint.","principle":"A deliverable must depend on every in-plan producer of configuration it consumes.","root_cause":"Remote preflight reads Qdrant and FalkorDB endpoints created by § 1.2, while § 1.3 depends only on § 1.1.","section_id":"1.3","severity":"blocking"},{"category":"bad-sequencing","check_key":"second-machine-safety-barrier","description":"The stated barrier permits the exact cross-machine corruption and false cleanup that P2 is designed to prevent.","finding_id":"F-SECOND-MACHINE-BARRIER","fix":"Require § 2.1, § 2.2, § 2.3, § 2.4, and § 3.1 to land and validate before any second daemon connects; reflect that barrier in dependencies, runbook order, and e2e setup.","location":"Constraints / P2","prevention":"Enumerate all current global consumers and include every safety-critical scoping leaf in the connection barrier.","principle":"A second daemon may connect only after every cross-machine destructive or mutating consumer is machine-scoped.","root_cause":"The constraint gates only § 2.1 and § 3.1 even though § 2.2-§ 2.4 prevent global agent polling, transcript work, workspace deletion, and cron failure sweeps.","section_id":"2.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"running-daemon-schema-lockstep","description":"The plan claims lockstep while leaving a window for unscoped or schema-incompatible writes from an already-running old build.","finding_id":"F-UPGRADE-LOCKSTEP-PROTOCOL","fix":"Adopt a stop-the-world M0 upgrade protocol: stop all daemons, update all machines, start one designated migrator, then start remaining daemons. Serialize head check and migration, recheck before exposing the runtime-role pool, and extend § 3.2/§ 4.1 accordingly.","location":"P3 / § 3.1","prevention":"Test stale-start, concurrent-start, and stale-already-running states, and document the supported upgrade protocol.","principle":"Schema lockstep must cover already-running daemons and the race between checking the head and applying a migration.","root_cause":"The proposed MAX(version) check runs at startup, while migration locking covers only individual applications; an old daemon can remain active as a newer daemon advances the schema.","section_id":"3.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"on-demand-summary-machine-scope","description":"Daemon B can still summarize daemon A's session through the on-demand route, violating acceptance 2.3.2 despite scoping the background summarizer.","finding_id":"F-REMOTE-SUMMARY-ROUTE","fix":"Add `src/gobby/sessions/summary_generation.py::generate_summary` and `src/gobby/servers/routes/sessions/analytics.py::generate_session_summary` to Targets; reject non-local ownership before Path/workspace access and test the route from the non-owning daemon.","location":"P2 / § 2.3","prevention":"Sweep scheduled, on-demand, CLI, and route-level summary consumers and test each non-owner entry point before filesystem access.","principle":"Every summary path that opens a transcript or workspace must verify local machine ownership first.","root_cause":"`sessions.summary_generation.generate_summary`, called by the HTTP analytics route, fetches any session and opens its transcript/workspace without a machine check; neither surface is targeted.","section_id":"2.3","severity":"blocking"}],"reviewer_session":"d11a4716-a54c-4a5f-bd57-0bbb6b34288b","round":1,"verdict":"needs_review"},"session_id":"d5f358bb-1c03-4d42-860a-67dc4205a48e"}
```

- 2026-08-03 stage-native resubmission (planner session 33ce1d9c): no new adversary findings supplied; round-1 repairs re-verified against the current tree — migration slots 354-370 occupied with 371 still provisionally reserved, UUID-identity prerequisites (365/366) on disk, `install.py` at 939 lines, no plan-anchored files changed since the repair commit (ced43f23b); base and project-aware `gobby plans validate` both pass.

**Round 2** `kind: verification`

- reviewer_run: 3002da40-d9a6-4bb5-a9f8-71e12c1c7b07
- reviewer_session: bd50b5ab-605d-4e00-b71f-a106bfcfd719
- verdict: needs_review
- findings:
- F-R2-BUNDLED-MANIFEST-PARITY / blocking / accepted / bundled bootstrap bytes require inventory regeneration and integrity coverage
- F-R2-COLD-START-CONFIG-ORDER / blocking / accepted / public cold start must revive PostgreSQL from bootstrap before DB-backed config
- F-R2-EXPOSE-LIFECYCLE-SERIALIZATION / blocking / accepted / all managed-compose transitions need one bounded lifecycle lock
- F-R2-IPV6-BIND-CONTRACT / blocking / accepted / M0 IPv4-bind and DNS-host contract avoids ambiguous Compose and URL serialization
- F-R2-REMOTE-PREFLIGHT-DEADLINES / blocking / accepted / every required remote probe now has bounded liveness and cleanup
- F-R2-AGENT-SWEEP-SCOPE / blocking / accepted / cleanup and termination storage/call-through paths require local ownership
- F-R2-ACTIVE-RUN-CALLER-SEMANTICS / blocking / accepted / explicit local and global APIs preserve resource safety and metadata invariants
- F-R2-TRANSCRIPT-FALLBACK-CALLERS / blocking / accepted / reader and watchdog callers must refuse remote ownership before fallback work
- F-R2-SESSION-FILESYSTEM-CONSUMERS / blocking / accepted / handoff, commits, and summary metadata need the shared ownership guard
- F-R2-MIGRATION-LOCK-SPAN / blocking / accepted / one lock must enclose baseline, head, pending, and all migration decisions
- resolution_notes: Unattended final capped round. All ten findings accepted from
  canonical evidence and independently reconciled with current repository structure.
  Repairs add the missing targets, deterministic contracts, bounded deadlines,
  local/global query split, filesystem ownership guard, lifecycle serialization,
  and enclosing migration lock orchestration. Project-aware base validation passes.

```json plan-review-round
{"evidence_id":"4e6fd6d3-182d-4462-a96a-6191ac0e06e6","plan_hash":"d504b84046c90cb89005e371e89fb7e95480d13f4ffc110a1ba9df5da0c7f744","round_number":2,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"bcd9041d0996519f8a0e5269838e59c9e5aa07136b6a4ed73d834ff3299844bb","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":3,"emitted_findings":10,"total":13},"evidence_id":"4e6fd6d3-182d-4462-a96a-6191ac0e06e6","lanes":[{"candidate_count":3,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":4,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":6,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":10,"manifest_digest":"8af1f5debfca90fff138df8b0b05ec6cdb49661fa159c30ce8794f906419ad78","status":"valid"},"source_digest":"0b8c693f4d7124b8ca9a1010b972c49bb228c8ed9f9287f8c0a17b0100a17e62","version":1},"findings":[{"category":"traceability","check_key":"bundled-content-manifest-parity","description":"Implementing §1.1 as written changes the installed bootstrap template while leaving its recorded hash stale, so the repository's bundled-content integrity check fails even when every listed acceptance item passes.","finding_id":"F-R2-BUNDLED-MANIFEST-PARITY","fix":"Add `src/gobby/install/bundled_content_manifest.json` as a generated target, require regeneration after the bootstrap-template edit, and include the existing manifest/tree integrity test in §1.1 acceptance.","location":"P1 / § 1.1","prevention":"For every bundled-content edit, trace the source through generated inventory/hash artifacts and their integrity tests before finalizing Targets and Acceptance.","principle":"Editing installed bundled content must update the committed inventory that authenticates those bytes.","root_cause":"The section targets `src/gobby/install/shared/config/bootstrap.yaml` but omits `src/gobby/install/bundled_content_manifest.json`; the existing integrity test compares the committed manifest to a freshly hashed bundled tree.","section_id":"1.1","severity":"blocking"},{"category":"bad-sequencing","check_key":"bootstrap-before-database-start-order","description":"The repaired bootstrap source is not enough to make the hub recoverable: the public start path can still fail on database configuration before compose revival, and a literal implementation of 1.2.2 would restore the database dependency.","finding_id":"F-R2-COLD-START-CONFIG-ORDER","fix":"State that public start loads `BootstrapConfig` first, performs topology-aware checks, starts local compose from bootstrap-only values, and only then resolves DB-backed `DaemonConfig`; rewrite 1.2.2 to name `BootstrapConfig.services_bind_address` and make 1.2.6 invoke the public start command.","location":"P1 / §§ 1.1-1.2","prevention":"Exercise cold-start acceptance through the public command with PostgreSQL unavailable, and trace each startup input to a source available before the dependency it starts.","principle":"Configuration required to revive PostgreSQL must be resolved before any PostgreSQL-backed runtime configuration is opened.","root_cause":"The public `start` command currently resolves `CliRuntime.config`, which opens the database and applies migrations, before `_services_start`; additionally, acceptance 1.2.2 contradicts the body by naming `databases.bind_address` rather than bootstrap `services_bind_address`.","section_id":"1.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"managed-compose-transition-serialization","description":"Concurrent expose/expose or expose/start/stop/install operations can health-check one binding, commit endpoints for another, or let rollback overwrite a competing successful transition.","finding_id":"F-R2-EXPOSE-LIFECYCLE-SERIALIZATION","fix":"Add one hub-local managed-services lifecycle lock spanning bootstrap snapshot, compose staging, health checks, endpoint `set_many`, and rollback; require expose, local `_services_start`/`_services_stop`, and installer compose refreshes to use it, with timeout and interleaving tests.","location":"P1 / § 1.2","prevention":"For each stateful CLI transition, enumerate concurrent commands and prove all conflicting paths share one bounded critical section with deterministic interleaving tests.","principle":"A multi-system state transition needs one serialization boundary shared by every command that can mutate the same external state.","root_cause":"Atomic bootstrap replacement and atomic config-store writes protect individual writes, but expose, local start/stop, and installer compose refreshes have no common lock around their read-modify-health-commit/rollback workflows.","section_id":"1.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"ipv6-compose-url-serialization","description":"`::1` and concrete Tailscale IPv6 addresses are accepted by the stated validator but become ambiguous Compose port strings; IPv6 published hosts also require bracketed URL authorities.","finding_id":"F-R2-IPV6-BIND-CONTRACT","fix":"Use the least M0 mechanism: reject IPv6 bind and published-host literals, accept concrete IPv4 interface addresses plus DNS published hosts, and test IPv4 loopback, IPv6 loopback, Tailscale IPv4/IPv6, both wildcards, and DNS hosts.","location":"P1 / § 1.2","prevention":"Build an accepted-value matrix and round-trip each value through Compose binding and URL authority serialization before defining validation.","principle":"Every accepted address must have an unambiguous serializer for every downstream syntax that consumes it.","root_cause":"The input contract accepts loopback or concrete Tailscale addresses, including IPv6 variants, while the proposed Compose short-form `address:host-port:container-port` interpolation and Qdrant URL construction insert raw colon-bearing values.","section_id":"1.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"remote-preflight-bounded-liveness","description":"A Tailscale ACL drop, stalled DNS lookup, half-open TCP connection, or service that accepts but never answers can hang `gobby install` instead of producing the promised per-service failure.","finding_id":"F-R2-REMOTE-PREFLIGHT-DEADLINES","fix":"Specify bounded connect and operation timeouts plus one overall preflight deadline, use no automatic retries for M0 unless a bounded policy is justified, clean up clients on timeout/cancellation, and add timeout-specific service diagnostics and tests.","location":"P1 / § 1.3","prevention":"For each remote probe, specify temporal bounds and test refused, dropped, half-open, stalled-response, authentication-failed, and slow-success behavior.","principle":"A required preflight must terminate predictably for every network failure mode it is intended to diagnose.","root_cause":"The plan specifies probe operations and error messages but no connect timeout, read/query timeout, retry budget, overall deadline, or client cleanup behavior.","section_id":"1.3","severity":"blocking"},{"category":"traceability","check_key":"agent-cleanup-termination-machine-scope","description":"Daemon B can still fail or terminate daemon A's pending/running rows because the cleanup and pending-termination paths remain global despite §2.2's machine-scope promise.","finding_id":"F-R2-AGENT-SWEEP-SCOPE","fix":"Add `storage/agents/_cleanup.py`, `storage/agents/_termination.py`, `agents/agent_cleanup.py`, and their protocols/fakes to Targets; pass local `machine_id` through stale-running, stale-pending, and termination reconciliation, and test that remote rows receive no probe or mutation.","location":"P2 / § 2.2","prevention":"Trace lifecycle requirements through storage mixins, host protocols/fakes, orchestrator call-throughs, and local process/tmux effects rather than stopping at the monitor facade.","principle":"Every lifecycle path that interprets shared agent rows against local process or tmux state must filter ownership before reading or mutating them.","root_cause":"The plan targets `_queries.py` and `lifecycle_monitor.py`, but stale-pending SQL is in `_cleanup.py`, termination-candidate selection is in `_termination.py`, and the stale-sweep call-through is in `agent_cleanup.py`; those paths are unscoped and absent from Targets.","section_id":"2.2","severity":"blocking"},{"category":"traceability","check_key":"active-run-local-global-api-split","description":"Making `list_active` require a local machine ID without updating all callers leaves destructive local consumers global or silently weakens cross-machine build/task invariants by localizing consumers that must remain global.","finding_id":"F-R2-ACTIVE-RUN-CALLER-SEMANTICS","fix":"Introduce explicitly named machine-scoped and global active-run query APIs; target and migrate agent health, memory watchdog, idle/restart reconciliation, websocket tmux, task recovery, relevant protocols/fakes, and global build/task consumers; test both semantics. Check and decompose `lifecycle_monitor.py` if its projected edit reaches the 1,000-line ceiling.","location":"P2 / § 2.2","prevention":"Inventory every caller before changing shared-query semantics, classify it as local-resource or global-metadata, and pin both classes with cross-machine regression tests.","principle":"Local-resource queries and global metadata invariants need separate, explicit APIs when they consume the same shared rows.","root_cause":"`list_active` has many production consumers beyond the targeted lifecycle monitor. Health, memory, idle/restart, websocket tmux, and task recovery need local scope, while build observability/control and task-transition checks deliberately need global visibility.","section_id":"2.2","severity":"blocking"},{"category":"traceability","check_key":"transcript-fallback-owner-caller-closure","description":"The new owner-aware fallback signature is not closed over production callers, so remote sessions can still trigger local path/cache/glob work or have a non-owner machine's path persisted.","finding_id":"F-R2-TRANSCRIPT-FALLBACK-CALLERS","fix":"Add `sessions/transcript_reader.py` and `agents/watchdog/transcript_resolver.py` to Targets; require owner/local identities at every fallback call, reject mismatches before stored-path stat/cache/glob, forbid remote-row updates, and add reader/messages and watchdog tests.","location":"P2 / § 2.3","prevention":"Trace every fallback scanner to all callers and shared-row writers, then test refusal through each production entry point before filesystem access.","principle":"A filesystem fallback must receive authoritative owner and local machine identities before any stat, cache lookup, glob, or persistence.","root_cause":"`TranscriptReader` and `WatchdogTranscriptResolver` also call `find_transcript_on_disk` but are absent from Targets; `TranscriptReader` can additionally persist a re-derived path to the shared session row.","section_id":"2.3","severity":"blocking"},{"category":"traceability","check_key":"session-filesystem-git-consumer-closure","description":"Guarding the listed analyzer and summary paths does not stop these earlier or independent consumers from reading another machine's supposed local paths or executing git in a locally resolved directory.","finding_id":"F-R2-SESSION-FILESYSTEM-CONSUMERS","fix":"Add `cli/sessions.py::create_handoff`, `mcp_proxy/tools/sessions/_commits.py`, and `mcp_proxy/tools/sessions/_summary_metadata.py` to Targets; centralize ownership refusal before every filesystem or git operation and add remote-owner tests at each seam.","location":"P2 / § 2.3","prevention":"Sweep scheduled, HTTP, CLI, and MCP session surfaces for `Path`, transcript, workspace, and git operations and test each from a non-owning daemon.","principle":"Every session operation that touches local transcripts, workspaces, or repositories must verify local ownership before the first local probe.","root_cause":"CLI `create_handoff`, the session commits MCP tool, and compact summary metadata resolve transcripts/workspaces and run git for arbitrary sessions, but none is listed in §2.3 Targets.","section_id":"2.3","severity":"blocking"},{"category":"bad-sequencing","check_key":"schema-head-enclosing-advisory-lock","description":"A newer build can advance the schema after an older build's head check but before its pending decision, allowing the older build to exit successfully against an unsupported newer schema; targeting only the hub method cannot close that race.","finding_id":"F-R2-MIGRATION-LOCK-SPAN","fix":"Add `MigrationRunner` lock orchestration to Targets and define one lock-owning API spanning bookkeeping initialization, head inspection, pending discovery/revalidation, baseline decision, and all application decisions; keep per-version CAS as defense in depth and add barrier-controlled transactional, non-transactional, no-pending, and first-write tests.","location":"P3 / § 3.1","prevention":"Test old/new build concurrency with barriers at head inspection, pending discovery, baseline, transactional application, and non-transactional application.","principle":"Schema-head validation and the complete migration decision must occur under one uninterrupted lock before any baseline or migration write.","root_cause":"`PostgresHubDatabase.apply_migrations` has no enclosing migration lock; `MigrationRunner.apply_pending` discovers applied/pending versions before locking and acquires locks per migration, while non-transactional migrations use a separate per-migration session lock and baseline handling precedes the runner.","section_id":"3.1","severity":"blocking"}],"reviewer_session":"bd50b5ab-605d-4e00-b71f-a106bfcfd719","round":2,"verdict":"needs_review"},"session_id":"d5f358bb-1c03-4d42-860a-67dc4205a48e"}
```

**Human handoff after round 2 cap** `kind: verification`

- trigger: finalized round 2 returned `needs_review`, reaching the configured two-round cap
- authority: operator-approved M0 recovery sequence requires explicit human handoff after final finding disposition
- evidence: 4e6fd6d3-182d-4462-a96a-6191ac0e06e6
- findings_disposition: all ten blocking findings accepted with rationale and repaired before finalization
- completed_plan_review_rounds: 2
- next_action: derive and atomically apply the coordinator handoff manifest from deterministic routing decisions
- round_3: prohibited

## M1 Task Manifest
`kind: manifest`

```yaml
- title: Add datastore_mode bootstrap key and remote-DSN support
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: "1.1.1: `datastore_mode` parses with `local` default and errors\
    \ on unknown values. test: `tests/config/test_bootstrap.py::test_datastore_mode_parsing`.\n\
    1.1.2: Remote mode accepts a non-loopback postgresql DSN that local mode rejects.\
    \ test: `tests/config/test_bootstrap.py::test_remote_mode_allows_nonloopback_database_url`.\n\
    1.1.3: Remote mode rejects loopback DSNs with an actionable error. test: `tests/config/test_bootstrap.py::test_remote_mode_rejects_loopback_database_url`.\n\
    1.1.4: `gobby start`/`gobby stop` skip compose management in remote mode. test:\
    \ `tests/cli/test_daemon_remote_mode.py::test_start_skips_services_in_remote_mode`.\n\
    1.1.5: `datastore_mode` flows bootstrap \u2192 DaemonConfig. symbol: `gobby.config.bootstrap.BootstrapConfig`.\n\
    1.1.6: Rust bootstrap reader tolerates the new key. test: `crates/gcore/src/bootstrap.rs`\
    \ unit test `reads_bootstrap_with_datastore_mode`.\n1.1.7: `gobby restart` in\
    \ remote mode performs no compose interaction and requires no Docker (shared `_do_stop`/start\
    \ path, not just the public commands). test: `tests/cli/test_daemon_remote_mode.py::test_restart_skips_services_in_remote_mode`.\n\
    1.1.8: The regenerated bundled-content manifest authenticates the edited bootstrap\
    \ template and matches a fresh bundled-tree inventory. test: `tests/install/test_bundled_content_manifest.py::test_bundled_content_manifest_matches_tree`."
  labels:
  - covers:m0-shared-datastores-bridge:1.1:1.1.1
  - covers:m0-shared-datastores-bridge:1.1:1.1.2
  - covers:m0-shared-datastores-bridge:1.1:1.1.3
  - covers:m0-shared-datastores-bridge:1.1:1.1.4
  - covers:m0-shared-datastores-bridge:1.1:1.1.5
  - covers:m0-shared-datastores-bridge:1.1:1.1.6
  - covers:m0-shared-datastores-bridge:1.1:1.1.7
  - covers:m0-shared-datastores-bridge:1.1:1.1.8
  tdd: true
  source_section: '1.1'
  implementation_domain: backend
- title: Compose bind-address knob, published host, and gobby datastores expose
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: "1.2.1: Both compose templates use the bind-address variable\
    \ and remain identical. test: `tests/cli/test_compose_bind_address.py::test_templates_parameterized_and_identical`.\n\
    1.2.2: `resolve_compose_runtime` injects `GOBBY_SERVICES_BIND_ADDRESS` from `BootstrapConfig.services_bind_address`\
    \ without opening PostgreSQL. symbol: `gobby.cli.installers.compose_env.resolve_compose_runtime`.\n\
    1.2.3: FalkorDB/Qdrant installers derive config endpoints from `databases.published_host`\
    \ and no longer hardcode localhost. test: `tests/cli/test_compose_bind_address.py::test_installers_respect_published_host`.\n\
    1.2.4: `gobby datastores expose` persists keys, rewrites endpoint config, and\
    \ is idempotent; IPv4 loopback, concrete local Tailscale IPv4, and DNS published\
    \ hosts are accepted, while IPv6 bind/published-host literals and both wildcards\
    \ are refused before configuration or compose state changes. test: `tests/cli/test_datastores_expose.py::test_expose_sets_keys_and_endpoints`.\n\
    1.2.5: Expose command exists and is registered through the root CLI group in `src/gobby/cli/__init__.py`;\
    \ `gobby datastores expose --help` resolves via CliRunner. test: `tests/cli/test_datastores_expose.py::test_expose_registered_at_root`.\n\
    1.2.6: Cold start after exposure: with PostgreSQL down, the public `gobby start`\
    \ command loads bootstrap first, revives PostgreSQL on the tailnet address, then\
    \ resolves DB-backed runtime configuration, proven by an expose \u2192 stop \u2192\
    \ public-start sequence. test: `tests/cli/test_datastores_expose.py::test_cold_start_reads_bind_from_bootstrap`.\n\
    1.2.7: Expose failure injection (compose failure or readiness timeout) restores\
    \ the prior bind/compose state and leaves shared endpoint keys unchanged. test:\
    \ `tests/cli/test_datastores_expose.py::test_expose_failure_restores_prior_state`.\n\
    1.2.8: Deterministic interleavings of expose/expose, expose/start, expose/stop,\
    \ and expose/installer refresh serialize under one bounded lifecycle lock; a losing\
    \ transition cannot roll back or commit over the winner. test: `tests/cli/test_datastores_expose.py::test_managed_services_transitions_are_serialized`.\n\
    1.2.9: Address validation covers IPv4 loopback, IPv6 loopback, Tailscale IPv4,\
    \ Tailscale IPv6, `0.0.0.0`, `::`, DNS hosts, and IP-literal published hosts,\
    \ with accepted values round-tripped through Compose and endpoint serialization.\
    \ test: `tests/cli/test_datastores_expose.py::test_expose_address_contract`."
  labels:
  - covers:m0-shared-datastores-bridge:1.2:1.2.1
  - covers:m0-shared-datastores-bridge:1.2:1.2.2
  - covers:m0-shared-datastores-bridge:1.2:1.2.3
  - covers:m0-shared-datastores-bridge:1.2:1.2.4
  - covers:m0-shared-datastores-bridge:1.2:1.2.5
  - covers:m0-shared-datastores-bridge:1.2:1.2.6
  - covers:m0-shared-datastores-bridge:1.2:1.2.7
  - covers:m0-shared-datastores-bridge:1.2:1.2.8
  - covers:m0-shared-datastores-bridge:1.2:1.2.9
  tdd: true
  source_section: '1.2'
  implementation_domain: backend
- title: Remote-mode gobby install with reachability preflight
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  - '1.2'
  validation_criteria: '1.3.1: Remote-mode install performs no compose/provisioning
    calls. test: `tests/cli/test_cli_install.py::test_remote_mode_skips_datastore_provisioning`.

    1.3.2: Preflight failure for each of the three services yields an actionable per-service
    error. test: `tests/cli/test_cli_install.py::test_remote_mode_preflight_errors`.

    1.3.3: Missing KEK/token files produce copy-from-hub guidance and never regenerate.
    test: `tests/cli/test_cli_install.py::test_remote_mode_kek_token_guidance`.

    1.3.4: Remote-mode install succeeds through preflight with the Docker executable
    and daemon entirely unavailable. test: `tests/cli/test_cli_install.py::test_remote_mode_install_without_docker`.

    1.3.5: `src/gobby/cli/install.py` and the new helper module are each below the
    1,000-line ceiling after the change. behavior: "install.py remains thin orchestration
    under the line ceiling" in `src/gobby/cli/install.py`.

    1.3.6: Remote preflight enforces the 3-second connect, 5-second operation, and
    15-second overall deadlines with zero retries and client cleanup across refused,
    dropped, half-open, stalled, authentication-failed, and slow-success cases. test:
    `tests/cli/test_cli_install.py::test_remote_mode_preflight_deadlines`.'
  labels:
  - covers:m0-shared-datastores-bridge:1.3:1.3.1
  - covers:m0-shared-datastores-bridge:1.3:1.3.2
  - covers:m0-shared-datastores-bridge:1.3:1.3.3
  - covers:m0-shared-datastores-bridge:1.3:1.3.4
  - covers:m0-shared-datastores-bridge:1.3:1.3.5
  - covers:m0-shared-datastores-bridge:1.3:1.3.6
  tdd: true
  source_section: '1.3'
  implementation_domain: backend
- title: 'Migration: machine_id on worktrees, clones, agent_runs, cron_runs'
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: '2.1.1: Migration adds `UUID NOT NULL REFERENCES machines(id)`
    columns, swaps unique indexes, backfills only through the per-table conversion
    matrix, and aborts with row diagnostics rather than assigning a guessed or sentinel
    machine when ownership cannot be resolved. file: `src/gobby/storage/migrations/372_machine_scope.sql`.

    2.1.2: Baseline schema matches the migrated shape. file: `src/gobby/storage/postgres_baseline_schema.sql`.

    2.1.3: Same (project, branch) worktree can exist for two machine_ids; same path
    string can exist for two machine_ids. test: `tests/storage/test_worktrees.py::test_worktree_uniqueness_is_machine_scoped`.

    2.1.4: Worktree/clone/agent-run/cron-run creation stamps the local machine_id,
    and the models round-trip it through from_row and serialization. test: `tests/storage/test_machine_scope_writers.py::test_creation_paths_stamp_machine_id`.

    2.1.5: Every legal legacy row shape is covered: linked rows convert; NULL-session
    worktrees/clones, unresolvable agent_runs, undrained cron state, and ownerless
    terminal cron rows each abort with row diagnostics; the documented remediation
    then yields a successful rerun. test: `tests/storage/test_machine_scope_migration.py::test_legacy_shapes_convert_or_abort_with_remediation`.'
  labels:
  - covers:m0-shared-datastores-bridge:2.1:2.1.1
  - covers:m0-shared-datastores-bridge:2.1:2.1.2
  - covers:m0-shared-datastores-bridge:2.1:2.1.3
  - covers:m0-shared-datastores-bridge:2.1:2.1.4
  - covers:m0-shared-datastores-bridge:2.1:2.1.5
  tdd: true
  source_section: '2.1'
  implementation_domain: backend
- title: Scope agent lifecycle and workspace readers by machine_id
  category: code
  task_type: feature
  depends_on:
  - '2.1'
  validation_criteria: '2.2.1: Lifecycle monitor queries are machine-scoped. symbol:
    `gobby.storage.agents._queries.list_active`.

    2.2.2: A running agent row owned by another machine is never marked dead/stale
    by the local monitor. test: `tests/agents/test_lifecycle_monitor.py::test_monitor_ignores_other_machines_runs`.

    2.2.3: Stale-cleanup and deletion refuse other machines'' worktree/clone rows.
    test: `tests/storage/test_worktrees.py::test_cleanup_scoped_to_local_machine`.

    2.2.4: Claim/reuse never selects another machine''s workspace records. test: `tests/storage/test_worktrees.py::test_claim_scoped_to_local_machine`.

    2.2.5: Two-daemon regression: B''s isolation maintenance (expired cleanup and
    missing-path sweeps) leaves A''s rows and filesystem untouched. test: `tests/runner_maintenance/test_isolation_machine_scope.py::test_missing_path_sweep_ignores_remote_rows`.

    2.2.6: Stale-running, stale-pending, and pending-termination sweeps pass local
    machine identity through storage and cleanup orchestration; remote rows receive
    no probe or mutation. test: `tests/agents/test_lifecycle_monitor.py::test_cleanup_and_termination_ignore_other_machine_runs`.

    2.2.7: Every active-run caller is pinned to an explicit machine-scoped or global
    API, with cross-machine regressions proving local consumers ignore remote resources
    and build/task invariants retain global visibility. test: `tests/storage/agents/test_active_run_scope.py::test_local_and_global_active_run_apis`.

    2.2.8: `src/gobby/agents/lifecycle_monitor.py` and every new focused sibling remain
    below the 1,000-line ceiling. behavior: "lifecycle machine scoping stays below
    the production line ceiling" in `src/gobby/agents/lifecycle_monitor.py`.'
  labels:
  - covers:m0-shared-datastores-bridge:2.2:2.2.1
  - covers:m0-shared-datastores-bridge:2.2:2.2.2
  - covers:m0-shared-datastores-bridge:2.2:2.2.3
  - covers:m0-shared-datastores-bridge:2.2:2.2.4
  - covers:m0-shared-datastores-bridge:2.2:2.2.5
  - covers:m0-shared-datastores-bridge:2.2:2.2.6
  - covers:m0-shared-datastores-bridge:2.2:2.2.7
  - covers:m0-shared-datastores-bridge:2.2:2.2.8
  tdd: true
  source_section: '2.2'
  implementation_domain: backend
- title: Scope session background consumers to local-machine sessions
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: '2.3.1: Pending-transcript selection is machine-scoped. symbol:
    `gobby.storage.sessions._transcript.get_pending_transcript_sessions`.

    2.3.2: A remote-machine session is never processed, summarized, or watchdogged
    locally, and `transcript_processed` remains untouched by the non-owning daemon.
    test: `tests/sessions/test_machine_scoped_consumers.py::test_remote_sessions_skipped`.

    2.3.3: Fallback transcript scanners return None for remote-machine sessions. test:
    `tests/sessions/test_machine_scoped_consumers.py::test_fallback_scan_refuses_remote_sessions`.

    2.3.4: The on-demand summary route refuses a remote-machine session before touching
    the filesystem. test: `tests/sessions/test_machine_scoped_consumers.py::test_on_demand_summary_refuses_remote_sessions`.

    2.3.5: TranscriptReader and watchdog fallback reject remote ownership before stored-path
    stat, cache, glob, or persistence; remote rows remain unchanged. test: `tests/sessions/test_machine_scoped_consumers.py::test_reader_and_watchdog_refuse_remote_fallback`.

    2.3.6: CLI handoff, session commits MCP, and compact summary metadata refuse remote-owned
    sessions before filesystem or git access. test: `tests/sessions/test_machine_scoped_consumers.py::test_session_filesystem_surfaces_refuse_remote_owner`.'
  labels:
  - covers:m0-shared-datastores-bridge:2.3:2.3.1
  - covers:m0-shared-datastores-bridge:2.3:2.3.2
  - covers:m0-shared-datastores-bridge:2.3:2.3.3
  - covers:m0-shared-datastores-bridge:2.3:2.3.4
  - covers:m0-shared-datastores-bridge:2.3:2.3.5
  - covers:m0-shared-datastores-bridge:2.3:2.3.6
  tdd: true
  source_section: '2.3'
  implementation_domain: backend
- title: Scope cron reconcile and stale sweeps by machine_id
  category: code
  task_type: feature
  depends_on:
  - '2.1'
  validation_criteria: '2.4.1: Startup reconcile fails only runs whose `machine_id`
    exactly matches the local machine. symbol: `gobby.storage.cron_children._fail_remaining_active_runs`.

    2.4.2: Restarting one daemon leaves the other machine''s in-flight cron runs untouched.
    test: `tests/scheduler/test_cron_machine_scope.py::test_restart_does_not_fail_remote_runs`.

    2.4.3: Stale-run timeout and concurrency counting are machine-scoped. test: `tests/scheduler/test_cron_machine_scope.py::test_stale_sweep_and_slots_scoped`.'
  labels:
  - covers:m0-shared-datastores-bridge:2.4:2.4.1
  - covers:m0-shared-datastores-bridge:2.4:2.4.2
  - covers:m0-shared-datastores-bridge:2.4:2.4.3
  tdd: true
  source_section: '2.4'
  implementation_domain: backend
- title: Migration lockstep guard
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: '3.1.1: Startup against a newer-than-known schema fails with
    the actionable error and performs no writes, with bookkeeping, head inspection,
    baseline decision, pending discovery/revalidation, and all migration application
    enclosed by one advisory lock. test: `tests/storage/test_migration_lockstep.py::test_newer_schema_fails_closed`.

    3.1.2: Guard lives on the shared runtime path. symbol: `gobby.storage.hub.postgres.PostgresHubDatabase.apply_migrations`.

    3.1.3: Barrier-controlled old/new build races at head inspection, pending discovery,
    baseline first write, transactional application, non-transactional application,
    and the no-pending exit all serialize; the older build cannot report success against
    a newer head. test: `tests/storage/test_migration_lockstep.py::test_enclosing_lock_serializes_every_schema_decision`.'
  labels:
  - covers:m0-shared-datastores-bridge:3.1:3.1.1
  - covers:m0-shared-datastores-bridge:3.1:3.1.2
  - covers:m0-shared-datastores-bridge:3.1:3.1.3
  tdd: true
  source_section: '3.1'
  implementation_domain: backend
- title: Shared-datastores runbook
  category: docs
  task_type: feature
  depends_on: []
  validation_criteria: '3.2.1: Guide documents hub exposure, client setup, M0 boundaries,
    and deferrals as above. file: `docs/guides/shared-stack.md`.

    3.2.2: The unsupported-remote-Postgres claim is gone and replaced by the `datastore_mode:
    remote` contract. behavior: "remote datastores supported path" in `docs/guides/shared-stack.md`.'
  labels:
  - covers:m0-shared-datastores-bridge:3.2:3.2.1
  - covers:m0-shared-datastores-bridge:3.2:3.2.2
  tdd: false
  source_section: '3.2'
  assigned_agent: tech-writer
- title: Two-machine end-to-end acceptance
  category: test
  task_type: feature
  depends_on:
  - '1.1'
  - '1.2'
  - '1.3'
  - '2.1'
  - '2.2'
  - '2.3'
  - '2.4'
  - '3.1'
  - '3.2'
  validation_criteria: '4.1.1: Automated two-daemon e2e covers items 3-7 against an
    isolated temporary PostgreSQL/Qdrant/FalkorDB stack, including the cross-daemon
    vector-and-graph memory round-trip against those provisioned services. test: `tests/e2e/test_shared_datastores_m0.py::test_two_machine_continuity_and_isolation`.

    4.1.2: Manual checklist for items 1-2 and 8 ships in the runbook. behavior: "M0
    acceptance checklist" in `docs/guides/shared-stack.md`.'
  labels:
  - covers:m0-shared-datastores-bridge:4.1:4.1.1
  - covers:m0-shared-datastores-bridge:4.1:4.1.2
  tdd: false
  source_section: '4.1'
  assigned_agent: backend-developer
```
