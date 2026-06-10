# Configuration - Reference

## Inspect Existing Ownership First

Start from the repo's configured files:

- `pyproject.toml` for build backend, project metadata, dependencies, Python version, Ruff, mypy, pytest, coverage, and tooling tables
- `setup.cfg` or `setup.py` for older packages
- `tox.ini`, `noxfile.py`, `pytest.ini`, `mypy.ini`, and `ruff.toml` when tool config is split
- lockfiles and environment files such as `uv.lock`, `requirements*.txt`, `Pipfile`, or `.python-version`

Do not paste a generic config over a framework or library template. Preserve generated files, package-manager lockfiles, local wrapper commands, and CI-owned settings.

## Package Metadata

For new or actively maintained packages, prefer PEP 621 metadata in `pyproject.toml`:

```toml
[project]
name = "example"
requires-python = ">=3.13"
dependencies = [
  "httpx>=0.28",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
```

Keep dependency ranges, extras, entry points, package data, and build backend choices aligned with the repo. Do not change package layout or distribution semantics as a side effect of lint cleanup.

## Tooling Tables

Keep tool config explicit and colocated when the repo uses `pyproject.toml`:

```toml
[tool.ruff]
line-length = 100
target-version = "py313"

[tool.ruff.lint]
select = ["E", "F", "I", "B", "UP"]

[tool.mypy]
python_version = "3.13"
strict = true

[tool.pytest.ini_options]
testpaths = ["tests"]
```

Use the repo's configured tools and target versions. If strict mypy or Ruff rules are already enabled, fix code instead of weakening the setting.

## Python Version

Match syntax and stdlib features to the declared runtime:

| Declared runtime | Typical choices |
| --- | --- |
| `>=3.13` | modern generics, `pathlib`, `tomllib`, `TaskGroup`, precise type hints |
| `>=3.11` | `ExceptionGroup`, `TaskGroup`, `typing.Self`, `tomllib` |
| older support | backports, conservative syntax, compatibility tests |

Do not introduce syntax that excludes supported consumers. In libraries, check classifiers, CI matrix, package metadata, and docs before using newer syntax.

## Layout And Imports

Prefer the repo's existing layout. Common modern package shape:

```text
src/
  package_name/
tests/
pyproject.toml
```

Keep public package exports intentional. Avoid importing private module paths across package boundaries. When editing scripts, make the runtime entry point explicit through console scripts, module execution, or existing wrappers.

## Dependency Management

Use the existing package manager:

- `uv` projects: update `pyproject.toml` and `uv.lock` through repo-approved commands
- pip-tools projects: update `.in` and generated `.txt` files together
- Poetry/Pipenv projects: preserve their lockfile workflow
- constraints files: keep runtime, dev, and CI constraints separated

Do not hand-edit generated lockfiles unless the repo documents that workflow.

## Type Checker Configuration

Strict type checking should be scoped deliberately:

```toml
[tool.mypy]
strict = true
warn_unused_ignores = true
warn_return_any = true

[[tool.mypy.overrides]]
module = ["vendor_without_stubs.*"]
ignore_missing_imports = true
```

Use overrides for real external stub gaps. Do not hide internal modules behind blanket `ignore_errors = true`.

## Test And Coverage Configuration

Make test selection and coverage match the repo:

```toml
[tool.pytest.ini_options]
markers = [
  "unit: fast isolated tests",
  "integration: tests requiring local services",
]

[tool.coverage.run]
source = ["src"]
branch = true
```

Keep slow, integration, network, daemon, and database tests isolated through markers or fixtures. Do not make global pytest config depend on local user state.

## Environment And Secrets

Validate environment variables at process boundaries and keep `.env` files out of committed secrets. Use typed config loaders or schema validators when the application has more than a handful of environment keys.
