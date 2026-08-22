Plan artifact: `.gobby/plans/pre-push-cleanup.md`

# Pre-push Cleanup

## Overview
`kind: framing`

Make the five failed gates from `reports/pre-push-1787152669.json` pass:
frontend Prettier, `gobby-code` tests under `--no-default-features`, Bandit B311
on hub pool jitter, pip-audit for `cryptography`/`h2`, and pytest collection
(nested `pytest_plugins` plus duplicate `test_*.py` basenames). Pytest aborted
during collection (exit 2) before any of the 29,808 selected tests ran.

## Constraints
`kind: framing`

Confirmed Decision Record:

- Full Plan-Coverage epic. Canonical artifact is `.gobby/plans/pre-push-cleanup.md`.
- Scope is those five failed gates only. CodeRabbit passed and is non-gating.
  Unknown pytest *assertion* or `--cov-fail-under=80` failures that appear after
  collection starts are out of this epic.
- Pytest collection: replace nested-conftest `pytest_plugins` with fixture
  imports. Mass-rename colliding `test_*.py` basenames. Do **not** set
  `importmode = importlib`.
- Cargo: keep pre-push/CI `--no-default-features`. Cfg-gate AI-only tests and
  silence the two no-ai `dead_code` warnings in the cargo report.
- Bandit: `# nosec B311` on pool jitter, matching existing backoff call sites.
  Do not switch jitter to `secrets`.
- pip-audit: raise `cryptography>=50.0.0`; constrain transitive `h2>=4.4.1`.
  Keep the existing `--ignore-vuln` list. Do not switch pre-push to `uv audit`.
- Prettier: format only the two warned web files.
- 0.5.0 has not shipped; no compatibility shims.
- Do not run the full pytest suite as a leaf gate. Collection-only is the
  pytest acceptance. `./pre-push-test.sh` remains the human/epic verification
  surface for the five gates.

Named defaults:

- Rename with `git mv`. Re-scan `tests/**/test_*.py` at implementation time;
  the 2026-08-19 listing is a lower bound (pytest 9 aborted after two
  collection errors). At least 65 colliding basenames (~160 files) exist.
- A repo-level uniqueness test in `tests/meta/test_import_hygiene.py` prevents
  the collision from returning.
- Load the `rust` skill before editing `crates/gcode`.
- `uv lock` after pyproject constraint edits.

Non-goals:

- Enabling default Cargo features in CI or pre-push.
- Unifying pre-push `pip-audit` with CI `uv audit`.
- Formatting the rest of `web/`.
- Fixing Bandit comment-parser warnings (`Test in comment: ...`).
- Dead-code cleanups outside the two cargo-report warnings.

## P1: Pytest collection
`kind: framing`

**Goal**: `pytest --collect-only` with the pre-push selection args exits 0.

### 1.1 Replace nested pytest_plugins with fixture imports [category: test]
`kind: deliverable`

Targets:
- `tests/agents/conftest.py`

Pytest 9.0.3 rejects `pytest_plugins` in non-top-level conftest files. The
pre-push failure is:

```text
ERROR collecting tests/agents
Defining 'pytest_plugins' in a non-top-level conftest is no longer supported
  tests/agents/conftest.py
```

`tests/agents/conftest.py` currently contains only:

```python
"""Share isolated definition-schema fixtures with agent sync tests."""

pytest_plugins = ["tests.storage.definitions.conftest"]
```

Do **not** hoist that plugin into `tests/conftest.py`. The definitions conftest
defines autouse `_reset_revision_globals`, which would then run for every test
in the suite. Keep it scoped to `tests/agents/` (directory conftest still
covers `tests/storage/definitions/`).

Replace `pytest_plugins` with fixture imports so pytest registers the same
fixtures without the nested-plugin hook:

```python
"""Share isolated definition-schema fixtures with agent sync tests."""

from tests.storage.definitions.conftest import (
    _reset_revision_globals,
    definition_db,
    scoped_postgres_dsn,
)

__all__ = [
    "_reset_revision_globals",
    "definition_db",
    "scoped_postgres_dsn",
]
```

Leave `pytest_plugins` in **test modules** unchanged. Pytest 9 only forbids it
in nested conftest files. These stay:

- `tests/mcp_proxy/tools/test_agent_definitions.py`
- `tests/workflows/test_agent_resolver.py`
- `tests/storage/test_managed_credentials.py`

