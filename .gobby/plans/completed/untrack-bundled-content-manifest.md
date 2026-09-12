Plan artifact: `.gobby/plans/untrack-bundled-content-manifest.md`
**Plan ID:** untrack-bundled-content-manifest

# Untrack The Bundled Content Manifest

## Overview
`kind: framing`

Bug #22175: two live sessions in one checkout both regenerate
`src/gobby/install/bundled_content_manifest.json` after editing bundled skills, the
file becomes dirty and attributed to both open tasks, and every commit, restore,
release, and reclaim path for it is blocked in both directions. The committed copy is
redundant: `verify_bundled_integrity` prefers Git and reads the manifest only in
packaged installs without Git, and `build_backend/__init__.py` regenerates the file
in both `build_wheel` and `build_sdist`. The fix is to stop tracking the file and
delete every mechanism that exists only to keep the committed copy in sync with
`src/gobby/install/shared/`. Build-time generation, the wheel membership check, the
`MANIFEST.in` include, the `pyproject.toml` package-data entry, and the packaged-install
integrity fallback are unchanged.

## Constraints
`kind: framing`

- Keep: `build_backend/__init__.py` (`_stage_bundled_content_manifest`,
  `_verify_wheel_contains_required_files`), `MANIFEST.in` line 2 (the sdist include is
  what carries the now-untracked generated file into the sdist that `build_sdist`
  staged), the `install/bundled_content_manifest.json` package-data entry in
  `pyproject.toml`, `src/gobby/sync/integrity.py::_verify_manifest_integrity`,
  `load_bundled_content_manifest`, `build_bundled_content_manifest`,
  `write_bundled_content_manifest`, `hash_file_bytes`, `iter_bundled_manifest_files`,
  `should_include_bundled_file`, `tests/sync/test_integrity.py`, and
  `tests/install/test_bundled_content_manifest.py::test_manifest_membership_matches_wheel`
  (it copies `git ls-files` into a fresh source root, so after untracking it proves
  the build backend generates the manifest with no committed copy).
- Delete only mechanism whose sole purpose is committed-copy parity. No new
  ownership-model behavior: `commit_guard`, `release_task_paths`, and
  `task_claim_state` are untouched. Options 1 to 3 in #22175 are superseded.
- Ordering: the close-time gate in `_evaluate_close` fails any linked commit that
  touches `src/gobby/install/shared/` while the committed manifest is absent or stale,
  and the pre-push check runs `python -m gobby.install.manifest` against HEAD. Both
  are gone before the file is untracked (2.1 depends on 1.2, which depends on 1.1) and
  before the skill text under `shared/` changes (2.2 depends on 2.1). The close gate
  runs inside the daemon, so the daemon serving the checkout must be restarted
  (announce through `gobby-agents:send_message`, wait for a quiet window) after 1.1
  merges and before 2.2 closes; otherwise the old gate rejects 2.2's commit.
- Two targeted files sit above the 850-line inspection threshold and are decomposed
  inside the leaf that touches them: `_lifecycle_close.py` (960 lines) loses its tool
  registration to a new `_lifecycle_close_tool.py`, and the impeccable upgrade
  `transform.py` (851 lines) loses its reference-transform group to a new
  `reference_transform.py` beside it.
- Consumer sweep evidence (run from the 0.5.0 checkout):
  `gcode grep -F "bundled_content_manifest" src/ tests/ build_backend/ scripts/ docs/ MANIFEST.in pyproject.toml`
  hits: MANIFEST.in:2; build_backend/__init__.py; docs/reviews/support-infra.md
  (historical review, untouched); pyproject.toml:166; src/gobby/hooks/event_handlers/_tool.py;
  src/gobby/install/manifest.py; src/gobby/install/shared/skills/repository-maintenance/SKILL.md;
  src/gobby/mcp_proxy/tools/tasks/_lifecycle_close.py; src/gobby/mcp_proxy/tools/tasks/_task_scope.py;
  src/gobby/sync/integrity.py; tests/agents/test_plan_adversary_internal_research_definition.py;
  tests/ci/test_pre_push_manifest.py; tests/cli/test_cli_sync_coverage.py (fake path,
  integrity fallback, untouched); tests/hooks/test_tool_handlers.py;
  tests/install/test_bundled_content_manifest.py; tests/mcp_proxy/tools/tasks/test_close_task_flow.py;
  tests/mcp_proxy/tools/tasks/test_task_scope.py; tests/skills/test_pipelines_and_cron_skill.py;
  tests/skills/test_repository_maintenance_skill.py; tests/skills/test_upgrade_transform.py;
  tests/sync/test_integrity.py; tests/test_build_backend.py.
  `gcode blast-radius check_committed_bundled_content_manifest`: manifest.py
  `check_linked_committed_bundled_manifest`, `main`; tests/install four tests.
  `gcode grep -w "_record_dirty_generated_artifacts|_GENERATED_ARTIFACT_SOURCES" src/ tests/`:
  only `_tool.py` lines 33, 258, 271, 297.
  `gcode grep -F "_BUNDLED_CONTENT_MANIFEST" src/gobby/mcp_proxy/tools/tasks/_task_scope.py`:
  lines 39, 106, 109; `_SHARED_INSTALL_ROOT` only at 38 and 107.
  `gcode grep -F "gobby.install.manifest"`: pre-push-test.sh:89, repository-maintenance
  SKILL.md:104, `_lifecycle_close.py` 12 and 452, integrity.py:19, tests listed above.
  `gcode grep -w register_close_task src/ tests/`: `_lifecycle.py` 15 and 49,
  `_lifecycle_close.py` 810 and 959, test_close_task_attributed_cleanliness.py 21 and
  152, test_close_task_flow.py 33, 876, 911, 985, 1084, 1149.
  Test patch sites on names that move with the registration:
  test_close_task_flow.py 879 to 881, 914 to 917, 988 to 989
  (`_evaluate_close`, `_commit_close`, `active_review_response`, `launch_close_review`);
  test_lifecycle_close_orchestration.py 713 (`close_module._evaluate_close`).
  The unindexed transform `src/gobby/install/shared/skills/impeccable/.upgrade/transform.py`
  imports `build_bundled_content_manifest` (line 21) and writes the manifest at lines
  734 to 735 inside `_prepare_upgrade`; its reference helpers (lines 342 to 496) and
  the constants they use (lines 28 to 69) have no other callers except
  `_prepare_upgrade` lines 676 to 677; tests reach the module only through
  `main`, `upgrade`, `UpgradeRejected`, and `StagedCandidate`.
