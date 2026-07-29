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
- Ordering invariant: 2.1 and 3.1 must land before a second machine points at the
  shared database.

## P1: Remote datastore topology
`kind: framing`

**Goal**: A client daemon can start against remote datastores; the hub can expose its
Docker stack on the tailnet without hand-editing generated files.

### 1.1 Add datastore_mode bootstrap key and remote-DSN support [category: code]
`kind: deliverable`

Target: `src/gobby/config/bootstrap.py`, `src/gobby/config/app.py`,
`src/gobby/cli/daemon.py`, `src/gobby/install/shared/config/bootstrap.yaml`,
`crates/gcore/src/bootstrap.rs`

Add top-level bootstrap key `datastore_mode: Literal["local", "remote"] = "local"`:

- Parse in `load_bootstrap()`: absent → `local`; unknown value → hard
  `BootstrapConfigError` (fail loud). Emit in `to_config_dict()` so `DaemonConfig`
  carries it.
- `_validate_managed_database_url` (`bootstrap.py:200`) runs only when
  `datastore_mode == "local"`. In `remote` mode still require `postgres(ql)://` scheme
  and a full DSN with credentials and hostname; loopback hostnames in remote mode are a
  hard error pointing the user back to `local` (a mode that lies about topology is
  worse than a failed parse).
- `gobby start` skips `_services_start` (`cli/daemon.py:67`) and `gobby stop` skips
  `_services_stop` (`cli/daemon.py:165`) in remote mode — a client machine must never
  run compose against a stack it does not own. Log one line stating remote mode and
  the target host.
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

### 1.2 Compose bind-address knob, published host, and gobby datastores expose [category: code]
`kind: deliverable`

Target: `src/gobby/data/docker-compose.services.yml`,
`crates/gcore/assets/docker-compose.services.yml`,
`src/gobby/cli/installers/compose_env.py`, `src/gobby/cli/installers/falkor.py`,
`src/gobby/cli/installers/qdrant.py`, `src/gobby/cli/installers/postgres.py`,
`src/gobby/cli/daemon.py`, new `src/gobby/cli/datastores.py`

Parameterize the hub-side bind address and make exposure survive reinstalls:

- Both compose template copies (they must stay byte-identical): change every hardcoded
  `"127.0.0.1:${PORT}:..."` binding for postgres, qdrant (HTTP + gRPC), and falkordb to
  `"${GOBBY_SERVICES_BIND_ADDRESS:-127.0.0.1}:${PORT}:..."`. The FalkorDB browser port
  stays loopback-only.
- New config_store keys: `databases.bind_address` (compose bind, e.g. Tailscale IP) and
  `databases.published_host` (DNS name clients dial, e.g. `gobby-box.tailnet.ts.net`).
  `resolve_compose_runtime` injects `GOBBY_SERVICES_BIND_ADDRESS` from
  `databases.bind_address` (default `127.0.0.1`); process-env override continues to
  work because compose_env merges `canonical | dict(os.environ)`.
- `install_falkordb._update_config` (`falkor.py:158-163`) and the qdrant installer's
  `_update_config` currently hardcode `127.0.0.1`/`http://localhost:6333` into the
  SHARED config_store — a hub reinstall would clobber every client's endpoints. Both
  must derive host/url from `databases.published_host`, falling back to localhost when
  unset.
- New hub-side command `gobby datastores expose --bind <addr> --host <name>`: persists
both keys, rewrites `databases.qdrant.url` → `http://<host>:<qdrant_port>` and
`databases.falkordb.host` → `<host>`, re-runs compose up with the new binding, and
reasserts the `unless-stopped` restart policy. Idempotent; `--bind` accepts loopback
or a concrete Tailscale interface address and rejects wildcard addresses
(`0.0.0.0` and `::`) while Qdrant authentication is deferred.
- No Qdrant API key in this deliverable (deferred; documented in 3.2). No hand edits to
  the installed compose file are ever required — the knob lives in env/config_store, so
  `_refresh_unified_compose` (`falkor.py:118`) template overwrites are harmless.

**Acceptance:**