`tests/conftest.py` already declares top-level
`pytest_plugins = ["tests.fixtures.postgres", "tests.review_coverage_helpers"]`.
Do not add the definitions plugin there.

**Acceptance:**

- 1.1.1 - `tests/agents/conftest.py` has no `pytest_plugins` assignment and
  imports `definition_db`, `scoped_postgres_dsn`, and `_reset_revision_globals`
  from `tests.storage.definitions.conftest`. file: `tests/agents/conftest.py`.
- 1.1.2 - Collecting `tests/agents` no longer raises the nested
  `pytest_plugins` error. test: `tests/agents/conftest.py`.

### 1.2 Unique test module basenames [category: test]
`kind: deliverable`

Targets:
- `tests/meta/test_import_hygiene.py::_iter_python_files`
- `tests/meta/test_import_hygiene.py::test_test_module_basenames_are_unique`
- `tests/project_verification/test_refresh.py::*` — scope-reason: rename the entire colliding test module
- `tests/providers/capabilities/test_refresh.py::*` — scope-reason: rename the entire colliding test module

Pytest's default import mode keys modules by basename. The pre-push run then
failed:

```text
import file mismatch:
  tests/project_verification/test_refresh.py
  tests/providers/capabilities/test_refresh.py
```

A live scan of `tests/**/test_*.py` found at least 65 colliding basenames
(examples: `test_refresh.py` (2), `test_models.py` (5+), `test_pipelines.py`
(5), `test_communications.py` (5), `test_base.py` (5), `test_manager.py` (6)).
Pytest 9 aborted after two collection errors, so the implementing leaf must
**re-scan the tree** and rename every remaining collision, not only
`test_refresh.py`.

Do not set `importmode = importlib`. Do not mass-keep generic names.

**Naming algorithm** (deterministic, apply with `git mv`):

```python
from pathlib import Path

def new_basename(path: Path, tests_root: Path) -> str:
    rel = path.relative_to(tests_root)
    suffix = path.stem.removeprefix("test_")
    parents = list(rel.parent.parts)
    if parents and parents[-1] == suffix:
        parents = parents[:-1]
    prefix = "_".join(parents)
    return f"test_{prefix}_{suffix}.py" if prefix else f"{path.name}"
```

Worked examples:

- `tests/project_verification/test_refresh.py` →
  `test_project_verification_refresh.py`
- `tests/providers/capabilities/test_refresh.py` →
  `test_providers_capabilities_refresh.py`
- `tests/cli/test_cli.py` → keep `test_cli.py` (parent equals stem)
- `tests/communications/test_cli.py` → `test_communications_cli.py`
- `tests/cli/hub_backup/test_cli.py` → `test_cli_hub_backup_cli.py`
- `tests/agents/watchdog/test_models.py` → `test_agents_watchdog_models.py`

Procedure:

1. Add `test_test_module_basenames_are_unique` to
   `tests/meta/test_import_hygiene.py` first. It must fail on the current tree.
2. Collect every `test_*.py` under `tests/` (skip `__pycache__`). Group by
   `path.name`. For each group with `len > 1`, compute `new_basename` for every
   file. If a computed name already exists as a file *outside* the group, append
   the next unused parent segment (or `_2` as last resort) until unique.
3. `git mv` each source to the new basename in the same directory. Do not move
   files across packages.
4. Update package-qualified imports of renamed modules
   (`from tests.<pkg>.test_<old> import ...`). Known current cross-test import
   `from tests.agents.test_capture import FakeCaptureStorage` is **not** in a
   colliding group; leave it unless the scan says otherwise.
5. Delete stale `**/__pycache__/test_*.pyc` for renamed modules.
6. Re-run the uniqueness test and collection.

Uniqueness test shape (reuse `_iter_python_files` / `TESTS_DIR` /
`REPO_ROOT` already in `tests/meta/test_import_hygiene.py`):

```python
def test_test_module_basenames_are_unique() -> None:
    files = [
        path
        for path in _iter_python_files(TESTS_DIR)
        if path.name.startswith("test_")
    ]
    by_name: dict[str, list[str]] = {}
    for path in files:
        by_name.setdefault(path.name, []).append(
            str(path.relative_to(REPO_ROOT))
        )
    collisions = {
        name: paths for name, paths in sorted(by_name.items()) if len(paths) > 1
    }
    assert collisions == {}, f"duplicate test module basenames: {collisions}"
```