- `.gobby/plans/gcode-import-communities.md` line 419 (another session's uncommitted
  draft) still says to regenerate the manifest; its owner removes that line. Not a
  Target here.
- After 2.2 lands, the four memories that state the regenerate-and-commit convention
  are rewritten by the implementing session (ids in 2.2).

## P1: Retire the sync mechanisms
`kind: framing`

**Goal**: No gate, hook, or transform compares, attributes, or regenerates the
committed manifest.

### 1.1 Remove the close-time manifest gate and split out the close-task registration [category: code]
`kind: deliverable`

Targets:
- `src/gobby/mcp_proxy/tools/tasks/_lifecycle_close.py::_evaluate_close`
- `src/gobby/mcp_proxy/tools/tasks/_lifecycle_close.py::register_close_task`
- `src/gobby/mcp_proxy/tools/tasks/_lifecycle_close_tool.py`
- `src/gobby/mcp_proxy/tools/tasks/_lifecycle.py::*` — scope-reason: swap the module-level `register_close_task` import to the new tool module
- `tests/mcp_proxy/tools/tasks/test_mcp_close_checklist.py::_evaluate`
- `tests/mcp_proxy/tools/test_task_lifecycle_coverage.py::_complete_close_review`
- `tests/mcp_proxy/tools/test_task_lifecycle_coverage.py::test_close_task_git_helper_calls_follow_repo_path_resolution`
- `tests/mcp_proxy/tools/tasks/test_close_task_flow.py::_committed_manifest_is_current`
- `tests/mcp_proxy/tools/tasks/test_close_task_flow.py::test_stale_committed_bundled_manifest_blocks_close`
- `tests/mcp_proxy/tools/tasks/test_close_task_flow.py::test_blocked_preview_returns_diagnostics_without_commit`
- `tests/mcp_proxy/tools/tasks/test_close_task_flow.py::test_ready_preview_commits_same_evaluation`
- `tests/mcp_proxy/tools/tasks/test_close_task_flow.py::test_concurrent_ordinary_closes_share_review_without_closing_or_releasing_claim`
- `tests/mcp_proxy/tools/tasks/test_close_task_attributed_cleanliness.py::close_harness`
- `tests/mcp_proxy/tools/tasks/test_lifecycle_close_orchestration.py::test_submit_close_review_claims_before_heavy_work`

Gate removal. In `src/gobby/mcp_proxy/tools/tasks/_lifecycle_close.py::_evaluate_close`
delete the block that starts `manifest_check = await
check_linked_committed_bundled_manifest(Path(repo_path), commit_shas)` and returns the
`stale_bundled_content_manifest` failure with its regenerate-and-commit action, so the
gate-7 `evaluation.pass_gate(7, "linked_commits", ...)` follows the
`validate_commit_requirements` check directly. Delete the
`from gobby.install.manifest import (check_linked_committed_bundled_manifest_async as
check_linked_committed_bundled_manifest)` import and `from pathlib import Path`
(line 439 was its only use).

Split. `_lifecycle_close.py` is 960 lines. Move `register_close_task` with its
`close_task` and `submit_close_review` closures and both `registry.register(...)`
calls, unchanged, into the new module
`src/gobby/mcp_proxy/tools/tasks/_lifecycle_close_tool.py`:

```python
"""MCP registration for close_task and submit_close_review."""

from __future__ import annotations

from typing import Any, Literal

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._lifecycle_close import _commit_close, _evaluate_close
from gobby.mcp_proxy.tools.tasks._lifecycle_close_orchestration import (
    active_review_response,
    launch_close_review,
)
from gobby.mcp_proxy.tools.tasks._lifecycle_close_orchestration import (
    submit_close_review as finalize_close_review,
)
from gobby.tasks.generation_schemas import TASK_CLOSE_VALIDATION_SCHEMA


def register_close_task(registry: InternalToolRegistry, ctx: RegistryContext) -> None:
    """Register the checklist-based close_task tool."""
    ...  # body moved verbatim from _lifecycle_close.py
```

In `_lifecycle_close.py` drop the imports that only the registration used
(`InternalToolRegistry`, `active_review_response`, `launch_close_review`,
`finalize_close_review`, `TASK_CLOSE_VALIDATION_SCHEMA`; keep `Literal`, still used by
`_evaluate_close`) and remove `"register_close_task"` from `__all__`. In
`src/gobby/mcp_proxy/tools/tasks/_lifecycle.py` change the import at line 15 to
`from gobby.mcp_proxy.tools.tasks._lifecycle_close_tool import register_close_task`;
`create_lifecycle_registry` keeps calling it. `_evaluate_close` stays the evaluation
engine and keeps every patch point the flow tests rely on
(`resolve_task_id_for_mcp`, `evaluate_criteria_review`, `resolve_task_repo_path`,
`collect_commit_diff_text`, `resolve_close_commit_shas`, ...).

