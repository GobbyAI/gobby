Plan artifact: `.gobby/plans/gate10-test-run-reuse.md`
**Plan ID:** gate10-test-run-reuse

# Gate 10 Test-Run Reuse On A Daemon-Recorded Bounded Runner

## Overview
`kind: framing`

Task #24538 (Plan Gate 10 test-run reuse keyed by module, import closure, conftest
chain and environment). Josh approved planning it on 2026-10-15 19:12 CDT. The design
input of record is `.gobby/plans/research/gate10-reuse-scenarios-2026-10-15.md`. The
Orchestrator (gobby#15977) ruled on 2026-10-15 that this plan also owns the bounded-run
tool researched in #24534 (existing Rust process supervisors for the gdaemon
bounded-run tool), so the plan reaches Josh complete, with no deferrals.

Today Gate 10 (`validation_commands`) reads every command and exit code from session
transcripts. Any later task edit makes every earlier run stale
(`criterion_commands.py::first_invalidating_edit`), so a one-line edit after a green
packet reruns the whole packet. Seats also cannot see or signal a runaway test run:
`ps` stays denied (#24414 closed wont_fix), so host watchers and Release Manager round
trips do that work.

The plan delivers three things:

1. A bounded-run launcher, `gdaemon run --bound <seconds> [--term-grace <seconds>] --
   <command>`. It behaves like coreutils `timeout`: output passes through byte for
   byte, the launcher exits with the child's exit code, and it exits 124 when the time
   limit ends the run. The running gdaemon spawns the child itself, in its own process
   group, inside the caller's own SRT sandbox policy, admitted against the test-hub
   slot limit, and records the outcome itself.
2. A run-record store. gdaemon writes one row per bounded run, including the git tree
   the run executed against, captured by the daemon at run start and run end.
3. Gate 10 reuse. A test module's earlier passing record keeps credit while its reuse
   key is unchanged at the close candidate. The key is the module's own bytes, its
   import closure, its conftest chain and the environment set.

## Decision Record
`kind: framing`

Each decision below is settled for this plan. Items marked "ratified at approval" are
Orchestrator-accepted defaults that Josh rules on when he approves the stamped plan.

1. **Scope.** This plan owns the launcher, the record store and the reuse
   evaluation (Orchestrator, 2026-10-15). No deferrals and no placeholder task refs.
2. **Where the child runs.** gdaemon spawns it. A launcher-reported result is
   forgeable by a seat, and only the parent of a process can read its exit status on
   macOS, so a daemon-recorded run requires a daemon-spawned child.
3. **Sandbox and caller identity (Security Reviewer ratifies before landing).** The
   launcher authenticates with the seat's existing managed capability, the
   `gobby-agent-v1` token in the private `GOBBY_MANAGED_EXECUTION_BOOTSTRAP` envelope
   that `crates/gcore/src/local_token.rs::managed_api_token` already reads. gdaemon
   verifies the token's signature, resolves the agent run it names, and wraps the
   child in that run's own SRT policy. Environment-claimed session or run ids are
   never trusted. Callers without a managed token, and runs whose sandbox backend is
   `provider-native`, are refused with exit 125. A run whose sandbox is disabled runs
   unwrapped, which grants nothing that seat lacks. No new credential, no sandbox
   widening.
4. **After the grace window (ratified at approval).** At the bound gdaemon sends
   SIGTERM to the run's own process group, waits the grace window with the group
   leader unreaped, then sends SIGKILL to that group and reaps it with a 5-second
   bound, exactly as `BackendChild::drain` does. Rationale: the group is
   daemon-owned and non-interactive, and a group that ignores TERM would otherwise
   hold a test-hub slot forever. Nothing outside the run's own group is ever
   signalled. Josh's standing host-watcher rule is TERM-only, so he rules on this
   at approval.
5. **Slot limit.** A constant of 3 concurrent test-hub runs, the upper bound of
   Josh's 18:45 ruling. Josh revisits it after this tool lands, with a load check.
   Admission follows the 19:05 ruling: `ruff`, `mypy`, `gobby test-types audit`,
   `mkdir`, `tar`, `git` and `cp` run without a slot; every other command takes one.
   The bound covers queue wait plus run time.
6. **Exit codes.** Child's exit code on completion; 124 when the bound ends the run;
   125 when the launcher or daemon fails (unreachable daemon, refused caller, refused
   cwd, spawn failure). The launcher prints nothing of its own on success. On 124 and
   125 it prints exactly one stderr line starting `gdaemon run:`.
7. **Per-module outcome comes from the exit code only.** Exit 0 from a reusable
   pytest invocation records every target module as passed; any other outcome
   records every target module as failed under that key. Records never parse output.
   This is how rtk recall output is credited: a record's credit never depends on
   output, so rtk compaction and the `[full output: rtk recall <id>]` hint cannot
   change it. The transcript path keeps crediting rtk output as it does today.
8. **Reuse key.** For module `m` at close candidate `C`, the key is the bytes at `C`
   of: `m`, its import closure, its conftest chain, and the environment set
   (`uv.lock`, `pyproject.toml`, `.python-version`, `crates/gcore/assets/schema/`,
   `src/gobby/install/shared/`), plus the Python version from `.venv/pyvenv.cfg`.
   A record's key equals the candidate's key exactly when no path in that set
   changed between the record's tree and `C`; this follows by induction from `m`,
   because removing an import requires editing a file already in the closure. The
   gate therefore compares trees with `git diff --name-only`. The daemon captured
   the record's tree when the run happened, so no seat supplies a key or a result.
9. **Import closure source (deviation from the design input).** The closure is
   computed by AST over the candidate's git blobs with the existing
   `close_test_coverage.py::_module_imports` parser, not from the code index. The
   code index reflects the working tree, not the candidate commit, and
   `gcode imports` requires the optional graph backend. Unindexed or data-file edits
   still void reuse, because any changed path outside Python sources does.
10. **Conservative start.** Reuse is refused for a module whose own file or conftest
    chain starts subprocesses, reads repository or home paths, or uses dynamic
    imports; any changed path that is not a `.py` file under `src/` or `tests/`
    voids all reuse; a module with both a passing and a failing record under one key
    is never reused. These rules are deliberately coarse. Widening them is a
    separate decision after measurement on real closes, per recommendation 3 of the
    design input.
11. **Visibility.** Every reused pass appears in the gate details with its record
    id, session, original start and end time and recorded tree.
12. **Retention.** Records older than 14 days are deleted at insert time. Git's
    default `gc.pruneExpire` also drops unreachable snapshot trees after about two
    weeks; a missing tree fails reuse closed.

## Constraints
`kind: framing`

- **Security Reviewer landing gate.** Requirement 4 of #24538: the Security
  Reviewer reviews this design, Decision Record items 2 to 4 above in particular,
  before any deliverable lands on `0.5.0`. Landing waits for that sign-off. It is a
  landing gate, not a leaf.
- **#24535 coordination (Close gates: prove rtk-wrapped validation runs earn full
  credit).** No deliverable depends on it. Records carry the child's exit code and
  never parse output. Deliverable 2.4 teaches the transcript path that
  `gdaemon run ... --` is exit-preserving; that is new work here, not a fix #24535
  owns. If #24535 changes `_first_executable` or the wrapper rules first, 2.4 merges
  onto its result. Release Manager packets keep `RTK_DISABLED=1` until #24535 lands,
  independent of this plan.