- 1.2.1 - Both compose templates use the bind-address variable and remain identical. test: `tests/cli/test_compose_bind_address.py::test_templates_parameterized_and_identical`.
- 1.2.2 - `resolve_compose_runtime` injects `GOBBY_SERVICES_BIND_ADDRESS` from `databases.bind_address`. symbol: `gobby.cli.installers.compose_env.resolve_compose_runtime`.
- 1.2.3 - FalkorDB/Qdrant installers derive config endpoints from `databases.published_host` and no longer hardcode localhost. test: `tests/cli/test_compose_bind_address.py::test_installers_respect_published_host`.
- 1.2.4 - `gobby datastores expose` persists keys, rewrites endpoint config, and is idempotent; loopback and a concrete Tailscale address are accepted, while `0.0.0.0` and `::` are refused before configuration or compose state changes. test: `tests/cli/test_datastores_expose.py::test_expose_sets_keys_and_endpoints`.
- 1.2.5 - Expose command exists and is registered. file: `src/gobby/cli/datastores.py`.

### 1.3 Remote-mode gobby install with reachability preflight [category: code] (depends: 1.1)
`kind: deliverable`

Target: `src/gobby/cli/install.py`, `src/gobby/cli/installers/postgres.py`,
`src/gobby/cli/installers/qdrant.py`, `src/gobby/cli/installers/falkor.py`

`gobby install` on a machine whose bootstrap.yaml says `datastore_mode: remote`:

- Skips postgres/qdrant/falkordb provisioning and all compose interaction. In
  particular `install_postgres` must not take its existing-bootstrap branch
  (`postgres.py:253-270`) that reuses the remote `database_url` as local compose
  credentials — that would provision a pointless local Postgres seeded with the hub's
  credentials.
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

## P2: Machine scoping for shared-database safety
`kind: framing`

**Goal**: Two daemons on one database cannot damage each other's filesystem-backed
state or lifecycle.

### 2.1 Migration: machine_id on worktrees, clones, agent_runs, cron_runs [category: code]
`kind: deliverable`

Target: `src/gobby/storage/migrations/343_machine_scope.sql`, `src/gobby/storage/postgres_baseline_schema.sql`, `src/gobby/storage/worktrees.py`, `src/gobby/storage/clones.py`, `src/gobby/storage/agents/_manager.py`, `src/gobby/storage/cron_runs.py`, `src/gobby/agents/isolation_worktree.py`, `src/gobby/agents/isolation_clone.py`

The migration file is new; use the next free number after
`src/gobby/storage/migrations/342_task_validation_epoch.sql`.

Schema changes (baseline + migration kept in sync per the repo's migration-contract
rules):

- `worktrees`: ADD `machine_id TEXT NOT NULL`. Backfill only from the authoritative
`sessions.machine_id` join via `agent_session_id`; an unresolved owner aborts the
migration with diagnostics instead of guessing.
Replace `idx_worktrees_path` UNIQUE(worktree_path) → UNIQUE(machine_id,
worktree_path); replace `idx_worktrees_branch` UNIQUE(project_id, branch_name) →
UNIQUE(project_id, branch_name, machine_id). Branch uniqueness is per-machine
because local git branches do not sync between machines.
- `clones`: ADD `machine_id TEXT NOT NULL` (same authoritative-session backfill and
fail-closed handling); `idx_clones_path` →
UNIQUE(machine_id, clone_path).
- `agent_runs`: ADD `machine_id TEXT NOT NULL` (same authoritative-session backfill
and fail-closed handling); new index `(machine_id, status)`.
- `cron_runs`: ADD `machine_id TEXT NOT NULL`; backfill through the run's
authoritative session ownership and abort on an unresolved row.

Writers stamp `get_machine_id()` at creation: worktree create
(`src/gobby/storage/worktrees.py:118`), clone create (`src/gobby/storage/clones.py:137`),
agent_run create (`src/gobby/storage/agents/_manager.py`), cron run create
(`src/gobby/storage/cron_runs.py:34`, scheduler passes it). Dataclasses (`Worktree`,
clone and agent-run models) gain the field.