Tests. In `tests/mcp_proxy/tools/tasks/test_close_task_flow.py` delete
`test_stale_committed_bundled_manifest_blocks_close`; in the autouse fixture
`_committed_manifest_is_current` remove the
`patch.object(lifecycle, "check_linked_committed_bundled_manifest", ...)` line and
rename the fixture `_close_gates_are_quiet` (it still patches `collect_commit_paths`
and both `unlinked_tagged_commits`). Import `register_close_task` from
`gobby.mcp_proxy.tools.tasks._lifecycle_close_tool` and add
`import gobby.mcp_proxy.tools.tasks._lifecycle_close_tool as close_tool`. In
`test_blocked_preview_returns_diagnostics_without_commit`,
`test_ready_preview_commits_same_evaluation`, and
`test_concurrent_ordinary_closes_share_review_without_closing_or_releasing_claim`
change `patch.object(lifecycle, "_evaluate_close" | "_commit_close" |
"active_review_response" | "launch_close_review", ...)` to `patch.object(close_tool,
...)` because the closures now resolve those names in the tool module. In
`tests/mcp_proxy/tools/tasks/test_close_task_attributed_cleanliness.py` update the
`register_close_task` import used by `close_harness`. In
`tests/mcp_proxy/tools/tasks/test_lifecycle_close_orchestration.py::test_submit_close_review_claims_before_heavy_work`
change `monkeypatch.setattr(close_module, "_evaluate_close", evaluate_close)` to target
`gobby.mcp_proxy.tools.tasks._lifecycle_close_tool` (import it as `close_tool`), since
the registered `submit_close_review` passes the tool module's `_evaluate_close`.

Consumer re-verification (no edit expected): `test_mcp_close_checklist.py::_evaluate`
and `test_task_lifecycle_coverage.py::_complete_close_review` call `_evaluate_close`
directly with an unchanged signature;
`test_close_task_git_helper_calls_follow_repo_path_resolution` parses the source of
`_evaluate_close` and `_commit_close` for call ordering (`resolve_task_repo_path`
before `resolve_close_commit_shas` and `validate_commit_requirements`), which the
gate deletion does not change. Run them in the validation set.

Validation: `DATABASE_URL=... GOBBY_TEST_PROTECT=1 uv run pytest tests/mcp_proxy/tools/tasks/test_close_task_flow.py tests/mcp_proxy/tools/tasks/test_close_task_attributed_cleanliness.py tests/mcp_proxy/tools/tasks/test_lifecycle_close_orchestration.py tests/mcp_proxy/tools/tasks/test_close_checklist.py tests/mcp_proxy/tools/tasks/test_mcp_close_checklist.py tests/mcp_proxy/tools/test_task_lifecycle_coverage.py -q`,
`uv run ruff check src/ tests/`, `uv run mypy src/`; `wc -l
src/gobby/mcp_proxy/tools/tasks/_lifecycle_close.py` is below 850.

**Acceptance:**

- 1.1.1 - `_evaluate_close` no longer imports from `gobby.install.manifest` and emits no `stale_bundled_content_manifest` failure. symbol: `_evaluate_close`. file: `src/gobby/mcp_proxy/tools/tasks/_lifecycle_close.py`.
- 1.1.2 - `register_close_task` lives in the new tool module and `_lifecycle_close.py` is under 850 lines. symbol: `register_close_task`. file: `src/gobby/mcp_proxy/tools/tasks/_lifecycle_close_tool.py`.
- 1.1.3 - The lifecycle registry imports `register_close_task` from the tool module and still registers `close_task` and `submit_close_review`. file: `src/gobby/mcp_proxy/tools/tasks/_lifecycle.py`.
- 1.1.4 - Close-flow tests pass without patching a manifest checker and with the closure patches on the tool module. test: `tests/mcp_proxy/tools/tasks/test_close_task_flow.py::test_ready_preview_commits_same_evaluation`.
- 1.1.5 - The oversized-review orchestration test passes with its evaluator patch on the tool module. test: `tests/mcp_proxy/tools/tasks/test_lifecycle_close_orchestration.py::test_submit_close_review_claims_before_heavy_work`.

### 1.2 Retire the committed-manifest checkers and the pre-push check [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `src/gobby/install/manifest.py::CommittedManifestCheck`
- `src/gobby/install/manifest.py::check_committed_bundled_content_manifest`
- `src/gobby/install/manifest.py::check_committed_bundled_content_manifest_async`
- `src/gobby/install/manifest.py::check_linked_committed_bundled_manifest`
- `src/gobby/install/manifest.py::check_linked_committed_bundled_manifest_async`
- `src/gobby/install/manifest.py::_committed_shared_files`
- `src/gobby/install/manifest.py::_committed_shared_files_async`
- `src/gobby/install/manifest.py::_shared_files_from_archive`
- `src/gobby/install/manifest.py::_build_committed_bundled_content_manifest`
- `src/gobby/install/manifest.py::_build_committed_bundled_content_manifest_async`
- `src/gobby/install/manifest.py::_git_bytes`
- `src/gobby/install/manifest.py::_git_bytes_async`
- `src/gobby/install/manifest.py::_daemon_git_error_text`
- `src/gobby/install/manifest.py::_git_error_text`
- `src/gobby/install/manifest.py::_manifest_parity_errors`
- `src/gobby/install/manifest.py::main`
- `src/gobby/install/manifest.py::write_bundled_content_manifest`
- `src/gobby/install/manifest.py::_write_bundled_content_manifest`
- `pre-push-test.sh::check_committed_bundled_manifest`
- `tests/install/test_bundled_content_manifest.py::test_current_committed_bundled_content_manifest_matches_git_tree`
- `tests/install/test_bundled_content_manifest.py::test_main_write_round_trips_to_committed_check`
- `tests/install/test_bundled_content_manifest.py::test_main_write_treeish_uses_committed_shared_files`
- `tests/install/test_bundled_content_manifest.py::test_committed_checker_ignores_worktree_and_scopes_linked_commits`
- `tests/install/test_bundled_content_manifest.py::_git`
- `tests/ci/test_pre_push_manifest.py::test_pre_push_runs_committed_bundled_manifest_checker`
- `tests/ci/test_pre_push_manifest.py::test_pre_push_execution_fails_on_stale_committed_bundled_manifest`
- `tests/sync/test_integrity.py::TestVerifyBundledIntegrity.test_non_git_clean_manifest_verifies_cleanly`
- `tests/sync/test_integrity.py::TestVerifyBundledIntegrity.test_non_git_tampered_yaml_maps_to_skipped_content_type`
- `tests/sync/test_integrity.py::TestVerifyBundledIntegrity.test_non_git_unreadable_manifest_file_is_dirty`
- `tests/sync/test_integrity.py::TestVerifyBundledIntegrity.test_non_git_extra_bundled_yaml_is_untracked`

