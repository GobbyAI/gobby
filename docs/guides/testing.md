# Testing

Gobby's test workflow is optimized for targeted verification. The repository has
thousands of tests, so contributors and agents should run the narrowest backend
or frontend command that proves the change. Backend pytest runs started by
agents must be isolated from the user's running daemon and local daemon state.

## Backend Pytest

Backend tests live under `tests/` and are configured in `pyproject.toml`.
Pytest is run through `uv` so it uses the project environment:

```bash
uv run pytest tests/tasks/test_validation.py -v
```

Agents must enable Gobby's test protection switch on every pytest run:

```bash
GOBBY_TEST_PROTECT=1 uv run pytest tests/tasks/test_validation.py -v
```

Run a package or marker slice when the affected surface spans more than one
file. Agents keep the same `GOBBY_TEST_PROTECT=1` prefix:

```bash
GOBBY_TEST_PROTECT=1 uv run pytest tests/tasks/ -v
GOBBY_TEST_PROTECT=1 uv run pytest tests/workflows/ -m "not slow" -v
GOBBY_TEST_PROTECT=1 uv run pytest tests/servers/ --cov=gobby --cov-report=term-missing --cov-fail-under=80
```

Avoid the repository-wide pytest command during normal agent work. The full
suite is intentionally reserved for explicit human requests and broader gates
because it is large and slow.

## Pytest Configuration

The canonical pytest settings are in `pyproject.toml`:

- `asyncio_mode = "auto"` for async tests.
- `testpaths = ["tests"]` limits default collection to the backend test tree.
- `pythonpath = ["src"]` makes `gobby` importable from source.
- `python_files = ["test_*.py", "run_*_sandbox.py"]`.
- `python_classes = ["Test*"]`.
- `python_functions = ["test_*"]`.
- `addopts` enables verbose output and disables `faulthandler`.

Coverage is not enabled by default in local pytest runs. CI's backend test job
enforces the 80% project threshold with `--cov=gobby`; local coverage runs
should add:

```bash
GOBBY_TEST_PROTECT=1 uv run pytest tests/path/ --cov=gobby --cov-report=term-missing --cov-fail-under=80
```

The main Python CI job excludes `tests/voice` and
`tests/servers/routes/test_voice_routes.py`. Those run in the dedicated
`voice-extra` job, which installs `uv sync --dev --extra voice` before running
the focused voice suite.

The project pre-push verification in `.gobby/project.json` runs backend lint,
format, and type checks plus frontend lint, type checks, and Vitest. It does not
run backend pytest by default, so run focused backend pytest yourself when a
change touches Python behavior.

## Markers

Use markers to describe test scope and to keep targeted runs readable:

| Marker | Use |
|--------|-----|
| `unit` | Fast, isolated tests. |
| `integration` | Tests that exercise multiple components together. |
| `e2e` | End-to-end flows; `tests/conftest.py` moves these to the end of collection. |
| `slow` | Long-running tests; deselect locally with `-m "not slow"`. |
| `cli` | Click command and CLI behavior tests. |
| `skill_tdd` | Skill pressure-scenario tests. |
| `no_config_protection` | Opt out of the production-resource protection fixture for exceptional cases. |

Sandbox compatibility tests under `tests/integration/sandbox/` are skipped
unless the run explicitly passes `--run-sandbox`:

```bash
uv run pytest tests/integration/sandbox/ --run-sandbox -v
```

## Shared Fixtures

The root `tests/conftest.py` provides common isolation and test helpers:

- `temp_dir` gives each test a temporary working directory.
- `repo_root` points to the repository root.
- `safe_db_dir` and `safe_gobby_home_dir` isolate database and home-directory
  state.
- `temp_db`, `session_manager`, `project_manager`, and `mcp_manager` provide
  storage-backed managers on a migrated temporary database.
- `mock_config`, `mock_config_with_websocket`, `default_config`,
  `mock_daemon_config`, `mock_machine_id`, and `mock_llm_service` cover common
  daemon dependencies.
- `protect_production_resources` is applied automatically. It sets
  `GOBBY_TEST_PROTECT=1`, safe `GOBBY_*` paths, and patches config
  loading/saving so tests do not touch the user's real daemon database, home
  directory, logs, or hooks.
- `GOBBY_TEST_PROTECT=1` is the explicit subprocess safety switch used by
  daemon and CLI tests. It fences process-discovery helpers such as
  `get_daemon_pid()` and daemon stop paths so they ignore the user's running
  daemon.

Domain test packages may add narrower fixtures in their own `conftest.py`
files. For example, `tests/tasks/conftest.py` provides a validation prompt
loader mock, and `tests/servers/conftest.py` provides HTTP server and
`TestClient` fixtures.

E2E daemon tests under `tests/e2e/` spawn isolated daemon processes. Use their
fixtures rather than connecting to a real local daemon. The e2e environment sets
`GOBBY_TEST_PROTECT=1`, clears parent `GOBBY_DATABASE_PATH` and
`GOBBY_CONFIG_FILE` overrides before spawning the daemon, replaces `HOME` with a
temporary directory, clears provider API keys, and uses high-numbered ports in
the 30000-40000 range while excluding production ports `60887`, `60888`, and
`60889`.

## Frontend Tests

The web UI lives in `web/`. Use npm scripts from that directory:

```bash
cd web
npm run test -- src/__tests__/App.test.tsx
npm run type-check
npm run lint
```

`npm run test` maps to `vitest run`; `npm run test:watch` starts watch mode.
Playwright is configured at `web/playwright.config.ts`, and browser tests live
under `web/tests/`. Without `PLAYWRIGHT_BASE_URL`, Playwright starts
`npm run dev` and targets `http://localhost:60889`; with
`PLAYWRIGHT_BASE_URL`, it uses the supplied app URL. Run a specific Playwright
file instead of the whole browser suite:

```bash
cd web
npx playwright test tests/provider-picker.spec.ts
```

## Picking Validation

Choose validation based on the changed surface:

| Change | Recommended command |
|--------|---------------------|
| One backend module | `GOBBY_TEST_PROTECT=1 uv run pytest tests/<matching-file>.py -v` |
| Task, workflow, or server behavior | `GOBBY_TEST_PROTECT=1 uv run pytest tests/<domain>/ -v` |
| Coverage-sensitive backend work | `GOBBY_TEST_PROTECT=1 uv run pytest tests/<domain>/ --cov=gobby --cov-report=term-missing --cov-fail-under=80` |
| CLI behavior | `GOBBY_TEST_PROTECT=1 uv run pytest tests/cli/ -m cli -v` |
| Frontend component | `cd web && npm run test -- src/__tests__/<file>.test.tsx` |
| Frontend type or lint change | `cd web && npm run type-check` or `cd web && npm run lint` |
| Browser flow | `cd web && npx playwright test tests/<file>.spec.ts` |

For non-agent local backend validation, the `GOBBY_TEST_PROTECT=1` prefix is
still safe to keep. It makes daemon-discovery and stop helpers use the same
production-resource fence that agent runs require.

When a test fails, keep the rerun focused on the failing file or marker until
the failure is understood. Broaden only when the change touches shared behavior.

_Last verified: 2026-05-07_