Collection command (same ignores/deselects as `pre-push-test.sh`, no coverage):

```bash
GOBBY_TEST_PROTECT=1 uv run pytest --collect-only \
  --ignore=tests/integration/sandbox \
  --ignore=tests/packaging/test_installed_wheel_ui_smoke.py \
  --deselect=tests/agents/test_spawn_executor_droid.py::test_droid_worktree_spawn_fires_pre_tool_use_against_gobby_daemon \
  --deselect=tests/e2e/test_build_dispatcher_autonomy.py::test_real_small_gobby_build_canary \
  --deselect=tests/sessions/test_e2e_session_tracking.py::test_full_lifecycle
```

That command must exit 0. Do not add `--cov-fail-under=80` here. Do not run
the 30+ minute suite.

**Acceptance:**

- 1.2.1 - `tests/meta/test_import_hygiene.py` fails the uniqueness test on a
  tree that still has duplicate `test_*.py` basenames and passes after the
  rename sweep. test: `tests/meta/test_import_hygiene.py::test_test_module_basenames_are_unique`.
- 1.2.2 - The two modules named `test_refresh.py` no longer share a basename.
  file: `tests/project_verification/test_refresh.py`.
- 1.2.3 - `GOBBY_TEST_PROTECT=1 uv run pytest --collect-only` with the
  pre-push ignore/deselect set exits 0 with no `pytest_plugins` and no import
  mismatch errors. behavior: "pytest collection succeeds with unique test module basenames" in `pre-push-test.sh`.

## P2: Cargo --no-default-features
`kind: framing`

**Goal**: `gobby-code` lib tests compile and run with the same
`--no-default-features` set as `pre-push-test.sh` and `.github/workflows/rust-ci.yml`.

### 2.1 Cfg-gate AI-only gcode tests [category: code]
`kind: deliverable`

Targets:
- `crates/gcode/src/config/layers.rs::*` — scope-reason: cfg-gate AI-only test imports and tests so the inline test module compiles without the ai feature
- `crates/gcode/src/vector/code_symbols/embedding.rs::*` — scope-reason: cfg-gate AI-only test imports and the no-ai dead_code const
- `crates/gcode/src/vector/code_symbols/types.rs::VectorLifecycleError`

Load the `rust` skill before editing. Do not enable the `ai` feature in
pre-push or rust-ci. `gobby-code` default features already include `ai`;
`--no-default-features` is the canonical CI set.

Compile errors from the cargo report:

1. `crates/gcode/src/config/layers.rs` test module imports
   `layers_from_daemon_result`, which is `#[cfg(feature = "ai")]`.
2. `crates/gcode/src/vector/code_symbols/embedding.rs` test module imports
   `embedding_source_from_resolved_ai_context`, also `#[cfg(feature = "ai")]`.

Do **not** wrap the entire `layers.rs` `mod tests` in `feature = "ai"`. Tests
such as `embedding_details_attribute_served_values_to_daemon` and the
`ServiceSource` cases must keep running without `ai`.

Required edits:

- Split the `layers.rs` test `use super::{...}` so `layers_from_daemon_result`
  is `#[cfg(feature = "ai")]`. Gate
  `read_config_layers_warns_for_unregistered_served_keys` the same way; it
  calls `gobby_core::ai::effective_config::daemon_mode_layers_at`.
- In `embedding.rs` tests, cfg-gate the import of
  `embedding_source_from_resolved_ai_context` and any test that needs
  `AiContext` / that helper (`daemon_source_is_selected_for_daemon_route`).
  Gate `embed_via_daemon_or_err_uses_document_mode_for_indexing` with `ai` as
  well if `INDEXING_EMBED_QUERY_MODE` is only meaningful under `ai`.
- Keep tests that only call `resolve_embedding_config_from_source` if they
  still compile without `ai`. Confirm with
  `cargo test -p gobby-code --no-default-features --no-run`.

Dead_code warnings in the same report (`gobby-code` lib, not test):

- `INDEXING_EMBED_QUERY_MODE` is only read inside `#[cfg(feature = "ai")]`.
- `VectorLifecycleError::EmbeddingHttp` is only constructed from
  `embedding_error`, which is `#[cfg(feature = "ai")]`.

Silence them without cfg-stripping the variant (that would force
`vector_error_kind` in `crates/gcode/src/projection/sync.rs` and the
`Display` match to grow cfg arms):