Delete every committed-tree comparison and the CLI entry point from
`src/gobby/install/manifest.py`: `CommittedManifestCheck`,
`check_committed_bundled_content_manifest`, its `_async` twin,
`check_linked_committed_bundled_manifest`, its `_async` twin,
`_committed_shared_files`, `_committed_shared_files_async`, `_shared_files_from_archive`,
`_build_committed_bundled_content_manifest`, its `_async` twin, `_git_bytes`,
`_git_bytes_async`, `_daemon_git_error_text`, `_git_error_text`,
`_manifest_parity_errors`, `main`, and the `if __name__ == "__main__"` block. Fold
`_write_bundled_content_manifest` into `write_bundled_content_manifest` (no other
caller once `main` is gone):

```python
def write_bundled_content_manifest(install_dir: Path) -> Path:
    """Write the packaged bundled-content manifest below *install_dir*."""
    shared_dir = install_dir / MANIFEST_ROOT
    if not shared_dir.is_dir():
        raise FileNotFoundError(f"Shared directory not found: {shared_dir}")
    manifest = build_bundled_content_manifest(shared_dir)
    manifest_path = install_dir / MANIFEST_FILENAME
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest_path
```

Keep `_should_include_relative_path` (still called by `should_include_bundled_file`).
Drop the imports that become unused: `argparse`, `io`, `subprocess`, `sys`,
`tarfile`, `dataclass`, `Iterable`, the `TYPE_CHECKING` `GitResult` import, and the
`_INSTALL_TREE_PATH`, `_SHARED_TREE_PATH`, `_MANIFEST_TREE_PATH` constants. The module
docstring stays accurate. `build_backend/__init__.py` calls
`write_bundled_content_manifest(install_dir)` through `getattr` and is unaffected.
Consumer re-verification (no edit expected): the four `TestVerifyBundledIntegrity`
non-git cases call `write_bundled_content_manifest(tmp_path)` and read the result
through the packaged-install fallback; the folded function keeps the same path,
content, and return value.

In `pre-push-test.sh` remove the `check_committed_bundled_manifest` function, the
`--bundled-manifest-only` early-exit branch, and the ">>> Checking committed
bundled-content manifest..." block that records the `bundled-manifest` command result.
No other pre-push step references it.

Tests: in `tests/install/test_bundled_content_manifest.py` delete
`test_current_committed_bundled_content_manifest_matches_git_tree`,
`test_main_write_round_trips_to_committed_check`,
`test_main_write_treeish_uses_committed_shared_files`,
`test_committed_checker_ignores_worktree_and_scopes_linked_commits`, the `_git` helper,
and the now-unused imports (`check_*`, `main`, `sys`, `subprocess` if unused). Keep
`test_bundled_content_manifest_matches_tree` (leaf 2.1 removes it) and
`test_manifest_membership_matches_wheel`. In `tests/ci/test_pre_push_manifest.py` delete
`test_pre_push_runs_committed_bundled_manifest_checker`,
`test_pre_push_execution_fails_on_stale_committed_bundled_manifest`, and the
`write_bundled_content_manifest` import; `sys` stays (used by `_run`).

Validation: `DATABASE_URL=... GOBBY_TEST_PROTECT=1 uv run pytest tests/install/test_bundled_content_manifest.py tests/ci/test_pre_push_manifest.py tests/sync/test_integrity.py tests/test_build_backend.py -q`,
`uv run ruff check src/ tests/`, `uv run mypy src/`, `bash -n pre-push-test.sh`.

**Acceptance:**

- 1.2.1 - `gobby.install.manifest` exposes no committed-tree checker and no `main`; `python -m gobby.install.manifest` is no longer a command. symbol: `write_bundled_content_manifest`. file: `src/gobby/install/manifest.py`.
- 1.2.2 - `pre-push-test.sh` contains no `check_committed_bundled_manifest` function, no `--bundled-manifest-only` branch, and no `bundled-manifest` record. file: `pre-push-test.sh`.
- 1.2.3 - The remaining manifest tests pass with the checker tests removed. test: `tests/install/test_bundled_content_manifest.py::test_manifest_membership_matches_wheel`.
- 1.2.4 - Pre-push manifest-tool tests pass without the stale-manifest cases. test: `tests/ci/test_pre_push_manifest.py::test_manifest_records_identity_commands_and_success`.

### 1.3 Stop attributing and regenerating the manifest on shared edits [category: code]
`kind: deliverable`

