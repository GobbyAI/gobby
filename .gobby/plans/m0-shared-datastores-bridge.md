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
  `src/gobby/install/shared/config/bootstrap.yaml`.
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

### 1.2 Compose bind-address knob, published host, and gobby datastores expose [category: code]
`kind: deliverable`

Targets:
- `src/gobby/data/docker-compose.services.yml::*` — scope-reason: parameterize datastore port bindings across all three compose services
- `crates/gcore/assets/docker-compose.services.yml::*` — scope-reason: keep the embedded compose template byte-identical with the Python template
- `src/gobby/cli/installers/compose_env.py::resolve_compose_runtime`
- `src/gobby/cli/installers/falkor.py::_update_config`
- `src/gobby/cli/installers/qdrant.py::_update_config`
- `src/gobby/cli/__init__.py::*` — scope-reason: root CLI groups are imported and registered here; the new datastores group must be added to the registry
- `src/gobby/cli/daemon.py::_services_start`
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
- `install_falkordb._update_config` (`falkor.py:158-163`) and the qdrant installer's
  `_update_config` currently hardcode `127.0.0.1`/`http://localhost:6333` into the
  SHARED config_store — a hub reinstall would clobber every client's endpoints. Both
  must derive host/url from `databases.published_host`, falling back to localhost when
  unset.
- New hub-side command `gobby datastores expose --bind <addr> --host <name>`, ordered
so a failure cannot leave clients pointed at unreachable endpoints: (1) validate
inputs (`--bind` accepts loopback or a concrete Tailscale interface address and
rejects wildcard addresses `0.0.0.0`/`::` while Qdrant authentication is deferred);
(2) stage: write the machine-local bootstrap bind key and re-run compose up with the
candidate binding; (3) health-check all three services on the new binding;
(4) commit: only then rewrite the shared dial endpoints — `databases.published_host`,
`databases.qdrant.url` → `http://<host>:<qdrant_port>`, `databases.falkordb.host` →
`<host>` — and reassert the `unless-stopped` restart policy. On any staging or
health-check failure, restore the prior bind value and compose state and leave the
shared endpoint keys untouched. Idempotent.
- No Qdrant API key in this deliverable (deferred; documented in 3.2). No hand edits to
  the installed compose file are ever required — the knob lives in env/config_store, so
  `_refresh_unified_compose` (`falkor.py:118`) template overwrites are harmless.

**Acceptance:**

- 1.2.1 - Both compose templates use the bind-address variable and remain identical. test: `tests/cli/test_compose_bind_address.py::test_templates_parameterized_and_identical`.
- 1.2.2 - `resolve_compose_runtime` injects `GOBBY_SERVICES_BIND_ADDRESS` from `databases.bind_address`. symbol: `gobby.cli.installers.compose_env.resolve_compose_runtime`.
- 1.2.3 - FalkorDB/Qdrant installers derive config endpoints from `databases.published_host` and no longer hardcode localhost. test: `tests/cli/test_compose_bind_address.py::test_installers_respect_published_host`.
- 1.2.4 - `gobby datastores expose` persists keys, rewrites endpoint config, and is idempotent; loopback and a concrete Tailscale address are accepted, while `0.0.0.0` and `::` are refused before configuration or compose state changes. test: `tests/cli/test_datastores_expose.py::test_expose_sets_keys_and_endpoints`.
- 1.2.5 - Expose command exists and is registered through the root CLI group in `src/gobby/cli/__init__.py`; `gobby datastores expose --help` resolves via CliRunner. test: `tests/cli/test_datastores_expose.py::test_expose_registered_at_root`.
- 1.2.6 - Cold start after exposure: with PostgreSQL down, `_services_start` still binds the tailnet address because the bind source is the machine-local bootstrap key, proven by an expose → stop → cold-start sequence. test: `tests/cli/test_datastores_expose.py::test_cold_start_reads_bind_from_bootstrap`.
- 1.2.7 - Expose failure injection (compose failure or readiness timeout) restores the prior bind/compose state and leaves shared endpoint keys unchanged. test: `tests/cli/test_datastores_expose.py::test_expose_failure_restores_prior_state`.

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
- `src/gobby/agents/lifecycle_monitor.py::*` — scope-reason: thread the local machine id through all lifecycle query call sites
- `src/gobby/storage/worktrees.py::*` — scope-reason: machine-scope claim, lookup, stale discovery, and cleanup surfaces
- `src/gobby/storage/clones.py::*` — scope-reason: machine-scope claim, lookup, stale discovery, and cleanup surfaces
- `src/gobby/dispatch/daemon_resume.py::*` — scope-reason: restrict daemon-stop resume candidates to local tmux and workspace state
- `src/gobby/runner_maintenance/isolation.py::*` — scope-reason: the recurring isolation maintenance loop deletes expired workspaces and prunes records whose paths are missing on local disk; both sweeps must be local-machine-only
- `src/gobby/hooks/event_handlers/_misc.py::MiscEventHandlerMixin.handle_worktree_remove`