- **Operator rulings (Josh, 2026-10-15).** 18:45 slot limit; 19:05 trivial-run list;
  19:06-19:07 rtk hard requirement. Seats never rerun without rtk to satisfy a gate.
- **Rust release discipline.** Every leaf that changes a shipped binary's crate or a
  recursive path dependency bumps that binary's patch version and `Cargo.lock` in its
  own commit, keeps managed version pins and the gcode maintenance floor in step, and
  is promoted through `promote_workspace_binary_set`. A gcore asset change (1.1)
  reaches `gcode`, `gdaemon`, `ghook`, `gclient` and the `gobby-terminals` library.
  Installing a new gdaemon needs a global restart announcement and a quiet window.
- **Monolith ceiling.** `transcript_outcomes.py` (853 lines) and
  `validation_detection.py` (867 lines) are above the 850-line heuristic, so 2.4
  moves code out of both before adding to them. `_lifecycle_close.py` (929 lines) is
  not targeted: reuse records reach the gate through `load_confinement_context`.
- **Test isolation.** Python tests use the isolated test hub
  (`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test
  GOBBY_TEST_PROTECT=1`). Rust tests that spawn children use temporary repositories
  and never signal anything outside the processes they create.
- **Single admission point.** Slot admission is one in-memory counter inside the
  single gdaemon instance (it holds the daemon lease), checked and taken under one
  lock immediately before spawn.

## Granularity
`kind: framing`

Nine deliverables in four phases. Three lifecycle owners are kept apart: the
record store (P1), the run lifecycle inside gdaemon (2.2 service, 2.3 client), and
the close-time reuse evaluation (P3). The transcript credit (2.4) and the rtk
rewrite placement (2.5) are separate behaviors with separate tests. The wrap-prefix
persistence (2.1) is a Python prerequisite of 2.2, and docs (P4) follow behavior.
2.2 exceeds six
acceptance items because admission, sandboxed spawn, bound, snapshot and record are
one request lifecycle whose parts cannot be tested apart; splitting it would leave
leaves that spawn unrecorded or record unspawned runs. `crates/gdaemon/Cargo.toml`
is a shared Target across 1.1, 2.2 and 2.3, ordered by dependencies.

## P1: Run record store
`kind: framing`

**Goal**: one durable row per bounded run, written only by gdaemon.

### 1.1 Validation run record table [category: code]
`kind: deliverable`

Targets:
- `crates/gcore/assets/schema/migrations/469_validation_run_records.sql`
- `crates/gcore/assets/schema/catalog.manifest.json::*` — scope-reason: catalog entry for migration 469
- `crates/gcore/src/schema/assets.rs::*` — scope-reason: registers migration 469 in the embedded asset list
- `crates/gcore/src/grant/bundle.rs::*` — scope-reason: derived schema-identity carrier for migration 469
- `crates/gcore/tests/schema_contract.rs::*` — scope-reason: schema contract expectations for migration 469
- `crates/gdaemon/tests/cli_contract.rs::*` — scope-reason: schema identity expectations for migration 469
- `src/gobby/storage/schema_expected_identity.json::*` — scope-reason: regenerated expected schema identity
- `crates/gcore/Cargo.toml`
- `crates/gcode/Cargo.toml`
- `crates/gdaemon/Cargo.toml`
- `crates/ghook/Cargo.toml`
- `crates/gclient/Cargo.toml`
- `Cargo.lock`

Research context. Migrations live under `crates/gcore/assets/schema/migrations/`;
the latest is `468_terminal_host_epochs.sql`, landed with its carriers in
`3fa292372e`. The derived-carriers lint requires every carrier listed above in the
same deliverable. Follow 468's grant pattern: `REVOKE ALL ... FROM PUBLIC` and grant
to `gobby_daemon_runtime`, the role both gdaemon and the Python daemon use; the
Python gate needs SELECT, gdaemon needs SELECT, INSERT and DELETE.

Create `validation_run_records`:

- `id uuid PRIMARY KEY`, `project_id uuid NOT NULL`, `session_id uuid NOT NULL`,
  `agent_run_id text NOT NULL` (all taken from the verified capability, never from
  the request body).
