# Testing

Gobby's test workflow is optimized for targeted verification. The repository has
thousands of tests, so contributors and agents should run the narrowest backend
or frontend command that proves the change.

## Backend Pytest

Backend tests live under `tests/` and are configured in `pyproject.toml`.
Pytest is run through `uv` so it uses the project environment:

```bash
uv run pytest tests/tasks/test_validation.py -v
```

Agents must also enable Gobby's test protection switch on every pytest run:

```bash
GOBBY_TEST_PROTECT=1 uv run pytest tests/tasks/test_validation.py -v
```

Run a package or marker slice when the affected surface spans more than one
file:

```bash
uv run pytest tests/tasks/ -v
uv run pytest tests/workflows/ -m "not slow" -v
uv run pytest tests/servers/ --cov=gobby --cov-report=term-missing --cov-fail-under=80
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

Coverage is not enabled by default in local pytest runs. CI and pre-push gates
enforce the 80% project threshold; local coverage runs should add:

```bash
uv run pytest tests/path/ --cov=gobby --cov-report=term-missing --cov-fail-under=80
```

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
- `protect_production_resources` is applied automatically. It sets safe
  `GOBBY_*` paths and patches config loading/saving so tests do not touch the
  user's real daemon database, home directory, logs, or hooks.
- `GOBBY_TEST_PROTECT=1` is the explicit subprocess safety switch used by
  daemon and CLI tests to keep process-discovery helpers away from the user's
  running daemon.

Domain test packages may add narrower fixtures in their own `conftest.py`
files. For example, `tests/tasks/conftest.py` provides a validation prompt
loader mock, and `tests/servers/conftest.py` provides HTTP server and
`TestClient` fixtures.

E2E daemon tests under `tests/e2e/` spawn isolated daemon processes. Use their
fixtures rather than connecting to a real local daemon; the e2e environment sets
temporary `HOME`, database, config, log paths, and free high-numbered ports.

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
under `web/tests/`. Run a specific Playwright file instead of the whole browser
suite:

```bash
cd web
npx playwright test tests/provider-picker.spec.ts
```

## Picking Validation

Choose validation based on the changed surface:

| Change | Recommended command |
|--------|---------------------|
| One backend module | `uv run pytest tests/<matching-file>.py -v` |
| Task, workflow, or server behavior | `uv run pytest tests/<domain>/ -v` |
| Coverage-sensitive backend work | `uv run pytest tests/<domain>/ --cov=gobby --cov-report=term-missing --cov-fail-under=80` |
| CLI behavior | `uv run pytest tests/cli/ -m cli -v` |
| Frontend component | `cd web && npm run test -- src/__tests__/<file>.test.tsx` |
| Frontend type or lint change | `cd web && npm run type-check` or `cd web && npm run lint` |
| Browser flow | `cd web && npx playwright test tests/<file>.spec.ts` |

For agent-run backend validation, prefix the pytest examples above with
`GOBBY_TEST_PROTECT=1`.

When a test fails, keep the rerun focused on the failing file or marker until
the failure is understood. Broaden only when the change touches shared behavior.

_Last verified: 2026-05-07_