Targets:
- `src/gobby/hooks/event_handlers/_tool.py::ToolEventHandlerMixin.handle_after_tool`
- `src/gobby/hooks/event_handlers/_tool.py::ToolEventHandlerMixin._record_dirty_generated_artifacts`
- `src/gobby/mcp_proxy/tools/tasks/_task_scope.py::evaluate_task_scope_async`
- `src/gobby/install/shared/skills/impeccable/.upgrade/transform.py`
- `src/gobby/install/shared/skills/impeccable/.upgrade/reference_transform.py`
- `src/gobby/install/shared/skills/impeccable/.upgrade/README.md`
- `src/gobby/hooks/event_handlers/__init__.py::*` — scope-reason: consumer re-verification only; `EventHandlers` composes the mixin and binds `handle_after_tool`, no edit expected
- `src/gobby/mcp_proxy/tools/tasks/_lifecycle_close_finalization.py::*` — scope-reason: consumer re-verification only; `commit_close` re-runs `evaluate_task_scope_async` with an unchanged signature, no edit expected
- `tests/hooks/test_tool_handlers.py::TestToolHandlerEdgeCases.test_execute_tracks_dirty_bundled_manifest_for_owned_shared_edit`
- `tests/mcp_proxy/tools/tasks/test_task_scope.py::test_bundled_manifest_in_scope_when_shared_tree_changes`
- `tests/skills/test_upgrade_transform.py::_load_transform`
- `tests/skills/test_upgrade_transform.py::_write_fixture_repo`
- `tests/skills/test_upgrade_transform.py::test_transform_mini_release_idempotent`

Three side effects exist only because the manifest was a committed sibling of the
shared tree. Remove all three.

Hook attribution. In `src/gobby/hooks/event_handlers/_tool.py` delete the module
constant `_GENERATED_ARTIFACT_SOURCES`, the method
`ToolEventHandlerMixin._record_dirty_generated_artifacts`, and the block in
`handle_after_tool` that calls it (the `if not is_failure and self._session_manager and
canonical_tool_kind in {"write", "execute"}` guard with its `try`/`except` logging
"Failed to attribute generated artifacts"). `os`, `SessionVariableManager`,
`active_task_id_for_edit`, and `task_edited_file_set_for_checkout` remain used
elsewhere in the module; `uv run ruff check` confirms.

Scope exemption. In `src/gobby/mcp_proxy/tools/tasks/_task_scope.py::evaluate_task_scope_async`
delete the `if _BUNDLED_CONTENT_MANIFEST in actual_paths and any(_path_is_under(path,
_SHARED_INSTALL_ROOT) ...)` removal and the `_BUNDLED_CONTENT_MANIFEST` and
`_SHARED_INSTALL_ROOT` constants (`_SHARED_INSTALL_ROOT` has no other use;
`_path_is_under` stays, used by `_paths_relevant_to_scope`). An untracked file never
appears in commit paths, and the hook no longer attributes it, so the exemption is dead.

Upgrade transform. In
`src/gobby/install/shared/skills/impeccable/.upgrade/transform.py::_prepare_upgrade`
delete the mirror block (lines 721 to 735: `mirror = workspace / "shared"`, the
`copytree`, the `rmtree` and `copytree` of scripts, the loop that writes `writes` into
the mirror, and `writes[install_dir / "bundled_content_manifest.json"] =
manifest_bytes`) and the `from gobby.install.manifest import
build_bundled_content_manifest` import. `_canonical_json` stays (used by
`_package_json`); `shutil` stays (used by `_replace_scripts`). The report's
`changed_paths` then covers the skill, notice, dependency, lock, reference, and script
files only. In the sibling `README.md` step 4 ("Pins and manifest") drop "and the
bundled-content manifest" and the sentence "The manifest excludes every leading-dot
path component, matching packaged wheel membership."; retitle the step "Pins".

Split. `transform.py` is 851 lines. Move the reference-transform group into the new
sibling module `src/gobby/install/shared/skills/impeccable/.upgrade/reference_transform.py`:
`UpgradeRejected`, the constants `REFERENCE_FRONTMATTER_RE`, `CATALOGUE_ROW_RE`,
`ADDITIONAL_CONTEXT_RE`, `MARKDOWN_REFERENCE_RE`, `COMMAND_REFERENCE_RE`,
`BACKTICK_COMMAND_REFERENCE_RE`, `PLACEHOLDER_RE`, `PREAMBLE`, `SCRIPTS_RESOLVER`,
`AVAILABLE_COMMANDS`, `CURATED_REFERENCE_PATHS`, and the functions `_parse_catalogue`
(renamed `parse_catalogue`), `_reference_loader`, `_transform_prose`, `_add_resolver`,
`mechanical_reference_transform`, and `_reference_plan` (renamed `reference_plan`),
bodies unchanged, with the `difflib`, `re`, `Mapping`, and `Path` imports they need.
`transform.py` then imports them as siblings:

```python
from reference_transform import (
    UpgradeRejected,
    mechanical_reference_transform,
    parse_catalogue,
    reference_plan,
)
```

The README's documented invocation runs the file as a script
(`uv run python src/.../.upgrade/transform.py 3.5.0`), which puts `.upgrade/` first on
`sys.path`, so the sibling import resolves in production without a package or
`sys.path` edit. `transform.py` keeps re-exporting `UpgradeRejected` through that
import so `module.UpgradeRejected` in tests is unchanged. `_prepare_upgrade` calls
`parse_catalogue(notice_source)` and `reference_plan(...)` at the former lines 676 to
677. Drop `difflib` and `Mapping` from `transform.py` if nothing else uses them.

Tests: delete
`tests/hooks/test_tool_handlers.py::TestToolHandlerEdgeCases::test_execute_tracks_dirty_bundled_manifest_for_owned_shared_edit`
and `tests/mcp_proxy/tools/tasks/test_task_scope.py::test_bundled_manifest_in_scope_when_shared_tree_changes`.
In `tests/skills/test_upgrade_transform.py::_load_transform` insert
`str(TRANSFORM_PATH.parent)` at the front of `sys.path` (once, guarded by a membership
check) before `spec.loader.exec_module(module)` so the sibling import resolves under
`spec_from_file_location`. In `_write_fixture_repo` remove the `_write_json(install_dir
/ "bundled_content_manifest.json", {...})` write, and in
`test_transform_mini_release_idempotent` remove the `manifest = json.loads((repo_root /
... / "bundled_content_manifest.json").read_text())` assertion block (`assert
manifest["files"]` and the dot-part check); the remaining assertions on the scripts
directory, `package.json`, lockfile, dependency pins, and `judgment_needed` are
unchanged.