- `argv jsonb NOT NULL` (the submitted argv), `cwd text NOT NULL`,
  `repo_root text NOT NULL`.
- `started_at timestamptz NOT NULL`, `completed_at timestamptz NOT NULL`.
- `outcome text NOT NULL CHECK (outcome IN ('passed','failed','timed_out','interrupted'))`,
  `exit_code integer` (null for `timed_out` and `interrupted`).
- `tree_start text`, `tree_end text` (git tree ids; null when the snapshot failed).
- `python_version text` (the `version_info` value from `<repo_root>/.venv/pyvenv.cfg`,
  null when absent).
- `reusable boolean NOT NULL`, `not_reusable_reason text`
  (`CHECK (reusable = (not_reusable_reason IS NULL))`).
- Index `(project_id, completed_at DESC)`.

Run the shipped schema checks after writing the migration: `cargo test -p gobby-core
--test schema_contract` and `cargo test -p gobby-daemon --test cli_contract`. Bump the
patch version of each listed binary crate and `gobby-core`, and update `Cargo.lock`.

**Acceptance:**

- 1.1.1 - Migration 469 creates `validation_run_records` with the columns, checks and index above, revokes PUBLIC and grants `gobby_daemon_runtime` SELECT, INSERT and DELETE. file: `crates/gcore/assets/schema/migrations/469_validation_run_records.sql`.
- 1.1.2 - The schema catalog, asset registry, grant bundle and expected identity carry migration 469, and the schema contract tests pass. file: `crates/gcore/assets/schema/catalog.manifest.json`. file: `src/gobby/storage/schema_expected_identity.json`. test: `crates/gcore/tests/schema_contract.rs::catalog_manifest_matches_embedded_migrations`.
- 1.1.3 - `gobby-core` and every dependent shipped binary carry a patch-version bump with `Cargo.lock` updated. file: `Cargo.lock`.

## P2: Bounded-run launcher
`kind: framing`

**Goal**: seats run validation through gdaemon, which supervises, sandboxes,
admits and records each run, with transcript and rtk behavior unchanged.

### 2.1 Persist the SRT wrap prefix on managed runs [category: code]
`kind: deliverable`

Targets:
- `src/gobby/agents/spawn_executor_support.py::_record_resume_launch_details`
- `src/gobby/agents/resume_executor.py::resume_agent_run`
- `tests/agents/test_spawn_executor_support.py::*` — scope-reason: adds the persisted wrap-prefix case to the existing spawn support suite

Research context. `src/gobby/agents/srt_runtime.py::SandboxLaunch.wrap` builds the
SRT argv as `[node_path, runner_path, "--settings", policy_path, "--violations",
violation_path, "--", *command]` when the backend is `srt` and enforced, and returns
the command unchanged otherwise. `SandboxLaunch.metadata` persists `backend`,
`enforced`, `policy_path`, `violation_path`, `managed_bootstrap_path`,
`provider_executable`, `runtime_version`, `policy_schema_version` and `policy_hash`,
but not `node_path` or `runner_path`, so gdaemon cannot rebuild the wrap prefix. Two
writers store it in the agent run's resume metadata:
`spawn_executor_support.py::_record_resume_launch_details`
(`metadata["sandbox"] = sandbox_launch.metadata()`) and
`resume_executor.py::resume_agent_run` (`launch_updates["sandbox"] =
launch.metadata()`). `srt_runtime.py` is 988 lines, so it is not edited. Add the new
function `persisted_sandbox_metadata(launch)` to `spawn_executor_support.py`
(643 lines). It returns `launch.metadata()` plus `wrap_prefix`: `launch.wrap([])`
for an enforced `srt` launch, which yields exactly the prefix through `--`, and
`[]` otherwise. Both writers call it.

Consumers unchanged:
- `src/gobby/agents/spawn_executor.py` — no-edit-reason: calls `_record_resume_launch_details` with an unchanged signature.
- `src/gobby/agents/spawn_executor_providers.py` — no-edit-reason: calls `_record_resume_launch_details` with an unchanged signature.
- `tests/agents/test_spawn_executor.py` — no-edit-reason: exercises the unchanged call shape.
- `src/gobby/dispatch/daemon_resume.py` — no-edit-reason: calls `resume_agent_run` with an unchanged signature.
- `src/gobby/runner_lifecycle_agents.py` — no-edit-reason: calls `resume_agent_run` with an unchanged signature.
- `src/gobby/runner_lifecycle_reconcile.py` — no-edit-reason: calls `resume_agent_run` with an unchanged signature.
- `tests/agents/test_resume_placement.py` — no-edit-reason: exercises the unchanged call shape.
- `tests/dispatch/test_daemon_resume.py` — no-edit-reason: exercises the unchanged call shape.

**Acceptance:**

- 2.1.1 - The persisted sandbox metadata carries `wrap_prefix` equal to the argv prefix `SandboxLaunch.wrap` produces for an enforced SRT launch, and an empty list otherwise, from both the spawn and the resume writer. file: `src/gobby/agents/spawn_executor_support.py`. test: `tests/agents/test_spawn_executor_support.py::test_persisted_sandbox_metadata_carries_wrap_prefix`.

### 2.2 gdaemon bounded-run service [category: code] (depends: 1.1, 2.1)
`kind: deliverable`