**Acceptance:**

- 2.1.1 - Migration adds NOT NULL columns, swaps unique indexes, backfills only through authoritative session ownership, and aborts with row diagnostics rather than assigning a guessed or sentinel machine when ownership cannot be resolved. file: `src/gobby/storage/migrations/343_machine_scope.sql`.
- 2.1.2 - Baseline schema matches the migrated shape. file: `src/gobby/storage/postgres_baseline_schema.sql`.
- 2.1.3 - Same (project, branch) worktree can exist for two machine_ids; same path string can exist for two machine_ids. test: `tests/storage/test_worktrees.py::test_worktree_uniqueness_is_machine_scoped`.
- 2.1.4 - Worktree/clone/agent-run/cron-run creation stamps the local machine_id. test: `tests/storage/test_machine_scope_writers.py::test_creation_paths_stamp_machine_id`.

### 2.2 Scope agent lifecycle and workspace readers by machine_id [category: code] (depends: 2.1)
`kind: deliverable`

Target: `src/gobby/storage/agents/_queries.py`, `src/gobby/agents/lifecycle_monitor.py`,
`src/gobby/storage/worktrees.py`, `src/gobby/storage/clones.py`,
`src/gobby/dispatch/daemon_resume.py`, `src/gobby/hooks/event_handlers/_misc.py`

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
- Claim/reuse/path-lookup surfaces (`claim`, `claim_if_available`, `get_by_path`,
  `get_by_branch`) are machine-scoped so daemon B never claims or reuses A's
  workspace records.
- Time-based session pause/expire (`sessions/session_lifecycle.py:252`) deliberately
  stays global: pausing a stale session belonging to the asleep machine is correct
  and keeps the pack-up scenario clean. Add a comment pinning this decision.

**Acceptance:**

- 2.2.1 - Lifecycle monitor queries are machine-scoped. symbol: `gobby.storage.agents._queries.list_active`.
- 2.2.2 - A running agent row owned by another machine is never marked dead/stale by the local monitor. test: `tests/agents/test_lifecycle_monitor.py::test_monitor_ignores_other_machines_runs`.
- 2.2.3 - Stale-cleanup and deletion refuse other machines' worktree/clone rows. test: `tests/storage/test_worktrees.py::test_cleanup_scoped_to_local_machine`.
- 2.2.4 - Claim/reuse never selects another machine's workspace records. test: `tests/storage/test_worktrees.py::test_claim_scoped_to_local_machine`.

### 2.3 Scope session background consumers to local-machine sessions [category: code]
`kind: deliverable`

Target: `src/gobby/storage/sessions/_transcript.py`, `src/gobby/sessions/summarize.py`,
`src/gobby/sessions/analyzer.py`, `src/gobby/agents/idle_check_handler.py`,
`src/gobby/sessions/transcript_paths.py`,
`src/gobby/hooks/event_handlers/_session_start/transcripts.py`

`sessions.machine_id` already exists (NOT NULL), so this is query-scoping only:

- `get_pending_transcript_sessions` (`storage/sessions/_transcript.py:21` — confirmed
  unscoped today) filters `machine_id = local`; daemon B must not attempt to process
  transcript files that live on machine A's disk.
- Summarizer, analyzer, and idle-watchdog session selection restrict to local-machine
  sessions wherever the operation opens `session.transcript_path` or the session's
  workspace.
- On-disk fallback scanners (`sessions/transcript_paths.py::_find_transcript_on_disk`,
  `_session_start/transcripts.py::derive_transcript_path`) return None for sessions
  whose machine_id differs from the local machine — this closes the misattach hazard
  where daemon B globs its own `~/.claude`/`~/.codex` and attaches the wrong local
  file to a remote session.
- Read-only metadata surfaces (session list APIs, wiki recaps over stored summaries)
  stay global — sharing is the point of M0.

**Acceptance:**