Validation: `DATABASE_URL=... GOBBY_TEST_PROTECT=1 uv run pytest tests/hooks/test_tool_handlers.py tests/mcp_proxy/tools/tasks/test_task_scope.py tests/skills/test_upgrade_transform.py -q`,
`uv run ruff check src/ tests/`, `uv run mypy src/`, and
`uv run mypy "src/gobby/install/shared/skills/impeccable/.upgrade/transform.py" "src/gobby/install/shared/skills/impeccable/.upgrade/reference_transform.py"`
(the standalone check passes today on `transform.py`); `wc -l` on `transform.py` is
below 850.

**Acceptance:**

- 1.3.1 - `_tool.py` defines no `_GENERATED_ARTIFACT_SOURCES` and no `_record_dirty_generated_artifacts`; `handle_after_tool` records only the paths the tool itself wrote. symbol: `ToolEventHandlerMixin.handle_after_tool`. file: `src/gobby/hooks/event_handlers/_tool.py`.
- 1.3.2 - `evaluate_task_scope_async` performs no manifest-path removal and the module defines no `_BUNDLED_CONTENT_MANIFEST`. symbol: `evaluate_task_scope_async`. file: `src/gobby/mcp_proxy/tools/tasks/_task_scope.py`.
- 1.3.3 - The impeccable upgrade transform neither imports `gobby.install.manifest` nor writes `bundled_content_manifest.json`, and its reference-transform group lives in the sibling module. file: `src/gobby/install/shared/skills/impeccable/.upgrade/reference_transform.py`.
- 1.3.4 - The upgrade README no longer lists the manifest as a transform output. behavior: "Pins" step without a manifest sentence in `src/gobby/install/shared/skills/impeccable/.upgrade/README.md`.
- 1.3.5 - Hook and scope test modules pass with the manifest cases removed. test: `tests/hooks/test_tool_handlers.py::TestToolHandlerEdgeCases::test_after_tool_shell_non_edits_skip_tracking`.
- 1.3.6 - The upgrade transform test passes without a manifest fixture or assertion and with the sibling import resolving. test: `tests/skills/test_upgrade_transform.py::test_transform_mini_release_idempotent`.

## P2: Untrack the file and fix the guidance
`kind: framing`

**Goal**: The manifest is a build artifact: ignored by Git, generated by the build
backend, absent from every test that read the committed copy.

### 2.1 Untrack and ignore the manifest [category: config] (depends: 1.2, 1.3)
`kind: deliverable`

Targets:
- `src/gobby/install/bundled_content_manifest.json::*` — scope-reason: remove the whole generated file from the Git index; the on-disk copy is not edited
- `.gitignore`
- `tests/install/test_bundled_content_manifest.py::test_bundled_content_manifest_matches_tree`
- `tests/test_build_backend.py::test_committed_bundled_content_manifest_matches_shared_tree`
- `tests/agents/test_plan_adversary_internal_research_definition.py::test_removed_researcher_is_absent_from_inventory_and_manifest`
- `tests/skills/test_pipelines_and_cron_skill.py::test_bundled_manifest_tracks_pipelines_and_cron`
- `tests/skills/test_repository_maintenance_skill.py::test_bundled_manifest_registers_repository_maintenance`

Run `git rm --cached src/gobby/install/bundled_content_manifest.json` so the working
copy stays on disk for any local packaged-install experiment but leaves the index. Add
to `.gitignore`, next to the existing "Built web UI assets staged into the wheel"
entry:

```gitignore
# Bundled-content manifest generated by build_backend for wheels and sdists
src/gobby/install/bundled_content_manifest.json
```

`MANIFEST.in` and the `pyproject.toml` package-data entry stay: setuptools reads the
staged file from disk at build time, and `build_sdist` writes it before packing.

Delete every test that reads the committed manifest from the repository root:
`tests/install/test_bundled_content_manifest.py::test_bundled_content_manifest_matches_tree`
(and the `json` import if unused);
`tests/test_build_backend.py::test_committed_bundled_content_manifest_matches_shared_tree`
plus its `build_bundled_content_manifest` import (line 528 was its only use);
`tests/skills/test_pipelines_and_cron_skill.py::test_bundled_manifest_tracks_pipelines_and_cron`
plus the `hash_file_bytes` import and `json` if unused;
`tests/skills/test_repository_maintenance_skill.py::test_bundled_manifest_registers_repository_maintenance`
plus the `MANIFEST_FILE` constant and `json` if unused. In
`tests/agents/test_plan_adversary_internal_research_definition.py` rename
`test_removed_researcher_is_absent_from_inventory_and_manifest` to
`test_removed_researcher_is_absent_from_inventory`, keep the
`assert not (AGENTS_DIR / f"{REMOVED_RESEARCHER}.yaml").exists()` line, drop the
manifest assertion, `MANIFEST_PATH`, and `json` if unused.

Validation: `git ls-files src/gobby/install/bundled_content_manifest.json` prints
nothing; `git check-ignore src/gobby/install/bundled_content_manifest.json` prints the
path; `DATABASE_URL=... GOBBY_TEST_PROTECT=1 uv run pytest tests/install/test_bundled_content_manifest.py tests/test_build_backend.py tests/agents/test_plan_adversary_internal_research_definition.py tests/skills/test_pipelines_and_cron_skill.py tests/skills/test_repository_maintenance_skill.py -q`
(the wheel-membership test builds a real sdist and wheel from a `git ls-files` copy and
proves the manifest is generated with no committed copy); `uv run ruff check tests/`.