Targets:
- `crates/gdaemon/src/bounded_run.rs`
- `crates/gdaemon/src/bounded_run/supervise.rs`
- `crates/gdaemon/src/bounded_run/snapshot.rs`
- `crates/gdaemon/src/bounded_run/record.rs`
- `crates/gdaemon/src/lifecycle/group.rs`
- `crates/gdaemon/src/lifecycle/mod.rs`
- `crates/gdaemon/src/lifecycle/backend.rs::BackendChild::drain`
- `crates/gdaemon/src/lifecycle/backend.rs::BackendChild::signal_owned_group`
- `crates/gdaemon/src/lifecycle/backend.rs::signal_group`
- `crates/gdaemon/src/front_door/routes.rs::FAMILIES`
- `crates/gdaemon/src/lib.rs`
- `crates/gdaemon/tests/bounded_run.rs`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`

Research context. #24534 found no crate worth adding: pueue is a queue, not a
bounded runner; process-wrap has no TERM-grace-KILL timer; tokio `kill_on_drop` kills
only the direct child. The workspace already implements the pattern on tokio and
libc. `crates/gdaemon/src/lifecycle/backend.rs` spawns with `process_group(0)` and
`kill_on_drop(true)`; `BackendChild::drain` sends SIGTERM to the group, keeps the
leader unreaped through the grace window so its PGID cannot be reused, then SIGKILLs
the group and reaps with a 5-second bound; `BackendChild::signal_owned_group`
tolerates macOS EPERM for an all-zombie group only after the leader has exited;
`signal_group` treats ESRCH as already gone. `crates/gterminal/src/host/native_ops.rs`
`terminate_group` is the same pattern and stays untouched (no gterm release). Move
`signal_group` and the EPERM-tolerant group signal and drain logic into the new
`crates/gdaemon/src/lifecycle/group.rs`, have `BackendChild` call it, and use it from
the run supervisor. On macOS a descendant that calls `setsid` escapes the group;
that limit is stated in the docs (4.1) and not worked around.

Route: add a native `RunFamily` to `FAMILIES` in
`crates/gdaemon/src/front_door/routes.rs`, owning
`POST /api/bounded-runs`. Request body: `argv` (non-empty string array), `cwd`
(absolute), `env` (string map, the launcher's environment verbatim), `bound_seconds`
(1 to 3600), `grace_seconds` (1 to 60, default 10). The response streams framed
stdout and stderr chunks, then one final frame with `outcome`, `exit_code` and
`record_id`.

Authentication: require `Authorization: Bearer gobby-agent-v1.<...>`. Parse it with
`crates/gcore/src/grant/handshake.rs::parse_capability_token` and verify the
signature with the managed signing key, HMAC-SHA256 of the bootstrap `api_key` over
`gobby-managed-token-v1`, exactly as Python's
`src/gobby/utils/local_token.py::derive_managed_signing_key` and
`classify_agent_api_token` do (`crates/gdaemon/src/front_door/challenge.rs` already
computes that HMAC). Require `kind == "agent_run"`, an unexpired `exp`, and an
`agent_run_id`. Load that run's resume metadata `sandbox` object from the hub. If the
token is missing or invalid, or the sandbox backend is `provider-native`, refuse
before spawning. A conformance test verifies a token minted by Python's
`issue_agent_api_token` from a fixture.

Checks before admission: `cwd` must resolve, with symlinks resolved, inside a
registered project checkout or one of its git worktrees for the token's
`project_id`; `repo_root` is `git rev-parse --show-toplevel` of `cwd`.

Admission: classify the core executable after stripping `env`, `rtk`, `uv run` and
`nice` prefixes. `ruff`, `mypy`, `gobby test-types audit`, `mkdir`, `tar`, `git` and
`cp` run immediately. Everything else waits for one of 3 slots held in one
`tokio::sync::Semaphore`. The bound clock starts at request arrival, so queue wait
counts against it; a run whose bound ends while queued is recorded as `timed_out`
without spawning.

Spawn: argv is `wrap_prefix + submitted argv`, where `wrap_prefix` is the run's
persisted `sandbox.wrap_prefix` from 2.1 (empty when the run's sandbox is disabled).
A run whose sandbox is enforced `srt` but has no `wrap_prefix` is refused. The child
gets exactly the request `env` (with `GOBBY_AGENT_API_TOKEN` removed), `cwd`,
`stdin` null, its own process group, and `kill_on_drop(true)`. stdout and stderr
stream to the client unchanged.

Bound: at the deadline, SIGTERM the group, wait `grace_seconds` with the leader
unreaped, SIGKILL the group, reap within 5 seconds (Decision Record item 4). Outcome
`timed_out`. When gdaemon shuts down with runs in flight, it drains each the same way
and records `interrupted`. A client disconnect also drains the run and records
`interrupted`.

Snapshot (`snapshot.rs`): copy `<gitdir>/index` to a private temporary file, then run
`git add -A` and `git write-tree` with `GIT_INDEX_FILE` pointing at the copy, in
`repo_root`. The real index, HEAD, refs and working tree are never written; the
only repository write is loose objects. Take it once before spawn (`tree_start`) and
once after reap (`tree_end`). A failed snapshot records null and makes the record not
reusable.

Record (`record.rs`): in one transaction insert the row and delete rows with
`completed_at < now() - interval '14 days'`. `outcome` is `passed` for exit 0 and
`failed` otherwise. `reusable` is true only when all hold, else
`not_reusable_reason` names the first failure: outcome is `passed` or `failed`;
both snapshots exist and are equal; the core executable is pytest (`pytest`,
`python -m pytest`, `uv run pytest`, optionally behind `rtk`); every non-option
argument is a file or directory path (no `::` node id); every option is in the
allowlist `-q`, `-v`, `-vv`, `-s`, `-x`, `--no-cov`, `-n <N>`, `-nN`,
`--numprocesses=<N>`, `-p no:<name>`, `--tb=<style>`, `-r<chars>`, `--maxfail=<N>`,
`--timeout=<N>`, `--durations=<N>`, `--color=<when>`; and `PYTEST_ADDOPTS`,
`PYTEST_PLUGINS` and `PYTHONPATH` are unset or empty in `env`.

Focused checks: `cargo test -p gobby-daemon --test bounded_run`,
`cargo test -p gobby-daemon --test lifecycle`, `cargo clippy -p gobby-daemon
--all-targets -- -D warnings`.

**Acceptance:**

- 2.2.1 - `BackendChild` drains through the shared group helper with unchanged behavior. file: `crates/gdaemon/src/lifecycle/group.rs`. test: `crates/gdaemon/tests/lifecycle.rs::backend_drain_terms_then_kills_owned_group`.
- 2.2.2 - A request without a valid managed capability, with a forged signature, or for a provider-native run is refused before any spawn. test: `crates/gdaemon/tests/bounded_run.rs::refuses_unmanaged_forged_and_provider_native_callers`.
- 2.2.3 - A token minted by Python's `issue_agent_api_token` verifies in gdaemon. test: `crates/gdaemon/tests/bounded_run.rs::verifies_python_minted_capability`.
- 2.2.4 - An SRT-enforced run's child argv starts with the persisted wrap prefix, and a sandbox-disabled run's argv is unwrapped. test: `crates/gdaemon/tests/bounded_run.rs::wraps_child_in_caller_srt_policy`.
- 2.2.5 - A cwd outside the token project's checkouts and worktrees is refused. test: `crates/gdaemon/tests/bounded_run.rs::refuses_cwd_outside_project_checkouts`.
- 2.2.6 - A fourth slot-taking run waits until a slot frees, a trivial command runs without a slot, and a run whose bound ends in the queue records `timed_out` unspawned. test: `crates/gdaemon/tests/bounded_run.rs::admits_three_slots_and_bounds_queue_wait`.
- 2.2.7 - At the bound a TERM-ignoring group is TERMed, then KILLed after the grace window and reaped; nothing outside the group is signalled; the outcome is `timed_out`. test: `crates/gdaemon/tests/bounded_run.rs::bound_terms_then_kills_only_the_run_group`.
- 2.2.8 - The snapshot captures uncommitted and untracked-unignored changes, leaves the real index and HEAD unchanged, and a mid-run edit makes `tree_start` differ from `tree_end` and the record not reusable. test: `crates/gdaemon/tests/bounded_run.rs::snapshots_tree_without_touching_index`.
- 2.2.9 - Records carry identity from the verified token, the argv, outcome and exit code, and the reusability rules above, each refusal with its reason; insert prunes rows older than 14 days. test: `crates/gdaemon/tests/bounded_run.rs::records_outcome_identity_and_reusability`.

### 2.3 `gdaemon run` launcher client [category: code] (depends: 2.2)
`kind: deliverable`

Targets:
- `crates/gdaemon/src/main.rs::Command`
- `crates/gdaemon/src/main.rs::main`
- `crates/gdaemon/src/bounded_run/client.rs`
- `crates/gdaemon/tests/bounded_run_cli.rs`
- `crates/gdaemon/tests/cli_contract.rs::*` — scope-reason: adds the run subcommand to the CLI contract expectations
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`