- 2.3.1 - Pending-transcript selection is machine-scoped. symbol: `gobby.storage.sessions._transcript.get_pending_transcript_sessions`.
- 2.3.2 - A remote-machine session is never processed, summarized, or watchdogged locally, and `transcript_processed` remains untouched by the non-owning daemon. test: `tests/sessions/test_machine_scoped_consumers.py::test_remote_sessions_skipped`.
- 2.3.3 - Fallback transcript scanners return None for remote-machine sessions. test: `tests/sessions/test_machine_scoped_consumers.py::test_fallback_scan_refuses_remote_sessions`.

### 2.4 Scope cron reconcile and stale sweeps by machine_id [category: code] (depends: 2.1)
`kind: deliverable`

Target: `src/gobby/storage/cron_children.py`, `src/gobby/storage/cron_runs.py`,
`src/gobby/scheduler/scheduler.py`

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

Target: `src/gobby/storage/hub/postgres.py`, `src/gobby/storage/migrations.py`

Today `MigrationRunner.apply_pending` (`storage/migrations.py:122`) is advisory-locked
and CAS-safe against concurrent migrators but silently ignores recorded versions it
does not know — an older build runs happily against a newer schema.

In `PostgresHubDatabase.apply_migrations` (`storage/hub/postgres.py:390`), after
ensuring the bookkeeping table: `SELECT MAX(version) FROM schema_migrations`; if it
exceeds `latest_known_version()` (exists at `storage/migrations.py:439`), raise a fatal
`MigrationUnsupportedError`: "hub schema is vX but this gobby build knows vY — update
gobby on this machine." Every CLI and the daemon reach the database through
`runtime_hub_database(apply_migrations=True)` (`storage/hub/runtime.py:17`), so one
check covers all entry points. Roll-forward (DB older than code) already works. Log
code/db schema versions at startup. Update the version-pinned migration-contract tests
per the repo's baseline rules.

**Acceptance:**

- 3.1.1 - Startup against a newer-than-known schema fails with the actionable error and performs no writes. test: `tests/storage/test_migration_lockstep.py::test_newer_schema_fails_closed`.
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
transcript continuity arrives with #17435); version-lockstep expectation; deferred
items (Qdrant API key with ACLs mandatory meanwhile, hook machine_id fallbacks →
#17435, Postgres `max_connections` sizing note for two daemons + CLI churn).

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
`GOBBY_TEST_PROTECT=1`), manual checklist for the pieces that need real machines:

1. Hub exposure: containers healthy; binds only on the tailnet address; ports
   unreachable from a non-ACL device (manual).
2. Client startup: both daemons pass the readiness gate against the remote stack; both
   machine_ids appear in `machines` (note: registration currently happens on session
   create/hook ingress via `LocalMachineManager.upsert_seen` — trigger a session or
   add a startup upsert as a one-line nicety).
3. Task continuity (the pack-up scenario): create/claim on A; stop A; list/continue/
   complete on B; restart A and observe convergence. Memory round-trip: store on A,
   recall (vector + graph) on B.
4. Session metadata: A's sessions visible on B with A's machine_id; B never processes
   A's transcripts; no misattached local files.
5. Isolation safety: same project+branch worktree on both machines without unique
   violation; an agent running in tmux on A is never marked dead by B; B's stale
   cleanup never touches A's rows or filesystem.
6. Cron: an interval job fires exactly once per occurrence with both daemons up;
   restarting B does not fail A's in-flight run.
7. Lockstep: newer migration applied from one machine → the stale machine fails clean.
8. Concurrency smoke: parallel CLI activity on both machines; observe Postgres
   connection count against `postgres_pool` × 2 daemons (default max_connections 100).

**Acceptance:**

- 4.1.1 - Automated two-daemon e2e covers items 3-7 against an isolated temporary PostgreSQL/Qdrant/FalkorDB stack, including the cross-daemon vector-and-graph memory round-trip against those provisioned services. test: `tests/e2e/test_shared_datastores_m0.py::test_two_machine_continuity_and_isolation`.
- 4.1.2 - Manual checklist for items 1-2 and 8 ships in the runbook. behavior: "M0 acceptance checklist" in `docs/guides/shared-stack.md`.

## V1 Plan Changelog

`kind: verification`
