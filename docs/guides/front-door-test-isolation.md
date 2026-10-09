# Front-door test isolation

The shared-token cutover (#23519) exercised a Rust front door and Python runner
across startup, credential rotation, standby takeover, and shutdown. These lessons
keep that validation isolated without hiding the safety checks that protect the
operator's running daemon.

## Observe writes through shutdown

Use `tests/e2e/conftest.py::external_write_audit_environment` for E2E-owned Python
processes. Its session-scoped observer stays active through fixture teardown and
reports attempted mutations under the operator's `.gobby` directory, including
failed writes. A passing test body alone does not prove isolated shutdown.

The audit log must live outside the monitored directory. A checkout or scratch
archive under `~/.gobby/worktrees/` still lies inside that directory, so placing the
log there makes `observe_writes` reject the setup. Use pytest's temporary directory
for the log. Keep failures visible; do not narrow the monitored root to make them
disappear.

Imports can write `__pycache__` beside source files even when the daemon's state
home is temporary. The audit fixture sets both `sys.dont_write_bytecode = True`
for the current interpreter and `PYTHONDONTWRITEBYTECODE=1` for child interpreters.
Its bounded monkeypatch restores both afterward. An environment variable alone
does not change an already-running interpreter's bytecode setting. The parent and
child regression is
`tests/test_e2e_external_write_guard.py::test_audit_environment_prevents_import_bytecode_writes`.

## Seed identity before starting either runner

Daemon subprocesses cannot see a pytest process's patched machine-ID cache.
`e2e_config` writes a synthetic `machine_id` into the isolated `GOBBY_HOME`;
the scoped PostgreSQL identity rows must agree with it. Give each fixture its own
state home, bootstrap, logs, and ports before starting the runner.

For same-deployment takeover, `_spawn_same_deployment_standby` in
`tests/e2e/test_runtime_boundary.py` copies the boundary fixture's synthetic
`machine_id` into the standby bootstrap directory. It also supplies the boundary's
isolated `HOME` and `GOBBY_HOME` to the child. A standby bootstrap path alone does
not seed machine identity or isolate every home-based lookup. Reuse the fixture's
identity; do not obtain one from the operator's real home or change production
identity handling to accommodate a test.

## Preserve the live-hub guard

Some test setup intentionally inspects the configured operator bootstrap before
function-scoped home isolation. In `tests/fixtures/postgres.py`,
`pytest_configure` calls `_live_hub_identity` and installs a guard that refuses
connections to the configured live hub. Read-only fixture inspection, including
file metadata checks, is distinct from a mutation caught by the write audit.

Moving `GOBBY_HOME` to an empty directory before this guard resolves the configured
hub can make `_live_hub_identity` return `None`. That hides the configured
`database_url` and leaves only the default live-hub identity protected. The cutover
withdrew that isolation approach. Preserve guard discovery, then isolate the daemon
subprocesses with the existing fixtures. This does not authorize agents to inspect
the operator's bootstrap or credentials themselves.

For Python validation, supply the isolated test hub explicitly and retain the
daemon-protection flag:

```bash
DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest <focused-path>
```

See [Testing](testing.md) for the broader fixture and validation workflow. These
isolation lessons preserve authentication and live-hub protections; they grant no
validation exemptions.