Research context. `crates/gdaemon/src/main.rs::Command` has `Schema` and `Serve`.
Add `Run { bound: u64, term_grace: u64 (default 10), argv: Vec<String> }` parsed as
`gdaemon run --bound <s> [--term-grace <s>] -- <argv...>`; `--bound` is required and
`--` is required before argv. The client reads the managed token with
`crates/gcore/src/local_token.rs::managed_api_token`, resolves the local daemon
address from `~/.gobby/bootstrap.yaml` the way other local clients do, posts the
request from 2.2 with its current working directory and full environment, writes
stdout frames to its stdout and stderr frames to its stderr byte for byte, and exits
with the final frame's exit code. On `timed_out` it prints
`gdaemon run: time limit <N>s reached; process group terminated` to stderr and exits
124. On refusal, an unreachable daemon or a broken stream it prints
`gdaemon run: <reason>` to stderr and exits 125. On success it prints nothing of its
own. It forwards SIGINT and SIGTERM by closing the request, which 2.2 drains as
`interrupted`. Add the new subcommand to the CLI contract test.

**Acceptance:**

- 2.3.1 - Output passes through byte for byte on both streams and the launcher exits with the child's exit code. test: `crates/gdaemon/tests/bounded_run_cli.rs::passes_output_and_exit_code_through`.
- 2.3.2 - A bound expiry exits 124 with exactly one `gdaemon run:` stderr line. test: `crates/gdaemon/tests/bounded_run_cli.rs::exits_124_with_one_trailer_line`.
- 2.3.3 - A missing token, refused caller or unreachable daemon exits 125 with one `gdaemon run:` stderr line and spawns nothing. test: `crates/gdaemon/tests/bounded_run_cli.rs::exits_125_on_launcher_failure`.
- 2.3.4 - `gdaemon run` appears in the CLI contract with required `--bound` and `--`. test: `crates/gdaemon/tests/cli_contract.rs::cli_contract_lists_run_subcommand`.

### 2.4 Transcript gates credit launcher runs [category: code] (depends: 2.3)
`kind: deliverable`

Targets:
- `src/gobby/tasks/transcript_outcomes.py::_first_executable`
- `src/gobby/tasks/transcript_outcomes.py::_launcher_arguments`
- `src/gobby/tasks/transcript_launchers.py`
- `src/gobby/config/validation_detection.py::default_validation_wrapper_rules`
- `src/gobby/config/validation_detection.py::_wrapper_rule`
- `src/gobby/config/validation_detection.py::_iter_wrapper_rules`
- `src/gobby/config/validation_wrappers.py`
- `crates/gcore/assets/config/runtime_config_contract.json::*` — scope-reason: derived carrier for the wrapper-rule move under src/gobby/config
- `src/gobby/tasks/tdd_retained_output.py::_GENERATED_LINE_RE`
- `tests/tasks/test_transcript_outcomes.py::*` — scope-reason: adds launcher credit cases to the transcript outcome suite
- `tests/config/test_validation_detection.py::*` — scope-reason: adds the gdaemon-run wrapper rule case
- `tests/tasks/test_tdd_retained_output.py::*` — scope-reason: adds the launcher trailer case
- `tests/tasks/test_close_checklist.py::*` — scope-reason: adds the end-to-end launcher credit case for Gate 10

