Plan artifact: `.gobby/plans/daemon-restart-preflight.md`
**Plan ID:** daemon-restart-preflight

# Restart and cutover prove the start before the stop

## Overview
`kind: framing`

game-goblins (their #432) reported the Gobby daemon down at their 00:00 and 05:00
America/Chicago scheduled jobs on 09-13, 09-14 and 09-15. They shipped an inline
`gobby start` backstop (their #225) that fires once when the MCP proxy answers
`Gobby daemon is not running`. Forensics over `~/.gobby/logs/daemon.log`, their sync cron
logs and the Claude transcripts show both midnight outages were Gobby coordinators running
restart-shaped commands from the shared checkout `/Users/josh/Projects/gobby`:

| Night | Sender | Failure | Down |
| --- | --- | --- | --- |
| 09-14 00:07 | session ce394863, `uv run gobby restart --wait` | the start half failed on a mixed installed binary set; two manual starts before recovery | about 10 min |
| 09-15 00:04 | session d17a8b4f, `uv run gobby cutover --path .` | the build embedded another session's uncommitted schema WIP (migration 438, an edited `baseline.sql`, an edited identity pin, `assets.rs`); the new daemon died in `gdaemon schema apply` with `unrecognized schema lineage; recreate from a verified backup`, then `Error: daemon restart failed (exit 1)` | 7 h |

Structural causes, in the order the deliverables address them:

1. `gobby restart` (`src/gobby/cli/daemon.py`, `restart`) and `gobby cutover`
   (`src/gobby/cli/cutover.py`, `run_cutover`) are stop-then-start with no way back.
   `gobby stop` runs `launchctl bootout`, so launchd `KeepAlive` cannot help. Rolling the
   binaries back is not viable either: after a cutover the checkout pin matches the new
   binaries and the daemon refuses an installed identity that differs from the expected one,
   and `tests/cli/test_cutover.py::test_cutover_has_no_private_replacement_or_rollback_machinery`
   forbids private rollback machinery. The only sound guarantee is: prove the start will
   succeed, then stop.
2. The pre-stop check `_schema_restart_refusal` (added after the 09-14 incident) compares
   identities and versions only. It cannot see the lineage failure that took the daemon down
   on 09-15, because only gdaemon's `apply_locked` in `crates/gcore/src/schema/runner.rs`
   computes that verdict, and there is no non-mutating way to run it today.
3. Cutover builds whatever is in the shared working tree.
4. The shutdown record written by `write_shutdown_intent` in `src/gobby/shutdown_intent.py`
   stores only `sender_pid`; attributing a restart took transcript archaeology.

Out of scope, tracked elsewhere: the spawn-time MCP/HTTP timeouts game-goblins also saw (no
single culprit found; needs a day of metrics first) and the observability compose stack
(task #22401). game-goblins task 5df38ebc closes their #432 and keeps #225 as the backstop.

## Constraints
`kind: framing`

- No rollback mechanism, no private replacement set, no second copy of the binaries. The
  guarantee is prove-then-stop.
- `format_shutdown_source` output is pinned by `tests/servers/routes/test_admin.py` (line 307
  area) and `tests/test_runner_lifecycle.py` (line 3185 area) and stays byte-identical.
- `gobby start` keeps its own checks unchanged; only `restart` and `cutover` gain the preflight.
- The plan command never takes the schema apply advisory lock and never runs `CREATE SCHEMA`.
  It is read-only and fails closed if it observes a mid-apply state.
- `src/gobby/cli/daemon.py` (997 lines) and `crates/gcore/src/schema/runner.rs` (876 lines)
  are both under the 1,000-line ceiling and must stay there; each deliverable that touches
  them moves new code into a new module rather than growing them.
- The dirty, foreign `.gobby/plans/gdaemon-front-door.md` still names
  `_schema_restart_refusal` near line 257; it is uncommitted work of another session and is
  left alone.
- Implementation runs in an isolated worktree with its own `CARGO_TARGET_DIR`. Shipping is a
  crate plus Python change, so it goes live through the announced cutover flow from the main
  checkout, never while a close validator is running.

## P1: Read-only schema plan
`kind: framing`

**Goal**: gdaemon can answer "would `schema apply` succeed against this database right now"
without writing anything, using the exact validation `apply_locked` performs.

### 1.1 SchemaRunner::plan and gdaemon schema plan [category: code]
`kind: deliverable`

Targets:
- `crates/gcore/src/schema/runner.rs::apply_locked`
- `crates/gcore/src/schema/runner.rs::apply_pending_migrations`
- `crates/gcore/src/schema/runner_plan.rs`
- `crates/gcore/src/schema/mod.rs`
- `crates/gdaemon/src/main.rs::SchemaCommand`
- `crates/gdaemon/src/main.rs::main`
- `crates/gcore/src/schema/runner_tests.rs::*` — scope-reason: adds the plan behaviour tests beside the existing apply tests
- `crates/gdaemon/tests/cli_contract.rs::*` — scope-reason: adds the plan subcommand contract tests
- `crates/gdaemon/tests/schema_cli.rs::*` — scope-reason: adds the plan integration tests against a scratch schema

`runner.rs` is 876 lines. Split the new plan code out of it: move `PlanReport`,
`validate_lineage`, `resolve_pending_migrations` and `plan` into the new
`crates/gcore/src/schema/runner_plan.rs`, included from `runner.rs` with the same
`#[path = "runner_plan.rs"] mod runner_plan;` pattern `runner_adoption.rs` already uses
(runner.rs lines 17-19). `runner.rs` must shrink or stay flat, never grow.

Changes in `crates/gcore/src/schema/`:

- New `pub struct PlanReport { pub database_head: i32, pub code_head: i32, pub baseline_pending: bool, pub pending_versions: Vec<i32> }`, a sibling of `ApplyReport`.
- New `fn validate_lineage(&mut self, backup: Option<&VerifiedBackup>) -> Result<(i32, BaselineState), SchemaError>`
  (exact backup parameter type as `apply_locked` receives today), extracted from the head of
  `apply_locked`: the head reads, the "database is newer than this runner" refusal, the backup
  checks, `classify_baseline_state`, and the `CorruptPartial` arm that returns
  `SchemaError::Unsupported("unrecognized schema lineage; recreate from a verified backup")`.
  `apply_locked` calls `validate_lineage` and keeps its existing apply arms unchanged, so the
  apply-side error text and ordering do not move.
- Split `apply_pending_migrations` at its `let mut count = 0;` line into
  `resolve_pending_migrations(...) -> Result<Vec<&EmbeddedMigration>, SchemaError>` (receipt
  validation plus the destructive-authorization checks, no writes) and the existing execution
  loop, which now iterates the resolved list.
- New `pub fn plan(&mut self) -> Result<PlanReport, SchemaError>`: `verify_embedded_assets`,
  `set_search_path` (session-local), `validate_lineage(None)`. Fresh lineage: `require_pg_search`
  and `verify_adopted_columns`, every embedded migration version pending, `baseline_pending = true`.
  Existing lineage: `resolve_pending_migrations(..., destructive_authorized = false)`, which
  returns `Ok` with the owed versions when the lineage is recognised and only non-destructive
  migrations are pending. `plan` performs lineage validation and pending resolution only: no
  `ensure_schema` (it runs `CREATE SCHEMA`), no `acquire_apply_lock` (it polls up to 600 s),
  and no call into `verify` or `verify_adopted_columns` beyond the fresh-lineage check above.
- Export `PlanReport` from `crates/gcore/src/schema/mod.rs` next to `ApplyReport`.

**Granularity:** seven acceptance items, four production files. Kept as one deliverable
because `plan`, its extracted helpers and the CLI subcommand share one lifecycle (the runner's
validation path) and are only testable together against a live schema.

Changes in `crates/gdaemon/src/main.rs`:

- Add `SchemaCommand::Plan { schema: Option<String> }` as a sibling of `Apply`, not a flag on
  it, so it never interacts with `--destructive`, backups or the maintenance epoch.
- New `fn plan_schema(schema: Option<&str>) -> Result<()>` mirroring `apply_schema`'s
  connection handling only (`validate_schema_name`, `resolve_database_url`, the
  `connect_readwrite` call with the redacted-URL error) without the backup, destructive or
  maintenance-epoch branches. It never calls `verify_database_identity` and never calls
  `SchemaRunner::verify`: those bail exactly when the database head differs from the embedded
  head, which is the normal state of every migration-owing restart and of every cutover. The
  command runs `SchemaRunner::plan` and prints one line:
  `schema <name> plan: database v<db>, code v<code>, baseline_pending=<bool>, pending_migrations=<n> [<versions>]`.
  Errors propagate through anyhow so stderr carries the same `SchemaError` text apply emits.
- `main` dispatches the new variant.

**Research context:**

- Observed: `apply_locked` (runner.rs 118-173) is the only place that computes the lineage
  verdict; `apply_internal` (96-116) wraps it with `ensure_schema`, `set_search_path` and the
  advisory lock. `apply_pending_migrations` (656-739) validates receipts before its
  `let mut count = 0;` loop. The code index lists no callers of `apply_locked` outside this
  file; the literal sweep confirms `apply_internal -> apply_locked` and
  `apply_locked -> apply_pending_migrations` (line 162) are the only consumers.
- Reusable fixtures: `runner_tests.rs` `unrecognized_receipt_still_rejects` (1233-1268) builds a
  corrupt lineage; the `pg_try_advisory_lock(hashtext('postgres_migrations_apply'), 0)` probe
  (1540-1555) shows whether the apply lock is held. `schema_cli.rs` `ScratchSchema` and
  `scoped_database_url` give an isolated schema per test; `cli_contract.rs`
  `apply_has_no_dsn_argument` and `connection_errors_redact_dsn_credentials` are the shapes for
  the plan contract tests.
- Rejected: a `--dry-run` flag on `Apply` (would inherit destructive/backup/epoch plumbing); a
  Python-side re-implementation of the lineage rules (two sources of truth, the exact defect
  that hid the 09-15 failure).
- Planned verification (not yet run): `cargo fmt -p gobby-core -p gobby-daemon -- --check`,
  `cargo clippy -p gobby-daemon -p gobby-core --all-targets -- -D warnings`,
  `GOBBY_SCHEMA_TEST_DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test cargo nextest run -p gobby-core --features postgres -E 'test(plan)'`,
  `GOBBY_TEST_POSTGRES_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test cargo nextest run -p gobby-daemon`.
  Expected: all green, `wc -l crates/gcore/src/schema/runner.rs` at or below 876.

**Acceptance:**

- 1.1.1 - `PlanReport` and `SchemaRunner::plan` exist in the new module and `runner.rs` did not grow. file: `crates/gcore/src/schema/runner_plan.rs`. symbol: `plan`.
- 1.1.2 - `apply_locked` delegates its validation head to `validate_lineage` and every existing apply test still passes unchanged. symbol: `validate_lineage`.
- 1.1.3 - Plan on a fresh database reports the baseline and every migration pending and writes nothing (no schema, no receipts). test: `crates/gcore/src/schema/runner_tests.rs::plan_on_fresh_database_reports_everything_pending_and_writes_nothing`.
- 1.1.4 - Plan rejects a corrupt lineage with the exact `unrecognized schema lineage` text and rejects a database newer than the runner, touching no receipts in either case. test: `crates/gcore/src/schema/runner_tests.rs::plan_rejects_corrupt_lineage_without_touching_receipts`. test: `crates/gcore/src/schema/runner_tests.rs::plan_rejects_newer_database`.
- 1.1.5 - Plan after a completed apply reports zero pending and never holds the apply advisory lock. test: `crates/gcore/src/schema/runner_tests.rs::plan_after_apply_is_empty`. test: `crates/gcore/src/schema/runner_tests.rs::plan_does_not_hold_the_apply_lock`.
- 1.1.6 - `gdaemon schema plan` is exposed, takes no DSN argument, redacts credentials in connection errors, does not create a missing schema and reports no pending work after apply. test: `crates/gdaemon/tests/cli_contract.rs::schema_help_exposes_plan`. test: `crates/gdaemon/tests/schema_cli.rs::plan_reports_fresh_schema_without_creating_it`. test: `crates/gdaemon/tests/schema_cli.rs::plan_after_apply_reports_no_pending`.
- 1.1.7 - Plan against an existing lineage that owes pending non-destructive migrations returns Ok, reports those versions pending, and performs no schema verification. test: `crates/gcore/src/schema/runner_tests.rs::plan_reports_pending_without_verifying_identity`.

## P2: Python preflight shared by restart and cutover
`kind: framing`

**Goal**: both commands run one preflight that proves the start half, including the
read-only schema plan, before anything is stopped or promoted.

### 2.1 Shared start preflight for gobby restart [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `src/gobby/cli/daemon_preflight.py`
- `src/gobby/cli/daemon.py::restart`
- `src/gobby/cli/daemon.py::_schema_restart_refusal`
- `src/gobby/storage/schema_contract.py::*` — scope-reason: `_run_gdaemon` gains binary and remedy parameters and returns stdout; `plan_schema` is added beside `apply_schema` and `verify_schema`
- `tests/cli/test_daemon_preflight.py`
- `tests/cli/test_cli_daemon.py::*` — scope-reason: `TestRestartCommand` gains an autouse fixture replacing the two `_schema_restart_refusal` patches and the restart worktree patch moves to the new module
- `tests/cli/test_daemon_set_coherence.py::test_restart_refuses_mixed_installed_binary_set_before_stop`
- `tests/storage/test_schema_divergence.py::*` — scope-reason: `_restart_preflight` retargets its worktree patch and a new plan-failure test is added
- `tests/storage/test_schema_contract.py::*` — scope-reason: adds the `plan_schema` child-environment tests

Consumers unchanged:
- `src/gobby/cli/__init__.py` — no-edit-reason: it only re-exports `restart`; the click signature does not change.
- `src/gobby/cli/hub_backup/cli.py` — no-edit-reason: imports `_services_start` and `_services_stop` only; the word restart appears in its prose, not as a call.
- `src/gobby/cli/pack.py` — no-edit-reason: imports `_services_stop` and `_services_start` only; no call into `restart`.
- `src/gobby/servers/routes/admin/_lifecycle.py` — no-edit-reason: defines its own HTTP `restart` route and imports only `_wait_for_daemon_health` from the daemon module.
- `tests/cli/test_cli_falkor.py` — no-edit-reason: reads `restart.params` names only (line 99); the click options of `restart` do not change.
- `tests/cli/test_daemon_remote_mode.py` — no-edit-reason: calls `_do_stop` directly with `shutdown_intent="restart"` (lines 52-76) and never invokes the `restart` command.

`daemon.py` is 997 lines. Move the preflight out of `daemon.py` into the new
`src/gobby/cli/daemon_preflight.py` (about 90 lines) and delete `_schema_restart_refusal`
from `daemon.py`, so `daemon.py` shrinks to roughly 976 lines.

`src/gobby/storage/schema_contract.py`: `_run_gdaemon(database_url, args, *, action, binary=None, remedy=...)`
resolves the installed gdaemon only when `binary` is None, keeps the existing env handling
(`DATABASE_URL` pinned, `GOBBY_EXPECTED_SCHEMA_IDENTITY` dropped), returns `result.stdout`, and
uses `remedy` in the failure suffix. The default stays `Run \`gobby install\` to refresh gdaemon`
for the three existing callers. New `plan_schema(database_url: str, *, gdaemon: Path | None = None) -> str`
runs `["schema", "plan"]` with `action="schema plan"` (rendering `gdaemon schema plan failed: ...`
like the existing `schema apply` and `schema verify` sites) and
`remedy="Fix the schema inputs or the hub before restarting"`, because reinstalling gdaemon
never repairs a lineage or pending-migration failure. It returns stdout; callers treat return
code 0 as success without parsing the line.

New `src/gobby/cli/daemon_preflight.py`. `worktree_daemon_refusal` is imported at module
level, exactly as `daemon.py` does today, so `monkeypatch.setattr("gobby.cli.daemon_preflight.worktree_daemon_refusal", ...)`
resolves. Only the cycle-prone helpers stay lazy inside the function: `require_cli_database`
from `gobby.cli.runtime`, and `binary_set_apply_refusal` and `schema_apply_refusal` from
`gobby.storage.schema_divergence`. `probe_set_member_identity`, `expected_schema_identity`,
`plan_schema` and the two error types are module-level imports as well.

```python
def restart_start_refusal(ctx: click.Context, gdaemon: Path | None = None) -> str | None:
    """Reason the start half would fail, or None. gdaemon=None proves the installed set;
    a candidate path proves an unpromoted build (cutover, before promotion).
    An unreadable hub URL skips the schema plan and leaves the start unproven."""
    if refusal := worktree_daemon_refusal():
        return refusal
    database: HubDatabase | None = None
    if gdaemon is None:
        if refusal := binary_set_apply_refusal():
            return refusal
        database = _open_hub(ctx)             # require_cli_database(ctx, apply_migrations=False), or None on any error
        if refusal := schema_apply_refusal(database):
            return refusal
    else:
        if refusal := _candidate_identity_refusal(gdaemon):
            return refusal
    url = _hub_url(database)                  # see below; None means "skip the plan"
    if url is None:
        logger.debug("Restart preflight skipped the schema plan: no readable hub URL")
        return None
    try:
        plan_schema(url, gdaemon=gdaemon)
    except SchemaContractError as exc:
        return str(exc)
    return None


def _candidate_identity_refusal(gdaemon: Path) -> str | None:
    """Candidate mode fails closed: anything the probe or the pin cannot read is a refusal."""
    try:
        candidate = probe_set_member_identity(gdaemon, "gdaemon")
        expected = expected_schema_identity()
    except (BinarySetCoherenceError, SchemaContractError) as exc:
        return f"candidate gdaemon cannot be verified: {exc}"
    if candidate != expected:
        return f"candidate gdaemon identity {candidate} does not match the checkout pin {expected}"
    return None
```

Hub URL, `_hub_url(database)`: `database.conninfo` when the read-only hub opened in installed
mode (the same string the daemon uses at `src/gobby/storage/postgres.py` around line 383),
else the bootstrap `database_url` read from `~/.gobby/bootstrap.yaml` through the existing
bootstrap loader. Candidate mode never opens the hub, so it always resolves the bootstrap
URL, which is the same URL gdaemon's own `resolve_database_url` picks for the daemon that
`restart` will start. When neither source is readable the plan is skipped with a debug log,
the same "unreadable is not a refusal" convention `schema_apply_refusal` follows for the
installed-mode hub; the candidate identity check above is the one place that fails closed,
because an unverifiable candidate must never be promoted.

`src/gobby/cli/daemon.py`: `restart` replaces its `worktree_daemon_refusal()` call and its
`_schema_restart_refusal(ctx)` call with one `restart_start_refusal(ctx)` before `_do_stop`,
printing `Refusing to restart: <reason>` and `The running daemon was left alone.` and exiting 1.
`start` is untouched.

Tests:

- `tests/cli/test_cli_daemon.py::TestRestartCommand` (class starts at line 1227) gains a class
  autouse fixture that patches `gobby.cli.daemon.restart_start_refusal` to return None; it
  replaces the two `patch("gobby.cli.daemon._schema_restart_refusal", ...)` lines (1572,
  1655). Without it the via-service tests would exec the real installed gdaemon.
  `test_restart_refuses_linked_worktree_before_stopping` (line 2516 area) patches
  `gobby.cli.daemon_preflight.worktree_daemon_refusal` instead; the start test at line 2500
  keeps patching `gobby.cli.daemon.worktree_daemon_refusal`.
- `tests/cli/test_daemon_set_coherence.py::test_restart_refuses_mixed_installed_binary_set_before_stop`
  and `tests/storage/test_schema_divergence.py::_restart_preflight` retarget their worktree
  patch to the new module. `_restart_preflight`'s `subprocess.run` stub dispatches on argv:
  `version --json` keeps returning the installed identity and `schema plan` returns exit 0
  with the one-line report, so the existing four restart tests run the real `plan_schema`
  step and `test_restart_proceeds_when_the_hub_merely_owes_a_migration` (line 548) proves a
  migration-owing hub is not refused. The plan step is never stubbed out wholesale.
- New `tests/storage/test_schema_divergence.py::test_restart_refuses_before_stopping_when_the_schema_plan_fails`:
  the `subprocess.run` stub dispatches on argv (`version --json` succeeds, `schema plan` fails
  with the lineage text); assert the refusal carries `unrecognized schema lineage` and
  `stopped == []`.
- New `tests/cli/test_daemon_preflight.py`: plan failure becomes a refusal carrying gdaemon
  stderr; timeout and OSError become refusals; no readable URL returns None; check order is
  worktree, set, identity, plan (each earlier failure short-circuits); candidate mode uses
  the given binary path, pins `DATABASE_URL`, drops `GOBBY_EXPECTED_SCHEMA_IDENTITY`, skips
  the installed-set checks and reaches `plan_schema` with the bootstrap URL; candidate identity
  different from the pin is a refusal; a candidate binary the probe cannot run
  (`BinarySetCoherenceError`) and an unreadable pin (`SchemaContractError`) are refusals, never
  tracebacks; `worktree_daemon_refusal` is a module attribute the tests can patch.

**Granularity:** nine acceptance items, three production files. Kept as one deliverable
because the preflight module, its `restart` wiring and `plan_schema` are one behaviour with
one failure surface; the only split available is a test-only leaf, which the contract rejects.
- `tests/storage/test_schema_contract.py::test_plan_uses_candidate_binary_and_pins_database_url`
  following `test_verify_pins_database_without_checkout_identity_in_child_environment`.

**Research context:**

- Observed: `restart` (daemon.py 760-792) already checks before stopping and prints the two
  refusal lines; `_schema_restart_refusal` (707-725) calls `binary_set_apply_refusal` and
  `schema_apply_refusal` from `src/gobby/storage/schema_divergence.py` (lines 143 and 228)
  with `require_cli_database(ctx, apply_migrations=False)`. `_run_gdaemon` (schema_contract.py
  62-95) resolves the binary with `resolve_native_bin("gdaemon")`, pins `DATABASE_URL`, drops
  `GOBBY_EXPECTED_SCHEMA_IDENTITY`, uses a 300 s timeout and raises `SchemaContractError`.
  `probe_set_member_identity(binary, member)` lives in `src/gobby/install/bin_set_coherence.py`
  line 46; `expected_schema_identity()` in schema_contract.py line 25.
- Consumers: `restart` is imported by `src/gobby/cli/__init__.py` (re-export) and
  `src/gobby/cli/cutover.py` (edited in 2.2). `_schema_restart_refusal` has one caller
  (`restart`) and two test patch sites. `worktree_daemon_refusal` (defined in
  `src/gobby/utils/dev.py` line 134) keeps its `start` and `runner.main` callers; only the
  restart patch sites move.
- Rejected: keeping the plan check inside `daemon.py` (would cross 1,000 lines); parsing the
  plan stdout in Python (return code is the contract, the line is for humans).
- Planned verification (not yet run):
  `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/cli/test_cli_daemon.py tests/cli/test_daemon_preflight.py tests/cli/test_daemon_set_coherence.py tests/storage/test_schema_divergence.py tests/storage/test_schema_contract.py`,
  `uv run ruff check src/ tests/`, `uv run mypy src/`, `wc -l src/gobby/cli/daemon.py` below 1000.

**Acceptance:**

- 2.1.1 - The new module exposes `restart_start_refusal(ctx, gdaemon=None)` running worktree, installed-set, identity and schema-plan checks in that order and skipping the plan only when no hub URL is readable. file: `src/gobby/cli/daemon_preflight.py`. symbol: `restart_start_refusal`.
- 2.1.2 - `plan_schema` runs `gdaemon schema plan` with the installed or the given candidate binary, the database URL pinned and the checkout identity dropped from the child environment. test: `tests/storage/test_schema_contract.py::test_plan_uses_candidate_binary_and_pins_database_url`.
- 2.1.3 - `restart` calls the preflight once before `_do_stop`, `_schema_restart_refusal` no longer exists, and `daemon.py` stays under 1,000 lines. symbol: `restart`. file: `src/gobby/cli/daemon.py`.
- 2.1.4 - A failing schema plan refuses the restart before the stop with the gdaemon error text. test: `tests/storage/test_schema_divergence.py::test_restart_refuses_before_stopping_when_the_schema_plan_fails`.
- 2.1.5 - Preflight unit tests cover plan failure, timeout, OSError, missing URL, check order, candidate mode, an unreadable candidate binary and an unreadable pin (both refusals). test: `tests/cli/test_daemon_preflight.py`.
- 2.1.6 - The existing restart tests pass with the autouse preflight fixture and the retargeted worktree patches. test: `tests/cli/test_cli_daemon.py::TestRestartCommand`. test: `tests/cli/test_daemon_set_coherence.py::test_restart_refuses_mixed_installed_binary_set_before_stop`.
- 2.1.7 - A restart proceeds without refusal when the hub merely owes a migration, exercising the real plan step rather than a stub. test: `tests/storage/test_schema_divergence.py::test_restart_proceeds_when_the_hub_merely_owes_a_migration`.
- 2.1.8 - `daemon_preflight.py` binds `worktree_daemon_refusal` as a module-level attribute so the retargeted patch sites resolve. test: `tests/cli/test_daemon_preflight.py::test_worktree_refusal_is_patchable_at_module_level`.
- 2.1.9 - Candidate mode never opens the hub and reaches `plan_schema` with the bootstrap `database_url` and the candidate binary path. test: `tests/cli/test_daemon_preflight.py::test_candidate_mode_plans_with_bootstrap_url`.

### 2.2 Cutover proves the candidate and refuses dirty schema inputs [category: code] (depends: 2.1)
`kind: deliverable`

Targets:
- `src/gobby/cli/cutover.py::run_cutover`
- `src/gobby/cli/cutover.py::cutover`
- `tests/cli/test_cutover.py::*` — scope-reason: every `run_cutover` caller gains `start_refusal`, `_invoke_cli` stubs the dirty gate, and the new gate tests are added

`src/gobby/cli/cutover.py`:

- `run_cutover(root, bin_dir, *, restart_daemon, start_refusal: Callable[[Path], str | None])`:
  after `_build_artifacts`, call `start_refusal(artifacts["gdaemon"])` and raise
  `CutoverError(f"refusing to promote: {reason}")` **before** `promote_workspace_binary_set`.
  Nothing is promoted and nothing is stopped on refusal. `_verify_restart_target` and
  `restart_daemon()` stay where they are.
- New `_dirty_schema_inputs(root: Path) -> list[str]` runs
  `git status --porcelain --untracked-files=all -- crates/gcore/assets/schema crates/gcore/src/schema src/gobby/storage/schema_expected_identity.json`
  through `_run` and returns the listed paths; a git failure raises `CutoverError` (fails closed).
  All four 09-15 paths fall inside this scope; routine non-schema dirt on the shared checkout
  (today: `crates/gcode/**`, `crates/gterminal/vendor/.../zig-out.bak-*`) does not block.
- The `cutover` click command gains `--allow-dirty` (flag, default off). Without it, a
  non-empty `_dirty_schema_inputs` result is a `ClickException` naming the dirty paths and
  the flag. The command passes `start_refusal=lambda candidate: restart_start_refusal(ctx, candidate)`
  and keeps `restart_daemon` as the second, installed-set proof inside `restart`.

Tests in `tests/cli/test_cutover.py`: existing `run_cutover` callers pass
`start_refusal=lambda _p: None`; `_invoke_cli` monkeypatches `_dirty_schema_inputs` to return
`[]`. New tests: refusal before promotion when the candidate plan fails (bin_dir untouched,
`restart_daemon=pytest.fail`); the CLI refuses dirty schema inputs and names them;
`--allow-dirty` skips the gate; the gate ignores non-schema dirt and sees schema dirt against a
real `git init` repository in `tmp_path`; the gate fails closed when git is unavailable.
`test_cutover_has_no_private_replacement_or_rollback_machinery` keeps passing: no replacement
set, no sidecars.

**Research context:**

- Observed: `run_cutover` (cutover.py 125-145) is build, promote, `_verify_restart_target`,
  `restart_daemon()`; the `cutover` command (158-174) wraps `ctx.invoke(restart, ...)` and maps
  a non-zero `SystemExit` to `CutoverError("daemon restart failed (exit N)")`, which is the exact
  09-15 error line. `_run` (51-74) is the existing subprocess helper and `_workspace_root`
  (35-41) resolves `root`. `tests/cli/test_cutover.py` calls `run_cutover` at lines 80 and 117
  and `_invoke_cli` (157-170) drives the click command with a monkeypatched `run_cutover`.
- Consumers: `run_cutover` is called only by `cutover` and the tests in this deliverable's
  Targets. `cutover` is registered in the CLI group and has no other callers.
- Rejected: gating on any dirty file (blocks routine coordinator work and invites
  `--allow-dirty` by reflex); proving the candidate after promotion (a refusal there would
  already have replaced the installed set).
- Planned verification (not yet run):
  `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/cli/test_cutover.py`,
  `uv run ruff check src/ tests/`, `uv run mypy src/`.

**Acceptance:**

- 2.2.1 - `run_cutover` refuses before promotion when the candidate plan fails, leaving `bin_dir` untouched and never calling `restart_daemon`. test: `tests/cli/test_cutover.py::test_run_cutover_refuses_before_promotion_when_candidate_plan_fails`.
- 2.2.2 - The CLI refuses when schema inputs are dirty and names the paths and the override flag. test: `tests/cli/test_cutover.py::test_cli_refuses_dirty_schema_inputs_and_names_them`.
- 2.2.3 - `--allow-dirty` skips the dirty gate and nothing else. test: `tests/cli/test_cutover.py::test_cli_allow_dirty_skips_the_gate`.
- 2.2.4 - `_dirty_schema_inputs` sees only schema-input dirt against a real git repository and fails closed without git. test: `tests/cli/test_cutover.py::test_dirty_schema_inputs_is_scoped_to_schema_paths`. test: `tests/cli/test_cutover.py::test_dirty_schema_inputs_fails_closed_without_git`.
- 2.2.5 - No private replacement or rollback machinery appears. test: `tests/cli/test_cutover.py::test_cutover_has_no_private_replacement_or_rollback_machinery`.

## P3: Attribution
`kind: framing`

**Goal**: the shutdown record says which command sent the stop, so the next incident is
attributed from `~/.gobby/shutdown_source.json` and the daemon log instead of transcripts.

### 3.1 Shutdown record names the sending command [category: code]
`kind: deliverable`

Targets:
- `src/gobby/shutdown_intent.py::write_shutdown_intent`
- `src/gobby/shutdown_intent.py::ShutdownIntentRecord`
- `src/gobby/runner_maintenance/lifecycle.py::setup_signal_handlers`
- `tests/test_shutdown_intent.py::*` — scope-reason: adds the sender-detail tests beside the existing marker tests
- `tests/test_runner_lifecycle.py::*` — scope-reason: adds the `Shutdown sender:` log assertion beside the existing `Shutdown source:` test

Consumers unchanged:
- `src/gobby/runner.py` — no-edit-reason: `_on_lease_loss` passes no `sender_pid`; the daemon is the sender there and its own argv is the correct attribution.
- `src/gobby/cli/hub_maintenance.py` — no-edit-reason: the maintenance marker writer is the CLI process that owns the campaign; its argv is the correct attribution.
- `tests/e2e/test_terminal_client_stack.py` — no-edit-reason: calls `write_shutdown_intent` with the same positional and keyword contract; extra `details` keys are ignored by its assertions.
- `tests/servers/routes/test_admin.py` — no-edit-reason: asserts the `format_shutdown_source` string, which does not change.
- `tests/terminals/test_host_shutdown_preservation.py` — no-edit-reason: asserts the `drain_terminals` detail only.
- `tests/terminals/test_runtime_contract.py` — no-edit-reason: passes an explicit marker home and reads `intent` only.
- `tests/hooks/test_health_gate.py` — no-edit-reason: writes a restart marker into `tmp_path` (lines 68, 160) and asserts the hook health gate, not the marker contents.
- `tests/test_runner_lease_lifecycle.py` — no-edit-reason: monkeypatches `runner_module.write_shutdown_intent` with a recorder (line 204); the real writer is not called.
- `tests/utils/test_utils_daemon_client.py` — no-edit-reason: writes a restart marker into `tmp_path` (line 225) and asserts client behaviour, not the marker contents.
- `src/gobby/runner_lifecycle.py` — no-edit-reason: calls `setup_signal_handlers` with the same arguments (line 147); only the handler body's logging changes.
- `src/gobby/runner_maintenance/__init__.py` — no-edit-reason: re-exports `setup_signal_handlers` unchanged (line 36).

`src/gobby/shutdown_intent.py::write_shutdown_intent`: when `sender_pid is None` the writer is
the sender (every CLI stop path, the daemon's own lease-loss and HTTP-route writers), so merge
`sender_argv=list(sys.argv)`, `sender_ppid=os.getppid()` and `sender_cwd=os.getcwd()` into
`details` before writing. An explicit `sender_pid` records none of them, because the writer
is then speaking for another process. For the HTTP routes the record therefore names the
daemon itself, which is true (the daemon signals itself) and is already labelled by the
`http_restart` and `http_shutdown` sources.

`ShutdownIntentRecord` gains a `sender_detail` property, modelled on `drain_terminals`: it
returns `argv=[...] ppid=<n> cwd=<path>` from `details` or None when any key is absent.
`format_shutdown_source` is unchanged.

`src/gobby/runner_maintenance/lifecycle.py::setup_signal_handlers`: inside `handle_shutdown`,
directly after the existing `logger.info("Shutdown source: %s", ...)` line, log
`Shutdown sender: %s` once when `shutdown_record.sender_detail` is not None.

Tests: `tests/test_shutdown_intent.py` adds `test_write_shutdown_intent_records_sender_when_writer_is_sender`
(argv, ppid and cwd land under `details` in both marker files and merge with caller details
such as `drain_terminals`) and `test_explicit_sender_pid_records_no_sender_detail`.
`tests/test_runner_lifecycle.py` adds `test_signal_handler_logs_shutdown_sender_when_recorded` beside
the existing `Shutdown source:` assertion at line 3185, and the existing test proves the
source line is byte-identical.

**Research context:**

- Observed: `write_shutdown_intent` (shutdown_intent.py 94-116) builds `source`, `intent`,
  `sender_pid or os.getpid()`, `timestamp`, optional `details`, and writes both
  `shutdown_source.json` and `shutdown_intent_active.json` via `_write_marker_atomically`.
  `ShutdownIntentRecord` (35-56) already exposes `preserve_agents` and `drain_terminals` from
  `details`; `_record_from_marker_data` (347-376) carries `details` through, so no reader
  changes are needed. `write_shutdown_source` in lifecycle.py (48-73) is the wrapper the CLI
  (`src/gobby/cli/utils_shutdown.py` line 113, `src/gobby/cli/installers/service.py` lines
  597 and 645) and the admin routes (`src/gobby/servers/routes/admin/_lifecycle.py` lines
  266-272 and 358-364) use; none of them pass `sender_pid`, so they all gain attribution
  without edits. The 09-14 and 09-15 senders would have been recorded as
  `uv run gobby restart --wait` and `uv run gobby cutover --path .` with their cwd.
- Rejected: a new record field and a new `format_shutdown_source` shape (two pinned tests and
  the admin status endpoint would change for no reader benefit); per-call-site argv capture
  (the shared writer is the single guard).
- Planned verification (not yet run):
  `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/test_shutdown_intent.py tests/test_runner_lifecycle.py tests/servers/routes/test_admin.py`.

**Acceptance:**

- 3.1.1 - A writer that is the sender records `sender_argv`, `sender_ppid` and `sender_cwd` under `details` in both marker files, merged with caller-supplied details. test: `tests/test_shutdown_intent.py::test_write_shutdown_intent_records_sender_when_writer_is_sender`.
- 3.1.2 - An explicit `sender_pid` records no sender details. test: `tests/test_shutdown_intent.py::test_explicit_sender_pid_records_no_sender_detail`.
- 3.1.3 - `ShutdownIntentRecord.sender_detail` renders the three keys or returns None. symbol: `ShutdownIntentRecord.sender_detail`.
- 3.1.4 - The signal handler logs `Shutdown sender:` once after an unchanged `Shutdown source:` line. test: `tests/test_runner_lifecycle.py::test_signal_handler_logs_shutdown_sender_when_recorded`.

## P4: Documentation
`kind: framing`

**Goal**: operators and agents learn the new refusal, the dirty gate and the read-only plan
from the same pages that already describe restart and cutover.

### 4.1 Document the preflight, the dirty gate and the plan command [category: docs] (depends: 1.1, 2.1, 2.2, 3.1)
`kind: deliverable`

Targets:
- `docs/guides/cli-commands.md`
- `docs/guides/hub-install-contract.md`
- `crates/AGENTS.md`
- `AGENTS.md`

- `docs/guides/cli-commands.md` under `### gobby cutover` (line 316 area): the `[--allow-dirty]`
  flag, the schema-input dirty gate with its three paths, the candidate `gdaemon schema plan`
  proof, and the sentence "on refusal nothing is promoted and nothing is stopped". Under
  `### gobby restart` (line 129 area): one sentence that the start half is proven (installed
  set, identity, read-only schema plan) before the stop.
- `docs/guides/hub-install-contract.md` under `## Native binaries and schema changes` (line 26
  area): `gdaemon schema plan` as the read-only dry run both commands use, with its one-line
  output format.
- `crates/AGENTS.md` line 13 comment on `uv run gobby cutover`: mention the pre-promotion
  proof and the dirty gate.
- `AGENTS.md` Development Commands paragraph that starts "Start the daemon only from the main
  checkout" (line 134 area): one sentence on restart and cutover refusing before the stop when
  the start would fail, and on `--allow-dirty`.

**Research context:**

- Observed anchors: `docs/guides/cli-commands.md` has `### \`gobby restart\`` at line 129 and
  `### \`gobby cutover\`` at line 316; `docs/guides/hub-install-contract.md` has
  `## Native binaries and schema changes` at line 26; `crates/AGENTS.md` lines 11-14 hold the
  cargo and cutover command comments; `AGENTS.md` line 134 starts the daemon-start paragraph.
- Planned verification (not yet run): the prose names `--allow-dirty`, `gdaemon schema plan`
  and the three dirty-gate paths exactly as implemented in 2.2; `uv run ruff check` is not
  affected.

**Acceptance:**

- 4.1.1 - The CLI guide documents `--allow-dirty`, the dirty gate scope, the candidate plan and the nothing-promoted-nothing-stopped refusal under cutover, and the proven start half under restart. file: `docs/guides/cli-commands.md`.
- 4.1.2 - The hub install contract names `gdaemon schema plan` as the read-only dry run and shows its output line. file: `docs/guides/hub-install-contract.md`.
- 4.1.3 - The crates instructions describe the cutover proof and dirty gate. file: `crates/AGENTS.md`.
- 4.1.4 - The root instructions state that restart and cutover refuse before stopping when the start would fail. file: `AGENTS.md`.

## 5 End-to-end verification
`kind: verification`

Worktree, before merge:

```bash
cargo fmt -p gobby-core -p gobby-daemon -- --check
cargo clippy -p gobby-daemon -p gobby-core --all-targets -- -D warnings
GOBBY_SCHEMA_TEST_DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test cargo nextest run -p gobby-core --features postgres -E 'test(plan)'
GOBBY_TEST_POSTGRES_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test cargo nextest run -p gobby-daemon
uv run ruff format src/ tests/ && uv run ruff check src/ tests/ && uv run mypy src/
DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/cli/test_cli_daemon.py tests/cli/test_cutover.py tests/cli/test_daemon_preflight.py tests/cli/test_daemon_set_coherence.py tests/storage/test_schema_divergence.py tests/storage/test_schema_contract.py tests/test_shutdown_intent.py tests/test_runner_lifecycle.py tests/servers/routes/test_admin.py
uv run gobby test-types audit tests/ --baseline .gobby/test-types-baseline.json --fail-on-new
wc -l src/gobby/cli/daemon.py crates/gcore/src/schema/runner.rs
```

Live proof by the coordinator from the main checkout after merge and the announced cutover:
the new `gdaemon schema plan` against the dev hub prints `pending_migrations=0`; a restart
with `--wait` shows no refusal and `~/.gobby/shutdown_source.json` carries `sender_argv`;
a deliberately dirtied identity pin makes cutover refuse before building, and restoring the
pin clears it.

Risks accepted: a plan that overlaps a real apply can refuse spuriously (fail closed, rare,
and the only routine apply is daemon startup); an unreadable hub URL skips the plan and
leaves the start unproven, which the preflight docstring says; a cutover now runs two plans
(candidate, then installed inside `restart`), about one to two seconds.

## V1 Plan Changelog
`kind: verification`

- Initial draft authored from the approved interactive plan (~/.claude/plans/lazy-singing-jellyfish.md) and task #22403; base validation clean.

Round 1 (plan-adversary-taskless, Claude Opus xhigh, run 5ea01d7b): verdict needs_review with two blocking and three nit findings. Coordinator gobby#12967 accepted all five under delegated authority. PR1-001: plan() and gdaemon schema plan must never call verify_database_identity; pin the migration-owing case in Rust and in the restart preflight test. PR1-002: bind database before the installed/candidate branch; candidate mode resolves the bootstrap database_url. PR1-003: candidate identity probe and pin errors become refusal strings; the candidate branch fails closed. PR1-004: plan_schema passes action="schema plan" and a remedy naming the real fix, matching task #22403. PR1-005: daemon_preflight imports worktree_daemon_refusal at module level so the retargeted patch sites resolve.

```json plan-review-round
{"evidence_id":"85cf6979-c6d4-480d-b35e-449b128ab301","plan_hash":"96747e5441675bf72db058496c44331a8fd12869b9f8e661c2fc713c82a5b07f","round_number":1,"round_result":{"artifact_path":".gobby/plans/daemon-restart-preflight.md","coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"e1d7167cc30a23709a744296927f9278a73b34b3f72b672f2f0dd8d1952f4782","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":15,"emitted_findings":5,"total":20},"evidence_id":"85cf6979-c6d4-480d-b35e-449b128ab301","lanes":[{"candidate_count":4,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":10,"lane_id":"repository_blast_radius","status":"delegated-verified"},{"candidate_count":6,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":5,"manifest_digest":"3811370cc30b251b70b43e5285cf87e2f07cd9da7af4bda23d02ca05428bc51b","status":"valid"},"source_digest":"389045ca0cde87e83c855f98934157daa6fb74dd8fe7fd1dfca3d9e99ff3f360","version":1},"evidence_id":"85cf6979-c6d4-480d-b35e-449b128ab301","findings":[{"category":"weak-testability","check_key":"plan-succeeds-with-pending-migrations-unproven","description":"Nothing in the plan proves that SchemaRunner::plan returns Ok when an existing lineage still owes pending non-destructive migrations. That is the state of every ordinary restart on an upgraded checkout and, by construction, of every cutover. 1.1 tells the implementer that plan_schema mirrors verify_schema's connection handling including 'identity verification'; the only identity verification in crates/gdaemon/src/main.rs::verify_schema is verify_database_identity (main.rs:385-406), which bails precisely when the heads differ. Read literally, plan() refuses every migration-owing restart and every cutover. tests/storage/test_schema_divergence.py::test_restart_proceeds_when_the_hub_merely_owes_a_migration is neutered by 2.1's instruction that _restart_preflight 'also stubs plan_schema'.","finding_id":"PR1-001","fix":"State explicitly in 1.1 that plan() performs lineage validation and pending resolution only and never calls verify_database_identity or SchemaRunner::verify, then add the acceptance items in the typed repair so both the Rust behaviour and the Python preflight pin the migration-owing case.","location":".gobby/plans/daemon-restart-preflight.md section 1.1 (acceptance 1.1.3/1.1.5/1.1.6 and the plan_schema paragraph) and section 2.1 (the _restart_preflight test note)","prevention":"When a new read-only command is specified as mirroring an existing command, name the functions it does and does not call, rather than referring to the reference implementation by prose label.","repairs":[{"items":[{"artifact":"test: `crates/gcore/src/schema/runner_tests.rs::plan_reports_pending_without_verifying_identity`","prose":"Plan against an existing lineage that owes pending non-destructive migrations returns Ok, reports those versions pending, and performs no schema verification"}],"kind":"add_acceptance","section_id":"1.1"},{"items":[{"artifact":"test: `tests/storage/test_schema_divergence.py::test_restart_proceeds_when_the_hub_merely_owes_a_migration`","prose":"A restart proceeds without refusal when the hub merely owes a migration, exercising the real plan step rather than a stub"}],"kind":"add_acceptance","section_id":"2.1"}],"root_cause":"The plan describes its reference implementation by an ambiguous prose label ('identity verification') that names a routine whose failure condition is the plan's own normal operating condition.","section_id":"1.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"preflight-candidate-branch-hub-url-undefined","description":"The pseudocode binds `database` only inside the installed branch, then calls `_hub_url(ctx, database)` unconditionally. On the candidate branch (the cutover path) `database` is never assigned, so the function raises before it reaches plan_schema; run_cutover does not catch it (CutoverError only), so the command aborts with a traceback and the proof never runs.","finding_id":"PR1-002","fix":"Bind `database = None` before the branch and state that candidate mode resolves the bootstrap database_url, the same URL gdaemon's resolve_database_url picks for the started daemon. Add an acceptance item in 2.1 that candidate mode reaches plan_schema with a resolved URL.","location":".gobby/plans/daemon-restart-preflight.md section 2.1, the restart_start_refusal pseudocode block and the following 'Hub URL' paragraph","prevention":"When plan pseudocode branches, check that every name read after the branch is bound on all paths, especially on the branch that carries the plan's primary scenario.","principle":"The path a change exists to protect must be the path specified most precisely, not the one left implicit.","section_id":"2.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"candidate-identity-probe-failure-unhandled","description":"probe_set_member_identity raises BinarySetCoherenceError on timeout, OSError, non-zero exit or unparseable JSON (src/gobby/install/bin_set_coherence.py:46-75); expected_schema_identity() raises SchemaContractError on an invalid pin (schema_contract.py:25-32). Either escapes restart_start_refusal as a traceback instead of a refusal string, and the plan leaves the fail-closed versus unreadable-is-not-a-refusal convention undecided for the candidate branch.","finding_id":"PR1-003","fix":"Convert BinarySetCoherenceError and SchemaContractError into refusal strings on the candidate branch, state that the candidate branch fails closed, and extend 2.1.5 to cover an unreadable candidate binary and an unreadable pin.","location":".gobby/plans/daemon-restart-preflight.md section 2.1, the candidate branch of the restart_start_refusal pseudocode","prevention":"When a new call site reuses a helper that raises, record which exceptions it raises and which convention the new site follows.","principle":"A guard that raises instead of refusing has not decided what to do; unreadable inputs need a stated verdict.","section_id":"2.1","severity":"nit"},{"category":"over-engineering","check_key":"run-gdaemon-remedy-knob-single-value","description":"2.1 widens _run_gdaemon with a `remedy` parameter but never supplies a non-default value; plan_schema inherits 'Run `gobby install` to refresh gdaemon', which misdirects the operator on an unrecognized schema lineage. action='plan the schema' also renders 'gdaemon plan the schema failed', inconsistent with the existing call sites. Task #22403 specifies action='schema plan' and remedy='Fix the schema inputs or the hub before restarting'.","finding_id":"PR1-004","fix":"Have plan_schema pass a remedy naming the real fix and use action='schema plan'; align the plan with task #22403.","location":".gobby/plans/daemon-restart-preflight.md section 2.1, the schema_contract.py paragraph and the matching Targets scope-reason","prevention":"A new parameter needs a caller that sets it to something other than its default within the same plan; otherwise inline the value.","principle":"A knob with one setting is speculative surface, and an inherited default remedy that misdirects the operator is worse than no remedy.","section_id":"2.1","severity":"nit"},{"category":"weak-testability","check_key":"lazy-import-defeats-retargeted-patch-sites","description":"A name imported inside a function body never becomes a module attribute, so monkeypatch.setattr on gobby.cli.daemon_preflight.worktree_daemon_refusal would raise AttributeError. daemon.py imports worktree_daemon_refusal at module level; only require_cli_database, binary_set_apply_refusal and schema_apply_refusal are lazy.","finding_id":"PR1-005","fix":"State that daemon_preflight.py imports worktree_daemon_refusal at module level and reserves lazy imports for the gobby.cli.runtime and gobby.storage.schema_divergence helpers.","location":".gobby/plans/daemon-restart-preflight.md section 2.1, the lazy-imports sentence versus the three retargeted patch sites","prevention":"When a plan moves a patched symbol to a new module, record whether each moved import is module-level or function-local.","repairs":[{"items":[{"artifact":"test: `tests/cli/test_daemon_preflight.py::test_worktree_refusal_is_patchable_at_module_level`","prose":"`daemon_preflight.py` binds `worktree_daemon_refusal` as a module-level attribute so the retargeted patch sites resolve"}],"kind":"add_acceptance","section_id":"2.1"}],"root_cause":"The plan generalised 'daemon.py uses lazy imports' to every import in the new module.","section_id":"2.1","severity":"nit"}],"lane_execution_note":"No provider-native internal subagent tool was exposed in this session, so all three lanes ran as sequential parent work under the capacity-failure clause; repository_blast_radius spot-checked the clean deterministic sweep directly against exact source symbols and consumers with gcode.","max_review_rounds":1,"plan_hash":"96747e5441675bf72db058496c44331a8fd12869b9f8e661c2fc713c82a5b07f","round_number":1,"verdict":"needs_review"},"session_id":"47981968-3c34-4e7b-aaee-8c5fb187c963"}
```
- Round 1 repairs applied: typed acceptance repairs 1.1.7, 2.1.7, 2.1.8 via apply_plan_review_repairs; coordinator prose edits for PR1-001 (plan_schema mirrors apply_schema connection handling, never verify_database_identity), PR1-002 (database bound before the branch, candidate mode resolves the bootstrap URL, new 2.1.9), PR1-003 (_candidate_identity_refusal fails closed, 2.1.5 extended), PR1-004 (action="schema plan", real remedy), PR1-005 (module-level worktree_daemon_refusal import); Granularity decisions recorded for 1.1 and 2.1.