Every reader that inspects local processes/tmux or touches the local filesystem must
see only rows owned by this machine:

- `list_active` (`storage/agents/_queries.py:197`) gains a required `machine_id`
  parameter; `AgentLifecycleMonitor` (`agents/lifecycle_monitor.py:87`) passes the
  local id — daemon B must never poll daemon A's tmux runs and mark them dead. Same
  scoping for `cleanup_stale_pending_runs`, `reconcile_pending_terminations`,
  `refresh_active_run_dispatch_mutexes`, and `list_daemon_stop_resume_candidates`
  (consumed by `dispatch/daemon_resume.py:105` — resume metadata references local
  tmux sessions and paths).
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

**Acceptance:**

- 2.2.1 - Lifecycle monitor queries are machine-scoped. symbol: `gobby.storage.agents._queries.list_active`.
- 2.2.2 - A running agent row owned by another machine is never marked dead/stale by the local monitor. test: `tests/agents/test_lifecycle_monitor.py::test_monitor_ignores_other_machines_runs`.
- 2.2.3 - Stale-cleanup and deletion refuse other machines' worktree/clone rows. test: `tests/storage/test_worktrees.py::test_cleanup_scoped_to_local_machine`.
- 2.2.4 - Claim/reuse never selects another machine's workspace records. test: `tests/storage/test_worktrees.py::test_claim_scoped_to_local_machine`.
- 2.2.5 - Two-daemon regression: B's isolation maintenance (expired cleanup and missing-path sweeps) leaves A's rows and filesystem untouched. test: `tests/runner_maintenance/test_isolation_machine_scope.py::test_missing_path_sweep_ignores_remote_rows`.

### 2.3 Scope session background consumers to local-machine sessions [category: code]
`kind: deliverable`

Targets:
- `src/gobby/storage/sessions/_transcript.py::_TranscriptMixin.get_pending_transcript_sessions`
- `src/gobby/sessions/summarize.py::generate_session_summaries`
- `src/gobby/sessions/analyzer.py::TranscriptAnalyzer`
- `src/gobby/agents/idle_check_handler.py::IdleCheckHandler.check_idle_agents`
- `src/gobby/sessions/transcript_paths.py::find_transcript_on_disk`
- `src/gobby/hooks/event_handlers/_session_start/transcripts.py::derive_transcript_path`
- `src/gobby/tasks/transcript_evidence.py::*` — scope-reason: ownership-bearing caller of the on-disk fallback scanner; must pass the session's owner machine into the refusal boundary
- `src/gobby/hooks/event_handlers/_session_start/flow.py::*` — scope-reason: ownership-bearing caller wiring session machine identity into derive_transcript_path
- `src/gobby/sessions/summary_generation.py::generate_summary`
- `src/gobby/servers/routes/sessions/analytics.py::generate_session_summary`

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
- `src/gobby/storage/migrations.py::MigrationUnsupportedError`
- `src/gobby/storage/migrations.py::latest_known_version`

Today `MigrationRunner.apply_pending` (`storage/migrations.py:122`) is advisory-locked
and CAS-safe against concurrent migrators but silently ignores recorded versions it
does not know — an older build runs happily against a newer schema.

In `PostgresHubDatabase.apply_migrations` (`storage/hub/postgres.py:390`), after
ensuring the bookkeeping table: `SELECT MAX(version) FROM schema_migrations`; if it
exceeds `latest_known_version()` (exists at `storage/migrations.py:439`), raise a fatal
`MigrationUnsupportedError`: "hub schema is vX but this gobby build knows vY — update
gobby on this machine." The head check runs INSIDE the existing migration advisory
lock, serialized with application, so a concurrent migrator cannot advance the schema
between the check and this build's first write. Every CLI and the daemon reach the
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

- 3.1.1 - Startup against a newer-than-known schema fails with the actionable error and performs no writes, with the head check serialized under the migration advisory lock. test: `tests/storage/test_migration_lockstep.py::test_newer_schema_fails_closed`.
- 3.1.2 - Guard lives on the shared runtime path. symbol: `gobby.storage.hub.postgres.PostgresHubDatabase.apply_migrations`.

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