Research context. Transcript credit resolves a command's executable through
`transcript_outcomes.py::_first_executable`, which unwraps `timeout`, `gtimeout`,
`time`, `exec`, `command`, `xargs`, `stdbuf`, `sudo` and `caffeinate` with
`_launcher_arguments` (flag and value-option tables, stopping at `--`). A run whose
executable resolves is not wrapped, so `timeout 300 uv run pytest x` credits today.
Add `gdaemon` with first argument `run` as a launcher: flags none, value options
`--bound` and `--term-grace`, terminated by the required `--`. Other `gdaemon`
subcommands stay unresolved. The launcher's 124 is a failure outcome, as with
`timeout`.

Validation-command detection and criterion matching unwrap wrappers through
`validation_detection.py::default_validation_wrapper_rules`; `timeout` is a
`"delimiter"` rule, and `_unwrap_matched_rule` returns the tokens after the
delimiter. Add `_wrapper_rule("gdaemon-run", "gdaemon run", "delimiter",
["gdaemon run"])` so `gdaemon run --bound 300 -- uv run pytest x` matches the
criterion `uv run pytest x`. Any Target under `src/gobby/config/` requires the
`runtime_config_contract.json` carrier.

Both files are above the 850-line heuristic (853 and 867 lines). Move
`_launcher_arguments` and its option tables from `transcript_outcomes.py` into the
new `src/gobby/tasks/transcript_launchers.py`, imported by `_first_executable`
(its only caller). Move `default_validation_wrapper_rules` and `_wrapper_rule` from
`validation_detection.py` into the new `src/gobby/config/validation_wrappers.py`,
imported by `_iter_wrapper_rules` (the only caller of
`default_validation_wrapper_rules`). Both files end smaller than they start.

The launcher's 124 trailer line must not count as retained failure output, so add
`gdaemon run: .*` to `tdd_retained_output.py::_GENERATED_LINE_RE`.

Focused checks: `GOBBY_TEST_PROTECT=1 uv run pytest tests/tasks/test_transcript_outcomes.py
tests/config/test_validation_detection.py tests/tasks/test_tdd_retained_output.py
tests/tasks/test_close_checklist.py -q`.

**Acceptance:**

- 2.4.1 - `gdaemon run --bound N [--term-grace G] -- <cmd>` resolves to `<cmd>`'s executable and is credited unwrapped; a missing `--` or another gdaemon subcommand stays unresolved. test: `tests/tasks/test_transcript_outcomes.py::test_gdaemon_run_launcher_resolves_child_executable`.
- 2.4.2 - Criterion matching treats `gdaemon run --bound 300 -- uv run pytest x` as `uv run pytest x`. test: `tests/config/test_validation_detection.py::test_gdaemon_run_delimiter_wrapper_unwraps_child`.
- 2.4.3 - A passing launcher pytest run of a changed test, also behind `rtk`, earns the same Gate 10 credit as the bare run, and exit 124 is an unresolved failure. test: `tests/tasks/test_close_checklist.py::test_gdaemon_run_pytest_earns_bare_run_credit`.
- 2.4.4 - The launcher trailer line is generated output, not retained failure text. test: `tests/tasks/test_tdd_retained_output.py::test_gdaemon_run_trailer_is_generated_line`.
- 2.4.5 - `transcript_outcomes.py` and `validation_detection.py` each end below their starting line counts, with the moved code in the two new modules. file: `src/gobby/tasks/transcript_launchers.py`. file: `src/gobby/config/validation_wrappers.py`.

### 2.5 rtk rewrites inside the launcher [category: code] (depends: 2.4)
`kind: deliverable`

Targets:
- `src/gobby/workflows/engine/proxy_hooks.py::ProxyHooksMixin._run_rtk_proxy`
- `tests/workflows/test_proxy_hooks.py::*` — scope-reason: adds launcher-prefix rewrite cases to the proxy hook suite

Research context. The `rtk-command-rewrite` rule
(`src/gobby/install/shared/workflows/rules/integrations/rtk-command-rewrite.yaml`,
`before_tool` on Bash) runs `ProxyHooksMixin._run_rtk_proxy`, which passes the whole
command to `rtk rewrite -- <command>` and replaces it with rtk's output after its
plausibility, shell-context, package-manager and worktree-git checks. Stock rtk does
not know `gdaemon run`, so a launcher command would go uncompacted. When the command
is one shell segment whose words start with `gdaemon run` options and `--`, pass
only the words after `--` to `rtk rewrite`, run the existing checks on that inner
result, and reassemble the original launcher prefix with the rewritten inner command,
so rtk runs inside the launcher (`gdaemon run --bound 300 -- rtk pytest x`). A
command already containing `rtk` after `--` is left unchanged.

**Acceptance:**

- 2.5.1 - `gdaemon run --bound 300 -- pytest x` is rewritten to `gdaemon run --bound 300 -- rtk pytest x` with the prefix preserved byte for byte. test: `tests/workflows/test_proxy_hooks.py::test_rtk_rewrites_inside_gdaemon_run_launcher`.
- 2.5.2 - A rejected inner rewrite leaves the whole launcher command unchanged, and an inner `rtk` command is not rewritten twice. test: `tests/workflows/test_proxy_hooks.py::test_rtk_launcher_rewrite_keeps_command_on_rejection`.

## P3: Gate 10 reuse
`kind: framing`

**Goal**: a test module keeps credit from a daemon record while its key is unchanged
at the close candidate.