```rust
#[cfg_attr(not(feature = "ai"), allow(dead_code))]
const INDEXING_EMBED_QUERY_MODE: bool = false;
```

```rust
#[cfg_attr(not(feature = "ai"), allow(dead_code))]
EmbeddingHttp {
    status: u16,
    body: String,
},
```

Do not add new public API. Do not change default-feature behavior.

Validate:

```bash
cargo test -p gobby-code --no-default-features --no-run
cargo nextest run --profile ci -p gobby-code --no-default-features
cargo clippy -p gobby-code --all-targets --no-default-features -- -D warnings
```

**Acceptance:**

- 2.1.1 - `layers.rs` tests compile without the `ai` feature; AI-only tests
  remain compiled when `ai` is on. file: `crates/gcode/src/config/layers.rs`.
- 2.1.2 - `embedding.rs` tests compile without the `ai` feature. file:
  `crates/gcode/src/vector/code_symbols/embedding.rs`.
- 2.1.3 - `cargo nextest run --profile ci --workspace --no-default-features`
  no longer fails with E0432 on `layers_from_daemon_result` or
  `embedding_source_from_resolved_ai_context`. behavior: "workspace tests compile with --no-default-features" in `pre-push-test.sh`.
- 2.1.4 - `cargo clippy -p gobby-code --all-targets --no-default-features -- -D warnings`
  does not fail on `INDEXING_EMBED_QUERY_MODE` or `EmbeddingHttp`. symbol:
  `VectorLifecycleError`.

## P3: Security scanners
`kind: framing`

**Goal**: Bandit and pip-audit match the existing house style and the two
published fixes.

### 3.1 Annotate hub pool jitter as non-crypto [category: code]
`kind: deliverable`

Targets:
- `src/gobby/storage/hub/postgres_pool.py::_acquire_with_backoff`

Bandit B311 fires at `postgres_pool.py:149`:

```python
time.sleep(backoff * (1 + random.uniform(0, POOL_TIMEOUT_RETRY_JITTER_RATIO)))
```

This is acquisition-timeout jitter, not a cryptographic nonce. The same
pattern is already suppressed at:

- `src/gobby/communications/adapters/discord.py` (`# nosec B311 # jitter, not crypto`)
- `src/gobby/integrations/linear_graphql.py`
- `src/gobby/llm/claude_runtime.py`
- `src/gobby/mcp_proxy/importer.py`

Add an inline `# nosec B311` on that `random.uniform` call, with the jitter
rationale. Do not switch to `secrets.SystemRandom`. Do not add B311 to
`[tool.bandit] skips` (that would hide real PRNG misuse).

Validate:

```bash
uv run bandit -c pyproject.toml -r src/ -q
```

Must report zero issues (the pre-push failure was this single Low/High B311).

**Acceptance:**

- 3.1.1 - `_acquire_with_backoff` keeps `random.uniform` jitter and carries
  `# nosec B311` on that line. symbol: `_acquire_with_backoff`.
- 3.1.2 - `uv run bandit -c pyproject.toml -r src/ -q` exits 0. behavior:
  "bandit reports no B311 on hub pool jitter" in `pre-push-test.sh`.

### 3.2 Bump cryptography and constrain h2 [category: config]
`kind: deliverable`

Targets:
- `pyproject.toml`
- `uv.lock`

pip-audit (pre-push ignore list already applied) reported:

| Package | Locked | Advisory | Fix |
| --- | --- | --- | --- |
| cryptography | 49.0.0 | PYSEC-2026-3552 (PKCS#7 EnvelopedData Bleichenbacher oracle, introduced 44.0.0) | 50.0.0 |
| h2 | 4.3.0 | PYSEC-2026-3628 / CVE-2026-71554 (duplicate Host header / request smuggling) | 4.4.1 |

`cryptography` is a direct dependency at `>=48.0.1`. Raise the floor:

```toml
"cryptography>=50.0.0",
```

Update the adjacent comment to name PYSEC-2026-3552 / cryptography 50.0.0.

`h2` is transitive (HTTP/2 stack). Add a constraint, do not add a direct
runtime dependency:

```toml
# [tool.uv] constraint-dependencies
"h2>=4.4.1",  # PYSEC-2026-3628 duplicate Host header
```

Then `uv lock`. Do not add PYSEC-2026-3552 or PYSEC-2026-3628 to
`PIP_AUDIT_IGNORE_ARGS` in `pre-push-test.sh` / `pre-push-test-short.sh`.
Leave the existing ignores (`CVE-2025-69872`, `CVE-2026-4539`,
`CVE-2025-3000`) unchanged.

Validate:

```bash
uv run pip-audit --ignore-vuln CVE-2025-69872 --ignore-vuln CVE-2026-4539 --ignore-vuln CVE-2025-3000
```

The `gobby` "Dependency not found on PyPI" skip is informational and already
present; it is not a failure.

**Acceptance:**

- 3.2.1 - Direct cryptography floor is `>=50.0.0`. file: `pyproject.toml`.
- 3.2.2 - `h2>=4.4.1` is in `[tool.uv] constraint-dependencies` and `uv.lock`
  resolves `h2` to 4.4.1 or newer. file: `uv.lock`.
- 3.2.3 - `uv run pip-audit` with the existing three `--ignore-vuln` flags
  exits 0 and does not report PYSEC-2026-3552 or PYSEC-2026-3628. behavior:
  "pip-audit passes for cryptography 50+ and h2 4.4.1+" in `pre-push-test.sh`.

## P4: Frontend format
`kind: framing`

**Goal**: `web` `format:check` matches the two files Prettier already knows how
to fix.

### 4.1 Format the two Prettier-warned files [category: refactor]
`kind: deliverable`

Targets:
- `web/src/components/agents/AgentProviderSettings.tsx::*` — scope-reason: Prettier-only rewrite of the whole module
- `web/src/components/settings/sections/configAccessors.ts::*` — scope-reason: Prettier-only rewrite of the whole module

`cd web && npm run format:check` reported:

```text
[warn] src/components/agents/AgentProviderSettings.tsx
[warn] src/components/settings/sections/configAccessors.ts
```

Run Prettier write on those two paths only. No logic, import, or Tailwind
class-meaning changes. `AgentProviderSettings` is ~460 lines; formatting must
not push any production file to 1,000 lines (the current files are well under).

```bash
cd web && npx prettier --write src/components/agents/AgentProviderSettings.tsx \
  src/components/settings/sections/configAccessors.ts
cd web && npm run format:check
```

**Acceptance:**

- 4.1.1 - `AgentProviderSettings.tsx` is Prettier-clean with no semantic
  edits. file: `web/src/components/agents/AgentProviderSettings.tsx`.
- 4.1.2 - `configAccessors.ts` is Prettier-clean with no semantic edits.
  file: `web/src/components/settings/sections/configAccessors.ts`.
- 4.1.3 - `cd web && npm run format:check` exits 0. behavior: "frontend format check passes" in `pre-push-test.sh`.

## V2 Pre-push gates
`kind: verification`

After all deliverables, these five pre-push commands must pass (same flags as
`pre-push-test.sh`). Do not require a green full pytest run beyond collection:

```bash
cd web && npm run format:check
cargo nextest run --profile ci --workspace --no-default-features
cargo test --doc --workspace --no-default-features
uv run bandit -c pyproject.toml -r src/ -q
uv run pip-audit --ignore-vuln CVE-2025-69872 --ignore-vuln CVE-2026-4539 --ignore-vuln CVE-2025-3000
GOBBY_TEST_PROTECT=1 uv run pytest --collect-only \
  --ignore=tests/integration/sandbox \
  --ignore=tests/packaging/test_installed_wheel_ui_smoke.py \
  --deselect=tests/agents/test_spawn_executor_droid.py::test_droid_worktree_spawn_fires_pre_tool_use_against_gobby_daemon \
  --deselect=tests/e2e/test_build_dispatcher_autonomy.py::test_real_small_gobby_build_canary \
  --deselect=tests/sessions/test_e2e_session_tracking.py::test_full_lifecycle
```

Optional human check: `./pre-push-test.sh`. If pytest then fails assertions or
coverage after collection, that is outside this epic and is reported, not
silently expanded.

## V1 Plan Changelog
`kind: verification`

**Draft 1** `kind: verification`

- Decision Record confirmed: five failed gates; mass-rename colliding test
  modules rather than `importmode=importlib`; nested `pytest_plugins` replaced
  with fixture imports; cargo stays `--no-default-features`; Bandit nosec on
  jitter; cryptography 50 + h2 4.4.1; Prettier on two web files; unknown
  pytest assertion failures out of scope.