**Acceptance:**

- 2.1.1 - The manifest is not tracked and is ignored. file: `.gitignore`. behavior: "git ls-files prints nothing for the path and git check-ignore matches it" in `.gitignore`.
- 2.1.2 - The committed-copy parity tests are gone and the build still produces a manifest that matches the packaged shared tree. test: `tests/install/test_bundled_content_manifest.py::test_manifest_membership_matches_wheel`.
- 2.1.3 - Build backend tests pass without a repo-root manifest read. test: `tests/test_build_backend.py::test_stage_bundled_content_manifest_writes_manifest`.
- 2.1.4 - The adversary, pipelines, and repository-maintenance skill tests pass without a repo-root manifest read. test: `tests/agents/test_plan_adversary_internal_research_definition.py::test_removed_researcher_is_absent_from_inventory`.

### 2.2 Replace the regenerate-and-commit guidance [category: docs] (depends: 2.1)
`kind: deliverable`

Targets:
- `src/gobby/install/shared/skills/repository-maintenance/SKILL.md`

In the "Gobby Examples" list of
`src/gobby/install/shared/skills/repository-maintenance/SKILL.md` replace the bullet
that says the manifest "is a generated inventory. Change the shared source, run
`uv run python -m gobby.install.manifest --write`, and verify the regenerated output
instead of hand-maintaining hashes." with:

```markdown
- `src/gobby/install/bundled_content_manifest.json` is a build artifact: ignored by
  Git and written by `build_backend` into every wheel and sdist. Change the shared
  source only; never generate, hand-edit, or commit the manifest.
```

The scenario `tests/skills/scenarios/repository-maintenance/generated-content.yaml`
is generic (regenerate separated content from its source of truth) and stays.
`tests/skills/test_repository_maintenance_skill.py` asserts frontmatter and scenario
action order, not this bullet, so it needs no change beyond 2.1.

After the commit lands, rewrite the memories that still instruct regenerating and
committing the manifest so they state the new convention (untracked build artifact,
never regenerate or commit): `24aa9ab3-e956-5e21-b788-74133821d226`,
`3a4e8d7e-7ebb-5628-8719-7830ae821ae2`, `125ac8f3-92f5-54af-ac28-cd4131a967fd`, and the
packaging fact `3d3b86b7-422b-56bf-b33d-48739ddd69e4`, via `gobby-memory` update tools.

Validation: `gcode grep -F "manifest --write" src/gobby/install/shared/ docs/` returns
nothing; `DATABASE_URL=... GOBBY_TEST_PROTECT=1 uv run pytest tests/skills/test_repository_maintenance_skill.py -q`.

**Acceptance:**

- 2.2.1 - The repository-maintenance skill describes the manifest as an ignored build artifact and no bundled skill or doc instructs running `gobby.install.manifest --write`. behavior: "never generate, hand-edit, or commit the manifest" in `src/gobby/install/shared/skills/repository-maintenance/SKILL.md`.
- 2.2.2 - The skill still loads and validates. test: `tests/skills/test_repository_maintenance_skill.py::test_skill_is_public_on_demand_and_discoverable_to_every_agent`.

## E1 End-to-end verification
`kind: verification`

From a fresh clone of the merged branch: `git ls-files` does not list the manifest;
`uv build --sdist` then `uv build --wheel <sdist>` produces a wheel whose
`gobby/install/bundled_content_manifest.json` equals
`build_bundled_content_manifest(<wheel>/gobby/install/shared)`
(`tests/install/test_bundled_content_manifest.py::test_manifest_membership_matches_wheel`);
`bash pre-push-test.sh` has no bundled-manifest step; two sessions editing different
bundled skills in one checkout each commit and close their own task without touching
or being blocked by the manifest, because nothing dirties it.

## Task Mapping
`kind: framing`

<!-- Updated after task creation -->
| Plan Item | Task Ref | Status |
|-----------|----------|--------|

## M1 Task Manifest
`kind: manifest`