### 3.1 Reuse evaluation [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `src/gobby/tasks/validation_reuse.py`
- `tests/tasks/test_validation_reuse.py`

Research context. New module, so `close_checklist.py` (805 lines) and
`close_test_coverage.py` (812 lines) only gain wiring. Inputs: the record rows
(loaded in 3.2), the close root, the candidate commit, and the required test module
paths. Output: a frozen `ReuseCredit` (new) mapping each reused module to its
evidence (`record_id`, `session_id`, `started_at`, `completed_at`, `tree`), plus a
per-module refusal reason for the rest.

Reused helpers: `close_test_coverage.py::_module_imports` (AST imports without
executing code, relative imports resolved), `_module_name`,
`CloseCandidate.blobs` (path to blob id at the candidate),
`pytest_module_paths`, and `command_equivalence.py::pytest_targets` for record
argv targets. Blob contents come from one `git cat-file --batch` per evaluation.

Rules, applied per required module `m`, with no reuse at all when the candidate
commit is unknown:

1. Eligible records: same project, `reusable` true, `completed_at` within 14
   days, and `m` among the record's target modules (directory targets expand
   through `git ls-tree -r --name-only <tree_end> <dir>` and `pytest_module_paths`).
   Take at most the 50 newest; if more match, refuse `m` (`lookback_exceeded`).
2. Changed paths: `git diff --name-only -z <tree_end> <candidate>` in the close
   root, once per distinct tree. A git failure (for example a pruned tree) makes
   that record ineligible.
3. Global void: if a changed path is not a `.py` file under `src/` or `tests/`, or
   is in the environment set (`uv.lock`, `pyproject.toml`, `.python-version`,
   `crates/gcore/assets/schema/**`, `src/gobby/install/shared/**`), that record
   gives no reuse to any module. A record whose `python_version` differs from the
   close root's `.venv/pyvenv.cfg` value gives none either.
4. Closure at the candidate: `m`, then transitively every repository module it
   imports (`gobby.*` under `src/gobby/`, `tests.*` under `tests/`), every
   `__init__.py` of each package traversed, every `conftest.py` from `tests/` down
   to `m`'s directory, and every module named by a string literal matching
   `gobby(\.\w+)+` in any closure file (for `patch("gobby.x.y")` targets),
   resolved to the longest existing module prefix.
5. Unbounded inputs: refuse `m` if `m` or any conftest in its chain imports or
   calls `subprocess`, `os.system`, `os.exec*`, `os.spawn*`, `os.popen`, `pty`, or
   `asyncio.create_subprocess_*`; uses `__file__`, `Path.cwd`, `os.getcwd`,
   `Path.home` or `expanduser`; or contains a string literal starting with `src/`,
   `tests/`, `crates/`, `web/` or `.gobby/`. Refuse `m` if any closure file calls
   `importlib.import_module`, `__import__`, `pkgutil` iteration, `runpy` or
   `importlib.metadata.entry_points` with a non-literal argument.
6. Same key: a record has `m`'s candidate key when none of its changed paths is in
   `m`'s closure and no global void applies.
7. Conflict: if any same-key record for `m` is `failed`, refuse `m`
   (`failed_under_key`). Otherwise the newest same-key `passed` record is the
   evidence.

Rejected alternatives: code-index import closure (working tree, not the candidate;
graph backend optional); per-module outcomes from junit XML or output parsing (adds
an output dependency rtk could disturb; exit-code-only is enough for conservative
reuse); a stored per-module key hash (the tree diff is equivalent and needs no
record-time closure computation).

Focused check: `GOBBY_TEST_PROTECT=1 uv run pytest tests/tasks/test_validation_reuse.py -q`.

**Acceptance:**

- 3.1.1 - A one-line edit to a source file reuses every target module whose closure excludes it and refuses those that import it, directly, transitively or through a package `__init__.py`. test: `tests/tasks/test_validation_reuse.py::test_edit_outside_closure_keeps_credit`.
- 3.1.2 - An edit to a conftest in the chain or a `patch("gobby...")` string target refuses the module. test: `tests/tasks/test_validation_reuse.py::test_conftest_and_patch_target_join_closure`.
- 3.1.3 - A changed non-Python path, an environment-set path or a different Python version voids reuse for every module. test: `tests/tasks/test_validation_reuse.py::test_data_env_and_python_version_void_all_reuse`.
- 3.1.4 - A module or conftest with subprocess, repository-path or home-path use, or a closure file with a non-literal dynamic import, is refused. test: `tests/tasks/test_validation_reuse.py::test_unbounded_inputs_always_rerun`.
- 3.1.5 - A module with both a passing and a failing record under the same key is refused, while a failure under a different key does not cancel a pass. test: `tests/tasks/test_validation_reuse.py::test_pass_and_fail_under_one_key_never_reused`.
- 3.1.6 - A pruned tree, an unknown candidate, non-reusable records and more than 50 matching records all fail closed. test: `tests/tasks/test_validation_reuse.py::test_reuse_fails_closed`.
- 3.1.7 - A rebase that changes only modules outside a test's closure keeps its credit at the new candidate. test: `tests/tasks/test_validation_reuse.py::test_rebase_recomputes_keys_at_candidate`.

### 3.2 Gate 10 consumes reuse credit and shows it [category: code] (depends: 3.1, 2.4)
`kind: deliverable`

Targets:
- `src/gobby/tasks/validation_confinement.py::ConfinementContext`
- `src/gobby/tasks/validation_confinement.py::load_confinement_context`
- `src/gobby/tasks/close_checklist.py::_evaluate_validation_commands`
- `tests/tasks/test_validation_confinement.py::*` — scope-reason: adds the record projection case
- `tests/tasks/test_close_checklist.py::*` — scope-reason: adds reuse credit cases to the Gate 10 suite

