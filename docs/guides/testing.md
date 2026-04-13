# Testing Overview

This guide provides a high-level overview of the testing infrastructure in Gobby.

## Running Tests

Gobby uses `pytest` for Python backend tests and `vitest`/`playwright` for frontend tests.

### Backend Tests

To run the full suite:
```bash
uv run pytest
```

To run a specific test file:
```bash
uv run pytest tests/path/to/test_file.py
```

### Frontend Tests

Located in the `web/` directory:
```bash
cd web
npm run test      # Unit tests (Vitest)
npm run test:e2e  # E2E tests (Playwright)
```

## Test Markers

- `unit`: Fast, isolated tests.
- `integration`: Tests interaction between components.
- `e2e`: Full system flows.
- `slow`: Long-running tests.