```yaml
- title: Remove the close-time manifest gate and split out the close-task registration
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: '1.1.1: `_evaluate_close` no longer imports from `gobby.install.manifest`
    and emits no `stale_bundled_content_manifest` failure. symbol: `_evaluate_close`.
    file: `src/gobby/mcp_proxy/tools/tasks/_lifecycle_close.py`.

    1.1.2: `register_close_task` lives in the new tool module and `_lifecycle_close.py`
    is under 850 lines. symbol: `register_close_task`. file: `src/gobby/mcp_proxy/tools/tasks/_lifecycle_close_tool.py`.

    1.1.3: The lifecycle registry imports `register_close_task` from the tool module
    and still registers `close_task` and `submit_close_review`. file: `src/gobby/mcp_proxy/tools/tasks/_lifecycle.py`.

    1.1.4: Close-flow tests pass without patching a manifest checker and with the
    closure patches on the tool module. test: `tests/mcp_proxy/tools/tasks/test_close_task_flow.py::test_ready_preview_commits_same_evaluation`.

    1.1.5: The oversized-review orchestration test passes with its evaluator patch
    on the tool module. test: `tests/mcp_proxy/tools/tasks/test_lifecycle_close_orchestration.py::test_submit_close_review_claims_before_heavy_work`.'
  labels:
  - covers:untrack-bundled-content-manifest:1.1:1.1.1
  - covers:untrack-bundled-content-manifest:1.1:1.1.2
  - covers:untrack-bundled-content-manifest:1.1:1.1.3
  - covers:untrack-bundled-content-manifest:1.1:1.1.4
  - covers:untrack-bundled-content-manifest:1.1:1.1.5
  tdd: true
  source_section: '1.1'
  implementation_domain: backend
- title: Retire the committed-manifest checkers and the pre-push check
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  validation_criteria: '1.2.1: `gobby.install.manifest` exposes no committed-tree
    checker and no `main`; `python -m gobby.install.manifest` is no longer a command.
    symbol: `write_bundled_content_manifest`. file: `src/gobby/install/manifest.py`.

    1.2.2: `pre-push-test.sh` contains no `check_committed_bundled_manifest` function,
    no `--bundled-manifest-only` branch, and no `bundled-manifest` record. file: `pre-push-test.sh`.

    1.2.3: The remaining manifest tests pass with the checker tests removed. test:
    `tests/install/test_bundled_content_manifest.py::test_manifest_membership_matches_wheel`.

    1.2.4: Pre-push manifest-tool tests pass without the stale-manifest cases. test:
    `tests/ci/test_pre_push_manifest.py::test_manifest_records_identity_commands_and_success`.'
  labels:
  - covers:untrack-bundled-content-manifest:1.2:1.2.1
  - covers:untrack-bundled-content-manifest:1.2:1.2.2
  - covers:untrack-bundled-content-manifest:1.2:1.2.3
  - covers:untrack-bundled-content-manifest:1.2:1.2.4
  tdd: true
  source_section: '1.2'
  implementation_domain: backend
- title: Stop attributing and regenerating the manifest on shared edits
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: '1.3.1: `_tool.py` defines no `_GENERATED_ARTIFACT_SOURCES`
    and no `_record_dirty_generated_artifacts`; `handle_after_tool` records only the
    paths the tool itself wrote. symbol: `ToolEventHandlerMixin.handle_after_tool`.
    file: `src/gobby/hooks/event_handlers/_tool.py`.

    1.3.2: `evaluate_task_scope_async` performs no manifest-path removal and the module
    defines no `_BUNDLED_CONTENT_MANIFEST`. symbol: `evaluate_task_scope_async`. file:
    `src/gobby/mcp_proxy/tools/tasks/_task_scope.py`.

    1.3.3: The impeccable upgrade transform neither imports `gobby.install.manifest`
    nor writes `bundled_content_manifest.json`, and its reference-transform group
    lives in the sibling module. file: `src/gobby/install/shared/skills/impeccable/.upgrade/reference_transform.py`.

    1.3.4: The upgrade README no longer lists the manifest as a transform output.
    behavior: "Pins" step without a manifest sentence in `src/gobby/install/shared/skills/impeccable/.upgrade/README.md`.

    1.3.5: Hook and scope test modules pass with the manifest cases removed. test:
    `tests/hooks/test_tool_handlers.py::TestToolHandlerEdgeCases::test_after_tool_shell_non_edits_skip_tracking`.

    1.3.6: The upgrade transform test passes without a manifest fixture or assertion
    and with the sibling import resolving. test: `tests/skills/test_upgrade_transform.py::test_transform_mini_release_idempotent`.'
  labels:
  - covers:untrack-bundled-content-manifest:1.3:1.3.1
  - covers:untrack-bundled-content-manifest:1.3:1.3.2
  - covers:untrack-bundled-content-manifest:1.3:1.3.3
  - covers:untrack-bundled-content-manifest:1.3:1.3.4
  - covers:untrack-bundled-content-manifest:1.3:1.3.5
  - covers:untrack-bundled-content-manifest:1.3:1.3.6
  tdd: true
  source_section: '1.3'
  implementation_domain: backend
- title: Untrack and ignore the manifest
  category: config
  task_type: feature
  depends_on:
  - '1.2'
  - '1.3'
  validation_criteria: '2.1.1: The manifest is not tracked and is ignored. file: `.gitignore`.
    behavior: "git ls-files prints nothing for the path and git check-ignore matches
    it" in `.gitignore`.

    2.1.2: The committed-copy parity tests are gone and the build still produces a
    manifest that matches the packaged shared tree. test: `tests/install/test_bundled_content_manifest.py::test_manifest_membership_matches_wheel`.

    2.1.3: Build backend tests pass without a repo-root manifest read. test: `tests/test_build_backend.py::test_stage_bundled_content_manifest_writes_manifest`.

    2.1.4: The adversary, pipelines, and repository-maintenance skill tests pass without
    a repo-root manifest read. test: `tests/agents/test_plan_adversary_internal_research_definition.py::test_removed_researcher_is_absent_from_inventory`.'
  labels:
  - covers:untrack-bundled-content-manifest:2.1:2.1.1
  - covers:untrack-bundled-content-manifest:2.1:2.1.2
  - covers:untrack-bundled-content-manifest:2.1:2.1.3
  - covers:untrack-bundled-content-manifest:2.1:2.1.4
  tdd: true
  source_section: '2.1'
  assigned_agent: backend-developer
- title: Replace the regenerate-and-commit guidance
  category: docs
  task_type: feature
  depends_on:
  - '2.1'
  validation_criteria: '2.2.1: The repository-maintenance skill describes the manifest
    as an ignored build artifact and no bundled skill or doc instructs running `gobby.install.manifest
    --write`. behavior: "never generate, hand-edit, or commit the manifest" in `src/gobby/install/shared/skills/repository-maintenance/SKILL.md`.

    2.2.2: The skill still loads and validates. test: `tests/skills/test_repository_maintenance_skill.py::test_skill_is_public_on_demand_and_discoverable_to_every_agent`.'
  labels:
  - covers:untrack-bundled-content-manifest:2.2:2.2.1
  - covers:untrack-bundled-content-manifest:2.2:2.2.2
  tdd: false
  source_section: '2.2'
  assigned_agent: tech-writer
```