Research context. `_lifecycle_close.py::_evaluate_close` already projects durable
close inputs once per close with `asyncio.to_thread(load_confinement_context, db,
project_id=..., closing_session_id=..., evidence=...)` and passes the result to
`evaluate_validation_commands(confinement_context=...)`. Add a
`run_records` field to `ConfinementContext` and load the project's records from the
last 14 days in `load_confinement_context`, so `_lifecycle_close.py` (929 lines)
stays untouched.

In `_evaluate_validation_commands`, the inner `covers(tests)` already removes
`confined_paths` from the uncovered set. Compute `ReuseCredit` with 3.1 for the
modules still uncovered by fresh passing runs and confinement, then remove reused
modules the same way, for both `pytest_required_paths` and
`python_source_related_tests`. Fresh transcript evidence keeps priority: a reused
module never hides a fresh failure, and `unresolved_failures` still blocks.
`has_success` for the required `test` category is also true when reuse covered at
least one required module, no required module is left uncovered, and no fresh
test-category failure is unresolved. Add `details["reused_test_credit"]`: one entry
per reused module with `path`, `record_id`, `session_id`, `started_at`,
`completed_at` and `tree`, plus `details["reuse_refusals"]` (module to reason).

Consumers unchanged:
- `src/gobby/mcp_proxy/tools/tasks/_lifecycle_close.py` — no-edit-reason: calls `load_confinement_context` with unchanged arguments and passes the result through unchanged.

Focused check: `GOBBY_TEST_PROTECT=1 uv run pytest tests/tasks/test_close_checklist.py
tests/tasks/test_validation_confinement.py tests/tasks/test_validation_reuse.py -q`.

**Acceptance:**

- 3.2.1 - `load_confinement_context` returns the project's records from the last 14 days in `ConfinementContext.run_records`. symbol: `ConfinementContext`. test: `tests/tasks/test_validation_confinement.py::test_load_confinement_context_projects_run_records`.
- 3.2.2 - After a one-line edit, Gate 10 passes with fresh runs for affected modules and reuse for the rest, with no fresh run of the reused modules. test: `tests/tasks/test_close_checklist.py::test_gate10_reuses_unaffected_module_records`.
- 3.2.3 - A fresh unresolved test failure still fails the gate even when every required module has reuse credit. test: `tests/tasks/test_close_checklist.py::test_gate10_reuse_never_hides_fresh_failure`.
- 3.2.4 - Gate details list every reused pass with record id, session, original start and end time and tree, and every refusal with its reason. test: `tests/tasks/test_close_checklist.py::test_gate10_details_show_reused_pass_provenance`.

## P4: Documentation
`kind: framing`

**Goal**: operators and seats know how to run bounded validation and how reuse credits it.

### 4.1 Document the launcher and reuse [category: docs] (depends: 2.5, 3.2)
`kind: deliverable`

Targets:
- `docs/guides/tasks.md`
- `docs/guides/cli-commands.md`
- `docs/guides/sandboxing.md`

Research context. `docs/guides/tasks.md` "Git and Validation" says validation
commands come from transcripts and that a later task edit makes a clean run stale;
add the reuse exception, its conservative rules and the `reused_test_credit`
details. `docs/guides/cli-commands.md` documents gdaemon subcommands; add
`gdaemon run` with its exit codes, admission classes and the 3-slot limit.
`docs/guides/sandboxing.md` gains that bounded runs execute inside the caller's own
SRT policy, provider-native callers are refused, and a descendant that calls
`setsid` escapes the process group on macOS.

**Acceptance:**

- 4.1.1 - The tasks guide states when a daemon record keeps Gate 10 credit, the refusal rules and where reused passes are shown. behavior: "reused_test_credit" in `docs/guides/tasks.md`.
- 4.1.2 - The CLI guide documents `gdaemon run` syntax, exit codes 124 and 125, admission classes and the slot limit. behavior: "gdaemon run" in `docs/guides/cli-commands.md`.
- 4.1.3 - The sandboxing guide states the caller-policy wrap, the provider-native refusal and the `setsid` limit. behavior: "gdaemon run" in `docs/guides/sandboxing.md`.

## E1 End-to-end verification
`kind: verification`

These are completion gates for the implementation. Except for plan validation, none
has run yet. Each runs after the leaves land, and each must hold before the epic
closes.

Scoped suites must pass, Python against the isolated test hub:
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/agents/test_srt_runtime.py tests/tasks/test_transcript_outcomes.py tests/config/test_validation_detection.py tests/tasks/test_tdd_retained_output.py tests/workflows/test_proxy_hooks.py tests/tasks/test_validation_reuse.py tests/tasks/test_validation_confinement.py tests/tasks/test_close_checklist.py -q`.
Rust: `cargo test -p gobby-core --test schema_contract`, `cargo test -p gobby-daemon`,
`cargo clippy --workspace --all-targets -- -D warnings`. Then `uv run ruff format src/`,
`uv run ruff check src/ tests/`, `uv run mypy src/`, and the test-type audit over the
changed tests must all pass.

Live checks run after the Security Reviewer's sign-off, merge, promotion of the bumped
binary set and an announced restart. From a sandboxed seat, `gdaemon run --bound 600
-- uv run pytest <one test file> -q` must stream pytest output, exit with pytest's
code, and write one reusable record. A fourth concurrent pytest launch must wait for a
slot. A `--bound 5 -- sleep 60` run must exit 124 with one trailer line. After a
one-line edit to a source file outside that test's closure, a close preview must show
the module in `details.reused_test_credit` with the original run's session and time.
With the rtk rewrite rule enabled, the seat's launcher command must be rewritten with
`rtk` after `--` and must still earn credit.

## V1 Plan Changelog
`kind: framing`

- 2026-10-15: First narrative draft by the Plan Writer (gobby#16992) after the
  Orchestrator's scope ruling.
